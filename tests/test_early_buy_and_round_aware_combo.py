"""Tests for Task 12: 1400G opening buy, counter Tier-2 boots, round-aware combo strategy, and bundled icons."""

from __future__ import annotations

from pathlib import Path

from hex_helper.data.provider import AugmentInfo, CoreBuild, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore, DatasetDescriptor
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.augment_combo import build_combo_plan, evaluate_picker_combos
from hex_helper.scoring.matchup_optimizer import (
    build_next_buy_plan,
    optimize_next_items,
    recommend_counter_boots,
)


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path)
    static.items = {
        "1001": ItemStaticInfo("1001", "速度之靴", 300, ("Boots",), {}, "", "boots"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "3047": ItemStaticInfo("3047", "铁板靴", 1100, ("Boots",), {"FlatArmorMod": 25.0}, "", "boots"),
        "3111": ItemStaticInfo("3111", "水银之靴", 1100, ("Boots",), {"FlatSpellBlockMod": 20.0}, "", "boots"),
        "3006": ItemStaticInfo("3006", "狂战士胫甲", 1100, ("Boots",), {"PercentAttackSpeedMod": 0.25}, "", "boots"),
        "3158": ItemStaticInfo("3158", "明朗之靴", 900, ("Boots",), {}, "", "boots"),
        "3802": ItemStaticInfo("3802", "遗失的章节", 1200, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 40.0}, "", "other"),
        "1011": ItemStaticInfo("1011", "巨人腰带", 900, ("Health",), {"FlatHPPoolMod": 380.0}, "", "other"),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment", components=("3802",)),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110.0}, "", "complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0}, "", "complete_equipment", components=("1011",)),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3400, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
    }
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "103": HeroStat("103", "阿狸", "九尾妖狐", 50000, 26000, 0.52, 0.05, 20, False),
        "36": HeroStat("36", "蒙多医生", "祖安狂人", 40000, 21200, 0.53, 0.04, 12, False),
        "236": HeroStat("236", "卢锡安", "圣枪游侠", 60000, 30600, 0.51, 0.06, 25, False),
    }
    store._augments = {
        "3018": AugmentInfo("3018", "大腕", "prismatic"),
        "1048": AugmentInfo("1048", "珠光护手", "prismatic"),
        "3123": AugmentInfo("3123", "珠光手镯", "gold"),
        "1045": AugmentInfo("1045", "炼狱导管", "prismatic"),
        "1030": AugmentInfo("1030", "尤里卡", "prismatic"),
        "1092": AugmentInfo("1092", "易损", "gold"),
        "1068": AugmentInfo("1068", "循环往复", "gold"),
    }
    store._core_builds = {
        "103": [CoreBuild(("6655", "4645"), True, 10000, 5400, 0.54, 0.40)],
        "36": [CoreBuild(("3084",), True, 10000, 5400, 0.54, 0.50)],
        "236": [CoreBuild(("3031",), True, 10000, 5200, 0.52, 0.45)],
    }
    return store


def test_1400g_opening_buy_ap_and_tank_and_not_midgame(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    threat, recs = optimize_next_items(store, "103", (), 1400.0, [], (), ())
    plan_open = build_next_buy_plan(store, "103", (), 1400.0, recs, threat=threat, game_time=25.0)
    assert plan_open is not None
    assert plan_open.is_opening_buy is True
    assert "遗失的章节" in plan_open.buy_now_hint

    # Tank opening at 1400G favors Guardian's Horn (950G) + Ruby Crystal
    t_tank, recs_tank = optimize_next_items(store, "36", (), 1400.0, [], (), ())
    plan_tank = build_next_buy_plan(store, "36", (), 1400.0, recs_tank, threat=t_tank, game_time=15.0)
    assert plan_tank is not None
    assert plan_tank.is_opening_buy is True
    assert "守护者号角" in plan_tank.buy_now_hint

    # Mid-game (game_time=300s) MUST NOT show opening fountain buy
    plan_mid = build_next_buy_plan(store, "103", (), 1400.0, recs, threat=threat, game_time=300.0)
    assert plan_mid is not None
    assert plan_mid.is_opening_buy is False


def test_counter_tier2_boots_selection(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    # 3 hard-CC enemies -> Mercury's Treads (3111)
    cc_enemies = (
        PlayerLiveInfo("E1", "蕾欧娜", "Leona", "CHAOS", False, 9, 2, 2, 10, (), ("闪现", "雪球")),
        PlayerLiveInfo("E2", "阿木木", "Amumu", "CHAOS", False, 9, 2, 2, 10, (), ("闪现", "雪球")),
        PlayerLiveInfo("E3", "莫甘娜", "Morgana", "CHAOS", False, 9, 2, 2, 10, (), ("闪现",)),
    )
    threat_cc, _ = optimize_next_items(store, "103", ("1001",), 900.0, [], (), cc_enemies)
    bid, bname, breason = recommend_counter_boots(store, "103", ("1001",), [], threat_cc)
    assert bid == "3111"
    assert "水银" in bname
    assert "控" in breason

    # 4 heavy AD enemies -> Plated Steelcaps (3047)
    ad_enemies = tuple(
        PlayerLiveInfo(f"E{i}", "卢锡安", "Lucian", "CHAOS", False, 9, 4, 2, 4, ("3031",), ("闪现", "雪球"))
        for i in range(4)
    )
    threat_ad, _ = optimize_next_items(store, "36", (), 1100.0, [], (), ad_enemies)
    bid2, bname2, _ = recommend_counter_boots(store, "36", (), [], threat_ad)
    assert bid2 == "3047"
    assert "铁板" in bname2

    # Already owns Tier-2 boots -> returns empty
    assert recommend_counter_boots(store, "103", ("3020",), [], threat_cc) == ("", "", "")


def test_round_aware_combo_encourages_r1_gamble_and_blocks_r4_unstarted_gamble(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    big_shot = store.get_augment("3018")
    jeweled = store.get_augment("1048")
    assert big_shot is not None and jeweled is not None

    # Round 1 (0 augments owned): Lucian offered 大腕 (3018) -> encouraged as gamble opener!
    v_r1 = evaluate_picker_combos(store, "236", [], (), [(0, big_shot)])
    assert v_r1.round_index == 1
    assert v_r1.cards[0].verdict == "gamble"
    assert v_r1.cards[0].combo_delta >= 7.0

    # Round 4 (3 unrelated augments owned, 0 cores of big_shot_jeweled, no spell-crit axis):
    # MUST NOT recommend starting unstarted gamble and must flag 末轮慎赌!
    v_r4 = evaluate_picker_combos(store, "236", ["1045", "1030", "1068"], (), [(0, big_shot)])
    assert v_r4.round_index == 4
    assert v_r4.cards[0].verdict != "gamble"
    assert v_r4.cards[0].combo_delta < 0
    assert "第4轮" in v_r4.cards[0].reason

    # Even when axis-aligned on Round 4, verdict is NEVER "gamble"
    v_r4_aligned = evaluate_picker_combos(store, "236", ["1045", "1030", "1092"], (), [(0, big_shot)])
    assert v_r4_aligned.round_index == 4
    assert v_r4_aligned.cards[0].verdict != "gamble"

    # Round 4 completing an ALREADY OWNED core combo (holds 3018, offered 1048) -> boosted 收官成型!
    v_r4_finish = evaluate_picker_combos(store, "236", ["3018", "1030", "1092"], (), [(0, jeweled)])
    assert v_r4_finish.round_index == 4
    assert v_r4_finish.cards[0].verdict == "jackpot"
    assert v_r4_finish.cards[0].verdict_label == "收官成型"

    # Once 4/4 augments picked, plan recommends no further chase targets
    plan_done = build_combo_plan(store, "236", ["3018", "1048", "1030", "1092"], ())
    assert plan_done.round_index == 4
    assert plan_done.recommended_targets == ()


def test_stage_bundled_seed_includes_4digit_item_icons_within_budget(tmp_path: Path) -> None:
    import sys

    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from build_windows_release import stage_bundled_seed

    src = tmp_path / "cache"
    (src / "datasets").mkdir(parents=True)
    (src / "static").mkdir(parents=True)
    (src / "icons" / "item").mkdir(parents=True)
    (src / "datasets" / "index.json").write_text("{}", encoding="utf-8")
    (src / "static" / "ddragon_items.json").write_text("{}", encoding="utf-8")
    (src / "icons" / "item" / "4645.png").write_bytes(b"\x89PNG" + b"0" * 500)
    (src / "icons" / "item" / "774645.png").write_bytes(b"\x89PNG" + b"0" * 500)  # 6-digit skipped

    staging = tmp_path / "staged"
    count, total = stage_bundled_seed(src, staging)
    assert count == 3
    assert total > 500
    assert (staging / "icons" / "item" / "4645.png").is_file()
    assert not (staging / "icons" / "item" / "774645.png").exists()
