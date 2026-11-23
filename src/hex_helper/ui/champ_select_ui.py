"""Champion Select floating overlay styled after League of Legends V16.23 Hextech Client."""

from __future__ import annotations

from collections.abc import Callable
import threading

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QRectF, Qt
from PySide6.QtGui import QAction, QColor, QGuiApplication, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hex_helper.config import format_display_patch
from hex_helper.data.aram_balance import get_aram_balance, get_hero_ceiling_tag
from hex_helper.data.store import DataStore
from hex_helper.game.models import ChampSelectState
from hex_helper.scoring.tier import score_hero_tier
from hex_helper.ui.resizable_overlay import FramelessResizeController, HextechCornerGrip
from hex_helper.ui.theme import BASE_OVERLAY_QSS, load_app_pixmap, tier_badge_qss
from hex_helper.ui.win32_window import (
    UiBridgeInvoker,
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
    """Classify a champion's primary ARAM profile ('AP' | 'AD' | 'TANK') from top 26.23 core builds."""
    ad_s = 0.0
    ap_s = 0.0
    tank_s = 0.0
    for cb in store.get_core_builds(hero_id)[:2]:
        for iid in cb.items:
            info = store.get_item_info(iid)
            st = info.stats or {}
            ad_s += float(st.get("FlatPhysicalDamageMod", 0.0)) * 1.4
            ad_s += float(st.get("FlatCritChanceMod", 0.0)) * 110.0
            ap_s += float(st.get("FlatMagicDamageMod", 0.0)) * 1.15
            tank_s += float(st.get("FlatArmorMod", 0.0)) * 1.45
            tank_s += float(st.get("FlatSpellBlockMod", 0.0)) * 1.45
            tank_s += float(st.get("FlatHPPoolMod", 0.0)) * 0.14
    if tank_s > max(ad_s, ap_s) * 1.15 and tank_s >= 110.0:
        return "TANK"
    if ap_s >= ad_s and ap_s >= 40.0:
        return "AP"
    if ad_s > ap_s and ad_s >= 35.0:
        return "AD"
    return "AP"


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
    ) -> None:
        super().__init__()
        self._on_pos_changed = on_pos_changed
        self._on_size_changed = on_size_changed
        self._on_core_action = on_core_action
        self._ai_bar_action: str = ""
        self._drag_offset: QPoint | None = None
        self._icon_cache: dict[tuple[str, str], QPixmap] = {}
        self._fetching: set[str] = set()
        self._icon_invoker = UiBridgeInvoker()
        self._last_state: ChampSelectState | None = None
        self._last_store: DataStore | None = None
        self._fade_anim: QPropertyAnimation | None = None
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
        close_btn.clicked.connect(self.hide)
        header.addWidget(self.icon_lbl)
        header.addWidget(self.title_lbl)
        header.addStretch(1)
        header.addWidget(self.patch_lbl)
        header.addWidget(close_btn)
        self.layout_main.addLayout(header)

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
        self._grip_bl = HextechCornerGrip(self.root, corner="bl")
        self._grip_br = HextechCornerGrip(self.root, corner="br")
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

    def _on_live_resize(self, target: QRect, _edge: str) -> None:
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
                self.update_state(self._last_state, self._last_store)
                self.setGeometry(target.x(), target.y(), w, h)
                self._layout_corner_grips()

    def _on_resize_finished(self, target: QRect, _edge: str) -> None:
        self._custom_w = max(self.MIN_W, min(self.MAX_W, target.width()))
        self._custom_h = max(self.MIN_H, target.height())
        new_bucket = self._width_bucket(self._custom_w)
        if new_bucket != self._last_density_bucket:
            self._last_density_bucket = new_bucket
            if self._last_state is not None and self._last_store is not None:
                self.update_state(self._last_state, self._last_store)
        self._layout_corner_grips()
        if self._on_size_changed:
            self._on_size_changed([self._custom_w, self._custom_h])
        if self._on_pos_changed:
            self._on_pos_changed([self.x(), self.y()])

    def reset_default_size(self) -> None:
        self._custom_w = None
        self._custom_h = None
        self._last_density_bucket = self._width_bucket(self.DEFAULT_W)
        self._apply_header_density(self.DEFAULT_W)
        self.setFixedWidth(self.DEFAULT_W)
        if self._last_state is not None and self._last_store is not None:
            self.update_state(self._last_state, self._last_store)
        else:
            self.rows_scroll.setMinimumHeight(64)
            self.rows_scroll.setMaximumHeight(self._QWIDGETSIZE_MAX)
            self.setFixedHeight(320)
        self._layout_corner_grips()
        if self._on_size_changed:
            self._on_size_changed(None)

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

    def _refresh_last_state(self) -> None:
        if self._last_state is not None and self._last_store is not None and self.isVisible():
            self.update_state(self._last_state, self._last_store)

    def update_state(self, state: ChampSelectState, store: DataStore) -> None:
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
        comp_txt, is_warn, needed_roles = analyze_team_needs(ally_ids, store)

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
            compact: bool = False,
        ) -> QFrame | None:
            h = store.get_hero(hid)
            if h is None:
                return None
            wr, name, rank, games = h.win_rate, h.name, h.rank, h.games
            row = QFrame()
            row.setObjectName("SubCard")
            if is_mine:
                row.setStyleSheet(
                    "QFrame#SubCard { background-color: rgba(48, 36, 14, 232); "
                    "border: 1px solid #C89B3C; border-left: 3px solid #F0E6D2; border-radius: 2px; }"
                )
            elif gap_badge:
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
            hl.setSpacing(5)

            tier = score_hero_tier(wr)
            t_lbl = QLabel(tier)
            t_lbl.setFixedWidth(26)
            t_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t_lbl.setStyleSheet(tier_badge_qss(tier))

            ico_size = 20 if (compact or is_narrow) else (28 if is_wide else 24)
            ico_lbl = QLabel()
            ico_lbl.setFixedSize(ico_size, ico_size)
            ico_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pm = self._get_portrait(hid, store, "#F0E6D2" if is_mine else ("#0AC8B9" if gap_badge else "#785A28"))
            if pm is not None:
                ico_lbl.setPixmap(pm.scaled(ico_size, ico_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
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
            elif role_tag:
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

        # 2. Actionable Bench Section: gap-fillers boosted + highlighted, then win-rate sorted
        bench_items: list[tuple[int, float, str, str]] = []
        for hid in state.bench_hero_ids:
            if not hid or hid in seen:
                continue
            h = store.get_hero(hid)
            if h is None:
                continue
            seen.add(hid)
            h_role = _classify_hero_role(hid, store)
            gap_badge = _ROLE_GAP_BADGE.get(h_role, "") if h_role in needed_roles else ""
            bench_items.append((1 if gap_badge else 0, h.win_rate, hid, gap_badge))

        if bench_items:
            bench_items.sort(key=lambda it: (it[0], it[1]), reverse=True)
            _append_widget(_make_section_hdr(f"◈ 备战席 · 可立即换取 ({len(bench_items)})", "#0AC8B9"))
            for _prio, _wr, hid, gap_badge in bench_items[:8]:
                brow = _build_hero_row(hid, "备战", gap_badge=gap_badge)
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

        if comp_txt:
            self.comp_alert_lbl.setText(comp_txt)
            if is_warn:
                self.comp_alert_lbl.setStyleSheet(
                    "background-color: rgba(68, 38, 10, 215); border: 1px solid #D97706; "
                    "border-radius: 2px; padding: 4px 7px; color: #FDE68A; font-size: 10px; font-weight: bold;"
                )
            else:
                self.comp_alert_lbl.setStyleSheet(
                    "background-color: rgba(6, 40, 34, 195); border: 1px solid #09C269; "
                    "border-radius: 2px; padding: 4px 7px; color: #6EE7B7; font-size: 10px;"
                )
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

        scr = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        avail_h = scr.availableGeometry().height() if scr is not None else 720
        if self._custom_h is not None:
            self.rows_scroll.setMinimumHeight(64)
            self.rows_scroll.setMaximumHeight(self._QWIDGETSIZE_MAX)
            self.layout_main.invalidate()
            self.layout_main.activate()
            self.setFixedHeight(min(avail_h - 24, max(self.MIN_H, self._custom_h)))
        else:
            # Allow full 10-13 hero visibility on 1080p/1440p without scrolling; bound safely on 1080p@150% DPI
            max_scroll_h = max(260, min(780, avail_h - 180))
            target_scroll_h = min(max_scroll_h, max(64, content_h))
            self.rows_scroll.setFixedHeight(target_scroll_h)

            self.layout_main.invalidate()
            self.layout_main.activate()
            outer_lay = self.layout()
            if outer_lay is not None:
                outer_lay.invalidate()
                outer_lay.activate()
            total_h = max(self.layout_main.sizeHint().height() + 4, target_scroll_h + 100)
            self.setFixedHeight(min(avail_h - 24, total_h))
        self.ensure_on_screen()
        self._layout_corner_grips()

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

    def show_overlay(self) -> None:
        self.ensure_on_screen()
        was_hidden = not self.isVisible()
        if was_hidden:
            self.setWindowOpacity(0.15)
        show_no_activate(self, click_through=False)
        self._layout_corner_grips()
        if was_hidden:
            anim = QPropertyAnimation(self, b"windowOpacity", self)
            anim.setDuration(155)
            anim.setStartValue(0.15)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.start()
            self._fade_anim = anim

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            a_reset_size = QAction("恢复默认尺寸", menu)
            a_reset_size.triggered.connect(self.reset_default_size)
            a_hide = QAction("隐藏选人悬浮窗", menu)
            a_hide.triggered.connect(self.hide)
            menu.addAction(a_reset_size)
            menu.addSeparator()
            menu.addAction(a_hide)
            menu.exec(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            pos = event.globalPosition().toPoint() - self._drag_offset
            scr = QGuiApplication.screenAt(event.globalPosition().toPoint()) or QGuiApplication.primaryScreen()
            if scr is not None:
                geo = scr.availableGeometry()
                cx, cy = clamp_to_screen_rect(pos.x(), pos.y(), self.width(), self.height(), geo, margin=4)
                pos = QPoint(cx, cy)
            self.move(pos)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._drag_offset = None
            if self._on_pos_changed:
                self._on_pos_changed([self.x(), self.y()])
