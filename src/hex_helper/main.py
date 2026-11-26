"""HexHelper desktop entrypoint with Windows single-instance mutex, preflight check, and logging."""

from __future__ import annotations

import argparse
import ctypes
import inspect
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from hex_helper.config import get_app_data_dir, load_config, save_config
from hex_helper.edition import is_commercial_edition, is_devlab_allowed
from hex_helper.logging_ring import install_match_log_handler
from hex_helper.setup.preflight import run_windows_preflight

ERROR_ALREADY_EXISTS = 183


def _setup_logging() -> None:
    data_dir = get_app_data_dir()
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_dir / "hex_helper.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    install_match_log_handler(data_dir=data_dir)
    # Silence noisy per-second 2999/LCU httpx request logs so hex_helper.log stays clean & actionable
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _show_fatal_box(title: str, text: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def _acquire_single_instance_mutex() -> int | None:
    kernel32 = ctypes.windll.kernel32
    mutex_name = os.environ.get("HEX_SINGLE_INSTANCE_MUTEX") or "Local\\HexHelperSingleInstance"
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def _run_preflight_cli(report_out: str | None) -> int:
    data_dir = get_app_data_dir()
    config_path = data_dir / "config.json"
    cfg = load_config(config_path)
    report = run_windows_preflight(cfg, data_dir)
    if report.lol_install is not None:
        save_config(cfg, config_path)
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if report_out:
        out_path = Path(report_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload, encoding="utf-8")
    if sys.stdout is not None:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                pass
        print(payload)
    return 0


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HexHelper - ARAM Mayhem overlay assistant")
    parser.add_argument("--minimized", action="store_true", help="Start minimized to system tray")
    parser.add_argument(
        "--preflight-check-only",
        action="store_true",
        help="Run Windows dependency preflight, output JSON report, and exit without GUI",
    )
    parser.add_argument(
        "--preflight-report-out",
        default="",
        help="Optional output JSON file path for --preflight-check-only",
    )
    parser.add_argument(
        "--no-wizard",
        action="store_true",
        help="Skip the first-run setup wizard dialog on startup",
    )
    parser.add_argument(
        "--devlab",
        action="store_true",
        help="Enable local DevLab flight recorder (non-commercial dev builds only)",
    )
    parser.add_argument(
        "--devlab-roi-snapshots",
        action="store_true",
        help="Enable bounded anomaly ROI crop snapshots in DevLab mode",
    )
    parser.add_argument(
        "--devlab-debug",
        action="store_true",
        help="Enable isolated per-match DEBUG runtime log in DevLab mode",
    )
    return parser


def resolve_devlab_flags(args: argparse.Namespace) -> dict[str, bool]:
    """Resolve effective DevLab flags, unconditionally returning False in commercial builds."""
    if is_commercial_edition() or not is_devlab_allowed():
        return {
            "devlab": False,
            "devlab_roi_snapshots": False,
            "devlab_debug": False,
        }
    return {
        "devlab": bool(getattr(args, "devlab", False)) or _env_truthy("HEX_DEVLAB"),
        "devlab_roi_snapshots": bool(getattr(args, "devlab_roi_snapshots", False)) or _env_truthy("HEX_DEVLAB_ROI"),
        "devlab_debug": bool(getattr(args, "devlab_debug", False)) or _env_truthy("HEX_DEVLAB_DEBUG"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.preflight_check_only:
        return _run_preflight_cli(args.preflight_report_out or None)

    devlab_flags = resolve_devlab_flags(args)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QDialog

    from hex_helper.app import HexHelperController
    from hex_helper.ui.setup_wizard import SetupWizardDialog
    from hex_helper.ui.theme import init_hextech_fonts, load_app_icon

    _setup_logging()
    logger = logging.getLogger(__name__)
    mutex = _acquire_single_instance_mutex()
    if mutex is None:
        logger.info("HexHelper is already running; skipping duplicate launch.")
        return 0

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("HexHelper.AramMayhem.Client.1")
    except Exception:
        pass

    data_dir = get_app_data_dir()
    config_path = data_dir / "config.json"
    had_config = config_path.exists()
    cfg = load_config(config_path)
    report = run_windows_preflight(cfg, data_dir)
    if report.lol_install is not None or not had_config:
        save_config(cfg, config_path)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    qt_app = QApplication(sys.argv)
    init_hextech_fonts()
    qt_app.setWindowIcon(load_app_icon())
    qt_app.setQuitOnLastWindowClosed(False)

    if not args.no_wizard and (not had_config or not report.all_critical_ok or not cfg.lockfile_path):
        wizard = SetupWizardDialog(cfg, report, data_dir)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            ctypes.windll.kernel32.CloseHandle(mutex)
            return 0

    try:
        controller_kwargs: dict[str, Any] = {"show_lookup_on_start": not args.minimized}
        if not is_commercial_edition() and is_devlab_allowed():
            if devlab_flags["devlab"]:
                os.environ["HEX_DEVLAB"] = "1"
            if devlab_flags["devlab_roi_snapshots"]:
                os.environ["HEX_DEVLAB_ROI"] = "1"
            if devlab_flags["devlab_debug"]:
                os.environ["HEX_DEVLAB_DEBUG"] = "1"
            sig_params = inspect.signature(HexHelperController.__init__).parameters
            if "devlab_enabled" in sig_params:
                controller_kwargs["devlab_enabled"] = devlab_flags["devlab"]
            if "devlab_roi_snapshots" in sig_params:
                controller_kwargs["devlab_roi_snapshots"] = devlab_flags["devlab_roi_snapshots"]
            if "devlab_debug_log" in sig_params:
                controller_kwargs["devlab_debug_log"] = devlab_flags["devlab_debug"]
        controller = HexHelperController(qt_app, **controller_kwargs)
        port_info = controller.bridge.bound_port if controller.bridge is not None else "cloud"
        logger.info("HexHelper started successfully (mode=%s)", port_info)
    except Exception as exc:
        logger.exception("HexHelper failed during startup")
        _show_fatal_box("HexHelper 启动失败", f"启动异常：{exc}\n请查看日志：{get_app_data_dir() / 'logs' / 'hex_helper.log'}")
        ctypes.windll.kernel32.CloseHandle(mutex)
        return 1

    try:
        return qt_app.exec()
    finally:
        controller.shutdown()
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    sys.exit(main())
