import json
from pathlib import Path

from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.build_synergy import is_transformative_augment
from hex_helper.scoring.matchup_optimizer import (
    build_next_buy_plan,
    compute_item_buy_now,
    optimize_next_items,
    reconcile_match_build_plan,
)


def _make_matchup_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3031": ItemStaticInfo("3031", "无尽之刃", 3600, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3085": ItemStaticInfo("3085", "卢安娜的飓风", 2650, ("AttackSpeed", "CriticalStrike"), {"PercentAttackSpeedMod": 0.4, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3033": ItemStaticInfo("3033", "凡性的提醒", 3000, ("Damage", "CriticalStrike", "ArmorPenetration"), {"FlatPhysicalDamageMod": 35, "FlatCritChanceMod": 0.25}, "重伤", "complete_equipment", has_grievous=True),
        "3036": ItemStaticInfo("3036", "多米尼克领主的致意", 3300, ("Damage", "CriticalStrike", "ArmorPenetration"), {"FlatPhysicalDamageMod": 35, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3153": ItemStaticInfo("3153", "破败王者之刃", 3200, ("Damage", "AttackSpeed", "LifeSteal", "OnHit"), {"FlatPhysicalDamageMod": 40, "PercentAttackSpeedMod": 0.25, "PercentLifeStealMod": 0.1}, "", "complete_equipment"),
        "3156": ItemStaticInfo("3156", "玛莫提乌斯之噬", 3100, ("SpellBlock", "Damage"), {"FlatSpellBlockMod": 40, "FlatPhysicalDamageMod": 60}, "", "complete_equipment"),
        "3091": ItemStaticInfo("3091", "智慧末刃", 2800, ("SpellBlock", "AttackSpeed"), {"FlatSpellBlockMod": 45, "PercentAttackSpeedMod": 0.5}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Health", "Armor"), {"FlatHPPoolMod": 150, "FlatArmorMod": 75}, "重伤", "complete_equipment", has_grievous=True),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100}, "", "complete_equipment"),
        "3123": ItemStaticInfo("3123", "死刑宣告", 800, ("Damage",), {"FlatPhysicalDamageMod": 15}, "重伤", "other", has_grievous=True),
    }
    ds_dir = tmp_path / "ds"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({"222": {"name": "金克丝", "title": "暴走萝莉", "games": 458049, "wins": 220046, "win_rate": 0.4804, "pick_rate": 0.04, "rank": 123}}),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(json.dumps({"3081": {"name": "命运之武器", "rarity": "gold"}}), encoding="utf-8")
    (ds_dir / "hero_augments.json").write_text(json.dumps({"222": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({"222": [{"items": ["3085", "3031", "3036"], "ordered": True, "games": 50000, "wins": 25500, "win": 0.51, "pick": 0.12}]}),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "222": {
                "3031": {"games": 20000, "wins": 10100, "win_rate": 0.505, "low_sample": False},
                "3085": {"games": 25000, "wins": 12600, "win_rate": 0.504, "low_sample": False},
                "3033": {"games": 15000, "wins": 7650, "win_rate": 0.510, "low_sample": False},
                "3036": {"games": 18000, "wins": 9360, "win_rate": 0.520, "low_sample": False},
                "3153": {"games": 12000, "wins": 6120, "win_rate": 0.510, "low_sample": False},
                "3156": {"games": 5000, "wins": 2550, "win_rate": 0.510, "low_sample": False},
                "3091": {"games": 4000, "wins": 2040, "win_rate": 0.510, "low_sample": False},
            }
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    return store


def test_grievous_wounds_boosted_when_enemy_heals_and_allies_lack_gw(tmp_path: Path):
    store = _make_matchup_store(tmp_path)
    allies = [
        PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 11, 4, 2, 5, ("3085",)),
        PlayerLiveInfo("a2", "拉克丝", "Lux", "ORDER", False, 11, 3, 3, 6, ("6655",)),
    ]
    enemies = [
        PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 12, 8, 2, 4, ("3075", "3084")),
        PlayerLiveInfo("e2", "索拉卡", "Soraka", "CHAOS", False, 11, 1, 2, 12, ()),
        PlayerLiveInfo("e3", "蒙多医生", "DrMundo", "CHAOS", False, 12, 5, 3, 5, ("3084", "3075")),
    ]
    threat, recs = optimize_next_items(store, "222", ["3085"], my_gold=3100.0, selected_augments=[], allies=allies, enemies=enemies)
    assert threat.enemy_heal_score >= 2.0
    assert threat.ally_has_grievous is False
    # Mortal Reminder (3033) should jump to #1 because enemy heals + stacks armor and allies have no GW
    assert recs[0].item_id == "3033"
    assert recs[0].affordable is True
    assert any("重伤" in r for r in recs[0].reasons)

    # Now give an ally Thornmail (3075, has_grievous) -> LDR (3036) should outrank Mortal Reminder!
    allies_with_gw = [
        allies[0],
        PlayerLiveInfo("a2", "泰坦", "Nautilus", "ORDER", False, 11, 2, 4, 10, ("3075",)),
    ]
    threat2, recs2 = optimize_next_items(store, "222", ["3085"], my_gold=3400.0, selected_augments=[], allies=allies_with_gw, enemies=enemies)
    assert threat2.ally_has_grievous is True
    ids2 = [r.item_id for r in recs2]
    assert "3036" in ids2
    assert ids2.index("3036") < (ids2.index("3033") if "3033" in ids2 else 99)


def test_heavy_ap_enemy_boosts_mr_options_and_gold_gap(tmp_path: Path):
    store = _make_matchup_store(tmp_path)
    allies = [PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 11, 4, 3, 4, ("3085",))]
    enemies = [
        PlayerLiveInfo("e1", "辛德拉", "Syndra", "CHAOS", False, 13, 10, 1, 4, ("6655", "3089")),
        PlayerLiveInfo("e2", "泽拉斯", "Xerath", "CHAOS", False, 12, 6, 2, 6, ("6655", "3089")),
        PlayerLiveInfo("e3", "维克托", "Viktor", "CHAOS", False, 12, 4, 2, 5, ("6655",)),
    ]
    threat, recs = optimize_next_items(store, "222", ["3085"], my_gold=2600.0, selected_augments=[], allies=allies, enemies=enemies)
    assert threat.enemy_ap_ratio >= 0.85
    assert threat.top_threat_name == "辛德拉"
    top_ids = [r.item_id for r in recs[:3]]
    assert "3156" in top_ids or "3091" in top_ids
    maw = next(r for r in recs if r.item_id == "3156")
    assert maw.affordable is False
    assert maw.gold_needed == 500


def test_ap_hero_filters_off_profile_ad_items_and_computes_component_buy_now(tmp_path: Path):
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "1001": ItemStaticInfo("1001", "鞋子", 300, ("Boots",), {}, "", "boots"),
        "1029": ItemStaticInfo("1029", "布甲", 300, ("Armor",), {"FlatArmorMod": 15}, "", "other"),
        "1052": ItemStaticInfo("1052", "增幅典籍", 400, ("SpellDamage",), {"FlatMagicDamageMod": 20}, "", "other"),
        "1058": ItemStaticInfo("1058", "无用大棒", 1200, ("SpellDamage",), {"FlatMagicDamageMod": 65}, "", "other"),
        "2420": ItemStaticInfo("2420", "探索者的护臂", 1600, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 40, "FlatArmorMod": 25}, "", "other", components=("1052", "1052", "1029")),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots", "MagicPenetration"), {}, "", "boots", components=("1001",)),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105, "FlatArmorMod": 50}, "", "complete_equipment", components=("1058", "2420")),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment", components=("1058", "1058")),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110}, "", "complete_equipment"),
        "4646": ItemStaticInfo("4646", "风暴狂涌", 2800, ("SpellDamage",), {"FlatMagicDamageMod": 90}, "", "complete_equipment"),
        "6696": ItemStaticInfo("6696", "公理圆弧", 3000, ("Damage", "ArmorPenetration"), {"FlatPhysicalDamageMod": 55}, "", "complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3600, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage", "MagicPenetration"), {"FlatMagicDamageMod": 95}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_eve"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({"28": {"name": "伊芙琳", "title": "痛苦之拥", "games": 50000, "wins": 23300, "win_rate": 0.466, "pick_rate": 0.02, "rank": 110}}),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(
        json.dumps({"1238": {"name": "质变：棱彩阶", "rarity": "prismatic"}, "2001": {"name": "珠光护手", "rarity": "prismatic"}}),
        encoding="utf-8",
    )
    (ds_dir / "hero_augments.json").write_text(json.dumps({"28": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({"28": [{"items": ["4646", "4645", "3089"], "ordered": True, "games": 10000, "wins": 5000, "win": 0.50, "pick": 0.15}]}),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "28": {
                "6696": {"games": 150, "wins": 90, "win_rate": 0.60, "low_sample": False},
                "3031": {"games": 180, "wins": 105, "win_rate": 0.583, "low_sample": False},
                "3157": {"games": 8000, "wins": 4000, "win_rate": 0.50, "low_sample": False},
                "3089": {"games": 12000, "wins": 6120, "win_rate": 0.51, "low_sample": False},
                "3135": {"games": 6000, "wins": 3000, "win_rate": 0.50, "low_sample": False},
            }
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "first-prismatic", "首次必棱彩", "patches/16.23/first-prismatic", "", False), ds_dir)

    allies = [PlayerLiveInfo("me", "伊芙琳", "Evelynn", "ORDER", True, 11, 5, 3, 4, ("4645", "4646", "1001"))]
    enemies = [
        PlayerLiveInfo("e1", "李青", "LeeSin", "CHAOS", False, 12, 8, 2, 5, ("6696",)),
        PlayerLiveInfo("e2", "永恩", "Yone", "CHAOS", False, 12, 7, 3, 4, ("3031",)),
        PlayerLiveInfo("e3", "纳亚菲利", "Naafiri", "CHAOS", False, 11, 5, 3, 3, ("6696",)),
    ]

    threat, recs = optimize_next_items(store, "28", ("4645", "4646", "1001"), 1174.0, ["1238"], allies, enemies)
    rec_ids = [r.item_id for r in recs]
    # Pure AD items must NEVER pollute AP Evelynn's recommendations
    assert "6696" not in rec_ids
    assert "3031" not in rec_ids
    # Zhonya's Hourglass (3157) should be #1 against heavy AD enemies
    assert rec_ids[0] == "3157"

    # With Jeweled Gauntlet (珠光护手), Infinity Edge (3031) becomes legal
    _, recs_crit = optimize_next_items(store, "28", ("4645", "4646", "1001"), 1174.0, ["2001"], allies, enemies)
    assert "3031" in [r.item_id for r in recs_crit]
    assert "6696" not in [r.item_id for r in recs_crit]

    # NextBuyPlan & component credit math with owned Amplifying Tome (1052, 400G) + Boots (1001) + 1174G
    plan = build_next_buy_plan(store, "28", ("4645", "4646", "1001", "1052"), 1174.0, recs)
    assert plan is not None
    assert plan.target.item_id == "3157"
    assert plan.effective_cost == 2850  # 3250 - 400 credit from Amplifying Tome
    assert plan.affordable_now is False
    assert plan.gold_gap == 1676
    assert "增幅典籍" in plan.buy_now_hint and "布甲" in plan.buy_now_hint
    assert "升二级鞋" in plan.buy_now_hint
    assert plan.remaining_build_path[0] == "中娅沙漏"

    # Direct boot upgrade calculation: owning Boots (1001) with 1315G targeting Sorcerer's Shoes (3020, 1100G)
    eff_b, aff_b, gap_b, hint_b = compute_item_buy_now(store, "3020", ("1001",), 1315.0)
    assert eff_b == 800
    assert aff_b is True
    assert gap_b == 0
    assert "法师之靴" in hint_b and "800G" in hint_b


def test_no_mana_no_problem_boosts_archangel_staff_on_ap_hero(tmp_path: Path):
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3070": ItemStaticInfo("3070", "女神之泪", 400, ("Mana",), {"FlatMPPoolMod": 240}, "", "other"),
        "3003": ItemStaticInfo("3003", "大天使之杖", 2900, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 70, "FlatMPPoolMod": 600}, "", "complete_equipment", components=("3070",)),
        "3115": ItemStaticInfo("3115", "纳什之牙", 3000, ("SpellDamage", "AttackSpeed"), {"FlatMagicDamageMod": 80, "PercentAttackSpeedMod": 0.5}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3500, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_kayle"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({"10": {"name": "凯尔", "title": "正义天使", "games": 80000, "wins": 44300, "win_rate": 0.554, "pick_rate": 0.03, "rank": 5}}),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(
        json.dumps({"3059": {"name": "没法力，没问题", "rarity": "silver"}}),
        encoding="utf-8",
    )
    (ds_dir / "hero_augments.json").write_text(json.dumps({"10": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({"10": [{"items": ["3115", "3089"], "ordered": True, "games": 20000, "wins": 11500, "win": 0.575, "pick": 0.2}]}),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "10": {
                "3115": {"games": 15000, "wins": 8400, "win_rate": 0.56, "low_sample": False},
                "3089": {"games": 12000, "wins": 6720, "win_rate": 0.56, "low_sample": False},
            }
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    allies = [PlayerLiveInfo("me", "凯尔", "Kayle", "ORDER", True, 6, 1, 1, 2, ("3070", "3115"))]
    enemies = [PlayerLiveInfo("e1", "艾克", "Ekko", "CHAOS", False, 6, 2, 1, 2, ())]
    _, recs = optimize_next_items(store, "10", ("3070", "3115"), 1500.0, ["3059"], allies, enemies)
    ids = [r.item_id for r in recs]
    assert "3003" in ids[:2]
    arch = next(r for r in recs if r.item_id == "3003")
    assert any(("没法力" in reason or "蓝转" in reason) for reason in arch.reasons)


def test_brand_m0_filters_pure_tank_and_build_plan_lock_prevents_flip_flop(tmp_path: Path):
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3802": ItemStaticInfo("3802", "遗失的章节", 1200, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 40, "FlatMPPoolMod": 300}, "", "other"),
        "2503": ItemStaticInfo("2503", "黯炎火炬", 2800, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 80, "FlatMPPoolMod": 600}, "", "complete_equipment", components=("3802",)),
        "6653": ItemStaticInfo("6653", "兰德里的折磨", 3000, ("SpellDamage", "Health"), {"FlatMagicDamageMod": 60, "FlatHPPoolMod": 300}, "", "complete_equipment"),
        "8010": ItemStaticInfo("8010", "放血者的诅咒", 2900, ("SpellDamage", "Health"), {"FlatMagicDamageMod": 65, "FlatHPPoolMod": 400}, "", "complete_equipment"),
        "3116": ItemStaticInfo("3116", "瑞莱的冰晶节杖", 2600, ("SpellDamage", "Health"), {"FlatMagicDamageMod": 65, "FlatHPPoolMod": 400}, "", "complete_equipment"),
        "3102": ItemStaticInfo("3102", "女妖面纱", 3000, ("SpellDamage", "SpellBlock"), {"FlatMagicDamageMod": 105, "FlatSpellBlockMod": 40}, "", "complete_equipment"),
        "2504": ItemStaticInfo("2504", "败魔", 2900, ("SpellBlock", "Health"), {"FlatSpellBlockMod": 80, "FlatHPPoolMod": 400}, "", "complete_equipment"),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, ("SpellBlock", "Health"), {"FlatSpellBlockMod": 50, "FlatHPPoolMod": 400}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_brand"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({"63": {"name": "布兰德", "title": "复仇焰魂", "games": 50000, "wins": 25000, "win_rate": 0.50, "pick_rate": 0.03, "rank": 40}}),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(
        json.dumps({
            "3163": {"name": "迷人蝴蝶", "rarity": "gold"},
            "3049": {"name": "装甲冲击", "rarity": "gold"},
            "1105": {"name": "家园卫士", "rarity": "silver"},
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_augments.json").write_text(json.dumps({"63": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({"63": [{"items": ["2503", "6653", "8010"], "ordered": True, "games": 110000, "wins": 55600, "win": 0.506, "pick": 0.25}]}),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "63": {
                "8010": {"games": 67000, "wins": 34237, "win_rate": 0.511, "low_sample": False},
                "2503": {"games": 360000, "wins": 181800, "win_rate": 0.505, "low_sample": False},
                "6653": {"games": 395000, "wins": 198685, "win_rate": 0.503, "low_sample": False},
                "2504": {"games": 2200, "wins": 1095, "win_rate": 0.498, "low_sample": False},
                "3065": {"games": 3900, "wins": 1936, "win_rate": 0.496, "low_sample": False},
            }
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    enemies_m0 = [
        PlayerLiveInfo("e1", "德邦总管", "XinZhao", "CHAOS", False, 3, 0, 0, 0, ()),
        PlayerLiveInfo("e2", "诡术妖姬", "Leblanc", "CHAOS", False, 3, 0, 0, 0, ()),
    ]
    threat, recs = optimize_next_items(store, "63", (), 1400.0, [], [], enemies_m0, limit=4)
    rec_ids = [r.item_id for r in recs]
    # Pure tank MR items must NOT pollute Brand, and 0/0/0 enemy is NOT a fed threat
    assert threat.top_threat_name == ""
    assert "2504" not in rec_ids and "3065" not in rec_ids
    # Slot-ordered 1st core (2503 黯炎火炬) must beat 3rd-slot survivorship-bias 8010 at 0 items
    assert rec_ids[0] == "2503"

    # Transformative augment detection
    assert is_transformative_augment(store, "3163") is True  # 迷人蝴蝶
    assert is_transformative_augment(store, "3049") is True  # 装甲冲击
    assert is_transformative_augment(store, "1105") is False  # 家园卫士

    # Plan continuity: once 8010 is locked, routine death proposals must NOT flip-flop away from 8010
    lock1, plan1 = reconcile_match_build_plan(store, (), [], "", ["8010", "2503", "6653"], allow_overhaul=True)
    assert lock1 == "8010"
    lock2, plan2 = reconcile_match_build_plan(store, (), plan1, lock1, ["3116", "3102", "6653"], allow_overhaul=False)
    assert lock2 == "8010" and plan2[0] == "8010"

    # Once player completes 8010, lock smoothly advances to 2503
    lock3, plan3 = reconcile_match_build_plan(store, ("8010",), plan2, lock2, ["3116", "2503"], allow_overhaul=False)
    assert lock3 == "2503" and "8010" not in plan3

    # If player buys >=800G component (遗失的章节 1200G -> 2503) while another item was locked, honor shop choice
    lock_shop, _ = reconcile_match_build_plan(store, ("3802",), ["6653", "3116"], "6653", ["2503", "6653"], allow_overhaul=False)
    assert lock_shop == "2503"

    # Transformative augment overhaul (allow_overhaul=True) IS permitted to pivot remaining slots
    lock_ov, plan_ov = reconcile_match_build_plan(store, ("8010",), plan3, lock3, ["3116", "3102"], allow_overhaul=True)
    assert lock_ov == "3116" and plan_ov[:2] == ["3116", "3102"]
