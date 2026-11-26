"""Strict read-only HTTP GET client for League Client Update (LCU) API."""

from __future__ import annotations

import base64
import logging
import math
import time
from typing import Any
from urllib.parse import quote

import httpx

from hex_helper.game.lockfile import find_lcu_credentials
from hex_helper.game.models import ChampSelectState, EogMatchSnapshot, LcuCredentials, LiveMatchSnapshot, PlayerLiveInfo

logger = logging.getLogger(__name__)

# Known ARAM: Mayhem queue IDs (2400 is CN/Global Queue 2400)
MAYHEM_QUEUE_IDS = {2400, 2401, 2402}

_AUGMENT_STAT_KEYS: tuple[str, ...] = (
    "playerAugment0",
    "playerAugment1",
    "playerAugment2",
    "playerAugment3",
    "playerAugment4",
    "playerAugment5",
    "playerAugment6",
    "PLAYER_AUGMENT_0",
    "PLAYER_AUGMENT_1",
    "PLAYER_AUGMENT_2",
    "PLAYER_AUGMENT_3",
    "PLAYER_AUGMENT_4",
    "PLAYER_AUGMENT_5",
    "PLAYER_AUGMENT_6",
    "augment1",
    "augment2",
    "augment3",
    "augment4",
    "augment5",
    "augment6",
    "AUGMENT1",
    "AUGMENT2",
    "AUGMENT3",
    "AUGMENT4",
    "AUGMENT5",
    "AUGMENT6",
)
_AUGMENT_LIST_KEYS: tuple[str, ...] = (
    "augments",
    "cherryAugments",
    "CHERRY_AUGMENTS",
    "playerAugments",
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


def _stat_int(stats: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        if key in stats and stats[key] is not None:
            return _safe_int(stats[key], default)
    return default


def _extract_items_from_stats(stats: dict[str, Any]) -> tuple[str, ...]:
    items: list[str] = []
    for slot in range(6):
        raw_val = stats.get(f"ITEM{slot}")
        if raw_val is None:
            raw_val = stats.get(f"item{slot}")
        iid = _safe_int(raw_val, 0)
        if iid > 0:
            items.append(str(iid))
    return tuple(items)


def _probe_augments_from_dicts(*objs: dict[str, Any] | None) -> list[int]:
    """Probe any Mayhem / Cherry / Arena augment IDs present in LCU stats dicts."""
    found: list[int] = []
    seen: set[int] = set()

    def _add(raw_v: Any) -> None:
        if isinstance(raw_v, dict):
            aid = _safe_int(raw_v.get("id") or raw_v.get("augmentId"), 0)
        else:
            aid = _safe_int(raw_v, 0)
        if aid > 0 and aid not in seen:
            seen.add(aid)
            found.append(aid)

    for obj in objs:
        if not isinstance(obj, dict):
            continue
        for key in _AUGMENT_STAT_KEYS:
            if key in obj:
                _add(obj.get(key))
        for list_key in _AUGMENT_LIST_KEYS:
            raw_list = obj.get(list_key)
            if isinstance(raw_list, (list, tuple)):
                for entry in raw_list:
                    _add(entry)
    return found


def _empty_eog_snapshot(eog_source: str = "none") -> dict[str, Any]:
    return EogMatchSnapshot(eog_source=eog_source).to_dict()


def is_mayhem_session(session: dict[str, Any], force_all_modes: bool = False) -> tuple[int, str, bool]:
    """Extract (queue_id, game_mode, is_mayhem) from /lol-gameflow/v1/session payload."""
    game_data = session.get("gameData") or {}
    queue = game_data.get("queue") or {}
    queue_id = _safe_int(queue.get("id"), 0)
    queue_type = str(queue.get("type", "") or "").upper()
    map_data = session.get("map") or {}
    game_mode = str(map_data.get("gameMode") or queue.get("gameMode") or "").upper()
    desc = str(queue.get("description") or "").lower()

    is_mayhem = (
        queue_id in MAYHEM_QUEUE_IDS
        or "KIWI" in game_mode
        or "MAYHEM" in game_mode
        or "KIWI" in queue_type
        or "MAYHEM" in queue_type
        or "海克斯" in desc
        or "mayhem" in desc
    )
    if force_all_modes:
        is_mayhem = True
    return queue_id, game_mode or queue_type or "UNKNOWN", is_mayhem


def parse_champ_select_session(
    cs_payload: dict[str, Any],
    queue_id: int = 2400,
    game_mode: str = "KIWI",
    is_mayhem: bool = True,
    game_id: int = 0,
) -> ChampSelectState:
    """Parse /lol-champ-select/v1/session JSON into ChampSelectState (zero PII)."""
    local_cell = cs_payload.get("localPlayerCellId")
    my_hero_id: str | None = None
    team_ids: list[str] = []
    spell1_id = 0
    spell2_id = 0

    for member in cs_payload.get("myTeam") or []:
        if not isinstance(member, dict):
            continue
        is_local = local_cell is not None and member.get("cellId") == local_cell
        if is_local:
            spell1_id = max(0, _safe_int(member.get("spell1Id"), 0))
            spell2_id = max(0, _safe_int(member.get("spell2Id"), 0))
        cid = _safe_int(member.get("championId"), 0)
        if cid <= 0:
            cid = _safe_int(member.get("championPickIntent"), 0)
        if cid > 0:
            team_ids.append(str(cid))
            if is_local:
                my_hero_id = str(cid)

    bench_ids: list[str] = []
    for b in cs_payload.get("benchChampions") or []:
        if not isinstance(b, dict):
            continue
        cid = _safe_int(b.get("championId"), 0)
        if cid > 0:
            bench_ids.append(str(cid))

    rerolls_remaining = -1
    if "rerollsRemaining" in cs_payload and cs_payload.get("rerollsRemaining") is not None:
        rerolls_remaining = _safe_int(cs_payload.get("rerollsRemaining"), -1)

    timer = cs_payload.get("timer") if isinstance(cs_payload.get("timer"), dict) else {}
    timer_phase = str(timer.get("phase") or "").strip()
    raw_rem = timer.get("adjustedTimeLeftInPhase")
    if raw_rem is None:
        raw_rem = timer.get("timeLeftInPhase")
    timer_remaining_ms = max(0, _safe_int(raw_rem, 0))

    resolved_game_id = max(0, _safe_int(game_id, 0))
    if resolved_game_id <= 0:
        resolved_game_id = max(0, _safe_int(cs_payload.get("gameId"), 0))

    trade_states: list[str] = []
    for trade in cs_payload.get("trades") or []:
        if isinstance(trade, dict):
            st = str(trade.get("state") or "").strip()
            if st:
                trade_states.append(st)
        elif isinstance(trade, str) and trade.strip():
            trade_states.append(trade.strip())

    return ChampSelectState(
        queue_id=queue_id,
        game_mode=game_mode,
        is_mayhem=is_mayhem,
        my_hero_id=my_hero_id,
        team_hero_ids=tuple(team_ids),
        bench_hero_ids=tuple(bench_ids),
        rerolls_remaining=rerolls_remaining,
        spell1_id=spell1_id,
        spell2_id=spell2_id,
        timer_phase=timer_phase,
        timer_remaining_ms=timer_remaining_ms,
        game_id=resolved_game_id,
        trade_states=trade_states,
    )


class LcuClient:
    """Strict read-only LCU client (GET requests only; zero mutations)."""

    def __init__(self, preferred_lockfile: str = "", force_all_modes: bool = False) -> None:
        self.preferred_lockfile = preferred_lockfile
        self.force_all_modes = force_all_modes
        self.creds: LcuCredentials | None = None
        self.last_error: str = ""
        self._client: httpx.Client | None = None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _ensure_connected(self) -> bool:
        if self.creds is not None and self._client is not None:
            return True
        creds = find_lcu_credentials(self.preferred_lockfile)
        if creds is None:
            self.last_error = "未检测到 LeagueClient lockfile"
            return False
        token = base64.b64encode(f"riot:{creds.password}".encode("utf-8")).decode("ascii")
        self.close()
        self.creds = creds
        self._client = httpx.Client(
            base_url=f"{creds.protocol}://127.0.0.1:{creds.port}",
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            verify=False,
            timeout=1.2,
            trust_env=False,
        )
        self.last_error = ""
        return True

    def _get_json(self, path: str) -> Any | None:
        if not self._ensure_connected() or self._client is None:
            return None
        try:
            resp = self._client.get(path)
            if resp.status_code in (400, 403, 404):
                return None
            resp.raise_for_status()
            self.last_error = ""
            return resp.json()
        except httpx.HTTPStatusError as exc:
            self.last_error = str(exc)
            if exc.response.status_code == 401:
                self.creds = None
                self.close()
            return None
        except Exception as exc:
            self.last_error = str(exc)
            # Connection refused / port changed -> reset creds to re-discover
            self.creds = None
            self.close()
            return None

    def get_gameflow_session(self) -> dict[str, Any] | None:
        """Read-only GET of /lol-gameflow/v1/session."""
        raw = self._get_json("/lol-gameflow/v1/session")
        return raw if isinstance(raw, dict) else None

    def get_gameflow_game_id(self) -> tuple[int, int]:
        """Read-only lookup of (game_id, queue_id) from /lol-gameflow/v1/session gameData."""
        session = self.get_gameflow_session()
        if not isinstance(session, dict):
            return 0, 0
        game_data = session.get("gameData")
        if not isinstance(game_data, dict):
            return 0, 0
        game_id = max(0, _safe_int(game_data.get("gameId"), 0))
        queue = game_data.get("queue")
        queue_id = max(0, _safe_int(queue.get("id"), 0)) if isinstance(queue, dict) else 0
        return game_id, queue_id

    def get_current_rune_page(self) -> dict[str, Any]:
        """Read-only GET of /lol-perks/v1/currentpage returning sanitized rune IDs only (zero PII)."""
        raw = self._get_json("/lol-perks/v1/currentpage")
        if not isinstance(raw, dict):
            return {
                "primary_style_id": 0,
                "sub_style_id": 0,
                "selected_perk_ids": [],
            }
        selected_perks: list[int] = []
        for entry in raw.get("selectedPerkIds") or []:
            pid = _safe_int(entry, 0)
            if pid > 0:
                selected_perks.append(pid)
        return {
            "primary_style_id": max(0, _safe_int(raw.get("primaryStyleId"), 0)),
            "sub_style_id": max(0, _safe_int(raw.get("subStyleId"), 0)),
            "selected_perk_ids": selected_perks,
        }

    def get_current_summoner(self) -> dict[str, Any] | None:
        """Read-only GET of /lol-summoner/v1/current-summoner."""
        raw = self._get_json("/lol-summoner/v1/current-summoner")
        return raw if isinstance(raw, dict) else None

    def get_ranked_stats(self, puuid: str) -> dict[str, Any] | None:
        """Read-only GET of ranked tier summary for a player PUUID."""
        pid = str(puuid or "").strip()
        if not pid:
            return None
        safe_pid = quote(pid, safe="-_")
        raw = self._get_json(f"/lol-ranked/v1/ranked-stats/{safe_pid}")
        return raw if isinstance(raw, dict) else None

    def get_match_history(self, puuid: str, count: int = 15) -> list[dict[str, Any]]:
        """Read-only GET of recent match history (empty list on hidden CN profiles)."""
        pid = str(puuid or "").strip()
        if not pid:
            return []
        end_idx = max(1, min(20, int(count))) - 1
        safe_pid = quote(pid, safe="-_")
        raw = self._get_json(
            f"/lol-match-history/v1/products/lol/{safe_pid}/matches?begIndex=0&endIndex={end_idx}"
        )
        if not isinstance(raw, dict):
            return []
        games_field = raw.get("games")
        if isinstance(games_field, dict):
            games = games_field.get("games")
        elif isinstance(games_field, list):
            games = games_field
        else:
            games = None
        return [g for g in games if isinstance(g, dict)] if isinstance(games, list) else []

    def poll(self) -> tuple[str, ChampSelectState | None]:
        """Poll LCU gameflow phase and (if in ChampSelect) the current team & bench."""
        phase_raw = self._get_json("/lol-gameflow/v1/gameflow-phase")
        if not isinstance(phase_raw, str):
            return "Disconnected", None

        if phase_raw != "ChampSelect":
            return phase_raw, None

        session = self._get_json("/lol-gameflow/v1/session") or {}
        session_dict = session if isinstance(session, dict) else {}
        queue_id, game_mode, is_mayhem = is_mayhem_session(
            session_dict,
            force_all_modes=self.force_all_modes,
        )
        game_data = session_dict.get("gameData")
        game_id = max(0, _safe_int(game_data.get("gameId"), 0)) if isinstance(game_data, dict) else 0
        cs = self._get_json("/lol-champ-select/v1/session")
        if not isinstance(cs, dict):
            return phase_raw, None
        return phase_raw, parse_champ_select_session(cs, queue_id, game_mode, is_mayhem, game_id=game_id)

    def get_eog_snapshot(self) -> dict[str, Any]:
        """Read-only lookup of End-Of-Game outcome, authoritative 6-slot items, KDA, damage, and 10-player summary."""
        result = _empty_eog_snapshot(eog_source="none")
        eog = self._get_json("/lol-end-of-game/v1/eog-stats-block")
        if not isinstance(eog, dict):
            return result

        result["eog_source"] = "eog_stats_block"
        dur = max(0, _safe_int(eog.get("gameLength"), 0))
        if dur > 0:
            result["duration_s"] = dur
        result["game_id"] = max(0, _safe_int(eog.get("gameId"), 0))
        result["queue_id"] = max(0, _safe_int(eog.get("queueId"), 0))

        early_surrender = bool(
            eog.get("gameEndedInEarlySurrender")
            or eog.get("earlySurrenderAccomplice")
            or eog.get("earlySurrender")
        )

        local_p = eog.get("localPlayer")
        local_team_id = 0
        local_champ_id = 0
        local_puuid = ""
        local_summoner_id = 0
        if isinstance(local_p, dict):
            local_team_id = _safe_int(local_p.get("teamId"), 0)
            local_champ_id = _safe_int(local_p.get("championId"), 0)
            local_puuid = str(local_p.get("puuid") or "").strip()
            local_summoner_id = _safe_int(local_p.get("summonerId"), 0)
            stats = local_p.get("stats")
            if isinstance(stats, dict):
                if "WIN" in stats:
                    result["won"] = 1 if _safe_int(stats.get("WIN"), 0) > 0 else 0
                elif "LOSE" in stats:
                    result["won"] = 0 if _safe_int(stats.get("LOSE"), 0) > 0 else 1
                elif "win" in stats:
                    result["won"] = 1 if bool(stats.get("win")) else 0

                eog_items = _extract_items_from_stats(stats)
                if eog_items:
                    result["item_ids"] = eog_items

                k = stats.get("CHAMPIONS_KILLED", stats.get("kills"))
                d = stats.get("NUM_DEATHS", stats.get("deaths"))
                a = stats.get("ASSISTS", stats.get("assists"))
                if k is not None or d is not None or a is not None:
                    result["kda"] = f"{_safe_int(k, 0)}/{_safe_int(d, 0)}/{_safe_int(a, 0)}"

                result["total_damage_dealt_to_champions"] = max(
                    0,
                    _stat_int(stats, "TOTAL_DAMAGE_DEALT_TO_CHAMPIONS", "totalDamageDealtToChampions"),
                )
                result["total_damage_taken"] = max(
                    0,
                    _stat_int(stats, "TOTAL_DAMAGE_TAKEN", "totalDamageTaken"),
                )
                result["total_heal"] = max(
                    0,
                    _stat_int(stats, "TOTAL_HEAL", "totalHeal"),
                )
                result["total_damage_shielded_on_teammates"] = max(
                    0,
                    _stat_int(
                        stats,
                        "TOTAL_DAMAGE_SHIELDED_ON_TEAMMATES",
                        "totalDamageShieldedOnTeammates",
                    ),
                )
                result["time_ccing_others"] = max(
                    0,
                    _stat_int(stats, "TIME_CCING_OTHERS", "timeCCingOthers"),
                )
                result["gold_earned"] = max(
                    0,
                    _stat_int(stats, "GOLD_EARNED", "goldEarned"),
                )
                result["total_time_spent_dead"] = max(
                    0,
                    _stat_int(stats, "TOTAL_TIME_SPENT_DEAD", "totalTimeSpentDead"),
                )
                if stats.get("GAME_ENDED_IN_EARLY_SURRENDER") or stats.get("gameEndedInEarlySurrender"):
                    early_surrender = True
                result["eog_augments_probed"] = _probe_augments_from_dicts(stats, local_p)
            else:
                result["eog_augments_probed"] = _probe_augments_from_dicts(local_p)

        all_players_final: list[dict[str, Any]] = []
        for team in eog.get("teams") or []:
            if not isinstance(team, dict):
                continue
            team_id = _safe_int(team.get("teamId"), 0)
            if team.get("gameEndedInEarlySurrender"):
                early_surrender = True
            if result["won"] not in (0, 1) and local_team_id and team_id == local_team_id:
                if "isWinningTeam" in team:
                    result["won"] = 1 if bool(team.get("isWinningTeam")) else 0

            for p in team.get("players") or []:
                if not isinstance(p, dict):
                    continue
                p_stats = p.get("stats") if isinstance(p.get("stats"), dict) else {}
                p_team_id = _safe_int(p.get("teamId"), team_id)
                p_champ_id = _safe_int(p.get("championId"), 0)
                is_me = bool(p.get("isLocalPlayer", False))
                if not is_me and local_puuid and str(p.get("puuid") or "").strip() == local_puuid:
                    is_me = True
                if not is_me and local_summoner_id > 0 and _safe_int(p.get("summonerId"), 0) == local_summoner_id:
                    is_me = True
                if not is_me and local_champ_id > 0 and p_champ_id == local_champ_id and (
                    not local_team_id or p_team_id == local_team_id
                ):
                    is_me = True

                pk = _stat_int(p_stats, "CHAMPIONS_KILLED", "kills")
                pd = _stat_int(p_stats, "NUM_DEATHS", "deaths")
                pa = _stat_int(p_stats, "ASSISTS", "assists")
                p_entry: dict[str, Any] = {
                    "hero_id": str(p_champ_id) if p_champ_id > 0 else "",
                    "team_id": p_team_id,
                    "is_ally": bool(local_team_id and p_team_id == local_team_id),
                    "is_me": is_me,
                    "items": list(_extract_items_from_stats(p_stats)),
                    "kills": pk,
                    "deaths": pd,
                    "assists": pa,
                    "kda": f"{pk}/{pd}/{pa}",
                    "damage_to_champions": max(
                        0,
                        _stat_int(p_stats, "TOTAL_DAMAGE_DEALT_TO_CHAMPIONS", "totalDamageDealtToChampions"),
                    ),
                    "damage_taken": max(
                        0,
                        _stat_int(p_stats, "TOTAL_DAMAGE_TAKEN", "totalDamageTaken"),
                    ),
                    "gold_earned": max(0, _stat_int(p_stats, "GOLD_EARNED", "goldEarned")),
                }
                p_augs = _probe_augments_from_dicts(p_stats, p)
                if p_augs:
                    p_entry["augments_probed"] = p_augs
                all_players_final.append(p_entry)

        result["game_ended_in_early_surrender"] = early_surrender
        result["all_players_final"] = all_players_final
        return result

    def get_match_history_eog_fallback(
        self,
        game_id: int = 0,
        hero_id: int = 0,
    ) -> dict[str, Any] | None:
        """Read-only EOG fallback via current-summoner match history when EndOfGame block was missed."""
        raw = self._get_json(
            "/lol-match-history/v1/products/lol/current-summoner/matches?begIndex=0&endIndex=3"
        )
        if not isinstance(raw, dict):
            return None

        games_field = raw.get("games")
        if isinstance(games_field, dict):
            games_raw = games_field.get("games")
        elif isinstance(games_field, list):
            games_raw = games_field
        else:
            games_raw = None
        if not isinstance(games_raw, list):
            return None

        games = [g for g in games_raw if isinstance(g, dict)]
        if not games:
            return None

        # Sort newest first when timestamps are present and distinct
        ts_vals = [_safe_int(g.get("gameCreation"), 0) for g in games if _safe_int(g.get("gameCreation"), 0) > 0]
        if len(ts_vals) >= 2 and len(set(ts_vals)) > 1:
            games = sorted(games, key=lambda g: _safe_int(g.get("gameCreation"), 0), reverse=True)

        target_game_id = max(0, _safe_int(game_id, 0))
        target_hero_id = max(0, _safe_int(hero_id, 0))
        now_s = time.time()

        matched_game: dict[str, Any] | None = None
        matched_local_p: dict[str, Any] | None = None

        for g in games:
            g_id = max(0, _safe_int(g.get("gameId"), 0))
            dur = max(0, _safe_int(g.get("gameDuration"), 0))
            if dur > 86400:
                dur //= 1000

            participants = [p for p in (g.get("participants") or []) if isinstance(p, dict)]
            if not participants:
                continue

            if target_game_id > 0:
                if g_id != target_game_id:
                    continue
                # Matched by authoritative gameId; pick local participant
                local_p = participants[0] if len(participants) == 1 else None
                if local_p is None and target_hero_id > 0:
                    for p in participants:
                        if _safe_int(p.get("championId"), 0) == target_hero_id:
                            local_p = p
                            break
                if local_p is None:
                    local_p = participants[0]
                matched_game = g
                matched_local_p = local_p
                break

            # Fallback matching without gameId: require recency within 10 minutes
            created_raw = _safe_int(g.get("gameCreation"), 0)
            if created_raw > 0:
                created_s = created_raw / 1000.0 if created_raw > 10_000_000_000 else float(created_raw)
                ended_s = created_s + float(dur)
                if (now_s - ended_s) > 600.0 and (now_s - created_s) > 600.0:
                    continue

            if target_hero_id > 0:
                local_p = None
                for p in participants:
                    if _safe_int(p.get("championId"), 0) == target_hero_id:
                        local_p = p
                        break
                if local_p is None:
                    continue
                matched_game = g
                matched_local_p = local_p
                break

            matched_game = g
            matched_local_p = participants[0]
            break

        if matched_game is None or matched_local_p is None:
            return None

        result = _empty_eog_snapshot(eog_source="match_history_fallback")
        dur = max(0, _safe_int(matched_game.get("gameDuration"), 0))
        if dur > 86400:
            dur //= 1000
        result["duration_s"] = dur
        result["game_id"] = max(0, _safe_int(matched_game.get("gameId"), 0))
        result["queue_id"] = max(0, _safe_int(matched_game.get("queueId"), 0))

        local_stats = matched_local_p.get("stats") if isinstance(matched_local_p.get("stats"), dict) else {}
        local_team_id = _safe_int(matched_local_p.get("teamId"), 0)
        local_champ_id = _safe_int(matched_local_p.get("championId"), 0)
        local_part_id = _safe_int(matched_local_p.get("participantId"), -1)

        if "win" in local_stats:
            result["won"] = 1 if bool(local_stats.get("win")) else 0
        elif "WIN" in local_stats:
            result["won"] = 1 if _safe_int(local_stats.get("WIN"), 0) > 0 else 0

        result["item_ids"] = _extract_items_from_stats(local_stats)
        lk = _stat_int(local_stats, "kills", "CHAMPIONS_KILLED")
        ld = _stat_int(local_stats, "deaths", "NUM_DEATHS")
        la = _stat_int(local_stats, "assists", "ASSISTS")
        if any(
            key in local_stats
            for key in ("kills", "deaths", "assists", "CHAMPIONS_KILLED", "NUM_DEATHS", "ASSISTS")
        ):
            result["kda"] = f"{lk}/{ld}/{la}"

        result["total_damage_dealt_to_champions"] = max(
            0,
            _stat_int(local_stats, "totalDamageDealtToChampions", "TOTAL_DAMAGE_DEALT_TO_CHAMPIONS"),
        )
        result["total_damage_taken"] = max(
            0,
            _stat_int(local_stats, "totalDamageTaken", "TOTAL_DAMAGE_TAKEN"),
        )
        result["total_heal"] = max(
            0,
            _stat_int(local_stats, "totalHeal", "TOTAL_HEAL"),
        )
        result["total_damage_shielded_on_teammates"] = max(
            0,
            _stat_int(
                local_stats,
                "totalDamageShieldedOnTeammates",
                "TOTAL_DAMAGE_SHIELDED_ON_TEAMMATES",
            ),
        )
        result["time_ccing_others"] = max(
            0,
            _stat_int(local_stats, "timeCCingOthers", "TIME_CCING_OTHERS"),
        )
        result["gold_earned"] = max(
            0,
            _stat_int(local_stats, "goldEarned", "GOLD_EARNED"),
        )
        result["total_time_spent_dead"] = max(
            0,
            _stat_int(local_stats, "totalTimeSpentDead", "TOTAL_TIME_SPENT_DEAD"),
        )
        result["game_ended_in_early_surrender"] = bool(
            local_stats.get("gameEndedInEarlySurrender")
            or local_stats.get("GAME_ENDED_IN_EARLY_SURRENDER")
            or matched_game.get("gameEndedInEarlySurrender")
        )
        result["eog_augments_probed"] = _probe_augments_from_dicts(local_stats, matched_local_p)

        all_players_final: list[dict[str, Any]] = []
        for p in matched_game.get("participants") or []:
            if not isinstance(p, dict):
                continue
            p_stats = p.get("stats") if isinstance(p.get("stats"), dict) else {}
            p_team_id = _safe_int(p.get("teamId"), 0)
            p_champ_id = _safe_int(p.get("championId"), 0)
            p_part_id = _safe_int(p.get("participantId"), -2)
            is_me = (
                p is matched_local_p
                or (local_part_id >= 0 and p_part_id == local_part_id)
                or (local_champ_id > 0 and p_champ_id == local_champ_id and p_team_id == local_team_id)
            )
            pk = _stat_int(p_stats, "kills", "CHAMPIONS_KILLED")
            pd = _stat_int(p_stats, "deaths", "NUM_DEATHS")
            pa = _stat_int(p_stats, "assists", "ASSISTS")
            p_entry: dict[str, Any] = {
                "hero_id": str(p_champ_id) if p_champ_id > 0 else "",
                "team_id": p_team_id,
                "is_ally": bool(local_team_id and p_team_id == local_team_id),
                "is_me": is_me,
                "items": list(_extract_items_from_stats(p_stats)),
                "kills": pk,
                "deaths": pd,
                "assists": pa,
                "kda": f"{pk}/{pd}/{pa}",
                "damage_to_champions": max(
                    0,
                    _stat_int(p_stats, "totalDamageDealtToChampions", "TOTAL_DAMAGE_DEALT_TO_CHAMPIONS"),
                ),
                "damage_taken": max(
                    0,
                    _stat_int(p_stats, "totalDamageTaken", "TOTAL_DAMAGE_TAKEN"),
                ),
                "gold_earned": max(0, _stat_int(p_stats, "goldEarned", "GOLD_EARNED")),
            }
            p_augs = _probe_augments_from_dicts(p_stats, p)
            if p_augs:
                p_entry["augments_probed"] = p_augs
            all_players_final.append(p_entry)

        result["all_players_final"] = all_players_final
        return result

    def get_eog_won(self) -> int:
        """Read-only lookup of local player win/loss from End-Of-Game stats block (-1 if unavailable)."""
        return int(self.get_eog_snapshot().get("won", -1))

    def get_in_game_fallback_snapshot(
        self,
        hero_resolver: Any | None = None,
        *,
        game_time_s: float = 0.0,
        allow_all_modes: bool = False,
    ) -> LiveMatchSnapshot | None:
        """Build a degraded read-only LiveMatchSnapshot from /lol-gameflow/v1/session when 2999 is down."""
        session = self.get_gameflow_session()
        if not isinstance(session, dict):
            return None
        phase = str(session.get("phase") or "")
        if phase != "InProgress":
            return None
        gd = session.get("gameData")
        if not isinstance(gd, dict):
            return None
        queue = gd.get("queue") if isinstance(gd.get("queue"), dict) else {}
        queue_id = max(0, _safe_int(queue.get("id"), 0))
        game_mode = str(queue.get("gameMode") or gd.get("gameMode") or "").strip().upper()
        map_info = session.get("map") if isinstance(session.get("map"), dict) else {}
        map_number = max(0, _safe_int(map_info.get("id"), 0))
        is_supported = (
            allow_all_modes
            or queue_id in (2400, 450)
            or game_mode in ("KIWI", "ARAM", "CHERRY")
        )
        if not is_supported:
            return None

        my_sid = getattr(self, "_cached_my_summoner_id", 0)
        my_puuid = getattr(self, "_cached_my_puuid", "")
        if not my_sid and not my_puuid:
            me = self.get_current_summoner()
            if isinstance(me, dict):
                my_sid = _safe_int(me.get("summonerId"), 0)
                my_puuid = str(me.get("puuid") or "").strip()
                self._cached_my_summoner_id = my_sid
                self._cached_my_puuid = my_puuid

        team_one_raw = gd.get("teamOne") if isinstance(gd.get("teamOne"), list) else []
        team_two_raw = gd.get("teamTwo") if isinstance(gd.get("teamTwo"), list) else []
        if not team_one_raw and not team_two_raw:
            return None

        def _build_player(entry: dict[str, Any], team_label: str, idx: int) -> tuple[PlayerLiveInfo, bool] | None:
            cid = _safe_int(entry.get("championId"), 0)
            if cid <= 0:
                return None
            cid_str = str(cid)
            cname = cid_str
            raw_cname = cid_str
            if callable(hero_resolver):
                try:
                    resolved = hero_resolver(cid_str)
                    if isinstance(resolved, tuple) and len(resolved) >= 2:
                        cname = str(resolved[0] or cid_str)
                        raw_cname = str(resolved[1] or cname)
                except Exception:
                    pass
            p_sid = _safe_int(entry.get("summonerId"), 0)
            p_puuid = str(entry.get("puuid") or "").strip()
            is_me = bool((my_sid and p_sid == my_sid) or (my_puuid and p_puuid == my_puuid))
            info = PlayerLiveInfo(
                riot_id=f"lcu_{team_label.lower()}_{idx}",
                champion_name=cname,
                raw_champion_name=raw_cname,
                team=team_label,
                is_dead=False,
                level=3,
                kills=0,
                deaths=0,
                assists=0,
            )
            return info, is_me

        order_players: list[PlayerLiveInfo] = []
        chaos_players: list[PlayerLiveInfo] = []
        my_player: PlayerLiveInfo | None = None
        for idx, entry in enumerate(team_one_raw):
            if not isinstance(entry, dict):
                continue
            built = _build_player(entry, "ORDER", idx)
            if built is None:
                continue
            p_info, is_me = built
            order_players.append(p_info)
            if is_me:
                my_player = p_info
        for idx, entry in enumerate(team_two_raw):
            if not isinstance(entry, dict):
                continue
            built = _build_player(entry, "CHAOS", idx)
            if built is None:
                continue
            p_info, is_me = built
            chaos_players.append(p_info)
            if is_me:
                my_player = p_info

        if my_player is None:
            return None

        allies = tuple(order_players if my_player.team == "ORDER" else chaos_players)
        enemies = tuple(chaos_players if my_player.team == "ORDER" else order_players)
        return LiveMatchSnapshot(
            game_time=max(0.0, float(game_time_s)),
            my_player=my_player,
            allies=allies,
            enemies=enemies,
            current_gold=0.0,
            game_mode=game_mode,
            map_number=map_number,
        )
