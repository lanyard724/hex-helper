"""End-to-end tests for HexHelper v1.0.0 Commercial Client, Self-Updater & Live Uvicorn integration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

from fastapi.testclient import TestClient
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)
import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload, CoachRequestPayload, utc_now_iso
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _build_test_store(cache_dir: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir)
    static.items = {
        "3084": ItemStaticInfo("3084", "心之钢", 3000, category="complete_equipment"),
        "3083": ItemStaticInfo("3083", "狂徒铠甲", 3100, category="complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3500, category="complete_equipment", stats={"FlatMagicDamageMod": 130.0}),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, category="complete_equipment", stats={"FlatMagicDamageMod": 105.0}),
        "3165": ItemStaticInfo("3165", "莫雷洛秘典", 2200, category="complete_equipment", stats={"FlatMagicDamageMod": 75.0}, has_grievous=True),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, category="boots"),
        "2139": ItemStaticInfo("2139", "巫术合剂", 500, category="consumable"),
        "2138": ItemStaticInfo("2138", "钢铁合剂", 500, category="consumable"),
        "2140": ItemStaticInfo("2140", "愤怒合剂", 500, category="consumable"),
    }
    static.hero_aliases["63"] = "Brand"
    static.hero_cn_names["63"] = "布兰德"
    store = DataStore(static)
    store._descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "2026-11-22T00:00:00Z", False)
    store._heroes["63"] = HeroStat("63", "布兰德", "复仇焰魂", 10000, 5200, 0.52, 0.05, 22)
    store._augments["3163"] = AugmentInfo("3163", "迷人蝴蝶", "gold")
    store._core_builds["63"] = [CoreBuild(("3084", "3083", "3157"), True, 1000, 560, 0.56, 0.12)]
    return store


class _ScriptedCloudGateway:
    """In-memory stub for ServerCoachGateway testing Tier A & Tier B grounding + billing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tier_a_advice: dict[str, Any] = {
            "augment_pick_advice": "",
            "buy_now": "直接合成【狂徒铠甲】",
            "build_path": ["心之钢", "狂徒铠甲", "大帽"],
            "next_items": [
                {"item_id": "3084", "name": "心之钢", "priority": "下件必出", "reason": "已拥有应被本地过滤"},
                {"item_id": "3083", "name": "狂徒铠甲", "priority": "下件必出", "reason": "续航拉满"},
                {"item_id": "", "name": "大帽", "priority": "次选", "reason": "别名映射测试"},
            ],
            "tactical_summary": "卡视野消耗前排吸收火力",
        }
        self.tier_b_advice: dict[str, Any] = {
            "augment_pick_advice": "",
            "buy_now": "再买大帽",
            "build_path": ["灭世者的死亡之帽"],
            "next_items": [
                {"item_id": "3089", "name": "灭世者的死亡之帽", "priority": "下件必出", "reason": "常规堆法强"},
            ],
            "tactical_summary": "满格后保持走位拉扯",
        }

    def key_status(self) -> dict[str, Any]:
        return {
            "gemini_key_count": 1,
            "deepseek_key_count": 1,
            "gemini_configured": True,
            "deepseek_configured": True,
        }

    def advise(
        self,
        prompt_payload: dict[str, Any],
        trigger_reason: str,
        tier: str = "a",
        *,
        tier_a_model: str = "deepseek-flash",
        tier_b_model: str = "gemini-4-hydrogen",
        system_instruction_override: str = "",
    ):
        from hex_server.billing import compute_billable_tokens
        from hex_server.coach_gateway import GatewayAdviseResult

        self.calls.append({"tier": tier, "trigger_reason": trigger_reason, "prompt_payload": prompt_payload})
        is_b = tier == "b"
        usage = compute_billable_tokens(
            input_tok=2000,
            cached_tok=0,
            output_tok=250 if is_b else 500,
            thought_tok=750 if is_b else 0,
            total_tok=3000 if is_b else 2500,
        )
        return GatewayAdviseResult(
            advice_json=dict(self.tier_b_advice if is_b else self.tier_a_advice),
            usage=usage,
            tier=tier,
            model=tier_b_model if is_b else tier_a_model,
            thinking_level="medium" if is_b else "",
            latency_ms=42,
        )

    def close(self) -> None:
        return None


def _build_inprocess_server(tmp_path: Path):
    from hex_server.config import ServerSettings
    from hex_server.dataset_mirror import DatasetMirrorService
    from hex_server.main import create_app

    data_dir = tmp_path / "server_data"
    settings = ServerSettings(
        data_dir=data_dir,
        db_path=data_dir / "hex_server.db",
        releases_dir=data_dir / "releases",
        mirror_dir=data_dir / "dataset_mirror",
        uploads_dir=data_dir / "uploads",
        jwt_secret="commercial-e2e-test-secret-key-minimum-32-bytes",
        admin_user="admin",
        admin_password="TestAdminPassword#2026",
    )
    settings.ensure_dirs()
    # Upload valid WeChat QR PNG for QR recharge flow
    (settings.uploads_dir / "manual_wechat.png").write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    )

    # Seed mirrored dataset + static JSON files in mirror_dir
    mirror_dir = settings.mirror_dir
    ds_rel = "patches/16.23/normal"
    ds_dir = mirror_dir / ds_rel
    ds_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "heroes.json": json.dumps(
            {
                "63": {
                    "name": "布兰德",
                    "title": "复仇焰魂",
                    "games": 10000,
                    "wins": 5200,
                    "win_rate": 0.52,
                    "pick_rate": 0.05,
                    "rank": 22,
                    "low_sample": False,
                }
            }
        ).encode("utf-8"),
        "augments.json": json.dumps({"3163": {"name": "迷人蝴蝶", "rarity": "gold"}}).encode("utf-8"),
        "hero_augments.json": json.dumps(
            {"63": {"3163": {"games": 1200, "wins": 680, "win_rate": 0.5667, "low_sample": False}}}
        ).encode("utf-8"),
        "hero_spells.json": json.dumps({"63": {}}).encode("utf-8"),
        "core_builds.json": json.dumps({"63": []}).encode("utf-8"),
        "hero_augment_items.json": json.dumps({}).encode("utf-8"),
        "hero_item_recommendations.json": json.dumps({}).encode("utf-8"),
        "item_metadata.json": json.dumps({"3084": {"category": "complete_equipment"}}).encode("utf-8"),
    }
    for fname, raw in payloads.items():
        (ds_dir / fname).write_bytes(raw)
    meta = {
        "snapshot_at": "2026-11-22T00:00:00Z",
        "files": {fname: hashlib.sha256(raw).hexdigest() for fname, raw in payloads.items()},
    }
    (ds_dir / "meta.json").write_bytes(json.dumps(meta).encode("utf-8"))
    (mirror_dir / "index.json").write_bytes(
        json.dumps(
            {
                "datasets": [
                    {
                        "patch": "16.23",
                        "mode": "normal",
                        "mode_label": "常规",
                        "path": ds_rel,
                        "snapshot_at": "2026-11-22T00:00:00Z",
                        "bands": False,
                        "seq": 10,
                    }
                ]
            }
        ).encode("utf-8")
    )
    (mirror_dir / "latest.json").write_bytes(
        json.dumps({"patch": "16.23", "mode": "normal", "path": ds_rel, "seq": 10}).encode("utf-8")
    )
    (mirror_dir / "ddragon_versions.json").write_bytes(json.dumps(["16.23.1"]).encode("utf-8"))
    (mirror_dir / "ddragon_items.json").write_bytes(
        json.dumps(
            {
                "data": {
                    "3084": {"name": "心之钢", "gold": {"total": 3000}, "tags": ["Health"], "stats": {}},
                    "3083": {"name": "狂徒铠甲", "gold": {"total": 3100}, "tags": ["Health"], "stats": {}},
                }
            }
        ).encode("utf-8")
    )
    (mirror_dir / "tencent_heroes.json").write_bytes(
        json.dumps({"hero": [{"heroId": "63", "alias": "Brand", "title": "布兰德", "name": "复仇焰魂"}]}).encode("utf-8")
    )
    (mirror_dir / "cdragon_augments.json").write_bytes(json.dumps([]).encode("utf-8"))
    (mirror_dir / "cdragon_spells.json").write_bytes(json.dumps([{"id": 4, "name": "闪现"}]).encode("utf-8"))

    gateway = _ScriptedCloudGateway()
    mirror = DatasetMirrorService(mirror_dir)
    app = create_app(settings, gateway=gateway, mirror_service=mirror)
    return app, gateway


# ---------------------------------------------------------------------------
# 1. Commercial Config & Dynamic Edition Defaults
# ---------------------------------------------------------------------------


def test_commercial_config_defaults_and_sanitization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hex_helper.config import COACH_MODES, AppConfig, load_config, save_config

    assert "cloud" in COACH_MODES

    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "https://cloud.hexhelper.cn/")
    cfg = AppConfig()
    assert cfg.proxy_url == ""
    assert cfg.bridge_enabled is False
    assert cfg.coach_mode == "cloud"
    assert cfg.coach_tier == "auto"
    assert cfg.first_run_completed is False
    assert cfg.lol_install_dir == ""
    assert cfg.resolved_cloud_api_base() == "https://cloud.hexhelper.cn"

    # Legacy dev config values are sanitized when loaded in commercial mode
    dirty = AppConfig.from_dict(
        {
            "proxy_url": "http://127.0.0.1:7900",
            "bridge_enabled": True,
            "coach_mode": "gemini",
            "coach_tier": "B",
            "first_run_completed": True,
            "lol_install_dir": r"D:\Games\英雄联盟",
        }
    )
    assert dirty.proxy_url == ""
    assert dirty.bridge_enabled is False
    assert dirty.coach_mode == "cloud"
    assert dirty.coach_tier == "b"
    assert dirty.first_run_completed is True
    assert dirty.lol_install_dir == r"D:\Games\英雄联盟"

    cfg_path = tmp_path / "config.json"
    save_config(dirty, cfg_path)
    reloaded = load_config(cfg_path)
    assert reloaded.coach_tier == "b"
    assert reloaded.proxy_url == ""


# ---------------------------------------------------------------------------
# 2. Windows DPAPI Session Store Roundtrip & Tamper Rejection
# ---------------------------------------------------------------------------


def test_dpapi_session_store_encrypts_and_rejects_tamper(tmp_path: Path) -> None:
    from hex_helper.cloud.session_store import DpapiSessionStore

    session_file = tmp_path / "session.dat"
    store = DpapiSessionStore(session_file)
    assert store.load() is None

    payload = {
        "access_token": "eyJ_secret_access_token_xyz",
        "refresh_token": "rt_secret_refresh_token_987",
        "username": "AlanSummoner",
        "device_id": "win-device-01",
    }
    store.save(payload)
    assert session_file.is_file()

    raw_bytes = session_file.read_bytes()
    assert b"eyJ_secret_access_token_xyz" not in raw_bytes
    assert b"rt_secret_refresh_token_987" not in raw_bytes
    assert b"AlanSummoner" not in raw_bytes

    restored = store.load()
    assert restored == payload

    # Flip middle bytes -> DPAPI must reject tampering cleanly and return None
    tampered = bytearray(raw_bytes)
    tampered[len(tampered) // 2] ^= 0xFF
    tampered[-1] ^= 0x55
    session_file.write_bytes(bytes(tampered))
    assert store.load() is None

    # Truncated/garbage file -> returns None
    session_file.write_bytes(b"corrupt")
    assert store.load() is None

    # Clear deletes file
    store.save(payload)
    assert session_file.is_file()
    store.clear()
    assert not session_file.exists()
    assert store.load() is None


# ---------------------------------------------------------------------------
# 3. Commercial LookupWindow & EdgeBuildDrawer UI Hiding + A/B Controls
# ---------------------------------------------------------------------------


def test_commercial_lookup_window_hides_api_proxy_and_exposes_account_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hex_helper.config import AppConfig
    from hex_helper.ui.lookup_window import LookupWindow

    app = _qt_app()
    monkeypatch.setenv("HEX_EDITION", "commercial")
    cfg = AppConfig()

    switched_tiers: list[str] = []
    rescan_calls: list[bool] = []
    lw = LookupWindow(
        cfg=cfg,
        on_switch_tier=lambda t: switched_tiers.append(t),
        on_rescan_lol=lambda: rescan_calls.append(True),
    )

    # All API / Proxy / Bridge widgets MUST be hidden/omitted in commercial mode
    for attr in (
        "gemini_config_card",
        "gemini_key_edit",
        "gemini_model_combo",
        "gemini_thinking_combo",
        "coach_mode_combo",
        "test_gemini_btn",
        "gemini_adaptive_chk",
        "toggle_gemini_form_btn",
        "proxy_edit",
    ):
        w = getattr(lw, attr, None)
        assert w is None or (not w.isVisible()), f"{attr} must be omitted or hidden in commercial UI"

    # Zero forbidden proxy/VPN/API/bridge text anywhere in descendant widgets
    forbidden_tokens = ("代理", "梯子", "Gemini API Key", "18421", "AIza")
    for w in lw.findChildren(QWidget):
        texts: list[str] = []
        if isinstance(w, (QLabel, QPushButton, QCheckBox)):
            texts.append(w.text())
        elif isinstance(w, QLineEdit):
            texts.extend([w.text(), w.placeholderText()])
        elif isinstance(w, QTextEdit):
            texts.append(w.toPlainText())
        elif isinstance(w, QComboBox):
            texts.extend(w.itemText(i) for i in range(w.count()))
        texts.append(w.toolTip())
        joined = " ".join(t for t in texts if t)
        for tok in forbidden_tokens:
            assert tok not in joined, f"Forbidden commercial UI text {tok!r} found in {w!r}: {joined!r}"

    # Commercial Account/Wallet & A/B Tier controls MUST exist and respond
    assert lw.account_card is not None
    assert lw.tier_a_btn is not None
    assert lw.tier_b_btn is not None
    assert lw.login_btn is not None
    assert lw.recharge_btn is not None
    assert lw.logout_btn is not None
    assert lw.check_update_btn is not None
    assert lw.rescan_lol_btn is not None

    lw.set_account_state(
        "AlanTest",
        {
            "username": "AlanTest",
            "balance_credits": 10000.0,
            "balance_cny": 10.0,
            "ai_enabled": True,
            "ai_cutoff": False,
        },
        tier="a",
    )
    assert "AlanTest" in lw.account_user_lbl.text()
    assert "10,000" in lw.account_balance_lbl.text()
    assert "10.00" in lw.account_balance_lbl.text()
    assert lw.cutoff_banner.isHidden() is True

    lw.tier_b_btn.click()
    assert switched_tiers == ["b"]
    lw.tier_a_btn.click()
    assert switched_tiers == ["b", "a"]

    # Zero-balance cutoff banner
    lw.set_account_state(
        "AlanTest",
        {
            "username": "AlanTest",
            "balance_credits": 0.0,
            "balance_cny": 0.0,
            "ai_enabled": False,
            "ai_cutoff": True,
        },
        tier="a",
        cutoff_reason="余额不足 · AI 已暂停",
    )
    assert lw.cutoff_banner.isHidden() is False
    assert "余额不足" in lw.cutoff_banner_lbl.text()

    lw.rescan_lol_btn.click()
    assert rescan_calls == [True]

    lw.close()
    app.processEvents()


def test_edge_drawer_commercial_ab_toggle_and_cutoff_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PySide6.QtCore import Qt
    from hex_helper.ui.edge_drawer import EdgeBuildDrawer

    app = _qt_app()
    monkeypatch.setenv("HEX_EDITION", "commercial")

    tiers: list[str] = []
    recharges: list[bool] = []
    logins: list[bool] = []

    drawer = EdgeBuildDrawer(
        on_switch_tier=lambda t: tiers.append(t),
        on_recharge_click=lambda: recharges.append(True),
        on_login_click=lambda: logins.append(True),
    )
    assert drawer.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating) is True
    assert drawer.tier_a_pill is not None and drawer.tier_b_pill is not None
    assert drawer.tier_toggle_wrap.isHidden() is False

    drawer.tier_b_pill.click()
    assert tiers == ["b"]

    # Cutoff due to insufficient credits -> clears shimmer, shows recharge CTA
    drawer.set_coach_online(True, label="● 云端AI · A档")
    drawer.set_coach_pending(True, "推演中...")
    assert drawer.coach_shimmer.is_active() is True

    drawer.set_ai_cutoff(True, reason="余额不足 · AI 已暂停", balance_credits=0.0, code="INSUFFICIENT_CREDITS")
    assert drawer.coach_shimmer.is_active() is False
    assert drawer.cutoff_banner.isHidden() is False
    assert "余额不足" in drawer.cutoff_banner_lbl.text()
    assert "算力" in drawer.cutoff_action_btn.text()
    drawer.cutoff_action_btn.click()
    assert recharges == [True]

    # Cutoff due to NOT_LOGGED_IN -> clickable login CTA
    drawer.set_ai_cutoff(True, reason="请先登录账号", balance_credits=None, code="NOT_LOGGED_IN")
    assert "登录" in drawer.cutoff_action_btn.text()
    drawer.cutoff_action_btn.click()
    assert logins == [True]

    # Unlock after recharge
    drawer.set_ai_cutoff(False, reason="", balance_credits=10000.0)
    assert drawer.cutoff_banner.isHidden() is True

    drawer.close()
    app.processEvents()


# ---------------------------------------------------------------------------
# 4. End-to-End Integration: Real FastAPI Server + Client + Controller + UI
# ---------------------------------------------------------------------------


def test_commercial_e2e_server_ab_grounding_402_cutoff_qr_recharge_and_mirror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hex_helper.cloud.client import CloudCoachError, CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore
    from hex_helper.data.aramhex_sync import AramHexSyncer
    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.ui.account_dialogs import LoginRegisterDialog, RechargeDialog
    from hex_server.routers.orders import settle_order_paid

    app_qt = _qt_app()
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "client_data"))
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "http://cloud.test")

    fastapi_app, gateway = _build_inprocess_server(tmp_path)

    with TestClient(fastapi_app) as test_http:
        db = fastapi_app.state.db

        # A. Verify CN zero-proxy dataset & static mirror sync against /v1/updates/dataset
        def mirror_fetcher(url: str) -> bytes:
            path = url.split("http://cloud.test", 1)[-1]
            r = test_http.get(path)
            r.raise_for_status()
            return r.content

        cache_dir = tmp_path / "sync_cache"
        syncer = AramHexSyncer(
            cache_dir,
            proxy_url="",
            base_url="http://cloud.test/v1/updates/dataset",
            fetcher=mirror_fetcher,
        )
        desc, ds_local_dir = syncer.sync_dataset("latest", "normal")
        assert desc.patch == "16.23"
        assert (ds_local_dir / "heroes.json").is_file()

        static_mgr = StaticAssetsManager(
            cache_dir,
            proxy_url="",
            mirror_base_url="http://cloud.test/v1/updates/dataset",
            fetcher=mirror_fetcher,
        )
        static_mgr.sync_all()
        assert "3084" in static_mgr.items
        assert static_mgr.hero_aliases.get("63") == "Brand"

        # Verify offline fallback to cached static files when mirror raises
        static_offline = StaticAssetsManager(
            cache_dir,
            proxy_url="",
            mirror_base_url="http://cloud.test/v1/updates/dataset",
            fetcher=lambda _u: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        static_offline.sync_all()
        assert "3084" in static_offline.items
        assert static_offline.hero_aliases.get("63") == "Brand"

        # B. Register via CloudGatewayClient -> DPAPI persistence -> session restore -> 401 refresh
        sess_store = DpapiSessionStore(tmp_path / "client_data" / "session.dat")
        client1 = CloudGatewayClient(
            base_url="http://cloud.test",
            session_store=sess_store,
            http_client_factory=lambda _base, _timeout: test_http,
        )
        reg_res = client1.register("CommercialPlayer", "Pass#2026")
        assert reg_res["user"]["username"] == "CommercialPlayer"
        assert (tmp_path / "client_data" / "session.dat").is_file()

        # Restore into fresh client and force access_token expiry/invalidity -> verify auto refresh
        client2 = CloudGatewayClient(
            base_url="http://cloud.test",
            session_store=sess_store,
            http_client_factory=lambda _base, _timeout: test_http,
        )
        restored = client2.restore_session()
        assert restored is not None and restored["username"] == "CommercialPlayer"
        old_refresh = restored["refresh_token"]
        client2._session["access_token"] = "expired.invalid.jwt"  # force 401 on first attempt

        me_data = client2.get_me()
        assert me_data["user"]["username"] == "CommercialPlayer"
        assert client2._session["refresh_token"] != old_refresh  # rotated and persisted

        # Credit user with 10,000 credits (10,000,000 micro_credits) via DB for A/B coach tests
        user_id = int(me_data["user"]["id"])
        with db.write_tx() as conn:
            conn.execute(
                "UPDATE users SET balance_micro_credits = 10000000 WHERE id = ?;",
                (user_id,),
            )

        store = _build_test_store(tmp_path / "store_cache")

        # C. Tier A advise: ground_coach_advice filters already-owned 3084 心之钢 & grounds "大帽" -> 3089
        req_a = CoachRequestPayload(
            request_id="req-tier-a-1",
            trigger_reason="manual_f6",
            timestamp=utc_now_iso(),
            dataset_patch="26.23",
            my_hero={"hero_id": "63", "name": "布兰德", "gold": 3200, "items": ["心之钢"]},
        )
        adv_a, stats_a = client2.advise(req_a, store, tier="a")
        assert stats_a.tier == "a"
        assert adv_a.source == "云端AI · A档极速"
        assert [it.item_id for it in adv_a.next_items] == ["3083", "3089"]
        assert gateway.calls[-1]["tier"] == "a"
        # Tier A billing: 2000 * 2.0 + 500 * 8.0 = 8,000 micro_credits = 8.0 credits
        assert stats_a.cost_credits == 8.0

        # D. Tier B advise at 6-slot full build: ground_coach_advice enforces elixir & blocks fake 7th item!
        req_b = CoachRequestPayload(
            request_id="req-tier-b-1",
            trigger_reason="death_enemy_build_changed",
            timestamp=utc_now_iso(),
            dataset_patch="26.23",
            my_hero={
                "hero_id": "63",
                "name": "布兰德",
                "gold": 1200,
                "items": ["心之钢", "狂徒铠甲", "中娅沙漏", "莫雷洛秘典", "法师之靴", "灭世者的死亡之帽"],
            },
        )
        adv_b, stats_b = client2.advise(req_b, store, tier="b")
        assert stats_b.tier == "b"
        assert adv_b.source == "云端AI · B档旗舰"
        assert gateway.calls[-1]["tier"] == "b"
        assert adv_b.build_path == []
        assert adv_b.next_items[0].item_id in ("2138", "2139")
        # Tier B billing: (2000*1.5 + 1000*7.5) * 0.5 * 7.2 = 37,800 micro_credits = 37.8 credits
        assert stats_b.cost_credits == 37.8

        # E. Drain wallet -> 402 hard cutoff -> QR Recharge -> auto-unlock!
        with db.write_tx() as conn:
            conn.execute("UPDATE users SET balance_micro_credits = 0 WHERE id = ?;", (user_id,))

        with pytest.raises(CloudCoachError) as exc_info:
            client2.advise(req_a, store, tier="a")
        assert exc_info.value.status_code == 402
        assert exc_info.value.code == "INSUFFICIENT_CREDITS"

        # F. Full HexHelperController E2E in commercial mode
        from hex_helper import app as app_mod

        class _NoopCapturer:
            backend = "noop"
            consecutive_black_frames = 0

            def grab(self, **_kw):
                raise RuntimeError("unused")

            def close(self) -> None:
                return None

        from PySide6.QtCore import QAbstractNativeEventFilter

        class _NoopHotkeys(QAbstractNativeEventFilter):
            def nativeEventFilter(self, eventType, message):
                return False, 0

            def register(self, *_a, **_kw) -> None:
                return None

            def unregister_all(self) -> None:
                return None

        monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
        monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
        monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
        monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
        monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)

        ctrl = app_mod.HexHelperController(
            app_qt,
            show_lookup_on_start=False,
            cloud_http_client_factory=lambda _b, _t: test_http,
        )
        try:
            assert ctrl.bridge is None
            assert ctrl.gemini_coach is None
            assert "google.genai" not in sys.modules

            ctrl.store = store
            ctrl.sm.current_hero_id = "63"

            # Wait for startup wallet poll to reflect 0 credits -> cutoff active
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not ctrl._cloud_ai_cutoff:
                app_qt.processEvents()
                time.sleep(0.02)

            assert ctrl._cloud_ai_cutoff is True
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is False
            assert ctrl.lookup_window.cutoff_banner.isHidden() is False

            # Attempting F6 while cut off MUST NOT send any request to /v1/coach/advise
            calls_before = len(gateway.calls)
            ctrl.trigger_coach_request("manual_f6")
            app_qt.processEvents()
            assert len(gateway.calls) == calls_before

            # Open RechargeDialog -> create order -> fetch QR -> admin settles -> poll auto-unlocks!
            paid_events: list[dict[str, Any]] = []
            dlg = RechargeDialog(
                ctrl.cloud_client,
                on_paid=lambda data: (paid_events.append(data), ctrl._on_recharge_paid(data)),
            )
            order = ctrl.cloud_client.create_order("pkg_10", "manual_wechat")
            assert order["status"] == "pending"
            assert order["qr_image_url"] == "/v1/payments/qr-image/manual_wechat"
            qr_bytes = ctrl.cloud_client.fetch_qr_image_bytes(order["qr_image_url"])
            assert qr_bytes.startswith(b"\x89PNG")

            dlg.show_created_order(order, qr_bytes)
            assert order["memo_code"] in dlg.order_info_lbl.text()

            settle_order_paid(db, order["order_no"], "wx-e2e-999", "admin")
            dlg.poll_once_sync()
            app_qt.processEvents()

            assert len(paid_events) == 1
            assert ctrl._cloud_ai_cutoff is False
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is True
            assert ctrl.lookup_window.cutoff_banner.isHidden() is True
            assert "10,000" in ctrl.lookup_window.account_balance_lbl.text()

            dlg.close()

            # Now F6 cloud coach works end-to-end and switches tier cleanly!
            ctrl._on_switch_tier("b")
            assert ctrl.cfg.coach_tier == "b"
            ctrl.trigger_coach_request("manual_f6")
            deadline2 = time.monotonic() + 3.0
            while time.monotonic() < deadline2 and ctrl._latest_advice is None:
                app_qt.processEvents()
                time.sleep(0.02)
            assert ctrl._latest_advice is not None
            assert ctrl._latest_advice.source == "云端AI · B档旗舰"

            # Also verify live 402 cutoff inside CloudCoachWorker when balance drains right before F6
            with db.write_tx() as conn:
                conn.execute("UPDATE users SET balance_micro_credits = 0 WHERE id = ?;", (user_id,))
            assert ctrl._cloud_ai_cutoff is False
            ctrl.trigger_coach_request("manual_f6")
            deadline3 = time.monotonic() + 3.0
            while time.monotonic() < deadline3 and not ctrl._cloud_ai_cutoff:
                app_qt.processEvents()
                time.sleep(0.02)
            assert ctrl._cloud_ai_cutoff is True
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is False
            assert ctrl.lookup_window.cutoff_banner.isHidden() is False

            # Verify RechargeDialog fallback QR rendering from qr_payload when no PNG uploaded
            dlg_payload = RechargeDialog(ctrl.cloud_client, auto_load=False)
            dlg_payload.show_created_order(
                {
                    "order_no": "ORD-20261122-DEADBEEF",
                    "pay_amount_yuan": "5.00",
                    "credits_amount": 5000,
                    "memo_code": "7K9P",
                    "channel_label": "支付宝扫码支付",
                    "qr_payload": "https://qr.alipay.com/fkx-test",
                },
                qr_bytes=None,
            )
            assert dlg_payload.qr_image_lbl.pixmap() is not None
            assert not dlg_payload.qr_image_lbl.pixmap().isNull()
            dlg_payload.close()

            # Verify commercial diagnostics have zero proxy / API Key / 18421 mentions
            ctrl._refresh_diagnostics()
            diag_txt = ctrl.lookup_window.diag_text.toPlainText()
            for bad_tok in ("代理", "Gemini API", "18421", "AIza"):
                assert bad_tok not in diag_txt

            # 60s wallet heartbeat detects admin re-enabling / draining account
            with db.write_tx() as conn:
                conn.execute("UPDATE users SET balance_micro_credits = 5000000 WHERE id = ?;", (user_id,))
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._cloud_ai_cutoff is False
            with db.write_tx() as conn:
                conn.execute("UPDATE users SET balance_micro_credits = 0 WHERE id = ?;", (user_id,))
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._cloud_ai_cutoff is True
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is False

            # LoginRegisterDialog smoke test
            login_dlg = LoginRegisterDialog(ctrl.cloud_client)
            login_dlg.set_mode("register")
            assert login_dlg.confirm_password_edit.isHidden() is False
            login_dlg.set_mode("login")
            assert login_dlg.confirm_password_edit.isHidden() is True
            login_dlg.close()
        finally:
            ctrl.shutdown()
            app_qt.processEvents()


# ---------------------------------------------------------------------------
# 5. Focused Review Fix Tests (lock_all, logout/refresh race, loading, etc.)
# ---------------------------------------------------------------------------


def _patch_controller_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtCore import QAbstractNativeEventFilter
    from hex_helper import app as app_mod

    class _NoopCapturer:
        backend = "noop"
        consecutive_black_frames = 0

        def grab(self, **_kw):
            raise RuntimeError("unused")

        def close(self) -> None:
            return None

    class _NoopHotkeys(QAbstractNativeEventFilter):
        def nativeEventFilter(self, eventType, message):
            return False, 0

        def register(self, *_a, **_kw) -> None:
            return None

        def unregister_all(self) -> None:
            return None

    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)


def test_cutoff_policy_lock_all_vs_ai_only_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hex_helper import app as app_mod
    from hex_helper import edition as edition_mod
    from hex_helper.cloud import client as cloud_client_mod
    from hex_helper.cloud.client import CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore
    from hex_helper.game.models import ChampSelectState
    from hex_helper.vision.detector import DetectedCard

    app_qt = _qt_app()
    monkeypatch.setattr(edition_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(app_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(cloud_client_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "client_data"))
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "http://cloud.test")
    _patch_controller_env(monkeypatch)

    fastapi_app, _ = _build_inprocess_server(tmp_path)
    store = _build_test_store(tmp_path / "store_cache")

    with TestClient(fastapi_app) as test_http:
        db = fastapi_app.state.db
        db.set_setting("ai_only_cutoff", False)  # policy = lock_all

        sess_store = DpapiSessionStore(tmp_path / "client_data" / "session.dat")
        boot_client = CloudGatewayClient(
            base_url="http://cloud.test",
            session_store=sess_store,
            http_client_factory=lambda _b, _t: test_http,
        )
        reg = boot_client.register("LockPolicyUser", "Pass#2026")
        user_id = int(reg["user"]["id"])
        with db.write_tx() as conn:
            conn.execute("UPDATE users SET balance_micro_credits = 0 WHERE id = ?;", (user_id,))

        ctrl = app_mod.HexHelperController(
            app_qt,
            show_lookup_on_start=False,
            cloud_http_client_factory=lambda _b, _t: test_http,
        )
        try:
            ctrl.store = store
            ctrl.sm.current_hero_id = "63"

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not (
                ctrl._cloud_ai_cutoff and ctrl._cloud_cutoff_policy == "lock_all"
            ):
                app_qt.processEvents()
                time.sleep(0.02)

            assert ctrl._cloud_ai_cutoff is True
            assert ctrl._cloud_cutoff_policy == "lock_all"

            # LookupWindow Pages 0 & 1 must be locked/hidden and Page 2 focused
            assert ctrl.lookup_window.nav_btns[0].isEnabled() is False
            assert ctrl.lookup_window.nav_btns[1].isEnabled() is False
            assert ctrl.lookup_window.hero_browser_body.isHidden() is True
            assert ctrl.lookup_window.hero_locked_card.isHidden() is False
            assert ctrl.lookup_window.special_builds_body.isHidden() is True
            assert ctrl.lookup_window.special_locked_card.isHidden() is False
            assert ctrl.lookup_window.pages.currentIndex() == 2

            # EdgeBuildDrawer hides local build cards while keeping recharge CTA clickable
            assert ctrl.edge_drawer.next_buy_card.isHidden() is True
            assert ctrl.edge_drawer.roadmap_card.isHidden() is True
            assert ctrl.edge_drawer.aug_frame.isHidden() is True
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is False
            assert ctrl.edge_drawer.cutoff_action_btn.isEnabled() is True

            # ChampSelectOverlay & AugmentBadgesOverlay MUST stay hidden under lock_all
            cs = ChampSelectState(450, "KIWI", True, "63", ("63",), ())
            ctrl._apply_lcu_poll("ChampSelect", cs)
            assert ctrl.champ_overlay.isVisible() is False

            cards = (
                DetectedCard(0, store.get_augment("3163"), 0.95, (100, 200, 320, 240), "gold"),
            )
            ctrl.sm.state = app_mod.GameState.AUGMENT_PICKING
            ctrl.sm.active_cards = cards
            ctrl._render_augment_badges(cards)
            assert ctrl.augment_overlay.isVisible() is False

            # Switch server policy to ai_only (still 0 credits) -> local features unlock immediately
            db.set_setting("ai_only_cutoff", True)
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._cloud_ai_cutoff is True
            assert ctrl._cloud_cutoff_policy == "ai_only"
            assert ctrl.lookup_window.nav_btns[0].isEnabled() is True
            assert ctrl.lookup_window.nav_btns[1].isEnabled() is True
            assert ctrl.lookup_window.hero_browser_body.isHidden() is False
            assert ctrl.lookup_window.hero_locked_card.isHidden() is True
            assert ctrl.lookup_window.special_builds_body.isHidden() is False
            assert ctrl.edge_drawer.next_buy_card.isHidden() is False
            assert ctrl.edge_drawer.roadmap_card.isHidden() is False
            assert ctrl.edge_drawer.aug_frame.isHidden() is False
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is False
            assert ctrl.augment_overlay.isVisible() is True

            ctrl._apply_lcu_poll("ChampSelect", cs)
            assert ctrl.champ_overlay.isVisible() is True

            # Re-enable lock_all, then top up credits -> balance recovery unlocks everything
            db.set_setting("ai_only_cutoff", False)
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl.lookup_window.hero_locked_card.isHidden() is False
            assert ctrl.champ_overlay.isVisible() is False

            with db.write_tx() as conn:
                conn.execute("UPDATE users SET balance_micro_credits = 5000000 WHERE id = ?;", (user_id,))
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._cloud_ai_cutoff is False
            assert ctrl.lookup_window.hero_browser_body.isHidden() is False
            assert ctrl.lookup_window.cutoff_banner.isHidden() is True
            assert ctrl.edge_drawer.next_buy_card.isHidden() is False
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is True
            ctrl._apply_lcu_poll("ChampSelect", cs)
            assert ctrl.champ_overlay.isVisible() is True
        finally:
            ctrl.shutdown()
            app_qt.processEvents()


def test_concurrent_logout_vs_refresh_never_resurrects_session_dat(tmp_path: Path) -> None:
    import threading
    import httpx
    from hex_helper.cloud.client import CloudCoachError, CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore

    session_file = tmp_path / "session.dat"
    sess_store = DpapiSessionStore(session_file)
    sess_store.save(
        {
            "access_token": "expired.access",
            "refresh_token": "valid.refresh",
            "username": "RaceUser",
            "device_id": "win-race",
        }
    )

    refresh_started = threading.Event()
    allow_refresh_finish = threading.Event()

    class _RaceHttpClient:
        def request(self, method: str, path: str, **_kwargs):
            req = httpx.Request(method, f"http://cloud.test{path}")
            if path == "/v1/auth/me":
                return httpx.Response(401, request=req, json={"detail": "expired"})
            if path == "/v1/auth/refresh":
                refresh_started.set()
                allow_refresh_finish.wait(timeout=3.0)
                return httpx.Response(
                    200,
                    request=req,
                    json={
                        "access_token": "new.access.token",
                        "refresh_token": "new.refresh.token",
                        "user": {"username": "RaceUser", "balance_credits": 1000.0},
                    },
                )
            if path == "/v1/auth/logout":
                return httpx.Response(200, request=req, json={"ok": True})
            raise AssertionError(f"Unexpected path {path}")

        def close(self) -> None:
            return None

    client = CloudGatewayClient(
        base_url="http://cloud.test",
        session_store=sess_store,
        http_client_factory=lambda _b, _t: _RaceHttpClient(),
    )
    client.restore_session()

    errors: list[Exception] = []

    def worker() -> None:
        try:
            client.get_me()
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    assert refresh_started.wait(timeout=2.0)

    # Logout happens while _refresh_locked HTTP POST is mid-flight
    client.logout()
    assert client.is_logged_in is False
    assert not session_file.exists()

    allow_refresh_finish.set()
    t.join(timeout=2.0)

    assert len(errors) == 1
    assert isinstance(errors[0], CloudCoachError)
    assert errors[0].code == "NOT_LOGGED_IN"
    assert client.is_logged_in is False
    assert not session_file.exists()
    assert sess_store.load() is None


def test_restored_session_wallet_loading_state_before_me_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from hex_helper import app as app_mod
    from hex_helper.cloud.client import CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore

    app_qt = _qt_app()
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "client_data"))
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "http://cloud.test")
    _patch_controller_env(monkeypatch)

    fastapi_app, _ = _build_inprocess_server(tmp_path)

    with TestClient(fastapi_app) as test_http:
        db = fastapi_app.state.db
        sess_store = DpapiSessionStore(tmp_path / "client_data" / "session.dat")
        boot_client = CloudGatewayClient(
            base_url="http://cloud.test",
            session_store=sess_store,
            http_client_factory=lambda _b, _t: test_http,
        )
        reg = boot_client.register("LoadingUser", "Pass#2026")
        with db.write_tx() as conn:
            conn.execute(
                "UPDATE users SET balance_micro_credits = 10000000 WHERE id = ?;",
                (int(reg["user"]["id"]),),
            )

        allow_me = threading.Event()
        me_entered = threading.Event()

        class _GatedHttpClient:
            def request(self, method: str, path: str, **kwargs):
                if path == "/v1/auth/me":
                    me_entered.set()
                    allow_me.wait(timeout=3.0)
                return test_http.request(method, path, **kwargs)

            def close(self) -> None:
                return None

        ctrl = app_mod.HexHelperController(
            app_qt,
            show_lookup_on_start=False,
            cloud_http_client_factory=lambda _b, _t: _GatedHttpClient(),
        )
        try:
            assert me_entered.wait(timeout=2.0)
            app_qt.processEvents()
            assert ctrl.cloud_client.is_logged_in is True
            assert ctrl.cloud_client.last_wallet is None

            assert "同步中" in ctrl.lookup_window.account_balance_lbl.text()
            assert "0 积分" not in ctrl.lookup_window.account_balance_lbl.text()
            assert "同步中" in ctrl.lookup_window.coach_badge_lbl.text()
            assert "已就绪" not in ctrl.lookup_window.coach_page_status.text()
            assert "连接中" in ctrl.edge_drawer.coach_status_lbl.text()
            assert "同步中" in ctrl.edge_drawer.coach_balance_lbl.text()
            assert ctrl.edge_drawer.coach_balance_lbl.isHidden() is False

            allow_me.set()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and ctrl.cloud_client.last_wallet is None:
                app_qt.processEvents()
                time.sleep(0.02)
            app_qt.processEvents()
            assert "10,000" in ctrl.lookup_window.account_balance_lbl.text()
            assert "已就绪" in ctrl.lookup_window.coach_page_status.text()
        finally:
            allow_me.set()
            ctrl.shutdown()
            app_qt.processEvents()


def test_edge_drawer_set_coach_advice_none_preserves_cutoff_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hex_helper.ui.edge_drawer import EdgeBuildDrawer

    app_qt = _qt_app()
    monkeypatch.setenv("HEX_EDITION", "commercial")
    drawer = EdgeBuildDrawer()
    try:
        drawer.set_coach_online(True, label="● 云端 · A档极速")
        drawer.set_ai_cutoff(
            True,
            reason="余额不足 · AI 已暂停",
            balance_credits=0.0,
            code="INSUFFICIENT_CREDITS",
        )
        assert "余额不足" in drawer.coach_status_lbl.text()
        assert drawer.cutoff_banner.isHidden() is False

        # Clearing advice on match/augment reset MUST keep dormant cutoff status intact
        drawer.set_coach_advice(None)
        assert drawer._ai_cutoff is True
        assert "余额不足" in drawer.coach_status_lbl.text()
        assert "#C8AA6E" in drawer.coach_status_lbl.styleSheet()
        assert drawer.cutoff_banner.isHidden() is False
        assert "本地 5ms 引擎" in drawer.coach_content_lbl.text()
    finally:
        drawer.close()
        app_qt.processEvents()


def test_non_json_502_html_response_raises_cloud_coach_error(tmp_path: Path) -> None:
    import httpx
    from hex_helper.cloud.client import CloudCoachError, CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore

    class _Html502Client:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self.body = body

        def request(self, method: str, path: str, **_kwargs):
            req = httpx.Request(method, f"http://cloud.test{path}")
            return httpx.Response(
                self.status,
                request=req,
                content=self.body,
                headers={"content-type": "text/html"},
            )

        def close(self) -> None:
            return None

    sess_store = DpapiSessionStore(tmp_path / "session.dat")
    sess_store.save(
        {
            "access_token": "tok",
            "refresh_token": "ref",
            "username": "U",
            "device_id": "d",
        }
    )

    store = _build_test_store(tmp_path / "cache")
    req_obj = CoachRequestPayload(
        request_id="req-502",
        trigger_reason="manual_f6",
        timestamp=utc_now_iso(),
        dataset_patch="26.23",
        my_hero={"hero_id": "63", "name": "布兰德", "gold": 1000, "items": []},
    )

    client_502 = CloudGatewayClient(
        base_url="http://cloud.test",
        session_store=sess_store,
        http_client_factory=lambda _b, _t: _Html502Client(502, b"<html>502 Bad Gateway</html>"),
    )
    client_502.restore_session()
    with pytest.raises(CloudCoachError) as exc1:
        client_502.advise(req_obj, store, tier="a")
    assert exc1.value.status_code == 502

    client_200_html = CloudGatewayClient(
        base_url="http://cloud.test",
        session_store=sess_store,
        http_client_factory=lambda _b, _t: _Html502Client(200, b"<html>Captive Portal</html>"),
    )
    client_200_html.restore_session()
    for fn in (
        lambda: client_200_html.get_me(),
        lambda: client_200_html.get_packages(),
        lambda: client_200_html.create_order("pkg_10", "manual_wechat"),
        lambda: client_200_html.poll_order("ORD-1"),
        lambda: client_200_html.advise(req_obj, store, tier="a"),
        lambda: client_200_html.check_update_manifest("1.0.0"),
    ):
        with pytest.raises(CloudCoachError) as exc2:
            fn()
        assert exc2.value.code == "INVALID_RESPONSE"


def test_recharge_dialog_empty_channels_stale_poll_and_lookup_settings_safety(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hex_helper.cloud.client import CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore
    from hex_helper.config import AppConfig
    from hex_helper.ui.account_dialogs import RechargeDialog
    from hex_helper.ui.lookup_window import LookupWindow

    app_qt = _qt_app()
    monkeypatch.setenv("HEX_EDITION", "commercial")

    client = CloudGatewayClient(
        base_url="http://cloud.test",
        session_store=DpapiSessionStore(tmp_path / "session.dat"),
    )
    paid_calls: list[dict[str, Any]] = []
    dlg = RechargeDialog(client, auto_load=False, on_paid=lambda d: paid_calls.append(d))
    try:
        # Empty channels disables radios + create_order_btn with explicit hint
        dlg._populate_packages(
            {
                "packages": [{"id": "pkg_10", "label": "10元", "amount_fen": 1000, "credits": 10000}],
                "channels": [],
            }
        )
        assert dlg.wechat_radio.isEnabled() is False
        assert dlg.alipay_radio.isEnabled() is False
        assert dlg.create_order_btn.isEnabled() is False
        assert "暂未开启" in dlg.status_lbl.text()

        # Stale poll result for an old order must be ignored when _current_order_no changed
        dlg.show_created_order(
            {
                "order_no": "ORD-NEW-002",
                "pay_amount_yuan": "10.00",
                "credits_amount": 10000,
                "memo_code": "AB12",
                "qr_payload": "wxp://new",
            }
        )
        dlg._handle_poll_result(
            {"order_no": "ORD-OLD-001", "status": "paid", "credits_amount": 10000}
        )
        assert paid_calls == []
        assert dlg._paid_notified is False
    finally:
        dlg.close()

    # Preserve coach_mode == "off" in _collect_cfg_from_ui & reject invalid browse directory
    import threading
    from hex_helper.game.lol_locator import LolInstallInfo

    saved_cfgs: list[AppConfig] = []
    cfg = AppConfig(coach_mode="off", lockfile_path=r"D:\Games\LoL\LeagueClient\lockfile")
    lw = LookupWindow(cfg=cfg, on_save_settings=lambda c: saved_cfgs.append(c))
    try:
        collected = lw._collect_cfg_from_ui()
        assert collected.coach_mode == "off"

        bad_dir = tmp_path / "not_lol_folder"
        bad_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "hex_helper.ui.lookup_window.QFileDialog.getExistingDirectory",
            lambda *_a, **_kw: str(bad_dir),
        )
        lw._browse_lockfile()
        assert lw.lockfile_edit.text() == r"D:\Games\LoL\LeagueClient\lockfile"
        assert "未检测到" in lw.lol_detect_status_lbl.text()

        # Valid browse persists config immediately
        valid_lol = tmp_path / "HeroLeague"
        (valid_lol / "LeagueClient").mkdir(parents=True, exist_ok=True)
        (valid_lol / "LeagueClient" / "LeagueClient.exe").write_bytes(b"MZ")
        monkeypatch.setattr(
            "hex_helper.ui.lookup_window.QFileDialog.getExistingDirectory",
            lambda *_a, **_kw: str(valid_lol),
        )
        lw._browse_lockfile()
        assert len(saved_cfgs) == 1
        assert str(valid_lol / "LeagueClient" / "lockfile") == lw.lockfile_edit.text()

        # Async rescan runs off main thread and re-enables button on UI thread
        scan_threads: list[threading.Thread] = []
        scan_done = threading.Event()

        def _fake_detect(*_a, **_kw):
            scan_threads.append(threading.current_thread())
            scan_done.set()
            return LolInstallInfo(
                install_dir=valid_lol,
                lockfile_path=valid_lol / "LeagueClient" / "lockfile",
                source="manual_test",
            )

        monkeypatch.setattr("hex_helper.ui.lookup_window.detect_primary_lol_install", _fake_detect)
        lw._rescan_lol_clicked()
        assert lw.rescan_lol_btn.isEnabled() is False or scan_done.is_set()
        assert scan_done.wait(timeout=2.0)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not lw.rescan_lol_btn.isEnabled():
            app_qt.processEvents()
            time.sleep(0.01)
        assert lw.rescan_lol_btn.isEnabled() is True
        assert scan_threads and scan_threads[0] is not threading.main_thread()
    finally:
        lw.close()
        app_qt.processEvents()


def test_modal_parent_minimized_retried_401_clear_and_cutoff_clears_augment_shimmer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from hex_helper import app as app_mod
    from hex_helper.cloud.client import CloudCoachError, CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore
    from hex_helper.vision.detector import DetectedCard

    app_qt = _qt_app()
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "client_data"))
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "http://cloud.test")
    _patch_controller_env(monkeypatch)

    # Verify retried 401 on order endpoint clears local DPAPI session cleanly
    sess_file = tmp_path / "order_401_session.dat"
    sess_store = DpapiSessionStore(sess_file)
    sess_store.save(
        {
            "access_token": "tok",
            "refresh_token": "ref",
            "username": "OrderUser",
            "device_id": "d",
        }
    )

    class _Always401Client:
        def request(self, method: str, path: str, **_kwargs):
            req = httpx.Request(method, f"http://cloud.test{path}")
            if path == "/v1/auth/refresh":
                return httpx.Response(
                    200,
                    request=req,
                    json={"access_token": "new.tok", "refresh_token": "new.ref"},
                )
            return httpx.Response(401, request=req, json={"detail": "revoked"})

        def close(self) -> None:
            return None

    client = CloudGatewayClient(
        base_url="http://cloud.test",
        session_store=sess_store,
        http_client_factory=lambda _b, _t: _Always401Client(),
    )
    client.restore_session()
    with pytest.raises(CloudCoachError) as exc_401:
        client.create_order("pkg_10", "manual_wechat")
    assert exc_401.value.status_code == 401
    assert client.is_logged_in is False
    assert not sess_file.exists()

    # Verify modal parent is None when LookupWindow is minimized/hidden & cutoff clears augment shimmer
    fastapi_app, _ = _build_inprocess_server(tmp_path)
    store = _build_test_store(tmp_path / "store_cache")
    with TestClient(fastapi_app) as test_http:
        ctrl = app_mod.HexHelperController(
            app_qt,
            show_lookup_on_start=False,
            cloud_http_client_factory=lambda _b, _t: test_http,
        )
        try:
            ctrl.store = store
            ctrl.sm.current_hero_id = "63"
            assert ctrl._modal_dialog_parent() is None

            ctrl.lookup_window.showMinimized()
            app_qt.processEvents()
            assert ctrl._modal_dialog_parent() is None

            ctrl.sm.state = app_mod.GameState.AUGMENT_PICKING
            ctrl.sm.active_cards = (
                DetectedCard(0, store.get_augment("3163"), 0.95, (100, 200, 320, 240), "gold"),
            )
            ctrl.edge_drawer._coach_online = True
            ctrl.edge_drawer.set_coach_pending(True, "推演中")
            ctrl._render_augment_badges(ctrl.sm.active_cards)
            assert ctrl.augment_overlay._coach_banner_shimmer is not None
            assert ctrl.augment_overlay._coach_banner_shimmer.is_active() is True

            ctrl._on_cloud_cutoff("INSUFFICIENT_CREDITS", "余额不足 · AI 已暂停", {"cutoff_policy": "ai_only"})
            assert ctrl.augment_overlay._coach_banner_shimmer is None
            assert ctrl.augment_overlay._coach_banner is None
        finally:
            ctrl.shutdown()
            app_qt.processEvents()


# ---------------------------------------------------------------------------
# 6. Task 10: SHA-256 Self-Updater, apply_update.bat, UpdatePromptDialog & Live Uvicorn E2E
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_live_uvicorn(fastapi_app) -> tuple[str, Any, Any]:
    import threading
    import httpx
    import uvicorn

    port = _find_free_port()
    config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="HexUvicornE2E", daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 8.0
    with httpx.Client(proxy=None, trust_env=False, timeout=2.0) as probe:
        while time.monotonic() < deadline:
            if not thread.is_alive():
                raise RuntimeError("Live uvicorn server exited prematurely during startup")
            try:
                resp = probe.get(f"{base_url}/health")
                if resp.status_code == 200:
                    return base_url, server, thread
            except Exception:
                pass
            time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=3.0)
    raise TimeoutError(f"Live uvicorn server did not become healthy on {base_url}")


def _make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in entries.items():
            zf.writestr(arcname, content)
    return buf.getvalue()


def test_updater_sha256_mismatch_zip_slip_and_apply_update_bat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import zipfile
    from hex_helper.cloud.updater import (
        UpdateInfo,
        UpdateVerificationError,
        download_and_verify_update,
        launch_update_installer,
    )

    dest_dir = tmp_path / "downloads"
    dest_dir.mkdir()

    # 1. Empty expected sha256 must raise UpdateVerificationError before writing anything
    empty_info = UpdateInfo(
        current_version="1.0.0",
        latest_version="1.0.1",
        update_available=True,
        force_update=False,
        min_supported_version="1.0.0",
        download_url="/v1/updates/download/1.0.1",
        sha256="",
        file_size=128,
        changelog="test",
    )
    with pytest.raises(UpdateVerificationError):
        download_and_verify_update(empty_info, dest_dir, base_url="http://127.0.0.1:9")
    assert list(dest_dir.iterdir()) == []

    # 2. Zip Slip rejection during launch_update_installer
    evil_zip = tmp_path / "evil_slip.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../escaped_pwned.txt", b"evil")
    app_dir_slip = tmp_path / "app_slip"
    app_dir_slip.mkdir()
    with pytest.raises(UpdateVerificationError):
        launch_update_installer(evil_zip, app_dir_slip, current_pid=0)
    assert not (tmp_path / "escaped_pwned.txt").exists()

    # 3. .exe silent installer invocation matches packaging/installer_gui.py flags
    fake_setup = tmp_path / "HexHelper_Setup_v1.0.1_win64.exe"
    fake_setup.write_bytes(b"MZ")
    spawned: list[tuple[list[str], dict[str, Any]]] = []

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        spawned.append((list(cmd), dict(kwargs)))
        return _FakeProc()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("hex_helper.cloud.updater.subprocess.Popen", _fake_popen)
        launch_update_installer(fake_setup, app_dir_slip, current_pid=999999)
    assert len(spawned) == 1
    assert spawned[0][0] == [
        str(fake_setup),
        "--silent",
        "--install-dir",
        str(app_dir_slip),
    ]

    # 4. Real apply_update.bat execution via cmd.exe when BOTH TEMP and app_dir have spaces:
    #    - Preserves top-level unrelated files (safe on Desktop/Downloads)
    #    - Mirrors _internal/ cleanly (purging stale _internal/old_stale.dll)
    #    - Waits for a real short-lived process PID to exit & cleans up staging + temp .bat
    import tempfile

    spaced_temp = tmp_path / "User Temp Spaces"
    spaced_temp.mkdir(parents=True)
    spaced_app_dir = tmp_path / "Program Files" / "Hex Helper App"
    spaced_app_dir.mkdir(parents=True)
    (spaced_app_dir / "user_desktop_note.txt").write_text("keep-me", encoding="utf-8")
    (spaced_app_dir / "uninstall_hexhelper.cmd").write_text("@echo uninstall", encoding="utf-8")
    (spaced_app_dir / "_internal").mkdir(parents=True)
    (spaced_app_dir / "_internal" / "old_stale.dll").write_bytes(b"stale-dll")

    update_zip = tmp_path / "HexHelper_v1.0.1_win64_portable.zip"
    update_zip.write_bytes(
        _make_zip_bytes(
            {
                "version.txt": b"1.0.1",
                "_internal/payload.dat": b"updated-binary-payload",
            }
        )
    )

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TEMP", str(spaced_temp))
            mp.setenv("TMP", str(spaced_temp))
            mp.setattr(tempfile, "tempdir", str(spaced_temp))
            launch_update_installer(update_zip, spaced_app_dir, current_pid=sleeper.pid)
        sleeper.wait(timeout=5.0)
    finally:
        if sleeper.poll() is None:
            sleeper.kill()

    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        if (
            (spaced_app_dir / "version.txt").is_file()
            and (spaced_app_dir / "_internal" / "payload.dat").is_file()
            and not (spaced_app_dir / "_internal" / "old_stale.dll").exists()
            and list(spaced_temp.iterdir()) == []
        ):
            break
        time.sleep(0.1)

    assert (spaced_app_dir / "version.txt").read_text(encoding="utf-8") == "1.0.1"
    assert (spaced_app_dir / "_internal" / "payload.dat").read_bytes() == b"updated-binary-payload"
    assert (spaced_app_dir / "user_desktop_note.txt").read_text(encoding="utf-8") == "keep-me"
    assert (spaced_app_dir / "uninstall_hexhelper.cmd").is_file()
    assert not (spaced_app_dir / "_internal" / "old_stale.dll").exists()
    assert list(spaced_temp.iterdir()) == []

    # 5. apply_update.bat cleans up staging & self-deletes even when robocopy fails (RC >= 8)
    fail_stage = spaced_temp / "fail_stage"
    fail_stage.mkdir(parents=True)
    (fail_stage / "x.txt").write_text("x", encoding="utf-8")
    blocked_app_file = tmp_path / "app_is_actually_a_file"
    blocked_app_file.write_text("cannot-mkdir-here", encoding="utf-8")
    fail_bat = spaced_temp / "fail_apply.bat"
    src_bat = Path(__file__).resolve().parents[1] / "packaging" / "apply_update.bat"
    fail_bat.write_bytes(src_bat.read_bytes())
    fail_rc = subprocess.call(
        ["cmd.exe", "/c", "call", str(fail_bat), "0", str(fail_stage), str(blocked_app_file)]
    )
    assert fail_rc >= 8
    assert not fail_stage.exists()
    assert not fail_bat.exists()

    # 6. progress_cb throttling (>= 256 KB or >= 1% or final byte) avoids per-chunk Qt signal spam
    import hex_helper.cloud.updater as updater_mod

    payload_320k = b"X" * (320 * 1024)
    chunks_1k = [payload_320k[i : i + 1024] for i in range(0, len(payload_320k), 1024)]

    class _ChunkyStreamResp:
        status_code = 200
        url = "http://cloud.test/v1/updates/download/1.0.2"
        headers = {"content-disposition": 'attachment; filename="HexHelper_1.0.2.zip"'}

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def iter_bytes(self, chunk_size: int = 65536):
            yield from chunks_1k

    class _ChunkyClient:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def stream(self, _method: str, _url: str):
            return _ChunkyStreamResp()

    throttle_events: list[tuple[int, int]] = []
    chunky_info = UpdateInfo(
        current_version="1.0.0",
        latest_version="1.0.2",
        update_available=True,
        force_update=False,
        min_supported_version="1.0.0",
        download_url="/v1/updates/download/1.0.2",
        sha256=hashlib.sha256(payload_320k).hexdigest(),
        file_size=0,  # unknown content-length forces byte-threshold throttling path
        changelog="throttle test",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(updater_mod, "_new_zero_proxy_client", lambda *_a, **_kw: _ChunkyClient())
        out_art = download_and_verify_update(
            chunky_info,
            tmp_path / "throttle_dl",
            base_url="http://cloud.test",
            progress_cb=lambda got, tot: throttle_events.append((got, tot)),
        )
    assert out_art.read_bytes() == payload_320k
    assert len(throttle_events) <= 4
    assert throttle_events[-1] == (len(payload_320k), len(payload_320k))

    # 7. Wrapped zip root (HexHelper_v1.0.2/HexHelper.exe) unwraps cleanly into staging root
    from hex_helper.cloud.updater import _unwrap_single_root_if_needed

    wrapped_stage = tmp_path / "wrapped_stage"
    (wrapped_stage / "HexHelper_v1.0.2" / "_internal").mkdir(parents=True)
    (wrapped_stage / "HexHelper_v1.0.2" / "HexHelper.exe").write_bytes(b"MZ")
    (wrapped_stage / "HexHelper_v1.0.2" / "_internal" / "a.bin").write_bytes(b"123")
    _unwrap_single_root_if_needed(wrapped_stage)
    assert (wrapped_stage / "HexHelper.exe").is_file()
    assert (wrapped_stage / "_internal" / "a.bin").is_file()
    assert not (wrapped_stage / "HexHelper_v1.0.2").exists()


def test_update_prompt_dialog_force_and_sha_error_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hex_helper.cloud.updater import UpdateInfo, UpdateVerificationError
    from hex_helper.ui.account_dialogs import UpdatePromptDialog

    app_qt = _qt_app()

    opt_info = UpdateInfo(
        current_version="1.0.0",
        latest_version="1.0.1",
        update_available=True,
        force_update=False,
        min_supported_version="1.0.0",
        download_url="/v1/updates/download/1.0.1",
        sha256="a" * 64,
        file_size=1024 * 1024,
        changelog="优化自更新流程\n修复若干问题",
    )
    dlg_opt = UpdatePromptDialog(opt_info, "http://127.0.0.1:18480", tmp_path / "dl")
    try:
        assert "1.0.0" in dlg_opt.version_lbl.text()
        assert "1.0.1" in dlg_opt.version_lbl.text()
        assert "优化自更新流程" in dlg_opt.changelog_lbl.text()
        assert dlg_opt.later_btn.isHidden() is False
        assert dlg_opt.later_btn.isEnabled() is True

        def _fail_download(*_a, **_kw):
            raise UpdateVerificationError("SHA-256 校验失败：文件已损坏")

        monkeypatch.setattr("hex_helper.ui.account_dialogs.download_and_verify_update", _fail_download)
        dlg_opt.download_btn.click()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not dlg_opt.download_btn.isEnabled():
            app_qt.processEvents()
            time.sleep(0.02)
        app_qt.processEvents()
        assert "SHA-256" in dlg_opt.status_lbl.text()
        assert dlg_opt.downloaded_artifact_path is None
    finally:
        dlg_opt.close()
        app_qt.processEvents()

    force_info = UpdateInfo(
        current_version="1.0.0",
        latest_version="1.1.0",
        update_available=True,
        force_update=True,
        min_supported_version="1.1.0",
        download_url="/v1/updates/download/1.1.0",
        sha256="b" * 64,
        file_size=2048,
        changelog="强制安全更新",
    )
    dlg_force = UpdatePromptDialog(force_info, "http://127.0.0.1:18480", tmp_path / "dl2")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        assert dlg_force.later_btn.isHidden() is True
        assert dlg_force.later_btn.isEnabled() is False
        assert dlg_force.exit_app_btn.isHidden() is False
        assert dlg_force.exit_app_btn.isEnabled() is True
        assert dlg_force.force_exit_requested is False

        dlg_force.show()
        app_qt.processEvents()

        # Esc & [X] MUST NOT close or dismiss a forced update dialog
        esc_ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        app_qt.sendEvent(dlg_force, esc_ev)
        dlg_force.reject()
        assert dlg_force.close() is False
        app_qt.processEvents()
        assert dlg_force.isVisible() is True

        # Explicit "退出程序" button sets force_exit_requested and closes cleanly
        dlg_force.exit_app_btn.click()
        app_qt.processEvents()
        assert dlg_force.force_exit_requested is True
        assert dlg_force.isVisible() is False
    finally:
        if dlg_force.isVisible():
            dlg_force.exit_app_btn.click()
        app_qt.processEvents()


def test_live_uvicorn_full_commercial_e2e_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from hex_helper import app as app_mod
    from hex_helper import edition as edition_mod
    from hex_helper.cloud import client as cloud_client_mod
    from hex_helper.cloud.client import CloudCoachError, CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore
    from hex_helper.cloud.updater import (
        UpdateInfo,
        UpdateVerificationError,
        check_for_update,
        download_and_verify_update,
    )
    from hex_helper.ui.account_dialogs import RechargeDialog, UpdatePromptDialog

    app_qt = _qt_app()
    monkeypatch.setattr(edition_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(app_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(cloud_client_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "client_data"))
    # Deliberately inject a dead ambient proxy to prove zero-proxy client & updater bypass it cleanly!
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    _patch_controller_env(monkeypatch)

    fastapi_app, gateway = _build_inprocess_server(tmp_path)
    store = _build_test_store(tmp_path / "store_cache")
    base_url, server, thread = _start_live_uvicorn(fastapi_app)
    monkeypatch.setenv("HEX_CLOUD_API_BASE", base_url)

    ctrl = None
    try:
        # 1. Register fresh user via live uvicorn -> balance == 0 -> Tier A & Tier B both return 402
        sess_store = DpapiSessionStore(tmp_path / "client_data" / "session.dat")
        client = CloudGatewayClient(base_url=base_url, session_store=sess_store)
        try:
            reg = client.register("LiveE2EUser", "PlayerPass#2026")
            user_id = int(reg["user"]["id"])
            assert float(reg["user"]["balance_credits"]) == 0.0
            assert reg["user"]["ai_cutoff"] is True

            req_a = CoachRequestPayload(
                request_id="live-e2e-a-0",
                trigger_reason="manual_f6",
                timestamp=utc_now_iso(),
                dataset_patch="26.23",
                my_hero={"hero_id": "63", "name": "布兰德", "gold": 3200, "items": ["心之钢"]},
            )
            with pytest.raises(CloudCoachError) as exc_a0:
                client.advise(req_a, store, tier="a")
            assert exc_a0.value.status_code == 402
            assert exc_a0.value.code == "INSUFFICIENT_CREDITS"

            with pytest.raises(CloudCoachError) as exc_b0:
                client.advise(req_a, store, tier="b")
            assert exc_b0.value.status_code == 402
            assert exc_b0.value.code == "INSUFFICIENT_CREDITS"
        finally:
            client.close()

        # Boot real HexHelperController against live uvicorn (zero TestClient mock!)
        ctrl = app_mod.HexHelperController(app_qt, show_lookup_on_start=False)
        ctrl.store = store
        ctrl.sm.current_hero_id = "63"
        assert ctrl._startup_update_timer is not None
        assert ctrl._startup_update_timer.isSingleShot() is True
        assert ctrl._startup_update_timer.interval() == 3000
        ctrl._startup_update_timer.stop()

        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not ctrl._cloud_ai_cutoff:
            app_qt.processEvents()
            time.sleep(0.02)
        assert ctrl._cloud_ai_cutoff is True
        assert ctrl.edge_drawer.cutoff_banner.isHidden() is False
        assert ctrl.lookup_window.cutoff_banner.isHidden() is False

        # 2. Create manual_wechat ¥10 QR order -> Admin logs in & confirms -> Client polls -> 10,000 credits & AI unlocks
        dlg = RechargeDialog(
            ctrl.cloud_client,
            on_paid=ctrl._on_recharge_paid,
            auto_load=False,
        )
        order = ctrl.cloud_client.create_order("pkg_10", "manual_wechat")
        assert order["status"] == "pending"
        assert order["credits_amount"] == 10000
        qr_bytes = ctrl.cloud_client.fetch_qr_image_bytes(order["qr_image_url"])
        assert qr_bytes.startswith(b"\x89PNG")
        dlg.show_created_order(order, qr_bytes)

        with httpx.Client(base_url=base_url, proxy=None, trust_env=False, timeout=10.0) as admin_http:
            admin_login = admin_http.post(
                "/v1/auth/login",
                json={"username": "admin", "password": "TestAdminPassword#2026"},
            )
            assert admin_login.status_code == 200, admin_login.text
            admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

            confirm_resp = admin_http.post(
                f"/admin/api/orders/{order['order_no']}/confirm",
                headers=admin_headers,
                json={"provider_trade_no": "wx-live-e2e-001"},
            )
            assert confirm_resp.status_code == 200, confirm_resp.text
            assert confirm_resp.json()["balance_credits"] == 10000.0

            poll_data = dlg.poll_once_sync()
            app_qt.processEvents()
            assert poll_data is not None
            assert poll_data["status"] == "paid"
            assert float(poll_data["wallet"]["balance_credits"]) == 10000.0
            assert ctrl._cloud_ai_cutoff is False
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is True
            assert ctrl.lookup_window.cutoff_banner.isHidden() is True
            assert "10,000" in ctrl.lookup_window.account_balance_lbl.text()
            dlg.close()

            # 3. Tier A (deepseek-flash) advise -> exact 2.0 / 8.0 credits/1K deducted & grounded advice
            req_a1 = CoachRequestPayload(
                request_id="live-e2e-a-1",
                trigger_reason="manual_f6",
                timestamp=utc_now_iso(),
                dataset_patch="26.23",
                my_hero={"hero_id": "63", "name": "布兰德", "gold": 3200, "items": ["心之钢"]},
            )
            adv_a, stats_a = ctrl.cloud_client.advise(req_a1, store, tier="a")
            assert stats_a.tier == "a"
            assert stats_a.model == "deepseek-flash"
            assert stats_a.cost_credits == 8.0
            assert stats_a.balance_credits == 9992.0
            assert [it.item_id for it in adv_a.next_items] == ["3083", "3089"]
            assert gateway.calls[-1]["tier"] == "a"

            # 4. Tier B (gemini-4-hydrogen) advise -> exact half-price 5.4 / 27.0 credits/1K deducted from SAME wallet
            req_b1 = CoachRequestPayload(
                request_id="live-e2e-b-1",
                trigger_reason="death_enemy_build_changed",
                timestamp=utc_now_iso(),
                dataset_patch="26.23",
                my_hero={
                    "hero_id": "63",
                    "name": "布兰德",
                    "gold": 1200,
                    "items": ["心之钢", "狂徒铠甲", "中娅沙漏", "莫雷洛秘典", "法师之靴", "灭世者的死亡之帽"],
                },
            )
            adv_b, stats_b = ctrl.cloud_client.advise(req_b1, store, tier="b")
            assert stats_b.tier == "b"
            assert stats_b.model == "gemini-4-hydrogen"
            assert stats_b.cost_credits == 37.8
            assert stats_b.balance_credits == 9954.2
            assert adv_b.build_path == []
            assert adv_b.next_items[0].item_id in ("2138", "2139")
            assert gateway.calls[-1]["tier"] == "b"

            logs_resp = admin_http.get(
                "/admin/api/usage-logs",
                params={"username": "LiveE2EUser"},
                headers=admin_headers,
            )
            assert logs_resp.status_code == 200
            assert logs_resp.json()["total"] == 2

            # 5. Admin sets balance to 0 -> next advise returns 402 -> AI cut off again
            adj_resp = admin_http.post(
                f"/admin/api/users/{user_id}/adjust-credits",
                headers=admin_headers,
                json={"delta_credits": -9954.2, "note": "zero out in live E2E"},
            )
            assert adj_resp.status_code == 200, adj_resp.text
            assert adj_resp.json()["balance_credits"] == 0.0

            with pytest.raises(CloudCoachError) as exc_after_zero:
                ctrl.cloud_client.advise(req_a1, store, tier="a")
            assert exc_after_zero.value.status_code == 402

            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._cloud_ai_cutoff is True
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is False

            # 6. Admin uploads release 1.0.1 .zip -> client detects, downloads & verifies SHA-256, and rejects tamper!
            release_bytes = _make_zip_bytes(
                {
                    "HexHelper.exe": b"MZ-dummy-1.0.1",
                    "version.txt": b"1.0.1",
                }
            )
            expected_sha256 = hashlib.sha256(release_bytes).hexdigest()

            up_resp = admin_http.post(
                "/admin/api/releases/upload",
                headers=admin_headers,
                data={
                    "version": "1.0.1",
                    "min_supported_version": "1.0.0",
                    "force_update": "false",
                    "changelog": "Live E2E v1.0.1 更新包",
                    "is_active": "true",
                },
                files={"file": ("HexHelper_v1.0.1_win64_portable.zip", release_bytes, "application/zip")},
            )
            assert up_resp.status_code == 200, up_resp.text
            assert up_resp.json()["release"]["sha256"] == expected_sha256

            info = check_for_update(base_url, "1.0.0")
            assert info is not None
            assert info.update_available is True
            assert info.latest_version == "1.0.1"
            assert info.sha256 == expected_sha256
            assert info.file_size == len(release_bytes)

            dl_dir = tmp_path / "client_updates"
            progress_events: list[tuple[int, int]] = []
            verified_path = download_and_verify_update(
                info,
                dl_dir,
                base_url=base_url,
                progress_cb=lambda got, total: progress_events.append((got, total)),
            )
            assert verified_path.is_file()
            assert verified_path.read_bytes() == release_bytes
            assert progress_events and progress_events[-1][0] == len(release_bytes)

            # Tampered SHA-256 MUST fail verification and leave zero corrupted files behind
            tamper_dir = tmp_path / "tamper_updates"
            tampered_info = UpdateInfo(
                current_version=info.current_version,
                latest_version=info.latest_version,
                update_available=True,
                force_update=False,
                min_supported_version=info.min_supported_version,
                download_url=info.download_url,
                sha256="f" * 64,
                file_size=info.file_size,
                changelog=info.changelog,
            )
            with pytest.raises(UpdateVerificationError):
                download_and_verify_update(tampered_info, tamper_dir, base_url=base_url)
            assert (not tamper_dir.exists()) or list(tamper_dir.iterdir()) == []

            # Also verify UpdatePromptDialog downloads cleanly from live uvicorn and sets downloaded_artifact_path
            dlg_dir = tmp_path / "dlg_updates"
            upd_dlg = UpdatePromptDialog(info, base_url, dlg_dir)
            try:
                upd_dlg.download_btn.click()
                dl_deadline = time.monotonic() + 5.0
                while time.monotonic() < dl_deadline and upd_dlg.downloaded_artifact_path is None:
                    app_qt.processEvents()
                    time.sleep(0.02)
                app_qt.processEvents()
                assert upd_dlg.downloaded_artifact_path is not None
                assert upd_dlg.downloaded_artifact_path.read_bytes() == release_bytes
            finally:
                upd_dlg.close()
                app_qt.processEvents()

            # Verify manual Check Update button & startup check trigger update prompt flow cleanly
            prompted_versions: list[str] = []
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    ctrl,
                    "_prompt_update_dialog",
                    lambda upd_info: prompted_versions.append(upd_info.latest_version),
                )
                ctrl.lookup_window.check_update_btn.click()
                chk_deadline = time.monotonic() + 4.0
                while time.monotonic() < chk_deadline and not prompted_versions:
                    app_qt.processEvents()
                    time.sleep(0.02)
                assert prompted_versions == ["1.0.1"]
                assert "1.0.1" in ctrl.lookup_window.update_status_lbl.text()

                # Startup check also discovers 1.0.1 silently without blocking
                ctrl._check_updates_on_startup()
                chk_deadline2 = time.monotonic() + 4.0
                while time.monotonic() < chk_deadline2 and len(prompted_versions) < 2:
                    app_qt.processEvents()
                    time.sleep(0.02)
                assert prompted_versions == ["1.0.1", "1.0.1"]

            # Verify _prompt_update_dialog launches installer & shuts down when dialog accepts
            launched_args: list[tuple[Path, Path, int]] = []
            shutdown_calls: list[bool] = []

            class _FakeAcceptedDlg:
                def __init__(self, _info, _base, dest, parent=None) -> None:
                    self.downloaded_artifact_path = dest / "verified.zip"

                def exec(self) -> int:
                    from PySide6.QtWidgets import QDialog

                    return int(QDialog.DialogCode.Accepted)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("hex_helper.ui.account_dialogs.UpdatePromptDialog", _FakeAcceptedDlg)
                mp.setattr(
                    app_mod,
                    "launch_update_installer",
                    lambda art, adir, pid: launched_args.append((art, adir, pid)),
                )
                mp.setattr(ctrl, "shutdown", lambda: shutdown_calls.append(True))
                ctrl._prompt_update_dialog(info)
                assert len(launched_args) == 1
                assert launched_args[0][0].name == "verified.zip"
                assert shutdown_calls == [True]

                # When force_update=True, rejecting dialog OR installer launch error MUST still shut down client
                class _FakeRejectedDlg:
                    def __init__(self, _info, _base, _dest, parent=None) -> None:
                        self.downloaded_artifact_path = None

                    def exec(self) -> int:
                        from PySide6.QtWidgets import QDialog

                        return int(QDialog.DialogCode.Rejected)

                forced_info = UpdateInfo(
                    current_version="1.0.0",
                    latest_version="1.0.1",
                    update_available=True,
                    force_update=True,
                    min_supported_version="1.0.1",
                    download_url=info.download_url,
                    sha256=info.sha256,
                    file_size=info.file_size,
                    changelog="forced",
                )
                mp.setattr("hex_helper.ui.account_dialogs.UpdatePromptDialog", _FakeRejectedDlg)
                ctrl._prompt_update_dialog(forced_info)
                assert shutdown_calls == [True, True]

                mp.setattr("hex_helper.ui.account_dialogs.UpdatePromptDialog", _FakeAcceptedDlg)
                mp.setattr(
                    app_mod,
                    "launch_update_installer",
                    lambda *_a: (_ for _ in ()).throw(RuntimeError("launch failed")),
                )
                ctrl._prompt_update_dialog(forced_info)
                assert shutdown_calls == [True, True, True]

            # Verify concurrent check-update calls are debounced to a single in-flight request
            import threading

            entered = threading.Event()
            release = threading.Event()
            manifest_calls: list[str] = []

            def _slow_check(base: str, ver: str, **_kw):
                manifest_calls.append(ver)
                entered.set()
                release.wait(timeout=3.0)
                return info

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(app_mod, "check_for_update", _slow_check)
                mp.setattr(ctrl, "_prompt_update_dialog", lambda _i: None)
                ctrl.lookup_window.check_update_btn.click()
                assert entered.wait(timeout=2.0)
                ctrl.lookup_window.check_update_btn.click()
                ctrl._check_updates_on_startup()
                release.set()
                deb_deadline = time.monotonic() + 3.0
                while time.monotonic() < deb_deadline and ctrl._update_check_in_flight:
                    app_qt.processEvents()
                    time.sleep(0.02)
                app_qt.processEvents()
                assert len(manifest_calls) == 1
    finally:
        if ctrl is not None:
            ctrl.shutdown()
            app_qt.processEvents()
        server.should_exit = True
        thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# 7. Task 11: Service Lock (503), Version Gate (426), Live Heartbeat Update Push
# ---------------------------------------------------------------------------


def test_cloud_client_sends_version_header_and_parses_426_503_extra(
    tmp_path: Path,
) -> None:
    import httpx
    from hex_helper.cloud.client import CloudCoachError, CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore
    from hex_helper.cloud.updater import UpdateInfo, parse_update_info
    from hex_helper.edition import APP_VERSION

    seen_headers: list[dict[str, str]] = []
    next_response = {"status": 503}

    class _GateInspectClient:
        def request(self, method: str, path: str, **kwargs):
            hdrs = {k.lower(): v for k, v in (kwargs.get("headers") or {}).items()}
            seen_headers.append(hdrs)
            req = httpx.Request(method, f"http://cloud.test{path}")
            if next_response["status"] == 503:
                return httpx.Response(
                    503,
                    request=req,
                    json={
                        "code": "SERVICE_LOCKED",
                        "message": "系统全服维护中",
                        "ai_cutoff": True,
                        "service_lock": {"locked": True, "reason": "系统全服维护中"},
                        "update": {"latest_version": "1.0.0", "update_available": False, "force_update": False},
                    },
                )
            return httpx.Response(
                426,
                request=req,
                json={
                    "code": "CLIENT_UPDATE_REQUIRED",
                    "message": "请立即更新至 v1.0.1",
                    "ai_cutoff": True,
                    "service_lock": {"locked": False, "reason": ""},
                    "update": {
                        "current_version": APP_VERSION,
                        "latest_version": "1.0.1",
                        "update_available": True,
                        "force_update": True,
                        "min_supported_version": "1.0.1",
                        "download_url": "/v1/updates/download/1.0.1",
                        "sha256": "a" * 64,
                        "file_size": 1024,
                        "changelog": "forced",
                    },
                },
            )

        def close(self) -> None:
            return None

    sess_store = DpapiSessionStore(tmp_path / "session.dat")
    sess_store.save(
        {"access_token": "tok", "refresh_token": "ref", "username": "VerUser", "device_id": "d"}
    )
    client = CloudGatewayClient(
        base_url="http://cloud.test",
        session_store=sess_store,
        http_client_factory=lambda _b, _t: _GateInspectClient(),
    )
    client.restore_session()

    store = _build_test_store(tmp_path / "cache")
    req_obj = CoachRequestPayload(
        request_id="req-hdr-1",
        trigger_reason="manual_f6",
        timestamp=utc_now_iso(),
        dataset_patch="26.23",
        my_hero={"hero_id": "63", "name": "布兰德", "gold": 1000, "items": []},
    )

    with pytest.raises(CloudCoachError) as exc503:
        client.advise(req_obj, store, tier="a")
    assert exc503.value.status_code == 503
    assert exc503.value.code == "SERVICE_LOCKED"
    assert exc503.value.extra["service_lock"]["locked"] is True
    assert seen_headers[-1].get("x-client-version") == APP_VERSION

    next_response["status"] = 426
    with pytest.raises(CloudCoachError) as exc426:
        client.create_order("pkg_10", "manual_wechat")
    assert exc426.value.status_code == 426
    assert exc426.value.code == "CLIENT_UPDATE_REQUIRED"
    assert exc426.value.extra["update"]["latest_version"] == "1.0.1"
    assert exc426.value.extra["update"]["force_update"] is True
    assert seen_headers[-1].get("x-client-version") == APP_VERSION

    parsed = parse_update_info(exc426.value.extra["update"], APP_VERSION)
    assert isinstance(parsed, UpdateInfo)
    assert parsed.force_update is True
    assert parsed.latest_version == "1.0.1"
    assert parse_update_info(None, APP_VERSION) is None


def test_live_service_lock_heartbeat_forced_426_and_optional_dismiss_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from PySide6.QtWidgets import QDialog
    from hex_helper import app as app_mod
    from hex_helper import edition as edition_mod
    from hex_helper.cloud import client as cloud_client_mod
    from hex_helper.cloud.client import CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore
    from hex_helper.game.models import ChampSelectState
    from hex_helper.vision.detector import DetectedCard

    app_qt = _qt_app()
    monkeypatch.setattr(edition_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(app_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setattr(cloud_client_mod, "APP_VERSION", "1.0.0")
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "client_data"))
    _patch_controller_env(monkeypatch)

    fastapi_app, gateway = _build_inprocess_server(tmp_path)
    store = _build_test_store(tmp_path / "store_cache")
    base_url, server, thread = _start_live_uvicorn(fastapi_app)
    monkeypatch.setenv("HEX_CLOUD_API_BASE", base_url)

    ctrl = None
    try:
        with httpx.Client(base_url=base_url, proxy=None, trust_env=False, timeout=10.0) as admin_http:
            admin_login = admin_http.post(
                "/v1/auth/login",
                json={"username": "admin", "password": "TestAdminPassword#2026"},
            )
            assert admin_login.status_code == 200
            admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

            # A. Unlogged-in controller heartbeat detects service_lock via /v1/updates/manifest and unlocks when cleared
            ctrl_unlogged = app_mod.HexHelperController(app_qt, show_lookup_on_start=False)
            try:
                if ctrl_unlogged._startup_update_timer is not None:
                    ctrl_unlogged._startup_update_timer.stop()
                ctrl_unlogged.store = store
                ctrl_unlogged.sm.current_hero_id = "63"

                maint_msg = "临时停服维护：正在轮换云端节点，请稍候"
                assert (
                    admin_http.put(
                        "/admin/api/service-lock",
                        headers=admin_headers,
                        json={"locked": True, "reason": maint_msg},
                    ).status_code
                    == 200
                )
                ctrl_unlogged._poll_cloud_wallet_sync()
                app_qt.processEvents()
                assert ctrl_unlogged._service_locked is True
                assert ctrl_unlogged._cloud_ai_cutoff is True
                assert ctrl_unlogged._cloud_cutoff_code == "SERVICE_LOCKED"
                assert maint_msg in ctrl_unlogged.lookup_window.cutoff_banner_lbl.text()
                assert maint_msg in ctrl_unlogged.edge_drawer.cutoff_banner_lbl.text()

                assert (
                    admin_http.put(
                        "/admin/api/service-lock",
                        headers=admin_headers,
                        json={"locked": False},
                    ).status_code
                    == 200
                )
                ctrl_unlogged._poll_cloud_wallet_sync()
                app_qt.processEvents()
                assert ctrl_unlogged._service_locked is False
                assert ctrl_unlogged._cloud_cutoff_code == "NOT_LOGGED_IN"
            finally:
                ctrl_unlogged.shutdown()
                app_qt.processEvents()

            # B. Register & fund logged-in player (10,000 credits)
            sess_store = DpapiSessionStore(tmp_path / "client_data" / "session.dat")
            reg_client = CloudGatewayClient(base_url=base_url, session_store=sess_store)
            try:
                reg = reg_client.register("LockE2EUser", "PlayerPass#2026")
                user_id = int(reg["user"]["id"])
            finally:
                reg_client.close()

            assert (
                admin_http.post(
                    f"/admin/api/users/{user_id}/adjust-credits",
                    headers=admin_headers,
                    json={"delta_credits": 10000.0, "note": "seed"},
                ).status_code
                == 200
            )

            ctrl = app_mod.HexHelperController(app_qt, show_lookup_on_start=False)
            if ctrl._startup_update_timer is not None:
                ctrl._startup_update_timer.stop()
            ctrl.store = store
            ctrl.sm.current_hero_id = "63"

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and (ctrl.cloud_client.last_wallet is None or ctrl._cloud_ai_cutoff):
                app_qt.processEvents()
                time.sleep(0.02)
            assert ctrl._cloud_ai_cutoff is False

            # Render champ & augment overlays initially
            cs = ChampSelectState(450, "KIWI", True, "63", ("63",), ())
            ctrl._apply_lcu_poll("ChampSelect", cs)
            assert ctrl.champ_overlay.isVisible() is True

            # 1. Admin turns ON Service Lock -> heartbeat locks AI & overlays and shows maintenance reason
            lock_reason = "紧急停服升级中，预计 3 分钟恢复"
            assert (
                admin_http.put(
                    "/admin/api/service-lock",
                    headers=admin_headers,
                    json={"locked": True, "reason": lock_reason},
                ).status_code
                == 200
            )
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._service_locked is True
            assert ctrl._cloud_ai_cutoff is True
            assert ctrl._cloud_cutoff_code == "SERVICE_LOCKED"
            assert ctrl.champ_overlay.isVisible() is False
            assert ctrl.lookup_window.cutoff_banner.isHidden() is False
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is False
            assert lock_reason in ctrl.lookup_window.cutoff_banner_lbl.text()
            assert lock_reason in ctrl.edge_drawer.cutoff_banner_lbl.text()

            # F6 while locked must NOT invoke upstream gateway
            calls_before_lock = len(gateway.calls)
            ctrl.trigger_coach_request("manual_f6")
            app_qt.processEvents()
            assert len(gateway.calls) == calls_before_lock

            # 2. Admin turns OFF Service Lock -> heartbeat automatically unlocks!
            assert (
                admin_http.put(
                    "/admin/api/service-lock",
                    headers=admin_headers,
                    json={"locked": False},
                ).status_code
                == 200
            )
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._service_locked is False
            assert ctrl._cloud_ai_cutoff is False
            assert ctrl.lookup_window.cutoff_banner.isHidden() is True
            assert ctrl.edge_drawer.cutoff_banner.isHidden() is True
            assert ctrl.edge_drawer.cutoff_banner_lbl.wordWrap() is True

            # 2b. Raise min_client_version="1.0.1" BEFORE any release is uploaded -> locks & cites "v1.0.1" (never "v1.0.0"!)
            assert (
                admin_http.put(
                    "/admin/api/service-lock",
                    headers=admin_headers,
                    json={"min_client_version": "1.0.1"},
                ).status_code
                == 200
            )
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._force_update_required is True
            assert ctrl._cloud_cutoff_code == "CLIENT_UPDATE_REQUIRED"
            assert "v1.0.1" in ctrl._force_update_reason
            assert "请更新至 v1.0.0" not in ctrl._force_update_reason
            assert "v1.0.1" in ctrl.lookup_window.cutoff_banner_lbl.text()

            assert (
                admin_http.put(
                    "/admin/api/service-lock",
                    headers=admin_headers,
                    json={"min_client_version": ""},
                ).status_code
                == 200
            )
            ctrl._poll_cloud_wallet_sync()
            app_qt.processEvents()
            assert ctrl._force_update_required is False
            assert ctrl._cloud_ai_cutoff is False

            # 3. Optional update 1.0.1 (force_update=False): pops once on heartbeat, dismissing sets _dismissed_optional_version,
            #    second heartbeat does NOT re-pop, while manual "检查更新" still opens it!
            opt_zip = _make_zip_bytes({"HexHelper.exe": b"MZ-1.0.1-opt"})
            assert (
                admin_http.post(
                    "/admin/api/releases/upload",
                    headers=admin_headers,
                    data={
                        "version": "1.0.1",
                        "min_supported_version": "1.0.0",
                        "force_update": "false",
                        "changelog": "可选体验更新 1.0.1",
                        "is_active": "true",
                    },
                    files={"file": ("HexHelper_1.0.1.zip", opt_zip, "application/zip")},
                ).status_code
                == 200
            )

            seen_dialogs: list[tuple[str, bool]] = []

            class _DismissingFakeDialog:
                def __init__(self, upd_info, _base, _dest, parent=None) -> None:
                    self.downloaded_artifact_path = None
                    seen_dialogs.append((upd_info.latest_version, bool(upd_info.force_update)))

                def exec(self) -> int:
                    return int(QDialog.DialogCode.Rejected)

            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("hex_helper.ui.account_dialogs.UpdatePromptDialog", _DismissingFakeDialog)

                ctrl._poll_cloud_wallet_sync()
                app_qt.processEvents()
                assert seen_dialogs == [("1.0.1", False)]
                assert ctrl._dismissed_optional_version == "1.0.1"
                assert ctrl._cloud_ai_cutoff is False

                # Second heartbeat MUST NOT re-pop dismissed optional version 1.0.1
                ctrl._poll_cloud_wallet_sync()
                app_qt.processEvents()
                assert seen_dialogs == [("1.0.1", False)]

                # Manual "检查更新" ALWAYS re-opens dialog!
                ctrl.lookup_window.check_update_btn.click()
                dl_wait = time.monotonic() + 3.0
                while time.monotonic() < dl_wait and len(seen_dialogs) < 2:
                    app_qt.processEvents()
                    time.sleep(0.02)
                assert seen_dialogs == [("1.0.1", False), ("1.0.1", False)]

                # Even when _dismissed_optional_version == "1.0.1" and update_info.force_update=False,
                # too_old / CLIENT_UPDATE_REQUIRED MUST force-upgrade dialog & pass force_update=True!
                from hex_helper.cloud.updater import UpdateInfo

                forced_via_min = UpdateInfo(
                    current_version="1.0.0",
                    latest_version="1.0.1",
                    update_available=True,
                    force_update=False,
                    min_supported_version="1.0.1",
                    download_url="/v1/updates/download/1.0.1",
                    sha256="a" * 64,
                    file_size=len(opt_zip),
                    changelog="forced by min_supported_version",
                )
                with pytest.MonkeyPatch.context() as mp_spy:
                    mp_spy.setattr(
                        ctrl,
                        "_prompt_update_dialog",
                        lambda upd_info: seen_dialogs.append((upd_info.latest_version, bool(upd_info.force_update))),
                    )
                    ctrl._apply_service_lock_and_update(
                        None,
                        forced_via_min,
                        gate_code="CLIENT_UPDATE_REQUIRED",
                    )
                assert seen_dialogs[-1] == ("1.0.1", True)
                assert ctrl._force_update_required is True
                assert not hasattr(ctrl, "_update_dialog_open")

            # 4. Admin uploads forced 1.0.2 -> heartbeat immediately cuts off AI and pops non-skippable UpdatePromptDialog
            force_zip = _make_zip_bytes({"HexHelper.exe": b"MZ-1.0.2-force"})
            assert (
                admin_http.post(
                    "/admin/api/releases/upload",
                    headers=admin_headers,
                    data={
                        "version": "1.0.2",
                        "min_supported_version": "1.0.2",
                        "force_update": "true",
                        "changelog": "重要强制更新 1.0.2",
                        "is_active": "true",
                    },
                    files={"file": ("HexHelper_1.0.2.zip", force_zip, "application/zip")},
                ).status_code
                == 200
            )

            forced_dialogs: list[tuple[str, bool]] = []
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    ctrl,
                    "_prompt_update_dialog",
                    lambda upd_info: forced_dialogs.append((upd_info.latest_version, bool(upd_info.force_update))),
                )
                ctrl._poll_cloud_wallet_sync()
                app_qt.processEvents()
                assert forced_dialogs == [("1.0.2", True)]
                assert ctrl._cloud_ai_cutoff is True
                assert ctrl._cloud_cutoff_code == "CLIENT_UPDATE_REQUIRED"
                assert ctrl.lookup_window.cutoff_banner.isHidden() is False
                assert ctrl.edge_drawer.cutoff_banner.isHidden() is False

                # 5. Also verify instant F6 426 detection via CloudCoachWorker (before any heartbeat ran!)
                ctrl.sm.state = app_mod.GameState.IN_GAME
                ctrl._force_update_required = False
                ctrl._recompute_cloud_cutoff_state()
                assert ctrl._cloud_ai_cutoff is False

                calls_before_426 = len(gateway.calls)
                ctrl.trigger_coach_request("manual_f6")
                dl_426 = time.monotonic() + 3.0
                while time.monotonic() < dl_426 and len(forced_dialogs) < 2:
                    app_qt.processEvents()
                    time.sleep(0.02)
                assert len(gateway.calls) == calls_before_426
                assert forced_dialogs == [("1.0.2", True), ("1.0.2", True)]
                assert ctrl._cloud_ai_cutoff is True
                assert ctrl._cloud_cutoff_code == "CLIENT_UPDATE_REQUIRED"
    finally:
        if ctrl is not None:
            ctrl.shutdown()
            app_qt.processEvents()
        server.should_exit = True
        thread.join(timeout=5.0)


def test_signup_bonus_dialog_and_registration_flow(tmp_path: Path, monkeypatch) -> None:
    """Verify server register signup_bonus_credits, LoginRegisterDialog state, and SignupBonusDialog popup."""
    from PySide6.QtWidgets import QDialog

    from hex_helper.app import HexHelperController
    from hex_helper.cloud.client import CloudGatewayClient, DpapiSessionStore
    from hex_helper.config import AppConfig
    from hex_helper.ui.account_dialogs import LoginRegisterDialog, SignupBonusDialog

    app_qt = _qt_app()

    # 1. Server /v1/auth/register returns signup_bonus_credits=100 and credits user wallet 100.0
    app, _gateway = _build_inprocess_server(tmp_path)
    with TestClient(app) as tc:
        app.state.db.update_pricing({"signup_bonus_credits": 100})
        reg_resp = tc.post(
            "/v1/auth/register",
            json={"username": "FriendAlan100", "password": "Password123!", "device_id": "dev-bonus-1"},
        )
        assert reg_resp.status_code == 201
        reg_data = reg_resp.json()
        assert reg_data["signup_bonus_credits"] == 100
        assert reg_data["user"]["balance_credits"] == pytest.approx(100.0)
        assert reg_data["user"]["ai_enabled"] is True

    # 2. SignupBonusDialog renders cleanly without missing widget imports
    bonus_dlg = SignupBonusDialog("AlanFriend", 100.0)
    assert "100 积分" in bonus_dlg.badge_lbl.text()
    assert "AlanFriend" in bonus_dlg.body_lbl.text()
    assert "100 积分" in bonus_dlg.body_lbl.text()
    bonus_dlg.close()

    # 3. LoginRegisterDialog mode visibility + explicit & fallback bonus extraction
    cloud = CloudGatewayClient(base_url="http://127.0.0.1:18080", session_store=DpapiSessionStore(tmp_path / "sess.bin"))
    dlg = LoginRegisterDialog(cloud, initial_mode="login")
    assert dlg.register_bonus_hint_lbl.isHidden() is True
    dlg.set_mode("register")
    assert dlg.register_bonus_hint_lbl.isHidden() is False

    dlg._on_auth_ok(
        {"signup_bonus_credits": 100, "user": {"username": "NewPal", "balance_credits": 100.0}},
        mode="register",
    )
    assert dlg.was_registration is True
    assert dlg.signup_bonus_credits == pytest.approx(100.0)

    dlg._on_auth_ok(
        {"user": {"username": "FallbackPal", "balance_credits": 100.0}},
        mode="register",
    )
    assert dlg.was_registration is True
    assert dlg.signup_bonus_credits == pytest.approx(100.0)

    dlg._on_auth_ok(
        {"user": {"username": "OldPal", "balance_credits": 100.0}},
        mode="login",
    )
    assert dlg.was_registration is False
    assert dlg.signup_bonus_credits == pytest.approx(0.0)
    dlg.close()

    # 4. HexHelperController.open_login_dialog() pops SignupBonusDialog ONLY on registration with bonus > 0
    monkeypatch.setenv("HEX_EDITION", "commercial")
    monkeypatch.setenv("HEX_CLOUD_API_BASE", "http://127.0.0.1:18080")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "client_data"))
    ctrl = HexHelperController(app_qt, show_lookup_on_start=False)
    if ctrl._startup_update_timer is not None:
        ctrl._startup_update_timer.stop()
    ctrl.store = _build_test_store(tmp_path / "cache")
    try:
        popped_bonuses: list[tuple[str, float]] = []

        class _FakeBonusDialog:
            def __init__(self, username: str, bonus_credits: float, parent=None) -> None:
                popped_bonuses.append((username, bonus_credits))

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

        class _FakeRegisterLoginDialog:
            mode_to_sim = "register"

            def __init__(self, cloud_client, parent=None, initial_mode: str = "login") -> None:
                self.was_registration = self.mode_to_sim == "register"
                self.signup_bonus_credits = 100.0 if self.was_registration else 0.0
                self.auth_result = {
                    "user": {
                        "username": "AlanNewUser",
                        "balance_credits": 100.0,
                        "ai_enabled": True,
                        "ai_cutoff": False,
                        "cutoff_policy": "ai_only",
                    }
                }

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

        monkeypatch.setattr("hex_helper.ui.account_dialogs.LoginRegisterDialog", _FakeRegisterLoginDialog)
        monkeypatch.setattr("hex_helper.ui.account_dialogs.SignupBonusDialog", _FakeBonusDialog)
        monkeypatch.setattr(ctrl, "_poll_cloud_wallet_async", lambda: None)

        _FakeRegisterLoginDialog.mode_to_sim = "register"
        ctrl.open_login_dialog()
        assert popped_bonuses == [("AlanNewUser", 100.0)]

        _FakeRegisterLoginDialog.mode_to_sim = "login"
        ctrl.open_login_dialog()
        assert popped_bonuses == [("AlanNewUser", 100.0)]
    finally:
        ctrl.shutdown()
        app_qt.processEvents()
        cloud.close()


def test_patch_zip_contains_only_exe_and_updater_downloads_external_cos_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify lightweight patch zip invariant (zero _internal/) + external HTTPS COS download."""
    import hashlib
    import importlib.util
    import zipfile

    import httpx

    from hex_helper.cloud import updater as updater_mod
    from hex_helper.cloud.updater import UpdateInfo, download_and_verify_update

    spec = importlib.util.spec_from_file_location(
        "build_windows_release_mod",
        ROOT / "scripts" / "build_windows_release.py",
    )
    assert spec is not None and spec.loader is not None
    build_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_mod)

    fake_bundle = tmp_path / "HexHelper"
    (fake_bundle / "_internal" / "PySide6").mkdir(parents=True)
    (fake_bundle / "HexHelper.exe").write_bytes(b"MZ-fake-new-exe-v1.0.1" * 1024)
    (fake_bundle / "_internal" / "PySide6" / "Qt6Core.dll").write_bytes(b"dll-bytes")

    patch_zip = tmp_path / "HexHelper_v1.0.1_win64_patch.zip"
    build_mod._build_patch_zip(fake_bundle, patch_zip)

    with zipfile.ZipFile(patch_zip, "r") as zf:
        assert zf.namelist() == ["HexHelper.exe"]

    raw_patch = patch_zip.read_bytes()
    patch_sha = hashlib.sha256(raw_patch).hexdigest()

    cos_url = "https://hexhelper-1250000001.cos.ap-shanghai.myqcloud.com/releases/1.0.1/HexHelper_v1.0.1_win64_patch.zip"
    seen_urls: list[str] = []

    def cos_handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(str(req.url))
        return httpx.Response(200, content=raw_patch, headers={"Content-Length": str(len(raw_patch))})

    update = UpdateInfo(
        update_available=True,
        force_update=False,
        current_version="1.0.0",
        latest_version="1.0.1",
        min_supported_version="1.0.0",
        download_url=cos_url,
        sha256=patch_sha,
        file_size=len(raw_patch),
        changelog="国内 COS 轻量补丁",
    )

    monkeypatch.setattr(
        updater_mod,
        "_new_zero_proxy_client",
        lambda _timeout_s, **_kw: httpx.Client(transport=httpx.MockTransport(cos_handler)),
    )
    saved = download_and_verify_update(
        update,
        tmp_path / "dl",
        base_url="https://api.priswebtool.site",
    )

    assert seen_urls == [cos_url]
    assert saved.is_file()
    assert saved.name == "HexHelper_v1.0.1_win64_patch.zip"
    assert saved.read_bytes() == raw_patch



