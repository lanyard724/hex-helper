"""Tests for Weathered Hextech Seal visuals, 6-state core lifecycle, peephole hints & dual-track unseal."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

import hex_helper.edition as edition_mod
from hex_helper.cloud.client import CloudGatewayClient
from hex_helper.config import AppConfig, load_config, save_config
from hex_helper.ui.account_dialogs import RechargeDialog
from hex_helper.ui.augment_overlay import AugmentBadgesOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.hex_seal_overlay import (
    CoreEnergyState,
    CoreInsightPreviewDialog,
    HextechSealOverlay,
    HextechSealSandboxDialog,
    SealMode,
    build_live_peephole_hint,
    resolve_core_energy_state,
)
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.match_recap_card import MatchRecapData, MatchRecapDialog, render_match_recap_image


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_config_seal_persistence_roundtrip(tmp_path: Path) -> None:
    cfg = AppConfig(
        unsealed_user_ids=["u1", "u2", "u1", ""],
        seal_snooze_until=1799999999,
        newbie_banner_dismissed=True,
        opening_brief_seal_dismissed_count=3,
    )
    p = tmp_path / "config.json"
    save_config(cfg, p)
    reloaded = load_config(p)
    assert reloaded.unsealed_user_ids == ["u1", "u2"]
    assert reloaded.seal_snooze_until == 1799999999
    assert reloaded.newbie_banner_dismissed is True
    assert reloaded.opening_brief_seal_dismissed_count == 3


def test_resolve_core_energy_state_all_six_stages_and_veteran_exemption() -> None:
    # Priority 1: System hard gate always wins
    s_gate = resolve_core_energy_state(
        is_logged_in=False,
        username=None,
        wallet=None,
        cutoff_code="SERVICE_LOCKED",
        cutoff_reason="维护中",
    )
    assert s_gate.state == CoreEnergyState.SYSTEM_GATE
    assert s_gate.seal_mode == SealMode.UNLOCKED

    # Priority 2: Manual coach_mode='off' hides all commercial seals
    s_off = resolve_core_energy_state(
        is_logged_in=True,
        username="PureLocal",
        wallet={"balance_credits": 0, "ai_cutoff": True},
        coach_mode="off",
    )
    assert s_off.state == CoreEnergyState.DISABLED_BY_USER

    # S0: Guest unclaimed (NEVER rusted!)
    s0 = resolve_core_energy_state(is_logged_in=False, username="", wallet=None)
    assert s0.state == CoreEnergyState.GUEST_UNCLAIMED
    assert s0.seal_mode == SealMode.GUEST_CLAIM

    # S1a & S1b: Trial active & low energy soft warning
    s1a = resolve_core_energy_state(
        is_logged_in=True,
        username="Newbie",
        wallet={"id": "u10", "balance_credits": 450.0, "total_recharged_fen": 0, "ai_enabled": True, "ai_cutoff": False},
        tier="auto",
    )
    assert s1a.state == CoreEnergyState.TRIAL_LOW  # 450 // 320 == 1 match left
    assert s1a.is_low_energy is True

    s1_rich = resolve_core_energy_state(
        is_logged_in=True,
        username="Newbie",
        wallet={"id": "u10", "balance_credits": 700.0, "total_recharged_fen": 0, "ai_enabled": True, "ai_cutoff": False},
        tier="auto",
    )
    assert s1_rich.state == CoreEnergyState.TRIAL_ACTIVE
    assert s1_rich.seal_mode == SealMode.UNLOCKED

    # S2: Trial exhausted -> Signature Weathered Hextech Seal
    s2 = resolve_core_energy_state(
        is_logged_in=True,
        username="Newbie",
        wallet={"id": "u10", "balance_credits": 0.0, "total_recharged_fen": 0, "ai_enabled": False, "ai_cutoff": True},
        tier="auto",
    )
    assert s2.state == CoreEnergyState.CORRODED_TRIAL_END
    assert s2.seal_mode == SealMode.CORRODED

    # S2b: Returning paid user at 0 credits -> Clean dormant recharge slot
    s2b = resolve_core_energy_state(
        is_logged_in=True,
        username="PaidVet",
        wallet={"id": "u20", "balance_credits": 0.0, "total_recharged_fen": 990, "ai_enabled": False, "ai_cutoff": True},
        tier="auto",
    )
    assert s2b.state == CoreEnergyState.CORRODED_RETURNING
    assert s2b.seal_mode == SealMode.RETURNING_DORMANT

    # S3: First recharge armed -> READY_TO_UNSEAL
    s3 = resolve_core_energy_state(
        is_logged_in=True,
        username="NewPayer",
        wallet={
            "id": "u30",
            "balance_credits": 10500.0,
            "total_recharged_fen": 990,
            "total_spent_micro_credits": 0,
            "ai_enabled": True,
            "ai_cutoff": False,
        },
        unsealed_user_ids=[],
        force_ready_to_unseal=True,
    )
    assert s3.state == CoreEnergyState.READY_TO_UNSEAL
    assert s3.seal_mode == SealMode.CHARGED_READY

    # Veteran active user with spent history smoothly exempted from re-locking on upgrade
    s4_vet = resolve_core_energy_state(
        is_logged_in=True,
        username="OldActive",
        wallet={
            "id": "u40",
            "balance_credits": 5000.0,
            "total_recharged_fen": 1990,
            "total_spent_micro_credits": 820000,
            "ai_enabled": True,
            "ai_cutoff": False,
        },
        unsealed_user_ids=[],
    )
    assert s4_vet.state == CoreEnergyState.ACTIVE_PAID
    assert s4_vet.seal_mode == SealMode.UNLOCKED


def test_live_peephole_hint_priority() -> None:
    h_spec = build_live_peephole_hint(
        state=CoreEnergyState.CORRODED_TRIAL_END,
        my_gold=1800,
        is_dead=True,
        special_augment_name="小蜜蜂！！！",
    )
    assert "小蜜蜂" in h_spec and "重构" in h_spec

    h_heal = build_live_peephole_hint(
        state=CoreEnergyState.CORRODED_TRIAL_END,
        my_gold=1650,
        is_dead=True,
        enemy_heal_high=True,
        ally_has_grievous=False,
    )
    assert "1,650G" in h_heal and "重伤" in h_heal

    h_guest = build_live_peephole_hint(
        state=CoreEnergyState.GUEST_UNCLAIMED,
        my_gold=1400,
        is_dead=True,
        top_threat_name="亚托克斯",
        top_threat_kda="8/2",
    )
    assert "领 450 积分唤醒" in h_guest and "亚托克斯" in h_guest


def test_seal_overlay_zero_focus_idle_timer_and_shatter_frames(qapp: QApplication) -> None:
    host = QWidget()
    host.resize(300, 110)
    seal = HextechSealOverlay(host, seal_id="test_unit", title="测试封印", compact=True, allow_idle_ambient=False)
    try:
        assert seal.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert seal.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating) is True

        host.show()
        seal.set_seal_mode(SealMode.CORRODED, subtitle="手握 1,650G · 反制待唤醒", cta_text="查看差异")
        qapp.processEvents()
        # In-game compact idle must run at 0 FPS (timer stopped -> static DPR pixmap cache)
        assert seal._tick_timer.isActive() is False

        # Deterministic shatter frames render cleanly without errors
        for ms in (80.0, 200.0, 410.0, 650.0):
            seal.set_unseal_frame_for_test(ms, fast=False)
            pm = host.grab()
            assert not pm.isNull() and pm.width() > 0

        # Lobby ambient timer freezes immediately when LOL game becomes active
        lobby_seal = HextechSealOverlay(host, seal_id="lobby_test", title="大厅封印", compact=False, allow_idle_ambient=True)
        lobby_seal.set_seal_mode(SealMode.CORRODED)
        qapp.processEvents()
        assert lobby_seal._tick_timer.isActive() is True
        lobby_seal.set_game_active(True)
        assert lobby_seal._tick_timer.isActive() is False
        lobby_seal.set_seal_mode(SealMode.UNLOCKED)
        assert lobby_seal.isVisible() is False
    finally:
        host.close()
        qapp.processEvents()


def test_preview_dialog_snooze_and_dual_track_recharge_ceremony(qapp: QApplication) -> None:
    dlg = CoreInsightPreviewDialog(
        state=CoreEnergyState.CORRODED_TRIAL_END,
        last_ai_highlight="第 3 件补湮灭宝珠压制 9/2 剑魔",
    )
    try:
        dlg.select_tab(2)
        assert "散件" in dlg.ai_title_lbl.text()
        dlg.snooze_btn.click()
        assert dlg.chosen_action == "snooze"
    finally:
        dlg.close()

    cloud = CloudGatewayClient("https://api.hexhelper.example.com")
    rdlg = RechargeDialog(cloud, auto_load=False, tier="auto", in_game=False, is_first_recharge=True)
    try:
        rdlg.show_created_order(
            {
                "order_no": "HEX-SEAL-001",
                "memo_code": "Z9K1",
                "pay_amount_yuan": "9.91",
                "credits_amount": 10500,
                "qr_payload": "wxp://seal-test",
            }
        )
        rdlg._handle_poll_result(
            {
                "order_no": "HEX-SEAL-001",
                "status": "paid",
                "credits_amount": 10500,
                "wallet": {"balance_credits": 10500.0, "total_recharged_fen": 990},
            }
        )
        assert rdlg.paid_ceremony_box.isHidden() is False
        assert rdlg.qr_image_lbl.isHidden() is True
        assert "击碎封印" in rdlg.paid_ceremony_btn.text()
        rdlg.paid_ceremony_btn.click()
        assert rdlg.unseal_requested is True
    finally:
        rdlg.close()


def test_commercial_surfaces_no_crimson_for_dormant_and_clean_recap_png(qapp: QApplication) -> None:
    prev_ed = edition_mod._COMPILED_EDITION
    try:
        edition_mod._COMPILED_EDITION = "commercial"
        lw = LookupWindow(cfg=AppConfig())
        lw.resize(940, 610)
        snap_s2 = resolve_core_energy_state(
            is_logged_in=True,
            username="TrialDone",
            wallet={"id": "u99", "balance_credits": 0.0, "total_recharged_fen": 0, "ai_enabled": False, "ai_cutoff": True},
        )
        lw.set_account_state(
            "TrialDone",
            {"id": "u99", "balance_credits": 0.0, "total_recharged_fen": 0, "ai_enabled": False, "ai_cutoff": True},
            cutoff_code="INSUFFICIENT_CREDITS",
            cutoff_reason="核心能量已尽",
        )
        lw.set_core_lifecycle(snap_s2)
        lw._switch_page(2, force=True)
        lw.show()
        qapp.processEvents()
        # No crimson red alarm banner for normal 0-credit user!
        assert "rgba(120, 28, 28" not in lw.cutoff_banner.styleSheet()
        assert lw.seal_shelf_wrap is not None and lw.seal_shelf_wrap.isVisible() is True
        assert lw.coach_output_card.height() >= 150

        # Drawer also drops crimson banner and shows calm neutral pill dot
        drawer = EdgeBuildDrawer()
        drawer.set_ai_cutoff(True, reason="核心能量已尽", balance_credits=0.0, code="INSUFFICIENT_CREDITS")
        drawer.set_core_lifecycle(snap_s2)
        assert "rgba(120, 28, 28" not in drawer.cutoff_banner.styleSheet()
        assert "#5B5A56" in drawer.pill_coach_dot.styleSheet()
        assert drawer.coach_seal_slot.isHidden() is False

        # ChampSelect 26px bar shows cleanly and hides when snoozed
        cs = ChampSelectOverlay()
        cs.set_core_lifecycle(snap_s2)
        assert cs.ai_readiness_bar.isHidden() is False
        snap_snoozed = resolve_core_energy_state(
            is_logged_in=True,
            username="TrialDone",
            wallet={"id": "u99", "balance_credits": 0.0, "total_recharged_fen": 0, "ai_enabled": False, "ai_cutoff": True},
            seal_snooze_until=int(time.time()) + 3600,
        )
        cs.set_core_lifecycle(snap_snoozed)
        assert cs.ai_readiness_bar.isHidden() is True

        # Recap PNG image remains 100% pure (identical to non-commercial render), outer bar is only in dialog!
        recap = MatchRecapData(hero_id="63", hero_name="布兰德", won=1, kda="12/4/19")
        pure_img = render_match_recap_image(recap)
        rdlg = MatchRecapDialog(recap, lifecycle_state=CoreEnergyState.CORRODED_TRIAL_END)
        assert rdlg.core_nudge_bar is not None
        assert rdlg.card_image.size() == pure_img.size()
        assert rdlg.card_image.bits().tobytes() == pure_img.bits().tobytes()

        # Zero #FF5A1F anywhere in desktop widgets
        sandbox = HextechSealSandboxDialog()
        for root_w in (lw, drawer, cs, rdlg, sandbox):
            qss = root_w.styleSheet() + " ".join(w.styleSheet() for w in root_w.findChildren(QWidget))
            assert "#FF5A1F" not in qss.upper()
        sandbox.close()
        rdlg.close()
        cs.close()
        drawer.close()
        lw.close()
        qapp.processEvents()
    finally:
        edition_mod._COMPILED_EDITION = prev_ed
