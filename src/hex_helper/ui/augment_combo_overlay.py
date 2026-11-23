"""Independent Dual-Mode LoL Hextech Floating Overlay for Augment Combos, Gambles & Anti-Synergy Downgrades.

Strictly decoupled from the Bayesian win-rate AugmentBadgesOverlay:
- Plan mode (normal in-game): compact left-edge pill <-> 304px roadmap showing chased combos,
  top follow-up targets to gamble/reroll for, and penalized conflict axes.
- Picker mode (during 3-card augment selection): auto-expands to diagnose all 3 live candidates
  (jackpot / gamble / synergy / neutral / conflict-downgrade + reroll hint), then cleanly restores
  prior collapsed/expanded state once selection closes.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QPoint, QRect, Qt, QVariantAnimation
from PySide6.QtGui import QAction, QGuiApplication, QMouseEvent, QScreen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from hex_helper.config import HotkeyConfig
from hex_helper.scoring.augment_combo import (
    ComboPlan,
    PickerCardComboAdvice,
    PickerComboVerdict,
)
from hex_helper.ui.hextech_widgets import HextechActionButton, HextechPulseFrame
from hex_helper.ui.resizable_overlay import FramelessResizeController, HextechCornerGrip
from hex_helper.ui.theme import BASE_OVERLAY_QSS, RARITY_COLORS, display_font
from hex_helper.ui.win32_window import show_no_activate

_PROFILE_PILL_BADGE: dict[str, tuple[str, str]] = {
    "AP": ("AP", "#38BDF8"),
    "AD": ("AD", "#FBBF24"),
    "CRIT_SPELL": ("核爆", "#E879F9"),
    "TANK": ("坦", "#4ADE80"),
    "SUPPORT": ("辅", "#2DD4BF"),
    "HYBRID": ("双修", "#C084FC"),
    "BALANCED": ("联", "#C8AA6E"),
}

_SLOT_LABELS_3: tuple[str, ...] = ("左", "中", "右")
_SLOT_LABELS_4: tuple[str, ...] = ("左1", "中左", "中右", "右4")
_SLOT_LABELS: tuple[str, ...] = _SLOT_LABELS_3


def _clip(text: str, max_len: int) -> str:
    s = " ".join((text or "").strip().split())
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _clear_layout(layout) -> None:
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            _clear_layout(it.layout())


class AugmentComboOverlay(QWidget):
    """Independent left-edge Hextech dual-mode augment combo & anti-synergy floating HUD."""

    COLLAPSED_W = 48
    COLLAPSED_MIN_H = 152
    EXPANDED_W = 316
    MIN_EXPANDED_W = 236
    MAX_EXPANDED_W = 540
    EXPANDED_MIN_H = 180
    _QWIDGETSIZE_MAX = 16777215

    def __init__(
        self,
        *,
        on_pos_changed: Callable[[list[int]], None] | None = None,
        on_size_changed: Callable[[list[int] | None], None] | None = None,
        on_closed_by_user: Callable[[], None] | None = None,
        initial_pos: list[int] | None = None,
        initial_size: list[int] | None = None,
        y_ratio: float = 0.26,
    ) -> None:
        super().__init__()
        self._on_pos_changed = on_pos_changed
        self._on_size_changed = on_size_changed
        self._on_closed_by_user = on_closed_by_user
        self._y_ratio = y_ratio
        self._target_screen: QScreen | None = None
        self._custom_pos: QPoint | None = (
            QPoint(int(initial_pos[0]), int(initial_pos[1]))
            if (initial_pos and len(initial_pos) == 2)
            else None
        )
        self._custom_w: int | None = (
            max(self.MIN_EXPANDED_W, min(self.MAX_EXPANDED_W, int(initial_size[0])))
            if (initial_size and len(initial_size) >= 1 and int(initial_size[0]) > 0)
            else None
        )
        self._custom_h: int | None = (
            max(self.EXPANDED_MIN_H, int(initial_size[1]))
            if (initial_size and len(initial_size) >= 2 and int(initial_size[1]) > 0)
            else None
        )
        self._last_rendered_w: int = self._custom_w or self.EXPANDED_W
        self._drag_offset: QPoint | None = None
        self._anchor_left: bool = self._infer_initial_anchor_left(self._custom_pos)
        self._expanded: bool = False
        self._pre_picker_expanded: bool | None = None
        self._mode: str = "plan"  # "plan" | "picker"
        self._full_locked: bool = False
        self._last_plan: ComboPlan | None = None
        self._last_verdict: PickerComboVerdict | None = None
        self._picker_rows: list[QWidget] = []
        self._last_combo_pulse_sig: str = ""
        self._slide_anim = QVariantAnimation(self)
        self._slide_anim.setDuration(170)
        self._slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide_anim.valueChanged.connect(self._on_slide_width)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(BASE_OVERLAY_QSS)

        root_lay = QHBoxLayout(self)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        # === Collapsed Compact Pill (48px left-edge Hextech capsule) ===
        self.pill = QFrame()
        self.pill.setObjectName("PanelRoot")
        self.pill.setFixedWidth(self.COLLAPSED_W)
        self.pill.setMinimumHeight(self.COLLAPSED_MIN_H)
        self.pill.setToolTip("海克斯联动建议 · 双击或点「展」展开 · F7 开关 · 拖动调位")
        pill_lay = QVBoxLayout(self.pill)
        pill_lay.setContentsMargins(4, 4, 4, 6)
        pill_lay.setSpacing(4)

        self.pill_close_btn = QPushButton("×")
        self.pill_close_btn.setFixedSize(18, 16)
        self.pill_close_btn.setToolTip("隐藏海克斯组合悬浮窗（F7 重开）")
        self.pill_close_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 13px; color: #A09B8C; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 40); }"
        )
        self.pill_close_btn.clicked.connect(self._close_by_user)

        self.pill_crest_lbl = QLabel("联")
        self.pill_crest_lbl.setFixedSize(34, 34)
        self.pill_crest_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pill_crest_lbl.setFont(display_font(11, bold=True))
        self.pill_crest_lbl.setStyleSheet(
            "background-color: #091428; border: 2px solid #C8AA6E; border-radius: 17px; "
            "color: #F0E6D2; font-weight: bold;"
        )

        self.pill_tag_lbl = QLabel("组合")
        self.pill_tag_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pill_tag_lbl.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-size: 9px; font-weight: bold;"
        )

        self.pill_status_dot = QLabel("●")
        self.pill_status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pill_status_dot.setStyleSheet(
            "border: none; background: transparent; color: #5B5A56; font-size: 9px;"
        )

        self.pill_expand_btn = HextechActionButton("展", variant="cyan", height=24)
        self.pill_expand_btn.setFixedWidth(36)
        self.pill_expand_btn.setMinimumWidth(36)
        self.pill_expand_btn.clicked.connect(lambda: self.set_expanded(True))

        pill_lay.addWidget(self.pill_close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        pill_lay.addWidget(self.pill_crest_lbl, alignment=Qt.AlignmentFlag.AlignCenter)
        pill_lay.addWidget(self.pill_tag_lbl)
        pill_lay.addWidget(self.pill_status_dot)
        pill_lay.addWidget(self.pill_expand_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        root_lay.addWidget(self.pill)

        # === Expanded Dual-Mode Panel (304px Hextech HUD) ===
        self.panel = QFrame()
        self.panel.setObjectName("PanelRoot")
        self.panel.setFixedWidth(self.EXPANDED_W)
        panel_lay = QVBoxLayout(self.panel)
        panel_lay.setContentsMargins(10, 8, 10, 8)
        panel_lay.setSpacing(6)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(5)
        self.hdr_title_lbl = QLabel("◈ 海克斯联动")
        self.hdr_title_lbl.setFont(display_font(11, bold=True, letter_spacing=0.4))
        self.hdr_title_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")

        self.profile_badge_lbl = QLabel("待定流派")
        self.profile_badge_lbl.setStyleSheet(
            "background-color: rgba(10, 200, 185, 36); border: 1px solid #0AC8B9; "
            "border-radius: 2px; padding: 1px 6px; color: #A5F3FC; font-size: 10px; font-weight: bold;"
        )

        self.collapse_btn = HextechActionButton("收起", variant="gold", height=22)
        self.collapse_btn.setMinimumWidth(38)
        self.collapse_btn.clicked.connect(lambda: self.set_expanded(False))

        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(18, 18)
        self.close_btn.setToolTip("隐藏海克斯组合悬浮窗（F7 重开）")
        self.close_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 14px; color: #A09B8C; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); }"
        )
        self.close_btn.clicked.connect(self._close_by_user)

        hdr.addWidget(self.hdr_title_lbl)
        hdr.addWidget(self.profile_badge_lbl)
        hdr.addStretch(1)
        hdr.addWidget(self.collapse_btn)
        hdr.addWidget(self.close_btn)
        panel_lay.addLayout(hdr)

        # Scrollable body container so compact DPI screens never clip cards
        self.panel_scroll = QScrollArea()
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.panel_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.panel_scroll.viewport().setStyleSheet("background: transparent;")
        self.panel_body = QWidget()
        self.panel_body.setObjectName("ComboScrollBody")
        self.panel_body.setStyleSheet("QWidget#ComboScrollBody { background: transparent; border: none; }")
        body_lay = QVBoxLayout(self.panel_body)
        body_lay.setContentsMargins(0, 0, 2, 0)
        body_lay.setSpacing(6)
        self.panel_scroll.setWidget(self.panel_body)
        panel_lay.addWidget(self.panel_scroll, 1)

        # --- PLAN MODE CONTAINER ---
        self.plan_wrap = QWidget()
        self.plan_wrap.setObjectName("ComboPlanWrap")
        self.plan_wrap.setStyleSheet("QWidget#ComboPlanWrap { background: transparent; border: none; }")
        plan_lay = QVBoxLayout(self.plan_wrap)
        plan_lay.setContentsMargins(0, 0, 0, 0)
        plan_lay.setSpacing(6)
        plan_lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.active_combo_card = HextechPulseFrame()
        self.active_combo_card.setStyleSheet(
            "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(8, 34, 48, 240), stop:1 rgba(10, 20, 34, 240)); "
            "border: 1px solid #C8AA6E; border-left: 3px solid #0AC8B9; border-radius: 2px; }"
        )
        ac_lay = QVBoxLayout(self.active_combo_card)
        ac_lay.setContentsMargins(8, 6, 8, 6)
        ac_lay.setSpacing(3)
        self.active_combo_hdr = QLabel("◈ 当前联动方向")
        self.active_combo_hdr.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-size: 10px; font-weight: bold;"
        )
        self.active_combo_body = QLabel("进局选定英雄后展示招牌核爆与可赌组合")
        self.active_combo_body.setWordWrap(True)
        self.active_combo_body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.active_combo_body.setStyleSheet(
            "border: none; background: transparent; color: #E2E8F0; font-size: 11px;"
        )
        ac_lay.addWidget(self.active_combo_hdr)
        ac_lay.addWidget(self.active_combo_body)
        plan_lay.addWidget(self.active_combo_card)

        self.targets_card = QFrame()
        self.targets_card.setObjectName("SubCard")
        t_lay = QVBoxLayout(self.targets_card)
        t_lay.setContentsMargins(8, 5, 8, 6)
        t_lay.setSpacing(4)
        self.targets_hdr = QLabel("◈ 值得追 / 留骰子赌的搭配")
        self.targets_hdr.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-size: 10px; font-weight: bold;"
        )
        t_lay.addWidget(self.targets_hdr)
        self.targets_list_wrap = QWidget()
        self.targets_list_wrap.setObjectName("ComboTargetsWrap")
        self.targets_list_wrap.setStyleSheet("QWidget#ComboTargetsWrap { background: transparent; border: none; }")
        self.targets_list_lay = QVBoxLayout(self.targets_list_wrap)
        self.targets_list_lay.setContentsMargins(0, 0, 0, 0)
        self.targets_list_lay.setSpacing(4)
        t_lay.addWidget(self.targets_list_wrap)
        plan_lay.addWidget(self.targets_card)

        self.avoid_banner = QFrame()
        self.avoid_banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.avoid_banner.setStyleSheet(
            "QFrame { background-color: rgba(88, 18, 24, 225); border: 1px solid #DC2626; "
            "border-left: 3px solid #F87171; border-radius: 2px; }"
        )
        ab_lay = QHBoxLayout(self.avoid_banner)
        ab_lay.setContentsMargins(8, 4, 8, 4)
        self.avoid_lbl = QLabel("")
        self.avoid_lbl.setWordWrap(True)
        self.avoid_lbl.setStyleSheet(
            "border: none; background: transparent; color: #FECACA; font-size: 10px; font-weight: bold;"
        )
        ab_lay.addWidget(self.avoid_lbl)
        self.avoid_banner.hide()
        plan_lay.addWidget(self.avoid_banner)

        body_lay.addWidget(self.plan_wrap)

        # --- PICKER DIAGNOSIS MODE CONTAINER ---
        self.picker_wrap = QWidget()
        self.picker_wrap.setObjectName("ComboPickerWrap")
        self.picker_wrap.setStyleSheet("QWidget#ComboPickerWrap { background: transparent; border: none; }")
        pk_lay = QVBoxLayout(self.picker_wrap)
        pk_lay.setContentsMargins(0, 0, 0, 0)
        pk_lay.setSpacing(5)
        pk_lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.picker_cards_wrap = QWidget()
        self.picker_cards_wrap.setObjectName("ComboPickerCardsWrap")
        self.picker_cards_wrap.setStyleSheet("QWidget#ComboPickerCardsWrap { background: transparent; border: none; }")
        self.picker_cards_lay = QVBoxLayout(self.picker_cards_wrap)
        self.picker_cards_lay.setContentsMargins(0, 0, 0, 0)
        self.picker_cards_lay.setSpacing(5)
        pk_lay.addWidget(self.picker_cards_wrap)

        self.reroll_banner = QFrame()
        self.reroll_banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.reroll_banner.setStyleSheet(
            "QFrame { background-color: rgba(64, 40, 10, 232); border: 1px solid #F59E0B; "
            "border-left: 3px solid #FDE68A; border-radius: 2px; }"
        )
        rb_lay = QHBoxLayout(self.reroll_banner)
        rb_lay.setContentsMargins(8, 5, 8, 5)
        self.reroll_lbl = QLabel("")
        self.reroll_lbl.setWordWrap(True)
        self.reroll_lbl.setStyleSheet(
            "border: none; background: transparent; color: #FEF3C7; font-size: 10px; font-weight: bold;"
        )
        rb_lay.addWidget(self.reroll_lbl)
        self.reroll_banner.hide()
        pk_lay.addWidget(self.reroll_banner)

        self.picker_wrap.hide()
        body_lay.addWidget(self.picker_wrap)
        body_lay.addStretch(1)

        # Footer note emphasizing decoupling from top win-rate badges
        self.footer_lbl = QLabel("独立联动建议 · 不改上方胜率梯度 · F7 切换")
        self.footer_lbl.setStyleSheet(
            "border: none; background: transparent; color: #64748B; font-size: 9px;"
        )
        panel_lay.addWidget(self.footer_lbl)

        root_lay.addWidget(self.panel)
        self.panel.hide()

        self._grip_bl = HextechCornerGrip(self.panel, "bl")
        self._grip_br = HextechCornerGrip(self.panel, "br")
        self._resize_ctrl = FramelessResizeController(
            self,
            min_w=self.MIN_EXPANDED_W,
            max_w=self.MAX_EXPANDED_W,
            min_h=self.EXPANDED_MIN_H,
            max_h=1100,
            can_resize=lambda: self._expanded and not self._full_locked,
            on_live_resize=self._on_live_resize,
            on_resize_finished=self._on_resize_finished,
            screen_provider=self._active_screen,
        )
        self._resize_ctrl.attach(self.panel)
        self._resize_ctrl.attach(self.panel_scroll.viewport())
        self._resize_ctrl.attach(self._grip_bl)
        self._resize_ctrl.attach(self._grip_br)

        self._reposition()

    def _layout_corner_grips(self) -> None:
        pw = self.panel.width()
        ph = self.panel.height()
        if pw <= 20 or ph <= 20:
            return
        self._grip_bl.move(1, ph - self._grip_bl.height() - 1)
        self._grip_br.move(pw - self._grip_br.width() - 1, ph - self._grip_br.height() - 1)
        self._grip_bl.raise_()
        self._grip_br.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._expanded:
            self._layout_corner_grips()

    def _apply_header_density(self, w: int) -> None:
        compact = w <= 268
        if self._mode == "picker":
            self.hdr_title_lbl.setText("◈ 联动诊断" if compact else "◈ 三选一联动诊断")
        else:
            self.hdr_title_lbl.setText("◈ 联动" if compact else "◈ 海克斯联动")
        self.profile_badge_lbl.setVisible(not (w <= 248 and self._mode == "picker"))
        self.collapse_btn.setText("收" if w <= 258 else "收起")
        self.collapse_btn.setMinimumWidth(26 if w <= 258 else 38)

    def _on_live_resize(self, target: QRect, _edge: str) -> None:
        w = max(self.MIN_EXPANDED_W, min(self.MAX_EXPANDED_W, target.width()))
        h = max(self.EXPANDED_MIN_H, target.height())
        self._custom_w = w
        self._custom_h = h
        self._apply_header_density(w)
        self.setFixedWidth(w)
        self.setFixedHeight(h)
        self.panel.setFixedWidth(w)
        self.panel.setFixedHeight(h)
        self.panel_body.setFixedWidth(max(180, w - 22))
        self.setGeometry(target.x(), target.y(), w, h)
        self._layout_corner_grips()

        if abs(w - self._last_rendered_w) >= 28:
            self._last_rendered_w = w
            if self._mode == "picker" and self._last_verdict is not None:
                self._rebuild_picker_rows(self._last_verdict)
            elif self._last_plan is not None:
                self._rebuild_plan_targets(self._last_plan)

    def _on_resize_finished(self, target: QRect, _edge: str) -> None:
        self._custom_w = max(self.MIN_EXPANDED_W, min(self.MAX_EXPANDED_W, target.width()))
        self._custom_h = max(self.EXPANDED_MIN_H, target.height())
        if self._custom_w != self._last_rendered_w:
            self._last_rendered_w = self._custom_w
            if self._mode == "picker" and self._last_verdict is not None:
                self._rebuild_picker_rows(self._last_verdict)
            elif self._last_plan is not None:
                self._rebuild_plan_targets(self._last_plan)
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or self._active_screen()
        if screen is not None:
            self._target_screen = screen
            geo = screen.geometry()
            self._anchor_left = (self.x() - geo.left()) <= 28
            self._custom_pos = QPoint(geo.left(), self.y()) if self._anchor_left else self.pos()
        if self._on_size_changed:
            self._on_size_changed([self._custom_w, self._custom_h])
        if self._on_pos_changed and self._custom_pos is not None:
            self._on_pos_changed([self._custom_pos.x(), self._custom_pos.y()])

    def reset_default_size(self) -> None:
        self._custom_w = None
        self._custom_h = None
        self._reposition()
        if self._mode == "picker" and self._last_verdict is not None:
            self._rebuild_picker_rows(self._last_verdict)
        elif self._last_plan is not None:
            self._rebuild_plan_targets(self._last_plan)
        self._reposition()
        if self._on_size_changed:
            self._on_size_changed(None)

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    @property
    def mode(self) -> str:
        return self._mode

    def update_hotkey_labels(self, hk: HotkeyConfig) -> None:
        k_combo = (hk.toggle_combo or "").strip()
        k_sidebars = (hk.toggle_sidebars or "").strip()
        reopen = f"{k_combo} 重开" if k_combo else "可在托盘重开"
        self.pill_close_btn.setToolTip(f"隐藏海克斯组合悬浮窗（{reopen}）")
        self.close_btn.setToolTip(f"隐藏海克斯组合悬浮窗（{reopen}）")
        parts = ["海克斯联动建议 · 双击或点「展」展开"]
        if k_combo:
            parts.append(f"{k_combo} 开关")
        if k_sidebars:
            parts.append(f"{k_sidebars} 双侧栏同步")
        parts.append("拖动调位")
        self.pill.setToolTip(" · ".join(parts))

    @classmethod
    def _infer_initial_anchor_left(cls, pos: QPoint | None) -> bool:
        if pos is None:
            return True
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return False
        geo = screen.geometry()
        return (pos.x() - geo.left()) <= 32

    @classmethod
    def compute_expanded_width(cls, logical_screen_w: int) -> int:
        if logical_screen_w >= 1680:
            return cls.EXPANDED_W
        if logical_screen_w >= 1440:
            return 268
        return 244

    def set_target_screen(self, screen: QScreen | None) -> None:
        if screen is not None and screen is not self._target_screen:
            self._target_screen = screen
            self._reposition()

    def _active_screen(self) -> QScreen | None:
        if self._target_screen is not None and self._target_screen in QGuiApplication.screens():
            return self._target_screen
        return QGuiApplication.primaryScreen()

    def _reposition(self, *, override_logical_width: int | None = None) -> None:
        screen = self._active_screen()
        if screen is None:
            return
        geo = screen.geometry()
        eff_screen_w = int(override_logical_width) if override_logical_width else geo.width()
        if self._expanded:
            auto_w = self.compute_expanded_width(eff_screen_w)
            w = (
                min(self._custom_w, max(self.MIN_EXPANDED_W, geo.width() - 24))
                if (self._custom_w is not None and override_logical_width is None)
                else auto_w
            )
        else:
            w = self.COLLAPSED_W
        self.setFixedWidth(w)
        if self._expanded:
            self._apply_header_density(w)
            self.panel.setMinimumHeight(0)
            self.panel.setMaximumHeight(self._QWIDGETSIZE_MAX)
            self.panel.setFixedWidth(w)
            inner_w = max(180, w - 22)
            self.panel_body.setFixedWidth(inner_w)
            for row_w in self._picker_rows:
                rl = row_w.layout()
                if rl is not None:
                    rl.invalidate()
                    rl.activate()
            for child_wrap in (
                self.targets_list_wrap,
                self.targets_card,
                self.plan_wrap,
                self.picker_cards_wrap,
                self.picker_wrap,
                self.panel_body,
            ):
                cl = child_wrap.layout()
                if cl is not None:
                    cl.invalidate()
                    cl.activate()
            lay = self.panel.layout()
            if lay is not None:
                lay.invalidate()
                lay.activate()
            if self._custom_h is not None and override_logical_width is None:
                h = min(geo.height() - 32, max(self.EXPANDED_MIN_H, self._custom_h))
            else:
                active_wrap = self.picker_wrap if self._mode == "picker" else self.plan_wrap
                active_h_w = active_wrap.heightForWidth(inner_w)
                active_hint = max(active_h_w if active_h_w > 0 else 0, active_wrap.sizeHint().height())
                if self._mode == "picker" and self._picker_rows:
                    row_sum = sum(
                        max(r.heightForWidth(inner_w), r.sizeHint().height(), 46)
                        for r in self._picker_rows
                    ) + 5 * max(0, len(self._picker_rows) - 1)
                    if not self.reroll_banner.isHidden():
                        row_sum += max(self.reroll_banner.heightForWidth(inner_w), self.reroll_banner.sizeHint().height(), 28) + 5
                    active_hint = max(active_hint, row_sum)
                body_h_for_w = self.panel_body.heightForWidth(inner_w)
                body_hint_h = max(body_h_for_w if body_h_for_w > 0 else 0, self.panel_body.sizeHint().height(), active_hint)
                chrome_h = 62  # header + footer + vertical margins/spacing
                h = min(geo.height() - 64, max(self.EXPANDED_MIN_H, chrome_h + body_hint_h + 12))
            self.panel.setFixedHeight(h)
        else:
            self.pill.setMinimumHeight(0)
            self.pill.setMaximumHeight(self._QWIDGETSIZE_MAX)
            self.pill.setFixedWidth(w)
            play = self.pill.layout()
            if play is not None:
                play.invalidate()
                play.activate()
            hint_h = max(self.COLLAPSED_MIN_H, self.pill.sizeHint().height())
            h = min(geo.height() - 64, hint_h + 4)
            self.pill.setFixedHeight(h)
        self.setFixedHeight(h)

        if self._custom_pos is not None and not self._anchor_left:
            x = max(geo.left(), min(geo.right() - w + 1, self._custom_pos.x()))
            y = max(geo.top(), min(geo.bottom() - h + 1, self._custom_pos.y()))
        else:
            y_base = (
                self._custom_pos.y()
                if self._custom_pos is not None
                else int(geo.top() + geo.height() * self._y_ratio)
            )
            y = max(geo.top() + 16, min(geo.bottom() - h - 16, y_base))
            x = geo.left()
        self.setGeometry(x, y, w, h)
        if self._expanded:
            self._layout_corner_grips()

    def _close_by_user(self) -> None:
        self.hide()
        if self._on_closed_by_user:
            self._on_closed_by_user()

    def reset_to_left_edge(self) -> None:
        self._custom_pos = None
        self._anchor_left = True
        self._reposition()
        if self._on_pos_changed:
            self._on_pos_changed([self.x(), self.y()])

    def _on_slide_width(self, val) -> None:
        w = max(self.COLLAPSED_W, int(val))
        self._reposition(override_logical_width=w)

    def _resolved_expanded_width(self) -> int:
        screen = self._active_screen()
        geo = screen.geometry() if screen is not None else None
        eff_w = geo.width() if geo is not None else 1920
        auto_w = self.compute_expanded_width(eff_w)
        if self._custom_w is not None and geo is not None:
            return min(self._custom_w, max(self.MIN_EXPANDED_W, geo.width() - 24))
        return self._custom_w or auto_w

    def set_expanded(self, expanded: bool) -> None:
        was_visible = self.isVisible()
        start_w = self.width() if was_visible else (self.COLLAPSED_W if expanded else self._resolved_expanded_width())
        target_w = self._resolved_expanded_width() if expanded else self.COLLAPSED_W
        self._expanded = bool(expanded)
        self.pill.setVisible(not self._expanded)
        self.panel.setVisible(self._expanded)
        self._reposition()
        if was_visible:
            if start_w != target_w:
                self._slide_anim.stop()
                self._slide_anim.setStartValue(start_w)
                self._slide_anim.setEndValue(target_w)
                self._slide_anim.start()
            show_no_activate(self, click_through=False)

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def show_overlay(self) -> None:
        if self._full_locked:
            self.hide()
            return
        self._reposition()
        show_no_activate(self, click_through=False)

    def set_full_locked(self, locked: bool, reason: str = "") -> None:
        self._full_locked = bool(locked)
        if self._full_locked and self.isVisible():
            self.hide()

    def get_exclude_rect(self) -> tuple[int, int, int, int] | None:
        """Return physical screen bounding box when visible so OCR never reads self text."""
        if not self.isVisible():
            return None
        dpr = float(self.devicePixelRatioF() or 1.0)
        geo = self.frameGeometry()
        return (
            int(geo.left() * dpr) - 6,
            int(geo.top() * dpr) - 6,
            int(geo.right() * dpr) + 6,
            int(geo.bottom() * dpr) + 6,
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if (not self._expanded) or event.position().y() <= 38:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        elif event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            a_toggle = QAction("收起成小条" if self._expanded else "展开联动面板", menu)
            a_toggle.triggered.connect(self.toggle_expanded)
            a_reset_size = QAction("恢复默认尺寸", menu)
            a_reset_size.triggered.connect(self.reset_default_size)
            a_reset = QAction("复位到屏幕左侧", menu)
            a_reset.triggered.connect(self.reset_to_left_edge)
            a_hide = QAction("隐藏海克斯组合悬浮窗", menu)
            a_hide.triggered.connect(self._close_by_user)
            menu.addAction(a_toggle)
            menu.addAction(a_reset_size)
            menu.addAction(a_reset)
            menu.addSeparator()
            menu.addAction(a_hide)
            menu.exec(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self._expanded:
            self._drag_offset = None
            self.set_expanded(True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            screen = QGuiApplication.screenAt(event.globalPosition().toPoint()) or self._active_screen()
            pos = event.globalPosition().toPoint() - self._drag_offset
            if screen is not None:
                geo = screen.geometry()
                pos.setX(max(geo.left(), min(geo.right() - self.width() + 1, pos.x())))
                pos.setY(max(geo.top(), min(geo.bottom() - self.height() + 1, pos.y())))
            self.move(pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            screen = QGuiApplication.screenAt(self.frameGeometry().center()) or self._active_screen()
            if screen is not None:
                self._target_screen = screen
                geo = screen.geometry()
                self._anchor_left = (self.x() - geo.left()) <= 28
                if self._anchor_left:
                    self.move(geo.left(), self.y())
                    self._custom_pos = QPoint(geo.left(), self.y())
                else:
                    self._custom_pos = self.pos()
            else:
                self._anchor_left = False
                self._custom_pos = self.pos()
            if self._on_pos_changed:
                self._on_pos_changed([self._custom_pos.x(), self._custom_pos.y()])
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_plan(self, plan: ComboPlan) -> None:
        """Update normal in-game combo plan & collapsed pill indicators."""
        self._last_plan = plan
        prof_short, prof_color = _PROFILE_PILL_BADGE.get(plan.profile.primary_profile, ("联", "#C8AA6E"))
        self.pill_crest_lbl.setText(prof_short)
        self.pill_crest_lbl.setStyleSheet(
            f"background-color: #091428; border: 2px solid {prof_color}; border-radius: 17px; "
            f"color: {prof_color}; font-weight: bold;"
        )

        if plan.active_combos:
            top_c = plan.active_combos[0]
            if top_c.completed:
                self.pill_tag_lbl.setText("已成")
                self.pill_status_dot.setStyleSheet("border: none; background: transparent; color: #09C269; font-size: 10px;")
            elif top_c.gamble:
                self.pill_tag_lbl.setText("赌核")
                self.pill_status_dot.setStyleSheet("border: none; background: transparent; color: #F59E0B; font-size: 10px;")
            else:
                self.pill_tag_lbl.setText("追组")
                self.pill_status_dot.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 10px;")
        elif plan.opening_combos:
            self.pill_tag_lbl.setText("开局")
            self.pill_status_dot.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-size: 9px;")
        else:
            self.pill_tag_lbl.setText("待机")
            self.pill_status_dot.setStyleSheet("border: none; background: transparent; color: #5B5A56; font-size: 9px;")

        self.profile_badge_lbl.setText(_clip(plan.profile.direction_label, 10))

        # Active / Opening summary card
        if plan.active_combos:
            lines: list[str] = []
            for c in plan.active_combos[:2]:
                status_tag = "✓已成型" if c.completed else ("🎲追赌" if c.gamble else "追核心")
                chase = f" → 追{'/'.join(c.missing_core_names[:2])}" if c.missing_core_names else ""
                lines.append(f"【{c.name}】{status_tag}{chase}")
            self.active_combo_hdr.setText(f"◈ {plan.hero_name} · 正在联动")
            self.active_combo_body.setText("\n".join(lines))
            pulse_sig = "|".join(f"{c.name}:{int(c.completed)}" for c in plan.active_combos[:2])
            if pulse_sig and pulse_sig != self._last_combo_pulse_sig:
                self._last_combo_pulse_sig = pulse_sig
                if hasattr(self, "active_combo_card"):
                    self.active_combo_card.trigger_pulse("#0AC8B9", 520)
        elif plan.opening_combos:
            lines = []
            for c in plan.opening_combos[:2]:
                tag = "🎲赌上限" if c.gamble else "推荐路线"
                cores = "+".join(c.missing_core_names[:2])
                lines.append(f"【{c.name}】{tag}（{cores}）")
            self.active_combo_hdr.setText(f"◈ {plan.hero_name} · 开局值得赌的组合")
            self.active_combo_body.setText("\n".join(lines))
        else:
            self.active_combo_hdr.setText("◈ 当前联动方向")
            self.active_combo_body.setText("进局选定英雄与海克斯后，实时推演后续搭配与降权方向")

        # Top follow-ups list
        self._rebuild_plan_targets(plan)

        if plan.avoid_summary:
            self.avoid_lbl.setText(f"▽ {plan.avoid_summary}")
            self.avoid_banner.show()
        else:
            self.avoid_banner.hide()

        if self._mode == "plan":
            self._apply_header_density(self._custom_w or self.EXPANDED_W)
            self.plan_wrap.show()
            self.picker_wrap.hide()
            self._reposition()

    def _rebuild_plan_targets(self, plan: ComboPlan) -> None:
        _clear_layout(self.targets_list_lay)
        eff_w = self._custom_w or self.width() or self.EXPANDED_W
        reason_cap = max(24, int((eff_w - 36) / 6.2))
        if plan.recommended_targets:
            self.targets_hdr.setText(
                "◈ 后续最配 / 留骰子追" if plan.active_combos else "◈ 开局核心起手目标"
            )
            for t in plan.recommended_targets[:4]:
                rar_color, _ = RARITY_COLORS.get(t.rarity, ("#C89B3C", ""))
                row = QFrame()
                row.setStyleSheet(
                    f"QFrame {{ background-color: rgba(9, 20, 40, 225); "
                    f"border: 1px solid #334155; border-left: 3px solid {rar_color}; border-radius: 2px; }}"
                )
                rl = QVBoxLayout(row)
                rl.setContentsMargins(7, 4, 7, 4)
                rl.setSpacing(2)

                top_r = QHBoxLayout()
                top_r.setSpacing(4)
                nm_lbl = QLabel(t.name)
                nm_lbl.setStyleSheet(
                    f"border: none; background: transparent; color: {rar_color}; font-size: 11px; font-weight: bold;"
                )
                top_r.addWidget(nm_lbl)
                if t.gamble:
                    gb = QLabel("赌上限")
                    gb.setStyleSheet(
                        "background-color: rgba(245, 158, 11, 45); border: 1px solid #F59E0B; "
                        "border-radius: 2px; padding: 0px 4px; color: #FDE68A; font-size: 9px; font-weight: bold;"
                    )
                    top_r.addWidget(gb)
                if t.hero_signature:
                    hs = QLabel("绝配")
                    hs.setStyleSheet(
                        "background-color: rgba(10, 200, 185, 40); border: 1px solid #0AC8B9; "
                        "border-radius: 2px; padding: 0px 4px; color: #A5F3FC; font-size: 9px; font-weight: bold;"
                    )
                    top_r.addWidget(hs)
                top_r.addStretch(1)
                rl.addLayout(top_r)

                rs_lbl = QLabel(_clip(t.reason, reason_cap))
                rs_lbl.setWordWrap(True)
                rs_lbl.setToolTip(t.reason)
                rs_lbl.setStyleSheet(
                    "border: none; background: transparent; color: #CBD5E1; font-size: 10px;"
                )
                rl.addWidget(rs_lbl)
                self.targets_list_lay.addWidget(row)
                row.show()
        else:
            empty = QLabel("◇ 选定首个海克斯后自动列出最搭配目标")
            empty.setStyleSheet("border: none; background: transparent; color: #64748B; font-size: 10px;")
            self.targets_list_lay.addWidget(empty)
            empty.show()

    def _rebuild_picker_rows(self, verdict: PickerComboVerdict) -> None:
        _clear_layout(self.picker_cards_lay)
        self._picker_rows = []
        total_slots = max(len(verdict.cards), max((c.slot_index + 1 for c in verdict.cards), default=0))
        for card in verdict.cards:
            row = self._build_picker_row(
                card,
                is_best=(card.slot_index == verdict.best_slot_index),
                total_slots=total_slots,
            )
            self.picker_cards_lay.addWidget(row)
            row.show()
            self._picker_rows.append(row)

    def enter_picker_mode(self, verdict: PickerComboVerdict) -> None:
        """Switch to 3-card live combo/downgrade diagnosis and auto-expand without stealing focus."""
        self._last_verdict = verdict
        if self._mode != "picker":
            self._pre_picker_expanded = self._expanded
            self._mode = "picker"

        self._apply_header_density(self._custom_w or self.EXPANDED_W)
        self.profile_badge_lbl.setText(_clip(verdict.profile.direction_label, 10))
        self.plan_wrap.hide()
        self.picker_wrap.show()

        self._rebuild_picker_rows(verdict)

        if verdict.reroll_advice:
            self.reroll_lbl.setText(f"🎲 {verdict.reroll_advice}")
            self.reroll_banner.show()
        else:
            self.reroll_banner.hide()

        self._expanded = True
        self.pill.hide()
        self.panel.show()
        self._reposition()
        if not self._full_locked and self.isVisible():
            show_no_activate(self, click_through=False)

    def exit_picker_mode(self) -> None:
        """Return cleanly to plan mode and restore prior collapsed/expanded preference."""
        if self._mode != "picker":
            return
        self._mode = "plan"
        self.picker_wrap.hide()
        self.plan_wrap.show()
        self.hdr_title_lbl.setText("◈ 海克斯联动")
        restore_exp = self._pre_picker_expanded if self._pre_picker_expanded is not None else False
        self._pre_picker_expanded = None
        self.set_expanded(restore_exp)

    def _build_picker_row(self, card: PickerCardComboAdvice, *, is_best: bool, total_slots: int = 3) -> QFrame:
        labels = _SLOT_LABELS_4 if total_slots >= 4 else _SLOT_LABELS_3
        slot_txt = labels[card.slot_index] if 0 <= card.slot_index < len(labels) else f"#{card.slot_index + 1}"
        rar_color, _ = RARITY_COLORS.get(card.rarity, ("#C89B3C", ""))

        if card.verdict == "jackpot":
            bg = "rgba(8, 46, 56, 240)"
            border = "#0AC8B9" if is_best else "#C8AA6E"
            badge_bg = "rgba(10, 200, 185, 55)"
            badge_fg = "#A5F3FC"
            delta_fg = "#6EE7B7"
        elif card.verdict == "gamble":
            bg = "rgba(48, 30, 10, 238)"
            border = "#F59E0B"
            badge_bg = "rgba(245, 158, 11, 55)"
            badge_fg = "#FDE68A"
            delta_fg = "#FBBF24"
        elif card.verdict == "synergy":
            bg = "rgba(10, 30, 44, 232)"
            border = "#0397AB"
            badge_bg = "rgba(3, 151, 171, 45)"
            badge_fg = "#A5F3FC"
            delta_fg = "#38BDF8"
        elif card.verdict == "conflict":
            bg = "rgba(62, 16, 22, 230)"
            border = "#DC2626"
            badge_bg = "rgba(220, 38, 38, 55)"
            badge_fg = "#FECACA"
            delta_fg = "#F87171"
        else:
            bg = "rgba(10, 18, 32, 220)"
            border = "#334155"
            badge_bg = "rgba(51, 65, 85, 90)"
            badge_fg = "#CBD5E1"
            delta_fg = "#94A3B8"

        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ background-color: {bg}; border: 1px solid {border}; "
            f"border-left: 3px solid {border}; border-radius: 2px; }}"
        )
        rl = QVBoxLayout(row)
        rl.setContentsMargins(7, 5, 7, 5)
        rl.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(3)
        slot_lbl = QLabel(slot_txt)
        slot_lbl.setStyleSheet(
            "background-color: rgba(200, 170, 110, 35); border: 1px solid #785A28; "
            "border-radius: 2px; padding: 0px 4px; color: #F0E6D2; font-size: 9px; font-weight: bold;"
        )
        eff_w = self._custom_w or self.width() or self.EXPANDED_W
        name_cap = 6 if eff_w <= 256 else (9 if eff_w < 360 else 16)
        nm_lbl = QLabel(_clip(card.name, name_cap))
        nm_lbl.setToolTip(card.name)
        nm_lbl.setStyleSheet(
            f"border: none; background: transparent; color: {rar_color}; font-size: 11px; font-weight: bold;"
        )
        v_txt = card.verdict_label.replace("·降权", "") if eff_w <= 256 else card.verdict_label
        v_lbl = QLabel(v_txt)
        v_lbl.setStyleSheet(
            f"background-color: {badge_bg}; border: 1px solid {border}; border-radius: 2px; "
            f"padding: 0px 5px; color: {badge_fg}; font-size: 9px; font-weight: bold;"
        )
        delta_txt = f"{card.combo_delta:+.1f}" if abs(card.combo_delta) >= 0.1 else "0"
        d_lbl = QLabel(delta_txt)
        d_lbl.setStyleSheet(
            f"border: none; background: transparent; color: {delta_fg}; font-size: 10px; font-weight: bold;"
        )
        top.addWidget(slot_lbl)
        top.addWidget(nm_lbl)
        top.addStretch(1)
        top.addWidget(v_lbl)
        top.addWidget(d_lbl)
        rl.addLayout(top)

        reason_cap = max(24, int((eff_w - 36) / 6.0))
        rs_lbl = QLabel(_clip(card.reason, reason_cap))
        rs_lbl.setWordWrap(True)
        rs_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        rs_lbl.setToolTip(card.reason)
        rs_lbl.setStyleSheet(
            "border: none; background: transparent; color: #E2E8F0; font-size: 10px;"
            if card.verdict != "conflict"
            else "border: none; background: transparent; color: #FECACA; font-size: 10px;"
        )
        rl.addWidget(rs_lbl)
        return row
