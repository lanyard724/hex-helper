"""Read-only augment selection tracker: GetAsyncKeyState clicks + 33Hz cursor-dwell fallback (ACE-safe)."""

from __future__ import annotations

import ctypes
import math
import time
from collections import deque
from ctypes import wintypes
from typing import Any

from hex_helper.vision.capture import WindowRect

_user32 = ctypes.windll.user32
VK_LBUTTON = 0x01


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def build_slot_hit_boxes(
    game_rect: WindowRect,
    card_boxes: dict[int, tuple[int, int, int, int]] | None = None,
) -> dict[int, tuple[int, int, int, int]]:
    """Build generous full-column hitboxes covering card art, title, description, and Select button."""
    gl, gt, gw, gh = game_rect.left, game_rect.top, game_rect.width, game_rect.height
    # Cover card art (0.16) through the bottom Select button (~0.80), stopping just above Reroll (~0.83+)
    y0 = int(gt + gh * 0.16)
    y1 = int(gt + gh * 0.88)

    boxes: dict[int, tuple[int, int, int, int]] = {
        0: (int(gl + gw * 0.08), y0, int(gl + gw * 0.39), y1),
        1: (int(gl + gw * 0.39), y0, int(gl + gw * 0.61), y1),
        2: (int(gl + gw * 0.61), y0, int(gl + gw * 0.92), y1),
    }
    if card_boxes and len(card_boxes) >= 2:
        ordered = sorted(
            ((slot, (b[0] + b[2]) * 0.5) for slot, b in card_boxes.items()),
            key=lambda kv: kv[1],
        )
        gaps = [ordered[i + 1][1] - ordered[i][1] for i in range(len(ordered) - 1)]
        min_gap = min(gaps) if gaps else gw * 0.24
        half_w = max(int(gw * 0.08), min(int(gw * 0.14), int(min_gap * 0.52)))
        dynamic_boxes: dict[int, tuple[int, int, int, int]] = {}
        for i, (slot, cx) in enumerate(ordered):
            left_x = int((ordered[i - 1][1] + cx) * 0.5) if i > 0 else int(max(gl, cx - half_w))
            right_x = int((cx + ordered[i + 1][1]) * 0.5) if i + 1 < len(ordered) else int(min(gl + gw, cx + half_w))
            dynamic_boxes[slot] = (left_x, y0, right_x, y1)
        return dynamic_boxes
    return boxes


def _slot_at_point(
    x: int,
    y: int,
    card_hit_boxes: dict[int, tuple[int, int, int, int]],
) -> int | None:
    for slot, (l, t, r, b) in card_hit_boxes.items():
        if l <= x <= r and t <= y <= b:
            return slot
    return None


class ClickTracker:
    """Tracks augment card selection using BOTH GetAsyncKeyState clicks AND read-only GetCursorPos dwell.

    Tencent ACE (SGuard64) often zeroes GetAsyncKeyState for external apps while LoL is foreground,
    so recording a 33Hz rolling cursor-slot trajectory lets us identify the exact clicked card the
    moment the picker disappears — 100% read-only with zero hooks.
    """

    def __init__(self, history_seconds: float = 3.0) -> None:
        self._history_seconds = history_seconds
        self._was_down = False
        self.last_clicked_slot: int | None = None
        self.last_click_ts: float | None = None
        self.last_click_pos: tuple[int, int] | None = None
        self.last_hovered_slot: int | None = None
        self.last_resolve_confidence: float = 0.0
        self.last_reroll_mono: float = 0.0
        self._hover_history: deque[tuple[float, int | None, bool, tuple[int, int] | None]] = deque()
        self._reset_ts: float = 0.0
        self._last_rerolled_slot: int | None = None
        self._last_reroll_ts: float = 0.0
        self._poll_count: int = 0
        self._any_lbutton_state_nonzero: bool = False
        self._click_events_seen: int = 0
        self._last_reroll_y_threshold: int | None = None
        self._last_game_rect: WindowRect | None = None
        self._last_hit_boxes: dict[int, tuple[int, int, int, int]] = {}
        self._last_fast_vanish_diffs: dict[int, float] = {}
        self._last_fast_vanish_ts: float | None = None
        self._last_resolve_diagnostics: dict[str, Any] = {}

    def reset(self, now_mono: float | None = None) -> None:
        """Clear stale click latch and hover history when picker opens."""
        try:
            _user32.GetAsyncKeyState(VK_LBUTTON)
        except Exception:
            pass
        self._was_down = False
        self.last_clicked_slot = None
        self.last_click_ts = None
        self.last_click_pos = None
        self.last_hovered_slot = None
        self.last_resolve_confidence = 0.0
        self.last_reroll_mono = 0.0
        self._hover_history.clear()
        self._reset_ts = time.monotonic() if now_mono is None else now_mono
        self._last_rerolled_slot = None
        self._last_reroll_ts = 0.0
        self._poll_count = 0
        self._any_lbutton_state_nonzero = False
        self._click_events_seen = 0
        self._last_reroll_y_threshold = None
        self._last_game_rect = None
        self._last_hit_boxes = {}
        self._last_fast_vanish_diffs = {}
        self._last_fast_vanish_ts = None
        self._last_resolve_diagnostics = {}

    def mark_reroll(self, slot: int | None = None, now_mono: float | None = None) -> None:
        """Clear stale click latch on reroll WITHOUT erasing live cursor travel toward the next card."""
        ts = time.monotonic() if now_mono is None else now_mono
        self.last_clicked_slot = None
        self.last_click_ts = None
        self._last_rerolled_slot = slot
        self._last_reroll_ts = ts
        self.last_reroll_mono = ts

    def note_reroll_event(self, ts_mono: float | None = None, slot: int | None = None) -> None:
        """Record a reroll timestamp (monotonic seconds) so `ms_since_last_reroll` stays accurate."""
        ts = time.monotonic() if ts_mono is None else float(ts_mono)
        self.last_reroll_mono = ts
        self._last_reroll_ts = ts
        if slot is not None:
            self._last_rerolled_slot = slot

    def note_fast_vanish_diffs(
        self,
        slot_diffs: dict[int, float],
        vanish_ts: float | None = None,
    ) -> None:
        """Record per-slot visual vanish frame-diff scores for inclusion in resolve diagnostics."""
        self._last_fast_vanish_diffs = {int(k): round(float(v), 3) for k, v in slot_diffs.items()}
        self._last_fast_vanish_ts = vanish_ts

    def last_resolve_diagnostics(self) -> dict[str, Any]:
        """Return a shallow copy of structured diagnostics from the most recent `resolve_selection`."""
        return dict(self._last_resolve_diagnostics)

    def record_cursor_sample(
        self,
        now_mono: float,
        slot: int | None,
        clicked: bool = False,
        pos: tuple[int, int] | None = None,
        in_reroll_zone: bool = False,
    ) -> None:
        """Deterministic ingestion entrypoint (used both by live poll() and unit tests)."""
        self.last_hovered_slot = slot
        self._hover_history.append((now_mono, slot, in_reroll_zone, pos))
        cutoff = now_mono - self._history_seconds
        while self._hover_history and self._hover_history[0][0] < cutoff:
            self._hover_history.popleft()

        if clicked:
            self._click_events_seen += 1
            self.last_click_pos = pos
            if slot is not None and not in_reroll_zone:
                self.last_clicked_slot = slot
                self.last_click_ts = now_mono

    def poll(
        self,
        card_hit_boxes: dict[int, tuple[int, int, int, int]],
        now_mono: float | None = None,
        reroll_y_threshold: int | None = None,
        game_rect: WindowRect | None = None,
    ) -> int | None:
        """Poll cursor position + left mouse button and record sample into rolling history."""
        ts = time.monotonic() if now_mono is None else now_mono
        self._poll_count += 1
        self._last_hit_boxes = dict(card_hit_boxes)
        if reroll_y_threshold is not None:
            self._last_reroll_y_threshold = int(reroll_y_threshold)
        if game_rect is not None:
            self._last_game_rect = game_rect

        pt = _POINT()
        pos: tuple[int, int] | None = None
        hovered_slot: int | None = None
        in_reroll_zone = False
        if _user32.GetCursorPos(ctypes.byref(pt)):
            cx, cy = int(pt.x), int(pt.y)
            pos = (cx, cy)
            hovered_slot = _slot_at_point(cx, cy, card_hit_boxes)
            if reroll_y_threshold is not None and cy >= reroll_y_threshold:
                in_reroll_zone = True

        state = int(_user32.GetAsyncKeyState(VK_LBUTTON))
        if state != 0:
            self._any_lbutton_state_nonzero = True
        is_down = bool(state & 0x8000)
        clicked_since_last = bool(state & 0x0001)
        clicked = (is_down and not self._was_down) or clicked_since_last
        self._was_down = is_down

        self.record_cursor_sample(ts, hovered_slot, clicked=clicked, pos=pos, in_reroll_zone=in_reroll_zone)
        return self.last_clicked_slot

    @staticmethod
    def _resolve_fast_vanish_tiebreak(
        vanish_diffs: dict[int, float],
        valid_slots: set[int] | None = None,
    ) -> tuple[int, float, float] | None:
        """Return (winner_slot, top1, top2) if fast_vanish_diffs has a clear >=1.35x winner."""
        if not vanish_diffs:
            return None
        filtered = [
            (int(s), float(d))
            for s, d in vanish_diffs.items()
            if (valid_slots is None or int(s) in valid_slots) and float(d) > 0.0
        ]
        if not filtered:
            return None
        filtered.sort(key=lambda kv: kv[1], reverse=True)
        top1_slot, top1_val = filtered[0]
        top2_val = filtered[1][1] if len(filtered) > 1 else 0.0
        if top1_val > 1e-3 and top1_val >= 1.35 * max(0.0, top2_val):
            return top1_slot, top1_val, top2_val
        return None

    def _compute_close_cursor_context(
        self,
        hist: list[tuple[float, int | None, bool, tuple[int, int] | None]],
        close_ts: float,
        game_rect: WindowRect | None,
        reroll_y_threshold: int | None,
    ) -> dict[str, Any]:
        eff_rect = game_rect if game_rect is not None else self._last_game_rect
        eff_reroll_y = reroll_y_threshold if reroll_y_threshold is not None else self._last_reroll_y_threshold

        close_sample: tuple[float, int | None, bool, tuple[int, int] | None] | None = None
        if hist:
            up_to_close = [item for item in hist if item[0] <= close_ts + 0.08]
            pool = up_to_close if up_to_close else hist
            close_sample = min(pool, key=lambda item: abs(item[0] - close_ts))

        close_pos = close_sample[3] if close_sample is not None else self.last_click_pos
        close_in_reroll = bool(close_sample[2]) if close_sample is not None else False
        close_slot = close_sample[1] if close_sample is not None else None

        rel_x: float | None = None
        rel_y: float | None = None
        reroll_y_rel: float | None = None

        if eff_rect is not None and eff_rect.width > 0 and eff_rect.height > 0:
            if close_pos is not None:
                rel_x = round((close_pos[0] - eff_rect.left) / float(eff_rect.width), 4)
                rel_y = round((close_pos[1] - eff_rect.top) / float(eff_rect.height), 4)
            if eff_reroll_y is not None:
                reroll_y_rel = round((eff_reroll_y - eff_rect.top) / float(eff_rect.height), 4)
        elif self._last_hit_boxes:
            y0 = min(b[1] for b in self._last_hit_boxes.values())
            y1 = max(b[3] for b in self._last_hit_boxes.values())
            if y1 > y0:
                est_gh = (y1 - y0) / 0.72
                est_gt = y0 - 0.16 * est_gh
                if close_pos is not None:
                    rel_y = round((close_pos[1] - est_gt) / est_gh, 4)
                if eff_reroll_y is not None:
                    reroll_y_rel = round((eff_reroll_y - est_gt) / est_gh, 4)

        return {
            "cursor_pos_at_close": close_pos,
            "cursor_rel_x": rel_x,
            "cursor_rel_y": rel_y,
            "cursor_slot_at_close": close_slot,
            "in_reroll_zone": close_in_reroll,
            "reroll_y_threshold": eff_reroll_y,
            "reroll_y_threshold_rel": reroll_y_rel,
        }

    def resolve_selection(
        self,
        close_ts: float,
        valid_slots: set[int] | None = None,
        rerolled_slot: int | None = None,
        reroll_ts: float | None = None,
        game_rect: WindowRect | None = None,
        fast_vanish_diffs: dict[int, float] | None = None,
        reroll_y_threshold: int | None = None,
    ) -> tuple[int | None, str]:
        """Resolve which card slot the player picked around `close_ts` (the moment picker vanished).

        Prioritizes:
        1. Direct GetAsyncKeyState click (outside reroll button strip) -> confidence 1.0.
        2. Decisive-dwell hysteresis on tight final-moment window `[close_ts - 0.36, close_ts + 0.08]`:
           identifies decisive click-pauses (`>= 3` slow samples `<= 220 px/s` in `[close_ts - 0.85, close_ts + 0.05]`),
           heavily penalizes final fast fly-throughs (`> 480 px/s` in `[close_ts - 0.14, close_ts + 0.08]` at `0.15x`)
           so swiping back to lane right after clicking NEVER overwrites the real clicked card.
        3. Wider dwell window `[close_ts - 1.15, close_ts + 0.22]` with discounted reroll-zone samples.
        """
        self.last_resolve_confidence = 0.0
        eff_reroll_slot = rerolled_slot if rerolled_slot is not None else self._last_rerolled_slot
        eff_reroll_ts = reroll_ts if reroll_ts is not None else self._last_reroll_ts

        diag_reroll_ts = max(
            float(reroll_ts or 0.0),
            float(self._last_reroll_ts or 0.0),
            float(self.last_reroll_mono or 0.0),
        )
        ms_since_last_reroll: float | None = (
            round((close_ts - diag_reroll_ts) * 1000.0, 1)
            if diag_reroll_ts > 0.0 and close_ts >= diag_reroll_ts
            else None
        )
        eff_vanish_diffs = (
            {int(k): round(float(v), 3) for k, v in fast_vanish_diffs.items()}
            if fast_vanish_diffs is not None
            else dict(self._last_fast_vanish_diffs)
        )
        hist = list(self._hover_history)
        close_ctx = self._compute_close_cursor_context(
            hist=hist,
            close_ts=close_ts,
            game_rect=game_rect,
            reroll_y_threshold=reroll_y_threshold,
        )
        ace_blocked_suspected = bool(
            self._poll_count >= 3 and not self._any_lbutton_state_nonzero and self._click_events_seen == 0
        )

        if (
            self.last_clicked_slot is not None
            and self.last_click_ts is not None
            and self.last_click_ts >= max(self._reset_ts, eff_reroll_ts)
            and (close_ts - 2.5) <= self.last_click_ts <= (close_ts + 0.6)
            and (valid_slots is None or self.last_clicked_slot in valid_slots)
        ):
            self.last_resolve_confidence = 1.0
            self._last_resolve_diagnostics = {
                "chosen_slot": self.last_clicked_slot,
                "chosen_source": "click",
                "chosen_tier": "tier1_click",
                "confidence": 1.0,
                "ace_blocked_suspected": False,
                "get_async_key_polls": self._poll_count,
                "get_async_key_nonzero": self._any_lbutton_state_nonzero,
                "fast_vanish_diffs": eff_vanish_diffs,
                "cursor_rel_x": close_ctx["cursor_rel_x"],
                "cursor_rel_y": close_ctx["cursor_rel_y"],
                "cursor_slot_at_close": close_ctx["cursor_slot_at_close"],
                "in_reroll_zone": close_ctx["in_reroll_zone"],
                "reroll_y_threshold": close_ctx["reroll_y_threshold"],
                "reroll_y_threshold_rel": close_ctx["reroll_y_threshold_rel"],
                "ms_since_last_reroll": ms_since_last_reroll,
                "rerolled_slot": eff_reroll_slot,
                "left_rerolled_slot": False,
                "decisive_slots": [],
                "per_slot_scores": {},
                "tier_scores": {},
                "tier2a_skipped_flythrough": False,
                "history_sample_count": len(hist),
                "has_pos_samples": any(item[3] is not None for item in hist),
            }
            return self.last_clicked_slot, "click"

        # Pre-compute instantaneous cursor speeds (px/s) when (x, y) coordinates are available
        speeds: list[float | None] = [None] * len(hist)
        for i in range(1, len(hist)):
            t0, _, _, p0 = hist[i - 1]
            t1, _, _, p1 = hist[i]
            dt = t1 - t0
            if p0 is not None and p1 is not None and 0.004 <= dt <= 0.40:
                speeds[i] = math.hypot(p1[0] - p0[0], p1[1] - p0[1]) / dt
        # Forward-fill start-of-segment speed on the same slot so a 3-sample pause from index 0 counts all 3
        for i in range(len(hist) - 1):
            if speeds[i] is None and speeds[i + 1] is not None and hist[i][1] == hist[i + 1][1]:
                speeds[i] = speeds[i + 1]

        has_pos_samples = any(s is not None for s in speeds)

        # Compute per-slot deceleration-pause evidence (<= 220 px/s, non-reroll) in [close_ts - 0.85, close_ts + 0.05]
        pause_t_start = max(self._reset_ts, eff_reroll_ts, close_ts - 0.85)
        pause_t_end = close_ts + 0.05
        slow_counts: dict[int, int] = {}
        for idx, (ts, slot, in_reroll, _pos) in enumerate(hist):
            if slot is None or in_reroll or ts < pause_t_start or ts > pause_t_end:
                continue
            if valid_slots is not None and slot not in valid_slots:
                continue
            spd = speeds[idx]
            if spd is not None and spd <= 220.0:
                slow_counts[slot] = slow_counts.get(slot, 0) + 1
        decisive_slots: set[int] = {s for s, cnt in slow_counts.items() if cnt >= 3}

        # Check if player deliberately left the just-rerolled slot onto another card right before closing
        # (Exclude fast fly-through > 480 px/s so clicking the rerolled slot and swiping away doesn't discount it)
        left_rerolled_slot = False
        if eff_reroll_slot is not None and (close_ts - eff_reroll_ts) <= 2.8:
            other_recent = sum(
                1
                for idx, (ts, slot, in_reroll, _pos) in enumerate(hist)
                if slot is not None
                and slot != eff_reroll_slot
                and not in_reroll
                and (valid_slots is None or slot in valid_slots)
                and (close_ts - 0.24) <= ts <= (close_ts + 0.08)
                and (speeds[idx] is None or speeds[idx] <= 480.0)
            )
            if other_recent >= 2:
                left_rerolled_slot = True

        tier_scores_diag: dict[str, dict[int, dict[str, Any]]] = {}
        last_slot_scores: dict[int, dict[str, Any]] = {}
        tier2a_skipped_flythrough = False

        # Tier 2a: Tight final-moment click-pause window (never accepts reroll-button strip)
        # Tier 2b: Wider fallback if mouse flicked away early after a decisive click-pause
        for tier_key, win_before, win_after, allow_reroll in (
            ("tier2a_tight_dwell", 0.36, 0.08, False),
            ("tier2b_wide_dwell", 1.15, 0.22, True),
        ):
            scores: dict[int, float] = {}
            counts: dict[int, int] = {}
            win_slow_counts: dict[int, int] = {}
            win_fast_counts: dict[int, int] = {}
            win_flythrough_counts: dict[int, int] = {}
            t_start = max(self._reset_ts, close_ts - win_before)
            t_end = close_ts + win_after

            for idx, (ts, slot, in_reroll, _pos) in enumerate(hist):
                if slot is None or ts < t_start or ts > t_end:
                    continue
                if valid_slots is not None and slot not in valid_slots:
                    continue
                if in_reroll and not allow_reroll:
                    continue
                # Peak weight right at the click instant (~60ms before visual vanish)
                dist = abs(ts - (close_ts - 0.06))
                weight = max(0.15, 2.2 - dist * 3.0)
                spd = speeds[idx]

                has_other_decisive = any(d != slot for d in decisive_slots)
                in_final_flythrough_win = (close_ts - 0.14) <= ts <= (close_ts + 0.08)
                is_flythrough_override = (
                    has_other_decisive
                    and slot not in decisive_slots
                    and in_final_flythrough_win
                    and spd is not None
                    and spd > 480.0
                )

                if is_flythrough_override:
                    weight *= 0.15
                    win_flythrough_counts[slot] = win_flythrough_counts.get(slot, 0) + 1
                elif spd is not None:
                    if spd <= 220.0:
                        weight *= 2.5
                    elif spd <= 260.0:
                        weight *= 2.2
                    elif spd > 650.0:
                        weight *= 0.22
                    elif spd > 480.0:
                        weight *= 0.45

                if spd is not None:
                    if spd <= 220.0:
                        win_slow_counts[slot] = win_slow_counts.get(slot, 0) + 1
                    elif spd > 480.0:
                        win_fast_counts[slot] = win_fast_counts.get(slot, 0) + 1

                if in_reroll:
                    weight *= 0.25
                if left_rerolled_slot and slot == eff_reroll_slot and ts < (close_ts - 0.14):
                    weight *= 0.20

                scores[slot] = scores.get(slot, 0.0) + weight
                counts[slot] = counts.get(slot, 0) + 1

            slot_breakdown = {
                s: {
                    "score": round(sc, 3),
                    "samples": counts.get(s, 0),
                    "slow_samples": win_slow_counts.get(s, 0),
                    "fast_samples": win_fast_counts.get(s, 0),
                    "flythrough_penalized": win_flythrough_counts.get(s, 0),
                    "decisive": s in decisive_slots,
                }
                for s, sc in sorted(scores.items())
            }
            tier_scores_diag[tier_key] = slot_breakdown
            last_slot_scores = slot_breakdown

            if scores:
                ranked = sorted(scores.items(), key=lambda kv: (kv[1], counts.get(kv[0], 0)), reverse=True)
                best_slot, top1 = ranked[0]
                if counts.get(best_slot, 0) >= 2:
                    # Do not let a pure fast fly-through win Tier 2a when another slot holds a decisive click-pause
                    if (
                        win_before <= 0.40
                        and best_slot not in decisive_slots
                        and any(d != best_slot for d in decisive_slots)
                        and win_slow_counts.get(best_slot, 0) == 0
                        and win_fast_counts.get(best_slot, 0) > 0
                    ):
                        tier2a_skipped_flythrough = True
                        continue

                    top2 = ranked[1][1] if len(ranked) > 1 else 0.0
                    margin_ratio = (top1 / (top1 + top2)) if (top1 + top2) > 0.0 else 0.0
                    if has_pos_samples:
                        win_slow = slow_counts.get(best_slot, 0)
                        slow_scale = 1.0 if win_slow >= 4 else min(1.0, 0.45 + 0.18 * win_slow)
                    else:
                        win_cnt = counts.get(best_slot, 0)
                        slow_scale = min(0.92, 0.50 + 0.08 * win_cnt)
                    conf = round(max(0.0, min(1.0, margin_ratio * slow_scale)), 4)
                    chosen_slot = best_slot
                    chosen_source = "cursor_dwell"
                    chosen_tier = tier_key
                    vanish_tiebreak_used = False
                    if conf < 0.72:
                        vb = self._resolve_fast_vanish_tiebreak(eff_vanish_diffs, valid_slots=valid_slots)
                        if vb is not None:
                            v_slot, v_top1, v_top2 = vb
                            vanish_tiebreak_used = True
                            v_ratio = v_top1 / max(1e-6, v_top1 + v_top2)
                            if v_slot != best_slot:
                                chosen_slot = v_slot
                                chosen_source = "fast_vanish_tiebreak"
                                chosen_tier = f"{tier_key}+fast_vanish_tiebreak"
                                conf = round(max(conf, min(0.76, v_ratio)), 4)
                            else:
                                conf = round(max(conf, min(0.78, v_ratio)), 4)
                    self.last_resolve_confidence = conf
                    self._last_resolve_diagnostics = {
                        "chosen_slot": chosen_slot,
                        "chosen_source": chosen_source,
                        "chosen_tier": chosen_tier,
                        "confidence": self.last_resolve_confidence,
                        "ace_blocked_suspected": ace_blocked_suspected,
                        "get_async_key_polls": self._poll_count,
                        "get_async_key_nonzero": self._any_lbutton_state_nonzero,
                        "fast_vanish_diffs": eff_vanish_diffs,
                        "fast_vanish_tiebreak_used": vanish_tiebreak_used,
                        "cursor_rel_x": close_ctx["cursor_rel_x"],
                        "cursor_rel_y": close_ctx["cursor_rel_y"],
                        "cursor_slot_at_close": close_ctx["cursor_slot_at_close"],
                        "in_reroll_zone": close_ctx["in_reroll_zone"],
                        "reroll_y_threshold": close_ctx["reroll_y_threshold"],
                        "reroll_y_threshold_rel": close_ctx["reroll_y_threshold_rel"],
                        "ms_since_last_reroll": ms_since_last_reroll,
                        "rerolled_slot": eff_reroll_slot,
                        "left_rerolled_slot": left_rerolled_slot,
                        "decisive_slots": sorted(decisive_slots),
                        "per_slot_scores": slot_breakdown,
                        "tier_scores": tier_scores_diag,
                        "tier2a_skipped_flythrough": tier2a_skipped_flythrough,
                        "history_sample_count": len(hist),
                        "has_pos_samples": has_pos_samples,
                    }
                    return chosen_slot, chosen_source

        vb_fallback = self._resolve_fast_vanish_tiebreak(eff_vanish_diffs, valid_slots=valid_slots)
        if vb_fallback is not None:
            v_slot, v_top1, v_top2 = vb_fallback
            v_conf = round(min(0.75, max(0.55, v_top1 / max(1e-6, v_top1 + v_top2))), 4)
            self.last_resolve_confidence = v_conf
            self._last_resolve_diagnostics = {
                "chosen_slot": v_slot,
                "chosen_source": "fast_vanish_tiebreak",
                "chosen_tier": "tier3_fast_vanish_tiebreak",
                "confidence": v_conf,
                "ace_blocked_suspected": ace_blocked_suspected,
                "get_async_key_polls": self._poll_count,
                "get_async_key_nonzero": self._any_lbutton_state_nonzero,
                "fast_vanish_diffs": eff_vanish_diffs,
                "fast_vanish_tiebreak_used": True,
                "cursor_rel_x": close_ctx["cursor_rel_x"],
                "cursor_rel_y": close_ctx["cursor_rel_y"],
                "cursor_slot_at_close": close_ctx["cursor_slot_at_close"],
                "in_reroll_zone": close_ctx["in_reroll_zone"],
                "reroll_y_threshold": close_ctx["reroll_y_threshold"],
                "reroll_y_threshold_rel": close_ctx["reroll_y_threshold_rel"],
                "ms_since_last_reroll": ms_since_last_reroll,
                "rerolled_slot": eff_reroll_slot,
                "left_rerolled_slot": left_rerolled_slot,
                "decisive_slots": sorted(decisive_slots),
                "per_slot_scores": last_slot_scores,
                "tier_scores": tier_scores_diag,
                "tier2a_skipped_flythrough": tier2a_skipped_flythrough,
                "history_sample_count": len(hist),
                "has_pos_samples": has_pos_samples,
            }
            return v_slot, "fast_vanish_tiebreak"

        self.last_resolve_confidence = 0.0
        self._last_resolve_diagnostics = {
            "chosen_slot": None,
            "chosen_source": "",
            "chosen_tier": "fallback_none",
            "confidence": 0.0,
            "ace_blocked_suspected": ace_blocked_suspected,
            "get_async_key_polls": self._poll_count,
            "get_async_key_nonzero": self._any_lbutton_state_nonzero,
            "fast_vanish_diffs": eff_vanish_diffs,
            "fast_vanish_tiebreak_used": False,
            "cursor_rel_x": close_ctx["cursor_rel_x"],
            "cursor_rel_y": close_ctx["cursor_rel_y"],
            "cursor_slot_at_close": close_ctx["cursor_slot_at_close"],
            "in_reroll_zone": close_ctx["in_reroll_zone"],
            "reroll_y_threshold": close_ctx["reroll_y_threshold"],
            "reroll_y_threshold_rel": close_ctx["reroll_y_threshold_rel"],
            "ms_since_last_reroll": ms_since_last_reroll,
            "rerolled_slot": eff_reroll_slot,
            "left_rerolled_slot": left_rerolled_slot,
            "decisive_slots": sorted(decisive_slots),
            "per_slot_scores": last_slot_scores,
            "tier_scores": tier_scores_diag,
            "tier2a_skipped_flythrough": tier2a_skipped_flythrough,
            "history_sample_count": len(hist),
            "has_pos_samples": has_pos_samples,
        }
        return None, ""
