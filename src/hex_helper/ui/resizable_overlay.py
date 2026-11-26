"""Reusable frameless 8-edge/corner resize controller, Hextech handles, and 4K-adaptive scale metrics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QWidget

MIN_OVERLAY_SCALE: float = 0.85
MAX_OVERLAY_SCALE: float = 2.20
ALL_RESIZE_EDGES: frozenset[str] = frozenset({"l", "r", "t", "b", "tl", "tr", "bl", "br"})


@dataclass(frozen=True)
class OverlayScaleMetrics:
    """Proportional pixel and font-size scaling helper for adaptive in-game overlays."""

    scale: float = 1.0

    def px(self, base: int | float, *, floor: int = 1) -> int:
        return max(floor, int(round(float(base) * self.scale)))

    def pt(self, base: int | float, *, floor: int = 8) -> int:
        return max(floor, int(round(float(base) * self.scale)))

    def font_px(self, base: int | float, *, floor: int = 8) -> int:
        return max(floor, int(round(float(base) * self.scale)))


def compute_auto_overlay_scale(
    logical_w: int,
    logical_h: int | None = None,
    dpr: float = 1.0,
) -> float:
    """Compute automatic initial overlay scale for the active display.

    - 1080p @ 100% (1920x1080) and 4K @ 200% (1920x1080, dpr=2.0) stay at 1.0x.
    - 2K QHD @ 100% (2560x1440, dpr=1.0) auto-scales to ~1.24x (~456px drawer width).
    - 4K UHD @ 100% (3840x2160, dpr=1.0) scales to ~1.72x so fonts/icons are immediately legible.
    - 4K UHD @ 125% (3072x1728, dpr=1.25) scales to ~1.43x.
    - 4K UHD @ 150% (2560x1440, dpr=1.5) scales to ~1.24x.
    - Ultrawide (3440x1440) and 5K (5120x2880) scale smoothly up to 2.0x.
    """
    lw = max(800, int(logical_w or 1920))
    lh = max(600, int(logical_h)) if (logical_h is not None and int(logical_h) > 0) else max(600, int(round(lw * 9.0 / 16.0)))
    eff_dpr = max(1.0, float(dpr or 1.0))
    phys_w = lw * eff_dpr
    raw_ratio = min(lw / 1920.0, lh / 1080.0)

    if lw >= 2400 or lh >= 1350 or (phys_w >= 3200.0 and eff_dpr < 1.85 and raw_ratio > 1.05):
        scale = 1.0 + max(0.0, raw_ratio - 1.0) * 0.72
        return round(max(1.0, min(2.0, scale)), 2)
    return 1.0


def compute_overlay_scale(
    width: int,
    base_width: int,
    height: int | None = None,
    min_comfort_height: int | None = None,
    *,
    min_scale: float = MIN_OVERLAY_SCALE,
    max_scale: float = MAX_OVERLAY_SCALE,
) -> float:
    """Derive proportional UI scale from current expanded overlay dimensions."""
    bw = max(120.0, float(base_width))
    w_scale = float(max(120, int(width))) / bw
    scale = w_scale
    if height is not None and min_comfort_height is not None and int(height) > 0:
        h_cap = max(min_scale, float(height) / float(max(120, int(min_comfort_height))))
        scale = min(scale, h_cap)
    return round(max(min_scale, min(max_scale, scale)), 2)


class HextechCornerGrip(QWidget):
    """Subtle LoL V16.23 Hextech gold/cyan corner resize bracket supporting all 4 corners."""

    _CURSORS: dict[str, Qt.CursorShape] = {
        "tl": Qt.CursorShape.SizeFDiagCursor,
        "br": Qt.CursorShape.SizeFDiagCursor,
        "tr": Qt.CursorShape.SizeBDiagCursor,
        "bl": Qt.CursorShape.SizeBDiagCursor,
    }

    def __init__(
        self,
        parent: QWidget,
        corner: str = "br",
        *,
        on_double_click: Callable[[], None] | None = None,
        prominent: bool = True,
    ) -> None:
        super().__init__(parent)
        self._corner = corner if corner in self._CURSORS else "br"
        self._on_double_click = on_double_click
        self._prominent = bool(prominent)
        self._hovered = False
        self.setProperty("hex_resize_edge", self._corner)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.setFixedSize(16, 16)
        self.setCursor(self._CURSORS[self._corner])
        self.setToolTip("按住拖拽缩放悬浮窗与字号（双击或右键可恢复默认尺寸）")
        self.setStyleSheet("background: transparent; border: none;")

    def set_grip_size(self, size: int) -> None:
        sz = max(14, min(26, int(size)))
        if self.width() != sz or self.height() != sz:
            self.setFixedSize(sz, sz)
            self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._on_double_click is not None:
            self._on_double_click()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        base_alpha = 185 if self._prominent else 115
        gold_alpha = 245 if self._hovered else base_alpha
        cyan_alpha = 245 if self._hovered else (195 if self._prominent else 135)
        pen_gold = QPen(QColor(200, 170, 110, gold_alpha), 1.6 if self._hovered else 1.35)
        pen_cyan = QPen(QColor(10, 200, 185, cyan_alpha), 1.4 if self._hovered else 1.2)
        arm = max(6.5, min(11.0, w * 0.56))
        d1 = max(7.5, w * 0.62)
        d2 = max(4.5, w * 0.36)
        m = 2.0

        if self._corner == "br":
            p.setPen(pen_gold)
            p.drawLine(int(w - arm), int(h - m), int(w - m), int(h - m))
            p.drawLine(int(w - m), int(h - arm), int(w - m), int(h - m))
            p.setPen(pen_cyan)
            p.drawLine(int(w - d1), int(h - m - 1), int(w - m - 1), int(h - d1))
            p.drawLine(int(w - d2), int(h - m - 1), int(w - m - 1), int(h - d2))
        elif self._corner == "bl":
            p.setPen(pen_gold)
            p.drawLine(int(m), int(h - arm), int(m), int(h - m))
            p.drawLine(int(m), int(h - m), int(arm), int(h - m))
            p.setPen(pen_cyan)
            p.drawLine(int(m + 1), int(h - d1), int(d1), int(h - m - 1))
            p.drawLine(int(m + 1), int(h - d2), int(d2), int(h - m - 1))
        elif self._corner == "tl":
            p.setPen(pen_gold)
            p.drawLine(int(m), int(arm), int(m), int(m))
            p.drawLine(int(m), int(m), int(arm), int(m))
            p.setPen(pen_cyan)
            p.drawLine(int(m + 1), int(d1), int(d1), int(m + 1))
            p.drawLine(int(m + 1), int(d2), int(d2), int(m + 1))
        else:  # "tr"
            p.setPen(pen_gold)
            p.drawLine(int(w - arm), int(m), int(w - m), int(m))
            p.drawLine(int(w - m), int(m), int(w - m), int(arm))
            p.setPen(pen_cyan)
            p.drawLine(int(w - d1), int(m + 1), int(w - m - 1), int(d1))
            p.drawLine(int(w - d2), int(m + 1), int(w - m - 1), int(d2))
        p.end()


class HextechEdgeHandle(QWidget):
    """Dedicated edge resize hit-strip with an optional centered Hextech metallic grip notch."""

    def __init__(
        self,
        parent: QWidget,
        edge: str,
        *,
        show_notch: bool = False,
        on_double_click: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._edge = edge if edge in ("l", "r", "t", "b") else "l"
        self._show_notch = bool(show_notch)
        self._on_double_click = on_double_click
        self._hovered = False
        self.setProperty("hex_resize_edge", self._edge)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.setCursor(
            Qt.CursorShape.SizeHorCursor
            if self._edge in ("l", "r")
            else Qt.CursorShape.SizeVerCursor
        )
        if self._show_notch:
            self.setToolTip("按住左右拖拽缩放悬浮窗与字体大小（双击恢复默认尺寸）")
        self.setStyleSheet("background: transparent; border: none;")

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hovered = True
        if self._show_notch:
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        if self._show_notch:
            self.update()
        super().leaveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._on_double_click is not None:
            self._on_double_click()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if not self._show_notch:
            return
        w, h = float(self.width()), float(self.height())
        if w < 4 or h < 24:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._edge in ("l", "r"):
            notch_h = min(max(30.0, h * 0.14), 68.0)
            notch_w = 3.0 if not self._hovered else 3.6
            cy = h * 0.5
            nx = 1.5 if self._edge == "l" else (w - notch_w - 1.5)
            ny = cy - notch_h * 0.5
            fill_c = QColor(10, 200, 185, 190 if self._hovered else 78)
            rim_c = QColor(200, 170, 110, 245 if self._hovered else 155)
            p.setPen(QPen(rim_c, 1.0))
            p.setBrush(fill_c)
            p.drawRoundedRect(QRectF(nx, ny, notch_w, notch_h), 1.5, 1.5)
            # Center cyan micro-accent pip
            pip_c = QColor(124, 248, 238, 245 if self._hovered else 180)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pip_c)
            p.drawEllipse(QRectF(nx + 0.3, cy - 1.6, max(2.2, notch_w - 0.6), 3.2))
        else:
            notch_w = min(max(30.0, w * 0.14), 68.0)
            notch_h = 3.0 if not self._hovered else 3.6
            cx = w * 0.5
            nx = cx - notch_w * 0.5
            ny = 1.5 if self._edge == "t" else (h - notch_h - 1.5)
            fill_c = QColor(10, 200, 185, 190 if self._hovered else 78)
            rim_c = QColor(200, 170, 110, 245 if self._hovered else 155)
            p.setPen(QPen(rim_c, 1.0))
            p.setBrush(fill_c)
            p.drawRoundedRect(QRectF(nx, ny, notch_w, notch_h), 1.5, 1.5)
        p.end()


class FramelessResizeController(QObject):
    """Intercepts 8-direction border & corner mouse drags on frameless Qt6 overlays without stealing focus.

    Supports left, right, top, bottom, and all 4 corner resizing, dynamic hover cursor
    feedback, screen-boundary clamping, and anchor-preserving left/top-edge drags.
    """

    EDGE_MARGIN = 8
    CORNER_MARGIN = 16

    def __init__(
        self,
        window: QWidget,
        *,
        min_w: int,
        max_w: int,
        min_h: int,
        max_h: int,
        allowed_edges: set[str] | frozenset[str] | tuple[str, ...] | None = None,
        can_resize: Callable[[], bool] | None = None,
        on_live_resize: Callable[[QRect, str], None],
        on_resize_finished: Callable[[QRect, str], None] | None = None,
        screen_provider: Callable[[], object | None] | None = None,
    ) -> None:
        super().__init__(window)
        self._win = window
        self.min_w = int(min_w)
        self.max_w = int(max_w)
        self.min_h = int(min_h)
        self.max_h = int(max_h)
        self._allowed_edges: frozenset[str] = (
            frozenset(e for e in allowed_edges if e in ALL_RESIZE_EDGES)
            if allowed_edges is not None
            else ALL_RESIZE_EDGES
        )
        self._can_resize = can_resize or (lambda: True)
        self._on_live_resize = on_live_resize
        self._on_resize_finished = on_resize_finished
        self._screen_provider = screen_provider

        self._active_edge: str = ""
        self._press_global: QPoint | None = None
        self._start_geo: QRect | None = None
        self._did_move: bool = False
        self._installed_on: list[QWidget] = []

        self._win.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._win.setMouseTracking(True)
        self.attach(self._win)

    @property
    def is_resizing(self) -> bool:
        return bool(self._active_edge)

    def attach(self, widget: QWidget) -> None:
        if widget in self._installed_on:
            return
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        self._installed_on.append(widget)

    def _filter_allowed(self, edge: str) -> str:
        return edge if edge in self._allowed_edges else ""

    def hit_test_global(self, global_pt: QPoint) -> str:
        """Return edge token ('tl' | 'tr' | 'bl' | 'br' | 'l' | 'r' | 't' | 'b' | '') for a global point."""
        if not self._win.isVisible() or not self._can_resize():
            return ""
        local = self._win.mapFromGlobal(global_pt)
        w = self._win.width()
        h = self._win.height()
        x, y = local.x(), local.y()
        if x < -2 or y < -2 or x > w + 2 or y > h + 2:
            return ""

        near_l = x <= self.EDGE_MARGIN
        near_r = x >= w - self.EDGE_MARGIN
        near_t = y <= self.EDGE_MARGIN
        near_b = y >= h - self.EDGE_MARGIN

        in_tl = x <= self.CORNER_MARGIN and y <= self.CORNER_MARGIN
        in_tr = x >= w - self.CORNER_MARGIN and y <= self.CORNER_MARGIN
        in_bl = x <= self.CORNER_MARGIN and y >= h - self.CORNER_MARGIN
        in_br = x >= w - self.CORNER_MARGIN and y >= h - self.CORNER_MARGIN

        if in_tl and "tl" in self._allowed_edges:
            return "tl"
        if in_tr and "tr" in self._allowed_edges:
            return "tr"
        if in_bl and "bl" in self._allowed_edges:
            return "bl"
        if in_br and "br" in self._allowed_edges:
            return "br"
        if near_l and near_t and "tl" in self._allowed_edges:
            return "tl"
        if near_r and near_t and "tr" in self._allowed_edges:
            return "tr"
        if near_l and near_b and "bl" in self._allowed_edges:
            return "bl"
        if near_r and near_b and "br" in self._allowed_edges:
            return "br"
        if near_l and "l" in self._allowed_edges:
            return "l"
        if near_r and "r" in self._allowed_edges:
            return "r"
        if near_t and "t" in self._allowed_edges:
            return "t"
        if near_b and "b" in self._allowed_edges:
            return "b"
        return ""

    @staticmethod
    def _cursor_for_edge(edge: str) -> Qt.CursorShape | None:
        if edge in ("l", "r"):
            return Qt.CursorShape.SizeHorCursor
        if edge in ("t", "b"):
            return Qt.CursorShape.SizeVerCursor
        if edge in ("tl", "br"):
            return Qt.CursorShape.SizeFDiagCursor
        if edge in ("tr", "bl"):
            return Qt.CursorShape.SizeBDiagCursor
        return None

    def _apply_hover_cursor(self, edge: str) -> None:
        shape = self._cursor_for_edge(edge)
        if shape is not None:
            self._win.setCursor(shape)
        else:
            self._win.unsetCursor()

    def _screen_bounds(self) -> QRect | None:
        scr = None
        if self._screen_provider is not None:
            scr = self._screen_provider()
        if scr is None:
            scr = QGuiApplication.screenAt(self._win.frameGeometry().center()) or QGuiApplication.primaryScreen()
        return scr.geometry() if scr is not None else None

    def compute_target_rect(self, global_pt: QPoint) -> QRect:
        assert self._start_geo is not None and self._press_global is not None
        dx = global_pt.x() - self._press_global.x()
        dy = global_pt.y() - self._press_global.y()

        orig = self._start_geo
        x = orig.x()
        y = orig.y()
        w = orig.width()
        h = orig.height()

        bounds = self._screen_bounds()
        eff_max_w = min(self.max_w, bounds.width() - 24) if bounds is not None else self.max_w
        eff_max_h = min(self.max_h, bounds.height() - 32) if bounds is not None else self.max_h

        if "r" in self._active_edge:
            w = max(self.min_w, min(eff_max_w, orig.width() + dx))
            if bounds is not None and x + w > bounds.right() + 1:
                w = max(self.min_w, bounds.right() + 1 - x)
        elif "l" in self._active_edge:
            right_anchor = orig.x() + orig.width()
            w = max(self.min_w, min(eff_max_w, orig.width() - dx))
            x = right_anchor - w
            if bounds is not None and x < bounds.left():
                x = bounds.left()
                w = max(self.min_w, min(eff_max_w, right_anchor - x))

        if "b" in self._active_edge:
            h = max(self.min_h, min(eff_max_h, orig.height() + dy))
            if bounds is not None and y + h > bounds.bottom() + 1:
                h = max(self.min_h, bounds.bottom() + 1 - y)
        elif "t" in self._active_edge:
            bottom_anchor = orig.y() + orig.height()
            h = max(self.min_h, min(eff_max_h, orig.height() - dy))
            y = bottom_anchor - h
            if bounds is not None and y < bounds.top():
                y = bounds.top()
                h = max(self.min_h, min(eff_max_h, bottom_anchor - y))

        return QRect(int(x), int(y), int(w), int(h))

    def _edge_for_watched(self, watched: QObject, global_pt: QPoint) -> str:
        if not self._win.isVisible() or not self._can_resize():
            return ""
        prop = watched.property("hex_resize_edge")
        if isinstance(prop, str) and prop:
            allowed = self._filter_allowed(prop)
            if allowed:
                return allowed
        return self.hit_test_global(global_pt)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        etype = event.type()
        if etype == QEvent.Type.HoverMove and not self._active_edge:
            if self._can_resize():
                self._apply_hover_cursor(self._edge_for_watched(watched, QCursor.pos()))
            else:
                self._win.unsetCursor()
            return False

        if etype == QEvent.Type.HoverLeave and not self._active_edge:
            self._win.unsetCursor()
            return False

        if etype == QEvent.Type.MouseButtonDblClick:
            # Cancel any pending press state so double-clicking a grip cleanly resets size
            self._active_edge = ""
            self._press_global = None
            self._start_geo = None
            self._did_move = False
            return False

        if etype == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.LeftButton and self._can_resize():
                gpos = event.globalPosition().toPoint()
                edge = self._edge_for_watched(watched, gpos)
                if edge:
                    self._active_edge = edge
                    self._press_global = gpos
                    self._start_geo = QRect(self._win.frameGeometry())
                    self._did_move = False
                    self._apply_hover_cursor(edge)
                    event.accept()
                    return True
            return False

        if etype == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            gpos = event.globalPosition().toPoint()
            if self._active_edge and self._start_geo is not None and self._press_global is not None:
                if (gpos - self._press_global).manhattanLength() >= 2:
                    self._did_move = True
                if self._did_move:
                    target = self.compute_target_rect(gpos)
                    self._on_live_resize(target, self._active_edge)
                event.accept()
                return True
            if self._can_resize():
                self._apply_hover_cursor(self._edge_for_watched(watched, gpos))
            return False

        if etype == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            if self._active_edge and event.button() == Qt.MouseButton.LeftButton:
                gpos = event.globalPosition().toPoint()
                edge = self._active_edge
                moved = self._did_move
                target = (
                    self.compute_target_rect(gpos)
                    if (moved and self._start_geo and self._press_global)
                    else self._win.frameGeometry()
                )
                self._active_edge = ""
                self._press_global = None
                self._start_geo = None
                self._did_move = False
                if moved:
                    self._on_live_resize(target, edge)
                    if self._on_resize_finished is not None:
                        self._on_resize_finished(target, edge)
                self._apply_hover_cursor(self._edge_for_watched(watched, gpos))
                event.accept()
                return True

        return False
