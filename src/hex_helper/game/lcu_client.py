"""Strict read-only HTTP GET client for League Client Update (LCU) API."""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from hex_helper.game.lockfile import find_lcu_credentials
from hex_helper.game.models import ChampSelectState, LcuCredentials

logger = logging.getLogger(__name__)

# Known ARAM: Mayhem queue IDs (2400 is CN/Global Queue 2400)
MAYHEM_QUEUE_IDS = {2400, 2401, 2402}


def is_mayhem_session(session: dict[str, Any], force_all_modes: bool = False) -> tuple[int, str, bool]:
    """Extract (queue_id, game_mode, is_mayhem) from /lol-gameflow/v1/session payload."""
    game_data = session.get("gameData") or {}
    queue = game_data.get("queue") or {}
    queue_id = int(queue.get("id", 0) or 0)
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
) -> ChampSelectState:
    """Parse /lol-champ-select/v1/session JSON into ChampSelectState."""
    local_cell = cs_payload.get("localPlayerCellId")
    my_hero_id: str | None = None
    team_ids: list[str] = []

    for member in cs_payload.get("myTeam") or []:
        cid = int(member.get("championId", 0) or 0)
        if cid <= 0:
            cid = int(member.get("championPickIntent", 0) or 0)
        if cid > 0:
            team_ids.append(str(cid))
            if member.get("cellId") == local_cell:
                my_hero_id = str(cid)

    bench_ids: list[str] = []
    for b in cs_payload.get("benchChampions") or []:
        cid = int(b.get("championId", 0) or 0)
        if cid > 0:
            bench_ids.append(str(cid))

    return ChampSelectState(
        queue_id=queue_id,
        game_mode=game_mode,
        is_mayhem=is_mayhem,
        my_hero_id=my_hero_id,
        team_hero_ids=tuple(team_ids),
        bench_hero_ids=tuple(bench_ids),
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
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            self.last_error = ""
            return resp.json()
        except Exception as exc:
            self.last_error = str(exc)
            # Connection refused / port changed -> reset creds to re-discover
            self.creds = None
            self.close()
            return None

    def poll(self) -> tuple[str, ChampSelectState | None]:
        """Poll LCU gameflow phase and (if in ChampSelect) the current team & bench."""
        phase_raw = self._get_json("/lol-gameflow/v1/gameflow-phase")
        if not isinstance(phase_raw, str):
            return "Disconnected", None

        if phase_raw != "ChampSelect":
            return phase_raw, None

        session = self._get_json("/lol-gameflow/v1/session") or {}
        queue_id, game_mode, is_mayhem = is_mayhem_session(
            session if isinstance(session, dict) else {},
            force_all_modes=self.force_all_modes,
        )
        cs = self._get_json("/lol-champ-select/v1/session")
        if not isinstance(cs, dict):
            return phase_raw, None
        return phase_raw, parse_champ_select_session(cs, queue_id, game_mode, is_mayhem)

    def get_eog_won(self) -> int:
        """Read-only lookup of local player win/loss from End-Of-Game stats block (-1 if unavailable)."""
        eog = self._get_json("/lol-end-of-game/v1/eog-stats-block")
        if not isinstance(eog, dict):
            return -1
        local_p = eog.get("localPlayer")
        if isinstance(local_p, dict):
            stats = local_p.get("stats")
            if isinstance(stats, dict):
                if "WIN" in stats:
                    return 1 if int(stats.get("WIN") or 0) > 0 else 0
                if "LOSE" in stats:
                    return 0 if int(stats.get("LOSE") or 0) > 0 else 1
            local_team_id = local_p.get("teamId")
            for team in eog.get("teams") or []:
                if isinstance(team, dict) and team.get("teamId") == local_team_id:
                    if "isWinningTeam" in team:
                        return 1 if bool(team.get("isWinningTeam")) else 0
        return -1
