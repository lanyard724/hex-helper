"""Tests for pure read-only LCU 10-player LobbyScout (ratings, premade detection, hidden-history fallback, AI export)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import httpx

from hex_helper.bridge.protocol import CoachRequestPayload
from hex_helper.coach.gemini_coach import COACH_SYSTEM_INSTRUCTION, build_coach_prompt_payload
from hex_helper.data.provider import DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.lcu_client import LcuClient
from hex_helper.game.lobby_scout import LobbyScoutSnapshot, PlayerScoutCard, scout_lobby_readonly
from hex_helper.game.models import LcuCredentials


class _FakeReadOnlyLcu:
    def __init__(self, current_puuid: str = "") -> None:
        self.write_calls = 0
        self._current_puuid = current_puuid

    def get_current_summoner(self) -> dict[str, Any] | None:
        if not self._current_puuid:
            return None
        return {"puuid": self._current_puuid}

    def get_gameflow_session(self) -> dict[str, Any]:
        return {
            "gameData": {
                "teamOne": [
                    {"puuid": "p1", "summonerName": "我方人马", "championId": 120},
                    {"puuid": "p2", "summonerName": "队友B", "championId": 236},
                ],
                "teamTwo": [
                    {"puuid": "e1", "summonerName": "敌方辛德拉", "championId": 134},
                    {"puuid": "e2", "summonerName": "敌方黑店B", "championId": 55},
                    {"puuid": "e3", "summonerName": "隐藏哥", "championId": 1},
                ],
            }
        }

    def get_ranked_stats(self, puuid: str) -> dict[str, Any] | None:
        tiers = {
            "p1": {"queueMap": {"RANKED_SOLO_5x5": {"tier": "MASTER", "division": "I"}}},
            "p2": {"queueMap": {"RANKED_SOLO_5x5": {"tier": "GOLD", "division": "II"}}},
            "e1": {"queueMap": {"RANKED_SOLO_5x5": {"tier": "DIAMOND", "division": "I"}}},
            "e2": {"queueMap": {"RANKED_SOLO_5x5": {"tier": "PLATINUM", "division": "II"}}},
            "e3": {"queueMap": {"RANKED_SOLO_5x5": {"tier": "EMERALD", "division": "IV"}}},
        }
        return tiers.get(puuid)

    def get_match_history(self, puuid: str, count: int = 15) -> list[dict[str, Any]]:
        if puuid == "e3":
            return []  # Simulates CN hidden match history
        if puuid == "p1":
            return [
                {
                    "gameId": 9000 + i,
                    "queueId": 2400,
                    "participants": [{"stats": {"win": True, "kills": 14, "deaths": 4, "assists": 18}}],
                }
                for i in range(10)
            ]
        if puuid in ("e1", "e2"):
            # Share recent gameIds -> detected as enemy duo premade
            return [
                {
                    "gameId": 7000 + i,
                    "queueId": 2400,
                    "participants": [{"stats": {"win": i < 7, "kills": 11, "deaths": 5, "assists": 12}}],
                }
                for i in range(10)
            ]
        return [
            {
                "gameId": 8000 + i,
                "queueId": 450,
                "participants": [{"stats": {"win": i % 2 == 0, "kills": 6, "deaths": 7, "assists": 10}}],
            }
            for i in range(6)
        ]


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "cache")
    static.items = {
        "3078": ItemStaticInfo("3078", "三相之力", 3333, tags=("Damage",), category="complete_equipment"),
    }
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "datasets/16.23/normal", "2026-11-23", True)
    store._heroes["120"] = HeroStat("120", "战争之影", "赫卡里姆", 50000, 26000, 0.52, 0.04, 15, False)
    store._heroes["236"] = HeroStat("236", "圣枪游侠", "卢锡安", 50000, 25500, 0.51, 0.04, 22, False)
    store._heroes["134"] = HeroStat("134", "暗黑元首", "辛德拉", 50000, 26500, 0.53, 0.05, 8, False)
    store._heroes["55"] = HeroStat("55", "不祥之刃", "卡特琳娜", 50000, 25000, 0.50, 0.04, 30, False)
    store._heroes["1"] = HeroStat("1", "黑暗之女", "安妮", 50000, 25500, 0.51, 0.03, 25, False)
    return store


def test_lobby_scout_computes_mvp_premade_and_graceful_hidden_fallback() -> None:
    lcu = _FakeReadOnlyLcu()
    snap = scout_lobby_readonly(lcu, store=None, my_puuid="p1", pace_s=0.0)  # type: ignore[arg-type]

    assert snap is not None
    assert isinstance(snap, LobbyScoutSnapshot)
    assert snap.mvp_card is not None
    assert snap.mvp_card.puuid == "p1"
    assert snap.mvp_card.is_mvp_carry is True
    assert snap.mvp_card.streak_tag == "10连胜"
    assert snap.mvp_card.rank_label == "大师"
    assert snap.mvp_lead_gap > 150
    assert snap.top_enemy_card is not None
    assert snap.top_enemy_card.puuid == "e1"
    assert snap.top_enemy_card.rank_label == "钻一"
    assert snap.degraded_count == 1

    e3_card = next(c for c in snap.enemy_team if c.puuid == "e3")
    assert isinstance(e3_card, PlayerScoutCard)
    assert e3_card.hidden_history is True
    assert e3_card.hex_rating > 1500  # Fallback still estimates from rank tier
    assert 1200 <= e3_card.hex_rating <= 4200

    # Enemy duo premade detected from shared gameIds
    e1_card = next(c for c in snap.enemy_team if c.puuid == "e1")
    e2_card = next(c for c in snap.enemy_team if c.puuid == "e2")
    assert e1_card.premade_group != "" and e1_card.premade_group == e2_card.premade_group
    assert any("敌方2人车队" in note for note in snap.premade_notes)

    # Exports cleanly into CoachRequestPayload
    ctx = snap.to_coach_context()
    assert ctx["top_enemy_rating"] == e1_card.hex_rating
    assert ctx["ally_mvp_rating"] == snap.mvp_card.hex_rating
    assert len(ctx["enemy_premades"]) == 1

    req = CoachRequestPayload(
        request_id="r1",
        trigger_reason="game_start",
        timestamp="2026-11-23T21:00:00+08:00",
        dataset_patch="16.23",
        my_hero={"name": "人马"},
        lobby_scout=ctx,
    )
    restored = CoachRequestPayload.from_dict(req.to_dict())
    assert restored.lobby_scout["top_enemy_rating"] == e1_card.hex_rating


def test_lobby_scout_zero_pii_and_prompt_payload_wiring(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    lcu = _FakeReadOnlyLcu()
    snap = scout_lobby_readonly(lcu, store=store, my_puuid="p1", pace_s=0.0)  # type: ignore[arg-type]
    assert snap is not None

    ctx = snap.to_coach_context()
    assert ctx["top_enemy_champion"] == "暗黑元首"
    assert ctx["ally_mvp_champion"] == "战争之影"
    assert "钻一" in ctx["top_enemy_tag"]
    assert any("暗黑元首" in p and "不祥之刃" in p for p in ctx["enemy_premades"])

    # Strictly zero raw PUUIDs or summoner names anywhere in exported coach context
    serialized_ctx = json.dumps(ctx, ensure_ascii=False)
    for forbidden in ("puuid", "p1", "p2", "e1", "e2", "e3", "我方人马", "队友B", "敌方辛德拉", "敌方黑店B", "隐藏哥"):
        assert forbidden not in serialized_ctx

    # Backward compatibility: legacy dict missing lobby_scout defaults to {}
    legacy_req = CoachRequestPayload.from_dict(
        {
            "request_id": "legacy-1",
            "trigger_reason": "game_start",
            "timestamp": "2026-11-23T21:00:00+08:00",
            "dataset_patch": "16.23",
            "my_hero": {"hero_id": "120", "name": "战争之影"},
        }
    )
    assert legacy_req.lobby_scout == {}
    legacy_prompt = build_coach_prompt_payload(legacy_req, store)
    assert "lobby_scout" not in legacy_prompt

    # Prompt payload injects sanitized lobby_scout in volatile tail & strips accidental PII keys
    dirty_ctx = {**ctx, "puuid": "secret-puuid-123", "summoner_name": "SecretName#CN1"}
    req = CoachRequestPayload(
        request_id="r-scout-1",
        trigger_reason="game_start",
        timestamp="2026-11-23T21:00:00+08:00",
        dataset_patch="16.23",
        my_hero={"hero_id": "120", "name": "战争之影", "gold": 1400, "items": []},
        lobby_scout=dirty_ctx,
    )
    prompt = build_coach_prompt_payload(req, store)
    keys = list(prompt.keys())
    assert keys[:3] == ["dataset_patch", "hero_static", "hero_big_data_top_builds"]
    assert keys.index("lobby_scout") > keys.index("trigger_reason")
    assert prompt["lobby_scout"]["top_enemy_champion"] == "暗黑元首"
    assert "puuid" not in prompt["lobby_scout"]
    assert "summoner_name" not in prompt["lobby_scout"]
    assert "lobby_scout" in COACH_SYSTEM_INSTRUCTION


def test_lobby_scout_team_two_auto_detect_loss_streak_and_empty_session(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    class _TeamTwoLcu(_FakeReadOnlyLcu):
        def get_match_history(self, puuid: str, count: int = 15) -> list[dict[str, Any]]:
            if puuid == "e2":
                return [
                    {
                        "gameId": 6000 + i,
                        "queueId": 2400,
                        "participants": [{"stats": {"win": False, "kills": 2, "deaths": 10, "assists": 4}}],
                    }
                    for i in range(4)
                ]
            return super().get_match_history(puuid, count=count)

    # Auto-detects my_puuid via get_current_summoner() -> e1 is on teamTwo (CHAOS)
    lcu_t2 = _TeamTwoLcu(current_puuid="e1")
    snap = scout_lobby_readonly(lcu_t2, store=store, my_puuid="", pace_s=0.0)  # type: ignore[arg-type]
    assert snap is not None
    assert all(c.team == "CHAOS" for c in snap.my_team)
    assert all(c.team == "ORDER" for c in snap.enemy_team)
    e2_card = next(c for c in snap.my_team if c.puuid == "e2")
    assert e2_card.streak_tag == "4连败"
    assert snap.top_enemy_card is not None and snap.top_enemy_card.puuid == "p1"

    # None session degrades cleanly to None
    class _EmptyLcu:
        def get_gameflow_session(self) -> None:
            return None

    assert scout_lobby_readonly(_EmptyLcu(), store=store, pace_s=0.0) is None  # type: ignore[arg-type]

    # All-hidden / unranked lobby degrades gracefully without errors
    class _AllHiddenLcu:
        def get_gameflow_session(self) -> dict[str, Any]:
            return {
                "gameData": {
                    "teamOne": [{"puuid": f"a{i}", "championId": 120} for i in range(5)],
                    "teamTwo": [{"puuid": f"b{i}", "championId": 134} for i in range(5)],
                }
            }

        def get_ranked_stats(self, _puuid: str) -> None:
            return None

        def get_match_history(self, _puuid: str, count: int = 15) -> list[dict[str, Any]]:
            return []

    degraded = scout_lobby_readonly(_AllHiddenLcu(), store=store, pace_s=0.0)  # type: ignore[arg-type]
    assert degraded is not None
    assert degraded.degraded_count == 10
    assert all(c.hidden_history and c.hex_rating == 1900 and c.rank_label == "未定级" for c in degraded.my_team + degraded.enemy_team)
    assert degraded.mvp_lead_gap == 0


def test_lcu_client_new_getters_strictly_get_only_and_403_resilient() -> None:
    recorded: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.method, str(request.url.raw_path.decode("ascii"))))
        path = request.url.path
        if path == "/lol-gameflow/v1/session":
            return httpx.Response(200, json={"gameData": {"teamOne": [], "teamTwo": []}})
        if path == "/lol-summoner/v1/current-summoner":
            return httpx.Response(200, json={"puuid": "local-puuid-1"})
        if path == "/lol-ranked/v1/ranked-stats/p1":
            return httpx.Response(200, json={"queueMap": {"RANKED_SOLO_5x5": {"tier": "DIAMOND", "division": "II"}}})
        if path == "/lol-match-history/v1/products/lol/p1/matches":
            return httpx.Response(200, json={"games": {"games": [{"gameId": 101, "queueId": 2400}]}})
        if path == "/lol-match-history/v1/products/lol/hidden/matches":
            return httpx.Response(403, json={"message": "private"})
        return httpx.Response(404)

    lcu = LcuClient()
    lcu.creds = LcuCredentials(pid=1234, port=55555, password="secret", protocol="https")
    lcu._client = httpx.Client(
        base_url="https://127.0.0.1:55555",
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )

    try:
        assert lcu.get_gameflow_session() == {"gameData": {"teamOne": [], "teamTwo": []}}
        assert lcu.get_current_summoner() == {"puuid": "local-puuid-1"}
        assert lcu.get_ranked_stats("p1") == {"queueMap": {"RANKED_SOLO_5x5": {"tier": "DIAMOND", "division": "II"}}}
        assert lcu.get_match_history("p1", count=15) == [{"gameId": 101, "queueId": 2400}]

        # 403 hidden profile returns [] gracefully WITHOUT tearing down the LCU client connection
        assert lcu.get_match_history("hidden", count=10) == []
        assert lcu._client is not None
        assert lcu.creds is not None

        # Empty PUUID makes zero requests
        count_before = len(recorded)
        assert lcu.get_ranked_stats("") is None
        assert lcu.get_match_history("   ") == []
        assert len(recorded) == count_before

        assert recorded, "Expected GET requests to be recorded"
        assert all(method == "GET" for method, _ in recorded)
        assert any("begIndex=0&endIndex=14" in p for _, p in recorded)
    finally:
        lcu.close()


def test_lcu_and_lobby_scout_have_zero_write_http_calls_ast() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    forbidden_methods = {"post", "put", "patch", "delete"}
    for rel in ("src/hex_helper/game/lcu_client.py", "src/hex_helper/game/lobby_scout.py"):
        src = (repo_root / rel).read_text(encoding="utf-8")
        tree = ast.parse(src, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr.lower() not in forbidden_methods, (
                    f"Forbidden HTTP write call .{node.func.attr}() found in {rel}:{node.lineno}"
                )


def test_server_coach_gateway_surfaces_lobby_scout_callout() -> None:
    server_root = Path(__file__).resolve().parent.parent / "server"
    if str(server_root) not in sys.path:
        sys.path.insert(0, str(server_root))

    from hex_server.coach_gateway import (
        COACH_SYSTEM_INSTRUCTION as SERVER_SYS_PROMPT,
        ServerCoachGateway,
    )

    assert "lobby_scout" in SERVER_SYS_PROMPT
    assert len(SERVER_SYS_PROMPT) <= int(2219 * 0.65)

    captured_gem: list[dict[str, Any]] = []

    def gem_factory(_key: str, _proxy: str, _timeout: float):
        class _FakeInter:
            def create(self, **kwargs: Any):
                captured_gem.append(kwargs)
                return SimpleNamespace(
                    status="completed",
                    output_text=json.dumps(
                        {
                            "augment_pick_advice": "",
                            "buy_now": "先买守护者之锤",
                            "build_path": ["三相之力"],
                            "next_items": [{"item_id": "3078", "name": "三相之力", "priority": "下件必出", "reason": "核心"}],
                            "tactical_summary": "盯防暗黑元首进场",
                            "opening_brief": {
                                "enemy_threat": "敌核心暗黑元首3220分+2黑车队",
                                "augment_goal": "追技能极速流",
                                "opening_buy": "1400G守护者之锤",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    usage=SimpleNamespace(total_input_tokens=500, total_output_tokens=120, total_tokens=620),
                )

        return SimpleNamespace(interactions=_FakeInter(), close=lambda: None)

    gw = ServerCoachGateway(gemini_keys=("k1",), deepseek_keys=(), gemini_client_factory=gem_factory)
    try:
        payload = {
            "dataset_patch": "26.23",
            "trigger_reason": "game_start",
            "my_hero": {"hero_id": "120", "name": "战争之影"},
            "lobby_scout": {
                "puuid": "leaked-puuid-should-strip",
                "top_enemy_champion": "暗黑元首",
                "top_enemy_rating": 3220,
                "top_enemy_tag": "钻一·WR70%(10场)·7连胜·车队A",
                "enemy_premades": ["2黑(暗黑元首+不祥之刃)"],
                "ally_mvp_champion": "战争之影",
                "ally_mvp_rating": 3940,
            },
        }
        res = gw.advise(payload, "game_start", tier="b")
        assert res.advice_json["opening_brief"]["enemy_threat"]
        sent_input = captured_gem[0]["kwargs"]["input"] if "kwargs" in captured_gem[0] else captured_gem[0]["input"]
        assert "暗黑元首" in sent_input
        assert "3220" in sent_input
        assert "2黑(暗黑元首+不祥之刃)" in sent_input
        assert "开局雷达重点" in sent_input
        assert "leaked-puuid-should-strip" not in sent_input
    finally:
        gw.close()


def test_server_coach_gateway_callout_handles_malformed_and_bool_ratings() -> None:
    server_root = Path(__file__).resolve().parent.parent / "server"
    if str(server_root) not in sys.path:
        sys.path.insert(0, str(server_root))

    from hex_server.coach_gateway import _build_lobby_scout_callout

    for bad_val in ("unknown", " ", {}, [], True, False, float("nan"), float("inf"), float("-inf")):
        callout = _build_lobby_scout_callout(
            {
                "lobby_scout": {
                    "top_enemy_champion": "暗黑元首",
                    "top_enemy_rating": bad_val,
                    "ally_mvp_champion": "战争之影",
                    "ally_mvp_rating": bad_val,
                }
            }
        )
        assert "估分1" not in callout
        assert "战争之影(1)" not in callout
        assert "暗黑元首" in callout

    # Valid numeric string float degrades cleanly without throwing
    float_str_callout = _build_lobby_scout_callout(
        {
            "lobby_scout": {
                "top_enemy_champion": "暗黑元首",
                "top_enemy_rating": "3200.5",
                "ally_mvp_champion": "战争之影",
                "ally_mvp_rating": "3940.0",
            }
        }
    )
    assert "估分3200" in float_str_callout
    assert "战争之影(3940)" in float_str_callout


def test_lobby_scout_resilient_to_dirty_game_creation_and_kda_values(tmp_path: Path) -> None:
    store = _make_store(tmp_path)

    class _DirtyHistoryLcu(_FakeReadOnlyLcu):
        def get_match_history(self, puuid: str, count: int = 15) -> list[dict[str, Any]]:
            if puuid == "e1":
                # Mix valid distinct gameCreation timestamps with malformed string/dict/bool/NaN values
                # AND malformed KDA values (string, dict, bool, inf)
                return [
                    {
                        "gameId": 7101,
                        "queueId": 2400,
                        "gameCreation": 1700000100000,
                        "participants": [{"stats": {"win": True, "kills": "not_a_num", "deaths": {}, "assists": True}}],
                    },
                    {
                        "gameId": 7102,
                        "queueId": 2400,
                        "gameCreation": "invalid_timestamp",
                        "participants": [{"stats": {"win": True, "kills": float("nan"), "deaths": " ", "assists": []}}],
                    },
                    {
                        "gameId": 7103,
                        "queueId": 2400,
                        "gameCreation": 1700000200000,
                        "participants": [{"stats": {"win": True, "kills": 10, "deaths": 2, "assists": 12}}],
                    },
                    {
                        "gameId": True,
                        "queueId": 2400,
                        "gameCreation": {"bad": 1},
                        "participants": [{"stats": {"win": False, "kills": float("inf"), "deaths": False, "assists": 4}}],
                    },
                ]
            return super().get_match_history(puuid, count=count)

    snap = scout_lobby_readonly(_DirtyHistoryLcu(), store=store, my_puuid="p1", pace_s=0.0)  # type: ignore[arg-type]
    assert snap is not None
    assert len(snap.my_team) == 2
    assert len(snap.enemy_team) == 3

    e1_card = next(c for c in snap.enemy_team if c.puuid == "e1")
    assert e1_card.aram_games == 4
    assert e1_card.aram_wr == 0.75
    # Newest game (1700000200000) is a win, 2nd newest (1700000100000) is a win,
    # malformed timestamps sort to 0 at tail; bool/dirty kills/deaths/assists count as 0 (not 1!)
    assert e1_card.kda == round((10 + 12 + 4) / 2, 2)
    assert 1200 <= e1_card.hex_rating <= 4200
