"""Phase C2 UI tests: P0-6 3081 card hint, P0-7 comp tags + 4/4 clear + next_augment_goal capsule, P0-3 band switch."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QWidget

from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload
from hex_helper.config import DATA_BAND_MODES, AppConfig
from hex_helper.data.provider import AugmentInfo, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import (
    ActiveComboProgress,
    ComboFollowUp,
    ComboPlan,
    MatchBuildProfile,
    build_combo_plan,
)
from hex_helper.scoring.destiny import summarize_destiny_card_hint
from hex_helper.scoring.matchup_optimizer import DynamicItemRec, MatchupThreatProfile, NextBuyPlan
from hex_helper.scoring.tier import AugmentScore
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay, _SingleBadgeCard
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.lookup_window import LookupWindow

LIVE_1623_NORMAL = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_1623_NORMAL",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal",
    )
)
LIVE_1622_NORMAL = Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\aramhex_16_22_normal")


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _ResizeCounter(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self.count += 1
        return False


def test_desktop_ui_files_keep_hextech_palette_no_tactile_orange() -> None:
    ui_dir = Path(__file__).resolve().parents[1] / "src" / "hex_helper" / "ui"
    for fname in ("augment_overlay.py", "augment_combo_overlay.py", "edge_drawer.py", "lookup_window.py"):
        content = (ui_dir / fname).read_text(encoding="utf-8")
        assert "#FF5A1F" not in content.upper(), f"Forbidden Web Tactile Orange found in {fname}"


def test_augment_overlay_destiny_hint_and_warning_no_clipping_and_dedup() -> None:
    app = _qt_app()
    ov = AugmentBadgesOverlay()

    sc_good = AugmentScore("3081", "命运之武器", "gold", "S", 0.56, 0.55, 0.03, 9000, False, percentile=88.0)
    sc_bad = AugmentScore("3081", "命运之武器", "gold", "C", 0.48, 0.48, -0.015, 2500, False, percentile=22.0)

    # 1. Positive hint only + zero clipping across 168..258px widths
    item_good = AugmentBadgeRenderItem(
        slot_index=0,
        score=sc_good,
        screen_box=(360, 420, 620, 460),
        is_best=True,
        destiny_hint="◈ 绑定首选：海克斯科技枪刃 (+6.4%)",
        destiny_warning="",
    )
    # 2. Hint + negative payoff warning pill across 168..258px widths
    item_warn = AugmentBadgeRenderItem(
        slot_index=1,
        score=sc_bad,
        screen_box=(820, 420, 1080, 460),
        is_best=False,
        destiny_hint="◈ 绑定首选：心之钢 (-1.2%)",
        destiny_warning="▽ 绑定收益偏弱 · 慎选",
    )

    for width in (258, 226, 207, 196, 180, 168):
        for case_item in (item_good, item_warn):
            card = _SingleBadgeCard(case_item, animate_sheen=False, card_width=width)
            card.setParent(ov)
            card.ensurePolished()
            card.adjustSize()
            lay = card.layout()
            if lay is not None:
                lay.activate()
            assert card.minimumSizeHint().width() <= width
            for lbl in card.findChildren(QLabel):
                if not lbl.wordWrap():
                    assert lbl.geometry().width() >= lbl.sizeHint().width()
            card.setParent(None)
            card.deleteLater()

    # 3. Render in AugmentBadgesOverlay and verify signature dedup + invalidation
    ov.render_cards([item_good, item_warn], screen_rect=(0, 0, 1920, 1080))
    app.processEvents()
    texts = [lbl.text() for lbl in ov.findChildren(QLabel)]
    assert "◈ 绑定首选：海克斯科技枪刃 (+6.4%)" in texts
    assert "◈ 绑定首选：心之钢 (-1.2%)" in texts
    assert "▽ 绑定收益偏弱 · 慎选" in texts
    styles = " | ".join(w.styleSheet() for w in ov.findChildren(QWidget) if w.styleSheet())
    assert "#FF5A1F" not in styles.upper()

    ids_before = [id(b) for b in ov._badges]
    ov.render_cards([item_good, item_warn], screen_rect=(0, 0, 1920, 1080))
    assert [id(b) for b in ov._badges] == ids_before

    item_good_changed = AugmentBadgeRenderItem(
        slot_index=0,
        score=sc_good,
        screen_box=(360, 420, 620, 460),
        is_best=True,
        destiny_hint="◈ 绑定首选：育恩塔尔荒野箭 (+2.5%)",
        destiny_warning="",
    )
    ov.render_cards([item_good_changed, item_warn], screen_rect=(0, 0, 1920, 1080))
    app.processEvents()
    updated_texts = [lbl.text() for lbl in ov.findChildren(QLabel)]
    assert "◈ 绑定首选：育恩塔尔荒野箭 (+2.5%)" in updated_texts

    ov.close()
    app.processEvents()


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_augment_overlay_renders_real_1623_destiny_hints(tmp_path: Path) -> None:
    app = _qt_app()
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda _u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )

    akali_hint = summarize_destiny_card_hint(store, "84", aug_delta=0.02)
    rell_hint = summarize_destiny_card_hint(store, "526", aug_delta=-0.0146)
    assert akali_hint is not None and rell_hint is not None

    ov = AugmentBadgesOverlay()
    sc0 = AugmentScore("3081", "命运之武器", "gold", "S", 0.49, 0.44, 0.02, 4000, False, percentile=82.0)
    sc1 = AugmentScore("3081", "命运之武器", "gold", "C", 0.48, 0.49, -0.015, 1800, False, percentile=20.0)
    ov.render_cards(
        [
            AugmentBadgeRenderItem(
                0,
                sc0,
                (360, 420, 620, 460),
                is_best=True,
                destiny_hint=akali_hint.hint_line,
                destiny_warning=akali_hint.warning_line,
            ),
            AugmentBadgeRenderItem(
                1,
                sc1,
                (820, 420, 1080, 460),
                is_best=False,
                destiny_hint=rell_hint.hint_line,
                destiny_warning=rell_hint.warning_line,
            ),
        ],
        screen_rect=(0, 0, 1920, 1080),
    )
    app.processEvents()
    texts = [lbl.text() for lbl in ov.findChildren(QLabel)]
    assert akali_hint.hint_line in texts
    assert rell_hint.hint_line in texts
    assert rell_hint.warning_line in texts
    assert "(+" not in rell_hint.hint_line
    akali_lbl = next(lbl for lbl in ov.findChildren(QLabel) if lbl.text() == akali_hint.hint_line)
    rell_lbl = next(lbl for lbl in ov.findChildren(QLabel) if lbl.text() == rell_hint.hint_line)
    assert "#0AC8B9" in akali_lbl.styleSheet().upper()
    assert "#C8AA6E" in rell_lbl.styleSheet().upper()
    assert "#0AC8B9" not in rell_lbl.styleSheet().upper()
    ov.close()
    app.processEvents()


def test_combo_overlay_renders_comp_tags_without_overflow_and_clears_at_4_of_4() -> None:
    app = _qt_app()
    ov = AugmentComboOverlay(initial_size=[244, 420])
    profile = MatchBuildProfile(
        primary_profile="AD",
        direction_label="暴击持续输出",
        favored_axes=("AUTO_ONHIT", "CRIT_SPELL"),
        penalized_axes=(),
        penalized_labels=(),
        evidence=("已选双刀流",),
    )
    plan_active = ComboPlan(
        hero_id="222",
        hero_name="金克丝",
        profile=profile,
        active_combos=(
            ActiveComboProgress(
                "c1",
                "攻速暴击",
                False,
                ("1225",),
                ("1077",),
                ("灵魂虹吸",),
                False,
                "持续打坦",
                "追灵魂虹吸",
            ),
        ),
        recommended_targets=(
            ComboFollowUp("1077", "灵魂虹吸", "gold", "c1", "攻速暴击", "打敌多坦持续吸血", True, 12.0, True, comp_tag="打敌多坦"),
            ComboFollowUp("1080", "会心防御", "silver", "c2", "保命轴", "防刺客突进", False, 9.5, False, comp_tag="防突进保命"),
            ComboFollowUp("1115", "最万用的瞄准镜", "prismatic", "c3", "射程轴", "打长手脆皮", False, 8.8, False, comp_tag="打长手脆皮"),
            ComboFollowUp("1045", "炼狱导管", "prismatic", "c4", "灼烧轴", "压制高回复", False, 8.0, False, comp_tag="压高回复"),
        ),
        opening_combos=(),
        avoid_summary="",
        round_index=2,
    )

    ov.set_expanded(True)
    ov.set_plan(plan_active)
    ov.show()
    app.processEvents()

    target_labels = [lbl.text() for lbl in ov.targets_list_wrap.findChildren(QLabel)]
    for tag in ("打敌多坦", "防突进保命", "打长手脆皮", "压高回复"):
        assert tag in target_labels
    for row in ov.targets_list_wrap.findChildren(QFrame):
        assert row.minimumSizeHint().width() <= ov.width()

    # Transition to 4/4 selected -> stale targets cleared & clean 4/4 notice shown
    plan_full = ComboPlan(
        hero_id="222",
        hero_name="金克丝",
        profile=profile,
        active_combos=plan_active.active_combos,
        recommended_targets=(),
        opening_combos=(),
        avoid_summary="",
        round_index=4,
    )
    ov.set_plan(plan_full)
    app.processEvents()

    full_labels = [lbl.text() for lbl in ov.targets_list_wrap.findChildren(QLabel)]
    assert full_labels == ["✓ 已持有 4 枚海克斯 · 专注出装"]
    assert "追灵魂虹吸" not in ov.active_combo_body.text()
    assert ov.targets_hdr.isHidden() is True

    # Reset back to 0/4 empty state restores opening prompt cleanly
    plan_empty = ComboPlan(
        hero_id="222",
        hero_name="金克丝",
        profile=profile,
        active_combos=(),
        recommended_targets=(),
        opening_combos=(),
        avoid_summary="",
        round_index=1,
    )
    ov.set_plan(plan_empty, selected_count=0)
    app.processEvents()
    reset_labels = [lbl.text() for lbl in ov.targets_list_wrap.findChildren(QLabel)]
    assert any("选定首个海克斯" in txt for txt in reset_labels)
    assert ov.targets_hdr.isHidden() is False

    ov.close()
    app.processEvents()


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_combo_overlay_real_1623_three_tank_comp_tag_and_4_of_4(tmp_path: Path) -> None:
    app = _qt_app()
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda _u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )
    threat = MatchupThreatProfile(
        enemy_armor_score=3.5,
        enemy_hp_score=4.5,
        enemy_mr_score=1.5,
        enemy_ad_ratio=0.6,
        enemy_ap_ratio=0.4,
        enemy_heal_score=1.0,
        enemy_shield_score=0.5,
        enemy_cc_score=2.0,
        ally_has_grievous=False,
        ally_has_shield_break=False,
        top_threat_name="蒙多医生",
        top_threat_kda="5/2/8",
        top_threat_type="AD",
        summary_tags=("敌方高坦",),
        enemy_tank_count=3,
    )
    plan = build_combo_plan(
        store,
        "222",
        ["1079"],
        ("3031", "3085"),
        limit=4,
        threat=threat,
        enemy_profiles=("TANK", "TANK", "TANK", "AD", "AP"),
    )
    ov = AugmentComboOverlay()
    ov.set_expanded(True)
    ov.set_plan(plan)
    ov.show()
    app.processEvents()
    labels = [lbl.text() for lbl in ov.targets_list_wrap.findChildren(QLabel)]
    assert "打敌多坦" in labels

    plan_4 = build_combo_plan(
        store,
        "222",
        ["1225", "1079", "1115", "1328"],
        ("3031", "3085"),
        limit=4,
        threat=threat,
        enemy_profiles=("TANK", "TANK", "TANK", "AD", "AP"),
    )
    ov.set_plan(plan_4)
    app.processEvents()
    assert [lbl.text() for lbl in ov.targets_list_wrap.findChildren(QLabel)] == ["✓ 已持有 4 枚海克斯 · 专注出装"]
    ov.close()
    app.processEvents()


def test_edge_drawer_next_augment_goal_capsule_lifecycle_and_zero_resize_churn(tmp_path: Path) -> None:
    app = _qt_app()
    static = StaticAssetsManager(tmp_path, fetcher=lambda _u: b"{}")
    static.items["3033"] = ItemStaticInfo("3033", "凡性的提醒", 3000, ("Damage",), {}, "", "complete_equipment")
    store = DataStore(static)
    store._heroes["222"] = HeroStat("222", "金克丝", "暴走萝莉", 50000, 25000, 0.50, 0.05, 20)
    for aid, nm in (("1079", "闪电打击"), ("1225", "双刀流"), ("1115", "最万用的瞄准镜"), ("1328", "暴击律动")):
        store._augments[aid] = AugmentInfo(aid, nm, "gold")

    drawer = EdgeBuildDrawer()
    drawer.set_expanded(True)
    drawer.show()
    app.processEvents()

    rec = DynamicItemRec("3033", "凡性的提醒", 3000, True, 0, 9.2, 0.54, 5000, ("破甲重伤",), ())
    plan = NextBuyPlan(rec, 3000, True, 0, "直合凡性的提醒", ("凡性的提醒",), ("3033",))
    drawer.refresh_match_view(store, "222", ["1079"], (), 3100.0, None, [rec], next_plan=plan)
    app.processEvents()

    # Empty/absent next_augment_goal stays hidden (zero wasted space)
    assert drawer.next_aug_goal_lbl.isHidden() is True
    h_without_goal = drawer.height()

    # Advice carrying next_augment_goal displays capsule directly beneath buy_now_lbl
    advice_dict = {
        "request_id": "req-p07-1",
        "next_items": [{"item_id": "3033", "name": "凡性的提醒", "priority": "下件必出", "reason": "敌双坦高回血"}],
        "buy_now": "直合凡性的提醒",
        "build_path": ["凡性的提醒"],
        "tactical_summary": "打敌双坦先补破甲重伤",
    }
    adv = CoachAdvicePayload.from_dict(advice_dict)
    setattr(adv, "next_augment_goal", "追灵魂虹吸/双刀流打敌双坦")
    drawer.set_coach_advice(adv, allow_auto_expand=False)
    app.processEvents()

    assert drawer.next_aug_goal_lbl.isHidden() is False
    assert drawer.next_aug_goal_lbl.text() == "下轮海克斯：追灵魂虹吸/双刀流打敌双坦"
    nb_lay = drawer.next_buy_card.layout()
    assert nb_lay.indexOf(drawer.next_aug_goal_lbl) == nb_lay.indexOf(drawer.buy_now_lbl) + 1
    assert drawer.height() >= h_without_goal

    # 1Hz ticks with unchanged state trigger ZERO resizes
    rc = _ResizeCounter()
    drawer.installEventFilter(rc)
    for _ in range(10):
        drawer.refresh_match_view(store, "222", ["1079"], (), 3100.0, None, [rec], next_plan=plan)
        app.processEvents()
    assert rc.count == 0
    drawer.removeEventFilter(rc)

    # Selecting 4/4 augments automatically hides next_augment_goal capsule
    drawer.refresh_match_view(
        store,
        "222",
        ["1079", "1225", "1115", "1328"],
        (),
        3100.0,
        None,
        [rec],
        next_plan=plan,
    )
    app.processEvents()
    assert drawer.next_aug_goal_lbl.isHidden() is True
    assert drawer.next_aug_goal_lbl.text() == ""

    # Dropping back to 3/4 with empty goal keeps hidden; clearing advice hides cleanly
    setattr(adv, "next_augment_goal", "")
    drawer.refresh_match_view(store, "222", ["1079", "1225", "1115"], (), 3100.0, None, [rec], next_plan=plan)
    drawer.set_coach_advice(adv, allow_auto_expand=False)
    app.processEvents()
    assert drawer.next_aug_goal_lbl.isHidden() is True

    drawer.close()
    app.processEvents()


def test_lookup_window_data_band_mode_switch_and_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _qt_app()
    saved_cfgs: list[AppConfig] = []
    changed_events: list[dict] = []

    cfg = AppConfig(data_band_mode="blended")
    lw = LookupWindow(
        cfg=cfg,
        on_save_settings=lambda c: saved_cfgs.append(c),
        on_settings_changed=lambda ch: changed_events.append(ch),
    )
    lw.resize(940, 610)
    lw._switch_page(3, force=True)
    lw.show()
    app.processEvents()

    assert hasattr(lw, "data_band_combo")
    assert hasattr(lw, "data_band_hint_lbl")
    codes = [lw.data_band_combo.itemData(i) for i in range(lw.data_band_combo.count())]
    assert tuple(codes) == DATA_BAND_MODES
    labels = [lw.data_band_combo.itemText(i) for i in range(lw.data_band_combo.count())]
    assert any("全分段全量" in t for t in labels)
    assert any("高分均衡" in t for t in labels)
    assert any("高分 Top5%" in t for t in labels)
    assert lw.data_band_combo.currentData() == "blended"
    assert lw.data_band_hint_lbl.text() == "当前版本全量数据"

    # Layout check at 940x610: combo & hint must not overlap each other
    assert not lw.data_band_combo.geometry().intersects(lw.data_band_hint_lbl.geometry())

    # Switching to "high" and saving persists cfg.data_band_mode & notifies settings change
    hi_idx = lw.data_band_combo.findData("high")
    assert hi_idx >= 0
    lw.data_band_combo.setCurrentIndex(hi_idx)
    lw._save_clicked()
    assert saved_cfgs and saved_cfgs[-1].data_band_mode == "high"
    assert changed_events and changed_events[-1].get("data_band_mode") == "high"

    # Binding store WITHOUT bands/high keeps "当前版本全量数据"
    static = StaticAssetsManager(tmp_path, fetcher=lambda _u: b"{}")
    store_all = DataStore(static)
    store_all.descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store_all._has_high_band = False
    lw.bind_store(store_all)
    assert lw.data_band_hint_lbl.text() == "当前版本全量数据"

    # Binding store WITH bands/high updates hint away from fallback notice
    if (LIVE_1622_NORMAL / "heroes.json").is_file() and (
        LIVE_1622_NORMAL / "bands" / "high" / "hero_item_recommendations.json"
    ).is_file():
        store_hi = DataStore(static)
        store_hi.load_from_dir(
            DatasetDescriptor("16.22", "normal", "常规", "patches/16.22/normal", "", True),
            LIVE_1622_NORMAL,
        )
    else:
        store_hi = DataStore(static)
        store_hi.descriptor = DatasetDescriptor("16.22", "normal", "常规", "patches/16.22/normal", "", True)
        store_hi._has_high_band = True
    lw.bind_store(store_hi)
    assert store_hi.has_high_band() is True
    assert "当前版本全量数据" not in lw.data_band_hint_lbl.text()
    assert "高分 Top5%" in lw.data_band_hint_lbl.text()

    # Commercial mode must also expose data_band_combo cleanly without forbidden tokens
    lw.close()
    monkeypatch.setenv("HEX_EDITION", "commercial")
    lw_comm = LookupWindow(cfg=AppConfig(data_band_mode="high"))
    assert lw_comm.data_band_combo.currentData() == "high"
    collected = lw_comm._collect_cfg_from_ui()
    assert collected.data_band_mode == "high"
    lw_comm.close()
    app.processEvents()
