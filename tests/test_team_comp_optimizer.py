"""Tests for 0ms C(15,5)=3003 team comp optimizer, swap deltas, and climb/fun dual modes."""

from __future__ import annotations

import time
from itertools import combinations

from hex_helper.data.provider import CoreBuild, HeroStat, ItemStaticInfo
from hex_helper.scoring.team_comp import (
    BestFiveComp,
    SwapRecommendation,
    TeamCompAnalysis,
    _extract_feat,
    _score_five,
    analyze_champ_select_pool,
    classify_hero_role,
)


class _FakeStore:
    def __init__(self, heroes: dict[str, HeroStat], profiles: dict[str, str]) -> None:
        self._heroes = heroes
        self._profiles = profiles

    def get_hero(self, hid: str) -> HeroStat | None:
        return self._heroes.get(str(hid))

    def get_core_builds(self, hid: str) -> list[CoreBuild]:
        prof = self._profiles.get(str(hid))
        if prof is None:
            return []
        return [CoreBuild(items=(f"item_{prof}",), ordered=True, games=5000, wins=2600, win_rate=0.52, pick_rate=0.3)]

    def get_item_info(self, iid: str) -> ItemStaticInfo:
        if iid == "item_TANK":
            return ItemStaticInfo(
                iid,
                "心之钢",
                3000,
                stats={"FlatHPPoolMod": 900.0, "FlatArmorMod": 60.0, "FlatSpellBlockMod": 40.0},
            )
        if iid == "item_AD":
            return ItemStaticInfo(
                iid,
                "无尽之刃",
                3400,
                stats={"FlatPhysicalDamageMod": 80.0, "FlatCritChanceMod": 0.25},
            )
        return ItemStaticInfo(iid, "卢登的伙伴", 2850, stats={"FlatMagicDamageMod": 100.0})


def _mk_hero(hid: str, name: str, wr: float, rank: int = 20) -> HeroStat:
    return HeroStat(hid, name, name, games=15000, wins=int(15000 * wr), win_rate=wr, pick_rate=0.05, rank=rank)


def test_swap_delta_recommends_frontline_tank_when_team_is_all_ap() -> None:
    heroes = {
        "1": _mk_hero("1", "拉克丝", 0.51),
        "2": _mk_hero("2", "泽拉斯", 0.52),
        "3": _mk_hero("3", "吉格斯", 0.51),
        "4": _mk_hero("4", "维克托", 0.50),
        "5": _mk_hero("5", "辛德拉", 0.49),
        "6": _mk_hero("6", "诺提勒斯", 0.525),
        "7": _mk_hero("7", "金克丝", 0.53),
    }
    profiles = {"1": "AP", "2": "AP", "3": "AP", "4": "AP", "5": "AP", "6": "TANK", "7": "AD"}
    store = _FakeStore(heroes, profiles)

    res = analyze_champ_select_pool(
        my_hero_id="5",
        team_hero_ids=("1", "2", "3", "4", "5"),
        bench_hero_ids=("6", "7"),
        store=store,  # type: ignore[arg-type]
        goal_mode="climb",
    )
    assert isinstance(res, TeamCompAnalysis)
    assert res.goal_mode == "climb"
    assert res.best_swap is not None
    assert isinstance(res.best_swap, SwapRecommendation)
    assert res.best_swap.hero_id in ("6", "7")
    assert res.best_swap.score_delta > 4.0
    assert res.best_five is not None
    assert isinstance(res.best_five, BestFiveComp)
    assert "6" in res.best_five.hero_ids and "7" in res.best_five.hero_ids
    assert "TANK" in res.needed_roles and "AD" in res.needed_roles
    assert res.is_warn is True
    assert res.best_swap.hero_name in res.summary_text


def test_15_hero_pool_enumerates_all_3003_combinations_under_40ms() -> None:
    heroes = {str(i): _mk_hero(str(i), f"英雄{i}", 0.48 + (i % 7) * 0.01) for i in range(1, 16)}
    profiles = {str(i): ("TANK" if i % 4 == 0 else ("AD" if i % 3 == 0 else "AP")) for i in range(1, 16)}
    store = _FakeStore(heroes, profiles)

    t0 = time.perf_counter()
    res_climb = analyze_champ_select_pool(
        my_hero_id="1",
        team_hero_ids=("1", "2", "3", "4", "5"),
        bench_hero_ids=tuple(str(i) for i in range(6, 16)),
        store=store,  # type: ignore[arg-type]
        goal_mode="climb",
    )
    elapsed_climb_ms = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    res_fun = analyze_champ_select_pool(
        my_hero_id="1",
        team_hero_ids=("1", "2", "3", "4", "5"),
        bench_hero_ids=tuple(str(i) for i in range(6, 16)),
        store=store,  # type: ignore[arg-type]
        goal_mode="fun",
    )
    elapsed_fun_ms = (time.perf_counter() - t1) * 1000.0

    assert res_climb.best_five is not None
    assert res_climb.best_five.evaluated_combos == 3003
    assert elapsed_climb_ms < 40.0
    assert res_fun.best_five is not None
    assert res_fun.best_five.evaluated_combos == 3003
    assert elapsed_fun_ms < 40.0


def test_dual_modes_climb_vs_fun_uses_ceiling_tags_and_aram_balance() -> None:
    # Real Riot IDs:
    # "86"  (盖伦): no ceiling tag, mild ARAM buff (dmg_taken 0.95)
    # "360" (莎弥拉): curated ceiling tag "大腕珠光核爆", HIGH_HEAL_HEROES
    # "7"   (乐芙兰): no ceiling tag, heavy ARAM buff (dmg_dealt 1.1, dmg_taken 0.9, AH +20)
    # "115" (吉格斯): no ceiling tag, heavy ARAM nerf (dmg_dealt 0.92, dmg_taken 1.2, AH -20)
    heroes = {
        "9001": _mk_hero("9001", "队友法师甲", 0.50),
        "9002": _mk_hero("9002", "队友法师乙", 0.50),
        "9003": _mk_hero("9003", "队友射手丙", 0.50),
        "9004": _mk_hero("9004", "队友法师丁", 0.50),
        "9005": _mk_hero("9005", "我的普通法师", 0.49),
        "86": _mk_hero("86", "盖伦", 0.51),
        "360": _mk_hero("360", "莎弥拉", 0.51),
        "7": _mk_hero("7", "乐芙兰", 0.50),
        "115": _mk_hero("115", "吉格斯", 0.50),
    }
    profiles = {
        "9001": "AP",
        "9002": "AP",
        "9003": "AD",
        "9004": "AP",
        "9005": "AP",
        "86": "TANK",
        "360": "AD",
        "7": "AP",
        "115": "AP",
    }
    store = _FakeStore(heroes, profiles)

    res_climb = analyze_champ_select_pool(
        my_hero_id="9005",
        team_hero_ids=("9001", "9002", "9003", "9004", "9005"),
        bench_hero_ids=("86", "360", "7", "115"),
        store=store,  # type: ignore[arg-type]
        goal_mode="climb",
    )
    res_fun = analyze_champ_select_pool(
        my_hero_id="9005",
        team_hero_ids=("9001", "9002", "9003", "9004", "9005"),
        bench_hero_ids=("86", "360", "7", "115"),
        store=store,  # type: ignore[arg-type]
        goal_mode=" FUN ",
    )

    assert res_climb.goal_mode == "climb"
    assert res_fun.goal_mode == "fun"

    # Climb prioritizes filling missing frontline tank over a high-ceiling AD carry
    assert res_climb.best_swap is not None
    assert res_climb.best_swap.hero_id == "86"
    assert res_climb.best_swap.reason == "补前排开团"
    assert res_climb.best_five is not None and "冲分最优" in res_climb.best_five.tags

    # Fun prioritizes curated high-ceiling combo champion ("360" 莎弥拉)
    assert res_fun.best_swap is not None
    assert res_fun.best_swap.hero_id == "360"
    assert res_fun.best_swap.reason == "高上限爽局核"
    assert res_fun.best_five is not None and "爽局高上限" in res_fun.best_five.tags

    # ARAM hidden balance edge: heavily buffed "7" (乐芙兰) beats heavily nerfed "115" (吉格斯) at equal WR/role
    delta_lb, _ = res_climb.swap_deltas["7"]
    delta_ziggs, _ = res_climb.swap_deltas["115"]
    assert delta_lb > delta_ziggs


def test_all_ad_team_recommends_ap_or_tank_and_flags_needed_roles() -> None:
    heroes = {
        "9101": _mk_hero("9101", "射手A", 0.50),
        "9102": _mk_hero("9102", "射手B", 0.50),
        "9103": _mk_hero("9103", "射手C", 0.50),
        "9104": _mk_hero("9104", "射手D", 0.50),
        "9105": _mk_hero("9105", "射手E", 0.49),
        "9106": _mk_hero("9106", "法师补位", 0.52),
    }
    profiles = {
        "9101": "AD",
        "9102": "AD",
        "9103": "AD",
        "9104": "AD",
        "9105": "AD",
        "9106": "AP",
    }
    store = _FakeStore(heroes, profiles)

    res = analyze_champ_select_pool(
        my_hero_id="9105",
        team_hero_ids=("9101", "9102", "9103", "9104", "9105"),
        bench_hero_ids=("9106",),
        store=store,  # type: ignore[arg-type]
        goal_mode="climb",
    )
    assert res.needed_roles == frozenset({"TANK", "AP"})
    assert res.best_swap is not None
    assert res.best_swap.hero_id == "9106"
    assert res.best_swap.reason == "破纯AD困局"
    assert res.best_five is not None
    assert res.best_five.evaluated_combos == 6


def test_edge_cases_empty_small_duplicates_over15_and_balanced_comp() -> None:
    heroes = {str(9200 + i): _mk_hero(str(9200 + i), f"测试英雄{i}", 0.50 + (i % 5) * 0.005) for i in range(18)}
    profiles = {
        str(9200 + i): ("TANK" if i % 4 == 0 else ("AD" if i % 3 == 0 else "AP")) for i in range(18)
    }
    store = _FakeStore(heroes, profiles)

    # 1. Empty / unknown pool
    empty = analyze_champ_select_pool(None, ("unknown1",), ("unknown2",), store)  # type: ignore[arg-type]
    assert empty.current_score == 0.0
    assert empty.best_swap is None
    assert empty.swap_deltas == {}
    assert empty.best_five is None
    assert empty.summary_text == ""
    assert empty.is_warn is False
    assert empty.needed_roles == frozenset()

    # 2. Fewer than 5 total unique heroes + duplicate entries across team & bench
    small = analyze_champ_select_pool(
        my_hero_id="9200",
        team_hero_ids=("9200", "9201", "9202", "9201"),
        bench_hero_ids=("9201", "9203", "9203"),
        store=store,  # type: ignore[arg-type]
    )
    assert small.best_five is None
    assert set(small.swap_deltas.keys()) == {"9203"}

    # 3. Pool > 15 champions is capped to 15 -> 3003 combos max
    capped = analyze_champ_select_pool(
        my_hero_id=None,
        team_hero_ids=tuple(str(9200 + i) for i in range(5)),
        bench_hero_ids=tuple(str(9200 + i) for i in range(5, 18)),
        store=store,  # type: ignore[arg-type]
    )
    assert capped.swap_deltas == {}
    assert capped.best_swap is None
    assert capped.best_five is not None
    assert capped.best_five.evaluated_combos == 3003

    # 4. Balanced comp with weak bench -> non-warning balanced summary
    balanced_heroes = {
        "9301": _mk_hero("9301", "诺提勒斯", 0.53),
        "9302": _mk_hero("9302", "拉克丝", 0.53),
        "9303": _mk_hero("9303", "金克丝", 0.53),
        "9304": _mk_hero("9304", "维克托", 0.52),
        "9305": _mk_hero("9305", "艾希", 0.52),
        "9306": _mk_hero("9306", "弱备选", 0.45),
    }
    balanced_profiles = {"9301": "TANK", "9302": "AP", "9303": "AD", "9304": "AP", "9305": "AD", "9306": "AP"}
    balanced_store = _FakeStore(balanced_heroes, balanced_profiles)
    bal = analyze_champ_select_pool(
        my_hero_id="9305",
        team_hero_ids=("9301", "9302", "9303", "9304", "9305"),
        bench_hero_ids=("9306",),
        store=balanced_store,  # type: ignore[arg-type]
        goal_mode="invalid_fallback",
    )
    assert bal.goal_mode == "climb"
    assert bal.best_swap is None
    assert bal.is_warn is False
    assert bal.needed_roles == frozenset()
    assert bal.summary_text.startswith("✓ 阵容均衡")


def test_classify_hero_role_handles_profiles_and_empty_builds() -> None:
    heroes = {
        "1": _mk_hero("1", "坦克", 0.5),
        "2": _mk_hero("2", "物理", 0.5),
        "3": _mk_hero("3", "法术", 0.5),
        "4": _mk_hero("4", "无出装", 0.5),
    }
    store = _FakeStore(heroes, {"1": "TANK", "2": "AD", "3": "AP"})
    assert classify_hero_role("1", store) == "TANK"  # type: ignore[arg-type]
    assert classify_hero_role("2", store) == "AD"  # type: ignore[arg-type]
    assert classify_hero_role("3", store) == "AP"  # type: ignore[arg-type]
    assert classify_hero_role("4", store) == "AP"  # type: ignore[arg-type]


def test_fast_enumerator_matches_bruteforce_score_five_and_warns_without_bench_swap() -> None:
    heroes = {str(i): _mk_hero(str(i), f"英雄{i}", 0.48 + (i % 6) * 0.01) for i in range(1, 11)}
    profiles = {str(i): ("TANK" if i % 4 == 0 else ("AD" if i % 3 == 0 else "AP")) for i in range(1, 11)}
    store = _FakeStore(heroes, profiles)

    feats = [_extract_feat(str(i), store) for i in range(1, 11)]  # type: ignore[arg-type]
    valid_feats = [f for f in feats if f is not None]

    for mode in ("climb", "fun"):
        res = analyze_champ_select_pool(
            my_hero_id="1",
            team_hero_ids=("1", "2", "3", "4", "5"),
            bench_hero_ids=("6", "7", "8", "9", "10"),
            store=store,  # type: ignore[arg-type]
            goal_mode=mode,
        )
        assert res.best_five is not None
        assert res.best_five.evaluated_combos == 252
        expected_max = max(_score_five(comb, mode) for comb in combinations(valid_feats, 5))
        assert res.best_five.score == expected_max

    # All-AP team with empty bench -> triggers missing-role warning text without swap action
    ap_only_store = _FakeStore(
        {str(i): _mk_hero(str(i), f"法师{i}", 0.50) for i in range(1, 6)},
        {str(i): "AP" for i in range(1, 6)},
    )
    no_bench = analyze_champ_select_pool(
        my_hero_id="1",
        team_hero_ids=("1", "2", "3", "4", "5"),
        bench_hero_ids=(),
        store=ap_only_store,  # type: ignore[arg-type]
        goal_mode="climb",
    )
    assert no_bench.best_swap is None
    assert no_bench.is_warn is True
    assert "⚠ 阵容缺前排/物理" in no_bench.summary_text

