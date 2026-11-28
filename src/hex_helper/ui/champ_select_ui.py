"""Champion Select floating overlay styled after League of Legends V16.23 Hextech Client."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QGuiApplication, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QVBoxLayout,
    QWidget,
)

from hex_helper.config import format_display_patch
from hex_helper.data.aram_balance import get_aram_balance, get_hero_ceiling_tag
from hex_helper.data.store import DataStore
from hex_helper.game.models import ChampSelectState
from hex_helper.scoring.team_comp import (
    BestFiveComp,
    TeamCompAnalysis,
    analyze_champ_select_pool,
    classify_hero_role,
)
from hex_helper.scoring.tier import score_hero_tier
from hex_helper.ui.resizable_overlay import FramelessResizeController, HextechCornerGrip
from hex_helper.ui.theme import BASE_OVERLAY_QSS, HEXTECH_MENU_QSS, load_app_pixmap, tier_badge_qss
from hex_helper.ui.win32_window import (
    UiBridgeInvoker,
    apply_capture_exclusion,
    clamp_to_screen_rect,
    physical_rect_to_logical,
    screen_for_physical_rect,
    show_no_activate,
)


def _circular_portrait(path_str: str, size: int = 24, border_hex: str = "#785A28") -> QPixmap | None:
    src = QPixmap(path_str)
    if src.isNull():
        return None
    scaled = src.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(QRectF(1.0, 1.0, size - 2.0, size - 2.0))
    p.setClipPath(clip)
    p.drawPixmap(0, 0, scaled)
    p.setClipping(False)
    p.setPen(QPen(QColor(border_hex), 1.3))
    p.drawEllipse(QRectF(1.0, 1.0, size - 2.0, size - 2.0))
    p.end()
    return out


def _classify_hero_role(hero_id: str, store: DataStore) -> str:
    """Classify a champion's primary ARAM profile ('AP' | 'AD' | 'TANK') from top core builds."""
    return classify_hero_role(hero_id, store)


_MODE_BTN_CLIMB_ACTIVE_QSS = (
    "QPushButton { background-color: rgba(10, 200, 185, 56); border: 1px solid #0AC8B9; "
    "border-radius: 2px; color: #F0E6D2; font-size: 10px; font-weight: bold; padding: 1px 8px; }"
)
_MODE_BTN_FUN_ACTIVE_QSS = (
    "QPushButton { background-color: rgba(200, 155, 60, 64); border: 1px solid #C8AA6E; "
    "border-radius: 2px; color: #F0E6D2; font-size: 10px; font-weight: bold; padding: 1px 8px; }"
)
_MODE_BTN_IDLE_QSS = (
    "QPushButton { background-color: transparent; border: 1px solid transparent; "
    "border-radius: 2px; color: #A09B8C; font-size: 10px; font-weight: 600; padding: 1px 8px; }"
    "QPushButton:hover { color: #F0E6D2; background-color: rgba(200, 170, 110, 24); }"
)
_COMP_ALERT_QSS: dict[str, str] = {
    "warn": (
        "background-color: rgba(68, 38, 10, 215); border: 1px solid #C89B3C; "
        "border-left: 3px solid #FDE68A; border-radius: 2px; padding: 4px 7px; "
        "color: #FDE68A; font-size: 10px; font-weight: bold;"
    ),
    "swap": (
        "background-color: rgba(6, 36, 46, 224); border: 1px solid #0AC8B9; "
        "border-left: 3px solid #5EEAD4; border-radius: 2px; padding: 4px 7px; "
        "color: #A5F3FC; font-size: 10px; font-weight: bold;"
    ),
    "ok": (
        "background-color: rgba(6, 40, 34, 195); border: 1px solid #09C269; "
        "border-radius: 2px; padding: 4px 7px; color: #6EE7B7; font-size: 10px;"
    ),
}


_ROLE_GAP_BADGE: dict[str, str] = {
    "TANK": "补前排",
    "AD": "补AD",
    "AP": "补AP",
}


def analyze_team_needs(
    team_hero_ids: tuple[str, ...] | list[str],
    store: DataStore,
) -> tuple[str, bool, set[str]]:
    """Analyze current 3-5 ally champions; return `(summary_text, is_warn, needed_roles)`."""
    unique_ids = list(dict.fromkeys(str(h) for h in team_hero_ids if h))
    if len(unique_ids) < 3:
        return "", False, set()

    ap_cnt = 0
    ad_cnt = 0
    tank_cnt = 0
    for hid in unique_ids:
        role = _classify_hero_role(hid, store)
        if role == "AP":
            ap_cnt += 1
        elif role == "AD":
            ad_cnt += 1
        else:
            tank_cnt += 1

    total = len(unique_ids)
    needed: set[str] = set()
    if tank_cnt == 0 and total >= 3:
        needed.add("TANK")
    if ap_cnt >= 4 or (total >= 3 and ap_cnt == total) or (ap_cnt >= 3 and ad_cnt == 0):
        needed.add("AD")
    if ad_cnt >= 4 or (total >= 3 and ad_cnt == total) or (ad_cnt >= 3 and ap_cnt == 0):
        needed.add("AP")

    if ap_cnt >= 4 or (total >= 3 and ap_cnt == total):
        return f"⚠ 队内重AP ({ap_cnt}AP · {tank_cnt}前排) · 优先看备战席物理/前排", True, needed
    if ad_cnt >= 4 or (total >= 3 and ad_cnt == total):
        return f"⚠ 队内重AD ({ad_cnt}AD · {tank_cnt}前排) · 优先看备战席法师/前排", True, needed
    if total >= 4 and tank_cnt == 0:
        return f"⚠ 队内缺前排开团 ({ad_cnt}AD · {ap_cnt}AP · 0坦) · 可看备战席坦克", True, needed
    if needed:
        gaps = "/".join(_ROLE_GAP_BADGE[r] for r in ("TANK", "AD", "AP") if r in needed)
        return f"◈ 阵容参考 ({ad_cnt}AD · {ap_cnt}AP · {tank_cnt}前排 · 备战席可{gaps})", False, needed
    return f"✓ 阵容均衡 ({ad_cnt}AD · {ap_cnt}AP · {tank_cnt}前排)", False, needed


def summarize_team_composition(team_hero_ids: tuple[str, ...] | list[str], store: DataStore) -> tuple[str, bool]:
    """Analyze current 3-5 ally champions and warn if AP/AD/frontline balance is severely skewed."""
    txt, is_warn, _ = analyze_team_needs(team_hero_ids, store)
    return txt, is_warn


class ChampSelectOverlay(QWidget):
    """Draggable, resizable, non-activating LoL Hextech panel docked beside LeagueClient during Champ Select."""

    closed_by_user = Signal()
    geometry_changed = Signal(dict)
    subcard_action = Signal(str, str, dict)

    DEFAULT_W = 366
    MIN_W = 310
    MAX_W = 580
    MIN_H = 220
    _QWIDGETSIZE_MAX = 16777215

    def __init__(
        self,
        on_pos_changed: Callable[[list[int]], None] | None = None,
        on_size_changed: Callable[[list[int] | None], None] | None = None,
        initial_size: list[int] | None = None,
        on_core_action: Callable[[str], None] | None = None,
        on_goal_mode_changed: Callable[[str], None] | None = None,
        initial_goal_mode: str = "climb",
        on_closed_by_user: Callable[[], None] | None = None,
        on_geometry_changed: Callable[[dict[str, Any]], None] | None = None,
        on_subcard_action: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_pos_changed = on_pos_changed
        self._on_size_changed = on_size_changed
        self._on_core_action = on_core_action
        self._on_goal_mode_changed = on_goal_mode_changed
        self._on_closed_by_user = on_closed_by_user
        self._on_geometry_changed = on_geometry_changed
        self._on_subcard_action = on_subcard_action
        self._gesture_start_norm_rect: list[float] | None = None
        self._goal_mode: str = "fun" if str(initial_goal_mode or "climb").strip().lower() == "fun" else "climb"
        self._applied_goal_mode_style: str = ""
        self._best_five_expanded: bool = False
        self._last_best_five_sig: tuple | None = None
        self._last_comp_alert_style: str = ""
        self._last_rows_content_h: int = 64
        self._ai_bar_action: str = ""
        self._drag_offset: QPoint | None = None
        self._drag_press_global: QPoint | None = None
        self._did_drag: bool = False
        self._icon_cache: dict[tuple[str, str], QPixmap] = {}
        self._fetching: set[str] = set()
        self._icon_invoker = UiBridgeInvoker()
        self._last_state: ChampSelectState | None = None
        self._last_store: DataStore | None = None
        self._last_analysis: TeamCompAnalysis | None = None
        self._fade_anim: QPropertyAnimation | None = None
        self._allow_screen_recording: bool = False
        self._custom_w: int | None = None
        self._custom_h: int | None = None
        if initial_size and len(initial_size) == 2:
            try:
                iw, ih = int(initial_size[0]), int(initial_size[1])
                if iw >= self.MIN_W:
                    self._custom_w = max(self.MIN_W, min(self.MAX_W, iw))
                if ih >= self.MIN_H:
                    self._custom_h = max(self.MIN_H, ih)
            except Exception:
                pass
        self._last_density_bucket: int = self._width_bucket(self._custom_w or self.DEFAULT_W)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(BASE_OVERLAY_QSS)
        self.setFixedWidth(self._custom_w or self.DEFAULT_W)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self.root = QFrame()
        self.root.setObjectName("PanelRoot")
        outer.addWidget(self.root)

        self.layout_main = QVBoxLayout(self.root)
        self.layout_main.setContentsMargins(11, 9, 11, 10)
        self.layout_main.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(7)
        self.icon_lbl = QLabel()
        self.icon_lbl.setPixmap(load_app_pixmap(24))
        self.icon_lbl.setFixedSize(24, 24)
        self.title_lbl = QLabel("海克斯大乱斗 · 选人决策")
        self.title_lbl.setStyleSheet("font-weight: bold; color: #F0E6D2; font-size: 13px;")
        self.patch_lbl = QLabel("")
        self.patch_lbl.setStyleSheet("color: #C8AA6E; font-size: 11px;")
        close_btn = QPushButton("×")
        close_btn.setObjectName("WinCtrlBtn")
        close_btn.setFixedSize(22, 20)
        close_btn.setToolTip("关闭本轮选人浮窗")
        close_btn.clicked.connect(self._close_by_user)
        self.close_btn = close_btn
        header.addWidget(self.icon_lbl)
        header.addWidget(self.title_lbl)
        header.addStretch(1)
        header.addWidget(self.patch_lbl)
        header.addWidget(close_btn)
        self.layout_main.addLayout(header)

        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(0, 0, 0, 0)
        mode_bar.setSpacing(6)

        self.mode_pill_frame = QFrame()
        self.mode_pill_frame.setStyleSheet(
            "QFrame { background-color: #040E1A; border: 1px solid #785A28; border-radius: 2px; }"
        )
        pill_lay = QHBoxLayout(self.mode_pill_frame)
        pill_lay.setContentsMargins(2, 2, 2, 2)
        pill_lay.setSpacing(2)

        self.mode_climb_btn = QPushButton("冲分")
        self.mode_climb_btn.setCheckable(True)
        self.mode_climb_btn.setFixedHeight(19)
        self.mode_climb_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_climb_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mode_climb_btn.setToolTip("冲分视角：优先前后排/AD-AP均衡、控制链与稳定胜率")
        self.mode_climb_btn.clicked.connect(lambda: self.set_goal_mode("climb"))

        self.mode_fun_btn = QPushButton("爽局")
        self.mode_fun_btn.setCheckable(True)
        self.mode_fun_btn.setFixedHeight(19)
        self.mode_fun_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mode_fun_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mode_fun_btn.setToolTip("爽局视角：加权海克斯高上限爆发与爽点英雄")
        self.mode_fun_btn.clicked.connect(lambda: self.set_goal_mode("fun"))

        pill_lay.addWidget(self.mode_climb_btn)
        pill_lay.addWidget(self.mode_fun_btn)

        self.team_score_lbl = QLabel("")
        self.team_score_lbl.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-size: 10px; font-weight: bold;"
        )
        mode_bar.addWidget(self.mode_pill_frame)
        mode_bar.addStretch(1)
        mode_bar.addWidget(self.team_score_lbl)
        self.layout_main.addLayout(mode_bar)
        self._sync_goal_mode_buttons()

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background-color: rgba(200, 170, 110, 75);")
        self.layout_main.addWidget(div)

        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.rows_scroll.viewport().setStyleSheet("background: transparent;")
        self.rows_body = QWidget()
        self.rows_body.setObjectName("ChampRowsScrollBody")
        self.rows_body.setStyleSheet("QWidget#ChampRowsScrollBody { background: transparent; border: none; }")
        self.rows_container = QVBoxLayout(self.rows_body)
        self.rows_container.setContentsMargins(0, 0, 5, 0)
        self.rows_container.setSpacing(4)
        self.rows_container.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.rows_scroll.setWidget(self.rows_body)
        self.layout_main.addWidget(self.rows_scroll, 1)

        # Collapsible Best-5 strip (inline C(n,5) optimal lineup preview, zero extra floating windows)
        self.best_five_strip = QFrame()
        self.best_five_strip.setStyleSheet(
            "QFrame { background-color: rgba(6, 20, 34, 224); "
            "border: 1px solid rgba(120, 90, 40, 145); "
            "border-left: 2px solid #C8AA6E; border-radius: 2px; }"
        )
        b5_lay = QVBoxLayout(self.best_five_strip)
        b5_lay.setContentsMargins(7, 4, 7, 4)
        b5_lay.setSpacing(4)

        b5_hdr = QHBoxLayout()
        b5_hdr.setContentsMargins(0, 0, 0, 0)
        b5_hdr.setSpacing(5)
        self.best_five_hdr_lbl = QLabel("")
        self.best_five_hdr_lbl.setStyleSheet(
            "border: none; background: transparent; color: #F0E6D2; font-size: 10px; font-weight: bold;"
        )
        self.best_five_toggle_btn = QPushButton("展开")
        self.best_five_toggle_btn.setFixedHeight(18)
        self.best_five_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.best_five_toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.best_five_toggle_btn.setStyleSheet(
            "QPushButton { background-color: rgba(17, 30, 46, 220); border: 1px solid #785A28; "
            "border-radius: 2px; color: #C8AA6E; font-size: 9px; font-weight: bold; padding: 0px 6px; }"
            "QPushButton:hover { border-color: #C8AA6E; color: #F0E6D2; }"
        )
        self.best_five_toggle_btn.clicked.connect(self._toggle_best_five)
        b5_hdr.addWidget(self.best_five_hdr_lbl, 1)
        b5_hdr.addWidget(self.best_five_toggle_btn)
        b5_lay.addLayout(b5_hdr)

        self.best_five_body = QWidget()
        self.best_five_body.setStyleSheet("background: transparent; border: none;")
        b5_body_lay = QVBoxLayout(self.best_five_body)
        b5_body_lay.setContentsMargins(0, 2, 0, 0)
        b5_body_lay.setSpacing(3)

        self.best_five_heroes_lay = QHBoxLayout()
        self.best_five_heroes_lay.setContentsMargins(0, 0, 0, 0)
        self.best_five_heroes_lay.setSpacing(4)
        b5_body_lay.addLayout(self.best_five_heroes_lay)

        self.best_five_tags_lbl = QLabel("")
        self.best_five_tags_lbl.setStyleSheet(
            "border: none; background: transparent; color: #0AC8B9; font-size: 9px;"
        )
        b5_body_lay.addWidget(self.best_five_tags_lbl)

        self.best_five_body.hide()
        b5_lay.addWidget(self.best_five_body)
        self.best_five_strip.hide()
        self.layout_main.addWidget(self.best_five_strip)

        self.comp_alert_lbl = QLabel("")
        self.comp_alert_lbl.setWordWrap(True)
        self.comp_alert_lbl.hide()
        self.layout_main.addWidget(self.comp_alert_lbl)

        self.spell_lbl = QLabel("")
        self.spell_lbl.setWordWrap(True)
        self.spell_lbl.setStyleSheet("color: #0AC8B9; font-size: 11px; padding-top: 2px;")
        self.layout_main.addWidget(self.spell_lbl)

        # Compact 24px commercial AI readiness footer (30-60s calm pre-game window; never touches champion rows)
        self.ai_readiness_bar = QFrame()
        self.ai_readiness_bar.setFixedHeight(26)
        self.ai_readiness_bar.setStyleSheet(
            "QFrame { background-color: rgba(6, 18, 30, 210); border-top: 1px solid rgba(120, 90, 40, 110); "
            "border-radius: 2px; }"
        )
        air_lay = QHBoxLayout(self.ai_readiness_bar)
        air_lay.setContentsMargins(6, 2, 6, 2)
        air_lay.setSpacing(6)
        self.ai_readiness_lbl = QLabel("")
        self.ai_readiness_lbl.setStyleSheet(
            "border: none; background: transparent; color: #A09B8C; font-size: 10px; font-weight: bold;"
        )
        self.ai_readiness_btn = QPushButton("")
        self.ai_readiness_btn.setFixedHeight(19)
        self.ai_readiness_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ai_readiness_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.ai_readiness_btn.setStyleSheet(
            "QPushButton { background-color: rgba(10, 48, 60, 220); border: 1px solid #0AC8B9; "
            "color: #F0E6D2; font-size: 9px; font-weight: bold; padding: 0px 7px; border-radius: 2px; }"
            "QPushButton:hover { border-color: #7CF8EE; color: #FFFFFF; }"
        )
        self.ai_readiness_btn.clicked.connect(self._on_ai_bar_clicked)
        air_lay.addWidget(self.ai_readiness_lbl, 1)
        air_lay.addWidget(self.ai_readiness_btn)
        self.ai_readiness_bar.hide()
        self.layout_main.addWidget(self.ai_readiness_bar)

        # Frameless edge/corner resize grips & controller
        self._grip_bl = HextechCornerGrip(
            self.root, corner="bl", on_double_click=self.reset_default_size
        )
        self._grip_br = HextechCornerGrip(
            self.root, corner="br", on_double_click=self.reset_default_size
        )
        self._resizer = FramelessResizeController(
            self,
            min_w=self.MIN_W,
            max_w=self.MAX_W,
            min_h=self.MIN_H,
            max_h=1200,
            can_resize=lambda: True,
            on_live_resize=self._on_live_resize,
            on_resize_finished=self._on_resize_finished,
        )
        for child_w in (
            self.root,
            self.rows_scroll,
            self.rows_scroll.viewport(),
            self.rows_body,
            self._grip_bl,
            self._grip_br,
        ):
            self._resizer.attach(child_w)
        self._apply_header_density(self._custom_w or self.DEFAULT_W)
        if self._custom_h is not None:
            self.setFixedHeight(self._custom_h)

    @staticmethod
    def _width_bucket(w: int) -> int:
        if w < 342:
            return 0
        if w >= 445:
            return 2
        return 1

    def _apply_header_density(self, w: int) -> None:
        narrow = w < 342
        self.icon_lbl.setVisible(not narrow)
        self.title_lbl.setText("选人决策" if narrow else "海克斯大乱斗 · 选人决策")

    def _layout_corner_grips(self) -> None:
        rw = self.root.width()
        rh = self.root.height()
        if rw <= 20 or rh <= 20:
            return
        self._grip_bl.move(1, rh - self._grip_bl.height() - 1)
        self._grip_br.move(rw - self._grip_br.width() - 1, rh - self._grip_br.height() - 1)
        self._grip_bl.raise_()
        self._grip_br.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_corner_grips()

    def get_tracked_scrollbars(self) -> dict[str, QScrollBar]:
        """Return key internal vertical scrollbars for optional DevLab overflow observation."""
        return {"champ_select.rows": self.rows_scroll.verticalScrollBar()}

    def _close_by_user(self) -> None:
        self.closed_by_user.emit()
        if self._on_closed_by_user is not None:
            self._on_closed_by_user()
        self.hide()

    def _emit_subcard_action(
        self,
        card: str,
        action: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(meta) if isinstance(meta, dict) else {}
        self.subcard_action.emit(card, action, payload)
        if self._on_subcard_action is not None:
            self._on_subcard_action(card, action, payload)

    def _capture_norm_rect(self) -> tuple[list[float], QRect, int, int, float]:
        screens = list(QGuiApplication.screens())
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.geometry()
            dpr = max(1.0, float(screen.devicePixelRatio() or 1.0))
            try:
                screen_idx = screens.index(screen)
            except ValueError:
                screen_idx = 0
        else:
            geo = QRect(0, 0, 1920, 1080)
            dpr = 1.0
            screen_idx = 0
        sw = max(1, geo.width())
        sh = max(1, geo.height())
        norm = [
            round((self.x() - geo.x()) / sw, 4),
            round((self.y() - geo.y()) / sh, 4),
            round(self.width() / sw, 4),
            round(self.height() / sh, 4),
        ]
        return norm, geo, screen_idx, max(1, len(screens)), dpr

    def _compute_snapped_edge(self, geo: QRect) -> str:
        tol = max(72, int(geo.width() * 0.05))
        dist_l = abs(self.x() - geo.left())
        dist_r = abs((geo.right() + 1) - (self.x() + self.width()))
        if dist_r <= tol and dist_r <= dist_l:
            return "right"
        if dist_l <= tol:
            return "left"
        dist_t = abs(self.y() - geo.top())
        dist_b = abs((geo.bottom() + 1) - (self.y() + self.height()))
        if dist_t <= 36 and dist_t <= dist_b:
            return "top"
        if dist_b <= 36:
            return "bottom"
        return "detached"

    def _emit_geometry_changed(self, action: str) -> None:
        norm, geo, screen_idx, screen_cnt, dpr = self._capture_norm_rect()
        prev_norm = self._gesture_start_norm_rect
        self._gesture_start_norm_rect = None
        saved_size = [int(self._custom_w), int(self._custom_h)] if (self._custom_w is not None and self._custom_h is not None) else None
        payload: dict[str, Any] = {
            "overlay": "champ_select",
            "action": action,
            "x": int(self.x()),
            "y": int(self.y()),
            "width": int(self.width()),
            "height": int(self.height()),
            "pos": [int(self.x()), int(self.y())],
            "size": saved_size,
            "scale": None,
            "expanded": True,
            "snapped_edge": self._compute_snapped_edge(geo),
            "screen_rect": [int(geo.x()), int(geo.y()), int(geo.width()), int(geo.height())],
            "screen_index": int(screen_idx),
            "screen_count": int(screen_cnt),
            "dpr": round(float(dpr), 3),
            "norm_rect": norm,
            "prev_norm_rect": prev_norm,
        }
        self.geometry_changed.emit(payload)
        if self._on_geometry_changed is not None:
            self._on_geometry_changed(payload)

    def _on_live_resize(self, target: QRect, _edge: str) -> None:
        if self._gesture_start_norm_rect is None:
            self._gesture_start_norm_rect = self._capture_norm_rect()[0]
        w = max(self.MIN_W, min(self.MAX_W, target.width()))
        h = max(self.MIN_H, target.height())
        self._custom_w = w
        self._custom_h = h
        self.rows_scroll.setMinimumHeight(64)
        self.rows_scroll.setMaximumHeight(self._QWIDGETSIZE_MAX)
        self._apply_header_density(w)
        self.setFixedWidth(w)
        self.setFixedHeight(h)
        self.setGeometry(target.x(), target.y(), w, h)
        self._layout_corner_grips()

        new_bucket = self._width_bucket(w)
        if new_bucket != self._last_density_bucket:
            self._last_density_bucket = new_bucket
            if self._last_state is not None and self._last_store is not None:
                self.update_state(self._last_state, self._last_store, team_comp_analysis=self._last_analysis)
                self.setGeometry(target.x(), target.y(), w, h)
                self._layout_corner_grips()

    def _on_resize_finished(self, target: QRect, _edge: str) -> None:
        self._custom_w = max(self.MIN_W, min(self.MAX_W, target.width()))
        self._custom_h = max(self.MIN_H, target.height())
        new_bucket = self._width_bucket(self._custom_w)
        if new_bucket != self._last_density_bucket:
            self._last_density_bucket = new_bucket
            if self._last_state is not None and self._last_store is not None:
                self.update_state(self._last_state, self._last_store, team_comp_analysis=self._last_analysis)
        self._layout_corner_grips()
        if self._on_size_changed:
            self._on_size_changed([self._custom_w, self._custom_h])
        if self._on_pos_changed:
            self._on_pos_changed([self.x(), self.y()])
        self._emit_geometry_changed("resize")

    def reset_default_size(self) -> None:
        self._gesture_start_norm_rect = self._capture_norm_rect()[0]
        self._custom_w = None
        self._custom_h = None
        self._last_density_bucket = self._width_bucket(self.DEFAULT_W)
        self._apply_header_density(self.DEFAULT_W)
        self.setFixedWidth(self.DEFAULT_W)
        if self._last_state is not None and self._last_store is not None:
            self.update_state(self._last_state, self._last_store, team_comp_analysis=self._last_analysis)
        else:
            self.rows_scroll.setMinimumHeight(64)
            self.rows_scroll.setMaximumHeight(self._QWIDGETSIZE_MAX)
            self.setFixedHeight(320)
        self._layout_corner_grips()
        if self._on_size_changed:
            self._on_size_changed(None)
        self._emit_geometry_changed("reset_size")

    def _get_portrait(self, hid: str, store: DataStore, border_hex: str) -> QPixmap | None:
        key = (hid, border_hex)
        cached = self._icon_cache.get(key)
        if cached is not None:
            return cached
        path = store.static.icons_dir / "champion" / f"{hid}.png"
        if path.exists() and path.stat().st_size > 200:
            pm = _circular_portrait(str(path), size=24, border_hex=border_hex)
            if pm is not None:
                self._icon_cache[key] = pm
                return pm
        if hid not in self._fetching:
            self._fetching.add(hid)

            def _fetch() -> None:
                try:
                    p = store.static.ensure_icon("champion", hid)
                    if p is not None and p.exists():
                        self._icon_invoker.invoke.emit(self._refresh_last_state)
                finally:
                    self._fetching.discard(hid)

            threading.Thread(target=_fetch, name=f"HexCsIcon-{hid}", daemon=True).start()
        return None

    def _on_ai_bar_clicked(self) -> None:
        if self._ai_bar_action and self._on_core_action is not None:
            self._on_core_action(self._ai_bar_action)

    def set_core_lifecycle(self, snapshot) -> None:
        """Render a restrained 26px pre-game AI readiness strip (hidden when snoozed, off, or dev)."""
        if snapshot is None:
            self.ai_readiness_bar.hide()
            return
        from hex_helper.ui.hex_seal_overlay import CoreEnergyState

        st = snapshot.state
        if st in (CoreEnergyState.SYSTEM_GATE, CoreEnergyState.DISABLED_BY_USER, CoreEnergyState.SYNCING):
            self.ai_readiness_bar.hide()
            return
        if snapshot.snoozed and st in (CoreEnergyState.GUEST_UNCLAIMED, CoreEnergyState.CORRODED_TRIAL_END):
            self.ai_readiness_bar.hide()
            return

        tier_short = {"auto": "智能", "a": "A档", "b": "B档"}.get(snapshot.tier, "智能")
        if st == CoreEnergyState.GUEST_UNCLAIMED:
            self._ai_bar_action = "register"
            self.ai_readiness_lbl.setText("◈ 注册赠 450 积分（约 2 局）· 进局 5 秒定盘")
            self.ai_readiness_lbl.setStyleSheet(
                "border: none; background: transparent; color: #5EEAD4; font-size: 10px; font-weight: bold;"
            )
            self.ai_readiness_btn.setText("免费领")
            self.ai_readiness_btn.show()
        elif st == CoreEnergyState.CORRODED_TRIAL_END:
            self._ai_bar_action = "preview"
            self.ai_readiness_lbl.setText("◇ AI 定盘封印中 · 本地 5ms 选人/出装照常护航")
            self.ai_readiness_lbl.setStyleSheet(
                "border: none; background: transparent; color: #C8AA6E; font-size: 10px; font-weight: bold;"
            )
            self.ai_readiness_btn.setText("趁读条充能")
            self.ai_readiness_btn.show()
        elif st == CoreEnergyState.CORRODED_RETURNING:
            self._ai_bar_action = "recharge"
            self.ai_readiness_lbl.setText("◇ 核心待补能 · 本地 5ms 持续护航")
            self.ai_readiness_lbl.setStyleSheet(
                "border: none; background: transparent; color: #C8AA6E; font-size: 10px; font-weight: bold;"
            )
            self.ai_readiness_btn.setText("快速补能")
            self.ai_readiness_btn.show()
        elif st == CoreEnergyState.READY_TO_UNSEAL:
            self._ai_bar_action = "unseal_lobby"
            self.ai_readiness_lbl.setText(f"⚡ 核心已充能（约剩 {snapshot.est_matches} 局）· 进局自动破封")
            self.ai_readiness_lbl.setStyleSheet(
                "border: none; background: transparent; color: #0AC8B9; font-size: 10px; font-weight: bold;"
            )
            self.ai_readiness_btn.setText("去破封")
            self.ai_readiness_btn.show()
        elif st in (CoreEnergyState.TRIAL_ACTIVE, CoreEnergyState.TRIAL_LOW):
            low = st == CoreEnergyState.TRIAL_LOW
            self._ai_bar_action = "recharge" if low else ""
            self.ai_readiness_lbl.setText(
                f"● 体验 AI 待命（约剩 {snapshot.est_matches} 局）· 进局 5 秒定盘"
            )
            self.ai_readiness_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {'#FDE68A' if low else '#0AC8B9'}; font-size: 10px; font-weight: bold;"
            )
            self.ai_readiness_btn.setText("续能")
            self.ai_readiness_btn.setVisible(low)
        else:
            low = st == CoreEnergyState.PAID_LOW
            self._ai_bar_action = "recharge" if low else ""
            self.ai_readiness_lbl.setText(
                f"● AI 参谋就绪（{tier_short} · 约剩 {snapshot.est_matches} 局）· 进局定盘"
            )
            self.ai_readiness_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {'#FDE68A' if low else '#09C269'}; font-size: 10px; font-weight: bold;"
            )
            self.ai_readiness_btn.setText("续能")
            self.ai_readiness_btn.setVisible(low)
        self.ai_readiness_bar.show()

    @property
    def goal_mode(self) -> str:
        return self._goal_mode

    def _sync_goal_mode_buttons(self) -> None:
        is_climb = self._goal_mode == "climb"
        self.mode_climb_btn.setChecked(is_climb)
        self.mode_fun_btn.setChecked(not is_climb)
        if self._applied_goal_mode_style != self._goal_mode:
            self._applied_goal_mode_style = self._goal_mode
            self.mode_climb_btn.setStyleSheet(_MODE_BTN_CLIMB_ACTIVE_QSS if is_climb else _MODE_BTN_IDLE_QSS)
            self.mode_fun_btn.setStyleSheet(_MODE_BTN_FUN_ACTIVE_QSS if not is_climb else _MODE_BTN_IDLE_QSS)

    def set_goal_mode(self, mode: str, *, emit_callback: bool = True) -> None:
        """Switch between 'climb' (冲分) and 'fun' (爽局) scoring profiles and refresh view."""
        norm = "fun" if str(mode or "climb").strip().lower() == "fun" else "climb"
        changed = norm != self._goal_mode
        self._goal_mode = norm
        self._sync_goal_mode_buttons()
        if not changed:
            return
        self._last_analysis = None
        if emit_callback:
            self._emit_subcard_action("goal_mode", norm, {"goal_mode": norm})
            if self._on_goal_mode_changed is not None:
                self._on_goal_mode_changed(norm)
        if self._last_state is not None and self._last_store is not None:
            self.update_state(self._last_state, self._last_store)

    def set_best_five_expanded(self, expanded: bool) -> None:
        exp = bool(expanded)
        self._best_five_expanded = exp
        self.best_five_body.setVisible(exp)
        self.best_five_toggle_btn.setText("收起" if exp else "展开")
        self._sync_window_height(self._last_rows_content_h)

    def _toggle_best_five(self) -> None:
        next_exp = not self._best_five_expanded
        self.set_best_five_expanded(next_exp)
        self._emit_subcard_action(
            "best_five",
            "expand" if next_exp else "collapse",
            {"expanded": next_exp},
        )

    def _render_best_five(
        self,
        bf: BestFiveComp | None,
        ally_set: set[str],
        store: DataStore,
        *,
        is_narrow: bool,
    ) -> None:
        if bf is None:
            self.best_five_strip.hide()
            self._last_best_five_sig = None
            return

        hdr_txt = (
            f"◈ 已算 {bf.evaluated_combos} 阵 · 最强 {bf.score:.1f}分"
            if is_narrow
            else f"◈ 已算 {bf.evaluated_combos} 阵 · 最强5人 {bf.score:.1f}分"
        )
        self.best_five_hdr_lbl.setText(hdr_txt)

        tag_txt = " · ".join(bf.tags) if bf.tags else ""
        self.best_five_tags_lbl.setText(tag_txt)
        self.best_five_tags_lbl.setVisible(bool(tag_txt))

        portraits: list[tuple[str, str, bool, str, QPixmap | None]] = []
        for hid, hname in zip(bf.hero_ids, bf.hero_names):
            disp_name = store.display_hero_name(hid, hname) if hasattr(store, "display_hero_name") else hname
            in_team = hid in ally_set
            border_hex = "#C8AA6E" if in_team else "#0AC8B9"
            pm = self._get_portrait(hid, store, border_hex)
            portraits.append((hid, disp_name, in_team, border_hex, pm))

        icon_ready = tuple((hid, pm is not None) for hid, _hname, _in_team, _border, pm in portraits)
        locale_tag = str(getattr(store, "client_locale", "zh_cn") or "zh_cn")
        sig = (bf.hero_ids, bf.hero_names, locale_tag, tuple(sorted(ally_set)), is_narrow, icon_ready)
        if sig != self._last_best_five_sig:
            while self.best_five_heroes_lay.count():
                item = self.best_five_heroes_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.hide()
                    w.setParent(None)
                    w.deleteLater()

            for hid, hname, in_team, border_hex, pm in portraits:
                chip = QFrame()
                chip.setStyleSheet(
                    "QFrame { background-color: rgba(9, 20, 40, 200); "
                    "border: 1px solid rgba(200, 170, 110, 95); border-radius: 2px; }"
                    if in_team
                    else "QFrame { background-color: rgba(6, 36, 46, 215); "
                    "border: 1px solid rgba(10, 200, 185, 145); border-radius: 2px; }"
                )
                chip.setToolTip(f"{hname}（{'当前队内' if in_team else '备战席可换'}）")
                cl = QVBoxLayout(chip)
                cl.setContentsMargins(2, 3, 2, 2)
                cl.setSpacing(2)
                cl.setAlignment(Qt.AlignmentFlag.AlignHCenter)

                ico_sz = 18
                ico = QLabel()
                ico.setFixedSize(ico_sz, ico_sz)
                ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
                if pm is not None:
                    ico.setPixmap(
                        pm.scaled(
                            ico_sz,
                            ico_sz,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                else:
                    ico.setText(hname[:1])
                    ico.setStyleSheet(
                        f"background-color: #040E1A; border: 1px solid {border_hex}; "
                        f"border-radius: {ico_sz // 2}px; color: {border_hex}; font-size: 9px; font-weight: bold;"
                    )

                nm = QLabel(hname[:4] if is_narrow and len(hname) > 4 else hname)
                nm.setAlignment(Qt.AlignmentFlag.AlignCenter)
                nm.setStyleSheet(
                    f"border: none; background: transparent; font-size: 9px; font-weight: bold; "
                    f"color: {'#F0E6D2' if in_team else '#A5F3FC'};"
                )
                cl.addWidget(ico, 0, Qt.AlignmentFlag.AlignHCenter)
                cl.addWidget(nm)
                self.best_five_heroes_lay.addWidget(chip, 1)
            self._last_best_five_sig = sig

        self.best_five_body.setVisible(self._best_five_expanded)
        self.best_five_toggle_btn.setText("收起" if self._best_five_expanded else "展开")
        self.best_five_strip.show()

    def _sync_window_height(self, content_h: int) -> None:
        self._last_rows_content_h = max(64, int(content_h))
        scr = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        avail_h = scr.availableGeometry().height() if scr is not None else 720
        max_win_h = max(self.MIN_H, avail_h - 24)
        if self._custom_h is not None:
            self.rows_scroll.setMinimumHeight(64)
            self.rows_scroll.setMaximumHeight(self._QWIDGETSIZE_MAX)
            self.layout_main.invalidate()
            self.layout_main.activate()
            self.setFixedHeight(min(max_win_h, max(self.MIN_H, self._custom_h)))
        else:
            max_scroll_h = max(260, min(780, avail_h - 180))
            target_scroll_h = min(max_scroll_h, self._last_rows_content_h)
            self.rows_scroll.setFixedHeight(target_scroll_h)

            for sub_w in (self.best_five_body, self.best_five_strip):
                lay_s = sub_w.layout()
                if lay_s is not None:
                    lay_s.invalidate()
                    lay_s.activate()
                sub_w.updateGeometry()

            self.layout_main.invalidate()
            self.layout_main.activate()
            self.root.updateGeometry()
            outer_lay = self.layout()
            if outer_lay is not None:
                outer_lay.invalidate()
                outer_lay.activate()
            total_h = max(self.layout_main.sizeHint().height() + 4, target_scroll_h + 100)
            if total_h > max_win_h and target_scroll_h > 120:
                shrunk_scroll_h = max(120, target_scroll_h - (total_h - max_win_h))
                self.rows_scroll.setFixedHeight(shrunk_scroll_h)
                self.layout_main.invalidate()
                self.layout_main.activate()
                self.root.updateGeometry()
                if outer_lay is not None:
                    outer_lay.invalidate()
                    outer_lay.activate()
                total_h = self.layout_main.sizeHint().height() + 4
            self.setFixedHeight(min(max_win_h, total_h))
        self.ensure_on_screen()
        self._layout_corner_grips()

    def _refresh_last_state(self) -> None:
        if self._last_state is not None and self._last_store is not None and self.isVisible():
            self.update_state(self._last_state, self._last_store, team_comp_analysis=self._last_analysis)

    def update_state(
        self,
        state: ChampSelectState,
        store: DataStore,
        team_comp_analysis: TeamCompAnalysis | None = None,
    ) -> None:
        self._last_state = state
        self._last_store = store

        while self.rows_container.count():
            item = self.rows_container.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        if store.descriptor:
            disp = format_display_patch(store.descriptor.patch)
            mode_tag = "首次必棱彩" if store.descriptor.mode == "first-prismatic" else "常规"
            self.patch_lbl.setText(f"{disp.split('（')[0]} · {mode_tag}")

        ally_ids = list(dict.fromkeys(([state.my_hero_id] if state.my_hero_id else []) + list(state.team_hero_ids)))
        ally_set = set(ally_ids)
        if team_comp_analysis is not None and team_comp_analysis.goal_mode == self._goal_mode:
            analysis = team_comp_analysis
        else:
            analysis = analyze_champ_select_pool(
                state.my_hero_id,
                state.team_hero_ids,
                state.bench_hero_ids,
                store,
                goal_mode=self._goal_mode,
            )
        self._last_analysis = analysis
        needed_roles = analysis.needed_roles
        best_swap_id = analysis.best_swap.hero_id if analysis.best_swap is not None else ""

        mode_cn = "爽局" if self._goal_mode == "fun" else "冲分"
        if len(ally_ids) >= 3 and analysis.current_score > 0:
            self.team_score_lbl.setText(f"阵容 {analysis.current_score:.1f}分 · {mode_cn}")
        else:
            self.team_score_lbl.setText("爽局高上限视角" if self._goal_mode == "fun" else "冲分均衡视角")

        eff_w = self._custom_w or self.width() or self.DEFAULT_W
        is_narrow = eff_w < 342
        is_wide = eff_w >= 445
        self._last_density_bucket = self._width_bucket(eff_w)
        self._apply_header_density(eff_w)
        self.setFixedWidth(eff_w)

        def _make_section_hdr(text: str, accent: str = "#C8AA6E") -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"border: none; background: transparent; color: {accent}; "
                "font-size: 10px; font-weight: bold; padding-top: 2px;"
            )
            return lbl

        def _build_hero_row(
            hid: str,
            role_tag: str,
            *,
            is_mine: bool = False,
            gap_badge: str = "",
            swap_delta: float | None = None,
            swap_reason: str = "",
            is_best_swap: bool = False,
            compact: bool = False,
        ) -> QFrame | None:
            h = store.get_hero(hid)
            if h is None:
                return None
            disp_name = store.display_hero_name(hid, h.name) if hasattr(store, "display_hero_name") else h.name
            wr, name, rank, games = h.win_rate, disp_name, h.rank, h.games
            has_pos_swap = swap_delta is not None and swap_delta >= 1.5
            row = QFrame()
            row.setObjectName("SubCard")
            if is_mine:
                row.setStyleSheet(
                    "QFrame#SubCard { background-color: rgba(48, 36, 14, 232); "
                    "border: 1px solid #C89B3C; border-left: 3px solid #F0E6D2; border-radius: 2px; }"
                )
            elif is_best_swap:
                row.setStyleSheet(
                    "QFrame#SubCard { background-color: rgba(6, 42, 52, 236); "
                    "border: 1px solid #0AC8B9; border-left: 3px solid #FDE68A; border-radius: 2px; }"
                )
            elif gap_badge or has_pos_swap:
                row.setStyleSheet(
                    "QFrame#SubCard { background-color: rgba(6, 36, 46, 228); "
                    "border: 1px solid #0397AB; border-left: 3px solid #0AC8B9; border-radius: 2px; }"
                )
            elif compact:
                row.setStyleSheet(
                    "QFrame#SubCard { background-color: rgba(8, 18, 30, 196); "
                    "border: 1px solid rgba(120, 90, 40, 95); border-radius: 2px; }"
                )

            vl = QVBoxLayout(row)
            vl.setContentsMargins(7, 4 if not compact else 3, 7, 4 if not compact else 3)
            vl.setSpacing(2)

            hl = QHBoxLayout()
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setSpacing(3 if is_narrow else 5)

            tier = score_hero_tier(wr)
            t_lbl = QLabel(tier)
            t_lbl.setFixedWidth(26)
            t_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t_lbl.setStyleSheet(tier_badge_qss(tier))

            ico_size = 20 if (compact or is_narrow) else (28 if is_wide else 24)
            ico_lbl = QLabel()
            ico_lbl.setFixedSize(ico_size, ico_size)
            ico_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = self._get_portrait(
                hid,
                store,
                "#F0E6D2" if is_mine else ("#0AC8B9" if (gap_badge or has_pos_swap) else "#785A28"),
            )
            if pm is not None:
                ico_lbl.setPixmap(
                    pm.scaled(
                        ico_size,
                        ico_size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            else:
                ico_lbl.setText(name[:1])
                ico_lbl.setStyleSheet(
                    f"background-color: #091428; border: 1px solid #785A28; border-radius: {ico_size // 2}px; "
                    "color: #C8AA6E; font-size: 10px; font-weight: bold;"
                )

            n_lbl = QLabel(name)
            n_lbl.setStyleSheet(
                f"border: none; background: transparent; font-weight: bold; color: #F0E6D2; "
                f"font-size: {'11px' if is_narrow else ('13px' if is_wide else '12px')};"
            )

            hl.addWidget(t_lbl)
            hl.addWidget(ico_lbl)
            hl.addWidget(n_lbl)

            if gap_badge:
                gb_lbl = QLabel(f"[{gap_badge}]")
                gb_lbl.setStyleSheet(
                    "background-color: rgba(10, 200, 185, 42); border: 1px solid #0AC8B9; "
                    "border-radius: 2px; padding: 0px 4px; color: #A5F3FC; font-size: 9px; font-weight: bold;"
                )
                hl.addWidget(gb_lbl)

            if has_pos_swap and swap_delta is not None:
                if gap_badge or is_narrow or not swap_reason:
                    gain_txt = f"+{swap_delta:.1f}分"
                else:
                    gain_txt = f"+{swap_delta:.1f}分 · {swap_reason}"
                gain_lbl = QLabel(gain_txt)
                gain_lbl.setStyleSheet(
                    "background-color: rgba(200, 155, 60, 48); border: 1px solid #C8AA6E; "
                    "border-radius: 2px; padding: 0px 4px; color: #FDE68A; font-size: 9px; font-weight: bold;"
                )
                if swap_reason:
                    gain_lbl.setToolTip(f"换取后整队 +{swap_delta:.1f}分 · {swap_reason}")
                hl.addWidget(gain_lbl)
            elif not gap_badge and role_tag:
                tag_lbl = QLabel(role_tag)
                tag_lbl.setStyleSheet(
                    "border: none; background: transparent; color: #FDE68A; font-size: 10px; font-weight: bold;"
                    if is_mine
                    else "border: none; background: transparent; color: #94A3B8; font-size: 10px;"
                )
                hl.addWidget(tag_lbl)

            hl.addStretch(1)
            wr_color = "#0AC8B9" if wr >= 0.51 else ("#FCA5A5" if wr < 0.475 else "#F0E6D2")
            wr_lbl = QLabel(f"{wr * 100:.1f}%")
            wr_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {wr_color}; font-weight: bold; "
                f"font-size: {'11px' if is_narrow else ('13px' if is_wide else '12px')};"
            )
            meta_txt = (
                f"#{rank}"
                if is_narrow
                else (f"#{rank} · {games:,}场" if is_wide else f"#{rank} · {max(1, games // 1000)}k")
            )
            meta_lbl = QLabel(meta_txt)
            meta_lbl.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 10px;")
            hl.addWidget(wr_lbl)
            hl.addWidget(meta_lbl)
            vl.addLayout(hl)

            # Veteran micro-line: ARAM hidden balance modifiers + Hex Mayhem ceiling tag
            bal = get_aram_balance(hid)
            bal_txt, bal_sent = bal.format_badge(max_tokens=(2 if is_narrow else (4 if is_wide else 3))) if bal else ("", "neutral")
            ceil_txt = get_hero_ceiling_tag(hid)
            if bal_txt or ceil_txt:
                ml = QHBoxLayout()
                ml.setContentsMargins(31, 0, 0, 0)
                ml.setSpacing(6)
                if bal_txt:
                    b_color = (
                        "#34D399"
                        if bal_sent == "buff"
                        else ("#FCA5A5" if bal_sent == "nerf" else "#FDE68A")
                    )
                    b_lbl = QLabel(bal_txt)
                    b_lbl.setStyleSheet(
                        f"border: none; background: transparent; color: {b_color}; font-size: 9px; font-weight: bold;"
                    )
                    ml.addWidget(b_lbl)
                ml.addStretch(1)
                if ceil_txt:
                    c_lbl = QLabel(f"⚡{ceil_txt}")
                    c_lbl.setStyleSheet(
                        "border: none; background: transparent; color: #5EEAD4; font-size: 9px; font-weight: bold;"
                    )
                    ml.addWidget(c_lbl)
                vl.addLayout(ml)

            return row

        seen: set[str] = set()
        added_widgets: list[QWidget] = []

        def _append_widget(w: QWidget) -> None:
            self.rows_container.addWidget(w)
            w.show()
            added_widgets.append(w)

        # 1. Pinned Top: My currently locked champion
        if state.my_hero_id:
            seen.add(state.my_hero_id)
            my_row = _build_hero_row(state.my_hero_id, "锁定", is_mine=True)
            if my_row is not None:
                _append_widget(_make_section_hdr("◈ 我的当前锁定", "#FDE68A"))
                _append_widget(my_row)

        # 2. Actionable Bench Section: positive swap-gain + gap-fillers boosted, then win-rate sorted
        bench_items: list[tuple[int, float, int, float, str, str, float | None, str]] = []
        for hid in state.bench_hero_ids:
            if not hid or hid in seen:
                continue
            h = store.get_hero(hid)
            if h is None:
                continue
            seen.add(hid)
            h_role = classify_hero_role(hid, store)
            gap_badge = _ROLE_GAP_BADGE.get(h_role, "") if h_role in needed_roles else ""
            sd_entry = analysis.swap_deltas.get(hid)
            swap_delta = sd_entry[0] if sd_entry is not None else None
            swap_reason = sd_entry[1] if sd_entry is not None else ""
            has_pos_gain = 1 if (swap_delta is not None and swap_delta >= 1.5) else 0
            sort_delta = swap_delta if has_pos_gain and swap_delta is not None else -999.0
            bench_items.append(
                (has_pos_gain, sort_delta, 1 if gap_badge else 0, h.win_rate, hid, gap_badge, swap_delta, swap_reason)
            )

        if bench_items:
            bench_items.sort(key=lambda it: (it[0], it[1], it[2], it[3]), reverse=True)
            _append_widget(_make_section_hdr(f"◈ 备战席 · 可立即换取 ({len(bench_items)})", "#0AC8B9"))
            for _pg, _sd, _prio, _wr, hid, gap_badge, swap_delta, swap_reason in bench_items[:8]:
                brow = _build_hero_row(
                    hid,
                    "备战",
                    gap_badge=gap_badge,
                    swap_delta=swap_delta,
                    swap_reason=swap_reason,
                    is_best_swap=(hid == best_swap_id),
                )
                if brow is not None:
                    _append_widget(brow)

        # 3. Teammates Reference Section (visually separated below bench)
        mate_items: list[tuple[float, str]] = []
        for hid in state.team_hero_ids:
            if not hid or hid in seen:
                continue
            h = store.get_hero(hid)
            if h is None:
                continue
            seen.add(hid)
            mate_items.append((h.win_rate, hid))

        if mate_items:
            mate_items.sort(key=lambda it: it[0], reverse=True)
            _append_widget(_make_section_hdr(f"◈ 队友已选 ({len(mate_items)})", "#A09B8C"))
            for _wr, hid in mate_items[:4]:
                mrow = _build_hero_row(hid, "队友", compact=True)
                if mrow is not None:
                    _append_widget(mrow)

        # 4. Collapsible Best-5 strip (C(n,5) optimal lineup preview)
        self._render_best_five(analysis.best_five, ally_set, store, is_narrow=is_narrow)

        # 5. Unified Action Footer (replaces static-only alert with actionable swap decision)
        if analysis.summary_text:
            self.comp_alert_lbl.setText(analysis.summary_text)
            if analysis.is_warn:
                style_key = "warn"
            elif analysis.best_swap is not None and analysis.summary_text.startswith("⚡"):
                style_key = "swap"
            else:
                style_key = "ok"
            if style_key != self._last_comp_alert_style:
                self._last_comp_alert_style = style_key
                self.comp_alert_lbl.setStyleSheet(_COMP_ALERT_QSS[style_key])
            self.comp_alert_lbl.show()
        else:
            self.comp_alert_lbl.hide()

        if state.my_hero_id:
            spells = store.get_hero_spells(state.my_hero_id)[:2]
            if spells:
                parts = []
                for sp in spells:
                    s1 = store.static.get_spell_name(sp.spell_ids[0])
                    s2 = store.static.get_spell_name(sp.spell_ids[1])
                    parts.append(f"{s1}+{s2} ({sp.win_rate * 100:.1f}%)")
                self.spell_lbl.setText("◈ 推荐召唤师技能：" + "  |  ".join(parts))
            else:
                self.spell_lbl.setText("")
        else:
            self.spell_lbl.setText("◇ 等待锁定英雄...")
        self.spell_lbl.setVisible(bool(self.spell_lbl.text()))

        # Explicitly measure every shown row widget so live updates while visible NEVER collapse to 60px!
        self.rows_container.invalidate()
        self.rows_container.activate()
        spacing = max(0, self.rows_container.spacing())
        explicit_sum_h = 0
        for idx, w in enumerate(added_widgets):
            lay_w = w.layout()
            if lay_w is not None:
                lay_w.invalidate()
                lay_w.activate()
            wh = max(w.sizeHint().height(), w.minimumSizeHint().height(), 18)
            explicit_sum_h += wh + (spacing if idx > 0 else 0)
        body_hint_h = self.rows_body.sizeHint().height()
        content_h = max(explicit_sum_h, body_hint_h) + 8
        self._sync_window_height(content_h)

    def position_beside_lcu(self, lcu_phys_rect: tuple[int, int, int, int]) -> None:
        """Dock cleanly beside LCU in logical DPI coordinates, flipping left or clamping to stay on-screen."""
        scr = screen_for_physical_rect(lcu_phys_rect) or QGuiApplication.primaryScreen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        lcu_log, _dpr = physical_rect_to_logical(lcu_phys_rect, screen=scr)
        w = max(self.width(), self._custom_w or self.DEFAULT_W)
        h = max(self.height(), self.sizeHint().height(), self.MIN_H)

        right_x = lcu_log.right() + 10
        left_x = lcu_log.left() - w - 10
        if right_x + w <= geo.right() - 4:
            x = right_x
        elif left_x >= geo.left() + 4:
            x = left_x
        else:
            x = right_x
        x, y = clamp_to_screen_rect(x, lcu_log.top(), w, h, geo, margin=8)
        self.move(x, y)

    def ensure_on_screen(self) -> None:
        scr = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if scr is None:
            return
        geo = scr.availableGeometry()
        w = max(self.width(), self._custom_w or self.DEFAULT_W)
        h = max(self.height(), self.sizeHint().height(), 180)
        cx, cy = clamp_to_screen_rect(self.x(), self.y(), w, h, geo, margin=6)
        if cx != self.x() or cy != self.y():
            self.move(cx, cy)

    def set_screen_recording_allowed(self, allowed: bool) -> None:
        self._allow_screen_recording = bool(allowed)
        if self.isVisible() or bool(self.internalWinId()):
            apply_capture_exclusion(self, exclude=not self._allow_screen_recording)

    def show_overlay(self) -> None:
        self.ensure_on_screen()
        if self.windowOpacity() < 0.99:
            self.setWindowOpacity(1.0)
        show_no_activate(
            self,
            click_through=False,
            exclude_from_capture=not self._allow_screen_recording,
        )
        self._layout_corner_grips()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._gesture_start_norm_rect = self._capture_norm_rect()[0]
            gpos = event.globalPosition().toPoint()
            self._drag_press_global = gpos
            self._drag_offset = gpos - self.frameGeometry().topLeft()
            self._did_drag = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            menu.setStyleSheet(HEXTECH_MENU_QSS)
            a_reset_size = QAction("恢复默认尺寸", menu)
            a_reset_size.triggered.connect(self.reset_default_size)
            a_hide = QAction("隐藏选人悬浮窗", menu)
            a_hide.triggered.connect(self._close_by_user)
            menu.addAction(a_reset_size)
            menu.addSeparator()
            menu.addAction(a_hide)
            menu.exec(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 38:
            self._drag_offset = None
            self._drag_press_global = None
            self._did_drag = False
            self._gesture_start_norm_rect = None
            self.reset_default_size()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            gpos = event.globalPosition().toPoint()
            if not self._did_drag:
                if self._drag_press_global is None or (gpos - self._drag_press_global).manhattanLength() < 4:
                    event.accept()
                    return
                self._did_drag = True
            pos = gpos - self._drag_offset
            scr = QGuiApplication.screenAt(gpos) or QGuiApplication.primaryScreen()
            if scr is not None:
                geo = scr.availableGeometry()
                cx, cy = clamp_to_screen_rect(pos.x(), pos.y(), self.width(), self.height(), geo, margin=4)
                pos = QPoint(cx, cy)
            self.move(pos)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            did_drag = self._did_drag
            self._drag_offset = None
            self._drag_press_global = None
            self._did_drag = False
            if not did_drag:
                self._gesture_start_norm_rect = None
                event.accept()
                return
            if self._on_pos_changed:
                self._on_pos_changed([self.x(), self.y()])
            self._emit_geometry_changed("drag")
            event.accept()
            return
        super().mouseReleaseEvent(event)
