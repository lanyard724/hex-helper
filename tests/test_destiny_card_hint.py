"""Tests for P0-6 Destiny's Weapon (3081) scorer fix and concise card hint (G5)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.destiny import (
    DESTINY_AUGMENT_ID,
    DESTINY_NEGATIVE_WARNING,
    DestinyCardHint,
    DestinyItemRec,
    rank_destiny_items,
    summarize_destiny_card_hint,
)
from hex_helper.scoring.tier import score_hero_augment

LIVE_1623_NORMAL = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_1623_NORMAL",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal",
    )
)


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _load_real_1623_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )
    return store


def _real_aug_delta(store: DataStore, hero_id: str, *, smoothed: bool = False) -> float:
    hero = store.get_hero(hero_id)
    aug = store.get_hero_augment_stat(hero_id, DESTINY_AUGMENT_ID)
    assert hero is not None and aug is not None and aug.games > 0
    if smoothed:
        sc = score_hero_augment(DESTINY_AUGMENT_ID, "命运之武器", "gold", hero.win_rate, aug.wins, aug.games)
        return sc.delta
    return aug.win_rate - hero.win_rate


def _build_synthetic_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3146": ItemStaticInfo("3146", "海克斯科技枪刃", 3250, ("SpellDamage",), {}, "", "complete_equipment"),
        "3153": ItemStaticInfo("3153", "破败王者之刃", 3200, ("Damage",), {}, "", "complete_equipment"),
        "2510": ItemStaticInfo("2510", "黄昏与黎明", 3100, ("SpellDamage",), {}, "", "complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {}, "", "complete_equipment"),
        "4646": ItemStaticInfo("4646", "风暴狂涌", 2900, ("SpellDamage",), {}, "", "complete_equipment"),
    }
    ds = tmp_path / "syn_ds"
    _write_json(
        ds / "heroes.json",
        {
            # Hero with control_games >= 100 and control WR lower than hero WR
            "84": {"name": "阿卡丽", "title": "离群之刺", "games": 100000, "wins": 43820, "win_rate": 0.4382, "pick_rate": 0.03, "rank": 150},
            # Dual-style hero (AP vs AD)
            "145": {"name": "卡莎", "title": "虚空之女", "games": 120000, "wins": 58800, "win_rate": 0.4900, "pick_rate": 0.05, "rank": 80},
            # Low control sample (< 100) must fall back to baseline_win_rate (hero.win_rate)
            "99": {"name": "拉克丝", "title": "光辉女郎", "games": 80000, "wins": 41600, "win_rate": 0.5200, "pick_rate": 0.04, "rank": 40},
            # Negative threshold synthetic test hero
            "53": {"name": "布里茨", "title": "蒸汽机器人", "games": 90000, "wins": 42300, "win_rate": 0.4700, "pick_rate": 0.02, "rank": 120},
            # Zero-item hero
            "234": {"name": "佛耶戈", "title": "破败之王", "games": 70000, "wins": 33600, "win_rate": 0.4800, "pick_rate": 0.02, "rank": 100},
        },
    )
    _write_json(ds / "augments.json", {"3081": {"name": "命运之武器", "rarity": "gold"}})
    _write_json(ds / "hero_augments.json", {h: {} for h in ("84", "145", "99", "53", "234")})
    _write_json(ds / "core_builds.json", {h: [] for h in ("84", "145", "99", "53", "234")})
    _write_json(ds / "hero_item_recommendations.json", {h: {} for h in ("84", "145", "99", "53", "234")})
    _write_json(
        ds / "hero_augment_items.json",
        {
            "84": {
                "3081": {
                    "items": [
                        {
                            "item": "3146",
                            "name": "海克斯科技枪刃",
                            "games": 4000,
                            "wins": 1979,
                            "low_sample": False,
                            "control": {"games": 80000, "wins": 34400},  # ctrl_wr = 0.4300
                            "style": "不拆",
                        }
                    ]
                }
            },
            "145": {
                "3081": {
                    "items": [
                        {
                            "item": "2510",
                            "name": "黄昏与黎明",
                            "games": 1500,
                            "wins": 810,  # 0.5400
                            "low_sample": False,
                            "control": {"games": 50000, "wins": 25000},  # 0.5000 -> +4.0%
                            "style": "AP",
                        },
                        {
                            "item": "3153",
                            "name": "破败王者之刃",
                            "games": 2500,
                            "wins": 1375,  # 0.5500
                            "low_sample": False,
                            "control": {"games": 50000, "wins": 22000},  # 0.4400 -> +11.0%
                            "style": "AD",
                        },
                    ]
                }
            },
            "99": {
                "3081": {
                    "items": [
                        {
                            "item": "4646",
                            "name": "风暴狂涌",
                            "games": 1000,
                            "wins": 540,  # 0.5400
                            "low_sample": False,
                            "control": {"games": 50, "wins": 20},  # < 100 -> ignored, use hero.win_rate 0.5200 -> +2.0%
                            "style": "AP",
                        }
                    ]
                }
            },
            "53": {
                "3081": {
                    "items": [
                        {
                            "item": "3084",
                            "name": "心之钢",
                            "games": 3000,
                            "wins": 1380,  # 0.4600
                            "low_sample": False,
                            "control": {"games": 40000, "wins": 18800},  # 0.4700 -> -1.0%
                            "style": "AP",
                        }
                    ]
                }
            },
            "234": {"3081": {"items": []}},
        },
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds)
    return store


def test_backward_compatibility_and_control_vs_baseline_delta(tmp_path: Path) -> None:
    # 6-arg positional constructor remains intact
    rec = DestinyItemRec("3089", "灭世者的死亡之帽", 0.58, 0.04, 1200, False)
    assert rec.item_id == "3089"
    assert rec.style == ""
    assert rec.control_games == 0
    assert rec.control_win_rate is None

    store = _build_synthetic_store(tmp_path)

    # control_games >= 100 uses control WR (0.4300) rather than hero WR (0.4382)
    akali_recs = rank_destiny_items(store, "84")
    assert len(akali_recs) == 1
    expected_akali_delta = (1979 / 4000) - 0.4300
    assert akali_recs[0].delta == pytest.approx(expected_akali_delta, abs=1e-6)
    assert akali_recs[0].control_win_rate == pytest.approx(0.4300, abs=1e-6)

    # control_games < 100 falls back to baseline_win_rate (hero WR 0.5200)
    lux_recs = rank_destiny_items(store, "99")
    assert len(lux_recs) == 1
    assert lux_recs[0].control_win_rate is None
    assert lux_recs[0].delta == pytest.approx(0.5400 - 0.5200, abs=1e-6)

    # Zero-item and unknown heroes return empty / None gracefully
    assert rank_destiny_items(store, "234") == []
    assert summarize_destiny_card_hint(store, "234") is None
    assert summarize_destiny_card_hint(store, "99999") is None


def test_synthetic_profile_preference_and_alert_boundaries(tmp_path: Path) -> None:
    store = _build_synthetic_store(tmp_path)

    # Unmatched / empty profile picks highest delta (AD +11.0% > AP +4.0%)
    hint_default = summarize_destiny_card_hint(store, "145")
    assert isinstance(hint_default, DestinyCardHint)
    assert hint_default.top_item_id == "3153"
    assert hint_default.top_delta == pytest.approx(0.11, abs=1e-4)
    assert hint_default.hint_line == "◈ 绑定首选：破败王者之刃 (+11.0%)"
    assert hint_default.is_negative_alert is False
    assert hint_default.warning_line == ""

    # AP profile (case-insensitive / alias normalized) prioritizes AP item (+4.0%)
    for prof in ("AP", "ap", "法强"):
        hint_ap = summarize_destiny_card_hint(store, "145", profile=prof)
        assert hint_ap is not None
        assert hint_ap.top_item_id == "2510"
        assert hint_ap.style == "AP"
        assert hint_ap.hint_line == "◈ 绑定首选：黄昏与黎明 (+4.0%)"

    # Unmatched profile (SUPPORT) falls back to high-delta ranking
    hint_sup = summarize_destiny_card_hint(store, "145", profile="SUPPORT")
    assert hint_sup is not None and hint_sup.top_item_id == "3153"

    # Exact alert boundary on synthetic hero "53" (item delta = -0.010, exactly at boundary)
    # 1) aug_delta = -0.004 (>= -0.005) and top_delta = -0.010 (not < -0.010) -> False
    h_edge_no = summarize_destiny_card_hint(store, "53", aug_delta=-0.004)
    assert h_edge_no is not None
    assert h_edge_no.is_negative_alert is False
    assert h_edge_no.warning_line == ""

    # 2) aug_delta = -0.006 (< -0.005) and top_delta = -0.010 (< 0.0) -> True
    h_edge_yes = summarize_destiny_card_hint(store, "53", aug_delta=-0.006)
    assert h_edge_yes is not None
    assert h_edge_yes.is_negative_alert is True
    assert h_edge_yes.warning_line == DESTINY_NEGATIVE_WARNING

    # 3) Dead -1.5% rule check: even aug_delta = -0.025 does NOT warn when top_delta > 0
    h_pos_item = summarize_destiny_card_hint(store, "84", aug_delta=-0.025)
    assert h_pos_item is not None
    assert h_pos_item.is_negative_alert is False
    assert h_pos_item.warning_line == ""


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_real_1623_all_173_heroes_no_half_baseline(tmp_path: Path) -> None:
    store = _load_real_1623_store(tmp_path)
    heroes = store.all_heroes()
    assert len(heroes) == 173

    zero_count = 0
    for h in heroes:
        d = store.get_destiny_data(h.hero_id)
        assert d is not None, f"Missing DestinyData for {h.hero_id}"
        assert abs(d.baseline_win_rate - 0.50) > 1e-6, f"Hero {h.hero_id} fell back to 0.50!"
        assert d.baseline_win_rate == pytest.approx(h.win_rate, abs=1e-4)
        if not d.items:
            zero_count += 1
            assert summarize_destiny_card_hint(store, h.hero_id) is None
    assert zero_count == 5


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_real_1623_inversion_fixes_yasuo_ashe_and_alerts(tmp_path: Path) -> None:
    store = _load_real_1623_store(tmp_path)

    # Akali (84), LeBlanc (7), Kennen (85) must be POSITIVE (fixing -0.53%/-2.40%/-2.20% bugs)
    for hid, expected_item in (("84", "3146"), ("7", "4646"), ("85", "4646")):
        hint = summarize_destiny_card_hint(store, hid, aug_delta=_real_aug_delta(store, hid))
        assert hint is not None
        assert hint.top_item_id == expected_item
        assert hint.top_delta > 0.03, f"Hero {hid} expected strongly positive delta, got {hint.top_delta}"
        assert hint.is_negative_alert is False
        assert hint.hint_line.startswith("◈ 绑定首选：")

    # Yasuo (157) must be realistic +2.0% to +4.0% (not inflated +12.11%)
    yasuo = summarize_destiny_card_hint(store, "157", aug_delta=_real_aug_delta(store, "157"))
    assert yasuo is not None
    assert yasuo.top_item_id == "3032"
    assert 0.02 <= yasuo.top_delta <= 0.04, f"Yasuo delta out of [2%, 4%]: {yasuo.top_delta}"
    assert yasuo.is_negative_alert is False

    # Ashe (22) mainstream AP profile returns NEGATIVE top_delta (fixing +3.98% inversion bug)
    ashe_ap = summarize_destiny_card_hint(store, "22", aug_delta=_real_aug_delta(store, "22"), profile="AP")
    assert ashe_ap is not None
    assert ashe_ap.top_item_id == "4628"
    assert ashe_ap.top_delta < 0.0
    assert ashe_ap.is_negative_alert is True
    assert ashe_ap.warning_line == DESTINY_NEGATIVE_WARNING

    # Rell (526), Blitzcrank (53), Nautilus (111), Sion (14) trigger negative alert (raw & smoothed aug_delta)
    for hid in ("526", "53", "111", "14"):
        for smoothed in (False, True):
            hint = summarize_destiny_card_hint(store, hid, aug_delta=_real_aug_delta(store, hid, smoothed=smoothed))
            assert hint is not None
            assert hint.is_negative_alert is True, f"Hero {hid} (smoothed={smoothed}) should alert"
            assert hint.warning_line == DESTINY_NEGATIVE_WARNING

    # Teemo (17) has negative aug_delta (-1.92%) but positive Statikk Shiv (+1.25%): MUST NOT alert
    teemo = summarize_destiny_card_hint(store, "17", aug_delta=_real_aug_delta(store, "17"))
    assert teemo is not None
    assert teemo.top_delta > 0.01
    assert teemo.is_negative_alert is False
    assert teemo.warning_line == ""


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_real_1623_dual_style_heroes_respect_profile(tmp_path: Path) -> None:
    store = _load_real_1623_store(tmp_path)

    # Kai'Sa (145): AP -> 2510, AD -> 3153
    kaisa_ap = summarize_destiny_card_hint(store, "145", profile="AP")
    kaisa_ad = summarize_destiny_card_hint(store, "145", profile="AD")
    assert kaisa_ap is not None and kaisa_ap.top_item_id == "2510" and kaisa_ap.style == "AP"
    assert kaisa_ad is not None and kaisa_ad.top_item_id == "3153" and kaisa_ad.style == "AD"

    # Varus (110): AD -> 3153, AP -> 3115
    varus_ad = summarize_destiny_card_hint(store, "110", profile="AD")
    varus_ap = summarize_destiny_card_hint(store, "110", profile="ap")
    assert varus_ad is not None and varus_ad.top_item_id == "3153" and varus_ad.style == "AD"
    assert varus_ap is not None and varus_ap.top_item_id == "3115" and varus_ap.style == "AP"

    # Kayle (10): AP -> 2510, AD -> 2517
    kayle_ap = summarize_destiny_card_hint(store, "10", profile="法强")
    kayle_ad = summarize_destiny_card_hint(store, "10", profile="ad")
    assert kayle_ap is not None and kayle_ap.top_item_id == "2510"
    assert kayle_ad is not None and kayle_ad.top_item_id == "2517"

    # Shyvana (102): AP -> 2510, TANK -> 3084
    shyv_ap = summarize_destiny_card_hint(store, "102", profile="AP")
    shyv_tank = summarize_destiny_card_hint(store, "102", profile="TANK")
    assert shyv_ap is not None and shyv_ap.top_item_id == "2510"
    assert shyv_tank is not None and shyv_tank.top_item_id == "3084" and shyv_tank.style == "坦克"


def test_unsplit_alert_consistency_and_no_positive_cyan_contradiction(tmp_path: Path) -> None:
    """Unsplit heroes must never show positive '(+X.X%)' alongside red warning or false-alert on positive aug_delta."""
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {}, "", "complete_equipment"),
    }
    ds = tmp_path / "syn_unsplit"
    _write_json(
        ds / "heroes.json",
        {
            # Poppy-like: hero_wr=0.5417, item_wr=0.5314 (base_d=-1.03%), ctrl_wr=0.5072 (top_d=+2.42%)
            "78": {"name": "波比", "title": "圣锤之毅", "games": 100000, "wins": 54170, "win_rate": 0.5417, "pick_rate": 0.03, "rank": 10},
            # Rell-like: hero_wr=0.5438, item_wr=0.5293 (base_d=-1.45%), ctrl_wr=0.5227 (top_d=+0.66%)
            "526": {"name": "芮尔", "title": "镕铁少女", "games": 100000, "wins": 54380, "win_rate": 0.5438, "pick_rate": 0.02, "rank": 8},
        },
    )
    _write_json(ds / "augments.json", {"3081": {"name": "命运之武器", "rarity": "gold"}})
    _write_json(ds / "hero_augments.json", {"78": {}, "526": {}})
    _write_json(ds / "core_builds.json", {"78": [], "526": []})
    _write_json(ds / "hero_item_recommendations.json", {"78": {}, "526": {}})
    _write_json(
        ds / "hero_augment_items.json",
        {
            "78": {
                "3081": {
                    "items": [
                        {
                            "item": "3084",
                            "name": "心之钢",
                            "games": 6274,
                            "wins": 3334,  # 0.5314
                            "low_sample": False,
                            "control": {"games": 149430, "wins": 75786},  # 0.5072
                            "style": "不拆",
                        }
                    ]
                }
            },
            "526": {
                "3081": {
                    "items": [
                        {
                            "item": "3084",
                            "name": "心之钢",
                            "games": 4393,
                            "wins": 2325,  # 0.5293
                            "low_sample": False,
                            "control": {"games": 151441, "wins": 79162},  # 0.5227
                            "style": "不拆",
                        }
                    ]
                }
            },
        },
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds)

    # 1) Poppy-like with positive aug_delta (+1.11%) and positive control delta (+2.4%): MUST NOT false-alert
    poppy = summarize_destiny_card_hint(store, "78", aug_delta=0.0111)
    assert poppy is not None
    assert poppy.is_negative_alert is False
    assert poppy.warning_line == ""
    assert "(+2.4%)" in poppy.hint_line

    # 2) Rell-like with negative aug_delta (-1.46%): alerts AND displays negative hero-baseline delta (-1.5%), NOT (+0.7%)
    rell = summarize_destiny_card_hint(store, "526", aug_delta=-0.0146)
    assert rell is not None
    assert rell.is_negative_alert is True
    assert rell.warning_line == DESTINY_NEGATIVE_WARNING
    assert rell.top_delta < 0.0
    assert "(+" not in rell.hint_line
    assert "(-1.5%)" in rell.hint_line


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_real_1623_all_173_heroes_never_mix_positive_hint_with_red_warning(tmp_path: Path) -> None:
    """Across all 173 real heroes, zero badges may show '(+' alongside is_negative_alert, and Poppy must not false-alert."""
    store = _load_real_1623_store(tmp_path)

    # Poppy (78) has positive 3081 aug_delta (+1.11%) and positive control delta (+2.4%): MUST NOT false-alert
    for smoothed in (False, True):
        poppy = summarize_destiny_card_hint(store, "78", aug_delta=_real_aug_delta(store, "78", smoothed=smoothed))
        assert poppy is not None
        assert poppy.is_negative_alert is False
        assert poppy.warning_line == ""
        assert "(+" in poppy.hint_line

    # Sion (14), Leona (89), Rell (526), Blitzcrank (53), Nautilus (111) alert with consistent negative delta
    for hid in ("14", "89", "526", "53", "111"):
        for smoothed in (False, True):
            hint = summarize_destiny_card_hint(store, hid, aug_delta=_real_aug_delta(store, hid, smoothed=smoothed))
            assert hint is not None
            assert hint.is_negative_alert is True, f"Hero {hid} (smoothed={smoothed}) should alert"
            assert hint.top_delta < 0.0, f"Hero {hid} alerted with non-negative top_delta={hint.top_delta}"
            assert "(+" not in hint.hint_line, f"Hero {hid} contradictory positive hint: {hint.hint_line}"

    # Global invariant across ALL 173 heroes x all profiles (raw & smoothed aug_delta)
    for h in store.all_heroes():
        aug = store.get_hero_augment_stat(h.hero_id, DESTINY_AUGMENT_ID)
        if aug is None or aug.games <= 0:
            continue
        for smoothed in (False, True):
            aug_d = _real_aug_delta(store, h.hero_id, smoothed=smoothed)
            for prof in ("", "AP", "AD", "TANK"):
                hint = summarize_destiny_card_hint(store, h.hero_id, aug_delta=aug_d, profile=prof)
                if hint is None:
                    continue
                if hint.is_negative_alert:
                    assert hint.top_delta < 0.0, (
                        f"Hero {h.hero_id} ({h.name}, prof={prof}) alerted with top_delta={hint.top_delta}"
                    )
                    assert "(+" not in hint.hint_line, (
                        f"Hero {h.hero_id} ({h.name}, prof={prof}) contradictory hint={hint.hint_line}"
                    )
