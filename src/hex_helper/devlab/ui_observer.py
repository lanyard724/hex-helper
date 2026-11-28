"""Lightweight per-widget Qt UI observers for DevLab UX telemetry.

Strictly attaches ONLY to individual overlay QWidgets (Show/Hide events only)
and specific QScrollBars (in-memory value/overflow aggregation). Never installs
a global ``QApplication.installEventFilter`` and never emits during scroll/drag.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QScrollBar, QWidget

from hex_helper.devlab.schema import (
    DOMAIN_UI,
    EV_UI_OVERLAY_SESSION,
    EV_UI_SCROLL_OVERFLOW,
)

_RAPID_DISMISS_MS = 2500
_GHOST_REPOPUP_MS = 2000
_SCROLL_RANGE_DEBOUNCE_MS = 150
_SCROLL_OVERFLOW_THRESHOLD_PX = 8
_USER_CLOSE_SOURCES = frozenset({"user_close", "hotkey_hide", "escape"})
_USER_OPEN_SOURCES = frozenset({"tray_show", "hotkey_show", "lookup_btn"})
_USER_OPEN_PREFIXES = ("manual", "hotkey", "tray", "user", "lookup")
_RAPID_DISMISS_CLOSE_SOURCES = frozenset({"user_close", "hotkey_hide", "escape", "auto_or_unknown"})


def _is_user_open_source(source: str) -> bool:
    low = (source or "").strip().lower()
    if not low:
        return False
    return low in _USER_OPEN_SOURCES or low.startswith(_USER_OPEN_PREFIXES)


class ScrollOverflowTracker(QObject):
    """Tracks scroll depth & settled overflow purely in memory; emits once when flushed."""

    def __init__(
        self,
        name: str,
        scrollbar: QScrollBar,
        recorder: Any,
        *,
        overlay_name: str = "",
        debounce_ms: int = _SCROLL_RANGE_DEBOUNCE_MS,
        overflow_threshold_px: int = _SCROLL_OVERFLOW_THRESHOLD_PX,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.name = str(name)
        self.overlay_name = str(overlay_name or "")
        self._scrollbar = scrollbar
        self._recorder = recorder
        self._debounce_ms = max(0, int(debounce_ms))
        self._overflow_threshold_px = max(0, int(overflow_threshold_px))

        self._max_scroll_px = 0
        self._max_overflow_px = 0
        self._scroll_events_count = 0
        self._shown_mono: float | None = None
        self._connected = False

        self._range_debounce_timer = QTimer(self)
        self._range_debounce_timer.setSingleShot(True)
        self._range_debounce_timer.timeout.connect(self._on_range_debounce_timeout)

        try:
            scrollbar.valueChanged.connect(self._on_value_changed)
            scrollbar.rangeChanged.connect(self._on_range_changed)
            self._connected = True
        except Exception:
            self._connected = False

    def note_overlay_shown(self, now_mono: float | None = None) -> None:
        """Mark overlay show timestamp to suppress <150ms post-show layout transition spikes."""
        self._shown_mono = time.monotonic() if now_mono is None else float(now_mono)
        self._max_overflow_px = 0
        try:
            if self._range_debounce_timer.isActive():
                self._range_debounce_timer.stop()
        except RuntimeError:
            pass

    def _record_settled_overflow(self, maximum: int) -> None:
        mx = max(0, int(maximum))
        if mx > self._overflow_threshold_px and mx > self._max_overflow_px:
            self._max_overflow_px = mx

    def _on_range_debounce_timeout(self) -> None:
        try:
            cur_max = max(0, int(self._scrollbar.maximum()))
        except RuntimeError:
            return
        self._record_settled_overflow(cur_max)

    def _on_value_changed(self, value: int) -> None:
        v = max(0, int(value))
        if v > self._max_scroll_px:
            self._max_scroll_px = v
        try:
            mx = max(0, int(self._scrollbar.maximum()))
            self._record_settled_overflow(mx)
        except RuntimeError:
            pass
        self._scroll_events_count += 1

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._debounce_ms <= 0:
            self._record_settled_overflow(maximum)
            return
        # Trailing 150ms debounce filters 1-frame Qt show/resize layout squeeze spikes
        try:
            self._range_debounce_timer.start(self._debounce_ms)
        except RuntimeError:
            pass

    def flush_session(
        self,
        overlay_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Emit a single ``ui_scroll_overflow_session`` if settled overflow (>8px) or scroll occurred."""
        try:
            if self._range_debounce_timer.isActive():
                self._range_debounce_timer.stop()
        except RuntimeError:
            pass

        in_show_grace = (
            self._shown_mono is not None
            and self._debounce_ms > 0
            and (time.monotonic() - self._shown_mono) * 1000.0 < self._debounce_ms
        )
        if not in_show_grace or self._scroll_events_count > 0:
            try:
                cur_max = max(0, int(self._scrollbar.maximum()))
                self._record_settled_overflow(cur_max)
            except RuntimeError:
                pass

        max_scroll = self._max_scroll_px
        max_overflow = self._max_overflow_px
        scroll_count = self._scroll_events_count

        self._max_scroll_px = 0
        self._max_overflow_px = 0
        self._scroll_events_count = 0
        self._shown_mono = None

        has_overflow = max_overflow > self._overflow_threshold_px
        has_scroll = scroll_count > 0 and max_scroll > 0
        if not has_overflow and not has_scroll:
            return False

        payload: dict[str, Any] = {
            "overlay": str(overlay_name or self.overlay_name or self.name),
            "scroll_area": self.name,
            "max_scroll_px": max_scroll,
            "max_overflow_px": max_overflow,
            "scroll_events_count": scroll_count,
            "scrolled": has_scroll,
        }
        if context:
            payload["context"] = dict(context)

        if self._recorder is not None:
            self._recorder.emit(DOMAIN_UI, EV_UI_SCROLL_OVERFLOW, payload)
        return True

    def uninstall(self) -> None:
        try:
            if self._range_debounce_timer.isActive():
                self._range_debounce_timer.stop()
        except RuntimeError:
            pass
        if not self._connected:
            return
        self._connected = False
        try:
            self._scrollbar.valueChanged.disconnect(self._on_value_changed)
        except (RuntimeError, TypeError):
            pass
        try:
            self._scrollbar.rangeChanged.disconnect(self._on_range_changed)
        except (RuntimeError, TypeError):
            pass


class OverlayVisibilityFilter(QObject):
    """Per-widget Show/Hide event filter computing exact dwell time & rapid-dismiss friction."""

    def __init__(
        self,
        overlay_name: str,
        recorder: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.overlay_name = str(overlay_name)
        self._recorder = recorder

        self._is_visible = False
        self._shown_mono: float | None = None
        self._active_open_source = "auto_or_unknown"
        self._pending_open_source = ""
        self._pending_close_source = ""
        self._last_user_close_mono: float | None = None
        self._ghost_repopup = False
        self._ms_since_last_user_close: int | None = None
        self._linked_scrolls: list[tuple[ScrollOverflowTracker, Callable[[], dict[str, Any]] | None]] = []

    def mark_open_source(self, source: str) -> None:
        """Annotate the trigger source for the current or upcoming Show event."""
        clean = str(source or "").strip()
        if not clean:
            return
        self._pending_open_source = clean
        if self._is_visible and self._active_open_source == "auto_or_unknown":
            self._active_open_source = clean
            if _is_user_open_source(clean):
                self._ghost_repopup = False

    def mark_close_source(self, source: str) -> None:
        """Annotate the close source for the upcoming Hide event."""
        clean = str(source or "").strip()
        if clean:
            self._pending_close_source = clean

    def attach_scroll_tracker(
        self,
        tracker: ScrollOverflowTracker,
        context_fn: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """Flush ``tracker`` automatically whenever this overlay hides."""
        self._linked_scrolls.append((tracker, context_fn))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        ev_type = event.type()
        if ev_type == QEvent.Type.Show:
            if not self._is_visible:
                now = time.monotonic()
                self._is_visible = True
                self._shown_mono = now
                self._active_open_source = self._pending_open_source or "auto_or_unknown"
                self._pending_open_source = ""

                for tracker, _ctx_fn in self._linked_scrolls:
                    tracker.note_overlay_shown(now)

                if self._last_user_close_mono is not None:
                    delta_ms = max(0, int((now - self._last_user_close_mono) * 1000))
                    self._ms_since_last_user_close = delta_ms
                    self._ghost_repopup = (
                        delta_ms < _GHOST_REPOPUP_MS
                        and not _is_user_open_source(self._active_open_source)
                    )
                else:
                    self._ms_since_last_user_close = None
                    self._ghost_repopup = False
            return False

        if ev_type == QEvent.Type.Hide:
            if self._is_visible and self._shown_mono is not None:
                now = time.monotonic()
                dwell_ms = max(0, int((now - self._shown_mono) * 1000))
                open_source = self._active_open_source
                close_source = self._pending_close_source or "auto_or_unknown"
                self._pending_close_source = ""

                if close_source in _USER_CLOSE_SOURCES:
                    self._last_user_close_mono = now

                rapid_dismiss = (
                    dwell_ms < _RAPID_DISMISS_MS and close_source in _RAPID_DISMISS_CLOSE_SOURCES
                )
                ghost_repopup = self._ghost_repopup and not _is_user_open_source(open_source)
                ms_since_close = self._ms_since_last_user_close

                self._is_visible = False
                self._shown_mono = None
                self._active_open_source = "auto_or_unknown"
                self._ghost_repopup = False
                self._ms_since_last_user_close = None

                # Flush any attached scroll overflow trackers before emitting hide session
                for tracker, ctx_fn in self._linked_scrolls:
                    ctx = None
                    if ctx_fn is not None:
                        try:
                            ctx = ctx_fn()
                        except Exception:
                            ctx = None
                    tracker.flush_session(overlay_name=self.overlay_name, context=ctx)

                payload: dict[str, Any] = {
                    "overlay": self.overlay_name,
                    "dwell_ms": dwell_ms,
                    "open_source": open_source,
                    "close_source": close_source,
                    "rapid_dismiss": rapid_dismiss,
                    "ghost_repopup": ghost_repopup,
                }
                if ms_since_close is not None:
                    payload["ms_since_last_user_close"] = ms_since_close

                if self._recorder is not None:
                    self._recorder.emit(DOMAIN_UI, EV_UI_OVERLAY_SESSION, payload)
            return False

        return False


@dataclass
class DevLabUiObserverBundle:
    """Managed collection of per-widget overlay filters and scrollbar trackers."""

    overlay_filters: dict[str, OverlayVisibilityFilter] = field(default_factory=dict)
    scroll_trackers: dict[str, ScrollOverflowTracker] = field(default_factory=dict)
    _overlays: dict[str, QWidget] = field(default_factory=dict)

    def mark_open(self, name: str, source: str) -> None:
        flt = self.overlay_filters.get(name)
        if flt is not None:
            flt.mark_open_source(source)

    def mark_close(self, name: str, source: str) -> None:
        flt = self.overlay_filters.get(name)
        if flt is not None:
            flt.mark_close_source(source)

    def flush_scroll(
        self,
        name: str,
        overlay_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> bool:
        tracker = self.scroll_trackers.get(name)
        if tracker is None:
            return False
        return tracker.flush_session(overlay_name=overlay_name, context=context)

    def flush_all_scrolls(
        self,
        overlay_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        emitted = 0
        for tracker in self.scroll_trackers.values():
            if overlay_name and tracker.overlay_name and tracker.overlay_name != overlay_name:
                continue
            if tracker.flush_session(overlay_name=overlay_name, context=context):
                emitted += 1
        return emitted

    def uninstall(self) -> None:
        for name, flt in list(self.overlay_filters.items()):
            widget = self._overlays.get(name)
            if widget is not None:
                try:
                    widget.removeEventFilter(flt)
                except RuntimeError:
                    pass
        for tracker in list(self.scroll_trackers.values()):
            tracker.uninstall()
        self.overlay_filters.clear()
        self.scroll_trackers.clear()
        self._overlays.clear()


def _resolve_owner_overlay(
    scroll_key: str,
    scrollbar: QScrollBar,
    overlays: dict[str, QWidget],
) -> str:
    for ov_name in overlays:
        if scroll_key == ov_name or scroll_key.startswith(f"{ov_name}.") or scroll_key.startswith(f"{ov_name}_"):
            return ov_name
    try:
        top_window = scrollbar.window()
        for ov_name, widget in overlays.items():
            if widget is top_window or (widget is not None and widget.isAncestorOf(scrollbar)):
                return ov_name
    except RuntimeError:
        pass
    return ""


def install_devlab_ui_observers(
    overlays: dict[str, QWidget],
    scrollbars: dict[str, QScrollBar],
    recorder: Any,
) -> DevLabUiObserverBundle:
    """Install per-widget visibility filters and scrollbar overflow trackers."""
    bundle = DevLabUiObserverBundle()
    valid_overlays: dict[str, QWidget] = {}

    for name, widget in (overlays or {}).items():
        if widget is None:
            continue
        flt = OverlayVisibilityFilter(name, recorder, parent=widget)
        widget.installEventFilter(flt)
        bundle.overlay_filters[name] = flt
        bundle._overlays[name] = widget
        valid_overlays[name] = widget

    for sname, sb in (scrollbars or {}).items():
        if sb is None:
            continue
        owner_ov = _resolve_owner_overlay(sname, sb, valid_overlays)
        tracker = ScrollOverflowTracker(sname, sb, recorder, overlay_name=owner_ov, parent=sb)
        bundle.scroll_trackers[sname] = tracker
        if owner_ov and owner_ov in bundle.overlay_filters:
            bundle.overlay_filters[owner_ov].attach_scroll_tracker(tracker)

    return bundle
