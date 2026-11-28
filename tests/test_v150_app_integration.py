"""WS-E v1.5.0 integration tests: warmup dedup, 75s cache cover, 12s 2999 grace, hud_scale passthrough, version 1.5.0."""

from __future__ import annotations

from pathlib import Path
import socket
import time
from typing import Any

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

import hex_helper
from hex_helper import app as app_mod
from hex_helper import edition as edition_mod
from hex_helper.app import GameState, HexHelperController, HexStateMachine
from hex_helper.bridge.protocol import CoachAdvicePayload
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.action_tracker import MatchActionTracker
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher
from hex_helper.vision.capture import CaptureResult, WindowRect


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


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", fetcher=lambda _u: b"")
    static.items = {
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105.0}, "", "complete_equipment"),
        "3071": ItemStaticInfo("3071", "黑色切割者", 3000, ("Damage", "Health"), {"FlatPhysicalDamageMod": 40.0}, "", "complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0}, "", "complete_equipment"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "1058": ItemStaticInfo("1058", "无用大棒", 1200, ("SpellDamage",), {"FlatMagicDamageMod": 65.0}, "", "other"),
    }
    store = DataStore(static)
    store._descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "99": HeroStat("99", "拉克丝", "Lux", 80000, 41600, 0.52, 0.08, 15, False),
        "98": HeroStat("98", "慎", "Shen", 45000, 23400, 0.52, 0.05, 22, False),
    }
    store._core_builds = {
        "99": [CoreBuild(("6655", "3089", "3157"), True, 12000, 6240, 0.52, 0.42)],
        "98": [CoreBuild(("3084", "3071"), True, 9000, 4680, 0.52, 0.35)],
    }
    store._augments = {
        "1001": AugmentInfo("1001", "珠光护手", "prismatic", "技能可暴击"),
        "1002": AugmentInfo("1002", "尤里卡", "prismatic", "法强转急速"),
        "1238": AugmentInfo("1238", "质变：棱彩阶", "gold", "获得一个随机棱彩强化符文"),
        "2001": AugmentInfo("2001", "炼狱导管", "prismatic", "技能附带持续灼烧"),
    }
    return store


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


def _patch_headless_controller(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _cleanup_controller(ctrl: HexHelperController, qapp: QApplication) -> None:
    for attr in ("edge_drawer", "combo_overlay", "champ_overlay", "augment_overlay", "lookup_window"):
        w = getattr(ctrl, attr, None)
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
    ctrl.shutdown()
    qapp.processEvents()


def test_v150_formal_version_alignment() -> None:
    assert edition_mod.APP_VERSION == "1.5.2"
    assert hex_helper.__version__ == "1.5.2"
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.5.2"' in pyproject


def test_v150_pre_death_warmup_90s_cooldown_and_state_sig_dedup(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    sm = HexStateMachine(store=store)
    assert sm.PRE_DEATH_WARMUP_COOLDOWN_S == 90.0

    enemy_a = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 0, 1, ("3071",))
    me_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 6, 1, 0, 2, ("1058",))

    # 1. Match start at t=100.0 -> game_start
    eff0 = sm.on_live_snapshot(LiveMatchSnapshot(20.0, me_alive, (me_alive,), (enemy_a,), 1400.0), "99", now_mono=100.0, store=store)
    assert eff0.coach_trigger == "game_start"

    # 2. At t=140.0 (+40s < 90s) -> blocked by 90s cooldown
    eff_early = sm.on_live_snapshot(LiveMatchSnapshot(85.0, me_alive, (me_alive,), (enemy_a,), 1600.0), "99", now_mono=140.0, store=store)
    assert eff_early.coach_trigger == ""

    # 3. At t=195.0 (+95s >= 90s) with gold=1600 -> fires pre_death_warmup (single warmup for this life)
    eff_w1 = sm.on_live_snapshot(LiveMatchSnapshot(140.0, me_alive, (me_alive,), (enemy_a,), 1600.0), "99", now_mono=195.0, store=store)
    assert eff_w1.coach_trigger == "pre_death_warmup"

    # 4. At t=295.0 (+100s >= 90s) with gold=1750 (unchanged enemy & augs) -> DEDUPED!
    eff_dedup = sm.on_live_snapshot(LiveMatchSnapshot(240.0, me_alive, (me_alive,), (enemy_a,), 1750.0), "99", now_mono=295.0, store=store)
    assert eff_dedup.coach_trigger == ""

    # 5. At t=300.0 gold rises into 1850G/2100G in SAME life with unchanged enemy & augs -> STILL SUPPRESSED!
    eff_gold_bucket = sm.on_live_snapshot(LiveMatchSnapshot(245.0, me_alive, (me_alive,), (enemy_a,), 1850.0), "99", now_mono=300.0, store=store)
    assert eff_gold_bucket.coach_trigger == ""

    # 6. At t=400.0 (+100s) gold=2100 unchanged enemy & augs -> STILL SUPPRESSED!
    eff_dedup2 = sm.on_live_snapshot(LiveMatchSnapshot(345.0, me_alive, (me_alive,), (enemy_a,), 2100.0), "99", now_mono=400.0, store=store)
    assert eff_dedup2.coach_trigger == ""

    # 7. At t=405.0 enemy completes a new major item (3084) -> RE-FIRES even with gold < 1400G!
    enemy_b = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 10, 2, 1, 1, ("3071", "3084"))
    eff_enemy = sm.on_live_snapshot(LiveMatchSnapshot(350.0, me_alive, (me_alive,), (enemy_b,), 900.0), "99", now_mono=405.0, store=store)
    assert eff_enemy.coach_trigger == "pre_death_warmup"

    # 8. At t=500.0 (+95s) selecting a new augment changes signature -> RE-FIRES
    assert sm.add_selected_augment("1001") is True
    eff_aug = sm.on_live_snapshot(LiveMatchSnapshot(445.0, me_alive, (me_alive,), (enemy_b,), 1500.0), "99", now_mono=500.0, store=store)
    assert eff_aug.coach_trigger == "pre_death_warmup"

    # 9. Death transition resets same-life warmup lock & _last_warmup_state_sig so post-death can re-warm after respawn + cooldown
    me_dead = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", True, 10, 1, 1, 2, ("1058",))
    eff_dead = sm.on_live_snapshot(LiveMatchSnapshot(460.0, me_dead, (me_dead,), (enemy_b,), 1550.0), "99", now_mono=515.0, store=store)
    assert eff_dead.death_detected is True
    assert sm._last_warmup_state_sig == ()
    assert sm._warmed_up_this_life is False

    # Respawn at t=530.0
    sm.on_live_snapshot(LiveMatchSnapshot(475.0, me_alive, (me_alive,), (enemy_b,), 1550.0), "99", now_mono=530.0, store=store)
    # After >= 90s from death (t=610.0 >= 515.0 + 90.0), identical pre-death sig (same enemy/items/augs) CAN re-warm in new life!
    eff_post_death = sm.on_live_snapshot(LiveMatchSnapshot(555.0, me_alive, (me_alive,), (enemy_b,), 1550.0), "99", now_mono=610.0, store=store)
    assert eff_post_death.coach_trigger == "pre_death_warmup"

    # reset_match clears warmup state sig cleanly
    sm.reset_match()
    assert sm._last_warmup_state_sig == ()
    assert sm._warmed_up_this_life is False


def test_v150_warmup_covers_death_75s_and_records_warmup_served(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_warmup"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        ctrl.cfg.auto_trigger_coach = True
        monkeypatch.setattr(ctrl, "_is_any_coach_online", lambda: True)
        monkeypatch.setattr(ctrl, "_should_defer_death_coach", lambda _s: False)

        triggered: list[str] = []
        monkeypatch.setattr(ctrl, "trigger_coach_request", lambda reason: triggered.append(reason))

        emitted_events: list[tuple[str, str, dict[str, Any]]] = []
        ctrl._devlab_active = True
        tracker = MatchActionTracker(
            lambda cat, etype, payload, _gt: emitted_events.append((cat, etype, dict(payload))),
            is_complete_item_fn=lambda iid: iid in ("6655", "3089", "3157"),
        )
        ctrl._action_tracker = tracker

        assert ctrl.WARMUP_COVERS_DEATH_WINDOW_S == 120.0

        enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 8, 2, 1, 1, ("3071",))
        me_alive = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 8, 2, 0, 3, ("1058",))

        # Start match alive
        ctrl._apply_live_snapshot(LiveMatchSnapshot(180.0, me_alive, (me_alive,), (enemy,), 1600.0))
        triggered.clear()

        # Seed fresh pre_death_warmup cache aged 95s (within 120s window)
        ctrl._latest_advice = CoachAdvicePayload(request_id="req-warm-95s", tactical_summary="预热建议")
        ctrl._last_warmup_advice_mono = time.monotonic() - 95.0
        ctrl._last_warmup_advice_enemy_sig = ctrl.sm._enemy_build_signature(
            LiveMatchSnapshot(240.0, me_alive, (me_alive,), (enemy,), 2000.0),
            store=ctrl.store,
        )

        # Death at gt=240s with 2000G -> served in 0ms from 95s warmup cache!
        me_dead = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", True, 8, 2, 1, 3, ("1058",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(240.0, me_dead, (me_dead,), (enemy,), 2000.0))
        assert triggered == []

        # Respawn to flush death_shop_window and inspect warmup_served / coach_arrived_before_buy
        me_revived = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 8, 2, 1, 3, ("6655",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(255.0, me_revived, (me_revived,), (enemy,), 350.0))
        dws = [p for _c, et, p in emitted_events if et == "death_shop_window"]
        assert len(dws) == 1
        assert dws[0]["warmup_served"] is True
        assert dws[0]["coach_arrived_before_buy"] is True
        assert dws[0]["coach_skip_reason"] == "pre_death_warmup_cached"

        # Now test expired warmup (> 120s, e.g. 125s old) -> MUST fire fresh death_enemy_build_changed
        ctrl.sm._last_death_coach_mono = time.monotonic() - 130.0
        ctrl._last_warmup_advice_mono = time.monotonic() - 125.0
        me_dead2 = PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", True, 10, 2, 2, 3, ("6655",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(355.0, me_dead2, (me_dead2,), (enemy,), 2200.0))
        assert triggered == ["death_enemy_build_changed"]
    finally:
        _cleanup_controller(ctrl, qapp)


def test_v150_lcu_2999_grace_12_conflict_probe_and_degraded_4_enemy_refresh(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert HexHelperController.LCU_2999_DEGRADED_GRACE_FAILURES == 12

    # 1. _probe_port_2999_conflict: reachable TCP 2999 (LoL loading) MUST return False!
    class _DummyConn:
        def __enter__(self) -> "_DummyConn":
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_kw: _DummyConn())
    assert HexHelperController._probe_port_2999_conflict() is False

    # Unreachable TCP 2999 + bind raises WSAEADDRINUSE (10048) -> True (real conflict)
    def _refuse(*_a: Any, **_kw: Any) -> Any:
        raise ConnectionRefusedError("refused")

    class _FailBindSock:
        def bind(self, _addr: Any) -> None:
            err = OSError("address in use")
            err.winerror = 10048
            raise err

        def close(self) -> None:
            return None

    monkeypatch.setattr(socket, "create_connection", _refuse)
    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: _FailBindSock())
    assert HexHelperController._probe_port_2999_conflict() is True

    # Unreachable TCP 2999 + bind succeeds -> False (port free, LoL just hasn't started)
    class _FreeBindSock:
        def bind(self, _addr: Any) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(socket, "socket", lambda *_a, **_kw: _FreeBindSock())
    assert HexHelperController._probe_port_2999_conflict() is False

    # 2. _apply_live_snapshot grace gate + 4-enemy degraded roster refresh & anti-regression
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_degraded"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        ctrl.cfg.auto_trigger_coach = False

        probe_calls: list[int] = []
        monkeypatch.setattr(
            ctrl,
            "_probe_port_2999_conflict",
            lambda: (probe_calls.append(1), True)[1],
        )

        me = PlayerLiveInfo("lcu_order_0", "慎", "Shen", "ORDER", False, 3, 0, 0, 0, ())
        allies = tuple(
            me if i == 0 else PlayerLiveInfo(f"lcu_order_{i}", f"友军{i}", f"Ally{i}", "ORDER", False, 3, 0, 0, 0, ())
            for i in range(5)
        )
        enemies_4 = tuple(
            PlayerLiveInfo(f"lcu_chaos_{i}", f"敌军{i}", f"Enemy{i}", "CHAOS", False, 3, 0, 0, 0, ())
            for i in range(4)
        )
        enemies_5 = tuple(
            PlayerLiveInfo(f"lcu_chaos_{i}", f"敌军{i}", f"Enemy{i}", "CHAOS", False, 3, 0, 0, 0, ())
            for i in range(5)
        )

        # Below 12-failure grace window -> does NOT probe port 2999 conflict
        ctrl.live._consecutive_failures = 5
        ctrl._apply_live_snapshot(LiveMatchSnapshot(5.0, me, allies, enemies_4, 0.0), degraded_2999=True)
        assert probe_calls == []
        assert len(ctrl._last_live.enemies) == 4  # type: ignore[union-attr]

        # Reaches 12-failure grace window -> probes conflict ONCE, and 4 -> 5 enemy roster refreshes cleanly!
        ctrl.live._consecutive_failures = 12
        ctrl._apply_live_snapshot(LiveMatchSnapshot(12.0, me, allies, enemies_5, 0.0), degraded_2999=True)
        assert len(probe_calls) == 1
        assert ctrl._port_2999_conflict_detected is True
        assert len(ctrl._last_live.enemies) == 5  # type: ignore[union-attr]

        # Transient LCU poll dropping back to 4 enemies MUST NOT regress the fuller 5-enemy roster
        ctrl._apply_live_snapshot(LiveMatchSnapshot(13.0, me, allies, enemies_4, 0.0), degraded_2999=True)
        assert len(probe_calls) == 1  # Still probed only once
        assert len(ctrl._last_live.enemies) == 5  # type: ignore[union-attr]
    finally:
        _cleanup_controller(ctrl, qapp)


def test_v150_telemetry_worker_loop_12s_grace_and_roster_refresh(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orig_loop = HexHelperController._telemetry_worker_loop
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_loop"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        ctrl.store._loaded = True  # type: ignore[attr-defined]

        me = PlayerLiveInfo("lcu_order_0", "慎", "Shen", "ORDER", False, 3, 0, 0, 0, ())
        allies = tuple(
            me if i == 0 else PlayerLiveInfo(f"lcu_order_{i}", f"友军{i}", f"Ally{i}", "ORDER", False, 3, 0, 0, 0, ())
            for i in range(5)
        )
        enemies_4 = tuple(
            PlayerLiveInfo(f"lcu_chaos_{i}", f"敌军{i}", f"Enemy{i}", "CHAOS", False, 3, 0, 0, 0, ())
            for i in range(4)
        )
        enemies_5 = tuple(
            PlayerLiveInfo(f"lcu_chaos_{i}", f"敌军{i}", f"Enemy{i}", "CHAOS", False, 3, 0, 0, 0, ())
            for i in range(5)
        )

        # Simulate: 2 ChampSelect ticks (failures must reset to 0), then 13 InProgress ticks without 2999:
        # ticks 1..11 in InProgress MUST NOT enter degraded fallback; tick 12 enters with 4 enemies;
        # tick 13 refreshes to 5 enemies.
        phases = ["ChampSelect", "ChampSelect"] + ["InProgress"] * 13
        tick = {"idx": 0}
        emitted_degraded_enemies: list[int] = []
        emitted_lcu_phases: list[str] = []

        class _FakeLive:
            def __init__(self) -> None:
                self.consecutive_failures = 0

            def poll(self) -> Any:
                self.consecutive_failures += 1
                return None

            def reset_failures(self) -> None:
                self.consecutive_failures = 0

            def close(self) -> None:
                return None

        fallback_call_count = {"n": 0}

        class _FakeLcu:
            def poll(self) -> tuple[str, Any]:
                i = tick["idx"]
                tick["idx"] += 1
                if i >= len(phases):
                    ctrl._stop_event.set()
                    return "None", None
                return phases[i], None

            def get_in_game_fallback_snapshot(self, **_kw: Any) -> LiveMatchSnapshot:
                fallback_call_count["n"] += 1
                enemies = enemies_4 if fallback_call_count["n"] == 1 else enemies_5
                return LiveMatchSnapshot(float(fallback_call_count["n"]), me, allies, enemies, 0.0)

            def close(self) -> None:
                return None

        class _ImmediateWait:
            def __init__(self) -> None:
                self._flag = False

            def is_set(self) -> bool:
                return self._flag

            def set(self) -> None:
                self._flag = True

            def wait(self, _timeout: float = 0.0) -> bool:
                if tick["idx"] >= len(phases):
                    self._flag = True
                return self._flag

        class _StubSignal:
            def emit(self, fn: Any) -> None:
                fn()

        class _StubInvoker:
            invoke = _StubSignal()

        ctrl.live = _FakeLive()  # type: ignore[assignment]
        ctrl.lcu = _FakeLcu()  # type: ignore[assignment]
        ctrl._stop_event = _ImmediateWait()  # type: ignore[assignment]
        ctrl.ui_invoker = _StubInvoker()  # type: ignore[assignment]
        monkeypatch.setattr(ctrl, "_apply_live_snapshot", lambda snap, degraded_2999=False: emitted_degraded_enemies.append(len(snap.enemies)) if degraded_2999 else None)
        monkeypatch.setattr(ctrl, "_apply_lcu_poll", lambda p, _c: emitted_lcu_phases.append(p))

        # Invoke the real unpatched _telemetry_worker_loop method
        orig_loop(ctrl)

        # 2 ChampSelect + 11 grace InProgress ticks went to _apply_lcu_poll; ticks 12 and 13 entered degraded mode!
        assert emitted_lcu_phases == ["ChampSelect", "ChampSelect"] + ["InProgress"] * 11
        assert emitted_degraded_enemies == [4, 5]
    finally:
        _cleanup_controller(ctrl, qapp)


def test_v150_hud_scale_passthrough_for_verify_transmute_and_strip_crop(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata_hud"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        ctrl.store = _make_store(tmp_path)
        ctrl.sm._store = ctrl.store
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "99"
        ctrl.cfg.auto_trigger_coach = False

        hud_scale = 0.65
        ctrl._hud_scale = hud_scale

        matcher = AugmentIconMatcher(cache_dir=None, allow_download=False)
        ico_1001 = _make_distinct_icon(111)
        ico_2001 = _make_distinct_icon(222)
        matcher.register_template("1001", ico_1001, "珠光护手")
        matcher.register_template("2001", ico_2001, "炼狱导管")
        ctrl.icon_matcher = matcher

        rect = WindowRect(hwnd=1, left=0, top=0, width=2560, height=1440)
        rects = dict(matcher.get_hud_slot_rects(2560, 1440, hud_scale=hud_scale))

        # 1. Verify _verify_picked_augment_from_frame passes hud_scale=0.65 and matches scaled HUD icon
        frame_verify = np.full((1440, 2560, 3), 18, dtype=np.uint8)
        x0, y0, x1, y1 = rects[0]
        frame_verify[y0:y1, x0:x1] = cv2.resize(ico_1001, (x1 - x0, y1 - y0), interpolation=cv2.INTER_AREA)

        # Without hud_scale (1.0), icon at 0.65 is missed; WITH controller._hud_scale=0.65, it matches!
        assert matcher.verify_picked_augment(frame_verify, rect, candidate_ids=("1001", "2001"), slot_index_hint=0, hud_scale=1.0) is None
        hit = ctrl._verify_picked_augment_from_frame(
            frame_verify,
            game_rect=rect,
            candidate_ids=("1001", "2001"),
            committed_id="",
            slot_index_hint=0,
        )
        assert hit is not None
        assert hit.augment_id == "1001"
        assert matcher.last_verify_diagnostics().get("hud_scale") == pytest.approx(hud_scale, abs=0.001)

        # 2. Verify _try_resolve_transmute_via_icon passes hud_scale=0.65 and resolves pending Transmute
        ctrl.sm.add_selected_augment("1238")
        ctrl.sm.start_transmute_if_applicable("1238", now_mono=10.0)
        frame_tx = np.full((1440, 2560, 3), 18, dtype=np.uint8)
        tx0, ty0, tx1, ty1 = rects[1]
        frame_tx[ty0:ty1, tx0:tx1] = cv2.resize(ico_2001, (tx1 - tx0, ty1 - ty0), interpolation=cv2.INTER_AREA)
        tx_hit = ctrl._try_resolve_transmute_via_icon(frame_tx, rect)
        assert tx_hit is not None
        assert tx_hit.augment_id == "2001"
        assert "2001" in ctrl.sm.selected_augments

        # 3. Verify _emit_icon_verify_devlab passes hud_scale=0.65 to build_hud_icon_strip_crop
        strip_scales: list[float] = []
        orig_strip = matcher.build_hud_icon_strip_crop

        def _spy_strip(fb: np.ndarray, gr: Any, *, tile_size: int = 28, hud_scale: float = 1.0) -> np.ndarray | None:
            strip_scales.append(hud_scale)
            return orig_strip(fb, gr, tile_size=tile_size, hud_scale=hud_scale)

        monkeypatch.setattr(matcher, "build_hud_icon_strip_crop", _spy_strip)
        enqueued: list[str] = []

        class _StubDevLab:
            enable_roi_snapshots = True

            def record_event(self, *_a: Any, **_kw: Any) -> int:
                return 7

            def enqueue_roi_snapshot(self, name: str, _img: Any, **_kw: Any) -> None:
                enqueued.append(name)

        ctrl._devlab_active = True
        ctrl._devlab = _StubDevLab()
        ctrl._emit_icon_verify_devlab(
            frame_verify,
            rect,
            committed_id="1001",
            candidate_ids=("1001",),
            slot_index_hint=0,
            action_taken="corrected",
            hit=None,
        )
        assert strip_scales == [pytest.approx(hud_scale)]
        assert enqueued == ["hud_icon_strip"]

        # 4. Legacy stub without hud_scale parameter still works via TypeError fallback
        legacy_calls: list[str] = []

        class _LegacyMatcher:
            template_count = 1

            def verify_picked_augment(self, _f: Any, _r: Any, candidate_ids=None, slot_index_hint=None):
                legacy_calls.append("verify")
                return None

            def build_hud_icon_strip_crop(self, _f: Any, _r: Any):
                legacy_calls.append("strip")
                return np.zeros((28, 112, 3), dtype=np.uint8)

        ctrl.icon_matcher = _LegacyMatcher()  # type: ignore[assignment]
        ctrl._verify_picked_augment_from_frame(
            frame_verify,
            game_rect=rect,
            candidate_ids=("1001",),
            committed_id="1001",
            slot_index_hint=0,
        )
        assert legacy_calls == ["verify", "strip"]
    finally:
        _cleanup_controller(ctrl, qapp)
