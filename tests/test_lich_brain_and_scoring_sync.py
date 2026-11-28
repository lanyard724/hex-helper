"""Tests for 66xxxx explicit item aliases, augment_combo profile passthrough, and 3094 Lich Brain DB."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hex_helper.data.augment_combos import get_augment_combo_db
from hex_helper.data.provider import AugmentInfo, DatasetDescriptor, ItemStaticInfo
from hex_helper.data.special_builds import BUNDLED_DB_PATH, SpecialAugmentBuild, get_special_augment_db
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import (
    _MAYHEM_ITEM_PREFIXES,
    SIX_DIGIT_ITEM_ALIASES,
    DataStore,
    _canonical_item_key,
)
from hex_helper.scoring.augment_combo import (
    build_combo_plan,
    evaluate_picker_combos,
    infer_match_build_profile,
)
from hex_helper.scoring.build_synergy import item_is_owned, normalize_item_id
from hex_helper.scoring.matchup_optimizer import (
    _item_in_static_catalog,
    build_next_buy_plan,
    canonicalize_item_id,
    infer_unified_hero_profile,
    is_valid_plan_item,
    optimize_next_items,
    reconcile_match_build_plan,
    reconcile_match_build_plan_with_trace,
)

LIVE_CACHE_ROOT = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Administrator\AppData\Local")) / "HexHelper" / "cache"
LIVE_1623_NORMAL = LIVE_CACHE_ROOT / "datasets" / "16.23" / "normal"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. SIX_DIGIT_ITEM_ALIASES & 663172 Zephyr vs 3172 Gunmetal Greaves isolation
# ---------------------------------------------------------------------------


def test_six_digit_aliases_and_mayhem_prefix_boundary() -> None:
    assert _MAYHEM_ITEM_PREFIXES == ("12", "22", "32", "44", "77", "99")
    assert "66" not in _MAYHEM_ITEM_PREFIXES
    assert SIX_DIGIT_ITEM_ALIASES == {
        "663039": "3039",
        "663146": "3146",
        "663193": "3193",
        "664011": "4011",
        "664403": "4403",
        "664644": "4644",
        "667666": "6676",
    }

    for six_id, canon_id in SIX_DIGIT_ITEM_ALIASES.items():
        assert _canonical_item_key(six_id) == canon_id
        assert normalize_item_id(six_id) == canon_id
        assert canonicalize_item_id(six_id) == canon_id
        assert item_is_owned(canon_id, (six_id,)) is True

    # Unaliased 66xxxx items (especially 663172 灵风 vs 3172 炮铜胫甲) MUST remain untouched!
    assert _canonical_item_key("663172") == "663172"
    assert normalize_item_id("663172") == "663172"
    assert canonicalize_item_id("663172") == "663172"
    assert item_is_owned("3172", ("663172",)) is False
    assert item_is_owned("663172", ("3172",)) is False

    # Existing Mayhem prefixes + Tear normalization stay intact
    assert normalize_item_id("773111") == "3111"
    assert normalize_item_id("124011") == "4011"
    assert normalize_item_id("3040") == "3003"


def test_store_get_item_info_and_catalog_dedup_for_six_digit_aliases(tmp_path: Path) -> None:
    # Case A: Static catalog has 4-digit 4011/6676/3172 + 6-digit 663172 (without 664011/667666)
    static_a = StaticAssetsManager(cache_dir=tmp_path / "a", fetcher=lambda u: b"{}")
    static_a.items = {
        "4011": ItemStaticInfo("4011", "花晓之剑", 2500, ("Health", "SpellDamage"), {"FlatMagicDamageMod": 45, "FlatHPPoolMod": 200}, "", "complete_equipment"),
        "6676": ItemStaticInfo("6676", "收集者", 3000, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 50, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "663172": ItemStaticInfo("663172", "灵风", 2500, ("AttackSpeed", "Tenacity"), {"PercentAttackSpeedMod": 0.40}, "", "complete_equipment"),
        "3172": ItemStaticInfo("3172", "炮铜胫甲", 1100, ("AttackSpeed", "LifeSteal"), {"FlatMovementSpeedMod": 45}, "", "other"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
    }
    store_a = DataStore(static_a)

    info_4011 = store_a.get_item_info("664011")
    assert info_4011.item_id == "4011"
    assert info_4011.name == "花晓之剑"
    assert info_4011.category == "complete_equipment"

    info_6676 = store_a.get_item_info("667666")
    assert info_6676.item_id == "6676"
    assert info_6676.name == "收集者"
    assert info_6676.category == "complete_equipment"

    info_zephyr = store_a.get_item_info("663172")
    assert info_zephyr.item_id == "663172"
    assert info_zephyr.name == "灵风"

    assert _item_in_static_catalog(store_a, "664011") is True
    assert _item_in_static_catalog(store_a, "667666") is True
    assert _item_in_static_catalog(store_a, "663172") is True
    assert is_valid_plan_item(store_a, "664011") is True
    assert is_valid_plan_item(store_a, "667666") is True
    assert is_valid_plan_item(store_a, "663172") is True
    assert is_valid_plan_item(store_a, "3172") is False

    # Routing dedup: 664011 + 4011 collapse to 4011; 667666 + 6676 collapse to 6676; 663172 stays 663172
    lock, plan = reconcile_match_build_plan(
        store_a,
        my_items=(),
        committed_plan=["664011", "667666", "663172"],
        locked_next_item="664011",
        proposed_ids=["4011", "6676", "3172", "3089"],
    )
    assert lock == "4011"
    assert plan == ["4011", "6676", "663172", "3089"]

    lock_t, plan_t, _ = reconcile_match_build_plan_with_trace(
        store_a,
        my_items=("664011",),
        committed_plan=["4011", "667666", "663172"],
        locked_next_item="4011",
        proposed_ids=["664011", "6676", "3089"],
    )
    assert lock_t == "6676"
    assert plan_t == ["6676", "663172", "3089"]

    # Case B: Static catalog has only 6-digit 664011/667666 and lacks 663172 (only 3172 present)
    static_b = StaticAssetsManager(cache_dir=tmp_path / "b", fetcher=lambda u: b"{}")
    static_b.items = {
        "664011": ItemStaticInfo("664011", "花晓之剑", 2500, ("Health", "SpellDamage"), {"FlatMagicDamageMod": 35}, "", "complete_equipment"),
        "667666": ItemStaticInfo("667666", "收集者", 3000, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 50}, "", "complete_equipment"),
        "3172": ItemStaticInfo("3172", "炮铜胫甲", 1100, ("AttackSpeed", "LifeSteal"), {"FlatMovementSpeedMod": 45}, "", "other"),
    }
    store_b = DataStore(static_b)
    assert store_b.get_item_info("4011").name == "花晓之剑"
    assert store_b.get_item_info("6676").name == "收集者"
    assert store_b.get_item_info("664011").item_id == "4011"
    assert store_b.get_item_info("667666").item_id == "6676"
    # Missing 663172 MUST NOT silently fall back to 3172 炮铜胫甲!
    assert store_b.get_item_info("663172").name == "装备#663172"
    assert _item_in_static_catalog(store_b, "4011") is True
    assert _item_in_static_catalog(store_b, "6676") is True
    assert _item_in_static_catalog(store_b, "663172") is False

    # Case C: build_next_buy_plan deduplicates 664011 & 4011 in remaining_build_ids and special gate handles 664011
    ds_dir = tmp_path / "ds"
    _write_json(
        ds_dir / "heroes.json",
        {"37": {"name": "娑娜", "title": "琴瑟仙女", "games": 50000, "wins": 26000, "win_rate": 0.52, "pick_rate": 0.04, "rank": 20}},
    )
    _write_json(ds_dir / "augments.json", {"3163": {"name": "迷人蝴蝶", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {"37": {"3163": {"games": 2000, "wins": 1100, "win_rate": 0.55, "low_sample": False}}})
    _write_json(
        ds_dir / "core_builds.json",
        {"37": [{"items": ["664011", "663172", "3089"], "ordered": True, "games": 10000, "wins": 5400, "win": 0.54, "pick": 0.25}]},
    )
    _write_json(
        ds_dir / "hero_item_recommendations.json",
        {
            "37": {
                "664011": {"games": 5000, "wins": 2700, "win_rate": 0.54, "low_sample": False},
                "4011": {"games": 5000, "wins": 2800, "win_rate": 0.56, "low_sample": False},
                "663172": {"games": 3000, "wins": 1650, "win_rate": 0.55, "low_sample": False},
            }
        },
    )
    store_c = DataStore(static_a)
    store_c.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    _, recs = optimize_next_items(store_c, "37", (), 3000.0, (), (), (), limit=4)
    rec_ids = [r.item_id for r in recs]
    assert "4011" in rec_ids and "664011" not in rec_ids
    assert "663172" in rec_ids and "3172" not in rec_ids
    plan_c = build_next_buy_plan(store_c, "37", (), 3000.0, recs)
    assert plan_c is not None
    assert plan_c.remaining_build_ids.count("4011") == 1
    assert "663172" in plan_c.remaining_build_ids

    # Special gate handles 664011 -> 4011 in hero_item_recommendations
    gated_sp = SpecialAugmentBuild(
        augment_id="99999",
        name="测试门禁",
        aliases=(),
        rarity="gold",
        archetype="测试",
        mechanic="测试",
        transformative=True,
        allow_off_profile=True,
        shifts_profile="SUPPORT",
        boost=3.0,
        core_items=("4011",),
        force_items=("4011",),
        required_tags=(),
        stat_keys=(),
        penalize_tags=(),
        avoid_note="",
        playstyle_tip="",
        force_item_gate=(("item_id", "4011"), ("min_pr", 0.08), ("min_delta", -0.01)),
    )
    assert gated_sp.apply_hero_gate(store_c, "37").force_items == ("4011",)


# ---------------------------------------------------------------------------
# 2. augment_combo.py my_stats + data_band_mode passthrough eliminates conflict
# ---------------------------------------------------------------------------


def test_augment_combo_forwards_my_stats_and_data_band_mode_to_unified_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hex_helper.scoring.augment_combo as ac

    static = StaticAssetsManager(cache_dir=tmp_path / "static", fetcher=lambda u: b"{}")
    static.items = {
        "3031": ItemStaticInfo("3031", "无尽之刃", 3400, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3036": ItemStaticInfo("3036", "多米尼克领主的致意", 3000, ("Damage", "ArmorPenetration"), {"FlatPhysicalDamageMod": 35, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "4633": ItemStaticInfo("4633", "裂隙制造者", 3100, ("SpellDamage", "Health"), {"FlatMagicDamageMod": 70, "FlatHPPoolMod": 350}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_band"
    _write_json(
        ds_dir / "heroes.json",
        {"105": {"name": "菲兹", "title": "潮汐海灵", "games": 50000, "wins": 25000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 30}},
    )
    _write_json(
        ds_dir / "augments.json",
        {
            "1068": {"name": "循环往复", "rarity": "gold"},
            "1013": {"name": "星界躯体", "rarity": "gold"},
            "1022": {"name": "灵巧", "rarity": "silver"},
        },
    )
    _write_json(ds_dir / "hero_augments.json", {"105": {"1068": {"games": 1000, "wins": 550, "win_rate": 0.55, "low_sample": False}}})
    # All-band defaults to AD, whereas High-band plays AP!
    _write_json(
        ds_dir / "core_builds.json",
        {"105": [{"items": ["3031", "3036"], "ordered": True, "games": 10000, "wins": 5000, "win": 0.50, "pick": 0.3}]},
    )
    _write_json(
        ds_dir / "hero_item_recommendations.json",
        {"105": {"3031": {"games": 5000, "wins": 2500, "win_rate": 0.50, "low_sample": False}}},
    )
    _write_json(
        ds_dir / "bands" / "high" / "heroes.json",
        {"105": {"name": "菲兹", "title": "潮汐海灵", "games": 5000, "wins": 2700, "win_rate": 0.54, "pick_rate": 0.05, "rank": 10}},
    )
    _write_json(
        ds_dir / "bands" / "high" / "hero_augments.json",
        {"105": {"1068": {"games": 500, "wins": 290, "win_rate": 0.58, "low_sample": False}}},
    )
    _write_json(
        ds_dir / "bands" / "high" / "core_builds.json",
        {"105": [{"items": ["4633", "3089"], "ordered": True, "games": 2000, "wins": 1120, "win": 0.56, "pick": 0.4}]},
    )
    _write_json(
        ds_dir / "bands" / "high" / "hero_item_recommendations.json",
        {"105": {"4633": {"games": 1200, "wins": 672, "win_rate": 0.56, "low_sample": False}}},
    )

    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", True), ds_dir)

    calls: list[dict[str, object]] = []
    orig_infer = ac.infer_unified_hero_profile

    def _spy_infer(*args, **kwargs):
        calls.append({"args": args, "kwargs": dict(kwargs)})
        return orig_infer(*args, **kwargs)

    monkeypatch.setattr(ac, "infer_unified_hero_profile", _spy_infer)

    live_stats = {"abilityPower": 420.0, "attackDamage": 65.0}
    offered = [(0, store.get_augment("1068")), (1, store.get_augment("1022"))]

    prof_hi = infer_match_build_profile(store, "105", (), (), my_stats=live_stats, data_band_mode="high")
    plan_hi = build_combo_plan(store, "105", (), (), my_stats=live_stats, data_band_mode="high")
    verd_hi = evaluate_picker_combos(store, "105", (), (), offered, my_stats=live_stats, data_band_mode="high")
    right_hi = infer_unified_hero_profile("105", store, my_stats=live_stats, data_band_mode="high")

    assert len(calls) == 3
    for c in calls:
        assert c["kwargs"]["my_stats"] == live_stats
        assert c["kwargs"]["data_band_mode"] == "high"

    assert right_hi == "AP"
    assert prof_hi.primary_profile == right_hi
    assert plan_hi.profile.primary_profile == right_hi
    assert verd_hi.profile.primary_profile == right_hi

    # Without my_stats: high band still separates AP (high) vs AD (all), matching right-hand optimizer!
    assert infer_unified_hero_profile("105", store, data_band_mode="all") == "AD"
    assert build_combo_plan(store, "105", (), (), data_band_mode="all").profile.primary_profile == "AD"
    assert build_combo_plan(store, "105", (), (), data_band_mode="high").profile.primary_profile == "AP"
    assert evaluate_picker_combos(store, "105", (), (), offered, data_band_mode="high").profile.primary_profile == "AP"

    # Live stats override alone aligns left & right when my_stats flips AP -> AD
    ad_stats = {"attackDamage": 850.0, "abilityPower": 0.0}
    assert infer_unified_hero_profile("105", store, my_stats=ad_stats, data_band_mode="high") == "AD"
    assert build_combo_plan(store, "105", (), (), my_stats=ad_stats, data_band_mode="high").profile.primary_profile == "AD"
    assert evaluate_picker_combos(store, "105", (), (), offered, my_stats=ad_stats, data_band_mode="high").profile.primary_profile == "AD"


# ---------------------------------------------------------------------------
# 3. 3094 巫妖大脑 (Lich Brain) special_augment_builds.json & augment_combos.json
# ---------------------------------------------------------------------------


def test_3094_lich_brain_assets_schema_and_combo_cleanup() -> None:
    sp_raw = json.loads(BUNDLED_DB_PATH.read_text(encoding="utf-8"))
    by_id = {str(e["id"]): e for e in sp_raw["archetypes"] if isinstance(e, dict)}
    assert "3094" in by_id
    raw_3094 = by_id["3094"]
    assert raw_3094["name"] == "巫妖大脑"
    assert raw_3094["rarity"] == "silver"
    assert raw_3094["transformative"] is False
    assert raw_3094["allow_off_profile"] is False
    assert raw_3094["shifts_profile"] == ""
    assert raw_3094["boost"] == pytest.approx(2.8)
    assert raw_3094["core_items"] == ["4633", "3116", "6653", "6657", "4629", "3065", "3084", "2502", "3083"]
    assert raw_3094["force_items"] == []
    assert raw_3094["stat_keys"] == ["FlatHPPoolMod", "FlatMagicDamageMod"]

    sp_db = get_special_augment_db()
    sp_3094 = sp_db.get_by_id("3094")
    assert sp_3094 is not None
    assert sp_3094.name == "巫妖大脑"
    assert sp_db.get_by_name("Lich Brain") == sp_3094
    assert sp_db.get_by_name("血量转法强") == sp_3094

    cdb = get_augment_combo_db()
    # 3094 removed from on-hit / AP-auto traps
    eth = cdb.get_by_id("ethereal_weapon_onhit_mage")
    mks = cdb.get_by_id("marksmage_ap_auto")
    assert eth is not None and "3094" not in eth.core_augments and "3094" not in eth.follow_ups
    assert mks is not None and "3094" not in mks.core_augments and "3094" not in mks.follow_ups
    assert "巫妖大脑" not in mks.synergy_reason

    # 3094 added to tank_engine_steel_heart follow_ups
    tank_rule = cdb.get_by_id("tank_engine_steel_heart")
    assert tank_rule is not None and "3094" in tank_rule.follow_ups

    # Axes fixed from AUTO_ONHIT to ("AP_SPELL", "TANK_HP")
    assert cdb.get_axes("3094") == ("AP_SPELL", "TANK_HP")

    # New lich_brain_hp_ap_bruiser combo
    lb_rule = cdb.get_by_id("lich_brain_hp_ap_bruiser")
    assert lb_rule is not None
    assert lb_rule.required_anchor == "3094"
    assert "3094" in lb_rule.core_augments
    assert {"1041", "1152"}.issubset(set(lb_rule.core_augments))
    assert {"1353", "1018", "3262"}.issubset(set(lb_rule.follow_ups))
    for hid in ("82", "8", "50", "223", "13", "3"):
        assert hid in lb_rule.hero_bonus


@pytest.mark.skipif(not (LIVE_1623_NORMAL / "heroes.json").is_file(), reason="Real 16.23/normal dataset not present locally")
def test_3094_lich_brain_real_1623_item_and_combo_behavior() -> None:
    static = StaticAssetsManager(LIVE_CACHE_ROOT)
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )

    for hid in ("82", "8", "50", "223", "13", "3"):
        # Item recs: HP+AP / HP+Tank items receive positive augment_synergy from 3094
        _, recs = optimize_next_items(store, hid, (), 3100.0, ["3094"], (), (), limit=5, collect_breakdown=True)
        assert any(
            r.item_id in ("4633", "3116", "6653", "6657", "4629", "3065", "3084", "2502", "3083")
            and r.score_breakdown.get("augment_synergy", 0.0) >= 2.0
            for r in recs[:3]
        ), f"Expected 3094 synergy item in top 3 for hero {hid}, got {recs}"

        # Combo plan with 3094 activates lich_brain_hp_ap_bruiser and keeps targets strictly eligible
        plan = build_combo_plan(store, hid, ["3094"], ())
        assert any(c.combo_id == "lich_brain_hp_ap_bruiser" for c in plan.active_combos)
        target_ids = {t.augment_id for t in plan.recommended_targets}
        assert all(store.is_augment_eligible_for_hero(hid, tid) for tid in target_ids)
        if hid == "13":
            assert "1353" not in target_ids and "1018" not in target_ids

        # Picker: 1041 Goliath -> jackpot; 3262 Slimetime -> synergy; 1047 Crit -> conflict
        verdict = evaluate_picker_combos(
            store,
            hid,
            ["3094"],
            (),
            [
                (0, store.get_augment("1041")),
                (1, store.get_augment("3262")),
                (2, store.get_augment("1047")),
            ],
        )
        assert verdict.cards[0].verdict == "jackpot"
        assert verdict.cards[1].verdict == "synergy"
        assert verdict.cards[2].verdict == "conflict"

    # Holding 1353 (Tank Engine) treats 3094 (Lich Brain) as synergy follow-up
    v_1353 = evaluate_picker_combos(
        store,
        "223",
        ["1353"],
        (),
        [(0, store.get_augment("3094")), (1, store.get_augment("1047"))],
    )
    assert v_1353.cards[0].verdict == "synergy"
    assert v_1353.cards[0].combo_delta >= 6.0
