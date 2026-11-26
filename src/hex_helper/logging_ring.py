"""20-match rolling runtime log ring, privacy sanitizer, and feedback bundle builder.

Persists per-match log files under ``<data_dir>/logs/matches/`` with a strict FIFO
cap (default 20 matches): once a new match pushes the count past ``max_matches``,
the oldest match log file(s) are immediately removed from disk and from ``index.json``.
Logs emitted outside an active match (startup, preflight, login, lobby) are captured
in a bounded ``session_preamble.log`` so pre-game errors are never lost.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import json
import logging
from pathlib import Path
import re
import threading
from typing import Any

from hex_helper.config import get_app_data_dir

DEFAULT_MAX_MATCHES = 20
DEFAULT_MAX_MATCH_BYTES = 192 * 1024
DEFAULT_MAX_PREAMBLE_BYTES = 48 * 1024
DEFAULT_MAX_BUNDLE_BYTES = 360 * 1024
_KEEP_HEAD_BYTES = 24 * 1024
_TRIM_MARKER = "\n[... earlier lines in this match trimmed to respect per-match size cap ...]\n"

_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b")
_RIOT_AUTH_RE = re.compile(r"(?i)(riot:)([^\s@\"']{4,})(@)")
_REMOTING_TOKEN_RE = re.compile(r"(?i)(--remoting-auth-token=)([^\s\"']+)")
_GEMINI_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")
_OPENAI_DS_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_COS_KEY_RE = re.compile(r"\bAKID[A-Za-z0-9]{16,}\b")
_AUTH_KV_RE = re.compile(
    r"(?i)\b(password|passwd|refresh_token|access_token|secret_key|api_key)\s*([=:])\s*([^\s,;&\"'\}]{4,})"
)
_RIOT_KV_QUOTED_RE = re.compile(
    r"(?i)([\"']?\b(?:puuid|summonerId|accountId|summonerName|riotId|riotIdGameName|riotIdTagline)\b[\"']?\s*[:=]\s*)([\"'])([^\"']+)\2"
)
_RIOT_KV_RE = re.compile(
    r"(?i)\b(puuid|summonerId|accountId|summonerName|riotId|riotIdGameName|riotIdTagline)\s*([=:])\s*([^\s,;&\"'\}\]]{3,})"
)
_RIOT_PUUID_RE = re.compile(
    r"(?:\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{76,82}(?![A-Za-z0-9_-]))"
)
_RIOT_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_/\\.#-])([A-Za-z0-9\u4e00-\u9fff\u3400-\u4dbf\uac00-\ud7af\u3040-\u30ff][A-Za-z0-9_.\-\u4e00-\u9fff\u3400-\u4dbf\uac00-\ud7af\u3040-\u30ff]{1,15})#([A-Za-z0-9]{2,5})(?![A-Za-z0-9_#])"
)
_RIOT_ID_NON_PLAYER_PREFIXES: frozenset[str] = frozenset(
    {
        "match",
        "item",
        "hero",
        "champ",
        "champion",
        "aug",
        "augment",
        "slot",
        "card",
        "seq",
        "ev",
        "event",
        "pid",
        "id",
        "req",
        "request",
        "order",
        "ticket",
        "err",
        "error",
        "line",
        "frame",
        "round",
        "turn",
        "team",
        "pass",
        "phase",
        "tier",
        "issue",
        "pr",
        "bug",
        "step",
        "stage",
        "worker",
        "thread",
        "proc",
        "port",
        "code",
        "http",
        "https",
        "color",
        "rgb",
        "rgba",
        "hex",
        "bg",
        "fg",
        "border",
    }
)
_FILE_EXT_SUFFIXES = (
    ".py",
    ".md",
    ".html",
    ".htm",
    ".json",
    ".jsonl",
    ".log",
    ".js",
    ".ts",
    ".css",
    ".png",
    ".jpg",
    ".com",
    ".cn",
    ".net",
    ".org",
    ".io",
    ".dev",
    ".app",
    ".gg",
    ".test",
)
_LINE_ANCHOR_RE = re.compile(r"^L\d+$")
_WIN_USER_PATH_RE = re.compile(r"(?i)([A-Z]:[/\\]Users[/\\])([^/\\\s\"'<>|]+)([/\\])")


def _redact_riot_id_match(match: re.Match[str]) -> str:
    name = match.group(1)
    tag = match.group(2)
    low_name = name.lower()
    if low_name in _RIOT_ID_NON_PLAYER_PREFIXES:
        return match.group(0)
    if any(low_name.endswith(ext) for ext in _FILE_EXT_SUFFIXES):
        return match.group(0)
    if _LINE_ANCHOR_RE.match(tag):
        return match.group(0)
    # Latin-only Riot GameNames are at least 3 chars (2-char CJK names like 阿岚#CN1 are valid)
    if name.isascii() and (len(name) < 3 or name.isdigit()):
        return match.group(0)
    return "<RIOT_ID_REDACTED>"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_file_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def sanitize_log_text(text: str) -> str:
    """Scrub tokens, API keys, LCU passwords, Riot PUUID/IDs, and Windows usernames from log text."""
    if not text:
        return ""
    cleaned = _BEARER_RE.sub(r"\1<REDACTED>", text)
    cleaned = _JWT_RE.sub("<JWT_REDACTED>", cleaned)
    cleaned = _RIOT_AUTH_RE.sub(r"\1<REDACTED>\3", cleaned)
    cleaned = _REMOTING_TOKEN_RE.sub(r"\1<REDACTED>", cleaned)
    cleaned = _GEMINI_KEY_RE.sub("<API_KEY_REDACTED>", cleaned)
    cleaned = _OPENAI_DS_KEY_RE.sub("<API_KEY_REDACTED>", cleaned)
    cleaned = _COS_KEY_RE.sub("<SECRET_ID_REDACTED>", cleaned)
    cleaned = _AUTH_KV_RE.sub(r"\1\2<REDACTED>", cleaned)
    cleaned = _RIOT_KV_QUOTED_RE.sub(r"\1\2<REDACTED>\2", cleaned)
    cleaned = _RIOT_KV_RE.sub(r"\1\2<REDACTED>", cleaned)
    cleaned = _RIOT_PUUID_RE.sub("<PUUID_REDACTED>", cleaned)
    cleaned = _RIOT_ID_RE.sub(_redact_riot_id_match, cleaned)
    cleaned = _WIN_USER_PATH_RE.sub(r"\1<USER>\3", cleaned)
    return cleaned


def compress_log_bundle(text: str) -> str:
    """Return base64-encoded gzip representation of UTF-8 log text."""
    raw = (text or "").encode("utf-8", errors="replace")
    compressed = gzip.compress(raw, compresslevel=6)
    return base64.b64encode(compressed).decode("ascii")


@dataclass
class MatchLogEntry:
    seq: int
    match_key: str
    file_name: str
    started_at: str
    ended_at: str = ""
    hero_id: str = ""
    hero_name: str = ""
    dataset_patch: str = ""
    dataset_mode: str = ""
    won: int = -1
    duration_s: int = 0
    lines: int = 0
    bytes_size: int = 0
    warn_count: int = 0
    error_count: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MatchLogEntry:
        return cls(
            seq=int(raw.get("seq", 0)),
            match_key=str(raw.get("match_key") or ""),
            file_name=str(raw.get("file_name") or ""),
            started_at=str(raw.get("started_at") or ""),
            ended_at=str(raw.get("ended_at") or ""),
            hero_id=str(raw.get("hero_id") or ""),
            hero_name=str(raw.get("hero_name") or ""),
            dataset_patch=str(raw.get("dataset_patch") or ""),
            dataset_mode=str(raw.get("dataset_mode") or ""),
            won=int(raw.get("won", -1)),
            duration_s=int(raw.get("duration_s", 0)),
            lines=int(raw.get("lines", 0)),
            bytes_size=int(raw.get("bytes_size", 0)),
            warn_count=int(raw.get("warn_count", 0)),
            error_count=int(raw.get("error_count", 0)),
        )


def _trim_file_head_tail(path: Path, max_bytes: int, keep_head: int = _KEEP_HEAD_BYTES) -> int:
    """Keep the first ``keep_head`` bytes plus the newest tail so total <= ``max_bytes``."""
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return len(data)
    marker = _TRIM_MARKER.encode("utf-8")
    head_budget = min(keep_head, max(0, max_bytes // 4))
    tail_budget = max(1024, max_bytes - head_budget - len(marker))

    head_chunk = data[:head_budget]
    nl_head = head_chunk.rfind(b"\n")
    if nl_head > 0:
        head_chunk = head_chunk[: nl_head + 1]

    tail_chunk = data[-tail_budget:]
    nl_tail = tail_chunk.find(b"\n")
    if 0 <= nl_tail < len(tail_chunk) - 1:
        tail_chunk = tail_chunk[nl_tail + 1 :]

    compacted = head_chunk + marker + tail_chunk
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(compacted)
    tmp.replace(path)
    return len(compacted)


def _trim_file_tail_only(path: Path, max_bytes: int) -> int:
    """Keep only the newest ``max_bytes`` of a rolling preamble log."""
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return len(data)
    tail = data[-max_bytes:]
    nl = tail.find(b"\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1 :]
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(tail)
    tmp.replace(path)
    return len(tail)


class MatchLogRing:
    """Thread-safe FIFO rolling log archive retaining at most ``max_matches`` (20) matches."""

    def __init__(
        self,
        root_dir: Path,
        *,
        max_matches: int = DEFAULT_MAX_MATCHES,
        max_match_bytes: int = DEFAULT_MAX_MATCH_BYTES,
        max_preamble_bytes: int = DEFAULT_MAX_PREAMBLE_BYTES,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.max_matches = max(1, int(max_matches))
        self.max_match_bytes = max(8 * 1024, int(max_match_bytes))
        self.max_preamble_bytes = max(8 * 1024, int(max_preamble_bytes))
        self._index_path = self.root_dir / "index.json"
        self._preamble_path = self.root_dir / "session_preamble.log"
        self._lock = threading.RLock()

        self._next_seq: int = 1
        self._matches: list[MatchLogEntry] = []
        self._active_seq: int | None = None
        self._writes_since_meta_flush: int = 0

        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._load_index()
        self._enforce_cap_locked()

    @property
    def active_entry(self) -> MatchLogEntry | None:
        with self._lock:
            return self._get_active_entry_locked()

    def _get_active_entry_locked(self) -> MatchLogEntry | None:
        if self._active_seq is None:
            return None
        for entry in reversed(self._matches):
            if entry.seq == self._active_seq:
                return entry
        self._active_seq = None
        return None

    def _load_index(self) -> None:
        if not self._index_path.is_file():
            return
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        loaded: list[MatchLogEntry] = []
        for item in raw.get("matches") or []:
            if not isinstance(item, dict):
                continue
            entry = MatchLogEntry.from_dict(item)
            if not entry.file_name:
                continue
            path = self.root_dir / entry.file_name
            if not path.is_file():
                continue
            try:
                entry.bytes_size = path.stat().st_size
            except OSError:
                pass
            loaded.append(entry)
        loaded.sort(key=lambda e: e.seq)
        self._matches = loaded
        max_seen = max((e.seq for e in loaded), default=0)
        self._next_seq = max(int(raw.get("next_seq", 1)), max_seen + 1)

    def _save_index_locked(self) -> None:
        payload = {
            "next_seq": self._next_seq,
            "max_matches": self.max_matches,
            "updated_at": _utc_now_iso(),
            "matches": [asdict(e) for e in self._matches],
        }
        tmp = self._index_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._index_path)
        except OSError:
            pass

    def _enforce_cap_locked(self) -> list[str]:
        """Evict oldest matches whenever len(_matches) > max_matches, and prune orphan files."""
        evicted: list[str] = []
        while len(self._matches) > self.max_matches:
            oldest = self._matches.pop(0)
            evicted.append(oldest.file_name)
            if self._active_seq == oldest.seq:
                self._active_seq = None
            try:
                (self.root_dir / oldest.file_name).unlink(missing_ok=True)
            except OSError:
                pass

        known_files = {e.file_name for e in self._matches}
        try:
            for candidate in self.root_dir.glob("match_*.log"):
                if candidate.name not in known_files:
                    candidate.unlink(missing_ok=True)
        except OSError:
            pass

        self._save_index_locked()
        return evicted

    def begin_match(
        self,
        match_key: str = "",
        *,
        hero_id: str = "",
        hero_name: str = "",
        dataset_patch: str = "",
        dataset_mode: str = "",
    ) -> MatchLogEntry:
        """Start a new match log bucket (or re-attach if the latest match shares match_key)."""
        clean_key = (match_key or "").strip()
        with self._lock:
            # Re-attach to latest match if the app restarted mid-match with identical match_key
            if clean_key and self._matches and self._matches[-1].match_key == clean_key:
                existing = self._matches[-1]
                self._active_seq = existing.seq
                if hero_id:
                    existing.hero_id = hero_id
                if hero_name:
                    existing.hero_name = hero_name
                if dataset_patch:
                    existing.dataset_patch = dataset_patch
                if dataset_mode:
                    existing.dataset_mode = dataset_mode
                existing.ended_at = ""
                self._append_to_file_locked(
                    self.root_dir / existing.file_name,
                    f"--- REATTACHED MATCH #{existing.seq:05d} | {_utc_now_iso()} | hero={existing.hero_name or existing.hero_id or '-'} ---",
                    max_bytes=self.max_match_bytes,
                    entry=existing,
                    levelno=logging.INFO,
                )
                self._save_index_locked()
                return existing

            # Close previous active match cleanly if still open
            prev = self._get_active_entry_locked()
            if prev is not None and not prev.ended_at:
                prev.ended_at = _utc_now_iso()

            seq = self._next_seq
            self._next_seq += 1
            now_iso = _utc_now_iso()
            file_name = f"match_{seq:05d}_{_utc_file_stamp()}.log"
            entry = MatchLogEntry(
                seq=seq,
                match_key=clean_key,
                file_name=file_name,
                started_at=now_iso,
                hero_id=(hero_id or "").strip(),
                hero_name=(hero_name or "").strip(),
                dataset_patch=(dataset_patch or "").strip(),
                dataset_mode=(dataset_mode or "").strip(),
            )
            self._matches.append(entry)
            self._active_seq = seq

            header = (
                f"========== MATCH #{seq:05d} | {now_iso} | "
                f"hero={entry.hero_name or entry.hero_id or 'unknown'} | "
                f"patch={entry.dataset_patch or '-'} | mode={entry.dataset_mode or '-'} =========="
            )
            self._append_to_file_locked(
                self.root_dir / file_name,
                header,
                max_bytes=self.max_match_bytes,
                entry=entry,
                levelno=logging.INFO,
            )
            self._enforce_cap_locked()
            return entry

    def update_match_meta(
        self,
        *,
        hero_id: str | None = None,
        hero_name: str | None = None,
        dataset_patch: str | None = None,
        dataset_mode: str | None = None,
    ) -> None:
        with self._lock:
            entry = self._get_active_entry_locked()
            if entry is None:
                return
            changed = False
            if hero_id and hero_id != entry.hero_id:
                entry.hero_id = hero_id
                changed = True
            if hero_name and hero_name != entry.hero_name:
                entry.hero_name = hero_name
                changed = True
            if dataset_patch and dataset_patch != entry.dataset_patch:
                entry.dataset_patch = dataset_patch
                changed = True
            if dataset_mode and dataset_mode != entry.dataset_mode:
                entry.dataset_mode = dataset_mode
                changed = True
            if changed:
                self._append_to_file_locked(
                    self.root_dir / entry.file_name,
                    (
                        f"--- META UPDATE | {_utc_now_iso()} | "
                        f"hero={entry.hero_name or entry.hero_id or '-'} | "
                        f"patch={entry.dataset_patch or '-'} | mode={entry.dataset_mode or '-'} ---"
                    ),
                    max_bytes=self.max_match_bytes,
                    entry=entry,
                    levelno=logging.INFO,
                )
                self._save_index_locked()

    def end_match(self, *, won: int = -1, duration_s: int = 0) -> None:
        with self._lock:
            entry = self._get_active_entry_locked()
            if entry is None:
                return
            entry.ended_at = _utc_now_iso()
            if won in (0, 1):
                entry.won = int(won)
            if duration_s > 0:
                entry.duration_s = int(duration_s)
            outcome = {1: "WIN", 0: "LOSS"}.get(entry.won, "UNKNOWN")
            footer = (
                f"--- MATCH #{entry.seq:05d} END | {entry.ended_at} | "
                f"result={outcome} | duration={entry.duration_s}s | "
                f"warns={entry.warn_count} errors={entry.error_count} ---"
            )
            self._append_to_file_locked(
                self.root_dir / entry.file_name,
                footer,
                max_bytes=self.max_match_bytes,
                entry=entry,
                levelno=logging.INFO,
            )
            self._active_seq = None
            self._save_index_locked()

    def append_line(self, line: str, *, levelno: int = logging.INFO) -> None:
        text = (line or "").rstrip("\r\n")
        if not text:
            return
        with self._lock:
            entry = self._get_active_entry_locked()
            if entry is not None:
                self._append_to_file_locked(
                    self.root_dir / entry.file_name,
                    text,
                    max_bytes=self.max_match_bytes,
                    entry=entry,
                    levelno=levelno,
                )
                self._writes_since_meta_flush += 1
                if self._writes_since_meta_flush >= 25 or levelno >= logging.WARNING:
                    self._writes_since_meta_flush = 0
                    self._save_index_locked()
            else:
                self._append_to_preamble_locked(text)

    def _append_to_file_locked(
        self,
        path: Path,
        line: str,
        *,
        max_bytes: int,
        entry: MatchLogEntry,
        levelno: int,
    ) -> None:
        encoded = (line + "\n").encode("utf-8", errors="replace")
        try:
            with path.open("ab") as fh:
                fh.write(encoded)
            entry.lines += 1
            if levelno >= logging.ERROR:
                entry.error_count += 1
            elif levelno >= logging.WARNING:
                entry.warn_count += 1
            size = path.stat().st_size
            if size > max_bytes:
                size = _trim_file_head_tail(path, max_bytes)
            entry.bytes_size = size
        except OSError:
            pass

    def _append_to_preamble_locked(self, line: str) -> None:
        encoded = (line + "\n").encode("utf-8", errors="replace")
        try:
            with self._preamble_path.open("ab") as fh:
                fh.write(encoded)
            if self._preamble_path.stat().st_size > self.max_preamble_bytes:
                _trim_file_tail_only(self._preamble_path, self.max_preamble_bytes)
        except OSError:
            pass

    def list_matches(self) -> list[MatchLogEntry]:
        with self._lock:
            return [MatchLogEntry(**asdict(e)) for e in self._matches]

    def summary_stats(self) -> dict[str, Any]:
        with self._lock:
            preamble_bytes = 0
            try:
                if self._preamble_path.is_file():
                    preamble_bytes = self._preamble_path.stat().st_size
            except OSError:
                preamble_bytes = 0
            match_bytes = sum(max(0, int(e.bytes_size)) for e in self._matches)
            total_errors = sum(int(e.error_count) for e in self._matches)
            total_warns = sum(int(e.warn_count) for e in self._matches)
            return {
                "match_count": len(self._matches),
                "max_matches": self.max_matches,
                "active_seq": self._active_seq,
                "match_bytes": match_bytes,
                "preamble_bytes": preamble_bytes,
                "total_bytes": match_bytes + preamble_bytes,
                "total_errors": total_errors,
                "total_warns": total_warns,
                "root_dir": str(self.root_dir),
            }

    def build_feedback_bundle(
        self,
        *,
        max_matches: int | None = None,
        max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
        sanitize: bool = True,
    ) -> tuple[str, int, dict[str, Any]]:
        """Assemble a sanitized rolling log bundle (up to 20 matches) bounded by ``max_bytes``."""
        limit_matches = self.max_matches if max_matches is None else max(1, min(int(max_matches), self.max_matches))
        budget = max(16 * 1024, int(max_bytes))

        with self._lock:
            selected = list(self._matches[-limit_matches:])
            preamble_raw = ""
            if self._preamble_path.is_file():
                try:
                    preamble_raw = self._preamble_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    preamble_raw = ""

            match_payloads: list[tuple[MatchLogEntry, str]] = []
            for entry in selected:
                p = self.root_dir / entry.file_name
                try:
                    body = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
                except OSError:
                    body = ""
                match_payloads.append((entry, body))

        if sanitize:
            preamble_raw = sanitize_log_text(preamble_raw)
            match_payloads = [(e, sanitize_log_text(b)) for e, b in match_payloads]

        # Build index table (always included at top for fast triage)
        header_lines = [
            f"=== HexHelper Rolling Runtime Log Bundle ({_utc_now_iso()}) ===",
            f"Retained Matches: {len(match_payloads)} / {self.max_matches} (FIFO rolling window)",
        ]
        if match_payloads:
            header_lines.append("--- Match Index (oldest -> newest) ---")
            for e, _ in match_payloads:
                outcome = {1: "WIN", 0: "LOSS"}.get(e.won, "LIVE" if not e.ended_at else "-")
                header_lines.append(
                    f"  #{e.seq:05d} | {e.started_at} | hero={e.hero_name or e.hero_id or '-'} | "
                    f"patch={e.dataset_patch or '-'} | mode={e.dataset_mode or '-'} | "
                    f"res={outcome} | warn={e.warn_count} err={e.error_count} | {e.bytes_size}B"
                )
        header_text = "\n".join(header_lines) + "\n\n"

        # Trim session preamble to <= 32 KB tail
        preamble_section = ""
        if preamble_raw.strip():
            pre_bytes = preamble_raw.encode("utf-8", errors="replace")
            if len(pre_bytes) > 32 * 1024:
                pre_bytes = pre_bytes[-(32 * 1024) :]
                nl = pre_bytes.find(b"\n")
                if nl >= 0:
                    pre_bytes = pre_bytes[nl + 1 :]
                preamble_raw = "[... earlier session log truncated ...]\n" + pre_bytes.decode(
                    "utf-8", errors="replace"
                )
            preamble_section = "=== SESSION / STARTUP / LOBBY LOG ===\n" + preamble_raw.rstrip() + "\n\n"

        fixed_bytes = len(header_text.encode("utf-8")) + len(preamble_section.encode("utf-8"))
        remaining = max(8 * 1024, budget - fixed_bytes)

        # Pack newest matches with priority so recent bugs are never truncated away
        packed_sections_rev: list[str] = []
        included_count = 0
        for entry, body in reversed(match_payloads):
            section = body.rstrip() + "\n\n"
            sec_bytes = len(section.encode("utf-8", errors="replace"))
            if sec_bytes <= remaining:
                packed_sections_rev.append(section)
                remaining -= sec_bytes
                included_count += 1
            elif remaining >= 4096:
                raw_sec = section.encode("utf-8", errors="replace")
                tail = raw_sec[-(remaining - 128) :]
                nl = tail.find(b"\n")
                if 0 <= nl < len(tail) - 1:
                    tail = tail[nl + 1 :]
                partial = (
                    f"========== MATCH #{entry.seq:05d} (tail truncated to fit upload budget) ==========\n"
                    + tail.decode("utf-8", errors="replace")
                )
                packed_sections_rev.append(partial)
                remaining = 0
                included_count += 1
                break
            else:
                break

        packed_sections = list(reversed(packed_sections_rev))
        full_text = header_text + preamble_section + "".join(packed_sections)
        raw_bytes = len(full_text.encode("utf-8", errors="replace"))
        stats = {
            "included_matches": included_count,
            "stored_matches": len(match_payloads),
            "max_matches": self.max_matches,
            "raw_bytes": raw_bytes,
        }
        return full_text, included_count, stats


class MatchRingLogHandler(logging.Handler):
    """Logging handler forwarding formatted records into a ``MatchLogRing``."""

    def __init__(self, ring: MatchLogRing, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.ring.append_line(msg, levelno=record.levelno)
        except Exception:
            # Never break application execution from a log handler
            pass


_GLOBAL_RING_LOCK = threading.Lock()
_GLOBAL_RING: MatchLogRing | None = None
_GLOBAL_HANDLER: MatchRingLogHandler | None = None


def get_match_log_ring(data_dir: Path | None = None, *, max_matches: int = DEFAULT_MAX_MATCHES) -> MatchLogRing:
    """Return process-wide MatchLogRing singleton (creating it on first access)."""
    global _GLOBAL_RING
    with _GLOBAL_RING_LOCK:
        if _GLOBAL_RING is None:
            base = Path(data_dir) if data_dir is not None else get_app_data_dir()
            _GLOBAL_RING = MatchLogRing(base / "logs" / "matches", max_matches=max_matches)
        return _GLOBAL_RING


def install_match_log_handler(
    data_dir: Path | None = None,
    *,
    max_matches: int = DEFAULT_MAX_MATCHES,
) -> MatchLogRing:
    """Attach the rolling 20-match handler to the root logger (idempotent)."""
    global _GLOBAL_HANDLER
    ring = get_match_log_ring(data_dir=data_dir, max_matches=max_matches)
    with _GLOBAL_RING_LOCK:
        root_logger = logging.getLogger()
        if _GLOBAL_HANDLER is not None and _GLOBAL_HANDLER in root_logger.handlers:
            return ring
        handler = MatchRingLogHandler(ring, level=logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root_logger.addHandler(handler)
        _GLOBAL_HANDLER = handler
    return ring
