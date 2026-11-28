"""G2 tests: P0-2 unilateral de-bias + confounding-by-indication companion clamp & P0-3 high-band integration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import TEAR_ALL_IDS, DataStore
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.matchup_optimizer import optimize_next_items

LIVE_CACHE_ROOT = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Administrator\AppData\Local")) / "HexHelper" / "cache"
LIVE_1623_NORMAL = LIVE_CACHE_ROOT / "datasets" / "16.23" / "normal"
LIVE_1622_CANDIDATES = (
    Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\aramhex_16_22_normal"),
    Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\aramhex_16_22"),
)


def _find_live_1622_dir() -> Path | None:
    for cand in LIVE_1622_CANDIDATES:
        if (cand / "heroes.json").is_file() and (cand / "bands" / "high" / "hero_item_recommendations.json").is_file():
            return cand
    return None


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_live_store(data_dir: Path, patch: str, bands: bool = False) -> DataStore:
    static = StaticAssetsManager(LIVE_CACHE_ROOT)
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor(
            patch=patch,
            mode="normal",
            mode_label="常规",
            path=f"patches/{patch}/normal",
            snapshot_at="test",
            bands=bands,
        ),
        data_dir,
    )
    return store


def _mk_enemy(name: str, items: tuple[str, ...]) -> PlayerLiveInfo:
    return PlayerLiveInfo(
        riot_id=name,
        champion_name=name,
        raw_champion_name=name,
        team="CHAOS",
        is_dead=False,
        level=11,
        kills=5,
        deaths=4,
        assists=8,
        item_ids=items,
        summoner_spells=("闪现", "雪球"),
    )


# ---------------------------------------------------------------------------
# Synthetic unit tests: unilateral de-bias, companion clamp, high-band decay
# ---------------------------------------------------------------------------


def test_unilateral_debias_and_companion_clamp_semantics(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 100}, "", "complete_equipment"),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
        "3165": ItemStaticInfo("3165", "莫雷洛秘典", 2850, ("SpellDamage", "Health"), {"FlatMagicDamageMod": 75, "FlatHPPoolMod": 350}, "重伤", "complete_equipment", has_grievous=True),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Armor", "Health"), {"FlatArmorMod": 75, "FlatHPPoolMod": 150}, "重伤", "complete_equipment", has_grievous=True),
        "6697": ItemStaticInfo("6697", "狂妄", 2800, ("Damage",), {"FlatPhysicalDamageMod": 60}, "", "complete_equipment"),
        "6695": ItemStaticInfo("6695", "巨蛇之牙", 2500, ("Damage",), {"FlatPhysicalDamageMod": 55}, "破盾", "complete_equipment", has_shield_break=True),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3600, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "6676": ItemStaticInfo("6676", "收集者", 3000, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 50, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
    }
    heroes = {
        "99": {"name": "拉克丝", "title": "光辉", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.05, "rank": 20},
        "101": {"name": "泽拉斯", "title": "远古巫灵", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.05, "rank": 25},
        "238": {"name": "劫", "title": "影流之主", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.05, "rank": 30},
        "126": {"name": "杰斯", "title": "未来守护者", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.05, "rank": 35},
    }
    core = {
        "99": [{"items": ["6655", "4645", "3089"], "ordered": True, "games": 30000, "wins": 15300, "win": 0.51, "pick": 0.30}],
        "101": [{"items": ["6655", "4645", "3089"], "ordered": True, "games": 30000, "wins": 15300, "win": 0.51, "pick": 0.30}],
        "238": [{"items": ["6697", "6676", "3031"], "ordered": True, "games": 30000, "wins": 15300, "win": 0.51, "pick": 0.30}],
        "126": [{"items": ["6697", "6676", "3031"], "ordered": True, "games": 30000, "wins": 15300, "win": 0.51, "pick": 0.30}],
    }
    # Mainstream items (PR >= 8% and g >= 1000):
    # - 3089 has +4% positive bias (should be trimmed by -0.65 * 0.04)
    # - 3165 has -5% negative bias (must NOT receive +3.25 free points under unilateral de-bias!)
    # - 6695 has -4% negative bias (must NOT receive +2.60 free points under unilateral de-bias!)
    item_recs = {
        "99": {
            "6655": {"games": 40000, "wins": 20400, "win_rate": 0.51, "low_sample": False},
            "4645": {"games": 35000, "wins": 17850, "win_rate": 0.51, "low_sample": False},
            "3089": {"games": 20000, "wins": 10800, "win_rate": 0.54, "low_sample": False},
            "3165": {"games": 10000, "wins": 4500, "win_rate": 0.45, "low_sample": False},
        },
        "101": {
            "6655": {"games": 40000, "wins": 20400, "win_rate": 0.51, "low_sample": False},
            "3089": {"games": 20000, "wins": 10800, "win_rate": 0.54, "low_sample": False},
            "3165": {"games": 10000, "wins": 4500, "win_rate": 0.45, "low_sample": False},
        },
        "238": {
            "6697": {"games": 40000, "wins": 20400, "win_rate": 0.51, "low_sample": False},
            "6676": {"games": 35000, "wins": 17850, "win_rate": 0.51, "low_sample": False},
            "3031": {"games": 25000, "wins": 12750, "win_rate": 0.51, "low_sample": False},
            "6695": {"games": 10000, "wins": 4600, "win_rate": 0.46, "low_sample": False},
        },
        "126": {
            "6697": {"games": 40000, "wins": 20400, "win_rate": 0.51, "low_sample": False},
            "6695": {"games": 10000, "wins": 4600, "win_rate": 0.46, "low_sample": False},
        },
    }
    ds_dir = tmp_path / "ds_p02"
    _write_json(ds_dir / "heroes.json", heroes)
    _write_json(ds_dir / "augments.json", {"1002": {"name": "尖端发明家", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {h: {} for h in heroes})
    _write_json(ds_dir / "core_builds.json", core)
    _write_json(ds_dir / "hero_item_recommendations.json", item_recs)

    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    assert store.get_item_global_bias("3089") == pytest.approx(0.04, abs=1e-6)
    assert store.get_item_global_bias("3165") == pytest.approx(-0.05, abs=1e-6)
    assert store.get_item_global_bias("6695") == pytest.approx(-0.04, abs=1e-6)

    # 1. Empty enemies (no heal threat): 3165 keeps full negative base_winrate (no negative-bias free gift, no clamp)
    _, recs_empty = optimize_next_items(store, "99", ("6655", "4645"), 3600.0, (), (), (), limit=6, collect_breakdown=True)
    morello_empty = next(r for r in recs_empty if r.item_id == "3165")
    rabadon_empty = next(r for r in recs_empty if r.item_id == "3089")
    assert morello_empty.score_breakdown["base_winrate"] < -4.0
    # Positive bias (+4%) IS unilaterally trimmed: ~4.0 - 0.65*4.0 = ~1.4 (not 4.0)
    assert 0.5 < rabadon_empty.score_breakdown["base_winrate"] < 2.0

    # 2. Active heal threat & NO ally GW: 3165 negative base_winrate clamps to 0.0 and jumps to #1
    heal_enemies = [_mk_enemy("索拉卡", ()), _mk_enemy("蒙多医生", ())]
    thr_h, recs_heal = optimize_next_items(
        store, "99", ("6655", "4645"), 3600.0, (), (), heal_enemies, limit=6, collect_breakdown=True
    )
    assert thr_h.enemy_heal_score >= 1.8 and not thr_h.ally_has_grievous
    morello_heal = next(r for r in recs_heal if r.item_id == "3165")
    assert morello_heal.score_breakdown["base_winrate"] == 0.0
    assert recs_heal[0].item_id == "3165"

    # 3. Active heal threat WITH ally GW: clamp does NOT fire; 3165 stays negative base_winrate
    ally_gw = [PlayerLiveInfo("a1", "泰坦", "Nautilus", "ORDER", False, 11, 2, 3, 9, ("3075",))]
    thr_hgw, recs_ally_gw = optimize_next_items(
        store, "99", ("6655", "4645"), 3600.0, (), ally_gw, heal_enemies, limit=6, collect_breakdown=True
    )
    assert thr_hgw.ally_has_grievous is True
    morello_ally_gw = next(r for r in recs_ally_gw if r.item_id == "3165")
    assert morello_ally_gw.score_breakdown["base_winrate"] < -4.0
    assert recs_ally_gw[0].item_id != "3165"

    # 4. Shield break clamp: empty enemies keeps negative base_winrate; active shield threat clamps to 0.0
    _, zed_empty = optimize_next_items(store, "238", ("6697", "6676"), 3600.0, (), (), (), limit=6, collect_breakdown=True)
    serp_empty = next(r for r in zed_empty if r.item_id == "6695")
    assert serp_empty.score_breakdown["base_winrate"] < -3.0

    shield_enemies = [_mk_enemy("卡尔玛", ()), _mk_enemy("璐璐", ())]
    thr_s, zed_shield = optimize_next_items(
        store, "238", ("6697", "6676"), 3600.0, (), (), shield_enemies, limit=6, collect_breakdown=True
    )
    assert thr_s.enemy_shield_score >= 1.8 and not thr_s.ally_has_shield_break
    serp_shield = next(r for r in zed_shield if r.item_id == "6695")
    assert serp_shield.score_breakdown["base_winrate"] == 0.0
    assert zed_shield[0].item_id == "6695"

    # Ally already owns Serpent's Fang -> clamp does NOT fire
    ally_sb = [PlayerLiveInfo("a1", "杰斯", "Jayce", "ORDER", False, 11, 4, 3, 6, ("6695",))]
    thr_s2, zed_ally_sb = optimize_next_items(
        store, "238", ("6697", "6676"), 3600.0, (), ally_sb, shield_enemies, limit=6, collect_breakdown=True
    )
    assert thr_s2.ally_has_shield_break is True
    serp_ally_sb = next(r for r in zed_ally_sb if r.item_id == "6695")
    assert serp_ally_sb.score_breakdown["base_winrate"] < -3.0


def test_high_band_mode_forwarding_and_low_sample_decay_synthetic(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110}, "", "complete_equipment"),
        "3100": ItemStaticInfo("3100", "巫妖之祸", 3100, ("SpellDamage",), {"FlatMagicDamageMod": 100}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105, "FlatArmorMod": 50}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "16.22" / "normal"
    _write_json(ds_dir / "heroes.json", {"105": {"name": "菲兹", "title": "潮汐海灵", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 20}})
    _write_json(ds_dir / "augments.json", {"1002": {"name": "尖端发明家", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {"105": {}})
    _write_json(ds_dir / "core_builds.json", {"105": [{"items": ["4645", "3100"], "ordered": True, "games": 20000, "wins": 10400, "win": 0.52, "pick": 0.40}]})
    _write_json(
        ds_dir / "hero_item_recommendations.json",
        {
            "105": {
                "4645": {"games": 25000, "wins": 13000, "win_rate": 0.52, "low_sample": False},
                "3100": {"games": 15000, "wins": 7500, "win_rate": 0.50, "low_sample": False},
                "3157": {"games": 12000, "wins": 6000, "win_rate": 0.50, "low_sample": False},
            }
        },
    )
    hi_dir = ds_dir / "bands" / "high"
    _write_json(hi_dir / "heroes.json", {"105": {"name": "菲兹", "title": "潮汐海灵", "games": 10000, "wins": 5000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 20}})
    _write_json(hi_dir / "core_builds.json", {"105": [{"items": ["3100", "4645"], "ordered": True, "games": 2500, "wins": 1400, "win": 0.56, "pick": 0.45}]})
    _write_json(
        hi_dir / "hero_item_recommendations.json",
        {
            "105": {
                "3100": {"games": 3000, "wins": 1680, "win_rate": 0.56, "low_sample": False},
                "4645": {"games": 2000, "wins": 1000, "win_rate": 0.50, "low_sample": False},
                # Low-sample high-band item: 60 games (< 300) -> w_eff = 60/300 = 0.20 in "high", 0.10 in "blended"
                "3157": {"games": 60, "wins": 42, "win_rate": 0.70, "low_sample": True},
            }
        },
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.22", "normal", "常规", "patches/16.22/normal", "", True), ds_dir)

    _, recs_all = optimize_next_items(store, "105", (), 3200.0, (), (), (), limit=4, data_band_mode="all")
    _, recs_mid = optimize_next_items(store, "105", (), 3200.0, (), (), (), limit=4, data_band_mode="blended")
    _, recs_hi = optimize_next_items(store, "105", (), 3200.0, (), (), (), limit=4, data_band_mode="high")

    assert recs_all[0].item_id == "4645"
    assert recs_hi[0].item_id == "3100"

    zh_all = next(r for r in recs_all if r.item_id == "3157")
    zh_mid = next(r for r in recs_mid if r.item_id == "3157")
    zh_hi = next(r for r in recs_hi if r.item_id == "3157")
    # Smooth decay: 0.50 -> 0.52 (blended, w_eff=0.10) -> 0.54 (high, w_eff=0.20), never raw 0.70!
    assert zh_all.win_rate == pytest.approx(0.50, abs=1e-4)
    assert zh_mid.win_rate == pytest.approx(0.52, abs=1e-4)
    assert zh_hi.win_rate == pytest.approx(0.54, abs=1e-4)


# ---------------------------------------------------------------------------
# Real 16.23/normal G2 Acceptance Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not (LIVE_1623_NORMAL / "heroes.json").is_file(), reason="Real 16.23/normal dataset not present locally")
def test_g2_real_1623_zero_false_gw_or_serpents_across_all_173_heroes() -> None:
    store = _load_live_store(LIVE_1623_NORMAL, "16.23", bands=False)
    heroes = store.all_heroes()
    assert len(heroes) == 173

    situational_ids = {"3033", "3075", "3165", "6695", "6609"}
    false_hits: list[tuple[str, str, str, str, float]] = []
    for h in heroes:
        _, recs = optimize_next_items(store, h.hero_id, (), 3000.0, (), (), (), limit=4)
        for rec in recs[:3]:
            if rec.item_id in situational_ids:
                false_hits.append((h.hero_id, h.name, rec.item_id, rec.name, rec.score))

    assert false_hits == [], f"Unexpected false GW/Shield-Break items in empty-enemy Top 3: {false_hits}"


@pytest.mark.skipif(not (LIVE_1623_NORMAL / "heroes.json").is_file(), reason="Real 16.23/normal dataset not present locally")
def test_g2_real_1623_five_heal_and_five_shield_counter_rankings() -> None:
    store = _load_live_store(LIVE_1623_NORMAL, "16.23", bands=False)

    # 5-heal enemy comp (heal >= 8, ally has no GW)
    heal_enemies = (
        _mk_enemy("索拉卡", ("6617", "3065")),
        _mk_enemy("蒙多医生", ("3084", "3065")),
        _mk_enemy("亚托克斯", ("6610", "3071")),
        _mk_enemy("弗拉基米尔", ("4633", "3157")),
        _mk_enemy("金克丝", ("3031", "3085")),
    )
    for hid, my_items, expected_gw in (
        ("99", ("6655", "4645"), "3165"),   # Lux -> Morellonomicon
        ("103", ("6655", "4645"), "3165"),  # Ahri -> Morellonomicon
        ("112", ("2503", "4645"), "3165"),  # Viktor -> Morellonomicon
        ("36", ("3084", "3083"), "3075"),   # Dr. Mundo -> Thornmail
    ):
        thr, recs = optimize_next_items(store, hid, my_items, 3000.0, (), (), heal_enemies, limit=6)
        assert thr.enemy_heal_score >= 8.0
        assert thr.ally_has_grievous is False
        top2 = [r.item_id for r in recs[:2]]
        assert expected_gw in top2, f"Hero {hid} expected {expected_gw} in Top 2, got {[(r.item_id, r.name, r.score) for r in recs[:4]]}"

    # 5-shield enemy comp (shield >= 7, ally has no shield-break)
    shield_enemies = (
        _mk_enemy("卡尔玛", ("6617", "3107")),
        _mk_enemy("璐璐", ("3504", "3190")),
        _mk_enemy("迦娜", ("3107", "3222")),
        _mk_enemy("塔姆", ("3084", "3119")),
        _mk_enemy("金克丝", ("6673", "3031")),
    )
    for hid, my_items in (
        ("238", ("6697", "6694")),  # Zed
        ("202", ("6676", "3031")),  # Jhin
        ("126", ("6697", "3004")),  # Jayce
    ):
        thr, recs = optimize_next_items(store, hid, my_items, 3000.0, (), (), shield_enemies, limit=6)
        assert thr.enemy_shield_score >= 7.0
        assert thr.ally_has_shield_break is False
        top2 = [r.item_id for r in recs[:2]]
        assert "6695" in top2, f"Hero {hid} expected 6695 in Top 2, got {[(r.item_id, r.name, r.score) for r in recs[:4]]}"


# ---------------------------------------------------------------------------
# Real 16.22/normal (with bands/high) P0-3 Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_find_live_1622_dir() is None, reason="Local 16.22/normal dataset with bands/high not present")
def test_g2_real_1622_high_band_mode_and_low_sample_smooth_decay() -> None:
    d22 = _find_live_1622_dir()
    assert d22 is not None
    store = _load_live_store(d22, "16.22", bands=True)
    assert store.has_high_band() is True

    # Darius (122): all-rank starter is Heartsteel (3084), high-band shifts to Stridebreaker (6631)
    _, dar_all = optimize_next_items(store, "122", (), 3300.0, (), (), (), limit=4, data_band_mode="all")
    _, dar_mid = optimize_next_items(store, "122", (), 3300.0, (), (), (), limit=4, data_band_mode="blended")
    _, dar_hi = optimize_next_items(store, "122", (), 3300.0, (), (), (), limit=4, data_band_mode="high")
    assert dar_all[0].item_id == "3084"
    assert dar_hi[0].item_id == "6631"
    assert len(dar_mid) >= 3

    # Olaf (2): all-rank favors Experimental Hexplate (3073), high-band shifts to Stridebreaker (6631)
    _, olaf_all = optimize_next_items(store, "2", (), 3300.0, (), (), (), limit=4, data_band_mode="all")
    _, olaf_hi = optimize_next_items(store, "2", (), 3300.0, (), (), (), limit=4, data_band_mode="high")
    assert olaf_all[0].item_id == "3073"
    assert olaf_hi[0].item_id == "6631"

    # Verify smooth decay inside optimize_next_items for a genuine low-sample high-band item (high_games < 300)
    raw_hi = json.loads((d22 / "bands" / "high" / "hero_item_recommendations.json").read_text(encoding="utf-8"))
    checked = False
    for hid, hmap in raw_hi.items():
        all_recs = store.get_hero_item_recs(hid, band_mode="all")
        for iid, st in hmap.items():
            hg = int(st.get("games", 0))
            if not (20 <= hg <= 180) or iid in TEAR_ALL_IDS or iid not in all_recs:
                continue
            a_wr = all_recs[iid].win_rate
            h_wr = float(st["win_rate"])
            if abs(h_wr - a_wr) < 0.025:
                continue
            _, opt_all = optimize_next_items(store, hid, (), 3500.0, (), (), (), limit=30, data_band_mode="all")
            _, opt_mid = optimize_next_items(store, hid, (), 3500.0, (), (), (), limit=30, data_band_mode="blended")
            _, opt_hi = optimize_next_items(store, hid, (), 3500.0, (), (), (), limit=30, data_band_mode="high")
            r_all = next((r for r in opt_all if r.item_id == iid), None)
            r_mid = next((r for r in opt_mid if r.item_id == iid), None)
            r_hi = next((r for r in opt_hi if r.item_id == iid), None)
            if r_all and r_mid and r_hi and r_all.win_rate is not None and r_mid.win_rate is not None and r_hi.win_rate is not None:
                w_eff_hi = hg / 300.0
                w_eff_mid = 0.5 * w_eff_hi
                exp_hi = (1.0 - w_eff_hi) * a_wr + w_eff_hi * h_wr
                exp_mid = (1.0 - w_eff_mid) * a_wr + w_eff_mid * h_wr
                assert r_hi.win_rate == pytest.approx(exp_hi, abs=1e-4)
                assert r_mid.win_rate == pytest.approx(exp_mid, abs=1e-4)
                checked = True
                break
        if checked:
            break
    assert checked, "Expected at least one candidate low-sample high-band item in 16.22/normal"
