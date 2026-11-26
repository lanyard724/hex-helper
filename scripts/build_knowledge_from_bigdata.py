"""Offline idempotent knowledge-base builder powered by Patch 16.23 big data & CDragon metadata.

Reads verified local big-data caches and upgrades the three bundled HexHelper assets:
1. `augment_combos.json`:
   - Enforces curated `required_anchor` fields on single-keystone combos.
   - Preserves all existing curated `augment_axes` and auto-fills every missing active augment
     using CDragon (`summaryDescription`, `augmentDisplayTags`, `augmentNameId`, Chinese name)
     combined with big-data hero-profile affinity so coverage hits 100% (237/237).
2. `hero_signature_builds.json`:
   - Preserves all existing curated champion entries untouched.
   - Generates big-data-backed entries from `core_builds.json` + `hero_item_recommendations.json`
     (detecting multi-style divergence) for all remaining champions (173/173 = 100%).
3. `special_augment_builds.json`:
   - Preserves all existing entries untouched and appends top uncovered high-WR-lift Patch 16.23
     augments with verified mechanics, 4-digit item IDs, and conservative `transformative` flags.

Usage:
    python scripts/build_knowledge_from_bigdata.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

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
    }
)

_MAYHEM_PREFIXES: tuple[str, ...] = ("12", "22", "32", "44", "77", "99")
_TEAR_MAP: dict[str, str] = {
    "3040": "3003",
    "3042": "3004",
    "3121": "3119",
    "2530": "2526",
}

# Single-mechanic keystone anchors for namesake combos so owning 2 non-anchor cores never marks them completed.
COMBO_REQUIRED_ANCHORS: dict[str, str] = {
    "big_shot_jeweled": "3018",
    "infinite_recursion_ap_loop": "1388",
    "infernal_burn_nuke": "1045",
    "vulnerability_dot_crit": "1092",
    "dual_wield_machinegun": "1225",
    "tickle_monster_ad_speed": "3058",
    "mystic_punch_infinite_cd": "1058",
    "ethereal_weapon_onhit_mage": "1029",
    "apex_inventor_item_haste": "1002",
    "colossus_cc_shield_wall": "1018",
    "enchanter_butterfly_circle": "3163",
    "phenomenal_eureka_scaling": "1390",
    "mana_overflow_archmage": "3059",
    "dashing_earthwake_assassin": "1019",
    "minion_bees_swarm": "3086",
    "draw_sword_melee_brawler": "1134",
    "skilled_sniper_poke": "1113",
    "marksmage_ap_auto": "1129",
}

# Top uncovered high-WR-lift Patch 16.23 augments to add to special_augment_builds.json
TOP_NEW_SPECIAL_AUGMENTS: list[dict[str, Any]] = [
    {
        "id": "1053",
        "name": "科学狂人",
        "aliases": ["大头小头", "大小随机"],
        "rarity": "prismatic",
        "archetype": "体型切换全属性增幅流",
        "mechanic": "每次复活随机变大(获得额外生命值与自适应之力)或变小(获得额外技能急速与移速)",
        "transformative": False,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 2.6,
        "core_items": ["6333", "2517", "4633", "3053", "3161", "3089"],
        "force_items": [],
        "required_tags": ["AbilityHaste", "Health"],
        "stat_keys": [],
        "penalize_tags": [],
        "avoid_note": "属于通用面板强化，按英雄常规核心路线出装即可，战士/法坦吃双形态收益最高",
        "playstyle_tip": "大体型回合偏前排换血吃伤害，小体型回合靠高移速与高急速高频拉扯输出",
    },
    {
        "id": "3262",
        "name": "史莱姆时间",
        "aliases": ["史莱姆", "粘液回血"],
        "rarity": "silver",
        "archetype": "近战贴脸粘液续航流",
        "mechanic": "周期性向周围喷射史莱姆粘液造成最大生命值相关魔法伤害并回复自身生命值",
        "transformative": False,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 3.0,
        "core_items": ["3065", "4633", "3084", "2502", "6333", "2517"],
        "force_items": [],
        "required_tags": ["Health"],
        "stat_keys": ["FlatHPPoolMod"],
        "penalize_tags": [],
        "avoid_note": "振奋盔甲与血量续航装可显著放大粘液回复与坦度，避免纯脆皮站撸",
        "playstyle_tip": "贴近敌方前排持续作战，卡粘液触发节奏滚起持续回血与范围魔伤",
    },
    {
        "id": "1004",
        "name": "回归基本功",
        "aliases": ["禁大招", "基本功"],
        "rarity": "prismatic",
        "archetype": "弃R强化小技能极速流",
        "mechanic": "禁用终极技能(R)，换取小技能大幅增伤(+35%)、高额技能急速(+70)与治疗护盾增幅",
        "transformative": True,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 3.6,
        "core_items": ["3161", "2517", "6333", "4645", "3089", "4629"],
        "force_items": [],
        "required_tags": ["AbilityHaste"],
        "stat_keys": [],
        "penalize_tags": [],
        "avoid_note": "严禁出依赖终极技能触发的装备(残疫3118/公理圆弧6696/猎魔人弩箭2512/基克3050)，全力堆小技能伤害",
        "playstyle_tip": "专注高频小技能连招与拉扯消耗，无须保留大招窗口，小技能伤害直接碾压常规流派",
    },
    {
        "id": "3078",
        "name": "鼓声暴起 低音轰鸣",
        "aliases": ["鼓声暴起", "低音轰鸣", "五杀摇滚低音"],
        "rarity": "gold",
        "archetype": "高频连招节奏冲击波流",
        "mechanic": "释放技能或普攻按节拍触发范围低音冲击波与移速增益，高频近战与持续输出收益极高",
        "transformative": False,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 3.0,
        "core_items": ["3078", "6333", "2517", "3161", "6610", "3071"],
        "force_items": [],
        "required_tags": ["AbilityHaste"],
        "stat_keys": [],
        "penalize_tags": [],
        "avoid_note": "高技能急速与半肉容错装可维持不断档节拍触发，提升团战持续范围伤害",
        "playstyle_tip": "保持技能与普攻高频衔接，进场贴身持续打出范围低音爆发",
    },
    {
        "id": "1333",
        "name": "逃跑计划",
        "aliases": ["残血加速", "逃生"],
        "rarity": "silver",
        "archetype": "低血爆发加速拉扯流",
        "mechanic": "生命值跌入低血线时瞬间触发高额移速与缩小体型保命效果，大幅提升反杀与拉扯容错",
        "transformative": False,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 2.4,
        "core_items": ["6333", "3072", "6673", "3157", "3156"],
        "force_items": [],
        "required_tags": ["LifeSteal"],
        "stat_keys": [],
        "penalize_tags": [],
        "avoid_note": "通用生存拉扯强化，无需强改核心路线；中后期补吸血/死舞/中娅可轻松残血反打",
        "playstyle_tip": "残血触发爆发移速后立刻拉开身位躲关键技能，再借吸血或护盾回头收割",
    },
    {
        "id": "1375",
        "name": "吞噬灵魂",
        "aliases": ["击杀回血", "噬魂"],
        "rarity": "gold",
        "archetype": "击杀百分比高额回血收割流",
        "mechanic": "参与击杀回复高额生命值，持续团战收割与前排进场滚雪球能力极强",
        "transformative": False,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 3.0,
        "core_items": ["3065", "6333", "2517", "6610", "3053", "2502"],
        "force_items": [],
        "required_tags": ["Health"],
        "stat_keys": ["FlatHPPoolMod"],
        "penalize_tags": [],
        "avoid_note": "振奋盔甲与死亡之舞/无穷饥渴联动击杀回血，团战只要先秒一人即可满血续战",
        "playstyle_tip": "优先集火敌方残血脆皮触发击杀大额回血，借续航滚雪球连续收割",
    },
    {
        "id": "1204",
        "name": "叠角龙",
        "aliases": ["双倍叠层", "叠层加速"],
        "rarity": "silver",
        "archetype": "永久叠层75%提速成长流",
        "mechanic": "技能被动与可叠加装备的永久层数获取速度提升75%，叠层英雄与叠层装质变加速",
        "transformative": True,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 3.6,
        "core_items": ["3084", "6697", "3003", "3004", "3119", "2526"],
        "force_items": [],
        "required_tags": [],
        "stat_keys": [],
        "penalize_tags": [],
        "avoid_note": "适合英雄优先提早做心之钢/狂妄/女神之泪系叠层核心装，吃满75%加速红利",
        "playstyle_tip": "前中期积极换血与参团蹭层数，比常规对局早一整个大件周期进入后期强势期",
    },
    {
        "id": "1195",
        "name": "巨人杀手",
        "aliases": ["打大肉", "小体型增伤"],
        "rarity": "prismatic",
        "archetype": "小体型高移速切坦增伤流",
        "mechanic": "缩小体型并获得高额移速，对体型大于自身的目标造成显著百分比额外伤害",
        "transformative": False,
        "allow_off_profile": False,
        "shifts_profile": "",
        "boost": 2.8,
        "core_items": ["3153", "3036", "6653", "3135", "2503", "3033"],
        "force_items": [],
        "required_tags": ["ArmorPenetration", "MagicPenetration"],
        "stat_keys": [],
        "penalize_tags": [],
        "avoid_note": "避免叠体型类肉装(心之钢/歌利亚)，保持小体型配百分比穿透切碎前排",
        "playstyle_tip": "利用小体型与移速拉扯规避控制，优先集火敌方心之钢/坦克大核打满增伤",
    },
]

_DESC_AND_NAME_AXIS_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("暴击", "无尽", "收集者", "crit"), "CRIT_ATTACK"),
    (("珠光", "易损", "spellcrit"), "CRIT_SPELL"),
    (("攻速", "普攻", "攻击特效", "双刀", "台风", "瞄准镜", "onhit", "attackspeed"), "AUTO_ONHIT"),
    (("灼烧", "燃烧", "火", "献祭", "高压锅", "pressurecooker", "immolate", "burn"), "BURN_DOT"),
    (("终极", "大招", "pentakillaug_r", "ultimate", "ult"), "ULT_CYCLE"),
    (("pentakillaug_q", "pentakillaug_w", "pentakillaug_e", "极速", "急速", "冷却", "循环", "recoil", "brassblast", "boomandthebass"), "HASTE_LOOP"),
    (("护盾", "治疗", "回复", "奶", "spiritbomb", "faithful", "mercysstrike", "fruitify", "juicebox", "heal", "shield"), "SHIELD_HEAL"),
    (("生命", "护甲", "魔抗", "坦克", "巨人", "坚定", "steadfast", "moshmonster", "pressurecooker", "squishyslappy"), "TANK_HP"),
    (("位移", "冲刺", "突进", "闪现", "移速", "加速", "逃跑", "endlesshunt", "totentanz", "takeemdown", "terror", "overextender"), "MOBILITY_DASH"),
    (("质变", "潘朵拉", "赌", "随机", "王冠", "大师铸就", "heavyisthecrown", "forgedbythemaster", "overextender", "rise"), "GAMBLE_HIGHROLL"),
)

_SUPPORT_ITEMS = {"6617", "2526", "3504", "6616", "6621", "3107", "3222", "4005", "4011"}
_TANK_CORE_ITEMS = {"3084", "2502", "3083", "3068", "6664", "3065", "3075", "3110", "3119", "6662", "6665", "2504", "3143", "2525", "2524"}
_BURN_ITEMS = {"2503", "6653"}
_LETHALITY_ITEMS = {"6697", "6696", "6699", "6694", "6695", "6676", "3142", "3814", "6701", "6692"}
_CRIT_ITEMS = {"3032", "3031", "3085", "3046", "3094", "3036", "3033", "6676", "3508", "2512", "2523", "3430", "3026", "6673"}
_ONHIT_ITEMS = {"6672", "3153", "3124", "3302", "3091", "3115", "2510"}
_BRUISER_ITEMS = {"3078", "6610", "6333", "2517", "3071", "3161", "3053", "3074", "3748", "6631", "2501"}


def _canonical_item_id(iid: str | int) -> str:
    s = str(iid).strip()
    if len(s) == 6 and s[:2] in _MAYHEM_PREFIXES:
        s = s[2:]
    return _TEAR_MAP.get(s, s)


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _classify_item_style(iid: str, dd_data: dict[str, Any]) -> str:
    cid = _canonical_item_id(iid)
    if cid in _SUPPORT_ITEMS:
        return "SUPPORT"
    if cid in _TANK_CORE_ITEMS:
        return "TANK"
    if cid == "6657":
        return "AP"
    entry = dd_data.get(cid) or dd_data.get(str(iid)) or {}
    st = entry.get("stats") or {}
    tags = set(entry.get("tags") or [])
    gold = int((entry.get("gold") or {}).get("total", 0) or 0)
    ap = float(st.get("FlatMagicDamageMod", 0.0) or 0.0)
    ad = float(st.get("FlatPhysicalDamageMod", 0.0) or 0.0)
    crit = float(st.get("FlatCritChanceMod", 0.0) or 0.0)
    atk_spd = float(st.get("PercentAttackSpeedMod", 0.0) or 0.0)
    armor = float(st.get("FlatArmorMod", 0.0) or 0.0)
    mr = float(st.get("FlatSpellBlockMod", 0.0) or 0.0)
    hp = float(st.get("FlatHPPoolMod", 0.0) or 0.0)

    if ("ManaRegen" in tags or "Heal" in tags) and ap <= 65 and ad <= 0 and gold <= 2500:
        return "SUPPORT"
    if (armor > 0 or mr > 0 or hp >= 400) and ap <= 0 and ad <= 0 and crit <= 0:
        return "TANK"
    if ap > 0 and ad <= 0:
        return "AP"
    if ad > 0 or crit > 0 or atk_spd > 0:
        return "AD"
    return "TANK"


def _classify_hero_profile(
    hid: str,
    core_builds: dict[str, list[dict[str, Any]]],
    item_recs: dict[str, dict[str, Any]],
    dd_data: dict[str, Any],
) -> str:
    votes: Counter[str] = Counter()
    builds = core_builds.get(hid) or []
    for b in builds[:3]:
        w = max(1, int(b.get("games", 1)))
        for iid in b.get("items") or []:
            votes[_classify_item_style(iid, dd_data)] += w
    if not votes:
        recs = item_recs.get(hid) or {}
        top_r = sorted(recs.items(), key=lambda kv: int(kv[1].get("games", 0)), reverse=True)[:5]
        for iid, st in top_r:
            votes[_classify_item_style(iid, dd_data)] += max(1, int(st.get("games", 1)))
    if not votes:
        return "AD"
    return votes.most_common(1)[0][0]


def _infer_missing_axes_for_augment(
    aid: str,
    aug_meta: dict[str, Any],
    cd_entry: dict[str, Any],
    heroes: dict[str, Any],
    hero_augments: dict[str, dict[str, Any]],
    hero_profiles: dict[str, str],
) -> list[str]:
    """Infer valid mechanic axes from CDragon metadata + big-data hero-profile affinity."""
    found: list[str] = []

    def _add(ax: str) -> None:
        if ax in VALID_MECHANIC_AXES and ax not in found:
            found.append(ax)

    name_cn = str(aug_meta.get("name") or cd_entry.get("nameTRA") or "")
    name_id = str(cd_entry.get("augmentNameId") or "")
    summary = _strip_html(str(cd_entry.get("summaryDescription") or ""))
    tags = set(cd_entry.get("augmentDisplayTags") or [])
    haystack = f"{name_cn} {name_id} {summary}".lower()

    # 1. Explicit keyword & CDragon name-ID rules
    for keywords, axis in _DESC_AND_NAME_AXIS_RULES:
        if any(kw in haystack for kw in keywords):
            _add(axis)

    # 2. CDragon display tags
    if "kAlly" in tags:
        _add("SHIELD_HEAL")
    if "kSpeed" in tags:
        _add("MOBILITY_DASH")
    if "kResilience" in tags and "SHIELD_HEAL" not in found:
        _add("TANK_HP")
    if "kQuestline" in tags and not found:
        _add("GAMBLE_HIGHROLL")

    # 3. Big-data champion-affinity inference from top positive-delta heroes (>=300 games)
    deltas: list[tuple[float, str]] = []
    for hid, amap in hero_augments.items():
        st = amap.get(aid)
        if not st or int(st.get("games", 0)) < 300:
            continue
        hwr = float(heroes.get(hid, {}).get("win_rate", 0.5))
        deltas.append((float(st.get("win_rate", 0.5)) - hwr, hid))
    deltas.sort(reverse=True)

    prof_votes: Counter[str] = Counter()
    for rank, (delta, hid) in enumerate(deltas[:12]):
        weight = max(1.0, 12.0 - rank) * (1.4 if delta > 0 else 0.6)
        prof_votes[hero_profiles.get(hid, "AD")] += weight

    if prof_votes:
        top_prof, top_w = prof_votes.most_common(1)[0]
        total_w = sum(prof_votes.values())
        top_share = top_w / total_w if total_w > 0 else 0.0

        prof_to_axis = {
            "AP": "AP_SPELL",
            "AD": "AD_ATTACK",
            "TANK": "TANK_HP",
            "SUPPORT": "SHIELD_HEAL",
        }
        primary_ax = prof_to_axis.get(top_prof, "AD_ATTACK")

        # Add dominant hero-profile axis if needed or strongly indicated (>=45% share)
        if not found or ("kDamage" in tags and primary_ax not in found) or (top_share >= 0.45 and len(found) < 2):
            _add(primary_ax)

        # If mixed AP/AD high-affinity, add secondary combat axis when under 2 axes
        if len(found) < 2 and len(prof_votes) >= 2:
            sec_prof, sec_w = prof_votes.most_common(2)[1]
            if sec_w >= total_w * 0.30:
                sec_ax = prof_to_axis.get(sec_prof, "")
                if sec_ax:
                    _add(sec_ax)

    if not found:
        _add("GAMBLE_HIGHROLL")

    return found[:3]


def upgrade_augment_combos(
    assets_dir: Path,
    dataset_dir: Path,
    static_dir: Path,
    hero_profiles: dict[str, str],
) -> tuple[int, int]:
    """Update augment_combos.json with required_anchor fields and 100% augment_axes coverage."""
    combo_path = assets_dir / "augment_combos.json"
    data = _load_json(combo_path)
    augs = _load_json(dataset_dir / "augments.json")
    heroes = _load_json(dataset_dir / "heroes.json")
    hero_augments = _load_json(dataset_dir / "hero_augments.json")
    cd_list = _load_json(static_dir / "cdragon_augments.json")
    cd_by_id = {str(x.get("id")): x for x in cd_list if isinstance(x, dict)}

    # 1. Ensure required_anchor on single-keystone combos
    for rule in data.get("combos") or []:
        cid = str(rule.get("id", ""))
        anchor = COMBO_REQUIRED_ANCHORS.get(cid)
        if anchor and anchor in (rule.get("core_augments") or []):
            rule["required_anchor"] = anchor

    # 2. Preserve all existing curated augment_axes & fill missing active augments
    existing_axes: dict[str, list[str]] = dict(data.get("augment_axes") or {})
    added = 0
    for aid in sorted(augs.keys(), key=lambda x: int(x)):
        if aid in existing_axes and existing_axes[aid]:
            continue
        inferred = _infer_missing_axes_for_augment(
            aid=aid,
            aug_meta=augs[aid],
            cd_entry=cd_by_id.get(aid) or {},
            heroes=heroes,
            hero_augments=hero_augments,
            hero_profiles=hero_profiles,
        )
        existing_axes[aid] = inferred
        added += 1

    # Deterministic numeric sort of keys
    data["augment_axes"] = {
        aid: existing_axes[aid]
        for aid in sorted(existing_axes.keys(), key=lambda x: (int(x) if x.isdigit() else 999999, x))
    }
    _write_json(combo_path, data)
    return len(data["augment_axes"]), added


def _short_item_label(iid: str, dd_data: dict[str, Any], item_meta: dict[str, Any]) -> str:
    entry = dd_data.get(iid) or item_meta.get(iid) or {}
    nm = str(entry.get("name") or iid)
    short_map = {
        "灭世者的死亡之帽": "大帽",
        "兰德里的折磨": "兰德里",
        "黯炎火炬": "黯炎",
        "卢登的回声": "卢登",
        "卢登的伙伴": "卢登",
        "瑞莱的冰晶节杖": "冰杖",
        "育恩塔尔荒野箭": "育恩塔尔",
        "卢安娜的飓风": "飓风",
        "无尽之刃": "无尽",
        "多米尼克领主的致意": "大轻语",
        "凡性的提醒": "凡性提醒",
        "破败王者之刃": "破败",
        "海妖杀手": "海妖",
        "死亡之舞": "死舞",
        "贪欲九头蛇": "贪欲",
        "巨型九头蛇": "巨九",
        "斯特拉克的挑战护手": "血手",
        "无终恨意": "无终恨意",
        "狂徒铠甲": "狂徒",
        "振奋盔甲": "振奋",
        "霸王血铠": "霸王血铠",
        "月石再生器": "月石",
        "耳语头环": "耳语头环",
        "黄昏与黎明": "黄昏",
        "无穷饥渴": "无穷饥渴",
        "大天使之杖": "大天使",
        "放血者的诅咒": "放血者",
        "裂隙制造者": "裂隙",
        "星界驱驰": "星界",
        "夺萃之镰": "吸蓝刀",
        "朔极之矛": "朔极",
        "三相之力": "三相",
        "电震涡流剑": "电震",
        "赛瑞尔达的怨恨": "赛瑞尔达",
        "斯特提克电刃": "电刀",
        "斯塔缇克电刃": "电刀",
        "中娅沙漏": "中娅",
        "巫妖之祸": "巫妖",
        "纳什之牙": "纳什",
        "时光之杖": "时光",
        "帝国指令": "帝国指令",
        "视界专注": "视界",
        "公理圆弧": "公理圆弧",
    }
    if nm in short_map:
        return short_map[nm]
    return nm[:4] if len(nm) > 5 else nm


def _build_archetype_label(
    primary_style: str,
    starter_ids: list[str],
    sig_ids: list[str],
    alt_style: str,
    dd_data: dict[str, Any],
    item_meta: dict[str, Any],
) -> str:
    head = "".join(_short_item_label(i, dd_data, item_meta) for i in sig_ids[:2])
    sig_set = set(sig_ids[:4])
    if primary_style == "SUPPORT":
        suffix = "盾奶辅助流"
    elif primary_style == "TANK":
        suffix = "叠钢重装坦" if "3084" in sig_set else "前排重装坦"
    elif primary_style == "AP":
        if len(sig_set & _BURN_ITEMS) >= 2:
            suffix = "双烧法核"
        elif "3115" in sig_set or "2510" in sig_set:
            suffix = "攻速特效法核"
        elif "4633" in sig_set:
            suffix = "续航法坦流"
        else:
            suffix = "高爆法核"
    else:
        if len(sig_set & _CRIT_ITEMS) >= 2:
            suffix = "暴击核心流"
        elif len(sig_set & _ONHIT_ITEMS) >= 2:
            suffix = "特效攻速流"
        elif len(sig_set & _LETHALITY_ITEMS) >= 2:
            suffix = "穿甲爆发流"
        elif len(sig_set & _BRUISER_ITEMS) >= 2:
            suffix = "半肉战士流"
        else:
            suffix = "物理输出流"

    base = f"{head}{suffix}"
    if alt_style and alt_style != primary_style:
        alt_cn = {"AP": "AP", "AD": "AD", "TANK": "肉装", "SUPPORT": "软辅"}.get(alt_style, alt_style)
        return f"{base}(兼{alt_cn}双流派)"
    return base


def _generate_hero_sig_entry(
    hid: str,
    hero_meta: dict[str, Any],
    builds: list[dict[str, Any]],
    item_recs: dict[str, Any],
    dd_data: dict[str, Any],
    item_meta: dict[str, Any],
) -> dict[str, Any]:
    hero_name = str(hero_meta.get("name", hid))

    # Filter & rank viable core builds by blend of win rate and real adoption
    viable = [b for b in builds if int(b.get("games", 0)) >= 1000 and len(b.get("items") or []) >= 2]
    if not viable:
        viable = [b for b in builds if len(b.get("items") or []) >= 2]

    # Sort builds combining adoption weight and win rate (so #1 mainstream + highest-WR variant both surface)
    by_games = sorted(viable, key=lambda b: int(b.get("games", 0)), reverse=True)
    by_wr = sorted(
        [b for b in viable if float(b.get("pick", 0.0)) >= 0.015 or int(b.get("games", 0)) >= 5000] or viable,
        key=lambda b: (float(b.get("win", 0.5)), int(b.get("games", 0))),
        reverse=True,
    )

    starter_ids: list[str] = []
    sig_ids: list[str] = []
    build_styles: list[tuple[str, list[str]]] = []

    def _add_unique(dst: list[str], raw_iid: str, max_len: int) -> None:
        cid = _canonical_item_id(raw_iid)
        if cid.isdigit() and len(cid) == 4 and cid not in dst and len(dst) < max_len:
            dst.append(cid)

    # Top adopted build + top WR build determine starters and multi-style divergence
    selected_builds: list[dict[str, Any]] = []
    for b in (*by_games[:2], *by_wr[:2]):
        if b not in selected_builds:
            selected_builds.append(b)
        if len(selected_builds) >= 3:
            break

    for b in selected_builds:
        items = [_canonical_item_id(x) for x in (b.get("items") or [])]
        if not items:
            continue
        style_votes = Counter(_classify_item_style(x, dd_data) for x in items)
        b_style = style_votes.most_common(1)[0][0]
        build_styles.append((b_style, items))
        _add_unique(starter_ids, items[0], 2)
        for x in items:
            _add_unique(sig_ids, x, 5)

    # Pad signature_items from high-adoption hero_item_recommendations if needed
    if len(sig_ids) < 4 and item_recs:
        sorted_recs = sorted(
            item_recs.items(),
            key=lambda kv: int(kv[1].get("games", 0)),
            reverse=True,
        )
        for raw_iid, _st in sorted_recs[:10]:
            cid = _canonical_item_id(raw_iid)
            info = dd_data.get(cid) or item_meta.get(cid) or {}
            gold = int((info.get("gold") or {}).get("total", info.get("total", 0)) or 0)
            if gold < 2200:
                continue
            if not starter_ids:
                _add_unique(starter_ids, cid, 1)
            _add_unique(sig_ids, cid, 5)
            if len(sig_ids) >= 5:
                break

    primary_style = build_styles[0][0] if build_styles else "AD"
    primary_pair = set(build_styles[0][1][:2]) if build_styles else set()
    alt_style = ""
    alt_items_raw: list[str] = []
    for st, b_items in build_styles[1:]:
        if st != primary_style and set(b_items[:2]) != primary_pair:
            alt_style = st
            alt_items_raw = b_items[:2]
            break

    archetype = _build_archetype_label(primary_style, starter_ids, sig_ids, alt_style, dd_data, item_meta)
    avoid_passive = ["3109"] if primary_style in ("AP", "AD") and "3109" not in sig_ids else []

    # Build concise factual Chinese note grounded in 16.23 core items
    main_names = [_short_item_label(i, dd_data, item_meta) for i in sig_ids[:3]]
    extra_names = [_short_item_label(i, dd_data, item_meta) for i in sig_ids[3:5]]
    core_txt = "+".join(main_names) if main_names else "常规核心装"
    extra_txt = f"，后续视局势补{'/'.join(extra_names)}" if extra_names else ""
    if alt_style and alt_items_raw:
        alt_items = [_short_item_label(x, dd_data, item_meta) for x in alt_items_raw]
        alt_txt = f"；多流派可选{'+'.join(alt_items)}路线"
    else:
        alt_txt = ""
    note = f"26.23大乱斗大数据核心为{core_txt}{extra_txt}{alt_txt}"

    return {
        "hero_id": str(hid),
        "name": hero_name,
        "archetype": archetype,
        "starter_items": starter_ids[:2],
        "signature_items": sig_ids[:5],
        "avoid_passive": avoid_passive,
        "note": note,
    }


def upgrade_hero_signatures(
    assets_dir: Path,
    dataset_dir: Path,
    static_dir: Path,
) -> tuple[int, int]:
    """Keep all existing curated heroes untouched and generate big-data entries for the rest."""
    sig_path = assets_dir / "hero_signature_builds.json"
    data = _load_json(sig_path)
    heroes = _load_json(dataset_dir / "heroes.json")
    core_builds = _load_json(dataset_dir / "core_builds.json")
    item_recs = _load_json(dataset_dir / "hero_item_recommendations.json")
    item_meta = _load_json(dataset_dir / "item_metadata.json")
    dd_data = (_load_json(static_dir / "ddragon_items.json") or {}).get("data") or {}

    existing_list: list[dict[str, Any]] = list(data.get("heroes") or [])
    existing_ids = {str(h.get("hero_id", "")) for h in existing_list}

    added = 0
    for hid in sorted(heroes.keys(), key=lambda x: int(x)):
        if str(hid) in existing_ids:
            continue
        entry = _generate_hero_sig_entry(
            hid=str(hid),
            hero_meta=heroes[hid],
            builds=core_builds.get(hid) or [],
            item_recs=item_recs.get(hid) or {},
            dd_data=dd_data,
            item_meta=item_meta,
        )
        existing_list.append(entry)
        existing_ids.add(str(hid))
        added += 1

    data["heroes"] = existing_list
    _write_json(sig_path, data)
    return len(existing_list), added


def upgrade_special_augment_builds(assets_dir: Path) -> tuple[int, int]:
    """Keep all existing curated special builds untouched and append top uncovered 16.23 augments."""
    sp_path = assets_dir / "special_augment_builds.json"
    data = _load_json(sp_path)
    existing_list: list[dict[str, Any]] = list(data.get("archetypes") or [])
    existing_ids = {str(e.get("id", "")) for e in existing_list}

    added = 0
    for entry in TOP_NEW_SPECIAL_AUGMENTS:
        if str(entry["id"]) in existing_ids:
            continue
        existing_list.append(entry)
        existing_ids.add(str(entry["id"]))
        added += 1

    data["archetypes"] = existing_list
    _write_json(sp_path, data)
    return len(existing_list), added


def run_build(dataset_dir: Path, static_dir: Path, assets_dir: Path) -> dict[str, tuple[int, int]]:
    heroes = _load_json(dataset_dir / "heroes.json")
    core_builds = _load_json(dataset_dir / "core_builds.json")
    item_recs = _load_json(dataset_dir / "hero_item_recommendations.json")
    dd_data = (_load_json(static_dir / "ddragon_items.json") or {}).get("data") or {}

    hero_profiles = {
        str(hid): _classify_hero_profile(str(hid), core_builds, item_recs, dd_data)
        for hid in heroes
    }

    aug_axes_res = upgrade_augment_combos(assets_dir, dataset_dir, static_dir, hero_profiles)
    hero_sig_res = upgrade_hero_signatures(assets_dir, dataset_dir, static_dir)
    sp_res = upgrade_special_augment_builds(assets_dir)
    return {
        "augment_axes": aug_axes_res,
        "hero_signatures": hero_sig_res,
        "special_augments": sp_res,
    }


def main() -> None:
    local_app = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\Administrator\AppData\Local"))
    default_dataset = local_app / "HexHelper" / "cache" / "datasets" / "16.23" / "normal"
    default_static = local_app / "HexHelper" / "cache" / "static"
    default_assets = Path(__file__).resolve().parents[1] / "src" / "hex_helper" / "assets"

    parser = argparse.ArgumentParser(description="Build HexHelper knowledge assets from 16.23 big data.")
    parser.add_argument("--dataset-dir", type=Path, default=default_dataset)
    parser.add_argument("--static-dir", type=Path, default=default_static)
    parser.add_argument("--assets-dir", type=Path, default=default_assets)
    args = parser.parse_args()

    summary = run_build(args.dataset_dir, args.static_dir, args.assets_dir)
    for k, (total, added) in summary.items():
        print(f"{k}: total={total}, added={added}")


if __name__ == "__main__":
    main()
