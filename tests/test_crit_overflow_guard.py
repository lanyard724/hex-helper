"""G4 Verification Tests: P0-5 Dual-Source Crit-Overflow Guard & Tier D-Card Reroll Guard."""

from __future__ import annotations

import itertools
import os
from pathlib import Path

import pytest

from hex_helper.data.provider import AugmentInfo, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import (
    EXEMPT_HYBRID_CRIT_AUGMENT_IDS,
    PickerCardComboAdvice,
    build_combo_plan,
    estimate_effective_crit_chance,
    evaluate_picker_combos,
)
from hex_helper.scoring.matchup_optimizer import optimize_next_items
from hex_helper.scoring.tier import (
    STAT_OVERFLOW_LABELS,
    AugmentScore,
    rank_picker_cards,
    score_hero_augment,
)


def _load_live_1623_store() -> DataStore:
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    cache_root = Path(local_app) / "HexHelper" / "cache" if local_app else Path("")
    d23n = cache_root / "datasets" / "16.23" / "normal"
    if not (d23n / "heroes.json").is_file():
        pytest.skip("Local 16.23/normal dataset not available on disk")
    static = StaticAssetsManager(cache_root)
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        d23n,
    )
    return store


@pytest.fixture(scope="module")
def live_store() -> DataStore:
    return _load_live_1623_store()


def test_estimate_effective_crit_chance_dual_source_and_special_champions(
    tmp_path: Path,
) -> None:
    static = StaticAssetsManager(tmp_path)
    static.items["3031"] = ItemStaticInfo(
        "3031", "无尽之刃", 3400, ("Damage", "CriticalStrike"), {"FlatCritChanceMod": 0.25}, "", "complete_equipment"
    )
    static.items["3036"] = ItemStaticInfo(
        "3036", "多米尼克领主的致意", 3300, ("Damage", "CriticalStrike"), {"FlatCritChanceMod": 0.25}, "", "complete_equipment"
    )
    static.items["3032"] = ItemStaticInfo(
        "3032", "育恩塔尔荒野箭", 3000, ("Damage", "AttackSpeed"), {"FlatPhysicalDamageMod": 50.0}, "", "complete_equipment"
    )
    store = DataStore(static)

    # Live critChance > 1.5 normalizes by /100
    assert estimate_effective_crit_chance(store, "51", (), (), my_stats={"critChance": 100.0}) == 1.0
    assert estimate_effective_crit_chance(store, "51", (), (), my_stats={"critChance": 0.75}) == 0.75

    # Yun Tal (3032 / 223032): 0.12 early (<300s), 0.25 stacked (>=300s)
    assert estimate_effective_crit_chance(store, "222", ("3032",), (), game_time_s=120.0) == 0.12
    assert estimate_effective_crit_chance(store, "222", ("223032",), (), game_time_s=360.0) == 0.25

    # Augment crit + 1048 AP scaling (0.25 + 0.045 * AP/100)
    assert abs(
        estimate_effective_crit_chance(store, "112", (), ("1048",), my_stats={"abilityPower": 800.0}) - 0.61
    ) < 1e-4

    # Yasuo (157) & Yone (777): 2x multiplier
    assert estimate_effective_crit_chance(store, "157", ("3031", "3036"), ()) == 1.0
    assert estimate_effective_crit_chance(store, "777", ("3031", "3036"), ()) == 1.0

    # Tryndamere (23): fountain 0-fury floor (+0.35 when item_crit >= 0.50), beats stale live 0.50
    assert (
        estimate_effective_crit_chance(
            store, "23", ("3031", "3036"), (), my_stats={"critChance": 0.50}
        )
        == 0.85
    )
    assert estimate_effective_crit_chance(store, "23", ("3031",), ()) == 0.25


def _run_gold_monte_carlo(
    store: DataStore,
    hid: str,
    owned_items: tuple[str, ...],
    owned_augs: tuple[str, ...],
    my_stats: dict[str, float] | None,
    *,
    max_pairs: int = 500,
) -> dict[str, float]:
    hero = store.get_hero(hid)
    assert hero is not None
    ha_map = store.get_hero_augments(hid)
    gold_pool = [
        aid
        for aid, st in ha_map.items()
        if aid != "1047"
        and aid not in owned_augs
        and st.games >= 200
        and (au := store.get_augment(aid)) is not None
        and au.rarity == "gold"
    ]
    rdist = store.get_hero_rarity_distribution(hid, "gold")
    base_map: dict[str, AugmentScore] = {}
    for aid in ["1047", *gold_pool]:
        st = ha_map[aid]
        au = store.get_augment(aid)
        assert au is not None
        base_map[aid] = score_hero_augment(
            aid, au.name, "gold", hero.win_rate, st.wins, st.games, rarity_dist=rdist
        )

    pairs = list(itertools.combinations(gold_pool, 2))
    step = max(1, len(pairs) // max_pairs)
    sampled = pairs[::step]

    win_1047 = 0
    both_below_no_gamble_cnt = 0
    d_trap_cnt = 0
    banner_on_weak_cnt = 0

    for a2, a3 in sampled:
        offered_ids = ("1047", a2, a3)
        offered = [(idx, store.get_augment(aid)) for idx, aid in enumerate(offered_ids)]
        verdict = evaluate_picker_combos(
            store, hid, owned_augs, owned_items, offered, my_stats=my_stats, game_time_s=600.0
        )
        combo_by_slot = {vc.slot_index: vc for vc in verdict.cards}
        scored = [(idx, base_map[aid]) for idx, aid in enumerate(offered_ids)]
        decisions, banner = rank_picker_cards(scored, combo_by_slot=combo_by_slot, game_time_s=600.0)
        best = next((d for d in decisions if d.is_best), None)
        if best is not None and best.slot_index == 0:
            win_1047 += 1

        other_below = base_map[a2].is_below_rarity_par and base_map[a3].is_below_rarity_par
        other_has_real_jackpot_or_gamble = any(
            combo_by_slot.get(i) is not None and combo_by_slot[i].verdict in ("jackpot", "gamble")
            for i in (1, 2)
        )
        if other_below and not other_has_real_jackpot_or_gamble:
            both_below_no_gamble_cnt += 1
            if banner:
                banner_on_weak_cnt += 1
            elif best is not None and best.slot_index in (1, 2) and base_map[offered_ids[best.slot_index]].tier in ("C", "D"):
                d_trap_cnt += 1

    return {
        "n": float(len(sampled)),
        "win_1047_rate": win_1047 / max(1, len(sampled)),
        "both_below_no_gamble": float(both_below_no_gamble_cnt),
        "banner_on_weak": float(banner_on_weak_cnt),
        "d_trap_cnt": float(d_trap_cnt),
    }


def test_six_hero_full_crit_monte_carlo_and_zero_d_tier_trap(live_store: DataStore) -> None:
    # 5 non-overflow-conversion heroes at 100% crit: 1047 #1 strictly 0.0% & D-tier trap rate 0%
    strict_zero_cases = [
        ("895", ("6676", "3031", "3036", "6673"), ("1328", "1079")),  # Nilah
        ("236", ("3508", "3031", "3036", "6675"), ("1019", "1079")),  # Lucian
        ("51", ("3031", "6676", "3036", "3094"), ("1328", "1115")),   # Caitlyn
        ("41", ("3508", "6676", "3031", "3036"), ("1079",)),          # Gangplank
        ("222", ("3031", "3085", "3036", "3094"), ("1225", "1079")),  # Jinx
    ]
    for hid, items, augs in strict_zero_cases:
        res = _run_gold_monte_carlo(
            live_store, hid, items, augs, my_stats={"critChance": 1.0}, max_pairs=450
        )
        assert res["n"] >= 100
        assert res["win_1047_rate"] == 0.0, f"Hero {hid} pushed 1047 #1 at rate {res['win_1047_rate']}"
        assert res["d_trap_cnt"] == 0.0, f"Hero {hid} had D/C trap promotions: {res['d_trap_cnt']}"
        if res["both_below_no_gamble"] > 0:
            assert res["banner_on_weak"] == res["both_below_no_gamble"], (
                f"Hero {hid} missed reroll banner: {res['banner_on_weak']}/{res['both_below_no_gamble']}"
            )

    # 6th hero: Yasuo (157) at 100% crit -> -14.0 strong demotion vs unpenalized baseline
    yasuo_full = _run_gold_monte_carlo(
        live_store, "157", ("3031", "3033"), ("1328", "1019"), my_stats={"critChance": 1.0}, max_pairs=450
    )
    yasuo_low = _run_gold_monte_carlo(
        live_store, "157", (), ("1328", "1019"), my_stats={"critChance": 0.25}, max_pairs=450
    )
    assert yasuo_full["win_1047_rate"] < 0.20, f"Yasuo full-crit 1047 rate too high: {yasuo_full['win_1047_rate']}"
    assert yasuo_full["win_1047_rate"] < yasuo_low["win_1047_rate"] * 0.45, (
        f"Yasuo 1047 drop insufficient: {yasuo_low['win_1047_rate']:.3f} -> {yasuo_full['win_1047_rate']:.3f}"
    )


def test_1336_upgrade_ie_yasuo_yone_senna_partial_and_hybrid_exemptions(
    live_store: DataStore,
) -> None:
    aug_1047 = live_store.get_augment("1047")
    assert aug_1047 is not None
    cait = live_store.get_hero("51")
    assert cait is not None
    st_1047 = live_store.get_hero_augment_stat("51", "1047")
    assert st_1047 is not None
    rdist = live_store.get_hero_rarity_distribution("51", "gold")
    sc_1047 = score_hero_augment("1047", aug_1047.name, "gold", cait.win_rate, st_1047.wins, st_1047.games, rarity_dist=rdist)

    # 1. 1336 (升级：无尽之刃) full crit: active conflict -4.5 (NOT dead neutral=0.0!)
    v_1336 = evaluate_picker_combos(
        live_store,
        "51",
        ["1336", "1328"],
        ("3031", "6676", "3036", "3094"),
        [(0, aug_1047)],
        my_stats={"critChance": 1.0},
    )
    assert v_1336.cards[0].verdict == "conflict"
    assert v_1336.cards[0].verdict_label == "满暴递减"
    assert v_1336.cards[0].combo_delta == -4.5
    decs, _ = rank_picker_cards([(0, sc_1047)], combo_by_slot={0: v_1336.cards[0]})
    assert decs[0].combo_shift == -4.5
    assert decs[0].final_hex_score < decs[0].base_hex_score

    # 2. Yasuo (157) & Yone (777) full crit: -14.0
    for hid in ("157", "777"):
        v_bro = evaluate_picker_combos(
            live_store, hid, ["1328"], ("3031", "3033"), [(0, aug_1047)], my_stats={"critChance": 1.0}
        )
        assert v_bro.cards[0].verdict == "conflict"
        assert v_bro.cards[0].verdict_label == "满暴溢转·偏弱"
        assert v_bro.cards[0].combo_delta == -14.0
        d_bro, _ = rank_picker_cards([(0, sc_1047)], combo_by_slot={0: v_bro.cards[0]})
        assert d_bro[0].combo_shift == -14.0

    # 3. Senna (235) full crit: -18.0
    v_senna = evaluate_picker_combos(
        live_store, "235", ["1079"], ("6676", "3031", "3094"), [(0, aug_1047)], my_stats={"critChance": 1.0}
    )
    assert v_senna.cards[0].verdict == "conflict"
    assert v_senna.cards[0].verdict_label == "暴击溢转·低效"
    assert v_senna.cards[0].combo_delta == -18.0
    d_senna, _ = rank_picker_cards([(0, sc_1047)], combo_by_slot={0: v_senna.cards[0]})
    assert d_senna[0].combo_shift == -18.0

    # 4. Partial crit overflow (75% crit -> wasted=0.5 -> -9.0) vs safe 50% crit (no trigger)
    v_part = evaluate_picker_combos(
        live_store, "51", [], ("3031", "6676", "3036"), [(0, aug_1047)], my_stats={"critChance": 0.75}
    )
    assert v_part.cards[0].verdict == "conflict"
    assert v_part.cards[0].verdict_label == "暴击半溢·折价"
    assert v_part.cards[0].combo_delta == -9.0

    v_safe = evaluate_picker_combos(
        live_store, "51", [], ("3031", "6676"), [(0, aug_1047)], my_stats={"critChance": 0.50}
    )
    assert v_safe.cards[0].verdict_label not in STAT_OVERFLOW_LABELS

    # 5. Hybrid/mechanic crit augments (1048/1092/1077/1080/1118/1328/3123) STRICTLY EXEMPT at 100% crit
    offered_hybrid = [
        (i, live_store.get_augment(aid))
        for i, aid in enumerate(sorted(EXEMPT_HYBRID_CRIT_AUGMENT_IDS))
        if live_store.get_augment(aid) is not None
    ]
    v_hyb = evaluate_picker_combos(
        live_store,
        "51",
        ["1079"],
        ("3031", "6676", "3036", "3094"),
        offered_hybrid,
        my_stats={"critChance": 100.0},
    )
    for card in v_hyb.cards:
        assert card.verdict_label not in STAT_OVERFLOW_LABELS, f"Hybrid card {card.augment_id} falsely penalized!"


def test_rank_picker_cards_d_tier_ordinary_synergy_does_not_suppress_reroll_banner() -> None:
    sc_ovf = AugmentScore("1047", "关键暴击", "gold", "A", 0.49, 0.49, -0.01, 20000, False, percentile=55.0, is_below_rarity_par=False)
    sc_d1 = AugmentScore("2001", "升级：耀光", "gold", "D", 0.44, 0.44, -0.05, 8000, False, percentile=8.0, is_below_rarity_par=True)
    sc_d2 = AugmentScore("2002", "弱金卡", "gold", "C", 0.46, 0.46, -0.03, 8000, False, percentile=18.0, is_below_rarity_par=True)

    combo_by_slot = {
        0: PickerCardComboAdvice(0, "1047", "关键暴击", "gold", "conflict", "暴击溢出·废卡", -35.0, "100%溢出", "", False),
        1: PickerCardComboAdvice(1, "2001", "升级：耀光", "gold", "synergy", "流派顺滑", 3.2, "契合AD", "", False),
    }
    decisions, banner = rank_picker_cards([(0, sc_ovf), (1, sc_d1), (2, sc_d2)], combo_by_slot=combo_by_slot)
    assert banner != "", "Below-par D card ordinary synergy must NOT suppress reroll banner!"
    assert "骰子" in banner
    assert all(not d.is_best for d in decisions)
    assert decisions[0].combo_shift == -35.0


def test_left_overlay_build_combo_plan_filters_1047_at_75_pct_crit(live_store: DataStore) -> None:
    # Caitlyn (51) holds 1328 (core of crit_sustain_carry with 1047)
    plan_low = build_combo_plan(live_store, "51", ["1328"], ("3031",), my_stats={"critChance": 0.50})
    assert any(t.augment_id == "1047" for t in plan_low.recommended_targets)

    plan_high_items = build_combo_plan(live_store, "51", ["1328"], ("3031", "6676"))
    assert all(t.augment_id != "1047" for t in plan_high_items.recommended_targets)

    plan_high_live = build_combo_plan(live_store, "51", ["1328"], (), my_stats={"critChance": 0.80})
    assert all(t.augment_id != "1047" for t in plan_high_live.recommended_targets)


def test_item_optimizer_does_not_penalize_navori_rfc_hurricane_at_100_pct_crit(
    live_store: DataStore,
) -> None:
    # Rev 2 CUT: matchup_optimizer MUST NOT apply -2.8 crit-overflow penalty to Navori (6675), RFC (3094), Hurricane (3085)
    checks = [
        ("498", ("3508", "3031"), "6675"),  # Xayah -> Navori
        ("15", ("3508", "3031"), "6675"),   # Sivir -> Navori
        ("202", ("6676", "3031"), "3094"),  # Jhin -> RFC
        ("222", ("3031", "6676"), "3085"),  # Jinx -> Hurricane
        ("29", ("3031", "6676"), "3085"),   # Twitch -> Hurricane
    ]
    for hid, two_crit_items, target_iid in checks:
        _, recs_without_1047 = optimize_next_items(live_store, hid, two_crit_items, 3200.0, (), (), (), limit=12)
        _, recs_with_1047 = optimize_next_items(live_store, hid, two_crit_items, 3200.0, ("1047",), (), (), limit=12)
        r_no = next((r for r in recs_without_1047 if r.item_id == target_iid), None)
        r_yes = next((r for r in recs_with_1047 if r.item_id == target_iid), None)
        assert r_no is not None and r_yes is not None, f"Missing item {target_iid} for hero {hid}"
        # Score must NOT drop by a -2.8 crit-overflow penalty when holding 1047 at 100% crit
        assert r_yes.score >= r_no.score - 0.5, (
            f"Item {target_iid} on hero {hid} unexpectedly penalized at 100% crit: {r_no.score} -> {r_yes.score}"
        )
        assert not any("暴击溢出" in reason for reason in r_yes.reasons)
