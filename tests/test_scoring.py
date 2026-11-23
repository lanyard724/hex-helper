import json
from pathlib import Path

from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.build_synergy import evaluate_augment_synergies, item_is_owned
from hex_helper.scoring.destiny import rank_destiny_items
from hex_helper.scoring.tier import score_hero_augment, score_hero_tier


def _build_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3153": ItemStaticInfo("3153", "破败王者之刃", 3200, ("Damage", "AttackSpeed", "OnHit"), {"FlatPhysicalDamageMod": 40, "PercentAttackSpeedMod": 0.25}, "", "complete_equipment"),
        "3036": ItemStaticInfo("3036", "多米尼克领主的致意", 3300, ("Damage", "CriticalStrike", "ArmorPenetration"), {"FlatPhysicalDamageMod": 35, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("Armor", "SpellDamage", "Active"), {"FlatMagicDamageMod": 105, "FlatArmorMod": 50}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds"
    ds_dir.mkdir()
    (ds_dir / "heroes.json").write_text(
        json.dumps({"222": {"name": "金克丝", "title": "暴走萝莉", "games": 458049, "wins": 220046, "win_rate": 0.4804, "pick_rate": 0.04, "rank": 123}}),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(
        json.dumps({"3081": {"name": "命运之武器", "rarity": "gold"}, "1002": {"name": "尖端发明家", "rarity": "gold"}}),
        encoding="utf-8",
    )
    (ds_dir / "hero_augments.json").write_text(json.dumps({"222": {}}), encoding="utf-8")
    (ds_dir / "hero_augment_items.json").write_text(
        json.dumps({
            "222": {
                "3081": {
                    "baseline": {"games": 100000, "wins": 47970},
                    "items": [
                        {"item": "3036", "name": "多米尼克领主的致意", "games": 73782, "wins": 38337, "low_sample": False},
                        {"item": "3153", "name": "破败王者之刃", "games": 59511, "wins": 32642, "low_sample": False},
                    ],
                }
            }
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    return store


def test_bayesian_tier_suppresses_tiny_sample():
    # 15 wins / 20 games = 75% raw, but tiny sample should be smoothed down and marked '?'
    tiny = score_hero_augment("999", "测试", "gold", hero_win_rate=0.50, wins=15, games=20)
    assert tiny.tier == "?"
    assert tiny.low_sample is True
    assert tiny.smoothed_win_rate < 0.56

    # Real 26.23 Jinx Destiny's Weapon: 24939 / 46967 vs prior 0.4804 -> delta +5.05% -> S+
    destiny = score_hero_augment("3081", "命运之武器", "gold", hero_win_rate=0.4804, wins=24939, games=46967)
    assert destiny.tier == "S+"
    assert destiny.delta > 0.049
    assert destiny.low_sample is False

    # Real 26.23 Jinx Buzzing Monster: 3799 / 9645 vs prior 0.4804 -> D
    bad = score_hero_augment("3058", "嗡嗡怪兽", "gold", hero_win_rate=0.4804, wins=3799, games=9645)
    assert bad.tier == "D"
    assert bad.delta < -0.08


def test_hero_tier():
    assert score_hero_tier(0.5539) == "S+"
    assert score_hero_tier(0.5222) == "S"
    assert score_hero_tier(0.4804) == "B"


def test_destiny_ranking_and_synergy(tmp_path: Path):
    store = _build_store(tmp_path)
    recs = rank_destiny_items(store, "222")
    assert len(recs) == 2
    assert recs[0].item_id == "3153"  # Bork beats LDR by win-rate delta
    assert recs[0].delta > recs[1].delta

    bonus, labels = evaluate_augment_synergies(store, ["1002"], store.get_item_info("3157"))
    assert bonus >= 3.0
    assert any("尖端发明家" in l or "装备急速" in l for l in labels)

    assert item_is_owned("3003", ["3040"]) is True
    assert item_is_owned("3153", ["123153"]) is True
