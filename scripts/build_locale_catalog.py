"""Offline Riot CommunityDragon locale catalog builder and verifier for HexHelper v1.5.2+.

Fetches (or loads from local cache dir) official Riot `zh_my` (Singapore/Malaysia Simplified)
and `zh_tw` (Taiwan Traditional) cherry-augments, items, and champion-summary JSONs, validates
100% coverage against the local active ARAM Mayhem dataset, extracts a compact fallback T2S
character table, and emits `src/hex_helper/data/locale_catalog_data.py` as a zlib-9 + base64
module compiled straight into `HexHelper.exe`'s embedded PYZ archive (compatible with code-only
`_patch.zip` updates).
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import gzip
import json
import os
import sys
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "src" / "hex_helper" / "data" / "locale_catalog_data.py"
DEFAULT_DATASET_DIR = Path(
    os.environ.get(
        "HEX_HELPER_DATASET_DIR",
        str(Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "HexHelper" / "cache" / "datasets" / "16.23" / "normal"),
    )
)

CDRAGON_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global"
TARGET_LOCALES: tuple[str, ...] = ("zh_my", "zh_tw")
MAX_B64_BYTES = 45_000

# CJK / Shinjitai / variant overrides verified against WinRT zh-Hans-CN OCR on zh_tw card titles
VARIANT_T2S_OVERRIDES: dict[str, str] = {
    "絕": "绝",
    "絶": "绝",  # U+7D76 Japanese Shinjitai frequently emitted by WinRT zh-Hans-CN for 大絕覺醒
    "祕": "秘",
    "內": "内",
    "恆": "恒",
    "盜": "盗",
    "昇": "升",
    "臺": "台",
    "裡": "里",
    "啓": "启",
    "啟": "启",
    "爲": "为",
    "眞": "真",
    "羣": "群",
    "峯": "峰",
    "迴": "回",
    "砲": "炮",
}

_FILE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("augments", "v1/cherry-augments.json", "cherry_{loc}.json"),
    ("items", "v1/items.json", "items_{loc}.json"),
    ("champions", "v1/champion-summary.json", "champs_{loc}.json"),
)


def _win32_lcmap_t2s_char(ch: str) -> str:
    if os.name != "nt" or not ch:
        return ch
    kernel32 = ctypes.windll.kernel32
    lcm_simplified = 0x02000000
    n = kernel32.LCMapStringEx("zh-CN", lcm_simplified, ch, len(ch), None, 0, None, None, 0)
    if n <= 0:
        return ch
    buf = ctypes.create_unicode_buffer(n + 1)
    kernel32.LCMapStringEx("zh-CN", lcm_simplified, ch, len(ch), buf, n, None, None, 0)
    return buf.value or ch


def _fetch_or_load_json(url: str, local_cache: Path | None, proxy_url: str = "") -> object:
    if local_cache is not None and local_cache.is_file() and local_cache.stat().st_size > 100:
        return json.loads(local_cache.read_text(encoding="utf-8"))

    handlers: list[urllib.request.BaseHandler] = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={"Accept-Encoding": "gzip", "User-Agent": "HexHelperLocaleBuilder/1.5.2"},
    )
    with opener.open(req, timeout=45) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    if local_cache is not None:
        local_cache.parent.mkdir(parents=True, exist_ok=True)
        local_cache.write_bytes(raw)
    return json.loads(raw.decode("utf-8"))


def _extract_locale_entries(
    *,
    locale: str,
    from_dir: Path | None,
    proxy_url: str,
) -> dict[str, dict[str, str]]:
    parsed: dict[str, object] = {}
    for kind, rel_url, cache_tpl in _FILE_SPECS:
        cache_path = (from_dir / cache_tpl.format(loc=locale)) if from_dir is not None else None
        url = f"{CDRAGON_BASE}/{locale}/{rel_url}"
        parsed[kind] = _fetch_or_load_json(url, cache_path, proxy_url=proxy_url)

    aug_map = {
        str(int(entry["id"])): str(entry.get("nameTRA") or "").strip()
        for entry in parsed["augments"]  # type: ignore[iterable]
        if isinstance(entry, dict) and entry.get("id") is not None and str(entry.get("nameTRA") or "").strip()
    }
    item_map = {
        str(int(entry["id"])): str(entry.get("name") or "").strip()
        for entry in parsed["items"]  # type: ignore[iterable]
        if isinstance(entry, dict) and entry.get("id") is not None and str(entry.get("name") or "").strip()
    }
    hero_map = {
        str(int(entry["id"])): str(entry.get("name") or "").strip()
        for entry in parsed["champions"]  # type: ignore[iterable]
        if isinstance(entry, dict)
        and entry.get("id") is not None
        and int(entry["id"]) > 0
        and str(entry.get("name") or "").strip()
    }
    return {
        "augments": dict(sorted(aug_map.items(), key=lambda kv: int(kv[0]))),
        "items": dict(sorted(item_map.items(), key=lambda kv: int(kv[0]))),
        "heroes": dict(sorted(hero_map.items(), key=lambda kv: int(kv[0]))),
    }


def _build_fallback_t2s_map(tw_catalog: dict[str, dict[str, str]]) -> dict[str, str]:
    chars: set[str] = set()
    for bucket in tw_catalog.values():
        for text in bucket.values():
            for ch in text:
                if "\u4e00" <= ch <= "\u9fff":
                    chars.add(ch)
    out: dict[str, str] = {}
    for ch in sorted(chars):
        mapped = VARIANT_T2S_OVERRIDES.get(ch, _win32_lcmap_t2s_char(ch))
        if mapped and mapped != ch:
            out[ch] = mapped
    for k, v in VARIANT_T2S_OVERRIDES.items():
        if v and v != k:
            out[k] = v
    return dict(sorted(out.items()))


def _load_active_dataset_ids(dataset_dir: Path) -> tuple[set[str], set[str]]:
    aug_path = dataset_dir / "augments.json"
    hero_path = dataset_dir / "heroes.json"
    if not (aug_path.is_file() and hero_path.is_file()):
        return set(), set()

    raw_augs = json.loads(aug_path.read_text(encoding="utf-8"))
    if isinstance(raw_augs, dict):
        active_augs = {str(k) for k in raw_augs.keys()}
    else:
        active_augs = {str(x["id"]) for x in raw_augs if isinstance(x, dict) and "id" in x}

    raw_heroes = json.loads(hero_path.read_text(encoding="utf-8"))
    if isinstance(raw_heroes, dict):
        active_heroes = {str(k) for k in raw_heroes.keys()}
    else:
        active_heroes = {
            str(x.get("id") or x.get("hero_id"))
            for x in raw_heroes
            if isinstance(x, dict) and (x.get("id") or x.get("hero_id"))
        }
    return active_augs, active_heroes


def verify_payload(payload: dict[str, object], dataset_dir: Path | None, b64_bytes_len: int) -> dict[str, object]:
    locales = payload.get("locales")
    if not isinstance(locales, dict):
        raise AssertionError("Payload missing 'locales' mapping")
    active_augs, active_heroes = (
        _load_active_dataset_ids(dataset_dir) if dataset_dir is not None else (set(), set())
    )

    stats: dict[str, object] = {"b64_bytes": b64_bytes_len, "locales": {}}
    if b64_bytes_len > MAX_B64_BYTES:
        raise AssertionError(f"Base64 payload {b64_bytes_len} exceeds budget {MAX_B64_BYTES}")

    for loc in TARGET_LOCALES:
        entry = locales.get(loc)
        if not isinstance(entry, dict):
            raise AssertionError(f"Missing locale {loc}")
        augs = entry.get("augments") or {}
        items = entry.get("items") or {}
        heroes = entry.get("heroes") or {}
        if len(augs) < 500 or len(items) < 850 or len(heroes) < 170:
            raise AssertionError(
                f"[{loc}] unexpected catalog size: augs={len(augs)} items={len(items)} heroes={len(heroes)}"
            )
        missing_augs = sorted(aid for aid in active_augs if aid not in augs)
        missing_heroes = sorted(hid for hid in active_heroes if hid not in heroes)
        if missing_augs:
            raise AssertionError(f"[{loc}] missing active augments: {missing_augs}")
        if missing_heroes:
            raise AssertionError(f"[{loc}] missing active heroes: {missing_heroes}")
        stats["locales"][loc] = {  # type: ignore[index]
            "augments": len(augs),
            "active_aug_cov": f"{len(active_augs) - len(missing_augs)}/{len(active_augs)}" if active_augs else "n/a",
            "items": len(items),
            "heroes": len(heroes),
            "active_hero_cov": f"{len(active_heroes) - len(missing_heroes)}/{len(active_heroes)}" if active_heroes else "n/a",
        }

    fb = payload.get("fallback_t2s") or {}
    if not isinstance(fb, dict) or len(fb) < 150:
        raise AssertionError(f"fallback_t2s too small: {len(fb) if isinstance(fb, dict) else 0}")
    for req_k, req_v in VARIANT_T2S_OVERRIDES.items():
        if fb.get(req_k) != req_v:
            raise AssertionError(f"Variant T2S mismatch for {req_k!r}: got {fb.get(req_k)!r}, expected {req_v!r}")
    stats["fallback_t2s_chars"] = len(fb)
    return stats


def build_and_write(
    *,
    from_dir: Path | None,
    proxy_url: str,
    dataset_dir: Path,
    out_path: Path,
) -> dict[str, object]:
    locales_data: dict[str, dict[str, dict[str, str]]] = {}
    for loc in TARGET_LOCALES:
        locales_data[loc] = _extract_locale_entries(
            locale=loc,
            from_dir=from_dir,
            proxy_url=proxy_url,
        )

    fallback_t2s = _build_fallback_t2s_map(locales_data["zh_tw"])
    payload = {
        "schema": 1,
        "source": "cdragon/latest",
        "locales": locales_data,
        "fallback_t2s": fallback_t2s,
    }
    raw_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    z_bytes = zlib.compress(raw_bytes, level=9)
    b64_text = base64.b64encode(z_bytes).decode("ascii")
    stats = verify_payload(payload, dataset_dir, len(b64_text.encode("ascii")))
    stats["raw_bytes"] = len(raw_bytes)
    stats["zlib_bytes"] = len(z_bytes)

    lines = [
        '"""Auto-generated compressed overseas Chinese Riot locale dictionary (embedded into PYZ).',
        "",
        "Do not edit manually; regenerate via: python scripts/build_locale_catalog.py",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "LOCALE_CATALOG_SCHEMA: int = 1",
        f"LOCALE_CATALOG_RAW_BYTES: int = {len(raw_bytes)}",
        f"LOCALE_CATALOG_ZLIB_BYTES: int = {len(z_bytes)}",
        "LOCALE_CATALOG_B64_ZLIB: str = (",
    ]
    for i in range(0, len(b64_text), 100):
        chunk = b64_text[i : i + 100]
        lines.append(f'    "{chunk}"')
    lines.append(")")
    lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return stats


def check_existing(out_path: Path, dataset_dir: Path) -> dict[str, object]:
    if not out_path.is_file():
        raise FileNotFoundError(f"Generated locale module missing: {out_path}")
    ns: dict[str, object] = {}
    exec(out_path.read_text(encoding="utf-8"), ns)
    b64_str = str(ns.get("LOCALE_CATALOG_B64_ZLIB") or "")
    raw_bytes = zlib.decompress(base64.b64decode(b64_str.encode("ascii")))
    payload = json.loads(raw_bytes.decode("utf-8"))
    stats = verify_payload(payload, dataset_dir, len(b64_str.encode("ascii")))
    stats["raw_bytes"] = len(raw_bytes)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build/verify embedded overseas Riot locale catalog")
    parser.add_argument("--proxy", default="", help="Optional HTTP(S) proxy URL for CDragon downloads")
    parser.add_argument("--from-dir", default="", help="Optional directory containing cached cherry/items/champs JSONs")
    parser.add_argument("--dataset-dir", default=str(DEFAULT_DATASET_DIR), help="Active dataset directory for coverage check")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output Python module path")
    parser.add_argument("--check", action="store_true", help="Verify existing generated module without rebuilding")
    args = parser.parse_args(argv)

    out_path = Path(args.out)
    dataset_dir = Path(args.dataset_dir)
    if args.check:
        stats = check_existing(out_path, dataset_dir)
        print("[locale-check] PASS:", json.dumps(stats, ensure_ascii=False))
        return 0

    from_dir = Path(args.from_dir) if args.from_dir else None
    stats = build_and_write(
        from_dir=from_dir,
        proxy_url=str(args.proxy or "").strip(),
        dataset_dir=dataset_dir,
        out_path=out_path,
    )
    print("[locale-build] PASS:", json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
