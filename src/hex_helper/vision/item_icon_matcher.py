"""ACE-safe pure read-only visual HUD item slot matcher for 2999-unavailable Plan B sync.

Zero process hooks, zero memory reads — strictly operates on captured BGR screen frames and
local DDragon item PNG icons (`<cache_dir>/icons/item/*.png`) using 28x28 normalized template
correlation (`TM_CCOEFF_NORMED`, 0.65 weight) + HSV hue/saturation histogram correlation (0.35 weight).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import cv2
import numpy as np

from hex_helper.vision.augment_icon_matcher import (
    HIST_WEIGHT,
    TEMPLATE_SIZE,
    TEMPLATE_WEIGHT,
    _compute_descriptor,
    _decode_to_bgr,
    _extract_patch_windows_26,
    _normalize_rows_zero_mean,
    _normalize_vec_zero_mean,
    _read_image_unicode,
    is_valid_frame,
)
from hex_helper.vision.capture import WindowRect

logger = logging.getLogger(__name__)

# Ignore trinkets, Poro-Snax, and ARAM Mayhem stat anvils
IGNORED_HUD_ITEM_IDS: frozenset[str] = frozenset(
    {"2052", "220013", "3340", "3363", "3364", "1402", "1403", "1404", "0"}
)

# Height-anchored bottom-HUD 2x3 item grid offsets at GlobalScale = 1.0 (empirically calibrated on live 2560x1440 LoL HUD).
# Each tuple: (dx_from_center_over_h, dy_up_from_bottom_over_h, side_over_h)
DEFAULT_HUD_ITEM_SLOT_OFFSETS_H: tuple[tuple[float, float, float], ...] = (
    (0.1771, 0.1049, 0.0382),  # Slot 0 (top-left, key 1)
    (0.2222, 0.1049, 0.0382),  # Slot 1 (top-mid, key 2)
    (0.2674, 0.1049, 0.0382),  # Slot 2 (top-right, key 3)
    (0.1771, 0.0611, 0.0382),  # Slot 3 (bottom-left, key 5)
    (0.2222, 0.0611, 0.0382),  # Slot 4 (bottom-mid, key 6)
    (0.2674, 0.0611, 0.0382),  # Slot 5 (bottom-right, key 7)
)


def read_lol_hud_scale(lol_install_dir: str = "", lockfile_path: str = "") -> float:
    """Read League of Legends `GlobalScale` (0.0..1.0) from PersistedSettings.json safely."""
    candidates: list[Path] = []
    for raw in (lol_install_dir, lockfile_path):
        text = str(raw or "").strip()
        if not text:
            continue
        p = Path(text)
        for root in (p, p.parent, p.parent.parent):
            candidates.append(root / "Game" / "Config" / "PersistedSettings.json")
            candidates.append(root / "Config" / "PersistedSettings.json")

    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for file_entry in data.get("files") or []:
            if not isinstance(file_entry, dict):
                continue
            for section in file_entry.get("sections") or []:
                if not isinstance(section, dict):
                    continue
                for setting in section.get("settings") or []:
                    if not isinstance(setting, dict):
                        continue
                    if str(setting.get("name") or "") == "GlobalScale":
                        try:
                            val = float(setting.get("value"))
                            return max(0.0, min(1.0, val))
                        except (TypeError, ValueError):
                            pass
    return 1.0


@dataclass(frozen=True)
class ItemMatchResult:
    item_id: str
    score: float
    margin: float = 0.0
    hud_slot_index: int = -1
    template_score: float = 0.0
    hist_score: float = 0.0
    patch_std: float = 0.0
    template_count: int = 0


@dataclass(frozen=True)
class _ItemTemplate:
    item_id: str
    gray_28: np.ndarray
    gray_core_26: np.ndarray
    gray_zoom_26: np.ndarray
    hsv_hist: np.ndarray
    gray_top_16: np.ndarray
    hsv_hist_top: np.ndarray


def _compute_top_descriptor(bgr_img: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Compute top-58% unobstructed art descriptor immune to bottom-half stack numbers (e.g. Heartsteel 1374)."""
    bgr = _decode_to_bgr(bgr_img)
    if bgr is None or bgr.shape[0] < 6 or bgr.shape[1] < 6:
        return None
    interp = cv2.INTER_AREA if (bgr.shape[0] >= TEMPLATE_SIZE and bgr.shape[1] >= TEMPLATE_SIZE) else cv2.INTER_CUBIC
    bgr_28 = cv2.resize(bgr, (TEMPLATE_SIZE, TEMPLATE_SIZE), interpolation=interp)
    gray_28 = cv2.cvtColor(bgr_28, cv2.COLOR_BGR2GRAY)
    gray_top_16 = gray_28[1:17, 1:27].copy()

    hsv_top = cv2.cvtColor(bgr_28[:17, :], cv2.COLOR_BGR2HSV)
    h_ch = hsv_top[:, :, 0].copy()
    s_ch = hsv_top[:, :, 1]
    h_ch[s_ch < 20] = 0
    hs_top = np.stack((h_ch, s_ch), axis=-1)
    hist_top = cv2.calcHist([hs_top], [0, 1], None, [12, 6], [0, 180, 0, 256]).astype(np.float32)
    hsum = float(hist_top.sum())
    if hsum > 0.0:
        hist_top /= hsum
    return gray_top_16, hist_top


def _extract_patch_windows_top16(patch_gray_28: np.ndarray) -> np.ndarray:
    """Extract all 12 shifted 16x26 windows from patch_gray_28[:19, :] as a (416, 12) normalized float32 matrix."""
    top_band = patch_gray_28[:19, :]
    rows = np.empty((12, 16 * 26), dtype=np.float32)
    idx = 0
    for dy in range(4):
        for dx in range(3):
            rows[idx] = top_band[dy : dy + 16, dx : dx + 26].reshape(-1)
            idx += 1
    normed = _normalize_rows_zero_mean(rows, min_std=1e-3)
    return np.ascontiguousarray(normed.T, dtype=np.float32)


class ItemIconMatcher:
    """Matches the 6 bottom-HUD item slots against cached DDragon item icon PNGs."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        slot_offsets_h: Sequence[tuple[float, float, float]] | None = None,
        edge_trim_ratio: float = 0.08,
    ) -> None:
        self.cache_dir: Path | None = Path(cache_dir) if cache_dir is not None else None
        self.icon_dir: Path | None = (self.cache_dir / "icons" / "item") if self.cache_dir is not None else None
        self._slot_offsets_h = tuple(slot_offsets_h if slot_offsets_h is not None else DEFAULT_HUD_ITEM_SLOT_OFFSETS_H)
        self._edge_trim_ratio = max(0.0, min(0.20, float(edge_trim_ratio)))
        self._templates: dict[str, _ItemTemplate] = {}
        self._last_scan_diag: dict[str, Any] = {}

        # Pre-stacked zero-mean L2-normalized float32 matrices for single-BLAS matmul matching
        self._matrix_ids: list[str] = []
        self._id_to_row: dict[str, int] = {}
        self._T_core: np.ndarray | None = None
        self._T_zoom: np.ndarray | None = None
        self._T_top: np.ndarray | None = None
        self._H_hist: np.ndarray | None = None
        self._H_hist_top: np.ndarray | None = None
        self._matrices_dirty: bool = True
        self._matrix_version: int = 0
        # Per-slot lightweight 16x16 grayscale frame-diff cache
        self._slot_cache: dict[int, tuple[np.ndarray, tuple[Any, ...], str, ItemMatchResult | None, float]] = {}

        self._load_cached_icons()

    @property
    def template_count(self) -> int:
        return len(self._templates)

    def last_scan_diagnostics(self) -> dict[str, Any]:
        return dict(self._last_scan_diag)

    def clear_slot_cache(self) -> None:
        """Clear per-slot 16x16 frame-diff cache (e.g., on match reset or template reload)."""
        self._slot_cache.clear()

    def _ensure_matrices(self) -> None:
        """Lazily build pre-stacked zero-mean L2-normalized float32 template matrices for BLAS matmul."""
        if (
            not self._matrices_dirty
            and self._T_core is not None
            and len(self._matrix_ids) == len(self._templates)
        ):
            return
        ids = list(self._templates.keys())
        self._matrix_ids = ids
        self._id_to_row = {iid: i for i, iid in enumerate(ids)}
        if not ids:
            self._T_core = np.zeros((0, 26 * 26), dtype=np.float32)
            self._T_zoom = np.zeros((0, 26 * 26), dtype=np.float32)
            self._T_top = np.zeros((0, 16 * 26), dtype=np.float32)
            self._H_hist = np.zeros((0, 72), dtype=np.float32)
            self._H_hist_top = np.zeros((0, 72), dtype=np.float32)
        else:
            core_raw = np.stack([self._templates[iid].gray_core_26.reshape(-1) for iid in ids], axis=0)
            zoom_raw = np.stack([self._templates[iid].gray_zoom_26.reshape(-1) for iid in ids], axis=0)
            top_raw = np.stack([self._templates[iid].gray_top_16.reshape(-1) for iid in ids], axis=0)
            hist_raw = np.stack([self._templates[iid].hsv_hist.reshape(-1) for iid in ids], axis=0)
            hist_top_raw = np.stack([self._templates[iid].hsv_hist_top.reshape(-1) for iid in ids], axis=0)
            self._T_core = _normalize_rows_zero_mean(core_raw, min_std=1e-3)
            self._T_zoom = _normalize_rows_zero_mean(zoom_raw, min_std=1e-3)
            self._T_top = _normalize_rows_zero_mean(top_raw, min_std=1e-3)
            self._H_hist = _normalize_rows_zero_mean(hist_raw, min_norm=1e-6)
            self._H_hist_top = _normalize_rows_zero_mean(hist_top_raw, min_norm=1e-6)
        self._matrices_dirty = False
        self._matrix_version += 1

    def reload_cached_icons(self) -> int:
        prev_count = len(self._templates)
        self._load_cached_icons()
        if len(self._templates) != prev_count:
            self.clear_slot_cache()
        return self.template_count

    def _load_cached_icons(self) -> None:
        if self.icon_dir is None or not self.icon_dir.exists():
            return
        try:
            png_files = sorted(self.icon_dir.glob("*.png"))
        except Exception:
            return
        for png in png_files:
            iid = png.stem.strip()
            if not iid or iid in IGNORED_HUD_ITEM_IDS or iid in self._templates:
                continue
            img = _read_image_unicode(png)
            if img is not None:
                self.register_template(iid, img)

    def register_template(self, item_id: str, bgr_img: np.ndarray) -> bool:
        iid = str(item_id).strip()
        if not iid or iid in IGNORED_HUD_ITEM_IDS:
            return False
        desc = _compute_descriptor(bgr_img)
        top_desc = _compute_top_descriptor(bgr_img)
        if desc is None or top_desc is None:
            return False
        gray_28, gray_core_26, gray_zoom_26, hsv_hist = desc
        gray_top_16, hsv_hist_top = top_desc
        self._templates[iid] = _ItemTemplate(
            item_id=iid,
            gray_28=gray_28,
            gray_core_26=gray_core_26,
            gray_zoom_26=gray_zoom_26,
            hsv_hist=hsv_hist,
            gray_top_16=gray_top_16,
            hsv_hist_top=hsv_hist_top,
        )
        self._matrices_dirty = True
        self._slot_cache.clear()
        return True

    @staticmethod
    def _resolve_game_view(frame_bgr: np.ndarray, game_rect: WindowRect | None) -> np.ndarray:
        fh, fw = frame_bgr.shape[:2]
        if (
            game_rect is not None
            and game_rect.width > 0
            and game_rect.height > 0
            and (fw > game_rect.width or fh > game_rect.height)
            and game_rect.left >= 0
            and game_rect.top >= 0
            and game_rect.right <= fw
            and game_rect.bottom <= fh
        ):
            return frame_bgr[game_rect.top : game_rect.bottom, game_rect.left : game_rect.right]
        return frame_bgr

    def get_hud_item_slot_rects(
        self,
        frame_width: int,
        frame_height: int,
        *,
        hud_scale: float = 1.0,
    ) -> list[tuple[int, tuple[int, int, int, int]]]:
        """Compute pixel bounding boxes for the 6 bottom-HUD item slots anchored on bottom-center & height."""
        fw = max(1, int(frame_width))
        fh = max(1, int(frame_height))
        scale = 0.5 + 0.5 * max(0.0, min(1.0, float(hud_scale)))
        rects: list[tuple[int, tuple[int, int, int, int]]] = []
        for idx, (dx_h, dy_h, side_h) in enumerate(self._slot_offsets_h):
            cx = (fw * 0.5) + (dx_h * fh * scale)
            cy = fh - (dy_h * fh * scale)
            side = max(10, int(round(side_h * fh * scale)))
            half = side * 0.5
            x0 = max(0, int(round(cx - half)))
            y0 = max(0, int(round(cy - half)))
            x1 = min(fw, x0 + side)
            y1 = min(fh, y0 + side)
            if x1 > x0 + 6 and y1 > y0 + 6:
                rects.append((idx, (x0, y0, x1, y1)))
        return rects

    def extract_hud_item_patches(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect | None = None,
        *,
        hud_scale: float = 1.0,
    ) -> list[tuple[int, np.ndarray]]:
        if not is_valid_frame(frame_bgr):
            return []
        view = self._resolve_game_view(frame_bgr, game_rect)
        vh, vw = view.shape[:2]
        if vh < 64 or vw < 64:
            return []
        patches: list[tuple[int, np.ndarray]] = []
        for slot_idx, (x0, y0, x1, y1) in self.get_hud_item_slot_rects(vw, vh, hud_scale=hud_scale):
            crop = view[y0:y1, x0:x1]
            ch, cw = crop.shape[:2]
            if ch < 8 or cw < 8:
                continue
            side = min(ch, cw)
            if ch != cw:
                sy = (ch - side) // 2
                sx = (cw - side) // 2
                crop = crop[sy : sy + side, sx : sx + side]
            if side >= 20 and self._edge_trim_ratio > 0.0:
                trim = min(int(round(side * self._edge_trim_ratio)), (side - 8) // 2)
                if trim > 0:
                    crop = crop[trim : side - trim, trim : side - trim]
            if crop.size > 0 and crop.shape[0] >= 8 and crop.shape[1] >= 8:
                patches.append((slot_idx, crop.copy()))
        return patches

    @staticmethod
    def _score_against_template(
        patch_gray_28: np.ndarray,
        patch_hist: np.ndarray,
        patch_hist_top: np.ndarray | None,
        tmpl: _ItemTemplate,
    ) -> tuple[float, float, float]:
        if float(np.std(tmpl.gray_core_26)) < 1e-3:
            tmpl_score = 0.0
        else:
            res = cv2.matchTemplate(patch_gray_28, tmpl.gray_core_26, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(res)
            tmpl_score = 0.0 if math.isnan(max_val) else max(0.0, min(1.0, float(max_val)))
            if tmpl_score < 0.88 and float(np.std(tmpl.gray_zoom_26)) >= 1e-3:
                res_z = cv2.matchTemplate(patch_gray_28, tmpl.gray_zoom_26, cv2.TM_CCOEFF_NORMED)
                _, max_z, _, _ = cv2.minMaxLoc(res_z)
                if not math.isnan(max_z):
                    tmpl_score = max(tmpl_score, max(0.0, min(1.0, float(max_z))))
        hist_corr = float(cv2.compareHist(patch_hist, tmpl.hsv_hist, cv2.HISTCMP_CORREL))
        hist_score = 0.0 if math.isnan(hist_corr) else max(0.0, min(1.0, hist_corr))
        full_combined = TEMPLATE_WEIGHT * tmpl_score + HIST_WEIGHT * hist_score

        # Top-58% unobstructed art score: rescues stack-counter items (Heartsteel 1374, RoA, Tear, Mejai's)
        top_combined = 0.0
        if patch_hist_top is not None and float(np.std(tmpl.gray_top_16)) >= 1e-3:
            res_t = cv2.matchTemplate(patch_gray_28[:19, :], tmpl.gray_top_16, cv2.TM_CCOEFF_NORMED)
            _, max_t, _, _ = cv2.minMaxLoc(res_t)
            top_tmpl = 0.0 if math.isnan(max_t) else max(0.0, min(1.0, float(max_t)))
            top_hcorr = float(cv2.compareHist(patch_hist_top, tmpl.hsv_hist_top, cv2.HISTCMP_CORREL))
            top_hist = 0.0 if math.isnan(top_hcorr) else max(0.0, min(1.0, top_hcorr))
            top_combined = (TEMPLATE_WEIGHT * top_tmpl + HIST_WEIGHT * top_hist) * 0.96

        if top_combined > full_combined:
            return top_combined, max(tmpl_score, top_tmpl), max(hist_score, top_hist)
        return full_combined, tmpl_score, hist_score

    @staticmethod
    def is_empty_slot_patch(patch_bgr: np.ndarray, *, min_patch_std: float = 8.0) -> bool:
        """Return True only when the HUD slot patch is genuinely an empty dark slot background."""
        desc = _compute_descriptor(patch_bgr)
        if desc is None:
            return True
        patch_gray_28 = desc[0]
        return float(np.std(patch_gray_28)) < min_patch_std or float(np.mean(patch_gray_28)) < 16.0

    def _match_descriptors(
        self,
        patch_gray_28: np.ndarray,
        patch_hist: np.ndarray,
        patch_hist_top: np.ndarray | None,
        patch_std: float,
        *,
        candidate_ids: Sequence[str] | None = None,
        min_score: float = 0.62,
        min_margin: float = 0.03,
    ) -> ItemMatchResult | None:
        self._ensure_matrices()
        assert (
            self._T_core is not None
            and self._T_zoom is not None
            and self._T_top is not None
            and self._H_hist is not None
            and self._H_hist_top is not None
        )

        if candidate_ids is not None:
            seen: set[str] = set()
            row_indices: list[int] = []
            for raw_cid in candidate_ids:
                cid = str(raw_cid).strip()
                if not cid or cid in seen or cid in IGNORED_HUD_ITEM_IDS:
                    continue
                seen.add(cid)
                ridx = self._id_to_row.get(cid)
                if ridx is not None:
                    row_indices.append(ridx)
            if not row_indices:
                return None
            row_ids = [self._matrix_ids[i] for i in row_indices]
            idx_arr = np.asarray(row_indices, dtype=np.intp)
            t_core = self._T_core[idx_arr]
            t_zoom = self._T_zoom[idx_arr]
            t_top = self._T_top[idx_arr]
            h_hist = self._H_hist[idx_arr]
            h_hist_top = self._H_hist_top[idx_arr]
        else:
            row_ids = self._matrix_ids
            t_core = self._T_core
            t_zoom = self._T_zoom
            t_top = self._T_top
            h_hist = self._H_hist
            h_hist_top = self._H_hist_top

        n_rows = len(row_ids)
        if n_rows == 0:
            return None

        # Vectorized BLAS correlation across all templates simultaneously
        p_win = _extract_patch_windows_26(patch_gray_28)  # (676, 9)
        core_scores = np.clip((t_core @ p_win).max(axis=1), 0.0, 1.0)
        zoom_scores = np.clip((t_zoom @ p_win).max(axis=1), 0.0, 1.0)
        tmpl_scores = np.where(core_scores < 0.88, np.maximum(core_scores, zoom_scores), core_scores)

        h_vec = _normalize_vec_zero_mean(patch_hist.reshape(-1), min_norm=1e-6)
        hist_scores = np.clip(h_hist @ h_vec, 0.0, 1.0)
        full_combined = TEMPLATE_WEIGHT * tmpl_scores + HIST_WEIGHT * hist_scores

        if patch_hist_top is not None:
            p_top = _extract_patch_windows_top16(patch_gray_28)  # (416, 12)
            top_tmpl = np.clip((t_top @ p_top).max(axis=1), 0.0, 1.0)
            h_top_vec = _normalize_vec_zero_mean(patch_hist_top.reshape(-1), min_norm=1e-6)
            top_hist = np.clip(h_hist_top @ h_top_vec, 0.0, 1.0)
            top_combined = (TEMPLATE_WEIGHT * top_tmpl + HIST_WEIGHT * top_hist) * 0.96
            use_top = top_combined > full_combined
            combined = np.where(use_top, top_combined, full_combined)
            eff_tmpl = np.where(use_top, np.maximum(tmpl_scores, top_tmpl), tmpl_scores)
            eff_hist = np.where(use_top, np.maximum(hist_scores, top_hist), hist_scores)
        else:
            combined = full_combined
            eff_tmpl = tmpl_scores
            eff_hist = hist_scores

        best_idx = int(np.argmax(combined))
        best_score = float(combined[best_idx])
        best_id = row_ids[best_idx]
        best_tmpl_s = float(eff_tmpl[best_idx])
        best_hist_s = float(eff_hist[best_idx])

        if n_rows > 1:
            combined_copy = combined.copy()
            combined_copy[best_idx] = -1.0
            second_score = max(0.0, float(np.max(combined_copy)))
        else:
            second_score = 0.0

        margin = max(0.0, best_score - second_score)
        if not best_id or best_score < min_score or (n_rows > 1 and margin < min_margin and best_score < 0.85):
            return None
        return ItemMatchResult(
            item_id=best_id,
            score=round(best_score, 4),
            margin=round(margin, 4),
            hud_slot_index=-1,
            template_score=round(best_tmpl_s, 4),
            hist_score=round(best_hist_s, 4),
            patch_std=round(patch_std, 3),
            template_count=self.template_count,
        )

    def match_patch(
        self,
        patch_bgr: np.ndarray,
        *,
        candidate_ids: Sequence[str] | None = None,
        min_score: float = 0.62,
        min_margin: float = 0.03,
        min_patch_std: float = 8.0,
    ) -> ItemMatchResult | None:
        desc = _compute_descriptor(patch_bgr)
        if desc is None:
            return None
        patch_gray_28, _, _, patch_hist = desc
        patch_std = float(np.std(patch_gray_28))
        # Empty item slots in LoL HUD are near-uniform dark teal/slate boxes
        if patch_std < min_patch_std or float(np.mean(patch_gray_28)) < 16.0:
            return None
        top_desc = _compute_top_descriptor(patch_bgr)
        patch_hist_top = top_desc[1] if top_desc is not None else None
        return self._match_descriptors(
            patch_gray_28,
            patch_hist,
            patch_hist_top,
            patch_std,
            candidate_ids=candidate_ids,
            min_score=min_score,
            min_margin=min_margin,
        )

    def scan_hud_slots_with_state(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect | None = None,
        *,
        candidate_ids: Sequence[str] | None = None,
        min_score: float = 0.62,
        min_margin: float = 0.03,
        hud_scale: float = 1.0,
    ) -> tuple[list[ItemMatchResult], dict[int, str]]:
        """Scan all 6 slots and classify each as 'matched', 'empty', or 'occluded' (cooldown/shop/hover)."""
        hits: list[ItemMatchResult] = []
        states: dict[int, str] = {}
        diag: dict[str, Any] = {
            "template_count": self.template_count,
            "hud_scale": float(hud_scale),
            "slot_matches": {},
            "slot_states": {},
            "slot_stds": {},
            "cache_hits": 0,
        }
        if self.template_count == 0 or not is_valid_frame(frame_bgr):
            self._last_scan_diag = diag
            return hits, states

        self._ensure_matrices()
        cand_key = tuple(str(c).strip() for c in candidate_ids) if candidate_ids is not None else None
        cache_key = (self._matrix_version, cand_key, round(float(min_score), 4), round(float(min_margin), 4), round(float(hud_scale), 3))

        patches = self.extract_hud_item_patches(frame_bgr, game_rect, hud_scale=hud_scale)
        for slot_idx, patch in patches:
            bgr = _decode_to_bgr(patch)
            if bgr is None or bgr.shape[0] < 6 or bgr.shape[1] < 6:
                states[slot_idx] = "empty"
                diag["slot_states"][slot_idx] = "empty"
                continue

            # Lightweight per-slot 16x16 grayscale frame-diff cache skips unchanged static slots
            gray_16 = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (16, 16), interpolation=cv2.INTER_AREA)
            cached = self._slot_cache.get(slot_idx)
            if cached is not None and cached[1] == cache_key:
                diff = float(np.mean(np.abs(gray_16.astype(np.int16) - cached[0].astype(np.int16))))
                if diff < 1.5:
                    c_state, c_hit, c_std = cached[2], cached[3], cached[4]
                    diag["cache_hits"] += 1
                    diag["slot_stds"][slot_idx] = c_std
                    states[slot_idx] = c_state
                    diag["slot_states"][slot_idx] = c_state
                    if c_hit is not None:
                        hits.append(c_hit)
                        diag["slot_matches"][slot_idx] = {"item_id": c_hit.item_id, "score": c_hit.score}
                    continue

            desc = _compute_descriptor(bgr)
            if desc is None:
                states[slot_idx] = "empty"
                diag["slot_states"][slot_idx] = "empty"
                self._slot_cache[slot_idx] = (gray_16, cache_key, "empty", None, 0.0)
                continue

            patch_gray_28, _, _, patch_hist = desc
            patch_std = float(np.std(patch_gray_28))
            patch_std_r = round(patch_std, 2)
            diag["slot_stds"][slot_idx] = patch_std_r

            if patch_std < 8.0 or float(np.mean(patch_gray_28)) < 16.0:
                states[slot_idx] = "empty"
                diag["slot_states"][slot_idx] = "empty"
                self._slot_cache[slot_idx] = (gray_16, cache_key, "empty", None, patch_std_r)
                continue

            top_desc = _compute_top_descriptor(bgr)
            patch_hist_top = top_desc[1] if top_desc is not None else None
            hit = self._match_descriptors(
                patch_gray_28,
                patch_hist,
                patch_hist_top,
                patch_std,
                candidate_ids=candidate_ids,
                min_score=min_score,
                min_margin=min_margin,
            )
            if hit is not None:
                wrapped = ItemMatchResult(
                    item_id=hit.item_id,
                    score=hit.score,
                    margin=hit.margin,
                    hud_slot_index=slot_idx,
                    template_score=hit.template_score,
                    hist_score=hit.hist_score,
                    patch_std=hit.patch_std,
                    template_count=hit.template_count,
                )
                hits.append(wrapped)
                states[slot_idx] = "matched"
                diag["slot_states"][slot_idx] = "matched"
                diag["slot_matches"][slot_idx] = {"item_id": wrapped.item_id, "score": wrapped.score}
                self._slot_cache[slot_idx] = (gray_16, cache_key, "matched", wrapped, patch_std_r)
            else:
                states[slot_idx] = "occluded"
                diag["slot_states"][slot_idx] = "occluded"
                self._slot_cache[slot_idx] = (gray_16, cache_key, "occluded", None, patch_std_r)
        self._last_scan_diag = diag
        return hits, states

    def scan_hud_items(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect | None = None,
        *,
        candidate_ids: Sequence[str] | None = None,
        min_score: float = 0.62,
        min_margin: float = 0.03,
        hud_scale: float = 1.0,
    ) -> list[ItemMatchResult]:
        """Scan all 6 bottom-HUD item slots and return confident matches."""
        hits, _ = self.scan_hud_slots_with_state(
            frame_bgr,
            game_rect,
            candidate_ids=candidate_ids,
            min_score=min_score,
            min_margin=min_margin,
            hud_scale=hud_scale,
        )
        return hits


class VisualSlotVoteTracker:
    """Multi-frame slot vote filter preventing 1-frame VFX/cursor occlusion false positives."""

    def __init__(self, *, confirm_frames: int = 2, drop_misses: int = 3) -> None:
        self.confirm_frames = max(1, int(confirm_frames))
        self.drop_misses = max(1, int(drop_misses))
        self._pending: dict[int, tuple[str, int]] = {}
        self._confirmed: dict[int, str] = {}
        self._misses: dict[int, int] = {}

    def reset(self) -> None:
        self._pending.clear()
        self._confirmed.clear()
        self._misses.clear()

    def update(
        self,
        slot_to_id: dict[int, str],
        *,
        force_confirm: bool = False,
        slot_states: dict[int, str] | None = None,
    ) -> list[str]:
        """Update with observed `{slot_idx: entity_id}` and optional per-slot state ('matched'|'empty'|'occluded').

        When `slot_states` is provided, 'occluded' slots (active cooldown sweep, stack overlay,
        shop/Tab panel covering the HUD) HOLD their confirmed item without incrementing miss eviction,
        eliminating repeated drop/re-add flicker. Only genuinely 'empty' slots evict after `drop_misses`.
        """
        seen_slots = set(slot_to_id.keys())
        for slot_idx, eid in slot_to_id.items():
            eid_str = str(eid).strip()
            if not eid_str:
                continue
            self._misses[slot_idx] = 0
            if force_confirm or self.confirm_frames <= 1:
                self._confirmed[slot_idx] = eid_str
                self._pending.pop(slot_idx, None)
                continue
            prev_id, prev_cnt = self._pending.get(slot_idx, ("", 0))
            if prev_id == eid_str:
                cnt = prev_cnt + 1
            else:
                cnt = 1
            if cnt >= self.confirm_frames:
                self._confirmed[slot_idx] = eid_str
                self._pending.pop(slot_idx, None)
            else:
                self._pending[slot_idx] = (eid_str, cnt)

        for slot_idx in list(self._confirmed.keys()):
            if slot_idx not in seen_slots:
                if slot_states is not None and slot_states.get(slot_idx) != "empty":
                    # Occluded by cooldown sweep / tooltip / shop / Tab: hold confirmed item steady
                    continue
                m = self._misses.get(slot_idx, 0) + 1
                if m >= self.drop_misses:
                    self._confirmed.pop(slot_idx, None)
                    self._misses.pop(slot_idx, None)
                else:
                    self._misses[slot_idx] = m

        for slot_idx in list(self._pending.keys()):
            if slot_idx not in seen_slots:
                self._pending.pop(slot_idx, None)

        return [self._confirmed[s] for s in sorted(self._confirmed.keys())]

    @property
    def confirmed_ids(self) -> list[str]:
        return [self._confirmed[s] for s in sorted(self._confirmed.keys())]
