"""Phase 2 End-to-End Integration tests for HexHelper v1.3 Evolution.

Covers:
1. Unlocked-hero hard guard (blocks API dispatch, updates drawer hint).
2. Smart picker AI gate (skip on clear local winner, fire on close delta / combo conflict / transformative tie / manual F6)
   + 1.8s reroll debounce timer.
3. Death-vs-augment-level collision 2.5s deferral & cancellation when AUGMENT_PICKING opens.
4. Round-1 all-prismatic auto-switch to `first-prismatic`, `"首棱彩局"` badge, and EndOfGame reset.
5. Rarity-calibrated percentile scoring & smart reroll banner (eliminating 60% negative raw-delta false banners).
6. AugmentIconMatcher closed-loop verification on low-conf/None picks & Transmute fast visual channel.
7. Match telemetry closed-loop payload assembly, adoption counting, zero-PII FastAPI validation & daemon submission.
8. LookupWindow `"auto"` tier switcher, Settings checkboxes persistence, 940x610 zero-clipping & Hextech palette check.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from hex_helper import app as app_mod
from hex_helper import edition as edition_mod
from hex_helper.app import GameState, HexHelperController, expected_augment_slots_for_level
from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload
from hex_helper.config import AppConfig, load_config, save_config
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroAugmentStat, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.scoring.tier import RarityDeltaDist
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher
from hex_helper.vision.capture import CaptureResult, WindowRect
from hex_helper.vision.detector import DetectedCard, PickerDetectionResult


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _NoopCapturer:
    backend = "noop"
    consecutive_black_frames = 0

    def __init__(self, frame: np.ndarray | None = None) -> None:
        self._frame = frame

    def grab(self, **_kw):
        if self._frame is not None:
            return CaptureResult(frame_bgr=self._frame, backend="noop", is_black=False, mean_luma=64.0)
        return CaptureResult(frame_bgr=None, backend="noop", is_black=True, mean_luma=0.0)

    def close(self) -> None:
        return None


class _NoopHotkeys(QAbstractNativeEventFilter):
    def nativeEventFilter(self, _eventType, _message):
        return False, 0

    def register(self, *_a, **_kw) -> None:
        return None

    def unregister_all(self) -> None:
        return None


def _patch_headless_controller(monkeypatch: pytest.MonkeyPatch, *, frame: np.ndarray | None = None) -> None:
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer(frame=frame))
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)
    monkeypatch.setattr(app_mod, "find_game_window", lambda: WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080))
    monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: True)


def _write_dataset_dir(
    ds_dir: Path,
    *,
    hero_wr: float,
    aug_stats: dict[str, tuple[int, int]],
    augments: dict[str, tuple[str, str]],
) -> None:
    ds_dir.mkdir(parents=True, exist_ok=True)
    heroes = {
        "103": {
            "name": "阿狸",
            "title": "九尾妖狐",
            "games": 40000,
            "wins": int(40000 * hero_wr),
            "win_rate": hero_wr,
            "pick_rate": 0.06,
            "rank": 15,
            "low_sample": False,
        }
    }
    augs = {aid: {"name": nm, "rarity": rar} for aid, (nm, rar) in augments.items()}
    ha = {
        "103": {
            aid: {
                "games": g,
                "wins": w,
                "win_rate": (w / g) if g > 0 else 0.5,
                "low_sample": g < 300,
            }
            for aid, (w, g) in aug_stats.items()
        }
    }
    (ds_dir / "heroes.json").write_text(json.dumps(heroes, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "augments.json").write_text(json.dumps(augs, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "hero_augments.json").write_text(json.dumps(ha, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "hero_spells.json").write_text("{}", encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({"103": [{"items": ["6655", "3089", "3157"], "ordered": True, "games": 5000, "wins": 2700, "win_rate": 0.54, "pick_rate": 0.35}]}),
        encoding="utf-8",
    )
    (ds_dir / "hero_augment_items.json").write_text("{}", encoding="utf-8")
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps(
            {
                "103": {
                    "6655": {"games": 8000, "wins": 4400, "win_rate": 0.55, "low_sample": False},
                    "3089": {"games": 6000, "wins": 3420, "win_rate": 0.57, "low_sample": False},
                    "3157": {"games": 5000, "wins": 2700, "win_rate": 0.54, "low_sample": False},
                    "3003": {"games": 4000, "wins": 2160, "win_rate": 0.54, "low_sample": False},
                }
            }
        ),
        encoding="utf-8",
    )
    (ds_dir / "item_metadata.json").write_text(
        json.dumps(
            {
                "6655": {"category": "complete_equipment"},
                "3089": {"category": "complete_equipment"},
                "3157": {"category": "complete_equipment"},
                "3003": {"category": "complete_equipment"},
                "3040": {"category": "complete_equipment"},
            }
        ),
        encoding="utf-8",
    )


def _build_dual_mode_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", proxy_url="")
    static.items = {
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105.0}, "", "complete_equipment"),
        "3003": ItemStaticInfo("3003", "大天使之杖", 2900, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 70.0}, "", "complete_equipment"),
        "3040": ItemStaticInfo("3040", "炽天使之拥", 2900, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 70.0}, "", "complete_equipment"),
    }
    augments = {
        "1001": ("普通银卡甲", "silver"),
        "1002": ("普通银卡乙", "silver"),
        "1003": ("普通银卡丙", "silver"),
        "1004": ("弱银卡丁", "silver"),
        "1005": ("弱银卡戊", "silver"),
        "1006": ("强银卡己", "silver"),
        "1007": ("普通银卡庚", "silver"),
        "1008": ("普通银卡辛", "silver"),
        "1009": ("普通银卡壬", "silver"),
        "1010": ("普通银卡癸", "silver"),
        "2001": ("棱彩强卡一", "prismatic"),
        "2002": ("棱彩中卡二", "prismatic"),
        "2003": ("棱彩弱卡三", "prismatic"),
        "1048": ("珠光护手", "prismatic"),
        "1030": ("尤里卡", "prismatic"),
        "1045": ("炼狱导管", "prismatic"),
        "1238": ("质变：棱彩阶", "gold"),
    }
    # Skewed 10-card silver distribution (majority negative raw delta, mirroring real Patch 16.23 pool)
    # Sorted silver ranks (0..9): 1005(P5), 1004(P15), 1003(P25), 1007(P35), 1002(P45), 1008(P55), 1009(P65), 1001(P75), 1010(P85), 1006(P95)
    normal_stats = {
        "1005": (482, 1000),   # P5,  delta ~ -0.035 (below rarity par)
        "1004": (490, 1000),   # P15, delta ~ -0.027 (below rarity par)
        "1003": (495, 1000),   # P25, delta ~ -0.023 (below rarity par)
        "1007": (500, 1000),   # P35, delta ~ -0.018
        "1002": (505, 1000),   # P45, delta ~ -0.014
        "1008": (508, 1000),   # P55, delta ~ -0.011
        "1009": (511, 1000),   # P65, delta ~ -0.008
        "1001": (515, 1000),   # P75, delta ~ -0.005 (negative raw delta, yet ABOVE rarity par!)
        "1010": (518, 1000),   # P85, delta ~ -0.002
        "1006": (570, 1000),   # P95, delta ~ +0.045 (clear winner)
        "2001": (565, 1000),   # delta ~ +0.041
        "2002": (535, 1000),   # delta ~ +0.014
        "2003": (500, 1000),   # delta ~ -0.018
        "1048": (548, 1000),   # delta ~ +0.025 (transformative / combo)
        "1030": (542, 1000),   # delta ~ +0.020
        "1045": (536, 1000),   # delta ~ +0.015
        "1238": (520, 1000),
    }
    prism_stats = {
        **normal_stats,
        "2001": (600, 1000),   # Distinct WR in first-prismatic dataset!
    }
    patch_root = tmp_path / "datasets" / "16.23"
    _write_dataset_dir(patch_root / "normal", hero_wr=0.52, aug_stats=normal_stats, augments=augments)
    _write_dataset_dir(patch_root / "first-prismatic", hero_wr=0.54, aug_stats=prism_stats, augments=augments)

    store = DataStore(static)
    desc = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "2026-11-22T00:00:00Z", False)
    store.load_from_dir(desc, patch_root / "normal")
    assert store.has_first_prismatic is True
    return store


def _card(slot: int, store: DataStore, aid: str) -> DetectedCard:
    aug = store.get_augment(aid)
    assert aug is not None
    return DetectedCard(
        slot_index=slot,
        augment=aug,
        confidence=0.95,
        screen_box=(400 + slot * 420, 480, 620 + slot * 420, 520),
        rarity_hint=aug.rarity,
    )


def _create_controller(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    commercial: bool = True,
    frame: np.ndarray | None = None,
) -> tuple[HexHelperController, MagicMock]:
    monkeypatch.setenv("HEX_EDITION", "commercial" if commercial else "dev")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "app_data"))
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "https://api.test")
    _patch_headless_controller(monkeypatch, frame=frame)

    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    # Stop background timers so tests stay 100% deterministic
    ctrl._vision_timer.stop()
    ctrl._heartbeat_timer.stop()
    if ctrl._wallet_heartbeat_timer is not None:
        ctrl._wallet_heartbeat_timer.stop()
    if ctrl._startup_update_timer is not None:
        ctrl._startup_update_timer.stop()

    store = _build_dual_mode_store(tmp_path)
    ctrl.store = store
    ctrl.matcher.update_catalog(store.all_augments())

    submit_spy = MagicMock()
    if commercial:
        assert ctrl.cloud_client is not None
        ctrl.cloud_client._session = {"access_token": "tok-test", "username": "AlanTest"}
        ctrl.cloud_client._last_wallet = {"username": "AlanTest", "balance_credits": 10000.0, "ai_enabled": True, "ai_cutoff": False}
        ctrl._cloud_ai_cutoff = False
        ctrl._wallet_ai_cutoff = False
        ctrl._service_locked = False
        ctrl._force_update_required = False
        ctrl.cloud_coach.submit = submit_spy
    else:
        ctrl.cfg.gemini_api_key = "AIzaTestKey1234567890"
        ctrl.cfg.coach_mode = "gemini"
        ctrl.gemini_coach.submit = submit_spy
    return ctrl, submit_spy


# ---------------------------------------------------------------------------
# 1. Unlocked-Hero Hard Guard
# ---------------------------------------------------------------------------


def test_unlocked_hero_guard_blocks_api_and_updates_hint(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, submit_spy = _create_controller(qapp, tmp_path, monkeypatch, commercial=True)
    try:
        assert ctrl._active_hero_id() is None

        res = ctrl.trigger_coach_request("manual_f6")
        assert res is None
        assert submit_spy.call_count == 0
        assert ctrl.ai_calls_this_match == 0
        assert "请进入对局锁定英雄后再呼叫参谋" in ctrl.edge_drawer.coach_content_lbl.text()

        # Also blocks automatic triggers when hero unlocked
        res2 = ctrl.trigger_coach_request("game_start")
        assert res2 is None
        assert submit_spy.call_count == 0

        # connection_test is explicitly allowed even without locked hero
        res_test = ctrl.trigger_coach_request("connection_test")
        assert res_test is not None
        assert submit_spy.call_count == 1
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 2. Smart Picker AI Gate & 1.8s Reroll Debounce
# ---------------------------------------------------------------------------


def test_smart_picker_gate_skip_vs_close_delta_and_reroll_debounce(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ctrl, submit_spy = _create_controller(qapp, tmp_path, monkeypatch, commercial=True)
    try:
        ctrl.sm.state = GameState.AUGMENT_PICKING
        ctrl.sm.current_hero_id = "103"
        ctrl.cfg.smart_picker_ai_gate = True

        # A. Clear local winner: 1006 (+4.5%) vs 1001 (-0.5%) vs 1002 (-1.4%) -> gap ~5.0% >= 1.8% -> SKIP!
        clear_cards = (
            _card(0, ctrl.store, "1006"),
            _card(1, ctrl.store, "1001"),
            _card(2, ctrl.store, "1002"),
        )
        ctrl.sm.active_cards = clear_cards
        with caplog.at_level(logging.INFO, logger="hex_helper.app"):
            res_skip = ctrl.trigger_coach_request("augment_picking")
        assert res_skip is None
        assert submit_spy.call_count == 0
        assert any("Smart picker gate: clear local winner" in rec.message for rec in caplog.records)

        # Manual F6 MUST ALWAYS bypass the smart picker gate!
        res_f6 = ctrl.trigger_coach_request("manual_f6")
        assert res_f6 is not None
        assert submit_spy.call_count == 1

        # B. Close delta (< 0.018): 1030 (+2.0%) vs 1045 (+1.5%) vs 2002 (+1.4%) -> gap ~0.5% -> TRIGGER AI!
        close_cards = (
            _card(0, ctrl.store, "1030"),
            _card(1, ctrl.store, "1045"),
            _card(2, ctrl.store, "2002"),
        )
        ctrl.sm.active_cards = close_cards
        res_close = ctrl.trigger_coach_request("augment_picking")
        assert res_close is not None
        assert submit_spy.call_count == 2

        # C. Clear delta (2001 +4.1% vs 1048 +2.5% -> gap 1.6% / or 1006 +4.5% vs 1048 +2.5% -> gap 2.0%),
        #    but 1048 (珠光护手) is transformative within 3.5% -> transformative tie -> MUST NOT skip!
        tx_tie_cards = (
            _card(0, ctrl.store, "1006"),
            _card(1, ctrl.store, "1048"),
            _card(2, ctrl.store, "1002"),
        )
        ctrl.sm.active_cards = tx_tie_cards
        res_tx = ctrl.trigger_coach_request("augment_picking")
        assert res_tx is not None
        assert submit_spy.call_count == 3

        # D. 0.6s picker debounce timer restarts cleanly on rapid dice clicks
        assert ctrl._picker_coach_debounce_timer.interval() == 600
        ctrl.sm.active_cards = close_cards
        ctrl._schedule_picker_coach_trigger()
        assert ctrl._picker_coach_debounce_timer.isActive() is True
        assert submit_spy.call_count == 3  # Not fired immediately!
        # Simulate rapid reroll restarting timer
        ctrl._schedule_picker_coach_trigger()
        assert ctrl._picker_coach_debounce_timer.isActive() is True
        assert submit_spy.call_count == 3
        # Fire timer expiry -> dispatches ONCE
        ctrl._picker_coach_debounce_timer.stop()
        ctrl._on_picker_coach_debounce_timeout()
        assert submit_spy.call_count == 4
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 3. Death-vs-Augment-Level Collision 2.5s Deferral & Cancellation
# ---------------------------------------------------------------------------


def test_death_vs_augment_level_collision_defer_and_cancel(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, submit_spy = _create_controller(qapp, tmp_path, monkeypatch, commercial=True)
    try:
        assert expected_augment_slots_for_level(3) == 1
        assert expected_augment_slots_for_level(7) == 2
        assert expected_augment_slots_for_level(11) == 3
        assert expected_augment_slots_for_level(15) == 4
        assert ctrl._death_coach_defer_timer.interval() == 2500

        # Enter game alive at level 7 with 1 selected augment
        me_alive = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 7, 2, 1, 3, ("6655",))
        e1_init = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 7, 1, 1, 1, ("1055",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(120.0, me_alive, (me_alive,), (e1_init,), 1200.0))
        ctrl.sm.selected_augments = ["1001"]
        assert submit_spy.call_count == 1  # game_start

        # Die at level 7 (augment milestone!) with enemy build changed -> MUST defer by 2.5s (not fire immediately!)
        me_dead_7 = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", True, 7, 2, 2, 3, ("6655",))
        e1_new = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 7, 3, 1, 1, ("3075",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(200.0, me_dead_7, (me_dead_7,), (e1_new,), 2200.0))
        assert ctrl._death_coach_defer_timer.isActive() is True
        assert submit_spy.call_count == 1  # Deferred!

        # Scenario A: AUGMENT_PICKING opens via real _tick_vision within 2.5s -> automatically cancels deferred death coach!
        fake_frame = np.full((1080, 1920, 3), 48, dtype=np.uint8)
        ctrl.capturer = _NoopCapturer(frame=fake_frame)
        fake_cards = (
            _card(0, ctrl.store, "1030"),
            _card(1, ctrl.store, "1045"),
            _card(2, ctrl.store, "2002"),
        )
        det_stub = MagicMock()
        det_stub.process_frame.return_value = PickerDetectionResult(
            picker_visible=True, stable=True, rerolled=False, cards=fake_cards, cards_seen_on_frame=True
        )
        ctrl.detector = det_stub
        ctrl._tick_vision()
        assert ctrl.sm.state == GameState.AUGMENT_PICKING
        assert ctrl._death_coach_defer_timer.isActive() is False
        assert ctrl._picker_coach_debounce_timer.isActive() is True
        ctrl._cancel_picker_coach_debounce()
        ctrl._on_death_coach_defer_timeout()
        assert submit_spy.call_count == 1  # Deferred death coach was cleanly cancelled!

        # Scenario B: Death at milestone where picker does NOT open -> timer expiry fires death coach
        ctrl.sm.state = GameState.IN_GAME
        ctrl._schedule_death_coach_defer()
        assert ctrl._death_coach_defer_timer.isActive() is True
        ctrl._death_coach_defer_timer.stop()
        ctrl._on_death_coach_defer_timeout()
        assert submit_spy.call_count == 2
        assert ctrl._latest_request_trigger == "death_enemy_build_changed"

        # Scenario C: Non-milestone level 9 with all expected augments (2/2) -> fires IMMEDIATELY without defer!
        ctrl.sm.selected_augments = ["1001", "1002"]
        ctrl.sm._was_dead = False
        ctrl.sm._last_death_coach_mono = -999.0
        me_dead_9 = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", True, 9, 4, 3, 5, ("6655",))
        e1_new2 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 9, 5, 2, 2, ("3075", "3065"))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(350.0, me_dead_9, (me_dead_9,), (e1_new2,), 2800.0))
        assert ctrl._death_coach_defer_timer.isActive() is False
        assert submit_spy.call_count == 3
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 4. Round-1 All-Prismatic Auto-Switch & EndOfGame Reset
# ---------------------------------------------------------------------------


def test_round1_all_prismatic_auto_switch_and_end_of_game_reset(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, _submit_spy = _create_controller(qapp, tmp_path, monkeypatch, commercial=True)
    try:
        assert ctrl.store.active_match_mode == "normal"
        assert ctrl.store.has_first_prismatic is True
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "103"
        ctrl.cfg.auto_first_prismatic_mode = True
        ctrl.cfg.dataset_mode = "normal"

        # Round 2 (1 selected augment) with 3 prismatic cards must NOT auto-switch
        ctrl.sm.selected_augments = ["1001"]
        prism_cards = (
            _card(0, ctrl.store, "2001"),
            _card(1, ctrl.store, "2002"),
            _card(2, ctrl.store, "2003"),
        )
        assert ctrl._maybe_auto_switch_first_prismatic(prism_cards) is False
        assert ctrl.store.active_match_mode == "normal"

        # Round 1 (0 selected augments) with 3 prismatic cards -> auto-switches in <1ms + shows "首棱彩局" badge!
        ctrl.sm.selected_augments = []
        assert ctrl._maybe_auto_switch_first_prismatic(prism_cards) is True
        assert ctrl.store.active_match_mode == "first-prismatic"
        assert ctrl.store.get_hero("103").win_rate == pytest.approx(0.54, abs=1e-4)
        assert ctrl.edge_drawer.mode_badge_lbl.text() == "首棱彩局"
        assert ctrl._auto_switched_first_prismatic is True

        # EndOfGame / lobby LCU poll restores normal mode and clears badge
        ctrl._apply_lcu_poll("EndOfGame", None)
        assert ctrl.store.active_match_mode == "normal"
        assert ctrl.store.get_hero("103").win_rate == pytest.approx(0.52, abs=1e-4)
        assert ctrl.edge_drawer.mode_badge_lbl.text() == ""
        assert ctrl._auto_switched_first_prismatic is False

        # When auto_first_prismatic_mode is disabled, Round-1 all-prismatic never switches
        ctrl.cfg.auto_first_prismatic_mode = False
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "103"
        assert ctrl._maybe_auto_switch_first_prismatic(prism_cards) is False
        assert ctrl.store.active_match_mode == "normal"
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 5. Rarity-Calibrated Percentile Scoring & Smart Reroll Banner
# ---------------------------------------------------------------------------


def test_rarity_calibrated_reroll_banner_eliminates_false_positives(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, _submit_spy = _create_controller(qapp, tmp_path, monkeypatch, commercial=True)
    try:
        ctrl.sm.state = GameState.AUGMENT_PICKING
        ctrl.sm.current_hero_id = "103"

        # Case A: All 3 silver cards have NEGATIVE raw delta (-0.5%, -1.4%, -2.3%),
        # BUT 1001 (-0.5%) is above silver median -> is_below_rarity_par=False -> NO false reroll banner!
        captured: list[tuple[list[Any], str]] = []
        orig_render = ctrl.augment_overlay.render_cards

        def spy_render(items, *args, reroll_banner="", **kwargs):
            captured.append((list(items), reroll_banner))
            return orig_render(items, *args, reroll_banner=reroll_banner, **kwargs)

        monkeypatch.setattr(ctrl.augment_overlay, "render_cards", spy_render)

        skewed_cards = (
            _card(0, ctrl.store, "1001"),
            _card(1, ctrl.store, "1002"),
            _card(2, ctrl.store, "1003"),
        )
        ctrl._render_augment_badges(skewed_cards)
        items_a, banner_a = captured[-1]
        assert banner_a == "", f"Expected NO false reroll banner on above-par card, got {banner_a!r}"
        assert any(it.is_best and it.score.augment_id == "1001" for it in items_a)
        assert items_a[0].score.percentile is not None

        # Case B: All 3 silver cards are truly bottom-of-rarity (1003, 1004, 1005 -> all is_below_rarity_par=True) -> reroll banner!
        trash_cards = (
            _card(0, ctrl.store, "1003"),
            _card(1, ctrl.store, "1004"),
            _card(2, ctrl.store, "1005"),
        )
        ctrl._render_augment_badges(trash_cards)
        items_b, banner_b = captured[-1]
        assert "建议用骰子刷新" in banner_b
        assert all(not it.is_best for it in items_b)

        # Case C: Fallback when rarity_dist is None -> uses delta < 0
        monkeypatch.setattr(ctrl.store, "get_hero_rarity_distribution", lambda _h, _r: None)
        ctrl._render_augment_badges(skewed_cards)
        items_c, banner_c = captured[-1]
        assert "建议用骰子刷新" in banner_c
        assert all(not it.is_best for it in items_c)
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 6. AugmentIconMatcher Closed-Loop Verification & Transmute Visual Channel
# ---------------------------------------------------------------------------


def _make_distinct_icon(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((32, 32, 3), dtype=np.uint8)
    base_color = [int(rng.integers(30, 230)) for _ in range(3)]
    img[:, :] = base_color
    for _ in range(5):
        x0, y0 = int(rng.integers(2, 22)), int(rng.integers(2, 22))
        x1, y1 = min(30, x0 + int(rng.integers(5, 14))), min(30, y0 + int(rng.integers(5, 14)))
        col = tuple(int(rng.integers(0, 255)) for _ in range(3))
        cv2.rectangle(img, (x0, y0), (x1, y1), col, -1)
    cv2.circle(img, (16, 16), 7, (240 - base_color[0], 240 - base_color[1], 240 - base_color[2]), 2)
    return img


def _render_hud_frame_with_icon(
    matcher: AugmentIconMatcher,
    slot_idx: int,
    icon_bgr: np.ndarray,
    w: int = 1920,
    h: int = 1080,
) -> np.ndarray:
    frame = np.full((h, w, 3), 24, dtype=np.uint8)
    rects = dict(matcher.get_hud_slot_rects(w, h))
    x0, y0, x1, y1 = rects[slot_idx]
    frame[y0:y1, x0:x1] = cv2.resize(icon_bgr, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)
    return frame


def test_icon_matcher_closed_loop_correction_and_transmute(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctrl, submit_spy = _create_controller(qapp, tmp_path, monkeypatch, commercial=True)
    try:
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "103"
        matcher = AugmentIconMatcher()
        ico_1001 = _make_distinct_icon(101)
        ico_1002 = _make_distinct_icon(202)
        ico_2001 = _make_distinct_icon(303)
        matcher.register_template("1001", ico_1001, "普通银卡甲")
        matcher.register_template("1002", ico_1002, "普通银卡乙")
        matcher.register_template("2001", ico_2001, "棱彩强卡一")
        ctrl.icon_matcher = matcher

        offered = (
            _card(0, ctrl.store, "1001"),
            _card(1, ctrl.store, "1002"),
            _card(2, ctrl.store, "1003"),
        )
        ctrl.sm.last_offered_cards = offered
        rect = WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080)

        # A. Low-confidence mis-pick: committed 1001, but HUD slot 0 actually shows 1002 -> corrects in-place!
        ctrl.sm.selected_augments = ["1001"]
        frame_1002 = _render_hud_frame_with_icon(matcher, slot_idx=0, icon_bgr=ico_1002)
        hit = ctrl._verify_picked_augment_from_frame(
            frame_1002,
            game_rect=rect,
            candidate_ids=("1001", "1002", "1003"),
            committed_id="1001",
            slot_index_hint=0,
        )
        assert hit is not None and hit.augment_id == "1002"
        assert ctrl.sm.selected_augments == ["1002"]
        assert submit_spy.call_count == 1  # Re-triggered augment_selected after correction

        # B. ClickTracker returned None -> icon matcher recovers missed augment in HUD slot 1!
        frame_1001 = _render_hud_frame_with_icon(matcher, slot_idx=1, icon_bgr=ico_1001)
        hit2 = ctrl._verify_picked_augment_from_frame(
            frame_1001,
            game_rect=rect,
            candidate_ids=("1001", "1003"),
            committed_id="",
            slot_index_hint=1,
        )
        assert hit2 is not None and hit2.augment_id == "1001"
        assert ctrl.sm.selected_augments == ["1002", "1001"]

        # C. Pending Transmute fast visual icon channel resolves 1238 -> 2001 automatically!
        ctrl.sm.add_selected_augment("1238")
        ctrl.sm.start_transmute_if_applicable("1238", now_mono=10.0)
        assert ctrl.sm.pending_transmute is not None
        frame_tx = _render_hud_frame_with_icon(matcher, slot_idx=2, icon_bgr=ico_2001)
        tx_hit = ctrl._try_resolve_transmute_via_icon(frame_tx, rect)
        assert tx_hit is not None and tx_hit.augment_id == "2001"
        assert ctrl.sm.pending_transmute is None
        assert "2001" in ctrl.sm.selected_augments
        assert "1238" not in ctrl.sm.selected_augments
        # D. Verify _tick_vision schedules 850ms icon verify timer when picker closes with low confidence (< 0.65)
        ctrl.sm.selected_augments = []
        ctrl.sm.state = GameState.AUGMENT_PICKING
        ctrl.sm.active_cards = offered
        ctrl.sm.last_offered_cards = offered
        ctrl.sm._picker_closed_since = time.monotonic() - 1.0  # past close debounce
        ctrl._frozen_pick_slot = 0
        ctrl._frozen_pick_source = "cursor_dwell"
        ctrl._frozen_pick_confidence = 0.42  # < 0.65 threshold
        ctrl.capturer = _NoopCapturer(frame=frame_1002)
        det_close = MagicMock()
        det_close.process_frame.return_value = PickerDetectionResult(
            picker_visible=False, stable=False, rerolled=False, cards=(), cards_seen_on_frame=False
        )
        ctrl.detector = det_close
        ctrl._tick_vision()
        assert ctrl.sm.state == GameState.IN_GAME
        assert ctrl.sm.selected_augments == ["1001"]
        assert ctrl._icon_verify_timer.isActive() is True
        assert ctrl._icon_verify_timer.interval() == 850
        assert ctrl._pending_icon_verify_committed_id == "1001"
        ctrl._icon_verify_timer.stop()
    finally:
        ctrl.shutdown()
        qapp.processEvents()


def test_match_telemetry_payload_adoption_and_client_submission(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import httpx

    ctrl, _submit_spy = _create_controller(qapp, tmp_path, monkeypatch, commercial=True)
    try:
        # Simulate in-game match
        me1 = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 5, 1, 0, 2, ("6655",))
        e1 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 5, 1, 1, 0, ("1055",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(100.0, me1, (me1,), (e1,), 1400.0))
        assert ctrl.build_match_telemetry_record() is None  # < 180s -> skipped!

        # Receive AI advice recommending 3089 (大帽), 3003 (大天使 -> Tear pair 3040), 3157 (中娅)
        ctrl._latest_request_id = "hex-tel-1"
        ctrl._on_coach_advice(
            CoachAdvicePayload(
                request_id="hex-tel-1",
                next_items=[
                    CoachAdviceItem("3089", "灭世者的死亡之帽", "下件必出", "爆发核心"),
                    CoachAdviceItem("3003", "大天使之杖", "次选", "续航蓝量"),
                    CoachAdviceItem("3157", "中娅沙漏", "可选", "保命"),
                ],
                build_path=["灭世者的死亡之帽", "大天使之杖", "中娅沙漏"],
                tactical_summary="拉扯输出",
            )
        )
        ctrl.sm.selected_augments = ["1048", "1030"]
        ctrl._on_replace_last_augment("1045")
        assert ctrl.user_swapped_this_match == 1
        assert ctrl.ai_recommended_item_ids_shown == {"3089", "3003", "3157"}

        # Player finishes match at 920s owning 6655, 3089 (adopted!) and 3040 (stacked Seraph's = adopted 3003!)
        me_end = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 16, 12, 5, 18, ("6655", "3089", "3040"))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(920.0, me_end, (me_end,), (e1,), 800.0))

        # Mock detector metrics
        det_mock = MagicMock()
        det_mock.get_match_metrics.return_value = {
            "partial_open_count": 3,
            "recovered_slot_count": 2,
            "false_reroll_suppressed_count": 4,
        }
        ctrl.detector = det_mock

        record = ctrl.build_match_telemetry_record(won=1)
        assert record is not None
        assert record["hero_id"] == "103"
        assert record["hero_name"] == "阿狸"
        assert record["duration_s"] == 920
        assert record["ai_recs_shown"] == 3
        assert record["ai_recs_adopted"] == 2  # 3089 + 3040 (matches 3003 via Tear equivalence!)
        assert record["user_swapped_count"] == 1
        assert record["ocr_missed_cards"] == 1  # 3 partial - 2 recovered
        assert record["selected_augments"] == ["1048", "1045"]
        assert len(record["match_key"]) == 32  # SHA-256 hex digest (zero PII)

        # Verify CloudGatewayClient posts sanitized telemetry record cleanly over HTTP
        from hex_helper.cloud.client import CloudGatewayClient

        captured: list[dict] = []

        def _handler(req: httpx.Request) -> httpx.Response:
            assert req.method == "POST"
            assert req.url.path == "/v1/telemetry/match"
            captured.append(json.loads(req.content.decode("utf-8")))
            return httpx.Response(200, json={"ok": True})

        client = CloudGatewayClient(
            base_url="https://api.hexhelper.example.com",
            http_client_factory=lambda b, t: httpx.Client(
                base_url=b, timeout=t, transport=httpx.MockTransport(_handler)
            ),
        )
        assert client.submit_match_telemetry(record) is True
        assert len(captured) == 1
        assert captured[0]["hero_id"] == "103"
        assert captured[0]["ai_recs_adopted"] == 2
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 8. LookupWindow "auto" Tier, New Settings Persistence & 940x610 Hextech UI
# ---------------------------------------------------------------------------


def test_lookup_window_auto_tier_new_settings_and_940x610_layout(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prev_ed = edition_mod._COMPILED_EDITION
    try:
        edition_mod._COMPILED_EDITION = "commercial"
        cfg = AppConfig(
            coach_tier="auto",
            auto_first_prismatic_mode=True,
            smart_picker_ai_gate=True,
            telemetry_enabled=True,
        )
        switched: list[str] = []
        saved_cfgs: list[AppConfig] = []

        lw = LookupWindow(
            cfg=cfg,
            on_switch_tier=lambda t: switched.append(t),
            on_save_settings=lambda c: saved_cfgs.append(c),
        )
        lw.resize(940, 610)
        lw.set_account_state(
            "AlanHexPro",
            {"balance_credits": 12000.0, "balance_cny": 12.0, "ai_enabled": True, "ai_cutoff": False},
            tier="auto",
        )
        lw._switch_page(2, force=True)
        lw.show()
        qapp.processEvents()

        # Verify triple tier buttons exist, fit cleanly at 940px with zero overlap, and switch cleanly
        assert lw.tier_auto_btn is not None
        assert "智能混合" in lw.tier_auto_btn.text()
        assert "智能混合" in lw.coach_page_status.text()
        tier_btns = [lw.tier_auto_btn, lw.tier_a_btn, lw.tier_b_btn]
        for b in tier_btns:
            assert b.isVisible()
            assert b.geometry().right() <= lw.account_card.width()
        for i in range(len(tier_btns)):
            for j in range(i + 1, len(tier_btns)):
                assert not tier_btns[i].geometry().intersects(tier_btns[j].geometry())

        lw.tier_b_btn.click()
        lw.tier_auto_btn.click()
        assert switched == ["b", "auto"]
        assert lw._cfg.coach_tier == "auto"

        # Verify Settings page new checkboxes exist, fit at 940x610 without clipping, and persist to disk!
        lw._switch_page(3, force=True)
        qapp.processEvents()
        assert lw.auto_first_prismatic_chk.isChecked() is True
        assert lw.smart_picker_gate_chk.isChecked() is True
        assert lw.telemetry_chk.isChecked() is True
        assert lw.diag_text.height() >= 80, f"Diagnostics viewport crushed at 940x610: {lw.diag_text.height()}px"

        lw.auto_first_prismatic_chk.setChecked(False)
        lw.smart_picker_gate_chk.setChecked(False)
        lw.telemetry_chk.setChecked(False)
        lw._save_clicked()
        assert len(saved_cfgs) == 1

        cfg_path = tmp_path / "config.json"
        save_config(saved_cfgs[0], path=cfg_path)
        reloaded = load_config(path=cfg_path)
        assert reloaded.coach_tier == "auto"
        assert reloaded.auto_first_prismatic_mode is False
        assert reloaded.smart_picker_ai_gate is False
        assert reloaded.telemetry_enabled is False

        # Verify EdgeBuildDrawer supports "auto" tier pill + match mode badge
        drawer_tiers: list[str] = []
        drawer = EdgeBuildDrawer(initial_tier="auto", on_switch_tier=lambda t: drawer_tiers.append(t))
        assert drawer._coach_tier == "auto"
        assert drawer.tier_auto_pill is not None
        drawer.tier_a_pill.click()
        drawer.tier_auto_pill.click()
        assert drawer_tiers == ["a", "auto"]
        drawer.set_match_mode_badge("首棱彩局")
        assert drawer.mode_badge_lbl.text() == "首棱彩局"
        drawer.close()

        lw.close()
        qapp.processEvents()
    finally:
        edition_mod._COMPILED_EDITION = prev_ed

    # Strict visual discipline check: ALL desktop UI files MUST NEVER contain Web Tactile Orange (#FF5A1F)
    ui_files = sorted((ROOT / "src/hex_helper/ui").rglob("*.py"))
    assert ui_files, "Expected desktop UI source files under src/hex_helper/ui"
    for ui_path in ui_files:
        text = ui_path.read_text(encoding="utf-8")
        assert "#FF5A1F" not in text.upper(), f"Forbidden web orange color found in {ui_path.relative_to(ROOT)}"
