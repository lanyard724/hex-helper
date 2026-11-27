"""Pure-Python read-only match action differ and real-time causal attribution engine.

Zero Qt imports and zero ``hex_helper.devlab`` imports so this module stays
lightweight, testable, and safe under commercial build isolation rules.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable, Sequence

from hex_helper.game.models import ChampSelectState, LiveMatchSnapshot, PlayerLiveInfo

EmitFn = Callable[[str, str, dict[str, Any], float | None], None]

# Trinkets, free ARAM Poro-Snax (including Mayhem 220013), and stat-anvil placeholders
IGNORED_ITEM_IDS: frozenset[int] = frozenset(
    {
        1402,  # Stat Anvil placeholder
        1403,  # Stat Anvil placeholder
        1404,  # Stat Anvil placeholder
        2052,  # Poro-Snax
        2054,  # Diet Poro-Snax
        3330,  # Scarecrow Effigy
        3340,  # Stealth Ward
        3363,  # Farsight Alteration
        3364,  # Oracle Lens
        3513,  # Eye of the Herald
        220013,  # ARAM Mayhem Poro-Snax
    }
)

CONSUMABLE_ITEM_IDS: frozenset[int] = frozenset(
    {
        2003,  # Health Potion
        2010,  # Total Biscuit of Everlasting Will
        2031,  # Refillable Potion
        2055,  # Control Ward
        2138,  # Elixir of Iron
        2139,  # Elixir of Sorcery
        2140,  # Elixir of Wrath
    }
)

TIER2_BOOT_IDS: frozenset[int] = frozenset(
    {
        3006,
        3009,
        3010,
        3013,
        3020,
        3047,
        3111,
        3158,
        3170,
        3171,
        3172,
        3173,
        3174,
        3175,
        3176,
    }
)

# Tactical enemy milestone item groups
ANTI_HEAL_ITEM_IDS: frozenset[int] = frozenset({3033, 3075, 3076, 3123, 3165, 3916, 6609})
ARMOR_PEN_ITEM_IDS: frozenset[int] = frozenset({3032, 3033, 3035, 3036, 3071, 3302, 6694})
MR_PEN_ITEM_IDS: frozenset[int] = frozenset({3118, 3135, 3137, 3302, 4630, 4645})
STASIS_ITEM_IDS: frozenset[int] = frozenset({2420, 2421, 3157, 3191})
SHIELD_CUT_ITEM_IDS: frozenset[int] = frozenset({6695})
QSS_SPELLSHIELD_ITEM_IDS: frozenset[int] = frozenset({3102, 3139, 3140, 3814})

_ALL_TACTICAL_ENEMY_ITEM_IDS: frozenset[int] = (
    ANTI_HEAL_ITEM_IDS
    | ARMOR_PEN_ITEM_IDS
    | MR_PEN_ITEM_IDS
    | STASIS_ITEM_IDS
    | SHIELD_CUT_ITEM_IDS
    | QSS_SPELLSHIELD_ITEM_IDS
)

# Mid-tier components that should not trigger enemy completed-item milestones when price is unknown
_NON_COMPLETE_MID_COMPONENT_IDS: frozenset[int] = frozenset(
    {
        1001,
        2051,
        3024,
        3044,
        3057,
        3067,
        3070,
        3077,
        3082,
        3086,
        3108,
        3112,
        3113,
        3133,
        3134,
        3144,
        3145,
        3155,
        3177,
        3184,
        3211,
        3802,
        6660,
    }
)

_ITEM_EQUIVALENCE: dict[int, int] = {
    3040: 3003,  # Seraph's Embrace <-> Archangel's Staff
    3042: 3004,  # Muramana <-> Manamune
    3121: 3119,  # Fimbulwinter <-> Winter's Approach
    2530: 2526,
}

# ARAM Mayhem augment unlock milestones: (champion_level, expected_augment_slots)
AUGMENT_UNLOCK_MILESTONES: tuple[tuple[int, int], ...] = (
    (3, 1),
    (7, 2),
    (11, 3),
    (15, 4),
)

_DODGE_NEXT_PHASES: frozenset[str] = frozenset({"Lobby", "Matchmaking", "None", "ReadyCheck"})
_LOCKED_NEXT_PHASES: frozenset[str] = frozenset({"InProgress", "GameStart", "Reconnect"})
_ALWAYS_EMIT_LIVE_EVENTS: frozenset[str] = frozenset(
    {"FirstBlood", "Multikill", "Ace", "TurretKilled", "InhibKilled", "GameEnd"}
)
_DEATH_WINDOW_COACH_TRIGGERS: frozenset[str] = frozenset(
    {"death_enemy_build_changed", "manual_f6"}
)


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if math.isfinite(val) else default
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            try:
                fval = float(text)
            except ValueError:
                return default
            return int(fval) if math.isfinite(fval) else default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _is_ignored_item(item_id: int) -> bool:
    if item_id <= 0 or item_id in IGNORED_ITEM_IDS:
        return True
    s = str(item_id)
    if len(s) == 6 and s[:2] in ("12", "22", "32", "44", "77", "99"):
        stripped = _safe_int(s[2:], 0)
        if stripped in IGNORED_ITEM_IDS:
            return True
    return False


def _canonical_item_id(item_id: int) -> int:
    if item_id <= 0:
        return 0
    s = str(item_id)
    if len(s) == 6 and s[:2] in ("12", "22", "32", "44", "77", "99"):
        stripped = _safe_int(s[2:], 0)
        if stripped > 0:
            item_id = stripped
    return _ITEM_EQUIVALENCE.get(item_id, item_id)


def _is_augment_granted_special_item(item_id: str | int, price: int = 0) -> bool:
    """Return True for 6-digit ARAM Mayhem augment-upgraded (22xxxx) or Prismatic-anvil (99xxxx) items."""
    _ = price  # Reserved for future cost-aware heuristics
    raw_str = str(item_id if item_id is not None else "").strip()
    if not raw_str:
        return False
    if raw_str.isdigit():
        digits = raw_str
    else:
        val = _safe_int(raw_str, 0)
        if val <= 0:
            return False
        digits = str(val)
    if len(digits) != 6 or digits == "220013":
        return False
    return digits.startswith("22") or digits.startswith("99")


def _hero_key(player: PlayerLiveInfo) -> str:
    raw = str(player.raw_champion_name or "").strip()
    if raw:
        prefix = "game_character_displayname_"
        if raw.lower().startswith(prefix):
            return raw[len(prefix):]
        return raw
    return str(player.champion_name or "").strip()


def _extract_inventory(player: PlayerLiveInfo) -> tuple[Counter[int], dict[int, dict[str, Any]]]:
    """Return (item_counts, item_meta_by_id) with ignored trinkets/anvils filtered out."""
    counts: Counter[int] = Counter()
    meta: dict[int, dict[str, Any]] = {}

    if player.item_details:
        for entry in player.item_details:
            if not isinstance(entry, dict):
                continue
            iid = _safe_int(entry.get("item_id"), 0)
            if _is_ignored_item(iid):
                continue
            cnt = max(1, _safe_int(entry.get("count"), 1))
            price = max(0, _safe_int(entry.get("price"), 0))
            consumable = bool(entry.get("consumable", False)) or (iid in CONSUMABLE_ITEM_IDS)
            can_use = bool(entry.get("can_use", False))
            counts[iid] += cnt
            prev = meta.get(iid)
            if prev is None or price > prev["price"]:
                meta[iid] = {
                    "price": price,
                    "consumable": consumable,
                    "can_use": can_use,
                }
        return counts, meta

    # Fallback for snapshots built without item_details
    for raw_iid in player.item_ids:
        iid = _safe_int(raw_iid, 0)
        if _is_ignored_item(iid):
            continue
        counts[iid] += 1
        meta.setdefault(
            iid,
            {
                "price": 0,
                "consumable": iid in CONSUMABLE_ITEM_IDS,
                "can_use": False,
            },
        )
    return counts, meta


@dataclass(frozen=True)
class _LocalRecRecord:
    seq: int
    ts_mono: float
    game_time_s: float
    item_ids: tuple[int, ...]
    norm_item_ids: tuple[int, ...]
    boots_rec: int
    norm_boots_rec: int
    hero_profile: str
    combo_hero_profile: str
    pivot_banner: str
    source: str


@dataclass(frozen=True)
class _CoachAdviceRecord:
    seq: int
    ts_mono: float
    game_time_s: float
    request_id: str
    trigger_reason: str
    item_ids: tuple[int, ...]
    norm_item_ids: tuple[int, ...]
    recommended_augment_id: str
    latency_ms: float


@dataclass
class _PendingPurchase:
    item_id: int
    action: str  # "buy" | "upgrade"
    price: int
    gold_delta: float
    spent_gold: float
    consumed_components: tuple[int, ...]
    purchase_game_time_s: float
    purchase_mono: float
    purchase_seq: int
    in_death_window: bool
    local_rec: _LocalRecRecord | None
    coach_advice: _CoachAdviceRecord | None


@dataclass
class _DeathShopWindow:
    death_start_time: float
    death_start_mono: float
    gold_at_death: float
    killer_hero: str = ""
    items_bought: list[int] = field(default_factory=list)
    first_buy_seen: bool = False
    first_buy_time: float | None = None
    coach_triggered: bool = False
    coach_skip_reason: str = ""
    warmup_served: bool = False
    coach_arrived: bool = False
    coach_arrived_before_buy: bool = False
    coach_request_id: str = ""
    coach_latency_ms: float = 0.0


@dataclass
class _UnlockTimer:
    milestone_level: int
    expected_slots: int
    reached_game_time_s: float
    reached_mono: float
    armed: bool = True
    picker_opened: bool = False
    alerted: bool = False


class MatchActionTracker:
    """Stateful read-only action differ and real-time recommendation attribution engine."""

    def __init__(
        self,
        emit_fn: EmitFn,
        *,
        clock: Callable[[], float] | None = None,
        undo_window_s: float = 2.0,
        unlock_miss_timeout_s: float = 45.0,
        is_complete_item_fn: Callable[[str], bool] | None = None,
    ) -> None:
        self._emit_fn = emit_fn
        self._clock: Callable[[], float] = clock if clock is not None else time.perf_counter
        self._undo_window_s = max(0.0, float(undo_window_s))
        self._unlock_miss_timeout_s = max(1.0, float(unlock_miss_timeout_s))
        self._is_complete_item_fn: Callable[[str], bool] | None = is_complete_item_fn
        self.reset()

    def reset(self) -> None:
        """Reset all champ-select, live-match, recommendation, and timer states."""
        self._seq: int = 0

        # Champ-select state
        self._in_champ_select: bool = False
        self._cs_last_hero_id: str = ""
        self._cs_last_team: tuple[str, ...] = ()
        self._cs_last_bench: tuple[str, ...] = ()
        self._cs_last_rerolls: int = -1
        self._cs_last_spells: tuple[int, int] = (0, 0)
        self._cs_last_rec_sig: tuple[Any, ...] | None = None
        self._cs_last_best_swap: str = ""
        self._cs_last_top_rec: str = ""
        self._cs_last_goal_mode: str = ""
        self._cs_last_hero_ratings: dict[str, float] = {}
        self._cs_ever_followed_best_swap: bool = False
        self._cs_action_count: int = 0

        # Recommendation histories (bounded)
        self._local_rec_history: deque[_LocalRecRecord] = deque(maxlen=64)
        self._last_local_rec_sig: tuple[Any, ...] | None = None
        self._coach_advice_history: deque[_CoachAdviceRecord] = deque(maxlen=32)
        self._last_plan_override_sig: tuple[Any, ...] | None = None

        # Live inventory & 2s undo buffer
        self._live_initialized: bool = False
        self._prev_my_items: Counter[int] = Counter()
        self._prev_item_meta: dict[int, dict[str, Any]] = {}
        self._prev_gold: float = 0.0
        self._last_game_time_s: float = 0.0
        self._pending_purchases: list[_PendingPurchase] = []
        self._attributed_completed_items: set[str] = set()

        # Enemy milestones & incremental event cursor
        self._enemy_seen_items: dict[str, set[int]] = {}
        self._last_event_id: int = -1
        self._last_my_death_killer: str = ""
        self._last_my_death_event_time: float = -999.0

        # Death-shop window
        self._was_dead: bool = False
        self._death_window: _DeathShopWindow | None = None
        self._death_warmup_served: bool = False
        self._pending_death_decision: tuple[bool, str, bool, float] | None = None

        # Level & augment unlock silent-miss timers
        self._emitted_level_milestones: set[int] = set()
        self._unlock_timers: dict[int, _UnlockTimer] = {}
        self._last_picker_opened_game_time: float = -1.0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(
        self,
        domain: str,
        event_type: str,
        payload: dict[str, Any],
        game_time_s: float | None = None,
    ) -> None:
        self._emit_fn(domain, event_type, payload, game_time_s)

    def _clear_champ_select_state(self) -> None:
        self._in_champ_select = False
        self._cs_last_hero_id = ""
        self._cs_last_team = ()
        self._cs_last_bench = ()
        self._cs_last_rerolls = -1
        self._cs_last_spells = (0, 0)
        self._cs_last_rec_sig = None
        self._cs_last_best_swap = ""
        self._cs_last_top_rec = ""
        self._cs_last_goal_mode = ""
        self._cs_last_hero_ratings = {}
        self._cs_ever_followed_best_swap = False
        self._cs_action_count = 0

    def _resolve_top_recommended_hero(
        self,
        my_hero: str,
        bench: tuple[str, ...],
        best_swap: str,
        hero_ratings: dict[str, float] | None,
        team_comp_analysis: dict[str, Any] | None,
    ) -> str:
        if best_swap:
            return best_swap
        if isinstance(team_comp_analysis, dict):
            top_from_comp = str(
                team_comp_analysis.get("top_recommended_hero")
                or team_comp_analysis.get("best_swap_hero_id")
                or ""
            ).strip()
            if top_from_comp:
                return top_from_comp
        # Empty best_swap means the recommender advises keeping the current hero
        if my_hero:
            return my_hero
        if hero_ratings:
            candidates = [h for h in list(bench) if h and h in hero_ratings]
            pool = candidates if candidates else list(hero_ratings.keys())
            if pool:
                return str(max(pool, key=lambda h: float(hero_ratings[h])))
        return my_hero

    # ------------------------------------------------------------------
    # 1. Champ Select Tracking
    # ------------------------------------------------------------------
    def on_lcu_champ_select(
        self,
        phase: str,
        cs: ChampSelectState | None,
        *,
        team_comp_analysis: dict[str, Any] | None = None,
        goal_mode: str = "",
        best_swap_hero_id: str = "",
        hero_ratings: dict[str, float] | None = None,
    ) -> None:
        """Track ChampSelect diffs (reroll, bench swap, spell change, dodge, lock) and deduped recs."""
        phase_str = str(phase or "").strip()

        if phase_str == "ChampSelect" and cs is not None:
            my_hero = str(cs.my_hero_id or "").strip()
            team = tuple(str(x) for x in cs.team_hero_ids)
            bench = tuple(str(x) for x in cs.bench_hero_ids)
            rerolls = int(cs.rerolls_remaining)
            spells = (int(cs.spell1_id), int(cs.spell2_id))

            effective_best_swap = str(best_swap_hero_id or "").strip()
            if not effective_best_swap and isinstance(team_comp_analysis, dict):
                effective_best_swap = str(team_comp_analysis.get("best_swap_hero_id") or "").strip()

            clean_ratings = (
                {str(k): round(float(v), 3) for k, v in hero_ratings.items()}
                if isinstance(hero_ratings, dict)
                else {}
            )
            top_rec = self._resolve_top_recommended_hero(
                my_hero, bench, effective_best_swap, clean_ratings, team_comp_analysis
            )

            if self._in_champ_select:
                prev_hero = self._cs_last_hero_id
                prev_bench = self._cs_last_bench
                prev_rerolls = self._cs_last_rerolls
                prev_spells = self._cs_last_spells
                prev_best_swap = self._cs_last_best_swap

                # Hero changed: reroll vs bench_swap vs pick_change
                if my_hero and prev_hero and my_hero != prev_hero:
                    if prev_rerolls >= 0 and rerolls >= 0 and rerolls < prev_rerolls:
                        action_type = "reroll"
                    elif my_hero in prev_bench:
                        action_type = "bench_swap"
                    else:
                        action_type = "pick_change"

                    compare_best = prev_best_swap or effective_best_swap
                    was_best_swap = bool(compare_best and my_hero == compare_best)
                    if was_best_swap:
                        self._cs_ever_followed_best_swap = True

                    self._cs_action_count += 1
                    self._emit(
                        "game",
                        "champ_select_action",
                        {
                            "action_type": action_type,
                            "from_hero": prev_hero,
                            "to_hero": my_hero,
                            "rerolls_remaining": rerolls,
                            "bench_size": len(bench),
                            "was_best_swap": was_best_swap,
                            "timer_phase": str(cs.timer_phase or ""),
                            "timer_remaining_ms": int(cs.timer_remaining_ms),
                        },
                        None,
                    )

                # Summoner spell changed
                if (
                    (prev_spells[0] > 0 or prev_spells[1] > 0)
                    and (spells[0] > 0 or spells[1] > 0)
                    and spells != prev_spells
                ):
                    self._cs_action_count += 1
                    self._emit(
                        "game",
                        "champ_select_action",
                        {
                            "action_type": "spell_change",
                            "hero_id": my_hero,
                            "from_spells": [prev_spells[0], prev_spells[1]],
                            "to_spells": [spells[0], spells[1]],
                            "rerolls_remaining": rerolls,
                            "bench_size": len(bench),
                        },
                        None,
                    )
            else:
                self._in_champ_select = True
                self._cs_action_count = 0
                self._cs_ever_followed_best_swap = False

            # Deduped ChampSelect recommendation snapshot
            rec_sig = (
                my_hero,
                tuple(sorted(team)),
                tuple(sorted(bench)),
                str(goal_mode or ""),
                effective_best_swap,
            )
            if (my_hero or team or bench) and rec_sig != self._cs_last_rec_sig:
                self._cs_last_rec_sig = rec_sig
                self._emit(
                    "rec",
                    "champ_select_recommendation",
                    {
                        "my_hero_id": my_hero,
                        "team_hero_ids": list(team),
                        "bench_hero_ids": list(bench),
                        "goal_mode": str(goal_mode or ""),
                        "best_swap_hero_id": effective_best_swap,
                        "top_recommended_hero": top_rec,
                        "hero_ratings": clean_ratings,
                        "team_comp_analysis": (
                            dict(team_comp_analysis) if isinstance(team_comp_analysis, dict) else {}
                        ),
                        "rerolls_remaining": rerolls,
                        "queue_id": int(cs.queue_id),
                        "game_mode": str(cs.game_mode or ""),
                    },
                    None,
                )

            self._cs_last_hero_id = my_hero
            self._cs_last_team = team
            self._cs_last_bench = bench
            self._cs_last_rerolls = rerolls
            self._cs_last_spells = spells
            self._cs_last_best_swap = effective_best_swap
            self._cs_last_top_rec = top_rec
            if goal_mode:
                self._cs_last_goal_mode = str(goal_mode)
            if clean_ratings:
                self._cs_last_hero_ratings = clean_ratings
            return

        # Exiting ChampSelect
        if not self._in_champ_select:
            return

        if phase_str in _DODGE_NEXT_PHASES:
            self._emit(
                "game",
                "champ_select_outcome_delta",
                {
                    "outcome": "dodged",
                    "next_phase": phase_str,
                    "final_hero_id": self._cs_last_hero_id,
                    "team_hero_ids": list(self._cs_last_team),
                    "bench_hero_ids": list(self._cs_last_bench),
                    "rerolls_remaining": self._cs_last_rerolls,
                    "action_count": self._cs_action_count,
                    "top_recommended_hero": self._cs_last_top_rec,
                    "best_swap_hero_id": self._cs_last_best_swap,
                    "goal_mode": self._cs_last_goal_mode,
                },
                None,
            )
            self._clear_champ_select_state()
        elif phase_str in _LOCKED_NEXT_PHASES:
            final_hero = self._cs_last_hero_id
            if self._cs_last_best_swap:
                top_rec = self._cs_last_top_rec or self._cs_last_best_swap
                followed_best = self._cs_ever_followed_best_swap or (
                    bool(final_hero) and final_hero == self._cs_last_best_swap
                )
            else:
                # Empty best_swap_hero_id means model recommends KEEPING current hero
                top_rec = self._cs_last_top_rec or final_hero
                followed_best = bool(final_hero) and (not top_rec or final_hero == top_rec)
            self._emit(
                "game",
                "champ_select_outcome_delta",
                {
                    "outcome": "locked",
                    "next_phase": phase_str,
                    "final_hero_id": final_hero,
                    "team_hero_ids": list(self._cs_last_team),
                    "bench_hero_ids": list(self._cs_last_bench),
                    "rerolls_remaining": self._cs_last_rerolls,
                    "action_count": self._cs_action_count,
                    "followed_best_swap": followed_best,
                    "top_recommended_hero": top_rec,
                    "best_swap_hero_id": self._cs_last_best_swap,
                    "goal_mode": self._cs_last_goal_mode,
                },
                None,
            )
            self._clear_champ_select_state()

    # ------------------------------------------------------------------
    # 2. Recommendation & Coach History Recording
    # ------------------------------------------------------------------
    def record_local_build_rec(
        self,
        game_time_s: float,
        recs_summary: list[dict[str, Any]],
        *,
        hero_profile: str = "",
        combo_hero_profile: str = "",
        boots_rec: int = 0,
        pivot_banner: str = "",
        source: str = "local",
    ) -> None:
        """Record local item recommendation snapshot (deduped by top-4 IDs + profile + pivot)."""
        now_mono = self._clock()
        seq = self._next_seq()
        gt = float(game_time_s or 0.0)

        item_ids: list[int] = []
        norm_ids: list[int] = []
        for entry in recs_summary or []:
            if not isinstance(entry, dict):
                continue
            iid = _safe_int(entry.get("item_id", entry.get("id")), 0)
            if iid > 0:
                item_ids.append(iid)
                norm_ids.append(_canonical_item_id(iid))

        boots_int = max(0, _safe_int(boots_rec, 0))
        top4 = tuple(item_ids[:4])
        hp = str(hero_profile or "").strip()
        chp = str(combo_hero_profile or "").strip()
        pv = str(pivot_banner or "").strip()
        src = str(source or "local").strip()

        rec_record = _LocalRecRecord(
            seq=seq,
            ts_mono=now_mono,
            game_time_s=gt,
            item_ids=tuple(item_ids[:6]),
            norm_item_ids=tuple(norm_ids[:6]),
            boots_rec=boots_int,
            norm_boots_rec=_canonical_item_id(boots_int) if boots_int > 0 else 0,
            hero_profile=hp,
            combo_hero_profile=chp,
            pivot_banner=pv,
            source=src,
        )

        sig = (top4, hp, chp, boots_int, pv)
        if sig == self._last_local_rec_sig and self._local_rec_history:
            return

        self._last_local_rec_sig = sig
        self._local_rec_history.append(rec_record)

        self._emit(
            "rec",
            "local_build_recommendation_snapshot",
            {
                "top_item_ids": list(top4),
                "recs": [dict(r) for r in (recs_summary or [])[:6] if isinstance(r, dict)],
                "hero_profile": hp,
                "combo_hero_profile": chp,
                "profile_conflict": bool(hp and chp and hp != chp),
                "boots_rec": boots_int,
                "pivot_banner": pv,
                "source": src,
            },
            gt,
        )

    def record_coach_advice(
        self,
        game_time_s: float,
        *,
        request_id: str,
        trigger_reason: str,
        recommended_item_ids: Sequence[int | str],
        recommended_augment_id: str = "",
        latency_ms: float = 0.0,
    ) -> None:
        """Record latest timestamped AI coach recommendation for causal attribution and death window."""
        now_mono = self._clock()
        seq = self._next_seq()
        gt = float(game_time_s or 0.0)

        ids: list[int] = []
        norm_ids: list[int] = []
        for raw in recommended_item_ids or ():
            iid = _safe_int(raw, 0)
            if iid > 0:
                ids.append(iid)
                norm_ids.append(_canonical_item_id(iid))

        entry = _CoachAdviceRecord(
            seq=seq,
            ts_mono=now_mono,
            game_time_s=gt,
            request_id=str(request_id or "").strip(),
            trigger_reason=str(trigger_reason or "").strip(),
            item_ids=tuple(ids),
            norm_item_ids=tuple(norm_ids),
            recommended_augment_id=str(recommended_augment_id or "").strip(),
            latency_ms=float(latency_ms or 0.0),
        )
        self._coach_advice_history.append(entry)

        if (
            self._death_window is not None
            and entry.trigger_reason.lower() in _DEATH_WINDOW_COACH_TRIGGERS
        ):
            dw = self._death_window
            dw.coach_arrived = True
            dw.coach_request_id = entry.request_id
            dw.coach_latency_ms = entry.latency_ms
            if not dw.first_buy_seen:
                dw.coach_arrived_before_buy = True

    def record_plan_override(self, game_time_s: float, override_trace: dict[str, Any]) -> None:
        """Record >=800G component route override trace from reconcile_match_build_plan_with_trace."""
        if not isinstance(override_trace, dict) or not override_trace:
            return
        gt = float(game_time_s or 0.0)
        sig = (
            str(override_trace.get("old_locked_item") or override_trace.get("previous_locked") or ""),
            str(override_trace.get("new_locked_item") or override_trace.get("new_locked") or ""),
            _safe_int(override_trace.get("invested_gold", override_trace.get("best_invested_gold")), 0),
        )
        if sig == self._last_plan_override_sig:
            return
        self._last_plan_override_sig = sig

        payload = dict(override_trace)
        payload.setdefault("event_kind", "plan_override")
        payload["override_triggered"] = True
        self._emit("rec", "item_purchase_and_plan_override_delta", payload, gt)

    def note_picker_opened(self, game_time_s: float) -> None:
        """Inform tracker that the augment picker opened (prevents false silent-miss alerts)."""
        self._last_picker_opened_game_time = float(game_time_s or 0.0)
        for timer in self._unlock_timers.values():
            if not timer.alerted:
                timer.picker_opened = True

    def note_death_coach_decision(
        self,
        triggered: bool,
        skip_reason: str = "",
        *,
        warmup_served: bool | None = None,
    ) -> None:
        """Inform tracker whether AI coach was triggered or skipped for the active death window."""
        trig = bool(triggered)
        reason = str(skip_reason or "").strip()
        is_warmup_served = (
            bool(warmup_served)
            if warmup_served is not None
            else (reason == "pre_death_warmup_cached")
        )
        self._death_warmup_served = is_warmup_served
        if self._death_window is not None:
            self._death_window.coach_triggered = trig
            self._death_window.coach_skip_reason = reason
            self._death_window.warmup_served = is_warmup_served
            if is_warmup_served:
                self._death_window.coach_arrived = True
                self._death_window.coach_arrived_before_buy = True
            self._pending_death_decision = None
        else:
            self._pending_death_decision = (trig, reason, is_warmup_served, self._clock())

    # ------------------------------------------------------------------
    # 3. Causal Lookups & Pending Purchase Confirmation
    # ------------------------------------------------------------------
    def _latest_causal_local_rec(
        self,
        purchase_seq: int,
        purchase_mono: float,
        purchase_game_time_s: float,
    ) -> _LocalRecRecord | None:
        for rec in reversed(self._local_rec_history):
            if (
                rec.seq < purchase_seq
                and rec.ts_mono <= purchase_mono
                and rec.game_time_s <= purchase_game_time_s
            ):
                return rec
        return None

    def _latest_causal_coach_advice(
        self,
        purchase_seq: int,
        purchase_mono: float,
        purchase_game_time_s: float,
    ) -> _CoachAdviceRecord | None:
        for adv in reversed(self._coach_advice_history):
            if (
                adv.seq < purchase_seq
                and adv.ts_mono <= purchase_mono
                and adv.game_time_s <= purchase_game_time_s
            ):
                return adv
        return None

    def _is_meaningful_purchase(self, pending: _PendingPurchase) -> bool:
        if _is_augment_granted_special_item(pending.item_id, pending.price):
            return True
        norm_id = _canonical_item_id(pending.item_id)
        if self._is_complete_item_fn is not None:
            if norm_id in TIER2_BOOT_IDS:
                return True
            try:
                if self._is_complete_item_fn(str(pending.item_id)):
                    return True
                if norm_id > 0 and norm_id != pending.item_id:
                    return bool(self._is_complete_item_fn(str(norm_id)))
                return False
            except Exception:
                return False
        if norm_id in TIER2_BOOT_IDS or pending.action == "upgrade":
            return True
        if pending.price > 0:
            return pending.price >= 800
        # Fallback when item_details price is unavailable in minimal fixtures
        return norm_id >= 2500

    def _confirm_pending_purchase(self, pending: _PendingPurchase) -> None:
        self._emit(
            "game",
            "item_transaction",
            {
                "action": pending.action,
                "item_id": pending.item_id,
                "price": pending.price,
                "gold_delta": round(pending.gold_delta, 1),
                "consumed_components": list(pending.consumed_components),
                "is_dead": pending.in_death_window,
            },
            pending.purchase_game_time_s,
        )

        if not self._is_meaningful_purchase(pending):
            return

        augment_granted = _is_augment_granted_special_item(pending.item_id, pending.price)
        norm_id = _canonical_item_id(pending.item_id)
        dedup_key = (
            f"aug:{pending.item_id}"
            if augment_granted
            else str(norm_id if norm_id > 0 else pending.item_id)
        )
        if dedup_key in self._attributed_completed_items:
            return
        self._attributed_completed_items.add(dedup_key)

        local_rec = pending.local_rec
        coach_adv = pending.coach_advice

        matched_local_rank = 0
        if local_rec is not None:
            for idx, (rid, rnid) in enumerate(zip(local_rec.item_ids, local_rec.norm_item_ids), start=1):
                if pending.item_id == rid or norm_id == rnid:
                    matched_local_rank = idx
                    break
            if matched_local_rank == 0 and local_rec.boots_rec > 0:
                if pending.item_id == local_rec.boots_rec or norm_id == local_rec.norm_boots_rec:
                    matched_local_rank = 1

        matched_coach = False
        if coach_adv is not None:
            if pending.item_id in coach_adv.item_ids or norm_id in coach_adv.norm_item_ids:
                matched_coach = True

        local_top1 = local_rec.norm_item_ids[0] if (local_rec and local_rec.norm_item_ids) else 0
        coach_top1 = coach_adv.norm_item_ids[0] if (coach_adv and coach_adv.norm_item_ids) else 0
        local_vs_coach_disagreed = bool(local_top1 and coach_top1 and local_top1 != coach_top1)

        matched_local = matched_local_rank > 0
        if matched_local and matched_coach:
            sided_with = "both"
        elif matched_local:
            sided_with = "local"
        elif matched_coach:
            sided_with = "coach"
        else:
            sided_with = "neither"

        self._emit(
            "rec",
            "item_purchase_and_plan_override_delta",
            {
                "event_kind": "item_purchase",
                "item_id": pending.item_id,
                "action": pending.action,
                "price": pending.price,
                "augment_granted": augment_granted,
                "consumed_components": list(pending.consumed_components),
                "matched_local_rank": matched_local_rank,
                "matched_local_top1": matched_local_rank == 1,
                "matched_coach": matched_coach,
                "local_top_items": list(local_rec.item_ids[:4]) if local_rec else [],
                "coach_rec_items": list(coach_adv.item_ids[:4]) if coach_adv else [],
                "coach_request_id": coach_adv.request_id if coach_adv else "",
                "local_rec_age_s": (
                    round(pending.purchase_game_time_s - local_rec.game_time_s, 2)
                    if local_rec is not None
                    else None
                ),
                "coach_advice_age_s": (
                    round(pending.purchase_game_time_s - coach_adv.game_time_s, 2)
                    if coach_adv is not None
                    else None
                ),
                "local_vs_coach_disagreed": local_vs_coach_disagreed,
                "sided_with": sided_with,
            },
            pending.purchase_game_time_s,
        )

    def _elapsed_since(self, start_game_time_s: float, start_mono: float, cur_game_time_s: float, cur_mono: float) -> float:
        dt_game = cur_game_time_s - start_game_time_s
        dt_mono = cur_mono - start_mono
        if dt_game >= 0.0:
            return max(dt_game, dt_mono)
        return max(0.0, dt_mono)

    def _confirm_mature_pending(self, cur_game_time_s: float, cur_mono: float, *, force_all: bool = False) -> None:
        still_pending: list[_PendingPurchase] = []
        for pending in self._pending_purchases:
            elapsed = self._elapsed_since(
                pending.purchase_game_time_s, pending.purchase_mono, cur_game_time_s, cur_mono
            )
            if force_all or elapsed >= self._undo_window_s:
                self._confirm_pending_purchase(pending)
            else:
                still_pending.append(pending)
        self._pending_purchases = still_pending

    def flush_pending_transactions(self) -> None:
        """Flush all pending 2s-undo purchases and any open death-shop window (called at EOG)."""
        self._confirm_mature_pending(self._last_game_time_s, self._clock(), force_all=True)
        if self._death_window is not None:
            self._close_death_window(
                respawn_game_time_s=self._last_game_time_s,
                gold_at_respawn=self._prev_gold,
                closed_by="match_end",
            )

    # ------------------------------------------------------------------
    # 4. Death-Shop Window Helpers
    # ------------------------------------------------------------------
    def _close_death_window(
        self,
        respawn_game_time_s: float,
        gold_at_respawn: float,
        *,
        closed_by: str = "respawn",
    ) -> None:
        dw = self._death_window
        if dw is None:
            return
        self._death_window = None
        warmup_served = bool(dw.warmup_served or self._death_warmup_served)
        self._death_warmup_served = False
        arrived = bool(dw.coach_arrived or warmup_served)
        arrived_before_buy = bool(dw.coach_arrived_before_buy or warmup_served)
        duration_s = max(0.0, round(respawn_game_time_s - dw.death_start_time, 2))
        self._emit(
            "coach",
            "death_shop_window",
            {
                "death_start_time": round(dw.death_start_time, 2),
                "death_duration_s": duration_s,
                "killer_hero": dw.killer_hero,
                "gold_at_death": round(dw.gold_at_death, 1),
                "gold_at_respawn": round(float(gold_at_respawn), 1),
                "items_bought": list(dw.items_bought),
                "bought_count": len(dw.items_bought),
                "coach_triggered": dw.coach_triggered,
                "coach_arrived": arrived,
                "coach_arrived_before_buy": arrived_before_buy,
                "warmup_served": warmup_served,
                "coach_skip_reason": dw.coach_skip_reason,
                "coach_request_id": dw.coach_request_id,
                "coach_latency_ms": round(dw.coach_latency_ms, 1),
                "closed_by": closed_by,
            },
            respawn_game_time_s,
        )

    # ------------------------------------------------------------------
    # 5. Live Match Snapshot Processing
    # ------------------------------------------------------------------
    def _process_live_events(self, snap: LiveMatchSnapshot) -> bool:
        """Process incremental raw_events cursor. Returns True if GameEnd was seen."""
        game_end_seen = False
        for ev in snap.raw_events or ():
            if not isinstance(ev, dict):
                continue
            ev_id = _safe_int(ev.get("event_id"), -1)
            if ev_id <= self._last_event_id:
                continue
            self._last_event_id = ev_id

            ev_name = str(ev.get("event_name") or "").strip()
            ev_time = float(ev.get("event_time") if ev.get("event_time") is not None else snap.game_time)

            if ev_name == "ChampionKill" and ev.get("victim_is_me"):
                killer = str(ev.get("killer_hero") or "").strip()
                self._last_my_death_killer = killer
                self._last_my_death_event_time = ev_time
                if self._death_window is not None and not self._death_window.killer_hero and killer:
                    self._death_window.killer_hero = killer

            if ev_name == "GameEnd":
                game_end_seen = True

            should_emit = ev_name in _ALWAYS_EMIT_LIVE_EVENTS or (
                ev_name == "ChampionKill"
                and (
                    bool(ev.get("killer_is_me"))
                    or bool(ev.get("victim_is_me"))
                    or bool(ev.get("assister_is_me"))
                )
            )
            if not should_emit:
                continue

            payload: dict[str, Any] = {
                "event_id": ev_id,
                "event_name": ev_name,
                "event_time": round(ev_time, 2),
                "killer_hero": str(ev.get("killer_hero") or ""),
                "victim_hero": str(ev.get("victim_hero") or ""),
                "assister_heroes": list(ev.get("assister_heroes") or []),
                "killer_is_me": bool(ev.get("killer_is_me", False)),
                "victim_is_me": bool(ev.get("victim_is_me", False)),
                "assister_is_me": bool(ev.get("assister_is_me", False)),
                "killer_is_ally": bool(ev.get("killer_is_ally", False)),
                "victim_is_ally": bool(ev.get("victim_is_ally", False)),
            }
            if ev.get("turret_killed"):
                payload["turret_killed"] = str(ev["turret_killed"])
            if "kill_streak" in ev:
                payload["kill_streak"] = _safe_int(ev.get("kill_streak"), 0)
            if "acing_team" in ev:
                payload["acing_team"] = str(ev.get("acing_team") or "")
            if "acer_hero" in ev:
                payload["acer_hero"] = str(ev.get("acer_hero") or "")
            if "inhib_killed" in ev:
                payload["inhib_killed"] = str(ev.get("inhib_killed") or "")
            if "result" in ev:
                payload["result"] = str(ev.get("result") or "")

            self._emit("game", "live_game_event", payload, ev_time)
        return game_end_seen

    def _process_enemy_milestones(self, snap: LiveMatchSnapshot) -> None:
        for enemy in snap.enemies:
            enemy_key = _hero_key(enemy)
            if not enemy_key:
                continue
            seen = self._enemy_seen_items.setdefault(enemy_key, set())
            counts, meta = _extract_inventory(enemy)
            for raw_iid in counts:
                norm_id = _canonical_item_id(raw_iid)
                if norm_id <= 0 or norm_id in seen:
                    continue
                info = meta.get(raw_iid) or {}
                price = int(info.get("price", 0))
                consumable = bool(info.get("consumable", False))
                if consumable:
                    continue

                tags: list[str] = []
                if norm_id in ANTI_HEAL_ITEM_IDS:
                    tags.append("anti_heal")
                if norm_id in ARMOR_PEN_ITEM_IDS:
                    tags.append("armor_pen")
                if norm_id in MR_PEN_ITEM_IDS:
                    tags.append("mr_pen")
                if norm_id in STASIS_ITEM_IDS:
                    tags.append("stasis")
                if norm_id in SHIELD_CUT_ITEM_IDS:
                    tags.append("shield_cut")
                if norm_id in QSS_SPELLSHIELD_ITEM_IDS:
                    tags.append("qss_spellshield")

                is_completed = (price >= 2200) or (
                    price == 0
                    and norm_id >= 2500
                    and norm_id not in _NON_COMPLETE_MID_COMPONENT_IDS
                    and norm_id not in TIER2_BOOT_IDS
                )
                if is_completed:
                    tags.insert(0, "completed")

                if not tags:
                    continue

                seen.add(norm_id)
                self._emit(
                    "game",
                    "enemy_item_milestone",
                    {
                        "enemy_hero": enemy_key,
                        "item_id": raw_iid,
                        "canonical_item_id": norm_id,
                        "price": price,
                        "tags": tags,
                        "enemy_kda": f"{enemy.kills}/{enemy.deaths}/{enemy.assists}",
                    },
                    snap.game_time,
                )

    def _process_level_and_unlock_milestones(
        self,
        snap: LiveMatchSnapshot,
        now_mono: float,
        *,
        selected_augment_count: int,
        is_picker_open: bool,
        picker_ever_opened_since_level: bool,
        is_degraded_2999: bool = False,
    ) -> None:
        if snap.my_player is None:
            return
        cur_level = max(1, int(snap.my_player.level))
        is_dead = bool(snap.my_player.is_dead)
        gt = float(snap.game_time or 0.0)

        if is_picker_open:
            self.note_picker_opened(gt)

        for lvl, expected_slots in AUGMENT_UNLOCK_MILESTONES:
            if cur_level >= lvl and lvl not in self._emitted_level_milestones:
                self._emitted_level_milestones.add(lvl)
                self._emit(
                    "game",
                    "level_and_skill_milestone",
                    {
                        "level": cur_level,
                        "milestone_level": lvl,
                        "expected_augment_slots": expected_slots,
                        "selected_augment_count": int(selected_augment_count),
                        "ability_levels": dict(snap.my_ability_levels),
                        "maxed_ability": str(snap.my_maxed_ability or ""),
                        "is_degraded_2999": bool(is_degraded_2999),
                    },
                    gt,
                )
                if not is_degraded_2999 and lvl not in self._unlock_timers:
                    # Lv 3 pops at fountain immediately; Lv 7/11/15 only pop after death/recall
                    armed_now = (lvl <= 3) or is_dead
                    self._unlock_timers[lvl] = _UnlockTimer(
                        milestone_level=lvl,
                        expected_slots=expected_slots,
                        reached_game_time_s=gt,
                        reached_mono=now_mono,
                        armed=armed_now,
                        picker_opened=bool(is_picker_open or picker_ever_opened_since_level),
                    )

        if is_degraded_2999:
            return

        for timer in self._unlock_timers.values():
            if timer.alerted:
                continue
            if is_picker_open or picker_ever_opened_since_level:
                timer.picker_opened = True
            if int(selected_augment_count) >= timer.expected_slots:
                timer.alerted = True
                continue
            if timer.picker_opened:
                continue
            if not timer.armed:
                if is_dead:
                    timer.armed = True
                    timer.reached_game_time_s = gt
                    timer.reached_mono = now_mono
                else:
                    continue
            elapsed = self._elapsed_since(timer.reached_game_time_s, timer.reached_mono, gt, now_mono)
            if elapsed >= self._unlock_miss_timeout_s:
                timer.alerted = True
                self._emit(
                    "vision",
                    "augment_unlock_silent_miss",
                    {
                        "milestone_level": timer.milestone_level,
                        "current_level": cur_level,
                        "expected_augment_slots": timer.expected_slots,
                        "selected_augment_count": int(selected_augment_count),
                        "elapsed_since_unlock_s": round(elapsed, 2),
                        "reached_game_time_s": round(timer.reached_game_time_s, 2),
                    },
                    gt,
                )

    def _process_my_item_transactions(
        self,
        snap: LiveMatchSnapshot,
        now_mono: float,
    ) -> None:
        if snap.my_player is None:
            return
        gt = float(snap.game_time or 0.0)
        cur_gold = float(snap.current_gold or 0.0)
        curr_counts, curr_meta = _extract_inventory(snap.my_player)

        if not self._live_initialized:
            self._live_initialized = True
            self._prev_my_items = curr_counts
            self._prev_item_meta = curr_meta
            self._prev_gold = cur_gold
            return

        gold_delta = cur_gold - self._prev_gold
        added: Counter[int] = curr_counts - self._prev_my_items
        removed: Counter[int] = self._prev_my_items - curr_counts

        # 1. Check 2-second Undo filter against pending purchases
        if removed and self._pending_purchases:
            remaining_pending: list[_PendingPurchase] = []
            for pending in self._pending_purchases:
                if removed.get(pending.item_id, 0) > 0:
                    elapsed = self._elapsed_since(
                        pending.purchase_game_time_s, pending.purchase_mono, gt, now_mono
                    )
                    components_restored = bool(
                        pending.consumed_components
                        and all(added.get(cid, 0) > 0 for cid in pending.consumed_components)
                    )
                    gold_restored = (
                        (pending.spent_gold > 0.0 and gold_delta >= pending.spent_gold * 0.85)
                        or (pending.spent_gold <= 0.0 and gold_delta >= 100.0)
                    )
                    if elapsed <= self._undo_window_s and (components_restored or gold_restored):
                        removed[pending.item_id] -= 1
                        if removed[pending.item_id] <= 0:
                            del removed[pending.item_id]
                        if components_restored:
                            for cid in pending.consumed_components:
                                added[cid] -= 1
                                if added[cid] <= 0:
                                    del added[cid]
                        if self._death_window is not None and pending.item_id in self._death_window.items_bought:
                            self._death_window.items_bought.remove(pending.item_id)
                            if not self._death_window.items_bought:
                                self._death_window.first_buy_seen = False
                                self._death_window.first_buy_time = None
                        self._emit(
                            "game",
                            "item_transaction",
                            {
                                "action": "undo",
                                "item_id": pending.item_id,
                                "price": pending.price,
                                "gold_delta": round(gold_delta, 1),
                                "elapsed_s": round(elapsed, 2),
                                "restored_components": list(pending.consumed_components),
                            },
                            gt,
                        )
                        continue
                remaining_pending.append(pending)
            self._pending_purchases = remaining_pending

        # 2. Confirm any pending purchases that have matured (>= 2.0s)
        self._confirm_mature_pending(gt, now_mono, force_all=False)

        # 3. Classify upgrades (removed components + higher-tier item added same tick)
        non_consumable_removed: list[int] = []
        for iid, cnt in list(removed.items()):
            info = self._prev_item_meta.get(iid) or {}
            is_cons = bool(info.get("consumable", False)) or (iid in CONSUMABLE_ITEM_IDS)
            if not is_cons:
                non_consumable_removed.extend([iid] * cnt)

        consumed_for_upgrade: list[int] = []
        upgrade_target_id: int | None = None
        if added and non_consumable_removed:
            max_removed_price = max(
                int((self._prev_item_meta.get(rid) or {}).get("price", 0)) for rid in non_consumable_removed
            )
            # Pick highest-priced newly added non-consumable item as upgrade target
            best_cand_id = 0
            best_cand_price = -1
            for iid in added:
                info = curr_meta.get(iid) or {}
                if bool(info.get("consumable", False)) or (iid in CONSUMABLE_ITEM_IDS):
                    continue
                p = int(info.get("price", 0))
                if p > best_cand_price:
                    best_cand_price = p
                    best_cand_id = iid
            if best_cand_id > 0 and (
                (best_cand_price > max_removed_price and best_cand_price > 0)
                or (best_cand_price == 0 and max_removed_price == 0 and gold_delta <= 0.0)
            ):
                upgrade_target_id = best_cand_id
                consumed_for_upgrade = list(non_consumable_removed)
                # Confirm any still-pending component that is now being upgraded
                still_pending_after_upg: list[_PendingPurchase] = []
                consumed_counter = Counter(consumed_for_upgrade)
                for p_item in self._pending_purchases:
                    if consumed_counter.get(p_item.item_id, 0) > 0:
                        consumed_counter[p_item.item_id] -= 1
                        self._confirm_pending_purchase(p_item)
                    else:
                        still_pending_after_upg.append(p_item)
                self._pending_purchases = still_pending_after_upg
                # Remove consumed components from `removed` so they aren't emitted as sells
                for cid in consumed_for_upgrade:
                    if removed.get(cid, 0) > 0:
                        removed[cid] -= 1
                        if removed[cid] <= 0:
                            del removed[cid]

        # 4. Handle newly added items -> hold in 2s undo buffer with purchase-time causal attribution
        total_added_units = sum(added.values())
        if total_added_units > 0:
            for iid, cnt in added.items():
                info = curr_meta.get(iid) or {}
                item_price = int(info.get("price", 0))
                for copy_idx in range(cnt):
                    is_upg = upgrade_target_id is not None and iid == upgrade_target_id and copy_idx == 0
                    action = "upgrade" if is_upg else "buy"
                    used_comps = tuple(consumed_for_upgrade) if is_upg else ()
                    comp_price_sum = sum(
                        int((self._prev_item_meta.get(c) or {}).get("price", 0)) for c in used_comps
                    )
                    if item_price > 0:
                        est_spent = float(max(0, item_price - comp_price_sum))
                    else:
                        est_spent = max(0.0, -gold_delta) / float(total_added_units)

                    p_seq = self._next_seq()
                    local_rec = self._latest_causal_local_rec(p_seq, now_mono, gt)
                    coach_adv = self._latest_causal_coach_advice(p_seq, now_mono, gt)
                    pending = _PendingPurchase(
                        item_id=iid,
                        action=action,
                        price=item_price,
                        gold_delta=gold_delta,
                        spent_gold=est_spent,
                        consumed_components=used_comps,
                        purchase_game_time_s=gt,
                        purchase_mono=now_mono,
                        purchase_seq=p_seq,
                        in_death_window=self._death_window is not None,
                        local_rec=local_rec,
                        coach_advice=coach_adv,
                    )
                    self._pending_purchases.append(pending)

                    if self._death_window is not None:
                        dw = self._death_window
                        dw.items_bought.append(iid)
                        if not dw.first_buy_seen:
                            dw.first_buy_seen = True
                            dw.first_buy_time = gt
                            if dw.coach_arrived:
                                dw.coach_arrived_before_buy = True

        # 5. Handle remaining removed items -> confirm any matching pending buy first, then consume vs sell
        if removed and self._pending_purchases:
            rem_counter = Counter(removed)
            still_pending_before_sell: list[_PendingPurchase] = []
            for p_item in self._pending_purchases:
                if rem_counter.get(p_item.item_id, 0) > 0:
                    rem_counter[p_item.item_id] -= 1
                    self._confirm_pending_purchase(p_item)
                else:
                    still_pending_before_sell.append(p_item)
            self._pending_purchases = still_pending_before_sell

        for iid, cnt in removed.items():
            prev_info = self._prev_item_meta.get(iid) or {}
            prev_price = int(prev_info.get("price", 0))
            is_cons = bool(prev_info.get("consumable", False)) or (iid in CONSUMABLE_ITEM_IDS)
            action = "consume" if (is_cons and gold_delta <= 40.0) else "sell"
            for _ in range(cnt):
                self._emit(
                    "game",
                    "item_transaction",
                    {
                        "action": action,
                        "item_id": iid,
                        "price": prev_price,
                        "gold_delta": round(gold_delta, 1),
                    },
                    gt,
                )

        self._prev_my_items = curr_counts
        self._prev_item_meta = curr_meta
        self._prev_gold = cur_gold

    def on_live_snapshot(
        self,
        snap: LiveMatchSnapshot,
        *,
        selected_augment_count: int = 0,
        is_picker_open: bool = False,
        picker_ever_opened_since_level: bool = False,
        is_degraded_2999: bool = False,
    ) -> None:
        """Process one LiveClientData snapshot: events, death window, items, enemy milestones, levels."""
        now_mono = self._clock()
        gt = float(snap.game_time or 0.0)
        self._last_game_time_s = gt

        # Close champ-select as locked if LCU missed the transition into game
        if self._in_champ_select:
            self.on_lcu_champ_select("InProgress", None)

        # 1. Incremental sanitized live game events (updates killer_hero before death window check)
        game_end_seen = self._process_live_events(snap)

        # 2. Death-shop window transitions
        is_dead = bool(snap.my_player and snap.my_player.is_dead)
        if is_dead and not self._was_dead:
            # Player died again -> any earlier alive purchases obviously left fountain
            if self._pending_purchases:
                self._confirm_mature_pending(gt, now_mono, force_all=True)
            killer = ""
            if self._last_my_death_killer and abs(gt - self._last_my_death_event_time) <= 15.0:
                killer = self._last_my_death_killer
            self._death_warmup_served = False
            self._death_window = _DeathShopWindow(
                death_start_time=gt,
                death_start_mono=now_mono,
                gold_at_death=float(snap.current_gold or 0.0),
                killer_hero=killer,
            )
            if self._pending_death_decision is not None:
                trig, reason, is_warmup, dec_mono = self._pending_death_decision
                self._pending_death_decision = None
                if (now_mono - dec_mono) <= 2.0:
                    self._death_window.coach_triggered = trig
                    self._death_window.coach_skip_reason = reason
                    self._death_window.warmup_served = is_warmup
                    self._death_warmup_served = is_warmup
                    if is_warmup:
                        self._death_window.coach_arrived = True
                        self._death_window.coach_arrived_before_buy = True
        elif not is_dead and self._was_dead and self._death_window is not None:
            self._close_death_window(
                respawn_game_time_s=gt,
                gold_at_respawn=float(snap.current_gold or 0.0),
                closed_by="respawn",
            )
        self._was_dead = is_dead

        # 3. Player item diff, 2s undo filter, and real-time causal attribution
        self._process_my_item_transactions(snap, now_mono)

        # 4. Enemy key item milestones
        self._process_enemy_milestones(snap)

        # 5. Level & augment unlock milestones + 45s silent miss detector
        self._process_level_and_unlock_milestones(
            snap,
            now_mono,
            selected_augment_count=selected_augment_count,
            is_picker_open=is_picker_open,
            picker_ever_opened_since_level=picker_ever_opened_since_level,
            is_degraded_2999=is_degraded_2999,
        )

        # 6. Flush remaining pending purchases immediately if GameEnd occurred
        if game_end_seen or snap.game_end_result in (0, 1):
            self.flush_pending_transactions()
