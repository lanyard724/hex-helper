"""v1.5.0 WS-C OCR guards:
1. AugmentMatcher 2-char ambiguous prefix rejection vs exact 2-char augment preservation.
2. AugmentPickerDetector locked-slot ambiguous reroll guard (cursor-hover occlusion protection).
3. Self-overlay pixel pre-masking (`_mask_excluded_regions`) & `shadowed_pass2` exclusion filtering.
4. Tiny glyph (<12px h or <16px w) and all-slots-matched near-miss noise suppression.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hex_helper.config import NormalizedRect
from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.detector import (
    AugmentPickerDetector,
    _filter_excluded_shadowed_pass2,
    _mask_excluded_regions,
)
from hex_helper.vision.matcher import AugmentMatcher, MatchResult
from hex_helper.vision.ocr_winrt import OcrLine, WinRtOcrEngine


def _make_v150_catalog() -> list[AugmentInfo]:
    return [
        # Exact 2-char augments that MUST keep matching at 1.0
        AugmentInfo("1101", "灵巧", "silver"),
        AugmentInfo("1102", "残忍", "prismatic"),
        AugmentInfo("1103", "夜狩", "gold"),
        AugmentInfo("1104", "豪猪", "silver"),
        AugmentInfo("1105", "捐赠", "silver"),
        AugmentInfo("1106", "侵蚀", "gold"),
        # Real-world m_00011 collision pair ("升 级" prefix tie)
        AugmentInfo("1318", "升级：中娅", "gold"),
        AugmentInfo("1319", "升级：献祭", "gold"),
        AugmentInfo("1320", "升级：狂妄", "gold"),
        # Normal multi-char augments
        AugmentInfo("1050", "无尽大杀四方", "gold"),
        AugmentInfo("3058", "嗡嗡怪兽", "gold"),
        AugmentInfo("3081", "命运之武器", "gold"),
        AugmentInfo("3131", "屹立不倒", "gold"),
        # Near-miss targets for synergy-tag / background-noise tests (0.44..0.56 score -> genuine near-miss)
        AugmentInfo("2201", "治疗增幅", "gold"),
        AugmentInfo("2202", "魔法飞弹", "gold"),
        AugmentInfo("2203", "匕首", "gold"),
    ]


def _make_textured_frame(seed: int = 1, w: int = 2560, h: int = 1440) -> np.ndarray:
    rng = np.random.default_rng(seed)
    frame = rng.integers(25, 185, size=(h, w, 3), dtype=np.uint8)
    frame[380:460, 420:2140] = (55, 165, 215)
    return frame


def _find_cn_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    raise RuntimeError("No Chinese TrueType font found")


# ---------------------------------------------------------------------------
# 1. Matcher: exact 2-char augments preserved, ambiguous 2-char prefix rejected
# ---------------------------------------------------------------------------


def test_matcher_preserves_exact_two_char_augments_and_rejects_ambiguous_prefix() -> None:
    matcher = AugmentMatcher(_make_v150_catalog())

    # Exact 2-char augments MUST match cleanly at score == 1.0 (with and without rarity hint)
    expected_two_char = [
        ("1101", "灵巧", "silver"),
        ("1102", "残忍", "prismatic"),
        ("1103", "夜狩", "gold"),
        ("1104", "豪猪", "silver"),
        ("1105", "捐赠", "silver"),
        ("1106", "侵蚀", "gold"),
    ]
    for aug_id, name, rarity in expected_two_char:
        res_plain, diag_plain = matcher.match_line_with_diagnostics(name)
        assert res_plain is not None, f"Exact 2-char augment {name!r} was unexpectedly rejected"
        assert res_plain.augment.augment_id == aug_id
        assert res_plain.score == 1.0
        assert diag_plain["matched"] is True
        assert diag_plain["ambiguous_rejected"] is False
        assert matcher.match_line(name) == res_plain

        res_hint = matcher.match_line(name, rarity_hint=rarity)
        assert res_hint is not None
        assert res_hint.augment.augment_id == aug_id
        assert res_hint.score == 1.0

    # Real m_00011 ev#174 case: cursor hides "献祭" in "升级：献祭" -> raw OCR "升 级"
    for raw_prefix in ("升 级", "升级"):
        res, diag = matcher.match_line_with_diagnostics(raw_prefix)
        assert res is None, f"Ambiguous prefix {raw_prefix!r} must be rejected, got {res}"
        assert diag["matched"] is False
        assert diag["ambiguous_rejected"] is True
        assert diag["top1_score"] < 0.85
        assert diag["margin"] < 0.03
        assert diag["is_near_miss"] is False
        assert matcher.match_line(raw_prefix) is None

        # Even with matching gold rarity hint (all 升级：* candidates share gold rarity -> margin remains 0.0)
        res_gold, diag_gold = matcher.match_line_with_diagnostics(raw_prefix, rarity_hint="gold")
        assert res_gold is None
        assert diag_gold["matched"] is False
        assert diag_gold["ambiguous_rejected"] is True
        assert diag_gold["margin"] == 0.0
        assert matcher.match_line(raw_prefix, rarity_hint="gold") is None

    # Full titles still match at 1.0 cleanly
    r_immolate = matcher.match_line("升级：献祭")
    r_zhonya = matcher.match_line("升级：中娅")
    assert r_immolate is not None and r_immolate.augment.augment_id == "1319" and r_immolate.score == 1.0
    assert r_zhonya is not None and r_zhonya.augment.augment_id == "1318" and r_zhonya.score == 1.0


# ---------------------------------------------------------------------------
# 2. Detector: Locked-slot ambiguous reroll guard
# ---------------------------------------------------------------------------


def test_locked_slot_guard_preserves_card_on_low_margin_or_top2_collision_and_allows_real_reroll() -> None:
    catalog = _make_v150_catalog()
    real_matcher = AugmentMatcher(catalog)
    aug_by_id = {a.augment_id: a for a in catalog}

    class _ControllableMatcher:
        """Delegates to real AugmentMatcher unless a synthetic hover token is injected."""

        def __init__(self) -> None:
            self.overrides: dict[str, tuple[MatchResult | None, dict[str, Any]]] = {}

        def match_line_with_diagnostics(
            self,
            raw_text: str,
            rarity_hint: str | None = None,
            min_score: float = 0.58,
        ) -> tuple[MatchResult | None, dict[str, Any]]:
            if raw_text in self.overrides:
                return self.overrides[raw_text]
            return real_matcher.match_line_with_diagnostics(raw_text, rarity_hint=rarity_hint, min_score=min_score)

        def match_line(
            self,
            raw_text: str,
            rarity_hint: str | None = None,
            min_score: float = 0.58,
        ) -> MatchResult | None:
            res, _ = self.match_line_with_diagnostics(raw_text, rarity_hint=rarity_hint, min_score=min_score)
            return res

    matcher = _ControllableMatcher()
    # Case 1: margin == 0.0 (even with score 0.80) -> MUST guard
    matcher.overrides["HOVER_ZERO_MARGIN"] = (
        MatchResult(
            augment=aug_by_id["1318"],
            score=0.80,
            raw_text="HOVER_ZERO_MARGIN",
            top2_augment_id="1319",
            top2_augment_name="升级：献祭",
            top2_score=0.80,
            margin=0.0,
        ),
        {
            "raw_text": "HOVER_ZERO_MARGIN",
            "matched": True,
            "top1_augment_id": "1318",
            "top1_augment_name": "升级：中娅",
            "top1_score": 0.80,
            "top2_augment_id": "1319",
            "top2_augment_name": "升级：献祭",
            "top2_score": 0.80,
            "margin": 0.0,
            "seq_ratio": 0.80,
            "jaccard_ratio": 0.80,
            "substring_hit": False,
            "rarity_boosted": False,
            "is_near_miss": False,
            "near_miss_top1_id": "",
            "near_miss_top1_name": "",
            "near_miss_top1_score": 0.0,
        },
    )
    # Case 2: score == 0.70 < 0.78 and top2_id == prev_card ("1319") even when margin >= 0.08 -> MUST guard
    matcher.overrides["HOVER_LOW_SCORE_TOP2_PREV"] = (
        MatchResult(
            augment=aug_by_id["1318"],
            score=0.70,
            raw_text="HOVER_LOW_SCORE_TOP2_PREV",
            top2_augment_id="1319",
            top2_augment_name="升级：献祭",
            top2_score=0.60,
            margin=0.10,
        ),
        {
            "raw_text": "HOVER_LOW_SCORE_TOP2_PREV",
            "matched": True,
            "top1_augment_id": "1318",
            "top1_augment_name": "升级：中娅",
            "top1_score": 0.70,
            "top2_augment_id": "1319",
            "top2_augment_name": "升级：献祭",
            "top2_score": 0.60,
            "margin": 0.10,
            "seq_ratio": 0.70,
            "jaccard_ratio": 0.70,
            "substring_hit": False,
            "rarity_boosted": False,
            "is_near_miss": False,
            "near_miss_top1_id": "",
            "near_miss_top1_name": "",
            "near_miss_top1_score": 0.0,
        },
    )

    card_roi = NormalizedRect(0.14, 0.24, 0.72, 0.48)
    title_roi = NormalizedRect(0.14, 0.31, 0.72, 0.25)
    game_rect = WindowRect(left=0, top=0, width=2560, height=1440, hwnd=1)
    tl, _, tr, _ = title_roi.to_pixels(2560, 1440)
    band_w = tr - tl

    line_s0 = OcrLine("嗡嗡怪兽", x=band_w * 0.12, y=140.0, w=160.0, h=36.0)
    line_s1 = OcrLine("命运之武器", x=band_w * 0.45, y=140.0, w=180.0, h=36.0)
    line_s2_orig = OcrLine("升级：献祭", x=band_w * 0.78, y=140.0, w=180.0, h=36.0)
    line_s2_hover_zero = OcrLine("HOVER_ZERO_MARGIN", x=band_w * 0.78, y=140.0, w=180.0, h=36.0)
    line_s2_hover_low = OcrLine("HOVER_LOW_SCORE_TOP2_PREV", x=band_w * 0.78, y=140.0, w=180.0, h=36.0)
    line_s2_reroll = OcrLine("无尽大杀四方", x=band_w * 0.78, y=140.0, w=200.0, h=36.0)
    line_s2_reroll_zhonya = OcrLine("升级：中娅", x=band_w * 0.78, y=140.0, w=180.0, h=36.0)

    class _ScriptedOcr:
        def __init__(self, frames: list[list[OcrLine]]) -> None:
            self._frames = list(frames)
            self._idx = 0

        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
        ) -> list[OcrLine]:
            if crop.shape[1] != band_w:
                return []
            out = self._frames[min(self._idx, len(self._frames) - 1)]
            self._idx += 1
            return out

    ocr = _ScriptedOcr(
        [
            [line_s0, line_s1, line_s2_orig],        # f0: pending
            [line_s0, line_s1, line_s2_orig],        # f1: lock 3058, 3081, 1319 (conf=1.0)
            [line_s0, line_s1, line_s2_hover_zero],  # f2: hover margin=0.0 -> guarded!
            [line_s0, line_s1, line_s2_hover_zero],  # f3: sustained hover margin=0.0 -> STILL guarded!
            [line_s0, line_s1, line_s2_hover_low],   # f4: hover score=0.70, top2=1319 -> guarded!
            [line_s0, line_s1, line_s2_hover_low],   # f5: sustained hover score=0.70 -> STILL guarded!
            [line_s0, line_s1, line_s2_reroll],      # f6: genuine reroll -> 无尽大杀四方 (pending)
            [line_s0, line_s1, line_s2_reroll],      # f7: genuine reroll stabilizes -> rerolled=True!
            [line_s0, line_s1, line_s2_reroll_zhonya],  # f8: genuine reroll -> 升级：中娅 (pending)
            [line_s0, line_s1, line_s2_reroll_zhonya],  # f9: genuine reroll stabilizes -> rerolled=True!
        ]
    )
    det = AugmentPickerDetector(ocr, matcher, card_roi, title_roi)  # type: ignore[arg-type]
    results = [det.process_frame(_make_textured_frame(seed=500 + i), game_rect) for i in range(10)]

    # Locked on f1
    assert results[1].stable is True and results[1].rerolled is False
    assert [c.augment.augment_id for c in results[1].cards] == ["3058", "3081", "1319"]

    # f2..f5: simulated cursor hover MUST preserve 1319 (升级：献祭) and NEVER trigger reroll
    for fi in (2, 3, 4, 5):
        r = results[fi]
        assert r.picker_visible is True
        assert r.rerolled is False, f"Frame {fi} falsely triggered reroll during hover!"
        assert [c.augment.augment_id for c in r.cards] == ["3058", "3081", "1319"]
        slot2_diag = next(s for s in r.diagnostics["slots"] if s["slot_idx"] == 2)
        assert slot2_diag["matched_id"] == "1319"
        assert slot2_diag["ambiguous_reroll_guarded"] is True
    assert det.false_reroll_suppressed_count == 4

    # f6..f7: genuine high-margin reroll (升级：献祭 -> 无尽大杀四方) registers immediately
    assert results[6].rerolled is False
    assert results[7].stable is True and results[7].rerolled is True
    assert [c.augment.augment_id for c in results[7].cards] == ["3058", "3081", "1050"]

    # f8..f9: genuine high-margin reroll (无尽大杀四方 -> 升级：中娅) also registers cleanly
    assert results[9].stable is True and results[9].rerolled is True
    assert [c.augment.augment_id for c in results[9].cards] == ["3058", "3081", "1318"]


def test_locked_slot_guard_protects_against_ambiguous_recovered_slot() -> None:
    """If slot 2 drops in main pass and recovery guesses an ambiguous card, preserve locked card."""
    catalog = _make_v150_catalog()
    real_matcher = AugmentMatcher(catalog)
    aug_by_id = {a.augment_id: a for a in catalog}

    class _RecoveryAmbiguousMatcher:
        def match_line_with_diagnostics(
            self,
            raw_text: str,
            rarity_hint: str | None = None,
            min_score: float = 0.58,
        ) -> tuple[MatchResult | None, dict[str, Any]]:
            if raw_text == "RECOVERED_AMBIG":
                return (
                    MatchResult(
                        augment=aug_by_id["1318"],
                        score=0.72,
                        raw_text=raw_text,
                        top2_augment_id="1319",
                        top2_augment_name="升级：献祭",
                        top2_score=0.69,
                        margin=0.03,
                    ),
                    {
                        "raw_text": raw_text,
                        "matched": True,
                        "top1_augment_id": "1318",
                        "top1_augment_name": "升级：中娅",
                        "top1_score": 0.72,
                        "top2_augment_id": "1319",
                        "top2_augment_name": "升级：献祭",
                        "top2_score": 0.69,
                        "margin": 0.03,
                        "seq_ratio": 0.72,
                        "jaccard_ratio": 0.72,
                        "substring_hit": False,
                        "rarity_boosted": False,
                        "is_near_miss": False,
                        "near_miss_top1_id": "",
                        "near_miss_top1_name": "",
                        "near_miss_top1_score": 0.0,
                    },
                )
            return real_matcher.match_line_with_diagnostics(raw_text, rarity_hint=rarity_hint, min_score=min_score)

    card_roi = NormalizedRect(0.14, 0.24, 0.72, 0.48)
    title_roi = NormalizedRect(0.14, 0.31, 0.72, 0.25)
    game_rect = WindowRect(left=0, top=0, width=2560, height=1440, hwnd=1)
    tl, _, tr, _ = title_roi.to_pixels(2560, 1440)
    band_w = tr - tl
    s2_start = int(band_w * 0.64)

    line_s0 = OcrLine("嗡嗡怪兽", x=band_w * 0.12, y=140.0, w=160.0, h=36.0)
    line_s1 = OcrLine("命运之武器", x=band_w * 0.45, y=140.0, w=180.0, h=36.0)
    line_s2 = OcrLine("升级：献祭", x=band_w * 0.78, y=140.0, w=180.0, h=36.0)

    class _DropThenAmbigRecoverOcr:
        def __init__(self) -> None:
            self.full_calls = 0

        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
        ) -> list[OcrLine]:
            if crop.shape[1] == band_w:
                self.full_calls += 1
                if self.full_calls <= 2:
                    return [line_s0, line_s1, line_s2]
                return [line_s0, line_s1]  # slot 2 missed on frame 3 -> triggers recovery
            # Recovery sub-crop returns an ambiguous low-margin candidate for slot 2
            return [OcrLine("RECOVERED_AMBIG", x=(band_w * 0.78 - s2_start) * 2.0, y=40.0, w=200.0, h=50.0)]

    det = AugmentPickerDetector(_DropThenAmbigRecoverOcr(), _RecoveryAmbiguousMatcher(), card_roi, title_roi)  # type: ignore[arg-type]
    det.process_frame(_make_textured_frame(seed=601), game_rect)
    r_lock = det.process_frame(_make_textured_frame(seed=602), game_rect)
    assert r_lock.stable is True
    assert [c.augment.augment_id for c in r_lock.cards] == ["3058", "3081", "1319"]

    r_hover = det.process_frame(_make_textured_frame(seed=603), game_rect)
    assert r_hover.rerolled is False
    assert [c.augment.augment_id for c in r_hover.cards] == ["3058", "3081", "1319"]
    assert r_hover.diagnostics["recovered_slots"] == []
    assert det.recovered_slot_count == 0
    assert det.false_reroll_suppressed_count == 1


# ---------------------------------------------------------------------------
# 3. Self-overlay pixel pre-masking & shadowed_pass2 exclusion filtering
# ---------------------------------------------------------------------------


def test_mask_excluded_regions_paints_overlap_without_mutating_source() -> None:
    src = np.full((100, 200, 3), (220, 210, 200), dtype=np.uint8)

    # No overlap -> returns exact same array without copy
    non_overlap = _mask_excluded_regions(src, origin_x=300, origin_y=400, exclude_rects=[(0, 0, 100, 100)])
    assert non_overlap is src

    # Partial + full overlap -> returns copy with intersecting slices painted (16, 14, 12), source untouched
    masked = _mask_excluded_regions(
        src,
        origin_x=300,
        origin_y=400,
        exclude_rects=[
            (450, 420, 560, 480),  # local x=[150..200], y=[20..80] (clamped at right edge)
            (280, 390, 320, 430),  # local x=[0..20], y=[0..30] (clamped at top-left)
        ],
    )
    assert masked is not src
    assert np.all(src == (220, 210, 200))
    assert np.all(masked[20:80, 150:200] == (16, 14, 12))
    assert np.all(masked[0:30, 0:20] == (16, 14, 12))
    assert np.all(masked[50:90, 60:120] == (220, 210, 200))


def test_detector_pre_masks_overlay_before_ocr_and_filters_shadowed_pass2() -> None:
    matcher = AugmentMatcher(_make_v150_catalog())
    card_roi = NormalizedRect(0.08, 0.24, 0.84, 0.48)
    title_roi = NormalizedRect(0.08, 0.385, 0.84, 0.052)
    fw, fh = 2560, 1440
    game_rect = WindowRect(left=100, top=50, width=fw, height=fh, hwnd=1)
    tl, tt, tr, tb = title_roi.to_pixels(fw, fh)
    band_w, band_h = tr - tl, tb - tt
    band_origin_x = game_rect.left + tl
    band_origin_y = game_rect.top + tt

    # Place simulated edge_drawer on the right edge inside title_band_roi
    drawer_local_x0 = band_w - 220
    drawer_rect = (
        band_origin_x + drawer_local_x0,
        band_origin_y + 4,
        band_origin_x + band_w + 100,
        band_origin_y + band_h + 200,
    )

    frame = _make_textured_frame(seed=701, w=fw, h=fh)
    # Paint bright cyan/white edge_drawer text pixels inside the drawer rect in frame coords
    frame[tt + 4 : tb, tl + drawer_local_x0 : tr] = (245, 240, 235)
    orig_drawer_pixel = tuple(int(v) for v in frame[tt + 10, tl + drawer_local_x0 + 20])
    assert orig_drawer_pixel == (245, 240, 235)

    seen_crops: list[np.ndarray] = []

    class _InspectingOcr:
        def __init__(self) -> None:
            self.last_shadowed_pass2: list[dict[str, Any]] = []
            self.last_pass_timings_ms: dict[str, float] = {"ocr_p1_ms": 4.2, "ocr_p2_ms": 3.1}

        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
        ) -> list[OcrLine]:
            seen_crops.append(crop.copy())
            if crop.shape[1] == band_w:
                # Inject 3 shadowed_pass2 entries:
                # 1. kept_bbox inside drawer_rect -> MUST be dropped
                # 2. dropped_bbox inside drawer_rect -> MUST be dropped
                # 3. real card conflict outside drawer_rect -> MUST be kept
                self.last_shadowed_pass2 = [
                    {
                        "kept_pass1_text": "Gemini · G4-hydrogen",
                        "dropped_pass2_text": "BadRequestError",
                        "kept_bbox": [float(drawer_local_x0 + 10), 10.0, 140.0, 22.0],
                        "dropped_bbox": [float(drawer_local_x0 + 12), 11.0, 138.0, 22.0],
                        "dx": 1.0,
                        "dy": 1.0,
                    },
                    {
                        "kept_pass1_text": "EdgeText",
                        "dropped_pass2_text": "G3.8-flash",
                        "kept_bbox": [float(drawer_local_x0 - 40), 10.0, 20.0, 20.0],
                        "dropped_bbox": [float(drawer_local_x0 + 20), 10.0, 80.0, 20.0],
                        "dx": 20.0,
                        "dy": 0.0,
                    },
                    {
                        "kept_pass1_text": "嗡",
                        "dropped_pass2_text": "嗡嗡怪兽",
                        "kept_bbox": [240.0, 16.0, 120.0, 28.0],
                        "dropped_bbox": [242.0, 17.0, 122.0, 28.0],
                        "dx": 3.0,
                        "dy": 1.0,
                    },
                ]
                # Return only 2 cards so slot 2 recovery ALSO runs (verifying slot_crop masking + 0.5x shadowed filter!)
                return [
                    OcrLine("嗡嗡怪兽", x=band_w * 0.22, y=16.0, w=140.0, h=28.0),
                    OcrLine("命运之武器", x=band_w * 0.46, y=16.0, w=160.0, h=28.0),
                ]
            # Recovery call on slot 2 (2x upscaled sub-crop overlapping drawer on its right edge)
            sx_start = int(band_w * 0.64)
            drawer_in_slot_2x = (drawer_local_x0 - sx_start) * 2.0
            self.last_shadowed_pass2 = [
                {
                    "kept_pass1_text": "DrawerRetryLeak",
                    "dropped_pass2_text": "DrawerRetryLeak2",
                    "kept_bbox": [float(drawer_in_slot_2x + 20), 20.0, 100.0, 30.0],
                    "dropped_bbox": [float(drawer_in_slot_2x + 22), 20.0, 100.0, 30.0],
                    "dx": 2.0,
                    "dy": 0.0,
                }
            ]
            return [
                OcrLine("屹立不倒", x=(band_w * 0.70 - sx_start) * 2.0, y=32.0, w=260.0, h=56.0),
            ]

    det = AugmentPickerDetector(_InspectingOcr(), matcher, card_roi, title_roi)  # type: ignore[arg-type]
    res = det.process_frame(frame, game_rect, exclude_rects=[drawer_rect], force=True)

    # 1. Original frame_bgr MUST NOT be mutated
    assert tuple(int(v) for v in frame[tt + 10, tl + drawer_local_x0 + 20]) == (245, 240, 235)
    assert len(seen_crops) == 2

    # 2. Main title_crop passed to OCR MUST have drawer region painted neutral dark (16, 14, 12)
    main_crop = seen_crops[0]
    assert np.all(main_crop[4:, drawer_local_x0:] == (16, 14, 12))
    # Region outside drawer remains unmasked textured pixels
    assert not np.all(main_crop[:, : drawer_local_x0 - 10] == (16, 14, 12))

    # 3. Recovery slot_crop MUST also have been masked before 2x enhancement (bright 245 overlay replaced by dark fill)
    recovery_crop = seen_crops[1]
    sx_start = int(band_w * 0.64)
    drawer_col_2x = (drawer_local_x0 - sx_start) * 2 + 12
    assert float(np.mean(recovery_crop[20:, drawer_col_2x:])) < 20.0
    assert float(np.max(recovery_crop[20:, drawer_col_2x:])) < 30.0

    # 4. Both main-pass and recovery-pass shadowed_pass2 entries inside drawer_rect MUST be stripped
    shadowed = res.diagnostics["shadowed_pass2"]
    assert len(shadowed) == 1
    assert shadowed[0]["dropped_pass2_text"] == "嗡嗡怪兽"

    # Direct helper check with empty/non-empty exclude_rects
    assert len(_filter_excluded_shadowed_pass2(shadowed, band_origin_x, band_origin_y, [])) == 1


def test_real_winrt_ocr_zero_self_overlay_leakage_with_pixel_premask() -> None:
    """Real Windows WinRT OCR must read zero text from our own edge_drawer when pre-masked."""
    W, H = 2560, 1440
    img = Image.new("RGB", (W, H), (14, 20, 32))
    draw = ImageDraw.Draw(img)
    title_font = _find_cn_font(38)
    drawer_font = _find_cn_font(26)

    for cx, title in ((780, "嗡嗡怪兽"), (1280, "命运之武器"), (1780, "升级：献祭")):
        draw.rectangle([cx - 205, 380, cx + 205, 940], fill=(22, 30, 46), outline=(210, 165, 65), width=8)
        draw.rectangle([cx - 193, 392, cx + 193, 432], fill=(210, 165, 65))
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
        draw.text((cx - tw // 2, 575), title, fill=(246, 240, 224), font=title_font)

    # Render HexHelper's own edge_drawer directly inside the right edge of title_band_roi
    drawer_rect = (2060, 500, 2550, 760)
    draw.rectangle(drawer_rect, fill=(9, 20, 40), outline=(200, 170, 110), width=3)
    draw.text((2080, 565), "Gemini · G4-hydrogen", fill=(235, 245, 255), font=drawer_font)
    draw.text((2080, 600), "BadRequestError 400", fill=(255, 190, 120), font=drawer_font)

    frame = cv2.cvtColor(np.asarray(img, dtype=np.uint8), cv2.COLOR_RGB2BGR)
    ocr = WinRtOcrEngine("zh-Hans-CN")
    try:
        det = AugmentPickerDetector(
            ocr_engine=ocr,
            matcher=AugmentMatcher(_make_v150_catalog()),
            card_band_roi=NormalizedRect(0.08, 0.24, 0.84, 0.48),
            title_band_roi=NormalizedRect(0.08, 0.385, 0.84, 0.065),
        )
        res = det.process_frame(
            frame,
            WindowRect(0, 0, W, H, 0),
            exclude_rects=[drawer_rect],
            force=True,
        )
        assert res.picker_visible is True and res.stable is True
        assert [c.augment.augment_id for c in res.cards] == ["3058", "3081", "1319"]
        assert res.diagnostics["shadowed_pass2"] == []
        assert res.diagnostics["near_misses"] == []
    finally:
        ocr.close()


# ---------------------------------------------------------------------------
# 4. Tiny glyph (<12px h or <16px w) & already-matched-slot near-miss suppression
# ---------------------------------------------------------------------------


def test_tiny_glyph_and_all_slots_matched_near_miss_suppression() -> None:
    matcher = AugmentMatcher(_make_v150_catalog())
    card_roi = NormalizedRect(0.14, 0.24, 0.72, 0.48)
    title_roi = NormalizedRect(0.14, 0.31, 0.72, 0.25)
    game_rect = WindowRect(left=0, top=0, width=2560, height=1440, hwnd=1)
    tl, _, tr, _ = title_roi.to_pixels(2560, 1440)
    band_w = tr - tl

    # Confirm real DevLab noise inputs actually produce matcher near-misses before detector filtering
    nm_tiny_res, nm_tiny_diag = matcher.match_line_with_diagnostics("台 匕")
    assert nm_tiny_res is None and nm_tiny_diag["is_near_miss"] is True
    synergy_text = "持续治疗"
    nm_syn_res, nm_syn_diag = matcher.match_line_with_diagnostics(synergy_text)
    assert nm_syn_res is None and nm_syn_diag["is_near_miss"] is True
    missed_card_text = "魔法克星"
    nm_miss_res, nm_miss_diag = matcher.match_line_with_diagnostics(missed_card_text)
    assert nm_miss_res is None and nm_miss_diag["is_near_miss"] is True

    # Case A: Only 2/3 cards matched + tiny 12x7px glyph + tiny 15x14px glyph + normal missed-card near-miss (96x24px)
    # -> Tiny glyphs MUST be skipped, genuine normal-sized missed-card near-miss MUST be kept!
    class _PartialWithTinyAndRealNearMissOcr:
        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
        ) -> list[OcrLine]:
            if crop.shape[1] != band_w:
                # Recovery pass also sees a tiny 12x7px artifact (24x14px in 2x coords) -> MUST be skipped!
                return [OcrLine("台 匕", x=100.0, y=40.0, w=24.0, h=14.0)]
            return [
                OcrLine("嗡嗡怪兽", x=band_w * 0.12, y=140.0, w=160.0, h=36.0),
                OcrLine("命运之武器", x=band_w * 0.45, y=140.0, w=180.0, h=36.0),
                OcrLine("台 匕", x=band_w * 0.78, y=40.0, w=12.0, h=7.0),          # tiny 12x7px -> DROP
                OcrLine("台 匕", x=band_w * 0.80, y=50.0, w=15.0, h=14.0),         # w < 16 -> DROP
                OcrLine("台 匕", x=band_w * 0.82, y=60.0, w=20.0, h=11.0),         # h < 12 -> DROP
                OcrLine(missed_card_text, x=band_w * 0.78, y=120.0, w=96.0, h=24.0),  # genuine -> KEEP
            ]

    det_partial = AugmentPickerDetector(_PartialWithTinyAndRealNearMissOcr(), matcher, card_roi, title_roi)  # type: ignore[arg-type]
    res_partial = det_partial.process_frame(_make_textured_frame(seed=801), game_rect, force=True)
    assert len(res_partial.cards) == 2
    assert len(res_partial.diagnostics["near_misses"]) == 1
    assert res_partial.diagnostics["near_misses"][0]["raw_text"] == missed_card_text

    # Case B: All 3/3 cards cleanly matched (confidence >= 0.85) + extra background/synergy near-miss ABOVE title
    # -> Spurious near-miss MUST be suppressed so near_misses == []!
    class _FullMatchWithSynergyTagOcr:
        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
        ) -> list[OcrLine]:
            return [
                OcrLine("嗡嗡怪兽", x=band_w * 0.12, y=140.0, w=160.0, h=36.0),
                OcrLine("命运之武器", x=band_w * 0.45, y=140.0, w=180.0, h=36.0),
                OcrLine("屹立不倒", x=band_w * 0.78, y=140.0, w=160.0, h=36.0),
                # Synergy tags sitting ABOVE title (y=40 < 140) with normal bbox size -> suppressed once 3/3 >= 0.85!
                OcrLine(synergy_text, x=band_w * 0.14, y=40.0, w=90.0, h=22.0),
                OcrLine(missed_card_text, x=band_w * 0.78, y=40.0, w=90.0, h=22.0),
                OcrLine("台 匕", x=band_w * 0.48, y=30.0, w=12.0, h=7.0),
            ]

    det_full = AugmentPickerDetector(_FullMatchWithSynergyTagOcr(), matcher, card_roi, title_roi)  # type: ignore[arg-type]
    res_full = det_full.process_frame(_make_textured_frame(seed=802), game_rect, force=True)
    assert len(res_full.cards) == 3
    assert all(c.confidence >= 0.85 for c in res_full.cards)
    assert res_full.diagnostics["near_misses"] == []

    # Case C: 3/3 matched but one card has low confidence (< 0.85) -> non-tiny near-miss IS retained for DevLab
    class _LowConfThirdCardMatcher:
        def match_line_with_diagnostics(
            self,
            raw_text: str,
            rarity_hint: str | None = None,
            min_score: float = 0.58,
        ) -> tuple[MatchResult | None, dict[str, Any]]:
            if raw_text == "LOW_CONF_CARD":
                aug = AugmentInfo("3131", "屹立不倒", "gold")
                return (
                    MatchResult(augment=aug, score=0.72, raw_text=raw_text, margin=0.25),
                    {
                        "raw_text": raw_text,
                        "matched": True,
                        "top1_augment_id": "3131",
                        "top1_augment_name": "屹立不倒",
                        "top1_score": 0.72,
                        "top2_augment_id": "",
                        "top2_augment_name": "",
                        "top2_score": 0.47,
                        "margin": 0.25,
                        "seq_ratio": 0.72,
                        "jaccard_ratio": 0.72,
                        "substring_hit": False,
                        "rarity_boosted": False,
                        "is_near_miss": False,
                        "near_miss_top1_id": "",
                        "near_miss_top1_name": "",
                        "near_miss_top1_score": 0.0,
                    },
                )
            return matcher.match_line_with_diagnostics(raw_text, rarity_hint=rarity_hint, min_score=min_score)

    class _FullMatchLowConfOcr:
        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
        ) -> list[OcrLine]:
            return [
                OcrLine("嗡嗡怪兽", x=band_w * 0.12, y=140.0, w=160.0, h=36.0),
                OcrLine("命运之武器", x=band_w * 0.45, y=140.0, w=180.0, h=36.0),
                OcrLine("LOW_CONF_CARD", x=band_w * 0.78, y=140.0, w=160.0, h=36.0),
                OcrLine(synergy_text, x=band_w * 0.78, y=40.0, w=90.0, h=22.0),
            ]

    det_low = AugmentPickerDetector(_FullMatchLowConfOcr(), _LowConfThirdCardMatcher(), card_roi, title_roi)  # type: ignore[arg-type]
    res_low = det_low.process_frame(_make_textured_frame(seed=803), game_rect, force=True)
    assert len(res_low.cards) == 3
    assert len(res_low.diagnostics["near_misses"]) == 1
    assert res_low.diagnostics["near_misses"][0]["raw_text"] == synergy_text
