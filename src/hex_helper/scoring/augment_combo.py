"""0ms Local Augment-to-Augment Synergy, High-Ceiling Gamble & Anti-Synergy Downgrade Engine.

Completely independent from Bayesian win-rate tier scoring (scoring/tier.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from hex_helper.data.augment_combos import (
    VALID_MECHANIC_AXES,
    AugmentComboRule,
    get_augment_combo_db,
)
from hex_helper.data.provider import AugmentInfo
from hex_helper.data.special_builds import get_special_augment_db
from hex_helper.data.store import DataStore

AXIS_LABELS: dict[str, str] = {
    "AP_SPELL": "法术技能",
    "AD_ATTACK": "物理攻击",
    "AUTO_ONHIT": "普攻/攻速特效",
    "CRIT_ATTACK": "物理暴击",
    "CRIT_SPELL": "技能暴击",
    "HASTE_LOOP": "技能循环极速",
    "BURN_DOT": "持续灼烧",
    "TANK_HP": "坦克生命",
    "SHIELD_HEAL": "盾奶辅助",
    "ULT_CYCLE": "大招循环",
    "MOBILITY_DASH": "位移机动",
    "GAMBLE_HIGHROLL": "高上限赌狗",
}

_HYBRID_BRIDGE_AUGMENT_IDS: frozenset[str] = frozenset({"1054", "1029", "1129", "1205", "1206"})
_META_NON_COMBAT_AXES: frozenset[str] = frozenset({"GAMBLE_HIGHROLL", "MOBILITY_DASH"})

_RARITY_PRIORITY: dict[str, float] = {
    "prismatic": 1.2,
    "gold": 0.7,
    "silver": 0.3,
}

_NAME_AXIS_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("暴击", "无尽", "收集者"), "CRIT_ATTACK"),
    (("珠光", "易损"), "CRIT_SPELL"),
    (("攻速", "射击", "双刀", "台风", "瞄准镜", "灵巧", "九头蛇", "扳机"), "AUTO_ONHIT"),
    (("大力", "物理", "焚天", "死亡之舞", "斩杀", "耀光"), "AD_ATTACK"),
    (("魔法", "法强", "巫师", "法力", "奥术", "风暴", "咒语", "飞弹", "法师"), "AP_SPELL"),
    (("循环", "急速", "尤里卡", "面包", "冲拳"), "HASTE_LOOP"),
    (("炼狱", "灼烧", "火", "侵蚀", "献祭", "蜜蜂"), "BURN_DOT"),
    (("坦克", "巨人", "钢化", "生命", "护甲", "巨像", "不动如山", "重量级", "坚不可破"), "TANK_HP"),
    (("护盾", "治疗", "急救", "屏障", "风语者", "蝴蝶", "死亡之环", "全心为你", "治愈", "拯救"), "SHIELD_HEAL"),
    (("终极", "大招", "杀戮时间", "精怪魔法"), "ULT_CYCLE"),
    (("身法", "闪现", "疾奔", "雪球", "逃跑", "快步", "练腿"), "MOBILITY_DASH"),
    (("质变", "潘朵拉", "属性", "捐赠", "夺金", "命运之武器", "大腕"), "GAMBLE_HIGHROLL"),
)


@dataclass(frozen=True)
class MatchBuildProfile:
    primary_profile: str  # "AP" | "AD" | "CRIT_SPELL" | "TANK" | "SUPPORT" | "HYBRID" | "BALANCED"
    direction_label: str
    favored_axes: tuple[str, ...]
    penalized_axes: tuple[str, ...]
    penalized_labels: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ActiveComboProgress:
    combo_id: str
    name: str
    gamble: bool
    owned_ids: tuple[str, ...]
    missing_core_ids: tuple[str, ...]
    missing_core_names: tuple[str, ...]
    completed: bool
    synergy_reason: str
    reroll_hint: str


@dataclass(frozen=True)
class ComboFollowUp:
    augment_id: str
    name: str
    rarity: str
    combo_id: str
    combo_name: str
    reason: str
    gamble: bool
    priority_score: float
    hero_signature: bool


@dataclass(frozen=True)
class ComboPlan:
    hero_id: str | None
    hero_name: str
    profile: MatchBuildProfile
    active_combos: tuple[ActiveComboProgress, ...]
    recommended_targets: tuple[ComboFollowUp, ...]
    opening_combos: tuple[ActiveComboProgress, ...]
    avoid_summary: str
    round_index: int = 1


@dataclass(frozen=True)
class PickerCardComboAdvice:
    slot_index: int
    augment_id: str
    name: str
    rarity: str
    verdict: str  # "jackpot" | "gamble" | "synergy" | "neutral" | "conflict"
    verdict_label: str
    combo_delta: float
    reason: str
    combo_name: str
    gamble: bool


@dataclass(frozen=True)
class PickerComboVerdict:
    profile: MatchBuildProfile
    cards: tuple[PickerCardComboAdvice, ...]
    best_slot_index: int
    reroll_advice: str
    round_index: int = 1


def _aug_name_rarity(store: DataStore | None, augment_id: str) -> tuple[str, str]:
    aid = str(augment_id)
    if store is not None:
        info = store.get_augment(aid)
        if info is not None:
            return info.name, info.rarity
    sp = get_special_augment_db().get_by_id(aid)
    if sp is not None:
        return sp.name, sp.rarity
    return f"海克斯#{aid}", "gold"


def infer_augment_axes(store: DataStore | None, augment_id: str) -> tuple[str, ...]:
    """Return mechanic axes for any augment (explicit DB -> special builds -> name keywords)."""
    aid = str(augment_id).strip()
    if not aid:
        return ()

    db = get_augment_combo_db()
    explicit = db.get_axes(aid)
    if explicit:
        return explicit

    found: list[str] = []

    def _add(ax: str) -> None:
        if ax in VALID_MECHANIC_AXES and ax not in found:
            found.append(ax)

    sp = get_special_augment_db().resolve(store, aid)
    if sp is not None:
        prof = sp.shifts_profile.upper()
        if prof == "AP":
            _add("AP_SPELL")
        elif prof == "AD":
            _add("AD_ATTACK")
        elif prof == "TANK":
            _add("TANK_HP")
        req = set(sp.required_tags)
        if "SpellDamage" in req:
            _add("AP_SPELL")
        if "Damage" in req:
            _add("AD_ATTACK")
        if "AttackSpeed" in req or "OnHit" in req:
            _add("AUTO_ONHIT")
        if "CriticalStrike" in req:
            _add("CRIT_ATTACK")
        if "Health" in req or "Armor" in req or "SpellBlock" in req:
            _add("TANK_HP")
        if "ManaRegen" in req:
            _add("SHIELD_HEAL")

    name, _ = _aug_name_rarity(store, aid)
    for keywords, axis in _NAME_AXIS_KEYWORDS:
        if any(kw in name for kw in keywords):
            _add(axis)

    return tuple(found)


def infer_match_build_profile(
    store: DataStore | None,
    hero_id: str | None,
    selected_augments: Sequence[str],
    my_items: Sequence[str],
) -> MatchBuildProfile:
    """Infer live match build direction and anti-synergy axes from owned augments + inventory + hero."""
    db = get_augment_combo_db()
    sp_db = get_special_augment_db()
    owned_ids = [str(a).strip() for a in selected_augments if str(a).strip()]
    owned_set = set(owned_ids)
    hid = str(hero_id).strip() if hero_id else ""
    has_hybrid_bridge = bool(owned_set & _HYBRID_BRIDGE_AUGMENT_IDS)

    axis_scores: dict[str, float] = {}
    profile_scores: dict[str, float] = {
        "AP": 0.0,
        "AD": 0.0,
        "CRIT_SPELL": 0.0,
        "TANK": 0.0,
        "SUPPORT": 0.0,
    }
    anti_votes: dict[str, float] = {}
    evidence: list[str] = []

    def _bump_axis(ax: str, weight: float) -> None:
        axis_scores[ax] = axis_scores.get(ax, 0.0) + weight

    # 1. Strongest signal: already selected augments
    for aid in owned_ids:
        axes = infer_augment_axes(store, aid)
        nm, _ = _aug_name_rarity(store, aid)
        for ax in axes:
            _bump_axis(ax, 2.4)
            if ax in ("AP_SPELL", "BURN_DOT"):
                profile_scores["AP"] += 2.2
            elif ax == "HASTE_LOOP":
                profile_scores["AP"] += 1.4
            elif ax in ("AD_ATTACK", "AUTO_ONHIT", "CRIT_ATTACK"):
                profile_scores["AD"] += 2.1
            elif ax == "CRIT_SPELL":
                profile_scores["CRIT_SPELL"] += 2.8
            elif ax == "TANK_HP":
                profile_scores["TANK"] += 2.4
            elif ax == "SHIELD_HEAL":
                profile_scores["SUPPORT"] += 2.6

        sp = sp_db.resolve(store, aid)
        if sp is not None and sp.shifts_profile:
            sp_prof = sp.shifts_profile.upper()
            if sp_prof in profile_scores:
                profile_scores[sp_prof] += 3.8
                evidence.append(f"已选【{nm}】锁定 {sp_prof}")

        for rule in db.combos_for_augment(aid):
            if aid in rule.core_augments:
                h_match = bool(hid and hid in rule.hero_bonus)
                bonus = 3.2 if h_match else 2.0
                if rule.profile in profile_scores:
                    profile_scores[rule.profile] += bonus
                for anti_ax in rule.anti_axes:
                    anti_votes[anti_ax] = anti_votes.get(anti_ax, 0.0) + (2.6 if h_match else 1.7)
                if len(evidence) < 3 and nm not in " ".join(evidence):
                    evidence.append(f"已选【{nm}】触发「{rule.name}」")

    # 2. Live inventory signal (detects AP Miss Fortune / AP Kai'Sa / Tank pivots immediately)
    if store is not None:
        ap_item_w = 0.0
        ad_item_w = 0.0
        tank_item_w = 0.0
        for raw_iid in my_items:
            iid = str(raw_iid).strip()
            if not iid:
                continue
            info = store.get_item_info(iid)
            if not info.name or info.category == "consumable":
                continue
            stats = info.stats or {}
            tags = set(info.tags or ())
            if info.category == "boots":
                if iid.endswith("3020"):
                    ap_item_w += 0.6
                    _bump_axis("AP_SPELL", 0.6)
                elif iid.endswith("3006"):
                    ad_item_w += 0.6
                    _bump_axis("AUTO_ONHIT", 0.6)
                continue
            w = 1.8 if (info.category == "complete_equipment" or info.gold >= 2200) else 0.65
            ap_v = float(stats.get("FlatMagicDamageMod", 0.0) or 0.0)
            ad_v = float(stats.get("FlatPhysicalDamageMod", 0.0) or 0.0)
            as_v = float(stats.get("PercentAttackSpeedMod", 0.0) or 0.0)
            crit_v = float(stats.get("FlatCritChanceMod", 0.0) or 0.0)
            armor_v = float(stats.get("FlatArmorMod", 0.0) or 0.0)
            mr_v = float(stats.get("FlatSpellBlockMod", 0.0) or 0.0)
            hp_v = float(stats.get("FlatHPPoolMod", 0.0) or 0.0)

            if ap_v > 0:
                ap_item_w += w
                _bump_axis("AP_SPELL", w)
                profile_scores["AP"] += w * 1.15
            if ad_v > 0:
                ad_item_w += w
                _bump_axis("AD_ATTACK", w)
                profile_scores["AD"] += w * 1.15
            if as_v > 0:
                _bump_axis("AUTO_ONHIT", w * 0.9)
                profile_scores["AD"] += w * 0.45
            if crit_v > 0:
                _bump_axis("CRIT_ATTACK", w * 0.9)
                profile_scores["AD"] += w * 0.45
            if (armor_v > 0 or mr_v > 0 or hp_v >= 350) and ap_v <= 0 and ad_v <= 0:
                tank_item_w += w
                _bump_axis("TANK_HP", w)
                profile_scores["TANK"] += w * 1.1
            if ("ManaRegen" in tags or "Heal" in tags) and ap_v <= 75 and ad_v <= 0:
                _bump_axis("SHIELD_HEAL", w * 0.85)
                profile_scores["SUPPORT"] += w * 0.85

        if ap_item_w >= 1.8 and ap_item_w >= ad_item_w + 1.2:
            evidence.append("当前已出 AP 装备")
        elif ad_item_w >= 1.8 and ad_item_w >= ap_item_w + 1.2:
            evidence.append("当前已出物理装备")
        elif tank_item_w >= 1.8:
            evidence.append("当前已出坦克防御装")

    # 3. Determine primary_profile & favored/penalized axes
    ranked_profiles = sorted(profile_scores.items(), key=lambda kv: kv[1], reverse=True)
    top_prof, top_score = ranked_profiles[0]
    second_prof, second_score = ranked_profiles[1]

    if top_score < 1.6:
        primary_profile = "BALANCED"
    elif has_hybrid_bridge and top_prof in ("AP", "AD") and second_prof in ("AP", "AD") and second_score >= 1.5:
        primary_profile = "HYBRID"
    else:
        primary_profile = top_prof

    favored_axes = tuple(
        ax for ax, sc in sorted(axis_scores.items(), key=lambda kv: kv[1], reverse=True) if sc >= 1.2
    )[:5]

    penalized: list[str] = []

    def _penalize(ax: str) -> None:
        if ax in VALID_MECHANIC_AXES and ax not in penalized:
            # Never penalize an axis strongly supported by owned augments/items
            if axis_scores.get(ax, 0.0) >= 2.2:
                return
            penalized.append(ax)

    if primary_profile == "AP" and not has_hybrid_bridge:
        for ax in ("AD_ATTACK", "AUTO_ONHIT", "CRIT_ATTACK"):
            _penalize(ax)
    elif primary_profile == "AD" and not has_hybrid_bridge:
        for ax in ("AP_SPELL", "SHIELD_HEAL"):
            _penalize(ax)
    elif primary_profile == "CRIT_SPELL":
        for ax in ("TANK_HP", "SHIELD_HEAL"):
            _penalize(ax)
    elif primary_profile == "TANK":
        for ax in ("CRIT_ATTACK", "AUTO_ONHIT"):
            _penalize(ax)
        if axis_scores.get("AP_SPELL", 0.0) < 1.2:
            _penalize("AP_SPELL")
    elif primary_profile == "SUPPORT":
        for ax in ("AD_ATTACK", "AUTO_ONHIT", "CRIT_ATTACK"):
            _penalize(ax)

    for ax, vote in sorted(anti_votes.items(), key=lambda kv: kv[1], reverse=True):
        if vote >= 1.7 and not (has_hybrid_bridge and ax in ("AP_SPELL", "AD_ATTACK", "AUTO_ONHIT")):
            _penalize(ax)

    direction_label = _format_direction_label(primary_profile, axis_scores)
    penalized_labels = tuple(AXIS_LABELS[ax] for ax in penalized if ax in AXIS_LABELS)
    return MatchBuildProfile(
        primary_profile=primary_profile,
        direction_label=direction_label,
        favored_axes=favored_axes,
        penalized_axes=tuple(penalized),
        penalized_labels=penalized_labels,
        evidence=tuple(evidence[:3]),
    )


def _format_direction_label(primary_profile: str, axis_scores: dict[str, float]) -> str:
    if primary_profile == "AP":
        if axis_scores.get("HASTE_LOOP", 0.0) >= 2.0:
            return "AP 技能循环流"
        if axis_scores.get("BURN_DOT", 0.0) >= 2.0:
            return "AP 灼烧消耗流"
        return "AP 法术爆发流"
    if primary_profile == "CRIT_SPELL":
        return "技能暴击核爆流"
    if primary_profile == "AD":
        if axis_scores.get("AUTO_ONHIT", 0.0) >= 2.0:
            return "AD 攻速特效流"
        if axis_scores.get("CRIT_ATTACK", 0.0) >= 2.0:
            return "AD 暴击射手流"
        return "物理输出流"
    if primary_profile == "TANK":
        return "重装坦克叠血流"
    if primary_profile == "SUPPORT":
        return "盾奶软辅联动流"
    if primary_profile == "HYBRID":
        return "双修特效混伤流"
    return "待定流派 · 开局博上限"


def _build_progress(store: DataStore | None, rule: AugmentComboRule, owned_set: set[str]) -> ActiveComboProgress:
    owned_in_combo = tuple(aid for aid in (*rule.core_augments, *rule.follow_ups) if aid in owned_set)
    missing_core_ids = tuple(aid for aid in rule.core_augments if aid not in owned_set)
    missing_core_names = tuple(_aug_name_rarity(store, aid)[0] for aid in missing_core_ids)
    owned_core_count = sum(1 for aid in rule.core_augments if aid in owned_set)
    has_anchor = (not rule.required_anchor) or (rule.required_anchor in owned_set)
    completed = has_anchor and (
        owned_core_count >= 2
        or (len(rule.core_augments) <= 1 and owned_core_count >= 1 and len(owned_in_combo) >= 2)
    )
    return ActiveComboProgress(
        combo_id=rule.combo_id,
        name=rule.name,
        gamble=rule.gamble,
        owned_ids=owned_in_combo,
        missing_core_ids=missing_core_ids,
        missing_core_names=missing_core_names,
        completed=completed,
        synergy_reason=rule.synergy_reason,
        reroll_hint=rule.reroll_hint,
    )


def build_combo_plan(
    store: DataStore | None,
    hero_id: str | None,
    selected_augments: Sequence[str],
    my_items: Sequence[str],
    *,
    limit: int = 4,
) -> ComboPlan:
    """Build in-game combo roadmap: active combos, top follow-ups to chase/gamble, and avoid list."""
    db = get_augment_combo_db()
    hid = str(hero_id).strip() if hero_id else ""
    hero = store.get_hero(hid) if (store is not None and hid) else None
    hero_name = hero.name if hero is not None else "未锁定英雄"
    owned_ids = [str(a).strip() for a in selected_augments if str(a).strip()]
    owned_set = set(owned_ids)
    round_index = min(4, len(owned_ids) + 1)

    profile = infer_match_build_profile(store, hid or None, owned_ids, my_items)
    penalized_set = set(profile.penalized_axes)

    # Active combos: require >=1 owned core augment so generic follow-ups alone never masquerade as active combos
    scored_active: list[tuple[float, AugmentComboRule, ActiveComboProgress]] = []
    for rule in db.entries:
        owned_cores = [aid for aid in rule.core_augments if aid in owned_set]
        owned_fups = [aid for aid in rule.follow_ups if aid in owned_set]
        if not owned_cores:
            continue
        has_anchor = (not rule.required_anchor) or (rule.required_anchor in owned_set)
        h_match = bool(hid and hid in rule.hero_bonus)
        prog = _build_progress(store, rule, owned_set)
        rank_score = (
            len(owned_cores) * 10.0
            + len(owned_fups) * 3.0
            + (4.5 if h_match else 0.0)
            + (1.8 if rule.gamble else 0.0)
            + (2.5 if rule.profile == profile.primary_profile else 0.0)
            - (12.0 if not has_anchor else 0.0)
        )
        scored_active.append((rank_score, rule, prog))

    scored_active.sort(key=lambda x: x[0], reverse=True)
    active_combos = tuple(p for _, _, p in scored_active[:3])

    # Opening hero signature combos (shown when 0 augments owned yet)
    opening_combos: list[ActiveComboProgress] = []
    if not owned_set:
        hero_rules = db.combos_for_hero(hid) if hid else []
        hero_rules_sorted = sorted(
            hero_rules,
            key=lambda r: (
                1 if r.profile == profile.primary_profile else 0,
                1 if r.gamble else 0,
                len(r.core_augments),
            ),
            reverse=True,
        )
        for rule in hero_rules_sorted[:3]:
            opening_combos.append(_build_progress(store, rule, owned_set))
        if not opening_combos:
            for fallback_id in ("big_shot_jeweled", "infinite_recursion_ap_loop", "tank_engine_steel_heart"):
                r = db.get_by_id(fallback_id)
                if r is not None:
                    opening_combos.append(_build_progress(store, r, owned_set))

    # Candidate follow-up augments to chase / save reroll dice for
    best_by_aug: dict[str, ComboFollowUp] = {}

    def _consider_target(
        aid: str,
        rule: AugmentComboRule,
        base_score: float,
        reason: str,
        is_core: bool,
    ) -> None:
        if aid in owned_set:
            return
        axes = infer_augment_axes(store, aid)
        # Skip non-core suggestions that conflict cleanly with locked profile
        if not is_core and axes and penalized_set and set(axes).issubset(penalized_set):
            return
        nm, rar = _aug_name_rarity(store, aid)
        h_sig = bool(hid and hid in rule.hero_bonus)
        score = (
            base_score
            + (3.2 if h_sig else 0.0)
            + (1.6 if rule.gamble else 0.0)
            + _RARITY_PRIORITY.get(rar, 0.3)
        )
        cand = ComboFollowUp(
            augment_id=aid,
            name=nm,
            rarity=rar,
            combo_id=rule.combo_id,
            combo_name=rule.name,
            reason=reason,
            gamble=rule.gamble,
            priority_score=round(score, 2),
            hero_signature=h_sig,
        )
        prev = best_by_aug.get(aid)
        if prev is None or cand.priority_score > prev.priority_score:
            best_by_aug[aid] = cand

    for _rank, rule, prog in scored_active[:4]:
        has_anchor = (not rule.required_anchor) or (rule.required_anchor in owned_set)
        owned_core_names = [_aug_name_rarity(store, a)[0] for a in rule.core_augments if a in owned_set]
        anchor_txt = "+".join(owned_core_names[:2]) if owned_core_names else ""
        if not has_anchor:
            req_nm = _aug_name_rarity(store, rule.required_anchor)[0]
            _consider_target(
                rule.required_anchor,
                rule,
                base_score=9.5,
                reason=f"补齐关键核心【{req_nm}】激活 · {rule.name}",
                is_core=True,
            )
            continue
        core_reason = (
            f"与【{anchor_txt}】核爆成型 · {rule.name}"
            if anchor_txt
            else f"补齐核心成型 · {rule.name}"
        )
        fup_reason = (
            f"强搭配【{anchor_txt}】 · 放大{rule.name}"
            if anchor_txt
            else f"强适配 · 放大{rule.name}"
        )
        for aid in rule.core_augments:
            _consider_target(
                aid,
                rule,
                base_score=11.0,
                reason=core_reason,
                is_core=True,
            )
        for idx, aid in enumerate(rule.follow_ups[:6]):
            _consider_target(
                aid,
                rule,
                base_score=6.8 - idx * 0.25,
                reason=fup_reason,
                is_core=False,
            )

    if not best_by_aug:
        # Round 1 opening targets from hero signature combos
        for prog in opening_combos:
            rule = db.get_by_id(prog.combo_id)
            if rule is None:
                continue
            for idx, aid in enumerate(rule.core_augments[:2]):
                partner_ids = [x for x in rule.core_augments if x != aid]
                partner_nm = _aug_name_rarity(store, partner_ids[0])[0] if partner_ids else ""
                reason = (
                    f"开局可赌「{rule.name}」（配{partner_nm}）"
                    if (rule.gamble and partner_nm)
                    else f"开局优质核心 · {rule.name}"
                )
                _consider_target(aid, rule, base_score=8.0 - idx * 0.5, reason=reason, is_core=True)

    if len(owned_ids) >= 4:
        recommended_targets: tuple[ComboFollowUp, ...] = ()
    else:
        recommended_targets = tuple(
            sorted(best_by_aug.values(), key=lambda t: t.priority_score, reverse=True)[: max(1, limit)]
        )

    avoid_summary = (
        f"降权避坑：{' · '.join(profile.penalized_labels[:3])}"
        if profile.penalized_labels
        else ""
    )

    return ComboPlan(
        hero_id=hid or None,
        hero_name=hero_name,
        profile=profile,
        active_combos=active_combos,
        recommended_targets=recommended_targets,
        opening_combos=tuple(opening_combos),
        avoid_summary=avoid_summary,
        round_index=round_index,
    )


def evaluate_picker_combos(
    store: DataStore | None,
    hero_id: str | None,
    selected_augments: Sequence[str],
    my_items: Sequence[str],
    offered: Sequence[tuple[int, AugmentInfo | None]],
) -> PickerComboVerdict:
    """Evaluate 3 live offered augments strictly on combo synergy, gamble upside, and conflict downgrade."""
    db = get_augment_combo_db()
    hid = str(hero_id).strip() if hero_id else ""
    owned_ids = [str(a).strip() for a in selected_augments if str(a).strip()]
    owned_set = set(owned_ids)
    round_index = min(4, len(owned_ids) + 1)

    profile = infer_match_build_profile(store, hid or None, owned_ids, my_items)
    favored_set = set(profile.favored_axes)
    penalized_set = set(profile.penalized_axes)

    active_rules: list[AugmentComboRule] = [
        r for r in db.entries if any(a in owned_set for a in r.core_augments)
    ]

    advices: list[PickerCardComboAdvice] = []
    for slot_index, aug in offered:
        if aug is None:
            continue
        aid = str(aug.augment_id)
        nm = aug.name
        rar = aug.rarity
        cand_axes = infer_augment_axes(store, aid)
        cand_axis_set = set(cand_axes)

        # 1. Check JACKPOT: completes/amplifies core of an active owned combo
        best_jackpot: tuple[float, AugmentComboRule, str] | None = None
        for rule in active_rules:
            if aid in rule.core_augments and aid not in owned_set:
                if rule.required_anchor and rule.required_anchor not in owned_set and aid != rule.required_anchor:
                    continue
                owned_cores = [_aug_name_rarity(store, a)[0] for a in rule.core_augments if a in owned_set]
                h_match = bool(hid and hid in rule.hero_bonus)
                delta = 9.5 + (2.0 if h_match else 0.0) + (1.0 if rule.gamble else 0.0)
                reason = f"与已选【{'+'.join(owned_cores[:2])}】核爆成型 · {rule.name}"
                if best_jackpot is None or delta > best_jackpot[0]:
                    best_jackpot = (delta, rule, reason)

        if best_jackpot is not None:
            delta, rule, reason = best_jackpot
            if round_index >= 4:
                delta += 0.8
            v_lbl = "收官成型" if round_index >= 4 else ("核爆绝配" if rule.gamble else "核心成型")
            advices.append(
                PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="jackpot",
                    verdict_label=v_lbl,
                    combo_delta=round(delta, 1),
                    reason=reason,
                    combo_name=rule.name,
                    gamble=rule.gamble,
                )
            )
            continue

        # 2. Check active combo follow-up synergy
        best_followup: tuple[float, AugmentComboRule, str] | None = None
        for rule in active_rules:
            if aid in rule.follow_ups and aid not in owned_set:
                if rule.required_anchor and rule.required_anchor not in owned_set:
                    continue
                owned_cores = [_aug_name_rarity(store, a)[0] for a in rule.core_augments if a in owned_set]
                h_match = bool(hid and hid in rule.hero_bonus)
                delta = 6.2 + (1.3 if h_match else 0.0) + (0.6 if rule.gamble else 0.0)
                reason = f"强适配【{'+'.join(owned_cores[:2])}】 · {rule.name}"
                if best_followup is None or delta > best_followup[0]:
                    best_followup = (delta, rule, reason)

        if best_followup is not None:
            delta, rule, reason = best_followup
            advices.append(
                PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="synergy",
                    verdict_label="组合顺滑",
                    combo_delta=round(delta, 1),
                    reason=reason,
                    combo_name=rule.name,
                    gamble=rule.gamble,
                )
            )
            continue

        # 3. Check ANTI-SYNERGY CONFLICT (ignore non-combat meta axes like GAMBLE_HIGHROLL/MOBILITY_DASH!)
        penalized_hits = [ax for ax in cand_axes if ax in penalized_set]
        favored_hits = [ax for ax in cand_axes if ax in favored_set and ax not in penalized_set]
        combat_favored_hits = [ax for ax in favored_hits if ax not in _META_NON_COMBAT_AXES]
        if penalized_hits and not combat_favored_hits and profile.primary_profile != "BALANCED":
            penalty = -4.5 - min(2.5, (len(penalized_hits) - 1) * 1.5)
            hit_lbl = "/".join(AXIS_LABELS.get(ax, ax) for ax in penalized_hits[:2])
            advices.append(
                PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="conflict",
                    verdict_label="流派冲突·降权",
                    combo_delta=round(penalty, 1),
                    reason=f"当前{profile.direction_label}，{hit_lbl}系降权",
                    combo_name="",
                    gamble=False,
                )
            )
            continue

        # 4. Round-aware HIGH-CEILING GAMBLE OPENER:
        #    - Round 1-2: encourage high-ceiling gamble openers (more rounds left to hit partners)
        #    - Round 3: only hero-signature gambles needing <=1 more partner, modest boost
        #    - Round 4 (finale): NEVER push unstarted multi-card gambles (no future augment rounds!)
        best_gamble: tuple[float, AugmentComboRule, str] | None = None
        if round_index <= 3:
            for rule in db.combos_for_augment(aid):
                if aid not in rule.core_augments or not rule.gamble:
                    continue
                # Skip gamble combos whose profile directly clashes with locked profile
                if profile.primary_profile in ("AP", "AD", "TANK", "SUPPORT") and rule.profile in (
                    "AP",
                    "AD",
                    "TANK",
                    "SUPPORT",
                ) and rule.profile != profile.primary_profile:
                    continue
                h_match = bool(hid and hid in rule.hero_bonus)
                if not h_match and rule.combo_id != "gacha_transmute_highroll":
                    continue
                missing_core_ids = [x for x in rule.core_augments if x != aid and x not in owned_set]
                if round_index == 3 and (not h_match or len(missing_core_ids) > 1):
                    continue
                partners = [_aug_name_rarity(store, x)[0] for x in missing_core_ids]
                partner_txt = "/".join(partners[:2])
                if round_index == 1:
                    delta = 7.6 if h_match else 4.8
                    prefix = f"第1轮高上限可赌「{rule.name}」"
                elif round_index == 2:
                    delta = 6.8 if h_match else 4.2
                    prefix = f"高上限可赌「{rule.name}」"
                else:
                    delta = 4.2
                    prefix = f"第3轮末班车可赌「{rule.name}」"
                reason = f"{prefix} · 后续追【{partner_txt}】" if partner_txt else prefix
                if best_gamble is None or delta > best_gamble[0]:
                    best_gamble = (delta, rule, reason)

        if best_gamble is not None:
            delta, rule, reason = best_gamble
            advices.append(
                PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="gamble",
                    verdict_label="高上限可赌" if round_index <= 2 else "末班可赌",
                    combo_delta=round(delta, 1),
                    reason=reason,
                    combo_name=rule.name,
                    gamble=True,
                )
            )
            continue

        # 4b. Round 4 finale anti-gamble guard: warn against unstarted multi-piece gamble seeds!
        if round_index >= 4:
            unstarted_gamble = next(
                (
                    r
                    for r in db.combos_for_augment(aid)
                    if r.gamble
                    and aid in r.core_augments
                    and len(r.core_augments) >= 2
                    and not any(c in owned_set for c in r.core_augments)
                ),
                None,
            )
            if unstarted_gamble is not None and not combat_favored_hits:
                advices.append(
                    PickerCardComboAdvice(
                        slot_index=slot_index,
                        augment_id=aid,
                        name=nm,
                        rarity=rar,
                        verdict="neutral",
                        verdict_label="末轮慎赌",
                        combo_delta=-1.5,
                        reason=f"第4轮收官无后续海克斯 · 难凑齐「{unstarted_gamble.name}」，优先即时战力",
                        combo_name="",
                        gamble=False,
                    )
                )
                continue

        # 5. Check same-axis positive alignment
        if favored_hits and not penalized_hits:
            hit_lbl = "/".join(AXIS_LABELS.get(ax, ax) for ax in favored_hits[:2])
            delta = 3.2 + min(1.5, (len(favored_hits) - 1) * 0.9)
            advices.append(
                PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="synergy",
                    verdict_label="流派顺滑",
                    combo_delta=round(delta, 1),
                    reason=f"契合当前{profile.direction_label}（{hit_lbl}）",
                    combo_name="",
                    gamble=False,
                )
            )
            continue

        # 6. Check opening non-gamble hero signature core at 0 augments
        if not owned_set and hid:
            sig_rule = next(
                (r for r in db.combos_for_augment(aid) if aid in r.core_augments and hid in r.hero_bonus),
                None,
            )
            if sig_rule is not None:
                partners = [_aug_name_rarity(store, x)[0] for x in sig_rule.core_augments if x != aid]
                p_txt = "/".join(partners[:2])
                advices.append(
                    PickerCardComboAdvice(
                        slot_index=slot_index,
                        augment_id=aid,
                        name=nm,
                        rarity=rar,
                        verdict="synergy",
                        verdict_label="招牌起手",
                        combo_delta=5.0,
                        reason=f"适合开「{sig_rule.name}」· 后续配【{p_txt}】" if p_txt else f"适合开「{sig_rule.name}」",
                        combo_name=sig_rule.name,
                        gamble=sig_rule.gamble,
                    )
                )
                continue

        # 7. Neutral fallback
        advices.append(
            PickerCardComboAdvice(
                slot_index=slot_index,
                augment_id=aid,
                name=nm,
                rarity=rar,
                verdict="neutral",
                verdict_label="常规",
                combo_delta=0.0,
                reason="与当前流派无明显强联动或冲突，可参考上方胜率卡",
                combo_name="",
                gamble=False,
            )
        )

    best_slot = -1
    best_delta = 2.99
    for item in advices:
        if item.combo_delta > best_delta:
            best_delta = item.combo_delta
            best_slot = item.slot_index

    reroll_advice = ""
    if best_slot < 0 and active_rules:
        # Prioritize unfinished hero-matching or gamble combos for reroll advice
        ranked_chase = sorted(
            active_rules,
            key=lambda r: (
                1 if any(a not in owned_set for a in r.core_augments) else 0,
                1 if (hid and hid in r.hero_bonus) else 0,
                1 if r.gamble else 0,
            ),
            reverse=True,
        )
        top_chase = ranked_chase[0]
        missing = [_aug_name_rarity(store, a)[0] for a in top_chase.core_augments if a not in owned_set]
        if missing:
            reroll_advice = f"本轮无强联动 · 可用骰子追【{' / '.join(missing[:2])}】（{top_chase.name}）"
        elif top_chase.reroll_hint:
            reroll_advice = f"本轮无强联动 · {top_chase.reroll_hint}"

    return PickerComboVerdict(
        profile=profile,
        cards=tuple(advices),
        best_slot_index=best_slot,
        reroll_advice=reroll_advice,
        round_index=round_index,
    )


def export_combo_coach_context(
    plan: ComboPlan,
    picker_verdict: PickerComboVerdict | None = None,
) -> dict[str, Any]:
    """Serialize compact combo & anti-synergy summary for zero-extra-cost AI Coach prompt injection."""
    payload: dict[str, Any] = {
        "round_index": picker_verdict.round_index if picker_verdict is not None else plan.round_index,
        "direction": plan.profile.direction_label,
        "primary_profile": plan.profile.primary_profile,
        "penalized_axes": list(plan.profile.penalized_labels),
        "active_combos": [
            {
                "name": c.name,
                "gamble": c.gamble,
                "completed": c.completed,
                "chase": list(c.missing_core_names[:2]),
            }
            for c in plan.active_combos[:2]
        ],
        "top_targets": [
            {"name": t.name, "combo": t.combo_name, "gamble": t.gamble}
            for t in plan.recommended_targets[:3]
        ],
    }
    if picker_verdict is not None and picker_verdict.cards:
        payload["picker_verdicts"] = [
            {
                "slot": c.slot_index,
                "name": c.name,
                "verdict": c.verdict_label,
                "delta": c.combo_delta,
                "reason": c.reason,
            }
            for c in picker_verdict.cards
        ]
        if picker_verdict.reroll_advice:
            payload["reroll_advice"] = picker_verdict.reroll_advice
    return payload
