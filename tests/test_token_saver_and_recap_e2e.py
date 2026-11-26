"""Phase 2 End-to-End Integration Tests:
1. Smart Death-Trigger Gating (50s cooldown, significant enemy items, >=1100G gold / falling behind bypass).
2. Opening 10-Player Match Brief Card in EdgeBuildDrawer + Match-Friendly Balance Pill + Hextech discipline.
3. End-Of-Game Match Highlight Recap Card wiring (MatchRecapData, LookupWindow hook, non-blocking MatchRecapDialog, tray alert).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtCore import QAbstractNativeEventFilter, QRect
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from hex_helper import app as app_mod
from hex_helper import edition as edition_mod
from hex_helper.app import (
    GameState,
    HexHelperController,
    HexStateMachine,
    is_significant_enemy_item,
)
from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload
from hex_helper.data.provider import DatasetDescriptor, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.match_recap_card import MatchRecapData, MatchRecapDialog
from hex_helper.vision.capture import CaptureResult, WindowRect


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _NoopCapturer:
    backend = "noop"
    consecutive_black_frames = 0

    def grab(self, **_kw):
        return CaptureResult(frame_bgr=None, backend="noop", is_black=True, mean_luma=0.0)

    def close(self) -> None:
        return None


class _NoopHotkeys(QAbstractNativeEventFilter):
    def nativeEventFilter(self, _eventType, _message):
        return False, 0

    def register(self, *_a, **_kw) -> None:
        return None

    def unregister_all(self) -> None:
        return None


def _patch_headless_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)
    monkeypatch.setattr(app_mod, "find_game_window", lambda: WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080))
    monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: True)


def _write_png_file(path: Path, color: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(48, 48, QImage.Format.Format_ARGB32)
    img.fill(QColor(color))
    assert img.save(str(path), "PNG")
    assert path.stat().st_size > 64
    return path


def _build_test_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", proxy_url="")
    static.hero_aliases["103"] = "Ahri"
    test_items = {
        # Cheap base components & consumables
        "1036": ItemStaticInfo("1036", "长剑", 350, ("Damage",), {"FlatPhysicalDamageMod": 10.0}, "", "other"),
        "1028": ItemStaticInfo("1028", "红水晶", 400, ("Health",), {"FlatHPPoolMod": 150.0}, "", "other"),
        "1052": ItemStaticInfo("1052", "增幅典籍", 435, ("SpellDamage",), {"FlatMagicDamageMod": 20.0}, "", "other"),
        "1001": ItemStaticInfo("1001", "草鞋", 300, ("Boots",), {}, "", "boots"),
        "1055": ItemStaticInfo("1055", "多兰之刃", 450, ("Damage",), {}, "", "other"),
        "2052": ItemStaticInfo("2052", "魄罗佳肴", 0, (), {}, "", "consumable"),
        # Tactical counters (<2200G)
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "3111": ItemStaticInfo("3111", "水银之靴", 1100, ("Boots",), {}, "", "boots"),
        "3123": ItemStaticInfo("3123", "死刑宣告", 800, ("Damage",), {}, "重伤", "other", has_grievous=True),
        "3916": ItemStaticInfo("3916", "遗忘宝珠", 800, ("SpellDamage",), {}, "重伤", "other", has_grievous=True),
        "3076": ItemStaticInfo("3076", "棘刺背心", 800, ("Armor",), {}, "重伤", "other", has_grievous=True),
        "3140": ItemStaticInfo("3140", "水银饰带", 1300, ("SpellBlock",), {}, "", "other"),
        "3191": ItemStaticInfo("3191", "探索者的护臂", 1600, ("SpellDamage", "Armor"), {}, "", "other"),
        "2420": ItemStaticInfo("2420", "秒表", 750, (), {}, "", "other"),
        # Complete items (>=2200G)
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105.0}, "", "complete_equipment"),
        "3165": ItemStaticInfo("3165", "莫雷洛秘典", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 75.0}, "重伤", "complete_equipment", has_grievous=True),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage",), {"FlatMagicDamageMod": 95.0}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Armor", "Health"), {"FlatArmorMod": 75.0}, "重伤", "complete_equipment", has_grievous=True),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0}, "", "complete_equipment"),
    }

    ds_dir = tmp_path / "datasets" / "16.23" / "normal"
    ds_dir.mkdir(parents=True, exist_ok=True)
    heroes = {
        "103": {
            "name": "阿狸",
            "title": "九尾妖狐",
            "games": 40000,
            "wins": 21200,
            "win_rate": 0.53,
            "pick_rate": 0.06,
            "rank": 12,
            "low_sample": False,
        }
    }
    augments = {
        "1048": {"name": "珠光护手", "rarity": "prismatic"},
        "1030": {"name": "尤里卡", "rarity": "prismatic"},
        "1045": {"name": "炼狱导管", "rarity": "prismatic"},
        "1092": {"name": "易损", "rarity": "gold"},
        "1001": {"name": "普通银卡", "rarity": "silver"},
    }
    hero_augs = {
        "103": {
            aid: {"games": 1000, "wins": 560, "win_rate": 0.56, "low_sample": False}
            for aid in augments
        }
    }
    (ds_dir / "heroes.json").write_text(json.dumps(heroes, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "augments.json").write_text(json.dumps(augments, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "hero_augments.json").write_text(json.dumps(hero_augs, ensure_ascii=False), encoding="utf-8")
    (ds_dir / "hero_spells.json").write_text("{}", encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({"103": [{"items": ["6655", "3089", "3157"], "ordered": True, "games": 5000, "wins": 2750, "win_rate": 0.55, "pick_rate": 0.35}]}),
        encoding="utf-8",
    )
    (ds_dir / "hero_augment_items.json").write_text("{}", encoding="utf-8")
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps(
            {
                "103": {
                    "6655": {"games": 8000, "wins": 4400, "win_rate": 0.55, "low_sample": False},
                    "3089": {"games": 6000, "wins": 3420, "win_rate": 0.57, "low_sample": False},
                    "3157": {"games": 5000, "wins": 2700, "win_rate": 0.54, "low_sample": False},
                    "3165": {"games": 4000, "wins": 2240, "win_rate": 0.56, "low_sample": False},
                }
            }
        ),
        encoding="utf-8",
    )
    (ds_dir / "item_metadata.json").write_text(
        json.dumps(
            {
                "6655": {"category": "complete_equipment"},
                "3089": {"category": "complete_equipment"},
                "3157": {"category": "complete_equipment"},
                "3165": {"category": "complete_equipment"},
                "3135": {"category": "complete_equipment"},
                "3075": {"category": "complete_equipment"},
                "3084": {"category": "complete_equipment"},
            }
        ),
        encoding="utf-8",
    )

    store = DataStore(static)
    desc = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "2026-11-23T00:00:00Z", False)
    store.load_from_dir(desc, ds_dir)
    # Restore rich static item catalog after load_from_dir (which calls load_cached without ddragon_items.json)
    static.items = dict(test_items)
    return store


def _create_controller(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HexHelperController, MagicMock]:
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "app_data"))
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "https://api.test")
    _patch_headless_controller(monkeypatch)

    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    ctrl._vision_timer.stop()
    ctrl._heartbeat_timer.stop()
    if ctrl._wallet_heartbeat_timer is not None:
        ctrl._wallet_heartbeat_timer.stop()
    if ctrl._startup_update_timer is not None:
        ctrl._startup_update_timer.stop()

    store = _build_test_store(tmp_path)
    ctrl.store = store
    ctrl.matcher.update_catalog(store.all_augments())

    submit_spy = MagicMock()
    assert ctrl.cloud_client is not None
    ctrl.cloud_client._session = {"access_token": "tok-e2e", "username": "AlanPhase2"}
    ctrl.cloud_client._last_wallet = {
        "username": "AlanPhase2",
        "balance_credits": 4850.0,
        "ai_enabled": True,
        "ai_cutoff": False,
    }
    ctrl._cloud_ai_cutoff = False
    ctrl._wallet_ai_cutoff = False
    ctrl._service_locked = False
    ctrl._force_update_required = False
    ctrl.cloud_coach.submit = submit_spy
    ctrl._refresh_coach_online_ui()
    return ctrl, submit_spy


# ---------------------------------------------------------------------------
# 1. Smart Death-Trigger Gating (50s cooldown, significant items, gold/behind)
# ---------------------------------------------------------------------------


def test_significant_enemy_item_filter_with_and_without_store(tmp_path: Path) -> None:
    store = _build_test_store(tmp_path)

    # Cheap base components & Poro-Snax MUST NOT count as significant enemy items
    for cheap_id in ("1036", "1028", "1052", "1001", "1055", "2052"):
        assert is_significant_enemy_item(cheap_id, store) is False
        assert is_significant_enemy_item(cheap_id, None) is False

    # Complete items (>=2200G) and key tactical counters MUST count as significant
    for sig_id in ("3075", "3084", "3089", "3020", "3111", "3123", "3916", "3076", "3140", "3191", "2420"):
        assert is_significant_enemy_item(sig_id, store) is True
        assert is_significant_enemy_item(sig_id, None) is True


def test_smart_death_gating_skips_cheap_components_and_enforces_50s_cooldown(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert HexStateMachine.DEATH_COACH_COOLDOWN_S == 50.0

    fake_now = [100.0]
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: fake_now[0])

    ctrl, submit_spy = _create_controller(qapp, tmp_path, monkeypatch)
    try:
        # Start match alive at level 8 with 2 augments already selected (avoids milestone deferral)
        me_alive = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 8, 2, 1, 3, ("6655",))
        e1_start = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 8, 2, 1, 1, ("1055",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(180.0, me_alive, (me_alive,), (e1_start,), 500.0))
        ctrl.sm.selected_augments = ["1048", "1030"]
        assert submit_spy.call_count == 1  # game_start

        # Death #1: Enemy only bought 350G Long Sword + 400G Ruby Crystal + Poro-Snax, player gold=650 < 1100, KDA 2/2/3 -> SKIP!
        fake_now[0] = 170.0
        me_dead1 = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", True, 8, 2, 2, 3, ("6655",))
        e1_cheap = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 8, 3, 1, 1, ("1055", "1036", "1028", "2052"))
        with caplog.at_level(logging.INFO, logger="hex_helper.app"):
            ctrl._apply_live_snapshot(LiveMatchSnapshot(250.0, me_dead1, (me_dead1,), (e1_cheap,), 650.0))
        assert submit_spy.call_count == 1
        assert any("Skipping death_enemy_build_changed" in r.message for r in caplog.records)

        # Respawn alive
        fake_now[0] = 185.0
        ctrl._apply_live_snapshot(LiveMatchSnapshot(265.0, me_alive, (me_alive,), (e1_cheap,), 700.0))

        # Death #2: Enemy still only has cheap components, BUT player has >= 1100G shopping gold (1250G) -> TRIGGER!
        fake_now[0] = 200.0
        me_dead2 = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", True, 8, 3, 3, 4, ("6655",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(280.0, me_dead2, (me_dead2,), (e1_cheap,), 1250.0))
        assert submit_spy.call_count == 2
        assert ctrl._latest_request_trigger == "death_enemy_build_changed"

        # Respawn alive
        fake_now[0] = 215.0
        ctrl._apply_live_snapshot(LiveMatchSnapshot(295.0, me_alive, (me_alive,), (e1_cheap,), 200.0))

        # Death #3: Only 30s after Death #2 (< 50s cooldown!) even though enemy completed 3075 Thornmail -> SKIPPED!
        fake_now[0] = 230.0
        me_dead3 = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", True, 8, 3, 4, 4, ("6655",))
        e1_complete = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 9, 5, 1, 2, ("3075",))
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="hex_helper.app"):
            ctrl._apply_live_snapshot(LiveMatchSnapshot(310.0, me_dead3, (me_dead3,), (e1_complete,), 1800.0))
        assert submit_spy.call_count == 2
        assert any("cooldown" in r.message for r in caplog.records)

        # Respawn alive
        fake_now[0] = 245.0
        ctrl._apply_live_snapshot(LiveMatchSnapshot(325.0, me_alive, (me_alive,), (e1_complete,), 300.0))

        # Death #4: 55s after Death #2 (>= 50s cooldown), player gold=400 < 1100, enemy completed >=2200G item -> TRIGGER!
        fake_now[0] = 255.0
        me_dead4 = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", True, 8, 3, 5, 4, ("6655",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(335.0, me_dead4, (me_dead4,), (e1_complete,), 400.0))
        assert submit_spy.call_count == 3

        # Respawn alive, then Death #5 55s later: no new enemy item, gold=300 < 1100, BUT falling behind (1/6/2) -> TRIGGER!
        fake_now[0] = 290.0
        ctrl._apply_live_snapshot(LiveMatchSnapshot(370.0, me_alive, (me_alive,), (e1_complete,), 300.0))
        fake_now[0] = 310.0
        me_behind = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", True, 8, 1, 6, 2, ("6655",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(390.0, me_behind, (me_behind,), (e1_complete,), 300.0))
        assert submit_spy.call_count == 4
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 2. Opening 10-Player Match Brief Card & Match-Friendly Drawer Balance Pill
# ---------------------------------------------------------------------------


def test_opening_brief_card_render_auto_expand_dismiss_and_balance_pill(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prev_ed = edition_mod._COMPILED_EDITION
    try:
        edition_mod._COMPILED_EDITION = "commercial"
        ctrl, _submit_spy = _create_controller(qapp, tmp_path, monkeypatch)
        try:
            drawer = ctrl.edge_drawer
            drawer.show()
            drawer.set_expanded(False)
            qapp.processEvents()

            # Commercial balance pill leads with "约剩 X 局" and updates on tier switch
            assert drawer.coach_balance_lbl.text() == "约剩 15 局"
            assert "4,850 积分" in drawer.coach_balance_lbl.toolTip()
            drawer.set_coach_tier("a")
            assert drawer.coach_balance_lbl.text() == "约剩 22 局"
            drawer.set_coach_tier("b")
            assert drawer.coach_balance_lbl.text() == "约剩 4 局"
            drawer.set_coach_tier("auto")
            assert drawer.coach_balance_lbl.text() == "约剩 15 局"

            # Enter game & receive game_start CoachAdvicePayload with 3-row opening_brief
            me = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 3, 0, 0, 0, ("3802",))
            e1 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 3, 0, 0, 0, ("1055",))
            ctrl._apply_live_snapshot(LiveMatchSnapshot(18.0, me, (me,), (e1,), 1400.0))
            drawer.set_expanded(False)
            qapp.processEvents()

            opening_adv = CoachAdvicePayload(
                request_id=ctrl._latest_request_id or "hex-open-1",
                next_items=[CoachAdviceItem("6655", "卢登的伙伴", "下件必出", "先手爆发清线")],
                buy_now="直购失落章节 (1200G)",
                build_path=["卢登的伙伴", "灭世者的死亡之帽", "中娅沙漏"],
                tactical_summary="开局拉扯消耗，中期补重伤压制亚托克斯回复",
                opening_brief={
                    "enemy_threat": "亚托克斯高回复 + 强开团，切忌交 E 盲进",
                    "augment_goal": "优先追珠光护手 / 尤里卡技能暴击流",
                    "opening_buy": "失落章节 + 鞋子出门，二件看回血补遗忘宝珠",
                },
            )
            ctrl._on_coach_advice(opening_adv)
            qapp.processEvents()

            # Opening brief card is visible, populated, and auto-expanded the drawer!
            assert drawer._expanded is True
            assert drawer.opening_brief_card.isHidden() is False
            assert drawer.opening_brief_card.isVisible() is True
            assert "◈ 开局 10 人作战简报" in drawer.opening_brief_title_lbl.text()
            assert "亚托克斯" in drawer.opening_brief_threat_lbl.text()
            assert "珠光护手" in drawer.opening_brief_augment_lbl.text()
            assert "失落章节" in drawer.opening_brief_buy_lbl.text()

            # Settle slide animation completely before capturing visual audit snapshot
            if drawer._slide_anim.state() == drawer._slide_anim.State.Running:
                drawer._slide_anim.stop()
            drawer._on_slide_finished()
            drawer._reposition_right_edge()
            qapp.processEvents()
            assert drawer.width() == EdgeBuildDrawer.EXPANDED_W

            # Save headless visual audit snapshot to verify crisp LOL V16.23 Hextech styling
            audit_dir = Path(r"C:\Users\Administrator\.gemini\antigravity\brain\c6d79029-fe63-4195-8605-30bd8143d2a5\audit_visual")
            if audit_dir.parent.exists():
                audit_dir.mkdir(parents=True, exist_ok=True)
                drawer.grab().save(str(audit_dir / "edge_drawer_opening_brief.png"), "PNG")

            # Resizable overlay still works cleanly while opening_brief_card is visible
            drawer._on_live_resize(QRect(drawer.x(), drawer.y(), 310, 520), "bl")
            qapp.processEvents()
            assert drawer.width() == 310
            assert drawer.opening_brief_card.isVisible() is True
            drawer.reset_default_size()
            qapp.processEvents()

            # Clicking × dismiss button hides opening_brief_card and suppresses re-show for this match
            drawer.opening_brief_dismiss_btn.click()
            qapp.processEvents()
            assert drawer._opening_brief_dismissed is True
            assert drawer.opening_brief_card.isHidden() is True
            drawer.set_expanded(False)
            ctrl._on_coach_advice(opening_adv)
            qapp.processEvents()
            assert drawer.opening_brief_card.isHidden() is True
            assert drawer._expanded is False

            # Resetting state (new match) allows opening_brief_card again, and empty opening_brief auto-hides it
            drawer.reset_opening_brief_state()
            ctrl._on_coach_advice(opening_adv)
            qapp.processEvents()
            assert drawer.opening_brief_card.isHidden() is False

            later_adv = CoachAdvicePayload(
                request_id=ctrl._latest_request_id or "hex-mid-1",
                next_items=[CoachAdviceItem("3165", "莫雷洛秘典", "下件必出", "敌方回血成型补重伤")],
                tactical_summary="敌方双回血已成型，直接锁莫雷洛秘典压制团战回复！",
                opening_brief={},
            )
            ctrl._on_coach_advice(later_adv)
            qapp.processEvents()
            assert drawer.opening_brief_card.isHidden() is True

            # Respects _overlays_hidden_by_user=True (must NOT auto-expand!)
            drawer.reset_opening_brief_state()
            drawer.set_expanded(False)
            ctrl._overlays_hidden_by_user = True
            ctrl._on_coach_advice(opening_adv)
            qapp.processEvents()
            assert drawer._expanded is False

            # Strict LOL V16.23 Hextech palette check: zero Web orange #FF5A1F
            all_qss = drawer.styleSheet() + " ".join(w.styleSheet() for w in drawer.findChildren(QWidget))
            assert "#FF5A1F" not in all_qss.upper()
        finally:
            ctrl.shutdown()
            qapp.processEvents()
    finally:
        edition_mod._COMPILED_EDITION = prev_ed


# ---------------------------------------------------------------------------
# 3. End-Of-Game Match Highlight Recap Card Wiring
# ---------------------------------------------------------------------------


def test_end_of_game_builds_recap_unlocks_lookup_opens_dialog_and_notifies_tray(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctrl, _submit_spy = _create_controller(qapp, tmp_path, monkeypatch)
    try:
        # Prepare local cached champion portrait & 2 item icons (others gracefully stay "")
        hero_png = _write_png_file(ctrl.store.static.icons_dir / "champion" / "103.png", "#0AC8B9")
        item1_png = _write_png_file(ctrl.store.static.icons_dir / "item" / "6655.png", "#C8AA6E")
        item2_png = _write_png_file(ctrl.store.static.icons_dir / "item" / "3089.png", "#D946EF")

        tray_messages: list[tuple[str, str]] = []
        monkeypatch.setattr(
            ctrl.tray,
            "showMessage",
            lambda title, msg, *_a, **_kw: tray_messages.append((title, msg)),
        )

        # Recap button starts disabled before any finished match
        assert ctrl.lookup_window.recap_btn.isEnabled() is False
        assert ctrl._last_recap_data is None

        # Short match (<180s) MUST NOT generate recap or unlock button
        me_early = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 4, 1, 0, 2, ("6655",))
        e1 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 4, 0, 1, 0, ("1055",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(120.0, me_early, (me_early,), (e1,), 800.0))
        ctrl._apply_lcu_poll("EndOfGame", None)
        assert ctrl._last_recap_data is None
        assert ctrl.lookup_window.recap_btn.isEnabled() is False
        assert tray_messages == []

        # Full match (920s) with 4 augments forming a real completed combo, AI tactical summary, and Victory!
        me_start = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 3, 0, 0, 0, ("3802",))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(30.0, me_start, (me_start,), (e1,), 1400.0))

        ctrl.sm.selected_augments = ["1048", "1030", "1045", "1092"]
        ctrl._latest_request_id = "hex-recap-hl"
        ctrl._on_coach_advice(
            CoachAdvicePayload(
                request_id="hex-recap-hl",
                next_items=[CoachAdviceItem("3165", "莫雷洛秘典", "下件必出", "针对敌方双回血提前补重伤")],
                tactical_summary="珠光护手+尤里卡双棱彩成型，提前补莫雷洛秘典压死对面亚托克斯团战续航！",
            )
        )

        me_end = PlayerLiveInfo(
            "me",
            "阿狸",
            "Ahri",
            "ORDER",
            False,
            18,
            14,
            5,
            22,
            ("6655", "3089", "3157", "3165", "3020", "3135", "2052"),
        )
        snap_end = LiveMatchSnapshot(
            game_time=920.0,
            my_player=me_end,
            allies=(me_end,),
            enemies=(e1,),
            current_gold=1850.0,
            my_team_kills=52,
            enemy_team_kills=41,
            game_end_result=1,
        )
        ctrl._apply_live_snapshot(snap_end)

        # End-Of-Game LCU transition captures recap, unlocks button, and shows polite tray alert
        ctrl._apply_lcu_poll("EndOfGame", None)
        qapp.processEvents()

        recap = ctrl._last_recap_data
        assert isinstance(recap, MatchRecapData)
        assert recap.hero_id == "103"
        assert recap.hero_name == "阿狸"
        assert recap.hero_icon_path == str(hero_png)
        assert recap.won == 1
        assert recap.kda == "14/5/22"
        assert recap.team_kills == "52:41"
        assert recap.duration_s == 920
        assert len(recap.augments) == 4
        assert recap.augments[0] == ("珠光护手", "prismatic")
        assert recap.combo_title != ""
        assert len(recap.item_entries) == 6  # Poro-Snax (2052) excluded!
        assert recap.item_entries[0] == ("卢登的伙伴", str(item1_png))
        assert recap.item_entries[1] == ("灭世者的死亡之帽", str(item2_png))
        assert recap.item_entries[2] == ("中娅沙漏", "")  # Uncached icon -> graceful empty path
        assert "莫雷洛秘典" in recap.ai_highlight

        # LookupWindow recap button unlocked & wired directly to _open_last_match_recap
        assert ctrl.lookup_window.recap_btn.isEnabled() is True
        assert "阿狸" in ctrl.lookup_window.recap_btn.toolTip()
        assert "胜利" in ctrl.lookup_window.recap_btn.toolTip()

        # Polite victory tray notification fired
        assert len(tray_messages) == 1
        assert "🏆 本局战报卡已生成，打开主界面点「战报卡」一键复制发群！" in tray_messages[0][1]

        # Clicking LookupWindow recap button opens non-blocking MatchRecapDialog
        ctrl.lookup_window.recap_btn.click()
        qapp.processEvents()
        dlg = ctrl._active_recap_dialog
        assert isinstance(dlg, MatchRecapDialog)
        assert dlg.isModal() is False
        assert dlg.isVisible() is True
        assert dlg.card_image.width() == 760
        assert dlg.card_image.height() == 480
        dlg.close()

        # Loss match without completed combo still unlocks recap button but stays quiet on tray
        tray_messages.clear()
        me_loss = PlayerLiveInfo("me", "阿狸", "Ahri", "ORDER", False, 15, 5, 8, 12, ("6655", "3020"))
        ctrl._apply_live_snapshot(LiveMatchSnapshot(30.0, me_start, (me_start,), (e1,), 1400.0))
        ctrl.sm.selected_augments = ["1001"]
        ctrl._latest_request_id = "hex-loss-1"
        ctrl._on_coach_advice(
            CoachAdvicePayload(
                request_id="hex-loss-1",
                next_items=[CoachAdviceItem("3157", "中娅沙漏", "推荐", "保命规避刺客突进")],
                tactical_summary="",  # Tests fallback to key counter item reason!
            )
        )
        ctrl._apply_live_snapshot(
            LiveMatchSnapshot(
                game_time=610.0,
                my_player=me_loss,
                allies=(me_loss,),
                enemies=(e1,),
                current_gold=600.0,
                my_team_kills=25,
                enemy_team_kills=38,
                game_end_result=0,
            )
        )
        ctrl._apply_lcu_poll("EndOfGame", None)
        assert ctrl._last_recap_data is not None
        assert ctrl._last_recap_data.won == 0
        assert "中娅沙漏" in ctrl._last_recap_data.ai_highlight
        assert ctrl.lookup_window.recap_btn.isEnabled() is True
        assert tray_messages == []
    finally:
        ctrl.shutdown()
        qapp.processEvents()
