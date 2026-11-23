"""Tests for Patch 26.23 Special Augment Build Database & optimizer integration."""

from __future__ import annotations

import json
from pathlib import Path

from hex_helper.data.hero_signatures import get_hero_signature_db
from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.special_builds import BUNDLED_DB_PATH, SpecialAugmentDatabase, get_special_augment_db
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.build_synergy import item_is_owned, normalize_item_id
from hex_helper.scoring.matchup_optimizer import optimize_next_items


def test_special_db_has_all_core_archetypes_and_nickname_search():
    db = get_special_augment_db()
    assert len(db.entries) >= 40

    # Nickname / colloquial lookups must hit exact Patch 26.23 augments
    assert db.get_by_name("小蝴蝶").augment_id == "3163"  # 迷人蝴蝶
    assert db.get_by_name("小蜜蜂").augment_id == "3086"  # 小蜜蜂！！！
    assert db.get_by_name("大蜜蜂").augment_id == "3048"  # 弹跳蜜蜂
    assert db.get_by_name("死亡之环").augment_id == "1015"
    assert db.get_by_name("装甲冲击").augment_id == "3049"
    assert db.get_by_name("没法力").augment_id == "3059"
    assert db.get_by_name("嗡嗡怪兽").augment_id == "3058"
    assert db.get_by_name("花剑").augment_id == "1379"  # 升级：花晓之剑

    # Every entry must have non-empty mechanic and valid item IDs
    for e in db.entries:
        assert e.augment_id and e.name and e.mechanic
        for iid in (*e.force_items, *e.core_items):
            assert iid.isdigit() and len(iid) == 4


def test_special_db_user_override_merges_cleanly(tmp_path: Path):
    custom = tmp_path / "special_augment_builds.json"
    custom.write_text(
        json.dumps(
            {
                "patch": "26.24",
                "archetypes": [
                    {
                        "id": "9999",
                        "name": "测试自定义流派",
                        "aliases": ["自建流派"],
                        "rarity": "prismatic",
                        "archetype": "测试流",
                        "mechanic": "测试自定义覆盖",
                        "transformative": True,
                        "allow_off_profile": True,
                        "boost": 5.0,
                        "core_items": ["3089"],
                        "force_items": ["3089"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db = SpecialAugmentDatabase(bundled_path=BUNDLED_DB_PATH, user_override_path=custom)
    hit = db.get_by_name("自建流派")
    assert hit is not None and hit.augment_id == "9999" and hit.force_items == ("3089",)


def _make_special_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "6617": ItemStaticInfo("6617", "月石再生器", 2200, ("Health", "SpellDamage", "ManaRegen"), {"FlatMagicDamageMod": 25, "FlatHPPoolMod": 200}, "", "complete_equipment"),
        "3504": ItemStaticInfo("3504", "炽热香炉", 2200, ("AttackSpeed", "SpellDamage", "ManaRegen"), {"FlatMagicDamageMod": 45}, "", "complete_equipment"),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, ("Health", "SpellBlock", "HealthRegen"), {"FlatHPPoolMod": 400, "FlatSpellBlockMod": 50}, "", "complete_equipment"),
        "3083": ItemStaticInfo("3083", "狂徒铠甲", 3100, ("Health", "HealthRegen"), {"FlatHPPoolMod": 1000}, "", "complete_equipment"),
        "3110": ItemStaticInfo("3110", "冰霜之心", 2500, ("Armor", "Mana"), {"FlatArmorMod": 75, "FlatMPPoolMod": 400}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Health", "Armor"), {"FlatArmorMod": 75, "FlatHPPoolMod": 150}, "", "complete_equipment"),
        "3115": ItemStaticInfo("3115", "纳什之牙", 2900, ("AttackSpeed", "SpellDamage", "OnHit"), {"FlatMagicDamageMod": 80, "PercentAttackSpeedMod": 0.5}, "", "complete_equipment"),
        "6672": ItemStaticInfo("6672", "海妖杀手", 3000, ("Damage", "AttackSpeed", "OnHit"), {"FlatPhysicalDamageMod": 45, "PercentAttackSpeedMod": 0.4}, "", "complete_equipment"),
        "3074": ItemStaticInfo("3074", "贪欲九头蛇", 3300, ("Damage", "LifeSteal", "OnHit"), {"FlatPhysicalDamageMod": 65}, "", "complete_equipment"),
        "3072": ItemStaticInfo("3072", "饮血剑", 3400, ("Damage", "LifeSteal"), {"FlatPhysicalDamageMod": 80}, "", "complete_equipment"),
        "3085": ItemStaticInfo("3085", "卢安娜的飓风", 2650, ("AttackSpeed", "CriticalStrike", "OnHit"), {"PercentAttackSpeedMod": 0.4, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "4011": ItemStaticInfo("4011", "花晓之剑", 2500, ("Health", "SpellDamage", "OnHit"), {"FlatMagicDamageMod": 45, "FlatHPPoolMod": 200}, "", "complete_equipment"),
        "6655": ItemStaticInfo("6655", "卢登的回声", 2750, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 100, "FlatMPPoolMod": 600}, "", "complete_equipment"),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_special"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({
            "99": {"name": "拉克丝", "title": "光辉女郎", "games": 50000, "wins": 25000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 50},
            "222": {"name": "金克丝", "title": "暴走萝莉", "games": 50000, "wins": 25000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 50},
        }),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(
        json.dumps({
            "3163": {"name": "迷人蝴蝶", "rarity": "gold"},
            "3086": {"name": "小蜜蜂！！！", "rarity": "gold"},
            "3058": {"name": "嗡嗡怪兽", "rarity": "gold"},
            "1015": {"name": "死亡之环", "rarity": "prismatic"},
            "3049": {"name": "装甲冲击", "rarity": "gold"},
            "1379": {"name": "升级：花晓之剑", "rarity": "prismatic"},
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_augments.json").write_text(json.dumps({"99": {}, "222": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({
            "99": [{"items": ["6655", "4645"], "ordered": True, "games": 10000, "wins": 5000, "win": 0.50, "pick": 0.2}],
            "222": [{"items": ["3085", "6672"], "ordered": True, "games": 10000, "wins": 5000, "win": 0.50, "pick": 0.2}],
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "99": {"6655": {"games": 10000, "wins": 5000, "win_rate": 0.50, "low_sample": False}},
            "222": {"3085": {"games": 10000, "wins": 5000, "win_rate": 0.50, "low_sample": False}},
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    return store


def test_special_augments_drive_exact_archetype_builds(tmp_path: Path):
    store = _make_special_store(tmp_path)
    enemies = [PlayerLiveInfo("e1", "艾克", "Ekko", "CHAOS", False, 6, 1, 1, 1, ())]

    # 1. 迷人蝴蝶 (3163) on Lux -> Moonstone Renewer (6617) jumps to #1!
    _, recs_bf = optimize_next_items(store, "99", (), 2200.0, ["3163"], [], enemies, limit=4)
    assert recs_bf[0].item_id == "6617"

    # 2. 死亡之环 (1015) on Lux -> Spirit Visage (3065) bypasses pure-tank filter and jumps to #1!
    _, recs_cod = optimize_next_items(store, "99", (), 2700.0, ["1015"], [], enemies, limit=4)
    assert recs_cod[0].item_id == "3065"
    assert "3083" in [r.item_id for r in recs_cod]

    # 3. 装甲冲击 (3049) -> Armor items (3110 / 3075) jump into top recommendations!
    _, recs_arm = optimize_next_items(store, "99", (), 2500.0, ["3049"], [], enemies, limit=4)
    assert any(r.item_id in ("3110", "3075") for r in recs_arm[:2])

    # 4. 嗡嗡怪兽 (3058) on Jinx -> boosts high-AD Ravenous/BT and penalizes zero-AD Hurricane (3085)!
    _, recs_buzz = optimize_next_items(store, "222", (), 3400.0, ["3058"], [], enemies, limit=4)
    buzz_ids = [r.item_id for r in recs_buzz]
    assert recs_buzz[0].item_id in ("3074", "3072")
    assert buzz_ids.index(recs_buzz[0].item_id) < (buzz_ids.index("3085") if "3085" in buzz_ids else 99)

    # 5. 小蜜蜂！！！ (3086) on Lux -> Nashor's Tooth (3115) enters top picks!
    _, recs_bee = optimize_next_items(store, "99", (), 2900.0, ["3086"], [], enemies, limit=4)
    assert "3115" in [r.item_id for r in recs_bee[:2]]

    # 6. 升级：花晓之剑 (1379) -> Sword of Blossoming Dawn (4011) locks #1!
    _, recs_sword = optimize_next_items(store, "99", (), 2500.0, ["1379"], [], enemies, limit=4)
    assert recs_sword[0].item_id == "4011"


def test_mundo_heartsteel_beats_niche_survivorship_outliers_and_77_prefix_normalization(tmp_path: Path):
    # Verify 77xxxx Mayhem variant IDs normalize cleanly to 4-digit base IDs (fixes 水银之靴 773111 lock bug)
    assert normalize_item_id("773111") == "3111"
    assert item_is_owned("773111", ("3111",)) is True

    # Verify Hero Signature DB loaded with 30 iconic champions
    hdb = get_hero_signature_db()
    assert len(hdb.entries) >= 28
    mundo_sig = hdb.get("36")
    assert mundo_sig is not None and "3084" in mundo_sig.starter_items

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900}, "", "complete_equipment"),
        "3083": ItemStaticInfo("3083", "狂徒铠甲", 3100, ("Health",), {"FlatHPPoolMod": 1000}, "", "complete_equipment"),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, ("Health", "SpellBlock"), {"FlatHPPoolMod": 400, "FlatSpellBlockMod": 50}, "", "complete_equipment"),
        "2501": ItemStaticInfo("2501", "霸王血铠", 3300, ("Health", "Damage"), {"FlatHPPoolMod": 550, "FlatPhysicalDamageMod": 30}, "", "complete_equipment"),
        "2525": ItemStaticInfo("2525", "原生质护带", 2600, ("Health",), {"FlatHPPoolMod": 500}, "", "complete_equipment"),
        "3109": ItemStaticInfo("3109", "骑士之誓", 2300, ("Health", "Armor"), {"FlatHPPoolMod": 200, "FlatArmorMod": 40}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_mundo"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({"36": {"name": "蒙多医生", "title": "祖安狂人", "games": 300000, "wins": 153810, "win_rate": 0.5127, "pick_rate": 0.04, "rank": 25}}),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(json.dumps({}), encoding="utf-8")
    (ds_dir / "hero_augments.json").write_text(json.dumps({"36": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({
            "36": [
                {"items": ["3084", "3083", "3065"], "ordered": True, "games": 409127, "wins": 220438, "win": 0.5388, "pick": 0.205},
                {"items": ["3084", "3083", "2501"], "ordered": True, "games": 203091, "wins": 106115, "win": 0.5225, "pick": 0.102},
            ]
        }),
        encoding="utf-8",
    )
    # Reproduce exact real 26.23 Mundo skew: 232k-game Heartsteel (50.53%) vs 2.4k-game 2525 (58.38%) & 743-game 3109 (57.47%)
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "36": {
                "3084": {"games": 232247, "wins": 117354, "win_rate": 0.5053, "low_sample": False},
                "3083": {"games": 205749, "wins": 105919, "win_rate": 0.5148, "low_sample": False},
                "3065": {"games": 126096, "wins": 65317, "win_rate": 0.5180, "low_sample": False},
                "2501": {"games": 73423, "wins": 37908, "win_rate": 0.5163, "low_sample": False},
                "2525": {"games": 2400, "wins": 1401, "win_rate": 0.5838, "low_sample": False},
                "3109": {"games": 743, "wins": 427, "win_rate": 0.5747, "low_sample": False},
            }
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    enemies = [
        PlayerLiveInfo("e1", "诺克萨斯之手", "Darius", "CHAOS", False, 3, 0, 0, 0, ()),
        PlayerLiveInfo("e2", "复仇焰魂", "Brand", "CHAOS", False, 3, 0, 0, 0, ()),
        PlayerLiveInfo("e3", "维克托", "Viktor", "CHAOS", False, 3, 0, 0, 0, ()),
    ]
    _, recs = optimize_next_items(store, "36", (), 1400.0, [], [], enemies, limit=4)
    ids = [r.item_id for r in recs]
    assert ids[0] == "3084"  # 心之钢 MUST be #1!
    assert "3083" in ids[:3] and "3065" in ids[:3]
    assert "3109" not in ids[:3] and "2525" not in ids[:2]


def test_six_digit_item_recs_and_core_build_off_profile_exemption(tmp_path: Path):
    """Verify 12xxxx Mayhem item_recs keys aggregate properly (Hubris 126697) and top-3 core builds bypass off-profile filter (Yasuo 3430)."""
    static = StaticAssetsManager(tmp_path / "cache_sys")
    static.items = {
        "6697": ItemStaticInfo("6697", "狂妄", 2950, ("Damage",), {"FlatPhysicalDamageMod": 60}, "", "complete_equipment"),
        "126697": ItemStaticInfo("126697", "狂妄", 2950, ("Damage",), {"FlatPhysicalDamageMod": 60}, "", "complete_equipment"),
        "6333": ItemStaticInfo("6333", "死亡之舞", 3300, ("Damage", "Armor"), {"FlatPhysicalDamageMod": 60, "FlatArmorMod": 50}, "", "complete_equipment"),
        "3032": ItemStaticInfo("3032", "育恩塔尔荒野箭", 3000, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 55, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3430": ItemStaticInfo("3430", "毁坏仪式", 3000, ("SpellDamage", "CriticalStrike"), {"FlatMagicDamageMod": 55, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "123430": ItemStaticInfo("123430", "毁坏仪式", 3000, ("SpellDamage", "CriticalStrike"), {"FlatMagicDamageMod": 55, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_sys"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({
            "238": {"name": "劫", "title": "影流之主", "games": 200000, "wins": 102000, "win_rate": 0.51, "pick_rate": 0.04, "rank": 50},
            "157": {"name": "亚索", "title": "疾风剑豪", "games": 250000, "wins": 130000, "win_rate": 0.52, "pick_rate": 0.05, "rank": 30},
        }),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(json.dumps({}), encoding="utf-8")
    (ds_dir / "hero_augments.json").write_text(json.dumps({"238": {}, "157": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({
            "238": [{"items": ["126697", "6333", "3032"], "ordered": True, "games": 95000, "wins": 53200, "win": 0.56, "pick": 0.35}],
            "157": [{"items": ["3032", "123430", "6333"], "ordered": True, "games": 110000, "wins": 74800, "win": 0.68, "pick": 0.40}],
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "238": {
                "126697": {"games": 140000, "wins": 78400, "win_rate": 0.56, "low_sample": False},
                "6333": {"games": 80000, "wins": 44000, "win_rate": 0.55, "low_sample": False},
            },
            "157": {
                "3032": {"games": 120000, "wins": 72000, "win_rate": 0.60, "low_sample": False},
                "123430": {"games": 115000, "wins": 74750, "win_rate": 0.65, "low_sample": False},
                "6333": {"games": 90000, "wins": 52200, "win_rate": 0.58, "low_sample": False},
            },
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    enemies = [PlayerLiveInfo("e1", "拉克丝", "Lux", "CHAOS", False, 3, 0, 0, 0, ())]
    # 1. Zed with 126697 in item_recs MUST preserve 140k games and rank Hubris (6697) #1!
    _, zed_recs = optimize_next_items(store, "238", (), 1400.0, [], [], enemies, limit=4)
    assert zed_recs[0].item_id == "6697"
    assert zed_recs[0].games == 140000

    # 2. Yasuo (AD profile) with 123430 (AP+Crit Rite of Ruin) in Top-3 core builds MUST NOT filter 3430!
    _, yas_recs = optimize_next_items(store, "157", ("3032",), 1600.0, [], [], enemies, limit=4)
    assert "3430" in [r.item_id for r in yas_recs[:2]]
    rite = next(r for r in yas_recs if r.item_id == "3430")
    assert rite.games == 115000

