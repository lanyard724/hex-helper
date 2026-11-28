"""Phase 2 integration tests covering P0-1, P0-2, P0-4, P0-5, P1 tuning/async/death attribution, and Worker E skin phantom."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from hex_helper import app as app_mod
from hex_helper.app import MAX_AUGMENT_SLOTS, GameState, HexHelperController, HexStateMachine
from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload
from hex_helper.config import AppConfig, load_config, save_config
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroAugmentStat, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.action_tracker import MatchActionTracker
from hex_helper.game.models import ChampSelectState, LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.scoring.augment_combo import ComboPlan, PickerComboVerdict
from hex_helper.ui.match_recap_card import MatchRecapDialog
from hex_helper.vision.capture import CaptureResult, WindowRect
from hex_helper.vision.detector import DetectedCard, PickerDetectionResult


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

    def flush_and_close(self) -> None:
        return None


class _NoopHotkeys(QAbstractNativeEventFilter):
    last_failed_hotkeys: dict[str, int] = {}

    def nativeEventFilter(self, _eventType: Any, _message: Any) -> tuple[bool, int]:
        return False, 0

    def register(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def unregister_all(self) -> None:
        return None


class _FakeDevLabRecorder:
    enable_roi_snapshots = True
    enable_debug_log = False
    active = True

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.rois: list[tuple[str, tuple[int, ...], int]] = []
        self.meta_updates: list[dict[str, Any]] = []
        self.begun_matches: list[dict[str, Any]] = []

    def begin_match(self, **kwargs: Any) -> str:
        self.begun_matches.append(dict(kwargs))
        return "sess-1"

    def begin_champ_select(self, **_kwargs: Any) -> None:
        return None

    def update_match_meta(self, **kwargs: Any) -> None:
        self.meta_updates.append(dict(kwargs))

    def set_context(self, **_kwargs: Any) -> None:
        return None

    def emit(self, category: str, event_type: str, payload: dict[str, Any] | None = None, **_kw: Any) -> int:
        clean = dict(payload or {})
        json.dumps(clean, ensure_ascii=False)
        self.events.append((category, event_type, clean))
        return len(self.events)

    def enqueue_roi_snapshot(self, tag: str, bgr_crop: Any, ev_seq: int = 0) -> str | None:
        if len([r for r in self.rois if r[0] == tag]) >= 5:
            return None
        self.rois.append((tag, tuple(bgr_crop.shape), ev_seq))
        return f"roi/{tag}.png"

    def record_latency_sample(self, *_a: Any, **_kw: Any) -> None:
        return None

    def flush_latency_summary(self, **_kw: Any) -> None:
        return None

    def end_match(self, **_kw: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def flush_and_close(self) -> None:
        return None


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", fetcher=lambda _u: b"")
    static.items = {
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105.0}, "", "complete_equipment"),
        "3071": ItemStaticInfo("3071", "黑色切割者", 3000, ("Damage", "Health"), {"FlatPhysicalDamageMod": 40.0}, "", "complete_equipment"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "1058": ItemStaticInfo("1058", "无用大棒", 1200, ("SpellDamage",), {"FlatMagicDamageMod": 65.0}, "", "other"),
    }
    store = DataStore(static)
    store._descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "99": HeroStat("99", "拉克丝", "Lux", 80000, 41600, 0.52, 0.08, 15, False),
    }
    store._core_builds = {
        "99": [CoreBuild(("6655", "3089", "3157"), True, 12000, 6240, 0.52, 0.42)],
    }
    augs = {
        "1001": AugmentInfo("1001", "珠光护手", "prismatic", "技能可暴击"),
        "1002": AugmentInfo("1002", "尤里卡", "prismatic", "法强转急速"),
        "1004": AugmentInfo("1004", "回归基本功", "gold", "技能伤害提升"),
        "1238": AugmentInfo("1238", "质变：棱彩阶", "gold", "立刻获得一个随机棱彩强化符文"),
        "2001": AugmentInfo("2001", "炼狱导管", "prismatic", "技能附带持续灼烧"),
        "2002": AugmentInfo("2002", "秘法冲拳", "silver", "普攻减冷却"),
        "2003": AugmentInfo("2003", "巫师思维", "gold", "法强提升"),
        "2004": AugmentInfo("2004", "小丑学院", "gold", "阵亡爆炸"),
        "2005": AugmentInfo("2005", "冰寒", "silver", "减速强化"),
        "2006": AugmentInfo("2006", "全能龙魂", "prismatic", "获得龙魂"),
        "3170": AugmentInfo("3170", "重型", "silver", "活动奖励重型"),
        "3171": AugmentInfo("3171", "力量", "silver", "活动奖励力量"),
        "3172": AugmentInfo("3172", "鞭挞", "silver", "活动奖励鞭挞"),
    }
    store._augments = augs
    store._hero_augments = {
        "99": {
            aid: HeroAugmentStat(aid, 4000, 2160, 0.54, False)
            for aid in augs
        }
    }
    return store


def _make_card(store: DataStore, slot: int, aid: str) -> DetectedCard:
    aug = store.get_augment(aid)
    assert aug is not None
    x0 = 320 + slot * 420
    return DetectedCard(
        slot_index=slot,
        augment=aug,
        confidence=0.96,
        screen_box=(x0, 260, x0 + 300, 760),
        rarity_hint=aug.rarity,
    )


def _patch_headless_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)
    monkeypatch.setattr(
        app_mod,
        "find_game_window",
        lambda: WindowRect(hwnd=1001, left=0, top=0, width=1920, height=1080),
    )
    monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: True)


def _cleanup_controller(ctrl: HexHelperController, qapp: QApplication) -> None:
    dlg = getattr(ctrl, "_active_recap_dialog", None)
    if dlg is not None:
        try:
            dlg.close()
        except Exception:
            pass
    for attr in ("edge_drawer", "combo_overlay", "champ_overlay", "augment_overlay", "lookup_window"):
        w = getattr(ctrl, attr, None)
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
    ctrl.shutdown()
    qapp.processEvents()


def test_p0_1_screen_recording_toggle_anti_unfocused_text_input_and_restore_focus(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_p01"))
    _patch_headless_controller(monkeypatch)
    fg_restores: list[int] = []
    monkeypatch.setattr(app_mod, "restore_foreground_window", lambda hwnd: fg_restores.append(int(hwnd)) or True)
    overlay_fg_calls: list[tuple[int, tuple[int, ...]]] = []
    overlay_fg_state = {"val": False}

    def _fake_overlay_fg(hwnd: int, *, exclude_hwnds: Any = ()) -> bool:
        overlay_fg_calls.append((int(hwnd), tuple(sorted(int(x) for x in exclude_hwnds))))
        return overlay_fg_state["val"]

    monkeypatch.setattr(app_mod, "is_game_or_overlay_foreground", _fake_overlay_fg)

    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store

        # Hidden widgets must never allocate native HWNDs in _non_overlay_hwnds
        assert ctrl._non_overlay_hwnds() == set()

        # Toggle from EdgeBuildDrawer right-click menu handler persists cfg and syncs all 4 overlays + LookupWindow
        ctrl.edge_drawer._handle_toggle_screen_recording(True)
        assert ctrl.cfg.allow_screen_recording_overlays is True
        assert load_config().allow_screen_recording_overlays is True
        assert ctrl.edge_drawer._allow_screen_recording is True
        assert ctrl.combo_overlay._allow_screen_recording is True
        assert ctrl.augment_overlay._allow_screen_recording is True
        assert ctrl.champ_overlay._allow_screen_recording is True
        assert ctrl.lookup_window.screen_recording_chk.isChecked() is True

        # Saving settings from LookupWindow reverses toggle back to False
        new_cfg = AppConfig.from_dict(ctrl.cfg.to_dict())
        new_cfg.allow_screen_recording_overlays = False
        ctrl._on_save_settings(new_cfg)
        assert ctrl.cfg.allow_screen_recording_overlays is False
        assert ctrl.edge_drawer._allow_screen_recording is False
        assert ctrl.combo_overlay._allow_screen_recording is False
        assert ctrl.augment_overlay._allow_screen_recording is False
        assert ctrl.champ_overlay._allow_screen_recording is False

        # Request focus restore from EdgeBuildDrawer routes cleanly to game HWND 1001
        ctrl.edge_drawer._on_request_restore_game_focus()
        assert fg_restores == [1001]

        # Enter match with game focused
        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 0, 1, ("3071",))
        me = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 6, 1, 0, 1, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(90.0, me, (me,), (enemy,), 1200.0))
        ctrl.edge_drawer.show_overlay()
        ctrl.combo_overlay.show_overlay()

        # When game HWND loses foreground ONLY because overlay/drawer has focus -> do NOT hide!
        monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: False)
        overlay_fg_state["val"] = True
        ctrl._apply_live_snapshot(LiveMatchSnapshot(92.0, me, (me,), (enemy,), 1250.0))
        assert ctrl.edge_drawer.isVisible() is True
        assert ctrl.combo_overlay.isVisible() is True

        # When neither game nor overlays are foreground (e.g. LookupWindow shown) -> excluded HWND passed,
        # _text_input_active protects drawer & combo_overlay while active, then hides once False!
        ctrl.lookup_window.show()
        ctrl.edge_drawer._text_input_active = True
        overlay_fg_state["val"] = False
        ctrl._apply_live_snapshot(LiveMatchSnapshot(94.0, me, (me,), (enemy,), 1300.0))
        assert ctrl.edge_drawer.isVisible() is True
        assert ctrl.combo_overlay.isVisible() is True
        assert any(int(ctrl.lookup_window.internalWinId()) in ex for _h, ex in overlay_fg_calls if ctrl.lookup_window.internalWinId())

        ctrl.edge_drawer._text_input_active = False
        ctrl._apply_live_snapshot(LiveMatchSnapshot(96.0, me, (me,), (enemy,), 1350.0))
        assert ctrl.edge_drawer.isVisible() is False
        assert ctrl.combo_overlay.isVisible() is False
    finally:
        _cleanup_controller(ctrl, qapp)


def test_p0_2_back_to_back_disjoint_pickers_commit_and_recent_pool(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_p02"))
    _patch_headless_controller(monkeypatch)

    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        ctrl.cfg.auto_trigger_coach = True
        ctrl.cfg.smart_picker_ai_gate = False
        devlab = _FakeDevLabRecorder()
        ctrl._devlab = devlab
        ctrl._devlab_active = True

        monkeypatch.setattr(ctrl, "_is_any_coach_online", lambda: True)
        triggered: list[str] = []
        monkeypatch.setattr(ctrl, "trigger_coach_request", lambda reason, **_kw: triggered.append(reason))
        flashed: list[str] = []
        monkeypatch.setattr(ctrl.edge_drawer, "flash_new_augment", lambda aid: flashed.append(aid))

        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 0, 1, ("3071",))
        me = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 6, 1, 0, 1, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(45.0, me, (me,), (enemy,), 900.0))
        triggered.clear()

        rect = WindowRect(hwnd=1001, left=0, top=0, width=1920, height=1080)
        round_a = (_make_card(ctrl.store, 0, "1001"), _make_card(ctrl.store, 1, "1002"), _make_card(ctrl.store, 2, "1004"))
        round_b = (_make_card(ctrl.store, 0, "2001"), _make_card(ctrl.store, 1, "2002"), _make_card(ctrl.store, 2, "2003"))

        # Frame 1: Round A opens
        ctrl._apply_vision_frame_results(
            res=PickerDetectionResult(picker_visible=True, stable=True, rerolled=False, cards=round_a, cards_seen_on_frame=True),
            game_rect=rect,
            now_mono=100.0,
            force=True,
            capture_ms=1.0,
            timings={},
            roi_crops={},
            transmute_hits=[],
            passive_hover_hit=None,
            transmute_trace={},
            plan_b_ran=False,
            plan_b_item_hits=[],
            plan_b_item_states={},
            plan_b_aug_hits=[],
            frame_bgr=None,
            t_main0=time.perf_counter(),
            from_worker=False,
        )
        assert ctrl.sm.state == GameState.AUGMENT_PICKING
        assert ctrl._picker_coach_debounce_timer.isActive()

        # Simulate click on Round A slot 1 (1002), then next frame arrives directly with disjoint Round B!
        # Keep cards_seen_on_frame=True to prove Round A slot resolution runs BEFORE _frozen_pick_slot clear.
        monkeypatch.setattr(ctrl.click_tracker, "resolve_selection", lambda **_kw: (1, "win_click"))
        ctrl._apply_vision_frame_results(
            res=PickerDetectionResult(
                picker_visible=True,
                cards=round_b,
                stable=True,
                rerolled=True,
                cards_seen_on_frame=True,
            ),
            game_rect=rect,
            now_mono=101.0,
            force=True,
            capture_ms=1.0,
            timings={},
            roi_crops={},
            transmute_hits=[],
            passive_hover_hit=None,
            transmute_trace={},
            plan_b_ran=False,
            plan_b_item_hits=[],
            plan_b_item_states={},
            plan_b_aug_hits=[],
            frame_bgr=None,
            t_main0=time.perf_counter(),
            from_worker=False,
        )

        # Round A committed cleanly, flashed, fired augment_selected; Round B opened fresh!
        assert ctrl.sm.selected_augments == ["1002"]
        assert flashed == ["1002"]
        assert "augment_selected" in triggered
        assert ctrl.sm.state == GameState.AUGMENT_PICKING
        assert [c.augment.augment_id for c in ctrl.sm.active_cards] == ["2001", "2002", "2003"]

        transitions = [p for _c, et, p in devlab.events if et == "vision.picker.transition"]
        assert [t["transition"] for t in transitions] == ["open", "close", "open"]
        assert transitions[1]["committed_augment_id"] == "1002"
        assert transitions[1]["back_to_back"] is True

        # Recent offered pool orders Round B unrecorded ahead of Round A unrecorded, excluding picked 1002
        pool = ctrl.sm.recent_unrecorded_augments()
        assert pool == ["2001", "2002", "2003", "1001", "1004"]
        assert ctrl.edge_drawer._recent_unrecorded_augments == pool
    finally:
        _cleanup_controller(ctrl, qapp)


def test_p0_4_unlock_6_augment_slots_goal_cap_and_recap_v2(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_p04"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        assert MAX_AUGMENT_SLOTS == 6

        for aid in ("1001", "1002", "1004", "2001", "2002", "2003"):
            assert ctrl.sm.add_selected_augment(aid) is True
        assert len(ctrl.sm.selected_augments) == 6
        assert ctrl.sm.add_selected_augment("2004") is False

        ctrl._match_max_game_time_s = 320.0
        ctrl._match_last_hero_id = "99"
        ctrl._match_last_my_items = ("6655", "3089")

        # Below 4 augments (< 4), next_augment_goal is preserved; once >= 4 regular augments are owned, it is cleared
        ctrl.sm.selected_augments = ["1001", "1002", "1004"]
        ctrl._latest_advice = CoachAdvicePayload(request_id="r3", next_augment_goal="抓终极强化")
        ctrl._refresh_drawer_view()
        assert ctrl._latest_advice.next_augment_goal == "抓终极强化"

        ctrl.sm.selected_augments = ["1001", "1002", "1004", "2001"]
        ctrl._refresh_drawer_view()
        assert ctrl._latest_advice.next_augment_goal == ""

        ctrl.sm.selected_augments = ["1001", "1002", "1004", "2001", "2002", "2003"]
        ctrl._latest_advice = CoachAdvicePayload(request_id="r6", next_augment_goal="不应保留")
        ctrl._refresh_drawer_view()
        assert ctrl._latest_advice.next_augment_goal == ""

        recap = ctrl.build_match_recap_data(won=1)
        assert recap is not None
        assert len(recap.augments) == 6
        assert len(recap.augment_icon_paths) == 6
    finally:
        _cleanup_controller(ctrl, qapp)


def test_p0_5_bonus_cards_3170_3173_isolated_with_badges_and_45s_phantom_rollback(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_p05"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        ctrl.cfg.auto_trigger_coach = True
        devlab = _FakeDevLabRecorder()
        ctrl._devlab = devlab
        ctrl._devlab_active = True

        monkeypatch.setattr(ctrl, "_is_any_coach_online", lambda: True)
        triggered: list[str] = []
        monkeypatch.setattr(ctrl, "trigger_coach_request", lambda reason, **_kw: triggered.append(reason))
        enter_picker_calls: list[Any] = []
        monkeypatch.setattr(ctrl.combo_overlay, "enter_picker_mode", lambda v: enter_picker_calls.append(v))
        badge_renders: list[list[str]] = []
        orig_render = ctrl.augment_overlay.render_cards
        monkeypatch.setattr(
            ctrl.augment_overlay,
            "render_cards",
            lambda items, **kw: (badge_renders.append([it.score.augment_id for it in items]), orig_render(items, **kw)),
        )

        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 0, 1, ("3071",))
        me = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 6, 1, 0, 1, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(60.0, me, (me,), (enemy,), 1000.0))
        triggered.clear()

        # Pre-seed normal augment & plan
        assert ctrl.sm.add_selected_augment("1001") is True
        ctrl.sm.committed_plan = ["6655", "3089", "3157"]
        ctrl.sm.locked_next_item = "6655"
        ctrl.sm.allow_plan_overhaul = False

        rect = WindowRect(hwnd=1001, left=0, top=0, width=1920, height=1080)
        bonus_cards = (
            _make_card(ctrl.store, 0, "3170"),
            _make_card(ctrl.store, 1, "3171"),
            _make_card(ctrl.store, 2, "3172"),
        )
        ctrl._apply_vision_frame_results(
            res=PickerDetectionResult(picker_visible=True, stable=True, rerolled=False, cards=bonus_cards, cards_seen_on_frame=True),
            game_rect=rect,
            now_mono=200.0,
            force=True,
            capture_ms=1.0,
            timings={},
            roi_crops={},
            transmute_hits=[],
            passive_hover_hit=None,
            transmute_trace={},
            plan_b_ran=False,
            plan_b_item_hits=[],
            plan_b_item_states={},
            plan_b_aug_hits=[],
            frame_bgr=None,
            t_main0=time.perf_counter(),
            from_worker=False,
        )
        # Shows 0ms local badges, NEVER enters AUGMENT_PICKING or combo picker mode or AI trigger
        assert badge_renders and badge_renders[-1] == ["3170", "3171", "3172"]
        assert ctrl.augment_overlay.isVisible() is True
        assert ctrl.sm.state == GameState.IN_GAME
        assert ctrl.sm.selected_augments == ["1001"]
        assert enter_picker_calls == []
        assert triggered == []
        assert not (set(ctrl.sm.recent_unrecorded_augments()) & {"3170", "3171", "3172"})

        # Close bonus offer -> badges hide and nothing gets committed
        ctrl._apply_vision_frame_results(
            res=PickerDetectionResult(picker_visible=False, stable=False, rerolled=False, cards=(), cards_seen_on_frame=False),
            game_rect=rect,
            now_mono=202.0,
            force=True,
            capture_ms=1.0,
            timings={},
            roi_crops={},
            transmute_hits=[],
            passive_hover_hit=None,
            transmute_trace={},
            plan_b_ran=False,
            plan_b_item_hits=[],
            plan_b_item_states={},
            plan_b_aug_hits=[],
            frame_bgr=None,
            t_main0=time.perf_counter(),
            from_worker=False,
        )
        assert ctrl.augment_overlay.isVisible() is False
        assert ctrl.sm.selected_augments == ["1001"]

        # 45s phantom rollback test with transformative augment 1238
        offer = (_make_card(ctrl.store, 0, "1238"), _make_card(ctrl.store, 1, "1002"), _make_card(ctrl.store, 2, "1004"))
        ctrl.sm.on_picker_result(
            PickerDetectionResult(picker_visible=True, stable=True, rerolled=False, cards=offer, cards_seen_on_frame=True),
            clicked_slot=None,
            now_mono=300.0,
        )
        ctrl.sm.on_picker_result(
            PickerDetectionResult(picker_visible=False, stable=False, rerolled=False, cards=(), cards_seen_on_frame=False),
            clicked_slot=0,
            now_mono=301.0,
        )
        eff_commit = ctrl.sm.on_picker_result(
            PickerDetectionResult(picker_visible=False, stable=False, rerolled=False, cards=(), cards_seen_on_frame=False),
            clicked_slot=0,
            now_mono=301.35,
        )
        assert eff_commit.newly_selected_augment == "1238"
        assert ctrl.sm.selected_augments == ["1001", "1238"]
        assert ctrl.sm.pending_transmute is not None
        # Simulate overhaul mutation on committed_plan after transformative commit
        ctrl.sm.allow_plan_overhaul = True
        ctrl.sm.plan_overhaul_reason = "质变：棱彩阶"
        ctrl.sm.committed_plan = []
        ctrl.sm.locked_next_item = ""

        # Re-open SAME offer within 45s (t = 315.0s, dt = 13.65s) -> auto-rollback!
        ctrl._apply_vision_frame_results(
            res=PickerDetectionResult(picker_visible=True, stable=True, rerolled=False, cards=offer, cards_seen_on_frame=True),
            game_rect=rect,
            now_mono=315.0,
            force=True,
            capture_ms=1.0,
            timings={},
            roi_crops={},
            transmute_hits=[],
            passive_hover_hit=None,
            transmute_trace={},
            plan_b_ran=False,
            plan_b_item_hits=[],
            plan_b_item_states={},
            plan_b_aug_hits=[],
            frame_bgr=None,
            t_main0=time.perf_counter(),
            from_worker=False,
        )
        assert ctrl.sm.selected_augments == ["1001"]
        assert ctrl.sm.pending_transmute is None
        assert ctrl.sm.committed_plan == ["6655", "3089", "3157"]
        assert ctrl.sm.locked_next_item == "6655"
        assert ctrl.sm.allow_plan_overhaul is False
        assert ctrl.sm.plan_overhaul_reason == ""
        rb_events = [p for _c, et, p in devlab.events if et == "vision.pick.phantom_rollback"]
        assert len(rb_events) == 1
        assert rb_events[0]["rolled_back_augment_id"] == "1238"
        assert rb_events[0]["restored_plan"] == ["6655", "3089", "3157"]

        # Outside 45s window -> must NOT roll back
        sm_exp = HexStateMachine(store=ctrl.store)
        sm_exp.state = GameState.IN_GAME
        sm_exp.committed_plan = ["6655", "3089"]
        sm_exp.locked_next_item = "6655"
        sm_exp.on_picker_result(PickerDetectionResult(picker_visible=True, stable=True, rerolled=False, cards=offer, cards_seen_on_frame=True), None, 400.0)
        sm_exp.on_picker_result(PickerDetectionResult(picker_visible=False, stable=False, rerolled=False, cards=(), cards_seen_on_frame=False), 1, 401.0)
        sm_exp.on_picker_result(PickerDetectionResult(picker_visible=False, stable=False, rerolled=False, cards=(), cards_seen_on_frame=False), 1, 401.3)
        assert sm_exp.selected_augments == ["1002"]
        eff_late = sm_exp.on_picker_result(
            PickerDetectionResult(picker_visible=True, stable=True, rerolled=False, cards=offer, cards_seen_on_frame=True),
            None,
            447.0,
        )
        assert eff_late.phantom_rolled_back_id == ""
        assert sm_exp.selected_augments == ["1002"]
    finally:
        _cleanup_controller(ctrl, qapp)


def test_p1_band_mode_death_attribution_async_lcu_and_hud_wide_diag(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_p1"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        ctrl.cfg.data_band_mode = "high"
        ctrl.cfg.auto_trigger_coach = True
        ctrl.cfg.smart_picker_ai_gate = False
        ctrl.cfg.bridge_enabled = True
        devlab = _FakeDevLabRecorder()
        ctrl._devlab = devlab
        ctrl._devlab_active = True

        class _FakeBridge:
            def is_coach_online(self) -> bool:
                return True

            def publish_request(self, _req: Any) -> None:
                return None

            def stop(self) -> None:
                return None

        ctrl.bridge = _FakeBridge()

        # 1. data_band_mode passthrough to evaluate_picker_combos & build_combo_plan
        eval_bands: list[str | None] = []
        plan_bands: list[str | None] = []
        orig_eval = app_mod.evaluate_picker_combos
        orig_plan = app_mod.build_combo_plan

        def _fake_eval(**kw: Any) -> PickerComboVerdict:
            eval_bands.append(kw.get("data_band_mode"))
            return orig_eval(**kw)

        def _fake_plan(**kw: Any) -> ComboPlan:
            plan_bands.append(kw.get("data_band_mode"))
            return orig_plan(**kw)

        monkeypatch.setattr(app_mod, "evaluate_picker_combos", _fake_eval)
        monkeypatch.setattr(app_mod, "build_combo_plan", _fake_plan)

        ctrl.sm.current_hero_id = "99"
        ctrl._refresh_combo_plan_view()
        assert plan_bands[-1] == "high"
        offer = (_make_card(ctrl.store, 0, "1001"), _make_card(ctrl.store, 1, "1002"), _make_card(ctrl.store, 2, "1004"))
        ctrl.sm.state = GameState.AUGMENT_PICKING
        ctrl.sm.active_cards = offer
        ctrl._render_augment_badges(offer)
        assert eval_bands[-1] == "high"
        ctrl._last_combo_verdict = None
        ctrl._should_skip_picker_ai_call(offer)
        assert eval_bands[-1] == "high"
        ctrl._last_combo_verdict = None
        ctrl.trigger_coach_request("augment_picking")
        assert plan_bands[-1] == "high"
        assert eval_bands[-1] == "high"
        ctrl.sm.state = GameState.IDLE
        ctrl.sm.active_cards = ()
        ctrl._last_combo_verdict = None

        # 2. Async LCU _start_or_attach_match_session does NOT block Qt thread & drops stale sessions
        gate = threading.Event()
        lcu_calls: list[int] = []

        def _slow_lcu() -> tuple[int, int]:
            lcu_calls.append(1)
            gate.wait(timeout=2.0)
            return 888777, 2400

        monkeypatch.setattr(ctrl, "_safe_get_lcu_game_id", _slow_lcu)
        monkeypatch.setattr(ctrl, "_safe_get_current_rune_page", lambda: {"primary_style_id": 8200, "sub_style_id": 8100, "selected_perk_ids": [8229]})

        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 3, 0, 0, 0, ("3071",))
        me_lvl3 = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 3, 0, 0, 0, ("1058",))
        snap0 = LiveMatchSnapshot(15.0, me_lvl3, (me_lvl3,), (enemy,), 500.0, game_mode="KIWI")

        t0 = time.perf_counter()
        ctrl._start_or_attach_match_session(snap0, "99")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < 250.0  # returned before gate unlocked!

        # Simulate a newer match starting before slow Match 1 thread finished -> Match 1 result must be dropped!
        prev_seq = ctrl._match_session_seq
        ctrl._match_session_seq = prev_seq + 1
        ctrl._active_game_id = 999111
        ctrl._active_match_key = "gid:999111"
        gate.set()
        deadline = time.monotonic() + 1.5
        while not lcu_calls and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        qapp.processEvents()
        assert ctrl._active_game_id == 999111
        assert all(u.get("match_key_hash") != "888777" for u in devlab.meta_updates)

        # 3. Death defer superseded by AUGMENT_PICKING -> augment_selected fixes death_shop_window attribution!
        emitted_tracker: list[tuple[str, str, dict[str, Any]]] = []
        ctrl._action_tracker = MatchActionTracker(
            lambda cat, et, p, _gt: emitted_tracker.append((cat, et, dict(p))),
            is_complete_item_fn=lambda iid: iid in ("6655", "3089"),
        )
        ctrl.sm.state = GameState.IN_GAME
        ctrl._apply_live_snapshot(LiveMatchSnapshot(100.0, me_lvl3, (me_lvl3,), (enemy,), 1600.0))
        ctrl._last_warmup_advice_mono = 0.0
        me_dead = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", True, 3, 0, 1, 0, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(120.0, me_dead, (me_dead,), (enemy,), 1600.0))
        assert ctrl._death_coach_defer_timer.isActive()

        # Opening picker supersedes death defer, then user picks augment -> trigger_coach_request("augment_selected")
        ctrl._cancel_death_coach_defer("AUGMENT_PICKING opened")
        assert ctrl._death_defer_superseded_by_picker is True
        ctrl.sm.add_selected_augment("1001")
        req = ctrl.trigger_coach_request("augment_selected")
        assert req is not None
        assert ctrl._death_superseded_req_id == req.request_id
        assert ctrl._action_tracker._death_window is not None
        assert ctrl._action_tracker._death_window.coach_triggered is True
        assert ctrl._action_tracker._death_window.coach_skip_reason == ""

        # Advice arrives before respawn/buy
        ctrl._on_coach_advice(
            CoachAdvicePayload(
                request_id=req.request_id,
                tactical_summary="出卢登",
                next_items=[CoachAdviceItem("6655", "卢登的伙伴")],
            )
        )
        assert ctrl._action_tracker._death_window.coach_arrived is True
        assert ctrl._action_tracker._death_window.coach_arrived_before_buy is True

        # Respawn flushes death_shop_window with clean attribution
        me_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 3, 0, 1, 0, ("6655",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(136.0, me_alive, (me_alive,), (enemy,), 300.0))
        dws = [p for _c, et, p in emitted_tracker if et == "death_shop_window"]
        assert len(dws) == 1
        assert dws[0]["coach_triggered"] is True
        assert dws[0]["coach_arrived"] is True
        assert dws[0]["coach_skip_reason"] == ""

        # 4. hud_wide_diag_crop popped from vdiag JSON & enqueued <= 1 per match
        wide_crop = np.zeros((32, 64, 3), dtype=np.uint8)
        ctrl.icon_matcher._last_verify_diagnostics = {  # type: ignore[union-attr]
            "outcome": "below_threshold",
            "hud_strip_blind": True,
            "hud_wide_diag_crop": wide_crop,
        }
        rect = WindowRect(hwnd=1001, left=0, top=0, width=1920, height=1080)
        ctrl._emit_icon_verify_devlab(
            np.zeros((100, 100, 3), dtype=np.uint8),
            rect,
            committed_id="",
            candidate_ids=("1001", "1002"),
            slot_index_hint=0,
            action_taken="none",
            hit=None,
        )
        ctrl._emit_icon_verify_devlab(
            np.zeros((100, 100, 3), dtype=np.uint8),
            rect,
            committed_id="",
            candidate_ids=("1001", "1002"),
            slot_index_hint=1,
            action_taken="none",
            hit=None,
        )
        verify_events = [p for _c, et, p in devlab.events if et == "vision.icon_verify.outcome"]
        assert len(verify_events) == 2
        assert "hud_wide_diag_crop" not in verify_events[0]
        wide_rois = [r for r in devlab.rois if r[0] == "hud_wide_diag"]
        assert len(wide_rois) == 1
    finally:
        _cleanup_controller(ctrl, qapp)


def test_worker_e_skin_tracking_prefetch_and_async_recap_hydration(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_e"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store

        splash_path = tmp_path / "lux_skin_99008.jpg"
        splash_requests: list[tuple[str, int]] = []
        ensure_gate = threading.Event()

        def _resolve_cached(hid: str, sid: int = 0) -> tuple[str, str]:
            if str(hid) == "99" and int(sid) == 99008 and splash_path.is_file():
                return str(splash_path), "星之守护者"
            return "", ""

        def _ensure_skin(hid: str, sid: int = 0) -> tuple[Path | None, str]:
            splash_requests.append((str(hid), int(sid)))
            if int(sid) == 99008:
                ensure_gate.wait(timeout=1.5)
                img = QImage(320, 180, QImage.Format.Format_ARGB32)
                img.fill(QColor("#5B21B6"))
                img.save(str(splash_path), "JPG", 90)
                return splash_path, "星之守护者"
            return None, ""

        monkeypatch.setattr(ctrl.store.static, "resolve_cached_skin_splash", _resolve_cached)
        monkeypatch.setattr(ctrl.store.static, "ensure_skin_splash", _ensure_skin)

        # Champ select updates my_skin_id=99007 and triggers prefetch
        ensure_gate.set()
        cs = ChampSelectState(
            queue_id=2400,
            game_mode="KIWI",
            is_mayhem=True,
            my_hero_id="99",
            team_hero_ids=("99",),
            game_id=555666,
            my_skin_id=99007,
        )
        ctrl._apply_lcu_poll("ChampSelect", cs)
        assert ctrl._match_last_skin_id == 99007
        deadline = time.monotonic() + 1.0
        while ("99", 99007) not in splash_requests and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ("99", 99007) in splash_requests

        # In-game snapshot updates skin_id=99008 & skin_name, then at EOG recap builds before download completes
        ensure_gate.clear()
        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 12, 3, 2, 1, ("3071",))
        me = PlayerLiveInfo(
            "me",
            "拉克丝",
            "Lux",
            "ORDER",
            False,
            12,
            8,
            1,
            6,
            ("6655", "3089"),
            skin_id=99008,
            skin_name="星之守护者",
        )
        ctrl._apply_live_snapshot(LiveMatchSnapshot(300.0, me, (me,), (enemy,), 1500.0, my_team_kills=14, enemy_team_kills=9))
        assert ctrl._match_last_skin_id == 99008
        assert ctrl._match_last_skin_name == "星之守护者"

        ctrl._capture_post_match_recap(won=1)
        assert ctrl._last_recap_data is not None
        assert ctrl._last_recap_data.skin_id == 99008
        assert ctrl._last_recap_data.skin_name == "星之守护者"
        assert ctrl._last_recap_data.skin_splash_path == ""

        dlg = ctrl._open_last_match_recap()
        assert isinstance(dlg, MatchRecapDialog)
        qapp.processEvents()

        # Unblock background download -> async hydration refreshes _last_recap_data & live dialog in-place!
        ensure_gate.set()
        deadline2 = time.monotonic() + 2.0
        while time.monotonic() < deadline2:
            qapp.processEvents()
            if ctrl._last_recap_data.skin_splash_path == str(splash_path) and dlg.data.skin_splash_path == str(splash_path):
                break
            time.sleep(0.02)

        assert ctrl._last_recap_data.skin_splash_path == str(splash_path)
        assert dlg.data.skin_splash_path == str(splash_path)
    finally:
        _cleanup_controller(ctrl, qapp)
