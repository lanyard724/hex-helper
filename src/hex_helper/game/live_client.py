"""Strict read-only client for Riot's official Live Client Data API (127.0.0.1:2999)."""

from __future__ import annotations

import logging
import math
import socket
from typing import Any

import httpx

from hex_helper.game.models import ItemSlotInfo, LiveMatchSnapshot, PlayerLiveInfo

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


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if math.isfinite(val) else default
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            try:
                fval = float(text)
            except ValueError:
                return default
            return int(fval) if math.isfinite(fval) else default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        fval = float(val)
        return fval if math.isfinite(fval) else default
    except (TypeError, ValueError):
        return default


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


def _hero_key_from_player(p: PlayerLiveInfo) -> str:
    raw = str(p.raw_champion_name or "").strip()
    if raw:
        prefix = "game_character_displayname_"
        if raw.lower().startswith(prefix):
            return raw[len(prefix):]
        return raw
    return str(p.champion_name or "").strip()


def _sanitize_non_player_entity(raw_name: str) -> str:
    """Map known non-player engine entity names without ever leaking unknown summoner names."""
    low = raw_name.strip().lower()
    if not low:
        return ""
    if low.startswith("turret_") or "turret" in low and ("_t1_" in low or "_t2_" in low):
        return "turret"
    if low.startswith("minion_") or low.startswith("ha_chaosminion") or low.startswith("ha_orderminion"):
        return "minion"
    if low.startswith("sru_") or low.startswith("ha_ap_") or low.startswith("poro"):
        return "neutral"
    if low == "executed":
        return "executed"
    return ""


def _resolve_event_actor(
    raw_name: Any,
    name_lookup: dict[str, tuple[str, bool, bool]],
) -> tuple[str, bool, bool]:
    """Resolve an event actor name strictly to (hero_key, is_me, is_ally) with zero PII passthrough."""
    text = str(raw_name or "").strip()
    if not text:
        return "", False, False
    low = text.lower()
    hit = name_lookup.get(low)
    if hit is not None:
        return hit
    if "#" in low:
        base = low.split("#", 1)[0].strip()
        hit = name_lookup.get(base)
        if hit is not None:
            return hit
    return _sanitize_non_player_entity(text), False, False


def _extract_events_and_summary(
    payload: dict[str, Any],
    my_team: str,
    name_lookup: dict[str, tuple[str, bool, bool]],
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """Extract (ally_turrets_lost, enemy_turrets_lost, game_end_result, sanitized_raw_events)."""
    events_block = payload.get("events")
    if not isinstance(events_block, dict):
        return 0, 0, -1, []
    events = events_block.get("Events")
    if not isinstance(events, list):
        return 0, 0, -1, []

    ally_prefix = "Turret_T1_" if my_team == "ORDER" else "Turret_T2_"
    enemy_prefix = "Turret_T2_" if my_team == "ORDER" else "Turret_T1_"
    ally_lost = 0
    enemy_lost = 0
    game_end_result = -1
    sanitized_events: list[dict[str, Any]] = []

    for idx, ev in enumerate(events):
        if not isinstance(ev, dict):
            continue
        ev_name = str(ev.get("EventName") or "").strip()
        ev_id = _safe_int(ev.get("EventID"), idx) if ev.get("EventID") is not None else idx
        ev_time = round(_safe_float(ev.get("EventTime"), 0.0), 2)

        turret_killed = ""
        if ev_name == "TurretKilled":
            turret_killed = str(ev.get("TurretKilled") or "").strip()
            if turret_killed.startswith(ally_prefix):
                ally_lost += 1
            elif turret_killed.startswith(enemy_prefix):
                enemy_lost += 1
        elif ev_name == "GameEnd":
            res = str(ev.get("Result") or "").strip().lower()
            if res == "win":
                game_end_result = 1
            elif res in ("lose", "loss", "defeat"):
                game_end_result = 0

        killer_hero, killer_is_me, killer_is_ally = _resolve_event_actor(ev.get("KillerName"), name_lookup)
        victim_hero, victim_is_me, victim_is_ally = _resolve_event_actor(ev.get("VictimName"), name_lookup)

        assister_heroes: list[str] = []
        assister_is_me = False
        raw_assisters = ev.get("Assisters")
        if isinstance(raw_assisters, list):
            for a_raw in raw_assisters:
                a_hero, a_is_me, _ = _resolve_event_actor(a_raw, name_lookup)
                if a_hero:
                    assister_heroes.append(a_hero)
                if a_is_me:
                    assister_is_me = True

        rec: dict[str, Any] = {
            "event_id": ev_id,
            "event_name": ev_name,
            "event_time": ev_time,
            "killer_hero": killer_hero,
            "victim_hero": victim_hero,
            "assister_heroes": assister_heroes,
            "killer_is_me": killer_is_me,
            "victim_is_me": victim_is_me,
            "assister_is_me": assister_is_me,
            "killer_is_ally": killer_is_ally,
            "victim_is_ally": victim_is_ally,
            "turret_killed": turret_killed,
        }
        if ev_name == "Multikill":
            rec["kill_streak"] = max(0, _safe_int(ev.get("KillStreak"), 0))
        elif ev_name == "Ace":
            rec["acing_team"] = str(ev.get("AcingTeam") or "").strip()
            acer_hero, acer_is_me, acer_is_ally = _resolve_event_actor(ev.get("Acer"), name_lookup)
            rec["acer_hero"] = acer_hero
            rec["acer_is_me"] = acer_is_me
            rec["acer_is_ally"] = acer_is_ally
        elif ev_name == "InhibKilled":
            rec["inhib_killed"] = str(ev.get("InhibKilled") or "").strip()
        elif ev_name == "GameEnd":
            rec["result"] = str(ev.get("Result") or "").strip()

        sanitized_events.append(rec)

    return ally_lost, enemy_lost, game_end_result, sanitized_events


def _extract_turrets_and_result(payload: dict[str, Any], my_team: str) -> tuple[int, int, int]:
    """Count (ally_turrets_lost, enemy_turrets_lost, game_end_result) from LiveClientData events.Events."""
    ally_lost, enemy_lost, game_end_result, _ = _extract_events_and_summary(payload, my_team, {})
    return ally_lost, enemy_lost, game_end_result


def _extract_active_runes_sanitized(full_runes: Any) -> dict[str, Any]:
    """Extract strictly numeric rune IDs from activePlayer.fullRunes (zero text/PII)."""
    if not isinstance(full_runes, dict) or not full_runes:
        return {}
    keystone_obj = full_runes.get("keystone")
    pt_obj = full_runes.get("primaryRuneTree")
    st_obj = full_runes.get("secondaryRuneTree")

    ks_id = _safe_int(keystone_obj.get("id"), 0) if isinstance(keystone_obj, dict) else 0
    pt_id = _safe_int(pt_obj.get("id"), 0) if isinstance(pt_obj, dict) else 0
    st_id = _safe_int(st_obj.get("id"), 0) if isinstance(st_obj, dict) else 0

    general_ids: list[int] = []
    for entry in full_runes.get("generalRunes") or []:
        if isinstance(entry, dict):
            rid = _safe_int(entry.get("id"), 0)
            if rid > 0:
                general_ids.append(rid)

    stat_ids: list[int] = []
    for entry in full_runes.get("statRunes") or []:
        if isinstance(entry, dict):
            rid = _safe_int(entry.get("id"), 0)
            if rid > 0:
                stat_ids.append(rid)

    if not (ks_id or pt_id or st_id or general_ids or stat_ids):
        return {}
    return {
        "keystone_id": ks_id,
        "primary_tree_id": pt_id,
        "secondary_tree_id": st_id,
        "general_rune_ids": general_ids,
        "stat_rune_ids": stat_ids,
    }


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
    player_keys_per_player: list[set[str]] = []
    my_player: PlayerLiveInfo | None = None

    for p in all_players_raw:
        if not isinstance(p, dict):
            continue
        scores = p.get("scores") or {}
        if not isinstance(scores, dict):
            scores = {}
        item_ids_list: list[str] = []
        item_details: list[dict[str, Any]] = []
        for it in p.get("items") or []:
            if not isinstance(it, dict):
                continue
            raw_iid = it.get("itemID")
            if raw_iid in (None, 0):
                continue
            item_ids_list.append(str(raw_iid))
            iid_int = _safe_int(raw_iid, 0)
            if iid_int > 0:
                count_val = _safe_int(it.get("count"), 1)
                item_details.append(
                    ItemSlotInfo(
                        item_id=iid_int,
                        count=max(0, count_val) if it.get("count") is not None else 1,
                        slot=_safe_int(it.get("slot"), -1),
                        price=max(0, _safe_int(it.get("price"), 0)),
                        consumable=bool(it.get("consumable", False)),
                        can_use=bool(it.get("canUse", False)),
                    ).to_dict()
                )

        p_runes = p.get("runes") if isinstance(p.get("runes"), dict) else {}
        p_ks = p_runes.get("keystone") if isinstance(p_runes.get("keystone"), dict) else {}
        p_pt = p_runes.get("primaryRuneTree") if isinstance(p_runes.get("primaryRuneTree"), dict) else {}
        p_st = p_runes.get("secondaryRuneTree") if isinstance(p_runes.get("secondaryRuneTree"), dict) else {}

        riot_id = str(p.get("riotId") or p.get("summonerName") or "")
        respawn_timer = max(0.0, round(_safe_float(p.get("respawnTimer"), 0.0), 1))

        info = PlayerLiveInfo(
            riot_id=riot_id,
            champion_name=str(p.get("championName") or ""),
            raw_champion_name=str(p.get("rawChampionName") or ""),
            team=str(p.get("team") or "ORDER"),
            is_dead=bool(p.get("isDead", False)),
            level=max(1, _safe_int(p.get("level"), 1)),
            kills=max(0, _safe_int(scores.get("kills"), 0)),
            deaths=max(0, _safe_int(scores.get("deaths"), 0)),
            assists=max(0, _safe_int(scores.get("assists"), 0)),
            item_ids=tuple(item_ids_list),
            summoner_spells=_extract_player_spells(p),
            respawn_timer=respawn_timer,
            creep_score=max(0, _safe_int(scores.get("creepScore"), 0)),
            keystone_id=max(0, _safe_int(p_ks.get("id"), 0)),
            primary_tree_id=max(0, _safe_int(p_pt.get("id"), 0)),
            secondary_tree_id=max(0, _safe_int(p_st.get("id"), 0)),
            item_details=item_details,
        )
        players.append(info)

        p_keys = {
            riot_id.strip().lower(),
            str(p.get("summonerName", "")).strip().lower(),
            str(p.get("riotIdGameName", "")).strip().lower(),
        }
        # Also include gameName portion before '#' in case Riot events omit the tag
        for extra_id in list(p_keys):
            if "#" in extra_id:
                p_keys.add(extra_id.split("#", 1)[0].strip())
        p_keys.discard("")
        player_keys_per_player.append(p_keys)
        if active_ids & p_keys:
            my_player = info

    if not players:
        return None

    if my_player is None and players:
        my_player = players[0]

    my_team = my_player.team if my_player else "ORDER"
    allies = tuple(p for p in players if p.team == my_team)
    enemies = tuple(p for p in players if p.team != my_team)

    # Build sanitized name -> (hero_key, is_me, is_ally) map for LiveClient events
    name_lookup: dict[str, tuple[str, bool, bool]] = {}
    for p_info, p_keys in zip(players, player_keys_per_player):
        hero_key = _hero_key_from_player(p_info)
        is_me = p_info is my_player
        is_ally = p_info.team == my_team
        for k in p_keys:
            name_lookup[k] = (hero_key, is_me, is_ally)

    raw_stats = active.get("championStats") or {}
    my_stats = (
        {
            str(k): float(v)
            for k, v in raw_stats.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        if isinstance(raw_stats, dict)
        else {}
    )

    # Parse active player's ability levels & inferred maxed basic skill (Q/W/E)
    raw_abilities = active.get("abilities") or {}
    ability_levels: dict[str, int] = {}
    if isinstance(raw_abilities, dict):
        for slot in ("Q", "W", "E", "R"):
            ab = raw_abilities.get(slot)
            if isinstance(ab, dict):
                lvl = _safe_int(ab.get("abilityLevel"), 0)
                if lvl > 0:
                    ability_levels[slot] = lvl
    basic_levels = [(slot, ability_levels.get(slot, 0)) for slot in ("Q", "W", "E")]
    basic_levels.sort(key=lambda pair: pair[1], reverse=True)
    my_maxed_ability = (
        basic_levels[0][0]
        if (basic_levels and basic_levels[0][1] >= 2 and basic_levels[0][1] > basic_levels[1][1])
        else ""
    )

    # Parse keystone rune name and sanitized active rune IDs
    full_runes = active.get("fullRunes") or {}
    keystone_obj = full_runes.get("keystone") if isinstance(full_runes, dict) else None
    my_keystone = (
        str(keystone_obj.get("displayName") or "").strip()
        if isinstance(keystone_obj, dict)
        else ""
    )
    active_runes = _extract_active_runes_sanitized(full_runes)

    my_team_kills = sum(p.kills for p in allies)
    enemy_team_kills = sum(p.kills for p in enemies)
    ally_turrets_lost, enemy_turrets_lost, game_end_result, raw_events = _extract_events_and_summary(
        payload, my_team, name_lookup
    )

    game_mode = str(game_data.get("gameMode") or "").strip() if isinstance(game_data, dict) else ""
    map_number = max(0, _safe_int(game_data.get("mapNumber"), 0)) if isinstance(game_data, dict) else 0

    return LiveMatchSnapshot(
        game_time=_safe_float(game_data.get("gameTime"), 0.0) if isinstance(game_data, dict) else 0.0,
        my_player=my_player,
        allies=allies,
        enemies=enemies,
        current_gold=_safe_float(active.get("currentGold"), 0.0),
        my_stats=my_stats,
        my_ability_levels=ability_levels,
        my_maxed_ability=my_maxed_ability,
        my_keystone=my_keystone,
        my_team_kills=my_team_kills,
        enemy_team_kills=enemy_team_kills,
        ally_turrets_lost=ally_turrets_lost,
        enemy_turrets_lost=enemy_turrets_lost,
        game_end_result=game_end_result,
        game_mode=game_mode,
        map_number=map_number,
        active_runes=active_runes,
        raw_events=raw_events,
    )


class LiveClientReader:
    """Polls 127.0.0.1:2999/liveclientdata/allgamedata via read-only GET."""

    def __init__(self, url: str = LIVE_CLIENT_URL, timeout: float = 0.6) -> None:
        self.url = url
        self.last_error: str = ""
        self._consecutive_failures: int = 0
        self._client = httpx.Client(verify=False, timeout=timeout, trust_env=False)

    @property
    def consecutive_failures(self) -> int:
        """Number of consecutive failed poll attempts since the last valid snapshot."""
        return self._consecutive_failures

    def reset_failures(self) -> None:
        self._consecutive_failures = 0

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
            self._consecutive_failures += 1
            return None
        try:
            resp = self._client.get(self.url)
            if resp.status_code != 200:
                self._consecutive_failures += 1
                return None
            snap = parse_allgamedata(resp.json())
            if snap is None:
                self._consecutive_failures += 1
                return None
            self.last_error = ""
            self._consecutive_failures = 0
            return snap
        except Exception as exc:
            self.last_error = str(exc)
            self._consecutive_failures += 1
            return None
