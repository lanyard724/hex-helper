"""Tests for streamlined 2-anchor augment badge fusion and picker de-bloat config."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from hex_helper.config import AppConfig, load_config, save_config
from hex_helper.data.provider import AugmentInfo, HeroAugmentStat, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.destiny import DestinyItemRec
from hex_helper.scoring.tier import AugmentScore, UnifiedAugmentDecision
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_default_config_disables_competing_side_popup_during_picker(tmp_path: Path) -> None:
    cfg = AppConfig()
    assert cfg.combo_auto_popup_on_picker is False
    assert cfg.champ_select_goal_mode == "climb"
    assert cfg.lobby_scout_enabled is True

    cfg.combo_auto_popup_on_picker = True
    cfg.champ_select_goal_mode = "fun"
    cfg.lobby_scout_enabled = False
    p = tmp_path / "config.json"
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.combo_auto_popup_on_picker is True
    assert loaded.champ_select_goal_mode == "fun"
    assert loaded.lobby_scout_enabled is False

    clamped = AppConfig.from_dict({"champ_select_goal_mode": "invalid_mode"})
    assert clamped.champ_select_goal_mode == "climb"


def test_badge_overlay_surfaces_hex_score_and_synergy_override_directly_on_card() -> None:
    app = _qt_app()
    ov = AugmentBadgesOverlay()

    s0 = AugmentScore("10", "小丑学院", "prismatic", "S", 0.54, 0.535, 0.02, 15000, False, percentile=78.0, z_score=0.85)
    s1 = AugmentScore("11", "歌利亚巨人", "prismatic", "A", 0.525, 0.522, 0.015, 14000, False, percentile=72.0, z_score=0.55)

    u0 = UnifiedAugmentDecision(
        slot_index=0,
        base_score=s0,
        base_hex_score=79.5,
        combo_shift=-4.7,
        pair_shift=0.0,
        final_hex_score=74.8,
        effective_delta=0.012,
        shift_kind="conflict",
        shift_label="互斥 ↓4.7",
        shift_reason="与坦克生命轴冲突",
        is_best=False,
        overridden_by_combo=False,
    )
    u1 = UnifiedAugmentDecision(
        slot_index=1,
        base_score=s1,
        base_hex_score=75.8,
        combo_shift=6.5,
        pair_shift=0.0,
        final_hex_score=82.3,
        effective_delta=0.026,
        shift_kind="synergy",
        shift_label="联动 ↑6.5",
        shift_reason="与已选【钢化你心】核爆成型",
        is_best=True,
        overridden_by_combo=True,
    )

    items = [
        AugmentBadgeRenderItem(slot_index=0, score=s0, screen_box=(360, 420, 620, 460), is_best=False, unified=u0),
        AugmentBadgeRenderItem(slot_index=1, score=s1, screen_box=(820, 420, 1080, 460), is_best=True, unified=u1),
    ]
    ov.render_cards(items, screen_rect=(0, 0, 1920, 1080))
    app.processEvents()

    labels = ov.findChildren(QLabel)
    all_text = " | ".join(lbl.text() for lbl in labels)
    all_tooltips = " | ".join(w.toolTip() for w in ov.findChildren(QWidget) if w.toolTip())
    all_styles = " | ".join(w.styleSheet() for w in ov.findChildren(QWidget) if w.styleSheet())

    assert "82.3" in all_text
    assert "联动 ↑6.5" in all_text
    assert "反超首选" in all_text
    assert "互斥 ↓4.7" in all_text
    assert "钢化你心" in all_text
    assert "15.0k场" in all_text
    assert "P78" in all_tooltips
    assert "#FF5A1F" not in all_styles.upper()

    # Zero-repaint dedup: identical render call must keep existing badge instances
    ids_before = [id(b) for b in ov._badges]
    ov.render_cards(items, screen_rect=(0, 0, 1920, 1080))
    assert [id(b) for b in ov._badges] == ids_before

    # Changing unified decision must trigger re-render
    u1_updated = UnifiedAugmentDecision(
        slot_index=1,
        base_score=s1,
        base_hex_score=75.8,
        combo_shift=8.0,
        pair_shift=0.0,
        final_hex_score=83.8,
        effective_delta=0.028,
        shift_kind="synergy",
        shift_label="联动 ↑8.0",
        shift_reason="与已选【钢化你心】核爆成型",
        is_best=True,
        overridden_by_combo=True,
    )
    items_updated = [
        items[0],
        AugmentBadgeRenderItem(slot_index=1, score=s1, screen_box=(820, 420, 1080, 460), is_best=True, unified=u1_updated),
    ]
    ov.render_cards(items_updated, screen_rect=(0, 0, 1920, 1080))
    app.processEvents()
    updated_text = " | ".join(lbl.text() for lbl in ov.findChildren(QLabel))
    assert "83.8" in updated_text
    assert "联动 ↑8.0" in updated_text

    ov.close()
    app.processEvents()


def test_badge_overlay_footer_priority_and_gamble_pill() -> None:
    app = _qt_app()
    ov = AugmentBadgesOverlay()

    s_dest = AugmentScore("3081", "命运", "prismatic", "S", 0.56, 0.55, 0.03, 9000, False, percentile=90.0)
    s_gamble = AugmentScore("3018", "大腕", "prismatic", "A", 0.52, 0.518, 0.01, 5000, False, percentile=65.0)
    u_dest = UnifiedAugmentDecision(
        slot_index=0,
        base_score=s_dest,
        base_hex_score=86.0,
        combo_shift=4.0,
        pair_shift=0.0,
        final_hex_score=90.0,
        effective_delta=0.036,
        shift_kind="synergy",
        shift_label="联动 ↑4.0",
        shift_reason="不应该覆盖命运绑定",
        is_best=True,
        overridden_by_combo=False,
    )
    u_gamble = UnifiedAugmentDecision(
        slot_index=1,
        base_score=s_gamble,
        base_hex_score=67.0,
        combo_shift=3.5,
        pair_shift=0.0,
        final_hex_score=70.5,
        effective_delta=0.015,
        shift_kind="gamble",
        shift_label="高上限 ↑3.5",
        shift_reason="可赌珠光暴击核爆",
        is_best=False,
        overridden_by_combo=False,
    )

    items = [
        AugmentBadgeRenderItem(
            slot_index=0,
            score=s_dest,
            screen_box=(360, 420, 620, 460),
            is_best=True,
            destiny_items=(DestinyItemRec("3089", "灭世者的死亡之帽", 0.58, 0.04, 1200, False),),
            archetype_hint="不应覆盖",
            unified=u_dest,
        ),
        AugmentBadgeRenderItem(
            slot_index=1,
            score=s_gamble,
            screen_box=(820, 420, 1080, 460),
            is_best=False,
            archetype_hint="普通流派提示",
            unified=u_gamble,
        ),
    ]
    ov.render_cards(items, screen_rect=(0, 0, 1920, 1080))
    app.processEvents()

    all_text = " | ".join(lbl.text() for lbl in ov.findChildren(QLabel))
    assert "命运绑定" in all_text
    assert "不应该覆盖命运绑定" not in all_text
    assert "高上限 ↑3.5" in all_text
    assert "可赌珠光暴击核爆" in all_text
    assert "普通流派提示" not in all_text

    ov.close()
    app.processEvents()


def test_picker_stops_competing_side_popup_by_default_and_preserves_f7_and_opt_in(tmp_path: Path) -> None:
    from hex_helper.app import GameState, HexHelperController, HexStateMachine
    from hex_helper.vision.detector import DetectedCard

    app = _qt_app()
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
    store._heroes["21"] = HeroStat("21", "厄运小姐", "MissFortune", 12000, 6360, 0.53, 0.09, 10)
    for aid, nm, rar in (
        ("1388", "无限循环往复", "prismatic"),
        ("1045", "炼狱导管", "prismatic"),
        ("1225", "双刀流", "prismatic"),
        ("1022", "灵巧", "silver"),
    ):
        store._augments[aid] = AugmentInfo(aid, nm, rar)
    # Close base win rates so synergy vs conflict flips the unified top pick on-card
    store._hero_augments["21"] = {
        "1225": HeroAugmentStat("1225", 1000, 550, 0.55, False),
        "1045": HeroAugmentStat("1045", 1000, 542, 0.542, False),
        "1022": HeroAugmentStat("1022", 1000, 520, 0.52, False),
    }

    captured: list[AugmentBadgeRenderItem] = []

    class _FakeAugOverlay:
        def isVisible(self) -> bool:
            return False

        def hide(self) -> None:
            pass

        def render_cards(self, items, **_kwargs) -> None:
            captured[:] = list(items)

    class _FakeDrawer:
        _coach_pending = False

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
    ctl.cfg = AppConfig()
    ctl.store = store
    ctl.sm = HexStateMachine()
    ctl.sm.state = GameState.AUGMENT_PICKING
    ctl.sm.current_hero_id = "21"
    ctl.sm.selected_augments = ["1388"]
    ctl._last_live = SimpleNamespace(my_player=SimpleNamespace(item_ids=("6653",)))
    ctl.augment_overlay = _FakeAugOverlay()
    ctl.edge_drawer = _FakeDrawer()
    ctl.combo_overlay = AugmentComboOverlay()

    cards = (
        DetectedCard(0, store.get_augment("1022"), 0.95, (300, 200, 500, 300), "silver"),
        DetectedCard(1, store.get_augment("1225"), 0.96, (700, 200, 900, 300), "prismatic"),
        DetectedCard(2, store.get_augment("1045"), 0.94, (1100, 200, 1300, 300), "prismatic"),
    )
    rect = SimpleNamespace(left=0, top=0, width=1920, height=1080)

    # 1. Default config (combo_auto_popup_on_picker=False): side overlay does NOT auto-popup,
    #    while top cards carry unified HexScore + combo override directly.
    ctl._render_augment_badges(cards, game_rect=rect)
    app.processEvents()

    assert ctl.combo_overlay.isVisible() is False
    assert ctl.combo_overlay.is_expanded is False
    assert ctl.combo_overlay.mode == "plan"

    by_aid = {it.score.augment_id: it for it in captured}
    assert by_aid["1045"].unified is not None
    assert by_aid["1045"].is_best is True
    assert by_aid["1045"].unified.overridden_by_combo is True
    assert by_aid["1045"].unified.shift_kind == "synergy"
    assert by_aid["1225"].unified is not None
    assert by_aid["1225"].is_best is False
    assert by_aid["1225"].unified.shift_kind == "conflict"

    # 2. Manual F7 toggle expands combo overlay and shows picker diagnosis cleanly
    ctl._toggle_combo_hotkey()
    ctl._render_augment_badges(cards, game_rect=rect)
    app.processEvents()
    assert ctl.combo_overlay.isVisible() is True
    assert ctl.combo_overlay.is_expanded is True
    assert ctl.combo_overlay.mode == "picker"

    # 3. Opt-in combo_auto_popup_on_picker=True auto-expands even when previously collapsed
    ctl.combo_overlay.exit_picker_mode()
    ctl.combo_overlay.set_expanded(False)
    ctl.combo_overlay.hide()
    assert ctl.combo_overlay.is_expanded is False
    ctl.cfg.combo_auto_popup_on_picker = True
    ctl._render_augment_badges(cards, game_rect=rect)
    app.processEvents()
    assert ctl.combo_overlay.isVisible() is True
    assert ctl.combo_overlay.is_expanded is True
    assert ctl.combo_overlay.mode == "picker"

    ctl.combo_overlay.close()
    app.processEvents()


def test_badge_card_top_row_never_clips_across_dpi_and_4card_widths() -> None:
    from hex_helper.ui.augment_overlay import _SingleBadgeCard

    app = _qt_app()
    ov = AugmentBadgesOverlay()

    def _mk_item(
        tier: str,
        hex_score: float,
        shift_kind: str,
        shift_label: str,
        *,
        is_best: bool,
        overridden: bool,
        games: int = 14000,
        low_sample: bool = False,
    ) -> AugmentBadgeRenderItem:
        sc = AugmentScore("11", "歌利亚巨人", "prismatic", tier, 0.565, 0.555, 0.045, games, low_sample, percentile=95.0)
        u = UnifiedAugmentDecision(
            slot_index=1,
            base_score=sc,
            base_hex_score=max(15.0, hex_score - 6.5),
            combo_shift=6.5,
            pair_shift=0.0,
            final_hex_score=hex_score,
            effective_delta=0.052,
            shift_kind=shift_kind,
            shift_label=shift_label,
            shift_reason="与已选【钢化你心】核爆成型",
            is_best=is_best,
            overridden_by_combo=overridden,
        )
        return AugmentBadgeRenderItem(slot_index=1, score=sc, screen_box=(820, 420, 1080, 460), is_best=is_best, unified=u)

    cases = [
        ("override+高上限5.5", _mk_item("A", 82.3, "gamble", "高上限 ↑5.5", is_best=True, overridden=True)),
        ("override+联动11.5", _mk_item("A", 93.5, "synergy", "联动 ↑11.5", is_best=True, overridden=True)),
        ("override+高上限11.5", _mk_item("A", 99.5, "gamble", "高上限 ↑11.5", is_best=True, overridden=True)),
        ("best_S++高上限5.5", _mk_item("S+", 96.7, "gamble", "高上限 ↑5.5", is_best=True, overridden=False)),
        ("best_S++联动11.5", _mk_item("S+", 99.5, "synergy", "联动 ↑11.5", is_best=True, overridden=False)),
        ("best_S++互斥10.0", _mk_item("S+", 88.5, "conflict", "互斥 ↓10.0", is_best=True, overridden=False)),
        ("best_S++低样本+高上限", _mk_item("S+", 96.7, "gamble", "高上限 ↑5.5", is_best=True, overridden=False, games=299, low_sample=True)),
    ]

    for case_name, item in cases:
        for width in (258, 226, 207, 196, 180, 168):
            card = _SingleBadgeCard(item, animate_sheen=False, card_width=width)
            card.setParent(ov)
            card.ensurePolished()
            card.adjustSize()
            lay = card.layout()
            if lay is not None:
                lay.activate()
            assert card.minimumSizeHint().width() <= width, (
                f"{case_name} clipped at width={width}: minHint={card.minimumSizeHint().width()}"
            )
            for lbl in card.findChildren(QLabel):
                if not lbl.wordWrap():
                    assert lbl.geometry().width() >= lbl.sizeHint().width(), (
                        f"{case_name} label {lbl.text()!r} truncated at width={width}: "
                        f"geom={lbl.geometry().width()} < sizeHint={lbl.sizeHint().width()}"
                    )
            card.setParent(None)
            card.deleteLater()

    ov.close()
    app.processEvents()


def test_f7_picker_toggle_and_manual_collapse_never_ghost_reexpand(tmp_path: Path) -> None:
    """Verify F7 peek/collapse during picker preserves pre-picker collapsed preference with zero ghost re-expand."""
    from hex_helper.app import GameState, HexHelperController, HexStateMachine
    from hex_helper.vision.detector import DetectedCard

    app = _qt_app()
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
    store._heroes["21"] = HeroStat("21", "厄运小姐", "MissFortune", 12000, 6360, 0.53, 0.09, 10)
    for aid, nm, rar in (
        ("1388", "无限循环往复", "prismatic"),
        ("1045", "炼狱导管", "prismatic"),
        ("1225", "双刀流", "prismatic"),
        ("1022", "灵巧", "silver"),
    ):
        store._augments[aid] = AugmentInfo(aid, nm, rar)
    store._hero_augments["21"] = {
        "1225": HeroAugmentStat("1225", 1000, 550, 0.55, False),
        "1045": HeroAugmentStat("1045", 1000, 542, 0.542, False),
        "1022": HeroAugmentStat("1022", 1000, 520, 0.52, False),
    }

    class _FakeAugOverlay:
        def isVisible(self) -> bool:
            return False

        def hide(self) -> None:
            pass

        def render_cards(self, _items, **_kwargs) -> None:
            pass

    class _FakeDrawer:
        _coach_pending = False

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
    ctl.cfg = AppConfig()  # combo_auto_popup_on_picker=False
    ctl.store = store
    ctl.sm = HexStateMachine()
    ctl.sm.state = GameState.AUGMENT_PICKING
    ctl.sm.current_hero_id = "21"
    ctl.sm.selected_augments = ["1388"]
    ctl._last_live = SimpleNamespace(my_player=SimpleNamespace(item_ids=("6653",)))
    ctl.augment_overlay = _FakeAugOverlay()
    ctl.edge_drawer = _FakeDrawer()
    ctl.combo_overlay = AugmentComboOverlay()

    cards = (
        DetectedCard(0, store.get_augment("1022"), 0.95, (300, 200, 500, 300), "silver"),
        DetectedCard(1, store.get_augment("1225"), 0.96, (700, 200, 900, 300), "prismatic"),
        DetectedCard(2, store.get_augment("1045"), 0.94, (1100, 200, 1300, 300), "prismatic"),
    )
    rect = SimpleNamespace(left=0, top=0, width=1920, height=1080)

    # Scenario 1: Visible collapsed pill during picker -> F7 open -> F7 close -> exit_picker_mode -> stays collapsed!
    ctl._refresh_combo_plan_view()
    ctl.combo_overlay.show_overlay()
    app.processEvents()
    assert ctl.combo_overlay.mode == "plan"
    assert ctl.combo_overlay.is_expanded is False

    ctl._render_augment_badges(cards, game_rect=rect)
    app.processEvents()
    assert ctl.combo_overlay.mode == "plan"
    assert ctl.combo_overlay.is_expanded is False

    ctl._toggle_combo_hotkey()  # F7 open during picker
    app.processEvents()
    assert ctl.combo_overlay.mode == "picker"
    assert ctl.combo_overlay.is_expanded is True
    assert ctl.combo_overlay._pre_picker_expanded is False

    ctl._toggle_combo_hotkey()  # F7 close during picker
    app.processEvents()
    assert ctl.combo_overlay.mode == "picker"
    assert ctl.combo_overlay.is_expanded is False
    assert ctl.combo_overlay._pre_picker_expanded is False

    ctl.combo_overlay.exit_picker_mode()
    app.processEvents()
    assert ctl.combo_overlay.mode == "plan"
    assert ctl.combo_overlay.is_expanded is False

    # Scenario 2: Visible collapsed pill -> F7 open -> player picks card directly -> exit_picker_mode -> restores collapsed!
    ctl._render_augment_badges(cards, game_rect=rect)
    ctl._toggle_combo_hotkey()
    app.processEvents()
    assert ctl.combo_overlay.mode == "picker"
    assert ctl.combo_overlay.is_expanded is True

    ctl.combo_overlay.exit_picker_mode()
    app.processEvents()
    assert ctl.combo_overlay.mode == "plan"
    assert ctl.combo_overlay.is_expanded is False

    # Scenario 3: Hidden overlay -> F7 open during picker -> exit_picker_mode -> restores collapsed!
    ctl.combo_overlay.hide()
    ctl._render_augment_badges(cards, game_rect=rect)
    ctl._toggle_combo_hotkey()
    app.processEvents()
    assert ctl.combo_overlay.isVisible() is True
    assert ctl.combo_overlay.mode == "picker"
    assert ctl.combo_overlay.is_expanded is True

    ctl.combo_overlay.exit_picker_mode()
    app.processEvents()
    assert ctl.combo_overlay.mode == "plan"
    assert ctl.combo_overlay.is_expanded is False

    # Scenario 4: Already expanded BEFORE picker -> user manually collapses via 收起 during picker -> exit_picker_mode stays collapsed!
    ctl.combo_overlay.set_expanded(True)
    ctl.combo_overlay.show_overlay()
    ctl._render_augment_badges(cards, game_rect=rect)
    app.processEvents()
    assert ctl.combo_overlay.mode == "picker"
    assert ctl.combo_overlay.is_expanded is True
    assert ctl.combo_overlay._pre_picker_expanded is True

    ctl.combo_overlay.set_expanded(False)  # Manual collapse button during picker
    app.processEvents()
    assert ctl.combo_overlay.is_expanded is False
    assert ctl.combo_overlay._pre_picker_expanded is False

    ctl.combo_overlay.exit_picker_mode()
    app.processEvents()
    assert ctl.combo_overlay.mode == "plan"
    assert ctl.combo_overlay.is_expanded is False

    ctl.combo_overlay.close()
    app.processEvents()
