"""Patch 26.23 Augment-to-Augment Synergy & Gamble Combo Database loader."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from hex_helper.config import get_app_data_dir

logger = logging.getLogger(__name__)

BUNDLED_COMBO_DB_PATH = Path(__file__).resolve().parents[1] / "assets" / "augment_combos.json"

VALID_MECHANIC_AXES: frozenset[str] = frozenset(
    {
        "AP_SPELL",
        "AD_ATTACK",
        "AUTO_ONHIT",
        "CRIT_ATTACK",
        "CRIT_SPELL",
        "HASTE_LOOP",
        "BURN_DOT",
        "TANK_HP",
        "SHIELD_HEAL",
        "ULT_CYCLE",
        "MOBILITY_DASH",
        "GAMBLE_HIGHROLL",
        "UTILITY_SUSTAIN",
    }
)


@dataclass(frozen=True)
class AugmentComboRule:
    combo_id: str
    name: str
    gamble: bool
    profile: str  # "AP" | "AD" | "CRIT_SPELL" | "TANK" | "SUPPORT" | "HYBRID" | "UTILITY"
    core_augments: tuple[str, ...]
    follow_ups: tuple[str, ...]
    hero_bonus: tuple[str, ...]
    anti_axes: tuple[str, ...]
    synergy_reason: str
    reroll_hint: str
    required_anchor: str = ""


def _parse_combo_rule(raw: dict[str, Any]) -> AugmentComboRule:
    return AugmentComboRule(
        combo_id=str(raw.get("id", "")).strip(),
        name=str(raw.get("name", "")).strip(),
        gamble=bool(raw.get("gamble", False)),
        profile=str(raw.get("profile", "HYBRID")).strip().upper() or "HYBRID",
        core_augments=tuple(str(x).strip() for x in (raw.get("core_augments") or []) if str(x).strip()),
        follow_ups=tuple(str(x).strip() for x in (raw.get("follow_ups") or []) if str(x).strip()),
        hero_bonus=tuple(str(x).strip() for x in (raw.get("hero_bonus") or []) if str(x).strip()),
        anti_axes=tuple(
            str(x).strip()
            for x in (raw.get("anti_axes") or [])
            if str(x).strip() in VALID_MECHANIC_AXES
        ),
        synergy_reason=str(raw.get("synergy_reason", "")).strip(),
        reroll_hint=str(raw.get("reroll_hint", "")).strip(),
        required_anchor=str(raw.get("required_anchor", "")).strip(),
    )


class AugmentComboDatabase:
    """In-memory indexed database of augment synergy combos and explicit mechanic axes."""

    def __init__(
        self,
        bundled_path: Path = BUNDLED_COMBO_DB_PATH,
        user_override_path: Path | None = None,
    ) -> None:
        self.patch: str = ""
        self.updated_at: str = ""
        self._by_id: dict[str, AugmentComboRule] = {}
        self._by_augment: dict[str, list[AugmentComboRule]] = {}
        self._by_hero: dict[str, list[AugmentComboRule]] = {}
        self._augment_axes: dict[str, tuple[str, ...]] = {}
        self._load_file(bundled_path)
        override = user_override_path or (get_app_data_dir() / "augment_combos.json")
        if override.exists():
            self._load_file(override)
        self._rebuild_indices()

    def _load_file(self, path: Path) -> None:
        if not path.exists():
            logger.warning("Augment combo DB file not found: %s", path)
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse augment combo DB %s: %s", path, exc)
            return
        if raw.get("patch"):
            self.patch = str(raw["patch"])
        if raw.get("updated_at"):
            self.updated_at = str(raw["updated_at"])
        for item in raw.get("combos") or []:
            if not isinstance(item, dict):
                continue
            rule = _parse_combo_rule(item)
            if rule.combo_id and rule.name and rule.core_augments:
                self._by_id[rule.combo_id] = rule
        raw_axes = raw.get("augment_axes") or {}
        if isinstance(raw_axes, dict):
            for aid, axes in raw_axes.items():
                clean_aid = str(aid).strip()
                if not clean_aid or not isinstance(axes, list):
                    continue
                valid = tuple(
                    dict.fromkeys(
                        str(a).strip() for a in axes if str(a).strip() in VALID_MECHANIC_AXES
                    )
                )
                if valid:
                    self._augment_axes[clean_aid] = valid

    def _rebuild_indices(self) -> None:
        self._by_augment.clear()
        self._by_hero.clear()
        for rule in self._by_id.values():
            seen_a: set[str] = set()
            for aid in (*rule.core_augments, *rule.follow_ups):
                if aid in seen_a:
                    continue
                seen_a.add(aid)
                self._by_augment.setdefault(aid, []).append(rule)
            for hid in rule.hero_bonus:
                self._by_hero.setdefault(hid, []).append(rule)

    @property
    def entries(self) -> list[AugmentComboRule]:
        return list(self._by_id.values())

    def get_by_id(self, combo_id: str) -> AugmentComboRule | None:
        return self._by_id.get(str(combo_id))

    def get_axes(self, augment_id: str | int) -> tuple[str, ...]:
        return self._augment_axes.get(str(augment_id), ())

    def all_explicit_axes(self) -> dict[str, tuple[str, ...]]:
        return dict(self._augment_axes)

    def combos_for_augment(self, augment_id: str | int) -> list[AugmentComboRule]:
        return list(self._by_augment.get(str(augment_id), ()))

    def combos_for_hero(self, hero_id: str | int) -> list[AugmentComboRule]:
        return list(self._by_hero.get(str(hero_id), ()))


@lru_cache(maxsize=1)
def get_augment_combo_db() -> AugmentComboDatabase:
    return AugmentComboDatabase()
