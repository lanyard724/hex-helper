import json
from pathlib import Path

import pytest

from hex_helper.data.aramhex_sync import AramHexSyncer, sha256_bytes
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore


def _make_mock_remote() -> dict[str, bytes]:
    heroes = {
        "222": {
            "name": "金克丝",
            "title": "暴走萝莉",
            "games": 458049,
            "wins": 220046,
            "win_rate": 0.4804,
            "pick_rate": 0.045,
            "rank": 123,
            "low_sample": False,
        }
    }
    augments = {
        "3081": {"name": "命运之武器", "rarity": "gold"},
        "3131": {"name": "屹立不倒", "rarity": "gold"},
    }
    hero_augments = {
        "222": {
            "3081": {"games": 46967, "wins": 24939, "win_rate": 0.531, "low_sample": False},
            "3131": {"games": 52922, "wins": 27281, "win_rate": 0.5155, "low_sample": False},
        }
    }
    hero_spells = {
        "222": {"4+6": {"games": 316721, "wins": 154433, "win_rate": 0.4876, "low_sample": False}}
    }
    core_builds = {
        "222": [
            {
                "items": ["3032", "3085", "3031"],
                "ordered": True,
                "games": 161249,
                "wins": 81576,
                "win": 0.5059,
                "pick": 0.058,
            }
        ]
    }
    hero_augment_items = {
        "222": {
            "3081": {
                "baseline": {"games": 100000, "wins": 47970},
                "items": [
                    {
                        "item": "3153",
                        "name": "破败王者之刃",
                        "games": 59511,
                        "wins": 32642,
                        "low_sample": False,
                    }
                ],
            }
        }
    }
    hero_item_recs = {
        "222": {"3153": {"games": 59511, "wins": 32642, "win_rate": 0.5485, "low_sample": False}}
    }
    item_meta = {
        "3153": {
            "category": "complete_equipment",
            "icon": "https://game.gtimg.cn/images/lol/act/img/item/3153.png",
        }
    }

    payloads = {
        "heroes.json": json.dumps(heroes).encode("utf-8"),
        "augments.json": json.dumps(augments).encode("utf-8"),
        "hero_augments.json": json.dumps(hero_augments).encode("utf-8"),
        "hero_spells.json": json.dumps(hero_spells).encode("utf-8"),
        "core_builds.json": json.dumps(core_builds).encode("utf-8"),
        "hero_augment_items.json": json.dumps(hero_augment_items).encode("utf-8"),
        "hero_item_recommendations.json": json.dumps(hero_item_recs).encode("utf-8"),
        "item_metadata.json": json.dumps(item_meta).encode("utf-8"),
    }
    meta = {
        "snapshot_at": "2026-11-20T08:00:00+08:00",
        "files": {k: sha256_bytes(v) for k, v in payloads.items()},
    }
    index = {
        "datasets": [
            {
                "patch": "16.23",
                "mode": "normal",
                "mode_label": "常规",
                "path": "patches/16.23/normal",
                "snapshot_at": "2026-11-20T08:00:00+08:00",
                "bands": False,
                "seq": 20,
                "matches_detail": {"valid": 4991976},
            }
        ]
    }
    latest = {"patch": "16.23", "mode": "normal", "path": "patches/16.23/normal", "seq": 20}

    remote: dict[str, bytes] = {
        "https://mock.test/index.json": json.dumps(index).encode("utf-8"),
        "https://mock.test/latest.json": json.dumps(latest).encode("utf-8"),
        "https://mock.test/patches/16.23/normal/meta.json": json.dumps(meta).encode("utf-8"),
    }
    for k, v in payloads.items():
        remote[f"https://mock.test/patches/16.23/normal/{k}"] = v
    return remote


def test_sync_sha256_skip_and_store_load(tmp_path: Path):
    remote = _make_mock_remote()
    calls: list[str] = []

    def fetcher(url: str) -> bytes:
        calls.append(url)
        return remote[url]

    syncer = AramHexSyncer(cache_dir=tmp_path, base_url="https://mock.test", fetcher=fetcher)
    desc, local_dir = syncer.sync_dataset("latest", "normal")
    assert desc.patch == "16.23"
    assert (local_dir / "heroes.json").exists()
    first_call_count = len(calls)

    # Second sync should skip all SHA-256 matched files (only fetches index/latest/meta)
    calls.clear()
    syncer.sync_dataset("latest", "normal")
    assert len(calls) == 3
    assert len(calls) < first_call_count

    # Load into DataStore and verify queries
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.hero_aliases["222"] = "Jinx"
    store = DataStore(static)
    store.load_from_dir(desc, local_dir)

    assert store.is_loaded
    assert store.get_hero("222").name == "金克丝"
    assert store.find_hero_by_name("game_character_displayname_Jinx").hero_id == "222"
    assert store.find_hero_by_name("金克丝").hero_id == "222"
    assert store.get_augment("3081").name == "命运之武器"
    assert store.get_destiny_data("222").items[0].item_id == "3153"
    assert store.get_core_builds("222")[0].items == ("3032", "3085", "3031")


def test_sync_rejects_corrupted_sha256(tmp_path: Path):
    remote = _make_mock_remote()
    remote["https://mock.test/patches/16.23/normal/heroes.json"] = b'{"tampered": true}'
    syncer = AramHexSyncer(cache_dir=tmp_path, base_url="https://mock.test", fetcher=lambda u: remote[u])
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        syncer.sync_dataset("latest", "normal")
    assert not (tmp_path / "datasets" / "16.23" / "normal" / "heroes.json").exists()
