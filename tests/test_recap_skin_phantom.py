"""Tests for Phase 1 Worker E: skin telemetry, Tencent splash/chroma cache, skin phantom backdrop, and 5-6 augments."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient
from PySide6.QtWidgets import QApplication

from hex_helper.data.static_assets import StaticAssetsManager, normalize_skin_id
from hex_helper.game.lcu_client import parse_champ_select_session
from hex_helper.game.live_client import parse_allgamedata
from hex_helper.ui.match_recap_card import (
    CARD_DEFAULT_HEIGHT,
    CARD_DEFAULT_WIDTH,
    MatchRecapData,
    render_match_recap_image,
)


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_rich_splash_jpg(path: Path, width: int = 1215, height: int = 717) -> Path:
    """Create a large multi-hue simulated splash artwork for phantom visual & pixel checks."""
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    bg = QLinearGradient(0.0, 0.0, float(width), float(height))
    bg.setColorAt(0.0, QColor("#1E1038"))
    bg.setColorAt(0.55, QColor("#5B21B6"))
    bg.setColorAt(1.0, QColor("#0F766E"))
    p.fillRect(img.rect(), bg)

    # Bright character silhouette / focal bloom in upper-center-right
    focal = QRadialGradient(width * 0.62, height * 0.30, width * 0.32)
    focal.setColorAt(0.0, QColor("#FDE68A"))
    focal.setColorAt(0.45, QColor("#A855F7"))
    focal.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(img.rect(), focal)

    # Vivid diagonal magenta/gold highlights
    p.setPen(QColor("#F472B6"))
    for offset in range(-120, 240, 36):
        p.drawLine(int(width * 0.40) + offset, 60, int(width * 0.78) + offset, int(height * 0.85))
    p.end()

    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "JPG", 92)
    return path


def _make_stub_icon(path: Path, color: str) -> str:
    img = QImage(48, 48, QImage.Format.Format_ARGB32)
    img.fill(QColor(color))
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "PNG")
    return str(path)


class _Http404Error(Exception):
    status_code = 404


def _sample_hero_82_payload() -> dict:
    return {
        "hero": {"heroId": "82", "alias": "Mordekaiser", "name": "铁铠冥魂", "title": "莫德凯撒"},
        "skins": [
            {
                "skinId": "82000",
                "name": "铁铠冥魂",
                "chromas": "0",
                "chromasBelongId": "0",
                "isBase": "1",
                "mainImg": "https://game.gtimg.cn/images/lol/act/img/skin/big_base_82.jpg",
            },
            {
                "skinId": "82006",
                "name": "暗星 莫德凯撒",
                "chromas": "0",
                "chromasBelongId": "0",
                "isBase": "0",
                "mainImg": "https://game.gtimg.cn/images/lol/act/img/skin/big_darkstar_82006.jpg",
            },
            {
                "skinId": "82007",
                "name": "暗星 莫德凯撒 天神星之陨",
                "chromas": "1",
                "chromasBelongId": "82006",
                "isBase": "0",
                "mainImg": "",
            },
        ],
    }


def test_normalize_skin_id_variants() -> None:
    assert normalize_skin_id("82", 6) == (82006, 6)
    assert normalize_skin_id("82", 82006) == (82006, 6)
    assert normalize_skin_id(82, "82006") == (82006, 6)
    assert normalize_skin_id("82", 0) == (82000, 0)
    assert normalize_skin_id("82", 82000) == (82000, 0)
    assert normalize_skin_id("1", 1005) == (1005, 5)
    assert normalize_skin_id("145", "1") == (145001, 1)
    assert normalize_skin_id("82", -5) == (82000, 0)
    assert normalize_skin_id("", 0) == (0, 0)


def test_lcu_and_live_client_parse_skin_fields() -> None:
    cs = parse_champ_select_session(
        {
            "localPlayerCellId": 2,
            "myTeam": [
                {"cellId": 1, "championId": 101, "selectedSkinId": 101003},
                {"cellId": 2, "championId": 82, "selectedSkinId": 82006, "spell1Id": 4, "spell2Id": 32},
            ],
        }
    )
    assert cs.my_hero_id == "82"
    assert cs.my_skin_id == 82006

    cs_no_skin = parse_champ_select_session(
        {"localPlayerCellId": 1, "myTeam": [{"cellId": 1, "championId": 82}]}
    )
    assert cs_no_skin.my_skin_id == 0

    live_payload = {
        "activePlayer": {"riotId": "Alan#CN1", "currentGold": 1400.0},
        "gameData": {"gameTime": 620.0},
        "allPlayers": [
            {
                "riotId": "Alan#CN1",
                "championName": "莫德凯撒",
                "rawChampionName": "game_character_displayname_Mordekaiser",
                "team": "ORDER",
                "isDead": False,
                "level": 14,
                "skinID": 6,
                "skinName": "暗星 莫德凯撒",
                "scores": {"kills": 9, "deaths": 3, "assists": 15},
                "items": [{"itemID": 3115}],
            },
            {
                "riotId": "Enemy#CN1",
                "championName": "金克丝",
                "rawChampionName": "game_character_displayname_Jinx",
                "team": "CHAOS",
                "isDead": False,
                "level": 14,
                "scores": {"kills": 5, "deaths": 6, "assists": 10},
                "items": [],
            },
        ],
    }
    snap = parse_allgamedata(live_payload)
    assert snap is not None
    assert snap.my_player is not None
    assert snap.my_player.skin_id == 6
    assert snap.my_player.skin_name == "暗星 莫德凯撒"
    assert snap.enemies[0].skin_id == 0
    assert snap.enemies[0].skin_name == ""


def test_resolve_skin_info_chroma_backtrack_and_cache(tmp_path: Path) -> None:
    payload_bytes = json.dumps(_sample_hero_82_payload(), ensure_ascii=False).encode("utf-8")
    calls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        if url == "https://game.gtimg.cn/images/lol/act/img/js/hero/82.js":
            return payload_bytes
        raise _Http404Error(f"404 Not Found: {url}")

    sm = StaticAssetsManager(tmp_path / "cache", fetcher=fake_fetch)

    eff_id, s_name, main_img = sm.resolve_skin_info("82", 82007)
    assert eff_id == 82006
    assert s_name == "暗星 莫德凯撒 天神星之陨"
    assert main_img == "https://game.gtimg.cn/images/lol/act/img/skin/big_darkstar_82006.jpg"
    assert (sm.static_dir / "hero_skins" / "82.json").is_file()
    assert len(calls) == 1

    # Second call resolves strictly from cached hero_skins/82.json (zero extra fetch)
    eff_id2, s_name2, main_img2 = sm.resolve_skin_info("82", 6)
    assert eff_id2 == 82006
    assert s_name2 == "暗星 莫德凯撒"
    assert main_img2 == "https://game.gtimg.cn/images/lol/act/img/skin/big_darkstar_82006.jpg"
    assert len(calls) == 1

    # Zero-network read-only cache lookup before jpg exists
    cached_path, cached_name = sm.resolve_cached_skin_splash("82", 82007)
    assert cached_path == ""
    assert cached_name == "暗星 莫德凯撒 天神星之陨"


def test_ensure_skin_splash_fallbacks_negative_cache_and_zero_network_lookup(tmp_path: Path) -> None:
    payload_bytes = json.dumps(_sample_hero_82_payload(), ensure_ascii=False).encode("utf-8")
    valid_jpg = b"\xff\xd8\xff\xe0" + (b"S" * 512)
    base_jpg = b"\xff\xd8\xff\xe0" + (b"B" * 512)

    # Case 1: mainImg fails (>256B guard), big82006.jpg fails (404), DDragon Mordekaiser_6.jpg succeeds
    calls_1: list[str] = []

    def fetch_ddragon_fallback(url: str) -> bytes:
        calls_1.append(url)
        if url.endswith("/js/hero/82.js"):
            return payload_bytes
        if url == "https://game.gtimg.cn/images/lol/act/img/skin/big_darkstar_82006.jpg":
            return b"tiny"  # <= 256 bytes rejected
        if url == "https://game.gtimg.cn/images/lol/act/img/skin/big82006.jpg":
            raise _Http404Error("404 Not Found")
        if url == "https://ddragon.leagueoflegends.com/cdn/img/champion/splash/Mordekaiser_6.jpg":
            return valid_jpg
        raise _Http404Error("404 Not Found")

    sm1 = StaticAssetsManager(tmp_path / "c1", fetcher=fetch_ddragon_fallback)
    p1, name1 = sm1.ensure_skin_splash("82", 7)  # chroma 82007 -> backtracks to 82006 (skin_num=6)
    assert p1 is not None and p1 == sm1.icons_dir / "skin" / "82006.jpg"
    assert p1.read_bytes() == valid_jpg
    assert name1 == "暗星 莫德凯撒 天神星之陨"
    assert calls_1 == [
        "https://game.gtimg.cn/images/lol/act/img/js/hero/82.js",
        "https://game.gtimg.cn/images/lol/act/img/skin/big_darkstar_82006.jpg",
        "https://game.gtimg.cn/images/lol/act/img/skin/big82006.jpg",
        "https://ddragon.leagueoflegends.com/cdn/img/champion/splash/Mordekaiser_6.jpg",
    ]

    # Hot-path zero-network read-only check must return cached path without touching network
    sm1._fetcher = lambda _u: (_ for _ in ()).throw(AssertionError("network call forbidden"))  # type: ignore[assignment]
    hot_path, hot_name = sm1.resolve_cached_skin_splash("82", 82007)
    assert hot_path == str(p1)
    assert hot_name == "暗星 莫德凯撒 天神星之陨"

    # Case 2: All skin_num > 0 splash candidates return 404 -> falls back to base skin 82000 & negative-caches 82006
    calls_2: list[str] = []

    def fetch_base_fallback(url: str) -> bytes:
        calls_2.append(url)
        if url.endswith("/js/hero/82.js"):
            return payload_bytes
        if url == "https://game.gtimg.cn/images/lol/act/img/skin/big_base_82.jpg":
            return base_jpg
        raise _Http404Error("404 Not Found")

    sm2 = StaticAssetsManager(tmp_path / "c2", fetcher=fetch_base_fallback)
    p2, name2 = sm2.ensure_skin_splash("82", 82006)
    assert p2 is not None and p2 == sm2.icons_dir / "skin" / "82000.jpg"
    assert p2.read_bytes() == base_jpg
    assert name2 == "暗星 莫德凯撒"
    assert ("skin", "82006") in sm2._icon_404_until

    # Re-calling 82006 uses negative cache for 82006 and hits local cached 82000 without network calls!
    calls_2.clear()
    p2_again, _ = sm2.ensure_skin_splash("82", 82006)
    assert p2_again == p2
    assert calls_2 == []


def test_render_recap_skin_phantom_5_6_augments_and_contrast(tmp_path: Path) -> None:
    _qt_app()
    splash_path = _make_rich_splash_jpg(tmp_path / "82006.jpg")
    hero_ico = _make_stub_icon(tmp_path / "hero_82.png", "#0AC8B9")
    aug_icons = tuple(_make_stub_icon(tmp_path / f"aug_{i}.png", "#C8AA6E") for i in range(6))
    item_entries = tuple(
        (nm, _make_stub_icon(tmp_path / f"item_{i}.png", "#3B82F6"))
        for i, nm in enumerate(
            ("纳什之牙", "峡谷制造者", "中娅沙漏", "灭世者的死亡之帽", "虚空之杖", "水银之靴")
        )
    )

    augs_5 = (
        ("珠光护手", "prismatic"),
        ("尤里卡", "prismatic"),
        ("灵魂虹吸", "gold"),
        ("科学狂人", "gold"),
        ("灵巧", "silver"),
    )
    augs_6 = augs_5 + (("升级献祭", "gold"),)

    base_kw = dict(
        hero_id="82",
        hero_name="莫德凯撒",
        hero_icon_path=hero_ico,
        won=1,
        kda="18/4/21",
        duration_s=1084,
        team_kills="51 : 39",
        is_first_prismatic=True,
        augments=augs_5,
        augment_icon_paths=aug_icons[:5],
        combo_title="暗星法爆战神 · 五海克斯成型",
        combo_completed=True,
        item_entries=item_entries,
        final_stats={
            "ad": 182.0,
            "ap": 845.0,
            "hp": 5240.0,
            "ah": 156.0,
            "armor": 215.0,
            "mr": 168.0,
            "as": 1.65,
            "ms": 445.0,
        },
        damage_dealt=74200,
        damage_share=0.36,
        damage_taken=61200,
        heal_and_shield=24100,
        gold_earned=20500,
        kill_participation=0.76,
        match_score=14.6,
        rating_grade="S+",
        mvp_badge="MVP",
        skin_id=82006,
        skin_name="暗星 莫德凯撒",
    )

    img_no_phantom = render_match_recap_image(MatchRecapData(**base_kw, skin_splash_path=""))
    img_win_phantom = render_match_recap_image(MatchRecapData(**base_kw, skin_splash_path=str(splash_path)))
    img_loss_phantom = render_match_recap_image(
        MatchRecapData(**{**base_kw, "won": 0, "mvp_badge": "SVP"}, skin_splash_path=str(splash_path))
    )
    img_6_augs = render_match_recap_image(
        MatchRecapData(**{**base_kw, "augments": augs_6, "augment_icon_paths": aug_icons}, skin_splash_path=str(splash_path))
    )

    for img in (img_win_phantom, img_loss_phantom, img_6_augs):
        assert not img.isNull()
        assert img.width() == CARD_DEFAULT_WIDTH
        assert img.height() == CARD_DEFAULT_HEIGHT

    # 1. Left portrait dark frame margin (x=14..22, y=60..115) must stay dark navy to protect portrait & title contrast
    left_luma_sum = 0.0
    left_samples = 0
    for y in range(60, 115, 3):
        for x in range(14, 22, 2):
            c = img_win_phantom.pixelColor(x, y)
            left_luma_sum += 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            left_samples += 1
    assert (left_luma_sum / max(1, left_samples)) < 38.0

    # 2. Mid-right open stage (e.g. y=208..214 and gaps between item tiles y=232..280) must reflect phantom tint
    mid_right_diffs = 0
    for y in range(132, 305, 3):
        for x in range(380, 725, 5):
            if img_win_phantom.pixelColor(x, y) != img_no_phantom.pixelColor(x, y):
                mid_right_diffs += 1
    assert mid_right_diffs > 400

    # 3. 5-augment vs 6-augment renders cleanly and differs in augment row
    aug_row_diffs = 0
    for y in range(134, 206, 2):
        for x in range(24, 736, 4):
            if img_6_augs.pixelColor(x, y) != img_win_phantom.pixelColor(x, y):
                aug_row_diffs += 1
    assert aug_row_diffs > 200

    # 4. Suppress SKIN // badge when skin_name is "default" or identical to hero_name
    img_default_skin = render_match_recap_image(
        MatchRecapData(**{**base_kw, "skin_name": "default"}, skin_splash_path="")
    )
    img_same_hero_skin = render_match_recap_image(
        MatchRecapData(**{**base_kw, "skin_name": "莫德凯撒"}, skin_splash_path="")
    )
    assert img_default_skin.bits().tobytes() == img_same_hero_skin.bits().tobytes()
    assert img_default_skin.bits().tobytes() != img_no_phantom.bits().tobytes()

    # 5. Strictly zero Web Tactile Orange (#FF5A1F) in pixels
    for img in (img_win_phantom, img_loss_phantom, img_6_augs):
        for y in range(0, img.height(), 4):
            for x in range(0, img.width(), 4):
                c = img.pixelColor(x, y)
                assert not (abs(c.red() - 255) <= 12 and abs(c.green() - 90) <= 12 and abs(c.blue() - 31) <= 12)


def test_ensure_skin_loading_and_center_and_zero_network_variants(tmp_path: Path) -> None:
    payload = _sample_hero_82_payload()
    payload["skins"][0]["loadingImg"] = "https://game.gtimg.cn/images/lol/act/img/skinloading/82000.jpg"
    payload["skins"][0]["centerImg"] = "https://game.gtimg.cn/images/lol/act/img/center/82000.jpg"
    payload["skins"][1]["loadingImg"] = "https://game.gtimg.cn/images/lol/act/img/skinloading/darkstar_82006.jpg"
    payload["skins"][1]["centerImg"] = "https://game.gtimg.cn/images/lol/act/img/center/darkstar_82006.jpg"
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    load_jpg = b"\xff\xd8\xff\xe0" + (b"L" * 512)
    cent_jpg = b"\xff\xd8\xff\xe0" + (b"C" * 512)
    base_load_jpg = b"\xff\xd8\xff\xe0" + (b"B" * 512)

    calls: list[str] = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        if url.endswith("/js/hero/82.js"):
            return payload_bytes
        if url == "https://game.gtimg.cn/images/lol/act/img/skinloading/darkstar_82006.jpg":
            raise _Http404Error("404 Not Found")
        if url == "https://game.gtimg.cn/images/lol/act/img/skinloading/82006.jpg":
            raise _Http404Error("404 Not Found")
        if url == "https://ddragon.leagueoflegends.com/cdn/img/champion/loading/Mordekaiser_6.jpg":
            return load_jpg
        if url == "https://game.gtimg.cn/images/lol/act/img/center/darkstar_82006.jpg":
            return cent_jpg
        raise _Http404Error("404 Not Found")

    sm = StaticAssetsManager(tmp_path / "c_variants", fetcher=fake_fetch)
    p_load = sm.ensure_skin_loading("82", 7)  # chroma 82007 -> backtracks to 82006 -> DDragon loading fallback
    assert p_load is not None and p_load == sm.icons_dir / "skin_loading" / "82006.jpg"
    assert p_load.read_bytes() == load_jpg

    p_cent = sm.ensure_skin_center("82", 82007)
    assert p_cent is not None and p_cent == sm.icons_dir / "skin_center" / "82006.jpg"
    assert p_cent.read_bytes() == cent_jpg

    # Hot-path zero-network read-only lookup
    sm._fetcher = lambda _u: (_ for _ in ()).throw(AssertionError("network call forbidden"))  # type: ignore[assignment]
    hot_load, hot_cent = sm.resolve_cached_skin_variants("82", 82007)
    assert hot_load == str(p_load)
    assert hot_cent == str(p_cent)

    # Base-skin fallback when skin_num > 0 loading fails completely
    def fetch_base_only(url: str) -> bytes:
        if url.endswith("/js/hero/82.js"):
            return payload_bytes
        if url == "https://game.gtimg.cn/images/lol/act/img/skinloading/82000.jpg":
            return base_load_jpg
        raise _Http404Error("404 Not Found")

    sm2 = StaticAssetsManager(tmp_path / "c_variants_base", fetcher=fetch_base_only)
    p_base_load = sm2.ensure_skin_loading("82", 6)
    assert p_base_load is not None and p_base_load == sm2.icons_dir / "skin_loading" / "82000.jpg"
    assert p_base_load.read_bytes() == base_load_jpg
