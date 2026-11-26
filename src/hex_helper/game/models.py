"""Shared read-only game telemetry data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LcuCredentials:
    pid: int
    port: int
    password: str
    protocol: str = "https"
    source: str = "lockfile"


@dataclass(frozen=True)
class ChampSelectState:
    queue_id: int = 0
    game_mode: str = ""
    is_mayhem: bool = False
    my_hero_id: str | None = None
    team_hero_ids: tuple[str, ...] = field(default_factory=tuple)
    bench_hero_ids: tuple[str, ...] = field(default_factory=tuple)
    rerolls_remaining: int = -1
    spell1_id: int = 0
    spell2_id: int = 0
    timer_phase: str = ""
    timer_remaining_ms: int = 0
    game_id: int = 0
    trade_states: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ItemSlotInfo:
    item_id: int
    count: int = 1
    slot: int = -1
    price: int = 0
    consumable: bool = False
    can_use: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": int(self.item_id),
            "count": int(self.count),
            "slot": int(self.slot),
            "price": int(self.price),
            "consumable": bool(self.consumable),
            "can_use": bool(self.can_use),
        }


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
    creep_score: int = 0
    keystone_id: int = 0
    primary_tree_id: int = 0
    secondary_tree_id: int = 0
    item_details: list[dict[str, Any]] = field(default_factory=list)


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
    game_mode: str = ""
    map_number: int = 0
    active_runes: dict[str, Any] = field(default_factory=dict)
    raw_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class EogMatchSnapshot:
    """Typed read-only End-Of-Game summary (strictly zero PII)."""

    won: int = -1
    item_ids: tuple[str, ...] = field(default_factory=tuple)
    kda: str = ""
    duration_s: int = 0
    total_damage_dealt_to_champions: int = 0
    total_damage_taken: int = 0
    total_heal: int = 0
    total_damage_shielded_on_teammates: int = 0
    time_ccing_others: int = 0
    gold_earned: int = 0
    total_time_spent_dead: int = 0
    game_ended_in_early_surrender: bool = False
    queue_id: int = 0
    game_id: int = 0
    all_players_final: list[dict[str, Any]] = field(default_factory=list)
    eog_augments_probed: list[int] = field(default_factory=list)
    eog_source: str = "eog_stats_block"

    def to_dict(self) -> dict[str, Any]:
        return {
            "won": int(self.won),
            "item_ids": tuple(self.item_ids),
            "kda": str(self.kda),
            "duration_s": int(self.duration_s),
            "total_damage_dealt_to_champions": int(self.total_damage_dealt_to_champions),
            "total_damage_taken": int(self.total_damage_taken),
            "total_heal": int(self.total_heal),
            "total_damage_shielded_on_teammates": int(self.total_damage_shielded_on_teammates),
            "time_ccing_others": int(self.time_ccing_others),
            "gold_earned": int(self.gold_earned),
            "total_time_spent_dead": int(self.total_time_spent_dead),
            "game_ended_in_early_surrender": bool(self.game_ended_in_early_surrender),
            "queue_id": int(self.queue_id),
            "game_id": int(self.game_id),
            "all_players_final": list(self.all_players_final),
            "eog_augments_probed": list(self.eog_augments_probed),
            "eog_source": str(self.eog_source),
        }
