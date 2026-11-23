"""Shared read-only game telemetry data structures."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LcuCredentials:
    pid: int
    port: int
    password: str
    protocol: str = "https"
    source: str = "lockfile"


@dataclass(frozen=True)
class ChampSelectState:
    queue_id: int
    game_mode: str
    is_mayhem: bool
    my_hero_id: str | None
    team_hero_ids: tuple[str, ...]
    bench_hero_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlayerLiveInfo:
    riot_id: str
    champion_name: str
    raw_champion_name: str
    team: str  # "ORDER" | "CHAOS"
    is_dead: bool
    level: int
    kills: int
    deaths: int
    assists: int
    item_ids: tuple[str, ...] = field(default_factory=tuple)
    summoner_spells: tuple[str, ...] = field(default_factory=tuple)
    respawn_timer: float = 0.0


@dataclass(frozen=True)
class LiveMatchSnapshot:
    game_time: float
    my_player: PlayerLiveInfo | None
    allies: tuple[PlayerLiveInfo, ...]
    enemies: tuple[PlayerLiveInfo, ...]
    current_gold: float = 0.0
    my_stats: dict[str, float] = field(default_factory=dict)
    my_ability_levels: dict[str, int] = field(default_factory=dict)
    my_maxed_ability: str = ""
    my_keystone: str = ""
    my_team_kills: int = 0
    enemy_team_kills: int = 0
    ally_turrets_lost: int = 0
    enemy_turrets_lost: int = 0
    game_end_result: int = -1  # -1 = ongoing/unknown, 1 = win, 0 = loss
