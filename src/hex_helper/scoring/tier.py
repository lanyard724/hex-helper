"""Bayesian-smoothed tier rating for champions and hero-augment pairs."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

DEFAULT_PRIOR_K = 100.0


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
