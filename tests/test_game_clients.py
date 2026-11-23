from hex_helper.game.lcu_client import is_mayhem_session, parse_champ_select_session
from hex_helper.game.live_client import parse_allgamedata
from hex_helper.game.lockfile import parse_lockfile_text


def test_parse_lockfile():
    creds = parse_lockfile_text("LeagueClient:12345:54321:secretToken123:https")
    assert creds is not None
    assert creds.pid == 12345
    assert creds.port == 54321
    assert creds.password == "secretToken123"
    assert parse_lockfile_text("bad_content") is None


def test_mayhem_session_and_champ_select():
    qid, mode, is_m = is_mayhem_session({"gameData": {"queue": {"id": 2400, "type": "KIWI"}}})
    assert qid == 2400
    assert is_m is True

    _, _, is_m_classic = is_mayhem_session({"gameData": {"queue": {"id": 450, "type": "ARAM"}}})
    assert is_m_classic is False
    _, _, is_m_forced = is_mayhem_session({"gameData": {"queue": {"id": 450, "type": "ARAM"}}}, force_all_modes=True)
    assert is_m_forced is True

    cs = parse_champ_select_session(
        {
            "localPlayerCellId": 2,
            "myTeam": [
                {"cellId": 1, "championId": 101},
                {"cellId": 2, "championId": 222},
            ],
            "benchChampions": [{"championId": 10}, {"championId": 236}],
        }
    )
    assert cs.my_hero_id == "222"
    assert cs.team_hero_ids == ("101", "222")
    assert cs.bench_hero_ids == ("10", "236")


def test_parse_live_allgamedata():
    payload = {
        "activePlayer": {
            "riotId": "Alan#CN1",
            "currentGold": 2350.5,
            "championStats": {"attackDamage": 145.0, "armor": 68.0},
        },
        "gameData": {"gameTime": 420.0},
        "allPlayers": [
            {
                "riotId": "Alan#CN1",
                "championName": "金克丝",
                "rawChampionName": "game_character_displayname_Jinx",
                "team": "ORDER",
                "isDead": True,
                "level": 10,
                "scores": {"kills": 5, "deaths": 2, "assists": 7},
                "items": [{"itemID": 3085}, {"itemID": 3006}],
            },
            {
                "riotId": "Enemy1#CN1",
                "championName": "亚托克斯",
                "rawChampionName": "game_character_displayname_Aatrox",
                "team": "CHAOS",
                "isDead": False,
                "level": 11,
                "scores": {"kills": 8, "deaths": 1, "assists": 3},
                "items": [{"itemID": 3075}],
            },
        ],
    }
    snap = parse_allgamedata(payload)
    assert snap is not None
    assert snap.my_player.champion_name == "金克丝"
    assert snap.my_player.is_dead is True
    assert snap.current_gold == 2350.5
    assert snap.my_player.item_ids == ("3085", "3006")
    assert len(snap.allies) == 1
    assert len(snap.enemies) == 1
    assert snap.enemies[0].champion_name == "亚托克斯"
    assert snap.enemies[0].kills == 8
