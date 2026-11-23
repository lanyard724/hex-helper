"""Tests for Patch 26.23 Augment Combo DB & 0ms Synergy/Anti-Synergy Recommendation Engine."""

from __future__ import annotations

import json
from pathlib import Path

from hex_helper.data.augment_combos import (
    VALID_MECHANIC_AXES,
    AugmentComboDatabase,
    get_augment_combo_db,
)


def _cached_2623_dataset_dir() -> Path | None:
    import os

    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app:
        return None
    p = Path(local_app) / "HexHelper" / "cache" / "datasets" / "16.23" / "normal"
    return p if (p / "augments.json").is_file() else None


def test_combo_db_loads_and_all_ids_valid():
    db = get_augment_combo_db()
    assert db.patch == "26.23"
    assert len(db.entries) >= 28

    seen_ids: set[str] = set()
    for rule in db.entries:
        assert rule.combo_id and rule.combo_id not in seen_ids
        seen_ids.add(rule.combo_id)
        assert rule.name
        assert len(rule.core_augments) >= 2
        assert rule.synergy_reason
        for ax in rule.anti_axes:
            assert ax in VALID_MECHANIC_AXES, f"Unknown anti_axis {ax!r} in {rule.combo_id}"

    # Verify specific iconic combos are present (Lucian BigShot+Jeweled, InfiniteRecursion AP loop)
    big_shot_combos = db.combos_for_augment("3018")
    assert any("1048" in c.core_augments or "1048" in c.follow_ups for c in big_shot_combos)
    assert any("236" in c.hero_bonus for c in big_shot_combos)

    inf_rec_combos = db.combos_for_augment("1388")
    assert len(inf_rec_combos) >= 1
    assert any("AD_ATTACK" in c.anti_axes for c in inf_rec_combos)

    # Audit against real local 26.23 active augment pool when available
    ds_dir = _cached_2623_dataset_dir()
    if ds_dir is not None:
        active_augs = json.loads((ds_dir / "augments.json").read_text(encoding="utf-8"))
        active_heroes = json.loads((ds_dir / "heroes.json").read_text(encoding="utf-8"))
        active_aug_ids = set(active_augs.keys())
        active_hero_ids = set(active_heroes.keys())

        for rule in db.entries:
            for aid in (*rule.core_augments, *rule.follow_ups):
                assert aid in active_aug_ids, f"Invalid augment id {aid} in combo {rule.combo_id}"
            for hid in rule.hero_bonus:
                assert hid in active_hero_ids, f"Invalid hero id {hid} in combo {rule.combo_id}"

        for aid, axes in db.all_explicit_axes().items():
            assert aid in active_aug_ids, f"Invalid augment id {aid} in augment_axes"
            assert len(axes) >= 1
            for ax in axes:
                assert ax in VALID_MECHANIC_AXES, f"Unknown axis {ax} for augment {aid}"


def test_combo_db_user_override_merges_cleanly(tmp_path: Path):
    override = tmp_path / "augment_combos.json"
    override.write_text(
        json.dumps(
            {
                "patch": "26.23",
                "combos": [
                    {
                        "id": "custom_test_combo",
                        "name": "自定义测试联动",
                        "gamble": True,
                        "profile": "AP",
                        "core_augments": ["1388", "1045"],
                        "follow_ups": ["1133"],
                        "hero_bonus": ["21"],
                        "anti_axes": ["AD_ATTACK"],
                        "synergy_reason": "测试联动说明",
                        "reroll_hint": "追炼狱导管",
                    }
                ],
                "augment_axes": {"3018": ["CRIT_SPELL", "ULT_CYCLE", "GAMBLE_HIGHROLL"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db = AugmentComboDatabase(user_override_path=override)
    c = db.get_by_id("custom_test_combo")
    assert c is not None
    assert c.name == "自定义测试联动"
    assert "GAMBLE_HIGHROLL" in db.get_axes("3018")


def _make_combo_test_store(tmp_path: Path):
    from hex_helper.data.provider import AugmentInfo, HeroStat, ItemStaticInfo
    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.data.store import DataStore

    static = StaticAssetsManager(tmp_path)
    static.items["6653"] = ItemStaticInfo(
        item_id="6653",
        name="兰德里的折磨",
        gold=3000,
        category="complete_equipment",
        tags=("SpellDamage", "Health"),
        stats={"FlatMagicDamageMod": 70.0, "FlatHPPoolMod": 300.0},
    )
    static.items["3089"] = ItemStaticInfo(
        item_id="3089",
        name="灭世者的死亡之帽",
        gold=3500,
        category="complete_equipment",
        tags=("SpellDamage",),
        stats={"FlatMagicDamageMod": 130.0},
    )
    static.items["3031"] = ItemStaticInfo(
        item_id="3031",
        name="无尽之刃",
        gold=3400,
        category="complete_equipment",
        tags=("Damage", "CriticalStrike"),
        stats={"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25},
    )

    store = DataStore(static)
    store._heroes["236"] = HeroStat("236", "卢锡安", "Lucian", 10000, 5100, 0.51, 0.08, 10)
    store._heroes["21"] = HeroStat("21", "厄运小姐", "MissFortune", 12000, 5300, 0.53, 0.09, 10)
    store._heroes["63"] = HeroStat("63", "布兰德", "Brand", 10000, 5300, 0.53, 0.07, 10)

    sample_augs = [
        ("3018", "大腕", "prismatic"),
        ("1048", "珠光护手", "prismatic"),
        ("3123", "珠光手镯", "gold"),
        ("1019", "全凭身法", "prismatic"),
        ("1047", "关键暴击", "gold"),
        ("1388", "无限循环往复", "prismatic"),
        ("1068", "循环往复", "gold"),
        ("1045", "炼狱导管", "prismatic"),
        ("1133", "魔法飞弹", "gold"),
        ("1030", "尤里卡", "prismatic"),
        ("1022", "灵巧", "silver"),
        ("1225", "双刀流", "prismatic"),
        ("1328", "暴击律动", "gold"),
        ("1013", "星界躯体", "gold"),
        ("1005", "喂呜喂呜", "gold"),
        ("1037", "急救用具", "silver"),
    ]
    for aid, nm, rar in sample_augs:
        store._augments[aid] = AugmentInfo(aid, nm, rar)
    return store


def test_lucian_big_shot_recommends_jeweled_gamble_and_jackpot(tmp_path: Path):
    from hex_helper.scoring.augment_combo import (
        build_combo_plan,
        evaluate_picker_combos,
        export_combo_coach_context,
    )

    store = _make_combo_test_store(tmp_path)

    # Round 1 (0 augments): Lucian sees Big Shot + Jeweled as a signature gamble route
    opening_plan = build_combo_plan(store, "236", [], ())
    assert any(c.combo_id == "big_shot_jeweled" for c in opening_plan.opening_combos)

    # Round 1 picker: Big Shot appears -> flagged as high-ceiling gamble opener
    r1_offered = [
        (0, store.get_augment("3018")),
        (1, store.get_augment("1013")),
        (2, store.get_augment("1005")),
    ]
    r1_verdict = evaluate_picker_combos(store, "236", [], (), r1_offered)
    assert r1_verdict.best_slot_index == 0
    assert r1_verdict.cards[0].verdict == "gamble"
    assert r1_verdict.cards[0].gamble is True
    assert r1_verdict.cards[0].combo_delta > 0

    # After picking Big Shot (3018): plan strongly recommends chasing Jeweled Gauntlet / Bracelet
    plan_after = build_combo_plan(store, "236", ["3018"], ())
    top_ids = [t.augment_id for t in plan_after.recommended_targets[:3]]
    assert "1048" in top_ids or "3123" in top_ids
    assert any(t.gamble for t in plan_after.recommended_targets)

    # Round 2 picker: Jeweled Gauntlet (1048) appears -> JACKPOT! Tank/support cards -> conflict!
    r2_offered = [
        (0, store.get_augment("1048")),
        (1, store.get_augment("1013")),
        (2, store.get_augment("1005")),
    ]
    r2_verdict = evaluate_picker_combos(store, "236", ["3018"], (), r2_offered)
    assert r2_verdict.best_slot_index == 0
    assert r2_verdict.cards[0].verdict == "jackpot"
    assert r2_verdict.cards[0].combo_delta >= 8.0
    assert r2_verdict.cards[1].verdict == "conflict"
    assert r2_verdict.cards[1].combo_delta < 0

    ctx = export_combo_coach_context(plan_after, r2_verdict)
    assert ctx["direction"]
    assert any("大腕" in c["name"] for c in ctx["active_combos"])
    assert len(ctx["picker_verdicts"]) == 3


def test_ap_miss_fortune_infinite_recursion_downgrades_attack_augments(tmp_path: Path):
    from hex_helper.scoring.augment_combo import (
        build_combo_plan,
        evaluate_picker_combos,
        infer_match_build_profile,
    )

    store = _make_combo_test_store(tmp_path)

    # AP Miss Fortune takes Infinite Recursion (1388)
    prof = infer_match_build_profile(store, "21", ["1388"], ("6653",))
    assert prof.primary_profile == "AP"
    assert "AD_ATTACK" in prof.penalized_axes
    assert "AUTO_ONHIT" in prof.penalized_axes

    plan = build_combo_plan(store, "21", ["1388"], ("6653",))
    rec_ids = [t.augment_id for t in plan.recommended_targets]
    assert "1045" in rec_ids or "1133" in rec_ids
    assert plan.avoid_summary and ("物理" in plan.avoid_summary or "攻速" in plan.avoid_summary)

    # Picker offers AD/AS augments alongside Infernal Conduit (1045)
    offered = [
        (0, store.get_augment("1022")),  # 灵巧 (AD/ONHIT)
        (1, store.get_augment("1225")),  # 双刀流 (AD/ONHIT)
        (2, store.get_augment("1045")),  # 炼狱导管 (AP/BURN/HASTE)
    ]
    verdict = evaluate_picker_combos(store, "21", ["1388"], ("6653",), offered)
    assert verdict.cards[0].verdict == "conflict"
    assert verdict.cards[0].combo_delta <= -3.5
    assert verdict.cards[1].verdict == "conflict"
    assert verdict.cards[1].combo_delta <= -3.5
    assert verdict.cards[2].verdict == "jackpot"
    assert verdict.cards[2].combo_delta >= 8.0
    assert verdict.best_slot_index == 2


def test_inventory_ap_items_alone_shift_profile_and_reroll_advice(tmp_path: Path):
    from hex_helper.scoring.augment_combo import (
        evaluate_picker_combos,
        infer_match_build_profile,
    )

    store = _make_combo_test_store(tmp_path)

    # Miss Fortune buys 2 AP completed items before picking -> inferred as AP, downgrades AD
    prof = infer_match_build_profile(store, "21", [], ("6653", "3089"))
    assert prof.primary_profile == "AP"
    assert "AD_ATTACK" in prof.penalized_axes

    # Lucian holding Big Shot (3018) gets 3 terrible tank/support cards -> triggers reroll advice
    offered_bad = [
        (0, store.get_augment("1013")),
        (1, store.get_augment("1005")),
        (2, store.get_augment("1037")),
    ]
    verdict = evaluate_picker_combos(store, "236", ["3018"], (), offered_bad)
    assert verdict.best_slot_index == -1
    assert verdict.reroll_advice and ("珠光" in verdict.reroll_advice or "骰子" in verdict.reroll_advice)


def test_all_237_active_augments_infer_axes_without_error(tmp_path: Path):
    from hex_helper.scoring.augment_combo import infer_augment_axes

    ds_dir = _cached_2623_dataset_dir()
    if ds_dir is None:
        return
    active_augs = json.loads((ds_dir / "augments.json").read_text(encoding="utf-8"))
    store = _make_combo_test_store(tmp_path)
    from hex_helper.data.provider import AugmentInfo

    for aid, info in active_augs.items():
        store._augments[aid] = AugmentInfo(aid, info["name"], info["rarity"])

    covered = 0
    for aid in active_augs:
        axes = infer_augment_axes(store, aid)
        assert isinstance(axes, tuple)
        for ax in axes:
            assert ax in VALID_MECHANIC_AXES
        if axes:
            covered += 1
    assert covered >= int(len(active_augs) * 0.80)
