"""PySide6 regression tests for independent dual-mode AugmentComboOverlay."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from hex_helper.data.provider import AugmentInfo, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import build_combo_plan, evaluate_picker_combos


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _build_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path)
    static.items["6653"] = ItemStaticInfo(
        item_id="6653",
        name="兰德里的折磨",
        gold=3000,
        category="complete_equipment",
        tags=("SpellDamage", "Health"),
        stats={"FlatMagicDamageMod": 70.0, "FlatHPPoolMod": 300.0},
    )
    store = DataStore(static)
    store._heroes["236"] = HeroStat("236", "卢锡安", "Lucian", 10000, 5100, 0.51, 0.08, 10)
    store._heroes["21"] = HeroStat("21", "厄运小姐", "MissFortune", 12000, 5300, 0.53, 0.09, 10)
    for aid, nm, rar in (
        ("3018", "大腕", "prismatic"),
        ("1048", "珠光护手", "prismatic"),
        ("3123", "珠光手镯", "gold"),
        ("1019", "全凭身法", "prismatic"),
        ("1388", "无限循环往复", "prismatic"),
        ("1045", "炼狱导管", "prismatic"),
        ("1133", "魔法飞弹", "gold"),
        ("1022", "灵巧", "silver"),
        ("1225", "双刀流", "prismatic"),
        ("1013", "星界躯体", "gold"),
    ):
        store._augments[aid] = AugmentInfo(aid, nm, rar)
    return store


def test_combo_overlay_collapsed_stability_and_left_edge_dock(tmp_path: Path):
    from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay

    app = _qt_app()
    store = _build_store(tmp_path)
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    geo = screen.geometry()

    ov = AugmentComboOverlay()
    plan = build_combo_plan(store, "236", ["3018"], ())
    ov.show_overlay()
    app.processEvents()
    ov.set_plan(plan)
    app.processEvents()

    assert ov.width() == AugmentComboOverlay.COLLAPSED_W
    assert ov.x() == geo.left()
    base_h = ov.height()
    assert base_h <= 220

    # 100 repeated refreshes while collapsed must cause ZERO vertical drift
    for _ in range(100):
        ov.set_plan(plan)
    assert ov.width() == AugmentComboOverlay.COLLAPSED_W
    assert ov.height() == base_h
    assert ov.get_exclude_rect() is not None

    ov.close()
    app.processEvents()


def test_combo_overlay_dual_mode_picker_auto_expand_and_restore(tmp_path: Path):
    from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay

    app = _qt_app()
    store = _build_store(tmp_path)

    ov = AugmentComboOverlay()
    plan = build_combo_plan(store, "21", ["1388"], ("6653",))
    ov.set_plan(plan)
    ov.show_overlay()
    app.processEvents()

    # Starts collapsed in plan mode
    assert ov.is_expanded is False
    assert ov.mode == "plan"

    # Entering 3-card picker switches to picker mode and auto-expands
    offered = [
        (0, store.get_augment("1022")),  # conflict
        (1, store.get_augment("1225")),  # conflict
        (2, store.get_augment("1045")),  # jackpot
    ]
    verdict = evaluate_picker_combos(store, "21", ["1388"], ("6653",), offered)
    ov.enter_picker_mode(verdict)
    app.processEvents()

    from PySide6.QtGui import QGuiApplication

    assert ov.mode == "picker"
    assert ov.is_expanded is True
    screen = QGuiApplication.primaryScreen()
    expected_w = (
        AugmentComboOverlay.compute_expanded_width(screen.geometry().width())
        if screen is not None
        else AugmentComboOverlay.EXPANDED_W
    )
    assert ov.width() == expected_w
    assert len(ov._picker_rows) == 3

    # Exiting picker restores plan mode and prior collapsed preference
    ov.exit_picker_mode()
    app.processEvents()
    assert ov.mode == "plan"
    assert ov.is_expanded is False
    assert ov.width() == AugmentComboOverlay.COLLAPSED_W

    # Full-lock hides overlay cleanly
    ov.set_full_locked(True, "维护中")
    assert ov.isVisible() is False

    ov.close()
    app.processEvents()


def test_combo_overlay_left_anchor_and_custom_pos(tmp_path: Path):
    from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay

    app = _qt_app()
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    geo = screen.geometry()

    ov_left = AugmentComboOverlay(initial_pos=[geo.left() + 8, geo.top() + 260])
    assert ov_left._anchor_left is True
    assert ov_left.x() == geo.left()
    ov_left.close()

    ov_mid = AugmentComboOverlay(initial_pos=[geo.left() + 340, geo.top() + 260])
    assert ov_mid._anchor_left is False
    assert ov_mid.x() == geo.left() + 340
    ov_mid.close()
    app.processEvents()


def test_config_persists_combo_overlay_fields(tmp_path: Path):
    from hex_helper.config import AppConfig, load_config, save_config

    cfg = AppConfig()
    assert cfg.hotkeys.toggle_combo == "F7"
    assert cfg.combo_overlay_enabled is True
    assert cfg.combo_drawer_pos is None

    cfg.combo_overlay_enabled = False
    cfg.combo_drawer_pos = [16, 320]
    cfg.hotkeys.toggle_combo = "F9"
    p = tmp_path / "config.json"
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.combo_overlay_enabled is False
    assert loaded.combo_drawer_pos == [16, 320]
    assert loaded.hotkeys.toggle_combo == "F9"


def test_protocol_and_coach_prompt_include_combo_context(tmp_path: Path):
    from hex_helper.bridge.protocol import CoachRequestPayload
    from hex_helper.coach.gemini_coach import build_coach_prompt_payload
    from hex_helper.scoring.augment_combo import export_combo_coach_context

    store = _build_store(tmp_path)
    plan = build_combo_plan(store, "236", ["3018"], ())
    ctx = export_combo_coach_context(plan)

    req = CoachRequestPayload(
        request_id="req-combo-1",
        trigger_reason="augment_picking",
        timestamp="2026-11-22T16:15:00+08:00",
        dataset_patch="16.23",
        my_hero={"hero_id": "236", "name": "卢锡安", "gold": 1400, "items": []},
        augment_combo_context=ctx,
    )
    roundtrip = CoachRequestPayload.from_dict(req.to_dict())
    assert roundtrip.augment_combo_context["primary_profile"] == "CRIT_SPELL"

    prompt_payload = build_coach_prompt_payload(req, store)
    assert prompt_payload["augment_combo_context"]["primary_profile"] == "CRIT_SPELL"


def test_lookup_window_combo_overlay_checkbox_and_callback():
    from hex_helper.config import AppConfig
    from hex_helper.ui.lookup_window import LookupWindow

    app = _qt_app()
    toggled: list[bool] = []
    cfg = AppConfig(combo_overlay_enabled=True)
    lw = LookupWindow(cfg=cfg, on_toggle_combo=lambda: toggled.append(True))
    assert hasattr(lw, "combo_overlay_chk")
    assert lw.combo_overlay_chk.isChecked() is True
    lw.combo_overlay_chk.setChecked(False)
    collected = lw._collect_cfg_from_ui()
    assert collected.combo_overlay_enabled is False
    lw.close()
    app.processEvents()


def test_app_controller_decouples_winrate_badges_from_combo_overlay(tmp_path: Path):
    """Verify HexHelperController drives AugmentComboOverlay independently without mutating win-rate scores."""
    from types import SimpleNamespace

    from hex_helper.app import GameState, HexHelperController, HexStateMachine
    from hex_helper.config import AppConfig
    from hex_helper.data.provider import HeroAugmentStat
    from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
    from hex_helper.vision.detector import DetectedCard

    app = _qt_app()
    store = _build_store(tmp_path)
    # Deliberately give AD Dual Wield (1225) a MUCH higher raw win rate on MF than Infernal Conduit (1045)
    store._hero_augments["21"] = {
        "1225": HeroAugmentStat("1225", 1000, 620, 0.62, False),
        "1022": HeroAugmentStat("1022", 1000, 560, 0.56, False),
        "1045": HeroAugmentStat("1045", 1000, 535, 0.535, False),
    }

    captured_badge_items: list = []

    class _FakeAugOverlay:
        def isVisible(self) -> bool:
            return False

        def hide(self) -> None:
            pass

        def get_exclude_rects(self) -> list[tuple[int, int, int, int]]:
            return [(10, 10, 100, 100)]

        def render_cards(self, items, **_kwargs) -> None:
            captured_badge_items[:] = list(items)

    class _FakeDrawer:
        _coach_pending = False

        def get_exclude_rect(self) -> tuple[int, int, int, int] | None:
            return (1800, 200, 1920, 400)

    class _FakeChamp:
        def isVisible(self) -> bool:
            return False

        def hide(self) -> None:
            pass

    ctl = object.__new__(HexHelperController)
    ctl._commercial = False
    ctl._service_locked = False
    ctl._force_update_required = False
    ctl._cloud_ai_cutoff = False
    ctl._cloud_cutoff_policy = "ai_only"
    ctl._overlays_hidden_by_user = False
    ctl._combo_hidden_by_user = False
    ctl._latest_advice = None
    ctl._last_combo_plan = None
    ctl._last_combo_verdict = None
    ctl._last_cs = None
    ctl.cfg = AppConfig(combo_overlay_enabled=True)
    ctl.store = store
    ctl.sm = HexStateMachine()
    ctl.sm.state = GameState.AUGMENT_PICKING
    ctl.sm.current_hero_id = "21"
    ctl.sm.selected_augments = ["1388"]  # AP MF with Infinite Recursion
    ctl._last_live = SimpleNamespace(my_player=SimpleNamespace(item_ids=("6653",)))
    ctl.augment_overlay = _FakeAugOverlay()
    ctl.edge_drawer = _FakeDrawer()
    ctl.champ_overlay = _FakeChamp()
    ctl.combo_overlay = AugmentComboOverlay()

    plan = ctl._refresh_combo_plan_view()
    assert plan.profile.primary_profile == "AP"
    assert ctl.combo_overlay.mode == "plan"
    assert ctl.combo_overlay.is_expanded is False

    cards = (
        DetectedCard(0, store.get_augment("1022"), 0.95, (300, 200, 500, 300), "silver"),
        DetectedCard(1, store.get_augment("1225"), 0.96, (700, 200, 900, 300), "prismatic"),
        DetectedCard(2, store.get_augment("1045"), 0.94, (1100, 200, 1300, 300), "prismatic"),
    )
    rect = SimpleNamespace(left=0, top=0, width=1920, height=1080)
    ctl._render_augment_badges(cards, game_rect=rect)
    app.processEvents()

    # 1. Top win-rate badge overlay remains 100% pure Bayesian win rate (1225 wins raw WR)
    assert len(captured_badge_items) == 3
    by_aid = {it.score.augment_id: it for it in captured_badge_items}
    assert by_aid["1225"].is_best is True
    assert by_aid["1045"].is_best is False

    # 2. Independent combo overlay auto-expands into picker mode, downgrades 1225 & crowns 1045
    assert ctl.combo_overlay.isVisible() is True
    assert ctl.combo_overlay.mode == "picker"
    assert ctl.combo_overlay.is_expanded is True
    assert ctl._last_combo_verdict is not None
    verdict_by_aid = {c.augment_id: c for c in ctl._last_combo_verdict.cards}
    assert verdict_by_aid["1225"].verdict == "conflict"
    assert verdict_by_aid["1225"].combo_delta < 0
    assert verdict_by_aid["1045"].verdict == "jackpot"
    assert ctl._last_combo_verdict.best_slot_index == 2

    # 3. Visible combo overlay is registered in OCR exclude rects
    excl = ctl._get_all_overlay_exclude_rects()
    combo_rect = ctl.combo_overlay.get_exclude_rect()
    assert combo_rect is not None
    assert combo_rect in excl

    # 4. Commercial full-lock immediately hides & locks combo overlay
    ctl._commercial = True
    ctl._service_locked = True
    ctl._sync_overlays_for_cutoff_change()
    app.processEvents()
    assert ctl.combo_overlay.isVisible() is False

    ctl.combo_overlay.close()
    app.processEvents()
