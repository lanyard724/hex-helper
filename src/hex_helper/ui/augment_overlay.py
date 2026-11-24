"""In-game 3/4-card augment picker passive overlay styled in LoL Hextech Client gold/cyan bevels (tight-bbox, zero-60fps)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from hex_helper.scoring.destiny import DestinyItemRec
from hex_helper.scoring.tier import AugmentScore, UnifiedAugmentDecision
from hex_helper.ui.hextech_widgets import HextechShimmerBar
from hex_helper.ui.theme import BASE_OVERLAY_QSS, RARITY_COLORS, tier_badge_qss
from hex_helper.ui.win32_window import physical_rect_to_logical, show_no_activate


@dataclass(frozen=True)
class AugmentBadgeRenderItem:
    slot_index: int
    score: AugmentScore
    screen_box: tuple[int, int, int, int]
    is_best: bool = False
    destiny_items: tuple[DestinyItemRec, ...] = ()
    uncertain: bool = False
    archetype_hint: str = ""
    is_transmute: bool = False
    unified: UnifiedAugmentDecision | None = None


def _compact_sample_text(score: AugmentScore) -> str:
    if score.games < 30:
        return f"仅{score.games}场" if score.games > 0 else "暂无样本"
    if score.low_sample or score.games < 300:
        return f"低样本·{score.games}场"
    if score.games >= 1000:
        return f"{score.games / 1000.0:.1f}k场"
    return f"{score.games}场"


class _SingleBadgeCard(QFrame):
    def __init__(
        self,
        item: AugmentBadgeRenderItem,
        animate_sheen: bool = True,
        card_width: int = 258,
    ) -> None:
        super().__init__()
        self._is_best = item.is_best
        self._sheen_t: float = -0.35
        self._sheen_anim: QVariantAnimation | None = None
        u = item.unified

        self.setObjectName("PanelRoot")
        if u is not None and u.overridden_by_combo:
            self.setStyleSheet(
                "QFrame#PanelRoot { background-color: rgba(9, 20, 40, 246); "
                "border: 2px solid #0AC8B9; border-top: 3px solid #F0E6D2; border-radius: 2px; }"
            )
        elif item.is_best:
            self.setStyleSheet(
                "QFrame#PanelRoot { background-color: rgba(9, 20, 40, 246); "
                "border: 2px solid #C89B3C; border-top: 3px solid #F0E6D2; border-radius: 2px; }"
            )
        else:
            rarity_color = RARITY_COLORS.get(item.score.rarity, ("#785A28", ""))[0]
            self.setStyleSheet(
                f"QFrame#PanelRoot {{ background-color: rgba(1, 10, 19, 238); "
                f"border: 1px solid #463714; border-top: 2px solid {rarity_color}; border-radius: 2px; }}"
            )

        card_w = max(168, min(258, int(card_width)))
        self.setFixedWidth(card_w)
        narrow_top = card_w < 246
        compact = card_w <= 214
        ultra_compact = card_w <= 180
        h_pad = 5 if ultra_compact else (6 if compact else (8 if narrow_top else 10))
        v_pad = 5 if ultra_compact else (6 if compact else 7)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(h_pad, v_pad, h_pad, v_pad)
        lay.setSpacing(3 if compact else 4)

        pct = item.score.percentile
        stat_tooltip = ""
        if pct is not None and item.score.games >= 30:
            z_txt = f" · z={item.score.z_score:+.2f}" if item.score.z_score is not None else ""
            stat_tooltip = f"同阶百分位 P{pct:.0f}{z_txt}"

        # Anchor 1 (Top row): Tier + HexScore on left, Shift Pill (or compact delta) on right
        top = QHBoxLayout()
        top.setSpacing(2 if compact else (3 if narrow_top else 4))
        if u is not None and u.overridden_by_combo:
            tier_text = "◈反超首选" if ultra_compact else ("◈ 反超首选" if narrow_top else "◈ 联动反超首选")
            t_pad = "1px 2px" if ultra_compact else ("1px 3px" if compact else ("1px 4px" if narrow_top else "1px 5px"))
            t_fs = "9px" if compact else "11px"
            t_lbl = QLabel(tier_text)
            t_lbl.setStyleSheet(
                "background-color: rgba(10, 200, 185, 42); border: 1px solid #0AC8B9; "
                f"border-radius: 2px; padding: {t_pad}; color: #F0E6D2; "
                f"font-weight: bold; font-size: {t_fs};"
            )
        else:
            if item.is_best:
                tier_text = f"◈{item.score.tier}首选" if ultra_compact else f"◈ {item.score.tier} 首选"
            else:
                tier_text = item.score.tier
            t_lbl = QLabel(tier_text)
            t_qss = tier_badge_qss(item.score.tier)
            if ultra_compact:
                t_qss = f"{t_qss} padding: 1px 2px; font-size: 9px;"
            elif compact:
                t_qss = f"{t_qss} padding: 1px 3px; font-size: 10px;"
            elif narrow_top:
                t_qss = f"{t_qss} padding: 1px 4px; font-size: 11px;"
            t_lbl.setStyleSheet(t_qss)
        top.addWidget(t_lbl)

        if u is not None and item.score.games >= 30:
            hs_color = (
                "#F0E6D2"
                if (item.is_best or u.final_hex_score >= 78.0)
                else ("#FCA5A5" if u.final_hex_score < 45.0 else "#C8AA6E")
            )
            hs_text = f"{u.final_hex_score:.1f}" if ultra_compact else f"{u.final_hex_score:.1f}分"
            hs_fs = "9px" if ultra_compact else ("10px" if compact else ("11px" if narrow_top else "12px"))
            hs_lbl = QLabel(hs_text)
            hs_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {hs_color}; "
                f"font-weight: bold; font-size: {hs_fs};"
            )
            hs_tip = f"综合分 {u.final_hex_score:.1f}（基础 {u.base_hex_score:.1f}）"
            if stat_tooltip:
                hs_tip = f"{hs_tip} · {stat_tooltip}"
            hs_lbl.setToolTip(hs_tip)
            top.addWidget(hs_lbl)

        top.addStretch(1)

        delta_pct = item.score.delta * 100.0
        if u is not None and u.shift_label:
            if u.shift_kind == "synergy":
                pill_bg, pill_fg = "rgba(6, 48, 56, 215)", "#0AC8B9"
            elif u.shift_kind == "gamble":
                pill_bg, pill_fg = "rgba(56, 36, 8, 215)", "#F59E0B"
            elif u.shift_kind == "conflict":
                pill_bg, pill_fg = "rgba(68, 16, 22, 215)", "#FCA5A5"
            else:
                pill_bg, pill_fg = "rgba(20, 32, 48, 215)", "#C8AA6E"
            p_pad = "1px 2px" if ultra_compact else ("1px 3px" if compact else ("1px 4px" if narrow_top else "1px 5px"))
            p_fs = "9px" if compact else "10px"
            d_lbl = QLabel(u.shift_label)
            d_lbl.setStyleSheet(
                f"background-color: {pill_bg}; border: 1px solid {pill_fg}; "
                f"border-radius: 2px; padding: {p_pad}; color: {pill_fg}; "
                f"font-weight: bold; font-size: {p_fs};"
            )
            delta_tip = f"单卡胜率差 {delta_pct:+.1f}%"
            if stat_tooltip:
                delta_tip = f"{delta_tip} · {stat_tooltip}"
            d_lbl.setToolTip(delta_tip)
        else:
            if pct is not None:
                d_color = (
                    "#0AC8B9"
                    if (pct >= 65.0 or delta_pct >= 0.5)
                    else ("#FCA5A5" if item.score.is_below_rarity_par else "#F0E6D2")
                )
            else:
                d_color = "#0AC8B9" if delta_pct >= 0.5 else ("#FCA5A5" if delta_pct <= -1.5 else "#F0E6D2")
            d_fs = "11px" if compact else "13px"
            d_lbl = QLabel(f"{delta_pct:+.1f}%")
            d_lbl.setStyleSheet(f"border: none; background: transparent; color: {d_color}; font-weight: bold; font-size: {d_fs};")
            if stat_tooltip:
                d_lbl.setToolTip(stat_tooltip)

        top.addWidget(d_lbl)
        lay.addLayout(top)

        # Anchor 2 (Mid row): Clean 2-item strip (raw win rate + compact sample; percentile moved to tooltip)
        mid = QHBoxLayout()
        wr_text = f"胜率 {item.score.raw_win_rate * 100:.1f}%" if item.score.games >= 30 else "样本不足"
        wr_lbl = QLabel(wr_text)
        wr_lbl.setStyleSheet("border: none; background: transparent; font-weight: bold; font-size: 12px; color: #F0E6D2;")
        g_lbl = QLabel(_compact_sample_text(item.score))
        g_lbl.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-size: 11px;"
            if item.score.low_sample
            else "border: none; background: transparent; color: #A09B8C; font-size: 11px;"
        )
        if stat_tooltip:
            wr_lbl.setToolTip(stat_tooltip)
            g_lbl.setToolTip(stat_tooltip)
            self.setToolTip(stat_tooltip)
        mid.addWidget(wr_lbl)
        mid.addStretch(1)
        mid.addWidget(g_lbl)
        lay.addLayout(mid)

        # Single prioritized footer line: destiny_items > is_transmute > shift_reason > archetype_hint > uncertain
        if item.destiny_items:
            top_d = " / ".join(f"{d.name}({d.delta * 100:+.1f}%)" for d in item.destiny_items[:2])
            d_hint = QLabel(f"◈ 命运绑定：{top_d}")
            d_hint.setWordWrap(True)
            d_hint.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-size: 11px;")
            lay.addWidget(d_hint)
        elif item.is_transmute:
            tx_lbl = QLabel("⚡ 质变符文 · 选后自动二次识别")
            tx_lbl.setStyleSheet(
                "background-color: rgba(58, 20, 74, 210); border: 1px solid #D946EF; "
                "border-radius: 2px; padding: 2px 6px; color: #F5D0FE; font-size: 10px; font-weight: bold;"
            )
            lay.addWidget(tx_lbl)
        elif u is not None and u.shift_reason:
            if u.shift_kind == "conflict":
                sr_prefix, sr_bg, sr_bd, sr_fg = "▽ ", "rgba(68, 16, 22, 210)", "#DC2626", "#FECACA"
            elif u.shift_kind == "gamble":
                sr_prefix, sr_bg, sr_bd, sr_fg = "◈ ", "rgba(56, 36, 8, 210)", "#F59E0B", "#FDE68A"
            else:
                sr_prefix, sr_bg, sr_bd, sr_fg = "◈ ", "rgba(8, 42, 54, 210)", "#0AC8B9", "#A5F3FC"
            sr_lbl = QLabel(f"{sr_prefix}{u.shift_reason}")
            sr_lbl.setWordWrap(True)
            sr_lbl.setToolTip(u.shift_reason)
            sr_lbl.setStyleSheet(
                f"background-color: {sr_bg}; border: 1px solid {sr_bd}; "
                f"border-radius: 2px; padding: 2px 6px; color: {sr_fg}; font-size: 10px; font-weight: bold;"
            )
            lay.addWidget(sr_lbl)
        elif item.archetype_hint:
            sp_lbl = QLabel(f"⚡ {item.archetype_hint}")
            sp_lbl.setWordWrap(True)
            sp_lbl.setStyleSheet(
                "background-color: rgba(8, 42, 54, 210); border: 1px solid #0AC8B9; "
                "border-radius: 2px; padding: 2px 6px; color: #A5F3FC; font-size: 10px; font-weight: bold;"
            )
            lay.addWidget(sp_lbl)
        elif item.uncertain:
            u_lbl = QLabel("◇ 识别待确认 · 按 F2 重扫")
            u_lbl.setStyleSheet("border: none; background: transparent; color: #FDBA74; font-size: 11px;")
            lay.addWidget(u_lbl)

        if item.is_best and animate_sheen:
            anim = QVariantAnimation(self)
            anim.setDuration(460)
            anim.setStartValue(-0.35)
            anim.setEndValue(1.35)
            anim.setEasingCurve(QEasingCurve.Type.OutQuad)
            anim.valueChanged.connect(self._on_sheen)
            anim.start()
            self._sheen_anim = anim

    def _on_sheen(self, val) -> None:
        self._sheen_t = float(val)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._is_best or self._sheen_t <= -0.3 or self._sheen_t >= 1.3:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        cx = w * self._sheen_t
        band = w * 0.28
        grad = QLinearGradient(cx - band, 0.0, cx + band, h)
        grad.setColorAt(0.0, QColor(245, 226, 184, 0))
        grad.setColorAt(0.5, QColor(245, 226, 184, 64))
        grad.setColorAt(1.0, QColor(245, 226, 184, 0))
        p.fillRect(QRectF(2.0, 2.0, w - 4.0, h - 4.0), QBrush(grad))
        p.end()


class AugmentBadgesOverlay(QWidget):
    """Tight-cropped transparent click-through layer placing rating cards directly above in-game cards."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(BASE_OVERLAY_QSS)

        self._badges: list[QWidget] = []
        self._card_anims: list[QPropertyAnimation] = []
        self._coach_banner: QFrame | None = None
        self._coach_banner_lbl: QLabel | None = None
        self._coach_banner_shimmer: HextechShimmerBar | None = None
        self._exclude_rects: list[tuple[int, int, int, int]] = []
        self._last_card_sig: tuple[tuple[int, str, bool], ...] = ()
        self._last_render_sig: tuple | None = None

    def get_exclude_rects(self) -> list[tuple[int, int, int, int]]:
        if not self.isVisible():
            return []
        return list(self._exclude_rects)

    def clear_badges(self) -> None:
        for anim in self._card_anims:
            anim.stop()
        self._card_anims.clear()
        for w in self._badges:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self._badges.clear()
        if self._coach_banner_shimmer is not None:
            self._coach_banner_shimmer.set_active(False)
            self._coach_banner_shimmer = None
        if self._coach_banner is not None:
            self._coach_banner.hide()
            self._coach_banner.setParent(None)
            self._coach_banner.deleteLater()
            self._coach_banner = None
            self._coach_banner_lbl = None
        self._exclude_rects.clear()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._last_card_sig = ()
        self._last_render_sig = None
        if self._coach_banner_shimmer is not None:
            self._coach_banner_shimmer.set_active(False)
        super().hideEvent(event)

    @staticmethod
    def compute_badge_width(logical_screen_w: int) -> int:
        if logical_screen_w >= 1680:
            return 258
        if logical_screen_w >= 1440:
            return 226
        return max(196, min(214, int(logical_screen_w * 0.162)))

    def render_cards(
        self,
        items: list[AugmentBadgeRenderItem],
        screen_rect: tuple[int, int, int, int],
        coach_augment_tip: str = "",
        coach_pending: bool = False,
        reroll_banner: str = "",
        gate_skipped_hint: str = "",
    ) -> None:
        render_sig = (
            screen_rect,
            coach_augment_tip,
            bool(coach_pending),
            reroll_banner,
            gate_skipped_hint,
            tuple(
                (
                    it.slot_index,
                    it.score.augment_id,
                    it.score.tier,
                    round(it.score.delta, 4),
                    round(it.score.raw_win_rate, 4),
                    it.score.games,
                    it.score.rarity,
                    it.screen_box,
                    it.is_best,
                    tuple((d.item_id, round(d.delta, 4)) for d in it.destiny_items),
                    it.uncertain,
                    it.archetype_hint,
                    it.is_transmute,
                    (
                        round(it.unified.final_hex_score, 1),
                        it.unified.shift_kind,
                        it.unified.shift_label,
                        it.unified.shift_reason,
                        it.unified.overridden_by_combo,
                    )
                    if it.unified is not None
                    else None,
                )
                for it in items
            ),
        )
        if self.isVisible() and render_sig == self._last_render_sig:
            return

        new_sig = tuple((it.slot_index, it.score.augment_id, it.is_best) for it in items)
        self._last_card_sig = new_sig
        self._last_render_sig = render_sig

        self.clear_badges()
        sl, st, sw, sh = screen_rect
        log_rect, dpr = physical_rect_to_logical(screen_rect)
        log_w = max(320, log_rect.width())
        log_h = max(240, log_rect.height())

        badge_w = self.compute_badge_width(log_w)
        # Auto-tighten badge width on 4-card layouts (or narrow spacing) so adjacent badges never overlap
        if len(items) >= 2:
            centers_log = sorted(
                ((it.screen_box[0] + it.screen_box[2]) * 0.5 - sl) / dpr for it in items
            )
            gaps = [centers_log[i + 1] - centers_log[i] for i in range(len(centers_log) - 1)]
            min_gap_log = min(gaps) if gaps else float(badge_w + 20)
            if len(items) >= 4 or min_gap_log < badge_w + 10:
                badge_w = min(badge_w, max(168, int(min_gap_log) - 10))
        min_badge_top = log_h
        placed: list[tuple[QWidget, int, int, int, int]] = []

        for item in items:
            # Zero 60fps sheen/slide animation on in-game layered overlay -> zero DWM UpdateLayeredWindow flood
            card = _SingleBadgeCard(item, animate_sheen=False, card_width=badge_w)
            card.setParent(self)
            card.adjustSize()

            box_l, box_t, box_r, _ = item.screen_box
            center_x_phys = (box_l + box_r) * 0.5 - sl
            top_y_phys = box_t - st
            cx_log = int(round(center_x_phys / dpr))
            y_log = max(12, int(round(top_y_phys / dpr)) - card.height() - 18)
            x_log = max(8, min(log_w - card.width() - 8, cx_log - card.width() // 2))

            placed.append((card, x_log, y_log, card.width(), card.height()))
            self._badges.append(card)
            min_badge_top = min(min_badge_top, y_log)

            self._exclude_rects.append(
                (
                    int(sl + x_log * dpr) - 6,
                    int(st + y_log * dpr) - 6,
                    int(sl + (x_log + card.width()) * dpr) + 6,
                    int(st + (y_log + card.height()) * dpr) + 6,
                )
            )

        banner_text = ""
        is_reroll_warn = False
        is_gate_saved = False
        show_shimmer = False
        if coach_augment_tip:
            banner_text = f"◈ AI 参谋：{coach_augment_tip}"
        elif reroll_banner:
            banner_text = f"🎲 {reroll_banner}" + ("  ·  AI 复核中…" if coach_pending else "")
            is_reroll_warn = True
            show_shimmer = bool(coach_pending)
        elif coach_pending:
            banner_text = "◈ AI 参谋正结合敌我 10 人阵容实时推演..."
            show_shimmer = True
        elif gate_skipped_hint:
            # Paid/trial-only transparent cost-saving feedback (strictly empty for free/0-credit users)
            banner_text = gate_skipped_hint
            is_gate_saved = True

        if banner_text:
            banner = QFrame(self)
            if is_reroll_warn:
                banner.setStyleSheet(
                    "QFrame { background-color: rgba(66, 36, 8, 246); "
                    "border: 1px solid #F59E0B; border-left: 3px solid #FDE68A; border-radius: 2px; }"
                )
            elif is_gate_saved:
                banner.setStyleSheet(
                    "QFrame { background-color: rgba(5, 22, 34, 218); "
                    "border: 1px solid rgba(10, 200, 185, 130); border-left: 3px solid #09C269; border-radius: 2px; }"
                )
            else:
                banner.setStyleSheet(
                    "QFrame { background-color: rgba(4, 28, 38, 244); "
                    "border: 1px solid #0AC8B9; border-left: 3px solid #C8AA6E; border-radius: 2px; }"
                )
            max_banner_w = max(320, int(log_w * 0.72))
            max_lbl_w = max(290, max_banner_w - 28)
            banner.setMaximumWidth(max_banner_w)
            bl = QVBoxLayout(banner)
            bl.setContentsMargins(14, 5 if is_gate_saved else 6, 14, 4 if show_shimmer else 5)
            bl.setSpacing(3)

            lbl = QLabel(banner_text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(
                "border: none; background: transparent; color: #FEF3C7; font-weight: bold; font-size: 13px;"
                if is_reroll_warn
                else (
                    "border: none; background: transparent; color: #A7F3D0; font-weight: bold; font-size: 11px;"
                    if is_gate_saved
                    else (
                        "border: none; background: transparent; color: #F0E6D2; font-weight: bold; font-size: 13px;"
                        if coach_augment_tip
                        else "border: none; background: transparent; color: #A5F3FC; font-weight: bold; font-size: 12px;"
                    )
                )
            )
            lbl.ensurePolished()
            natural_w = lbl.fontMetrics().horizontalAdvance(banner_text) + 12
            target_lbl_w = min(max_lbl_w, max(220, natural_w))
            lbl.setFixedWidth(target_lbl_w)
            bl.addWidget(lbl)

            if show_shimmer:
                # Static accent bar (animated=False) eliminates 60fps UpdateLayeredWindow GPU/DWM upload flood
                shimmer = HextechShimmerBar(height=2, accent="#0AC8B9", animated=False)
                shimmer.set_active(True)
                bl.addWidget(shimmer)
                self._coach_banner_shimmer = shimmer

            banner.adjustSize()
            bx = max(12, min(log_w - banner.width() - 12, (log_w - banner.width()) // 2))
            by = max(10, min_badge_top - banner.height() - 10)
            placed.append((banner, bx, by, banner.width(), banner.height()))
            self._coach_banner = banner
            self._coach_banner_lbl = lbl
            self._exclude_rects.append(
                (
                    int(sl + bx * dpr) - 4,
                    int(st + by * dpr) - 4,
                    int(sl + (bx + banner.width()) * dpr) + 4,
                    int(st + (by + banner.height()) * dpr) + 4,
                )
            )

        if not placed:
            self._last_render_sig = None
            self.hide()
            return

        # Tight-crop layered surface strictly around rendered badges + banner (shrinks 2560x1440 14MB -> ~1.5MB!)
        pad = 6
        min_x = max(0, min(px for _, px, _, _, _ in placed) - pad)
        min_y = max(0, min(py for _, _, py, _, _ in placed) - pad)
        max_x = min(log_w, max(px + pw for _, px, _, pw, _ in placed) + pad)
        max_y = min(log_h, max(py + ph for _, _, py, _, ph in placed) + pad)
        bbox = QRect(
            log_rect.left() + min_x,
            log_rect.top() + min_y,
            max(16, max_x - min_x),
            max(16, max_y - min_y),
        )
        if self.geometry() != bbox:
            self.setGeometry(bbox)
        for widget, px, py, _pw, _ph in placed:
            widget.move(px - min_x, py - min_y)
            widget.show()

        show_no_activate(self, click_through=True)
