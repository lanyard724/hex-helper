"""Tests for Hextech QMenu/QToolTip dark QSS, Win32 capture exclusion & focus control, and settings sync."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QWidget
import pytest

from hex_helper.config import AppConfig, load_config, save_config
from hex_helper.scoring.tier import AugmentScore
from hex_helper.ui import augment_overlay as aug_mod
from hex_helper.ui import champ_select_ui as cs_mod
from hex_helper.ui import lookup_window as lookup_mod
from hex_helper.ui import win32_window
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.theme import BASE_OVERLAY_QSS, HEXTECH_MENU_QSS
from hex_helper.vision import capture as capture_mod


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_config_allow_screen_recording_overlays_default_and_roundtrip(tmp_path: Path) -> None:
    default_cfg = AppConfig()
    assert default_cfg.allow_screen_recording_overlays is False

    missing_key_cfg = AppConfig.from_dict({})
    assert missing_key_cfg.allow_screen_recording_overlays is False

    truthy_cfg = AppConfig.from_dict({"allow_screen_recording_overlays": True})
    assert truthy_cfg.allow_screen_recording_overlays is True

    cfg_path = tmp_path / "config.json"
    save_config(truthy_cfg, cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.allow_screen_recording_overlays is True


def test_theme_exports_dark_qmenu_and_qtooltip_without_forbidden_orange() -> None:
    assert isinstance(HEXTECH_MENU_QSS, str) and len(HEXTECH_MENU_QSS.strip()) > 0
    for selector in (
        "QMenu",
        "QMenu::item",
        "QMenu::item:selected",
        "QMenu::item:disabled",
        "QMenu::separator",
        "QMenu::indicator",
        "QMenu::indicator:checked",
    ):
        assert selector in HEXTECH_MENU_QSS
        assert selector in BASE_OVERLAY_QSS

    for color in ("#061222", "#785A28", "#F0E6D2", "#162B40", "#C8AA6E", "#5B5A56", "#463714", "#010A13", "#0AC8B9", "#0397AB"):
        assert color in HEXTECH_MENU_QSS
        assert color in BASE_OVERLAY_QSS

    assert "QToolTip" in BASE_OVERLAY_QSS
    assert "#FF5A1F" not in HEXTECH_MENU_QSS.upper()
    assert "#FF5A1F" not in BASE_OVERLAY_QSS.upper()


def test_win32_capture_exclusion_focus_and_foreground_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _qt_app()
    assert win32_window.WDA_NONE == 0x00000000
    assert win32_window.WDA_EXCLUDEFROMCAPTURE == 0x00000011

    w = QWidget()
    w.resize(160, 80)

    # Under offscreen, capture exclusion and keyboard focus toggling must safely no-op and return False
    monkeypatch.setattr(win32_window.QGuiApplication, "platformName", staticmethod(lambda: "offscreen"))
    assert win32_window.apply_capture_exclusion(w, exclude=True) is False
    assert win32_window.set_allow_keyboard_focus(w, True) is False

    class _FakeUser32:
        def __init__(self) -> None:
            self.ex_styles: dict[int, int] = {}
            self.affinity_calls: list[tuple[int, int]] = []
            self.set_pos_calls: list[tuple[int, int]] = []
            self.valid_windows: set[int] = {9001}
            self.iconic_windows: set[int] = set()
            self.foreground_calls: list[int] = []
            self.raise_affinity_error = False

        def GetWindowLongW(self, hwnd: int, _idx: int) -> int:  # noqa: N802
            return self.ex_styles.get(int(hwnd), 0)

        def SetWindowLongW(self, hwnd: int, _idx: int, value: int) -> int:  # noqa: N802
            prev = self.ex_styles.get(int(hwnd), 0)
            self.ex_styles[int(hwnd)] = int(value)
            return prev

        def SetWindowPos(self, hwnd: int, _after, _x, _y, _cx, _cy, flags: int) -> int:  # noqa: N802
            self.set_pos_calls.append((int(hwnd), int(flags)))
            return 1

        def SetWindowDisplayAffinity(self, hwnd: int, affinity: int) -> int:  # noqa: N802
            if self.raise_affinity_error:
                raise OSError("affinity failed")
            self.affinity_calls.append((int(hwnd), int(affinity)))
            return 1

        def IsWindow(self, hwnd: int) -> int:  # noqa: N802
            return 1 if int(hwnd) in self.valid_windows else 0

        def IsIconic(self, hwnd: int) -> int:  # noqa: N802
            return 1 if int(hwnd) in self.iconic_windows else 0

        def SetForegroundWindow(self, hwnd: int) -> int:  # noqa: N802
            self.foreground_calls.append(int(hwnd))
            return 1

    fake_u32 = _FakeUser32()
    monkeypatch.setattr(win32_window, "_user32", fake_u32)
    monkeypatch.setattr(win32_window.QGuiApplication, "platformName", staticmethod(lambda: "windows"))

    # Default show_no_activate keeps backward compatibility (does NOT call SetWindowDisplayAffinity when None)
    win32_window.show_no_activate(w, click_through=False)
    app.processEvents()
    hwnd = int(w.winId())
    assert hwnd > 0
    assert fake_u32.affinity_calls == []
    assert fake_u32.ex_styles[hwnd] & win32_window.WS_EX_NOACTIVATE

    # Passing exclude_from_capture=True / False triggers WDA_EXCLUDEFROMCAPTURE / WDA_NONE after show
    w.hide()
    win32_window.show_no_activate(w, click_through=False, exclude_from_capture=True)
    app.processEvents()
    assert fake_u32.affinity_calls[-1] == (hwnd, win32_window.WDA_EXCLUDEFROMCAPTURE)

    assert win32_window.apply_capture_exclusion(w, exclude=False) is True
    assert fake_u32.affinity_calls[-1] == (hwnd, win32_window.WDA_NONE)

    fake_u32.raise_affinity_error = True
    assert win32_window.apply_capture_exclusion(w, exclude=True) is False
    fake_u32.raise_affinity_error = False

    # set_allow_keyboard_focus(True) removes WS_EX_NOACTIVATE, applies SWP_FRAMECHANGED, and activates/raises
    activate_calls: list[bool] = []
    raise_calls: list[bool] = []
    monkeypatch.setattr(w, "activateWindow", lambda: activate_calls.append(True))
    monkeypatch.setattr(w, "raise_", lambda: raise_calls.append(True))

    assert win32_window.set_allow_keyboard_focus(w, True) is True
    assert not (fake_u32.ex_styles[hwnd] & win32_window.WS_EX_NOACTIVATE)
    assert fake_u32.ex_styles[hwnd] & win32_window.WS_EX_TOOLWINDOW
    assert fake_u32.ex_styles[hwnd] & win32_window.WS_EX_TOPMOST
    assert fake_u32.set_pos_calls[-1][1] & win32_window.SWP_FRAMECHANGED
    assert activate_calls == [True]
    assert raise_calls == [True]

    # set_allow_keyboard_focus(False) restores WS_EX_NOACTIVATE
    assert win32_window.set_allow_keyboard_focus(w, False) is True
    assert fake_u32.ex_styles[hwnd] & win32_window.WS_EX_NOACTIVATE

    # restore_foreground_window safety guards
    assert win32_window.restore_foreground_window(None) is False
    assert win32_window.restore_foreground_window(0) is False
    assert win32_window.restore_foreground_window(9999) is False  # not IsWindow
    fake_u32.iconic_windows.add(9001)
    assert win32_window.restore_foreground_window(9001) is False  # minimized
    fake_u32.iconic_windows.discard(9001)
    assert win32_window.restore_foreground_window(9001) is True
    assert fake_u32.foreground_calls == [9001]

    w.close()
    app.processEvents()


def test_is_game_or_overlay_foreground_logic(monkeypatch: pytest.MonkeyPatch) -> None:
    my_pid = os.getpid()

    class _FakeCaptureUser32:
        def __init__(self) -> None:
            self.fg = 0
            self.valid_windows: set[int] = set()
            self.iconic_windows: set[int] = set()
            self.pids: dict[int, int] = {}

        def GetForegroundWindow(self) -> int:  # noqa: N802
            return self.fg

        def IsWindow(self, hwnd: int) -> int:  # noqa: N802
            return 1 if int(hwnd) in self.valid_windows else 0

        def IsIconic(self, hwnd: int) -> int:  # noqa: N802
            return 1 if int(hwnd) in self.iconic_windows else 0

        def GetWindowThreadProcessId(self, hwnd: int, pid_ptr) -> int:  # noqa: N802
            pid_val = self.pids.get(int(hwnd), 0)
            ctypes.cast(pid_ptr, ctypes.POINTER(wintypes.DWORD))[0] = pid_val
            return 1

    fake = _FakeCaptureUser32()
    monkeypatch.setattr(capture_mod, "_user32", fake)

    # 1. Mock compatibility: fake hwnd=1 foreground succeeds even when IsWindow(1) is False
    fake.fg = 1
    assert capture_mod.is_game_or_overlay_foreground(1) is True

    # 2. Zero game hwnd must never match
    fake.fg = 0
    assert capture_mod.is_game_or_overlay_foreground(0) is False

    # 3. Real game hwnd=100, foreground is our non-excluded overlay hwnd=200 (same PID)
    fake.valid_windows.add(100)
    fake.fg = 200
    fake.pids[200] = my_pid
    assert capture_mod.is_game_or_overlay_foreground(100, exclude_hwnds=(300,)) is True

    # 4. Excluded main window hwnd=300 (same PID) -> False
    fake.fg = 300
    fake.pids[300] = my_pid
    assert capture_mod.is_game_or_overlay_foreground(100, exclude_hwnds={300}) is False

    # 5. Foreign application hwnd=400 -> False
    fake.fg = 400
    fake.pids[400] = my_pid + 999
    assert capture_mod.is_game_or_overlay_foreground(100) is False

    # 6. Minimized game window while overlay is foreground -> False
    fake.fg = 200
    fake.iconic_windows.add(100)
    assert capture_mod.is_game_or_overlay_foreground(100) is False

    # 7. Closed game window -> False
    fake.iconic_windows.clear()
    fake.valid_windows.clear()
    assert capture_mod.is_game_or_overlay_foreground(100) is False


def test_augment_overlay_capture_exclusion_and_28px_clearance(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _qt_app()
    show_calls: list[tuple[bool, bool | None]] = []
    excl_calls: list[bool] = []

    def _spy_show(widget, click_through=False, *, exclude_from_capture=None):
        show_calls.append((bool(click_through), exclude_from_capture))
        widget.show()

    def _spy_excl(_widget, exclude=True):
        excl_calls.append(bool(exclude))
        return True

    monkeypatch.setattr(aug_mod, "show_no_activate", _spy_show)
    monkeypatch.setattr(aug_mod, "apply_capture_exclusion", _spy_excl)

    ov = AugmentBadgesOverlay()
    assert ov._allow_screen_recording is False

    items = [
        AugmentBadgeRenderItem(
            slot_index=0,
            score=AugmentScore("1001", "珠光护手", "gold", "S", 0.56, 0.54, 0.04, 8000, False, percentile=92.0),
            screen_box=(400, 420, 660, 460),
            is_best=True,
        )
    ]
    ov.render_cards(items, screen_rect=(0, 0, 1920, 1080))
    app.processEvents()

    assert show_calls[-1] == (True, True)
    card = ov._badges[0]
    card_top_in_screen = ov.y() + card.y()
    # Badge bottom must keep >= 28px clearance above card top_y (420)
    assert 420 - (card_top_in_screen + card.height()) >= 28

    ov.set_screen_recording_allowed(True)
    assert ov._allow_screen_recording is True
    assert excl_calls[-1] is False

    # Re-render with updated screen box passes exclude_from_capture=False
    items2 = [
        AugmentBadgeRenderItem(
            slot_index=0,
            score=AugmentScore("1001", "珠光护手", "gold", "S", 0.56, 0.54, 0.04, 8000, False, percentile=92.0),
            screen_box=(410, 420, 670, 460),
            is_best=True,
        )
    ]
    ov.render_cards(items2, screen_rect=(0, 0, 1920, 1080))
    assert show_calls[-1] == (True, False)

    ov.close()
    app.processEvents()


def test_champ_select_overlay_capture_exclusion_and_dark_qmenu(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _qt_app()
    show_calls: list[tuple[bool, bool | None]] = []
    excl_calls: list[bool] = []
    captured_menus: list[QMenu] = []

    monkeypatch.setattr(
        cs_mod,
        "show_no_activate",
        lambda widget, click_through=False, *, exclude_from_capture=None: (
            show_calls.append((bool(click_through), exclude_from_capture)),
            widget.show(),
        ),
    )
    monkeypatch.setattr(
        cs_mod,
        "apply_capture_exclusion",
        lambda _widget, exclude=True: excl_calls.append(bool(exclude)) or True,
    )

    class _FakeMenu(QMenu):
        def exec(self, *_args, **_kwargs):
            captured_menus.append(self)
            return None

    monkeypatch.setattr(cs_mod, "QMenu", _FakeMenu)

    ov = ChampSelectOverlay()
    assert ov._allow_screen_recording is False
    ov.show_overlay()
    app.processEvents()
    assert show_calls[-1] == (False, True)

    ov.set_screen_recording_allowed(True)
    assert ov._allow_screen_recording is True
    assert excl_calls[-1] is False

    ov.show_overlay()
    assert show_calls[-1] == (False, False)

    evt = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(20, 20),
        QPointF(20, 20),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    ov.mousePressEvent(evt)
    assert len(captured_menus) == 1
    assert captured_menus[0].styleSheet() == HEXTECH_MENU_QSS

    ov.close()
    app.processEvents()


def test_lookup_window_screen_recording_checkbox_layout_save_and_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _qt_app()
    msg_calls: list[tuple[str, tuple]] = []

    def _make_msg_stub(kind: str):
        def _stub(*args, **_kwargs):
            msg_calls.append((kind, args))
            return QMessageBox.StandardButton.Ok

        return _stub

    for kind in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, kind, staticmethod(_make_msg_stub(kind)))
        if hasattr(lookup_mod, "QMessageBox"):
            monkeypatch.setattr(lookup_mod.QMessageBox, kind, staticmethod(_make_msg_stub(kind)))

    saved_cfgs: list[AppConfig] = []
    changed_events: list[dict] = []

    cfg = AppConfig()
    assert cfg.allow_screen_recording_overlays is False

    lw = LookupWindow(
        cfg=cfg,
        on_save_settings=lambda c: saved_cfgs.append(c),
        on_settings_changed=lambda ch: changed_events.append(ch),
    )
    lw.resize(940, 610)
    lw._switch_page(3, force=True)
    lw.show()
    app.processEvents()

    assert hasattr(lw, "screen_recording_chk")
    assert lw.screen_recording_chk.text() == "录屏/截屏中显示游戏内悬浮窗"
    assert "OBS" in lw.screen_recording_chk.toolTip()
    assert lw.screen_recording_chk.isChecked() is False
    assert lw._safe_settings_snapshot()["allow_screen_recording_overlays"] is False

    # Layout check at 940x610 minimum size: checkbox visible, zero overlap with telemetry & band row, diag_text uncrushed
    chk_geo = lw.screen_recording_chk.geometry()
    assert chk_geo.width() > 100 and chk_geo.height() > 14
    assert not chk_geo.intersects(lw.telemetry_chk.geometry())
    assert not chk_geo.intersects(lw.data_band_combo.geometry())
    assert lw.diag_text.height() >= 80, f"diag_text crushed at 940x610: {lw.diag_text.height()}px"

    # Toggle and save persists config & notifies settings change
    lw.screen_recording_chk.setChecked(True)
    lw._save_clicked()
    assert not any(kind in ("warning", "critical") for kind, _ in msg_calls)
    assert saved_cfgs and saved_cfgs[-1].allow_screen_recording_overlays is True
    assert changed_events and changed_events[-1].get("allow_screen_recording_overlays") is True
    assert lw._safe_settings_snapshot()["allow_screen_recording_overlays"] is True

    # External menu toggle syncs checkbox + backing config
    lw.sync_screen_recording_checkbox(False)
    assert lw.screen_recording_chk.isChecked() is False
    assert lw._cfg.allow_screen_recording_overlays is False
    assert lw._safe_settings_snapshot()["allow_screen_recording_overlays"] is False

    lw.close()
    app.processEvents()
