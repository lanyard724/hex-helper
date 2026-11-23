"""Windows runtime dependency preflight, bundled seed cache extraction, and LoL discovery."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hex_helper.config import AppConfig
from hex_helper.edition import APP_VERSION, is_commercial_edition
from hex_helper.game.lol_locator import LolInstallInfo, detect_primary_lol_install

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_VCREDIST_REG_KEYS = (
    r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
    r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64",
)


@dataclass(frozen=True)
class PreflightItem:
    key: str
    label: str
    ok: bool
    detail: str
    fix_hint: str = ""
    critical: bool = True


@dataclass
class PreflightReport:
    items: list[PreflightItem]
    lol_install: LolInstallInfo | None = None

    @property
    def all_critical_ok(self) -> bool:
        return all(item.ok for item in self.items if item.critical)

    def get(self, key: str) -> PreflightItem | None:
        for item in self.items:
            if item.key == key:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "edition": "commercial" if is_commercial_edition() else "dev",
            "version": APP_VERSION,
            "all_critical_ok": self.all_critical_ok,
            "lol_install_dir": str(self.lol_install.install_dir) if self.lol_install else "",
            "lockfile_path": str(self.lol_install.lockfile_path) if self.lol_install else "",
            "lol_source": self.lol_install.source if self.lol_install else "",
            "items": [
                {
                    "key": it.key,
                    "label": it.label,
                    "ok": bool(it.ok),
                    "critical": bool(it.critical),
                    "detail": it.detail,
                    "fix_hint": it.fix_hint,
                }
                for it in self.items
            ],
        }


def get_bundle_root() -> Path:
    """Return frozen PyInstaller _MEIPASS root or repository/build root in source mode."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    repo_root = Path(__file__).resolve().parents[3]
    if (repo_root / "bundled_seed").is_dir():
        return repo_root
    if (repo_root / "build" / "bundled_seed").is_dir():
        return repo_root / "build"
    return repo_root


def check_vcredist_x64() -> tuple[bool, str]:
    """Verify Microsoft Visual C++ 2015-2022 x64 Redistributable availability."""
    if winreg is not None and os.name == "nt":
        for subkey in _VCREDIST_REG_KEYS:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
                    installed, _ = winreg.QueryValueEx(key, "Installed")
                    if int(installed) == 1:
                        version = ""
                        try:
                            ver_val, _ = winreg.QueryValueEx(key, "Version")
                            version = str(ver_val).strip()
                        except OSError:
                            pass
                        suffix = f" ({version})" if version else ""
                        return True, f"VC++ x64 运行库已就绪{suffix}"
            except OSError:
                continue

    sys_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    dll_path = sys_root / "System32" / "vcruntime140.dll"
    if dll_path.is_file():
        return True, f"检测到系统运行库 {dll_path}"

    return False, "未检测到 Microsoft VC++ 2015-2022 x64 运行库"


def check_winrt_zh_ocr() -> tuple[bool, str]:
    """Verify Windows WinRT Simplified Chinese (zh-Hans-CN) OCR engine availability."""
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        supported = bool(OcrEngine.is_language_supported(Language("zh-Hans-CN")))
        if supported:
            return True, "Windows WinRT 简体中文 OCR (zh-Hans-CN) 已就绪"
        return False, "系统未安装简体中文 (zh-Hans-CN) WinRT OCR 语言包"
    except Exception as exc:
        return False, f"WinRT zh-Hans-CN OCR 检测失败: {exc}"


def trigger_install_zh_ocr_pack() -> tuple[bool, str]:
    """Launch Windows elevated DISM/FoD installer for zh-CN OCR, falling back to Language Settings."""
    if os.name != "nt":
        return False, "仅支持在 Windows 10/11 上安装 WinRT OCR 语言包"

    ps_cmd = (
        "-NoProfile -ExecutionPolicy Bypass -Command "
        "\"Add-WindowsCapability -Online -Name 'Language.OCR~~~zh-CN~0.0.1.0'; "
        "Write-Host '完成后请关闭此窗口并返回 HexHelper 点击重新扫描'; Pause\""
    )
    try:
        rc = int(
            ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                "powershell.exe",
                ps_cmd,
                None,
                1,
            )
        )
        if rc > 32:
            return True, "已启动管理员安装程序补装 zh-CN OCR，完成后请点击「重新自动扫描 LOL」"
    except Exception as exc:
        logger.warning("Elevated WinRT OCR FoD launch failed: %s", exc)

    try:
        os.startfile("ms-settings:regionlanguage")  # type: ignore[attr-defined]
        return False, "已打开 Windows 语言设置，请添加「中文(简体，中国)」及其基本输入/OCR 功能"
    except Exception as exc:
        return False, f"无法启动语言包安装：{exc}"


def extract_bundled_seed_cache(
    data_dir: Path,
    *,
    seed_dir_override: Path | None = None,
) -> bool:
    """Copy bundled seed cache files into ``data_dir/cache`` without overwriting non-empty cache files."""
    seed_dir = (
        Path(seed_dir_override)
        if seed_dir_override is not None
        else (get_bundle_root() / "bundled_seed")
    )
    if not seed_dir.is_dir():
        return False

    cache_dir = Path(data_dir) / "cache"
    found_any = False
    for src in seed_dir.rglob("*"):
        if not src.is_file():
            continue
        found_any = True
        rel = src.relative_to(seed_dir)
        dst = cache_dir / rel
        try:
            if dst.exists() and dst.stat().st_size > 0:
                continue
        except OSError:
            pass
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)

    return found_any


def _patch_sort_key(patch: str) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in patch.split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _find_cached_dataset_label(cache_dir: Path) -> str | None:
    datasets_dir = cache_dir / "datasets"
    if not datasets_dir.is_dir():
        return None
    candidates: list[tuple[str, str]] = []
    for patch_dir in datasets_dir.iterdir():
        if not patch_dir.is_dir():
            continue
        for mode_dir in patch_dir.iterdir():
            if not mode_dir.is_dir():
                continue
            if (mode_dir / "heroes.json").is_file() and (mode_dir / "hero_augments.json").is_file():
                candidates.append((patch_dir.name, mode_dir.name))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (_patch_sort_key(c[0]), c[1] == "normal"), reverse=True)
    patch, mode = candidates[0]
    return f"{patch}/{mode}"


def run_windows_preflight(
    cfg: AppConfig,
    data_dir: Path,
    *,
    seed_dir_override: Path | None = None,
) -> PreflightReport:
    """Run all 4 Windows dependency & environment checks and populate cfg.lockfile_path when found."""
    data_path = Path(data_dir)
    vc_ok, vc_detail = check_vcredist_x64()
    ocr_ok, ocr_detail = check_winrt_zh_ocr()

    seeded = extract_bundled_seed_cache(data_path, seed_dir_override=seed_dir_override)
    dataset_label = _find_cached_dataset_label(data_path / "cache")
    if dataset_label:
        seed_ok = True
        src_note = "内置 26.23 种子已就绪" if seeded else "本地缓存已就绪"
        seed_detail = f"{src_note} ({dataset_label})"
        seed_hint = ""
    else:
        seed_ok = False
        seed_detail = "暂未发现本地离线数据集"
        seed_hint = "进入软件后将自动从云端同步 26.23 数据集"

    lol_info = detect_primary_lol_install(preferred_path=cfg.lockfile_path, allow_disk_scan=True)
    if lol_info is not None:
        cfg.lockfile_path = str(lol_info.lockfile_path)
        cfg.lol_install_dir = str(lol_info.install_dir)
        lol_ok = True
        elev_note = "（检测到游戏客户端以管理员运行）" if lol_info.client_elevated else ""
        lol_detail = f"已定位: {lol_info.install_dir} [{lol_info.source}]{elev_note}"
        lol_hint = (
            "客户端已提权运行，若自动连不上 LCU 请以管理员身份启动 HexHelper"
            if lol_info.client_elevated
            else ""
        )
    else:
        lol_ok = False
        lol_detail = "未自动检测到《英雄联盟》安装目录"
        lol_hint = "请点击「手动选择 LOL 目录」指向包含 LeagueClient.exe 的目录"

    items = [
        PreflightItem(
            key="vcredist",
            label="VC++ x64 运行库",
            ok=vc_ok,
            detail=vc_detail,
            fix_hint="" if vc_ok else "请安装 Microsoft Visual C++ 2015-2022 Redistributable (x64)",
            critical=True,
        ),
        PreflightItem(
            key="winrt_ocr",
            label="Windows 中文 OCR 引擎",
            ok=ocr_ok,
            detail=ocr_detail,
            fix_hint="" if ocr_ok else "请点击「补装中文 OCR」并在完成后重新扫描",
            critical=True,
        ),
        PreflightItem(
            key="seed_data",
            label="26.23 离线数据缓存",
            ok=seed_ok,
            detail=seed_detail,
            fix_hint=seed_hint,
            critical=False,
        ),
        PreflightItem(
            key="lol_path",
            label="英雄联盟客户端路径",
            ok=lol_ok,
            detail=lol_detail,
            fix_hint=lol_hint,
            critical=False,
        ),
    ]
    return PreflightReport(items=items, lol_install=lol_info)
