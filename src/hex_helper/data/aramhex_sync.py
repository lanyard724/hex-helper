"""Incremental SHA-256 verified synchronizer for czgreat/aramhex-data."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

import httpx

from hex_helper.data.provider import DatasetDescriptor

logger = logging.getLogger(__name__)

ARAMHEX_RAW_BASE = "https://raw.githubusercontent.com/czgreat/aramhex-data/main"
_DEFAULT_HEADERS = {
    "User-Agent": "HexHelper/1.1",
    "Accept-Encoding": "gzip, deflate",
}

REQUIRED_DATASET_FILES = (
    "meta.json",
    "heroes.json",
    "augments.json",
    "hero_augments.json",
    "hero_spells.json",
    "core_builds.json",
    "hero_augment_items.json",
    "hero_item_recommendations.json",
    "item_metadata.json",
)

OPTIONAL_DATASET_FILES = (
    "hero_items_raw.json",
)

HIGH_BAND_DATASET_FILES = (
    "bands/high/heroes.json",
    "bands/high/hero_augments.json",
    "bands/high/hero_item_recommendations.json",
    "bands/high/core_builds.json",
    "bands/high/hero_augment_items.json",
)

OPTIONAL_HIGH_BAND_FILES = (
    "bands/high/hero_items_raw.json",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _etag_sidecar(cache_file: Path) -> Path:
    return cache_file.with_suffix(cache_file.suffix + ".etag")


def http_get_bytes(
    url: str,
    proxy_url: str = "",
    timeout: float = 15.0,
    *,
    client: httpx.Client | None = None,
) -> bytes:
    """Fetch bytes via pooled client or proxy-first falling back to strict zero-proxy direct connection."""
    if client is not None:
        resp = client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    attempts: list[str | None] = []
    if proxy_url:
        attempts.append(proxy_url)
    attempts.append(None)

    last_err: Exception | None = None
    for proxy in attempts:
        try:
            with httpx.Client(
                proxy=proxy,
                trust_env=False,
                timeout=timeout,
                follow_redirects=True,
                headers=_DEFAULT_HEADERS,
            ) as tmp_client:
                resp = tmp_client.get(url)
                resp.raise_for_status()
                return resp.content
        except Exception as exc:
            last_err = exc
            logger.debug("HTTP GET failed for %s (proxy=%s): %s", url, proxy, exc)
    assert last_err is not None
    raise last_err


def http_get_cached(
    url: str,
    cache_file: Path,
    *,
    proxy_url: str = "",
    timeout: float = 15.0,
    client: httpx.Client | None = None,
) -> tuple[bytes, bool]:
    """Conditional HTTP GET using ETag / If-None-Match so unchanged remote JSON transfers 0 bytes (304)."""
    etag_path = _etag_sidecar(cache_file)
    req_headers: dict[str, str] = {}
    cached_etag = ""
    if cache_file.exists() and etag_path.exists():
        try:
            cached_etag = etag_path.read_text(encoding="utf-8").strip()
            if cached_etag:
                req_headers["If-None-Match"] = cached_etag
        except Exception:
            cached_etag = ""

    def _do_request(http_client: httpx.Client) -> tuple[bytes, bool]:
        resp = http_client.get(url, headers=req_headers or None, timeout=timeout)
        if resp.status_code == 304 and cache_file.exists():
            return cache_file.read_bytes(), True
        resp.raise_for_status()
        raw = resp.content
        new_etag = (resp.headers.get("etag") or "").strip()
        if new_etag:
            try:
                atomic_write_bytes(etag_path, new_etag.encode("utf-8"))
            except Exception:
                pass
        return raw, False

    if client is not None:
        return _do_request(client)

    attempts: list[str | None] = []
    if proxy_url:
        attempts.append(proxy_url)
    attempts.append(None)
    last_err: Exception | None = None
    for proxy in attempts:
        try:
            with httpx.Client(
                proxy=proxy,
                trust_env=False,
                timeout=timeout,
                follow_redirects=True,
                headers=_DEFAULT_HEADERS,
            ) as tmp_client:
                return _do_request(tmp_client)
        except Exception as exc:
            last_err = exc
            logger.debug("Conditional HTTP GET failed for %s (proxy=%s): %s", url, proxy, exc)
    assert last_err is not None
    raise last_err


class AramHexSyncer:
    """Syncs and SHA-256 verifies datasets from czgreat/aramhex-data (with connection pool & ETag 304)."""

    def __init__(
        self,
        cache_dir: Path,
        proxy_url: str = "http://127.0.0.1:7900",
        base_url: str = ARAMHEX_RAW_BASE,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.datasets_dir = self.cache_dir / "datasets"
        self.datasets_dir.mkdir(parents=True, exist_ok=True)
        self.proxy_url = proxy_url
        self.base_url = base_url.rstrip("/")
        self._custom_fetcher = fetcher
        self._pool_lock = threading.Lock()
        self._http_client: httpx.Client | None = None
        self._http_proxy_key: str | None = None
        self._fetcher = fetcher or self._pooled_fetch

    def _get_client(self) -> httpx.Client:
        key = (self.proxy_url or "").strip()
        with self._pool_lock:
            if self._http_client is not None and self._http_proxy_key == key:
                return self._http_client
            old = self._http_client
            self._http_client = httpx.Client(
                proxy=key or None,
                trust_env=False,
                timeout=15.0,
                follow_redirects=True,
                headers=_DEFAULT_HEADERS,
            )
            self._http_proxy_key = key
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        return self._http_client

    def close(self) -> None:
        with self._pool_lock:
            old = self._http_client
            self._http_client = None
            self._http_proxy_key = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _pooled_fetch(self, url: str) -> bytes:
        try:
            return http_get_bytes(url, client=self._get_client())
        except Exception:
            if self.proxy_url:
                return http_get_bytes(url, proxy_url="")
            raise

    def _fetch_json(self, rel_path: str, cache_file: Path | None = None) -> dict:
        url = f"{self.base_url}/{rel_path.lstrip('/')}"
        try:
            if self._custom_fetcher is None and cache_file is not None:
                try:
                    raw, from_304 = http_get_cached(url, cache_file, client=self._get_client())
                except Exception:
                    if self.proxy_url:
                        raw, from_304 = http_get_cached(url, cache_file, proxy_url="")
                    else:
                        raise
                parsed = json.loads(raw.decode("utf-8"))
                if not from_304:
                    atomic_write_bytes(cache_file, raw)
                return parsed
            raw = self._fetcher(url)
            parsed = json.loads(raw.decode("utf-8"))
            if cache_file is not None:
                atomic_write_bytes(cache_file, raw)
            return parsed
        except Exception as exc:
            if cache_file is not None and cache_file.exists():
                logger.warning("Using cached %s after fetch error: %s", cache_file.name, exc)
                return json.loads(cache_file.read_text(encoding="utf-8"))
            raise

    def fetch_index(self) -> list[DatasetDescriptor]:
        index_cache = self.datasets_dir / "index.json"
        data = self._fetch_json("index.json", cache_file=index_cache)
        out: list[DatasetDescriptor] = []
        for item in data.get("datasets", []):
            md = item.get("matches_detail") or {}
            out.append(
                DatasetDescriptor(
                    patch=str(item.get("patch", "")),
                    mode=str(item.get("mode", "normal")),
                    mode_label=str(item.get("mode_label") or item.get("mode") or "常规"),
                    path=str(item.get("path", "")),
                    snapshot_at=str(item.get("snapshot_at", "")),
                    bands=bool(item.get("bands", False)),
                    seq=int(item.get("seq", 0)),
                    valid_matches=int(md.get("valid", item.get("matches", 0)) or 0),
                )
            )
        return out

    def resolve_descriptor(self, patch: str = "latest", mode: str = "normal") -> DatasetDescriptor:
        datasets = self.fetch_index()
        if patch == "latest":
            latest_cache = self.datasets_dir / "latest.json"
            try:
                latest = self._fetch_json("latest.json", cache_file=latest_cache)
                target_patch = str(latest.get("patch", ""))
                target_mode = mode or str(latest.get("mode", "normal"))
                for ds in datasets:
                    if ds.patch == target_patch and ds.mode == target_mode:
                        return ds
                for ds in datasets:
                    if ds.patch == target_patch:
                        return ds
            except Exception:
                pass
            if datasets:
                for ds in datasets:
                    if ds.mode == mode:
                        return ds
                return datasets[0]
            raise RuntimeError("No datasets available in index.json")

        for ds in datasets:
            if ds.patch == patch and ds.mode == mode:
                return ds
        for ds in datasets:
            if ds.patch == patch:
                return ds
        if datasets:
            return datasets[0]
        raise RuntimeError(f"Dataset {patch}/{mode} not found")

    def sync_dataset(
        self,
        patch: str = "latest",
        mode: str = "normal",
        band: str = "",
        progress_cb: Callable[[str], None] | None = None,
    ) -> tuple[DatasetDescriptor, Path]:
        desc = self.resolve_descriptor(patch=patch, mode=mode)
        local_dir = self.datasets_dir / desc.patch / desc.mode
        local_dir.mkdir(parents=True, exist_ok=True)

        if progress_cb:
            progress_cb(f"检查元数据 {desc.patch}/{desc.mode}...")

        meta_path = local_dir / "meta.json"
        meta = self._fetch_json(f"{desc.path}/meta.json", cache_file=meta_path)
        files_manifest: dict[str, dict] = meta.get("files") or {}

        rel_targets: list[str] = [f for f in REQUIRED_DATASET_FILES if f != "meta.json"]
        optional_targets: set[str] = set()

        for opt_file in OPTIONAL_DATASET_FILES:
            if (not files_manifest) or (opt_file in files_manifest):
                if opt_file not in rel_targets:
                    rel_targets.append(opt_file)
                optional_targets.add(opt_file)

        meta_bands = meta.get("bands")
        has_bands = bool(meta_bands) if meta_bands is not None else bool(desc.bands)
        if has_bands:
            manifest_has_high = any(str(k).startswith("bands/high/") for k in files_manifest)
            for bf in HIGH_BAND_DATASET_FILES:
                if (not files_manifest) or (not manifest_has_high) or (bf in files_manifest):
                    if bf not in rel_targets:
                        rel_targets.append(bf)
                    optional_targets.add(bf)
            for opt_bf in OPTIONAL_HIGH_BAND_FILES:
                if (not files_manifest) or (not manifest_has_high) or (opt_bf in files_manifest):
                    if opt_bf not in rel_targets:
                        rel_targets.append(opt_bf)
                    optional_targets.add(opt_bf)

        if band and has_bands:
            for bf in (f"bands/{band}/heroes.json", f"bands/{band}/hero_augments.json"):
                if bf in files_manifest and bf not in rel_targets:
                    rel_targets.append(bf)
                    optional_targets.add(bf)

        for rel_file in rel_targets:
            manifest_entry = files_manifest.get(rel_file)
            if isinstance(manifest_entry, str):
                expected_sha = manifest_entry.strip().lower()
            elif isinstance(manifest_entry, dict):
                expected_sha = str(manifest_entry.get("sha256", "")).strip().lower()
            else:
                expected_sha = ""
            target_path = local_dir / rel_file
            if target_path.exists() and expected_sha:
                if sha256_file(target_path).lower() == expected_sha:
                    continue

            if progress_cb:
                progress_cb(f"同步 {desc.patch}/{desc.mode}/{rel_file}...")
            url = f"{self.base_url}/{desc.path}/{rel_file}"
            is_optional = rel_file in optional_targets
            try:
                raw = self._fetcher(url)
                if expected_sha:
                    actual_sha = sha256_bytes(raw).lower()
                    if actual_sha != expected_sha:
                        raise ValueError(
                            f"SHA-256 mismatch for {rel_file}: expected {expected_sha}, got {actual_sha}"
                        )
                # Ensure valid JSON before persisting
                json.loads(raw.decode("utf-8"))
                atomic_write_bytes(target_path, raw)
            except ValueError as exc:
                if is_optional:
                    logger.warning("Optional file %s failed validation (skipped): %s", rel_file, exc)
                else:
                    raise
            except Exception as exc:
                if target_path.exists():
                    logger.warning("Keeping existing cached %s after error: %s", rel_file, exc)
                elif rel_file in ("heroes.json", "augments.json", "hero_augments.json"):
                    raise
                elif is_optional:
                    logger.debug("Optional file %s unavailable (skipped): %s", rel_file, exc)
                else:
                    logger.warning("Optional file %s unavailable: %s", rel_file, exc)

        return desc, local_dir

    def find_any_cached_dataset(self) -> tuple[DatasetDescriptor, Path] | None:
        """Offline fallback: locate any complete cached dataset on disk."""
        if not self.datasets_dir.exists():
            return None
        candidates: list[tuple[str, str, Path]] = []
        for patch_dir in sorted(self.datasets_dir.iterdir(), reverse=True):
            if not patch_dir.is_dir():
                continue
            for mode_dir in sorted(patch_dir.iterdir()):
                if not mode_dir.is_dir():
                    continue
                if (mode_dir / "heroes.json").exists() and (mode_dir / "hero_augments.json").exists():
                    candidates.append((patch_dir.name, mode_dir.name, mode_dir))
        if not candidates:
            return None
        # Prefer normal mode on newest patch
        candidates.sort(key=lambda c: (c[0], c[1] == "normal"), reverse=True)
        patch, mode, d = candidates[0]
        meta_path = d / "meta.json"
        snap = ""
        bands = False
        if meta_path.exists():
            try:
                m = json.loads(meta_path.read_text(encoding="utf-8"))
                snap = str(m.get("snapshot_at", ""))
                high_dir = d / "bands" / "high"
                bands = any(
                    (high_dir / fname).exists()
                    for fname in (
                        "heroes.json",
                        "hero_augments.json",
                        "hero_item_recommendations.json",
                        "core_builds.json",
                    )
                )
            except Exception:
                pass
        return (
            DatasetDescriptor(
                patch=patch,
                mode=mode,
                mode_label="常规" if mode == "normal" else mode,
                path=f"patches/{patch}/{mode}",
                snapshot_at=snap,
                bands=bands,
            ),
            d,
        )
