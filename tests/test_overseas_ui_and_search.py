"""Tests for v1.5.2 T2S pinyin search, LookupWindow 940x610 locale controls, and localized overlays."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from hex_helper.config import AppConfig
from hex_helper.data.locale_pack import CLIENT_LOCALES
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import ChampSelectState
from hex_helper.scoring.augment_combo import ActiveComboProgress, ComboFollowUp, ComboPlan, MatchBuildProfile
from hex_helper.scoring.matchup_optimizer import DynamicItemRec, NextBuyPlan
from hex_helper.setup import preflight
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.pinyin_match import matches_augment_query, to_pinyin_initials


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _build_demo_store(tmp_path: Path, locale: str = "zh_tw") -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda _u: b"{}")
    static.items["3089"] = ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment")
    static.items["3020"] = ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots")
    store = DataStore(static)
    store.set_client_locale(locale)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "103": HeroStat("103", "阿狸", "九尾妖狐", 50000, 26500, 0.53, 0.05, 8, False),
        "222": HeroStat("222", "金克丝", "暴走萝莉", 48000, 24000, 0.50, 0.06, 18, False),
    }
    store._augments = {
        "1048": AugmentInfo("1048", "珠光护手", "prismatic"),
        "1349": AugmentInfo("1349", "大招觉醒", "prismatic"),
    }
    store._core_builds = {"103": [CoreBuild(("3089",), True, 5000, 2650, 0.53, 0.4)]}
    store._hero_name_index = {
        "阿狸": "103",
        "阿璃": "103",
        "ahri": "103",
        "金克丝": "222",
        "吉茵珂絲": "222",
        "jinx": "222",
    }
    return store


def test_pinyin_match_traditional_t2s_and_bilingual_alt_name() -> None:
    assert to_pinyin_initials("拔劍吧") == "bjb"
    assert to_pinyin_initials("珠光護手") == "zghs"

    ok_py, score_py = matches_augment_query("拔劍吧", "稜鏡", "bjb")
    assert ok_py is True and score_py >= 330

    ok_sc, _ = matches_augment_query("拔劍吧", "稜鏡", "拔剑")
    assert ok_sc is True

    ok_rar, _ = matches_augment_query("其他符文", "稜鏡", "棱镜")
    assert ok_rar is True

    # Localized name + CN fallback alt_name lets Mainland players search TW display using CN name
    ok_alt, score_alt = matches_augment_query("大絕覺醒", "稜鏡", "大招", alt_name="大招觉醒")
    assert ok_alt is True and score_alt >= 400


def test_lookup_window_940x610_locale_control_no_overlap_and_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _qt_app()
    saved_cfgs: list[AppConfig] = []
    changed_events: list[dict] = []

    monkeypatch.setattr(
        preflight,
        "check_winrt_ocr",
        lambda tag="zh-Hans-CN": (tag.lower() == "zh-hans-cn", "mock"),
    )

    cfg = AppConfig(client_locale="zh_cn")
    lw = LookupWindow(
        cfg=cfg,
        on_save_settings=lambda c: saved_cfgs.append(c),
        on_settings_changed=lambda ch: changed_events.append(ch),
    )
    lw.resize(940, 610)
    lw._switch_page(3, force=True)
    lw.show()
    app.processEvents()

    assert [lw.client_locale_combo.itemData(i) for i in range(lw.client_locale_combo.count())] == list(CLIENT_LOCALES)
    assert lw.install_tw_ocr_btn.isVisible() is False

    # Switch to zh_tw -> shows "裝繁体包" fallback helper button alongside compact hint without overlapping
    tw_idx = lw.client_locale_combo.findData("zh_tw")
    lw.client_locale_combo.setCurrentIndex(tw_idx)
    app.processEvents()
    assert lw.install_tw_ocr_btn.isVisible() is True
    assert lw.locale_ocr_hint_lbl.text() == "免装简转繁"

    c_geo = lw.client_locale_combo.geometry()
    h_geo = lw.locale_ocr_hint_lbl.geometry()
    b_geo = lw.install_tw_ocr_btn.geometry()
    sr_geo = lw.screen_recording_chk.geometry()
    db_geo = lw.data_band_combo.geometry()
    assert not c_geo.intersects(h_geo)
    assert not c_geo.intersects(b_geo)
    assert not h_geo.intersects(b_geo)
    assert not c_geo.intersects(sr_geo)
    assert not c_geo.intersects(db_geo)
    assert lw.diag_text.height() >= 80
    assert "#FF5A1F" not in lw.install_tw_ocr_btn.styleSheet().upper()

    lw._save_clicked()
    assert saved_cfgs and saved_cfgs[-1].client_locale == "zh_tw"
    assert changed_events and changed_events[-1].get("client_locale") == "zh_tw"

    # Binding localized store updates hero list & special augments list in LookupWindow
    store = _build_demo_store(tmp_path, "zh_tw")
    lw.bind_store(store)
    lw._switch_page(0, force=True)
    lw._filter_heroes("阿璃")
    assert lw.hero_list.count() == 1
    assert "阿璃" in lw.hero_list.item(0).text()

    lw._filter_heroes("阿狸")
    assert lw.hero_list.count() == 1
    assert "阿璃" in lw.hero_list.item(0).text()

    lw.close()
    app.processEvents()

    # Commercial mode must expose client_locale selector cleanly without overlap at 940x610
    monkeypatch.setenv("HEX_EDITION", "commercial")
    lw_comm = LookupWindow(cfg=AppConfig(client_locale="zh_my"))
    lw_comm.resize(940, 610)
    lw_comm._switch_page(3, force=True)
    lw_comm.show()
    app.processEvents()
    assert lw_comm.client_locale_combo.currentData() == "zh_my"
    assert lw_comm.diag_text.height() >= 80
    assert not lw_comm.client_locale_combo.geometry().intersects(lw_comm.screen_recording_chk.geometry())
    assert lw_comm._collect_cfg_from_ui().client_locale == "zh_my"
    lw_comm.close()
    app.processEvents()


def test_localized_overlays_render_overseas_names_on_locale_switch(tmp_path: Path) -> None:
    app = _qt_app()
    store = _build_demo_store(tmp_path, "zh_tw")

    # 1. EdgeBuildDrawer
    drawer = EdgeBuildDrawer()
    drawer.set_expanded(True)
    drawer.populate_catalogs(store, [store.descriptor])
    drawer.show()
    app.processEvents()

    assert "大招觉醒" in (drawer._aug_alt_names.get("1349") or "")
    aid_res = drawer._resolve_augment_id_from_input(drawer.aug_add_combo, drawer._all_augment_entries)
    assert aid_res == ""
    drawer._rebuild_aug_combo_items("大绝")
    assert drawer.aug_add_combo.count() >= 1 and drawer.aug_add_combo.itemData(0) == "1349"

    rec = DynamicItemRec("3089", "灭世者的死亡之帽", 3600, True, 0, 9.5, 0.56, 4000, ("高法强",), ())
    plan = NextBuyPlan(
        rec,
        3600,
        True,
        0,
        "直合灭世者的死亡之帽",
        ("灭世者的死亡之帽",),
        ("3089",),
        recommended_boots_id="3020",
        recommended_boots_name="法师之靴",
        recommended_boots_reason="法穿",
    )
    drawer.refresh_match_view(store, "103", ["1349"], (), 4000.0, None, [rec], next_plan=plan)
    app.processEvents()

    assert "阿璃" in drawer.hero_lbl.text()
    assert "死亡之帽" in drawer.next_item_title_lbl.text()
    assert "法師之靴" in drawer.boots_hint_lbl.text()
    chip_txts = [lbl.text() for lbl in drawer.aug_chips_wrap.findChildren(QLabel)]
    assert any("大絕覺醒" in t for t in chip_txts)

    # 2. ChampSelectOverlay
    cs_ov = ChampSelectOverlay()
    cs_ov.update_state(ChampSelectState(is_mayhem=True, my_hero_id="103", bench_hero_ids=("222",)), store)
    app.processEvents()
    cs_labels = [lbl.text() for lbl in cs_ov.findChildren(QLabel)]
    assert "阿璃" in cs_labels
    assert "吉茵珂絲" in cs_labels

    # 3. AugmentComboOverlay live locale rebind & switch to zh_my
    combo_ov = AugmentComboOverlay(initial_size=[260, 420])
    profile = MatchBuildProfile("AP", "法术爆发", ("AP_SPELL",), (), (), ())
    c_plan = ComboPlan(
        hero_id="103",
        hero_name="阿狸",
        profile=profile,
        active_combos=(
            ActiveComboProgress("c1", "暴击法术", False, ("1349",), ("1048",), ("珠光护手",), False, "双修", "追珠光护手"),
        ),
        recommended_targets=(ComboFollowUp("1048", "珠光护手", "prismatic", "c1", "暴击法术", "技能暴击", True, 11.0, True),),
        opening_combos=(),
        avoid_summary="",
        round_index=2,
    )
    combo_ov.set_locale_store(store)
    combo_ov.set_expanded(True)
    combo_ov.set_plan(c_plan)
    combo_ov.show()
    app.processEvents()
    assert "阿璃" in combo_ov.active_combo_hdr.text()
    assert "寶石手套" in combo_ov.active_combo_body.text()
    target_labels = [lbl.text() for lbl in combo_ov.targets_list_wrap.findChildren(QLabel)]
    assert "寶石手套" in target_labels

    store.set_client_locale("zh_my")
    combo_ov.set_locale_store(store)
    cs_ov.update_state(ChampSelectState(is_mayhem=True, my_hero_id="103", bench_hero_ids=("222",)), store)
    app.processEvents()
    assert "宝石手套" in combo_ov.active_combo_body.text()
    assert "吉茵珂丝" in [lbl.text() for lbl in cs_ov.findChildren(QLabel)]

    drawer.close()
    cs_ov.close()
    combo_ov.close()
    app.processEvents()
