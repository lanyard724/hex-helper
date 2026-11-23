"""Regression tests for HexHelper v1.1.1 (P0 bugfixes, P1 responsive/DPI layout, P2 zero-proxy network)."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

import hex_helper.edition as edition_mod
from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload
from hex_helper.cloud.client import CloudCoachWorker, CloudGatewayClient
from hex_helper.config import AppConfig
from hex_helper.data.aramhex_sync import AramHexSyncer, http_get_cached
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.models import PlayerLiveInfo
from hex_helper.scoring.augment_combo import build_combo_plan, evaluate_picker_combos
from hex_helper.scoring.matchup_optimizer import build_next_buy_plan, optimize_next_items
from hex_helper.scoring.tier import AugmentScore
from hex_helper.setup.preflight import PreflightItem, PreflightReport
from hex_helper.ui.account_dialogs import RechargeDialog
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.setup_wizard import SetupWizardDialog
from hex_helper.ui.win32_window import clamp_to_screen_rect, physical_rect_to_logical


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_ap_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "cache", proxy_url="", fetcher=lambda _u: b"{}")
    static.items = {
        "3152": ItemStaticInfo("3152", "海克斯科技火箭腰带", 2600, ("SpellDamage",), {"FlatMagicDamageMod": 70.0}, "", "complete_equipment"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110.0}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment"),
        "3165": ItemStaticInfo("3165", "莫雷洛秘典", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 75.0}, "造成魔法伤害施加40%重伤", "complete_equipment", has_grievous=True),
        "3916": ItemStaticInfo("3916", "遗忘宝珠", 800, ("SpellDamage",), {"FlatMagicDamageMod": 25.0}, "重伤", "other", has_grievous=True),
        "3033": ItemStaticInfo("3033", "凡性的提醒", 3000, ("Damage", "CriticalStrike", "ArmorPenetration"), {"FlatPhysicalDamageMod": 35.0, "FlatCritChanceMod": 0.25}, "造成物理伤害施加40%重伤", "complete_equipment", has_grievous=True),
        "3036": ItemStaticInfo("3036", "多米尼克领主的致意", 3000, ("Damage", "CriticalStrike", "ArmorPenetration"), {"FlatPhysicalDamageMod": 35.0, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3123": ItemStaticInfo("3123", "死刑宣告", 800, ("Damage",), {"FlatPhysicalDamageMod": 15.0}, "重伤", "other", has_grievous=True),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage",), {"FlatMagicDamageMod": 100.0}, "", "complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage",), {"FlatMagicDamageMod": 95.0}, "", "complete_equipment"),
        "6610": ItemStaticInfo("6610", "焚天", 3100, ("Damage",), {"FlatPhysicalDamageMod": 45.0, "FlatHPPoolMod": 400.0}, "", "complete_equipment"),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, ("Health", "SpellBlock"), {"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 50.0}, "", "complete_equipment"),
        "6617": ItemStaticInfo("6617", "月石再生器", 2200, ("ManaRegen",), {"FlatMagicDamageMod": 25.0}, "", "complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0}, "", "complete_equipment"),
    }
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "103": HeroStat("103", "阿狸", "九尾妖狐", 50000, 26000, 0.52, 0.05, 20, False),
    }
    store._augments = {
        "1048": AugmentInfo("1048", "珠光护手", "prismatic"),
        "3081": AugmentInfo("3081", "命运之武器", "gold"),
        "1030": AugmentInfo("1030", "尤里卡", "prismatic"),
        "1045": AugmentInfo("1045", "炼狱导管", "prismatic"),
        "1092": AugmentInfo("1092", "易损", "gold"),
        "1225": AugmentInfo("1225", "双刀流", "prismatic"),
    }
    store._core_builds = {
        "103": [CoreBuild(("6655", "4645", "3089"), True, 10000, 5400, 0.54, 0.35)],
    }
    return store


def test_p0_app_ocr_exclusion_and_signup_username_attribute():
    from hex_helper import app as app_mod

    src = inspect.getsource(app_mod.HexHelperController._tick_vision)
    assert "exclude_rects=self._get_all_overlay_exclude_rects()" in src

    login_src = inspect.getsource(app_mod.HexHelperController.open_login_dialog)
    assert "self.cloud_client.current_username" in login_src
    assert "self.cloud_client.username" not in login_src


def test_p0_combo_conflict_ignores_meta_axes_and_no_false_completed(tmp_path):
    store = _make_ap_store(tmp_path)
    sel = ["1048", "3081", "1030"]
    verdict = evaluate_picker_combos(
        store,
        hero_id="103",
        selected_augments=sel,
        my_items=("3152", "3020"),
        offered=[
            (0, store.get_augment("1045")),
            (1, store.get_augment("1092")),
            (2, store.get_augment("1225")),
        ],
    )
    dw = next(c for c in verdict.cards if c.augment_id == "1225")
    assert dw.verdict == "conflict", f"Expected Dual Wield conflict on AP Ahri, got {dw}"
    assert dw.combo_delta < 0

    plan = build_combo_plan(store, "103", sel, ("3152", "3020"))
    for c in plan.active_combos:
        if len(c.missing_core_ids) >= 2:
            assert not c.completed, f"Combo {c.name} missing {c.missing_core_names} must not be completed"
    for t in plan.recommended_targets:
        assert f"【{t.combo_name}】" not in t.reason


def test_p0_ap_jeweled_gauntlet_blocks_physical_mortal_reminder(tmp_path):
    store = _make_ap_store(tmp_path)
    enemies = (
        PlayerLiveInfo("E1", "亚托克斯", "Aatrox", "CHAOS", False, 12, 11, 2, 5, ("6610", "3065")),
        PlayerLiveInfo("E2", "索拉卡", "Soraka", "CHAOS", False, 11, 1, 3, 20, ("6617",)),
        PlayerLiveInfo("E3", "蒙多医生", "DrMundo", "CHAOS", False, 12, 6, 4, 11, ("3084", "3065")),
    )
    _th, recs = optimize_next_items(
        store, "103", ("3152", "3020"), 1650.0, ["1048", "3081", "1030"], (), enemies, limit=6
    )
    ids = [r.item_id for r in recs]
    assert "3033" not in ids
    assert "3036" not in ids
    assert "3123" not in ids
    assert "3165" in ids or "3916" in ids

    plan = build_next_buy_plan(store, "103", ("3152", "3020"), 1650.0, recs)
    assert plan is not None
    gw_count = sum(1 for iid in plan.remaining_build_ids if store.get_item_info(iid).has_grievous)
    assert gw_count <= 1


def test_p0_augment_overlay_reroll_banner_and_responsive_width(qapp):
    assert AugmentBadgesOverlay.compute_badge_width(2560) == 258
    assert AugmentBadgesOverlay.compute_badge_width(1536) == 226
    assert AugmentBadgesOverlay.compute_badge_width(1280) <= 214

    ov = AugmentBadgesOverlay()
    items = [
        AugmentBadgeRenderItem(
            slot_index=i,
            score=AugmentScore(str(1000 + i), f"卡{i}", "gold", "C", 0.48, 0.48, -0.02, 400, False),
            screen_box=(400 + i * 300, 400, 560 + i * 300, 440),
            is_best=False,
        )
        for i in range(3)
    ]
    ov.render_cards(items, (0, 0, 1920, 1080), reroll_banner="本轮三卡均负收益（最高仅 C 级）· 建议用骰子刷新")
    assert ov._coach_banner is not None
    assert "建议用骰子刷新" in ov._coach_banner_lbl.text()
    ov.hide()


def test_p1_responsive_widths_dpi_clamp_and_modal_heights(qapp, tmp_path):
    assert AugmentComboOverlay.compute_expanded_width(1920) == 316
    assert AugmentComboOverlay.compute_expanded_width(1536) == 268
    assert AugmentComboOverlay.compute_expanded_width(1280) == 244

    # Clamp stays strictly inside target rect
    cx, cy = clamp_to_screen_rect(1800, 900, 346, 500, QRect(0, 0, 1280, 720), margin=8)
    assert cx + 346 <= 1280 - 8
    assert cy + 500 <= 720 - 8

    # RechargeDialog with active QR stays compact (<=500px tall) and consumes dynamic pricing_summary
    cloud = CloudGatewayClient("https://api.hexhelper.example.com")
    dlg = RechargeDialog(cloud, auto_load=False)
    dlg._populate_packages(
        {
            "packages": [
                {"id": "pkg_5", "label": "体验包 · 5,000 积分", "amount_yuan": 5.0},
                {"id": "pkg_10", "label": "畅玩包 · 10,000 积分", "amount_yuan": 10.0},
                {"id": "pkg_30", "label": "进阶包 · 30,000 积分", "amount_yuan": 30.0},
                {"id": "pkg_50", "label": "高玩包 · 50,000 积分", "amount_yuan": 50.0},
            ],
            "channels": [{"channel": "manual_wechat", "label": "微信扫码支付"}],
            "pricing_summary": {
                "tier_a_input_credits_per_1k": 1.5,
                "tier_a_output_credits_per_1k": 6.0,
                "input_credits_per_1k": 4.5,
                "output_credits_per_1k": 22.0,
            },
        }
    )
    assert "1.5 / 6" in dlg.pricing_lbl.text()
    assert "4.5 / 22" in dlg.pricing_lbl.text()
    dlg.show_created_order(
        {
            "order_no": "HEX123",
            "memo_code": "K9A2",
            "pay_amount_yuan": "10.01",
            "channel_label": "微信扫码支付",
            "credits_amount": 10000,
            "qr_payload": "wxp://test_qr",
        }
    )
    dlg.show()
    QApplication.processEvents()
    assert dlg.height() <= 500, f"RechargeDialog too tall for 1080p@150%: {dlg.height()}px"
    dlg.hide()

    # SetupWizard stays compact (<=680px wide)
    rep = PreflightReport(
        items=[
            PreflightItem("vc_runtime", "VC++ 运行库", True, "ok"),
            PreflightItem("winrt_ocr", "WinRT OCR", False, "missing", "click fix", critical=True),
        ]
    )
    sw = SetupWizardDialog(AppConfig(), rep, tmp_path)
    sw.show()
    QApplication.processEvents()
    assert sw.width() <= 680, f"SetupWizard too wide: {sw.width()}px"
    sw.hide()

    # LookupWindow at 940x610 commercial coach page has ZERO button overlap
    prev_ed = edition_mod._COMPILED_EDITION
    try:
        edition_mod._COMPILED_EDITION = "commercial"
        lw = LookupWindow(AppConfig())
        lw.resize(940, 610)
        lw.set_account_state(
            "alan_pro_longname",
            {"balance_credits": 14820, "balance_cny": 14.82, "ai_cutoff": False, "ai_enabled": True},
            tier="b",
        )
        lw._switch_page(2, force=True)
        lw.show()
        QApplication.processEvents()
        btns = [b for b in (lw.recharge_btn, lw.logout_btn, lw.check_update_btn) if b.isVisible()]
        for i in range(len(btns)):
            for j in range(i + 1, len(btns)):
                r1 = btns[i].geometry()
                r2 = btns[j].geometry()
                assert not r1.intersects(r2), f"Buttons overlapped at 940px: {r1} vs {r2}"
        lw.hide()
    finally:
        edition_mod._COMPILED_EDITION = prev_ed


def test_p2_http_get_cached_etag_304_zero_redownload(tmp_path):
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if request.headers.get("if-none-match") == '"abc-123"':
            return httpx.Response(304, headers={"ETag": '"abc-123"'})
        return httpx.Response(200, content=b'{"ok": true}', headers={"ETag": '"abc-123"'})

    cache_file = tmp_path / "ddragon_items.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        raw1, from_304_1 = http_get_cached("https://example.test/ddragon_items.json", cache_file, client=client)
        assert not from_304_1
        assert json.loads(raw1) == {"ok": True}
        cache_file.write_bytes(raw1)

        raw2, from_304_2 = http_get_cached("https://example.test/ddragon_items.json", cache_file, client=client)
        assert from_304_2 is True
        assert json.loads(raw2) == {"ok": True}
        assert calls[1].get("if-none-match") == '"abc-123"'


def test_p2_cloud_client_retries_idempotent_get_and_worker_retries_429():
    attempts = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/orders/packages":
            attempts["get"] += 1
            if attempts["get"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return httpx.Response(200, json={"packages": [], "channels": []})
        return httpx.Response(404)

    client = CloudGatewayClient(
        "https://api.test",
        http_client_factory=lambda base, timeout: httpx.Client(base_url=base, transport=httpx.MockTransport(handler)),
    )
    data = client.get_packages()
    assert attempts["get"] == 2
    assert data == {"packages": [], "channels": []}

    # Worker 429 retry once without flashing immediate error
    adv_calls = {"n": 0}
    got_advice: list[CoachAdvicePayload] = []
    got_errors: list[str] = []

    mock_client = MagicMock()
    from hex_helper.cloud.client import CloudAdviseStats, CloudCoachError

    def fake_advise(req, store, tier="a"):
        adv_calls["n"] += 1
        if adv_calls["n"] == 1:
            raise CloudCoachError("COACH_REQUEST_IN_FLIGHT", 429, "in flight")
        return (
            CoachAdvicePayload(request_id=req.request_id, buy_now="直合影焰"),
            CloudAdviseStats(tier="a", model="deepseek-flash", thinking_level="", latency_ms=100),
        )

    mock_client.advise.side_effect = fake_advise
    worker = CloudCoachWorker(
        client=mock_client,
        on_advice=lambda a, _s: got_advice.append(a),
        on_error=lambda _rid, err: got_errors.append(err),
    )
    try:
        req = CoachRequestPayload(
            request_id="req-429-test",
            trigger_reason="augment_selected",
            timestamp="2026-11-22T00:00:00Z",
            dataset_patch="26.23",
            my_hero={"name": "阿狸"},
        )
        worker.submit(req, None, tier="a")
        import time

        deadline = time.monotonic() + 5.0
        while not got_advice and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(got_advice) == 1
        assert got_advice[0].buy_now == "直合影焰"
        assert got_errors == []
        assert adv_calls["n"] == 2
    finally:
        worker.stop()
