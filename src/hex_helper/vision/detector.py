"""Augment 3-card picker detector: fast gate, rarity border hint, self-overlay exclusion, reroll debounce."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from hex_helper.config import NormalizedRect
from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.matcher import AugmentMatcher
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


def estimate_slot_rarity(strip_bgr: np.ndarray) -> str | None:
    """Estimate silver / gold / prismatic card border rarity from BGR pixels above a card title."""
    if strip_bgr is None or strip_bgr.size == 0:
        return None
    pts = strip_bgr.astype(np.int16)
    b, g, r = pts[..., 0], pts[..., 1], pts[..., 2]
    low = np.minimum(np.minimum(b, g), r)
    high = np.maximum(np.maximum(b, g), r)
    total = float(b.size)
    if total <= 0:
        return None

    # Gold: warm amber/gold (inspired by verified LoL card frame thresholds)
    gold_ratio = float(
        np.count_nonzero((r >= 130) & (g >= 95) & (b <= 125) & (r - g >= 12) & (r - b >= 45))
    ) / total
    # Prismatic: high-saturation magenta / violet / cyan iridescent pixels
    hsv = cv2.cvtColor(strip_bgr, cv2.COLOR_BGR2HSV)
    h_ch, s_ch, v_ch = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    prism_ratio = float(
        np.count_nonzero((s_ch >= 95) & (v_ch >= 110) & (((h_ch >= 115) & (h_ch <= 170)) | ((h_ch >= 82) & (h_ch <= 108))))
    ) / total
    # Silver: bright neutral metallic silver-blue
    silver_ratio = float(
        np.count_nonzero((low >= 110) & (high <= 225) & (high - low <= 28) & (b >= r - 8))
    ) / total

    best = max(
        ("gold", gold_ratio),
        ("prismatic", prism_ratio),
        ("silver", silver_ratio),
        key=lambda x: x[1],
    )
    return best[0] if best[1] >= 0.035 else None


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
    ) -> None:
        self.ocr = ocr_engine
        self.matcher = matcher
        self.card_band_roi = card_band_roi
        self.title_band_roi = title_band_roi

        self._last_title_thumb: np.ndarray | None = None
        self._pending_ids: tuple[str, ...] = ()
        self._stable_count: int = 0
        self._locked_cards: tuple[DetectedCard, ...] = ()
        self._missing_frames: int = 0
        self._subset_drop_frames: int = 0

        self.partial_open_count: int = 0
        self.recovered_slot_count: int = 0
        self.false_reroll_suppressed_count: int = 0

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
    ) -> tuple[dict[int, DetectedCard], int, tuple[int, ...]]:
        """Cluster horizontally separated OCR hits into 3-card or 4-card slots and infer any missing slots."""
        if not raw_hits:
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
        if n >= 4:
            slot_matches = {i: _with_slot(card, i) for i, (_cx, card) in enumerate(clusters[:4])}
            return slot_matches, 4, ()

        gx_vals = [(tl + cx) / max(1.0, float(fw)) for cx, _ in clusters]

        if n == 3:
            # Determine whether these 3 cards are a complete 3-card layout OR a 4-card layout missing 1 card
            t3_sum, _t3_max, _t3_slots = self._best_template_assignment(gx_vals, self._TEMPLATE_3_GAME_X)
            t4_sum, t4_max, t4_slots = self._best_template_assignment(gx_vals, self._TEMPLATE_4_GAME_X)
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
                return slot_matches, 4, missing

            slot_matches = {i: _with_slot(card, i) for i, (_cx, card) in enumerate(clusters)}
            return slot_matches, 3, ()

        if n == 2:
            if prev_locked_count >= 4:
                _t4_sum, t4_max, t4_slots = self._best_template_assignment(gx_vals, self._TEMPLATE_4_GAME_X)
                if t4_max <= 0.075:
                    slot_matches = {
                        slot: _with_slot(card, slot)
                        for slot, (_cx, card) in zip(t4_slots, clusters)
                    }
                    missing = tuple(s for s in (0, 1, 2, 3) if s not in slot_matches)
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
            return slot_matches, 3, missing

        # Single card hit
        cx, card = clusters[0]
        rel_x = cx / max(1, band_w)
        slot = 0 if rel_x < 0.34 else (1 if rel_x < 0.67 else 2)
        return {slot: _with_slot(card, slot)}, 3, ()

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
    ) -> tuple[DetectedCard | None, int]:
        """Run targeted 2x contrast-enhanced OCR retry on a single missing slot sub-crop."""
        slice_map = self._SLOT_SLICE_RATIOS_4 if total_slots >= 4 else self._SLOT_SLICE_RATIOS_3
        r0, r1 = slice_map[missing_slot]
        sx_start = max(0, int(band_w * r0))
        sx_end = min(title_crop.shape[1], int(round(band_w * r1)))
        if sx_end - sx_start < 4 or title_crop.shape[0] < 4:
            return None, 0

        slot_crop = title_crop[:, sx_start:sx_end]
        enhanced_crop = self._enhance_slot_crop(slot_crop)
        raw_retry_lines = self.ocr.recognize_lines(enhanced_crop)

        mapped_lines = [
            OcrLine(
                text=rline.text,
                x=sx_start + rline.x * 0.5,
                y=rline.y * 0.5,
                w=rline.w * 0.5,
                h=rline.h * 0.5,
            )
            for rline in raw_retry_lines
        ]
        filtered = filter_excluded_lines(mapped_lines, band_origin_x, band_origin_y, exclude_rects)
        if not filtered:
            return None, 0

        existing_ids = {c.augment.augment_id for c in existing_matches.values()}
        existing_x_spans = [(c.screen_box[0], c.screen_box[2]) for c in existing_matches.values()]
        card_w = card_crop.shape[1]
        best_card: DetectedCard | None = None

        for mline in filtered:
            screen_cx = band_origin_x + mline.cx
            # Ignore lines bleeding from an already-matched neighbor card across the slice overlap
            if any((el - 12) <= screen_cx <= (er + 12) for (el, er) in existing_x_spans):
                continue

            sx0 = max(0, (tl - cl) + int(mline.x) - 20)
            sx1 = min(card_w, (tl - cl) + int(mline.x + mline.w) + 20)
            by_top = max(0, (tt - ct) - int(fh * 0.08))
            by_bot = max(by_top + 4, (tt - ct) + int(mline.y))
            border_strip = card_crop[by_top:by_bot, sx0:sx1]
            rarity_hint = estimate_slot_rarity(border_strip)

            match = self.matcher.match_line(mline.text, rarity_hint=rarity_hint)
            if match is None:
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

        return best_card, len(filtered)

    def _quick_gate(self, card_crop: np.ndarray, title_crop: np.ndarray) -> tuple[bool, np.ndarray, bool]:
        """Return (should_run_ocr, title_thumb, changed_significantly).

        Compares per-slot thumbnails across 4 horizontal bands so rerolling any card in 3-card or 4-card
        layouts immediately triggers OCR.
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

        if self._locked_cards and not changed:
            return False, thumb, False

        gray_card = cv2.cvtColor(card_crop, cv2.COLOR_BGR2GRAY)
        if float(np.std(gray_card)) < 12.0:
            return False, thumb, changed

        return True, thumb, changed

    def process_frame(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect,
        exclude_rects: list[tuple[int, int, int, int]] | None = None,
        force: bool = False,
    ) -> PickerDetectionResult:
        fh, fw = frame_bgr.shape[:2]
        cl, ct, cr, cb = self.card_band_roi.to_pixels(fw, fh)
        tl, tt, tr, tb = self.title_band_roi.to_pixels(fw, fh)

        card_crop = frame_bgr[ct:cb, cl:cr]
        title_crop = frame_bgr[tt:tb, tl:tr]
        if card_crop.size == 0 or title_crop.size == 0:
            return PickerDetectionResult(False, False, False, (), 0, True)

        should_ocr, thumb, changed = self._quick_gate(card_crop, title_crop)
        if not force and not should_ocr:
            if self._locked_cards:
                return PickerDetectionResult(True, True, False, self._locked_cards, 0, True)
            return PickerDetectionResult(False, False, False, (), 0, True)

        prev_locked_cards = self._locked_cards
        prev_locked_ids = tuple(c.augment.augment_id for c in prev_locked_cards)
        self._last_title_thumb = thumb

        band_origin_x = game_rect.left + tl
        band_origin_y = game_rect.top + tt
        excl = exclude_rects or []

        raw_lines = self.ocr.recognize_lines(title_crop)
        lines = filter_excluded_lines(raw_lines, band_origin_x, band_origin_y, excl)

        # Match every OCR line first, then dynamically cluster into 3-card or 4-card slots by horizontal cx
        band_w = max(1, tr - tl)
        card_w = card_crop.shape[1]
        raw_hits: list[tuple[float, DetectedCard]] = []

        for line in lines:
            # Sample border region slightly above the title line inside card_crop
            sx0 = max(0, (tl - cl) + int(line.x) - 20)
            sx1 = min(card_w, (tl - cl) + int(line.x + line.w) + 20)
            by_top = max(0, (tt - ct) - int(fh * 0.08))
            by_bot = max(by_top + 4, (tt - ct) + int(line.y))
            border_strip = card_crop[by_top:by_bot, sx0:sx1]
            rarity_hint = estimate_slot_rarity(border_strip)

            match = self.matcher.match_line(line.text, rarity_hint=rarity_hint)
            if match is None:
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

        slot_matches, total_slots, missing_slots = self._assign_dynamic_slots(
            raw_hits=raw_hits,
            band_w=band_w,
            fw=fw,
            tl=tl,
            prev_locked_count=len(prev_locked_cards),
        )

        # Targeted per-slot missing-card recovery (3-card missing 1, or 4-card missing 1..2)
        extra_lines_count = 0
        if len(slot_matches) >= 2 and missing_slots:
            for missing_slot in missing_slots:
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
                )
                extra_lines_count += n_extra
                if recovered_card is not None:
                    slot_matches[missing_slot] = recovered_card
                    self.recovered_slot_count += 1

        total_lines = len(lines) + extra_lines_count
        cards = tuple(slot_matches[k] for k in sorted(slot_matches.keys()))
        if len(cards) < 2:
            self._missing_frames += 1
            if self._missing_frames >= 2 or force:
                self._pending_ids = ()
                self._stable_count = 0
                self._locked_cards = ()
                self._subset_drop_frames = 0
                return PickerDetectionResult(False, False, False, (), total_lines, False, False)
            return PickerDetectionResult(
                bool(self._locked_cards), bool(self._locked_cards), False, self._locked_cards, total_lines, False, False
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

        # 1-frame grace when a single slot momentarily drops out from already-locked cards
        is_strict_subset_dropout = bool(
            prev_locked_cards
            and not any_shared_slot_changed
            and not newly_filled_slots
            and len(cur_slot_map) < len(prev_slot_map)
            and set(cur_ids).issubset(set(prev_locked_ids))
        )
        if is_strict_subset_dropout:
            self._subset_drop_frames += 1
            if not force and self._subset_drop_frames == 1:
                self.false_reroll_suppressed_count += 1
                return PickerDetectionResult(
                    picker_visible=True,
                    stable=True,
                    rerolled=False,
                    cards=self._locked_cards,
                    ocr_lines_count=total_lines,
                    skipped_by_gate=False,
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
        )
