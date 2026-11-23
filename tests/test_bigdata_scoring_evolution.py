"""Comprehensive tests for Big-Data Scoring Engine Evolution (Patch 16.23)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from hex_helper.data.augment_combos import VALID_MECHANIC_AXES, get_augment_combo_db
from hex_helper.data.hero_signatures import get_hero_signature_db
from hex_helper.data.provider import AugmentInfo, DatasetDescriptor, ItemStaticInfo
from hex_helper.data.special_builds import get_special_augment_db
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.augment_combo import build_combo_plan, evaluate_picker_combos
from hex_helper.scoring.matchup_optimizer import (
    analyze_matchup_threats,
    build_next_buy_plan,
    optimize_next_items,
    recommend_counter_boots,
)
from hex_helper.scoring.tier import RarityDeltaDist, score_hero_augment


def _local_1623_root() -> Path | None:
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app:
        return None
    p = Path(local_app) / "HexHelper" / "cache" / "datasets" / "16.23"
    return p if (p / "normal" / "heroes.json").is_file() else None


def _write_ds(ds_dir: Path, heroes: dict, augs: dict, hero_augs: dict, core: dict, item_recs: dict) -> None:
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "heroes.json").write_text(json.dumps(heroes, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "augments.json").write_text(json.dumps(augs, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "hero_augments.json").write_text(json.dumps(hero_augs, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(json.dumps(core, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "hero_item_recommendations.json").write_text(json.dumps(item_recs, ensure_ascii=False), encoding="utf-8")


def test_tear_item_pair_merging_and_no_double_counting(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3003": ItemStaticInfo("3003", "大天使之杖", 2900, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 70}, "", "complete_equipment"),
        "3040": ItemStaticInfo("3040", "炽天使之拥", 2900, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 70}, "", "complete_equipment"),
        "3004": ItemStaticInfo("3004", "魔宗", 2900, ("Damage", "Mana"), {"FlatPhysicalDamageMod": 35}, "", "complete_equipment"),
        "3042": ItemStaticInfo("3042", "魔切", 2900, ("Damage", "Mana"), {"FlatPhysicalDamageMod": 35}, "", "complete_equipment"),
        "3119": ItemStaticInfo("3119", "凛冬之临", 2400, ("Health", "Mana"), {"FlatHPPoolMod": 550}, "", "complete_equipment"),
        "3121": ItemStaticInfo("3121", "末日寒冬", 2400, ("Health", "Mana"), {"FlatHPPoolMod": 550}, "", "complete_equipment"),
        "2526": ItemStaticInfo("2526", "耳语头环", 2250, ("Health", "Mana", "ManaRegen"), {"FlatHPPoolMod": 200}, "", "complete_equipment"),
        "2530": ItemStaticInfo("2530", "歌之权冠", 2250, ("Health", "Mana", "ManaRegen"), {"FlatHPPoolMod": 200}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "16.23" / "normal"
    _write_ds(
        ds_dir,
        heroes={"13": {"name": "瑞兹", "title": "符文法师", "games": 50000, "wins": 25000, "win_rate": 0.50, "pick_rate": 0.03, "rank": 50}},
        augs={"1002": {"name": "尖端发明家", "rarity": "gold"}},
        hero_augs={"13": {}},
        core={"13": [{"items": ["3003", "3089"], "ordered": True, "games": 20000, "wins": 10600, "win": 0.53, "pick": 0.4}]},
        item_recs={
            "13": {
                # Unstacked 3003 has low WR (42%) while stacked 3040 has high WR (56%) -> merged should be 52.5%!
                "3003": {"games": 2000, "wins": 840, "win_rate": 0.42, "low_sample": False},
                "3040": {"games": 6000, "wins": 3360, "win_rate": 0.56, "low_sample": False},
                "3004": {"games": 1000, "wins": 440, "win_rate": 0.44, "low_sample": False},
                "3042": {"games": 3000, "wins": 1620, "win_rate": 0.54, "low_sample": False},
                "3119": {"games": 500, "wins": 225, "win_rate": 0.45, "low_sample": False},
                "3121": {"games": 1500, "wins": 825, "win_rate": 0.55, "low_sample": False},
                "2526": {"games": 400, "wins": 176, "win_rate": 0.44, "low_sample": False},
                "2530": {"games": 1600, "wins": 896, "win_rate": 0.56, "low_sample": False},
            }
        },
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    recs = store.get_hero_item_recs("13")
    for base_id, stack_id, exp_games, exp_wins in (
        ("3003", "3040", 8000, 4200),
        ("3004", "3042", 4000, 2060),
        ("3119", "3121", 2000, 1050),
        ("2526", "2530", 2000, 1072),
    ):
        assert recs[base_id].games == exp_games
        assert recs[base_id].wins == exp_wins
        assert recs[stack_id].games == exp_games
        assert recs[stack_id].wins == exp_wins
        assert abs(recs[base_id].win_rate - (exp_wins / exp_games)) < 1e-6
        assert abs(recs[stack_id].win_rate - recs[base_id].win_rate) < 1e-6

    # Ensure matchup_optimizer does NOT double count 8000 + 8000 = 16000 games!
    _, dyn = optimize_next_items(store, "13", (), 3000.0, [], [], [], limit=4)
    arch = next(r for r in dyn if r.item_id == "3003")
    assert arch.games == 8000


def test_item_global_debiasing_and_hierarchical_role_priors(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3165": ItemStaticInfo("3165", "莫雷洛秘典", 2850, ("SpellDamage", "Health"), {"FlatMagicDamageMod": 75, "FlatHPPoolMod": 350}, "重伤", "complete_equipment", has_grievous=True),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Armor", "Health"), {"FlatArmorMod": 75, "FlatHPPoolMod": 150}, "重伤", "complete_equipment", has_grievous=True),
        "6655": ItemStaticInfo("6655", "卢登的回声", 2850, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 100}, "", "complete_equipment"),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
        "2517": ItemStaticInfo("2517", "无穷饥渴", 3100, ("Damage", "LifeSteal"), {"FlatPhysicalDamageMod": 65}, "", "complete_equipment"),
        "6697": ItemStaticInfo("6697", "狂妄", 2800, ("Damage", "ArmorPenetration"), {"FlatPhysicalDamageMod": 55}, "", "complete_equipment"),
        "126697": ItemStaticInfo("126697", "狂妄", 2950, ("Damage", "ArmorPenetration"), {"FlatPhysicalDamageMod": 55}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105, "FlatArmorMod": 50}, "", "complete_equipment"),
    }
    heroes = {
        "99": {"name": "拉克丝", "title": "光辉", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 50},
        "101": {"name": "泽拉斯", "title": "远古巫灵", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 50},
        "63": {"name": "布兰德", "title": "复仇焰魂", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 50},
        "103": {"name": "阿狸", "title": "九尾妖狐", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 50},
    }
    core = {hid: [{"items": ["6655", "4645", "3089"], "ordered": True, "games": 30000, "wins": 15300, "win": 0.51, "pick": 0.3}] for hid in heroes}
    # Simulate reverse-causality bias: Morello (3165) has -7pp delta across all AP mages; 2517 has +3pp snowball bias;
    # 3157 Zhonya has strong +1.5pp class delta on 3 AP mages but cold sample (80 games) on Ahri (103).
    item_recs = {
        "99": {
            "6655": {"games": 20000, "wins": 10200, "win_rate": 0.51, "low_sample": False},
            "4645": {"games": 18000, "wins": 9360, "win_rate": 0.52, "low_sample": False},
            "3089": {"games": 15000, "wins": 7800, "win_rate": 0.52, "low_sample": False},
            "3165": {"games": 5000, "wins": 2150, "win_rate": 0.43, "low_sample": False},
            "3075": {"games": 2000, "wins": 890, "win_rate": 0.445, "low_sample": False},
            "2517": {"games": 2000, "wins": 1060, "win_rate": 0.53, "low_sample": False},
            "126697": {"games": 2000, "wins": 1040, "win_rate": 0.52, "low_sample": False},
            "3157": {"games": 4000, "wins": 2100, "win_rate": 0.525, "low_sample": False},
        },
        "101": {
            "6655": {"games": 20000, "wins": 10200, "win_rate": 0.51, "low_sample": False},
            "3089": {"games": 15000, "wins": 7800, "win_rate": 0.52, "low_sample": False},
            "3165": {"games": 4000, "wins": 1720, "win_rate": 0.43, "low_sample": False},
            "3075": {"games": 2000, "wins": 890, "win_rate": 0.445, "low_sample": False},
            "2517": {"games": 2000, "wins": 1060, "win_rate": 0.53, "low_sample": False},
            "126697": {"games": 2000, "wins": 1040, "win_rate": 0.52, "low_sample": False},
            "3157": {"games": 4000, "wins": 2100, "win_rate": 0.525, "low_sample": False},
        },
        "63": {
            "6655": {"games": 20000, "wins": 10200, "win_rate": 0.51, "low_sample": False},
            "3089": {"games": 15000, "wins": 7800, "win_rate": 0.52, "low_sample": False},
            "3165": {"games": 4000, "wins": 1720, "win_rate": 0.43, "low_sample": False},
            "3157": {"games": 4000, "wins": 2100, "win_rate": 0.525, "low_sample": False},
        },
        "103": {
            "6655": {"games": 20000, "wins": 10200, "win_rate": 0.51, "low_sample": False},
            "3157": {"games": 80, "wins": 40, "win_rate": 0.50, "low_sample": True},
        },
    }
    ds_dir = tmp_path / "ds_bias"
    _write_ds(ds_dir, heroes, {"1002": {"name": "尖端发明家", "rarity": "gold"}}, {h: {} for h in heroes}, core, item_recs)

    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    assert abs(store.get_item_global_bias("3165") - (-0.07)) < 1e-4
    assert abs(store.get_item_global_bias("3075") - (-0.055)) < 1e-4
    assert abs(store.get_item_global_bias("2517") - 0.03) < 1e-4
    # 6-digit Mayhem IDs normalize to 4-digit canonical bias
    assert abs(store.get_item_global_bias("6697") - 0.02) < 1e-4
    assert abs(store.get_item_global_bias("126697") - 0.02) < 1e-4

    # Hierarchical AP role prior for Zhonya (3157) reflects +2.5pp AP class performance
    ap_zhonya_prior = store.get_role_item_prior("AP", "3157")
    assert ap_zhonya_prior is not None and abs(ap_zhonya_prior - 0.025) < 1e-4

    # Against heavy healing enemies at 2 items, Morello (3165) must NOT be crushed by its -7pp reverse-causality bias!
    allies = [PlayerLiveInfo("me", "拉克丝", "Lux", "ORDER", False, 14, 4, 3, 8, ("6655", "4645"))]
    enemies = [
        PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 15, 8, 2, 4, ("2517",)),
        PlayerLiveInfo("e2", "索拉卡", "Soraka", "CHAOS", False, 14, 1, 2, 14, ("2526",)),
        PlayerLiveInfo("e3", "弗拉基米尔", "Vladimir", "CHAOS", False, 14, 6, 3, 5, ()),
    ]
    _, recs = optimize_next_items(store, "99", ("6655", "4645"), 3000.0, [], allies, enemies, limit=4)
    assert recs[0].item_id == "3165"
    assert recs[0].score > 5.0

    # Verify Ahri (103) cold-sample Zhonya (80 < 300 games) uses hierarchical AP role prior without crashing
    _, ahri_recs = optimize_next_items(store, "103", ("6655", "4645"), 3300.0, [], [], enemies, limit=4)
    zhonya_ahri = next(r for r in ahri_recs if r.item_id == "3157")
    assert zhonya_ahri.games == 80
    assert zhonya_ahri.score > 0.0


def test_rarity_percentile_tiering_and_backward_compatibility(tmp_path: Path) -> None:
    # Build skewed Gold rarity distribution where median is -1.3pp and P90 is +2.2pp
    deltas = tuple(sorted([round(-0.045 + i * 0.0014, 5) for i in range(50)]))
    mean_v = sum(deltas) / len(deltas)
    std_v = (sum((x - mean_v) ** 2 for x in deltas) / len(deltas)) ** 0.5
    gold_dist = RarityDeltaDist(
        mean=mean_v,
        std=std_v,
        p25=deltas[12],
        median=deltas[25],
        p75=deltas[37],
        p90=deltas[45],
        sorted_deltas=deltas,
    )

    # 1. Top Gold augment (+2.2pp, >P90) gets S+ with rarity_dist (would only be S under legacy fixed +3.5pp!)
    top_gold = score_hero_augment("1001", "金卡顶配", "gold", 0.50, wins=5220, games=10000, rarity_dist=gold_dist)
    assert top_gold.tier == "S+"
    assert top_gold.percentile is not None and top_gold.percentile >= 90.0
    assert top_gold.z_score is not None and top_gold.z_score > 1.0
    assert top_gold.is_below_rarity_par is False

    # 2. Legacy call without rarity_dist preserves old behavior and default fields
    legacy = score_hero_augment("1001", "金卡顶配", "gold", 0.50, wins=5220, games=10000)
    assert legacy.tier == "S"
    assert legacy.percentile is None
    assert legacy.z_score is None
    assert legacy.is_below_rarity_par is False

    # 3. Above-median slightly negative Gold (-0.5pp, ~P57) gets A and is NOT flagged below rarity par
    mid_gold = score_hero_augment("1002", "中上金卡", "gold", 0.50, wins=4950, games=10000, rarity_dist=gold_dist)
    assert mid_gold.tier == "A"
    assert mid_gold.is_below_rarity_par is False

    # 4. Bottom-tier Gold (-2.5pp, <P38 & <-0.8pp) IS flagged is_below_rarity_par=True
    low_gold = score_hero_augment("1003", "陷阱金卡", "gold", 0.50, wins=4750, games=10000, rarity_dist=gold_dist)
    assert low_gold.is_below_rarity_par is True

    # 5. Absolute guardrails: delta >= +3.5pp -> S+; delta <= -4.0pp -> D
    huge = score_hero_augment("1004", "超模", "gold", 0.50, wins=5380, games=10000, rarity_dist=gold_dist)
    assert huge.tier == "S+"
    disaster = score_hero_augment("1005", "灾难", "gold", 0.50, wins=4550, games=10000, rarity_dist=gold_dist)
    assert disaster.tier == "D"

    # 6. Verify DataStore precomputes (hero_id, rarity) distributions automatically
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    ds_dir = tmp_path / "ds_rar"
    augs = {str(2000 + i): {"name": f"卡{i}", "rarity": "gold"} for i in range(10)}
    ha = {"164": {str(2000 + i): {"games": 1000, "wins": 450 + i * 10, "win_rate": (450 + i * 10) / 1000.0, "low_sample": False} for i in range(10)}}
    _write_ds(
        ds_dir,
        heroes={"164": {"name": "卡蜜尔", "title": "青钢影", "games": 50000, "wins": 25000, "win_rate": 0.50, "pick_rate": 0.02, "rank": 80}},
        augs=augs,
        hero_augs=ha,
        core={},
        item_recs={},
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    dist = store.get_hero_rarity_distribution("164", "gold")
    assert dist is not None
    assert len(dist.sorted_deltas) == 10
    assert dist.p25 <= dist.median <= dist.p75 <= dist.p90


def test_camille_missing_anchor_combo_not_completed_and_picker_anchor_gating(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store._augments = {
        "3058": AugmentInfo("3058", "嗡嗡怪兽", "gold"),
        "1007": AugmentInfo("1007", "大力", "silver"),
        "1225": AugmentInfo("1225", "双刀流", "prismatic"),
        "1336": AugmentInfo("1336", "升级：无尽之刃", "gold"),
        "2005": AugmentInfo("2005", "扳机炼狱", "prismatic"),
    }

    # Camille holds 1007 (大力) + 1225 (双刀流), WITHOUT anchor 3058 (嗡嗡怪兽)
    plan = build_combo_plan(store, "164", ["1007", "1225"], ())
    tickle = next((c for c in plan.active_combos if c.combo_id == "tickle_monster_ad_speed"), None)
    assert tickle is not None
    assert tickle.completed is False
    assert tickle.missing_core_ids == ("3058",)
    # Anchored dual_wield_machinegun (owns 1225!) must outrank unanchored tickle_monster_ad_speed
    assert plan.active_combos[0].combo_id == "dual_wield_machinegun"

    # Picker offering follow-up 1336 while missing anchor 3058 must NOT falsely link to 嗡嗡怪兽纯 AD 极速
    v_fup = evaluate_picker_combos(store, "164", ["1007", "1225"], (), [(0, store.get_augment("1336"))])
    assert v_fup.cards[0].combo_name != "嗡嗡怪兽纯 AD 极速"

    # Picker offering the missing anchor 3058 itself IS a true jackpot!
    v_anchor = evaluate_picker_combos(store, "164", ["1007", "1225"], (), [(0, store.get_augment("3058"))])
    assert v_anchor.cards[0].verdict == "jackpot"
    assert v_anchor.cards[0].combo_name == "嗡嗡怪兽纯 AD 极速"

    # Once anchor 3058 + 1007 ARE both owned, tickle_monster_ad_speed becomes completed=True!
    plan_done = build_combo_plan(store, "164", ["3058", "1007"], ())
    tickle_done = next(c for c in plan_done.active_combos if c.combo_id == "tickle_monster_ad_speed")
    assert tickle_done.completed is True


def test_camille_real_match_threats_survival_override_and_four_item_no_boots(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "1001": ItemStaticInfo("1001", "速度之靴", 300, ("Boots",), {}, "", "boots"),
        "3047": ItemStaticInfo("3047", "铁板靴", 1100, ("Boots",), {"FlatArmorMod": 25}, "", "boots", components=("1001",)),
        "3078": ItemStaticInfo("3078", "三相之力", 3333, ("Damage", "Health", "AttackSpeed"), {"FlatPhysicalDamageMod": 36, "FlatHPPoolMod": 333, "PercentAttackSpeedMod": 0.30}, "", "complete_equipment"),
        "3074": ItemStaticInfo("3074", "贪欲九头蛇", 3300, ("Damage", "LifeSteal"), {"FlatPhysicalDamageMod": 65, "PercentLifeStealMod": 0.12}, "", "complete_equipment"),
        "2517": ItemStaticInfo("2517", "无穷饥渴", 3100, ("Damage", "LifeSteal"), {"FlatPhysicalDamageMod": 65}, "", "complete_equipment"),
        "3053": ItemStaticInfo("3053", "斯特拉克的挑战护手", 3200, ("Health", "Damage"), {"FlatHPPoolMod": 400}, "", "complete_equipment"),
        "6672": ItemStaticInfo("6672", "海妖杀手", 3000, ("Damage", "AttackSpeed", "OnHit"), {"FlatPhysicalDamageMod": 45, "PercentAttackSpeedMod": 0.40}, "", "complete_equipment"),
        "6333": ItemStaticInfo("6333", "死亡之舞", 3300, ("Damage", "Armor"), {"FlatPhysicalDamageMod": 60, "FlatArmorMod": 50}, "", "complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900}, "", "complete_equipment"),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, ("Health", "SpellBlock"), {"FlatHPPoolMod": 400, "FlatSpellBlockMod": 50}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Health", "Armor"), {"FlatHPPoolMod": 150, "FlatArmorMod": 75}, "", "complete_equipment"),
        "2504": ItemStaticInfo("2504", "败魔", 2900, ("Health", "SpellBlock"), {"FlatHPPoolMod": 400, "FlatSpellBlockMod": 80}, "", "complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3600, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "6697": ItemStaticInfo("6697", "狂妄", 2800, ("Damage", "ArmorPenetration"), {"FlatPhysicalDamageMod": 55}, "", "complete_equipment"),
        "2526": ItemStaticInfo("2526", "耳语头环", 2250, ("Health", "Mana", "ManaRegen"), {"FlatHPPoolMod": 200}, "", "complete_equipment"),
        "2525": ItemStaticInfo("2525", "原生质护带", 2600, ("Health",), {"FlatHPPoolMod": 600}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_camille"
    _write_ds(
        ds_dir,
        heroes={"164": {"name": "卡蜜尔", "title": "青钢影", "games": 120000, "wins": 58000, "win_rate": 0.483, "pick_rate": 0.02, "rank": 100}},
        augs={"1007": {"name": "大力", "rarity": "silver"}, "1225": {"name": "双刀流", "rarity": "prismatic"}},
        hero_augs={"164": {}},
        core={"164": [{"items": ["3078", "3074", "6333"], "ordered": True, "games": 50000, "wins": 26500, "win": 0.53, "pick": 0.25}]},
        item_recs={
            "164": {
                "3078": {"games": 80000, "wins": 41600, "win_rate": 0.52, "low_sample": False},
                "6333": {"games": 50000, "wins": 27000, "win_rate": 0.54, "low_sample": False},
                "6672": {"games": 20000, "wins": 10400, "win_rate": 0.52, "low_sample": False},
            }
        },
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    camille_items = ("3078", "3074", "2517", "3053")
    allies_behind = [
        PlayerLiveInfo("me", "卡蜜尔", "Camille", "ORDER", True, 16, 5, 16, 20, camille_items),
    ]
    # Enemies have ONLY 1 Heartsteel (3084) + 1 Spirit Visage (3065), plus fed 21-kill Smolder & 17-kill Lee Sin
    enemies_single_tank = [
        PlayerLiveInfo("e1", "斯莫德", "Smolder", "CHAOS", False, 18, 21, 5, 12, ("3031", "6672")),
        PlayerLiveInfo("e2", "李青", "LeeSin", "CHAOS", False, 17, 17, 8, 14, ("126697", "3065", "2517")),
        PlayerLiveInfo("e3", "塔姆", "TahmKench", "CHAOS", False, 16, 4, 9, 18, ("3084",)),
        PlayerLiveInfo("e4", "艾希", "Ashe", "CHAOS", False, 16, 3, 11, 20, ("6672",)),
    ]

    # 1. Threat threshold fix: 1 Heartsteel + 1 Spirit Visage MUST NOT falsely flag high-armor or MR-stacking!
    threat, recs = optimize_next_items(
        store,
        "164",
        camille_items,
        2400.0,
        ["1007"],
        allies_behind,
        enemies_single_tank,
        limit=4,
        committed_plan=("6672", "6333"),
        locked_next_item="6672",
    )
    assert "敌方高坦/堆甲" not in threat.summary_tags
    assert "敌方堆魔抗" not in threat.summary_tags
    assert threat.enemy_heal_score > 0.0  # 2517 无穷饥渴 recognized in HEAL_ITEM_IDS!

    # 2. Defensive emergency override: uninvested locked 6672 beaten by Death's Dance (6333) when 5/16 vs 21-kill carry!
    assert recs[0].item_id == "6333"

    # 3. Genuine 2-tank / 2-MR-builder comp DOES trigger both tags
    enemies_real_tanks = [
        PlayerLiveInfo("e1", "蒙多", "DrMundo", "CHAOS", False, 16, 6, 4, 10, ("3084", "3075", "3065")),
        PlayerLiveInfo("e2", "赛恩", "Sion", "CHAOS", False, 16, 5, 5, 12, ("3084", "3075", "2504")),
    ]
    threat_real = analyze_matchup_threats(store, allies_behind, enemies_real_tanks)
    assert "敌方高坦/堆甲" in threat_real.summary_tags
    assert "敌方堆魔抗" in threat_real.summary_tags

    # 4. S16.23 shield/heal items (2526 耳语头环, 2525 原生质护带) contribute to heal & shield threat scores
    s16_enemies = [
        PlayerLiveInfo("e1", "卡尔玛", "Karma", "CHAOS", False, 12, 2, 2, 12, ("2526",)),
        PlayerLiveInfo("e2", "赛恩", "Sion", "CHAOS", False, 12, 3, 2, 10, ("2525",)),
    ]
    threat_s16 = analyze_matchup_threats(store, [], s16_enemies)
    assert threat_s16.enemy_heal_score > 0.8
    assert threat_s16.enemy_shield_score > 0.8

    # 5. Late-game 4-item no-boots rule: 4 complete items + 0 boots (or only basic 1001) -> suppress Tier-2 boots!
    assert recommend_counter_boots(store, "164", camille_items, [], threat) == ("", "", "")
    assert recommend_counter_boots(store, "164", (*camille_items, "1001"), [], threat) == ("", "", "")
    plan_4 = build_next_buy_plan(store, "164", (*camille_items, "1001"), 1200.0, recs, threat=threat)
    assert plan_4 is not None
    assert plan_4.recommended_boots_id == ""
    assert plan_4.recommended_boots_name == ""
    assert "二级鞋" not in plan_4.buy_now_hint


def test_dual_mode_first_prismatic_instant_switching(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    base_patch = tmp_path / "16.23"
    norm_dir = base_patch / "normal"
    fp_dir = base_patch / "first-prismatic"

    _write_ds(
        norm_dir,
        heroes={"164": {"name": "卡蜜尔", "title": "青钢影", "games": 100000, "wins": 48300, "win_rate": 0.483, "pick_rate": 0.02, "rank": 100}},
        augs={"1048": {"name": "珠光护手", "rarity": "prismatic"}},
        hero_augs={"164": {"1048": {"games": 1000, "wins": 500, "win_rate": 0.50, "low_sample": False}}},
        core={},
        item_recs={},
    )
    _write_ds(
        fp_dir,
        heroes={"164": {"name": "卡蜜尔", "title": "青钢影", "games": 30000, "wins": 15900, "win_rate": 0.530, "pick_rate": 0.03, "rank": 40}},
        augs={"1048": {"name": "珠光护手", "rarity": "prismatic"}},
        hero_augs={"164": {"1048": {"games": 2000, "wins": 1160, "win_rate": 0.58, "low_sample": False}}},
        core={},
        item_recs={},
    )

    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), norm_dir)

    assert store.active_match_mode == "normal"
    assert store.has_first_prismatic is True
    assert abs(store.get_hero("164").win_rate - 0.483) < 1e-6

    t0 = time.perf_counter()
    switched = store.switch_match_mode("first-prismatic")
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert switched is True
    assert elapsed_ms < 1.0
    assert store.active_match_mode == "first-prismatic"
    assert abs(store.get_hero("164").win_rate - 0.530) < 1e-6
    assert abs(store.get_hero_augment_stat("164", "1048").win_rate - 0.58) < 1e-6

    assert store.switch_match_mode("normal") is True
    assert store.active_match_mode == "normal"
    assert abs(store.get_hero("164").win_rate - 0.483) < 1e-6
    assert store.switch_match_mode("nonexistent") is False


def test_knowledge_base_full_coverage_and_new_special_augments() -> None:
    cdb = get_augment_combo_db()
    hdb = get_hero_signature_db()
    sdb = get_special_augment_db()

    # 1. Augment axes 237/237 (100%) + curated anchors
    axes = cdb.all_explicit_axes()
    assert len(axes) == 237
    for aid, ax_tuple in axes.items():
        assert len(ax_tuple) >= 1, f"Empty axes for {aid}"
        for ax in ax_tuple:
            assert ax in VALID_MECHANIC_AXES

    assert cdb.get_by_id("tickle_monster_ad_speed").required_anchor == "3058"
    assert cdb.get_by_id("dual_wield_machinegun").required_anchor == "1225"
    assert cdb.get_by_id("enchanter_butterfly_circle").required_anchor == "3163"
    assert cdb.get_by_id("minion_bees_swarm").required_anchor == "3086"

    # 2. Hero signatures 173/173 (100%), curated Mundo preserved, all item IDs canonical 4-digit
    assert len(hdb.entries) == 173
    mundo = hdb.get("36")
    assert mundo is not None and "3084" in mundo.starter_items
    camille = hdb.get("164")
    assert camille is not None and "3078" in camille.starter_items
    for entry in hdb.entries:
        assert entry.hero_id and entry.name and entry.archetype and entry.note
        assert len(entry.starter_items) >= 1
        assert len(entry.signature_items) >= 3
        for iid in (*entry.starter_items, *entry.signature_items, *entry.avoid_passive):
            assert iid.isdigit() and len(iid) == 4, f"Bad item id {iid} in hero {entry.hero_id}"

    # 3. Top uncovered high-WR-lift Patch 16.23 augments added with conservative transformative flags
    expected_new = {
        "1053": ("科学狂人", False),
        "3262": ("史莱姆时间", False),
        "1004": ("回归基本功", True),
        "3078": ("鼓声暴起 低音轰鸣", False),
        "1333": ("逃跑计划", False),
        "1375": ("吞噬灵魂", False),
        "1204": ("叠角龙", True),
        "1195": ("巨人杀手", False),
    }
    for aid, (exp_name, exp_trans) in expected_new.items():
        sp = sdb.get_by_id(aid)
        assert sp is not None, f"Missing special augment {aid} ({exp_name})"
        assert sp.name == exp_name
        assert sp.transformative is exp_trans
        for iid in (*sp.force_items, *sp.core_items):
            assert iid.isdigit() and len(iid) == 4

    # 4. Cross-check against real local 16.23 dataset when available
    ds_root = _local_1623_root()
    if ds_root is not None:
        active_augs = json.loads((ds_root / "normal" / "augments.json").read_text(encoding="utf-8"))
        active_heroes = json.loads((ds_root / "normal" / "heroes.json").read_text(encoding="utf-8"))
        assert set(active_augs.keys()).issubset(set(axes.keys()))
        assert set(active_heroes.keys()).issubset({e.hero_id for e in hdb.entries})
