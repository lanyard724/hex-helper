"""Win32 extended window styles (WS_EX_NOACTIVATE / WS_EX_TRANSPARENT) and native RegisterHotKey."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QRect, Qt, Signal
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG
_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
_user32.SetWindowLongW.restype = wintypes.LONG
_user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.RegisterHotKey.restype = wintypes.BOOL
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.UnregisterHotKey.restype = wintypes.BOOL
_user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL

_WIN32_HOTKEY_ERROR_DESCS: dict[int, str] = {
    1409: "already registered",
    1408: "invalid thread",
    87: "invalid parameter",
    5: "access denied",
}


def _describe_win32_hotkey_error(err: int) -> str:
    if err in _WIN32_HOTKEY_ERROR_DESCS:
        return _WIN32_HOTKEY_ERROR_DESCS[err]
    return f"win32 error {err}" if err else "unknown error"

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SW_SHOWNOACTIVATE = 4
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011
HWND_TOPMOST = wintypes.HWND(-1)
WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# Comprehensive read-only Win32 virtual key table (zero hooks, zero injection)
VK_MAP: dict[str, int] = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    **{chr(c): c for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"NUM{d}": 0x60 + d for d in range(10)},
    "NUM*": 0x6A, "NUM+": 0x6B, "NUM-": 0x6D, "NUM.": 0x6E, "NUM/": 0x6F,
    "TAB": 0x09, "SPACE": 0x20,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "PAUSE": 0x13,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}

_CANONICAL_KEY_DISPLAY: dict[str, str] = {
    **{f"F{i}": f"F{i}" for i in range(1, 13)},
    **{chr(c): chr(c) for c in range(ord("A"), ord("Z") + 1)},
    **{str(d): str(d) for d in range(10)},
    **{f"NUM{d}": f"Num{d}" for d in range(10)},
    "NUM*": "Num*", "NUM+": "Num+", "NUM-": "Num-", "NUM.": "Num.", "NUM/": "Num/",
    "TAB": "Tab", "SPACE": "Space",
    "PAGEUP": "PageUp", "PAGEDOWN": "PageDown", "END": "End", "HOME": "Home",
    "LEFT": "Left", "UP": "Up", "RIGHT": "Right", "DOWN": "Down",
    "INSERT": "Insert", "PAUSE": "Pause",
    "`": "`", "-": "-", "=": "=", "[": "[", "]": "]",
    "\\": "\\", ";": ";", "'": "'", ",": ",", ".": ".", "/": "/",
}

_KEY_ALIASES: dict[str, str] = {
    "PGUP": "PAGEUP", "PRIOR": "PAGEUP",
    "PGDN": "PAGEDOWN", "NEXT": "PAGEDOWN",
    "INS": "INSERT",
    "TILDE": "`", "BACKQUOTE": "`", "BACKTICK": "`", "GRAVE": "`", "OEM_3": "`",
    "MINUS": "-", "PLUS": "=", "EQUAL": "=",
    "LBRACKET": "[", "RBRACKET": "]", "BACKSLASH": "\\",
    "SEMICOLON": ";", "APOSTROPHE": "'", "COMMA": ",", "PERIOD": ".", "SLASH": "/",
    "NUMPAD0": "NUM0", "NUMPAD1": "NUM1", "NUMPAD2": "NUM2", "NUMPAD3": "NUM3", "NUMPAD4": "NUM4",
    "NUMPAD5": "NUM5", "NUMPAD6": "NUM6", "NUMPAD7": "NUM7", "NUMPAD8": "NUM8", "NUMPAD9": "NUM9",
}

_MOD_ALIASES: dict[str, tuple[int, str]] = {
    "CTRL": (MOD_CONTROL, "Ctrl"),
    "CONTROL": (MOD_CONTROL, "Ctrl"),
    "ALT": (MOD_ALT, "Alt"),
    "OPTION": (MOD_ALT, "Alt"),
    "SHIFT": (MOD_SHIFT, "Shift"),
}


def parse_hotkey(spec: str) -> tuple[int, int, str] | None:
    """Parse hotkey string like 'F3' or 'Ctrl+Alt+S' into (win32_mods, vk_code, canonical_str).

    Returns None when spec is empty or unsupported.
    """
    raw = (spec or "").strip()
    if not raw:
        return None
    # Split on '+' while preserving a trailing '+' or 'Num+'
    tokens: list[str] = []
    cur = ""
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "+" and cur.strip() and cur.strip().upper() != "NUM":
            tokens.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        tokens.append(cur.strip())
    if not tokens:
        return None

    mods = 0
    seen_mods: set[str] = set()
    main_key = ""
    for tok in tokens:
        up = tok.upper()
        if up in _MOD_ALIASES:
            bit, cname = _MOD_ALIASES[up]
            mods |= bit
            seen_mods.add(cname)
        elif not main_key:
            main_key = _KEY_ALIASES.get(up, up)
        else:
            return None

    if not main_key:
        return None
    vk = VK_MAP.get(main_key)
    if vk is None:
        return None

    ordered_mods = [m for m in ("Ctrl", "Alt", "Shift") if m in seen_mods]
    disp_key = _CANONICAL_KEY_DISPLAY.get(main_key, main_key)
    canonical = "+".join([*ordered_mods, disp_key])
    return mods, vk, canonical


def normalize_hotkey(spec: str) -> str:
    """Return canonical hotkey text (e.g. 'Ctrl+Alt+X'), or '' if empty/invalid."""
    parsed = parse_hotkey(spec)
    return parsed[2] if parsed is not None else ""


def get_ex_style(hwnd: int) -> int:
    if not hwnd:
        return 0
    return int(_user32.GetWindowLongW(int(hwnd), GWL_EXSTYLE))


def apply_capture_exclusion(widget: QWidget, exclude: bool = True) -> bool:
    """Apply or clear WDA_EXCLUDEFROMCAPTURE on a widget's Win32 HWND."""
    if widget is None or QGuiApplication.platformName() == "offscreen":
        return False
    try:
        hwnd = int(widget.winId())
    except Exception:
        return False
    if not hwnd:
        return False
    affinity = WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE
    try:
        return bool(_user32.SetWindowDisplayAffinity(hwnd, affinity))
    except Exception as exc:
        logger.debug("SetWindowDisplayAffinity failed: %s", exc)
        return False


def apply_overlay_styles(
    widget: QWidget,
    click_through: bool = False,
    *,
    force: bool = True,
    exclude_from_capture: bool | None = None,
) -> int:
    """Apply non-activating topmost Win32 overlay styles (and optional full mouse pass-through).

    When force=False and GWL_EXSTYLE already carries all required bits (including WS_EX_TOPMOST),
    skip SetWindowLongW & SetWindowPos(SWP_FRAMECHANGED) to avoid forcing DWM surface recomposition.
    """
    if QGuiApplication.platformName() == "offscreen":
        return 0
    hwnd = int(widget.winId())
    if not hwnd:
        return 0
    current = get_ex_style(hwnd)
    desired = current | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
    if click_through:
        desired |= WS_EX_TRANSPARENT
    else:
        desired &= ~WS_EX_TRANSPARENT

    changed = desired != current
    if not force and not changed:
        if exclude_from_capture is not None:
            apply_capture_exclusion(widget, exclude=exclude_from_capture)
        return current

    if changed:
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, desired)
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | (SWP_FRAMECHANGED if changed else 0)
    _user32.SetWindowPos(
        hwnd,
        HWND_TOPMOST,
        0,
        0,
        0,
        0,
        flags,
    )
    style = get_ex_style(hwnd)
    if exclude_from_capture is not None:
        apply_capture_exclusion(widget, exclude=exclude_from_capture)
    return style


def screen_for_physical_rect(phys_rect: tuple[int, int, int, int] | None) -> QScreen | None:
    """Resolve the QScreen owning a physical pixel rect (left, top, width, height), supporting multi-monitor."""
    screens = QGuiApplication.screens()
    primary = QGuiApplication.primaryScreen()
    if phys_rect is None or not screens:
        return primary
    pl, pt, pw, ph = phys_rect
    pcx = pl + pw * 0.5
    pcy = pt + ph * 0.5
    for scr in screens:
        dpr = max(1.0, float(scr.devicePixelRatio() or 1.0))
        g = scr.geometry()
        sl = g.left() * dpr
        st = g.top() * dpr
        sr = sl + g.width() * dpr
        sb = st + g.height() * dpr
        if sl <= pcx <= sr and st <= pcy <= sb:
            return scr
    return primary


def physical_rect_to_logical(
    phys_rect: tuple[int, int, int, int],
    screen: QScreen | None = None,
) -> tuple[QRect, float]:
    """Convert physical pixel (left, top, width, height) into Qt logical QRect and DPR."""
    scr = screen or screen_for_physical_rect(phys_rect)
    dpr = max(1.0, float(scr.devicePixelRatio() if scr is not None else 1.0))
    pl, pt, pw, ph = phys_rect
    lx = int(round(pl / dpr))
    ly = int(round(pt / dpr))
    lw = max(1, int(round(pw / dpr)))
    lh = max(1, int(round(ph / dpr)))
    return QRect(lx, ly, lw, lh), dpr


def clamp_to_screen_rect(
    x: int,
    y: int,
    w: int,
    h: int,
    geo: QRect,
    *,
    margin: int = 8,
) -> tuple[int, int]:
    """Clamp top-left (x, y) so a (w, h) window stays fully inside geo with margin."""
    max_x = max(geo.left(), geo.right() - w + 1 - margin)
    max_y = max(geo.top(), geo.bottom() - h + 1 - margin)
    cx = max(geo.left() + margin, min(max_x, int(x)))
    cy = max(geo.top() + margin, min(max_y, int(y)))
    return cx, cy


def show_no_activate(
    widget: QWidget,
    click_through: bool = False,
    *,
    exclude_from_capture: bool | None = None,
) -> None:
    """Show a QWidget on top without stealing focus from the game window.

    No-ops Win32 frame-change calls when widget is already visible with matching extended styles.
    """
    widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    if widget.isVisible():
        apply_overlay_styles(
            widget,
            click_through=click_through,
            force=False,
            exclude_from_capture=exclude_from_capture,
        )
        return
    apply_overlay_styles(widget, click_through=click_through, force=False)
    widget.show()
    apply_overlay_styles(
        widget,
        click_through=click_through,
        force=True,
        exclude_from_capture=exclude_from_capture,
    )


def set_allow_keyboard_focus(widget: QWidget, allow: bool) -> bool:
    """Toggle WS_EX_NOACTIVATE on an overlay so interactive text inputs can gain keyboard focus on demand."""
    if widget is None or QGuiApplication.platformName() == "offscreen":
        return False
    try:
        hwnd = int(widget.winId())
    except Exception:
        return False
    if not hwnd:
        return False
    if allow:
        try:
            current = get_ex_style(hwnd)
            desired = (current & ~WS_EX_NOACTIVATE) | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, desired)
            _user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED,
            )
            widget.activateWindow()
            widget.raise_()
            return True
        except Exception as exc:
            logger.debug("set_allow_keyboard_focus(True) failed: %s", exc)
            return False
    apply_overlay_styles(widget, click_through=False, force=True)
    return True


def restore_foreground_window(hwnd: int | None) -> bool:
    """Safely return Win32 foreground focus to a non-minimized valid window handle."""
    if not hwnd:
        return False
    try:
        target = int(hwnd)
        if target <= 0:
            return False
        if not _user32.IsWindow(target) or _user32.IsIconic(target):
            return False
        return bool(_user32.SetForegroundWindow(target))
    except Exception as exc:
        logger.debug("restore_foreground_window failed: %s", exc)
        return False


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class GlobalHotkeyManager(QAbstractNativeEventFilter):
    """Read-only native Windows RegisterHotKey dispatcher (zero keyboard hook / zero injection)."""

    def __init__(self) -> None:
        super().__init__()
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._registered_specs: dict[int, str] = {}
        self.last_failed_hotkeys: dict[str, int] = {}
        self._next_id = 100
        self._suspended = False

    def set_suspended(self, suspended: bool) -> None:
        """Temporarily suspend dispatching while user is recording a new hotkey in the UI."""
        self._suspended = bool(suspended)

    def registered_hotkeys(self) -> set[str]:
        return set(self._registered_specs.values())

    def probe_available(self, key_name: str) -> tuple[bool, str]:
        """Probe whether Windows allows RegisterHotKey for key_name (skipping keys already owned by self)."""
        parsed = parse_hotkey(key_name)
        if parsed is None:
            return False, "不支持该按键"
        mods, vk, canonical = parsed
        if canonical in self._registered_specs.values():
            return True, ""
        probe_id = 0x7E00
        ctypes.set_last_error(0)
        ok = bool(_user32.RegisterHotKey(None, probe_id, mods | MOD_NOREPEAT, vk))
        if ok:
            try:
                _user32.UnregisterHotKey(None, probe_id)
            except Exception:
                pass
            return True, ""
        err = int(ctypes.get_last_error() or 0)
        if err and err != 1409:
            return False, f"「{canonical}」注册受限 (err={err}: {_describe_win32_hotkey_error(err)})"
        return False, f"「{canonical}」已被 Windows 或其他程序占用"

    def register(self, key_name: str, callback: Callable[[], None]) -> int | None:
        raw = (key_name or "").strip()
        if not raw:
            return None
        parsed = parse_hotkey(raw)
        if parsed is None:
            logger.warning("Unsupported hotkey: %s", key_name)
            return None
        mods, vk, canonical = parsed
        hk_id = self._next_id
        self._next_id += 1
        ctypes.set_last_error(0)
        ok = _user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk)
        if not ok:
            err = int(ctypes.get_last_error() or 0)
            self.last_failed_hotkeys[canonical] = err
            logger.warning(
                "RegisterHotKey failed for %s: err=%d (%s)",
                canonical,
                err,
                _describe_win32_hotkey_error(err),
            )
            return None
        self.last_failed_hotkeys.pop(canonical, None)
        self._callbacks[hk_id] = callback
        self._registered_specs[hk_id] = canonical
        return hk_id

    def unregister_all(self) -> None:
        for hk_id in list(self._callbacks.keys()):
            try:
                _user32.UnregisterHotKey(None, hk_id)
            except Exception:
                pass
        self._callbacks.clear()
        self._registered_specs.clear()
        self.last_failed_hotkeys.clear()

    def nativeEventFilter(self, event_type, message):  # noqa: N802
        if self._suspended:
            return False, 0
        try:
            if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = _MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    cb = self._callbacks.get(int(msg.wParam))
                    if cb is not None:
                        cb()
                        return True, 0
        except Exception as exc:
            logger.debug("Hotkey event error: %s", exc)
        return False, 0


class UiBridgeInvoker(QObject):
    """Thread-safe signal helper to marshal worker callbacks onto the Qt main thread."""

    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.invoke.connect(self._run)

    @staticmethod
    def _run(fn) -> None:
        if callable(fn):
            fn()
