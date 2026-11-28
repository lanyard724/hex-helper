"""Tests for multi-archetype build templates, Round-1 pivot, zombie purge, Late Heartsteel guard, & pill UI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from hex_helper.bridge.protocol import CoachRequestPayload
from hex_helper.coach.gemini_coach import build_high_snr_prompt_payload
from hex_helper.config import resolve_thinking_level
from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.augment_combo import build_combo_plan, infer_match_build_profile
from hex_helper.scoring.matchup_optimizer import (
    HeroBuildTemplate,
    build_next_buy_plan,
    get_hero_build_templates,
    infer_unified_hero_profile,
    optimize_next_items,
    reconcile_match_build_plan_with_trace,
)
from hex_helper.ui.edge_drawer import EdgeBuildDrawer

SERVER_ROOT = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from hex_server import coach_gateway as server_gw  # noqa: E402


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_archetype_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900}, "", "complete_equipment"),
        "3083": ItemStaticInfo("3083", "狂徒铠甲", 3100, ("Health",), {"FlatHPPoolMod": 1000}, "", "complete_equipment"),
        "3119": ItemStaticInfo("3119", "凛冬之临", 2400, ("Health", "Mana"), {"FlatHPPoolMod": 550, "FlatMPPoolMod": 860}, "", "complete_equipment"),
        "2502": ItemStaticInfo("2502", "无终恨意", 2800, ("Health", "Armor"), {"FlatHPPoolMod": 400, "FlatArmorMod": 60}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Health", "Armor"), {"FlatHPPoolMod": 150, "FlatArmorMod": 75}, "重伤", "complete_equipment", has_grievous=True),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, ("Health", "SpellBlock"), {"FlatHPPoolMod": 400, "FlatSpellBlockMod": 50}, "", "complete_equipment"),
        "2501": ItemStaticInfo("2501", "霸王血铠", 3300, ("Health", "Damage"), {"FlatHPPoolMod": 500, "FlatPhysicalDamageMod": 30}, "", "complete_equipment"),
        "3110": ItemStaticInfo("3110", "冰霜之心", 2500, ("Armor", "Mana", "Aura"), {"FlatArmorMod": 75, "FlatMPPoolMod": 400}, "", "complete_equipment"),
        "3118": ItemStaticInfo("3118", "残疫", 2700, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 85, "FlatMPPoolMod": 600}, "", "complete_equipment"),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage", "MagicPenetration"), {"FlatMagicDamageMod": 115}, "", "complete_equipment"),
        "4646": ItemStaticInfo("4646", "风暴狂涌", 2800, ("SpellDamage", "MagicPenetration"), {"FlatMagicDamageMod": 90}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage", "MagicPenetration"), {"FlatMagicDamageMod": 95}, "", "complete_equipment"),
        "6655": ItemStaticInfo("6655", "卢登的回声", 2850, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 100, "FlatMPPoolMod": 600}, "", "complete_equipment"),
        "2503": ItemStaticInfo("2503", "黯炎火炬", 2800, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 80, "FlatMPPoolMod": 600}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105, "FlatArmorMod": 50}, "", "complete_equipment"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {"FlatMovementSpeedMod": 45}, "", "boots"),
        "3111": ItemStaticInfo("3111", "水银之靴", 1250, ("Boots",), {"FlatMovementSpeedMod": 45, "FlatSpellBlockMod": 25}, "", "boots"),
        "3047": ItemStaticInfo("3047", "铁板靴", 1200, ("Boots",), {"FlatMovementSpeedMod": 45, "FlatArmorMod": 25}, "", "boots"),
        "3158": ItemStaticInfo("3158", "明朗之靴", 900, ("Boots",), {"FlatMovementSpeedMod": 45}, "", "boots"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3600, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3142": ItemStaticInfo("3142", "幽梦之灵", 2800, ("Damage",), {"FlatPhysicalDamageMod": 55}, "", "complete_equipment"),
        "6676": ItemStaticInfo("6676", "收集者", 3000, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 50, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({
            "53": {"name": "布里茨", "title": "蒸汽机器人", "games": 200000, "wins": 104000, "win_rate": 0.52, "pick_rate": 0.05, "rank": 20},
            "36": {"name": "蒙多医生", "title": "祖安狂人", "games": 200000, "wins": 104000, "win_rate": 0.52, "pick_rate": 0.05, "rank": 25},
            "3": {"name": "加里奥", "title": "正义巨像", "games": 200000, "wins": 104000, "win_rate": 0.52, "pick_rate": 0.05, "rank": 22},
        }),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(
        json.dumps({
            "1068": {"name": "循环往复", "rarity": "silver"},
            "1388": {"name": "无限循环往复", "rarity": "gold"},
            "1053": {"name": "恶火小斧", "rarity": "gold"},
            "1152": {"name": "钢化你心", "rarity": "gold"},
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_augments.json").write_text(
        json.dumps({
            "53": {
                "1068": {"games": 10000, "wins": 5400, "win_rate": 0.54, "low_sample": False},
                "1388": {"games": 9000, "wins": 5040, "win_rate": 0.56, "low_sample": False},
            },
            "36": {"1152": {"games": 15000, "wins": 8400, "win_rate": 0.56, "low_sample": False}},
            "3": {},
        }),
        encoding="utf-8",
    )
    (ds_dir / "core_builds.json").write_text(
        json.dumps({
            "53": [
                {"items": ["3084", "3119", "3083"], "ordered": True, "games": 45000, "wins": 24300, "win": 0.54, "pick": 0.35},
                {"items": ["3084", "3083", "2502"], "ordered": True, "games": 32000, "wins": 16960, "win": 0.53, "pick": 0.25},
                {"items": ["3118", "4645", "3089"], "ordered": True, "games": 18000, "wins": 9540, "win": 0.53, "pick": 0.14},
            ],
            "36": [
                {"items": ["3084", "3083", "3065", "2502"], "ordered": True, "games": 50000, "wins": 27000, "win": 0.54, "pick": 0.45},
            ],
            "3": [
                {"items": ["3084", "2502", "3083"], "ordered": True, "games": 40000, "wins": 21200, "win": 0.53, "pick": 0.40},
            ],
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "53": {
                "3084": {"games": 60000, "wins": 33000, "win_rate": 0.55, "low_sample": False},
                "3083": {"games": 35000, "wins": 18900, "win_rate": 0.54, "low_sample": False},
                "3119": {"games": 30000, "wins": 16200, "win_rate": 0.54, "low_sample": False},
                "3118": {"games": 18000, "wins": 9720, "win_rate": 0.54, "low_sample": False},
                "4645": {"games": 16000, "wins": 8480, "win_rate": 0.53, "low_sample": False},
                "4646": {"games": 14000, "wins": 7420, "win_rate": 0.53, "low_sample": False},
                "3089": {"games": 15000, "wins": 8250, "win_rate": 0.55, "low_sample": False},
                "3135": {"games": 10000, "wins": 5300, "win_rate": 0.53, "low_sample": False},
                "6655": {"games": 11000, "wins": 5830, "win_rate": 0.53, "low_sample": False},
            },
            "36": {
                "3084": {"games": 50000, "wins": 27000, "win_rate": 0.54, "low_sample": False},
                "3083": {"games": 32000, "wins": 17600, "win_rate": 0.55, "low_sample": False},
                "3065": {"games": 28000, "wins": 15400, "win_rate": 0.55, "low_sample": False},
                "2502": {"games": 26000, "wins": 14560, "win_rate": 0.56, "low_sample": False},
                "3075": {"games": 24000, "wins": 13200, "win_rate": 0.55, "low_sample": False},
                "2501": {"games": 20000, "wins": 11000, "win_rate": 0.55, "low_sample": False},
            },
            "3": {
                "3084": {"games": 40000, "wins": 21200, "win_rate": 0.53, "low_sample": False},
                "2502": {"games": 25000, "wins": 13250, "win_rate": 0.53, "low_sample": False},
                "4646": {"games": 6000, "wins": 3180, "win_rate": 0.53, "low_sample": False},
                "4645": {"games": 6000, "wins": 3180, "win_rate": 0.53, "low_sample": False},
                "3089": {"games": 5500, "wins": 3025, "win_rate": 0.55, "low_sample": False},
            },
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    return store


def _squishy_roster() -> tuple[list[PlayerLiveInfo], list[PlayerLiveInfo]]:
    allies = [PlayerLiveInfo("me", "布里茨", "Blitzcrank", "ORDER", False, 10, 4, 3, 8, ())]
    enemies = [
        PlayerLiveInfo("e1", "卢锡安", "Lucian", "CHAOS", False, 10, 5, 3, 4, ("3031",)),
        PlayerLiveInfo("e2", "派克", "Pyke", "CHAOS", False, 10, 4, 4, 4, ("3142",)),
        PlayerLiveInfo("e3", "烬", "Jhin", "CHAOS", False, 10, 3, 4, 5, ("6676",)),
        PlayerLiveInfo("e4", "奥莉安娜", "Orianna", "CHAOS", False, 10, 3, 4, 6, ("6655",)),
    ]
    return allies, enemies


def test_get_hero_build_templates_bigdata_and_preset(tmp_path: Path) -> None:
    store = _make_archetype_store(tmp_path)
    blitz_tmpls = get_hero_build_templates(store, "53")
    assert [(t.profile, t.source) for t in blitz_tmpls] == [("TANK", "bigdata"), ("AP", "bigdata")]

    galio_tmpls = get_hero_build_templates(store, "3")
    assert [(t.profile, t.source) for t in galio_tmpls] == [("TANK", "bigdata"), ("AP", "preset")]
    assert galio_tmpls[1].label == "AP核弹流"


def test_blitzcrank_round1_recursion_pivot_to_ap_no_heartsteel(tmp_path: Path) -> None:
    store = _make_archetype_store(tmp_path)
    allies, enemies = _squishy_roster()
    assert infer_unified_hero_profile("53", store, my_items=[], selected_augments=[]) == "TANK"
    assert infer_unified_hero_profile("53", store, my_items=[], selected_augments=["1068"]) == "AP"

    _, recs = optimize_next_items(store, "53", [], 1400.0, ["1068"], allies, enemies, game_time=18.7)
    ids = [r.item_id for r in recs]
    assert "3084" not in ids
    assert recs[0].item_id == "3118"
    assert set(ids).issubset({"3118", "4645", "4646", "3089", "3135", "6655", "2503", "3157", "3110"})

    plan = build_next_buy_plan(store, "53", [], 1400.0, recs, selected_augments=["1068"], game_time=18.7)
    assert plan is not None
    assert plan.active_profile == "AP"
    assert "3084" not in plan.remaining_build_ids


def test_ap_drift_zombie_tank_queue_purge_and_no_heartsteel_at_third_item(tmp_path: Path) -> None:
    store = _make_archetype_store(tmp_path)
    allies, enemies = _squishy_roster()
    my_items = ["4646", "4645"]
    augs = ["1068", "1388"]
    prof = infer_unified_hero_profile("53", store, my_items=my_items, selected_augments=augs)
    assert prof == "AP"

    _, recs = optimize_next_items(
        store,
        "53",
        my_items,
        2200.0,
        augs,
        allies,
        enemies,
        committed_plan=["3084", "3118", "3083"],
        locked_next_item="3084",
        game_time=493.5,
    )
    assert all(r.item_id not in ("3084", "3083", "3119") for r in recs)

    plan = build_next_buy_plan(store, "53", my_items, 2200.0, recs, selected_augments=augs, game_time=493.5)
    assert plan is not None
    assert plan.active_profile == "AP"
    assert "3084" not in plan.remaining_build_ids

    new_lock, new_plan, trace = reconcile_match_build_plan_with_trace(
        store,
        my_items,
        ["3084", "3118", "3083"],
        "3084",
        list(plan.remaining_build_ids),
        allow_overhaul=False,
        hero_id="53",
        selected_augments=augs,
        active_profile=plan.active_profile,
    )
    assert new_lock != "3084"
    assert "3084" not in new_plan
    assert "3083" not in new_plan
    assert trace["overridden"] is True
    assert trace["override_reason"] == "profile_drift_cleanup"


def test_late_heartsteel_penalty_at_two_completes_or_eight_minutes(tmp_path: Path) -> None:
    store = _make_archetype_store(tmp_path)
    allies = [PlayerLiveInfo("me", "蒙多医生", "DrMundo", "ORDER", False, 10, 4, 3, 8, ())]
    enemies = [
        PlayerLiveInfo("e1", "卢锡安", "Lucian", "CHAOS", False, 10, 4, 3, 4, ("3031",)),
        PlayerLiveInfo("e2", "奥莉安娜", "Orianna", "CHAOS", False, 10, 4, 3, 5, ("6655",)),
    ]
    _, recs_late = optimize_next_items(
        store, "36", ["3075", "3065"], 3000.0, [], allies, enemies, limit=8, game_time=500.0, collect_breakdown=True
    )
    hs_late = next((r for r in recs_late if r.item_id == "3084"), None)
    assert recs_late[0].item_id != "3084"
    if hs_late is not None:
        assert "顺位过晚" in hs_late.tags
        assert hs_late.score_breakdown["penalties"] <= -6.5

    # Exempt with Steel Your Heart (1152) at <=1 complete and <720s
    _, recs_1152 = optimize_next_items(
        store, "36", ["3075"], 3000.0, ["1152"], allies, enemies, limit=4, game_time=500.0, collect_breakdown=True
    )
    assert recs_1152[0].item_id == "3084"
    assert "顺位过晚" not in recs_1152[0].tags


def test_manual_preferred_profile_overrides_optimizer_and_combo(tmp_path: Path) -> None:
    store = _make_archetype_store(tmp_path)
    allies, enemies = _squishy_roster()
    assert infer_unified_hero_profile("53", store, my_items=[], selected_augments=[], preferred_profile="AP") == "AP"

    _, recs = optimize_next_items(store, "53", [], 1400.0, [], allies, enemies, preferred_profile="AP")
    assert recs[0].item_id == "3118"
    assert all(r.item_id != "3084" for r in recs)

    plan = build_next_buy_plan(store, "53", [], 1400.0, recs, preferred_profile="AP")
    assert plan is not None and plan.active_profile == "AP"

    bp = infer_match_build_profile(store, "53", [], [], preferred_profile="AP")
    assert bp.primary_profile == "AP"
    assert "AP" in bp.direction_label or "法" in bp.direction_label or "爆发" in bp.direction_label
    combo = build_combo_plan(store, "53", [], [], preferred_profile="AP")
    assert combo.profile.primary_profile == "AP"
    assert combo.recommended_targets or combo.opening_combos or combo.profile.direction_label


def test_edge_drawer_archetype_pills_switch_and_nofocus() -> None:
    _qt_app()
    selected: list[str] = []
    drawer = EdgeBuildDrawer(on_select_archetype=lambda p: selected.append(p))
    opts = [
        HeroBuildTemplate("TANK", "重装坦", ("3084", "3119", "3083"), "bigdata"),
        HeroBuildTemplate("AP", "AP法强", ("3118", "4645", "3089"), "bigdata"),
    ]
    drawer.set_archetype_options(opts, "TANK", is_manual=False)
    assert not drawer.archetype_bar_wrap.isHidden()
    assert len(drawer._archetype_btns) == 2
    assert all(b.focusPolicy() == Qt.FocusPolicy.NoFocus for b in drawer._archetype_btns)

    ap_btn = next(b for b in drawer._archetype_btns if b.property("archetype_profile") == "AP")
    ap_btn.click()
    assert selected == ["AP"]

    # Fewer than 2 templates hides the switcher cleanly
    drawer.set_archetype_options(opts[:1], "TANK", is_manual=False)
    assert drawer.archetype_bar_wrap.isHidden()
    drawer.close()


def test_high_snr_payload_preserves_diet_and_honors_selected_archetype(tmp_path: Path) -> None:
    store = _make_archetype_store(tmp_path)
    req = CoachRequestPayload(
        request_id="arch-req-1",
        trigger_reason="archetype_switched",
        timestamp="2026-11-28T12:00:00Z",
        dataset_patch="16.23",
        my_hero={"hero_id": "53", "name": "布里茨", "gold": 1400, "items": []},
        selected_augments=[],
        allies=[],
        enemies=[{"champion": "卢锡安", "kda": "1/0/0", "items": ["无尽之刃"]}],
        committed_build_plan={
            "owned_complete": [],
            "locked_next_item": {"item_id": "3118", "name": "残疫"},
            "remaining_plan": ["残疫", "影焰", "灭世者的死亡之帽"],
            "allow_overhaul": True,
            "overhaul_reason": "手动流派：AP法强",
            "selected_archetype": "AP",
            "available_archetypes": ["重装坦", "AP法强"],
        },
    )
    diet = build_high_snr_prompt_payload(req, store)
    keys = list(diet.keys())
    assert keys[:4] == ["dataset_patch", "hero_static", "hero_big_data_top_builds", "item_glossary"]
    assert keys.index("item_glossary") < keys.index("trigger_reason")
    assert len(json.dumps(diet, ensure_ascii=False, separators=(",", ":"))) <= 2150
    assert "3084" not in diet["item_glossary"]
    assert any(k in diet["item_glossary"] for k in ("3118", "4645", "3089"))
    assert diet["committed_build_plan"]["selected_archetype"] == "AP"

    sliced = server_gw.slice_prompt_payload_high_snr(diet, "archetype_switched")
    assert sliced["committed_build_plan"]["selected_archetype"] == "AP"
    assert sliced["committed_build_plan"]["available_archetypes"] == ["重装坦", "AP法强"]
    assert resolve_thinking_level("high", "archetype_switched", adaptive=True) == "low"
    assert server_gw.resolve_server_thinking_level("archetype_switched") == "low"
