"""Patch 26.23 (16.23) Special Augment Build Archetype Database loader & query engine."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from hex_helper.config import get_app_data_dir

logger = logging.getLogger(__name__)

BUNDLED_DB_PATH = Path(__file__).resolve().parents[1] / "assets" / "special_augment_builds.json"


@dataclass(frozen=True)
class SpecialAugmentBuild:
    augment_id: str
    name: str
    aliases: tuple[str, ...]
    rarity: str
    archetype: str
    mechanic: str
    transformative: bool
    allow_off_profile: bool
    shifts_profile: str  # "" | "AP" | "AD" | "TANK"
    boost: float
    core_items: tuple[str, ...]
    force_items: tuple[str, ...]
    required_tags: tuple[str, ...]
    stat_keys: tuple[str, ...]
    penalize_tags: tuple[str, ...]
    avoid_note: str
    playstyle_tip: str

    def to_coach_dict(self, store: Any | None = None) -> dict[str, Any]:
        core_names = [
            f"{store.get_item_info(i).name}({i})" if store is not None else i
            for i in self.core_items[:6]
        ]
        force_names = [
            f"{store.get_item_info(i).name}({i})" if store is not None else i
            for i in self.force_items
        ]
        return {
            "id": self.augment_id,
            "name": self.name,
            "archetype": self.archetype,
            "mechanic": self.mechanic,
            "force_items": force_names,
            "core_items": core_names,
            "avoid_note": self.avoid_note,
            "playstyle_tip": self.playstyle_tip,
        }


def _parse_entry(raw: dict[str, Any]) -> SpecialAugmentBuild:
    return SpecialAugmentBuild(
        augment_id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        aliases=tuple(str(a) for a in (raw.get("aliases") or []) if str(a).strip()),
        rarity=str(raw.get("rarity", "gold")),
        archetype=str(raw.get("archetype", "")),
        mechanic=str(raw.get("mechanic", "")),
        transformative=bool(raw.get("transformative", True)),
        allow_off_profile=bool(raw.get("allow_off_profile", False)),
        shifts_profile=str(raw.get("shifts_profile", "")),
        boost=float(raw.get("boost", 3.5)),
        core_items=tuple(str(i) for i in (raw.get("core_items") or []) if str(i).strip()),
        force_items=tuple(str(i) for i in (raw.get("force_items") or []) if str(i).strip()),
        required_tags=tuple(str(t) for t in (raw.get("required_tags") or []) if str(t).strip()),
        stat_keys=tuple(str(k) for k in (raw.get("stat_keys") or []) if str(k).strip()),
        penalize_tags=tuple(str(t) for t in (raw.get("penalize_tags") or []) if str(t).strip()),
        avoid_note=str(raw.get("avoid_note", "")),
        playstyle_tip=str(raw.get("playstyle_tip", "")),
    )


class SpecialAugmentDatabase:
    """In-memory indexed database of special build-defining augments (bundled + user overrides)."""

    def __init__(
        self,
        bundled_path: Path = BUNDLED_DB_PATH,
        user_override_path: Path | None = None,
    ) -> None:
        self.patch: str = ""
        self.updated_at: str = ""
        self._by_id: dict[str, SpecialAugmentBuild] = {}
        self._by_name: dict[str, SpecialAugmentBuild] = {}
        self._entries: list[SpecialAugmentBuild] = []
        self._load_file(bundled_path)
        override = user_override_path or (get_app_data_dir() / "special_augment_builds.json")
        if override.exists():
            self._load_file(override)

    def _load_file(self, path: Path) -> None:
        if not path.exists():
            logger.warning("Special augment DB file not found: %s", path)
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse special augment DB %s: %s", path, exc)
            return
        if raw.get("patch"):
            self.patch = str(raw["patch"])
        if raw.get("updated_at"):
            self.updated_at = str(raw["updated_at"])
        for item in raw.get("archetypes") or []:
            if not isinstance(item, dict):
                continue
            entry = _parse_entry(item)
            if not entry.name:
                continue
            if entry.augment_id:
                self._by_id[entry.augment_id] = entry
            self._by_name[entry.name] = entry
            for alias in entry.aliases:
                self._by_name[alias] = entry
        # Rebuild ordered unique list preserving latest override per id/name
        seen: set[str] = set()
        ordered: list[SpecialAugmentBuild] = []
        for entry in self._by_id.values():
            if entry.name not in seen:
                seen.add(entry.name)
                ordered.append(entry)
        self._entries = ordered

    @property
    def entries(self) -> list[SpecialAugmentBuild]:
        return list(self._entries)

    def get_by_id(self, augment_id: str | int) -> SpecialAugmentBuild | None:
        return self._by_id.get(str(augment_id))

    def get_by_name(self, name: str) -> SpecialAugmentBuild | None:
        if not name:
            return None
        direct = self._by_name.get(name)
        if direct is not None:
            return direct
        clean = name.replace("，", "").replace("！", "").replace("：", "").strip()
        for entry in self._entries:
            eclean = entry.name.replace("，", "").replace("！", "").replace("：", "").strip()
            if clean == eclean or (len(eclean) >= 3 and eclean in clean) or (len(clean) >= 3 and clean in eclean):
                return entry
            for alias in entry.aliases:
                if alias and (alias == clean or alias in name or name in alias):
                    return entry
        return None

    def resolve(self, store: Any | None, augment_id: str | int) -> SpecialAugmentBuild | None:
        hit = self.get_by_id(str(augment_id))
        if hit is not None:
            return hit
        if store is not None:
            aug = store.get_augment(str(augment_id))
            if aug is not None:
                return self.get_by_name(aug.name)
        return None

    def search(self, query: str) -> list[SpecialAugmentBuild]:
        q = query.strip().lower()
        if not q:
            return list(self._entries)
        out: list[SpecialAugmentBuild] = []
        for e in self._entries:
            hay = " ".join((e.name, *e.aliases, e.archetype, e.mechanic, e.avoid_note)).lower()
            if q in hay:
                out.append(e)
        return out

    def export_coach_context(
        self,
        store: Any | None,
        augment_ids: list[str] | tuple[str, ...],
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for aid in augment_ids:
            entry = self.resolve(store, aid)
            if entry is not None and entry.augment_id not in seen:
                seen.add(entry.augment_id)
                out.append(entry.to_coach_dict(store))
        return out


@lru_cache(maxsize=1)
def get_special_augment_db() -> SpecialAugmentDatabase:
    return SpecialAugmentDatabase()
