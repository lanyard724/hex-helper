"""Comprehensive unit & integration tests for HexHelper vision evolution:
1. Per-slot missing-card recovery, 2-card animation grace, and anti-false-reroll (AugmentPickerDetector)
2. Decisive-dwell freeze hysteresis and confidence scoring (ClickTracker)
3. ACE-safe 237-augment visual icon template matching & HUD verification (AugmentIconMatcher)
"""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hex_helper.config import NormalizedRect
from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher, IconMatchResult, is_valid_frame
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.click_tracker import ClickTracker
from hex_helper.vision.detector import AugmentPickerDetector
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import OcrLine, WinRtOcrEngine


def _make_catalog() -> list[AugmentInfo]:
    return [
        AugmentInfo("3058", "嗡嗡怪兽", "gold"),
        AugmentInfo("3081", "命运之武器", "gold"),
        AugmentInfo("3131", "屹立不倒", "gold"),
        AugmentInfo("2119", "过量延伸者", "gold"),
        AugmentInfo("1001", "泰坦的坚决", "prismatic"),
        AugmentInfo("1002", "尖端发明家", "gold"),
        AugmentInfo("1205", "物理转魔法", "silver"),
    ]


def _make_textured_frame(seed: int = 1, w: int = 2560, h: int = 1440) -> np.ndarray:
    """Create a textured non-uniform BGR frame that passes the quick-gate std & delta checks."""
    rng = np.random.default_rng(seed)
    frame = rng.integers(20, 180, size=(h, w, 3), dtype=np.uint8)
    # Paint gold header stripes across the card band so rarity hints are realistic
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


def _make_synthetic_icon(idx: int, size: int = 64) -> np.ndarray:
    """Generate 237+ visually distinct synthetic BGR augment icons (varied shapes + HSV palettes)."""
    # Spread hue, saturation, pattern type, rotation/symmetry, and secondary color deterministically
    h_primary = (idx * 37) % 180
    h_secondary = (h_primary + 55 + (idx % 3) * 25) % 180
    s_primary = 150 + (idx % 4) * 25
    v_primary = 175 + (idx % 3) * 25

    hsv_bg = np.zeros((size, size, 3), dtype=np.uint8)
    hsv_bg[:, :, 0] = (h_primary + 90) % 180
    hsv_bg[:, :, 1] = 45 + (idx % 5) * 18
    hsv_bg[:, :, 2] = 35 + (idx % 4) * 12
    img = cv2.cvtColor(hsv_bg, cv2.COLOR_HSV2BGR)

    c1_bgr = tuple(
        int(x) for x in cv2.cvtColor(np.uint8([[[h_primary, s_primary, v_primary]]]), cv2.COLOR_HSV2BGR)[0, 0]
    )
    c2_bgr = tuple(
        int(x) for x in cv2.cvtColor(np.uint8([[[h_secondary, 210, 235]]]), cv2.COLOR_HSV2BGR)[0, 0]
    )
    c3_bgr = (240, 240, 245) if (idx % 2 == 0) else (30, 35, 45)

    cx, cy = size // 2, size // 2
    shape_kind = idx % 8
    sub_kind = (idx // 8) % 6
    angle_deg = ((idx // 48) * 37 + (idx % 7) * 17) % 360
    rad = math.radians(angle_deg)

    # Primary geometric emblem
    if shape_kind == 0:
        cv2.circle(img, (cx, cy), size // 3, c1_bgr, -1)
        cv2.circle(img, (cx, cy), size // 6, c2_bgr, -1)
    elif shape_kind == 1:
        pts = np.array(
            [[cx, 8], [size - 8, cy], [cx, size - 8], [8, cy]], dtype=np.int32
        )
        cv2.fillPoly(img, [pts], c1_bgr)
        cv2.circle(img, (cx, cy), size // 6, c2_bgr, 3)
    elif shape_kind == 2:
        cv2.rectangle(img, (10, 14), (size - 10, size - 14), c1_bgr, -1)
        cv2.line(img, (10, 14), (size - 10, size - 14), c2_bgr, 4)
        cv2.line(img, (size - 10, 14), (10, size - 14), c3_bgr, 3)
    elif shape_kind == 3:
        pts = np.array([[cx, 7], [size - 9, size - 10], [9, size - 10]], dtype=np.int32)
        cv2.fillPoly(img, [pts], c1_bgr)
        cv2.circle(img, (cx, cy + 6), 8, c2_bgr, -1)
    elif shape_kind == 4:
        for k in range(3):
            y = 12 + k * 18
            cv2.rectangle(img, (8, y), (size - 8, y + 8), c1_bgr if k % 2 == 0 else c2_bgr, -1)
    elif shape_kind == 5:
        cv2.ellipse(img, (cx, cy), (size // 3, size // 5), angle_deg, 0, 360, c1_bgr, -1)
        cv2.ellipse(img, (cx, cy), (size // 5, size // 3), angle_deg, 0, 360, c2_bgr, 3)
    elif shape_kind == 6:
        pts = []
        for k in range(6):
            a = rad + k * math.pi / 3.0
            r = (size * 0.38) if (k % 2 == 0) else (size * 0.18)
            pts.append([int(cx + r * math.cos(a)), int(cy + r * math.sin(a))])
        cv2.fillPoly(img, [np.array(pts, dtype=np.int32)], c1_bgr)
        cv2.polylines(img, [np.array(pts, dtype=np.int32)], True, c2_bgr, 2)
    else:
        cv2.circle(img, (cx, cy), size // 3, c1_bgr, 5)
        dx = int(round(16 * math.cos(rad)))
        dy = int(round(16 * math.sin(rad)))
        cv2.line(img, (cx - dx, cy - dy), (cx + dx, cy + dy), c2_bgr, 5)
        cv2.circle(img, (cx + dx // 2, cy + dy // 2), 6, c3_bgr, -1)

    # Secondary asymmetric marker to differentiate all 237 icons clearly
    mx = int(cx + 15 * math.cos(rad + sub_kind * 0.9))
    my = int(cy + 15 * math.sin(rad + sub_kind * 0.9))
    if sub_kind % 3 == 0:
        cv2.circle(img, (mx, my), 6, c3_bgr, -1)
    elif sub_kind % 3 == 1:
        cv2.rectangle(img, (mx - 5, my - 5), (mx + 5, my + 5), c2_bgr, -1)
    else:
        cv2.line(img, (8, my), (size - 8, cy), c3_bgr, 2)

    return img


def _paste_icon_into_hud_slot(
    frame_bgr: np.ndarray,
    matcher: AugmentIconMatcher,
    slot_idx: int,
    icon_bgr: np.ndarray,
    *,
    border_bgr: tuple[int, int, int] | None = (60, 175, 225),
    noise_sigma: float = 4.0,
    brightness_shift: float = 0.0,
    seed: int = 123,
) -> None:
    """Paste an augment icon into HUD slot `slot_idx` with optional bezel, brightness shift, and noise."""
    fh, fw = frame_bgr.shape[:2]
    rect_map = dict(matcher.get_hud_slot_rects(fw, fh))
    x0, y0, x1, y1 = rect_map[slot_idx]
    slot_w, slot_h = x1 - x0, y1 - y0
    resized = cv2.resize(icon_bgr, (slot_w, slot_h), interpolation=cv2.INTER_AREA).astype(np.float32)
    if brightness_shift != 0.0:
        resized += brightness_shift
    if noise_sigma > 0.0:
        rng = np.random.default_rng(seed)
        resized += rng.normal(0.0, noise_sigma, size=resized.shape)
    patch = np.clip(resized, 0, 255).astype(np.uint8)
    if border_bgr is not None:
        cv2.rectangle(patch, (0, 0), (slot_w - 1, slot_h - 1), border_bgr, thickness=2)
    frame_bgr[y0:y1, x0:x1] = patch


# ---------------------------------------------------------------------------
# 1. Per-slot missing-card recovery & anti-false-reroll (AugmentPickerDetector)
# ---------------------------------------------------------------------------


def test_per_slot_missing_card_recovery_and_screen_box_mapping():
    catalog = _make_catalog()
    matcher = AugmentMatcher(catalog)
    card_roi = NormalizedRect(0.14, 0.24, 0.72, 0.48)
    title_roi = NormalizedRect(0.14, 0.31, 0.72, 0.25)
    game_rect = WindowRect(left=100, top=50, width=2560, height=1440, hwnd=1)
    tl, tt, tr, tb = title_roi.to_pixels(2560, 1440)
    band_w = tr - tl
    band_h = tb - tt

    slot_titles = {
        0: ("嗡嗡怪兽", "3058", int(band_w * 0.14), 140),
        1: ("命运之武器", "3081", int(band_w * 0.46), 140),
        2: ("屹立不倒", "3131", int(band_w * 0.78), 140),
    }

    for missing_slot in (0, 1, 2):
        r0, r1 = AugmentPickerDetector._SLOT_SLICE_RATIOS[missing_slot]
        sx_start = int(band_w * r0)
        sx_end = min(band_w, int(round(band_w * r1)))
        expected_retry_w = (sx_end - sx_start) * 2
        expected_retry_h = band_h * 2
        calls: list[tuple[int, int]] = []

        class _MissOneSlotOcr:
            def recognize_lines(self, crop: np.ndarray) -> list[OcrLine]:
                ch, cw = crop.shape[:2]
                calls.append((cw, ch))
                if cw == band_w:
                    # Full-band pass misses `missing_slot`
                    return [
                        OcrLine(name, x=float(lx), y=float(ly), w=160.0, h=40.0)
                        for s, (name, _aid, lx, ly) in slot_titles.items()
                        if s != missing_slot
                    ]
                # Targeted 2x enhanced retry recovers the missing slot (and includes a neighbor bleed line to test filtering!)
                name, _aid, lx, ly = slot_titles[missing_slot]
                rel_x_2x = (lx - sx_start) * 2.0
                rel_y_2x = ly * 2.0
                out = [OcrLine(name, x=rel_x_2x, y=rel_y_2x, w=320.0, h=80.0)]
                # Also inject an adjacent slot's line bleeding across the 4% overlap margin
                neighbor = 1 if missing_slot != 1 else 0
                n_name, _, n_lx, n_ly = slot_titles[neighbor]
                out.append(OcrLine(n_name, x=(n_lx - sx_start) * 2.0, y=n_ly * 2.0, w=320.0, h=80.0))
                return out

        detector = AugmentPickerDetector(_MissOneSlotOcr(), matcher, card_roi, title_roi)
        frame = _make_textured_frame(seed=10 + missing_slot)
        res = detector.process_frame(frame, game_rect, force=True)

        assert res.picker_visible is True
        assert res.stable is True
        assert len(res.cards) == 3
        assert [c.slot_index for c in res.cards] == [0, 1, 2]
        assert [c.augment.augment_id for c in res.cards] == ["3058", "3081", "3131"]

        # Verify retry ran ONCE on exact 2x upscaled sub-crop of ONLY the missing slot
        assert len(calls) == 2
        assert calls[0] == (band_w, band_h)
        assert calls[1] == (expected_retry_w, expected_retry_h)

        # Verify recovered screen_box maps accurately back to full screen coords
        recovered = res.cards[missing_slot]
        _, _, lx, ly = slot_titles[missing_slot]
        exp_box = (
            game_rect.left + tl + lx,
            game_rect.top + tt + ly,
            game_rect.left + tl + lx + 160,
            game_rect.top + tt + ly + 40,
        )
        assert recovered.screen_box == exp_box
        assert detector.recovered_slot_count == 1
        assert detector.get_match_metrics() == {
            "partial_open_count": 0,
            "recovered_slot_count": 1,
            "false_reroll_suppressed_count": 0,
        }
        detector.reset_match_metrics()
        assert detector.get_match_metrics()["recovered_slot_count"] == 0


def test_per_slot_recovery_real_winrt_ocr_on_enhanced_subcrop():
    """Verify real WinRT OCR reads the 2x LAB-CLAHE enhanced sub-crop when full-band pass misses slot 2."""
    W, H = 2560, 1440
    img = Image.new("RGB", (W, H), (14, 20, 32))
    draw = ImageDraw.Draw(img)
    title_font = _find_cn_font(38)
    for cx, title, fill in (
        (780, "嗡嗡怪兽", (245, 238, 220)),
        (1280, "命运之武器", (245, 238, 220)),
        (1780, "屹立不倒", (125, 128, 138)),  # dimmer low-contrast 3rd card
    ):
        draw.rectangle([cx - 205, 380, cx + 205, 940], fill=(22, 30, 46), outline=(210, 165, 65), width=8)
        draw.rectangle([cx - 193, 392, cx + 193, 432], fill=(210, 165, 65))
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
        draw.text((cx - tw // 2, 585), title, fill=fill, font=title_font)
    frame = cv2.cvtColor(np.asarray(img, dtype=np.uint8), cv2.COLOR_RGB2BGR)

    real_ocr = WinRtOcrEngine("zh-Hans-CN")
    title_roi = NormalizedRect(0.14, 0.31, 0.72, 0.25)
    tl, _, tr, _ = title_roi.to_pixels(W, H)
    full_band_w = tr - tl

    class _DropSlot2OnFullBandProxy:
        def recognize_lines(self, crop: np.ndarray) -> list[OcrLine]:
            lines = real_ocr.recognize_lines(crop)
            if crop.shape[1] == full_band_w:
                # Simulate real-world full-band miss of slot 2 while letting real WinRT OCR handle retry
                return [ln for ln in lines if (ln.cx / full_band_w) < 0.67]
            return lines

    try:
        detector = AugmentPickerDetector(
            ocr_engine=_DropSlot2OnFullBandProxy(),
            matcher=AugmentMatcher(_make_catalog()),
            card_band_roi=NormalizedRect(0.14, 0.24, 0.72, 0.48),
            title_band_roi=title_roi,
        )
        res = detector.process_frame(frame, WindowRect(0, 0, W, H, 0), force=True)
        assert res.picker_visible is True
        assert res.stable is True
        assert [c.augment.augment_id for c in res.cards] == ["3058", "3081", "3131"]
        assert detector.recovered_slot_count == 1
        # Slot 2 screen box must land around cx=1780
        s2_box = res.cards[2].screen_box
        s2_cx = (s2_box[0] + s2_box[2]) * 0.5
        assert 1680 <= s2_cx <= 1880
    finally:
        real_ocr.close()


def test_partial_two_card_fade_in_grace_and_anti_false_reroll():
    catalog = _make_catalog()
    matcher = AugmentMatcher(catalog)
    card_roi = NormalizedRect(0.14, 0.24, 0.72, 0.48)
    title_roi = NormalizedRect(0.14, 0.31, 0.72, 0.25)
    game_rect = WindowRect(0, 0, 2560, 1440, 0)
    tl, _, tr, _ = title_roi.to_pixels(2560, 1440)
    band_w = tr - tl

    line_a = OcrLine("嗡嗡怪兽", x=band_w * 0.12, y=140.0, w=160.0, h=40.0)
    line_b = OcrLine("命运之武器", x=band_w * 0.45, y=140.0, w=160.0, h=40.0)
    line_c = OcrLine("屹立不倒", x=band_w * 0.78, y=140.0, w=160.0, h=40.0)
    line_d = OcrLine("过量延伸者", x=band_w * 0.12, y=140.0, w=160.0, h=40.0)

    class _ScriptedOcr:
        def __init__(self, script: list[list[OcrLine]]) -> None:
            self.script = list(script)
            self.idx = 0

        def recognize_lines(self, crop: np.ndarray) -> list[OcrLine]:
            if crop.shape[1] != band_w:
                # Per-slot retry finds nothing during fade-in / dropout simulation
                return []
            out = self.script[min(self.idx, len(self.script) - 1)]
            self.idx += 1
            return out

    # Scenario 1: 2-card fade-in expands to 3 cards on frame 3 -> ZERO premature 2-card open, ZERO fake reroll!
    ocr_fade = _ScriptedOcr([
        [line_a, line_b],          # f1: 2 cards (stable_count=1 < 3)
        [line_a, line_b],          # f2: 2 cards (stable_count=2 < 3 -> STILL waiting!)
        [line_a, line_b, line_c],  # f3: 3rd card arrives (stable_count=1 < 2)
        [line_a, line_b, line_c],  # f4: 3 cards stable (stable_count=2 >= 2 -> clean 3-card open!)
    ])
    det = AugmentPickerDetector(ocr_fade, matcher, card_roi, title_roi)
    r1 = det.process_frame(_make_textured_frame(seed=101), game_rect)
    r2 = det.process_frame(_make_textured_frame(seed=102), game_rect)
    r3 = det.process_frame(_make_textured_frame(seed=103), game_rect)
    r4 = det.process_frame(_make_textured_frame(seed=104), game_rect)
    assert r1.stable is False and r1.rerolled is False
    assert r2.stable is False and r2.rerolled is False
    assert r3.stable is False and r3.rerolled is False
    assert r4.stable is True and r4.rerolled is False
    assert [c.augment.augment_id for c in r4.cards] == ["3058", "3081", "3131"]
    assert det.partial_open_count == 0

    # Scenario 2: Anti-false-reroll on locked 3-card picker (identical cards, 1-frame dropout grace, sustained dropout, real reroll)
    ocr_rr = _ScriptedOcr([
        [line_a, line_b, line_c],  # f1: pending 3
        [line_a, line_b, line_c],  # f2: lock (A, B, C)
        [line_a, line_b, line_c],  # f3: identical cards -> rerolled MUST be False
        [line_a, line_b],          # f4: momentary 1-frame strict-subset dropout -> 1 grace frame holds (A, B, C)!
        [line_a, line_b, line_c],  # f5: slot 2 returns -> rerolled MUST be False!
        [line_a, line_b],          # f6: dropout grace frame 1
        [line_a, line_b],          # f7: dropout frame 2 (past grace, pending (A,B))
        [line_a, line_b],          # f8: dropout frame 3 (stabilizes on strict subset, STILL rerolled=False!)
        [line_a, line_b, line_c],  # f9: slot 2 fills in with genuinely new card C (relative to (A,B))
        [line_a, line_b, line_c],  # f10: stabilizes -> rerolled=True
        [line_d, line_b, line_c],  # f11: real reroll on slot 0 (A -> D)
        [line_d, line_b, line_c],  # f12: stabilizes -> rerolled=True!
    ])
    det2 = AugmentPickerDetector(ocr_rr, matcher, card_roi, title_roi)
    results = [det2.process_frame(_make_textured_frame(seed=200 + i), game_rect) for i in range(12)]

    # Locked on f2
    assert results[1].stable is True and results[1].rerolled is False
    # Identical cards on f3 -> never reroll
    assert results[2].stable is True and results[2].rerolled is False
    # Momentary 1-frame strict-subset dropout on f4 -> keeps locked 3 cards for 1 grace frame!
    assert results[3].rerolled is False
    assert results[3].stable is True
    assert [c.augment.augment_id for c in results[3].cards] == ["3058", "3081", "3131"]
    assert det2.false_reroll_suppressed_count >= 1
    # Slot 2 returns on f5 -> zero false reroll
    assert results[4].rerolled is False
    assert [c.augment.augment_id for c in results[4].cards] == ["3058", "3081", "3131"]
    # Sustained strict-subset dropout f6..f8 -> NEVER emits rerolled=True
    assert results[5].rerolled is False
    assert results[6].rerolled is False
    assert results[7].rerolled is False
    # Previously missing slot filling in with C (f9->f10) -> rerolled=True
    assert results[9].stable is True and results[9].rerolled is True
    assert [c.augment.augment_id for c in results[9].cards] == ["3058", "3081", "3131"]
    # Real slot-0 reroll A->D (f11->f12) -> rerolled=True
    assert results[10].rerolled is False
    assert results[11].stable is True and results[11].rerolled is True
    assert [c.augment.augment_id for c in results[11].cards] == ["2119", "3081", "3131"]

    # Scenario 3: True 2-card partial open (3 consecutive frames) locks on frame 3 & increments partial_open_count
    ocr_part = _ScriptedOcr([
        [line_a, line_b],
        [line_a, line_b],
        [line_a, line_b],
    ])
    det3 = AugmentPickerDetector(ocr_part, matcher, card_roi, title_roi)
    p1 = det3.process_frame(_make_textured_frame(seed=301), game_rect)
    p2 = det3.process_frame(_make_textured_frame(seed=302), game_rect)
    p3 = det3.process_frame(_make_textured_frame(seed=303), game_rect)
    assert p1.stable is False
    assert p2.stable is False
    assert p3.stable is True and p3.rerolled is False
    assert len(p3.cards) == 2
    assert det3.partial_open_count == 1

    # Scenario 4: Same card set in swapped slots must NEVER emit rerolled=True
    line_a_mid = OcrLine("嗡嗡怪兽", x=band_w * 0.45, y=140.0, w=160.0, h=40.0)
    line_b_left = OcrLine("命运之武器", x=band_w * 0.12, y=140.0, w=160.0, h=40.0)
    ocr_swap = _ScriptedOcr([
        [line_a, line_b, line_c],
        [line_a, line_b, line_c],
        [line_b_left, line_a_mid, line_c],
        [line_b_left, line_a_mid, line_c],
    ])
    det4 = AugmentPickerDetector(ocr_swap, matcher, card_roi, title_roi)
    s_res = [det4.process_frame(_make_textured_frame(seed=400 + i), game_rect) for i in range(4)]
    assert s_res[1].stable is True
    assert s_res[3].stable is True and s_res[3].rerolled is False
    assert det4.false_reroll_suppressed_count == 1


# ---------------------------------------------------------------------------
# 2. Decisive-dwell freeze hysteresis & confidence (ClickTracker)
# ---------------------------------------------------------------------------


def test_click_tracker_post_click_swipe_over_hysteresis_and_confidence():
    # A) Direct click returns confidence 1.0
    t_click = ClickTracker()
    t_click.reset(now_mono=10.0)
    t_click.record_cursor_sample(10.50, slot=1, clicked=True, pos=(1280, 620))
    slot, src = t_click.resolve_selection(close_ts=10.60, valid_slots={0, 1, 2})
    assert (slot, src) == (1, "click")
    assert t_click.last_resolve_confidence == 1.0

    # B) Empty history returns None + confidence 0.0
    t_empty = ClickTracker()
    t_empty.reset(now_mono=20.0)
    assert t_empty.resolve_selection(close_ts=20.5, valid_slots={0, 1, 2}) == (None, "")
    assert t_empty.last_resolve_confidence == 0.0

    # C) Post-click swipe-over inside tight window:
    # Player pauses decisively on slot 1 (4 slow samples <= 220 px/s), clicks (blocked by ACE),
    # then immediately whips mouse right across slot 2 (> 480 px/s) in [close_ts - 0.14, close_ts + 0.08]
    t_swipe = ClickTracker()
    t_swipe.reset(now_mono=100.0)
    for k in range(4):
        t_swipe.record_cursor_sample(101.20 + k * 0.03, slot=1, clicked=False, pos=(1280 + k * 2, 620))
    # Rapid swipe across slot 2 right before picker vanishes at 101.50
    t_swipe.record_cursor_sample(101.41, slot=2, clicked=False, pos=(1680, 610))
    t_swipe.record_cursor_sample(101.44, slot=2, clicked=False, pos=(1960, 590))

    slot_c, src_c = t_swipe.resolve_selection(close_ts=101.50, valid_slots={0, 1, 2})
    assert slot_c == 1 and src_c == "cursor_dwell"
    assert 0.85 <= t_swipe.last_resolve_confidence <= 1.0

    # D) Delayed vanish detection: decisive pause on slot 1 ends at close_ts - 0.48 (outside Tier 2a 0.36s),
    # while 2 fast fly-through samples hit slot 2 at close_ts - 0.09 and close_ts - 0.06 -> MUST still pick slot 1!
    t_delayed = ClickTracker()
    t_delayed.reset(now_mono=200.0)
    for k in range(4):
        t_delayed.record_cursor_sample(201.00 + k * 0.03, slot=1, clicked=False, pos=(1280, 620 + k))
    # Fast fly-through across slot 2 (~1000+ px/s) in final 140ms
    t_delayed.record_cursor_sample(201.45, slot=1, clicked=False, pos=(1480, 620))
    t_delayed.record_cursor_sample(201.48, slot=2, clicked=False, pos=(1760, 620))
    t_delayed.record_cursor_sample(201.51, slot=2, clicked=False, pos=(2040, 620))

    slot_d, src_d = t_delayed.resolve_selection(close_ts=201.57, valid_slots={0, 1, 2})
    assert slot_d == 1 and src_d == "cursor_dwell"
    assert 0.70 <= t_delayed.last_resolve_confidence <= 1.0

    # E) Post-reroll click on rerolled slot 1 + immediate fast swipe over slot 2 -> MUST keep slot 1!
    t_rr = ClickTracker()
    t_rr.reset(now_mono=300.0)
    t_rr.mark_reroll(slot=1, now_mono=300.5)
    for k in range(5):
        t_rr.record_cursor_sample(300.80 + k * 0.03, slot=1, clicked=False, pos=(1280, 630 + k))
    # Fast whip right over slot 2 after clicking slot 1
    t_rr.record_cursor_sample(301.00, slot=2, clicked=False, pos=(1750, 620))
    t_rr.record_cursor_sample(301.03, slot=2, clicked=False, pos=(2020, 610))

    slot_e, src_e = t_rr.resolve_selection(close_ts=301.06, valid_slots={0, 1, 2}, rerolled_slot=1, reroll_ts=300.5)
    assert slot_e == 1 and src_e == "cursor_dwell"

    # F) Ambiguous split dwell yields lower confidence than decisive single-slot pause
    t_amb = ClickTracker()
    t_amb.reset(now_mono=400.0)
    for k in range(3):
        t_amb.record_cursor_sample(400.60 + k * 0.03, slot=0, clicked=False, pos=(760, 620))
    for k in range(3):
        t_amb.record_cursor_sample(400.72 + k * 0.03, slot=1, clicked=False, pos=(1280, 620))
    slot_f, src_f = t_amb.resolve_selection(close_ts=400.84, valid_slots={0, 1, 2})
    assert slot_f in (0, 1) and src_f == "cursor_dwell"
    assert 0.0 < t_amb.last_resolve_confidence < t_swipe.last_resolve_confidence


# ---------------------------------------------------------------------------
# 3. ACE-Safe 237-Augment Visual Icon Template Matcher (AugmentIconMatcher)
# ---------------------------------------------------------------------------


def test_icon_matcher_black_frame_rejection_and_hud_extraction():
    matcher = AugmentIconMatcher()
    assert is_valid_frame(None) is False
    assert is_valid_frame(np.empty((0, 0, 3), dtype=np.uint8)) is False
    assert is_valid_frame(np.zeros((1080, 1920, 3), dtype=np.uint8)) is False
    # Ultra-dark frame (mean luma <= 2.0) must be rejected
    almost_black = np.ones((1080, 1920, 3), dtype=np.uint8)
    assert matcher.is_valid_frame(almost_black) is False

    # Valid normal game frame
    valid_1080p = np.full((1080, 1920, 3), 28, dtype=np.uint8)
    assert matcher.is_valid_frame(valid_1080p) is True

    rect = WindowRect(0, 0, 1920, 1080, 0)
    assert matcher.extract_hud_augment_patches(np.zeros((1080, 1920, 3), dtype=np.uint8), rect) == []
    patches = matcher.extract_hud_augment_patches(valid_1080p, rect)
    assert len(patches) == 4
    assert [idx for idx, _ in patches] == [0, 1, 2, 3]
    for _idx, p in patches:
        assert p.shape[0] == p.shape[1]  # strictly square after edge trimming
        assert p.shape[0] >= 20

    # Also verify full-desktop capture containing a smaller game_rect window
    desktop = np.full((1440, 2560, 3), 30, dtype=np.uint8)
    sub_rect = WindowRect(left=320, top=180, width=1920, height=1080, hwnd=1)
    patches_sub = matcher.extract_hud_augment_patches(desktop, sub_rect)
    assert len(patches_sub) == 4


def test_icon_matcher_three_card_discrimination_and_margin():
    matcher = AugmentIconMatcher()
    icon_a = _make_synthetic_icon(3)
    icon_b = _make_synthetic_icon(19)
    icon_c = _make_synthetic_icon(47)
    assert matcher.register_template("1001", icon_a, name="泰坦的坚决") is True
    assert matcher.register_template("1002", icon_b, name="尖端发明家") is True
    assert matcher.register_template("1004", icon_c, name="回归基本功") is True

    # Empty dark HUD rejects cleanly (never false-matches blank slots)
    frame = np.full((1440, 2560, 3), 18, dtype=np.uint8)
    rect = WindowRect(0, 0, 2560, 1440, 0)
    assert matcher.verify_picked_augment(frame, rect, candidate_ids=["1001", "1002", "1004"]) is None

    # Paste chosen augment "1002" into HUD slot 0 with gold bezel, brightness offset, and noise
    _paste_icon_into_hud_slot(frame, matcher, slot_idx=0, icon_bgr=icon_b, brightness_shift=8.0, noise_sigma=5.0)

    res = matcher.verify_picked_augment(frame, rect, candidate_ids=["1001", "1002", "1004"])
    assert isinstance(res, IconMatchResult)
    assert res.augment_id == "1002"
    assert res.hud_slot_index == 0
    assert res.score >= 0.72
    assert res.margin >= 0.20
    assert res.name == "尖端发明家"

    # Direct match_patch with candidate subset strictly excludes unregistered/non-candidate winners
    direct = matcher.match_patch(icon_c, candidate_ids=["1001", "1002", "1004"])
    assert direct is not None and direct.augment_id == "1004" and direct.score >= 0.98 and direct.margin >= 0.30

    # Black frame always returns None
    black = np.zeros((1440, 2560, 3), dtype=np.uint8)
    assert matcher.verify_picked_augment(black, rect, candidate_ids=["1001", "1002", "1004"]) is None


def test_icon_matcher_full_237_bank_transmute_matching():
    """Verify full 237-template bank Transmute matching (`candidate_ids=None`) with slot_index_hint."""
    matcher = AugmentIconMatcher()
    icons: dict[str, np.ndarray] = {}
    for i in range(237):
        aid = str(1000 + i)
        img = _make_synthetic_icon(i, size=64)
        icons[aid] = img
        assert matcher.register_template(aid, img, name=f"Augment_{aid}") is True
    assert matcher.template_count == 237

    frame = np.full((1440, 2560, 3), 22, dtype=np.uint8)
    rect = WindowRect(0, 0, 2560, 1440, 0)
    # HUD slot 0 already holds an older owned augment; slot 1 receives the newly Transmute-rolled augment
    _paste_icon_into_hud_slot(frame, matcher, slot_idx=0, icon_bgr=icons["1005"], noise_sigma=3.0, seed=1)
    _paste_icon_into_hud_slot(frame, matcher, slot_idx=1, icon_bgr=icons["1177"], noise_sigma=4.0, seed=2)

    t0 = time.perf_counter()
    res = matcher.verify_picked_augment(frame, rect, candidate_ids=None, slot_index_hint=1)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert res is not None
    assert res.augment_id == "1177"
    assert res.hud_slot_index == 1
    assert res.score >= 0.72
    assert res.margin > 0.02
    assert elapsed_ms < 250.0  # pure in-memory 28x28 template bank stays fast


def test_icon_matcher_real_image_cache_cdragon_and_offline_resilience(tmp_path: Path):
    """Test real PNG icon loading, cdragon_augments.json parsing, lazy fetcher, and offline safety."""
    static_dir = tmp_path / "static"
    icons_dir = tmp_path / "icons" / "augment"
    static_dir.mkdir(parents=True)
    icons_dir.mkdir(parents=True)

    # Copy real cdragon_augments.json if available in workspace, else create realistic sample
    repo_root = Path(__file__).resolve().parents[1]
    bundled_cd = repo_root / "build" / "bundled_seed" / "static" / "cdragon_augments.json"
    if bundled_cd.exists():
        shutil.copy2(bundled_cd, static_dir / "cdragon_augments.json")
    else:
        sample_cd = [
            {
                "id": 1001,
                "nameTRA": "泰坦的坚决",
                "augmentSmallIconPath": "/lol-game-data/assets/ASSETS/UX/Kiwi/Augments/Icons/IAmTheJuggernaut_small.png",
            },
            {
                "id": 1002,
                "nameTRA": "尖端发明家",
                "augmentSmallIconPath": "/lol-game-data/assets/ASSETS/UX/Cherry/Augments/Icons/ApexInventor_small.png",
            },
            {
                "id": 1004,
                "nameTRA": "回归基本功",
                "augmentSmallIconPath": "/lol-game-data/assets/ASSETS/UX/Cherry/Augments/Icons/BackToBasics_small.png",
            },
        ]
        (static_dir / "cdragon_augments.json").write_text(json.dumps(sample_cd, ensure_ascii=False), encoding="utf-8")

    # Gather real PNG images (prefer real local LoL cache icons if present, always include repo hex_helper.png)
    real_pngs: list[Path] = []
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        champ_dir = Path(local_app) / "HexHelper" / "cache" / "icons" / "champion"
        if champ_dir.exists():
            real_pngs.extend(sorted(champ_dir.glob("*.png"))[:3])
    asset_png = repo_root / "src" / "hex_helper" / "assets" / "hex_helper.png"
    if len(real_pngs) < 3 and asset_png.exists():
        real_pngs.append(asset_png)

    # Populate cached icons for 1001 and 1002; leave 1004 for lazy download test
    if len(real_pngs) >= 2:
        shutil.copy2(real_pngs[0], icons_dir / "1001.png")
        shutil.copy2(real_pngs[1], icons_dir / "1002.png")
    else:
        cv2.imwrite(str(icons_dir / "1001.png"), _make_synthetic_icon(11))
        cv2.imwrite(str(icons_dir / "1002.png"), _make_synthetic_icon(22))

    # Create a real BGRA PNG payload for lazy-download icon 1004 (tests alpha compositing + atomic cache write)
    icon_1004_bgr = cv2.imread(str(real_pngs[2])) if len(real_pngs) >= 3 else _make_synthetic_icon(33)
    h, w = icon_1004_bgr.shape[:2]
    alpha = np.full((h, w, 1), 255, dtype=np.uint8)
    alpha[:4, :, :] = 0  # transparent edge strip like real CDragon icons
    icon_1004_bgra = np.concatenate([icon_1004_bgr, alpha], axis=2)
    ok_enc, enc_buf = cv2.imencode(".png", icon_1004_bgra)
    assert ok_enc
    lazy_png_bytes = enc_buf.tobytes()

    fetched_urls: list[str] = []

    def _mock_fetcher(url: str) -> bytes:
        fetched_urls.append(url)
        if "backtobasics" in url.lower():
            return lazy_png_bytes
        raise ConnectionError("offline simulated failure")

    matcher = AugmentIconMatcher(cache_dir=tmp_path, allow_download=True, fetcher=_mock_fetcher)
    assert matcher.known_url_count >= 3
    assert matcher.has_template("1001") is True
    assert matcher.has_template("1002") is True
    assert matcher.has_template("1004") is False

    # Embed real PNG icon 1004 into HUD slot 2 -> triggers lazy download, caches to disk, and matches!
    frame = np.full((1080, 1920, 3), 24, dtype=np.uint8)
    rect = WindowRect(0, 0, 1920, 1080, 0)
    _paste_icon_into_hud_slot(frame, matcher, slot_idx=2, icon_bgr=icon_1004_bgr, border_bgr=None, noise_sigma=2.0)

    res = matcher.verify_picked_augment(frame, rect, candidate_ids=["1001", "1002", "1004"])
    assert res is not None
    assert res.augment_id == "1004"
    assert res.hud_slot_index == 2
    assert res.score >= 0.72
    assert (icons_dir / "1004.png").exists()
    assert len(fetched_urls) == 1

    # Offline resilience: requesting an uncached icon whose fetch raises MUST NOT crash
    assert matcher.ensure_template("1205") is False
    # Non-existent cache_dir must also initialize cleanly without crashing
    offline_matcher = AugmentIconMatcher(cache_dir=tmp_path / "does_not_exist", allow_download=False)
    assert offline_matcher.template_count == 0
    assert offline_matcher.match_patch(icon_1004_bgr) is None
