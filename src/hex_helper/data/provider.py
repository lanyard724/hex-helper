"""Data models and pluggable StatsProvider protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class DatasetDescriptor:
    patch: str
    mode: str
    mode_label: str
    path: str
    snapshot_at: str
    bands: bool
    seq: int = 0
    valid_matches: int = 0

    @property
    def key(self) -> str:
        return f"{self.patch}/{self.mode}"


@dataclass(frozen=True)
class HeroStat:
    hero_id: str
    name: str
    title: str
    games: int
    wins: int
    win_rate: float
    pick_rate: float
    rank: int
    low_sample: bool = False


@dataclass(frozen=True)
class AugmentInfo:
    augment_id: str
    name: str
    rarity: str  # "silver" | "gold" | "prismatic"
    icon_url: str = ""


@dataclass(frozen=True)
class HeroAugmentStat:
    augment_id: str
    games: int
    wins: int
    win_rate: float
    low_sample: bool = False


@dataclass(frozen=True)
class CoreBuild:
    items: tuple[str, ...]
    ordered: bool
    games: int
    wins: int
    win_rate: float
    pick_rate: float


@dataclass(frozen=True)
class DestinyItemEntry:
    item_id: str
    name: str
    games: int
    wins: int
    low_sample: bool = False
    control_games: int = 0
    control_wins: int = 0
    style: str = ""

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games > 0 else 0.0

    @property
    def control_win_rate(self) -> float | None:
        return self.control_wins / self.control_games if self.control_games > 0 else None


@dataclass(frozen=True)
class DestinyData:
    baseline_games: int
    baseline_wins: int
    items: tuple[DestinyItemEntry, ...]

    @property
    def baseline_win_rate(self) -> float:
        return self.baseline_wins / self.baseline_games if self.baseline_games > 0 else 0.5


@dataclass(frozen=True)
class ItemRecStat:
    item_id: str
    games: int
    wins: int
    win_rate: float
    low_sample: bool = False


@dataclass(frozen=True)
class SpellComboStat:
    spell_ids: tuple[str, str]
    games: int
    wins: int
    win_rate: float
    low_sample: bool = False


@dataclass(frozen=True)
class ItemStaticInfo:
    item_id: str
    name: str
    gold: int = 0
    tags: tuple[str, ...] = field(default_factory=tuple)
    stats: dict[str, float] = field(default_factory=dict)
    description: str = ""
    category: str = "other"
    icon_url: str = ""
    has_grievous: bool = False
    has_shield_break: bool = False
    components: tuple[str, ...] = field(default_factory=tuple)
    aram_available: bool = True


class StatsProvider(Protocol):
    """Pluggable provider contract so backup sources (e.g. OP.GG / AramKit) can be added."""

    def fetch_index(self) -> list[DatasetDescriptor]: ...

    def sync_dataset(
        self,
        patch: str = "latest",
        mode: str = "normal",
        band: str = "",
        progress_cb: Callable[[str], None] | None = None,
    ) -> tuple[DatasetDescriptor, Path]: ...
