"""6-layer Windows League of Legends (CN WeGame / Tencent / Riot) auto-locator."""

from __future__ import annotations

import ctypes
import logging
import os
import re
import string
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

import psutil

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_LOL_PROCESS_NAMES = frozenset({
    "leagueclientux.exe",
    "leagueclient.exe",
    "league of legends.exe",
})

_SUBDIR_PARENTS = frozenset({
    "tcls",
    "leagueclient",
    "game",
    "wegamelauncher",
    "launcher",
    "rail_files",
    "riot client",
})

_SCAN_SKIP_DIRS = frozenset({
    "windows",
    "$recycle.bin",
    "system volume information",
    "programdata",
    "recovery",
    "perflogs",
    "msocache",
    "node_modules",
    ".git",
    ".venv",
    "appdata",
})

_SCAN_HINT_KEYWORDS = (
    "英雄联盟",
    "league of legends",
    "wegameapps",
    "tencent games",
    "rail_apps",
    "riot games",
    "lol",
    "games",
    "game",
    "program files",
    "program files (x86)",
    "tencent",
    "wegame",
)

_LNK_EXE_RE = re.compile(r'[A-Za-z]:\\[^\x00-\x1f"<>|*?\r\n]+?\.exe', re.IGNORECASE)
_TRAILING_ICON_INDEX_RE = re.compile(r",\s*-?\d+\s*$")
_EXE_WITH_ARGS_RE = re.compile(r"^(.+?\.exe)(?:\s+.*)?$", re.IGNORECASE)

_LOCATOR_CACHE_LOCK = threading.Lock()
_LOCATOR_CACHE: dict[tuple[str, bool, bool], tuple[float, list[LolInstallInfo]]] = {}


def clear_locator_cache() -> None:
    """Clear any cached LoL installation discovery results."""
    with _LOCATOR_CACHE_LOCK:
        _LOCATOR_CACHE.clear()


@dataclass
class LolInstallInfo:
    install_dir: Path
    league_client_dir: Path
    lockfile_path: Path
    source: str
    is_running: bool = False
    client_elevated: bool | None = None


def _norm_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _clean_raw_path(raw: str) -> str:
    text = (raw or "").replace("\x00", "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end_quote = text.find('"', 1)
        if end_quote > 1:
            text = text[1:end_quote].strip()
        else:
            text = text.strip('"').strip()
    else:
        text = _TRAILING_ICON_INDEX_RE.sub("", text).strip()
        exe_match = _EXE_WITH_ARGS_RE.match(text)
        if exe_match:
            text = exe_match.group(1).strip()
    return text.strip('"').strip()


def _extract_root_from_exe_or_path(raw: str) -> Path | None:
    """Normalize an exe/lockfile/icon/uninstall path back to the LoL install root."""
    cleaned = _clean_raw_path(raw)
    if not cleaned:
        return None
    p = Path(cleaned)
    if p.suffix.lower() in (".exe", ".lnk") or p.name.lower() == "lockfile":
        p = p.parent
    if p.name.lower() in _SUBDIR_PARENTS and p.parent != p:
        p = p.parent
    return p


def validate_lol_install_dir(candidate: Path, source: str = "scan") -> LolInstallInfo | None:
    """Verify that ``candidate`` is a genuine LoL installation directory."""
    try:
        if not candidate.is_dir():
            return None

        lc_subdir = candidate / "LeagueClient"
        if (lc_subdir / "LeagueClient.exe").is_file() or (lc_subdir / "LeagueClientUx.exe").is_file():
            return LolInstallInfo(
                install_dir=candidate,
                league_client_dir=lc_subdir,
                lockfile_path=lc_subdir / "lockfile",
                source=source,
            )

        if (candidate / "LeagueClient.exe").is_file() or (candidate / "LeagueClientUx.exe").is_file():
            if candidate.name.lower() == "leagueclient" and candidate.parent != candidate:
                install_dir = candidate.parent
                league_client_dir = candidate
            else:
                install_dir = candidate
                league_client_dir = candidate
            return LolInstallInfo(
                install_dir=install_dir,
                league_client_dir=league_client_dir,
                lockfile_path=league_client_dir / "lockfile",
                source=source,
            )

        if (candidate / "Game" / "League of Legends.exe").is_file():
            league_client_dir = lc_subdir if lc_subdir.is_dir() else candidate
            return LolInstallInfo(
                install_dir=candidate,
                league_client_dir=league_client_dir,
                lockfile_path=league_client_dir / "lockfile",
                source=source,
            )
    except OSError:
        return None
    return None


def _candidates_from_processes() -> tuple[list[tuple[Path, str, bool]], bool]:
    """Layer 2: inspect running LoL processes via psutil."""
    found: list[tuple[Path, str, bool]] = []
    saw_elevated = False
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in _LOL_PROCESS_NAMES:
                continue
            try:
                exe_path = proc.exe()
            except psutil.AccessDenied:
                saw_elevated = True
                continue
            except (psutil.NoSuchProcess, OSError):
                continue

            elevated = False
            if hasattr(proc, "cmdline"):
                try:
                    proc.cmdline()
                except psutil.AccessDenied:
                    elevated = True
                    saw_elevated = True
                except (psutil.NoSuchProcess, OSError):
                    pass

            if exe_path:
                root = _extract_root_from_exe_or_path(exe_path)
                if root is not None:
                    found.append((root, f"process:{name}", elevated))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return found, saw_elevated


def _wegame_sibling_candidates(base: Path) -> list[tuple[Path, str]]:
    results: list[tuple[Path, str]] = []
    app_containers = (
        base / "WeGameApps",
        base / "rail_apps",
        base.parent / "WeGameApps",
        base.parent / "rail_apps",
    )
    for container in app_containers:
        try:
            if not container.is_dir():
                continue
            with os.scandir(container) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    low = entry.name.lower()
                    if "英雄联盟" in low or "league" in low:
                        results.append((Path(entry.path), "registry_wegame"))
        except OSError:
            continue
    return results


def _candidates_from_registry() -> list[tuple[Path, str]]:
    """Layer 3: query Windows Tencent/WeGame and Uninstall registry entries."""
    candidates: list[tuple[Path, str]] = []
    if winreg is None or os.name != "nt":
        return candidates

    direct_keys = (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\LOL", False),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\LOL", False),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\LOL", False),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeGame", True),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\WeGame", True),
    )
    reg_value_names = ("InstallPath", "InstallDir", "setup", "Path", "ClientPath")

    for hive, subkey, is_wegame in direct_keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                for val_name in reg_value_names:
                    try:
                        val, _ = winreg.QueryValueEx(key, val_name)
                    except OSError:
                        continue
                    if not isinstance(val, str) or not val.strip():
                        continue
                    root = _extract_root_from_exe_or_path(val)
                    if root is not None:
                        candidates.append((root, "registry"))
                        if is_wegame:
                            candidates.extend(_wegame_sibling_candidates(root))
                    cleaned = _clean_raw_path(val)
                    if is_wegame and cleaned:
                        cleaned_path = Path(cleaned)
                        if root is None or _norm_key(cleaned_path) != _norm_key(root):
                            candidates.extend(_wegame_sibling_candidates(cleaned_path))
        except OSError:
            continue

    uninstall_roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, base_subkey in uninstall_roots:
        try:
            with winreg.OpenKey(hive, base_subkey) as parent_key:
                idx = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(parent_key, idx)
                        idx += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(parent_key, sub_name) as item_key:
                            try:
                                display_name, _ = winreg.QueryValueEx(item_key, "DisplayName")
                            except OSError:
                                continue
                            if not isinstance(display_name, str):
                                continue
                            dn_low = display_name.lower()
                            if "英雄联盟" not in dn_low and "league of legends" not in dn_low:
                                continue
                            for val_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    val, _ = winreg.QueryValueEx(item_key, val_name)
                                except OSError:
                                    continue
                                if isinstance(val, str) and val.strip():
                                    root = _extract_root_from_exe_or_path(val)
                                    if root is not None:
                                        candidates.append((root, "registry_uninstall"))
                    except OSError:
                        continue
        except OSError:
            continue

    return candidates


def _shortcut_search_dirs() -> list[Path]:
    dirs = [
        Path.home() / "Desktop",
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return dirs


def _extract_exe_paths_from_lnk(lnk_path: Path) -> list[str]:
    """Scan binary ShellLink (.lnk) bytes for target .exe paths without spawning PowerShell."""
    try:
        data = lnk_path.read_bytes()[:262144]
    except OSError:
        return []
    matches: list[str] = []
    seen: set[str] = set()
    decoded_chunks = (
        data.decode("utf-16-le", errors="ignore"),
        data[1:].decode("utf-16-le", errors="ignore"),
        data.decode("gb18030", errors="ignore"),
        data.decode("utf-8", errors="ignore"),
    )
    for text in decoded_chunks:
        for hit in _LNK_EXE_RE.findall(text):
            low = hit.lower()
            if low not in seen:
                seen.add(low)
                matches.append(hit)
    return matches


def _candidates_from_shortcuts() -> list[tuple[Path, str]]:
    """Layer 4: inspect Desktop and Start Menu .lnk shortcuts."""
    candidates: list[tuple[Path, str]] = []
    for base_dir in _shortcut_search_dirs():
        try:
            if not base_dir.is_dir():
                continue
            for lnk in base_dir.rglob("*.lnk"):
                name_low = lnk.name.lower()
                if not any(kw in name_low for kw in ("英雄联盟", "league", "lol")):
                    continue
                for exe_path in _extract_exe_paths_from_lnk(lnk):
                    root = _extract_root_from_exe_or_path(exe_path)
                    if root is not None:
                        candidates.append((root, "shortcut"))
        except OSError:
            continue
    return candidates


def _get_fixed_drives() -> list[Path]:
    """Return fixed local drive roots (DRIVE_FIXED == 3) on Windows."""
    if os.name != "nt":
        return []
    drives: list[Path] = []
    try:
        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for idx, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << idx):
                root_str = f"{letter}:\\"
                if kernel32.GetDriveTypeW(root_str) == 3:
                    drives.append(Path(root_str))
    except Exception:
        return []
    return drives


def _is_hint_dir(name_low: str) -> bool:
    return any(kw in name_low for kw in _SCAN_HINT_KEYWORDS)


def _scan_root_bounded(root: Path, *, max_depth: int = 3) -> list[LolInstallInfo]:
    """Layer 5: bounded shallow scan across a root directory."""
    found: list[LolInstallInfo] = []
    try:
        if not root.is_dir():
            return found
    except OSError:
        return found

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        next_depth = depth + 1
        if next_depth > max_depth:
            continue
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    low = entry.name.lower()
                    if low in _SCAN_SKIP_DIRS or low.startswith("$"):
                        continue
                    entry_path = Path(entry.path)
                    info = validate_lol_install_dir(entry_path, source="disk_scan")
                    if info is not None:
                        found.append(info)
                    if next_depth < max_depth and (depth == 0 or _is_hint_dir(low)):
                        stack.append((entry_path, next_depth))
        except OSError:
            continue
    return found


def detect_lol_installations(
    preferred_path: str = "",
    *,
    allow_disk_scan: bool = True,
    force_disk_scan: bool = False,
    cache_ttl_s: float = 0.0,
) -> list[LolInstallInfo]:
    """Discover and validate LoL installations across all 6 Windows detection layers."""
    cache_key = (preferred_path.strip(), bool(allow_disk_scan), bool(force_disk_scan))
    if cache_ttl_s > 0.0:
        now = time.monotonic()
        with _LOCATOR_CACHE_LOCK:
            cached = _LOCATOR_CACHE.get(cache_key)
            if cached is not None and (now - cached[0]) < cache_ttl_s:
                return [replace(item) for item in cached[1]]

    ordered_by_key: dict[str, LolInstallInfo] = {}

    def register(info: LolInstallInfo) -> None:
        key = _norm_key(info.install_dir)
        existing = ordered_by_key.get(key)
        if existing is None:
            ordered_by_key[key] = info
            return
        existing.is_running = existing.is_running or info.is_running
        if existing.client_elevated is True or info.client_elevated is True:
            existing.client_elevated = True
        elif existing.client_elevated is None and info.client_elevated is not None:
            existing.client_elevated = info.client_elevated

    # Layer 1: preferred_path
    if preferred_path and preferred_path.strip():
        pref_candidates: list[Path] = []
        extracted = _extract_root_from_exe_or_path(preferred_path)
        if extracted is not None:
            pref_candidates.append(extracted)
        cleaned = _clean_raw_path(preferred_path)
        if cleaned:
            raw_p = Path(cleaned)
            pref_candidates.append(raw_p)
            if raw_p.parent != raw_p:
                pref_candidates.append(raw_p.parent)
        for cand in pref_candidates:
            info = validate_lol_install_dir(cand, source="preferred_path")
            if info is not None:
                register(info)

    # Layer 2: running processes
    proc_candidates, saw_elevated_process = _candidates_from_processes()
    for cand, src, elevated in proc_candidates:
        info = validate_lol_install_dir(cand, source=src)
        if info is not None:
            info.is_running = True
            info.client_elevated = elevated
            register(info)

    # Layer 3: Windows Registry
    for cand, src in _candidates_from_registry():
        info = validate_lol_install_dir(cand, source=src)
        if info is not None:
            register(info)

    # Layer 4: Windows .lnk shortcuts
    for cand, src in _candidates_from_shortcuts():
        info = validate_lol_install_dir(cand, source=src)
        if info is not None:
            register(info)

    # Layer 5: bounded disk scan (fallback only when Layers 1-4 found nothing, unless forced)
    if allow_disk_scan and (force_disk_scan or not ordered_by_key):
        for drive_root in _get_fixed_drives():
            for info in _scan_root_bounded(drive_root, max_depth=3):
                register(info)

    results = list(ordered_by_key.values())

    # If a running LoL process was seen but could not be inspected due to UAC/UIPI,
    # flag discovered installation(s) so UI/preflight can warn the user about elevation.
    if saw_elevated_process and results and not any(item.is_running for item in results):
        with_lockfile = [item for item in results if item.lockfile_path.is_file()]
        targets = with_lockfile if with_lockfile else [results[0]]
        for item in targets:
            item.is_running = True
            item.client_elevated = True

    results.sort(key=lambda item: (0 if item.is_running else 1))

    if cache_ttl_s > 0.0:
        with _LOCATOR_CACHE_LOCK:
            _LOCATOR_CACHE[cache_key] = (time.monotonic(), [replace(item) for item in results])

    return results


def detect_primary_lol_install(
    preferred_path: str = "",
    *,
    allow_disk_scan: bool = True,
    force_disk_scan: bool = False,
    cache_ttl_s: float = 0.0,
) -> LolInstallInfo | None:
    """Return the highest-priority valid LoL installation, or None if not found."""
    installs = detect_lol_installations(
        preferred_path=preferred_path,
        allow_disk_scan=allow_disk_scan,
        force_disk_scan=force_disk_scan,
        cache_ttl_s=cache_ttl_s,
    )
    return installs[0] if installs else None


def is_riot_overseas_lockfile(lockfile_path: str | Path | None) -> bool:
    """Return True when lockfile points to a Riot Games client installation rather than WeGame/Tencent."""
    norm = str(lockfile_path or "").strip().lower().replace("/", "\\")
    return bool(norm and "riot games" in norm and "wegame" not in norm and "tencent" not in norm)
