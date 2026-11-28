"""Build-defining augment synergy rules (powered by Patch 26.23 Special Augment Database) and owned-item matching."""

from __future__ import annotations

from dataclasses import dataclass

from hex_helper.data.provider import ItemStaticInfo
from hex_helper.data.special_builds import SpecialAugmentBuild, get_special_augment_db
from hex_helper.data.store import SIX_DIGIT_ITEM_ALIASES, DataStore, _MAYHEM_ITEM_PREFIXES

# Normalized item upgrade aliases (ARAM/tear items -> base legendary equivalents)
ITEM_EQUIVALENCE: dict[str, str] = {
    "3040": "3003",  # 炽天使之拥 <-> 大天使之杖
    "3042": "3004",  # 魔切 <-> 魔宗
    "3121": "3119",  # 芬布尔之冬 <-> 凛冬之临
    "2530": "2526",
}


def normalize_item_id(item_id: str | int) -> str:
    s = str(item_id).strip()
    s = SIX_DIGIT_ITEM_ALIASES.get(s, s)
    # Strip 12xxxx / 22xxxx / 32xxxx / 44xxxx / 77xxxx / 99xxxx Arena/Mayhem variant prefixes if base 4-digit exists
    if len(s) == 6 and s[:2] in _MAYHEM_ITEM_PREFIXES:
        s = s[2:]
    return ITEM_EQUIVALENCE.get(s, s)


def item_is_owned(item_id: str, owned_item_ids: set[str] | tuple[str, ...] | list[str]) -> bool:
    target = normalize_item_id(item_id)
    return any(normalize_item_id(oid) == target for oid in owned_item_ids)


@dataclass(frozen=True)
class AugmentSynergyRule:
    keyword: str
    label: str
    required_tags: tuple[str, ...]
    preferred_item_ids: tuple[str, ...] = ()
    stat_keys: tuple[str, ...] = ()
    boost: float = 2.0
    shifts_profile: str = ""


# Fallback extra transformative keywords not explicitly in the 45-entry build DB
_EXTRA_TRANSFORMATIVE_KEYWORDS: tuple[str, ...] = (
    "万用瞄准镜",
    "物法皆修",
    "质变",
    "扳机炼狱",
    "全凭身法",
    "全凭手感",
)


def resolve_special_builds(
    store: DataStore | None,
    selected_augment_ids: list[str] | tuple[str, ...],
    hero_id: str | int | None = None,
    *,
    band_mode: str | None = None,
) -> list[SpecialAugmentBuild]:
    """Return all SpecialAugmentBuild entries matched by currently selected augments."""
    db = get_special_augment_db()
    out: list[SpecialAugmentBuild] = []
    seen: set[str] = set()
    for aid in selected_augment_ids:
        entry = (
            db.resolve_for_hero(store, aid, hero_id, band_mode=band_mode)
            if hero_id is not None
            else db.resolve(store, aid)
        )
        if entry is not None and entry.augment_id not in seen:
            seen.add(entry.augment_id)
            out.append(entry)
    return out


def is_transformative_augment(
    store: DataStore,
    augment_id: str,
    hero_id: str | int | None = None,
    *,
    band_mode: str | None = None,
) -> bool:
    """Return True if this augment justifies overhauling an already-committed match build plan."""
    db = get_special_augment_db()
    entry = (
        db.resolve_for_hero(store, augment_id, hero_id, band_mode=band_mode)
        if hero_id is not None
        else db.resolve(store, augment_id)
    )
    if entry is not None:
        return entry.transformative
    aug = store.get_augment(str(augment_id))
    if aug is None:
        return False
    return any(kw in aug.name for kw in _EXTRA_TRANSFORMATIVE_KEYWORDS)


def get_special_augment_candidate_items(
    store: DataStore,
    selected_augment_ids: list[str] | tuple[str, ...],
) -> tuple[list[str], set[str]]:
    """Return (candidate_item_ids_to_inject, off_profile_exempt_item_ids) from selected special augments."""
    candidates: list[str] = []
    exempt: set[str] = set()
    for entry in resolve_special_builds(store, selected_augment_ids):
        for raw_id in (*entry.force_items, *entry.core_items):
            nid = normalize_item_id(raw_id)
            if not nid:
                continue
            if nid not in candidates:
                candidates.append(nid)
            if entry.allow_off_profile or raw_id in entry.force_items:
                exempt.add(nid)
    return candidates, exempt


def evaluate_augment_synergies(
    store: DataStore,
    selected_augment_ids: list[str] | tuple[str, ...],
    item: ItemStaticInfo,
) -> tuple[float, list[str]]:
    """Return (synergy_score_delta, matched_labels) for an item using the Patch 26.23 Special Augment DB."""
    bonus = 0.0
    labels: list[str] = []
    norm_id = normalize_item_id(item.item_id)
    item_tags = set(item.tags)

    for entry in resolve_special_builds(store, selected_augment_ids):
        force_set = {normalize_item_id(x) for x in entry.force_items}
        core_list = [normalize_item_id(x) for x in entry.core_items]
        core_set = set(core_list)

        # Explicit anti-synergy penalty (e.g. 嗡嗡怪兽 converts AD->AS, so pure zero-AD AttackSpeed items are bad!)
        if entry.penalize_tags and (item_tags & set(entry.penalize_tags)):
            if item.stats.get("FlatPhysicalDamageMod", 0.0) <= 0 and norm_id not in core_set:
                bonus -= 3.5
                continue

        if norm_id in force_set:
            bonus += max(entry.boost + 2.8, 7.6)
            lbl = f"专属流派核心 · {entry.name}"
            if lbl not in labels:
                labels.append(lbl)
        elif norm_id in core_set:
            idx = core_list.index(norm_id)
            pos_adj = 1.0 if idx < 2 else (-0.8 if idx >= 4 else 0.0)
            bonus += max(1.0, entry.boost + pos_adj)
            lbl = f"流派适配 · {entry.archetype}"
            if lbl not in labels:
                labels.append(lbl)
        else:
            tag_hit = bool(entry.required_tags and (item_tags & set(entry.required_tags)))
            stat_hit = bool(entry.stat_keys and any(item.stats.get(k, 0.0) > 0 for k in entry.stat_keys))
            if tag_hit or stat_hit:
                bonus += round(entry.boost * 0.55, 2)
                lbl = f"流派适配 · {entry.name}"
                if lbl not in labels:
                    labels.append(lbl)

    return round(bonus, 2), labels


def detect_profile_override(
    store: DataStore,
    selected_augment_ids: list[str] | tuple[str, ...],
) -> str:
    """Return 'AP' | 'AD' | 'TANK' if a selected special augment fundamentally shifts build profile."""
    for entry in resolve_special_builds(store, selected_augment_ids):
        if entry.shifts_profile:
            return entry.shifts_profile
    return ""
