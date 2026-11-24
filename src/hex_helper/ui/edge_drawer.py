"""v0.3.0 Right-edge LoL Hextech Visual Icon HUD:
2-second glance Next-Buy hero card with real DDragon item icons, visual build-path strip,
zero stale timeline advice, and 1-click auto-pick swap.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from PySide6.QtCore import QEasingCurve, QPoint, QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QAction, QGuiApplication, QMouseEvent, QPixmap, QScreen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from hex_helper.bridge.protocol import CoachAdvicePayload, normalize_opening_brief
from hex_helper.config import HotkeyConfig, format_display_patch
from hex_helper.data.provider import AugmentInfo, DatasetDescriptor
from hex_helper.data.special_builds import get_special_augment_db
from hex_helper.data.store import DataStore
from hex_helper.edition import is_commercial_edition
from hex_helper.game.lobby_scout import LobbyScoutSnapshot, PlayerScoutCard
from hex_helper.scoring.build_synergy import item_is_owned, normalize_item_id
from hex_helper.scoring.destiny import DESTINY_AUGMENT_ID, rank_destiny_items
from hex_helper.scoring.matchup_optimizer import (
    ELIXIR_IDS,
    DynamicItemRec,
    MatchupThreatProfile,
    NextBuyPlan,
    analyze_inventory_slots,
    compute_item_buy_now,
)
from hex_helper.ui.account_dialogs import format_balance_match_friendly
from hex_helper.ui.hex_seal_overlay import (
    CoreEnergyState,
    CoreLifecycleSnapshot,
    HextechSealOverlay,
    SealMode,
    build_live_peephole_hint,
)
from hex_helper.ui.hextech_widgets import (
    HextechActionButton,
    HextechGoldRingIcon,
    HextechPulseFrame,
    HextechShimmerBar,
)
from hex_helper.ui.resizable_overlay import (
    FramelessResizeController,
    HextechCornerGrip,
    HextechEdgeHandle,
    OverlayScaleMetrics,
    compute_auto_overlay_scale,
    compute_overlay_scale,
)
from hex_helper.ui.theme import (
    BASE_OVERLAY_QSS,
    RARITY_COLORS,
    display_font,
    latin_serif_font,
    load_app_pixmap,
)
from hex_helper.ui.win32_window import UiBridgeInvoker, show_no_activate
from hex_helper.vision.transmute import NON_TARGET_AUGMENT_IDS, PendingTransmute, is_transmute_augment

_SHORT_ITEM_ALIASES: dict[str, str] = {
    "灭世者的死亡之帽": "大帽",
    "中娅沙漏": "中娅",
    "纳什之牙": "纳什",
    "黄昏与黎明": "黄昏",
    "炽天使之拥": "炽天使",
    "大天使之杖": "大天使",
    "女妖面纱": "女妖",
    "虚空之杖": "法穿棒",
    "海克斯科技枪刃": "科技枪",
    "莫雷洛秘典": "鬼书",
    "卢安娜的飓风": "飓风",
    "破败王者之刃": "破败",
    "凡性的提醒": "凡性",
    "多米尼克领主的致意": "大轻语",
    "玛莫提乌斯之噬": "饮魔刀",
    "兰德里的折磨": "面具",
    "卢登的伙伴": "卢登",
    "卢登的回声": "卢登",
    "风暴狂涌": "风暴",
    "法师之靴": "法穿鞋",
    "轻灵之靴": "轻灵鞋",
    "狂战士胫甲": "攻速鞋",
    "水银之靴": "水银鞋",
    "铁板靴": "布甲鞋",
    "明朗之靴": "CD鞋",
    "巫术合剂": "法合剂",
}


def _short_item_name(name: str) -> str:
    if name in _SHORT_ITEM_ALIASES:
        return _SHORT_ITEM_ALIASES[name]
    clean = name.replace("的", "")
    return clean[:4] if len(clean) > 4 else clean


def _clip_text(text: str, max_len: int) -> str:
    s = " ".join(text.strip().split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


class EdgeBuildDrawer(QWidget):
    """Collapsible right-edge LoL Hextech Visual Decision HUD (48px pill <-> 368px visual HUD)."""

    COLLAPSED_W = 48
    COLLAPSED_MIN_H = 184
    EXPANDED_W = 368
    MIN_EXPANDED_W = 288
    MAX_EXPANDED_W = 860
    EXPANDED_MIN_H = 220
    MAX_EXPANDED_H = 1850
    _MIN_COMFORT_H = 300
    _QWIDGETSIZE_MAX = 16777215

    def __init__(
        self,
        on_select_augment: Callable[[str], None] | None = None,
        on_remove_augment: Callable[[int], None] | None = None,
        on_replace_last_augment: Callable[[str], None] | None = None,
        on_resolve_transmute: Callable[[str], None] | None = None,
        on_dismiss_transmute: Callable[[], None] | None = None,
        on_call_coach: Callable[[], None] | None = None,
        on_switch_dataset: Callable[[str, str, str], None] | None = None,
        on_manual_hero: Callable[[str], None] | None = None,
        on_pos_changed: Callable[[list[int]], None] | None = None,
        on_size_changed: Callable[[list[int] | None], None] | None = None,
        on_scale_changed: Callable[[float | None], None] | None = None,
        on_closed_by_user: Callable[[], None] | None = None,
        on_switch_tier: Callable[[str], None] | None = None,
        on_recharge_click: Callable[[], None] | None = None,
        on_login_click: Callable[[], None] | None = None,
        on_check_update: Callable[[], None] | None = None,
        on_core_action: Callable[[str], None] | None = None,
        on_opening_brief_seal_dismissed: Callable[[], None] | None = None,
        initial_tier: str = "a",
        initial_pos: list[int] | None = None,
        initial_size: list[int] | None = None,
        initial_scale: float | None = None,
        y_ratio: float = 0.14,
    ) -> None:
        super().__init__()
        self._commercial = is_commercial_edition()
        self._on_select_augment = on_select_augment
        self._on_remove_augment = on_remove_augment
        self._on_replace_last_augment = on_replace_last_augment
        self._on_resolve_transmute = on_resolve_transmute
        self._on_dismiss_transmute = on_dismiss_transmute
        self._on_call_coach = on_call_coach
        self._on_switch_dataset = on_switch_dataset
        self._on_manual_hero = on_manual_hero
        self._on_pos_changed = on_pos_changed
        self._on_size_changed = on_size_changed
        self._on_scale_changed = on_scale_changed
        self._on_closed_by_user = on_closed_by_user
        self._on_switch_tier = on_switch_tier
        self._on_recharge_click = on_recharge_click
        self._on_login_click = on_login_click
        self._on_check_update = on_check_update
        self._on_core_action = on_core_action
        self._on_opening_brief_seal_dismissed = on_opening_brief_seal_dismissed
        self._lifecycle: CoreLifecycleSnapshot | None = None
        self._peephole_hint: str = ""
        self._is_player_dead: bool = False
        self._match_game_time_s: float = 0.0
        self._opening_brief_seal_dismissed: bool = False
        self._opening_brief_seal_dismissed_count: int = 0
        self._pending_auto_unseal: bool = False
        self._tier_nudge_timer = QTimer(self)
        self._tier_nudge_timer.setSingleShot(True)
        _init_tier = (initial_tier or "a").strip().lower()
        self._coach_tier = _init_tier if _init_tier in ("a", "b", "auto") else "a"
        self._match_mode_badge_text: str = ""
        self._ai_cutoff = False
        self._cutoff_code = ""
        self._cutoff_policy = "ai_only"
        self._cutoff_reason = ""
        self._update_available = False
        self._coach_loading = False
        self._destiny_has_content = False
        self._balance_credits: float | None = None
        self._pending_transmute: PendingTransmute | None = None
        self._y_ratio = y_ratio
        self._target_screen: QScreen | None = None
        self._custom_pos: QPoint | None = (
            QPoint(int(initial_pos[0]), int(initial_pos[1]))
            if (initial_pos and len(initial_pos) == 2)
            else None
        )
        if initial_size and len(initial_size) >= 1 and int(initial_size[0]) > 0:
            self._expanded_w: int = max(self.MIN_EXPANDED_W, min(self.MAX_EXPANDED_W, int(initial_size[0])))
            self._has_custom_w: bool = True
        elif initial_scale is not None and float(initial_scale) > 0:
            sc = max(0.85, min(2.20, float(initial_scale)))
            self._expanded_w = max(self.MIN_EXPANDED_W, min(self.MAX_EXPANDED_W, int(round(self.EXPANDED_W * sc))))
            self._has_custom_w = True
        else:
            self._expanded_w = self.EXPANDED_W
            self._has_custom_w = False
        self._custom_h: int | None = (
            max(self.EXPANDED_MIN_H, int(initial_size[1]))
            if (initial_size and len(initial_size) >= 2 and int(initial_size[1]) > 0)
            else None
        )
        self._ui_scale: float = 1.0
        self._applied_ui_scale: float = 0.0
        self._coach_status_color: str = "#A09B8C"
        self._rm_hdr_color: str = "#C8AA6E"
        self._coach_balance_color: str = "#FDE68A"
        self._cutoff_banner_color: str = "#E2D9C5"
        self._cost_badge_state: tuple[int, bool, int] | None = None
        self._last_layout_bucket: int = self._width_bucket(self._expanded_w)
        self._last_rendered_w: int = self._expanded_w
        self._drag_offset: QPoint | None = None
        self._anchor_right: bool = self._infer_initial_anchor_right(self._custom_pos)
        self._expanded = False
        self._coach_online = False
        self._coach_online_label = ""
        self._coach_ready_hint = ""
        self._coach_pending = False
        self._coach_pending_label = ""
        self._coach_advice: CoachAdvicePayload | None = None
        self._opening_brief_data: dict[str, str] = {}
        self._opening_brief_dismissed: bool = False
        self._advice_gold_snapshot: float | None = None
        self._advice_items_snapshot: tuple[str, ...] = ()
        self._advice_received_mono: float = 0.0
        self._picker_active = False

        self._hero_id: str | None = None
        self._selected_augments: list[str] = []
        self._recent_offered: list[AugmentInfo] = []
        self._recent_auto_committed_id: str = ""
        self._my_items: tuple[str, ...] = ()
        self._my_gold: float = 0.0
        self._threat: MatchupThreatProfile | None = None
        self._dynamic_recs: list[DynamicItemRec] = []
        self._next_plan: NextBuyPlan | None = None
        self._committed_plan: list[str] = []
        self._locked_next_item: str = ""
        self._overhaul_reason: str = ""
        self._store_ref: DataStore | None = None
        self._warning_text: str = ""
        self._datasets: list[DatasetDescriptor] = []
        self._updating_combos = False

        # Non-blocking async item icon cache
        self._icon_invoker = UiBridgeInvoker()
        self._pixmap_cache: dict[tuple[str, int], QPixmap] = {}
        self._fetching_icons: set[str] = set()
        self._icon_fetch_attempted: set[str] = set()
        self._is_closed: bool = False
        self._last_roadmap_sig: tuple[tuple[str, str, str], ...] = ()
        self._lobby_scout: LobbyScoutSnapshot | None = None
        self._radar_dismissed: bool = False
        self._radar_detail_expanded: bool = False
        self._last_radar_sig: tuple | None = None
        self._last_radar_detail_sig: LobbyScoutSnapshot | None = None
        self._last_pivot_banner_text: str = ""
        self._last_top_threat_sig: tuple[str, str, bool] | None = None

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

        # === Collapsed Live Mini-HUD Pill (Next Item Icon + Gold Readiness Ring + 4 Rarity Dots) ===
        self.pill = QFrame()
        self.pill.setObjectName("PanelRoot")
        self.pill.setFixedWidth(self.COLLAPSED_W)
        self.pill.setMinimumHeight(self.COLLAPSED_MIN_H)
        self.pill.setToolTip("双击或点「展」展开 · 按住拖动位置 · 点 × 隐藏 · 右键更多选项")
        pill_lay = QVBoxLayout(self.pill)
        pill_lay.setContentsMargins(4, 4, 4, 6)
        pill_lay.setSpacing(3)

        self.pill_close_btn = QPushButton("×")
        self.pill_close_btn.setFixedSize(20, 16)
        self.pill_close_btn.setToolTip("隐藏悬浮小窗")
        self.pill_close_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 13px; color: #A09B8C; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 40); }"
        )
        self.pill_close_btn.clicked.connect(self._close_by_user)

        self.pill_item_ring = HextechGoldRingIcon(size=36)
        self.pill_item_ring.setToolTip("下一件目标装备与金币进度（绿环=可直买）")

        self.pill_gold_lbl = QLabel("待机")
        self.pill_gold_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pill_gold_lbl.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-size: 9px; font-weight: bold;")

        self.pill_hero_lbl = QLabel("海")
        self.pill_hero_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pill_hero_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-weight: bold; font-size: 11px;")

        # 4 miniature rarity-colored augment progress dots
        dots_wrap = QWidget()
        dots_wrap.setStyleSheet("background: transparent; border: none;")
        dots_lay = QHBoxLayout(dots_wrap)
        dots_lay.setContentsMargins(2, 0, 2, 0)
        dots_lay.setSpacing(3)
        self._pill_aug_dots: list[QFrame] = []
        for _ in range(4):
            dot = QFrame()
            dot.setFixedSize(6, 6)
            dot.setStyleSheet("background-color: #334155; border: none; border-radius: 3px;")
            dots_lay.addWidget(dot)
            self._pill_aug_dots.append(dot)

        # Keep pill_prog_lbl hidden for test/back-compat readers
        self.pill_prog_lbl = QLabel("0/4")
        self.pill_prog_lbl.hide()

        self.pill_coach_dot = QLabel("●")
        self.pill_coach_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pill_coach_dot.setStyleSheet("border: none; background: transparent; color: #5B5A56; font-size: 9px;")
        self.pill_expand_btn = HextechActionButton("展", variant="cyan", height=24)
        self.pill_expand_btn.setFixedWidth(36)
        self.pill_expand_btn.setMinimumWidth(36)
        self.pill_expand_btn.clicked.connect(lambda: self.set_expanded(True))

        pill_lay.addWidget(self.pill_close_btn, alignment=Qt.AlignmentFlag.AlignRight)
        pill_lay.addWidget(self.pill_item_ring, alignment=Qt.AlignmentFlag.AlignCenter)
        pill_lay.addWidget(self.pill_gold_lbl)
        pill_lay.addWidget(self.pill_hero_lbl)
        pill_lay.addWidget(dots_wrap, alignment=Qt.AlignmentFlag.AlignCenter)
        pill_lay.addWidget(self.pill_coach_dot)
        pill_lay.addWidget(self.pill_expand_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        root_lay.addWidget(self.pill)

        # === Expanded Adaptive Visual Decision HUD ===
        self.panel = QFrame()
        self.panel.setObjectName("PanelRoot")
        self.panel.setFixedWidth(self.EXPANDED_W)
        panel_lay = QVBoxLayout(self.panel)
        panel_lay.setContentsMargins(10, 9, 10, 9)
        panel_lay.setSpacing(6)
        self._panel_lay = panel_lay

        # 1. Compact Header
        hdr = QHBoxLayout()
        hdr.setContentsMargins(6, 0, 0, 0)
        hdr.setSpacing(5)
        self._hdr_lay = hdr
        self.hdr_icon = QLabel()
        self.hdr_icon.setPixmap(load_app_pixmap(22))
        self.hdr_icon.setFixedSize(22, 22)
        self.hero_lbl = QLabel("未锁定英雄")
        self.hero_lbl.setFont(display_font(11, bold=True, letter_spacing=0.4))
        self.hero_lbl.setStyleSheet("color: #F0E6D2;")
        self.gold_lbl = QLabel("")
        self.gold_lbl.setFont(latin_serif_font(10, bold=True, letter_spacing=0.4))
        self.gold_lbl.setStyleSheet("color: #FBBF24;")
        self.mode_badge_lbl = QLabel("")
        self.mode_badge_lbl.setStyleSheet(
            "background-color: rgba(10, 200, 185, 36); border: 1px solid #0AC8B9; "
            "border-radius: 2px; padding: 0px 4px; color: #A5F3FC; font-size: 9px; font-weight: bold;"
        )
        self.mode_badge_lbl.setToolTip("当前对局已自动切换至对应大数据胜率表")
        self.mode_badge_lbl.hide()
        self.coach_btn = HextechActionButton("F6", variant="cyan", pointed_right=True, height=24)
        self.coach_btn.setMinimumWidth(46)
        self.coach_btn.setToolTip("立即呼叫 AI 参谋刷新 (F6)")
        self.coach_btn.clicked.connect(self._handle_call_coach)
        self.collapse_btn = HextechActionButton("收起", variant="gold", height=24)
        self.collapse_btn.setMinimumWidth(40)
        self.collapse_btn.clicked.connect(lambda: self.set_expanded(False))
        self.settings_menu_btn = QPushButton("⚙")
        self.settings_menu_btn.setFixedSize(20, 20)
        self.settings_menu_btn.setToolTip("展开/收起数据集与手动切英雄栏")
        self.settings_menu_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 12px; color: #C8AA6E; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); }"
        )
        self.settings_menu_btn.clicked.connect(self._toggle_tools_row)
        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setToolTip("隐藏悬浮窗（按 F3 重开）")
        self.close_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 14px; color: #A09B8C; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); }"
        )
        self.close_btn.clicked.connect(self._close_by_user)

        hdr.addWidget(self.hdr_icon)
        hdr.addWidget(self.hero_lbl)
        hdr.addWidget(self.gold_lbl)
        hdr.addWidget(self.mode_badge_lbl)
        hdr.addStretch(1)
        hdr.addWidget(self.coach_btn)
        hdr.addWidget(self.collapse_btn)
        hdr.addWidget(self.settings_menu_btn)
        hdr.addWidget(self.close_btn)
        panel_lay.addLayout(hdr)

        # Scrollable content host so high-DPI / stress states NEVER clip bottom controls
        self.panel_scroll = QScrollArea()
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.panel_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.panel_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.panel_scroll.viewport().setStyleSheet("background: transparent;")
        self.panel_body = QWidget()
        self.panel_body.setObjectName("DrawerScrollBody")
        self.panel_body.setStyleSheet("QWidget#DrawerScrollBody { background: transparent; border: none; }")
        body_lay = QVBoxLayout(self.panel_body)
        body_lay.setContentsMargins(0, 0, 3, 0)
        body_lay.setSpacing(6)
        self._body_lay = body_lay
        self.panel_scroll.setWidget(self.panel_body)
        panel_lay.addWidget(self.panel_scroll, 1)

        self.warn_lbl = QLabel("")
        self.warn_lbl.setWordWrap(True)
        self.warn_lbl.setStyleSheet(
            "background-color: rgba(127, 29, 29, 220); color: #FECACA; "
            "border: 1px solid #DC2626; border-radius: 2px; padding: 3px 6px; font-size: 10px;"
        )
        self.warn_lbl.hide()
        body_lay.addWidget(self.warn_lbl)

        # 1.25. ZERO-NEW-WINDOW COLLAPSIBLE 10-PLAYER LOBBY RADAR CARD (auto-collapses into top_threat_lbl at 90s)
        self.radar_card = QFrame()
        self.radar_card.setObjectName("LobbyRadarCard")
        self.radar_card.setStyleSheet(
            "QFrame#LobbyRadarCard { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(4, 18, 32, 238), stop:1 rgba(9, 20, 40, 238)); "
            "border: 1px solid #785A28; border-left: 2px solid #0AC8B9; border-radius: 2px; }"
        )
        rd_lay = QVBoxLayout(self.radar_card)
        rd_lay.setContentsMargins(8, 5, 8, 5)
        rd_lay.setSpacing(3)
        self._rd_lay = rd_lay

        rd_hdr = QHBoxLayout()
        rd_hdr.setContentsMargins(0, 0, 0, 0)
        rd_hdr.setSpacing(4)
        self.radar_title_lbl = QLabel("◈ 十人雷达")
        self.radar_title_lbl.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: 10px;"
        )
        self.radar_premade_lbl = QLabel("")
        self.radar_premade_lbl.setStyleSheet(
            "background-color: rgba(127, 29, 29, 175); border: 1px solid rgba(248, 113, 113, 165); "
            "border-radius: 2px; padding: 0px 4px; color: #FECACA; font-size: 9px; font-weight: bold;"
        )
        self.radar_premade_lbl.hide()
        self.radar_detail_btn = QPushButton("详情")
        self.radar_detail_btn.setFixedHeight(17)
        self.radar_detail_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.radar_detail_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.radar_detail_btn.setToolTip("展开或收起 10 人战力明细")
        self.radar_detail_btn.setStyleSheet(
            "QPushButton { background-color: rgba(8, 30, 44, 210); border: 1px solid #463714; "
            "color: #0AC8B9; font-size: 9px; font-weight: bold; padding: 0px 6px; border-radius: 2px; }"
            "QPushButton:hover { border-color: #0AC8B9; color: #F0E6D2; }"
        )
        self.radar_detail_btn.clicked.connect(self._toggle_radar_detail)
        self.radar_dismiss_btn = QPushButton("×")
        self.radar_dismiss_btn.setFixedSize(16, 16)
        self.radar_dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.radar_dismiss_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.radar_dismiss_btn.setToolTip("折叠雷达卡（敌方大腿将保留在顶部威胁栏）")
        self.radar_dismiss_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 12px; color: #A09B8C; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); border-radius: 2px; }"
        )
        self.radar_dismiss_btn.clicked.connect(self._dismiss_lobby_radar)
        rd_hdr.addWidget(self.radar_title_lbl)
        rd_hdr.addWidget(self.radar_premade_lbl)
        rd_hdr.addStretch(1)
        rd_hdr.addWidget(self.radar_detail_btn)
        rd_hdr.addWidget(self.radar_dismiss_btn)
        rd_lay.addLayout(rd_hdr)

        self.radar_summary_lbl = QLabel("")
        self.radar_summary_lbl.setWordWrap(True)
        self.radar_summary_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.radar_summary_lbl.setStyleSheet(
            "border: none; background: transparent; color: #F0E6D2; font-size: 10px; font-weight: bold;"
        )
        rd_lay.addWidget(self.radar_summary_lbl)

        self.radar_detail_wrap = QWidget()
        self.radar_detail_wrap.setObjectName("LobbyRadarDetailWrap")
        self.radar_detail_wrap.setStyleSheet(
            "QWidget#LobbyRadarDetailWrap { background: transparent; border: none; }"
        )
        self.radar_detail_lay = QVBoxLayout(self.radar_detail_wrap)
        self.radar_detail_lay.setContentsMargins(0, 2, 0, 0)
        self.radar_detail_lay.setSpacing(2)
        self.radar_detail_wrap.hide()
        rd_lay.addWidget(self.radar_detail_wrap)

        self.radar_card.hide()
        body_lay.addWidget(self.radar_card)

        # 1.5. OPENING 10-PLAYER MATCH BRIEF CARD (Hextech cyan/gold collapsible card on game_start)
        self.opening_brief_card = HextechPulseFrame()
        self.opening_brief_card.setObjectName("OpeningBriefCard")
        self.opening_brief_card.setStyleSheet(
            "QFrame#OpeningBriefCard { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(6, 28, 42, 240), stop:1 rgba(9, 20, 40, 240)); "
            "border: 1px solid #0AC8B9; border-top: 2px solid #C8AA6E; border-radius: 2px; }"
        )
        ob_lay = QVBoxLayout(self.opening_brief_card)
        ob_lay.setContentsMargins(8, 6, 8, 6)
        ob_lay.setSpacing(4)
        self._ob_lay = ob_lay
        self._ob_tag_specs: list[tuple[QLabel, str, str]] = []

        ob_hdr = QHBoxLayout()
        ob_hdr.setContentsMargins(0, 0, 0, 0)
        ob_hdr.setSpacing(4)
        self.opening_brief_title_lbl = QLabel("◈ 开局 10 人作战简报")
        self.opening_brief_title_lbl.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: 10px;"
        )
        self.opening_brief_dismiss_btn = QPushButton("×")
        self.opening_brief_dismiss_btn.setFixedSize(16, 16)
        self.opening_brief_dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.opening_brief_dismiss_btn.setToolTip("收起开局 10 人作战简报")
        self.opening_brief_dismiss_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 12px; color: #A09B8C; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); border-radius: 2px; }"
        )
        self.opening_brief_dismiss_btn.clicked.connect(self.dismiss_opening_brief)
        ob_hdr.addWidget(self.opening_brief_title_lbl)
        ob_hdr.addStretch(1)
        ob_hdr.addWidget(self.opening_brief_dismiss_btn)
        ob_lay.addLayout(ob_hdr)

        def _make_brief_row(tag_text: str, tag_color: str, tag_bg: str, val_color: str) -> tuple[QWidget, QLabel]:
            row_w = QWidget()
            row_w.setStyleSheet("background: transparent; border: none;")
            rl = QHBoxLayout(row_w)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            tag_lbl = QLabel(tag_text)
            tag_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tag_lbl.setFixedWidth(58)
            tag_lbl.setStyleSheet(
                f"background-color: {tag_bg}; border: 1px solid {tag_color}; border-radius: 2px; "
                f"padding: 1px 3px; color: {tag_color}; font-size: 9px; font-weight: bold;"
            )
            self._ob_tag_specs.append((tag_lbl, tag_color, tag_bg))
            val_lbl = QLabel("")
            val_lbl.setWordWrap(True)
            val_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            val_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {val_color}; font-size: 10px;"
            )
            rl.addWidget(tag_lbl, 0, Qt.AlignmentFlag.AlignTop)
            rl.addWidget(val_lbl, 1)
            ob_lay.addWidget(row_w)
            return row_w, val_lbl

        self.opening_brief_threat_row, self.opening_brief_threat_lbl = _make_brief_row(
            "敌方威胁", "#F0E6D2", "rgba(200, 155, 60, 42)", "#F0E6D2"
        )
        self.opening_brief_augment_row, self.opening_brief_augment_lbl = _make_brief_row(
            "海克斯目标", "#0AC8B9", "rgba(10, 200, 185, 32)", "#E2E8F0"
        )
        self.opening_brief_buy_row, self.opening_brief_buy_lbl = _make_brief_row(
            "出门对策", "#C8AA6E", "rgba(200, 170, 110, 32)", "#FDE68A"
        )
        self.opening_brief_card.hide()
        body_lay.addWidget(self.opening_brief_card)

        # 1.6. COMPACT OPENING BRIEF SEAL PREVIEW SLOT (shown only when drawer is manually opened in S0/S2/S3, never auto-expands)
        self.opening_brief_seal_wrap = HextechPulseFrame()
        self.opening_brief_seal_wrap.setObjectName("OpeningBriefSealWrap")
        self.opening_brief_seal_wrap.setFixedHeight(54)
        self.opening_brief_seal_wrap.setStyleSheet(
            "QFrame#OpeningBriefSealWrap { background-color: rgba(5, 16, 28, 224); "
            "border: 1px solid #463714; border-radius: 2px; }"
        )
        obsw_lay = QVBoxLayout(self.opening_brief_seal_wrap)
        obsw_lay.setContentsMargins(8, 4, 6, 4)
        obsw_lay.setSpacing(2)
        obsw_top = QHBoxLayout()
        obsw_top.setContentsMargins(0, 0, 0, 0)
        obsw_skel = QLabel("[敌方威胁]  [海克斯目标]  [出门对策]")
        obsw_skel.setStyleSheet("border: none; background: transparent; color: rgba(200, 170, 110, 75); font-size: 9px;")
        self._obsw_skel = obsw_skel
        self.opening_brief_seal_close_btn = QPushButton("×")
        self.opening_brief_seal_close_btn.setFixedSize(16, 16)
        self.opening_brief_seal_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.opening_brief_seal_close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.opening_brief_seal_close_btn.setToolTip("本局收起开局定盘预览（连续 3 局关闭将自动折叠）")
        self.opening_brief_seal_close_btn.setStyleSheet(
            "QPushButton { padding: 0px; font-size: 12px; color: #A09B8C; background: transparent; border: none; }"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); border-radius: 2px; }"
        )
        self.opening_brief_seal_close_btn.clicked.connect(self._dismiss_opening_brief_seal)
        obsw_top.addWidget(obsw_skel, 1)
        obsw_top.addWidget(self.opening_brief_seal_close_btn)
        obsw_lay.addLayout(obsw_top)
        obsw_lay.addStretch(1)

        self.opening_brief_seal = HextechSealOverlay(
            self.opening_brief_seal_wrap,
            seal_id="opening_brief",
            title="开局 10 人作战简报",
            subtitle="敌方威胁 · 海克斯目标 · 出门对策待唤醒",
            compact=True,
            allow_idle_ambient=False,
        )
        self.opening_brief_seal.unseal_settled.connect(
            lambda _s: self.opening_brief_seal_wrap.trigger_pulse("#0AC8B9", 360)
        )
        self.opening_brief_seal.seal_clicked.connect(self._on_drawer_seal_clicked)
        self.opening_brief_seal_close_btn.raise_()
        self.opening_brief_seal_wrap.hide()
        body_lay.addWidget(self.opening_brief_seal_wrap)

        # 2. HERO NEXT-BUY VISUAL CARD (44x44 Icon + Decisive Name + Real-Time Buy-Now Strip + Pulse Feedback)
        self.next_buy_card = HextechPulseFrame()
        self.next_buy_card.setStyleSheet(
            "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(8, 38, 50, 242), stop:1 rgba(10, 20, 34, 242)); "
            "border: 1px solid #0AC8B9; border-left: 3px solid #F0E6D2; border-radius: 2px; }"
        )
        nb_lay = QVBoxLayout(self.next_buy_card)
        nb_lay.setContentsMargins(9, 7, 9, 7)
        nb_lay.setSpacing(6)
        self._nb_lay = nb_lay

        hero_row = QHBoxLayout()
        hero_row.setSpacing(9)
        self._hero_row = hero_row
        self.next_icon_lbl = QLabel("?")
        self.next_icon_lbl.setFixedSize(44, 44)
        self.next_icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_icon_lbl.setStyleSheet(
            "background-color: #091428; border: 2px solid #C89B3C; border-radius: 2px; "
            "color: #C8AA6E; font-weight: bold; font-size: 16px;"
        )
        hero_row.addWidget(self.next_icon_lbl)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setSpacing(5)
        self.next_item_title_lbl = QLabel("▶ 等待锁定英雄")
        self.next_item_title_lbl.setFont(display_font(13, bold=True, letter_spacing=0.4))
        self.next_item_title_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
        self.next_cost_badge = QLabel("")
        self.next_cost_badge.setStyleSheet(
            "background-color: rgba(200, 155, 60, 45); border: 1px solid #C89B3C; "
            "border-radius: 2px; padding: 1px 5px; color: #FDE68A; font-size: 10px; font-weight: bold;"
        )
        self.next_cost_badge.hide()
        title_row.addWidget(self.next_item_title_lbl)
        title_row.addWidget(self.next_cost_badge)
        title_row.addStretch(1)
        info_col.addLayout(title_row)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(6)
        self.next_reason_lbl = QLabel("进局自动计算唯一优先装备")
        self.next_reason_lbl.setWordWrap(True)
        self.next_reason_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.next_reason_lbl.setStyleSheet("border: none; background: transparent; color: #CBD5E1; font-size: 11px;")
        self.next_source_lbl = QLabel("本地")
        self.next_source_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 10px;")
        sub_row.addWidget(self.next_reason_lbl, 1)
        sub_row.addWidget(self.next_source_lbl)
        info_col.addLayout(sub_row)

        hero_row.addLayout(info_col, 1)
        nb_lay.addLayout(hero_row)

        # Instant Gold Shopping Action Strip (ALWAYS 1 line, synced at 0ms to live gold!)
        self.buy_now_lbl = QLabel("◇ 进局后自动计算当前金币立刻买什么件")
        self.buy_now_lbl.setWordWrap(True)
        self.buy_now_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.buy_now_lbl.setStyleSheet(
            "background-color: rgba(50, 35, 12, 230); border: 1px solid #C89B3C; "
            "border-radius: 2px; padding: 5px 8px; color: #FDE68A; font-size: 12px; font-weight: bold;"
        )
        nb_lay.addWidget(self.buy_now_lbl)

        # Compact tactical micro-bar: counter Tier-2 boots tag + top enemy threat KDA/spells
        self.tactical_micro_wrap = QWidget()
        self.tactical_micro_wrap.setObjectName("NextMicroWrap")
        self.tactical_micro_wrap.setStyleSheet("QWidget#NextMicroWrap { background: transparent; border: none; }")
        tm_lay = QHBoxLayout(self.tactical_micro_wrap)
        tm_lay.setContentsMargins(0, 0, 0, 0)
        tm_lay.setSpacing(6)
        self.boots_hint_lbl = QLabel("")
        self.boots_hint_lbl.setStyleSheet(
            "background-color: rgba(10, 200, 185, 28); border: 1px solid rgba(10, 200, 185, 130); "
            "border-radius: 2px; padding: 1px 5px; color: #A5F3FC; font-size: 9px; font-weight: bold;"
        )
        self.boots_hint_lbl.hide()
        self.top_threat_lbl = QLabel("")
        self.top_threat_lbl.setStyleSheet(
            "background-color: rgba(127, 29, 29, 175); border: 1px solid rgba(248, 113, 113, 165); "
            "border-radius: 2px; padding: 1px 5px; color: #FECACA; font-size: 9px; font-weight: bold;"
        )
        self.top_threat_lbl.hide()
        tm_lay.addWidget(self.boots_hint_lbl)
        tm_lay.addStretch(1)
        tm_lay.addWidget(self.top_threat_lbl)
        self.tactical_micro_wrap.hide()
        nb_lay.addWidget(self.tactical_micro_wrap)

        body_lay.addWidget(self.next_buy_card)

        # 3. VISUAL ICON BUILD PATH STRIP (Real 28x28 icons + 3-4 char short labels)
        self.roadmap_card = QFrame()
        self.roadmap_card.setObjectName("SubCard")
        rm_lay = QVBoxLayout(self.roadmap_card)
        rm_lay.setContentsMargins(8, 5, 8, 5)
        rm_lay.setSpacing(4)
        self._rm_lay = rm_lay

        rm_top = QHBoxLayout()
        self.rm_hdr_lbl = QLabel("◈ 出装路线")
        self.rm_hdr_lbl.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: 10px;")
        self.backup_item_lbl = QLabel("")
        self.backup_item_lbl.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 10px;")
        rm_top.addWidget(self.rm_hdr_lbl)
        rm_top.addStretch(1)
        rm_top.addWidget(self.backup_item_lbl)
        rm_lay.addLayout(rm_top)

        self.pivot_banner_lbl = QLabel("")
        self.pivot_banner_lbl.setWordWrap(True)
        self.pivot_banner_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.pivot_banner_lbl.setStyleSheet(
            "background-color: rgba(8, 42, 54, 228); border: 1px solid #0AC8B9; "
            "border-left: 2px solid #C8AA6E; border-radius: 2px; padding: 2px 6px; "
            "color: #A5F3FC; font-size: 10px; font-weight: bold;"
        )
        self.pivot_banner_lbl.hide()
        rm_lay.addWidget(self.pivot_banner_lbl)

        self.roadmap_icons_wrap = QWidget()
        self.roadmap_icons_wrap.setObjectName("RoadmapIconsWrap")
        self.roadmap_icons_wrap.setStyleSheet("QWidget#RoadmapIconsWrap { background: transparent; border: none; }")
        self.roadmap_icons_lay = QHBoxLayout(self.roadmap_icons_wrap)
        self.roadmap_icons_lay.setContentsMargins(0, 0, 0, 0)
        self.roadmap_icons_lay.setSpacing(3)
        rm_lay.addWidget(self.roadmap_icons_wrap)

        # Hidden text mirror used for quick inspection / tests without cluttering UI
        self.roadmap_body_lbl = QLabel("")
        self.roadmap_body_lbl.hide()
        body_lay.addWidget(self.roadmap_card)

        # 4. DESTINY'S WEAPON (only visible when 3081 owned)
        self.destiny_card = QFrame()
        self.destiny_card.setObjectName("DestinyCard")
        dest_lay = QHBoxLayout(self.destiny_card)
        dest_lay.setContentsMargins(8, 4, 8, 4)
        dest_lay.setSpacing(6)
        self._dest_lay = dest_lay
        dest_title = QLabel("命运绑定:")
        dest_title.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-weight: bold; font-size: 10px;")
        self._dest_title_lbl = dest_title
        self.destiny_body_lbl = QLabel("")
        self.destiny_body_lbl.setStyleSheet("border: none; background: transparent; color: #FDE68A; font-size: 10px;")
        dest_lay.addWidget(dest_title)
        dest_lay.addWidget(self.destiny_body_lbl, 1)
        self.destiny_card.hide()
        body_lay.addWidget(self.destiny_card)

        # 5. ULTRA-CONCISE 1-2 LINE COACH / TACTICAL BAR (with 60fps Shimmer Bar while thinking + Pulse on arrival!)
        self.coach_card = HextechPulseFrame()
        self.coach_card.setObjectName("CoachCard")
        self.coach_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        coach_lay = QVBoxLayout(self.coach_card)
        coach_lay.setContentsMargins(8, 5, 8, 5)
        coach_lay.setSpacing(3)
        self._coach_lay = coach_lay

        self.coach_shimmer = HextechShimmerBar(height=3, accent="#0AC8B9")
        self.coach_shimmer.hide()
        coach_lay.addWidget(self.coach_shimmer)

        coach_top = QHBoxLayout()
        coach_top.setSpacing(4)
        self.coach_status_lbl = QLabel("● 云端参谋待登录" if self._commercial else "● 参谋待接入")
        self.coach_status_lbl.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-weight: bold; font-size: 10px;")
        self.threat_lbl = QLabel("敌情监测中")
        self.threat_lbl.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 10px;")
        self.coach_time_lbl = QLabel("")
        self.coach_time_lbl.setStyleSheet("border: none; background: transparent; color: #64748B; font-size: 10px;")
        coach_top.addWidget(self.coach_status_lbl)
        coach_top.addStretch(1)
        coach_top.addWidget(self.threat_lbl)
        coach_top.addWidget(self.coach_time_lbl)
        coach_lay.addLayout(coach_top)

        # Commercial compact Auto/A/B tier segmented pill + live credit badge row (prevents crowding threat_lbl)
        self.tier_toggle_wrap = QWidget()
        self.tier_toggle_wrap.setObjectName("TierToggleWrap")
        self.tier_toggle_wrap.setStyleSheet("QWidget#TierToggleWrap { background: transparent; border: none; }")
        tt_lay = QHBoxLayout(self.tier_toggle_wrap)
        tt_lay.setContentsMargins(0, 0, 0, 0)
        tt_lay.setSpacing(4)
        self.tier_auto_pill = QPushButton("智能")
        self.tier_auto_pill.setToolTip("智能混合：日常自动 A 档省积分，关键变阵/胶着局/F6 自动升 B 档")
        self.tier_a_pill = QPushButton("A 经济")
        self.tier_b_pill = QPushButton("B 旗舰")
        for tb in (self.tier_auto_pill, self.tier_a_pill, self.tier_b_pill):
            tb.setFixedHeight(18)
            tb.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tier_auto_pill.clicked.connect(lambda: self._handle_tier_switch("auto"))
        self.tier_a_pill.clicked.connect(lambda: self._handle_tier_switch("a"))
        self.tier_b_pill.clicked.connect(lambda: self._handle_tier_switch("b"))
        self.coach_balance_lbl = QLabel("")
        self.coach_balance_lbl.setStyleSheet("border: none; background: transparent; color: #FDE68A; font-size: 10px; font-weight: bold;")
        self.coach_balance_lbl.hide()
        tt_lay.addWidget(self.tier_auto_pill)
        tt_lay.addWidget(self.tier_a_pill)
        tt_lay.addWidget(self.tier_b_pill)
        tt_lay.addStretch(1)
        tt_lay.addWidget(self.coach_balance_lbl)
        self.tier_toggle_wrap.setVisible(self._commercial)
        coach_lay.addWidget(self.tier_toggle_wrap)

        # Gentle 2.2s feedback label when clicking tier switcher while dormant/unauthenticated
        self.tier_nudge_lbl = QLabel("")
        self.tier_nudge_lbl.setWordWrap(True)
        self.tier_nudge_lbl.setStyleSheet(
            "border: none; background: transparent; color: #C8AA6E; font-size: 9px;"
        )
        self.tier_nudge_lbl.hide()
        self._tier_nudge_timer.timeout.connect(self.tier_nudge_lbl.hide)
        coach_lay.addWidget(self.tier_nudge_lbl)

        self.coach_content_lbl = QLabel(
            "云端 AI 参谋就绪 · 选牌 / 阵亡 / F6 自动推演"
            if self._commercial
            else "可在「AI 参谋」页填入 Gemini API Key 独立推演"
        )
        self.coach_content_lbl.setWordWrap(True)
        self.coach_content_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.coach_content_lbl.setStyleSheet("border: none; background: transparent; color: #E2E8F0; font-size: 11px;")
        coach_lay.addWidget(self.coach_content_lbl)

        # Status / cutoff container: hosts either Hextech Weathered Seal (dormant S0/S2/S3) or hard gate bar
        self.cutoff_banner = QFrame()
        self.cutoff_banner.setStyleSheet(
            "QFrame { background-color: transparent; border: none; }"
        )
        cb_root_lay = QVBoxLayout(self.cutoff_banner)
        cb_root_lay.setContentsMargins(0, 0, 0, 0)
        cb_root_lay.setSpacing(4)

        # Compact Weathered Hextech Seal slot (52px horizontal strip: emblem left + live peephole + CTA right)
        self.coach_seal_slot = HextechPulseFrame()
        self.coach_seal_slot.setObjectName("CoachSealSlot")
        self.coach_seal_slot.setFixedHeight(52)
        self.coach_seal_slot.setStyleSheet(
            "QFrame#CoachSealSlot { background-color: rgba(4, 14, 24, 224); "
            "border: 1px solid #463714; border-radius: 2px; }"
        )
        css_lay = QVBoxLayout(self.coach_seal_slot)
        css_lay.setContentsMargins(8, 4, 8, 4)
        css_skel = QLabel("▶ 回城精准双散件 · 敌方核心反制 · 团战站位一句提醒")
        css_skel.setStyleSheet("border: none; background: transparent; color: rgba(200, 170, 110, 62); font-size: 9px;")
        self._css_skel = css_skel
        css_lay.addWidget(css_skel)
        self.coach_seal_overlay = HextechSealOverlay(
            self.coach_seal_slot,
            seal_id="drawer_coach",
            title="云端 AI 实时参谋",
            subtitle="本地 5ms 引擎持续护航 · 充能唤醒 10 人反制",
            compact=True,
            allow_idle_ambient=False,
        )
        self.coach_seal_overlay.unseal_settled.connect(lambda _s: self.coach_card.trigger_pulse("#0AC8B9", 460))
        self.coach_seal_overlay.seal_clicked.connect(self._on_drawer_seal_clicked)
        self.coach_seal_slot.hide()
        cb_root_lay.addWidget(self.coach_seal_slot)

        # Classic compact banner bar (visible for hard system gates / lock_all / mid-match advice preservation / legacy set_ai_cutoff)
        self.cutoff_gate_bar = QFrame()
        self.cutoff_gate_bar.setStyleSheet(
            "QFrame { background-color: rgba(6, 20, 34, 232); border: 1px solid #785A28; "
            "border-left: 2px solid #0AC8B9; border-radius: 2px; }"
        )
        cb_lay = QHBoxLayout(self.cutoff_gate_bar)
        cb_lay.setContentsMargins(6, 3, 6, 3)
        cb_lay.setSpacing(6)
        self.cutoff_banner_lbl = QLabel("本地 5ms 引擎持续护航 · 核心待充能")
        self.cutoff_banner_lbl.setWordWrap(True)
        self.cutoff_banner_lbl.setStyleSheet("border: none; background: transparent; color: #E2D9C5; font-size: 10px; font-weight: bold;")
        self.cutoff_action_btn = QPushButton("扫码充值")
        self.cutoff_action_btn.setFixedHeight(20)
        self.cutoff_action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cutoff_action_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cutoff_action_btn.setStyleSheet(
            "QPushButton { background-color: #0E3746; border: 1px solid #0AC8B9; color: #F0E6D2; "
            "font-size: 10px; font-weight: bold; padding: 0px 8px; border-radius: 2px; }"
            "QPushButton:hover { border-color: #5EEAD4; color: #FFFFFF; }"
        )
        self.cutoff_action_btn.clicked.connect(self._handle_cutoff_action)
        cb_lay.addWidget(self.cutoff_banner_lbl, 1)
        cb_lay.addWidget(self.cutoff_action_btn)
        cb_root_lay.addWidget(self.cutoff_gate_bar)

        # In-place non-activating inline preview panel (clicking in-game seal NEVER steals LOL keyboard focus!)
        self.inline_preview_box = QFrame()
        self.inline_preview_box.setStyleSheet(
            "QFrame { background-color: rgba(6, 20, 34, 238); border: 1px solid #785A28; "
            "border-left: 2px solid #0AC8B9; border-radius: 2px; }"
        )
        ipl = QVBoxLayout(self.inline_preview_box)
        ipl.setContentsMargins(7, 5, 7, 5)
        ipl.setSpacing(4)
        self.inline_preview_lbl = QLabel(
            "◈ 本地 5ms 出装与 3-4 卡识别永久免费不锁\n"
            "◈ 云端 AI 参谋：开局定盘 + 阵亡精准双散件 + 72 质变重构（5元打 15-25 局 · 永不过期）"
        )
        self.inline_preview_lbl.setWordWrap(True)
        self.inline_preview_lbl.setStyleSheet("border: none; background: transparent; color: #E2D9C5; font-size: 10px;")
        ip_btns = QHBoxLayout()
        ip_btns.setSpacing(6)
        self.inline_preview_cta_btn = QPushButton("打开详情 / 充能")
        self.inline_preview_cta_btn.setFixedHeight(20)
        self.inline_preview_cta_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.inline_preview_cta_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.inline_preview_cta_btn.setStyleSheet(
            "QPushButton { background-color: #0E3746; border: 1px solid #0AC8B9; color: #F0E6D2; "
            "font-size: 10px; font-weight: bold; padding: 0px 8px; border-radius: 2px; }"
            "QPushButton:hover { border-color: #7CF8EE; color: #FFFFFF; }"
        )
        self.inline_preview_cta_btn.clicked.connect(self._on_inline_preview_cta)
        ip_close_btn = QPushButton("收起")
        ip_close_btn.setFixedHeight(20)
        ip_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ip_close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ip_close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #463714; color: #A09B8C; "
            "font-size: 10px; padding: 0px 8px; border-radius: 2px; }"
            "QPushButton:hover { color: #F0E6D2; border-color: #C8AA6E; }"
        )
        ip_close_btn.clicked.connect(self.inline_preview_box.hide)
        self._ip_close_btn = ip_close_btn
        ip_btns.addWidget(self.inline_preview_cta_btn)
        ip_btns.addStretch(1)
        ip_btns.addWidget(ip_close_btn)
        ipl.addWidget(self.inline_preview_lbl)
        ipl.addLayout(ip_btns)
        self.inline_preview_box.hide()
        cb_root_lay.addWidget(self.inline_preview_box)

        self.cutoff_banner.hide()
        coach_lay.addWidget(self.cutoff_banner)
        self.set_coach_tier(self._coach_tier)
        body_lay.addWidget(self.coach_card)

        # 6. COMPACT AUGMENTS ROW (Inline Pills + Auto-Pick Pulse + 1-Click Swap + Collapsible '+' Add)
        self.aug_frame = HextechPulseFrame()
        self.aug_frame.setObjectName("SubCard")
        aug_lay = QVBoxLayout(self.aug_frame)
        aug_lay.setContentsMargins(8, 5, 8, 5)
        aug_lay.setSpacing(4)
        self._aug_lay = aug_lay

        aug_top = QHBoxLayout()
        aug_top.setSpacing(4)
        self.aug_hdr_lbl = QLabel("◈ 海克斯 (0/4)")
        self.aug_hdr_lbl.setStyleSheet("border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: 10px;")
        self.aug_toggle_add_btn = QPushButton("+")
        self.aug_toggle_add_btn.setFixedSize(20, 18)
        self.aug_toggle_add_btn.setToolTip("手动补录海克斯")
        self.aug_toggle_add_btn.setStyleSheet("padding: 0px; font-size: 12px;")
        self.aug_toggle_add_btn.clicked.connect(self._toggle_manual_aug_picker)
        aug_top.addWidget(self.aug_hdr_lbl)
        aug_top.addStretch(1)
        aug_top.addWidget(self.aug_toggle_add_btn)
        aug_lay.addLayout(aug_top)

        self.aug_chips_wrap = QWidget()
        self.aug_chips_wrap.setObjectName("AugChipsWrap")
        self.aug_chips_wrap.setStyleSheet("QWidget#AugChipsWrap { background: transparent; border: none; }")
        self.aug_chips_lay = QGridLayout(self.aug_chips_wrap)
        self.aug_chips_lay.setContentsMargins(0, 0, 0, 0)
        self.aug_chips_lay.setHorizontalSpacing(4)
        self.aug_chips_lay.setVerticalSpacing(4)
        aug_lay.addWidget(self.aug_chips_wrap)

        # 1-click quick-confirm OR swap bar for last 3 offered cards
        self.quick_confirm_wrap = QWidget()
        self.quick_confirm_wrap.setObjectName("QuickConfirmWrap")
        self.quick_confirm_wrap.setStyleSheet("QWidget#QuickConfirmWrap { background: transparent; border: none; }")
        self.quick_confirm_lay = QHBoxLayout(self.quick_confirm_wrap)
        self.quick_confirm_lay.setContentsMargins(0, 0, 0, 0)
        self.quick_confirm_lay.setSpacing(3)
        self.quick_confirm_wrap.hide()
        aug_lay.addWidget(self.quick_confirm_wrap)

        # Transmute secondary-recognition status + rarity-filtered fallback picker
        self.transmute_bar = QFrame()
        self.transmute_bar.setStyleSheet(
            "QFrame { background-color: rgba(45, 18, 62, 225); border: 1px solid #D946EF; border-radius: 2px; }"
        )
        tb_lay = QVBoxLayout(self.transmute_bar)
        tb_lay.setContentsMargins(6, 4, 6, 4)
        tb_lay.setSpacing(3)
        self._tb_lay = tb_lay
        tb_top = QHBoxLayout()
        tb_top.setSpacing(4)
        self.transmute_status_lbl = QLabel("⚡ 质变二次识别中 · 悬停新符文图标自动识别")
        self.transmute_status_lbl.setWordWrap(True)
        self.transmute_status_lbl.setStyleSheet(
            "border: none; background: transparent; color: #F5D0FE; font-size: 10px; font-weight: bold;"
        )
        tb_dismiss_btn = QPushButton("×")
        tb_dismiss_btn.setFixedSize(16, 16)
        tb_dismiss_btn.setToolTip("关闭质变待识别提示")
        tb_dismiss_btn.setStyleSheet(
            "padding: 0px; font-size: 11px; color: #E9D5FF; background: transparent; border: none;"
        )
        tb_dismiss_btn.clicked.connect(self._handle_dismiss_transmute)
        self._tb_dismiss_btn = tb_dismiss_btn
        tb_top.addWidget(self.transmute_status_lbl, 1)
        tb_top.addWidget(tb_dismiss_btn)
        tb_lay.addLayout(tb_top)

        tb_pick_row = QHBoxLayout()
        tb_pick_row.setSpacing(4)
        self.transmute_combo = QComboBox()
        self.transmute_combo.setEditable(True)
        self.transmute_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.transmute_combo.setFixedHeight(25)
        self.transmute_combo.setStyleSheet(
            "QComboBox { padding: 1px 6px; font-size: 11px; border: 1px solid #9333EA; }"
        )
        if self.transmute_combo.lineEdit():
            self.transmute_combo.lineEdit().setPlaceholderText("或手选质变结果...")
            self.transmute_combo.lineEdit().setStyleSheet("padding: 0px 2px; font-size: 11px;")
        tb_ok_btn = QPushButton("确认")
        tb_ok_btn.setFixedHeight(25)
        tb_ok_btn.setStyleSheet("padding: 0px 8px; font-size: 11px; border: 1px solid #D946EF; color: #F0E6D2;")
        tb_ok_btn.clicked.connect(self._handle_manual_resolve_transmute)
        self._tb_ok_btn = tb_ok_btn
        tb_pick_row.addWidget(self.transmute_combo, 1)
        tb_pick_row.addWidget(tb_ok_btn)
        tb_lay.addLayout(tb_pick_row)
        self.transmute_bar.hide()
        aug_lay.addWidget(self.transmute_bar)

        # Hidden-by-default manual dropdown
        self.aug_manual_row = QWidget()
        amr = QHBoxLayout(self.aug_manual_row)
        amr.setContentsMargins(0, 0, 0, 0)
        amr.setSpacing(4)
        self.aug_add_combo = QComboBox()
        self.aug_add_combo.setEditable(True)
        self.aug_add_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.aug_add_combo.setPlaceholderText("搜海克斯名...")
        aug_confirm_btn = QPushButton("添加")
        aug_confirm_btn.setFixedHeight(22)
        aug_confirm_btn.clicked.connect(self._handle_manual_add_augment)
        self._aug_confirm_btn = aug_confirm_btn
        amr.addWidget(self.aug_add_combo, 1)
        amr.addWidget(aug_confirm_btn)
        self.aug_manual_row.hide()
        aug_lay.addWidget(self.aug_manual_row)
        body_lay.addWidget(self.aug_frame)

        # 7. Collapsible Tools Row (Dataset + Manual Hero override, tucked behind header '⚙' to save 28px in-game!)
        self.tools_row = QWidget()
        self.tools_row.setObjectName("DrawerToolsRow")
        self.tools_row.setStyleSheet("QWidget#DrawerToolsRow { background: transparent; border: none; }")
        footer = QHBoxLayout(self.tools_row)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(5)
        self.dataset_combo = QComboBox()
        self.dataset_combo.setFixedHeight(22)
        self.dataset_combo.currentIndexChanged.connect(self._handle_dataset_changed)
        self.hero_combo = QComboBox()
        self.hero_combo.setEditable(True)
        self.hero_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if self.hero_combo.lineEdit():
            self.hero_combo.lineEdit().setPlaceholderText("切英雄")
        self.hero_combo.setFixedWidth(104)
        self.hero_combo.setFixedHeight(22)
        self.hero_combo.activated.connect(self._handle_manual_hero)
        footer.addWidget(self.dataset_combo, 1)
        footer.addWidget(self.hero_combo)
        self.tools_row.hide()
        body_lay.addWidget(self.tools_row)
        body_lay.addStretch(1)

        root_lay.addWidget(self.panel)
        self.panel.hide()

        self._edge_l = HextechEdgeHandle(
            self.panel, "l", show_notch=True, on_double_click=self.reset_default_size
        )
        self._edge_r = HextechEdgeHandle(self.panel, "r", show_notch=False)
        self._edge_t = HextechEdgeHandle(self.panel, "t", show_notch=False)
        self._edge_b = HextechEdgeHandle(self.panel, "b", show_notch=False)
        self._grip_tl = HextechCornerGrip(
            self.panel, "tl", on_double_click=self.reset_default_size, prominent=True
        )
        self._grip_bl = HextechCornerGrip(
            self.panel, "bl", on_double_click=self.reset_default_size, prominent=True
        )
        self._grip_br = HextechCornerGrip(
            self.panel, "br", on_double_click=self.reset_default_size, prominent=False
        )
        self._resize_ctrl = FramelessResizeController(
            self,
            min_w=self.MIN_EXPANDED_W,
            max_w=self.MAX_EXPANDED_W,
            min_h=self.EXPANDED_MIN_H,
            max_h=self.MAX_EXPANDED_H,
            can_resize=lambda: self._expanded and not self._is_full_locked,
            on_live_resize=self._on_live_resize,
            on_resize_finished=self._on_resize_finished,
            screen_provider=self._active_screen,
        )
        for handle in (
            self.panel,
            self.panel_scroll.viewport(),
            self._edge_l,
            self._edge_r,
            self._edge_t,
            self._edge_b,
            self._grip_tl,
            self._grip_bl,
            self._grip_br,
        ):
            self._resize_ctrl.attach(handle)

        self._slide_anim = QVariantAnimation(self)
        self._slide_anim.setDuration(170)
        self._slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide_anim.valueChanged.connect(self._apply_animated_width)
        self._slide_anim.finished.connect(self._on_slide_finished)
        self._apply_ui_scale(1.0, force=True)
        self._reposition_right_edge()

    @property
    def ui_scale(self) -> float:
        return self._ui_scale

    def _metrics(self) -> OverlayScaleMetrics:
        return OverlayScaleMetrics(self._ui_scale)

    def _fs(self, base_px: int | float, *, floor: int = 8) -> int:
        return self._metrics().font_px(base_px, floor=floor)

    def _px(self, base_px: int | float, *, floor: int = 1) -> int:
        return self._metrics().px(base_px, floor=floor)

    @classmethod
    def compute_default_expanded_width(
        cls,
        logical_screen_w: int,
        logical_screen_h: int | None = None,
        dpr: float = 1.0,
    ) -> int:
        auto_scale = compute_auto_overlay_scale(logical_screen_w, logical_screen_h, dpr)
        return max(cls.MIN_EXPANDED_W, min(cls.MAX_EXPANDED_W, int(round(cls.EXPANDED_W * auto_scale))))

    def _compute_scale_for_rect(self, w: int, h: int | None = None) -> float:
        return compute_overlay_scale(
            w,
            self.EXPANDED_W,
            height=h,
            min_comfort_height=self._MIN_COMFORT_H,
        )

    def _apply_ui_scale(self, scale: float, *, force: bool = False) -> bool:
        q_scale = round(max(0.85, min(2.20, float(scale))), 2)
        if not force and abs(q_scale - self._applied_ui_scale) < 0.03:
            return False
        self._ui_scale = q_scale
        self._applied_ui_scale = q_scale
        m = OverlayScaleMetrics(q_scale)

        self._panel_lay.setContentsMargins(m.px(10), m.px(9), m.px(10), m.px(9))
        self._panel_lay.setSpacing(m.px(6))
        self._hdr_lay.setContentsMargins(m.px(2), 0, 0, 0)
        self._hdr_lay.setSpacing(m.px(4))
        self._body_lay.setContentsMargins(0, 0, m.px(3), 0)
        self._body_lay.setSpacing(m.px(6))
        self._rd_lay.setContentsMargins(m.px(8), m.px(5), m.px(8), m.px(5))
        self._rd_lay.setSpacing(m.px(3))
        self._ob_lay.setContentsMargins(m.px(8), m.px(6), m.px(8), m.px(6))
        self._ob_lay.setSpacing(m.px(4))
        self._nb_lay.setContentsMargins(m.px(9), m.px(7), m.px(9), m.px(7))
        self._nb_lay.setSpacing(m.px(6))
        self._hero_row.setSpacing(m.px(9))
        self._rm_lay.setContentsMargins(m.px(8), m.px(5), m.px(8), m.px(5))
        self._rm_lay.setSpacing(m.px(4))
        self.roadmap_icons_lay.setSpacing(m.px(3))
        self._dest_lay.setContentsMargins(m.px(8), m.px(4), m.px(8), m.px(4))
        self._dest_lay.setSpacing(m.px(6))
        self._coach_lay.setContentsMargins(m.px(8), m.px(5), m.px(8), m.px(5))
        self._coach_lay.setSpacing(m.px(3))
        self._aug_lay.setContentsMargins(m.px(8), m.px(5), m.px(8), m.px(5))
        self._aug_lay.setSpacing(m.px(4))
        self.aug_chips_lay.setHorizontalSpacing(m.px(4))
        self.aug_chips_lay.setVerticalSpacing(m.px(4))
        self._tb_lay.setContentsMargins(m.px(6), m.px(4), m.px(6), m.px(4))
        self._tb_lay.setSpacing(m.px(3))

        sb_w = max(6, min(14, m.px(6)))
        self.panel_scroll.verticalScrollBar().setStyleSheet(
            f"QScrollBar:vertical {{ width: {sb_w}px; background: #010A13; border: none; }}"
            f"QScrollBar::handle:vertical {{ background: #785A28; border-radius: 2px; min-height: {m.px(24)}px; }}"
            "QScrollBar::handle:vertical:hover { background: #C8AA6E; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #010A13; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )

        # Header
        hdr_ico_sz = m.px(22, floor=18)
        self.hdr_icon.setFixedSize(hdr_ico_sz, hdr_ico_sz)
        self.hdr_icon.setPixmap(load_app_pixmap(hdr_ico_sz))
        self.hero_lbl.setFont(display_font(m.pt(11), bold=True, letter_spacing=0.3))
        self.hero_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #F0E6D2; font-size: {m.font_px(12)}px; font-weight: bold;"
        )
        self.gold_lbl.setFont(latin_serif_font(m.pt(10), bold=True, letter_spacing=0.3))
        self.gold_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #FBBF24; font-size: {m.font_px(11)}px; font-weight: bold;"
        )
        self.mode_badge_lbl.setStyleSheet(
            "background-color: rgba(10, 200, 185, 36); border: 1px solid #0AC8B9; "
            f"border-radius: 2px; padding: 0px {m.px(4)}px; color: #A5F3FC; "
            f"font-size: {m.font_px(9)}px; font-weight: bold;"
        )
        self.coach_btn.set_scaled_metrics(
            height=m.px(24, floor=20),
            min_width=m.px(42, floor=38),
            font_pt=m.pt(10),
        )
        btn_sq = m.px(20, floor=18)
        self.settings_menu_btn.setFixedSize(btn_sq, btn_sq)
        self.settings_menu_btn.setStyleSheet(
            f"QPushButton {{ padding: 0px; font-size: {m.font_px(12)}px; color: #C8AA6E; background: transparent; border: none; }}"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); }"
        )
        self.close_btn.setFixedSize(btn_sq, btn_sq)
        self.close_btn.setStyleSheet(
            f"QPushButton {{ padding: 0px; font-size: {m.font_px(14)}px; color: #A09B8C; background: transparent; border: none; }}"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); }"
        )

        # Warning & Lobby Radar
        self.warn_lbl.setStyleSheet(
            "background-color: rgba(127, 29, 29, 220); color: #FECACA; "
            f"border: 1px solid #DC2626; border-radius: 2px; padding: {m.px(3)}px {m.px(6)}px; "
            f"font-size: {m.font_px(10)}px;"
        )
        self.radar_title_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: {m.font_px(10)}px;"
        )
        self.radar_premade_lbl.setStyleSheet(
            "background-color: rgba(127, 29, 29, 175); border: 1px solid rgba(248, 113, 113, 165); "
            f"border-radius: 2px; padding: 0px {m.px(4)}px; color: #FECACA; font-size: {m.font_px(9)}px; font-weight: bold;"
        )
        self.radar_detail_btn.setFixedHeight(m.px(17, floor=16))
        self.radar_detail_btn.setStyleSheet(
            "QPushButton { background-color: rgba(8, 30, 44, 210); border: 1px solid #463714; "
            f"color: #0AC8B9; font-size: {m.font_px(9)}px; font-weight: bold; padding: 0px {m.px(6)}px; border-radius: 2px; }}"
            "QPushButton:hover { border-color: #0AC8B9; color: #F0E6D2; }"
        )
        sm_x = m.px(16, floor=14)
        self.radar_dismiss_btn.setFixedSize(sm_x, sm_x)
        self.radar_dismiss_btn.setStyleSheet(
            f"QPushButton {{ padding: 0px; font-size: {m.font_px(12)}px; color: #A09B8C; background: transparent; border: none; }}"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); border-radius: 2px; }"
        )
        self.radar_summary_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #F0E6D2; font-size: {m.font_px(10)}px; font-weight: bold;"
        )

        # Opening Brief & Seal Preview
        self.opening_brief_title_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: {m.font_px(10)}px;"
        )
        self.opening_brief_dismiss_btn.setFixedSize(sm_x, sm_x)
        self.opening_brief_dismiss_btn.setStyleSheet(
            f"QPushButton {{ padding: 0px; font-size: {m.font_px(12)}px; color: #A09B8C; background: transparent; border: none; }}"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); border-radius: 2px; }"
        )
        tag_w = m.px(58, floor=48)
        for tag_lbl, tag_color, tag_bg in self._ob_tag_specs:
            tag_lbl.setFixedWidth(tag_w)
            tag_lbl.setStyleSheet(
                f"background-color: {tag_bg}; border: 1px solid {tag_color}; border-radius: 2px; "
                f"padding: {m.px(1)}px {m.px(3)}px; color: {tag_color}; font-size: {m.font_px(9)}px; font-weight: bold;"
            )
        self.opening_brief_threat_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #F0E6D2; font-size: {m.font_px(10)}px;"
        )
        self.opening_brief_augment_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #E2E8F0; font-size: {m.font_px(10)}px;"
        )
        self.opening_brief_buy_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #FDE68A; font-size: {m.font_px(10)}px;"
        )
        self._obsw_skel.setStyleSheet(
            f"border: none; background: transparent; color: rgba(200, 170, 110, 75); font-size: {m.font_px(9)}px;"
        )
        self.opening_brief_seal_close_btn.setFixedSize(sm_x, sm_x)
        self.opening_brief_seal_close_btn.setStyleSheet(
            f"QPushButton {{ padding: 0px; font-size: {m.font_px(12)}px; color: #A09B8C; background: transparent; border: none; }}"
            "QPushButton:hover { color: #F0E6D2; background: rgba(200, 170, 110, 45); border-radius: 2px; }"
        )

        # Hero Next-Buy Card
        hero_ico_sz = m.px(44, floor=34)
        self.next_icon_lbl.setFixedSize(hero_ico_sz, hero_ico_sz)
        self.next_icon_lbl.setStyleSheet(
            "background-color: #091428; border: 2px solid #C89B3C; border-radius: 2px; "
            f"color: #C8AA6E; font-weight: bold; font-size: {m.font_px(16)}px;"
        )
        self.next_item_title_lbl.setFont(display_font(m.pt(13), bold=True, letter_spacing=0.4))
        self.next_item_title_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #F0E6D2; font-size: {m.font_px(15)}px; font-weight: bold;"
        )
        self.next_reason_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #CBD5E1; font-size: {m.font_px(11)}px;"
        )
        self.next_source_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #0AC8B9; font-size: {m.font_px(10)}px;"
        )
        self.buy_now_lbl.setStyleSheet(
            "background-color: rgba(50, 35, 12, 230); border: 1px solid #C89B3C; "
            f"border-radius: 2px; padding: {m.px(5)}px {m.px(8)}px; color: #FDE68A; "
            f"font-size: {m.font_px(12)}px; font-weight: bold;"
        )
        self.boots_hint_lbl.setStyleSheet(
            "background-color: rgba(10, 200, 185, 28); border: 1px solid rgba(10, 200, 185, 130); "
            f"border-radius: 2px; padding: {m.px(1)}px {m.px(5)}px; color: #A5F3FC; "
            f"font-size: {m.font_px(9)}px; font-weight: bold;"
        )
        self.top_threat_lbl.setStyleSheet(
            "background-color: rgba(127, 29, 29, 175); border: 1px solid rgba(248, 113, 113, 165); "
            f"border-radius: 2px; padding: {m.px(1)}px {m.px(5)}px; color: #FECACA; "
            f"font-size: {m.font_px(9)}px; font-weight: bold;"
        )
        if self._cost_badge_state is not None:
            self._set_cost_badge(*self._cost_badge_state)

        # Roadmap & Destiny
        self.rm_hdr_lbl.setStyleSheet(
            f"border: none; background: transparent; color: {self._rm_hdr_color}; "
            f"font-weight: bold; font-size: {m.font_px(10)}px;"
        )
        self.backup_item_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #A09B8C; font-size: {m.font_px(10)}px;"
        )
        self.pivot_banner_lbl.setStyleSheet(
            "background-color: rgba(8, 42, 54, 228); border: 1px solid #0AC8B9; "
            f"border-left: 2px solid #C8AA6E; border-radius: 2px; padding: {m.px(2)}px {m.px(6)}px; "
            f"color: #A5F3FC; font-size: {m.font_px(10)}px; font-weight: bold;"
        )
        self._dest_title_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #F0E6D2; font-weight: bold; font-size: {m.font_px(10)}px;"
        )
        self.destiny_body_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #FDE68A; font-size: {m.font_px(10)}px;"
        )

        # Coach / Tactical Bar
        self.coach_status_lbl.setStyleSheet(
            f"border: none; background: transparent; color: {self._coach_status_color}; "
            f"font-weight: bold; font-size: {m.font_px(10)}px;"
        )
        self.threat_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #0AC8B9; font-size: {m.font_px(10)}px;"
        )
        self.coach_time_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #64748B; font-size: {m.font_px(10)}px;"
        )
        tier_h = m.px(18, floor=16)
        for tb in (self.tier_auto_pill, self.tier_a_pill, self.tier_b_pill):
            tb.setFixedHeight(tier_h)
        self.set_coach_tier(self._coach_tier)
        self.coach_balance_lbl.setStyleSheet(
            f"border: none; background: transparent; color: {self._coach_balance_color}; "
            f"font-size: {m.font_px(10)}px; font-weight: bold;"
        )
        self.tier_nudge_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #C8AA6E; font-size: {m.font_px(9)}px;"
        )
        self.coach_content_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #E2E8F0; font-size: {m.font_px(11)}px;"
        )
        self.coach_seal_slot.setFixedHeight(m.px(52, floor=44))
        self._css_skel.setStyleSheet(
            f"border: none; background: transparent; color: rgba(200, 170, 110, 62); font-size: {m.font_px(9)}px;"
        )
        self.cutoff_banner_lbl.setStyleSheet(
            f"border: none; background: transparent; color: {self._cutoff_banner_color}; "
            f"font-size: {m.font_px(10)}px; font-weight: bold;"
        )
        act_h = m.px(20, floor=18)
        self.cutoff_action_btn.setFixedHeight(act_h)
        self.cutoff_action_btn.setStyleSheet(
            "QPushButton { background-color: #0E3746; border: 1px solid #0AC8B9; color: #F0E6D2; "
            f"font-size: {m.font_px(10)}px; font-weight: bold; padding: 0px {m.px(8)}px; border-radius: 2px; }}"
            "QPushButton:hover { border-color: #5EEAD4; color: #FFFFFF; }"
        )
        self.inline_preview_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #E2D9C5; font-size: {m.font_px(10)}px;"
        )
        self.inline_preview_cta_btn.setFixedHeight(act_h)
        self.inline_preview_cta_btn.setStyleSheet(
            "QPushButton { background-color: #0E3746; border: 1px solid #0AC8B9; color: #F0E6D2; "
            f"font-size: {m.font_px(10)}px; font-weight: bold; padding: 0px {m.px(8)}px; border-radius: 2px; }}"
            "QPushButton:hover { border-color: #7CF8EE; color: #FFFFFF; }"
        )
        self._ip_close_btn.setFixedHeight(act_h)
        self._ip_close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #463714; color: #A09B8C; "
            f"font-size: {m.font_px(10)}px; padding: 0px {m.px(8)}px; border-radius: 2px; }}"
            "QPushButton:hover { color: #F0E6D2; border-color: #C8AA6E; }"
        )

        # Augments, Transmute & Tools Row
        self.aug_hdr_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: {m.font_px(10)}px;"
        )
        self.aug_toggle_add_btn.setFixedSize(m.px(20, floor=18), m.px(18, floor=16))
        self.aug_toggle_add_btn.setStyleSheet(f"padding: 0px; font-size: {m.font_px(12)}px;")
        self.transmute_status_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #F5D0FE; font-size: {m.font_px(10)}px; font-weight: bold;"
        )
        self._tb_dismiss_btn.setFixedSize(sm_x, sm_x)
        self._tb_dismiss_btn.setStyleSheet(
            f"padding: 0px; font-size: {m.font_px(11)}px; color: #E9D5FF; background: transparent; border: none;"
        )
        tx_h = m.px(25, floor=22)
        self.transmute_combo.setFixedHeight(tx_h)
        self.transmute_combo.setStyleSheet(
            f"QComboBox {{ padding: 1px {m.px(6)}px; font-size: {m.font_px(11)}px; border: 1px solid #9333EA; }}"
        )
        if self.transmute_combo.lineEdit():
            self.transmute_combo.lineEdit().setStyleSheet(f"padding: 0px 2px; font-size: {m.font_px(11)}px;")
        self._tb_ok_btn.setFixedHeight(tx_h)
        self._tb_ok_btn.setStyleSheet(
            f"padding: 0px {m.px(8)}px; font-size: {m.font_px(11)}px; border: 1px solid #D946EF; color: #F0E6D2;"
        )
        tool_h = m.px(22, floor=20)
        self.aug_add_combo.setFixedHeight(tool_h)
        self.aug_add_combo.setStyleSheet(f"QComboBox {{ font-size: {m.font_px(11)}px; }}")
        self._aug_confirm_btn.setFixedHeight(tool_h)
        self._aug_confirm_btn.setStyleSheet(f"padding: 0px {m.px(8)}px; font-size: {m.font_px(11)}px;")
        self.dataset_combo.setFixedHeight(tool_h)
        self.dataset_combo.setStyleSheet(f"QComboBox {{ font-size: {m.font_px(11)}px; }}")
        self.hero_combo.setFixedWidth(m.px(104, floor=86))
        self.hero_combo.setFixedHeight(tool_h)
        self.hero_combo.setStyleSheet(f"QComboBox {{ font-size: {m.font_px(11)}px; }}")
        return True

    @staticmethod
    def _width_bucket(w: int) -> int:
        if w < 330:
            return 0
        if w >= 450:
            return 2
        return 1

    def _layout_corner_grips(self) -> None:
        pw = self.panel.width()
        ph = self.panel.height()
        if pw <= 20 or ph <= 20:
            return
        gsz = max(15, min(24, int(round(16 * self._ui_scale))))
        eth = max(7, min(11, int(round(8 * self._ui_scale))))
        top_eth = max(6, eth - 1)

        self._edge_l.setGeometry(0, gsz, eth, max(0, ph - 2 * gsz))
        self._edge_r.setGeometry(pw - eth, gsz, eth, max(0, ph - 2 * gsz))
        self._edge_t.setGeometry(gsz, 0, max(0, pw - 2 * gsz), top_eth)
        self._edge_b.setGeometry(gsz, ph - eth, max(0, pw - 2 * gsz), eth)

        for grip, gx, gy in (
            (self._grip_tl, 0, 0),
            (self._grip_bl, 0, ph - gsz),
            (self._grip_br, pw - gsz, ph - gsz),
        ):
            grip.set_grip_size(gsz)
            grip.move(gx, gy)

        for edge_w in (self._edge_l, self._edge_r, self._edge_t, self._edge_b):
            edge_w.raise_()
        for grip in (self._grip_tl, self._grip_bl, self._grip_br):
            grip.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._expanded:
            self._layout_corner_grips()

    def _apply_header_density(self, w: int) -> None:
        compact = w < 322
        self.hdr_icon.setVisible(not compact)
        self.mode_badge_lbl.setVisible(bool(self._match_mode_badge_text) and not compact)
        self.collapse_btn.setText("收" if compact else "收起")
        m = self._metrics()
        self.collapse_btn.set_scaled_metrics(
            height=m.px(24, floor=20),
            min_width=m.px(26 if compact else 40, floor=26),
            font_pt=m.pt(10),
        )

    def set_match_mode_badge(self, text: str) -> None:
        """Display or clear the active match dataset mode pill (e.g. '首棱彩局') in Hextech header."""
        clean = (text or "").strip()
        self._match_mode_badge_text = clean
        self.mode_badge_lbl.setText(clean)
        self.mode_badge_lbl.setVisible(bool(clean) and self._expanded_w >= 322)
        if clean:
            self.mode_badge_lbl.setToolTip(f"已自动切换至「{clean}」大数据胜率表，对局结束后自动恢复")

    def _rebuild_scaled_dynamic_widgets(self) -> None:
        self._last_roadmap_sig = ()
        self._render_decision_cards()
        if self._store_ref is not None:
            self._render_augment_chips(self._store_ref)
        self._rebuild_quick_confirm()
        if self._lobby_scout is not None and self._radar_detail_expanded:
            self._last_radar_detail_sig = None
            self._rebuild_radar_detail_rows(self._lobby_scout)

    def _invalidate_expanded_layouts(self) -> None:
        self.aug_chips_lay.invalidate()
        self.aug_chips_lay.activate()
        blay = self.panel_body.layout()
        if blay is not None:
            for i in range(blay.count()):
                sub_w = blay.itemAt(i).widget()
                if sub_w is not None and not sub_w.isHidden():
                    sl = sub_w.layout()
                    if sl is not None:
                        sl.invalidate()
                        sl.activate()
            blay.invalidate()
            blay.activate()
        play = self.panel.layout()
        if play is not None:
            play.invalidate()
            play.activate()

    def _compute_auto_expanded_height(self, w: int, geo: QRect) -> int:
        m = self._metrics()
        inner_w = max(200, w - m.px(22))
        minimap_guard_h = int(geo.height() * 0.25)
        blay = self.panel_body.layout()
        body_h_for_w = self.panel_body.heightForWidth(inner_w)
        explicit_h = 0
        if blay is not None:
            b_sp = max(0, blay.spacing())
            vis_cnt = 0
            for i in range(blay.count()):
                w_child = blay.itemAt(i).widget()
                if w_child is not None and not w_child.isHidden():
                    hfw = w_child.heightForWidth(inner_w)
                    ch = hfw if hfw > 0 else max(w_child.sizeHint().height(), w_child.minimumSizeHint().height())
                    explicit_h += ch + (b_sp if vis_cnt > 0 else 0)
                    vis_cnt += 1
        body_hint_h = explicit_h if explicit_h > 0 else max(body_h_for_w if body_h_for_w > 0 else 0, self.panel_body.sizeHint().height())
        header_chrome_h = m.px(56)
        max_h = max(
            self.EXPANDED_MIN_H,
            (geo.height() - minimap_guard_h - 24) if self._anchor_right else (geo.height() - 48),
        )
        return min(max_h, max(self.EXPANDED_MIN_H, header_chrome_h + body_hint_h + m.px(28)))

    def _apply_expanded_dimensions(
        self,
        w: int,
        h: int,
        *,
        x: int | None = None,
        y: int | None = None,
        force_scale: bool = False,
        auto_fit_h: bool = False,
    ) -> None:
        w = max(self.MIN_EXPANDED_W, min(self.MAX_EXPANDED_W, int(w)))
        h_for_scale = None if auto_fit_h else max(self.EXPANDED_MIN_H, int(h))
        self._expanded_w = w
        new_scale = self._compute_scale_for_rect(w, h_for_scale)
        scale_changed = self._apply_ui_scale(new_scale, force=force_scale)
        self._apply_header_density(w)
        self.panel.setFixedWidth(w)
        self.panel_body.setMinimumWidth(0)
        self.panel_body.setMaximumWidth(self._QWIDGETSIZE_MAX)

        new_bucket = self._width_bucket(w)
        if scale_changed or new_bucket != self._last_layout_bucket or abs(w - self._last_rendered_w) >= 24:
            self._last_layout_bucket = new_bucket
            self._last_rendered_w = w
            self._rebuild_scaled_dynamic_widgets()

        self._invalidate_expanded_layouts()
        if auto_fit_h:
            screen = self._active_screen()
            geo = screen.geometry() if screen is not None else QRect(0, 0, 1920, 1080)
            eff_h = self._compute_auto_expanded_height(w, geo)
            eff_y = max(geo.top() + 12, min(geo.bottom() - eff_h - 12, int(y) if y is not None else self.y()))
        else:
            eff_h = max(self.EXPANDED_MIN_H, int(h))
            eff_y = int(y) if y is not None else self.y()

        self.setFixedWidth(w)
        self.setFixedHeight(eff_h)
        self.panel.setFixedHeight(eff_h)
        if x is not None and y is not None:
            self.setGeometry(int(x), eff_y, w, eff_h)
        self._layout_corner_grips()

    def _on_live_resize(self, target: QRect, edge: str) -> None:
        if self._slide_anim.state() == QVariantAnimation.State.Running:
            self._slide_anim.stop()
        self._has_custom_w = True
        has_v = ("t" in edge) or ("b" in edge)
        if has_v:
            self._custom_h = max(self.EXPANDED_MIN_H, target.height())
        auto_fit_h = (not has_v) and (self._custom_h is None)
        eff_h = self._custom_h if self._custom_h is not None else target.height()
        self._apply_expanded_dimensions(
            target.width(),
            eff_h,
            x=target.x(),
            y=target.y(),
            force_scale=False,
            auto_fit_h=auto_fit_h,
        )

    def _on_resize_finished(self, target: QRect, edge: str) -> None:
        self._has_custom_w = True
        w = max(self.MIN_EXPANDED_W, min(self.MAX_EXPANDED_W, target.width()))
        has_v = ("t" in edge) or ("b" in edge)
        if has_v:
            self._custom_h = max(self.EXPANDED_MIN_H, target.height())
        auto_fit_h = (not has_v) and (self._custom_h is None)
        eff_h = self._custom_h if self._custom_h is not None else target.height()
        self._apply_expanded_dimensions(
            w,
            eff_h,
            x=target.x(),
            y=target.y(),
            force_scale=True,
            auto_fit_h=auto_fit_h,
        )
        screen = QGuiApplication.screenAt(self.frameGeometry().center()) or self._active_screen()
        if screen is not None:
            self._target_screen = screen
            geo = screen.geometry()
            self._anchor_right = (geo.right() - (self.x() + self.width())) <= 28
            if self._anchor_right:
                self._custom_pos = QPoint(geo.right() - self.COLLAPSED_W + 1, self.y())
            else:
                self._custom_pos = self.pos()
        if self._on_size_changed:
            saved_h = self._custom_h if self._custom_h is not None else 0
            self._on_size_changed([self._expanded_w, saved_h])
        if self._on_scale_changed:
            self._on_scale_changed(round(self._ui_scale, 3))
        if self._on_pos_changed and self._custom_pos is not None:
            self._on_pos_changed([self._custom_pos.x(), self._custom_pos.y()])

    def reset_default_size(self) -> None:
        self._has_custom_w = False
        self._custom_h = None
        screen = self._active_screen()
        if screen is not None:
            geo = screen.geometry()
            dpr = max(1.0, float(screen.devicePixelRatio() or 1.0))
            self._expanded_w = self.compute_default_expanded_width(geo.width(), geo.height(), dpr)
        else:
            self._expanded_w = self.EXPANDED_W
        self._applied_ui_scale = 0.0
        self._apply_ui_scale(self._compute_scale_for_rect(self._expanded_w, None), force=True)
        self._last_layout_bucket = self._width_bucket(self._expanded_w)
        self._last_rendered_w = self._expanded_w
        self._rebuild_scaled_dynamic_widgets()
        self._reposition_right_edge()
        if self._on_size_changed:
            self._on_size_changed(None)
        if self._on_scale_changed:
            self._on_scale_changed(None)

    def _get_item_pixmap(self, item_id: str, size: int) -> QPixmap | None:
        norm = normalize_item_id(item_id)
        if not norm or self._store_ref is None:
            return None
        key = (norm, size)
        cached = self._pixmap_cache.get(key)
        if cached is not None:
            return cached
        path = self._store_ref.static.icons_dir / "item" / f"{norm}.png"
        if path.exists() and path.stat().st_size > 64:
            pm = QPixmap(str(path))
            if not pm.isNull():
                scaled = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
                self._pixmap_cache[key] = scaled
                return scaled
            self._icon_fetch_attempted.add(norm)
            return None
        # Non-blocking background fetch if not cached yet (attempt at most once per drawer instance)
        if norm not in self._fetching_icons and norm not in self._icon_fetch_attempted and not self._is_closed:
            self._fetching_icons.add(norm)
            self._icon_fetch_attempted.add(norm)
            store = self._store_ref

            def _fetch() -> None:
                try:
                    p = store.static.ensure_icon("item", norm)
                    if not self._is_closed and p is not None and p.exists() and p.stat().st_size > 64:
                        self._icon_invoker.invoke.emit(self._on_icon_ready)
                finally:
                    self._fetching_icons.discard(norm)

            threading.Thread(target=_fetch, name=f"HexIcon-{norm}", daemon=True).start()
        return None

    def showEvent(self, event) -> None:
        self._is_closed = False
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        self._is_closed = True
        if self._slide_anim.state() == QVariantAnimation.State.Running:
            self._slide_anim.stop()
        super().closeEvent(event)

    def _on_icon_ready(self) -> None:
        if self._is_closed:
            return
        try:
            self._last_roadmap_sig = ()
            self._render_decision_cards()
        except RuntimeError:
            return

    def _toggle_manual_aug_picker(self) -> None:
        self.aug_manual_row.setVisible(not self.aug_manual_row.isVisible())
        if self._commercial and self._lifecycle is not None and self._lifecycle.is_sealed_dormant:
            self._sync_seal_slots()
        if self.isVisible():
            self._reposition_right_edge()

    def _toggle_tools_row(self) -> None:
        if self._is_full_locked:
            return
        self.tools_row.setVisible(not self.tools_row.isVisible())
        if self._commercial and self._lifecycle is not None and self._lifecycle.is_sealed_dormant:
            self._sync_seal_slots()
        if self.isVisible():
            self._reposition_right_edge()

    def flash_new_augment(self, aug_id: str) -> None:
        """Fire a crisp cyan-gold rim pulse on the augments section when an augment is auto-recorded."""
        color = "#D946EF" if is_transmute_augment(aug_id) else "#0AC8B9"
        self.aug_frame.trigger_pulse(color, duration_ms=620)

    @classmethod
    def _infer_initial_anchor_right(cls, pos: QPoint | None) -> bool:
        if pos is None:
            return True
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return False
        geo = screen.geometry()
        ix = pos.x()
        near_collapsed_right = (geo.right() - (ix + cls.COLLAPSED_W)) <= 32
        near_expanded_right = abs(ix - (geo.right() - cls.EXPANDED_W + 1)) <= 32
        return near_collapsed_right or near_expanded_right

    def _on_slide_finished(self) -> None:
        target_w = self._expanded_w if self._expanded else self.COLLAPSED_W
        if self.width() != target_w:
            self._apply_animated_width(target_w)
        if self._expanded:
            self._layout_corner_grips()

    def set_target_screen(self, screen: QScreen | None) -> None:
        if screen is not None and screen is not self._target_screen:
            self._target_screen = screen
            self._reposition_right_edge()

    def _active_screen(self) -> QScreen | None:
        if self._target_screen is not None and self._target_screen in QGuiApplication.screens():
            return self._target_screen
        return QGuiApplication.primaryScreen()

    def _apply_animated_width(self, val) -> None:
        screen = self._active_screen()
        if screen is None:
            return
        geo = screen.geometry()
        w = max(self.COLLAPSED_W, int(val))
        self.setFixedWidth(w)
        h = self.height()
        if self._anchor_right:
            x = geo.right() - w + 1
        else:
            x = max(geo.left(), min(geo.right() - w + 1, self.x()))
        self.setGeometry(x, self.y(), w, h)

    def _reposition_right_edge(self) -> None:
        screen = self._active_screen()
        if screen is None:
            return
        if self._slide_anim.state() == QVariantAnimation.State.Running:
            self._slide_anim.stop()
        geo = screen.geometry()
        dpr = max(1.0, float(screen.devicePixelRatio() or 1.0))
        if self._expanded and not self._has_custom_w:
            self._expanded_w = self.compute_default_expanded_width(geo.width(), geo.height(), dpr)
        w = min(self._expanded_w, max(self.MIN_EXPANDED_W, geo.width() - 24)) if self._expanded else self.COLLAPSED_W
        minimap_guard_h = int(geo.height() * 0.25)
        if self._expanded:
            target_scale = self._compute_scale_for_rect(w, self._custom_h)
            scale_changed = self._apply_ui_scale(target_scale, force=False)
            self._apply_header_density(w)
            if self.panel.width() != w:
                self.panel.setFixedWidth(w)
            self.panel_body.setMinimumWidth(0)
            self.panel_body.setMaximumWidth(self._QWIDGETSIZE_MAX)
            if scale_changed or self._width_bucket(w) != self._last_layout_bucket or abs(w - self._last_rendered_w) >= 24:
                self._last_layout_bucket = self._width_bucket(w)
                self._last_rendered_w = w
                self._rebuild_scaled_dynamic_widgets()
            self._invalidate_expanded_layouts()
            if self._custom_h is not None:
                h = min(geo.height() - 32, max(self.EXPANDED_MIN_H, self._custom_h))
            else:
                h = self._compute_auto_expanded_height(w, geo)
            if self.panel.height() != h or self.panel.minimumHeight() != h or self.panel.maximumHeight() != h:
                self.panel.setFixedHeight(h)
        else:
            if self.pill.width() != w:
                self.pill.setFixedWidth(w)
            play = self.pill.layout()
            if play is not None:
                play.invalidate()
                play.activate()
            # Measure pill natural hint cleanly without mutating window geometry when unchanged
            saved_min_h, saved_max_h = self.pill.minimumHeight(), self.pill.maximumHeight()
            if saved_min_h == saved_max_h and saved_min_h > 0:
                self.pill.setMinimumHeight(0)
                self.pill.setMaximumHeight(self._QWIDGETSIZE_MAX)
            hint_h = max(self.COLLAPSED_MIN_H, self.pill.sizeHint().height())
            h = min(geo.height() - 64, hint_h + 4)
            if self.pill.height() != h or self.pill.minimumHeight() != h or self.pill.maximumHeight() != h:
                self.pill.setFixedHeight(h)

        if self._custom_pos is not None and not self._anchor_right:
            x = max(geo.left(), min(geo.right() - w + 1, self._custom_pos.x()))
            y = max(geo.top(), min(geo.bottom() - h + 1, self._custom_pos.y()))
        else:
            y_base = self._custom_pos.y() if self._custom_pos is not None else int(geo.top() + geo.height() * self._y_ratio)
            # Keep expanded right-anchored drawer above bottom-right LoL minimap zone unless user customized height
            bottom_limit = (
                (geo.bottom() - 16)
                if (not self._expanded or self._custom_h is not None)
                else (geo.bottom() - minimap_guard_h - 8)
            )
            max_y = max(geo.top() + 12, bottom_limit - h)
            y = max(geo.top() + 12, min(max_y, y_base))
            x = geo.right() - w + 1

        target_rect = QRect(x, y, w, h)
        if self.minimumWidth() != w or self.maximumWidth() != w:
            self.setFixedWidth(w)
        if self.minimumHeight() != h or self.maximumHeight() != h:
            self.setFixedHeight(h)
        if self.geometry() != target_rect:
            self.setGeometry(target_rect)
        if self._expanded:
            self._layout_corner_grips()

    def _close_by_user(self) -> None:
        self.hide()
        if self._on_closed_by_user:
            self._on_closed_by_user()

    def reset_to_right_edge(self) -> None:
        self._custom_pos = None
        self._anchor_right = True
        self._reposition_right_edge()
        if self._on_pos_changed:
            self._on_pos_changed([self.x(), self.y()])

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._resize_ctrl.is_resizing or (
                self._expanded and self._resize_ctrl.hit_test_global(event.globalPosition().toPoint())
            ):
                event.ignore()
                return
            header_drag_h = self._px(40, floor=36)
            if (not self._expanded) or event.position().y() <= header_drag_h:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
                return
        elif event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            a_toggle = QAction("收起成小条" if self._expanded else "展开抽屉", menu)
            a_toggle.triggered.connect(self.toggle_expanded)
            a_reset_size = QAction("恢复默认尺寸（适配屏幕）", menu)
            a_reset_size.triggered.connect(self.reset_default_size)
            a_reset = QAction("复位到屏幕右侧", menu)
            a_reset.triggered.connect(self.reset_to_right_edge)
            a_hide = QAction("隐藏悬浮窗", menu)
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

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self._expanded:
            self._drag_offset = None
            self.set_expanded(True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            screen = QGuiApplication.screenAt(self.frameGeometry().center()) or self._active_screen()
            if screen is not None:
                self._target_screen = screen
                geo = screen.geometry()
                self._anchor_right = (geo.right() - (self.x() + self.width())) <= 28
                if self._anchor_right:
                    snapped_x = geo.right() - self.width() + 1
                    self.move(snapped_x, self.y())
                    self._custom_pos = QPoint(geo.right() - self.COLLAPSED_W + 1, self.y())
                else:
                    self._custom_pos = self.pos()
            else:
                self._anchor_right = False
                self._custom_pos = self.pos()
            if self._on_pos_changed:
                self._on_pos_changed([self._custom_pos.x(), self._custom_pos.y()])
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def set_expanded(self, expanded: bool) -> None:
        was_visible = self.isVisible()
        if self._slide_anim.state() == QVariantAnimation.State.Running:
            self._slide_anim.stop()
        self._expanded = bool(expanded)
        self.pill.setVisible(not self._expanded)
        self.panel.setVisible(self._expanded)
        # Single-frame snap eliminates 14x 60fps WS_EX_LAYERED HWND resize reallocations (saves ~30% DX11 1% Low!)
        self._reposition_right_edge()
        if was_visible:
            show_no_activate(self, click_through=False)
        if self._expanded and self._pending_auto_unseal:
            self._pending_auto_unseal = False
            QTimer.singleShot(120, lambda: self.play_ingame_auto_unseal(force_now=True))

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    def update_hotkey_labels(self, hk: HotkeyConfig) -> None:
        k_coach = (hk.call_coach or "").strip()
        k_drawer = (hk.toggle_drawer or "").strip()
        k_sidebars = (hk.toggle_sidebars or "").strip()
        short_btn = k_coach if (k_coach and len(k_coach) <= 4) else "AI"
        self.coach_btn.setText(short_btn)
        self.coach_btn.setToolTip(
            f"立即呼叫 AI 参谋刷新 ({k_coach})" if k_coach else "立即呼叫 AI 参谋刷新"
        )
        reopen_hint = f"按 {k_drawer} 重开" if k_drawer else "可在托盘重开"
        self.close_btn.setToolTip(f"隐藏悬浮窗（{reopen_hint}）")
        self.pill_close_btn.setToolTip(f"隐藏悬浮小窗（{reopen_hint}）")
        parts = ["双击或点「展」展开"]
        if k_drawer:
            parts.append(f"{k_drawer} 展开/收起")
        if k_sidebars:
            parts.append(f"{k_sidebars} 双侧栏同步")
        parts.append("按住拖动位置 · 点 × 隐藏 · 右键更多选项")
        self.pill.setToolTip(" · ".join(parts))

    def show_overlay(self) -> None:
        self._reposition_right_edge()
        show_no_activate(self, click_through=False)
        if self._expanded and self._pending_auto_unseal:
            self._pending_auto_unseal = False
            QTimer.singleShot(120, lambda: self.play_ingame_auto_unseal(force_now=True))

    def _dismiss_opening_brief_seal(self) -> None:
        self._opening_brief_seal_dismissed = True
        self._opening_brief_seal_dismissed_count += 1
        self.opening_brief_seal_wrap.hide()
        if self._on_opening_brief_seal_dismissed:
            self._on_opening_brief_seal_dismissed()
        if self._expanded and self.isVisible():
            self._reposition_right_edge()

    def _on_drawer_seal_clicked(self, seal_id: str, mode_val: str) -> None:
        if mode_val == SealMode.CHARGED_READY.value:
            # Shatter both drawer seals cleanly without stealing LOL focus
            if self.opening_brief_seal.mode == SealMode.CHARGED_READY:
                self.opening_brief_seal.play_unseal(fast=True)
            if self.coach_seal_overlay.mode == SealMode.CHARGED_READY:
                self.coach_seal_overlay.play_unseal(fast=True)
            self.inline_preview_box.hide()
            if self._on_core_action:
                self._on_core_action("unsealed_ingame")
            return
        # In-game click NEVER pops a modal window to steal LOL focus -> expand non-activating inline preview!
        if mode_val == SealMode.RETURNING_DORMANT.value:
            self.inline_preview_lbl.setText(
                "◈ 本地 5ms 出装持续护航 · 补能后自动恢复 10 人实时推演与精确散件买法"
            )
            self.inline_preview_cta_btn.setText("快速补能")
        elif mode_val == SealMode.GUEST_CLAIM.value:
            self.inline_preview_lbl.setText(
                "◈ 新用户注册即赠 450 积分（约 1.5-2 局智能 AI 体验）\n"
                "◈ 进局 5 秒开局定盘 + 阵亡精确双散件 · 本地 5ms 查表永久免费"
            )
            self.inline_preview_cta_btn.setText("免费领 450 积分")
        else:
            self.inline_preview_lbl.setText(
                "◈ 本地 5ms 出装与 3-4 卡识别永久免费不锁\n"
                "◈ 云端 AI 参谋：开局定盘 + 精准双散件 + 72 质变重构（5元打 15-25 局 · 永不过期）"
            )
            self.inline_preview_cta_btn.setText("查看实战对比 / 充能")
        self.inline_preview_box.setVisible(not self.inline_preview_box.isVisible())
        if self._expanded and self.isVisible():
            self._reposition_right_edge()

    def _on_inline_preview_cta(self) -> None:
        self.inline_preview_box.hide()
        st = self._lifecycle.state if self._lifecycle is not None else None
        if st == CoreEnergyState.GUEST_UNCLAIMED:
            if self._on_core_action:
                self._on_core_action("register")
            elif self._on_login_click:
                self._on_login_click()
        elif st == CoreEnergyState.CORRODED_RETURNING:
            if self._on_core_action:
                self._on_core_action("recharge")
            elif self._on_recharge_click:
                self._on_recharge_click()
        else:
            if self._on_core_action:
                self._on_core_action("preview")
            elif self._on_recharge_click:
                self._on_recharge_click()

    def play_ingame_auto_unseal(self, *, force_now: bool = False) -> None:
        """Track B: Streamlined 380ms auto-shatter on in-game recharge without blocking shopping."""
        if not force_now and (not self.isVisible() or not self._expanded):
            self._pending_auto_unseal = True
            return
        self._pending_auto_unseal = False
        self.inline_preview_box.hide()
        played = False
        if self.coach_seal_slot.isVisible() and self.coach_seal_overlay.mode != SealMode.UNLOCKED:
            self.coach_seal_overlay.play_unseal(fast=True)
            QTimer.singleShot(410, self.coach_seal_slot.hide)
            played = True
        if self.opening_brief_seal_wrap.isVisible() and self.opening_brief_seal.mode != SealMode.UNLOCKED:
            self.opening_brief_seal.play_unseal(fast=True)
            QTimer.singleShot(410, self.opening_brief_seal_wrap.hide)
            played = True
        if not played:
            self.coach_card.trigger_pulse("#0AC8B9", 520)

    def _compute_live_peephole_hint(self) -> str:
        if self._lifecycle is None:
            return ""
        special_name = ""
        if self._selected_augments:
            try:
                spec_db = get_special_augment_db()
                for aid in self._selected_augments:
                    entry = spec_db.get(aid) if hasattr(spec_db, "get") else None
                    if entry is not None:
                        special_name = getattr(entry, "name", "") or ""
                        break
            except Exception:
                pass
        threat = self._threat
        tags = list(getattr(threat, "summary_tags", ()) or ()) if threat is not None else []
        top_name = str(getattr(threat, "top_threat_name", "") or "") if threat is not None else ""
        top_kda = str(getattr(threat, "top_threat_kda", "") or "") if threat is not None else ""
        heal_high = bool(getattr(threat, "enemy_heal_score", 0.0) >= 1.8) if threat is not None else False
        ally_gw = bool(getattr(threat, "ally_has_grievous", False)) if threat is not None else False
        return build_live_peephole_hint(
            state=self._lifecycle.state,
            my_gold=self._my_gold,
            is_dead=self._is_player_dead,
            game_time_s=self._match_game_time_s,
            special_augment_name=special_name,
            threat_tags=tags,
            top_threat_name=top_name,
            top_threat_kda=top_kda,
            enemy_heal_high=heal_high,
            ally_has_grievous=ally_gw,
        )

    def _sync_seal_slots(self) -> None:
        if not self._commercial or self._is_full_locked or self._lifecycle is None:
            self.coach_seal_slot.hide()
            self.opening_brief_seal_wrap.hide()
            self.inline_preview_box.hide()
            self.cutoff_gate_bar.show()
            self.coach_content_lbl.show()
            return
        st = self._lifecycle.state
        if st in (
            CoreEnergyState.SYSTEM_GATE,
            CoreEnergyState.DISABLED_BY_USER,
            CoreEnergyState.SYNCING,
            CoreEnergyState.TRIAL_ACTIVE,
            CoreEnergyState.TRIAL_LOW,
            CoreEnergyState.READY_TO_UNSEAL,
            CoreEnergyState.ACTIVE_PAID,
            CoreEnergyState.PAID_LOW,
        ):
            self.coach_seal_overlay.set_seal_mode(SealMode.UNLOCKED)
            self.coach_seal_slot.hide()
            self.opening_brief_seal.set_seal_mode(SealMode.UNLOCKED)
            self.opening_brief_seal_wrap.hide()
            self.inline_preview_box.hide()
            self.cutoff_gate_bar.show()
            self.coach_content_lbl.show()
            if not self._ai_cutoff:
                self.cutoff_banner.hide()
            return

        self._peephole_hint = self._compute_live_peephole_hint()
        mode = self._lifecycle.seal_mode
        if st == CoreEnergyState.GUEST_UNCLAIMED:
            title_c = "初始晶核 · 450 积分待认领"
            cta_c = "领 2 局体验"
            brief_cta = "领 450 积分"
        elif st == CoreEnergyState.CORRODED_RETURNING:
            title_c = "海克斯核心 · 待补能"
            cta_c = "快速补能"
            brief_cta = "补能"
        elif st == CoreEnergyState.READY_TO_UNSEAL:
            title_c = "核心已充能 · 点击破封"
            cta_c = "✦ 破封"
            brief_cta = "✦ 破封"
        else:
            title_c = "海克斯 AI 参谋 · 休眠封印"
            cta_c = "查看差异"
            brief_cta = "5元充能"

        # If mid-match 402 happened while existing advice is still valid, keep existing advice readable without hiding it
        if self._coach_advice is not None and st in (
            CoreEnergyState.CORRODED_TRIAL_END,
            CoreEnergyState.CORRODED_RETURNING,
        ):
            self.coach_seal_slot.hide()
            self.cutoff_gate_bar.show()
            self.coach_content_lbl.show()
        else:
            self.coach_seal_overlay.set_snoozed(self._lifecycle.snoozed)
            self.coach_seal_overlay.set_copy(title=title_c, subtitle=self._peephole_hint, cta_text=cta_c)
            self.coach_seal_overlay.set_seal_mode(mode)
            self.coach_seal_slot.show()
            # Keep cutoff_banner visible as container while replacing inner gate bar with the Weathered Seal strip
            self.cutoff_gate_bar.hide()
            self.coach_content_lbl.hide()
            self.cutoff_banner.show()

        # Compact OpeningBrief seal slot (only early game <= 180s or pre-game, never auto-expands drawer)
        show_ob_seal = (
            not self._opening_brief_data
            and not self._opening_brief_dismissed
            and not self._opening_brief_seal_dismissed
            and not self._lifecycle.snoozed
            and (self._match_game_time_s <= 180.0)
        )
        if show_ob_seal:
            compact_h = self._px(30 if self._opening_brief_seal_dismissed_count >= 3 else 54, floor=26)
            self.opening_brief_seal_wrap.setFixedHeight(compact_h)
            ob_sub = self._peephole_hint if compact_h >= 50 else ""
            self.opening_brief_seal.set_snoozed(self._lifecycle.snoozed)
            self.opening_brief_seal.set_copy(
                title="开局 10 人作战简报",
                subtitle=ob_sub,
                cta_text=brief_cta if compact_h >= 50 else "",
            )
            self.opening_brief_seal.set_seal_mode(mode)
            self.opening_brief_seal_wrap.show()
            self.opening_brief_seal_close_btn.raise_()
        else:
            self.opening_brief_seal.set_seal_mode(SealMode.UNLOCKED)
            self.opening_brief_seal_wrap.hide()

    def set_core_lifecycle(
        self,
        snapshot: CoreLifecycleSnapshot | None,
        *,
        dismissed_count: int | None = None,
    ) -> None:
        self._lifecycle = snapshot
        if dismissed_count is not None:
            self._opening_brief_seal_dismissed_count = max(0, int(dismissed_count))
        self._sync_seal_slots()
        if snapshot is not None and snapshot.is_low_energy and self.coach_balance_lbl.isVisible():
            self._coach_balance_color = "#FDE68A"
            self.coach_balance_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #FDE68A; font-size: {self._fs(10)}px; font-weight: bold;"
            )
        elif self.coach_balance_lbl.isVisible():
            self._coach_balance_color = "#5EEAD4"
            self.coach_balance_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #5EEAD4; font-size: {self._fs(10)}px; font-weight: bold;"
            )
        if self._expanded and self.isVisible():
            self._reposition_right_edge()

    def set_live_vital_state(self, *, is_dead: bool = False, game_time_s: float = 0.0) -> None:
        prev_collapsed = self._match_game_time_s >= 90.0
        self._is_player_dead = bool(is_dead)
        self._match_game_time_s = max(0.0, float(game_time_s or 0.0))
        now_collapsed = self._match_game_time_s >= 90.0
        if self._lobby_scout is not None and prev_collapsed != now_collapsed:
            self._sync_lobby_radar()
        if self._commercial and self._lifecycle is not None and self._lifecycle.is_sealed_dormant:
            self._sync_seal_slots()

    def set_warning(self, message: str) -> None:
        self._warning_text = message
        if message:
            self.warn_lbl.setText(message)
            self.warn_lbl.show()
        else:
            self.warn_lbl.hide()

    def set_picker_active(self, active: bool) -> None:
        """Immediately hide stale 3-card pick tips as soon as picker closes."""
        if self._picker_active != active:
            self._picker_active = active
            self._render_decision_cards()
            if self._expanded and self.isVisible():
                self._reposition_right_edge()

    def _handle_tier_switch(self, tier: str) -> None:
        if self._commercial and self._ai_cutoff and self._cutoff_code in ("NOT_LOGGED_IN", "INSUFFICIENT_CREDITS"):
            self.tier_nudge_lbl.setText(
                "◈ 登录领 450 积分或充能唤醒核心后可自由切换档位（当前本地 5ms 引擎免费运行中）"
            )
            self.tier_nudge_lbl.show()
            self._tier_nudge_timer.start(2400)
            return
        self.set_coach_tier(tier)
        if self._on_switch_tier:
            self._on_switch_tier(self._coach_tier)

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

    def set_coach_tier(self, tier: str) -> None:
        raw = (tier or "a").strip().lower()
        norm = raw if raw in ("a", "b", "auto") else "a"
        self._coach_tier = norm
        fs = self._fs(9)
        pad_x = self._px(5)
        active_qss = (
            "QPushButton { background-color: rgba(10, 200, 185, 65); border: 1px solid #0AC8B9; "
            f"color: #F0E6D2; font-weight: bold; font-size: {fs}px; padding: 0px {pad_x}px; border-radius: 2px; }}"
        )
        idle_qss = (
            "QPushButton { background-color: rgba(8, 18, 32, 195); border: 1px solid #463714; "
            f"color: #A09B8C; font-size: {fs}px; padding: 0px {pad_x}px; border-radius: 2px; }}"
            "QPushButton:hover { border-color: #C8AA6E; color: #F0E6D2; }"
        )
        self.tier_auto_pill.setStyleSheet(active_qss if norm == "auto" else idle_qss)
        self.tier_a_pill.setStyleSheet(active_qss if norm == "a" else idle_qss)
        self.tier_b_pill.setStyleSheet(active_qss if norm == "b" else idle_qss)
        if self._balance_credits is not None:
            self.set_balance_credits(self._balance_credits)

    @property
    def _is_full_locked(self) -> bool:
        return bool(
            self._commercial
            and self._ai_cutoff
            and (
                self._cutoff_policy == "lock_all"
                or self._cutoff_code in ("SERVICE_LOCKED", "CLIENT_UPDATE_REQUIRED")
            )
        )

    def set_balance_credits(self, balance_credits: float | None) -> None:
        self._balance_credits = balance_credits
        if not self._commercial:
            self.coach_balance_lbl.hide()
            return
        if balance_credits is None:
            if self._coach_loading and not self._ai_cutoff:
                self.coach_balance_lbl.setText("余额同步中")
                self.coach_balance_lbl.setToolTip("正在同步账户余额…")
                self.coach_balance_lbl.show()
            else:
                self.coach_balance_lbl.hide()
            return
        friendly = format_balance_match_friendly(balance_credits, tier=self._coach_tier)
        short_pill = friendly.split(" (", 1)[0]
        self.coach_balance_lbl.setText(short_pill)
        self.coach_balance_lbl.setToolTip(f"{friendly} · 永不过期")
        self.coach_balance_lbl.show()

    def set_coach_loading(
        self,
        loading: bool,
        label: str = "● 云端连接中...",
        hint: str = "正在连接云端并同步账户积分余额…",
    ) -> None:
        self._coach_loading = bool(loading)
        if not self._commercial:
            return
        if self._coach_loading and not self._ai_cutoff:
            self.pill_coach_dot.setStyleSheet("color: #FBBF24; font-size: 10px;")
            self.coach_status_lbl.setText(label)
            self._coach_status_color = "#FBBF24"
            self.coach_status_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #FBBF24; font-weight: bold; font-size: {self._fs(10)}px;"
            )
            if self._balance_credits is None:
                self.coach_balance_lbl.setText("余额同步中")
                self.coach_balance_lbl.show()
            if self._coach_advice is None:
                self.coach_content_lbl.setText(hint)
        elif not self._coach_loading and self._balance_credits is None:
            self.coach_balance_lbl.hide()

    def _apply_cutoff_content_hint(self) -> None:
        eff_code = self._cutoff_code or "INSUFFICIENT_CREDITS"
        if eff_code == "SERVICE_LOCKED":
            self.coach_content_lbl.setText(self._cutoff_reason or "服务维护升级中，功能暂不可用")
            return
        if eff_code == "CLIENT_UPDATE_REQUIRED":
            self.coach_content_lbl.setText(self._cutoff_reason or "当前版本已停用，请更新至最新版本")
            return
        if self._is_full_locked:
            if eff_code == "NOT_LOGGED_IN":
                self.coach_content_lbl.setText("当前策略要求先登录账号 · 登录后解锁本地引擎与 AI 参谋")
            elif eff_code == "ACCOUNT_DISABLED":
                self.coach_content_lbl.setText("当前账号已被限制 · 全部功能已暂停使用")
            else:
                self.coach_content_lbl.setText("余额不足 · 全功能已锁定，请扫码充值后立即解锁")
            return
        if eff_code == "NOT_LOGGED_IN":
            self.coach_content_lbl.setText("本地 5ms 引擎运行中 · 登录后开启云端 AI 参谋")
        elif eff_code == "ACCOUNT_DISABLED":
            self.coach_content_lbl.setText("本地 5ms 引擎运行中 · 当前账号已被限制")
        else:
            self.coach_content_lbl.setText("本地 5ms 引擎仍在护航 · 充值后立即恢复 AI 推演")

    def set_ai_cutoff(
        self,
        locked: bool,
        reason: str = "",
        balance_credits: float | None = None,
        code: str = "",
        cutoff_policy: str | None = None,
        update_available: bool = False,
    ) -> None:
        self._ai_cutoff = bool(locked)
        if cutoff_policy is not None:
            self._cutoff_policy = "lock_all" if str(cutoff_policy).strip().lower() == "lock_all" else "ai_only"
        self._cutoff_reason = reason
        self._update_available = bool(update_available)
        if self._ai_cutoff:
            self._coach_loading = False
        self.set_balance_credits(balance_credits)
        if self._ai_cutoff:
            self._coach_pending = False
            self.coach_shimmer.set_active(False)
            self.coach_shimmer.hide()
            eff_code = code or "INSUFFICIENT_CREDITS"
            self._cutoff_code = eff_code
            is_gate = eff_code in ("SERVICE_LOCKED", "CLIENT_UPDATE_REQUIRED", "ACCOUNT_DISABLED")
            # Active laning capsule stays calm neutral slate (#5B5A56) for normal free/0-credit users; crimson ONLY for hard system gates
            dot_color = "#F87171" if is_gate else "#5B5A56"
            status_color = "#F87171" if is_gate else ("#0AC8B9" if eff_code == "NOT_LOGGED_IN" else "#C8AA6E")
            self.pill_coach_dot.setStyleSheet(f"color: {dot_color}; font-size: 10px;")
            if is_gate:
                self.cutoff_gate_bar.setStyleSheet(
                    "QFrame { background-color: rgba(120, 28, 28, 225); border: 1px solid #F59E0B; border-radius: 2px; }"
                )
                self._cutoff_banner_color = "#FEF3C7"
                self.cutoff_banner_lbl.setStyleSheet(
                    f"border: none; background: transparent; color: #FEF3C7; font-size: {self._fs(10)}px; font-weight: bold;"
                )
            else:
                self.cutoff_gate_bar.setStyleSheet(
                    "QFrame { background-color: rgba(6, 20, 34, 232); border: 1px solid #785A28; "
                    "border-left: 2px solid #0AC8B9; border-radius: 2px; }"
                )
                self._cutoff_banner_color = "#E2D9C5"
                self.cutoff_banner_lbl.setStyleSheet(
                    f"border: none; background: transparent; color: #E2D9C5; font-size: {self._fs(10)}px; font-weight: bold;"
                )
            if eff_code == "SERVICE_LOCKED":
                self.coach_status_lbl.setText("● 服务维护中")
                self.cutoff_banner_lbl.setText(reason or "服务维护升级中")
                self.cutoff_action_btn.setText("立即更新" if self._update_available else "检查更新")
                self.cutoff_action_btn.show()
            elif eff_code == "CLIENT_UPDATE_REQUIRED":
                self.coach_status_lbl.setText("● 需更新版本")
                self.cutoff_banner_lbl.setText(reason or "版本已停用，请立即更新")
                self.cutoff_action_btn.setText("立即更新")
                self.cutoff_action_btn.show()
            elif eff_code == "NOT_LOGGED_IN":
                self.coach_status_lbl.setText("◇ 未登录 · 晶核待认领")
                self.cutoff_banner_lbl.setText(reason or "注册赠 450 积分免费试 2 局（本地 5ms 永久免费）")
                self.cutoff_action_btn.setText("登录领积分")
                self.cutoff_action_btn.show()
            elif eff_code == "ACCOUNT_DISABLED":
                self.coach_status_lbl.setText("● 账号受限")
                self.cutoff_banner_lbl.setText(reason or "账号已停用")
                self.cutoff_action_btn.hide()
            else:
                self.coach_status_lbl.setText("◇ 核心休眠 · 余额不足")
                self.cutoff_banner_lbl.setText(
                    "本局已生成策略保持可见 · 下局进入休眠封印"
                    if self._coach_advice is not None and not self._is_full_locked
                    else (reason or "余额不足 · 本地 5ms 持续护航")
                )
                self.cutoff_action_btn.setText("扫码充值")
                self.cutoff_action_btn.show()
            self._coach_status_color = status_color
            self.coach_status_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {status_color}; font-weight: bold; font-size: {self._fs(10)}px;"
            )
            self.cutoff_banner.show()
            if self._coach_advice is None or self._is_full_locked:
                self._apply_cutoff_content_hint()
        else:
            self._cutoff_code = ""
            self.cutoff_banner.hide()
            if not self._coach_loading:
                self.set_coach_online(self._coach_online, label=self._coach_online_label, ready_hint=self._coach_ready_hint)

        self._sync_seal_slots()

        full_locked = self._is_full_locked
        if full_locked:
            self.opening_brief_card.hide()
        else:
            self._render_opening_brief()
        self._sync_lobby_radar()
        self.next_buy_card.setVisible(not full_locked)
        self.roadmap_card.setVisible(not full_locked)
        self.aug_frame.setVisible(not full_locked)
        if full_locked:
            self.tools_row.hide()
        self.destiny_card.setVisible(self._destiny_has_content and not full_locked)
        self._render_decision_cards()

        if self._expanded and self.isVisible():
            self._reposition_right_edge()

    def set_coach_pending(self, pending: bool, pending_label: str = "") -> None:
        self._coach_pending = pending
        self._coach_pending_label = pending_label
        if pending and self._coach_online and not self._ai_cutoff and not self._coach_loading:
            self.coach_shimmer.show()
            self.coach_shimmer.set_active(True)
            self.pill_coach_dot.setStyleSheet("border: none; background: transparent; color: #FBBF24; font-size: 10px;")
            self.coach_status_lbl.setText(pending_label or "● 参谋同步中…")
            self._coach_status_color = "#FBBF24"
            self.coach_status_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #FBBF24; font-weight: bold; font-size: {self._fs(10)}px;"
            )
        else:
            self.coach_shimmer.set_active(False)
            self.coach_shimmer.hide()
            if self._ai_cutoff or self._coach_loading:
                return
            self.set_coach_online(self._coach_online, label=self._coach_online_label, ready_hint=self._coach_ready_hint)

    def set_coach_error(self, message: str) -> None:
        self._coach_pending = False
        self.coach_shimmer.set_active(False)
        self.coach_shimmer.hide()
        self.coach_status_lbl.setText("● 参谋调用异常")
        self._coach_status_color = "#F87171"
        self.coach_status_lbl.setStyleSheet(
            f"border: none; background: transparent; color: #F87171; font-weight: bold; font-size: {self._fs(10)}px;"
        )
        if message:
            self.coach_content_lbl.setText(_clip_text(message, 46))
            self.coach_content_lbl.setToolTip(message)

    def set_coach_online(self, online: bool, label: str = "", ready_hint: str = "") -> None:
        self._coach_online = online
        self._coach_online_label = label
        self._coach_ready_hint = ready_hint
        if online:
            self.pill_coach_dot.setStyleSheet("color: #09C269; font-size: 11px;")
            self.coach_status_lbl.setText(label or "● 参谋在线")
            self._coach_status_color = "#09C269"
            self.coach_status_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #09C269; font-weight: bold; font-size: {self._fs(10)}px;"
            )
            if self._coach_advice is None:
                self.coach_content_lbl.setText(ready_hint or "参谋就绪 · 死亡或按 F6 自动更新战术提示")
        else:
            self.pill_coach_dot.setStyleSheet("color: #5B5A56; font-size: 10px;")
            self.coach_status_lbl.setText(label or ("● 未登录" if self._commercial else "● 参谋未接入"))
            self._coach_status_color = "#A09B8C"
            self.coach_status_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #A09B8C; font-weight: bold; font-size: {self._fs(10)}px;"
            )
            if self._coach_advice is None:
                fallback_hint = (
                    "本地 5ms 大数据引擎接管中（登录后开启云端 AI）"
                    if self._commercial
                    else "本地 5ms 引擎接管中（可在客户端填入 Gemini Key）"
                )
                self.coach_content_lbl.setText(ready_hint or fallback_hint)

    def set_committed_plan(
        self,
        committed_plan: list[str] | tuple[str, ...],
        locked_next_item: str,
        overhaul_reason: str = "",
    ) -> None:
        self._committed_plan = list(committed_plan)
        self._locked_next_item = str(locked_next_item or "")
        self._overhaul_reason = overhaul_reason
        if overhaul_reason:
            self.rm_hdr_lbl.setText(f"⚡ {_clip_text(overhaul_reason, 14)}")
            self._rm_hdr_color = "#0AC8B9"
            self.rm_hdr_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #0AC8B9; font-weight: bold; font-size: {self._fs(10)}px;"
            )
        elif self._locked_next_item:
            self.rm_hdr_lbl.setText("◈ 本局锁定路线")
            self._rm_hdr_color = "#C8AA6E"
            self.rm_hdr_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: {self._fs(10)}px;"
            )
        else:
            self.rm_hdr_lbl.setText("◈ 出装路线")
            self._rm_hdr_color = "#C8AA6E"
            self.rm_hdr_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #C8AA6E; font-weight: bold; font-size: {self._fs(10)}px;"
            )

    def dismiss_opening_brief(self) -> None:
        """Hide the opening 10-player brief card for the remainder of the current match."""
        self._opening_brief_dismissed = True
        self.opening_brief_card.hide()
        if self._expanded and self.isVisible():
            self._reposition_right_edge()

    def reset_opening_brief_state(self) -> None:
        """Clear opening brief dismissal and content on new match start or match end."""
        self._opening_brief_dismissed = False
        self._opening_brief_seal_dismissed = False
        self._opening_brief_data = {}
        self.opening_brief_card.hide()
        self._sync_seal_slots()

    def _render_opening_brief(self) -> bool:
        """Render the collapsible opening 10-player brief card; returns True when shown."""
        if self._is_full_locked or self._opening_brief_dismissed or not self._opening_brief_data:
            self.opening_brief_card.hide()
            return False
        rows = (
            ("enemy_threat", self.opening_brief_threat_row, self.opening_brief_threat_lbl),
            ("augment_goal", self.opening_brief_augment_row, self.opening_brief_augment_lbl),
            ("opening_buy", self.opening_brief_buy_row, self.opening_brief_buy_lbl),
        )
        any_shown = False
        for key, row_w, val_lbl in rows:
            txt = (self._opening_brief_data.get(key) or "").strip()
            if txt:
                val_lbl.setText(txt)
                val_lbl.setToolTip(txt)
                row_w.show()
                any_shown = True
            else:
                val_lbl.setText("")
                val_lbl.setToolTip("")
                row_w.hide()
        if any_shown:
            self.opening_brief_card.show()
            return True
        self.opening_brief_card.hide()
        return False

    @staticmethod
    def _format_radar_summary(snapshot: LobbyScoutSnapshot) -> str:
        mvp = snapshot.mvp_card
        enemy = snapshot.top_enemy_card
        gap = f" (+{snapshot.mvp_lead_gap})" if snapshot.mvp_lead_gap > 0 else ""
        if mvp is not None and enemy is not None:
            same_player = bool(
                (mvp.puuid and mvp.puuid == enemy.puuid)
                or (mvp.team == enemy.team and mvp.champion_name == enemy.champion_name and mvp.hex_rating == enemy.hex_rating)
            )
            if same_player:
                ally_top = snapshot.my_team[0] if snapshot.my_team else None
                left = f"RANK 01 敌大腿 {enemy.champion_name} {enemy.hex_rating}{gap}"
                right = f" · 我方 {ally_top.champion_name} {ally_top.hex_rating}" if ally_top is not None else ""
                return left + right
            return f"RANK 01 {mvp.champion_name} {mvp.hex_rating}{gap} · 敌大腿 {enemy.champion_name} {enemy.hex_rating}"
        if mvp is not None:
            return f"RANK 01 {mvp.champion_name} {mvp.hex_rating}{gap}"
        if enemy is not None:
            return f"敌大腿 {enemy.champion_name} {enemy.hex_rating}"
        return ""

    def _rebuild_radar_detail_rows(self, snapshot: LobbyScoutSnapshot) -> None:
        if self._last_radar_detail_sig == snapshot:
            return
        self._last_radar_detail_sig = snapshot

        while self.radar_detail_lay.count():
            it = self.radar_detail_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        def _append_section(title: str, cards: tuple[PlayerScoutCard, ...], accent: str) -> None:
            if not cards:
                return
            fs9 = self._fs(9)
            sec_lbl = QLabel(title)
            sec_lbl.setStyleSheet(
                f"border: none; background: transparent; color: {accent}; font-size: {fs9}px; font-weight: bold;"
            )
            self.radar_detail_lay.addWidget(sec_lbl)
            sec_lbl.show()
            for card in cards[:5]:
                row = QWidget()
                row.setStyleSheet("background: transparent; border: none;")
                rl = QHBoxLayout(row)
                rl.setContentsMargins(self._px(2), 0, self._px(2), 0)
                rl.setSpacing(self._px(4))
                prefix = "★" if card.is_mvp_carry else "·"
                name_lbl = QLabel(f"{prefix}{_clip_text(card.champion_name, 6)}")
                name_lbl.setStyleSheet(
                    f"border: none; background: transparent; color: {'#FDE68A' if card.is_mvp_carry else '#E2E8F0'}; "
                    f"font-size: {fs9}px; font-weight: bold;"
                )
                meta_parts = [str(card.hex_rating)]
                if card.rank_label:
                    meta_parts.append(card.rank_label)
                if card.aram_games > 0:
                    meta_parts.append(f"{int(round(card.aram_wr * 100))}%")
                elif card.hidden_history:
                    meta_parts.append("隐")
                if card.streak_tag:
                    meta_parts.append(card.streak_tag)
                if card.premade_group:
                    meta_parts.append(card.premade_group)
                meta_lbl = QLabel(" · ".join(meta_parts))
                meta_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                meta_lbl.setStyleSheet(
                    f"border: none; background: transparent; color: #A09B8C; font-size: {fs9}px;"
                )
                rl.addWidget(name_lbl)
                rl.addStretch(1)
                rl.addWidget(meta_lbl)
                self.radar_detail_lay.addWidget(row)
                row.show()

        _append_section("我方战力", snapshot.my_team, "#0AC8B9")
        _append_section("敌方战力", snapshot.enemy_team, "#FCA5A5")

    def _toggle_radar_detail(self) -> None:
        self._radar_detail_expanded = not self._radar_detail_expanded
        self._sync_lobby_radar()

    def _dismiss_lobby_radar(self) -> None:
        self._radar_dismissed = True
        self._radar_detail_expanded = False
        self._sync_lobby_radar()

    def _sync_top_threat_badge(self, *, threat_chars: int | None = None) -> bool:
        """Render top_threat_lbl with priority: live fed KDA > auto-collapsed LobbyScout top enemy carry."""
        eff_w = max(self.MIN_EXPANDED_W, self._expanded_w)
        max_chars = threat_chars if threat_chars is not None else max(13, int((eff_w - 120) / 11.0))

        threat_txt = ""
        tooltip = ""
        if self._threat is not None and self._threat.top_threat_name:
            sp_tags = [s for s in self._threat.top_threat_spells if s in ("雪球", "虚弱", "净化", "点燃")]
            sp_suffix = f"/{sp_tags[0]}" if sp_tags else ""
            threat_txt = f"盯:{_clip_text(self._threat.top_threat_name, 5)} {self._threat.top_threat_kda}{sp_suffix}"
            full_sp = "+".join(self._threat.top_threat_spells) if self._threat.top_threat_spells else ""
            tooltip = f"敌方头号威胁：{self._threat.top_threat_name} ({self._threat.top_threat_kda}) {full_sp}".strip()
        elif (
            self._lobby_scout is not None
            and self._lobby_scout.top_enemy_card is not None
            and (self._match_game_time_s >= 90.0 or self._radar_dismissed)
        ):
            ec = self._lobby_scout.top_enemy_card
            threat_txt = f"敌大腿:{_clip_text(ec.champion_name, 6)} {ec.hex_rating}"
            detail_parts = [f"{ec.hex_rating}分"]
            if ec.rank_label:
                detail_parts.append(ec.rank_label)
            if ec.aram_games > 0:
                detail_parts.append(f"近{ec.aram_games}场胜率{int(round(ec.aram_wr * 100))}%")
            elif ec.hidden_history:
                detail_parts.append("隐藏战绩")
            if ec.streak_tag:
                detail_parts.append(ec.streak_tag)
            if ec.premade_group:
                detail_parts.append(ec.premade_group)
            tooltip = f"开局十人雷达 · 敌方最高战力：{ec.champion_name} ({' · '.join(detail_parts)})"

        clipped = _clip_text(threat_txt, max_chars) if threat_txt else ""
        has_threat = bool(clipped)
        sig = (clipped, tooltip, has_threat)
        if sig != self._last_top_threat_sig:
            self._last_top_threat_sig = sig
            if has_threat:
                if self.top_threat_lbl.text() != clipped:
                    self.top_threat_lbl.setText(clipped)
                if self.top_threat_lbl.toolTip() != tooltip:
                    self.top_threat_lbl.setToolTip(tooltip)
                if self.top_threat_lbl.isHidden():
                    self.top_threat_lbl.show()
            else:
                if self.top_threat_lbl.text():
                    self.top_threat_lbl.setText("")
                if not self.top_threat_lbl.isHidden():
                    self.top_threat_lbl.hide()

        want_micro = bool((not self.boots_hint_lbl.isHidden()) or has_threat)
        if self.tactical_micro_wrap.isHidden() == want_micro:
            self.tactical_micro_wrap.setVisible(want_micro)
        return has_threat

    def _sync_lobby_radar(self) -> None:
        snap = self._lobby_scout
        is_collapsed_by_time = self._match_game_time_s >= 90.0
        sig = (
            snap,
            is_collapsed_by_time,
            self._radar_dismissed,
            self._radar_detail_expanded,
            self._is_full_locked,
        )
        if sig == self._last_radar_sig:
            return
        self._last_radar_sig = sig

        was_card_visible = not self.radar_card.isHidden()
        was_detail_visible = not self.radar_detail_wrap.isHidden()

        summary_txt = self._format_radar_summary(snap) if snap is not None else ""
        show_card = bool(
            snap is not None
            and summary_txt
            and not self._is_full_locked
            and not self._radar_dismissed
            and not is_collapsed_by_time
        )

        if show_card and snap is not None:
            if self.radar_summary_lbl.text() != summary_txt:
                self.radar_summary_lbl.setText(summary_txt)
            tip_lines = [summary_txt]
            if snap.premade_notes:
                tip_lines.append("车队：" + " / ".join(snap.premade_notes))
            if snap.degraded_count > 0:
                tip_lines.append(f"注：{snap.degraded_count} 人隐藏战绩，已按段位估算")
            full_tip = "\n".join(tip_lines)
            if self.radar_summary_lbl.toolTip() != full_tip:
                self.radar_summary_lbl.setToolTip(full_tip)

            if snap.premade_notes:
                pm_short = _clip_text(snap.premade_notes[0], 10)
                if self.radar_premade_lbl.text() != pm_short:
                    self.radar_premade_lbl.setText(pm_short)
                pm_tip = " / ".join(snap.premade_notes)
                if self.radar_premade_lbl.toolTip() != pm_tip:
                    self.radar_premade_lbl.setToolTip(pm_tip)
                if self.radar_premade_lbl.isHidden():
                    self.radar_premade_lbl.show()
            elif not self.radar_premade_lbl.isHidden():
                self.radar_premade_lbl.hide()

            btn_txt = "收起" if self._radar_detail_expanded else "详情"
            if self.radar_detail_btn.text() != btn_txt:
                self.radar_detail_btn.setText(btn_txt)

            if self._radar_detail_expanded:
                self._rebuild_radar_detail_rows(snap)
                if self.radar_detail_wrap.isHidden():
                    self.radar_detail_wrap.show()
            elif not self.radar_detail_wrap.isHidden():
                self.radar_detail_wrap.hide()

            if self.radar_card.isHidden():
                self.radar_card.show()
        else:
            if not self.radar_detail_wrap.isHidden():
                self.radar_detail_wrap.hide()
            if not self.radar_card.isHidden():
                self.radar_card.hide()

        self._sync_top_threat_badge()

        now_card_visible = not self.radar_card.isHidden()
        now_detail_visible = not self.radar_detail_wrap.isHidden()
        if (was_card_visible != now_card_visible or was_detail_visible != now_detail_visible) and self._expanded and self.isVisible():
            self._reposition_right_edge()

    def set_lobby_scout(
        self,
        snapshot: LobbyScoutSnapshot | None,
        game_time_s: float | None = None,
    ) -> None:
        """Bind or clear the 10-player LobbyScout snapshot inside the existing right-edge drawer."""
        if game_time_s is not None:
            self._match_game_time_s = max(0.0, float(game_time_s))
        if snapshot is None:
            self._lobby_scout = None
            self._radar_dismissed = False
            self._radar_detail_expanded = False
            self._last_radar_detail_sig = None
        else:
            if self._lobby_scout is None:
                self._radar_dismissed = False
            self._lobby_scout = snapshot
        self._sync_lobby_radar()

    def set_coach_advice(
        self,
        advice: CoachAdvicePayload | None,
        *,
        allow_auto_expand: bool = True,
    ) -> None:
        self._coach_advice = advice
        self._coach_pending = False
        self.coach_shimmer.set_active(False)
        self.coach_shimmer.hide()
        if not self._ai_cutoff and not self._coach_loading:
            self.set_coach_online(
                self._coach_online,
                label=self._coach_online_label,
                ready_hint=self._coach_ready_hint,
            )
        if advice is None:
            self._opening_brief_data = {}
            self.opening_brief_card.hide()
            self._advice_gold_snapshot = None
            self._advice_items_snapshot = ()
            self._advice_received_mono = 0.0
            self.coach_time_lbl.setText("")
            if self._ai_cutoff:
                self._apply_cutoff_content_hint()
            self._render_decision_cards()
            return
        self._opening_brief_data = normalize_opening_brief(advice.opening_brief)
        shown_brief = self._render_opening_brief()
        self._advice_gold_snapshot = self._my_gold
        self._advice_items_snapshot = self._my_items
        self._advice_received_mono = time.monotonic()
        ts = advice.updated_at.split("T")[-1][:5] if "T" in advice.updated_at else advice.updated_at[:5]
        self.coach_time_lbl.setText(ts)
        self._render_decision_cards()
        self.coach_card.trigger_pulse("#0AC8B9", duration_ms=560)
        self.next_buy_card.trigger_pulse("#C8AA6E", duration_ms=480)
        if shown_brief:
            self.opening_brief_card.trigger_pulse("#0AC8B9", duration_ms=560)
            if allow_auto_expand and not self._expanded and not self._is_full_locked:
                self.set_expanded(True)
            elif self._expanded and self.isVisible():
                self._reposition_right_edge()
        elif self._expanded and self.isVisible():
            self._reposition_right_edge()

    def get_exclude_rect(self) -> tuple[int, int, int, int] | None:
        """Return physical screen bounding box of this drawer when visible so OCR never reads self UI."""
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

    def set_pending_transmute(self, pending: PendingTransmute | None, store: DataStore | None = None) -> None:
        """Show/hide secondary transmute recognition bar with rarity-filtered fallback selector."""
        self._pending_transmute = pending
        if pending is None or pending.remaining_count <= 0:
            self.transmute_bar.hide()
            if self._expanded and self.isVisible():
                self._reposition_right_edge()
            return

        done = len(pending.resolved_ids)
        total = max(1, pending.expected_count)
        rarity_zh = RARITY_COLORS.get(pending.target_rarity or "", ("", "任意阶"))[1]
        if done > 0 and store is not None:
            got_names = "+".join(
                (store.get_augment(a).name if store.get_augment(a) else a) for a in pending.resolved_ids
            )
            txt = f"⚡ {pending.source_name} ({done}/{total}) 已识:{_clip_text(got_names, 8)} · 悬停第{done + 1}个"
        else:
            txt = f"⚡ {pending.source_name} ({done}/{total}) · 悬停新符文图标自动识别"
        self.transmute_status_lbl.setText(txt)

        if store is not None:
            self.transmute_combo.clear()
            owned = set(self._selected_augments) | set(pending.resolved_ids)
            for aug in sorted(store.all_augments(), key=lambda a: a.name):
                if aug.augment_id in NON_TARGET_AUGMENT_IDS or aug.augment_id in owned:
                    continue
                if pending.target_rarity and aug.rarity != pending.target_rarity:
                    continue
                rlabel = RARITY_COLORS.get(aug.rarity, ("", ""))[1]
                self.transmute_combo.addItem(f"{aug.name} ({rlabel})", aug.augment_id)
            self.transmute_combo.setCurrentIndex(-1)
            if self.transmute_combo.lineEdit():
                self.transmute_combo.lineEdit().setPlaceholderText(f"手选{rarity_zh}质变结果...")

        self.transmute_bar.show()
        if self._expanded and self.isVisible():
            self._reposition_right_edge()

    def set_recent_offered(self, offered: list[AugmentInfo], auto_committed_id: str = "") -> None:
        """Surface 1-click quick-confirm OR 1-click swap strip for the last 3 offered cards."""
        self._recent_offered = list(offered)
        self._recent_auto_committed_id = auto_committed_id
        self._rebuild_quick_confirm()
        if self._expanded and self.isVisible():
            self._reposition_right_edge()

    def _rebuild_quick_confirm(self) -> None:
        while self.quick_confirm_lay.count():
            it = self.quick_confirm_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        if not self._recent_offered:
            self.quick_confirm_wrap.hide()
            return

        # Case A: An augment was auto-recorded -> show compact 1-click swap buttons for the OTHER 2 cards
        m = self._metrics()
        if self._recent_auto_committed_id and self._recent_auto_committed_id in self._selected_augments:
            alts = [a for a in self._recent_offered if a.augment_id != self._recent_auto_committed_id]
            if not alts:
                self.quick_confirm_wrap.hide()
                return
            lbl = QLabel("改选:")
            lbl.setStyleSheet(f"border: none; background: transparent; color: #A09B8C; font-size: {m.font_px(10)}px;")
            self.quick_confirm_lay.addWidget(lbl)
            for aug in alts[:2]:
                btn = QPushButton(_clip_text(aug.name, 7))
                btn.setFixedHeight(m.px(20, floor=18))
                btn.setToolTip(f"若自动记错，点此替换为【{aug.name}】")
                btn.setStyleSheet(
                    f"QPushButton {{ padding: 0px {m.px(6)}px; font-size: {m.font_px(10)}px; border: 1px solid #475569; color: #CBD5E1; }}"
                    "QPushButton:hover { border-color: #0AC8B9; color: #F0E6D2; }"
                )
                btn.clicked.connect(lambda _=False, aid=aug.augment_id: self._swap_last_augment(aid))
                self.quick_confirm_lay.addWidget(btn)
            self.quick_confirm_lay.addStretch(1)
            dismiss = QPushButton("✓")
            d_sz = m.px(20, floor=18)
            dismiss.setFixedSize(d_sz, d_sz)
            dismiss.setToolTip("确认无误，隐藏改选栏")
            dismiss.setStyleSheet(
                f"padding: 0px; font-size: {m.font_px(11)}px; color: #09C269; border: none; background: transparent;"
            )
            dismiss.clicked.connect(lambda: self.set_recent_offered([]))
            self.quick_confirm_lay.addWidget(dismiss)
            self.quick_confirm_wrap.show()
            return

        # Case B: Nothing recorded yet -> 1-click confirm buttons
        unrecorded = [a for a in self._recent_offered if a.augment_id not in self._selected_augments]
        if not unrecorded or len(unrecorded) < len(self._recent_offered) or len(self._selected_augments) >= 4:
            self.quick_confirm_wrap.hide()
            return
        lbl = QLabel("刚选了:")
        lbl.setStyleSheet(f"border: none; background: transparent; color: #FBBF24; font-size: {m.font_px(10)}px;")
        self.quick_confirm_lay.addWidget(lbl)
        shown_unrec = unrecorded[:4]
        clip_len = 5 if len(shown_unrec) >= 4 else 6
        for aug in shown_unrec:
            btn = QPushButton(_clip_text(aug.name, clip_len))
            btn.setFixedHeight(m.px(20, floor=18))
            btn.setToolTip(f"点选记录【{aug.name}】")
            btn.setStyleSheet(
                f"QPushButton {{ padding: 0px {m.px(5)}px; font-size: {m.font_px(10)}px; border: 1px solid #C89B3C; color: #F0E6D2; }}"
                "QPushButton:hover { border-color: #0AC8B9; background: rgba(10, 200, 185, 45); }"
            )
            btn.clicked.connect(lambda _=False, aid=aug.augment_id: self._quick_pick_augment(aid))
            self.quick_confirm_lay.addWidget(btn)
        self.quick_confirm_wrap.show()

    def _quick_pick_augment(self, aug_id: str) -> None:
        self._recent_offered = []
        self._recent_auto_committed_id = ""
        self.quick_confirm_wrap.hide()
        if self._on_select_augment:
            self._on_select_augment(aug_id)

    def _swap_last_augment(self, aug_id: str) -> None:
        self._recent_offered = []
        self._recent_auto_committed_id = ""
        self.quick_confirm_wrap.hide()
        if self._on_replace_last_augment:
            self._on_replace_last_augment(aug_id)

    def populate_catalogs(self, store: DataStore, datasets: list[DatasetDescriptor]) -> None:
        self._store_ref = store
        self._updating_combos = True
        try:
            self._datasets = list(datasets)
            self.dataset_combo.clear()
            for ds in datasets[:8]:
                disp = format_display_patch(ds.patch).split("（")[0]
                label = f"{disp} · {'首棱彩' if ds.mode == 'first-prismatic' else '常规'}"
                self.dataset_combo.addItem(label, (ds.patch, ds.mode, ""))
                if ds.bands:
                    self.dataset_combo.addItem(f"{disp} · 前5%高分段", (ds.patch, ds.mode, "high"))

            self.aug_add_combo.clear()
            for aug in sorted(store.all_augments(), key=lambda a: a.name):
                rlabel = RARITY_COLORS.get(aug.rarity, ("", ""))[1]
                self.aug_add_combo.addItem(f"{aug.name} ({rlabel})", aug.augment_id)
            self.aug_add_combo.setCurrentIndex(-1)

            self.hero_combo.clear()
            for h in sorted(store.all_heroes(), key=lambda x: x.name):
                self.hero_combo.addItem(h.name, h.hero_id)
            self.hero_combo.setCurrentIndex(-1)
        finally:
            self._updating_combos = False

    def refresh_match_view(
        self,
        store: DataStore,
        hero_id: str | None,
        selected_augments: list[str],
        my_items: tuple[str, ...],
        my_gold: float,
        threat: MatchupThreatProfile | None,
        dynamic_recs: list[DynamicItemRec],
        next_plan: NextBuyPlan | None = None,
    ) -> None:
        self._store_ref = store
        self._hero_id = hero_id
        self._selected_augments = list(selected_augments)
        self._my_items = tuple(my_items)
        self._my_gold = my_gold
        self._threat = threat
        self._dynamic_recs = list(dynamic_recs)
        self._next_plan = next_plan

        hero = store.get_hero(hero_id) if hero_id else None
        if hero:
            self.hero_lbl.setText(f"{hero.name} {hero.win_rate * 100:.1f}%")
            self.pill_hero_lbl.setText(hero.name[:1])
        else:
            self.hero_lbl.setText("未锁定英雄")
            self.pill_hero_lbl.setText("海")
        self.gold_lbl.setText(f"{int(my_gold)}G" if my_gold > 0 else "")
        aug_cap = max(4, len(selected_augments))
        self.pill_prog_lbl.setText(f"{len(selected_augments)}/{aug_cap}")
        self.aug_hdr_lbl.setText(f"◈ 海克斯 ({len(selected_augments)}/{aug_cap})")

        if threat and threat.summary_tags:
            short_tags = [t.replace("敌方", "敌").replace("头号威胁:", "盯:") for t in threat.summary_tags[:2]]
            self.threat_lbl.setText(" · ".join(short_tags))
        elif hero:
            self.threat_lbl.setText("均衡局势")
        else:
            self.threat_lbl.setText("待进局")

        self._render_decision_cards()
        self._render_augment_chips(store)
        self._rebuild_quick_confirm()

        if hero_id and DESTINY_AUGMENT_ID in selected_augments:
            d_recs = rank_destiny_items(store, hero_id, limit=2)
            if d_recs:
                d_lines = [
                    f"{'✓' if item_is_owned(d.item_id, my_items) else ''}{_short_item_name(d.name)} {d.win_rate * 100:.0f}%"
                    for d in d_recs
                ]
                self.destiny_body_lbl.setText("  |  ".join(d_lines))
                self._destiny_has_content = True
                self.destiny_card.setVisible(not self._is_full_locked)
            else:
                self._destiny_has_content = False
                self.destiny_card.hide()
        else:
            self._destiny_has_content = False
            self.destiny_card.hide()

        if self._commercial and self._lifecycle is not None and self._lifecycle.is_sealed_dormant:
            self._sync_seal_slots()
        if self.isVisible():
            self._reposition_right_edge()

    def _find_item_id_by_name(self, name: str) -> str:
        if self._store_ref is None or not name:
            return ""
        for iid, info in self._store_ref.static.items.items():
            if info.name == name:
                return iid
        return ""

    def _render_decision_cards(self) -> None:
        if self._is_full_locked:
            self.radar_card.hide()
            self.opening_brief_card.hide()
            self.next_buy_card.hide()
            self.roadmap_card.hide()
            self.destiny_card.hide()
            self.aug_frame.hide()
            self.pill_item_ring.set_state(load_app_pixmap(28), progress=0.0, ready=False, fallback_text="锁")
            if self._cutoff_code == "SERVICE_LOCKED":
                self.pill_gold_lbl.setText("维护")
                self.pill_gold_lbl.setStyleSheet("border: none; background: transparent; color: #F87171; font-size: 9px; font-weight: bold;")
                self.pill_item_ring.setToolTip(self._cutoff_reason or "服务维护升级中，功能暂不可用")
            elif self._cutoff_code == "CLIENT_UPDATE_REQUIRED":
                self.pill_gold_lbl.setText("更新")
                self.pill_gold_lbl.setStyleSheet("border: none; background: transparent; color: #F87171; font-size: 9px; font-weight: bold;")
                self.pill_item_ring.setToolTip(self._cutoff_reason or "当前版本已停用，请立即更新至最新版本")
            else:
                self.pill_gold_lbl.setText("锁定")
                self.pill_gold_lbl.setStyleSheet("border: none; background: transparent; color: #F59E0B; font-size: 9px; font-weight: bold;")
                self.pill_item_ring.setToolTip("当前策略要求登录或充值后解锁全部功能")
            return

        store = self._store_ref
        adv = self._coach_advice
        plan = self._next_plan

        # Determine if Coach's buy_now text is still fresh for the EXACT current gold & items
        gold_drifted = (
            self._advice_gold_snapshot is not None
            and abs(self._my_gold - self._advice_gold_snapshot) >= 120.0
        )
        items_changed = bool(self._advice_items_snapshot and self._my_items != self._advice_items_snapshot)
        advice_stale = gold_drifted or items_changed

        inv = analyze_inventory_slots(store, self._my_items) if store is not None else None
        is_full_build = bool((inv is not None and inv.is_full_build) or (plan is not None and plan.is_full_build))

        def _is_justified_swap_or_elixir(it_obj) -> bool:
            iid_n = normalize_item_id(it_obj.item_id or self._find_item_id_by_name(it_obj.name))
            if iid_n in ELIXIR_IDS:
                return True
            p = it_obj.priority or ""
            r = it_obj.reason or ""
            return ("换" in p) or ("卖" in p) or ("卖" in r) or ("换" in r) or ("替换" in r)

        coach_primary = None
        coach_backup = None
        if adv and adv.next_items:
            unowned = [it for it in adv.next_items if not (it.item_id and item_is_owned(it.item_id, self._my_items))]
            if is_full_build:
                # At 6-slot full build, reject casual/stale 7th major items without explicit swap justification
                unowned = [it for it in unowned if _is_justified_swap_or_elixir(it)]
            if unowned:
                # Honor committed locked_next_item if present in coach list (prevents routine death flip-flopping!)
                locked_norm = normalize_item_id(self._locked_next_item) if (self._locked_next_item and not is_full_build) else ""
                if locked_norm and not item_is_owned(locked_norm, self._my_items):
                    match_idx = next(
                        (
                            i
                            for i, it in enumerate(unowned)
                            if normalize_item_id(it.item_id or self._find_item_id_by_name(it.name)) == locked_norm
                        ),
                        None,
                    )
                    if match_idx is not None:
                        coach_primary = unowned[match_idx]
                        rest = [it for i, it in enumerate(unowned) if i != match_idx]
                        coach_backup = rest[0] if rest else None
                    else:
                        # Locked item is still unowned; keep showing locked target from committed plan if store knows it
                        if store is not None:
                            linfo = store.get_item_info(locked_norm)
                            if linfo.name and linfo.gold > 0:
                                from hex_helper.bridge.protocol import CoachAdviceItem

                                coach_primary = CoachAdviceItem(
                                    item_id=locked_norm,
                                    name=linfo.name,
                                    priority="锁定下件",
                                    reason="延续本局既定路线",
                                )
                                coach_backup = unowned[0]
                if coach_primary is None:
                    coach_primary = unowned[0]
                    coach_backup = unowned[1] if len(unowned) > 1 else None

        target_id = ""
        target_name = ""
        pill_eff_cost = 0
        pill_aff = False
        pill_gap = 0

        eff_w = max(self.MIN_EXPANDED_W, self._expanded_w)
        norm_w = max(288.0, float(eff_w) / max(0.85, self._ui_scale))
        reason_chars = max(16, int((norm_w - 104.0) / 9.5))
        buy_chars = max(26, int((norm_w - 36.0) / 8.0))
        boots_chars = max(12, int((norm_w - 130.0) / 11.5))
        threat_chars = max(13, int((norm_w - 120.0) / 11.0))
        coach_tip_chars = max(22, int((norm_w - 48.0) / 10.5))
        coach_sum_chars = max(36, int((norm_w - 30.0) / 7.0))
        self.next_source_lbl.setVisible(eff_w >= 312)

        if coach_primary is not None:
            target_id = coach_primary.item_id or self._find_item_id_by_name(coach_primary.name)
            target_name = coach_primary.name
            target_norm = normalize_item_id(target_id)
            eff_cost, eff_aff, eff_gap, live_hint = (
                compute_item_buy_now(store, target_id, self._my_items, self._my_gold)
                if (store and target_id)
                else (0, False, 0, "")
            )
            pill_eff_cost, pill_aff, pill_gap = eff_cost, eff_aff, eff_gap
            if is_full_build and target_norm in ELIXIR_IDS:
                self.next_item_title_lbl.setText(f"✓ 六神满格 · {target_name}")
            elif is_full_build:
                self.next_item_title_lbl.setText(f"⇄ 建议换装 · {target_name}")
            else:
                self.next_item_title_lbl.setText(f"▶ {target_name}")
            self._set_cost_badge(eff_cost, eff_aff, eff_gap)
            self.next_source_lbl.setText("参谋")
            self.next_reason_lbl.setText(_clip_text(coach_primary.reason, reason_chars))
            self.next_reason_lbl.setToolTip(coach_primary.reason)

            # Use Coach buy_now ONLY if freshly matched to current gold/items & primary is still #1;
            # otherwise recompute at 0ms from live gold so shop advice NEVER lags behind!
            use_coach_buy_now = bool(
                adv.buy_now
                and not advice_stale
                and adv.next_items
                and coach_primary is adv.next_items[0]
            )
            buy_txt = adv.buy_now if use_coach_buy_now else (live_hint or f"目标：{target_name}")
            self.buy_now_lbl.setText(f"💰 {_clip_text(buy_txt, buy_chars)}")
            self.buy_now_lbl.setToolTip(buy_txt)

            if coach_backup is not None:
                self.backup_item_lbl.setText(f"备选：{_short_item_name(coach_backup.name)}")
                self.backup_item_lbl.setToolTip(f"{coach_backup.name}：{coach_backup.reason}")
            else:
                self.backup_item_lbl.setText("")
        elif plan is not None:
            target_id = plan.target.item_id
            target_name = plan.target.name
            pill_eff_cost, pill_aff, pill_gap = plan.effective_cost, plan.affordable_now, plan.gold_gap
            if is_full_build and normalize_item_id(target_id) in ELIXIR_IDS:
                self.next_item_title_lbl.setText(f"✓ 六神满格 · {target_name}")
            else:
                self.next_item_title_lbl.setText(f"▶ {target_name}")
            self._set_cost_badge(plan.effective_cost, plan.affordable_now, plan.gold_gap)
            self.next_source_lbl.setText("本地")
            reason_raw = "；".join(plan.target.reasons)
            self.next_reason_lbl.setText(_clip_text(reason_raw, reason_chars))
            self.next_reason_lbl.setToolTip(reason_raw)
            self.buy_now_lbl.setText(f"💰 {_clip_text(plan.buy_now_hint, buy_chars)}")
            self.buy_now_lbl.setToolTip(plan.buy_now_hint)
            if plan.backup is not None:
                lbl_prefix = "卖鞋可换：" if is_full_build else "备选："
                self.backup_item_lbl.setText(f"{lbl_prefix}{_short_item_name(plan.backup.name)}")
                self.backup_item_lbl.setToolTip(f"{plan.backup.name}（{plan.backup.price}G）")
            else:
                self.backup_item_lbl.setText("")
        else:
            self.next_item_title_lbl.setText("▶ 等待锁定英雄")
            self.next_cost_badge.hide()
            self.next_source_lbl.setText("待机")
            self.next_reason_lbl.setText("进局自动给出唯一优先出装")
            self.buy_now_lbl.setText("◇ 进局后显示当前金币立刻买什么件")
            self.backup_item_lbl.setText("")

        # Update Scaled Hero Item Icon + Collapsed Live Pill Gold-Readiness Ring (0ms sync!)
        hero_ico_px = max(24, self.next_icon_lbl.width() - 4)
        pm = self._get_item_pixmap(target_id, hero_ico_px) if target_id else None
        if pm is not None:
            self.next_icon_lbl.setPixmap(pm)
            self.next_icon_lbl.setText("")
        else:
            self.next_icon_lbl.setPixmap(QPixmap())
            self.next_icon_lbl.setText(target_name[:1] if target_name else "?")

        pill_pm = self._get_item_pixmap(target_id, 30) if target_id else None
        if pill_eff_cost > 0:
            prog = 1.0 if pill_aff else max(0.0, min(0.98, (pill_eff_cost - pill_gap) / float(pill_eff_cost)))
            self.pill_item_ring.set_state(pill_pm, progress=prog, ready=pill_aff, fallback_text=_short_item_name(target_name))
            if pill_aff:
                p_txt = "可买" if not is_full_build else "合剂"
                p_qss = "border: none; background: transparent; color: #6EE7B7; font-size: 9px; font-weight: bold;"
            else:
                p_txt = f"-{pill_gap}"
                p_qss = "border: none; background: transparent; color: #FDE68A; font-size: 9px; font-weight: bold;"
            if self.pill_gold_lbl.text() != p_txt:
                self.pill_gold_lbl.setText(p_txt)
            if self.pill_gold_lbl.styleSheet() != p_qss:
                self.pill_gold_lbl.setStyleSheet(p_qss)
            self.pill_item_ring.setToolTip(f"下件：{target_name}（{'可直买' if pill_aff else f'差 {pill_gap}G'}）")
        else:
            self.pill_item_ring.set_state(load_app_pixmap(28), progress=0.0, ready=False, fallback_text="海")
            p_qss = "border: none; background: transparent; color: #A09B8C; font-size: 9px;"
            if self.pill_gold_lbl.text() != "待机":
                self.pill_gold_lbl.setText("待机")
            if self.pill_gold_lbl.styleSheet() != p_qss:
                self.pill_gold_lbl.setStyleSheet(p_qss)

        # Build Visual Icon Roadmap: show ALL owned complete items (+ Tier-2 boots at 5+1 full build!)
        steps_visual: list[tuple[str, str, str]] = []
        seen_names: set[str] = set()
        if store is not None:
            for iid in self._my_items:
                info = store.get_item_info(iid)
                if (info.category == "complete_equipment" or info.gold >= 2200) and info.name not in seen_names:
                    seen_names.add(info.name)
                    steps_visual.append((normalize_item_id(iid), info.name, "owned"))
            if inv is not None and inv.is_full_build_with_boots and inv.tier2_boots_id and inv.tier2_boots_name not in seen_names:
                seen_names.add(inv.tier2_boots_name)
                steps_visual.append((inv.tier2_boots_id, inv.tier2_boots_name, "owned"))

        def _append_rem(iid_raw: str, nm_raw: str) -> None:
            nm_clean = (nm_raw or "").strip()
            if not nm_clean or nm_clean.startswith("装备#"):
                return
            if nm_clean in seen_names or any(n == nm_clean for _, n in rem_pairs):
                return
            nid = normalize_item_id(iid_raw) if iid_raw else self._find_item_id_by_name(nm_clean)
            if store is not None and nid:
                if store.static.items and nid not in store.static.items:
                    return
                info = store.get_item_info(nid)
                if info.name.startswith("装备#"):
                    return
                if info.category in ("consumable", "boots") or (info.category != "complete_equipment" and info.gold < 2200):
                    return
                nm_clean = info.name
                if nm_clean in seen_names or any(n == nm_clean for _, n in rem_pairs):
                    return
            rem_pairs.append((nid, nm_clean))

        rem_pairs: list[tuple[str, str]] = []
        if not is_full_build:
            if self._committed_plan and store is not None:
                for iid in self._committed_plan:
                    if not item_is_owned(iid, self._my_items):
                        _append_rem(iid, store.get_item_info(iid).name)
            if adv and adv.build_path:
                for nm in adv.build_path:
                    _append_rem("", nm)
            elif plan is not None:
                ids = plan.remaining_build_ids
                for idx, nm in enumerate(plan.remaining_build_path):
                    iid = ids[idx] if idx < len(ids) else ""
                    _append_rem(iid, nm)
            elif coach_primary is not None and adv is not None:
                for it in adv.next_items:
                    _append_rem(it.item_id or "", it.name)

        # Fit full 6-slot build on ONE row: at 6/6 full build show all 6 owned slots & ZERO fake 7th item!
        if is_full_build:
            final_steps: list[tuple[str, str, str]] = list(steps_visual[:6])
            self.rm_hdr_lbl.setText("◈ 六神已毕业 (6/6)")
            self._rm_hdr_color = "#09C269"
            self.rm_hdr_lbl.setStyleSheet(
                f"border: none; background: transparent; color: #09C269; font-weight: bold; font-size: {self._fs(10)}px;"
            )
        else:
            final_steps = list(steps_visual[:5])
            max_future = max(1, 6 - len(final_steps))
            for idx, (iid, nm) in enumerate(rem_pairs[:max_future]):
                final_steps.append((iid, nm, "next" if idx == 0 else "future"))

        # Explicit build-pivot comparison strip inside roadmap_card (signature-deduplicated)
        pivot_txt = (
            plan.pivot_banner_text.strip()
            if (plan is not None and not is_full_build and plan.pivot_banner_text)
            else ""
        )
        if pivot_txt != self._last_pivot_banner_text:
            self._last_pivot_banner_text = pivot_txt
            if pivot_txt:
                self.pivot_banner_lbl.setText(pivot_txt)
                self.pivot_banner_lbl.setToolTip(pivot_txt)
                if self.pivot_banner_lbl.isHidden():
                    self.pivot_banner_lbl.show()
            else:
                self.pivot_banner_lbl.setText("")
                self.pivot_banner_lbl.setToolTip("")
                if not self.pivot_banner_lbl.isHidden():
                    self.pivot_banner_lbl.hide()

        # Update text mirror for tests & accessibility
        text_tokens = [
            f"✓ {nm}" if st == "owned" else (f"【▶ {nm}】" if st == "next" else nm)
            for _iid, nm, st in (steps_visual + [(i, n, "next" if k == 0 else "future") for k, (i, n) in enumerate(rem_pairs[:4])])
        ]
        self.roadmap_body_lbl.setText("  ›  ".join(text_tokens))
        self._rebuild_roadmap_icons(final_steps)

        # Update compact tactical micro-bar (counter Tier-2 boots + top enemy threat KDA/spells or collapsed radar)
        boots_txt = ""
        if plan is not None and not is_full_build and plan.recommended_boots_name:
            short_boot = _short_item_name(plan.recommended_boots_name)
            boots_txt = f"鞋:{short_boot}({plan.recommended_boots_reason})" if plan.recommended_boots_reason else f"鞋:{short_boot}"
        if boots_txt:
            clipped_boots = _clip_text(boots_txt, boots_chars)
            if self.boots_hint_lbl.text() != clipped_boots:
                self.boots_hint_lbl.setText(clipped_boots)
            boots_tip = f"推荐二级鞋：{plan.recommended_boots_name}（{plan.recommended_boots_reason}）"
            if self.boots_hint_lbl.toolTip() != boots_tip:
                self.boots_hint_lbl.setToolTip(boots_tip)
            if self.boots_hint_lbl.isHidden():
                self.boots_hint_lbl.show()
        elif not self.boots_hint_lbl.isHidden():
            self.boots_hint_lbl.hide()

        has_threat = self._sync_top_threat_badge(threat_chars=threat_chars)
        want_micro = bool(boots_txt or has_threat)
        if self.tactical_micro_wrap.isHidden() == want_micro:
            self.tactical_micro_wrap.setVisible(want_micro)

        # Ultra-concise Coach Tactical Tip (ZERO stale 3-card tips after picker closed!)
        if adv is not None:
            lines: list[str] = []
            if self._picker_active and adv.augment_pick_advice:
                lines.append(f"【三选一】{_clip_text(adv.augment_pick_advice, coach_tip_chars)}")
            if adv.tactical_summary:
                lines.append(_clip_text(adv.tactical_summary, coach_sum_chars))
            elif not lines and adv.next_items:
                lines.append(" · ".join(_short_item_name(it.name) for it in adv.next_items[:3]))
            txt = "\n".join(lines) if lines else "参谋已同步当前局势"
            self.coach_content_lbl.setText(txt)
            self.coach_content_lbl.setToolTip(adv.tactical_summary or adv.augment_pick_advice or txt)

    def _set_cost_badge(self, eff_cost: int, affordable: bool, gap: int) -> None:
        self._cost_badge_state = (eff_cost, affordable, gap) if eff_cost > 0 else None
        if eff_cost <= 0:
            if not self.next_cost_badge.isHidden():
                self.next_cost_badge.hide()
            return
        m = self._metrics()
        if affordable:
            txt = f"可直买 {eff_cost}G"
            qss = (
                "background-color: rgba(9, 194, 105, 45); border: 1px solid #09C269; "
                f"border-radius: 2px; padding: {m.px(1)}px {m.px(5)}px; color: #6EE7B7; "
                f"font-size: {m.font_px(10)}px; font-weight: bold;"
            )
        else:
            txt = f"差 {gap}G"
            qss = (
                "background-color: rgba(200, 155, 60, 40); border: 1px solid #C89B3C; "
                f"border-radius: 2px; padding: {m.px(1)}px {m.px(5)}px; color: #FDE68A; "
                f"font-size: {m.font_px(10)}px; font-weight: bold;"
            )
        if self.next_cost_badge.text() != txt:
            self.next_cost_badge.setText(txt)
        if self.next_cost_badge.styleSheet() != qss:
            self.next_cost_badge.setStyleSheet(qss)
        if self.next_cost_badge.isHidden():
            self.next_cost_badge.show()

    def _rebuild_roadmap_icons(self, steps: list[tuple[str, str, str]]) -> None:
        sig = (round(self._ui_scale, 2), self._expanded_w, tuple(steps))
        if sig == self._last_roadmap_sig:
            return
        self._last_roadmap_sig = sig

        while self.roadmap_icons_lay.count():
            it = self.roadmap_icons_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        m = self._metrics()
        if not steps:
            empty = QLabel("◇ 进局后生成图标路线")
            empty.setStyleSheet(
                f"border: none; background: transparent; color: #64748B; font-size: {m.font_px(10)}px;"
            )
            self.roadmap_icons_lay.addWidget(empty)
            empty.show()
            return

        n_steps = len(steps)
        compact_6 = n_steps >= 6
        bucket = self._width_bucket(self._expanded_w)
        cap_len = 2 if bucket == 0 else (4 if self._ui_scale >= 1.35 else (5 if bucket == 2 else 3))

        # Compute guaranteed non-overflowing icon pixel budget for the current expanded width & scale
        avail_w = max(180, self._expanded_w - m.px(22) - m.px(16) - m.px(6))
        sep_w = m.px(9, floor=7)
        sp = m.px(3, floor=2)
        cell_budget = max(22, int((avail_w - max(0, n_steps - 1) * (sep_w + 2 * sp)) / max(1, n_steps)))

        for idx, (iid, name, state) in enumerate(steps):
            if idx > 0:
                sep = QLabel("›")
                sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
                sep.setStyleSheet(
                    f"border: none; background: transparent; color: #785A28; "
                    f"font-size: {m.font_px(12)}px; font-weight: bold;"
                )
                self.roadmap_icons_lay.addWidget(sep)
                sep.show()

            cell = QWidget()
            cell.setStyleSheet("background: transparent; border: none;")
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(m.px(2))

            base_ico = (32 if state == "next" else 28) if (compact_6 and self._ui_scale >= 1.2) else (
                (28 if state == "next" else 24) if compact_6 else (32 if state == "next" else 28)
            )
            if bucket == 0:
                base_ico = 24 if state == "next" else 20
            desired_ico = m.px(base_ico, floor=18)
            ico_size = max(18, min(desired_ico, cell_budget - 2))
            ico = QLabel()
            ico.setFixedSize(ico_size, ico_size)
            ico.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = self._get_item_pixmap(iid, max(14, ico_size - 4))
            if pm is not None:
                ico.setPixmap(pm)
            else:
                ico.setText(_short_item_name(name)[:2])

            if state == "owned":
                border = "1px solid #09C269"
                cap_color = "#6EE7B7"
                cap_txt = f"✓{_short_item_name(name)[:cap_len]}"
                tip = f"✓ 已购：{name}"
            elif state == "next":
                border = "2px solid #0AC8B9"
                cap_color = "#F0E6D2"
                cap_txt = f"▶{_short_item_name(name)[:cap_len]}"
                tip = f"▶ 下一件：{name}"
            else:
                border = "1px solid #463714"
                cap_color = "#A09B8C"
                cap_txt = _short_item_name(name)[: cap_len + 1]
                tip = f"后续：{name}"

            ico.setStyleSheet(
                f"background-color: #091428; border: {border}; border-radius: 2px; "
                f"color: #C8AA6E; font-size: {m.font_px(9)}px; font-weight: bold;"
            )
            ico.setToolTip(tip)

            cap = QLabel(cap_txt)
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cap.setStyleSheet(
                f"border: none; background: transparent; color: {cap_color}; "
                f"font-size: {m.font_px(9)}px; {'font-weight: bold;' if state == 'next' else ''}"
            )
            cap.setToolTip(tip)

            cl.addWidget(ico, alignment=Qt.AlignmentFlag.AlignCenter)
            cl.addWidget(cap)
            self.roadmap_icons_lay.addWidget(cell)
            cell.show()

        self.roadmap_icons_lay.addStretch(1)

    def _render_augment_chips(self, store: DataStore) -> None:
        # Sync collapsed pill 4 rarity dots
        for i, dot in enumerate(self._pill_aug_dots):
            if i < len(self._selected_augments):
                aid_d = self._selected_augments[i]
                aug_d = store.get_augment(aid_d)
                dc = "#E879F9" if is_transmute_augment(aid_d) else RARITY_COLORS.get(aug_d.rarity if aug_d else "gold", ("#C89B3C", ""))[0]
                dot.setStyleSheet(f"background-color: {dc}; border: none; border-radius: 3px;")
            else:
                dot.setStyleSheet("background-color: #334155; border: none; border-radius: 3px;")

        while self.aug_chips_lay.count():
            it = self.aug_chips_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        m = self._metrics()
        if not self._selected_augments:
            empty = QLabel("◇ 选卡后自动记入")
            empty.setStyleSheet(
                f"border: none; background: transparent; color: #64748B; font-size: {m.font_px(10)}px;"
            )
            self.aug_chips_lay.addWidget(empty, 0, 0, 1, 2)
            empty.show()
            return

        sp_db = get_special_augment_db()
        cols = 3 if self._expanded_w >= 450 else 2
        if self._expanded_w < 330:
            max_chars = 6
        elif self._expanded_w >= 450:
            max_chars = 8 if self._ui_scale >= 1.3 else 9
        else:
            max_chars = 9 if len(self._selected_augments) <= 2 else 7
        for idx, aid in enumerate(self._selected_augments):
            aug = store.get_augment(aid)
            name = aug.name if aug else aid
            is_tx = is_transmute_augment(aid)
            sp = sp_db.get_by_id(aid)
            if is_tx:
                disp_name = f"⚡{_clip_text(name.replace('质变：', '质变'), max_chars)}"
            elif sp is not None and sp.transformative:
                disp_name = f"✦{_clip_text(name.replace('，', '').replace('：', ''), max_chars)}"
            else:
                disp_name = _clip_text(name.replace("，", "").replace("：", ""), max_chars)
            color = "#E879F9" if is_tx else RARITY_COLORS.get(aug.rarity if aug else "gold", ("#C89B3C", ""))[0]
            bg_css = "rgba(58, 20, 74, 230)" if is_tx else ("rgba(10, 40, 52, 230)" if (sp and sp.transformative) else "rgba(15, 25, 42, 220)")
            chip = QFrame()
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            chip.setStyleSheet(
                f"QFrame {{ background-color: {bg_css}; border: 1px solid {color}; border-radius: 2px; }}"
            )
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(m.px(6), m.px(2), m.px(4), m.px(2))
            cl.setSpacing(m.px(3))
            lbl = QLabel(disp_name)
            lbl.setMinimumWidth(0)
            if is_tx:
                tip_txt = f"{name}（等待二次识别质变结果）"
            elif sp is not None:
                tip_txt = f"{name} · {sp.archetype}\n{sp.playstyle_tip}"
            else:
                tip_txt = name
            lbl.setToolTip(tip_txt)
            lbl.setStyleSheet(
                f"border: none; background: transparent; color: {color}; font-weight: bold; font-size: {m.font_px(10)}px;"
            )
            rm = QPushButton("×")
            rm_sz = m.px(14, floor=12)
            rm.setFixedSize(rm_sz, rm_sz)
            rm.setToolTip(f"移除 {name}")
            rm.setStyleSheet(
                f"padding: 0px; font-size: {m.font_px(11)}px; color: #A09B8C; background: transparent; border: none;"
            )
            rm.clicked.connect(lambda _=False, i=idx: self._on_remove_augment and self._on_remove_augment(i))
            cl.addWidget(lbl, 1)
            cl.addWidget(rm)
            self.aug_chips_lay.addWidget(chip, idx // cols, idx % cols)
            chip.show()
        for col_i in range(3):
            self.aug_chips_lay.setColumnStretch(col_i, 1 if col_i < cols else 0)

    def _handle_manual_resolve_transmute(self) -> None:
        idx = self.transmute_combo.currentIndex()
        aid = self.transmute_combo.itemData(idx) if idx >= 0 else None
        if not aid:
            txt = self.transmute_combo.currentText().strip()
            if txt and self._store_ref is not None:
                clean = txt.split(" (")[0].strip()
                for aug in self._store_ref.all_augments():
                    if aug.name == clean:
                        aid = aug.augment_id
                        break
        if aid and self._on_resolve_transmute:
            self._on_resolve_transmute(str(aid))

    def _handle_dismiss_transmute(self) -> None:
        self.transmute_bar.hide()
        if self._on_dismiss_transmute:
            self._on_dismiss_transmute()

    def _handle_manual_add_augment(self) -> None:
        idx = self.aug_add_combo.currentIndex()
        if idx >= 0:
            aid = self.aug_add_combo.itemData(idx)
            if aid and self._on_select_augment:
                self._on_select_augment(str(aid))
                self.aug_manual_row.hide()

    def _handle_call_coach(self) -> None:
        if self._on_call_coach:
            self._on_call_coach()

    def _handle_dataset_changed(self, idx: int) -> None:
        if self._updating_combos or idx < 0:
            return
        data = self.dataset_combo.itemData(idx)
        if data and self._on_switch_dataset:
            patch, mode, band = data
            self._on_switch_dataset(patch, mode, band)

    def _handle_manual_hero(self, idx: int) -> None:
        if self._updating_combos or idx < 0:
            return
        hid = self.hero_combo.itemData(idx)
        if hid and self._on_manual_hero:
            self._on_manual_hero(str(hid))
