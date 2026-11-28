"""Loopback-only HTTP + atomic file bridge connecting HexHelper to a live Antigravity session."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload

logger = logging.getLogger(__name__)
_UNSET = object()


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class AgentBridgeServer:
    """Thread-safe 127.0.0.1 HTTP server + file mirror for real-time Antigravity coaching."""

    def __init__(
        self,
        bridge_dir: Path,
        host: str = "127.0.0.1",
        port: int = 18421,
        on_advice: Callable[[CoachAdvicePayload], None] | None = None,
        on_online_changed: Callable[[bool], None] | None = None,
        dev_events_provider: Callable[[int], list[dict[str, Any]]] | None = None,
        dev_vision_provider: Callable[[int], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.bridge_dir = Path(bridge_dir)
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = int(port)
        self._on_advice = on_advice
        self._on_online_changed = on_online_changed
        self._dev_events_provider = dev_events_provider
        self._dev_vision_provider = dev_vision_provider

        self._cond = threading.Condition()
        self._latest_request: CoachRequestPayload | None = None
        self._latest_advice: CoachAdvicePayload | None = None
        self._last_heartbeat_mono: float = 0.0
        self._was_online: bool = False
        self._stopped: bool = False

        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        return self.port

    def is_coach_online(self, ttl_seconds: float = 15.0) -> bool:
        with self._cond:
            if self._last_heartbeat_mono <= 0.0:
                return False
            return (time.monotonic() - self._last_heartbeat_mono) <= ttl_seconds

    def touch_heartbeat(self) -> None:
        notify_online = False
        with self._cond:
            self._last_heartbeat_mono = time.monotonic()
            if not self._was_online:
                self._was_online = True
                notify_online = True
        if notify_online and self._on_online_changed:
            try:
                self._on_online_changed(True)
            except Exception:
                pass

    def check_online_timeout(self, ttl_seconds: float = 15.0) -> bool:
        """Called periodically by UI timer to transition online badge to offline when relay stops."""
        online = self.is_coach_online(ttl_seconds)
        changed = False
        with self._cond:
            if self._was_online and not online:
                self._was_online = False
                changed = True
        if changed and self._on_online_changed:
            try:
                self._on_online_changed(False)
            except Exception:
                pass
        return online

    def publish_request(self, payload: CoachRequestPayload) -> None:
        with self._cond:
            self._latest_request = payload
            self._cond.notify_all()
        try:
            _atomic_write_json(self.bridge_dir / "request.json", payload.to_dict())
        except Exception as exc:
            logger.debug("Failed writing request.json: %s", exc)

    def submit_advice(self, advice: CoachAdvicePayload) -> None:
        with self._cond:
            self._latest_advice = advice
            self._last_heartbeat_mono = time.monotonic()
            was = self._was_online
            self._was_online = True
        try:
            _atomic_write_json(self.bridge_dir / "advice.json", advice.to_dict())
        except Exception as exc:
            logger.debug("Failed writing advice.json: %s", exc)
        if not was and self._on_online_changed:
            try:
                self._on_online_changed(True)
            except Exception:
                pass
        if self._on_advice:
            try:
                self._on_advice(advice)
            except Exception as exc:
                logger.exception("on_advice callback failed: %s", exc)

    def latest_request(self) -> CoachRequestPayload | None:
        with self._cond:
            return self._latest_request

    def latest_advice(self) -> CoachAdvicePayload | None:
        with self._cond:
            return self._latest_advice

    def set_dev_providers(
        self,
        *,
        dev_events_provider: Callable[[int], list[dict[str, Any]]] | None | object = _UNSET,
        dev_vision_provider: Callable[[int], list[dict[str, Any]]] | None | object = _UNSET,
    ) -> None:
        """Attach or replace read-only DevLab event/vision ring providers at runtime."""
        with self._cond:
            if dev_events_provider is not _UNSET:
                self._dev_events_provider = dev_events_provider  # type: ignore[assignment]
            if dev_vision_provider is not _UNSET:
                self._dev_vision_provider = dev_vision_provider  # type: ignore[assignment]

    def get_dev_events(self, since_seq: int = 0) -> tuple[list[dict[str, Any]], int]:
        threshold = max(0, int(since_seq or 0))
        with self._cond:
            provider = self._dev_events_provider
        if provider is None:
            return [], threshold
        items = list(provider(threshold) or [])
        last_seq = threshold
        for ev in items:
            if isinstance(ev, dict):
                try:
                    last_seq = max(last_seq, int(ev.get("ev_seq", 0) or 0))
                except (TypeError, ValueError):
                    pass
        return items, last_seq

    def get_dev_vision_traces(self, since_seq: int = 0) -> tuple[list[dict[str, Any]], int]:
        threshold = max(0, int(since_seq or 0))
        with self._cond:
            provider = self._dev_vision_provider
        if provider is None:
            return [], threshold
        items = list(provider(threshold) or [])
        last_seq = threshold
        for ev in items:
            if isinstance(ev, dict):
                try:
                    last_seq = max(last_seq, int(ev.get("ev_seq", 0) or 0))
                except (TypeError, ValueError):
                    pass
        return items, last_seq

    def wait_for_request(self, after_id: str, timeout: float = 20.0) -> CoachRequestPayload | None:
        self.touch_heartbeat()
        deadline = time.monotonic() + max(0.1, min(60.0, timeout))
        with self._cond:
            while not self._stopped:
                if self._latest_request is not None and self._latest_request.request_id != after_id:
                    return self._latest_request
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=min(remaining, 1.0))
        return None

    def start(self) -> int:
        if self._server is not None:
            return self.bound_port
        self._stopped = False
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

            def _send_json(self, status: int, body: dict) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/api/coach/status":
                    req = bridge.latest_request()
                    adv = bridge.latest_advice()
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "running": True,
                            "coach_online": bridge.is_coach_online(),
                            "latest_request_id": req.request_id if req else "",
                            "has_advice": adv is not None,
                            "latest_request": req.to_dict() if req else None,
                            "latest_advice": adv.to_dict() if adv else None,
                        },
                    )
                    return

                if parsed.path == "/api/coach/wait":
                    qs = parse_qs(parsed.query)
                    after = (qs.get("after") or [""])[0]
                    try:
                        timeout = float((qs.get("timeout") or ["15"])[0])
                    except ValueError:
                        timeout = 15.0
                    req = bridge.wait_for_request(after_id=after, timeout=timeout)
                    if req is not None:
                        self._send_json(HTTPStatus.OK, {"event": True, "request": req.to_dict()})
                    else:
                        self._send_json(HTTPStatus.OK, {"event": False, "timeout": True})
                    return

                if parsed.path == "/api/dev/events":
                    qs = parse_qs(parsed.query)
                    try:
                        since_seq = max(0, int((qs.get("since_seq") or ["0"])[0]))
                    except (TypeError, ValueError):
                        since_seq = 0
                    try:
                        events, last_seq = bridge.get_dev_events(since_seq)
                        self._send_json(
                            HTTPStatus.OK,
                            {"ok": True, "events": events, "last_seq": last_seq},
                        )
                    except Exception as exc:
                        logger.debug("dev_events_provider failed: %s", exc)
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"ok": False, "error": "provider_error"},
                        )
                    return

                if parsed.path == "/api/dev/vision/trace":
                    qs = parse_qs(parsed.query)
                    try:
                        since_seq = max(0, int((qs.get("since_seq") or ["0"])[0]))
                    except (TypeError, ValueError):
                        since_seq = 0
                    try:
                        traces, last_seq = bridge.get_dev_vision_traces(since_seq)
                        self._send_json(
                            HTTPStatus.OK,
                            {"ok": True, "traces": traces, "last_seq": last_seq},
                        )
                    except Exception as exc:
                        logger.debug("dev_vision_provider failed: %s", exc)
                        self._send_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"ok": False, "error": "provider_error"},
                        )
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"

                if parsed.path == "/api/coach/heartbeat":
                    bridge.touch_heartbeat()
                    self._send_json(HTTPStatus.OK, {"ok": True, "coach_online": True})
                    return

                if parsed.path == "/api/coach/advice":
                    try:
                        data = json.loads(raw.decode("utf-8"))
                        advice = CoachAdvicePayload.from_dict(data)
                        bridge.submit_advice(advice)
                        self._send_json(
                            HTTPStatus.OK,
                            {"ok": True, "request_id": advice.request_id, "items": len(advice.next_items)},
                        )
                    except Exception as exc:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, name="HexCoachBridge", daemon=True)
        self._thread.start()
        return self.bound_port

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        self._thread = None
