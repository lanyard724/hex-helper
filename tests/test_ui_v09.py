"""Automated PySide6 widget & motion primitive tests for HexHelper v0.9.0 UI/UX overhaul."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication

from hex_helper.ui.hextech_widgets import (
    HextechGoldRingIcon,
    HextechPulseFrame,
    HextechShimmerBar,
)


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_motion_primitives_lifecycle_and_zero_idle_timers():
    app = _qt_app()

    shimmer = HextechShimmerBar(height=4)
    assert shimmer.is_active() is False
    shimmer.set_active(True)
    assert shimmer.is_active() is True
    shimmer.set_active(False)
    assert shimmer.is_active() is False

    pulse = HextechPulseFrame()
    pulse.trigger_pulse("#C8AA6E", duration_ms=120)
    assert pulse._pulse_t > 0.0

    ring = HextechGoldRingIcon(size=34)
    pm = QPixmap(32, 32)
    pm.fill(QColor("#1E293B"))
    ring.set_state(pm, progress=0.72, ready=False, fallback_text="帽")
    assert ring.progress == 0.72
    assert ring.ready is False
    ring.set_state(pm, progress=1.0, ready=True, fallback_text="帽")
    assert ring.ready is True
    app.processEvents()


def test_edge_drawer_v09_live_pill_shimmer_and_pulse():
    import tempfile
    from pathlib import Path
    from hex_helper.data.provider import AugmentInfo, ItemStaticInfo
    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.data.store import DataStore
    from hex_helper.scoring.matchup_optimizer import DynamicItemRec, NextBuyPlan
    from hex_helper.ui.edge_drawer import EdgeBuildDrawer

    app = _qt_app()
    with tempfile.TemporaryDirectory() as td:
        static = StaticAssetsManager(Path(td))
        static.items["3089"] = ItemStaticInfo(item_id="3089", name="灭世者的死亡之帽", gold=3500, category="complete_equipment")
        store = DataStore(static)
        store._augments["3163"] = AugmentInfo("3163", "迷人蝴蝶", "gold")

        drawer = EdgeBuildDrawer()
        target = DynamicItemRec("3089", "灭世者的死亡之帽", 3500, False, 900, 10.0, 0.54, 1000, ("核心",), ())
        plan = NextBuyPlan(target, 3500, False, 900, "先买无用大棒", ("灭世者的死亡之帽",), ("3089",))
        drawer.refresh_match_view(store, "63", ["3163"], (), 2600.0, None, [target], next_plan=plan)

        assert hasattr(drawer, "pill_item_ring")
        assert 0.70 <= drawer.pill_item_ring.progress <= 0.78
        assert drawer.pill_item_ring.ready is False
        assert "900" in drawer.pill_gold_lbl.text()
        assert drawer.tools_row.isHidden() is True

        drawer.set_coach_online(True, label="● Gemini")
        drawer.set_coach_pending(True, "思考中...")
        assert drawer.coach_shimmer.is_active() is True
        drawer.set_coach_advice(None)
        assert drawer.coach_shimmer.is_active() is False

        drawer.flash_new_augment("3163")
        assert drawer.aug_frame._pulse_t > 0.0
        drawer.close()
        app.processEvents()


def test_augment_badges_v09_archetype_callout_and_pending_banner():
    from hex_helper.scoring.tier import score_hero_augment
    from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay

    app = _qt_app()
    ov = AugmentBadgesOverlay()
    sc_best = score_hero_augment("3163", "迷人蝴蝶", "gold", 0.50, 15000, 26000, False)
    sc_tm = score_hero_augment("1238", "质变：棱彩阶", "gold", 0.50, 12000, 24000, False)
    ov.render_cards(
        [
            AugmentBadgeRenderItem(0, sc_best, (400, 420, 660, 460), is_best=True, archetype_hint="盾奶齐射流 · 月石+大天使"),
            AugmentBadgeRenderItem(1, sc_tm, (1150, 420, 1410, 460), is_best=False, is_transmute=True),
        ],
        screen_rect=(0, 0, 2560, 1440),
        coach_augment_tip="",
        coach_pending=True,
    )
    assert len(ov._badges) == 2
    assert ov._coach_banner is not None
    assert "推演" in ov._coach_banner_lbl.text()
    ov.close()
    app.processEvents()


def test_champ_select_team_composition_warning():
    import tempfile
    from pathlib import Path
    from hex_helper.data.provider import CoreBuild, HeroStat, ItemStaticInfo
    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.data.store import DataStore
    from hex_helper.ui.champ_select_ui import summarize_team_composition

    with tempfile.TemporaryDirectory() as td:
        static = StaticAssetsManager(Path(td))
        static.items["3089"] = ItemStaticInfo(
            item_id="3089",
            name="灭世者的死亡之帽",
            gold=3500,
            category="complete_equipment",
            stats={"FlatMagicDamageMod": 130.0},
        )
        store = DataStore(static)
        for hid, nm in (("63", "布兰德"), ("99", "拉克丝"), ("101", "泽拉斯"), ("1", "安妮")):
            store._heroes[hid] = HeroStat(hid, nm, nm, 10000, 5200, 0.52, 0.05, 10)
            store._core_builds[hid] = [CoreBuild(("3089",), True, 1000, 520, 0.52, 0.1)]

        msg, is_warn = summarize_team_composition(("63", "99", "101", "1"), store)
        assert is_warn is True
        assert "AP" in msg and "前排" in msg


def test_lookup_window_v09_collapsible_key_strip_history_and_special_filters():
    from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload
    from hex_helper.config import AppConfig
    from hex_helper.ui.lookup_window import LookupWindow

    app = _qt_app()
    cfg = AppConfig()
    lw = LookupWindow(cfg=cfg)

    assert lw.gemini_key_edit is None
    assert lw.coach_mode_combo is None

    total_all = lw.special_list.count()
    lw._set_special_category("质变")
    assert 0 < lw.special_list.count() < total_all

    adv = CoachAdvicePayload(
        request_id="r-hist-1",
        next_items=[CoachAdviceItem("3084", "心之钢", "下件必出", "钢神首发")],
        buy_now="先买巨人腰带",
        build_path=["心之钢", "狂徒铠甲"],
        tactical_summary="顶前排叠心之钢",
        updated_at="2026-11-21T23:10:00+08:00",
        source="Gemini (gemini-4-hydrogen · medium)",
    )
    lw.append_coach_history("蒙多医生", "game_start", adv, latency_ms=2840)
    assert len(lw._coach_history) == 1
    lw.close()
    app.processEvents()


def test_edge_drawer_collapsed_height_never_grows_and_panel_shrinks():
    import tempfile
    from pathlib import Path
    from hex_helper.data.provider import AugmentInfo, HeroStat, ItemStaticInfo
    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.data.store import DataStore
    from hex_helper.scoring.matchup_optimizer import DynamicItemRec, NextBuyPlan
    from hex_helper.ui.edge_drawer import EdgeBuildDrawer

    app = _qt_app()
    with tempfile.TemporaryDirectory() as td:
        static = StaticAssetsManager(Path(td))
        static.items["3089"] = ItemStaticInfo(
            item_id="3089", name="灭世者的死亡之帽", gold=3500, category="complete_equipment"
        )
        store = DataStore(static)
        store._heroes["6"] = HeroStat("6", "厄加特", "Urgot", 10000, 5200, 0.52, 0.05, 10)
        store._augments["3163"] = AugmentInfo("3163", "迷人蝴蝶", "gold")

        drawer = EdgeBuildDrawer()
        target = DynamicItemRec("3089", "灭世者的死亡之帽", 3500, False, 176, 10.0, 0.54, 1000, ("核心",), ())
        plan = NextBuyPlan(target, 3500, False, 176, "先买爆裂魔杖", ("灭世者的死亡之帽",), ("3089",))

        drawer.show()
        app.processEvents()
        drawer.refresh_match_view(store, "6", ["3163"], (), 3324.0, None, [target], next_plan=plan)
        baseline_h = drawer.height()
        assert baseline_h <= 240, f"Collapsed pill initial height unexpectedly tall: {baseline_h}"

        # Simulate 100 live telemetry poll ticks while collapsed (regression for +4px/tick full-screen stretch bug)
        for tick in range(100):
            drawer.refresh_match_view(
                store, "6", ["3163"], (), 3324.0 + tick, None, [target], next_plan=plan
            )
        assert drawer.width() == EdgeBuildDrawer.COLLAPSED_W
        assert drawer.height() == baseline_h, f"Collapsed pill height drifted from {baseline_h} to {drawer.height()}"
        assert drawer.pill.height() == baseline_h

        # Expand, wait for slide animation to settle, verify panel grows then shrinks back when hidden
        import time as _time
        from PySide6.QtCore import QVariantAnimation

        drawer.set_expanded(True)
        deadline = _time.monotonic() + 1.0
        while drawer._slide_anim.state() == QVariantAnimation.State.Running and _time.monotonic() < deadline:
            app.processEvents()
            _time.sleep(0.01)
        app.processEvents()
        expanded_base_h = drawer.height()
        assert drawer.width() == EdgeBuildDrawer.EXPANDED_W
        drawer.set_warning("测试长告警文案" * 8)
        drawer.tools_row.show()
        drawer._reposition_right_edge()
        expanded_tall_h = drawer.height()
        assert expanded_tall_h > expanded_base_h

        drawer.set_warning("")
        drawer.tools_row.hide()
        drawer._reposition_right_edge()
        assert drawer.height() <= expanded_base_h + 2

        # Collapse back and verify repeated ticks still stay pinned at baseline_h
        drawer.set_expanded(False)
        deadline = _time.monotonic() + 1.0
        while drawer._slide_anim.state() == QVariantAnimation.State.Running and _time.monotonic() < deadline:
            app.processEvents()
            _time.sleep(0.01)
        app.processEvents()
        for _ in range(50):
            drawer._reposition_right_edge()
        assert drawer.width() == EdgeBuildDrawer.COLLAPSED_W
        assert drawer.height() == baseline_h

        drawer.close()
        app.processEvents()


def test_edge_drawer_heals_legacy_expanded_right_edge_pos():
    from PySide6.QtGui import QGuiApplication
    from hex_helper.ui.edge_drawer import EdgeBuildDrawer

    app = _qt_app()
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    geo = screen.geometry()

    # Legacy v1.0.0 saved the expanded panel's left X when dragged vertically at the right edge
    legacy_expanded_x = geo.right() - EdgeBuildDrawer.EXPANDED_W + 1
    drawer_right = EdgeBuildDrawer(initial_pos=[legacy_expanded_x, geo.top() + 200])
    assert drawer_right._anchor_right is True
    assert drawer_right.x() == geo.right() - EdgeBuildDrawer.COLLAPSED_W + 1
    drawer_right.close()

    # Explicit user placement away from the right edge must remain custom-anchored
    custom_x = geo.left() + 120
    drawer_left = EdgeBuildDrawer(initial_pos=[custom_x, geo.top() + 200])
    assert drawer_left._anchor_right is False
    assert drawer_left.x() == custom_x
    drawer_left.close()
    app.processEvents()
