"""Offline LOL V16.23 Hextech match recap card v2 renderer & 1-click clipboard share dialog."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Callable

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

# Canonical logical design dimensions (generous 760x480 layout: 100% zero item-name clipping)
CARD_DEFAULT_WIDTH = 760
CARD_DEFAULT_HEIGHT = 480

# Authentic LOL V16.23 Hextech Palette (strictly desktop Hextech; ZERO Web Tactile Orange)
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
_DEFEAT_CRIMSON = "#D96674"
_DEFEAT_CRIMSON_SOFT = "#F5B8C0"
_DEFEAT_BG_TOP = "#2B111A"
_DEFEAT_BG_BOT = "#120A14"
_PANEL_DARK_LEFT = "#061626"
_PANEL_DARK_RIGHT = "#061A2A"
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

_STAT_KEY_MAP: dict[str, str] = {
    "ad": "ad",
    "attackdamage": "ad",
    "attack_damage": "ad",
    "ap": "ap",
    "abilitypower": "ap",
    "ability_power": "ap",
    "hp": "hp",
    "maxhealth": "hp",
    "max_health": "hp",
    "health": "hp",
    "armor": "armor",
    "arm": "armor",
    "mr": "mr",
    "magicresist": "mr",
    "magic_resist": "mr",
    "spellblock": "mr",
    "ah": "ah",
    "abilityhaste": "ah",
    "ability_haste": "ah",
    "as": "as",
    "attackspeed": "as",
    "attack_speed": "as",
    "ms": "ms",
    "movespeed": "ms",
    "move_speed": "ms",
}

# (canonical_key, zh_label, short_en, mayhem_gold_threshold)
_STAT_DISPLAY_SPECS: tuple[tuple[str, str, str, float], ...] = (
    ("ad", "攻击", "AD", 450.0),
    ("ap", "法强", "AP", 750.0),
    ("hp", "生命", "HP", 8000.0),
    ("ah", "急速", "AH", 140.0),
    ("armor", "护甲", "ARM", 300.0),
    ("mr", "魔抗", "MR", 220.0),
    ("as", "攻速", "AS", 2.5),
    ("ms", "移速", "MS", 520.0),
)

_CARD_FONTS_READY = False
_CINZEL_FAMILY = "Cinzel"
_COPY_BTN_DEFAULT_TEXT = "📋 一键复制战报图"
_COPY_BTN_COPIED_TEXT = "✓ 已复制，去群里 Ctrl+V 粘贴！"
_SAVE_BTN_DEFAULT_TEXT = "💾 保存图片"
_SAVE_BTN_SAVED_TEXT = "✓ 已保存图片"

_KDA_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)\s*$")
_TEAM_KILLS_RE = re.compile(r"^\s*(\d+)\s*[:\-/]\s*(\d+)\s*$")
_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


@dataclass(frozen=True)
class MatchRecapData:
    """Immutable snapshot of a completed ARAM Mayhem match for v2 card rendering."""

    hero_id: str
    hero_name: str
    hero_icon_path: str = ""
    won: int = -1  # 1 = 胜利, 0 = 惜败, -1 = 激战局
    kda: str = ""
    duration_s: int = 0
    team_kills: str = ""
    is_first_prismatic: bool = False
    augments: tuple[tuple[str, str], ...] = ()  # (name, rarity) up to 4
    combo_title: str = ""
    combo_completed: bool = False
    item_entries: tuple[tuple[str, str], ...] = ()  # (item_name, icon_path) up to 6
    item_ids: tuple[str, ...] = ()  # parallel raw item IDs for async icon hydration
    ai_highlight: str = ""  # Retained for backward compatibility with legacy callers/tests
    dataset_patch: str = "26.23"
    # v2 telemetry & visual fields
    augment_icon_paths: tuple[str, ...] = ()  # parallel local PNG paths for the 4 augments
    final_stats: dict[str, float] = field(default_factory=dict)
    damage_dealt: int = 0
    damage_share: float = 0.0
    damage_taken: int = 0
    heal_and_shield: int = 0
    gold_earned: int = 0
    kill_participation: float = 0.0
    match_score: float = 0.0
    rating_grade: str = ""
    mvp_badge: str = ""  # "MVP" | "SVP" | ""


def normalize_final_stats(raw_stats: dict[str, float] | None) -> dict[str, float]:
    """Normalize LiveClient or canonical stat dictionaries into {ad, ap, hp, armor, mr, ah, as, ms}."""
    if not isinstance(raw_stats, dict) or not raw_stats:
        return {}
    out: dict[str, float] = {}
    for raw_k, raw_v in raw_stats.items():
        norm_k = _STAT_KEY_MAP.get(str(raw_k or "").strip().lower())
        if not norm_k:
            continue
        try:
            val = float(raw_v)
        except (TypeError, ValueError):
            continue
        if val != val or val < 0.0:  # guard NaN / negative
            continue
        out[norm_k] = val
    return out


def _grade_for_score(score: float) -> str:
    if score >= 12.8:
        return "S+"
    if score >= 11.0:
        return "S"
    if score >= 9.5:
        return "A+"
    if score >= 8.0:
        return "A"
    return "B"


def compute_match_rating(
    kda: str | float = "",
    won: int = -1,
    damage_share: float = 0.0,
    damage_taken_share: float = 0.0,
    kp: float = 0.0,
    gold_per_min: float = 0.0,
) -> tuple[float, str, str]:
    """Compute a transparent 0.0-16.0 ARAM Mayhem combat rating.

    Returns ``(score, grade, badge)`` where ``grade`` is in ``{"S+", "S", "A+", "A", "B"}``
    and ``badge`` is ``"MVP"``, ``"SVP"``, or ``""``.
    """
    has_kda = False
    kda_ratio = 0.0
    if isinstance(kda, (int, float)):
        v = float(kda)
        if v > 0.0:
            kda_ratio = v
            has_kda = True
    elif isinstance(kda, str) and kda.strip():
        m = _KDA_RE.match(kda)
        if m:
            k, d, a = int(m.group(1)), int(m.group(2)), int(m.group(3))
            has_kda = True
            if d == 0:
                kda_ratio = min(12.0, (k + a) * 0.85) if (k + a) > 0 else 0.0
            else:
                kda_ratio = (k + a) / float(d)

    def _norm_share(val: float) -> float:
        try:
            f = float(val or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if f <= 0.0:
            return 0.0
        return min(1.0, f / 100.0 if f > 1.0 else f)

    dmg_s = _norm_share(damage_share)
    tank_s = _norm_share(damage_taken_share)
    kp_s = _norm_share(kp)
    try:
        gpm = max(0.0, float(gold_per_min or 0.0))
    except (TypeError, ValueError):
        gpm = 0.0

    has_combat = (dmg_s > 0.0) or (tank_s > 0.0) or (kp_s > 0.0) or (gpm > 0.0)
    if not has_kda and not has_combat:
        return 0.0, "", ""

    win_bonus = 0.9 if won == 1 else (-0.2 if won == 0 else 0.0)
    if has_combat:
        kda_pts = min(4.2, kda_ratio * 0.82)
        dmg_pts = min(3.4, max(0.0, dmg_s - 0.10) * 14.0)
        tank_pts = min(2.4, max(0.0, tank_s - 0.15) * 11.0)
        combat_pts = min(4.2, dmg_pts + tank_pts * 0.72)
        kp_pts = min(1.8, max(0.0, kp_s - 0.35) * 4.0)
        gpm_pts = min(1.0, max(0.0, (gpm - 750.0) / 550.0))
        raw_score = 4.4 + kda_pts + combat_pts + kp_pts + gpm_pts + win_bonus
    else:
        # KDA-only fallback when EOG shares are unavailable
        raw_score = 5.4 + min(5.6, kda_ratio * 1.05) + (1.2 if won == 1 else (-0.3 if won == 0 else 0.0))

    score = round(max(1.0, min(16.0, raw_score)), 1)
    grade = _grade_for_score(score)
    if won == 1 and score >= 12.8:
        badge = "MVP"
    elif won == 0 and score >= 12.0:
        badge = "SVP"
    else:
        badge = ""
    return score, grade, badge


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


def _format_cn_metric(value: int) -> str:
    v = max(0, int(value or 0))
    if v <= 0:
        return "--"
    if v >= 10_000:
        return f"{v / 10_000.0:.1f}万"
    return f"{v:,}"


def _format_gold_k(value: int) -> str:
    v = max(0, int(value or 0))
    if v <= 0:
        return "--"
    if v >= 1000:
        return f"{v / 1000.0:.1f}K"
    return str(v)


def _format_stat_value(key: str, val: float) -> str:
    if key == "as":
        return f"{val:.2f}"
    iv = int(round(val))
    if key == "hp" and iv >= 1000:
        return f"{iv:,}"
    return str(iv)


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


def _derive_kill_participation(data: MatchRecapData) -> float:
    kp = float(data.kill_participation or 0.0)
    if kp > 0.0:
        return min(1.0, kp / 100.0 if kp > 1.0 else kp)
    m_kda = _KDA_RE.match(data.kda or "")
    m_tk = _TEAM_KILLS_RE.match(data.team_kills or "")
    if m_kda and m_tk:
        k, _d, a = int(m_kda.group(1)), int(m_kda.group(2)), int(m_kda.group(3))
        my_team_k = int(m_tk.group(1))
        if my_team_k > 0:
            return min(1.0, (k + a) / float(my_team_k))
    return 0.0


def _resolve_rating(data: MatchRecapData) -> tuple[float, str, str]:
    """Resolve explicit or auto-computed (score, grade, badge) for header rendering."""
    if float(data.match_score or 0.0) > 0.0:
        sc = round(max(0.1, min(16.0, float(data.match_score))), 1)
        grade = (data.rating_grade or "").strip() or _grade_for_score(sc)
        badge = (data.mvp_badge or "").strip().upper()
        if badge not in ("MVP", "SVP"):
            badge = ""
        return sc, grade, badge

    kp_eff = _derive_kill_participation(data)
    gpm = (float(data.gold_earned) / (float(data.duration_s) / 60.0)) if (data.gold_earned > 0 and data.duration_s >= 60) else 0.0
    sc, grade, badge = compute_match_rating(
        kda=data.kda,
        won=data.won,
        damage_share=data.damage_share,
        damage_taken_share=0.0,
        kp=kp_eff,
        gold_per_min=gpm,
    )
    if (data.rating_grade or "").strip():
        grade = data.rating_grade.strip()
    if (data.mvp_badge or "").strip():
        b_raw = data.mvp_badge.strip().upper()
        badge = b_raw if b_raw in ("MVP", "SVP") else ""
    return sc, grade, badge


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

        lo, hi = 1, len(rem)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if fm.horizontalAdvance(rem[:mid]) <= max_width:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1

        split_at = best
        if (
            best < len(rem)
            and rem[best - 1].isascii()
            and rem[best - 1].isalnum()
            and rem[best].isascii()
            and rem[best].isalnum()
        ):
            last_sp = rem.rfind(" ", 0, best)
            if last_sp >= max(1, int(best * 0.55)):
                split_at = last_sp

        chunk = rem[:split_at].rstrip()
        if not chunk:
            chunk = rem[:best]
            split_at = best
        lines.append(chunk)
        leading_trim = len(clean[idx:]) - len(rem)
        idx += leading_trim + split_at

    return lines


def _draw_background_and_frame(p: QPainter, lw: float, lh: float, data: MatchRecapData) -> None:
    """Layer 1 & 2: Deep navy-black Hextech gradient, glows, diagonal sheen, bevels, corner brackets."""
    full_rect = QRectF(0.0, 0.0, lw, lh)

    bg = QLinearGradient(0.0, 0.0, lw, lh)
    bg.setColorAt(0.0, QColor(_BG_DARK_START))
    bg.setColorAt(0.52, QColor(_BG_DARK_MID))
    bg.setColorAt(1.0, QColor(_BG_DARK_END))
    p.fillRect(full_rect, QBrush(bg))

    # Status-reactive top-left atmospheric glow
    if data.won == 1:
        top_glow = QRadialGradient(QPointF(150.0, 74.0), 300.0)
        top_glow.setColorAt(0.0, QColor(200, 170, 110, 48))
        top_glow.setColorAt(0.50, QColor(10, 200, 185, 18))
        top_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    elif data.won == 0:
        top_glow = QRadialGradient(QPointF(150.0, 74.0), 290.0)
        top_glow.setColorAt(0.0, QColor(190, 72, 92, 42))
        top_glow.setColorAt(0.55, QColor(200, 155, 60, 14))
        top_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    else:
        top_glow = QRadialGradient(QPointF(150.0, 74.0), 280.0)
        top_glow.setColorAt(0.0, QColor(10, 200, 185, 42))
        top_glow.setColorAt(0.55, QColor(200, 170, 110, 15))
        top_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(full_rect, QBrush(top_glow))

    # Lower-right Hextech cyan bloom behind combat telemetry dashboard
    br_glow = QRadialGradient(QPointF(lw - 135.0, lh - 92.0), 290.0)
    br_glow.setColorAt(0.0, QColor(10, 200, 185, 36))
    br_glow.setColorAt(0.58, QColor(3, 151, 171, 14))
    br_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(full_rect, QBrush(br_glow))

    # Subtle prismatic shimmer behind augments row when prismatic augment present
    has_prism = data.is_first_prismatic or data.combo_completed or any(
        _normalize_rarity(r) == "prismatic" for _, r in data.augments
    )
    if has_prism:
        prism_glow = QRadialGradient(QPointF(lw * 0.5, 182.0), 290.0)
        prism_glow.setColorAt(0.0, QColor(217, 70, 239, 26))
        prism_glow.setColorAt(0.6, QColor(147, 51, 234, 9))
        prism_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(full_rect, QBrush(prism_glow))

    outer_rect = QRectF(5.0, 5.0, lw - 10.0, lh - 10.0)
    outer_path = _chamfer_path(outer_rect, 8.0)
    p.save()
    p.setClipPath(outer_path)

    # Subtle diagonal architectural sheen bands
    sheen_specs = (
        (lw * 0.14, 54.0, QColor(200, 170, 110, 12)),
        (lw * 0.44, 34.0, QColor(10, 200, 185, 12)),
        (lw * 0.72, 68.0, QColor(200, 170, 110, 10)),
        (lw * 0.90, 28.0, QColor(240, 230, 210, 8)),
    )
    for x_start, band_w, col in sheen_specs:
        poly = QPolygonF(
            [
                QPointF(x_start, 0.0),
                QPointF(x_start + band_w, 0.0),
                QPointF(x_start + band_w - lh * 0.50, lh),
                QPointF(x_start - lh * 0.50, lh),
            ]
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawPolygon(poly)

    p.setPen(QPen(QColor(200, 170, 110, 22), 1.0))
    p.drawLine(QPointF(lw * 0.62, 0.0), QPointF(lw * 0.62 - lh * 0.50, lh))
    p.setPen(QPen(QColor(10, 200, 185, 22), 1.0))
    p.drawLine(QPointF(lw * 0.88, 0.0), QPointF(lw * 0.88 - lh * 0.50, lh))

    # Faint Hextech compass rings on upper-right background
    rcx, rcy = lw - 76.0, 86.0
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(200, 170, 110, 20), 1.0))
    p.drawEllipse(QPointF(rcx, rcy), 86.0, 86.0)
    p.setPen(QPen(QColor(10, 200, 185, 20), 1.0, Qt.PenStyle.DashLine))
    p.drawEllipse(QPointF(rcx, rcy), 116.0, 116.0)
    p.restore()

    # Outer soft ambient rim glow
    rim_aura = (
        QColor(200, 170, 110, 44)
        if data.won == 1
        else (QColor(217, 102, 116, 36) if data.won == 0 else QColor(10, 200, 185, 40))
    )
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(rim_aura, 3.2))
    p.drawPath(outer_path)

    # Outer metallic gold bevel
    gold_bevel = QLinearGradient(0.0, 0.0, lw, lh)
    gold_bevel.setColorAt(0.0, QColor(_GOLD_CREAM))
    gold_bevel.setColorAt(0.28, QColor(_GOLD_PRIMARY))
    gold_bevel.setColorAt(0.65, QColor(_GOLD_BRONZE))
    gold_bevel.setColorAt(1.0, QColor(_GOLD_PRIMARY))
    p.setPen(QPen(QBrush(gold_bevel), 1.6))
    p.drawPath(outer_path)

    # Inner dark antique-gold hairline
    inner_rect = QRectF(10.0, 10.0, lw - 20.0, lh - 20.0)
    inner_path = _chamfer_path(inner_rect, 5.0)
    p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0))
    p.drawPath(inner_path)

    # Top center gold keystone ornament
    top_mid = lw * 0.5
    p.setPen(QPen(QColor(_GOLD_CREAM), 1.8))
    p.drawLine(QPointF(top_mid - 58.0, 5.0), QPointF(top_mid + 58.0, 5.0))
    _draw_diamond(p, top_mid, 5.0, 3.8, QColor(_GOLD_CREAM))
    _draw_diamond(p, top_mid, 5.0, 1.8, QColor(_CYAN_PRIMARY if data.won != 0 else _DEFEAT_CRIMSON))

    # 4 Corner Hextech Brackets + rivets
    arm = 18.0
    m = 3.5
    rivet_col = QColor(_GOLD_PRIMARY if data.won == 1 else (_DEFEAT_CRIMSON if data.won == 0 else _CYAN_PRIMARY))
    bracket_pen = QPen(
        QColor(_GOLD_CREAM), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin
    )
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
    """Layer 3: Status badge (VICTORY / DEFEAT / ARAM) + 首棱彩局 + Duration + Team Kills + Patch."""
    y = 17.0
    h = 26.0
    x = 24.0

    # 1. Victory / Defeat / ARAM status badge
    if data.won == 1:
        badge_text = "✦ 胜利 · VICTORY"
        bg_top, bg_bot = QColor("#36260E"), QColor("#15110B")
        border_c1, border_c2 = QColor(_GOLD_CREAM), QColor(_CYAN_PRIMARY)
        glow_col = QColor(200, 170, 110, 74)
        text_col = QColor(_GOLD_CREAM)
    elif data.won == 0:
        badge_text = "◆ 惜败 · DEFEAT"
        bg_top, bg_bot = QColor(_DEFEAT_BG_TOP), QColor(_DEFEAT_BG_BOT)
        border_c1, border_c2 = QColor(_DEFEAT_CRIMSON_SOFT), QColor(_GOLD_PRIMARY)
        glow_col = QColor(217, 102, 116, 66)
        text_col = QColor(_DEFEAT_CRIMSON_SOFT)
    else:
        badge_text = "◈ 激战局 · ARAM"
        bg_top, bg_bot = QColor("#0A3240"), QColor("#051721")
        border_c1, border_c2 = QColor(_CYAN_BRIGHT), QColor(_GOLD_PRIMARY)
        glow_col = QColor(10, 200, 185, 68)
        text_col = QColor(_CYAN_BRIGHT)

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

    x += badge_w + 8.0

    # 2. Optional 首棱彩局 prismatic tag
    if data.is_first_prismatic:
        prism_text = "✦ 首棱彩局"
        p_font = _sans_font(11, bold=True, letter_spacing=0.6)
        pfm = QFontMetricsF(p_font)
        pw = pfm.horizontalAdvance(prism_text) + 18.0
        p_rect = QRectF(x, y + 1.0, pw, h - 2.0)
        p_path = _chamfer_path(p_rect, 4.5)

        p.setPen(QPen(QColor(217, 70, 239, 64), 3.0))
        p.drawPath(p_path)

        p_fill = QLinearGradient(p_rect.left(), p_rect.top(), p_rect.right(), p_rect.bottom())
        p_fill.setColorAt(0.0, QColor("#36154D"))
        p_fill.setColorAt(1.0, QColor("#180A29"))
        p.fillPath(p_path, QBrush(p_fill))

        p_rim = QLinearGradient(p_rect.left(), p_rect.top(), p_rect.right(), p_rect.bottom())
        p_rim.setColorAt(0.0, QColor("#F5D0FE"))
        p_rim.setColorAt(0.5, QColor("#D946EF"))
        p_rim.setColorAt(1.0, QColor("#9333EA"))
        p.setPen(QPen(QBrush(p_rim), 1.3))
        p.drawPath(p_path)

        p.setFont(p_font)
        p.setPen(QColor("#FAE8FF"))
        p.drawText(p_rect, Qt.AlignmentFlag.AlignCenter, prism_text)

    # 3. Right-side compact chips: [Team Kills] [Duration] [PATCH]
    right_cursor = lw - 24.0
    chip_h = h - 2.0
    chip_y = y + 1.0

    def _draw_right_chip(
        label: str,
        value: str,
        *,
        val_color: QColor,
        border_color: QColor,
        latin_val: bool = False,
    ) -> None:
        nonlocal right_cursor
        lbl_f = _sans_font(10, bold=False)
        val_f = _latin_font(11, bold=True, letter_spacing=1.0) if latin_val else _sans_font(11, bold=True, letter_spacing=0.4)
        lfm = QFontMetricsF(lbl_f)
        vfm = QFontMetricsF(val_f)
        lw_p = lfm.horizontalAdvance(label) if label else 0.0
        vw_p = vfm.horizontalAdvance(value)
        inner_gap = 5.0 if label else 0.0
        cw = lw_p + inner_gap + vw_p + 18.0
        c_rect = QRectF(right_cursor - cw, chip_y, cw, chip_h)
        c_path = _chamfer_path(c_rect, 4.0)
        p.fillPath(c_path, QColor(9, 22, 38, 222))
        p.setPen(QPen(border_color, 1.05))
        p.drawPath(c_path)

        tx = c_rect.left() + 9.0
        if label:
            p.setFont(lbl_f)
            p.setPen(QColor(_TEXT_MUTED))
            p.drawText(QRectF(tx, chip_y, lw_p + 2.0, chip_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, label)
            tx += lw_p + inner_gap
        p.setFont(val_f)
        p.setPen(val_color)
        p.drawText(QRectF(tx, chip_y, vw_p + 4.0, chip_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, value)
        right_cursor -= cw + 7.0

    patch_str = f"PATCH {(data.dataset_patch or '26.23').strip()}"
    _draw_right_chip("", patch_str, val_color=QColor(_GOLD_PRIMARY), border_color=QColor(_GOLD_BRONZE), latin_val=True)

    dur_str = _format_duration(data.duration_s)
    if dur_str:
        _draw_right_chip("时长", dur_str, val_color=QColor(_GOLD_CREAM), border_color=QColor(_GOLD_HAIRLINE))

    tk_str = (data.team_kills or "").strip()
    if tk_str:
        _draw_right_chip("人头", tk_str, val_color=QColor(_CYAN_BRIGHT), border_color=QColor(3, 151, 171, 165))


def _draw_hero_portrait(p: QPainter, x: float, y: float, size: float, data: MatchRecapData) -> None:
    """62x62 circular hero portrait with metallic gold frame and monogram fallback."""
    cx = x + size * 0.5
    cy = y + size * 0.5
    r = size * 0.5

    halo_col = (
        QColor(200, 170, 110, 62)
        if data.won == 1
        else (QColor(217, 102, 116, 52) if data.won == 0 else QColor(10, 200, 185, 62))
    )
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
        side = min(img.width(), img.height())
        sx = (img.width() - side) // 2
        sy = (img.height() - side) // 2
        p.drawImage(inner_rect, img.copy(sx, sy, side, side))
    else:
        med = QRadialGradient(QPointF(cx, cy), r)
        med.setColorAt(0.0, QColor("#103A4F"))
        med.setColorAt(0.6, QColor("#091428"))
        med.setColorAt(1.0, QColor("#030A14"))
        p.fillRect(inner_rect, QBrush(med))

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(10, 200, 185, 115), 1.0))
        p.drawEllipse(inner_rect.adjusted(4.0, 4.0, -4.0, -4.0))

        hero_txt = (data.hero_name or "").strip()
        monogram = hero_txt[0] if hero_txt else ((data.hero_id or "H").strip()[:2].upper() or "H")
        m_font = _sans_font(25 if len(monogram) == 1 else 18, bold=True)
        p.setFont(m_font)
        p.setPen(QColor(0, 0, 0, 190))
        p.drawText(inner_rect.translated(0.0, 1.5), Qt.AlignmentFlag.AlignCenter, monogram)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(inner_rect, Qt.AlignmentFlag.AlignCenter, monogram)
    p.restore()

    ring_grad = QLinearGradient(x, y, x + size, y + size)
    ring_grad.setColorAt(0.0, QColor(_GOLD_CREAM))
    ring_grad.setColorAt(0.35, QColor(_GOLD_METAL))
    ring_grad.setColorAt(0.75, QColor(_GOLD_BRONZE))
    ring_grad.setColorAt(1.0, QColor(_GOLD_PRIMARY))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QBrush(ring_grad), 2.5))
    p.drawEllipse(QRectF(x + 1.25, y + 1.25, size - 2.5, size - 2.5))

    for dx, dy in ((0.0, -r), (r, 0.0), (0.0, r), (-r, 0.0)):
        _draw_diamond(p, cx + dx, cy + dy, 2.2, QColor(_GOLD_CREAM))


def _draw_header(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 4: Hero portrait + Hero name + Build Synergy Tag on left; KDA + Hardcore Rating Badge on right."""
    portrait_x = 24.0
    portrait_y = 55.0
    portrait_sz = 62.0
    _draw_hero_portrait(p, portrait_x, portrait_y, portrait_sz, data)

    # Right-side: [KDA Panel] + [Hardcore Rating Badge]
    score, grade, mvp_badge = _resolve_rating(data)
    right_x = lw - 24.0
    right_used_w = 0.0

    if score > 0.0 and grade:
        is_s_plus = grade == "S+"
        is_s = grade in ("S+", "S")
        rating_w = 136.0 if mvp_badge else 120.0
        r_rect = QRectF(right_x - rating_w, 54.0, rating_w, 64.0)
        r_path = _chamfer_path(r_rect, 7.0)

        # Metallic rating fill
        r_bg = QLinearGradient(r_rect.left(), r_rect.top(), r_rect.right(), r_rect.bottom())
        if is_s_plus:
            r_bg.setColorAt(0.0, QColor("#30220C"))
            r_bg.setColorAt(0.55, QColor("#122334"))
            r_bg.setColorAt(1.0, QColor("#071522"))
            rim_c1, rim_c2 = QColor(_GOLD_CREAM), QColor(_CYAN_PRIMARY)
            grade_col = QColor(_GOLD_CREAM)
            aura_col = QColor(200, 170, 110, 62)
        elif is_s:
            r_bg.setColorAt(0.0, QColor("#0A2C3B"))
            r_bg.setColorAt(1.0, QColor("#061424"))
            rim_c1, rim_c2 = QColor(_CYAN_BRIGHT), QColor(_GOLD_PRIMARY)
            grade_col = QColor(_CYAN_BRIGHT)
            aura_col = QColor(10, 200, 185, 52)
        else:
            r_bg.setColorAt(0.0, QColor("#112136"))
            r_bg.setColorAt(1.0, QColor("#071220"))
            rim_c1, rim_c2 = QColor(_GOLD_PRIMARY), QColor(_GOLD_BRONZE)
            grade_col = QColor(_GOLD_PRIMARY)
            aura_col = QColor(200, 170, 110, 30)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(aura_col, 3.0))
        p.drawPath(r_path)
        p.fillPath(r_path, QBrush(r_bg))

        rim_g = QLinearGradient(r_rect.left(), r_rect.top(), r_rect.right(), r_rect.bottom())
        rim_g.setColorAt(0.0, rim_c1)
        rim_g.setColorAt(1.0, rim_c2)
        p.setPen(QPen(QBrush(rim_g), 1.45))
        p.drawPath(r_path)

        # Top metallic highlight bar
        p.setPen(QPen(rim_c1, 1.8))
        p.drawLine(QPointF(r_rect.left() + 10.0, r_rect.top() + 0.6), QPointF(r_rect.right() - 10.0, r_rect.top() + 0.6))

        # Left column inside badge: Grade ("S+", "S", "A+", ...)
        g_font = _latin_font(25 if len(grade) <= 2 else 21, bold=True, letter_spacing=0.6)
        g_rect = QRectF(r_rect.left() + 8.0, r_rect.top() + 8.0, 50.0, 48.0)
        p.setFont(g_font)
        p.setPen(QColor(0, 0, 0, 205))
        p.drawText(g_rect.translated(0.0, 1.5), Qt.AlignmentFlag.AlignCenter, grade)
        p.setPen(grade_col)
        p.drawText(g_rect, Qt.AlignmentFlag.AlignCenter, grade)

        # Right column inside badge: Score + MVP/SVP crest (or RATING tag)
        sc_x = r_rect.left() + 58.0
        sc_w = r_rect.right() - sc_x - 8.0
        sc_font = _sans_font(16, bold=True, letter_spacing=0.3)
        p.setFont(sc_font)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(sc_x, r_rect.top() + 10.0, sc_w, 22.0), Qt.AlignmentFlag.AlignCenter, f"{score:.1f}")

        if mvp_badge:
            pill_r = QRectF(sc_x + 4.0, r_rect.top() + 36.0, sc_w - 8.0, 18.0)
            pill_p = _chamfer_path(pill_r, 3.0)
            is_mvp = mvp_badge == "MVP"
            p_bg = QLinearGradient(pill_r.left(), pill_r.top(), pill_r.right(), pill_r.bottom())
            if is_mvp:
                p_bg.setColorAt(0.0, QColor("#C89B3C"))
                p_bg.setColorAt(1.0, QColor("#785A28"))
                p_fg = QColor("#040E1A")
                p_bd = QColor(_GOLD_CREAM)
            else:
                p_bg.setColorAt(0.0, QColor("#0E4A5C"))
                p_bg.setColorAt(1.0, QColor("#072634"))
                p_fg = QColor(_CYAN_BRIGHT)
                p_bd = QColor(_CYAN_PRIMARY)
            p.fillPath(pill_p, QBrush(p_bg))
            p.setPen(QPen(p_bd, 1.0))
            p.drawPath(pill_p)
            p.setFont(_latin_font(9, bold=True, letter_spacing=1.0))
            p.setPen(p_fg)
            p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, mvp_badge)
        else:
            p.setFont(_latin_font(8, bold=True, letter_spacing=1.0))
            p.setPen(QColor(_TEXT_MUTED))
            p.drawText(QRectF(sc_x, r_rect.top() + 36.0, sc_w, 18.0), Qt.AlignmentFlag.AlignCenter, "RATING")

        right_x = r_rect.left() - 9.0
        right_used_w += rating_w + 9.0

    # KDA metric panel to the left of Rating Badge
    kda_raw = (data.kda or "").strip()
    if kda_raw:
        parsed = _parse_kda(kda_raw)
        num_font = _sans_font(22, bold=True, letter_spacing=0.5)
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

        kda_panel_w = max(148.0, min(206.0, text_w + 28.0))
        kda_rect = QRectF(right_x - kda_panel_w, 54.0, kda_panel_w, 64.0)
        kda_path = _chamfer_path(kda_rect, 6.0)

        k_bg = QLinearGradient(kda_rect.left(), kda_rect.top(), kda_rect.right(), kda_rect.bottom())
        k_bg.setColorAt(0.0, QColor(11, 26, 46, 224))
        k_bg.setColorAt(1.0, QColor(5, 14, 26, 234))
        p.fillPath(kda_path, QBrush(k_bg))
        p.setPen(QPen(QColor(_GOLD_BRONZE), 1.1))
        p.drawPath(kda_path)

        p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.6))
        p.drawLine(
            QPointF(kda_rect.left() + 10.0, kda_rect.top() + 0.5),
            QPointF(kda_rect.right() - 10.0, kda_rect.top() + 0.5),
        )

        num_y = kda_rect.top() + 7.0
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
            elided_kda = nfm.elidedText(kda_raw, Qt.TextElideMode.ElideRight, kda_panel_w - 18.0)
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(kda_rect.left() + 8.0, num_y, kda_panel_w - 16.0, num_h), Qt.AlignmentFlag.AlignCenter, elided_kda)

        ratio_font = _latin_font(10, bold=True, letter_spacing=1.3)
        p.setFont(ratio_font)
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(
            QRectF(kda_rect.left() + 6.0, kda_rect.top() + 40.0, kda_panel_w - 12.0, 18.0),
            Qt.AlignmentFlag.AlignCenter,
            ratio_s,
        )
        right_used_w += kda_panel_w + 12.0

    # Left column: Hero Name + Build Synergy Tag
    info_x = portrait_x + portrait_sz + 14.0
    info_max_w = max(120.0, (lw - 24.0 - right_used_w) - info_x)

    hero_display = (data.hero_name or data.hero_id or "未知英雄").strip()
    name_font = _sans_font(24, bold=True, letter_spacing=0.5)
    name_fm = QFontMetricsF(name_font)
    elided_hero = name_fm.elidedText(hero_display, Qt.TextElideMode.ElideRight, info_max_w)
    name_rect = QRectF(info_x, 54.0, info_max_w, 33.0)

    p.setFont(name_font)
    p.setPen(QColor(0, 0, 0, 210))
    p.drawText(name_rect.translated(0.0, 1.5), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_hero)
    p.setPen(QColor(_GOLD_CREAM))
    p.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_hero)

    # Build Synergy Tag Pill directly under Hero Name
    syn_y = 92.0
    syn_h = 24.0
    combo_txt = (data.combo_title or "").strip()
    if combo_txt:
        tag_prefix = "★ 成型" if data.combo_completed else "◆ 体系"
        syn_font = _sans_font(11, bold=True, letter_spacing=0.3)
        sfm = QFontMetricsF(syn_font)
        prefix_w = sfm.horizontalAdvance(tag_prefix) + 12.0
        max_body_w = max(40.0, info_max_w - prefix_w - 18.0)
        elided_combo = sfm.elidedText(combo_txt, Qt.TextElideMode.ElideRight, max_body_w)
        body_w = sfm.horizontalAdvance(elided_combo)
        pill_w = min(info_max_w, prefix_w + body_w + 18.0)
        syn_rect = QRectF(info_x, syn_y, pill_w, syn_h)
        syn_path = _chamfer_path(syn_rect, 4.5)

        if data.combo_completed:
            bg_g = QLinearGradient(syn_rect.left(), syn_rect.top(), syn_rect.right(), syn_rect.bottom())
            bg_g.setColorAt(0.0, QColor("#2B153D"))
            bg_g.setColorAt(0.55, QColor("#2A1E0D"))
            bg_g.setColorAt(1.0, QColor("#092434"))
            p.fillPath(syn_path, QBrush(bg_g))
            rim_g = QLinearGradient(syn_rect.left(), syn_rect.top(), syn_rect.right(), syn_rect.top())
            rim_g.setColorAt(0.0, QColor("#E879F9"))
            rim_g.setColorAt(0.5, QColor(_GOLD_CREAM))
            rim_g.setColorAt(1.0, QColor(_CYAN_PRIMARY))
            p.setPen(QPen(QBrush(rim_g), 1.3))
            p.drawPath(syn_path)
            pref_col = QColor("#F5D0FE")
            body_col = QColor("#FFF4D4")
        else:
            p.fillPath(syn_path, QColor(12, 26, 44, 220))
            p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.1))
            p.drawPath(syn_path)
            pref_col = QColor(_CYAN_BRIGHT)
            body_col = QColor(_GOLD_CREAM)

        p.setFont(syn_font)
        p.setPen(pref_col)
        p.drawText(QRectF(syn_rect.left() + 8.0, syn_y, prefix_w, syn_h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, tag_prefix)
        p.setPen(body_col)
        p.drawText(
            QRectF(syn_rect.left() + 6.0 + prefix_w, syn_y, max(24.0, syn_rect.width() - prefix_w - 12.0), syn_h),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided_combo,
        )
    else:
        sub_txt = "◈ 海克斯大乱斗 · ARAM MAYHEM"
        sf = _sans_font(11, bold=False, letter_spacing=0.3)
        sfm = QFontMetricsF(sf)
        cw = min(info_max_w, sfm.horizontalAdvance(sub_txt) + 18.0)
        c_rect = QRectF(info_x, syn_y, cw, syn_h)
        c_path = _chamfer_path(c_rect, 4.0)
        p.fillPath(c_path, QColor(9, 22, 38, 210))
        p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0))
        p.drawPath(c_path)
        p.setFont(sf)
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(c_rect.adjusted(8.0, 0.0, -6.0, 0.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, sub_txt)

    # Gold-fade horizontal divider below header
    div_y = 128.0
    div_grad = QLinearGradient(24.0, div_y, lw - 24.0, div_y)
    div_grad.setColorAt(0.0, QColor(120, 90, 40, 0))
    div_grad.setColorAt(0.18, QColor(120, 90, 40, 150))
    div_grad.setColorAt(0.5, QColor(200, 170, 110, 220))
    div_grad.setColorAt(0.82, QColor(120, 90, 40, 150))
    div_grad.setColorAt(1.0, QColor(120, 90, 40, 0))
    p.setPen(QPen(QBrush(div_grad), 1.0))
    p.drawLine(QPointF(24.0, div_y), QPointF(lw - 24.0, div_y))
    _draw_diamond(p, lw * 0.5, div_y, 3.0, QColor(_GOLD_PRIMARY))


def _draw_section_header(
    p: QPainter,
    left: float,
    right: float,
    y: float,
    zh_title: str,
    en_title: str,
    right_tag: str = "",
) -> None:
    zh_f = _sans_font(11, bold=True, letter_spacing=0.6)
    en_f = _latin_font(9, bold=True, letter_spacing=1.2)
    zfm = QFontMetricsF(zh_f)
    zh_w = zfm.horizontalAdvance(zh_title)
    p.setFont(zh_f)
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(left, y, zh_w + 6.0, 15.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, zh_title)

    p.setFont(en_f)
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(QRectF(left + zh_w + 8.0, y + 0.5, 180.0, 14.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, en_title)

    if right_tag:
        p.setFont(en_f)
        p.setPen(QColor(_CYAN_PRIMARY))
        p.drawText(QRectF(right - 160.0, y + 0.5, 160.0, 14.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, right_tag)


def _draw_augments_row(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 5: 4 rarity-coded Augment cards with real local 28x28 augment icons + rarity labels + names."""
    left = 24.0
    right = lw - 24.0
    avail_w = right - left

    augments = list(data.augments[:4])
    icon_paths = tuple(data.augment_icon_paths or ())
    count_tag = f"{len(augments)} / 4 AUGMENTS" if augments else ""
    _draw_section_header(p, left, right, 134.0, "◆ 海克斯强化", "HEXTECH AUGMENTS", count_tag)

    aug_y = 152.0
    aug_h = 54.0

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
    icon_sz = 30.0

    for idx, (aug_name, rarity_raw) in enumerate(augments):
        rk = _normalize_rarity(rarity_raw)
        pal = _RARITY_PALETTE[rk]
        accent = QColor(pal["accent"])
        bright = QColor(pal["bright"])

        px = left + idx * (pill_w + gap)
        rect = QRectF(px, aug_y, pill_w, aug_h)
        path = _chamfer_path(rect, 5.0)

        pg = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        pg.setColorAt(0.0, QColor(pal["bg_top"]))
        pg.setColorAt(1.0, QColor(pal["bg_bot"]))
        p.fillPath(path, QBrush(pg))

        p.save()
        p.setClipPath(path)
        wash = QLinearGradient(rect.left(), rect.top(), rect.left() + 64.0, rect.top())
        wash.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 50))
        wash.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.fillRect(rect, QBrush(wash))
        p.fillRect(QRectF(rect.left(), rect.top(), 3.0, rect.height()), accent)
        p.setPen(QPen(QColor(bright.red(), bright.green(), bright.blue(), 65), 1.0))
        p.drawLine(QPointF(rect.left() + 4.0, rect.top() + 1.0), QPointF(rect.right() - 4.0, rect.top() + 1.0))
        p.restore()

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 195), 1.25))
        p.drawPath(path)

        # Framed 30x30 local augment icon slot (28x28 artwork inside 1px rarity border)
        ico_x = rect.left() + 9.0
        ico_y = rect.top() + (aug_h - icon_sz) * 0.5
        ico_rect = QRectF(ico_x, ico_y, icon_sz, icon_sz)
        ico_path = _chamfer_path(ico_rect, 4.0)
        aug_ico_str = icon_paths[idx] if idx < len(icon_paths) else ""
        aug_img = _load_local_image(aug_ico_str)

        p.save()
        p.setClipPath(ico_path)
        p.fillRect(ico_rect, QColor("#050F1C"))
        if aug_img is not None:
            side = min(aug_img.width(), aug_img.height())
            sx0 = (aug_img.width() - side) // 2
            sy0 = (aug_img.height() - side) // 2
            p.drawImage(ico_rect.adjusted(1.0, 1.0, -1.0, -1.0), aug_img.copy(sx0, sy0, side, side))
        else:
            # Crisp geometric Hextech rarity emblem fallback
            em_g = QRadialGradient(ico_rect.center(), icon_sz * 0.55)
            em_g.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 85))
            em_g.setColorAt(1.0, QColor(6, 16, 28, 220))
            p.fillRect(ico_rect, QBrush(em_g))
            _draw_diamond(p, ico_rect.center().x(), ico_rect.center().y(), 7.5, accent)
            _draw_diamond(p, ico_rect.center().x(), ico_rect.center().y(), 3.6, bright)
        p.restore()

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(accent, 1.25))
        p.drawPath(ico_path)

        # Right side of card: rarity label + slot index on top, augment name below
        tx = ico_rect.right() + 8.0
        text_avail = max(36.0, rect.right() - tx - 7.0)

        top_row_y = rect.top() + 7.0
        p.setFont(_sans_font(10, bold=True, letter_spacing=0.5))
        p.setPen(bright)
        p.drawText(QRectF(tx, top_row_y, text_avail - 20.0, 14.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, pal["label"])

        p.setFont(_latin_font(9, bold=True, letter_spacing=0.9))
        p.setPen(QColor(bright.red(), bright.green(), bright.blue(), 125))
        p.drawText(QRectF(rect.right() - 26.0, top_row_y, 20.0, 14.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"0{idx + 1}")

        clean_name = (aug_name or "未知强化").strip()
        a_font = _sans_font(13, bold=True)
        afm = QFontMetricsF(a_font)
        if afm.horizontalAdvance(clean_name) > text_avail:
            a_font = _sans_font(11, bold=True)
            afm = QFontMetricsF(a_font)
        elided_name = afm.elidedText(clean_name, Qt.TextElideMode.ElideRight, text_avail)
        name_rect = QRectF(tx, rect.top() + 24.0, text_avail, 22.0)
        p.setFont(a_font)
        p.setPen(QColor(0, 0, 0, 190))
        p.drawText(name_rect.translated(0.0, 1.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_name)


def _draw_items_row(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 6: 6 spacious 48x48 beveled item icons + full unobstructed Chinese names underneath (ZERO clipping!)."""
    left = 24.0
    right = lw - 24.0
    avail_w = right - left

    items = list(data.item_entries[:6])
    right_tag = "6 / 6 FULL BUILD" if len(items) >= 6 else (f"{len(items)} / 6 ITEMS" if items else "")
    _draw_section_header(p, left, right, 214.0, "◆ 终局出装", "FINAL BUILD", right_tag)

    icon_y = 232.0
    icon_sz = 48.0
    cap_y = icon_y + icon_sz + 4.0  # 284.0
    cap_h = 22.0  # 284..306 -> 11px clear gap before bottom dashboard at y=317!

    if not items:
        empty_r = QRectF(left, icon_y, avail_w, 64.0)
        empty_p = _chamfer_path(empty_r, 5.0)
        p.fillPath(empty_p, QColor(8, 18, 32, 185))
        p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0, Qt.PenStyle.DashLine))
        p.drawPath(empty_p)
        p.setFont(_sans_font(12, bold=False))
        p.setPen(QColor(_TEXT_FAINT))
        p.drawText(empty_r, Qt.AlignmentFlag.AlignCenter, "暂未采集到终局装备数据")
        return

    max_slots = 6
    gap = 10.0
    slot_w = (avail_w - gap * (max_slots - 1)) / max_slots  # ~110.3px per slot!

    for idx in range(max_slots):
        sx = left + idx * (slot_w + gap)
        ix = sx + (slot_w - icon_sz) * 0.5
        icon_rect = QRectF(ix, icon_y, icon_sz, icon_sz)
        icon_path_shape = _chamfer_path(icon_rect, 4.0)

        if idx >= len(items):
            # Subtle empty Hextech inventory socket
            p.fillPath(icon_path_shape, QColor(6, 16, 28, 130))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(70, 55, 20, 135), 1.0, Qt.PenStyle.DashLine))
            p.drawPath(icon_path_shape)
            continue

        item_name, icon_path = items[idx]
        clean_name = (item_name or f"装备{idx + 1}").strip()
        img = _load_local_image(icon_path)

        # Cast depth shadow
        p.save()
        p.translate(1.5, 2.2)
        p.fillPath(icon_path_shape, QColor(0, 0, 0, 155))
        p.restore()

        p.save()
        p.setClipPath(icon_path_shape)
        if img is not None:
            p.fillRect(icon_rect, QColor("#040E1A"))
            side = min(img.width(), img.height())
            cx0 = (img.width() - side) // 2
            cy0 = (img.height() - side) // 2
            p.drawImage(icon_rect, img.copy(cx0, cy0, side, side))
        else:
            tile_g = QLinearGradient(icon_rect.left(), icon_rect.top(), icon_rect.left(), icon_rect.bottom())
            tile_g.setColorAt(0.0, QColor("#152438"))
            tile_g.setColorAt(1.0, QColor("#091322"))
            p.fillRect(icon_rect, QBrush(tile_g))
            p.setPen(QPen(QColor(200, 170, 110, 65), 1.0))
            p.drawLine(QPointF(icon_rect.left() + 4.0, icon_rect.top() + 2.0), QPointF(icon_rect.right() - 4.0, icon_rect.top() + 2.0))
            mono = clean_name[:2]
            p.setFont(_sans_font(13, bold=True))
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(icon_rect, Qt.AlignmentFlag.AlignCenter, mono)
        p.restore()

        # Metallic gold beveled border
        g_rim = QLinearGradient(icon_rect.left(), icon_rect.top(), icon_rect.right(), icon_rect.bottom())
        g_rim.setColorAt(0.0, QColor(_GOLD_CREAM))
        g_rim.setColorAt(0.45, QColor(_GOLD_PRIMARY))
        g_rim.setColorAt(1.0, QColor(_GOLD_BRONZE))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QBrush(g_rim), 1.55))
        p.drawPath(icon_path_shape)

        # Full unobstructed Chinese item name directly below icon (zero overlap!)
        cap_font = _sans_font(11, bold=False)
        cfm = QFontMetricsF(cap_font)
        if cfm.horizontalAdvance(clean_name) > slot_w - 2.0:
            cap_font = _sans_font(10, bold=False)
            cfm = QFontMetricsF(cap_font)
        elided_cap = cfm.elidedText(clean_name, Qt.TextElideMode.ElideRight, slot_w - 2.0)
        cap_rect = QRectF(sx, cap_y, slot_w, cap_h)
        p.setFont(cap_font)
        p.setPen(QColor(0, 0, 0, 175))
        p.drawText(cap_rect.translated(0.0, 1.0), Qt.AlignmentFlag.AlignCenter, elided_cap)
        p.setPen(QColor("#E2D9C5"))
        p.drawText(cap_rect, Qt.AlignmentFlag.AlignCenter, elided_cap)


def _draw_telemetry_dashboard(p: QPainter, lw: float, data: MatchRecapData) -> None:
    """Layer 7: 2-column Hextech Final Telemetry Dashboard (replaces legacy AI Highlight box!).

    - Left panel 「终局面板 · FINAL STATS」: 2-row metallic stat chips with Mayhem gold highlight.
    - Right panel 「硬核战绩 · COMBAT DATA」: Champion damage + share bar + tank/heal/KP/gold cells.
    """
    top_y = 317.0
    panel_h = 118.0
    left_x = 24.0
    gap = 12.0
    col_w = (lw - 48.0 - gap) * 0.5  # 350.0px each

    left_box = QRectF(left_x, top_y, col_w, panel_h)
    right_box = QRectF(left_x + col_w + gap, top_y, col_w, panel_h)

    # --- LEFT PANEL: 终局面板 · FINAL STATS ---
    l_path = _chamfer_path(left_box, 5.5)
    l_bg = QLinearGradient(left_box.left(), left_box.top(), left_box.right(), left_box.bottom())
    l_bg.setColorAt(0.0, QColor(_PANEL_DARK_LEFT))
    l_bg.setColorAt(1.0, QColor("#040E1A"))
    p.fillPath(l_path, QBrush(l_bg))

    p.save()
    p.setClipPath(l_path)
    l_wash = QLinearGradient(left_box.left(), left_box.top(), left_box.left() + 120.0, left_box.top())
    l_wash.setColorAt(0.0, QColor(200, 170, 110, 32))
    l_wash.setColorAt(1.0, QColor(200, 170, 110, 0))
    p.fillRect(left_box, QBrush(l_wash))
    p.fillRect(QRectF(left_box.left(), left_box.top(), 3.0, left_box.height()), QColor(_GOLD_PRIMARY))
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(120, 90, 40, 175), 1.1))
    p.drawPath(l_path)

    p.setFont(_sans_font(11, bold=True, letter_spacing=0.6))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(left_box.left() + 12.0, left_box.top() + 6.0, 140.0, 18.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "◆ 终局面板")
    p.setFont(_latin_font(9, bold=True, letter_spacing=1.2))
    p.setPen(QColor(_GOLD_BRONZE))
    p.drawText(QRectF(left_box.right() - 130.0, left_box.top() + 7.0, 118.0, 16.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "FINAL STATS")

    norm_stats = normalize_final_stats(data.final_stats)
    # Build visible chips from available stats (show up to 8 in 2x4 grid)
    stat_chips: list[tuple[str, str, bool]] = []
    for key, zh_lbl, en_lbl, mayhem_th in _STAT_DISPLAY_SPECS:
        if key in norm_stats and norm_stats[key] > 0.0:
            val = norm_stats[key]
            stat_chips.append((f"{zh_lbl} {en_lbl}", _format_stat_value(key, val), val >= mayhem_th))

    grid_x = left_box.left() + 11.0
    grid_y = left_box.top() + 28.0
    grid_w = left_box.width() - 20.0
    grid_h = left_box.height() - 36.0

    if stat_chips:
        # 2 rows x 4 columns (or 2x3 if <= 6 stats)
        cols = 4 if len(stat_chips) > 6 else max(2, min(4, (len(stat_chips) + 1) // 2))
        rows = 2 if len(stat_chips) > cols else 1
        cg, rg = 6.0, 6.0
        cw = (grid_w - cg * (cols - 1)) / cols
        ch = (grid_h - rg * (rows - 1)) / rows
        for i, (lbl_txt, val_txt, is_mayhem) in enumerate(stat_chips[: cols * rows]):
            r_i, c_i = divmod(i, cols)
            cr = QRectF(grid_x + c_i * (cw + cg), grid_y + r_i * (ch + rg), cw, ch)
            cp = _chamfer_path(cr, 3.5)
            if is_mayhem:
                cg_fill = QLinearGradient(cr.left(), cr.top(), cr.right(), cr.bottom())
                cg_fill.setColorAt(0.0, QColor(72, 52, 18, 225))
                cg_fill.setColorAt(1.0, QColor(30, 22, 9, 230))
                p.fillPath(cp, QBrush(cg_fill))
                p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.2))
                p.drawPath(cp)
                lbl_c = QColor(_GOLD_PRIMARY)
                val_c = QColor(_GOLD_CREAM)
                disp_lbl = f"✦{lbl_txt}"
            else:
                p.fillPath(cp, QColor(9, 22, 38, 215))
                p.setPen(QPen(QColor(70, 55, 20, 165), 0.95))
                p.drawPath(cp)
                lbl_c = QColor(_TEXT_MUTED)
                val_c = QColor("#E2E8F0")
                disp_lbl = lbl_txt

            p.setFont(_sans_font(9, bold=is_mayhem))
            p.setPen(lbl_c)
            p.drawText(QRectF(cr.left() + 3.0, cr.top() + 3.0, cr.width() - 6.0, 13.0), Qt.AlignmentFlag.AlignCenter, disp_lbl)

            v_font = _sans_font(14 if rows == 2 else 16, bold=True)
            p.setFont(v_font)
            p.setPen(val_c)
            p.drawText(QRectF(cr.left() + 3.0, cr.top() + 16.0, cr.width() - 6.0, cr.height() - 18.0), Qt.AlignmentFlag.AlignCenter, val_txt)
    else:
        # Graceful fallback summary chips when LiveClient final_stats is unavailable
        prism_c = sum(1 for _, r in data.augments[:4] if _normalize_rarity(r) == "prismatic")
        gold_c = sum(1 for _, r in data.augments[:4] if _normalize_rarity(r) == "gold")
        silv_c = sum(1 for _, r in data.augments[:4] if _normalize_rarity(r) == "silver")
        aug_desc = " · ".join(
            part
            for part in (
                f"{prism_c}棱彩" if prism_c else "",
                f"{gold_c}黄金" if gold_c else "",
                f"{silv_c}白银" if silv_c else "",
            )
            if part
        ) or "暂无强化"
        parsed_kda = _parse_kda(data.kda)
        kda_ratio_s = parsed_kda[3] if parsed_kda else (data.kda or "--")
        item_cnt = len(data.item_entries[:6])
        build_desc = f"{item_cnt}/6 满神装" if item_cnt >= 6 else (f"{item_cnt} 件出装" if item_cnt else "待采集")
        mode_desc = "首棱彩局" if data.is_first_prismatic else ("体系已成型" if data.combo_completed else "海克斯大乱斗")
        fallback_chips = [
            ("KDA 效率", kda_ratio_s, bool(parsed_kda and (parsed_kda[3] == "PERFECT" or float(parsed_kda[3].split()[0]) >= 5.0))),
            ("海克斯品质", aug_desc, prism_c >= 2),
            ("终局出装", build_desc, item_cnt >= 6),
            ("对局特征", mode_desc, data.combo_completed or data.is_first_prismatic),
        ]
        cg, rg = 7.0, 6.0
        cw = (grid_w - cg) * 0.5
        ch = (grid_h - rg) * 0.5
        for i, (lbl_txt, val_txt, hi) in enumerate(fallback_chips):
            r_i, c_i = divmod(i, 2)
            cr = QRectF(grid_x + c_i * (cw + cg), grid_y + r_i * (ch + rg), cw, ch)
            cp = _chamfer_path(cr, 3.5)
            p.fillPath(cp, QColor(42, 32, 14, 190) if hi else QColor(9, 22, 38, 215))
            p.setPen(QPen(QColor(_GOLD_PRIMARY if hi else _GOLD_HAIRLINE), 1.0))
            p.drawPath(cp)
            p.setFont(_sans_font(9, bold=False))
            p.setPen(QColor(_GOLD_PRIMARY if hi else _TEXT_MUTED))
            p.drawText(QRectF(cr.left() + 8.0, cr.top() + 3.0, cr.width() - 16.0, 13.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, lbl_txt)
            vf = _sans_font(12, bold=True)
            vfm = QFontMetricsF(vf)
            elided_v = vfm.elidedText(val_txt, Qt.TextElideMode.ElideRight, cr.width() - 16.0)
            p.setFont(vf)
            p.setPen(QColor(_GOLD_CREAM if hi else "#E2E8F0"))
            p.drawText(QRectF(cr.left() + 8.0, cr.top() + 17.0, cr.width() - 16.0, 18.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_v)

    # --- RIGHT PANEL: 硬核战绩 · COMBAT DATA ---
    r_path = _chamfer_path(right_box, 5.5)
    r_bg = QLinearGradient(right_box.left(), right_box.top(), right_box.right(), right_box.bottom())
    r_bg.setColorAt(0.0, QColor(_PANEL_DARK_RIGHT))
    r_bg.setColorAt(1.0, QColor("#04101C"))
    p.fillPath(r_path, QBrush(r_bg))

    p.save()
    p.setClipPath(r_path)
    r_wash = QLinearGradient(right_box.left(), right_box.top(), right_box.left() + 145.0, right_box.top())
    r_wash.setColorAt(0.0, QColor(10, 200, 185, 42))
    r_wash.setColorAt(1.0, QColor(10, 200, 185, 0))
    p.fillRect(right_box, QBrush(r_wash))
    p.fillRect(QRectF(right_box.left(), right_box.top(), 3.0, right_box.height()), QColor(_CYAN_PRIMARY))
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(3, 151, 171, 175), 1.1))
    p.drawPath(r_path)

    p.setFont(_sans_font(11, bold=True, letter_spacing=0.6))
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(right_box.left() + 12.0, right_box.top() + 6.0, 140.0, 18.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "◈ 硬核战绩")
    p.setFont(_latin_font(9, bold=True, letter_spacing=1.2))
    p.setPen(QColor(3, 151, 171, 200))
    p.drawText(QRectF(right_box.right() - 130.0, right_box.top() + 7.0, 118.0, 16.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "COMBAT DATA")

    # Compute normalized combat metrics
    raw_share = float(data.damage_share or 0.0)
    dmg_share = min(1.0, raw_share / 100.0 if raw_share > 1.0 else max(0.0, raw_share))
    kp_val = _derive_kill_participation(data)
    has_dmg = int(data.damage_dealt or 0) > 0 or dmg_share > 0.0

    bar_x = right_box.left() + 12.0
    bar_w = right_box.width() - 22.0
    row1_y = right_box.top() + 28.0

    if has_dmg:
        dmg_txt = _format_cn_metric(data.damage_dealt)
        share_pct = int(round(dmg_share * 100.0))
        is_carry_dmg = share_pct >= 30
        p.setFont(_sans_font(10, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(bar_x, row1_y, 70.0, 18.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "对英雄输出")

        p.setFont(_sans_font(15, bold=True))
        p.setPen(QColor(_GOLD_CREAM if is_carry_dmg else _CYAN_BRIGHT))
        p.drawText(QRectF(bar_x + 68.0, row1_y - 1.0, 100.0, 20.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, dmg_txt)

        if share_pct > 0:
            p.setFont(_sans_font(11, bold=True))
            p.setPen(QColor(_GOLD_PRIMARY if is_carry_dmg else _CYAN_BRIGHT))
            p.drawText(
                QRectF(bar_x + bar_w - 110.0, row1_y, 110.0, 18.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                f"全队占比 {share_pct}%",
            )
        fill_ratio = max(0.08, min(1.0, dmg_share * 2.2)) if dmg_share > 0.0 else 0.55
        bar_c1 = QColor(_GOLD_METAL if is_carry_dmg else _CYAN_MID)
        bar_c2 = QColor(_GOLD_CREAM if is_carry_dmg else _CYAN_BRIGHT)
    else:
        # Fallback hero bar when EOG damage telemetry is not provided: show KP% or KDA participation bar
        kp_pct = int(round(kp_val * 100.0))
        p.setFont(_sans_font(10, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(bar_x, row1_y, 82.0, 18.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "团战参与率")

        p.setFont(_sans_font(15, bold=True))
        p.setPen(QColor(_CYAN_BRIGHT))
        p.drawText(
            QRectF(bar_x + 74.0, row1_y - 1.0, 110.0, 20.0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            f"{kp_pct}%" if kp_pct > 0 else "同步完成",
        )
        score_v, grade_v, _ = _resolve_rating(data)
        if score_v > 0.0:
            p.setFont(_sans_font(10, bold=True))
            p.setPen(QColor(_GOLD_PRIMARY))
            p.drawText(
                QRectF(bar_x + bar_w - 110.0, row1_y, 110.0, 18.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                f"综合战力 {grade_v} · {score_v:.1f}",
            )
        fill_ratio = max(0.18, min(1.0, kp_val if kp_val > 0.0 else (score_v / 16.0 if score_v > 0.0 else 0.45)))
        bar_c1, bar_c2 = QColor(_CYAN_MID), QColor(_CYAN_BRIGHT)

    # Progress bar track
    track_r = QRectF(bar_x, row1_y + 22.0, bar_w, 7.0)
    track_p = _chamfer_path(track_r, 2.5)
    p.fillPath(track_p, QColor(4, 14, 24, 235))
    p.setPen(QPen(QColor(3, 151, 171, 115), 0.8))
    p.drawPath(track_p)

    fill_w = max(8.0, bar_w * fill_ratio)
    fill_r = QRectF(bar_x + 1.0, row1_y + 23.0, fill_w - 2.0, 5.0)
    fill_g = QLinearGradient(fill_r.left(), fill_r.top(), fill_r.right(), fill_r.top())
    fill_g.setColorAt(0.0, bar_c1)
    fill_g.setColorAt(1.0, bar_c2)
    p.fillPath(_chamfer_path(fill_r, 2.0), QBrush(fill_g))

    # Row 2: 4 compact telemetry cells
    cell_y = row1_y + 37.0
    cell_h = right_box.bottom() - cell_y - 9.0
    cell_gap = 6.0
    cell_w = (bar_w - cell_gap * 3.0) / 4.0

    kp_disp = f"{int(round(kp_val * 100.0))}%" if kp_val > 0.0 else "--"
    has_any_eog = any(
        int(v or 0) > 0 for v in (data.damage_taken, data.heal_and_shield, data.gold_earned, data.damage_dealt)
    )
    if has_any_eog:
        cells = (
            ("承受伤害", _format_cn_metric(data.damage_taken), QColor("#E2E8F0")),
            ("治疗护盾", _format_cn_metric(data.heal_and_shield), QColor(_CYAN_BRIGHT)),
            ("参团率 KP", kp_disp, QColor(_GOLD_CREAM if kp_val >= 0.70 else "#E2E8F0")),
            ("全场经济", _format_gold_k(data.gold_earned), QColor(_GOLD_PRIMARY)),
        )
    else:
        parsed_k = _parse_kda(data.kda)
        dur_s = _format_duration(data.duration_s) or "--"
        cells = (
            ("击杀", parsed_k[0] if parsed_k else "--", QColor(_GOLD_CREAM)),
            ("助攻", parsed_k[2] if parsed_k else "--", QColor(_CYAN_BRIGHT)),
            ("参团率 KP", kp_disp, QColor(_GOLD_PRIMARY)),
            ("对局时长", dur_s, QColor("#E2E8F0")),
        )

    for idx, (c_lbl, c_val, c_col) in enumerate(cells):
        cx = bar_x + idx * (cell_w + cell_gap)
        cr = QRectF(cx, cell_y, cell_w, cell_h)
        cp = _chamfer_path(cr, 3.5)
        p.fillPath(cp, QColor(7, 22, 36, 215))
        p.setPen(QPen(QColor(3, 151, 171, 110), 0.95))
        p.drawPath(cp)

        p.setFont(_sans_font(9, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(cr.left() + 2.0, cr.top() + 4.0, cr.width() - 4.0, 14.0), Qt.AlignmentFlag.AlignCenter, c_lbl)

        p.setFont(_sans_font(13, bold=True))
        p.setPen(c_col)
        p.drawText(QRectF(cr.left() + 2.0, cr.top() + 19.0, cr.width() - 4.0, cr.height() - 21.0), Qt.AlignmentFlag.AlignCenter, c_val)


def _draw_footer_watermark(p: QPainter, lw: float, lh: float, data: MatchRecapData) -> None:
    """Layer 8: Clean single-line HexHelper emblem + patch watermark."""
    sep_y = lh - 35.0
    p.setPen(QPen(QColor(120, 90, 40, 92), 1.0))
    p.drawLine(QPointF(26.0, sep_y), QPointF(lw - 26.0, sep_y))

    patch = (data.dataset_patch or "26.23").strip()
    prefix = "HEXHELPER"
    suffix = f" · 海克斯大乱斗 · {patch} 数据引擎"

    pre_font = _latin_font(10, bold=True, letter_spacing=1.5)
    suf_font = _sans_font(10, bold=False, letter_spacing=0.4)
    pfm = QFontMetricsF(pre_font)
    sfm = QFontMetricsF(suf_font)

    icon_sz = 14.0
    icon_gap = 6.0
    pre_w = pfm.horizontalAdvance(prefix)
    suf_w = sfm.horizontalAdvance(suffix)
    total_w = icon_sz + icon_gap + pre_w + suf_w

    fy = sep_y + 3.0
    fh = 21.0
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
    """Render an offline LOL V16.23 Hextech v2 match recap card purely with Qt QPainter."""
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
    _draw_augments_row(p, lw, data)
    _draw_items_row(p, lw, data)
    _draw_telemetry_dashboard(p, lw, data)
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
        on_recap_action: Callable[[str, bool], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.data = data
        self._card_width = card_width
        self._card_height = card_height
        self._card_image: QImage = render_match_recap_image(data, width=card_width, height=card_height)
        self._on_core_action = on_core_action
        self._on_recap_action = on_recap_action
        self._close_action_emitted: bool = False
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
                nb = HextechActionButton(
                    nudge_btn_txt, variant="cyan" if nudge_action == "register" else "gold", height=26, parent=bar
                )
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

    def update_data(self, data: MatchRecapData) -> None:
        """Re-render the recap card in-place (e.g. once missing item/hero/augment icons finish downloading)."""
        self.data = data
        self._card_image = render_match_recap_image(
            data,
            width=self._card_width,
            height=self._card_height,
        )
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

    def _emit_recap_action(self, action: str, ok: bool) -> None:
        if self._on_recap_action is not None:
            self._on_recap_action(str(action), bool(ok))

    def _emit_close_once(self) -> None:
        if self._close_action_emitted:
            return
        self._close_action_emitted = True
        self._emit_recap_action("close", True)

    def copy_to_clipboard(self) -> QImage:
        cb = QGuiApplication.clipboard()
        ok = False
        if cb is not None and not self._card_image.isNull():
            cb.setImage(self._card_image)
            ok = True
        self.copy_btn.setText(_COPY_BTN_COPIED_TEXT)
        self._copy_reset_timer.start(2600)
        self._emit_recap_action("copy", ok)
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
            self._emit_recap_action("save", False)
            return ""
        if self.save_to_path(chosen):
            self.save_btn.setText(_SAVE_BTN_SAVED_TEXT)
            self._save_reset_timer.start(2200)
            self._emit_recap_action("save", True)
            return chosen
        self._emit_recap_action("save", False)
        return ""

    def accept(self) -> None:
        self._emit_close_once()
        super().accept()

    def reject(self) -> None:
        self._emit_close_once()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._emit_close_once()
        super().closeEvent(event)
