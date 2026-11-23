"""SHA-256 verified Windows client auto-updater for HexHelper Commercial Edition."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable
from urllib.parse import unquote, urlparse
import zipfile

import httpx

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_ARCHIVE_SUFFIXES = frozenset({".exe", ".zip"})
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)


class UpdateError(RuntimeError):
    """Base error for update manifest fetch or download failures."""


class UpdateVerificationError(UpdateError):
    """Raised when SHA-256 verification, size check, or zip safety check fails."""


@dataclass(frozen=True)
class UpdateInfo:
    """Typed client update manifest returned by GET /v1/updates/manifest."""

    current_version: str
    latest_version: str
    update_available: bool
    force_update: bool
    min_supported_version: str
    download_url: str
    sha256: str
    file_size: int
    changelog: str
    dataset_patch: str = ""
    dataset_updated_at: str = ""
    service_locked: bool = False
    service_lock_reason: str = ""

    @classmethod
    def from_manifest(cls, data: dict[str, Any], *, fallback_current_version: str = "") -> UpdateInfo:
        lock_obj = data.get("service_lock") if isinstance(data.get("service_lock"), dict) else {}
        return cls(
            current_version=str(data.get("current_version") or fallback_current_version).strip(),
            latest_version=str(data.get("latest_version") or "").strip(),
            update_available=bool(data.get("update_available", False)),
            force_update=bool(data.get("force_update", False)),
            min_supported_version=str(data.get("min_supported_version") or "").strip(),
            download_url=str(data.get("download_url") or "").strip(),
            sha256=str(data.get("sha256") or "").strip().lower(),
            file_size=max(0, int(data.get("file_size") or 0)),
            changelog=str(data.get("changelog") or ""),
            dataset_patch=str(data.get("dataset_patch") or "").strip(),
            dataset_updated_at=str(data.get("dataset_updated_at") or "").strip(),
            service_locked=bool(lock_obj.get("locked", False)),
            service_lock_reason=str(lock_obj.get("reason") or "").strip(),
        )


def parse_update_info(data: Any, current_version: str = "") -> UpdateInfo | None:
    """Safely parse a manifest dict (from /v1/updates/manifest, /v1/auth/me, or 426) into UpdateInfo."""
    if not isinstance(data, dict) or not data:
        return None
    return UpdateInfo.from_manifest(data, fallback_current_version=current_version)


def _new_zero_proxy_client(timeout_s: float, *, user_agent: str = "HexHelperClient/1.0.0") -> httpx.Client:
    return httpx.Client(
        proxy=None,
        trust_env=False,
        timeout=max(2.0, float(timeout_s)),
        follow_redirects=True,
        headers={"User-Agent": user_agent},
    )


def _resolve_download_url(download_url: str, base_url: str) -> str:
    raw = (download_url or "").strip()
    if not raw:
        raise UpdateError("更新清单缺少下载地址")
    if raw.startswith(("http://", "https://")):
        return raw
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise UpdateError("相对更新地址需要有效的 cloud_base_url")
    if not base.startswith(("http://", "https://")):
        raise UpdateError(f"无效的更新服务地址：{base}")
    if raw.startswith("/"):
        return f"{base}{raw}"
    return f"{base}/{raw}"


def _extract_filename_from_disposition(header_val: str) -> str:
    if not header_val:
        return ""
    m_ext = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\r\n]+)", header_val)
    if m_ext:
        candidate = Path(unquote(m_ext.group(1)).replace("\\", "/")).name.strip()
        if candidate:
            return candidate
    m_plain = re.search(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\r\n]+)', header_val)
    if m_plain:
        raw = (m_plain.group(1) or m_plain.group(2) or "").strip().strip('"').strip("'")
        candidate = Path(raw.replace("\\", "/")).name.strip()
        if candidate:
            return candidate
    return ""


def _safe_artifact_filename(
    info: UpdateInfo,
    target_url: str,
    content_disposition: str,
) -> str:
    candidates: list[str] = []
    disp_name = _extract_filename_from_disposition(content_disposition)
    if disp_name:
        candidates.append(disp_name)
    url_name = Path(unquote(urlparse(target_url).path).replace("\\", "/")).name.strip()
    if url_name:
        candidates.append(url_name)

    for cand in candidates:
        cleaned = Path(cand.replace("\\", "/")).name.strip()
        if (
            cleaned
            and not cleaned.startswith(".")
            and ".." not in cleaned
            and "\x00" not in cleaned
            and Path(cleaned).suffix.lower() in _ALLOWED_ARCHIVE_SUFFIXES
        ):
            return cleaned

    safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", info.latest_version or "latest").strip("._") or "latest"
    return f"HexHelper_v{safe_ver}_update.zip"


def check_for_update(
    base_url: str,
    current_version: str,
    *,
    timeout_s: float = 10.0,
) -> UpdateInfo | None:
    """Query /v1/updates/manifest without ambient proxies and return parsed UpdateInfo."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return None

    clean_cur = (current_version or "1.0.0").strip() or "1.0.0"
    url = f"{base}/v1/updates/manifest"
    try:
        with _new_zero_proxy_client(timeout_s, user_agent=f"HexHelperClient/{clean_cur}") as client:
            resp = client.get(
                url,
                params={"current_version": clean_cur},
                headers={"X-Client-Version": clean_cur},
            )
    except httpx.HTTPError as exc:
        raise UpdateError(f"无法连接更新服务器：{exc}") from exc

    if resp.status_code != 200:
        raise UpdateError(f"检查更新失败 (HTTP {resp.status_code})")

    try:
        data = resp.json()
    except Exception as exc:
        raise UpdateError("更新服务器返回无效 JSON") from exc

    if not isinstance(data, dict):
        raise UpdateError("更新服务器返回异常数据结构")

    return UpdateInfo.from_manifest(data, fallback_current_version=clean_cur)


def download_and_verify_update(
    info: UpdateInfo,
    dest_dir: Path,
    *,
    base_url: str = "",
    progress_cb: Callable[[int, int], None] | None = None,
    timeout_s: float = 120.0,
) -> Path:
    """Stream update package, enforce SHA-256 + size verification, and atomically move into dest_dir."""
    expected_sha = (info.sha256 or "").strip().lower()
    if not expected_sha or not _SHA256_RE.fullmatch(expected_sha):
        raise UpdateVerificationError("更新清单缺少有效的 SHA-256 校验值，已拒绝下载")

    target_url = _resolve_download_url(info.download_url, base_url)
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    resolved_root = dest_root.resolve()

    tmp_path: Path | None = None
    tmp_handle: Any = None
    hasher = hashlib.sha256()
    downloaded = 0

    try:
        with _new_zero_proxy_client(timeout_s) as client:
            with client.stream("GET", target_url) as resp:
                if resp.status_code != 200:
                    raise UpdateError(f"下载更新包失败 (HTTP {resp.status_code})")

                raw_cl = resp.headers.get("content-length") or "0"
                try:
                    header_size = max(0, int(raw_cl))
                except ValueError:
                    header_size = 0
                total_bytes = max(int(info.file_size or 0), header_size)

                safe_name = _safe_artifact_filename(
                    info,
                    str(resp.url),
                    resp.headers.get("content-disposition", ""),
                )
                dest_path = (resolved_root / safe_name).resolve()
                if not dest_path.is_relative_to(resolved_root):
                    raise UpdateVerificationError(f"非法更新文件名：{safe_name}")

                fd, tmp_name = tempfile.mkstemp(
                    dir=resolved_root,
                    prefix=f".{safe_name}.",
                    suffix=".part",
                )
                tmp_path = Path(tmp_name)
                tmp_handle = os.fdopen(fd, "wb")

                last_reported_bytes = 0
                last_reported_pct = -1

                for chunk in resp.iter_bytes(chunk_size=65536):
                    if not chunk:
                        continue
                    hasher.update(chunk)
                    tmp_handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None:
                        cur_pct = int((downloaded * 100) / total_bytes) if total_bytes > 0 else -1
                        if (downloaded - last_reported_bytes) >= 262144 or (
                            total_bytes > 0 and cur_pct > last_reported_pct
                        ):
                            last_reported_bytes = downloaded
                            last_reported_pct = cur_pct
                            progress_cb(downloaded, total_bytes)

        tmp_handle.flush()
        tmp_handle.close()
        tmp_handle = None

        if downloaded <= 0:
            raise UpdateVerificationError("下载的更新包为空文件")

        actual_sha = hasher.hexdigest().lower()
        if actual_sha != expected_sha:
            raise UpdateVerificationError(
                f"更新包 SHA-256 校验失败（期望 {expected_sha[:12]}...，实际 {actual_sha[:12]}...），已自动删除损坏文件"
            )

        if info.file_size > 0 and downloaded != int(info.file_size):
            raise UpdateVerificationError(
                f"更新包大小不匹配（期望 {info.file_size} 字节，实际 {downloaded} 字节）"
            )

        final_total = max(downloaded, int(info.file_size or 0))
        if progress_cb is not None and (
            last_reported_bytes != downloaded or (total_bytes <= 0 and last_reported_bytes == 0)
        ):
            progress_cb(downloaded, final_total)

        os.replace(tmp_path, dest_path)
        tmp_path = None
        return dest_path
    except httpx.HTTPError as exc:
        raise UpdateError(f"下载更新包网络中断：{exc}") from exc
    finally:
        if tmp_handle is not None:
            try:
                tmp_handle.close()
            except Exception:
                pass
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _find_apply_update_script(app_dir: Path) -> Path:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        candidates.extend([mp / "packaging" / "apply_update.bat", mp / "apply_update.bat"])
    app_path = Path(app_dir)
    candidates.extend(
        [
            app_path / "apply_update.bat",
            app_path / "_internal" / "packaging" / "apply_update.bat",
            Path(__file__).resolve().parents[3] / "packaging" / "apply_update.bat",
        ]
    )
    for cand in candidates:
        if cand.is_file():
            return cand
    raise FileNotFoundError("未找到自更新脚本 apply_update.bat")


def _safe_extract_zip(zip_path: Path, staging_dir: Path) -> None:
    resolved_stage = staging_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            unix_mode = (member.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise UpdateVerificationError(f"更新包包含非法符号链接：{member.filename}")
            target = (resolved_stage / member.filename).resolve()
            if not target.is_relative_to(resolved_stage):
                raise UpdateVerificationError(f"更新包包含非法路径穿越：{member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def _unwrap_single_root_if_needed(staging_dir: Path) -> None:
    if (staging_dir / "HexHelper.exe").is_file():
        return
    children = list(staging_dir.iterdir())
    if len(children) == 1 and children[0].is_dir() and (children[0] / "HexHelper.exe").is_file():
        wrapper = children[0]
        for item in list(wrapper.iterdir()):
            shutil.move(str(item), str(staging_dir / item.name))
        wrapper.rmdir()


def launch_update_installer(artifact_path: Path, app_dir: Path, current_pid: int) -> None:
    """Launch verified .exe installer silently or hand off .zip payload to %TEMP% apply_update.bat."""
    art = Path(artifact_path).resolve()
    if not art.is_file():
        raise FileNotFoundError(f"未找到更新安装文件：{art}")

    target_dir = Path(app_dir).resolve()
    suffix = art.suffix.lower()
    win_flags = (_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0

    if suffix == ".exe":
        subprocess.Popen(
            [str(art), "--silent", "--install-dir", str(target_dir)],
            cwd=str(art.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=win_flags,
        )
        return

    if suffix == ".zip":
        staging_dir = Path(tempfile.mkdtemp(prefix="hex_update_stage_")).resolve()
        temp_bat_path: Path | None = None
        try:
            _safe_extract_zip(art, staging_dir)
            _unwrap_single_root_if_needed(staging_dir)
            src_bat = _find_apply_update_script(target_dir)

            fd, bat_name = tempfile.mkstemp(
                dir=tempfile.gettempdir(),
                prefix="hex_apply_update_",
                suffix=".bat",
            )
            os.close(fd)
            temp_bat_path = Path(bat_name).resolve()
            shutil.copyfile(src_bat, temp_bat_path)

            stage_arg = str(staging_dir).rstrip("\\/")
            app_arg = str(target_dir).rstrip("\\/")
            subprocess.Popen(
                ["cmd.exe", "/c", "call", str(temp_bat_path), str(int(current_pid)), stage_arg, app_arg],
                cwd=tempfile.gettempdir(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=win_flags,
            )
            temp_bat_path = None
            return
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            if temp_bat_path is not None:
                temp_bat_path.unlink(missing_ok=True)
            raise

    raise ValueError(f"不支持的更新包格式：{art.suffix}")
