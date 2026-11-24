"""Thread-safe in-memory read-only snapshot store for HexHelper datasets."""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
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
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.scoring.tier import DEFAULT_PRIOR_K, RarityDeltaDist

# Base <-> stacked Tear item pairs to merge so unstacked games never suffer artificial WR penalties.
TEAR_ITEM_PAIRS: tuple[tuple[str, str], ...] = (
    ("3003", "3040"),  # 大天使之杖 <-> 炽天使之拥
    ("3004", "3042"),  # 魔宗 <-> 魔切
    ("3119", "3121"),  # 凛冬之临 <-> 末日寒冬
    ("2526", "2530"),  # 耳语头环 <-> 歌之权冠
)
TEAR_STACKED_TO_BASE: dict[str, str] = {stacked: base for base, stacked in TEAR_ITEM_PAIRS}
TEAR_STACKED_IDS: frozenset[str] = frozenset(TEAR_STACKED_TO_BASE.keys())
_MAYHEM_ITEM_PREFIXES: tuple[str, ...] = ("12", "22", "32", "44", "77", "99")


def _canonical_item_key(item_id: str | int) -> str:
    s = str(item_id).strip()
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

        sample_items: list[str] = []
        for cb in core_builds[:2]:
            sample_items.extend(cb.items)
        if not sample_items and item_recs:
            top_recs = sorted(item_recs.values(), key=lambda r: r.games, reverse=True)[:5]
            sample_items = [r.item_id for r in top_recs]

        for iid in sample_items:
            info = self.static.get_item_info(iid)
            st = info.stats or {}
            ad_s += st.get("FlatPhysicalDamageMod", 0.0) * 1.1
            ad_s += st.get("FlatCritChanceMod", 0.0) * 100.0
            ap_s += st.get("FlatMagicDamageMod", 0.0) * 0.85
            tank_s += (
                st.get("FlatArmorMod", 0.0) * 1.1
                + st.get("FlatSpellBlockMod", 0.0) * 1.1
                + st.get("FlatHPPoolMod", 0.0) * 0.10
            )

        if tank_s >= max(ad_s, ap_s) * 1.15 and tank_s > 60:
            return "TANK"
        if ap_s > ad_s * 1.15:
            return "AP"
        return "AD"

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
        item_meta_raw = _read_json_safe(base / "item_metadata.json")

        # Refresh static item merge with dataset item_metadata.json on primary load
        if refresh_static:
            self.static.load_cached(dataset_item_meta=item_meta_raw)

        heroes: dict[str, HeroStat] = {}
        name_index: dict[str, str] = {}
        for hid, h in heroes_raw.items():
            hid_s = str(hid)
            hs = HeroStat(
                hero_id=hid_s,
                name=str(h.get("name", hid_s)),
                title=str(h.get("title", "")),
                games=int(h.get("games", 0)),
                wins=int(h.get("wins", 0)),
                win_rate=float(h.get("win_rate", 0.5)),
                pick_rate=float(h.get("pick_rate", 0.0)),
                rank=int(h.get("rank", 999)),
                low_sample=bool(h.get("low_sample", False)),
            )
            heroes[hid_s] = hs
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

        augments: dict[str, AugmentInfo] = {}
        for aid, a in augs_raw.items():
            aid_s = str(aid)
            augments[aid_s] = AugmentInfo(
                augment_id=aid_s,
                name=str(a.get("name", aid_s)),
                rarity=str(a.get("rarity", "gold")),
                icon_url=self.static.augment_icon_urls.get(aid_s, ""),
            )

        hero_augments: dict[str, dict[str, HeroAugmentStat]] = {}
        for hid, aug_map in ha_raw.items():
            hid_s = str(hid)
            inner: dict[str, HeroAugmentStat] = {}
            for aid, st in aug_map.items():
                aid_s = str(aid)
                inner[aid_s] = HeroAugmentStat(
                    augment_id=aid_s,
                    games=int(st.get("games", 0)),
                    wins=int(st.get("wins", 0)),
                    win_rate=float(st.get("win_rate", 0.5)),
                    low_sample=bool(st.get("low_sample", False)),
                )
            hero_augments[hid_s] = inner

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

        core_builds: dict[str, list[CoreBuild]] = {}
        for hid, blist in core_raw.items():
            parsed_b: list[CoreBuild] = []
            for b in blist:
                parsed_b.append(
                    CoreBuild(
                        items=tuple(str(x) for x in b.get("items", [])),
                        ordered=bool(b.get("ordered", True)),
                        games=int(b.get("games", 0)),
                        wins=int(b.get("wins", 0)),
                        win_rate=float(b.get("win", b.get("win_rate", 0.5))),
                        pick_rate=float(b.get("pick", 0.0)),
                    )
                )
            core_builds[str(hid)] = parsed_b

        destiny: dict[str, DestinyData] = {}
        augment_items: dict[tuple[str, str], DestinyData] = {}
        for hid, aug_dict in destiny_raw.items():
            if not isinstance(aug_dict, dict):
                continue
            hid_s = str(hid)
            for aid, entry_raw in aug_dict.items():
                if not isinstance(entry_raw, dict):
                    continue
                aid_s = str(aid)
                bl = entry_raw.get("baseline")
                bl_dict = bl if isinstance(bl, dict) else {}
                items_list: list[DestinyItemEntry] = []
                for it in entry_raw.get("items") or []:
                    if not isinstance(it, dict):
                        continue
                    item_id_s = str(it.get("item", ""))
                    items_list.append(
                        DestinyItemEntry(
                            item_id=item_id_s,
                            name=str(it.get("name") or self.static.get_item_name(item_id_s)),
                            games=int(it.get("games", 0)),
                            wins=int(it.get("wins", 0)),
                            low_sample=bool(it.get("low_sample", False)),
                        )
                    )
                parsed_entry = DestinyData(
                    baseline_games=int(bl_dict.get("games", 0)),
                    baseline_wins=int(bl_dict.get("wins", 0)),
                    items=tuple(items_list),
                )
                augment_items[(hid_s, aid_s)] = parsed_entry
                if aid_s == "3081":
                    destiny[hid_s] = parsed_entry

        # Parse item recommendations & merge Tear item pairs (base <-> stacked)
        item_recs: dict[str, dict[str, ItemRecStat]] = {}
        for hid, imap in item_recs_raw.items():
            inner_i: dict[str, ItemRecStat] = {}
            for iid, st in imap.items():
                iid_s = str(iid)
                inner_i[iid_s] = ItemRecStat(
                    item_id=iid_s,
                    games=int(st.get("games", 0)),
                    wins=int(st.get("wins", 0)),
                    win_rate=float(st.get("win_rate", 0.5)),
                    low_sample=bool(st.get("low_sample", False)),
                )
            for base_id, stacked_id in TEAR_ITEM_PAIRS:
                b_rec = inner_i.get(base_id)
                s_rec = inner_i.get(stacked_id)
                if b_rec is None and s_rec is None:
                    continue
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
            item_recs[str(hid)] = inner_i

        # Precompute canonical per-hero item stats for global bias & hierarchical role priors
        global_deltas: dict[str, list[float]] = {}
        role_deltas: dict[tuple[str, str], list[float]] = {}

        for hid, imap in item_recs.items():
            hero = heroes.get(hid)
            if hero is None:
                continue
            h_wr = hero.win_rate if 0.05 <= hero.win_rate <= 0.95 else 0.50
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
                if g >= 1000:
                    global_deltas.setdefault(cid, []).append(delta)
                if g >= 300:
                    role_deltas.setdefault((profile, cid), []).append(delta)

        item_global_bias: dict[str, float] = {}
        for cid, ds in global_deltas.items():
            if len(ds) >= 2:
                mean_d = sum(ds) / len(ds)
                item_global_bias[cid] = round(max(-0.08, min(0.06, mean_d)), 6)

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

    def get_hero(self, hero_id: str | int) -> HeroStat | None:
        with self._lock:
            return self._heroes.get(str(hero_id))

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

    def get_hero_augment_stat(self, hero_id: str | int, aug_id: str | int) -> HeroAugmentStat | None:
        with self._lock:
            return (self._hero_augments.get(str(hero_id)) or {}).get(str(aug_id))

    def get_hero_augments(self, hero_id: str | int) -> dict[str, HeroAugmentStat]:
        with self._lock:
            return dict(self._hero_augments.get(str(hero_id)) or {})

    def get_hero_spells(self, hero_id: str | int) -> list[SpellComboStat]:
        with self._lock:
            return list(self._hero_spells.get(str(hero_id)) or [])

    def get_core_builds(self, hero_id: str | int) -> list[CoreBuild]:
        with self._lock:
            return list(self._core_builds.get(str(hero_id)) or [])

    def get_augment_item_data(self, hero_id: str | int, augment_id: str | int) -> DestinyData | None:
        hid_s = str(hero_id)
        aid_s = str(augment_id)
        with self._lock:
            hit = self._augment_items.get((hid_s, aid_s))
            if hit is not None:
                return hit
            if aid_s == "3081":
                return self._destiny.get(hid_s)
            return None

    def get_destiny_data(self, hero_id: str | int) -> DestinyData | None:
        return self.get_augment_item_data(hero_id, "3081")

    def get_hero_item_recs(self, hero_id: str | int) -> dict[str, ItemRecStat]:
        with self._lock:
            return dict(self._item_recs.get(str(hero_id)) or {})

    def get_item_info(self, item_id: str | int) -> ItemStaticInfo:
        return self.static.get_item_info(str(item_id))

    def get_dataset_item_meta(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._item_meta_raw)


def _norm_name(text: str) -> str:
    return "".join(ch.lower() for ch in str(text) if ch.isalnum())
