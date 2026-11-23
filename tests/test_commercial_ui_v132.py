"""Commercial conversion & client account UX tests for HexHelper v1.3.2."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QWidget

import hex_helper.edition as edition_mod
from hex_helper.cloud.client import CloudGatewayClient
from hex_helper.config import AppConfig
from hex_helper.ui.account_dialogs import (
    ESTIMATED_CREDITS_PER_MATCH,
    RechargeDialog,
    estimate_remaining_matches,
    format_balance_match_friendly,
    format_package_unit_hint,
)
from hex_helper.ui.lookup_window import LookupWindow


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_estimate_remaining_matches_tiers_and_edge_cases() -> None:
    assert ESTIMATED_CREDITS_PER_MATCH == {"a": 220.0, "auto": 320.0, "b": 1100.0}

    assert estimate_remaining_matches(0.0, "auto") == 0
    assert estimate_remaining_matches(-50.0, "a") == 0
    assert estimate_remaining_matches(49.9, "auto") == 0

    # Small positive balances >= 50 credits must not display misleading 0 局
    assert estimate_remaining_matches(50.0, "auto") == 1
    assert estimate_remaining_matches(120.0, "b") == 1
    assert estimate_remaining_matches(219.0, "a") == 1

    # Standard match-count division per tier
    assert estimate_remaining_matches(4850.0, "auto") == 15
    assert estimate_remaining_matches(4850.0, "a") == 22
    assert estimate_remaining_matches(4850.0, "b") == 4
    assert estimate_remaining_matches(10000.0, "auto") == 31
    assert estimate_remaining_matches(10000.0, "a") == 45
    assert estimate_remaining_matches(10000.0, "b") == 9

    # Case-insensitive & unknown tier falls back to auto
    assert estimate_remaining_matches(4850.0, "AUTO") == 15
    assert estimate_remaining_matches(4850.0, "unknown") == 15


def test_format_balance_match_friendly_display() -> None:
    assert format_balance_match_friendly(4850.0, "auto") == "约剩 15 局 (4,850 积分)"
    assert format_balance_match_friendly(10000.0, "a") == "约剩 45 局 (10,000 积分)"
    assert format_balance_match_friendly(10000.0, "b") == "约剩 9 局 (10,000 积分)"
    assert format_balance_match_friendly(80.0, "b") == "约剩 1 局 (80 积分)"
    assert format_balance_match_friendly(0.0, "auto") == "约剩 0 局 (0 积分)"


def test_format_package_unit_hint_jiao_yuan_and_reassurance() -> None:
    assert format_package_unit_hint(500, 5000, "auto") == "约 16 局 · 约 3 毛/局 · 永不过期"
    assert format_package_unit_hint(1000, 10000, "auto") == "约 31 局 · 约 3 毛/局 · 永不过期"
    assert format_package_unit_hint(3000, 30000, "auto") == "约 94 局 · 约 3 毛/局 · 永不过期"
    assert format_package_unit_hint(5000, 50000, "auto") == "约 156 局 · 约 3 毛/局 · 永不过期"

    # Tier A cheaper (~2 jiao/match), Tier B in yuan (~1.1 yuan/match)
    assert format_package_unit_hint(1000, 10000, "a") == "约 45 局 · 约 2 毛/局 · 永不过期"
    assert format_package_unit_hint(1000, 10000, "b") == "约 9 局 · 约 1.1 元/局 · 永不过期"

    # Edge cases: ultra-cheap promo, ~0.98 yuan avoids "10 毛/局", zero credits safe
    assert "不足 1 毛/局" in format_package_unit_hint(100, 5000, "auto")
    assert "10 毛/局" not in format_package_unit_hint(970, 3200, "auto")
    assert "约 1 元/局" in format_package_unit_hint(970, 3200, "auto")
    assert format_package_unit_hint(500, 0, "auto") == "永不过期"


def test_recharge_dialog_match_friendly_pills_balance_and_compact_height(qapp: QApplication) -> None:
    cloud = CloudGatewayClient("https://api.hexhelper.example.com")
    dlg = RechargeDialog(cloud, auto_load=False, tier="auto", current_balance=4850.0)
    try:
        assert "约剩 15 局 (4,850 积分)" in dlg.balance_lbl.text()

        dlg._populate_packages(
            {
                "packages": [
                    {"id": "pkg_5", "label": "体验包 · 5,000 积分（约 7-10 局）", "amount_fen": 500, "credits": 5000},
                    {"id": "pkg_10", "label": "畅玩包 · 10,000 积分（约 15-20 局）", "amount_fen": 1000, "credits": 10000},
                    {"id": "pkg_30", "label": "进阶包 · 30,000 积分（约 45-60 局）", "amount_fen": 3000, "credits": 30000},
                    {"id": "pkg_50", "label": "高玩包 · 50,000 积分（约 75-100 局）", "amount_fen": 5000, "credits": 50000},
                ],
                "channels": [
                    {"channel": "manual_wechat", "label": "微信扫码支付"},
                    {"channel": "manual_alipay", "label": "支付宝扫码支付"},
                ],
                "pricing_summary": {
                    "tier_a_input_credits_per_1k": 2.0,
                    "tier_a_output_credits_per_1k": 8.0,
                    "input_credits_per_1k": 5.4,
                    "output_credits_per_1k": 27.0,
                },
            }
        )

        # Every package card has a Hextech gold/cyan pill with matches, per-match price & 永不过期
        assert len(dlg._pkg_cards) == 4
        assert dlg._pkg_cards["pkg_5"].hint_pill.text() == "约 16 局 · 约 3 毛/局 · 永不过期"
        assert dlg._pkg_cards["pkg_10"].hint_pill.text() == "约 31 局 · 约 3 毛/局 · 永不过期"
        assert "永不过期" in dlg._pkg_cards["pkg_50"].hint_pill.text()
        assert "7-10 局" not in dlg._pkg_cards["pkg_5"].radio.text()

        # Selecting a different package updates active card state
        dlg._pkg_cards["pkg_30"].radio.setChecked(True)
        assert dlg._selected_package_id == "pkg_30"
        assert "#0AC8B9" in dlg._pkg_cards["pkg_30"].styleSheet()

        # Dual-column QR mode remains strictly compact (<= 500px, well below 560px) with zero clipping
        dlg.show_created_order(
            {
                "order_no": "HEX-V132-001",
                "memo_code": "A7K9",
                "pay_amount_yuan": "30.01",
                "channel_label": "微信扫码支付",
                "credits_amount": 30000,
                "qr_payload": "wxp://hexhelper-v132-test",
            }
        )
        dlg.show()
        qapp.processEvents()
        assert dlg.height() <= 500, f"RechargeDialog dual-column QR height exceeded compact budget: {dlg.height()}px"
        assert dlg.order_card.isVisible() is True
        assert dlg.order_card.geometry().right() <= dlg.width()
        for card in dlg._pkg_cards.values():
            assert card.isVisible() is True
            assert card.hint_pill.geometry().right() <= card.width()
            assert not card.radio.geometry().intersects(card.hint_pill.geometry())

        # Paid poll updates header balance immediately to match-friendly display
        dlg._handle_poll_result(
            {
                "order_no": "HEX-V132-001",
                "status": "paid",
                "credits_amount": 30000,
                "wallet": {"balance_credits": 34850.0},
            }
        )
        assert "约剩 108 局 (34,850 积分)" in dlg.balance_lbl.text()

        # Strict LOL Hextech palette check: zero Web orange #FF5A1F, zero CDN URLs
        all_qss = dlg.styleSheet() + " ".join(w.styleSheet() for w in dlg.findChildren(QWidget))
        assert "#FF5A1F" not in all_qss.upper()
        assert "cdn." not in all_qss.lower()
    finally:
        dlg.close()
        qapp.processEvents()


def test_lookup_window_940x610_match_friendly_balance_recap_button_and_tier_card(
    qapp: QApplication,
) -> None:
    prev_ed = edition_mod._COMPILED_EDITION
    try:
        edition_mod._COMPILED_EDITION = "commercial"
        recap_clicks: list[bool] = []
        switched_tiers: list[str] = []

        # Backward compatibility: omitting on_open_recap works with zero error
        lw_compat = LookupWindow(cfg=AppConfig())
        assert hasattr(lw_compat, "recap_btn")
        assert lw_compat.recap_btn.isEnabled() is False
        lw_compat.close()

        lw = LookupWindow(
            cfg=AppConfig(coach_tier="auto"),
            on_switch_tier=lambda t: switched_tiers.append(t),
            on_open_recap=lambda: recap_clicks.append(True),
        )
        lw.resize(940, 610)
        lw.set_account_state(
            "AlanV132",
            {"balance_credits": 4850.0, "balance_cny": 4.85, "ai_enabled": True, "ai_cutoff": False},
            tier="auto",
        )
        lw._switch_page(2, force=True)
        lw.show()
        qapp.processEvents()

        # 1. Match-friendly balance leads with "约剩 X 局" and updates dynamically on tier switch
        assert lw.account_balance_lbl.text().startswith("约剩 15 局 (4,850 积分)")
        assert "4.85" in lw.account_balance_lbl.text()
        assert "约剩15局" in lw.coach_badge_lbl.text()
        assert "永不过期" in lw.account_est_lbl.text()

        lw.tier_a_btn.click()
        assert switched_tiers == ["a"]
        assert lw.account_balance_lbl.text().startswith("约剩 22 局 (4,850 积分)")
        lw.tier_b_btn.click()
        assert lw.account_balance_lbl.text().startswith("约剩 4 局 (4,850 积分)")
        lw.tier_auto_btn.click()
        assert lw.account_balance_lbl.text().startswith("约剩 15 局 (4,850 积分)")

        # 2. Free vs Paid + Auto hybrid tier explanation card
        assert lw.tier_info_card is not None
        assert lw.tier_info_lbl is not None
        info_txt = lw.tier_info_lbl.text()
        assert "永久免费" in info_txt
        assert "Auto 智能混合" in info_txt
        assert "Gemini" in info_txt
        assert "DeepSeek" in info_txt

        # 3. "🏆 战报卡" button: disabled before finished match, lights up & triggers callback after set_recap_available(True)
        assert lw.recap_btn.text() == "🏆 战报卡"
        assert lw.recap_btn.isEnabled() is False
        lw.recap_btn.click()
        assert recap_clicks == []

        lw.set_recap_available(True, summary="布兰德 · 胜利 14/5/22")
        assert lw.recap_btn.isEnabled() is True
        assert "点击查看并一键复制本局海克斯战报图" in lw.recap_btn.toolTip()
        assert "布兰德" in lw.recap_btn.toolTip()
        assert "#C8AA6E" in lw.recap_btn.styleSheet()
        lw.recap_btn.click()
        assert recap_clicks == [True]

        lw.set_recap_available(False)
        assert lw.recap_btn.isEnabled() is False
        lw.recap_btn.click()
        assert recap_clicks == [True]

        # 4. Layout integrity at 940x610: zero overlap, zero horizontal clipping, timeline uncrushed
        lw.set_recap_available(True, summary="阿狸 · 胜利")
        qapp.processEvents()
        assert lw.recap_btn.isVisible() is True
        assert lw.recap_btn.geometry().right() <= lw.width()
        assert not lw.recap_btn.geometry().intersects(lw.status_lbl.geometry())

        action_btns = [b for b in (lw.recharge_btn, lw.logout_btn, lw.check_update_btn) if b.isVisible()]
        for i in range(len(action_btns)):
            for j in range(i + 1, len(action_btns)):
                assert not action_btns[i].geometry().intersects(action_btns[j].geometry())

        tier_btns = [lw.tier_auto_btn, lw.tier_a_btn, lw.tier_b_btn]
        for b in tier_btns:
            assert b.isVisible() is True
            assert b.geometry().right() <= lw.account_card.width()
        for i in range(len(tier_btns)):
            for j in range(i + 1, len(tier_btns)):
                assert not tier_btns[i].geometry().intersects(tier_btns[j].geometry())

        assert lw.coach_output_card.height() >= 150, (
            f"Coach timeline card crushed at 940x610: {lw.coach_output_card.height()}px"
        )

        # 5. Strict LOL Hextech palette check: zero Web orange #FF5A1F
        all_qss = lw.styleSheet() + " ".join(w.styleSheet() for w in lw.findChildren(QWidget))
        assert "#FF5A1F" not in all_qss.upper()

        lw.close()
        qapp.processEvents()
    finally:
        edition_mod._COMPILED_EDITION = prev_ed
