"""Phase A1 Data Truth Layer Foundation tests (G1 + P0-1/P0-2/P0-3/P0-4/P0-6)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from hex_helper.config import DATA_BAND_MODES, AppConfig, load_config, save_config
from hex_helper.data.aramhex_sync import (
    HIGH_BAND_DATASET_FILES,
    OPTIONAL_DATASET_FILES,
    OPTIONAL_HIGH_BAND_FILES,
    AramHexSyncer,
    sha256_bytes,
)
from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import (
    TEAR_ALL_IDS,
    TEAR_ITEM_PAIRS,
    DataStore,
    compute_tear_backfill_scale,
)

LIVE_1623_NORMAL = Path(r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal")
LIVE_1623_FP = Path(r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\first-prismatic")
LIVE_1623_RAW = Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\1623_hero_items_raw.json")
LIVE_1622_DIR_CANDIDATES = (
    Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\aramhex_16_22_normal"),
    Path(r"z:\antigravity\scratch\opgg_aram_mayhem_audit\aramhex_16_22"),
)


def _find_live_1622_dir() -> Path | None:
    for cand in LIVE_1622_DIR_CANDIDATES:
        if (cand / "heroes.json").is_file() and (cand / "bands" / "high" / "hero_item_recommendations.json").is_file():
            return cand
    return None


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _stage_1623_normal_with_raw(tmp_path: Path) -> Path:
    """Copy real 16.23/normal + 1623_hero_items_raw.json into isolated tmp_path without touching user cache."""
    dst = tmp_path / "16.23" / "normal"
    dst.mkdir(parents=True, exist_ok=True)
    for f in LIVE_1623_NORMAL.glob("*.json"):
        shutil.copy2(f, dst / f.name)
    if LIVE_1623_RAW.is_file() and not (dst / "hero_items_raw.json").exists():
        shutil.copy2(LIVE_1623_RAW, dst / "hero_items_raw.json")
    return dst


# ---------------------------------------------------------------------------
# P0-1: Tear Stacked Items Backfill (Synthetic + Real 16.23/normal)
# ---------------------------------------------------------------------------


def test_tear_backfill_synthetic_non_tear_intersection_scale_and_all_four_pairs(tmp_path: Path) -> None:
    ds_dir = tmp_path / "16.23" / "normal"
    _write_json(
        ds_dir / "heroes.json",
        {"13": {"name": "瑞兹", "title": "符文法师", "games": 100000, "wins": 52000, "win_rate": 0.52, "pick_rate": 0.04, "rank": 20}},
    )
    _write_json(ds_dir / "augments.json", {"1002": {"name": "尖端发明家", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {"13": {"1002": {"games": 2000, "wins": 1040, "win_rate": 0.52}}})
    _write_json(ds_dir / "core_builds.json", {"13": []})

    # Non-Tear shared items: recs_common = 15000, raw_common = 20000 -> exact scale = 0.75.
    # Raw also contains huge quest/gift items (994403) + stacked Tear items so naive all/all ratio would be severely biased!
    _write_json(
        ds_dir / "hero_item_recommendations.json",
        {
            "13": {
                "3089": {"games": 9000, "wins": 4950, "win_rate": 0.55, "low_sample": False},
                "6657": {"games": 6000, "wins": 3180, "win_rate": 0.53, "low_sample": False},
                "3003": {"games": 1000, "wins": 380, "win_rate": 0.38, "low_sample": False},
                "3004": {"games": 200, "wins": 84, "win_rate": 0.42, "low_sample": True},
                "3119": {"games": 100, "wins": 40, "win_rate": 0.40, "low_sample": True},
                "2526": {"games": 100, "wins": 42, "win_rate": 0.42, "low_sample": True},
            }
        },
    )
    _write_json(
        ds_dir / "hero_items_raw.json",
        {
            "13": {
                "3089": {"games": 12000, "wins": 6600, "win_rate": 0.55},
                "6657": {"games": 8000, "wins": 4240, "win_rate": 0.53},
                "994403": {"games": 50000, "wins": 26000, "win_rate": 0.52},  # gift item excluded from common
                "3003": {"games": 1333, "wins": 506, "win_rate": 0.38},
                "3040": {"games": 40000, "wins": 21200, "win_rate": 0.53},  # scaled: 30000g, 15900w
                "3042": {"games": 2000, "wins": 1040, "win_rate": 0.52},   # scaled: 1500g, 780w
                "3121": {"games": 1000, "wins": 510, "win_rate": 0.51},    # scaled: 750g, 382w (round(382.5)=382)
                "2530": {"games": 200, "wins": 110, "win_rate": 0.55},     # scaled: 150g, 82w -> tot 250 (<300 low_sample=True)
            }
        },
    )

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    recs = store.get_hero_item_recs("13")

    assert TEAR_ALL_IDS == frozenset({"3003", "3040", "3004", "3042", "3119", "3121", "2526", "2530"})
    assert recs["3003"].games == 31000
    assert recs["3003"].wins == 16280
    assert recs["3040"] == recs["3003"].__class__("3040", 31000, 16280, 16280 / 31000, False)
    assert recs["3003"].low_sample is False
    assert recs["3003"].win_rate == pytest.approx(16280 / 31000, abs=1e-6)

    assert recs["3004"].games == 1700
    assert recs["3042"].games == 1700
    assert recs["3004"].low_sample is False

    assert recs["3119"].games == 850
    assert recs["3121"].games == 850
    assert recs["3119"].low_sample is False

    assert recs["2526"].games == 250
    assert recs["2530"].games == 250
    assert recs["2526"].low_sample is True


def test_tear_backfill_graceful_degradation_when_raw_missing(tmp_path: Path) -> None:
    ds_dir = tmp_path / "16.23" / "normal"
    _write_json(
        ds_dir / "heroes.json",
        {"13": {"name": "瑞兹", "title": "符文法师", "games": 100000, "wins": 52000, "win_rate": 0.52, "pick_rate": 0.04, "rank": 20}},
    )
    _write_json(ds_dir / "augments.json", {"1002": {"name": "尖端发明家", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {"13": {}})
    _write_json(ds_dir / "core_builds.json", {"13": []})
    _write_json(
        ds_dir / "hero_item_recommendations.json",
        {"13": {"3003": {"games": 1000, "wins": 380, "win_rate": 0.38, "low_sample": False}}},
    )

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    recs = store.get_hero_item_recs("13")
    assert recs["3003"].games == 1000
    assert recs["3003"].win_rate == pytest.approx(0.38, abs=1e-6)
    assert recs["3040"].games == 1000


@pytest.mark.skipif(
    not ((LIVE_1623_NORMAL / "heroes.json").is_file() and LIVE_1623_RAW.is_file()),
    reason="Real 16.23/normal dataset or 1623_hero_items_raw.json not present locally",
)
def test_tear_backfill_real_1623_normal_g1_targets(tmp_path: Path) -> None:
    staged_dir = _stage_1623_normal_with_raw(tmp_path)
    recs_raw = json.loads((LIVE_1623_NORMAL / "hero_item_recommendations.json").read_text(encoding="utf-8"))
    items_raw = json.loads(LIVE_1623_RAW.read_text(encoding="utf-8"))

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), staged_dir)

    # Verify G1 winrate repair (+5% to +14.7%+) for Ryze / Cassio / Jayce / EZ + all 4 Tear pairs
    checks = (
        ("13", "3003", "3040", 0.140, 0.152),   # Ryze ~+14.7%
        ("69", "3003", "3040", 0.095, 0.110),   # Cassio ~+10.2%
        ("126", "3004", "3042", 0.075, 0.090),  # Jayce ~+8.2%
        ("81", "3004", "3042", 0.050, 0.060),   # EZ ~+5.4%
        ("40", "2526", "2530", 0.115, 0.130),   # Janna ~+12.4%
        ("57", "3119", "3121", 0.045, 0.055),   # Maokai ~+4.95%
    )
    for hid, bid, sid, min_lift, max_lift in checks:
        old_wr = recs_raw[hid][bid]["wins"] / recs_raw[hid][bid]["games"]
        merged_b = store.get_hero_item_recs(hid)[bid]
        merged_s = store.get_hero_item_recs(hid)[sid]
        assert merged_b.games == merged_s.games
        assert merged_b.win_rate == pytest.approx(merged_s.win_rate, abs=1e-9)
        assert merged_b.games > recs_raw[hid][bid]["games"] * 5
        lift = merged_b.win_rate - old_wr
        assert min_lift <= lift <= max_lift, f"Hero {hid} {bid}/{sid} lift={lift:.4f} out of [{min_lift}, {max_lift}]"

    # Verify non-Tear intersection scale error < 7% on core benchmark heroes
    from hex_helper.data.provider import ItemRecStat

    for hid, bid in (("13", "3003"), ("69", "3003"), ("81", "3004"), ("40", "2526"), ("57", "3119")):
        inner = {
            k: ItemRecStat(k, int(v["games"]), int(v["wins"]), float(v["win_rate"]), bool(v.get("low_sample", False)))
            for k, v in recs_raw[hid].items()
        }
        raw_h = {k: (int(v["games"]), int(v["wins"])) for k, v in items_raw[hid].items() if int(v.get("games", 0)) > 0}
        scale = compute_tear_backfill_scale(inner, raw_h)
        true_scale = inner[bid].games / raw_h[bid][0]
        rel_err = abs(scale - true_scale) / true_scale
        assert rel_err < 0.07, f"Hero {hid} scale={scale:.4f} vs true={true_scale:.4f} (rel_err={rel_err:.3%})"


# ---------------------------------------------------------------------------
# P0-2: Mainstream Item Global Bias (g >= 1000 & PR >= 8%, weighted, [-0.06, 0.06])
# ---------------------------------------------------------------------------


def test_item_global_bias_mainstream_filter_weighted_mean_and_clamp(tmp_path: Path) -> None:
    ds_dir = tmp_path / "ds_bias"
    heroes = {
        "99": {"name": "拉克丝", "title": "光辉", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 20},
        "101": {"name": "泽拉斯", "title": "远古巫灵", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 30},
        "63": {"name": "布兰德", "title": "复仇焰魂", "games": 5000, "wins": 2500, "win_rate": 0.50, "pick_rate": 0.01, "rank": 80},
    }
    item_recs = {
        "99": {
            # Item 6655: mainstream (PR 30% >= 8%, g=30000 >= 1000), delta = +0.02
            "6655": {"games": 30000, "wins": 15600, "win_rate": 0.52, "low_sample": False},
            # Item 3165: niche PR 5% (< 8%) despite g=5000 -> MUST BE EXCLUDED
            "3165": {"games": 5000, "wins": 2100, "win_rate": 0.42, "low_sample": False},
            # Item 3089: extreme positive delta +0.10 -> MUST CLAMP to +0.06
            "3089": {"games": 10000, "wins": 6000, "win_rate": 0.60, "low_sample": False},
            # Item 3075: extreme negative delta -0.09 -> MUST CLAMP to -0.06
            "3075": {"games": 10000, "wins": 4100, "win_rate": 0.41, "low_sample": False},
        },
        "101": {
            # Item 6655: mainstream (PR 10% >= 8%, g=10000 >= 1000), delta = +0.06
            # Sample-weighted mean = (0.02*30000 + 0.06*10000) / 40000 = 0.03 (unweighted would be 0.04!)
            "6655": {"games": 10000, "wins": 5600, "win_rate": 0.56, "low_sample": False},
            "3165": {"games": 5000, "wins": 2100, "win_rate": 0.42, "low_sample": False},
            "3089": {"games": 10000, "wins": 6000, "win_rate": 0.60, "low_sample": False},
            "3075": {"games": 10000, "wins": 4100, "win_rate": 0.41, "low_sample": False},
        },
        "63": {
            # Item 4645: PR 16% >= 8%, but g=800 < 1000 -> MUST BE EXCLUDED
            "4645": {"games": 800, "wins": 480, "win_rate": 0.60, "low_sample": False},
        },
    }
    _write_json(ds_dir / "heroes.json", heroes)
    _write_json(ds_dir / "augments.json", {"1002": {"name": "尖端发明家", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {h: {} for h in heroes})
    _write_json(ds_dir / "core_builds.json", {h: [] for h in heroes})
    _write_json(ds_dir / "hero_item_recommendations.json", item_recs)

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    assert store.get_item_global_bias("6655") == pytest.approx(0.03, abs=1e-6)
    assert store.get_item_global_bias("3165") == 0.0
    assert store.get_item_global_bias("4645") == 0.0
    assert store.get_item_global_bias("3089") == pytest.approx(0.06, abs=1e-6)
    assert store.get_item_global_bias("3075") == pytest.approx(-0.06, abs=1e-6)


@pytest.mark.skipif(not (LIVE_1623_NORMAL / "heroes.json").is_file(), reason="Real 16.23/normal dataset not present locally")
def test_item_global_bias_real_1623_clamped_and_mainstream(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), LIVE_1623_NORMAL)
    biases = store.item_global_bias
    assert 60 <= len(biases) <= 105
    for iid, val in biases.items():
        assert -0.06 <= val <= 0.06, f"Bias for {iid} out of [-0.06, 0.06]: {val}"


# ---------------------------------------------------------------------------
# P0-3: Native bands/high Sync, Store Blending & Config Switch
# ---------------------------------------------------------------------------


def test_sync_optional_raw_and_high_band_with_404_tolerance(tmp_path: Path) -> None:
    base_payloads = {
        "heroes.json": json.dumps({"222": {"name": "金克丝", "title": "暴走萝莉", "games": 50000, "wins": 25000, "win_rate": 0.50, "pick_rate": 0.05, "rank": 10}}).encode(),
        "augments.json": json.dumps({"3081": {"name": "命运之武器", "rarity": "gold"}}).encode(),
        "hero_augments.json": json.dumps({"222": {"3081": {"games": 1000, "wins": 530, "win_rate": 0.53}}}).encode(),
        "hero_spells.json": json.dumps({}).encode(),
        "core_builds.json": json.dumps({"222": [{"items": ["3031", "3085"], "ordered": True, "games": 5000, "wins": 2550, "win": 0.51, "pick": 0.2}]}).encode(),
        "hero_augment_items.json": json.dumps({}).encode(),
        "hero_item_recommendations.json": json.dumps({"222": {"3031": {"games": 5000, "wins": 2550, "win_rate": 0.51}}}).encode(),
        "item_metadata.json": json.dumps({}).encode(),
        "hero_items_raw.json": json.dumps({"222": {"3031": {"games": 6000, "wins": 3060, "win_rate": 0.51}}}).encode(),
        "bands/high/heroes.json": json.dumps({"222": {"name": "金克丝", "title": "暴走萝莉", "games": 5000, "wins": 2650, "win_rate": 0.53, "pick_rate": 0.05, "rank": 10}}).encode(),
        "bands/high/hero_augments.json": json.dumps({"222": {"3081": {"games": 400, "wins": 220, "win_rate": 0.55}}}).encode(),
        "bands/high/hero_item_recommendations.json": json.dumps({"222": {"3031": {"games": 800, "wins": 440, "win_rate": 0.55}}}).encode(),
        "bands/high/core_builds.json": json.dumps({"222": [{"items": ["3085", "3031"], "ordered": True, "games": 600, "wins": 330, "win": 0.55, "pick": 0.3}]}).encode(),
        "bands/high/hero_augment_items.json": json.dumps({}).encode(),
    }
    # Simulate bands/high/hero_items_raw.json listed in manifest but returning 404 from CDN!
    files_manifest = {k: sha256_bytes(v) for k, v in base_payloads.items()}
    files_manifest["bands/high/hero_items_raw.json"] = "a" * 64

    meta = {
        "snapshot_at": "2026-11-26T09:00:00+08:00",
        "bands": {"bands": {"high": {"min_score": 2400}}},
        "files": files_manifest,
    }
    index = {
        "datasets": [
            {
                "patch": "16.22",
                "mode": "normal",
                "mode_label": "常规",
                "path": "patches/16.22/normal",
                "snapshot_at": "2026-11-26T09:00:00+08:00",
                "bands": True,
                "seq": 10,
            }
        ]
    }
    latest = {"patch": "16.22", "mode": "normal", "path": "patches/16.22/normal", "seq": 10}
    remote = {
        "https://mock.test/index.json": json.dumps(index).encode(),
        "https://mock.test/latest.json": json.dumps(latest).encode(),
        "https://mock.test/patches/16.22/normal/meta.json": json.dumps(meta).encode(),
        **{f"https://mock.test/patches/16.22/normal/{k}": v for k, v in base_payloads.items()},
    }

    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        if url not in remote:
            raise FileNotFoundError(f"404 Not Found: {url}")
        return remote[url]

    syncer = AramHexSyncer(cache_dir=tmp_path, base_url="https://mock.test", fetcher=fetcher)
    desc, local_dir = syncer.sync_dataset("latest", "normal")
    assert desc.patch == "16.22"
    assert OPTIONAL_DATASET_FILES == ("hero_items_raw.json",)
    assert "bands/high/hero_items_raw.json" in OPTIONAL_HIGH_BAND_FILES
    assert (local_dir / "hero_items_raw.json").is_file()
    for bf in HIGH_BAND_DATASET_FILES:
        assert (local_dir / bf).is_file(), f"Missing synced high-band file {bf}"
    assert not (local_dir / "bands/high/hero_items_raw.json").exists()

    # Second sync skips all SHA-256 matched files (only index, latest, meta, and missing optional raw)
    calls.clear()
    syncer.sync_dataset("latest", "normal")
    assert len(calls) == 4
    assert all(
        u.endswith(("index.json", "latest.json", "meta.json", "bands/high/hero_items_raw.json"))
        for u in calls
    )

    # Also verify root hero_items_raw.json HTTP 404 + boolean bands=True without manifest entries degrades cleanly
    tmp2 = tmp_path / "case_root_404"
    meta_bool = {"snapshot_at": "2026-11-26T09:00:00+08:00", "bands": True, "files": {}}
    remote2 = {
        "https://mock.test/index.json": json.dumps(index).encode(),
        "https://mock.test/latest.json": json.dumps(latest).encode(),
        "https://mock.test/patches/16.22/normal/meta.json": json.dumps(meta_bool).encode(),
        **{
            f"https://mock.test/patches/16.22/normal/{k}": v
            for k, v in base_payloads.items()
            if k not in ("hero_items_raw.json", "bands/high/hero_augment_items.json")
        },
    }

    def fetcher_404(url: str) -> bytes:
        if url not in remote2:
            raise FileNotFoundError(f"HTTP 404: {url}")
        return remote2[url]

    syncer2 = AramHexSyncer(cache_dir=tmp2, base_url="https://mock.test", fetcher=fetcher_404)
    _, local_dir2 = syncer2.sync_dataset("latest", "normal")
    assert (local_dir2 / "heroes.json").is_file()
    assert not (local_dir2 / "hero_items_raw.json").exists()
    assert (local_dir2 / "bands/high/hero_item_recommendations.json").is_file()
    assert not (local_dir2 / "bands/high/hero_augment_items.json").exists()


def test_bands_high_smooth_low_sample_decay_and_auto_fallback(tmp_path: Path) -> None:
    ds_dir = tmp_path / "16.22" / "normal"
    _write_json(
        ds_dir / "heroes.json",
        {"105": {"name": "菲兹", "title": "潮汐海灵", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.04, "rank": 20}},
    )
    _write_json(ds_dir / "augments.json", {"1002": {"name": "尖端发明家", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {"105": {"1002": {"games": 5000, "wins": 2500, "win_rate": 0.50}}})
    _write_json(
        ds_dir / "core_builds.json",
        {"105": [{"items": ["4645", "3100"], "ordered": True, "games": 20000, "wins": 10000, "win": 0.50, "pick": 0.40}]},
    )
    _write_json(
        ds_dir / "hero_item_recommendations.json",
        {
            "105": {
                "3100": {"games": 20000, "wins": 10000, "win_rate": 0.50, "low_sample": False},
                "4645": {"games": 20000, "wins": 10000, "win_rate": 0.50, "low_sample": False},
                "3157": {"games": 10000, "wins": 5000, "win_rate": 0.50, "low_sample": False},
            }
        },
    )

    # High band: 3100 has 600 games (full weight), 4645 has 150 games (0.5x decay), 3157 has 60 games (0.2x decay)
    hi_dir = ds_dir / "bands" / "high"
    _write_json(
        hi_dir / "heroes.json",
        {"105": {"name": "菲兹", "title": "潮汐海灵", "games": 10000, "wins": 5200, "win_rate": 0.52, "pick_rate": 0.04, "rank": 15}},
    )
    _write_json(
        hi_dir / "core_builds.json",
        {"105": [{"items": ["3100", "4645"], "ordered": True, "games": 2000, "wins": 1080, "win": 0.54, "pick": 0.45}]},
    )
    _write_json(
        hi_dir / "hero_item_recommendations.json",
        {
            "105": {
                "3100": {"games": 600, "wins": 360, "win_rate": 0.60, "low_sample": False},
                "4645": {"games": 150, "wins": 90, "win_rate": 0.60, "low_sample": True},
                "3157": {"games": 60, "wins": 36, "win_rate": 0.60, "low_sample": True},
                "3003": {"games": 100, "wins": 40, "win_rate": 0.40, "low_sample": True},
            }
        },
    )
    _write_json(
        hi_dir / "hero_items_raw.json",
        {
            "105": {
                "3100": {"games": 1200, "wins": 720, "win_rate": 0.60},  # scale = 600 / 1200 = 0.50
                "3040": {"games": 1000, "wins": 580, "win_rate": 0.58},  # scaled: 500g, 290w -> merged 600g, 330w (0.55)
            }
        },
    )

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.22", "normal", "常规", "patches/16.22/normal", "", True), ds_dir)
    assert store.has_high_band() is True
    assert store.has_high_band("normal") is True
    assert store.has_high_band("nonexistent") is False

    # band_mode="all" -> exact all-rank
    recs_all = store.get_hero_item_recs("105", band_mode="all")
    assert recs_all["3100"].win_rate == pytest.approx(0.50, abs=1e-6)

    # band_mode="high" (high_weight=1.0): w_eff = 1.0 * min(1, g/300)
    recs_hi = store.get_hero_item_recs("105", band_mode="high")
    assert recs_hi["3100"].win_rate == pytest.approx(0.60, abs=1e-6)  # w_eff = 1.0
    assert recs_hi["4645"].win_rate == pytest.approx(0.55, abs=1e-6)  # w_eff = 0.5
    assert recs_hi["3157"].win_rate == pytest.approx(0.52, abs=1e-6)  # w_eff = 0.2
    # Tear backfill also applied inside bands/high/ when bands/high/hero_items_raw.json is present
    assert recs_hi["3003"].games == 600
    assert recs_hi["3003"].win_rate == pytest.approx(330 / 600, abs=1e-6)
    assert recs_hi["3040"].games == 600

    # band_mode="blended" (high_weight=0.5): w_eff = 0.5 * min(1, g/300)
    recs_mid = store.get_hero_item_recs("105", band_mode="blended")
    assert recs_mid["3100"].win_rate == pytest.approx(0.55, abs=1e-6)   # w_eff = 0.5
    assert recs_mid["4645"].win_rate == pytest.approx(0.525, abs=1e-6)  # w_eff = 0.25
    assert recs_mid["3157"].win_rate == pytest.approx(0.51, abs=1e-6)   # w_eff = 0.10

    # Core builds switch smoothly
    assert store.get_core_builds("105", band_mode="all")[0].items == ("4645", "3100")
    assert store.get_core_builds("105", band_mode="high")[0].items == ("3100", "4645")

    # Auto-fallback when snapshot lacks bands/high
    no_band_dir = tmp_path / "16.23_noband" / "normal"
    for fn in ("heroes.json", "augments.json", "hero_augments.json", "core_builds.json", "hero_item_recommendations.json"):
        shutil.copy2(ds_dir / fn, (no_band_dir / fn).parent.mkdir(parents=True, exist_ok=True) or (no_band_dir / fn))
    store2 = DataStore(static)
    store2.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), no_band_dir)
    assert store2.has_high_band() is False
    assert store2.get_hero_item_recs("105", band_mode="high") == store2.get_hero_item_recs("105", band_mode="all")


@pytest.mark.skipif(_find_live_1622_dir() is None, reason="Local 16.22/normal dataset with bands/high not present")
def test_bands_high_real_1622_loading_and_decay(tmp_path: Path) -> None:
    d22 = _find_live_1622_dir()
    assert d22 is not None
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.22", "normal", "常规", "patches/16.22/normal", "", True), d22)

    assert store.has_high_band() is True
    # Ezreal (81): high-band WR is noticeably higher than all-rank WR
    ez_all = store.get_hero_item_recs("81", band_mode="all")["3078"]
    ez_mid = store.get_hero_item_recs("81", band_mode="blended")["3078"]
    ez_hi = store.get_hero_item_recs("81", band_mode="high")["3078"]
    assert ez_all.win_rate < ez_mid.win_rate < ez_hi.win_rate

    # Find a genuinely low-sample (<300 games) high-band item and verify smooth decay between all and raw high
    raw_hi_recs = json.loads((d22 / "bands" / "high" / "hero_item_recommendations.json").read_text(encoding="utf-8"))
    found_low = False
    for hid, hmap in raw_hi_recs.items():
        all_map = store.get_hero_item_recs(hid, band_mode="all")
        hi_map = store.get_hero_item_recs(hid, band_mode="high")
        for iid, st in hmap.items():
            hg = int(st.get("games", 0))
            if 10 <= hg <= 150 and iid in all_map and iid not in TEAR_ALL_IDS:
                a_wr = all_map[iid].win_rate
                h_wr = float(st["win_rate"])
                if abs(h_wr - a_wr) >= 0.02:
                    w_eff = hg / 300.0
                    expected_wr = (1.0 - w_eff) * a_wr + w_eff * h_wr
                    assert hi_map[iid].win_rate == pytest.approx(expected_wr, abs=1e-5)
                    found_low = True
                    break
        if found_low:
            break
    assert found_low, "Expected at least one low-sample high-band item in 16.22/normal to verify smooth decay"


def test_config_data_band_mode_validation_and_persistence(tmp_path: Path) -> None:
    assert DATA_BAND_MODES == ("all", "blended", "high")
    default_cfg = AppConfig()
    assert default_cfg.data_band_mode == "all"
    assert default_cfg.dataset_band == ""

    for valid in DATA_BAND_MODES:
        c = AppConfig.from_dict({"data_band_mode": valid.upper(), "dataset_band": "high"})
        assert c.data_band_mode == valid
        assert c.dataset_band == "high"
        assert c.to_dict()["data_band_mode"] == valid

    invalid = AppConfig.from_dict({"data_band_mode": "challenger_only"})
    assert invalid.data_band_mode == "all"

    cfg_path = tmp_path / "cfg.json"
    save_config(AppConfig(data_band_mode="blended", dataset_band=""), cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.data_band_mode == "blended"
    assert loaded.dataset_band == ""


# ---------------------------------------------------------------------------
# P0-6: destiny_style_v1 Baseline & Control Parsing (Synthetic + Real 16.23)
# ---------------------------------------------------------------------------


def test_destiny_style_v1_synthetic_and_legacy_compatibility(tmp_path: Path) -> None:
    ds_dir = tmp_path / "ds_destiny"
    _write_json(
        ds_dir / "heroes.json",
        {
            "84": {"name": "阿卡丽", "title": "离群之刺", "games": 100000, "wins": 43820, "win_rate": 0.4382, "pick_rate": 0.03, "rank": 150},
            "222": {"name": "金克丝", "title": "暴走萝莉", "games": 100000, "wins": 48000, "win_rate": 0.48, "pick_rate": 0.05, "rank": 60},
        },
    )
    _write_json(ds_dir / "augments.json", {"3081": {"name": "命运之武器", "rarity": "gold"}})
    _write_json(ds_dir / "hero_augments.json", {"84": {}, "222": {}})
    _write_json(ds_dir / "core_builds.json", {"84": [], "222": []})
    _write_json(ds_dir / "hero_item_recommendations.json", {"84": {}, "222": {}})
    _write_json(
        ds_dir / "hero_augment_items.json",
        {
            "84": {
                "3081": {
                    # destiny_style_v1: NO top-level baseline; item carries control & style
                    "recommendation_style": "destiny_style_v1",
                    "items": [
                        {
                            "item": "3146",
                            "name": "海克斯科技枪刃",
                            "games": 4000,
                            "wins": 1979,
                            "low_sample": False,
                            "control": {"games": 80000, "wins": 34424},
                            "style": "法强",
                        }
                    ],
                }
            },
            "222": {
                "3081": {
                    # Legacy format with top-level baseline and no control/style
                    "baseline": {"games": 50000, "wins": 24000},
                    "items": [{"item": "3153", "name": "破败王者之刃", "games": 10000, "wins": 5500, "low_sample": False}],
                }
            },
        },
    )

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)

    akali = store.get_destiny_data("84")
    assert akali is not None
    assert akali.baseline_games == 100000
    assert akali.baseline_wins == 43820
    assert akali.baseline_win_rate == pytest.approx(0.4382, abs=1e-6)
    it = akali.items[0]
    assert it.control_games == 80000
    assert it.control_wins == 34424
    assert it.control_win_rate == pytest.approx(34424 / 80000, abs=1e-6)
    assert it.style == "法强"

    jinx = store.get_destiny_data("222")
    assert jinx is not None
    assert jinx.baseline_games == 50000
    assert jinx.baseline_win_rate == pytest.approx(0.48, abs=1e-6)
    assert jinx.items[0].control_games == 0
    assert jinx.items[0].control_win_rate is None
    assert jinx.items[0].style == ""


@pytest.mark.skipif(not (LIVE_1623_NORMAL / "heroes.json").is_file(), reason="Real 16.23/normal dataset not present locally")
def test_destiny_style_v1_real_1623_all_173_heroes_no_half_fallback(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), LIVE_1623_NORMAL)

    heroes = store.all_heroes()
    assert len(heroes) == 173
    total_items = 0
    for h in heroes:
        d = store.get_destiny_data(h.hero_id)
        assert d is not None, f"Missing DestinyData for hero {h.hero_id} ({h.name})"
        assert d.baseline_games == h.games
        assert abs(d.baseline_win_rate - 0.50) > 1e-6, f"Hero {h.hero_id} regressed to 0.50 baseline!"
        assert d.baseline_win_rate == pytest.approx(h.win_rate, abs=1e-4)
        for it in d.items:
            total_items += 1
            assert it.control_games > 0
            assert it.control_win_rate is not None and 0.1 < it.control_win_rate < 0.9
            assert it.style != ""
    assert total_items >= 170

    # Spot-check Akali (84), Yasuo (157), Ashe (22)
    akali = store.get_destiny_data("84")
    assert akali is not None and akali.items
    assert akali.items[0].win_rate - akali.baseline_win_rate > 0.04  # +5.65%, NOT -0.53%!
    yasuo = store.get_destiny_data("157")
    assert yasuo is not None and yasuo.items
    assert 0.02 <= (yasuo.items[0].win_rate - yasuo.baseline_win_rate) <= 0.05  # +3.53%, NOT +12.11%!
    ashe = store.get_destiny_data("22")
    assert ashe is not None and ashe.items
    assert ashe.items[0].win_rate - ashe.baseline_win_rate < 0.0  # -1.17%, NOT +3.98%!


# ---------------------------------------------------------------------------
# P0-4: Authoritative 173-Hero Augment Pool API (Synthetic + Real 16.23)
# ---------------------------------------------------------------------------


def test_authoritative_augment_pool_synthetic_fail_open_and_thresholds(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")

    # 1. Empty store fails open safely
    empty_store = DataStore(static)
    assert empty_store.is_augment_eligible_for_hero("11", "1134") is True
    assert empty_store.is_augment_eligible_for_hero(None, "1134") is True
    assert empty_store.get_hero_eligible_augment_ids("11") == frozenset()
    assert empty_store.get_top_eligible_augments("11") == []

    # 2. Tiny synthetic fixture (hero.games < 500) handles gracefully without falsely killing small fixtures
    tiny_dir = tmp_path / "tiny" / "normal"
    _write_json(tiny_dir / "heroes.json", {"11": {"name": "易", "title": "无极剑圣", "games": 100, "wins": 50, "win_rate": 0.50, "pick_rate": 0.02, "rank": 50}})
    _write_json(tiny_dir / "augments.json", {"1001": {"name": "测试A", "rarity": "gold"}, "1134": {"name": "亮出你的剑", "rarity": "prismatic"}})
    _write_json(tiny_dir / "hero_augments.json", {"11": {"1001": {"games": 10, "wins": 6, "win_rate": 0.60}, "1134": {"games": 0, "wins": 0, "win_rate": 0.50}}})
    _write_json(tiny_dir / "core_builds.json", {"11": []})
    _write_json(tiny_dir / "hero_item_recommendations.json", {"11": {}})
    tiny_store = DataStore(static)
    tiny_store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), tiny_dir)
    assert tiny_store.is_augment_eligible_for_hero("11", "1001") is True
    assert tiny_store.is_augment_eligible_for_hero("11", "1134") is False

    # 3. Realistic dual-mode fixture (hero.games >= 500): enforces g >= 20 AND g / hero_g >= 0.00008 across modes
    base = tmp_path / "dual"
    for mode, h_games, aug_map in (
        (
            "normal",
            500000,
            {
                "1001": {"games": 5000, "wins": 2700, "win_rate": 0.54},  # eligible (1.0%)
                "1002": {"games": 30, "wins": 15, "win_rate": 0.50},      # PR = 0.00006 < 0.00008 -> NOT eligible in normal
                "1003": {"games": 15, "wins": 8, "win_rate": 0.53},       # games = 15 < 20 -> NOT eligible
            },
        ),
        (
            "first-prismatic",
            200000,
            {
                "1002": {"games": 25, "wins": 13, "win_rate": 0.52},      # g=25 >= 20 & PR=0.000125 >= 0.00008 -> ELIGIBLE via fp!
                "1003": {"games": 10, "wins": 5, "win_rate": 0.50},       # still < 20 -> NOT eligible
            },
        ),
    ):
        md = base / mode
        _write_json(md / "heroes.json", {"126": {"name": "杰斯", "title": "未来守护者", "games": h_games, "wins": h_games // 2, "win_rate": 0.50, "pick_rate": 0.03, "rank": 50}})
        _write_json(md / "augments.json", {"1001": {"name": "A", "rarity": "gold"}, "1002": {"name": "B", "rarity": "prismatic"}, "1003": {"name": "C", "rarity": "silver"}})
        _write_json(md / "hero_augments.json", {"126": aug_map})
        _write_json(md / "core_builds.json", {"126": []})
        _write_json(md / "hero_item_recommendations.json", {"126": {}})

    dual_store = DataStore(static)
    dual_store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), base / "normal")
    assert dual_store.has_first_prismatic is True
    assert dual_store.is_augment_eligible_for_hero("126", "1001") is True
    assert dual_store.is_augment_eligible_for_hero("126", "1002") is True  # rescued by first-prismatic!
    assert dual_store.is_augment_eligible_for_hero("126", "1003") is False
    assert dual_store.get_hero_eligible_augment_ids("126") == frozenset({"1001", "1002"})


@pytest.mark.skipif(
    not ((LIVE_1623_NORMAL / "heroes.json").is_file() and (LIVE_1623_FP / "heroes.json").is_file()),
    reason="Real 16.23/normal and 16.23/first-prismatic datasets not present locally",
)
def test_authoritative_augment_pool_real_1623(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), LIVE_1623_NORMAL)
    assert store.has_first_prismatic is True

    # 1134 亮出你的剑: strictly 31 eligible heroes!
    eligible_1134 = [h.hero_id for h in store.all_heroes() if store.is_augment_eligible_for_hero(h.hero_id, "1134")]
    assert len(eligible_1134) == 31

    # Melee / dual-form exclusions
    for excluded_hid in ("11", "126", "76", "157"):  # Yi, Jayce, Nidalee, Yasuo
        assert store.is_augment_eligible_for_hero(excluded_hid, "1134") is False
        assert "1134" not in store.get_hero_eligible_augment_ids(excluded_hid)

    # Edge-case eligible ranged / special heroes
    for included_hid in ("360", "6", "133", "412", "4"):  # Samira, Urgot, Quinn, Thresh, TF
        assert store.is_augment_eligible_for_hero(included_hid, "1134") is True
        assert "1134" in store.get_hero_eligible_augment_ids(included_hid)

    # Rare-but-legit augments MUST NOT be falsely killed (Kai'Sa Flashbang 1187, Karthus Flashy 1348)
    assert store.is_augment_eligible_for_hero("145", "1187") is True
    assert store.is_augment_eligible_for_hero("30", "1348") is True

    # get_top_eligible_augments returns valid ranked eligible tuples and respects rarity filter
    top_yi = store.get_top_eligible_augments("11", limit=8)
    assert len(top_yi) == 8
    yi_pool = store.get_hero_eligible_augment_ids("11")
    for aid, delta, pct in top_yi:
        assert aid in yi_pool
        assert aid != "1134"
        assert -0.5 < delta < 0.5
        assert 0.0 <= pct <= 100.0

    top_gold = store.get_top_eligible_augments("11", limit=5, rarity="gold")
    assert len(top_gold) == 5
    for aid, _, _ in top_gold:
        aug = store.get_augment(aid)
        assert aug is not None and aug.rarity == "gold"
