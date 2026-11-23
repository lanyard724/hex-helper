"""Destiny's Weapon (augment ID 3081) item recommendation ranking."""

from __future__ import annotations

from dataclasses import dataclass

from hex_helper.data.store import DataStore

DESTINY_AUGMENT_ID = "3081"


@dataclass(frozen=True)
class DestinyItemRec:
    item_id: str
    name: str
    win_rate: float
    delta: float
    games: int
    low_sample: bool


def rank_destiny_items(store: DataStore, hero_id: str | int, limit: int = 4) -> list[DestinyItemRec]:
    """Return top binding items for Destiny's Weapon (3081), sorted by win-rate delta over hero baseline."""
    data = store.get_destiny_data(hero_id)
    if data is None or not data.items:
        return []

    baseline = data.baseline_win_rate
    recs: list[DestinyItemRec] = []
    for it in data.items:
        wr = it.win_rate
        recs.append(
            DestinyItemRec(
                item_id=it.item_id,
                name=it.name or store.get_item_info(it.item_id).name,
                win_rate=wr,
                delta=wr - baseline,
                games=it.games,
                low_sample=it.low_sample,
            )
        )
    recs.sort(key=lambda r: (not r.low_sample, r.delta, r.games), reverse=True)
    return recs[:limit]
