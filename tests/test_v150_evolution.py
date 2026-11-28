"""Comprehensive v1.5.0 forensic evolution regression & verification suite (WS-1 through WS-9 + Commercial Isolation)."""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import threading
import time
from typing import Any

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication

from hex_helper import app as app_mod
from hex_helper import edition as edition_mod
from hex_helper.app import GameState, HexHelperController, HexStateMachine
from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload, CoachRequestPayload, utc_now_iso
from hex_helper.coach.gemini_coach import (
    GeminiCallStats,
    GeminiCoachWorker,
    build_coach_prompt_payload,
    build_picker_fast_prompt_payload,
    ground_coach_advice,
)
from hex_helper.config import AppConfig, NormalizedRect, resolve_thinking_level
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import ChampSelectState, LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.scoring.augment_combo import (
    INSTANT_PRISMATIC_TRANSMUTE_IDS,
    LATE_STACKER_AUGMENT_DISCOUNTS,
    evaluate_picker_combos,
    infer_match_build_profile,
)
from hex_helper.scoring.matchup_optimizer import (
    infer_hero_profile,
    infer_unified_hero_profile,
    reconcile_match_build_plan_with_trace,
)
from hex_helper.scoring.tier import AugmentScore, rank_picker_cards
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.match_recap_card import (
    CARD_DEFAULT_HEIGHT,
    CARD_DEFAULT_WIDTH,
    MatchRecapData,
    compute_match_rating,
    normalize_final_stats,
    render_match_recap_image,
)
from hex_helper.ui.resizable_overlay import compute_auto_overlay_scale
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher
from hex_helper.vision.capture import CaptureResult, WindowRect, is_black_frame
from hex_helper.vision.click_tracker import ClickTracker
from hex_helper.vision.detector import (
    AugmentPickerDetector,
    DetectedCard,
    PickerDetectionResult,
    estimate_slot_rarity_with_ratios,
)
from hex_helper.vision.item_icon_matcher import ItemIconMatcher
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import OcrLine
from hex_helper.vision.transmute import TransmuteRevealWatcher
from hex_helper.vision.worker import (
    GrayScreenDeathDetector,
    HexVisionWorker,
    VisionFrameOutput,
    VisionTickContext,
    compute_center_mean_saturation,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class _NoopCapturer:
    backend = "noop"
    consecutive_black_frames = 0

    def grab(self, **_kw: Any) -> CaptureResult:
        return CaptureResult(frame_bgr=None, backend="noop", is_black=True, mean_luma=0.0)

    def close(self) -> None:
        return None


class _NoopHotkeys(QAbstractNativeEventFilter):
    last_failed_hotkeys: dict[str, int] = {}

    def nativeEventFilter(self, _eventType: Any, _message: Any) -> tuple[bool, int]:
        return False, 0

    def register(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def unregister_all(self) -> None:
        return None


def _make_distinct_icon(seed: int, size: int = 64) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:, :] = rng.integers(30, 230, size=3, dtype=np.uint8)
    for _ in range(6):
        c = tuple(int(x) for x in rng.integers(20, 255, size=3))
        p1 = (int(rng.integers(4, size - 4)), int(rng.integers(4, size - 4)))
        p2 = (int(rng.integers(4, size - 4)), int(rng.integers(4, size - 4)))
        cv2.rectangle(img, p1, p2, c, -1)
    cv2.circle(img, (size // 2, size // 2), size // 5, (int((seed * 73) % 255), int((seed * 41) % 255), 220), -1)
    return img


def _make_rich_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", fetcher=lambda _u: b"")
    static.items = {
        # AD items
        "3071": ItemStaticInfo("3071", "黑色切割者", 3000, ("Damage", "Health"), {"FlatPhysicalDamageMod": 40.0, "FlatHPPoolMod": 400.0}, "", "complete_equipment"),
        "3153": ItemStaticInfo("3153", "破败王者之刃", 3200, ("Damage", "AttackSpeed", "LifeSteal"), {"FlatPhysicalDamageMod": 40.0, "PercentAttackSpeedMod": 0.25}, "", "complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3400, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25}, "", "complete_equipment", components=("1038", "1037", "1018")),
        "3036": ItemStaticInfo("3036", "多米尼克领主的致意", 3000, ("Damage", "ArmorPenetration"), {"FlatPhysicalDamageMod": 35.0}, "", "complete_equipment"),
        "6333": ItemStaticInfo("6333", "死亡之舞", 3300, ("Damage", "Armor"), {"FlatPhysicalDamageMod": 60.0, "FlatArmorMod": 50.0}, "", "complete_equipment"),
        "3072": ItemStaticInfo("3072", "饮血剑", 3400, ("Damage", "LifeSteal"), {"FlatPhysicalDamageMod": 80.0}, "", "complete_equipment"),
        # Components
        "1038": ItemStaticInfo("1038", "暴风之剑", 1300, ("Damage",), {"FlatPhysicalDamageMod": 40.0}, "", "other"),
        "1037": ItemStaticInfo("1037", "十字镐", 875, ("Damage",), {"FlatPhysicalDamageMod": 25.0}, "", "other"),
        "1018": ItemStaticInfo("1018", "灵巧披风", 600, ("CriticalStrike",), {"FlatCritChanceMod": 0.15}, "", "other"),
        "1058": ItemStaticInfo("1058", "无用大棒", 1200, ("SpellDamage",), {"FlatMagicDamageMod": 65.0}, "", "other"),
        # Tank items
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0}, "", "complete_equipment"),
        "3068": ItemStaticInfo("3068", "日炎圣盾", 2700, ("Health", "Armor"), {"FlatHPPoolMod": 350.0, "FlatArmorMod": 50.0}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Health", "Armor"), {"FlatHPPoolMod": 150.0, "FlatArmorMod": 75.0}, "", "complete_equipment"),
        # AP items
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment", components=("1058", "1058")),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105.0}, "", "complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage",), {"FlatMagicDamageMod": 95.0}, "", "complete_equipment"),
        "2510": ItemStaticInfo("2510", "黄昏与黎明", 3100, ("SpellDamage", "AttackSpeed"), {"FlatMagicDamageMod": 75.0, "PercentAttackSpeedMod": 0.25}, "", "complete_equipment"),
        # Enchanter Support items
        "6617": ItemStaticInfo("6617", "月石再生器", 2200, ("Health", "SpellDamage", "ManaRegen"), {"FlatMagicDamageMod": 25.0, "FlatHPPoolMod": 200.0}, "", "complete_equipment"),
        "3504": ItemStaticInfo("3504", "炽热香炉", 2200, ("SpellDamage", "ManaRegen"), {"FlatMagicDamageMod": 45.0}, "", "complete_equipment"),
        "6616": ItemStaticInfo("6616", "流水法杖", 2250, ("SpellDamage", "ManaRegen"), {"FlatMagicDamageMod": 35.0}, "", "complete_equipment"),
        "3107": ItemStaticInfo("3107", "救赎", 2300, ("Health", "ManaRegen"), {"FlatHPPoolMod": 200.0}, "", "complete_equipment"),
        "3222": ItemStaticInfo("3222", "米凯尔的祝福", 2300, ("Health", "ManaRegen"), {"FlatHPPoolMod": 250.0}, "", "complete_equipment"),
        # Boots
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "3047": ItemStaticInfo("3047", "铁板靴", 1200, ("Boots", "Armor"), {"FlatArmorMod": 25.0}, "", "boots"),
        "3111": ItemStaticInfo("3111", "水银之靴", 1300, ("Boots", "SpellBlock"), {"FlatSpellBlockMod": 20.0}, "", "boots"),
        # Item 4011 WITHOUT explicit category ("") to verify DataStore.get_item_info fallback -> complete_equipment
        "4011": ItemStaticInfo("4011", "花晓之剑", 2500, ("AttackSpeed",), {"FlatMagicDamageMod": 35.0, "PercentAttackSpeedMod": 0.30}, "", ""),
    }
    store = DataStore(static)
    store._descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "6": HeroStat("6", "厄加特", "无畏战车", 35000, 18200, 0.52, 0.04, 30, False),
        "44": HeroStat("44", "塔里克", "瓦洛兰之盾", 28000, 15120, 0.54, 0.03, 12, False),
        "99": HeroStat("99", "拉克丝", "光辉女郎", 80000, 41600, 0.52, 0.08, 15, False),
    }
    # Deliberately give Urgot ("6") and Taric ("44") tank core-build priors to verify static-prior decay
    store._core_builds = {
        "6": [CoreBuild(("3084", "3068", "3075"), True, 9000, 4680, 0.52, 0.35)],
        "44": [CoreBuild(("3084", "3068", "3075"), True, 8000, 4320, 0.54, 0.40)],
        "99": [CoreBuild(("6655", "3089", "3157"), True, 12000, 6240, 0.52, 0.42)],
    }
    store._augments = {
        "1001": AugmentInfo("1001", "珠光护手", "prismatic", "技能可以暴击"),
        "1002": AugmentInfo("1002", "尤里卡", "prismatic", "法强转化为技能急速"),
        "1004": AugmentInfo("1004", "回归基本功", "gold", "技能急速与伤害提升"),
        "1152": AugmentInfo("1152", "钢化你心", "prismatic", "心之钢叠层加速"),
        "1238": AugmentInfo("1238", "质变：棱彩阶", "gold", "立刻获得一个随机棱彩强化符文"),
        "1353": AugmentInfo("1353", "坦克引擎", "silver", "参与击杀叠加最大生命值"),
        "1404": AugmentInfo("1404", "属性！", "silver", "获得属性碎片"),
        "2077": AugmentInfo("2077", "贪欲束缚", "gold", "获得生命值"),
        "3116": AugmentInfo("3116", "将他们放倒倒倒", "gold", "位移突进"),
        "3121": AugmentInfo("3121", "水果化", "silver", "治疗果实强化"),
        "3163": AugmentInfo("3163", "迷人蝴蝶", "gold", "普攻触发治疗护盾"),
        "3171": AugmentInfo("3171", "力量", "silver", "获得力量"),
    }
    return store


def _patch_headless_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)
    monkeypatch.setattr(
        app_mod,
        "find_game_window",
        lambda: WindowRect(hwnd=1, left=0, top=0, width=2560, height=1440),
    )
    monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: True)


# ===========================================================================
# WS-1: HexVisionWorker, GrayScreenDeathDetector, BLAS Matchers, is_black_frame, ClickTracker
# ===========================================================================


def test_ws1_vision_worker_lifecycle_immutability_and_stale_gen_drop(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. VisionTickContext and VisionFrameOutput are strictly frozen dataclasses
    ctx = VisionTickContext(vision_gen=3, game_state="IN_GAME", hero_id="6", is_degraded_2999=True)
    out_sample = VisionFrameOutput(vision_gen=3, now_mono=12.5, visual_is_dead=True)
    with pytest.raises(FrozenInstanceError):
        ctx.vision_gen = 4  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        out_sample.vision_gen = 4  # type: ignore[misc]

    # 2. HexVisionWorker daemon thread start -> context push -> force_rescan -> stop
    bright_frame = np.full((360, 640, 3), (40, 180, 220), dtype=np.uint8)

    class _StubCapturer:
        backend = "stub"
        consecutive_black_frames = 0

        def grab(self, **_kw: Any) -> CaptureResult:
            return CaptureResult(frame_bgr=bright_frame, backend="stub", is_black=False, mean_luma=140.0)

        def close(self) -> None:
            return None

    received: list[VisionFrameOutput] = []
    got_ev = threading.Event()

    def _on_out(o: VisionFrameOutput) -> None:
        received.append(o)
        got_ev.set()

    worker = HexVisionWorker(
        on_output=_on_out,
        capturer=_StubCapturer(),  # type: ignore[arg-type]
        ocr_engine=None,
        tick_interval_s=0.05,
        find_window_fn=lambda: WindowRect(hwnd=1, left=0, top=0, width=640, height=360),
        is_foreground_fn=lambda _h: True,
        get_cursor_pos_fn=lambda: (100, 100),
    )
    worker._ocr_init_attempted = True  # Avoid instantiating real WinRT OCR in unit test
    try:
        worker.start()
        assert worker.is_running() is True
        worker.update_context(
            VisionTickContext(vision_gen=7, game_state="IN_GAME", hero_id="6", is_degraded_2999=True)
        )
        worker.enqueue_command("force_rescan")
        assert got_ev.wait(timeout=3.0) is True
        assert received[-1].vision_gen == 7
        assert received[-1].capture_backend == "stub"
    finally:
        worker.stop(timeout=2.0)
        assert worker.is_running() is False

    # 3. Stale vision_gen frame output is dropped cleanly by HexHelperController._on_vision_worker_output
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl._vision_gen = 5
        ctrl._vision_backend = "initial"
        ctrl._visual_is_dead = None
        ctrl.sm.is_degraded_2999 = True

        # Older gen=4 MUST be dropped without mutating controller state
        ctrl._on_vision_worker_output(
            VisionFrameOutput(vision_gen=4, now_mono=100.0, capture_backend="stale_backend", visual_is_dead=True)
        )
        assert ctrl._vision_backend == "initial"
        assert ctrl._visual_is_dead is None

        # Current gen=5 MUST be accepted
        ctrl._on_vision_worker_output(
            VisionFrameOutput(vision_gen=5, now_mono=101.0, capture_backend="fresh_backend", visual_is_dead=True)
        )
        assert ctrl._vision_backend == "fresh_backend"
        assert ctrl._visual_is_dead is True
    finally:
        ctrl.shutdown()
        qapp.processEvents()


def test_ws1_gray_screen_hsv_death_detector_hysteresis_and_speed() -> None:
    det = GrayScreenDeathDetector()
    # Grayscale frame (B=G=R=110 -> saturation 0 < 18)
    gray_frame = np.full((1440, 2560, 3), 110, dtype=np.uint8)
    # Mid-saturation deadzone (HSV S=22 -> between 18 and 26)
    mid_hsv = np.full((360, 640, 3), (90, 22, 160), dtype=np.uint8)
    mid_bgr = cv2.cvtColor(mid_hsv, cv2.COLOR_HSV2BGR)
    # Alive colorful frame (HSV S=140 >= 26)
    alive_hsv = np.full((1440, 2560, 3), (100, 140, 180), dtype=np.uint8)
    alive_bgr = cv2.cvtColor(alive_hsv, cv2.COLOR_HSV2BGR)

    assert compute_center_mean_saturation(gray_frame) < 18.0
    assert compute_center_mean_saturation(alive_bgr) >= 26.0

    # Performance budget: < 5ms even on 1440p (target <0.25ms)
    t0 = time.perf_counter()
    for _ in range(10):
        compute_center_mean_saturation(alive_bgr)
    avg_ms = (time.perf_counter() - t0) * 100.0
    assert avg_ms < 5.0, f"compute_center_mean_saturation too slow: {avg_ms:.2f}ms"

    # 1st gray frame -> still None (requires 2 consecutive frames)
    assert det.update(gray_frame) is None
    # 2nd gray frame -> DEAD (True)
    assert det.update(gray_frame) is True
    # Black frame MUST NOT flip dead state
    assert det.update(np.zeros((360, 640, 3), dtype=np.uint8), is_black=True) is True
    # Mid-saturation hysteresis zone (18..26) stays DEAD
    assert det.update(mid_bgr) is True
    assert det.update(mid_bgr) is True
    # 1st colorful frame -> still DEAD (2-frame revive hysteresis)
    assert det.update(alive_bgr) is True
    # 2nd colorful frame -> ALIVE (False)
    assert det.update(alive_bgr) is False

    det.reset()
    assert det.is_dead is None


def test_ws1_vectorized_matchers_is_black_frame_and_click_tracker() -> None:
    # 1. is_black_frame strided [::16, ::16] fast path + full-frame fallback
    bright_1440p = np.full((1440, 2560, 3), 90, dtype=np.uint8)
    dark_1440p = np.zeros((1440, 2560, 3), dtype=np.uint8)
    t0 = time.perf_counter()
    black_b, luma_b = is_black_frame(bright_1440p)
    fast_ms = (time.perf_counter() - t0) * 1000.0
    assert black_b is False and luma_b > 50.0
    assert fast_ms < 5.0, f"is_black_frame strided fast path too slow: {fast_ms:.2f}ms"

    black_d, luma_d = is_black_frame(dark_1440p)
    assert black_d is True and luma_d < 1.0

    # 2. Vectorized ItemIconMatcher (500 templates) & AugmentIconMatcher (237 templates) < 10ms & 100% accurate
    im = ItemIconMatcher(cache_dir=None)
    i_icons = {str(5000 + i): _make_distinct_icon(2000 + i) for i in range(500)}
    for iid, img in i_icons.items():
        assert im.register_template(iid, img)
    assert im.template_count == 500
    f_item = np.full((1440, 2560, 3), 12, dtype=np.uint8)
    i_rects = dict(im.get_hud_item_slot_rects(2560, 1440, hud_scale=1.0))
    i_placed = {0: "5010", 1: "5120", 2: "5250", 3: "5360", 4: "5410", 5: "5495"}
    for sl, iid in i_placed.items():
        x0, y0, x1, y1 = i_rects[sl]
        f_item[y0:y1, x0:x1] = cv2.resize(i_icons[iid], (x1 - x0, y1 - y0))
    grect_1440 = WindowRect(hwnd=1, left=0, top=0, width=2560, height=1440)
    # Warm lazy BLAS matrix stack, then benchmark cold (slot cache cleared)
    im.scan_hud_slots_with_state(f_item, grect_1440, hud_scale=1.0)
    im.clear_slot_cache()
    ti0 = time.perf_counter()
    i_hits, _ = im.scan_hud_slots_with_state(f_item, grect_1440, hud_scale=1.0)
    item_ms = (time.perf_counter() - ti0) * 1000.0
    assert {h.hud_slot_index: h.item_id for h in i_hits} == i_placed
    assert item_ms < 10.0, f"ItemIconMatcher vectorized 500-template scan exceeded 10ms: {item_ms:.2f}ms"

    am = AugmentIconMatcher(cache_dir=None, allow_download=False)
    a_icons = {str(1000 + i): _make_distinct_icon(7000 + i) for i in range(237)}
    for aid, img in a_icons.items():
        am.register_template(aid, img)
    f_aug = np.full((1080, 1920, 3), 12, dtype=np.uint8)
    a_rects = dict(am.get_hud_slot_rects(1920, 1080))
    a_placed = {0: "1005", 1: "1077", 2: "1150", 3: "1230"}
    for sl, aid in a_placed.items():
        x0, y0, x1, y1 = a_rects[sl]
        f_aug[y0:y1, x0:x1] = cv2.resize(a_icons[aid], (x1 - x0, y1 - y0))
    grect_1080 = WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080)
    am.scan_all_hud_augments(f_aug, grect_1080, hud_scale=1.0)
    am.clear_slot_cache()
    ta0 = time.perf_counter()
    a_hits = am.scan_all_hud_augments(f_aug, grect_1080, hud_scale=1.0)
    aug_ms = (time.perf_counter() - ta0) * 1000.0
    assert {h.hud_slot_index: h.augment_id for h in a_hits} == a_placed
    assert aug_ms < 10.0, f"AugmentIconMatcher vectorized 237-template scan exceeded 10ms: {aug_ms:.2f}ms"

    # 3. ClickTracker: dt <= 0.40 speed window & <0.72 fast_vanish_diffs >= 1.35x tiebreaker
    ct = ClickTracker()
    ct.reset(now_mono=10.0)
    # 0.35s intervals (would be None under legacy dt<=0.25, now computes slow speed <=220 px/s!)
    ct.record_cursor_sample(10.00, 1, clicked=False, pos=(960, 540))
    ct.record_cursor_sample(10.35, 1, clicked=False, pos=(964, 542))
    ct.record_cursor_sample(10.70, 1, clicked=False, pos=(966, 543))
    slot_dt, src_dt = ct.resolve_selection(close_ts=10.75, valid_slots={0, 1, 2})
    assert slot_dt == 1
    assert src_dt == "cursor_dwell"
    assert ct.last_resolve_diagnostics()["has_pos_samples"] is True
    assert 1 in ct.last_resolve_diagnostics()["decisive_slots"]

    # Ambiguous low-confidence dwell slightly favoring slot 0 -> fast_vanish_diffs >= 1.35x overrides to slot 2!
    ct2 = ClickTracker()
    ct2.reset(now_mono=20.0)
    ct2.record_cursor_sample(20.50, 2, clicked=False, pos=(1400, 500))
    ct2.record_cursor_sample(20.56, 2, clicked=False, pos=(1460, 500))
    ct2.record_cursor_sample(20.62, 0, clicked=False, pos=(400, 500))
    ct2.record_cursor_sample(20.68, 0, clicked=False, pos=(460, 500))
    slot_vb, src_vb = ct2.resolve_selection(
        close_ts=20.72,
        valid_slots={0, 1, 2},
        fast_vanish_diffs={0: 24.0, 1: 21.0, 2: 52.0},  # slot 2 is 2.17x >= 1.35x
    )
    assert slot_vb == 2
    assert src_vb == "fast_vanish_tiebreak"
    assert ct2.last_resolve_diagnostics()["fast_vanish_tiebreak_used"] is True


# ===========================================================================
# WS-2: Plan B Isolation, 2-char Substring Guard, Stat Anvil Rejection, ROI Migration, Tab Guard
# ===========================================================================


def test_ws2_cross_match_plan_b_isolation_and_ttl_suppression(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "ws2_appdata"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()

        # Simulate M#7 Gnar end-of-match state inside Plan B vote trackers & state machine
        ctrl._item_vote_tracker.update({0: "3084", 1: "3068", 2: "3153"}, force_confirm=True)
        ctrl._aug_vote_tracker.update({0: "1319"}, force_confirm=True)
        ctrl._aug_source_by_id["1319"] = "plan_b_hud"
        ctrl.sm.is_degraded_2999 = True
        ctrl.sm.visual_owned_items = ["3084", "3068", "3153"]
        ctrl.sm.suppress_visual_item("1011", now_mono=500.0)
        ctrl.sm.suppressed_visual_augments.add("1319")
        ctrl.sm.selected_augments = ["1319"]
        prev_gen = ctrl._vision_gen

        # Transitioning to M#8 Urgot calls _reset_plan_b_trackers(clear_sm_visual=True) -> ZERO M#7 leak!
        ctrl._reset_plan_b_trackers(clear_sm_visual=True)
        assert ctrl._vision_gen == prev_gen + 1
        assert ctrl._item_vote_tracker.update({}) == []
        assert ctrl._aug_vote_tracker.update({}) == []
        assert ctrl._aug_source_by_id == {}
        assert ctrl.sm.visual_owned_items == []
        assert ctrl.sm.suppressed_visual_items == {}
        assert ctrl.sm.suppressed_visual_augments == set()

        # Verify 45s TTL on suppressed_visual_items
        ctrl.sm.suppress_visual_item("1011", now_mono=1000.0)
        assert ctrl.sm.is_visual_item_suppressed("1011", now_mono=1040.0) is True
        assert ctrl.sm.is_visual_item_suppressed("1011", now_mono=1046.0) is False
    finally:
        ctrl.shutdown()
        qapp.processEvents()


def test_ws2_matcher_2char_guard_detector_stat_anvil_roi_migration_and_tab_guard(tmp_path: Path) -> None:
    # 1. matcher.py 2-char substring guard blocks "力量碎片" / "属性锻造器" / 27-char tooltip!
    catalog = [
        AugmentInfo("3171", "力量", "silver", "获得力量"),
        AugmentInfo("1404", "属性！", "silver", "获得属性"),
        AugmentInfo("1001", "珠光护手", "prismatic", "技能可暴击"),
        AugmentInfo("1002", "尤里卡", "prismatic", "法强转急速"),
        AugmentInfo("1003", "炼狱导管", "prismatic", "技能灼烧"),
    ]
    matcher = AugmentMatcher(catalog)
    assert matcher.match_line("力量碎片") is None
    assert matcher.match_line("属性锻造器") is None
    assert matcher.match_line("获得大量力量与额外生命值加成并在团战中持续触发效果共二十七字") is None
    exact_hit = matcher.match_line("力量")
    assert exact_hit is not None and exact_hit.augment.augment_id == "3171"

    # 2. detector.py rejects Stat Anvil popups & masks cyan exclude_rects from rarity estimation
    class _FakeAnvilOcr:
        def recognize_lines(self, _crop: np.ndarray, dual_pass: bool = True, force_dual_pass: bool = False) -> list[OcrLine]:
            return [
                OcrLine("属性锻造器", 40, 10, 120, 24),
                OcrLine("力量碎片", 200, 10, 100, 24),
                OcrLine("攻速碎片", 500, 10, 100, 24),
            ]

    det = AugmentPickerDetector(
        ocr_engine=_FakeAnvilOcr(),  # type: ignore[arg-type]
        matcher=matcher,
        card_band_roi=NormalizedRect(0.08, 0.24, 0.84, 0.48),
        title_band_roi=NormalizedRect(0.08, 0.385, 0.84, 0.052),
    )
    frame = np.full((1080, 1920, 3), 25, dtype=np.uint8)
    # Paint bright title pixels so structural pre-gate lets OCR run
    frame[420:460, 200:1700] = (230, 230, 230)
    res_anvil = det.process_frame(frame, WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080), force=True)
    assert res_anvil.picker_visible is False
    assert res_anvil.diagnostics.get("reason") == "stat_anvil_filtered"

    # Cyan overlay badge covering half of a gold card border MUST be masked out by exclude_rects
    strip = np.full((40, 120, 3), (40, 150, 210), dtype=np.uint8)  # Warm gold in BGR
    strip[:, :70] = (210, 200, 10)  # Bright Hextech cyan overlay badge covering left 70px
    rar_masked, _ = estimate_slot_rarity_with_ratios(
        strip,
        exclude_rects=[(100, 200, 170, 240)],
        strip_origin=(100, 200),
    )
    assert rar_masked == "gold"

    # 3. config.py auto-migrates legacy title_band_roi while preserving custom calibrated ROI
    cfg_old_wide = AppConfig.from_dict({"title_band_roi": {"x": 0.08, "y": 0.31, "w": 0.84, "h": 0.25}})
    assert cfg_old_wide.title_band_roi == NormalizedRect(0.08, 0.385, 0.84, 0.052)
    cfg_old_3card = AppConfig.from_dict({"title_band_roi": {"x": 0.14, "y": 0.31, "w": 0.72, "h": 0.25}})
    assert cfg_old_3card.title_band_roi == NormalizedRect(0.08, 0.385, 0.84, 0.052)
    cfg_custom = AppConfig.from_dict({"title_band_roi": {"x": 0.10, "y": 0.40, "w": 0.80, "h": 0.06}})
    assert cfg_custom.title_band_roi == NormalizedRect(0.10, 0.40, 0.80, 0.06)

    # 4. transmute.py tab_zone 2-frame guard vs bottom_hud single-frame commit
    class _FakeHoverOcr:
        def recognize_lines(self, _crop: np.ndarray, dual_pass: bool = True) -> list[OcrLine]:
            return [
                OcrLine("珠光护手", 40, 20, 120, 24),
                OcrLine("棱彩阶 · 你的技能可以造成暴击伤害", 40, 52, 220, 22),
            ]

    watcher = TransmuteRevealWatcher(_FakeHoverOcr(), catalog)  # type: ignore[arg-type]
    # Textured frame so std(gray) >= 10
    rng = np.random.default_rng(7)
    tex_frame = rng.integers(20, 220, size=(1080, 1920, 3), dtype=np.uint8)
    grect = WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080)
    tab_cursor = (960, 450)  # y_rel = 0.417 -> tab_zone

    # Prime steadiness, then wait >= 0.32s dwell
    assert watcher.scan_passive_hud_hover(tex_frame, grect, set(), cursor_pos=tab_cursor, now_mono=10.0) is None
    # Frame 1 in tab_zone without "强化符文" header -> MUST await 2nd frame confirmation!
    hit1 = watcher.scan_passive_hud_hover(tex_frame, grect, set(), cursor_pos=tab_cursor, now_mono=10.5)
    assert hit1 is None
    assert watcher.last_trace_diagnostics()["rejected_reason"] == "tab_zone_await_confirm"
    # Frame 2 (after 0.70s cooldown) -> commits cleanly!
    hit2 = watcher.scan_passive_hud_hover(tex_frame, grect, set(), cursor_pos=tab_cursor, now_mono=11.3)
    assert hit2 is not None
    assert hit2.augment.augment_id == "1001"
    assert hit2.cursor_zone == "tab_zone"


# ===========================================================================
# WS-3: Boot Silent Strip, 2-Concurrency Worker + Cancel + on_superseded, Fast Picker, Warmup
# ===========================================================================


def test_ws3_grounding_strips_boots_fast_picker_payload_thinking_and_worker_concurrency(tmp_path: Path) -> None:
    store = _make_rich_store(tmp_path)

    # 1. ground_coach_advice silently strips Tier-2 boots from build_path with invalid_route_blocked == 0!
    req = CoachRequestPayload(
        request_id="req-ws3-boots",
        trigger_reason="death_enemy_build_changed",
        timestamp=utc_now_iso(),
        dataset_patch="26.23",
        my_hero={"hero_id": "99", "name": "拉克丝", "gold": 2000, "items": ["卢登的伙伴"]},
        allies=[{"champion": "盖伦", "items": ["日炎圣盾", "多兰之盾"]}],
        enemies=[{"champion": "劫", "items": ["黑色切割者"]}, {"champion": "亚托克斯", "items": ["死亡之舞"]}],
        selected_augments=[{"id": "1001", "name": "珠光护手", "rarity": "prismatic"}],
        offered_augments=[
            {"slot": 0, "id": "1002", "name": "尤里卡", "rarity": "prismatic", "delta": "+2.5%"},
            {"slot": 1, "id": "1004", "name": "回归基本功", "rarity": "gold", "delta": "+1.1%"},
            {"slot": 2, "id": "1238", "name": "质变：棱彩阶", "rarity": "gold", "delta": "+0.8%"},
        ],
        threat_summary=["敌高爆发AD", "缺保命"],
    )
    stats_out: dict[str, int] = {}
    adv = ground_coach_advice(
        {
            "next_items": [{"item_id": "3089", "name": "灭世者的死亡之帽", "priority": "下件必出", "reason": "核爆爆发"}],
            "build_path": ["法师之靴", "灭世者的死亡之帽", "中娅沙漏", "铁板靴", "虚空之杖"],
            "buy_now": "先买无用大棒与法穿鞋",
            "tactical_summary": "站后排拉扯先手控人",
        },
        req,
        store,
        stats_out=stats_out,
    )
    assert "法师之靴" not in adv.build_path
    assert "铁板靴" not in adv.build_path
    assert adv.build_path == ["灭世者的死亡之帽", "中娅沙漏", "虚空之杖"]
    assert stats_out["invalid_route_blocked"] == 0
    assert stats_out["total_blocked"] == 0

    # 2. build_picker_fast_prompt_payload compactness (<50% size of full payload, strips glossary & top builds)
    full_payload = build_coach_prompt_payload(req, store)
    fast_payload = build_picker_fast_prompt_payload(req, store)
    assert "item_glossary" not in fast_payload
    assert "hero_big_data_top_builds" not in fast_payload
    assert "allies" not in fast_payload and "enemies" not in fast_payload
    assert "offered_augments" in fast_payload and "enemy_champs" in fast_payload
    full_json = json.dumps(full_payload, ensure_ascii=False)
    fast_json = json.dumps(fast_payload, ensure_ascii=False)
    assert len(fast_json) < len(full_json) * 0.65

    # 3. resolve_thinking_level: allowlist minimal on 3.6-flash, low floor on 3.8-flash & gemini-4, low death/non-tx
    assert resolve_thinking_level("medium", "augment_picking", model="gemini-3.8-flash") == "low"
    assert resolve_thinking_level("medium", "augment_picking", model="gemini-3.6-flash") == "minimal"
    assert resolve_thinking_level("medium", "augment_picking", model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("medium", "death_enemy_build_changed", model="gemini-4-hydrogen", fast_death=True) == "low"
    assert resolve_thinking_level("medium", "augment_selected", model="gemini-4-hydrogen", is_transformative=False) == "low"
    assert resolve_thinking_level("medium", "pre_death_warmup", model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("medium", "manual_f6", model="gemini-4-hydrogen") == "medium"

    # 4. GeminiCoachWorker 2-concurrency non-blocking preemption + cancel_pending + on_superseded
    picker_entered = threading.Event()
    release_picker = threading.Event()
    selected_done = threading.Event()
    superseded_done = threading.Event()
    advices: list[CoachAdvicePayload] = []
    superseded_calls: list[tuple[str, int]] = []

    class _FakeClient:
        def advise(self, r: CoachRequestPayload, _st: Any, **_kw: Any) -> tuple[CoachAdvicePayload, GeminiCallStats]:
            if r.trigger_reason == "augment_picking":
                picker_entered.set()
                release_picker.wait(timeout=4.0)
                return (
                    CoachAdvicePayload(request_id=r.request_id, augment_pick_advice="旧选卡"),
                    GeminiCallStats(model="gemini-3.8-flash", thinking_level="low", latency_ms=1200, total_tokens=620),
                )
            return (
                CoachAdvicePayload(request_id=r.request_id, tactical_summary="新选后路线"),
                GeminiCallStats(model="gemini-4-hydrogen", thinking_level="low", latency_ms=450, total_tokens=1100),
            )

        def close(self) -> None:
            return None

    worker = GeminiCoachWorker(
        on_advice=lambda a, _s: (advices.append(a), selected_done.set()),
        on_superseded=lambda r, s: (superseded_calls.append((r.request_id, s.total_tokens)), superseded_done.set()),
        client=_FakeClient(),  # type: ignore[arg-type]
        max_workers=2,
    )
    try:
        req_pick = CoachRequestPayload(
            request_id="req-pick-1",
            trigger_reason="augment_picking",
            timestamp=utc_now_iso(),
            dataset_patch="26.23",
            my_hero={"hero_id": "99", "name": "拉克丝"},
        )
        req_sel = CoachRequestPayload(
            request_id="req-sel-2",
            trigger_reason="augment_selected",
            timestamp=utc_now_iso(),
            dataset_patch="26.23",
            my_hero={"hero_id": "99", "name": "拉克丝"},
        )
        worker.submit(req_pick, store, api_key="k", model="gemini-4-hydrogen", thinking_level="medium", proxy_url="")
        assert picker_entered.wait(timeout=2.0) is True

        # Cancel in-flight picker and immediately submit augment_selected -> completes BEFORE picker unblocks!
        cancelled = worker.cancel_pending({"augment_picking"})
        assert cancelled == 1
        worker.submit(req_sel, store, api_key="k", model="gemini-4-hydrogen", thinking_level="medium", proxy_url="")
        assert selected_done.wait(timeout=2.0) is True
        assert [a.request_id for a in advices] == ["req-sel-2"]

        # Release blocked picker -> dropped & reported to on_superseded with real token usage
        release_picker.set()
        assert superseded_done.wait(timeout=2.0) is True
        assert superseded_calls == [("req-pick-1", 620)]
        assert [a.request_id for a in advices] == ["req-sel-2"]
    finally:
        release_picker.set()
        worker.stop()


def test_ws3_pre_death_warmup_trigger_cooldown_and_full_build_suppression(tmp_path: Path) -> None:
    store = _make_rich_store(tmp_path)
    sm = HexStateMachine(store=store)
    enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 0, 1, ("3071",))

    # Enter match at gt=20s -> game_start
    me_start = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 3, 0, 0, 0, ())
    eff0 = sm.on_live_snapshot(LiveMatchSnapshot(20.0, me_start, (me_start,), (enemy,), 1400.0), "99", now_mono=100.0, store=store)
    assert eff0.coach_trigger == "game_start"

    # Alive at gt=110s with gold>=1400 and >=90s elapsed -> triggers pre_death_warmup!
    eff_warm = sm.on_live_snapshot(LiveMatchSnapshot(110.0, me_start, (me_start,), (enemy,), 1650.0), "99", now_mono=195.0, store=store)
    assert eff_warm.coach_trigger == "pre_death_warmup"

    # Within 90s cooldown -> does NOT spam pre_death_warmup
    eff_cd = sm.on_live_snapshot(LiveMatchSnapshot(130.0, me_start, (me_start,), (enemy,), 1900.0), "99", now_mono=230.0, store=store)
    assert eff_cd.coach_trigger == ""

    # 6-item full build with unchanged enemy suppresses routine death_enemy_build_changed
    me_full_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 18, 5, 10, 15, ("6655", "3089", "3157", "3135", "2510", "3020"))
    sm.on_live_snapshot(LiveMatchSnapshot(800.0, me_full_alive, (me_full_alive,), (enemy,), 2800.0), "99", now_mono=300.0, store=store)
    me_full_dead = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", True, 18, 5, 11, 15, ("6655", "3089", "3157", "3135", "2510", "3020"))
    eff_full = sm.on_live_snapshot(LiveMatchSnapshot(820.0, me_full_dead, (me_full_dead,), (enemy,), 2900.0), "99", now_mono=320.0, store=store)
    assert eff_full.coach_trigger == ""
    assert eff_full.death_skip_reason == "full_build_6_items_no_enemy_change"


# ===========================================================================
# WS-4: Unified Hero Profile, 4 Fixed Axes, Reconcile Guards, 1238/1353/4011
# ===========================================================================


def test_ws4_unified_profile_axes_reconcile_gamble_discount_and_4011(tmp_path: Path) -> None:
    store = _make_rich_store(tmp_path)

    # 1. infer_unified_hero_profile static-prior decay + zero cross-engine conflict
    urgot_items = ("3071", "3153", "6333")
    assert infer_unified_hero_profile("6", store, my_items=urgot_items) == "AD"
    assert infer_hero_profile(store, "6", urgot_items, ()) == "AD"
    assert infer_match_build_profile(store, "6", (), urgot_items).primary_profile == "AD"

    taric_items = ("6617", "3504", "6616", "3107", "3222")
    assert infer_unified_hero_profile("44", store, my_items=taric_items) == "SUPPORT"
    assert infer_hero_profile(store, "44", taric_items, ()) == "SUPPORT"
    assert infer_match_build_profile(store, "44", (), taric_items).primary_profile == "SUPPORT"

    # 2. 4 fixed augment axes in assets/augment_combos.json
    combos_json = json.loads((REPO_ROOT / "src" / "hex_helper" / "assets" / "augment_combos.json").read_text(encoding="utf-8"))
    axes_map = combos_json["augment_axes"]
    assert axes_map["1004"] == ["HASTE_LOOP"]
    assert "UTILITY_SUSTAIN" in axes_map["3121"] and "SHIELD_HEAL" not in axes_map["3121"]
    assert axes_map["3116"] == ["MOBILITY_DASH"]
    assert axes_map["2077"] == ["TANK_HP"]

    # 3. reconcile_match_build_plan_with_trace: empty-lock no false override & >=800G component lock protection
    lock_empty, plan_empty, trace_empty = reconcile_match_build_plan_with_trace(
        store,
        my_items=("6655", "1058"),  # Just completed prev_lock 6655, holding 1200G Rod
        committed_plan=["6655", "3089", "3157"],
        locked_next_item="6655",
        proposed_ids=["3089", "3157", "3135"],
        allow_overhaul=False,
        hero_id="99",
    )
    assert lock_empty == "3089"
    assert trace_empty["overridden"] is False

    # Non-transformative allow_overhaul MUST protect >=800G component invested in cur_lock (3089 via 1058=1200G)
    lock_prot, plan_prot, trace_prot = reconcile_match_build_plan_with_trace(
        store,
        my_items=("1058",),
        committed_plan=["3089", "3157"],
        locked_next_item="3089",
        proposed_ids=["2510", "3157", "3135"],
        allow_overhaul=True,
        transformative_overhaul=False,
        hero_id="99",
    )
    assert lock_prot == "3089"
    assert plan_prot[0] == "3089"
    assert trace_prot["overridden"] is False

    # Genuine transformative_overhaul=True MAY pivot slot 1 away from cur_lock
    lock_tx, plan_tx, trace_tx = reconcile_match_build_plan_with_trace(
        store,
        my_items=("1058",),
        committed_plan=["3089", "3157"],
        locked_next_item="3089",
        proposed_ids=["2510", "3157", "3135"],
        allow_overhaul=True,
        transformative_overhaul=True,
        hero_id="99",
    )
    assert lock_tx == "2510"
    assert plan_tx[0] == "2510"
    assert trace_tx["overridden"] is True

    # 4. 1238 Round-3 gamble boost & 1353 Round-4 / gt>=720s late-stacker discount (zero double-discount)
    assert "1238" in INSTANT_PRISMATIC_TRANSMUTE_IDS
    assert "1353" in LATE_STACKER_AUGMENT_DISCOUNTS
    v_r3 = evaluate_picker_combos(
        store,
        "44",
        selected_augments=["1001", "1002"],  # Round 3
        my_items=("3084",),
        offered=[(0, store.get_augment("1238")), (1, store.get_augment("1004")), (2, store.get_augment("2077"))],
        game_time_s=420.0,
    )
    c_1238 = next(c for c in v_r3.cards if c.augment_id == "1238")
    assert c_1238.verdict == "gamble"
    assert c_1238.combo_delta == pytest.approx(4.2)

    v_r4 = evaluate_picker_combos(
        store,
        "44",
        selected_augments=["1001", "1002", "1004"],  # Round 4
        my_items=("3084",),
        offered=[(0, store.get_augment("1353")), (1, store.get_augment("2077"))],
        game_time_s=780.0,
    )
    c_1353 = next(c for c in v_r4.cards if c.augment_id == "1353")
    assert c_1353.verdict == "round_decay"
    assert c_1353.verdict_label == "末轮叠层折价"
    assert c_1353.combo_delta == pytest.approx(-8.5)

    # Verify rank_picker_cards does NOT double-discount when combo_by_slot already applied "round_decay"
    sc_1353 = AugmentScore(
        augment_id="1353",
        name="坦克引擎",
        rarity="silver",
        tier="S",
        raw_win_rate=0.55,
        smoothed_win_rate=0.55,
        delta=0.03,
        games=5000,
        low_sample=False,
    )
    sc_2077 = AugmentScore(
        augment_id="2077",
        name="贪欲束缚",
        rarity="gold",
        tier="A",
        raw_win_rate=0.53,
        smoothed_win_rate=0.53,
        delta=0.01,
        games=5000,
        low_sample=False,
    )
    combo_map = {c.slot_index: c for c in v_r4.cards}
    ranked, _ = rank_picker_cards([(0, sc_1353), (1, sc_2077)], combo_by_slot=combo_map, game_time_s=780.0, round_index=4)
    dec_1353 = next(d for d in ranked if d.slot_index == 0)
    assert dec_1353.combo_shift == pytest.approx(-8.5)  # Single calibrated discount, NOT doubled!

    # 5. 4011 花晓之剑 in special_augment_builds.json["3163"] AND DataStore.get_item_info fallback
    sp_json = json.loads((REPO_ROOT / "src" / "hex_helper" / "assets" / "special_augment_builds.json").read_text(encoding="utf-8"))
    entry_3163 = next(e for e in sp_json["archetypes"] if str(e.get("id")) == "3163")
    pool_3163 = [str(x) for x in (entry_3163.get("core_items") or [])] + [str(x) for x in (entry_3163.get("force_items") or [])]
    assert "4011" in pool_3163
    info_4011 = store.get_item_info("4011")
    assert info_4011.category == "complete_equipment"


# ===========================================================================
# WS-5 & WS-6: 2K Scale, 4px Drag Deadzone, 128px Anti-Strand Snap, Elastic Height, Close Order, Timers
# ===========================================================================


def test_ws5_ws6_overlay_ergonomics_snap_deadzone_elastic_close_and_timers(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_rich_store(tmp_path)

    # 1. 2K 1.24x auto scale & default drawer_y_ratio=0.16
    assert compute_auto_overlay_scale(2560, 1440, 1.0) == pytest.approx(1.24, abs=0.02)
    assert AppConfig().drawer_y_ratio == pytest.approx(0.16)

    # 2. 128px right-snap tolerance on 2560x1440 & anti-strand collapse
    class _FakeScreen2K:
        def geometry(self) -> QRect:
            return QRect(0, 0, 2560, 1440)

        def devicePixelRatio(self) -> float:  # noqa: N802
            return 1.0

    fake_2k = _FakeScreen2K()
    monkeypatch.setattr(EdgeBuildDrawer, "_active_screen", lambda self: fake_2k)
    monkeypatch.setattr(QGuiApplication, "screenAt", staticmethod(lambda _pt: fake_2k))
    assert EdgeBuildDrawer._right_snap_tolerance(QRect(0, 0, 2560, 1440)) == 128

    pos_saves: list[list[int]] = []
    geom_events: list[dict[str, Any]] = []
    close_order: list[tuple[str, bool]] = []

    drawer = EdgeBuildDrawer(
        on_pos_changed=lambda p: pos_saves.append(p),
        on_geometry_changed=lambda g: geom_events.append(g),
        on_closed_by_user=lambda: close_order.append(("edge_drawer", drawer.isVisible())),
        initial_size=[440, 250],  # Intentionally shallow persisted height -> elastic height MUST grow!
    )
    combo = AugmentComboOverlay(
        on_closed_by_user=lambda: close_order.append(("combo", combo.isVisible())),
    )
    champ = ChampSelectOverlay(
        on_closed_by_user=lambda: close_order.append(("champ", champ.isVisible())),
    )
    try:
        drawer.set_expanded(True)
        drawer.show()
        drawer.refresh_match_view(store, "99", ["1001", "1002"], ("6655",), 1600.0, None, [])
        qapp.processEvents()

        # Elastic height anti-overflow: expanded drawer height MUST be >= auto_h (not clamped to 250!)
        auto_h = drawer._compute_auto_expanded_height(drawer.width(), QRect(0, 0, 2560, 1440))
        assert drawer.height() >= auto_h
        assert drawer.height() > 250

        # 4px drag deadzone: 2px jitter click MUST NOT save pos or emit drag telemetry!
        pos_saves.clear()
        geom_events.clear()
        p0_local = QPointF(120.0, 18.0)
        p0_global = QPointF(drawer.mapToGlobal(QPoint(120, 18)))
        ev_press = QMouseEvent(QEvent.Type.MouseButtonPress, p0_local, p0_global, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        ev_move_2px = QMouseEvent(QEvent.Type.MouseMove, QPointF(122.0, 18.0), QPointF(p0_global.x() + 2, p0_global.y()), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        ev_rel = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(122.0, 18.0), QPointF(p0_global.x() + 2, p0_global.y()), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        drawer.mousePressEvent(ev_press)
        drawer.mouseMoveEvent(ev_move_2px)
        drawer.mouseReleaseEvent(ev_rel)
        assert pos_saves == []
        assert not any(g.get("action") == "drag" for g in geom_events)

        # Real 12px drag DOES commit pos and emit drag telemetry
        ev_press2 = QMouseEvent(QEvent.Type.MouseButtonPress, p0_local, p0_global, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        ev_move_12px = QMouseEvent(QEvent.Type.MouseMove, QPointF(108.0, 18.0), QPointF(p0_global.x() - 12, p0_global.y()), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        ev_rel2 = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(108.0, 18.0), QPointF(p0_global.x() - 12, p0_global.y()), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        drawer.mousePressEvent(ev_press2)
        drawer.mouseMoveEvent(ev_move_12px)
        drawer.mouseReleaseEvent(ev_rel2)
        assert len(pos_saves) == 1
        assert any(g.get("action") == "drag" for g in geom_events)

        # Anti-strand collapse: un-anchored expanded drawer sitting in right >=65% snaps right on collapse!
        drawer._anchor_right = False
        drawer.move(1700, 240)  # right edge ~2140 -> 83.6% >= 65%
        drawer.set_expanded(False)
        qapp.processEvents()
        assert drawer._anchor_right is True
        assert drawer.x() + drawer.width() >= 2559

        # WS-6 Timers: opening brief auto-collapse 20s & compact at gt>=90s; quick-confirm 15s/35s
        assert drawer.BRIEF_AUTO_COLLAPSE_MS == 20_000
        assert drawer.QUICK_CONFIRM_HIGH_CONF_MS == 15_000
        assert drawer.QUICK_CONFIRM_LOW_CONF_MS == 35_000

        brief_adv = CoachAdvicePayload(
            request_id="req-brief-ws6",
            opening_brief={
                "enemy_threat": "敌方双刺客强切",
                "augment_goal": "追珠光护手核爆",
                "opening_buy": "遗失的章节出门",
            },
            tactical_summary="抱团先手控制",
        )
        drawer.set_coach_advice(brief_adv, allow_auto_expand=True)
        qapp.processEvents()
        assert drawer.is_expanded is True
        assert drawer._brief_auto_collapse_timer.isActive() is True
        assert drawer._brief_auto_collapse_timer.interval() == 20_000

        # gt >= 90s compacts opening brief to single line
        drawer.set_live_vital_state(is_dead=False, game_time_s=95.0)
        assert drawer._opening_brief_compacted is True

        # High confidence (>=0.90) quick-confirm starts 15s timer; low confidence starts 35s timer
        offered = [store.get_augment("1001"), store.get_augment("1002"), store.get_augment("1004")]
        drawer.set_recent_offered([a for a in offered if a is not None], auto_committed_id="1001", confidence=0.98)
        assert drawer._quick_confirm_timer.isActive() is True
        assert drawer._quick_confirm_timer.interval() == 15_000

        drawer.set_recent_offered([a for a in offered if a is not None], auto_committed_id="1001", confidence=0.65)
        assert drawer._quick_confirm_timer.isActive() is True
        assert drawer._quick_confirm_timer.interval() == 35_000

        # _close_by_user() MUST invoke callback BEFORE hide() on all 3 overlays!
        combo.show()
        champ.show()
        qapp.processEvents()
        drawer._close_by_user()
        combo._close_by_user()
        champ._close_by_user()
        assert close_order == [("edge_drawer", True), ("combo", True), ("champ", True)]
        assert drawer.isVisible() is False
        assert combo.isVisible() is False
        assert champ.isVisible() is False
    finally:
        drawer.close()
        combo.close()
        champ.close()
        qapp.processEvents()


# ===========================================================================
# WS-9: MatchRecapCard v2 Rendering + Commercial Zero-DevLab Isolation
# ===========================================================================


def test_ws9_match_recap_card_v2_and_commercial_zero_devlab_isolation(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. WS-9 MatchRecapCard v2 canvas (760x480), rating, stats, EOG telemetry, 4 icons, zero AI highlight, zero clipping
    assert CARD_DEFAULT_WIDTH == 760 and CARD_DEFAULT_HEIGHT == 480
    score, grade, badge = compute_match_rating("15/4/27", won=1, damage_share=0.35, damage_taken_share=0.21, kp=0.81, gold_per_min=1120.0)
    assert score >= 12.8 and grade == "S+" and badge == "MVP"

    stats = normalize_final_stats({"attackDamage": 495, "abilityPower": 120, "maxHealth": 8400, "abilityHaste": 148, "armor": 265, "magicResist": 195, "attackSpeed": 2.42})
    assert set(stats.keys()) == {"ad", "ap", "hp", "ah", "armor", "mr", "as"}

    def _mk_png(name: str, hex_c: str) -> str:
        p = tmp_path / f"{name}.png"
        im = QImage(32, 32, QImage.Format.Format_ARGB32)
        im.fill(QColor(hex_c))
        assert im.save(str(p), "PNG")
        return str(p)

    aug_paths = tuple(_mk_png(f"a{i}", c) for i, c in enumerate(("#D946EF", "#C8AA6E", "#0AC8B9", "#94A3B8")))
    item_entries = tuple((nm, _mk_png(f"it{i}", "#2563EB")) for i, nm in enumerate(("黑色切割者", "破败王者之刃", "死亡之舞", "心之钢", "多米尼克领主的致意", "铁板靴")))

    d1 = MatchRecapData(
        hero_id="6",
        hero_name="厄加特",
        won=1,
        kda="15/4/27",
        duration_s=1020,
        team_kills="52:38",
        augments=(("双刀流", "prismatic"), ("贪欲束缚", "gold"), ("回归基本功", "gold"), ("水果化", "silver")),
        augment_icon_paths=aug_paths,
        item_entries=item_entries,
        final_stats=stats,
        damage_dealt=64200,
        damage_share=0.35,
        damage_taken=58900,
        heal_and_shield=16200,
        gold_earned=19800,
        kill_participation=0.81,
        match_score=score,
        rating_grade=grade,
        mvp_badge=badge,
        ai_highlight="",
    )
    d2 = MatchRecapData(**{**d1.__dict__, "ai_highlight": "AI 副驾高光不应出现"})
    im1 = render_match_recap_image(d1)
    im2 = render_match_recap_image(d2)
    assert im1.width() == 760 and im1.height() == 480
    assert im1.bits().tobytes() == im2.bits().tobytes()  # Zero AI highlight box rendered

    # Verify source layout has zero item-name clipping gap (cap_y 284 + cap_h 22 = 306 < top_y 317) & strict Hextech palette
    recap_src = (REPO_ROOT / "src" / "hex_helper" / "ui" / "match_recap_card.py").read_text(encoding="utf-8")
    assert "AI 副驾高光" not in recap_src
    assert "#FF5A1F" not in recap_src.upper()
    assert "#101113" not in recap_src.upper()

    # 2. Commercial edition ZERO static DevLab imports across ALL non-devlab source files & formal v1.5.2 version
    assert edition_mod.APP_VERSION == "1.5.2"
    src_root = REPO_ROOT / "src" / "hex_helper"
    for py_file in src_root.rglob("*.py"):
        if "devlab" in py_file.relative_to(src_root).parts:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("hex_helper.devlab"), f"Static devlab import in {py_file}"
            elif isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("hex_helper.devlab"), f"Static devlab import in {py_file}"

    monkeypatch.setattr(edition_mod, "_COMPILED_EDITION", "commercial")
    monkeypatch.setenv("HEX_EDITION", "dev")
    monkeypatch.setenv("HEX_DEVLAB", "1")
    assert edition_mod.is_commercial_edition() is True
    assert edition_mod.is_devlab_allowed() is False
    comm_cfg = AppConfig.from_dict({"devlab_enabled": True, "devlab_roi_snapshots": True, "devlab_debug_log": True})
    assert comm_cfg.devlab_enabled is False
    assert comm_cfg.devlab_roi_snapshots is False
    assert comm_cfg.devlab_debug_log is False
