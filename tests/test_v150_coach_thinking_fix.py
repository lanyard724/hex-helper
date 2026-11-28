"""WS-A regression tests: Gemini minimal thinking allowlist, 400 minimal->low fallback, and trigger-scoped timeouts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from hex_helper.bridge.protocol import CoachRequestPayload
from hex_helper.coach import gemini_coach as coach_mod
from hex_helper.coach.gemini_coach import (
    GeminiCoachClient,
    _is_thinking_level_400_error,
    resolve_coach_timeout_s,
)
from hex_helper.config import (
    GEMINI_MODELS,
    AppConfig,
    model_supports_minimal_thinking,
    resolve_thinking_level,
)

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from hex_server import coach_gateway as gateway_mod  # noqa: E402
from hex_server.coach_gateway import (  # noqa: E402
    GatewayUpstreamError,
    ServerCoachGateway,
    model_supports_minimal_thinking as server_model_supports_minimal_thinking,
    resolve_server_thinking_level,
)


class _FakeBadRequestError(Exception):
    status_code = 400


class _FakeRateLimitError(Exception):
    status_code = 429


def _valid_advice_json() -> str:
    return json.dumps(
        {
            "augment_pick_advice": "拿珠光护手，暴击增伤最高",
            "buy_now": "先补无用大棒",
            "build_path": ["灭世者的死亡之帽", "虚空之杖"],
            "next_items": [
                {
                    "item_id": "3089",
                    "name": "灭世者的死亡之帽",
                    "priority": "下件必出",
                    "reason": "配合珠光护手最大化爆发",
                }
            ],
            "tactical_summary": "站后排等前排开团再交控制",
        },
        ensure_ascii=False,
    )


def _ok_interaction() -> SimpleNamespace:
    return SimpleNamespace(
        status="completed",
        output_text=_valid_advice_json(),
        usage=SimpleNamespace(
            total_input_tokens=600,
            total_cached_tokens=0,
            total_output_tokens=120,
            total_thought_tokens=80,
            total_tokens=800,
        ),
    )


@pytest.fixture(autouse=True)
def _clean_minimal_caches():
    """Ensure module-level _UNSUPPORTED_MINIMAL_MODELS caches never bleed between tests."""
    with coach_mod._UNSUPPORTED_MINIMAL_MODELS_LOCK:
        coach_mod._UNSUPPORTED_MINIMAL_MODELS.clear()
    with gateway_mod._UNSUPPORTED_MINIMAL_MODELS_LOCK:
        gateway_mod._UNSUPPORTED_MINIMAL_MODELS.clear()
    try:
        yield
    finally:
        with coach_mod._UNSUPPORTED_MINIMAL_MODELS_LOCK:
            coach_mod._UNSUPPORTED_MINIMAL_MODELS.clear()
        with gateway_mod._UNSUPPORTED_MINIMAL_MODELS_LOCK:
            gateway_mod._UNSUPPORTED_MINIMAL_MODELS.clear()


# ===========================================================================
# 1. model_supports_minimal_thinking & GEMINI_MODELS & resolve_thinking_level
# ===========================================================================


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gemini-3.6-flash", True),
        ("models/gemini-3.6-flash", True),
        ("  GEMINI-3.6-FLASH  ", True),
        ("gemini-3.6-flash-001", True),
        ("gemini-3.5-flash-lite", True),
        ("models/GEMINI-3.5-FLASH-LITE", True),
        ("gemini-3.1-flash-lite", True),
        ("gemini-3-flash-preview", True),
        ("gemini-robotics-er-2-preview", True),
        ("gemini-3.8-flash", False),
        ("models/gemini-3.8-flash", False),
        ("gemini-3.8-flash-preview", False),
        ("gemini-4-hydrogen", False),
        ("gemini-4-argon", False),
        ("gemini-4-carbon", False),
        ("gemini-4-neon", False),
        ("models/gemini-4-hydrogen", False),
        ("gemini-3.6-flash2", False),
        ("", False),
        ("   ", False),
    ],
)
def test_model_supports_minimal_thinking_allowlist_client_and_server(model: str, expected: bool) -> None:
    assert model_supports_minimal_thinking(model) is expected
    assert server_model_supports_minimal_thinking(model) is expected


def test_gemini_models_includes_36_flash_and_keeps_existing_defaults() -> None:
    for required in (
        "gemini-4-hydrogen",
        "gemini-4-argon",
        "gemini-4-carbon",
        "gemini-4-neon",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
    ):
        assert required in GEMINI_MODELS
    assert AppConfig().gemini_picker_fast_model == "gemini-3.8-flash"


def test_resolve_thinking_level_matrix_across_models_and_triggers() -> None:
    # 1. gemini-3.8-flash MUST NEVER resolve to 'minimal' (clamped to 'low')
    for trigger in (
        "augment_picking",
        "pre_death_warmup",
        "connection_test",
        "death_enemy_build_changed",
    ):
        assert resolve_thinking_level("medium", trigger, adaptive=True, model="gemini-3.8-flash") == "low"
        assert resolve_thinking_level("minimal", trigger, adaptive=True, model="gemini-3.8-flash") == "low"
    assert resolve_thinking_level("minimal", "manual_f6", adaptive=False, model="gemini-3.8-flash") == "low"
    assert resolve_thinking_level("medium", "augment_selected", adaptive=True, model="gemini-3.8-flash", is_transformative=False) == "low"
    assert resolve_thinking_level("medium", "augment_selected", adaptive=True, model="gemini-3.8-flash", is_transformative=True) == "medium"
    assert resolve_thinking_level("medium", "game_start", adaptive=True, model="gemini-3.8-flash") == "medium"
    assert resolve_thinking_level("xhigh", "manual_f6", adaptive=True, model="gemini-3.8-flash") == "medium"

    # 2. gemini-4-hydrogen MUST NEVER resolve to 'minimal'
    assert resolve_thinking_level("medium", "augment_picking", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("minimal", "augment_picking", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("minimal", "manual_f6", adaptive=False, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("medium", "pre_death_warmup", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("medium", "connection_test", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("medium", "death_enemy_build_changed", adaptive=True, model="gemini-4-hydrogen", fast_death=True) == "low"
    assert resolve_thinking_level("medium", "death_enemy_build_changed", adaptive=True, model="gemini-4-hydrogen", fast_death=False) == "medium"
    assert resolve_thinking_level("medium", "augment_selected", adaptive=True, model="gemini-4-hydrogen", is_transformative=False) == "low"
    assert resolve_thinking_level("medium", "augment_selected", adaptive=True, model="gemini-4-hydrogen", is_transformative=True) == "medium"
    assert resolve_thinking_level("medium", "game_start", adaptive=True, model="gemini-4-hydrogen") == "medium"
    assert resolve_thinking_level("xhigh", "manual_f6", adaptive=True, model="gemini-4-hydrogen") == "medium"

    # 3. Allowlisted models (gemini-3.6-flash, gemini-3.5-flash-lite) allow 'minimal' on augment_picking
    for minimal_model in ("gemini-3.6-flash", "gemini-3.5-flash-lite"):
        assert resolve_thinking_level("medium", "augment_picking", adaptive=True, model=minimal_model) == "minimal"
        assert resolve_thinking_level("minimal", "augment_picking", adaptive=True, model=minimal_model) == "minimal"
        assert resolve_thinking_level("minimal", "manual_f6", adaptive=False, model=minimal_model) == "minimal"
        assert resolve_thinking_level("medium", "pre_death_warmup", adaptive=True, model=minimal_model) == "low"
        assert resolve_thinking_level("medium", "connection_test", adaptive=True, model=minimal_model) == "low"
        assert resolve_thinking_level("medium", "death_enemy_build_changed", adaptive=True, model=minimal_model) == "low"
        assert resolve_thinking_level("medium", "augment_selected", adaptive=True, model=minimal_model, is_transformative=False) == "low"
        assert resolve_thinking_level("medium", "augment_selected", adaptive=True, model=minimal_model, is_transformative=True) == "medium"
        assert resolve_thinking_level("medium", "game_start", adaptive=True, model=minimal_model) == "medium"
        assert resolve_thinking_level("medium", "manual_f6", adaptive=True, model=minimal_model) == "medium"


# ===========================================================================
# 2. Server resolve_server_thinking_level
# ===========================================================================


def test_resolve_server_thinking_level_matrix() -> None:
    # Non-minimal models clamp augment_picking -> 'low'
    assert resolve_server_thinking_level("augment_picking", "gemini-3.8-flash") == "low"
    assert resolve_server_thinking_level("augment_picking", "models/gemini-3.8-flash") == "low"
    assert resolve_server_thinking_level("augment_picking", "gemini-4-hydrogen") == "low"
    assert resolve_server_thinking_level("augment_picking", "gemini-4-argon") == "low"

    # Allowlisted models get 'minimal' on augment_picking
    assert resolve_server_thinking_level("augment_picking", "gemini-3.6-flash") == "minimal"
    assert resolve_server_thinking_level("augment_picking", "gemini-3.5-flash-lite") == "minimal"
    assert resolve_server_thinking_level("augment_picking", "gemini-3.1-flash-lite") == "minimal"
    assert resolve_server_thinking_level("augment_picking", "gemini-3-flash-preview") == "minimal"

    # Other triggers remain aligned across models
    for model in ("gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.6-flash"):
        assert resolve_server_thinking_level("game_start", model) == "low"
        assert resolve_server_thinking_level("pre_death_warmup", model) == "low"
        assert resolve_server_thinking_level("connection_test", model) == "low"
        assert resolve_server_thinking_level("augment_selected", model, is_transformative=False) == "low"
        assert resolve_server_thinking_level("augment_selected", model, is_transformative=True) == "medium"
        assert resolve_server_thinking_level("manual_f6", model) == "medium"

    assert resolve_server_thinking_level("death_enemy_build_changed", "gemini-4-hydrogen") == "medium"
    assert resolve_server_thinking_level("death_enemy_build_changed", "gemini-4-hydrogen", fast_death=True) == "low"
    assert resolve_server_thinking_level("death_enemy_build_changed", "gemini-3.8-flash") == "low"


# ===========================================================================
# 3. 400 THINKING_LEVEL_MINIMAL detection + automatic retry & caching
# ===========================================================================


def test_is_thinking_level_400_error_detection_client_and_server() -> None:
    live_err = _FakeBadRequestError(
        "Error code: 400 - {'error': {'message': 'Thinking level THINKING_LEVEL_MINIMAL is not supported for this model.'}}"
    )
    generic_400 = RuntimeError("HTTP 400 BadRequest: thinking level is not supported on this model")
    class BadRequestError(Exception):
        pass
    named_bad_req = BadRequestError("Thinking level minimal is not supported")

    unrelated_400 = _FakeBadRequestError("Error code: 400 - Invalid JSON schema")
    rate_limit_429 = _FakeRateLimitError("429 THINKING_LEVEL_MINIMAL rate limited")
    timeout_err = TimeoutError("timed out")

    for fn in (_is_thinking_level_400_error, gateway_mod._is_thinking_level_400_error):
        assert fn(live_err) is True
        assert fn(generic_400) is True
        assert fn(named_bad_req) is True
        assert fn(unrelated_400) is False
        assert fn(rate_limit_429) is False
        assert fn(timeout_err) is False


def test_gemini_coach_400_minimal_fallback_retry_and_cache() -> None:
    recorded: list[dict[str, Any]] = []

    class _FakeInteractions:
        def create(self, **kwargs: Any) -> Any:
            recorded.append(kwargs)
            tl = (kwargs.get("generation_config") or {}).get("thinking_level")
            if tl == "minimal":
                raise _FakeBadRequestError(
                    "Error code: 400 - {'error': {'message': 'Thinking level THINKING_LEVEL_MINIMAL is not supported for this model.'}}"
                )
            return _ok_interaction()

    fake_sdk = SimpleNamespace(interactions=_FakeInteractions(), close=lambda: None)
    client = GeminiCoachClient(client_factory=lambda _k, _p, _t: fake_sdk)
    try:
        req = CoachRequestPayload(
            request_id="req-pick-36",
            trigger_reason="augment_picking",
            timestamp="2026-11-27T01:00:00+08:00",
            dataset_patch="26.23",
            my_hero={"hero_id": "99", "name": "拉克丝", "gold": 1400},
            offered_augments=[{"id": "1001", "name": "珠光护手"}],
        )

        # Call 1 on gemini-3.6-flash: tries minimal -> 400 -> caches model -> retries ONCE with low
        advice1, stats1 = client.advise(
            req,
            store=None,
            api_key="AIzaTestKey",
            model="gemini-4-hydrogen",
            fast_picker_model="gemini-3.6-flash",
            thinking_level="medium",
            adaptive_thinking=True,
        )
        assert len(recorded) == 2
        assert recorded[0]["generation_config"] == {"thinking_level": "minimal"}
        assert recorded[1]["generation_config"] == {"thinking_level": "low"}
        assert stats1.model == "gemini-3.6-flash"
        assert stats1.thinking_level == "low"
        assert "low" in advice1.source
        assert "gemini-3.6-flash" in coach_mod._UNSUPPORTED_MINIMAL_MODELS

        # Call 2 on same model: cache hit skips minimal completely (single request directly at low)
        advice2, stats2 = client.advise(
            req,
            store=None,
            api_key="AIzaTestKey",
            model="gemini-4-hydrogen",
            fast_picker_model="gemini-3.6-flash",
            thinking_level="medium",
            adaptive_thinking=True,
        )
        assert len(recorded) == 3
        assert recorded[2]["generation_config"] == {"thinking_level": "low"}
        assert stats2.thinking_level == "low"
        assert "low" in advice2.source

        # Call 3 on gemini-3.8-flash: clamped to low upfront by allowlist (zero 400s!)
        advice3, stats3 = client.advise(
            req,
            store=None,
            api_key="AIzaTestKey",
            model="gemini-4-hydrogen",
            fast_picker_model="gemini-3.8-flash",
            thinking_level="medium",
            adaptive_thinking=True,
        )
        assert len(recorded) == 4
        assert recorded[3]["model"] == "gemini-3.8-flash"
        assert recorded[3]["generation_config"] == {"thinking_level": "low"}
        assert stats3.thinking_level == "low"
        assert "low" in advice3.source
    finally:
        client.close()


def test_gemini_coach_non_thinking_400_does_not_retry_or_cache() -> None:
    attempts = 0

    class _FakeInteractions:
        def create(self, **kwargs: Any) -> Any:
            nonlocal attempts
            attempts += 1
            raise _FakeBadRequestError("Error code: 400 - Invalid response_format schema")

    client = GeminiCoachClient(
        client_factory=lambda _k, _p, _t: SimpleNamespace(interactions=_FakeInteractions(), close=lambda: None)
    )
    try:
        req = CoachRequestPayload(
            request_id="req-bad-schema",
            trigger_reason="augment_picking",
            timestamp="2026-11-27T01:00:00+08:00",
            dataset_patch="26.23",
            my_hero={"hero_id": "99", "name": "拉克丝"},
        )
        with pytest.raises(_FakeBadRequestError):
            client.advise(
                req,
                store=None,
                api_key="AIzaTestKey",
                model="gemini-3.6-flash",
                thinking_level="medium",
                adaptive_thinking=True,
            )
        assert attempts == 1
        assert "gemini-3.6-flash" not in coach_mod._UNSUPPORTED_MINIMAL_MODELS
    finally:
        client.close()


def test_server_coach_gateway_400_minimal_fallback_retry_and_cache() -> None:
    recorded: list[dict[str, Any]] = []

    def fake_factory(api_key: str, proxy_url: str, timeout_s: float) -> Any:
        class _FakeInteractions:
            def create(self, **kwargs: Any) -> Any:
                recorded.append({"api_key": api_key, "kwargs": kwargs})
                tl = (kwargs.get("generation_config") or {}).get("thinking_level")
                if tl == "minimal":
                    raise _FakeBadRequestError(
                        "Error code: 400 - {'error': {'message': 'Thinking level THINKING_LEVEL_MINIMAL is not supported for this model.'}}"
                    )
                return _ok_interaction()

        return SimpleNamespace(interactions=_FakeInteractions(), close=lambda: None)

    gw = ServerCoachGateway(
        gemini_keys=["gem-key-1"],
        deepseek_keys=[],
        gemini_client_factory=fake_factory,
    )
    try:
        payload = {"my_hero": {"hero_name": "拉克丝"}, "offered_augments": [{"id": "1001", "name": "珠光护手"}]}

        # Call 1: gemini-3.6-flash tries minimal -> 400 -> caches -> retries once with low
        r1 = gw.advise(payload, "augment_picking", tier="b", tier_b_model="gemini-3.6-flash")
        assert r1.thinking_level == "low"
        assert r1.model == "gemini-3.6-flash"
        assert len(recorded) == 2
        assert recorded[0]["kwargs"]["generation_config"] == {"thinking_level": "minimal"}
        assert recorded[1]["kwargs"]["generation_config"] == {"thinking_level": "low"}
        assert "gemini-3.6-flash" in gateway_mod._UNSUPPORTED_MINIMAL_MODELS

        # Call 2: cache hit skips minimal immediately
        r2 = gw.advise(payload, "augment_picking", tier="b", tier_b_model="gemini-3.6-flash")
        assert r2.thinking_level == "low"
        assert len(recorded) == 3
        assert recorded[2]["kwargs"]["generation_config"] == {"thinking_level": "low"}

        # Call 3: gemini-3.8-flash starts directly at low with zero 400s
        r3 = gw.advise(payload, "augment_picking", tier="b", tier_b_model="gemini-3.8-flash")
        assert r3.thinking_level == "low"
        assert len(recorded) == 4
        assert recorded[3]["kwargs"]["generation_config"] == {"thinking_level": "low"}
    finally:
        gw.close()


def test_server_coach_gateway_unrelated_400_raises_without_retry() -> None:
    attempts = 0

    def fake_factory(_k: str, _p: str, _t: float) -> Any:
        class _FakeInteractions:
            def create(self, **kwargs: Any) -> Any:
                nonlocal attempts
                attempts += 1
                raise _FakeBadRequestError("Error code: 400 - Malformed input payload")

        return SimpleNamespace(interactions=_FakeInteractions(), close=lambda: None)

    gw = ServerCoachGateway(gemini_keys=["gem-key-1"], deepseek_keys=[], gemini_client_factory=fake_factory)
    try:
        with pytest.raises(GatewayUpstreamError):
            gw.advise({"my_hero": {"hero_name": "拉克丝"}}, "augment_picking", tier="b", tier_b_model="gemini-3.6-flash")
        assert attempts == 1
        assert "gemini-3.6-flash" not in gateway_mod._UNSUPPORTED_MINIMAL_MODELS
    finally:
        gw.close()


# ===========================================================================
# 4. Trigger-scoped timeouts (resolve_coach_timeout_s + SDK wiring)
# ===========================================================================


@pytest.mark.parametrize(
    "trigger_reason,expected_timeout_s",
    [
        ("augment_picking", 12.0),
        ("AUGMENT_PICKING", 12.0),
        ("  augment_picking  ", 12.0),
        ("pre_death_warmup", 18.0),
        ("death_enemy_build_changed", 20.0),
        ("game_start", 30.0),
        ("augment_selected", 30.0),
        ("manual_f6", 30.0),
        ("unknown_trigger", 30.0),
        ("", 30.0),
    ],
)
def test_resolve_coach_timeout_s(trigger_reason: str, expected_timeout_s: float) -> None:
    assert resolve_coach_timeout_s(trigger_reason) == expected_timeout_s


def test_gemini_coach_applies_trigger_scoped_timeout_without_evicting_pool() -> None:
    factory_calls: list[tuple[str, str, float]] = []
    create_timeouts: list[tuple[str, float]] = []

    class _FakeInteractions:
        def create(self, **kwargs: Any) -> Any:
            create_timeouts.append((kwargs["model"], kwargs["timeout"]))
            return _ok_interaction()

    fake_sdk = SimpleNamespace(interactions=_FakeInteractions(), close=lambda: None)

    def fake_factory(api_key: str, proxy_url: str, timeout_s: float) -> Any:
        factory_calls.append((api_key, proxy_url, timeout_s))
        return fake_sdk

    client = GeminiCoachClient(client_factory=fake_factory)
    try:
        cases = [
            ("augment_picking", 12.0),
            ("pre_death_warmup", 18.0),
            ("death_enemy_build_changed", 20.0),
            ("game_start", 30.0),
            ("augment_selected", 30.0),
            ("manual_f6", 30.0),
        ]
        for trigger, expected_t in cases:
            req = CoachRequestPayload(
                request_id=f"req-{trigger}",
                trigger_reason=trigger,
                timestamp="2026-11-27T01:00:00+08:00",
                dataset_patch="26.23",
                my_hero={"hero_id": "99", "name": "拉克丝"},
            )
            client.advise(req, store=None, api_key="AIzaTestKey", model="gemini-4-hydrogen")
            assert create_timeouts[-1][1] == expected_t

        # Warm pooled client stayed intact across all 6 distinct trigger timeouts (factory called once at 90s)
        assert factory_calls == [("AIzaTestKey", "", 90.0)]

        # Explicit caller override timeout_s is honored directly
        req_override = CoachRequestPayload(
            request_id="req-override",
            trigger_reason="augment_picking",
            timestamp="2026-11-27T01:00:00+08:00",
            dataset_patch="26.23",
            my_hero={"hero_id": "99", "name": "拉克丝"},
        )
        client.advise(req_override, store=None, api_key="AIzaTestKey", model="gemini-4-hydrogen", timeout_s=45.0)
        assert create_timeouts[-1][1] == 45.0
    finally:
        client.close()


def test_real_genai_sdk_honors_per_request_trigger_timeout_and_real_400_fallback() -> None:
    from google import genai
    from google.genai import types

    seen_requests: list[tuple[dict[str, Any], dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_requests.append((dict(request.extensions.get("timeout") or {}), body))
        tl = (body.get("generation_config") or {}).get("thinking_level")
        if tl == "minimal":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Thinking level THINKING_LEVEL_MINIMAL is not supported for this model."
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "int-test-1",
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": _valid_advice_json()}],
                    }
                ],
            },
        )

    def mock_sdk_factory(api_key: str, _proxy_url: str, timeout_s: float) -> Any:
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=max(5000, int(timeout_s * 1000)),
                retry_options=types.HttpRetryOptions(attempts=1),
                client_args={"transport": httpx.MockTransport(handler)},
            ),
        )

    client = GeminiCoachClient(client_factory=mock_sdk_factory)
    try:
        for trigger, expected_s in (
            ("augment_picking", 12.0),
            ("pre_death_warmup", 18.0),
            ("death_enemy_build_changed", 20.0),
            ("manual_f6", 30.0),
        ):
            req = CoachRequestPayload(
                request_id=f"sdk-{trigger}",
                trigger_reason=trigger,
                timestamp="2026-11-27T01:00:00+08:00",
                dataset_patch="26.23",
                my_hero={"hero_id": "99", "name": "拉克丝"},
            )
            client.advise(req, store=None, api_key="AIzaDummyKey", model="gemini-4-hydrogen")
            read_timeout = float(seen_requests[-1][0]["read"])
            assert expected_s - 0.5 <= read_timeout <= expected_s

        # Verify real SDK BadRequestError (HTTP 400) triggers automatic minimal->low retry & cache!
        seen_before = len(seen_requests)
        req_pick = CoachRequestPayload(
            request_id="sdk-real-400-fallback",
            trigger_reason="augment_picking",
            timestamp="2026-11-27T01:00:00+08:00",
            dataset_patch="26.23",
            my_hero={"hero_id": "99", "name": "拉克丝"},
            offered_augments=[{"id": "1001", "name": "珠光护手"}],
        )
        advice, stats = client.advise(
            req_pick,
            store=None,
            api_key="AIzaDummyKey",
            model="gemini-4-hydrogen",
            fast_picker_model="gemini-3.6-flash",
            thinking_level="medium",
            adaptive_thinking=True,
        )
        assert len(seen_requests) == seen_before + 2
        assert seen_requests[-2][1]["generation_config"]["thinking_level"] == "minimal"
        assert seen_requests[-1][1]["generation_config"]["thinking_level"] == "low"
        assert stats.thinking_level == "low"
        assert "low" in advice.source
        assert "gemini-3.6-flash" in coach_mod._UNSUPPORTED_MINIMAL_MODELS
    finally:
        client.close()
