"""Bayesian-smoothed tier rating for champions and hero-augment pairs."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Any, Sequence

DEFAULT_PRIOR_K = 100.0
LATE_STACKER_AUGMENT_DISCOUNTS: dict[str, float] = {
    "1353": -4.2,  # 坦克引擎：第4轮/12分钟后叠层周期不足
    "1152": -4.6,  # 钢化你心：第4轮/12分钟后心之钢任务难叠满
}


@dataclass(frozen=True)
class RarityDeltaDist:
    """Empirical win-rate delta distribution for a (hero, rarity) augment pool."""

    mean: float
    std: float
    p25: float
    median: float
    p75: float
    p90: float
    sorted_deltas: tuple[float, ...]


@dataclass(frozen=True)
class AugmentScore:
    augment_id: str
    name: str
    rarity: str
    tier: str  # "S+" | "S" | "A" | "B" | "C" | "D" | "?"
    raw_win_rate: float
    smoothed_win_rate: float
    delta: float
    games: int
    low_sample: bool
    percentile: float | None = None
    z_score: float | None = None
    is_below_rarity_par: bool = False

    @property
    def sample_label(self) -> str:
        if self.games < 30:
            return "样本不足"
        if self.low_sample or self.games < 300:
            return f"低样本 · {self.games:,}场"
        return f"{self.games:,}场"


def tier_from_delta(delta: float) -> str:
    if delta >= 0.035:
        return "S+"
    if delta >= 0.020:
        return "S"
    if delta >= 0.005:
        return "A"
    if delta >= -0.015:
        return "B"
    if delta >= -0.035:
        return "C"
    return "D"


def tier_from_rarity_percentile(delta: float, percentile: float) -> str:
    """Rarity-calibrated tiering with strong absolute delta guardrails."""
    if delta >= 0.035 or (percentile >= 90.0 and delta >= 0.015):
        return "S+"
    if delta <= -0.040:
        return "D"
    if percentile >= 75.0 and delta >= 0.004:
        return "S"
    if percentile >= 50.0:
        return "A"
    if percentile >= 28.0:
        return "B"
    if percentile >= 12.0:
        return "C"
    return "D"


def _compute_percentile(sorted_deltas: tuple[float, ...], value: float) -> float:
    n = len(sorted_deltas)
    if n <= 0:
        return 50.0
    lo = bisect_left(sorted_deltas, value)
    hi = bisect_right(sorted_deltas, value)
    if hi == 0:
        return 0.0
    if lo == n:
        return 100.0
    pct = ((lo + hi) / 2.0) / n * 100.0
    return round(max(0.0, min(100.0, pct)), 2)


def score_hero_tier(win_rate: float) -> str:
    """Rate a champion's overall ARAM Mayhem strength by win rate."""
    if win_rate >= 0.545:
        return "S+"
    if win_rate >= 0.520:
        return "S"
    if win_rate >= 0.500:
        return "A"
    if win_rate >= 0.475:
        return "B"
    if win_rate >= 0.450:
        return "C"
    return "D"


def score_hero_augment(
    augment_id: str,
    name: str,
    rarity: str,
    hero_win_rate: float,
    wins: int,
    games: int,
    low_sample: bool = False,
    k: float = DEFAULT_PRIOR_K,
    rarity_dist: RarityDeltaDist | None = None,
) -> AugmentScore:
    """Compute Bayesian-smoothed win rate and delta relative to the hero's baseline win rate."""
    prior = hero_win_rate if 0.05 <= hero_win_rate <= 0.95 else 0.50
    if games <= 0:
        return AugmentScore(
            augment_id=str(augment_id),
            name=name,
            rarity=rarity,
            tier="?",
            raw_win_rate=prior,
            smoothed_win_rate=prior,
            delta=0.0,
            games=0,
            low_sample=True,
            percentile=None,
            z_score=None,
            is_below_rarity_par=False,
        )

    raw_wr = wins / games
    smoothed_wr = (wins + k * prior) / (games + k)
    delta = smoothed_wr - prior
    is_low = bool(low_sample or games < 300)

    percentile: float | None = None
    z_score: float | None = None
    is_below_rarity_par = False

    if games < 30:
        tier = "?"
    elif rarity_dist is not None and rarity_dist.sorted_deltas:
        percentile = _compute_percentile(rarity_dist.sorted_deltas, delta)
        z_score = round((delta - rarity_dist.mean) / rarity_dist.std, 3) if rarity_dist.std > 1e-9 else 0.0
        tier = tier_from_rarity_percentile(delta, percentile)
        is_below_rarity_par = bool(percentile < 38.0 and delta < -0.008)
    else:
        tier = tier_from_delta(delta)

    return AugmentScore(
        augment_id=str(augment_id),
        name=name,
        rarity=rarity,
        tier=tier,
        raw_win_rate=raw_wr,
        smoothed_win_rate=smoothed_wr,
        delta=delta,
        games=games,
        low_sample=is_low,
        percentile=percentile,
        z_score=z_score,
        is_below_rarity_par=is_below_rarity_par,
    )


@dataclass(frozen=True)
class UnifiedAugmentDecision:
    """Single source of truth combining hero-augment stats, live combo/conflict shift, and crowd pair delta."""

    slot_index: int
    base_score: AugmentScore
    base_hex_score: float
    combo_shift: float
    pair_shift: float
    final_hex_score: float
    effective_delta: float
    shift_kind: str  # "synergy" | "conflict" | "gamble" | ""
    shift_label: str
    shift_reason: str
    is_best: bool = False
    overridden_by_combo: bool = False


def compute_hex_score(score: AugmentScore) -> float:
    """Map AugmentScore (percentile + Bayesian delta) into a clean 15.0..98.5 player-facing 100-point scale."""
    if score.games < 30:
        return 50.0
    delta_term = 58.0 + score.delta * 620.0
    if score.percentile is not None:
        pct_term = 18.0 + score.percentile * 0.78
        raw = 0.62 * pct_term + 0.38 * delta_term
    else:
        raw = delta_term
    return round(max(15.0, min(98.5, raw)), 1)


def rank_picker_cards(
    scored_cards: Sequence[tuple[int, AugmentScore]],
    combo_by_slot: dict[int, Any] | None = None,
    pair_deltas: dict[str, tuple[float, int]] | None = None,
    *,
    game_time_s: float = 0.0,
    round_index: int = 0,
) -> tuple[list[UnifiedAugmentDecision], str]:
    """Compute unified scores, apply live synergy/conflict/late-stacker re-ranking, and preserve all-weak reroll guard."""
    combo_map = combo_by_slot or {}
    pair_map = pair_deltas or {}
    is_late_stacker_window = (int(round_index or 0) >= 4) or (float(game_time_s or 0.0) >= 720.0)

    prelim: list[dict[str, Any]] = []
    base_best_slot: int | None = None
    base_best_key = (-999.0, -999.0)
    valid_scores: list[AugmentScore] = []

    for slot_index, sc in scored_cards:
        base_hex = compute_hex_score(sc)
        if sc.games >= 30:
            valid_scores.append(sc)
            cand_key = (base_hex, round(sc.delta, 5))
            if cand_key > base_best_key:
                base_best_key = cand_key
                base_best_slot = slot_index

        vc = combo_map.get(slot_index)
        combo_shift = 0.0
        shift_kind = ""
        shift_reason = ""
        verdict_name = getattr(vc, "verdict", "") if vc is not None else ""
        verdict_label = str(getattr(vc, "verdict_label", "") or "") if vc is not None else ""
        raw_cd = float(getattr(vc, "combo_delta", 0.0) or 0.0) if vc is not None else 0.0
        late_stacker_applied = verdict_label == "末轮叠层折价"

        if verdict_name == "jackpot":
            combo_shift = min(11.5, max(5.5, raw_cd * 0.85))
            shift_kind = "synergy"
            shift_reason = str(getattr(vc, "reason", "") or "")
        elif verdict_name == "synergy":
            combo_shift = min(7.5, max(3.0, raw_cd * 0.75))
            shift_kind = "synergy"
            shift_reason = str(getattr(vc, "reason", "") or "")
        elif verdict_name == "gamble":
            combo_shift = min(5.5, max(2.2, raw_cd * 0.65))
            shift_kind = "gamble"
            shift_reason = str(getattr(vc, "reason", "") or "")
        elif verdict_name == "conflict":
            if late_stacker_applied:
                combo_shift = max(-5.0, min(-3.8, raw_cd))
            else:
                combo_shift = max(-10.0, min(-3.5, raw_cd * 1.15))
            shift_kind = "conflict"
            shift_reason = str(getattr(vc, "reason", "") or "")

        if is_late_stacker_window and sc.augment_id in LATE_STACKER_AUGMENT_DISCOUNTS and not late_stacker_applied:
            late_disc = LATE_STACKER_AUGMENT_DISCOUNTS[sc.augment_id]
            combo_shift = combo_shift + late_disc
            late_stacker_applied = True
            if combo_shift < 0:
                shift_kind = "conflict"
                shift_reason = (
                    f"局末({int(game_time_s)}s)叠层周期不足 · 优先即时战力"
                    if float(game_time_s or 0.0) >= 720.0
                    else "第4轮收官叠层周期不足 · 叠层卡大幅折价，优先即时战力"
                )

        combo_shift = round(combo_shift, 1)

        pair_shift = 0.0
        p_entry = pair_map.get(sc.augment_id)
        if p_entry is not None:
            p_delta, p_games = float(p_entry[0]), int(p_entry[1])
            if p_games >= 10:
                conf = min(1.0, p_games / 50.0)
                pair_shift = round(max(-6.0, min(6.0, p_delta * 160.0 * conf)), 1)
                if not shift_kind and abs(pair_shift) >= 1.5:
                    shift_kind = "synergy" if pair_shift > 0 else "conflict"
                    shift_reason = f"实测配对胜率 {p_delta * 100:+.1f}% ({p_games}场)"

        total_shift = round(combo_shift + pair_shift, 1)
        final_hex = round(max(10.0, min(99.5, base_hex + total_shift)), 1)
        effective_delta = round(sc.delta + total_shift / 620.0, 5)

        if total_shift >= 1.5 and shift_kind == "gamble":
            shift_label = f"高上限 ↑{total_shift:.1f}"
        elif total_shift >= 1.5:
            shift_label = f"联动 ↑{total_shift:.1f}"
        elif total_shift <= -1.5:
            shift_label = (
                f"折价 ↓{abs(total_shift):.1f}"
                if late_stacker_applied
                else f"互斥 ↓{abs(total_shift):.1f}"
            )
        else:
            shift_label = ""

        prelim.append(
            {
                "slot_index": slot_index,
                "base_score": sc,
                "base_hex_score": base_hex,
                "combo_shift": combo_shift,
                "pair_shift": pair_shift,
                "total_shift": total_shift,
                "final_hex_score": final_hex,
                "effective_delta": effective_delta,
                "shift_kind": shift_kind,
                "shift_label": shift_label,
                "shift_reason": shift_reason,
                "verdict_name": verdict_name,
            }
        )

    all_below_par = bool(valid_scores) and all(
        (bool(sc.is_below_rarity_par) if sc.percentile is not None else (sc.delta < 0.0))
        for sc in valid_scores
    )
    has_combo_upside = any(
        (p["verdict_name"] in ("jackpot", "gamble", "synergy") and p["combo_shift"] > 0)
        or p["pair_shift"] >= 1.5
        for p in prelim
    )

    best_slot: int | None = None
    reroll_banner = ""
    if valid_scores or has_combo_upside:
        if all_below_par and not has_combo_upside:
            top_valid = max(
                (p for p in prelim if p["base_score"].games >= 30),
                key=lambda p: (p["base_hex_score"], p["base_score"].delta),
            )
            top_tier = top_valid["base_score"].tier
            reroll_banner = f"本轮三卡均低于同阶均线（最高仅 {top_tier} 级）· 建议用骰子刷新"
        else:
            eligible = [
                p
                for p in prelim
                if p["base_score"].games >= 30 or p["combo_shift"] > 0 or p["pair_shift"] >= 1.5
            ]
            if eligible:
                winner = max(eligible, key=lambda p: (p["final_hex_score"], p["effective_delta"]))
                best_slot = int(winner["slot_index"])

    out: list[UnifiedAugmentDecision] = []
    for p in prelim:
        is_best = best_slot is not None and int(p["slot_index"]) == best_slot
        overridden = bool(
            is_best
            and base_best_slot is not None
            and int(p["slot_index"]) != base_best_slot
            and (
                p["total_shift"] > 0
                or any(o["slot_index"] == base_best_slot and o["total_shift"] < 0 for o in prelim)
            )
        )
        out.append(
            UnifiedAugmentDecision(
                slot_index=int(p["slot_index"]),
                base_score=p["base_score"],
                base_hex_score=float(p["base_hex_score"]),
                combo_shift=float(p["combo_shift"]),
                pair_shift=float(p["pair_shift"]),
                final_hex_score=float(p["final_hex_score"]),
                effective_delta=float(p["effective_delta"]),
                shift_kind=str(p["shift_kind"]),
                shift_label=str(p["shift_label"]),
                shift_reason=str(p["shift_reason"]),
                is_best=is_best,
                overridden_by_combo=overridden,
            )
        )
    return out, reroll_banner
