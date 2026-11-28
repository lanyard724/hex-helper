"""Structured event envelope contract, domain/event constants, and PII scrubber for DevLab."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from hex_helper.logging_ring import sanitize_log_text

EVENT_SCHEMA_VERSION = 1

# Event domains
DOMAIN_GAME = "game"
DOMAIN_REC = "rec"
DOMAIN_VISION = "vision"
DOMAIN_UI = "ui"
DOMAIN_COACH = "coach"
DOMAIN_PERF = "perf"
DOMAIN_SYSTEM = "system"

VALID_DOMAINS: frozenset[str] = frozenset(
    {
        DOMAIN_GAME,
        DOMAIN_REC,
        DOMAIN_VISION,
        DOMAIN_UI,
        DOMAIN_COACH,
        DOMAIN_PERF,
        DOMAIN_SYSTEM,
    }
)

# Standardized event_type constants across all 5 evolution pillars
EV_GAMEFLOW_TRANSITION = "gameflow_phase_transition"
EV_CHAMP_SELECT_REC = "champ_select_recommendation"
EV_CHAMP_SELECT_ACTION = "champ_select_action"
EV_CHAMP_SELECT_OUTCOME = "champ_select_outcome_delta"
EV_AUGMENT_OFFER_REC = "augment_offer_recommendation"
EV_AUGMENT_REROLL_DELTA = "augment_reroll_delta"
EV_AUGMENT_PICK_DELTA = "augment_pick_decision_delta"
EV_LOCAL_BUILD_REC = "local_build_recommendation_snapshot"
EV_ITEM_TX = "item_transaction"
EV_ITEM_PURCHASE_DELTA = "item_purchase_and_plan_override_delta"
EV_ENEMY_ITEM_MILESTONE = "enemy_item_milestone"
EV_LIVE_GAME_EVENT = "live_game_event"
EV_DEATH_SHOP_WINDOW = "death_shop_window"
EV_LEVEL_MILESTONE = "level_and_skill_milestone"
EV_LOBBY_SCOUT = "lobby_scout_completed"
EV_RUNES_LOCKED = "match_runes_and_loadout_locked"
EV_MATCH_EOG = "match_eog_summary"

EV_VISION_CAPTURE_STATE = "vision.capture.state_change"
EV_VISION_PICKER_TRANSITION = "vision.picker.transition"
EV_VISION_OCR_ANOMALY = "vision.ocr.low_conf_or_nearmiss"
EV_VISION_CLICK_RESOLVED = "vision.click.selection_resolved"
EV_VISION_ICON_VERIFY = "vision.icon_verify.outcome"
EV_VISION_TRANSMUTE_TRACE = "vision.transmute.channel_trace"
EV_VISION_USER_CORRECTION = "vision.pick.user_correction"
EV_VISION_LATENCY_SPIKE = "vision.pipeline.latency_spike"

EV_UI_OVERLAY_SESSION = "ui_overlay_visibility_session"
EV_UI_LAYOUT_ADJUSTED = "ui_overlay_layout_adjusted"
EV_UI_SUBCARD = "ui_subcard_interact_or_dismiss"
EV_UI_SCROLL_OVERFLOW = "ui_scroll_overflow_session"
EV_UI_HOTKEY_TRAY = "ui_hotkey_and_tray_dispatch"
EV_UI_MANUAL_TRIGGER = "ui_manual_coach_or_scan_trigger"
EV_UI_CALIBRATION = "ui_calibration_attempt"
EV_UI_LOOKUP = "ui_lookup_navigation_and_search"
EV_UI_RECAP = "ui_match_recap_interaction"

EV_COACH_TURN = "ai_coach_turn_telemetry"
EV_SMART_PICKER_GATE = "smart_picker_gate_decision"
EV_MAIN_THREAD_STALL = "ui_main_thread_stall_summary"
EV_WRITER_HEALTH = "devlab_writer_health"

PII_KEY_BLACKLIST: frozenset[str] = frozenset(
    {
        "summonername",
        "riotid",
        "riotidgamename",
        "riotidtagline",
        "puuid",
        "summonerid",
        "accountid",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "gemini_api_key",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "remoting_auth_token",
        "chat_text",
        "raw_chat",
        "local_path",
        "file_path",
        "save_path",
        "contact",
        "email",
        "phone",
    }
)

_PII_COMPACT_KEYS: frozenset[str] = frozenset(
    k.lower().replace("_", "").replace("-", "") for k in PII_KEY_BLACKLIST
)

_MAX_LIST_ITEMS = 128


def _is_blacklisted_key(key: Any) -> bool:
    norm = str(key).strip().lower()
    if norm in PII_KEY_BLACKLIST:
        return True
    if norm.replace("-", "_") in PII_KEY_BLACKLIST:
        return True
    compact = norm.replace("_", "").replace("-", "")
    return compact in _PII_COMPACT_KEYS


def _scrub_str(text: str, max_str_len: int) -> str:
    cleaned = sanitize_log_text(text)
    if max_str_len > 0 and len(cleaned) > max_str_len:
        if max_str_len > 1:
            return cleaned[: max_str_len - 1] + "…"
        return cleaned[:max_str_len]
    return cleaned


def _scrub_value(val: Any, *, max_str_len: int, max_depth: int, depth: int) -> Any:
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, str):
        return _scrub_str(val, max_str_len)
    if isinstance(val, (Path, os.PathLike)):
        return _scrub_str(os.fspath(val), max_str_len)
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, bytes):
        return _scrub_str(val.decode("utf-8", errors="replace"), max_str_len)

    # Support numpy scalars without importing numpy at module level
    if hasattr(val, "item") and callable(val.item) and getattr(val, "ndim", 0) == 0:
        try:
            return _scrub_value(val.item(), max_str_len=max_str_len, max_depth=max_depth, depth=depth)
        except Exception:
            pass

    if depth >= max_depth:
        if isinstance(val, Mapping):
            return {"_truncated": True, "keys": len(val)}
        if isinstance(val, (list, tuple, set, frozenset)):
            return f"<truncated_seq len={len(val)}>"
        return _scrub_str(str(val), max_str_len)

    if isinstance(val, Mapping):
        out: dict[str, Any] = {}
        for k, v in val.items():
            if _is_blacklisted_key(k):
                continue
            sk = _scrub_str(str(k), max_str_len)
            out[sk] = _scrub_value(v, max_str_len=max_str_len, max_depth=max_depth, depth=depth + 1)
        return out

    if isinstance(val, (list, tuple, set, frozenset)):
        items = list(val)[:_MAX_LIST_ITEMS]
        return [
            _scrub_value(item, max_str_len=max_str_len, max_depth=max_depth, depth=depth + 1)
            for item in items
        ]

    # Small numpy ndarray fallback
    if hasattr(val, "tolist") and callable(val.tolist) and hasattr(val, "size"):
        try:
            if int(val.size) <= _MAX_LIST_ITEMS:
                return _scrub_value(val.tolist(), max_str_len=max_str_len, max_depth=max_depth, depth=depth + 1)
            return f"<ndarray shape={getattr(val, 'shape', '?')}>"
        except Exception:
            pass

    return _scrub_str(str(val), max_str_len)


def scrub_payload(
    payload: dict[str, Any] | None,
    *,
    max_str_len: int = 320,
    max_depth: int = 6,
) -> dict[str, Any]:
    """Recursively strip PII keys, sanitize strings, cap lengths, and coerce primitives."""
    if not payload or not isinstance(payload, Mapping):
        return {}
    cleaned = _scrub_value(payload, max_str_len=max_str_len, max_depth=max_depth, depth=0)
    return cleaned if isinstance(cleaned, dict) else {}


def hash_match_key(raw_key: str) -> str:
    """Return a 16-char SHA-256 hex digest of a raw match key / gameId."""
    text = (raw_key or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass(slots=True)
class DevLabEvent:
    """Canonical 14-field DevLab Flight Recorder event envelope."""

    schema_v: int
    ev_seq: int
    ts_utc: str
    ts_mono_ms: int
    game_time_s: float | None
    match_session_id: str
    match_key_hash: str
    app_version: str
    domain: str
    event_type: str
    game_state: str
    hero_id: str
    dataset_tag: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_v": self.schema_v,
            "ev_seq": self.ev_seq,
            "ts_utc": self.ts_utc,
            "ts_mono_ms": self.ts_mono_ms,
            "game_time_s": self.game_time_s,
            "match_session_id": self.match_session_id,
            "match_key_hash": self.match_key_hash,
            "app_version": self.app_version,
            "domain": self.domain,
            "event_type": self.event_type,
            "game_state": self.game_state,
            "hero_id": self.hero_id,
            "dataset_tag": self.dataset_tag,
            "payload": self.payload,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
