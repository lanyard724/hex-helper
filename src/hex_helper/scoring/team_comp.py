"""0ms ARAM Mayhem Team-Composition Enumerator (C(15,5)=3003), Swap Marginal Gain & Dual Goal Modes."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Sequence

from hex_helper.data.aram_balance import get_aram_balance, get_hero_ceiling_tag
from hex_helper.data.store import DataStore
from hex_helper.scoring.matchup_optimizer import HIGH_CC_HEROES, HIGH_HEAL_HEROES, HIGH_SHIELD_HEROES


@dataclass(frozen=True)
class _HeroFeat:
    hero_id: str
    name: str
    role: str  # "AP" | "AD" | "TANK"
    win_rate: float
    has_cc: bool
    has_sustain: bool
    balance_edge: float
    ceiling_bonus: float


@dataclass(frozen=True)
class SwapRecommendation:
    hero_id: str
    hero_name: str
    score_delta: float
    new_team_score: float
    reason: str


@dataclass(frozen=True)
class BestFiveComp:
    hero_ids: tuple[str, ...]
    hero_names: tuple[str, ...]
    score: float
    evaluated_combos: int
    tags: tuple[str, ...]


@dataclass(frozen=True)
class TeamCompAnalysis:
    goal_mode: str  # "climb" | "fun"
    current_score: float
    best_swap: SwapRecommendation | None
    swap_deltas: dict[str, tuple[float, str]]
    best_five: BestFiveComp | None
    summary_text: str
    is_warn: bool
    needed_roles: frozenset[str]


def classify_hero_role(hero_id: str, store: DataStore) -> str:
    """Classify a champion's primary ARAM profile ('AP' | 'AD' | 'TANK') from top core builds."""
    ad_s = 0.0
    ap_s = 0.0
    tank_s = 0.0
    for cb in store.get_core_builds(hero_id)[:2]:
        for iid in cb.items:
            info = store.get_item_info(iid)
            if info is None:
                continue
            st = info.stats or {}
            ad_s += float(st.get("FlatPhysicalDamageMod", 0.0)) * 1.4
            ad_s += float(st.get("FlatCritChanceMod", 0.0)) * 110.0
            ap_s += float(st.get("FlatMagicDamageMod", 0.0)) * 1.15
            tank_s += float(st.get("FlatArmorMod", 0.0)) * 1.45
            tank_s += float(st.get("FlatSpellBlockMod", 0.0)) * 1.45
            tank_s += float(st.get("FlatHPPoolMod", 0.0)) * 0.14
    if tank_s > max(ad_s, ap_s) * 1.15 and tank_s >= 110.0:
        return "TANK"
    if ap_s >= ad_s and ap_s >= 40.0:
        return "AP"
    if ad_s > ap_s and ad_s >= 35.0:
        return "AD"
    return "AP"


def _extract_feat(hid: str, store: DataStore) -> _HeroFeat | None:
    h = store.get_hero(hid)
    if h is None:
        return None
    role = classify_hero_role(hid, store)
    bal = get_aram_balance(hid)
    bal_edge = 0.0
    if bal is not None:
        bal_edge = (bal.dmg_dealt - 1.0) * 18.0 + (1.0 - bal.dmg_taken) * 18.0 + bal.ability_haste * 0.08
    ceil_tag = get_hero_ceiling_tag(hid)
    ceil_bonus = 6.5 if ceil_tag else 0.0
    return _HeroFeat(
        hero_id=str(hid),
        name=h.name,
        role=role,
        win_rate=float(h.win_rate),
        has_cc=(h.name in HIGH_CC_HEROES),
        has_sustain=(h.name in HIGH_HEAL_HEROES or h.name in HIGH_SHIELD_HEROES),
        balance_edge=round(bal_edge, 2),
        ceiling_bonus=ceil_bonus,
    )


def _score_five(feats: tuple[_HeroFeat, ...], goal_mode: str) -> float:
    n = len(feats)
    if n == 0:
        return 0.0

    wr_sum = 0.0
    ap_cnt = 0
    ad_cnt = 0
    tank_cnt = 0
    cc_cnt = 0
    sus_cnt = 0
    bal_sum = 0.0
    ceil_sum = 0.0

    for f in feats:
        wr_sum += f.win_rate
        if f.role == "AP":
            ap_cnt += 1
        elif f.role == "AD":
            ad_cnt += 1
        elif f.role == "TANK":
            tank_cnt += 1
        if f.has_cc:
            cc_cnt += 1
        if f.has_sustain:
            sus_cnt += 1
        bal_sum += f.balance_edge
        ceil_sum += f.ceiling_bonus

    base = 55.0 + (wr_sum / n - 0.50) * 420.0 + (bal_sum / n) * 0.45

    if goal_mode == "fun":
        comp_adj = ceil_sum * 0.95
        if tank_cnt >= 1:
            comp_adj += 3.5
        if n >= 4 and (ap_cnt == n or ad_cnt == n):
            comp_adj -= 5.0
        if sus_cnt >= 1 and ad_cnt >= 1:
            comp_adj += 3.0
    else:
        comp_adj = 0.0
        # Frontline balance
        if tank_cnt == 0 and n >= 4:
            comp_adj -= 9.5
        elif tank_cnt in (1, 2):
            comp_adj += 5.5
        elif tank_cnt >= 4:
            comp_adj -= 5.5
        # Damage profile split (prevent enemy single-resistance stack)
        if n >= 4 and (ap_cnt >= n or ad_cnt >= n):
            comp_adj -= 10.5
        elif n >= 4 and (ap_cnt >= 4 or ad_cnt >= 4):
            comp_adj -= 5.5
        elif ap_cnt >= 1 and ad_cnt >= 1:
            comp_adj += 4.5
        # CC & sustain synergy
        if cc_cnt >= 2:
            comp_adj += 3.2
        elif cc_cnt == 0 and n >= 4:
            comp_adj -= 3.0
        if sus_cnt >= 1 and (ad_cnt >= 1 or tank_cnt >= 1):
            comp_adj += 2.6
        comp_adj += ceil_sum * 0.18

    return round(max(10.0, min(99.5, base + comp_adj)), 1)


def _find_best_five(pool_feats: list[_HeroFeat], mode: str) -> BestFiveComp | None:
    """Fast C(n,5) enumerator (n <= 15 -> max 3003 combos, <5ms)."""
    if len(pool_feats) < 5:
        return None

    packed = [
        (
            f,
            f.win_rate,
            1 if f.role == "AP" else 0,
            1 if f.role == "AD" else 0,
            1 if f.role == "TANK" else 0,
            1 if f.has_cc else 0,
            1 if f.has_sustain else 0,
            f.balance_edge,
            f.ceiling_bonus,
        )
        for f in pool_feats
    ]

    evaluated = 0
    top_raw = -1e9
    top_tuple: tuple[_HeroFeat, ...] | None = None

    if mode == "fun":
        for a, b, c, d, e in combinations(packed, 5):
            evaluated += 1
            wr_sum = a[1] + b[1] + c[1] + d[1] + e[1]
            ap_cnt = a[2] + b[2] + c[2] + d[2] + e[2]
            ad_cnt = a[3] + b[3] + c[3] + d[3] + e[3]
            tank_cnt = a[4] + b[4] + c[4] + d[4] + e[4]
            sus_cnt = a[6] + b[6] + c[6] + d[6] + e[6]
            bal_sum = a[7] + b[7] + c[7] + d[7] + e[7]
            ceil_sum = a[8] + b[8] + c[8] + d[8] + e[8]

            raw = 55.0 + (wr_sum / 5.0 - 0.50) * 420.0 + (bal_sum / 5.0) * 0.45 + ceil_sum * 0.95
            if tank_cnt >= 1:
                raw += 3.5
            if ap_cnt == 5 or ad_cnt == 5:
                raw -= 5.0
            if sus_cnt >= 1 and ad_cnt >= 1:
                raw += 3.0
            if raw > top_raw:
                top_raw = raw
                top_tuple = (a[0], b[0], c[0], d[0], e[0])
    else:
        for a, b, c, d, e in combinations(packed, 5):
            evaluated += 1
            wr_sum = a[1] + b[1] + c[1] + d[1] + e[1]
            ap_cnt = a[2] + b[2] + c[2] + d[2] + e[2]
            ad_cnt = a[3] + b[3] + c[3] + d[3] + e[3]
            tank_cnt = a[4] + b[4] + c[4] + d[4] + e[4]
            cc_cnt = a[5] + b[5] + c[5] + d[5] + e[5]
            sus_cnt = a[6] + b[6] + c[6] + d[6] + e[6]
            bal_sum = a[7] + b[7] + c[7] + d[7] + e[7]
            ceil_sum = a[8] + b[8] + c[8] + d[8] + e[8]

            raw = 55.0 + (wr_sum / 5.0 - 0.50) * 420.0 + (bal_sum / 5.0) * 0.45 + ceil_sum * 0.18
            if tank_cnt == 0:
                raw -= 9.5
            elif tank_cnt <= 2:
                raw += 5.5
            elif tank_cnt >= 4:
                raw -= 5.5
            if ap_cnt == 5 or ad_cnt == 5:
                raw -= 10.5
            elif ap_cnt >= 4 or ad_cnt >= 4:
                raw -= 5.5
            elif ap_cnt >= 1 and ad_cnt >= 1:
                raw += 4.5
            if cc_cnt >= 2:
                raw += 3.2
            elif cc_cnt == 0:
                raw -= 3.0
            if sus_cnt >= 1 and (ad_cnt >= 1 or tank_cnt >= 1):
                raw += 2.6
            if raw > top_raw:
                top_raw = raw
                top_tuple = (a[0], b[0], c[0], d[0], e[0])

    if top_tuple is None:
        return None

    t_ap = sum(1 for f in top_tuple if f.role == "AP")
    t_ad = sum(1 for f in top_tuple if f.role == "AD")
    t_tk = sum(1 for f in top_tuple if f.role == "TANK")
    tags = (f"{t_ad}AD·{t_ap}AP·{t_tk}坦", "爽局高上限" if mode == "fun" else "冲分最优")
    return BestFiveComp(
        hero_ids=tuple(f.hero_id for f in top_tuple),
        hero_names=tuple(f.name for f in top_tuple),
        score=_score_five(top_tuple, mode),
        evaluated_combos=evaluated,
        tags=tags,
    )


def _swap_reason(cand: _HeroFeat, teammates: tuple[_HeroFeat, ...], goal_mode: str) -> str:
    ap_cnt = sum(1 for f in teammates if f.role == "AP")
    ad_cnt = sum(1 for f in teammates if f.role == "AD")
    tank_cnt = sum(1 for f in teammates if f.role == "TANK")
    if cand.role == "TANK" and tank_cnt == 0:
        return "补前排开团"
    if cand.role == "AD" and ad_cnt == 0 and ap_cnt >= 3:
        return "破纯AP困局"
    if cand.role == "AP" and ap_cnt == 0 and ad_cnt >= 3:
        return "破纯AD困局"
    if goal_mode == "fun" and cand.ceiling_bonus > 0:
        return "高上限爽局核"
    if cand.has_cc and sum(1 for f in teammates if f.has_cc) == 0:
        return "补关键先手控"
    if cand.has_sustain:
        return "补盾奶拉扯"
    return "高胜率补强"


def analyze_champ_select_pool(
    my_hero_id: str | None,
    team_hero_ids: Sequence[str],
    bench_hero_ids: Sequence[str],
    store: DataStore,
    goal_mode: str = "climb",
) -> TeamCompAnalysis:
    """Compute current team score, per-bench swap marginal gains, and C(n,5) optimal 5-man comp."""
    mode = "fun" if str(goal_mode).strip().lower() == "fun" else "climb"
    my_hid = str(my_hero_id).strip() if my_hero_id is not None and str(my_hero_id).strip() else ""
    team_unique = list(
        dict.fromkeys(([my_hid] if my_hid else []) + [str(h).strip() for h in team_hero_ids if h is not None and str(h).strip()])
    )
    team_set = set(team_unique)
    bench_unique = [
        h
        for h in dict.fromkeys(str(x).strip() for x in bench_hero_ids if x is not None and str(x).strip())
        if h not in team_set
    ]

    feats_by_id: dict[str, _HeroFeat] = {}
    for hid in team_unique + bench_unique:
        ft = _extract_feat(hid, store)
        if ft is not None:
            feats_by_id[hid] = ft

    team_feats = tuple(feats_by_id[h] for h in team_unique if h in feats_by_id)
    current_score = _score_five(team_feats, mode)

    # Identify missing roles for compact badges
    ap_cnt = sum(1 for f in team_feats if f.role == "AP")
    ad_cnt = sum(1 for f in team_feats if f.role == "AD")
    tank_cnt = sum(1 for f in team_feats if f.role == "TANK")
    total = len(team_feats)
    needed: set[str] = set()
    if total >= 3 and tank_cnt == 0:
        needed.add("TANK")
    if total >= 3 and (ap_cnt >= 4 or ap_cnt == total or (ap_cnt >= 3 and ad_cnt == 0)):
        needed.add("AD")
    if total >= 3 and (ad_cnt >= 4 or ad_cnt == total or (ad_cnt >= 3 and ap_cnt == 0)):
        needed.add("AP")

    # 1. Per-bench swap marginal gain (fixing teammates constant, swapping only my_hero_id)
    swap_deltas: dict[str, tuple[float, str]] = {}
    best_swap: SwapRecommendation | None = None
    if my_hid and my_hid in feats_by_id and bench_unique:
        mates = tuple(f for f in team_feats if f.hero_id != my_hid)
        for bhid in bench_unique:
            bfeat = feats_by_id.get(bhid)
            if bfeat is None:
                continue
            cand_team = (*mates, bfeat)
            cand_score = _score_five(cand_team, mode)
            delta = round(cand_score - current_score, 1)
            reason = _swap_reason(bfeat, mates, mode)
            swap_deltas[bhid] = (delta, reason)
            if delta >= 1.5 and (best_swap is None or delta > best_swap.score_delta):
                best_swap = SwapRecommendation(
                    hero_id=bhid,
                    hero_name=bfeat.name,
                    score_delta=delta,
                    new_team_score=cand_score,
                    reason=reason,
                )

    # 2. Full-pool C(n,5) enumeration (capped to 15 unique champions -> max 3003 combos, <10ms)
    pool_feats = list(feats_by_id.values())[:15]
    best_five = _find_best_five(pool_feats, mode)

    # 3. Unified actionable summary (merges static comp alert + concrete swap action)
    is_warn = False
    if total < 3:
        summary_text = ""
    elif best_swap is not None and (best_swap.score_delta >= 4.0 or needed):
        is_warn = bool(needed)
        summary_text = f"⚡ 建议换【{best_swap.hero_name}】整队 +{best_swap.score_delta:.1f}分 · {best_swap.reason}"
    elif needed:
        is_warn = True
        gap_txt = "/".join({"TANK": "前排", "AD": "物理", "AP": "法伤"}[r] for r in ("TANK", "AD", "AP") if r in needed)
        summary_text = f"⚠ 阵容缺{gap_txt} ({ad_cnt}AD·{ap_cnt}AP·{tank_cnt}坦 · 当前 {current_score:.1f}分)"
    else:
        pct_opt = int(round((current_score / best_five.score) * 100)) if (best_five and best_five.score > 0) else 100
        summary_text = f"✓ 阵容均衡 ({ad_cnt}AD·{ap_cnt}AP·{tank_cnt}坦 · 当前 {current_score:.1f}分 · 达池内 {min(100, pct_opt)}%)"

    return TeamCompAnalysis(
        goal_mode=mode,
        current_score=current_score,
        best_swap=best_swap,
        swap_deltas=swap_deltas,
        best_five=best_five,
        summary_text=summary_text,
        is_warn=is_warn,
        needed_roles=frozenset(needed),
    )
