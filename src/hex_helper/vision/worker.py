"""Persistent background vision worker thread (MTA COM) for HexHelper.

Offloads ScreenCapturer (dxcam/mss), WinRtOcrEngine, AugmentPickerDetector,
TransmuteRevealWatcher, ItemIconMatcher, AugmentIconMatcher, 60ms picker vanish
probing, and <0.25ms degraded-mode gray-screen death detection off the Qt UI thread.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from hex_helper.config import NormalizedRect
from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher
from hex_helper.vision.capture import (
    ScreenCapturer,
    WindowRect,
    attach_default_desktop,
    find_game_window,
    get_primary_screen_rect,
    is_hwnd_foreground,
)
from hex_helper.vision.detector import AugmentPickerDetector, DetectedCard, PickerDetectionResult
from hex_helper.vision.item_icon_matcher import ItemIconMatcher
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import WinRtOcrEngine
from hex_helper.vision.transmute import TransmuteRevealWatcher, get_cursor_pos

logger = logging.getLogger(__name__)

COINIT_MULTITHREADED = 0x0
COINIT_APARTMENTTHREADED = 0x2


def _resolve_worker_coinit_mode() -> int:
    """Choose worker COM apartment consistent with comtypes to prevent RPC_E_CHANGED_MODE."""
    if "comtypes" in sys.modules:
        if getattr(sys, "coinit_flags", COINIT_APARTMENTTHREADED) != COINIT_MULTITHREADED:
            return COINIT_APARTMENTTHREADED
        return COINIT_MULTITHREADED
    if not hasattr(sys, "coinit_flags"):
        sys.coinit_flags = COINIT_MULTITHREADED  # type: ignore[attr-defined]
    if getattr(sys, "coinit_flags", COINIT_MULTITHREADED) != COINIT_MULTITHREADED:
        return COINIT_APARTMENTTHREADED
    return COINIT_MULTITHREADED


@dataclass(frozen=True)
class VisionTickContext:
    """Immutable snapshot of app state pushed atomically from main thread to HexVisionWorker."""

    vision_gen: int = 0
    game_state: str = "IDLE"
    hero_id: str = ""
    game_time_s: float = 0.0
    selected_augments: tuple[str, ...] = ()
    is_degraded_2999: bool = False
    pending_transmute_active: bool = False
    exclude_rects: tuple[tuple[int, int, int, int], ...] = ()
    hud_scale: float = 1.0
    suppressed_visual_items: frozenset[str] = frozenset()
    suppressed_visual_augments: frozenset[str] = frozenset()
    devlab_active: bool = False
    devlab_roi_snapshots: bool = False
    card_band_roi: Any = None
    title_band_roi: Any = None
    cursor_pos: tuple[int, int] | None = None
    # Optional fields for active transmute / picker context
    pending_transmute: Any = None
    active_cards: tuple[Any, ...] = ()


@dataclass(frozen=True)
class VisionFrameOutput:
    """Immutable result emitted by HexVisionWorker back to the UI thread."""

    vision_gen: int
    now_mono: float
    game_rect: WindowRect | None = None
    game_fg: bool = False
    picker_result: PickerDetectionResult | None = None
    transmute_hits: list[Any] = field(default_factory=list)
    passive_hover_hit: Any = None
    transmute_trace: dict[str, Any] = field(default_factory=dict)
    plan_b_item_hits: list[Any] = field(default_factory=list)
    plan_b_item_states: dict[int, str] = field(default_factory=dict)
    plan_b_aug_hits: list[Any] = field(default_factory=list)
    plan_b_forced: bool = False
    visual_is_dead: bool | None = None
    fast_vanish_mono: float | None = None
    fast_vanish_diffs: dict[int, float] = field(default_factory=dict)
    capture_backend: str = ""
    timings_ms: dict[str, float] = field(default_factory=dict)
    roi_crops: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # Optional metadata for black-frame alerts, fast reroll hints, and fast-path classification
    is_black: bool = False
    consecutive_black_frames: int = 0
    fast_reroll_slot: int | None = None
    fast_vanish_only: bool = False
    plan_b_ran: bool = False


def compute_center_mean_saturation(frame_bgr: np.ndarray | None) -> float | None:
    """<0.25ms center-50% HSV mean saturation probe for degraded-mode gray-screen death detection."""
    if frame_bgr is None or not isinstance(frame_bgr, np.ndarray) or frame_bgr.size == 0:
        return None
    if frame_bgr.ndim < 3 or frame_bgr.shape[2] < 3:
        return None
    fh, fw = frame_bgr.shape[:2]
    if fh < 16 or fw < 16:
        return None
    y0, y1 = fh // 4, (fh * 3) // 4
    x0, x1 = fw // 4, (fw * 3) // 4
    center = frame_bgr[y0:y1, x0:x1]
    if center.size == 0:
        return None
    ch, cw = center.shape[:2]
    if ch >= 256 and cw >= 256:
        sy = max(1, ch // 72)
        sx = max(1, cw // 128)
        center = center[::sy, ::sx]
    small = cv2.resize(center, (64, 36), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(small[:, :, :3], cv2.COLOR_BGR2HSV)
    return float(np.mean(hsv[:, :, 1]))


class GrayScreenDeathDetector:
    """2-frame hysteresis gray-screen death/revive detector for 2999-degraded mode (<0.25ms)."""

    DEAD_SAT_THRESHOLD = 18.0
    ALIVE_SAT_THRESHOLD = 26.0
    REQUIRED_STREAK = 2

    def __init__(self) -> None:
        self._is_dead: bool | None = None
        self._dead_streak: int = 0
        self._alive_streak: int = 0
        self.last_mean_s: float | None = None

    def reset(self) -> None:
        self._is_dead = None
        self._dead_streak = 0
        self._alive_streak = 0
        self.last_mean_s = None

    @property
    def is_dead(self) -> bool | None:
        return self._is_dead

    def update(self, frame_bgr: np.ndarray | None, *, is_black: bool = False) -> bool | None:
        if is_black or frame_bgr is None or frame_bgr.size == 0:
            return self._is_dead
        mean_s = compute_center_mean_saturation(frame_bgr)
        self.last_mean_s = mean_s
        if mean_s is None:
            return self._is_dead
        if mean_s < self.DEAD_SAT_THRESHOLD:
            self._dead_streak += 1
            self._alive_streak = 0
            if self._dead_streak >= self.REQUIRED_STREAK:
                self._is_dead = True
        elif mean_s >= self.ALIVE_SAT_THRESHOLD:
            self._alive_streak += 1
            self._dead_streak = 0
            if self._alive_streak >= self.REQUIRED_STREAK:
                self._is_dead = False
        else:
            self._dead_streak = 0
            self._alive_streak = 0
        return self._is_dead


class HexVisionWorker:
    """Single persistent daemon thread owning screen capture, WinRT OCR, picker detection, and Plan B."""

    NORMAL_TICK_INTERVAL_S = 0.35
    FAST_VANISH_INTERVAL_S = 0.06
    PLAN_B_INTERVAL_S = 2.5
    IDLE_WAIT_INTERVAL_S = 0.25

    _COALESCE_CMDS: frozenset[str] = frozenset(
        {"force_rescan", "plan_b_rescan", "update_rois", "update_catalog", "reload_icons", "reset_match", "capture_picker_refs", "stop"}
    )

    def __init__(
        self,
        *,
        on_output: Callable[[VisionFrameOutput], None] | None = None,
        capturer: ScreenCapturer | None = None,
        ocr_engine: WinRtOcrEngine | None = None,
        detector: AugmentPickerDetector | None = None,
        transmute_watcher: TransmuteRevealWatcher | None = None,
        item_icon_matcher: ItemIconMatcher | None = None,
        augment_icon_matcher: AugmentIconMatcher | None = None,
        prefer_dxcam: bool = True,
        language_tag: str = "zh-Hans-CN",
        cache_dir: Path | str | None = None,
        card_band_roi: NormalizedRect | None = None,
        title_band_roi: NormalizedRect | None = None,
        initial_augments: list[AugmentInfo] | None = None,
        tick_interval_s: float = NORMAL_TICK_INTERVAL_S,
        fast_vanish_interval_s: float = FAST_VANISH_INTERVAL_S,
        plan_b_interval_s: float = PLAN_B_INTERVAL_S,
        find_window_fn: Callable[[], WindowRect | None] | None = None,
        is_foreground_fn: Callable[[int], bool] | None = None,
        primary_rect_fn: Callable[[], WindowRect] | None = None,
        get_cursor_pos_fn: Callable[[], tuple[int, int] | None] | None = None,
        clock_fn: Callable[[], float] | None = None,
    ) -> None:
        self._on_output = on_output
        self._capturer = capturer
        self._owns_capturer = capturer is None
        self._ocr_engine = ocr_engine
        self._owns_ocr = ocr_engine is None
        self._detector = detector
        self._transmute_watcher = transmute_watcher
        self._item_icon_matcher = item_icon_matcher
        self._augment_icon_matcher = augment_icon_matcher

        self._prefer_dxcam = bool(prefer_dxcam)
        self._language_tag = str(language_tag or "zh-Hans-CN")
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._card_band_roi = card_band_roi or NormalizedRect(0.08, 0.24, 0.84, 0.48)
        self._title_band_roi = title_band_roi or NormalizedRect(0.08, 0.385, 0.84, 0.052)
        self._augments: list[AugmentInfo] = list(initial_augments) if initial_augments else []

        self._tick_interval_s = max(0.02, float(tick_interval_s))
        self._fast_vanish_interval_s = max(0.015, float(fast_vanish_interval_s))
        self._plan_b_interval_s = max(0.2, float(plan_b_interval_s))

        self._find_window_fn = find_window_fn or find_game_window
        self._is_foreground_fn = is_foreground_fn or is_hwnd_foreground
        self._primary_rect_fn = primary_rect_fn or get_primary_screen_rect
        self._get_cursor_pos_fn = get_cursor_pos_fn or get_cursor_pos
        self._clock_fn = clock_fn or time.monotonic

        self._ctx_lock = threading.Lock()
        self._ctx = VisionTickContext()
        self._cmd_lock = threading.Lock()
        self._commands: deque[tuple[str, dict[str, Any]]] = deque()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Worker-thread-private state
        self._death_detector = GrayScreenDeathDetector()
        self._last_plan_b_scan_mono: float = 0.0
        self._last_full_tick_mono: float = 0.0
        self._last_vanish_tick_mono: float = 0.0
        self._last_frame_bgr: np.ndarray | None = None
        self._last_game_rect: WindowRect | None = None
        self._picker_slot_refs: dict[int, tuple[tuple[int, int, int, int], np.ndarray]] = {}
        self._picker_ref_band_bbox: tuple[int, int, int, int] | None = None
        self._picker_ref_card_ids: tuple[str, ...] = ()
        self._vanish_emitted: bool = False
        self._last_fast_reroll_slot: int | None = None
        self._last_fast_reroll_mono: float = 0.0
        self._ocr_init_attempted: bool = ocr_engine is not None

    def set_on_output(self, callback: Callable[[VisionFrameOutput], None] | None) -> None:
        self._on_output = callback

    def is_running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive() and not self._stop_event.is_set())

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._thread_entry,
            name="HexVisionWorker",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        self.enqueue_command("stop")
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=max(0.1, float(timeout)))

    def update_context(self, ctx: VisionTickContext) -> None:
        with self._ctx_lock:
            prev_state = self._ctx.game_state
            self._ctx = ctx
        if ctx.game_state != prev_state and ctx.game_state in ("IN_GAME", "AUGMENT_PICKING"):
            self._wake_event.set()

    def get_context(self) -> VisionTickContext:
        with self._ctx_lock:
            return self._ctx

    def enqueue_command(self, cmd: str, payload: dict[str, Any] | None = None) -> None:
        name = str(cmd or "").strip()
        if not name:
            return
        data = dict(payload) if payload else {}
        with self._cmd_lock:
            if name in self._COALESCE_CMDS and self._commands:
                # Coalesce duplicate pending commands, preserving latest payload
                self._commands = deque((c, p) for (c, p) in self._commands if c != name)
            self._commands.append((name, data))
        self._wake_event.set()

    def _pop_commands(self) -> list[tuple[str, dict[str, Any]]]:
        with self._cmd_lock:
            if not self._commands:
                return []
            out = list(self._commands)
            self._commands.clear()
            return out

    def _emit_output(self, out: VisionFrameOutput) -> None:
        cb = self._on_output
        if cb is None:
            return
        try:
            cb(out)
        except Exception as exc:
            logger.debug("HexVisionWorker on_output callback failed: %s", exc)

    # ------------------------------------------------------------------
    # Lazy worker-thread resource ownership (keeps dxcam/WinRT off UI thread)
    # ------------------------------------------------------------------

    def _ensure_capturer(self) -> ScreenCapturer:
        if self._capturer is None:
            self._capturer = ScreenCapturer(prefer_dxcam=self._prefer_dxcam, lazy=False)
            self._owns_capturer = True
        return self._capturer

    def _ensure_ocr_and_detector(self, ctx: VisionTickContext) -> None:
        if ctx.card_band_roi is not None and ctx.title_band_roi is not None:
            if ctx.card_band_roi != self._card_band_roi or ctx.title_band_roi != self._title_band_roi:
                self._card_band_roi = ctx.card_band_roi
                self._title_band_roi = ctx.title_band_roi
                if self._detector is not None:
                    self._detector.update_rois(self._card_band_roi, self._title_band_roi)

        if self._ocr_engine is None and not self._ocr_init_attempted:
            self._ocr_init_attempted = True
            try:
                self._ocr_engine = WinRtOcrEngine(self._language_tag)
                self._owns_ocr = True
            except Exception as exc:
                logger.warning("HexVisionWorker WinRT OCR init unavailable: %s", exc)
                self._ocr_engine = None

        if self._detector is None and self._ocr_engine is not None and self._augments:
            self._detector = AugmentPickerDetector(
                ocr_engine=self._ocr_engine,
                matcher=AugmentMatcher(self._augments),
                card_band_roi=self._card_band_roi,
                title_band_roi=self._title_band_roi,
            )

        if self._transmute_watcher is None and self._ocr_engine is not None and self._augments:
            self._transmute_watcher = TransmuteRevealWatcher(self._ocr_engine, self._augments)

    def _ensure_plan_b_matchers(self) -> None:
        if self._item_icon_matcher is None and self._cache_dir is not None:
            try:
                self._item_icon_matcher = ItemIconMatcher(cache_dir=self._cache_dir)
            except Exception as exc:
                logger.debug("HexVisionWorker ItemIconMatcher init failed: %s", exc)
        if self._augment_icon_matcher is None and self._cache_dir is not None:
            try:
                self._augment_icon_matcher = AugmentIconMatcher(cache_dir=self._cache_dir, allow_download=False)
            except Exception as exc:
                logger.debug("HexVisionWorker AugmentIconMatcher init failed: %s", exc)

    def _close_owned_resources(self) -> None:
        if self._owns_capturer and self._capturer is not None:
            try:
                self._capturer.close()
            except Exception:
                pass
            self._capturer = None
        if self._owns_ocr and self._ocr_engine is not None:
            try:
                self._ocr_engine.close()
            except Exception:
                pass
            self._ocr_engine = None

    # ------------------------------------------------------------------
    # Picker 60ms lightweight band-grab vanish & single-slot reroll probe
    # ------------------------------------------------------------------

    def _capture_picker_slot_refs(
        self,
        frame_bgr: np.ndarray | None,
        game_rect: WindowRect | None,
        cards: tuple[DetectedCard, ...] | list[DetectedCard],
    ) -> None:
        if frame_bgr is None or frame_bgr.size == 0 or game_rect is None or len(cards) < 2:
            return
        fh, fw = frame_bgr.shape[:2]
        refs: dict[int, tuple[tuple[int, int, int, int], np.ndarray]] = {}
        min_l, min_t, max_r, max_b = 10**9, 10**9, 0, 0
        for c in cards:
            l, t, r, b = c.screen_box
            x0 = max(0, min(fw - 1, l - game_rect.left - 8))
            y0 = max(0, min(fh - 1, t - game_rect.top - 6))
            x1 = max(x0 + 8, min(fw, r - game_rect.left + 8))
            y1 = max(y0 + 8, min(fh, b - game_rect.top + 6))
            crop = frame_bgr[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            thumb = cv2.resize(gray, (48, 16), interpolation=cv2.INTER_AREA)
            scr_box = (game_rect.left + x0, game_rect.top + y0, game_rect.left + x1, game_rect.top + y1)
            refs[int(c.slot_index)] = (scr_box, thumb)
            min_l = min(min_l, scr_box[0])
            min_t = min(min_t, scr_box[1])
            max_r = max(max_r, scr_box[2])
            max_b = max(max_b, scr_box[3])
        if len(refs) >= 2 and max_r > min_l and max_b > min_t:
            self._picker_slot_refs = refs
            self._picker_ref_band_bbox = (min_l, min_t, max_r, max_b)
            self._picker_ref_card_ids = tuple(str(c.augment.augment_id) for c in cards)
            self._vanish_emitted = False

    def _clear_picker_slot_refs(self) -> None:
        self._picker_slot_refs = {}
        self._picker_ref_band_bbox = None
        self._picker_ref_card_ids = ()
        self._vanish_emitted = False

    def _run_fast_vanish_check(self, ctx: VisionTickContext, now_mono: float) -> VisionFrameOutput | None:
        if not self._picker_slot_refs or self._picker_ref_band_bbox is None or self._vanish_emitted:
            return None
        t0 = time.perf_counter()
        capturer = self._ensure_capturer()
        bl, bt, br, bb = self._picker_ref_band_bbox
        t_cap0 = time.perf_counter()
        cap = capturer.grab(region=(bl, bt, br, bb))
        cap_ms = (time.perf_counter() - t_cap0) * 1000.0
        if cap is None or cap.is_black or cap.frame_bgr is None or cap.frame_bgr.size == 0:
            return None
        bh, bw = cap.frame_bgr.shape[:2]
        diffs: dict[int, float] = {}
        for slot, (sbox, ref_thumb) in self._picker_slot_refs.items():
            x0 = max(0, min(bw - 1, sbox[0] - bl))
            y0 = max(0, min(bh - 1, sbox[1] - bt))
            x1 = max(x0 + 4, min(bw, sbox[2] - bl))
            y1 = max(y0 + 4, min(bh, sbox[3] - bt))
            sub = cap.frame_bgr[y0:y1, x0:x1]
            if sub.size == 0:
                continue
            cur = cv2.resize(cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY), (48, 16), interpolation=cv2.INTER_AREA)
            diffs[slot] = round(float(np.mean(np.abs(cur.astype(np.int16) - ref_thumb.astype(np.int16)))), 3)
        if len(diffs) < 2:
            return None

        changed = [s for s, d in diffs.items() if d >= 22.0]
        total_ms = (time.perf_counter() - t0) * 1000.0
        timings = {
            "capture_ms": round(cap_ms, 3),
            "detect_ms": 0.0,
            "plan_b_ms": 0.0,
            "passive_hover_ms": 0.0,
            "total_ms": round(total_ms, 3),
        }
        if len(changed) >= 2 or len(changed) == len(diffs):
            self._vanish_emitted = True
            return VisionFrameOutput(
                vision_gen=ctx.vision_gen,
                now_mono=now_mono,
                game_rect=self._last_game_rect,
                game_fg=True,
                fast_vanish_mono=now_mono,
                fast_vanish_diffs=diffs,
                capture_backend=str(getattr(capturer, "backend", cap.backend) or ""),
                timings_ms=timings,
                fast_vanish_only=True,
            )
        if len(changed) == 1 and all(d < 16.0 for s, d in diffs.items() if s != changed[0]):
            rslot = changed[0]
            if rslot != self._last_fast_reroll_slot or (now_mono - self._last_fast_reroll_mono) > 0.45:
                self._last_fast_reroll_slot = rslot
                self._last_fast_reroll_mono = now_mono
                return VisionFrameOutput(
                    vision_gen=ctx.vision_gen,
                    now_mono=now_mono,
                    game_rect=self._last_game_rect,
                    game_fg=True,
                    fast_vanish_mono=None,
                    fast_vanish_diffs=diffs,
                    capture_backend=str(getattr(capturer, "backend", cap.backend) or ""),
                    timings_ms=timings,
                    fast_reroll_slot=rslot,
                    fast_vanish_only=True,
                )
        return None

    # ------------------------------------------------------------------
    # Plan B HUD sync execution (runs on worker thread using vectorized BLAS matchers)
    # ------------------------------------------------------------------

    def _run_plan_b_scan(
        self,
        frame_bgr: np.ndarray,
        game_rect: WindowRect,
        ctx: VisionTickContext,
        *,
        force_confirm: bool = False,
    ) -> tuple[list[Any], dict[int, str], list[Any], float]:
        t0 = time.perf_counter()
        self._ensure_plan_b_matchers()
        item_hits: list[Any] = []
        item_states: dict[int, str] = {}
        aug_hits: list[Any] = []

        if (ctx.is_degraded_2999 or force_confirm) and self._item_icon_matcher is not None:
            if self._item_icon_matcher.template_count == 0:
                self._item_icon_matcher.reload_cached_icons()
            if self._item_icon_matcher.template_count > 0:
                raw_hits, item_states = self._item_icon_matcher.scan_hud_slots_with_state(
                    frame_bgr,
                    game_rect,
                    min_score=0.58 if force_confirm else 0.62,
                    min_margin=0.03,
                    hud_scale=ctx.hud_scale,
                )
                if ctx.suppressed_visual_items:
                    item_hits = [h for h in raw_hits if str(h.item_id) not in ctx.suppressed_visual_items]
                else:
                    item_hits = list(raw_hits)

        if (
            len(ctx.selected_augments) < 4
            and self._augment_icon_matcher is not None
            and self._augment_icon_matcher.template_count > 0
        ):
            raw_aug_hits = self._augment_icon_matcher.scan_all_hud_augments(
                frame_bgr,
                game_rect,
                min_score=0.58 if force_confirm else 0.62,
                min_margin=0.04,
                hud_scale=ctx.hud_scale,
            )
            if ctx.suppressed_visual_augments:
                aug_hits = [h for h in raw_aug_hits if str(h.augment_id) not in ctx.suppressed_visual_augments]
            else:
                aug_hits = list(raw_aug_hits)

        return item_hits, item_states, aug_hits, (time.perf_counter() - t0) * 1000.0

    # ------------------------------------------------------------------
    # Full vision tick (capture + death check + picker OCR + transmute/hover + Plan B)
    # ------------------------------------------------------------------

    def _execute_full_tick(
        self,
        ctx: VisionTickContext,
        *,
        force: bool = False,
        plan_b_force: bool = False,
    ) -> VisionFrameOutput | None:
        t_total0 = time.perf_counter()
        now_mono = self._clock_fn()

        if not force and not plan_b_force and ctx.game_state not in ("IN_GAME", "AUGMENT_PICKING"):
            return None

        game_rect = self._find_window_fn()
        game_fg = False
        if game_rect is None:
            if not force:
                if plan_b_force:
                    return VisionFrameOutput(
                        vision_gen=ctx.vision_gen,
                        now_mono=now_mono,
                        game_rect=None,
                        game_fg=False,
                        plan_b_forced=True,
                        error="no_game_window",
                    )
                return None
            game_rect = self._primary_rect_fn()
            game_fg = True
        else:
            game_fg = bool(self._is_foreground_fn(game_rect.hwnd))
            if not force and not plan_b_force and not game_fg:
                return None

        self._ensure_ocr_and_detector(ctx)
        capturer = self._ensure_capturer()

        t_cap0 = time.perf_counter()
        cap = capturer.grab(region=game_rect.as_bbox())
        capture_ms = (time.perf_counter() - t_cap0) * 1000.0
        backend = str(getattr(capturer, "backend", getattr(cap, "backend", "")) or "")
        consec_black = int(getattr(capturer, "consecutive_black_frames", 0) or 0)

        if cap is None or cap.is_black or cap.frame_bgr is None:
            total_ms = (time.perf_counter() - t_total0) * 1000.0
            return VisionFrameOutput(
                vision_gen=ctx.vision_gen,
                now_mono=now_mono,
                game_rect=game_rect,
                game_fg=game_fg,
                plan_b_forced=plan_b_force,
                capture_backend=backend,
                timings_ms={
                    "capture_ms": round(capture_ms, 3),
                    "detect_ms": 0.0,
                    "plan_b_ms": 0.0,
                    "passive_hover_ms": 0.0,
                    "total_ms": round(total_ms, 3),
                },
                error="black_frame" if plan_b_force else "",
                is_black=True,
                consecutive_black_frames=consec_black,
            )

        frame_bgr = cap.frame_bgr
        self._last_frame_bgr = frame_bgr
        self._last_game_rect = game_rect

        # Degraded-mode <0.25ms gray-screen death detector (WS-6)
        visual_is_dead: bool | None = None
        if ctx.is_degraded_2999:
            visual_is_dead = self._death_detector.update(frame_bgr, is_black=False)

        # Manual Plan B rescan fast path: skip picker OCR & hover, only run Plan B + death check
        if plan_b_force and not force:
            item_hits, item_states, aug_hits, plan_b_ms = self._run_plan_b_scan(
                frame_bgr, game_rect, ctx, force_confirm=True
            )
            self._last_plan_b_scan_mono = now_mono
            total_ms = (time.perf_counter() - t_total0) * 1000.0
            return VisionFrameOutput(
                vision_gen=ctx.vision_gen,
                now_mono=now_mono,
                game_rect=game_rect,
                game_fg=game_fg,
                plan_b_item_hits=item_hits,
                plan_b_item_states=item_states,
                plan_b_aug_hits=aug_hits,
                plan_b_forced=True,
                visual_is_dead=visual_is_dead,
                capture_backend=backend,
                timings_ms={
                    "capture_ms": round(capture_ms, 3),
                    "detect_ms": 0.0,
                    "plan_b_ms": round(plan_b_ms, 3),
                    "passive_hover_ms": 0.0,
                    "total_ms": round(total_ms, 3),
                },
                is_black=False,
                consecutive_black_frames=consec_black,
                plan_b_ran=True,
            )

        # 1. Augment picker detection
        picker_result: PickerDetectionResult | None = None
        roi_crops: dict[str, Any] = {}
        detect_ms = 0.0
        excl_list = list(ctx.exclude_rects)
        if self._detector is not None:
            t_det0 = time.perf_counter()
            want_crops = bool(ctx.devlab_active and ctx.devlab_roi_snapshots)
            try:
                picker_result = self._detector.process_frame(
                    frame_bgr,
                    game_rect=game_rect,
                    exclude_rects=excl_list,
                    force=force,
                    capture_ms=capture_ms,
                    capture_crops=want_crops,
                )
            except TypeError:
                picker_result = self._detector.process_frame(
                    frame_bgr,
                    game_rect=game_rect,
                    exclude_rects=excl_list,
                    force=force,
                )
            detect_ms = (time.perf_counter() - t_det0) * 1000.0
            diag = getattr(picker_result, "diagnostics", None)
            if isinstance(diag, dict) and "_roi_crops" in diag:
                extracted = diag.pop("_roi_crops", None)
                if isinstance(extracted, dict):
                    roi_crops = extracted

            # Maintain 60ms lightweight vanish references automatically
            if picker_result.picker_visible and picker_result.stable and len(picker_result.cards) >= 2:
                card_ids = tuple(str(c.augment.augment_id) for c in picker_result.cards)
                if card_ids != self._picker_ref_card_ids or not self._picker_slot_refs:
                    self._capture_picker_slot_refs(frame_bgr, game_rect, picker_result.cards)
            elif not picker_result.picker_visible:
                self._clear_picker_slot_refs()

        picker_visible = bool(picker_result is not None and picker_result.picker_visible)
        picker_card_count = len(picker_result.cards) if picker_result is not None else 0

        # 2. Active transmute or passive HUD hover (strictly guarded against active picker!)
        transmute_hits: list[Any] = []
        passive_hover_hit: Any = None
        transmute_trace: dict[str, Any] = {}
        passive_hover_ms = 0.0

        cursor_pos = ctx.cursor_pos if ctx.cursor_pos is not None else self._get_cursor_pos_fn()
        if (
            ctx.game_state == "IN_GAME"
            and not picker_visible
            and picker_card_count == 0
            and self._transmute_watcher is not None
        ):
            if ctx.pending_transmute_active and ctx.pending_transmute is not None:
                t_tr0 = time.perf_counter()
                transmute_hits = self._transmute_watcher.process_frame(
                    frame_bgr,
                    game_rect=game_rect,
                    pending=ctx.pending_transmute,
                    already_owned_ids=set(ctx.selected_augments),
                    exclude_rects=excl_list,
                    cursor_pos=cursor_pos,
                    now_mono=now_mono,
                )
                passive_hover_ms = (time.perf_counter() - t_tr0) * 1000.0
                tr_fn = getattr(self._transmute_watcher, "last_trace_diagnostics", None)
                if callable(tr_fn):
                    transmute_trace = dict(tr_fn())
            elif not ctx.pending_transmute_active and len(ctx.selected_augments) < 4:
                t_hov0 = time.perf_counter()
                passive_hover_hit = self._transmute_watcher.scan_passive_hud_hover(
                    frame_bgr,
                    game_rect=game_rect,
                    already_owned_ids=set(ctx.selected_augments),
                    exclude_rects=excl_list,
                    cursor_pos=cursor_pos,
                    now_mono=now_mono,
                )
                passive_hover_ms = (time.perf_counter() - t_hov0) * 1000.0
                tr_fn = getattr(self._transmute_watcher, "last_trace_diagnostics", None)
                if callable(tr_fn):
                    transmute_trace = dict(tr_fn())

        # 3. Periodic 2.5s Plan B HUD sync (or forced)
        plan_b_item_hits: list[Any] = []
        plan_b_item_states: dict[int, str] = {}
        plan_b_aug_hits: list[Any] = []
        plan_b_ms = 0.0
        plan_b_ran = False
        should_plan_b = plan_b_force or (
            ctx.game_state == "IN_GAME"
            and not ctx.pending_transmute_active
            and not picker_visible
            and (ctx.is_degraded_2999 or len(ctx.selected_augments) == 0)
            and ctx.game_time_s >= 10.0
            and (now_mono - self._last_plan_b_scan_mono) >= self._plan_b_interval_s
        )
        if should_plan_b:
            self._last_plan_b_scan_mono = now_mono
            plan_b_item_hits, plan_b_item_states, plan_b_aug_hits, plan_b_ms = self._run_plan_b_scan(
                frame_bgr, game_rect, ctx, force_confirm=plan_b_force
            )
            plan_b_ran = True

        total_ms = (time.perf_counter() - t_total0) * 1000.0
        return VisionFrameOutput(
            vision_gen=ctx.vision_gen,
            now_mono=now_mono,
            game_rect=game_rect,
            game_fg=game_fg,
            picker_result=picker_result,
            transmute_hits=transmute_hits,
            passive_hover_hit=passive_hover_hit,
            transmute_trace=transmute_trace,
            plan_b_item_hits=plan_b_item_hits,
            plan_b_item_states=plan_b_item_states,
            plan_b_aug_hits=plan_b_aug_hits,
            plan_b_forced=plan_b_force,
            visual_is_dead=visual_is_dead,
            capture_backend=backend,
            timings_ms={
                "capture_ms": round(capture_ms, 3),
                "detect_ms": round(detect_ms, 3),
                "plan_b_ms": round(plan_b_ms, 3),
                "passive_hover_ms": round(passive_hover_ms, 3),
                "total_ms": round(total_ms, 3),
            },
            roi_crops=roi_crops,
            is_black=False,
            consecutive_black_frames=consec_black,
            plan_b_ran=plan_b_ran,
        )

    def run_once(self, *, force: bool = False, plan_b_force: bool = False) -> VisionFrameOutput | None:
        """Execute a single synchronous vision tick using the current context (for tests / sync fallback)."""
        ctx = self.get_context()
        return self._execute_full_tick(ctx, force=force, plan_b_force=plan_b_force)

    # ------------------------------------------------------------------
    # Command dispatcher & persistent thread main loop
    # ------------------------------------------------------------------

    def _handle_command(self, cmd: str, payload: dict[str, Any]) -> bool:
        """Handle one queued control command. Returns True if worker should stop."""
        if cmd == "stop":
            return True

        ctx = self.get_context()
        if cmd == "reset_match":
            if self._detector is not None:
                self._detector.reset()
                self._detector.reset_match_metrics()
            if self._transmute_watcher is not None:
                self._transmute_watcher.reset()
            if self._item_icon_matcher is not None:
                clear_fn = getattr(self._item_icon_matcher, "clear_slot_cache", None)
                if callable(clear_fn):
                    clear_fn()
            if self._augment_icon_matcher is not None:
                clear_fn = getattr(self._augment_icon_matcher, "clear_slot_cache", None)
                if callable(clear_fn):
                    clear_fn()
            self._death_detector.reset()
            self._clear_picker_slot_refs()
            self._last_plan_b_scan_mono = 0.0
            self._last_frame_bgr = None
            return False

        if cmd == "update_rois":
            cb = payload.get("card_band_roi")
            tb = payload.get("title_band_roi")
            if cb is not None:
                self._card_band_roi = cb
            if tb is not None:
                self._title_band_roi = tb
            if self._detector is not None:
                self._detector.update_rois(self._card_band_roi, self._title_band_roi)
            self._clear_picker_slot_refs()
            return False

        if cmd == "update_catalog":
            augs = payload.get("augments")
            if isinstance(augs, (list, tuple)):
                self._augments = list(augs)
                if self._detector is not None:
                    self._detector.matcher.update_catalog(self._augments)
                if self._transmute_watcher is not None:
                    self._transmute_watcher.update_catalog(self._augments)
            return False

        if cmd == "reload_icons":
            self._ensure_plan_b_matchers()
            if self._item_icon_matcher is not None:
                self._item_icon_matcher.reload_cached_icons()
            if self._augment_icon_matcher is not None:
                self._augment_icon_matcher.reload_cached_icons()
            return False

        if cmd == "capture_picker_refs":
            cards = payload.get("cards")
            frame = payload.get("frame_bgr")
            rect = payload.get("game_rect")
            if frame is None:
                frame = self._last_frame_bgr
            if rect is None:
                rect = self._last_game_rect
            if cards:
                self._capture_picker_slot_refs(frame, rect, cards)
            return False

        if cmd == "force_rescan":
            out = self._execute_full_tick(ctx, force=True, plan_b_force=False)
            if out is not None:
                self._last_full_tick_mono = out.now_mono
                self._emit_output(out)
            return False

        if cmd == "plan_b_rescan":
            out = self._execute_full_tick(ctx, force=False, plan_b_force=True)
            if out is not None:
                self._emit_output(out)
            return False

        return False

    def _thread_entry(self) -> None:
        com_inited = False
        try:
            coinit_mode = _resolve_worker_coinit_mode()
            hr = int(ctypes.windll.ole32.CoInitializeEx(None, coinit_mode))
            com_inited = hr in (0, 1)  # S_OK or S_FALSE
        except Exception as exc:
            logger.debug("HexVisionWorker CoInitializeEx skipped: %s", exc)

        try:
            attach_default_desktop()
        except Exception as exc:
            logger.debug("HexVisionWorker attach_default_desktop skipped: %s", exc)

        try:
            self._run_loop()
        finally:
            self._close_owned_resources()
            if com_inited:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:
                    pass

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            should_stop = False
            for cmd, payload in self._pop_commands():
                try:
                    if self._handle_command(cmd, payload):
                        should_stop = True
                        break
                except Exception as exc:
                    logger.debug("HexVisionWorker command %s failed: %s", cmd, exc)
                    ctx = self.get_context()
                    self._emit_output(
                        VisionFrameOutput(
                            vision_gen=ctx.vision_gen,
                            now_mono=self._clock_fn(),
                            error=f"cmd_{cmd}:{exc}",
                        )
                    )
            if should_stop or self._stop_event.is_set():
                break

            ctx = self.get_context()
            now_mono = self._clock_fn()

            if ctx.game_state in ("IN_GAME", "AUGMENT_PICKING"):
                # Lightweight 60ms vanish/reroll band-grab check while AUGMENT_PICKING
                in_picking = ctx.game_state == "AUGMENT_PICKING"
                if (
                    in_picking
                    and self._picker_slot_refs
                    and not self._vanish_emitted
                    and (now_mono - self._last_vanish_tick_mono) >= self._fast_vanish_interval_s
                ):
                    self._last_vanish_tick_mono = now_mono
                    try:
                        vanish_out = self._run_fast_vanish_check(ctx, now_mono)
                        if vanish_out is not None:
                            self._emit_output(vanish_out)
                    except Exception as exc:
                        logger.debug("HexVisionWorker fast vanish check error: %s", exc)

                # Full 350ms vision tick
                if (now_mono - self._last_full_tick_mono) >= self._tick_interval_s:
                    self._last_full_tick_mono = now_mono
                    try:
                        out = self._execute_full_tick(ctx, force=False, plan_b_force=False)
                        if out is not None:
                            self._emit_output(out)
                    except Exception as exc:
                        logger.debug("HexVisionWorker tick failed: %s", exc)
                        self._emit_output(
                            VisionFrameOutput(
                                vision_gen=ctx.vision_gen,
                                now_mono=self._clock_fn(),
                                error=str(exc),
                            )
                        )

                # Compute exact sleep deadline until next 60ms vanish check or 350ms full tick
                after_mono = self._clock_fn()
                next_full = max(0.005, self._tick_interval_s - (after_mono - self._last_full_tick_mono))
                if in_picking and self._picker_slot_refs and not self._vanish_emitted:
                    next_vanish = max(0.005, self._fast_vanish_interval_s - (after_mono - self._last_vanish_tick_mono))
                    wait_s = min(next_full, next_vanish)
                else:
                    wait_s = next_full
            else:
                self._clear_picker_slot_refs()
                wait_s = self.IDLE_WAIT_INTERVAL_S

            self._wake_event.wait(timeout=wait_s)
            self._wake_event.clear()
