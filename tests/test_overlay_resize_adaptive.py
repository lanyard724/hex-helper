"""Unit & layout tests for resizable, width/height-adaptive in-game floating overlays."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from hex_helper.config import AppConfig, load_config, save_config
from hex_helper.data.provider import AugmentInfo, CoreBuild, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore, DatasetDescriptor
from hex_helper.game.models import ChampSelectState
from hex_helper.scoring.augment_combo import (
    ActiveComboProgress,
    ComboFollowUp,
    ComboPlan,
    MatchBuildProfile,
    PickerCardComboAdvice,
    PickerComboVerdict,
)
from hex_helper.scoring.matchup_optimizer import (
    DynamicItemRec,
    MatchupThreatProfile,
    NextBuyPlan,
)
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.resizable_overlay import FramelessResizeController


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path)
    static.items = {
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105.0}, "", "complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage",), {"FlatMagicDamageMod": 95.0}, "", "complete_equipment"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0, "FlatArmorMod": 40.0}, "", "complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3400, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
    }
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "99": HeroStat("99", "拉克丝", "光辉女郎", 80000, 41600, 0.52, 0.08, 15, False),
        "103": HeroStat("103", "阿狸", "九尾妖狐", 60000, 31200, 0.52, 0.06, 18, False),
        "7": HeroStat("7", "乐芙兰", "诡术妖姬", 45000, 22500, 0.50, 0.04, 60, False),
        "147": HeroStat("147", "萨勒芬妮", "星籁歌姬", 55000, 29150, 0.53, 0.05, 10, False),
        "111": HeroStat("111", "诺提勒斯", "深海泰坦", 50000, 25500, 0.51, 0.05, 35, False),
        "236": HeroStat("236", "卢锡安", "圣枪游侠", 70000, 35700, 0.51, 0.07, 30, False),
    }
    store._core_builds = {
        "99": [CoreBuild(("6655", "3089", "3157"), True, 10000, 5200, 0.52, 0.4)],
        "103": [CoreBuild(("6655",), True, 10000, 5200, 0.52, 0.4)],
        "7": [CoreBuild(("6655",), True, 10000, 5000, 0.50, 0.4)],
        "147": [CoreBuild(("6655",), True, 10000, 5300, 0.53, 0.4)],
        "111": [CoreBuild(("3084",), True, 10000, 5100, 0.51, 0.4)],
        "236": [CoreBuild(("3031",), True, 10000, 5100, 0.51, 0.4)],
    }
    store._augments = {
        "1001": AugmentInfo("1001", "珠光护手", "prismatic", "技能可以暴击"),
        "1002": AugmentInfo("1002", "尤里卡", "prismatic", "法强转化为技能急速"),
        "1003": AugmentInfo("1003", "炼狱导管", "prismatic", "技能附带持续灼烧"),
        "1004": AugmentInfo("1004", "虚幻武器", "gold", "技能触发攻击特效"),
    }
    return store


def test_frameless_resize_controller_edges_and_left_anchor() -> None:
    app = QApplication.instance() or QApplication([])
    w = QWidget()
    w.setGeometry(400, 200, 360, 420)
    w.show()
    app.processEvents()

    allow = [True]
    live_rects: list[tuple[QRect, str]] = []
    ctrl = FramelessResizeController(
        w,
        min_w=260,
        max_w=540,
        min_h=200,
        max_h=900,
        can_resize=lambda: allow[0],
        on_live_resize=lambda r, e: live_rects.append((r, e)),
    )

    tl = w.mapToGlobal(QPoint(0, 0))
    assert ctrl.hit_test_global(QPoint(tl.x() + 2, tl.y() + 100)) == "l"
    assert ctrl.hit_test_global(QPoint(tl.x() + 358, tl.y() + 100)) == "r"
    assert ctrl.hit_test_global(QPoint(tl.x() + 180, tl.y() + 418)) == "b"
    assert ctrl.hit_test_global(QPoint(tl.x() + 4, tl.y() + 415)) == "bl"
    assert ctrl.hit_test_global(QPoint(tl.x() + 355, tl.y() + 415)) == "br"
    assert ctrl.hit_test_global(QPoint(tl.x() + 180, tl.y() + 200)) == ""

    # Disabled state (e.g. collapsed pill) must reject all edge hits
    allow[0] = False
    assert ctrl.hit_test_global(QPoint(tl.x() + 2, tl.y() + 100)) == ""
    allow[0] = True

    # Left-edge drag preserves right anchor (x + width == 760)
    ctrl._active_edge = "bl"
    ctrl._press_global = QPoint(400, 620)
    ctrl._start_geo = QRect(400, 200, 360, 420)
    rect = ctrl.compute_target_rect(QPoint(320, 700))
    assert rect.width() == 440
    assert rect.height() == 500
    assert rect.x() + rect.width() == 760

    # Clamping to min_w / min_h
    tiny = ctrl.compute_target_rect(QPoint(700, 250))
    assert tiny.width() == 260
    assert tiny.height() == 200
    assert tiny.x() + tiny.width() == 760
    w.close()


def test_edge_drawer_resize_adapts_columns_header_and_persists(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    store = _make_store(tmp_path)
    saved_sizes: list[list[int] | None] = []

    drawer = EdgeBuildDrawer(
        on_size_changed=lambda sz: saved_sizes.append(sz),
    )
    drawer.set_expanded(True)
    drawer.show()

    r1 = DynamicItemRec("3089", "灭世者的死亡之帽", 3600, False, 1950, 92.0, 0.57, 8200, ("当前已成型第一件爆发装，顺风直接堆叠大帽放大技能暴击收益",), ("核心",))
    r2 = DynamicItemRec("3157", "中娅沙漏", 3250, False, 1600, 86.0, 0.55, 6100, ("规避刺客突进开团",), ("保命",))
    r3 = DynamicItemRec("3135", "虚空之杖", 3000, False, 1350, 84.0, 0.56, 5400, ("敌方魔抗堆叠后必出穿透",), ("法穿",))
    recs = [r1, r2, r3]
    next_plan = NextBuyPlan(
        target=r1,
        effective_cost=3600,
        affordable_now=False,
        gold_gap=1950,
        buy_now_hint="先买无用大棒 (1200G)",
        remaining_build_path=("灭世者的死亡之帽", "中娅沙漏", "虚空之杖"),
        remaining_build_ids=("3089", "3157", "3135"),
        backup=r2,
        recommended_boots_id="3020",
        recommended_boots_name="法师之靴",
        recommended_boots_reason="提前补法穿鞋提爆发",
    )
    threat = MatchupThreatProfile(
        enemy_armor_score=1.2,
        enemy_hp_score=2.0,
        enemy_mr_score=1.5,
        enemy_ad_ratio=0.5,
        enemy_ap_ratio=0.5,
        enemy_heal_score=0.8,
        enemy_shield_score=0.4,
        enemy_cc_score=1.0,
        ally_has_grievous=False,
        ally_has_shield_break=False,
        top_threat_name="劫",
        top_threat_kda="11/3/4",
        top_threat_type="AD",
        summary_tags=("敌2前排", "盯:劫"),
    )
    drawer.refresh_match_view(
        store,
        "99",
        ["1001", "1002", "1003"],
        ("6655",),
        1650.0,
        threat,
        recs,
        next_plan=next_plan,
    )
    app.processEvents()

    # Collapsed pill blocks resize
    drawer.set_expanded(False)
    app.processEvents()
    assert drawer._resize_ctrl.hit_test_global(drawer.mapToGlobal(QPoint(2, 40))) == ""

    def _active_aug_cols() -> int:
        cols = [drawer.aug_chips_lay.getItemPosition(i)[1] for i in range(drawer.aug_chips_lay.count())]
        return (max(cols) + 1) if cols else 0

    # Expand and resize NARROW (296 x 420)
    drawer.set_expanded(True)
    drawer._on_live_resize(QRect(drawer.x(), drawer.y(), 296, 420), "bl")
    app.processEvents()
    assert drawer.width() == 296
    assert drawer.height() == 420
    assert not drawer.hdr_icon.isVisible()
    assert drawer.collapse_btn.text() == "收"
    assert _active_aug_cols() == 2

    # Resize WIDE (490 x 560) -> 3 augment columns + full header
    drawer._on_live_resize(QRect(drawer.x(), drawer.y(), 490, 560), "bl")
    drawer._on_resize_finished(QRect(drawer.x(), drawer.y(), 490, 560), "bl")
    app.processEvents()
    assert drawer.width() == 490
    assert drawer.height() == 560
    assert drawer.hdr_icon.isVisible()
    assert drawer.collapse_btn.text() == "收起"
    assert _active_aug_cols() == 3
    assert saved_sizes[-1] == [490, 560]

    # Reset restores default width 368 and notifies None
    drawer.reset_default_size()
    app.processEvents()
    assert drawer.width() == EdgeBuildDrawer.EXPANDED_W
    assert drawer._custom_h is None
    assert saved_sizes[-1] is None
    drawer.close()


def test_combo_overlay_resize_adapts_density_and_resets() -> None:
    app = QApplication.instance() or QApplication([])
    saved_sizes: list[list[int] | None] = []
    overlay = AugmentComboOverlay(
        on_size_changed=lambda sz: saved_sizes.append(sz),
    )
    profile = MatchBuildProfile(
        primary_profile="CRIT_SPELL",
        direction_label="技能暴击核爆",
        favored_axes=("CRIT_SPELL", "AP_SPELL"),
        penalized_axes=("AUTO_ONHIT",),
        penalized_labels=("普攻攻速",),
        evidence=("已拿珠光护手",),
    )
    long_reason = "与已拿的珠光护手形成双暴击核爆联动，大招与基础技能爆发直接翻倍，团战收割上限极高"
    plan = ComboPlan(
        hero_id="99",
        hero_name="拉克丝",
        profile=profile,
        active_combos=(
            ActiveComboProgress("c1", "核爆双暴", True, ("1001",), ("1002",), ("尤里卡",), False, long_reason, "留骰追尤里卡"),
        ),
        recommended_targets=(
            ComboFollowUp("1002", "尤里卡", "prismatic", "c1", "核爆双暴", long_reason, True, 9.5, True),
        ),
        opening_combos=(),
        avoid_summary="降权纯攻速特效符文",
    )
    verdict = PickerComboVerdict(
        profile=profile,
        cards=(
            PickerCardComboAdvice(0, "1002", "尤里卡", "prismatic", "jackpot", "天胡成型", 8.5, long_reason, "核爆双暴", True),
            PickerCardComboAdvice(1, "1003", "炼狱导管", "prismatic", "synergy", "高配合", 4.2, "持续灼烧配合技能急速高频触发", "灼烧流", False),
            PickerCardComboAdvice(2, "1004", "虚幻武器", "gold", "conflict", "流派冲突·降权", -3.0, "纯普攻特效轴与当前法核方向冲突", "", False),
        ),
        best_slot_index=0,
        reroll_advice="左位直接拿天胡",
    )
    overlay.set_plan(plan)
    overlay.enter_picker_mode(verdict)
    overlay.show()
    app.processEvents()

    # Resize NARROW (244 x 360)
    overlay._on_live_resize(QRect(overlay.x(), overlay.y(), 244, 360), "br")
    app.processEvents()
    assert overlay.width() == 244
    assert overlay.collapse_btn.text() == "收"
    assert not overlay.profile_badge_lbl.isVisible()
    narrow_texts = [lbl.text() for lbl in overlay.picker_cards_wrap.findChildren(QLabel)]
    narrow_reason = max(narrow_texts, key=len)

    # Resize WIDE (430 x 480)
    overlay._on_live_resize(QRect(overlay.x(), overlay.y(), 430, 480), "br")
    overlay._on_resize_finished(QRect(overlay.x(), overlay.y(), 430, 480), "br")
    app.processEvents()
    assert overlay.width() == 430
    assert overlay.collapse_btn.text() == "收起"
    assert overlay.profile_badge_lbl.isVisible()
    wide_texts = [lbl.text() for lbl in overlay.picker_cards_wrap.findChildren(QLabel)]
    wide_reason = max(wide_texts, key=len)
    assert len(wide_reason) > len(narrow_reason)
    assert saved_sizes[-1] == [430, 480]

    overlay.reset_default_size()
    app.processEvents()
    assert overlay._custom_w is None
    assert overlay._custom_h is None
    assert saved_sizes[-1] is None
    overlay.close()


def test_champ_select_resize_adapts_width_and_scroll_height(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    store = _make_store(tmp_path)
    saved_sizes: list[list[int] | None] = []
    overlay = ChampSelectOverlay(on_size_changed=lambda sz: saved_sizes.append(sz))
    state = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="99",
        team_hero_ids=("99", "103", "7", "147"),
        bench_hero_ids=("111", "236"),
    )
    overlay.update_state(state, store)
    overlay.show_overlay()
    app.processEvents()

    # Narrow + compact height (320 x 300)
    overlay._on_live_resize(QRect(overlay.x(), overlay.y(), 320, 300), "br")
    app.processEvents()
    assert overlay.width() == 320
    assert overlay.height() == 300
    assert overlay.title_lbl.text() == "选人决策"
    assert not overlay.icon_lbl.isVisible()
    assert overlay.rows_scroll.maximumHeight() > 100000
    small_scroll_h = overlay.rows_scroll.height()

    # Wide + tall (470 x 620)
    overlay._on_live_resize(QRect(overlay.x(), overlay.y(), 470, 620), "br")
    overlay._on_resize_finished(QRect(overlay.x(), overlay.y(), 470, 620), "br")
    app.processEvents()
    assert overlay.width() == 470
    assert overlay.height() == 620
    assert overlay.title_lbl.text() == "海克斯大乱斗 · 选人决策"
    assert overlay.icon_lbl.isVisible()
    assert overlay.rows_scroll.height() > small_scroll_h + 200
    wide_texts = [lbl.text() for lbl in overlay.rows_body.findChildren(QLabel)]
    assert any("场" in t for t in wide_texts)
    assert saved_sizes[-1] == [470, 620]

    # Reset restores 366 default width and auto-fit height
    overlay.reset_default_size()
    app.processEvents()
    assert overlay.width() == ChampSelectOverlay.DEFAULT_W
    assert overlay._custom_w is None
    assert overlay._custom_h is None
    assert saved_sizes[-1] is None
    overlay.close()


def test_app_config_persists_overlay_sizes(tmp_path: Path) -> None:
    cfg = AppConfig(
        drawer_size=[440, 560],
        combo_drawer_size=[360, 480],
        champ_window_size=[420, 640],
    )
    path = tmp_path / "config.json"
    save_config(cfg, path=path)
    loaded = load_config(path=path)
    assert loaded.drawer_size == [440, 560]
    assert loaded.combo_drawer_size == [360, 480]
    assert loaded.champ_window_size == [420, 640]

    # Reset back to None persists cleanly
    loaded.drawer_size = None
    save_config(loaded, path=path)
    reloaded = load_config(path=path)
    assert reloaded.drawer_size is None
