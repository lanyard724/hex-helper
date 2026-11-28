"""Phase C3 end-to-end integration tests for src/hex_helper/app.py wiring.

Covers live controller wiring on real 16.23/normal & 16.22/normal datasets:
- P0-5 live my_stats crit overflow guard across picker badges, smart gate, combo overlay, and coach payload crit_chance_pct
- P0-6 3081 Destiny card hint & negative payoff warning rendered through _render_augment_badges -> AugmentBadgesOverlay
- P0-7 comp-aware augment roadmap on death/warmup/augment_selected -> combo_overlay + CoachRequestPayload + high-SNR payload + EdgeBuildDrawer next_augment_goal + 4/4 auto-clear
- P1-1 hero-gated 1353 Tank Engine consistency in CoachRequestPayload.special_augment_builds and picker archetype hints
- P0-3 LookupWindow data_band_mode save -> config persistence -> live optimize_next_items / overlay refresh
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication, QLabel

from hex_helper import app as app_mod
from hex_helper.app import GameState, HexHelperController
from hex_helper.bridge.protocol import CoachAdvicePayload
from hex_helper.coach.gemini_coach import build_high_snr_prompt_payload
from hex_helper.config import load_config
from hex_helper.data.provider import DatasetDescriptor
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.vision.capture import CaptureResult, WindowRect
from hex_helper.vision.detector import DetectedCard

LIVE_1623_NORMAL = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_1623_NORMAL",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal",
    )
)
LIVE_1623_FP = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_1623_FP",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\first-prismatic",
    )
)
LIVE_1622_NORMAL_CANDIDATES = (
    Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\aramhex_16_22"),
    Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\aramhex_16_22_normal"),
)
LIVE_CACHE_ROOT = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_CACHE_ROOT",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache",
    )
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


@pytest.fixture(scope="module")
def live_1623_store() -> DataStore:
    if not (LIVE_1623_NORMAL / "heroes.json").is_file():
        pytest.skip("Local 16.23/normal dataset not available on disk")
    static = StaticAssetsManager(LIVE_CACHE_ROOT, fetcher=lambda _u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )
    if (LIVE_1623_FP / "heroes.json").is_file():
        store.preload_alternate_mode(LIVE_1623_FP, "first-prismatic")
    return store


@pytest.fixture(scope="module")
def live_1622_high_band_store(tmp_path_factory: pytest.TempPathFactory) -> DataStore:
    for cand in LIVE_1622_NORMAL_CANDIDATES:
        if (cand / "heroes.json").is_file() and (cand / "bands" / "high" / "heroes.json").is_file():
            static = StaticAssetsManager(LIVE_CACHE_ROOT, fetcher=lambda _u: b"{}")
            store = DataStore(static)
            store.load_from_dir(
                DatasetDescriptor("16.22", "normal", "常规", "patches/16.22/normal", "", True),
                cand,
            )
            return store
    # Self-contained fallback with synthetic bands/high cloned from 16.23/normal so test never skips
    if not (LIVE_1623_NORMAL / "heroes.json").is_file():
        pytest.skip("Neither 16.22 nor 16.23 local dataset available on disk")
    import shutil

    synth_dir = tmp_path_factory.mktemp("synth_high_band")
    for fname in ("heroes.json", "augments.json", "hero_augments.json", "hero_item_recommendations.json", "core_builds.json", "hero_augment_items.json"):
        src = LIVE_1623_NORMAL / fname
        if src.is_file():
            shutil.copy2(src, synth_dir / fname)
    high_dir = synth_dir / "bands" / "high"
    high_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("heroes.json", "hero_augments.json", "hero_item_recommendations.json", "core_builds.json", "hero_augment_items.json"):
        src = LIVE_1623_NORMAL / fname
        if src.is_file():
            shutil.copy2(src, high_dir / fname)
    static = StaticAssetsManager(LIVE_CACHE_ROOT, fetcher=lambda _u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.22", "normal", "常规", "patches/16.22/normal", "", True),
        synth_dir,
    )
    return store


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


def _make_controller(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: DataStore,
    subdir: str,
) -> HexHelperController:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / subdir))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    ctrl._vision_timer.stop()
    ctrl._heartbeat_timer.stop()
    ctrl.store = store
    ctrl.sm._store = store
    ctrl.lookup_window.bind_store(store)
    ctrl.cfg.auto_trigger_coach = False
    ctrl.cfg.smart_picker_ai_gate = False
    ctrl.cfg.bridge_enabled = False
    ctrl.cfg.coach_mode = "off"
    return ctrl


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


def _card(store: DataStore, slot: int, aid: str) -> DetectedCard:
    aug = store.get_augment(aid)
    assert aug is not None, f"Augment {aid} missing from dataset"
    x0 = 360 + slot * 440
    return DetectedCard(
        slot_index=slot,
        augment=aug,
        confidence=0.98,
        screen_box=(x0, 420, x0 + 260, 460),
        rarity_hint=aug.rarity,
    )


def test_c3_crit_overflow_my_stats_and_crit_chance_pct_end_to_end(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_1623_store: DataStore,
) -> None:
    ctrl = _make_controller(qapp, tmp_path, monkeypatch, live_1623_store, "appdata_crit")
    try:
        jinx = PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 13, 8, 2, 10, ("3031", "3085", "3036", "3094"))
        enemy = PlayerLiveInfo("e1", "拉克丝", "Lux", "CHAOS", False, 13, 4, 5, 8, ("6655", "3089"))
        snap_full_crit = LiveMatchSnapshot(
            game_time=620.0,
            my_player=jinx,
            allies=(jinx,),
            enemies=(enemy,),
            current_gold=1800.0,
            my_stats={"critChance": 1.0, "attackDamage": 290.0},
        )
        ctrl._apply_live_snapshot(snap_full_crit)
        ctrl.sm.state = GameState.AUGMENT_PICKING
        ctrl.sm.current_hero_id = "222"
        ctrl.sm.selected_augments = ["1079"]

        # 1047 (关键暴击) vs two eligible non-overflow gold cards on Jinx
        captured_items: list[Any] = []
        orig_render = ctrl.augment_overlay.render_cards

        def _spy_render(items: Any, *args: Any, **kwargs: Any) -> Any:
            captured_items[:] = list(items)
            return orig_render(items, *args, **kwargs)

        monkeypatch.setattr(ctrl.augment_overlay, "render_cards", _spy_render)
        cards = (
            _card(live_1623_store, 0, "1047"),
            _card(live_1623_store, 1, "1077"),
            _card(live_1623_store, 2, "1080"),
        )
        ctrl.sm.active_cards = cards
        ctrl._render_augment_badges(cards)
        qapp.processEvents()

        # 1. evaluate_picker_combos received my_stats -> 1047 flagged as 暴击溢出·废卡 (-35.0)
        verdict = ctrl._last_combo_verdict
        assert verdict is not None
        by_id = {vc.augment_id: vc for vc in verdict.cards}
        assert by_id["1047"].verdict == "conflict"
        assert by_id["1047"].verdict_label == "暴击溢出·废卡"
        assert by_id["1047"].combo_delta == pytest.approx(-35.0)

        # 2. Rendered augment_overlay cards MUST NOT mark 1047 as BEST
        assert any(it.score.augment_id != "1047" and it.is_best for it in captured_items)
        for it in captured_items:
            if it.score.augment_id == "1047":
                assert it.is_best is False

        # 3. _should_skip_picker_ai_call uses my_stats even when _last_combo_verdict is cleared
        ctrl._last_combo_verdict = None
        ctrl.cfg.smart_picker_ai_gate = True
        from hex_helper.scoring.tier import AugmentScore

        fake_scored = [
            (cards[0], AugmentScore("1047", "关键暴击", "gold", "S", 0.62, 0.61, 0.10, 5000, False, percentile=99.0)),
            (cards[1], AugmentScore("1077", "灵魂虹吸", "gold", "B", 0.52, 0.52, 0.01, 5000, False, percentile=55.0)),
            (cards[2], AugmentScore("1080", "会心防御", "gold", "B", 0.51, 0.51, 0.00, 5000, False, percentile=50.0)),
        ]
        should_skip, _ = ctrl._should_skip_picker_ai_call(cards, scored_cards=fake_scored)
        assert should_skip is False
        assert ctrl._last_smart_gate_eval["reason"] == "combo_conflict"
        ctrl.cfg.smart_picker_ai_gate = False

        # 4. trigger_coach_request attaches crit_chance_pct=100 (and omits when 0)
        req_pick = ctrl.trigger_coach_request("augment_picking")
        assert req_pick is not None
        assert req_pick.my_hero.get("crit_chance_pct") == 100
        high_snr_pick = build_high_snr_prompt_payload(req_pick, live_1623_store)
        assert high_snr_pick["my_hero"].get("crit_chance_pct") == 100

        # 5. build_combo_plan in _refresh_combo_plan_view filters out 1047 at >=75% crit
        plan = ctrl._refresh_combo_plan_view()
        assert all(t.augment_id != "1047" for t in plan.recommended_targets)

        # 6. Zero-crit champion (Mundo with Heartsteel) MUST omit crit_chance_pct completely
        mundo = PlayerLiveInfo("me", "蒙多医生", "DrMundo", "ORDER", False, 10, 2, 1, 8, ("3084",))
        ctrl._last_live = LiveMatchSnapshot(
            game_time=420.0,
            my_player=mundo,
            allies=(mundo,),
            enemies=(enemy,),
            current_gold=900.0,
            my_stats={"critChance": 0.0, "maxHealth": 4200.0},
        )
        ctrl._match_last_my_stats = {"critChance": 0.0, "maxHealth": 4200.0}
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "36"
        ctrl.sm.selected_augments = []
        ctrl.sm.active_cards = ()
        req_mundo = ctrl.trigger_coach_request("manual_f6")
        assert req_mundo is not None
        assert "crit_chance_pct" not in req_mundo.my_hero
    finally:
        _cleanup_controller(ctrl, qapp)


def test_c3_destiny_3081_card_hint_and_negative_warning_in_app_overlay(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_1623_store: DataStore,
) -> None:
    ctrl = _make_controller(qapp, tmp_path, monkeypatch, live_1623_store, "appdata_destiny")
    try:
        captured_items: list[Any] = []
        orig_render = ctrl.augment_overlay.render_cards

        def _spy_render(items: Any, *args: Any, **kwargs: Any) -> Any:
            captured_items[:] = list(items)
            return orig_render(items, *args, **kwargs)

        monkeypatch.setattr(ctrl.augment_overlay, "render_cards", _spy_render)

        # Case 1: Akali (84) offered 3081 -> positive binding hint, no negative warning
        ctrl.sm.state = GameState.AUGMENT_PICKING
        ctrl.sm.current_hero_id = "84"
        ctrl.sm.selected_augments = []
        akali_cards = (
            _card(live_1623_store, 0, "3081"),
            _card(live_1623_store, 1, "1077"),
            _card(live_1623_store, 2, "1080"),
        )
        ctrl._render_augment_badges(akali_cards)
        qapp.processEvents()

        akali_item = next(it for it in captured_items if it.score.augment_id == "3081")
        assert akali_item.destiny_hint.startswith("◈ 绑定首选：")
        assert "(+" in akali_item.destiny_hint
        assert akali_item.destiny_warning == ""
        akali_labels = [lbl.text() for lbl in ctrl.augment_overlay.findChildren(QLabel)]
        assert akali_item.destiny_hint in akali_labels

        # Case 2: Rell (526) offered 3081 -> negative binding warning pill rendered
        ctrl.sm.current_hero_id = "526"
        ctrl._render_augment_badges(akali_cards)
        qapp.processEvents()

        rell_item = next(it for it in captured_items if it.score.augment_id == "3081")
        assert rell_item.destiny_hint.startswith("◈ 绑定首选：")
        assert rell_item.destiny_warning == "▽ 绑定收益偏弱 · 慎选"
        rell_labels = [lbl.text() for lbl in ctrl.augment_overlay.findChildren(QLabel)]
        assert rell_item.destiny_hint in rell_labels
        assert "▽ 绑定收益偏弱 · 慎选" in rell_labels
    finally:
        _cleanup_controller(ctrl, qapp)


def test_c3_death_comp_aware_combo_overlay_next_aug_goal_and_4_of_4_clear(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_1623_store: DataStore,
) -> None:
    ctrl = _make_controller(qapp, tmp_path, monkeypatch, live_1623_store, "appdata_comp")
    try:
        jinx = PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", True, 11, 5, 3, 9, ("3031",))
        ally_lux = PlayerLiveInfo("a2", "拉克丝", "Lux", "ORDER", False, 11, 4, 4, 12, ("6655",))
        ally_braum = PlayerLiveInfo("a3", "布隆", "Braum", "ORDER", False, 11, 1, 5, 18, ("3084",))
        # Enemy 3-tank frontline + 2 carries
        e1 = PlayerLiveInfo("e1", "蒙多医生", "DrMundo", "CHAOS", False, 12, 7, 2, 10, ("3084", "3083", "3075"))
        e2 = PlayerLiveInfo("e2", "熔岩巨兽", "Malphite", "CHAOS", False, 11, 4, 4, 15, ("3084", "3075", "3110"))
        e3 = PlayerLiveInfo("e3", "奥恩", "Ornn", "CHAOS", False, 11, 3, 3, 17, ("3084", "3068", "3143"))
        e4 = PlayerLiveInfo("e4", "拉克丝", "Lux", "CHAOS", False, 11, 5, 4, 12, ("6655",))
        e5 = PlayerLiveInfo("e5", "凯特琳", "Caitlyn", "CHAOS", False, 11, 6, 3, 8, ("6676",))

        snap_dead = LiveMatchSnapshot(
            game_time=540.0,
            my_player=jinx,
            allies=(jinx, ally_lux, ally_braum),
            enemies=(e1, e2, e3, e4, e5),
            current_gold=2600.0,
            my_stats={"critChance": 0.25, "attackDamage": 185.0},
        )
        ctrl._apply_live_snapshot(snap_dead)
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "222"
        ctrl.sm.selected_augments = ["1079"]
        ctrl._refresh_drawer_view()
        ctrl.combo_overlay.set_expanded(True)
        qapp.processEvents()

        # 1. Local comp-aware plan vs 3 tanks tags 打敌多坦 and renders on combo_overlay
        assert ctrl._last_threat is not None
        assert ctrl._last_threat.enemy_tank_count >= 2
        plan = ctrl._last_combo_plan
        assert plan is not None
        assert plan.recommended_targets
        assert any(t.comp_tag == "打敌多坦" for t in plan.recommended_targets)
        for t in plan.recommended_targets:
            assert live_1623_store.is_augment_eligible_for_hero("222", t.augment_id)
        combo_labels = [lbl.text() for lbl in ctrl.combo_overlay.targets_list_wrap.findChildren(QLabel)]
        assert "打敌多坦" in combo_labels

        # 2. Death trigger builds CoachRequestPayload with comp-aware top_targets -> C1 next_aug_pool
        req_death = ctrl.trigger_coach_request("death_enemy_build_changed")
        assert req_death is not None
        assert req_death.my_hero.get("crit_chance_pct") == 25
        top_targets = req_death.augment_combo_context.get("top_targets") or []
        assert any(t.get("comp_tag") == "打敌多坦" for t in top_targets)
        hs_payload = build_high_snr_prompt_payload(req_death, live_1623_store)
        next_pool = (hs_payload.get("augment_combo_context") or {}).get("next_aug_pool") or []
        assert len(next_pool) >= 1
        assert any("打敌多坦" in item for item in next_pool)

        # 3. Receiving AI Coach advice with next_augment_goal renders capsule in edge_drawer
        adv = CoachAdvicePayload(
            request_id=req_death.request_id,
            buy_now="先出多米尼克领主的致意打三坦",
            tactical_summary="敌方三坦高甲，优先轻语破甲",
            next_augment_goal="下轮追灵魂虹吸打敌三坦",
        )
        ctrl._on_coach_advice(adv)
        qapp.processEvents()
        assert ctrl.edge_drawer.next_aug_goal_lbl.isHidden() is False
        assert "下轮追灵魂虹吸打敌三坦" in ctrl.edge_drawer.next_aug_goal_lbl.text()

        # 4. Reaching 4/4 augments clears combo_overlay targets, edge_drawer goal capsule, and AI next_aug_pool
        ctrl.sm.selected_augments = ["1079", "1225", "1115", "1328"]
        ctrl._refresh_drawer_view()
        req_full = ctrl.trigger_coach_request("augment_selected")
        qapp.processEvents()

        assert ctrl._last_combo_plan is not None
        assert ctrl._last_combo_plan.recommended_targets == ()
        full_combo_labels = [lbl.text() for lbl in ctrl.combo_overlay.targets_list_wrap.findChildren(QLabel)]
        assert full_combo_labels == ["✓ 已持有 4 枚海克斯 · 专注出装"]
        assert ctrl.edge_drawer.next_aug_goal_lbl.isHidden() is True
        assert ctrl.edge_drawer.next_aug_goal_lbl.text() == ""
        assert req_full is not None
        assert req_full.augment_combo_context.get("top_targets") == []
        hs_full = build_high_snr_prompt_payload(req_full, live_1623_store)
        assert "augment_combo_context" not in hs_full

        # Even if a late/external advice arrives with non-empty next_augment_goal at 4/4, app clears it
        adv_late = CoachAdvicePayload(
            request_id=req_full.request_id,
            buy_now="补无尽之刃",
            next_augment_goal="不应该显示的下轮海克斯",
        )
        ctrl._on_coach_advice(adv_late)
        qapp.processEvents()
        assert ctrl._latest_advice is not None
        assert ctrl._latest_advice.next_augment_goal == ""
        assert ctrl.edge_drawer.next_aug_goal_lbl.isHidden() is True
    finally:
        _cleanup_controller(ctrl, qapp)


def test_c3_tank_engine_1353_hero_gating_in_coach_payload_and_picker_badge(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_1623_store: DataStore,
) -> None:
    ctrl = _make_controller(qapp, tmp_path, monkeypatch, live_1623_store, "appdata_tank_gate")
    try:
        enemy = PlayerLiveInfo("e1", "拉克丝", "Lux", "CHAOS", False, 9, 3, 2, 5, ("6655",))

        # Vladimir (8) holding 1353 fails Heartsteel gate -> NO force 心之钢 in AI payload or picker badge
        vlad = PlayerLiveInfo("me", "弗拉基米尔", "Vladimir", "ORDER", False, 9, 4, 2, 6, ("3152",))
        ctrl._last_live = LiveMatchSnapshot(360.0, vlad, (vlad,), (enemy,), 1500.0)
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "8"
        ctrl.sm.selected_augments = ["1353"]
        ctrl.sm.active_cards = ()
        req_vlad = ctrl.trigger_coach_request("augment_selected")
        assert req_vlad is not None
        sp_vlad = [b for b in req_vlad.special_augment_builds if b.get("id") == "1353"]
        assert len(sp_vlad) == 1
        assert not any("心之钢" in str(x) for x in (sp_vlad[0].get("force_items") or []))
        assert not any("心之钢" in str(x) for x in (sp_vlad[0].get("core_items") or []))

        # Picker badge on Vladimir offering 1353 must NOT advertise 心之钢
        captured_items: list[Any] = []
        orig_render = ctrl.augment_overlay.render_cards

        def _spy_render(items: Any, *args: Any, **kwargs: Any) -> Any:
            captured_items[:] = list(items)
            return orig_render(items, *args, **kwargs)

        monkeypatch.setattr(ctrl.augment_overlay, "render_cards", _spy_render)
        ctrl.sm.state = GameState.AUGMENT_PICKING
        ctrl.sm.selected_augments = []
        cards_1353 = (
            _card(live_1623_store, 0, "1353"),
            _card(live_1623_store, 1, "1077"),
            _card(live_1623_store, 2, "1080"),
        )
        ctrl._render_augment_badges(cards_1353)
        vlad_badge = next(it for it in captured_items if it.score.augment_id == "1353")
        assert "心之钢" not in vlad_badge.archetype_hint

        # Mundo (36) holding 1353 passes Heartsteel gate -> DOES force 心之钢
        mundo = PlayerLiveInfo("me", "蒙多医生", "DrMundo", "ORDER", False, 9, 3, 2, 8, ())
        ctrl._last_live = LiveMatchSnapshot(360.0, mundo, (mundo,), (enemy,), 1500.0)
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "36"
        ctrl.sm.selected_augments = ["1353"]
        req_mundo = ctrl.trigger_coach_request("augment_selected")
        assert req_mundo is not None
        sp_mundo = [b for b in req_mundo.special_augment_builds if b.get("id") == "1353"]
        assert len(sp_mundo) == 1
        assert any("心之钢" in str(x) for x in (sp_mundo[0].get("force_items") or []))
    finally:
        _cleanup_controller(ctrl, qapp)


def test_c3_data_band_mode_settings_save_and_live_refresh(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_1622_high_band_store: DataStore,
    live_1623_store: DataStore,
) -> None:
    ctrl = _make_controller(qapp, tmp_path, monkeypatch, live_1622_high_band_store, "appdata_band")
    try:
        assert live_1622_high_band_store.has_high_band() is True
        assert "高分" in ctrl.lookup_window.data_band_hint_lbl.text()

        jinx = PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 6, 1, 0, 2, ())
        enemy = PlayerLiveInfo("e1", "拉克丝", "Lux", "CHAOS", False, 6, 1, 1, 1, ("6655",))
        ctrl._last_live = LiveMatchSnapshot(180.0, jinx, (jinx,), (enemy,), 1400.0)
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "222"

        seen_band_modes: list[str] = []
        orig_opt = app_mod.optimize_next_items

        def _spy_opt(*args: Any, **kwargs: Any) -> Any:
            seen_band_modes.append(str(kwargs.get("data_band_mode", "MISSING")))
            return orig_opt(*args, **kwargs)

        monkeypatch.setattr(app_mod, "optimize_next_items", _spy_opt)

        # Initial drawer refresh uses "all"
        ctrl._refresh_drawer_view()
        assert seen_band_modes[-1] == "all"

        # Switch LookupWindow data_band_combo -> "high" and trigger save (same cfg object mutation path!)
        idx_high = ctrl.lookup_window.data_band_combo.findData("high")
        assert idx_high >= 0
        ctrl.lookup_window.data_band_combo.setCurrentIndex(idx_high)
        ctrl.lookup_window._save_clicked()
        qapp.processEvents()

        assert ctrl.cfg.data_band_mode == "high"
        assert load_config().data_band_mode == "high"
        assert seen_band_modes[-1] == "high"

        # Switch to "blended" -> refreshes again immediately
        idx_blended = ctrl.lookup_window.data_band_combo.findData("blended")
        ctrl.lookup_window.data_band_combo.setCurrentIndex(idx_blended)
        ctrl.lookup_window._save_clicked()
        qapp.processEvents()
        assert ctrl.cfg.data_band_mode == "blended"
        assert seen_band_modes[-1] == "blended"

        # When switched to a dataset without bands/high (16.23/normal), UI shows fallback label cleanly
        ctrl.store = live_1623_store
        ctrl.sm._store = live_1623_store
        ctrl.lookup_window.bind_store(live_1623_store)
        assert ctrl.lookup_window.data_band_hint_lbl.text() == "当前版本全量数据"
        ctrl._refresh_drawer_view()
        assert seen_band_modes[-1] == "blended"
    finally:
        _cleanup_controller(ctrl, qapp)


def test_c3_dive_and_zero_tank_team_profiles_boost_survivability(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_1623_store: DataStore,
) -> None:
    ctrl = _make_controller(qapp, tmp_path, monkeypatch, live_1623_store, "appdata_dive")
    try:
        cait = PlayerLiveInfo("me", "凯特琳", "Caitlyn", "ORDER", True, 10, 3, 4, 6, ("6676",))
        a2 = PlayerLiveInfo("a2", "拉克丝", "Lux", "ORDER", False, 10, 2, 4, 8, ("6655",))
        a3 = PlayerLiveInfo("a3", "泽拉斯", "Xerath", "ORDER", False, 10, 3, 3, 7, ("6655",))
        a4 = PlayerLiveInfo("a4", "迦娜", "Janna", "ORDER", False, 10, 0, 3, 12, ("3504",))
        a5 = PlayerLiveInfo("a5", "布兰德", "Brand", "ORDER", False, 10, 4, 5, 6, ("6653",))

        # Enemy squishy dive assassins with snowballs (no HP tank items -> dive threat, not multi-tank)
        e1 = PlayerLiveInfo("e1", "劫", "Zed", "CHAOS", False, 11, 9, 2, 5, ("6697", "3142"), ("闪现", "雪球"))
        e2 = PlayerLiveInfo("e2", "泰隆", "Talon", "CHAOS", False, 11, 8, 3, 6, ("6697", "3142"), ("闪现", "雪球"))
        e3 = PlayerLiveInfo("e3", "卡兹克", "Khazix", "CHAOS", False, 11, 7, 2, 7, ("6697", "3142"), ("闪现", "雪球"))
        e4 = PlayerLiveInfo("e4", "拉克丝", "Lux", "CHAOS", False, 10, 3, 3, 9, ("6655",), ("闪现", "清晰术"))
        e5 = PlayerLiveInfo("e5", "金克丝", "Jinx", "CHAOS", False, 10, 4, 2, 8, ("3031",), ("闪现", "治疗"))

        ctrl._last_live = LiveMatchSnapshot(
            game_time=480.0,
            my_player=cait,
            allies=(cait, a2, a3, a4, a5),
            enemies=(e1, e2, e3, e4, e5),
            current_gold=1900.0,
        )
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.current_hero_id = "51"
        ctrl.sm.selected_augments = ["1115"]

        ally_profs, enemy_profs = ctrl._current_team_profiles()
        assert "TANK" not in ally_profs
        assert sum(1 for p in enemy_profs if p == "FIGHTER") >= 2

        ctrl._refresh_drawer_view()
        plan = ctrl._last_combo_plan
        assert plan is not None
        assert any(t.comp_tag == "防突进保命" for t in plan.recommended_targets)
    finally:
        _cleanup_controller(ctrl, qapp)
