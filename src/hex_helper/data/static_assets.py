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
TENCENT_HERO_DETAIL_URL_TMPL = "https://game.gtimg.cn/images/lol/act/img/js/hero/{hero_id}.js"
TENCENT_SKIN_BIG_URL_TMPL = "https://game.gtimg.cn/images/lol/act/img/skin/big{skin_id}.jpg"
TENCENT_SKIN_LOADING_URL_TMPL = "https://game.gtimg.cn/images/lol/act/img/skinloading/{skin_id}.jpg"
TENCENT_SKIN_CENTER_URL_TMPL = "https://game.gtimg.cn/images/lol/act/img/center/{skin_id}.jpg"
DDRAGON_SKIN_SPLASH_URL_TMPL = "https://ddragon.leagueoflegends.com/cdn/img/champion/splash/{alias}_{skin_num}.jpg"
DDRAGON_SKIN_LOADING_URL_TMPL = "https://ddragon.leagueoflegends.com/cdn/img/champion/loading/{alias}_{skin_num}.jpg"

ICON_404_NEGATIVE_TTL_S = 6.0 * 3600.0
ICON_TRANSIENT_FAIL_TTL_S = 300.0
SKIN_SPLASH_MIN_BYTES = 256


def _parse_non_neg_int(val: str | int | float | None) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return max(0, val)
    if isinstance(val, float):
        return max(0, int(val)) if val == val else 0
    text = str(val).strip()
    if not text:
        return 0
    try:
        return max(0, int(text))
    except ValueError:
        try:
            fval = float(text)
            return max(0, int(fval)) if fval == fval else 0
        except ValueError:
            return 0


def normalize_skin_id(hero_id: str | int, skin_id: int | str) -> tuple[int, int]:
    """Normalize (hero_id, skin_num_or_full_id) into (full_skin_id, skin_num)."""
    hero_int = _parse_non_neg_int(hero_id)
    raw_skin = _parse_non_neg_int(skin_id)
    if hero_int <= 0:
        if raw_skin >= 1000:
            return raw_skin, raw_skin % 1000
        return raw_skin, raw_skin
    if raw_skin == 0:
        return hero_int * 1000, 0
    if raw_skin >= 1000:
        return raw_skin, raw_skin % 1000
    return hero_int * 1000 + raw_skin, raw_skin


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
        proxy_url: str = "http://127.0.0.1:7900",
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
        self._skin_lock = threading.Lock()
        self._http_client: httpx.Client | None = None
        self._direct_client: httpx.Client | None = None
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

    def _get_direct_client(self) -> httpx.Client:
        with self._pool_lock:
            if self._direct_client is not None:
                return self._direct_client
            self._direct_client = httpx.Client(
                proxy=None,
                trust_env=False,
                timeout=12.0,
                follow_redirects=True,
                headers={"User-Agent": "HexHelper/1.1", "Accept-Encoding": "gzip, deflate"},
            )
            return self._direct_client

    def close(self) -> None:
        with self._pool_lock:
            old = self._http_client
            old_direct = self._direct_client
            self._http_client = None
            self._direct_client = None
            self._http_proxy_key = None
        for client in (old, old_direct):
            if client is not None:
                try:
                    client.close()
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
            if entry:
                gold_dict = entry.get("gold") or {}
                purchasable = bool(gold_dict.get("purchasable", True))
                maps = entry.get("maps") or {}
                aram_flag = bool(maps.get("12", True)) if (isinstance(maps, dict) and maps) else True
                aram_available = bool(aram_flag and purchasable)
            else:
                aram_available = True
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
                aram_available=aram_available,
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

    def _fetch_skin_bytes(self, url: str, *, prefer_direct: bool = True) -> bytes:
        if self._custom_fetcher is not None or self._fetcher != self._pooled_fetch:
            return self._fetcher(url)
        if prefer_direct:
            return http_get_bytes(url, client=self._get_direct_client())
        return self._fetcher(url)

    def _load_hero_skins_payload(
        self,
        hero_id: str,
        *,
        allow_network: bool,
        full_skin_id: int = 0,
    ) -> dict | None:
        safe_hid = "".join(ch for ch in str(hero_id or "").strip() if ch.isalnum() or ch in ("_", "-"))
        if not safe_hid:
            return None
        target = self.static_dir / "hero_skins" / f"{safe_hid}.json"

        cached_payload: dict | None = None
        with self._skin_lock:
            if target.is_file():
                try:
                    parsed = json.loads(target.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        cached_payload = parsed
                except Exception:
                    cached_payload = None

        if cached_payload is not None:
            if not allow_network:
                return cached_payload
            skins = cached_payload.get("skins")
            if isinstance(skins, list):
                if full_skin_id <= 0:
                    return cached_payload
                target_sid_str = str(full_skin_id)
                for s in skins:
                    if isinstance(s, dict) and str(_parse_non_neg_int(s.get("skinId"))) == target_sid_str:
                        return cached_payload

        if not allow_network:
            return None

        js_url = TENCENT_HERO_DETAIL_URL_TMPL.format(hero_id=safe_hid)
        fail_key = ("hero_skins", safe_hid)
        now = time.monotonic()
        if self._url_404_until.get(js_url, 0.0) > now or self._icon_fail_until.get(fail_key, 0.0) > now:
            return cached_payload

        try:
            raw = self._fetch_skin_bytes(js_url, prefer_direct=True)
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                with self._skin_lock:
                    atomic_write_bytes(target, raw)
                self._url_404_until.pop(js_url, None)
                self._icon_fail_until.pop(fail_key, None)
                return parsed
        except Exception as exc:
            if _is_http_404(exc):
                self._url_404_until[js_url] = time.monotonic() + ICON_404_NEGATIVE_TTL_S
            else:
                self._icon_fail_until[fail_key] = time.monotonic() + ICON_TRANSIENT_FAIL_TTL_S
            logger.debug("Hero skin metadata fetch failed for %s: %s", safe_hid, exc)
        return cached_payload

    @staticmethod
    def _extract_skin_art_urls(
        payload: dict | None,
        hero_id: str,
        skin_id: int | str,
    ) -> tuple[int, str, str, str, str, str]:
        """Return (effective_skin_id, skin_name, main_img, loading_img, center_img, hero_alias) with chroma parent backtracking."""
        full_skin_id, _ = normalize_skin_id(hero_id, skin_id)
        if not isinstance(payload, dict):
            return full_skin_id, "", "", "", "", ""

        hero_block = payload.get("hero")
        json_alias = str(hero_block.get("alias") or "").strip() if isinstance(hero_block, dict) else ""

        skins_raw = payload.get("skins")
        if not isinstance(skins_raw, list):
            return full_skin_id, "", "", "", "", json_alias

        skins_by_id: dict[str, dict] = {}
        for entry in skins_raw:
            if not isinstance(entry, dict):
                continue
            sid_int = _parse_non_neg_int(entry.get("skinId"))
            if sid_int > 0:
                skins_by_id[str(sid_int)] = entry

        matched = skins_by_id.get(str(full_skin_id))
        if not isinstance(matched, dict):
            return full_skin_id, "", "", "", "", json_alias

        skin_name = str(matched.get("name") or "").strip()
        effective_skin_id = full_skin_id
        main_img = str(matched.get("mainImg") or "").strip()
        loading_img = str(matched.get("loadingImg") or "").strip()
        center_img = str(matched.get("centerImg") or "").strip()

        if str(matched.get("chromas") or "").strip() == "1":
            belong_id = _parse_non_neg_int(matched.get("chromasBelongId"))
            if belong_id > 0 and belong_id != full_skin_id:
                parent = skins_by_id.get(str(belong_id))
                if isinstance(parent, dict):
                    effective_skin_id = _parse_non_neg_int(parent.get("skinId")) or belong_id
                    parent_img = str(parent.get("mainImg") or "").strip()
                    if parent_img:
                        main_img = parent_img
                    parent_loading = str(parent.get("loadingImg") or "").strip()
                    if parent_loading:
                        loading_img = parent_loading
                    parent_center = str(parent.get("centerImg") or "").strip()
                    if parent_center:
                        center_img = parent_center
                    if not skin_name:
                        skin_name = str(parent.get("name") or "").strip()
                else:
                    effective_skin_id = belong_id

        return effective_skin_id, skin_name, main_img, loading_img, center_img, json_alias

    @staticmethod
    def _extract_skin_entry(
        payload: dict | None,
        hero_id: str,
        skin_id: int | str,
    ) -> tuple[int, str, str, str]:
        """Return (effective_skin_id, skin_name, main_img_url, hero_alias) with chroma parent backtracking."""
        eff_id, skin_name, main_img, _loading_img, _center_img, json_alias = (
            StaticAssetsManager._extract_skin_art_urls(payload, hero_id, skin_id)
        )
        return eff_id, skin_name, main_img, json_alias

    def resolve_skin_info(self, hero_id: str, skin_id: int = 0) -> tuple[int, str, str]:
        """Fetch & cache Tencent hero/{hero_id}.js, resolving (effective_skin_id, skin_name, main_img_url).

        Chromas (chromas == "1") automatically backtrack to their parent skin's skinId and mainImg.
        """
        safe_hid = "".join(ch for ch in str(hero_id or "").strip() if ch.isalnum() or ch in ("_", "-"))
        full_skin_id, _ = normalize_skin_id(safe_hid, skin_id)
        if not safe_hid:
            return full_skin_id, "", ""
        payload = self._load_hero_skins_payload(safe_hid, allow_network=True, full_skin_id=full_skin_id)
        eff_id, skin_name, main_img, _alias = self._extract_skin_entry(payload, safe_hid, skin_id)
        return eff_id, skin_name, main_img

    def _find_cached_skin_file(self, subdir: str, candidate_ids: tuple[int, ...]) -> str:
        target_dir = self.icons_dir / subdir
        seen_ids: set[int] = set()
        for cand_id in candidate_ids:
            if cand_id <= 0 or cand_id in seen_ids:
                continue
            seen_ids.add(cand_id)
            local = target_dir / f"{cand_id}.jpg"
            try:
                if local.is_file() and local.stat().st_size > SKIN_SPLASH_MIN_BYTES:
                    return str(local)
            except OSError:
                continue
        return ""

    def resolve_cached_skin_splash(self, hero_id: str, skin_id: int = 0) -> tuple[str, str]:
        """Zero-network read-only lookup in local icons/skin/*.jpg and static/hero_skins/{hero_id}.json."""
        safe_hid = "".join(ch for ch in str(hero_id or "").strip() if ch.isalnum() or ch in ("_", "-"))
        if not safe_hid:
            return "", ""
        full_skin_id, _ = normalize_skin_id(safe_hid, skin_id)
        payload = self._load_hero_skins_payload(safe_hid, allow_network=False, full_skin_id=full_skin_id)
        eff_id, skin_name, _main_img, _alias = self._extract_skin_entry(payload, safe_hid, skin_id)
        return self._find_cached_skin_file("skin", (eff_id, full_skin_id)), skin_name

    def resolve_cached_skin_variants(self, hero_id: str, skin_id: int = 0) -> tuple[str, str]:
        """Zero-network read-only lookup for cached (skin_loading_path, skin_center_path)."""
        safe_hid = "".join(ch for ch in str(hero_id or "").strip() if ch.isalnum() or ch in ("_", "-"))
        if not safe_hid:
            return "", ""
        full_skin_id, _ = normalize_skin_id(safe_hid, skin_id)
        payload = self._load_hero_skins_payload(safe_hid, allow_network=False, full_skin_id=full_skin_id)
        eff_id, _skin_name, _main_img, _alias = self._extract_skin_entry(payload, safe_hid, skin_id)
        cands = (eff_id, full_skin_id)
        return (
            self._find_cached_skin_file("skin_loading", cands),
            self._find_cached_skin_file("skin_center", cands),
        )

    def _download_skin_candidates(
        self,
        local: Path,
        fail_key: tuple[str, str],
        candidate_urls: list[str],
        *,
        safe_hid: str,
        eff_id: int,
    ) -> bool:
        now = time.monotonic()
        if (
            self._icon_404_until.get(fail_key, 0.0) > now
            or self._icon_fail_until.get(fail_key, 0.0) > now
        ):
            return False

        saw_404 = False
        saw_non_404 = False
        for cand_url in candidate_urls:
            if self._url_404_until.get(cand_url, 0.0) > now:
                saw_404 = True
                continue
            is_tencent = cand_url.startswith(TENCENT_IMG_BASE) or "game.gtimg.cn" in cand_url
            try:
                raw = self._fetch_skin_bytes(cand_url, prefer_direct=is_tencent)
                if len(raw) > SKIN_SPLASH_MIN_BYTES:
                    with self._skin_lock:
                        atomic_write_bytes(local, raw)
                    self._icon_fail_until.pop(fail_key, None)
                    self._icon_404_until.pop(fail_key, None)
                    return True
                saw_non_404 = True
            except Exception as exc:
                if _is_http_404(exc):
                    saw_404 = True
                    self._url_404_until[cand_url] = now + ICON_404_NEGATIVE_TTL_S
                else:
                    saw_non_404 = True
                logger.debug("Skin asset fetch failed %s/%s (%s): %s", safe_hid, eff_id, cand_url, exc)

        if candidate_urls:
            if saw_404 and not saw_non_404:
                self._icon_404_until[fail_key] = now + ICON_404_NEGATIVE_TTL_S
            else:
                self._icon_fail_until[fail_key] = now + ICON_TRANSIENT_FAIL_TTL_S
        return False

    def ensure_skin_splash(self, hero_id: str, skin_id: int = 0) -> tuple[Path | None, str]:
        """Download and disk-cache champion skin splash JPG (Tencent zero-proxy -> DDragon -> base skin)."""
        safe_hid = "".join(ch for ch in str(hero_id or "").strip() if ch.isalnum() or ch in ("_", "-"))
        if not safe_hid:
            return None, ""
        hero_int = _parse_non_neg_int(safe_hid)
        full_skin_id, orig_skin_num = normalize_skin_id(safe_hid, skin_id)

        payload = self._load_hero_skins_payload(safe_hid, allow_network=True, full_skin_id=full_skin_id)
        eff_id, skin_name, main_img, json_alias = self._extract_skin_entry(payload, safe_hid, skin_id)
        if eff_id <= 0:
            return None, skin_name

        if hero_int > 0 and eff_id >= 1000 and eff_id // 1000 == hero_int:
            eff_skin_num = eff_id % 1000
        else:
            _, eff_skin_num = normalize_skin_id(safe_hid, eff_id)

        local = self.icons_dir / "skin" / f"{eff_id}.jpg"
        if local.is_file() and local.stat().st_size > SKIN_SPLASH_MIN_BYTES:
            return local, skin_name

        candidate_urls: list[str] = []

        def _add_cand(u: str) -> None:
            clean = (u or "").strip()
            if clean and clean not in candidate_urls:
                candidate_urls.append(clean)

        _add_cand(main_img)
        _add_cand(TENCENT_SKIN_BIG_URL_TMPL.format(skin_id=eff_id))
        alias = (self.hero_aliases.get(safe_hid) or json_alias or "").strip()
        if alias and not alias.isdigit():
            _add_cand(DDRAGON_SKIN_SPLASH_URL_TMPL.format(alias=alias, skin_num=eff_skin_num))

        if self._download_skin_candidates(
            local,
            ("skin", str(eff_id)),
            candidate_urls,
            safe_hid=safe_hid,
            eff_id=eff_id,
        ):
            return local, skin_name

        if (orig_skin_num > 0 or eff_skin_num > 0) and hero_int > 0 and eff_id != hero_int * 1000:
            base_path, base_name = self.ensure_skin_splash(safe_hid, 0)
            if base_path is not None:
                return base_path, (skin_name or base_name)

        return None, skin_name

    def _ensure_skin_variant(self, hero_id: str, skin_id: int, *, variant: str) -> Path | None:
        safe_hid = "".join(ch for ch in str(hero_id or "").strip() if ch.isalnum() or ch in ("_", "-"))
        if not safe_hid or variant not in ("loading", "center"):
            return None
        hero_int = _parse_non_neg_int(safe_hid)
        full_skin_id, orig_skin_num = normalize_skin_id(safe_hid, skin_id)

        payload = self._load_hero_skins_payload(safe_hid, allow_network=True, full_skin_id=full_skin_id)
        eff_id, _skin_name, _main_img, loading_img, center_img, json_alias = self._extract_skin_art_urls(
            payload, safe_hid, skin_id
        )
        if eff_id <= 0:
            return None

        if hero_int > 0 and eff_id >= 1000 and eff_id // 1000 == hero_int:
            eff_skin_num = eff_id % 1000
        else:
            _, eff_skin_num = normalize_skin_id(safe_hid, eff_id)

        subdir = "skin_loading" if variant == "loading" else "skin_center"
        local = self.icons_dir / subdir / f"{eff_id}.jpg"
        if local.is_file() and local.stat().st_size > SKIN_SPLASH_MIN_BYTES:
            return local

        candidate_urls: list[str] = []

        def _add_cand(u: str) -> None:
            clean = (u or "").strip()
            if clean and clean not in candidate_urls:
                candidate_urls.append(clean)

        if variant == "loading":
            _add_cand(loading_img)
            _add_cand(TENCENT_SKIN_LOADING_URL_TMPL.format(skin_id=eff_id))
            alias = (self.hero_aliases.get(safe_hid) or json_alias or "").strip()
            if alias and not alias.isdigit():
                _add_cand(DDRAGON_SKIN_LOADING_URL_TMPL.format(alias=alias, skin_num=eff_skin_num))
        else:
            _add_cand(center_img)
            _add_cand(TENCENT_SKIN_CENTER_URL_TMPL.format(skin_id=eff_id))

        if self._download_skin_candidates(
            local,
            (subdir, str(eff_id)),
            candidate_urls,
            safe_hid=safe_hid,
            eff_id=eff_id,
        ):
            return local

        if (orig_skin_num > 0 or eff_skin_num > 0) and hero_int > 0 and eff_id != hero_int * 1000:
            return self._ensure_skin_variant(safe_hid, 0, variant=variant)
        return None

    def ensure_skin_loading(self, hero_id: str, skin_id: int = 0) -> Path | None:
        """Download and disk-cache official vertical loading portrait JPG (308x560)."""
        return self._ensure_skin_variant(hero_id, skin_id, variant="loading")

    def ensure_skin_center(self, hero_id: str, skin_id: int = 0) -> Path | None:
        """Download and disk-cache official centered hero splash JPG (1280x720)."""
        return self._ensure_skin_variant(hero_id, skin_id, variant="center")
