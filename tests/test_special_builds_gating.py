"""G8 tests: P1-1 Tank Engine (1353) & Steel Your Heart (1152) data-driven Heartsteel gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.special_builds import BUNDLED_DB_PATH, SpecialAugmentDatabase, get_special_augment_db
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.matchup_optimizer import (
    build_next_buy_plan,
    infer_unified_hero_profile,
    optimize_next_items,
)

LIVE_CACHE_ROOT = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Administrator\AppData\Local")) / "HexHelper" / "cache"
LIVE_1623_NORMAL = LIVE_CACHE_ROOT / "datasets" / "16.23" / "normal"

# G8 benchmark heroes on real 16.23/normal
GATE_PASS_HEROES = (
    ("54", "墨菲特"),
    ("266", "亚托克斯"),
    ("20", "努努和威朗普"),
    ("254", "蔚"),
    ("223", "塔姆"),
    ("36", "蒙多医生"),
)
GATE_FAIL_HEROES = (
    ("33", "拉莫斯", "TANK"),
    ("526", "芮尔", "TANK"),
    ("78", "波比", "TANK"),
    ("8", "弗拉基米尔", "AP"),
    ("517", "塞拉斯", "AP"),
    ("164", "卡蜜尔", "AD"),
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_live_1623_store() -> DataStore:
    static = StaticAssetsManager(LIVE_CACHE_ROOT)
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor(
            patch="16.23",
            mode="normal",
            mode_label="常规",
            path="patches/16.23/normal",
            snapshot_at="test",
            bands=False,
        ),
        LIVE_1623_NORMAL,
    )
    return store


def test_special_augment_json_gate_config_and_no_static_tank_only() -> None:
    raw = json.loads(BUNDLED_DB_PATH.read_text(encoding="utf-8"))
    by_id = {str(e["id"]): e for e in raw.get("archetypes", []) if isinstance(e, dict)}
    for aid in ("1353", "1152"):
        assert aid in by_id
        entry = by_id[aid]
        assert "apply_only_profiles" not in entry, f"{aid} must NOT use static apply_only_profiles"
        gate = entry.get("force_item_gate")
        assert isinstance(gate, dict)
        assert gate["item_id"] == "3084"
        assert gate["min_pr"] == pytest.approx(0.08, abs=1e-6)
        assert gate["min_delta"] == pytest.approx(-0.012, abs=1e-6)

    db = get_special_augment_db()
    for aid in ("1353", "1152"):
        sp = db.get_by_id(aid)
        assert sp is not None
        assert sp.force_item_gate_dict == {"item_id": "3084", "min_pr": 0.08, "min_delta": -0.012}
        assert sp.gate_item_id == "3084"
        assert sp.gate_min_pr == pytest.approx(0.08, abs=1e-6)
        assert sp.gate_min_delta == pytest.approx(-0.012, abs=1e-6)


def test_special_build_apply_hero_gate_synthetic_boundaries_and_coach_context(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900}, "", "complete_equipment"),
        "3083": ItemStaticInfo("3083", "狂徒铠甲", 3100, ("Health",), {"FlatHPPoolMod": 1000}, "", "complete_equipment"),
        "4633": ItemStaticInfo("4633", "峡谷制造者", 3100, ("SpellDamage", "Health"), {"FlatMagicDamageMod": 70, "FlatHPPoolMod": 350}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "ds_gate"
    _write_json(
        ds_dir / "heroes.json",
        {
            "100": {"name": "通过者", "title": "测试", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 20},
            "101": {"name": "低选取", "title": "测试", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 30},
            "102": {"name": "负胜率", "title": "测试", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 40},
        },
    )
    _write_json(ds_dir / "augments.json", {"1353": {"name": "坦克引擎", "rarity": "gold"}, "1152": {"name": "钢化你心", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {"100": {}, "101": {}, "102": {}})
    _write_json(
        ds_dir / "core_builds.json",
        {
            "100": [{"items": ["4633", "3089"], "ordered": True, "games": 20000, "wins": 10000, "win": 0.50, "pick": 0.2}],
            "101": [{"items": ["4633", "3089"], "ordered": True, "games": 20000, "wins": 10000, "win": 0.50, "pick": 0.2}],
            "102": [{"items": ["4633", "3089"], "ordered": True, "games": 20000, "wins": 10000, "win": 0.50, "pick": 0.2}],
        },
    )
    _write_json(
        ds_dir / "hero_item_recommendations.json",
        {
            # Hero 100: PR = 8.0% (>= 8%), delta = -1.2% (>= -1.2%) -> PASS
            "100": {"3084": {"games": 8000, "wins": 3904, "win_rate": 0.488, "low_sample": False}},
            # Hero 101: PR = 7.9% (< 8%), delta = +2.0% -> FAIL
            "101": {"3084": {"games": 7900, "wins": 4108, "win_rate": 0.520, "low_sample": False}},
            # Hero 102: PR = 30.0%, delta = -1.3% (< -1.2%) -> FAIL
            "102": {"3084": {"games": 30000, "wins": 14610, "win_rate": 0.487, "low_sample": False}},
        },
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    db = SpecialAugmentDatabase(bundled_path=BUNDLED_DB_PATH, user_override_path=tmp_path / "none.json")

    for aid in ("1353", "1152"):
        # Backward compatible without hero_id or store
        raw_sp = db.resolve_for_hero(store, aid, None)
        assert raw_sp is not None and raw_sp.shifts_profile == "TANK" and raw_sp.force_items == ("3084",)
        assert raw_sp.apply_hero_gate(None, "101") == raw_sp

        # Pass boundary
        sp_pass = db.resolve_for_hero(store, aid, "100")
        assert sp_pass is not None
        assert sp_pass.shifts_profile == "TANK"
        assert sp_pass.force_items == ("3084",)
        assert sp_pass.allow_off_profile is True

        # Fail by PR or delta
        for fail_hid in ("101", "102"):
            sp_fail = db.resolve_for_hero(store, aid, fail_hid)
            assert sp_fail is not None
            assert sp_fail.transformative is False
            assert sp_fail.shifts_profile == ""
            assert sp_fail.force_items == ()
            assert sp_fail.allow_off_profile is False
            assert "3084" not in sp_fail.core_items
            assert "3083" in sp_fail.core_items
            assert sp_fail.boost <= 1.5

        from hex_helper.scoring.build_synergy import is_transformative_augment

        assert is_transformative_augment(store, aid) is True  # Legacy 2-arg backward compatibility
        assert is_transformative_augment(store, aid, hero_id="100") is True
        assert is_transformative_augment(store, aid, hero_id="101") is False
        assert is_transformative_augment(store, aid, hero_id="102") is False

    # export_coach_context respects hero_id when supplied and remains backward compatible when omitted
    ctx_legacy = db.export_coach_context(store, ["1353"])
    assert any("3084" in x for x in ctx_legacy[0]["force_items"])
    ctx_pass = db.export_coach_context(store, ["1353"], hero_id="100")
    assert any("3084" in x for x in ctx_pass[0]["force_items"])
    ctx_fail = db.export_coach_context(store, ["1353"], hero_id="102")
    assert ctx_fail[0]["force_items"] == []
    assert all("3084" not in x for x in ctx_fail[0]["core_items"])


@pytest.mark.skipif(not (LIVE_1623_NORMAL / "heroes.json").is_file(), reason="Real 16.23/normal dataset not present locally")
def test_g8_real_1623_tank_engine_and_steel_your_heart_pass_and_fail_heroes() -> None:
    store = _load_live_1623_store()
    db = get_special_augment_db()

    # 1. Passing heroes: Malphite, Aatrox, Nunu, Vi, Tahm Kench, Dr. Mundo -> ACTIVATE Heartsteel & TANK shift
    for aid in ("1353", "1152"):
        for hid, cname in GATE_PASS_HEROES:
            resolved = db.resolve_for_hero(store, aid, hid)
            assert resolved is not None, f"Failed to resolve {aid}"
            assert "3084" in resolved.force_items, f"{aid} should force 3084 for {cname}({hid})"
            assert resolved.shifts_profile == "TANK", f"{aid} should shift profile to TANK for {cname}({hid})"
            assert resolved.allow_off_profile is True

            prof = infer_unified_hero_profile(hid, store, selected_augments=[aid])
            assert prof == "TANK", f"{cname}({hid}) with {aid} expected profile TANK, got {prof}"

            _, recs = optimize_next_items(
                store, hid, (), 3000.0, [aid], (), (), limit=4, collect_breakdown=True
            )
            assert recs[0].item_id == "3084", (
                f"{cname}({hid}) with {aid} expected Heartsteel (3084) #1, got {[(r.item_id, r.name, r.score) for r in recs]}"
            )
            assert recs[0].score_breakdown["augment_synergy"] >= 7.0

    # 2. Failing heroes: Rammus, Rell, Poppy, Vladimir, Sylas, Camille -> DO NOT force Heartsteel or shift profile
    for aid in ("1353", "1152"):
        for hid, cname, expected_prof in GATE_FAIL_HEROES:
            resolved = db.resolve_for_hero(store, aid, hid)
            assert resolved is not None
            assert resolved.force_items == (), f"{aid} must NOT force 3084 for {cname}({hid})"
            assert resolved.shifts_profile == "", f"{aid} must NOT shift profile for {cname}({hid})"
            assert resolved.allow_off_profile is False
            assert "3084" not in resolved.core_items
            assert resolved.boost <= 1.5

            prof = infer_unified_hero_profile(hid, store, selected_augments=[aid])
            assert prof == expected_prof, f"{cname}({hid}) with {aid} should keep native {expected_prof}, got {prof}"

            _, base_recs = optimize_next_items(
                store, hid, (), 3000.0, (), (), (), limit=6, collect_breakdown=True
            )
            thr, recs = optimize_next_items(
                store, hid, (), 3000.0, [aid], (), (), limit=6, collect_breakdown=True
            )
            if expected_prof != "TANK":
                assert "3084" not in [r.item_id for r in recs[:4]], (
                    f"Non-tank {cname}({hid}) with gated-off {aid} must NOT include 3084 in Top 4"
                )
            else:
                # Even if a tank's own base core_builds lists 3084, gated-off 1353/1152 MUST add 0.0 synergy to 3084
                rec_3084 = next((r for r in recs if r.item_id == "3084"), None)
                if rec_3084 is not None:
                    assert rec_3084.score_breakdown["augment_synergy"] == 0.0
                    assert not any("专属流派核心" in t for t in rec_3084.tags)
                    base_3084 = next((r for r in base_recs if r.item_id == "3084"), None)
                    if base_3084 is not None:
                        assert rec_3084.score <= base_3084.score
            plan = build_next_buy_plan(store, hid, (), 3000.0, recs[:4], threat=thr, selected_augments=[aid])
            assert plan is not None
            assert plan.pivot_banner_text == "", f"{cname}({hid}) with gated-off {aid} emitted false pivot banner: {plan.pivot_banner_text}"

    # 3. Verify is_transformative_augment & controller plan lock / smart gate respect hero gating on real 16.23 data
    from hex_helper.app import HexHelperController, HexStateMachine
    from hex_helper.config import AppConfig
    from hex_helper.scoring.build_synergy import is_transformative_augment
    from hex_helper.scoring.tier import AugmentScore
    from hex_helper.vision.detector import DetectedCard

    for aid in ("1353", "1152"):
        for hid, cname in GATE_PASS_HEROES:
            assert is_transformative_augment(store, aid, hero_id=hid) is True, f"{cname}({hid}) should treat {aid} as transformative"
        for hid, cname, _ in GATE_FAIL_HEROES:
            assert is_transformative_augment(store, aid, hero_id=hid) is False, f"{cname}({hid}) must NOT treat gated-off {aid} as transformative"

    class _DummyDrawer:
        def set_coach_advice(self, _adv) -> None:
            return None

    # Vladimir (8) picking 1353 MUST NOT invalidate committed plan or wipe >=800G locked_next_item
    ctrl_vlad = HexHelperController.__new__(HexHelperController)
    ctrl_vlad.cfg = AppConfig(smart_picker_ai_gate=True)
    ctrl_vlad.store = store
    ctrl_vlad.sm = HexStateMachine()
    ctrl_vlad.sm.current_hero_id = "8"
    ctrl_vlad.sm.committed_plan = ["3152", "3089"]
    ctrl_vlad.sm.locked_next_item = "3152"
    ctrl_vlad.sm.selected_augments = ["1353"]
    ctrl_vlad.edge_drawer = _DummyDrawer()
    ctrl_vlad._latest_advice = object()
    ctrl_vlad._latest_request_id = "keep-me"
    ctrl_vlad._last_drawer_refresh_sig = "sig"
    ctrl_vlad._last_live = None
    ctrl_vlad._last_combo_verdict = None
    ctrl_vlad._flag_if_transformative_augment("1353")
    assert ctrl_vlad.sm.locked_next_item == "3152"
    assert ctrl_vlad.sm.committed_plan == ["3152", "3089"]
    assert ctrl_vlad.sm.plan_overhaul_reason == ""

    # Smart picker gate on Vladimir (8) MUST NOT trigger false transformative_tie when 1353 is offered against a decisive winner
    aug_win = store.get_augment("1103")  # 面包和黄油 (non-transformative gold)
    aug_1353 = store.get_augment("1353")
    aug_low = store.get_augment("1133")  # 魔法飞弹 (non-transformative gold)
    assert aug_win is not None and aug_1353 is not None and aug_low is not None
    cards = (
        DetectedCard(0, aug_win, 0.99, (300, 200, 500, 300), aug_win.rarity),
        DetectedCard(1, aug_1353, 0.99, (700, 200, 900, 300), aug_1353.rarity),
        DetectedCard(2, aug_low, 0.99, (1100, 200, 1300, 300), aug_low.rarity),
    )
    fake_scored = [
        (cards[0], AugmentScore(aug_win.augment_id, aug_win.name, aug_win.rarity, "S+", 0.56, 0.555, 0.055, 5000, False, percentile=98.0)),
        (cards[1], AugmentScore("1353", aug_1353.name, aug_1353.rarity, "B", 0.525, 0.525, 0.025, 5000, False, percentile=60.0)),
        (cards[2], AugmentScore(aug_low.augment_id, aug_low.name, aug_low.rarity, "C", 0.49, 0.49, -0.01, 5000, False, percentile=30.0)),
    ]
    ctrl_vlad.sm.selected_augments = ["1022"]
    should_skip, _ = ctrl_vlad._should_skip_picker_ai_call(cards, scored_cards=fake_scored)
    assert should_skip is True, f"Expected decisive skip on Vladimir with gated-off 1353, got {ctrl_vlad._last_smart_gate_eval}"
    assert ctrl_vlad._last_smart_gate_eval["reason"] == "decisive_local_winner"

    # Matchup optimizer keeps hero signature tie-breakers active on Vladimir (8) holding gated-off 1353
    _, vlad_recs_1353 = optimize_next_items(store, "8", (), 3000.0, ["1353"], (), (), limit=8, collect_breakdown=True)
    assert any(r.score_breakdown.get("hero_signature", 0.0) > 0.0 for r in vlad_recs_1353), (
        "Hero signature tie-breakers were wrongly disabled on Vladimir holding gated-off 1353"
    )

    # Mundo (36) picking 1353 DOES invalidate committed plan with '海克斯变阵：坦克引擎'
    ctrl_mundo = HexHelperController.__new__(HexHelperController)
    ctrl_mundo.cfg = AppConfig()
    ctrl_mundo.store = store
    ctrl_mundo.sm = HexStateMachine()
    ctrl_mundo.sm.current_hero_id = "36"
    ctrl_mundo.sm.committed_plan = ["3083", "3065"]
    ctrl_mundo.sm.locked_next_item = "3083"
    ctrl_mundo.sm.selected_augments = ["1353"]
    ctrl_mundo.edge_drawer = _DummyDrawer()
    ctrl_mundo._latest_advice = object()
    ctrl_mundo._latest_request_id = "old-mundo"
    ctrl_mundo._flag_if_transformative_augment("1353")
    assert ctrl_mundo.sm.locked_next_item == ""
    assert ctrl_mundo.sm.committed_plan == []
    assert "坦克引擎" in ctrl_mundo.sm.plan_overhaul_reason
