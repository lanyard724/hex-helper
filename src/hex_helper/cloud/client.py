"""Commercial cloud API client and single-worker latest-wins AI Coach executor."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
import platform
import threading
import time
from typing import Any, Callable
import uuid

import httpx

from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload
from hex_helper.cloud.session_store import DpapiSessionStore
from hex_helper.coach.gemini_coach import build_high_snr_prompt_payload, ground_coach_advice
from hex_helper.data.store import DataStore
from hex_helper.edition import APP_VERSION

logger = logging.getLogger(__name__)


class CloudCoachError(RuntimeError):
    """Structured error from the commercial cloud backend (401/402/403/426/429/503/5xx)."""

    def __init__(
        self,
        code: str,
        status_code: int,
        message: str,
        wallet: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "CLOUD_ERROR")
        self.status_code = int(status_code)
        self.message = str(message or code)
        self.wallet = dict(wallet) if isinstance(wallet, dict) else None
        self.extra = dict(extra) if isinstance(extra, dict) else {}


@dataclass(frozen=True)
class CloudAdviseStats:
    tier: str
    model: str
    thinking_level: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    total_tokens: int = 0
    cost_credits: float = 0.0
    balance_credits: float = 0.0
    wallet: dict[str, Any] = field(default_factory=dict)
    routed_tier: str = ""


@dataclass(frozen=True)
class CloudAdviseResult:
    advice: CoachAdvicePayload
    stats: CloudAdviseStats


def _default_device_id() -> str:
    node = platform.node() or os.environ.get("COMPUTERNAME") or "pc"
    return f"win-{node[:32]}"


def _tier_source_label(tier: str, *, requested_tier: str = "") -> str:
    eff = (tier or "a").strip().lower()
    req = (requested_tier or "").strip().lower()
    if req == "auto":
        return "云端AI · 智能路由(B档)" if eff == "b" else "云端AI · 智能路由(A档)"
    return "云端AI · B档旗舰" if eff == "b" else "云端AI · A档极速"


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    """Parse HTTP JSON body cleanly, raising CloudCoachError on HTML/non-JSON responses."""
    try:
        data = resp.json()
    except Exception as exc:
        raise CloudCoachError(
            "INVALID_RESPONSE",
            resp.status_code,
            f"云端返回异常响应格式 (HTTP {resp.status_code})",
        ) from exc
    if not isinstance(data, dict):
        raise CloudCoachError(
            "INVALID_RESPONSE",
            resp.status_code,
            f"云端返回数据结构异常 (HTTP {resp.status_code})",
        )
    return data


def _parse_error_payload(resp: httpx.Response) -> tuple[str, str, dict[str, Any] | None, dict[str, Any]]:
    code = ""
    message = ""
    wallet: dict[str, Any] | None = None
    extra: dict[str, Any] = {}
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        code = str(data.get("code") or "").strip()
        message = str(data.get("message") or data.get("detail") or "").strip()
        if any(k in data for k in ("balance_credits", "balance_micro_credits", "ai_cutoff")):
            bal = float(data.get("balance_credits") or 0.0)
            wallet = {
                "balance_credits": bal,
                "balance_cny": round(bal / 1000.0, 4),
                "balance_micro_credits": int(data.get("balance_micro_credits") or 0),
                "min_call_micro_credits": int(data.get("min_call_micro_credits") or 1000),
                "ai_cutoff": bool(data.get("ai_cutoff", True)),
                "ai_enabled": not bool(data.get("ai_cutoff", True)),
            }
        if isinstance(data.get("service_lock"), dict):
            extra["service_lock"] = dict(data["service_lock"])
        if isinstance(data.get("update"), dict):
            extra["update"] = dict(data["update"])
    if not message:
        message = f"云端服务请求失败 (HTTP {resp.status_code})"
    if not code:
        if resp.status_code == 401:
            code = "NOT_LOGGED_IN"
        elif resp.status_code == 402:
            code = "INSUFFICIENT_CREDITS"
        elif resp.status_code == 403:
            code = "ACCOUNT_DISABLED"
        elif resp.status_code == 426:
            code = "CLIENT_UPDATE_REQUIRED"
        elif resp.status_code == 429:
            code = "COACH_REQUEST_IN_FLIGHT"
        elif resp.status_code == 503:
            code = "SERVICE_LOCKED"
        else:
            code = f"HTTP_{resp.status_code}"
    return code, message, wallet, extra


class CloudGatewayClient:
    """Thread-safe zero-proxy client for HexHelper commercial cloud APIs."""

    FAST_TIMEOUT_S: float = 10.0
    TELEMETRY_TIMEOUT_S: float = 5.0

    def __init__(
        self,
        base_url: str,
        session_store: DpapiSessionStore | None = None,
        *,
        http_client_factory: Callable[[str, float], httpx.Client] | None = None,
        timeout_s: float = 65.0,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.session_store = session_store or DpapiSessionStore()
        self._http_client_factory = http_client_factory
        self._timeout_s = max(5.0, float(timeout_s))

        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._http_client: httpx.Client | None = None
        self._session: dict[str, Any] = {}
        self._last_wallet: dict[str, Any] | None = None
        self._session_gen: int = 0

    @property
    def is_logged_in(self) -> bool:
        with self._lock:
            return bool(self._session.get("access_token") or self._session.get("refresh_token"))

    @property
    def current_username(self) -> str:
        with self._lock:
            return str(self._session.get("username") or "")

    @property
    def last_wallet(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_wallet) if self._last_wallet is not None else None

    def _get_http(self) -> httpx.Client:
        with self._lock:
            if self._http_client is not None:
                return self._http_client
            if self._http_client_factory is not None:
                client = self._http_client_factory(self.base_url, self._timeout_s)
            else:
                # Zero proxy & trust_env=False so CN players never route through broken local proxies
                client = httpx.Client(
                    base_url=self.base_url,
                    timeout=self._timeout_s,
                    proxy=None,
                    trust_env=False,
                    follow_redirects=True,
                    headers={
                        "User-Agent": f"HexHelperClient/{APP_VERSION}",
                        "X-Client-Version": APP_VERSION,
                    },
                )
            self._http_client = client
            return client

    def close(self) -> None:
        with self._lock:
            client = self._http_client
            self._http_client = None
        if client is not None and self._http_client_factory is None:
            try:
                client.close()
            except Exception:
                pass

    def _persist_session(
        self,
        payload: dict[str, Any],
        *,
        device_id: str = "",
        expected_gen: int | None = None,
        bump_gen: bool = False,
    ) -> dict[str, Any]:
        user_obj = payload.get("user") if isinstance(payload.get("user"), dict) else None
        with self._lock:
            if expected_gen is not None and self._session_gen != expected_gen:
                raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录")
            if bump_gen:
                self._session_gen += 1
            username = str((user_obj or {}).get("username") or self._session.get("username") or "")
            dev_id = device_id or str(self._session.get("device_id") or _default_device_id())
            sess = {
                "access_token": str(payload.get("access_token") or ""),
                "refresh_token": str(payload.get("refresh_token") or ""),
                "username": username,
                "device_id": dev_id,
            }
            self._session = sess
            if user_obj is not None:
                self._last_wallet = dict(user_obj)
            self.session_store.save(sess)
            return dict(sess)

    def _clear_local_session(self, *, expected_gen: int | None = None) -> None:
        with self._lock:
            if expected_gen is not None and self._session_gen != expected_gen:
                return
            self._session_gen += 1
            self._session = {}
            self._last_wallet = None
            self.session_store.clear()

    def restore_session(self) -> dict[str, Any] | None:
        loaded = self.session_store.load()
        if not loaded or not (loaded.get("access_token") or loaded.get("refresh_token")):
            return None
        with self._lock:
            self._session_gen += 1
            self._session = {
                "access_token": str(loaded.get("access_token") or ""),
                "refresh_token": str(loaded.get("refresh_token") or ""),
                "username": str(loaded.get("username") or ""),
                "device_id": str(loaded.get("device_id") or _default_device_id()),
            }
            return dict(self._session)

    @staticmethod
    def _base_headers() -> dict[str, str]:
        return {"X-Client-Version": APP_VERSION}

    def _auth_headers(self) -> dict[str, str]:
        with self._lock:
            token = str(self._session.get("access_token") or "").strip()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def _reset_http_pool(self) -> None:
        if self._http_client_factory is not None:
            return
        with self._lock:
            old = self._http_client
            self._http_client = None
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = self._base_headers()
        extra_headers = kwargs.pop("headers", None)
        if extra_headers:
            headers.update(extra_headers)
        retry_get = bool(kwargs.pop("_retry_get", True))
        if "timeout" not in kwargs and self._http_client_factory is None:
            kwargs["timeout"] = self._timeout_s if path == "/v1/coach/advise" else min(self._timeout_s, self.FAST_TIMEOUT_S)

        is_idempotent_get = method.strip().upper() == "GET"
        attempts = 2 if (is_idempotent_get and retry_get) else 1
        last_exc: httpx.HTTPError | None = None
        for attempt in range(attempts):
            client = self._get_http()
            try:
                return client.request(method, path, headers=headers, **kwargs)
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    logger.debug("Transient cloud GET error on %s (%s); reconnecting once", path, type(exc).__name__)
                    self._reset_http_pool()
                    time.sleep(0.35)
                    continue
                raise CloudCoachError("NETWORK_ERROR", 0, f"网络连接失败：{exc}") from exc
            except httpx.HTTPError as exc:
                raise CloudCoachError("NETWORK_ERROR", 0, f"网络连接失败：{exc}") from exc
        raise CloudCoachError("NETWORK_ERROR", 0, f"网络连接失败：{last_exc}")

    def _request_with_auth(self, method: str, path: str, **kwargs: Any) -> tuple[httpx.Response, int]:
        with self._lock:
            gen_before = self._session_gen
            access_before = str(self._session.get("access_token") or "")
            refresh_tok = str(self._session.get("refresh_token") or "")
        if not access_before and not refresh_tok:
            raise CloudCoachError("NOT_LOGGED_IN", 401, "请先登录 HexHelper 账号")

        if not access_before and refresh_tok:
            self.refresh_session()

        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._auth_headers())
        resp = self._request(method, path, headers=headers, **kwargs)
        if resp.status_code != 401:
            return resp, gen_before

        # Auto-refresh once on 401 (guarded by _refresh_lock to avoid rotating token double-spend)
        with self._refresh_lock:
            with self._lock:
                if self._session_gen != gen_before:
                    raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已变更，请重新登录")
                current_access = str(self._session.get("access_token") or "")
                current_refresh = str(self._session.get("refresh_token") or "")
            if not current_access and not current_refresh:
                raise CloudCoachError("NOT_LOGGED_IN", 401, "请先登录 HexHelper 账号")
            if current_access == access_before:
                self._refresh_locked()

        with self._lock:
            if self._session_gen != gen_before:
                raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录")
            retry_auth = self._auth_headers()
        if not retry_auth:
            raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录")
        retry_headers = dict(headers)
        retry_headers.update(retry_auth)
        return self._request(method, path, headers=retry_headers, **kwargs), gen_before

    def register(self, username: str, password: str, device_id: str = "") -> dict[str, Any]:
        dev_id = (device_id or "").strip() or _default_device_id()
        resp = self._request(
            "POST",
            "/v1/auth/register",
            json={"username": username.strip(), "password": password, "device_id": dev_id},
        )
        if resp.status_code not in (200, 201):
            code, msg, wallet, extra = _parse_error_payload(resp)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        data = _safe_json(resp)
        self._persist_session(data, device_id=dev_id, bump_gen=True)
        return data

    def login(self, username: str, password: str, device_id: str = "") -> dict[str, Any]:
        dev_id = (device_id or "").strip() or _default_device_id()
        resp = self._request(
            "POST",
            "/v1/auth/login",
            json={"username": username.strip(), "password": password, "device_id": dev_id},
        )
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        data = _safe_json(resp)
        self._persist_session(data, device_id=dev_id, bump_gen=True)
        return data

    def _refresh_locked(self) -> dict[str, Any]:
        with self._lock:
            gen_before = self._session_gen
            rt = str(self._session.get("refresh_token") or "").strip()
            dev_id = str(self._session.get("device_id") or _default_device_id())
        if not rt:
            self._clear_local_session(expected_gen=gen_before)
            raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录")

        resp = self._request(
            "POST",
            "/v1/auth/refresh",
            json={"refresh_token": rt, "device_id": dev_id},
        )
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            if resp.status_code in (401, 403):
                self._clear_local_session(expected_gen=gen_before)
                mapped = "ACCOUNT_DISABLED" if resp.status_code == 403 else "NOT_LOGGED_IN"
                raise CloudCoachError(mapped, resp.status_code, msg, wallet, extra)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)

        data = _safe_json(resp)
        self._persist_session(data, device_id=dev_id, expected_gen=gen_before)
        return data

    def refresh_session(self) -> dict[str, Any]:
        with self._refresh_lock:
            return self._refresh_locked()

    def logout(self) -> None:
        with self._lock:
            rt = str(self._session.get("refresh_token") or "")
            headers = self._auth_headers()
            self._session_gen += 1
            self._session = {}
            self._last_wallet = None
            self.session_store.clear()
        if rt or headers:
            try:
                self._request(
                    "POST",
                    "/v1/auth/logout",
                    json={"refresh_token": rt} if rt else None,
                    headers=headers,
                )
            except Exception:
                pass

    def get_me(self) -> dict[str, Any]:
        resp, gen_before = self._request_with_auth("GET", "/v1/auth/me")
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            if resp.status_code == 401:
                self._clear_local_session(expected_gen=gen_before)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        data = _safe_json(resp)
        if isinstance(data.get("user"), dict):
            with self._lock:
                if self._session_gen == gen_before:
                    self._last_wallet = dict(data["user"])
        return data

    def advise(
        self,
        req: CoachRequestPayload,
        store: DataStore | None,
        tier: str = "a",
    ) -> tuple[CoachAdvicePayload, CloudAdviseStats]:
        norm_tier = (tier or "a").strip().lower()
        if norm_tier not in ("a", "b", "auto"):
            norm_tier = "a"

        prompt_obj = build_high_snr_prompt_payload(req, store)
        hero_name = str((req.my_hero or {}).get("name") or "").strip()
        payload = {
            "request_id": req.request_id,
            "trigger_reason": req.trigger_reason,
            "tier": norm_tier,
            "prompt_payload": prompt_obj,
            "hero_name": hero_name or None,
        }

        t0 = time.monotonic()
        resp, gen_before = self._request_with_auth("POST", "/v1/coach/advise", json=payload)
        if resp.status_code != 200:
            code, msg, err_wallet, extra = _parse_error_payload(resp)
            if resp.status_code == 402:
                with self._lock:
                    merged = dict(self._last_wallet or {})
                    if err_wallet:
                        merged.update(err_wallet)
                    merged["ai_cutoff"] = True
                    merged["ai_enabled"] = False
                    if self._session_gen == gen_before:
                        self._last_wallet = merged
                raise CloudCoachError("INSUFFICIENT_CREDITS", 402, msg, merged, extra)
            if resp.status_code == 403:
                with self._lock:
                    merged = dict(self._last_wallet or {})
                    merged["ai_cutoff"] = True
                    merged["ai_enabled"] = False
                    merged["status"] = "banned"
                    if self._session_gen == gen_before:
                        self._last_wallet = merged
                raise CloudCoachError("ACCOUNT_DISABLED", 403, msg, merged, extra)
            if resp.status_code == 401:
                self._clear_local_session(expected_gen=gen_before)
                raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录", extra=extra)
            raise CloudCoachError(code, resp.status_code, msg, err_wallet, extra)

        data = _safe_json(resp)
        advice_json = data.get("advice_json") or {}
        raw_routed_tier = str(data.get("routed_tier") or "").strip().lower()
        raw_resp_tier = str(data.get("tier") or "").strip().lower()
        effective_tier = raw_routed_tier or raw_resp_tier or ("a" if norm_tier == "auto" else norm_tier)
        if effective_tier not in ("a", "b"):
            effective_tier = "b" if "gemini" in str(data.get("model") or "").lower() else "a"
        routed_tier = raw_routed_tier or effective_tier
        model_name = str(
            data.get("model")
            or ("gemini-4-hydrogen" if effective_tier == "b" else "deepseek-flash")
        )
        thinking_level = str(data.get("thinking_level") or "")
        latency_ms = int(data.get("latency_ms") or ((time.monotonic() - t0) * 1000))
        usage = data.get("usage") or {}
        wallet = data.get("wallet") or {}

        with self._lock:
            merged_wallet = dict(self._last_wallet or {})
            if isinstance(wallet, dict):
                merged_wallet.update(wallet)
            if self._session_gen == gen_before:
                self._last_wallet = merged_wallet

        # Unified local item-ID grounding, owned-item dedup & 6-slot elixir rules for Tier A, Tier B & Auto
        advice = ground_coach_advice(
            advice_json,
            req,
            store,
            model=model_name,
            thinking_level=thinking_level or "cloud",
        )
        advice.source = _tier_source_label(effective_tier, requested_tier=norm_tier)

        stats = CloudAdviseStats(
            tier=effective_tier,
            model=model_name,
            thinking_level=thinking_level,
            latency_ms=latency_ms,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            thought_tokens=int(usage.get("thought_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cost_credits=float(wallet.get("cost_credits") or 0.0),
            balance_credits=float(wallet.get("balance_credits") or 0.0),
            wallet=dict(merged_wallet),
            routed_tier=routed_tier,
        )
        return advice, stats

    def request_coach_advice(
        self,
        req: CoachRequestPayload,
        store: DataStore | None = None,
        tier: str = "a",
    ) -> tuple[CoachAdvicePayload, CloudAdviseStats]:
        """Request cloud AI coach advice supporting tier in ('a', 'b', 'auto')."""
        return self.advise(req, store, tier=tier)

    def submit_match_telemetry(self, payload: dict[str, Any]) -> bool:
        """Best-effort post-match telemetry upload to POST /v1/telemetry/match (5.0s timeout).

        Attaches Bearer token when logged in, otherwise sends anonymously.
        Strictly non-throwing: returns True on HTTP 2xx and False on any failure.
        """
        if not isinstance(payload, dict) or not payload:
            return False
        if not self.base_url and self._http_client_factory is None:
            return False
        try:
            headers = self._auth_headers()
            resp = self._request(
                "POST",
                "/v1/telemetry/match",
                json=payload,
                headers=headers or None,
                timeout=self.TELEMETRY_TIMEOUT_S,
            )
            return 200 <= int(resp.status_code) < 300
        except Exception as exc:
            logger.debug("submit_match_telemetry failed (ignored): %s", exc)
            return False

    def fetch_augment_pair_stats(
        self,
        hero_id: str = "",
        min_games: int = 3,
    ) -> list[dict[str, Any]]:
        """Fetch crowd-sourced augment pair stats from GET /v1/telemetry/augment-pairs (5.0s timeout).

        Strictly best-effort: returns [] on any network, HTTP, or payload parsing failure.
        """
        if not self.base_url and self._http_client_factory is None:
            return []
        params: dict[str, Any] = {"min_games": max(1, int(min_games))}
        clean_hero = str(hero_id or "").strip()
        if clean_hero:
            params["hero_id"] = clean_hero
        try:
            headers = self._auth_headers()
            resp = self._request(
                "GET",
                "/v1/telemetry/augment-pairs",
                params=params,
                headers=headers or None,
                timeout=self.TELEMETRY_TIMEOUT_S,
                _retry_get=False,
            )
            if resp.status_code != 200:
                return []
            body = resp.json()
            if isinstance(body, list):
                return [dict(item) for item in body if isinstance(item, dict)]
            if isinstance(body, dict):
                for key in ("pairs", "items", "stats", "data"):
                    val = body.get(key)
                    if isinstance(val, list):
                        return [dict(item) for item in val if isinstance(item, dict)]
            return []
        except Exception as exc:
            logger.debug("fetch_augment_pair_stats failed (ignored): %s", exc)
            return []

    def get_packages(self) -> dict[str, Any]:
        resp = self._request("GET", "/v1/orders/packages")
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        return _safe_json(resp)

    def create_order(
        self,
        package_id: str,
        channel: str,
        *,
        defer_notify: bool = True,
    ) -> dict[str, Any]:
        resp, gen_before = self._request_with_auth(
            "POST",
            "/v1/orders/create",
            json={
                "package_id": package_id,
                "channel": channel,
                "defer_notify": bool(defer_notify),
            },
        )
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            if resp.status_code == 401:
                self._clear_local_session(expected_gen=gen_before)
                raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录", wallet, extra)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        return _safe_json(resp)

    def notify_order_paid(self, order_no: str) -> dict[str, Any]:
        resp, gen_before = self._request_with_auth(
            "POST",
            f"/v1/orders/{order_no}/notify-paid",
        )
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            if resp.status_code == 401:
                self._clear_local_session(expected_gen=gen_before)
                raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录", wallet, extra)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        return _safe_json(resp)

    def poll_order(self, order_no: str) -> dict[str, Any]:
        resp, gen_before = self._request_with_auth("GET", f"/v1/orders/{order_no}")
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            if resp.status_code == 401:
                self._clear_local_session(expected_gen=gen_before)
                raise CloudCoachError("NOT_LOGGED_IN", 401, "登录状态已失效，请重新登录", wallet, extra)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        data = _safe_json(resp)
        if data.get("status") == "paid" and isinstance(data.get("wallet"), dict):
            with self._lock:
                if self._session_gen == gen_before:
                    self._last_wallet = dict(data["wallet"])
        return data

    def fetch_qr_image_bytes(self, qr_image_url: str) -> bytes:
        target = (qr_image_url or "").strip()
        if not target:
            raise CloudCoachError("QR_MISSING", 404, "未配置收款二维码图片")
        if target.startswith("http://") or target.startswith("https://"):
            path = target
        else:
            path = target if target.startswith("/") else f"/{target}"
        resp = self._request("GET", path)
        if resp.status_code != 200:
            raise CloudCoachError("QR_FETCH_FAILED", resp.status_code, f"获取收款码失败 (HTTP {resp.status_code})")
        return bytes(resp.content)

    def check_update_manifest(self, current_version: str) -> dict[str, Any]:
        resp = self._request(
            "GET",
            "/v1/updates/manifest",
            params={"current_version": current_version},
        )
        if resp.status_code != 200:
            code, msg, wallet, extra = _parse_error_payload(resp)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        return _safe_json(resp)

    def submit_feedback(
        self,
        *,
        category: str,
        message: str,
        contact: str = "",
        dataset_patch: str = "",
        dataset_mode: str = "",
        os_info: str = "",
        match_count: int = 0,
        diag: dict[str, Any] | None = None,
        log_gzip_b64: str = "",
        log_text: str = "",
    ) -> dict[str, Any]:
        """Submit user feedback with optional gzip-compressed sanitized rolling runtime logs.

        Works both when signed in (automatically attaches Bearer token) and anonymously
        (so players can still report login or startup issues).
        """
        payload: dict[str, Any] = {
            "category": (category or "other").strip() or "other",
            "message": (message or "").strip(),
            "contact": (contact or "").strip()[:128],
            "client_version": APP_VERSION,
            "dataset_patch": (dataset_patch or "").strip()[:32],
            "dataset_mode": (dataset_mode or "").strip()[:32],
            "os_info": (os_info or platform.platform()).strip()[:128],
            "match_count": max(0, min(20, int(match_count or 0))),
            "diag": dict(diag) if isinstance(diag, dict) else {},
        }
        if log_gzip_b64:
            payload["log_gzip_b64"] = log_gzip_b64
        elif log_text:
            payload["log_text"] = log_text

        headers = self._auth_headers()
        resp = self._request(
            "POST",
            "/v1/feedback",
            json=payload,
            headers=headers or None,
            timeout=18.0,
        )
        if resp.status_code not in (200, 201):
            code, msg, wallet, extra = _parse_error_payload(resp)
            raise CloudCoachError(code, resp.status_code, msg, wallet, extra)
        return _safe_json(resp)


@dataclass
class _PendingCloudJob:
    req: CoachRequestPayload
    store: DataStore | None
    tier: str


class CloudCoachWorker:
    """Single-worker latest-wins background executor for commercial cloud AI Coach."""

    def __init__(
        self,
        *,
        client: CloudGatewayClient,
        on_advice: Callable[[CoachAdvicePayload, CloudAdviseStats], None] | None = None,
        on_cutoff: Callable[[str, str, dict[str, Any]], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self._client = client
        self._on_advice = on_advice
        self._on_cutoff = on_cutoff
        self._on_error = on_error

        self._cond = threading.Condition()
        self._pending: _PendingCloudJob | None = None
        self._latest_submitted_id: str = ""
        self._in_flight_trigger: str = ""
        self._stopped = False
        self._in_flight = False
        self.last_stats: CloudAdviseStats | None = None
        self.last_error: str = ""

        self._thread = threading.Thread(target=self._run_loop, name="HexCloudCoachWorker", daemon=True)
        self._thread.start()

    @property
    def is_busy(self) -> bool:
        with self._cond:
            return self._in_flight or (self._pending is not None)

    def has_inflight_trigger(self, trigger_reason: str) -> bool:
        """Return True iff a queued or non-cancelled in-flight cloud call matches trigger_reason."""
        target = (trigger_reason or "").strip().lower()
        if not target:
            return False
        with self._cond:
            if (
                self._in_flight
                and bool(self._latest_submitted_id)
                and self._in_flight_trigger.strip().lower() == target
            ):
                return True
            if (
                self._pending is not None
                and (self._pending.req.trigger_reason or "").strip().lower() == target
            ):
                return True
            return False

    def submit(
        self,
        req: CoachRequestPayload,
        store: DataStore | None,
        *,
        tier: str = "a",
    ) -> None:
        norm_tier = (tier or "a").strip().lower()
        if norm_tier not in ("a", "b", "auto"):
            norm_tier = "a"
        with self._cond:
            self._latest_submitted_id = req.request_id
            self._pending = _PendingCloudJob(req=req, store=store, tier=norm_tier)
            self._cond.notify_all()

    def cancel_pending(self, trigger_reasons: set[str] | frozenset[str] | None = None) -> int:
        """Drop queued/in-flight cloud requests matching trigger_reasons so late replies cannot overwrite live advice."""
        cancelled = 0
        with self._cond:
            if self._pending is not None:
                if trigger_reasons is None or self._pending.req.trigger_reason in trigger_reasons:
                    self._pending = None
                    self._latest_submitted_id = ""
                    cancelled += 1
            if self._in_flight and (trigger_reasons is None or self._in_flight_trigger in trigger_reasons):
                self._latest_submitted_id = ""
                cancelled += 1
        return cancelled

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._pending = None
            self._latest_submitted_id = ""
            self._in_flight_trigger = ""
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def _run_loop(self) -> None:
        while True:
            with self._cond:
                while not self._stopped and self._pending is None:
                    self._cond.wait(timeout=1.0)
                if self._stopped:
                    return
                job = self._pending
                self._pending = None
                self._in_flight = True
                self._in_flight_trigger = str(getattr(job.req, "trigger_reason", "") or "") if job else ""

            if job is None:
                with self._cond:
                    self._in_flight = False
                    self._in_flight_trigger = ""
                continue

            try:
                advice, stats = self._client.advise(job.req, job.store, tier=job.tier)
                with self._cond:
                    superseded = job.req.request_id != self._latest_submitted_id
                    self._in_flight = False
                    self._in_flight_trigger = ""
                self.last_stats = stats
                self.last_error = ""
                if superseded:
                    logger.info("Dropping superseded cloud advice for %s", job.req.request_id)
                    continue
                if self._on_advice:
                    self._on_advice(advice, stats)
            except CloudCoachError as exc:
                # If server rejected with 429 in-flight overlap, wait briefly and retry once if still latest
                if exc.status_code == 429 or exc.code == "COACH_REQUEST_IN_FLIGHT":
                    with self._cond:
                        superseded = self._stopped or (job.req.request_id != self._latest_submitted_id)
                    if not superseded:
                        time.sleep(1.5)
                        with self._cond:
                            superseded = self._stopped or (job.req.request_id != self._latest_submitted_id)
                        if not superseded:
                            try:
                                advice, stats = self._client.advise(job.req, job.store, tier=job.tier)
                                with self._cond:
                                    superseded = job.req.request_id != self._latest_submitted_id
                                    self._in_flight = False
                                    self._in_flight_trigger = ""
                                self.last_stats = stats
                                self.last_error = ""
                                if not superseded and self._on_advice:
                                    self._on_advice(advice, stats)
                                continue
                            except CloudCoachError as retry_exc:
                                exc = retry_exc
                            except Exception as retry_err:
                                with self._cond:
                                    superseded = job.req.request_id != self._latest_submitted_id
                                    self._in_flight = False
                                    self._in_flight_trigger = ""
                                err_msg = f"{type(retry_err).__name__}: {retry_err}"
                                self.last_error = err_msg
                                if not superseded and self._on_error:
                                    self._on_error(job.req.request_id, err_msg)
                                continue
                with self._cond:
                    superseded = job.req.request_id != self._latest_submitted_id
                    self._in_flight = False
                    self._in_flight_trigger = ""
                self.last_error = exc.message
                if exc.code in (
                    "INSUFFICIENT_CREDITS",
                    "NOT_LOGGED_IN",
                    "ACCOUNT_DISABLED",
                    "SERVICE_LOCKED",
                    "CLIENT_UPDATE_REQUIRED",
                ) or exc.status_code in (
                    401,
                    402,
                    403,
                    426,
                    503,
                ):
                    logger.warning("Cloud coach cutoff (%s): %s", exc.code, exc.message)
                    if self._on_cutoff:
                        cutoff_payload = dict(exc.wallet or {})
                        if exc.extra:
                            cutoff_payload.update(exc.extra)
                        self._on_cutoff(exc.code, exc.message, cutoff_payload)
                elif not superseded and self._on_error:
                    self._on_error(job.req.request_id, exc.message)
            except Exception as exc:
                with self._cond:
                    superseded = job.req.request_id != self._latest_submitted_id
                    self._in_flight = False
                err_msg = f"{type(exc).__name__}: {exc}"
                self.last_error = err_msg
                logger.warning("Cloud coach call failed for %s: %s", job.req.request_id, err_msg)
                if not superseded and self._on_error:
                    self._on_error(job.req.request_id, err_msg)
