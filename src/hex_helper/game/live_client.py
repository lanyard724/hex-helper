"""Strict read-only client for Riot's official Live Client Data API (127.0.0.1:2999)."""

from __future__ import annotations

import logging
import socket
from typing import Any

import httpx

from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo

logger = logging.getLogger(__name__)

LIVE_CLIENT_URL = "https://127.0.0.1:2999/liveclientdata/allgamedata"

_SPELL_NORMALIZATION: tuple[tuple[tuple[str, ...], str], ...] = (
    (("summonersnowball", "snowball", "mark", "标记", "雪球", "冲刺"), "雪球"),
    (("summonerflash", "flash", "闪现"), "闪现"),
    (("summonerexhaust", "exhaust", "虚弱"), "虚弱"),
    (("summonerboost", "cleanse", "净化"), "净化"),
    (("summonerbarrier", "barrier", "屏障"), "屏障"),
    (("summonerheal", "heal", "治疗"), "治疗"),
    (("summonerhaste", "ghost", "幽灵疾步", "疾跑"), "疾跑"),
    (("summonerdot", "ignite", "引燃", "点燃"), "点燃"),
    (("summonermana", "clarity", "清晰"), "清晰"),
)


def _normalize_summoner_spell(spell_obj: dict[str, Any] | None) -> str:
    if not isinstance(spell_obj, dict):
        return ""
    raw_disp = str(spell_obj.get("displayName") or "").strip()
    raw_key = str(spell_obj.get("rawDisplayName") or "").strip()
    probe = f"{raw_key} {raw_disp}".lower()
    for tokens, label in _SPELL_NORMALIZATION:
        if any(tok in probe for tok in tokens):
            return label
    return raw_disp


def _extract_player_spells(raw_player: dict[str, Any]) -> tuple[str, ...]:
    sp_block = raw_player.get("summonerSpells")
    if not isinstance(sp_block, dict):
        return ()
    out: list[str] = []
    for k in ("summonerSpellOne", "summonerSpellTwo"):
        label = _normalize_summoner_spell(sp_block.get(k))
        if label:
            out.append(label)
    return tuple(out)


def _extract_turrets_and_result(payload: dict[str, Any], my_team: str) -> tuple[int, int, int]:
    """Count (ally_turrets_lost, enemy_turrets_lost, game_end_result) from LiveClientData events.Events."""
    events_block = payload.get("events")
    if not isinstance(events_block, dict):
        return 0, 0, -1
    events = events_block.get("Events")
    if not isinstance(events, list):
        return 0, 0, -1

    ally_prefix = "Turret_T1_" if my_team == "ORDER" else "Turret_T2_"
    enemy_prefix = "Turret_T2_" if my_team == "ORDER" else "Turret_T1_"
    ally_lost = 0
    enemy_lost = 0
    game_end_result = -1
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ev_name = ev.get("EventName")
        if ev_name == "TurretKilled":
            turret = str(ev.get("TurretKilled") or "")
            if turret.startswith(ally_prefix):
                ally_lost += 1
            elif turret.startswith(enemy_prefix):
                enemy_lost += 1
        elif ev_name == "GameEnd":
            res = str(ev.get("Result") or "").strip().lower()
            if res == "win":
                game_end_result = 1
            elif res in ("lose", "loss", "defeat"):
                game_end_result = 0
    return ally_lost, enemy_lost, game_end_result


def parse_allgamedata(payload: dict[str, Any]) -> LiveMatchSnapshot | None:
    """Parse Riot Live Client Data /allgamedata JSON into LiveMatchSnapshot."""
    if not isinstance(payload, dict):
        return None
    active = payload.get("activePlayer") or {}
    all_players_raw = payload.get("allPlayers") or []
    game_data = payload.get("gameData") or {}

    if not all_players_raw:
        return None

    active_ids = {
        str(active.get("riotId", "")).strip().lower(),
        str(active.get("summonerName", "")).strip().lower(),
        str(active.get("riotIdGameName", "")).strip().lower(),
    }
    active_ids.discard("")

    players: list[PlayerLiveInfo] = []
    my_player: PlayerLiveInfo | None = None

    for p in all_players_raw:
        scores = p.get("scores") or {}
        items = tuple(
            str(it.get("itemID"))
            for it in (p.get("items") or [])
            if it.get("itemID") not in (None, 0)
        )
        riot_id = str(p.get("riotId") or p.get("summonerName") or "")
        raw_respawn = p.get("respawnTimer", 0.0)
        try:
            respawn_timer = max(0.0, round(float(raw_respawn or 0.0), 1))
        except (TypeError, ValueError):
            respawn_timer = 0.0
        info = PlayerLiveInfo(
            riot_id=riot_id,
            champion_name=str(p.get("championName") or ""),
            raw_champion_name=str(p.get("rawChampionName") or ""),
            team=str(p.get("team") or "ORDER"),
            is_dead=bool(p.get("isDead", False)),
            level=int(p.get("level", 1) or 1),
            kills=int(scores.get("kills", 0) or 0),
            deaths=int(scores.get("deaths", 0) or 0),
            assists=int(scores.get("assists", 0) or 0),
            item_ids=items,
            summoner_spells=_extract_player_spells(p),
            respawn_timer=respawn_timer,
        )
        players.append(info)
        p_keys = {
            riot_id.strip().lower(),
            str(p.get("summonerName", "")).strip().lower(),
            str(p.get("riotIdGameName", "")).strip().lower(),
        }
        p_keys.discard("")
        if active_ids & p_keys:
            my_player = info

    if my_player is None and players:
        my_player = players[0]

    my_team = my_player.team if my_player else "ORDER"
    allies = tuple(p for p in players if p.team == my_team)
    enemies = tuple(p for p in players if p.team != my_team)

    raw_stats = active.get("championStats") or {}
    my_stats = {
        str(k): float(v)
        for k, v in raw_stats.items()
        if isinstance(v, (int, float))
    }

    # Parse active player's ability levels & inferred maxed basic skill (Q/W/E)
    raw_abilities = active.get("abilities") or {}
    ability_levels: dict[str, int] = {}
    if isinstance(raw_abilities, dict):
        for slot in ("Q", "W", "E", "R"):
            ab = raw_abilities.get(slot)
            if isinstance(ab, dict):
                try:
                    lvl = int(ab.get("abilityLevel", 0) or 0)
                except (TypeError, ValueError):
                    lvl = 0
                if lvl > 0:
                    ability_levels[slot] = lvl
    basic_levels = [(slot, ability_levels.get(slot, 0)) for slot in ("Q", "W", "E")]
    basic_levels.sort(key=lambda pair: pair[1], reverse=True)
    my_maxed_ability = (
        basic_levels[0][0]
        if (basic_levels and basic_levels[0][1] >= 2 and basic_levels[0][1] > basic_levels[1][1])
        else ""
    )

    # Parse keystone rune name
    full_runes = active.get("fullRunes") or {}
    keystone_obj = full_runes.get("keystone") if isinstance(full_runes, dict) else None
    my_keystone = (
        str(keystone_obj.get("displayName") or "").strip()
        if isinstance(keystone_obj, dict)
        else ""
    )

    my_team_kills = sum(p.kills for p in allies)
    enemy_team_kills = sum(p.kills for p in enemies)
    ally_turrets_lost, enemy_turrets_lost, game_end_result = _extract_turrets_and_result(payload, my_team)

    return LiveMatchSnapshot(
        game_time=float(game_data.get("gameTime", 0.0) or 0.0),
        my_player=my_player,
        allies=allies,
        enemies=enemies,
        current_gold=float(active.get("currentGold", 0.0) or 0.0),
        my_stats=my_stats,
        my_ability_levels=ability_levels,
        my_maxed_ability=my_maxed_ability,
        my_keystone=my_keystone,
        my_team_kills=my_team_kills,
        enemy_team_kills=enemy_team_kills,
        ally_turrets_lost=ally_turrets_lost,
        enemy_turrets_lost=enemy_turrets_lost,
        game_end_result=game_end_result,
    )


class LiveClientReader:
    """Polls 127.0.0.1:2999/liveclientdata/allgamedata via read-only GET."""

    def __init__(self, url: str = LIVE_CLIENT_URL, timeout: float = 0.6) -> None:
        self.url = url
        self.last_error: str = ""
        self._client = httpx.Client(verify=False, timeout=timeout, trust_env=False)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def poll(self) -> LiveMatchSnapshot | None:
        # Fast local TCP check (~1-50ms) avoids 600ms HTTPS timeout when not currently in a match
        try:
            with socket.create_connection(("127.0.0.1", 2999), timeout=0.06):
                pass
        except OSError:
            return None
        try:
            resp = self._client.get(self.url)
            if resp.status_code != 200:
                return None
            self.last_error = ""
            return parse_allgamedata(resp.json())
        except Exception as exc:
            self.last_error = str(exc)
            return None
