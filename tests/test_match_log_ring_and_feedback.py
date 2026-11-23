"""Unit and GUI tests for 20-match rolling log ring, privacy sanitization, and FeedbackDialog."""

from __future__ import annotations

import base64
import gzip
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from PySide6.QtWidgets import QApplication

from hex_helper.cloud.client import CloudGatewayClient
from hex_helper.config import AppConfig
from hex_helper.logging_ring import (
    DEFAULT_MAX_MATCHES,
    MatchLogRing,
    MatchRingLogHandler,
    compress_log_bundle,
    sanitize_log_text,
)
from hex_helper.ui.feedback_dialog import FeedbackDialog
from hex_helper.ui.lookup_window import LookupWindow


def _ensure_qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def test_match_log_ring_20_match_fifo_rolling_and_persistence(tmp_path: Path) -> None:
    ring_dir = tmp_path / "matches"
    ring = MatchLogRing(ring_dir, max_matches=DEFAULT_MAX_MATCHES)
    assert ring.max_matches == 20

    # Lobby preamble before any match starts
    ring.append_line("2026-11-23 10:00:00 [INFO] boot: HexHelper started", levelno=logging.INFO)

    created_files: dict[int, str] = {}
    for i in range(1, 26):
        entry = ring.begin_match(
            match_key=f"hero_{i}|ally_a|enemy_b",
            hero_id=str(100 + i),
            hero_name=f"英雄{i}",
            dataset_patch="16.23",
            dataset_mode="normal",
        )
        created_files[i] = entry.file_name
        ring.append_line(f"2026-11-23 10:{i:02d}:01 [INFO] game: match {i} tick", levelno=logging.INFO)
        if i % 5 == 0:
            ring.append_line(f"2026-11-23 10:{i:02d}:02 [ERROR] ocr: missed card in match {i}", levelno=logging.ERROR)
        ring.end_match(won=(i % 2), duration_s=900 + i)

    matches = ring.list_matches()
    assert len(matches) == 20
    assert [m.seq for m in matches] == list(range(6, 26))

    # Oldest 5 match files must be physically deleted from disk
    for old_seq in range(1, 6):
        assert not (ring_dir / created_files[old_seq]).exists()
    for kept_seq in range(6, 26):
        assert (ring_dir / created_files[kept_seq]).is_file()

    # Persists cleanly across restart and continues FIFO eviction at match #26
    reloaded = MatchLogRing(ring_dir, max_matches=20)
    assert [m.seq for m in reloaded.list_matches()] == list(range(6, 26))
    entry26 = reloaded.begin_match(match_key="hero_26|x|y", hero_name="卡莎", dataset_patch="16.23")
    assert entry26.seq == 26
    assert [m.seq for m in reloaded.list_matches()] == list(range(7, 27))
    assert not (ring_dir / created_files[6]).exists()

    # Re-attaching the exact same match_key does NOT consume a new rolling slot
    reattached = reloaded.begin_match(match_key="hero_26|x|y", hero_name="卡莎")
    assert reattached.seq == 26
    assert len(reloaded.list_matches()) == 20


def test_match_log_ring_single_match_size_cap_and_handler(tmp_path: Path) -> None:
    ring = MatchLogRing(tmp_path / "matches", max_matches=5, max_match_bytes=16 * 1024)
    handler = MatchRingLogHandler(ring, level=logging.INFO)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    test_logger = logging.getLogger("hex_helper.test_ring_handler")
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False
    test_logger.addHandler(handler)
    try:
        test_logger.info("startup preflight ok")
        entry = ring.begin_match("mk_big", hero_name="阿狸", dataset_patch="16.23", dataset_mode="first-prismatic")
        for idx in range(400):
            test_logger.info("frame=%04d payload=%s", idx, "X" * 90)
        test_logger.warning("transient ocr jitter")
        test_logger.error("simulated failure line")
        ring.end_match(won=1, duration_s=1120)
    finally:
        test_logger.removeHandler(handler)

    saved = (tmp_path / "matches" / entry.file_name).read_text(encoding="utf-8")
    assert len(saved.encode("utf-8")) <= 16 * 1024 + 512
    assert "MATCH #00001" in saved
    assert "simulated failure line" in saved
    stats = ring.summary_stats()
    assert stats["match_count"] == 1
    assert stats["total_warns"] >= 1
    assert stats["total_errors"] >= 1


def test_sanitize_log_text_and_bundle_gzip_roundtrip(tmp_path: Path) -> None:
    fake_gem = "AIza" + "SyDummyKeyValue123456789012345"
    fake_ds = "sk-" + "1234567890abcdef123456"
    fake_cos = "AKID" + "1234567890ABCDEF12345"
    sample = (
        "Authorization: Bearer abcdef1234567890_secret_token\n"
        "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c\n"
        "https://riot:MySuperLcuPass99@127.0.0.1:54321/lol\n"
        "--remoting-auth-token=RiotSecretTok88\n"
        f"gem={fake_gem} ds={fake_ds} cos={fake_cos}\n"
        r"path=C:\Users\AlanPrivate\AppData\Local\HexHelper\logs\hex_helper.log"
    )
    cleaned = sanitize_log_text(sample)
    assert "abcdef1234567890_secret_token" not in cleaned
    assert "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" not in cleaned
    assert "MySuperLcuPass99" not in cleaned
    assert "RiotSecretTok88" not in cleaned
    assert fake_gem not in cleaned
    assert fake_ds not in cleaned
    assert fake_cos not in cleaned
    assert "AlanPrivate" not in cleaned
    assert r"C:\Users\<USER>\AppData" in cleaned

    ring = MatchLogRing(tmp_path / "matches", max_matches=20)
    ring.begin_match("mk_san", hero_name="拉克丝", dataset_patch="16.23")
    ring.append_line(sample, levelno=logging.WARNING)
    ring.end_match(won=1, duration_s=840)

    bundle_text, match_count, meta = ring.build_feedback_bundle()
    assert match_count == 1
    assert meta["max_matches"] == 20
    assert "AlanPrivate" not in bundle_text
    assert "MySuperLcuPass99" not in bundle_text
    assert "拉克丝" in bundle_text

    gz_b64 = compress_log_bundle(bundle_text)
    decompressed = gzip.decompress(base64.b64decode(gz_b64)).decode("utf-8")
    assert decompressed == bundle_text


def test_cloud_client_submit_feedback_sends_optional_auth_and_gzip() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "path": request.url.path,
                "auth": request.headers.get("authorization", ""),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(
            200,
            json={"ok": True, "id": 7, "ticket_no": "FB-20261123-ABCDEF", "log_bytes": 1280},
        )

    transport = httpx.MockTransport(handler)
    client = CloudGatewayClient(
        base_url="https://api.example.test",
        http_client_factory=lambda base_url, timeout: httpx.Client(base_url=base_url, transport=transport),
    )

    # 1. Anonymous submission
    res_anon = client.submit_feedback(
        category="ocr_picker",
        message="第 2 轮选卡有 1 张卡没识别出来",
        contact="qq:12345",
        dataset_patch="16.23",
        dataset_mode="normal",
        match_count=3,
        diag={"ocr_ready": True},
        log_gzip_b64=compress_log_bundle("hello rolling log"),
    )
    assert res_anon["ticket_no"] == "FB-20261123-ABCDEF"
    assert captured[0]["path"] == "/v1/feedback"
    assert captured[0]["auth"] == ""
    assert captured[0]["body"]["match_count"] == 3
    assert captured[0]["body"]["log_gzip_b64"]

    # 2. Signed-in submission automatically attaches Bearer token
    client._session = {"access_token": "tok_test_abc", "refresh_token": "", "username": "alan"}
    client.submit_feedback(category="build_ai", message="阵亡推荐延迟偏高", match_count=1)
    assert captured[1]["auth"] == "Bearer tok_test_abc"


def test_feedback_dialog_hextech_ui_and_submit_flow(tmp_path: Path) -> None:
    app = _ensure_qapp()
    ring = MatchLogRing(tmp_path / "matches", max_matches=20)
    ring.begin_match("mk_ui", hero_name="阿狸", dataset_patch="16.23")
    ring.append_line("2026-11-23 [INFO] picker: detected 4 cards", levelno=logging.INFO)
    ring.end_match(won=1, duration_s=920)

    submitted_payloads: list[dict[str, Any]] = []

    def fake_submit(payload: dict[str, Any]) -> dict[str, Any]:
        submitted_payloads.append(payload)
        return {"ok": True, "ticket_no": "FB-20261123-112233", "log_bytes": 2048}

    dlg = FeedbackDialog(
        log_ring=ring,
        submit_fn=fake_submit,
        diag_snapshot={"ocr_ready": True, "capture_backend": "dxcam"},
        dataset_patch="16.23",
        dataset_mode="normal",
        username="alan_player",
    )
    # Strict desktop aesthetic rule: ZERO Web Admin Tactile Orange (#FF5A1F)
    assert "#ff5a1f" not in dlg.styleSheet().lower()
    assert "1 / 20 局" in dlg.log_summary_lbl.text()

    # Empty message is blocked cleanly
    dlg._handle_submit()
    assert not submitted_payloads
    assert "至少输入 2 个字" in dlg.status_lbl.text()

    # Preview toggles cleanly
    assert not dlg.preview_box.isVisible()
    dlg._toggle_preview()
    assert "阿狸" in dlg.preview_box.toPlainText()

    # Select category and submit
    dlg._select_category("build_ai")
    dlg.message_edit.setPlainText("阿狸首棱彩局出装推荐顺序可以更紧凑")
    dlg.contact_edit.setText("wx:alan_test")
    dlg._handle_submit()

    # Wait briefly for background thread + Qt signal delivery
    import time

    deadline = time.monotonic() + 3.0
    while not dlg._last_ticket_no and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)

    assert len(submitted_payloads) == 1
    assert submitted_payloads[0]["category"] == "build_ai"
    assert submitted_payloads[0]["match_count"] == 1
    assert submitted_payloads[0]["log_gzip_b64"]
    assert dlg._last_ticket_no == "FB-20261123-112233"
    assert "FB-20261123-112233" in dlg.success_lbl.text()
    dlg.close()


def test_lookup_window_wires_feedback_buttons() -> None:
    _ensure_qapp()
    clicked: list[int] = []
    win = LookupWindow(cfg=AppConfig(), on_open_feedback=lambda: clicked.append(1))
    win.feedback_btn.click()
    win.diag_feedback_btn.click()
    assert len(clicked) == 2
    win.close()
