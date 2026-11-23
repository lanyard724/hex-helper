"""Comprehensive tests for game-style custom hotkeys, 3-tier conflict analyzer, UI sync & dual-sidebar toggle."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from hex_helper.app import HexHelperController
from hex_helper.config import AppConfig, HotkeyConfig
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.hotkey_settings import (
    HOTKEY_ACTION_SPECS,
    HotkeySettingsDialog,
    analyze_hotkey_conflicts,
    dict_to_hotkey_config,
    format_hotkeys_summary,
    hotkeys_to_dict,
    qt_key_event_to_hotkey,
)
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.win32_window import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    VK_MAP,
    normalize_hotkey,
    parse_hotkey,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_parse_and_normalize_hotkey_variants() -> None:
    # Single F-keys, letters, digits, numpad & symbols
    assert normalize_hotkey("f3") == "F3"
    assert normalize_hotkey("  alt + shift + ctrl + s ") == "Ctrl+Alt+Shift+S"
    assert normalize_hotkey("control+option+backtick") == "Ctrl+Alt+`"
    assert normalize_hotkey("numpad0") == "Num0"
    assert normalize_hotkey("shift+pgup") == "Shift+PageUp"
    assert normalize_hotkey("") == ""
    assert normalize_hotkey("invalid_key_xyz") == ""
    assert normalize_hotkey("Ctrl+Alt") == ""  # modifier-only is invalid

    parsed = parse_hotkey("Ctrl+Alt+Shift+F9")
    assert parsed is not None
    mods, vk, canonical = parsed
    assert mods == (MOD_CONTROL | MOD_ALT | MOD_SHIFT)
    assert vk == VK_MAP["F9"]
    assert canonical == "Ctrl+Alt+Shift+F9"


def test_qt_key_event_conversion() -> None:
    # Modifier-only press ignored
    ev_mod = QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Control), Qt.KeyboardModifier.ControlModifier)
    assert qt_key_event_to_hotkey(ev_mod) == ("ignore", None)

    # Esc cancels
    ev_esc = QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Escape), Qt.KeyboardModifier.NoModifier)
    assert qt_key_event_to_hotkey(ev_esc) == ("cancel", None)

    # Bare Backspace / Delete unbinds
    ev_bs = QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Backspace), Qt.KeyboardModifier.NoModifier)
    assert qt_key_event_to_hotkey(ev_bs) == ("clear", "")
    ev_del = QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Delete), Qt.KeyboardModifier.NoModifier)
    assert qt_key_event_to_hotkey(ev_del) == ("clear", "")

    # Ctrl+Alt+S
    ev_combo = QKeyEvent(
        QEvent.Type.KeyPress,
        int(Qt.Key.Key_S),
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
    )
    assert qt_key_event_to_hotkey(ev_combo) == ("bound", "Ctrl+Alt+S")

    # Shift+Tab (Qt emits Key_Backtab on Windows)
    ev_btab = QKeyEvent(
        QEvent.Type.KeyPress,
        int(Qt.Key.Key_Backtab),
        Qt.KeyboardModifier.ShiftModifier,
    )
    assert qt_key_event_to_hotkey(ev_btab) == ("bound", "Shift+Tab")

    # Numpad 0
    ev_num = QKeyEvent(
        QEvent.Type.KeyPress,
        int(Qt.Key.Key_0),
        Qt.KeyboardModifier.KeypadModifier,
    )
    assert qt_key_event_to_hotkey(ev_num) == ("bound", "Num0")

    # Shifted symbol (Shift+1 -> Key_Exclam)
    ev_shift1 = QKeyEvent(
        QEvent.Type.KeyPress,
        int(Qt.Key.Key_Exclam),
        Qt.KeyboardModifier.ShiftModifier,
    )
    assert qt_key_event_to_hotkey(ev_shift1) == ("bound", "Shift+1")


def test_analyze_hotkey_conflicts_three_tiers() -> None:
    bindings = hotkeys_to_dict(HotkeyConfig())
    clean = analyze_hotkey_conflicts(bindings)
    assert not clean.has_blocking_errors
    assert not clean.internal_conflicts
    assert not clean.reserved_errors
    assert not clean.lol_warnings

    # 1) Internal duplicate blocks save
    dup = dict(bindings)
    dup["toggle_drawer"] = "F7"  # collides with toggle_combo
    rep_dup = analyze_hotkey_conflicts(dup)
    assert rep_dup.has_blocking_errors
    assert "toggle_combo" in rep_dup.internal_conflicts["toggle_drawer"]
    assert "toggle_drawer" in rep_dup.internal_conflicts["toggle_combo"]

    # 2) Reserved system shortcut blocks save
    res = dict(bindings)
    res["toggle_sidebars"] = "Alt+F4"
    rep_res = analyze_hotkey_conflicts(res)
    assert rep_res.has_blocking_errors
    assert "toggle_sidebars" in rep_res.reserved_errors

    # 3) Bare LOL key emits soft warning (does NOT block save); adding Alt clears warning
    lol = dict(bindings)
    lol["toggle_sidebars"] = "Q"
    rep_lol = analyze_hotkey_conflicts(lol)
    assert not rep_lol.has_blocking_errors
    assert "toggle_sidebars" in rep_lol.lol_warnings

    lol_safe = dict(bindings)
    lol_safe["toggle_sidebars"] = "Alt+Q"
    rep_safe = analyze_hotkey_conflicts(lol_safe)
    assert "toggle_sidebars" not in rep_safe.lol_warnings

    # 4) External OS occupancy reported as soft notice
    rep_occ = analyze_hotkey_conflicts(bindings, os_occupied={"toggle_drawer": "「F3」已被其他程序占用"})
    assert not rep_occ.has_blocking_errors
    assert "toggle_drawer" in rep_occ.os_occupied


def test_hotkey_config_roundtrip_and_legacy_compat() -> None:
    # Legacy config.json only had 6 keys -> new fields get clean defaults
    legacy = AppConfig.from_dict({"hotkeys": {"toggle_drawer": "Alt+D", "call_coach": ""}})
    assert legacy.hotkeys.toggle_drawer == "Alt+D"
    assert legacy.hotkeys.call_coach == ""  # explicit unbind preserved!
    assert legacy.hotkeys.toggle_sidebars == "F9"
    assert legacy.hotkeys.toggle_lookup == ""
    assert legacy.hotkeys.open_recap == ""

    # Full 9-key roundtrip
    custom = HotkeyConfig(
        force_scan="Ctrl+F2",
        toggle_drawer="Alt+]",
        toggle_all="Ctrl+Alt+H",
        call_coach="Num0",
        toggle_combo="Alt+[",
        calibrate_roi="",
        toggle_sidebars="Alt+`",
        toggle_lookup="Ctrl+Shift+H",
        open_recap="Alt+R",
    )
    cfg = AppConfig(hotkeys=custom)
    restored = AppConfig.from_dict(cfg.to_dict())
    assert restored.hotkeys == custom


def test_hotkey_settings_dialog_record_swap_overwrite_undo_and_probe(qapp) -> None:
    suspend_log: list[bool] = []
    occupied_probe_keys = {"Alt+X"}

    def fake_probe(k: str) -> tuple[bool, str]:
        if k in occupied_probe_keys:
            return False, f"「{k}」已被系统或其它软件占用"
        return True, ""

    dlg = HotkeySettingsDialog(
        HotkeyConfig(),
        probe_available=fake_probe,
        on_suspend_hotkeys=lambda flag: suspend_log.append(flag),
    )
    assert len(dlg._capture_btns) == len(HOTKEY_ACTION_SPECS) == 9
    assert dlg.save_btn.isEnabled()
    assert dlg.conflict_banner.isHidden()

    # 1) Start recording on toggle_drawer -> suspend(True), Esc cancels -> suspend(False)
    btn_drawer = dlg._capture_btns["toggle_drawer"]
    btn_drawer.start_recording()
    assert btn_drawer.is_recording
    assert suspend_log == [True]
    btn_drawer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Escape), Qt.KeyboardModifier.NoModifier))
    assert not btn_drawer.is_recording
    assert btn_drawer.key_text == "F3"
    assert suspend_log == [True, False]

    # 2) Record F7 on toggle_drawer (collides with toggle_combo=F7) -> conflict banner + Swap!
    btn_drawer.start_recording()
    btn_drawer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_F7), Qt.KeyboardModifier.NoModifier))
    assert not dlg.save_btn.isEnabled()
    assert not dlg.conflict_banner.isHidden()
    assert not dlg.swap_btn.isHidden()
    dlg._resolve_swap()
    assert dlg._draft["toggle_drawer"] == "F7"
    assert dlg._draft["toggle_combo"] == "F3"
    assert dlg.save_btn.isEnabled()
    assert dlg.conflict_banner.isHidden()

    # 3) Record F9 on toggle_drawer (collides with toggle_sidebars=F9) -> Overwrite unbinds opponent
    btn_drawer.start_recording()
    btn_drawer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_F9), Qt.KeyboardModifier.NoModifier))
    assert not dlg.save_btn.isEnabled()
    dlg._resolve_overwrite()
    assert dlg._draft["toggle_drawer"] == "F9"
    assert dlg._draft["toggle_sidebars"] == ""
    assert dlg.save_btn.isEnabled()

    # 4) Record F2 on toggle_drawer (collides with force_scan=F2) -> Undo reverts to F9
    btn_drawer.start_recording()
    btn_drawer.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_F2), Qt.KeyboardModifier.NoModifier))
    assert not dlg.save_btn.isEnabled()
    dlg._resolve_undo()
    assert dlg._draft["toggle_drawer"] == "F9"
    assert dlg._draft["force_scan"] == "F2"
    assert dlg.save_btn.isEnabled()

    # 5) Record OS-occupied Alt+X -> marks occupied soft warning (does not block save)
    btn_side = dlg._capture_btns["toggle_sidebars"]
    btn_side.start_recording()
    btn_side.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_X), Qt.KeyboardModifier.AltModifier))
    assert "toggle_sidebars" in dlg._os_occupied
    assert not dlg.notice_banner.isHidden()
    assert dlg.save_btn.isEnabled()

    # 6) Record LOL bare key Q on toggle_lookup -> LOL warning shown
    btn_lookup = dlg._capture_btns["toggle_lookup"]
    btn_lookup.start_recording()
    btn_lookup.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, int(Qt.Key.Key_Q), Qt.KeyboardModifier.NoModifier))
    assert dlg._draft["toggle_lookup"] == "Q"
    assert not dlg.notice_banner.isHidden()

    # 7) Reset defaults restores all 9 defaults cleanly
    dlg.reset_defaults()
    assert dict_to_hotkey_config(dlg._draft) == HotkeyConfig()
    assert dlg.save_btn.isEnabled()
    assert dlg.conflict_banner.isHidden()
    assert dlg.notice_banner.isHidden()

    # 8) Apply & save yields valid result_hotkeys
    dlg._draft["toggle_sidebars"] = "Alt+`"
    dlg._on_save_clicked()
    assert dlg.result_hotkeys is not None
    assert dlg.result_hotkeys.toggle_sidebars == "Alt+`"
    dlg.close()


def test_ui_hotkey_label_sync_and_no_tactile_orange(qapp) -> None:
    cfg = AppConfig()
    lw = LookupWindow(cfg)
    drawer = EdgeBuildDrawer()
    combo = AugmentComboOverlay()

    custom_hk = HotkeyConfig(
        force_scan="Alt+F2",
        toggle_drawer="Alt+D",
        toggle_all="Alt+H",
        call_coach="Num0",
        toggle_combo="Alt+C",
        calibrate_roi="Ctrl+F8",
        toggle_sidebars="Alt+`",
        toggle_lookup="Ctrl+L",
        open_recap="Alt+R",
    )
    lw.update_hotkeys_display(custom_hk)
    drawer.update_hotkey_labels(custom_hk)
    combo.update_hotkey_labels(custom_hk)

    assert "Ctrl+F8" in lw.cal_btn.text()
    assert "Num0" in lw.footer_coach_btn.text()
    assert "Alt+C" in lw.toggle_combo_btn.text()
    assert "Alt+`" in lw.hotkeys_summary_lbl.text()
    assert drawer.coach_btn.text() == "Num0"
    assert "Alt+D" in drawer.close_btn.toolTip()
    assert "Alt+C" in combo.pill_close_btn.toolTip()
    assert "Alt+`" in combo.pill.toolTip()

    # Long modifier combo shortens right-drawer header button to "AI" cleanly without overflow
    custom_hk.call_coach = "Ctrl+Alt+F6"
    drawer.update_hotkey_labels(custom_hk)
    assert drawer.coach_btn.text() == "AI"
    assert "Ctrl+Alt+F6" in drawer.coach_btn.toolTip()

    # Strictly zero Tactile Orange (#FF5A1F) in desktop hotkey UI module
    src_path = Path(__file__).resolve().parents[1] / "src" / "hex_helper" / "ui" / "hotkey_settings.py"
    assert "#FF5A1F" not in src_path.read_text(encoding="utf-8").upper()

    lw.close()
    drawer.close()
    combo.close()


def test_dual_sidebar_toggle_state_machine(qapp) -> None:
    ctrl = object.__new__(HexHelperController)
    ctrl.cfg = AppConfig(combo_overlay_enabled=True)
    ctrl._overlays_hidden_by_user = False
    ctrl._combo_hidden_by_user = False
    ctrl._commercial = False
    ctrl._service_locked = False
    ctrl._force_update_required = False
    ctrl._cloud_ai_cutoff = False
    ctrl._cloud_cutoff_policy = "ai_only"
    ctrl.edge_drawer = EdgeBuildDrawer()
    ctrl.combo_overlay = AugmentComboOverlay()
    ctrl._refresh_combo_plan_view = lambda: None  # type: ignore[method-assign]

    # Initially hidden/collapsed -> first press expands & shows BOTH sidebars
    ctrl._toggle_sidebars_hotkey()
    assert ctrl.edge_drawer.isVisible() and ctrl.edge_drawer.is_expanded
    assert ctrl.combo_overlay.isVisible() and ctrl.combo_overlay.is_expanded

    # Second press -> collapses BOTH visible sidebars to 48px pills
    ctrl._toggle_sidebars_hotkey()
    assert not ctrl.edge_drawer.is_expanded
    assert not ctrl.combo_overlay.is_expanded

    # Third press (when only right sidebar is expanded) -> collapses right cleanly (quick clear)
    ctrl.edge_drawer.set_expanded(True)
    ctrl._toggle_sidebars_hotkey()
    assert not ctrl.edge_drawer.is_expanded
    assert not ctrl.combo_overlay.is_expanded

    # Fourth press (both collapsed) -> expands BOTH again
    ctrl._toggle_sidebars_hotkey()
    assert ctrl.edge_drawer.is_expanded
    assert ctrl.combo_overlay.is_expanded

    ctrl.edge_drawer.close()
    ctrl.combo_overlay.close()


def test_format_hotkeys_summary_handles_all_unbound() -> None:
    empty = HotkeyConfig(
        force_scan="",
        toggle_drawer="",
        toggle_all="",
        call_coach="",
        toggle_combo="",
        calibrate_roi="",
        toggle_sidebars="",
        toggle_lookup="",
        open_recap="",
    )
    txt = format_hotkeys_summary(empty)
    assert "解绑" in txt
