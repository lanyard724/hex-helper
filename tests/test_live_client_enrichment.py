"""Tests for Task 10: LiveClientData enrichment (spells, abilities, keystone, respawn, turrets) & dive threat."""

from __future__ import annotations

from pathlib import Path

from hex_helper.data.provider import HeroStat, ItemStaticInfo
from hex_helper.data.store import DataStore, DatasetDescriptor
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.game.live_client import parse_allgamedata
from hex_helper.scoring.matchup_optimizer import analyze_matchup_threats


def _sample_allgamedata() -> dict:
    return {
        "activePlayer": {
            "riotId": "Alan#CN1",
            "currentGold": 1400.0,
            "abilities": {
                "Q": {"abilityLevel": 4},
                "W": {"abilityLevel": 1},
                "E": {"abilityLevel": 2},
                "R": {"abilityLevel": 1},
            },
            "fullRunes": {
                "keystone": {"displayName": "黑暗收割"},
            },
            "championStats": {"abilityPower": 85.0},
        },
        "gameData": {"gameTime": 420.5},
        "events": {
            "Events": [
                {"EventName": "GameStart", "EventTime": 0.0},
                {"EventName": "TurretKilled", "TurretKilled": "Turret_T2_C_05_A", "EventTime": 310.0},
                {"EventName": "TurretKilled", "TurretKilled": "Turret_T2_C_04_A", "EventTime": 380.0},
                {"EventName": "TurretKilled", "TurretKilled": "Turret_T1_C_05_A", "EventTime": 405.0},
            ]
        },
        "allPlayers": [
            {
                "riotId": "Alan#CN1",
                "championName": "阿狸",
                "rawChampionName": "game_character_displayname_Ahri",
                "team": "ORDER",
                "isDead": True,
                "respawnTimer": 18.4,
                "level": 9,
                "scores": {"kills": 5, "deaths": 2, "assists": 10},
                "items": [{"itemID": 6655}],
                "summonerSpells": {
                    "summonerSpellOne": {"displayName": "闪现", "rawDisplayName": "GeneratedTip_SummonerSpell_SummonerFlash_DisplayName"},
                    "summonerSpellTwo": {"displayName": "标记", "rawDisplayName": "GeneratedTip_SummonerSpell_SummonerSnowball_DisplayName"},
                },
            },
            {
                "riotId": "E1#CN1",
                "championName": "亚托克斯",
                "rawChampionName": "game_character_displayname_Aatrox",
                "team": "CHAOS",
                "isDead": False,
                "respawnTimer": 0.0,
                "level": 10,
                "scores": {"kills": 9, "deaths": 2, "assists": 6},
                "items": [{"itemID": 6610}],
                "summonerSpells": {
                    "summonerSpellOne": {"displayName": "闪现", "rawDisplayName": "SummonerFlash"},
                    "summonerSpellTwo": {"displayName": "雪球", "rawDisplayName": "SummonerSnowball"},
                },
            },
            {
                "riotId": "E2#CN1",
                "championName": "阿木木",
                "rawChampionName": "game_character_displayname_Amumu",
                "team": "CHAOS",
                "isDead": False,
                "level": 9,
                "scores": {"kills": 2, "deaths": 4, "assists": 12},
                "items": [],
                "summonerSpells": {
                    "summonerSpellOne": {"displayName": "闪现", "rawDisplayName": "SummonerFlash"},
                    "summonerSpellTwo": {"displayName": "标记", "rawDisplayName": "SummonerSnowball"},
                },
            },
            {
                "riotId": "E3#CN1",
                "championName": "蕾欧娜",
                "rawChampionName": "game_character_displayname_Leona",
                "team": "CHAOS",
                "isDead": False,
                "level": 9,
                "scores": {"kills": 1, "deaths": 5, "assists": 11},
                "items": [],
                "summonerSpells": {
                    "summonerSpellOne": {"displayName": "标记", "rawDisplayName": "SummonerSnowball"},
                    "summonerSpellTwo": {"displayName": "虚弱", "rawDisplayName": "SummonerExhaust"},
                },
            },
            {
                "riotId": "E4#CN1",
                "championName": "拉克丝",
                "rawChampionName": "game_character_displayname_Lux",
                "team": "CHAOS",
                "isDead": False,
                "level": 9,
                "scores": {"kills": 3, "deaths": 3, "assists": 8},
                "items": [],
                "summonerSpells": {
                    "summonerSpellOne": {"displayName": "闪现", "rawDisplayName": "SummonerFlash"},
                    "summonerSpellTwo": {"displayName": "虚弱", "rawDisplayName": "SummonerExhaust"},
                },
            },
        ],
    }


def test_parse_allgamedata_extracts_spells_abilities_keystone_respawn_and_turrets() -> None:
    snap = parse_allgamedata(_sample_allgamedata())
    assert snap is not None
    assert snap.my_player is not None
    assert snap.my_player.summoner_spells == ("闪现", "雪球")
    assert snap.my_player.respawn_timer == 18.4
    assert snap.my_ability_levels == {"Q": 4, "W": 1, "E": 2, "R": 1}
    assert snap.my_maxed_ability == "Q"
    assert snap.my_keystone == "黑暗收割"
    assert snap.my_team_kills == 5
    assert snap.enemy_team_kills == 15
    assert snap.ally_turrets_lost == 1
    assert snap.enemy_turrets_lost == 2


def test_analyze_matchup_threats_counts_snowballs_exhausts_and_top_threat_spells(tmp_path: Path) -> None:
    snap = parse_allgamedata(_sample_allgamedata())
    assert snap is not None
    static = StaticAssetsManager(tmp_path)
    static.items = {
        "6610": ItemStaticInfo("6610", "焚天", 3100, ("Damage",), {"FlatPhysicalDamageMod": 45.0, "FlatHPPoolMod": 400.0}, "", "complete_equipment"),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
    }
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "103": HeroStat("103", "阿狸", "九尾妖狐", 50000, 26000, 0.52, 0.05, 20, False),
    }

    threat = analyze_matchup_threats(store, snap.allies, snap.enemies)
    assert threat.enemy_snowball_count == 3
    assert threat.enemy_exhaust_count == 2
    assert threat.top_threat_name == "亚托克斯"
    assert "雪球" in threat.top_threat_spells
    assert any("3雪球强开" in t for t in threat.summary_tags)
    assert any("2虚弱防突" in t for t in threat.summary_tags)
