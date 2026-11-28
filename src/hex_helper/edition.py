"""Application edition and release metadata for HexHelper."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

APP_VERSION = "1.5.2"
APP_NAME = "HexHelper"
_COMPILED_EDITION = "dev"

_COMPILED_IS_COMMERCIAL: bool = _COMPILED_EDITION.strip().lower() == "commercial"

EDITION: str = (
    "commercial"
    if _COMPILED_IS_COMMERCIAL
    else os.environ.get("HEX_EDITION", _COMPILED_EDITION).strip().lower()
)
IS_COMMERCIAL: bool = EDITION == "commercial"
DEFAULT_CLOUD_API_BASE: str = os.environ.get(
    "HEX_CLOUD_API_BASE",
    "https://api.hexhelper.example.com",
).rstrip("/")

_UNSET = object()
_GIT_SHORT_SHA_CACHE: str | object = _UNSET


def is_commercial_edition() -> bool:
    """Return True when running as the commercial edition.

    When ``_COMPILED_EDITION == "commercial"`` (commercial release builds), this
    lock is unconditional and cannot be bypassed via ``HEX_EDITION``.
    In non-commercial source builds, dynamic ``HEX_EDITION`` env overrides remain
    supported for testing and local verification.
    """
    if _COMPILED_EDITION.strip().lower() == "commercial":
        return True
    return os.environ.get("HEX_EDITION", _COMPILED_EDITION).strip().lower() == "commercial"


def is_devlab_allowed() -> bool:
    """Return True only in local non-commercial development/devlab environments."""
    compiled = _COMPILED_EDITION.strip().lower()
    if compiled == "commercial" or is_commercial_edition():
        return False
    if getattr(sys, "frozen", False) and compiled != "devlab":
        return False
    return True


def _read_git_short_sha_from_fs(start_dir: Path) -> str:
    """Read short 7-char HEAD commit SHA directly from .git without spawning a process."""
    try:
        cur = start_dir.resolve()
        for _ in range(6):
            dot_git = cur / ".git"
            if dot_git.exists():
                git_dir = dot_git
                if dot_git.is_file():
                    raw = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
                    if raw.lower().startswith("gitdir:"):
                        rel_target = raw.split(":", 1)[1].strip()
                        git_dir = (cur / rel_target).resolve()
                head_file = git_dir / "HEAD"
                if not head_file.is_file():
                    return ""
                head_text = head_file.read_text(encoding="utf-8", errors="ignore").strip()
                if not head_text:
                    return ""
                sha = ""
                if head_text.startswith("ref:"):
                    ref_name = head_text.split(":", 1)[1].strip()
                    ref_file = git_dir / ref_name
                    if ref_file.is_file():
                        sha = ref_file.read_text(encoding="utf-8", errors="ignore").strip()
                    else:
                        packed = git_dir / "packed-refs"
                        if packed.is_file():
                            for line in packed.read_text(encoding="utf-8", errors="ignore").splitlines():
                                line = line.strip()
                                if not line or line.startswith("#") or line.startswith("^"):
                                    continue
                                parts = line.split(" ", 1)
                                if len(parts) == 2 and parts[1].strip() == ref_name:
                                    sha = parts[0].strip()
                                    break
                else:
                    sha = head_text
                sha = sha.strip().lower()
                if len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha[:7]):
                    return sha[:7]
                return ""
            if cur.parent == cur:
                break
            cur = cur.parent
    except Exception:
        return ""
    return ""


def _resolve_git_short_sha() -> str:
    global _GIT_SHORT_SHA_CACHE
    if _GIT_SHORT_SHA_CACHE is not _UNSET:
        return str(_GIT_SHORT_SHA_CACHE)

    if getattr(sys, "frozen", False):
        _GIT_SHORT_SHA_CACHE = ""
        return ""

    here = Path(__file__).resolve().parent
    sha = _read_git_short_sha_from_fs(here)
    if not sha:
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                ["git", "rev-parse", "--short=7", "HEAD"],
                cwd=str(here),
                capture_output=True,
                text=True,
                timeout=0.5,
                creationflags=creationflags,
            )
            if proc.returncode == 0:
                cand = proc.stdout.strip().lower()
                if len(cand) >= 7 and all(c in "0123456789abcdef" for c in cand[:7]):
                    sha = cand[:7]
        except Exception:
            sha = ""

    _GIT_SHORT_SHA_CACHE = sha
    return sha


def get_dev_build_tag() -> str:
    """Return ``f'{APP_VERSION}+{short_sha}'`` when a local git SHA is available, else ``APP_VERSION``."""
    sha = _resolve_git_short_sha()
    return f"{APP_VERSION}+{sha}" if sha else APP_VERSION
