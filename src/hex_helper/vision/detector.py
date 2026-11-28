"""Augment 3-card picker detector: fast gate, rarity border hint, self-overlay exclusion, reroll debounce."""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import cv2
import numpy as np

from hex_helper.config import NormalizedRect
from hex_helper.data.locale_pack import get_stat_anvil_cues, normalize_client_locale, t2s_normalize
from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.matcher import AugmentMatcher, MatchResult, normalize_ocr_text
from hex_helper.vision.ocr_winrt import OcrLine, WinRtOcrEngine


@dataclass(frozen=True)
class DetectedCard:
    slot_index: int  # 0 (left), 1 (mid), 2 (right), or 3 (4th card if present)
    augment: AugmentInfo
    confidence: float
    screen_box: tuple[int, int, int, int]  # (left, top, right, bottom) of title in screen coords
    rarity_hint: str | None


@dataclass(frozen=True)
class PickerDetectionResult:
    picker_visible: bool
    stable: bool
    rerolled: bool
    cards: tuple[DetectedCard, ...]
    ocr_lines_count: int = 0
    skipped_by_gate: bool = False
    cards_seen_on_frame: bool = True
    diagnostics: dict[str, Any] = field(default_factory=dict)


def compute_dhash(image_bgr: np.ndarray, size: int = 8) -> int:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    diff = small[:, 1:] > small[:, :-1]
    bits = 0
    for val in diff.flatten():
        bits = (bits << 1) | int(bool(val))
    return bits


def hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def estimate_slot_rarity_with_ratios(
    strip_bgr: np.ndarray,
    exclude_rects: Sequence[tuple[int, int, int, int]] | None = None,
    *,
    strip_origin: tuple[int, int] = (0, 0),
) -> tuple[str | None, dict[str, float]]:
    """Estimate silver / gold / prismatic card border rarity plus normalized 3-channel ratios.

    When `exclude_rects` is provided, pixels covered by our own overlay badges are masked out
    so cyan overlay chrome never contaminates gold/silver cards into prismatic.
    """
    empty_ratios = {"gold": 0.0, "prismatic": 0.0, "silver": 0.0}
    if strip_bgr is None or strip_bgr.size == 0:
        return None, empty_ratios
    sh, sw = strip_bgr.shape[:2]
    valid_mask: np.ndarray | None = None
    if exclude_rects:
        ox, oy = int(strip_origin[0]), int(strip_origin[1])
        mask = np.ones((sh, sw), dtype=bool)
        any_clipped = False
        for (el, et, er, eb) in exclude_rects:
            lx0 = max(0, int(el) - ox)
            ly0 = max(0, int(et) - oy)
            lx1 = min(sw, int(er) - ox)
            ly1 = min(sh, int(eb) - oy)
            if lx1 > lx0 and ly1 > ly0:
                mask[ly0:ly1, lx0:lx1] = False
                any_clipped = True
        if any_clipped:
            valid_mask = mask
            total = float(np.count_nonzero(valid_mask))
            if total <= 0:
                return None, empty_ratios
        else:
            total = float(sh * sw)
    else:
        total = float(sh * sw)
    if total <= 0:
        return None, empty_ratios

    pts = strip_bgr.astype(np.int16)
    b, g, r = pts[..., 0], pts[..., 1], pts[..., 2]
    low = np.minimum(np.minimum(b, g), r)
    high = np.maximum(np.maximum(b, g), r)

    # Gold: warm amber/gold (inspired by verified LoL card frame thresholds)
    gold_mask = (r >= 130) & (g >= 95) & (b <= 125) & (r - g >= 12) & (r - b >= 45)
    # Prismatic: high-saturation magenta / violet / cyan iridescent pixels
    hsv = cv2.cvtColor(strip_bgr, cv2.COLOR_BGR2HSV)
    h_ch, s_ch, v_ch = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    prism_mask = (s_ch >= 95) & (v_ch >= 110) & (((h_ch >= 115) & (h_ch <= 170)) | ((h_ch >= 82) & (h_ch <= 108)))
    # Silver: bright neutral metallic silver-blue
    silver_mask = (low >= 110) & (high <= 225) & (high - low <= 28) & (b >= r - 8)

    if valid_mask is not None:
        gold_mask = gold_mask & valid_mask
        prism_mask = prism_mask & valid_mask
        silver_mask = silver_mask & valid_mask

    gold_ratio = float(np.count_nonzero(gold_mask)) / total
    prism_ratio = float(np.count_nonzero(prism_mask)) / total
    silver_ratio = float(np.count_nonzero(silver_mask)) / total

    ratios = {
        "gold": round(gold_ratio, 4),
        "prismatic": round(prism_ratio, 4),
        "silver": round(silver_ratio, 4),
    }
    best = max(
        ("gold", gold_ratio),
        ("prismatic", prism_ratio),
        ("silver", silver_ratio),
        key=lambda x: x[1],
    )
    return (best[0] if best[1] >= 0.035 else None), ratios


def estimate_slot_rarity(
    strip_bgr: np.ndarray,
    exclude_rects: Sequence[tuple[int, int, int, int]] | None = None,
    *,
    strip_origin: tuple[int, int] = (0, 0),
) -> str | None:
    """Estimate silver / gold / prismatic card border rarity from BGR pixels above a card title."""
    rarity, _ = estimate_slot_rarity_with_ratios(strip_bgr, exclude_rects=exclude_rects, strip_origin=strip_origin)
    return rarity


def _is_stat_anvil_lines(lines: Sequence[OcrLine], client_locale: str = "zh_cn") -> bool:
    """Detect ARAM Mayhem Stat Anvil popup across CN/MY/TW locales or >=2 '*碎片' lines."""
    cues = get_stat_anvil_cues(client_locale)
    shard_count = 0
    for line in lines:
        compact = "".join(str(line.text or "").split())
        norm = normalize_ocr_text(compact)
        compact_t2s = t2s_normalize(compact)
        for cue in cues.title_cues:
            cue_t2s = t2s_normalize(cue)
            if cue in compact or cue in norm or cue in compact_t2s or cue_t2s in compact_t2s:
                return True
        if (
            compact.endswith(cues.shard_suffix)
            or norm.endswith(cues.shard_suffix)
            or compact_t2s.endswith(cues.shard_suffix)
        ):
            shard_count += 1
            if shard_count >= 2:
                return True
    return False


def filter_excluded_lines(
    lines: list[OcrLine],
    band_origin_x: int,
    band_origin_y: int,
    exclude_rects: list[tuple[int, int, int, int]] | tuple[tuple[int, int, int, int], ...],
) -> list[OcrLine]:
    """Drop OCR lines whose center falls inside any of our own overlay rectangles."""
    if not exclude_rects:
        return lines
    kept: list[OcrLine] = []
    for line in lines:
        sx = band_origin_x + line.cx
        sy = band_origin_y + line.cy
        inside = any(l <= sx <= r and t <= sy <= b for (l, t, r, b) in exclude_rects)
        if not inside:
            kept.append(line)
    return kept


def _mask_excluded_regions(
    crop_bgr: np.ndarray,
    origin_x: int,
    origin_y: int,
    exclude_rects: Sequence[tuple[int, int, int, int]] | None,
) -> np.ndarray:
    """Paint neutral dark fill over pixels intersecting our own overlay rectangles before OCR."""
    if not exclude_rects or crop_bgr is None or crop_bgr.size == 0:
        return crop_bgr
    ch, cw = crop_bgr.shape[:2]
    ox, oy = int(origin_x), int(origin_y)
    masked: np.ndarray | None = None
    for (el, et, er, eb) in exclude_rects:
        lx0 = max(0, int(el) - ox)
        ly0 = max(0, int(et) - oy)
        lx1 = min(cw, int(er) - ox)
        ly1 = min(ch, int(eb) - oy)
        if lx1 > lx0 and ly1 > ly0:
            if masked is None:
                masked = crop_bgr.copy()
            if masked.ndim == 2:
                masked[ly0:ly1, lx0:lx1] = 14
            else:
                masked[ly0:ly1, lx0:lx1] = (16, 14, 12)
    return masked if masked is not None else crop_bgr


def _filter_excluded_shadowed_pass2(
    shadowed: Sequence[dict[str, Any]],
    origin_x: float,
    origin_y: float,
    exclude_rects: Sequence[tuple[int, int, int, int]] | None,
    *,
    coord_scale: float = 1.0,
) -> list[dict[str, Any]]:
    """Drop shadowed_pass2 entries whose kept_bbox or dropped_bbox center falls inside `exclude_rects`."""
    if not shadowed:
        return []
    if not exclude_rects:
        return list(shadowed)

    def _bbox_in_excluded(bbox: Any) -> bool:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return False
        bx, by, bw, bh = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        sx = origin_x + (bx + bw * 0.5) * coord_scale
        sy = origin_y + (by + bh * 0.5) * coord_scale
        return any(l <= sx <= r and t <= sy <= b for (l, t, r, b) in exclude_rects)

    kept: list[dict[str, Any]] = []
    for entry in shadowed:
        if not isinstance(entry, dict):
            continue
        if _bbox_in_excluded(entry.get("kept_bbox")) or _bbox_in_excluded(entry.get("dropped_bbox")):
            continue
        kept.append(entry)
    return kept


class AugmentPickerDetector:
    """Two-stage picker gate + adaptive 3/4-card WinRT OCR + per-slot recovery + anti-false-reroll."""

    _SLOT_SLICE_RATIOS_3: dict[int, tuple[float, float]] = {
        0: (0.00, 0.36),
        1: (0.32, 0.68),
        2: (0.64, 1.00),
    }
    _SLOT_SLICE_RATIOS_4: dict[int, tuple[float, float]] = {
        0: (0.00, 0.28),
        1: (0.24, 0.52),
        2: (0.48, 0.76),
        3: (0.72, 1.00),
    }
    _SLOT_SLICE_RATIOS = _SLOT_SLICE_RATIOS_3
    _TEMPLATE_3_GAME_X: tuple[float, ...] = (0.27, 0.50, 0.73)
    _TEMPLATE_4_GAME_X: tuple[float, ...] = (0.185, 0.395, 0.605, 0.815)

    def __init__(
        self,
        ocr_engine: WinRtOcrEngine,
        matcher: AugmentMatcher,
        card_band_roi: NormalizedRect,
        title_band_roi: NormalizedRect,
        client_locale: str = "zh_cn",
    ) -> None:
        self.ocr = ocr_engine
        self.matcher = matcher
        self.card_band_roi = card_band_roi
        self.title_band_roi = title_band_roi
        self.client_locale: str = normalize_client_locale(client_locale)

        self._last_title_thumb: np.ndarray | None = None
        self._pending_ids: tuple[str, ...] = ()
        self._stable_count: int = 0
        self._locked_cards: tuple[DetectedCard, ...] = ()
        self._missing_frames: int = 0
        self._subset_drop_frames: int = 0

        self.partial_open_count: int = 0
        self.recovered_slot_count: int = 0
        self.recovery_failed_slot_count: int = 0
        self.false_reroll_suppressed_count: int = 0

    def set_client_locale(self, locale: str) -> None:
        self.client_locale = normalize_client_locale(locale)

    def reset(self) -> None:
        self._last_title_thumb = None
        self._pending_ids = ()
        self._stable_count = 0
        self._locked_cards = ()
        self._missing_frames = 0
        self._subset_drop_frames = 0

    def reset_match_metrics(self) -> None:
        self.partial_open_count = 0
        self.recovered_slot_count = 0
        self.recovery_failed_slot_count = 0
        self.false_reroll_suppressed_count = 0

    def get_match_metrics(self) -> dict[str, int]:
        return {
            "partial_open_count": self.partial_open_count,
            "recovered_slot_count": self.recovered_slot_count,
            "false_reroll_suppressed_count": self.false_reroll_suppressed_count,
        }

    def update_rois(self, card_band: NormalizedRect, title_band: NormalizedRect) -> None:
        self.card_band_roi = card_band
        self.title_band_roi = title_band
        self.reset()

    def _ocr_accepts_kwarg(self, name: str) -> bool:
        fn = getattr(self.ocr, "recognize_lines", None)
        if not callable(fn):
            return False
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return False
        for p in sig.parameters.values():
            if p.kind == inspect.Parameter.VAR_KEYWORD or p.name == name:
                return True
        return False

    def _call_ocr(
        self,
        crop: np.ndarray,
        *,
        dual_pass: bool = True,
        force_dual_pass: bool = False,
        skip_clahe: bool = False,
    ) -> list[OcrLine]:
        fn = self.ocr.recognize_lines
        kwargs: dict[str, Any] = {}
        if self._ocr_accepts_kwarg("dual_pass"):
            kwargs["dual_pass"] = dual_pass
        if self._ocr_accepts_kwarg("force_dual_pass"):
            kwargs["force_dual_pass"] = force_dual_pass
        if skip_clahe and self._ocr_accepts_kwarg("skip_clahe"):
            kwargs["skip_clahe"] = True
        return fn(crop, **kwargs)

    @staticmethod
    def _estimate_line_rarity(
        card_crop: np.ndarray,
        line: OcrLine,
        tl: int,
        cl: int,
        tt: int,
        ct: int,
        fh: int,
        game_left: int,
        game_top: int,
        exclude_rects: Sequence[tuple[int, int, int, int]] | None,
    ) -> tuple[str | None, dict[str, float]]:
        card_h, card_w = card_crop.shape[:2]
        sx0 = max(0, (tl - cl) + int(line.x) - 20)
        sx1 = min(card_w, (tl - cl) + int(line.x + line.w) + 20)
        by_top = max(0, (tt - ct) - int(fh * 0.08))
        by_bot = max(by_top + 4, (tt - ct) + int(line.y))
        border_strip = card_crop[by_top:by_bot, sx0:sx1]
        rarity_hint, rarity_ratios = estimate_slot_rarity_with_ratios(
            border_strip,
            exclude_rects=exclude_rects,
            strip_origin=(game_left + cl + sx0, game_top + ct + by_top),
        )
        # If overlay badges occluded the top border strip, fall back to outer side borders flanking the title
        if rarity_hint is None and exclude_rects and card_h > 8 and card_w > 16:
            sy0 = max(0, (tt - ct) + int(line.y) - 12)
            sy1 = min(card_h, (tt - ct) + int(line.y + line.h) + 24)
            if sy1 > sy0 + 4:
                side_candidates: list[tuple[str | None, dict[str, float]]] = []
                lx0 = max(0, sx0 - 28)
                lx1 = min(card_w, sx0 + 12)
                if lx1 > lx0 + 4:
                    side_candidates.append(
                        estimate_slot_rarity_with_ratios(
                            card_crop[sy0:sy1, lx0:lx1],
                            exclude_rects=exclude_rects,
                            strip_origin=(game_left + cl + lx0, game_top + ct + sy0),
                        )
                    )
                rx0 = max(0, sx1 - 12)
                rx1 = min(card_w, sx1 + 28)
                if rx1 > rx0 + 4:
                    side_candidates.append(
                        estimate_slot_rarity_with_ratios(
                            card_crop[sy0:sy1, rx0:rx1],
                            exclude_rects=exclude_rects,
                            strip_origin=(game_left + cl + rx0, game_top + ct + sy0),
                        )
                    )
                valid_sides = [(rh, rr) for (rh, rr) in side_candidates if rh is not None]
                if valid_sides:
                    best_side = max(valid_sides, key=lambda item: max(item[1].values()))
                    return best_side
        return rarity_hint, rarity_ratios

    def _match_line_with_diag(
        self,
        raw_text: str,
        rarity_hint: str | None = None,
    ) -> tuple[MatchResult | None, dict[str, Any]]:
        fn = getattr(self.matcher, "match_line_with_diagnostics", None)
        if callable(fn):
            return fn(raw_text, rarity_hint=rarity_hint)
        match = self.matcher.match_line(raw_text, rarity_hint=rarity_hint)
        if match is not None:
            return match, {
                "raw_text": raw_text,
                "matched": True,
                "top1_augment_id": match.augment.augment_id,
                "top1_augment_name": match.augment.name,
                "top1_score": round(float(match.score), 4),
                "top2_augment_id": getattr(match, "top2_augment_id", ""),
                "top2_augment_name": getattr(match, "top2_augment_name", ""),
                "top2_score": float(getattr(match, "top2_score", 0.0)),
                "margin": float(getattr(match, "margin", 0.0)),
                "seq_ratio": float(getattr(match, "seq_ratio", 0.0)),
                "jaccard_ratio": float(getattr(match, "jaccard_ratio", 0.0)),
                "substring_hit": bool(getattr(match, "substring_hit", False)),
                "rarity_boosted": bool(getattr(match, "rarity_boosted", False)),
                "is_near_miss": False,
                "near_miss_top1_id": "",
                "near_miss_top1_name": "",
                "near_miss_top1_score": 0.0,
            }
        return None, {
            "raw_text": raw_text,
            "matched": False,
            "top1_augment_id": "",
            "top1_augment_name": "",
            "top1_score": 0.0,
            "top2_augment_id": "",
            "top2_augment_name": "",
            "top2_score": 0.0,
            "margin": 0.0,
            "seq_ratio": 0.0,
            "jaccard_ratio": 0.0,
            "substring_hit": False,
            "rarity_boosted": False,
            "is_near_miss": False,
            "near_miss_top1_id": "",
            "near_miss_top1_name": "",
            "near_miss_top1_score": 0.0,
        }

    @staticmethod
    def _coarse_slot_for_cx(cx: float, band_w: int, total_slots: int = 3) -> int:
        rel_x = cx / max(1, band_w)
        if total_slots >= 4:
            if rel_x < 0.25:
                return 0
            if rel_x < 0.50:
                return 1
            if rel_x < 0.75:
                return 2
            return 3
        if rel_x < 0.34:
            return 0
        if rel_x < 0.67:
            return 1
        return 2

    @staticmethod
    def _clamp_title_bbox(line: OcrLine, title_w: int, title_h: int) -> tuple[int, int, int, int]:
        x0 = max(0, min(title_w, int(round(line.x))))
        y0 = max(0, min(title_h, int(round(line.y))))
        x1 = max(x0, min(title_w, int(round(line.x + line.w))))
        y1 = max(y0, min(title_h, int(round(line.y + line.h))))
        return (x0, y0, x1, y1)

    @classmethod
    def _build_roi_crops(
        cls,
        title_crop: np.ndarray,
        slots_diag: list[dict[str, Any]],
        near_misses: list[dict[str, Any]],
        total_slots: int,
        band_w: int,
    ) -> dict[str, np.ndarray]:
        """Slice compact per-slot title crops (NEVER full-screen) for DevLab ROI snapshots."""
        th, tw = title_crop.shape[:2]
        if th < 4 or tw < 4:
            return {}
        slice_map = cls._SLOT_SLICE_RATIOS_4 if total_slots >= 4 else cls._SLOT_SLICE_RATIOS_3
        crops: dict[str, np.ndarray] = {}

        def _crop_from_bbox(bbox: tuple[int, int, int, int] | None, slot_idx: int) -> np.ndarray | None:
            if bbox is not None and len(bbox) == 4:
                x0, y0, x1, y1 = bbox
                if x1 > x0 and y1 > y0:
                    px0 = max(0, x0 - 32)
                    py0 = max(0, y0 - 24)
                    px1 = min(tw, x1 + 32)
                    py1 = min(th, y1 + 24)
                    if px1 > px0 + 4 and py1 > py0 + 4:
                        return title_crop[py0:py1, px0:px1].copy()
            r0, r1 = slice_map.get(slot_idx, (0.0, 1.0))
            sx0 = max(0, int(band_w * r0))
            sx1 = min(tw, int(round(band_w * r1)))
            if sx1 > sx0 + 4:
                return title_crop[:, sx0:sx1].copy()
            return None

        for entry in slots_diag:
            sidx = int(entry.get("slot_idx", 0))
            key = f"title_crop_slot_{sidx}"
            if key in crops:
                continue
            sub = _crop_from_bbox(entry.get("title_crop_bbox"), sidx)
            if sub is not None and sub.size > 0:
                crops[key] = sub

        for nm in near_misses[:4]:
            sidx = int(nm.get("slot_idx", 0))
            key = f"title_crop_slot_{sidx}"
            if key in crops:
                continue
            sub = _crop_from_bbox(nm.get("title_crop_bbox"), sidx)
            if sub is not None and sub.size > 0:
                crops[key] = sub

        return crops

    @staticmethod
    def _enhance_slot_crop(slot_crop: np.ndarray) -> np.ndarray:
        """Upscale 2x with INTER_CUBIC and boost bright-text contrast via LAB L-channel CLAHE + stretch."""
        sh, sw = slot_crop.shape[:2]
        upscaled = cv2.resize(slot_crop, (max(1, sw * 2), max(1, sh * 2)), interpolation=cv2.INTER_CUBIC)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        if upscaled.ndim == 2:
            l_enh = clahe.apply(upscaled)
            p_lo, p_hi = float(np.percentile(l_enh, 10)), float(np.percentile(l_enh, 99))
            if p_hi - p_lo > 8.0:
                l_enh = np.clip((l_enh.astype(np.float32) - p_lo) * (255.0 / (p_hi - p_lo)), 0.0, 255.0).astype(np.uint8)
            return cv2.cvtColor(l_enh, cv2.COLOR_GRAY2BGR)

        lab = cv2.cvtColor(upscaled, cv2.COLOR_BGR2LAB)
        l_ch, a_ch, b_ch = cv2.split(lab)
        l_enh = clahe.apply(l_ch)
        p_lo, p_hi = float(np.percentile(l_enh, 10)), float(np.percentile(l_enh, 99))
        if p_hi - p_lo > 8.0:
            l_enh = np.clip((l_enh.astype(np.float32) - p_lo) * (255.0 / (p_hi - p_lo)), 0.0, 255.0).astype(np.uint8)
        return cv2.cvtColor(cv2.merge((l_enh, a_ch, b_ch)), cv2.COLOR_LAB2BGR)

    @staticmethod
    def _in_slot_core(rel_x: float, slot: int, total_slots: int = 3) -> bool:
        if total_slots >= 4:
            cores = ((0.0, 0.24), (0.27, 0.49), (0.51, 0.73), (0.76, 1.01))
            if 0 <= slot < len(cores):
                lo, hi = cores[slot]
                return lo <= rel_x <= hi
            return False
        if slot == 0:
            return rel_x < 0.32
        if slot == 1:
            return 0.36 <= rel_x <= 0.64
        return rel_x > 0.68

    @classmethod
    def _best_template_assignment(
        cls,
        gx_vals: list[float],
        template: tuple[float, ...],
    ) -> tuple[float, float, tuple[int, ...]]:
        """Find strictly-increasing slot assignment minimizing L1 distance to normalized centers."""
        n = len(gx_vals)
        m = len(template)
        best_sum = 1e9
        best_max = 1e9
        best_slots: tuple[int, ...] = tuple(range(n))

        def _dfs(idx: int, start_slot: int, cur_slots: list[int], cur_sum: float, cur_max: float) -> None:
            nonlocal best_sum, best_max, best_slots
            if idx == n:
                if cur_sum < best_sum:
                    best_sum = cur_sum
                    best_max = cur_max
                    best_slots = tuple(cur_slots)
                return
            rem = n - idx
            for s in range(start_slot, m - rem + 1):
                err = abs(gx_vals[idx] - template[s])
                cur_slots.append(s)
                _dfs(idx + 1, s + 1, cur_slots, cur_sum + err, max(cur_max, err))
                cur_slots.pop()

        _dfs(0, 0, [], 0.0, 0.0)
        return best_sum, best_max, best_slots

    def _assign_dynamic_slots(
        self,
        raw_hits: list[tuple[float, DetectedCard]],
        band_w: int,
        fw: int,
        tl: int,
        prev_locked_count: int,
        layout_diag_out: dict[str, Any] | None = None,
    ) -> tuple[dict[int, DetectedCard], int, tuple[int, ...]]:
        """Cluster horizontally separated OCR hits into 3-card or 4-card slots and infer any missing slots."""
        if not raw_hits:
            if layout_diag_out is not None:
                layout_diag_out.update(
                    {
                        "chosen_layout": 3,
                        "cluster_count": 0,
                        "cluster_game_x": [],
                        "template_3_l1_error": None,
                        "template_3_l1_max": None,
                        "template_4_l1_error": None,
                        "template_4_l1_max": None,
                    }
                )
            return {}, 3, ()

        # Sort left-to-right and merge multi-line hits belonging to the same card
        sorted_hits = sorted(raw_hits, key=lambda item: item[0])
        merge_dist = max(12.0, band_w * 0.11)
        clusters: list[tuple[float, DetectedCard]] = []
        for cx, card in sorted_hits:
            if clusters and (cx - clusters[-1][0]) < merge_dist:
                if card.confidence > clusters[-1][1].confidence:
                    clusters[-1] = (cx, card)
            else:
                clusters.append((cx, card))

        # Cap spurious extra hits > 4 by keeping the 4 highest-confidence clusters in left-to-right order
        if len(clusters) > 4:
            top4 = sorted(clusters, key=lambda item: item[1].confidence, reverse=True)[:4]
            clusters = sorted(top4, key=lambda item: item[0])

        def _with_slot(card: DetectedCard, slot: int) -> DetectedCard:
            return DetectedCard(
                slot_index=slot,
                augment=card.augment,
                confidence=card.confidence,
                screen_box=card.screen_box,
                rarity_hint=card.rarity_hint,
            )

        n = len(clusters)
        gx_vals = [(tl + cx) / max(1.0, float(fw)) for cx, _ in clusters]
        t3_sum: float | None = None
        t3_max: float | None = None
        t4_sum: float | None = None
        t4_max: float | None = None
        t4_slots: tuple[int, ...] = ()

        if n <= 3:
            t3_sum, t3_max, _t3_slots = self._best_template_assignment(gx_vals, self._TEMPLATE_3_GAME_X)
        if n <= 4:
            t4_sum, t4_max, t4_slots = self._best_template_assignment(gx_vals, self._TEMPLATE_4_GAME_X)

        def _record_layout(chosen: int) -> None:
            if layout_diag_out is not None:
                layout_diag_out.update(
                    {
                        "chosen_layout": chosen,
                        "cluster_count": n,
                        "cluster_game_x": [round(gx, 4) for gx in gx_vals],
                        "template_3_l1_error": round(t3_sum, 4) if t3_sum is not None else None,
                        "template_3_l1_max": round(t3_max, 4) if t3_max is not None else None,
                        "template_4_l1_error": round(t4_sum, 4) if t4_sum is not None else None,
                        "template_4_l1_max": round(t4_max, 4) if t4_max is not None else None,
                    }
                )

        if n >= 4:
            slot_matches = {i: _with_slot(card, i) for i, (_cx, card) in enumerate(clusters[:4])}
            _record_layout(4)
            return slot_matches, 4, ()

        if n == 3:
            # Determine whether these 3 cards are a complete 3-card layout OR a 4-card layout missing 1 card
            assert t3_sum is not None and t4_sum is not None and t4_max is not None
            min_dist_to_center = min(abs(gx - 0.50) for gx in gx_vals)

            is_four_card_missing_one = (
                (prev_locked_count >= 4 and t4_max <= 0.068 and t4_sum <= t3_sum + 0.03)
                or (min_dist_to_center > 0.052 and t4_max <= 0.055 and t4_sum < t3_sum * 0.60)
            )
            if is_four_card_missing_one:
                slot_matches = {
                    slot: _with_slot(card, slot)
                    for slot, (_cx, card) in zip(t4_slots, clusters)
                }
                missing = tuple(s for s in (0, 1, 2, 3) if s not in slot_matches)
                _record_layout(4)
                return slot_matches, 4, missing

            slot_matches = {i: _with_slot(card, i) for i, (_cx, card) in enumerate(clusters)}
            _record_layout(3)
            return slot_matches, 3, ()

        if n == 2:
            if prev_locked_count >= 4 and t4_max is not None and t4_max <= 0.075:
                slot_matches = {
                    slot: _with_slot(card, slot)
                    for slot, (_cx, card) in zip(t4_slots, clusters)
                }
                missing = tuple(s for s in (0, 1, 2, 3) if s not in slot_matches)
                _record_layout(4)
                return slot_matches, 4, missing

            # Standard 3-card layout missing 1 card (preserve exact rel_x zone semantics)
            slot_matches = {}
            for cx, card in clusters:
                rel_x = cx / max(1, band_w)
                if rel_x < 0.34:
                    slot = 0
                elif rel_x < 0.67:
                    slot = 1
                else:
                    slot = 2
                if slot in slot_matches:
                    # Two distinct horizontal clusters landed in same coarse bucket; push right or left into free slot
                    if slot + 1 <= 2 and (slot + 1) not in slot_matches:
                        slot = slot + 1
                    elif slot - 1 >= 0 and (slot - 1) not in slot_matches:
                        slot = slot - 1
                slot_matches[slot] = _with_slot(card, slot)
            missing = tuple(s for s in (0, 1, 2) if s not in slot_matches)
            _record_layout(3)
            return slot_matches, 3, missing

        # Single card hit
        cx, card = clusters[0]
        rel_x = cx / max(1, band_w)
        slot = 0 if rel_x < 0.34 else (1 if rel_x < 0.67 else 2)
        _record_layout(3)
        return {slot: _with_slot(card, slot)}, 3, ()

    def _match_slot_lines(
        self,
        filtered: Sequence[OcrLine],
        *,
        missing_slot: int,
        title_w: int,
        title_h: int,
        band_w: int,
        band_origin_x: int,
        band_origin_y: int,
        card_crop: np.ndarray,
        tl: int,
        cl: int,
        tt: int,
        ct: int,
        fh: int,
        exclude_rects: Sequence[tuple[int, int, int, int]],
        existing_matches: dict[int, DetectedCard],
        total_slots: int,
    ) -> tuple[DetectedCard | None, dict[str, Any] | None, dict[str, Any] | None]:
        existing_ids = {c.augment.augment_id for c in existing_matches.values()}
        existing_x_spans = [(c.screen_box[0], c.screen_box[2]) for c in existing_matches.values()]
        game_left = band_origin_x - tl
        game_top = band_origin_y - tt
        best_card: DetectedCard | None = None
        best_meta: dict[str, Any] | None = None
        best_near_miss: dict[str, Any] | None = None

        for mline in filtered:
            screen_cx = band_origin_x + mline.cx
            # Ignore lines bleeding from an already-matched neighbor card across the slice overlap
            if any((el - 12) <= screen_cx <= (er + 12) for (el, er) in existing_x_spans):
                continue

            rarity_hint, rarity_ratios = self._estimate_line_rarity(
                card_crop=card_crop,
                line=mline,
                tl=tl,
                cl=cl,
                tt=tt,
                ct=ct,
                fh=fh,
                game_left=game_left,
                game_top=game_top,
                exclude_rects=exclude_rects,
            )

            match, mdiag = self._match_line_with_diag(mline.text, rarity_hint=rarity_hint)
            crop_bbox = self._clamp_title_bbox(mline, title_w, title_h)
            if match is None:
                if mdiag.get("is_near_miss") and float(mline.h) >= 12.0 and float(mline.w) >= 16.0:
                    nm_score = float(mdiag.get("near_miss_top1_score", 0.0))
                    if best_near_miss is None or nm_score > float(best_near_miss.get("near_miss_score", 0.0)):
                        best_near_miss = {
                            "slot_idx": missing_slot,
                            "raw_text": mline.text,
                            "matched_id": "",
                            "matched_name": "",
                            "score": 0.0,
                            "top2_id": str(mdiag.get("top2_augment_id", "")),
                            "top2_name": str(mdiag.get("top2_augment_name", "")),
                            "top2_score": float(mdiag.get("top2_score", 0.0)),
                            "margin": float(mdiag.get("margin", 0.0)),
                            "seq_ratio": float(mdiag.get("seq_ratio", 0.0)),
                            "jaccard_ratio": float(mdiag.get("jaccard_ratio", 0.0)),
                            "rarity_hint": rarity_hint,
                            "rarity_ratios": rarity_ratios,
                            "pass_id": int(getattr(mline, "pass_id", 1)),
                            "substring_hit": bool(mdiag.get("substring_hit", False)),
                            "rarity_boosted": bool(mdiag.get("rarity_boosted", False)),
                            "near_miss_id": str(mdiag.get("near_miss_top1_id", "")),
                            "near_miss_score": nm_score,
                            "title_crop_bbox": crop_bbox,
                            "recovered": False,
                        }
                continue

            rel_x = mline.cx / max(1, band_w)
            if match.augment.augment_id in existing_ids and not self._in_slot_core(
                rel_x, missing_slot, total_slots=total_slots
            ):
                continue

            screen_box = (
                int(round(band_origin_x + mline.x)),
                int(round(band_origin_y + mline.y)),
                int(round(band_origin_x + mline.x + mline.w)),
                int(round(band_origin_y + mline.y + mline.h)),
            )
            card = DetectedCard(
                slot_index=missing_slot,
                augment=match.augment,
                confidence=match.score,
                screen_box=screen_box,
                rarity_hint=rarity_hint,
            )
            if best_card is None or card.confidence > best_card.confidence:
                best_card = card
                best_meta = {
                    "slot_idx": missing_slot,
                    "raw_text": mline.text,
                    "matched_id": match.augment.augment_id,
                    "matched_name": match.augment.name,
                    "score": round(float(match.score), 4),
                    "top2_id": str(mdiag.get("top2_augment_id", getattr(match, "top2_augment_id", ""))),
                    "top2_name": str(mdiag.get("top2_augment_name", getattr(match, "top2_augment_name", ""))),
                    "top2_score": float(mdiag.get("top2_score", getattr(match, "top2_score", 0.0))),
                    "margin": float(mdiag.get("margin", getattr(match, "margin", 0.0))),
                    "seq_ratio": float(mdiag.get("seq_ratio", getattr(match, "seq_ratio", 0.0))),
                    "jaccard_ratio": float(mdiag.get("jaccard_ratio", getattr(match, "jaccard_ratio", 0.0))),
                    "rarity_hint": rarity_hint,
                    "rarity_ratios": rarity_ratios,
                    "pass_id": int(getattr(mline, "pass_id", 1)),
                    "substring_hit": bool(mdiag.get("substring_hit", getattr(match, "substring_hit", False))),
                    "rarity_boosted": bool(mdiag.get("rarity_boosted", getattr(match, "rarity_boosted", False))),
                    "near_miss_id": "",
                    "near_miss_score": 0.0,
                    "title_crop_bbox": crop_bbox,
                    "recovered": True,
                }
        return best_card, best_meta, best_near_miss

    def _recover_slot_trimmed_fallback(
        self,
        masked_slot_crop: np.ndarray,
        *,
        missing_slot: int,
        sx_start: int,
        sy_start: int,
        title_w: int,
        title_h: int,
        band_w: int,
        band_origin_x: int,
        band_origin_y: int,
        card_crop: np.ndarray,
        tl: int,
        cl: int,
        tt: int,
        ct: int,
        fh: int,
        exclude_rects: Sequence[tuple[int, int, int, int]],
        existing_matches: dict[int, DetectedCard],
        total_slots: int,
    ) -> tuple[DetectedCard | None, dict[str, Any] | None, float, int]:
        """Trim left/right 12% metal borders + top 15% distracting edge and run clean skip_clahe OCR."""
        sh, sw = masked_slot_crop.shape[:2]
        trim_x0 = int(round(sw * 0.12))
        trim_x1 = int(round(sw * 0.88))
        trim_y0 = int(round(sh * 0.15))
        if trim_x1 - trim_x0 < 12 or sh - trim_y0 < 12:
            return None, None, 0.0, 0

        trimmed = masked_slot_crop[trim_y0:sh, trim_x0:trim_x1]
        achrom = np.min(trimmed, axis=2) if trimmed.ndim == 3 else trimmed
        trimmed_b15_y1 = max(trim_y0 + 12, int(round(sh * 0.85)))
        trimmed_b15 = masked_slot_crop[trim_y0:trimmed_b15_y1, trim_x0:trim_x1]

        candidates: list[tuple[int, int, float, np.ndarray]] = [
            (0, 0, 1.0, achrom),
            (8, 8, 1.0, cv2.copyMakeBorder(achrom, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=14)),
            (0, 0, 0.5, self._enhance_slot_crop(achrom)),
            (
                8,
                8,
                1.0,
                cv2.copyMakeBorder(trimmed_b15, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=(16, 14, 12)),
            ),
        ]

        extra_ocr_ms = 0.0
        extra_lines = 0
        excl_list = list(exclude_rects)
        for pad_x, pad_y, coord_scale, buf in candidates:
            t0 = time.perf_counter()
            raw_lines = self._call_ocr(buf, dual_pass=True, force_dual_pass=True, skip_clahe=True)
            extra_ocr_ms += (time.perf_counter() - t0) * 1000.0
            if not raw_lines:
                continue
            mapped = [
                OcrLine(
                    text=rline.text,
                    x=sx_start + trim_x0 + rline.x * coord_scale - pad_x,
                    y=sy_start + trim_y0 + rline.y * coord_scale - pad_y,
                    w=rline.w * coord_scale,
                    h=rline.h * coord_scale,
                    pass_id=int(getattr(rline, "pass_id", 1)),
                )
                for rline in raw_lines
            ]
            filtered = filter_excluded_lines(mapped, band_origin_x, band_origin_y, excl_list)
            extra_lines += len(filtered)
            if not filtered:
                continue
            cand_card, cand_meta, _ = self._match_slot_lines(
                filtered,
                missing_slot=missing_slot,
                title_w=title_w,
                title_h=title_h,
                band_w=band_w,
                band_origin_x=band_origin_x,
                band_origin_y=band_origin_y,
                card_crop=card_crop,
                tl=tl,
                cl=cl,
                tt=tt,
                ct=ct,
                fh=fh,
                exclude_rects=exclude_rects,
                existing_matches=existing_matches,
                total_slots=total_slots,
            )
            if cand_card is not None:
                return cand_card, cand_meta, extra_ocr_ms, extra_lines
        return None, None, extra_ocr_ms, extra_lines

    def _recover_slot_subcrop(
        self,
        masked_slot_crop: np.ndarray,
        *,
        missing_slot: int,
        sx_start: int,
        sy_start: int,
        sx_end: int,
        sy_end: int,
        title_w: int,
        title_h: int,
        band_w: int,
        band_origin_x: int,
        band_origin_y: int,
        card_crop: np.ndarray,
        tl: int,
        cl: int,
        tt: int,
        ct: int,
        fh: int,
        exclude_rects: Sequence[tuple[int, int, int, int]],
        existing_matches: dict[int, DetectedCard],
        total_slots: int = 3,
        diag_out: dict[str, Any] | None = None,
    ) -> tuple[DetectedCard | None, int]:
        """Run primary 2x-CLAHE (skip_clahe in WinRT) then clean trimmed fallback on a slot crop."""
        enhanced_crop = self._enhance_slot_crop(masked_slot_crop)
        t_ocr0 = time.perf_counter()
        raw_retry_lines = self._call_ocr(
            enhanced_crop,
            dual_pass=True,
            force_dual_pass=True,
            skip_clahe=True,
        )
        ocr_ms = (time.perf_counter() - t_ocr0) * 1000.0
        raw_retry_shadowed = list(getattr(self.ocr, "last_shadowed_pass2", []) or [])
        retry_shadowed = _filter_excluded_shadowed_pass2(
            raw_retry_shadowed,
            band_origin_x + sx_start,
            band_origin_y + sy_start,
            exclude_rects,
            coord_scale=0.5,
        )

        mapped_lines = [
            OcrLine(
                text=rline.text,
                x=sx_start + rline.x * 0.5,
                y=sy_start + rline.y * 0.5,
                w=rline.w * 0.5,
                h=rline.h * 0.5,
                pass_id=int(getattr(rline, "pass_id", 1)),
            )
            for rline in raw_retry_lines
        ]
        excl_list = list(exclude_rects)
        filtered = filter_excluded_lines(mapped_lines, band_origin_x, band_origin_y, excl_list)
        if diag_out is not None:
            diag_out["ocr_ms"] = ocr_ms
            diag_out["match_ms"] = 0.0
            diag_out["shadowed_pass2"] = retry_shadowed
            diag_out["slot_slice_bbox"] = (sx_start, sy_start, sx_end, sy_end)
            diag_out["matched_meta"] = None
            diag_out["near_miss"] = None

        best_card: DetectedCard | None = None
        best_meta: dict[str, Any] | None = None
        best_near_miss: dict[str, Any] | None = None
        t_match0 = time.perf_counter()
        if filtered:
            best_card, best_meta, best_near_miss = self._match_slot_lines(
                filtered,
                missing_slot=missing_slot,
                title_w=title_w,
                title_h=title_h,
                band_w=band_w,
                band_origin_x=band_origin_x,
                band_origin_y=band_origin_y,
                card_crop=card_crop,
                tl=tl,
                cl=cl,
                tt=tt,
                ct=ct,
                fh=fh,
                exclude_rects=exclude_rects,
                existing_matches=existing_matches,
                total_slots=total_slots,
            )

        total_lines = len(filtered)
        if best_card is None and self._ocr_accepts_kwarg("skip_clahe"):
            fb_card, fb_meta, fb_ocr_ms, fb_lines = self._recover_slot_trimmed_fallback(
                masked_slot_crop,
                missing_slot=missing_slot,
                sx_start=sx_start,
                sy_start=sy_start,
                title_w=title_w,
                title_h=title_h,
                band_w=band_w,
                band_origin_x=band_origin_x,
                band_origin_y=band_origin_y,
                card_crop=card_crop,
                tl=tl,
                cl=cl,
                tt=tt,
                ct=ct,
                fh=fh,
                exclude_rects=exclude_rects,
                existing_matches=existing_matches,
                total_slots=total_slots,
            )
            ocr_ms += fb_ocr_ms
            total_lines += fb_lines
            if fb_card is not None:
                best_card = fb_card
                best_meta = fb_meta

        if diag_out is not None:
            diag_out["ocr_ms"] = ocr_ms
            diag_out["match_ms"] = (time.perf_counter() - t_match0) * 1000.0
            diag_out["matched_meta"] = best_meta
            diag_out["near_miss"] = best_near_miss
        return best_card, total_lines

    def recover_slot_from_crop(
        self,
        slot_crop: np.ndarray,
        *,
        slot_index: int = 0,
        exclude_rects: Sequence[tuple[int, int, int, int]] | None = None,
        existing_matches: dict[int, DetectedCard] | None = None,
        total_slots: int = 3,
        diag_out: dict[str, Any] | None = None,
    ) -> DetectedCard | None:
        """Recover a card directly from a standalone single-slot BGR crop (primary + clean fallback)."""
        if slot_crop is None or slot_crop.size == 0:
            return None
        sh, sw = slot_crop.shape[:2]
        if sh < 4 or sw < 4:
            return None
        excl = list(exclude_rects or ())
        masked = _mask_excluded_regions(slot_crop, 0, 0, excl)
        card, _ = self._recover_slot_subcrop(
            masked,
            missing_slot=slot_index,
            sx_start=0,
            sy_start=0,
            sx_end=sw,
            sy_end=sh,
            title_w=sw,
            title_h=sh,
            band_w=sw,
            band_origin_x=0,
            band_origin_y=0,
            card_crop=slot_crop,
            tl=0,
            cl=0,
            tt=0,
            ct=0,
            fh=max(sh * 19, 1080),
            exclude_rects=excl,
            existing_matches=existing_matches or {},
            total_slots=total_slots,
            diag_out=diag_out,
        )
        return card

    def _recover_missing_slot(
        self,
        missing_slot: int,
        title_crop: np.ndarray,
        card_crop: np.ndarray,
        band_w: int,
        band_origin_x: int,
        band_origin_y: int,
        tl: int,
        cl: int,
        tt: int,
        ct: int,
        fh: int,
        exclude_rects: list[tuple[int, int, int, int]],
        existing_matches: dict[int, DetectedCard],
        total_slots: int = 3,
        diag_out: dict[str, Any] | None = None,
    ) -> tuple[DetectedCard | None, int]:
        """Run targeted 2x contrast-enhanced OCR retry on a single missing slot sub-crop."""
        slice_map = self._SLOT_SLICE_RATIOS_4 if total_slots >= 4 else self._SLOT_SLICE_RATIOS_3
        r0, r1 = slice_map[missing_slot]
        title_h, title_w = title_crop.shape[:2]
        sx_start = max(0, int(band_w * r0))
        sx_end = min(title_w, int(round(band_w * r1)))
        if sx_end - sx_start < 4 or title_h < 4:
            return None, 0

        # Crop tight to neighbor card title Y-band when OCR engine supports modern dual-pass flags
        # (strips badges & card description lines while preserving full-height compatibility for legacy 1-arg test stubs)
        sy_start = 0
        sy_end = title_h
        if existing_matches and self._ocr_accepts_kwarg("force_dual_pass"):
            ny0s = [c.screen_box[1] - band_origin_y for c in existing_matches.values()]
            ny1s = [c.screen_box[3] - band_origin_y for c in existing_matches.values()]
            min_ny0 = min(ny0s)
            max_ny1 = max(ny1s)
            line_span = max(24, int(max_ny1 - min_ny0))
            pad_y = max(18, int(round(line_span * 0.75)))
            cand_y0 = max(0, int(min_ny0) - pad_y)
            cand_y1 = min(title_h, int(max_ny1) + pad_y)
            if cand_y1 - cand_y0 >= 16:
                sy_start, sy_end = cand_y0, cand_y1

        slot_crop = title_crop[sy_start:sy_end, sx_start:sx_end]
        masked_slot_crop = _mask_excluded_regions(
            slot_crop,
            band_origin_x + sx_start,
            band_origin_y + sy_start,
            exclude_rects,
        )
        return self._recover_slot_subcrop(
            masked_slot_crop,
            missing_slot=missing_slot,
            sx_start=sx_start,
            sy_start=sy_start,
            sx_end=sx_end,
            sy_end=sy_end,
            title_w=title_w,
            title_h=title_h,
            band_w=band_w,
            band_origin_x=band_origin_x,
            band_origin_y=band_origin_y,
            card_crop=card_crop,
            tl=tl,
            cl=cl,
            tt=tt,
            ct=ct,
            fh=fh,
            exclude_rects=exclude_rects,
            existing_matches=existing_matches,
            total_slots=total_slots,
            diag_out=diag_out,
        )

    @staticmethod
    def _passes_idle_card_structure_gate(card_crop: np.ndarray, title_crop: np.ndarray) -> bool:
        """<0.5ms 3-column dark-card backdrop + bright-title glyph pre-gate on non-locked idle frames."""
        ch, cw = card_crop.shape[:2]
        th, tw = title_crop.shape[:2]
        if ch < 8 or cw < 24 or th < 8 or tw < 24:
            return False
        cs_y = max(1, ch // 64)
        cs_x = max(1, cw // 180)
        card_sub = card_crop[::cs_y, ::cs_x]
        card_gray = cv2.cvtColor(card_sub, cv2.COLOR_BGR2GRAY) if card_sub.ndim == 3 else card_sub

        ts_y = max(1, th // 64)
        ts_x = max(1, tw // 240)
        title_sub = title_crop[::ts_y, ::ts_x]
        title_gray = cv2.cvtColor(title_sub, cv2.COLOR_BGR2GRAY) if title_sub.ndim == 3 else title_sub

        cg_w = card_gray.shape[1]
        tg_w = title_gray.shape[1]
        if cg_w < 9 or tg_w < 9:
            return False

        valid_cols = 0
        for col in range(3):
            cx0 = (col * cg_w) // 3
            cx1 = ((col + 1) * cg_w) // 3
            tx0 = (col * tg_w) // 3
            tx1 = ((col + 1) * tg_w) // 3
            c_col = card_gray[:, cx0:cx1]
            t_col = title_gray[:, tx0:tx1]
            if c_col.size == 0 or t_col.size == 0:
                continue
            dark_ratio = float(np.count_nonzero(c_col <= 105)) / float(c_col.size)
            if dark_ratio < 0.18:
                continue
            t_max = int(np.max(t_col))
            if t_max < 145:
                continue
            t_dark = int(np.percentile(t_col, 20))
            if (t_max - t_dark) < 45:
                continue
            valid_cols += 1
        return valid_cols >= 2

    def _quick_gate(self, card_crop: np.ndarray, title_crop: np.ndarray) -> tuple[bool, np.ndarray, bool]:
        """Return (should_run_ocr, title_thumb, changed_significantly).

        Skips static unchanged frames for BOTH locked and idle states (preserving pending stabilization
        and missing-frame confirmation), and applies a <0.5ms 3-column dark-card + bright-title
        structural pre-gate on non-locked idle frames.
        """
        gray_title = cv2.cvtColor(title_crop, cv2.COLOR_BGR2GRAY)
        thumb = cv2.resize(gray_title, (240, 48), interpolation=cv2.INTER_AREA)

        changed = True
        if self._last_title_thumb is not None and self._last_title_thumb.shape == thumb.shape:
            diff = np.abs(thumb.astype(np.int16) - self._last_title_thumb.astype(np.int16))
            # Check overlapping 60px windows (step 30px) + 3-slot bands so a rerolled title straddling
            # any 3-card or 4-card boundary can never be split and diluted below threshold.
            slot_diffs = [float(np.mean(diff[:, i * 30 : i * 30 + 60])) for i in range(7)]
            slot_diffs.extend(float(np.mean(diff[:, i * 80 : (i + 1) * 80])) for i in range(3))
            changed = max(slot_diffs) >= 1.8

        if self._locked_cards and not changed and self._missing_frames == 0:
            return False, thumb, False

        # Idle static-frame skip: when no picker is locked/pending/closing and the title band didn't change
        if (
            not self._locked_cards
            and not self._pending_ids
            and self._missing_frames == 0
            and self._last_title_thumb is not None
            and not changed
        ):
            return False, thumb, False

        gray_card = cv2.cvtColor(card_crop, cv2.COLOR_BGR2GRAY)
        if float(np.std(gray_card)) < 12.0:
            return False, thumb, changed

        # <0.5ms 3-column dark-card + bright-title structural pre-gate on non-locked idle frames
        if not self._locked_cards and not self._pending_ids:
            if not self._passes_idle_card_structure_gate(card_crop, title_crop):
                return False, thumb, changed

        return True, thumb, changed

    def process_frame(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect,
        exclude_rects: list[tuple[int, int, int, int]] | None = None,
        force: bool = False,
        capture_ms: float | None = None,
        capture_crops: bool = False,
    ) -> PickerDetectionResult:
        fh, fw = frame_bgr.shape[:2]
        cl, ct, cr, cb = self.card_band_roi.to_pixels(fw, fh)
        tl, tt, tr, tb = self.title_band_roi.to_pixels(fw, fh)

        def _base_gate_diag(skipped: bool) -> dict[str, Any]:
            d: dict[str, Any] = {
                "ocr_p1_ms": 0.0,
                "ocr_p2_ms": 0.0,
                "recovery_ocr_ms": 0.0,
                "match_ms": 0.0,
                "ocr_language_tag": str(
                    getattr(self.ocr, "actual_language_tag", "") or getattr(self.ocr, "language_tag", "")
                ),
                "chosen_layout": len(self._locked_cards) if len(self._locked_cards) >= 4 else 3,
                "template_3_l1_error": None,
                "template_3_l1_max": None,
                "template_4_l1_error": None,
                "template_4_l1_max": None,
                "slots": [],
                "near_misses": [],
                "partial_open": False,
                "recovered_slots": [],
                "recovery_failed_slots": [],
                "shadowed_pass2": [],
                "gate_skipped": skipped,
            }
            if capture_ms is not None:
                d["capture_ms"] = round(float(capture_ms), 3)
            return d

        card_crop = frame_bgr[ct:cb, cl:cr]
        title_crop = frame_bgr[tt:tb, tl:tr]
        if card_crop.size == 0 or title_crop.size == 0:
            return PickerDetectionResult(False, False, False, (), 0, True, True, _base_gate_diag(True))

        should_ocr, thumb, changed = self._quick_gate(card_crop, title_crop)
        if not force and not should_ocr:
            if not self._locked_cards and changed:
                self._last_title_thumb = thumb
            gdiag = _base_gate_diag(True)
            if self._locked_cards:
                return PickerDetectionResult(True, True, False, self._locked_cards, 0, True, True, gdiag)
            return PickerDetectionResult(False, False, False, (), 0, True, True, gdiag)

        prev_locked_cards = self._locked_cards
        prev_locked_ids = tuple(c.augment.augment_id for c in prev_locked_cards)
        self._last_title_thumb = thumb

        band_origin_x = game_rect.left + tl
        band_origin_y = game_rect.top + tt
        excl = exclude_rects or []

        use_dual_pass = bool(prev_locked_cards or self._pending_ids or force)
        ocr_title_crop = _mask_excluded_regions(title_crop, band_origin_x, band_origin_y, excl)
        t_ocr0 = time.perf_counter()
        raw_lines = self._call_ocr(ocr_title_crop, dual_pass=use_dual_pass)
        ocr_total_ms = (time.perf_counter() - t_ocr0) * 1000.0
        ocr_timings = getattr(self.ocr, "last_pass_timings_ms", None)
        if isinstance(ocr_timings, dict) and ("ocr_p1_ms" in ocr_timings or "ocr_p2_ms" in ocr_timings):
            ocr_p1_ms = float(ocr_timings.get("ocr_p1_ms", 0.0))
            ocr_p2_ms = float(ocr_timings.get("ocr_p2_ms", 0.0))
        else:
            ocr_p1_ms = round(ocr_total_ms, 3)
            ocr_p2_ms = 0.0
        raw_shadowed_pass2: list[dict[str, Any]] = list(getattr(self.ocr, "last_shadowed_pass2", []) or [])
        shadowed_pass2 = _filter_excluded_shadowed_pass2(
            raw_shadowed_pass2,
            band_origin_x,
            band_origin_y,
            excl,
            coord_scale=1.0,
        )

        # Reject ARAM Mayhem Stat Anvil popup ('属性锻造器'/'属性铁砧'/'能力值鐵砧' or >=2 '*碎片' lines) immediately
        if _is_stat_anvil_lines(raw_lines, client_locale=self.client_locale):
            self._pending_ids = ()
            self._stable_count = 0
            self._locked_cards = ()
            self._missing_frames = 0
            self._subset_drop_frames = 0
            anvil_diag = _base_gate_diag(False)
            anvil_diag["ocr_p1_ms"] = round(ocr_p1_ms, 3)
            anvil_diag["ocr_p2_ms"] = round(ocr_p2_ms, 3)
            anvil_diag["reason"] = "stat_anvil_filtered"
            anvil_diag["stat_anvil_filtered"] = True
            return PickerDetectionResult(
                picker_visible=False,
                stable=False,
                rerolled=False,
                cards=(),
                ocr_lines_count=len(raw_lines),
                skipped_by_gate=False,
                cards_seen_on_frame=False,
                diagnostics=anvil_diag,
            )

        lines = filter_excluded_lines(raw_lines, band_origin_x, band_origin_y, excl)

        # Match every OCR line first, then dynamically cluster into 3-card or 4-card slots by horizontal cx
        band_w = max(1, tr - tl)
        title_h, title_w = title_crop.shape[:2]
        raw_hits: list[tuple[float, DetectedCard]] = []
        hit_meta_by_box: dict[tuple[int, int, int, int], dict[str, Any]] = {}
        raw_near_misses: list[tuple[float, dict[str, Any]]] = []

        t_match0 = time.perf_counter()
        for line in lines:
            rarity_hint, rarity_ratios = self._estimate_line_rarity(
                card_crop=card_crop,
                line=line,
                tl=tl,
                cl=cl,
                tt=tt,
                ct=ct,
                fh=fh,
                game_left=game_rect.left,
                game_top=game_rect.top,
                exclude_rects=excl,
            )

            match, mdiag = self._match_line_with_diag(line.text, rarity_hint=rarity_hint)
            crop_bbox = self._clamp_title_bbox(line, title_w, title_h)
            if match is None:
                if mdiag.get("is_near_miss") and float(line.h) >= 12.0 and float(line.w) >= 16.0:
                    raw_near_misses.append(
                        (
                            float(line.cx),
                            {
                                "slot_idx": 0,
                                "raw_text": line.text,
                                "matched_id": "",
                                "matched_name": "",
                                "score": 0.0,
                                "top2_id": str(mdiag.get("top2_augment_id", "")),
                                "top2_name": str(mdiag.get("top2_augment_name", "")),
                                "top2_score": float(mdiag.get("top2_score", 0.0)),
                                "margin": float(mdiag.get("margin", 0.0)),
                                "seq_ratio": float(mdiag.get("seq_ratio", 0.0)),
                                "jaccard_ratio": float(mdiag.get("jaccard_ratio", 0.0)),
                                "rarity_hint": rarity_hint,
                                "rarity_ratios": rarity_ratios,
                                "pass_id": int(getattr(line, "pass_id", 1)),
                                "substring_hit": bool(mdiag.get("substring_hit", False)),
                                "rarity_boosted": bool(mdiag.get("rarity_boosted", False)),
                                "near_miss_id": str(mdiag.get("near_miss_top1_id", "")),
                                "near_miss_score": float(mdiag.get("near_miss_top1_score", 0.0)),
                                "title_crop_bbox": crop_bbox,
                                "recovered": False,
                            },
                        )
                    )
                continue

            screen_box = (
                int(round(band_origin_x + line.x)),
                int(round(band_origin_y + line.y)),
                int(round(band_origin_x + line.x + line.w)),
                int(round(band_origin_y + line.y + line.h)),
            )
            card = DetectedCard(
                slot_index=0,
                augment=match.augment,
                confidence=match.score,
                screen_box=screen_box,
                rarity_hint=rarity_hint,
            )
            raw_hits.append((float(line.cx), card))
            prev_meta = hit_meta_by_box.get(screen_box)
            if prev_meta is None or match.score > float(prev_meta.get("score", -1.0)):
                hit_meta_by_box[screen_box] = {
                    "slot_idx": 0,
                    "raw_text": line.text,
                    "matched_id": match.augment.augment_id,
                    "matched_name": match.augment.name,
                    "score": round(float(match.score), 4),
                    "top2_id": str(mdiag.get("top2_augment_id", getattr(match, "top2_augment_id", ""))),
                    "top2_name": str(mdiag.get("top2_augment_name", getattr(match, "top2_augment_name", ""))),
                    "top2_score": float(mdiag.get("top2_score", getattr(match, "top2_score", 0.0))),
                    "margin": float(mdiag.get("margin", getattr(match, "margin", 0.0))),
                    "seq_ratio": float(mdiag.get("seq_ratio", getattr(match, "seq_ratio", 0.0))),
                    "jaccard_ratio": float(mdiag.get("jaccard_ratio", getattr(match, "jaccard_ratio", 0.0))),
                    "rarity_hint": rarity_hint,
                    "rarity_ratios": rarity_ratios,
                    "pass_id": int(getattr(line, "pass_id", 1)),
                    "substring_hit": bool(mdiag.get("substring_hit", getattr(match, "substring_hit", False))),
                    "rarity_boosted": bool(mdiag.get("rarity_boosted", getattr(match, "rarity_boosted", False))),
                    "near_miss_id": "",
                    "near_miss_score": 0.0,
                    "title_crop_bbox": crop_bbox,
                    "recovered": False,
                }
        match_ms = (time.perf_counter() - t_match0) * 1000.0

        layout_diag: dict[str, Any] = {}
        slot_matches, total_slots, missing_slots = self._assign_dynamic_slots(
            raw_hits=raw_hits,
            band_w=band_w,
            fw=fw,
            tl=tl,
            prev_locked_count=len(prev_locked_cards),
            layout_diag_out=layout_diag,
        )

        # Assign coarse slot indices to main-pass near-misses now that total_slots is known,
        # dropping description-line pseudo near-misses sitting below already-matched card titles
        near_misses: list[dict[str, Any]] = []
        near_miss_by_slot: dict[int, dict[str, Any]] = {}
        matched_title_bottoms = [c.screen_box[3] - band_origin_y for c in slot_matches.values()]
        median_matched_bot = float(np.median(matched_title_bottoms)) if matched_title_bottoms else None
        for nm_cx, nm_entry in raw_near_misses:
            nm_slot = self._coarse_slot_for_cx(nm_cx, band_w, total_slots=total_slots)
            bbox = nm_entry.get("title_crop_bbox")
            nm_y0 = int(bbox[1]) if isinstance(bbox, (tuple, list)) and len(bbox) == 4 else 0
            nm_screen_cx = band_origin_x + nm_cx
            matched_card = slot_matches.get(nm_slot)
            if matched_card is not None:
                m_local_y0 = matched_card.screen_box[1] - band_origin_y
                m_local_y1 = matched_card.screen_box[3] - band_origin_y
                if nm_y0 >= (m_local_y0 + 12) or nm_y0 >= (m_local_y1 - 4):
                    continue
            if any(
                (mc.screen_box[0] - 48) <= nm_screen_cx <= (mc.screen_box[2] + 48)
                and nm_y0 >= ((mc.screen_box[3] - band_origin_y) - 4)
                for mc in slot_matches.values()
            ):
                continue
            if len(slot_matches) >= total_slots and median_matched_bot is not None and nm_y0 >= (median_matched_bot - 4.0):
                continue

            nm_entry["slot_idx"] = nm_slot
            near_misses.append(nm_entry)
            prev_nm = near_miss_by_slot.get(nm_slot)
            if prev_nm is None or nm_entry["near_miss_score"] > prev_nm["near_miss_score"]:
                near_miss_by_slot[nm_slot] = nm_entry

        # Targeted per-slot missing-card recovery (3-card missing 1, or 4-card missing 1..2)
        extra_lines_count = 0
        recovery_ocr_ms = 0.0
        recovered_slots: list[int] = []
        recovery_failed_slots: list[int] = []
        recovered_meta_by_slot: dict[int, dict[str, Any]] = {}
        failed_slice_bbox_by_slot: dict[int, tuple[int, int, int, int]] = {}
        if len(slot_matches) >= 2 and missing_slots:
            for missing_slot in missing_slots:
                rdiag: dict[str, Any] = {}
                recovered_card, n_extra = self._recover_missing_slot(
                    missing_slot=missing_slot,
                    title_crop=title_crop,
                    card_crop=card_crop,
                    band_w=band_w,
                    band_origin_x=band_origin_x,
                    band_origin_y=band_origin_y,
                    tl=tl,
                    cl=cl,
                    tt=tt,
                    ct=ct,
                    fh=fh,
                    exclude_rects=excl,
                    existing_matches=slot_matches,
                    total_slots=total_slots,
                    diag_out=rdiag,
                )
                extra_lines_count += n_extra
                recovery_ocr_ms += float(rdiag.get("ocr_ms", 0.0))
                match_ms += float(rdiag.get("match_ms", 0.0))
                if rdiag.get("shadowed_pass2"):
                    shadowed_pass2.extend(rdiag["shadowed_pass2"])
                if recovered_card is not None:
                    slot_matches[missing_slot] = recovered_card
                    self.recovered_slot_count += 1
                    recovered_slots.append(missing_slot)
                    if rdiag.get("matched_meta"):
                        recovered_meta_by_slot[missing_slot] = rdiag["matched_meta"]
                else:
                    self.recovery_failed_slot_count += 1
                    recovery_failed_slots.append(missing_slot)
                    if rdiag.get("slot_slice_bbox"):
                        failed_slice_bbox_by_slot[missing_slot] = rdiag["slot_slice_bbox"]
                    r_nm = rdiag.get("near_miss")
                    if isinstance(r_nm, dict):
                        near_misses.append(r_nm)
                        prev_nm = near_miss_by_slot.get(missing_slot)
                        if prev_nm is None or float(r_nm.get("near_miss_score", 0.0)) > float(
                            prev_nm.get("near_miss_score", 0.0)
                        ):
                            near_miss_by_slot[missing_slot] = r_nm

        # Locked-slot ambiguous reroll guard: preserve high-confidence locked card against low-margin / hover occlusion
        preserved_locked_slots: set[int] = set()
        if prev_locked_cards:
            prev_locked_by_slot = {c.slot_index: c for c in prev_locked_cards}
            for sidx, cand_card in list(slot_matches.items()):
                prev_card = prev_locked_by_slot.get(sidx)
                if (
                    prev_card is None
                    or prev_card.confidence < 0.85
                    or cand_card.augment.augment_id == prev_card.augment.augment_id
                ):
                    continue
                cand_meta = recovered_meta_by_slot.get(sidx) or hit_meta_by_box.get(cand_card.screen_box)
                if cand_meta is not None:
                    cand_score = float(cand_meta.get("score", cand_card.confidence))
                    cand_margin = float(cand_meta.get("margin", 1.0))
                    cand_top2_id = str(cand_meta.get("top2_id", ""))
                else:
                    cand_score = float(cand_card.confidence)
                    cand_margin = 1.0
                    cand_top2_id = ""
                if cand_margin < 0.08 or (cand_score < 0.78 and cand_top2_id == prev_card.augment.augment_id):
                    slot_matches[sidx] = prev_card
                    preserved_locked_slots.add(sidx)
                    if sidx in recovered_meta_by_slot:
                        recovered_meta_by_slot.pop(sidx, None)
                    if sidx in recovered_slots:
                        recovered_slots.remove(sidx)
                        self.recovered_slot_count = max(0, self.recovered_slot_count - 1)
                    self.false_reroll_suppressed_count += 1

        # Suppress spurious background/synergy-tag near-misses when every card slot is cleanly identified
        if len(slot_matches) >= total_slots and all(
            card.confidence >= 0.85 for card in slot_matches.values()
        ):
            near_misses = []
            near_miss_by_slot = {}

        # Assemble per-slot diagnostic list
        slots_diag: list[dict[str, Any]] = []
        matched_slot_indices = set(slot_matches.keys())
        all_diag_slots = sorted(
            matched_slot_indices | set(recovery_failed_slots) | set(near_miss_by_slot.keys())
        )
        for sidx in all_diag_slots:
            if sidx in slot_matches:
                card = slot_matches[sidx]
                if sidx in preserved_locked_slots:
                    px0 = max(0, min(title_w, card.screen_box[0] - band_origin_x))
                    py0 = max(0, min(title_h, card.screen_box[1] - band_origin_y))
                    px1 = max(px0, min(title_w, card.screen_box[2] - band_origin_x))
                    py1 = max(py0, min(title_h, card.screen_box[3] - band_origin_y))
                    meta = {
                        "slot_idx": sidx,
                        "raw_text": card.augment.name,
                        "matched_id": card.augment.augment_id,
                        "matched_name": card.augment.name,
                        "score": round(float(card.confidence), 4),
                        "top2_id": "",
                        "top2_name": "",
                        "top2_score": 0.0,
                        "margin": 1.0,
                        "seq_ratio": 1.0,
                        "jaccard_ratio": 1.0,
                        "rarity_hint": card.rarity_hint,
                        "rarity_ratios": {"gold": 0.0, "prismatic": 0.0, "silver": 0.0},
                        "pass_id": 1,
                        "substring_hit": False,
                        "rarity_boosted": False,
                        "near_miss_id": "",
                        "near_miss_score": 0.0,
                        "title_crop_bbox": (px0, py0, px1, py1),
                        "recovered": False,
                        "ambiguous_reroll_guarded": True,
                    }
                elif sidx in recovered_meta_by_slot:
                    meta = dict(recovered_meta_by_slot[sidx])
                else:
                    base = hit_meta_by_box.get(card.screen_box)
                    meta = dict(base) if base is not None else {
                        "slot_idx": sidx,
                        "raw_text": card.augment.name,
                        "matched_id": card.augment.augment_id,
                        "matched_name": card.augment.name,
                        "score": round(float(card.confidence), 4),
                        "top2_id": "",
                        "top2_name": "",
                        "top2_score": 0.0,
                        "margin": 0.0,
                        "seq_ratio": 0.0,
                        "jaccard_ratio": 0.0,
                        "rarity_hint": card.rarity_hint,
                        "rarity_ratios": {"gold": 0.0, "prismatic": 0.0, "silver": 0.0},
                        "pass_id": 1,
                        "substring_hit": False,
                        "rarity_boosted": False,
                        "near_miss_id": "",
                        "near_miss_score": 0.0,
                        "title_crop_bbox": (0, 0, 0, 0),
                        "recovered": False,
                    }
                meta["slot_idx"] = sidx
                slots_diag.append(meta)
            elif sidx in near_miss_by_slot:
                slots_diag.append(dict(near_miss_by_slot[sidx]))
            elif sidx in recovery_failed_slots:
                slots_diag.append(
                    {
                        "slot_idx": sidx,
                        "raw_text": "",
                        "matched_id": "",
                        "matched_name": "",
                        "score": 0.0,
                        "top2_id": "",
                        "top2_name": "",
                        "top2_score": 0.0,
                        "margin": 0.0,
                        "seq_ratio": 0.0,
                        "jaccard_ratio": 0.0,
                        "rarity_hint": None,
                        "rarity_ratios": {"gold": 0.0, "prismatic": 0.0, "silver": 0.0},
                        "pass_id": 1,
                        "substring_hit": False,
                        "rarity_boosted": False,
                        "near_miss_id": "",
                        "near_miss_score": 0.0,
                        "title_crop_bbox": failed_slice_bbox_by_slot.get(sidx, (0, 0, 0, 0)),
                        "recovered": False,
                    }
                )

        def _finalize_diag(is_partial: bool, picker_open: bool) -> dict[str, Any]:
            diag: dict[str, Any] = {
                "ocr_p1_ms": round(ocr_p1_ms, 3),
                "ocr_p2_ms": round(ocr_p2_ms, 3),
                "recovery_ocr_ms": round(recovery_ocr_ms, 3),
                "match_ms": round(match_ms, 3),
                "ocr_language_tag": str(
                    getattr(self.ocr, "actual_language_tag", "") or getattr(self.ocr, "language_tag", "")
                ),
                "chosen_layout": int(layout_diag.get("chosen_layout", total_slots)),
                "template_3_l1_error": layout_diag.get("template_3_l1_error"),
                "template_3_l1_max": layout_diag.get("template_3_l1_max"),
                "template_4_l1_error": layout_diag.get("template_4_l1_error"),
                "template_4_l1_max": layout_diag.get("template_4_l1_max"),
                "slots": slots_diag,
                "near_misses": near_misses,
                "partial_open": bool(is_partial),
                "recovered_slots": list(recovered_slots),
                "recovery_failed_slots": list(recovery_failed_slots),
                "shadowed_pass2": shadowed_pass2,
                "gate_skipped": False,
            }
            if capture_ms is not None:
                diag["capture_ms"] = round(float(capture_ms), 3)
            if capture_crops:
                has_low_conf_or_narrow = any(
                    (entry.get("matched_id") and (float(entry.get("score", 1.0)) < 0.78 or float(entry.get("margin", 1.0)) < 0.08 or bool(entry.get("substring_hit"))))
                    for entry in slots_diag
                )
                has_anomaly = bool(
                    near_misses
                    or shadowed_pass2
                    or recovered_slots
                    or recovery_failed_slots
                    or is_partial
                    or has_low_conf_or_narrow
                )
                if has_anomaly or picker_open:
                    roi_crops = self._build_roi_crops(
                        title_crop=title_crop,
                        slots_diag=slots_diag,
                        near_misses=near_misses,
                        total_slots=total_slots,
                        band_w=band_w,
                    )
                    if roi_crops:
                        diag["_roi_crops"] = roi_crops
            return diag

        total_lines = len(lines) + extra_lines_count
        cards = tuple(slot_matches[k] for k in sorted(slot_matches.keys()))
        if len(cards) < 2:
            self._missing_frames += 1
            if self._missing_frames >= 2 or force:
                self._pending_ids = ()
                self._stable_count = 0
                self._locked_cards = ()
                self._subset_drop_frames = 0
                return PickerDetectionResult(
                    False, False, False, (), total_lines, False, False, _finalize_diag(False, False)
                )
            return PickerDetectionResult(
                bool(self._locked_cards),
                bool(self._locked_cards),
                False,
                self._locked_cards,
                total_lines,
                False,
                False,
                _finalize_diag(False, bool(self._locked_cards)),
            )

        self._missing_frames = 0
        cur_ids = tuple(c.augment.augment_id for c in cards)
        prev_slot_map = {c.slot_index: c.augment.augment_id for c in prev_locked_cards}
        cur_slot_map = {c.slot_index: c.augment.augment_id for c in cards}

        shared_slots = set(prev_slot_map.keys()) & set(cur_slot_map.keys())
        any_shared_slot_changed = any(prev_slot_map[s] != cur_slot_map[s] for s in shared_slots)
        newly_filled_slots = [
            s
            for s, aid in cur_slot_map.items()
            if s not in prev_slot_map and aid not in prev_locked_ids
        ]

        # 2-frame grace when a single slot momentarily drops out from already-locked cards
        is_strict_subset_dropout = bool(
            prev_locked_cards
            and not any_shared_slot_changed
            and not newly_filled_slots
            and len(cur_slot_map) < len(prev_slot_map)
            and set(cur_ids).issubset(set(prev_locked_ids))
        )
        if is_strict_subset_dropout:
            self._subset_drop_frames += 1
            if not force and self._subset_drop_frames <= 2:
                if cur_ids == self._pending_ids:
                    self._stable_count += 1
                else:
                    self._pending_ids = cur_ids
                    self._stable_count = 1
                self.false_reroll_suppressed_count += 1
                return PickerDetectionResult(
                    picker_visible=True,
                    stable=True,
                    rerolled=False,
                    cards=self._locked_cards,
                    ocr_lines_count=total_lines,
                    skipped_by_gate=False,
                    diagnostics=_finalize_diag(False, True),
                )
        else:
            self._subset_drop_frames = 0

        if prev_locked_cards and cur_ids == prev_locked_ids:
            self._pending_ids = cur_ids
            self._stable_count = max(self._stable_count + 1, 2)
        elif cur_ids == self._pending_ids:
            self._stable_count += 1
        else:
            self._pending_ids = cur_ids
            self._stable_count = 1

        # Animation grace: partial opens (2-card on 3-slot, or 3-card missing 1 on 4-slot) need 3 stable frames
        is_partial_open = not prev_locked_cards and len(cards) < total_slots
        required_stable = 3 if is_partial_open else 2
        is_stable = force or self._stable_count >= required_stable

        # Anti-false-reroll: require genuinely changed shared slot or genuinely new filled slot + non-identical card set
        card_set_changed = cur_ids != prev_locked_ids and set(cur_ids) != set(prev_locked_ids)
        genuine_reroll = bool(
            prev_locked_cards
            and card_set_changed
            and (any_shared_slot_changed or bool(newly_filled_slots))
        )
        rerolled = bool(is_stable and genuine_reroll)

        if is_stable and prev_locked_cards and cur_ids != prev_locked_ids and not genuine_reroll:
            self.false_reroll_suppressed_count += 1

        if is_stable:
            if is_partial_open:
                self.partial_open_count += 1
            self._locked_cards = cards

        return PickerDetectionResult(
            picker_visible=True,
            stable=is_stable,
            rerolled=rerolled,
            cards=cards,
            ocr_lines_count=total_lines,
            skipped_by_gate=False,
            diagnostics=_finalize_diag(is_partial_open, True),
        )
