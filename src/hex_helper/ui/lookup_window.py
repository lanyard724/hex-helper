"""Authentic League of Legends V16.23 Client-styled Frameless Main Window with custom Hextech widgets, fonts, shadows & 60fps motion."""

from __future__ import annotations

import math
import threading
from typing import Any, Callable

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from hex_helper.bridge.protocol import CoachAdvicePayload
from hex_helper.config import (
    GEMINI_MODELS,
    GEMINI_THINKING_LEVELS,
    AppConfig,
    HotkeyConfig,
    format_display_patch,
    get_app_data_dir,
    mask_api_key,
)
from hex_helper.data.hero_signatures import get_hero_signature_db
from hex_helper.data.provider import DatasetDescriptor
from hex_helper.data.special_builds import SpecialAugmentBuild, get_special_augment_db
from hex_helper.data.store import DataStore
from hex_helper.edition import APP_VERSION, is_commercial_edition
from hex_helper.game.lol_locator import clear_locator_cache, detect_primary_lol_install, validate_lol_install_dir
from hex_helper.scoring.build_synergy import normalize_item_id
from hex_helper.scoring.destiny import rank_destiny_items
from hex_helper.scoring.tier import score_hero_augment, score_hero_tier
from hex_helper.ui.account_dialogs import (
    ESTIMATED_CREDITS_PER_MATCH,
    estimate_remaining_matches,
    format_balance_match_friendly,
)
from hex_helper.ui.hex_seal_overlay import (
    CoreEnergyState,
    CoreLifecycleSnapshot,
    HextechSealOverlay,
    HextechSealSandboxDialog,
    SealMode,
)
from hex_helper.ui.hextech_widgets import (
    HextechActionButton,
    HextechInteractiveCard,
    HextechModeTab,
    HextechNavButton,
    HextechPlayButton,
    HextechPulseFrame,
    HextechShimmerBar,
    attach_hextech_shadow,
)
from hex_helper.ui.hotkey_settings import format_hotkeys_summary
from hex_helper.ui.theme import (
    BASE_OVERLAY_QSS,
    RARITY_COLORS,
    display_font,
    init_hextech_fonts,
    latin_serif_font,
    load_app_icon,
    load_app_pixmap,
    tier_badge_qss,
)
from hex_helper.ui.win32_window import UiBridgeInvoker


def _circular_icon(path_str: str, size: int = 62, border_hex: str = "#C8AA6E") -> QPixmap:
    src = QPixmap(path_str)
    if src.isNull():
        return QPixmap()
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
    clip.addEllipse(QRectF(2, 2, size - 4, size - 4))
    p.setClipPath(clip)
    p.drawPixmap(0, 0, scaled)
    p.setClipping(False)
    # Double metallic gold bevel ring
    ring = QLinearGradient(0, 0, size, size)
    ring.setColorAt(0.0, QColor("#F0E6D2"))
    ring.setColorAt(0.45, QColor(border_hex))
    ring.setColorAt(1.0, QColor("#785A28"))
    p.setPen(QPen(QBrush(ring), 2.4))
    p.drawEllipse(QRectF(2, 2, size - 4, size - 4))
    p.end()
    return out


class _HextechCanvasWidget(QWidget):
    """Paints the LoL V16.23 deep slate-blue radial aura into a cached QPixmap for 0.2ms zero-lag blits."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bg_cache: QPixmap | None = None
        self._motes = (
            (0.28, 0.24, 1.5, 48),
            (0.44, 0.18, 1.8, 56),
            (0.62, 0.30, 1.4, 44),
            (0.76, 0.22, 1.6, 52),
            (0.54, 0.42, 1.3, 38),
            (0.84, 0.48, 1.5, 42),
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._bg_cache = None
        super().resizeEvent(event)

    def _rebuild_cache(self) -> QPixmap:
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg = QLinearGradient(0.0, 0.0, 0.0, float(h))
        bg.setColorAt(0.0, QColor("#040E1A"))
        bg.setColorAt(0.40, QColor("#09192D"))
        bg.setColorAt(1.0, QColor("#020810"))
        p.fillRect(0, 0, w, h, QBrush(bg))

        glow = QRadialGradient(QPointF(w * 0.55, h * 0.30), max(w, h) * 0.46)
        glow.setColorAt(0.0, QColor(14, 160, 188, 46))
        glow.setColorAt(0.45, QColor(10, 52, 86, 24))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(0, 0, w, h, QBrush(glow))

        p.setPen(Qt.PenStyle.NoPen)
        for mx, my, radius, alpha in self._motes:
            p.setBrush(QColor(200, 170, 110, alpha))
            p.drawEllipse(QPointF(w * mx, h * my), radius, radius)

        p.setPen(QPen(QColor("#463714"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0))

        top_gold = QLinearGradient(0.0, 0.0, float(w), 0.0)
        top_gold.setColorAt(0.0, QColor("#463714"))
        top_gold.setColorAt(0.22, QColor("#C8AA6E"))
        top_gold.setColorAt(0.50, QColor("#F0E6D2"))
        top_gold.setColorAt(0.78, QColor("#C89B3C"))
        top_gold.setColorAt(1.0, QColor("#463714"))
        p.fillRect(QRectF(0.0, 0.0, float(w), 2.0), QBrush(top_gold))
        p.end()
        self._bg_cache = pm
        return pm

    def paintEvent(self, _event) -> None:  # noqa: N802
        pm = self._bg_cache or self._rebuild_cache()
        p = QPainter(self)
        p.drawPixmap(0, 0, pm)
        p.end()


class LookupWindow(QWidget):
    """Frameless LoL V16.23 Client-inspired main window with custom Hextech controls, fonts & animations."""

    def __init__(
        self,
        cfg: AppConfig,
        on_save_settings: Callable[[AppConfig], None] | None = None,
        on_sync_now: Callable[[], None] | None = None,
        on_open_calibration: Callable[[], None] | None = None,
        on_test_coach: Callable[[], None] | None = None,
        on_test_gemini: Callable[[AppConfig], None] | None = None,
        on_toggle_drawer: Callable[[], None] | None = None,
        on_toggle_combo: Callable[[], None] | None = None,
        on_switch_dataset: Callable[[str, str, str], None] | None = None,
        on_login_click: Callable[[], None] | None = None,
        on_logout_click: Callable[[], None] | None = None,
        on_recharge_click: Callable[[], None] | None = None,
        on_switch_tier: Callable[[str], None] | None = None,
        on_check_update: Callable[[], None] | None = None,
        on_rescan_lol: Callable[[], None] | None = None,
        on_open_recap: Callable[[], None] | None = None,
        on_core_action: Callable[[str], None] | None = None,
        on_unseal_complete: Callable[[], None] | None = None,
        on_dismiss_newbie_banner: Callable[[], None] | None = None,
        on_open_hotkeys: Callable[[], None] | None = None,
        on_open_feedback: Callable[[], None] | None = None,
        on_reset_overlay_sizes: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        init_hextech_fonts()
        self._cfg = cfg
        self._commercial = is_commercial_edition()
        self._on_save_settings = on_save_settings
        self._on_sync_now = on_sync_now
        self._on_open_calibration = on_open_calibration
        self._on_test_coach = on_test_coach
        self._on_test_gemini = on_test_gemini
        self._on_toggle_drawer = on_toggle_drawer
        self._on_toggle_combo = on_toggle_combo
        self._on_switch_dataset = on_switch_dataset
        self._on_login_click = on_login_click
        self._on_logout_click = on_logout_click
        self._on_recharge_click = on_recharge_click
        self._on_switch_tier = on_switch_tier
        self._on_check_update = on_check_update
        self._on_rescan_lol = on_rescan_lol
        self._on_open_recap = on_open_recap
        self._on_core_action = on_core_action
        self._on_unseal_complete = on_unseal_complete
        self._on_dismiss_newbie_banner = on_dismiss_newbie_banner
        self._on_open_hotkeys = on_open_hotkeys
        self._on_open_feedback = on_open_feedback
        self._on_reset_overlay_sizes = on_reset_overlay_sizes
        self.tab2_push_btn: HextechActionButton | None = None
        self.hotkeys_summary_lbl: QLabel | None = None
        self.open_hotkeys_btn: HextechActionButton | None = None
        self.diag_hdr_lbl: QLabel | None = None
        self._lifecycle: CoreLifecycleSnapshot | None = None
        self._lobby_seal_cards: list[HextechPulseFrame] = []
        self._lobby_seals: list[HextechSealOverlay] = []
        self.seal_shelf_wrap: QWidget | None = None
        self.seal_active_bar: QFrame | None = None
        self.newbie_welcome_bar: QFrame | None = None
        self._domino_unseal_in_progress: bool = False
        self._recap_available: bool = False
        self._recap_summary: str = ""
        self._store: DataStore | None = None
        self._datasets: list[DatasetDescriptor] = []
        self._drag_offset: QPoint | None = None
        self._selected_hero_id: str | None = None
        self._ui_invoker = UiBridgeInvoker()
        self._icon_cache: dict[str, QPixmap] = {}
        self._item_icon_cache: dict[tuple[str, int], QPixmap] = {}
        self._fetching_item_icons: set[str] = set()
        self._fade_anim: QPropertyAnimation | None = None
        self._normal_patch: str = "latest"
        self._prism_patch: str = "latest"
        self._high_band_patch: str = "16.22"
        self._special_category_filter: str = "全部"
        self._gemini_form_collapsed: bool = False if self._commercial else bool(cfg.resolved_gemini_api_key())
        self._coach_history: list[dict[str, str]] = []
        self._cutoff_code: str = ""
        self._cutoff_policy: str = "ai_only"
        self._is_full_locked: bool = False
        self._update_available: bool = False
        self._lol_rescan_in_flight: bool = False
        self._closed: bool = False
        self._account_username: str | None = None
        self._account_wallet: dict[str, Any] | None = None

        # Dev-only references (kept None in commercial mode so zero API/proxy widgets exist)
        self.gemini_config_card = None
        self.gemini_key_edit = None
        self.gemini_model_combo = None
        self.gemini_thinking_combo = None
        self.coach_mode_combo = None
        self.test_gemini_btn = None
        self.gemini_adaptive_chk = None
        self.toggle_gemini_form_btn = None
        self.gemini_conn_status_lbl = None
        self.proxy_edit = None

        # Commercial-only references
        self.account_card = None
        self.tier_auto_btn = None
        self.tier_a_btn = None
        self.tier_b_btn = None
        self.tier_info_card = None
        self.tier_info_lbl = None
        self.login_btn = None
        self.recharge_btn = None
        self.logout_btn = None
        self.check_update_btn = None
        self.rescan_lol_btn = None

        self.setWindowTitle(f"HexHelper v{APP_VERSION} · 海克斯大乱斗客户端")
        self.setWindowIcon(load_app_icon())
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1140, 730)
        self.setMinimumSize(940, 610)

        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QListWidget {
                background-color: rgba(1, 10, 19, 222);
                border: 1px solid #463714;
                border-radius: 2px;
                outline: 0;
            }
            QListWidget::item {
                padding: 7px 10px;
                border-bottom: 1px solid rgba(70, 55, 20, 75);
                color: #F0E6D2;
            }
            QListWidget::item:hover {
                background-color: rgba(18, 40, 62, 215);
            }
            QListWidget::item:selected {
                background-color: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(200, 170, 110, 55),
                    stop:1 rgba(12, 34, 54, 230)
                );
                border-left: 3px solid #C8AA6E;
                color: #FFFFFF;
            }
            QTextEdit {
                background-color: rgba(1, 10, 19, 220);
                border: 1px solid #463714;
                border-radius: 2px;
                color: #F0E6D2;
            }
            """
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.canvas = _HextechCanvasWidget()
        outer.addWidget(self.canvas)

        root = QVBoxLayout(self.canvas)
        root.setContentsMargins(1, 2, 1, 1)
        root.setSpacing(0)

        # === 1. Authentic LoL Client Top Navigation Bar ===
        self.topbar = QFrame()
        self.topbar.setFixedHeight(66)
        self.topbar.setStyleSheet(
            "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(3, 12, 22, 240), stop:1 rgba(1, 10, 19, 230)); "
            "border-bottom: 1px solid rgba(120, 90, 40, 165); }"
        )
        tb = QHBoxLayout(self.topbar)
        tb.setContentsMargins(14, 0, 12, 0)
        tb.setSpacing(6)

        # Authentic custom-painted LoL Hextech chevron button (toggles in-game Edge Drawer)
        self.play_btn = HextechPlayButton("局内浮窗")
        self.play_btn.setToolTip("点击展开 / 收起局内右侧出装与参谋悬浮抽屉 (F3)")
        self.play_btn.clicked.connect(lambda: self._on_toggle_drawer and self._on_toggle_drawer())
        tb.addWidget(self.play_btn)
        tb.addSpacing(10)

        # Sculpted LoL Nav Tabs with top golden spotlight cone
        self.nav_btns: list[HextechNavButton] = []
        for idx, label in enumerate(("英雄速查", "特殊流派库", "对局与参谋", "设置与诊断")):
            btn = HextechNavButton(label, active=(idx == 0))
            btn.clicked.connect(lambda _=False, i=idx: self._switch_page(i))
            self.nav_btns.append(btn)
            tb.addWidget(btn)

        tb.addStretch(1)

        # Right Summoner-Card Live Status Box (clickable to jump to AI Coach / Claim 450 / Recharge)
        self.status_box = HextechPulseFrame()
        self.status_box.setObjectName("TopStatusBox")
        self.status_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.status_box.setToolTip("点击前往对局参谋 / 账号状态页")
        self.status_box.setStyleSheet(
            "QFrame#TopStatusBox { background-color: rgba(7, 17, 32, 195); "
            "border-left: 1px solid rgba(120, 90, 40, 140); border-radius: 2px; }"
            "QFrame#TopStatusBox:hover { background-color: rgba(12, 30, 48, 225); "
            "border-left: 1px solid #C8AA6E; }"
        )
        self.status_box.mousePressEvent = lambda ev: self._on_status_box_clicked(ev)  # type: ignore[method-assign]
        sb_lay = QVBoxLayout(self.status_box)
        sb_lay.setContentsMargins(10, 6, 10, 6)
        sb_lay.setSpacing(2)
        self.status_title_lbl = QLabel("HEXHELPER · 26.23")
        self.status_title_lbl.setFont(latin_serif_font(10, bold=True, letter_spacing=1.2))
        self.status_title_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
        self.coach_badge_lbl = QLabel(
            "◇ 领 450 积分体验 · 点击登录" if self._commercial else "● 本地 5ms 引擎运行中"
        )
        self.coach_badge_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 11px;")
        sb_lay.addWidget(self.status_title_lbl)
        sb_lay.addWidget(self.coach_badge_lbl)
        tb.addWidget(self.status_box)

        min_btn = QPushButton("—")
        min_btn.setObjectName("WinCtrlBtn")
        min_btn.setFixedSize(30, 30)
        min_btn.clicked.connect(self.showMinimized)
        close_btn = QPushButton("×")
        close_btn.setObjectName("WinCtrlBtn")
        close_btn.setFixedSize(30, 30)
        close_btn.setToolTip("最小化到托盘")
        close_btn.clicked.connect(self.hide)
        tb.addWidget(min_btn)
        tb.addWidget(close_btn)
        root.addWidget(self.topbar)

        # === 2. Sub-Nav Bar with Custom Painted Diamond Selectors ===
        subbar = QFrame()
        subbar.setFixedHeight(38)
        subbar.setStyleSheet(
            "QFrame { background-color: rgba(4, 12, 23, 185); border-bottom: 1px solid rgba(70, 55, 20, 125); }"
        )
        sb = QHBoxLayout(subbar)
        sb.setContentsMargins(18, 0, 16, 0)
        sb.setSpacing(18)

        self.mode_normal_btn = HextechModeTab("26.23 常规模式", active=True)
        self.mode_normal_btn.clicked.connect(lambda: self._trigger_mode_switch(self._normal_patch, "normal", ""))
        self.mode_prism_btn = HextechModeTab("26.23 首次必棱彩", active=False)
        self.mode_prism_btn.clicked.connect(lambda: self._trigger_mode_switch(self._prism_patch, "first-prismatic", ""))
        self.mode_high_btn = HextechModeTab("26.22 前5%高分段", active=False)
        self.mode_high_btn.clicked.connect(lambda: self._trigger_mode_switch(self._high_band_patch, "normal", "high"))

        self.status_lbl = QLabel("数据加载中...")
        self.status_lbl.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 11px;")

        self.recap_btn = QPushButton("🏆 战报卡")
        self.recap_btn.setFixedHeight(24)
        self.recap_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recap_btn.clicked.connect(self._handle_recap_clicked)
        self.set_recap_available(False)

        sb.addWidget(self.mode_normal_btn)
        sb.addWidget(self.mode_prism_btn)
        sb.addWidget(self.mode_high_btn)
        sb.addStretch(1)
        sb.addWidget(self.status_lbl)
        sb.addWidget(self.recap_btn)
        root.addWidget(subbar)

        # === 3. Main Stacked Pages ===
        self.pages = QStackedWidget()
        self.pages.setStyleSheet("background: transparent;")
        root.addWidget(self.pages, 1)

        self._build_hero_browser_page()
        self._build_special_builds_page()
        self._build_coach_page()
        self._build_settings_page()

        # === 4. Bottom LoL Client Footer Bar with Double-Bevel Metallic Buttons ===
        footer = QFrame()
        footer.setFixedHeight(54)
        footer.setStyleSheet(
            "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
            "stop:0 rgba(2, 10, 20, 236), stop:1 rgba(1, 7, 14, 245)); "
            "border-top: 1px solid rgba(120, 90, 40, 155); }"
        )
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(16, 8, 12, 8)
        fl.setSpacing(10)

        self.cal_btn = HextechActionButton("校准三选一 F8", variant="gold", height=34)
        self.cal_btn.clicked.connect(lambda: self._on_open_calibration and self._on_open_calibration())
        sync_btn = HextechActionButton("同步最新数据", variant="gold", height=34)
        sync_btn.clicked.connect(lambda: self._on_sync_now and self._on_sync_now())
        self.feedback_btn = HextechActionButton("问题反馈", variant="gold", height=34)
        self.feedback_btn.setToolTip("遇到识别异常或报错？点击一键反馈并附带最近 20 局脱敏运行日志")
        self.feedback_btn.clicked.connect(lambda: self._on_open_feedback and self._on_open_feedback())
        self.footer_coach_btn = HextechActionButton("呼叫参谋推演 F6", variant="cyan", pointed_right=True, height=36)
        self.footer_coach_btn.setMinimumWidth(172)
        self.footer_coach_btn.clicked.connect(lambda: self._on_test_coach and self._on_test_coach())

        self.version_lbl = QLabel(f"v{APP_VERSION} · 26.23")
        self.version_lbl.setFont(latin_serif_font(9, bold=True, letter_spacing=1.5))
        self.version_lbl.setStyleSheet("border: none; background: transparent; color: #5B5A56;")
        grip = QSizeGrip(self)
        grip.setFixedSize(14, 14)

        fl.addWidget(self.cal_btn)
        fl.addWidget(sync_btn)
        fl.addWidget(self.feedback_btn)
        fl.addStretch(1)
        fl.addWidget(self.footer_coach_btn)
        fl.addSpacing(14)
        fl.addWidget(self.version_lbl)
        fl.addWidget(grip)
        root.addWidget(footer)
        self.update_hotkeys_display(self._cfg.hotkeys)

    def _build_lock_all_notice_card(self, section_title: str) -> tuple[HextechInteractiveCard, QLabel, HextechActionButton]:
        card = HextechInteractiveCard("#F59E0B")
        card.setFixedWidth(540)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(32, 32, 32, 32)
        cl.setSpacing(14)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        ttl = QLabel(f"◈ {section_title}已暂停访问")
        ttl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ttl.setFont(display_font(15, bold=True, letter_spacing=1.0))
        ttl.setStyleSheet("border: none; background: transparent; color: #FDE68A;")

        desc = QLabel("当前账号未登录或算力额度不足，请先登录或补充算力以解锁全部功能")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-size: 13px;")

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        act_btn = HextechActionButton("立即补充算力", variant="cyan", pointed_right=True, height=34)
        act_btn.setMinimumWidth(132)
        act_btn.clicked.connect(self._handle_cutoff_action)
        goto_btn = HextechActionButton("前往账号与算力页", variant="gold", height=34)
        goto_btn.setMinimumWidth(148)
        goto_btn.clicked.connect(lambda: self._switch_page(2, force=True))
        btn_row.addWidget(act_btn)
        btn_row.addWidget(goto_btn)

        cl.addWidget(ttl)
        cl.addWidget(desc)
        cl.addLayout(btn_row)
        card.hide()
        return card, desc, act_btn

    def _build_hero_browser_page(self) -> None:
        page = QWidget()
        outer_lay = QVBoxLayout(page)
        outer_lay.setContentsMargins(14, 12, 14, 12)
        outer_lay.setSpacing(8)

        # Gentle dismissible guest welcome strip (Tab 0 only for S0 unauthenticated, zero locks on heroes!)
        if self._commercial:
            nb = QFrame()
            nb.setFixedHeight(32)
            nb.setStyleSheet(
                "QFrame { background-color: rgba(6, 26, 40, 224); border: 1px solid rgba(10, 200, 185, 130); "
                "border-left: 3px solid #C8AA6E; border-radius: 2px; }"
            )
            nbl = QHBoxLayout(nb)
            nbl.setContentsMargins(10, 2, 8, 2)
            nbl.setSpacing(10)
            nl = QLabel("◈ 英雄大数据与本地 5ms 出装永久免费 · 新人注册即赠 450 积分（约 2 局智能 AI 开局定盘与精准散件）")
            nl.setStyleSheet("border: none; background: transparent; color: #E2D9C5; font-size: 11px; font-weight: bold;")
            claim_btn = HextechActionButton("免费领 450 积分", variant="cyan", height=22)
            claim_btn.clicked.connect(lambda: self._emit_core_action("register"))
            close_nb_btn = QPushButton("×")
            close_nb_btn.setFixedSize(20, 20)
            close_nb_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            close_nb_btn.setToolTip("永久关闭此提示")
            close_nb_btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; color: #A09B8C; font-size: 13px; }"
                "QPushButton:hover { color: #F0E6D2; }"
            )
            close_nb_btn.clicked.connect(self._dismiss_newbie_welcome)
            nbl.addWidget(nl, 1)
            nbl.addWidget(claim_btn)
            nbl.addWidget(close_nb_btn)
            nb.setVisible(not self._cfg.newbie_banner_dismissed)
            self.newbie_welcome_bar = nb
            outer_lay.addWidget(nb)

        self.hero_browser_body = QWidget()
        self.hero_browser_body.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(self.hero_browser_body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        left_col = QVBoxLayout()
        left_col.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("◇ 搜索英雄（中文名 / 称号）...")
        self.search_edit.setFixedHeight(34)
        self.search_edit.textChanged.connect(self._filter_heroes)
        self.hero_list = QListWidget()
        self.hero_list.setFixedWidth(238)
        self.hero_list.currentItemChanged.connect(self._on_hero_selected)
        attach_hextech_shadow(self.hero_list, blur_radius=16.0, y_offset=4.0)
        left_col.addWidget(self.search_edit)
        left_col.addWidget(self.hero_list, 1)
        lay.addLayout(left_col)

        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail_scroll.viewport().setStyleSheet("background: transparent;")
        self.detail_container = QWidget()
        self.detail_container.setStyleSheet("background: transparent;")
        self.detail_lay = QVBoxLayout(self.detail_container)
        self.detail_lay.setContentsMargins(4, 2, 10, 10)
        self.detail_lay.setSpacing(10)
        self.detail_scroll.setWidget(self.detail_container)
        lay.addWidget(self.detail_scroll, 1)

        placeholder = QLabel("◇ 请在左侧选择一名英雄查看 26.23 海克斯梯度与三件套路线")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setFont(display_font(12, bold=False, letter_spacing=1.2))
        placeholder.setStyleSheet("color: #A09B8C; padding: 60px;")
        self.detail_lay.addWidget(placeholder)

        self.hero_locked_card, self._hero_locked_desc_lbl, self._hero_locked_action_btn = (
            self._build_lock_all_notice_card("英雄速查")
        )
        outer_lay.addWidget(self.hero_browser_body, 1)
        outer_lay.addWidget(self.hero_locked_card, 1, Qt.AlignmentFlag.AlignCenter)
        self.pages.addWidget(page)

    def _build_special_builds_page(self) -> None:
        page = QWidget()
        outer_lay = QVBoxLayout(page)
        outer_lay.setContentsMargins(16, 14, 16, 14)
        outer_lay.setSpacing(0)

        self.special_builds_body = QWidget()
        self.special_builds_body.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(self.special_builds_body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        left = QVBoxLayout()
        left.setSpacing(8)
        self.special_search = QLineEdit()
        self.special_search.setPlaceholderText("搜索特殊海克斯 / 俗称 (如: 小蝴蝶 / 小蜜蜂 / 死亡之环 / 装甲冲击 / 没法力)...")
        self.special_search.setFixedHeight(34)
        self.special_search.textChanged.connect(self._filter_special_list)
        left.addWidget(self.special_search)

        # Category filter pills (全部 / 奶盾 / 灼烧 / 坦度 / 攻速暴击 / 蓝量 / 质变)
        cat_wrap = QWidget()
        cat_wrap.setStyleSheet("background: transparent;")
        cat_grid = QGridLayout(cat_wrap)
        cat_grid.setContentsMargins(0, 0, 0, 0)
        cat_grid.setSpacing(4)
        self._special_cat_btns: dict[str, QPushButton] = {}
        categories = ("全部", "奶盾", "灼烧", "坦度", "攻速暴击", "蓝量", "质变")
        for idx, cat in enumerate(categories):
            btn = QPushButton(cat)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, c=cat: self._set_special_category(c))
            self._special_cat_btns[cat] = btn
            cat_grid.addWidget(btn, idx // 4, idx % 4)
        left.addWidget(cat_wrap)

        self.special_list = QListWidget()
        self.special_list.setFixedWidth(256)
        self.special_list.currentRowChanged.connect(self._on_special_selected)
        left.addWidget(self.special_list, 1)
        lay.addLayout(left)

        right_col = QVBoxLayout()
        right_col.setSpacing(10)
        self.special_detail_card = HextechInteractiveCard("#0AC8B9")
        dl = QVBoxLayout(self.special_detail_card)
        dl.setContentsMargins(16, 14, 16, 14)
        dl.setSpacing(9)

        self.sp_title_lbl = QLabel("请选择左侧特殊海克斯")
        self.sp_title_lbl.setFont(display_font(16, bold=True, letter_spacing=0.8))
        self.sp_title_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")

        self.sp_sub_lbl = QLabel("")
        self.sp_sub_lbl.setWordWrap(True)
        self.sp_sub_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 12px; font-weight: bold;")

        self.sp_mech_lbl = QLabel("")
        self.sp_mech_lbl.setWordWrap(True)
        self.sp_mech_lbl.setStyleSheet("border: none; background: transparent; color: #E2E8F0; font-size: 13px;")

        self.sp_items_hdr = QLabel("◈ 26.23 专属推荐出装（本地引擎 + 参谋同步生效）")
        self.sp_items_hdr.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: 12px;")

        self.sp_items_icons_wrap = QWidget()
        self.sp_items_icons_wrap.setStyleSheet("background: transparent; border: none;")
        self.sp_items_icons_lay = QGridLayout(self.sp_items_icons_wrap)
        self.sp_items_icons_lay.setContentsMargins(0, 2, 0, 2)
        self.sp_items_icons_lay.setHorizontalSpacing(8)
        self.sp_items_icons_lay.setVerticalSpacing(6)

        self.sp_items_lbl = QLabel("")
        self.sp_items_lbl.setWordWrap(True)
        self.sp_items_lbl.setStyleSheet(
            "background-color: rgba(1, 10, 19, 195); border: 1px solid #C89B3C; "
            "border-radius: 2px; padding: 8px 12px; color: #FDE68A; font-size: 12px; font-weight: bold;"
        )

        self.sp_avoid_lbl = QLabel("")
        self.sp_avoid_lbl.setWordWrap(True)
        self.sp_avoid_lbl.setStyleSheet(
            "background-color: rgba(10, 28, 44, 190); border: 1px solid #0397AB; "
            "border-radius: 2px; padding: 8px 12px; color: #F0E6D2; font-size: 12px;"
        )

        self.sp_tip_lbl = QLabel("")
        self.sp_tip_lbl.setWordWrap(True)
        self.sp_tip_lbl.setStyleSheet("border: none; background: transparent; color: #94A3B8; font-size: 12px;")

        dl.addWidget(self.sp_title_lbl)
        dl.addWidget(self.sp_sub_lbl)
        dl.addWidget(self.sp_mech_lbl)
        dl.addWidget(self.sp_items_hdr)
        dl.addWidget(self.sp_items_icons_wrap)
        dl.addWidget(self.sp_items_lbl)
        dl.addWidget(self.sp_avoid_lbl)
        dl.addWidget(self.sp_tip_lbl)
        right_col.addWidget(self.special_detail_card)

        db = get_special_augment_db()
        db_hint = QLabel(
            f"◈ 已加载 {len(db.entries)} 个 Patch {db.patch or '26.23'} 特殊海克斯流派配方"
            f" · 支持在 {get_app_data_dir() / 'special_augment_builds.json'} 增量自定义覆盖"
        )
        db_hint.setWordWrap(True)
        db_hint.setStyleSheet("color: #A09B8C; font-size: 11px;")
        right_col.addWidget(db_hint)
        right_col.addStretch(1)
        lay.addLayout(right_col, 1)

        self._special_entries: list[SpecialAugmentBuild] = []
        self._set_special_category("全部")

        self.special_locked_card, self._special_locked_desc_lbl, self._special_locked_action_btn = (
            self._build_lock_all_notice_card("特殊流派库")
        )
        outer_lay.addWidget(self.special_builds_body, 1)
        outer_lay.addWidget(self.special_locked_card, 1, Qt.AlignmentFlag.AlignCenter)
        self.pages.addWidget(page)

    @staticmethod
    def _matches_special_category(entry: SpecialAugmentBuild, cat: str) -> bool:
        if cat == "全部":
            return True
        blob = f"{entry.name} {entry.archetype} {entry.mechanic} {entry.playstyle_tip}"
        if cat == "奶盾":
            return any(k in blob for k in ("奶", "盾", "治疗", "护盾", "蝴蝶", "蜜蜂"))
        if cat == "灼烧":
            return any(k in blob for k in ("灼烧", "火焰", "烧", "持续伤害", "导管", "炎"))
        if cat == "坦度":
            return any(k in blob for k in ("坦", "护甲", "魔抗", "心之钢", "装甲", "生命值", "体型"))
        if cat == "攻速暴击":
            return any(k in blob for k in ("暴击", "攻速", "普攻", "特效", "射手", "穿甲"))
        if cat == "蓝量":
            return any(k in blob for k in ("法力", "蓝量", "蓝", "冷却", "技能急速"))
        if cat == "质变":
            return bool(entry.shifts_profile) or any(k in blob for k in ("质变", "转", "双修", "魔武", "变阵"))
        return True

    def _set_special_category(self, cat: str) -> None:
        self._special_category_filter = cat
        for c, btn in self._special_cat_btns.items():
            if c == cat:
                btn.setStyleSheet(
                    "QPushButton { background-color: rgba(10, 200, 185, 55); border: 1px solid #0AC8B9; "
                    "color: #F0E6D2; font-size: 11px; font-weight: bold; padding: 0px 4px; border-radius: 2px; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { background-color: rgba(8, 18, 32, 195); border: 1px solid #463714; "
                    "color: #A09B8C; font-size: 11px; padding: 0px 4px; border-radius: 2px; }"
                    "QPushButton:hover { border-color: #C8AA6E; color: #F0E6D2; }"
                )
        self._filter_special_list(self.special_search.text())

    def _filter_special_list(self, text: str) -> None:
        db = get_special_augment_db()
        base = db.search(text)
        self._special_entries = [e for e in base if self._matches_special_category(e, self._special_category_filter)]
        self.special_list.clear()
        for e in self._special_entries:
            r_label = RARITY_COLORS.get(e.rarity, ("#C8AA6E", ""))[1]
            alias_s = f" ({'/'.join(e.aliases[:2])})" if e.aliases else ""
            item = QListWidgetItem(f"[{r_label}] {e.name}{alias_s}")
            item.setData(Qt.ItemDataRole.UserRole, e.augment_id)
            self.special_list.addItem(item)
        if self._special_entries:
            self.special_list.setCurrentRow(0)

    def _on_special_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._special_entries):
            return
        e = self._special_entries[row]
        r_label = RARITY_COLORS.get(e.rarity, ("#C8AA6E", ""))[1]
        aliases = f"  ·  俗称：{' / '.join(e.aliases)}" if e.aliases else ""
        self.sp_title_lbl.setText(f"{e.name}  [{r_label}]")
        tags = []
        if e.transformative:
            tags.append("⚡ 触发自动允许变阵")
        if e.allow_off_profile:
            tags.append("支持跨系出装豁免")
        if e.shifts_profile:
            tags.append(f"定位切至 {e.shifts_profile}")
        self.sp_sub_lbl.setText(f"流派：{e.archetype}{aliases}  |  {' · '.join(tags)}")
        self.sp_mech_lbl.setText(f"机制：{e.mechanic}")

        def _fmt_item(iid: str) -> str:
            if self._store is not None:
                info = self._store.get_item_info(iid)
                return f"{info.name}({info.gold}G)" if info.gold > 0 else info.name
            return iid

        # Render real DDragon item icon strip for force_items + core_items
        while self.sp_items_icons_lay.count():
            it = self.sp_items_icons_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        ordered_ids: list[tuple[str, bool]] = [(i, True) for i in e.force_items]
        for i in e.core_items:
            if i not in e.force_items:
                ordered_ids.append((i, False))
        for idx_it, (iid, is_force) in enumerate(ordered_ids[:6]):
            cell = QFrame()
            border_c = "#0AC8B9" if is_force else "#C89B3C"
            cell.setMinimumWidth(0)
            cell.setStyleSheet(
                f"QFrame {{ background-color: rgba(4, 14, 26, 220); border: 1px solid {border_c}; border-radius: 2px; }}"
            )
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(6, 4, 8, 4)
            cl.setSpacing(6)
            ico = QLabel()
            ico.setFixedSize(26, 26)
            ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = self._get_item_pixmap(iid, 24)
            nm_short = self._store.get_item_info(iid).name if self._store else iid
            if pm is not None:
                ico.setPixmap(pm)
            else:
                ico.setText(nm_short[:1])
                ico.setStyleSheet("color: #C8AA6E; font-weight: bold; font-size: 11px; border: none;")
            txt = QLabel(f"{'★' if is_force else ''}{nm_short}")
            txt.setMinimumWidth(0)
            txt.setStyleSheet(
                "border: none; background: transparent; color: #F0E6D2; font-size: 11px; font-weight: bold;"
                if is_force
                else "border: none; background: transparent; color: #CBD5E1; font-size: 11px;"
            )
            cl.addWidget(ico)
            cl.addWidget(txt, 1)
            self.sp_items_icons_lay.addWidget(cell, idx_it // 3, idx_it % 3)
        for col in range(3):
            self.sp_items_icons_lay.setColumnStretch(col, 1)
        self.sp_items_icons_wrap.setVisible(bool(ordered_ids))

        parts: list[str] = []
        if e.force_items:
            parts.append("【必出】" + " + ".join(_fmt_item(i) for i in e.force_items))
        if e.core_items:
            rest = [i for i in e.core_items if i not in e.force_items]
            if rest:
                parts.append("【核心】" + " › ".join(_fmt_item(i) for i in rest))
        if not parts:
            parts.append("按 26.23 全英雄实测绑定胜率榜动态匹配")
        self.sp_items_lbl.setText("\n".join(parts))
        self.sp_avoid_lbl.setText(f"出装要点：{e.avoid_note}")
        self.sp_tip_lbl.setText(f"打法思路：{e.playstyle_tip}")

    def _build_coach_page(self) -> None:
        self._build_commercial_coach_page()

    def _build_commercial_coach_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(18, 8, 18, 8)
        lay.setSpacing(6)

        self.account_card = HextechInteractiveCard("#0AC8B9")
        al = QVBoxLayout(self.account_card)
        al.setContentsMargins(14, 7, 14, 7)
        al.setSpacing(5)

        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        self.coach_page_status = QLabel("云端 AI 实时参谋 · 待登录")
        self.coach_page_status.setFont(display_font(12, bold=True, letter_spacing=0.8))
        self.coach_page_status.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")

        self.account_user_lbl = QLabel("账号：未登录（登录后可使用云端 AI 参谋）")
        self.account_user_lbl.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-size: 12px; font-weight: bold;")

        wallet_line = QHBoxLayout()
        wallet_line.setSpacing(14)
        self.account_balance_lbl = QLabel("余额：待登录同步")
        self.account_balance_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 13px; font-weight: bold;")
        self.account_est_lbl = QLabel("基础大数据 / 3-4符文识别 / 组合路线 永久免费")
        self.account_est_lbl.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 11px;")
        wallet_line.addWidget(self.account_balance_lbl)
        wallet_line.addWidget(self.account_est_lbl)
        wallet_line.addStretch(1)

        info_col.addWidget(self.coach_page_status)
        info_col.addWidget(self.account_user_lbl)
        info_col.addLayout(wallet_line)
        al.addLayout(info_col)

        # Dedicated action row below account summary so buttons NEVER overlap at 940px minimum width
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.login_btn = HextechActionButton("登录 / 注册", variant="cyan", pointed_right=True, height=30)
        self.login_btn.clicked.connect(lambda: self._on_login_click and self._on_login_click())
        self.recharge_btn = HextechActionButton("补充算力", variant="gold", height=30)
        self.recharge_btn.clicked.connect(lambda: self._on_recharge_click and self._on_recharge_click())
        self.logout_btn = HextechActionButton("切换账号 / 退出", variant="gold", height=30)
        self.logout_btn.clicked.connect(lambda: self._on_logout_click and self._on_logout_click())
        self.logout_btn.hide()
        self.check_update_btn = HextechActionButton("检查更新", variant="gold", height=30)
        self.check_update_btn.clicked.connect(lambda: self._on_check_update and self._on_check_update())
        push_btn = HextechActionButton("立即推演 F6", variant="cyan", pointed_right=True, height=30)
        push_btn.clicked.connect(lambda: self._on_test_coach and self._on_test_coach())
        self.tab2_push_btn = push_btn

        for b in (self.login_btn, self.recharge_btn, self.logout_btn, self.check_update_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        btn_row.addWidget(push_btn)
        al.addLayout(btn_row)

        # Auto / A / B Triple-Tier Segmented Switcher
        tier_row = QHBoxLayout()
        tier_row.setSpacing(6)
        tier_hdr = QLabel("AI 副驾档位：")
        tier_hdr.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-size: 11px; font-weight: bold;")
        self.tier_auto_btn = QPushButton("Auto 智能混合 (推荐 · 约3毛/局)")
        self.tier_auto_btn.setToolTip(
            "推荐默认：开局读条简报+质变大变阵走顶配 Gemini，日常反制走极速 DeepSeek，兼顾大脑与超低成本"
        )
        self.tier_a_btn = QPushButton("A档 · 极速省钱 (约2毛/局)")
        self.tier_a_btn.setToolTip("全程走极速 DeepSeek 模型，单局约 2 毛钱（2 / 8 积分/1K tokens）")
        self.tier_b_btn = QPushButton("B档 · 顶配旗舰 (约1.1元/局)")
        self.tier_b_btn.setToolTip("全程走顶配 Gemini 旗舰推理，适合高强度复杂对局（5.4 / 27 积分/1K tokens）")
        for tb in (self.tier_auto_btn, self.tier_a_btn, self.tier_b_btn):
            tb.setFixedHeight(26)
            tb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tier_auto_btn.clicked.connect(lambda: self._handle_tier_clicked("auto"))
        self.tier_a_btn.clicked.connect(lambda: self._handle_tier_clicked("a"))
        self.tier_b_btn.clicked.connect(lambda: self._handle_tier_clicked("b"))

        tier_row.addWidget(tier_hdr)
        tier_row.addWidget(self.tier_auto_btn)
        tier_row.addWidget(self.tier_a_btn)
        tier_row.addWidget(self.tier_b_btn)
        tier_row.addStretch(1)
        al.addLayout(tier_row)

        # Compact single-line Free vs Paid + Auto hybrid tier boundary strip (eliminates 3x duplicate copy)
        self.tier_info_card = QFrame()
        self.tier_info_card.setStyleSheet(
            "QFrame { background-color: rgba(4, 14, 26, 210); border: 1px solid #463714; "
            "border-left: 2px solid #09C269; border-radius: 2px; }"
        )
        til = QVBoxLayout(self.tier_info_card)
        til.setContentsMargins(10, 4, 10, 4)
        til.setSpacing(2)
        self.tier_info_lbl = QLabel(
            "✓ 永久免费不锁：英雄大数据 / 3-4卡识别 / 组合路线 / 本地 5ms 出装  |  ◈ Auto 智能混合：开局定盘与质变走旗舰推理档，日常反制走极速经济档（约 3 毛/局 · 服务期内有效）"
        )
        self.tier_info_lbl.setWordWrap(True)
        self.tier_info_lbl.setStyleSheet(
            "border: none; background: transparent; color: #E2D9C5; font-size: 11px;"
        )
        til.addWidget(self.tier_info_lbl)
        al.addWidget(self.tier_info_card)

        self.update_status_lbl = QLabel("")
        self.update_status_lbl.setWordWrap(True)
        self.update_status_lbl.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 11px;")
        self.update_status_lbl.hide()
        al.addWidget(self.update_status_lbl)

        lay.addWidget(self.account_card)
        self.set_coach_tier(self._cfg.coach_tier)

        # Unified status / seal container: hosts either hard system gate bar or 3-column Hextech Seal shelf
        self.cutoff_banner = QFrame()
        self.cutoff_banner.setStyleSheet("QFrame { background: transparent; border: none; }")
        cb_outer = QVBoxLayout(self.cutoff_banner)
        cb_outer.setContentsMargins(0, 0, 0, 0)
        cb_outer.setSpacing(6)

        self.cutoff_gate_bar = QFrame()
        self.cutoff_gate_bar.setStyleSheet(
            "QFrame { background-color: rgba(6, 20, 34, 224); border: 1px solid #785A28; "
            "border-left: 3px solid #0AC8B9; border-radius: 2px; }"
        )
        cb_lay = QHBoxLayout(self.cutoff_gate_bar)
        cb_lay.setContentsMargins(12, 5, 12, 5)
        cb_lay.setSpacing(10)
        self.cutoff_banner_lbl = QLabel("本地 5ms 大数据持续免费护航 · 充能后立即唤醒云端 AI 参谋")
        self.cutoff_banner_lbl.setStyleSheet("border: none; background: transparent; color: #E2D9C5; font-size: 11px; font-weight: bold;")
        self.cutoff_action_btn = HextechActionButton("充能唤醒", variant="cyan", height=26)
        self.cutoff_action_btn.clicked.connect(self._handle_cutoff_action)
        cb_lay.addWidget(self.cutoff_banner_lbl, 1)
        cb_lay.addWidget(self.cutoff_action_btn)
        cb_outer.addWidget(self.cutoff_gate_bar)
        self.cutoff_banner.hide()
        lay.addWidget(self.cutoff_banner)

        # Independent 3-Column Hextech AI Core Seal Showcase Shelf (S0 claim / S2 weathered seal / S3 domino shatter)
        self.seal_shelf_wrap = QWidget()
        self.seal_shelf_wrap.setStyleSheet("background: transparent; border: none;")
        ss_lay = QHBoxLayout(self.seal_shelf_wrap)
        ss_lay.setContentsMargins(0, 0, 0, 0)
        ss_lay.setSpacing(8)
        self._lobby_seal_cards.clear()
        self._lobby_seals.clear()
        seal_specs = (
            ("opening_brief", "开局 10 人 5 秒定盘", "敌方威胁 · 海克斯目标 · 出门对策", "敌方 3AP + 高回血\n优先抗压装 + 抓突进卡"),
            ("augment_decision", "72 质变重构与焦灼选牌", "胜率胶着结合 10 人阵容拍板", "迷人蝴蝶 53.2% 胜出\n后续出装全盘切换特效流"),
            ("death_counter", "阵亡精准散件 & F6 急救", "按精确金币配双散件 + 团战站位", "1,650G 买湮灭宝珠+红水晶\n针对 8/2 剑魔留控断二段Q"),
        )
        for sid, stitle, ssub, skel_txt in seal_specs:
            scard = HextechPulseFrame()
            scard.setFixedHeight(82)
            scard.setStyleSheet(
                "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                "stop:0 rgba(6, 30, 44, 230), stop:1 rgba(9, 20, 40, 232)); "
                "border: 1px solid #0397AB; border-top: 2px solid #C8AA6E; border-radius: 2px; }"
            )
            scl = QVBoxLayout(scard)
            scl.setContentsMargins(10, 6, 10, 6)
            scl.setSpacing(3)
            st_lbl = QLabel(f"◈ {stitle}")
            st_lbl.setFont(display_font(10, bold=True))
            st_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
            sb_lbl = QLabel(skel_txt)
            sb_lbl.setWordWrap(True)
            sb_lbl.setStyleSheet("border: none; background: transparent; color: #CBD5E1; font-size: 10px;")
            scl.addWidget(st_lbl)
            scl.addWidget(sb_lbl, 1)

            seal = HextechSealOverlay(
                scard,
                seal_id=sid,
                title=stitle,
                subtitle=ssub,
                compact=False,
                allow_idle_ambient=True,
            )
            seal.unseal_settled.connect(lambda _s, c=scard: c.trigger_pulse("#0AC8B9", 400))
            seal.seal_clicked.connect(self._on_lobby_seal_clicked)
            self._lobby_seal_cards.append(scard)
            self._lobby_seals.append(seal)
            ss_lay.addWidget(scard, 1)
        self.seal_shelf_wrap.hide()
        lay.addWidget(self.seal_shelf_wrap)

        # Collapsed 26px active ribbon when AI core is unlocked (reclaims 70%+ vertical area for timeline!)
        self.seal_active_bar = QFrame()
        self.seal_active_bar.setFixedHeight(26)
        self.seal_active_bar.setStyleSheet(
            "QFrame { background-color: rgba(6, 26, 38, 215); border: 1px solid rgba(10, 200, 185, 130); "
            "border-left: 3px solid #C8AA6E; border-radius: 2px; }"
        )
        sabl = QHBoxLayout(self.seal_active_bar)
        sabl.setContentsMargins(10, 2, 10, 2)
        self.seal_active_lbl = QLabel("◈ 云端 AI 三重参谋已激活：开局 10 人定盘  ·  72 套质变重构  ·  阵亡精准双散件 & F6 急救")
        self.seal_active_lbl.setStyleSheet("border: none; background: transparent; color: #5EEAD4; font-size: 10px; font-weight: bold;")
        sabl.addWidget(self.seal_active_lbl, 1)
        self.seal_active_bar.hide()
        lay.addWidget(self.seal_active_bar)

        lay.addWidget(self._build_coach_output_card(), 1)
        self.pages.addWidget(page)

    def _build_coach_output_card(self) -> HextechInteractiveCard:
        # Match Coach Timeline & Live Output Viewport (reclaims 70% area for post-game review!)
        self.coach_output_card = HextechInteractiveCard("#0397AB")
        cl = QVBoxLayout(self.coach_output_card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(8)

        hdr_row = QHBoxLayout()
        ttl = QLabel("◈ 本局实时参谋推演时间轴（自动归档每次选牌与出装决策，便于赛后复盘）")
        ttl.setFont(display_font(11, bold=True, letter_spacing=1.0))
        ttl.setStyleSheet("border: none; background: transparent; color: #C8AA6E;")
        clear_hist_btn = QPushButton("清空记录")
        clear_hist_btn.setFixedHeight(22)
        clear_hist_btn.setStyleSheet(
            "QPushButton { padding: 0px 8px; font-size: 10px; color: #A09B8C; border: 1px solid #463714; background: transparent; }"
            "QPushButton:hover { color: #F0E6D2; border-color: #C8AA6E; }"
        )
        clear_hist_btn.clicked.connect(self._clear_coach_history)
        hdr_row.addWidget(ttl)
        hdr_row.addStretch(1)
        hdr_row.addWidget(clear_hist_btn)
        cl.addLayout(hdr_row)

        self.coach_page_body = QLabel("暂未收到参谋回写。进局开打、三选一或按 F6 后，此处将实时记录每一步推演与耗时。")
        self.coach_page_body.setWordWrap(True)
        self.coach_page_body.setStyleSheet(
            "background-color: rgba(1, 10, 19, 175); border: 1px solid rgba(10, 200, 185, 110); "
            "border-radius: 2px; padding: 8px 10px; color: #F0E6D2; font-size: 12px;"
        )
        cl.addWidget(self.coach_page_body)

        self.coach_history_scroll = QScrollArea()
        self.coach_history_scroll.setWidgetResizable(True)
        self.coach_history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.coach_history_scroll.viewport().setStyleSheet("background: transparent;")
        self.coach_history_container = QWidget()
        self.coach_history_container.setStyleSheet("background: transparent;")
        self.coach_history_lay = QVBoxLayout(self.coach_history_container)
        self.coach_history_lay.setContentsMargins(0, 2, 4, 4)
        self.coach_history_lay.setSpacing(6)
        self.coach_history_lay.addStretch(1)
        self.coach_history_scroll.setWidget(self.coach_history_container)
        cl.addWidget(self.coach_history_scroll, 1)

        return self.coach_output_card

    def set_gemini_form_collapsed(self, collapsed: bool) -> None:
        if self._commercial or self.gemini_config_card is None or self.toggle_gemini_form_btn is None:
            return
        self._gemini_form_collapsed = bool(collapsed)
        self.gemini_config_card.setVisible(not self._gemini_form_collapsed)
        self.toggle_gemini_form_btn.setText("修改配置" if self._gemini_form_collapsed else "收起配置")

    def _handle_recap_clicked(self) -> None:
        if self._recap_available and self._on_open_recap is not None:
            self._on_open_recap()

    def set_recap_available(self, available: bool, summary: str = "") -> None:
        """Toggle the top bar post-match Hextech recap card button state & tooltip."""
        self._recap_available = bool(available)
        self._recap_summary = (summary or "").strip()
        self.recap_btn.setEnabled(self._recap_available)
        if self._recap_available:
            tip = "点击查看并一键复制本局海克斯战报图"
            if self._recap_summary:
                tip = f"{tip}\n{self._recap_summary}"
            self.recap_btn.setToolTip(tip)
            self.recap_btn.setStyleSheet(
                "QPushButton { background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                "stop:0 rgba(52, 41, 18, 228), stop:1 rgba(14, 28, 44, 236)); "
                "border: 1px solid #C8AA6E; border-radius: 2px; padding: 0px 10px; "
                "color: #F0E6D2; font-size: 11px; font-weight: bold; }"
                "QPushButton:hover { background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                "stop:0 rgba(74, 58, 24, 242), stop:1 rgba(10, 44, 64, 242)); "
                "border-color: #0AC8B9; color: #FFFFFF; }"
                "QPushButton:pressed { background-color: rgba(10, 28, 44, 245); border-color: #F0E6D2; }"
            )
        else:
            self.recap_btn.setToolTip("完成一局海克斯大乱斗后自动生成赛后战报卡")
            self.recap_btn.setStyleSheet(
                "QPushButton { background-color: rgba(8, 18, 32, 135); border: 1px solid #463714; "
                "border-radius: 2px; padding: 0px 10px; color: #6B675E; font-size: 11px; font-weight: bold; }"
            )

    def _refresh_account_balance_labels(self, wallet: dict[str, Any]) -> None:
        bal_credits = float(wallet.get("balance_credits") or 0.0)
        bal_cny = float(
            wallet.get("balance_cny")
            if wallet.get("balance_cny") is not None
            else (bal_credits / 1000.0)
        )
        norm_tier = self._cfg.coach_tier if self._cfg.coach_tier in ESTIMATED_CREDITS_PER_MATCH else "auto"
        est_auto = estimate_remaining_matches(bal_credits, "auto")
        est_a = estimate_remaining_matches(bal_credits, "a")
        est_b = estimate_remaining_matches(bal_credits, "b")
        self.account_balance_lbl.setText(
            f"{format_balance_match_friendly(bal_credits, norm_tier)} · ¥{bal_cny:.2f}"
        )
        self.account_est_lbl.setText(
            f"混合约 {est_auto} 局 · A档约 {est_a} 局 · B档约 {est_b} 局 · 服务期内有效"
        )

    def _handle_tier_clicked(self, tier: str) -> None:
        raw = (tier or "a").strip().lower()
        norm = raw if raw in ("a", "b", "auto") else "a"
        self.set_coach_tier(norm)
        if self._on_switch_tier:
            self._on_switch_tier(norm)

    def _on_status_box_clicked(self, event) -> None:
        if event is not None and event.button() != Qt.MouseButton.LeftButton:
            return
        if self._commercial:
            st = self._lifecycle.state if self._lifecycle is not None else None
            if st == CoreEnergyState.GUEST_UNCLAIMED:
                self._emit_core_action("register")
                return
            if st == CoreEnergyState.CORRODED_TRIAL_END:
                self._switch_page(2, force=True)
                self._emit_core_action("preview")
                return
            if st == CoreEnergyState.CORRODED_RETURNING:
                self._switch_page(2, force=True)
                self._emit_core_action("recharge")
                return
        self._switch_page(2, force=True)

    def _emit_core_action(self, action: str) -> None:
        if self._on_core_action is not None:
            self._on_core_action(action)
            return
        if action in ("register", "login") and self._on_login_click is not None:
            self._on_login_click()
        elif action in ("preview", "recharge") and self._on_recharge_click is not None:
            self._on_recharge_click()

    def _dismiss_newbie_welcome(self) -> None:
        self._cfg.newbie_banner_dismissed = True
        if self.newbie_welcome_bar is not None:
            self.newbie_welcome_bar.hide()
        if self._on_dismiss_newbie_banner is not None:
            self._on_dismiss_newbie_banner()

    def open_seal_sandbox(self) -> None:
        dlg = HextechSealSandboxDialog(parent=self)
        dlg.exec()

    def set_game_active(self, active: bool) -> None:
        """Freeze lobby seal idle dust timer during live LOL games to guarantee 0 FPS overhead."""
        for seal in self._lobby_seals:
            seal.set_game_active(active)

    def _on_lobby_seal_clicked(self, seal_id: str, mode_val: str) -> None:
        if mode_val == SealMode.CHARGED_READY.value:
            self.trigger_domino_unseal(starting_seal_id=seal_id)
            return
        if mode_val == SealMode.GUEST_CLAIM.value:
            self._emit_core_action("register")
        elif mode_val == SealMode.RETURNING_DORMANT.value:
            self._emit_core_action("recharge")
        else:
            self._emit_core_action("preview")

    def trigger_domino_unseal(self, starting_seal_id: str = "") -> None:
        """Trigger 90ms-staggered domino shatter across all 3 lobby AI core seals, then settle into active bar."""
        if not self._lobby_seals:
            return
        self._domino_unseal_in_progress = True
        ordered: list[HextechSealOverlay] = []
        for s in self._lobby_seals:
            if s.seal_id == starting_seal_id:
                ordered.insert(0, s)
            else:
                ordered.append(s)

        delay = 0
        for idx, seal in enumerate(ordered):
            if seal.mode == SealMode.UNSEALING:
                delay = max(delay, 90)
                continue
            if idx == 0 and starting_seal_id and seal.seal_id == starting_seal_id:
                delay = 90
                continue
            QTimer.singleShot(delay, lambda s=seal: s.play_unseal(fast=False))
            delay += 90

        total_wait_ms = max(760, delay + 720)
        QTimer.singleShot(total_wait_ms, self._finish_domino_unseal)

    def _finish_domino_unseal(self) -> None:
        self._domino_unseal_in_progress = False
        if self._closed:
            return
        if self.seal_shelf_wrap is not None:
            self.seal_shelf_wrap.hide()
        if hasattr(self, "cutoff_banner") and self.cutoff_banner is not None and not self._cutoff_code:
            self.cutoff_banner.hide()
        if self.seal_active_bar is not None:
            self.seal_active_bar.show()
        self.status_box.trigger_pulse("#0AC8B9", 520)
        if self._on_unseal_complete is not None:
            self._on_unseal_complete()

    def _style_cutoff_banner(self, *, is_system_gate: bool) -> None:
        if not hasattr(self, "cutoff_gate_bar") or self.cutoff_gate_bar is None:
            return
        if is_system_gate:
            self.cutoff_gate_bar.setStyleSheet(
                "QFrame { background-color: rgba(120, 28, 28, 215); border: 1px solid #F59E0B; border-radius: 2px; }"
            )
            self.cutoff_banner_lbl.setStyleSheet(
                "border: none; background: transparent; color: #FEF3C7; font-size: 11px; font-weight: bold;"
            )
        else:
            self.cutoff_gate_bar.setStyleSheet(
                "QFrame { background-color: rgba(6, 20, 34, 224); border: 1px solid #785A28; "
                "border-left: 3px solid #0AC8B9; border-radius: 2px; }"
            )
            self.cutoff_banner_lbl.setStyleSheet(
                "border: none; background: transparent; color: #E2D9C5; font-size: 11px; font-weight: bold;"
            )

    def set_core_lifecycle(self, snapshot: CoreLifecycleSnapshot | None) -> None:
        """Synchronize topbar clickable status card, Tab 0 welcome strip, and Tab 2 3-card seal shelf."""
        if not self._commercial:
            return
        self._lifecycle = snapshot
        if snapshot is None:
            return

        st = snapshot.state
        if self.newbie_welcome_bar is not None:
            show_nb = (
                st == CoreEnergyState.GUEST_UNCLAIMED
                and not self._cfg.newbie_banner_dismissed
                and not snapshot.snoozed
                and not self._is_full_locked
            )
            self.newbie_welcome_bar.setVisible(show_nb)

        if self.seal_shelf_wrap is None or self.seal_active_bar is None:
            return
        if self._domino_unseal_in_progress:
            return

        if st in (CoreEnergyState.SYSTEM_GATE, CoreEnergyState.DISABLED_BY_USER, CoreEnergyState.SYNCING):
            self.seal_shelf_wrap.hide()
            self.seal_active_bar.hide()
            if hasattr(self, "cutoff_gate_bar"):
                self.cutoff_gate_bar.setVisible(st == CoreEnergyState.SYSTEM_GATE)
            return

        if st == CoreEnergyState.GUEST_UNCLAIMED:
            self.seal_active_bar.hide()
            for seal in self._lobby_seals:
                seal.set_snoozed(snapshot.snoozed)
                seal.set_seal_mode(SealMode.GUEST_CLAIM, cta_text="领 450 积分")
            self.seal_shelf_wrap.setVisible(not self._is_full_locked)
            if hasattr(self, "cutoff_gate_bar"):
                self.cutoff_gate_bar.setVisible(self._is_full_locked)
            self.cutoff_banner.show()
            self.coach_badge_lbl.setText("◇ 领 450 积分体验 · 点击登录")
            self.coach_badge_lbl.setStyleSheet(
                "border: none; background: transparent; color: #5EEAD4; font-size: 11px; font-weight: bold;"
            )
        elif st == CoreEnergyState.CORRODED_TRIAL_END:
            self.seal_active_bar.hide()
            for seal in self._lobby_seals:
                seal.set_snoozed(snapshot.snoozed)
                seal.set_seal_mode(SealMode.CORRODED, cta_text="查看实战差异")
            self.seal_shelf_wrap.setVisible(not self._is_full_locked)
            if hasattr(self, "cutoff_gate_bar"):
                self.cutoff_gate_bar.setVisible(self._is_full_locked)
            self.cutoff_banner.show()
            self.coach_badge_lbl.setText("◇ 核心休眠封印 · 点击充能")
            self.coach_badge_lbl.setStyleSheet(
                "border: none; background: transparent; color: #C8AA6E; font-size: 11px; font-weight: bold;"
            )
        elif st == CoreEnergyState.CORRODED_RETURNING:
            self.seal_active_bar.hide()
            for seal in self._lobby_seals:
                seal.set_snoozed(snapshot.snoozed)
                seal.set_seal_mode(SealMode.RETURNING_DORMANT, cta_text="快速补能")
            self.seal_shelf_wrap.setVisible(not self._is_full_locked)
            if hasattr(self, "cutoff_gate_bar"):
                self.cutoff_gate_bar.setVisible(self._is_full_locked)
            self.cutoff_banner.show()
            self.coach_badge_lbl.setText("◇ 核心待补能 · 点击续充")
            self.coach_badge_lbl.setStyleSheet(
                "border: none; background: transparent; color: #C8AA6E; font-size: 11px; font-weight: bold;"
            )
        elif st == CoreEnergyState.READY_TO_UNSEAL:
            self.seal_active_bar.hide()
            for seal in self._lobby_seals:
                seal.set_snoozed(False)
                seal.set_seal_mode(
                    SealMode.CHARGED_READY,
                    subtitle="能量已注入 · 点击击碎封印",
                    cta_text="✦ 点击破封",
                )
            self.seal_shelf_wrap.setVisible(not self._is_full_locked)
            self.cutoff_banner.hide()
            self.coach_badge_lbl.setText(f"✦ 核心已充能 · 约剩{snapshot.est_matches}局 · 点此破封")
            self.coach_badge_lbl.setStyleSheet(
                "border: none; background: transparent; color: #0AC8B9; font-size: 11px; font-weight: bold;"
            )
        else:
            # S1 (TRIAL_ACTIVE / TRIAL_LOW) or S4 (ACTIVE_PAID / PAID_LOW): compact 26px active ribbon
            for seal in self._lobby_seals:
                seal.set_seal_mode(SealMode.UNLOCKED)
            self.seal_shelf_wrap.hide()
            if hasattr(self, "cutoff_gate_bar"):
                self.cutoff_gate_bar.show()
            self.cutoff_banner.hide()
            if st == CoreEnergyState.TRIAL_LOW:
                self.seal_active_lbl.setText(
                    f"✦ 免费体验核心余能（约剩 {snapshot.est_matches} 局）· 本局结束后 AI 将休眠，本地 5ms 永久免费"
                )
                self.seal_active_lbl.setStyleSheet(
                    "border: none; background: transparent; color: #FDE68A; font-size: 10px; font-weight: bold;"
                )
            elif st == CoreEnergyState.PAID_LOW:
                self.seal_active_lbl.setText(
                    f"✦ 云端 AI 三重参谋运行中（约剩 {snapshot.est_matches} 局 · 点击右上角或上方按钮随时续充）"
                )
                self.seal_active_lbl.setStyleSheet(
                    "border: none; background: transparent; color: #FDE68A; font-size: 10px; font-weight: bold;"
                )
            else:
                self.seal_active_lbl.setText(
                    "◈ 云端 AI 三重参谋已激活：开局 10 人定盘  ·  72 套质变重构  ·  阵亡精准双散件 & F6 急救"
                )
                self.seal_active_lbl.setStyleSheet(
                    "border: none; background: transparent; color: #5EEAD4; font-size: 10px; font-weight: bold;"
                )
            self.seal_active_bar.show()

    def _handle_cutoff_action(self) -> None:
        if self._cutoff_code in ("SERVICE_LOCKED", "CLIENT_UPDATE_REQUIRED"):
            if self._on_check_update:
                self._on_check_update()
        elif self._cutoff_code == "NOT_LOGGED_IN":
            if self._on_login_click:
                self._on_login_click()
        else:
            if self._on_recharge_click:
                self._on_recharge_click()

    def update_pricing_summary(self, pricing: dict[str, Any] | None) -> None:
        if not isinstance(pricing, dict) or self.tier_a_btn is None or self.tier_b_btn is None:
            return
        try:
            a_in = pricing.get("tier_a_input_credits_per_1k")
            a_out = pricing.get("tier_a_output_credits_per_1k")
            b_in = pricing.get("input_credits_per_1k")
            b_out = pricing.get("output_credits_per_1k")
            if a_in is not None and a_out is not None:
                self.tier_a_btn.setToolTip(
                    f"全程极速 DeepSeek 模型，约 2 毛/局（{a_in:g} / {a_out:g} 积分/1K tokens）"
                )
            if b_in is not None and b_out is not None:
                self.tier_b_btn.setToolTip(
                    f"全程顶配 Gemini 旗舰推理，约 1.1 元/局（{b_in:g} / {b_out:g} 积分/1K tokens）"
                )
        except Exception:
            pass

    def set_coach_tier(self, tier: str) -> None:
        raw = (tier or "a").strip().lower()
        norm = raw if raw in ("a", "b", "auto") else "a"
        self._cfg.coach_tier = norm
        if self.tier_a_btn is None or self.tier_b_btn is None:
            return
        active_qss = (
            "QPushButton { background-color: rgba(10, 200, 185, 58); border: 1px solid #0AC8B9; "
            "color: #F0E6D2; font-weight: bold; font-size: 11px; padding: 0px 10px; border-radius: 2px; }"
        )
        idle_qss = (
            "QPushButton { background-color: rgba(8, 18, 32, 195); border: 1px solid #463714; "
            "color: #A09B8C; font-size: 11px; padding: 0px 10px; border-radius: 2px; }"
            "QPushButton:hover { border-color: #C8AA6E; color: #F0E6D2; }"
        )
        if self.tier_auto_btn is not None:
            self.tier_auto_btn.setStyleSheet(active_qss if norm == "auto" else idle_qss)
        self.tier_a_btn.setStyleSheet(active_qss if norm == "a" else idle_qss)
        self.tier_b_btn.setStyleSheet(active_qss if norm == "b" else idle_qss)
        if (
            self._commercial
            and self._account_username
            and isinstance(self._account_wallet, dict)
            and hasattr(self, "account_balance_lbl")
        ):
            self._refresh_account_balance_labels(self._account_wallet)
            if not self._cutoff_code:
                bal_credits = float(self._account_wallet.get("balance_credits") or 0.0)
                est_cur = estimate_remaining_matches(bal_credits, norm)
                tier_lbl = {"auto": "智能混合", "b": "B档旗舰", "a": "A档极速"}.get(norm, "智能混合")
                tier_short = {"auto": "智能", "b": "B档", "a": "A档"}.get(norm, "智能")
                uname_compact = (
                    (self._account_username[:8] + "…")
                    if len(self._account_username) > 8
                    else self._account_username
                )
                self.coach_page_status.setText(f"云端 AI 实时参谋 · ● {tier_lbl}已就绪")
                self.coach_badge_lbl.setText(f"● {uname_compact} · 约剩{est_cur}局 · {tier_short}")
                self.coach_badge_lbl.setToolTip(
                    f"{self._account_username} · {format_balance_match_friendly(bal_credits, norm)} · {tier_lbl}"
                )

    def _apply_lock_all_ui(
        self,
        locked: bool,
        *,
        reason: str = "",
        code: str = "",
        update_available: bool | None = None,
    ) -> None:
        self._is_full_locked = bool(self._commercial and locked)
        if update_available is not None:
            self._update_available = bool(update_available)
        self.hero_browser_body.setVisible(not self._is_full_locked)
        self.hero_locked_card.setVisible(self._is_full_locked)
        self.special_builds_body.setVisible(not self._is_full_locked)
        self.special_locked_card.setVisible(self._is_full_locked)

        if len(self.nav_btns) >= 2:
            self.nav_btns[0].setEnabled(not self._is_full_locked)
            self.nav_btns[1].setEnabled(not self._is_full_locked)
        self.mode_normal_btn.setEnabled(not self._is_full_locked)
        self.mode_prism_btn.setEnabled(not self._is_full_locked)
        self.mode_high_btn.setEnabled(not self._is_full_locked)

        if self._is_full_locked:
            eff_code = code or self._cutoff_code or "INSUFFICIENT_CREDITS"
            if eff_code == "SERVICE_LOCKED":
                msg = reason or "服务维护升级中，请稍候或更新至最新版本"
                btn_txt = "立即更新" if self._update_available else "刷新状态 / 检查更新"
                show_btn = True
            elif eff_code == "CLIENT_UPDATE_REQUIRED":
                msg = reason or "当前版本已停用，请立即更新至最新版本"
                btn_txt = "立即更新"
                show_btn = True
            elif eff_code == "NOT_LOGGED_IN":
                msg = reason or "当前策略要求先登录 HexHelper 账号以解锁本地查表与云端 AI 参谋"
                btn_txt = "登录 / 注册"
                show_btn = True
            elif eff_code == "ACCOUNT_DISABLED":
                msg = reason or "当前账号已被限制，功能已暂停使用"
                btn_txt = ""
                show_btn = False
            else:
                msg = reason or "当前账号算力额度不足，全功能已锁定 · 请补充算力后立即解锁"
                btn_txt = "立即补充算力"
                show_btn = True
            self._hero_locked_desc_lbl.setText(msg)
            self._special_locked_desc_lbl.setText(msg)
            self._hero_locked_action_btn.setText(btn_txt)
            self._special_locked_action_btn.setText(btn_txt)
            self._hero_locked_action_btn.setVisible(show_btn)
            self._special_locked_action_btn.setVisible(show_btn)
            if self.pages.currentIndex() in (0, 1):
                self._switch_page(2, force=True)

    def set_account_state(
        self,
        username: str | None,
        wallet: dict[str, Any] | None,
        tier: str = "a",
        cutoff_reason: str = "",
        cutoff_code: str = "",
        cutoff_policy: str | None = None,
        update_available: bool = False,
    ) -> None:
        if not self._commercial or self.account_card is None:
            return
        self._account_username = username
        self._account_wallet = dict(wallet) if isinstance(wallet, dict) else None
        self._update_available = bool(update_available)
        self.set_coach_tier(tier)

        pol = str(
            cutoff_policy
            if cutoff_policy is not None
            else ((wallet or {}).get("cutoff_policy") or "")
        ).strip().lower()
        if pol in ("ai_only", "lock_all"):
            self._cutoff_policy = pol

        logged_in = bool(username)
        self.login_btn.setVisible(not logged_in)
        self.logout_btn.setVisible(logged_in)

        if cutoff_code in ("SERVICE_LOCKED", "CLIENT_UPDATE_REQUIRED"):
            self._cutoff_code = cutoff_code
            if logged_in and isinstance(wallet, dict):
                self.account_user_lbl.setText(f"账号：{username}")
                self._refresh_account_balance_labels(wallet)
            elif logged_in:
                self.account_user_lbl.setText(f"账号：{username}")
                self.account_balance_lbl.setText("余额：-")
                self.account_est_lbl.setText("服务维护或版本升级中")
            else:
                self.account_user_lbl.setText("账号：未登录")
                self.account_balance_lbl.setText("余额：-")
                self.account_est_lbl.setText("服务维护或版本升级中")

            is_lock = cutoff_code == "SERVICE_LOCKED"
            default_msg = (
                "服务维护升级中，请稍候或更新至最新版本"
                if is_lock
                else "当前版本已停用，请立即更新至最新版本"
            )
            banner_msg = (cutoff_reason or "").strip() or default_msg
            self.coach_page_status.setText("云端服务维护锁定" if is_lock else "客户端需要强制更新")
            self.coach_badge_lbl.setText("● 服务维护锁定" if is_lock else "● 需强制更新")
            self.coach_badge_lbl.setStyleSheet(
                "border: none; background: transparent; color: #F87171; font-size: 11px; font-weight: bold;"
            )
            self.cutoff_banner_lbl.setText(banner_msg)
            self._style_cutoff_banner(is_system_gate=True)
            self.cutoff_action_btn.setText(
                "立即更新" if (self._update_available or not is_lock) else "刷新状态 / 检查更新"
            )
            self.cutoff_action_btn.setVisible(True)
            self.cutoff_banner.show()
            self._apply_lock_all_ui(
                True,
                reason=banner_msg,
                code=cutoff_code,
                update_available=self._update_available,
            )
            return

        if not logged_in:
            self._cutoff_code = cutoff_code or "NOT_LOGGED_IN"
            full_locked = self._cutoff_policy == "lock_all"
            self.account_user_lbl.setText("账号：未登录")
            self.account_balance_lbl.setText("初始晶核：注册即赠 450 积分（约 1.5-2 局体验）")
            self.account_est_lbl.setText(
                "当前策略要求登录后解锁全部功能"
                if full_locked
                else "国内节点直连 · 按量分摊算力 · 服务期内有效"
            )
            self.coach_page_status.setText("云端 AI 实时参谋 · 初始晶核待认领")
            self.coach_badge_lbl.setText(
                "● 未登录 · 功能已锁定" if full_locked else "◇ 领 450 积分体验 · 点击登录"
            )
            self.coach_badge_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {'#F59E0B' if full_locked else '#5EEAD4'}; font-size: 11px; font-weight: bold;"
            )
            self._style_cutoff_banner(is_system_gate=full_locked)
            self.cutoff_banner_lbl.setText(
                cutoff_reason
                or (
                    "请先登录 HexHelper 账号以解锁全部功能"
                    if full_locked
                    else "新用户 10 秒注册即赠 450 积分（约 2 局智能 AI）· 本地大数据引擎永久免费"
                )
            )
            self.cutoff_action_btn.setText("领 450 积分")
            self.cutoff_action_btn.setVisible(True)
            self.cutoff_banner.show()
            self._apply_lock_all_ui(
                full_locked,
                reason=self.cutoff_banner_lbl.text(),
                code=self._cutoff_code,
            )
            return

        if wallet is None and not cutoff_reason:
            self._cutoff_code = ""
            self.account_user_lbl.setText(f"账号：{username}")
            self.account_balance_lbl.setText("积分余额：同步中...")
            self.account_est_lbl.setText("预计可推演：正在获取账户信息...")
            self.coach_page_status.setText("云端 AI 实时参谋 · 同步账户中...")
            self.coach_badge_lbl.setText(f"● {username} · 同步中...")
            self.coach_badge_lbl.setStyleSheet(
                "border: none; background: transparent; color: #FBBF24; font-size: 11px; font-weight: bold;"
            )
            self.cutoff_banner.hide()
            self._apply_lock_all_ui(False)
            return

        w = wallet or {}
        bal_credits = float(w.get("balance_credits") or 0.0)
        ai_cutoff = bool(cutoff_reason or w.get("ai_cutoff", False) or not w.get("ai_enabled", True))
        norm_tier = self._cfg.coach_tier if self._cfg.coach_tier in ESTIMATED_CREDITS_PER_MATCH else "auto"
        est_cur = estimate_remaining_matches(bal_credits, norm_tier)

        self.account_user_lbl.setText(f"账号：{username}")
        self._refresh_account_balance_labels(w)

        tier_lbl = {"auto": "智能混合", "b": "B档旗舰", "a": "A档极速"}.get(norm_tier, "智能混合")
        tier_short = {"auto": "智能", "b": "B档", "a": "A档"}.get(norm_tier, "智能")
        uname_compact = (username[:8] + "…") if len(username) > 8 else username
        if ai_cutoff:
            code = cutoff_code or ("ACCOUNT_DISABLED" if w.get("status") == "banned" else "INSUFFICIENT_CREDITS")
            self._cutoff_code = code
            full_locked = self._cutoff_policy == "lock_all"
            reason_txt = cutoff_reason or ("账号已停用" if code == "ACCOUNT_DISABLED" else "余额不足 · AI 已暂停")
            self.coach_page_status.setText(f"云端 AI 实时参谋 · {reason_txt}")
            self.coach_badge_lbl.setText(
                f"● {uname_compact} · 约剩{est_cur}局 · {'锁定' if full_locked else '暂停'}"
            )
            self.coach_badge_lbl.setToolTip(
                f"{username} · {format_balance_match_friendly(bal_credits, norm_tier)} · {reason_txt}"
            )
            self.coach_badge_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {'#F87171' if code == 'ACCOUNT_DISABLED' else '#C8AA6E'}; font-size: 11px; font-weight: bold;"
            )
            self._style_cutoff_banner(is_system_gate=(code == "ACCOUNT_DISABLED" or full_locked))
            suffix = (
                "（当前策略已锁定全部功能，请补充算力后立即解锁）"
                if full_locked
                else "（本地 5ms 大数据持续免费护航 · 充能后立即破封）"
            )
            self.cutoff_banner_lbl.setText(f"{reason_txt}{suffix}")
            self.cutoff_action_btn.setText("立即补充算力")
            self.cutoff_action_btn.setVisible(code != "ACCOUNT_DISABLED")
            self.cutoff_banner.show()
            self._apply_lock_all_ui(full_locked, reason=self.cutoff_banner_lbl.text(), code=code)
        else:
            self._cutoff_code = ""
            self.coach_page_status.setText(f"云端 AI 实时参谋 · ● {tier_lbl}已就绪")
            self.coach_badge_lbl.setText(f"● {uname_compact} · 约剩{est_cur}局 · {tier_short}")
            self.coach_badge_lbl.setToolTip(
                f"{username} · {format_balance_match_friendly(bal_credits, norm_tier)} · {tier_lbl}"
            )
            self.coach_badge_lbl.setStyleSheet("border: none; background: transparent; color: #09C269; font-size: 11px; font-weight: bold;")
            self.cutoff_banner.hide()
            self._apply_lock_all_ui(False)

    def set_update_status(self, text: str, *, is_error: bool = False) -> None:
        if not self._commercial or not hasattr(self, "update_status_lbl"):
            return
        if not text:
            self.update_status_lbl.hide()
            return
        color = "#F87171" if is_error else "#0AC8B9"
        self.update_status_lbl.setStyleSheet(f"border: none; background: transparent; color: {color}; font-size: 11px;")
        self.update_status_lbl.setText(text)
        self.update_status_lbl.show()

    def set_lol_rescan_result(self, lockfile_path: str, detail: str, ok: bool) -> None:
        if lockfile_path:
            self.lockfile_edit.setText(lockfile_path)
        if hasattr(self, "lol_detect_status_lbl"):
            color = "#09C269" if ok else "#FBBF24"
            self.lol_detect_status_lbl.setStyleSheet(f"border: none; background: transparent; color: {color}; font-size: 11px;")
            self.lol_detect_status_lbl.setText(("✓ " if ok else "! ") + detail)

    def _clear_coach_history(self) -> None:
        self._coach_history.clear()
        self._rebuild_coach_history_ui()

    def append_coach_history(
        self,
        hero_name: str,
        trigger: str,
        advice: CoachAdvicePayload,
        latency_ms: int = 0,
    ) -> None:
        trigger_zh = {
            "game_start": "开局",
            "augment_picking": "三选一",
            "augment_selected": "选完海克斯",
            "death_enemy_build_changed": "阵亡换装",
            "manual_f6": "手动 F6",
        }.get(trigger, trigger or "推演")
        ts = advice.updated_at.split("T")[-1][:8] if "T" in advice.updated_at else advice.updated_at[:8]
        top_items = " / ".join(f"[{it.priority}]{it.name}" for it in advice.next_items[:2])
        entry = {
            "ts": ts,
            "hero": hero_name or "未知英雄",
            "trigger": trigger_zh,
            "latency": f"{latency_ms}ms" if latency_ms > 0 else "",
            "source": advice.source,
            "items": top_items,
            "pick": advice.augment_pick_advice,
            "buy_now": advice.buy_now,
            "summary": advice.tactical_summary,
        }
        self._coach_history.insert(0, entry)
        self._coach_history = self._coach_history[:20]
        if "暂未收到参谋回写" in self.coach_page_body.text():
            self.set_coach_advice(advice)
        self._rebuild_coach_history_ui()

    def _rebuild_coach_history_ui(self) -> None:
        while self.coach_history_lay.count():
            it = self.coach_history_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        for rec in self._coach_history:
            row = QFrame()
            row.setStyleSheet(
                "QFrame { background-color: rgba(6, 18, 32, 210); border: 1px solid #463714; "
                "border-left: 2px solid #0AC8B9; border-radius: 2px; }"
            )
            rl = QVBoxLayout(row)
            rl.setContentsMargins(10, 6, 10, 6)
            rl.setSpacing(2)
            meta = f"{rec['ts']} · 【{rec['trigger']}】{rec['hero']}"
            if rec["latency"]:
                meta += f" · {rec['latency']}"
            m_lbl = QLabel(meta)
            m_lbl.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-size: 10px; font-weight: bold;")
            body_parts = []
            if rec["pick"]:
                body_parts.append(f"选牌：{rec['pick']}")
            if rec["items"]:
                body_parts.append(f"装备：{rec['items']}")
            if rec["buy_now"]:
                body_parts.append(f"买单：{rec['buy_now']}")
            if rec["summary"]:
                body_parts.append(f"战术：{rec['summary']}")
            b_lbl = QLabel("  |  ".join(body_parts))
            b_lbl.setWordWrap(True)
            b_lbl.setStyleSheet("border: none; background: transparent; color: #E2E8F0; font-size: 11px;")
            rl.addWidget(m_lbl)
            rl.addWidget(b_lbl)
            self.coach_history_lay.addWidget(row)
        self.coach_history_lay.addStretch(1)

    def _build_settings_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(22, 14, 22, 14)
        lay.setSpacing(10)

        form_card = HextechInteractiveCard("#C8AA6E")
        fl = QVBoxLayout(form_card)
        fl.setContentsMargins(16, 12, 16, 12)
        fl.setSpacing(8)

        lf_row = QHBoxLayout()
        lf_lbl = QLabel("《英雄联盟》客户端 / lockfile 路径：" if self._commercial else "国服 LeagueClient lockfile 路径：")
        lf_lbl.setStyleSheet("border: none; background: transparent; color: #C8AA6E;")
        self.lockfile_edit = QLineEdit(self._cfg.lockfile_path)
        browse_btn = HextechActionButton("浏览选择", variant="gold", height=30)
        browse_btn.clicked.connect(self._browse_lockfile)
        lf_row.addWidget(lf_lbl)
        lf_row.addWidget(self.lockfile_edit, 1)
        if self._commercial:
            self.rescan_lol_btn = HextechActionButton("一键重新侦测", variant="cyan", height=30)
            self.rescan_lol_btn.clicked.connect(self._rescan_lol_clicked)
            lf_row.addWidget(self.rescan_lol_btn)
        lf_row.addWidget(browse_btn)
        fl.addLayout(lf_row)

        if self._commercial:
            init_lol_txt = (
                f"✓ 当前路径：{self._cfg.lol_install_dir or self._cfg.lockfile_path}"
                if (self._cfg.lol_install_dir or self._cfg.lockfile_path)
                else "! 暂未定位客户端路径，可点击「一键重新侦测」或「浏览选择」"
            )
            self.lol_detect_status_lbl = QLabel(init_lol_txt)
            self.lol_detect_status_lbl.setWordWrap(True)
            self.lol_detect_status_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 11px;")
            fl.addWidget(self.lol_detect_status_lbl)

        chk_grid = QGridLayout()
        chk_grid.setContentsMargins(0, 2, 0, 2)
        chk_grid.setHorizontalSpacing(16)
        chk_grid.setVerticalSpacing(6)

        self.peek_chk = QCheckBox("阵亡逛商店自动微展抽屉，复活后自动收起")
        self.peek_chk.setChecked(self._cfg.auto_peek_on_death)
        self.combo_overlay_chk = QCheckBox("启用独立「海克斯联动/赌狗组合」浮窗 (F7)")
        self.combo_overlay_chk.setChecked(self._cfg.combo_overlay_enabled)
        self.force_mode_chk = QCheckBox("在所有大乱斗/自定义房间启用选人浮窗")
        self.force_mode_chk.setChecked(self._cfg.force_all_modes)
        self.coach_auto_chk = QCheckBox("AI 参谋就绪时，在开局/选牌/阵亡自动推演")
        self.coach_auto_chk.setChecked(self._cfg.auto_trigger_coach)
        self.auto_first_prismatic_chk = QCheckBox("首棱彩局自动切换大数据")
        self.auto_first_prismatic_chk.setToolTip(
            "首轮三选一全为棱彩海克斯时自动秒切至「首次必棱彩」大数据胜率表，对局结束后自动切回默认模式"
        )
        self.auto_first_prismatic_chk.setChecked(self._cfg.auto_first_prismatic_mode)
        self.smart_picker_gate_chk = QCheckBox("选卡明显首选时跳过冗余AI")
        self.smart_picker_gate_chk.setToolTip(
            "本地 0ms 大数据已给出显著首选且无联动冲突时跳过自动云端调用以节省积分（仍可随时按 F6 手动询问）"
        )
        self.smart_picker_gate_chk.setChecked(self._cfg.smart_picker_ai_gate)
        self.telemetry_chk = QCheckBox("匿名对局数据改进")
        self.telemetry_chk.setToolTip(
            "赛后匿名上报英雄、海克斯组合与推荐采纳率统计（不含任何账号隐私或聊天记录），帮助持续优化推荐准确度"
        )
        self.telemetry_chk.setChecked(self._cfg.telemetry_enabled)

        chk_grid.addWidget(self.peek_chk, 0, 0)
        chk_grid.addWidget(self.combo_overlay_chk, 0, 1)
        chk_grid.addWidget(self.force_mode_chk, 1, 0)
        chk_grid.addWidget(self.coach_auto_chk, 1, 1)
        chk_grid.addWidget(self.auto_first_prismatic_chk, 2, 0)
        chk_grid.addWidget(self.smart_picker_gate_chk, 2, 1)
        chk_grid.addWidget(self.telemetry_chk, 3, 0)
        chk_grid.setColumnStretch(0, 1)
        chk_grid.setColumnStretch(1, 1)
        fl.addLayout(chk_grid)

        # Compact Hextech Custom Hotkey Strip (launches full game-style keybind editor)
        hk_bar = QFrame()
        hk_bar.setFixedHeight(36)
        hk_bar.setStyleSheet(
            "QFrame { background-color: rgba(5, 16, 30, 220); border: 1px solid rgba(10, 200, 185, 110); "
            "border-left: 3px solid #C8AA6E; border-radius: 2px; }"
        )
        hk_row = QHBoxLayout(hk_bar)
        hk_row.setContentsMargins(10, 4, 8, 4)
        hk_row.setSpacing(8)
        hk_ttl = QLabel("◈ 游戏快捷键：")
        hk_ttl.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-size: 11px; font-weight: bold;")
        self.hotkeys_summary_lbl = QLabel(format_hotkeys_summary(self._cfg.hotkeys))
        self.hotkeys_summary_lbl.setStyleSheet("border: none; background: transparent; color: #CBD5E1; font-size: 10px;")
        self.open_hotkeys_btn = HextechActionButton("自定义快捷键", variant="cyan", height=26)
        self.open_hotkeys_btn.setMinimumWidth(104)
        self.open_hotkeys_btn.setToolTip("打开游戏级快捷键设置面板（支持双侧边栏收起、组合键与冲突互换）")
        self.open_hotkeys_btn.clicked.connect(lambda: self._on_open_hotkeys and self._on_open_hotkeys())
        hk_row.addWidget(hk_ttl)
        hk_row.addWidget(self.hotkeys_summary_lbl, 1)
        hk_row.addWidget(self.open_hotkeys_btn)
        fl.addWidget(hk_bar)

        save_row = QHBoxLayout()
        save_btn = HextechActionButton("确认并保存设置", variant="cyan", pointed_right=True, height=34)
        save_btn.clicked.connect(self._save_clicked)
        self.toggle_combo_btn = HextechActionButton("预览/切换海克斯联动浮窗 (F7)", variant="gold", height=34)
        self.toggle_combo_btn.clicked.connect(lambda: self._on_toggle_combo and self._on_toggle_combo())
        self.reset_overlay_sizes_btn = HextechActionButton("重置左右悬浮窗尺寸", variant="gold", height=34)
        self.reset_overlay_sizes_btn.setToolTip(
            "将左右两侧推荐悬浮窗恢复至当前屏幕（含 4K）自适应默认尺寸与字号；也可双击浮窗边角缩放把手一键恢复"
        )
        self.reset_overlay_sizes_btn.clicked.connect(
            lambda: self._on_reset_overlay_sizes and self._on_reset_overlay_sizes()
        )
        save_row.addWidget(save_btn)
        save_row.addWidget(self.toggle_combo_btn)
        save_row.addWidget(self.reset_overlay_sizes_btn)
        if not self._commercial:
            self.seal_sandbox_btn = HextechActionButton("预览海克斯封印与 720ms 破封沙盒", variant="gold", height=34)
            self.seal_sandbox_btn.clicked.connect(self.open_seal_sandbox)
            save_row.addWidget(self.seal_sandbox_btn)
        save_row.addStretch(1)
        fl.addLayout(save_row)
        lay.addWidget(form_card)

        diag_card = HextechInteractiveCard("#0397AB")
        dl = QVBoxLayout(diag_card)
        dl.setContentsMargins(16, 12, 16, 12)
        dl.setSpacing(6)
        diag_hdr_row = QHBoxLayout()
        diag_hdr_row.setContentsMargins(0, 0, 0, 0)
        diag_hdr_row.setSpacing(8)
        self.diag_hdr_lbl = QLabel(f"◈ 实时系统诊断与热键（{format_hotkeys_summary(self._cfg.hotkeys)}）")
        self.diag_hdr_lbl.setFont(display_font(10, bold=True, letter_spacing=0.8))
        self.diag_hdr_lbl.setStyleSheet("border: none; background: transparent; color: #C8AA6E;")
        self.diag_feedback_btn = HextechActionButton("问题反馈 & 上报日志", variant="cyan", height=26)
        self.diag_feedback_btn.setMinimumWidth(148)
        self.diag_feedback_btn.setToolTip("打开问题反馈窗，自动打包最近 20 局滚动脱敏日志回传开发后台")
        self.diag_feedback_btn.clicked.connect(lambda: self._on_open_feedback and self._on_open_feedback())
        diag_hdr_row.addWidget(self.diag_hdr_lbl, 1)
        diag_hdr_row.addWidget(self.diag_feedback_btn)
        self.diag_text = QTextEdit()
        self.diag_text.setReadOnly(True)
        dl.addLayout(diag_hdr_row)
        dl.addWidget(self.diag_text, 1)
        lay.addWidget(diag_card, 1)

        self.pages.addWidget(page)

    def update_hotkeys_display(self, hk: HotkeyConfig, occupied: dict[str, str] | None = None) -> None:
        """Dynamically sync all visible hotkey labels/tooltips after user saves custom bindings."""
        self._cfg.hotkeys = hk
        k_drawer = (hk.toggle_drawer or "").strip()
        k_combo = (hk.toggle_combo or "").strip()
        k_coach = (hk.call_coach or "").strip()
        k_cal = (hk.calibrate_roi or "").strip()
        k_sidebars = (hk.toggle_sidebars or "").strip()

        drawer_hint = f" ({k_drawer})" if k_drawer else ""
        sidebar_hint = f" | 双侧栏同步 ({k_sidebars})" if k_sidebars else ""
        self.play_btn.setToolTip(f"点击展开 / 收起局内右侧出装与参谋悬浮抽屉{drawer_hint}{sidebar_hint}")

        self.cal_btn.setText(f"校准三选一 {k_cal}" if k_cal else "校准三选一")
        self.footer_coach_btn.setText(f"呼叫参谋推演 {k_coach}" if k_coach else "呼叫参谋推演")
        if self.tab2_push_btn is not None:
            self.tab2_push_btn.setText(f"立即推演 {k_coach}" if k_coach else "立即推演")

        combo_suffix = f" ({k_combo})" if k_combo else ""
        self.combo_overlay_chk.setText(f"启用独立「海克斯联动/赌狗组合」浮窗{combo_suffix}")
        self.toggle_combo_btn.setText(f"预览/切换海克斯联动浮窗{combo_suffix}")

        summary = format_hotkeys_summary(hk)
        occ_cnt = len(occupied or {})
        if self.hotkeys_summary_lbl is not None:
            if occ_cnt > 0:
                self.hotkeys_summary_lbl.setText(f"{summary}  ·  ! {occ_cnt} 项被其他软件占用")
                self.hotkeys_summary_lbl.setStyleSheet(
                    "border: none; background: transparent; color: #FCA5A5; font-size: 10px; font-weight: bold;"
                )
            else:
                self.hotkeys_summary_lbl.setText(summary)
                self.hotkeys_summary_lbl.setStyleSheet(
                    "border: none; background: transparent; color: #CBD5E1; font-size: 10px;"
                )
            self.hotkeys_summary_lbl.setToolTip(summary)
        if self.diag_hdr_lbl is not None:
            self.diag_hdr_lbl.setText(f"◈ 实时系统诊断与热键（{summary}）")

    def _switch_page(self, idx: int, *, force: bool = False) -> None:
        if not force and 0 <= idx < len(self.nav_btns) and not self.nav_btns[idx].isEnabled():
            return
        if self.pages.currentIndex() == idx:
            return
        self.pages.setCurrentIndex(idx)
        for i, btn in enumerate(self.nav_btns):
            btn.set_active(i == idx)
        # Smooth OutCubic fade-in transition on page change
        w = self.pages.currentWidget()
        if w is not None:
            eff = QGraphicsOpacityEffect(w)
            w.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", self)
            anim.setDuration(190)
            anim.setStartValue(0.25)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda: w.setGraphicsEffect(None))
            anim.start()
            self._fade_anim = anim

    def _trigger_mode_switch(self, patch: str, mode: str, band: str) -> None:
        self.mode_normal_btn.set_active(mode == "normal" and not band)
        self.mode_prism_btn.set_active(mode == "first-prismatic")
        self.mode_high_btn.set_active(band == "high")
        if self._on_switch_dataset:
            self._on_switch_dataset(patch, mode, band)

    def _format_initial_key_hint(self) -> str:
        eff_key = self._cfg.resolved_gemini_api_key()
        proxy_s = self._cfg.proxy_url or "直连（无代理）"
        if eff_key:
            src = "本地配置" if self._cfg.gemini_api_key.strip() else "环境变量 GEMINI_API_KEY"
            th_s = (
                f"自适应(最高 {self._cfg.gemini_thinking_level})"
                if self._cfg.gemini_adaptive_thinking
                else self._cfg.gemini_thinking_level
            )
            return f"✓ 已加载 Key（{mask_api_key(eff_key)} · {src}） · 模型：{self._cfg.gemini_model} · 思考：{th_s} · 代理：{proxy_s}"
        return f"◇ 尚未配置 Gemini API Key（代理：{proxy_s}），当前将使用本地 5ms 大数据引擎或 18421 外部桥接"

    def _toggle_api_key_visibility(self) -> None:
        if self.gemini_key_edit.echoMode() == QLineEdit.EchoMode.Password:
            self.gemini_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self.toggle_key_btn.setText("隐藏")
        else:
            self.gemini_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.toggle_key_btn.setText("显示")

    def _collect_cfg_from_ui(self) -> AppConfig:
        self._cfg.lockfile_path = self.lockfile_edit.text().strip()
        self._cfg.auto_peek_on_death = self.peek_chk.isChecked()
        self._cfg.combo_overlay_enabled = self.combo_overlay_chk.isChecked()
        self._cfg.force_all_modes = self.force_mode_chk.isChecked()
        self._cfg.auto_trigger_coach = self.coach_auto_chk.isChecked()
        self._cfg.auto_first_prismatic_mode = self.auto_first_prismatic_chk.isChecked()
        self._cfg.smart_picker_ai_gate = self.smart_picker_gate_chk.isChecked()
        self._cfg.telemetry_enabled = self.telemetry_chk.isChecked()
        if self._commercial:
            self._cfg.proxy_url = ""
            self._cfg.bridge_enabled = False
            self._cfg.coach_mode = "off" if self._cfg.coach_mode == "off" else "cloud"
            return self._cfg

        if self.proxy_edit is not None:
            self._cfg.proxy_url = self.proxy_edit.text().strip()
        if self.coach_mode_combo is not None:
            self._cfg.coach_mode = str(self.coach_mode_combo.currentData() or "auto")
        if self.gemini_key_edit is not None:
            self._cfg.gemini_api_key = self.gemini_key_edit.text().strip()
        if self.gemini_model_combo is not None:
            self._cfg.gemini_model = self.gemini_model_combo.currentText().strip() or "gemini-4-hydrogen"
        if self.gemini_thinking_combo is not None:
            self._cfg.gemini_thinking_level = str(self.gemini_thinking_combo.currentData() or "medium")
        if self.gemini_adaptive_chk is not None:
            self._cfg.gemini_adaptive_thinking = self.gemini_adaptive_chk.isChecked()
        return self._cfg

    def _test_gemini_clicked(self) -> None:
        if self._commercial or self.test_gemini_btn is None or self.gemini_conn_status_lbl is None:
            return
        cfg = self._collect_cfg_from_ui()
        if self._on_save_settings:
            self._on_save_settings(cfg)
        self.test_gemini_btn.setEnabled(False)
        test_lbl = "自适应快速自检 low" if cfg.gemini_adaptive_thinking else cfg.gemini_thinking_level
        self.gemini_conn_status_lbl.setText(f"⏳ 正在通过 {cfg.proxy_url or '直连'} 验证 {cfg.gemini_model} ({test_lbl}) 并预热长连接...")
        self.gemini_conn_status_lbl.setStyleSheet("border: none; background: transparent; color: #FBBF24; font-size: 11px;")
        if self._on_test_gemini:
            self._on_test_gemini(cfg)

    def set_test_connection_result(self, ok: bool, message: str) -> None:
        if self._commercial or self.test_gemini_btn is None or self.gemini_conn_status_lbl is None:
            return
        self.test_gemini_btn.setEnabled(True)
        self.gemini_conn_status_lbl.setText(("✓ " if ok else "✕ ") + message)
        color = "#09C269" if ok else "#F87171"
        self.gemini_conn_status_lbl.setStyleSheet(f"border: none; background: transparent; color: {color}; font-size: 11px;")

    def set_coach_online(
        self,
        online: bool,
        badge_text: str = "",
        banner_text: str = "",
    ) -> None:
        if online:
            self.coach_badge_lbl.setText(badge_text or "● AI 参谋在线")
            self.coach_badge_lbl.setStyleSheet("border: none; background: transparent; color: #09C269; font-size: 11px; font-weight: bold;")
            self.coach_page_status.setText(banner_text or "AI 实时参谋 · ● 在线已就绪")
        else:
            self.coach_badge_lbl.setText(badge_text or "● 本地 5ms 引擎运行中")
            self.coach_badge_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 11px;")
            self.coach_page_status.setText(banner_text or "AI 实时参谋 · 待配置或接入")

    def set_coach_advice(self, advice: CoachAdvicePayload | None) -> None:
        if advice is None:
            return
        lines: list[str] = []
        if advice.augment_pick_advice:
            lines.append(f"【三选一建议】{advice.augment_pick_advice}")
        for it in advice.next_items:
            lines.append(f"◈ [{it.priority}] {it.name} — {it.reason}")
        if advice.tactical_summary:
            lines.append(f"\n战术思路：{advice.tactical_summary}")
        lines.append(f"\n更新时间：{advice.updated_at}（来源：{advice.source}）")
        self.coach_page_body.setText("\n".join(lines))

    def set_datasets(self, datasets: list[DatasetDescriptor]) -> None:
        self._datasets = list(datasets)
        normal_ds = next((ds for ds in self._datasets if ds.mode == "normal"), None)
        if normal_ds is not None:
            self._normal_patch = "latest"
            disp = format_display_patch(normal_ds.patch).split("（")[0]
            self.mode_normal_btn.setText(f"{disp} 常规模式")
        prism_ds = next((ds for ds in self._datasets if ds.mode == "first-prismatic"), None)
        if prism_ds is not None:
            self._prism_patch = prism_ds.patch
            disp = format_display_patch(prism_ds.patch).split("（")[0]
            self.mode_prism_btn.setText(f"{disp} 首次必棱彩")
        for ds in self._datasets:
            if ds.mode == "normal" and ds.bands:
                self._high_band_patch = ds.patch
                disp = format_display_patch(ds.patch).split("（")[0]
                self.mode_high_btn.setText(f"{disp} 前5%高分段")
                break

    def _get_item_pixmap(self, item_id: str, size: int = 26) -> QPixmap | None:
        norm = normalize_item_id(item_id)
        if not norm or self._store is None:
            return None
        key = (norm, size)
        cached = self._item_icon_cache.get(key)
        if cached is not None:
            return cached
        path = self._store.static.icons_dir / "item" / f"{norm}.png"
        if path.exists() and path.stat().st_size > 200:
            pm = QPixmap(str(path))
            if not pm.isNull():
                scaled = pm.scaled(
                    size,
                    size,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._item_icon_cache[key] = scaled
                return scaled
        if norm not in self._fetching_item_icons:
            self._fetching_item_icons.add(norm)
            store = self._store

            def _fetch() -> None:
                try:
                    p = store.static.ensure_icon("item", norm)
                    if p is not None and p.exists():
                        self._ui_invoker.invoke.emit(self._refresh_current_item_icons)
                finally:
                    self._fetching_item_icons.discard(norm)

            threading.Thread(target=_fetch, name=f"HexLwItem-{norm}", daemon=True).start()
        return None

    def _refresh_current_item_icons(self) -> None:
        if self._selected_hero_id and self.pages.currentIndex() == 0:
            self._render_hero_detail(self._selected_hero_id)
        elif self.pages.currentIndex() == 1 and self.special_list.currentRow() >= 0:
            self._on_special_selected(self.special_list.currentRow())

    def bind_store(self, store: DataStore) -> None:
        self._store = store
        if store.descriptor:
            disp = format_display_patch(store.descriptor.patch)
            short_ver = disp.split("（")[0]
            mode = "首次必棱彩" if store.descriptor.mode == "first-prismatic" else "常规"
            band = f" · {store.active_band}" if store.active_band else ""
            self.status_title_lbl.setText(f"HEXHELPER · {short_ver}")
            self.status_lbl.setText(
                f"{disp} · {mode}{band}  |  {len(store.all_heroes())} 英雄 · {len(store.all_augments())} 海克斯"
            )
            self.version_lbl.setText(f"v{APP_VERSION} · {short_ver}")
        self._filter_heroes(self.search_edit.text())
        if self.hero_list.count() > 0 and self.hero_list.currentRow() < 0:
            self.hero_list.setCurrentRow(0)
        if self.special_list.currentRow() >= 0:
            self._on_special_selected(self.special_list.currentRow())

    def update_diagnostics(self, lines: list[str]) -> None:
        self.diag_text.setPlainText("\n".join(lines))

    def _filter_heroes(self, query: str) -> None:
        self.hero_list.clear()
        if self._store is None:
            return
        q = query.strip().lower()
        for h in self._store.all_heroes():
            if q and q not in h.name.lower() and q not in h.title.lower():
                continue
            tier = score_hero_tier(h.win_rate)
            item = QListWidgetItem(f"[{tier:<2}]  #{h.rank:<3}  {h.name}    {h.win_rate * 100:.2f}%")
            item.setData(Qt.ItemDataRole.UserRole, h.hero_id)
            cached_pm = self._icon_cache.get(h.hero_id)
            if cached_pm is not None:
                item.setIcon(QIcon(cached_pm))
            self.hero_list.addItem(item)

    def _clear_detail(self) -> None:
        old = self.detail_scroll.takeWidget()
        if old is not None:
            old.hide()
            old.setParent(None)
            old.deleteLater()
        self.detail_container = QWidget()
        self.detail_container.setStyleSheet("background: transparent;")
        self.detail_lay = QVBoxLayout(self.detail_container)
        self.detail_lay.setContentsMargins(4, 2, 10, 10)
        self.detail_lay.setSpacing(10)
        self.detail_scroll.setWidget(self.detail_container)

    def _on_hero_selected(self, cur: QListWidgetItem | None, _prev=None) -> None:
        if cur is None or self._store is None:
            return
        hid = str(cur.data(Qt.ItemDataRole.UserRole))
        self._selected_hero_id = hid
        self._render_hero_detail(hid)

        if hid not in self._icon_cache:
            store = self._store

            def fetch_icon() -> None:
                path = store.static.ensure_icon("champion", hid)
                if path is not None:
                    def apply_icon() -> None:
                        pm = _circular_icon(str(path), 62, "#C8AA6E")
                        if not pm.isNull():
                            self._icon_cache[hid] = pm
                            if self._selected_hero_id == hid:
                                self._render_hero_detail(hid)

                    self._ui_invoker.invoke.emit(apply_icon)

            threading.Thread(target=fetch_icon, daemon=True).start()

    def _render_hero_detail(self, hid: str) -> None:
        if self._store is None:
            return
        hero = self._store.get_hero(hid)
        if hero is None:
            return

        self._clear_detail()
        tier = score_hero_tier(hero.win_rate)

        # 1. Hero Showcase Banner Card with Shadow & Sculpted Serif Title
        banner = HextechInteractiveCard("#C8AA6E")
        banner.setMinimumWidth(0)
        bh = QHBoxLayout(banner)
        bh.setContentsMargins(12, 10, 12, 10)
        bh.setSpacing(10)

        avatar = QLabel()
        avatar.setFixedSize(56, 56)
        avatar.setStyleSheet("border: none; background: transparent;")
        pm = self._icon_cache.get(hid)
        if pm is None:
            disk_ico = self._store.static.icons_dir / "champion" / f"{hid}.png"
            if disk_ico.exists() and disk_ico.stat().st_size > 200:
                loaded_pm = _circular_icon(str(disk_ico), 54, "#C8AA6E")
                if not loaded_pm.isNull():
                    self._icon_cache[hid] = loaded_pm
                    pm = loaded_pm
        avatar.setPixmap(pm if pm is not None else load_app_pixmap(54))
        bh.addWidget(avatar)

        title_col = QVBoxLayout()
        title_col.setSpacing(4)
        top_line = QHBoxLayout()
        t_badge = QLabel(tier)
        t_badge.setFont(latin_serif_font(10, bold=True, letter_spacing=0.8))
        t_badge.setStyleSheet(tier_badge_qss(tier))
        name_lbl = QLabel(hero.name)
        name_lbl.setFont(display_font(16, bold=True, letter_spacing=1.0))
        name_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
        sub_lbl = QLabel(f"·  {hero.title}")
        sub_lbl.setMinimumWidth(0)
        sub_lbl.setFont(display_font(10, bold=False, letter_spacing=0.4))
        sub_lbl.setStyleSheet("border: none; background: transparent; color: #A09B8C;")
        top_line.addWidget(t_badge)
        top_line.addWidget(name_lbl)
        top_line.addWidget(sub_lbl)
        top_line.addStretch(1)
        title_col.addLayout(top_line)

        spells = self._store.get_hero_spells(hid)[:2]
        if spells:
            sp_txt = "  |  ".join(
                f"{self._store.static.get_spell_name(s.spell_ids[0])}+{self._store.static.get_spell_name(s.spell_ids[1])} ({s.win_rate * 100:.1f}%)"
                for s in spells
            )
            sp_lbl = QLabel(f"◈ 推荐召唤师技能：{sp_txt}")
            sp_lbl.setWordWrap(True)
            sp_lbl.setMinimumWidth(0)
            sp_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 11px;")
            title_col.addWidget(sp_lbl)
        bh.addLayout(title_col, 1)

        # Metallic KPI Tiles with sculpted Cinzel numerals (compact padding so 940px never clips!)
        for k_title, k_val, k_color in (
            ("版本胜率", f"{hero.win_rate * 100:.2f}%", "#0AC8B9" if hero.win_rate >= 0.50 else "#F0E6D2"),
            ("全英雄排名", f"#{hero.rank}", "#C8AA6E"),
            ("有效场次", f"{hero.games:,}", "#F0E6D2"),
        ):
            kpi = QFrame()
            kpi.setStyleSheet(
                "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                "stop:0 rgba(8, 18, 32, 210), stop:1 rgba(2, 9, 18, 225)); "
                "border: 1px solid #5C461E; border-radius: 2px; }"
            )
            kl = QVBoxLayout(kpi)
            kl.setContentsMargins(8, 5, 8, 5)
            kl.setSpacing(2)
            l1 = QLabel(k_title)
            l1.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 9px;")
            l2 = QLabel(k_val)
            l2.setFont(latin_serif_font(11, bold=True, letter_spacing=0.6))
            l2.setStyleSheet(f"border: none; background: transparent; color: {k_color};")
            kl.addWidget(l1, alignment=Qt.AlignmentFlag.AlignCenter)
            kl.addWidget(l2, alignment=Qt.AlignmentFlag.AlignCenter)
            bh.addWidget(kpi)

        self.detail_lay.addWidget(banner)

        # 2. Top 10 Augments 2-Column Tactile Hextech Grid
        scored = []
        for aid, st in self._store.get_hero_augments(hid).items():
            aug = self._store.get_augment(aid)
            if not aug:
                continue
            rdist = self._store.get_hero_rarity_distribution(hid, aug.rarity)
            scored.append(
                score_hero_augment(
                    aid,
                    aug.name,
                    aug.rarity,
                    hero.win_rate,
                    st.wins,
                    st.games,
                    st.low_sample,
                    rarity_dist=rdist,
                )
            )
        scored.sort(key=lambda s: s.delta, reverse=True)

        aug_hdr = QLabel("◈  最强海克斯 TOP 10（贝叶斯防噪排序）")
        aug_hdr.setFont(display_font(11, bold=True, letter_spacing=1.5))
        aug_hdr.setStyleSheet("color: #C8AA6E; padding-top: 2px;")
        self.detail_lay.addWidget(aug_hdr)

        grid_wrap = QWidget()
        grid_wrap.setMinimumWidth(0)
        grid_wrap.setStyleSheet("background: transparent;")
        grid = QGridLayout(grid_wrap)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for idx, s in enumerate(scored[:10]):
            r_color, r_label = RARITY_COLORS.get(s.rarity, ("#785A28", ""))
            card = HextechInteractiveCard(r_color)
            card.setMinimumWidth(0)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            cl = QHBoxLayout(card)
            cl.setContentsMargins(8, 6, 8, 6)
            cl.setSpacing(6)

            tb = QLabel(s.tier)
            tb.setFixedWidth(28)
            tb.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tb.setFont(latin_serif_font(9, bold=True, letter_spacing=0.4))
            tb.setStyleSheet(tier_badge_qss(s.tier))

            mid = QVBoxLayout()
            mid.setSpacing(1)
            nm = QLabel(s.name)
            nm.setMinimumWidth(0)
            nm.setFont(display_font(10, bold=True, letter_spacing=0.4))
            nm.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
            meta = QLabel(f"{r_label} · {s.sample_label}")
            meta.setMinimumWidth(0)
            meta.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 10px;")
            mid.addWidget(nm)
            mid.addWidget(meta)

            right = QVBoxLayout()
            right.setSpacing(1)
            dl = QLabel(f"{s.delta * 100:+.2f}%")
            dl.setAlignment(Qt.AlignmentFlag.AlignRight)
            dl.setFont(display_font(10, bold=True, letter_spacing=0.4))
            dl.setStyleSheet("border: none; background: transparent; color: #0AC8B9;")
            wl = QLabel(f"胜率 {s.raw_win_rate * 100:.1f}%")
            wl.setAlignment(Qt.AlignmentFlag.AlignRight)
            wl.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-size: 10px;")
            right.addWidget(dl)
            right.addWidget(wl)

            cl.addWidget(tb)
            cl.addLayout(mid, 1)
            cl.addLayout(right)
            grid.addWidget(card, idx // 2, idx % 2)
        self.detail_lay.addWidget(grid_wrap)

        # 3. Destiny's Weapon Showcase
        destiny = rank_destiny_items(self._store, hid, limit=4)
        if destiny:
            d_card = HextechInteractiveCard("#C89B3C")
            d_card.setMinimumWidth(0)
            dl = QVBoxLayout(d_card)
            dl.setContentsMargins(12, 8, 12, 8)
            dl.setSpacing(6)
            dh = QLabel("◈  命运之武器（3081）专属绑定装胜率榜")
            dh.setFont(display_font(10, bold=True, letter_spacing=1.2))
            dh.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
            dl.addWidget(dh)
            d_grid = QGridLayout()
            d_grid.setSpacing(6)
            for d_idx, d in enumerate(destiny):
                pill = QLabel(f"◇ {d.name}  {d.win_rate * 100:.1f}% ({d.delta * 100:+.1f}%)")
                pill.setMinimumWidth(0)
                pill.setStyleSheet(
                    "background-color: rgba(1, 10, 19, 195); border: 1px solid #C89B3C; "
                    "border-radius: 2px; padding: 4px 8px; color: #F0E6D2; font-weight: bold; font-size: 11px;"
                )
                d_grid.addWidget(pill, d_idx // 2, d_idx % 2)
            d_grid.setColumnStretch(0, 1)
            d_grid.setColumnStretch(1, 1)
            dl.addLayout(d_grid)
            self.detail_lay.addWidget(d_card)

        # 4. Ordered Core 3-Item Builds Top 3
        builds = self._store.get_core_builds(hid)[:3]
        sig = get_hero_signature_db().get(hid, hero.name)
        if sig is not None:
            sig_names = " › ".join(self._store.get_item_info(i).name for i in sig.signature_items[:5])
            sig_lbl = QLabel(f"◈ 海克斯招牌定位（低权重参考，以大数据为准）：【{sig.archetype}】{sig_names} — {sig.note}")
            sig_lbl.setWordWrap(True)
            sig_lbl.setStyleSheet(
                "background-color: rgba(12, 30, 48, 175); border: 1px solid rgba(200, 170, 110, 115); "
                "border-radius: 2px; padding: 6px 10px; color: #F0E6D2; font-size: 11px;"
            )
            self.detail_lay.addWidget(sig_lbl)
        if builds:
            b_hdr = QLabel("◈  26.23 高胜率有序三件套")
            b_hdr.setFont(display_font(11, bold=True, letter_spacing=1.5))
            b_hdr.setStyleSheet("color: #C8AA6E; padding-top: 2px;")
            self.detail_lay.addWidget(b_hdr)

            for b in builds:
                bc = HextechInteractiveCard("#0397AB")
                bl = QHBoxLayout(bc)
                bl.setContentsMargins(12, 7, 12, 7)
                bl.setSpacing(6)
                for idx_i, iid in enumerate(b.items):
                    if idx_i > 0:
                        sep = QLabel("›")
                        sep.setStyleSheet("border: none; background: transparent; color: #785A28; font-weight: bold; font-size: 13px;")
                        bl.addWidget(sep)
                    info = self._store.get_item_info(iid)
                    ico = QLabel()
                    ico.setFixedSize(26, 26)
                    ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    ico.setStyleSheet("background-color: #091428; border: 1px solid #C89B3C; border-radius: 2px;")
                    ipm = self._get_item_pixmap(iid, 24)
                    if ipm is not None:
                        ico.setPixmap(ipm)
                    else:
                        ico.setText(info.name[:1])
                    ico.setToolTip(f"{info.name} ({info.gold}G)")
                    nm_lbl = QLabel(info.name)
                    nm_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-weight: bold; font-size: 12px;")
                    bl.addWidget(ico)
                    bl.addWidget(nm_lbl)
                ml = QLabel(f"胜率 {b.win_rate * 100:.2f}%  ·  选用 {b.pick_rate * 100:.1f}%  ·  {b.games:,}场")
                ml.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 11px;")
                bl.addStretch(1)
                bl.addWidget(ml)
                self.detail_lay.addWidget(bc)

        # 5. Bottom 4 Traps
        if len(scored) >= 4:
            trap_names = "   |   ".join(f"{s.name} ({s.delta * 100:+.1f}%)" for s in scored[-4:])
            trap_lbl = QLabel(f"◇ 避坑海克斯：{trap_names}")
            trap_lbl.setWordWrap(True)
            trap_lbl.setStyleSheet(
                "background-color: rgba(110, 24, 24, 115); border: 1px solid rgba(220, 38, 38, 130); "
                "border-radius: 2px; padding: 7px 12px; color: #FCA5A5; font-size: 11px;"
            )
            self.detail_lay.addWidget(trap_lbl)

        self.detail_lay.addStretch(1)

    def _on_rescan_lol_done(self, info, err: str = "") -> None:
        self._lol_rescan_in_flight = False
        if self._closed:
            return
        if self.rescan_lol_btn is not None:
            self.rescan_lol_btn.setEnabled(True)
        if err:
            self.set_lol_rescan_result("", f"侦测客户端失败：{err}", False)
            return
        if info is not None:
            self._cfg.lockfile_path = str(info.lockfile_path)
            self._cfg.lol_install_dir = str(info.install_dir)
            self.set_lol_rescan_result(str(info.lockfile_path), f"已定位：{info.install_dir} [{info.source}]", True)
            if self._on_save_settings:
                self._on_save_settings(self._collect_cfg_from_ui())
        else:
            self.set_lol_rescan_result("", "未自动检测到《英雄联盟》客户端，请点「浏览选择」手动指定", False)

    def _rescan_lol_clicked(self) -> None:
        if self._on_rescan_lol:
            self._on_rescan_lol()
            return
        if self._lol_rescan_in_flight:
            return
        self._lol_rescan_in_flight = True
        if self.rescan_lol_btn is not None:
            self.rescan_lol_btn.setEnabled(False)
        if hasattr(self, "lol_detect_status_lbl"):
            self.lol_detect_status_lbl.setStyleSheet("border: none; background: transparent; color: #FBBF24; font-size: 11px;")
            self.lol_detect_status_lbl.setText("⏳ 正在全盘侦测《英雄联盟》客户端路径...")
        preferred = self.lockfile_edit.text().strip()

        def worker() -> None:
            try:
                clear_locator_cache()
                info = detect_primary_lol_install(preferred_path=preferred, allow_disk_scan=True)
                self._ui_invoker.invoke.emit(lambda r=info: self._on_rescan_lol_done(r))
            except Exception as exc:
                self._ui_invoker.invoke.emit(lambda e=str(exc): self._on_rescan_lol_done(None, err=e))

        threading.Thread(target=worker, name="HexLolRescan", daemon=True).start()

    def _browse_lockfile(self) -> None:
        if self._commercial:
            chosen_dir = QFileDialog.getExistingDirectory(self, "选择《英雄联盟》安装根目录或 LeagueClient 目录")
            if chosen_dir:
                from pathlib import Path

                info = validate_lol_install_dir(Path(chosen_dir), source="manual")
                if info is not None:
                    self._cfg.lol_install_dir = str(info.install_dir)
                    self._cfg.lockfile_path = str(info.lockfile_path)
                    self.set_lol_rescan_result(str(info.lockfile_path), f"已手动定位：{info.install_dir}", True)
                    if self._on_save_settings:
                        self._on_save_settings(self._collect_cfg_from_ui())
                    return
                self.set_lol_rescan_result(
                    "",
                    f"所选目录未检测到 LeagueClient.exe / League of Legends.exe：{chosen_dir}，请重新选择《英雄联盟》安装目录",
                    False,
                )
            return
        path, _ = QFileDialog.getOpenFileName(self, "选择 LeagueClient lockfile")
        if path:
            self.lockfile_edit.setText(path)

    def _save_clicked(self) -> None:
        cfg = self._collect_cfg_from_ui()
        if self._on_save_settings:
            self._on_save_settings(cfg)
        if not self._commercial and self.gemini_conn_status_lbl is not None:
            self.gemini_conn_status_lbl.setText(self._format_initial_key_hint())
            color = "#09C269" if cfg.resolved_gemini_api_key() else "#0AC8B9"
            self.gemini_conn_status_lbl.setStyleSheet(f"border: none; background: transparent; color: {color}; font-size: 11px;")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closed = True
        super().closeEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 68:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_offset = None
