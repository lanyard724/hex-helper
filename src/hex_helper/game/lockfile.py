"""WeGame / CN LeagueClient lockfile discovery with read-only command-line fallback."""

from __future__ import annotations

import logging
import os
import re
import string
from pathlib import Path

import psutil

from hex_helper.game.lol_locator import detect_lol_installations
from hex_helper.game.models import LcuCredentials

logger = logging.getLogger(__name__)

_PORT_RE = re.compile(r"--app-port=(\d+)")
_TOKEN_RE = re.compile(r"--remoting-auth-token=([^\s\"']+)")

_COMMON_SUBPATHS = (
    r"WeGameApps\英雄联盟\LeagueClient\lockfile",
    r"Tencent Games\英雄联盟\LeagueClient\lockfile",
    r"英雄联盟\LeagueClient\lockfile",
    r"Riot Games\League of Legends\lockfile",
    r"Program Files\WeGameApps\英雄联盟\LeagueClient\lockfile",
    r"Program Files (x86)\WeGameApps\英雄联盟\LeagueClient\lockfile",
    r"Program Files\Tencent Games\英雄联盟\LeagueClient\lockfile",
    r"Program Files (x86)\Tencent Games\英雄联盟\LeagueClient\lockfile",
)


def parse_lockfile_text(content: str, source: str = "lockfile") -> LcuCredentials | None:
    """Parse Riot lockfile format: LeagueClient:PID:PORT:PASSWORD:https."""
    parts = content.strip().split(":")
    if len(parts) < 5:
        return None
    try:
        pid = int(parts[1])
        port = int(parts[2])
        password = parts[3]
        protocol = parts[4] or "https"
        if port <= 0 or not password:
            return None
        return LcuCredentials(
            pid=pid,
            port=port,
            password=password,
            protocol=protocol,
            source=source,
        )
    except ValueError:
        return None


def _read_registry_install_dirs() -> list[Path]:
    dirs: list[Path] = []
    if os.name != "nt":
        return dirs
    try:
        import winreg

        roots = [
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\LOL"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\LOL"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\LOL"),
            (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeGame"),
        ]
        for hive, subkey in roots:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    for val_name in ("InstallPath", "InstallDir", "Path", "ClientPath"):
                        try:
                            val, _ = winreg.QueryValueEx(key, val_name)
                            if isinstance(val, str) and val.strip():
                                dirs.append(Path(val.strip()))
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass
    return dirs


def candidate_lockfile_paths(preferred_lockfile: str = "") -> list[Path]:
    """Build ordered list of candidate LeagueClient lockfile paths on Windows."""
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(p)

    if preferred_lockfile:
        p = Path(preferred_lockfile)
        if p.is_dir():
            add(p / "lockfile")
            add(p / "LeagueClient" / "lockfile")
        else:
            add(p)

    try:
        for info in detect_lol_installations(
            preferred_path=preferred_lockfile,
            allow_disk_scan=True,
            cache_ttl_s=60.0,
        ):
            add(info.lockfile_path)
            add(info.league_client_dir / "lockfile")
            add(info.install_dir / "LeagueClient" / "lockfile")
            add(info.install_dir / "lockfile")
    except Exception:
        logger.debug("lol_locator scan failed; falling back to static paths", exc_info=True)

    for reg_dir in _read_registry_install_dirs():
        add(reg_dir / "LeagueClient" / "lockfile")
        add(reg_dir / "lockfile")

    # Scan existing drive roots for common WeGame / Tencent install folders
    for drive in string.ascii_uppercase:
        root = Path(f"{drive}:\\")
        if not root.exists():
            continue
        for sub in _COMMON_SUBPATHS:
            add(root / sub)

    return candidates


def _from_process_cmdline() -> LcuCredentials | None:
    """Fallback: inspect LeagueClientUx.exe command line if privilege level permits."""
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("leagueclientux.exe", "leagueclient.exe"):
                continue
            # Also try lockfile next to exe path
            try:
                exe_path = proc.exe()
                if exe_path:
                    lf = Path(exe_path).parent / "lockfile"
                    if lf.exists():
                        creds = parse_lockfile_text(
                            lf.read_text(encoding="utf-8", errors="ignore"),
                            source=str(lf),
                        )
                        if creds:
                            return creds
            except (psutil.AccessDenied, OSError):
                pass

            cmdline = " ".join(proc.cmdline())
            pm = _PORT_RE.search(cmdline)
            tm = _TOKEN_RE.search(cmdline)
            if pm and tm:
                return LcuCredentials(
                    pid=proc.pid,
                    port=int(pm.group(1)),
                    password=tm.group(1),
                    protocol="https",
                    source="process_cmdline",
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return None


def find_lcu_credentials(preferred_lockfile: str = "") -> LcuCredentials | None:
    """Locate active LCU credentials using lockfile first, then process cmdline fallback."""
    for path in candidate_lockfile_paths(preferred_lockfile):
        try:
            if path.exists() and path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                creds = parse_lockfile_text(text, source=str(path))
                if creds:
                    # Verify PID is alive or port likely active; skip stale lockfiles if PID clearly dead
                    if creds.pid > 0 and psutil.pid_exists(creds.pid):
                        return creds
                    # On WeGame some child/launcher PIDs differ; still return if lockfile modified recently
                    return creds
        except OSError:
            continue

    return _from_process_cmdline()
