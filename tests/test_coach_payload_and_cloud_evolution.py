"""Tests for prefix-cache-friendly coach payload optimization, config extensions, and cloud telemetry evolution."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload
from hex_helper.cloud.client import CloudCoachWorker, CloudGatewayClient
from hex_helper.cloud.session_store import DpapiSessionStore
from hex_helper.coach.gemini_coach import (
    COACH_RESPONSE_SCHEMA,
    COACH_SYSTEM_INSTRUCTION,
    build_coach_prompt_payload,
    ground_coach_advice,
)
from hex_helper.config import (
    COACH_TIERS,
    AppConfig,
    _default_coach_tier,
    load_config,
    resolve_thinking_level,
    save_config,
)
from hex_helper.data.provider import CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "cache")
    static.items = {
        "3078": ItemStaticInfo("3078", "三相之力", 3333, tags=("Damage",), category="complete_equipment"),
        "3153": ItemStaticInfo("3153", "破败王者之刃", 3200, tags=("Damage", "LifeSteal"), category="complete_equipment"),
        "6333": ItemStaticInfo("6333", "死亡之舞", 3300, tags=("Damage", "Armor"), category="complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, tags=("Health",), category="complete_equipment"),
        "3083": ItemStaticInfo("3083", "狂徒铠甲", 3100, tags=("Health",), category="complete_equipment"),
        "3033": ItemStaticInfo("3033", "凡性的提醒", 3000, tags=("Damage",), category="complete_equipment"),
        "3184": ItemStaticInfo("3184", "守护者之锤", 950, tags=("Damage",), category="starter"),
        "3111": ItemStaticInfo("3111", "水银之靴", 1200, tags=("Boots",), category="boots"),
        "2052": ItemStaticInfo("2052", "魄罗佳肴", 0, tags=(), category="other"),
    }
    static.hero_aliases["164"] = "Camille"
    static.hero_cn_names["164"] = "青钢影"

    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "datasets/16.23/normal", "2026-11-22", True)
    store._heroes["164"] = HeroStat("164", "卡蜜尔", "青钢影", 120000, 61800, 0.515, 0.04, 35, False)
    store._core_builds["164"] = [
        CoreBuild(("3078", "6333", "3033"), True, 4500, 2430, 0.54, 0.22),
    ]
    return store


def test_prompt_payload_static_prefix_order_strips_volatile_and_poro_snax(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    req_turn1 = CoachRequestPayload(
        request_id="req-turn-001",
        trigger_reason="game_start",
        timestamp="2026-11-22T20:00:01+08:00",
        dataset_patch="26.23",
        my_hero={
            "hero_id": "164",
            "name": "卡蜜尔",
            "win_rate": 0.515,
            "gold": 1400,
            "is_dead": False,
            "items": ["守护者之锤", "魄罗佳肴"],
        },
        selected_augments=[],
        offered_augments=[],
        allies=[
            {"champion": "青钢影", "kda": "0/0/0", "items": ["守护者之锤", "魄罗佳肴"]},
            {"champion": "拉克丝", "kda": "0/0/0", "items": ["失落之章", "魄罗佳肴"]},
        ],
        enemies=[
            {"champion": "亚托克斯", "kda": "0/0/0", "items": ["守护者之锤", "魄罗佳肴"]},
        ],
        threat_summary=["敌方高回复"],
        local_engine_top_items=[
            {"item_id": "3078", "name": "三相之力", "price": 3333, "effective_cost": 3333, "affordable": False, "buy_now_hint": "", "reasons": ["核心", "高胜率", "第3条应截断"]},
            {"item_id": "6333", "name": "死亡之舞", "price": 3300, "effective_cost": 2500, "affordable": True, "buy_now_hint": "买考尔菲德战锤", "reasons": ["抗爆发"]},
            {"item_id": "3033", "name": "凡性的提醒", "price": 3000, "effective_cost": 3000, "affordable": False, "buy_now_hint": "", "reasons": ["重伤"]},
            {"item_id": "3083", "name": "狂徒铠甲", "price": 3100, "effective_cost": 3100, "affordable": False, "buy_now_hint": "", "reasons": ["续航"]},
            {"item_id": "3084", "name": "心之钢", "price": 3000, "effective_cost": 3000, "affordable": False, "buy_now_hint": "", "reasons": ["第5件应截断"]},
        ],
        committed_build_plan={"owned_complete": [], "allow_overhaul": False, "overhaul_reason": ""},
        special_augment_builds=[],
        hero_signature_hint={
            "archetype": "单带爆发战士",
            "starter_items": ["守护者之锤(3184)"],
            "signature_items": ["三相之力(3078)"],
            "note": "优先三相之力",
        },
        augment_combo_context={},
    )

    p1 = build_coach_prompt_payload(req_turn1, store)
    keys = list(p1.keys())

    # Volatile fields excluded from prompt dict (still intact on CoachRequestPayload)
    assert "request_id" not in p1
    assert "timestamp" not in p1
    assert req_turn1.request_id == "req-turn-001"
    assert req_turn1.timestamp == "2026-11-22T20:00:01+08:00"

    # Empty optional sections omitted
    assert "offered_augments" not in p1
    assert "special_augment_builds" not in p1
    assert "augment_combo_context" not in p1

    # Static prefix comes strictly FIRST in exact deterministic order
    assert keys[:5] == [
        "dataset_patch",
        "hero_static",
        "hero_big_data_top_builds",
        "hero_signature_hint",
        "item_glossary",
    ]
    assert keys[5:] == [
        "trigger_reason",
        "my_hero",
        "inventory_status",
        "selected_augments",
        "committed_build_plan",
        "local_engine_top_items",
        "threat_summary",
        "allies",
        "enemies",
    ]

    assert p1["hero_static"] == {"hero_id": "164", "name": "卡蜜尔", "win_rate": 0.515}
    assert "hero_id" not in p1["my_hero"]
    assert "win_rate" not in p1["my_hero"]
    assert p1["my_hero"]["name"] == "卡蜜尔"
    assert p1["my_hero"]["kda"] == "0/0/0"

    # 魄罗佳肴 stripped from my_hero, allies, enemies without mutating original request
    assert "魄罗佳肴" not in p1["my_hero"]["items"]
    assert all("魄罗佳肴" not in a["items"] for a in p1["allies"])
    assert all("魄罗佳肴" not in e["items"] for e in p1["enemies"])
    assert "魄罗佳肴" in req_turn1.my_hero["items"]

    # Self ("青钢影") deduplicated from allies; enemies untouched
    assert [a["champion"] for a in p1["allies"]] == ["拉克丝"]
    assert [e["champion"] for e in p1["enemies"]] == ["亚托克斯"]

    # Local engine recommendations capped to top 4 and compacted
    assert len(p1["local_engine_top_items"]) == 4
    assert "price" not in p1["local_engine_top_items"][0]
    assert p1["local_engine_top_items"][0]["effective_cost"] == 3333
    assert "affordable" not in p1["local_engine_top_items"][0]
    assert "buy_now_hint" not in p1["local_engine_top_items"][0]
    assert p1["local_engine_top_items"][0]["reasons"] == ["核心", "高胜率"]
    assert p1["local_engine_top_items"][1]["price"] == 3300
    assert p1["local_engine_top_items"][1]["effective_cost"] == 2500
    assert p1["local_engine_top_items"][1]["affordable"] is True
    assert p1["local_engine_top_items"][1]["buy_now_hint"] == "买考尔菲德战锤"
    # Empty containers pruned inside committed_build_plan
    assert p1["committed_build_plan"] == {"allow_overhaul": False}

    # Compact item glossary format ("名称|金币")
    assert p1["item_glossary"]["3078"] == "三相之力|3333"
    assert p1["item_glossary"]["3184"] == "守护者之锤|950"

    # Turn 5 with changed volatile fields shares exact byte-identical static prefix!
    req_turn5 = CoachRequestPayload(
        request_id="req-turn-005",
        trigger_reason="death_enemy_build_changed",
        timestamp="2026-11-22T20:14:33+08:00",
        dataset_patch="26.23",
        my_hero={
            "hero_id": "164",
            "name": "卡蜜尔",
            "win_rate": 0.515,
            "gold": 2850,
            "is_dead": True,
            "items": ["三相之力", "水银之靴", "魄罗佳肴"],
        },
        selected_augments=[{"id": "1001", "name": "秘术冲拳", "rarity": "prismatic"}],
        allies=[
            {"champion": "青钢影", "kda": "8/3/11", "items": ["三相之力", "水银之靴", "魄罗佳肴"]},
            {"champion": "拉克丝", "kda": "5/2/16", "items": ["卢登的伙伴", "魄罗佳肴"]},
        ],
        enemies=[
            {"champion": "亚托克斯", "kda": "9/4/6", "items": ["焚天", "死亡之舞", "魄罗佳肴"]},
        ],
        hero_signature_hint=req_turn1.hero_signature_hint,
        local_engine_top_items=req_turn1.local_engine_top_items[:2],
    )
    p5 = build_coach_prompt_payload(req_turn5, store)

    s1 = json.dumps(p1, ensure_ascii=False, separators=(",", ":"))
    s5 = json.dumps(p5, ensure_ascii=False, separators=(",", ":"))
    marker = ',"trigger_reason":'
    assert marker in s1 and marker in s5
    assert s1.split(marker, 1)[0] == s5.split(marker, 1)[0]
    assert p5["my_hero"]["kda"] == "8/3/11"


def test_special_augment_builds_extracts_parenthesized_item_ids_for_glossary(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    req = CoachRequestPayload(
        request_id="req-bork",
        trigger_reason="augment_selected",
        timestamp="2026-11-22T20:05:00+08:00",
        dataset_patch="26.23",
        my_hero={"hero_id": "164", "name": "卡蜜尔", "gold": 1900, "items": ["魄罗佳肴"]},
        special_augment_builds=[
            {
                "id": "2040",
                "name": "双刀流",
                "force_items": ["破败王者之刃(3153)"],
                "core_items": ["心之钢(773084)", "狂徒铠甲(3083)"],
            }
        ],
    )
    payload = build_coach_prompt_payload(req, store)
    assert "3153" in payload["item_glossary"]
    assert payload["item_glossary"]["3153"] == "破败王者之刃|3200"
    assert "3084" in payload["item_glossary"]
    assert "3083" in payload["item_glossary"]
    assert "special_augment_builds" in payload
    assert "hero_signature_hint" not in payload


def test_allies_self_dedup_fallback_without_store_by_items_and_kda() -> None:
    req = CoachRequestPayload(
        request_id="req-nostore",
        trigger_reason="manual_f6",
        timestamp="2026-11-22T20:10:00+08:00",
        dataset_patch="26.23",
        my_hero={
            "hero_id": "164",
            "name": "卡蜜尔",
            "kda": "7/2/9",
            "gold": 1200,
            "items": ["三相之力", "魄罗佳肴"],
        },
        allies=[
            {"champion": "青钢影", "kda": "7/2/9", "items": ["三相之力", "魄罗佳肴"]},
            {"champion": "迦娜", "kda": "1/1/20", "items": ["月石再生器", "魄罗佳肴"]},
        ],
        enemies=[
            {"champion": "青钢影", "kda": "7/2/9", "items": ["三相之力", "魄罗佳肴"]},
        ],
    )
    payload = build_coach_prompt_payload(req, store=None)
    assert [a["champion"] for a in payload["allies"]] == ["迦娜"]
    # Enemy roster is strictly untouched even if names/items coincide
    assert [e["champion"] for e in payload["enemies"]] == ["青钢影"]
    assert payload["enemies"][0]["items"] == ["三相之力"]


def test_config_evolution_defaults_auto_tier_and_no_xhigh_default(tmp_path: Path) -> None:
    assert "auto" in COACH_TIERS

    default_cfg = AppConfig()
    assert default_cfg.coach_tier == "auto"
    assert default_cfg.gemini_thinking_level == "medium"
    assert default_cfg.telemetry_enabled is True
    assert default_cfg.auto_first_prismatic_mode is True
    assert default_cfg.smart_picker_ai_gate is True

    # Never returns xhigh from resolve_thinking_level
    for trigger in ("augment_picking", "connection_test", "game_start", "augment_selected", "death_enemy_build_changed", "manual_f6"):
        for adaptive in (True, False):
            lvl = resolve_thinking_level("xhigh", trigger, adaptive=adaptive, model="gemini-4-hydrogen")
            assert lvl != "xhigh"
    assert resolve_thinking_level("xhigh", "augment_picking", adaptive=True) == "low"
    assert resolve_thinking_level("", "manual_f6", adaptive=True) == "medium"

    # from_dict validation + roundtrip
    cfg = AppConfig.from_dict(
        {
            "coach_tier": "AUTO",
            "telemetry_enabled": False,
            "auto_first_prismatic_mode": False,
            "smart_picker_ai_gate": False,
            "gemini_thinking_level": "invalid_level",
            "drawer_size": [340, 520],
        }
    )
    assert cfg.coach_tier == "auto"
    assert cfg.telemetry_enabled is False
    assert cfg.auto_first_prismatic_mode is False
    assert cfg.smart_picker_ai_gate is False
    assert cfg.gemini_thinking_level == "medium"
    assert cfg.drawer_size == [340, 520]

    cfg_path = tmp_path / "config.json"
    save_config(cfg, cfg_path)
    reloaded = load_config(cfg_path)
    assert reloaded.coach_tier == "auto"
    assert reloaded.telemetry_enabled is False
    assert reloaded.auto_first_prismatic_mode is False
    assert reloaded.smart_picker_ai_gate is False
    assert reloaded.drawer_size == [340, 520]

    # Invalid coach_tier falls back to "a"
    bad_tier_cfg = AppConfig.from_dict({"coach_tier": "turbo_s_plus"})
    assert bad_tier_cfg.coach_tier == "auto"


def test_cloud_client_auto_tier_and_routed_tier_parsing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    recorded_requests: list[dict[str, Any]] = []

    class _FakeHttp:
        def __init__(self, routed_tier: str, model: str) -> None:
            self.routed_tier = routed_tier
            self.model = model

        def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
            recorded_requests.append({"method": method, "path": path, **kwargs})
            body = {
                "request_id": kwargs["json"]["request_id"],
                "tier": self.routed_tier,
                "routed_tier": self.routed_tier,
                "model": self.model,
                "thinking_level": "medium" if self.routed_tier == "b" else "",
                "latency_ms": 88,
                "advice_json": {
                    "augment_pick_advice": "",
                    "buy_now": "直接合成【三相之力】",
                    "build_path": ["三相之力", "死亡之舞"],
                    "next_items": [
                        {"item_id": "3078", "name": "三相之力", "priority": "下件必出", "reason": "核心输出"}
                    ],
                    "tactical_summary": "拉扯等二段Q真伤",
                },
                "usage": {"input_tokens": 900, "output_tokens": 180, "thought_tokens": 120, "total_tokens": 1200},
                "wallet": {"cost_credits": 5.2, "balance_credits": 994.8},
            }
            return httpx.Response(200, json=body)

    sess_store = DpapiSessionStore(tmp_path / "session.dat")
    fake_b = _FakeHttp(routed_tier="b", model="gemini-4-hydrogen")
    client = CloudGatewayClient(
        base_url="https://cloud.example",
        session_store=sess_store,
        http_client_factory=lambda _base, _timeout: fake_b,
    )
    client._session = {"access_token": "tok-abc", "refresh_token": "rt-abc", "username": "Alan"}

    req = CoachRequestPayload(
        request_id="req-auto-b",
        trigger_reason="manual_f6",
        timestamp="2026-11-22T20:30:00+08:00",
        dataset_patch="26.23",
        my_hero={"hero_id": "164", "name": "卡蜜尔", "gold": 3400, "items": ["魄罗佳肴"]},
    )
    advice, stats = client.request_coach_advice(req, store, tier="auto")

    sent = recorded_requests[-1]
    assert sent["method"] == "POST"
    assert sent["path"] == "/v1/coach/advise"
    assert sent["json"]["tier"] == "auto"
    assert sent["json"]["request_id"] == "req-auto-b"
    assert "request_id" not in sent["json"]["prompt_payload"]
    assert "timestamp" not in sent["json"]["prompt_payload"]
    assert list(sent["json"]["prompt_payload"].keys())[0] == "dataset_patch"

    assert stats.tier == "b"
    assert stats.routed_tier == "b"
    assert stats.model == "gemini-4-hydrogen"
    assert advice.source == "云端AI · 智能路由(B档)"
    assert [it.item_id for it in advice.next_items] == ["3078"]

    # Also test routing to tier A via CloudCoachWorker.submit(tier="auto")
    fake_b.routed_tier = "a"
    fake_b.model = "deepseek-flash"
    done_results: list[tuple[Any, Any]] = []
    import threading

    done_ev = threading.Event()
    worker = CloudCoachWorker(
        client=client,
        on_advice=lambda adv, st: (done_results.append((adv, st)), done_ev.set()),
    )
    try:
        worker.submit(req, store, tier="auto")
        assert done_ev.wait(timeout=3.0)
        adv_a, st_a = done_results[0]
        assert st_a.tier == "a"
        assert st_a.routed_tier == "a"
        assert adv_a.source == "云端AI · 智能路由(A档)"
    finally:
        worker.stop()


def test_cloud_telemetry_submit_and_fetch_resilience(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class _ScriptedTelemetryHttp:
        def __init__(self) -> None:
            self.mode = "ok_dict"

        def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
            calls.append({"method": method, "path": path, **kwargs})
            if self.mode == "timeout":
                raise httpx.ConnectTimeout("timed out")
            if self.mode == "connect_error":
                raise httpx.ConnectError("connection refused")
            if self.mode == "http_500":
                return httpx.Response(500, json={"detail": "boom"})
            if self.mode == "bad_json":
                return httpx.Response(200, content=b"<html>not json</html>")
            if path == "/v1/telemetry/match":
                return httpx.Response(202, json={"accepted": True})
            if self.mode == "ok_list":
                return httpx.Response(200, json=[{"pair": ["1001", "1002"], "games": 12, "win_rate": 0.61}])
            return httpx.Response(
                200,
                json={"pairs": [{"pair": ["3018", "3019"], "games": 8, "win_rate": 0.58}]},
            )

    http_stub = _ScriptedTelemetryHttp()
    client = CloudGatewayClient(
        base_url="https://cloud.example",
        session_store=DpapiSessionStore(tmp_path / "sess.dat"),
        http_client_factory=lambda _b, _t: http_stub,
    )

    sample_match = {"hero_id": "164", "win": True, "augments": ["1001", "1002"]}

    # 1) Anonymous telemetry upload (not logged in -> no Authorization header, timeout=5.0)
    assert client.submit_match_telemetry(sample_match) is True
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["path"] == "/v1/telemetry/match"
    assert calls[-1]["timeout"] == 5.0
    assert "Authorization" not in calls[-1]["headers"]

    # 2) Authenticated telemetry upload (logged in -> Bearer token attached)
    client._session = {"access_token": "jwt-live-token", "refresh_token": "rt", "username": "Alan"}
    assert client.submit_match_telemetry(sample_match) is True
    assert calls[-1]["headers"]["Authorization"] == "Bearer jwt-live-token"

    # 3) Empty payload short-circuits cleanly without error
    assert client.submit_match_telemetry({}) is False

    # 4) Fetch augment pair stats (wrapped dict + raw list)
    pairs_wrapped = client.fetch_augment_pair_stats(hero_id="164", min_games=5)
    assert calls[-1]["method"] == "GET"
    assert calls[-1]["path"] == "/v1/telemetry/augment-pairs"
    assert calls[-1]["params"] == {"min_games": 5, "hero_id": "164"}
    assert calls[-1]["timeout"] == 5.0
    assert pairs_wrapped == [{"pair": ["3018", "3019"], "games": 8, "win_rate": 0.58}]

    http_stub.mode = "ok_list"
    pairs_list = client.fetch_augment_pair_stats()
    assert calls[-1]["params"] == {"min_games": 3}
    assert pairs_list == [{"pair": ["1001", "1002"], "games": 12, "win_rate": 0.61}]

    # 5) Resilience: HTTP 500, malformed HTML, timeout, connect error NEVER raise!
    for failure_mode in ("http_500", "bad_json", "timeout", "connect_error"):
        http_stub.mode = failure_mode
        if failure_mode != "bad_json":
            assert client.submit_match_telemetry(sample_match) is False
        assert client.fetch_augment_pair_stats(hero_id="164", min_games=3) == []


def test_prompt_payload_strips_cheap_roster_components_keeps_strategic_and_self_items(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.static.items.update(
        {
            "1036": ItemStaticInfo("1036", "长剑", 350, tags=("Damage",), category="other"),
            "1028": ItemStaticInfo("1028", "红水晶", 400, tags=("Health",), category="other"),
            "1052": ItemStaticInfo("1052", "增幅典籍", 400, tags=("SpellDamage",), category="other"),
            "1029": ItemStaticInfo("1029", "布甲", 300, tags=("Armor",), category="other"),
            "3020": ItemStaticInfo("3020", "法师之靴", 1100, tags=("Boots", "MagicPenetration"), category="other"),
            "3158": ItemStaticInfo("3158", "明朗之靴", 900, tags=("Boots",), category="other"),
            "3070": ItemStaticInfo("3070", "女神之泪", 400, tags=("Mana",), category="other"),
            "3123": ItemStaticInfo("3123", "死刑宣告", 800, tags=("Damage",), category="other", has_grievous=True),
            "3916": ItemStaticInfo("3916", "湮灭宝珠", 800, tags=("SpellDamage",), category="other", has_grievous=True),
            "3076": ItemStaticInfo("3076", "棘刺背心", 800, tags=("Armor",), category="other", has_grievous=True),
            "3140": ItemStaticInfo("3140", "水银饰带", 1300, tags=("SpellBlock",), category="other"),
            "2420": ItemStaticInfo("2420", "探索者的护臂", 1100, tags=("Armor",), category="other"),
        }
    )
    req = CoachRequestPayload(
        request_id="req-roster-slim",
        trigger_reason="death_enemy_build_changed",
        timestamp="2026-11-23T01:00:00+08:00",
        dataset_patch="26.23",
        my_hero={
            "hero_id": "164",
            "name": "卡蜜尔",
            "gold": 1200,
            "items": ["三相之力", "长剑", "红水晶", "守护者之锤", "魄罗佳肴"],
        },
        allies=[
            {"champion": "青钢影", "kda": "3/2/5", "items": ["三相之力", "长剑", "红水晶", "守护者之锤", "魄罗佳肴"], "spells": ["闪现", "雪球"], "respawn_s": 20},
            {"champion": "拉克丝", "kda": "4/1/9", "items": ["法师之靴", "女神之泪", "增幅典籍", "红水晶", "魄罗佳肴"], "spells": ["闪现", "清晰术"], "respawn_s": 0},
            {"champion": "金克丝", "kda": "5/3/4", "items": ["死刑宣告", "长剑", "布甲", "水银之靴"], "spells": ["闪现", "治疗术"], "respawn_s": 12},
        ],
        enemies=[
            {"champion": "亚托克斯", "kda": "6/2/3", "items": ["棘刺背心", "长剑", "红水晶", "明朗之靴", "守护者之锤"], "spells": ["雪球", "闪现"], "respawn_s": 15},
            {"champion": "弗拉基米尔", "kda": "4/3/6", "items": ["湮灭宝珠", "水银饰带", "探索者的护臂", "增幅典籍"], "spells": ["疾跑", "闪现"], "respawn_s": 0},
        ],
    )
    payload = build_coach_prompt_payload(req, store)

    # Self keeps ALL cheap owned components intact (only 魄罗佳肴 stripped)
    assert payload["my_hero"]["items"] == ["三相之力", "长剑", "红水晶", "守护者之锤"]

    # Allies: cheap base components stripped; boots & strategic tear/GW kept; respawn_s & routine ally spells stripped
    allies_by_name = {a["champion"]: a for a in payload["allies"]}
    assert "青钢影" not in allies_by_name
    assert allies_by_name["拉克丝"]["items"] == ["法师之靴", "女神之泪"]
    assert allies_by_name["金克丝"]["items"] == ["死刑宣告", "水银之靴"]
    assert "respawn_s" not in allies_by_name["拉克丝"]
    assert "spells" not in allies_by_name["拉克丝"]

    # Enemies: cheap base components stripped; boots/GW/QSS/stopwatch kept; enemy spells preserved, respawn_s stripped
    enemies_by_name = {e["champion"]: e for e in payload["enemies"]}
    assert enemies_by_name["亚托克斯"]["items"] == ["棘刺背心", "明朗之靴"]
    assert enemies_by_name["亚托克斯"]["spells"] == ["雪球", "闪现"]
    assert "respawn_s" not in enemies_by_name["亚托克斯"]
    assert enemies_by_name["弗拉基米尔"]["items"] == ["湮灭宝珠", "水银饰带", "探索者的护臂"]


def test_special_builds_and_empty_pruning_by_trigger_reason(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    special = [
        {
            "id": "1045",
            "name": "炼狱导管",
            "archetype": "双烧流",
            "mechanic": "技能附加可叠加灼烧减CD",
            "force_items": ["破败王者之刃(3153)"],
            "core_items": ["死亡之舞(6333)"],
            "avoid_note": "不要乱出纯肉",
            "playstyle_tip": "远距离持续挂烧刷新技能",
        }
    ]
    combo = {
        "round_index": 3,
        "direction": "战士连击流",
        "primary_profile": "AD",
        "penalized_axes": ["纯AP"],
        "active_combos": [{"name": "连击核弹", "gamble": False, "completed": False, "chase": ["秘术冲拳"]}],
        "top_targets": [],
    }
    sig = {
        "archetype": "单带爆发战士",
        "signature_items": ["三相之力(3078)"],
        "avoid_passive": [],
        "note": "  ",
    }
    plan = {
        "owned_complete": ["三相之力"],
        "locked_next_item": {},
        "remaining_plan": [],
        "allow_overhaul": False,
        "overhaul_reason": "旧的变阵原因",
    }

    req_death = CoachRequestPayload(
        request_id="r-death",
        trigger_reason="death_enemy_build_changed",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "164", "name": "卡蜜尔", "gold": 1400, "items": ["三相之力"]},
        special_augment_builds=special,
        augment_combo_context=combo,
        hero_signature_hint=sig,
        committed_build_plan=plan,
    )
    p_death = build_coach_prompt_payload(req_death, store)
    sp_death = p_death["special_augment_builds"][0]
    assert "mechanic" not in sp_death
    assert "playstyle_tip" not in sp_death
    assert sp_death["force_items"] == ["破败王者之刃(3153)"]
    assert sp_death["avoid_note"] == "不要乱出纯肉"
    assert "avoid_passive" not in p_death["hero_signature_hint"]
    assert "note" not in p_death["hero_signature_hint"]
    assert "locked_next_item" not in p_death["committed_build_plan"]
    assert "remaining_plan" not in p_death["committed_build_plan"]
    assert "overhaul_reason" not in p_death["committed_build_plan"]
    assert "top_targets" not in p_death["augment_combo_context"]
    assert "penalized_axes" not in p_death["augment_combo_context"]

    # Non-death triggers keep full special_augment_builds prose & combo targets
    for full_trigger in ("game_start", "augment_picking", "augment_selected", "manual_f6"):
        req_full = CoachRequestPayload(
            request_id=f"r-{full_trigger}",
            trigger_reason=full_trigger,
            timestamp="ts",
            dataset_patch="26.23",
            my_hero={"hero_id": "164", "name": "卡蜜尔", "gold": 1400, "items": ["三相之力"]},
            special_augment_builds=special,
            augment_combo_context=combo,
            hero_signature_hint=sig,
        )
        p_full = build_coach_prompt_payload(req_full, store)
        sp_full = p_full["special_augment_builds"][0]
        assert sp_full["mechanic"] == "技能附加可叠加灼烧减CD"
        assert sp_full["playstyle_tip"] == "远距离持续挂烧刷新技能"
        assert p_full["augment_combo_context"]["penalized_axes"] == ["纯AP"]
        assert "top_targets" not in p_full["augment_combo_context"]


def test_opening_brief_protocol_schema_and_grounding(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    # Schema exposes optional opening_brief with 3 keys, NOT in required
    assert "opening_brief" in COACH_RESPONSE_SCHEMA["properties"]
    assert "opening_brief" not in COACH_RESPONSE_SCHEMA["required"]
    ob_props = COACH_RESPONSE_SCHEMA["properties"]["opening_brief"]["properties"]
    assert set(ob_props.keys()) == {"enemy_threat", "augment_goal", "opening_buy"}

    # Protocol backward compatibility when opening_brief absent
    legacy = CoachAdvicePayload.from_dict(
        {
            "request_id": "req-legacy",
            "buy_now": "先买耀光",
            "build_path": ["三相之力"],
            "next_items": [{"item_id": "3078", "name": "三相之力", "priority": "下件必出", "reason": "核心"}],
            "tactical_summary": "拉扯输出",
        }
    )
    assert legacy.opening_brief == {}
    assert legacy.to_dict()["opening_brief"] == {}

    # Protocol roundtrip with opening_brief trims whitespace and drops empty/extra keys
    with_brief = CoachAdvicePayload.from_dict(
        {
            "request_id": "req-open-1",
            "buy_now": "守护者战锤+鞋子",
            "opening_brief": {
                "enemy_threat": "  敌双AP消耗+剑魔高回复  ",
                "augment_goal": "追秘术冲拳/技能极速流",
                "opening_buy": "   ",
                "extra_ignored": "should drop",
            },
        }
    )
    assert with_brief.opening_brief == {
        "enemy_threat": "敌双AP消耗+剑魔高回复",
        "augment_goal": "追秘术冲拳/技能极速流",
    }
    assert CoachAdvicePayload.from_dict(with_brief.to_dict()).opening_brief == with_brief.opening_brief

    # ground_coach_advice preserves normalized opening_brief on CoachAdvicePayload
    req = CoachRequestPayload(
        request_id="req-open-ground",
        trigger_reason="game_start",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "164", "name": "卡蜜尔", "gold": 1400, "items": []},
    )
    grounded = ground_coach_advice(
        {
            "augment_pick_advice": "",
            "buy_now": "守护者之锤+长剑",
            "build_path": ["三相之力", "死亡之舞"],
            "next_items": [{"item_id": "3078", "name": "三相之力", "priority": "下件必出", "reason": "核心"}],
            "tactical_summary": "卡视野E墙开脆皮",
            "opening_brief": {
                "enemy_threat": "  敌方双AP长手+高回复  ",
                "augment_goal": " 追秘术冲拳或重击流 ",
                "opening_buy": " 1400G守护者之锤转三相 ",
                "unknown": "drop",
            },
        },
        req,
        store,
    )
    assert grounded.opening_brief == {
        "enemy_threat": "敌方双AP长手+高回复",
        "augment_goal": "追秘术冲拳或重击流",
        "opening_buy": "1400G守护者之锤转三相",
    }

    # Absent opening_brief grounds to {}
    grounded_empty = ground_coach_advice(
        {
            "augment_pick_advice": "",
            "buy_now": "先买耀光",
            "build_path": ["三相之力"],
            "next_items": [{"item_id": "3078", "name": "三相之力", "priority": "下件必出", "reason": "核心"}],
            "tactical_summary": "拉扯等二段Q",
        },
        req,
        store,
    )
    assert grounded_empty.opening_brief == {}


def test_commercial_default_coach_tier_auto_and_explicit_overrides(monkeypatch) -> None:
    monkeypatch.setenv("HEX_EDITION", "dev")
    assert _default_coach_tier() == "auto"
    assert AppConfig().coach_tier == "auto"

    monkeypatch.setenv("HEX_EDITION", "commercial")
    assert _default_coach_tier() == "auto"
    assert AppConfig().coach_tier == "auto"
    assert AppConfig.from_dict({}).coach_tier == "auto"
    assert AppConfig.from_dict({"coach_tier": "invalid_tier"}).coach_tier == "auto"
    assert AppConfig.from_dict({"coach_tier": "a"}).coach_tier == "a"
    assert AppConfig.from_dict({"coach_tier": "B"}).coach_tier == "b"
    assert AppConfig.from_dict({"coach_tier": "AUTO"}).coach_tier == "auto"


def test_camille_request_payload_shrinks_at_least_25_percent_and_prompt_compressed(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.static.items.update(
        {
            "1036": ItemStaticInfo("1036", "长剑", 350, tags=("Damage",), category="other"),
            "1028": ItemStaticInfo("1028", "红水晶", 400, tags=("Health",), category="other"),
            "1052": ItemStaticInfo("1052", "增幅典籍", 400, tags=("SpellDamage",), category="other"),
            "1029": ItemStaticInfo("1029", "布甲", 300, tags=("Armor",), category="other"),
            "1033": ItemStaticInfo("1033", "抗魔斗篷", 400, tags=("SpellBlock",), category="other"),
            "1042": ItemStaticInfo("1042", "短剑", 250, tags=("AttackSpeed",), category="other"),
            "3020": ItemStaticInfo("3020", "法师之靴", 1100, tags=("Boots",), category="other"),
            "3047": ItemStaticInfo("3047", "铁板靴", 1200, tags=("Boots",), category="other"),
            "3006": ItemStaticInfo("3006", "狂战士胫甲", 1100, tags=("Boots",), category="other"),
            "3123": ItemStaticInfo("3123", "死刑宣告", 800, tags=("Damage",), category="other", has_grievous=True),
            "3076": ItemStaticInfo("3076", "棘刺背心", 800, tags=("Armor",), category="other", has_grievous=True),
        }
    )

    camille_req = CoachRequestPayload(
        request_id="hex-camille-bench",
        trigger_reason="death_enemy_build_changed",
        timestamp="2026-11-23T01:15:00+08:00",
        dataset_patch="26.23（内部 16.23）",
        my_hero={
            "hero_id": "164",
            "name": "卡蜜尔",
            "win_rate": 0.515,
            "gold": 1750,
            "is_dead": True,
            "items": ["三相之力", "水银之靴", "长剑", "红水晶", "魄罗佳肴"],
            "game_time_s": 540,
            "spells": ["闪现", "雪球"],
            "respawn_s": 22,
        },
        selected_augments=[
            {"id": "1045", "name": "炼狱导管", "rarity": "prismatic"},
            {"id": "1060", "name": "海洋龙魂", "rarity": "silver"},
        ],
        allies=[
            {"champion": "青钢影", "kda": "5/3/8", "items": ["三相之力", "水银之靴", "长剑", "红水晶", "魄罗佳肴"], "spells": ["闪现", "雪球"], "respawn_s": 22},
            {"champion": "拉克丝", "kda": "6/2/12", "items": ["法师之靴", "增幅典籍", "增幅典籍", "红水晶", "魄罗佳肴"], "spells": ["闪现", "清晰术"], "respawn_s": 0},
            {"champion": "盲僧", "kda": "4/5/9", "items": ["铁板靴", "长剑", "布甲", "红水晶", "魄罗佳肴"], "spells": ["雪球", "闪现"], "respawn_s": 14},
            {"champion": "金克丝", "kda": "5/4/7", "items": ["狂战士胫甲", "长剑", "短剑", "死刑宣告", "魄罗佳肴"], "spells": ["闪现", "治疗术"], "respawn_s": 0},
            {"champion": "锤石", "kda": "1/5/16", "items": ["水银之靴", "红水晶", "抗魔斗篷", "布甲", "魄罗佳肴"], "spells": ["雪球", "闪现"], "respawn_s": 8},
        ],
        enemies=[
            {"champion": "亚托克斯", "kda": "7/3/5", "items": ["铁板靴", "长剑", "红水晶", "棘刺背心", "魄罗佳肴"], "spells": ["雪球", "闪现"], "respawn_s": 0},
            {"champion": "弗拉基米尔", "kda": "5/4/8", "items": ["法师之靴", "增幅典籍", "红水晶", "抗魔斗篷", "魄罗佳肴"], "spells": ["疾跑", "闪现"], "respawn_s": 12},
            {"champion": "凯特琳", "kda": "4/4/6", "items": ["狂战士胫甲", "长剑", "短剑", "布甲", "魄罗佳肴"], "spells": ["闪现", "屏障"], "respawn_s": 0},
            {"champion": "卡尔玛", "kda": "1/4/14", "items": ["法师之靴", "增幅典籍", "红水晶", "魄罗佳肴"], "spells": ["闪现", "虚弱"], "respawn_s": 0},
            {"champion": "莫甘娜", "kda": "2/6/10", "items": ["法师之靴", "增幅典籍", "红水晶", "魄罗佳肴"], "spells": ["闪现", "雪球"], "respawn_s": 19},
        ],
        threat_summary=["敌方高回复(亚托克斯/弗拉基米尔)"],
        local_engine_top_items=[
            {"item_id": "6333", "name": "死亡之舞", "price": 3300, "effective_cost": 2950, "affordable": False, "buy_now_hint": "先买考尔菲德战锤", "reasons": ["核心高胜率", "抗敌方物理爆发", "战士中盘质变", "第4条截断"]},
            {"item_id": "3033", "name": "凡性的提醒", "price": 3000, "effective_cost": 3000, "affordable": False, "buy_now_hint": "", "reasons": ["克制敌方高回复", "穿甲补输出", "第3条截断"]},
            {"item_id": "3083", "name": "狂徒铠甲", "price": 3100, "effective_cost": 3100, "affordable": False, "buy_now_hint": "", "reasons": ["续航", "撑血量", "第3条截断"]},
            {"item_id": "3084", "name": "心之钢", "price": 3000, "effective_cost": 3000, "affordable": False, "buy_now_hint": "", "reasons": ["叠血量", "前排容错", "第3条截断"]},
        ],
        committed_build_plan={
            "owned_complete": ["三相之力"],
            "locked_next_item": {"item_id": "6333", "name": "死亡之舞"},
            "remaining_plan": ["死亡之舞", "凡性的提醒"],
            "allow_overhaul": False,
            "overhaul_reason": "海克斯变阵：旧理由",
        },
        special_augment_builds=[
            {
                "id": "1045",
                "name": "炼狱导管",
                "archetype": "双烧无限减CD流",
                "mechanic": "技能附加可叠加灼烧，所有灼烧伤害跳动均缩减基础技能冷却",
                "force_items": ["破败王者之刃(3153)"],
                "core_items": ["死亡之舞(6333)"],
                "avoid_note": "物理战士慎转纯AP",
                "playstyle_tip": "靠持续跳烧频繁刷新基础技能拉扯进出",
            }
        ],
        hero_signature_hint={
            "archetype": "单带爆发战士",
            "starter_items": ["守护者之锤(3184)"],
            "signature_items": ["三相之力(3078)", "死亡之舞(6333)"],
            "avoid_passive": [],
            "note": "核心三相之力接死亡之舞",
        },
        augment_combo_context={
            "round_index": 2,
            "direction": "战士技能循环",
            "primary_profile": "AD",
            "penalized_axes": ["纯AP", "护盾辅助"],
            "active_combos": [{"name": "技能连击", "gamble": False, "completed": False, "chase": ["秘术冲拳", "老练狙神"]}],
            "top_targets": [],
        },
    )

    # Pre-slimming baseline: dict glossary {"name": ..., "gold": ...}, full raw request fields
    pre_glossary = {
        iid: {"name": info.name, "gold": info.gold}
        for iid, info in store.static.items.items()
        if info.gold > 0
    }
    pre_payload = camille_req.to_dict()
    pre_payload["hero_big_data_top_builds"] = [
        {"items": ["三相之力(3078)", "死亡之舞(6333)", "凡性的提醒(3033)"], "win_rate": 0.54, "pick_rate": 0.22, "games": 4500}
    ]
    pre_payload["item_glossary"] = pre_glossary
    pre_len = len(json.dumps(pre_payload, ensure_ascii=False, separators=(",", ":")))

    slim_payload = build_coach_prompt_payload(camille_req, store)
    slim_len = len(json.dumps(slim_payload, ensure_ascii=False, separators=(",", ":")))
    reduction_ratio = 1.0 - (slim_len / pre_len)
    assert reduction_ratio >= 0.25, f"Expected >=25% payload reduction, got {reduction_ratio:.1%} ({slim_len} vs {pre_len})"

    # System instruction compressed >=35% from 1835 chars while containing Rule 8 (opening_brief / game_start)
    assert len(COACH_SYSTEM_INSTRUCTION) <= int(1835 * 0.65)
    assert "opening_brief" in COACH_SYSTEM_INSTRUCTION
    assert "game_start" in COACH_SYSTEM_INSTRUCTION
