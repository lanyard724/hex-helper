"""Low-weight Special Champion Signature Build Database (tie-breaker only; 26.23 big data remains primary)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from hex_helper.config import get_app_data_dir

logger = logging.getLogger(__name__)

BUNDLED_HERO_SIG_PATH = Path(__file__).resolve().parents[1] / "assets" / "hero_signature_builds.json"


@dataclass(frozen=True)
class HeroSignatureBuild:
    hero_id: str
    name: str
    archetype: str
    starter_items: tuple[str, ...]
    signature_items: tuple[str, ...]
    avoid_passive: tuple[str, ...]
    note: str

    def to_coach_dict(self, store: Any | None = None) -> dict[str, Any]:
        fmt = lambda ids: [
            f"{store.get_item_info(i).name}({i})" if store is not None else i for i in ids
        ]
        return {
            "archetype": self.archetype,
            "starter_items": fmt(self.starter_items),
            "signature_items": fmt(self.signature_items),
            "avoid_passive": fmt(self.avoid_passive),
            "note": self.note,
        }


class HeroSignatureDatabase:
    """Indexed repository of special ARAM Mayhem champion signature items (low weight, big-data first)."""

    def __init__(
        self,
        bundled_path: Path = BUNDLED_HERO_SIG_PATH,
        user_override_path: Path | None = None,
    ) -> None:
        self._by_id: dict[str, HeroSignatureBuild] = {}
        self._by_name: dict[str, HeroSignatureBuild] = {}
        self._load_file(bundled_path)
        override = user_override_path or (get_app_data_dir() / "hero_signature_builds.json")
        if override.exists():
            self._load_file(override)

    def _load_file(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse hero signature DB %s: %s", path, exc)
            return
        for item in raw.get("heroes") or []:
            if not isinstance(item, dict):
                continue
            entry = HeroSignatureBuild(
                hero_id=str(item.get("hero_id", "")),
                name=str(item.get("name", "")),
                archetype=str(item.get("archetype", "")),
                starter_items=tuple(str(x) for x in (item.get("starter_items") or []) if str(x).strip()),
                signature_items=tuple(str(x) for x in (item.get("signature_items") or []) if str(x).strip()),
                avoid_passive=tuple(str(x) for x in (item.get("avoid_passive") or []) if str(x).strip()),
                note=str(item.get("note", "")),
            )
            if entry.hero_id:
                self._by_id[entry.hero_id] = entry
            if entry.name:
                self._by_name[entry.name] = entry

    @property
    def entries(self) -> list[HeroSignatureBuild]:
        return list(self._by_id.values())

    def get(self, hero_id: str | int, hero_name: str = "") -> HeroSignatureBuild | None:
        hit = self._by_id.get(str(hero_id))
        if hit is not None:
            return hit
        if hero_name:
            return self._by_name.get(hero_name)
        return None


@lru_cache(maxsize=1)
def get_hero_signature_db() -> HeroSignatureDatabase:
    return HeroSignatureDatabase()
