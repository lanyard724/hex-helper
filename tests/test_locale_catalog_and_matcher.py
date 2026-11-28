"""Tests for v1.5.2 embedded overseas locale catalog, DataStore isolation, and AugmentMatcher."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hex_helper.config import AppConfig
from hex_helper.data.locale_catalog_data import LOCALE_CATALOG_B64_ZLIB
from hex_helper.data.locale_pack import (
    CLIENT_LOCALES,
    build_ocr_catalog,
    get_locale_catalog,
    localize_augment_name,
    localize_hero_name,
    localize_item_name,
    normalize_client_locale,
)
from hex_helper.data.provider import AugmentInfo, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.vision.matcher import AugmentMatcher

LIVE_1623_NORMAL = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_1623_NORMAL",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal",
    )
)


def test_app_config_client_locale_default_normalization_and_roundtrip() -> None:
    assert AppConfig().client_locale == "zh_cn"
    assert AppConfig.from_dict({}).client_locale == "zh_cn"
    assert AppConfig.from_dict({"client_locale": "zh-TW"}).client_locale == "zh_tw"
    assert AppConfig.from_dict({"client_locale": "zh_SG"}).client_locale == "zh_my"
    assert AppConfig.from_dict({"client_locale": "unknown-XX"}).client_locale == "zh_cn"
    assert normalize_client_locale("tw") == "zh_tw"
    assert normalize_client_locale("MY") == "zh_my"
    assert normalize_client_locale("") == "zh_cn"
    assert CLIENT_LOCALES == ("zh_cn", "zh_my", "zh_tw")


def test_embedded_locale_catalog_size_and_completeness() -> None:
    # Fits well within ~45 KB budget so hot-patch PYZ delta stays small
    assert len(LOCALE_CATALOG_B64_ZLIB) <= 45_000

    for loc in ("zh_my", "zh_tw"):
        cat = get_locale_catalog(loc)
        assert len(cat["augments"]) >= 590
        assert len(cat["heroes"]) >= 173
        assert len(cat["items"]) >= 850
        # Spot-check high-variance localized names
        assert cat["augments"].get("1048")
        assert cat["augments"].get("1238")
        assert cat["items"].get("3089")

    assert localize_augment_name("1048", "珠光护手", "zh_tw") == "寶石手套"
    assert localize_augment_name("1238", "质变：棱彩阶", "zh_tw") == "質變：稜鏡"
    assert localize_hero_name("103", "阿狸", "zh_tw") == "阿璃"
    assert localize_hero_name("103", "阿狸", "zh_my") == "阿璃"
    assert localize_item_name("3089", "灭世者的死亡之帽", "zh_tw") == "死亡之帽"


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_embedded_catalog_covers_all_1623_active_augments_and_heroes(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda _u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )
    all_augs = store.all_augments()
    all_heroes = store.all_heroes()
    assert len(all_augs) == 237
    assert len(all_heroes) == 173

    for loc in ("zh_my", "zh_tw"):
        cat = get_locale_catalog(loc)
        missing_augs = [a.augment_id for a in all_augs if not cat["augments"].get(a.augment_id)]
        missing_heroes = [h.hero_id for h in all_heroes if not cat["heroes"].get(h.hero_id)]
        assert missing_augs == [], f"{loc} missing active augments: {missing_augs}"
        assert missing_heroes == [], f"{loc} missing active heroes: {missing_heroes}"


def test_zh_cn_isolation_and_datastore_canonical_preservation(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda _u: b"{}")
    static.items["3089"] = ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment")
    static.items["223089"] = ItemStaticInfo("223089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment")

    store = DataStore(static)
    store._heroes = {"103": HeroStat("103", "阿狸", "九尾妖狐", 50000, 26000, 0.52, 0.05, 10, False)}
    store._augments = {"1048": AugmentInfo("1048", "珠光护手", "prismatic", "技能暴击")}

    # zh_cn returns exact canonical CN names untouched
    store.set_client_locale("zh_cn")
    assert store.display_hero_name("103") == "阿狸"
    assert store.display_augment_name("1048") == "珠光护手"
    assert store.display_item_name("3089") == "灭世者的死亡之帽"
    cn_ocr = build_ocr_catalog(store.all_augments(), "zh_cn")
    assert [a.name for a in cn_ocr] == ["珠光护手"]

    # Switching to zh_tw updates display_* & build_ocr_augments ONLY, NEVER mutating canonical internals
    store.set_client_locale("zh_tw")
    assert store.get_hero("103").name == "阿狸"
    assert store.get_augment("1048").name == "珠光护手"
    assert store.get_item_info("3089").name == "灭世者的死亡之帽"

    assert store.display_hero_name("103") == "阿璃"
    assert store.display_augment_name("1048") == "寶石手套"
    assert store.display_item_name("3089") == "死亡之帽"
    assert store.display_item_name("223089") == "死亡之帽"

    tw_ocr = store.build_ocr_augments()
    assert tw_ocr[0].augment_id == "1048"
    assert tw_ocr[0].name == "寶石手套"
    assert store.get_augment("1048").name == "珠光护手"


def test_zh_my_duplicate_augment_name_rarity_disambiguation_and_short_title_guard() -> None:
    augs = [
        AugmentInfo("2076", "坚定不移", "silver"),
        AugmentInfo("3263", "坚定不移", "gold"),
        AugmentInfo("1007", "大力", "silver"),
        AugmentInfo("1323", "残忍", "prismatic"),
        AugmentInfo("1349", "大絕覺醒", "prismatic"),
    ]
    m_my = AugmentMatcher(build_ocr_catalog(augs[:4], "zh_my"), use_t2s=False)
    r_silver = m_my.match_line("坚定不移", rarity_hint="silver")
    r_gold = m_my.match_line("坚定不移", rarity_hint="gold")
    assert r_silver is not None and r_silver.augment.augment_id == "2076"
    assert r_gold is not None and r_gold.augment.augment_id == "3263"

    # 2-char short-title guard: noisy unrelated 2-char string must not false-match 2-char augment titles
    for use_t2s in (False, True):
        m_short = AugmentMatcher(augs, use_t2s=use_t2s)
        assert m_short.match_line("大方") is None
        exact = m_short.match_line("大 力")
        assert exact is not None and exact.augment.augment_id == "1007"
        shinjitai = m_short.match_line("大 絶 覺 醒")
        if use_t2s:
            assert shinjitai is not None and shinjitai.augment.augment_id == "1349"
        else:
            assert shinjitai is None or shinjitai.score < 0.85
