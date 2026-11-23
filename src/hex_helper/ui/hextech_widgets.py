"""Artisan League of Legends V16.23 Hextech custom QPainter widgets (zero software-blur lag, 60fps animations)."""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    Qt,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QAbstractButton, QFrame

from hex_helper.ui.theme import display_font, latin_serif_font, load_app_pixmap


def attach_hextech_shadow(widget, **_kwargs) -> None:
    """No-op shim: shadows are now directly hardware-friendly QPainter layered strokes (0ms CPU blur cost)."""
    return None


class _AnimatedHextechButton(QAbstractButton):
    """Base button with smooth 60fps hover glow, press sink, and 45-degree specular light sheen."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        self._hover_t: float = 0.0
        self._press_t: float = 0.0
        self._sheen_t: float = -0.4

        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(165)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_val)

        self._sheen_anim = QVariantAnimation(self)
        self._sheen_anim.setDuration(460)
        self._sheen_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._sheen_anim.valueChanged.connect(self._on_sheen_val)

    def _on_hover_val(self, val) -> None:
        self._hover_t = float(val)
        self.update()

    def _on_sheen_val(self, val) -> None:
        self._sheen_t = float(val)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.start()

        self._sheen_anim.stop()
        self._sheen_anim.setStartValue(-0.35)
        self._sheen_anim.setEndValue(1.35)
        self._sheen_anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()
        self._press_t = 0.0
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_t = 1.0
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._press_t = 0.0
        self.update()
        super().mouseReleaseEvent(event)

    def _draw_sheen(self, p: QPainter, clip_path: QPainterPath, rect: QRectF, tint: QColor) -> None:
        if self._sheen_t <= -0.3 or self._sheen_t >= 1.3:
            return
        p.save()
        p.setClipPath(clip_path)
        cx = rect.left() + rect.width() * self._sheen_t
        band_w = rect.width() * 0.28
        grad = QLinearGradient(cx - band_w, rect.top(), cx + band_w, rect.bottom())
        c_mid = QColor(tint.red(), tint.green(), tint.blue(), int(78 * max(0.35, self._hover_t)))
        c_zero = QColor(tint.red(), tint.green(), tint.blue(), 0)
        grad.setColorAt(0.0, c_zero)
        grad.setColorAt(0.5, c_mid)
        grad.setColorAt(1.0, c_zero)
        p.fillRect(rect, QBrush(grad))
        p.restore()


class HextechPlayButton(_AnimatedHextechButton):
    """Pixel-crafted replica of the League of Legends Client top-left Hextech PLAY chevron button."""

    def __init__(self, text: str = "PLAY", parent=None) -> None:
        super().__init__(text, parent)
        self.setFixedSize(192, 50)
        self._emblem = load_app_pixmap(44)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.42)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w, h = float(self.width()), float(self.height())
        t = self._hover_t
        sink = 1.0 if self._press_t > 0.5 else 0.0

        # 1. Outer recessed antique-bronze housing rectangle behind right wing
        frame_rect = QRectF(26.0, 5.0 + sink * 0.5, w - 28.0, h - 10.0)
        p.fillRect(frame_rect, QColor("#040C14"))
        p.setPen(QPen(QColor("#5C461E" if t < 0.5 else "#9A7738"), 1.0))
        p.drawRect(frame_rect)

        # 2. Inner dark ghost shadow chevron nested inside right end
        ghost = QPainterPath()
        gx0 = frame_rect.right() - 26.0
        gy_top = frame_rect.top() + 3.0
        gy_bot = frame_rect.bottom() - 3.0
        gy_mid = (gy_top + gy_bot) * 0.5
        ghost.moveTo(gx0, gy_top)
        ghost.lineTo(frame_rect.right() - 4.0, gy_top)
        ghost.lineTo(frame_rect.right() - 4.0, gy_bot)
        ghost.lineTo(gx0, gy_bot)
        ghost.lineTo(frame_rect.right() - 12.0, gy_mid)
        ghost.closeSubpath()
        p.fillPath(ghost, QColor(12, 34, 46, int(170 + 50 * t)))
        p.setPen(QPen(QColor(22, 74, 92, int(155 + 75 * t)), 1.2))
        p.drawPath(ghost)

        # 3. Main sculpted chevron body with left concave arc hugging the circular medallion
        cy = h * 0.5 + sink * 0.5
        top_y = 8.5 + sink * 0.5
        bot_y = h - 8.5 + sink * 0.5
        tip_x = frame_rect.right() - 8.0
        shoulder_x = tip_x - 16.0
        arc_left_x = 40.0

        chevron = QPainterPath()
        chevron.moveTo(arc_left_x, top_y)
        chevron.lineTo(shoulder_x, top_y)
        chevron.lineTo(tip_x, cy)
        chevron.lineTo(shoulder_x, bot_y)
        chevron.lineTo(arc_left_x, bot_y)
        chevron.quadTo(arc_left_x + 13.0, cy, arc_left_x, top_y)
        chevron.closeSubpath()

        if t > 0.02:
            p.setPen(QPen(QColor(10, 200, 185, int(68 * t)), 5.5))
            p.drawPath(chevron)

        fill = QLinearGradient(0.0, top_y, 0.0, bot_y)
        fill.setColorAt(0.0, QColor(int(34 + 10 * t), int(44 + 34 * t), int(54 + 44 * t)))
        fill.setColorAt(0.5, QColor(int(24 + 8 * t), int(31 + 28 * t), int(40 + 36 * t)))
        fill.setColorAt(1.0, QColor(int(17 + 5 * t), int(23 + 20 * t), int(30 + 28 * t)))
        p.fillPath(chevron, QBrush(fill))

        if t > 0.02:
            rad = QRadialGradient(QPointF((arc_left_x + tip_x) * 0.5, cy), (tip_x - arc_left_x) * 0.55)
            rad.setColorAt(0.0, QColor(10, 200, 185, int(74 * t)))
            rad.setColorAt(1.0, QColor(10, 200, 185, 0))
            p.save()
            p.setClipPath(chevron)
            p.fillRect(QRectF(arc_left_x, top_y, tip_x - arc_left_x, bot_y - top_y), QBrush(rad))
            p.restore()

        # Inner top specular bevel + bottom recessed shadow
        p.save()
        p.setClipPath(chevron)
        p.setPen(QPen(QColor(210, 250, 250, int(40 + 45 * t)), 1.0))
        p.drawLine(QPointF(arc_left_x + 2, top_y + 1.5), QPointF(shoulder_x, top_y + 1.5))
        p.setPen(QPen(QColor(0, 0, 0, 130), 1.5))
        p.drawLine(QPointF(arc_left_x + 2, bot_y - 1.5), QPointF(shoulder_x, bot_y - 1.5))
        p.restore()

        self._draw_sheen(
            p,
            chevron,
            QRectF(arc_left_x, top_y, tip_x - arc_left_x, bot_y - top_y),
            QColor("#8CF7EE"),
        )

        rim = QLinearGradient(0.0, top_y, 0.0, bot_y)
        rim.setColorAt(0.0, QColor("#7CF8EE" if t > 0.4 else "#4DE0D6"))
        rim.setColorAt(0.45, QColor("#0AC8B9"))
        rim.setColorAt(1.0, QColor("#04728F"))
        p.setPen(QPen(QBrush(rim), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin))
        p.drawPath(chevron)

        # 4. Left circular Hextech emblem overlapping the concave arc
        aura = QRadialGradient(QPointF(24.0, h * 0.5), 25.0)
        aura.setColorAt(0.0, QColor(10, 200, 185, int(90 + 80 * t)))
        aura.setColorAt(0.65, QColor(3, 151, 171, int(35 + 45 * t)))
        aura.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(aura))
        p.drawEllipse(QPointF(24.0, h * 0.5), 25.0, 25.0)
        p.drawPixmap(2, int(round((h - 44) * 0.5)), self._emblem)

        # 5. Pure sculpted Cinzel serif for ASCII/PLAY, or crisp bold sans-serif for Chinese
        is_ascii = self.text().isascii()
        font = latin_serif_font(13, bold=True, letter_spacing=3.2) if is_ascii else display_font(12, bold=True, letter_spacing=1.8)
        p.setFont(font)
        text_rect = QRectF(arc_left_x + 12.0, top_y, shoulder_x - arc_left_x - 6.0, bot_y - top_y)
        p.setPen(QColor(1, 10, 19, 225))
        p.drawText(text_rect.translated(0.0, 1.5), Qt.AlignmentFlag.AlignCenter, self.text())
        p.setPen(QColor("#FFFFFF") if t > 0.55 else QColor("#F0E6D2"))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class HextechActionButton(_AnimatedHextechButton):
    """Single-contour chamfered LoL Hextech metallic button (cyan primary or antique-gold secondary)."""

    def __init__(
        self,
        text: str,
        variant: str = "gold",
        pointed_right: bool = False,
        height: int = 34,
        parent=None,
    ) -> None:
        super().__init__(text, parent)
        self.variant = variant
        self.pointed_right = pointed_right
        self.setFixedHeight(height)
        extra_tip = 14 if pointed_right else 0
        self.setMinimumWidth(max(84 + extra_tip, len(text) * 14 + 34 + extra_tip))

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.42)

        w, h = float(self.width()), float(self.height())
        t = self._hover_t
        sink = 1.0 if self._press_t > 0.5 else 0.0
        is_cyan = self.variant == "cyan"

        m = 1.5
        top_y = m + sink * 0.5
        bot_y = h - m - 1.5 + sink * 0.5
        left_x = m
        right_x = w - m
        mid_y = (top_y + bot_y) * 0.5

        path = QPainterPath()
        if self.pointed_right:
            tip = 11.0
            path.moveTo(left_x, top_y)
            path.lineTo(right_x - tip, top_y)
            path.lineTo(right_x, mid_y)
            path.lineTo(right_x - tip, bot_y)
            path.lineTo(left_x, bot_y)
            path.closeSubpath()
        else:
            c = 3.5
            path.moveTo(left_x + c, top_y)
            path.lineTo(right_x - c, top_y)
            path.lineTo(right_x, top_y + c)
            path.lineTo(right_x, bot_y - c)
            path.lineTo(right_x - c, bot_y)
            path.lineTo(left_x + c, bot_y)
            path.lineTo(left_x, bot_y - c)
            path.lineTo(left_x, top_y + c)
            path.closeSubpath()

        # Shape-matched drop shadow (follows chamfered contour; no outer rectangular box)
        p.save()
        p.translate(0.0, 1.5)
        p.fillPath(path, QColor(0, 0, 0, 125))
        p.restore()

        if t > 0.02:
            aura_c = QColor(10, 200, 185, int(68 * t)) if is_cyan else QColor(200, 170, 110, int(58 * t))
            p.setPen(QPen(aura_c, 3.2))
            p.drawPath(path)

        grad = QLinearGradient(0.0, top_y, 0.0, bot_y)
        if is_cyan:
            grad.setColorAt(0.0, QColor(int(24 + 14 * t), int(46 + 34 * t), int(58 + 40 * t)))
            grad.setColorAt(1.0, QColor(int(12 + 8 * t), int(26 + 22 * t), int(36 + 28 * t)))
        else:
            grad.setColorAt(0.0, QColor(int(34 + 18 * t), int(42 + 18 * t), int(50 + 12 * t)))
            grad.setColorAt(1.0, QColor(int(18 + 10 * t), int(23 + 10 * t), int(30 + 6 * t)))
        p.fillPath(path, QBrush(grad))

        p.save()
        p.setClipPath(path)
        p.setPen(QPen(QColor(255, 248, 232, int(32 + 42 * t)), 1.0))
        p.drawLine(QPointF(left_x + 2, top_y + 1.2), QPointF(right_x - 2, top_y + 1.2))
        p.setPen(QPen(QColor(0, 0, 0, 125), 1.4))
        p.drawLine(QPointF(left_x + 2, bot_y - 1.2), QPointF(right_x - 2, bot_y - 1.2))
        p.restore()

        self._draw_sheen(
            p,
            path,
            QRectF(left_x, top_y, right_x - left_x, bot_y - top_y),
            QColor("#8CF7EE") if is_cyan else QColor("#F5E2B8"),
        )

        b_grad = QLinearGradient(0.0, top_y, 0.0, bot_y)
        if is_cyan:
            b_grad.setColorAt(0.0, QColor("#74F5EA" if t > 0.4 else "#3EDAD0"))
            b_grad.setColorAt(0.5, QColor("#0AC8B9"))
            b_grad.setColorAt(1.0, QColor("#037692"))
        else:
            b_grad.setColorAt(0.0, QColor("#F5E4C3" if t > 0.4 else "#D3B67A"))
            b_grad.setColorAt(0.5, QColor("#B9924A"))
            b_grad.setColorAt(1.0, QColor("#6E5222"))
        p.setPen(QPen(QBrush(b_grad), 1.9))
        p.drawPath(path)

        font = display_font(10 if h < 30 else 11, bold=True, letter_spacing=1.0)
        p.setFont(font)
        t_rect = QRectF(left_x + 4.0, top_y, right_x - left_x - (14.0 if self.pointed_right else 8.0), bot_y - top_y)
        p.setPen(QColor(1, 10, 19, 215))
        p.drawText(t_rect.translated(0.0, 1.2), Qt.AlignmentFlag.AlignCenter, self.text())
        p.setPen(QColor("#FFFFFF") if t > 0.55 else QColor("#F0E6D2"))
        p.drawText(t_rect, Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class HextechNavButton(_AnimatedHextechButton):
    """Exact replica of CN LoL Client top nav tabs (主页 / 经典模式 / 云顶之弈):
    top double-chevron golden V-arrow on active + bottom-up mist glow + bold sans-serif #C8AA6E -> #F0E6D2.
    """

    def __init__(self, text: str, active: bool = False, parent=None) -> None:
        super().__init__(text, parent)
        self._active = active
        self.setFixedHeight(64)
        self.setMinimumWidth(104)

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.42)
        w, h = float(self.width()), float(self.height())
        t = max(self._hover_t * 0.65, 1.0 if self._active else 0.0)

        # Authentic LoL CN Client bottom-up misty illumination rectangle on active/hover
        if t > 0.02:
            mist = QLinearGradient(0.0, h, 0.0, h * 0.18)
            mist.setColorAt(0.0, QColor(200, 188, 162, int(68 * t)))
            mist.setColorAt(0.45, QColor(145, 145, 140, int(28 * t)))
            mist.setColorAt(1.0, QColor(10, 20, 32, 0))
            p.fillRect(QRectF(4.0, h * 0.16, w - 8.0, h * 0.84), QBrush(mist))

        # Authentic top golden double-chevron downward V pointer above active tab (exact screenshot detail!)
        if self._active:
            cx = w * 0.5
            # Outer bronze V
            p.setPen(QPen(QColor("#C89B3C"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin))
            p.drawPolyline(QPolygonF([QPointF(cx - 11.0, 0.0), QPointF(cx, 10.5), QPointF(cx + 11.0, 0.0)]))
            # Inner bright gold V
            p.setPen(QPen(QColor("#F0E6D2"), 1.3))
            p.drawPolyline(QPolygonF([QPointF(cx - 6.5, 0.0), QPointF(cx, 6.2), QPointF(cx + 6.5, 0.0)]))

        # Crisp bold Chinese sans-serif matching 主页 / 经典模式 / 云顶之弈
        font = display_font(13, bold=True, letter_spacing=1.2)
        p.setFont(font)
        p.setPen(QColor(1, 10, 19, 210))
        p.drawText(QRectF(0.0, 3.5, w, h - 2.0), Qt.AlignmentFlag.AlignCenter, self.text())

        # Inactive tabs are warm antique gold (#C8AA6E), active/hover brighten to cream-white (#F0E6D2)
        if self._active:
            fg = QColor("#F0E6D2")
        else:
            r = int(200 + (240 - 200) * self._hover_t)
            g = int(170 + (230 - 170) * self._hover_t)
            b = int(110 + (210 - 110) * self._hover_t)
            fg = QColor(r, g, b)
        p.setPen(fg)
        p.drawText(QRectF(0.0, 2.0, w, h - 2.0), Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()


class HextechModeTab(_AnimatedHextechButton):
    """Sub-nav diamond mode selector replicating LoL's ◇ 极地大乱斗 / ◈ 海克斯大乱斗."""

    def __init__(self, text: str, active: bool = False, parent=None) -> None:
        super().__init__(text, parent)
        self._active = active
        self.setFixedHeight(32)
        self.setMinimumWidth(len(text) * 12 + 34)

    def set_active(self, active: bool) -> None:
        self._active = active
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.42)
        w, h = float(self.width()), float(self.height())
        t = max(self._hover_t * 0.7, 1.0 if self._active else 0.0)

        cx, cy = 11.0, h * 0.5
        r_out = 5.2
        outer_d = QPolygonF([
            QPointF(cx, cy - r_out),
            QPointF(cx + r_out, cy),
            QPointF(cx, cy + r_out),
            QPointF(cx - r_out, cy),
        ])
        p.setPen(QPen(QColor("#C8AA6E" if t > 0.3 else "#785A28"), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(outer_d)

        if self._active:
            r_in = 2.4
            inner_d = QPolygonF([
                QPointF(cx, cy - r_in),
                QPointF(cx + r_in, cy),
                QPointF(cx, cy + r_in),
                QPointF(cx - r_in, cy),
            ])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#F0E6D2"))
            p.drawPolygon(inner_d)

        font = display_font(10, bold=self._active, letter_spacing=0.6)
        p.setFont(font)
        fg = QColor("#F0E6D2") if self._active else QColor(
            int(175 + 65 * self._hover_t),
            int(164 + 66 * self._hover_t),
            int(138 + 72 * self._hover_t),
        )
        p.setPen(fg)
        p.drawText(QRectF(23.0, 0.0, w - 24.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()


class HextechInteractiveCard(QFrame):
    """Zero-lag tactile Hextech card with direct-painted metallic depth shadow and 60fps hover rim glow."""

    def __init__(self, accent_color: str = "#C8AA6E", parent=None) -> None:
        super().__init__(parent)
        self._accent = QColor(accent_color)
        self._hover_t = 0.0
        self.setMouseTracking(True)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_val)

    def _on_val(self, val) -> None:
        self._hover_t = float(val)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(1.0)
        self._anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._anim.stop()
        self._anim.setStartValue(self._hover_t)
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.42)
        w, h = float(self.width()), float(self.height())
        t = self._hover_t

        # Crisp bottom/right cast depth shadow (direct paint, 0ms blur cost!)
        p.fillRect(QRectF(2.0, h - 2.0, w - 2.0, 2.0), QColor(0, 0, 0, 130))
        p.fillRect(QRectF(w - 2.0, 2.0, 2.0, h - 2.0), QColor(0, 0, 0, 95))

        card_w, card_h = w - 2.0, h - 2.0
        bg = QLinearGradient(0.0, 0.0, 0.0, card_h)
        bg.setColorAt(0.0, QColor(int(14 + 10 * t), int(28 + 16 * t), int(46 + 20 * t), 228))
        bg.setColorAt(1.0, QColor(int(7 + 6 * t), int(16 + 10 * t), int(28 + 14 * t), 234))
        p.fillRect(QRectF(0.0, 0.0, card_w, card_h), QBrush(bg))

        # Top subtle specular metallic bevel highlight
        p.setPen(QPen(QColor(240, 230, 210, int(20 + 40 * t)), 1.0))
        p.drawLine(QPointF(3.0, 1.0), QPointF(card_w - 2.0, 1.0))

        # Outer bronze-gold border brightening smoothly on hover
        border_c = QColor(
            int(120 + (200 - 120) * t),
            int(90 + (170 - 90) * t),
            int(40 + (110 - 40) * t),
            int(145 + 105 * t),
        )
        p.setPen(QPen(border_c, 1.0))
        p.drawRect(QRectF(0.5, 0.5, card_w - 1.0, card_h - 1.0))

        # Left rarity/accent metallic bar with subtle glow
        p.fillRect(QRectF(0.0, 0.0, 3.2, card_h), self._accent)
        if t > 0.05:
            glow = QLinearGradient(3.0, 0.0, 48.0, 0.0)
            glow.setColorAt(0.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), int(52 * t)))
            glow.setColorAt(1.0, QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 0))
            p.fillRect(QRectF(3.0, 1.0, 46.0, card_h - 2.0), QBrush(glow))
        p.end()


class HextechShimmerBar(QFrame):
    """Lightweight 60fps Hextech cyan-gold diagonal sweep progress bar (auto-stops when inactive/hidden)."""

    def __init__(self, height: int = 3, accent: str = "#0AC8B9", parent=None) -> None:
        super().__init__(parent)
        self._accent = QColor(accent)
        self._phase: float = -0.35
        self._active = False
        self.setFixedHeight(height)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(1150)
        self._anim.setStartValue(-0.35)
        self._anim.setEndValue(1.35)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._anim.valueChanged.connect(self._on_val)

    def _on_val(self, val) -> None:
        self._phase = float(val)
        self.update()

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        if active:
            self._anim.start()
        else:
            self._anim.stop()
            self._phase = -0.35
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802
        if self._anim.state() == QVariantAnimation.State.Running:
            self._anim.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        if self._active and self._anim.state() != QVariantAnimation.State.Running:
            self._anim.start()
        super().showEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        if not self._active:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        p.fillRect(QRectF(0.0, 0.0, w, h), QColor(10, 42, 58, 120))
        cx = w * self._phase
        band = max(48.0, w * 0.34)
        grad = QLinearGradient(cx - band, 0.0, cx + band, 0.0)
        c0 = QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 0)
        c1 = QColor(self._accent.red(), self._accent.green(), self._accent.blue(), 220)
        c2 = QColor(240, 230, 210, 235)
        grad.setColorAt(0.0, c0)
        grad.setColorAt(0.35, c1)
        grad.setColorAt(0.50, c2)
        grad.setColorAt(0.65, c1)
        grad.setColorAt(1.0, c0)
        p.fillRect(QRectF(0.0, 0.0, w, h), QBrush(grad))
        p.end()


class HextechPulseFrame(QFrame):
    """QFrame capable of firing a crisp one-shot metallic rim pulse on live state updates."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pulse_t: float = 0.0
        self._pulse_color = QColor("#0AC8B9")
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_val)

    def _on_val(self, val) -> None:
        self._pulse_t = float(val)
        self.update()

    def trigger_pulse(self, color_hex: str = "#0AC8B9", duration_ms: int = 580) -> None:
        self._pulse_color = QColor(color_hex)
        self._pulse_t = 1.0
        self._anim.stop()
        self._anim.setDuration(max(100, int(duration_ms)))
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._pulse_t <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        t = self._pulse_t
        c = self._pulse_color
        p.fillRect(QRectF(1.0, 1.0, w - 2.0, h - 2.0), QColor(c.red(), c.green(), c.blue(), int(38 * t)))
        p.setPen(QPen(QColor(c.red(), c.green(), c.blue(), int(225 * t)), 2.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(1.0, 1.0, w - 2.0, h - 2.0))
        p.end()


class HextechGoldRingIcon(QFrame):
    """Compact item icon ringed by a live gold-readiness progress arc (emerald pulse when affordable)."""

    def __init__(self, size: int = 34, parent=None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self._pixmap: QPixmap | None = None
        self.progress: float = 0.0
        self.ready: bool = False
        self._fallback: str = "?"
        self._breath_t: float = 0.0

        self._breath_anim = QVariantAnimation(self)
        self._breath_anim.setDuration(1400)
        self._breath_anim.setStartValue(0.0)
        self._breath_anim.setEndValue(1.0)
        self._breath_anim.setLoopCount(-1)
        self._breath_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._breath_anim.valueChanged.connect(self._on_breath)

    def _on_breath(self, val) -> None:
        self._breath_t = float(val)
        self.update()

    def set_state(
        self,
        pixmap: QPixmap | None,
        progress: float,
        ready: bool,
        fallback_text: str = "?",
    ) -> None:
        self._pixmap = pixmap
        self.progress = max(0.0, min(1.0, float(progress)))
        self.ready = bool(ready)
        self._fallback = (fallback_text or "?")[:1]
        if self.ready and self.isVisible():
            if self._breath_anim.state() != QVariantAnimation.State.Running:
                self._breath_anim.start()
        else:
            if self._breath_anim.state() == QVariantAnimation.State.Running:
                self._breath_anim.stop()
            self._breath_t = 0.0
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802
        if self._breath_anim.state() == QVariantAnimation.State.Running:
            self._breath_anim.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        if self.ready and self._breath_anim.state() != QVariantAnimation.State.Running:
            self._breath_anim.start()
        super().showEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.42)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        s = float(self._size)
        pad = 3.0
        inner = QRectF(pad, pad, s - pad * 2.0, s - pad * 2.0)

        clip = QPainterPath()
        clip.addEllipse(inner)
        p.save()
        p.setClipPath(clip)
        p.fillRect(inner, QColor("#091428"))
        if self._pixmap is not None and not self._pixmap.isNull():
            p.drawPixmap(inner.toRect(), self._pixmap)
        else:
            p.setPen(QColor("#C8AA6E"))
            p.setFont(display_font(10, bold=True))
            p.drawText(inner, Qt.AlignmentFlag.AlignCenter, self._fallback)
        p.restore()

        ring_rect = QRectF(2.0, 2.0, s - 4.0, s - 4.0)
        p.setPen(QPen(QColor(70, 55, 20, 185), 2.2))
        p.drawEllipse(ring_rect)

        if self.ready:
            pulse = 1.0 - abs(self._breath_t * 2.0 - 1.0)
            arc_c = QColor(
                int(9 + (110 - 9) * pulse),
                int(194 + (231 - 194) * pulse),
                int(105 + (183 - 105) * pulse),
            )
            p.setPen(QPen(arc_c, 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawEllipse(ring_rect)
        elif self.progress > 0.02:
            p.setPen(QPen(QColor("#C89B3C"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(ring_rect, 90 * 16, -int(round(self.progress * 360 * 16)))
        p.end()
