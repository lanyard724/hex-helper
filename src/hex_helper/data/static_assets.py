"""Static metadata (DDragon item stats, Tencent CN heroes, CDragon augments/spells) & icon cache."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import httpx

from hex_helper.data.aramhex_sync import atomic_write_bytes, http_get_bytes, http_get_cached
from hex_helper.data.provider import ItemStaticInfo

logger = logging.getLogger(__name__)

DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_ITEM_URL_TMPL = "https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/item.json"
DDRAGON_ITEM_ICON_TMPL = "https://ddragon.leagueoflegends.com/cdn/{ver}/img/item/{item_id}.png"
TENCENT_HERO_LIST_URL = "https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js"
CDRAGON_V1_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1"
CDRAGON_ASSET_BASE = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/assets"
CDRAGON_ITEM_ICON_TMPL = "https://cdn.communitydragon.org/latest/item/{item_id}/icon"
TENCENT_IMG_BASE = "https://game.gtimg.cn/images/lol/act/img"

ICON_404_NEGATIVE_TTL_S = 6.0 * 3600.0
ICON_TRANSIENT_FAIL_TTL_S = 300.0


def _is_http_404(exc: BaseException) -> bool:
    """Return True iff exc (or its cause chain) represents an HTTP 404 Not Found error."""
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, httpx.HTTPStatusError) and cur.response is not None:
            if cur.response.status_code == 404:
                return True
        resp = getattr(cur, "response", None)
        if resp is not None and getattr(resp, "status_code", None) == 404:
            return True
        if getattr(cur, "status_code", None) == 404:
            return True
        msg = str(cur)
        if "404" in msg and (
            "Not Found" in msg or "HTTP" in msg or msg.strip() == "404" or "status 404" in msg.lower()
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False

GRIEVOUS_ITEM_IDS = {"3033", "3165", "3075", "3123", "3916", "3076", "6609"}
SHIELD_BREAK_ITEM_IDS = {"6695"}

FALLBACK_SPELLS: dict[str, tuple[str, str]] = {
    "1": ("净化", "SummonerBoost"),
    "3": ("虚弱", "SummonerExhaust"),
    "4": ("闪现", "SummonerFlash"),
    "6": ("幽灵疾步", "SummonerHaste"),
    "7": ("治疗术", "SummonerHeal"),
    "11": ("惩戒", "SummonerSmite"),
    "12": ("传送", "SummonerTeleport"),
    "13": ("清晰术", "SummonerMana"),
    "14": ("引燃", "SummonerDot"),
    "21": ("屏障", "SummonerBarrier"),
    "32": ("标记", "SummonerSnowball"),
}


class StaticAssetsManager:
    """Manages static metadata JSON and on-demand local icon caching."""

    def __init__(
        self,
        cache_dir: Path,
        proxy_url: str = "",
        fetcher: Callable[[str], bytes] | None = None,
        mirror_base_url: str = "",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.static_dir = self.cache_dir / "static"
        self.icons_dir = self.cache_dir / "icons"
        self.static_dir.mkdir(parents=True, exist_ok=True)
        self.icons_dir.mkdir(parents=True, exist_ok=True)
        self.proxy_url = proxy_url
        self.mirror_base_url = (mirror_base_url or "").strip().rstrip("/")
        self._custom_fetcher = fetcher
        self._pool_lock = threading.Lock()
        self._http_client: httpx.Client | None = None
        self._http_proxy_key: str | None = None
        self._icon_fail_until: dict[tuple[str, str], float] = {}
        self._icon_404_until: dict[tuple[str, str], float] = {}
        self._url_404_until: dict[str, float] = {}
        self._ddragon_version: str = "16.23.1"
        self._fetcher = fetcher or self._pooled_fetch

        self.items: dict[str, ItemStaticInfo] = {}
        self.hero_aliases: dict[str, str] = {}  # hero_id -> English alias e.g. "Jinx"
        self.hero_cn_names: dict[str, str] = {}  # hero_id -> CN name e.g. "金克丝"
        self.hero_titles: dict[str, str] = {}
        self.augment_icon_urls: dict[str, str] = {}  # augment_id -> CDragon PNG URL
        self.spell_names: dict[str, str] = {k: v[0] for k, v in FALLBACK_SPELLS.items()}
        self.spell_keys: dict[str, str] = {k: v[1] for k, v in FALLBACK_SPELLS.items()}

    def _get_client(self) -> httpx.Client:
        key = (self.proxy_url or "").strip()
        with self._pool_lock:
            if self._http_client is not None and self._http_proxy_key == key:
                return self._http_client
            old = self._http_client
            self._http_client = httpx.Client(
                proxy=key or None,
                trust_env=False,
                timeout=12.0,
                follow_redirects=True,
                headers={"User-Agent": "HexHelper/1.1", "Accept-Encoding": "gzip, deflate"},
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
        except Exception as exc:
            if _is_http_404(exc):
                raise
            if self.proxy_url:
                return http_get_bytes(url, proxy_url="")
            raise

    def _fetch_and_cache(self, url: str, filename: str) -> dict | list | None:
        target = self.static_dir / filename
        try:
            if self._custom_fetcher is None:
                try:
                    raw, from_304 = http_get_cached(url, target, client=self._get_client())
                except Exception:
                    if self.proxy_url:
                        raw, from_304 = http_get_cached(url, target, proxy_url="")
                    else:
                        raise
                parsed = json.loads(raw.decode("utf-8"))
                if not from_304:
                    atomic_write_bytes(target, raw)
                return parsed
            raw = self._fetcher(url)
            parsed = json.loads(raw.decode("utf-8"))
            atomic_write_bytes(target, raw)
            return parsed
        except Exception as exc:
            logger.debug("Static fetch failed for %s: %s", filename, exc)
            if target.exists():
                try:
                    return json.loads(target.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return None

    def load_cached(self, dataset_item_meta: dict[str, dict] | None = None) -> None:
        """Load any already-cached static files without hitting the network."""
        versions = self._read_json("ddragon_versions.json")
        if isinstance(versions, list) and versions:
            self._ddragon_version = str(versions[0])
        self._parse_ddragon_items(self._read_json("ddragon_items.json"), dataset_item_meta)
        self._parse_tencent_heroes(self._read_json("tencent_heroes.json"))
        self._parse_cdragon_augments(self._read_json("cdragon_augments.json"))
        self._parse_cdragon_spells(self._read_json("cdragon_spells.json"))

    def _fetch_from_mirror_or_url(
        self,
        primary_url: str,
        filename: str,
        *,
        prefer_domestic_direct: bool = False,
    ) -> dict | list | None:
        if prefer_domestic_direct and self._custom_fetcher is None:
            direct = self._fetch_and_cache(primary_url, filename)
            if direct is not None:
                return direct
        if self.mirror_base_url:
            mirror_urls = [f"{self.mirror_base_url}/{filename}"]
            if not self.mirror_base_url.endswith("/static"):
                mirror_urls.append(f"{self.mirror_base_url}/static/{filename}")
            target = self.static_dir / filename
            for murl in mirror_urls:
                try:
                    if self._custom_fetcher is None:
                        raw, from_304 = http_get_cached(murl, target, client=self._get_client())
                        parsed = json.loads(raw.decode("utf-8"))
                        if not from_304:
                            atomic_write_bytes(target, raw)
                        return parsed
                    raw = self._fetcher(murl)
                    parsed = json.loads(raw.decode("utf-8"))
                    atomic_write_bytes(target, raw)
                    return parsed
                except Exception as exc:
                    logger.debug("Mirror static fetch failed for %s (%s): %s", filename, murl, exc)
            if target.exists():
                try:
                    return json.loads(target.read_text(encoding="utf-8"))
                except Exception:
                    pass
            return None
        return self._fetch_and_cache(primary_url, filename)

    def sync_all(
        self,
        dataset_item_meta: dict[str, dict] | None = None,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        if progress_cb:
            progress_cb("同步 DDragon 装备属性与国服英雄静态库...")

        dd_ver = self._ddragon_version or "16.23.1"
        versions = self._fetch_from_mirror_or_url(DDRAGON_VERSIONS_URL, "ddragon_versions.json")
        if isinstance(versions, list) and versions:
            dd_ver = str(versions[0])
            self._ddragon_version = dd_ver

        dd_items = self._fetch_from_mirror_or_url(
            DDRAGON_ITEM_URL_TMPL.format(ver=dd_ver), "ddragon_items.json"
        )
        self._parse_ddragon_items(dd_items, dataset_item_meta)

        # Tencent hero_list.js is hosted on CN domestic CDN (game.gtimg.cn ~60ms, zero GCP egress)
        heroes = self._fetch_from_mirror_or_url(
            TENCENT_HERO_LIST_URL, "tencent_heroes.json", prefer_domestic_direct=True
        )
        self._parse_tencent_heroes(heroes)

        cd_augs = self._fetch_from_mirror_or_url(
            f"{CDRAGON_V1_BASE}/cherry-augments.json", "cdragon_augments.json"
        )
        self._parse_cdragon_augments(cd_augs)

        cd_spells = self._fetch_from_mirror_or_url(
            f"{CDRAGON_V1_BASE}/summoner-spells.json", "cdragon_spells.json"
        )
        self._parse_cdragon_spells(cd_spells)

    def _read_json(self, filename: str) -> dict | list | None:
        p = self.static_dir / filename
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _parse_ddragon_items(
        self,
        dd_raw: dict | list | None,
        dataset_item_meta: dict[str, dict] | None = None,
    ) -> None:
        ds_meta = dataset_item_meta or {}
        data = dd_raw.get("data", {}) if isinstance(dd_raw, dict) else {}
        parsed: dict[str, ItemStaticInfo] = {}

        all_ids = set(data.keys()) | set(ds_meta.keys())
        for iid in all_ids:
            entry = data.get(iid) or {}
            # Some ARAM Mayhem items have 12xxxx / 22xxxx variants that mirror base 4-digit IDs
            if not entry and len(iid) == 6 and iid[2:] in data:
                entry = data[iid[2:]]
            meta = ds_meta.get(iid) or {}
            name = str(entry.get("name") or meta.get("name") or iid)
            gold = int((entry.get("gold") or {}).get("total", 0) or 0)
            tags = tuple(str(t) for t in (entry.get("tags") or []))
            stats = {str(k): float(v) for k, v in (entry.get("stats") or {}).items()}
            desc = str(entry.get("description") or "")
            cat = str(meta.get("category") or ("complete_equipment" if gold >= 2200 else "other"))
            icon_url = str(meta.get("icon") or f"{TENCENT_IMG_BASE}/item/{iid}.png")
            has_gw = (iid in GRIEVOUS_ITEM_IDS) or ("重伤" in desc)
            has_sb = (iid in SHIELD_BREAK_ITEM_IDS) or ("护盾" in desc and "削减" in desc)
            comps = tuple(str(c) for c in (entry.get("from") or []))
            parsed[str(iid)] = ItemStaticInfo(
                item_id=str(iid),
                name=name,
                gold=gold,
                tags=tags,
                stats=stats,
                description=desc,
                category=cat,
                icon_url=icon_url,
                has_grievous=has_gw,
                has_shield_break=has_sb,
                components=comps,
            )
        if parsed:
            self.items = parsed

    def _parse_tencent_heroes(self, raw: dict | list | None) -> None:
        if not isinstance(raw, dict):
            return
        for h in raw.get("hero", []):
            hid = str(h.get("heroId", "")).strip()
            if not hid:
                continue
            alias = str(h.get("alias", "")).strip()
            # Tencent hero_list.js swaps 'name' (title) and 'title' (champion short name)
            cn_title = str(h.get("name", "")).strip()
            cn_name = str(h.get("title", "")).strip()
            if alias:
                self.hero_aliases[hid] = alias
            if cn_name:
                self.hero_cn_names[hid] = cn_name
            if cn_title:
                self.hero_titles[hid] = cn_title

    def _parse_cdragon_augments(self, raw: dict | list | None) -> None:
        if not isinstance(raw, list):
            return
        for entry in raw:
            aid = str(entry.get("id", "")).strip()
            icon_path = str(entry.get("augmentSmallIconPath", "")).strip()
            if aid and icon_path:
                rel = icon_path.lower().lstrip("/")
                while True:
                    if rel.startswith("lol-game-data/assets/"):
                        rel = rel[len("lol-game-data/assets/") :].lstrip("/")
                    elif rel.startswith("assets/"):
                        rel = rel[len("assets/") :].lstrip("/")
                    else:
                        break
                self.augment_icon_urls[aid] = f"{CDRAGON_ASSET_BASE}/{rel}"

    def _parse_cdragon_spells(self, raw: dict | list | None) -> None:
        if not isinstance(raw, list):
            return
        for entry in raw:
            sid = str(entry.get("id", "")).strip()
            name = str(entry.get("name", "")).strip()
            if sid and name:
                self.spell_names[sid] = name

    def get_item_info(self, item_id: str) -> ItemStaticInfo:
        iid = str(item_id)
        if iid in self.items:
            return self.items[iid]
        if len(iid) == 6 and iid[2:] in self.items:
            return self.items[iid[2:]]
        return ItemStaticInfo(
            item_id=iid,
            name=f"装备#{iid}",
            icon_url=f"{TENCENT_IMG_BASE}/item/{iid}.png",
        )

    def get_item_name(self, item_id: str) -> str:
        return self.get_item_info(item_id).name

    def get_spell_name(self, spell_id: str) -> str:
        return self.spell_names.get(str(spell_id), f"技能#{spell_id}")

    def get_champion_alias(self, hero_id: str) -> str:
        return self.hero_aliases.get(str(hero_id), "")

    def get_icon_url(self, kind: str, key: str) -> str | None:
        k = str(key)
        if kind == "champion":
            alias = self.hero_aliases.get(k, k)
            if not alias or alias.isdigit():
                return None
            return f"{TENCENT_IMG_BASE}/champion/{alias}.png"
        if kind == "item":
            return self.get_item_info(k).icon_url
        if kind == "spell":
            skey = self.spell_keys.get(k)
            if skey:
                return f"{TENCENT_IMG_BASE}/spell/{skey}.png"
            return None
        if kind == "augment":
            return self.augment_icon_urls.get(k)
        return None

    def ensure_icon(self, kind: str, key: str) -> Path | None:
        """Download and disk-cache an icon PNG (CN CDN -> DDragon/CDragon fallback + 6h 404 negative cache)."""
        safe_key = "".join(ch for ch in str(key) if ch.isalnum() or ch in ("_", "-"))
        if not safe_key:
            return None
        local = self.icons_dir / kind / f"{safe_key}.png"
        if local.exists() and local.stat().st_size > 64:
            return local

        fail_key = (kind, safe_key)
        now = time.monotonic()
        if self._icon_404_until.get(fail_key, 0.0) > now or self._icon_fail_until.get(fail_key, 0.0) > now:
            return None

        url = self.get_icon_url(kind, key)
        candidate_urls: list[str] = []

        def _append_unique(cand: str) -> None:
            if cand and cand not in candidate_urls:
                candidate_urls.append(cand)

        if kind == "item" and safe_key.isdigit():
            _append_unique(f"{TENCENT_IMG_BASE}/item/{safe_key}.png")
            if url:
                _append_unique(url)
            base_id = safe_key[2:] if (len(safe_key) == 6 and safe_key[:2] in ("12", "22", "77")) else ""
            if base_id:
                _append_unique(f"{TENCENT_IMG_BASE}/item/{base_id}.png")
            dd_ver = self._ddragon_version or "16.23.1"
            _append_unique(DDRAGON_ITEM_ICON_TMPL.format(ver=dd_ver, item_id=safe_key))
            if base_id:
                _append_unique(DDRAGON_ITEM_ICON_TMPL.format(ver=dd_ver, item_id=base_id))
            _append_unique(CDRAGON_ITEM_ICON_TMPL.format(item_id=safe_key))
            if base_id:
                _append_unique(CDRAGON_ITEM_ICON_TMPL.format(item_id=base_id))
        elif url:
            _append_unique(url)
        if not candidate_urls:
            return None

        saw_404 = False
        saw_non_404 = False
        for cand_url in candidate_urls:
            if self._url_404_until.get(cand_url, 0.0) > now:
                saw_404 = True
                continue
            try:
                raw = self._fetcher(cand_url)
                if len(raw) > 64:
                    atomic_write_bytes(local, raw)
                    self._icon_fail_until.pop(fail_key, None)
                    self._icon_404_until.pop(fail_key, None)
                    return local
            except Exception as exc:
                if _is_http_404(exc):
                    saw_404 = True
                    self._url_404_until[cand_url] = now + ICON_404_NEGATIVE_TTL_S
                else:
                    saw_non_404 = True
                logger.debug("Icon fetch failed %s/%s (%s): %s", kind, key, cand_url, exc)

        if saw_404 and not saw_non_404:
            self._icon_404_until[fail_key] = now + ICON_404_NEGATIVE_TTL_S
            logger.debug(
                "Icon %s/%s returned 404 across all CDNs; negative-cached for %.0fh",
                kind,
                safe_key,
                ICON_404_NEGATIVE_TTL_S / 3600.0,
            )
        else:
            self._icon_fail_until[fail_key] = now + ICON_TRANSIENT_FAIL_TTL_S
        return None
