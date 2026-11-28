"""G3 Verification Tests: P0-4 Authoritative 173-Hero Augment Pool & augment_combos.json Integrity."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hex_helper.data.augment_combos import get_augment_combo_db
from hex_helper.data.provider import DatasetDescriptor
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import build_combo_plan, evaluate_picker_combos


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


def test_augment_combos_json_has_zero_illegal_core_bindings_and_clean_1134_split(
    live_store: DataStore,
) -> None:
    db = get_augment_combo_db()
    heroes = live_store.all_heroes()
    assert len(heroes) == 173
    assert "normal" in live_store._mode_snapshots
    assert "first-prismatic" in live_store._mode_snapshots

    # Split rules: ranged 1134 vs melee 1006/2141 cleanly separated
    ranged_rule = db.get_by_id("draw_sword_ranged_brawler")
    melee_rule = db.get_by_id("blade_waltz_melee_brawler")
    assert ranged_rule is not None
    assert melee_rule is not None
    assert "1134" in ranged_rule.core_augments
    assert "1134" not in melee_rule.core_augments
    assert "1134" not in melee_rule.follow_ups
    assert {"1006", "2141"}.issubset(set(melee_rule.core_augments))

    # Zero illegal (rule, hero, core_augment) bindings across the entire JSON
    illegal: list[tuple[str, str, str]] = []
    for rule in db.entries:
        for hid in rule.hero_bonus:
            for aid in rule.core_augments:
                if not live_store.is_augment_eligible_for_hero(hid, aid):
                    illegal.append((rule.combo_id, hid, aid))
    assert illegal == [], f"Found illegal core bindings in augment_combos.json: {illegal[:10]}"


def test_all_173_heroes_have_zero_illegal_opening_combos_and_targets(
    live_store: DataStore,
) -> None:
    heroes = live_store.all_heroes()
    assert len(heroes) == 173

    illegal_opening: list[tuple[str, str, str]] = []
    illegal_targets: list[tuple[str, str]] = []
    empty_openings: list[str] = []

    for hero in heroes:
        hid = hero.hero_id
        plan = build_combo_plan(live_store, hid, [], ())
        if not plan.opening_combos:
            empty_openings.append(hid)
        for prog in plan.opening_combos:
            for aid in prog.missing_core_ids:
                if not live_store.is_augment_eligible_for_hero(hid, aid):
                    illegal_opening.append((hid, prog.combo_id, aid))
        for tgt in plan.recommended_targets:
            if not live_store.is_augment_eligible_for_hero(hid, tgt.augment_id):
                illegal_targets.append((hid, tgt.augment_id))

    assert empty_openings == [], f"Heroes with empty opening combos: {empty_openings[:10]}"
    assert illegal_opening == [], f"Illegal opening combo augments: {illegal_opening[:10]}"
    assert illegal_targets == [], f"Illegal opening recommended targets: {illegal_targets[:10]}"


def test_all_173_heroes_have_zero_illegal_midgame_targets_and_missing_cores(
    live_store: DataStore,
) -> None:
    db = get_augment_combo_db()
    illegal_missing: list[tuple[str, str, str]] = []
    illegal_targets: list[tuple[str, str]] = []

    for hero in live_store.all_heroes():
        hid = hero.hero_id
        # Seed mid-game with hero's first curated combo core (if any) or top eligible augment
        hero_rules = db.combos_for_hero(hid)
        owned: list[str] = []
        if hero_rules:
            for aid in hero_rules[0].core_augments:
                if live_store.is_augment_eligible_for_hero(hid, aid):
                    owned.append(aid)
                    break
        if not owned:
            top_elig = live_store.get_top_eligible_augments(hid, limit=2)
            owned = [aid for aid, _d, _p in top_elig[:1]]
        assert owned, f"Hero {hid} ({hero.name}) has no eligible augments"

        plan = build_combo_plan(live_store, hid, owned, ())
        for prog in plan.active_combos:
            for aid in prog.missing_core_ids:
                if not live_store.is_augment_eligible_for_hero(hid, aid):
                    illegal_missing.append((hid, prog.combo_id, aid))
        for tgt in plan.recommended_targets:
            if not live_store.is_augment_eligible_for_hero(hid, tgt.augment_id):
                illegal_targets.append((hid, tgt.augment_id))

    assert illegal_missing == [], f"Illegal mid-game missing_core_ids: {illegal_missing[:10]}"
    assert illegal_targets == [], f"Illegal mid-game recommended_targets: {illegal_targets[:10]}"


def test_melee_and_dual_form_heroes_never_recommended_1134_draw_sword(
    live_store: DataStore,
) -> None:
    # Yi (11), Yasuo (157), Yone (777), Jayce (126), Nidalee (76), Elise (60), Gnar (150), Kayn (141)
    forbidden_heroes = ("11", "157", "777", "126", "76", "60", "150", "141")
    for hid in forbidden_heroes:
        assert live_store.is_augment_eligible_for_hero(hid, "1134") is False

        # Opening + mid-game with 1058 (whose shared follow_ups contains 1134), 1006, 2141
        for owned in ([], ["1058"], ["1006"], ["2141"]):
            plan = build_combo_plan(live_store, hid, owned, ())
            opening_ids = {a for c in plan.opening_combos for a in c.missing_core_ids}
            active_missing = {a for c in plan.active_combos for a in c.missing_core_ids}
            target_ids = {t.augment_id for t in plan.recommended_targets}
            assert "1134" not in opening_ids, f"Hero {hid} got 1134 in opening_combos"
            assert "1134" not in active_missing, f"Hero {hid} got 1134 in active missing cores"
            assert "1134" not in target_ids, f"Hero {hid} got 1134 in recommended_targets (owned={owned})"

        # Gate C picker reroll advice must never mention 亮出你的剑
        weak_offered = [
            (0, live_store.get_augment("1005")),
            (1, live_store.get_augment("1037")),
            (2, live_store.get_augment("1013")),
        ]
        verdict = evaluate_picker_combos(live_store, hid, ["2141"], (), weak_offered)
        assert "亮出你的剑" not in verdict.reroll_advice

    # Ranged 1134 users remain eligible and draw_sword_ranged_brawler chases 2141 cleanly
    for hid in ("360", "236", "67", "29", "6", "133"):
        assert live_store.is_augment_eligible_for_hero(hid, "1134") is True
    samira_plan = build_combo_plan(live_store, "360", ["1134"], ())
    assert any(t.augment_id == "2141" for t in samira_plan.recommended_targets)


def test_rare_legal_augments_remain_eligible_and_gate_c_filters_unreachable_reroll(
    live_store: DataStore,
) -> None:
    # Kai'Sa (145) + 闪光弹 (1187), Karthus (30) + 闪闪现现 (1348), Senna (235) + 鞭挞 (3172)
    assert live_store.is_augment_eligible_for_hero("145", "1187") is True
    assert live_store.is_augment_eligible_for_hero("30", "1348") is True
    assert live_store.is_augment_eligible_for_hero("235", "3172") is True

    # Gate C: Yasuo (157) cannot roll 1029 (虚幻武器); holding 1058 (秘术冲拳) must NOT advise chasing 虚幻武器
    assert live_store.is_augment_eligible_for_hero("157", "1029") is False
    offered_bad = [
        (0, live_store.get_augment("1005")),
        (1, live_store.get_augment("1037")),
        (2, live_store.get_augment("1013")),
    ]
    verdict = evaluate_picker_combos(live_store, "157", ["1058"], (), offered_bad)
    assert "虚幻武器" not in verdict.reroll_advice
