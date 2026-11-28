"""Tests for pinyin augment search, on-demand keyboard focus, dark QMenu + screen recording toggle, and 5-6 slot unlock."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QInputMethodEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QMenu, QWidget
import pytest

from hex_helper.data.provider import AugmentInfo, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import (
    ActiveComboProgress,
    ComboPlan,
    MatchBuildProfile,
    PickerCardComboAdvice,
    PickerComboVerdict,
)
from hex_helper.scoring.matchup_optimizer import DynamicItemRec, NextBuyPlan
from hex_helper.ui import augment_combo_overlay as combo_mod
from hex_helper.ui import edge_drawer as drawer_mod
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.pinyin_match import matches_augment_query, to_pinyin_initials
from hex_helper.ui.theme import HEXTECH_MENU_QSS
from hex_helper.vision.transmute import PendingTransmute


LIVE_1623_AUGMENTS_PATH = Path(r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal\augments.json")


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _build_test_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path, fetcher=lambda _u: b"{}")
    static.items["3089"] = ItemStaticInfo(
        item_id="3089",
        name="灭世者的死亡之帽",
        gold=3600,
        category="complete_equipment",
    )
    static.items["3157"] = ItemStaticInfo(
        item_id="3157",
        name="中娅沙漏",
        gold=3250,
        category="complete_equipment",
    )
    store = DataStore(static)
    store._heroes["222"] = HeroStat("222", "金克丝", "Jinx", 50000, 26000, 0.52, 0.08, 12)
    augments = (
        ("9001", "巫妖大脑", "prismatic"),
        ("9002", "超强大脑", "gold"),
        ("9003", "泰坦的坚决", "gold"),
        ("9004", "回力OK镖", "gold"),
        ("9005", "质变：混沌", "prismatic"),
        ("9006", "升级：中娅", "silver"),
        ("9007", "灵巧", "silver"),
        ("9008", "珠光手镯", "gold"),
        ("9009", "夜狩", "gold"),
        ("9010", "鲨鱼诱饵", "prismatic"),
    )
    for aid, nm, rar in augments:
        store._augments[aid] = AugmentInfo(aid, nm, rar)
    return store


def test_pinyin_match_initials_full_1623_dataset_and_patch_table() -> None:
    assert to_pinyin_initials("巫妖大脑") == "wydn"
    assert to_pinyin_initials("回力OK镖") == "hlokb"
    assert to_pinyin_initials("质变：混沌") == "zbhd"
    assert to_pinyin_initials("升级：中娅") == "sjzy"
    assert to_pinyin_initials("珠光手镯") == "zgsz"
    assert to_pinyin_initials("鲨鱼诱饵") == "syye"
    assert to_pinyin_initials("夜狩") == "ys"
    assert to_pinyin_initials("软弹啪叽抓") == "rdpjz"
    assert to_pinyin_initials("深红钴蓝闪光") == "shglsg"
    assert to_pinyin_initials("巫妖铠娅婕璐烬璨曜霆魄魅焰噬翎刃蛰穹渊罡飓淬弈") == "wykyjljcytpmyslrzqygjcy"

    if not LIVE_1623_AUGMENTS_PATH.is_file():
        pytest.skip("16.23/normal/augments.json dataset not present locally")

    raw = json.loads(LIVE_1623_AUGMENTS_PATH.read_text(encoding="utf-8"))
    assert len(raw) == 237
    for aid, item in raw.items():
        name = str(item["name"])
        expected_len = sum(
            1 for ch in name if ("\u4e00" <= ch <= "\u9fff") or (ch.isascii() and ch.isalnum())
        )
        initials = to_pinyin_initials(name)
        assert len(initials) == expected_len, f"{aid}:{name} -> {initials!r}"
        assert initials.isascii() and initials.isalnum() and initials == initials.lower()


def test_matches_augment_query_priority_ranking() -> None:
    ok_cn_pfx, sc_cn_pfx = matches_augment_query("巫妖大脑", "棱彩", "巫妖")
    ok_cn_sub, sc_cn_sub = matches_augment_query("巫妖大脑", "棱彩", "大脑")
    ok_py_pfx, sc_py_pfx = matches_augment_query("巫妖大脑", "棱彩", "wy")
    ok_py_exact, sc_py_exact = matches_augment_query("巫妖大脑", "棱彩", "wydn")
    ok_py_sub, sc_py_sub = matches_augment_query("巫妖大脑", "棱彩", "dn")
    ok_rar_short, sc_rar_short = matches_augment_query("巫妖大脑", "棱彩", "彩")
    ok_rar_full, sc_rar_full = matches_augment_query("巫妖大脑", "棱彩", "棱彩")

    assert all((ok_cn_pfx, ok_cn_sub, ok_py_pfx, ok_py_exact, ok_py_sub, ok_rar_short, ok_rar_full))
    assert sc_cn_pfx > sc_cn_sub > sc_py_exact >= sc_py_pfx > sc_py_sub > sc_rar_full >= sc_rar_short > 0

    # Gold/silver rarity keywords
    assert matches_augment_query("灵巧", "白银", "银")[0] is True
    assert matches_augment_query("灵巧", "白银", "白银")[0] is True
    assert matches_augment_query("泰坦的坚决", "黄金", "金")[0] is True
    assert matches_augment_query("泰坦的坚决", "黄金", "黄金")[0] is True
    assert matches_augment_query("巫妖大脑", "棱彩", "白银") == (False, 0)
    assert matches_augment_query("巫妖大脑", "棱彩", "zzzz") == (False, 0)
    assert matches_augment_query("巫妖大脑", "棱彩", "巫x") == (False, 0)
    assert matches_augment_query("巫妖大脑", "棱彩", "  ") == (True, 0)


def test_edge_drawer_dark_qmenu_and_screen_recording_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _qt_app()
    show_calls: list[tuple[bool, bool | None]] = []
    excl_calls: list[bool] = []
    captured_menus: list[QMenu] = []
    toggled_cb: list[bool] = []

    monkeypatch.setattr(
        drawer_mod,
        "show_no_activate",
        lambda widget, click_through=False, *, exclude_from_capture=None: (
            show_calls.append((bool(click_through), exclude_from_capture)),
            widget.show(),
        ),
    )
    monkeypatch.setattr(
        drawer_mod,
        "apply_capture_exclusion",
        lambda _widget, exclude=True: excl_calls.append(bool(exclude)) or True,
    )
    class _FakeMenu(QMenu):
        def exec(self, *_args, **_kwargs):
            captured_menus.append(self)
            return None

    monkeypatch.setattr(drawer_mod, "QMenu", _FakeMenu)

    drawer = EdgeBuildDrawer(on_toggle_screen_recording=toggled_cb.append)
    assert drawer._allow_screen_recording is False
    drawer.show_overlay()
    app.processEvents()
    assert show_calls[-1] == (False, True)

    evt = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(16, 16),
        QPointF(16, 16),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    drawer.mousePressEvent(evt)
    assert len(captured_menus) == 1
    menu = captured_menus[0]
    assert menu.styleSheet() == HEXTECH_MENU_QSS
    assert "#FF5A1F" not in menu.styleSheet().upper()

    rec_actions = [a for a in menu.actions() if a.text() == "录屏/截屏中显示悬浮窗"]
    assert len(rec_actions) == 1
    rec_act = rec_actions[0]
    assert rec_act.isCheckable() is True
    assert rec_act.isChecked() is False

    # Triggering menu action updates flag, invokes callback, and disables capture exclusion
    rec_act.trigger()
    assert toggled_cb == [True]
    assert drawer._allow_screen_recording is True
    assert excl_calls[-1] is False

    drawer.set_expanded(True)
    assert show_calls[-1] == (False, False)

    # External sync back to False updates exclusion without re-emitting user callback
    drawer.set_screen_recording_allowed(False)
    assert drawer._allow_screen_recording is False
    assert excl_calls[-1] is True
    assert toggled_cb == [True]

    captured_menus.clear()
    drawer.close()
    drawer.deleteLater()
    app.processEvents()


def test_edge_drawer_on_demand_keyboard_focus_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _qt_app()
    store = _build_test_store(tmp_path)
    focus_calls: list[bool] = []
    restore_calls: list[bool] = []

    monkeypatch.setattr(
        drawer_mod,
        "set_allow_keyboard_focus",
        lambda _widget, allow: focus_calls.append(bool(allow)) or True,
    )

    drawer = EdgeBuildDrawer(on_request_restore_game_focus=lambda: restore_calls.append(True))
    drawer.populate_catalogs(store, [])
    drawer.set_expanded(True)
    drawer.show()
    app.processEvents()

    assert drawer._text_input_active is False

    # 1. Expanding manual augment row enters text input mode and allows real keystrokes
    drawer._toggle_manual_aug_picker()
    app.processEvents()
    assert drawer._text_input_active is True
    assert focus_calls[-1] is True
    aug_le = drawer.aug_add_combo.lineEdit()
    assert aug_le is not None
    QTest.keyClicks(aug_le, "wydn")
    assert aug_le.text() == "wydn"

    # 2. WindowDeactivate while a QComboBox popup is active must NOT drop input mode
    fake_popup = QWidget()
    monkeypatch.setattr(QApplication, "activePopupWidget", staticmethod(lambda: fake_popup))
    QApplication.sendEvent(drawer, QEvent(QEvent.Type.WindowDeactivate))
    assert drawer._text_input_active is True

    # 3. WindowDeactivate when activePopupWidget() is None exits input mode and restores game focus
    monkeypatch.setattr(QApplication, "activePopupWidget", staticmethod(lambda: None))
    QApplication.sendEvent(drawer, QEvent(QEvent.Type.WindowDeactivate))
    assert drawer._text_input_active is False
    assert focus_calls[-1] is False
    assert len(restore_calls) == 1

    # 4. Left-clicking lineEdit re-enters text input mode; Escape cancels & restores game focus
    click_evt = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(8, 8),
        QPointF(8, 8),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(aug_le, click_evt)
    assert drawer._text_input_active is True
    assert focus_calls[-1] is True

    QTest.keyClick(aug_le, Qt.Key.Key_Escape)
    app.processEvents()
    assert drawer.aug_manual_row.isHidden() is True
    assert drawer._text_input_active is False
    assert focus_calls[-1] is False
    assert len(restore_calls) == 2

    # 5. Expanding item_manual_row enters text input mode; collapsing restores game focus
    drawer.set_degraded_2999_mode(True)
    drawer._toggle_manual_item_picker()
    assert drawer._text_input_active is True
    assert focus_calls[-1] is True
    drawer._toggle_manual_item_picker()
    assert drawer._text_input_active is False
    assert focus_calls[-1] is False
    assert len(restore_calls) == 3

    # 6. Showing transmute_bar enters text input mode; dismissing restores game focus
    pending = PendingTransmute("1238", "质变：棱彩阶", 1, "prismatic", 10.0)
    drawer.set_pending_transmute(pending, store)
    assert drawer._text_input_active is True
    assert focus_calls[-1] is True
    drawer._handle_dismiss_transmute()
    assert drawer._text_input_active is False
    assert focus_calls[-1] is False
    assert len(restore_calls) == 4

    drawer.close()
    drawer.deleteLater()
    app.processEvents()


def test_edge_drawer_pinyin_search_recent_pinning_and_transmute_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _qt_app()
    store = _build_test_store(tmp_path)
    picked_augs: list[str] = []
    resolved_tx: list[str] = []
    restore_calls: list[bool] = []

    monkeypatch.setattr(drawer_mod, "set_allow_keyboard_focus", lambda _w, _a: True)

    drawer = EdgeBuildDrawer(
        on_select_augment=picked_augs.append,
        on_resolve_transmute=resolved_tx.append,
        on_request_restore_game_focus=lambda: restore_calls.append(True),
    )
    drawer.populate_catalogs(store, [])
    drawer.set_expanded(True)
    drawer.show()
    app.processEvents()

    # Sorted by rarity (棱彩 first, then 黄金, then 白银) + pinyin initials
    assert drawer._all_augment_entries[0][2] == "棱彩"
    assert drawer._all_augment_entries[-1][2] == "白银"

    # Pin 2 recent unrecorded augments at top
    drawer.set_recent_unrecorded_augments(["9008", "9001"])
    assert drawer.aug_add_combo.itemData(0) == "9008"
    assert drawer.aug_add_combo.itemText(0) == "★ 近期出现 · 珠光手镯 (黄金)"
    assert drawer.aug_add_combo.itemData(1) == "9001"
    assert drawer.aug_add_combo.itemText(1) == "★ 近期出现 · 巫妖大脑 (棱彩)"

    # Recording 9008 unpins it from the top while keeping 9001 pinned
    drawer.refresh_match_view(store, "222", ["9008"], (), 1200.0, None, [])
    assert drawer.aug_add_combo.itemData(0) == "9001"
    assert drawer.aug_add_combo.itemText(0) == "★ 近期出现 · 巫妖大脑 (棱彩)"
    assert not drawer.aug_add_combo.itemText(1).startswith("★ 近期出现")

    # Open picker, type "wydn" -> live filters combo without clearing typed text, Return commits 9001
    drawer._toggle_manual_aug_picker()
    aug_le = drawer.aug_add_combo.lineEdit()
    assert aug_le is not None
    QTest.keyClicks(aug_le, "wydn")
    assert aug_le.text() == "wydn"
    assert drawer.aug_add_combo.count() == 1
    assert drawer.aug_add_combo.itemData(0) == "9001"

    QTest.keyClick(aug_le, Qt.Key.Key_Return)
    app.processEvents()
    assert picked_augs == ["9001"]
    assert drawer.aug_manual_row.isHidden() is True
    assert drawer._text_input_active is False
    assert len(restore_calls) == 1

    # Also test Chinese IME substring + Return fallback when lineEdit text is edited
    drawer._toggle_manual_aug_picker()
    QApplication.sendEvent(aug_le, QInputMethodEvent("", []))
    aug_le.setText("大脑")
    aug_le.textEdited.emit("大脑")
    assert aug_le.text() == "大脑"
    assert drawer.aug_add_combo.count() == 2
    assert set(drawer.aug_add_combo.itemData(i) for i in range(drawer.aug_add_combo.count())) == {"9001", "9002"}
    QTest.keyClick(aug_le, Qt.Key.Key_Return)
    app.processEvents()
    assert len(picked_augs) == 2 and picked_augs[-1] in ("9001", "9002")

    # Transmute combo fuzzy / pinyin fallback resolution
    pending = PendingTransmute("9005", "质变：混沌", 1, "prismatic", 10.0)
    drawer.set_pending_transmute(pending, store)
    tx_le = drawer.transmute_combo.lineEdit()
    assert tx_le is not None
    QTest.keyClicks(tx_le, "syy")
    assert tx_le.text() == "syy"
    assert drawer.transmute_combo.count() == 1
    assert drawer.transmute_combo.itemData(0) == "9010"
    QTest.keyClick(tx_le, Qt.Key.Key_Return)
    app.processEvents()
    assert resolved_tx == ["9010"]

    drawer.close()
    drawer.deleteLater()
    app.processEvents()


def test_edge_drawer_unlocks_5_and_6_augment_slots(tmp_path: Path) -> None:
    app = _qt_app()
    store = _build_test_store(tmp_path)
    rec = DynamicItemRec("3089", "灭世者的死亡之帽", 3600, False, 1000, 10.0, 0.54, 1000, ("核心",), ())
    plan = NextBuyPlan(rec, 3600, False, 1000, "先买无用大棒", ("灭世者的死亡之帽",), ("3089",))

    drawer = EdgeBuildDrawer()
    drawer.set_expanded(True)
    drawer.show()
    app.processEvents()

    assert len(drawer._pill_aug_dots) == 6
    offered = [store.get_augment("9008"), store.get_augment("9009"), store.get_augment("9010")]
    assert all(a is not None for a in offered)
    offered_list = [a for a in offered if a is not None]

    # 2 augments -> first 4 dots shown, 5th/6th hidden, quick-confirm visible
    drawer.refresh_match_view(store, "222", ["9001", "9002"], (), 2000.0, None, [rec], next_plan=plan)
    drawer.set_recent_offered(offered_list)
    app.processEvents()
    assert all(not drawer._pill_aug_dots[i].isHidden() for i in range(4))
    assert drawer._pill_aug_dots[4].isHidden() and drawer._pill_aug_dots[5].isHidden()
    assert drawer.quick_confirm_wrap.isHidden() is False
    h_2aug = drawer.height()

    # 4 augments -> quick-confirm STILL visible because cap unlocked to 6; 5th/6th dots still hidden
    drawer.refresh_match_view(store, "222", ["9001", "9002", "9003", "9004"], (), 2000.0, None, [rec], next_plan=plan)
    drawer.set_recent_offered(offered_list)
    app.processEvents()
    assert drawer.aug_hdr_lbl.text() == "◈ 海克斯 (4/4)"
    assert drawer._pill_aug_dots[4].isHidden() and drawer._pill_aug_dots[5].isHidden()
    assert drawer.quick_confirm_wrap.isHidden() is False

    # 5 augments -> 5th dot shown, 6th hidden, quick-confirm STILL visible, 5 chips laid out cleanly
    drawer.refresh_match_view(
        store, "222", ["9001", "9002", "9003", "9004", "9005"], (), 2000.0, None, [rec], next_plan=plan
    )
    drawer.set_recent_offered(offered_list)
    app.processEvents()
    assert drawer.aug_hdr_lbl.text() == "◈ 海克斯 (5/5)"
    assert drawer.pill_prog_lbl.text() == "5/5"
    assert drawer._pill_aug_dots[4].isHidden() is False
    assert drawer._pill_aug_dots[5].isHidden() is True
    assert drawer.quick_confirm_wrap.isHidden() is False
    chips_5 = [
        w
        for w in drawer.aug_chips_wrap.findChildren(QFrame, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if type(w) is QFrame
    ]
    assert len(chips_5) == 5
    for i in range(len(chips_5)):
        assert chips_5[i].geometry().right() <= drawer.aug_chips_wrap.width()
        for j in range(i + 1, len(chips_5)):
            assert not chips_5[i].geometry().intersects(chips_5[j].geometry())
    assert drawer.height() > h_2aug

    # 6 augments -> all 6 dots shown, 6 chips laid out cleanly, quick-confirm hides at >= 6
    drawer.refresh_match_view(
        store,
        "222",
        ["9001", "9002", "9003", "9004", "9005", "9006"],
        (),
        2000.0,
        None,
        [rec],
        next_plan=plan,
    )
    drawer.set_recent_offered(offered_list)
    app.processEvents()
    assert drawer.aug_hdr_lbl.text() == "◈ 海克斯 (6/6)"
    assert drawer.pill_prog_lbl.text() == "6/6"
    assert all(not drawer._pill_aug_dots[i].isHidden() for i in range(6))
    assert drawer.quick_confirm_wrap.isHidden() is True
    chips_6 = [
        w
        for w in drawer.aug_chips_wrap.findChildren(QFrame, options=Qt.FindChildOption.FindDirectChildrenOnly)
        if type(w) is QFrame
    ]
    assert len(chips_6) == 6
    for i in range(len(chips_6)):
        assert chips_6[i].geometry().right() <= drawer.aug_chips_wrap.width()
        for j in range(i + 1, len(chips_6)):
            assert not chips_6[i].geometry().intersects(chips_6[j].geometry())
    assert drawer.panel_scroll.verticalScrollBar().maximum() == 0

    drawer.close()
    drawer.deleteLater()
    app.processEvents()


def test_augment_combo_overlay_dark_menu_recording_and_5_6_slot_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _qt_app()
    show_calls: list[tuple[bool, bool | None]] = []
    excl_calls: list[bool] = []
    captured_menus: list[QMenu] = []
    toggled_cb: list[bool] = []

    monkeypatch.setattr(
        combo_mod,
        "show_no_activate",
        lambda widget, click_through=False, *, exclude_from_capture=None: (
            show_calls.append((bool(click_through), exclude_from_capture)),
            widget.show(),
        ),
    )
    monkeypatch.setattr(
        combo_mod,
        "apply_capture_exclusion",
        lambda _widget, exclude=True: excl_calls.append(bool(exclude)) or True,
    )

    class _FakeMenu(QMenu):
        def exec(self, *_args, **_kwargs):
            captured_menus.append(self)
            return None

    monkeypatch.setattr(combo_mod, "QMenu", _FakeMenu)

    ov = AugmentComboOverlay(on_toggle_screen_recording=toggled_cb.append)
    assert ov._allow_screen_recording is False
    ov.show_overlay()
    app.processEvents()
    assert show_calls[-1] == (False, True)

    # Dark QMenu + checkable screen recording toggle
    evt = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(16, 16),
        QPointF(16, 16),
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
        Qt.KeyboardModifier.NoModifier,
    )
    ov.mousePressEvent(evt)
    assert len(captured_menus) == 1
    menu = captured_menus[0]
    assert menu.styleSheet() == HEXTECH_MENU_QSS
    assert "#FF5A1F" not in menu.styleSheet().upper()
    rec_act = [a for a in menu.actions() if a.text() == "录屏/截屏中显示悬浮窗"][0]
    assert rec_act.isCheckable() is True
    assert rec_act.isChecked() is False
    rec_act.trigger()
    assert toggled_cb == [True]
    assert ov._allow_screen_recording is True
    assert excl_calls[-1] is False

    # Dynamic 4/5/6 count wording (zero hardcoded 4/4 or 四枚)
    profile = MatchBuildProfile("AD", "攻速暴击", ("AUTO_ONHIT",), (), (), ("已选双刀流",))
    plan_empty_targets = ComboPlan(
        hero_id="222",
        hero_name="金克丝",
        profile=profile,
        active_combos=(),
        recommended_targets=(),
        opening_combos=(),
        avoid_summary="",
        round_index=4,
    )
    ov.set_expanded(True)
    for cnt in (4, 5, 6):
        ov.set_plan(plan_empty_targets, selected_count=cnt)
        app.processEvents()
        assert ov.active_combo_hdr.text() == f"◈ 金克丝 · 已持有 {cnt} 枚海克斯"
        assert ov.active_combo_body.text() == f"当前 {cnt} 枚海克斯已定型，结合流派与敌方威胁专注出装"
        lbls = [lbl.text() for lbl in ov.targets_list_wrap.findChildren(QLabel)]
        assert lbls == [f"✓ 已持有 {cnt} 枚海克斯 · 专注出装"]
        assert "4/4" not in ov.active_combo_body.text()
        assert "四枚" not in ov.active_combo_body.text()

    # Also test active_combos present with 5 augments
    plan_with_active = ComboPlan(
        hero_id="222",
        hero_name="金克丝",
        profile=profile,
        active_combos=(
            ActiveComboProgress("c1", "暴击联动", False, ("9001",), ("9008",), ("珠光手镯",), False, "暴击", ""),
        ),
        recommended_targets=(),
        opening_combos=(),
        avoid_summary="",
        round_index=4,
    )
    ov.set_plan(plan_with_active, selected_count=5)
    app.processEvents()
    assert [lbl.text() for lbl in ov.targets_list_wrap.findChildren(QLabel)] == ["✓ 已持有 5 枚海克斯 · 专注出装"]
    assert "追珠光手镯" not in ov.active_combo_body.text()

    # Extra 5th/6th round picker is NOT blocked by >=4 selected augments
    verdict = PickerComboVerdict(
        profile=profile,
        cards=(
            PickerCardComboAdvice(0, "9007", "灵巧", "silver", "synergy", "可补", 2.0, "补攻速", "暴击联动", False),
            PickerCardComboAdvice(1, "9008", "珠光手镯", "gold", "jackpot", "绝配", 6.5, "技能暴击", "暴击联动", False),
            PickerCardComboAdvice(2, "9009", "夜狩", "gold", "neutral", "一般", 0.5, "隐身", "", False),
        ),
        best_slot_index=1,
        reroll_advice="",
        round_index=4,
    )
    ov.enter_picker_mode(verdict)
    app.processEvents()
    assert ov.mode == "picker"
    assert ov.is_expanded is True
    assert len(ov._picker_rows) == 3
    assert show_calls[-1] == (False, False)

    ov.exit_picker_mode()
    app.processEvents()
    assert ov.mode == "plan"

    captured_menus.clear()
    ov.close()
    ov.deleteLater()
    app.processEvents()
