"""Zero-blocking background Flight Recorder engine for local DevLab self-evolution."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
from typing import Any
import uuid

from hex_helper.config import get_devlab_dir
from hex_helper.devlab.schema import (
    DOMAIN_PERF,
    DOMAIN_SYSTEM,
    DOMAIN_VISION,
    EV_MAIN_THREAD_STALL,
    EV_MATCH_EOG,
    EV_WRITER_HEALTH,
    EVENT_SCHEMA_VERSION,
    DevLabEvent,
    scrub_payload,
)
from hex_helper.edition import get_dev_build_tag
from hex_helper.logging_ring import sanitize_log_text

_UNSET = object()

_FLUSH_INTERVAL_S = 0.25
_FLUSH_BATCH_SIZE = 32
_LATENCY_FLUSH_INTERVAL_S = 60.0
_META_FLUSH_INTERVAL_S = 60.0
_MAX_LATENCY_SAMPLES_PER_OP = 2048

_IDLE_EVENTS_MAX_BYTES = 2 * 1024 * 1024
_IDLE_DEBUG_MAX_BYTES = 1 * 1024 * 1024
_MAX_ROI_SINGLE_BYTES = 64 * 1024
_MAX_ROI_WIDTH = 960
_MAX_ROI_HEIGHT = 540
_EOG_OVERFLOW_GRACE_BYTES = 16 * 1024

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]+")
_DEBUG_TRIM_MARKER = b"\n[... middle lines trimmed to respect DevLab debug_runtime.log cap ...]\n"
_BACKGROUND_PERF_OP_PREFIXES: tuple[str, ...] = ("vision_worker",)


def _is_background_perf_op(op_name: str) -> bool:
    """Return True if a latency metric op belongs to a background worker thread (not Qt main thread)."""
    low = str(op_name or "").strip().lower()
    return any(low.startswith(pfx) for pfx in _BACKGROUND_PERF_OP_PREFIXES)


def _utc_iso_ms(ts_epoch: float | None = None) -> str:
    dt = (
        datetime.fromtimestamp(ts_epoch, timezone.utc)
        if ts_epoch is not None
        else datetime.now(timezone.utc)
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _utc_file_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _safe_slug(raw: str, fallback: str = "x", max_len: int = 32) -> str:
    cleaned = _SAFE_TOKEN_RE.sub("_", (raw or "").strip()).strip("_")
    if not cleaned:
        return fallback
    return cleaned[:max_len]


def _atomic_write_json(path: Path, data: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)
    return len(raw)


def _trim_head_tail_bytes(path: Path, max_bytes: int) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if len(data) <= max_bytes:
        return len(data)
    head_budget = min(128 * 1024, max(2048, max_bytes // 4))
    tail_budget = max(2048, max_bytes - head_budget - len(_DEBUG_TRIM_MARKER))
    head = data[:head_budget]
    nl_h = head.rfind(b"\n")
    if nl_h > 0:
        head = head[: nl_h + 1]
    tail = data[-tail_budget:]
    nl_t = tail.find(b"\n")
    if 0 <= nl_t < len(tail) - 1:
        tail = tail[nl_t + 1 :]
    compacted = head + _DEBUG_TRIM_MARKER + tail
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(compacted)
        os.replace(tmp, path)
        return len(compacted)
    except OSError:
        return len(data)


def _trim_tail_jsonl(path: Path, max_bytes: int) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if len(data) <= max_bytes:
        return len(data)
    keep = max(4096, int(max_bytes * 0.75))
    tail = data[-keep:]
    nl = tail.find(b"\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1 :]
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(tail)
        os.replace(tmp, path)
        return len(tail)
    except OSError:
        return len(data)


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


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * pct
    lo = int(idx)
    hi = min(len(sorted_vals) - 1, lo + 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _encode_roi_png(bgr_crop: Any, max_bytes: int = _MAX_ROI_SINGLE_BYTES) -> bytes | None:
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        img = bgr_crop
        ok, buf = cv2.imencode(".png", img, [int(cv2.IMWRITE_PNG_COMPRESSION), 6])
        if not ok:
            return None
        raw = buf.tobytes()
        if len(raw) <= max_bytes:
            return raw

        # Iteratively downscale until <= max_bytes
        for _ in range(4):
            h, w = img.shape[:2]
            nw = max(16, int(round(w * 0.75)))
            nh = max(16, int(round(h * 0.75)))
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".png", img, [int(cv2.IMWRITE_PNG_COMPRESSION), 9])
            if not ok:
                return None
            raw = buf.tobytes()
            if len(raw) <= max_bytes:
                return raw
        return None
    except Exception:
        return None


class DevLabDebugLogHandler(logging.Handler):
    """Isolated DEBUG+ log handler attached ONLY to ``logging.getLogger('hex_helper')``.

    Forwards formatted records through the recorder's background writer thread into
    the active match's ``debug_runtime.log`` (or ``session_idle_debug.log`` when idle).
    Never writes to root logger or ``MatchLogRing``.
    """

    def __init__(self, recorder: DevLabFlightRecorder, level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self._recorder = recorder
        self._tls = threading.local()
        self.setFormatter(
            logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._tls, "in_emit", False):
            return
        self._tls.in_emit = True
        try:
            msg = self.format(record)
            self._recorder._enqueue_debug_log(msg)
        except Exception:
            pass
        finally:
            self._tls.in_emit = False


class DevLabFlightRecorder:
    """Async flight recorder with strict disk quotas and <15us main-thread emit path."""

    def __init__(
        self,
        root_dir: Path | None = None,
        *,
        enable_roi_snapshots: bool = False,
        enable_debug_log: bool = False,
        max_matches: int = 120,
        max_total_bytes: int = 250 * 1024 * 1024,
        max_roi_bytes: int = 30 * 1024 * 1024,
        max_match_events_bytes: int = 2 * 1024 * 1024,
        max_match_debug_bytes: int = 1 * 1024 * 1024,
        max_roi_per_match: int = 15,
        event_queue_max: int = 4096,
        roi_queue_max: int = 16,
        app_version: str | None = None,
    ) -> None:
        self.root_dir = Path(root_dir) if root_dir is not None else get_devlab_dir()
        self.matches_dir = self.root_dir / "matches"
        self.reports_dir = self.root_dir / "reports"
        self.index_path = self.root_dir / "index.json"
        self.idle_events_path = self.root_dir / "session_idle_events.jsonl"
        self.idle_debug_path = self.root_dir / "session_idle_debug.log"

        self.enable_roi_snapshots = bool(enable_roi_snapshots)
        self.enable_debug_log = bool(enable_debug_log)
        self.max_matches = max(1, int(max_matches))
        self.max_total_bytes = max(64 * 1024, int(max_total_bytes))
        self.max_roi_bytes = max(16 * 1024, int(max_roi_bytes))
        self.max_match_events_bytes = max(8 * 1024, int(max_match_events_bytes))
        self.max_match_debug_bytes = max(8 * 1024, int(max_match_debug_bytes))
        self.max_roi_per_match = max(0, int(max_roi_per_match))
        self.event_queue_max = max(64, int(event_queue_max))
        self.roi_queue_max = max(1, int(roi_queue_max))
        self.app_version = (app_version or "").strip() or get_dev_build_tag()

        self._event_q: queue.SimpleQueue[tuple[Any, ...]] = queue.SimpleQueue()
        self._roi_q: queue.Queue[tuple[str, str, str, Any]] = queue.Queue(maxsize=self.roi_queue_max)

        self._state_lock = threading.Lock()
        self._ring_lock = threading.Lock()
        self._latency_lock = threading.Lock()
        self._stats_lock = threading.Lock()

        # Main-thread hot-path context & counters
        self._closed = False
        self._started = False
        self._next_match_seq = 1
        self._active_match_seq: int | None = None
        self._active_session_id = ""
        self._active_match_dir_name = ""
        self._active_phase = "idle"  # "idle" | "champ_select" | "in_game"
        self._session_start_mono = time.monotonic()
        self._ev_seq = 0
        self._idle_ev_seq = 0
        self._roi_count_in_match = 0

        self._ctx_game_state = "IDLE"
        self._ctx_hero_id = ""
        self._ctx_dataset_tag = ""
        self._ctx_game_time_s: float | None = None
        self._ctx_match_key_hash = ""

        # Health counters (per-match, reset cleanly on match boundaries)
        self._dropped_events = 0
        self._dropped_roi = 0
        self._roi_capped_count = 0
        self._match_queue_dropped_events = 0
        self._match_queue_dropped_roi = 0
        self._max_queue_depth = 0
        self._total_events_written = 0
        self._disk_bytes_cached = 0
        self._roi_disk_bytes_cached = 0
        self._match_count_cached = 0
        self._meta_flush_interval_s = _META_FLUSH_INTERVAL_S

        # In-memory ring buffers (populated by writer thread with scrubbed dicts)
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=50)
        self._recent_vision_traces: deque[dict[str, Any]] = deque(maxlen=30)

        # 60s latency buckets
        self._latency_buckets: dict[str, list[float]] = {}
        self._last_latency_flush_mono = time.monotonic()

        # Writer-thread owned state
        self._writer_thread: threading.Thread | None = None
        self._writer_index_matches: list[dict[str, Any]] = []
        self._writer_match_metas: dict[str, dict[str, Any]] = {}
        self._writer_active_dir: str = ""
        self._writer_active_session: str = ""

        # Isolated debug logging handler
        self._debug_handler: DevLabDebugLogHandler | None = None
        self._prev_hex_logger_level: int | None = None

        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.matches_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self._load_initial_index()
        self.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the single background writer thread (idempotent)."""
        with self._state_lock:
            if self._closed or self._started:
                return
            self._started = True
            t = threading.Thread(target=self._writer_loop, name="HexDevLabWriter", daemon=True)
            self._writer_thread = t
        t.start()
        if self.enable_debug_log:
            self.install_debug_log_handler()

    def install_debug_log_handler(self) -> None:
        """Attach isolated DevLabDebugLogHandler strictly to ``hex_helper`` logger."""
        if self._debug_handler is not None:
            return
        hex_logger = logging.getLogger("hex_helper")
        self._prev_hex_logger_level = hex_logger.level
        if hex_logger.level == logging.NOTSET or hex_logger.level > logging.DEBUG:
            hex_logger.setLevel(logging.DEBUG)
        handler = DevLabDebugLogHandler(self, level=logging.DEBUG)
        hex_logger.addHandler(handler)
        self._debug_handler = handler
        self.enable_debug_log = True

    def uninstall_debug_log_handler(self) -> None:
        """Detach isolated DevLabDebugLogHandler from ``hex_helper`` logger."""
        handler = self._debug_handler
        if handler is None:
            return
        self._debug_handler = None
        hex_logger = logging.getLogger("hex_helper")
        try:
            hex_logger.removeHandler(handler)
        except Exception:
            pass
        if self._prev_hex_logger_level is not None:
            try:
                hex_logger.setLevel(self._prev_hex_logger_level)
            except Exception:
                pass
            self._prev_hex_logger_level = None

    def set_roi_snapshots_enabled(self, enabled: bool) -> None:
        """Toggle bounded anomaly ROI snapshot capture live (e.g. from tray menu)."""
        self.enable_roi_snapshots = bool(enabled)

    def set_context(
        self,
        *,
        game_state: str | object = _UNSET,
        hero_id: str | object = _UNSET,
        dataset_tag: str | object = _UNSET,
        game_time_s: float | None | object = _UNSET,
    ) -> None:
        """Update default envelope context fields used by subsequent ``emit()`` calls."""
        with self._state_lock:
            if game_state is not _UNSET and game_state is not None:
                self._ctx_game_state = str(game_state)
            if hero_id is not _UNSET and hero_id is not None:
                self._ctx_hero_id = str(hero_id)
            if dataset_tag is not _UNSET and dataset_tag is not None:
                self._ctx_dataset_tag = str(dataset_tag)
            if game_time_s is not _UNSET:
                self._ctx_game_time_s = None if game_time_s is None else float(game_time_s)

    def begin_champ_select(
        self,
        hero_id: str = "",
        dataset_tag: str = "",
        match_key_hash: str = "",
    ) -> str:
        """Begin (or reuse) a match session starting in champion select."""
        clean_hero = str(hero_id or "").strip()
        clean_tag = str(dataset_tag or "").strip()
        clean_mkh = str(match_key_hash or "").strip()

        with self._state_lock:
            if self._closed:
                return ""
            if (
                self._active_phase == "champ_select"
                and self._active_session_id
                and (not clean_mkh or clean_mkh == self._ctx_match_key_hash)
            ):
                if clean_hero:
                    self._ctx_hero_id = clean_hero
                if clean_tag:
                    self._ctx_dataset_tag = clean_tag
                if clean_mkh:
                    self._ctx_match_key_hash = clean_mkh
                self._ctx_game_state = "CHAMP_SELECT"
                session_id = self._active_session_id
                updates = {
                    k: v
                    for k, v in (
                        ("hero_id", clean_hero),
                        ("dataset_tag", clean_tag),
                        ("match_key_hash", clean_mkh),
                    )
                    if v
                }
                if updates:
                    self._event_q.put_nowait(("update_match_meta", session_id, updates))
                return session_id

            prev_session = self._active_session_id if self._active_phase != "idle" else ""
            prev_end_stats = {
                "roi_capped_count": self._roi_capped_count,
                "queue_dropped_roi": self._match_queue_dropped_roi,
                "queue_dropped_events": self._match_queue_dropped_events,
            }

            seq = self._next_match_seq
            self._next_match_seq += 1
            session_id = str(uuid.uuid4())
            slug = _safe_slug(clean_hero, fallback="cs", max_len=24) if clean_hero else "cs"
            dir_name = f"m_{seq:05d}_{_utc_file_stamp()}_{slug}"

            self._active_match_seq = seq
            self._active_session_id = session_id
            self._active_match_dir_name = dir_name
            self._active_phase = "champ_select"
            self._session_start_mono = time.monotonic()
            self._ev_seq = 0
            self._roi_count_in_match = 0
            self._roi_capped_count = 0
            self._dropped_events = 0
            self._dropped_roi = 0
            self._match_queue_dropped_events = 0
            self._match_queue_dropped_roi = 0
            self._max_queue_depth = 0

            self._ctx_game_state = "CHAMP_SELECT"
            self._ctx_hero_id = clean_hero
            if clean_tag:
                self._ctx_dataset_tag = clean_tag
            self._ctx_game_time_s = None
            self._ctx_match_key_hash = clean_mkh

            meta = self._build_initial_meta(
                seq=seq,
                dir_name=dir_name,
                session_id=session_id,
                match_key_hash=clean_mkh,
                phase="champ_select",
                hero_id=clean_hero,
                hero_name="",
                dataset_tag=self._ctx_dataset_tag,
                game_mode="",
                is_non_mayhem=False,
            )
            if prev_session:
                self._event_q.put_nowait(
                    (
                        "end_match",
                        prev_session,
                        {
                            "won": -1,
                            "duration_s": 0,
                            "eog_source": "superseded",
                            "ended_at": _utc_iso_ms(),
                            **prev_end_stats,
                        },
                    )
                )
            self._event_q.put_nowait(("begin_match", meta))
        return session_id

    def begin_match(
        self,
        match_key_hash: str = "",
        hero_id: str = "",
        hero_name: str = "",
        dataset_tag: str = "",
        game_mode: str = "",
        is_non_mayhem: bool = False,
    ) -> str:
        """Start an IN_GAME match session, upgrading an active champ_select session in-place."""
        clean_mkh = str(match_key_hash or "").strip()
        clean_hero = str(hero_id or "").strip()
        clean_hero_name = str(hero_name or "").strip()
        clean_tag = str(dataset_tag or "").strip()
        clean_mode = str(game_mode or "").strip()

        with self._state_lock:
            if self._closed:
                return ""

            # Smooth upgrade from champ_select -> in_game reuses SAME session_id and directory
            if self._active_phase == "champ_select" and self._active_session_id:
                self._active_phase = "in_game"
                self._ctx_game_state = "IN_GAME"
                if clean_hero:
                    self._ctx_hero_id = clean_hero
                if clean_tag:
                    self._ctx_dataset_tag = clean_tag
                if clean_mkh:
                    self._ctx_match_key_hash = clean_mkh
                session_id = self._active_session_id
                updates: dict[str, Any] = {
                    "phase": "in_game",
                    "game_started_at": _utc_iso_ms(),
                    "is_non_mayhem": bool(is_non_mayhem),
                }
                if clean_mkh:
                    updates["match_key_hash"] = clean_mkh
                if clean_hero:
                    updates["hero_id"] = clean_hero
                if clean_hero_name:
                    updates["hero_name"] = clean_hero_name
                if clean_tag:
                    updates["dataset_tag"] = clean_tag
                if clean_mode:
                    updates["game_mode"] = clean_mode
                self._event_q.put_nowait(("upgrade_match", session_id, updates))
                return session_id

            # Re-attach if already in_game for the same match_key_hash
            if (
                self._active_phase == "in_game"
                and self._active_session_id
                and clean_mkh
                and clean_mkh == self._ctx_match_key_hash
            ):
                if clean_hero:
                    self._ctx_hero_id = clean_hero
                if clean_tag:
                    self._ctx_dataset_tag = clean_tag
                session_id = self._active_session_id
                updates = {
                    k: v
                    for k, v in (
                        ("hero_id", clean_hero),
                        ("hero_name", clean_hero_name),
                        ("dataset_tag", clean_tag),
                        ("game_mode", clean_mode),
                    )
                    if v
                }
                if is_non_mayhem:
                    updates["is_non_mayhem"] = True
                if updates:
                    self._event_q.put_nowait(("update_match_meta", session_id, updates))
                return session_id

            prev_session = self._active_session_id if self._active_phase != "idle" else ""
            prev_end_stats = {
                "roi_capped_count": self._roi_capped_count,
                "queue_dropped_roi": self._match_queue_dropped_roi,
                "queue_dropped_events": self._match_queue_dropped_events,
            }

            seq = self._next_match_seq
            self._next_match_seq += 1
            session_id = str(uuid.uuid4())
            slug = _safe_slug(clean_hero, fallback="unknown", max_len=24)
            dir_name = f"m_{seq:05d}_{_utc_file_stamp()}_{slug}"

            self._active_match_seq = seq
            self._active_session_id = session_id
            self._active_match_dir_name = dir_name
            self._active_phase = "in_game"
            self._session_start_mono = time.monotonic()
            self._ev_seq = 0
            self._roi_count_in_match = 0
            self._roi_capped_count = 0
            self._dropped_events = 0
            self._dropped_roi = 0
            self._match_queue_dropped_events = 0
            self._match_queue_dropped_roi = 0
            self._max_queue_depth = 0

            self._ctx_game_state = "IN_GAME"
            self._ctx_hero_id = clean_hero
            if clean_tag:
                self._ctx_dataset_tag = clean_tag
            self._ctx_game_time_s = 0.0
            self._ctx_match_key_hash = clean_mkh

            meta = self._build_initial_meta(
                seq=seq,
                dir_name=dir_name,
                session_id=session_id,
                match_key_hash=clean_mkh,
                phase="in_game",
                hero_id=clean_hero,
                hero_name=clean_hero_name,
                dataset_tag=self._ctx_dataset_tag,
                game_mode=clean_mode,
                is_non_mayhem=bool(is_non_mayhem),
            )
            meta["game_started_at"] = meta["started_at"]
            if prev_session:
                self._event_q.put_nowait(
                    (
                        "end_match",
                        prev_session,
                        {
                            "won": -1,
                            "duration_s": 0,
                            "eog_source": "superseded",
                            "ended_at": _utc_iso_ms(),
                            **prev_end_stats,
                        },
                    )
                )
            self._event_q.put_nowait(("begin_match", meta))
        return session_id

    def update_match_meta(self, **kwargs: Any) -> None:
        """Update metadata fields on the active match session."""
        if not kwargs:
            return
        with self._state_lock:
            if self._closed or not self._active_session_id:
                return
            session_id = self._active_session_id
            if "hero_id" in kwargs and kwargs["hero_id"]:
                self._ctx_hero_id = str(kwargs["hero_id"])
            if "dataset_tag" in kwargs and kwargs["dataset_tag"]:
                self._ctx_dataset_tag = str(kwargs["dataset_tag"])
            if "match_key_hash" in kwargs and kwargs["match_key_hash"]:
                self._ctx_match_key_hash = str(kwargs["match_key_hash"])
            clean_updates = dict(kwargs)
        self._event_q.put_nowait(("update_match_meta", session_id, clean_updates))

    def end_match(
        self,
        won: int = -1,
        duration_s: int = 0,
        eog_source: str = "",
        summary_extra: dict[str, Any] | None = None,
    ) -> None:
        """Finalize active match session, emit trailing stall/health summaries, and return to idle."""
        with self._state_lock:
            if self._closed or not self._active_session_id:
                return
            has_active = True
        if has_active:
            self.flush_latency_summary(force=True)
            with self._state_lock:
                health_payload = {
                    "queue_depth": self._event_q.qsize(),
                    "max_queue_depth": self._max_queue_depth,
                    "dropped_events": self._dropped_events,
                    "dropped_roi": self._dropped_roi,
                    "roi_capped_count": self._roi_capped_count,
                    "roi_count": self._roi_count_in_match,
                }
            self.emit(
                DOMAIN_SYSTEM,
                EV_WRITER_HEALTH,
                health_payload,
                priority="high",
            )

        with self._state_lock:
            session_id = self._active_session_id
            if not session_id:
                return
            match_roi_capped = self._roi_capped_count
            match_q_drop_roi = self._match_queue_dropped_roi
            match_q_drop_ev = self._match_queue_dropped_events
            self._active_phase = "idle"
            self._active_session_id = ""
            self._active_match_seq = None
            self._active_match_dir_name = ""
            self._roi_count_in_match = 0
            self._roi_capped_count = 0
            self._dropped_events = 0
            self._dropped_roi = 0
            self._match_queue_dropped_events = 0
            self._match_queue_dropped_roi = 0
            self._max_queue_depth = 0
            self._ctx_game_state = "IDLE"
            self._ctx_game_time_s = None
            self._ctx_match_key_hash = ""

        end_data = {
            "won": int(won) if won in (0, 1) else -1,
            "duration_s": max(0, int(duration_s or 0)),
            "eog_source": str(eog_source or "").strip(),
            "ended_at": _utc_iso_ms(),
            "summary_extra": dict(summary_extra) if isinstance(summary_extra, dict) else {},
            "roi_capped_count": match_roi_capped,
            "queue_dropped_roi": match_q_drop_roi,
            "queue_dropped_events": match_q_drop_ev,
        }
        self._event_q.put_nowait(("end_match", session_id, end_data))

    def _build_initial_meta(
        self,
        *,
        seq: int,
        dir_name: str,
        session_id: str,
        match_key_hash: str,
        phase: str,
        hero_id: str,
        hero_name: str,
        dataset_tag: str,
        game_mode: str,
        is_non_mayhem: bool,
    ) -> dict[str, Any]:
        now_iso = _utc_iso_ms()
        return {
            "schema_v": EVENT_SCHEMA_VERSION,
            "seq": seq,
            "dir_name": dir_name,
            "match_session_id": session_id,
            "match_key_hash": match_key_hash,
            "app_version": self.app_version,
            "started_at": now_iso,
            "game_started_at": "",
            "ended_at": "",
            "phase": phase,
            "hero_id": hero_id,
            "hero_name": hero_name,
            "dataset_tag": dataset_tag,
            "game_mode": game_mode,
            "is_non_mayhem": is_non_mayhem,
            "won": -1,
            "duration_s": 0,
            "eog_source": "",
            "event_count": 0,
            "event_counts_by_type": {},
            "roi_count": 0,
            "roi_evicted": False,
            "events_bytes": 0,
            "debug_bytes": 0,
            "roi_bytes": 0,
            "dropped_events": 0,
            "dropped_roi": 0,
            "roi_capped_count": 0,
            "summary_extra": {},
        }

    # ------------------------------------------------------------------
    # Hot-path Event & ROI Submission (Zero Sync Disk I/O)
    # ------------------------------------------------------------------

    def emit(
        self,
        domain: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        game_time_s: float | None = None,
        game_state: str | None = None,
        hero_id: str | None = None,
        dataset_tag: str | None = None,
        priority: str = "normal",
    ) -> int:
        """Enqueue a structured event without blocking or touching disk (<15us P99)."""
        if self._closed:
            return 0

        qsize = self._event_q.qsize()
        if qsize > self._max_queue_depth:
            self._max_queue_depth = qsize
        if qsize >= self.event_queue_max:
            if priority != "high" or qsize >= self.event_queue_max + 256:
                with self._state_lock:
                    self._dropped_events += 1
                    if self._active_session_id:
                        self._match_queue_dropped_events += 1
                return 0

        ts_epoch = time.time()
        now_mono = time.monotonic()

        with self._state_lock:
            if self._closed:
                return 0
            session_id = self._active_session_id
            if session_id:
                self._ev_seq += 1
                seq = self._ev_seq
                mono_ms = max(0, int((now_mono - self._session_start_mono) * 1000))
                match_key_hash = self._ctx_match_key_hash
            else:
                self._idle_ev_seq += 1
                seq = self._idle_ev_seq
                mono_ms = max(0, int((now_mono - self._session_start_mono) * 1000))
                match_key_hash = ""
            gs = self._ctx_game_state if game_state is None else game_state
            hid = self._ctx_hero_id if hero_id is None else hero_id
            dtag = self._ctx_dataset_tag if dataset_tag is None else dataset_tag
            gt = self._ctx_game_time_s if game_time_s is None else game_time_s

        raw_payload = dict(payload) if isinstance(payload, dict) else {}
        self._event_q.put_nowait(
            (
                "event",
                seq,
                ts_epoch,
                mono_ms,
                gt,
                session_id,
                match_key_hash,
                domain,
                event_type,
                gs,
                hid,
                dtag,
                raw_payload,
                priority,
            )
        )
        return seq

    def enqueue_roi_snapshot(self, tag: str, bgr_crop: Any, ev_seq: int = 0) -> str | None:
        """Enqueue a small anomaly ROI crop for async PNG encoding (never full-screen)."""
        if self._closed or not self.enable_roi_snapshots or bgr_crop is None:
            return None
        if not (hasattr(bgr_crop, "shape") and hasattr(bgr_crop, "copy") and hasattr(bgr_crop, "ndim")):
            return None
        try:
            ndim = int(bgr_crop.ndim)
            if ndim not in (2, 3):
                return None
            h, w = int(bgr_crop.shape[0]), int(bgr_crop.shape[1])
        except Exception:
            return None
        if h <= 0 or w <= 0 or w > _MAX_ROI_WIDTH or h > _MAX_ROI_HEIGHT:
            return None

        safe_tag = _safe_slug(tag, fallback="roi", max_len=40)
        rel_path = f"roi_snapshots/ev{int(ev_seq):04d}_{safe_tag}.png"

        with self._state_lock:
            if self._closed or not self._active_session_id or not self._active_match_dir_name:
                return None
            if self._roi_count_in_match >= self.max_roi_per_match:
                # Policy cap hit (normal quota guard, NOT a queue/IO failure)
                self._roi_capped_count += 1
                return None
            session_id = self._active_session_id
            dir_name = self._active_match_dir_name
            self._roi_count_in_match += 1

        try:
            crop_copy = bgr_crop.copy()
            self._roi_q.put_nowait((session_id, dir_name, rel_path, crop_copy))
        except queue.Full:
            with self._state_lock:
                if self._active_session_id == session_id and self._roi_count_in_match > 0:
                    self._roi_count_in_match -= 1
                self._dropped_roi += 1
                if self._active_session_id == session_id:
                    self._match_queue_dropped_roi += 1
            return None
        except Exception:
            with self._state_lock:
                if self._active_session_id == session_id and self._roi_count_in_match > 0:
                    self._roi_count_in_match -= 1
                self._dropped_roi += 1
                if self._active_session_id == session_id:
                    self._match_queue_dropped_roi += 1
            return None

        return rel_path

    def _enqueue_debug_log(self, line: str) -> None:
        if self._closed or not self.enable_debug_log or not line:
            return
        if self._event_q.qsize() >= self.event_queue_max:
            with self._state_lock:
                self._dropped_events += 1
                if self._active_session_id:
                    self._match_queue_dropped_events += 1
            return
        self._event_q.put_nowait(("debug_log", self._active_session_id, line))

    # ------------------------------------------------------------------
    # Latency Buckets (60s window + end_match flush)
    # ------------------------------------------------------------------

    def record_latency_sample(self, op: str, elapsed_ms: float) -> None:
        """Record a main-thread operation duration in milliseconds into a bounded in-memory bucket."""
        if self._closed or not op:
            return
        val = max(0.0, float(elapsed_ms))
        with self._latency_lock:
            bucket = self._latency_buckets.get(op)
            if bucket is None:
                bucket = []
                self._latency_buckets[op] = bucket
            if len(bucket) < _MAX_LATENCY_SAMPLES_PER_OP:
                bucket.append(val)

    def flush_latency_summary(self, *, force: bool = False) -> bool:
        """Aggregate accumulated latency buckets and emit ``ui_main_thread_stall_summary``."""
        now_mono = time.monotonic()
        with self._latency_lock:
            window_s = max(0.0, now_mono - self._last_latency_flush_mono)
            if not force and window_s < _LATENCY_FLUSH_INTERVAL_S:
                return False
            if not self._latency_buckets:
                self._last_latency_flush_mono = now_mono
                return False
            snapshot = self._latency_buckets
            self._latency_buckets = {}
            self._last_latency_flush_mono = now_mono

        ops_summary: dict[str, dict[str, Any]] = {}
        total_ge_50 = 0
        worker_ge_50 = 0
        for op, vals in snapshot.items():
            if not vals:
                continue
            vals.sort()
            ge_50 = sum(1 for v in vals if v >= 50.0)
            if _is_background_perf_op(op):
                worker_ge_50 += ge_50
            else:
                total_ge_50 += ge_50
            ops_summary[op] = {
                "count": len(vals),
                "p50_ms": round(_percentile(vals, 0.50), 2),
                "p95_ms": round(_percentile(vals, 0.95), 2),
                "max_ms": round(vals[-1], 2),
                "ge_50ms_count": ge_50,
            }

        if not ops_summary:
            return False

        self.emit(
            DOMAIN_PERF,
            EV_MAIN_THREAD_STALL,
            {
                "window_s": round(window_s, 1),
                "ops": ops_summary,
                "total_ge_50ms_count": total_ge_50,
                "worker_ge_50ms_count": worker_ge_50,
            },
            priority="high",
        )
        return True

    # ------------------------------------------------------------------
    # Read-Only Ring & Status Snapshots (for Badge, Tray, AgentBridge)
    # ------------------------------------------------------------------

    def get_recent_events(self, since_seq: int = 0) -> list[dict[str, Any]]:
        """Return sanitized recent events (ring capacity 50) with ``ev_seq > since_seq``."""
        threshold = int(since_seq or 0)
        with self._ring_lock:
            if threshold <= 0:
                return list(self._recent_events)
            return [ev for ev in self._recent_events if int(ev.get("ev_seq", 0)) > threshold]

    def get_recent_vision_traces(self, since_seq: int = 0) -> list[dict[str, Any]]:
        """Return sanitized recent vision-domain events (ring capacity 30) with ``ev_seq > since_seq``."""
        threshold = int(since_seq or 0)
        with self._ring_lock:
            if threshold <= 0:
                return list(self._recent_vision_traces)
            return [ev for ev in self._recent_vision_traces if int(ev.get("ev_seq", 0)) > threshold]

    def get_status_snapshot(self) -> dict[str, Any]:
        """Return live telemetry health and quota snapshot for UI badge and tray status."""
        with self._state_lock:
            active_seq = self._active_match_seq
            active_session = self._active_session_id
            active_dir = self._active_match_dir_name
            active_phase = self._active_phase
            active_hero = self._ctx_hero_id
            ev_count = self._ev_seq if active_session else self._idle_ev_seq
            roi_count = self._roi_count_in_match
            roi_capped = self._roi_capped_count
            dropped_ev = self._dropped_events
            dropped_roi = self._dropped_roi
            max_q_depth = self._max_queue_depth
            closed = self._closed
        with self._stats_lock:
            total_written = self._total_events_written
            disk_bytes = self._disk_bytes_cached
            roi_bytes = self._roi_disk_bytes_cached
            match_count = self._match_count_cached
        return {
            "active": not closed,
            "recording_match": active_seq is not None,
            "active_match_seq": active_seq,
            "active_match_session_id": active_session,
            "active_match_dir": active_dir,
            "active_phase": active_phase,
            "active_hero_id": active_hero,
            "event_count": ev_count,
            "total_events_written": total_written,
            "queue_depth": self._event_q.qsize(),
            "max_queue_depth": max_q_depth,
            "roi_queue_depth": self._roi_q.qsize(),
            "dropped_events": dropped_ev,
            "dropped_roi": dropped_roi,
            "roi_capped_count": roi_capped,
            "roi_count": roi_count,
            "roi_cap": self.max_roi_per_match,
            "disk_bytes": disk_bytes,
            "roi_disk_bytes": roi_bytes,
            "match_count": match_count,
            "max_matches": self.max_matches,
            "enable_roi_snapshots": self.enable_roi_snapshots,
            "enable_debug_log": self.enable_debug_log,
            "root_dir": str(self.root_dir),
            "app_version": self.app_version,
        }

    # ------------------------------------------------------------------
    # Flush & Shutdown
    # ------------------------------------------------------------------

    def flush(self, timeout_s: float = 2.0) -> bool:
        """Wait until all previously enqueued events and ROIs are flushed to disk."""
        if self._closed:
            return True
        t = self._writer_thread
        if t is None or not t.is_alive():
            return False
        done = threading.Event()
        self._event_q.put_nowait(("flush", done))
        return done.wait(timeout=max(0.1, float(timeout_s)))

    def flush_and_close(self, timeout_s: float = 3.0) -> None:
        """Detach debug handler, flush all buffers cleanly, and stop writer thread."""
        self.uninstall_debug_log_handler()
        with self._state_lock:
            if self._closed:
                return
            has_active = bool(self._active_session_id)
        if has_active:
            self.flush_latency_summary(force=True)
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            t = self._writer_thread
        done = threading.Event()
        self._event_q.put_nowait(("shutdown", done))
        done.wait(timeout=max(0.2, float(timeout_s)))
        if t is not None and t.is_alive():
            t.join(timeout=max(0.2, float(timeout_s)))

    # ------------------------------------------------------------------
    # Initial Index Load
    # ------------------------------------------------------------------

    def _self_heal_interrupted_match(
        self,
        mdir: Path,
        entry: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Self-heal matches interrupted by hard kill where meta.json has stale 0 event_count."""
        meta_path = mdir / "meta.json"
        ev_path = mdir / "events.jsonl"
        dbg_path = mdir / "debug_runtime.log"
        roi_dir = mdir / "roi_snapshots"

        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                loaded_meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded_meta, dict):
                    meta = loaded_meta
            except Exception:
                meta = {}

        ev_size = 0
        try:
            if ev_path.is_file():
                ev_size = ev_path.stat().st_size
        except OSError:
            ev_size = 0

        meta_ev_count = int(meta.get("event_count", 0) or 0)
        entry_ev_count = int(entry.get("event_count", 0) or 0)
        phase = str(meta.get("phase") or "")
        ended_at = str(meta.get("ended_at") or "")
        meta_ev_bytes = int(meta.get("events_bytes", 0) or 0)

        needs_heal = (
            (ev_size > 0 and (meta_ev_count <= 0 or entry_ev_count <= 0 or not meta))
            or (phase in ("champ_select", "in_game") and not ended_at)
        )
        if not needs_heal:
            return entry, False

        # Reconstruct counts and summary fields from events.jsonl
        actual_count = 0
        counts_by_type: dict[str, int] = {}
        first_ts = ""
        last_ts = ""
        max_gt = 0.0
        ev_hero = ""
        ev_tag = ""
        ev_mkh = ""
        ev_sid = ""
        ev_ver = ""
        eog_won = -1
        eog_dur = 0
        eog_src = ""
        eog_mode = ""
        eog_non_mayhem: bool | None = None

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
                            continue
                        if not isinstance(obj, dict):
                            continue
                        actual_count += 1
                        et = str(obj.get("event_type") or "unknown")
                        counts_by_type[et] = counts_by_type.get(et, 0) + 1
                        ts_u = str(obj.get("ts_utc") or "")
                        if ts_u:
                            if not first_ts:
                                first_ts = ts_u
                            last_ts = ts_u
                        gt_raw = obj.get("game_time_s")
                        if isinstance(gt_raw, (int, float)) and gt_raw > max_gt:
                            max_gt = float(gt_raw)
                        if not ev_hero and obj.get("hero_id"):
                            ev_hero = str(obj["hero_id"])
                        if not ev_tag and obj.get("dataset_tag"):
                            ev_tag = str(obj["dataset_tag"])
                        if not ev_mkh and obj.get("match_key_hash"):
                            ev_mkh = str(obj["match_key_hash"])
                        if not ev_sid and obj.get("match_session_id"):
                            ev_sid = str(obj["match_session_id"])
                        if not ev_ver and obj.get("app_version"):
                            ev_ver = str(obj["app_version"])
                        if et == EV_MATCH_EOG:
                            pl = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                            w = pl.get("won")
                            if isinstance(w, int) and w in (0, 1):
                                eog_won = w
                            d = pl.get("duration_s")
                            if isinstance(d, (int, float)) and int(d) > eog_dur:
                                eog_dur = int(d)
                            if pl.get("eog_source"):
                                eog_src = str(pl["eog_source"])
                            if pl.get("game_mode"):
                                eog_mode = str(pl["game_mode"])
                            if "is_non_mayhem" in pl:
                                eog_non_mayhem = bool(pl["is_non_mayhem"])
            except OSError:
                pass

        if not meta:
            m_seq = re.match(r"^m_(\d+)(?:_|$)", mdir.name)
            parsed_seq = int(m_seq.group(1)) if m_seq else int(entry.get("seq", 0) or 0)
            meta = self._build_initial_meta(
                seq=parsed_seq,
                dir_name=mdir.name,
                session_id=ev_sid or str(entry.get("match_session_id") or ""),
                match_key_hash=ev_mkh or str(entry.get("match_key_hash") or ""),
                phase="interrupted",
                hero_id=ev_hero or str(entry.get("hero_id") or ""),
                hero_name=str(entry.get("hero_name") or ""),
                dataset_tag=ev_tag or str(entry.get("dataset_tag") or ""),
                game_mode=eog_mode or str(entry.get("game_mode") or ""),
                is_non_mayhem=bool(eog_non_mayhem) if eog_non_mayhem is not None else bool(entry.get("is_non_mayhem", False)),
            )
            if first_ts:
                meta["started_at"] = first_ts
            if ev_ver:
                meta["app_version"] = ev_ver

        meta["dir_name"] = mdir.name
        if actual_count > 0:
            meta["event_count"] = actual_count
            meta["event_counts_by_type"] = counts_by_type
        meta["events_bytes"] = ev_size
        try:
            meta["debug_bytes"] = dbg_path.stat().st_size if dbg_path.is_file() else 0
        except OSError:
            meta["debug_bytes"] = int(meta.get("debug_bytes", 0) or 0)
        if roi_dir.is_dir():
            roi_pngs = list(roi_dir.glob("*.png"))
            meta["roi_count"] = max(int(meta.get("roi_count", 0) or 0), len(roi_pngs))
            meta["roi_bytes"] = _dir_size_bytes(roi_dir)

        if not meta.get("hero_id") and ev_hero:
            meta["hero_id"] = ev_hero
        if not meta.get("dataset_tag") and ev_tag:
            meta["dataset_tag"] = ev_tag
        if not meta.get("match_key_hash") and ev_mkh:
            meta["match_key_hash"] = ev_mkh
        if not meta.get("match_session_id") and ev_sid:
            meta["match_session_id"] = ev_sid
        if not meta.get("app_version") and ev_ver:
            meta["app_version"] = ev_ver
        if int(meta.get("won", -1)) not in (0, 1) and eog_won in (0, 1):
            meta["won"] = eog_won
        if int(meta.get("duration_s", 0) or 0) <= 0:
            if eog_dur > 0:
                meta["duration_s"] = eog_dur
            elif max_gt > 0:
                meta["duration_s"] = int(round(max_gt))
        if not meta.get("eog_source") and eog_src:
            meta["eog_source"] = eog_src
        if not meta.get("game_mode") and eog_mode:
            meta["game_mode"] = eog_mode
        if eog_non_mayhem is not None:
            meta["is_non_mayhem"] = eog_non_mayhem

        if str(meta.get("phase") or "") in ("champ_select", "in_game") and not meta.get("ended_at"):
            meta["phase"] = "interrupted"
            meta["ended_at"] = last_ts or _utc_iso_ms()

        try:
            _atomic_write_json(meta_path, meta)
        except OSError:
            pass
        return self._to_index_entry(meta, mdir), True

    def _load_initial_index(self) -> None:
        loaded: list[dict[str, Any]] = []
        next_seq = 1
        healed_any = False
        if self.index_path.is_file():
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    next_seq = max(1, int(raw.get("next_seq", 1)))
                    for item in raw.get("matches") or []:
                        if not isinstance(item, dict):
                            continue
                        dname = str(item.get("dir_name") or "").strip()
                        if not dname:
                            continue
                        mdir = self.matches_dir / dname
                        if not mdir.is_dir():
                            continue
                        healed_entry, did_heal = self._self_heal_interrupted_match(mdir, dict(item))
                        if did_heal:
                            healed_any = True
                        loaded.append(healed_entry)
            except Exception:
                loaded = []

        # Also recover any match directories present on disk missing from index.json
        known_dirs = {str(m.get("dir_name") or "") for m in loaded}
        try:
            for child in sorted(self.matches_dir.iterdir()):
                if not child.is_dir() or child.name in known_dirs:
                    continue
                meta_path = child / "meta.json"
                ev_path = child / "events.jsonl"
                if meta_path.is_file():
                    try:
                        m = json.loads(meta_path.read_text(encoding="utf-8"))
                        if isinstance(m, dict) and m.get("dir_name"):
                            entry = self._to_index_entry(m, child)
                            healed_entry, _ = self._self_heal_interrupted_match(child, entry)
                            loaded.append(healed_entry)
                            healed_any = True
                            continue
                    except Exception:
                        pass
                if ev_path.is_file() and ev_path.stat().st_size > 0:
                    healed_entry, did_heal = self._self_heal_interrupted_match(child, {"dir_name": child.name})
                    if did_heal:
                        loaded.append(healed_entry)
                        healed_any = True
        except OSError:
            pass

        loaded.sort(key=lambda m: int(m.get("seq", 0)))
        max_seen = max((int(m.get("seq", 0)) for m in loaded), default=0)
        self._next_match_seq = max(next_seq, max_seen + 1)
        self._writer_index_matches = loaded
        if healed_any:
            self._save_index_writer()
        else:
            self._recompute_cached_disk_stats_writer()

    def _to_index_entry(self, meta: dict[str, Any], mdir: Path | None = None) -> dict[str, Any]:
        total_bytes = int(meta.get("events_bytes", 0)) + int(meta.get("debug_bytes", 0)) + int(meta.get("roi_bytes", 0))
        if total_bytes <= 0 and mdir is not None and mdir.is_dir():
            total_bytes = _dir_size_bytes(mdir)
        return {
            "seq": int(meta.get("seq", 0)),
            "dir_name": str(meta.get("dir_name") or ""),
            "match_session_id": str(meta.get("match_session_id") or ""),
            "match_key_hash": str(meta.get("match_key_hash") or ""),
            "app_version": str(meta.get("app_version") or self.app_version),
            "started_at": str(meta.get("started_at") or ""),
            "ended_at": str(meta.get("ended_at") or ""),
            "hero_id": str(meta.get("hero_id") or ""),
            "hero_name": str(meta.get("hero_name") or ""),
            "dataset_tag": str(meta.get("dataset_tag") or ""),
            "game_mode": str(meta.get("game_mode") or ""),
            "is_non_mayhem": bool(meta.get("is_non_mayhem", False)),
            "won": int(meta.get("won", -1)),
            "duration_s": int(meta.get("duration_s", 0)),
            "eog_source": str(meta.get("eog_source") or ""),
            "event_count": int(meta.get("event_count", 0)),
            "roi_count": int(meta.get("roi_count", 0)),
            "roi_capped_count": int(meta.get("roi_capped_count", 0)),
            "roi_evicted": bool(meta.get("roi_evicted", False)),
            "roi_bytes": int(meta.get("roi_bytes", 0)),
            "total_bytes": total_bytes,
        }

    # ------------------------------------------------------------------
    # Background Writer Thread (`HexDevLabWriter`)
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        pending_events: list[tuple[str, str, str, bool]] = []  # (session_id, dir_name, json_line, is_critical)
        pending_debug: list[tuple[str, str]] = []  # (dir_name, sanitized_line)
        last_flush_mono = time.monotonic()
        last_latency_check_mono = time.monotonic()
        last_meta_flush_mono = time.monotonic()

        while True:
            timeout = max(0.02, _FLUSH_INTERVAL_S - (time.monotonic() - last_flush_mono))
            try:
                item = self._event_q.get(timeout=timeout)
            except queue.Empty:
                item = None

            if item is not None:
                kind = item[0]
                if kind == "event":
                    self._handle_event_item_writer(item, pending_events)
                elif kind == "debug_log":
                    self._handle_debug_item_writer(item, pending_debug)
                elif kind == "begin_match":
                    self._flush_batches_writer(pending_events, pending_debug)
                    self._handle_begin_match_writer(item[1])
                    last_flush_mono = time.monotonic()
                    last_meta_flush_mono = last_flush_mono
                elif kind == "upgrade_match":
                    self._flush_batches_writer(pending_events, pending_debug)
                    self._handle_update_meta_writer(item[1], item[2], save_index=True)
                    last_flush_mono = time.monotonic()
                    last_meta_flush_mono = last_flush_mono
                elif kind == "update_match_meta":
                    self._handle_update_meta_writer(item[1], item[2], save_index=True)
                    last_meta_flush_mono = time.monotonic()
                elif kind == "end_match":
                    self._flush_batches_writer(pending_events, pending_debug)
                    self._drain_roi_queue_writer()
                    self._handle_end_match_writer(item[1], item[2])
                    last_flush_mono = time.monotonic()
                    last_meta_flush_mono = last_flush_mono
                elif kind == "flush":
                    self._flush_batches_writer(pending_events, pending_debug)
                    self._drain_roi_queue_writer()
                    self._flush_active_meta_writer()
                    self._save_index_writer()
                    last_flush_mono = time.monotonic()
                    last_meta_flush_mono = last_flush_mono
                    item[1].set()
                elif kind == "shutdown":
                    # Drain any remaining items placed before shutdown
                    while True:
                        try:
                            extra = self._event_q.get_nowait()
                        except queue.Empty:
                            break
                        ek = extra[0]
                        if ek == "event":
                            self._handle_event_item_writer(extra, pending_events)
                        elif ek == "debug_log":
                            self._handle_debug_item_writer(extra, pending_debug)
                        elif ek == "begin_match":
                            self._flush_batches_writer(pending_events, pending_debug)
                            self._handle_begin_match_writer(extra[1])
                        elif ek in ("upgrade_match", "update_match_meta"):
                            self._handle_update_meta_writer(extra[1], extra[2], save_index=True)
                        elif ek == "end_match":
                            self._flush_batches_writer(pending_events, pending_debug)
                            self._drain_roi_queue_writer()
                            self._handle_end_match_writer(extra[1], extra[2])
                        elif ek == "flush":
                            extra[1].set()
                    self._flush_batches_writer(pending_events, pending_debug)
                    self._drain_roi_queue_writer()
                    self._flush_active_meta_writer()
                    self._save_index_writer()
                    item[1].set()
                    return

            # Drain bounded ROI queue opportunistically
            if not self._roi_q.empty():
                self._drain_roi_queue_writer()

            now_mono = time.monotonic()
            if (
                len(pending_events) >= _FLUSH_BATCH_SIZE
                or len(pending_debug) >= _FLUSH_BATCH_SIZE
                or (now_mono - last_flush_mono) >= _FLUSH_INTERVAL_S
            ):
                if pending_events or pending_debug:
                    self._flush_batches_writer(pending_events, pending_debug)
                last_flush_mono = now_mono

            if (now_mono - last_latency_check_mono) >= 15.0:
                last_latency_check_mono = now_mono
                if not self._closed:
                    self.flush_latency_summary(force=False)

            if (now_mono - last_meta_flush_mono) >= self._meta_flush_interval_s:
                last_meta_flush_mono = now_mono
                if self._writer_active_session:
                    if pending_events or pending_debug:
                        self._flush_batches_writer(pending_events, pending_debug)
                        last_flush_mono = now_mono
                    self._flush_active_meta_writer()
                    self._save_index_writer()

    def _handle_event_item_writer(
        self,
        item: tuple[Any, ...],
        pending_events: list[tuple[str, str, str, bool]],
    ) -> None:
        (
            _kind,
            ev_seq,
            ts_epoch,
            mono_ms,
            game_time_s,
            session_id,
            match_key_hash,
            domain,
            event_type,
            game_state,
            hero_id,
            dataset_tag,
            raw_payload,
            priority,
        ) = item

        clean_payload = scrub_payload(raw_payload)
        ev = DevLabEvent(
            schema_v=EVENT_SCHEMA_VERSION,
            ev_seq=int(ev_seq),
            ts_utc=_utc_iso_ms(ts_epoch),
            ts_mono_ms=int(mono_ms),
            game_time_s=None if game_time_s is None else round(float(game_time_s), 3),
            match_session_id=str(session_id or ""),
            match_key_hash=str(match_key_hash or ""),
            app_version=self.app_version,
            domain=str(domain or DOMAIN_SYSTEM),
            event_type=str(event_type or "unknown"),
            game_state=str(game_state or "IDLE"),
            hero_id=str(hero_id or ""),
            dataset_tag=str(dataset_tag or ""),
            payload=clean_payload,
        )
        ev_dict = ev.to_dict()
        json_line = ev.to_json_line()

        with self._ring_lock:
            self._recent_events.append(ev_dict)
            if ev.domain == DOMAIN_VISION:
                self._recent_vision_traces.append(ev_dict)

        dir_name = ""
        meta = self._writer_match_metas.get(session_id) if session_id else None
        if meta is not None:
            dir_name = str(meta.get("dir_name") or "")
            meta["event_count"] = int(meta.get("event_count", 0)) + 1
            counts = meta.setdefault("event_counts_by_type", {})
            counts[ev.event_type] = int(counts.get(ev.event_type, 0)) + 1

        is_critical = priority == "high" or ev.event_type in (EV_MATCH_EOG, EV_WRITER_HEALTH)
        pending_events.append((session_id, dir_name, json_line, is_critical))

    def _handle_debug_item_writer(
        self,
        item: tuple[Any, ...],
        pending_debug: list[tuple[str, str]],
    ) -> None:
        _kind, session_id, raw_line = item
        clean_line = sanitize_log_text(raw_line).rstrip("\r\n")
        if not clean_line:
            return
        dir_name = ""
        if session_id and session_id in self._writer_match_metas:
            dir_name = str(self._writer_match_metas[session_id].get("dir_name") or "")
        elif self._writer_active_dir:
            dir_name = self._writer_active_dir
        pending_debug.append((dir_name, clean_line))

    def _flush_batches_writer(
        self,
        pending_events: list[tuple[str, str, str, bool]],
        pending_debug: list[tuple[str, str]],
    ) -> None:
        if pending_events:
            batch = list(pending_events)
            pending_events.clear()
            grouped: dict[tuple[str, str], list[tuple[str, bool]]] = {}
            for session_id, dir_name, line, is_crit in batch:
                grouped.setdefault((session_id, dir_name), []).append((line, is_crit))

            written_count = 0
            for (session_id, dir_name), entries in grouped.items():
                if dir_name:
                    mdir = self.matches_dir / dir_name
                    mdir.mkdir(parents=True, exist_ok=True)
                    ev_path = mdir / "events.jsonl"
                    meta = self._writer_match_metas.get(session_id)
                    cur_bytes = int(meta.get("events_bytes", 0)) if meta is not None else (
                        ev_path.stat().st_size if ev_path.is_file() else 0
                    )
                    chunks: list[bytes] = []
                    for line, is_crit in entries:
                        enc = (line + "\n").encode("utf-8", errors="replace")
                        cap = (
                            self.max_match_events_bytes + _EOG_OVERFLOW_GRACE_BYTES
                            if is_crit
                            else self.max_match_events_bytes
                        )
                        if cur_bytes + len(enc) > cap:
                            with self._state_lock:
                                if self._active_session_id == session_id:
                                    self._dropped_events += 1
                            if meta is not None:
                                meta["dropped_events"] = int(meta.get("dropped_events", 0)) + 1
                            continue
                        chunks.append(enc)
                        cur_bytes += len(enc)
                        written_count += 1
                    if chunks:
                        try:
                            with ev_path.open("ab") as fh:
                                fh.write(b"".join(chunks))
                            if meta is not None:
                                meta["events_bytes"] = ev_path.stat().st_size
                        except OSError:
                            pass
                else:
                    raw_blob = b"".join((line + "\n").encode("utf-8", errors="replace") for line, _ in entries)
                    try:
                        with self.idle_events_path.open("ab") as fh:
                            fh.write(raw_blob)
                        written_count += len(entries)
                        if self.idle_events_path.stat().st_size > _IDLE_EVENTS_MAX_BYTES:
                            _trim_tail_jsonl(self.idle_events_path, _IDLE_EVENTS_MAX_BYTES)
                    except OSError:
                        pass

            with self._stats_lock:
                self._total_events_written += written_count

        if pending_debug:
            dbatch = list(pending_debug)
            pending_debug.clear()
            grouped_dbg: dict[str, list[str]] = {}
            for dir_name, line in dbatch:
                grouped_dbg.setdefault(dir_name, []).append(line)

            for dir_name, lines in grouped_dbg.items():
                blob = ("\n".join(lines) + "\n").encode("utf-8", errors="replace")
                if dir_name:
                    mdir = self.matches_dir / dir_name
                    mdir.mkdir(parents=True, exist_ok=True)
                    dpath = mdir / "debug_runtime.log"
                    try:
                        with dpath.open("ab") as fh:
                            fh.write(blob)
                        sz = dpath.stat().st_size
                        if sz > self.max_match_debug_bytes:
                            sz = _trim_head_tail_bytes(dpath, self.max_match_debug_bytes)
                        for m in self._writer_match_metas.values():
                            if m.get("dir_name") == dir_name:
                                m["debug_bytes"] = sz
                                break
                    except OSError:
                        pass
                else:
                    try:
                        with self.idle_debug_path.open("ab") as fh:
                            fh.write(blob)
                        if self.idle_debug_path.stat().st_size > _IDLE_DEBUG_MAX_BYTES:
                            _trim_head_tail_bytes(self.idle_debug_path, _IDLE_DEBUG_MAX_BYTES)
                    except OSError:
                        pass

    def _drain_roi_queue_writer(self) -> None:
        wrote_any = False
        while True:
            try:
                session_id, dir_name, rel_path, bgr_crop = self._roi_q.get_nowait()
            except queue.Empty:
                break
            png_bytes = _encode_roi_png(bgr_crop, max_bytes=_MAX_ROI_SINGLE_BYTES)
            if png_bytes is None:
                with self._state_lock:
                    if self._active_session_id == session_id:
                        self._dropped_roi += 1
                meta = self._writer_match_metas.get(session_id)
                if meta is not None:
                    meta["dropped_roi"] = int(meta.get("dropped_roi", 0)) + 1
                continue

            target = self.matches_dir / dir_name / rel_path
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".png.tmp")
                tmp.write_bytes(png_bytes)
                os.replace(tmp, target)
                wrote_any = True
                meta = self._writer_match_metas.get(session_id)
                if meta is not None:
                    meta["roi_count"] = int(meta.get("roi_count", 0)) + 1
                    meta["roi_bytes"] = int(meta.get("roi_bytes", 0)) + len(png_bytes)
                    self._upsert_index_entry_writer(meta)
            except OSError:
                with self._state_lock:
                    if self._active_session_id == session_id:
                        self._dropped_roi += 1
                meta = self._writer_match_metas.get(session_id)
                if meta is not None:
                    meta["dropped_roi"] = int(meta.get("dropped_roi", 0)) + 1

        if wrote_any:
            self._enforce_quotas_writer()
            self._save_index_writer()

    def _handle_begin_match_writer(self, meta: dict[str, Any]) -> None:
        dir_name = str(meta["dir_name"])
        session_id = str(meta["match_session_id"])
        mdir = self.matches_dir / dir_name
        mdir.mkdir(parents=True, exist_ok=True)
        self._writer_match_metas[session_id] = meta
        self._writer_active_session = session_id
        self._writer_active_dir = dir_name
        try:
            _atomic_write_json(mdir / "meta.json", meta)
        except OSError:
            pass
        self._upsert_index_entry_writer(meta)
        self._enforce_quotas_writer()
        self._save_index_writer()

    def _handle_update_meta_writer(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        save_index: bool,
    ) -> None:
        meta = self._writer_match_metas.get(session_id)
        if meta is None:
            return
        clean = scrub_payload(updates)
        for k, v in clean.items():
            meta[k] = v
        mdir = self.matches_dir / str(meta["dir_name"])
        try:
            _atomic_write_json(mdir / "meta.json", meta)
        except OSError:
            pass
        self._upsert_index_entry_writer(meta)
        if save_index:
            self._save_index_writer()

    def _handle_end_match_writer(self, session_id: str, end_data: dict[str, Any]) -> None:
        meta = self._writer_match_metas.get(session_id)
        if meta is None:
            return
        meta["phase"] = "ended"
        meta["ended_at"] = str(end_data.get("ended_at") or _utc_iso_ms())
        won = int(end_data.get("won", -1))
        if won in (0, 1):
            meta["won"] = won
        dur = int(end_data.get("duration_s", 0))
        if dur > 0:
            meta["duration_s"] = dur
        if end_data.get("eog_source"):
            meta["eog_source"] = str(end_data["eog_source"])
        extra = end_data.get("summary_extra")
        if isinstance(extra, dict) and extra:
            meta["summary_extra"] = scrub_payload(extra)
        meta["dropped_events"] = int(meta.get("dropped_events", 0)) + int(
            end_data.get("queue_dropped_events", 0) or 0
        )
        meta["dropped_roi"] = int(meta.get("dropped_roi", 0)) + int(
            end_data.get("queue_dropped_roi", 0) or 0
        )
        meta["roi_capped_count"] = max(
            int(meta.get("roi_capped_count", 0) or 0),
            int(end_data.get("roi_capped_count", 0) or 0),
        )

        mdir = self.matches_dir / str(meta["dir_name"])
        if mdir.is_dir():
            ev_p = mdir / "events.jsonl"
            dbg_p = mdir / "debug_runtime.log"
            roi_p = mdir / "roi_snapshots"
            meta["events_bytes"] = ev_p.stat().st_size if ev_p.is_file() else 0
            meta["debug_bytes"] = dbg_p.stat().st_size if dbg_p.is_file() else 0
            meta["roi_bytes"] = _dir_size_bytes(roi_p) if roi_p.is_dir() else 0
            try:
                _atomic_write_json(mdir / "meta.json", meta)
            except OSError:
                pass

        self._upsert_index_entry_writer(meta)
        if self._writer_active_session == session_id:
            self._writer_active_session = ""
            self._writer_active_dir = ""
        # Release in-memory meta for ended match to keep footprint flat
        self._writer_match_metas.pop(session_id, None)
        self._enforce_quotas_writer()
        self._save_index_writer()

    def _flush_active_meta_writer(self) -> None:
        if not self._writer_active_session:
            return
        meta = self._writer_match_metas.get(self._writer_active_session)
        if meta is None:
            return
        with self._state_lock:
            if self._active_session_id == self._writer_active_session:
                meta["roi_capped_count"] = max(
                    int(meta.get("roi_capped_count", 0) or 0),
                    self._roi_capped_count,
                )
        mdir = self.matches_dir / str(meta["dir_name"])
        if mdir.is_dir():
            ev_p = mdir / "events.jsonl"
            dbg_p = mdir / "debug_runtime.log"
            roi_p = mdir / "roi_snapshots"
            try:
                meta["events_bytes"] = ev_p.stat().st_size if ev_p.is_file() else 0
                meta["debug_bytes"] = dbg_p.stat().st_size if dbg_p.is_file() else 0
                meta["roi_bytes"] = _dir_size_bytes(roi_p) if roi_p.is_dir() else 0
            except OSError:
                pass
            try:
                _atomic_write_json(mdir / "meta.json", meta)
            except OSError:
                pass
            self._upsert_index_entry_writer(meta)

    def _upsert_index_entry_writer(self, meta: dict[str, Any]) -> None:
        dir_name = str(meta.get("dir_name") or "")
        entry = self._to_index_entry(meta, self.matches_dir / dir_name)
        for idx, existing in enumerate(self._writer_index_matches):
            if existing.get("dir_name") == dir_name:
                self._writer_index_matches[idx] = entry
                return
        self._writer_index_matches.append(entry)
        self._writer_index_matches.sort(key=lambda m: int(m.get("seq", 0)))

    def _save_index_writer(self) -> None:
        payload = {
            "schema_v": EVENT_SCHEMA_VERSION,
            "next_seq": self._next_match_seq,
            "max_matches": self.max_matches,
            "updated_at": _utc_iso_ms(),
            "matches": list(self._writer_index_matches),
        }
        try:
            _atomic_write_json(self.index_path, payload)
        except OSError:
            pass
        self._recompute_cached_disk_stats_writer()

    def _recompute_cached_disk_stats_writer(self) -> None:
        total_match_bytes = 0
        total_roi_bytes = 0
        for item in self._writer_index_matches:
            total_match_bytes += max(0, int(item.get("total_bytes", 0)))
            total_roi_bytes += max(0, int(item.get("roi_bytes", 0)))
        idle_bytes = 0
        for p in (self.idle_events_path, self.idle_debug_path, self.index_path):
            try:
                if p.is_file():
                    idle_bytes += p.stat().st_size
            except OSError:
                pass
        with self._stats_lock:
            self._disk_bytes_cached = total_match_bytes + idle_bytes
            self._roi_disk_bytes_cached = total_roi_bytes
            self._match_count_cached = len(self._writer_index_matches)

    def _evict_match_roi_writer(self, entry: dict[str, Any]) -> int:
        dname = str(entry.get("dir_name") or "")
        if not dname:
            return 0
        mdir = self.matches_dir / dname
        roi_dir = mdir / "roi_snapshots"
        freed = 0
        if roi_dir.is_dir():
            freed = _dir_size_bytes(roi_dir)
            shutil.rmtree(roi_dir, ignore_errors=True)
        entry["roi_bytes"] = 0
        entry["roi_evicted"] = True
        entry["total_bytes"] = max(0, int(entry.get("total_bytes", 0)) - freed)
        meta_path = mdir / "meta.json"
        if meta_path.is_file():
            try:
                m = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(m, dict):
                    m["roi_bytes"] = 0
                    m["roi_evicted"] = True
                    _atomic_write_json(meta_path, m)
            except Exception:
                pass
        return freed

    def _enforce_quotas_writer(self) -> None:
        active_dir = self._writer_active_dir

        # 1. Refresh actual ROI/total sizes for matches that have ROI dirs
        total_roi = 0
        total_bytes = 0
        for entry in self._writer_index_matches:
            dname = str(entry.get("dir_name") or "")
            mdir = self.matches_dir / dname
            if not mdir.is_dir():
                entry["roi_bytes"] = 0
                entry["total_bytes"] = 0
                continue
            rdir = mdir / "roi_snapshots"
            rb = _dir_size_bytes(rdir) if rdir.is_dir() else 0
            tb = _dir_size_bytes(mdir)
            entry["roi_bytes"] = rb
            entry["total_bytes"] = tb
            total_roi += rb
            total_bytes += tb

        # 2. Enforce independent ROI byte quota first (FIFO oldest matches)
        if total_roi > self.max_roi_bytes:
            for entry in self._writer_index_matches:
                if total_roi <= self.max_roi_bytes:
                    break
                if entry.get("dir_name") == active_dir:
                    continue
                if int(entry.get("roi_bytes", 0)) <= 0:
                    continue
                freed = self._evict_match_roi_writer(entry)
                total_roi = max(0, total_roi - freed)
                total_bytes = max(0, total_bytes - freed)

        # 3. Enforce max_matches FIFO count cap (never evict currently active match)
        while len(self._writer_index_matches) > self.max_matches:
            victim_idx = next(
                (i for i, e in enumerate(self._writer_index_matches) if e.get("dir_name") != active_dir),
                None,
            )
            if victim_idx is None:
                break
            victim = self._writer_index_matches.pop(victim_idx)
            vdir = self.matches_dir / str(victim.get("dir_name") or "")
            freed = int(victim.get("total_bytes", 0))
            shutil.rmtree(vdir, ignore_errors=True)
            total_bytes = max(0, total_bytes - freed)

        # 4. Enforce total directory byte budget: evict remaining old ROIs first, then oldest matches
        if total_bytes > self.max_total_bytes:
            for entry in self._writer_index_matches:
                if total_bytes <= self.max_total_bytes:
                    break
                if entry.get("dir_name") == active_dir:
                    continue
                if int(entry.get("roi_bytes", 0)) > 0:
                    freed = self._evict_match_roi_writer(entry)
                    total_bytes = max(0, total_bytes - freed)

        while total_bytes > self.max_total_bytes and len(self._writer_index_matches) > 1:
            victim_idx = next(
                (i for i, e in enumerate(self._writer_index_matches) if e.get("dir_name") != active_dir),
                None,
            )
            if victim_idx is None:
                break
            victim = self._writer_index_matches.pop(victim_idx)
            vdir = self.matches_dir / str(victim.get("dir_name") or "")
            freed = int(victim.get("total_bytes", 0))
            shutil.rmtree(vdir, ignore_errors=True)
            total_bytes = max(0, total_bytes - freed)

        self._recompute_cached_disk_stats_writer()
