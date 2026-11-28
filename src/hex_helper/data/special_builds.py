"""Patch 26.23 (16.23) Special Augment Build Archetype Database loader & query engine."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from hex_helper.config import get_app_data_dir
from hex_helper.data.store import SIX_DIGIT_ITEM_ALIASES, _MAYHEM_ITEM_PREFIXES

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
    force_item_gate: tuple[tuple[str, Any], ...] = ()

    @property
    def force_item_gate_dict(self) -> dict[str, Any]:
        return dict(self.force_item_gate)

    @property
    def gate_item_id(self) -> str:
        gate = self.force_item_gate_dict
        iid = str(gate.get("item_id", "")).strip()
        if not iid and gate and self.force_items:
            iid = str(self.force_items[0]).strip()
        return iid

    @property
    def gate_min_pr(self) -> float:
        gate = self.force_item_gate_dict
        return float(gate.get("min_pr", 0.0))

    @property
    def gate_min_delta(self) -> float:
        gate = self.force_item_gate_dict
        return float(gate.get("min_delta", -1.0))

    def apply_hero_gate(
        self,
        store: Any | None,
        hero_id: str | int | None,
        *,
        band_mode: str | None = None,
    ) -> SpecialAugmentBuild:
        """Apply data-driven hero gate for force_items / shifts_profile (e.g. 1353/1152 Heartsteel gate).

        When the hero satisfies `PR >= min_pr` and `delta >= min_delta` on `gate_item_id`,
        keep the full transformative profile shift and forced item. Otherwise downgrade to
        a mild HP-item bonus without forcing the gated item or shifting the hero's profile.
        """
        if not self.force_item_gate or store is None or hero_id is None:
            return self
        hid = str(hero_id).strip()
        gate_iid = self.gate_item_id
        gate_canon = SIX_DIGIT_ITEM_ALIASES.get(gate_iid, gate_iid)
        if not hid or not gate_iid:
            return self

        min_pr = self.gate_min_pr
        min_delta = self.gate_min_delta
        use_band = bool(band_mode and str(band_mode).strip().lower() != "all")

        hero = None
        if hasattr(store, "get_hero"):
            try:
                hero = store.get_hero(hid, band_mode=band_mode) if use_band else store.get_hero(hid)
            except TypeError:
                hero = store.get_hero(hid)

        rec = None
        get_item_rec = getattr(store, "get_item_rec", None)
        if callable(get_item_rec):
            try:
                rec = get_item_rec(hid, gate_iid)
            except Exception:
                rec = None
        if rec is None and hasattr(store, "get_hero_item_recs"):
            try:
                recs = store.get_hero_item_recs(hid, band_mode=band_mode) if use_band else store.get_hero_item_recs(hid)
            except TypeError:
                recs = store.get_hero_item_recs(hid)
            if isinstance(recs, dict):
                rec = recs.get(gate_iid) or (recs.get(gate_canon) if gate_canon != gate_iid else None)
                if rec is None:
                    for raw_k, raw_v in recs.items():
                        ks = str(raw_k).strip()
                        ks_alias = SIX_DIGIT_ITEM_ALIASES.get(ks, ks)
                        if ks_alias == gate_canon or (
                            len(ks) == 6 and ks[:2] in _MAYHEM_ITEM_PREFIXES and ks[2:] == gate_canon
                        ):
                            rec = raw_v
                            break

        if (
            hero is not None
            and getattr(hero, "games", 0) > 0
            and rec is not None
            and getattr(rec, "games", 0) > 0
        ):
            pr = rec.games / max(1, hero.games)
            delta = rec.win_rate - hero.win_rate
            if pr + 1e-9 >= min_pr and delta + 1e-9 >= min_delta:
                return self

        filtered_core = tuple(
            iid for iid in self.core_items if SIX_DIGIT_ITEM_ALIASES.get(str(iid), str(iid)) != gate_canon
        )
        return replace(
            self,
            transformative=False,
            shifts_profile="",
            force_items=(),
            allow_off_profile=False,
            core_items=filtered_core,
            boost=min(self.boost, 1.5),
        )

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
    raw_gate = raw.get("force_item_gate")
    gate_pairs: list[tuple[str, Any]] = []
    if isinstance(raw_gate, dict) and raw_gate:
        if raw_gate.get("item_id") is not None and str(raw_gate["item_id"]).strip():
            gate_pairs.append(("item_id", str(raw_gate["item_id"]).strip()))
        if raw_gate.get("min_pr") is not None:
            gate_pairs.append(("min_pr", float(raw_gate["min_pr"])))
        if raw_gate.get("min_delta") is not None:
            gate_pairs.append(("min_delta", float(raw_gate["min_delta"])))
        for k, v in raw_gate.items():
            if k not in ("item_id", "min_pr", "min_delta"):
                gate_pairs.append((str(k), v))
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
        force_item_gate=tuple(gate_pairs),
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

    def resolve_for_hero(
        self,
        store: Any | None,
        augment_id: str | int,
        hero_id: str | int | None = None,
        *,
        band_mode: str | None = None,
    ) -> SpecialAugmentBuild | None:
        entry = self.resolve(store, augment_id)
        if entry is None:
            return None
        if hero_id is not None and entry.force_item_gate:
            return entry.apply_hero_gate(store, hero_id, band_mode=band_mode)
        return entry

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
        hero_id: str | int | None = None,
        *,
        band_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for aid in augment_ids:
            entry = (
                self.resolve_for_hero(store, aid, hero_id, band_mode=band_mode)
                if hero_id is not None
                else self.resolve(store, aid)
            )
            if entry is not None and entry.augment_id not in seen:
                seen.add(entry.augment_id)
                out.append(entry.to_coach_dict(store))
        return out


@lru_cache(maxsize=1)
def get_special_augment_db() -> SpecialAugmentDatabase:
    return SpecialAugmentDatabase()
