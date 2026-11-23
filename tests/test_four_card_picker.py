"""Tests for 4-card ARAM Mayhem augment picker maps (adaptive 3/4-slot clustering, ROI migration, UI & click boxes)."""

from __future__ import annotations

import os
import numpy as np
import pytest

from hex_helper.config import AppConfig, NormalizedRect
from hex_helper.data.provider import AugmentInfo
from hex_helper.scoring.augment_combo import MatchBuildProfile, PickerCardComboAdvice, PickerComboVerdict
from hex_helper.scoring.tier import AugmentScore
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.click_tracker import ClickTracker, build_slot_hit_boxes
from hex_helper.vision.detector import AugmentPickerDetector
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import OcrLine


def _make_augs() -> list[AugmentInfo]:
    return [
        AugmentInfo("2132", "术士果汁盒", "gold", ""),
        AugmentInfo("3116", "将他们放倒倒倒", "gold", ""),
        AugmentInfo("1150", "面包和果酱", "gold", ""),
        AugmentInfo("1211", "杀戮时间到了", "gold", ""),
        AugmentInfo("1004", "回归基本功", "prismatic", ""),
        AugmentInfo("1053", "科学狂人", "prismatic", ""),
    ]


def _textured_frame(w: int = 1920, h: int = 1080, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(30, 220, size=(h, w, 3), dtype=np.uint8)


class _StubOcr:
    def __init__(self, responses: list[list[OcrLine]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[int, int]] = []

    def recognize_lines(self, img: np.ndarray) -> list[OcrLine]:
        self.calls.append((int(img.shape[1]), int(img.shape[0])))
        if self._responses:
            return self._responses.pop(0)
        return []


def test_legacy_3card_roi_migrates_to_wide_4card_roi() -> None:
    raw = {
        "card_band_roi": {"x": 0.14, "y": 0.24, "w": 0.72, "h": 0.48},
        "title_band_roi": {"x": 0.14, "y": 0.31, "w": 0.72, "h": 0.25},
    }
    cfg = AppConfig.from_dict(raw)
    assert cfg.card_band_roi.x == pytest.approx(0.08)
    assert cfg.card_band_roi.w == pytest.approx(0.84)
    assert cfg.title_band_roi.x == pytest.approx(0.08)
    assert cfg.title_band_roi.w == pytest.approx(0.84)

    # Custom user-calibrated ROI must remain untouched
    custom = AppConfig.from_dict({
        "card_band_roi": {"x": 0.11, "y": 0.22, "w": 0.78, "h": 0.50},
        "title_band_roi": {"x": 0.11, "y": 0.30, "w": 0.78, "h": 0.24},
    })
    assert custom.title_band_roi.x == pytest.approx(0.11)
    assert custom.title_band_roi.w == pytest.approx(0.78)


def test_detector_detects_all_4_cards_without_slot_collision() -> None:
    augs = _make_augs()
    matcher = AugmentMatcher(augs)
    cfg = AppConfig()
    fw, fh = 1920, 1080
    tl, _tt, tr, _tb = cfg.title_band_roi.to_pixels(fw, fh)
    band_w = tr - tl
    # Real 4-card centers at game X = 0.185, 0.395, 0.605, 0.815
    cxs = [int(fw * gx) - tl for gx in (0.185, 0.395, 0.605, 0.815)]
    four_lines = [
        OcrLine("术士果汁盒", x=cxs[0] - 55, y=90, w=110, h=28),
        OcrLine("将他们放倒倒倒", x=cxs[1] - 75, y=90, w=150, h=28),
        OcrLine("面包和果酱", x=cxs[2] - 55, y=90, w=110, h=28),
        OcrLine("杀戮时间到了", x=cxs[3] - 65, y=90, w=130, h=28),
    ]
    ocr = _StubOcr([four_lines, four_lines])
    det = AugmentPickerDetector(ocr, matcher, cfg.card_band_roi, cfg.title_band_roi)
    rect = WindowRect(hwnd=1, left=0, top=0, width=fw, height=fh)

    r1 = det.process_frame(_textured_frame(fw, fh, 1), rect)
    assert r1.picker_visible and not r1.stable
    assert len(r1.cards) == 4

    r2 = det.process_frame(_textured_frame(fw, fh, 2), rect)
    assert r2.picker_visible and r2.stable and not r2.rerolled
    assert [(c.slot_index, c.augment.name) for c in r2.cards] == [
        (0, "术士果汁盒"),
        (1, "将他们放倒倒倒"),
        (2, "面包和果酱"),
        (3, "杀戮时间到了"),
    ]
    assert len(ocr.calls) == 2  # Zero wasted per-slot retry calls when all 4 cards found


def test_detector_recovers_missing_4th_card_via_geometry_and_per_slot_retry() -> None:
    augs = _make_augs()
    matcher = AugmentMatcher(augs)
    cfg = AppConfig()
    fw, fh = 1920, 1080
    tl, _tt, tr, _tb = cfg.title_band_roi.to_pixels(fw, fh)
    band_w = tr - tl
    cxs = [int(fw * gx) - tl for gx in (0.185, 0.395, 0.605, 0.815)]

    # Full-band pass misses slot 2 ("面包和果酱"); only slots 0, 1, 3 are seen (none near screen center 0.50!)
    partial_3_of_4 = [
        OcrLine("术士果汁盒", x=cxs[0] - 55, y=90, w=110, h=28),
        OcrLine("将他们放倒倒倒", x=cxs[1] - 75, y=90, w=150, h=28),
        OcrLine("杀戮时间到了", x=cxs[3] - 65, y=90, w=130, h=28),
    ]
    # Per-slot 2x retry on slot 2 (slice starts at 0.48 * band_w) returns 2x coords inside the sub-crop
    s2_start = int(band_w * 0.48)
    retry_slot2 = [
        OcrLine("面包和果酱", x=(cxs[2] - s2_start - 55) * 2, y=180, w=220, h=56),
    ]

    ocr = _StubOcr([partial_3_of_4, retry_slot2])
    det = AugmentPickerDetector(ocr, matcher, cfg.card_band_roi, cfg.title_band_roi)
    rect = WindowRect(hwnd=1, left=0, top=0, width=fw, height=fh)

    res = det.process_frame(_textured_frame(fw, fh, 7), rect, force=True)
    assert res.picker_visible and res.stable
    assert [(c.slot_index, c.augment.name) for c in res.cards] == [
        (0, "术士果汁盒"),
        (1, "将他们放倒倒倒"),
        (2, "面包和果酱"),
        (3, "杀戮时间到了"),
    ]
    assert det.recovered_slot_count == 1


def test_detector_4card_single_slot_reroll_triggers_cleanly() -> None:
    augs = _make_augs()
    matcher = AugmentMatcher(augs)
    cfg = AppConfig()
    fw, fh = 1920, 1080
    tl, _tt, tr, _tb = cfg.title_band_roi.to_pixels(fw, fh)
    cxs = [int(fw * gx) - tl for gx in (0.185, 0.395, 0.605, 0.815)]

    orig = [
        OcrLine("术士果汁盒", x=cxs[0] - 55, y=90, w=110, h=28),
        OcrLine("将他们放倒倒倒", x=cxs[1] - 75, y=90, w=150, h=28),
        OcrLine("面包和果酱", x=cxs[2] - 55, y=90, w=110, h=28),
        OcrLine("杀戮时间到了", x=cxs[3] - 65, y=90, w=130, h=28),
    ]
    rerolled_slot3 = [
        orig[0],
        orig[1],
        orig[2],
        OcrLine("科学狂人", x=cxs[3] - 50, y=90, w=100, h=28),
    ]
    ocr = _StubOcr([orig, orig, rerolled_slot3, rerolled_slot3])
    det = AugmentPickerDetector(ocr, matcher, cfg.card_band_roi, cfg.title_band_roi)
    rect = WindowRect(hwnd=1, left=0, top=0, width=fw, height=fh)

    det.process_frame(_textured_frame(fw, fh, 10), rect)
    r_open = det.process_frame(_textured_frame(fw, fh, 11), rect)
    assert r_open.stable and len(r_open.cards) == 4

    det.process_frame(_textured_frame(fw, fh, 12), rect)
    r_rr = det.process_frame(_textured_frame(fw, fh, 13), rect)
    assert r_rr.stable and r_rr.rerolled
    assert r_rr.cards[3].slot_index == 3 and r_rr.cards[3].augment.name == "科学狂人"


def test_click_tracker_build_slot_hit_boxes_supports_4_cards() -> None:
    rect = WindowRect(hwnd=1, left=0, top=0, width=2560, height=1440)
    card_boxes = {
        0: (400, 620, 540, 650),
        1: (940, 620, 1080, 650),
        2: (1480, 620, 1620, 650),
        3: (2020, 620, 2160, 650),
    }
    boxes = build_slot_hit_boxes(rect, card_boxes)
    assert set(boxes.keys()) == {0, 1, 2, 3}
    # Verify strictly non-overlapping ordered X ranges covering all 4 centers
    for i in range(4):
        l, _t, r, _b = boxes[i]
        cx = (card_boxes[i][0] + card_boxes[i][2]) * 0.5
        assert l < cx < r
        if i > 0:
            assert boxes[i - 1][2] <= l + 1

    # Verify ClickTracker resolves slot 3 cleanly
    ct = ClickTracker()
    ct.reset(now_mono=10.0)
    for k in range(5):
        ct.record_cursor_sample(10.2 + k * 0.03, slot=3, pos=(2090, 700))
    chosen, method = ct.resolve_selection(close_ts=10.4, valid_slots={0, 1, 2, 3})
    assert chosen == 3 and method == "cursor_dwell"


def test_augment_overlays_render_4_cards_without_overlap_or_truncation() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
    from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay

    app = QApplication.instance() or QApplication([])
    _ = app

    # 1. Passive top badges overlay at compact 1920x1080@150% logical width (1280px)
    overlay = AugmentBadgesOverlay()
    items = []
    for idx, (aid, name, cx) in enumerate(
        (
            ("2132", "术士果汁盒", 355),
            ("3116", "将他们放倒倒倒", 758),
            ("1150", "面包和果酱", 1161),
            ("1211", "杀戮时间到了", 1565),
        )
    ):
        sc = AugmentScore(aid, name, "gold", "S", 0.54, 0.538, 0.025, 12000, False, percentile=88.0)
        items.append(
            AugmentBadgeRenderItem(
                slot_index=idx,
                score=sc,
                screen_box=(cx - 60, 480, cx + 60, 510),
                is_best=(idx == 2),
            )
        )
    overlay.render_cards(items, screen_rect=(0, 0, 1920, 1080))
    assert len(overlay._badges) == 4
    xs = sorted((w.x(), w.x() + w.width()) for w in overlay._badges)
    for i in range(3):
        assert xs[i][1] <= xs[i + 1][0], f"Badge {i} overlaps badge {i+1}: {xs}"
    overlay.close()

    # 2. Combo overlay picker mode renders all 4 cards with 4-slot labels
    combo = AugmentComboOverlay()
    prof = MatchBuildProfile("AP", "AP 法术爆发流", ("AP_SPELL",), (), (), ())
    verdict = PickerComboVerdict(
        profile=prof,
        cards=tuple(
            PickerCardComboAdvice(
                slot_index=i,
                augment_id=str(1000 + i),
                name=f"测试符文{i}",
                rarity="gold",
                verdict="synergy" if i == 3 else "neutral",
                verdict_label="组合顺滑" if i == 3 else "常规备选",
                combo_delta=4.0 if i == 3 else 0.0,
                reason="联动测试",
                combo_name="测试流派",
                gamble=False,
            )
            for i in range(4)
        ),
        best_slot_index=3,
        reroll_advice="",
    )
    combo.enter_picker_mode(verdict)
    assert len(combo._picker_rows) == 4
    combo.close()
