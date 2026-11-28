"""Thread-safe in-memory read-only snapshot store for HexHelper datasets."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path

from hex_helper.data.provider import (
    AugmentInfo,
    CoreBuild,
    DatasetDescriptor,
    DestinyData,
    DestinyItemEntry,
    HeroAugmentStat,
    HeroStat,
    ItemRecStat,
    ItemStaticInfo,
    SpellComboStat,
)
from hex_helper.data.locale_pack import (
    all_overseas_hero_aliases,
    build_ocr_catalog,
    localize_augment_name,
    localize_hero_name,
    localize_item_name,
    normalize_client_locale,
)
from hex_helper.data.static_assets import TENCENT_IMG_BASE, StaticAssetsManager
from hex_helper.scoring.tier import DEFAULT_PRIOR_K, RarityDeltaDist, _compute_percentile

# Base <-> stacked Tear item pairs to merge so unstacked games never suffer artificial WR penalties.
TEAR_ITEM_PAIRS: tuple[tuple[str, str], ...] = (
    ("3003", "3040"),  # 大天使之杖 <-> 炽天使之拥
    ("3004", "3042"),  # 魔宗 <-> 魔切
    ("3119", "3121"),  # 凛冬之临 <-> 末日寒冬
    ("2526", "2530"),  # 耳语头环 <-> 歌之权冠
)
TEAR_STACKED_TO_BASE: dict[str, str] = {stacked: base for base, stacked in TEAR_ITEM_PAIRS}
TEAR_STACKED_IDS: frozenset[str] = frozenset(TEAR_STACKED_TO_BASE.keys())
TEAR_ALL_IDS: frozenset[str] = frozenset(x for pair in TEAR_ITEM_PAIRS for x in pair)

ITEM_GLOBAL_BIAS_MIN_GAMES: int = 1000
ITEM_GLOBAL_BIAS_MIN_PR: float = 0.08
DATA_BAND_HIGH_WEIGHTS: dict[str, float] = {
    "all": 0.0,
    "blended": 0.5,
    "high": 1.0,
}
HIGH_BAND_FULL_WEIGHT_GAMES: float = 300.0
AUGMENT_POOL_MIN_GAMES: int = 20
AUGMENT_POOL_MIN_PR: float = 0.00008
AUGMENT_POOL_REAL_HERO_MIN_GAMES: int = 500
_MAYHEM_ITEM_PREFIXES: tuple[str, ...] = ("12", "22", "32", "44", "77", "99")
SIX_DIGIT_ITEM_ALIASES: dict[str, str] = {
    "663039": "3039",  # 阿塔玛的清算
    "663146": "3146",  # 海克斯科技枪刃
    "663193": "3193",  # 石像鬼石板甲
    "664011": "4011",  # 花晓之剑
    "664403": "4403",  # 金铲铲
    "664644": "4644",  # 破碎王后之冕
    "667666": "6676",  # 收集者
}
_CANON_TO_SIX_DIGIT_ALIASES: dict[str, str] = {
    canon: six for six, canon in SIX_DIGIT_ITEM_ALIASES.items()
}
_TRINKET_AND_FREE_ITEM_IDS: frozenset[str] = frozenset(
    {"2052", "2054", "220013", "3340", "3363", "3364", "3330", "3513"}
)
_BOOT_ITEM_IDS: frozenset[str] = frozenset(
    {
        "1001", "3006", "3009", "3020", "3047", "3111", "3158", "3010", "3013",
        "3170", "3171", "3172", "3173", "3174", "3175", "3176",
    }
)
_ENCHANTER_SUPPORT_ITEM_IDS: frozenset[str] = frozenset(
    {"6617", "2526", "2530", "3504", "6616", "3107", "6621", "3222", "6620", "2065", "4011"}
)


def is_complete_equipment_info(info: ItemStaticInfo) -> bool:
    """Return True if an ItemStaticInfo represents a purchasable completed legendary item (even without item_metadata)."""
    if info.category == "complete_equipment":
        return True
    if info.gold < 2200:
        return False
    if not info.name or info.name.startswith("装备#"):
        return False
    if info.category in ("boots", "consumable"):
        return False
    tags = set(info.tags or ())
    if tags & {"Boots", "Consumable", "Trinket"}:
        return False
    nid = _canonical_item_key(info.item_id)
    if nid in _BOOT_ITEM_IDS or nid in _TRINKET_AND_FREE_ITEM_IDS or str(info.item_id) in _TRINKET_AND_FREE_ITEM_IDS:
        return False
    if info.name.endswith("靴") or info.name.endswith("胫甲"):
        return False
    return True


def _canonical_item_key(item_id: str | int) -> str:
    s = str(item_id).strip()
    s = SIX_DIGIT_ITEM_ALIASES.get(s, s)
    if len(s) == 6 and s[:2] in _MAYHEM_ITEM_PREFIXES:
        s = s[2:]
    return TEAR_STACKED_TO_BASE.get(s, s)


def _quantile_sorted(sorted_vals: list[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    pos = max(0.0, min(1.0, q)) * (n - 1)
    lo = int(math.floor(pos))
    hi = min(n - 1, lo + 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _read_json_safe(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def compute_tear_backfill_scale(
    inner_recs: dict[str, ItemRecStat],
    raw_hero: dict[str, tuple[int, int]],
) -> float:
    """Compute recs/raw caliber scale strictly on shared non-Tear items, clamped to [0.10, 1.0]."""
    common = (set(inner_recs.keys()) & set(raw_hero.keys())) - TEAR_ALL_IDS
    rec_common_g = sum(inner_recs[k].games for k in common if inner_recs[k].games > 0)
    raw_common_g = sum(raw_hero[k][0] for k in common if raw_hero[k][0] > 0)
    if common and raw_common_g > 0 and rec_common_g > 0:
        scale = rec_common_g / raw_common_g
    else:
        rec_all_g = sum(r.games for r in inner_recs.values() if r.games > 0)
        raw_all_g = sum(g for g, _ in raw_hero.values() if g > 0)
        scale = (rec_all_g / raw_all_g) if (raw_all_g > 0 and rec_all_g > 0) else 1.0
    return max(0.10, min(1.0, scale))


def _parse_heroes_map(heroes_raw: dict) -> dict[str, HeroStat]:
    heroes: dict[str, HeroStat] = {}
    for hid, h in heroes_raw.items():
        if not isinstance(h, dict):
            continue
        hid_s = str(hid)
        heroes[hid_s] = HeroStat(
            hero_id=hid_s,
            name=str(h.get("name", hid_s)),
            title=str(h.get("title", "")),
            games=int(h.get("games", 0) or 0),
            wins=int(h.get("wins", 0) or 0),
            win_rate=float(h.get("win_rate", 0.5)),
            pick_rate=float(h.get("pick_rate", 0.0)),
            rank=int(h.get("rank", 999)),
            low_sample=bool(h.get("low_sample", False)),
        )
    return heroes


def _parse_hero_augments_map(ha_raw: dict) -> dict[str, dict[str, HeroAugmentStat]]:
    hero_augments: dict[str, dict[str, HeroAugmentStat]] = {}
    for hid, aug_map in ha_raw.items():
        if not isinstance(aug_map, dict):
            continue
        hid_s = str(hid)
        inner: dict[str, HeroAugmentStat] = {}
        for aid, st in aug_map.items():
            if not isinstance(st, dict):
                continue
            aid_s = str(aid)
            inner[aid_s] = HeroAugmentStat(
                augment_id=aid_s,
                games=int(st.get("games", 0) or 0),
                wins=int(st.get("wins", 0) or 0),
                win_rate=float(st.get("win_rate", 0.5)),
                low_sample=bool(st.get("low_sample", False)),
            )
        hero_augments[hid_s] = inner
    return hero_augments


def _parse_core_builds_map(core_raw: dict) -> dict[str, list[CoreBuild]]:
    core_builds: dict[str, list[CoreBuild]] = {}
    for hid, blist in core_raw.items():
        if not isinstance(blist, list):
            continue
        parsed_b: list[CoreBuild] = []
        for b in blist:
            if not isinstance(b, dict):
                continue
            parsed_b.append(
                CoreBuild(
                    items=tuple(str(x) for x in b.get("items", [])),
                    ordered=bool(b.get("ordered", True)),
                    games=int(b.get("games", 0) or 0),
                    wins=int(b.get("wins", 0) or 0),
                    win_rate=float(b.get("win", b.get("win_rate", 0.5))),
                    pick_rate=float(b.get("pick", 0.0)),
                )
            )
        core_builds[str(hid)] = parsed_b
    return core_builds


def _parse_item_recs_with_tear_backfill(
    item_recs_raw: dict,
    items_raw: dict | None = None,
) -> dict[str, dict[str, ItemRecStat]]:
    """Parse hero_item_recommendations.json and merge/backfill stacked Tear items from optional hero_items_raw.json."""
    item_recs: dict[str, dict[str, ItemRecStat]] = {}
    raw_src = items_raw if isinstance(items_raw, dict) else {}
    for hid, imap in item_recs_raw.items():
        if not isinstance(imap, dict):
            continue
        hid_s = str(hid)
        inner_i: dict[str, ItemRecStat] = {}
        for iid, st in imap.items():
            if not isinstance(st, dict):
                continue
            iid_s = str(iid)
            inner_i[iid_s] = ItemRecStat(
                item_id=iid_s,
                games=int(st.get("games", 0) or 0),
                wins=int(st.get("wins", 0) or 0),
                win_rate=float(st.get("win_rate", 0.5)),
                low_sample=bool(st.get("low_sample", False)),
            )

        raw_hero_dict = raw_src.get(hid_s)
        raw_hero: dict[str, tuple[int, int]] = {}
        if isinstance(raw_hero_dict, dict):
            for riid, rst in raw_hero_dict.items():
                if not isinstance(rst, dict):
                    continue
                rg = int(rst.get("games", 0) or 0)
                if rg <= 0:
                    continue
                rw = int(rst.get("wins", 0) or 0)
                raw_hero[str(riid)] = (rg, max(0, min(rg, rw)))

        # Compute caliber scale BEFORE mutating inner_i with Tear merges.
        scale = compute_tear_backfill_scale(inner_i, raw_hero) if raw_hero else 1.0

        for base_id, stacked_id in TEAR_ITEM_PAIRS:
            b_rec = inner_i.get(base_id)
            s_rec = inner_i.get(stacked_id)
            backfilled = False
            adj_g = 0
            adj_w = 0
            if (s_rec is None or s_rec.games <= 0) and stacked_id in raw_hero:
                raw_g, raw_w = raw_hero[stacked_id]
                if raw_g > 0:
                    adj_g = int(round(raw_g * scale))
                    adj_w = max(0, min(adj_g, int(round(raw_w * scale))))
                    if adj_g > 0:
                        backfilled = True

            if b_rec is None and s_rec is None and not backfilled:
                continue

            if backfilled:
                tot_g = (b_rec.games if b_rec else 0) + adj_g
                tot_w = (b_rec.wins if b_rec else 0) + adj_w
                merged_wr = (tot_w / tot_g) if tot_g > 0 else 0.5
                merged_low = tot_g < 300
            else:
                tot_g = (b_rec.games if b_rec else 0) + (s_rec.games if s_rec else 0)
                tot_w = (b_rec.wins if b_rec else 0) + (s_rec.wins if s_rec else 0)
                merged_wr = (tot_w / tot_g) if tot_g > 0 else 0.5
                if b_rec is not None and s_rec is not None:
                    merged_low = tot_g < 300
                else:
                    only = b_rec or s_rec
                    merged_low = bool(only.low_sample) if only is not None else True

            inner_i[base_id] = ItemRecStat(
                item_id=base_id,
                games=tot_g,
                wins=tot_w,
                win_rate=merged_wr,
                low_sample=merged_low,
            )
            inner_i[stacked_id] = ItemRecStat(
                item_id=stacked_id,
                games=tot_g,
                wins=tot_w,
                win_rate=merged_wr,
                low_sample=merged_low,
            )
        item_recs[hid_s] = inner_i
    return item_recs


@dataclass
class _ModeSnapshot:
    mode: str
    descriptor: DatasetDescriptor
    dataset_dir: Path
    active_band: str
    heroes: dict[str, HeroStat]
    augments: dict[str, AugmentInfo]
    hero_augments: dict[str, dict[str, HeroAugmentStat]]
    hero_spells: dict[str, list[SpellComboStat]]
    core_builds: dict[str, list[CoreBuild]]
    destiny: dict[str, DestinyData]
    augment_items: dict[tuple[str, str], DestinyData]
    item_recs: dict[str, dict[str, ItemRecStat]]
    item_meta_raw: dict[str, dict]
    hero_name_index: dict[str, str]
    item_global_bias: dict[str, float]
    hero_rarity_dists: dict[tuple[str, str], RarityDeltaDist]
    role_item_priors: dict[tuple[str, str], float]
    tear_merged: bool = True
    has_high_band: bool = False
    high_heroes: dict[str, HeroStat] = field(default_factory=dict)
    high_hero_augments: dict[str, dict[str, HeroAugmentStat]] = field(default_factory=dict)
    high_item_recs: dict[str, dict[str, ItemRecStat]] = field(default_factory=dict)
    high_core_builds: dict[str, list[CoreBuild]] = field(default_factory=dict)
    high_destiny: dict[str, DestinyData] = field(default_factory=dict)
    high_augment_items: dict[tuple[str, str], DestinyData] = field(default_factory=dict)


class DataStore:
    """In-memory indexed store loaded from a verified local dataset directory."""

    def __init__(self, static_assets: StaticAssetsManager) -> None:
        self._lock = threading.RLock()
        self.static = static_assets
        self.descriptor: DatasetDescriptor | None = None
        self.active_band: str = ""
        self.dataset_dir: Path | None = None

        self._heroes: dict[str, HeroStat] = {}
        self._augments: dict[str, AugmentInfo] = {}
        self._hero_augments: dict[str, dict[str, HeroAugmentStat]] = {}
        self._hero_spells: dict[str, list[SpellComboStat]] = {}
        self._core_builds: dict[str, list[CoreBuild]] = {}
        self._destiny: dict[str, DestinyData] = {}
        self._augment_items: dict[tuple[str, str], DestinyData] = {}
        self._item_recs: dict[str, dict[str, ItemRecStat]] = {}
        self._item_meta_raw: dict[str, dict] = {}

        # Optional native bands/high tables for active mode
        self._has_high_band: bool = False
        self._high_heroes: dict[str, HeroStat] = {}
        self._high_hero_augments: dict[str, dict[str, HeroAugmentStat]] = {}
        self._high_item_recs: dict[str, dict[str, ItemRecStat]] = {}
        self._high_core_builds: dict[str, list[CoreBuild]] = {}
        self._high_destiny: dict[str, DestinyData] = {}
        self._high_augment_items: dict[tuple[str, str], DestinyData] = {}

        # Fast reverse lookup: normalized name / alias / title -> hero_id
        self._hero_name_index: dict[str, str] = {}

        # Precomputed statistical calibration tables
        self._item_global_bias: dict[str, float] = {}
        self._hero_rarity_dists: dict[tuple[str, str], RarityDeltaDist] = {}
        self._role_item_priors: dict[tuple[str, str], float] = {}
        self._tear_merged: bool = False

        # Dual-mode snapshot cache ("normal" <-> "first-prismatic")
        self._mode_snapshots: dict[str, _ModeSnapshot] = {}
        self._active_mode: str = "normal"

        # Cached authoritative hero augment pool across all loaded modes: hero_id -> (has_pool_data, eligible_ids)
        self._eligible_pool_cache: dict[str, tuple[bool, frozenset[str]]] = {}
        self._client_locale: str = "zh_cn"

    @property
    def client_locale(self) -> str:
        with self._lock:
            return self._client_locale

    def set_client_locale(self, locale: str) -> None:
        norm = normalize_client_locale(locale)
        with self._lock:
            self._client_locale = norm

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return bool(self._heroes and self._augments)

    @property
    def active_match_mode(self) -> str:
        with self._lock:
            return self._active_mode

    @property
    def has_first_prismatic(self) -> bool:
        with self._lock:
            return "first-prismatic" in self._mode_snapshots

    @property
    def tear_pairs_merged(self) -> bool:
        with self._lock:
            return self._tear_merged

    @property
    def item_global_bias(self) -> dict[str, float]:
        with self._lock:
            return dict(self._item_global_bias)

    def _infer_static_hero_profile(
        self,
        core_builds: list[CoreBuild],
        item_recs: dict[str, ItemRecStat],
    ) -> str:
        ad_s = 0.0
        ap_s = 0.0
        tank_s = 0.0
        sup_s = 0.0

        sample_items: list[str] = []
        for cb in core_builds[:2]:
            sample_items.extend(cb.items)
        if not sample_items and item_recs:
            top_recs = sorted(item_recs.values(), key=lambda r: r.games, reverse=True)[:5]
            sample_items = [r.item_id for r in top_recs]

        for iid in sample_items:
            info = self.get_item_info(iid)
            st = info.stats or {}
            tags = set(info.tags or ())
            nid = _canonical_item_key(iid)
            ap_v = float(st.get("FlatMagicDamageMod", 0.0) or 0.0)
            ad_v = float(st.get("FlatPhysicalDamageMod", 0.0) or 0.0)
            if (nid in _ENCHANTER_SUPPORT_ITEM_IDS) or (
                ("ManaRegen" in tags or "Heal" in tags) and ap_v <= 75.0 and ad_v <= 0.0 and info.category != "consumable"
            ):
                sup_s += 85.0
                ap_s += ap_v * 0.35
                tank_s += float(st.get("FlatHPPoolMod", 0.0) or 0.0) * 0.03
                continue
            ad_s += ad_v * 1.1
            ad_s += float(st.get("FlatCritChanceMod", 0.0) or 0.0) * 100.0
            ap_s += ap_v * 0.85
            tank_s += (
                float(st.get("FlatArmorMod", 0.0) or 0.0) * 1.1
                + float(st.get("FlatSpellBlockMod", 0.0) or 0.0) * 1.1
                + float(st.get("FlatHPPoolMod", 0.0) or 0.0) * 0.10
            )

        if sup_s >= max(ad_s, ap_s, tank_s) * 1.05 and sup_s > 45.0:
            return "SUPPORT"
        if tank_s >= max(ad_s, ap_s, sup_s) * 1.15 and tank_s > 60:
            return "TANK"
        if ap_s > ad_s * 1.15:
            return "AP"
        return "AD"

    def _parse_augment_items_tables(
        self,
        destiny_raw: dict,
        heroes: dict[str, HeroStat],
        fallback_heroes: dict[str, HeroStat] | None = None,
    ) -> tuple[dict[str, DestinyData], dict[tuple[str, str], DestinyData]]:
        destiny: dict[str, DestinyData] = {}
        augment_items: dict[tuple[str, str], DestinyData] = {}
        for hid, aug_dict in destiny_raw.items():
            if not isinstance(aug_dict, dict):
                continue
            hid_s = str(hid)
            hero_stat = heroes.get(hid_s) or (fallback_heroes.get(hid_s) if fallback_heroes else None)
            for aid, entry_raw in aug_dict.items():
                if not isinstance(entry_raw, dict):
                    continue
                aid_s = str(aid)
                bl = entry_raw.get("baseline")
                bl_dict = bl if isinstance(bl, dict) else {}
                bl_games = int(bl_dict.get("games", 0) or 0)
                bl_wins = int(bl_dict.get("wins", 0) or 0)
                # P0-6: destiny_style_v1 omits top-level baseline; inherit hero's real games/wins, NEVER 0.50!
                if bl_games <= 0 and hero_stat is not None and hero_stat.games > 0:
                    bl_games = int(hero_stat.games)
                    bl_wins = int(hero_stat.wins)
                    if bl_wins <= 0 and 0.0 < hero_stat.win_rate < 1.0:
                        bl_wins = int(round(hero_stat.win_rate * bl_games))
                items_list: list[DestinyItemEntry] = []
                for it in entry_raw.get("items") or []:
                    if not isinstance(it, dict):
                        continue
                    item_id_s = str(it.get("item", ""))
                    ctrl = it.get("control")
                    ctrl_dict = ctrl if isinstance(ctrl, dict) else {}
                    items_list.append(
                        DestinyItemEntry(
                            item_id=item_id_s,
                            name=str(it.get("name") or self.static.get_item_name(item_id_s)),
                            games=int(it.get("games", 0) or 0),
                            wins=int(it.get("wins", 0) or 0),
                            low_sample=bool(it.get("low_sample", False)),
                            control_games=int(ctrl_dict.get("games", 0) or 0),
                            control_wins=int(ctrl_dict.get("wins", 0) or 0),
                            style=str(it.get("style") or "").strip(),
                        )
                    )
                parsed_entry = DestinyData(
                    baseline_games=bl_games,
                    baseline_wins=bl_wins,
                    items=tuple(items_list),
                )
                augment_items[(hid_s, aid_s)] = parsed_entry
                if aid_s == "3081":
                    destiny[hid_s] = parsed_entry
        return destiny, augment_items

    def _build_snapshot(
        self,
        descriptor: DatasetDescriptor,
        dataset_dir: Path,
        band: str,
        mode_name: str,
        *,
        refresh_static: bool = True,
    ) -> _ModeSnapshot:
        base = Path(dataset_dir)
        band_dir = base / "bands" / band if band else None
        use_band = bool(
            band_dir
            and (band_dir / "heroes.json").exists()
            and (band_dir / "hero_augments.json").exists()
        )

        heroes_raw = _read_json_safe((band_dir / "heroes.json") if use_band else (base / "heroes.json"))
        augs_raw = _read_json_safe(base / "augments.json")
        ha_raw = _read_json_safe(
            (band_dir / "hero_augments.json") if use_band else (base / "hero_augments.json")
        )
        spells_raw = _read_json_safe(base / "hero_spells.json")
        core_raw = _read_json_safe(base / "core_builds.json")
        destiny_raw = _read_json_safe(base / "hero_augment_items.json")
        item_recs_raw = _read_json_safe(base / "hero_item_recommendations.json")
        items_raw = _read_json_safe(base / "hero_items_raw.json")
        item_meta_raw = _read_json_safe(base / "item_metadata.json")

        # Refresh static item merge with dataset item_metadata.json on primary load
        if refresh_static:
            self.static.load_cached(dataset_item_meta=item_meta_raw)

        heroes = _parse_heroes_map(heroes_raw)
        name_index: dict[str, str] = {}
        for hid_s, hs in heroes.items():
            for key in (
                hs.name,
                hs.title,
                f"{hs.title}{hs.name}",
                self.static.get_champion_alias(hid_s),
                self.static.hero_cn_names.get(hid_s, ""),
            ):
                norm = _norm_name(key)
                if norm:
                    name_index[norm] = hid_s
        for hid_s, ov_names in all_overseas_hero_aliases().items():
            if hid_s not in heroes:
                continue
            for ov_name in ov_names:
                norm = _norm_name(ov_name)
                if norm:
                    name_index.setdefault(norm, hid_s)

        augments: dict[str, AugmentInfo] = {}
        for aid, a in augs_raw.items():
            aid_s = str(aid)
            augments[aid_s] = AugmentInfo(
                augment_id=aid_s,
                name=str(a.get("name", aid_s)),
                rarity=str(a.get("rarity", "gold")),
                icon_url=self.static.augment_icon_urls.get(aid_s, ""),
            )

        hero_augments = _parse_hero_augments_map(ha_raw)

        hero_spells: dict[str, list[SpellComboStat]] = {}
        for hid, sp_map in spells_raw.items():
            combos: list[SpellComboStat] = []
            for combo_key, st in sp_map.items():
                parts = str(combo_key).split("+")
                if len(parts) == 2:
                    combos.append(
                        SpellComboStat(
                            spell_ids=(parts[0], parts[1]),
                            games=int(st.get("games", 0)),
                            wins=int(st.get("wins", 0)),
                            win_rate=float(st.get("win_rate", 0.5)),
                            low_sample=bool(st.get("low_sample", False)),
                        )
                    )
            combos.sort(key=lambda c: (c.games >= 300, c.win_rate, c.games), reverse=True)
            hero_spells[str(hid)] = combos

        core_builds = _parse_core_builds_map(core_raw)
        destiny, augment_items = self._parse_augment_items_tables(destiny_raw, heroes)
        item_recs = _parse_item_recs_with_tear_backfill(item_recs_raw, items_raw)

        # Load optional native bands/high snapshot tables when present on disk
        high_dir = base / "bands" / "high"
        high_heroes: dict[str, HeroStat] = {}
        high_hero_augments: dict[str, dict[str, HeroAugmentStat]] = {}
        high_item_recs: dict[str, dict[str, ItemRecStat]] = {}
        high_core_builds: dict[str, list[CoreBuild]] = {}
        high_destiny: dict[str, DestinyData] = {}
        high_augment_items: dict[tuple[str, str], DestinyData] = {}
        if high_dir.is_dir():
            high_heroes = _parse_heroes_map(_read_json_safe(high_dir / "heroes.json"))
            high_hero_augments = _parse_hero_augments_map(_read_json_safe(high_dir / "hero_augments.json"))
            high_core_builds = _parse_core_builds_map(_read_json_safe(high_dir / "core_builds.json"))
            high_destiny, high_augment_items = self._parse_augment_items_tables(
                _read_json_safe(high_dir / "hero_augment_items.json"),
                high_heroes,
                fallback_heroes=heroes,
            )
            high_item_recs = _parse_item_recs_with_tear_backfill(
                _read_json_safe(high_dir / "hero_item_recommendations.json"),
                _read_json_safe(high_dir / "hero_items_raw.json"),
            )
        has_high_band = bool(high_item_recs or high_core_builds or high_hero_augments or high_destiny)

        # Precompute canonical per-hero item stats for mainstream global bias & hierarchical role priors
        global_weighted_num: dict[str, float] = {}
        global_weighted_den: dict[str, int] = {}
        global_hero_cnt: dict[str, int] = {}
        role_deltas: dict[tuple[str, str], list[float]] = {}

        for hid, imap in item_recs.items():
            hero = heroes.get(hid)
            if hero is None:
                continue
            h_wr = hero.win_rate if 0.05 <= hero.win_rate <= 0.95 else 0.50
            h_games = max(1, hero.games)
            profile = self._infer_static_hero_profile(core_builds.get(hid, []), imap)

            # Aggregate canonical keys per hero without double-counting merged Tear stacked aliases
            canon_agg: dict[str, tuple[int, int]] = {}
            for raw_iid, rec in imap.items():
                if raw_iid in TEAR_STACKED_IDS and TEAR_STACKED_TO_BASE[raw_iid] in imap:
                    continue
                cid = _canonical_item_key(raw_iid)
                prev_g, prev_w = canon_agg.get(cid, (0, 0))
                canon_agg[cid] = (prev_g + rec.games, prev_w + rec.wins)

            for cid, (g, w) in canon_agg.items():
                if g <= 0:
                    continue
                delta = (w / g) - h_wr
                if g >= ITEM_GLOBAL_BIAS_MIN_GAMES and (g / h_games) >= ITEM_GLOBAL_BIAS_MIN_PR:
                    global_weighted_num[cid] = global_weighted_num.get(cid, 0.0) + delta * g
                    global_weighted_den[cid] = global_weighted_den.get(cid, 0) + g
                    global_hero_cnt[cid] = global_hero_cnt.get(cid, 0) + 1
                if g >= 300:
                    role_deltas.setdefault((profile, cid), []).append(delta)

        item_global_bias: dict[str, float] = {}
        for cid, den in global_weighted_den.items():
            if global_hero_cnt.get(cid, 0) >= 2 and den > 0:
                mean_d = global_weighted_num[cid] / den
                item_global_bias[cid] = round(max(-0.06, min(0.06, mean_d)), 6)

        role_item_priors: dict[tuple[str, str], float] = {}
        for key, ds in role_deltas.items():
            if len(ds) >= 2:
                mean_d = sum(ds) / len(ds)
                role_item_priors[key] = round(max(-0.10, min(0.08, mean_d)), 6)

        # Precompute (hero_id, rarity) empirical Bayesian-smoothed delta distributions
        hero_rarity_dists: dict[tuple[str, str], RarityDeltaDist] = {}
        for hid, aug_map in hero_augments.items():
            hero = heroes.get(hid)
            prior = hero.win_rate if (hero and 0.05 <= hero.win_rate <= 0.95) else 0.50
            by_rarity: dict[str, list[float]] = {}
            for aid, st in aug_map.items():
                if st.games < 30:
                    continue
                aug = augments.get(aid)
                rar = (aug.rarity if aug is not None else "gold").strip().lower() or "gold"
                sm_wr = (st.wins + DEFAULT_PRIOR_K * prior) / (st.games + DEFAULT_PRIOR_K)
                by_rarity.setdefault(rar, []).append(sm_wr - prior)
            for rar, ds in by_rarity.items():
                if len(ds) < 3:
                    continue
                ds.sort()
                n = len(ds)
                mean_v = sum(ds) / n
                var_v = sum((x - mean_v) ** 2 for x in ds) / n
                hero_rarity_dists[(hid, rar)] = RarityDeltaDist(
                    mean=mean_v,
                    std=math.sqrt(var_v),
                    p25=_quantile_sorted(ds, 0.25),
                    median=_quantile_sorted(ds, 0.50),
                    p75=_quantile_sorted(ds, 0.75),
                    p90=_quantile_sorted(ds, 0.90),
                    sorted_deltas=tuple(ds),
                )

        return _ModeSnapshot(
            mode=mode_name,
            descriptor=descriptor,
            dataset_dir=base,
            active_band=band if use_band else "",
            heroes=heroes,
            augments=augments,
            hero_augments=hero_augments,
            hero_spells=hero_spells,
            core_builds=core_builds,
            destiny=destiny,
            augment_items=augment_items,
            item_recs=item_recs,
            item_meta_raw=item_meta_raw,
            hero_name_index=name_index,
            item_global_bias=item_global_bias,
            hero_rarity_dists=hero_rarity_dists,
            role_item_priors=role_item_priors,
            tear_merged=True,
            has_high_band=has_high_band,
            high_heroes=high_heroes,
            high_hero_augments=high_hero_augments,
            high_item_recs=high_item_recs,
            high_core_builds=high_core_builds,
            high_destiny=high_destiny,
            high_augment_items=high_augment_items,
        )

    def _apply_snapshot_locked(self, snap: _ModeSnapshot) -> None:
        self.descriptor = snap.descriptor
        self.dataset_dir = snap.dataset_dir
        self.active_band = snap.active_band
        self._heroes = snap.heroes
        self._augments = snap.augments
        self._hero_augments = snap.hero_augments
        self._hero_spells = snap.hero_spells
        self._core_builds = snap.core_builds
        self._destiny = snap.destiny
        self._augment_items = snap.augment_items
        self._item_recs = snap.item_recs
        self._item_meta_raw = snap.item_meta_raw
        self._has_high_band = snap.has_high_band
        self._high_heroes = snap.high_heroes
        self._high_hero_augments = snap.high_hero_augments
        self._high_item_recs = snap.high_item_recs
        self._high_core_builds = snap.high_core_builds
        self._high_destiny = snap.high_destiny
        self._high_augment_items = snap.high_augment_items
        self._hero_name_index = snap.hero_name_index
        self._item_global_bias = snap.item_global_bias
        self._hero_rarity_dists = snap.hero_rarity_dists
        self._role_item_priors = snap.role_item_priors
        self._tear_merged = snap.tear_merged
        self._active_mode = snap.mode

    def load_from_dir(
        self,
        descriptor: DatasetDescriptor,
        dataset_dir: Path,
        band: str = "",
    ) -> None:
        base = Path(dataset_dir)
        mode_name = (descriptor.mode or base.name or "normal").strip() or "normal"
        snap = self._build_snapshot(descriptor, base, band, mode_name, refresh_static=True)

        with self._lock:
            self._mode_snapshots.clear()
            self._eligible_pool_cache.clear()
            self._mode_snapshots[mode_name] = snap
            self._apply_snapshot_locked(snap)

        # Auto-preload sibling mode ("normal" <-> "first-prismatic") if available on disk
        sibling_mode = "first-prismatic" if mode_name == "normal" else ("normal" if mode_name == "first-prismatic" else "")
        if sibling_mode:
            sibling_dir = base.parent / sibling_mode
            if (sibling_dir / "heroes.json").is_file():
                self.preload_alternate_mode(sibling_dir, sibling_mode, band=band)

    def preload_alternate_mode(
        self,
        mode_dir: Path,
        mode_name: str,
        band: str = "",
    ) -> bool:
        """Load an alternate match-mode dataset (e.g. 'first-prismatic') into memory for <1ms switching."""
        target = Path(mode_dir)
        clean_mode = str(mode_name).strip()
        if not clean_mode or not (target / "heroes.json").is_file():
            return False

        with self._lock:
            base_desc = self.descriptor
        patch = base_desc.patch if base_desc is not None else target.parent.name
        alt_desc = DatasetDescriptor(
            patch=patch,
            mode=clean_mode,
            mode_label="首次必棱彩" if clean_mode == "first-prismatic" else "常规",
            path=f"patches/{patch}/{clean_mode}",
            snapshot_at=base_desc.snapshot_at if base_desc is not None else "",
            bands=bool((target / "bands").is_dir()),
            seq=base_desc.seq if base_desc is not None else 0,
            valid_matches=base_desc.valid_matches if base_desc is not None else 0,
        )
        alt_snap = self._build_snapshot(alt_desc, target, band, clean_mode, refresh_static=False)
        with self._lock:
            self._mode_snapshots[clean_mode] = alt_snap
            self._eligible_pool_cache.clear()
        return True

    def switch_match_mode(self, mode: str) -> bool:
        """Atomically swap active in-memory tables to a preloaded match mode in <1ms."""
        clean_mode = str(mode).strip()
        with self._lock:
            snap = self._mode_snapshots.get(clean_mode)
            if snap is None:
                return False
            self._apply_snapshot_locked(snap)
            return True

    def get_item_global_bias(self, item_id: str | int) -> float:
        cid = _canonical_item_key(item_id)
        with self._lock:
            return self._item_global_bias.get(cid, 0.0)

    def get_hero_rarity_distribution(self, hero_id: str | int, rarity: str) -> RarityDeltaDist | None:
        key = (str(hero_id), str(rarity).strip().lower())
        with self._lock:
            return self._hero_rarity_dists.get(key)

    def get_role_item_prior(self, profile: str, item_id: str | int) -> float | None:
        key = (str(profile).strip().upper(), _canonical_item_key(item_id))
        with self._lock:
            return self._role_item_priors.get(key)

    def has_high_band(self, mode: str | None = None) -> bool:
        """Return True if native bands/high data is loaded for the active (or specified) mode."""
        with self._lock:
            if mode is None:
                return self._has_high_band
            snap = self._mode_snapshots.get(str(mode).strip())
            return bool(snap.has_high_band) if snap is not None else False

    def _resolve_high_weight_locked(
        self,
        *,
        high_weight: float | None = None,
        band_mode: str | None = None,
    ) -> float:
        if not self._has_high_band:
            return 0.0
        if high_weight is not None:
            return max(0.0, min(1.0, float(high_weight)))
        if band_mode is not None:
            return DATA_BAND_HIGH_WEIGHTS.get(str(band_mode).strip().lower(), 0.0)
        return 0.0

    def _get_hero_pool_locked(self, hid: str) -> tuple[bool, frozenset[str]]:
        if self._mode_snapshots:
            cached = self._eligible_pool_cache.get(hid)
            if cached is not None:
                return cached
            sources = [(s.heroes, s.hero_augments) for s in self._mode_snapshots.values()]
        else:
            sources = [(self._heroes, self._hero_augments)]

        has_pool_data = False
        eligible: set[str] = set()
        for hmap, hamap in sources:
            hero = hmap.get(hid)
            aug_map = hamap.get(hid)
            if hero is None or not aug_map:
                continue
            has_pool_data = True
            hero_g = hero.games
            if hero_g < AUGMENT_POOL_REAL_HERO_MIN_GAMES:
                for aid, st in aug_map.items():
                    if st.games > 0:
                        eligible.add(aid)
            else:
                denom = max(1, hero_g)
                for aid, st in aug_map.items():
                    if st.games >= AUGMENT_POOL_MIN_GAMES and (st.games / denom) >= AUGMENT_POOL_MIN_PR:
                        eligible.add(aid)

        result = (has_pool_data, frozenset(eligible))
        if self._mode_snapshots:
            self._eligible_pool_cache[hid] = result
        return result

    def is_augment_eligible_for_hero(
        self,
        hero_id: str | int | None,
        augment_id: str | int,
    ) -> bool:
        """Authoritative pool check across all loaded modes (normal + first-prismatic).

        A hero-augment pair is eligible iff any loaded mode satisfies
        `games >= 20` AND `games / max(1, hero.games) >= 0.00008` (on real datasets where
        `hero.games >= 500`). Fails open safely when no augment stats exist for the hero
        (empty store or minimal synthetic test fixtures).
        """
        if hero_id is None:
            return True
        hid = str(hero_id).strip()
        aid = str(augment_id).strip()
        if not aid:
            return False
        if not hid:
            return True
        with self._lock:
            has_pool_data, eligible_ids = self._get_hero_pool_locked(hid)
            if not has_pool_data:
                return True
            return aid in eligible_ids

    def get_hero_eligible_augment_ids(self, hero_id: str | int | None) -> frozenset[str]:
        """Return cached frozenset of authoritative eligible augment IDs for a hero."""
        if hero_id is None:
            return frozenset()
        hid = str(hero_id).strip()
        if not hid:
            return frozenset()
        with self._lock:
            _, eligible_ids = self._get_hero_pool_locked(hid)
            return eligible_ids

    def get_top_eligible_augments(
        self,
        hero_id: str | int | None,
        limit: int = 8,
        rarity: str | None = None,
    ) -> list[tuple[str, float, float]]:
        """Return top eligible augments as (augment_id, smoothed_delta, rarity_percentile)."""
        if hero_id is None or limit <= 0:
            return []
        hid = str(hero_id).strip()
        if not hid:
            return []
        target_rar = str(rarity).strip().lower() if rarity else ""

        with self._lock:
            aug_map = self._hero_augments.get(hid) or {}
            if not aug_map:
                return []
            has_pool_data, eligible_ids = self._get_hero_pool_locked(hid)
            hero = self._heroes.get(hid)
            prior = hero.win_rate if (hero and 0.05 <= hero.win_rate <= 0.95) else 0.50

            ranked: list[tuple[float, float, float, int, str]] = []
            for aid, st in aug_map.items():
                if has_pool_data and aid not in eligible_ids:
                    continue
                if st.games <= 0:
                    continue
                aug = self._augments.get(aid)
                rar = (aug.rarity if aug is not None else "gold").strip().lower() or "gold"
                if target_rar and rar != target_rar:
                    continue
                sm_wr = (st.wins + DEFAULT_PRIOR_K * prior) / (st.games + DEFAULT_PRIOR_K)
                delta = sm_wr - prior
                rdist = self._hero_rarity_dists.get((hid, rar))
                pct = _compute_percentile(rdist.sorted_deltas, delta) if (rdist and rdist.sorted_deltas) else 50.0
                # Match tier.py compute_hex_score weighting (0.62 percentile + 0.38 delta)
                composite = 0.62 * (18.0 + pct * 0.78) + 0.38 * (58.0 + delta * 620.0)
                if st.games < 300:
                    composite -= 1.5
                ranked.append((composite, delta, pct, st.games, aid))

            ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
            return [(aid, round(delta, 6), round(pct, 2)) for _, delta, pct, _, aid in ranked[: int(limit)]]

    def get_hero(
        self,
        hero_id: str | int,
        *,
        high_weight: float | None = None,
        band_mode: str | None = None,
    ) -> HeroStat | None:
        hid_s = str(hero_id)
        with self._lock:
            base_hero = self._heroes.get(hid_s)
            if base_hero is None:
                return None
            base_w = self._resolve_high_weight_locked(high_weight=high_weight, band_mode=band_mode)
            if base_w <= 0.0:
                return base_hero
            hi_hero = self._high_heroes.get(hid_s)
            if hi_hero is None or hi_hero.games <= 0:
                return base_hero
            w_eff = base_w * min(1.0, hi_hero.games / HIGH_BAND_FULL_WEIGHT_GAMES)
            if w_eff >= 0.9999:
                return hi_hero
            blended_wr = (1.0 - w_eff) * base_hero.win_rate + w_eff * hi_hero.win_rate
            blended_g = max(1, int(round((1.0 - w_eff) * base_hero.games + w_eff * hi_hero.games)))
            blended_w = max(0, min(blended_g, int(round(blended_wr * blended_g))))
            return replace(
                base_hero,
                games=blended_g,
                wins=blended_w,
                win_rate=blended_wr,
                low_sample=bool(base_hero.low_sample and hi_hero.games < 300),
            )

    def all_heroes(self) -> list[HeroStat]:
        with self._lock:
            return sorted(self._heroes.values(), key=lambda h: h.win_rate, reverse=True)

    def find_hero_by_name(self, name: str) -> HeroStat | None:
        if not name:
            return None
        cleaned = str(name)
        for prefix in ("game_character_displayname_", "Characters/", "character_"):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix) :]
        norm = _norm_name(cleaned)
        with self._lock:
            hid = self._hero_name_index.get(norm)
            if hid:
                return self._heroes.get(hid)
            # Substring fallback for names with punctuation / titles
            for key, candidate_id in self._hero_name_index.items():
                if len(norm) >= 2 and (norm in key or key in norm):
                    return self._heroes.get(candidate_id)
        return None

    def get_augment(self, aug_id: str | int) -> AugmentInfo | None:
        with self._lock:
            return self._augments.get(str(aug_id))

    def all_augments(self) -> list[AugmentInfo]:
        with self._lock:
            return list(self._augments.values())

    def display_hero_name(
        self,
        hero_id: str | int,
        fallback: str = "",
        *,
        locale: str | None = None,
    ) -> str:
        """Return localized hero name for UI presentation (never alters internal canonical HeroStat.name)."""
        hid_s = str(hero_id or "").strip()
        loc = normalize_client_locale(locale) if locale is not None else self.client_locale
        fb = fallback
        if not fb and hid_s:
            with self._lock:
                hs = self._heroes.get(hid_s)
                if hs is not None:
                    fb = hs.name
        if not hid_s:
            return fb
        return localize_hero_name(hid_s, fb, loc)

    def display_augment_name(
        self,
        aug_id: str | int,
        fallback: str = "",
        *,
        locale: str | None = None,
    ) -> str:
        """Return localized augment name for UI presentation (never alters internal canonical AugmentInfo.name)."""
        aid_s = str(aug_id or "").strip()
        loc = normalize_client_locale(locale) if locale is not None else self.client_locale
        fb = fallback
        if not fb and aid_s:
            with self._lock:
                aug = self._augments.get(aid_s)
                if aug is not None:
                    fb = aug.name
        if not aid_s:
            return fb
        return localize_augment_name(aid_s, fb, loc)

    def display_item_name(
        self,
        item_id: str | int,
        fallback: str = "",
        *,
        locale: str | None = None,
    ) -> str:
        """Return localized item name for UI presentation (handles Mayhem 6-digit aliases transparently)."""
        iid_s = str(item_id or "").strip()
        loc = normalize_client_locale(locale) if locale is not None else self.client_locale
        info = self.get_item_info(iid_s) if iid_s else None
        fb = fallback or (info.name if info is not None else "")
        if not iid_s or loc == "zh_cn":
            return fb
        localized = localize_item_name(iid_s, "", loc)
        if localized:
            return localized
        if info is not None and info.item_id and info.item_id != iid_s:
            localized = localize_item_name(info.item_id, "", loc)
            if localized:
                return localized
        alias_target = SIX_DIGIT_ITEM_ALIASES.get(iid_s)
        if alias_target:
            localized = localize_item_name(alias_target, "", loc)
            if localized:
                return localized
        return fb

    def build_ocr_augments(self, locale: str | None = None) -> list[AugmentInfo]:
        """Return AugmentInfo list tailored to the active LoL client locale for OCR matching."""
        loc = normalize_client_locale(locale) if locale is not None else self.client_locale
        return build_ocr_catalog(self.all_augments(), loc)

    def get_hero_augment_stat(
        self,
        hero_id: str | int,
        aug_id: str | int,
        *,
        high_weight: float | None = None,
        band_mode: str | None = None,
    ) -> HeroAugmentStat | None:
        hid_s = str(hero_id)
        aid_s = str(aug_id)
        with self._lock:
            base_st = (self._hero_augments.get(hid_s) or {}).get(aid_s)
            base_w = self._resolve_high_weight_locked(high_weight=high_weight, band_mode=band_mode)
            if base_w <= 0.0:
                return base_st
            hi_st = (self._high_hero_augments.get(hid_s) or {}).get(aid_s)
            if hi_st is None or hi_st.games <= 0:
                return base_st
            if base_st is None or base_st.games <= 0:
                return hi_st
            w_eff = base_w * min(1.0, hi_st.games / HIGH_BAND_FULL_WEIGHT_GAMES)
            if w_eff >= 0.9999:
                return hi_st
            blended_wr = (1.0 - w_eff) * base_st.win_rate + w_eff * hi_st.win_rate
            blended_g = max(1, int(round((1.0 - w_eff) * base_st.games + w_eff * hi_st.games)))
            blended_w = max(0, min(blended_g, int(round(blended_wr * blended_g))))
            return HeroAugmentStat(
                augment_id=aid_s,
                games=blended_g,
                wins=blended_w,
                win_rate=blended_wr,
                low_sample=bool(base_st.low_sample and hi_st.games < 300),
            )

    def get_hero_augments(self, hero_id: str | int) -> dict[str, HeroAugmentStat]:
        with self._lock:
            return dict(self._hero_augments.get(str(hero_id)) or {})

    def get_hero_spells(self, hero_id: str | int) -> list[SpellComboStat]:
        with self._lock:
            return list(self._hero_spells.get(str(hero_id)) or [])

    def get_core_builds(
        self,
        hero_id: str | int,
        *,
        high_weight: float | None = None,
        band_mode: str | None = None,
    ) -> list[CoreBuild]:
        hid_s = str(hero_id)
        with self._lock:
            all_builds = list(self._core_builds.get(hid_s) or [])
            base_w = self._resolve_high_weight_locked(high_weight=high_weight, band_mode=band_mode)
            if base_w <= 0.0:
                return all_builds
            hi_builds = list(self._high_core_builds.get(hid_s) or [])
            if not hi_builds:
                return all_builds
            if not all_builds:
                return hi_builds

            all_by_key = {cb.items: cb for cb in all_builds}
            hi_by_key = {cb.items: cb for cb in hi_builds}
            all_tot_g = max(1, sum(max(1, cb.games) for cb in all_builds))
            hi_tot_g = max(1, sum(max(1, cb.games) for cb in hi_builds))
            target_tot = (1.0 - base_w) * all_tot_g + base_w * hi_tot_g
            top_hi_w = base_w * min(1.0, max((cb.games for cb in hi_builds), default=0) / HIGH_BAND_FULL_WEIGHT_GAMES)

            # Fast exact path when full high-band mode has adequate sample across all high builds
            if base_w >= 0.9999 and all(cb.games >= HIGH_BAND_FULL_WEIGHT_GAMES for cb in hi_builds):
                return hi_builds

            merged: list[CoreBuild] = []
            seen_keys: list[tuple[str, ...]] = list(dict.fromkeys([*(cb.items for cb in hi_builds), *(cb.items for cb in all_builds)]))
            for items_key in seen_keys:
                a_cb = all_by_key.get(items_key)
                h_cb = hi_by_key.get(items_key)
                if a_cb is not None and h_cb is not None:
                    w_eff = base_w * min(1.0, h_cb.games / HIGH_BAND_FULL_WEIGHT_GAMES)
                    if w_eff >= 0.9999:
                        merged.append(h_cb)
                        continue
                    wr = (1.0 - w_eff) * a_cb.win_rate + w_eff * h_cb.win_rate
                    pr = (1.0 - w_eff) * a_cb.pick_rate + w_eff * h_cb.pick_rate
                    share = (1.0 - w_eff) * (a_cb.games / all_tot_g) + w_eff * (h_cb.games / hi_tot_g)
                    g = max(1, int(round(share * target_tot)))
                    w = max(0, min(g, int(round(wr * g))))
                    merged.append(
                        CoreBuild(
                            items=items_key,
                            ordered=h_cb.ordered,
                            games=g,
                            wins=w,
                            win_rate=wr,
                            pick_rate=pr,
                        )
                    )
                elif h_cb is not None:
                    w_eff = base_w * min(1.0, h_cb.games / HIGH_BAND_FULL_WEIGHT_GAMES)
                    if w_eff >= 0.9999:
                        merged.append(h_cb)
                        continue
                    pr = w_eff * h_cb.pick_rate
                    g = max(1, int(round(w_eff * (h_cb.games / hi_tot_g) * target_tot)))
                    w = max(0, min(g, int(round(h_cb.win_rate * g))))
                    merged.append(
                        CoreBuild(
                            items=items_key,
                            ordered=h_cb.ordered,
                            games=g,
                            wins=w,
                            win_rate=h_cb.win_rate,
                            pick_rate=pr,
                        )
                    )
                elif a_cb is not None:
                    a_weight = max(0.0, 1.0 - top_hi_w)
                    if a_weight <= 1e-6:
                        continue
                    pr = a_weight * a_cb.pick_rate
                    g = max(1, int(round(a_weight * (a_cb.games / all_tot_g) * target_tot)))
                    w = max(0, min(g, int(round(a_cb.win_rate * g))))
                    merged.append(
                        CoreBuild(
                            items=items_key,
                            ordered=a_cb.ordered,
                            games=g,
                            wins=w,
                            win_rate=a_cb.win_rate,
                            pick_rate=pr,
                        )
                    )
            merged.sort(key=lambda cb: (cb.pick_rate, cb.games, cb.win_rate), reverse=True)
            return merged

    def get_augment_item_data(
        self,
        hero_id: str | int,
        augment_id: str | int,
        *,
        high_weight: float | None = None,
        band_mode: str | None = None,
    ) -> DestinyData | None:
        hid_s = str(hero_id)
        aid_s = str(augment_id)
        with self._lock:
            base_w = self._resolve_high_weight_locked(high_weight=high_weight, band_mode=band_mode)
            if base_w >= 0.5:
                hi_hit = self._high_augment_items.get((hid_s, aid_s))
                if hi_hit is None and aid_s == "3081":
                    hi_hit = self._high_destiny.get(hid_s)
                if hi_hit is not None and sum(it.games for it in hi_hit.items) >= HIGH_BAND_FULL_WEIGHT_GAMES:
                    return hi_hit
            hit = self._augment_items.get((hid_s, aid_s))
            if hit is not None:
                return hit
            if aid_s == "3081":
                return self._destiny.get(hid_s)
            return None

    def get_destiny_data(
        self,
        hero_id: str | int,
        *,
        high_weight: float | None = None,
        band_mode: str | None = None,
    ) -> DestinyData | None:
        return self.get_augment_item_data(hero_id, "3081", high_weight=high_weight, band_mode=band_mode)

    def get_hero_item_recs(
        self,
        hero_id: str | int,
        *,
        high_weight: float | None = None,
        band_mode: str | None = None,
    ) -> dict[str, ItemRecStat]:
        hid_s = str(hero_id)
        with self._lock:
            all_recs = self._item_recs.get(hid_s) or {}
            base_w = self._resolve_high_weight_locked(high_weight=high_weight, band_mode=band_mode)
            if base_w <= 0.0:
                return dict(all_recs)
            hi_recs = self._high_item_recs.get(hid_s) or {}
            if not hi_recs:
                return dict(all_recs)
            if not all_recs:
                return dict(hi_recs)

            all_hero = self._heroes.get(hid_s)
            hi_hero = self._high_heroes.get(hid_s)
            all_hero_g = all_hero.games if (all_hero and all_hero.games > 0) else sum(r.games for r in all_recs.values())
            hi_hero_g = hi_hero.games if (hi_hero and hi_hero.games > 0) else sum(r.games for r in hi_recs.values())
            use_pr_scale = all_hero_g > 0 and hi_hero_g > 0
            target_hero_g = (1.0 - base_w) * all_hero_g + base_w * hi_hero_g if use_pr_scale else 0.0

            blended: dict[str, ItemRecStat] = {}
            for iid in set(all_recs.keys()) | set(hi_recs.keys()):
                a_rec = all_recs.get(iid)
                h_rec = hi_recs.get(iid)
                if a_rec is not None and h_rec is not None:
                    w_eff = base_w * min(1.0, max(0, h_rec.games) / HIGH_BAND_FULL_WEIGHT_GAMES)
                    if w_eff <= 0.0:
                        if use_pr_scale:
                            g = max(1, int(round((a_rec.games / all_hero_g) * target_hero_g)))
                            w = max(0, min(g, int(round(a_rec.win_rate * g))))
                            blended[iid] = ItemRecStat(iid, g, w, a_rec.win_rate, a_rec.low_sample)
                        else:
                            blended[iid] = a_rec
                        continue
                    if w_eff >= 0.9999:
                        blended[iid] = h_rec
                        continue
                    wr = (1.0 - w_eff) * a_rec.win_rate + w_eff * h_rec.win_rate
                    if use_pr_scale:
                        pr = (1.0 - w_eff) * (a_rec.games / all_hero_g) + w_eff * (h_rec.games / hi_hero_g)
                        g = max(1, int(round(pr * target_hero_g)))
                    else:
                        g = max(1, int(round((1.0 - w_eff) * a_rec.games + w_eff * h_rec.games)))
                    w = max(0, min(g, int(round(wr * g))))
                    low = bool(a_rec.low_sample and h_rec.games < 300)
                    blended[iid] = ItemRecStat(
                        item_id=iid,
                        games=g,
                        wins=w,
                        win_rate=wr,
                        low_sample=low,
                    )
                elif a_rec is not None:
                    if use_pr_scale:
                        g = max(1, int(round((a_rec.games / all_hero_g) * target_hero_g)))
                        w = max(0, min(g, int(round(a_rec.win_rate * g))))
                        blended[iid] = ItemRecStat(iid, g, w, a_rec.win_rate, a_rec.low_sample)
                    else:
                        blended[iid] = a_rec
                elif h_rec is not None:
                    blended[iid] = h_rec
            return blended

    def get_item_info(self, item_id: str | int) -> ItemStaticInfo:
        iid = str(item_id).strip()
        alias_target = SIX_DIGIT_ITEM_ALIASES.get(iid)
        lookup_id = alias_target or iid

        if alias_target:
            if alias_target in self.static.items:
                info = self.static.items[alias_target]
            elif iid in self.static.items:
                info = replace(self.static.items[iid], item_id=alias_target)
            else:
                info = self.static.get_item_info(alias_target)
        elif iid in self.static.items:
            info = self.static.items[iid]
        elif len(iid) == 6 and iid[:2] not in _MAYHEM_ITEM_PREFIXES:
            # Keep unaliased 6-digit Mayhem items (e.g. 663172 Zephyr) distinct from unrelated 4-digit IDs (3172)
            info = ItemStaticInfo(
                item_id=iid,
                name=f"装备#{iid}",
                icon_url=f"{TENCENT_IMG_BASE}/item/{iid}.png",
            )
        else:
            info = self.static.get_item_info(iid)

        # Fallback to 6-digit Mayhem variant (e.g. 124011 / 664011 -> 4011 花晓之剑) when base 4-digit ID is absent
        if info.name.startswith("装备#") and len(lookup_id) == 4 and lookup_id.isdigit():
            for pfx in _MAYHEM_ITEM_PREFIXES:
                alt_id = f"{pfx}{lookup_id}"
                if alt_id in self.static.items:
                    info = replace(self.static.items[alt_id], item_id=lookup_id)
                    break
            if info.name.startswith("装备#"):
                six_id = _CANON_TO_SIX_DIGIT_ALIASES.get(lookup_id)
                if six_id and six_id in self.static.items:
                    info = replace(self.static.items[six_id], item_id=lookup_id)

        # Populate gold from dataset item_metadata if DDragon entry lacked total gold
        if info.gold <= 0 and not info.name.startswith("装备#"):
            with self._lock:
                meta = (
                    self._item_meta_raw.get(lookup_id)
                    or self._item_meta_raw.get(iid)
                    or self._item_meta_raw.get(info.item_id)
                )
                if meta is None and len(lookup_id) == 4 and lookup_id.isdigit():
                    for pfx in _MAYHEM_ITEM_PREFIXES:
                        meta = self._item_meta_raw.get(f"{pfx}{lookup_id}")
                        if meta is not None:
                            break
                    if meta is None:
                        six_id = _CANON_TO_SIX_DIGIT_ALIASES.get(lookup_id)
                        if six_id:
                            meta = self._item_meta_raw.get(six_id)
            if isinstance(meta, dict):
                meta_gold = int(meta.get("total") or meta.get("gold") or 0)
                if meta_gold > 0:
                    info = replace(info, gold=meta_gold)
        # Fallback complete_equipment classification for >=2200G non-boot/trinket items missing item_metadata
        if info.category != "complete_equipment" and is_complete_equipment_info(info):
            info = replace(info, category="complete_equipment")
        return info

    def is_item_in_dataset(self, item_id: str | int) -> bool:
        """Return True iff item_id (or its ARAM-Mayhem 12/22/32/66 variant) exists in loaded dataset stats."""
        raw = str(item_id).strip()
        if not raw:
            return False
        canon = _canonical_item_key(raw)
        with self._lock:
            if not self._item_meta_raw and not self._item_global_bias:
                return False
            cands = [raw]
            if canon and canon != raw:
                cands.append(canon)
            if len(canon) == 4 and canon.isdigit():
                for pfx in ("12", "22", "32", "66"):
                    cands.append(f"{pfx}{canon}")
                six_id = _CANON_TO_SIX_DIGIT_ALIASES.get(canon)
                if six_id:
                    cands.append(six_id)
            for cid in cands:
                if cid in self._item_meta_raw or cid in self._item_global_bias:
                    return True
            return False

    def get_dataset_item_meta(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._item_meta_raw)


def _norm_name(text: str) -> str:
    return "".join(ch.lower() for ch in str(text) if ch.isalnum())
