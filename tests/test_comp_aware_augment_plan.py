"""G6 Verification Tests: P0-7 Local Comp-Aware Augment Plan & P1-1 Special Build Gating Consistency."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hex_helper.data.provider import AugmentInfo, DatasetDescriptor, HeroAugmentStat, HeroStat
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import (
    build_combo_plan,
    export_combo_coach_context,
    infer_match_build_profile,
)
from hex_helper.scoring.matchup_optimizer import MatchupThreatProfile


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


def _make_threat(**overrides) -> MatchupThreatProfile:
    defaults = dict(
        enemy_armor_score=0.5,
        enemy_hp_score=0.8,
        enemy_mr_score=0.4,
        enemy_ad_ratio=0.5,
        enemy_ap_ratio=0.5,
        enemy_heal_score=0.5,
        enemy_shield_score=0.4,
        enemy_cc_score=1.0,
        ally_has_grievous=False,
        ally_has_shield_break=False,
        top_threat_name="",
        top_threat_kda="",
        top_threat_type="MIXED",
        summary_tags=(),
        enemy_snowball_count=1,
        enemy_exhaust_count=0,
        top_threat_spells=(),
        enemy_tank_count=0,
        enemy_mr_builder_count=0,
    )
    defaults.update(overrides)
    return MatchupThreatProfile(**defaults)


def test_enemy_three_tank_comp_boosts_eligible_antitank_targets(live_store: DataStore) -> None:
    threat_3tank = _make_threat(
        enemy_tank_count=3,
        enemy_hp_score=4.5,
        enemy_armor_score=3.8,
        summary_tags=("敌方高坦/堆甲",),
    )
    enemy_profiles = ("TANK", "TANK", "TANK", "AD", "AP")

    # Test on Jinx (222) and Brand (63)
    for hid, owned, items in (
        ("222", ["1079"], ("3031", "3085")),
        ("63", ["1388"], ("6653",)),
    ):
        plan_base = build_combo_plan(live_store, hid, owned, items, limit=4)
        plan_tank = build_combo_plan(
            live_store,
            hid,
            owned,
            items,
            limit=4,
            threat=threat_3tank,
            enemy_profiles=enemy_profiles,
        )
        assert plan_tank.recommended_targets
        assert any(t.comp_tag == "打敌多坦" for t in plan_tank.recommended_targets), (
            f"Hero {hid} expected 打敌多坦 tag vs 3 tanks, got {plan_tank.recommended_targets}"
        )
        # Every target must be 100% pool-eligible
        for t in plan_tank.recommended_targets:
            assert live_store.is_augment_eligible_for_hero(hid, t.augment_id)
        # Boosted anti-tank target must score higher than in no-threat baseline (if present in both)
        base_by_id = {t.augment_id: t.priority_score for t in plan_base.recommended_targets}
        tagged = [t for t in plan_tank.recommended_targets if t.comp_tag == "打敌多坦"]
        for t in tagged:
            if t.augment_id in base_by_id:
                assert t.priority_score > base_by_id[t.augment_id]


def test_enemy_dive_and_ally_zero_tank_boosts_survivability_mobility(live_store: DataStore) -> None:
    threat_dive = _make_threat(
        enemy_snowball_count=3,
        enemy_cc_score=2.6,
        enemy_ad_ratio=0.72,
    )
    ally_profiles = ("AD", "AP", "AP", "SUPPORT", "AD")  # 0 tanks!
    enemy_profiles = ("ASSASSIN", "FIGHTER", "FIGHTER", "AD", "SUPPORT")

    for hid, owned, items in (
        ("51", ["1115"], ("6676",)),
        ("99", ["1068"], ("6655",)),
    ):
        plan_dive = build_combo_plan(
            live_store,
            hid,
            owned,
            items,
            limit=4,
            threat=threat_dive,
            ally_profiles=ally_profiles,
            enemy_profiles=enemy_profiles,
        )
        assert plan_dive.recommended_targets
        assert any(t.comp_tag == "防突进保命" for t in plan_dive.recommended_targets), (
            f"Hero {hid} expected 防突进保命 tag vs dive/0-tank, got {plan_dive.recommended_targets}"
        )
        for t in plan_dive.recommended_targets:
            assert live_store.is_augment_eligible_for_hero(hid, t.augment_id)


def test_enemy_squishy_poke_and_high_heal_comp_tags(live_store: DataStore) -> None:
    # Pure squishy ranged poke comp
    threat_poke = _make_threat(enemy_tank_count=0, enemy_hp_score=0.5)
    plan_poke = build_combo_plan(
        live_store,
        "236",
        ["1019"],
        ("3508",),
        limit=4,
        threat=threat_poke,
        enemy_profiles=("AP", "AP", "AD", "AD", "SUPPORT"),
    )
    assert any(t.comp_tag == "打长手脆皮" for t in plan_poke.recommended_targets)
    assert all(live_store.is_augment_eligible_for_hero("236", t.augment_id) for t in plan_poke.recommended_targets)

    # High heal comp
    threat_heal = _make_threat(enemy_heal_score=3.8)
    plan_heal = build_combo_plan(
        live_store,
        "63",
        ["1068"],
        ("6653",),
        limit=4,
        threat=threat_heal,
    )
    assert any(t.comp_tag == "压高回复" for t in plan_heal.recommended_targets)
    assert all(live_store.is_augment_eligible_for_hero("63", t.augment_id) for t in plan_heal.recommended_targets)


def test_four_of_four_selected_augments_clears_targets_and_coach_payload(
    live_store: DataStore,
) -> None:
    threat_3tank = _make_threat(enemy_tank_count=3, enemy_hp_score=5.0)
    plan_full = build_combo_plan(
        live_store,
        "222",
        ["1225", "1079", "1115", "1328"],
        ("3031", "3085"),
        threat=threat_3tank,
        enemy_profiles=("TANK", "TANK", "TANK", "AD", "AP"),
    )
    assert plan_full.round_index == 4
    assert plan_full.recommended_targets == ()
    coach_dict = plan_full.to_coach_dict()
    assert coach_dict["top_targets"] == []


def test_coach_dict_exposes_structured_next_aug_pool_fields(live_store: DataStore) -> None:
    threat_3tank = _make_threat(enemy_tank_count=3, enemy_hp_score=4.5)
    plan = build_combo_plan(
        live_store,
        "222",
        ["1079"],
        ("3031",),
        limit=4,
        threat=threat_3tank,
        enemy_profiles=("TANK", "TANK", "TANK", "AD", "AP"),
    )
    ctx = plan.to_coach_dict()
    assert ctx == export_combo_coach_context(plan)
    assert len(ctx["top_targets"]) >= 1
    first = ctx["top_targets"][0]
    for key in ("id", "name", "rarity", "tier", "delta", "comp_tag", "combo", "gamble"):
        assert key in first


def test_empirical_heavy_demotion_blocks_low_percentile_trap_from_top(tmp_path: Path) -> None:
    static = StaticAssetsManager(tmp_path)
    store = DataStore(static)
    store._heroes["236"] = HeroStat("236", "卢锡安", "Lucian", 100000, 50000, 0.50, 0.08, 10)
    # Populate a gold rarity distribution where 1047 is terrible (P5, -5%) and 1328 is strong (P90, +4%)
    augs = {
        "3018": ("大腕", "prismatic"),
        "1048": ("珠光护手", "prismatic"),
        "1047": ("关键暴击", "gold"),
        "1328": ("暴击律动", "gold"),
    }
    for i in range(10):
        aid = f"900{i}"
        augs[aid] = (f"测试金卡{i}", "gold")
    for aid, (nm, rar) in augs.items():
        store._augments[aid] = AugmentInfo(aid, nm, rar)

    ha_map = {
        "3018": HeroAugmentStat("3018", 5000, 2600, 0.52, False),
        "1048": HeroAugmentStat("1048", 5000, 2700, 0.54, False),
        "1047": HeroAugmentStat("1047", 5000, 2250, 0.45, False),  # -5% delta, bottom percentile!
        "1328": HeroAugmentStat("1328", 5000, 2700, 0.54, False),  # +4% delta, top percentile!
    }
    for i in range(10):
        aid = f"900{i}"
        wins = 2400 + i * 25
        ha_map[aid] = HeroAugmentStat(aid, 5000, wins, wins / 5000.0, False)
    store._hero_augments["236"] = ha_map

    # Build rarity dist for gold
    from hex_helper.scoring.tier import DEFAULT_PRIOR_K, RarityDeltaDist

    gold_deltas = sorted(
        ((st.wins + DEFAULT_PRIOR_K * 0.50) / (st.games + DEFAULT_PRIOR_K)) - 0.50
        for aid, st in ha_map.items()
        if store._augments[aid].rarity == "gold"
    )
    mean_v = sum(gold_deltas) / len(gold_deltas)
    std_v = (sum((x - mean_v) ** 2 for x in gold_deltas) / len(gold_deltas)) ** 0.5
    store._hero_rarity_dists[("236", "gold")] = RarityDeltaDist(
        mean=mean_v,
        std=std_v,
        p25=gold_deltas[len(gold_deltas) // 4],
        median=gold_deltas[len(gold_deltas) // 2],
        p75=gold_deltas[(len(gold_deltas) * 3) // 4],
        p90=gold_deltas[-2],
        sorted_deltas=tuple(gold_deltas),
    )

    plan = build_combo_plan(store, "236", ["3018"], (), limit=4)
    # 1047 (first follow_up of big_shot_jeweled) is heavily demoted and MUST NOT beat 1048 or 1328
    ids = [t.augment_id for t in plan.recommended_targets]
    assert ids[0] == "1048"
    if "1047" in ids and "1328" in ids:
        assert ids.index("1328") < ids.index("1047")


def test_p1_1_tank_engine_hero_gate_consistency_in_match_profile(live_store: DataStore) -> None:
    # Vladimir (8) holding 1353 (坦克引擎) fails Heartsteel gate -> must NOT shift to TANK
    vlad_prof = infer_match_build_profile(live_store, "8", ["1353"], ())
    assert vlad_prof.primary_profile != "TANK"

    # Mundo (36) & Malphite (54) pass Heartsteel gate -> DO resolve to TANK
    mundo_prof = infer_match_build_profile(live_store, "36", ["1353"], ())
    malph_prof = infer_match_build_profile(live_store, "54", ["1353"], ())
    assert mundo_prof.primary_profile == "TANK"
    assert malph_prof.primary_profile == "TANK"
