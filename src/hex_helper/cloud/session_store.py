"""Windows DPAPI encrypted session token persistence (%LOCALAPPDATA%\\HexHelper\\session.dat)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from hex_helper.config import get_app_data_dir

logger = logging.getLogger(__name__)

_CRYPTPROTECT_UI_FORBIDDEN = 0x01
_APP_ENTROPY = b"HexHelper.CloudSession.v1"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _make_blob(raw: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_char]]:
    buf = ctypes.create_string_buffer(raw, len(raw))
    ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    return _DATA_BLOB(cbData=len(raw), pbData=ptr), buf


def _get_dpapi_funcs() -> tuple[Any, Any, Any]:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32.CryptProtectData, crypt32.CryptUnprotectData, kernel32.LocalFree


def dpapi_protect(plaintext: bytes, entropy: bytes = _APP_ENTROPY) -> bytes:
    """Encrypt bytes for the current Windows user via CryptProtectData."""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is only available on Windows")
    if not plaintext:
        raise ValueError("Cannot DPAPI-protect empty payload")

    protect_fn, _, local_free = _get_dpapi_funcs()
    in_blob, _in_buf = _make_blob(plaintext)
    ent_blob, _ent_buf = _make_blob(entropy)
    out_blob = _DATA_BLOB()

    ok = protect_fn(
        ctypes.byref(in_blob),
        "HexHelperSession",
        ctypes.byref(ent_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"CryptProtectData failed (winerror={err})")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            local_free(out_blob.pbData)


def dpapi_unprotect(ciphertext: bytes, entropy: bytes = _APP_ENTROPY) -> bytes:
    """Decrypt bytes protected via CryptProtectData for the current Windows user."""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is only available on Windows")
    if not ciphertext:
        raise ValueError("Empty DPAPI ciphertext")

    _, unprotect_fn, local_free = _get_dpapi_funcs()
    in_blob, _in_buf = _make_blob(ciphertext)
    ent_blob, _ent_buf = _make_blob(entropy)
    out_blob = _DATA_BLOB()

    ok = unprotect_fn(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(ent_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(err, f"CryptUnprotectData failed (winerror={err})")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            local_free(out_blob.pbData)


class DpapiSessionStore:
    """Stores cloud session credentials DPAPI-encrypted at %LOCALAPPDATA%\\HexHelper\\session.dat."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else (get_app_data_dir() / "session.dat")
        self._io_lock = threading.Lock()

    def save(self, data: dict[str, Any]) -> Path:
        if not isinstance(data, dict):
            raise TypeError("Session data must be a dict")
        raw_json = json.dumps(data, ensure_ascii=False).encode("utf-8")
        encrypted = dpapi_protect(raw_json)
        with self._io_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as fh:
                    tmp_path = Path(fh.name)
                    fh.write(encrypted)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, self.path)
                tmp_path = None
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
        return self.path

    def load(self) -> dict[str, Any] | None:
        with self._io_lock:
            if not self.path.is_file():
                return None
            try:
                encrypted = self.path.read_bytes()
                if not encrypted:
                    return None
                decrypted = dpapi_unprotect(encrypted)
                parsed = json.loads(decrypted.decode("utf-8"))
                if isinstance(parsed, dict):
                    return parsed
            except Exception as exc:
                logger.debug("Failed to load DPAPI session from %s: %s", self.path, exc)
            return None

    def clear(self) -> None:
        with self._io_lock:
            candidates = [self.path, self.path.with_suffix(".dat.tmp")]
            try:
                if self.path.parent.is_dir():
                    candidates.extend(self.path.parent.glob(f".{self.path.name}.*.tmp"))
            except OSError:
                pass
            for p in candidates:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
