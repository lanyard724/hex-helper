"""ACE-safe pure read-only 237-augment visual icon template matcher for HUD verification & Transmute resolution.

Zero process hooks, zero memory reads — strictly operates on captured BGR screen frames and
local/cached CommunityDragon augment icon PNGs using 28x28 normalized template correlation
(`TM_CCOEFF_NORMED`, 0.65 weight) + HSV hue/saturation histogram correlation (0.35 weight).
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import cv2
import httpx
import numpy as np

from hex_helper.vision.capture import WindowRect

logger = logging.getLogger(__name__)

CDRAGON_ASSET_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/assets"
TEMPLATE_SIZE = 28
TEMPLATE_WEIGHT = 0.65
HIST_WEIGHT = 0.35

# Standard 16:9 League of Legends ARAM Mayhem bottom center-left champion HUD 4-slot augment panel
# Normalized (x, y, w, h) coordinates relative to game client area
DEFAULT_HUD_AUGMENT_SLOTS: tuple[tuple[float, float, float, float], ...] = (
    (0.272, 0.878, 0.018, 0.032),  # HUD slot 0 (top-left)
    (0.293, 0.878, 0.018, 0.032),  # HUD slot 1 (top-right)
    (0.272, 0.916, 0.018, 0.032),  # HUD slot 2 (bottom-left)
    (0.293, 0.916, 0.018, 0.032),  # HUD slot 3 (bottom-right)
)


@dataclass(frozen=True)
class IconMatchResult:
    augment_id: str
    score: float
    margin: float
    hud_slot_index: int = -1
    template_score: float = 0.0
    hist_score: float = 0.0
    name: str = ""


@dataclass(frozen=True)
class _IconTemplate:
    augment_id: str
    gray_28: np.ndarray        # (28, 28) uint8 grayscale
    gray_core_26: np.ndarray   # (26, 26) uint8 center crop for +/-1px shift-invariant matchTemplate
    gray_zoom_26: np.ndarray   # (26, 26) uint8 inner-86% zoomed crop for edge-trimmed HUD patches
    hsv_hist: np.ndarray       # normalized (12, 6) float32 Hue-Saturation histogram
    name: str = ""


def is_valid_frame(frame_bgr: np.ndarray | None) -> bool:
    """Verify captured frame is non-empty and non-black (guards DirectX black swapchain frames)."""
    if frame_bgr is None or not isinstance(frame_bgr, np.ndarray) or frame_bgr.size == 0 or frame_bgr.ndim < 2:
        return False
    if int(np.max(frame_bgr)) <= 0:
        return False
    if frame_bgr.ndim == 3 and frame_bgr.shape[2] >= 3:
        gray = cv2.cvtColor(frame_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
        mean_luma = float(np.mean(gray))
    else:
        mean_luma = float(np.mean(frame_bgr))
    return mean_luma > 2.0


def _decode_to_bgr(img: np.ndarray | None) -> np.ndarray | None:
    """Convert BGR / BGRA / grayscale ndarray into clean 3-channel uint8 BGR (compositing alpha on dark HUD)."""
    if img is None or not isinstance(img, np.ndarray) or img.size == 0 or img.ndim < 2:
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img.astype(np.uint8, copy=False), cv2.COLOR_GRAY2BGR)
    ch = img.shape[2]
    if ch == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        # Composite onto dark Hextech HUD slot tone so transparent CDragon PNGs match in-game rendering
        bg = np.array([24.0, 18.0, 14.0], dtype=np.float32)
        comp = img[:, :, :3].astype(np.float32) * alpha + bg * (1.0 - alpha)
        return np.clip(comp, 0.0, 255.0).astype(np.uint8)
    if ch >= 3:
        return img[:, :, :3].astype(np.uint8, copy=False)
    return None


def _compute_descriptor(img: np.ndarray | None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Compute 28x28 normalized grayscale + shift/zoom cores + HSV hue/sat histogram descriptor."""
    bgr = _decode_to_bgr(img)
    if bgr is None or bgr.shape[0] < 4 or bgr.shape[1] < 4:
        return None

    interp = cv2.INTER_AREA if (bgr.shape[0] >= TEMPLATE_SIZE and bgr.shape[1] >= TEMPLATE_SIZE) else cv2.INTER_CUBIC
    bgr_28 = cv2.resize(bgr, (TEMPLATE_SIZE, TEMPLATE_SIZE), interpolation=interp)
    gray_28 = cv2.cvtColor(bgr_28, cv2.COLOR_BGR2GRAY)
    gray_core_26 = gray_28[1:-1, 1:-1].copy()
    gray_zoom_26 = cv2.resize(gray_28[2:-2, 2:-2], (TEMPLATE_SIZE - 2, TEMPLATE_SIZE - 2), interpolation=cv2.INTER_LINEAR)

    hsv_28 = cv2.cvtColor(bgr_28, cv2.COLOR_BGR2HSV)
    h_ch = hsv_28[:, :, 0].copy()
    s_ch = hsv_28[:, :, 1]
    # Zero out undefined hue on near-achromatic pixels to stabilize silver/dark icons
    h_ch[s_ch < 20] = 0
    hs_28 = np.stack((h_ch, s_ch), axis=-1)
    hist = cv2.calcHist([hs_28], [0, 1], None, [12, 6], [0, 180, 0, 256]).astype(np.float32)
    hist_sum = float(hist.sum())
    if hist_sum > 0.0:
        hist /= hist_sum

    return gray_28, gray_core_26, gray_zoom_26, hist


def _read_image_unicode(path: Path) -> np.ndarray | None:
    """Read image file safely even when Windows path contains non-ASCII characters."""
    try:
        raw = np.fromfile(str(path), dtype=np.uint8)
        if raw.size == 0:
            return None
        return cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
    except Exception:
        return None


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


class AugmentIconMatcher:
    """Pure read-only visual icon template matcher for the 237-augment catalog & HUD verification."""

    def __init__(
        self,
        cache_dir: Path | str | None = None,
        *,
        cdragon_json_path: Path | str | None = None,
        proxy_url: str = "",
        fallback_proxy_url: str = "",
        allow_download: bool = False,
        hud_slot_rois: Sequence[tuple[float, float, float, float]] | None = None,
        edge_trim_ratio: float = 0.06,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cache_dir: Path | None = Path(cache_dir) if cache_dir is not None else None
        self.icon_dir: Path | None = (self.cache_dir / "icons" / "augment") if self.cache_dir is not None else None
        self.proxy_url = (proxy_url or "").strip()
        self.fallback_proxy_url = (fallback_proxy_url or "").strip()
        self.allow_download = bool(allow_download)
        self._edge_trim_ratio = max(0.0, min(0.20, float(edge_trim_ratio)))
        self._hud_slot_rois: tuple[tuple[float, float, float, float], ...] = tuple(
            hud_slot_rois if hud_slot_rois is not None else DEFAULT_HUD_AUGMENT_SLOTS
        )
        self._fetcher = fetcher

        self._icon_urls: dict[str, str] = {}
        self._augment_names: dict[str, str] = {}
        self._templates: dict[str, _IconTemplate] = {}
        self._fail_until: dict[str, float] = {}

        self._load_cdragon_metadata(cdragon_json_path)
        self._load_cached_icons()

    @property
    def template_count(self) -> int:
        return len(self._templates)

    @property
    def known_url_count(self) -> int:
        return len(self._icon_urls)

    def has_template(self, augment_id: str) -> bool:
        return str(augment_id) in self._templates

    def _load_cdragon_metadata(self, explicit_path: Path | str | None) -> None:
        candidates: list[Path] = []
        if explicit_path is not None:
            candidates.append(Path(explicit_path))
        if self.cache_dir is not None:
            candidates.append(self.cache_dir / "static" / "cdragon_augments.json")
            candidates.append(self.cache_dir / "cdragon_augments.json")

        for path in candidates:
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug("Failed reading cdragon_augments.json at %s: %s", path, exc)
                continue
            if not isinstance(raw, list):
                continue
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                aid = str(entry.get("id", "")).strip()
                if not aid:
                    continue
                name = str(entry.get("nameTRA") or entry.get("name") or "").strip()
                if name:
                    self._augment_names[aid] = name
                icon_path = str(
                    entry.get("augmentSmallIconPath") or entry.get("augmentLargeIconPath") or ""
                ).strip()
                if icon_path:
                    rel = icon_path.lower()
                    prefix = "/lol-game-data/assets/"
                    if rel.startswith(prefix):
                        rel = rel[len(prefix) :]
                    self._icon_urls[aid] = f"{CDRAGON_ASSET_BASE}/{rel.lstrip('/')}"
            break

    def _load_cached_icons(self) -> None:
        if self.icon_dir is None or not self.icon_dir.exists():
            return
        try:
            png_files = sorted(self.icon_dir.glob("*.png"))
        except Exception:
            return
        for png in png_files:
            aid = png.stem.strip()
            if not aid:
                continue
            img = _read_image_unicode(png)
            if img is not None:
                self.register_template(aid, img, name=self._augment_names.get(aid, ""))

    def register_template(self, augment_id: str, bgr_img: np.ndarray, name: str = "") -> bool:
        """Precompute and register a 28x28 grayscale + HSV-hue/sat template descriptor in memory."""
        aid = str(augment_id).strip()
        if not aid:
            return False
        desc = _compute_descriptor(bgr_img)
        if desc is None:
            return False
        gray_28, gray_core_26, gray_zoom_26, hsv_hist = desc
        eff_name = name or self._augment_names.get(aid, "")
        self._templates[aid] = _IconTemplate(
            augment_id=aid,
            gray_28=gray_28,
            gray_core_26=gray_core_26,
            gray_zoom_26=gray_zoom_26,
            hsv_hist=hsv_hist,
            name=eff_name,
        )
        return True

    def _fetch_bytes(self, url: str) -> bytes | None:
        if self._fetcher is not None:
            try:
                return self._fetcher(url)
            except Exception as exc:
                logger.debug("Custom icon fetcher failed for %s: %s", url, exc)
                return None

        # Ordered attempts: configured proxy -> direct -> optional fallback proxy (never crashes offline)
        proxy_attempts: list[str | None] = []
        if self.proxy_url:
            proxy_attempts.append(self.proxy_url)
        proxy_attempts.append(None)
        if self.fallback_proxy_url and self.fallback_proxy_url not in proxy_attempts:
            proxy_attempts.append(self.fallback_proxy_url)

        for proxy in proxy_attempts:
            try:
                with httpx.Client(
                    proxy=proxy,
                    trust_env=False,
                    timeout=8.0,
                    follow_redirects=True,
                    headers={"User-Agent": "HexHelper/1.1"},
                ) as client:
                    resp = client.get(url)
                    if resp.status_code == 200 and len(resp.content) > 64:
                        return resp.content
            except Exception as exc:
                logger.debug("Icon HTTP fetch failed (%s, proxy=%s): %s", url, proxy, exc)
        return None

    def ensure_template(self, augment_id: str) -> bool:
        """Ensure template is in memory: checks memory -> local cache -> optional lazy HTTP download."""
        aid = str(augment_id).strip()
        if not aid:
            return False
        if aid in self._templates:
            return True

        safe_id = "".join(ch for ch in aid if ch.isalnum() or ch in ("_", "-"))
        if not safe_id:
            return False

        local_path = (self.icon_dir / f"{safe_id}.png") if self.icon_dir is not None else None
        if local_path is not None and local_path.exists():
            img = _read_image_unicode(local_path)
            if img is not None and self.register_template(aid, img):
                return True

        if not self.allow_download:
            return False

        now = time.monotonic()
        if self._fail_until.get(aid, 0.0) > now:
            return False

        url = self._icon_urls.get(aid)
        if not url:
            return False

        raw = self._fetch_bytes(url)
        if not raw or len(raw) <= 64:
            self._fail_until[aid] = now + 300.0
            return False

        img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None or not self.register_template(aid, img):
            self._fail_until[aid] = now + 300.0
            return False

        if local_path is not None:
            try:
                _atomic_write_bytes(local_path, raw)
            except Exception as exc:
                logger.debug("Failed writing icon cache %s: %s", local_path, exc)
        self._fail_until.pop(aid, None)
        return True

    @staticmethod
    def is_valid_frame(frame_bgr: np.ndarray | None) -> bool:
        return is_valid_frame(frame_bgr)

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

    def get_hud_slot_rects(self, frame_width: int, frame_height: int) -> list[tuple[int, tuple[int, int, int, int]]]:
        """Return pixel bounding boxes `(slot_index, (x0, y0, x1, y1))` for the 4 HUD augment slots."""
        fw = max(1, int(frame_width))
        fh = max(1, int(frame_height))
        rects: list[tuple[int, tuple[int, int, int, int]]] = []
        for idx, (rx, ry, rw, rh) in enumerate(self._hud_slot_rois):
            # Preserve square aspect ratio anchored on height so ultrawide/16:10 stay square
            cx = (rx + rw * 0.5) * fw
            cy = (ry + rh * 0.5) * fh
            side = max(8, int(round(min(rw * fw, rh * fh))))
            half = side * 0.5
            x0 = max(0, int(round(cx - half)))
            y0 = max(0, int(round(cy - half)))
            x1 = min(fw, x0 + side)
            y1 = min(fh, y0 + side)
            if x1 > x0 + 4 and y1 > y0 + 4:
                rects.append((idx, (x0, y0, x1, y1)))
        return rects

    def extract_hud_augment_patches(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect | None = None,
    ) -> list[tuple[int, np.ndarray]]:
        """Extract the 4 square HUD augment icon patches with automatic square edge trimming."""
        if not self.is_valid_frame(frame_bgr):
            return []

        view = self._resolve_game_view(frame_bgr, game_rect)
        vh, vw = view.shape[:2]
        if vh < 64 or vw < 64:
            return []

        patches: list[tuple[int, np.ndarray]] = []
        for slot_idx, (x0, y0, x1, y1) in self.get_hud_slot_rects(vw, vh):
            crop = view[y0:y1, x0:x1]
            ch, cw = crop.shape[:2]
            if ch < 8 or cw < 8:
                continue

            # Center-crop to exact square if clipped at frame edges
            side = min(ch, cw)
            if ch != cw:
                sy = (ch - side) // 2
                sx = (cw - side) // 2
                crop = crop[sy : sy + side, sx : sx + side]

            # Trim outer rarity bezel/frame pixels while keeping >= 8x8 interior art
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
        tmpl: _IconTemplate,
    ) -> tuple[float, float, float]:
        # Shift-invariant 26x26 template correlation inside 28x28 patch
        if float(np.std(tmpl.gray_core_26)) < 1e-3:
            tmpl_score = 0.0
        else:
            res = cv2.matchTemplate(patch_gray_28, tmpl.gray_core_26, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(res)
            tmpl_score = 0.0 if math.isnan(max_val) else max(0.0, min(1.0, float(max_val)))
            # If patch lost a few edge pixels to HUD bezel trimming, also test zoomed inner template
            if tmpl_score < 0.88 and float(np.std(tmpl.gray_zoom_26)) >= 1e-3:
                res_z = cv2.matchTemplate(patch_gray_28, tmpl.gray_zoom_26, cv2.TM_CCOEFF_NORMED)
                _, max_z, _, _ = cv2.minMaxLoc(res_z)
                if not math.isnan(max_z):
                    tmpl_score = max(tmpl_score, max(0.0, min(1.0, float(max_z))))

        hist_corr = float(cv2.compareHist(patch_hist, tmpl.hsv_hist, cv2.HISTCMP_CORREL))
        hist_score = 0.0 if math.isnan(hist_corr) else max(0.0, min(1.0, hist_corr))
        combined = TEMPLATE_WEIGHT * tmpl_score + HIST_WEIGHT * hist_score
        return combined, tmpl_score, hist_score

    def match_patch(
        self,
        patch_bgr: np.ndarray,
        candidate_ids: Sequence[str] | None = None,
        min_score: float = 0.58,
    ) -> IconMatchResult | None:
        """Match a single icon patch against `candidate_ids` (or all loaded templates if None)."""
        desc = _compute_descriptor(patch_bgr)
        if desc is None:
            return None
        patch_gray_28, _, _, patch_hist = desc

        # Reject empty/flat HUD slots so blank background never false-matches an augment
        if float(np.std(patch_gray_28)) < 3.0:
            return None

        if candidate_ids is not None:
            seen: set[str] = set()
            target_ids: list[str] = []
            for raw_id in candidate_ids:
                cid = str(raw_id).strip()
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                if self.ensure_template(cid):
                    target_ids.append(cid)
        else:
            target_ids = list(self._templates.keys())

        if not target_ids:
            return None

        scored: list[tuple[float, float, float, _IconTemplate]] = []
        for aid in target_ids:
            tmpl = self._templates.get(aid)
            if tmpl is None:
                continue
            combined, tmpl_s, hist_s = self._score_against_template(patch_gray_28, patch_hist, tmpl)
            scored.append((combined, tmpl_s, hist_s, tmpl))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        top1_score, top1_tmpl, top1_hist, top1 = scored[0]
        if top1_score < min_score:
            return None

        top2_score = scored[1][0] if len(scored) > 1 else 0.0
        margin = max(0.0, top1_score - top2_score)
        return IconMatchResult(
            augment_id=top1.augment_id,
            score=round(top1_score, 4),
            margin=round(margin, 4),
            hud_slot_index=-1,
            template_score=round(top1_tmpl, 4),
            hist_score=round(top1_hist, 4),
            name=top1.name,
        )

    def verify_picked_augment(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect | None,
        candidate_ids: Sequence[str] | None = None,
        slot_index_hint: int | None = None,
        *,
        min_score: float = 0.58,
    ) -> IconMatchResult | None:
        """Scan HUD augment slots against `candidate_ids` (or full 237-template bank when None for Transmute)."""
        if not self.is_valid_frame(frame_bgr):
            return None

        patches = self.extract_hud_augment_patches(frame_bgr, game_rect)
        if not patches:
            return None

        # If caller knows which HUD slot (0..3) just filled, test it first so older slots never shadow it
        if slot_index_hint is not None:
            for slot_idx, patch in patches:
                if slot_idx == slot_index_hint:
                    hit = self.match_patch(patch, candidate_ids=candidate_ids, min_score=min_score)
                    if hit is not None:
                        return IconMatchResult(
                            augment_id=hit.augment_id,
                            score=hit.score,
                            margin=hit.margin,
                            hud_slot_index=slot_idx,
                            template_score=hit.template_score,
                            hist_score=hit.hist_score,
                            name=hit.name,
                        )
                    break

        best_hit: IconMatchResult | None = None
        for slot_idx, patch in patches:
            if slot_index_hint is not None and slot_idx == slot_index_hint:
                continue
            hit = self.match_patch(patch, candidate_ids=candidate_ids, min_score=min_score)
            if hit is None:
                continue
            wrapped = IconMatchResult(
                augment_id=hit.augment_id,
                score=hit.score,
                margin=hit.margin,
                hud_slot_index=slot_idx,
                template_score=hit.template_score,
                hist_score=hit.hist_score,
                name=hit.name,
            )
            if best_hit is None or (wrapped.score, wrapped.margin) > (best_hit.score, best_hit.margin):
                best_hit = wrapped
        return best_hit
