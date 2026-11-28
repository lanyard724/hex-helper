"""Offline LOL V16.23 Hextech multi-template match recap card renderer & 1-click clipboard share dialog."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
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
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from hex_helper.config import DEFAULT_RECAP_TEMPLATE as _CFG_DEFAULT_RECAP_TEMPLATE
from hex_helper.ui.hextech_widgets import HextechActionButton
from hex_helper.ui.theme import (
    ASSETS_DIR,
    BASE_OVERLAY_QSS,
    init_hextech_fonts,
    load_app_icon,
)

# Canonical logical default dimensions (preserved for backward compatibility with default template)
CARD_DEFAULT_WIDTH = 760
CARD_DEFAULT_HEIGHT = 480

# Authentic LOL V16.23 Hextech Palette (strictly desktop Hextech; ZERO Web Tactile Orange)
_BG_DARK_START = "#040B14"
_BG_DARK_MID = "#081422"
_BG_DARK_END = "#030A13"
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
_TEXT_MUTED = "#94A3B8"
_TEXT_FAINT = "#64748B"

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
class RecapTemplateSpec:
    """Metadata and native logical geometry for one selectable match recap card layout."""

    id: str
    short_label: str
    display_name: str
    subtitle: str
    logical_width: int
    logical_height: int


RECAP_TEMPLATE_ORDER: tuple[str, ...] = (
    "endfield_split",
    "cinematic_banner",
    "diagonal_hologram",
    "editorial_glass",
)

RECAP_TEMPLATES: dict[str, RecapTemplateSpec] = {
    "endfield_split": RecapTemplateSpec(
        id="endfield_split",
        short_label="01 干员档案",
        display_name="干员档案双栏",
        subtitle="左 40% 全高立绘无遮挡 · 右 60% 秩序数据网格",
        logical_width=820,
        logical_height=480,
    ),
    "cinematic_banner": RecapTemplateSpec(
        id="cinematic_banner",
        short_label="02 宽幕海报",
        display_name="宽银幕头图海报",
        subtitle="上 46% 宽画幅原画头图 · 下 54% 冷静战报台",
        logical_width=780,
        logical_height=500,
    ),
    "diagonal_hologram": RecapTemplateSpec(
        id="diagonal_hologram",
        short_label="03 斜切全息",
        display_name="14° 斜切全息界面",
        subtitle="右半斜切大画幅皮肤舞台 · 底部通栏出装坞",
        logical_width=820,
        logical_height=480,
    ),
    "editorial_glass": RecapTemplateSpec(
        id="editorial_glass",
        short_label="04 通透玻璃",
        display_name="极简黑金通透玻璃",
        subtitle="760×480 全幅柔化皮肤虚影 · 熏黑玻璃信息条",
        logical_width=760,
        logical_height=480,
    ),
}

DEFAULT_RECAP_TEMPLATE: str = _CFG_DEFAULT_RECAP_TEMPLATE

_TEMPLATE_ALIASES: dict[str, str] = {
    "endfield_split": "endfield_split",
    "variant_a": "endfield_split",
    "a": "endfield_split",
    "01": "endfield_split",
    "1": "endfield_split",
    "cinematic_banner": "cinematic_banner",
    "variant_b": "cinematic_banner",
    "b": "cinematic_banner",
    "02": "cinematic_banner",
    "2": "cinematic_banner",
    "diagonal_hologram": "diagonal_hologram",
    "variant_c": "diagonal_hologram",
    "c": "diagonal_hologram",
    "03": "diagonal_hologram",
    "3": "diagonal_hologram",
    "editorial_glass": "editorial_glass",
    "variant_d": "editorial_glass",
    "d": "editorial_glass",
    "04": "editorial_glass",
    "4": "editorial_glass",
}


def normalize_recap_template(template: str | None) -> str:
    """Normalize a configured or user-supplied template key to a known template ID."""
    raw = (template or "").strip().lower()
    if raw in RECAP_TEMPLATES:
        return raw
    return _TEMPLATE_ALIASES.get(raw, DEFAULT_RECAP_TEMPLATE)


def get_recap_template_spec(template: str | None) -> RecapTemplateSpec:
    """Return the validated RecapTemplateSpec for a template key."""
    return RECAP_TEMPLATES[normalize_recap_template(template)]


@dataclass(frozen=True)
class MatchRecapData:
    """Immutable snapshot of a completed ARAM Mayhem match for v2 multi-template rendering."""

    hero_id: str
    hero_name: str
    hero_icon_path: str = ""
    won: int = -1  # 1 = 胜利, 0 = 惜败, -1 = 激战局
    kda: str = ""
    duration_s: int = 0
    team_kills: str = ""
    is_first_prismatic: bool = False
    augments: tuple[tuple[str, str], ...] = ()  # (name, rarity) up to 6
    combo_title: str = ""
    combo_completed: bool = False
    item_entries: tuple[tuple[str, str], ...] = ()  # (item_name, icon_path) up to 6
    item_ids: tuple[str, ...] = ()  # parallel raw item IDs for async icon hydration
    ai_highlight: str = ""  # Retained strictly for backward compatibility with legacy callers
    dataset_patch: str = "26.23"
    # v2 telemetry & visual fields
    augment_icon_paths: tuple[str, ...] = ()  # parallel local PNG paths for up to 6 augments
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
    skin_id: int = 0
    skin_name: str = ""
    skin_splash_path: str = ""
    skin_loading_path: str = ""
    skin_center_path: str = ""
    hero_alias: str = ""


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
    """Compute a transparent 0.0-16.0 ARAM Mayhem combat rating."""
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


def _mono_font(pixel_size: int, bold: bool = False, letter_spacing: float = 0.8) -> QFont:
    _ensure_card_fonts()
    f = QFont()
    f.setFamilies(["Consolas", "Segoe UI", "Microsoft YaHei UI"])
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


# ---------------------------------------------------------------------------
# Shared Data Resolvers & Endfield/Gacha Rendering Primitives
# ---------------------------------------------------------------------------

def _match_status_tokens(data: MatchRecapData) -> tuple[str, str, str, QColor, QColor]:
    """Return (en_upper, zh_word, combined_badge_txt, accent_col, text_col)."""
    if data.won == 1:
        return ("VICTORY", "胜利", "✦ 胜利 · VICTORY", QColor(_GOLD_PRIMARY), QColor(_GOLD_CREAM))
    if data.won == 0:
        return ("DEFEAT", "惜败", "◆ 惜败 · DEFEAT", QColor(_DEFEAT_CRIMSON), QColor(_DEFEAT_CRIMSON_SOFT))
    return ("ARAM", "激战局", "◈ 激战局 · ARAM", QColor(_CYAN_PRIMARY), QColor(_CYAN_BRIGHT))


def _visible_skin_name(data: MatchRecapData) -> str:
    """Return displayable skin name, suppressing 'default' or base skin equal to hero name."""
    s = (data.skin_name or "").strip()
    if not s or s.lower() == "default" or s == (data.hero_name or "").strip():
        return ""
    return s


def _hero_display_name(data: MatchRecapData) -> str:
    return (data.hero_name or data.hero_id or "未知英雄").strip()


def _hero_code_and_watermark(data: MatchRecapData) -> tuple[str, str]:
    hid_raw = (data.hero_id or "").strip()
    if hid_raw.isdigit():
        hero_code = hid_raw.zfill(3)
    else:
        hero_code = (hid_raw.upper() or "HEX")[:6]
    skin_part = str(int(data.skin_id)) if int(data.skin_id or 0) > 0 else "BASE"
    file_tag = f"FILE // {hero_code}-{skin_part}"

    alias_clean = (data.hero_alias or "").strip().upper()
    if not alias_clean and hid_raw.isascii() and hid_raw and not hid_raw.isdigit():
        alias_clean = hid_raw.upper()
    watermark = alias_clean or "ARAM MAYHEM"
    return file_tag, watermark


def _meta_parts(data: MatchRecapData, *, include_patch: bool = True) -> list[str]:
    parts: list[str] = []
    tk = (data.team_kills or "").strip()
    if tk:
        parts.append(f"人头 {tk}")
    dur = _format_duration(data.duration_s)
    if dur:
        parts.append(dur)
    if include_patch:
        patch = (data.dataset_patch or "26.23").strip()
        parts.append(f"PATCH {patch}")
    return parts


def _combo_line(data: MatchRecapData) -> tuple[str, QColor]:
    combo = (data.combo_title or "").strip()
    if not combo:
        return "", QColor(_GOLD_PRIMARY)
    prefix = "★ 成型  " if data.combo_completed else "◆ 体系  "
    col = QColor(_CYAN_BRIGHT if data.combo_completed else _GOLD_CREAM)
    return f"{prefix}{combo}", col


def _has_prismatic_presence(data: MatchRecapData) -> bool:
    return bool(
        data.is_first_prismatic
        or data.combo_completed
        or any(_normalize_rarity(r) == "prismatic" for _, r in data.augments[:6])
    )


@dataclass(frozen=True)
class _StatCellSpec:
    full_label: str
    short_label: str
    value: str
    highlighted: bool


@dataclass(frozen=True)
class _CombatSummarySpec:
    has_dmg: bool
    headline_label: str
    headline_val: str
    right_tag: str
    fill_ratio: float
    is_carry: bool
    cells: tuple[tuple[str, str, QColor], ...]


def _prepare_stat_items(data: MatchRecapData) -> tuple[bool, list[_StatCellSpec]]:
    norm = normalize_final_stats(data.final_stats)
    out: list[_StatCellSpec] = []
    for key, zh_lbl, en_lbl, mayhem_th in _STAT_DISPLAY_SPECS:
        if key in norm and norm[key] > 0.0:
            val = norm[key]
            out.append(
                _StatCellSpec(
                    full_label=f"{zh_lbl} {en_lbl}",
                    short_label=en_lbl,
                    value=_format_stat_value(key, val),
                    highlighted=val >= mayhem_th,
                )
            )
    if out:
        return True, out[:8]

    # Graceful 4-cell fallback when LiveClient final_stats is absent
    prism_c = sum(1 for _, r in data.augments[:6] if _normalize_rarity(r) == "prismatic")
    gold_c = sum(1 for _, r in data.augments[:6] if _normalize_rarity(r) == "gold")
    silv_c = sum(1 for _, r in data.augments[:6] if _normalize_rarity(r) == "silver")
    aug_desc = "·".join(
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
    mode_desc = "首棱彩局" if data.is_first_prismatic else ("体系成型" if data.combo_completed else "海克斯大乱斗")
    kda_hi = bool(parsed_kda and (parsed_kda[3] == "PERFECT" or float(parsed_kda[3].split()[0]) >= 5.0))
    fallback = [
        _StatCellSpec("KDA 效率", "KDA", kda_ratio_s, kda_hi),
        _StatCellSpec("海克斯品质", "品质", aug_desc, prism_c >= 2),
        _StatCellSpec("终局出装", "出装", build_desc, item_cnt >= 6),
        _StatCellSpec("对局特征", "模式", mode_desc, data.combo_completed or data.is_first_prismatic),
    ]
    return False, fallback


def _prepare_combat_summary(data: MatchRecapData) -> _CombatSummarySpec:
    raw_share = float(data.damage_share or 0.0)
    dmg_share = min(1.0, raw_share / 100.0 if raw_share > 1.0 else max(0.0, raw_share))
    kp_val = _derive_kill_participation(data)
    has_dmg = int(data.damage_dealt or 0) > 0 or dmg_share > 0.0

    if has_dmg:
        dmg_txt = _format_cn_metric(data.damage_dealt)
        share_pct = int(round(dmg_share * 100.0))
        is_carry = share_pct >= 30
        right_tag = f"占比 {share_pct}%" if share_pct > 0 else "已统计"
        fill_ratio = max(0.08, min(1.0, dmg_share * 2.2)) if dmg_share > 0.0 else 0.55
        hl_label, hl_val = "对英雄输出", dmg_txt
    else:
        kp_pct = int(round(kp_val * 100.0))
        is_carry = False
        hl_label = "团战参与率"
        hl_val = f"{kp_pct}%" if kp_pct > 0 else "同步完成"
        score_v, grade_v, _ = _resolve_rating(data)
        right_tag = f"战力 {grade_v} · {score_v:.1f}" if score_v > 0.0 else "基础战绩"
        fill_ratio = max(0.18, min(1.0, kp_val if kp_val > 0.0 else (score_v / 16.0 if score_v > 0.0 else 0.45)))

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
    return _CombatSummarySpec(
        has_dmg=has_dmg,
        headline_label=hl_label,
        headline_val=hl_val,
        right_tag=right_tag,
        fill_ratio=fill_ratio,
        is_carry=is_carry,
        cells=cells,
    )


def _cover_crop(
    src: QImage,
    target_w: float,
    target_h: float,
    *,
    focal_x: float = 0.5,
    focal_y: float = 0.25,
    oversample: float = 2.0,
) -> QImage:
    sw, sh = src.width(), src.height()
    if sw <= 0 or sh <= 0 or target_w <= 0 or target_h <= 0:
        return src
    target_ar = target_w / target_h
    src_ar = sw / float(sh)
    if src_ar > target_ar:
        crop_h = sh
        crop_w = max(1, int(round(sh * target_ar)))
        sx = max(0, min(sw - crop_w, int(round((sw - crop_w) * focal_x))))
        sy = 0
    else:
        crop_w = sw
        crop_h = max(1, int(round(sw / target_ar)))
        sx = 0
        sy = max(0, min(sh - crop_h, int(round((sh - crop_h) * focal_y))))
    return src.copy(sx, sy, crop_w, crop_h).scaled(
        max(1, int(round(target_w * oversample))),
        max(1, int(round(target_h * oversample))),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _trim_loading_card_border(img: QImage) -> QImage:
    """Trim dark outer vignette framing bars from older 308x560 Riot loading portraits."""
    w, h = img.width(), img.height()
    if w < 120 or h < 180 or w >= h:
        return img
    probe = img if img.format() == QImage.Format.Format_ARGB32 else img.convertToFormat(QImage.Format.Format_ARGB32)
    bx = max(2, min(8, int(round(w * 0.025))))
    by = max(2, min(10, int(round(h * 0.018))))
    luma_sum = 0.0
    count = 0
    for x in range(1, w - 1, 4):
        for y in (1, min(h - 2, by), max(1, h - 1 - by), h - 2):
            px = probe.pixel(x, y)
            luma_sum += 0.299 * ((px >> 16) & 0xFF) + 0.587 * ((px >> 8) & 0xFF) + 0.114 * (px & 0xFF)
            count += 1
    for y in range(1, h - 1, 6):
        for x in (1, min(w - 2, bx), max(1, w - 1 - bx), w - 2):
            px = probe.pixel(x, y)
            luma_sum += 0.299 * ((px >> 16) & 0xFF) + 0.587 * ((px >> 8) & 0xFF) + 0.114 * (px & 0xFF)
            count += 1
    if count > 0 and (luma_sum / count) < 42.0:
        dx = max(4, min(w // 6, int(round(w * 0.035))))
        dy = max(4, min(h // 6, int(round(h * 0.020))))
        if w - 2 * dx >= 80 and h - 2 * dy >= 120:
            return img.copy(dx, dy, w - 2 * dx, h - 2 * dy)
    return img


def _estimate_subject_focal(img: QImage) -> tuple[float, float]:
    """Estimate normalized (focal_x, focal_y) of the primary hero figure inside uncentered splash art."""
    sw, sh = img.width(), img.height()
    if sw <= 0 or sh <= 0:
        return 0.50, 0.20
    pw, ph = 64, 36
    probe = img.scaled(pw, ph, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if probe.format() != QImage.Format.Format_ARGB32:
        probe = probe.convertToFormat(QImage.Format.Format_ARGB32)

    lum = [[0.0] * pw for _ in range(ph)]
    chroma = [[0.0] * pw for _ in range(ph)]
    for y in range(ph):
        row_l = lum[y]
        row_c = chroma[y]
        for x in range(pw):
            px = probe.pixel(x, y)
            r = (px >> 16) & 0xFF
            g = (px >> 8) & 0xFF
            b = px & 0xFF
            row_l[x] = 0.299 * r + 0.587 * g + 0.114 * b
            row_c[x] = float(max(r, g, b) - min(r, g, b))

    col_scores = [0.0] * pw
    grid = [[0.0] * pw for _ in range(ph)]
    total_w = 0.0
    for y in range(1, ph - 1):
        y_norm = (y + 0.5) / float(ph)
        vy = math.exp(-0.5 * ((y_norm - 0.32) / 0.26) ** 2)
        for x in range(1, pw - 1):
            x_norm = (x + 0.5) / float(pw)
            if x_norm < 0.12:
                hx = max(0.25, x_norm / 0.12)
            elif x_norm > 0.88:
                hx = max(0.25, (1.0 - x_norm) / 0.12)
            else:
                hx = 1.0
            gx = abs(lum[y][x + 1] - lum[y][x - 1])
            gy = abs(lum[y + 1][x] - lum[y - 1][x])
            w_px = ((gx + gy) * 1.0 + chroma[y][x] * 0.75 + max(0.0, lum[y][x] - 85.0) * 0.35) * vy * hx
            grid[y][x] = w_px
            col_scores[x] += w_px
            total_w += w_px

    if total_w <= 1e-3:
        return 0.50, 0.20

    win = 15
    best_s = 1
    best_sum = -1.0
    cur_sum = sum(col_scores[1 : 1 + win])
    best_sum = cur_sum
    for start in range(2, pw - win):
        cur_sum += col_scores[start + win - 1] - col_scores[start - 1]
        if cur_sum > best_sum:
            best_sum = cur_sum
            best_s = start

    wx_sum = 0.0
    wy_sum = 0.0
    w_win = 0.0
    for y in range(1, ph - 1):
        y_norm = (y + 0.5) / float(ph)
        for x in range(best_s, min(pw - 1, best_s + win)):
            wt = grid[y][x]
            if wt > 0.0:
                wx_sum += ((x + 0.5) / float(pw)) * wt
                wy_sum += y_norm * wt
                w_win += wt

    if w_win <= 1e-3:
        fx = (best_s + win * 0.5) / float(pw)
        fy = 0.20
    else:
        fx = wx_sum / w_win
        fy = wy_sum / w_win

    return max(0.24, min(0.76, fx)), max(0.14, min(0.32, fy))


def _scale_fit_height_anchored(
    src: QImage,
    target_w: float,
    target_h: float,
    *,
    anchor_src_x: float,
    anchor_dst_x: float,
    zoom: float = 1.0,
    focal_y: float = 0.08,
    oversample: float = 2.0,
) -> tuple[QImage, QRectF]:
    """Scale src by target_h * zoom keeping exact aspect ratio and anchoring focal point at anchor_dst_x."""
    sw, sh = src.width(), src.height()
    if sw <= 0 or sh <= 0 or target_w <= 0 or target_h <= 0:
        return src, QRectF(0.0, 0.0, max(1.0, target_w), max(1.0, target_h))
    draw_h = target_h * max(1.0, float(zoom))
    draw_w = draw_h * (sw / float(sh))
    draw_x = anchor_dst_x - anchor_src_x * draw_w
    draw_y = -(draw_h - target_h) * max(0.0, min(1.0, focal_y))
    scaled = src.scaled(
        max(1, int(round(draw_w * oversample))),
        max(1, int(round(draw_h * oversample))),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return scaled, QRectF(draw_x, draw_y, draw_w, draw_h)


def _load_preferred_center_art(data: MatchRecapData) -> tuple[QImage | None, bool]:
    """Prefer official Riot 1280x720 centered splash, then mainImg splash, then loading portrait."""
    cent = _load_local_image(data.skin_center_path)
    if cent is not None:
        return cent, True
    splash = _load_local_image(data.skin_splash_path)
    if splash is not None:
        return splash, False
    load = _load_local_image(data.skin_loading_path)
    if load is not None:
        return _trim_loading_card_border(load), True
    return None, False


def _load_preferred_portrait_art(data: MatchRecapData) -> tuple[QImage | None, str]:
    """Prefer official 308x560 vertical loading art, then centered splash, then mainImg splash."""
    load = _load_local_image(data.skin_loading_path)
    if load is not None:
        return _trim_loading_card_border(load), "loading"
    cent = _load_local_image(data.skin_center_path)
    if cent is not None:
        return cent, "center"
    splash = _load_local_image(data.skin_splash_path)
    if splash is not None:
        return splash, "splash"
    return None, "none"


def _blend_with_saturation(src: QImage, sat_keep: float, tint_rgba: tuple[int, int, int, int]) -> QImage:
    argb = src.convertToFormat(QImage.Format.Format_ARGB32)
    out = QImage(argb.size(), QImage.Format.Format_ARGB32)
    gray = argb.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)

    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    p.drawImage(0, 0, gray)
    if sat_keep > 0.01:
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setOpacity(max(0.0, min(1.0, sat_keep)))
        p.drawImage(0, 0, argb)
    if tint_rgba[3] > 0:
        p.setOpacity(1.0)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.fillRect(out.rect(), QColor(*tint_rgba))
    p.end()
    return out


def _make_tinted_ghost(src: QImage, tint: QColor) -> QImage:
    w, h = src.width(), src.height()
    gray = src.convertToFormat(QImage.Format.Format_Grayscale8)
    alpha_mask = gray.convertToFormat(QImage.Format.Format_Alpha8)
    out = QImage(w, h, QImage.Format.Format_ARGB32)
    out.fill(QColor(tint.red(), tint.green(), tint.blue(), 220))
    out.setAlphaChannel(alpha_mask)
    return out


def _apply_rect_fade_mask(
    img: QImage,
    *,
    left_fade: float = 0.0,
    right_fade: float = 0.0,
    top_fade: float = 0.0,
    bottom_fade: float = 0.0,
    peak_alpha: float = 1.0,
) -> QImage:
    out = img.convertToFormat(QImage.Format.Format_ARGB32).copy()
    w, h = float(out.width()), float(out.height())
    if w <= 0.0 or h <= 0.0:
        return out
    pk = int(round(max(0.0, min(1.0, peak_alpha)) * 255))
    p = QPainter(out)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)

    if left_fade > 0 or right_fade > 0 or peak_alpha < 0.999:
        hg = QLinearGradient(0.0, 0.0, w, 0.0)
        hg.setColorAt(0.0, QColor(255, 255, 255, 0 if left_fade > 0 else pk))
        if left_fade > 0:
            hg.setColorAt(min(0.49, left_fade), QColor(255, 255, 255, pk))
        if right_fade > 0:
            hg.setColorAt(max(0.51, 1.0 - right_fade), QColor(255, 255, 255, pk))
            hg.setColorAt(1.0, QColor(255, 255, 255, 0))
        else:
            hg.setColorAt(1.0, QColor(255, 255, 255, pk))
        p.fillRect(out.rect(), QBrush(hg))

    if top_fade > 0 or bottom_fade > 0:
        vg = QLinearGradient(0.0, 0.0, 0.0, h)
        vg.setColorAt(0.0, QColor(255, 255, 255, 0 if top_fade > 0 else 255))
        if top_fade > 0:
            vg.setColorAt(min(0.49, top_fade), QColor(255, 255, 255, 255))
        if bottom_fade > 0:
            vg.setColorAt(max(0.51, 1.0 - bottom_fade), QColor(255, 255, 255, 255))
            vg.setColorAt(1.0, QColor(255, 255, 255, 0))
        else:
            vg.setColorAt(1.0, QColor(255, 255, 255, 255))
        p.fillRect(out.rect(), QBrush(vg))

    p.end()
    return out


def _draw_crosshair(p: QPainter, x: float, y: float, r: float = 4.0, col: QColor | None = None) -> None:
    p.save()
    p.setPen(QPen(col or QColor(200, 170, 110, 130), 1.0))
    p.drawLine(QPointF(x - r, y), QPointF(x + r, y))
    p.drawLine(QPointF(x, y - r), QPointF(x, y + r))
    p.restore()


def _draw_scanlines(p: QPainter, rect: QRectF, step: float = 3.0, alpha: int = 7) -> None:
    p.save()
    p.setClipRect(rect)
    p.setPen(QPen(QColor(220, 240, 255, alpha), 1.0))
    y = rect.top()
    while y < rect.bottom():
        p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        y += step
    p.restore()


def _draw_ambient_fallback_stage(
    p: QPainter,
    rect: QRectF,
    data: MatchRecapData,
    *,
    focal_cx: float,
    focal_cy: float,
) -> None:
    """Rich atmospheric Hextech stage fallback when skin splash artwork is not cached."""
    hero_ico = _load_local_image(data.hero_icon_path)
    p.save()
    p.setClipRect(rect)

    accent_c = (
        QColor(200, 170, 110, 44)
        if data.won == 1
        else (QColor(217, 102, 116, 40) if data.won == 0 else QColor(10, 200, 185, 42))
    )
    rad = QRadialGradient(QPointF(focal_cx, focal_cy), max(rect.width(), rect.height()) * 0.52)
    rad.setColorAt(0.0, accent_c)
    rad.setColorAt(0.55, QColor(10, 200, 185, 18))
    rad.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(rect, QBrush(rad))

    if hero_ico is not None:
        side_px = int(min(hero_ico.width(), hero_ico.height()))
        sq = hero_ico.copy(
            (hero_ico.width() - side_px) // 2,
            (hero_ico.height() - side_px) // 2,
            side_px,
            side_px,
        )
        graded = _blend_with_saturation(sq, 0.55, (6, 18, 32, 54))
        masked = _apply_rect_fade_mask(
            graded,
            left_fade=0.30,
            right_fade=0.30,
            top_fade=0.22,
            bottom_fade=0.32,
            peak_alpha=0.28,
        )
        sz = min(rect.width(), rect.height()) * 0.78
        p.drawImage(QRectF(focal_cx - sz * 0.5, focal_cy - sz * 0.5, sz, sz), masked)

    # Geometric Hextech compass arcs
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(200, 170, 110, 28), 1.0))
    p.drawEllipse(QPointF(focal_cx, focal_cy), 84.0, 84.0)
    p.setPen(QPen(QColor(10, 200, 185, 24), 1.0, Qt.PenStyle.DashLine))
    p.drawEllipse(QPointF(focal_cx, focal_cy), 124.0, 124.0)
    p.restore()


def _draw_kda_inline(p: QPainter, x: float, y: float, h: float, kda_str: str, *, px: int = 22) -> float:
    """Draw colored K / D / A numbers cleanly without heavy background boxes."""
    parsed = _parse_kda(kda_str)
    f = _sans_font(px, bold=True, letter_spacing=0.3)
    fm = QFontMetricsF(f)
    p.setFont(f)
    if parsed is not None:
        k_s, d_s, a_s, _ratio = parsed
        parts = (
            (k_s, QColor(_GOLD_CREAM)),
            (" / ", QColor(148, 163, 184, 150)),
            (d_s, QColor(_DEFEAT_CRIMSON)),
            (" / ", QColor(148, 163, 184, 150)),
            (a_s, QColor(_CYAN_BRIGHT)),
        )
        cx = x
        for txt, col in parts:
            w = fm.horizontalAdvance(txt)
            p.setPen(QColor(0, 0, 0, 180))
            p.drawText(QRectF(cx, y + 1.0, w + 4.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
            p.setPen(col)
            p.drawText(QRectF(cx, y, w + 4.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, txt)
            cx += w
        return cx - x
    fallback = (kda_str or "--").strip()
    w = min(180.0, fm.horizontalAdvance(fallback))
    el = fm.elidedText(fallback, Qt.TextElideMode.ElideRight, 180.0)
    p.setPen(QColor(0, 0, 0, 180))
    p.drawText(QRectF(x, y + 1.0, w + 4.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, el)
    p.setPen(QColor(_GOLD_CREAM))
    p.drawText(QRectF(x, y, w + 4.0, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, el)
    return w


def _measure_kda_width(kda_str: str, *, px: int) -> float:
    parsed = _parse_kda(kda_str)
    fm = QFontMetricsF(_sans_font(px, bold=True, letter_spacing=0.3))
    if parsed is not None:
        return fm.horizontalAdvance(f"{parsed[0]} / {parsed[1]} / {parsed[2]}")
    return min(180.0, fm.horizontalAdvance((kda_str or "--").strip()))


def _draw_item_slot_modern(
    p: QPainter,
    rect: QRectF,
    item_entry: tuple[str, str] | None,
    idx: int,
    *,
    name_px: int = 10,
    max_name_w: float | None = None,
) -> None:
    path = _chamfer_path(rect, 4.0)
    if item_entry is None:
        p.fillPath(path, QColor(6, 15, 26, 125))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(70, 55, 20, 130), 1.0, Qt.PenStyle.DashLine))
        p.drawPath(path)
        return

    item_name, icon_path = item_entry
    clean_name = (item_name or f"装备{idx + 1}").strip()
    ico = _load_local_image(icon_path)

    p.save()
    p.setClipPath(path)
    p.fillRect(rect, QColor("#050D18"))
    if ico is not None:
        side = min(ico.width(), ico.height())
        cx0 = (ico.width() - side) // 2
        cy0 = (ico.height() - side) // 2
        p.drawImage(rect, ico.copy(cx0, cy0, side, side))
    else:
        tile_g = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        tile_g.setColorAt(0.0, QColor("#152438"))
        tile_g.setColorAt(1.0, QColor("#091322"))
        p.fillRect(rect, QBrush(tile_g))
        p.setFont(_sans_font(12, bold=True))
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, clean_name[:2])
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(200, 170, 110, 150), 1.05))
    p.drawPath(path)

    nw = max_name_w or (rect.width() + 26.0)
    f = _sans_font(name_px, bold=False)
    fm = QFontMetricsF(f)
    if fm.horizontalAdvance(clean_name) > nw and name_px > 9:
        f = _sans_font(9, bold=False)
        fm = QFontMetricsF(f)
    elided = fm.elidedText(clean_name, Qt.TextElideMode.ElideRight, nw)
    p.setFont(f)
    cap_r = QRectF(rect.center().x() - nw * 0.5, rect.bottom() + 4.0, nw, 15.0)
    p.setPen(QColor(0, 0, 0, 170))
    p.drawText(cap_r.translated(0.0, 1.0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided)
    p.setPen(QColor("#CBD5E1"))
    p.drawText(cap_r, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, elided)


def _draw_augment_bar_modern(
    p: QPainter,
    rect: QRectF,
    name: str,
    rarity_raw: str,
    icon_path: str,
    idx: int,
    *,
    style: str = "left_bar",
    icon_sz: float = 22.0,
    name_px: int = 11,
) -> None:
    rk = _normalize_rarity(rarity_raw)
    pal = _RARITY_PALETTE[rk]
    r_zh = pal["label"]
    accent = QColor(pal["accent"])
    bright = QColor(pal["bright"])
    path = _chamfer_path(rect, 3.0)

    if style == "smoked_glass":
        p.fillPath(path, QColor(6, 14, 24, 178))
        p.save()
        p.setClipPath(path)
        p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 2.8), accent)
        l_strip = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.top() + rect.height() * 0.72)
        l_strip.setColorAt(0.0, accent)
        l_strip.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        p.fillRect(QRectF(rect.left(), rect.top(), 2.6, rect.height() * 0.72), QBrush(l_strip))
        glow = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.top() + 34.0)
        glow.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 64))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(rect, QBrush(glow))
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 85), 0.95))
        p.drawPath(path)
    elif style == "top_bar":
        p.fillPath(path, QColor(10, 20, 34, 224))
        p.save()
        p.setClipPath(path)
        p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 2.2), accent)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 75), 0.9))
        p.drawPath(path)
    else:  # left_bar
        p.fillPath(path, QColor(11, 21, 34, 222))
        p.save()
        p.setClipPath(path)
        p.fillRect(QRectF(rect.left(), rect.top(), 2.8, rect.height()), accent)
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(148, 163, 184, 38), 0.85))
        p.drawPath(path)

    ico_x = rect.left() + 7.5
    ico_y = rect.top() + (rect.height() - icon_sz) * 0.5
    ico_r = QRectF(ico_x, ico_y, icon_sz, icon_sz)
    ico_path = _chamfer_path(ico_r, 3.0)
    ico = _load_local_image(icon_path)

    p.save()
    p.setClipPath(ico_path)
    p.fillRect(ico_r, QColor(_BG_DARK_START))
    if ico is not None:
        side = min(ico.width(), ico.height())
        sx0 = (ico.width() - side) // 2
        sy0 = (ico.height() - side) // 2
        p.drawImage(ico_r.adjusted(1.0, 1.0, -1.0, -1.0), ico.copy(sx0, sy0, side, side))
    else:
        em_g = QRadialGradient(ico_r.center(), icon_sz * 0.62)
        em_g.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 165))
        em_g.setColorAt(0.65, QColor(accent.red(), accent.green(), accent.blue(), 65))
        em_g.setColorAt(1.0, QColor(6, 16, 28, 220))
        p.fillRect(ico_r, QBrush(em_g))
        _draw_diamond(p, ico_r.center().x(), ico_r.center().y(), icon_sz * 0.34, accent)
        _draw_diamond(p, ico_r.center().x(), ico_r.center().y(), icon_sz * 0.16, bright)
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 155), 1.0))
    p.drawPath(ico_path)

    tx = ico_r.right() + 7.0
    clean_name = (name or "未知强化").strip()
    if rect.height() >= 42.0:
        avail = max(24.0, rect.right() - tx - 5.0)
        p.setFont(_sans_font(9, bold=True))
        p.setPen(bright)
        p.drawText(QRectF(tx, rect.top() + 5.0, max(16.0, avail - 20.0), 13.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, r_zh)
        p.setFont(_mono_font(9, bold=False))
        p.setPen(QColor(148, 163, 184, 130))
        p.drawText(QRectF(rect.right() - 24.0, rect.top() + 5.0, 18.0, 13.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"0{idx + 1}")

        cand_sizes = (name_px, max(9, name_px - 1), 9)
        nf = _sans_font(cand_sizes[0], bold=True)
        nfm = QFontMetricsF(nf)
        for sz in cand_sizes:
            cf = _sans_font(sz, bold=True)
            cfm = QFontMetricsF(cf)
            nf, nfm = cf, cfm
            if cfm.horizontalAdvance(clean_name) <= avail:
                break
        el = nfm.elidedText(clean_name, Qt.TextElideMode.ElideRight, avail)
        p.setFont(nf)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(tx, rect.top() + 20.0, avail, 19.0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, el)
    else:
        right_tag = f"{r_zh} 0{idx + 1}"
        rf = _sans_font(9, bold=False)
        rw = QFontMetricsF(rf).horizontalAdvance(right_tag) + 6.0
        avail = max(28.0, rect.right() - tx - rw - 6.0)
        nf = _sans_font(name_px, bold=True)
        nfm = QFontMetricsF(nf)
        if nfm.horizontalAdvance(clean_name) > avail and name_px > 9:
            nf = _sans_font(9, bold=True)
            nfm = QFontMetricsF(nf)
        el = nfm.elidedText(clean_name, Qt.TextElideMode.ElideRight, avail)
        p.setFont(nf)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(tx, rect.top(), avail, rect.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, el)
        p.setFont(rf)
        p.setPen(QColor(bright.red(), bright.green(), bright.blue(), 175))
        p.drawText(QRectF(rect.right() - rw - 6.0, rect.top(), rw, rect.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, right_tag)


def _draw_empty_augments_box(p: QPainter, rect: QRectF) -> None:
    path = _chamfer_path(rect, 4.0)
    p.fillPath(path, QColor(8, 18, 32, 165))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0, Qt.PenStyle.DashLine))
    p.drawPath(path)
    p.setFont(_sans_font(11, bold=False))
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "本局暂无已记录的海克斯强化")


def _draw_empty_items_box(p: QPainter, rect: QRectF) -> None:
    path = _chamfer_path(rect, 4.0)
    p.fillPath(path, QColor(8, 18, 32, 165))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(_GOLD_HAIRLINE), 1.0, Qt.PenStyle.DashLine))
    p.drawPath(path)
    p.setFont(_sans_font(11, bold=False))
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂未采集到终局装备数据")


# ---------------------------------------------------------------------------
# Template 1: Endfield Operator Archive Asymmetrical Split (820 x 480)
# ---------------------------------------------------------------------------

def _render_endfield_split(p: QPainter, data: MatchRecapData) -> None:
    lw, lh = 820.0, 480.0
    p.fillRect(QRectF(0.0, 0.0, lw, lh), QColor("#050C14"))

    # Subtle obsidian canvas + micro-dot grid on right zone
    p.fillRect(QRectF(0.0, 0.0, lw, lh), QColor("#060D16"))
    p.setPen(QPen(QColor(255, 255, 255, 11), 1.0))
    for gx in range(340, 810, 22):
        for gy in range(20, 470, 22):
            p.drawPoint(gx, gy)

    if _has_prismatic_presence(data):
        prism_glow = QRadialGradient(QPointF(560.0, 155.0), 230.0)
        prism_glow.setColorAt(0.0, QColor(217, 70, 239, 20))
        prism_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(320.0, 0.0, 500.0, 300.0), QBrush(prism_glow))

    # Left Hero Art Stage (0..342)
    art_w, art_h = 342.0, 480.0
    art_src, art_kind = _load_preferred_portrait_art(data)
    echo_col = QColor(_DEFEAT_CRIMSON if data.won == 0 else _CYAN_PRIMARY)
    if art_src is not None:
        if art_kind == "loading":
            hero_art = _cover_crop(art_src, art_w, art_h, focal_x=0.50, focal_y=0.08)
        elif art_kind == "center":
            hero_art = _cover_crop(art_src, art_w, art_h, focal_x=0.50, focal_y=0.12)
        else:
            fx, fy = _estimate_subject_focal(art_src)
            hero_art = _cover_crop(art_src, art_w, art_h, focal_x=fx, focal_y=min(0.18, fy))
        hero_graded = _blend_with_saturation(hero_art, 0.82, (6, 18, 32, 26))
        hero_masked = _apply_rect_fade_mask(hero_graded, right_fade=0.22, bottom_fade=0.04, peak_alpha=0.96)
        ghost = _apply_rect_fade_mask(_make_tinted_ghost(hero_art, echo_col), right_fade=0.35, left_fade=0.45, peak_alpha=0.20)
        p.drawImage(QRectF(12.0, -3.0, art_w, art_h), ghost)
        p.drawImage(QRectF(0.0, 0.0, art_w, art_h), hero_masked)
    else:
        _draw_ambient_fallback_stage(p, QRectF(0.0, 0.0, art_w, art_h), data, focal_cx=156.0, focal_cy=190.0)

    _draw_scanlines(p, QRectF(0.0, 0.0, 320.0, lh), step=3.0, alpha=7)

    top_scrim = QLinearGradient(0.0, 0.0, 0.0, 64.0)
    top_scrim.setColorAt(0.0, QColor(4, 10, 18, 178))
    top_scrim.setColorAt(1.0, QColor(4, 10, 18, 0))
    p.fillRect(QRectF(0.0, 0.0, art_w, 64.0), QBrush(top_scrim))

    file_tag, watermark = _hero_code_and_watermark(data)
    p.save()
    p.translate(22.0, 328.0)
    p.rotate(-90.0)
    p.setFont(_latin_font(28, bold=True, letter_spacing=3.8))
    p.setPen(QColor(240, 230, 210, 22))
    p.drawText(QRectF(0.0, 0.0, 300.0, 34.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, watermark)
    p.restore()

    p.setFont(_mono_font(9, bold=True, letter_spacing=1.2))
    p.setPen(QColor(0, 0, 0, 195))
    p.drawText(QRectF(20.0, 17.0, 260.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, file_tag)
    p.setPen(QColor(10, 200, 185, 220))
    p.drawText(QRectF(20.0, 16.0, 260.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, file_tag)

    # Bottom-left dark scrim cradling champion identity
    scrim = QLinearGradient(0.0, 270.0, 0.0, lh)
    scrim.setColorAt(0.0, QColor(5, 12, 20, 0))
    scrim.setColorAt(0.45, QColor(5, 12, 20, 195))
    scrim.setColorAt(1.0, QColor(4, 9, 16, 246))
    p.fillRect(QRectF(0.0, 270.0, art_w, 210.0), QBrush(scrim))

    en_stat, zh_stat, _combined, stat_accent, stat_text = _match_status_tokens(data)
    status_line = f"{en_stat} · {zh_stat}"
    if data.is_first_prismatic:
        status_line += "  ·  首棱彩局"

    skin_txt = _visible_skin_name(data)
    combo_txt, combo_col = _combo_line(data)

    # Dynamically pack bottom-left stack above baseline y=448
    p.fillRect(QRectF(20.0, 344.0, 3.0, 11.0), stat_accent)
    p.setFont(_mono_font(10, bold=True, letter_spacing=1.2))
    p.setPen(stat_text)
    p.drawText(QRectF(29.0, 342.0, 275.0, 15.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, status_line)

    hero_disp = _hero_display_name(data)
    hf = _sans_font(27, bold=True, letter_spacing=0.5)
    hfm = QFontMetricsF(hf)
    el_hero = hfm.elidedText(hero_disp, Qt.TextElideMode.ElideRight, 286.0)
    p.setFont(hf)
    p.setPen(QColor(0, 0, 0, 195))
    p.drawText(QRectF(20.0, 362.0, 290.0, 36.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_hero)
    p.setPen(QColor(_GOLD_CREAM))
    p.drawText(QRectF(20.0, 360.0, 290.0, 36.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_hero)

    sub_y = 398.0
    if skin_txt:
        sf = _mono_font(10, bold=True, letter_spacing=0.8)
        el_skin = QFontMetricsF(sf).elidedText(f"SKIN // {skin_txt}", Qt.TextElideMode.ElideRight, 288.0)
        p.setFont(sf)
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(QRectF(20.0, sub_y, 290.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_skin)
        sub_y += 21.0

    if combo_txt:
        cf = _sans_font(11, bold=True)
        el_combo = QFontMetricsF(cf).elidedText(combo_txt, Qt.TextElideMode.ElideRight, 292.0)
        p.setFont(cf)
        p.setPen(combo_col)
        p.drawText(QRectF(20.0, sub_y, 294.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_combo)

    p.setFont(_mono_font(9, bold=False, letter_spacing=1.0))
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(QRectF(20.0, 448.0, 260.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "HEXHELPER // ARAM MAYHEM")

    # Separator beam
    p.setPen(QPen(QColor(200, 170, 110, 95), 1.1))
    p.drawLine(QPointF(322.0, 16.0), QPointF(322.0, lh - 16.0))
    _draw_crosshair(p, 322.0, 348.0, r=4.0, col=QColor(10, 200, 185, 190))

    # Right Zone (342..796, width 454)
    rx, rw = 342.0, 454.0
    parsed_kda = _parse_kda(data.kda)
    meta_segs: list[str] = []
    if parsed_kda:
        meta_segs.append(parsed_kda[3])
    meta_segs.extend(_meta_parts(data, include_patch=True))
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_TEXT_MUTED))
    p.drawText(QRectF(rx, 20.0, 316.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "  ·  ".join(meta_segs))
    _draw_kda_inline(p, rx, 38.0, 34.0, data.kda, px=25)

    score, grade, mvp_badge = _resolve_rating(data)
    if score > 0.0 and grade:
        p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.5))
        p.drawLine(QPointF(680.0, 22.0), QPointF(796.0, 22.0))
        p.setFont(_latin_font(32 if len(grade) <= 2 else 26, bold=True, letter_spacing=0.5))
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(666.0, 24.0, 68.0, 46.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, grade)
        p.setFont(_sans_font(15, bold=True))
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(740.0, 26.0, 56.0, 22.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{score:.1f}")
        if mvp_badge:
            mvp_r = QRectF(748.0, 50.0, 48.0, 16.0)
            p.fillPath(_chamfer_path(mvp_r, 2.0), QColor(_GOLD_PRIMARY if mvp_badge == "MVP" else _CYAN_PRIMARY))
            p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
            p.setPen(QColor(_BG_DARK_START))
            p.drawText(mvp_r, Qt.AlignmentFlag.AlignCenter, mvp_badge)
        else:
            p.setFont(_mono_font(8, bold=True, letter_spacing=1.0))
            p.setPen(QColor(_TEXT_MUTED))
            p.drawText(QRectF(740.0, 50.0, 56.0, 16.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "RATING")

    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(rx, 82.0), QPointF(rx + rw, 82.0))

    # Augments Grid (0..6)
    augs = list(data.augments[:6])
    aug_icons = tuple(data.augment_icon_paths or ())
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.2))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(rx, 92.0, 230.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "HEXTECH AUGMENTS // 海克斯强化")
    p.setPen(QColor(_CYAN_PRIMARY))
    aug_den = 6 if len(augs) == 6 else max(4, len(augs))
    p.drawText(
        QRectF(rx + rw - 90.0, 92.0, 90.0, 14.0),
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        f"0{len(augs)} / 0{aug_den}" if augs else "00 / 04",
    )

    aug_y0 = 112.0
    if not augs:
        _draw_empty_augments_box(p, QRectF(rx, aug_y0, rw, 94.0))
    else:
        col_gap = 8.0
        col_w = (rw - col_gap) / 2.0
        use_two_rows = len(augs) <= 4
        row_h = 44.0 if use_two_rows else 28.0
        row_gap = 6.0 if use_two_rows else 5.0
        ico_sz = 24.0 if use_two_rows else 20.0
        for idx, (aname, rar) in enumerate(augs):
            r_i, c_i = divmod(idx, 2)
            ar = QRectF(rx + c_i * (col_w + col_gap), aug_y0 + r_i * (row_h + row_gap), col_w, row_h)
            ico_p = aug_icons[idx] if idx < len(aug_icons) else ""
            _draw_augment_bar_modern(p, ar, aname, rar, ico_p, idx, style="left_bar", icon_sz=ico_sz, name_px=11)
        if len(augs) == 5:
            s6_r = QRectF(rx + (col_w + col_gap), aug_y0 + 2 * (row_h + row_gap), col_w, row_h)
            p.setPen(QPen(QColor(148, 163, 184, 34), 1.0, Qt.PenStyle.DashLine))
            p.setBrush(QColor(8, 16, 28, 90))
            p.drawPath(_chamfer_path(s6_r, 3.0))
            p.setFont(_mono_font(9, bold=False, letter_spacing=1.0))
            p.setPen(QColor(_TEXT_FAINT))
            p.drawText(s6_r, Qt.AlignmentFlag.AlignCenter, "06 // EXTRA SLOT STANDBY")

    # Final Build Items (0..6)
    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(rx, 218.0), QPointF(rx + rw, 218.0))
    items = list(data.item_entries[:6])
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.2))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(rx, 226.0, 220.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "FINAL BUILD // 终局出装")
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(rx + rw - 90.0, 226.0, 90.0, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"0{len(items)} / 06")

    if not items:
        _draw_empty_items_box(p, QRectF(rx, 246.0, rw, 58.0))
    else:
        cell_w = rw / 6.0
        for idx in range(6):
            ix = rx + idx * cell_w + (cell_w - 42.0) * 0.5
            entry = items[idx] if idx < len(items) else None
            _draw_item_slot_modern(p, QRectF(ix, 246.0, 42.0, 42.0), entry, idx, name_px=10, max_name_w=cell_w - 2.0)

    # Bottom Telemetry Split
    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(rx, 318.0), QPointF(rx + rw, 318.0))

    has_stats, stat_cells = _prepare_stat_items(data)
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(
        QRectF(rx, 328.0, 220.0, 14.0),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        "FINAL STATS // 属性面板" if has_stats else "MATCH PROFILE // 对局摘要",
    )

    stat_w = 236.0
    if has_stats:
        cols = 4 if len(stat_cells) > 6 else max(2, min(4, (len(stat_cells) + 1) // 2))
        rows = 2 if len(stat_cells) > cols else 1
        scw = stat_w / float(cols)
        cell_h = 42.0 if rows == 2 else 88.0
        for idx, sc_item in enumerate(stat_cells[: cols * rows]):
            r_i, c_i = divmod(idx, cols)
            sx = rx + c_i * scw
            sy = 348.0 + r_i * 48.0
            p.fillRect(QRectF(sx, sy, scw - 5.0, cell_h), QColor(10, 20, 32, 165))
            if sc_item.highlighted:
                p.fillRect(QRectF(sx, sy, 2.0, cell_h), QColor(_GOLD_PRIMARY))
            p.setFont(_sans_font(9, bold=False))
            p.setPen(QColor(_GOLD_PRIMARY if sc_item.highlighted else _TEXT_MUTED))
            p.drawText(QRectF(sx + 5.0, sy + 4.0, scw - 10.0, 13.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.short_label)
            p.setFont(_mono_font(13, bold=True, letter_spacing=0.2))
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(sx + 5.0, sy + 18.0, scw - 10.0, cell_h - 22.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.value)
    else:
        scw = stat_w / 2.0
        for idx, sc_item in enumerate(stat_cells[:4]):
            r_i, c_i = divmod(idx, 2)
            sx = rx + c_i * scw
            sy = 348.0 + r_i * 48.0
            p.fillRect(QRectF(sx, sy, scw - 6.0, 42.0), QColor(10, 20, 32, 165))
            if sc_item.highlighted:
                p.fillRect(QRectF(sx, sy, 2.0, 42.0), QColor(_GOLD_PRIMARY))
            p.setFont(_sans_font(9, bold=False))
            p.setPen(QColor(_GOLD_PRIMARY if sc_item.highlighted else _TEXT_MUTED))
            p.drawText(QRectF(sx + 7.0, sy + 4.0, scw - 14.0, 13.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.full_label)
            sc_w = scw - 14.0
            vf = _sans_font(11, bold=True)
            vfm = QFontMetricsF(vf)
            if vfm.horizontalAdvance(sc_item.value) > sc_w:
                vf = _sans_font(10, bold=True)
                vfm = QFontMetricsF(vf)
            v_el = vfm.elidedText(sc_item.value, Qt.TextElideMode.ElideRight, sc_w)
            p.setFont(vf)
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(sx + 7.0, sy + 18.0, sc_w, 20.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, v_el)

    vx = rx + stat_w + 10.0
    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(vx, 328.0), QPointF(vx, 444.0))

    cx = vx + 14.0
    cw = rx + rw - cx
    combat = _prepare_combat_summary(data)
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(cx, 328.0, cw, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "COMBAT DATA // 战绩")

    p.setFont(_sans_font(10, bold=False))
    p.setPen(QColor(_TEXT_MUTED))
    p.drawText(QRectF(cx, 348.0, 62.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, combat.headline_label)
    p.setFont(_mono_font(14, bold=True))
    p.setPen(QColor(_GOLD_CREAM if combat.is_carry or not combat.has_dmg else _CYAN_BRIGHT))
    p.drawText(QRectF(cx + 58.0, 346.0, 74.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, combat.headline_val)
    p.setFont(_mono_font(10, bold=True))
    p.setPen(QColor(_CYAN_BRIGHT))
    p.drawText(QRectF(cx + cw - 78.0, 348.0, 78.0, 16.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, combat.right_tag)

    p.fillRect(QRectF(cx, 370.0, cw, 3.5), QColor(255, 255, 255, 24))
    bar_g = QLinearGradient(cx, 370.0, cx + cw * combat.fill_ratio, 370.0)
    bar_g.setColorAt(0.0, QColor(_GOLD_PRIMARY))
    bar_g.setColorAt(1.0, QColor(_CYAN_PRIMARY))
    p.fillRect(QRectF(cx, 370.0, cw * combat.fill_ratio, 3.5), QBrush(bar_g))

    mcw = (cw - 8.0) / 2.0
    for idx, (clbl, cval, ccol) in enumerate(combat.cells):
        r_i, c_i = divmod(idx, 2)
        mx = cx + c_i * (mcw + 8.0)
        my = 384.0 + r_i * 31.0
        p.fillRect(QRectF(mx, my, mcw, 26.0), QColor(10, 20, 32, 165))
        p.setFont(_sans_font(9, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(mx + 6.0, my, 52.0, 26.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, clbl)
        p.setFont(_mono_font(11, bold=True))
        p.setPen(ccol)
        p.drawText(QRectF(mx + 46.0, my, mcw - 52.0, 26.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, cval)

    patch_txt = (data.dataset_patch or "26.23").strip()
    p.setPen(QPen(QColor(200, 170, 110, 65), 1.0))
    p.drawLine(QPointF(rx, 456.0), QPointF(rx + rw, 456.0))
    p.setFont(_mono_font(8, bold=False, letter_spacing=1.0))
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(QRectF(rx, 460.0, rw, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"HEXHELPER  ·  {patch_txt} DATA ENGINE")

    p.setPen(QPen(QColor(200, 170, 110, 95), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(0.5, 0.5, lw - 1.0, lh - 1.0))


# ---------------------------------------------------------------------------
# Template 2: Widescreen Cinema Gacha Banner (780 x 500)
# ---------------------------------------------------------------------------

def _render_cinematic_banner(p: QPainter, data: MatchRecapData) -> None:
    lw, lh = 780.0, 500.0
    banner_h = 244.0
    p.fillRect(QRectF(0.0, 0.0, lw, lh), QColor("#050B14"))

    if _has_prismatic_presence(data):
        pg = QRadialGradient(QPointF(lw * 0.5, 278.0), 280.0)
        pg.setColorAt(0.0, QColor(217, 70, 239, 22))
        pg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0.0, 220.0, lw, 120.0), QBrush(pg))

    banner_rect = QRectF(0.0, 0.0, lw, banner_h)
    art_src, is_centered = _load_preferred_center_art(data)
    echo_col = QColor(_DEFEAT_CRIMSON if data.won == 0 else _CYAN_PRIMARY)
    p.save()
    p.setClipRect(banner_rect)
    if art_src is not None:
        fx = 0.50 if is_centered else _estimate_subject_focal(art_src)[0]
        bg_crop = _cover_crop(art_src, lw, banner_h, focal_x=fx, focal_y=0.20)
        bg_wings = _blend_with_saturation(bg_crop, 0.45, (4, 11, 20, 160))
        p.drawImage(banner_rect, bg_wings)

        fg_scaled, fg_rect = _scale_fit_height_anchored(
            art_src,
            lw,
            banner_h,
            anchor_src_x=fx,
            anchor_dst_x=lw * 0.50,
            zoom=1.02,
            focal_y=0.0,
        )
        fg_graded = _blend_with_saturation(fg_scaled, 0.88, (5, 14, 26, 20))
        fg_masked = _apply_rect_fade_mask(
            fg_graded,
            left_fade=0.22,
            right_fade=0.22,
            bottom_fade=0.14,
            peak_alpha=0.96,
        )
        ghost = _apply_rect_fade_mask(
            _make_tinted_ghost(fg_scaled, echo_col),
            left_fade=0.26,
            right_fade=0.26,
            bottom_fade=0.20,
            peak_alpha=0.14,
        )
        p.drawImage(fg_rect.translated(10.0, -2.0), ghost)
        p.drawImage(fg_rect, fg_masked)
    else:
        _draw_ambient_fallback_stage(p, banner_rect, data, focal_cx=lw * 0.62, focal_cy=112.0)

    _draw_scanlines(p, banner_rect, step=3.0, alpha=7)
    p.restore()

    en_stat, _zh_stat, combined_stat, stat_accent, stat_text = _match_status_tokens(data)
    p.setFont(_latin_font(54, bold=True, letter_spacing=6.0))
    p.setPen(QColor(240, 230, 210, 18))
    p.drawText(QRectF(24.0, 34.0, 480.0, 66.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, en_stat)

    t_wash = QLinearGradient(0.0, 0.0, 0.0, 48.0)
    t_wash.setColorAt(0.0, QColor(4, 10, 18, 155))
    t_wash.setColorAt(1.0, QColor(4, 10, 18, 0))
    p.fillRect(QRectF(0.0, 0.0, lw, 48.0), QBrush(t_wash))

    l_wash = QLinearGradient(0.0, 0.0, 320.0, 0.0)
    l_wash.setColorAt(0.0, QColor(4, 10, 18, 175))
    l_wash.setColorAt(1.0, QColor(4, 10, 18, 0))
    p.fillRect(banner_rect, QBrush(l_wash))

    b_fade = QLinearGradient(0.0, 124.0, 0.0, banner_h)
    b_fade.setColorAt(0.0, QColor(5, 11, 20, 0))
    b_fade.setColorAt(0.55, QColor(5, 11, 20, 180))
    b_fade.setColorAt(1.0, QColor(5, 11, 20, 250))
    p.fillRect(QRectF(0.0, 124.0, lw, banner_h - 124.0), QBrush(b_fade))

    r_scrim = QRadialGradient(QPointF(665.0, 200.0), 190.0)
    r_scrim.setColorAt(0.0, QColor(4, 10, 18, 145))
    r_scrim.setColorAt(1.0, QColor(4, 10, 18, 0))
    p.fillRect(QRectF(460.0, 110.0, 320.0, banner_h - 110.0), QBrush(r_scrim))

    # Top bar
    p.fillRect(QRectF(24.0, 21.0, 3.0, 11.0), stat_accent)
    status_str = combined_stat + ("   |   ✦ 首棱彩局" if data.is_first_prismatic else "")
    p.setFont(_sans_font(11, bold=True, letter_spacing=0.6))
    p.setPen(stat_text)
    p.drawText(QRectF(33.0, 18.0, 300.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, status_str)

    meta_str = "   ·   ".join(_meta_parts(data, include_patch=True))
    p.setFont(_mono_font(10, bold=True, letter_spacing=0.9))
    p.setPen(QColor(226, 232, 240, 205))
    p.drawText(QRectF(lw - 354.0, 18.0, 330.0, 18.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, meta_str)

    # Bottom-left hero identity
    hero_disp = _hero_display_name(data)
    skin_txt = _visible_skin_name(data)
    combo_txt, combo_col = _combo_line(data)
    hero_y = 154.0 if (skin_txt and combo_txt) else (164.0 if (skin_txt or combo_txt) else 184.0)

    hf = _sans_font(28, bold=True, letter_spacing=0.5)
    el_hero = QFontMetricsF(hf).elidedText(hero_disp, Qt.TextElideMode.ElideRight, 380.0)
    p.setFont(hf)
    p.setPen(QColor(0, 0, 0, 205))
    p.drawText(QRectF(24.0, hero_y + 2.0, 380.0, 36.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_hero)
    p.setPen(QColor(_GOLD_CREAM))
    p.drawText(QRectF(24.0, hero_y, 380.0, 36.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_hero)

    sub_y = hero_y + 38.0
    if skin_txt:
        sf = _mono_font(10, bold=True, letter_spacing=0.9)
        el_skin = QFontMetricsF(sf).elidedText(f"SKIN // {skin_txt}", Qt.TextElideMode.ElideRight, 380.0)
        p.setFont(sf)
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(QRectF(24.0, sub_y, 380.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_skin)
        sub_y += 20.0
    if combo_txt:
        cf = _sans_font(11, bold=True)
        el_combo = QFontMetricsF(cf).elidedText(combo_txt, Qt.TextElideMode.ElideRight, 380.0)
        p.setFont(cf)
        p.setPen(combo_col)
        p.drawText(QRectF(24.0, sub_y, 380.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_combo)

    # Bottom-right KDA + Rating
    score, grade, mvp_badge = _resolve_rating(data)
    has_rating = score > 0.0 and bool(grade)
    kda_anchor_r = 600.0 if has_rating else (lw - 24.0)
    parsed_kda = _parse_kda(data.kda)
    ratio_str = parsed_kda[3] if parsed_kda else "KDA"
    p.setFont(_sans_font(10, bold=False))
    p.setPen(QColor(_TEXT_MUTED))
    p.drawText(QRectF(kda_anchor_r - 170.0, 162.0, 170.0, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, ratio_str)
    kda_w = _measure_kda_width(data.kda, px=24)
    _draw_kda_inline(p, kda_anchor_r - kda_w, 178.0, 32.0, data.kda, px=24)

    if has_rating:
        p.setPen(QPen(QColor(200, 170, 110, 100), 1.0))
        p.drawLine(QPointF(618.0, 164.0), QPointF(618.0, 222.0))
        p.setFont(_latin_font(33 if len(grade) <= 2 else 26, bold=True, letter_spacing=0.6))
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(630.0, 160.0, 68.0, 52.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, grade)
        p.setFont(_mono_font(15, bold=True))
        p.drawText(QRectF(700.0, 166.0, 56.0, 20.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{score:.1f}")
        if mvp_badge:
            mvp_r = QRectF(708.0, 192.0, 48.0, 17.0)
            p.fillPath(_chamfer_path(mvp_r, 2.0), QColor(_GOLD_PRIMARY if mvp_badge == "MVP" else _CYAN_PRIMARY))
            p.setFont(_mono_font(9, bold=True, letter_spacing=1.0))
            p.setPen(QColor(_BG_DARK_START))
            p.drawText(mvp_r, Qt.AlignmentFlag.AlignCenter, mvp_badge)
        else:
            p.setFont(_mono_font(8, bold=True, letter_spacing=1.0))
            p.setPen(QColor(_TEXT_MUTED))
            p.drawText(QRectF(700.0, 192.0, 56.0, 17.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "RATING")

    # Horizon divider
    p.setPen(QPen(QColor(200, 170, 110, 140), 1.1))
    p.drawLine(QPointF(24.0, banner_h), QPointF(lw - 24.0, banner_h))
    _draw_crosshair(p, lw * 0.5, banner_h, r=5.0, col=QColor(_GOLD_CREAM))

    pad = 24.0
    aw = lw - pad * 2.0

    # 1. Single horizontal row of augments (0..6)
    augs = list(data.augments[:6])
    aug_icons = tuple(data.augment_icon_paths or ())
    if not augs:
        _draw_empty_augments_box(p, QRectF(pad, 256.0, aw, 46.0))
    else:
        n = len(augs)
        agap = 6.0 if n >= 6 else 8.0
        apw = min(320.0, (aw - agap * (n - 1)) / float(n))
        ico_sz = 20.0 if n >= 6 else 24.0
        n_px = 10 if n >= 6 else 11
        for idx, (aname, rar) in enumerate(augs):
            ar = QRectF(pad + idx * (apw + agap), 256.0, apw, 46.0)
            ico_p = aug_icons[idx] if idx < len(aug_icons) else ""
            _draw_augment_bar_modern(p, ar, aname, rar, ico_p, idx, style="top_bar", icon_sz=ico_sz, name_px=n_px)

    # 2. Middle Split: Items + Combat Summary
    items = list(data.item_entries[:6])
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(pad, 314.0, 220.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"FINAL BUILD  ·  0{len(items)} / 06")
    if not items:
        _draw_empty_items_box(p, QRectF(pad, 333.0, 456.0, 62.0))
    else:
        icw = 456.0 / 6.0
        for idx in range(6):
            ix = pad + idx * icw + (icw - 42.0) * 0.5
            entry = items[idx] if idx < len(items) else None
            _draw_item_slot_modern(p, QRectF(ix, 333.0, 42.0, 42.0), entry, idx, name_px=10, max_name_w=icw - 2.0)

    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(494.0, 314.0), QPointF(494.0, 404.0))

    cx, cw = 510.0, lw - pad - 510.0
    combat = _prepare_combat_summary(data)
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(cx, 314.0, cw, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "COMBAT DATA")
    p.setFont(_sans_font(10, bold=False))
    p.setPen(QColor(_TEXT_MUTED))
    p.drawText(QRectF(cx, 331.0, 70.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, combat.headline_label)
    p.setFont(_mono_font(14, bold=True))
    p.setPen(QColor(_GOLD_CREAM if combat.is_carry or not combat.has_dmg else _CYAN_BRIGHT))
    p.drawText(QRectF(cx + 58.0, 330.0, 76.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, combat.headline_val)
    p.setFont(_mono_font(10, bold=True))
    p.setPen(QColor(_CYAN_BRIGHT))
    p.drawText(QRectF(cx + cw - 82.0, 331.0, 82.0, 16.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, combat.right_tag)

    p.fillRect(QRectF(cx, 353.0, cw, 3.5), QColor(255, 255, 255, 24))
    bg = QLinearGradient(cx, 353.0, cx + cw * combat.fill_ratio, 353.0)
    bg.setColorAt(0.0, QColor(_GOLD_PRIMARY))
    bg.setColorAt(1.0, QColor(_CYAN_PRIMARY))
    p.fillRect(QRectF(cx, 353.0, cw * combat.fill_ratio, 3.5), QBrush(bg))

    ccw = (cw - 8.0) / 2.0
    for idx, (clbl, cval, ccol) in enumerate(combat.cells):
        r_i, c_i = divmod(idx, 2)
        mx = cx + c_i * (ccw + 8.0)
        my = 362.0 + r_i * 22.0
        p.setFont(_sans_font(9, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(mx, my, 54.0, 20.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, clbl)
        p.setFont(_mono_font(11, bold=True))
        p.setPen(ccol)
        p.drawText(QRectF(mx + 44.0, my, ccw - 44.0, 20.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, cval)

    # 3. Bottom Stat Ticker Strip
    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(pad, 414.0), QPointF(lw - pad, 414.0))
    p.drawLine(QPointF(pad, 466.0), QPointF(lw - pad, 466.0))
    p.fillRect(QRectF(pad, 415.0, aw, 50.0), QColor(9, 17, 29, 165))

    has_stats, stat_cells = _prepare_stat_items(data)
    cols = max(1, len(stat_cells))
    tcw = aw / float(cols)
    for idx, sc_item in enumerate(stat_cells):
        tx = pad + idx * tcw
        if idx > 0:
            p.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
            p.drawLine(QPointF(tx, 422.0), QPointF(tx, 458.0))
        if sc_item.highlighted:
            p.fillRect(QRectF(tx + 12.0, 415.0, tcw - 24.0, 2.0), QColor(_GOLD_PRIMARY))
        p.setFont(_sans_font(9, bold=False))
        p.setPen(QColor(_GOLD_PRIMARY if sc_item.highlighted else _TEXT_MUTED))
        p.drawText(QRectF(tx + 4.0, 420.0, tcw - 8.0, 15.0), Qt.AlignmentFlag.AlignCenter, sc_item.full_label)
        vf = _mono_font(14 if has_stats else 12, bold=True)
        v_el = QFontMetricsF(vf).elidedText(sc_item.value, Qt.TextElideMode.ElideRight, tcw - 12.0)
        p.setFont(vf)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(tx + 4.0, 436.0, tcw - 8.0, 22.0), Qt.AlignmentFlag.AlignCenter, v_el)

    patch_txt = (data.dataset_patch or "26.23").strip()
    p.setFont(_mono_font(8, bold=False, letter_spacing=1.2))
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(QRectF(pad, 472.0, aw, 16.0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, f"HEXHELPER  //  ARAM MAYHEM {patch_txt} ENGINE")

    p.setPen(QPen(QColor(200, 170, 110, 95), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(0.5, 0.5, lw - 1.0, lh - 1.0))


# ---------------------------------------------------------------------------
# Template 3: Endfield 14-deg Diagonal Hologram Gacha Screen (820 x 480)
# ---------------------------------------------------------------------------

def _render_diagonal_hologram(p: QPainter, data: MatchRecapData) -> None:
    lw, lh = 820.0, 480.0
    dock_y = 352.0
    p.fillRect(QRectF(0.0, 0.0, lw, lh), QColor("#040A12"))

    x_cut_top, x_cut_bot = 368.0, 272.0
    stage_poly = QPolygonF(
        [
            QPointF(x_cut_top - 40.0, 0.0),
            QPointF(lw, 0.0),
            QPointF(lw, dock_y + 10.0),
            QPointF(x_cut_bot - 40.0, dock_y + 10.0),
        ]
    )
    stage_path = QPainterPath()
    stage_path.addPolygon(stage_poly)

    art_w, art_h = 560.0, dock_y + 12.0
    art_src, is_centered = _load_preferred_center_art(data)
    echo_col = QColor(_DEFEAT_CRIMSON if data.won == 0 else _CYAN_PRIMARY)

    p.save()
    p.setClipPath(stage_path)
    if art_src is not None:
        fx = 0.50 if is_centered else _estimate_subject_focal(art_src)[0]
        bg_stage = _cover_crop(art_src, art_w, art_h, focal_x=fx, focal_y=0.20)
        bg_graded = _blend_with_saturation(bg_stage, 0.45, (4, 10, 18, 175))
        p.drawImage(QRectF(lw - art_w, 0.0, art_w, art_h), bg_graded)

        fg_scaled, fg_rect = _scale_fit_height_anchored(
            art_src,
            lw,
            dock_y,
            anchor_src_x=fx,
            anchor_dst_x=558.0,
            zoom=1.03,
            focal_y=0.05,
        )
        fg_graded = _blend_with_saturation(fg_scaled, 0.88, (6, 16, 28, 22))
        fg_masked = _apply_rect_fade_mask(
            fg_graded,
            left_fade=0.14,
            right_fade=0.06,
            bottom_fade=0.12,
            peak_alpha=0.96,
        )
        ghost = _apply_rect_fade_mask(
            _make_tinted_ghost(fg_scaled, echo_col),
            left_fade=0.14,
            right_fade=0.12,
            bottom_fade=0.16,
            peak_alpha=0.16,
        )
        p.drawImage(fg_rect.translated(-10.0, -2.0), ghost)
        p.drawImage(fg_rect, fg_masked)
    else:
        _draw_ambient_fallback_stage(
            p,
            QRectF(x_cut_bot - 20.0, 0.0, lw - x_cut_bot + 20.0, dock_y),
            data,
            focal_cx=lw * 0.68,
            focal_cy=dock_y * 0.48,
        )
    _draw_scanlines(p, QRectF(x_cut_bot - 40.0, 0.0, lw - x_cut_bot + 40.0, dock_y), step=3.0, alpha=7)
    p.restore()

    if _has_prismatic_presence(data):
        pg = QRadialGradient(QPointF(180.0, 230.0), 170.0)
        pg.setColorAt(0.0, QColor(217, 70, 239, 24))
        pg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0.0, 120.0, 350.0, 220.0), QBrush(pg))

    left_poly = QPolygonF(
        [
            QPointF(0.0, 0.0),
            QPointF(x_cut_top, 0.0),
            QPointF(x_cut_bot, dock_y),
            QPointF(0.0, dock_y),
        ]
    )
    left_path = QPainterPath()
    left_path.addPolygon(left_poly)
    lg = QLinearGradient(0.0, 0.0, x_cut_top, 0.0)
    lg.setColorAt(0.0, QColor(4, 10, 18, 250))
    lg.setColorAt(0.84, QColor(6, 14, 24, 238))
    lg.setColorAt(1.0, QColor(8, 18, 30, 195))
    p.fillPath(left_path, QBrush(lg))

    p.setPen(QPen(QColor(10, 200, 185, 110), 1.0))
    p.drawLine(QPointF(x_cut_top + 6.0, 0.0), QPointF(x_cut_bot + 6.0, dock_y))
    p.setPen(QPen(QColor(200, 170, 110, 185), 1.6))
    p.drawLine(QPointF(x_cut_top, 0.0), QPointF(x_cut_bot, dock_y))
    _draw_crosshair(p, (x_cut_top + x_cut_bot) * 0.5, dock_y * 0.5, r=4.5, col=QColor(_CYAN_BRIGHT))

    # Top-right floating grade stamp
    score, grade, mvp_badge = _resolve_rating(data)
    if score > 0.0 and grade:
        stamp_scrim = QRadialGradient(QPointF(762.0, 44.0), 140.0)
        stamp_scrim.setColorAt(0.0, QColor(4, 10, 18, 165))
        stamp_scrim.setColorAt(0.65, QColor(4, 10, 18, 95))
        stamp_scrim.setColorAt(1.0, QColor(4, 10, 18, 0))
        p.fillRect(QRectF(620.0, 0.0, 200.0, 120.0), QBrush(stamp_scrim))
        p.setFont(_latin_font(33 if len(grade) <= 2 else 26, bold=True, letter_spacing=0.5))
        p.setPen(QColor(0, 0, 0, 195))
        p.drawText(QRectF(692.0, 18.0, 104.0, 42.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, grade)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(692.0, 16.0, 104.0, 42.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, grade)
        sub_stamp = f"{score:.1f}  ·  {mvp_badge}" if mvp_badge else f"{score:.1f}  ·  RATING"
        p.setFont(_mono_font(12, bold=True, letter_spacing=0.8))
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(QRectF(650.0, 56.0, 146.0, 18.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, sub_stamp)

    # Vertical technical serial on far-right edge
    patch_txt = (data.dataset_patch or "26.23").strip()
    serial_tail = f"SKIN-{data.skin_id}" if int(data.skin_id or 0) > 0 else f"HERO-{(data.hero_id or '0').upper()}"
    p.save()
    p.translate(lw - 14.0, 326.0)
    p.rotate(-90.0)
    p.setFont(_mono_font(8, bold=True, letter_spacing=1.5))
    p.setPen(QColor(240, 230, 210, 142))
    p.drawText(QRectF(0.0, 0.0, 220.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"HEXHELPER // {patch_txt} // {serial_tail}")
    p.restore()

    # Left Info Column
    lx = 24.0
    en_stat, zh_stat, _combined, stat_accent, stat_text = _match_status_tokens(data)
    top_segs = [f"{en_stat} · {zh_stat}"]
    if data.is_first_prismatic:
        top_segs.append("首棱彩局")
    dur = _format_duration(data.duration_s)
    if dur:
        top_segs.append(dur)

    p.fillRect(QRectF(lx, 20.0, 3.0, 11.0), stat_accent)
    p.setFont(_mono_font(10, bold=True, letter_spacing=0.9))
    p.setPen(stat_text)
    p.drawText(QRectF(lx + 9.0, 18.0, 310.0, 15.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "  ·  ".join(top_segs))

    hero_disp = _hero_display_name(data)
    hf = _sans_font(26, bold=True)
    el_hero = QFontMetricsF(hf).elidedText(hero_disp, Qt.TextElideMode.ElideRight, 310.0)
    p.setFont(hf)
    p.setPen(QColor(_GOLD_CREAM))
    p.drawText(QRectF(lx, 42.0, 310.0, 34.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_hero)

    skin_txt = _visible_skin_name(data)
    combo_txt, combo_col = _combo_line(data)
    sub_y = 78.0
    if skin_txt:
        sf = _mono_font(10, bold=True, letter_spacing=0.8)
        el_skin = QFontMetricsF(sf).elidedText(f"SKIN // {skin_txt}", Qt.TextElideMode.ElideRight, 305.0)
        p.setFont(sf)
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(QRectF(lx, sub_y, 305.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_skin)
        sub_y += 18.0
    if combo_txt:
        cf = _sans_font(11, bold=True)
        el_combo = QFontMetricsF(cf).elidedText(combo_txt, Qt.TextElideMode.ElideRight, 296.0)
        p.setFont(cf)
        p.setPen(combo_col)
        p.drawText(QRectF(lx, sub_y, 296.0, 16.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_combo)
        sub_y += 20.0
    kda_y = max(118.0, sub_y + 4.0)

    kw = _draw_kda_inline(p, lx, kda_y, 28.0, data.kda, px=22)
    parsed_kda = _parse_kda(data.kda)
    kda_meta: list[str] = []
    if parsed_kda:
        kda_meta.append(parsed_kda[3])
    tk = (data.team_kills or "").strip()
    if tk:
        kda_meta.append(tk)
    if kda_meta:
        p.setFont(_mono_font(10, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(lx + kw + 10.0, kda_y + 5.0, 130.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, " · ".join(kda_meta))

    # Stacked Augments (0..6) clipped safely left of diagonal vector line
    augs = list(data.augments[:6])
    aug_icons = tuple(data.augment_icon_paths or ())
    aug_hdr_y = max(156.0, kda_y + 36.0)
    aug_den = 6 if len(augs) == 6 else max(4, len(augs))
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(
        QRectF(lx, aug_hdr_y, 270.0, 14.0),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        f"HEXTECH AUGMENTS  ·  0{len(augs)} / 0{aug_den}" if augs else "HEXTECH AUGMENTS  ·  00 / 04",
    )

    if not augs:
        _draw_empty_augments_box(p, QRectF(lx, aug_hdr_y + 20.0, 248.0, 44.0))
    else:
        n = len(augs)
        row_step = 26.5 if n >= 6 else 30.5
        bar_h = 22.5 if n >= 6 else 25.5
        ico_sz = 17.5 if n >= 6 else 19.0
        n_px = 10 if n >= 6 else 11
        y0 = aug_hdr_y + 19.0
        for idx, (aname, rar) in enumerate(augs):
            ry = y0 + idx * row_step
            if ry + bar_h > dock_y - 6.0:
                break
            x_diag = x_cut_top + (x_cut_bot - x_cut_top) * ((ry + bar_h) / dock_y)
            strip_w = max(120.0, min(266.0, x_diag - lx - 18.0))
            ico_p = aug_icons[idx] if idx < len(aug_icons) else ""
            _draw_augment_bar_modern(p, QRectF(lx, ry, strip_w, bar_h), aname, rar, ico_p, idx, style="left_bar", icon_sz=ico_sz, name_px=n_px)

    # Bottom Frosted Command Dock (352..480)
    p.fillRect(QRectF(0.0, dock_y, lw, lh - dock_y), QColor(5, 11, 20, 240))
    p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.3))
    p.drawLine(QPointF(0.0, dock_y), QPointF(lw, dock_y))

    items = list(data.item_entries[:6])
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(16.0, dock_y + 10.0, 200.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"FINAL BUILD  ·  0{len(items)} / 06")
    if not items:
        _draw_empty_items_box(p, QRectF(16.0, dock_y + 32.0, 426.0, 62.0))
    else:
        icw = 426.0 / 6.0
        for idx in range(6):
            ix = 16.0 + idx * icw + (icw - 42.0) * 0.5
            entry = items[idx] if idx < len(items) else None
            _draw_item_slot_modern(p, QRectF(ix, dock_y + 32.0, 42.0, 42.0), entry, idx, name_px=10, max_name_w=icw - 2.0)

    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(450.0, dock_y + 12.0), QPointF(450.0, lh - 12.0))

    # Middle Dock: Stats or Summary
    has_stats, stat_cells = _prepare_stat_items(data)
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.0))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(462.0, dock_y + 10.0, 184.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "FINAL STATS" if has_stats else "MATCH PROFILE")
    if has_stats:
        p.setPen(QPen(QColor(255, 255, 255, 16), 1.0))
        p.drawLine(QPointF(553.0, dock_y + 32.0), QPointF(553.0, dock_y + 108.0))
        for idx, sc_item in enumerate(stat_cells[:8]):
            r_i, c_i = divmod(idx, 2)
            sx = 462.0 + c_i * 100.0
            sy = dock_y + 30.0 + r_i * 21.0
            p.setFont(_sans_font(9, bold=False))
            p.setPen(QColor(_GOLD_PRIMARY if sc_item.highlighted else _TEXT_MUTED))
            p.drawText(QRectF(sx, sy, 30.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.short_label)
            p.setFont(_mono_font(11, bold=True))
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(sx + 30.0, sy, 52.0, 18.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, sc_item.value)
    else:
        for idx, sc_item in enumerate(stat_cells[:4]):
            sy = dock_y + 30.0 + idx * 21.0
            p.setFont(_sans_font(9, bold=False))
            p.setPen(QColor(_GOLD_PRIMARY if sc_item.highlighted else _TEXT_MUTED))
            p.drawText(QRectF(462.0, sy, 40.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.short_label)
            vf = _mono_font(10, bold=True)
            v_el = QFontMetricsF(vf).elidedText(sc_item.value, Qt.TextElideMode.ElideRight, 138.0)
            p.setFont(vf)
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(504.0, sy, 140.0, 18.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, v_el)

    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(656.0, dock_y + 12.0), QPointF(656.0, lh - 12.0))

    # Right Dock: Combat Telemetry
    rx, rw = 668.0, 136.0
    combat = _prepare_combat_summary(data)
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.0))
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(rx, dock_y + 10.0, rw, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "COMBAT DATA")
    p.setFont(_mono_font(13, bold=True))
    p.setPen(QColor(_GOLD_CREAM if combat.is_carry or not combat.has_dmg else _CYAN_BRIGHT))
    p.drawText(QRectF(rx, dock_y + 28.0, 70.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, combat.headline_val)
    p.setFont(_mono_font(10, bold=True))
    p.setPen(QColor(_CYAN_BRIGHT))
    p.drawText(QRectF(rx + 60.0, dock_y + 29.0, rw - 60.0, 16.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, combat.right_tag)

    p.fillRect(QRectF(rx, dock_y + 49.0, rw, 3.0), QColor(255, 255, 255, 24))
    p.fillRect(QRectF(rx, dock_y + 49.0, rw * combat.fill_ratio, 3.0), QColor(_CYAN_PRIMARY))

    for idx, (clbl, cval, ccol) in enumerate(combat.cells):
        sy = dock_y + 57.0 + idx * 15.0
        p.setFont(_sans_font(9, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(rx, sy, 68.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, clbl)
        p.setFont(_mono_font(10, bold=True))
        p.setPen(ccol)
        p.drawText(QRectF(rx + 68.0, sy, rw - 68.0, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, cval)

    p.setPen(QPen(QColor(200, 170, 110, 95), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(0.5, 0.5, lw - 1.0, lh - 1.0))


# ---------------------------------------------------------------------------
# Template 4 (Default): Minimal Obsidian & Translucent Glass Editorial (760 x 480)
# ---------------------------------------------------------------------------

def _render_editorial_glass(p: QPainter, data: MatchRecapData) -> None:
    lw, lh = 760.0, 480.0
    full_rect = QRectF(0.0, 0.0, lw, lh)

    bg = QLinearGradient(0.0, 0.0, lw, lh)
    bg.setColorAt(0.0, QColor("#040B14"))
    bg.setColorAt(0.55, QColor("#071220"))
    bg.setColorAt(1.0, QColor("#030A13"))
    p.fillRect(full_rect, QBrush(bg))

    # Subtle Hextech atmospheric blooms keeping left portrait margin dark (< 38 luma)
    br_glow = QRadialGradient(QPointF(lw - 135.0, lh - 100.0), 270.0)
    br_glow.setColorAt(0.0, QColor(10, 200, 185, 32))
    br_glow.setColorAt(0.60, QColor(3, 151, 171, 11))
    br_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(full_rect, QBrush(br_glow))

    if _has_prismatic_presence(data):
        prism_glow = QRadialGradient(QPointF(lw * 0.52, 174.0), 280.0)
        prism_glow.setColorAt(0.0, QColor(217, 70, 239, 24))
        prism_glow.setColorAt(0.65, QColor(147, 51, 234, 8))
        prism_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(full_rect, QBrush(prism_glow))

    # Full-width continuous atmospheric skin phantom backdrop (no inner box clipping the silhouette)
    art_src, is_centered = _load_preferred_center_art(data)
    echo_col = QColor(_DEFEAT_CRIMSON if data.won == 0 else _CYAN_PRIMARY)
    if art_src is not None:
        fx = 0.50 if is_centered else _estimate_subject_focal(art_src)[0]
        art = _cover_crop(art_src, lw, lh, focal_x=fx, focal_y=0.14)
        graded = _blend_with_saturation(art, 0.62, (6, 18, 32, 52))
        phantom = _apply_rect_fade_mask(graded, left_fade=0.34, bottom_fade=0.30, peak_alpha=0.58)
        ghost = _apply_rect_fade_mask(_make_tinted_ghost(art, echo_col), left_fade=0.42, bottom_fade=0.38, peak_alpha=0.18)
        p.drawImage(QRectF(14.0, -4.0, lw, lh), ghost)
        p.drawImage(QRectF(0.0, 0.0, lw, lh), phantom)
    else:
        # Subtle compass & gold-cyan diagonal sheen when splash artwork is absent
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(200, 170, 110, 22), 1.0))
        p.drawEllipse(QPointF(lw - 78.0, 96.0), 82.0, 82.0)
        p.setPen(QPen(QColor(10, 200, 185, 22), 1.0, Qt.PenStyle.DashLine))
        p.drawEllipse(QPointF(lw - 78.0, 96.0), 116.0, 116.0)
        p.restore()

    _draw_scanlines(p, full_rect, step=3.0, alpha=6)

    # Single minimal outer frame + apex crosshairs
    p.setPen(QPen(QColor(200, 170, 110, 115), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(0.5, 0.5, lw - 1.0, lh - 1.0))
    _draw_crosshair(p, lw * 0.5, 14.0, r=4.0, col=QColor(_GOLD_PRIMARY))
    _draw_crosshair(p, 560.0, 128.0, r=4.5, col=QColor(10, 200, 185, 160))

    pad = 24.0
    aw = lw - pad * 2.0

    # Top minimal status ribbon (victory gold vs defeat crimson inside x=26..170, y=18..44)
    _en_stat, _zh_stat, combined_stat, stat_accent, stat_text = _match_status_tokens(data)
    p.fillRect(QRectF(pad, 21.0, 3.0, 11.0), stat_accent)
    status_line = combined_stat + ("   |   首棱彩局" if data.is_first_prismatic else "")
    p.setFont(_sans_font(11, bold=True, letter_spacing=0.7))
    p.setPen(stat_text)
    p.drawText(QRectF(pad + 9.0, 18.0, 310.0, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, status_line)

    meta_str = "   ·   ".join(_meta_parts(data, include_patch=True))
    p.setFont(_mono_font(10, bold=False, letter_spacing=0.8))
    p.setPen(QColor(_TEXT_MUTED))
    p.drawText(QRectF(lw - pad - 330.0, 18.0, 330.0, 18.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, meta_str)

    # Left Hero Portrait (54x54 chamfered square)
    por_r = QRectF(pad, 52.0, 54.0, 54.0)
    por_p = _chamfer_path(por_r, 6.0)
    hero_ico = _load_local_image(data.hero_icon_path)
    p.save()
    p.setClipPath(por_p)
    if hero_ico is not None:
        side = min(hero_ico.width(), hero_ico.height())
        sx0 = (hero_ico.width() - side) // 2
        sy0 = (hero_ico.height() - side) // 2
        sq = hero_ico.copy(sx0, sy0, side, side)
        inset = max(0, min(6, side // 14))
        p.drawImage(por_r, sq.copy(inset, inset, side - inset * 2, side - inset * 2) if inset > 0 else sq)
    else:
        med = QRadialGradient(por_r.center(), 28.0)
        med.setColorAt(0.0, QColor("#0B5A68"))
        med.setColorAt(0.65, QColor("#081422"))
        med.setColorAt(1.0, QColor("#040B14"))
        p.fillRect(por_r, QBrush(med))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(10, 200, 185, 145), 1.25))
        p.drawPath(_chamfer_path(por_r.adjusted(4.0, 4.0, -4.0, -4.0), 4.0))
        hero_disp_m = (data.hero_name or "").strip()
        monogram = hero_disp_m[0] if hero_disp_m else ((data.hero_id or "H").strip()[:2].upper() or "H")
        p.setFont(_sans_font(22 if len(monogram) == 1 else 16, bold=True))
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(por_r, Qt.AlignmentFlag.AlignCenter, monogram)
    p.restore()
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(_GOLD_PRIMARY), 1.3))
    p.drawPath(por_p)

    # Right Borderless KDA + Grade/Score
    score, grade, mvp_badge = _resolve_rating(data)
    has_rating = score > 0.0 and bool(grade)
    kda_anchor_r = 584.0 if has_rating else (lw - pad)

    kda_w = _measure_kda_width(data.kda, px=23)
    _draw_kda_inline(p, kda_anchor_r - kda_w, 52.0, 30.0, data.kda, px=23)
    parsed_kda = _parse_kda(data.kda)
    p.setFont(_mono_font(10, bold=False))
    p.setPen(QColor(_TEXT_MUTED))
    p.drawText(
        QRectF(kda_anchor_r - 130.0, 84.0, 130.0, 16.0),
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        parsed_kda[3] if parsed_kda else "KDA",
    )
    left_limit_x = kda_anchor_r - max(kda_w, 120.0) - 14.0

    if has_rating:
        p.setPen(QPen(QColor(200, 170, 110, 110), 1.0))
        p.drawLine(QPointF(602.0, 54.0), QPointF(602.0, 102.0))
        p.setFont(_latin_font(31 if len(grade) <= 2 else 25, bold=True, letter_spacing=0.5))
        p.setPen(QColor(0, 0, 0, 195))
        p.drawText(QRectF(614.0, 52.0, 68.0, 48.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, grade)
        p.setPen(QColor(_GOLD_CREAM))
        p.drawText(QRectF(614.0, 50.0, 68.0, 48.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, grade)
        p.setFont(_mono_font(15, bold=True))
        p.drawText(QRectF(680.0, 52.0, 56.0, 20.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{score:.1f}")
        if mvp_badge:
            mvp_r = QRectF(688.0, 76.0, 48.0, 16.0)
            p.fillPath(_chamfer_path(mvp_r, 2.0), QColor(_GOLD_PRIMARY if mvp_badge == "MVP" else _CYAN_PRIMARY))
            p.setFont(_mono_font(9, bold=True, letter_spacing=1.0))
            p.setPen(QColor(_BG_DARK_START))
            p.drawText(mvp_r, Qt.AlignmentFlag.AlignCenter, mvp_badge)
        else:
            p.setFont(_mono_font(8, bold=True, letter_spacing=1.0))
            p.setPen(QColor(_TEXT_MUTED))
            p.drawText(QRectF(680.0, 76.0, 56.0, 16.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "RATING")

    # Hero name + dynamic non-colliding SKIN // + combo
    info_x = 88.0
    info_max_w = max(140.0, left_limit_x - info_x)
    hero_disp = _hero_display_name(data)
    hf = _sans_font(25, bold=True)
    hfm = QFontMetricsF(hf)
    skin_txt = _visible_skin_name(data)
    hero_cap_w = info_max_w * (0.58 if skin_txt else 1.0)
    el_hero = hfm.elidedText(hero_disp, Qt.TextElideMode.ElideRight, max(96.0, hero_cap_w))
    p.setFont(hf)
    p.setPen(QColor(0, 0, 0, 200))
    p.drawText(QRectF(info_x, 51.5, info_max_w, 30.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_hero)
    p.setPen(QColor(_GOLD_CREAM))
    p.drawText(QRectF(info_x, 50.0, info_max_w, 30.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_hero)

    if skin_txt:
        hero_actual_w = min(info_max_w - 72.0, hfm.horizontalAdvance(el_hero))
        skin_x = info_x + hero_actual_w + 12.0
        avail_skin_w = max(0.0, left_limit_x - skin_x)
        if avail_skin_w >= 64.0:
            sf = _mono_font(10, bold=True, letter_spacing=0.8)
            el_skin = QFontMetricsF(sf).elidedText(f"SKIN // {skin_txt}", Qt.TextElideMode.ElideRight, avail_skin_w)
            p.setFont(sf)
            p.setPen(QColor(_GOLD_PRIMARY))
            p.drawText(QRectF(skin_x, 58.0, avail_skin_w, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_skin)

    combo_txt, combo_col = _combo_line(data)
    if combo_txt:
        cf = _sans_font(11, bold=True)
        el_combo = QFontMetricsF(cf).elidedText(combo_txt, Qt.TextElideMode.ElideRight, info_max_w)
        p.setFont(cf)
        p.setPen(combo_col)
        p.drawText(QRectF(info_x, 84.0, info_max_w, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, el_combo)
    else:
        p.setFont(_sans_font(10, bold=False))
        p.setPen(QColor(_GOLD_PRIMARY))
        p.drawText(QRectF(info_x, 84.0, info_max_w, 18.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "◈ 海克斯大乱斗 · ARAM MAYHEM")

    # Smoked-glass Augments Row (y: 130..198)
    augs = list(data.augments[:6])
    aug_icons = tuple(data.augment_icon_paths or ())
    aug_den = 6 if len(augs) == 6 else max(4, len(augs))
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(pad, 130.0, 220.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "HEXTECH AUGMENTS")
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(
        QRectF(lw - pad - 80.0, 130.0, 80.0, 14.0),
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
        f"0{len(augs)} / 0{aug_den}" if augs else "00 / 04",
    )

    if not augs:
        _draw_empty_augments_box(p, QRectF(pad, 147.0, aw, 50.0))
    else:
        n = len(augs)
        agap = 6.0 if n >= 6 else 8.0
        apw = min(320.0, (aw - agap * (n - 1)) / float(n))
        ico_sz = 20.0 if n >= 6 else 24.0
        n_px = 10 if n >= 6 else 11
        for idx, (aname, rar) in enumerate(augs):
            ar = QRectF(pad + idx * (apw + agap), 147.0, apw, 50.0)
            ico_p = aug_icons[idx] if idx < len(aug_icons) else ""
            _draw_augment_bar_modern(p, ar, aname, rar, ico_p, idx, style="smoked_glass", icon_sz=ico_sz, name_px=n_px)

    # Floating 6 Items Row (y: 218..304)
    items = list(data.item_entries[:6])
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.1))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(QRectF(pad, 218.0, 200.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "FINAL BUILD")
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(lw - pad - 80.0, 218.0, 80.0, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"0{len(items)} / 06")

    if not items:
        _draw_empty_items_box(p, QRectF(pad, 238.0, aw, 64.0))
    else:
        icw = aw / 6.0
        for idx in range(6):
            ix = pad + idx * icw + (icw - 44.0) * 0.5
            entry = items[idx] if idx < len(items) else None
            _draw_item_slot_modern(p, QRectF(ix, 238.0, 44.0, 44.0), entry, idx, name_px=10, max_name_w=icw - 4.0)

    # Unified Translucent Glass Footer Deck (y: 326..446)
    deck_r = QRectF(pad, 326.0, aw, 120.0)
    deck_p = _chamfer_path(deck_r, 4.0)
    p.fillPath(deck_p, QColor(4, 11, 20, 188))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(200, 170, 110, 68), 1.0))
    p.drawPath(deck_p)

    has_stats, stat_cells = _prepare_stat_items(data)
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.0))
    p.setPen(QColor(_GOLD_PRIMARY))
    p.drawText(
        QRectF(pad + 14.0, 336.0, 200.0, 14.0),
        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
        "FINAL STATS // 终局面板" if has_stats else "MATCH PROFILE // 对局摘要",
    )

    if has_stats:
        scw = 340.0 / 4.0
        for idx, sc_item in enumerate(stat_cells[:8]):
            r_i, c_i = divmod(idx, 4)
            sx = pad + 14.0 + c_i * scw
            sy = 356.0 + r_i * 41.0
            p.setFont(_sans_font(9, bold=False))
            p.setPen(QColor(_GOLD_PRIMARY if sc_item.highlighted else _TEXT_MUTED))
            p.drawText(QRectF(sx, sy, scw - 6.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.full_label)
            p.setFont(_mono_font(14, bold=True))
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(sx, sy + 15.0, scw - 6.0, 20.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.value)
    else:
        scw = 340.0 / 2.0
        for idx, sc_item in enumerate(stat_cells[:4]):
            r_i, c_i = divmod(idx, 2)
            sx = pad + 14.0 + c_i * scw
            sy = 356.0 + r_i * 41.0
            if sc_item.highlighted:
                p.fillRect(QRectF(sx, sy + 3.0, 2.0, 28.0), QColor(_GOLD_PRIMARY))
            tx_off = 6.0 if sc_item.highlighted else 0.0
            p.setFont(_sans_font(9, bold=False))
            p.setPen(QColor(_GOLD_PRIMARY if sc_item.highlighted else _TEXT_MUTED))
            p.drawText(QRectF(sx + tx_off, sy, scw - 10.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, sc_item.full_label)
            vf = _sans_font(12, bold=True)
            v_el = QFontMetricsF(vf).elidedText(sc_item.value, Qt.TextElideMode.ElideRight, scw - 12.0)
            p.setFont(vf)
            p.setPen(QColor(_GOLD_CREAM))
            p.drawText(QRectF(sx + tx_off, sy + 15.0, scw - 10.0, 20.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, v_el)

    p.setPen(QPen(QColor(255, 255, 255, 22), 1.0))
    p.drawLine(QPointF(386.0, 336.0), QPointF(386.0, 436.0))

    cx, cw = 402.0, lw - pad - 14.0 - 402.0
    combat = _prepare_combat_summary(data)
    p.fillRect(QRectF(cx, 337.5, 3.0, 11.0), QColor(_CYAN_PRIMARY))
    p.setFont(_mono_font(9, bold=True, letter_spacing=1.0))
    p.setPen(QColor(_CYAN_PRIMARY))
    p.drawText(QRectF(cx + 8.0, 336.0, 140.0, 14.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "COMBAT DATA // 战绩")
    p.setFont(_mono_font(11, bold=True))
    p.setPen(QColor(_GOLD_CREAM if combat.is_carry or not combat.has_dmg else _CYAN_BRIGHT))
    short_hl = "输出" if combat.has_dmg else "参团"
    p.drawText(QRectF(cx + cw - 175.0, 334.0, 96.0, 18.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{short_hl} {combat.headline_val}")
    p.setPen(QColor(_CYAN_BRIGHT))
    p.drawText(QRectF(cx + cw - 78.0, 334.0, 78.0, 18.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, combat.right_tag)

    p.fillRect(QRectF(cx, 356.0, cw, 5.0), QColor(255, 255, 255, 24))
    bg_bar = QLinearGradient(cx, 356.0, cx + cw * combat.fill_ratio, 356.0)
    bg_bar.setColorAt(0.0, QColor(_GOLD_PRIMARY))
    bg_bar.setColorAt(1.0, QColor(_CYAN_PRIMARY))
    p.fillRect(QRectF(cx, 356.0, cw * combat.fill_ratio, 5.0), QBrush(bg_bar))

    ccw = cw / 4.0
    for idx, (clbl, cval, ccol) in enumerate(combat.cells):
        mx = cx + idx * ccw
        p.setFont(_sans_font(9, bold=False))
        p.setPen(QColor(_TEXT_MUTED))
        p.drawText(QRectF(mx, 376.0, ccw, 16.0), Qt.AlignmentFlag.AlignCenter, clbl)
        p.setFont(_mono_font(14, bold=True))
        p.setPen(ccol)
        p.drawText(QRectF(mx, 396.0, ccw, 22.0), Qt.AlignmentFlag.AlignCenter, cval)

    # Bottom technical baseline
    patch_txt = (data.dataset_patch or "26.23").strip()
    p.setFont(_mono_font(8, bold=False, letter_spacing=1.2))
    p.setPen(QColor(_TEXT_FAINT))
    p.drawText(QRectF(pad, 454.0, aw, 16.0), Qt.AlignmentFlag.AlignCenter, f"HEXHELPER  ·  ARAM MAYHEM {patch_txt} DATA ENGINE")


_TEMPLATE_PAINTERS = {
    "endfield_split": _render_endfield_split,
    "cinematic_banner": _render_cinematic_banner,
    "diagonal_hologram": _render_diagonal_hologram,
    "editorial_glass": _render_editorial_glass,
}


def render_match_recap_image(
    data: MatchRecapData,
    width: int | None = None,
    height: int | None = None,
    *,
    template: str = DEFAULT_RECAP_TEMPLATE,
) -> QImage:
    """Render an offline LOL V16.23 Hextech v2 match recap card in the selected layout template."""
    _ensure_card_fonts()
    spec = get_recap_template_spec(template)
    base_w = float(spec.logical_width)
    base_h = float(spec.logical_height)
    w = max(320, int(width)) if width is not None else spec.logical_width
    h = max(220, int(height)) if height is not None else spec.logical_height

    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(_BG_DARK_START))

    scale = min(w / base_w, h / base_h)
    offset_x = (w - base_w * scale) * 0.5
    offset_y = (h - base_h * scale) * 0.5

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if abs(offset_x) > 1e-4 or abs(offset_y) > 1e-4:
        p.translate(offset_x, offset_y)
    if abs(scale - 1.0) > 1e-4:
        p.scale(scale, scale)

    painter_fn = _TEMPLATE_PAINTERS[spec.id]
    painter_fn(p, data)
    p.end()
    return img


def copy_recap_to_clipboard(
    data: MatchRecapData,
    width: int | None = None,
    height: int | None = None,
    *,
    template: str = DEFAULT_RECAP_TEMPLATE,
) -> QImage:
    """Render match recap card in the specified template and copy the QImage directly to clipboard."""
    img = render_match_recap_image(data, width=width, height=height, template=template)
    cb = QGuiApplication.clipboard()
    if cb is not None:
        cb.setImage(img)
    return img


class MatchRecapDialog(QDialog):
    """Hextech dark-blue preview modal with live 4-template switcher, 1-click copy & PNG export."""

    def __init__(
        self,
        data: MatchRecapData,
        parent: QWidget | None = None,
        *,
        card_width: int | None = None,
        card_height: int | None = None,
        template: str = DEFAULT_RECAP_TEMPLATE,
        lifecycle_state=None,
        snoozed: bool = False,
        on_core_action=None,
        on_recap_action: Callable[[str, bool], None] | None = None,
        on_template_changed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.data = data
        self._card_width_override = card_width
        self._card_height_override = card_height
        self._template_id: str = normalize_recap_template(template)
        self._on_core_action = on_core_action
        self._on_recap_action = on_recap_action
        self._on_template_changed = on_template_changed
        self._close_action_emitted: bool = False
        self._recap_cta_action = ""
        self._card_image: QImage = render_match_recap_image(
            data,
            width=card_width,
            height=card_height,
            template=self._template_id,
        )

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
            QFrame#RecapTemplateBar {
                background-color: #061322;
                border: 1px solid #463714;
                border-left: 3px solid #C8AA6E;
                border-radius: 2px;
            }
            QPushButton#RecapTemplateChip {
                background-color: #081828;
                color: #A09B8C;
                border: 1px solid #463714;
                border-radius: 2px;
                padding: 4px 11px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton#RecapTemplateChip:hover {
                border-color: #C8AA6E;
                color: #F0E6D2;
                background-color: #0B2034;
            }
            QPushButton#RecapTemplateChip:checked {
                background-color: #0B2A3C;
                color: #F0E6D2;
                border: 1px solid #0AC8B9;
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
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(10)

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

        # Interactive 4-template selector strip (STRICTLY outside the exported PNG image)
        tpl_bar = QFrame(self)
        tpl_bar.setObjectName("RecapTemplateBar")
        tb_lay = QHBoxLayout(tpl_bar)
        tb_lay.setContentsMargins(10, 6, 10, 6)
        tb_lay.setSpacing(8)

        tb_title = QLabel("版式模板")
        tb_title.setFont(_sans_font(11, bold=True))
        tb_title.setStyleSheet("color: #C8AA6E; background: transparent; border: none;")
        tb_lay.addWidget(tb_title)

        self.template_buttons: dict[str, QPushButton] = {}
        for tpl_id in RECAP_TEMPLATE_ORDER:
            spec = RECAP_TEMPLATES[tpl_id]
            btn = QPushButton(spec.short_label, tpl_bar)
            btn.setObjectName("RecapTemplateChip")
            btn.setCheckable(True)
            btn.setChecked(tpl_id == self._template_id)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(f"{spec.display_name}（{spec.logical_width}×{spec.logical_height}）\n{spec.subtitle}")
            btn.clicked.connect(lambda _checked=False, tid=tpl_id: self.set_template(tid, emit_change=True))
            tb_lay.addWidget(btn)
            self.template_buttons[tpl_id] = btn

        tb_lay.addStretch(1)
        self.template_size_label = QLabel("", tpl_bar)
        self.template_size_label.setFont(_mono_font(10, bold=False, letter_spacing=0.6))
        self.template_size_label.setStyleSheet("color: #94A3B8; background: transparent; border: none;")
        tb_lay.addWidget(self.template_size_label)
        root.addWidget(tpl_bar)

        # Preview card container
        preview_frame = QFrame(self)
        preview_frame.setObjectName("RecapPreviewFrame")
        pf_lay = QVBoxLayout(preview_frame)
        pf_lay.setContentsMargins(6, 6, 6, 6)

        self.preview_label = QLabel(preview_frame)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background: transparent; border: none;")
        pf_lay.addWidget(self.preview_label)
        root.addWidget(preview_frame)

        # Optional post-match outer nudge strip (STRICTLY outside the exported PNG image)
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

        self._apply_preview_pixmap()
        self.adjustSize()

    @property
    def card_image(self) -> QImage:
        return self._card_image

    @property
    def current_template(self) -> str:
        return self._template_id

    def _apply_preview_pixmap(self) -> None:
        pm = QPixmap.fromImage(self._card_image)
        max_preview_w = 820
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail_w = screen.availableGeometry().width()
            if avail_w > 0:
                max_preview_w = min(max_preview_w, max(480, avail_w - 120))
        if pm.width() > max_preview_w:
            pm = pm.scaledToWidth(max_preview_w, Qt.TransformationMode.SmoothTransformation)
        self.preview_label.setPixmap(pm)
        spec = get_recap_template_spec(self._template_id)
        self.template_size_label.setText(f"{self._card_image.width()} × {self._card_image.height()}  ·  {spec.display_name}")
        for tid, btn in self.template_buttons.items():
            prev = btn.blockSignals(True)
            btn.setChecked(tid == self._template_id)
            btn.blockSignals(prev)

    def _rerender_card(self) -> None:
        self._card_image = render_match_recap_image(
            self.data,
            width=self._card_width_override,
            height=self._card_height_override,
            template=self._template_id,
        )
        self._apply_preview_pixmap()
        self.adjustSize()

    def set_template(self, template_id: str, *, emit_change: bool = True) -> None:
        """Switch the active recap card template in-place and re-render the preview."""
        norm = normalize_recap_template(template_id)
        changed = norm != self._template_id
        self._template_id = norm
        self._rerender_card()
        if emit_change and changed and self._on_template_changed is not None:
            self._on_template_changed(norm)

    def update_data(self, data: MatchRecapData) -> None:
        """Re-render the recap card in-place preserving the currently active template."""
        self.data = data
        self._rerender_card()

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
