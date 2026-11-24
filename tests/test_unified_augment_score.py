"""Tests for unified 0-100 HexScore and combo/conflict live rank override."""

from __future__ import annotations

from dataclasses import fields

from hex_helper.scoring.augment_combo import PickerCardComboAdvice
from hex_helper.scoring.tier import (
    AugmentScore,
    UnifiedAugmentDecision,
    compute_hex_score,
    rank_picker_cards,
)


def _mk_score(
    aid: str,
    name: str,
    delta: float,
    pct: float | None,
    games: int = 12000,
    below: bool = False,
) -> AugmentScore:
    return AugmentScore(
        augment_id=aid,
        name=name,
        rarity="prismatic",
        tier="?" if games < 30 else ("S" if delta >= 0.02 else ("A" if delta >= 0.0 else "C")),
        raw_win_rate=0.50 + delta,
        smoothed_win_rate=0.50 + delta,
        delta=delta,
        games=games,
        low_sample=games < 300,
        percentile=pct,
        z_score=round(delta * 40.0, 2) if pct is not None else None,
        is_below_rarity_par=below,
    )


def test_augment_score_fields_remain_backward_compatible() -> None:
    expected = (
        "augment_id",
        "name",
        "rarity",
        "tier",
        "raw_win_rate",
        "smoothed_win_rate",
        "delta",
        "games",
        "low_sample",
        "percentile",
        "z_score",
        "is_below_rarity_par",
    )
    assert tuple(f.name for f in fields(AugmentScore)) == expected


def test_compute_hex_score_maps_percentile_and_delta_to_bounded_100_scale() -> None:
    high = _mk_score("1", "易损", delta=0.045, pct=95.0)
    low = _mk_score("2", "缩小引擎", delta=-0.032, pct=12.0, below=True)
    missing = _mk_score("3", "冷门卡", delta=0.0, pct=50.0, games=10)

    s_high = compute_hex_score(high)
    s_low = compute_hex_score(low)
    s_missing = compute_hex_score(missing)

    assert 82.0 <= s_high <= 99.0
    assert 15.0 <= s_low <= 48.0
    assert s_high > s_low + 35.0
    assert s_missing == 50.0


def test_compute_hex_score_without_percentile_and_clamping() -> None:
    no_pct_strong = _mk_score("1", "强卡", delta=0.04, pct=None, games=5000)
    no_pct_extreme_high = _mk_score("2", "超模卡", delta=0.25, pct=None, games=5000)
    no_pct_extreme_low = _mk_score("3", "地狱卡", delta=-0.25, pct=None, games=5000)
    border_low = _mk_score("4", "边界卡", delta=0.04, pct=90.0, games=29)
    border_valid = _mk_score("5", "刚好达标", delta=0.04, pct=90.0, games=30)

    assert 75.0 <= compute_hex_score(no_pct_strong) <= 90.0
    assert compute_hex_score(no_pct_extreme_high) == 98.5
    assert compute_hex_score(no_pct_extreme_low) == 15.0
    assert compute_hex_score(border_low) == 50.0
    assert compute_hex_score(border_valid) > 80.0


def test_rank_picker_cards_flips_winner_on_synergy_vs_conflict_and_marks_override() -> None:
    # Base ranking: slot 0 (小丑学院) > slot 1 (歌利亚巨人) > slot 2 (尤里卡)
    c0 = _mk_score("10", "小丑学院", delta=0.022, pct=78.0)
    c1 = _mk_score("11", "歌利亚巨人", delta=0.016, pct=72.0)
    c2 = _mk_score("12", "尤里卡", delta=0.002, pct=50.0)

    combo_by_slot = {
        0: PickerCardComboAdvice(
            slot_index=0,
            augment_id="10",
            name="小丑学院",
            rarity="prismatic",
            verdict="conflict",
            verdict_label="冲突降权",
            combo_delta=-4.5,
            reason="与坦克生命轴冲突",
            combo_name="",
            gamble=False,
        ),
        1: PickerCardComboAdvice(
            slot_index=1,
            augment_id="11",
            name="歌利亚巨人",
            rarity="prismatic",
            verdict="jackpot",
            verdict_label="核心成型",
            combo_delta=9.5,
            reason="与已选【钢化你心】核爆成型",
            combo_name="钢化巨兽",
            gamble=False,
        ),
    }

    decisions, reroll_banner = rank_picker_cards(
        [(0, c0), (1, c1), (2, c2)],
        combo_by_slot=combo_by_slot,
    )
    assert reroll_banner == ""
    assert [d.slot_index for d in decisions] == [0, 1, 2]
    assert all(isinstance(d, UnifiedAugmentDecision) for d in decisions)
    by_slot = {d.slot_index: d for d in decisions}

    assert by_slot[1].is_best is True
    assert by_slot[1].overridden_by_combo is True
    assert by_slot[1].shift_kind == "synergy"
    assert by_slot[1].combo_shift > 0
    assert by_slot[1].shift_label.startswith("联动 ↑")
    assert "钢化你心" in by_slot[1].shift_reason
    assert by_slot[1].final_hex_score > by_slot[0].final_hex_score

    assert by_slot[0].is_best is False
    assert by_slot[0].overridden_by_combo is False
    assert by_slot[0].shift_kind == "conflict"
    assert by_slot[0].combo_shift < 0
    assert by_slot[0].shift_label.startswith("互斥 ↓")


def test_rank_picker_cards_preserves_reroll_dice_warning_when_all_weak_and_no_combo() -> None:
    weak_cards = [
        (0, _mk_score("20", "弱卡A", delta=-0.025, pct=18.0, below=True)),
        (1, _mk_score("21", "弱卡B", delta=-0.018, pct=25.0, below=True)),
        (2, _mk_score("22", "弱卡C", delta=-0.030, pct=10.0, below=True)),
    ]
    decisions, reroll_banner = rank_picker_cards(weak_cards)
    assert all(not d.is_best for d in decisions)
    assert "骰子" in reroll_banner
    assert "C 级" in reroll_banner


def test_rank_picker_cards_gamble_upside_overrides_weak_reroll_guard() -> None:
    weak_cards = [
        (0, _mk_score("20", "弱卡A", delta=-0.015, pct=26.0, below=True)),
        (1, _mk_score("21", "大腕", delta=-0.018, pct=22.0, below=True)),
        (2, _mk_score("22", "弱卡C", delta=-0.028, pct=12.0, below=True)),
    ]
    combo_by_slot = {
        1: PickerCardComboAdvice(
            slot_index=1,
            augment_id="21",
            name="大腕",
            rarity="prismatic",
            verdict="gamble",
            verdict_label="高上限赌狗",
            combo_delta=6.0,
            reason="追珠光护手可核爆",
            combo_name="大腕珠光",
            gamble=True,
        ),
    }
    decisions, reroll_banner = rank_picker_cards(weak_cards, combo_by_slot=combo_by_slot)
    assert reroll_banner == ""
    by_slot = {d.slot_index: d for d in decisions}
    assert by_slot[1].is_best is True
    assert by_slot[1].overridden_by_combo is True
    assert by_slot[1].shift_kind == "gamble"
    assert by_slot[1].shift_label.startswith("高上限 ↑")


def test_rank_picker_cards_applies_pair_deltas_with_sample_confidence() -> None:
    c0 = _mk_score("30", "常规A", delta=0.018, pct=74.0)
    c1 = _mk_score("31", "配对B", delta=0.015, pct=70.0)
    c2 = _mk_score("32", "小样本配对C", delta=0.010, pct=62.0)

    pair_deltas = {
        "31": (0.035, 60),  # Full confidence -> +5.6 -> flips winner over slot 0
        "32": (0.050, 5),   # Below 10-game threshold -> ignored (0.0)
    }
    decisions, reroll_banner = rank_picker_cards(
        [(0, c0), (1, c1), (2, c2)],
        pair_deltas=pair_deltas,
    )
    assert reroll_banner == ""
    by_slot = {d.slot_index: d for d in decisions}
    assert by_slot[1].pair_shift == 5.6
    assert by_slot[1].shift_kind == "synergy"
    assert by_slot[1].shift_label == "联动 ↑5.6"
    assert "60场" in by_slot[1].shift_reason
    assert by_slot[1].is_best is True
    assert by_slot[1].overridden_by_combo is True
    assert by_slot[2].pair_shift == 0.0
    assert by_slot[2].shift_label == ""


def test_rank_picker_cards_handles_empty_low_sample_and_four_slots() -> None:
    assert rank_picker_cards([]) == ([], "")

    # Weak valid cards alongside one missing-sample card must still show valid top tier, not '?'
    mixed_weak = [
        (0, _mk_score("40", "未知卡", delta=0.0, pct=None, games=5, below=False)),
        (1, _mk_score("41", "弱卡1", delta=-0.020, pct=20.0, below=True)),
        (2, _mk_score("42", "弱卡2", delta=-0.025, pct=15.0, below=True)),
    ]
    decisions, reroll_banner = rank_picker_cards(mixed_weak)
    assert all(not d.is_best for d in decisions)
    assert "骰子" in reroll_banner
    assert "?" not in reroll_banner

    # 4-card picker preserves exact input order
    four_cards = [
        (3, _mk_score("53", "卡3", delta=0.005, pct=55.0)),
        (0, _mk_score("50", "卡0", delta=0.028, pct=85.0)),
        (2, _mk_score("52", "卡2", delta=0.012, pct=65.0)),
        (1, _mk_score("51", "卡1", delta=-0.005, pct=40.0)),
    ]
    four_decisions, banner = rank_picker_cards(four_cards)
    assert banner == ""
    assert [d.slot_index for d in four_decisions] == [3, 0, 2, 1]
    assert [d.is_best for d in four_decisions] == [False, True, False, False]
    assert all(not d.overridden_by_combo for d in four_decisions)
