"""Tests for HexHelper v1.5.2 client token/cost optimizations (Wave D)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

from hex_helper import app as app_mod
from hex_helper.app import GameState, HexHelperController, HexStateMachine
from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload, utc_now_iso
from hex_helper.coach.gemini_coach import (
    COACH_SYSTEM_INSTRUCTION,
    PICKER_FAST_SYSTEM_INSTRUCTION,
    GeminiCallStats,
    GeminiCoachWorker,
    build_high_snr_prompt_payload,
    build_picker_fast_prompt_payload,
    ground_coach_advice,
)
from hex_helper.data.provider import (
    AugmentInfo,
    CoreBuild,
    DatasetDescriptor,
    HeroAugmentStat,
    HeroStat,
    ItemStaticInfo,
)
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.vision.capture import CaptureResult, WindowRect

SERVER_ROOT = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from hex_server import coach_gateway as server_gw  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    inst = QApplication.instance()
    if inst is None:
        inst = QApplication([])
    return inst  # type: ignore[return-value]


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "cache")
    static.items = {
        "1058": ItemStaticInfo("1058", "无用大棒", 1200, ("SpellDamage",), {"FlatMagicDamageMod": 65.0}, category="component"),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, category="complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, category="complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage",), {"FlatMagicDamageMod": 95.0}, category="complete_equipment"),
        "3071": ItemStaticInfo("3071", "黑色切割者", 3000, ("Damage", "Health"), {"FlatPhysicalDamageMod": 40.0}, category="complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0}, category="complete_equipment"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), category="boots"),
        "3111": ItemStaticInfo("3111", "水银之靴", 1250, ("Boots",), category="boots"),
        "2139": ItemStaticInfo("2139", "巫术合剂", 500, ("Consumable",), category="consumable"),
    }
    static.hero_aliases.update({"99": "Lux", "266": "Aatrox"})
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("26.23", "normal", "常规", "datasets/26.23/normal", "", False)
    store._heroes["99"] = HeroStat("99", "拉克丝", "光辉女郎", 100000, 52000, 0.52, 0.1, 20, False)
    store._heroes["266"] = HeroStat("266", "亚托克斯", "暗裔剑魔", 90000, 45900, 0.51, 0.08, 30, False)
    store._hero_name_index.update({"拉克丝": "99", "lux": "99", "亚托克斯": "266", "aatrox": "266"})
    store._core_builds["99"] = [
        CoreBuild(("6655", "3089", "3135"), True, 4000, 2120, 0.53, 0.25),
    ]
    store._augments["1001"] = AugmentInfo("1001", "海克斯1", "silver")
    store._augments["1002"] = AugmentInfo("1002", "海克斯2", "gold")
    store._augments["1048"] = AugmentInfo("1048", "珠光护手", "prismatic")
    store._hero_augments["99"] = {
        "1048": HeroAugmentStat("1048", 5000, 2750, 0.55, False),
    }
    return store


class _NoopCapturer:
    backend = "noop"
    consecutive_black_frames = 0

    def grab(self, *_a: Any, **_kw: Any) -> CaptureResult:
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


def _make_controller(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subdir: str = "appdata",
) -> tuple[HexHelperController, MagicMock]:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / subdir))
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)
    monkeypatch.setattr(
        app_mod,
        "find_game_window",
        lambda: WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080),
    )
    monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: True)

    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    ctrl._vision_timer.stop()
    ctrl._heartbeat_timer.stop()
    ctrl.store = _make_store(tmp_path)
    ctrl.sm._store = ctrl.store
    ctrl.cfg.auto_trigger_coach = True
    ctrl.cfg.smart_picker_ai_gate = False
    monkeypatch.setattr(ctrl, "_is_any_coach_online", lambda: True)
    monkeypatch.setattr(ctrl, "_is_direct_gemini_active", lambda: True)
    submit_spy = MagicMock()
    assert ctrl.gemini_coach is not None
    monkeypatch.setattr(ctrl.gemini_coach, "submit", submit_spy)
    return ctrl, submit_spy


def _cleanup(ctrl: HexHelperController, qapp: QApplication) -> None:
    ctrl.shutdown()
    qapp.processEvents()


def test_same_life_single_warmup_lock_and_tier2_boots_sig_exclusion(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    sm = HexStateMachine(store=store)
    assert sm.PRE_DEATH_WARMUP_COOLDOWN_S == 90.0

    enemy_base = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 0, 1, ("3071",))
    enemy_boots_only = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 7, 1, 0, 1, ("3071", "3111"))
    me_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 6, 1, 0, 2, ("1058",))

    # Tier-2 boots never alter _enemy_build_signature
    assert sm._enemy_build_signature(
        LiveMatchSnapshot(100.0, me_alive, (me_alive,), (enemy_base,), 1000.0),
        store=store,
    ) == sm._enemy_build_signature(
        LiveMatchSnapshot(105.0, me_alive, (me_alive,), (enemy_boots_only,), 1000.0),
        store=store,
    )

    eff0 = sm.on_live_snapshot(LiveMatchSnapshot(20.0, me_alive, (me_alive,), (enemy_base,), 1400.0), "99", now_mono=100.0, store=store)
    assert eff0.coach_trigger == "game_start"

    # First warmup of life fires at t=195 (>=90s cooldown, >=1400G)
    eff_w1 = sm.on_live_snapshot(LiveMatchSnapshot(140.0, me_alive, (me_alive,), (enemy_base,), 1500.0), "99", now_mono=195.0, store=store)
    assert eff_w1.coach_trigger == "pre_death_warmup"
    assert sm._warmed_up_this_life is True

    # Gold increases arbitrarily in same life + enemy finishes Tier-2 boots -> STILL suppressed!
    for idx, gold_val in enumerate((2200.0, 3100.0, 4500.0), start=1):
        eff_same = sm.on_live_snapshot(
            LiveMatchSnapshot(250.0 + idx * 100.0, me_alive, (me_alive,), (enemy_boots_only,), gold_val),
            "99",
            now_mono=195.0 + idx * 100.0,
            store=store,
        )
        assert eff_same.coach_trigger == ""

    # Enemy finishes major item 3084 after cooldown -> allowed 2nd warmup in same life
    enemy_major = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 10, 2, 0, 1, ("3071", "3111", "3084"))
    eff_major = sm.on_live_snapshot(LiveMatchSnapshot(580.0, me_alive, (me_alive,), (enemy_major,), 1200.0), "99", now_mono=595.0, store=store)
    assert eff_major.coach_trigger == "pre_death_warmup"

    # Death resets same-life warmup lock cleanly
    me_dead = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", True, 10, 1, 1, 2, ("1058",))
    sm.on_live_snapshot(LiveMatchSnapshot(600.0, me_dead, (me_dead,), (enemy_major,), 1250.0), "99", now_mono=610.0, store=store)
    assert sm._warmed_up_this_life is False


def test_inflight_warmup_reuse_and_workers_has_inflight_trigger(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Verify GeminiCoachWorker.has_inflight_trigger returns True while warmup HTTP is flying
    entered = threading.Event()
    release = threading.Event()

    class _FakeGemClient:
        def advise(self, r: CoachRequestPayload, _st: Any, **_kw: Any):
            entered.set()
            release.wait(timeout=3.0)
            return (
                CoachAdvicePayload(request_id=r.request_id, tactical_summary="ok"),
                GeminiCallStats(model="gemini-4-hydrogen", thinking_level="low", latency_ms=100),
            )

        def close(self) -> None:
            return None

    gem_worker = GeminiCoachWorker(client=_FakeGemClient(), max_workers=2)  # type: ignore[arg-type]
    try:
        gem_worker.submit(
            CoachRequestPayload(
                request_id="req-w1",
                trigger_reason="pre_death_warmup",
                timestamp=utc_now_iso(),
                dataset_patch="26.23",
                my_hero={"hero_id": "99", "name": "拉克丝"},
            ),
            None,
            api_key="k",
            model="gemini-4-hydrogen",
            thinking_level="low",
            proxy_url="",
        )
        assert entered.wait(timeout=2.0) is True
        assert gem_worker.has_inflight_trigger("pre_death_warmup") is True
        assert gem_worker.has_inflight_trigger("manual_f6") is False
    finally:
        release.set()
        gem_worker.stop()

    # 2. Verify controller suppresses duplicate death_enemy_build_changed when fresh in-flight warmup matches enemy sig
    ctrl, submit_spy = _make_controller(qapp, tmp_path, monkeypatch, "appdata_inflight")
    try:
        monkeypatch.setattr(ctrl, "_should_defer_death_coach", lambda _s: False)
        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 8, 1, 0, 1, ("3071",))
        me_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 8, 1, 0, 2, ("1058",))

        ctrl._apply_live_snapshot(LiveMatchSnapshot(180.0, me_alive, (me_alive,), (enemy,), 1500.0))
        submit_spy.reset_mock()

        # Simulate pre_death_warmup dispatched 3s ago and still in flight with same enemy sig
        assert ctrl.gemini_coach is not None
        monkeypatch.setattr(ctrl.gemini_coach, "has_inflight_trigger", lambda trig: trig == "pre_death_warmup")
        ctrl._latest_advice = None
        ctrl._last_warmup_dispatched_mono = time.monotonic() - 3.0
        ctrl._last_warmup_dispatched_enemy_sig = ctrl.sm._enemy_build_signature(
            LiveMatchSnapshot(220.0, me_alive, (me_alive,), (enemy,), 1900.0),
            store=ctrl.store,
        )

        me_dead = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", True, 8, 1, 1, 2, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(225.0, me_dead, (me_dead,), (enemy,), 1900.0))
        assert submit_spy.call_count == 0

        # Stale (> 15s) warmup MUST NOT block fresh death coach
        ctrl.sm._was_dead = False
        ctrl.sm._last_death_coach_mono = -999.0
        ctrl._last_warmup_dispatched_mono = time.monotonic() - 20.0
        ctrl._apply_live_snapshot(LiveMatchSnapshot(290.0, me_dead, (me_dead,), (enemy,), 2200.0))
        assert submit_spy.call_count == 1
        assert submit_spy.call_args.args[0].trigger_reason == "death_enemy_build_changed"
    finally:
        _cleanup(ctrl, qapp)


def test_manual_remove_augment_defers_and_cancels_on_readd(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctrl, submit_spy = _make_controller(qapp, tmp_path, monkeypatch, "appdata_rem_defer")
    try:
        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 8, 1, 0, 1, ("3071",))
        me_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 8, 1, 0, 2, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(120.0, me_alive, (me_alive,), (enemy,), 1500.0))
        ctrl.sm.add_selected_augment("1001")
        submit_spy.reset_mock()

        # Removing an augment defers by 2500ms rather than firing immediately
        assert ctrl._augment_remove_coach_timer is not None
        ctrl._on_manual_remove_augment(0)
        assert submit_spy.call_count == 0
        assert ctrl._augment_remove_coach_timer.isActive() is True

        # Player immediately re-adds the corrected augment -> timer cancels cleanly, only 1 request fires!
        ctrl._on_manual_add_augment("1002")
        assert ctrl._augment_remove_coach_timer.isActive() is False
        assert submit_spy.call_count == 1
        assert submit_spy.call_args.args[0].trigger_reason == "augment_selected"

        # If not re-added and timer fires in IN_GAME -> fires 1 deferred augment_selected
        ctrl._on_manual_remove_augment(0)
        assert ctrl._augment_remove_coach_timer.isActive() is True
        ctrl._augment_remove_coach_timer.stop()
        ctrl._on_augment_remove_coach_timeout()
        assert submit_spy.call_count == 2
    finally:
        _cleanup(ctrl, qapp)


def test_augment_selected_15s_dedup_and_transformative_flag_and_prompt_sync(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert COACH_SYSTEM_INSTRUCTION == server_gw.COACH_SYSTEM_INSTRUCTION
    assert PICKER_FAST_SYSTEM_INSTRUCTION == server_gw.PICKER_FAST_SYSTEM_INSTRUCTION
    assert "思考直切核心" in COACH_SYSTEM_INSTRUCTION
    assert "冗长自检" in COACH_SYSTEM_INSTRUCTION
    assert "禁冗长自检" in PICKER_FAST_SYSTEM_INSTRUCTION

    ctrl, submit_spy = _make_controller(qapp, tmp_path, monkeypatch, "appdata_sel_dedup")
    try:
        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 8, 1, 0, 1, ("3071",))
        me_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 8, 1, 0, 2, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(120.0, me_alive, (me_alive,), (enemy,), 1500.0))
        submit_spy.reset_mock()

        # Ordinary Round-1 augment -> allow_overhaul=True, transformative_overhaul=False
        ctrl.sm.add_selected_augment("1001")
        ctrl.sm.allow_plan_overhaul = True
        ctrl.sm.plan_overhaul_reason = ""

        req1 = ctrl.trigger_coach_request("augment_selected")
        assert req1 is not None
        assert req1.committed_build_plan["allow_overhaul"] is True
        assert req1.committed_build_plan["transformative_overhaul"] is False
        assert submit_spy.call_count == 1

        # High-SNR midgame payload keeps keys[:4] == [..., item_glossary] even when hero_signature_hint is present
        req_with_sig = CoachRequestPayload(
            request_id=req1.request_id,
            trigger_reason="augment_selected",
            timestamp=req1.timestamp,
            dataset_patch=req1.dataset_patch,
            my_hero=req1.my_hero,
            selected_augments=req1.selected_augments,
            allies=req1.allies,
            enemies=req1.enemies,
            committed_build_plan=req1.committed_build_plan,
            hero_signature_hint={"archetype": "爆发法师", "signature_items": ["3089"]},
        )
        high_snr = build_high_snr_prompt_payload(req_with_sig, ctrl.store)
        assert list(high_snr.keys())[:4] == [
            "dataset_patch",
            "hero_static",
            "hero_big_data_top_builds",
            "item_glossary",
        ]
        assert high_snr["committed_build_plan"]["transformative_overhaul"] is False

        fast_pick = build_picker_fast_prompt_payload(req_with_sig, ctrl.store, compact_comp=True)
        assert list(fast_pick.keys())[:3] == ["dataset_patch", "hero_static", "trigger_reason"]

        # Duplicate augment_selected within 15s for unchanged state -> suppressed!
        req_dup = ctrl.trigger_coach_request("augment_selected")
        assert req_dup is None
        assert submit_spy.call_count == 1

        # Genuine transformative pivot sets transformative_overhaul=True and bypasses dedup
        ctrl.sm.add_selected_augment("1048")
        ctrl.sm.allow_plan_overhaul = True
        ctrl.sm.plan_overhaul_reason = "海克斯变阵：珠光护手"
        req2 = ctrl.trigger_coach_request("augment_selected")
        assert req2 is not None
        assert req2.committed_build_plan["transformative_overhaul"] is True
        assert submit_spy.call_count == 2
    finally:
        _cleanup(ctrl, qapp)


def test_ground_coach_advice_normalizes_build_path_and_blocks_hallucinated_pipes_dicts(
    tmp_path: Path,
) -> None:
    assert COACH_SYSTEM_INSTRUCTION == server_gw.COACH_SYSTEM_INSTRUCTION
    store = _make_store(tmp_path)
    req = CoachRequestPayload(
        request_id="req-bp-norm",
        trigger_reason="death_enemy_build_changed",
        timestamp=utc_now_iso(),
        dataset_patch="26.23",
        my_hero={"hero_id": "99", "name": "拉克丝", "items": []},
    )

    stats_valid: dict[str, int] = {}
    grounded_valid = ground_coach_advice(
        {
            "augment_pick_advice": "",
            "next_augment_goal": "",
            "buy_now": "买无用大棒",
            "build_path": [
                "6655|卢登的伙伴",
                {"item_id": "3089", "name": "灭世者的死亡之帽"},
                "{'item_id': '3135', 'name': '虚空之杖'}",
            ],
            "next_items": [
                {"item_id": "6655", "name": "卢登的伙伴", "priority": "下件必出", "reason": "核心"}
            ],
            "tactical_summary": "消耗为主",
        },
        req,
        store,
        stats_out=stats_valid,
    )
    assert grounded_valid.build_path == ["卢登的伙伴", "灭世者的死亡之帽", "虚空之杖"]
    assert stats_valid["invalid_route_blocked"] == 0
    assert stats_valid["total_blocked"] == 0

    stats_bad: dict[str, int] = {}
    grounded_bad = ground_coach_advice(
        {
            "augment_pick_advice": "",
            "next_augment_goal": "",
            "buy_now": "买无用大棒",
            "build_path": [
                "9999|虚构神装",
                {"item_id": "9999", "name": "虚构神装"},
                "3089|灭世者的死亡之帽",
            ],
            "next_items": [
                {"item_id": "3089", "name": "灭世者的死亡之帽", "priority": "下件必出", "reason": "高法强"}
            ],
            "tactical_summary": "消耗为主",
        },
        req,
        store,
        stats_out=stats_bad,
    )
    assert grounded_bad.build_path == ["灭世者的死亡之帽"]
    assert stats_bad["invalid_route_blocked"] == 2
