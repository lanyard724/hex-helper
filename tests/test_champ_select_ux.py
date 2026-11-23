"""Tests for Task 11: Champ Select bench gap-filler highlights, ARAM hidden balance & Hex ceiling tags."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel

from hex_helper.data.aram_balance import get_aram_balance, get_hero_ceiling_tag
from hex_helper.data.provider import CoreBuild, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore, DatasetDescriptor
from hex_helper.game.models import ChampSelectState
from hex_helper.ui.champ_select_ui import ChampSelectOverlay, analyze_team_needs


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path)
    static.items = {
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0, "FlatArmorMod": 40.0}, "", "complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3400, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
    }
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "99": HeroStat("99", "拉克丝", "光辉女郎", 80000, 41600, 0.52, 0.08, 15, False),
        "103": HeroStat("103", "阿狸", "九尾妖狐", 60000, 31200, 0.52, 0.06, 18, False),
        "7": HeroStat("7", "乐芙兰", "诡术妖姬", 45000, 22500, 0.50, 0.04, 60, False),
        "147": HeroStat("147", "萨勒芬妮", "星籁歌姬", 55000, 29150, 0.53, 0.05, 10, False),
        "111": HeroStat("111", "诺提勒斯", "深海泰坦", 50000, 25500, 0.51, 0.05, 35, False),
        "236": HeroStat("236", "卢锡安", "圣枪游侠", 70000, 35700, 0.51, 0.07, 30, False),
    }
    store._core_builds = {
        "99": [CoreBuild(("6655",), True, 10000, 5200, 0.52, 0.4)],
        "103": [CoreBuild(("6655",), True, 10000, 5200, 0.52, 0.4)],
        "7": [CoreBuild(("6655",), True, 10000, 5000, 0.50, 0.4)],
        "147": [CoreBuild(("6655",), True, 10000, 5300, 0.53, 0.4)],
        "111": [CoreBuild(("3084",), True, 10000, 5100, 0.51, 0.4)],
        "236": [CoreBuild(("3031",), True, 10000, 5100, 0.51, 0.4)],
    }
    return store


def test_aram_balance_and_ceiling_tags() -> None:
    lux = get_aram_balance("99")
    assert lux is not None
    txt, sent = lux.format_badge()
    assert sent == "nerf"
    assert "伤-10%" in txt and "承+10%" in txt

    lb = get_aram_balance("7")
    assert lb is not None
    lb_txt, lb_sent = lb.format_badge()
    assert lb_sent == "buff"
    assert "伤+10%" in lb_txt and "承-10%" in lb_txt

    assert get_aram_balance("37") is None  # Sona default 1.0/1.0
    assert get_hero_ceiling_tag("236") == "大腕珠光核爆"
    assert get_hero_ceiling_tag("99") != ""


def test_champ_select_pins_my_hero_separates_bench_and_highlights_gap_fillers(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    store = _make_store(tmp_path)

    # Team is 4 AP (0 Tank, 0 AD) -> needs TANK and AD
    msg, is_warn, needed = analyze_team_needs(["99", "103", "7", "147"], store)
    assert is_warn is True
    assert "TANK" in needed and "AD" in needed
    assert "重AP" in msg

    state = ChampSelectState(
        queue_id=450,
        game_mode="ARAM",
        is_mayhem=True,
        my_hero_id="99",
        team_hero_ids=("99", "103", "7", "147"),
        bench_hero_ids=("111", "236"),
    )
    overlay = ChampSelectOverlay()
    overlay.update_state(state, store)
    app.processEvents()

    all_texts = [lbl.text() for lbl in overlay.findChildren(QLabel)]
    assert any("我的当前锁定" in t for t in all_texts)
    assert any("备战席" in t for t in all_texts)
    assert any("队友已选" in t for t in all_texts)
    assert any("[补前排]" in t for t in all_texts)
    assert any("[补AD]" in t for t in all_texts)
    assert any("伤-10%" in t for t in all_texts)  # Lux ARAM nerf badge
    assert any("大腕珠光核爆" in t for t in all_texts)  # Lucian ceiling tag
    assert overlay.height() >= 360
    overlay.close()


def test_champ_select_expands_cleanly_when_updated_while_already_visible(tmp_path: Path) -> None:
    """Regression: live LCU updates arriving AFTER show_overlay() must expand scroll area, never stay stuck at 60px."""
    app = QApplication.instance() or QApplication([])
    store = _make_store(tmp_path)

    overlay = ChampSelectOverlay()
    # Step 1: initial 1-hero state + show_overlay()
    overlay.update_state(
        ChampSelectState(2400, "KIWI", True, "99", ("99",), ()),
        store,
    )
    overlay.show_overlay()
    app.processEvents()
    initial_h = overlay.height()

    # Step 2: live update with 4 allies + 2 bench while ALREADY visible
    overlay.update_state(
        ChampSelectState(2400, "KIWI", True, "99", ("99", "103", "7", "147"), ("111", "236")),
        store,
    )
    app.processEvents()
    assert overlay.rows_scroll.height() >= 320
    assert overlay.height() > initial_h + 180
    overlay.close()
