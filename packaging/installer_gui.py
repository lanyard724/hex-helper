"""Standalone Windows GUI double-click installer for HexHelper."""

from __future__ import annotations

import argparse
import base64
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import zipfile

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


def _ensure_src_on_path() -> None:
    if not getattr(sys, "frozen", False):
        src = Path(__file__).resolve().parents[1] / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))


_ensure_src_on_path()

from hex_helper.config import get_app_data_dir, load_config, save_config  # noqa: E402
from hex_helper.edition import APP_NAME, APP_VERSION  # noqa: E402
from hex_helper.game.lol_locator import (  # noqa: E402
    LolInstallInfo,
    clear_locator_cache,
    detect_primary_lol_install,
    validate_lol_install_dir,
)
from hex_helper.setup.preflight import (  # noqa: E402
    check_vcredist_x64,
    check_winrt_zh_ocr,
    extract_bundled_seed_cache,
    trigger_install_zh_ocr_pack,
)

_UNINSTALL_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\HexHelper"
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


def _get_resource_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1] / "build"


def _default_install_dir() -> Path:
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app) if local_app else (Path.home() / "AppData" / "Local")
    return base / "Programs" / APP_NAME


def _get_desktop_dir() -> Path:
    if os.name == "nt":
        try:
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            rc = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)
            if rc == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return Path.home() / "Desktop"


def _get_start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / APP_NAME


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _create_windows_shortcut(
    lnk_path: Path,
    target_path: Path,
    *,
    working_dir: Path,
    icon_path: Path,
    description: str = "",
) -> None:
    if os.name != "nt":
        return
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut({_ps_quote(str(lnk_path))}); "
        f"$sc.TargetPath = {_ps_quote(str(target_path))}; "
        f"$sc.WorkingDirectory = {_ps_quote(str(working_dir))}; "
        f"$sc.IconLocation = {_ps_quote(str(icon_path))}; "
        f"$sc.Description = {_ps_quote(description)}; "
        "$sc.Save()"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        check=True,
        creationflags=_CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _write_uninstall_script(
    install_dir: Path,
    *,
    desktop_lnk: Path | None = None,
    start_menu_dir: Path | None = None,
    remove_registry: bool = True,
) -> Path:
    install_str = str(install_dir)
    if '"' in install_str or "\r" in install_str or "\n" in install_str:
        raise ValueError(f"安装路径包含非法字符: {install_str!r}")
    uninstall_cmd = install_dir / "uninstall_hexhelper.cmd"
    target_desktop_lnk = desktop_lnk if desktop_lnk is not None else (_get_desktop_dir() / f"{APP_NAME}.lnk")
    target_sm_dir = start_menu_dir if start_menu_dir is not None else _get_start_menu_dir()
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        'cd /d "%TEMP%"',
        f'set "HEX_UNINST_DIR={install_str}"',
        "taskkill /F /IM HexHelper.exe >nul 2>&1",
        f'if exist "{target_desktop_lnk}" del /F /Q "{target_desktop_lnk}" >nul 2>&1',
        f'if exist "{target_sm_dir}" rmdir /S /Q "{target_sm_dir}" >nul 2>&1',
    ]
    if remove_registry:
        lines.append(f'reg delete "HKCU\\{_UNINSTALL_REG_KEY}" /f >nul 2>&1')
    lines.append('start /min "" cmd /c "ping 127.0.0.1 -n 2 >nul & rmdir /S /Q "%%HEX_UNINST_DIR%%""')
    uninstall_cmd.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return uninstall_cmd


def _register_uninstall_entry(
    install_dir: Path,
    exe_path: Path,
    uninstall_cmd: Path,
    installed_bytes: int,
) -> None:
    if winreg is None or os.name != "nt":
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _UNINSTALL_REG_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, f"{APP_NAME} - 海克斯大乱斗小助手")
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, f"{exe_path},0")
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall_cmd}"')
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, max(1, int(installed_bytes // 1024)))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def perform_install(
    *,
    payload_zip: Path,
    install_dir: Path,
    lockfile_path: str = "",
    create_desktop_shortcut: bool = True,
    create_start_menu: bool = True,
    register_uninstall: bool = True,
    progress_cb=None,
) -> Path:
    """Extract payload archive, configure lockfile_path & bundled seed, create shortcuts & uninstall entry."""
    if not payload_zip.is_file():
        raise FileNotFoundError(f"未找到内置安装包载荷: {payload_zip}")

    def report(pct: int, text: str) -> None:
        if progress_cb is not None:
            progress_cb(pct, text)

    report(5, "准备安装目录...")
    install_dir.mkdir(parents=True, exist_ok=True)

    # Terminate any existing HexHelper.exe in target directory before overwriting
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "HexHelper.exe"],
                creationflags=_CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    resolved_install_dir = install_dir.resolve()
    total_bytes = 0
    with zipfile.ZipFile(payload_zip, "r") as zf:
        members = zf.infolist()
        total = max(1, len(members))
        for idx, info in enumerate(members):
            target = (install_dir / info.filename).resolve()
            if not target.is_relative_to(resolved_install_dir):
                raise ValueError(f"非法压缩包路径: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                total_bytes += info.file_size
            if idx % 25 == 0 or idx == total - 1:
                pct = 10 + int(65 * ((idx + 1) / total))
                report(pct, f"正在解压程序文件 ({idx + 1}/{total})...")

    exe_path = install_dir / "HexHelper.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(f"解压后未找到主程序: {exe_path}")

    report(80, "释放 26.23 离线数据种子并写入配置...")
    data_dir = get_app_data_dir()
    bundled_seed = install_dir / "_internal" / "bundled_seed"
    if bundled_seed.is_dir():
        extract_bundled_seed_cache(data_dir, seed_dir_override=bundled_seed)

    cfg_path = data_dir / "config.json"
    cfg = load_config(cfg_path)
    if lockfile_path.strip():
        cfg.lockfile_path = lockfile_path.strip()
    save_config(cfg, cfg_path)

    uninstall_cmd = _write_uninstall_script(install_dir)

    if create_desktop_shortcut:
        report(88, "创建桌面快捷方式...")
        _create_windows_shortcut(
            _get_desktop_dir() / f"{APP_NAME}.lnk",
            exe_path,
            working_dir=install_dir,
            icon_path=exe_path,
            description="HexHelper - 海克斯大乱斗智能助手",
        )

    if create_start_menu:
        report(92, "创建开始菜单快捷方式...")
        sm_dir = _get_start_menu_dir()
        _create_windows_shortcut(
            sm_dir / f"{APP_NAME}.lnk",
            exe_path,
            working_dir=install_dir,
            icon_path=exe_path,
            description="HexHelper - 海克斯大乱斗智能助手",
        )
        _create_windows_shortcut(
            sm_dir / f"卸载 {APP_NAME}.lnk",
            uninstall_cmd,
            working_dir=install_dir.parent,
            icon_path=exe_path,
            description=f"卸载 {APP_NAME}",
        )

    if register_uninstall:
        report(96, "注册 Windows 卸载信息...")
        _register_uninstall_entry(install_dir, exe_path, uninstall_cmd, total_bytes)

    report(100, "安装完成！")
    return exe_path


def _enable_windows_dpi_awareness() -> None:
    """Opt into Windows System DPI awareness so tkinter renders sharply and never clips buttons."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _enable_dark_titlebar(root: tk.Tk) -> None:
    """Apply Windows 10/11 immersive dark titlebar if supported."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        val = ctypes.c_int(1)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)
            )
    except Exception:
        pass


class HexInstallerApp:
    """LOL V16.23 Hextech dark tkinter GUI installer with DPI-safe adaptive geometry."""

    def __init__(self) -> None:
        _enable_windows_dpi_awareness()
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION} 安装向导")
        self.root.resizable(True, True)
        self.root.configure(bg="#040E1A")
        _enable_dark_titlebar(self.root)

        res_root = _get_resource_root()
        self.payload_zip = res_root / "HexHelper_payload.zip"
        ico_candidates = (
            res_root / "hex_helper.ico",
            Path(__file__).resolve().parents[1] / "src" / "hex_helper" / "assets" / "hex_helper.ico",
        )
        for ico in ico_candidates:
            if ico.is_file():
                try:
                    self.root.iconbitmap(str(ico))
                except Exception:
                    pass
                break

        self.install_dir_var = tk.StringVar(value=str(_default_install_dir()))
        self.lol_dir_var = tk.StringVar(value="")
        self.lockfile_path: str = ""
        self.desktop_var = tk.BooleanVar(value=True)
        self.start_menu_var = tk.BooleanVar(value=True)
        self.launch_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._fit_window_geometry()
        self.root.after(50, self._run_env_scan)

    def _fit_window_geometry(self) -> None:
        """Auto-size and center window to fit all content + bottom install button at any Windows DPI scale."""
        self.root.update_idletasks()
        sw = max(800, int(self.root.winfo_screenwidth()))
        sh = max(600, int(self.root.winfo_screenheight()))
        req_w = max(700, int(self.root.winfo_reqwidth()) + 28)
        req_h = max(560, int(self.root.winfo_reqheight()) + 20)
        w = min(req_w, sw - 64)
        h = min(req_h, sh - 80)
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self.root.minsize(min(640, w), min(h, sh - 80))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Hex.Horizontal.TProgressbar",
            troughcolor="#030A14",
            background="#0AC8B9",
            bordercolor="#785A28",
            lightcolor="#2EE6D6",
            darkcolor="#089E92",
            thickness=10,
        )

        # Pack footer FIRST with side="bottom" so the Install button can NEVER be clipped off-screen
        prog_frame = tk.Frame(self.root, bg="#091428", highlightbackground="#785A28", highlightthickness=1)
        prog_frame.pack(side="bottom", fill="x", padx=16, pady=(6, 14))

        prog_inner = tk.Frame(prog_frame, bg="#091428")
        prog_inner.pack(fill="x", padx=14, pady=(10, 12))

        self.status_lbl = tk.Label(
            prog_inner,
            text="准备就绪，点击右侧「立即安装 HexHelper」开始安装",
            font=("Microsoft YaHei UI", 9),
            fg="#A09B8C",
            bg="#091428",
            anchor="w",
        )
        self.status_lbl.pack(fill="x", pady=(0, 6))
        self.progress = ttk.Progressbar(prog_inner, style="Hex.Horizontal.TProgressbar", maximum=100, value=0)
        self.progress.pack(fill="x", pady=(0, 10))

        btn_row = tk.Frame(prog_inner, bg="#091428")
        btn_row.pack(fill="x")
        tk.Label(
            btn_row,
            text="免代理直连 · 无需 Python 环境",
            font=("Microsoft YaHei UI", 8),
            fg="#785A28",
            bg="#091428",
            anchor="w",
        ).pack(side="left")

        self.install_btn = tk.Button(
            btn_row,
            text="立即安装 HexHelper",
            command=self._start_install,
            bg="#C8AA6E",
            fg="#040E1A",
            activebackground="#F0E6D2",
            activeforeground="#040E1A",
            relief="flat",
            bd=0,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=22,
            pady=7,
            cursor="hand2",
        )
        self.install_btn.pack(side="right")

        # Scroll-safe upper content container
        body = tk.Frame(self.root, bg="#040E1A")
        body.pack(side="top", fill="both", expand=True, padx=16, pady=(14, 4))

        # Header
        hdr = tk.Frame(body, bg="#091428", highlightbackground="#C8AA6E", highlightthickness=1)
        hdr.pack(fill="x", pady=(0, 8))
        tk.Label(
            hdr,
            text=f"HEXHELPER v{APP_VERSION} · 一键安装向导",
            font=("Microsoft YaHei UI", 13, "bold"),
            fg="#F0E6D2",
            bg="#091428",
        ).pack(anchor="w", padx=14, pady=(10, 2))
        tk.Label(
            hdr,
            text="内置 26.23 离线数据 · 自动 6 层定位《英雄联盟》客户端 · 商业免代理直连",
            font=("Microsoft YaHei UI", 9),
            fg="#A09B8C",
            bg="#091428",
        ).pack(anchor="w", padx=14, pady=(0, 10))

        # Preflight section
        env_frame = tk.Frame(body, bg="#091428", highlightbackground="#463714", highlightthickness=1)
        env_frame.pack(fill="x", pady=(0, 8))
        tk.Label(
            env_frame,
            text="SYS.01  运行环境与依赖检测",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#C8AA6E",
            bg="#091428",
        ).pack(anchor="w", padx=12, pady=(8, 4))

        self.vc_lbl = tk.Label(env_frame, text="• VC++ x64 运行库：检测中...", font=("Microsoft YaHei UI", 9), fg="#A09B8C", bg="#091428", anchor="w")
        self.vc_lbl.pack(fill="x", padx=14, pady=1)

        ocr_row = tk.Frame(env_frame, bg="#091428")
        ocr_row.pack(fill="x", padx=14, pady=(1, 8))
        self.ocr_lbl = tk.Label(ocr_row, text="• WinRT 中文 OCR：检测中...", font=("Microsoft YaHei UI", 9), fg="#A09B8C", bg="#091428", anchor="w")
        self.ocr_lbl.pack(side="left", fill="x", expand=True)
        self.fix_ocr_btn = tk.Button(
            ocr_row,
            text="补装中文 OCR",
            command=self._on_fix_ocr,
            bg="#0F223A",
            fg="#F0E6D2",
            activebackground="#173355",
            activeforeground="#FFFFFF",
            relief="solid",
            bd=1,
            font=("Microsoft YaHei UI", 8, "bold"),
            cursor="hand2",
        )

        # LoL Path section
        lol_frame = tk.Frame(body, bg="#091428", highlightbackground="#463714", highlightthickness=1)
        lol_frame.pack(fill="x", pady=(0, 8))
        tk.Label(
            lol_frame,
            text="SYS.02  《英雄联盟》客户端目录（自动 6 层探测）",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#C8AA6E",
            bg="#091428",
        ).pack(anchor="w", padx=12, pady=(8, 4))

        lol_row = tk.Frame(lol_frame, bg="#091428")
        lol_row.pack(fill="x", padx=12, pady=(0, 4))
        self.lol_entry = tk.Entry(
            lol_row,
            textvariable=self.lol_dir_var,
            bg="#030A14",
            fg="#F0E6D2",
            insertbackground="#F0E6D2",
            relief="solid",
            bd=1,
            font=("Microsoft YaHei UI", 9),
        )
        self.lol_entry.pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(
            lol_row,
            text="浏览...",
            command=self._browse_lol,
            bg="#0F223A",
            fg="#F0E6D2",
            activebackground="#173355",
            activeforeground="#FFFFFF",
            relief="solid",
            bd=1,
            font=("Microsoft YaHei UI", 9),
            padx=10,
            cursor="hand2",
        ).pack(side="left", padx=(6, 0))
        tk.Button(
            lol_row,
            text="重扫",
            command=self._rescan_lol,
            bg="#0F223A",
            fg="#F0E6D2",
            activebackground="#173355",
            activeforeground="#FFFFFF",
            relief="solid",
            bd=1,
            font=("Microsoft YaHei UI", 9),
            padx=10,
            cursor="hand2",
        ).pack(side="left", padx=(4, 0))

        self.lol_status_lbl = tk.Label(lol_frame, text="", font=("Microsoft YaHei UI", 8), fg="#A09B8C", bg="#091428", anchor="w")
        self.lol_status_lbl.pack(fill="x", padx=12, pady=(0, 8))

        # Install dir + Options
        dir_frame = tk.Frame(body, bg="#091428", highlightbackground="#463714", highlightthickness=1)
        dir_frame.pack(fill="x", pady=(0, 4))
        tk.Label(
            dir_frame,
            text="SYS.03  HexHelper 安装路径",
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#C8AA6E",
            bg="#091428",
        ).pack(anchor="w", padx=12, pady=(8, 4))

        dir_row = tk.Frame(dir_frame, bg="#091428")
        dir_row.pack(fill="x", padx=12, pady=(0, 6))
        tk.Entry(
            dir_row,
            textvariable=self.install_dir_var,
            bg="#030A14",
            fg="#F0E6D2",
            insertbackground="#F0E6D2",
            relief="solid",
            bd=1,
            font=("Microsoft YaHei UI", 9),
        ).pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(
            dir_row,
            text="更改目录",
            command=self._browse_install_dir,
            bg="#0F223A",
            fg="#F0E6D2",
            activebackground="#173355",
            activeforeground="#FFFFFF",
            relief="solid",
            bd=1,
            font=("Microsoft YaHei UI", 9),
            padx=10,
            cursor="hand2",
        ).pack(side="left", padx=(6, 0))

        opt_row = tk.Frame(dir_frame, bg="#091428")
        opt_row.pack(fill="x", padx=12, pady=(2, 8))
        for txt, var in (
            ("创建桌面快捷方式", self.desktop_var),
            ("创建开始菜单快捷方式", self.start_menu_var),
            ("安装完成后立即启动", self.launch_var),
        ):
            tk.Checkbutton(
                opt_row,
                text=txt,
                variable=var,
                bg="#091428",
                fg="#F0E6D2",
                selectcolor="#030A14",
                activebackground="#091428",
                activeforeground="#F0E6D2",
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", padx=(0, 14))

    def _run_env_scan(self) -> None:
        vc_ok, vc_msg = check_vcredist_x64()
        self.vc_lbl.configure(
            text=f"{'✓' if vc_ok else '✕'} VC++ x64 运行库：{vc_msg}",
            fg="#4ADE80" if vc_ok else "#F87171",
        )

        ocr_ok, ocr_msg = check_winrt_zh_ocr()
        self.ocr_lbl.configure(
            text=f"{'✓' if ocr_ok else '✕'} WinRT 中文 OCR：{ocr_msg}",
            fg="#4ADE80" if ocr_ok else "#F87171",
        )
        if not ocr_ok:
            self.fix_ocr_btn.pack(side="right")
        else:
            self.fix_ocr_btn.pack_forget()

        self._rescan_lol()
        self._fit_window_geometry()

    def _rescan_lol(self) -> None:
        clear_locator_cache()
        info = detect_primary_lol_install(preferred_path=self.lol_dir_var.get().strip(), allow_disk_scan=True)
        if info is not None:
            self.lol_dir_var.set(str(info.install_dir))
            self.lockfile_path = str(info.lockfile_path)
            self.lol_status_lbl.configure(
                text=f"✓ 已自动定位 [{info.source}] -> lockfile: {info.lockfile_path}",
                fg="#4ADE80",
            )
        else:
            self.lockfile_path = ""
            self.lol_status_lbl.configure(
                text="! 未自动发现客户端，可手动点击「浏览...」指定，或安装后在设置向导配置",
                fg="#FBBF24",
            )

    def _browse_lol(self) -> None:
        chosen = filedialog.askdirectory(title="选择《英雄联盟》安装根目录 (含 LeagueClient)")
        if not chosen:
            return
        info = validate_lol_install_dir(Path(chosen), source="manual")
        if info is None:
            messagebox.showwarning("目录无效", "所选目录未找到 LeagueClient.exe 或 League of Legends.exe，请重新选择。")
            return
        self.lol_dir_var.set(str(info.install_dir))
        self.lockfile_path = str(info.lockfile_path)
        self.lol_status_lbl.configure(
            text=f"✓ 已手动确认 -> lockfile: {info.lockfile_path}",
            fg="#4ADE80",
        )

    def _browse_install_dir(self) -> None:
        chosen = filedialog.askdirectory(title="选择 HexHelper 安装目录")
        if chosen:
            self.install_dir_var.set(str(Path(chosen)))

    def _on_fix_ocr(self) -> None:
        ok, msg = trigger_install_zh_ocr_pack()
        messagebox.showinfo("中文 OCR 语言包", msg)
        if ok:
            self._run_env_scan()

    def _start_install(self) -> None:
        target_dir = Path(self.install_dir_var.get().strip() or str(_default_install_dir()))
        lol_input = self.lol_dir_var.get().strip()
        if lol_input:
            info = validate_lol_install_dir(Path(lol_input), source="manual")
            if info is not None:
                self.lockfile_path = str(info.lockfile_path)
            else:
                self.lockfile_path = ""
                messagebox.showwarning(
                    "目录无效",
                    "当前填写的《英雄联盟》目录无效，本次将跳过写入 lockfile 路径（安装后可在向导中重选）。",
                )
        else:
            self.lockfile_path = ""

        self.install_btn.configure(state="disabled", text="正在安装...")

        def worker() -> None:
            try:
                exe_path = perform_install(
                    payload_zip=self.payload_zip,
                    install_dir=target_dir,
                    lockfile_path=self.lockfile_path,
                    create_desktop_shortcut=self.desktop_var.get(),
                    create_start_menu=self.start_menu_var.get(),
                    register_uninstall=True,
                    progress_cb=lambda pct, msg: self.root.after(
                        0, lambda p=pct, m=msg: self._update_progress(p, m)
                    ),
                )
                self.root.after(0, lambda: self._on_install_success(exe_path))
            except PermissionError as exc:
                hint = (
                    f"没有权限写入目标目录：{target_dir}\n"
                    f"详情：{exc}\n\n"
                    "建议改选默认目录或 D:\\HexHelper，或右键安装包「以管理员身份运行」。"
                )
                self.root.after(0, lambda err=hint: self._on_install_error(err))
            except Exception as exc:
                self.root.after(0, lambda err=str(exc): self._on_install_error(err))

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, pct: int, msg: str) -> None:
        self.progress.configure(value=pct)
        self.status_lbl.configure(text=msg, fg="#F2F2F0")

    def _on_install_success(self, exe_path: Path) -> None:
        self.progress.configure(value=100)
        self.status_lbl.configure(text=f"✓ 安装完成：{exe_path}", fg="#4ADE80")
        if self.launch_var.get():
            try:
                subprocess.Popen(
                    [str(exe_path)],
                    cwd=str(exe_path.parent),
                    creationflags=_DETACHED_PROCESS,
                )
            except Exception as exc:
                messagebox.showwarning("启动提示", f"安装成功，但自动启动失败：{exc}\n请手动打开 {exe_path}")
        else:
            messagebox.showinfo("安装完成", f"HexHelper v{APP_VERSION} 已安装到：\n{exe_path.parent}")
        self.root.destroy()

    def _on_install_error(self, err: str) -> None:
        self.install_btn.configure(state="normal", text="立即安装 HexHelper")
        self.status_lbl.configure(text=f"✕ 安装失败：{err}", fg="#EF4444")
        messagebox.showerror("安装失败", f"安装过程中发生错误：\n{err}")

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HexHelper Windows Installer")
    parser.add_argument("--silent", action="store_true", help="Run non-interactively without GUI")
    parser.add_argument("--install-dir", default="", help="Override target install directory")
    parser.add_argument("--no-launch", action="store_true", help="Do not launch HexHelper.exe after install")
    parser.add_argument("--no-shortcuts", action="store_true", help="Skip Desktop/Start Menu shortcuts and Uninstall registry")
    args = parser.parse_args(argv)

    if args.silent:
        install_dir = Path(args.install_dir) if args.install_dir else _default_install_dir()
        lol_info: LolInstallInfo | None = detect_primary_lol_install(allow_disk_scan=True)
        lockfile = str(lol_info.lockfile_path) if lol_info is not None else ""
        exe_path = perform_install(
            payload_zip=_get_resource_root() / "HexHelper_payload.zip",
            install_dir=install_dir,
            lockfile_path=lockfile,
            create_desktop_shortcut=not args.no_shortcuts,
            create_start_menu=not args.no_shortcuts,
            register_uninstall=not args.no_shortcuts,
        )
        if not args.no_launch:
            subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent), creationflags=_DETACHED_PROCESS)
        return 0

    app = HexInstallerApp()
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
