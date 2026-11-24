"""Offline LOL V16.23 Hextech match recap card renderer & 1-click clipboard share dialog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from hex_helper.ui.hextech_widgets import HextechActionButton
from hex_helper.ui.theme import (
    ASSETS_DIR,
    BASE_OVERLAY_QSS,
    init_hextech_fonts,
    load_app_icon,
)

# Canonical logical design dimensions (16:9-ish shareable trophy card)
CARD_DEFAULT_WIDTH = 760
CARD_DEFAULT_HEIGHT = 430

# Authentic LOL V16.23 Hextech Palette (strictly desktop Hextech; zero Web orange)
_BG_DARK_START = "#040E1A"
_BG_DARK_MID = "#091428"
_BG_DARK_END = "#030B16"
_GOLD_CREAM = "#F0E6D2"
_GOLD_PRIMARY = "#C8AA6E"
_GOLD_METAL = "#C89B3C"
_GOLD_BRONZE = "#785A28"
_GOLD_HAIRLINE = "#463714"
_CYAN_BRIGHT = "#5EEAD4"
_CYAN_PRIMARY = "#0AC8B9"
_CYAN_MID = "#0397AB"
_COACH_PANEL_BG = "#061A2A"
_TEXT_MUTED = "#A09B8C"
_TEXT_FAINT = "#6B6B68"

_RARITY_PALETTE: dict[str, dict[str, str]] = {
    "prismatic": {
        "label": "棱彩",
        "accent": "#D946EF",
        "bright": "#F5D0FE",
        "bg_top": "#251138",
        "bg_bot": "#100B22",
    },
    "gold": {
        "label": "黄金",
        "accent": "#C8AA6E",
        "bright": "#F0E6D2",
        "bg_top": "#241C10",
        "bg_bot": "#101520",
    },
    "silver": {
        "label": "白银",
        "accent": "#94A3B8",
        "bright": "#E2E8F0",
        "bg_top": "#172230",
        "bg_bot": "#0C1420",
    },
}

_CARD_FONTS_READY = False
_CINZEL_FAMILY = "Cinzel"
_COPY_BTN_DEFAULT_TEXT = "📋 一键复制战报图"
_COPY_BTN_COPIED_TEXT = "✓ 已复制，去群里 Ctrl+V 粘贴！"
_SAVE_BTN_DEFAULT_TEXT = "💾 保存图片"
_SAVE_BTN_SAVED_TEXT = "✓ 已保存图片"

_KDA_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*$")
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass(frozen=True)
class MatchRecapData:
    """Immutable snapshot of a completed ARAM Mayhem match for card rendering."""

    hero_id: str
    hero_name: str
    hero_icon_path: str = ""
    won: int = -1  # 1 = 胜利, 0 = 败局/激战, -1 = 对局结束
    kda: str = ""
    duration_s: int = 0
    team_kills: str = ""
    is_first_prismatic: bool = False
    augments: tuple[tuple[str, str], ...] = ()  # (name, rarity) up to 4
    combo_title: str = ""
    combo_completed: bool = False
    item_entries: tuple[tuple[str, str], ...] = ()  # (item_name, icon_path) up to 6
    ai_highlight: str = ""
    dataset_patch: str = "26.23"


def _ensure_card_fonts() -> None:
    """Register bundled Cinzel + Windows CJK/symbol fonts when running under offscreen Qt."""
    global _CARD_FONTS_READY, _CINZEL_FAMILY
    if _CARD_FONTS_READY:
        return
    _CARD_FONTS_READY = True

    try:
        init_hextech_fonts()
    except Exception:
        pass

    families = set(QFontDatabase.families())
    for fam in families:
        if "cinzel" in fam.lower():
            _CINZEL_FAMILY = fam
            break

    # Under QT_QPA_PLATFORM=offscreen on Windows, system fonts are not auto-populated.
    need_cjk = not any(
        f in families for f in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC", "Source Han Sans SC")
    )
    need_sym = "Segoe UI Symbol" not in families
    if need_cjk or need_sym:
        win_fonts = Path(r"C:\Windows\Fonts")
        if win_fonts.is_dir():
            candidates = (
                "msyh.ttc",
                "msyhbd.ttc",
                "seguisym.ttf",
                "segoeui.ttf",
                "segoeuib.ttf",
                "simhei.ttf",
            )
            for fname in candidates:
                fp = win_fonts / fname
                if fp.is_file():
                    try:
                        QFontDatabase.addApplicationFont(str(fp))
                    except Exception:
                        pass
            QFont.insertSubstitutions("Noto Sans SC", ["Microsoft YaHei UI", "Segoe UI Symbol", "Segoe UI"])
            QFont.insertSubstitutions("Microsoft YaHei UI", ["Segoe UI Symbol", "Segoe UI"])


def _sans_font(pixel_size: int, bold: bool = False, letter_spacing: float = 0.0) -> QFont:
    _ensure_card_fonts()
    f = QFont()
    f.setFamilies(
        [
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "Noto Sans SC",
            "Source Han Sans SC",
            "Segoe UI Symbol",
            "Segoe UI",
        ]
    )
    f.setPixelSize(max(8, int(round(pixel_size))))
    f.setWeight(QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
    if letter_spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def _latin_font(pixel_size: int, bold: bool = True, letter_spacing: float = 1.4) -> QFont:
    _ensure_card_fonts()
    f = QFont()
    f.setFamilies([_CINZEL_FAMILY, "Georgia", "Cambria", "Microsoft YaHei UI"])
    f.setPixelSize(max(8, int(round(pixel_size))))
    f.setWeight(QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
    if letter_spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def _normalize_rarity(rarity: str) -> str:
    raw = (rarity or "").strip().lower()
    if raw in ("prismatic", "prism", "棱彩", "彩色", "彩"):
        return "prismatic"
    if raw in ("gold", "golden", "黄金", "金"):
        return "gold"
    if raw in ("silver", "白银", "银"):
        return "silver"
    return "silver"


def _format_duration(duration_s: int) -> str:
    sec = max(0, int(duration_s or 0))
    if sec <= 0:
        return ""
    mins, rem = divmod(sec, 60)
    return f"{mins:02d}:{rem:02d}"


def _parse_kda(kda: str) -> tuple[str, str, str, str] | None:
    m = _KDA_RE.match(kda or "")
    if not m:
        return None
    k_str, d_str, a_str = m.group(1), m.group(2), m.group(3)
    k, d, a = int(k_str), int(d_str), int(a_str)
    if d == 0:
        ratio = "PERFECT" if (k + a) > 0 else "0.0 KDA"
    else:
        ratio = f"{(k + a) / d:.1f} KDA"
    return k_str, d_str, a_str, ratio


def _safe_filename_part(name: str) -> str:
    cleaned = _INVALID_FILENAME_RE.sub("_", (name or "").strip()).strip(" ._")
    return cleaned[:40] or "match"


def _load_local_image(path_str: str) -> QImage | None:
    raw = (path_str or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_file():
        return None
    img = QImage(str(p))
    if img.isNull() or img.width() <= 0 or img.height() <= 0:
        return None
    return img


def _load_emblem_image() -> QImage | None:
    for fname in ("hex_helper.png", "hex_helper.ico"):
        img = _load_local_image(str(ASSETS_DIR / fname))
        if img is not None:
            return img
    return None


def _chamfer_path(rect: QRectF, chamfer: float) -> QPainterPath:
    c = max(0.0, min(chamfer, rect.width() * 0.45, rect.height() * 0.45))
    x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
    path = QPainterPath()
    path.moveTo(x0 + c, y0)
    path.lineTo(x1 - c, y0)
    path.lineTo(x1, y0 + c)
    path.lineTo(x1, y1 - c)
    path.lineTo(x1 - c, y1)
    path.lineTo(x0 + c, y1)
    path.lineTo(x0, y1 - c)
    path.lineTo(x0, y0 + c)
    path.closeSubpath()
    return path


def _draw_diamond(p: QPainter, cx: float, cy: float, radius: float, color: QColor) -> None:
    poly = QPolygonF(
        [
            QPointF(cx, cy - radius),
            QPointF(cx + radius, cy),
            QPointF(cx, cy + radius),
            QPointF(cx - radius, cy),
        ]
    )
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawPolygon(poly)
    p.restore()


def _wrap_text_max_lines(
    text: str,
    fm: QFontMetricsF,
    max_width: float,
    max_lines: int = 2,
) -> list[str]:
    """Wrap mixed CJK/Latin text cleanly into at most `max_lines`, eliding overflow on the last line."""
    clean = " ".join((text or "").split())
    if not clean or max_lines <= 0 or max_width <= 8.0:
        return []

    lines: list[str] = []
    idx = 0
    n = len(clean)

    while idx < n and len(lines) < max_lines:
        is_last = len(lines) == max_lines - 1
        rem = clean[idx:].lstrip()
        if not rem:
            break
        if fm.horizontalAdvance(rem) <= max_width:
            lines.append(rem)
            break
        if is_last:
            lines.append(fm.elidedText(rem, Qt.TextElideMode.ElideRight, max_width))
            break

        # Binary search largest fitting prefix
        lo, hi = 1, len(rem)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if fm.horizontalAdvance(rem[:mid]) <= max_width:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        # Prefer breaking at an ASCII space if near the end of an ASCII word
        split_at = best
        if best < len(rem) and rem[best - 1].isascii() and rem[best - 1].isalnum() and rem[best].isascii() and rem[best].isalnum():
            last_sp = rem.rfind(" ", 0, best)
            if last_sp >= max(1, int(best * 0.55)):
                split_at = last_sp

        chunk = rem[:split_at].rstrip()
        if not chunk:
            chunk = rem[:best]
            split_at = best
        lines.append(chunk)
        # Advance idx past consumed prefix inside original `clean`
        leading_trim = len(clean[idx:]) - len(rem)
        idx += leading_trim + split_at

    return lines


def _draw_background_and_frame(p: QPainter, lw: float, lh: float, data: MatchRecapData) -> None:
    """Layer 1 & 2: Deep navy-black Hextech gradient, glows, diagonal sheen, bevels, corner brackets."""
    full_rect = QRectF(0.0, 0.0, lw, lh)

    # Base deep obsidian-navy gradient (#040E1A -> #091428 -> #030B16)
    bg = QLinearGradient(0.0, 0.0, lw, lh)
    bg.setColorAt(0.0, QColor(_BG_DARK_START))
    bg.setColorAt(0.52, QColor(_BG_DARK_MID))
    bg.setColorAt(1.0, QColor(_BG_DARK_END))
    p.fillRect(full_rect, QBrush(bg))

    # Status-reactive radial atmosphere glows
    if data.won == 1:
        top_glow = QRadialGradient(QPointF(140.0, 74.0), 290.0)
        top_glow.setColorAt(0.0, QColor(200, 170, 110, 46))
        top_glow.setColorAt(0.55, QColor(200, 155, 60, 16))
        top_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    elif data.won == 0:
        top_glow = QRadialGradient(QPointF(140.0, 74.0), 290.0)
        top_glow.setColorAt(0.0, QColor(10, 200, 185, 44))
        top_glow.setColorAt(0.55, QColor(3, 151, 171, 16))
        top_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    else:
        top_glow = QRadialGradient(QPointF(140.0, 74.0), 270.0)
        top_glow.setColorAt(0.0, QColor(110, 180, 175, 34))
        top_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(full_rect, QBrush(top_glow))

    # Lower-right subtle Hextech cyan bloom
    br_glow = QRadialGradient(QPointF(lw - 105.0, lh - 68.0), 260.0)
    br_glow.setColorAt(0.0, QColor(10, 200, 185, 30))
    br_glow.setColorAt(0.6, QColor(3, 151, 171, 12))
    br_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(full_rect, QBrush(br_glow))

    # Subtle prismatic shimmer behind center band when high-roll / first prismatic
    has_prism = data.is_first_prismatic or data.combo_completed or any(
        _normalize_rarity(r) == "prismatic" for _, r in data.augments
    )
    if has_prism:
        prism_glow = QRadialGradient(QPointF(lw * 0.5, 184.0), 280.0)
        prism_glow.setColorAt(0.0, QColor(217, 70, 239, 26))
        prism_glow.setColorAt(0.6, QColor(147, 51, 234, 9))
        prism_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(full_rect, QBrush(prism_glow))

    # Clipped interior diagonal sheen lines + faint geometric Hextech rune arc
    outer_rect = QRectF(5.0, 5.0, lw - 10.0, lh - 10.0)
    outer_path = _chamfer_path(outer_rect, 8.0)
    p.save()
    p.setClipPath(outer_path)

    # Diagonal sheen ribbons (45-degree specular architectural streaks)
    sheen_specs = (
        (lw * 0.14, 54.0, QColor(200, 170, 110, 13)),
        (lw * 0.44, 34.0, QColor(10, 200, 185, 11)),
        (lw * 0.72, 68.0, QColor(200, 170, 110, 10)),
        (lw * 0.90, 26.0, QColor(240, 230, 210, 8)),
    )
    for x_start, band_w, col in sheen_specs:
        poly = QPolygonF(
            [
                QPointF(x_start, 0.0),
                QPointF(x_start + band_w, 0.0),
                QPointF(x_start + band_w - lh * 0.52, lh),
                QPointF(x_start - lh * 0.52, lh),
            ]
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPolygon(poly)

    # Fine diagonal hairline strokes
    p.setPen(QPen(QColor(200, 170, 110, 22), 1.0))
    p.drawLine(QPointF(lw * 0.62, 0.0), QPointF(lw * 0.62 - lh * 0.52, lh))
    p.setPen(QPen(QColor(10, 200, 185, 20), 1.0))
    p.drawLine(QPointF(lw * 0.88, 0.0), QPointF(lw * 0.88 - lh * 0.52, lh))

    # Faint Hextech geometric compass rings on upper-right background
    rcx, rcy = lw - 76.0, 84.0
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(200, 170, 110, 20), 1.0))
    p.drawEllipse(QPointF(rcx, rcy), 84.0, 84.0)
    p.setPen(QPen(QColor(10, 200, 185, 18), 1.0, Qt.PenStyle.DashLine))
    p.drawEllipse(QPointF(rcx, rcy), 112.0, 112.0)
    p.restore()

    # Outer soft ambient rim glow
    rim_aura = QColor(200, 170, 110, 42) if data.won != 0 else QColor(10, 200, 185, 40)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(rim_aura, 3.2))
    p.drawPath(outer_path)

    # Outer metallic gold bevel (#F0E6D2 -> #C8AA6E -> #785A28 -> #C8AA6E)
    gold_bevel = QLinearGradient(0.0, 0.0, lw, lh)
    gold_bevel.setColorAt(0.0, QColor(_GOLD_CREAM))
    gold_bevel.setColorAt(0.28, QColor(_GOLD_PRIMARY))
    gold_bevel.setColorAt(0.65, QColor(_GOLD_BRONZE))
    gold_bevel.setColorAt(1.0, QColor(_GOLD_PRIMARY))
    p.setPen(QPen(QBrush(gold_bevel), 1.6))
    p.drawPath(outer_path)

    # Inner dark antique-gold hairline (#463714)
    inner_rect = QRectF(10.0, 10.0, lw - 20.0, lh - 20.0)
    inner_path = _chamfer_path(inner_rect, 5.0)
    p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0))
    p.drawPath(inner_path)

    # Top center gold keystone ornament
    top_mid = lw * 0.5
    p.setPen(QPen(QColor(_GOLD_CREAM), 1.8))
    p.drawLine(QPointF(top_mid - 58.0, 5.0), QPointF(top_mid + 58.0, 5.0))
    _draw_diamond(p, top_mid, 5.0, 3.8, QColor(_GOLD_CREAM))
    _draw_diamond(p, top_mid, 5.0, 1.8, QColor(_CYAN_PRIMARY if data.won == 0 else _GOLD_METAL))

    # 4 Corner Hextech Brackets + tiny rivets
    arm = 18.0
    m = 3.5
    rivet_col = QColor(_GOLD_PRIMARY if data.won != 0 else _CYAN_PRIMARY)
    bracket_pen = QPen(QColor(_GOLD_CREAM), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin)
    p.setPen(bracket_pen)
    corners = (
        (m, m, 1.0, 1.0),
        (lw - m, m, -1.0, 1.0),
        (m, lh - m, 1.0, -1.0),
        (lw - m, lh - m, -1.0, -1.0),
    )
    for cx, cy, dx, dy in corners:
        p.drawPolyline(
            QPolygonF(
                [
                    QPointF(cx, cy + dy * arm),
                    QPointF(cx, cy + dy * 4.0),
                    QPointF(cx + dx * 4.0, cy),
                    QPointF(cx + dx * arm, cy),
                ]
            )
        )
        _draw_diamond(p, cx + dx * 8.5, cy + dy * 8.5, 2.2, rivet_col)


def _draw_top_ribbon(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 3: Top-left Victory/Battle badge + optional 首棱彩局 tag + top-right Patch badge."""
    y = 18.0
    h = 26.0
    x = 24.0

    # 1. Victory / Battle / Recap status badge
    if data.won == 1:
        badge_text = "✦ 胜利 · VICTORY"
        bg_top, bg_bot = QColor("#36260E"), QColor("#181108")
        border_c1, border_c2 = QColor(_GOLD_CREAM), QColor(_GOLD_METAL)
        glow_col = QColor(200, 170, 110, 72)
        text_col = QColor(_GOLD_CREAM)
    elif data.won == 0:
        badge_text = "◈ 激战局 · ARAM"
        bg_top, bg_bot = QColor("#0A3240"), QColor("#051721")
        border_c1, border_c2 = QColor(_CYAN_BRIGHT), QColor(_CYAN_PRIMARY)
        glow_col = QColor(10, 200, 185, 70)
        text_col = QColor(_CYAN_BRIGHT)
    else:
        badge_text = "◆ 对局结束 · RECAP"
        bg_top, bg_bot = QColor("#122338"), QColor("#081322")
        border_c1, border_c2 = QColor(_GOLD_PRIMARY), QColor(_CYAN_MID)
        glow_col = QColor(200, 170, 110, 44)
        text_col = QColor(_GOLD_CREAM)

    badge_font = _sans_font(12, bold=True, letter_spacing=0.9)
    fm = QFontMetricsF(badge_font)
    badge_w = fm.horizontalAdvance(badge_text) + 24.0
    badge_rect = QRectF(x, y, badge_w, h)
    badge_path = _chamfer_path(badge_rect, 5.0)

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(glow_col, 3.6))
    p.drawPath(badge_path)

    b_fill = QLinearGradient(badge_rect.left(), badge_rect.top(), badge_rect.left(), badge_rect.bottom())
    b_fill.setColorAt(0.0, bg_top)
    b_fill.setColorAt(1.0, bg_bot)
    p.fillPath(badge_path, QBrush(b_fill))

    b_rim = QLinearGradient(badge_rect.left(), badge_rect.top(), badge_rect.right(), badge_rect.bottom())
    b_rim.setColorAt(0.0, border_c1)
    b_rim.setColorAt(1.0, border_c2)
    p.setPen(QPen(QBrush(b_rim), 1.4))
    p.drawPath(badge_path)

    p.setFont(badge_font)
    p.setPen(QColor(0, 0, 0, 190))
    p.drawText(badge_rect.translated(0.0, 1.0), Qt.AlignmentFlag.AlignCenter, badge_text)
    p.setPen(text_col)
    p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge_text)

    x += badge_w + 9.0

    # 2. Optional 首棱彩局 prismatic tag
    if data.is_first_prismatic:
        prism_text = "✦ 首棱彩局"
        p_font = _sans_font(12, bold=True, letter_spacing=0.6)
        pfm = QFontMetricsF(p_font)
        pw = pfm.horizontalAdvance(prism_text) + 20.0
        p_rect = QRectF(x, y, pw, h)
        p_path = _chamfer_path(p_rect, 5.0)

        p.setPen(QPen(QColor(217, 70, 239, 68), 3.2))
        p.drawPath(p_path)

        p_fill = QLinearGradient(p_rect.left(), p_rect.top(), p_rect.right(), p_rect.bottom())
        p_fill.setColorAt(0.0, QColor("#36154D"))
        p_fill.setColorAt(1.0, QColor("#180A29"))
        p.fillPath(p_path, QBrush(p_fill))

        p_rim = QLinearGradient(p_rect.left(), p_rect.top(), p_rect.right(), p_rect.bottom())
        p_rim.setColorAt(0.0, QColor("#F5D0FE"))
        p_rim.setColorAt(0.5, QColor("#D946EF"))
        p_rim.setColorAt(1.0, QColor("#9333EA"))
        p.setPen(QPen(QBrush(p_rim), 1.4))
        p.drawPath(p_path)

        p.setFont(p_font)
        p.setPen(QColor("#FAE8FF"))
        p.drawText(p_rect, Qt.AlignmentFlag.AlignCenter, prism_text)

    # 3. Top-right Patch badge
    patch_str = f"PATCH {(data.dataset_patch or '26.23').strip()}"
    patch_font = _latin_font(11, bold=True, letter_spacing=1.2)
    pt_fm = QFontMetricsF(patch_font)
    pt_w = pt_fm.horizontalAdvance(patch_str) + 22.0
    pt_rect = QRectF(lw - 24.0 - pt_w, y + 1.0, pt_w, h - 2.0)
    pt_path = _chamfer_path(pt_rect, 4.0)
    p.fillPath(pt_path, QColor(9, 22, 38, 220))
    p.setPen(QPen(QColor(_GOLD_BRONZE), 1.1))
    p.drawPath(pt_path)
    p.setFont(patch_font)
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(pt_rect, Qt.AlignmentFlag.AlignCenter, patch_str)


def _draw_hero_portrait(p: QPainter, x: float, y: float, size: float, data: MatchRecapData) -> None:
    """64x64 circular/beveled hero portrait with metallic gold frame and monogram fallback."""
    cx = x + size * 0.5
    cy = y + size * 0.5
    r = size * 0.5

    # Soft outer halo
    halo_col = QColor(200, 170, 110, 62) if data.won != 0 else QColor(10, 200, 185, 62)
    halo = QRadialGradient(QPointF(cx, cy), r + 7.0)
    halo.setColorAt(0.7, halo_col)
    halo.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(halo))
    p.drawEllipse(QPointF(cx, cy), r + 7.0, r + 7.0)

    inner_rect = QRectF(x + 2.0, y + 2.0, size - 4.0, size - 4.0)
    clip = QPainterPath()
    clip.addEllipse(inner_rect)

    img = _load_local_image(data.hero_icon_path)
    p.save()
    p.setClipPath(clip)
    if img is not None:
        # Center-crop square and smooth-scale
        side = min(img.width(), img.height())
        sx = (img.width() - side) // 2
        sy = (img.height() - side) // 2
        cropped = img.copy(sx, sy, side, side)
        p.drawImage(inner_rect, cropped)
    else:
        # Graceful Hextech gold monogram fallback
        med = QRadialGradient(QPointF(cx, cy), r)
        med.setColorAt(0.0, QColor("#103A4F"))
        med.setColorAt(0.6, QColor("#091428"))
        med.setColorAt(1.0, QColor("#030A14"))
        p.fillRect(inner_rect, QBrush(med))

        # Inner subtle cyan rune circle
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(10, 200, 185, 110), 1.0))
        p.drawEllipse(inner_rect.adjusted(4.0, 4.0, -4.0, -4.0))

        hero_txt = (data.hero_name or "").strip()
        monogram = hero_txt[0] if hero_txt else ((data.hero_id or "H").strip()[:2].upper() or "H")
        m_font = _sans_font(26 if len(monogram) == 1 else 18, bold=True)
        p.setFont(m_font)
        p.setPen(QColor(0, 0, 0, 190))
        p.drawText(inner_rect.translated(0.0, 1.5), Qt.AlignmentFlag.AlignCenter, monogram)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(inner_rect, Qt.AlignmentFlag.AlignCenter, monogram)
    p.restore()

    # Metallic antique-gold circular ring
    ring_grad = QLinearGradient(x, y, x + size, y + size)
    ring_grad.setColorAt(0.0, QColor(_GOLD_CREAM))
    ring_grad.setColorAt(0.35, QColor(_GOLD_METAL))
    ring_grad.setColorAt(0.75, QColor(_GOLD_BRONZE))
    ring_grad.setColorAt(1.0, QColor(_GOLD_PRIMARY))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QBrush(ring_grad), 2.6))
    p.drawEllipse(QRectF(x + 1.3, y + 1.3, size - 2.6, size - 2.6))

    # Subtle 4 cardinal gold micro-diamonds on portrait bezel
    for dx, dy in ((0.0, -r), (r, 0.0), (0.0, r), (-r, 0.0)):
        _draw_diamond(p, cx + dx, cy + dy, 2.3, QColor(_GOLD_CREAM))


def _draw_header(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 4: Hero portrait, hero name, KDA block, duration (MM:SS), and team kills."""
    portrait_x = 26.0
    portrait_y = 56.0
    portrait_sz = 64.0
    _draw_hero_portrait(p, portrait_x, portrait_y, portrait_sz, data)

    # Right-side KDA metric panel (measure first so hero name never collides)
    kda_raw = (data.kda or "").strip()
    kda_panel_w = 0.0
    if kda_raw:
        parsed = _parse_kda(kda_raw)
        num_font = _sans_font(24, bold=True, letter_spacing=0.6)
        nfm = QFontMetricsF(num_font)
        if parsed is not None:
            k_s, d_s, a_s, ratio_s = parsed
            text_w = (
                nfm.horizontalAdvance(k_s)
                + nfm.horizontalAdvance(" / ")
                + nfm.horizontalAdvance(d_s)
                + nfm.horizontalAdvance(" / ")
                + nfm.horizontalAdvance(a_s)
            )
        else:
            text_w = nfm.horizontalAdvance(kda_raw)
            ratio_s = "KDA"

        kda_panel_w = max(168.0, min(260.0, text_w + 36.0))
        kda_rect = QRectF(lw - 24.0 - kda_panel_w, 57.0, kda_panel_w, 62.0)
        kda_path = _chamfer_path(kda_rect, 6.0)

        k_bg = QLinearGradient(kda_rect.left(), kda_rect.top(), kda_rect.right(), kda_rect.bottom())
        k_bg.setColorAt(0.0, QColor(11, 26, 46, 220))
        k_bg.setColorAt(1.0, QColor(5, 14, 26, 232))
        p.fillPath(kda_path, QBrush(k_bg))
        p.setPen(QPen(QColor(_GOLD_BRONZE), 1.1))
        p.drawPath(kda_path)

        # Top micro gold bar on KDA panel
        p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.8))
        p.drawLine(QPointF(kda_rect.left() + 12.0, kda_rect.top() + 0.5), QPointF(kda_rect.right() - 12.0, kda_rect.top() + 0.5))

        # Draw K / D / A centered in upper half
        num_y = kda_rect.top() + 6.0
        num_h = 31.0
        p.setFont(num_font)
        if parsed is not None:
            k_s, d_s, a_s, ratio_s = parsed
            parts = (
                (k_s, QColor(_GOLD_CREAM)),
                (" / ", QColor(_GOLD_BRONZE)),
                (d_s, QColor("#E28B7E")),
                (" / ", QColor(_GOLD_BRONZE)),
                (a_s, QColor(_CYAN_BRIGHT)),
            )
            cur_x = kda_rect.center().x() - text_w * 0.5
            for seg, col in parts:
                seg_w = nfm.horizontalAdvance(seg)
                seg_r = QRectF(cur_x, num_y, seg_w + 1.0, num_h)
                p.setPen(QColor(0, 0, 0, 185))
                p.drawText(seg_r.translated(0.0, 1.2), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, seg)
                p.setPen(col)
                p.drawText(seg_r, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, seg)
                cur_x += seg_w
        else:
            elided_kda = nfm.elidedText(kda_raw, Qt.TextElideMode.ElideRight, kda_panel_w - 20.0)
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(kda_rect.left() + 8.0, num_y, kda_panel_w - 16.0, num_h), Qt.AlignmentFlag.AlignCenter, elided_kda)

        # Lower ratio sub-label
        ratio_font = _latin_font(10, bold=True, letter_spacing=1.4)
        p.setFont(ratio_font)
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(QRectF(kda_rect.left() + 6.0, kda_rect.top() + 39.0, kda_panel_w - 12.0, 18.0), Qt.AlignmentFlag.AlignCenter, ratio_s)

    # Left hero identity + metadata column
    info_x = portrait_x + portrait_sz + 16.0
    info_max_w = (lw - 24.0 - kda_panel_w - (14.0 if kda_panel_w > 0 else 0.0)) - info_x

    hero_display = (data.hero_name or data.hero_id or "未知英雄").strip()
    name_font = _sans_font(24, bold=True, letter_spacing=0.6)
    name_fm = QFontMetricsF(name_font)
    elided_hero = name_fm.elidedText(hero_display, Qt.TextElideMode.ElideRight, max(80.0, info_max_w))
    name_rect = QRectF(info_x, 56.0, info_max_w, 33.0)

    p.setFont(name_font)
    p.setPen(QColor(0, 0, 0, 210))
    p.drawText(name_rect.translated(0.0, 1.5), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_hero)
    p.setPen(QColor(_GOLD_CREAM))
    p.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_hero)

    # Metadata row: Duration + Team Kills + Mode tag
    meta_y = 94.0
    meta_h = 24.0
    meta_x = info_x
    meta_chips: list[tuple[str, str, QColor]] = []

    dur_str = _format_duration(data.duration_s)
    if dur_str:
        meta_chips.append(("时长", dur_str, QColor(_GOLD_CREAM)))
    if (data.team_kills or "").strip():
        meta_chips.append(("人头比", data.team_kills.strip(), QColor(_CYAN_BRIGHT)))
    if not meta_chips:
        meta_chips.append(("模式", "海克斯大乱斗", QColor(_GOLD_PRIMARY)))

    lbl_font = _sans_font(11, bold=False)
    val_font = _sans_font(12, bold=True, letter_spacing=0.4)
    lfm = QFontMetricsF(lbl_font)
    vfm = QFontMetricsF(val_font)

    for idx, (lbl, val, val_col) in enumerate(meta_chips):
        chip_w = lfm.horizontalAdvance(lbl) + vfm.horizontalAdvance(val) + 24.0
        if meta_x + chip_w > info_x + info_max_w and idx > 0:
            break
        c_rect = QRectF(meta_x, meta_y, min(chip_w, info_x + info_max_w - meta_x), meta_h)
        c_path = _chamfer_path(c_rect, 4.0)
        p.fillPath(c_path, QColor(9, 22, 38, 210))
        p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0))
        p.drawPath(c_path)

        p.setFont(lbl_font)
        p.setPen(QColor(_TEXT_MUTED))
        lw_px = lfm.horizontalAdvance(lbl)
        p.drawText(QRectF(c_rect.left() + 8.0, meta_y, lw_px + 2.0, meta_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, lbl)

        p.setFont(val_font)
        p.setPen(val_col)
        val_x = c_rect.left() + 14.0 + lw_px
        val_elided = vfm.elidedText(val, Qt.TextElideMode.ElideRight, max(20.0, c_rect.right() - val_x - 6.0))
        p.drawText(QRectF(val_x, meta_y, max(20.0, c_rect.right() - val_x - 6.0), meta_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, val_elided)

        meta_x += c_rect.width() + 8.0

    # Gold-fade horizontal divider below header
    div_y = 130.0
    div_grad = QLinearGradient(24.0, div_y, lw - 24.0, div_y)
    div_grad.setColorAt(0.0, QColor(120, 90, 40, 0))
    div_grad.setColorAt(0.18, QColor(120, 90, 40, 150))
    div_grad.setColorAt(0.5, QColor(200, 170, 110, 220))
    div_grad.setColorAt(0.82, QColor(120, 90, 40, 150))
    div_grad.setColorAt(1.0, QColor(120, 90, 40, 0))
    p.setPen(QPen(QBrush(div_grad), 1.0))
    p.drawLine(QPointF(24.0, div_y), QPointF(lw - 24.0, div_y))
    _draw_diamond(p, lw * 0.5, div_y, 3.2, QColor(_GOLD_PRIMARY))


def _draw_combo_and_augments(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 5: Combo Highlight Banner + up to 4 rarity-coded Augment pills (zero overlap)."""
    left = 24.0
    avail_w = lw - 48.0
    combo_txt = (data.combo_title or "").strip()

    if combo_txt:
        banner_rect = QRectF(left, 138.0, avail_w, 34.0)
        banner_path = _chamfer_path(banner_rect, 6.0)

        if data.combo_completed:
            fill_g = QLinearGradient(banner_rect.left(), banner_rect.top(), banner_rect.right(), banner_rect.bottom())
            fill_g.setColorAt(0.0, QColor("#2A143A"))
            fill_g.setColorAt(0.48, QColor("#2D210E"))
            fill_g.setColorAt(1.0, QColor("#0A2535"))
            p.fillPath(banner_path, QBrush(fill_g))

            rim_g = QLinearGradient(banner_rect.left(), banner_rect.top(), banner_rect.right(), banner_rect.top())
            rim_g.setColorAt(0.0, QColor("#E879F9"))
            rim_g.setColorAt(0.45, QColor(_GOLD_CREAM))
            rim_g.setColorAt(0.75, QColor(_GOLD_PRIMARY))
            rim_g.setColorAt(1.0, QColor(_CYAN_PRIMARY))
            p.setPen(QPen(QBrush(rim_g), 1.5))
            p.drawPath(banner_path)
            tag_text = "★ 体系成型"
            tag_bg = QColor(200, 155, 60, 68)
            tag_border = QColor(_GOLD_CREAM)
            tag_fg = QColor("#FFF4D4")
            title_fg = QColor("#FFF4D4")
        else:
            fill_g = QLinearGradient(banner_rect.left(), banner_rect.top(), banner_rect.right(), banner_rect.bottom())
            fill_g.setColorAt(0.0, QColor("#142236"))
            fill_g.setColorAt(1.0, QColor("#0A1525"))
            p.fillPath(banner_path, QBrush(fill_g))
            p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.2))
            p.drawPath(banner_path)
            tag_text = "◆ 核心连招"
            tag_bg = QColor(200, 170, 110, 38)
            tag_border = QColor(_GOLD_PRIMARY)
            tag_fg = QColor(_GOLD_CREAM)
            title_fg = QColor(_GOLD_CREAM)

        # Left tag badge inside combo banner
        tag_font = _sans_font(11, bold=True, letter_spacing=0.6)
        tfm = QFontMetricsF(tag_font)
        tag_w = tfm.horizontalAdvance(tag_text) + 16.0
        tag_rect = QRectF(banner_rect.left() + 8.0, banner_rect.top() + 6.0, tag_w, 22.0)
        tag_path = _chamfer_path(tag_rect, 3.5)
        p.fillPath(tag_path, tag_bg)
        p.setPen(QPen(tag_border, 1.0))
        p.drawPath(tag_path)
        p.setFont(tag_font)
        p.setPen(tag_fg)
        p.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag_text)

        # Main combo title text + subtle right status mark
        right_mark = "COMBO ONLINE" if data.combo_completed else "SYNERGY PLAN"
        rm_font = _latin_font(9, bold=True, letter_spacing=1.3)
        rm_fm = QFontMetricsF(rm_font)
        rm_w = rm_fm.horizontalAdvance(right_mark) + 12.0
        p.setFont(rm_font)
        p.setPen(QColor(94, 234, 212, 185) if data.combo_completed else QColor(200, 170, 110, 150))
        p.drawText(
            QRectF(banner_rect.right() - rm_w - 8.0, banner_rect.top(), rm_w, banner_rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            right_mark,
        )

        c_font = _sans_font(15, bold=True, letter_spacing=0.5)
        cfm = QFontMetricsF(c_font)
        tx = tag_rect.right() + 12.0
        tw = max(40.0, banner_rect.right() - tx - rm_w - 18.0)
        elided_combo = cfm.elidedText(combo_txt, Qt.TextElideMode.ElideRight, tw)
        t_rect = QRectF(tx, banner_rect.top(), tw, banner_rect.height())
        p.setFont(c_font)
        p.setPen(QColor(0, 0, 0, 200))
        p.drawText(t_rect.translated(0.0, 1.2), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_combo)
        p.setPen(title_fg)
        p.drawText(t_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_combo)

        aug_y = 180.0
        aug_h = 56.0
    else:
        # Subtle section header when no combo title is provided
        sec_font = _sans_font(11, bold=True, letter_spacing=0.8)
        p.setFont(sec_font)
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(QRectF(left, 139.0, 220.0, 18.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "◆ 本局海克斯强化")
        aug_y = 162.0
        aug_h = 68.0

    augments = list(data.augments[:4])
    if not augments:
        empty_rect = QRectF(left, aug_y, avail_w, aug_h)
        empty_path = _chamfer_path(empty_rect, 5.0)
        p.fillPath(empty_path, QColor(8, 18, 32, 185))
        p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0, Qt.PenStyle.DashLine))
        p.drawPath(empty_path)
        p.setFont(_sans_font(12, bold=False))
        p.setPen(QColor(_TEXT_FAINT))
        p.drawText(empty_rect, Qt.AlignmentFlag.AlignCenter, "本局暂无已记录的海克斯强化")
        return

    n = len(augments)
    gap = 10.0
    pill_w = (avail_w - gap * (n - 1)) / n if n > 1 else min(340.0, avail_w)

    for idx, (aug_name, rarity_raw) in enumerate(augments):
        rk = _normalize_rarity(rarity_raw)
        pal = _RARITY_PALETTE[rk]
        accent = QColor(pal["accent"])
        bright = QColor(pal["bright"])

        px = left + idx * (pill_w + gap)
        rect = QRectF(px, aug_y, pill_w, aug_h)
        path = _chamfer_path(rect, 5.0)

        # Pill fill gradient
        pg = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        pg.setColorAt(0.0, QColor(pal["bg_top"]))
        pg.setColorAt(1.0, QColor(pal["bg_bot"]))
        p.fillPath(path, QBrush(pg))

        # Subtle inner left rarity wash
        p.save()
        p.setClipPath(path)
        wash = QLinearGradient(rect.left(), rect.top(), rect.left() + 56.0, rect.top())
        wash.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 46))
        wash.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.fillRect(rect, QBrush(wash))

        # Left 3.5px vertical rarity bar
        p.fillRect(QRectF(rect.left(), rect.top(), 3.5, rect.height()), accent)

        # Subtle top specular edge
        p.setPen(QPen(QColor(bright.red(), bright.green(), bright.blue(), 65), 1.0))
        p.drawLine(QPointF(rect.left() + 4.0, rect.top() + 1.0), QPointF(rect.right() - 4.0, rect.top() + 1.0))
        p.restore()

        # Crisp chamfered rarity border
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 195), 1.25))
        p.drawPath(path)

        # Top row: diamond + rarity label + slot index
        top_row_y = rect.top() + 7.0
        _draw_diamond(p, rect.left() + 14.0, top_row_y + 7.0, 3.3, accent)

        r_font = _sans_font(10, bold=True, letter_spacing=0.6)
        p.setFont(r_font)
        p.setPen(bright)
        p.drawText(QRectF(rect.left() + 22.0, top_row_y, pill_w - 50.0, 15.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, pal["label"])

        idx_font = _latin_font(9, bold=True, letter_spacing=1.0)
        p.setFont(idx_font)
        p.setPen(QColor(bright.red(), bright.green(), bright.blue(), 120))
        p.drawText(QRectF(rect.right() - 32.0, top_row_y, 24.0, 15.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"0{idx + 1}")

        # Main augment title (auto-shrinks slightly for long names, elides as safety net)
        clean_name = (aug_name or "未知强化").strip()
        text_avail = pill_w - 20.0
        a_font = _sans_font(14, bold=True)
        afm = QFontMetricsF(a_font)
        if afm.horizontalAdvance(clean_name) > text_avail:
            a_font = _sans_font(12, bold=True)
            afm = QFontMetricsF(a_font)
        elided_name = afm.elidedText(clean_name, Qt.TextElideMode.ElideRight, text_avail)

        name_rect = QRectF(rect.left() + 11.0, rect.top() + 25.0, text_avail, rect.height() - 29.0)
        p.setFont(a_font)
        p.setPen(QColor(0, 0, 0, 190))
        p.drawText(name_rect.translated(0.0, 1.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)


def _draw_items_row(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 6: Up to 6 beveled 42x42 item icons (or gold-bordered text chips when PNG missing)."""
    left = 24.0
    y_top = 246.0
    row_h = 58.0

    # Left section label + crisp vertical gold hairline separator
    lbl_rect = QRectF(left, y_top + 6.0, 86.0, 22.0)
    p.setFont(_sans_font(12, bold=True, letter_spacing=0.6))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(lbl_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "◆ 终局出装")

    p.setFont(_latin_font(9, bold=True, letter_spacing=1.2))
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(QRectF(left + 11.0, y_top + 28.0, 82.0, 16.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "FINAL BUILD")

    vdiv_x = left + 92.0
    vdiv_g = QLinearGradient(vdiv_x, y_top + 2.0, vdiv_x, y_top + 52.0)
    vdiv_g.setColorAt(0.0, QColor(120, 90, 40, 0))
    vdiv_g.setColorAt(0.5, QColor(200, 170, 110, 145))
    vdiv_g.setColorAt(1.0, QColor(120, 90, 40, 0))
    p.setPen(QPen(QBrush(vdiv_g), 1.0))
    p.drawLine(QPointF(vdiv_x, y_top + 2.0), QPointF(vdiv_x, y_top + 52.0))

    track_x = left + 104.0
    track_w = (lw - 24.0) - track_x
    items = list(data.item_entries[:6])

    if not items:
        empty_r = QRectF(track_x, y_top + 6.0, track_w, 42.0)
        empty_p = _chamfer_path(empty_r, 4.0)
        p.fillPath(empty_p, QColor(8, 18, 32, 185))
        p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0, Qt.PenStyle.DashLine))
        p.drawPath(empty_p)
        p.setFont(_sans_font(11, bold=False))
        p.setPen(QColor(_TEXT_FAINT))
        p.drawText(empty_r, Qt.AlignmentFlag.AlignCenter, "暂未采集到终局装备数据")
        return

    max_slots = 6
    gap = 8.0
    slot_w = (track_w - gap * (max_slots - 1)) / max_slots
    icon_sz = 42.0

    for idx, (item_name, icon_path) in enumerate(items):
        sx = track_x + idx * (slot_w + gap)
        clean_name = (item_name or f"装备{idx + 1}").strip()
        img = _load_local_image(icon_path)

        if img is not None:
            ix = sx + (slot_w - icon_sz) * 0.5
            iy = y_top + 1.0
            icon_rect = QRectF(ix, iy, icon_sz, icon_sz)
            icon_path_shape = _chamfer_path(icon_rect, 3.5)

            # Cast depth shadow
            p.save()
            p.translate(1.5, 2.0)
            p.fillPath(icon_path_shape, QColor(0, 0, 0, 145))
            p.restore()

            p.save()
            p.setClipPath(icon_path_shape)
            p.fillRect(icon_rect, QColor("#040E1A"))
            side = min(img.width(), img.height())
            cx0 = (img.width() - side) // 2
            cy0 = (img.height() - side) // 2
            p.drawImage(icon_rect, img.copy(cx0, cy0, side, side))
            p.restore()

            # Metallic gold beveled border
            g_rim = QLinearGradient(icon_rect.left(), icon_rect.top(), icon_rect.right(), icon_rect.bottom())
            g_rim.setColorAt(0.0, QColor(_GOLD_CREAM))
            g_rim.setColorAt(0.45, QColor(_GOLD_PRIMARY))
            g_rim.setColorAt(1.0, QColor(_GOLD_BRONZE))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QBrush(g_rim), 1.5))
            p.drawPath(icon_path_shape)

            # Subtle item name caption below icon
            cap_font = _sans_font(10, bold=False)
            cfm = QFontMetricsF(cap_font)
            elided_cap = cfm.elidedText(clean_name, Qt.TextElideMode.ElideRight, slot_w - 2.0)
            p.setFont(cap_font)
            p.setPen(QColor(_TEXT_MUTED))
            p.drawText(QRectF(sx, iy + icon_sz + 2.0, slot_w, row_h - icon_sz - 3.0), Qt.AlignmentFlag.AlignCenter, elided_cap)
        else:
            # Gold-bordered text chip fallback (height=42 to align harmoniously with icons)
            chip_rect = QRectF(sx + 1.0, y_top + 6.0, slot_w - 2.0, icon_sz)
            chip_path = _chamfer_path(chip_rect, 4.0)

            c_bg = QLinearGradient(chip_rect.left(), chip_rect.top(), chip_rect.left(), chip_rect.bottom())
            c_bg.setColorAt(0.0, QColor("#152336"))
            c_bg.setColorAt(1.0, QColor("#0A1422"))
            p.fillPath(chip_path, QBrush(c_bg))

            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.3))
            p.drawPath(chip_path)

            # Tiny top specular line
            p.setPen(QPen(QColor(240, 230, 210, 50), 1.0))
            p.drawLine(QPointF(chip_rect.left() + 4.0, chip_rect.top() + 1.2), QPointF(chip_rect.right() - 4.0, chip_rect.top() + 1.2))

            chip_font = _sans_font(10, bold=True)
            tfm = QFontMetricsF(chip_font)
            wrapped = _wrap_text_max_lines(clean_name, tfm, chip_rect.width() - 10.0, max_lines=2)
            p.setFont(chip_font)
            p.setPen(QColor(_GOLD_CREAM))
            if len(wrapped) <= 1:
                p.drawText(chip_rect.adjusted(4.0, 0.0, -4.0, 0.0), Qt.AlignmentFlag.AlignCenter, wrapped[0] if wrapped else clean_name)
            else:
                line_h = 14.0
                start_y = chip_rect.center().y() - line_h
                for li, line in enumerate(wrapped[:2]):
                    p.drawText(
                        QRectF(chip_rect.left() + 4.0, start_y + li * line_h, chip_rect.width() - 8.0, line_h),
                        Qt.AlignmentFlag.AlignCenter,
                        line,
                    )


def _draw_ai_highlight_box(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 7: Cyan-left-accented dark panel (#061A2A) displaying ◈ AI 副驾高光 + 2-line wrapped text."""
    box = QRectF(24.0, 314.0, lw - 48.0, 70.0)
    path = _chamfer_path(box, 5.0)

    bg = QLinearGradient(box.left(), box.top(), box.right(), box.bottom())
    bg.setColorAt(0.0, QColor(_COACH_PANEL_BG))
    bg.setColorAt(1.0, QColor("#04121E"))
    p.fillPath(path, QBrush(bg))

    p.save()
    p.setClipPath(path)
    # Soft cyan wash fading rightward from left accent bar
    wash = QLinearGradient(box.left(), box.top(), box.left() + 165.0, box.top())
    wash.setColorAt(0.0, QColor(10, 200, 185, 38))
    wash.setColorAt(1.0, QColor(10, 200, 185, 0))
    p.fillRect(box, QBrush(wash))

    # Crisp 3.5px Hextech cyan left accent bar
    p.fillRect(QRectF(box.left(), box.top(), 3.5, box.height()), QColor(_CYAN_PRIMARY))
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(3, 151, 171, 165), 1.1))
    p.drawPath(path)

    # Header line inside AI panel
    hdr_font = _sans_font(12, bold=True, letter_spacing=0.6)
    p.setFont(hdr_font)
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(box.left() + 14.0, box.top() + 7.0, 200.0, 18.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "◈ AI 副驾高光")

    tag_font = _latin_font(9, bold=True, letter_spacing=1.3)
    p.setFont(tag_font)
    p.setPen(QColor(3, 151, 171, 190))
    p.drawText(QRectF(box.right() - 180.0, box.top() + 8.0, 168.0, 16.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "TACTICAL INSIGHT")

    # Body text (max 2 cleanly wrapped lines)
    raw_hi = (data.ai_highlight or "").strip()
    is_default = not raw_hi
    body_text = raw_hi or "本局由 HexHelper 全程零注入监测大数据胜率出装与海克斯选牌。"

    body_font_px = _sans_font(13, bold=False)
    bfm = QFontMetricsF(body_font_px)
    text_x = box.left() + 14.0
    text_w = box.width() - 26.0
    lines = _wrap_text_max_lines(body_text, bfm, text_w, max_lines=2)

    p.setFont(body_font_px)
    p.setPen(QColor(_TEXT_MUTED if is_default else _GOLD_CREAM))
    line_h = 19.0
    start_y = box.top() + (36.0 if len(lines) == 1 else 28.0)
    for idx, line in enumerate(lines[:2]):
        p.drawText(
            QRectF(text_x, start_y + idx * line_h, text_w, line_h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            line,
        )


def _draw_footer_watermark(p: QPainter, lw: float, lh: float, data: MatchRecapData) -> None:
    """Layer 8: Subtle footer hairline + HexHelper emblem + watermark line."""
    sep_y = lh - 39.0
    p.setPen(QPen(QColor(120, 90, 40, 88), 1.0))
    p.drawLine(QPointF(26.0, sep_y), QPointF(lw - 26.0, sep_y))

    patch = (data.dataset_patch or "26.23").strip()
    prefix = "HEXHELPER"
    suffix = f" · 海克斯大乱斗 AI 副驾 · 零注入纯只读 · {patch} 大数据"

    pre_font = _latin_font(10, bold=True, letter_spacing=1.5)
    suf_font = _sans_font(10, bold=False, letter_spacing=0.4)
    pfm = QFontMetricsF(pre_font)
    sfm = QFontMetricsF(suf_font)

    icon_sz = 14.0
    icon_gap = 6.0
    pre_w = pfm.horizontalAdvance(prefix)
    suf_w = sfm.horizontalAdvance(suffix)
    total_w = icon_sz + icon_gap + pre_w + suf_w

    fy = sep_y + 4.0
    fh = 22.0
    start_x = (lw - total_w) * 0.5

    emblem = _load_emblem_image()
    if emblem is not None:
        p.drawImage(QRectF(start_x, fy + (fh - icon_sz) * 0.5, icon_sz, icon_sz), emblem)
    else:
        _draw_diamond(p, start_x + icon_sz * 0.5, fy + fh * 0.5, 5.0, QColor(_CYAN_PRIMARY))
        _draw_diamond(p, start_x + icon_sz * 0.5, fy + fh * 0.5, 2.5, QColor(_GOLD_CREAM))

    p.setFont(pre_font)
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(
        QRectF(start_x + icon_sz + icon_gap, fy, pre_w + 4.0, fh),
        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        prefix,
    )

    p.setFont(suf_font)
    p.setPen(QColor(_TEXT_MUTED))
    p.drawText(
        QRectF(start_x + icon_sz + icon_gap + pre_w, fy, suf_w + 8.0, fh),
        Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
        suffix,
    )


def render_match_recap_image(
    data: MatchRecapData,
    width: int = CARD_DEFAULT_WIDTH,
    height: int = CARD_DEFAULT_HEIGHT,
) -> QImage:
    """Render an offline LOL V16.23 Hextech match recap card purely with Qt QPainter."""
    _ensure_card_fonts()
    w = max(320, int(width))
    h = max(220, int(height))

    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(_BG_DARK_START))

    scale = min(w / float(CARD_DEFAULT_WIDTH), h / float(CARD_DEFAULT_HEIGHT))
    lw = w / scale
    lh = h / scale

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if abs(scale - 1.0) > 1e-4:
        p.scale(scale, scale)

    _draw_background_and_frame(p, lw, lh, data)
    _draw_top_ribbon(p, lw, data)
    _draw_header(p, lw, data)
    _draw_combo_and_augments(p, lw, data)
    _draw_items_row(p, lw, data)
    _draw_ai_highlight_box(p, lw, data)
    _draw_footer_watermark(p, lw, lh, data)
    p.end()
    return img


def copy_recap_to_clipboard(
    data: MatchRecapData,
    width: int = CARD_DEFAULT_WIDTH,
    height: int = CARD_DEFAULT_HEIGHT,
) -> QImage:
    """Render match recap card and copy the QImage directly to the system clipboard."""
    img = render_match_recap_image(data, width=width, height=height)
    cb = QGuiApplication.clipboard()
    if cb is not None:
        cb.setImage(img)
    return img


class MatchRecapDialog(QDialog):
    """Hextech dark-blue preview modal with 1-click clipboard copy & PNG export."""

    def __init__(
        self,
        data: MatchRecapData,
        parent: QWidget | None = None,
        *,
        card_width: int = CARD_DEFAULT_WIDTH,
        card_height: int = CARD_DEFAULT_HEIGHT,
        lifecycle_state=None,
        snoozed: bool = False,
        on_core_action=None,
    ) -> None:
        super().__init__(parent)
        self.data = data
        self._card_image: QImage = render_match_recap_image(data, width=card_width, height=card_height)
        self._on_core_action = on_core_action
        self._recap_cta_action = ""

        hero_label = (data.hero_name or data.hero_id or "对局").strip()
        self.setWindowTitle(f"HexHelper · {hero_label} 高光战报卡")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
                font-family: "Microsoft YaHei UI", "Segoe UI Symbol", "Segoe UI", sans-serif;
            }
            QDialog QLabel {
                font-family: "Microsoft YaHei UI", "Segoe UI Symbol", "Segoe UI", sans-serif;
            }
            QFrame#RecapPreviewFrame {
                background-color: #020912;
                border: 1px solid #785A28;
                border-top: 2px solid #C8AA6E;
                border-radius: 2px;
            }
            """
        )

        self._copy_reset_timer = QTimer(self)
        self._copy_reset_timer.setSingleShot(True)
        self._copy_reset_timer.timeout.connect(self._reset_copy_btn_text)

        self._save_reset_timer = QTimer(self)
        self._save_reset_timer.setSingleShot(True)
        self._save_reset_timer.timeout.connect(self._reset_save_btn_text)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # Top header bar
        hdr_row = QHBoxLayout()
        hdr_row.setSpacing(10)
        title_lbl = QLabel("✦ 海克斯局末高光战报卡")
        title_lbl.setFont(_sans_font(16, bold=True, letter_spacing=1.0))
        title_lbl.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")

        hint_lbl = QLabel("一键复制图片，去微信 / QQ / NGA / 贴吧直接 Ctrl+V 粘贴")
        hint_lbl.setFont(_sans_font(12, bold=False))
        hint_lbl.setStyleSheet("color: #0AC8B9; background: transparent; border: none;")
        hdr_row.addWidget(title_lbl)
        hdr_row.addStretch(1)
        hdr_row.addWidget(hint_lbl)
        root.addLayout(hdr_row)

        # Preview card container
        preview_frame = QFrame(self)
        preview_frame.setObjectName("RecapPreviewFrame")
        pf_lay = QVBoxLayout(preview_frame)
        pf_lay.setContentsMargins(6, 6, 6, 6)

        self.preview_label = QLabel(preview_frame)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background: transparent; border: none;")
        pm = QPixmap.fromImage(self._card_image)
        max_preview_w = 760
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail_w = screen.availableGeometry().width()
            if avail_w > 0:
                max_preview_w = min(max_preview_w, max(480, avail_w - 120))
        if pm.width() > max_preview_w:
            pm = pm.scaledToWidth(max_preview_w, Qt.TransformationMode.SmoothTransformation)
        self.preview_label.setPixmap(pm)
        pf_lay.addWidget(self.preview_label)
        root.addWidget(preview_frame)

        # Optional post-match outer nudge strip (STRICTLY outside the exported PNG image!)
        self.core_nudge_bar: QFrame | None = None
        if lifecycle_state is not None and not snoozed:
            from hex_helper.ui.hex_seal_overlay import CoreEnergyState

            nudge_txt = ""
            nudge_btn_txt = ""
            nudge_action = ""
            if lifecycle_state == CoreEnergyState.GUEST_UNCLAIMED:
                nudge_txt = "◈ 本局尚未启用 AI 战报点评 · 领取 450 免费积分，下局解锁 10 人开局定盘与专属高光复盘"
                nudge_btn_txt = "免费领 450 积分"
                nudge_action = "register"
            elif lifecycle_state == CoreEnergyState.CORRODED_TRIAL_END:
                nudge_txt = "◇ 本局 AI 高光点评处于休眠封印 · 充能后解锁下局 10 人定盘与精准散件反制（服务期内有效）"
                nudge_btn_txt = "查看差异 / 充能"
                nudge_action = "preview"
            elif lifecycle_state == CoreEnergyState.CORRODED_RETURNING:
                nudge_txt = "◇ AI 核心能量已耗尽 · 一键补能恢复下局 10 人实时推演与高光战报点评"
                nudge_btn_txt = "快速补能"
                nudge_action = "recharge"
            elif lifecycle_state == CoreEnergyState.TRIAL_LOW:
                nudge_txt = "✦ 免费体验额度即将用尽 · 小额按量分摊算力（¥5 起，服务期内有效，未消耗实付可退）"
                nudge_btn_txt = "去充能"
                nudge_action = "recharge"

            if nudge_txt:
                self._recap_cta_action = nudge_action
                bar = QFrame(self)
                bar.setStyleSheet(
                    "QFrame { background-color: rgba(8, 22, 36, 225); border: 1px solid #785A28; "
                    "border-left: 3px solid #0AC8B9; border-radius: 2px; }"
                )
                bl = QHBoxLayout(bar)
                bl.setContentsMargins(10, 6, 10, 6)
                bl.setSpacing(10)
                nl = QLabel(nudge_txt)
                nl.setWordWrap(True)
                nl.setStyleSheet("border: none; background: transparent; color: #E2D9C5; font-size: 11px; font-weight: bold;")
                nb = HextechActionButton(nudge_btn_txt, variant="cyan" if nudge_action == "register" else "gold", height=26, parent=bar)
                nb.clicked.connect(self._trigger_core_nudge)
                bl.addWidget(nl, 1)
                bl.addWidget(nb)
                root.addWidget(bar)
                self.core_nudge_bar = bar

        # Bottom Hextech action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        self.copy_btn = HextechActionButton(_COPY_BTN_DEFAULT_TEXT, variant="cyan", height=38, parent=self)
        self.copy_btn.setMinimumWidth(264)
        self.copy_btn.clicked.connect(self.copy_to_clipboard)

        self.save_btn = HextechActionButton(_SAVE_BTN_DEFAULT_TEXT, variant="gold", height=38, parent=self)
        self.save_btn.setMinimumWidth(132)
        self.save_btn.clicked.connect(self.save_image_dialog)

        self.close_btn = HextechActionButton("关闭", variant="gold", height=38, parent=self)
        self.close_btn.setMinimumWidth(96)
        self.close_btn.clicked.connect(self.accept)

        btn_row.addWidget(self.copy_btn)
        btn_row.addWidget(self.save_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.close_btn)
        root.addLayout(btn_row)

        self.adjustSize()

    @property
    def card_image(self) -> QImage:
        return self._card_image

    def default_save_filename(self) -> str:
        hero_part = _safe_filename_part(self.data.hero_name or self.data.hero_id or "match")
        return f"HexHelper_{hero_part}_战报.png"

    def _trigger_core_nudge(self) -> None:
        if self._recap_cta_action and self._on_core_action is not None:
            self._on_core_action(self._recap_cta_action)

    def _reset_copy_btn_text(self) -> None:
        self.copy_btn.setText(_COPY_BTN_DEFAULT_TEXT)

    def _reset_save_btn_text(self) -> None:
        self.save_btn.setText(_SAVE_BTN_DEFAULT_TEXT)

    def copy_to_clipboard(self) -> QImage:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setImage(self._card_image)
        self.copy_btn.setText(_COPY_BTN_COPIED_TEXT)
        self._copy_reset_timer.start(2600)
        return self._card_image

    def save_to_path(self, path: str | Path) -> bool:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        return bool(self._card_image.save(str(target), "PNG"))

    def save_image_dialog(self) -> str:
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            "保存海克斯战报图",
            self.default_save_filename(),
            "PNG 图片 (*.png)",
        )
        if not chosen:
            return ""
        if self.save_to_path(chosen):
            self.save_btn.setText(_SAVE_BTN_SAVED_TEXT)
            self._save_reset_timer.start(2200)
            return chosen
        return ""
