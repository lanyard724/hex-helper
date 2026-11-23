"""Real-time 10-player team comp & enemy build counter-optimization engine (<5ms local)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from hex_helper.data.hero_signatures import get_hero_signature_db
from hex_helper.data.provider import ItemRecStat, ItemStaticInfo
from hex_helper.data.store import DataStore
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.build_synergy import (
    detect_profile_override,
    evaluate_augment_synergies,
    get_special_augment_candidate_items,
    item_is_owned,
    normalize_item_id,
    resolve_special_builds,
)
from hex_helper.scoring.destiny import DESTINY_AUGMENT_ID, rank_destiny_items

# High-sustain / healing champions (matched by CN name or English alias fragment)
HIGH_HEAL_HEROES = {
    "亚托克斯", "Aatrox", "蒙多医生", "DrMundo", "索拉卡", "Soraka",
    "弗拉基米尔", "Vladimir", "沃里克", "Warwick", "斯维因", "Swain",
    "米利欧", "Milio", "布莱尔", "Briar", "莎弥拉", "Samira", "萨弥拉",
    "悠米", "Yuumi", "娜美", "Nami", "娑娜", "Sona", "塞拉斯", "Sylas",
    "凯隐", "Kayn", "扎克", "Zac", "茂凯", "Maokai", "德莱厄斯", "Darius",
    "俄洛伊", "Illaoi", "费德提克", "Fiddlesticks", "塔里克", "Taric",
    "赫卡里姆", "Hecarim", "沃利贝尔", "Volibear", "妮菈", "Nilah",
}

HIGH_SHIELD_HEROES = {
    "卡尔玛", "Karma", "璐璐", "Lulu", "迦娜", "Janna", "塔姆", "TahmKench",
    "艾翁", "Ivern", "萨勒芬妮", "Seraphine", "慎", "Shen", "拉克丝", "Lux",
    "莫甘娜", "Morgana", "瑟提", "Sett", "莫德凯撒", "Mordekaiser",
    "奥莉安娜", "Orianna", "芮尔", "Rell", "诺提勒斯", "Nautilus",
    "乌迪尔", "Udyr", "锐雯", "Riven", "米利欧", "Milio", "李青", "LeeSin",
}

HIGH_CC_HEROES = {
    "玛尔扎哈", "Malzahar", "莫甘娜", "Morgana", "蕾欧娜", "Leona",
    "诺提勒斯", "Nautilus", "丽桑卓", "Lissandra", "安妮", "Annie",
    "阿木木", "Amumu", "维迦", "Veigar", "拉克丝", "Lux", "锤石", "Thresh",
    "布里茨", "Blitzcrank", "斯卡纳", "Skarner", "瑟庄妮", "Sejuani",
    "艾希", "Ashe", "韦鲁斯", "Varus", "费德提克", "Fiddlesticks",
    "妮蔻", "Neeko", "枷萝", "Zyra", "茂凯", "Maokai", "阿利斯塔", "Alistar",
}

HEAL_ITEM_IDS = {
    "3153", "3072", "3074", "6673", "3065", "6617", "6610", "3111", "4633", "3147",
    "2517", "2510", "2525", "2526", "2530", "4011",
}
SHIELD_ITEM_IDS = {
    "3156", "3190", "3040", "3119", "3121", "3053", "6673", "3107", "3222",
    "2526", "2530", "2525", "3430",
}
S16_ITEM_IDS = frozenset(
    {"2517", "2510", "6697", "3430", "2512", "2523", "4011", "2526", "2524", "2525"}
)

# Curated situational counter items per damage profile (only added when threat triggers)
PROFILE_COUNTER_ITEMS: dict[str, tuple[str, ...]] = {
    "AD": ("3033", "3036", "3153", "3156", "3091", "6695", "3139", "6333", "3026", "3123"),
    "AP": ("3165", "3135", "3137", "6653", "4645", "3157", "3102", "3916"),
    "TANK": ("3075", "2504", "4401", "3143", "3110", "3065", "3084", "2502"),
}

ARMOR_PEN_ITEMS = {"3036", "3033", "3071", "6694", "3035"}
HP_SHRED_ITEMS = {"3153", "6653"}
MAGIC_PEN_ITEMS = {"3135", "3137", "4645", "3118"}
PHYSICAL_ONLY_ITEMS = {"3033", "3036", "3123", "6609", "6694", "3035", "3071", "6695", "6697"}
MAGIC_ONLY_ITEMS = {"3135", "3137", "3165", "3916"}
MR_DEFENSE_ITEMS = {"3156", "3091", "3102", "2504", "4401", "3065", "3139"}
ARMOR_DEFENSE_ITEMS = {"3157", "6333", "3075", "3143", "3110", "3026", "2502"}
QSS_CC_ITEMS = {"3139", "3140", "3102", "3814"}

# Trinkets / free Poro-Snax never occupy any of the 6 main inventory slots
TRINKET_AND_FREE_ITEM_IDS = {"2052", "2054", "3340", "3363", "3364", "3330", "3513"}
# Completed Tier-2 / Tier-3 boots (occupy 1 of the 6 main inventory slots)
TIER2_BOOT_IDS = {
    "3006", "3009", "3020", "3047", "3111", "3158", "3010", "3013",
    "3170", "3171", "3172", "3173", "3174", "3175", "3176",
}
ELIXIR_IDS = {"2138", "2139", "2140"}
_PROFILE_DEFAULT_ELIXIR: dict[str, tuple[str, str]] = {
    "AP": ("2139", "巫术合剂"),
    "TANK": ("2138", "钢铁合剂"),
    "AD": ("2140", "愤怒合剂"),
}


@dataclass(frozen=True)
class InventoryStatus:
    complete_items: tuple[str, ...]
    tier2_boots_id: str
    tier2_boots_name: str
    tier2_boots_gold: int
    occupied_slots: int
    is_six_complete: bool
    is_full_build_with_boots: bool
    is_full_build: bool
    recommended_elixir_id: str
    recommended_elixir_name: str


def analyze_inventory_slots(
    store: DataStore,
    my_items: tuple[str, ...] | list[str],
    profile: str = "AP",
) -> InventoryStatus:
    """Inspect player's 6 real inventory slots (excluding trinkets/Poro-Snax) to detect true full-build state."""
    complete_items: list[str] = []
    tier2_boots_id = ""
    tier2_boots_name = ""
    tier2_boots_gold = 0
    occupied = 0

    for raw in my_items:
        raw_s = str(raw).strip()
        if not raw_s:
            continue
        nid = normalize_item_id(raw_s)
        if not nid or raw_s in ("220013",) or nid in TRINKET_AND_FREE_ITEM_IDS:
            continue
        info = store.get_item_info(nid)
        if info.name == "魄罗佳肴" or (info.gold == 0 and info.category != "complete_equipment"):
            continue
        occupied += 1
        is_t2_boot = (
            nid in TIER2_BOOT_IDS
            or (info.gold >= 900 and (info.name.endswith("靴") or info.name.endswith("胫甲")))
        )
        if is_t2_boot and not tier2_boots_id:
            tier2_boots_id = nid
            tier2_boots_name = info.name
            tier2_boots_gold = info.gold
        elif info.category == "complete_equipment" or info.gold >= 2200:
            complete_items.append(nid)

    is_six_complete = len(complete_items) >= 6
    is_full_with_boots = (len(complete_items) == 5 and bool(tier2_boots_id))
    elx_id, elx_fallback = _PROFILE_DEFAULT_ELIXIR.get(profile, ("2139", "巫术合剂"))
    elx_info = store.get_item_info(elx_id)
    elx_name = elx_info.name if (elx_info.name and not elx_info.name.startswith("装备#")) else elx_fallback
    return InventoryStatus(
        complete_items=tuple(complete_items),
        tier2_boots_id=tier2_boots_id,
        tier2_boots_name=tier2_boots_name,
        tier2_boots_gold=tier2_boots_gold,
        occupied_slots=min(6, occupied),
        is_six_complete=is_six_complete,
        is_full_build_with_boots=is_full_with_boots,
        is_full_build=(is_six_complete or is_full_with_boots),
        recommended_elixir_id=elx_id,
        recommended_elixir_name=elx_name,
    )


@dataclass(frozen=True)
class MatchupThreatProfile:
    enemy_armor_score: float
    enemy_hp_score: float
    enemy_mr_score: float
    enemy_ad_ratio: float
    enemy_ap_ratio: float
    enemy_heal_score: float
    enemy_shield_score: float
    enemy_cc_score: float
    ally_has_grievous: bool
    ally_has_shield_break: bool
    top_threat_name: str
    top_threat_kda: str
    top_threat_type: str  # "AD" | "AP" | "MIXED"
    summary_tags: tuple[str, ...]
    enemy_snowball_count: int = 0
    enemy_exhaust_count: int = 0
    top_threat_spells: tuple[str, ...] = ()
    enemy_tank_count: int = 0
    enemy_mr_builder_count: int = 0


@dataclass(frozen=True)
class DynamicItemRec:
    item_id: str
    name: str
    price: int
    affordable: bool
    gold_needed: int
    score: float
    win_rate: float | None
    games: int
    reasons: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True)
class NextBuyPlan:
    target: DynamicItemRec
    effective_cost: int
    affordable_now: bool
    gold_gap: int
    buy_now_hint: str
    remaining_build_path: tuple[str, ...]
    remaining_build_ids: tuple[str, ...] = ()
    backup: DynamicItemRec | None = None
    is_full_build: bool = False
    full_build_mode: str = ""  # "" | "six_complete" | "five_plus_boots"
    recommended_boots_id: str = ""
    recommended_boots_name: str = ""
    recommended_boots_reason: str = ""
    is_opening_buy: bool = False


def _player_weight(p: PlayerLiveInfo) -> float:
    return max(0.7, min(1.6, 1.0 + (p.kills - p.deaths) * 0.08 + p.assists * 0.02))


def _matches_hero_set(p: PlayerLiveInfo, hero_set: set[str], store: DataStore) -> bool:
    if p.champion_name in hero_set:
        return True
    raw = p.raw_champion_name.replace("game_character_displayname_", "")
    if raw in hero_set:
        return True
    h = store.find_hero_by_name(p.champion_name) or store.find_hero_by_name(p.raw_champion_name)
    if h is not None:
        if h.name in hero_set:
            return True
        alias = store.static.get_champion_alias(h.hero_id)
        if alias in hero_set:
            return True
    return False


def _infer_hero_profile(
    store: DataStore,
    hero_id: str,
    my_items: tuple[str, ...] | list[str],
    selected_augments: tuple[str, ...] | list[str],
) -> str:
    override = detect_profile_override(store, selected_augments)
    if override:
        return override

    ad_s = 0.0
    ap_s = 0.0
    tank_s = 0.0

    # Check currently owned items first
    for iid in my_items:
        info = store.get_item_info(iid)
        ad_s += info.stats.get("FlatPhysicalDamageMod", 0.0) * 1.4
        ad_s += info.stats.get("FlatCritChanceMod", 0.0) * 120.0
        ap_s += info.stats.get("FlatMagicDamageMod", 0.0) * 1.0
        tank_s += (
            info.stats.get("FlatArmorMod", 0.0) * 1.3
            + info.stats.get("FlatSpellBlockMod", 0.0) * 1.3
            + info.stats.get("FlatHPPoolMod", 0.0) * 0.14
        )

    # Also check core builds for the champion
    for cb in store.get_core_builds(hero_id)[:2]:
        for iid in cb.items:
            info = store.get_item_info(iid)
            ad_s += info.stats.get("FlatPhysicalDamageMod", 0.0) * 1.1
            ad_s += info.stats.get("FlatCritChanceMod", 0.0) * 100.0
            ap_s += info.stats.get("FlatMagicDamageMod", 0.0) * 0.85
            tank_s += (
                info.stats.get("FlatArmorMod", 0.0) * 1.1
                + info.stats.get("FlatSpellBlockMod", 0.0) * 1.1
                + info.stats.get("FlatHPPoolMod", 0.0) * 0.10
            )

    if tank_s >= max(ad_s, ap_s) * 1.15 and tank_s > 60:
        return "TANK"
    if ap_s > ad_s * 1.15:
        return "AP"
    return "AD"


def analyze_matchup_threats(
    store: DataStore,
    allies: tuple[PlayerLiveInfo, ...] | list[PlayerLiveInfo],
    enemies: tuple[PlayerLiveInfo, ...] | list[PlayerLiveInfo],
) -> MatchupThreatProfile:
    armor_score = 0.0
    hp_score = 0.0
    mr_score = 0.0
    ad_power = 0.0
    ap_power = 0.0
    heal_score = 0.0
    shield_score = 0.0
    cc_score = 0.0

    top_weight = 0.0
    top_name = ""
    top_kda = ""
    top_type = "MIXED"
    top_spells: tuple[str, ...] = ()
    enemy_snowball_count = 0
    enemy_exhaust_count = 0
    enemy_tank_count = 0
    enemy_mr_builder_count = 0

    for p in enemies:
        w = _player_weight(p)
        p_ad = 0.0
        p_ap = 0.0
        p_armor = 0.0
        p_hp = 0.0
        p_mr = 0.0
        if "雪球" in p.summoner_spells:
            enemy_snowball_count += 1
        if "虚弱" in p.summoner_spells:
            enemy_exhaust_count += 1

        for iid in p.item_ids:
            norm = normalize_item_id(iid)
            info = store.get_item_info(iid)
            if not info.stats and norm and norm != str(iid):
                info = store.get_item_info(norm)
            st = info.stats
            item_armor = st.get("FlatArmorMod", 0.0)
            item_hp = st.get("FlatHPPoolMod", 0.0)
            item_mr = st.get("FlatSpellBlockMod", 0.0)
            p_armor += item_armor
            p_hp += item_hp
            p_mr += item_mr
            armor_score += (item_armor / 60.0) * w
            hp_score += (item_hp / 450.0) * w
            mr_score += (item_mr / 55.0) * w
            p_ad += (
                st.get("FlatPhysicalDamageMod", 0.0)
                + st.get("FlatCritChanceMod", 0.0) * 120.0
                + st.get("PercentAttackSpeedMod", 0.0) * 80.0
            )
            p_ap += st.get("FlatMagicDamageMod", 0.0) * 0.85

            if norm in HEAL_ITEM_IDS or str(iid) in HEAL_ITEM_IDS or st.get("PercentLifeStealMod", 0.0) > 0:
                heal_score += 0.55 * w
            if norm in SHIELD_ITEM_IDS or str(iid) in SHIELD_ITEM_IDS:
                shield_score += 0.50 * w

        if p_armor >= 60.0 or p_hp >= 750.0:
            enemy_tank_count += 1
        if p_mr >= 40.0:
            enemy_mr_builder_count += 1

        # Early game fallback if enemy hasn't bought items yet
        if p_ad == 0.0 and p_ap == 0.0:
            h = store.find_hero_by_name(p.champion_name) or store.find_hero_by_name(p.raw_champion_name)
            if h is not None:
                prof = _infer_hero_profile(store, h.hero_id, (), ())
                if prof == "AP":
                    p_ap = 70.0
                elif prof == "AD":
                    p_ad = 70.0
                else:
                    armor_score += 0.4 * w
                    hp_score += 0.5 * w

        ad_power += p_ad * w
        ap_power += p_ap * w

        if _matches_hero_set(p, HIGH_HEAL_HEROES, store):
            heal_score += 1.2 * w
        if _matches_hero_set(p, HIGH_SHIELD_HEROES, store):
            shield_score += 1.0 * w
        if _matches_hero_set(p, HIGH_CC_HEROES, store):
            cc_score += 1.0 * w

        if w > top_weight:
            top_weight = w
            top_name = p.champion_name or p.raw_champion_name
            top_kda = f"{p.kills}/{p.deaths}/{p.assists}"
            top_spells = p.summoner_spells
            if p_ap > p_ad * 1.25:
                top_type = "AP"
            elif p_ad > p_ap * 1.25:
                top_type = "AD"
            else:
                top_type = "MIXED"

    if top_weight < 1.2:
        top_name = ""
        top_kda = ""
        top_type = "MIXED"
        top_spells = ()

    total_dmg = ad_power + ap_power
    ad_ratio = (ad_power / total_dmg) if total_dmg > 0 else 0.5
    ap_ratio = (ap_power / total_dmg) if total_dmg > 0 else 0.5

    ally_has_gw = any(
        store.get_item_info(iid).has_grievous for p in allies for iid in p.item_ids
    )
    ally_has_sb = any(
        store.get_item_info(iid).has_shield_break for p in allies for iid in p.item_ids
    )

    has_high_armor_tank = (armor_score >= 2.6) or (
        enemy_tank_count >= 2 and (armor_score >= 1.8 or hp_score >= 3.2)
    )
    has_mr_stack = (mr_score >= 2.4) or (enemy_mr_builder_count >= 2 and mr_score >= 1.8)

    summary: list[str] = []
    if has_high_armor_tank:
        summary.append("敌方高坦/堆甲")
    if has_mr_stack:
        summary.append("敌方堆魔抗")
    if heal_score >= 1.8:
        summary.append("敌方高回复(缺重伤)" if not ally_has_gw else "敌方高回复(队友有重伤)")
    if shield_score >= 1.8:
        summary.append("敌方厚护盾")
    if ap_ratio >= 0.65:
        summary.append(f"敌方重AP({int(ap_ratio * 100)}%)")
    elif ad_ratio >= 0.65:
        summary.append(f"敌方重AD({int(ad_ratio * 100)}%)")
    if cc_score >= 2.2:
        summary.append("敌方强控")
    if enemy_snowball_count >= 3:
        summary.append(f"敌方{enemy_snowball_count}雪球强开")
    if enemy_exhaust_count >= 2:
        summary.append(f"敌方{enemy_exhaust_count}虚弱防突")
    if top_weight >= 1.2 and top_name:
        summary.append(f"头号威胁:{top_name}({top_kda})")

    return MatchupThreatProfile(
        enemy_armor_score=round(armor_score, 2),
        enemy_hp_score=round(hp_score, 2),
        enemy_mr_score=round(mr_score, 2),
        enemy_ad_ratio=round(ad_ratio, 3),
        enemy_ap_ratio=round(ap_ratio, 3),
        enemy_heal_score=round(heal_score, 2),
        enemy_shield_score=round(shield_score, 2),
        enemy_cc_score=round(cc_score, 2),
        ally_has_grievous=ally_has_gw,
        ally_has_shield_break=ally_has_sb,
        top_threat_name=top_name,
        top_threat_kda=top_kda,
        top_threat_type=top_type,
        summary_tags=tuple(summary),
        enemy_snowball_count=enemy_snowball_count,
        enemy_exhaust_count=enemy_exhaust_count,
        top_threat_spells=top_spells,
        enemy_tank_count=enemy_tank_count,
        enemy_mr_builder_count=enemy_mr_builder_count,
    )


# Deleted legacy item IDs from older LoL seasons occasionally hallucinated by LLMs -> canonical 26.23 IDs
LEGACY_ITEM_ID_ALIASES: dict[str, str] = {
    "3151": "6653",  # Old Liandry's Torment -> 兰德里的折磨 (6653)
    "4637": "6653",  # Old Demonic Embrace -> 兰德里的折磨 (6653)
}


def canonicalize_item_id(item_id: str | int) -> str:
    """Normalize 6-digit Mayhem prefixes and map known deleted legacy IDs to current 26.23 IDs."""
    nid = normalize_item_id(item_id)
    if not nid:
        return ""
    return LEGACY_ITEM_ID_ALIASES.get(nid, nid)


def is_valid_plan_item(store: DataStore, item_id: str | int) -> bool:
    """Return True iff item_id resolves to a real purchasable complete equipment item in current DDragon."""
    nid = canonicalize_item_id(item_id)
    if not nid:
        return False
    if store.static.items and nid not in store.static.items:
        return False
    info = store.get_item_info(nid)
    if not info.name or info.name.startswith("装备#"):
        return False
    if info.category in ("consumable", "boots"):
        return False
    return info.category == "complete_equipment" or info.gold >= 2200


def _resolve_my_player(
    store: DataStore,
    hero_id: str,
    my_items: tuple[str, ...] | list[str],
    allies: tuple[PlayerLiveInfo, ...] | list[PlayerLiveInfo],
    my_player: PlayerLiveInfo | None = None,
) -> PlayerLiveInfo | None:
    if my_player is not None:
        return my_player
    hero = store.get_hero(hero_id)
    hero_alias = store.static.get_champion_alias(hero_id).lower()
    hero_name = hero.name if hero is not None else ""
    for p in allies:
        if hero_name and p.champion_name == hero_name:
            return p
        raw = p.raw_champion_name.replace("game_character_displayname_", "").lower()
        if hero_alias and (raw == hero_alias or p.champion_name.lower() == hero_alias):
            return p
        found = store.find_hero_by_name(p.champion_name) or store.find_hero_by_name(p.raw_champion_name)
        if found is not None and found.hero_id == hero_id:
            return p
    if my_items:
        my_norm = sorted(normalize_item_id(x) for x in my_items if str(x).strip())
        for p in allies:
            p_norm = sorted(normalize_item_id(x) for x in p.item_ids if str(x).strip())
            if p_norm == my_norm:
                return p
    return None


def optimize_next_items(
    store: DataStore,
    hero_id: str | int,
    my_items: tuple[str, ...] | list[str],
    my_gold: float,
    selected_augments: tuple[str, ...] | list[str],
    allies: tuple[PlayerLiveInfo, ...] | list[PlayerLiveInfo],
    enemies: tuple[PlayerLiveInfo, ...] | list[PlayerLiveInfo],
    limit: int = 4,
    committed_plan: tuple[str, ...] | list[str] = (),
    locked_next_item: str = "",
    my_player: PlayerLiveInfo | None = None,
) -> tuple[MatchupThreatProfile, list[DynamicItemRec]]:
    """Compute dynamically re-ranked next items with slot-ordered core progression & plan continuity."""
    hid = str(hero_id)
    hero = store.get_hero(hid)
    hero_wr = hero.win_rate if hero else 0.50
    threat = analyze_matchup_threats(store, allies, enemies)
    profile = _infer_hero_profile(store, hid, my_items, selected_augments)
    resolved_me = _resolve_my_player(store, hid, my_items, allies, my_player=my_player)
    severely_behind = bool(resolved_me is not None and resolved_me.deaths >= resolved_me.kills + 6)
    has_fed_enemy_carry = any(e.kills >= 15 for e in enemies)
    survival_emergency = severely_behind or has_fed_enemy_carry

    raw_item_recs = store.get_hero_item_recs(hid)
    tear_merged = bool(getattr(store, "tear_pairs_merged", False))
    item_recs: dict[str, ItemRecStat] = {}
    for raw_k, rec in raw_item_recs.items():
        if tear_merged and raw_k in ("3040", "3042", "3121", "2530"):
            base_k = normalize_item_id(raw_k)
            if base_k in raw_item_recs:
                continue
        nk = normalize_item_id(raw_k)
        prev = item_recs.get(nk)
        if prev is None:
            item_recs[nk] = ItemRecStat(
                item_id=nk,
                games=rec.games,
                wins=rec.wins,
                win_rate=rec.win_rate,
                low_sample=rec.low_sample,
            )
        else:
            tot_g = prev.games + rec.games
            tot_w = prev.wins + rec.wins
            item_recs[nk] = ItemRecStat(
                item_id=nk,
                games=tot_g,
                wins=tot_w,
                win_rate=(tot_w / tot_g) if tot_g > 0 else 0.0,
                low_sample=prev.low_sample and rec.low_sample,
            )
    core_builds = store.get_core_builds(hid)
    core_item_ids: set[str] = {normalize_item_id(x) for cb in core_builds[:3] for x in cb.items}

    # Count already-completed major items so 1st/2nd core items stay grounded while 3rd+ adapts to enemy comp
    owned_complete_count = sum(
        1
        for iid in my_items
        if (store.get_item_info(iid).category == "complete_equipment" or store.get_item_info(iid).gold >= 2200)
    )
    next_slot_core_bonus = 3.2 if owned_complete_count == 0 else (2.6 if owned_complete_count == 1 else 1.4)

    # Game-weighted consensus starter detection across top core builds (e.g. Heartsteel on Mundo/Tahm/Sett)
    starter_games: dict[str, int] = {}
    total_cb_games = 0
    for cb in core_builds[:3]:
        if cb.items:
            s0 = normalize_item_id(cb.items[0])
            starter_games[s0] = starter_games.get(s0, 0) + max(1, cb.games)
            total_cb_games += max(1, cb.games)
    consensus_starters: set[str] = {
        iid for iid, g in starter_games.items() if total_cb_games > 0 and (g / total_cb_games) >= 0.50
    }

    next_slot_core_ids: set[str] = set()
    for cb in core_builds[:2]:
        for cid in cb.items:
            nc = normalize_item_id(cid)
            if nc and not item_is_owned(nc, my_items):
                next_slot_core_ids.add(nc)
                break

    # Low-weight champion signature tie-breaker (NEVER overrides big data or special augments)
    hero_sig = get_hero_signature_db().get(hid, hero.name if hero else "")
    sig_starters = {normalize_item_id(x) for x in (hero_sig.starter_items if hero_sig else ())}
    sig_items = {normalize_item_id(x) for x in (hero_sig.signature_items if hero_sig else ())}
    sig_avoid_passive = {normalize_item_id(x) for x in (hero_sig.avoid_passive if hero_sig else ())}

    # Scale Bayesian shrinkage prior proportionally to hero's top-item sample size to defeat 1%-pickrate survivorship bias!
    max_item_games = max((r.games for r in item_recs.values()), default=0)
    shrink_prior = max(120.0, max_item_games * 0.05)

    committed_norm: list[str] = [normalize_item_id(x) for x in committed_plan if x]
    locked_norm = normalize_item_id(locked_next_item) if locked_next_item else ""

    # Build destiny map if player owns Destiny's Weapon (3081)
    destiny_map: dict[str, float] = {}
    if DESTINY_AUGMENT_ID in {str(a) for a in selected_augments}:
        for d in rank_destiny_items(store, hid, limit=5):
            destiny_map[normalize_item_id(d.item_id)] = d.delta

    # Assemble candidate pool: real high-usage hero items + core build items + profile counter items
    candidate_ids: list[str] = []
    seen_norm: set[str] = set()

    # Load special-augment candidate items & off-profile exemptions from the 26.23 Special Augment Database
    special_candidates, special_exempt = get_special_augment_candidate_items(store, selected_augments)
    active_specials = resolve_special_builds(store, selected_augments)
    has_transformative_special = any(sp.transformative for sp in active_specials)
    has_unowned_force_special = any(
        sp.transformative and any(not item_is_owned(fi, my_items) for fi in sp.force_items)
        for sp in active_specials
    )

    # Check whether player has a crit/conversion/tank augment that allows off-profile items
    aug_names = " ".join(
        (store.get_augment(a).name if store.get_augment(a) else "") for a in selected_augments
    )
    allows_spell_crit = "珠光护手" in aug_names or "珠光手镯" in aug_names
    allows_ad_on_ap = "魔法转物理" in aug_names
    allows_ap_on_ad = "物理转魔法" in aug_names
    allows_tank_on_carry = any(k in aug_names for k in ("坦克引擎", "装甲冲击", "心之钢", "钢化你心", "死亡之环", "蛋白粉奶昔", "升级：献祭"))

    def _is_off_profile_item(info: ItemStaticInfo) -> bool:
        norm_i = normalize_item_id(info.item_id)
        # Strict damage-type gating: physical armor-pen/GW items NEVER belong on pure AP builds (even with Jeweled Gauntlet)
        if profile == "AP" and not allows_ad_on_ap and norm_i in PHYSICAL_ONLY_ITEMS:
            return True
        if profile == "AD" and not allows_ap_on_ad and norm_i in MAGIC_ONLY_ITEMS:
            return True
        if norm_i in special_exempt or norm_i in core_item_ids:
            return False
        st = info.stats
        ad_v = st.get("FlatPhysicalDamageMod", 0.0)
        ap_v = st.get("FlatMagicDamageMod", 0.0)
        crit_v = st.get("FlatCritChanceMod", 0.0)
        as_v = st.get("PercentAttackSpeedMod", 0.0)
        armor_pen = "ArmorPenetration" in info.tags
        is_pure_tank = (
            ad_v <= 0
            and ap_v <= 0
            and crit_v <= 0
            and as_v <= 0
            and (
                st.get("FlatArmorMod", 0.0) > 0
                or st.get("FlatSpellBlockMod", 0.0) > 0
                or st.get("FlatHPPoolMod", 0.0) >= 350
            )
            and norm_i not in ("3140", "3139")
        )
        if profile == "AP":
            if not allows_ad_on_ap and (ad_v > 0 or armor_pen) and ap_v <= 0:
                if not (allows_spell_crit and crit_v > 0 and norm_i == "3031"):
                    return True
            if is_pure_tank and not allows_tank_on_carry:
                return True
        elif profile == "AD":
            if not allows_ap_on_ad and ap_v > 0 and ad_v <= 0 and "SpellBlock" not in info.tags:
                return True
            if is_pure_tank and not allows_tank_on_carry:
                return True
        return False

    player_has_gw_complete = any(
        store.get_item_info(i).has_grievous
        and (store.get_item_info(i).category == "complete_equipment" or store.get_item_info(i).gold >= 2200)
        for i in my_items
    )

    def add_candidate(iid: str) -> None:
        norm = canonicalize_item_id(iid)
        if not norm or norm in seen_norm:
            return
        if item_is_owned(norm, my_items):
            return
        if store.static.items and norm not in store.static.items:
            return
        info = store.get_item_info(norm)
        if not info.name or info.name.startswith("装备#"):
            return
        if norm in ("3123", "3916") and player_has_gw_complete:
            return
        # Skip boots/consumables unless explicitly a counter component (800G GW / 1300G QSS) or special DB item
        if info.category != "complete_equipment" and norm not in ("3123", "3916", "3140") and norm not in special_exempt and info.gold < 2200:
            return
        if _is_off_profile_item(info):
            return
        seen_norm.add(norm)
        candidate_ids.append(norm)

    # Ensure locked/committed plan items AND special-augment archetype core items enter the candidate pool
    if locked_norm:
        add_candidate(locked_norm)
    for cid in committed_norm:
        add_candidate(cid)
    for sid in special_candidates:
        add_candidate(sid)

    # Top hero items from 26.23 stats
    sorted_recs = sorted(item_recs.values(), key=lambda r: r.games, reverse=True)
    for r in sorted_recs[:28]:
        if r.games >= 80 or len(candidate_ids) < 12:
            add_candidate(r.item_id)

    for cid in core_item_ids:
        add_candidate(cid)

    # Add situational counter items for the hero's profile when threats are present
    for cid in PROFILE_COUNTER_ITEMS.get(profile, ()):
        # Only add cheap GW components if healing threat & no ally GW & low gold
        if cid in ("3123", "3916") and not (threat.enemy_heal_score >= 1.8 and not threat.ally_has_grievous):
            continue
        add_candidate(cid)

    # If player owns Tear of the Goddess (3070) or picked No Mana No Problem, include Archangel's Staff (3003)
    if profile == "AP" and (item_is_owned("3070", my_items) or "没法力" in aug_names):
        add_candidate("3003")

    has_high_armor_tank = (threat.enemy_armor_score >= 2.6) or (
        threat.enemy_tank_count >= 2 and (threat.enemy_armor_score >= 1.8 or threat.enemy_hp_score >= 3.2)
    )
    has_mr_stack = (threat.enemy_mr_score >= 2.4) or (
        threat.enemy_mr_builder_count >= 2 and threat.enemy_mr_score >= 1.8
    )

    scored: list[DynamicItemRec] = []
    for iid in candidate_ids:
        info: ItemStaticInfo = store.get_item_info(iid)
        rec_stat = item_recs.get(iid)
        games = rec_stat.games if rec_stat else 0
        raw_wr = rec_stat.win_rate if rec_stat else None
        pick_share = (games / max_item_games) if max_item_games > 0 else 0.0
        bias = store.get_item_global_bias(iid)
        role_prior = store.get_role_item_prior(profile, iid) if games < 300 else None
        if rec_stat and rec_stat.games > 0:
            if games < 300 and role_prior is not None:
                class_prior_wr = max(0.30, min(0.70, hero_wr + role_prior))
                smoothed_wr = (rec_stat.wins + shrink_prior * class_prior_wr) / (rec_stat.games + shrink_prior)
            else:
                smoothed_wr = (rec_stat.wins + shrink_prior * hero_wr) / (rec_stat.games + shrink_prior)
            effective_delta = (smoothed_wr - hero_wr) - 0.65 * bias
            base_score = effective_delta * 100.0
            # Clamp low-adoption (<5% pick share) late-slot survivorship-bias outliers
            if pick_share < 0.05:
                base_score = max(-2.0, min(2.0, base_score))
        elif role_prior is not None:
            effective_delta = role_prior - 0.65 * bias
            base_score = max(-2.0, min(2.0, effective_delta * 100.0))
        else:
            base_score = 0.0

        score = base_score
        reasons: list[str] = []
        tags: list[str] = []

        # Big-data mainstream adoption confidence bonus (rewards real high-sample core items over 0.3%-pick outliers)
        if pick_share >= 0.08:
            score += min(2.4, math.log10(1.0 + pick_share * 20.0) * 1.8)

        # Committed build-plan continuity & component-investment lock (prevents death-to-death flip-flopping!)
        invested_credit, _, _ = _owned_component_credit(store, iid, list(my_items))
        if locked_norm and iid == locked_norm:
            if invested_credit == 0 and survival_emergency:
                score += 1.6
            else:
                score += 5.2
            tags.append("锁定下件")
        elif iid in committed_norm:
            score += 2.2
            tags.append("既定路线")

        if invested_credit >= 700:
            score += min(4.8, 2.6 + invested_credit / 800.0)
            reasons.append(f"已购{invested_credit}G散件，顺势合成")
            tags.append("已有散件")

        # Slot-ordered big-data core progression (gives consensus 1st-slot core like Heartsteel / Blackfire strong anchor)
        if owned_complete_count == 0 and iid in consensus_starters:
            score += 2.0 if (has_unowned_force_special and iid not in special_exempt) else 4.4
            tags.append("大数据首选")
        elif iid in next_slot_core_ids:
            score += next_slot_core_bonus
            tags.append("当前顺位核心")
        elif iid in core_item_ids:
            score += 1.2
            tags.append("核心三件套")

        # Gentle low-weight hero signature tie-breaker (strictly subordinate to big data & special augments!)
        if not has_transformative_special:
            if owned_complete_count == 0 and iid in sig_starters:
                score += 1.2
                tags.append("英雄招牌")
            elif iid in sig_items:
                score += 0.8
            elif iid in sig_avoid_passive and iid not in special_exempt:
                score -= 1.6

        # Destiny's Weapon statistical bonus
        if iid in destiny_map:
            ddelta = destiny_map[iid]
            score += 6.0 + ddelta * 60.0
            reasons.append(f"命运之武器高胜率绑定 ({ddelta * 100:+.1f}%)")
            tags.append("命运绑定")

        # Augment synergy bonus / anti-synergy penalty (from 26.23 Special Augment Database)
        syn_bonus, syn_labels = evaluate_augment_synergies(store, selected_augments, info)
        if syn_bonus != 0:
            score += syn_bonus
            if syn_labels:
                tags.extend(syn_labels)
                reasons.append(syn_labels[0])

        # --- Matchup situational adjustments ---
        # 1. Armor / HP shred (armor pen strictly gated to physical profiles)
        if has_high_armor_tank and (
            (iid in ARMOR_PEN_ITEMS and (profile != "AP" or allows_ad_on_ap))
            or (iid in HP_SHRED_ITEMS and (iid != "3153" or profile != "AP" or allows_ad_on_ap))
        ):
            boost = 4.2 if iid in ARMOR_PEN_ITEMS else 3.8
            score += boost
            if threat.enemy_hp_score >= 3.2 and iid in HP_SHRED_ITEMS:
                reasons.append("敌方堆血前排厚，百分比打坦")
            else:
                reasons.append("敌方护甲偏高，提前补穿甲")
            tags.append("打坦对策")

        # 2. Magic Pen vs MR stacking (strictly gated to magic profiles)
        if has_mr_stack and iid in MAGIC_PEN_ITEMS and (profile != "AD" or allows_ap_on_ad):
            score += 4.5 if iid in ("3135", "3137") else 2.5
            reasons.append("敌方多人已出魔抗，法穿收益拉满")
            tags.append("法穿对策")

        # 3. Grievous Wounds vs High Healing (with ally deduplication!)
        if threat.enemy_heal_score >= 1.8:
            if info.has_grievous:
                if not threat.ally_has_grievous:
                    score += 5.8 if iid not in ("3123", "3916") else (4.8 if my_gold < 2200 else 2.0)
                    reasons.append("敌方高回复且我方尚无重伤")
                    tags.append("必出重伤")
                else:
                    score -= 1.8
                    reasons.append("队友已有重伤，优先级下调")
            elif iid == "3036" and threat.ally_has_grievous and threat.enemy_armor_score >= 1.5:
                score += 2.2
                reasons.append("队友已有重伤，直出大轻语拉满输出")

        # 4. Shield breaking
        if threat.enemy_shield_score >= 1.8 and not threat.ally_has_shield_break:
            if info.has_shield_break:
                score += 4.8
                reasons.append("敌方多护盾阵容，破盾收益极高")
                tags.append("破盾对策")
            elif iid == "4645" and profile == "AP":
                score += 2.0
                reasons.append("敌方护盾多，影焰法穿增伤")

        # Defensive counter scaling: don't force passive defensive items at 0 items over scaling core starters (e.g. Heartsteel / Hubris)!
        def_scale = 0.35 if owned_complete_count == 0 else (0.80 if owned_complete_count == 1 else 1.0)

        # 5. Heavy AP enemy -> MR defensive-offensive
        if (threat.enemy_ap_ratio >= 0.65 or (threat.top_threat_type == "AP" and threat.top_threat_name)) and (
            iid in MR_DEFENSE_ITEMS
        ):
            score += (4.2 if threat.enemy_ap_ratio >= 0.65 else 2.4) * def_scale
            if survival_emergency:
                score += 1.5 * def_scale
            if threat.top_threat_type == "AP" and threat.top_threat_name:
                reasons.append(f"敌方重AP({int(threat.enemy_ap_ratio * 100)}%)且{threat.top_threat_name}肥，补魔抗防秒")
            else:
                reasons.append(f"敌方实际AP出装占比{int(threat.enemy_ap_ratio * 100)}%，补魔抗")
            tags.append("防AP")

        # 6. Heavy AD enemy -> Armor defensive-offensive
        if (threat.enemy_ad_ratio >= 0.65 or (threat.top_threat_type == "AD" and threat.top_threat_name)) and (
            iid in ARMOR_DEFENSE_ITEMS
        ):
            score += (4.0 if threat.enemy_ad_ratio >= 0.65 else 2.2) * def_scale
            if survival_emergency:
                score += 1.5 * def_scale
            reasons.append(f"敌方实际AD出装占比{int(threat.enemy_ad_ratio * 100)}%，补护甲保命")
            tags.append("防AD")

        # 7. Hard CC -> QSS / Spellshield
        if threat.enemy_cc_score >= 2.2 and iid in QSS_CC_ITEMS:
            score += 3.2 * def_scale
            reasons.append("敌方硬控开团多，补解控/法术护盾")
            tags.append("解控保命")

        if not reasons:
            if raw_wr is not None and games >= 200:
                reasons.append(f"26.23 英雄常规高胜率成装 ({raw_wr * 100:.1f}%)")
            else:
                reasons.append("版本常规核心路线")

        price = info.gold
        affordable = bool(price > 0 and my_gold >= price)
        gold_needed = max(0, int(price - my_gold)) if price > 0 else 0

        scored.append(
            DynamicItemRec(
                item_id=iid,
                name=info.name,
                price=price,
                affordable=affordable,
                gold_needed=gold_needed,
                score=round(score, 2),
                win_rate=raw_wr,
                games=games,
                reasons=tuple(reasons[:2]),
                tags=tuple(dict.fromkeys(tags)),
            )
        )

    scored.sort(key=lambda r: (r.score, r.games), reverse=True)
    return threat, scored[:limit]


def _owned_component_credit(
    store: DataStore,
    item_id: str,
    owned_pool: list[str],
) -> tuple[int, list[str], list[str]]:
    """Compute (credit_gold, missing_direct_components, remaining_unmatched_owned_pool)."""
    info = store.get_item_info(item_id)
    pool = [normalize_item_id(x) for x in owned_pool]
    if not info.components:
        return 0, [], pool
    credit = 0
    missing: list[str] = []
    for comp in info.components:
        nc = normalize_item_id(comp)
        if nc in pool:
            pool.remove(nc)
            credit += store.get_item_info(nc).gold
        else:
            # Check one level deeper (e.g., owns Amplifying Tome inside Seeker's Armguard)
            sub_info = store.get_item_info(nc)
            sub_credit = 0
            for sub in sub_info.components:
                ns = normalize_item_id(sub)
                if ns in pool:
                    pool.remove(ns)
                    sub_credit += store.get_item_info(ns).gold
            credit += sub_credit
            missing.append(nc)
    return credit, missing, pool


def compute_item_buy_now(
    store: DataStore,
    item_id: str,
    my_items: tuple[str, ...] | list[str],
    my_gold: float,
) -> tuple[int, bool, int, str]:
    """Compute (effective_cost, affordable_now, gold_gap, buy_now_hint) for a specific target item."""
    info = store.get_item_info(item_id)
    credit, missing_comps, _ = _owned_component_credit(store, item_id, list(my_items))
    effective_cost = max(0, info.gold - credit) if info.gold > 0 else 0
    affordable_now = bool(effective_cost > 0 and my_gold >= effective_cost)
    gold_gap = max(0, int(effective_cost - my_gold)) if effective_cost > 0 else 0

    inv_for_boots = analyze_inventory_slots(store, my_items)
    suppress_late_boots = len(inv_for_boots.complete_items) >= 4
    owns_basic_boots = item_is_owned("1001", my_items) and not suppress_late_boots
    norm_target = normalize_item_id(item_id)
    is_boot_upgrade = "Boots" in info.tags or norm_target in ("3020", "3006", "3047", "3111", "3158", "3009")

    if affordable_now:
        return (
            effective_cost,
            True,
            0,
            f"直接合成【{info.name}】({effective_cost}G)",
        )

    picks: list[tuple[str, int]] = []
    budget = int(my_gold)
    # Remove any already-owned direct components from pool so only subcomponents remain
    rem_pool = [normalize_item_id(x) for x in my_items]
    for comp in info.components:
        nc = normalize_item_id(comp)
        if nc in rem_pool:
            rem_pool.remove(nc)

    ordered = sorted(missing_comps, key=lambda c: store.get_item_info(c).gold, reverse=True)
    for cid in ordered:
        cinfo = store.get_item_info(cid)
        sub_credit, sub_missing, next_pool = _owned_component_credit(store, cid, rem_pool)
        rem_pool = next_pool
        c_eff = max(0, cinfo.gold - sub_credit)
        if 0 < c_eff <= budget:
            picks.append((cinfo.name, c_eff))
            budget -= c_eff
        elif sub_missing:
            for sub_id in sorted(sub_missing, key=lambda s: store.get_item_info(s).gold, reverse=True):
                sinfo = store.get_item_info(sub_id)
                if 0 < sinfo.gold <= budget:
                    picks.append((sinfo.name, sinfo.gold))
                    budget -= sinfo.gold

    if picks:
        parts = " + ".join(f"{nm}({g}G)" for nm, g in picks[:2])
        extra = " 或升二级鞋(800G)" if (owns_basic_boots and my_gold >= 800 and not is_boot_upgrade) else ""
        hint = f"先买：{parts}{extra}"
    elif owns_basic_boots and my_gold >= 800 and not is_boot_upgrade:
        hint = f"先升二级鞋(800G)，再攒【{info.name}】"
    elif ordered:
        cheapest = min((store.get_item_info(c) for c in ordered), key=lambda x: x.gold if x.gold > 0 else 99999)
        diff = max(0, cheapest.gold - int(my_gold))
        hint = f"差 {diff}G 买 {cheapest.name}({cheapest.gold}G)"
    else:
        hint = f"差 {gold_gap}G 直合【{info.name}】"

    return effective_cost, False, gold_gap, hint


_BOOTS_FALLBACK_NAMES: dict[str, str] = {
    "3111": "水银之靴",
    "3047": "铁板靴",
    "3020": "法师之靴",
    "3006": "狂战士胫甲",
    "3158": "明朗之靴",
    "3009": "轻灵之靴",
}


def _boot_display_name(store: DataStore, boot_id: str) -> str:
    info = store.get_item_info(boot_id)
    if info.name and not info.name.startswith("装备#"):
        return info.name
    return _BOOTS_FALLBACK_NAMES.get(boot_id, "二级鞋")


def recommend_counter_boots(
    store: DataStore,
    hero_id: str | int,
    my_items: tuple[str, ...] | list[str],
    selected_augments: tuple[str, ...] | list[str],
    threat: MatchupThreatProfile | None,
) -> tuple[str, str, str]:
    """Recommend situational Tier-2 boots `(item_id, name, short_reason)` when not yet owned."""
    profile = _infer_hero_profile(store, str(hero_id), my_items, selected_augments)
    inv = analyze_inventory_slots(store, my_items, profile=profile)
    if inv.tier2_boots_id or inv.is_six_complete or len(inv.complete_items) >= 4:
        return "", "", ""

    cc = threat.enemy_cc_score if threat else 0.0
    ad_r = threat.enemy_ad_ratio if threat else 0.5
    ap_r = threat.enemy_ap_ratio if threat else 0.5
    mr_s = threat.enemy_mr_score if threat else 0.0
    snowballs = threat.enemy_snowball_count if threat else 0
    top_type = threat.top_threat_type if threat else "MIXED"

    # 1. Hard CC or heavy AP against melee/tank -> Mercury's Treads (3111)
    if cc >= 2.2:
        reason = "敌强控/多AP" if ap_r >= 0.55 else "敌强控减控"
        return "3111", _boot_display_name(store, "3111"), reason
    if profile == "TANK" and (ap_r >= 0.58 or (ap_r >= 0.48 and cc >= 1.2)):
        return "3111", _boot_display_name(store, "3111"), "敌重AP/抗控"

    # 2. Heavy AD / physical dive threat -> Plated Steelcaps (3047)
    if ad_r >= 0.65 and (profile == "TANK" or ad_r >= 0.72 or snowballs >= 2 or top_type == "AD"):
        reason = "敌重AD/强开" if snowballs >= 2 else "敌重AD减普攻"
        return "3047", _boot_display_name(store, "3047"), reason

    # 3. Profile offensive / tempo boots
    if profile == "AP":
        if mr_s >= 2.2:
            return "3158", _boot_display_name(store, "3158"), "敌高魔抗转CD"
        return "3020", _boot_display_name(store, "3020"), "法穿打脆皮"

    if profile == "AD":
        # Inspect hero's top core builds to distinguish AS/Crit carries vs Caster AD
        as_weight = 0.0
        for cb in store.get_core_builds(str(hero_id))[:2]:
            for cid in cb.items:
                st = store.get_item_info(cid).stats
                as_weight += float(st.get("PercentAttackSpeedMod", 0.0)) * 2.0
                as_weight += float(st.get("FlatCritChanceMod", 0.0)) * 1.2
        if as_weight >= 0.45:
            return "3006", _boot_display_name(store, "3006"), "攻速拉满输出"
        return "3158", _boot_display_name(store, "3158"), "CD高频施法"

    # Tank default when balanced enemy comp
    if ap_r >= 0.52 or cc >= 1.2:
        return "3111", _boot_display_name(store, "3111"), "韧性抗魔抗"
    return "3047", _boot_display_name(store, "3047"), "护甲减普攻"


def compute_aram_opening_buy_hint(
    store: DataStore,
    profile: str,
    target_item_id: str,
    my_items: tuple[str, ...] | list[str],
    my_gold: float,
    game_time: float,
) -> str:
    """Generate a tailored 1400G ARAM fountain opening buy suggestion at match start (0 items, <95s)."""
    inv = analyze_inventory_slots(store, my_items, profile=profile)
    if inv.occupied_slots > 0 or len(inv.complete_items) > 0:
        return ""
    if my_gold < 950.0 or my_gold > 1550.0:
        return ""
    if game_time > 95.0:
        return ""

    budget = int(my_gold)
    guardian_map = {
        "AP": ("守护者宝珠", "增幅典籍", 400),
        "AD": ("守护者战锤", "长剑", 350),
        "TANK": ("守护者号角", "红水晶", 400),
    }
    g_name, g_sub, g_sub_cost = guardian_map.get(profile, ("守护者宝珠", "草鞋", 300))

    # Inspect flagship component of the hero's 1st core item (e.g., Lost Chapter 1200G, Sheen 900G, Dirk 1000G, Bami 900G)
    tinfo = store.get_item_info(target_item_id)
    best_comp: tuple[str, int] | None = None
    for cid in tinfo.components:
        cinfo = store.get_item_info(cid)
        if 850 <= cinfo.gold <= budget and (best_comp is None or cinfo.gold > best_comp[1]):
            best_comp = (cinfo.name, cinfo.gold)

    if profile == "TANK":
        # On ARAM tanks, Guardian's Horn (950G) is statistically broken early unless rushing a 900G core component
        if budget >= 1350:
            return f"开局1400G：守护者号角(950G)+红水晶(400G)，或直接买{best_comp[0]}({best_comp[1]}G)" if best_comp else "开局1400G：守护者号角(950G)+红水晶(400G)"
        return "开局出门：守护者号角(950G)+草鞋(300G)"

    if best_comp is not None:
        cname, cgold = best_comp
        rem = budget - cgold
        if rem >= g_sub_cost:
            return f"开局1400G：{cname}({cgold}G)+{g_sub}({g_sub_cost}G)"
        if rem >= 300:
            return f"开局1400G：{cname}({cgold}G)+草鞋(300G)"
        return f"开局1400G：{cname}({cgold}G)+药水，或{g_name}(950G)+草鞋"

    if budget >= 950 + g_sub_cost:
        return f"开局1400G：{g_name}(950G)+{g_sub}({g_sub_cost}G)"
    if budget >= 1250:
        return f"开局出门：{g_name}(950G)+草鞋(300G)"
    return f"开局出门：{g_name}(950G)"


def build_next_buy_plan(
    store: DataStore,
    hero_id: str | int,
    my_items: tuple[str, ...] | list[str],
    my_gold: float,
    dynamic_recs: list[DynamicItemRec],
    *,
    threat: MatchupThreatProfile | None = None,
    game_time: float = 0.0,
    selected_augments: tuple[str, ...] | list[str] = (),
) -> NextBuyPlan | None:
    """Distill ranked recommendations into ONE decisive Next Item (or Elixir when 6-slot full build)."""
    profile = _infer_hero_profile(store, str(hero_id), my_items, selected_augments)
    inv = analyze_inventory_slots(store, my_items, profile=profile)
    if inv.is_full_build:
        elx_cost = 500
        elx_aff = my_gold >= elx_cost
        elx_gap = max(0, elx_cost - int(my_gold))
        backup_swap: DynamicItemRec | None = None

        if inv.is_six_complete:
            reasons = ("六神装已满，默认磕合剂（除非有必换刚需）",)
            hint = (
                f"已满六神 · 优先磕【{inv.recommended_elixir_name}】(500G)"
                if elx_aff
                else f"已满六神 · 差 {elx_gap}G 磕【{inv.recommended_elixir_name}】(500G)"
            )
            fb_mode = "six_complete"
        else:
            # 5 completed major items + Tier-2 boots = 6 slots occupied (never recommend buying components!)
            fb_mode = "five_plus_boots"
            boots_sell = int(inv.tier2_boots_gold * 0.70)
            top_complete = next(
                (r for r in dynamic_recs if is_valid_plan_item(store, r.item_id)),
                None,
            )
            if top_complete is not None and (my_gold + boots_sell) >= top_complete.price:
                reasons = (f"5大件+{inv.tier2_boots_name}满格 · 磕合剂或卖鞋直换【{top_complete.name}】",)
                hint = f"先磕【{inv.recommended_elixir_name}】(500G)；钱够可卖【{inv.tier2_boots_name}】直换【{top_complete.name}】"
                backup_swap = top_complete
            else:
                reasons = (f"5大件+{inv.tier2_boots_name}已满6格，先磕合剂",)
                hint = (
                    f"已满6格 · 优先磕【{inv.recommended_elixir_name}】(500G)"
                    if elx_aff
                    else f"已满6格 · 差 {elx_gap}G 磕【{inv.recommended_elixir_name}】(500G)"
                )

        elx_target = DynamicItemRec(
            item_id=inv.recommended_elixir_id,
            name=inv.recommended_elixir_name,
            price=elx_cost,
            affordable=elx_aff,
            gold_needed=elx_gap,
            score=100.0,
            win_rate=None,
            games=0,
            reasons=reasons,
            tags=("六神满格", "合剂"),
        )
        return NextBuyPlan(
            target=elx_target,
            effective_cost=elx_cost,
            affordable_now=elx_aff,
            gold_gap=elx_gap,
            buy_now_hint=hint,
            remaining_build_path=(),
            remaining_build_ids=(),
            backup=backup_swap,
            is_full_build=True,
            full_build_mode=fb_mode,
        )

    if not dynamic_recs:
        return None
    target = dynamic_recs[0]
    backup = dynamic_recs[1] if len(dynamic_recs) > 1 else None

    effective_cost, affordable_now, gold_gap, buy_now_hint = compute_item_buy_now(
        store, target.item_id, my_items, my_gold
    )

    boots_id, boots_name, boots_reason = recommend_counter_boots(
        store, hero_id, my_items, selected_augments, threat
    )
    if boots_name and "升二级鞋" in buy_now_hint:
        buy_now_hint = buy_now_hint.replace("升二级鞋", f"升二级鞋·{boots_name}")

    opening_hint = compute_aram_opening_buy_hint(
        store, profile, target.item_id, my_items, my_gold, game_time
    )
    is_opening = False
    if opening_hint and not affordable_now:
        buy_now_hint = opening_hint
        is_opening = True

    # Build remaining full item path: ONLY real complete equipment items (never basic 800G components or ghost IDs!)
    path_ids: list[str] = []
    path_names: list[str] = []
    has_gw_in_path = any(
        store.get_item_info(i).has_grievous
        and (store.get_item_info(i).category == "complete_equipment" or store.get_item_info(i).gold >= 2200)
        for i in my_items
    )
    for rec in dynamic_recs[:6]:
        nid = canonicalize_item_id(rec.item_id)
        if is_valid_plan_item(store, nid) and rec.name not in path_names:
            info_rec = store.get_item_info(nid)
            if info_rec.has_grievous:
                if has_gw_in_path:
                    continue
                has_gw_in_path = True
            path_ids.append(nid)
            path_names.append(rec.name)
            if len(path_ids) >= 4:
                break
    # Pad from hero's top core build if fewer than 3 remaining items
    for cb in store.get_core_builds(hero_id)[:1]:
        for iid in cb.items:
            nid = canonicalize_item_id(iid)
            if not item_is_owned(nid, my_items) and is_valid_plan_item(store, nid):
                info_cb = store.get_item_info(nid)
                if info_cb.has_grievous and has_gw_in_path:
                    continue
                nm = info_cb.name
                if nm not in path_names:
                    if info_cb.has_grievous:
                        has_gw_in_path = True
                    path_ids.append(nid)
                    path_names.append(nm)
    return NextBuyPlan(
        target=target,
        effective_cost=effective_cost,
        affordable_now=affordable_now,
        gold_gap=gold_gap,
        buy_now_hint=buy_now_hint,
        remaining_build_path=tuple(path_names[:4]),
        remaining_build_ids=tuple(path_ids[:4]),
        backup=backup,
        recommended_boots_id=boots_id,
        recommended_boots_name=boots_name,
        recommended_boots_reason=boots_reason,
        is_opening_buy=is_opening,
    )


def reconcile_match_build_plan(
    store: DataStore,
    my_items: tuple[str, ...] | list[str],
    committed_plan: list[str] | tuple[str, ...],
    locked_next_item: str,
    proposed_ids: list[str] | tuple[str, ...],
    allow_overhaul: bool = False,
) -> tuple[str, list[str]]:
    """Maintain a coherent full-match build plan with strict anti-flip-flop hysteresis.

    Rules:
    0. Once the player hits 6-slot full build (6 complete items, or 5 complete + Tier-2 boots),
       clear any remaining next-item lock so no unprompted 7th item is ever pushed.
    1. Only real purchasable complete equipment items in current DDragon may enter the plan (zero ghost IDs / components).
    2. Completed items naturally drop from remaining plan and advance the lock to the next slot.
    3. If the player invests >= 800G of components into a different complete item (and < 400G into
       the old locked item), honor the player's actual shop choice immediately.
    4. Only allow full plan overhaul when `allow_overhaul=True` (initial plan lock or transformative
       augment like 迷人蝴蝶 / 装甲冲击 / 珠光护手 / 没法力) — never flip-flop between routine deaths!
    """
    if analyze_inventory_slots(store, my_items).is_full_build:
        return "", []

    clean_committed: list[str] = []
    for x in committed_plan:
        nx = canonicalize_item_id(x)
        if nx and nx not in clean_committed and not item_is_owned(nx, my_items) and is_valid_plan_item(store, nx):
            clean_committed.append(nx)

    clean_proposed: list[str] = []
    for x in proposed_ids:
        nx = canonicalize_item_id(x)
        if nx and nx not in clean_proposed and not item_is_owned(nx, my_items) and is_valid_plan_item(store, nx):
            clean_proposed.append(nx)

    cur_lock = canonicalize_item_id(locked_next_item) if locked_next_item else ""
    if cur_lock and (item_is_owned(cur_lock, my_items) or not is_valid_plan_item(store, cur_lock)):
        cur_lock = ""

    # Initial establishment or legitimate transformative-augment / augment-correction overhaul
    if allow_overhaul or not clean_committed:
        merged = list(clean_proposed[:5])
        if not allow_overhaul:
            for cid in clean_committed:
                if cid not in merged and len(merged) < 5:
                    merged.append(cid)
        new_lock = merged[0] if merged else ""
        return new_lock, merged

    # Check if player manually invested >= 800G in components of a different complete item in shop
    lock_credit = _owned_component_credit(store, cur_lock, list(my_items))[0] if cur_lock else 0
    if lock_credit < 400:
        best_inv_id = ""
        best_inv_gold = 0
        for pid in clean_proposed:
            if pid == cur_lock:
                continue
            cred = _owned_component_credit(store, pid, list(my_items))[0]
            if cred >= 800 and cred > best_inv_gold:
                best_inv_gold = cred
                best_inv_id = pid
        if best_inv_id:
            cur_lock = best_inv_id
            if best_inv_id in clean_committed:
                clean_committed.remove(best_inv_id)
            clean_committed.insert(0, best_inv_id)

    # Preserve locked_next_item and committed core slots; only pad open tail slots from proposed
    if cur_lock:
        if cur_lock in clean_committed:
            clean_committed.remove(cur_lock)
        clean_committed.insert(0, cur_lock)
    elif clean_committed:
        cur_lock = clean_committed[0]
    elif clean_proposed:
        cur_lock = clean_proposed[0]
        clean_committed.append(cur_lock)

    for pid in clean_proposed:
        if len(clean_committed) >= 5:
            break
        if pid not in clean_committed:
            clean_committed.append(pid)

    return cur_lock, clean_committed[:5]
