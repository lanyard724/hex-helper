"""Game-grade LoL Hextech custom hotkey settings dialog, capture keycap widget & 3-tier conflict analyzer.

Supports click-to-record, Ctrl/Alt/Shift combinations, Esc cancel, Backspace/Delete unbind,
internal conflict Swap/Overwrite/Undo resolution, LOL in-game key warnings, and live Win32 occupancy probes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hex_helper.config import HotkeyConfig
from hex_helper.ui.hextech_widgets import HextechActionButton
from hex_helper.ui.theme import BASE_OVERLAY_QSS, body_font, display_font, latin_serif_font, load_app_icon
from hex_helper.ui.win32_window import normalize_hotkey, parse_hotkey


@dataclass(frozen=True)
class HotkeyActionSpec:
    field_name: str
    label: str
    desc: str
    category: str
    default_key: str


HOTKEY_ACTION_SPECS: tuple[HotkeyActionSpec, ...] = (
    HotkeyActionSpec(
        field_name="toggle_drawer",
        label="右侧出装抽屉 · 展开 / 收起",
        desc="展开或收起右侧边缘出装与 AI 参谋抽屉",
        category="侧边栏与悬浮窗",
        default_key="F3",
    ),
    HotkeyActionSpec(
        field_name="toggle_combo",
        label="左侧海克斯联动浮窗 · 展开 / 收起",
        desc="展开或收起左侧海克斯联动 / 赌狗组合规划浮窗",
        category="侧边栏与悬浮窗",
        default_key="F7",
    ),
    HotkeyActionSpec(
        field_name="toggle_sidebars",
        label="左右侧边栏同步 · 一键收起 / 展开",
        desc="同步收起或展开左右两侧边栏悬浮窗（团战快速清屏）",
        category="侧边栏与悬浮窗",
        default_key="F9",
    ),
    HotkeyActionSpec(
        field_name="toggle_all",
        label="全部游戏内浮窗 · 隐藏 / 恢复",
        desc="一键临时隐藏或恢复全部游戏内悬浮层",
        category="侧边栏与悬浮窗",
        default_key="F4",
    ),
    HotkeyActionSpec(
        field_name="force_scan",
        label="手动重扫三选一 / 四符文卡片",
        desc="立即对当前海克斯选卡界面重新执行 OCR 与图标识别",
        category="实战识别与 AI 参谋",
        default_key="F2",
    ),
    HotkeyActionSpec(
        field_name="call_coach",
        label="立即呼叫 AI 参谋推演",
        desc="随时强制拉取一次当前 10 人局势与精确散件建议",
        category="实战识别与 AI 参谋",
        default_key="F6",
    ),
    HotkeyActionSpec(
        field_name="calibrate_roi",
        label="校准选卡识别区域",
        desc="打开框选校准层（适配异形带鱼屏或自定义分辨率）",
        category="实战识别与 AI 参谋",
        default_key="F8",
    ),
    HotkeyActionSpec(
        field_name="toggle_lookup",
        label="主控台英雄速查窗 · 呼出 / 隐藏",
        desc="随时切换显示或最小化 HexHelper 主窗口",
        category="主控台与赛后工具",
        default_key="",
    ),
    HotkeyActionSpec(
        field_name="open_recap",
        label="打开本局高光战报卡",
        desc="快速弹出最近一局的海克斯战报卡预览与一键复制窗",
        category="主控台与赛后工具",
        default_key="",
    ),
)

ACTION_SPEC_BY_FIELD: dict[str, HotkeyActionSpec] = {s.field_name: s for s in HOTKEY_ACTION_SPECS}

# System-critical shortcuts blocked from binding to protect Windows usability
RESERVED_SYSTEM_HOTKEYS: set[str] = {
    "Alt+F4",
    "Alt+Tab",
    "Alt+Shift+Tab",
    "Ctrl+Alt+Delete",
}

# Default League of Legends in-game bare keys that global RegisterHotKey would intercept during gameplay
LOL_BARE_KEY_WARNINGS: dict[str, str] = {
    "Q": "Q 技能键",
    "W": "W 技能键",
    "E": "E 技能键",
    "R": "R 终极技能键",
    "D": "召唤师技能 D 键",
    "F": "召唤师技能 F 键",
    "P": "商店开关 P 键",
    "B": "回城键",
    "Tab": "计分板键",
    "Space": "居中锁定视角键",
    "A": "攻击移动键",
    "S": "停止动作键",
    "G": "快捷信号键",
    "V": "撤退信号键",
    "Y": "视角锁定切换键",
    "C": "英雄属性面板键",
    "1": "主动装备栏 1",
    "2": "主动装备栏 2",
    "3": "主动装备栏 3",
    "4": "饰品/视野键 4",
    "5": "主动装备栏 5",
    "6": "主动装备栏 6",
}

_QT_KEY_TO_NAME: dict[int, str] = {
    int(Qt.Key.Key_F1): "F1",
    int(Qt.Key.Key_F2): "F2",
    int(Qt.Key.Key_F3): "F3",
    int(Qt.Key.Key_F4): "F4",
    int(Qt.Key.Key_F5): "F5",
    int(Qt.Key.Key_F6): "F6",
    int(Qt.Key.Key_F7): "F7",
    int(Qt.Key.Key_F8): "F8",
    int(Qt.Key.Key_F9): "F9",
    int(Qt.Key.Key_F10): "F10",
    int(Qt.Key.Key_F11): "F11",
    int(Qt.Key.Key_F12): "F12",
    int(Qt.Key.Key_Tab): "Tab",
    int(Qt.Key.Key_Backtab): "Tab",
    int(Qt.Key.Key_Space): "Space",
    int(Qt.Key.Key_PageUp): "PageUp",
    int(Qt.Key.Key_PageDown): "PageDown",
    int(Qt.Key.Key_Home): "Home",
    int(Qt.Key.Key_End): "End",
    int(Qt.Key.Key_Insert): "Insert",
    int(Qt.Key.Key_Pause): "Pause",
    int(Qt.Key.Key_Left): "Left",
    int(Qt.Key.Key_Right): "Right",
    int(Qt.Key.Key_Up): "Up",
    int(Qt.Key.Key_Down): "Down",
    int(Qt.Key.Key_QuoteLeft): "`",
    int(Qt.Key.Key_AsciiTilde): "`",
    int(Qt.Key.Key_Minus): "-",
    int(Qt.Key.Key_Equal): "=",
    int(Qt.Key.Key_BracketLeft): "[",
    int(Qt.Key.Key_BracketRight): "]",
    int(Qt.Key.Key_Backslash): "\\",
    int(Qt.Key.Key_Semicolon): ";",
    int(Qt.Key.Key_Apostrophe): "'",
    int(Qt.Key.Key_Comma): ",",
    int(Qt.Key.Key_Period): ".",
    int(Qt.Key.Key_Slash): "/",
}

_MODIFIER_ONLY_KEYS: set[int] = {
    int(Qt.Key.Key_Control),
    int(Qt.Key.Key_Shift),
    int(Qt.Key.Key_Alt),
    int(Qt.Key.Key_Meta),
    int(Qt.Key.Key_AltGr),
    int(Qt.Key.Key_CapsLock),
    int(Qt.Key.Key_NumLock),
    int(Qt.Key.Key_ScrollLock),
}


def qt_key_event_to_hotkey(event: QKeyEvent) -> tuple[str, str | None]:
    """Convert QKeyEvent into (action_kind, canonical_hotkey).

    action_kind:
      - 'ignore': modifier-only press (keep waiting for primary key)
      - 'cancel': Esc pressed (abort recording)
      - 'clear': Backspace / Delete pressed (unbind key)
      - 'unsupported': unsupported key pressed
      - 'bound': valid hotkey captured (canonical_hotkey is non-empty)
    """
    key_int = int(event.key())
    if key_int in _MODIFIER_ONLY_KEYS:
        return "ignore", None
    if key_int == int(Qt.Key.Key_Escape):
        return "cancel", None

    mods = event.modifiers()
    has_ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
    has_alt = bool(mods & Qt.KeyboardModifier.AltModifier)
    has_shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
    is_keypad = bool(mods & Qt.KeyboardModifier.KeypadModifier)

    if key_int in (int(Qt.Key.Key_Backspace), int(Qt.Key.Key_Delete)) and not (has_ctrl or has_alt or has_shift):
        return "clear", ""

    main_name = ""
    if is_keypad and int(Qt.Key.Key_0) <= key_int <= int(Qt.Key.Key_9):
        main_name = f"Num{key_int - int(Qt.Key.Key_0)}"
    elif is_keypad and key_int == int(Qt.Key.Key_Asterisk):
        main_name = "Num*"
    elif is_keypad and key_int == int(Qt.Key.Key_Plus):
        main_name = "Num+"
    elif is_keypad and key_int == int(Qt.Key.Key_Minus):
        main_name = "Num-"
    elif is_keypad and key_int == int(Qt.Key.Key_Period):
        main_name = "Num."
    elif is_keypad and key_int == int(Qt.Key.Key_Slash):
        main_name = "Num/"
    elif int(Qt.Key.Key_A) <= key_int <= int(Qt.Key.Key_Z):
        main_name = chr(key_int)
    elif int(Qt.Key.Key_0) <= key_int <= int(Qt.Key.Key_9):
        main_name = chr(key_int)
    elif key_int in _QT_KEY_TO_NAME:
        main_name = _QT_KEY_TO_NAME[key_int]
    else:
        # Handle Shift+symbol layouts where Qt reports shifted symbols like '!' / '_' / '+'
        shift_base_map = {
            int(Qt.Key.Key_Exclam): "1",
            int(Qt.Key.Key_At): "2",
            int(Qt.Key.Key_NumberSign): "3",
            int(Qt.Key.Key_Dollar): "4",
            int(Qt.Key.Key_Percent): "5",
            int(Qt.Key.Key_AsciiCircum): "6",
            int(Qt.Key.Key_Ampersand): "7",
            int(Qt.Key.Key_Asterisk): "8",
            int(Qt.Key.Key_ParenLeft): "9",
            int(Qt.Key.Key_ParenRight): "0",
            int(Qt.Key.Key_Underscore): "-",
            int(Qt.Key.Key_Plus): "=",
            int(Qt.Key.Key_BraceLeft): "[",
            int(Qt.Key.Key_BraceRight): "]",
            int(Qt.Key.Key_Bar): "\\",
            int(Qt.Key.Key_Colon): ";",
            int(Qt.Key.Key_QuoteDbl): "'",
            int(Qt.Key.Key_Less): ",",
            int(Qt.Key.Key_Greater): ".",
            int(Qt.Key.Key_Question): "/",
        }
        main_name = shift_base_map.get(key_int, "")

    if not main_name:
        return "unsupported", None

    parts: list[str] = []
    if has_ctrl:
        parts.append("Ctrl")
    if has_alt:
        parts.append("Alt")
    if has_shift:
        parts.append("Shift")
    parts.append(main_name)
    canonical = normalize_hotkey("+".join(parts))
    if not canonical:
        return "unsupported", None
    return "bound", canonical


@dataclass(frozen=True)
class HotkeyConflictReport:
    """Comprehensive 3-tier conflict analysis for an entire HotkeyConfig draft."""

    internal_conflicts: dict[str, list[str]]   # field_name -> list of colliding other field_names
    lol_warnings: dict[str, str]               # field_name -> soft LOL gameplay key warning
    reserved_errors: dict[str, str]            # field_name -> hard blocked system shortcut reason
    os_occupied: dict[str, str]                # field_name -> external Windows app occupancy warning

    @property
    def has_blocking_errors(self) -> bool:
        return bool(self.internal_conflicts or self.reserved_errors)


def analyze_hotkey_conflicts(
    bindings: dict[str, str],
    *,
    os_occupied: dict[str, str] | None = None,
) -> HotkeyConflictReport:
    """Analyze internal duplicates, reserved system shortcuts, LOL bare-key traps, and OS occupancy."""
    key_to_fields: dict[str, list[str]] = {}
    lol_warnings: dict[str, str] = {}
    reserved_errors: dict[str, str] = {}

    for field_name, raw_key in bindings.items():
        canonical = normalize_hotkey(raw_key)
        if not canonical:
            continue
        if canonical in RESERVED_SYSTEM_HOTKEYS:
            reserved_errors[field_name] = f"「{canonical}」为系统保留快捷键，禁止绑定"
            continue
        key_to_fields.setdefault(canonical, []).append(field_name)
        if canonical in LOL_BARE_KEY_WARNINGS:
            lol_warnings[field_name] = (
                f"「{canonical}」是 LOL 默认{LOL_BARE_KEY_WARNINGS[canonical]}，"
                f"单键绑定会拦截游戏输入，建议改用 Alt+{canonical} 或 F2-F12"
            )

    internal_conflicts: dict[str, list[str]] = {}
    for _key, fields in key_to_fields.items():
        if len(fields) > 1:
            for f in fields:
                internal_conflicts[f] = [other for other in fields if other != f]

    return HotkeyConflictReport(
        internal_conflicts=internal_conflicts,
        lol_warnings=lol_warnings,
        reserved_errors=reserved_errors,
        os_occupied=dict(os_occupied or {}),
    )


def hotkeys_to_dict(hk: HotkeyConfig) -> dict[str, str]:
    return {spec.field_name: normalize_hotkey(getattr(hk, spec.field_name, "")) for spec in HOTKEY_ACTION_SPECS}


def dict_to_hotkey_config(data: dict[str, str]) -> HotkeyConfig:
    kwargs = {}
    for spec in HOTKEY_ACTION_SPECS:
        kwargs[spec.field_name] = normalize_hotkey(data.get(spec.field_name, ""))
    return HotkeyConfig(**kwargs)


def format_hotkeys_summary(hk: HotkeyConfig) -> str:
    """Return compact one-line summary for diagnostics & Settings header."""
    short_labels = (
        ("force_scan", "重扫"),
        ("toggle_drawer", "右侧栏"),
        ("toggle_combo", "左侧栏"),
        ("toggle_sidebars", "双侧栏"),
        ("toggle_all", "隐藏全部"),
        ("call_coach", "AI参谋"),
        ("calibrate_roi", "校准"),
    )
    parts: list[str] = []
    for fname, zh in short_labels:
        k = normalize_hotkey(getattr(hk, fname, ""))
        if k:
            parts.append(f"{k} {zh}")
    return " | ".join(parts) if parts else "全部热键已解绑（点击「自定义快捷键」配置）"


class HotkeyCaptureButton(QPushButton):
    """Game-style click-to-record sculpted Hextech keycap button."""

    recording_started = Signal(str)           # field_name
    recording_cancelled = Signal(str)         # field_name
    hotkey_captured = Signal(str, str)        # field_name, canonical_hotkey ("" = cleared)
    capture_rejected = Signal(str, str)       # field_name, reason

    def __init__(self, field_name: str, initial_key: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.field_name = field_name
        self._key_text = normalize_hotkey(initial_key)
        self._recording = False
        self._visual_state = "normal"  # "normal" | "conflict" | "warning" | "occupied"
        self.setFixedHeight(30)
        self.setMinimumWidth(148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(latin_serif_font(10, bold=True, letter_spacing=0.8))
        self.clicked.connect(self._on_clicked)
        self._refresh_visual()

    @property
    def key_text(self) -> str:
        return self._key_text

    @property
    def is_recording(self) -> bool:
        return self._recording

    def set_hotkey(self, key_text: str) -> None:
        self._key_text = normalize_hotkey(key_text)
        self._recording = False
        self._refresh_visual()

    def set_visual_state(self, state: str) -> None:
        self._visual_state = state if state in ("normal", "conflict", "warning", "occupied") else "normal"
        self._refresh_visual()

    def start_recording(self) -> None:
        if self._recording:
            return
        self._recording = True
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._refresh_visual()
        self.recording_started.emit(self.field_name)

    def cancel_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._refresh_visual()
        self.recording_cancelled.emit(self.field_name)

    def _on_clicked(self) -> None:
        if self._recording:
            self.cancel_recording()
        else:
            self.start_recording()

    def _refresh_visual(self) -> None:
        if self._recording:
            self.setText("请按键... (Esc取消)")
            self.setFont(body_font(9, bold=True))
            self.setStyleSheet(
                "QPushButton { background-color: rgba(10, 62, 76, 235); border: 1.5px solid #0AC8B9; "
                "border-radius: 2px; color: #7CF8EE; padding: 0px 8px; }"
            )
            return

        if not self._key_text:
            self.setText("未绑定")
            self.setFont(body_font(10, bold=False))
            self.setStyleSheet(
                "QPushButton { background-color: rgba(5, 14, 24, 185); border: 1px dashed #463714; "
                "border-radius: 2px; color: #5B5A56; padding: 0px 8px; }"
                "QPushButton:hover { border-color: #C8AA6E; color: #A09B8C; }"
            )
            return

        self.setText(self._key_text.replace("+", " + "))
        self.setFont(latin_serif_font(10, bold=True, letter_spacing=0.6))
        if self._visual_state in ("conflict", "occupied"):
            self.setStyleSheet(
                "QPushButton { background-color: rgba(86, 20, 22, 225); border: 1.5px solid #F87171; "
                "border-radius: 2px; color: #FECACA; padding: 0px 8px; }"
                "QPushButton:hover { border-color: #FCA5A5; color: #FFFFFF; }"
            )
        elif self._visual_state == "warning":
            self.setStyleSheet(
                "QPushButton { background-color: rgba(62, 42, 12, 225); border: 1.5px solid #F59E0B; "
                "border-radius: 2px; color: #FDE68A; padding: 0px 8px; }"
                "QPushButton:hover { border-color: #FDE68A; color: #FFFFFF; }"
            )
        else:
            self.setStyleSheet(
                "QPushButton { background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                "stop:0 #142638, stop:1 #091424); border: 1px solid #785A28; "
                "border-bottom: 2px solid #463714; border-radius: 2px; color: #F0E6D2; padding: 0px 8px; }"
                "QPushButton:hover { border-color: #C8AA6E; color: #FFFFFF; background-color: #1B334B; }"
            )

    def event(self, ev: QEvent) -> bool:
        if self._recording:
            if ev.type() == QEvent.Type.ShortcutOverride:
                ev.accept()
                return True
            if ev.type() == QEvent.Type.KeyPress:
                self.keyPressEvent(ev)  # type: ignore[arg-type]
                return True
        return super().event(ev)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if not self._recording:
            super().keyPressEvent(event)
            return
        event.accept()
        kind, canonical = qt_key_event_to_hotkey(event)
        if kind == "ignore":
            return
        if kind == "cancel":
            self.cancel_recording()
            return
        if kind == "clear":
            self._recording = False
            self._key_text = ""
            self._refresh_visual()
            self.hotkey_captured.emit(self.field_name, "")
            return
        if kind == "unsupported" or not canonical:
            self._recording = False
            self._refresh_visual()
            self.capture_rejected.emit(self.field_name, "暂不支持该按键（支持 F1-F12、字母、数字、小键盘及 Ctrl/Alt/Shift 组合）")
            return
        if canonical in RESERVED_SYSTEM_HOTKEYS:
            self._recording = False
            self._refresh_visual()
            self.capture_rejected.emit(self.field_name, f"「{canonical}」为系统保留快捷键，禁止绑定")
            return
        self._recording = False
        self._key_text = canonical
        self._refresh_visual()
        self.hotkey_captured.emit(self.field_name, canonical)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        if self._recording:
            self.cancel_recording()
        super().focusOutEvent(event)


class HotkeySettingsDialog(QDialog):
    """Game-style Hextech custom hotkey editor with instant conflict resolution (Swap/Overwrite/Undo)."""

    def __init__(
        self,
        initial_hotkeys: HotkeyConfig,
        *,
        os_occupied: dict[str, str] | None = None,
        probe_available: Callable[[str], tuple[bool, str]] | None = None,
        on_suspend_hotkeys: Callable[[bool], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._draft: dict[str, str] = hotkeys_to_dict(initial_hotkeys)
        self._os_occupied: dict[str, str] = dict(os_occupied or {})
        self._probe_available = probe_available
        self._on_suspend_hotkeys = on_suspend_hotkeys
        self._hotkeys_suspended: bool = False
        self._active_recording_field: str = ""
        self._pending_swap_ctx: tuple[str, str, str, str] | None = None  # (target_field, collided_field, new_key, old_target_key)
        self.result_hotkeys: HotkeyConfig | None = None

        self.setWindowTitle("HexHelper · 游戏快捷键自定义")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.setMinimumSize(700, 560)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        # Header
        hdr_row = QHBoxLayout()
        title = QLabel("◈ 自定义全局快捷键（点击键帽录入 · 支持 Ctrl / Alt / Shift 组合）")
        title.setFont(display_font(13, bold=True, letter_spacing=0.6))
        title.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        tip = QLabel("Esc 取消录入 · Backspace / Del 解绑")
        tip.setStyleSheet("color: #0AC8B9; font-size: 11px; background: transparent; border: none;")
        hdr_row.addWidget(title)
        hdr_row.addStretch(1)
        hdr_row.addWidget(tip)
        root.addLayout(hdr_row)

        # Inline Conflict & Game-Style Quick Resolution Banner (Swap / Overwrite / Undo)
        self.conflict_banner = QFrame(self)
        self.conflict_banner.setStyleSheet(
            "QFrame { background-color: rgba(74, 22, 24, 232); border: 1px solid #F87171; "
            "border-left: 3px solid #FDE68A; border-radius: 2px; }"
        )
        cbl = QHBoxLayout(self.conflict_banner)
        cbl.setContentsMargins(10, 7, 10, 7)
        cbl.setSpacing(8)
        self.conflict_lbl = QLabel("")
        self.conflict_lbl.setWordWrap(True)
        self.conflict_lbl.setStyleSheet("border: none; background: transparent; color: #FEF3C7; font-size: 11px; font-weight: bold;")
        self.swap_btn = QPushButton("⇄ 互换键位")
        self.overwrite_btn = QPushButton("覆盖并解绑对方")
        self.undo_btn = QPushButton("撤销")
        for b, primary in ((self.swap_btn, True), (self.overwrite_btn, False), (self.undo_btn, False)):
            b.setFixedHeight(24)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if primary:
                b.setStyleSheet(
                    "QPushButton { background-color: #0E3746; border: 1px solid #0AC8B9; color: #F0E6D2; "
                    "font-size: 10px; font-weight: bold; padding: 0px 9px; border-radius: 2px; }"
                    "QPushButton:hover { border-color: #7CF8EE; color: #FFFFFF; }"
                )
            else:
                b.setStyleSheet(
                    "QPushButton { background-color: rgba(16, 26, 38, 220); border: 1px solid #785A28; color: #F0E6D2; "
                    "font-size: 10px; font-weight: bold; padding: 0px 8px; border-radius: 2px; }"
                    "QPushButton:hover { border-color: #C8AA6E; color: #FFFFFF; }"
                )
        self.swap_btn.clicked.connect(self._resolve_swap)
        self.overwrite_btn.clicked.connect(self._resolve_overwrite)
        self.undo_btn.clicked.connect(self._resolve_undo)
        cbl.addWidget(self.conflict_lbl, 1)
        cbl.addWidget(self.swap_btn)
        cbl.addWidget(self.overwrite_btn)
        cbl.addWidget(self.undo_btn)
        self.conflict_banner.hide()
        root.addWidget(self.conflict_banner)

        # Soft Notice Banner (LOL default key warning / external OS occupancy notice)
        self.notice_banner = QFrame(self)
        self.notice_banner.setStyleSheet(
            "QFrame { background-color: rgba(52, 38, 12, 224); border: 1px solid #C89B3C; "
            "border-left: 3px solid #FDE68A; border-radius: 2px; }"
        )
        nbl = QHBoxLayout(self.notice_banner)
        nbl.setContentsMargins(10, 6, 10, 6)
        self.notice_lbl = QLabel("")
        self.notice_lbl.setWordWrap(True)
        self.notice_lbl.setStyleSheet("border: none; background: transparent; color: #FDE68A; font-size: 11px;")
        nbl.addWidget(self.notice_lbl)
        self.notice_banner.hide()
        root.addWidget(self.notice_banner)

        # Scrollable categorized hotkey rows
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet("background: transparent;")
        list_host = QWidget()
        list_host.setStyleSheet("background: transparent;")
        list_lay = QVBoxLayout(list_host)
        list_lay.setContentsMargins(0, 2, 6, 4)
        list_lay.setSpacing(6)

        self._capture_btns: dict[str, HotkeyCaptureButton] = {}
        self._row_status_lbls: dict[str, QLabel] = {}
        self._row_frames: dict[str, QFrame] = {}

        last_cat = ""
        for spec in HOTKEY_ACTION_SPECS:
            if spec.category != last_cat:
                last_cat = spec.category
                cat_lbl = QLabel(f"◈ {spec.category}")
                cat_lbl.setFont(display_font(10, bold=True, letter_spacing=0.8))
                cat_lbl.setStyleSheet(
                    "color: #C8AA6E; background: transparent; border: none; padding-top: 4px; padding-bottom: 1px;"
                )
                list_lay.addWidget(cat_lbl)

            row_frame = QFrame(list_host)
            row_frame.setStyleSheet(
                "QFrame { background-color: rgba(9, 20, 36, 218); border: 1px solid rgba(120, 90, 40, 125); "
                "border-radius: 2px; }"
            )
            rl = QHBoxLayout(row_frame)
            rl.setContentsMargins(10, 6, 10, 6)
            rl.setSpacing(10)

            text_col = QVBoxLayout()
            text_col.setContentsMargins(0, 0, 0, 0)
            text_col.setSpacing(1)
            lbl = QLabel(spec.label)
            lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-size: 12px; font-weight: bold;")
            sub = QLabel(spec.desc)
            sub.setStyleSheet("border: none; background: transparent; color: #A09B8C; font-size: 10px;")
            row_st = QLabel("")
            row_st.setWordWrap(True)
            row_st.setStyleSheet("border: none; background: transparent; color: #FDE68A; font-size: 10px; font-weight: bold;")
            row_st.hide()
            text_col.addWidget(lbl)
            text_col.addWidget(sub)
            text_col.addWidget(row_st)

            cap_btn = HotkeyCaptureButton(spec.field_name, self._draft.get(spec.field_name, ""), parent=row_frame)
            cap_btn.recording_started.connect(self._on_recording_started)
            cap_btn.recording_cancelled.connect(self._on_recording_cancelled)
            cap_btn.hotkey_captured.connect(self._on_hotkey_captured)
            cap_btn.capture_rejected.connect(self._on_capture_rejected)

            clr_btn = QPushButton("×")
            clr_btn.setFixedSize(24, 30)
            clr_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            clr_btn.setToolTip("清除此快捷键（设为未绑定）")
            clr_btn.setStyleSheet(
                "QPushButton { background-color: rgba(8, 18, 32, 195); border: 1px solid #463714; "
                "color: #A09B8C; font-size: 13px; padding: 0px; border-radius: 2px; }"
                "QPushButton:hover { border-color: #C8AA6E; color: #F0E6D2; }"
            )
            clr_btn.clicked.connect(lambda _=False, fn=spec.field_name: self._clear_field(fn))

            rl.addLayout(text_col, 1)
            rl.addWidget(cap_btn)
            rl.addWidget(clr_btn)

            self._capture_btns[spec.field_name] = cap_btn
            self._row_status_lbls[spec.field_name] = row_st
            self._row_frames[spec.field_name] = row_frame
            list_lay.addWidget(row_frame)

        list_lay.addStretch(1)
        scroll.setWidget(list_host)
        root.addWidget(scroll, 1)

        # Footer action bar
        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.reset_btn = HextechActionButton("恢复全部默认键位", variant="gold", height=34)
        self.reset_btn.clicked.connect(self.reset_defaults)
        self.cancel_btn = HextechActionButton("取消", variant="gold", height=34)
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = HextechActionButton("应用并保存快捷键", variant="cyan", pointed_right=True, height=34)
        self.save_btn.clicked.connect(self._on_save_clicked)

        foot.addWidget(self.reset_btn)
        foot.addStretch(1)
        foot.addWidget(self.cancel_btn)
        foot.addWidget(self.save_btn)
        root.addLayout(foot)

        self._refresh_all_conflict_states()

    def _suspend_global(self, flag: bool) -> None:
        if self._hotkeys_suspended == flag:
            return
        self._hotkeys_suspended = flag
        if self._on_suspend_hotkeys is not None:
            try:
                self._on_suspend_hotkeys(flag)
            except Exception:
                pass

    def _on_recording_started(self, field_name: str) -> None:
        for fn, btn in self._capture_btns.items():
            if fn != field_name and btn.is_recording:
                btn.cancel_recording()
        self._active_recording_field = field_name
        self._suspend_global(True)

    def _on_recording_cancelled(self, field_name: str) -> None:
        if self._active_recording_field == field_name:
            self._active_recording_field = ""
            self._suspend_global(False)

    def _on_capture_rejected(self, field_name: str, reason: str) -> None:
        if self._active_recording_field == field_name:
            self._active_recording_field = ""
            self._suspend_global(False)
        self.notice_lbl.setText(f"! {reason}")
        self.notice_banner.show()

    def _clear_field(self, field_name: str) -> None:
        self._pending_swap_ctx = None
        self._draft[field_name] = ""
        self._os_occupied.pop(field_name, None)
        self._capture_btns[field_name].set_hotkey("")
        self._refresh_all_conflict_states()

    def _on_hotkey_captured(self, field_name: str, canonical: str) -> None:
        if self._active_recording_field == field_name:
            self._active_recording_field = ""
            self._suspend_global(False)

        old_key = self._draft.get(field_name, "")
        self._pending_swap_ctx = None
        self._os_occupied.pop(field_name, None)

        if canonical:
            # Check internal duplicate first so we can offer instant Swap / Overwrite / Undo
            collided = next(
                (fn for fn, kv in self._draft.items() if fn != field_name and kv == canonical),
                None,
            )
            self._draft[field_name] = canonical
            if collided is not None:
                self._pending_swap_ctx = (field_name, collided, canonical, old_key)
            elif self._probe_available is not None:
                ok, occ_msg = self._probe_available(canonical)
                if not ok and occ_msg:
                    self._os_occupied[field_name] = occ_msg
        else:
            self._draft[field_name] = ""

        self._refresh_all_conflict_states()

    def _resolve_swap(self) -> None:
        if self._pending_swap_ctx is None:
            return
        target_fn, collided_fn, new_key, old_target_key = self._pending_swap_ctx
        self._draft[target_fn] = new_key
        self._draft[collided_fn] = old_target_key
        self._capture_btns[target_fn].set_hotkey(new_key)
        self._capture_btns[collided_fn].set_hotkey(old_target_key)
        self._pending_swap_ctx = None
        self._refresh_all_conflict_states()

    def _resolve_overwrite(self) -> None:
        if self._pending_swap_ctx is None:
            return
        target_fn, collided_fn, new_key, _old_target_key = self._pending_swap_ctx
        self._draft[target_fn] = new_key
        self._draft[collided_fn] = ""
        self._capture_btns[target_fn].set_hotkey(new_key)
        self._capture_btns[collided_fn].set_hotkey("")
        self._pending_swap_ctx = None
        self._refresh_all_conflict_states()

    def _resolve_undo(self) -> None:
        if self._pending_swap_ctx is None:
            return
        target_fn, _collided_fn, _new_key, old_target_key = self._pending_swap_ctx
        self._draft[target_fn] = old_target_key
        self._capture_btns[target_fn].set_hotkey(old_target_key)
        self._pending_swap_ctx = None
        self._refresh_all_conflict_states()

    def reset_defaults(self) -> None:
        self._pending_swap_ctx = None
        self._os_occupied.clear()
        for spec in HOTKEY_ACTION_SPECS:
            self._draft[spec.field_name] = spec.default_key
            self._capture_btns[spec.field_name].set_hotkey(spec.default_key)
        self._refresh_all_conflict_states()

    def _refresh_all_conflict_states(self) -> None:
        report = analyze_hotkey_conflicts(self._draft, os_occupied=self._os_occupied)

        for spec in HOTKEY_ACTION_SPECS:
            fn = spec.field_name
            btn = self._capture_btns[fn]
            st_lbl = self._row_status_lbls[fn]
            row_f = self._row_frames[fn]

            if fn in report.reserved_errors:
                btn.set_visual_state("conflict")
                st_lbl.setText(f"✕ {report.reserved_errors[fn]}")
                st_lbl.setStyleSheet("border: none; background: transparent; color: #F87171; font-size: 10px; font-weight: bold;")
                st_lbl.show()
                row_f.setStyleSheet(
                    "QFrame { background-color: rgba(58, 18, 22, 224); border: 1px solid #F87171; border-radius: 2px; }"
                )
            elif fn in report.internal_conflicts:
                others = "、".join(ACTION_SPEC_BY_FIELD[o].label.split(" · ")[0] for o in report.internal_conflicts[fn] if o in ACTION_SPEC_BY_FIELD)
                btn.set_visual_state("conflict")
                st_lbl.setText(f"✕ 键位冲突：与【{others}】重复")
                st_lbl.setStyleSheet("border: none; background: transparent; color: #FCA5A5; font-size: 10px; font-weight: bold;")
                st_lbl.show()
                row_f.setStyleSheet(
                    "QFrame { background-color: rgba(58, 18, 22, 224); border: 1px solid #F87171; border-radius: 2px; }"
                )
            elif fn in report.os_occupied:
                btn.set_visual_state("occupied")
                st_lbl.setText(f"! {report.os_occupied[fn]}（保存时可能无法生效，建议换键）")
                st_lbl.setStyleSheet("border: none; background: transparent; color: #FCA5A5; font-size: 10px; font-weight: bold;")
                st_lbl.show()
                row_f.setStyleSheet(
                    "QFrame { background-color: rgba(52, 24, 18, 220); border: 1px solid #F59E0B; border-radius: 2px; }"
                )
            elif fn in report.lol_warnings:
                btn.set_visual_state("warning")
                st_lbl.setText(f"! {report.lol_warnings[fn]}")
                st_lbl.setStyleSheet("border: none; background: transparent; color: #FDE68A; font-size: 10px;")
                st_lbl.show()
                row_f.setStyleSheet(
                    "QFrame { background-color: rgba(34, 28, 14, 218); border: 1px solid #C89B3C; border-radius: 2px; }"
                )
            else:
                btn.set_visual_state("normal")
                st_lbl.hide()
                row_f.setStyleSheet(
                    "QFrame { background-color: rgba(9, 20, 36, 218); border: 1px solid rgba(120, 90, 40, 125); "
                    "border-radius: 2px; }"
                )

        # Top blocking conflict banner
        if report.internal_conflicts:
            if self._pending_swap_ctx is not None:
                t_fn, c_fn, nk, old_k = self._pending_swap_ctx
                c_name = ACTION_SPEC_BY_FIELD[c_fn].label
                swap_hint = f"（对方将换为 {old_k}）" if old_k else "（对方将置为空）"
                self.conflict_lbl.setText(f"⚠ 键位冲突：「{nk}」已绑定给【{c_name}】")
                self.swap_btn.setText(f"⇄ 互换键位{swap_hint}")
                self.swap_btn.show()
                self.overwrite_btn.show()
                self.undo_btn.show()
            else:
                first_fn = next(iter(report.internal_conflicts.keys()))
                self.conflict_lbl.setText(
                    f"⚠ 检测到 {len(report.internal_conflicts)} 项重复键位冲突（如【{ACTION_SPEC_BY_FIELD[first_fn].label}】），请修改或解绑后再保存"
                )
                self.swap_btn.hide()
                self.overwrite_btn.hide()
                self.undo_btn.hide()
            self.conflict_banner.show()
        elif report.reserved_errors:
            first_msg = next(iter(report.reserved_errors.values()))
            self.conflict_lbl.setText(f"⚠ {first_msg}")
            self.swap_btn.hide()
            self.overwrite_btn.hide()
            self.undo_btn.hide()
            self.conflict_banner.show()
        else:
            self.conflict_banner.hide()

        # Soft notice summary
        soft_msgs: list[str] = []
        if report.os_occupied:
            soft_msgs.append(f"有 {len(report.os_occupied)} 项热键当前被其他软件占用（已标红提醒）")
        if report.lol_warnings:
            soft_msgs.append(f"有 {len(report.lol_warnings)} 项使用了 LOL 常用单键，进局可能拦截游戏按键（建议加 Alt / Ctrl 修饰键）")
        if soft_msgs:
            self.notice_lbl.setText("! " + "  |  ".join(soft_msgs))
            self.notice_banner.show()
        else:
            self.notice_banner.hide()

        self.save_btn.setEnabled(not report.has_blocking_errors)

    def _on_save_clicked(self) -> None:
        report = analyze_hotkey_conflicts(self._draft, os_occupied=self._os_occupied)
        if report.has_blocking_errors:
            self._refresh_all_conflict_states()
            return
        self.result_hotkeys = dict_to_hotkey_config(self._draft)
        self.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # Never close entire dialog when user presses Esc just to cancel keycap recording
        if event.key() == Qt.Key.Key_Escape and self._active_recording_field:
            btn = self._capture_btns.get(self._active_recording_field)
            if btn is not None:
                btn.cancel_recording()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._suspend_global(False)
        super().closeEvent(event)

    def reject(self) -> None:
        self._suspend_global(False)
        super().reject()

    def accept(self) -> None:
        self._suspend_global(False)
        super().accept()
