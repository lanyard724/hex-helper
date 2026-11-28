"""Offline multi-locale dictionary, Win32 T2S normalization, and visual cue bundles."""

from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass, replace
from functools import lru_cache
import json
import os
import re
from typing import Any, Sequence
import zlib

from hex_helper.data.provider import AugmentInfo

CLIENT_LOCALES: tuple[str, ...] = ("zh_cn", "zh_my", "zh_tw")
CLIENT_LOCALE_LABELS: dict[str, str] = {
    "zh_cn": "国服简体 (zh_CN)",
    "zh_my": "新加坡简体 (zh_MY)",
    "zh_tw": "台服繁体 (zh_TW)",
}

# Variant & Japanese Shinjitai overrides verified against WinRT zh-Hans-CN OCR outputs
_VARIANT_T2S: dict[str, str] = {
    "絕": "绝",
    "絶": "绝",
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
_VARIANT_T2S_TRANS = str.maketrans(_VARIANT_T2S)
_LCMAP_SIMPLIFIED_CHINESE = 0x02000000

_PAYLOAD_CACHE: dict[str, Any] | None = None
_FALLBACK_TRANS_CACHE: dict[int, str] | None = None


@dataclass(frozen=True)
class StatAnvilCues:
    title_cues: tuple[str, ...]
    shard_suffix: str


@dataclass(frozen=True)
class TransmuteCues:
    chat_strong_cues: tuple[str, ...]
    after_cue_re: re.Pattern[str]
    non_augment_tooltip_cues: tuple[str, ...]
    rarity_labels: tuple[str, ...]
    augment_nouns: tuple[str, ...]
    prismatic_cues: tuple[str, ...]
    gold_cues: tuple[str, ...]
    silver_cues: tuple[str, ...]


_STAT_ANVIL_CUES: dict[str, StatAnvilCues] = {
    "zh_cn": StatAnvilCues(title_cues=("属性锻造器",), shard_suffix="碎片"),
    "zh_my": StatAnvilCues(title_cues=("属性铁砧", "属性锻造器"), shard_suffix="碎片"),
    "zh_tw": StatAnvilCues(
        title_cues=("能力值鐵砧", "屬性鐵砧", "能力值铁砧", "属性铁砧", "属性锻造器"),
        shard_suffix="碎片",
    ),
}

_COMMON_NON_AUGMENT_TOOLTIP_CUES: tuple[str, ...] = (
    "点击以购买",
    "點擊以購買",
    "出售价格",
    "出售價格",
    "法力消耗",
    "魔力消耗",
    "消耗魔力",
    "生命值消耗",
    "生命消耗",
    "能量消耗",
    "合成路线",
    "合成路線",
    "唯一被动",
    "唯一被動",
)

_TRANSMUTE_CUES: dict[str, TransmuteCues] = {
    "zh_cn": TransmuteCues(
        chat_strong_cues=("获得", "质变", "变成", "转化", "强化符文", "随机获得"),
        after_cue_re=re.compile(r"(?:获得了?|质变为|质变|变成了?|转化为|强化符文[:：]?)\s*[:：\-—>]*\s*(.+)"),
        non_augment_tooltip_cues=_COMMON_NON_AUGMENT_TOOLTIP_CUES,
        rarity_labels=("棱彩阶", "黄金阶", "白银阶", "棱彩", "黄金", "白银", "强化符文"),
        augment_nouns=("强化符文",),
        prismatic_cues=("棱彩",),
        gold_cues=("黄金",),
        silver_cues=("白银",),
    ),
    "zh_my": TransmuteCues(
        chat_strong_cues=("获得", "质变", "变成", "变为", "转化", "增幅装置", "强化符文", "随机获得"),
        after_cue_re=re.compile(
            r"(?:获得了?|质变为?|变成了?|变为|转化为|增幅装置[:：]?|强化符文[:：]?)\s*[:：\-—>]*\s*(.+)"
        ),
        non_augment_tooltip_cues=_COMMON_NON_AUGMENT_TOOLTIP_CUES,
        rarity_labels=("棱彩", "金色", "银色", "棱彩阶", "黄金阶", "白银阶", "增幅装置", "强化符文"),
        augment_nouns=("增幅装置", "强化符文"),
        prismatic_cues=("棱彩",),
        gold_cues=("金色", "黄金"),
        silver_cues=("银色", "白银"),
    ),
    "zh_tw": TransmuteCues(
        chat_strong_cues=(
            "獲得", "獲得了", "質變", "變成", "轉化", "增幅裝置", "隨機獲得",
            "获得", "质变", "变成", "转化", "增幅装置", "随机获得",
        ),
        after_cue_re=re.compile(
            r"(?:獲得了?|获得了?|質變為?|质变为?|變成了?|变成了?|轉化為|转化为|增幅裝置[:：]?|增幅装置[:：]?|強化符文[:：]?|强化符文[:：]?)\s*[:：\-—>]*\s*(.+)"
        ),
        non_augment_tooltip_cues=_COMMON_NON_AUGMENT_TOOLTIP_CUES,
        rarity_labels=(
            "稜鏡", "金級", "銀級", "金色", "白銀", "黃金", "增幅裝置",
            "棱镜", "棱彩", "金级", "银级", "白银", "黄金", "增幅装置",
        ),
        augment_nouns=("增幅裝置", "增幅装置", "强化符文"),
        prismatic_cues=("稜鏡", "棱镜", "棱彩"),
        gold_cues=("金級", "金色", "黃金", "金级", "黄金"),
        silver_cues=("銀級", "白銀", "银级", "银色", "白银"),
    ),
}


def normalize_client_locale(raw: str | None) -> str:
    """Normalize client locale string to one of CLIENT_LOCALES ('zh_cn', 'zh_my', 'zh_tw')."""
    val = str(raw or "").strip().lower().replace("-", "_")
    if val in CLIENT_LOCALES:
        return val
    if val in ("zh_sg", "zh_my_sg", "sg", "my"):
        return "zh_my"
    if val in ("zh_hk", "zh_mo", "zh_hant", "zh_hant_tw", "tw"):
        return "zh_tw"
    return "zh_cn"


def desired_ocr_language_tag(locale: str) -> str:
    """Return desired WinRT OCR language tag for the selected LoL client locale."""
    norm = normalize_client_locale(locale)
    if norm == "zh_tw":
        return "zh-Hant-TW"
    return "zh-Hans-CN"


def _load_payload() -> dict[str, Any]:
    global _PAYLOAD_CACHE, _FALLBACK_TRANS_CACHE
    if _PAYLOAD_CACHE is not None:
        return _PAYLOAD_CACHE
    try:
        from hex_helper.data.locale_catalog_data import LOCALE_CATALOG_B64_ZLIB

        raw = zlib.decompress(base64.b64decode(LOCALE_CATALOG_B64_ZLIB.encode("ascii")))
        data = json.loads(raw.decode("utf-8"))
        if isinstance(data, dict):
            _PAYLOAD_CACHE = data
            fb = data.get("fallback_t2s") or {}
            if isinstance(fb, dict):
                merged = dict(fb)
                merged.update(_VARIANT_T2S)
                _FALLBACK_TRANS_CACHE = str.maketrans(merged)
            return _PAYLOAD_CACHE
    except Exception:
        pass
    _PAYLOAD_CACHE = {"schema": 1, "locales": {}}
    return _PAYLOAD_CACHE


@lru_cache(maxsize=4096)
def t2s_normalize(text: str) -> str:
    """Normalize Traditional Chinese & Japanese Shinjitai glyphs to Simplified Chinese without OpenCC."""
    raw = str(text or "")
    if not raw:
        return ""
    out = raw
    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            n = kernel32.LCMapStringEx(
                "zh-CN",
                _LCMAP_SIMPLIFIED_CHINESE,
                raw,
                len(raw),
                None,
                0,
                None,
                None,
                0,
            )
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                kernel32.LCMapStringEx(
                    "zh-CN",
                    _LCMAP_SIMPLIFIED_CHINESE,
                    raw,
                    len(raw),
                    buf,
                    n,
                    None,
                    None,
                    0,
                )
                if buf.value:
                    out = buf.value
        except Exception:
            out = raw
    if out == raw:
        _load_payload()
        if _FALLBACK_TRANS_CACHE is not None:
            out = out.translate(_FALLBACK_TRANS_CACHE)
    return out.translate(_VARIANT_T2S_TRANS)


def get_locale_catalog(locale: str) -> dict[str, dict[str, str]]:
    """Return locale dictionary {'augments': {...}, 'items': {...}, 'heroes': {...}} or empty buckets."""
    norm = normalize_client_locale(locale)
    if norm == "zh_cn":
        return {"augments": {}, "items": {}, "heroes": {}}
    payload = _load_payload()
    locales = payload.get("locales") or {}
    entry = locales.get(norm)
    if not isinstance(entry, dict):
        return {"augments": {}, "items": {}, "heroes": {}}
    return {
        "augments": dict(entry.get("augments") or {}),
        "items": dict(entry.get("items") or {}),
        "heroes": dict(entry.get("heroes") or {}),
    }


def _localize_from_bucket(bucket_name: str, key: str | int, fallback: str, locale: str) -> str:
    norm = normalize_client_locale(locale)
    if norm == "zh_cn":
        return fallback
    payload = _load_payload()
    locales = payload.get("locales") or {}
    entry = locales.get(norm)
    if not isinstance(entry, dict):
        return fallback
    bucket = entry.get(bucket_name)
    if not isinstance(bucket, dict):
        return fallback
    val = str(bucket.get(str(key), "") or "").strip()
    return val if val else fallback


def localize_augment_name(aug_id: str | int, fallback: str = "", locale: str = "zh_cn") -> str:
    return _localize_from_bucket("augments", aug_id, fallback, locale)


def localize_item_name(item_id: str | int, fallback: str = "", locale: str = "zh_cn") -> str:
    return _localize_from_bucket("items", item_id, fallback, locale)


def localize_hero_name(hero_id: str | int, fallback: str = "", locale: str = "zh_cn") -> str:
    return _localize_from_bucket("heroes", hero_id, fallback, locale)


def build_ocr_catalog(augments: Sequence[AugmentInfo], locale: str = "zh_cn") -> list[AugmentInfo]:
    """Return locale-isolated AugmentInfo copies for OCR matching (preserving canonical IDs/rarities)."""
    norm = normalize_client_locale(locale)
    if norm == "zh_cn":
        return list(augments)
    payload = _load_payload()
    locales = payload.get("locales") or {}
    entry = locales.get(norm)
    aug_map = entry.get("augments") if isinstance(entry, dict) else None
    if not isinstance(aug_map, dict) or not aug_map:
        return list(augments)

    out: list[AugmentInfo] = []
    for aug in augments:
        loc_name = str(aug_map.get(str(aug.augment_id), "") or "").strip()
        if loc_name and loc_name != aug.name:
            out.append(replace(aug, name=loc_name))
        else:
            out.append(aug)
    return out


def all_overseas_hero_aliases() -> dict[str, tuple[str, ...]]:
    """Return {hero_id: (zh_my_name, zh_tw_name, ...)} for secondary hero lookup indexing."""
    payload = _load_payload()
    locales = payload.get("locales") or {}
    merged: dict[str, list[str]] = {}
    for loc in ("zh_my", "zh_tw"):
        entry = locales.get(loc)
        heroes = entry.get("heroes") if isinstance(entry, dict) else None
        if not isinstance(heroes, dict):
            continue
        for hid, hname in heroes.items():
            nm = str(hname or "").strip()
            if not nm:
                continue
            bucket = merged.setdefault(str(hid), [])
            if nm not in bucket:
                bucket.append(nm)
    return {hid: tuple(names) for hid, names in merged.items()}


def get_stat_anvil_cues(locale: str = "zh_cn") -> StatAnvilCues:
    return _STAT_ANVIL_CUES.get(normalize_client_locale(locale), _STAT_ANVIL_CUES["zh_cn"])


def get_transmute_cues(locale: str = "zh_cn") -> TransmuteCues:
    return _TRANSMUTE_CUES.get(normalize_client_locale(locale), _TRANSMUTE_CUES["zh_cn"])
