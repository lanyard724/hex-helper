"""F8 Interactive ROI Calibration Overlay for dragging the title-band scan box on any screen resolution."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from hex_helper.config import NormalizedRect


class CalibrationOverlay(QWidget):
    """Full-screen translucent overlay allowing the user to drag a red rectangle over the 3 card titles."""

    def __init__(
        self,
        initial_title_roi: NormalizedRect,
        on_calibrated: Callable[[NormalizedRect, NormalizedRect], None],
        on_calibration_finished: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__()
        self._initial = initial_title_roi
        self._on_calibrated = on_calibrated
        self._on_calibration_finished = on_calibration_finished
        self._origin: QPoint | None = None
        self._current_rect: QRect | None = None
        self._session_finished: bool = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _emit_finished(self, outcome: str, title_roi: NormalizedRect | None = None) -> None:
        if self._session_finished:
            return
        self._session_finished = True
        if self._on_calibration_finished is None:
            return
        screens = list(QGuiApplication.screens())
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.geometry()
            dpr = round(max(1.0, float(screen.devicePixelRatio() or 1.0)), 3)
            try:
                screen_idx = screens.index(screen)
            except ValueError:
                screen_idx = 0
            res = [int(geo.width()), int(geo.height())]
        else:
            dpr = 1.0
            screen_idx = 0
            res = [int(max(1, self.width())), int(max(1, self.height()))]
        custom_rect_norm = (
            [round(title_roi.x, 4), round(title_roi.y, 4), round(title_roi.w, 4), round(title_roi.h, 4)]
            if title_roi is not None
            else None
        )
        self._on_calibration_finished(
            {
                "outcome": outcome,
                "screen_index": int(screen_idx),
                "resolution": res,
                "dpr": dpr,
                "custom_rect_norm": custom_rect_norm,
            }
        )

    def open_calibrator(self) -> None:
        self._session_finished = False
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.geometry()
            self.setGeometry(geo)
            l, t, r, b = self._initial.to_pixels(geo.width(), geo.height())
            self._current_rect = QRect(l, t, r - l, b - t)
        self.showFullScreen()
        self.activateWindow()
        self.raise_()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(8, 12, 22, 130))

        painter.setPen(QColor("#FBBF24"))
        painter.drawText(
            32,
            48,
            "HexHelper ROI 校准：按住鼠标左键框选【三张海克斯卡片标题所在的横向条带】，松开自动保存；按 Esc 取消。",
        )

        if self._current_rect is not None:
            pen = QPen(QColor("#EF4444"), 3)
            painter.setPen(pen)
            painter.fillRect(self._current_rect, QColor(239, 68, 68, 40))
            painter.drawRect(self._current_rect)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current_rect = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._origin is not None:
            self._current_rect = QRect(self._origin, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._current_rect is not None:
            rect = self._current_rect.normalized()
            self._origin = None
            w = max(1, self.width())
            h = max(1, self.height())
            if rect.width() >= 120 and rect.height() >= 30:
                title_roi = NormalizedRect(
                    x=rect.left() / w,
                    y=rect.top() / h,
                    w=rect.width() / w,
                    h=rect.height() / h,
                ).clamp()
                card_roi = NormalizedRect(
                    x=title_roi.x,
                    y=max(0.05, title_roi.y - 0.08),
                    w=title_roi.w,
                    h=min(0.90, title_roi.h + 0.22),
                ).clamp()
                self._on_calibrated(card_roi, title_roi)
                self._emit_finished("saved", title_roi)
            else:
                self._emit_finished("cancelled", None)
            self.hide()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._emit_finished("cancelled", None)
            self.hide()
