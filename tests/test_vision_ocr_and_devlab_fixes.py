"""Regression tests for Phase 1 Worker C: skip_clahe OCR, 4-PNG recall, 2-frame flicker grace, HUD blind diag, and DevLab window_stats."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from hex_helper.config import NormalizedRect
from hex_helper.data.provider import AugmentInfo
from hex_helper.devlab.analyzer import DevLabAnalyzer, EV_VISION_WINDOW_STATS
from hex_helper.devlab.recorder import DevLabFlightRecorder
from hex_helper.devlab.schema import (
    DOMAIN_GAME,
    DOMAIN_VISION,
    EV_MATCH_EOG,
    EV_VISION_LATENCY_SPIKE,
    hash_match_key,
)
from hex_helper.vision.augment_icon_matcher import (
    AugmentIconMatcher,
    build_hud_wide_diag_crop,
)
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.detector import AugmentPickerDetector
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import OcrLine, WinRtOcrEngine

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_AUGMENTS_JSON = REPO_ROOT / "build" / "bundled_seed" / "datasets" / "16.23" / "normal" / "augments.json"

REAL_FIELD_PNG_CASES: tuple[tuple[str, str, int, str], ...] = (
    (
        "1087",
        "台风",
        1,
        r"C:\Users\Administrator\AppData\Local\HexHelper\devlab\matches\m_00023_20261127_162933_cs\roi_snapshots\ev0238_title_crop_slot_1.png",
    ),
    (
        "2104",
        "灵魄炸弹",
        1,
        r"C:\Users\Administrator\AppData\Local\HexHelper\devlab\matches\m_00021_20261127_154634_cs\roi_snapshots\ev0150_title_crop_slot_1.png",
    ),
    (
        "2098",
        "吃过路兵",
        0,
        r"C:\Users\Administrator\AppData\Local\HexHelper\devlab\matches\m_00024_20261127_164813_cs\roi_snapshots\ev0221_title_crop_slot_0.png",
    ),
    (
        "1311",
        "溢流",
        0,
        r"C:\Users\Administrator\AppData\Local\HexHelper\devlab\matches\m_00022_20261127_160950_cs\roi_snapshots\ev0117_title_crop_slot_0.png",
    ),
)


def _load_real_catalog() -> list[AugmentInfo]:
    if REAL_AUGMENTS_JSON.exists():
        raw = json.loads(REAL_AUGMENTS_JSON.read_text(encoding="utf-8"))
        return [
            AugmentInfo(
                augment_id=str(aid),
                name=str(entry.get("name") or ""),
                rarity=str(entry.get("rarity") or "silver"),
            )
            for aid, entry in raw.items()
            if isinstance(entry, dict) and entry.get("name")
        ]
    return [
        AugmentInfo("1087", "台风", "silver"),
        AugmentInfo("2104", "灵魄炸弹", "prismatic"),
        AugmentInfo("2098", "吃过路兵", "prismatic"),
        AugmentInfo("1311", "溢流", "gold"),
        AugmentInfo("1415", "双生火焰", "silver"),
        AugmentInfo("3262", "史莱姆时间", "silver"),
        AugmentInfo("1379", "升级：花晓之剑", "prismatic"),
        AugmentInfo("1323", "残忍", "prismatic"),
        AugmentInfo("1332", "不祥契约", "prismatic"),
        AugmentInfo("1329", "史上最大雪球", "prismatic"),
        AugmentInfo("2132", "术士果汁盒", "gold"),
        AugmentInfo("1072", "炽烈黎明", "gold"),
    ]


def _make_textured_frame(seed: int, w: int = 2560, h: int = 1440) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(25, 220, size=(h, w, 3), dtype=np.uint8)


def _make_distinct_icon(seed: int, size: int = 64) -> np.ndarray:
    rng = np.random.default_rng(seed + 100)
    img = np.full((size, size, 3), 24, dtype=np.uint8)
    c1 = tuple(int(v) for v in rng.integers(90, 255, size=3))
    c2 = tuple(int(v) for v in rng.integers(80, 240, size=3))
    mode = seed % 3
    if mode == 0:
        cv2.circle(img, (size // 2, size // 2), size // 3, c1, -1)
        cv2.line(img, (4, 4), (size - 5, size - 5), c2, 4)
    elif mode == 1:
        cv2.rectangle(img, (10, 10), (size - 10, size - 10), c1, 4)
        cv2.circle(img, (size // 2, size // 2), size // 6, c2, -1)
    else:
        pts = np.array([[size // 2, 8], [size - 10, size - 12], [10, size - 12]], dtype=np.int32)
        cv2.fillPoly(img, [pts], c1)
        cv2.circle(img, (size // 2, size // 2 + 4), 7, c2, -1)
    return img


# ---------------------------------------------------------------------------
# 1. WinRtOcrEngine skip_clahe & scale cap
# ---------------------------------------------------------------------------


def test_ocr_winrt_skip_clahe_skips_clahe_and_limits_scale() -> None:
    ocr = WinRtOcrEngine("zh-Hans-CN")
    try:
        # 2x-enhanced slot crop (150x1676): skip_clahe=False blows up to 2600px, skip_clahe=True keeps scale=1.0
        enh_crop = np.full((150, 1676, 3), 40, dtype=np.uint8)
        enh_crop[50:95, 720:880] = (230, 225, 215)

        sc_clahe, v1_c, _ = ocr._prepare_variants(enh_crop, need_inverted=True, skip_clahe=False)
        assert sc_clahe > 1.2
        assert max(v1_c.shape[:2]) >= 2500

        sc_clean, v1_clean, v2_clean = ocr._prepare_variants(enh_crop, need_inverted=True, skip_clahe=True)
        assert sc_clean == 1.0
        assert v1_clean.shape == (150, 1676, 4)
        assert v2_clean is not None and v2_clean.shape == (150, 1676, 4)

        expected_gray = cv2.cvtColor(enh_crop, cv2.COLOR_BGR2GRAY)
        assert np.array_equal(v1_clean[:, :, 0], expected_gray)
        assert np.array_equal(v2_clean[:, :, 0], 255 - expected_gray)

        # Small crop (40x200) under skip_clahe=True gets bounded mild scale (<= 1.5x, never 2600px+)
        tiny_crop = np.full((40, 200, 3), 25, dtype=np.uint8)
        tiny_crop[10:30, 60:140] = (220, 220, 220)
        sc_tiny, v1_tiny, v2_tiny = ocr._prepare_variants(tiny_crop, need_inverted=True, skip_clahe=True)
        assert 1.0 <= sc_tiny <= 1.5
        assert v1_tiny.shape[:2] == (60, 300)
        assert v2_tiny is not None and v2_tiny.shape[:2] == (60, 300)

        # Real OCR smoke with skip_clahe=True on crisp white-on-dark banner
        lines = ocr.recognize_lines(enh_crop, dual_pass=True, force_dual_pass=True, skip_clahe=True)
        assert isinstance(lines, list)
    finally:
        ocr.close()


# ---------------------------------------------------------------------------
# 2. Detector double-CLAHE fix, zero hot-path overhead, and 4 real PNG recall
# ---------------------------------------------------------------------------


def test_detector_recovery_passes_skip_clahe_and_keeps_hotpath_zero_overhead() -> None:
    catalog = _load_real_catalog()
    matcher = AugmentMatcher(catalog)
    card_roi = NormalizedRect(0.08, 0.24, 0.84, 0.48)
    title_roi = NormalizedRect(0.08, 0.385, 0.84, 0.052)
    game_rect = WindowRect(left=0, top=0, width=2560, height=1440, hwnd=1)
    tl, _, tr, _ = title_roi.to_pixels(2560, 1440)
    band_w = tr - tl

    calls: list[dict[str, Any]] = []

    class _SpyOcr:
        def __init__(self, drop_slot2: bool) -> None:
            self.drop_slot2 = drop_slot2

        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
            skip_clahe: bool = False,
        ) -> list[OcrLine]:
            calls.append(
                {
                    "shape": crop.shape,
                    "dual_pass": dual_pass,
                    "force_dual_pass": force_dual_pass,
                    "skip_clahe": skip_clahe,
                }
            )
            if crop.shape[1] == band_w:
                lines = [
                    OcrLine("双生火焰", x=530.0, y=24.0, w=110.0, h=28.0),
                    OcrLine("史莱姆时间", x=1010.0, y=24.0, w=130.0, h=28.0),
                ]
                if not self.drop_slot2:
                    lines.append(OcrLine("台风", x=1530.0, y=24.0, w=60.0, h=28.0))
                return lines
            # Recovery sub-crop is 2x upscaled; primary recovery succeeds immediately
            return [OcrLine("台风", x=300.0, y=40.0, w=120.0, h=56.0)]

    # Hot path: all 3 slots match on Pass 1 -> exactly 1 OCR call, skip_clahe=False, zero recovery/fallback
    det_hot = AugmentPickerDetector(_SpyOcr(drop_slot2=False), matcher, card_roi, title_roi)  # type: ignore[arg-type]
    res_hot = det_hot.process_frame(_make_textured_frame(seed=11), game_rect, force=True)
    assert len(res_hot.cards) == 3
    assert len(calls) == 1
    assert calls[0]["skip_clahe"] is False
    assert det_hot.recovered_slot_count == 0

    # Recovery path: slot 2 missing -> primary recovery uses skip_clahe=True (no double CLAHE!), exits without fallback
    calls.clear()
    det_rec = AugmentPickerDetector(_SpyOcr(drop_slot2=True), matcher, card_roi, title_roi)  # type: ignore[arg-type]
    res_rec = det_rec.process_frame(_make_textured_frame(seed=12), game_rect, force=True)
    assert [c.augment.augment_id for c in res_rec.cards] == ["1415", "3262", "1087"]
    assert len(calls) == 2
    assert calls[0]["skip_clahe"] is False
    assert calls[1]["skip_clahe"] is True
    assert calls[1]["force_dual_pass"] is True


@pytest.mark.parametrize("expected_id,expected_name,slot_idx,png_path_str", REAL_FIELD_PNG_CASES)
def test_detector_recalls_real_field_png_via_winrt(
    expected_id: str,
    expected_name: str,
    slot_idx: int,
    png_path_str: str,
) -> None:
    png_path = Path(png_path_str)
    if not png_path.exists():
        pytest.skip(f"Real field ROI snapshot not found on host: {png_path}")

    img = cv2.imdecode(np.fromfile(str(png_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.size > 0

    matcher = AugmentMatcher(_load_real_catalog())
    ocr = WinRtOcrEngine("zh-Hans-CN")
    card_roi = NormalizedRect(0.08, 0.24, 0.84, 0.48)
    title_roi = NormalizedRect(0.08, 0.385, 0.84, 0.052)
    try:
        det = AugmentPickerDetector(ocr, matcher, card_roi, title_roi)

        # 1) Direct single-slot crop recovery on the exact field PNG
        card_direct = det.recover_slot_from_crop(img, slot_index=slot_idx)
        assert card_direct is not None, f"Direct slot recovery failed for {expected_name} ({png_path.name})"
        assert card_direct.augment.augment_id == expected_id
        assert card_direct.augment.name == expected_name

        # 2) Full process_frame() end-to-end recovery when Pass 1 misses that slot
        fw, fh = 2560, 1440
        tl, tt, tr, tb = title_roi.to_pixels(fw, fh)
        band_w = tr - tl
        frame = np.full((fh, fw, 3), 24, dtype=np.uint8)
        # Ensure _passes_idle_card_structure_gate sees bright title & dark card columns
        frame[tt : tt + img.shape[0], tl : tl + 200] = (230, 230, 230)

        # Paste exact 70x774 slot slice from the field PNG into slot_idx inside title_band
        if slot_idx == 0:
            slot_slice = img[3:73, :774]
            sx0 = tl
        else:
            slot_slice = img[3:73, 32:-32]
            sx0 = tl + int(band_w * 0.32)
        sy0 = tt + 3
        sh_s, sw_s = slot_slice.shape[:2]
        frame[sy0 : sy0 + sh_s, sx0 : sx0 + sw_s] = slot_slice

        # Proxy Pass 1 hits the 2 neighbor cards and drops target `slot_idx`, letting real WinRT recover it
        neighbor_lines_all = {
            0: OcrLine("升级：花晓之剑", x=530.0, y=24.0, w=120.0, h=28.0),
            1: OcrLine("术士果汁盒", x=1010.0, y=24.0, w=130.0, h=28.0),
            2: OcrLine("史莱姆时间", x=1510.0, y=24.0, w=130.0, h=28.0),
        }

        class _Pass1DropTargetSlotOcr:
            def recognize_lines(
                self,
                crop: np.ndarray,
                dual_pass: bool = True,
                force_dual_pass: bool = False,
                skip_clahe: bool = False,
            ) -> list[OcrLine]:
                if crop.shape[1] == band_w:
                    return [ln for s, ln in neighbor_lines_all.items() if s != slot_idx]
                return ocr.recognize_lines(
                    crop,
                    dual_pass=dual_pass,
                    force_dual_pass=force_dual_pass,
                    skip_clahe=skip_clahe,
                )

        det_e2e = AugmentPickerDetector(_Pass1DropTargetSlotOcr(), matcher, card_roi, title_roi)  # type: ignore[arg-type]
        res = det_e2e.process_frame(frame, WindowRect(0, 0, fw, fh, 0), force=True)
        assert res.picker_visible is True
        by_slot = {c.slot_index: c for c in res.cards}
        assert slot_idx in by_slot, f"E2E recovery failed for {expected_name} ({png_path.name})"
        assert by_slot[slot_idx].augment.augment_id == expected_id
        assert slot_idx in res.diagnostics["recovered_slots"]
    finally:
        ocr.close()


# ---------------------------------------------------------------------------
# 3. 2-frame single-slot dropout flicker tolerance (_subset_drop_frames <= 2)
# ---------------------------------------------------------------------------


def test_detector_tolerates_two_consecutive_subset_dropout_frames() -> None:
    matcher = AugmentMatcher(_load_real_catalog())
    card_roi = NormalizedRect(0.08, 0.24, 0.84, 0.48)
    title_roi = NormalizedRect(0.08, 0.385, 0.84, 0.052)
    game_rect = WindowRect(left=0, top=0, width=2560, height=1440, hwnd=1)
    tl, _, tr, _ = title_roi.to_pixels(2560, 1440)
    band_w = tr - tl

    line_0 = OcrLine("双生火焰", x=band_w * 0.12, y=24.0, w=120.0, h=28.0)
    line_1 = OcrLine("术士果汁盒", x=band_w * 0.46, y=24.0, w=130.0, h=28.0)
    line_2 = OcrLine("史莱姆时间", x=band_w * 0.78, y=24.0, w=130.0, h=28.0)

    class _TwoFrameDropOcr:
        def __init__(self, frames: list[list[OcrLine]]) -> None:
            self.frames = frames
            self.idx = 0

        def recognize_lines(
            self,
            crop: np.ndarray,
            dual_pass: bool = True,
            force_dual_pass: bool = False,
            skip_clahe: bool = False,
        ) -> list[OcrLine]:
            if crop.shape[1] != band_w:
                return []
            out = self.frames[min(self.idx, len(self.frames) - 1)]
            self.idx += 1
            return out

    ocr = _TwoFrameDropOcr(
        [
            [line_0, line_1, line_2],  # f0: pending
            [line_0, line_1, line_2],  # f1: locked (1415, 2132, 3262)
            [line_0, line_1],          # f2: dropout frame 1 -> grace holds all 3
            [line_0, line_1],          # f3: dropout frame 2 -> grace STILL holds all 3!
            [line_0, line_1, line_2],  # f4: slot 2 returns -> zero false reroll
        ]
    )
    det = AugmentPickerDetector(ocr, matcher, card_roi, title_roi)  # type: ignore[arg-type]
    results = [det.process_frame(_make_textured_frame(seed=300 + i), game_rect) for i in range(5)]

    assert results[1].stable is True
    assert [c.augment.augment_id for c in results[1].cards] == ["1415", "2132", "3262"]

    # Both consecutive dropout frames (f2 and f3) MUST keep all 3 locked cards with rerolled=False
    for fi in (2, 3):
        assert results[fi].picker_visible is True
        assert results[fi].stable is True
        assert results[fi].rerolled is False
        assert [c.augment.augment_id for c in results[fi].cards] == ["1415", "2132", "3262"]
    assert det.false_reroll_suppressed_count == 2

    # Slot 2 recovery on f4 emits no false reroll
    assert results[4].rerolled is False
    assert [c.augment.augment_id for c in results[4].cards] == ["1415", "2132", "3262"]


# ---------------------------------------------------------------------------
# 4. AugmentIconMatcher blind-scan diag & lazy hud_wide_diag_crop
# ---------------------------------------------------------------------------


def test_icon_matcher_blind_scan_attaches_lazy_hud_wide_diag_crop(monkeypatch: pytest.MonkeyPatch) -> None:
    matcher = AugmentIconMatcher(cache_dir=None, allow_download=False)
    # Use a vivid chromatic icon (magenta/cyan rings) so HSV histogram does not trivially match gray patches
    icon_a = np.full((64, 64, 3), (18, 22, 16), dtype=np.uint8)
    cv2.circle(icon_a, (32, 32), 22, (235, 60, 220), -1)
    cv2.circle(icon_a, (32, 32), 10, (240, 210, 40), -1)
    icon_b = _make_distinct_icon(14, size=64)
    assert matcher.register_template("1001", icon_a, name="泰坦的坚决") is True
    assert matcher.register_template("1002", icon_b, name="尖端发明家") is True

    fw, fh = 2560, 1440
    rect = WindowRect(0, 0, fw, fh, 0)
    frame_ok = np.full((fh, fw, 3), 20, dtype=np.uint8)
    slot0_rect = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=1.0))[0]
    x0, y0, x1, y1 = slot0_rect
    frame_ok[y0:y1, x0:x1] = cv2.resize(icon_a, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)

    # Spy on build_hud_wide_diag_crop to prove zero allocation on regular path
    wide_calls: list[tuple[int, int]] = []
    orig_wide = matcher.build_hud_wide_diag_crop

    def _spy_wide(f: np.ndarray, gr: WindowRect | None = None) -> np.ndarray | None:
        wide_calls.append(f.shape[:2])
        return orig_wide(f, gr)

    monkeypatch.setattr(matcher, "build_hud_wide_diag_crop", _spy_wide)

    # Normal hit -> hud_strip_blind=False, NO hud_wide_diag_crop allocated
    hit = matcher.verify_picked_augment(frame_ok, rect, candidate_ids=["1001", "1002"], slot_index_hint=0)
    assert hit is not None and hit.augment_id == "1001"
    diag_ok = matcher.last_verify_diagnostics()
    assert diag_ok["hud_strip_blind"] is False
    assert "hud_wide_diag_crop" not in diag_ok
    assert wide_calls == []

    # Blind scan: default HUD slots land on inverted/orthogonal green stat strip (score <= 0.0),
    # while real icon sits shifted inside the wide bottom-left HUD area (hud_scale=0.5)
    frame_blind = np.full((fh, fw, 3), 20, dtype=np.uint8)
    for sx0, sy0, sx1, sy1 in dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=1.0)).values():
        frame_blind[sy0:sy1, sx0:sx1] = (40, 220, 40)
        cx, cy = (sx0 + sx1) // 2, (sy0 + sy1) // 2
        frame_blind[cy - 10 : cy + 10, cx - 10 : cx + 10] = (15, 25, 15)  # inverted center hole -> negative tmpl corr

    scaled_s0 = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=0.5))[0]
    ax0, ay0, ax1, ay1 = scaled_s0
    frame_blind[ay0:ay1, ax0:ax1] = cv2.resize(icon_b, (ax1 - ax0, ay1 - ay0), interpolation=cv2.INTER_AREA)

    miss = matcher.verify_picked_augment(frame_blind, rect, candidate_ids=["1001"], slot_index_hint=0, hud_scale=1.0)
    assert miss is None
    diag_blind = matcher.last_verify_diagnostics()
    assert float(diag_blind["best_score"]) <= 0.0
    assert diag_blind["hud_strip_blind"] is True
    wide_crop = diag_blind.get("hud_wide_diag_crop")
    assert isinstance(wide_crop, np.ndarray)
    expected_h = fh - int(round(fh * 0.84))
    expected_w = int(round(fw * 0.38)) - int(round(fw * 0.05))
    assert wide_crop.shape == (expected_h, expected_w, 3)
    assert len(wide_calls) == 1

    # Also verify subwindow game_rect slicing in build_hud_wide_diag_crop
    desktop = np.full((1440, 2560, 3), 10, dtype=np.uint8)
    sub_rect = WindowRect(left=320, top=180, width=1920, height=1080, hwnd=2)
    desktop[180 + 920 : 180 + 960, 320 + 200 : 320 + 240] = (40, 200, 240)
    sub_wide = build_hud_wide_diag_crop(desktop, sub_rect)
    assert sub_wide is not None
    assert sub_wide.shape == (1080 - int(round(1080 * 0.84)), int(round(1920 * 0.38)) - int(round(1920 * 0.05)), 3)


# ---------------------------------------------------------------------------
# 5. DevLabAnalyzer window_stats denominator priority & legacy fallback
# ---------------------------------------------------------------------------


def test_devlab_analyzer_window_stats_priority_and_legacy_fallback(tmp_path: Path) -> None:
    # 1) Match WITH vision.pipeline.window_stats (like #20 龙王): 1/3051 -> 0.03%, NO false alarm
    dir_ws = tmp_path / "with_window_stats"
    rec = DevLabFlightRecorder(root_dir=dir_ws, enable_roi_snapshots=False, enable_debug_log=False, app_version="1.5.0")
    try:
        rec.begin_match(match_key_hash=hash_match_key("m_ws"), hero_id="136", hero_name="奥瑞利安索尔", dataset_tag="16.23/normal")
        rec.emit(
            DOMAIN_VISION,
            EV_VISION_WINDOW_STATS,
            {"window_s": 144.3, "total_frames": 3051, "idle_dual_pass_frames": 1, "idle_dual_pass_rate": 0.0003},
            game_time_s=600.0,
            game_state="IN_GAME",
        )
        rec.emit(
            DOMAIN_VISION,
            EV_VISION_LATENCY_SPIKE,
            {"picker_open": False, "ocr_p1_ms": 9.0, "ocr_p2_ms": 10.4, "dual_pass_ocr_ran": True},
            game_time_s=605.0,
            game_state="IN_GAME",
        )
        rec.emit(DOMAIN_GAME, EV_MATCH_EOG, {"won": 1, "duration_s": 1033, "eog_source": "eog_stats_block"}, game_time_s=1033.0)
        rec.end_match(won=1, duration_s=1033, eog_source="eog_stats_block")
        assert rec.flush(timeout_s=3.0) is True
    finally:
        rec.flush_and_close()

    analyzer_ws = DevLabAnalyzer(root_dir=dir_ws)
    matches_ws = analyzer_ws.list_matches()
    assert len(matches_ws) == 1
    replay_ws = analyzer_ws.replay_match(matches_ws[0]["match_dir"], write_outputs=False)
    assert replay_ws.facts["ingame_vision_frames"] == 3051
    assert replay_ws.facts["ingame_idle_dual_pass_frames"] == 1
    assert replay_ws.kpis["runtime"]["in_game_idle_dual_pass_rate"] == pytest.approx(1.0 / 3051.0, abs=1e-4)
    report_ws = analyzer_ws.generate_evolution_report(last_n=5)
    assert "DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION" not in {i.detector_rule for i in report_ws.issues}

    # 2) Legacy match WITHOUT window_stats: falls back to EV_VISION_LATENCY_SPIKE (1/1 = 100%)
    dir_old = tmp_path / "legacy_no_window_stats"
    rec_old = DevLabFlightRecorder(root_dir=dir_old, enable_roi_snapshots=False, enable_debug_log=False, app_version="1.4.9")
    try:
        rec_old.begin_match(match_key_hash=hash_match_key("m_old"), hero_id="136", hero_name="奥瑞利安索尔", dataset_tag="16.23/normal")
        rec_old.emit(
            DOMAIN_VISION,
            EV_VISION_LATENCY_SPIKE,
            {"picker_open": False, "ocr_p1_ms": 30.0, "ocr_p2_ms": 40.0, "in_game_idle_dual_pass": True},
            game_time_s=400.0,
            game_state="IN_GAME",
        )
        rec_old.emit(DOMAIN_GAME, EV_MATCH_EOG, {"won": 1, "duration_s": 900, "eog_source": "eog_stats_block"}, game_time_s=900.0)
        rec_old.end_match(won=1, duration_s=900, eog_source="eog_stats_block")
        assert rec_old.flush(timeout_s=3.0) is True
    finally:
        rec_old.flush_and_close()

    analyzer_old = DevLabAnalyzer(root_dir=dir_old)
    matches_old = analyzer_old.list_matches()
    replay_old = analyzer_old.replay_match(matches_old[0]["match_dir"], write_outputs=False)
    assert replay_old.facts["ingame_vision_frames"] == 1
    assert replay_old.facts["ingame_idle_dual_pass_frames"] == 1
    assert replay_old.kpis["runtime"]["in_game_idle_dual_pass_rate"] == pytest.approx(1.0)
    report_old = analyzer_old.generate_evolution_report(last_n=5)
    assert "DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION" in {i.detector_rule for i in report_old.issues}

    # 3) Real host Match #20 (奥瑞利安索尔) verification when present
    real_m20 = Path(r"C:\Users\Administrator\AppData\Local\HexHelper\devlab\matches\m_00020_20261127_152741_cs")
    if real_m20.exists():
        real_analyzer = DevLabAnalyzer()
        rep20 = real_analyzer.replay_match(real_m20, write_outputs=False)
        assert rep20.facts["ingame_vision_frames"] == 3051
        assert rep20.facts["ingame_idle_dual_pass_frames"] == 1
        assert rep20.kpis["runtime"]["in_game_idle_dual_pass_rate"] < 0.01
        assert "DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION" not in {i.detector_rule for i in rep20.issues}
