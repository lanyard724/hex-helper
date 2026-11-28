"""Offline match replay analyzer, 10 deterministic issue detectors, evolution report & A/B comparator.

Pure-Python module with zero Qt/PySide6 dependencies so it can run headless from CLI,
CI pipelines, or AI coding sessions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable

from hex_helper.config import get_devlab_dir
from hex_helper.devlab.schema import (
    EV_AUGMENT_OFFER_REC,
    EV_AUGMENT_PICK_DELTA,
    EV_AUGMENT_REROLL_DELTA,
    EV_CHAMP_SELECT_ACTION,
    EV_CHAMP_SELECT_OUTCOME,
    EV_CHAMP_SELECT_REC,
    EV_COACH_TURN,
    EV_DEATH_SHOP_WINDOW,
    EV_ENEMY_ITEM_MILESTONE,
    EV_GAMEFLOW_TRANSITION,
    EV_ITEM_PURCHASE_DELTA,
    EV_ITEM_TX,
    EV_LEVEL_MILESTONE,
    EV_LIVE_GAME_EVENT,
    EV_LOBBY_SCOUT,
    EV_LOCAL_BUILD_REC,
    EV_MAIN_THREAD_STALL,
    EV_MATCH_EOG,
    EV_RUNES_LOCKED,
    EV_SMART_PICKER_GATE,
    EV_UI_CALIBRATION,
    EV_UI_HOTKEY_TRAY,
    EV_UI_LAYOUT_ADJUSTED,
    EV_UI_LOOKUP,
    EV_UI_MANUAL_TRIGGER,
    EV_UI_OVERLAY_SESSION,
    EV_UI_RECAP,
    EV_UI_SCROLL_OVERFLOW,
    EV_UI_SUBCARD,
    EV_VISION_CAPTURE_STATE,
    EV_VISION_CLICK_RESOLVED,
    EV_VISION_ICON_VERIFY,
    EV_VISION_LATENCY_SPIKE,
    EV_VISION_OCR_ANOMALY,
    EV_VISION_PICKER_TRANSITION,
    EV_VISION_TRANSMUTE_TRACE,
    EV_VISION_USER_CORRECTION,
    EV_WRITER_HEALTH,
    EVENT_SCHEMA_VERSION,
    PII_KEY_BLACKLIST,
)
from hex_helper.logging_ring import (
    _AUTH_KV_RE,
    _BEARER_RE,
    _COS_KEY_RE,
    _GEMINI_KEY_RE,
    _JWT_RE,
    _OPENAI_DS_KEY_RE,
    _REMOTING_TOKEN_RE,
    _RIOT_AUTH_RE,
    _RIOT_ID_RE,
    _RIOT_KV_QUOTED_RE,
    _RIOT_KV_RE,
    _RIOT_PUUID_RE,
    _WIN_USER_PATH_RE,
    _redact_riot_id_match,
)

EV_AUGMENT_UNLOCK_SILENT_MISS = "augment_unlock_silent_miss"
EV_VISION_WINDOW_STATS = "vision.pipeline.window_stats"

DEFAULT_MAX_TOTAL_BYTES = 250 * 1024 * 1024
DEFAULT_MAX_ROI_BYTES = 30 * 1024 * 1024
DEFAULT_MAX_MATCHES = 120

_STAT_ANVIL_IDS: frozenset[str] = frozenset({"1402", "1403", "1404", "3171"})
_NON_MAYHEM_MODES: frozenset[str] = frozenset(
    {"CLASSIC", "PRACTICETOOL", "CUSTOM", "TUTORIAL", "TFT", "CHERRY", "ARENA", "URF", "ONEFORALL"}
)
_UNLOCK_LEVEL_TO_SLOTS: dict[int, int] = {3: 1, 7: 2, 11: 3, 15: 4}
_MATCH_DIR_SEQ_RE = re.compile(r"^m_(\d+)(?:_|$)")
_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_RES_DPR_SUFFIX_RE = re.compile(r"^(.+?)@([0-9]+(?:\.[0-9]+)?)x$", re.IGNORECASE)
_SEVERITY_ORDER: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}
_PII_COMPACT_BLACKLIST: frozenset[str] = frozenset(
    k.lower().replace("_", "").replace("-", "") for k in PII_KEY_BLACKLIST
)
_INTERNAL_UUID_FIELDS: frozenset[str] = frozenset(
    {"match_session_id", "request_id", "coach_request_id", "session_id"}
)
_AUTO_OPEN_SOURCES: frozenset[str] = frozenset(
    {"auto_death_peek", "auto_brief_expand", "auto_picker", "auto_champ_select", "auto_or_unknown"}
)
_USER_INITIATED_OPEN_SOURCES: frozenset[str] = frozenset({"tray_show", "hotkey_show", "lookup_btn"})
_MANUAL_OPEN_PREFIXES: tuple[str, ...] = ("manual", "hotkey", "tray", "user", "click", "button", "lookup")
_RAPID_CLOSE_SOURCES: frozenset[str] = frozenset({"user_close", "hotkey_hide", "escape", "auto_or_unknown"})
_BACKGROUND_PERF_OP_PREFIXES: tuple[str, ...] = ("vision_worker",)

# Direction map for cross-version A/B comparison: True = higher is better, False = lower is better
_HIGHER_IS_BETTER: dict[str, bool] = {
    "mayhem_winrate": True,
    "augment_top1_adoption_rate": True,
    "augment_avg_hex_regret": False,
    "augment_avg_wr_regret": False,
    "augment_top1_reroll_rate": False,
    "reroll_avg_hex_gain": True,
    "item_top1_adoption_rate": True,
    "item_top4_adoption_rate": True,
    "plan_override_rate": False,
    "profile_conflict_rate": False,
    "champ_select_best_swap_rate": True,
    "champ_select_dodge_rate": False,
    "unlock_silent_miss_count": False,
    "ocr_nearmiss_count": False,
    "ocr_shadowed_pass2_count": False,
    "reroll_desync_count": False,
    "y_threshold_clip_count": False,
    "icon_empty_bank_count": False,
    "icon_flat_patch_count": False,
    "tab_zone_pollution_count": False,
    "user_correction_count": False,
    "rapid_dismiss_rate": False,
    "ghost_repopup_count": False,
    "detached_edge_rate": False,
    "scroll_overflow_rate": False,
    "f4_emergency_hide_count": False,
    "recap_share_rate": True,
    "avg_latency_ms": False,
    "p95_latency_ms": False,
    "picker_ai_timely_rate": True,
    "death_ai_timely_rate": True,
    "death_warmup_hit_count": True,
    "sided_with_coach_rate": True,
    "grounding_block_rate": False,
    "manual_f6_after_gate_skip_rate": False,
    "eog_complete_rate": True,
    "eog_race_lost_rate": False,
    "tick_vision_p95_ms": False,
    "main_thread_ge_50ms_stalls": False,
    "vision_worker_p95_ms": False,
    "vision_worker_ge_50ms_count": False,
    "click_timer_max_gap_ms": False,
    "in_game_idle_dual_pass_rate": False,
    "writer_dropped_events": False,
    "writer_dropped_roi": False,
}


def _is_background_perf_op(op_name: str) -> bool:
    """Return True if a latency metric op belongs to a background worker thread (not Qt main thread)."""
    low = str(op_name or "").strip().lower()
    return any(low.startswith(pfx) for pfx in _BACKGROUND_PERF_OP_PREFIXES)


def _is_augment_granted_special_item_id(item_id: Any) -> bool:
    """Return True for 6-digit ARAM Mayhem augment-upgraded (22xxxx) or Prismatic-anvil (99xxxx) items."""
    raw = str(item_id if item_id is not None else "").strip()
    if not raw:
        return False
    if raw.isdigit():
        digits = raw
    else:
        val = _safe_int(raw, 0)
        if val <= 0:
            return False
        digits = str(val)
    if len(digits) != 6 or digits == "220013":
        return False
    return digits.startswith("22") or digits.startswith("99")


# ---------------------------------------------------------------------------
# Utility Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if math.isfinite(val) else default
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return default
        try:
            return int(s)
        except ValueError:
            try:
                f = float(s)
                return int(f) if math.isfinite(f) else default
            except ValueError:
                return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _opt_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in mapping and mapping[k] is not None:
            return mapping[k]
    return default


def _first_nonempty(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = mapping.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


def _extract_near_miss_score(mapping: dict[str, Any]) -> float | None:
    for k in ("near_miss_score", "near_miss_top1_score", "top1_score", "score"):
        v = _opt_float(mapping.get(k))
        if v is not None and v > 0.0:
            return v
    return _opt_float(_first(mapping, "near_miss_score", "near_miss_top1_score", "top1_score", "score"))


def _weighted_mean_p95(buckets: Iterable[tuple[int, float]]) -> float:
    total_w = 0
    weighted_sum = 0.0
    for cnt, val in buckets:
        w = max(0, int(cnt))
        if w > 0 and math.isfinite(val):
            total_w += w
            weighted_sum += w * float(val)
    if total_w <= 0:
        return 0.0
    return round(weighted_sum / total_w, 2)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * pct
    lo = int(idx)
    hi = min(len(s) - 1, lo + 1)
    frac = idx - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _safe_ratio(num: int | float, den: int | float) -> float | None:
    if den <= 0:
        return None
    return round(float(num) / float(den), 4)


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


def _fmt_num(val: float | int | None, suffix: str = "") -> str:
    if val is None:
        return "N/A"
    if isinstance(val, int) and not isinstance(val, bool):
        return f"{val}{suffix}"
    return f"{float(val):.2f}{suffix}"


def _fmt_mmss(game_time_s: float | None) -> str:
    if game_time_s is None:
        return "PRE"
    total = max(0, int(round(game_time_s)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _safe_file_token(raw: str, fallback: str = "ver") -> str:
    cleaned = _SAFE_FILE_RE.sub("_", (raw or "").strip()).strip("._")
    return cleaned[:48] if cleaned else fallback


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, data: Any) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _dir_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    try:
        for root, _dirs, files in os.walk(path):
            for fname in files:
                try:
                    total += os.path.getsize(os.path.join(root, fname))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _parse_seq_from_dir(dir_name: str) -> int:
    m = _MATCH_DIR_SEQ_RE.match(dir_name or "")
    return int(m.group(1)) if m else 0


def _is_auto_open_source(source: str) -> bool:
    low = (source or "").strip().lower()
    if not low:
        return False
    if low in _AUTO_OPEN_SOURCES or low.startswith("auto"):
        return True
    if low in _USER_INITIATED_OPEN_SOURCES or any(low.startswith(p) for p in _MANUAL_OPEN_PREFIXES):
        return False
    return False


def _is_user_initiated_open_source(source: str) -> bool:
    low = (source or "").strip().lower()
    if not low:
        return False
    if low in _USER_INITIATED_OPEN_SOURCES:
        return True
    return any(low.startswith(p) for p in _MANUAL_OPEN_PREFIXES)


def _extract_resolution_key(payload: dict[str, Any]) -> str:
    res = str(_first(payload, "resolution", "screen_res", default="") or "").strip()
    if not res:
        w = _safe_int(_first(payload, "screen_w", "screen_width", "width", "frame_width"), 0)
        h = _safe_int(_first(payload, "screen_h", "screen_height", "height", "frame_height"), 0)
        if w > 0 and h > 0:
            res = f"{w}x{h}"
        else:
            rect = payload.get("game_rect")
            if isinstance(rect, (list, tuple)) and len(rect) == 4:
                rw = _safe_int(rect[2], 0)
                rh = _safe_int(rect[3], 0)
                if rw > 0 and rh > 0:
                    res = f"{rw}x{rh}"
    if not res:
        ar = str(payload.get("aspect_ratio") or "").strip()
        if ar:
            res = f"ar:{ar}"
    if not res:
        return ""
    m_sfx = _RES_DPR_SUFFIX_RE.match(res)
    if m_sfx:
        base_res = m_sfx.group(1).strip()
        sfx_dpr = _opt_float(m_sfx.group(2))
        if sfx_dpr is not None and sfx_dpr > 0:
            if abs(sfx_dpr - 1.0) < 0.005:
                res = base_res
            else:
                return f"{base_res}@{sfx_dpr:.2f}x"
    dpr = _opt_float(_first(payload, "dpr", "dpi_scale", "device_pixel_ratio"))
    if dpr is not None and dpr > 0 and abs(dpr - 1.0) >= 0.005:
        return f"{res}@{dpr:.2f}x"
    return res


def _extract_roi_refs(payload: dict[str, Any], match_dir_name: str, ev_seq: int, disk_rois: list[str]) -> list[str]:
    refs: list[str] = []
    for k in ("roi_path", "roi_snapshot", "snapshot_path"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            refs.append(v.strip())
    for k in ("roi_paths", "roi_snapshots"):
        v = payload.get(k)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    refs.append(item.strip())
    if ev_seq > 0 and disk_rois:
        prefix = f"ev{ev_seq:04d}_"
        for rel in disk_rois:
            if Path(rel).name.startswith(prefix):
                refs.append(rel)
    # Normalize with match_dir_name prefix for cross-match clarity
    out: list[str] = []
    seen: set[str] = set()
    for r in refs:
        norm = r.replace("\\", "/")
        if match_dir_name and not norm.startswith(match_dir_name + "/") and norm.startswith("roi_snapshots/"):
            norm = f"{match_dir_name}/{norm}"
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


# ---------------------------------------------------------------------------
# Public Data Structures
# ---------------------------------------------------------------------------


@dataclass
class DetectedIssue:
    """Actionable issue produced by one of the 10 deterministic DevLab detectors."""

    issue_id: str
    pillar: str
    severity: str  # "P0" | "P1" | "P2"
    detector_rule: str
    sample_count: int
    metric_value: float
    summary: str
    target_files_and_symbols: list[str] = field(default_factory=list)
    evidence_match_seqs: list[int] = field(default_factory=list)
    evidence_roi_paths: list[str] = field(default_factory=list)
    proposed_experiment: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchReplayResult:
    """Offline replay result for a single recorded DevLab match."""

    seq: int
    dir_name: str
    match_dir: str
    match_session_id: str
    match_key_hash: str
    app_version: str
    started_at: str
    ended_at: str
    hero_id: str
    hero_name: str
    dataset_tag: str
    game_mode: str
    is_non_mayhem: bool
    won: int
    duration_s: int
    eog_source: str
    event_count: int
    corrupt_lines: int
    roi_paths: list[str] = field(default_factory=list)
    kpis: dict[str, Any] = field(default_factory=dict)
    issues: list[DetectedIssue] = field(default_factory=list)
    timeline_markdown: str = ""
    timeline_path: str = ""
    summary_path: str = ""
    facts: dict[str, Any] = field(default_factory=dict)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "schema_v": EVENT_SCHEMA_VERSION,
            "seq": self.seq,
            "dir_name": self.dir_name,
            "match_session_id": self.match_session_id,
            "match_key_hash": self.match_key_hash,
            "app_version": self.app_version,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "hero_id": self.hero_id,
            "hero_name": self.hero_name,
            "dataset_tag": self.dataset_tag,
            "game_mode": self.game_mode,
            "is_non_mayhem": self.is_non_mayhem,
            "won": self.won,
            "duration_s": self.duration_s,
            "eog_source": self.eog_source,
            "event_count": self.event_count,
            "corrupt_lines": self.corrupt_lines,
            "roi_paths": list(self.roi_paths),
            "kpis": self.kpis,
            "issues": [iss.to_dict() for iss in self.issues],
            "timeline_path": self.timeline_path,
        }


@dataclass
class EvolutionReport:
    """Multi-match 5-pillar evolution summary and AI-consumable optimization backlog."""

    generated_at: str
    stamp: str
    root_dir: str
    total_matches: int
    mayhem_matches: int
    non_mayhem_matches: int
    match_seqs: list[int] = field(default_factory=list)
    mayhem_match_seqs: list[int] = field(default_factory=list)
    non_mayhem_match_seqs: list[int] = field(default_factory=list)
    pillars: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_resolution: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_hero: dict[str, dict[str, Any]] = field(default_factory=dict)
    roi_index: list[dict[str, Any]] = field(default_factory=list)
    issues: list[DetectedIssue] = field(default_factory=list)
    report_markdown: str = ""
    report_path: str = ""
    backlog_path: str = ""
    latest_report_path: str = ""
    latest_backlog_path: str = ""

    def to_backlog_dict(self) -> dict[str, Any]:
        return {
            "schema_v": EVENT_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "stamp": self.stamp,
            "root_dir": self.root_dir,
            "total_matches": self.total_matches,
            "mayhem_matches": self.mayhem_matches,
            "non_mayhem_matches": self.non_mayhem_matches,
            "match_seqs": list(self.match_seqs),
            "mayhem_match_seqs": list(self.mayhem_match_seqs),
            "non_mayhem_match_seqs": list(self.non_mayhem_match_seqs),
            "pillars": self.pillars,
            "by_resolution": self.by_resolution,
            "by_hero": self.by_hero,
            "issues": [iss.to_dict() for iss in self.issues],
            "roi_index": list(self.roi_index),
        }


@dataclass
class VersionDiffReport:
    """Cross-version A/B comparison across all 5 evolution pillars."""

    generated_at: str
    ver_a: str
    ver_b: str
    matches_a: int
    matches_b: int
    mayhem_matches_a: int
    mayhem_matches_b: int
    pillars_a: dict[str, dict[str, Any]] = field(default_factory=dict)
    pillars_b: dict[str, dict[str, Any]] = field(default_factory=dict)
    deltas: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    issues_a: list[DetectedIssue] = field(default_factory=list)
    issues_b: list[DetectedIssue] = field(default_factory=list)
    diff_markdown: str = ""
    diff_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_v": EVENT_SCHEMA_VERSION,
            "generated_at": self.generated_at,
            "ver_a": self.ver_a,
            "ver_b": self.ver_b,
            "matches_a": self.matches_a,
            "matches_b": self.matches_b,
            "mayhem_matches_a": self.mayhem_matches_a,
            "mayhem_matches_b": self.mayhem_matches_b,
            "pillars_a": self.pillars_a,
            "pillars_b": self.pillars_b,
            "deltas": self.deltas,
            "issues_a": [i.to_dict() for i in self.issues_a],
            "issues_b": [i.to_dict() for i in self.issues_b],
            "diff_path": self.diff_path,
        }


# ---------------------------------------------------------------------------
# 10 Deterministic Automated Issue Detectors
# ---------------------------------------------------------------------------


def run_issue_detectors(match_replays: list[MatchReplayResult]) -> list[DetectedIssue]:
    """Run all 10 deterministic automated issue detectors across one or more match replays."""
    if not match_replays:
        return []

    issues: list[DetectedIssue] = []

    det = _detect_eog_race_lost(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_ocr_silent_miss_at_unlock(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_ocr_nearmiss_or_pass2_shadowed(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_reroll_desync_or_y_clip(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_empty_icon_bank_or_hud_misalign(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_tab_passive_hover_pollution(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_scoring_trap_or_profile_conflict(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_coach_timing_and_hallucination(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_overlay_irritation_and_overflow(match_replays)
    if det is not None:
        issues.append(det)

    det = _detect_main_thread_stall_and_click_starvation(match_replays)
    if det is not None:
        issues.append(det)

    issues.sort(key=lambda i: (_SEVERITY_ORDER.get(i.severity, 9), -i.sample_count, i.detector_rule))
    return issues


def _detect_eog_race_lost(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    eligible: list[MatchReplayResult] = []
    lost: list[MatchReplayResult] = []
    for r in replays:
        eff_dur = max(r.duration_s, _safe_int(r.facts.get("max_game_time_s"), 0))
        if eff_dur >= 180:
            eligible.append(r)
            src = (r.eog_source or "").strip().lower()
            if r.won not in (0, 1) or src == "timeout":
                lost.append(r)
    if not eligible:
        return None
    rate = len(lost) / len(eligible)
    if rate <= 0.05:
        return None
    severity = "P0" if (rate >= 0.20 or len(lost) >= 3) else "P1"
    return DetectedIssue(
        issue_id="DET_EOG_RACE_LOST",
        pillar="runtime",
        severity=severity,
        detector_rule="DET_EOG_RACE_LOST",
        sample_count=len(lost),
        metric_value=round(rate, 4),
        summary=(
            f"{len(lost)}/{len(eligible)} ({rate * 100:.1f}%) matches with duration >= 180s "
            f"lost authoritative EOG outcome (won=-1 or eog_source=timeout)."
        ),
        target_files_and_symbols=[
            "src/hex_helper/app.py:HexHelperController._apply_lcu_poll",
            "src/hex_helper/app.py:HexStateMachine.on_lcu_poll",
            "src/hex_helper/game/lcu_client.py:LcuClient.get_eog_snapshot",
            "src/hex_helper/game/lcu_client.py:LcuClient.get_match_history_eog_fallback",
        ],
        evidence_match_seqs=sorted({r.seq for r in lost}),
        evidence_roi_paths=[],
        proposed_experiment=(
            "Verify 3-consecutive-failure LiveClient 2999 debounce, extend PENDING_EOG buffer to 20s, "
            "and confirm get_match_history_eog_fallback(game_id=...) resolves missing EOG stats blocks."
        ),
        details={
            "eligible_matches": len(eligible),
            "lost_matches": len(lost),
            "eog_sources": dict(Counter(r.eog_source or "empty" for r in lost)),
        },
    )


def _detect_ocr_silent_miss_at_unlock(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    misses: list[dict[str, Any]] = []
    seqs: set[int] = set()
    rois: list[str] = []
    by_level: Counter[int] = Counter()
    by_res: Counter[str] = Counter()

    for r in replays:
        m_misses = r.facts.get("unlock_silent_misses") or []
        for item in m_misses:
            misses.append(item)
            seqs.add(r.seq)
            by_level[_safe_int(item.get("milestone_level"), 0)] += 1
            res_k = str(r.facts.get("primary_resolution") or "unknown")
            by_res[res_k] += 1
        if m_misses:
            rois.extend(r.roi_paths[:3])

    if not misses:
        return None

    total_milestones = sum(_safe_int(r.facts.get("unlock_milestones_reached"), 0) for r in replays)
    rate = len(misses) / max(1, total_milestones)
    severity = "P0" if (len(misses) >= 2 or rate >= 0.15) else "P1"
    return DetectedIssue(
        issue_id="DET_OCR_SILENT_MISS_AT_UNLOCK_LEVEL",
        pillar="vision",
        severity=severity,
        detector_rule="DET_OCR_SILENT_MISS_AT_UNLOCK_LEVEL",
        sample_count=len(misses),
        metric_value=round(rate, 4) if total_milestones > 0 else float(len(misses)),
        summary=(
            f"Detected {len(misses)} silent augment picker miss(es) within 45s after unlock levels "
            f"{dict(sorted(by_level.items()))} with zero picker open transitions."
        ),
        target_files_and_symbols=[
            "src/hex_helper/vision/detector.py:AugmentPickerDetector._quick_gate",
            "src/hex_helper/vision/detector.py:AugmentPickerDetector.process_frame",
            "src/hex_helper/game/action_tracker.py:MatchActionTracker._process_level_and_unlock_milestones",
        ],
        evidence_match_seqs=sorted(seqs),
        evidence_roi_paths=list(dict.fromkeys(rois))[:12],
        proposed_experiment=(
            "Inspect game_rect aspect ratio / DPI scaling and _quick_gate luminance thresholds for "
            f"affected resolutions ({dict(by_res)}); run offline ROI replay on level 3/7/11/15 frames."
        ),
        details={
            "silent_miss_count": len(misses),
            "total_milestones": total_milestones,
            "by_level": dict(sorted(by_level.items())),
            "by_resolution": dict(by_res),
        },
    )


def _detect_ocr_nearmiss_or_pass2_shadowed(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    nearmiss_terms: Counter[str] = Counter()
    nearmiss_top1: dict[str, str] = {}
    shadowed_cases: list[dict[str, Any]] = []
    short_sub_cases: list[dict[str, Any]] = []
    seqs: set[int] = set()
    rois: list[str] = []

    for r in replays:
        nms = r.facts.get("ocr_near_misses") or []
        shs = r.facts.get("ocr_shadowed_pass2") or []
        subs = r.facts.get("ocr_short_substring_hits") or []
        for nm in nms:
            raw = str(nm.get("raw_text") or "").strip() or "<empty>"
            nearmiss_terms[raw] += 1
            if nm.get("top1_id"):
                nearmiss_top1[raw] = str(nm["top1_id"])
            seqs.add(r.seq)
            rois.extend(nm.get("roi_paths") or [])
        for sh in shs:
            shadowed_cases.append(sh)
            seqs.add(r.seq)
            rois.extend(sh.get("roi_paths") or [])
        for sb in subs:
            short_sub_cases.append(sb)
            seqs.add(r.seq)
            rois.extend(sb.get("roi_paths") or [])

    total = sum(nearmiss_terms.values()) + len(shadowed_cases) + len(short_sub_cases)
    if total == 0:
        return None

    severity = "P0" if (len(shadowed_cases) >= 2 or total >= 5) else "P1"
    top_terms = [f"{t}({c})" for t, c in nearmiss_terms.most_common(5)]
    parts: list[str] = []
    if nearmiss_terms:
        parts.append(f"{sum(nearmiss_terms.values())} near-miss(es) [0.42-0.58] top={', '.join(top_terms)}")
    if shadowed_cases:
        parts.append(f"{len(shadowed_cases)} pass2 line(s) shadowed by pass1 dedup")
    if short_sub_cases:
        parts.append(f"{len(short_sub_cases)} 2-char short-name substring risk hit(s)")

    return DetectedIssue(
        issue_id="DET_OCR_NEARMISS_OR_PASS2_SHADOWED",
        pillar="vision",
        severity=severity,
        detector_rule="DET_OCR_NEARMISS_OR_PASS2_SHADOWED",
        sample_count=total,
        metric_value=float(total),
        summary="OCR matching anomalies: " + "; ".join(parts) + ".",
        target_files_and_symbols=[
            "src/hex_helper/vision/ocr_winrt.py:WinRtOcrEngine.recognize_lines",
            "src/hex_helper/vision/matcher.py:AugmentMatcher.match_line",
            "src/hex_helper/vision/detector.py:AugmentPickerDetector.process_frame",
        ],
        evidence_match_seqs=sorted(seqs),
        evidence_roi_paths=list(dict.fromkeys(rois))[:16],
        proposed_experiment=(
            "Audit top near-miss raw_text terms for patch-renamed/new augments, prefer higher-score/longer "
            "pass2 text during pass1/pass2 bbox dedup, and tighten substring bonus for <=2-char augment names."
        ),
        details={
            "nearmiss_count": sum(nearmiss_terms.values()),
            "nearmiss_top_terms": [
                {"raw_text": t, "count": c, "top1_id": nearmiss_top1.get(t, "")}
                for t, c in nearmiss_terms.most_common(10)
            ],
            "shadowed_pass2_count": len(shadowed_cases),
            "shadowed_samples": shadowed_cases[:5],
            "short_substring_count": len(short_sub_cases),
        },
    )


def _detect_reroll_desync_or_y_clip(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    desync_cases: list[dict[str, Any]] = []
    y_clip_cases: list[dict[str, Any]] = []
    clipped_ys: list[float] = []
    seqs: set[int] = set()
    rois: list[str] = []

    for r in replays:
        for d in r.facts.get("reroll_desync_events") or []:
            desync_cases.append(d)
            seqs.add(r.seq)
            rois.extend(d.get("roi_paths") or [])
        for y in r.facts.get("y_threshold_clip_events") or []:
            y_clip_cases.append(y)
            if y.get("cursor_rel_y") is not None:
                clipped_ys.append(float(y["cursor_rel_y"]))
            seqs.add(r.seq)
            rois.extend(y.get("roi_paths") or [])

    total = len(desync_cases) + len(y_clip_cases)
    if total == 0:
        return None

    suggested_y = round(min(0.90, max(clipped_ys) + 0.025), 3) if clipped_ys else 0.85
    severity = "P0" if (len(desync_cases) >= 2 or len(y_clip_cases) >= 3) else "P1"
    parts: list[str] = []
    if desync_cases:
        parts.append(f"{len(desync_cases)} Reroll Desync (<850ms followed by manual replace)")
    if y_clip_cases:
        parts.append(
            f"{len(y_clip_cases)} click(s) clipped in reroll zone at y in [0.76, 0.83] "
            f"(suggested reroll_y_threshold_rel={suggested_y})"
        )

    return DetectedIssue(
        issue_id="DET_REROLL_DESYNC_OR_Y_THRESHOLD_CLIP",
        pillar="vision",
        severity=severity,
        detector_rule="DET_REROLL_DESYNC_OR_Y_THRESHOLD_CLIP",
        sample_count=total,
        metric_value=float(total),
        summary="Click/Reroll attribution defects: " + "; ".join(parts) + ".",
        target_files_and_symbols=[
            "src/hex_helper/vision/click_tracker.py:ClickTracker.resolve_selection",
            "src/hex_helper/vision/click_tracker.py:ClickTracker._compute_close_cursor_context",
            "src/hex_helper/vision/detector.py:AugmentPickerDetector.process_frame",
        ],
        evidence_match_seqs=sorted(seqs),
        evidence_roi_paths=list(dict.fromkeys(rois))[:12],
        proposed_experiment=(
            f"Raise reroll_y_threshold_rel to {suggested_y} to avoid clipping bottom-of-card picks, "
            "and extend post-reroll card-refresh lock / stale-slot invalidation window past 850ms."
        ),
        details={
            "reroll_desync_count": len(desync_cases),
            "y_clip_count": len(y_clip_cases),
            "clipped_cursor_rel_ys": [round(v, 4) for v in clipped_ys[:20]],
            "suggested_reroll_y_threshold_rel": suggested_y,
        },
    )


def _detect_empty_icon_bank_or_hud_misalign(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    empty_bank = 0
    flat_patches = 0
    consec_streaks = 0
    by_res: Counter[str] = Counter()
    seqs: set[int] = set()
    rois: list[str] = []

    for r in replays:
        eb = _safe_int(r.facts.get("icon_empty_bank_count"), 0)
        fp = _safe_int(r.facts.get("icon_flat_patch_count"), 0)
        cs = _safe_int(r.facts.get("icon_consecutive_miss_streaks"), 0)
        if eb or fp or cs:
            empty_bank += eb
            flat_patches += fp
            consec_streaks += cs
            seqs.add(r.seq)
            res_k = str(r.facts.get("icon_resolution") or r.facts.get("primary_resolution") or "unknown")
            by_res[res_k] += eb + fp + cs
            rois.extend(r.facts.get("icon_anomaly_roi_paths") or [])

    total = empty_bank + flat_patches + consec_streaks
    if total == 0:
        return None

    severity = "P0" if (empty_bank >= 1 or flat_patches >= 2) else "P1"
    parts: list[str] = []
    if empty_bank:
        parts.append(f"{empty_bank} skipped_empty_bank (template_count=0)")
    if flat_patches:
        parts.append(f"{flat_patches} flat HUD patch verify(s) (all 4 slots patch_std < 8.0)")
    if consec_streaks:
        parts.append(f"{consec_streaks} consecutive low-score miss streak(s) (>=3)")

    return DetectedIssue(
        issue_id="DET_EMPTY_ICON_BANK_OR_HUD_MISALIGN",
        pillar="vision",
        severity=severity,
        detector_rule="DET_EMPTY_ICON_BANK_OR_HUD_MISALIGN",
        sample_count=total,
        metric_value=float(total),
        summary="HUD icon verifier failure: " + "; ".join(parts) + ".",
        target_files_and_symbols=[
            "src/hex_helper/vision/augment_icon_matcher.py:AugmentIconMatcher.reload_cached_icons",
            "src/hex_helper/vision/augment_icon_matcher.py:AugmentIconMatcher.verify_picked_augment",
            "src/hex_helper/vision/augment_icon_matcher.py:AugmentIconMatcher._resolve_game_view",
        ],
        evidence_match_seqs=sorted(seqs),
        evidence_roi_paths=list(dict.fromkeys(rois))[:12],
        proposed_experiment=(
            "Ensure reload_cached_icons() runs immediately after seed extraction/icon sync, and inspect "
            f"4x28x28 HUD strip crops under {dict(by_res)} to recalibrate ultrawide/HUD-scale slot ROIs."
        ),
        details={
            "empty_bank_count": empty_bank,
            "flat_patch_count": flat_patches,
            "consecutive_miss_streaks": consec_streaks,
            "by_resolution": dict(by_res),
        },
    )


def _detect_tab_passive_hover_pollution(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    total_passive_hits = 0
    tab_zone_hits = 0
    tab_removed = 0
    seqs: set[int] = set()
    rois: list[str] = []

    for r in replays:
        total_passive_hits += _safe_int(r.facts.get("passive_hover_hits"), 0)
        tab_zone_hits += _safe_int(r.facts.get("tab_zone_hits"), 0)
        removed_here = _safe_int(r.facts.get("tab_zone_removed_count"), 0)
        if removed_here > 0:
            tab_removed += removed_here
            seqs.add(r.seq)
            rois.extend(r.facts.get("transmute_roi_paths") or [])

    if tab_removed == 0:
        return None

    denom = max(1, total_passive_hits, tab_zone_hits, tab_removed)
    fp_rate = tab_removed / denom
    severity = "P0" if (tab_removed >= 3 or fp_rate >= 0.25) else "P1"
    return DetectedIssue(
        issue_id="DET_TAB_PASSIVE_HOVER_POLLUTION",
        pillar="vision",
        severity=severity,
        detector_rule="DET_TAB_PASSIVE_HOVER_POLLUTION",
        sample_count=tab_removed,
        metric_value=round(fp_rate, 4),
        summary=(
            f"{tab_removed} passive HUD hover hit(s) in cursor_zone='tab_zone' were manually removed "
            f"by the player (Tab scoreboard pollution rate={fp_rate * 100:.1f}% across {denom} passive hits)."
        ),
        target_files_and_symbols=[
            "src/hex_helper/vision/transmute.py:TransmuteRevealWatcher.scan_passive_hud_hover",
            "src/hex_helper/vision/transmute.py:classify_cursor_zone",
        ],
        evidence_match_seqs=sorted(seqs),
        evidence_roi_paths=list(dict.fromkeys(rois))[:10],
        proposed_experiment=(
            "Gate scan_passive_hud_hover to only commit augments when cursor_zone == 'bottom_hud' "
            "(strictly reject cursor_zone == 'tab_zone' to block reading other players' augments on Tab)."
        ),
        details={
            "tab_removed_count": tab_removed,
            "tab_zone_hits": tab_zone_hits,
            "total_passive_hits": total_passive_hits,
            "false_positive_rate": round(fp_rate, 4),
        },
    )


def _detect_scoring_trap_or_profile_conflict(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    aug_top1_offers: Counter[str] = Counter()
    aug_top1_adopted: Counter[str] = Counter()
    aug_top1_rerolled: Counter[str] = Counter()
    aug_seqs: dict[str, set[int]] = defaultdict(set)

    item_top1_offers: Counter[str] = Counter()
    item_top1_adopted: Counter[str] = Counter()
    item_seqs: dict[str, set[int]] = defaultdict(set)

    hero_purchases: Counter[str] = Counter()
    hero_overrides: Counter[str] = Counter()
    hero_override_seqs: dict[str, set[int]] = defaultdict(set)

    profile_conflicts: list[dict[str, Any]] = []
    conflict_seqs: set[int] = set()

    for r in replays:
        hero = r.hero_id or "unknown"
        for aid, cnt in (r.facts.get("aug_top1_offers") or {}).items():
            aug_top1_offers[str(aid)] += _safe_int(cnt, 0)
            aug_seqs[str(aid)].add(r.seq)
        for aid, cnt in (r.facts.get("aug_top1_adopted") or {}).items():
            aug_top1_adopted[str(aid)] += _safe_int(cnt, 0)
        for aid, cnt in (r.facts.get("aug_top1_rerolled") or {}).items():
            aug_top1_rerolled[str(aid)] += _safe_int(cnt, 0)

        for iid, cnt in (r.facts.get("item_top1_offers") or {}).items():
            item_top1_offers[str(iid)] += _safe_int(cnt, 0)
            item_seqs[str(iid)].add(r.seq)
        for iid, cnt in (r.facts.get("item_top1_adopted") or {}).items():
            item_top1_adopted[str(iid)] += _safe_int(cnt, 0)

        p_cnt = _safe_int(r.facts.get("meaningful_purchases"), 0)
        if "player_component_override_count" in r.facts:
            o_cnt = _safe_int(r.facts.get("player_component_override_count"), 0)
        else:
            o_cnt = _safe_int(r.facts.get("plan_override_count"), 0)
        hero_purchases[hero] += p_cnt
        hero_overrides[hero] += o_cnt
        if o_cnt > 0:
            hero_override_seqs[hero].add(r.seq)

        for conf in r.facts.get("profile_conflicts") or []:
            profile_conflicts.append({"seq": r.seq, "hero_id": hero, **conf})
            conflict_seqs.add(r.seq)

    trap_augments: list[dict[str, Any]] = []
    evidence_seqs: set[int] = set()
    for aid, offers in aug_top1_offers.items():
        if offers < 5:
            continue
        adopt_r = aug_top1_adopted[aid] / offers
        reroll_r = aug_top1_rerolled[aid] / offers
        if adopt_r < 0.35 or reroll_r > 0.45:
            trap_augments.append(
                {
                    "augment_id": aid,
                    "top1_offers": offers,
                    "adopt_rate": round(adopt_r, 4),
                    "reroll_rate": round(reroll_r, 4),
                }
            )
            evidence_seqs.update(aug_seqs[aid])

    trap_items: list[dict[str, Any]] = []
    for iid, offers in item_top1_offers.items():
        if offers < 5:
            continue
        adopt_r = item_top1_adopted[iid] / offers
        if adopt_r < 0.35:
            trap_items.append(
                {
                    "item_id": iid,
                    "top1_offers": offers,
                    "adopt_rate": round(adopt_r, 4),
                }
            )
            evidence_seqs.update(item_seqs[iid])

    high_override_heroes: list[dict[str, Any]] = []
    for hero, ov_cnt in hero_overrides.items():
        if ov_cnt <= 0:
            continue
        p_cnt = hero_purchases[hero]
        denom = max(p_cnt, ov_cnt)
        ov_rate = ov_cnt / denom
        if ov_rate > 0.30 and (p_cnt >= 3 or ov_cnt >= 2):
            high_override_heroes.append(
                {
                    "hero_id": hero,
                    "overrides": ov_cnt,
                    "purchases": p_cnt,
                    "override_rate": round(ov_rate, 4),
                }
            )
            evidence_seqs.update(hero_override_seqs[hero])

    if profile_conflicts:
        evidence_seqs.update(conflict_seqs)

    findings = len(trap_augments) + len(trap_items) + len(high_override_heroes) + len(profile_conflicts)
    if findings == 0:
        return None

    parts: list[str] = []
    if trap_augments:
        parts.append(f"{len(trap_augments)} overrated trap augment(s)")
    if trap_items:
        parts.append(f"{len(trap_items)} overrated trap item(s)")
    if high_override_heroes:
        parts.append(f"{len(high_override_heroes)} hero(es) with >30% >=800G route override rate")
    if profile_conflicts:
        parts.append(f"{len(profile_conflicts)} cross-engine matchup vs combo hero_profile conflict(s)")

    max_override_rate = max((h["override_rate"] for h in high_override_heroes), default=0.0)
    conflict_match_rate = len(conflict_seqs) / max(1, len(replays))
    severity = (
        "P0"
        if (len(trap_augments) + len(trap_items) >= 2 or max_override_rate > 0.50 or conflict_match_rate > 0.25)
        else "P1"
    )

    return DetectedIssue(
        issue_id="DET_SCORING_TRAP_OR_PROFILE_CONFLICT",
        pillar="recommendation",
        severity=severity,
        detector_rule="DET_SCORING_TRAP_OR_PROFILE_CONFLICT",
        sample_count=findings,
        metric_value=round(max(max_override_rate, conflict_match_rate, float(len(trap_augments) + len(trap_items))), 4),
        summary="Scoring & build-plan mismatch: " + "; ".join(parts) + ".",
        target_files_and_symbols=[
            "src/hex_helper/scoring/matchup_optimizer.py:optimize_next_items",
            "src/hex_helper/scoring/matchup_optimizer.py:reconcile_match_build_plan_with_trace",
            "src/hex_helper/scoring/matchup_optimizer.py:infer_hero_profile",
            "src/hex_helper/scoring/augment_combo.py:evaluate_picker_combos",
            "src/hex_helper/scoring/augment_combo.py:infer_match_build_profile",
        ],
        evidence_match_seqs=sorted(evidence_seqs),
        evidence_roi_paths=[],
        proposed_experiment=(
            "Inspect score_breakdown for overrated trap picks, unify infer_hero_profile vs infer_match_build_profile "
            "axis classification, and review >=800G component continuity bonuses in reconcile_match_build_plan."
        ),
        details={
            "trap_augments": trap_augments,
            "trap_items": trap_items,
            "high_override_heroes": high_override_heroes,
            "profile_conflicts": profile_conflicts[:15],
        },
    )


def _detect_coach_timing_and_hallucination(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    picker_ai_total = 0
    picker_ai_late = 0
    death_ai_total = 0
    death_ai_late = 0
    coach_turns_total = 0
    grounding_blocked_turns = 0
    grounding_breakdown: Counter[str] = Counter()
    gate_skips_total = 0
    f6_after_gate_skip = 0

    picker_seqs: set[int] = set()
    death_seqs: set[int] = set()
    grounding_seqs: set[int] = set()
    gate_seqs: set[int] = set()

    for r in replays:
        pt = _safe_int(r.facts.get("picker_ai_total"), 0)
        pl = _safe_int(r.facts.get("picker_ai_late"), 0)
        picker_ai_total += pt
        picker_ai_late += pl
        if pl > 0:
            picker_seqs.add(r.seq)

        dt = _safe_int(r.facts.get("death_ai_buy_windows"), 0)
        dl = _safe_int(r.facts.get("death_ai_late_windows"), 0)
        death_ai_total += dt
        death_ai_late += dl
        if dl > 0:
            death_seqs.add(r.seq)

        ct = _safe_int(r.facts.get("coach_turns_total"), 0)
        gb = _safe_int(r.facts.get("grounding_blocked_turns"), 0)
        coach_turns_total += ct
        grounding_blocked_turns += gb
        if gb > 0:
            grounding_seqs.add(r.seq)
        for k, v in (r.facts.get("grounding_breakdown") or {}).items():
            grounding_breakdown[str(k)] += _safe_int(v, 0)

        gs = _safe_int(r.facts.get("smart_gate_skips"), 0)
        f6 = _safe_int(r.facts.get("f6_within_10s_after_gate_skip"), 0)
        gate_skips_total += gs
        f6_after_gate_skip += f6
        if f6 > 0:
            gate_seqs.add(r.seq)

    picker_late_rate = picker_ai_late / picker_ai_total if picker_ai_total > 0 else 0.0
    death_late_rate = death_ai_late / death_ai_total if death_ai_total > 0 else 0.0
    grounding_rate = grounding_blocked_turns / coach_turns_total if coach_turns_total > 0 else 0.0
    gate_f6_rate = f6_after_gate_skip / gate_skips_total if gate_skips_total > 0 else 0.0

    triggered_parts: list[str] = []
    evidence_seqs: set[int] = set()
    samples = 0
    worst_rate = 0.0

    if picker_ai_total >= 1 and picker_late_rate > 0.25:
        triggered_parts.append(f"picker AI late {picker_ai_late}/{picker_ai_total} ({picker_late_rate * 100:.1f}% > 25%)")
        evidence_seqs.update(picker_seqs)
        samples += picker_ai_late
        worst_rate = max(worst_rate, picker_late_rate)

    if death_ai_total >= 1 and death_late_rate > 0.30:
        triggered_parts.append(f"death-shop AI late {death_ai_late}/{death_ai_total} ({death_late_rate * 100:.1f}% > 30%)")
        evidence_seqs.update(death_seqs)
        samples += death_ai_late
        worst_rate = max(worst_rate, death_late_rate)

    if coach_turns_total >= 1 and grounding_rate > 0.10:
        triggered_parts.append(
            f"grounding blocked {grounding_blocked_turns}/{coach_turns_total} turns ({grounding_rate * 100:.1f}% > 10%)"
        )
        evidence_seqs.update(grounding_seqs)
        samples += grounding_blocked_turns
        worst_rate = max(worst_rate, grounding_rate)

    if gate_skips_total >= 1 and gate_f6_rate > 0.20:
        triggered_parts.append(
            f"manual F6 within 10s after Smart Gate skip {f6_after_gate_skip}/{gate_skips_total} "
            f"({gate_f6_rate * 100:.1f}% > 20%)"
        )
        evidence_seqs.update(gate_seqs)
        samples += f6_after_gate_skip
        worst_rate = max(worst_rate, gate_f6_rate)

    if not triggered_parts:
        return None

    severity = (
        "P0"
        if (grounding_rate > 0.25 or picker_late_rate > 0.50 or death_late_rate > 0.50)
        else "P1"
    )
    return DetectedIssue(
        issue_id="DET_COACH_TIMING_AND_HALLUCINATION",
        pillar="coach",
        severity=severity,
        detector_rule="DET_COACH_TIMING_AND_HALLUCINATION",
        sample_count=samples,
        metric_value=round(worst_rate, 4),
        summary="AI coach value/timing degradation: " + "; ".join(triggered_parts) + ".",
        target_files_and_symbols=[
            "src/hex_helper/coach/gemini_coach.py:ground_coach_advice",
            "src/hex_helper/coach/gemini_coach.py:build_coach_prompt_payload",
            "src/hex_helper/app.py:HexHelperController.trigger_coach_request",
            "src/hex_helper/app.py:HexHelperController._should_defer_death_coach",
        ],
        evidence_match_seqs=sorted(evidence_seqs),
        evidence_roi_paths=[],
        proposed_experiment=(
            "Lower thinking_level on picker/death triggers to cut arrival latency, tighten allowed-item glossary "
            "constraints to reduce grounding blocks, and retune Smart Picker Gate 0.018 gap threshold."
        ),
        details={
            "picker_ai_late": picker_ai_late,
            "picker_ai_total": picker_ai_total,
            "picker_late_rate": round(picker_late_rate, 4),
            "death_ai_late": death_ai_late,
            "death_ai_total": death_ai_total,
            "death_late_rate": round(death_late_rate, 4),
            "grounding_blocked_turns": grounding_blocked_turns,
            "coach_turns_total": coach_turns_total,
            "grounding_block_rate": round(grounding_rate, 4),
            "grounding_breakdown": dict(grounding_breakdown),
            "f6_after_gate_skip": f6_after_gate_skip,
            "gate_skips_total": gate_skips_total,
            "gate_f6_rate": round(gate_f6_rate, 4),
        },
    )


def _detect_overlay_irritation_and_overflow(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    auto_total_by_src: Counter[str] = Counter()
    auto_rapid_by_src: Counter[str] = Counter()
    auto_seqs_by_src: dict[str, set[int]] = defaultdict(set)

    detached_by_res: Counter[str] = Counter()
    detached_seqs: dict[str, set[int]] = defaultdict(set)

    ingame_overlay_sessions = 0
    scroll_overflow_sessions = 0
    scroll_seqs: set[int] = set()

    ghost_repopups = 0
    ghost_seqs: set[int] = set()

    for r in replays:
        for src, cnt in (r.facts.get("auto_sessions_by_source") or {}).items():
            auto_total_by_src[str(src)] += _safe_int(cnt, 0)
        for src, cnt in (r.facts.get("auto_rapid_by_source") or {}).items():
            c = _safe_int(cnt, 0)
            if c > 0:
                auto_rapid_by_src[str(src)] += c
                auto_seqs_by_src[str(src)].add(r.seq)

        for res_k, cnt in (r.facts.get("detached_edge_by_res") or {}).items():
            c = _safe_int(cnt, 0)
            if c > 0:
                detached_by_res[str(res_k)] += c
                detached_seqs[str(res_k)].add(r.seq)

        ingame_overlay_sessions += _safe_int(r.facts.get("ingame_overlay_sessions"), 0)
        ovf = _safe_int(r.facts.get("scroll_overflow_sessions"), 0)
        if ovf > 0:
            scroll_overflow_sessions += ovf
            scroll_seqs.add(r.seq)

        gh = _safe_int(r.facts.get("ghost_repopup_count"), 0)
        if gh > 0:
            ghost_repopups += gh
            ghost_seqs.add(r.seq)

    triggered_parts: list[str] = []
    evidence_seqs: set[int] = set()
    samples = 0
    worst_rate = 0.0

    irritating_sources: dict[str, dict[str, Any]] = {}
    for src, total in auto_total_by_src.items():
        if total < 2:
            continue
        rap = auto_rapid_by_src[src]
        rate = rap / total
        if rate > 0.35:
            irritating_sources[src] = {"rapid": rap, "total": total, "rate": round(rate, 4)}
            triggered_parts.append(f"auto source '{src}' rapid_dismiss {rap}/{total} ({rate * 100:.1f}% > 35%)")
            evidence_seqs.update(auto_seqs_by_src[src])
            samples += rap
            worst_rate = max(worst_rate, rate)

    hotspots = {k: v for k, v in detached_by_res.items() if v >= 3}
    if hotspots:
        triggered_parts.append(f"detached-edge drag hotspots {hotspots}")
        for k in hotspots:
            evidence_seqs.update(detached_seqs[k])
            samples += hotspots[k]

    scroll_denom = max(ingame_overlay_sessions, scroll_overflow_sessions)
    scroll_rate = scroll_overflow_sessions / scroll_denom if scroll_denom > 0 else 0.0
    if scroll_overflow_sessions >= 1 and scroll_rate > 0.25:
        triggered_parts.append(
            f"in-game scroll overflow {scroll_overflow_sessions}/{scroll_denom} ({scroll_rate * 100:.1f}% > 25%)"
        )
        evidence_seqs.update(scroll_seqs)
        samples += scroll_overflow_sessions
        worst_rate = max(worst_rate, scroll_rate)

    if ghost_repopups >= 1:
        triggered_parts.append(f"{ghost_repopups} ghost re-popup(s) within 2s of user close")
        evidence_seqs.update(ghost_seqs)
        samples += ghost_repopups

    if not triggered_parts:
        return None

    severity = "P0" if (ghost_repopups >= 2 or worst_rate >= 0.60) else "P1"
    return DetectedIssue(
        issue_id="DET_OVERLAY_IRRITATION_AND_OVERFLOW",
        pillar="ux",
        severity=severity,
        detector_rule="DET_OVERLAY_IRRITATION_AND_OVERFLOW",
        sample_count=samples,
        metric_value=round(worst_rate, 4) if worst_rate > 0 else float(samples),
        summary="Overlay UX friction detected: " + "; ".join(triggered_parts) + ".",
        target_files_and_symbols=[
            "src/hex_helper/ui/edge_drawer.py:EdgeBuildDrawer",
            "src/hex_helper/ui/augment_combo_overlay.py:AugmentComboOverlay",
            "src/hex_helper/ui/champ_select_ui.py:ChampSelectOverlay",
            "src/hex_helper/devlab/ui_observer.py:OverlayVisibilityFilter",
            "src/hex_helper/app.py:HexHelperController._on_overlay_geometry_changed",
        ],
        evidence_match_seqs=sorted(evidence_seqs),
        evidence_roi_paths=[],
        proposed_experiment=(
            "Suppress ChampSelectOverlay re-show for current session after user close, compact stacked subcards "
            "above the 2-second scan fold to eliminate scroll overflow, and recalibrate edge defaults for hotspot DPIs."
        ),
        details={
            "irritating_auto_sources": irritating_sources,
            "detached_edge_hotspots": hotspots,
            "scroll_overflow_sessions": scroll_overflow_sessions,
            "ingame_overlay_sessions": ingame_overlay_sessions,
            "scroll_overflow_rate": round(scroll_rate, 4),
            "ghost_repopup_count": ghost_repopups,
        },
    )


def _detect_main_thread_stall_and_click_starvation(replays: list[MatchReplayResult]) -> DetectedIssue | None:
    max_tick_vision_p95 = 0.0
    tick_seqs: set[int] = set()

    high_stall_matches: dict[int, int] = {}
    max_click_gap_ms = 0.0
    click_gap_events = 0
    click_seqs: set[int] = set()

    ingame_frames_total = 0
    ingame_dual_pass_total = 0
    dual_pass_seqs: set[int] = set()

    for r in replays:
        tv_p95 = _safe_float(r.facts.get("tick_vision_p95_ms"), 0.0)
        if tv_p95 >= 100.0:
            tick_seqs.add(r.seq)
        if tv_p95 > max_tick_vision_p95:
            max_tick_vision_p95 = tv_p95

        stalls = _safe_int(r.facts.get("main_thread_ge_50ms_stalls"), 0)
        if stalls > 10:
            high_stall_matches[r.seq] = stalls

        gap = _safe_float(r.facts.get("click_timer_max_gap_ms"), 0.0)
        g_cnt = _safe_int(r.facts.get("click_starvation_events"), 0)
        if gap > max_click_gap_ms:
            max_click_gap_ms = gap
        if gap >= 90.0 or g_cnt > 0:
            click_gap_events += max(1, g_cnt)
            click_seqs.add(r.seq)

        ig_f = _safe_int(r.facts.get("ingame_vision_frames"), 0)
        ig_dp = _safe_int(r.facts.get("ingame_idle_dual_pass_frames"), 0)
        ingame_frames_total += ig_f
        ingame_dual_pass_total += ig_dp
        if ig_f > 0 and (ig_dp / ig_f) > 0.60:
            dual_pass_seqs.add(r.seq)

    dual_pass_rate = ingame_dual_pass_total / ingame_frames_total if ingame_frames_total > 0 else 0.0

    triggered_parts: list[str] = []
    evidence_seqs: set[int] = set()
    samples = 0

    if max_tick_vision_p95 >= 100.0:
        triggered_parts.append(f"_tick_vision P95={max_tick_vision_p95:.1f}ms (>=100ms)")
        evidence_seqs.update(tick_seqs)
        samples += len(tick_seqs)

    if high_stall_matches:
        max_stalls = max(high_stall_matches.values())
        triggered_parts.append(
            f"{len(high_stall_matches)} match(es) exceeded 10 >=50ms main-thread stalls (max={max_stalls})"
        )
        evidence_seqs.update(high_stall_matches.keys())
        samples += sum(high_stall_matches.values())

    if max_click_gap_ms >= 90.0:
        triggered_parts.append(
            f"AUGMENT_PICKING _click_timer starvation max_gap={max_click_gap_ms:.1f}ms (>=90ms, n={click_gap_events})"
        )
        evidence_seqs.update(click_seqs)
        samples += click_gap_events

    if ingame_frames_total >= 1 and dual_pass_rate > 0.60:
        triggered_parts.append(
            f"IN_GAME idle dual-pass OCR ratio={dual_pass_rate * 100:.1f}% "
            f"({ingame_dual_pass_total}/{ingame_frames_total} > 60%)"
        )
        evidence_seqs.update(dual_pass_seqs)
        samples += ingame_dual_pass_total

    if not triggered_parts:
        return None

    max_stalls_val = max(high_stall_matches.values(), default=0)
    severity = (
        "P0"
        if (max_tick_vision_p95 >= 150.0 or max_click_gap_ms >= 120.0 or max_stalls_val >= 25)
        else "P1"
    )
    headline_metric = max(max_tick_vision_p95, max_click_gap_ms, float(max_stalls_val), round(dual_pass_rate * 100.0, 2))
    return DetectedIssue(
        issue_id="DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION",
        pillar="runtime",
        severity=severity,
        detector_rule="DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION",
        sample_count=samples,
        metric_value=round(headline_metric, 2),
        summary="Main-thread latency / click-timer starvation: " + "; ".join(triggered_parts) + ".",
        target_files_and_symbols=[
            "src/hex_helper/app.py:HexHelperController._tick_vision",
            "src/hex_helper/app.py:HexHelperController._tick_click_sample",
            "src/hex_helper/vision/detector.py:AugmentPickerDetector._quick_gate",
            "src/hex_helper/vision/ocr_winrt.py:WinRtOcrEngine.recognize_lines",
        ],
        evidence_match_seqs=sorted(evidence_seqs),
        evidence_roi_paths=[],
        proposed_experiment=(
            "Move synchronous WinRT OCR off Qt main thread into worker pool, tighten _quick_gate to skip "
            "pass2 OCR during idle IN_GAME frames, and verify _click_timer stays under 45ms gap during AUGMENT_PICKING."
        ),
        details={
            "max_tick_vision_p95_ms": round(max_tick_vision_p95, 2),
            "high_stall_matches": high_stall_matches,
            "max_click_gap_ms": round(max_click_gap_ms, 2),
            "click_starvation_events": click_gap_events,
            "ingame_idle_dual_pass_frames": ingame_dual_pass_total,
            "ingame_vision_frames": ingame_frames_total,
            "ingame_idle_dual_pass_rate": round(dual_pass_rate, 4),
        },
    )


# ---------------------------------------------------------------------------
# DevLabAnalyzer Core Class
# ---------------------------------------------------------------------------


class DevLabAnalyzer:
    """Offline match replay, 5-pillar evolution report, version A/B diff, and health doctor."""

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_roi_bytes: int = DEFAULT_MAX_ROI_BYTES,
        max_matches: int = DEFAULT_MAX_MATCHES,
    ) -> None:
        self.root_dir = Path(root_dir) if root_dir is not None else get_devlab_dir()
        self.matches_dir = self.root_dir / "matches"
        self.reports_dir = self.root_dir / "reports"
        self.index_path = self.root_dir / "index.json"
        self.idle_events_path = self.root_dir / "session_idle_events.jsonl"
        self.idle_debug_path = self.root_dir / "session_idle_debug.log"
        self.max_total_bytes = max_total_bytes
        self.max_roi_bytes = max_roi_bytes
        self.max_matches = max_matches

    # ------------------------------------------------------------------
    # Match Discovery & Loading
    # ------------------------------------------------------------------

    def list_matches(self) -> list[dict[str, Any]]:
        """Return ordered match metadata from index.json + graceful on-disk directory discovery."""
        by_dir: dict[str, dict[str, Any]] = {}

        if self.index_path.is_file():
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(raw, dict):
                    for entry in raw.get("matches") or []:
                        if not isinstance(entry, dict):
                            continue
                        dname = str(entry.get("dir_name") or "").strip()
                        if not dname:
                            continue
                        mdir = self.matches_dir / dname
                        if not mdir.is_dir():
                            continue
                        item = dict(entry)
                        item["dir_name"] = dname
                        item["seq"] = _safe_int(item.get("seq"), _parse_seq_from_dir(dname))
                        item["match_dir"] = str(mdir)
                        by_dir[dname] = item
            except Exception:
                pass

        if self.matches_dir.is_dir():
            try:
                for child in sorted(self.matches_dir.iterdir()):
                    if not child.is_dir() or child.name in by_dir:
                        continue
                    meta_path = child / "meta.json"
                    events_path = child / "events.jsonl"
                    if not meta_path.is_file() and not events_path.is_file():
                        continue
                    meta: dict[str, Any] = {}
                    if meta_path.is_file():
                        try:
                            loaded = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                            if isinstance(loaded, dict):
                                meta = loaded
                        except Exception:
                            meta = {}
                    seq = _safe_int(meta.get("seq"), _parse_seq_from_dir(child.name))
                    roi_dir = child / "roi_snapshots"
                    roi_files = list(roi_dir.glob("*.png")) if roi_dir.is_dir() else []
                    by_dir[child.name] = {
                        "seq": seq,
                        "dir_name": child.name,
                        "match_dir": str(child),
                        "match_session_id": str(meta.get("match_session_id") or ""),
                        "match_key_hash": str(meta.get("match_key_hash") or ""),
                        "app_version": str(meta.get("app_version") or ""),
                        "started_at": str(meta.get("started_at") or ""),
                        "ended_at": str(meta.get("ended_at") or ""),
                        "hero_id": str(meta.get("hero_id") or ""),
                        "hero_name": str(meta.get("hero_name") or ""),
                        "dataset_tag": str(meta.get("dataset_tag") or ""),
                        "game_mode": str(meta.get("game_mode") or ""),
                        "is_non_mayhem": bool(meta.get("is_non_mayhem", False)),
                        "won": _safe_int(meta.get("won"), -1),
                        "duration_s": _safe_int(meta.get("duration_s"), 0),
                        "eog_source": str(meta.get("eog_source") or ""),
                        "event_count": _safe_int(meta.get("event_count"), 0),
                        "roi_count": _safe_int(meta.get("roi_count"), len(roi_files)),
                        "roi_bytes": _safe_int(meta.get("roi_bytes"), _dir_size_bytes(roi_dir)),
                        "total_bytes": _safe_int(meta.get("total_bytes"), _dir_size_bytes(child)),
                    }
            except OSError:
                pass

        result = list(by_dir.values())
        result.sort(key=lambda m: (_safe_int(m.get("seq"), 0), str(m.get("dir_name") or "")))
        return result

    def resolve_match_dir(self, target: str) -> Path | None:
        """Resolve 'latest', match seq ('42' / '#42' / '00042'), dir_name, or direct path."""
        raw = str(target or "").strip()
        if not raw:
            return None

        cand = Path(raw)
        if cand.is_dir() and ((cand / "meta.json").is_file() or (cand / "events.jsonl").is_file()):
            return cand

        matches = self.list_matches()
        if not matches:
            return None

        if raw.lower() == "latest":
            return Path(matches[-1]["match_dir"])

        # Direct dir_name match
        for m in matches:
            if m.get("dir_name") == raw:
                return Path(m["match_dir"])

        # Numeric seq match (with optional leading '#')
        num_str = raw.lstrip("#")
        if num_str.isdigit():
            want_seq = int(num_str)
            for m in reversed(matches):
                if _safe_int(m.get("seq"), -1) == want_seq:
                    return Path(m["match_dir"])

        return None

    def load_match_events(self, match_dir: Path | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Safely load meta.json and events.jsonl, tolerating partial/corrupt trailing lines."""
        mdir = Path(match_dir)
        meta: dict[str, Any] = {}
        meta_path = mdir / "meta.json"
        if meta_path.is_file():
            try:
                loaded = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    meta = loaded
            except Exception:
                meta = {}

        meta.setdefault("dir_name", mdir.name)
        if not meta.get("seq"):
            meta["seq"] = _parse_seq_from_dir(mdir.name)

        events: list[dict[str, Any]] = []
        corrupt_lines = 0
        ev_path = mdir / "events.jsonl"
        if ev_path.is_file():
            try:
                with ev_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw_line in fh:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            corrupt_lines += 1
                            continue
                        if isinstance(obj, dict):
                            events.append(obj)
                        else:
                            corrupt_lines += 1
            except OSError:
                pass

        # Preserve strict causal ordering by ev_seq (stable fallback on original line order)
        indexed = list(enumerate(events))
        indexed.sort(key=lambda pair: (_safe_int(pair[1].get("ev_seq"), pair[0] + 1), pair[0]))
        ordered = [ev for _, ev in indexed]
        meta["_corrupt_lines"] = corrupt_lines
        return meta, ordered

    # ------------------------------------------------------------------
    # Single-Match Replay
    # ------------------------------------------------------------------

    def replay_match(
        self,
        match_dir: Path | str,
        *,
        write_outputs: bool = True,
        out_dir: Path | None = None,
    ) -> MatchReplayResult:
        """Reconstruct chronological timeline, compute 5-pillar KPIs, and run single-match detectors."""
        mdir = Path(match_dir)
        meta, events = self.load_match_events(mdir)
        corrupt_lines = _safe_int(meta.pop("_corrupt_lines", 0), 0)

        seq = _safe_int(meta.get("seq"), _parse_seq_from_dir(mdir.name))
        dir_name = str(meta.get("dir_name") or mdir.name)

        roi_dir = mdir / "roi_snapshots"
        disk_rois: list[str] = []
        if roi_dir.is_dir():
            for p in sorted(roi_dir.glob("*.png")):
                disk_rois.append(f"roi_snapshots/{p.name}")
        full_roi_paths = [f"{dir_name}/{rel}" for rel in disk_rois]

        # Extract authoritative metadata with event fallbacks
        hero_id = str(meta.get("hero_id") or "")
        hero_name = str(meta.get("hero_name") or "")
        app_version = str(meta.get("app_version") or "")
        dataset_tag = str(meta.get("dataset_tag") or "")
        game_mode = str(meta.get("game_mode") or "")
        match_session_id = str(meta.get("match_session_id") or "")
        match_key_hash = str(meta.get("match_key_hash") or "")
        started_at = str(meta.get("started_at") or "")
        ended_at = str(meta.get("ended_at") or "")
        won = _safe_int(meta.get("won"), -1)
        duration_s = _safe_int(meta.get("duration_s"), 0)
        eog_source = str(meta.get("eog_source") or "")
        is_non_mayhem_flag = meta.get("is_non_mayhem")

        for ev in events:
            if not hero_id and ev.get("hero_id"):
                hero_id = str(ev["hero_id"])
            if not app_version and ev.get("app_version"):
                app_version = str(ev["app_version"])
            if not dataset_tag and ev.get("dataset_tag"):
                dataset_tag = str(ev["dataset_tag"])
            if not match_session_id and ev.get("match_session_id"):
                match_session_id = str(ev["match_session_id"])
            if not match_key_hash and ev.get("match_key_hash"):
                match_key_hash = str(ev["match_key_hash"])
            if not started_at and ev.get("ts_utc"):
                started_at = str(ev["ts_utc"])
            if ev.get("event_type") == EV_MATCH_EOG:
                p = ev.get("payload") or {}
                if won not in (0, 1) and _safe_int(p.get("won"), -1) in (0, 1):
                    won = _safe_int(p.get("won"), -1)
                if duration_s <= 0 and _safe_int(p.get("duration_s"), 0) > 0:
                    duration_s = _safe_int(p.get("duration_s"), 0)
                if not eog_source and p.get("eog_source"):
                    eog_source = str(p["eog_source"])
                if not game_mode and p.get("game_mode"):
                    game_mode = str(p["game_mode"])
                if is_non_mayhem_flag is None and "is_non_mayhem" in p:
                    is_non_mayhem_flag = bool(p["is_non_mayhem"])

        is_non_mayhem = bool(is_non_mayhem_flag) if is_non_mayhem_flag is not None else False
        if not is_non_mayhem and game_mode.strip().upper() in _NON_MAYHEM_MODES:
            is_non_mayhem = True

        # Parse all events into per-match KPIs, detector facts, and timeline entries
        kpis, facts, timeline_lines, sections = self._analyze_single_match_events(
            seq=seq,
            dir_name=dir_name,
            meta=meta,
            events=events,
            disk_rois=disk_rois,
            is_non_mayhem=is_non_mayhem,
            won=won,
            duration_s=duration_s,
            eog_source=eog_source,
        )

        result = MatchReplayResult(
            seq=seq,
            dir_name=dir_name,
            match_dir=str(mdir),
            match_session_id=match_session_id,
            match_key_hash=match_key_hash,
            app_version=app_version,
            started_at=started_at,
            ended_at=ended_at,
            hero_id=hero_id,
            hero_name=hero_name,
            dataset_tag=dataset_tag,
            game_mode=game_mode,
            is_non_mayhem=is_non_mayhem,
            won=won,
            duration_s=duration_s,
            eog_source=eog_source,
            event_count=len(events),
            corrupt_lines=corrupt_lines,
            roi_paths=full_roi_paths,
            kpis=kpis,
            issues=[],
            timeline_markdown="",
            facts=facts,
        )

        # Single-match issue detection
        result.issues = run_issue_detectors([result])
        result.timeline_markdown = self._render_match_timeline_md(result, sections, timeline_lines)

        if write_outputs:
            target_reports = Path(out_dir) if out_dir is not None else self.reports_dir
            target_reports.mkdir(parents=True, exist_ok=True)
            tl_path = target_reports / f"match_{seq:05d}_timeline.md"
            _atomic_write_text(tl_path, result.timeline_markdown)
            result.timeline_path = str(tl_path)

            sum_path = mdir / "replay_summary.json"
            try:
                _atomic_write_json(sum_path, result.to_summary_dict())
                result.summary_path = str(sum_path)
            except OSError:
                pass

        return result

    def _analyze_single_match_events(
        self,
        *,
        seq: int,
        dir_name: str,
        meta: dict[str, Any],
        events: list[dict[str, Any]],
        disk_rois: list[str],
        is_non_mayhem: bool,
        won: int,
        duration_s: int,
        eog_source: str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str], dict[str, list[str]]]:
        timeline_lines: list[str] = []
        sections: dict[str, list[str]] = {
            "champ_select": [],
            "lobby_and_runes": [],
            "eog": [],
        }

        max_game_time_s = float(duration_s or 0)
        resolutions_seen: Counter[str] = Counter()

        # Pillar 1: Recommendation & Scoring counters
        cs_recs = 0
        cs_actions = 0
        cs_followed_best_swap: bool | None = None
        cs_dodged = False

        aug_offers = 0
        aug_picks = 0
        aug_unresolved_picks = 0
        aug_top1_adopted = 0
        aug_hex_regrets: list[float] = []
        aug_wr_regrets: list[float] = []
        aug_rerolls = 0
        aug_top1_rerolled = 0
        aug_reroll_gains: list[float] = []
        aug_top1_offers_map: Counter[str] = Counter()
        aug_top1_adopted_map: Counter[str] = Counter()
        aug_top1_rerolled_map: Counter[str] = Counter()
        last_offer_top1_id = ""
        offer_seen_in_round = False

        item_purchases = 0
        augment_granted_items = 0
        item_top1_adopted = 0
        item_top4_adopted = 0
        item_top1_offers_map: Counter[str] = Counter()
        item_top1_adopted_map: Counter[str] = Counter()
        plan_override_count = 0
        player_component_override_count = 0
        coach_overhaul_count = 0
        local_rec_snapshots = 0
        profile_conflicts: list[dict[str, Any]] = []
        seen_profile_conflict_keys: set[tuple[str, str]] = set()

        # Pillar 2: Vision / OCR / Click / Icon / Transmute counters
        picker_opens = 0
        picker_open_times: list[float] = []
        partial_open_count = 0
        recovered_slots = 0
        recovery_failed_slots = 0

        unlock_milestones: list[dict[str, Any]] = []
        my_death_times: list[float] = []
        explicit_silent_misses: list[dict[str, Any]] = []
        silent_miss_levels_seen: set[int] = set()
        max_owned_augs_by_time: list[tuple[float, int]] = []
        running_owned_augs = 0

        ocr_near_misses: list[dict[str, Any]] = []
        ocr_shadowed_pass2: list[dict[str, Any]] = []
        ocr_short_substring_hits: list[dict[str, Any]] = []

        click_resolutions = 0
        ace_blocked_count = 0
        reroll_desync_events: list[dict[str, Any]] = []
        y_threshold_clip_events: list[dict[str, Any]] = []
        last_click_resolve: dict[str, Any] | None = None
        last_reroll_mono_ms: int | None = None

        icon_verify_count = 0
        icon_empty_bank_count = 0
        icon_flat_patch_count = 0
        icon_miss_streak = 0
        icon_consecutive_miss_streaks = 0
        icon_resolution = ""
        icon_anomaly_roi_paths: list[str] = []

        passive_hover_hits = 0
        tab_zone_hits = 0
        tab_zone_aug_ids: set[str] = set()
        tab_zone_removed_count = 0
        transmute_roi_paths: list[str] = []
        user_correction_count = 0

        # Pillar 3: UX & Ergonomics counters
        overlay_sessions = 0
        ingame_overlay_sessions = 0
        dwell_values: list[int] = []
        auto_sessions_by_source: Counter[str] = Counter()
        auto_rapid_by_source: Counter[str] = Counter()
        ghost_repopup_count = 0
        layout_adjustments = 0
        detached_edge_count = 0
        detached_edge_by_res: Counter[str] = Counter()
        scroll_overflow_sessions = 0
        f4_emergency_hide_count = 0
        f2_rescan_count = 0
        lookup_zero_hits: Counter[str] = Counter()
        recap_shown_count = 0
        recap_shared_count = 0

        # Pillar 4: AI Coach counters
        coach_turns_total = 0
        coach_latencies: list[float] = []
        coach_total_tokens = 0
        coach_stale_dropped = 0
        picker_ai_total = 0
        picker_ai_late = 0
        seen_picker_ai_req_ids: set[str] = set()
        death_ai_buy_windows = 0
        death_ai_late_windows = 0
        death_warmup_hit_count = 0
        disagreement_count = 0
        sided_with_coach_count = 0
        grounding_blocked_turns = 0
        grounding_breakdown: Counter[str] = Counter()
        smart_gate_skips = 0
        last_gate_skip_mono_ms: int | None = None
        last_gate_skip_game_s: float | None = None
        last_gate_skip_counted_f6 = False
        f6_within_10s_after_gate_skip = 0

        # Pillar 5: Runtime stability & latency counters
        tick_vision_p95_buckets: list[tuple[int, float]] = []
        tick_vision_p95_ms = 0.0
        main_thread_ge_50ms_stalls = 0
        vision_worker_p95_buckets: list[tuple[int, float]] = []
        vision_worker_p95_ms = 0.0
        vision_worker_ge_50ms_count = 0
        click_timer_max_gap_ms = 0.0
        click_starvation_events = 0
        ingame_vision_frames = 0
        ingame_idle_dual_pass_frames = 0
        window_stats_frames = 0
        window_stats_idle_dual_pass_frames = 0
        has_window_stats = False
        live_disconnect_recoveries = 0
        writer_dropped_events = _safe_int(meta.get("dropped_events"), 0)
        writer_dropped_roi = _safe_int(meta.get("dropped_roi"), 0)
        writer_roi_capped_count = _safe_int(meta.get("roi_capped_count"), 0)

        for ev in events:
            ev_seq = _safe_int(ev.get("ev_seq"), 0)
            ev_type = str(ev.get("event_type") or "")
            game_state = str(ev.get("game_state") or "")
            gt = _opt_float(ev.get("game_time_s"))
            mono_ms = _safe_int(ev.get("ts_mono_ms"), 0)
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}

            if gt is not None and gt > max_game_time_s:
                max_game_time_s = gt

            res_k = _extract_resolution_key(payload)
            if res_k:
                resolutions_seen[res_k] += 1

            roi_refs = _extract_roi_refs(payload, dir_name, ev_seq, disk_rois)
            ts_lbl = _fmt_mmss(gt)

            # ---- Gameflow & Pre-game ----
            if ev_type == EV_GAMEFLOW_TRANSITION:
                frm = _first(payload, "from_phase", "from_state", "prev_phase", default="?")
                to = _first(payload, "to_phase", "to_state", "next_phase", default="?")
                reason = str(payload.get("reason") or "")
                if payload.get("reconnected") or payload.get("live_recovered") or "recover" in reason.lower():
                    live_disconnect_recoveries += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Phase** `{frm}` -> `{to}`"
                    + (f" ({reason})" if reason else "")
                )

            elif ev_type == EV_CHAMP_SELECT_REC:
                cs_recs += 1
                my_h = payload.get("my_hero_id") or ev.get("hero_id") or "?"
                best_sw = payload.get("best_swap_hero_id") or "-"
                top_h = payload.get("top_recommended_hero") or "-"
                mode = payload.get("goal_mode") or "-"
                msg = f"Pool rec: my={my_h}, top={top_h}, best_swap={best_sw}, goal={mode}, bench={len(payload.get('bench_hero_ids') or [])}"
                sections["champ_select"].append(msg)
                timeline_lines.append(f"- `[{ts_lbl}]` #{ev_seq} **CS Rec** {msg}")

            elif ev_type == EV_CHAMP_SELECT_ACTION:
                cs_actions += 1
                atype = payload.get("action_type") or "?"
                if atype == "spell_change":
                    msg = f"Spell change: {payload.get('from_spells')} -> {payload.get('to_spells')}"
                else:
                    msg = (
                        f"{atype}: {payload.get('from_hero')} -> {payload.get('to_hero')} "
                        f"(best_swap={payload.get('was_best_swap')}, rerolls={payload.get('rerolls_remaining')})"
                    )
                sections["champ_select"].append(msg)
                timeline_lines.append(f"- `[{ts_lbl}]` #{ev_seq} **CS Action** {msg}")

            elif ev_type == EV_CHAMP_SELECT_OUTCOME:
                outcome = str(payload.get("outcome") or "")
                if outcome == "dodged":
                    cs_dodged = True
                if "followed_best_swap" in payload:
                    cs_followed_best_swap = bool(payload.get("followed_best_swap"))
                msg = (
                    f"Outcome={outcome}, final={payload.get('final_hero_id')}, "
                    f"followed_best_swap={payload.get('followed_best_swap')}, actions={payload.get('action_count')}"
                )
                sections["champ_select"].append(msg)
                timeline_lines.append(f"- `[{ts_lbl}]` #{ev_seq} **CS Outcome** {msg}")

            elif ev_type == EV_LOBBY_SCOUT:
                gap = _first(payload, "rating_gap", "lobby_scout_rating_gap", default=None)
                msg = f"Lobby scout: rating_gap={gap}, allies={payload.get('ally_avg_rating')}, enemies={payload.get('enemy_avg_rating')}"
                sections["lobby_and_runes"].append(msg)
                timeline_lines.append(f"- `[{ts_lbl}]` #{ev_seq} **Lobby Scout** {msg}")

            elif ev_type == EV_RUNES_LOCKED:
                ks = _first(payload, "keystone_id", "primary_style_id", default="-")
                sub = _first(payload, "sub_style_id", "secondary_tree_id", default="-")
                msg = f"Runes locked: primary={ks}, sub={sub}, perks={payload.get('selected_perk_ids')}"
                sections["lobby_and_runes"].append(msg)
                timeline_lines.append(f"- `[{ts_lbl}]` #{ev_seq} **Runes** {msg}")

            # ---- Augments & Recommendations ----
            elif ev_type == EV_AUGMENT_OFFER_REC:
                cards = payload.get("cards") or payload.get("offers") or []
                card_anvil_hits = 0
                if isinstance(cards, list):
                    for c_item in cards:
                        cid = (
                            str(c_item.get("augment_id") or c_item.get("id") or "")
                            if isinstance(c_item, dict)
                            else str(c_item or "")
                        ).strip()
                        if cid in _STAT_ANVIL_IDS:
                            card_anvil_hits += 1
                top1_id = str(_first(payload, "top1_augment_id", "recommended_augment_id", "best_augment_id", default="") or "").strip()
                is_anvil = (
                    bool(payload.get("is_stat_anvil"))
                    or top1_id in _STAT_ANVIL_IDS
                    or card_anvil_hits >= 2
                )
                if not is_anvil:
                    aug_offers += 1
                    offer_seen_in_round = True
                    if top1_id:
                        aug_top1_offers_map[top1_id] += 1
                        last_offer_top1_id = top1_id
                else:
                    offer_seen_in_round = False
                    last_offer_top1_id = ""
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Aug Offer** top1=`{top1_id or '-'}` cards={len(cards)}"
                    + (" [combo_flip]" if payload.get("combo_flipped") else "")
                )

            elif ev_type == EV_AUGMENT_REROLL_DELTA:
                aug_rerolls += 1
                last_reroll_mono_ms = mono_ms
                old_id = str(_first(payload, "old_augment_id", "from_augment_id", "rerolled_augment_id", default="") or "")
                new_id = str(_first(payload, "new_augment_id", "to_augment_id", default="") or "")
                was_top1 = bool(_first(payload, "was_top1", "rerolled_top1", default=False))
                if not was_top1 and old_id and old_id == last_offer_top1_id:
                    was_top1 = True
                if was_top1:
                    aug_top1_rerolled += 1
                    target_id = old_id or last_offer_top1_id
                    if target_id:
                        aug_top1_rerolled_map[target_id] += 1
                gain = _opt_float(_first(payload, "hex_gain", "reroll_hex_gain", "score_delta"))
                if gain is None:
                    old_s = _opt_float(_first(payload, "old_hex_score", "old_score"))
                    new_s = _opt_float(_first(payload, "new_hex_score", "new_score"))
                    if old_s is not None and new_s is not None:
                        gain = new_s - old_s
                if gain is not None:
                    aug_reroll_gains.append(gain)
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Aug Reroll** slot={payload.get('slot')} `{old_id}` -> `{new_id}`"
                    + (f" gain={gain:+.2f}" if gain is not None else "")
                )

            elif ev_type == EV_AUGMENT_PICK_DELTA:
                picked_id = str(_first(payload, "picked_augment_id", "augment_id", "chosen_augment_id", default="") or "").strip()
                top1_id = str(_first(payload, "recommended_augment_id", "top1_augment_id", default=last_offer_top1_id) or "").strip()
                sided_with_raw = str(payload.get("sided_with") or "").strip().lower()
                src_raw = str(_first(payload, "source", "pick_source", default="") or "").strip().lower()
                is_anvil = (
                    bool(payload.get("is_stat_anvil"))
                    or picked_id in _STAT_ANVIL_IDS
                    or (not picked_id and top1_id in _STAT_ANVIL_IDS)
                )
                is_unresolved = (
                    (not picked_id)
                    or sided_with_raw == "unresolved"
                    or src_raw in ("none", "unresolved")
                )
                if is_anvil:
                    offer_seen_in_round = False
                elif is_unresolved:
                    aug_unresolved_picks += 1
                    if offer_seen_in_round and last_offer_top1_id and aug_top1_offers_map.get(last_offer_top1_id, 0) > 0:
                        aug_top1_offers_map[last_offer_top1_id] -= 1
                        if aug_top1_offers_map[last_offer_top1_id] <= 0:
                            del aug_top1_offers_map[last_offer_top1_id]
                    offer_seen_in_round = False
                else:
                    aug_picks += 1
                    running_owned_augs += 1
                    if gt is not None:
                        max_owned_augs_by_time.append((gt, running_owned_augs))
                    matched_top1 = payload.get("matched_top1")
                    if matched_top1 is None:
                        matched_top1 = payload.get("followed_top1")
                    if matched_top1 is None and picked_id and top1_id:
                        matched_top1 = picked_id == top1_id
                    if bool(matched_top1):
                        aug_top1_adopted += 1
                        if top1_id:
                            aug_top1_adopted_map[top1_id] += 1
                    # If offer event was omitted in this round, still count top1 exposure from pick delta
                    if top1_id and not offer_seen_in_round:
                        aug_top1_offers_map[top1_id] += 1
                    offer_seen_in_round = False
                    hr = _opt_float(_first(payload, "hex_score_regret", "score_regret"))
                    if hr is not None:
                        aug_hex_regrets.append(hr)
                    wr = _opt_float(_first(payload, "wr_delta_regret", "wr_regret"))
                    if wr is not None:
                        aug_wr_regrets.append(wr)

                ai_before = _first(payload, "ai_advice_arrived_before_pick", "coach_arrived_before_pick")
                pick_req_id = str(_first(payload, "coach_request_id", "request_id", default="") or "").strip()
                if ai_before is not None and pick_req_id and pick_req_id not in seen_picker_ai_req_ids:
                    seen_picker_ai_req_ids.add(pick_req_id)
                    picker_ai_total += 1
                    if not bool(ai_before):
                        picker_ai_late += 1

                if not is_anvil and not is_unresolved and bool(
                    _first(payload, "local_vs_coach_disagreed", "ai_disagreed", default=False)
                ):
                    disagreement_count += 1
                    if sided_with_raw == "coach":
                        sided_with_coach_count += 1

                src = _first(payload, "source", "pick_source", default="?")
                conf = _opt_float(payload.get("confidence"))
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Aug Pick** `{picked_id}` (top1=`{top1_id}`, "
                    f"src={src}, conf={conf if conf is not None else '-'})"
                    + (" [UNRESOLVED]" if (is_unresolved and not is_anvil) else "")
                )

            elif ev_type == EV_LOCAL_BUILD_REC:
                local_rec_snapshots += 1
                hp = str(_first(payload, "hero_profile", "matchup_hero_profile", default="") or "")
                chp = str(payload.get("combo_hero_profile") or "")
                conflict = bool(payload.get("profile_conflict")) or bool(hp and chp and hp != chp)
                if conflict:
                    ckey = (hp, chp)
                    if ckey not in seen_profile_conflict_keys:
                        seen_profile_conflict_keys.add(ckey)
                        profile_conflicts.append({"matchup_hero_profile": hp, "combo_hero_profile": chp})
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Build Rec** top={payload.get('top_item_ids')} "
                    f"profile={hp or '-'}/{chp or '-'}"
                    + (" [CONFLICT]" if conflict else "")
                )

            elif ev_type == EV_ITEM_TX:
                action = str(payload.get("action") or "?")
                iid = payload.get("item_id")
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Item TX** `{action}` item={iid} "
                    f"price={payload.get('price', 0)} gold_delta={payload.get('gold_delta', 0)}"
                )

            elif ev_type == EV_ITEM_PURCHASE_DELTA:
                ev_kind = str(payload.get("event_kind") or "")
                is_override = (
                    ev_kind == "plan_override"
                    or bool(payload.get("override_triggered"))
                    or bool(payload.get("overridden"))
                )
                if is_override:
                    plan_override_count += 1
                    ov_reason = str(payload.get("override_reason") or "").strip().lower()
                    ov_src = str(payload.get("source") or "").strip().lower()
                    if ov_reason == "allow_overhaul" or (
                        ov_src == "coach" and "component" not in ov_reason and "800" not in ov_reason
                    ):
                        coach_overhaul_count += 1
                    else:
                        player_component_override_count += 1
                    timeline_lines.append(
                        f"- `[{ts_lbl}]` #{ev_seq} **Plan Override** reason={payload.get('override_reason')} "
                        f"gold={_first(payload, 'committed_gold', 'invested_gold', default=0)} "
                        f"`{payload.get('previous_route', '')}` -> `{payload.get('new_route', '')}`"
                    )
                else:
                    is_aug_granted = bool(payload.get("augment_granted")) or _is_augment_granted_special_item_id(
                        payload.get("item_id")
                    )
                    if is_aug_granted:
                        augment_granted_items += 1
                        timeline_lines.append(
                            f"- `[{ts_lbl}]` #{ev_seq} **Augment Item Grant** item={payload.get('item_id')} "
                            f"[excluded_from_shop_adoption]"
                        )
                    else:
                        item_purchases += 1
                        rank = _safe_int(payload.get("matched_local_rank"), 0)
                        matched_top1 = bool(payload.get("matched_local_top1")) or rank == 1
                        if matched_top1:
                            item_top1_adopted += 1
                        if 1 <= rank <= 4:
                            item_top4_adopted += 1
                        top_items = payload.get("local_top_items") or []
                        if isinstance(top_items, list) and top_items:
                            t1 = str(top_items[0])
                            item_top1_offers_map[t1] += 1
                            if matched_top1:
                                item_top1_adopted_map[t1] += 1
                        if bool(payload.get("local_vs_coach_disagreed")):
                            disagreement_count += 1
                            if str(payload.get("sided_with") or "").lower() == "coach":
                                sided_with_coach_count += 1
                        timeline_lines.append(
                            f"- `[{ts_lbl}]` #{ev_seq} **Purchase Delta** item={payload.get('item_id')} "
                            f"local_rank={rank} coach={payload.get('matched_coach')} sided={payload.get('sided_with')}"
                        )

            elif ev_type == EV_ENEMY_ITEM_MILESTONE:
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Enemy Item** {payload.get('enemy_hero')} "
                    f"item={payload.get('item_id')} tags={payload.get('tags')}"
                )

            elif ev_type == EV_LIVE_GAME_EVENT:
                if str(payload.get("event_name") or "") == "ChampionKill" and bool(payload.get("victim_is_me")):
                    ev_t = _opt_float(payload.get("event_time"))
                    if ev_t is None:
                        ev_t = gt
                    if ev_t is not None:
                        my_death_times.append(ev_t)
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Game Event** {payload.get('event_name')} "
                    f"killer={payload.get('killer_hero') or '-'} victim={payload.get('victim_hero') or '-'}"
                )

            elif ev_type == EV_LEVEL_MILESTONE:
                mlvl = _safe_int(_first(payload, "milestone_level", "level"), 0)
                exp_slots = _safe_int(payload.get("expected_augment_slots"), _UNLOCK_LEVEL_TO_SLOTS.get(mlvl, 0))
                sel_cnt = _safe_int(payload.get("selected_augment_count"), running_owned_augs)
                if sel_cnt > running_owned_augs:
                    running_owned_augs = sel_cnt
                if gt is not None:
                    max_owned_augs_by_time.append((gt, running_owned_augs))
                if mlvl in _UNLOCK_LEVEL_TO_SLOTS:
                    unlock_milestones.append(
                        {
                            "milestone_level": mlvl,
                            "expected_augment_slots": exp_slots,
                            "selected_augment_count": sel_cnt,
                            "game_time_s": gt if gt is not None else 0.0,
                            "is_degraded_2999": bool(payload.get("is_degraded_2999", False)),
                        }
                    )
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Level Milestone** Lv{mlvl} "
                    f"(augs={sel_cnt}/{exp_slots}, maxed={payload.get('maxed_ability') or '-'})"
                )

            elif ev_type == EV_AUGMENT_UNLOCK_SILENT_MISS:
                mlvl = _safe_int(payload.get("milestone_level"), 0)
                if mlvl not in silent_miss_levels_seen:
                    silent_miss_levels_seen.add(mlvl)
                    explicit_silent_misses.append(
                        {
                            "milestone_level": mlvl,
                            "expected_augment_slots": _safe_int(payload.get("expected_augment_slots"), 0),
                            "selected_augment_count": _safe_int(payload.get("selected_augment_count"), 0),
                            "elapsed_since_unlock_s": _safe_float(payload.get("elapsed_since_unlock_s"), 45.0),
                            "source": "explicit",
                        }
                    )
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **ALERT Silent Unlock Miss** Lv{mlvl} "
                    f"augs={payload.get('selected_augment_count')}/{payload.get('expected_augment_slots')} "
                    f"elapsed={payload.get('elapsed_since_unlock_s')}s"
                )

            # ---- Vision / OCR / Click / Icon / Transmute ----
            elif ev_type == EV_VISION_CAPTURE_STATE:
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Vision Capture** backend={payload.get('backend')} "
                    f"black_frame={payload.get('black_frame')} res={res_k or '-'}"
                )

            elif ev_type == EV_VISION_PICKER_TRANSITION:
                tr = str(_first(payload, "transition", "state", "action", default="") or "").strip().lower()
                is_reroll = tr in ("reroll", "rerolled") or "reroll" in tr
                is_open = (not is_reroll) and (
                    tr in ("open", "opened", "enter", "shown")
                    or (not tr and bool(payload.get("is_open")))
                )
                if is_open:
                    picker_opens += 1
                    if gt is not None:
                        picker_open_times.append(gt)
                if payload.get("partial_open"):
                    partial_open_count += 1
                rec_slots = payload.get("recovered_slots")
                if isinstance(rec_slots, list):
                    recovered_slots += len(rec_slots)
                else:
                    recovered_slots += _safe_int(rec_slots, 0)
                fail_slots = payload.get("recovery_failed_slots")
                if isinstance(fail_slots, list):
                    recovery_failed_slots += len(fail_slots)
                else:
                    recovery_failed_slots += _safe_int(fail_slots, 0)

                # Also inspect embedded diagnostics if present
                self._collect_ocr_anomalies(
                    payload, roi_refs, ocr_near_misses, ocr_shadowed_pass2, ocr_short_substring_hits
                )
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Picker** `{tr or ('open' if is_open else 'close')}` "
                    f"layout={payload.get('chosen_layout', '-')} cards={payload.get('card_count', '-')}"
                    + (" [partial]" if payload.get("partial_open") else "")
                )

            elif ev_type == EV_VISION_OCR_ANOMALY:
                self._collect_ocr_anomalies(
                    payload, roi_refs, ocr_near_misses, ocr_shadowed_pass2, ocr_short_substring_hits
                )
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **OCR Anomaly** lang={payload.get('ocr_language_tag', '-')} "
                    f"near_misses={len(payload.get('near_misses') or [])} "
                    f"shadowed={len(payload.get('shadowed_pass2') or [])}"
                    + (f" roi={roi_refs[0]}" if roi_refs else "")
                )

            elif ev_type == EV_VISION_CLICK_RESOLVED:
                click_resolutions += 1
                if payload.get("ace_blocked_suspected"):
                    ace_blocked_count += 1
                ms_reroll = _opt_float(payload.get("ms_since_last_reroll"))
                if ms_reroll is None and last_reroll_mono_ms is not None and mono_ms >= last_reroll_mono_ms:
                    ms_reroll = float(mono_ms - last_reroll_mono_ms)
                cursor_y = _opt_float(payload.get("cursor_rel_y"))
                in_reroll_zone = bool(payload.get("in_reroll_zone"))
                chosen_slot = payload.get("chosen_slot")
                if cursor_y is not None and 0.76 <= cursor_y <= 0.83 and (in_reroll_zone or chosen_slot is None):
                    y_threshold_clip_events.append(
                        {
                            "ev_seq": ev_seq,
                            "cursor_rel_y": cursor_y,
                            "reroll_y_threshold_rel": payload.get("reroll_y_threshold_rel"),
                            "in_reroll_zone": in_reroll_zone,
                            "roi_paths": roi_refs,
                        }
                    )
                last_click_resolve = {
                    "ev_seq": ev_seq,
                    "mono_ms": mono_ms,
                    "ms_since_last_reroll": ms_reroll,
                    "chosen_slot": chosen_slot,
                    "chosen_source": payload.get("chosen_source"),
                    "roi_paths": roi_refs,
                }
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Click Resolved** slot={chosen_slot} "
                    f"src={payload.get('chosen_source')} conf={payload.get('confidence')} "
                    f"y={cursor_y if cursor_y is not None else '-'} in_reroll={in_reroll_zone}"
                )

            elif ev_type == EV_VISION_ICON_VERIFY:
                icon_verify_count += 1
                if res_k and not icon_resolution:
                    icon_resolution = res_k
                tpl_cnt = _safe_int(payload.get("template_count"), -1)
                outcome = str(payload.get("outcome") or "").lower()
                is_empty = tpl_cnt == 0 or outcome == "skipped_empty_bank"

                raw_stds = payload.get("slot_patch_stds")
                std_vals: list[float] = []
                if isinstance(raw_stds, list):
                    std_vals = [_safe_float(x, 99.0) for x in raw_stds if x is not None]
                elif isinstance(raw_stds, dict):
                    std_vals = [_safe_float(x, 99.0) for x in raw_stds.values() if x is not None]
                is_flat = outcome == "flat_patches" or (bool(std_vals) and all(v < 8.0 for v in std_vals))

                if is_empty:
                    icon_empty_bank_count += 1
                    icon_anomaly_roi_paths.extend(roi_refs)
                if is_flat:
                    icon_flat_patch_count += 1
                    icon_anomaly_roi_paths.extend(roi_refs)

                if outcome == "below_threshold":
                    icon_miss_streak += 1
                    if icon_miss_streak == 3:
                        icon_consecutive_miss_streaks += 1
                        icon_anomaly_roi_paths.extend(roi_refs)
                elif outcome == "matched":
                    icon_miss_streak = 0

                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Icon Verify** outcome={outcome or '-'} "
                    f"tpls={tpl_cnt} best={payload.get('best_id') or '-'} score={payload.get('best_score', '-')}"
                )

            elif ev_type == EV_VISION_TRANSMUTE_TRACE:
                channel = str(payload.get("channel") or "").lower()
                czone = str(payload.get("cursor_zone") or "other").lower()
                matched_ids: list[str] = []
                raw_mids = payload.get("matched_ids")
                if isinstance(raw_mids, list):
                    matched_ids = [str(x) for x in raw_mids if x]
                else:
                    single_id = _first(payload, "resolved_augment_id", "augment_id", default="")
                    if single_id:
                        matched_ids = [str(single_id)]

                is_passive = ("passive" in channel or "hover" in channel or czone == "tab_zone")
                if is_passive and matched_ids:
                    passive_hover_hits += len(matched_ids)
                    if czone == "tab_zone":
                        tab_zone_hits += len(matched_ids)
                        tab_zone_aug_ids.update(matched_ids)
                        transmute_roi_paths.extend(roi_refs)
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Transmute** ch={channel or '-'} zone={czone} matched={matched_ids}"
                )

            elif ev_type == EV_VISION_USER_CORRECTION:
                user_correction_count += 1
                action = str(_first(payload, "action", "correction_type", default="") or "").lower()
                old_id = str(_first(payload, "old_augment_id", "from_augment_id", "removed_augment_id", "augment_id", default="") or "")
                new_id = str(_first(payload, "new_augment_id", "to_augment_id", "added_augment_id", default="") or "")

                # Check Reroll Desync (<850ms followed by manual replace)
                if any(tok in action for tok in ("replace", "swap", "override")):
                    ms_reroll = _opt_float(payload.get("ms_since_last_reroll"))
                    linked_rois = list(roi_refs)
                    if ms_reroll is None and last_click_resolve is not None:
                        if abs(mono_ms - _safe_int(last_click_resolve.get("mono_ms"), 0)) <= 30_000:
                            ms_reroll = _opt_float(last_click_resolve.get("ms_since_last_reroll"))
                            linked_rois.extend(last_click_resolve.get("roi_paths") or [])
                    if ms_reroll is not None and 0.0 <= ms_reroll < 850.0:
                        reroll_desync_events.append(
                            {
                                "ev_seq": ev_seq,
                                "ms_since_last_reroll": round(ms_reroll, 1),
                                "old_augment_id": old_id,
                                "new_augment_id": new_id,
                                "roi_paths": linked_rois,
                            }
                        )

                # Check Tab Passive Hover Pollution (removed after tab_zone passive hover)
                if any(tok in action for tok in ("remove", "delete")):
                    prev_src = str(_first(payload, "prev_source", "original_source", "source", default="") or "").lower()
                    czone = str(payload.get("cursor_zone") or "").lower()
                    was_tab = (
                        bool(payload.get("was_tab_zone"))
                        or czone == "tab_zone"
                        or "tab" in prev_src
                        or (old_id and old_id in tab_zone_aug_ids)
                    )
                    if was_tab:
                        tab_zone_removed_count += 1
                        if old_id in tab_zone_aug_ids:
                            tab_zone_aug_ids.discard(old_id)
                        transmute_roi_paths.extend(roi_refs)

                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **User Correction** `{action}` `{old_id}` -> `{new_id}`"
                )

            elif ev_type == EV_VISION_WINDOW_STATS:
                tot_f = max(0, _safe_int(payload.get("total_frames"), 0))
                idle_f = max(0, _safe_int(payload.get("idle_dual_pass_frames"), 0))
                if tot_f > 0 or "total_frames" in payload:
                    has_window_stats = True
                    window_stats_frames += tot_f
                    window_stats_idle_dual_pass_frames += idle_f
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Vision Window** total={tot_f} idle_dual={idle_f} "
                    f"rate={payload.get('idle_dual_pass_rate', '-')}"
                )

            elif ev_type == EV_VISION_LATENCY_SPIKE:
                gap = _opt_float(_first(payload, "click_timer_max_gap_ms", "max_gap_ms"))
                if gap is not None:
                    if gap > click_timer_max_gap_ms:
                        click_timer_max_gap_ms = gap
                    if gap >= 90.0 and (game_state == "AUGMENT_PICKING" or payload.get("in_augment_picking", True)):
                        click_starvation_events += 1
                is_ingame_idle = (
                    game_state == "IN_GAME"
                    and not bool(payload.get("picker_open"))
                ) or bool(payload.get("in_game_idle_frame"))
                if is_ingame_idle:
                    ingame_vision_frames += 1
                    p2_ms = _safe_float(payload.get("ocr_p2_ms"), 0.0)
                    if p2_ms > 0.0 or bool(payload.get("in_game_idle_dual_pass")) or bool(payload.get("dual_pass_ocr_ran")):
                        ingame_idle_dual_pass_frames += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Vision Spike** total={_first(payload, 'total_ms', 'tick_ms', default='-')}ms "
                    f"p1={payload.get('ocr_p1_ms', '-')}ms p2={payload.get('ocr_p2_ms', '-')}ms click_gap={gap or '-'}ms"
                )

            # ---- Overlay UX & Ergonomics ----
            elif ev_type == EV_UI_OVERLAY_SESSION:
                overlay_sessions += 1
                ov = str(payload.get("overlay") or "")
                dwell = _safe_int(payload.get("dwell_ms"), 0)
                dwell_values.append(dwell)
                open_src = str(payload.get("open_source") or "auto_or_unknown")
                close_src = str(payload.get("close_source") or "auto_or_unknown")
                rapid = bool(payload.get("rapid_dismiss")) or (dwell < 2500 and close_src in _RAPID_CLOSE_SOURCES)
                raw_ghost = bool(payload.get("ghost_repopup")) or (
                    payload.get("ms_since_last_user_close") is not None
                    and _safe_int(payload.get("ms_since_last_user_close"), 99999) < 2000
                )
                ghost = raw_ghost and not _is_user_initiated_open_source(open_src)
                if game_state in ("IN_GAME", "AUGMENT_PICKING") or "champ_select" not in ov.lower():
                    ingame_overlay_sessions += 1
                if _is_auto_open_source(open_src):
                    auto_sessions_by_source[open_src] += 1
                    if rapid:
                        auto_rapid_by_source[open_src] += 1
                if ghost:
                    ghost_repopup_count += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Overlay** `{ov}` dwell={dwell}ms "
                    f"open={open_src} close={close_src}"
                    + (" [RAPID_DISMISS]" if rapid else "")
                    + (" [GHOST_REPOPUP]" if ghost else "")
                )

            elif ev_type == EV_UI_LAYOUT_ADJUSTED:
                layout_adjustments += 1
                detached = bool(_first(payload, "detached_edge", "detached_from_edge", "edge_detached", default=False))
                if detached:
                    detached_edge_count += 1
                    detached_edge_by_res[res_k or "unknown"] += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Layout** `{payload.get('overlay')}` action={payload.get('action', 'move')} "
                    f"res={res_k or '-'} detached_edge={detached}"
                )

            elif ev_type == EV_UI_SUBCARD:
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Subcard** `{payload.get('overlay')}` "
                    f"card={_first(payload, 'subcard', 'card', default='-')} action={payload.get('action', '-')}"
                )

            elif ev_type == EV_UI_SCROLL_OVERFLOW:
                max_ovf = _safe_int(payload.get("max_overflow_px"), 0)
                max_scr = _safe_int(payload.get("max_scroll_px"), 0)
                if max_ovf > 0 or max_scr > 0:
                    scroll_overflow_sessions += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Scroll Overflow** `{payload.get('scroll_area')}` "
                    f"max_scroll={max_scr}px overflow={max_ovf}px"
                )

            elif ev_type == EV_UI_HOTKEY_TRAY:
                act = str(_first(payload, "action", "hotkey", "key", default="") or "").lower()
                if "f4" in act or "emergency_hide" in act or "hide_all" in act:
                    f4_emergency_hide_count += 1
                if "f2" in act or "rescan" in act:
                    f2_rescan_count += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Hotkey/Tray** src={payload.get('source', '-')} action=`{act}`"
                )

            elif ev_type == EV_UI_MANUAL_TRIGGER:
                trig = str(_first(payload, "trigger", "action", default="") or "").lower()
                if "rescan" in trig or "f2" in trig:
                    f2_rescan_count += 1
                is_coach_f6 = any(tok in trig for tok in ("coach", "f6", "manual_ai"))
                if is_coach_f6:
                    prev_skipped = bool(_first(payload, "prev_gate_skipped", "after_smart_gate_skip", default=False))
                    ms_since_skip = _opt_float(payload.get("ms_since_gate_skip"))
                    s_since_skip = _opt_float(payload.get("s_since_gate_skip"))
                    within_10s = False
                    if prev_skipped:
                        if ms_since_skip is not None:
                            within_10s = ms_since_skip <= 10_000.0
                        elif s_since_skip is not None:
                            within_10s = s_since_skip <= 10.0
                        else:
                            within_10s = True
                    elif last_gate_skip_mono_ms is not None and not last_gate_skip_counted_f6:
                        dt_ms = mono_ms - last_gate_skip_mono_ms
                        dt_game = (
                            (gt - last_gate_skip_game_s)
                            if (gt is not None and last_gate_skip_game_s is not None)
                            else None
                        )
                        if (0 <= dt_ms <= 10_000) or (dt_game is not None and 0.0 <= dt_game <= 10.0):
                            within_10s = True
                    if within_10s:
                        f6_within_10s_after_gate_skip += 1
                        last_gate_skip_counted_f6 = True
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Manual Trigger** `{trig}` stale={payload.get('advice_stale')}"
                )

            elif ev_type == EV_UI_CALIBRATION:
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Calibration** result={_first(payload, 'result', 'outcome', default='-')}"
                )

            elif ev_type == EV_UI_LOOKUP:
                q = str(payload.get("query") or "").strip()
                zero_hit = bool(payload.get("zero_hit")) or (
                    q and _safe_int(_first(payload, "result_count", "hits", default=-1), -1) == 0
                )
                if zero_hit and q:
                    lookup_zero_hits[q] += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Lookup** action={payload.get('action', '-')} query=`{q}` zero_hit={zero_hit}"
                )

            elif ev_type == EV_UI_RECAP:
                act = str(payload.get("action") or "").lower()
                if act in ("generated", "shown", "open"):
                    recap_shown_count += 1
                elif act in ("copy", "save", "share"):
                    recap_shared_count += 1
                timeline_lines.append(f"- `[{ts_lbl}]` #{ev_seq} **Recap** action=`{act}`")

            # ---- AI Coach ----
            elif ev_type == EV_COACH_TURN:
                coach_turns_total += 1
                lat = _opt_float(payload.get("latency_ms"))
                if lat is not None and lat >= 0:
                    coach_latencies.append(lat)
                coach_total_tokens += _safe_int(payload.get("total_tokens"), 0)
                stale = bool(_first(payload, "stale_dropped", "expired", "discarded", default=False))
                if stale:
                    coach_stale_dropped += 1

                trig = str(payload.get("trigger_reason") or "").strip().lower()
                coach_req_id = str(payload.get("request_id") or "").strip()
                if trig == "augment_picking":
                    arrived_before = payload.get("arrived_before_pick")
                    if arrived_before is not None and (
                        not coach_req_id or coach_req_id not in seen_picker_ai_req_ids
                    ):
                        if coach_req_id:
                            seen_picker_ai_req_ids.add(coach_req_id)
                        picker_ai_total += 1
                        if not bool(arrived_before):
                            picker_ai_late += 1

                gstats = payload.get("grounding_stats")
                turn_blocked = 0
                if isinstance(gstats, dict):
                    for gk in (
                        "unknown_item_blocked",
                        "already_owned_blocked",
                        "full_build_7th_item_blocked",
                        "invalid_route_blocked",
                    ):
                        cnt = _safe_int(gstats.get(gk), 0)
                        if cnt > 0:
                            grounding_breakdown[gk] += cnt
                    turn_blocked = _safe_int(
                        gstats.get("total_blocked"),
                        sum(
                            _safe_int(gstats.get(gk), 0)
                            for gk in (
                                "unknown_item_blocked",
                                "already_owned_blocked",
                                "full_build_7th_item_blocked",
                                "invalid_route_blocked",
                            )
                        ),
                    )
                else:
                    turn_blocked = _safe_int(_first(payload, "grounding_blocked_total", "total_blocked"), 0)
                if turn_blocked > 0:
                    grounding_blocked_turns += 1

                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **AI Coach** trigger=`{trig or '-'}` model={payload.get('model', '-')} "
                    f"lat={lat if lat is not None else '-'}ms tok={payload.get('total_tokens', 0)} "
                    f"blocked={turn_blocked}" + (" [STALE]" if stale else "")
                )

            elif ev_type == EV_SMART_PICKER_GATE:
                skipped = bool(_first(payload, "skipped", "gate_skipped", "skip_ai", default=False))
                if skipped:
                    smart_gate_skips += 1
                    last_gate_skip_mono_ms = mono_ms
                    last_gate_skip_game_s = gt
                    last_gate_skip_counted_f6 = False
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Smart Gate** skipped={skipped} "
                    f"gap={_first(payload, 'top1_top2_gap', 'gap', 'margin', default='-')}"
                )

            elif ev_type == EV_DEATH_SHOP_WINDOW:
                d_start = _opt_float(payload.get("death_start_time"))
                if d_start is not None:
                    my_death_times.append(d_start)
                trig = bool(payload.get("coach_triggered"))
                skip_reason = str(payload.get("coach_skip_reason") or "").strip()
                warmup_served = (payload.get("warmup_served") is True) or (
                    skip_reason == "pre_death_warmup_cached"
                )
                bought = _safe_int(payload.get("bought_count"), len(payload.get("items_bought") or []))
                arrived_before = bool(payload.get("coach_arrived_before_buy")) or warmup_served
                if bought > 0:
                    if warmup_served:
                        death_ai_buy_windows += 1
                        death_warmup_hit_count += 1
                    elif trig:
                        death_ai_buy_windows += 1
                        if not arrived_before:
                            death_ai_late_windows += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **Death Shop** dur={payload.get('death_duration_s', 0)}s "
                    f"killer={payload.get('killer_hero') or '-'} gold={payload.get('gold_at_death', 0)}->{payload.get('gold_at_respawn', 0)} "
                    f"bought={payload.get('items_bought')} coach_trig={trig} before_buy={arrived_before}"
                    + (" [warmup_0ms]" if warmup_served else "")
                )

            # ---- Perf & System ----
            elif ev_type == EV_MAIN_THREAD_STALL:
                ops = payload.get("ops") if isinstance(payload.get("ops"), dict) else {}
                has_per_op_ge50 = any(
                    isinstance(v, dict) and "ge_50ms_count" in v for v in ops.values()
                )
                if ops and has_per_op_ge50:
                    win_ge50 = sum(
                        _safe_int(v.get("ge_50ms_count"), 0)
                        for op_name, v in ops.items()
                        if isinstance(v, dict) and not _is_background_perf_op(op_name)
                    )
                    win_worker_ge50 = sum(
                        _safe_int(v.get("ge_50ms_count"), 0)
                        for op_name, v in ops.items()
                        if isinstance(v, dict) and _is_background_perf_op(op_name)
                    )
                else:
                    raw_total = _safe_int(payload.get("total_ge_50ms_count"), -1)
                    win_ge50 = max(0, raw_total) if raw_total >= 0 else 0
                    win_worker_ge50 = max(0, _safe_int(payload.get("worker_ge_50ms_count"), 0))
                main_thread_ge_50ms_stalls += max(0, win_ge50)
                vision_worker_ge_50ms_count += max(0, win_worker_ge50)
                for op_name, stats in ops.items():
                    if not isinstance(stats, dict):
                        continue
                    low_op = op_name.lower()
                    if _is_background_perf_op(op_name):
                        wp95 = _opt_float(stats.get("p95_ms"))
                        if wp95 is not None and wp95 >= 0.0:
                            wcnt = max(1, _safe_int(stats.get("count"), 1))
                            vision_worker_p95_buckets.append((wcnt, wp95))
                        continue
                    if "tick_vision" in low_op:
                        p95 = _opt_float(stats.get("p95_ms"))
                        if p95 is not None and p95 >= 0.0:
                            cnt = max(1, _safe_int(stats.get("count"), 1))
                            tick_vision_p95_buckets.append((cnt, p95))
                    if "click_timer" in low_op:
                        mx = _safe_float(stats.get("max_ms"), 0.0)
                        if mx > click_timer_max_gap_ms:
                            click_timer_max_gap_ms = mx
                        if mx >= 90.0:
                            click_starvation_events += 1
                timeline_lines.append(
                    f"- `[{ts_lbl}]` #{ev_seq} **MainThread Stall** win={payload.get('window_s', '-')}s "
                    f">=50ms={win_ge50}"
                    + (f" worker>=50ms={win_worker_ge50}" if win_worker_ge50 > 0 else "")
                    + f" ops={list(ops.keys())}"
                )

            elif ev_type == EV_WRITER_HEALTH:
                writer_dropped_events = max(writer_dropped_events, _safe_int(payload.get("dropped_events"), 0))
                if "roi_capped_count" in payload:
                    writer_dropped_roi = max(writer_dropped_roi, _safe_int(payload.get("dropped_roi"), 0))
                    writer_roi_capped_count = max(
                        writer_roi_capped_count, _safe_int(payload.get("roi_capped_count"), 0)
                    )
                else:
                    pl_drop_roi = _safe_int(payload.get("dropped_roi"), 0)
                    pl_roi_cnt = _safe_int(payload.get("roi_count"), 0)
                    if pl_drop_roi > 0 and _safe_int(meta.get("dropped_roi"), 0) == 0 and pl_roi_cnt >= 15:
                        writer_roi_capped_count = max(writer_roi_capped_count, pl_drop_roi)
                    else:
                        writer_dropped_roi = max(writer_dropped_roi, pl_drop_roi)

            elif ev_type == EV_MATCH_EOG:
                eog_line = (
                    f"Result={ {1: 'WIN', 0: 'LOSS'}.get(_safe_int(payload.get('won'), won), 'UNKNOWN') }, "
                    f"duration={payload.get('duration_s', duration_s)}s, "
                    f"source={payload.get('eog_source', eog_source) or '-'}, "
                    f"KDA={payload.get('kda', '-')}, "
                    f"dmg={payload.get('total_damage_dealt_to_champions', '-')}, "
                    f"gold={payload.get('gold_earned', '-')}, "
                    f"items={payload.get('item_ids', [])}"
                )
                sections["eog"].append(eog_line)
                timeline_lines.append(f"- `[{ts_lbl}]` #{ev_seq} **EOG** {eog_line}")

        # Derived unlock silent-miss detection for 3/7/11/15 milestones (Mayhem matches only)
        if not is_non_mayhem:
            sorted_deaths = sorted(my_death_times)
            for ms in unlock_milestones:
                mlvl = ms["milestone_level"]
                if mlvl in silent_miss_levels_seen:
                    continue
                if ms.get("is_degraded_2999"):
                    continue
                t0 = float(ms["game_time_s"])
                exp_slots = int(ms["expected_augment_slots"])
                if int(ms["selected_augment_count"]) >= exp_slots:
                    continue
                # Levels 7/11/15 only pop after death/recall when death telemetry is available
                eff_t0 = t0
                if mlvl > 3 and sorted_deaths:
                    first_death = next((dt for dt in sorted_deaths if dt >= (t0 - 2.0)), None)
                    if first_death is None:
                        continue
                    eff_t0 = max(t0, first_death)
                if max_game_time_s < eff_t0 + 45.0:
                    continue
                opened_in_window = any((t0 - 10.0) <= pt <= (eff_t0 + 45.0) for pt in picker_open_times)
                if opened_in_window:
                    continue
                reached_slots_in_window = any(
                    t0 <= ot <= (eff_t0 + 45.0) and cnt >= exp_slots for ot, cnt in max_owned_augs_by_time
                )
                if reached_slots_in_window:
                    continue
                silent_miss_levels_seen.add(mlvl)
                explicit_silent_misses.append(
                    {
                        "milestone_level": mlvl,
                        "expected_augment_slots": exp_slots,
                        "selected_augment_count": int(ms["selected_augment_count"]),
                        "elapsed_since_unlock_s": round(max_game_time_s - eff_t0, 1),
                        "source": "derived",
                    }
                )

        tick_vision_p95_ms = _weighted_mean_p95(tick_vision_p95_buckets)
        vision_worker_p95_ms = _weighted_mean_p95(vision_worker_p95_buckets)
        primary_resolution = resolutions_seen.most_common(1)[0][0] if resolutions_seen else ""

        auto_total = sum(auto_sessions_by_source.values())
        auto_rapid = sum(auto_rapid_by_source.values())
        scroll_denom = max(ingame_overlay_sessions, scroll_overflow_sessions)

        if has_window_stats:
            ingame_vision_frames = window_stats_frames
            ingame_idle_dual_pass_frames = window_stats_idle_dual_pass_frames

        kpis: dict[str, Any] = {
            "recommendation": {
                "cs_recs": cs_recs,
                "cs_actions": cs_actions,
                "cs_followed_best_swap": cs_followed_best_swap,
                "cs_dodged": cs_dodged,
                "augment_offers": aug_offers,
                "augment_picks": aug_picks,
                "augment_unresolved_picks": aug_unresolved_picks,
                "augment_top1_adoption_rate": _safe_ratio(aug_top1_adopted, aug_picks),
                "augment_avg_hex_regret": round(sum(aug_hex_regrets) / len(aug_hex_regrets), 3) if aug_hex_regrets else None,
                "augment_avg_wr_regret": round(sum(aug_wr_regrets) / len(aug_wr_regrets), 4) if aug_wr_regrets else None,
                "augment_rerolls": aug_rerolls,
                "augment_top1_reroll_rate": _safe_ratio(aug_top1_rerolled, max(aug_offers, aug_picks)),
                "reroll_avg_hex_gain": round(sum(aug_reroll_gains) / len(aug_reroll_gains), 3) if aug_reroll_gains else None,
                "item_purchases": item_purchases,
                "augment_granted_items": augment_granted_items,
                "item_top1_adoption_rate": _safe_ratio(item_top1_adopted, item_purchases),
                "item_top4_adoption_rate": _safe_ratio(item_top4_adopted, item_purchases),
                "plan_override_count": plan_override_count,
                "player_component_override_count": player_component_override_count,
                "coach_overhaul_count": coach_overhaul_count,
                "plan_override_rate": _safe_ratio(
                    player_component_override_count,
                    max(item_purchases, player_component_override_count),
                ),
                "profile_conflict_count": len(profile_conflicts),
                "profile_conflict_rate": _safe_ratio(len(profile_conflicts), local_rec_snapshots),
            },
            "vision": {
                "picker_opens": picker_opens,
                "partial_open_count": partial_open_count,
                "recovered_slots": recovered_slots,
                "recovery_failed_slots": recovery_failed_slots,
                "unlock_silent_miss_count": len(explicit_silent_misses),
                "ocr_nearmiss_count": len(ocr_near_misses),
                "ocr_shadowed_pass2_count": len(ocr_shadowed_pass2),
                "short_substring_fp_count": len(ocr_short_substring_hits),
                "click_resolutions": click_resolutions,
                "ace_blocked_count": ace_blocked_count,
                "reroll_desync_count": len(reroll_desync_events),
                "y_threshold_clip_count": len(y_threshold_clip_events),
                "icon_verify_count": icon_verify_count,
                "icon_empty_bank_count": icon_empty_bank_count,
                "icon_flat_patch_count": icon_flat_patch_count,
                "passive_hover_hits": passive_hover_hits,
                "tab_zone_pollution_count": tab_zone_removed_count,
                "user_correction_count": user_correction_count,
            },
            "ux": {
                "overlay_sessions": overlay_sessions,
                "auto_popup_sessions": auto_total,
                "rapid_dismiss_count": auto_rapid,
                "rapid_dismiss_rate": _safe_ratio(auto_rapid, auto_total),
                "avg_dwell_ms": round(sum(dwell_values) / len(dwell_values), 1) if dwell_values else None,
                "ghost_repopup_count": ghost_repopup_count,
                "layout_adjustments": layout_adjustments,
                "detached_edge_count": detached_edge_count,
                "detached_edge_rate": _safe_ratio(detached_edge_count, layout_adjustments),
                "scroll_overflow_sessions": scroll_overflow_sessions,
                "scroll_overflow_rate": _safe_ratio(scroll_overflow_sessions, scroll_denom),
                "f4_emergency_hide_count": f4_emergency_hide_count,
                "f2_rescan_count": f2_rescan_count,
                "lookup_zero_hits": dict(lookup_zero_hits),
                "recap_share_rate": _safe_ratio(recap_shared_count, recap_shown_count),
            },
            "coach": {
                "coach_turns": coach_turns_total,
                "avg_latency_ms": round(sum(coach_latencies) / len(coach_latencies), 1) if coach_latencies else None,
                "p95_latency_ms": round(_percentile(coach_latencies, 0.95), 1) if coach_latencies else None,
                "total_tokens": coach_total_tokens,
                "stale_dropped_count": coach_stale_dropped,
                "picker_ai_timely_rate": _safe_ratio(picker_ai_total - picker_ai_late, picker_ai_total),
                "death_ai_timely_rate": _safe_ratio(death_ai_buy_windows - death_ai_late_windows, death_ai_buy_windows),
                "death_warmup_hit_count": death_warmup_hit_count,
                "disagreement_count": disagreement_count,
                "sided_with_coach_rate": _safe_ratio(sided_with_coach_count, disagreement_count),
                "grounding_blocked_turns": grounding_blocked_turns,
                "grounding_block_rate": _safe_ratio(grounding_blocked_turns, coach_turns_total),
                "grounding_breakdown": dict(grounding_breakdown),
                "smart_gate_skips": smart_gate_skips,
                "manual_f6_after_gate_skip_rate": _safe_ratio(f6_within_10s_after_gate_skip, smart_gate_skips),
            },
            "runtime": {
                "max_game_time_s": round(max_game_time_s, 1),
                "eog_complete": won in (0, 1) and (eog_source or "").lower() != "timeout",
                "tick_vision_p95_ms": round(tick_vision_p95_ms, 2),
                "main_thread_ge_50ms_stalls": main_thread_ge_50ms_stalls,
                "vision_worker_p95_ms": round(vision_worker_p95_ms, 2),
                "vision_worker_ge_50ms_count": vision_worker_ge_50ms_count,
                "click_timer_max_gap_ms": round(click_timer_max_gap_ms, 2),
                "click_starvation_events": click_starvation_events,
                "in_game_idle_dual_pass_rate": _safe_ratio(ingame_idle_dual_pass_frames, ingame_vision_frames),
                "live_disconnect_recoveries": live_disconnect_recoveries,
                "writer_dropped_events": writer_dropped_events,
                "writer_dropped_roi": writer_dropped_roi,
                "writer_roi_capped_count": writer_roi_capped_count,
            },
        }

        facts: dict[str, Any] = {
            "max_game_time_s": max_game_time_s,
            "primary_resolution": primary_resolution,
            "unlock_milestones_reached": len(unlock_milestones),
            "unlock_silent_misses": explicit_silent_misses,
            "ocr_near_misses": ocr_near_misses,
            "ocr_shadowed_pass2": ocr_shadowed_pass2,
            "ocr_short_substring_hits": ocr_short_substring_hits,
            "picker_opens": picker_opens,
            "partial_open_count": partial_open_count,
            "recovered_slots": recovered_slots,
            "recovery_failed_slots": recovery_failed_slots,
            "click_resolutions": click_resolutions,
            "ace_blocked_count": ace_blocked_count,
            "reroll_desync_events": reroll_desync_events,
            "y_threshold_clip_events": y_threshold_clip_events,
            "icon_verify_count": icon_verify_count,
            "icon_empty_bank_count": icon_empty_bank_count,
            "icon_flat_patch_count": icon_flat_patch_count,
            "icon_consecutive_miss_streaks": icon_consecutive_miss_streaks,
            "icon_resolution": icon_resolution,
            "icon_anomaly_roi_paths": list(dict.fromkeys(icon_anomaly_roi_paths)),
            "passive_hover_hits": passive_hover_hits,
            "tab_zone_hits": tab_zone_hits,
            "tab_zone_removed_count": tab_zone_removed_count,
            "transmute_roi_paths": list(dict.fromkeys(transmute_roi_paths)),
            "user_correction_count": user_correction_count,
            "aug_top1_offers": dict(aug_top1_offers_map),
            "aug_top1_adopted": dict(aug_top1_adopted_map),
            "aug_top1_rerolled": dict(aug_top1_rerolled_map),
            "aug_hex_regrets": aug_hex_regrets,
            "aug_wr_regrets": aug_wr_regrets,
            "aug_reroll_gains": aug_reroll_gains,
            "aug_picks": aug_picks,
            "aug_unresolved_picks": aug_unresolved_picks,
            "aug_top1_adopted_total": aug_top1_adopted,
            "aug_offers": aug_offers,
            "aug_top1_rerolled_total": aug_top1_rerolled,
            "item_top1_offers": dict(item_top1_offers_map),
            "item_top1_adopted": dict(item_top1_adopted_map),
            "meaningful_purchases": item_purchases,
            "augment_granted_items": augment_granted_items,
            "item_top1_adopted_total": item_top1_adopted,
            "item_top4_adopted_total": item_top4_adopted,
            "plan_override_count": plan_override_count,
            "player_component_override_count": player_component_override_count,
            "coach_overhaul_count": coach_overhaul_count,
            "local_rec_snapshots": local_rec_snapshots,
            "profile_conflicts": profile_conflicts,
            "cs_followed_best_swap": cs_followed_best_swap,
            "cs_dodged": cs_dodged,
            "picker_ai_total": picker_ai_total,
            "picker_ai_late": picker_ai_late,
            "death_ai_buy_windows": death_ai_buy_windows,
            "death_ai_late_windows": death_ai_late_windows,
            "death_warmup_hit_count": death_warmup_hit_count,
            "coach_turns_total": coach_turns_total,
            "coach_latencies": coach_latencies,
            "coach_total_tokens": coach_total_tokens,
            "coach_stale_dropped": coach_stale_dropped,
            "disagreement_count": disagreement_count,
            "sided_with_coach_count": sided_with_coach_count,
            "grounding_blocked_turns": grounding_blocked_turns,
            "grounding_breakdown": dict(grounding_breakdown),
            "smart_gate_skips": smart_gate_skips,
            "f6_within_10s_after_gate_skip": f6_within_10s_after_gate_skip,
            "overlay_sessions": overlay_sessions,
            "auto_sessions_by_source": dict(auto_sessions_by_source),
            "auto_rapid_by_source": dict(auto_rapid_by_source),
            "dwell_values": dwell_values,
            "ingame_overlay_sessions": ingame_overlay_sessions,
            "scroll_overflow_sessions": scroll_overflow_sessions,
            "ghost_repopup_count": ghost_repopup_count,
            "layout_adjustments": layout_adjustments,
            "detached_edge_count": detached_edge_count,
            "detached_edge_by_res": dict(detached_edge_by_res),
            "f4_emergency_hide_count": f4_emergency_hide_count,
            "f2_rescan_count": f2_rescan_count,
            "lookup_zero_hits": dict(lookup_zero_hits),
            "recap_shown_count": recap_shown_count,
            "recap_shared_count": recap_shared_count,
            "tick_vision_p95_ms": tick_vision_p95_ms,
            "tick_vision_p95_buckets": [[c, v] for c, v in tick_vision_p95_buckets],
            "main_thread_ge_50ms_stalls": main_thread_ge_50ms_stalls,
            "vision_worker_p95_ms": vision_worker_p95_ms,
            "vision_worker_p95_buckets": [[c, v] for c, v in vision_worker_p95_buckets],
            "vision_worker_ge_50ms_count": vision_worker_ge_50ms_count,
            "click_timer_max_gap_ms": click_timer_max_gap_ms,
            "click_starvation_events": click_starvation_events,
            "ingame_vision_frames": ingame_vision_frames,
            "ingame_idle_dual_pass_frames": ingame_idle_dual_pass_frames,
            "live_disconnect_recoveries": live_disconnect_recoveries,
            "writer_dropped_events": writer_dropped_events,
            "writer_dropped_roi": writer_dropped_roi,
            "writer_roi_capped_count": writer_roi_capped_count,
        }

        return kpis, facts, timeline_lines, sections

    def _collect_ocr_anomalies(
        self,
        payload: dict[str, Any],
        roi_refs: list[str],
        out_nearmiss: list[dict[str, Any]],
        out_shadowed: list[dict[str, Any]],
        out_short_sub: list[dict[str, Any]],
    ) -> None:
        # 1. Nested near_misses list (from detector.py diagnostics)
        nms = payload.get("near_misses")
        if isinstance(nms, list):
            for nm in nms:
                if not isinstance(nm, dict):
                    continue
                score = _extract_near_miss_score(nm)
                if score is None or 0.42 <= score < 0.58:
                    out_nearmiss.append(
                        {
                            "raw_text": str(nm.get("raw_text") or ""),
                            "top1_id": _first_nonempty(
                                nm,
                                "near_miss_id",
                                "near_miss_top1_id",
                                "top1_id",
                                "matched_id",
                                "augment_id",
                            ),
                            "score": round(score, 4) if score is not None else None,
                            "slot": _first(nm, "slot_idx", "slot"),
                            "roi_paths": list(roi_refs),
                        }
                    )
        else:
            # Flat single-slot near-miss payload
            flat_score = _extract_near_miss_score(payload)
            raw_t = str(payload.get("raw_text") or "").strip()
            if raw_t and flat_score is not None and 0.42 <= flat_score < 0.58:
                out_nearmiss.append(
                    {
                        "raw_text": raw_t,
                        "top1_id": _first_nonempty(
                            payload,
                            "near_miss_id",
                            "near_miss_top1_id",
                            "top1_id",
                            "matched_id",
                            "augment_id",
                        ),
                        "score": round(flat_score, 4),
                        "slot": _first(payload, "slot_idx", "slot"),
                        "roi_paths": list(roi_refs),
                    }
                )

        # 2. Pass2 shadowed by pass1
        shs = payload.get("shadowed_pass2")
        if isinstance(shs, list):
            for sh in shs:
                if isinstance(sh, dict):
                    out_shadowed.append(
                        {
                            "kept_pass1_text": str(sh.get("kept_pass1_text") or ""),
                            "dropped_pass2_text": str(sh.get("dropped_pass2_text") or ""),
                            "dx": sh.get("dx"),
                            "dy": sh.get("dy"),
                            "roi_paths": list(roi_refs),
                        }
                    )

        # 3. 2-char short-name substring risk hits (and slot-level near-misses if near_misses list omitted)
        slots = payload.get("slots")
        slot_entries: list[dict[str, Any]] = []
        if isinstance(slots, list):
            slot_entries = [s for s in slots if isinstance(s, dict)]
        elif payload.get("substring_hit"):
            slot_entries = [payload]
        has_explicit_nm_list = isinstance(nms, list) and len(nms) > 0
        for s in slot_entries:
            matched_id = _first_nonempty(s, "matched_id", "augment_id")
            if not has_explicit_nm_list and not matched_id:
                nm_sc = _extract_near_miss_score(s)
                nm_raw = str(s.get("raw_text") or "").strip()
                if nm_raw and nm_sc is not None and 0.42 <= nm_sc < 0.58:
                    out_nearmiss.append(
                        {
                            "raw_text": nm_raw,
                            "top1_id": _first_nonempty(s, "near_miss_id", "near_miss_top1_id", "top1_id"),
                            "score": round(nm_sc, 4),
                            "slot": _first(s, "slot_idx", "slot"),
                            "roi_paths": list(roi_refs),
                        }
                    )
            if not s.get("substring_hit"):
                continue
            name = _first_nonempty(s, "matched_name", "augment_name", "name")
            raw_t = str(s.get("raw_text") or "").strip()
            if (name and len(name) <= 2) or (not name and raw_t and len(raw_t) <= 2):
                out_short_sub.append(
                    {
                        "augment_id": matched_id,
                        "augment_name": name,
                        "raw_text": raw_t,
                        "slot": _first(s, "slot_idx", "slot"),
                        "score": _opt_float(s.get("score")),
                        "margin": _opt_float(s.get("margin")),
                        "roi_paths": list(roi_refs),
                    }
                )

    def _render_match_timeline_md(
        self,
        result: MatchReplayResult,
        sections: dict[str, list[str]],
        timeline_lines: list[str],
    ) -> str:
        outcome_label = {1: "WIN", 0: "LOSS"}.get(result.won, "UNKNOWN")
        mode_tag = "Non-Mayhem (Isolated)" if result.is_non_mayhem else "ARAM Mayhem"
        lines: list[str] = [
            f"# HexHelper DevLab 单局复盘时间轴 — Match #{result.seq:05d}",
            "",
            "| 字段 | 值 |",
            "|---|---|",
            f"| 对局目录 | `{result.dir_name}` |",
            f"| 英雄 | `{result.hero_name or result.hero_id or 'unknown'}` (`{result.hero_id or '-'}`) |",
            f"| 版本 / 数据集 | `{result.app_version or '-'}` / `{result.dataset_tag or '-'}` |",
            f"| 模式隔离标记 | `{mode_tag}` (`{result.game_mode or '-'}`) |",
            f"| 终局结果 / 来源 | `{outcome_label}` / `{result.eog_source or '-'}` |",
            f"| 对局时长 | `{result.duration_s}s` ({_fmt_mmss(float(result.duration_s))}) |",
            f"| 事件数 / 异常行 / ROI 切片 | `{result.event_count}` / `{result.corrupt_lines}` / `{len(result.roi_paths)}` |",
            "",
        ]

        lines.append("## 1. 选人阶段 (Champ Select)")
        if sections["champ_select"]:
            for item in sections["champ_select"]:
                lines.append(f"- {item}")
        else:
            lines.append("- （无选人阶段事件）")
        lines.append("")

        lines.append("## 2. 战绩侦察与符文基线 (Lobby Scout & Runes)")
        if sections["lobby_and_runes"]:
            for item in sections["lobby_and_runes"]:
                lines.append(f"- {item}")
        else:
            lines.append("- （无侦察或符文事件）")
        lines.append("")

        lines.append("## 3. 全链路时间轴 (Chronological Timeline)")
        if timeline_lines:
            lines.extend(timeline_lines)
        else:
            lines.append("- （无结构化事件）")
        lines.append("")

        lines.append("## 4. 终局战绩汇总 (EOG Summary)")
        if sections["eog"]:
            for item in sections["eog"]:
                lines.append(f"- {item}")
        else:
            lines.append(f"- Result=`{outcome_label}`, duration=`{result.duration_s}s`, eog_source=`{result.eog_source or '-'}`")
        lines.append("")

        lines.append("## 5. 五大演进支柱本局 KPI")
        for pillar_name, metrics in result.kpis.items():
            lines.append(f"### {pillar_name}")
            for k, v in metrics.items():
                if isinstance(v, dict) and not v:
                    continue
                lines.append(f"- `{k}`: `{v}`")
            lines.append("")

        lines.append("## 6. 本局触发异常告警与 ROI 索引")
        if result.issues:
            for iss in result.issues:
                lines.append(f"- `[{iss.severity}]` `{iss.detector_rule}` (n={iss.sample_count}): {iss.summary}")
        else:
            lines.append("- 无触发自动化异常告警。")
        if result.roi_paths:
            lines.append("")
            lines.append("### ROI 切片")
            for rp in result.roi_paths:
                lines.append(f"- `{rp}`")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Multi-Match Aggregation & Evolution Report
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_replay_fragments(replays: list[MatchReplayResult]) -> list[MatchReplayResult]:
        """Merge multi-folder restart fragments sharing the same non-empty match_key_hash."""
        if len(replays) <= 1:
            return list(replays)

        groups: list[list[MatchReplayResult]] = []
        hash_to_idx: dict[str, int] = {}
        for r in replays:
            mkh = str(r.match_key_hash or "").strip()
            if mkh and mkh in hash_to_idx:
                groups[hash_to_idx[mkh]].append(r)
            else:
                if mkh:
                    hash_to_idx[mkh] = len(groups)
                groups.append([r])

        merged_out: list[MatchReplayResult] = []
        for grp in groups:
            if len(grp) == 1:
                merged_out.append(grp[0])
                continue
            grp_sorted = sorted(grp, key=lambda x: (x.seq, x.started_at, x.dir_name))
            first = grp_sorted[0]
            last = grp_sorted[-1]

            # Pick authoritative outcome from fragment with known won in (0, 1)
            won = -1
            eog_source = ""
            for frag in reversed(grp_sorted):
                if frag.won in (0, 1):
                    won = frag.won
                    eog_source = frag.eog_source
                    break
            if not eog_source:
                for frag in reversed(grp_sorted):
                    src_l = (frag.eog_source or "").strip().lower()
                    if src_l and src_l != "superseded":
                        eog_source = frag.eog_source
                        break
                if not eog_source:
                    eog_source = last.eog_source

            hero_id = next((f.hero_id for f in reversed(grp_sorted) if f.hero_id), "")
            hero_name = next((f.hero_name for f in reversed(grp_sorted) if f.hero_name), "")
            dataset_tag = next((f.dataset_tag for f in reversed(grp_sorted) if f.dataset_tag), "")
            game_mode = next((f.game_mode for f in reversed(grp_sorted) if f.game_mode), "")
            app_version = next((f.app_version for f in reversed(grp_sorted) if f.app_version), "")
            started_at = next((f.started_at for f in grp_sorted if f.started_at), "")
            ended_at = next((f.ended_at for f in reversed(grp_sorted) if f.ended_at), "")
            duration_s = max((f.duration_s for f in grp_sorted), default=0)
            is_non_mayhem = all(f.is_non_mayhem for f in grp_sorted)
            roi_paths = list(dict.fromkeys(rp for f in grp_sorted for rp in f.roi_paths))

            # Merge facts cleanly across fragments
            sum_int_keys = (
                "unlock_milestones_reached",
                "picker_opens",
                "partial_open_count",
                "recovered_slots",
                "recovery_failed_slots",
                "click_resolutions",
                "ace_blocked_count",
                "icon_verify_count",
                "icon_empty_bank_count",
                "icon_flat_patch_count",
                "icon_consecutive_miss_streaks",
                "passive_hover_hits",
                "tab_zone_hits",
                "tab_zone_removed_count",
                "user_correction_count",
                "aug_picks",
                "aug_unresolved_picks",
                "aug_top1_adopted_total",
                "aug_offers",
                "aug_top1_rerolled_total",
                "meaningful_purchases",
                "augment_granted_items",
                "item_top1_adopted_total",
                "item_top4_adopted_total",
                "plan_override_count",
                "player_component_override_count",
                "coach_overhaul_count",
                "local_rec_snapshots",
                "picker_ai_total",
                "picker_ai_late",
                "death_ai_buy_windows",
                "death_ai_late_windows",
                "death_warmup_hit_count",
                "coach_turns_total",
                "coach_total_tokens",
                "coach_stale_dropped",
                "disagreement_count",
                "sided_with_coach_count",
                "grounding_blocked_turns",
                "smart_gate_skips",
                "f6_within_10s_after_gate_skip",
                "overlay_sessions",
                "ingame_overlay_sessions",
                "scroll_overflow_sessions",
                "ghost_repopup_count",
                "layout_adjustments",
                "detached_edge_count",
                "f4_emergency_hide_count",
                "f2_rescan_count",
                "recap_shown_count",
                "recap_shared_count",
                "main_thread_ge_50ms_stalls",
                "vision_worker_ge_50ms_count",
                "click_starvation_events",
                "ingame_vision_frames",
                "ingame_idle_dual_pass_frames",
                "live_disconnect_recoveries",
                "writer_dropped_events",
                "writer_dropped_roi",
                "writer_roi_capped_count",
            )
            merged_facts: dict[str, Any] = {}
            for k in sum_int_keys:
                merged_facts[k] = sum(_safe_int(f.facts.get(k), 0) for f in grp_sorted)

            # Fallback to kpis dict for counters if synthetic fragments didn't populate them in facts
            kpi_vision_fallbacks = (
                "picker_opens",
                "partial_open_count",
                "recovered_slots",
                "recovery_failed_slots",
                "click_resolutions",
                "ace_blocked_count",
                "icon_verify_count",
                "user_correction_count",
            )
            for k in kpi_vision_fallbacks:
                if merged_facts[k] == 0:
                    merged_facts[k] = sum(_safe_int(f.kpis.get("vision", {}).get(k), 0) for f in grp_sorted)
            if merged_facts["overlay_sessions"] == 0:
                merged_facts["overlay_sessions"] = sum(
                    _safe_int(f.kpis.get("ux", {}).get("overlay_sessions"), 0) for f in grp_sorted
                )

            merged_facts["max_game_time_s"] = max(
                (_safe_float(f.facts.get("max_game_time_s"), 0.0) for f in grp_sorted),
                default=float(duration_s),
            )
            merged_facts["click_timer_max_gap_ms"] = max(
                (_safe_float(f.facts.get("click_timer_max_gap_ms"), 0.0) for f in grp_sorted),
                default=0.0,
            )
            merged_facts["primary_resolution"] = next(
                (str(f.facts.get("primary_resolution") or "") for f in grp_sorted if f.facts.get("primary_resolution")),
                "",
            )
            merged_facts["icon_resolution"] = next(
                (str(f.facts.get("icon_resolution") or "") for f in grp_sorted if f.facts.get("icon_resolution")),
                "",
            )

            # Merge tick_vision_p95_buckets with sample-weighted P95
            combined_buckets: list[tuple[int, float]] = []
            for f in grp_sorted:
                raw_b = f.facts.get("tick_vision_p95_buckets")
                if isinstance(raw_b, list) and raw_b:
                    for item in raw_b:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            combined_buckets.append((_safe_int(item[0], 1), _safe_float(item[1], 0.0)))
                else:
                    p95_scalar = _safe_float(f.facts.get("tick_vision_p95_ms"), 0.0)
                    if p95_scalar > 0.0:
                        combined_buckets.append((1, p95_scalar))
            merged_facts["tick_vision_p95_buckets"] = [[c, v] for c, v in combined_buckets]
            merged_facts["tick_vision_p95_ms"] = _weighted_mean_p95(combined_buckets)

            combined_worker_buckets: list[tuple[int, float]] = []
            for f in grp_sorted:
                raw_wb = f.facts.get("vision_worker_p95_buckets")
                if isinstance(raw_wb, list) and raw_wb:
                    for item in raw_wb:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            combined_worker_buckets.append((_safe_int(item[0], 1), _safe_float(item[1], 0.0)))
                else:
                    wp95_scalar = _safe_float(f.facts.get("vision_worker_p95_ms"), 0.0)
                    if wp95_scalar > 0.0:
                        combined_worker_buckets.append((1, wp95_scalar))
            merged_facts["vision_worker_p95_buckets"] = [[c, v] for c, v in combined_worker_buckets]
            merged_facts["vision_worker_p95_ms"] = _weighted_mean_p95(combined_worker_buckets)

            # Merge list fields
            for lk in (
                "ocr_near_misses",
                "ocr_shadowed_pass2",
                "ocr_short_substring_hits",
                "reroll_desync_events",
                "y_threshold_clip_events",
                "aug_hex_regrets",
                "aug_wr_regrets",
                "aug_reroll_gains",
                "coach_latencies",
                "dwell_values",
            ):
                merged_facts[lk] = [item for f in grp_sorted for item in (f.facts.get(lk) or [])]

            # Deduped list fields
            seen_miss_lvls: set[int] = set()
            merged_misses: list[dict[str, Any]] = []
            for f in grp_sorted:
                for item in f.facts.get("unlock_silent_misses") or []:
                    lvl = _safe_int(item.get("milestone_level"), 0)
                    if lvl not in seen_miss_lvls:
                        seen_miss_lvls.add(lvl)
                        merged_misses.append(item)
            merged_facts["unlock_silent_misses"] = merged_misses

            seen_conf_keys: set[tuple[str, str]] = set()
            merged_confs: list[dict[str, Any]] = []
            for f in grp_sorted:
                for conf in f.facts.get("profile_conflicts") or []:
                    ck = (
                        str(conf.get("matchup_hero_profile") or ""),
                        str(conf.get("combo_hero_profile") or ""),
                    )
                    if ck not in seen_conf_keys:
                        seen_conf_keys.add(ck)
                        merged_confs.append(conf)
            merged_facts["profile_conflicts"] = merged_confs

            merged_facts["icon_anomaly_roi_paths"] = list(
                dict.fromkeys(p for f in grp_sorted for p in (f.facts.get("icon_anomaly_roi_paths") or []))
            )
            merged_facts["transmute_roi_paths"] = list(
                dict.fromkeys(p for f in grp_sorted for p in (f.facts.get("transmute_roi_paths") or []))
            )

            # Merge Counter-dict fields
            for dk in (
                "aug_top1_offers",
                "aug_top1_adopted",
                "aug_top1_rerolled",
                "item_top1_offers",
                "item_top1_adopted",
                "grounding_breakdown",
                "auto_sessions_by_source",
                "auto_rapid_by_source",
                "detached_edge_by_res",
                "lookup_zero_hits",
            ):
                acc: Counter[str] = Counter()
                for f in grp_sorted:
                    for ck, cv in (f.facts.get(dk) or {}).items():
                        acc[str(ck)] += _safe_int(cv, 0)
                merged_facts[dk] = dict(acc)

            # Champ-select logical state
            cs_followed = None
            for f in grp_sorted:
                if f.facts.get("cs_followed_best_swap") is not None:
                    cs_followed = bool(f.facts["cs_followed_best_swap"])
            merged_facts["cs_followed_best_swap"] = cs_followed
            played_in_game = (merged_facts["max_game_time_s"] > 0.0) or (won in (0, 1))
            merged_facts["cs_dodged"] = False if played_in_game else any(
                bool(f.facts.get("cs_dodged")) for f in grp_sorted
            )
            merged_facts["fragment_seqs"] = [f.seq for f in grp_sorted]

            merged_kpis = {
                "vision": {k: merged_facts[k] for k in kpi_vision_fallbacks},
                "ux": {"overlay_sessions": merged_facts["overlay_sessions"]},
            }

            merged_out.append(
                MatchReplayResult(
                    seq=first.seq,
                    dir_name=last.dir_name,
                    match_dir=last.match_dir,
                    match_session_id=last.match_session_id or first.match_session_id,
                    match_key_hash=first.match_key_hash,
                    app_version=app_version,
                    started_at=started_at,
                    ended_at=ended_at,
                    hero_id=hero_id,
                    hero_name=hero_name,
                    dataset_tag=dataset_tag,
                    game_mode=game_mode,
                    is_non_mayhem=is_non_mayhem,
                    won=won,
                    duration_s=duration_s,
                    eog_source=eog_source,
                    event_count=sum(f.event_count for f in grp_sorted),
                    corrupt_lines=sum(f.corrupt_lines for f in grp_sorted),
                    roi_paths=roi_paths,
                    kpis=merged_kpis,
                    issues=[],
                    timeline_markdown="",
                    facts=merged_facts,
                )
            )
        return merged_out

    def _aggregate_pillars(self, replays: list[MatchReplayResult]) -> dict[str, dict[str, Any]]:
        replays = self._merge_replay_fragments(replays)
        mayhem = [r for r in replays if not r.is_non_mayhem]
        non_mayhem = [r for r in replays if r.is_non_mayhem]

        mayhem_known = [r for r in mayhem if r.won in (0, 1)]
        mayhem_wins = sum(1 for r in mayhem_known if r.won == 1)

        # Pillar 1: Recommendation & Scoring (strictly mayhem pool for winrate & pick rates)
        p1_pool = mayhem if mayhem else replays
        aug_picks = sum(_safe_int(r.facts.get("aug_picks"), 0) for r in p1_pool)
        aug_unresolved_picks = sum(_safe_int(r.facts.get("aug_unresolved_picks"), 0) for r in p1_pool)
        aug_top1_adopted = sum(_safe_int(r.facts.get("aug_top1_adopted_total"), 0) for r in p1_pool)
        aug_offers = sum(_safe_int(r.facts.get("aug_offers"), 0) for r in p1_pool)
        aug_top1_rerolled = sum(_safe_int(r.facts.get("aug_top1_rerolled_total"), 0) for r in p1_pool)
        all_hex_regrets = [v for r in p1_pool for v in (r.facts.get("aug_hex_regrets") or [])]
        all_wr_regrets = [v for r in p1_pool for v in (r.facts.get("aug_wr_regrets") or [])]
        all_reroll_gains = [v for r in p1_pool for v in (r.facts.get("aug_reroll_gains") or [])]

        item_purchases = sum(_safe_int(r.facts.get("meaningful_purchases"), 0) for r in p1_pool)
        augment_granted_items = sum(_safe_int(r.facts.get("augment_granted_items"), 0) for r in p1_pool)
        item_top1_adopted = sum(_safe_int(r.facts.get("item_top1_adopted_total"), 0) for r in p1_pool)
        item_top4_adopted = sum(_safe_int(r.facts.get("item_top4_adopted_total"), 0) for r in p1_pool)
        plan_overrides = sum(_safe_int(r.facts.get("plan_override_count"), 0) for r in p1_pool)
        player_component_overrides = sum(
            _safe_int(
                r.facts.get("player_component_override_count", r.facts.get("plan_override_count")),
                0,
            )
            for r in p1_pool
        )
        coach_overhauls = sum(_safe_int(r.facts.get("coach_overhaul_count"), 0) for r in p1_pool)
        local_snaps = sum(_safe_int(r.facts.get("local_rec_snapshots"), 0) for r in p1_pool)
        profile_conflicts = sum(len(r.facts.get("profile_conflicts") or []) for r in p1_pool)

        cs_eval = [r for r in p1_pool if r.facts.get("cs_followed_best_swap") is not None]
        cs_followed = sum(1 for r in cs_eval if r.facts.get("cs_followed_best_swap"))
        cs_dodged = sum(1 for r in p1_pool if r.facts.get("cs_dodged"))

        # Pillar 2: Vision / OCR / Click / Icon / Transmute (across all matches)
        picker_opens = sum(
            _safe_int(r.facts.get("picker_opens", r.kpis.get("vision", {}).get("picker_opens")), 0)
            for r in replays
        )
        partial_opens = sum(
            _safe_int(r.facts.get("partial_open_count", r.kpis.get("vision", {}).get("partial_open_count")), 0)
            for r in replays
        )
        recovered_slots = sum(
            _safe_int(r.facts.get("recovered_slots", r.kpis.get("vision", {}).get("recovered_slots")), 0)
            for r in replays
        )
        recovery_failed = sum(
            _safe_int(
                r.facts.get("recovery_failed_slots", r.kpis.get("vision", {}).get("recovery_failed_slots")), 0
            )
            for r in replays
        )
        unlock_silent_misses = sum(len(r.facts.get("unlock_silent_misses") or []) for r in replays)
        ocr_nearmiss = sum(len(r.facts.get("ocr_near_misses") or []) for r in replays)
        ocr_shadowed = sum(len(r.facts.get("ocr_shadowed_pass2") or []) for r in replays)
        short_sub_fp = sum(len(r.facts.get("ocr_short_substring_hits") or []) for r in replays)
        click_resolutions = sum(
            _safe_int(r.facts.get("click_resolutions", r.kpis.get("vision", {}).get("click_resolutions")), 0)
            for r in replays
        )
        ace_blocked = sum(
            _safe_int(r.facts.get("ace_blocked_count", r.kpis.get("vision", {}).get("ace_blocked_count")), 0)
            for r in replays
        )
        reroll_desync = sum(len(r.facts.get("reroll_desync_events") or []) for r in replays)
        y_clips = sum(len(r.facts.get("y_threshold_clip_events") or []) for r in replays)
        icon_verifies = sum(
            _safe_int(r.facts.get("icon_verify_count", r.kpis.get("vision", {}).get("icon_verify_count")), 0)
            for r in replays
        )
        icon_empty = sum(_safe_int(r.facts.get("icon_empty_bank_count"), 0) for r in replays)
        icon_flat = sum(_safe_int(r.facts.get("icon_flat_patch_count"), 0) for r in replays)
        passive_hits = sum(_safe_int(r.facts.get("passive_hover_hits"), 0) for r in replays)
        tab_pollution = sum(_safe_int(r.facts.get("tab_zone_removed_count"), 0) for r in replays)
        user_corrections = sum(
            _safe_int(
                r.facts.get("user_correction_count", r.kpis.get("vision", {}).get("user_correction_count")), 0
            )
            for r in replays
        )

        # Pillar 3: UX & Ergonomics
        overlay_sessions = sum(
            _safe_int(r.facts.get("overlay_sessions", r.kpis.get("ux", {}).get("overlay_sessions")), 0)
            for r in replays
        )
        auto_total = sum(sum((r.facts.get("auto_sessions_by_source") or {}).values()) for r in replays)
        auto_rapid = sum(sum((r.facts.get("auto_rapid_by_source") or {}).values()) for r in replays)
        all_dwells = [v for r in replays for v in (r.facts.get("dwell_values") or [])]
        ghost_repopups = sum(_safe_int(r.facts.get("ghost_repopup_count"), 0) for r in replays)
        layout_adj = sum(_safe_int(r.facts.get("layout_adjustments"), 0) for r in replays)
        detached_edge = sum(_safe_int(r.facts.get("detached_edge_count"), 0) for r in replays)
        ingame_ov_sessions = sum(_safe_int(r.facts.get("ingame_overlay_sessions"), 0) for r in replays)
        scroll_ovf = sum(_safe_int(r.facts.get("scroll_overflow_sessions"), 0) for r in replays)
        f4_hides = sum(_safe_int(r.facts.get("f4_emergency_hide_count"), 0) for r in replays)
        f2_rescans = sum(_safe_int(r.facts.get("f2_rescan_count"), 0) for r in replays)
        zero_hits_all: Counter[str] = Counter()
        for r in replays:
            for q, cnt in (r.facts.get("lookup_zero_hits") or {}).items():
                zero_hits_all[str(q)] += _safe_int(cnt, 0)
        recap_shown = sum(_safe_int(r.facts.get("recap_shown_count"), 0) for r in replays)
        recap_shared = sum(_safe_int(r.facts.get("recap_shared_count"), 0) for r in replays)

        # Pillar 4: AI Coach
        coach_turns = sum(_safe_int(r.facts.get("coach_turns_total"), 0) for r in replays)
        all_lats = [v for r in replays for v in (r.facts.get("coach_latencies") or [])]
        coach_tokens = sum(_safe_int(r.facts.get("coach_total_tokens"), 0) for r in replays)
        coach_stale = sum(_safe_int(r.facts.get("coach_stale_dropped"), 0) for r in replays)
        picker_ai_total = sum(_safe_int(r.facts.get("picker_ai_total"), 0) for r in replays)
        picker_ai_late = sum(_safe_int(r.facts.get("picker_ai_late"), 0) for r in replays)
        death_ai_total = sum(_safe_int(r.facts.get("death_ai_buy_windows"), 0) for r in replays)
        death_ai_late = sum(_safe_int(r.facts.get("death_ai_late_windows"), 0) for r in replays)
        death_warmup_hits = sum(_safe_int(r.facts.get("death_warmup_hit_count"), 0) for r in replays)
        disagreements = sum(_safe_int(r.facts.get("disagreement_count"), 0) for r in replays)
        sided_coach = sum(_safe_int(r.facts.get("sided_with_coach_count"), 0) for r in replays)
        grounding_blocked = sum(_safe_int(r.facts.get("grounding_blocked_turns"), 0) for r in replays)
        grounding_bd: Counter[str] = Counter()
        for r in replays:
            for k, v in (r.facts.get("grounding_breakdown") or {}).items():
                grounding_bd[str(k)] += _safe_int(v, 0)
        gate_skips = sum(_safe_int(r.facts.get("smart_gate_skips"), 0) for r in replays)
        f6_after_skip = sum(_safe_int(r.facts.get("f6_within_10s_after_gate_skip"), 0) for r in replays)

        # Pillar 5: Runtime Stability & Latency
        eog_eligible = [
            r for r in replays if max(r.duration_s, _safe_int(r.facts.get("max_game_time_s"), 0)) >= 180
        ]
        eog_lost = [
            r
            for r in eog_eligible
            if r.won not in (0, 1) or (r.eog_source or "").strip().lower() == "timeout"
        ]
        eog_complete = len(eog_eligible) - len(eog_lost)
        all_tick_buckets: list[tuple[int, float]] = []
        for r in replays:
            raw_b = r.facts.get("tick_vision_p95_buckets")
            if isinstance(raw_b, list) and raw_b:
                for item in raw_b:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        all_tick_buckets.append((_safe_int(item[0], 1), _safe_float(item[1], 0.0)))
            else:
                p95_s = _safe_float(r.facts.get("tick_vision_p95_ms"), 0.0)
                if p95_s > 0.0:
                    all_tick_buckets.append((1, p95_s))
        weighted_tick_p95 = _weighted_mean_p95(all_tick_buckets)
        total_stalls = sum(_safe_int(r.facts.get("main_thread_ge_50ms_stalls"), 0) for r in replays)
        max_stalls_match = max((_safe_int(r.facts.get("main_thread_ge_50ms_stalls"), 0) for r in replays), default=0)
        all_worker_buckets: list[tuple[int, float]] = []
        for r in replays:
            raw_wb = r.facts.get("vision_worker_p95_buckets")
            if isinstance(raw_wb, list) and raw_wb:
                for item in raw_wb:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        all_worker_buckets.append((_safe_int(item[0], 1), _safe_float(item[1], 0.0)))
            else:
                wp95_s = _safe_float(r.facts.get("vision_worker_p95_ms"), 0.0)
                if wp95_s > 0.0:
                    all_worker_buckets.append((1, wp95_s))
        weighted_worker_p95 = _weighted_mean_p95(all_worker_buckets)
        total_worker_ge_50 = sum(_safe_int(r.facts.get("vision_worker_ge_50ms_count"), 0) for r in replays)
        max_click_gap = max((_safe_float(r.facts.get("click_timer_max_gap_ms"), 0.0) for r in replays), default=0.0)
        click_starv = sum(_safe_int(r.facts.get("click_starvation_events"), 0) for r in replays)
        ig_frames = sum(_safe_int(r.facts.get("ingame_vision_frames"), 0) for r in replays)
        ig_dp = sum(_safe_int(r.facts.get("ingame_idle_dual_pass_frames"), 0) for r in replays)
        recoveries = sum(_safe_int(r.facts.get("live_disconnect_recoveries"), 0) for r in replays)
        drop_ev = sum(_safe_int(r.facts.get("writer_dropped_events"), 0) for r in replays)
        drop_roi = sum(_safe_int(r.facts.get("writer_dropped_roi"), 0) for r in replays)
        roi_capped = sum(_safe_int(r.facts.get("writer_roi_capped_count"), 0) for r in replays)

        return {
            "recommendation": {
                "mayhem_matches": len(mayhem),
                "mayhem_known_outcome_matches": len(mayhem_known),
                "mayhem_winrate": _safe_ratio(mayhem_wins, len(mayhem_known)),
                "non_mayhem_isolated_matches": len(non_mayhem),
                "augment_picks": aug_picks,
                "augment_unresolved_picks": aug_unresolved_picks,
                "augment_top1_adoption_rate": _safe_ratio(aug_top1_adopted, aug_picks),
                "augment_avg_hex_regret": round(sum(all_hex_regrets) / len(all_hex_regrets), 3) if all_hex_regrets else None,
                "augment_avg_wr_regret": round(sum(all_wr_regrets) / len(all_wr_regrets), 4) if all_wr_regrets else None,
                "augment_top1_reroll_rate": _safe_ratio(aug_top1_rerolled, max(aug_offers, aug_picks)),
                "reroll_avg_hex_gain": round(sum(all_reroll_gains) / len(all_reroll_gains), 3) if all_reroll_gains else None,
                "item_purchases": item_purchases,
                "augment_granted_items": augment_granted_items,
                "item_top1_adoption_rate": _safe_ratio(item_top1_adopted, item_purchases),
                "item_top4_adoption_rate": _safe_ratio(item_top4_adopted, item_purchases),
                "plan_override_count": plan_overrides,
                "player_component_override_count": player_component_overrides,
                "coach_overhaul_count": coach_overhauls,
                "plan_override_rate": _safe_ratio(
                    player_component_overrides,
                    max(item_purchases, player_component_overrides),
                ),
                "profile_conflict_count": profile_conflicts,
                "profile_conflict_rate": _safe_ratio(profile_conflicts, local_snaps),
                "champ_select_best_swap_rate": _safe_ratio(cs_followed, len(cs_eval)),
                "champ_select_dodge_rate": _safe_ratio(cs_dodged, len(p1_pool)),
            },
            "vision": {
                "picker_opens": picker_opens,
                "partial_open_count": partial_opens,
                "recovered_slots": recovered_slots,
                "recovery_failed_slots": recovery_failed,
                "unlock_silent_miss_count": unlock_silent_misses,
                "ocr_nearmiss_count": ocr_nearmiss,
                "ocr_shadowed_pass2_count": ocr_shadowed,
                "short_substring_fp_count": short_sub_fp,
                "click_resolutions": click_resolutions,
                "ace_blocked_count": ace_blocked,
                "reroll_desync_count": reroll_desync,
                "y_threshold_clip_count": y_clips,
                "icon_verify_count": icon_verifies,
                "icon_empty_bank_count": icon_empty,
                "icon_flat_patch_count": icon_flat,
                "passive_hover_hits": passive_hits,
                "tab_zone_pollution_count": tab_pollution,
                "user_correction_count": user_corrections,
            },
            "ux": {
                "overlay_sessions": overlay_sessions,
                "auto_popup_sessions": auto_total,
                "rapid_dismiss_count": auto_rapid,
                "rapid_dismiss_rate": _safe_ratio(auto_rapid, auto_total),
                "avg_dwell_ms": round(sum(all_dwells) / len(all_dwells), 1) if all_dwells else None,
                "ghost_repopup_count": ghost_repopups,
                "layout_adjustments": layout_adj,
                "detached_edge_count": detached_edge,
                "detached_edge_rate": _safe_ratio(detached_edge, layout_adj),
                "scroll_overflow_sessions": scroll_ovf,
                "scroll_overflow_rate": _safe_ratio(scroll_ovf, max(ingame_ov_sessions, scroll_ovf)),
                "f4_emergency_hide_count": f4_hides,
                "f2_rescan_count": f2_rescans,
                "lookup_zero_hit_top": [{"query": q, "count": c} for q, c in zero_hits_all.most_common(10)],
                "recap_share_rate": _safe_ratio(recap_shared, recap_shown),
            },
            "coach": {
                "coach_turns": coach_turns,
                "avg_latency_ms": round(sum(all_lats) / len(all_lats), 1) if all_lats else None,
                "p95_latency_ms": round(_percentile(all_lats, 0.95), 1) if all_lats else None,
                "total_tokens": coach_tokens,
                "stale_dropped_count": coach_stale,
                "picker_ai_timely_rate": _safe_ratio(picker_ai_total - picker_ai_late, picker_ai_total),
                "death_ai_timely_rate": _safe_ratio(death_ai_total - death_ai_late, death_ai_total),
                "death_warmup_hit_count": death_warmup_hits,
                "disagreement_count": disagreements,
                "sided_with_coach_rate": _safe_ratio(sided_coach, disagreements),
                "grounding_blocked_turns": grounding_blocked,
                "grounding_block_rate": _safe_ratio(grounding_blocked, coach_turns),
                "grounding_breakdown": dict(grounding_bd),
                "smart_gate_skips": gate_skips,
                "manual_f6_after_gate_skip_rate": _safe_ratio(f6_after_skip, gate_skips),
            },
            "runtime": {
                "eog_eligible_matches": len(eog_eligible),
                "eog_complete_rate": _safe_ratio(eog_complete, len(eog_eligible)),
                "eog_race_lost_rate": _safe_ratio(len(eog_lost), len(eog_eligible)),
                "tick_vision_p95_ms": weighted_tick_p95,
                "main_thread_ge_50ms_stalls": total_stalls,
                "max_stalls_per_match": max_stalls_match,
                "vision_worker_p95_ms": weighted_worker_p95,
                "vision_worker_ge_50ms_count": total_worker_ge_50,
                "click_timer_max_gap_ms": round(max_click_gap, 2),
                "click_starvation_events": click_starv,
                "in_game_idle_dual_pass_rate": _safe_ratio(ig_dp, ig_frames),
                "live_disconnect_recoveries": recoveries,
                "writer_dropped_events": drop_ev,
                "writer_dropped_roi": drop_roi,
                "writer_roi_capped_count": roi_capped,
            },
        }

    def generate_evolution_report(
        self,
        match_dirs: list[Path] | None = None,
        *,
        last_n: int = 20,
        since: str | None = None,
        out_dir: Path | None = None,
    ) -> EvolutionReport:
        """Aggregate multi-match 5-pillar KPIs, run 10 detectors, and write report + backlog JSON."""
        selected_dirs: list[Path] = []
        if match_dirs is not None:
            selected_dirs = [Path(p) for p in match_dirs]
        else:
            entries = self.list_matches()
            if since:
                since_str = str(since).strip()
                entries = [e for e in entries if str(e.get("started_at") or "") >= since_str]
            if last_n and last_n > 0:
                entries = entries[-int(last_n) :]
            selected_dirs = [Path(e["match_dir"]) for e in entries]

        raw_replays = [self.replay_match(mdir, write_outputs=False) for mdir in selected_dirs]
        replays = self._merge_replay_fragments(raw_replays)
        mayhem = [r for r in replays if not r.is_non_mayhem]
        non_mayhem = [r for r in replays if r.is_non_mayhem]

        pillars = self._aggregate_pillars(replays)
        issues = run_issue_detectors(replays)

        # Breakdown by resolution/DPI
        by_resolution: dict[str, dict[str, Any]] = {}
        res_groups: dict[str, list[MatchReplayResult]] = defaultdict(list)
        for r in replays:
            res_k = str(r.facts.get("primary_resolution") or "unknown")
            res_groups[res_k].append(r)
        for res_k, grp in sorted(res_groups.items()):
            by_resolution[res_k] = {
                "matches": len(grp),
                "silent_misses": sum(len(r.facts.get("unlock_silent_misses") or []) for r in grp),
                "ocr_nearmiss": sum(len(r.facts.get("ocr_near_misses") or []) for r in grp),
                "icon_flat_patches": sum(_safe_int(r.facts.get("icon_flat_patch_count"), 0) for r in grp),
                "detached_edge_drags": sum(_safe_int(r.facts.get("detached_edge_count"), 0) for r in grp),
                "scroll_overflows": sum(_safe_int(r.facts.get("scroll_overflow_sessions"), 0) for r in grp),
            }

        # Breakdown by hero (strictly mayhem pool so custom/SR matches don't pollute hero winrate)
        by_hero: dict[str, dict[str, Any]] = {}
        hero_groups: dict[str, list[MatchReplayResult]] = defaultdict(list)
        for r in mayhem:
            hero_groups[r.hero_id or "unknown"].append(r)
        for hero, grp in sorted(hero_groups.items()):
            known = [r for r in grp if r.won in (0, 1)]
            wins = sum(1 for r in known if r.won == 1)
            ap = sum(_safe_int(r.facts.get("aug_picks"), 0) for r in grp)
            at1 = sum(_safe_int(r.facts.get("aug_top1_adopted_total"), 0) for r in grp)
            ip = sum(_safe_int(r.facts.get("meaningful_purchases"), 0) for r in grp)
            it4 = sum(_safe_int(r.facts.get("item_top4_adopted_total"), 0) for r in grp)
            ov = sum(
                _safe_int(r.facts.get("player_component_override_count", r.facts.get("plan_override_count")), 0)
                for r in grp
            )
            coach_ov = sum(_safe_int(r.facts.get("coach_overhaul_count"), 0) for r in grp)
            pc = sum(len(r.facts.get("profile_conflicts") or []) for r in grp)
            by_hero[hero] = {
                "matches": len(grp),
                "winrate": _safe_ratio(wins, len(known)),
                "augment_top1_adoption_rate": _safe_ratio(at1, ap),
                "item_top4_adoption_rate": _safe_ratio(it4, ip),
                "plan_overrides": ov,
                "coach_overhauls": coach_ov,
                "profile_conflicts": pc,
            }

        # ROI snapshot index (walk raw fragments so every on-disk roi_snapshots path resolves cleanly)
        roi_index: list[dict[str, Any]] = []
        for r in raw_replays:
            mdir = Path(r.match_dir)
            for rel in r.roi_paths:
                fname = Path(rel).name
                fpath = mdir / "roi_snapshots" / fname
                sz = fpath.stat().st_size if fpath.is_file() else 0
                roi_index.append(
                    {
                        "match_seq": r.seq,
                        "hero_id": r.hero_id,
                        "path": rel,
                        "file_name": fname,
                        "size_bytes": sz,
                    }
                )

        now_iso = _utc_now_iso()
        stamp = _utc_stamp()
        report = EvolutionReport(
            generated_at=now_iso,
            stamp=stamp,
            root_dir=str(self.root_dir),
            total_matches=len(replays),
            mayhem_matches=len(mayhem),
            non_mayhem_matches=len(non_mayhem),
            match_seqs=[r.seq for r in replays],
            mayhem_match_seqs=[r.seq for r in mayhem],
            non_mayhem_match_seqs=[r.seq for r in non_mayhem],
            pillars=pillars,
            by_resolution=by_resolution,
            by_hero=by_hero,
            roi_index=roi_index,
            issues=issues,
        )
        report.report_markdown = self._render_evolution_report_md(report)

        target_out = Path(out_dir) if out_dir is not None else self.reports_dir
        target_out.mkdir(parents=True, exist_ok=True)
        rep_path = target_out / f"evolution_report_{stamp}.md"
        bkl_path = target_out / f"evolution_backlog_{stamp}.json"
        latest_rep = target_out / "latest_evolution_report.md"
        latest_bkl = target_out / "latest_evolution_backlog.json"

        _atomic_write_text(rep_path, report.report_markdown)
        _atomic_write_json(bkl_path, report.to_backlog_dict())
        _atomic_write_text(latest_rep, report.report_markdown)
        _atomic_write_json(latest_bkl, report.to_backlog_dict())

        report.report_path = str(rep_path)
        report.backlog_path = str(bkl_path)
        report.latest_report_path = str(latest_rep)
        report.latest_backlog_path = str(latest_bkl)
        return report

    def _render_evolution_report_md(self, report: EvolutionReport) -> str:
        p1 = report.pillars.get("recommendation", {})
        p2 = report.pillars.get("vision", {})
        p3 = report.pillars.get("ux", {})
        p4 = report.pillars.get("coach", {})
        p5 = report.pillars.get("runtime", {})

        lines: list[str] = [
            f"# HexHelper DevLab 演进分析报告 ({report.stamp})",
            "",
            f"- 生成时间：`{report.generated_at}`",
            f"- 样本总量：`{report.total_matches}` 局（主海克斯大乱斗池：`{report.mayhem_matches}` 局；非海克斯/自定义隔离：`{report.non_mayhem_matches}` 局）",
            f"- 涵盖局号：`{report.match_seqs}`",
            "",
            "## 1. 五大演进支柱核心 KPI 总览",
            "",
            "| 演进支柱 | 关键指标 | 当前值 |",
            "|---|---|---|",
            f"| 1. 推荐与评分 | 主池胜率 (Mayhem Only, n={p1.get('mayhem_known_outcome_matches', 0)}) | `{_fmt_pct(p1.get('mayhem_winrate'))}` |",
            f"| 1. 推荐与评分 | 海克斯 Top1 采纳率 / 重随率 | `{_fmt_pct(p1.get('augment_top1_adoption_rate'))}` / `{_fmt_pct(p1.get('augment_top1_reroll_rate'))}` |",
            f"| 1. 推荐与评分 | 出装 Top1 / Top4 因果采纳率 | `{_fmt_pct(p1.get('item_top1_adoption_rate'))}` / `{_fmt_pct(p1.get('item_top4_adoption_rate'))}` |",
            f"| 1. 推荐与评分 | >=800G 路线推翻率 / 跨引擎流派冲突 | `{_fmt_pct(p1.get('plan_override_rate'))}` / `{p1.get('profile_conflict_count', 0)}` |",
            f"| 2. 视觉与 OCR | 解锁静默漏检 / 补救失败槽位 | `{p2.get('unlock_silent_miss_count', 0)}` / `{p2.get('recovery_failed_slots', 0)}` |",
            f"| 2. 视觉与 OCR | OCR 近阈值未命中 / Pass2 吞没 | `{p2.get('ocr_nearmiss_count', 0)}` / `{p2.get('ocr_shadowed_pass2_count', 0)}` |",
            f"| 2. 视觉与 OCR | Reroll Desync / Y 阈值误截 | `{p2.get('reroll_desync_count', 0)}` / `{p2.get('y_threshold_clip_count', 0)}` |",
            f"| 2. 视觉与 OCR | 图标空库 / 平坦裁偏 / Tab 误吸 | `{p2.get('icon_empty_bank_count', 0)}` / `{p2.get('icon_flat_patch_count', 0)}` / `{p2.get('tab_zone_pollution_count', 0)}` |",
            f"| 3. 浮窗 UX | 自动弹窗 <2.5s 快关烦躁率 / 幽灵重弹 | `{_fmt_pct(p3.get('rapid_dismiss_rate'))}` / `{p3.get('ghost_repopup_count', 0)}` |",
            f"| 3. 浮窗 UX | 脱离吸边比例 / 局内滚动溢出率 | `{_fmt_pct(p3.get('detached_edge_rate'))}` / `{_fmt_pct(p3.get('scroll_overflow_rate'))}` |",
            f"| 3. 浮窗 UX | F4 紧急隐藏 / F2 强制重扫 / 战报分享率 | `{p3.get('f4_emergency_hide_count', 0)}` / `{p3.get('f2_rescan_count', 0)}` / `{_fmt_pct(p3.get('recap_share_rate'))}` |",
            f"| 4. AI 参谋 | 选卡及时抵达率 / 阵亡窗及时抵达率 (0ms预热命中) | `{_fmt_pct(p4.get('picker_ai_timely_rate'))}` / `{_fmt_pct(p4.get('death_ai_timely_rate'))}` (`{p4.get('death_warmup_hit_count', 0)}`) |",
            f"| 4. AI 参谋 | Grounding 拦截率 / Gate 跳过后 10s 补呼 F6 | `{_fmt_pct(p4.get('grounding_block_rate'))}` / `{_fmt_pct(p4.get('manual_f6_after_gate_skip_rate'))}` |",
            f"| 4. AI 参谋 | 分歧局站队 AI 比例 / 延迟 P95 | `{_fmt_pct(p4.get('sided_with_coach_rate'))}` / `{_fmt_num(p4.get('p95_latency_ms'), 'ms')}` |",
            f"| 5. 运行稳定性 | EOG 胜负完整率 / 竞态丢失率 | `{_fmt_pct(p5.get('eog_complete_rate'))}` / `{_fmt_pct(p5.get('eog_race_lost_rate'))}` |",
            f"| 5. 运行稳定性 | `_tick_vision` P95 / 主线程 `>=50ms` 卡顿总数 | `{_fmt_num(p5.get('tick_vision_p95_ms'), 'ms')}` / `{p5.get('main_thread_ge_50ms_stalls', 0)}` |",
            f"| 5. 运行稳定性 | 后台 `vision_worker` P95 / `>=50ms` 帧数 | `{_fmt_num(p5.get('vision_worker_p95_ms'), 'ms')}` / `{p5.get('vision_worker_ge_50ms_count', 0)}` |",
            f"| 5. 运行稳定性 | `_click_timer` 最大间隙 / IN_GAME 空转双 pass | `{_fmt_num(p5.get('click_timer_max_gap_ms'), 'ms')}` / `{_fmt_pct(p5.get('in_game_idle_dual_pass_rate'))}` |",
            "",
            "## 2. 自动化问题检测榜单 (10 Detectors)",
            "",
        ]

        if report.issues:
            for iss in report.issues:
                lines.append(f"### `[{iss.severity}]` `{iss.detector_rule}` (samples={iss.sample_count}, metric={iss.metric_value})")
                lines.append(f"- 支柱：`{iss.pillar}`")
                lines.append(f"- 摘要：{iss.summary}")
                lines.append(f"- 定位符号：`{', '.join(iss.target_files_and_symbols)}`")
                lines.append(f"- 证据局号：`{iss.evidence_match_seqs}`")
                if iss.evidence_roi_paths:
                    lines.append(f"- 关联 ROI 切片：`{', '.join(iss.evidence_roi_paths[:6])}`")
                lines.append(f"- 建议实验：{iss.proposed_experiment}")
                lines.append("")
        else:
            lines.append("- 本批次 10 条自动化规则检测器零告警。")
            lines.append("")

        lines.append("## 3. 按分辨率 / DPI 短板拆分")
        lines.append("")
        if report.by_resolution:
            lines.append("| 分辨率/DPI | 局数 | 静默漏检 | OCR近阈值 | 图标平坦 | 拖离吸边 | 滚动溢出 |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for res_k, st in report.by_resolution.items():
                lines.append(
                    f"| `{res_k}` | {st['matches']} | {st['silent_misses']} | {st['ocr_nearmiss']} | "
                    f"{st['icon_flat_patches']} | {st['detached_edge_drags']} | {st['scroll_overflows']} |"
                )
        else:
            lines.append("- （无分辨率统计数据）")
        lines.append("")

        lines.append("## 4. 按英雄表现拆分（仅主海克斯大乱斗池）")
        lines.append("")
        if report.by_hero:
            lines.append("| 英雄 | 局数 | 胜率 | 海克斯Top1采纳 | 出装Top4采纳 | 路线推翻 | 流派冲突 |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for hero, st in report.by_hero.items():
                lines.append(
                    f"| `{hero}` | {st['matches']} | {_fmt_pct(st['winrate'])} | "
                    f"{_fmt_pct(st['augment_top1_adoption_rate'])} | {_fmt_pct(st['item_top4_adoption_rate'])} | "
                    f"{st['plan_overrides']} | {st['profile_conflicts']} |"
                )
        else:
            lines.append("- （无主海克斯大乱斗对局样本）")
        lines.append("")

        if report.non_mayhem_matches > 0:
            lines.append(
                f"> 非海克斯/自定义模式已自动隔离 `{report.non_mayhem_matches}` 局"
                f"（局号：`{report.non_mayhem_match_seqs}`），未混入主胜率池。"
            )
            lines.append("")

        lines.append("## 5. 异常 ROI 切片索引")
        lines.append("")
        if report.roi_index:
            for item in report.roi_index[:40]:
                lines.append(
                    f"- Match `#{item['match_seq']:05d}` (`{item['hero_id'] or '-'}`): "
                    f"`{item['path']}` ({item['size_bytes']} B)"
                )
        else:
            lines.append("- （无 ROI 切片记录）")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cross-Version A/B Comparator
    # ------------------------------------------------------------------

    def compare_versions(
        self,
        ver_a: str,
        ver_b: str,
        *,
        out_dir: Path | None = None,
    ) -> VersionDiffReport:
        """Compare 5-pillar metrics and detected issues across two app_version prefixes."""
        va = str(ver_a or "").strip()
        vb = str(ver_b or "").strip()
        entries = self.list_matches()

        dirs_a: list[Path] = []
        dirs_b: list[Path] = []
        for entry in entries:
            v = str(entry.get("app_version") or "").strip()
            match_a = bool(va) and (v == va or v.startswith(va))
            match_b = bool(vb) and (v == vb or v.startswith(vb))
            if match_a and match_b:
                # Assign to the more specific match (exact or longer prefix)
                if v == vb or (v != va and len(vb) > len(va)):
                    dirs_b.append(Path(entry["match_dir"]))
                else:
                    dirs_a.append(Path(entry["match_dir"]))
            elif match_a:
                dirs_a.append(Path(entry["match_dir"]))
            elif match_b:
                dirs_b.append(Path(entry["match_dir"]))

        replays_a = self._merge_replay_fragments([self.replay_match(p, write_outputs=False) for p in dirs_a])
        replays_b = self._merge_replay_fragments([self.replay_match(p, write_outputs=False) for p in dirs_b])

        pillars_a = self._aggregate_pillars(replays_a)
        pillars_b = self._aggregate_pillars(replays_b)
        issues_a = run_issue_detectors(replays_a)
        issues_b = run_issue_detectors(replays_b)

        deltas: dict[str, dict[str, dict[str, Any]]] = {}
        for pillar_name in ("recommendation", "vision", "ux", "coach", "runtime"):
            pa = pillars_a.get(pillar_name, {})
            pb = pillars_b.get(pillar_name, {})
            p_deltas: dict[str, dict[str, Any]] = {}
            for key in pa:
                val_a = pa.get(key)
                val_b = pb.get(key)
                if isinstance(val_a, (dict, list)) or isinstance(val_b, (dict, list)):
                    continue
                if isinstance(val_a, bool) or isinstance(val_b, bool):
                    continue
                if val_a is None and val_b is None:
                    p_deltas[key] = {"a": None, "b": None, "delta": None, "status": "na"}
                    continue
                if val_a is None or val_b is None:
                    p_deltas[key] = {"a": val_a, "b": val_b, "delta": None, "status": "partial"}
                    continue
                diff = round(float(val_b) - float(val_a), 4)
                status = "flat"
                if abs(diff) > 1e-9 and key in _HIGHER_IS_BETTER:
                    improved = (diff > 0) if _HIGHER_IS_BETTER[key] else (diff < 0)
                    status = "improved" if improved else "regressed"
                p_deltas[key] = {"a": val_a, "b": val_b, "delta": diff, "status": status}
            deltas[pillar_name] = p_deltas

        diff_report = VersionDiffReport(
            generated_at=_utc_now_iso(),
            ver_a=va,
            ver_b=vb,
            matches_a=len(replays_a),
            matches_b=len(replays_b),
            mayhem_matches_a=sum(1 for r in replays_a if not r.is_non_mayhem),
            mayhem_matches_b=sum(1 for r in replays_b if not r.is_non_mayhem),
            pillars_a=pillars_a,
            pillars_b=pillars_b,
            deltas=deltas,
            issues_a=issues_a,
            issues_b=issues_b,
        )
        diff_report.diff_markdown = self._render_version_diff_md(diff_report)

        target_out = Path(out_dir) if out_dir is not None else self.reports_dir
        target_out.mkdir(parents=True, exist_ok=True)
        safe_a = _safe_file_token(va, "verA")
        safe_b = _safe_file_token(vb, "verB")
        out_path = target_out / f"version_diff_{safe_a}_vs_{safe_b}.md"
        _atomic_write_text(out_path, diff_report.diff_markdown)
        diff_report.diff_path = str(out_path)
        return diff_report

    def _render_version_diff_md(self, diff: VersionDiffReport) -> str:
        lines: list[str] = [
            f"# HexHelper DevLab 跨版本 A/B 回归对比：`{diff.ver_a}` vs `{diff.ver_b}`",
            "",
            f"- 生成时间：`{diff.generated_at}`",
            f"- 版本 A (`{diff.ver_a}`)：`{diff.matches_a}` 局（主海克斯池 `{diff.mayhem_matches_a}` 局）",
            f"- 版本 B (`{diff.ver_b}`)：`{diff.matches_b}` 局（主海克斯池 `{diff.mayhem_matches_b}` 局）",
            "",
        ]

        for pillar_name in ("recommendation", "vision", "ux", "coach", "runtime"):
            lines.append(f"## 支柱：{pillar_name}")
            lines.append("")
            lines.append(f"| 指标 | `{diff.ver_a}` | `{diff.ver_b}` | Delta (B-A) | 判定 |")
            lines.append("|---|---:|---:|---:|---|")
            for k, row in diff.deltas.get(pillar_name, {}).items():
                a_v = row["a"]
                b_v = row["b"]
                d_v = row["delta"]
                is_rate = k.endswith("_rate") or k.endswith("winrate")
                if is_rate:
                    a_s = _fmt_pct(a_v)
                    b_s = _fmt_pct(b_v)
                    d_s = f"{d_v * 100:+.1f}pp" if d_v is not None else "N/A"
                else:
                    a_s = str(a_v) if a_v is not None else "N/A"
                    b_s = str(b_v) if b_v is not None else "N/A"
                    d_s = f"{d_v:+.4g}" if d_v is not None else "N/A"
                lines.append(f"| `{k}` | `{a_s}` | `{b_s}` | `{d_s}` | `{row['status']}` |")
            lines.append("")

        rules_a = {i.detector_rule for i in diff.issues_a}
        rules_b = {i.detector_rule for i in diff.issues_b}
        resolved = sorted(rules_a - rules_b)
        introduced = sorted(rules_b - rules_a)
        persisting = sorted(rules_a & rules_b)
        lines.append("## 自动化检测器变化")
        lines.append(f"- 已消除：`{resolved}`")
        lines.append(f"- 新引入：`{introduced}`")
        lines.append(f"- 仍存在：`{persisting}`")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Health, Quota & Zero-PII Doctor
    # ------------------------------------------------------------------

    def doctor(self, *, max_scan_matches: int = 30) -> dict[str, Any]:
        """Audit directory health, index consistency, disk/ROI quotas, PII leaks, and drops."""
        checks: dict[str, dict[str, Any]] = {}

        # 1. Directory & index health
        root_exists = self.root_dir.is_dir()
        matches_exists = self.matches_dir.is_dir()
        reports_exists = self.reports_dir.is_dir()
        index_exists = self.index_path.is_file()
        index_valid = False
        index_dirs: set[str] = set()
        if index_exists:
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(raw, dict) and isinstance(raw.get("matches"), list):
                    index_valid = True
                    for item in raw["matches"]:
                        if isinstance(item, dict) and item.get("dir_name"):
                            index_dirs.add(str(item["dir_name"]))
            except Exception:
                index_valid = False

        if not root_exists:
            checks["directory"] = {
                "status": "warn",
                "message": f"DevLab directory does not exist yet: {self.root_dir}",
                "root_exists": False,
                "matches_exists": False,
                "reports_exists": False,
                "index_exists": False,
            }
        elif index_exists and not index_valid:
            checks["directory"] = {
                "status": "fail",
                "message": "index.json exists but is corrupt JSON",
                "root_exists": True,
                "matches_exists": matches_exists,
                "reports_exists": reports_exists,
                "index_exists": True,
            }
        else:
            checks["directory"] = {
                "status": "pass",
                "message": "DevLab root layout healthy",
                "root_exists": True,
                "matches_exists": matches_exists,
                "reports_exists": reports_exists,
                "index_exists": index_exists,
            }

        # 2. Index consistency & JSONL integrity
        disk_dirs: set[str] = set()
        if matches_exists:
            try:
                disk_dirs = {p.name for p in self.matches_dir.iterdir() if p.is_dir()}
            except OSError:
                disk_dirs = set()

        missing_on_disk = sorted(index_dirs - disk_dirs)
        orphan_on_disk = sorted(disk_dirs - index_dirs) if index_valid else []

        matches = self.list_matches()
        recent_matches = matches[-max_scan_matches:] if max_scan_matches > 0 else matches
        corrupt_event_lines = 0
        total_dropped_events = 0
        total_dropped_roi = 0
        total_roi_capped = 0

        for m in recent_matches:
            mdir = Path(m["match_dir"])
            meta, evs = self.load_match_events(mdir)
            corrupt_event_lines += _safe_int(meta.get("_corrupt_lines"), 0)
            m_drop_ev = _safe_int(meta.get("dropped_events"), 0)
            m_drop_roi = _safe_int(meta.get("dropped_roi"), 0)
            m_roi_capped = _safe_int(meta.get("roi_capped_count"), 0)
            for ev in evs:
                if ev.get("event_type") == EV_WRITER_HEALTH:
                    pl = ev.get("payload") or {}
                    m_drop_ev = max(m_drop_ev, _safe_int(pl.get("dropped_events"), 0))
                    if "roi_capped_count" in pl:
                        m_drop_roi = max(m_drop_roi, _safe_int(pl.get("dropped_roi"), 0))
                        m_roi_capped = max(m_roi_capped, _safe_int(pl.get("roi_capped_count"), 0))
                    else:
                        pl_drop_roi = _safe_int(pl.get("dropped_roi"), 0)
                        pl_roi_cnt = _safe_int(pl.get("roi_count"), 0)
                        if pl_drop_roi > 0 and _safe_int(meta.get("dropped_roi"), 0) == 0 and pl_roi_cnt >= 15:
                            m_roi_capped = max(m_roi_capped, pl_drop_roi)
                        else:
                            m_drop_roi = max(m_drop_roi, pl_drop_roi)
            total_dropped_events += m_drop_ev
            total_dropped_roi += m_drop_roi
            total_roi_capped += m_roi_capped

        if missing_on_disk or orphan_on_disk or corrupt_event_lines > 0:
            checks["index_consistency"] = {
                "status": "warn",
                "message": (
                    f"missing_dirs={len(missing_on_disk)}, orphan_dirs={len(orphan_on_disk)}, "
                    f"corrupt_jsonl_lines={corrupt_event_lines}"
                ),
                "match_count": len(matches),
                "missing_dirs": missing_on_disk[:20],
                "orphan_dirs": orphan_on_disk[:20],
                "corrupt_event_lines": corrupt_event_lines,
            }
        else:
            checks["index_consistency"] = {
                "status": "pass",
                "message": f"{len(matches)} match(es) consistent with index",
                "match_count": len(matches),
                "missing_dirs": [],
                "orphan_dirs": [],
                "corrupt_event_lines": 0,
            }

        # 3. Disk & ROI quota check
        total_bytes = _dir_size_bytes(self.root_dir) if root_exists else 0
        roi_bytes = 0
        if matches_exists:
            for m in matches:
                rdir = Path(m["match_dir"]) / "roi_snapshots"
                if rdir.is_dir():
                    roi_bytes += _dir_size_bytes(rdir)

        total_pct = total_bytes / self.max_total_bytes if self.max_total_bytes > 0 else 0.0
        roi_pct = roi_bytes / self.max_roi_bytes if self.max_roi_bytes > 0 else 0.0
        if total_bytes > self.max_total_bytes or roi_bytes > self.max_roi_bytes:
            quota_status = "fail"
        elif total_pct >= 0.85 or roi_pct >= 0.85 or len(matches) > self.max_matches:
            quota_status = "warn"
        else:
            quota_status = "pass"
        checks["disk_quotas"] = {
            "status": quota_status,
            "message": (
                f"total={total_bytes / (1024 * 1024):.2f}MB/{self.max_total_bytes / (1024 * 1024):.0f}MB, "
                f"roi={roi_bytes / (1024 * 1024):.2f}MB/{self.max_roi_bytes / (1024 * 1024):.0f}MB, "
                f"matches={len(matches)}/{self.max_matches}"
            ),
            "total_bytes": total_bytes,
            "max_total_bytes": self.max_total_bytes,
            "roi_bytes": roi_bytes,
            "max_roi_bytes": self.max_roi_bytes,
            "match_count": len(matches),
            "max_matches": self.max_matches,
        }

        # 4. PII & credential leak scan across recent events.jsonl & debug_runtime.log + idle files
        pii_findings = self._scan_pii_leaks(recent_matches)
        checks["pii_scan"] = {
            "status": "fail" if pii_findings else "pass",
            "message": (
                f"Detected {len(pii_findings)} potential PII/token leak(s)"
                if pii_findings
                else "Zero PII/token leaks found in scanned events and debug logs"
            ),
            "scanned_matches": len(recent_matches),
            "leak_count": len(pii_findings),
            "findings": pii_findings[:25],
        }

        # 5. Queue drop health (real queue/IO failures only; policy roi_capped_count is informational)
        total_drops = total_dropped_events + total_dropped_roi
        if total_drops > 20:
            drop_status = "fail"
        elif total_drops > 0:
            drop_status = "warn"
        else:
            drop_status = "pass"
        checks["queue_drops"] = {
            "status": drop_status,
            "message": (
                f"dropped_events={total_dropped_events}, dropped_roi={total_dropped_roi}, "
                f"roi_capped={total_roi_capped}"
            ),
            "dropped_events": total_dropped_events,
            "dropped_roi": total_dropped_roi,
            "roi_capped_count": total_roi_capped,
        }

        statuses = [c["status"] for c in checks.values()]
        if "fail" in statuses:
            overall = "fail"
        elif "warn" in statuses:
            overall = "warn"
        else:
            overall = "pass"

        return {
            "status": overall,
            "checked_at": _utc_now_iso(),
            "root_dir": str(self.root_dir),
            "checks": checks,
        }

    def _scan_pii_leaks(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        max_findings = 50

        jsonl_files: list[tuple[str, Path]] = []
        text_files: list[tuple[str, Path]] = []
        if self.idle_events_path.is_file():
            jsonl_files.append(("session_idle_events.jsonl", self.idle_events_path))
        if self.idle_debug_path.is_file():
            text_files.append(("session_idle_debug.log", self.idle_debug_path))
        for m in matches:
            mdir = Path(m["match_dir"])
            dname = str(m.get("dir_name") or mdir.name)
            evp = mdir / "events.jsonl"
            if evp.is_file():
                jsonl_files.append((f"{dname}/events.jsonl", evp))
            dbgp = mdir / "debug_runtime.log"
            if dbgp.is_file():
                text_files.append((f"{dname}/debug_runtime.log", dbgp))

        for rel, path in jsonl_files:
            if len(findings) >= max_findings:
                break
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for lineno, raw_line in enumerate(fh, start=1):
                        if len(findings) >= max_findings:
                            break
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            # Fallback raw scan for malformed line
                            for pat in self._match_text_pii_patterns(line):
                                findings.append({"file": rel, "line": lineno, "pattern": pat})
                            continue
                        if isinstance(obj, dict):
                            self._scan_json_obj_for_pii(obj, rel, lineno, findings, max_findings)
            except OSError:
                continue

        for rel, path in text_files:
            if len(findings) >= max_findings:
                break
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for lineno, raw_line in enumerate(fh, start=1):
                        if len(findings) >= max_findings:
                            break
                        for pat in self._match_text_pii_patterns(raw_line):
                            findings.append({"file": rel, "line": lineno, "pattern": pat})
                            if len(findings) >= max_findings:
                                break
            except OSError:
                continue

        return findings

    def _scan_json_obj_for_pii(
        self,
        obj: Any,
        rel_file: str,
        lineno: int,
        findings: list[dict[str, Any]],
        max_findings: int,
        *,
        parent_key: str = "",
    ) -> None:
        if len(findings) >= max_findings:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if len(findings) >= max_findings:
                    return
                k_str = str(k)
                compact = k_str.strip().lower().replace("_", "").replace("-", "")
                if compact in _PII_COMPACT_BLACKLIST:
                    findings.append({"file": rel_file, "line": lineno, "pattern": f"blacklisted_key:{k_str}"})
                    continue
                self._scan_json_obj_for_pii(v, rel_file, lineno, findings, max_findings, parent_key=k_str)
        elif isinstance(obj, list):
            for item in obj:
                if len(findings) >= max_findings:
                    return
                self._scan_json_obj_for_pii(item, rel_file, lineno, findings, max_findings, parent_key=parent_key)
        elif isinstance(obj, str):
            skip_uuid = parent_key.strip().lower() in _INTERNAL_UUID_FIELDS
            for pat in self._match_text_pii_patterns(obj, skip_uuid=skip_uuid):
                findings.append({"file": rel_file, "line": lineno, "pattern": pat})
                if len(findings) >= max_findings:
                    return

    def _match_text_pii_patterns(self, text: str, *, skip_uuid: bool = False) -> list[str]:
        if not text:
            return []
        hits: list[str] = []
        if _GEMINI_KEY_RE.search(text):
            hits.append("gemini_api_key")
        if _OPENAI_DS_KEY_RE.search(text):
            hits.append("sk_api_key")
        if _COS_KEY_RE.search(text):
            hits.append("cos_secret_id")
        if _JWT_RE.search(text):
            hits.append("jwt_token")
        for m_bearer in _BEARER_RE.finditer(text):
            if "<REDACTED>" not in m_bearer.group(0):
                hits.append("bearer_token")
                break
        for m_ra in _RIOT_AUTH_RE.finditer(text):
            if "<REDACTED>" not in m_ra.group(2):
                hits.append("riot_lcu_auth")
                break
        for m_rt in _REMOTING_TOKEN_RE.finditer(text):
            if "<REDACTED>" not in m_rt.group(2):
                hits.append("remoting_auth_token")
                break
        for m_auth in _AUTH_KV_RE.finditer(text):
            if "<REDACTED>" not in m_auth.group(3):
                hits.append("auth_kv_secret")
                break
        riot_kv_hit = False
        for m_rq in _RIOT_KV_QUOTED_RE.finditer(text):
            if "<REDACTED>" not in m_rq.group(3):
                riot_kv_hit = True
                break
        if not riot_kv_hit:
            for m_rk in _RIOT_KV_RE.finditer(text):
                if "<REDACTED>" not in m_rk.group(3):
                    riot_kv_hit = True
                    break
        if riot_kv_hit:
            hits.append("riot_pii_kv")

        if not skip_uuid and _RIOT_PUUID_RE.search(text):
            hits.append("riot_puuid")

        for m_rid in _RIOT_ID_RE.finditer(text):
            if _redact_riot_id_match(m_rid) == "<RIOT_ID_REDACTED>":
                hits.append("riot_id")
                break

        for m_win in _WIN_USER_PATH_RE.finditer(text):
            if m_win.group(2) != "<USER>":
                hits.append("windows_user_path")
                break

        return hits


__all__ = [
    "DEFAULT_MAX_MATCHES",
    "DEFAULT_MAX_ROI_BYTES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DetectedIssue",
    "DevLabAnalyzer",
    "EV_AUGMENT_UNLOCK_SILENT_MISS",
    "EvolutionReport",
    "MatchReplayResult",
    "VersionDiffReport",
    "run_issue_detectors",
]
