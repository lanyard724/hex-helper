"""Authentic League of Legends Client (V16.23 Hextech Magic) theme, palette, and app icon loader."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)

# Authentic LoL V16.23 Client Hextech Palette
BG_OBSIDIAN = "#010A13"
BG_NAVY = "#091428"
BG_SURFACE = "#0A1420"
BG_ELEVATED = "#111E2E"
BG_BUTTON = "#1E2328"

GOLD_CREAM = "#F0E6D2"
GOLD_PRIMARY = "#C8AA6E"
GOLD_METAL = "#C89B3C"
GOLD_BRONZE = "#785A28"
GOLD_DARK = "#463714"

CYAN_GLOW = "#0AC8B9"
CYAN_MID = "#0397AB"
CYAN_DEEP = "#005A82"

TEXT_MUTED = "#A09B8C"
TEXT_FAINT = "#5B5A56"
GREEN_ONLINE = "#09C269"

# Weathered Hextech Seal Palette (LOL V16.23 native bronze/cyan, strictly zero Web Admin Tactile Orange)
SEAL_VEIL_TOP = "rgba(4, 14, 26, 198)"
SEAL_VEIL_MID = "rgba(9, 20, 40, 212)"
SEAL_VEIL_BOT = "rgba(1, 10, 19, 226)"
SEAL_BRONZE_DEEP = "#2A2316"
SEAL_BRONZE_DARK = "#463714"
SEAL_BRONZE_MID = "#5B5A56"
SEAL_BRONZE_WORN = "#785A28"
SEAL_PATINA_MIST = "#2C3E42"
SEAL_CORE_EXTINGUISHED = "#1E282D"
SEAL_CORE_RIM = "#2E434B"
SEAL_CRACK_DARK = "#071018"
SEAL_CYAN_PEAK = "#0AC8B9"
SEAL_CYAN_BRIGHT = "#7CF8EE"
SEAL_GOLD_IGNITE = "#C8AA6E"
SEAL_GOLD_FLASH = "#F0E6D2"
SEAL_AMBER_LOW = "#FDE68A"

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_FONTS_INITIALIZED = False
_DISPLAY_FAMILY = "Noto Serif SC"


def init_hextech_fonts() -> None:
    """Register bundled Cinzel sculpted serif alongside local Noto Serif SC & Noto Sans SC."""
    global _FONTS_INITIALIZED, _DISPLAY_FAMILY
    if _FONTS_INITIALIZED:
        return
    _FONTS_INITIALIZED = True
    fonts_dir = ASSETS_DIR / "fonts"
    if fonts_dir.exists():
        for fp in sorted(fonts_dir.glob("*.ttf")) + sorted(fonts_dir.glob("*.otf")):
            fid = QFontDatabase.addApplicationFont(str(fp))
            if fid >= 0:
                fams = QFontDatabase.applicationFontFamilies(fid)
                if fams and "cinzel" in fams[0].lower():
                    _DISPLAY_FAMILY = fams[0]


def display_font(point_size: int = 12, bold: bool = True, letter_spacing: float = 0.8) -> QFont:
    """Return CN LoL Client bold geometric sans-serif font (matching 主页/经典模式/云顶之弈)."""
    init_hextech_fonts()
    f = QFont()
    f.setFamilies(["Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei UI", "Segoe UI", "Segoe UI Emoji", "Segoe UI Symbol"])
    f.setPointSize(point_size)
    f.setWeight(QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
    if letter_spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def latin_serif_font(point_size: int = 12, bold: bool = True, letter_spacing: float = 2.0) -> QFont:
    """Return sculpted Cinzel Roman serif exclusively for Latin/numeric badges (PLAY, S+, V16.23)."""
    init_hextech_fonts()
    f = QFont()
    f.setFamilies([_DISPLAY_FAMILY, "Georgia", "Cambria"])
    f.setPointSize(point_size)
    f.setWeight(QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
    if letter_spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def body_font(point_size: int = 10, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(["Noto Sans SC", "Source Han Sans SC", "Microsoft YaHei UI", "Segoe UI", "Segoe UI Emoji", "Segoe UI Symbol"])
    f.setPointSize(point_size)
    f.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Medium)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f

TIER_STYLES: dict[str, tuple[str, str, str]] = {
    # tier -> (bg, fg, border)
    "S+": ("rgba(200, 155, 60, 0.28)", "#F0E6D2", "#C89B3C"),
    "S": ("rgba(10, 200, 185, 0.22)", "#5EEAD4", "#0AC8B9"),
    "A": ("rgba(3, 151, 171, 0.22)", "#38BDF8", "#0397AB"),
    "B": ("rgba(91, 90, 86, 0.28)", "#C8C3B8", "#5B5A56"),
    "C": ("rgba(194, 108, 42, 0.24)", "#FDBA74", "#B45309"),
    "D": ("rgba(185, 45, 45, 0.26)", "#FCA5A5", "#DC2626"),
    "?": ("rgba(70, 55, 20, 0.26)", "#A09B8C", "#463714"),
}

RARITY_COLORS: dict[str, tuple[str, str]] = {
    "prismatic": ("#D946EF", "棱彩"),
    "gold": ("#C89B3C", "黄金"),
    "silver": ("#A8B9CC", "白银"),
}

BASE_OVERLAY_QSS = """
QWidget {
    font-family: "Noto Sans SC", "Microsoft YaHei UI", "Segoe UI", "Segoe UI Emoji", "Segoe UI Symbol", sans-serif;
    color: #F0E6D2;
    font-size: 12px;
}
QFrame#PanelRoot {
    background-color: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(9, 20, 40, 244),
        stop:0.55 rgba(5, 14, 26, 242),
        stop:1 rgba(1, 10, 19, 246)
    );
    border: 1px solid #785A28;
    border-top: 2px solid #C8AA6E;
    border-radius: 2px;
}
QFrame#SubCard {
    background-color: rgba(10, 22, 36, 218);
    border: 1px solid rgba(120, 90, 40, 130);
    border-radius: 2px;
}
QFrame#CoachCard {
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(4, 32, 44, 232),
        stop:1 rgba(6, 20, 34, 238)
    );
    border: 1px solid #0397AB;
    border-left: 3px solid #0AC8B9;
    border-radius: 2px;
}
QFrame#DestinyCard {
    background-color: qlineargradient(
        x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(48, 34, 12, 232),
        stop:1 rgba(20, 16, 10, 235)
    );
    border: 1px solid #C89B3C;
    border-left: 3px solid #F0E6D2;
    border-radius: 2px;
}
QPushButton {
    background-color: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #1E2832,
        stop:1 #111821
    );
    border: 1px solid #785A28;
    border-radius: 2px;
    padding: 4px 10px;
    color: #F0E6D2;
    font-weight: 600;
}
QPushButton:hover {
    background-color: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #263544,
        stop:1 #172330
    );
    border: 1px solid #C8AA6E;
    color: #FFFFFF;
}
QPushButton:pressed {
    background-color: #091428;
    border: 1px solid #463714;
    color: #C8AA6E;
}
QPushButton#CyanActionBtn {
    background-color: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #142B36,
        stop:1 #0B1B24
    );
    border: 1px solid #0AC8B9;
    color: #F0E6D2;
    padding: 5px 14px;
    font-weight: bold;
}
QPushButton#CyanActionBtn:hover {
    background-color: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 #1A4252,
        stop:1 #0E2B38
    );
    border: 1px solid #5EEAD4;
    color: #FFFFFF;
}
QPushButton#NavTabBtn {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 16px;
    color: #A09B8C;
    font-size: 14px;
    font-weight: bold;
}
QPushButton#NavTabBtn:hover {
    color: #F0E6D2;
}
QPushButton#NavTabBtn[active="true"] {
    color: #F0E6D2;
    border-bottom: 2px solid #C8AA6E;
    background-color: qlineargradient(
        x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(200, 170, 110, 42),
        stop:1 rgba(200, 170, 110, 5)
    );
}
QPushButton#WinCtrlBtn {
    background: transparent;
    border: none;
    color: #A09B8C;
    font-size: 14px;
    font-weight: bold;
    padding: 2px 8px;
}
QPushButton#WinCtrlBtn:hover {
    color: #F0E6D2;
    background-color: rgba(200, 170, 110, 35);
}
QComboBox, QLineEdit {
    background-color: #061222;
    border: 1px solid #785A28;
    border-radius: 2px;
    padding: 3px 8px;
    color: #F0E6D2;
    selection-background-color: #005A82;
}
QComboBox:hover, QLineEdit:focus {
    border: 1px solid #C8AA6E;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    border-left: 1px solid #785A28;
    background-color: #0E1F34;
    width: 20px;
}
QComboBox::down-arrow {
    width: 6px;
    height: 6px;
    background-color: #C8AA6E;
    border: 1px solid #091428;
    border-radius: 1px;
}
QComboBox QAbstractItemView {
    background-color: #010A13;
    border: 1px solid #785A28;
    color: #F0E6D2;
    selection-background-color: #1E2832;
    selection-color: #C8AA6E;
}
QCheckBox {
    color: #F0E6D2;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid #785A28;
    background-color: #010A13;
    border-radius: 1px;
}
QCheckBox::indicator:checked {
    border: 1px solid #0AC8B9;
    background-color: #0397AB;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    width: 6px;
    background: #010A13;
    border: none;
}
QScrollBar::handle:vertical {
    background: #785A28;
    border-radius: 2px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #C8AA6E;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: #010A13;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar:horizontal {
    height: 0px;
    background: transparent;
}
"""


def tier_badge_qss(tier: str) -> str:
    bg, fg, border = TIER_STYLES.get(tier, TIER_STYLES["?"])
    return (
        f"background-color: {bg}; color: {fg}; border: 1px solid {border}; "
        "border-radius: 2px; padding: 1px 5px; font-weight: bold;"
    )


def render_fallback_emblem_pixmap(size: int = 64) -> QPixmap:
    """Vector-painted LoL Hextech gold medallion + cyan crystal H fallback emblem."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    cx, cy = size * 0.5, size * 0.5
    r_outer = size * 0.46
    r_inner = size * 0.37

    # Deep navy circular medallion core with cyan radial glow
    rad = QRadialGradient(cx, cy, r_outer)
    rad.setColorAt(0.0, QColor("#0B3A4E"))
    rad.setColorAt(0.55, QColor("#091428"))
    rad.setColorAt(1.0, QColor("#010A13"))
    p.setBrush(QBrush(rad))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(cx, cy), r_outer, r_outer)

    # Metallic antique-gold outer bevel ring
    gold_grad = QLinearGradient(0, 0, size, size)
    gold_grad.setColorAt(0.0, QColor("#F0E6D2"))
    gold_grad.setColorAt(0.35, QColor("#C89B3C"))
    gold_grad.setColorAt(0.75, QColor("#785A28"))
    gold_grad.setColorAt(1.0, QColor("#C8AA6E"))
    p.setPen(QPen(QBrush(gold_grad), max(2.0, size * 0.085)))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(cx, cy), r_outer - size * 0.04, r_outer - size * 0.04)

    # Inner cyan hairline ring
    p.setPen(QPen(QColor("#0AC8B9"), max(1.0, size * 0.025)))
    p.drawEllipse(QPointF(cx, cy), r_inner, r_inner)

    # Central glowing Hextech diamond crystal + sculpted gold "H"
    diamond = QPainterPath()
    d = size * 0.22
    diamond.moveTo(cx, cy - d)
    diamond.lineTo(cx + d * 0.65, cy)
    diamond.lineTo(cx, cy + d)
    diamond.lineTo(cx - d * 0.65, cy)
    diamond.closeSubpath()
    c_grad = QLinearGradient(cx, cy - d, cx, cy + d)
    c_grad.setColorAt(0.0, QColor("#5EEAD4"))
    c_grad.setColorAt(0.5, QColor("#0AC8B9"))
    c_grad.setColorAt(1.0, QColor("#005A82"))
    p.fillPath(diamond, QBrush(c_grad))

    font = QFont("Georgia", max(10, int(size * 0.40)), QFont.Weight.Bold)
    p.setFont(font)
    p.setPen(QColor("#F0E6D2"))
    p.drawText(QRectF(0, -size * 0.02, size, size), Qt.AlignmentFlag.AlignCenter, "H")
    p.end()
    return pm


def load_app_pixmap(size: int = 40) -> QPixmap:
    """Load generated LoL-style transparent Hextech emblem PNG/ICO preserving outer diamond points."""
    for name in ("hex_helper.png", "hex_helper.ico"):
        path = ASSETS_DIR / name
        if path.exists():
            src = QPixmap(str(path))
            if not src.isNull():
                return src.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
    return render_fallback_emblem_pixmap(size)


def load_app_icon() -> QIcon:
    """Return multi-resolution QIcon for window, taskbar, and system tray."""
    ico_path = ASSETS_DIR / "hex_helper.ico"
    if ico_path.exists():
        icon = QIcon(str(ico_path))
        if not icon.isNull():
            return icon
    png_path = ASSETS_DIR / "hex_helper.png"
    if png_path.exists():
        icon = QIcon(str(png_path))
        if not icon.isNull():
            return icon
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128):
        icon.addPixmap(render_fallback_emblem_pixmap(sz))
    return icon
