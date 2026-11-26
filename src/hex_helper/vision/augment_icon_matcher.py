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
from typing import Any, Callable, Sequence

import cv2
import httpx
import numpy as np

from hex_helper.vision.capture import WindowRect

logger = logging.getLogger(__name__)

CDRAGON_ASSET_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/assets"
TEMPLATE_SIZE = 28
TEMPLATE_WEIGHT = 0.65
HIST_WEIGHT = 0.35

# Empirically calibrated 16:9 League of Legends ARAM Mayhem bottom-left 2x2 augment panel (left of champion portrait)
# Normalized (x, y, w, h) coordinates relative to game client area at GlobalScale = 1.0
DEFAULT_HUD_AUGMENT_SLOTS: tuple[tuple[float, float, float, float], ...] = (
    (0.2219, 0.8799, 0.0281, 0.0500),  # HUD slot 0 (top-left)
    (0.2539, 0.8799, 0.0281, 0.0500),  # HUD slot 1 (top-right)
    (0.2219, 0.9368, 0.0281, 0.0500),  # HUD slot 2 (bottom-left)
    (0.2539, 0.9368, 0.0281, 0.0500),  # HUD slot 3 (bottom-right)
)


@dataclass(frozen=True)
class IconMatchResult:
    augment_id: str
    score: float
    margin: float = 0.0
    hud_slot_index: int = -1
    template_score: float = 0.0
    hist_score: float = 0.0
    name: str = ""
    top2_id: str = ""
    top2_score: float = 0.0
    patch_std: float = 0.0
    template_count: int = 0


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
    # Fast strided pre-check (60-200x faster on 1440p/4K); only fall back to full scan if sample looks dark
    if frame_bgr.shape[0] >= 32 and frame_bgr.shape[1] >= 32:
        sample = frame_bgr[::16, ::16]
        if int(np.max(sample)) > 8:
            if sample.ndim == 3 and sample.shape[2] >= 3:
                s_luma = float(np.mean(cv2.cvtColor(sample[:, :, :3], cv2.COLOR_BGR2GRAY)))
            else:
                s_luma = float(np.mean(sample))
            if s_luma > 2.5:
                return True
    if int(np.max(frame_bgr)) <= 0:
        return False
    if frame_bgr.ndim == 3 and frame_bgr.shape[2] >= 3:
        gray = cv2.cvtColor(frame_bgr[:, :, :3], cv2.COLOR_BGR2GRAY)
        mean_luma = float(np.mean(gray))
    else:
        mean_luma = float(np.mean(frame_bgr))
    return mean_luma > 2.0


def _normalize_rows_zero_mean(
    mat: np.ndarray,
    *,
    min_std: float | None = None,
    min_norm: float | None = None,
) -> np.ndarray:
    """Zero-mean & L2-normalize each row of a 2D float32 matrix (near-flat rows become 0)."""
    arr = np.asarray(mat, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] == 0 or arr.shape[1] == 0:
        return np.zeros((0, arr.shape[1] if arr.ndim == 2 else 0), dtype=np.float32)
    centered = arr - arr.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(centered, axis=1, keepdims=True)
    if min_norm is not None:
        thresh = float(min_norm)
    elif min_std is not None:
        thresh = float(min_std) * math.sqrt(float(arr.shape[1]))
    else:
        thresh = 1e-6
    valid = norms >= thresh
    safe_norms = np.where(valid, norms, 1.0)
    out = np.where(valid, centered / safe_norms, 0.0)
    return np.ascontiguousarray(out, dtype=np.float32)


def _normalize_vec_zero_mean(
    vec: np.ndarray,
    *,
    min_std: float | None = None,
    min_norm: float | None = None,
) -> np.ndarray:
    """Zero-mean & L2-normalize a 1D float32 vector (near-flat vector becomes 0)."""
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return np.zeros((0,), dtype=np.float32)
    centered = arr - float(arr.mean())
    norm = float(np.linalg.norm(centered))
    if min_norm is not None:
        thresh = float(min_norm)
    elif min_std is not None:
        thresh = float(min_std) * math.sqrt(float(arr.size))
    else:
        thresh = 1e-6
    if norm < thresh:
        return np.zeros_like(centered, dtype=np.float32)
    return np.ascontiguousarray(centered / norm, dtype=np.float32)


def _extract_patch_windows_26(patch_gray_28: np.ndarray) -> np.ndarray:
    """Extract all 9 shifted 26x26 windows from a 28x28 patch as a (676, 9) zero-mean L2-normalized float32 matrix."""
    rows = np.empty((9, 26 * 26), dtype=np.float32)
    idx = 0
    for dy in range(3):
        for dx in range(3):
            rows[idx] = patch_gray_28[dy : dy + 26, dx : dx + 26].reshape(-1)
            idx += 1
    normed = _normalize_rows_zero_mean(rows, min_std=1e-3)
    return np.ascontiguousarray(normed.T, dtype=np.float32)


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


def build_hud_icon_strip_crop(
    patches_or_frame: Sequence[tuple[int, np.ndarray]] | np.ndarray,
    game_rect: WindowRect | None = None,
    *,
    hud_slot_rois: Sequence[tuple[float, float, float, float]] | None = None,
    edge_trim_ratio: float = 0.06,
    tile_size: int = TEMPLATE_SIZE,
) -> np.ndarray | None:
    """Compose a compact 4 x tile_size x tile_size horizontal BGR strip (default 28x112x3) of the 4 HUD slots."""
    ts = max(8, int(tile_size))
    patches: list[tuple[int, np.ndarray]]
    if isinstance(patches_or_frame, np.ndarray):
        if not is_valid_frame(patches_or_frame):
            return None
        helper = AugmentIconMatcher(
            cache_dir=None,
            hud_slot_rois=hud_slot_rois,
            edge_trim_ratio=edge_trim_ratio,
        )
        patches = helper.extract_hud_augment_patches(patches_or_frame, game_rect)
        if not patches:
            return None
    else:
        patches = list(patches_or_frame)
        if not patches:
            return None

    strip = np.full((ts, ts * 4, 3), (18, 14, 12), dtype=np.uint8)
    placed = False
    for slot_idx, patch in patches:
        if not (0 <= int(slot_idx) < 4):
            continue
        bgr = _decode_to_bgr(patch)
        if bgr is None or bgr.shape[0] < 4 or bgr.shape[1] < 4:
            continue
        interp = cv2.INTER_AREA if (bgr.shape[0] >= ts and bgr.shape[1] >= ts) else cv2.INTER_CUBIC
        tile = cv2.resize(bgr, (ts, ts), interpolation=interp)
        x0 = int(slot_idx) * ts
        strip[:, x0 : x0 + ts] = tile
        placed = True
    return strip if placed else None


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
        self._cdragon_json_path: Path | str | None = cdragon_json_path
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
        self._last_patch_diag: dict[str, Any] = {}
        self._last_verify_diagnostics: dict[str, Any] = {}

        # Pre-stacked zero-mean L2-normalized float32 matrices for single-BLAS matmul matching
        self._matrix_ids: list[str] = []
        self._id_to_row: dict[str, int] = {}
        self._T_core: np.ndarray | None = None
        self._T_zoom: np.ndarray | None = None
        self._H_hist: np.ndarray | None = None
        self._matrices_dirty: bool = True
        self._matrix_version: int = 0
        self._slot_cache: dict[int, tuple[np.ndarray, tuple[Any, ...], IconMatchResult | None]] = {}

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
        self._id_to_row = {aid: i for i, aid in enumerate(ids)}
        if not ids:
            self._T_core = np.zeros((0, 26 * 26), dtype=np.float32)
            self._T_zoom = np.zeros((0, 26 * 26), dtype=np.float32)
            self._H_hist = np.zeros((0, 72), dtype=np.float32)
        else:
            core_raw = np.stack([self._templates[aid].gray_core_26.reshape(-1) for aid in ids], axis=0)
            zoom_raw = np.stack([self._templates[aid].gray_zoom_26.reshape(-1) for aid in ids], axis=0)
            hist_raw = np.stack([self._templates[aid].hsv_hist.reshape(-1) for aid in ids], axis=0)
            self._T_core = _normalize_rows_zero_mean(core_raw, min_std=1e-3)
            self._T_zoom = _normalize_rows_zero_mean(zoom_raw, min_std=1e-3)
            self._H_hist = _normalize_rows_zero_mean(hist_raw, min_norm=1e-6)
        self._matrices_dirty = False
        self._matrix_version += 1

    def reload_cached_icons(self, cdragon_json_path: Path | str | None = None) -> int:
        """Reload CDragon metadata and cached icon PNG templates from disk; return loaded template count."""
        meta_path = cdragon_json_path if cdragon_json_path is not None else self._cdragon_json_path
        self._fail_until.clear()
        self.clear_slot_cache()
        self._load_cdragon_metadata(meta_path)
        self._load_cached_icons()
        return self.template_count

    def last_verify_diagnostics(self) -> dict[str, Any]:
        """Return a shallow copy of diagnostics from the most recent `verify_picked_augment` call."""
        return dict(self._last_verify_diagnostics)

    def build_hud_icon_strip_crop(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect | None = None,
        *,
        tile_size: int = TEMPLATE_SIZE,
    ) -> np.ndarray | None:
        """Extract the 4 HUD augment slots and compose a compact 4 x 28 x 28 horizontal BGR strip."""
        patches = self.extract_hud_augment_patches(frame_bgr, game_rect)
        if not patches:
            return None
        return build_hud_icon_strip_crop(patches, tile_size=tile_size)

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
                    rel = icon_path.lower().lstrip("/")
                    while True:
                        if rel.startswith("lol-game-data/assets/"):
                            rel = rel[len("lol-game-data/assets/") :].lstrip("/")
                        elif rel.startswith("assets/"):
                            rel = rel[len("assets/") :].lstrip("/")
                        else:
                            break
                    self._icon_urls[aid] = f"{CDRAGON_ASSET_BASE}/{rel}"
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
        self._matrices_dirty = True
        self._slot_cache.clear()
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
        self._last_patch_diag = {
            "patch_std": 0.0,
            "template_count": self.template_count,
            "target_count": 0,
            "top1_id": "",
            "top1_score": 0.0,
            "top2_id": "",
            "top2_score": 0.0,
            "margin": 0.0,
            "outcome": "invalid_patch",
        }
        desc = _compute_descriptor(patch_bgr)
        if desc is None:
            return None
        patch_gray_28, _, _, patch_hist = desc

        # Reject empty/flat HUD slots so blank background never false-matches an augment
        patch_std = float(np.std(patch_gray_28))
        self._last_patch_diag["patch_std"] = round(patch_std, 3)
        if patch_std < 3.0:
            self._last_patch_diag["outcome"] = "flat_patch"
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

        self._last_patch_diag["template_count"] = self.template_count
        self._last_patch_diag["target_count"] = len(target_ids)
        if not target_ids:
            self._last_patch_diag["outcome"] = "skipped_empty_bank"
            return None

        self._ensure_matrices()
        assert self._T_core is not None and self._T_zoom is not None and self._H_hist is not None

        if candidate_ids is None:
            row_ids = self._matrix_ids
            t_core = self._T_core
            t_zoom = self._T_zoom
            h_hist = self._H_hist
        else:
            row_indices = [self._id_to_row[aid] for aid in target_ids if aid in self._id_to_row]
            if not row_indices:
                self._last_patch_diag["outcome"] = "skipped_empty_bank"
                return None
            row_ids = [self._matrix_ids[i] for i in row_indices]
            idx_arr = np.asarray(row_indices, dtype=np.intp)
            t_core = self._T_core[idx_arr]
            t_zoom = self._T_zoom[idx_arr]
            h_hist = self._H_hist[idx_arr]

        n_rows = len(row_ids)
        if n_rows == 0:
            self._last_patch_diag["outcome"] = "skipped_empty_bank"
            return None

        # Vectorized BLAS correlation across all templates simultaneously
        p_win = _extract_patch_windows_26(patch_gray_28)  # (676, 9)
        core_scores = np.clip((t_core @ p_win).max(axis=1), 0.0, 1.0)
        zoom_scores = np.clip((t_zoom @ p_win).max(axis=1), 0.0, 1.0)
        tmpl_scores = np.where(core_scores < 0.88, np.maximum(core_scores, zoom_scores), core_scores)

        h_vec = _normalize_vec_zero_mean(patch_hist.reshape(-1), min_norm=1e-6)
        hist_scores = np.clip(h_hist @ h_vec, 0.0, 1.0)
        combined = TEMPLATE_WEIGHT * tmpl_scores + HIST_WEIGHT * hist_scores

        top1_idx = int(np.argmax(combined))
        top1_score = float(combined[top1_idx])
        top1_tmpl = float(tmpl_scores[top1_idx])
        top1_hist = float(hist_scores[top1_idx])
        top1_id = row_ids[top1_idx]
        top1 = self._templates[top1_id]

        if n_rows > 1:
            combined_copy = combined.copy()
            combined_copy[top1_idx] = -1.0
            top2_idx = int(np.argmax(combined_copy))
            top2_id = row_ids[top2_idx]
            top2_score = max(0.0, float(combined[top2_idx]))
        else:
            top2_id = ""
            top2_score = 0.0

        margin = max(0.0, top1_score - top2_score)
        self._last_patch_diag.update(
            {
                "top1_id": top1.augment_id,
                "top1_score": round(top1_score, 4),
                "top2_id": top2_id,
                "top2_score": round(top2_score, 4),
                "margin": round(margin, 4),
            }
        )
        if top1_score < min_score:
            self._last_patch_diag["outcome"] = "below_threshold"
            return None

        self._last_patch_diag["outcome"] = "matched"
        return IconMatchResult(
            augment_id=top1.augment_id,
            score=round(top1_score, 4),
            margin=round(margin, 4),
            hud_slot_index=-1,
            template_score=round(top1_tmpl, 4),
            hist_score=round(top1_hist, 4),
            name=top1.name,
            top2_id=top2_id,
            top2_score=round(top2_score, 4),
            patch_std=round(patch_std, 3),
            template_count=self.template_count,
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
        diag: dict[str, Any] = {
            "template_count": self.template_count,
            "frame_valid": False,
            "slot_patch_stds": {},
            "best_slot": -1,
            "best_id": "",
            "best_score": 0.0,
            "top2_id": "",
            "top2_score": 0.0,
            "margin": 0.0,
            "min_score": float(min_score),
            "slot_index_hint": slot_index_hint,
            "outcome": "invalid_frame",
        }
        if not self.is_valid_frame(frame_bgr):
            self._last_verify_diagnostics = diag
            return None
        diag["frame_valid"] = True

        patches = self.extract_hud_augment_patches(frame_bgr, game_rect)
        if not patches:
            diag["outcome"] = "no_patches"
            self._last_verify_diagnostics = diag
            return None

        # Precompute per-slot grayscale stds for HUD alignment diagnostics
        slot_stds: dict[int, float] = {}
        for s_idx, p in patches:
            bgr = _decode_to_bgr(p)
            if bgr is not None and bgr.shape[0] >= 4 and bgr.shape[1] >= 4:
                interp = cv2.INTER_AREA if (bgr.shape[0] >= TEMPLATE_SIZE and bgr.shape[1] >= TEMPLATE_SIZE) else cv2.INTER_CUBIC
                g28 = cv2.cvtColor(cv2.resize(bgr, (TEMPLATE_SIZE, TEMPLATE_SIZE), interpolation=interp), cv2.COLOR_BGR2GRAY)
                slot_stds[s_idx] = round(float(np.std(g28)), 3)
            else:
                slot_stds[s_idx] = 0.0
        diag["slot_patch_stds"] = slot_stds

        best_miss: dict[str, Any] | None = None
        saw_empty_bank = False

        def _track_miss(s_idx: int) -> None:
            nonlocal best_miss, saw_empty_bank
            pd = self._last_patch_diag
            if pd.get("outcome") == "skipped_empty_bank":
                saw_empty_bank = True
            sc = float(pd.get("top1_score", 0.0))
            if pd.get("top1_id") and (best_miss is None or sc > float(best_miss.get("best_score", 0.0))):
                best_miss = {
                    "best_slot": s_idx,
                    "best_id": str(pd.get("top1_id", "")),
                    "best_score": sc,
                    "top2_id": str(pd.get("top2_id", "")),
                    "top2_score": float(pd.get("top2_score", 0.0)),
                    "margin": float(pd.get("margin", 0.0)),
                }

        # If caller knows which HUD slot (0..3) just filled, test it first so older slots never shadow it
        if slot_index_hint is not None:
            for slot_idx, patch in patches:
                if slot_idx == slot_index_hint:
                    hit = self.match_patch(patch, candidate_ids=candidate_ids, min_score=min_score)
                    if hit is not None:
                        wrapped = IconMatchResult(
                            augment_id=hit.augment_id,
                            score=hit.score,
                            margin=hit.margin,
                            hud_slot_index=slot_idx,
                            template_score=hit.template_score,
                            hist_score=hit.hist_score,
                            name=hit.name,
                            top2_id=hit.top2_id,
                            top2_score=hit.top2_score,
                            patch_std=hit.patch_std,
                            template_count=hit.template_count,
                        )
                        diag.update(
                            {
                                "template_count": self.template_count,
                                "best_slot": slot_idx,
                                "best_id": wrapped.augment_id,
                                "best_score": wrapped.score,
                                "top2_id": wrapped.top2_id,
                                "top2_score": wrapped.top2_score,
                                "margin": wrapped.margin,
                                "outcome": "matched",
                            }
                        )
                        self._last_verify_diagnostics = diag
                        return wrapped
                    _track_miss(slot_idx)
                    break

        best_hit: IconMatchResult | None = None
        for slot_idx, patch in patches:
            if slot_index_hint is not None and slot_idx == slot_index_hint:
                continue
            hit = self.match_patch(patch, candidate_ids=candidate_ids, min_score=min_score)
            if hit is None:
                _track_miss(slot_idx)
                continue
            wrapped = IconMatchResult(
                augment_id=hit.augment_id,
                score=hit.score,
                margin=hit.margin,
                hud_slot_index=slot_idx,
                template_score=hit.template_score,
                hist_score=hit.hist_score,
                name=hit.name,
                top2_id=hit.top2_id,
                top2_score=hit.top2_score,
                patch_std=hit.patch_std,
                template_count=hit.template_count,
            )
            if best_hit is None or (wrapped.score, wrapped.margin) > (best_hit.score, best_hit.margin):
                best_hit = wrapped

        diag["template_count"] = self.template_count
        if best_hit is not None:
            diag.update(
                {
                    "best_slot": best_hit.hud_slot_index,
                    "best_id": best_hit.augment_id,
                    "best_score": best_hit.score,
                    "top2_id": best_hit.top2_id,
                    "top2_score": best_hit.top2_score,
                    "margin": best_hit.margin,
                    "outcome": "matched",
                }
            )
        elif best_miss is not None:
            diag.update(best_miss)
            diag["outcome"] = "below_threshold"
        elif saw_empty_bank or self.template_count == 0:
            diag["outcome"] = "skipped_empty_bank"
        else:
            diag["outcome"] = "flat_patches"
        self._last_verify_diagnostics = diag
        return best_hit

    def preheat_templates(
        self,
        augment_ids: Sequence[str] | None = None,
        *,
        max_workers: int = 8,
        limit: int = 350,
    ) -> int:
        """Concurrently fetch & cache missing CDragon augment icon PNGs into local disk + memory."""
        if not self.allow_download:
            return self.template_count
        if not self._icon_urls:
            self._load_cdragon_metadata(self._cdragon_json_path)

        raw_ids = list(augment_ids) if augment_ids is not None else list(self._icon_urls.keys())
        missing: list[str] = []
        seen: set[str] = set()
        for raw_aid in raw_ids:
            aid = str(raw_aid).strip()
            if not aid or aid in seen or aid in self._templates:
                continue
            seen.add(aid)
            if aid in self._icon_urls:
                missing.append(aid)
            if len(missing) >= max(1, int(limit)):
                break

        if not missing:
            return self.template_count

        from concurrent.futures import ThreadPoolExecutor

        def _fetch_one(aid: str) -> None:
            try:
                self.ensure_template(aid)
            except Exception:
                pass

        workers = max(1, min(int(max_workers), len(missing)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="HexAugIcon") as pool:
            list(pool.map(_fetch_one, missing))
        return self.template_count

    def scan_all_hud_augments(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect | None = None,
        *,
        candidate_ids: Sequence[str] | None = None,
        min_score: float = 0.60,
        min_margin: float = 0.04,
        hud_scale: float = 1.0,
    ) -> list[IconMatchResult]:
        """Scan all 4 bottom-HUD augment slots for Plan B recovery of previously chosen augments."""
        if self.template_count == 0 or not self.is_valid_frame(frame_bgr):
            return []

        scale = 0.5 + 0.5 * max(0.0, min(1.0, float(hud_scale)))
        if abs(scale - 1.0) <= 0.02:
            patches = self.extract_hud_augment_patches(frame_bgr, game_rect)
        else:
            view = self._resolve_game_view(frame_bgr, game_rect)
            vh, vw = view.shape[:2]
            if vh < 64 or vw < 64:
                return []
            patches = []
            for idx, (rx, ry, rw, rh) in enumerate(self._hud_slot_rois):
                # Scale relative to bottom-center anchor (0.5, 1.0)
                cx_norm = 0.5 + ((rx + rw * 0.5) - 0.5) * scale
                cy_norm = 1.0 - (1.0 - (ry + rh * 0.5)) * scale
                side = max(8, int(round(min(rw * vw, rh * vh) * scale)))
                half = side * 0.5
                x0 = max(0, int(round(cx_norm * vw - half)))
                y0 = max(0, int(round(cy_norm * vh - half)))
                x1 = min(vw, x0 + side)
                y1 = min(vh, y0 + side)
                if x1 <= x0 + 6 or y1 <= y0 + 6:
                    continue
                crop = view[y0:y1, x0:x1]
                ch, cw = crop.shape[:2]
                s_min = min(ch, cw)
                if s_min >= 20 and self._edge_trim_ratio > 0.0:
                    trim = min(int(round(s_min * self._edge_trim_ratio)), (s_min - 8) // 2)
                    if trim > 0:
                        crop = crop[trim : s_min - trim, trim : s_min - trim]
                if crop.size > 0 and crop.shape[0] >= 8 and crop.shape[1] >= 8:
                    patches.append((idx, crop.copy()))

        self._ensure_matrices()
        cand_key = tuple(str(c).strip() for c in candidate_ids) if candidate_ids is not None else None
        cache_key = (self._matrix_version, cand_key, round(float(min_score), 4), round(float(min_margin), 4), round(float(hud_scale), 3))

        hits: list[IconMatchResult] = []
        seen_ids: set[str] = set()
        for slot_idx, patch in patches:
            bgr = _decode_to_bgr(patch)
            thumb_16: np.ndarray | None = None
            cached_used = False
            hit: IconMatchResult | None = None
            if bgr is not None and bgr.shape[0] >= 4 and bgr.shape[1] >= 4:
                gray_16 = cv2.resize(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (16, 16), interpolation=cv2.INTER_AREA)
                thumb_16 = gray_16
                cached = self._slot_cache.get(slot_idx)
                if cached is not None and cached[1] == cache_key:
                    diff = float(np.mean(np.abs(gray_16.astype(np.int16) - cached[0].astype(np.int16))))
                    if diff < 1.5:
                        hit = cached[2]
                        cached_used = True
            if not cached_used:
                raw_hit = self.match_patch(patch, candidate_ids=candidate_ids, min_score=min_score)
                if raw_hit is not None and not (
                    raw_hit.margin < min_margin and raw_hit.score < 0.82 and raw_hit.template_score < 0.85
                ):
                    hit = IconMatchResult(
                        augment_id=raw_hit.augment_id,
                        score=raw_hit.score,
                        margin=raw_hit.margin,
                        hud_slot_index=slot_idx,
                        template_score=raw_hit.template_score,
                        hist_score=raw_hit.hist_score,
                        name=raw_hit.name,
                        top2_id=raw_hit.top2_id,
                        top2_score=raw_hit.top2_score,
                        patch_std=raw_hit.patch_std,
                        template_count=raw_hit.template_count,
                    )
                else:
                    hit = None
                if thumb_16 is not None:
                    self._slot_cache[slot_idx] = (thumb_16, cache_key, hit)

            if hit is None or hit.augment_id in seen_ids:
                continue
            seen_ids.add(hit.augment_id)
            hits.append(hit)
        return hits
