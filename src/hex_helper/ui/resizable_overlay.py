"""Reusable frameless edge/corner resize controller & Hextech corner grip for in-game overlays."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget


class HextechCornerGrip(QWidget):
    """Subtle 14x14 Hextech gold diagonal resize handle anchored to an overlay corner."""

    def __init__(self, parent: QWidget, corner: str = "br") -> None:
        super().__init__(parent)
        self._corner = corner  # "br" | "bl"
        self.setFixedSize(14, 14)
        self.setCursor(
            Qt.CursorShape.SizeFDiagCursor
            if corner == "br"
            else Qt.CursorShape.SizeBDiagCursor
        )
        self.setToolTip("按住拖拽自由调整悬浮窗大小（右键可恢复默认尺寸）")
        self.setStyleSheet("background: transparent; border: none;")

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen_dim = QPen(QColor(200, 170, 110, 125), 1.3)
        pen_hi = QPen(QColor(10, 200, 185, 160), 1.3)
        w, h = self.width(), self.height()
        if self._corner == "br":
            p.setPen(pen_dim)
            p.drawLine(w - 11, h - 3, w - 3, h - 11)
            p.setPen(pen_hi)
            p.drawLine(w - 7, h - 3, w - 3, h - 7)
            p.drawPoint(w - 3, h - 3)
        else:
            p.setPen(pen_dim)
            p.drawLine(3, h - 11, 11, h - 3)
            p.setPen(pen_hi)
            p.drawLine(3, h - 7, 7, h - 3)
            p.drawPoint(3, h - 3)
        p.end()


class FramelessResizeController(QObject):
    """Intercepts border & corner mouse drags on frameless Qt6 overlays without stealing focus.

    Supports left, right, bottom, bottom-left, and bottom-right resizing, dynamic hover cursor
    feedback, screen-boundary clamping, and anchor-preserving left-edge drags.
    """

    EDGE_MARGIN = 7
    CORNER_MARGIN = 14

    def __init__(
        self,
        window: QWidget,
        *,
        min_w: int,
        max_w: int,
        min_h: int,
        max_h: int,
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
        self._can_resize = can_resize or (lambda: True)
        self._on_live_resize = on_live_resize
        self._on_resize_finished = on_resize_finished
        self._screen_provider = screen_provider

        self._active_edge: str = ""
        self._press_global: QPoint | None = None
        self._start_geo: QRect | None = None
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

    def hit_test_global(self, global_pt: QPoint) -> str:
        """Return edge token ('bl' | 'br' | 'l' | 'r' | 'b' | '') for a global point."""
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
        near_b = y >= h - self.EDGE_MARGIN

        in_bl = x <= self.CORNER_MARGIN and y >= h - self.CORNER_MARGIN
        in_br = x >= w - self.CORNER_MARGIN and y >= h - self.CORNER_MARGIN
        if in_bl:
            return "bl"
        if in_br:
            return "br"
        if near_l and near_b:
            return "bl"
        if near_r and near_b:
            return "br"
        if near_l:
            return "l"
        if near_r:
            return "r"
        if near_b:
            return "b"
        return ""

    @staticmethod
    def _cursor_for_edge(edge: str) -> Qt.CursorShape | None:
        if edge in ("l", "r"):
            return Qt.CursorShape.SizeHorCursor
        if edge == "b":
            return Qt.CursorShape.SizeVerCursor
        if edge == "bl":
            return Qt.CursorShape.SizeBDiagCursor
        if edge == "br":
            return Qt.CursorShape.SizeFDiagCursor
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

        return QRect(x, y, int(w), int(h))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        etype = event.type()
        if etype == QEvent.Type.HoverMove and not self._active_edge:
            if self._can_resize():
                self._apply_hover_cursor(self.hit_test_global(QCursor.pos()))
            else:
                self._win.unsetCursor()
            return False

        if etype == QEvent.Type.HoverLeave and not self._active_edge:
            self._win.unsetCursor()
            return False

        if etype == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            if event.button() == Qt.MouseButton.LeftButton and self._can_resize():
                gpos = event.globalPosition().toPoint()
                edge = self.hit_test_global(gpos)
                if edge:
                    self._active_edge = edge
                    self._press_global = gpos
                    self._start_geo = QRect(self._win.frameGeometry())
                    self._apply_hover_cursor(edge)
                    event.accept()
                    return True
            return False

        if etype == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            gpos = event.globalPosition().toPoint()
            if self._active_edge and self._start_geo is not None and self._press_global is not None:
                target = self.compute_target_rect(gpos)
                self._on_live_resize(target, self._active_edge)
                event.accept()
                return True
            if self._can_resize():
                self._apply_hover_cursor(self.hit_test_global(gpos))
            return False

        if etype == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            if self._active_edge and event.button() == Qt.MouseButton.LeftButton:
                gpos = event.globalPosition().toPoint()
                edge = self._active_edge
                target = self.compute_target_rect(gpos) if (self._start_geo and self._press_global) else self._win.frameGeometry()
                self._active_edge = ""
                self._press_global = None
                self._start_geo = None
                self._on_live_resize(target, edge)
                if self._on_resize_finished is not None:
                    self._on_resize_finished(target, edge)
                self._apply_hover_cursor(self.hit_test_global(gpos))
                event.accept()
                return True

        return False
