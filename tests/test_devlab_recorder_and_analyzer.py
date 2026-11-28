"""Comprehensive test suite for HexHelper DevLab Flight Recorder, MatchActionTracker,
PENDING_EOG race protection, Offline Replay Analyzer (all 10 detectors + CLI),
and Commercial Build Hard-Isolation gates.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

from hex_helper import app as app_mod
from hex_helper import edition as edition_mod
from hex_helper import main as main_mod
from hex_helper.app import GameState, HexHelperController
from hex_helper.config import AppConfig
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.devlab.analyzer import (
    DevLabAnalyzer,
    EV_AUGMENT_UNLOCK_SILENT_MISS,
)
from hex_helper.devlab.recorder import DevLabFlightRecorder
from hex_helper.devlab.schema import (
    DOMAIN_COACH,
    DOMAIN_GAME,
    DOMAIN_PERF,
    DOMAIN_REC,
    DOMAIN_UI,
    DOMAIN_VISION,
    EV_AUGMENT_OFFER_REC,
    EV_AUGMENT_PICK_DELTA,
    EV_AUGMENT_REROLL_DELTA,
    EV_COACH_TURN,
    EV_DEATH_SHOP_WINDOW,
    EV_ITEM_PURCHASE_DELTA,
    EV_LEVEL_MILESTONE,
    EV_LOCAL_BUILD_REC,
    EV_MAIN_THREAD_STALL,
    EV_MATCH_EOG,
    EV_SMART_PICKER_GATE,
    EV_UI_LAYOUT_ADJUSTED,
    EV_UI_MANUAL_TRIGGER,
    EV_UI_OVERLAY_SESSION,
    EV_UI_SCROLL_OVERFLOW,
    EV_VISION_CLICK_RESOLVED,
    EV_VISION_ICON_VERIFY,
    EV_VISION_LATENCY_SPIKE,
    EV_VISION_OCR_ANOMALY,
    EV_VISION_PICKER_TRANSITION,
    EV_VISION_TRANSMUTE_TRACE,
    EV_VISION_USER_CORRECTION,
    hash_match_key,
    scrub_payload,
)
from hex_helper.game.action_tracker import MatchActionTracker
from hex_helper.game.models import ChampSelectState, LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.logging_ring import MatchLogRing, MatchRingLogHandler
from hex_helper.scoring.matchup_optimizer import reconcile_match_build_plan_with_trace
from hex_helper.ui.lookup_window import HextechDevLabBadge, LookupWindow
from hex_helper.vision.capture import CaptureResult, WindowRect

REPO_ROOT = Path(__file__).resolve().parents[1]

ALL_10_DETECTORS: tuple[str, ...] = (
    "DET_EOG_RACE_LOST",
    "DET_OCR_SILENT_MISS_AT_UNLOCK_LEVEL",
    "DET_OCR_NEARMISS_OR_PASS2_SHADOWED",
    "DET_REROLL_DESYNC_OR_Y_THRESHOLD_CLIP",
    "DET_EMPTY_ICON_BANK_OR_HUD_MISALIGN",
    "DET_TAB_PASSIVE_HOVER_POLLUTION",
    "DET_SCORING_TRAP_OR_PROFILE_CONFLICT",
    "DET_COACH_TIMING_AND_HALLUCINATION",
    "DET_OVERLAY_IRRITATION_AND_OVERFLOW",
    "DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION",
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class _NoopCapturer:
    backend = "noop"
    consecutive_black_frames = 0

    def grab(self, **_kw: Any) -> CaptureResult:
        return CaptureResult(frame_bgr=None, backend="noop", is_black=True, mean_luma=0.0)

    def close(self) -> None:
        return None


class _NoopHotkeys(QAbstractNativeEventFilter):
    def nativeEventFilter(self, _eventType: Any, _message: Any) -> tuple[bool, int]:
        return False, 0

    def register(self, *_a: Any, **_kw: Any) -> None:
        return None

    def unregister_all(self) -> None:
        return None


def _patch_headless_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)
    monkeypatch.setattr(
        app_mod,
        "find_game_window",
        lambda: WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080),
    )
    monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: True)


def _make_recipe_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", proxy_url="")
    static.items = {
        "1038": ItemStaticInfo("1038", "暴风之剑", 1300, ("Damage",), {"FlatPhysicalDamageMod": 40.0}, "", "other"),
        "1037": ItemStaticInfo("1037", "十字镐", 875, ("Damage",), {"FlatPhysicalDamageMod": 25.0}, "", "other"),
        "1018": ItemStaticInfo("1018", "灵巧披风", 600, ("CriticalStrike",), {"FlatCritChanceMod": 0.15}, "", "other"),
        "1043": ItemStaticInfo("1043", "反曲之弓", 700, ("AttackSpeed",), {}, "", "other"),
        "3086": ItemStaticInfo("3086", "狂热", 1200, ("AttackSpeed", "CriticalStrike"), {}, "", "other"),
        "3031": ItemStaticInfo(
            "3031",
            "无尽之刃",
            3400,
            ("Damage", "CriticalStrike"),
            {"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25},
            "",
            "complete_equipment",
            components=("1038", "1037", "1018"),
        ),
        "3085": ItemStaticInfo(
            "3085",
            "卢安娜的飓风",
            2650,
            ("AttackSpeed", "CriticalStrike"),
            {"PercentAttackSpeedMod": 0.40, "FlatCritChanceMod": 0.25},
            "",
            "complete_equipment",
            components=("1043", "3086"),
        ),
        "3036": ItemStaticInfo(
            "3036",
            "多米尼克领主的致意",
            3000,
            ("Damage", "ArmorPenetration", "CriticalStrike"),
            {"FlatPhysicalDamageMod": 35.0, "FlatCritChanceMod": 0.25},
            "",
            "complete_equipment",
            components=("3035", "1018"),
        ),
        "3072": ItemStaticInfo(
            "3072",
            "饮血剑",
            3400,
            ("Damage", "LifeSteal"),
            {"FlatPhysicalDamageMod": 80.0},
            "",
            "complete_equipment",
            components=("1038", "1037"),
        ),
    }
    static.hero_aliases["222"] = "Jinx"
    static.hero_cn_names["222"] = "金克丝"

    store = DataStore(static)
    store._descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes["222"] = HeroStat("222", "金克丝", "暴走萝莉", 50000, 26500, 0.53, 0.08, 12, False)
    store._core_builds["222"] = [CoreBuild(("3085", "3031", "3036"), True, 8000, 4400, 0.55, 0.35)]
    store._augments["1001"] = AugmentInfo("1001", "珠光护手", "prismatic", "技能可暴击")
    return store


def _make_player(
    champion: str,
    raw_name: str,
    team: str = "ORDER",
    *,
    is_dead: bool = False,
    level: int = 3,
    kills: int = 0,
    deaths: int = 0,
    assists: int = 0,
    items: tuple[str, ...] = (),
    item_details: list[dict[str, Any]] | None = None,
) -> PlayerLiveInfo:
    return PlayerLiveInfo(
        riot_id="anon",
        champion_name=champion,
        raw_champion_name=raw_name,
        team=team,
        is_dead=is_dead,
        level=level,
        kills=kills,
        deaths=deaths,
        assists=assists,
        item_ids=items,
        item_details=item_details or [],
    )


def _collect_all_keys(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            _collect_all_keys(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_all_keys(item, out)


# ---------------------------------------------------------------------------
# 1. PII Red-Line Scrubbing
# ---------------------------------------------------------------------------


def test_pii_redline_scrubbing_events_meta_debug_log_and_doctor(tmp_path: Path) -> None:
    uuid_puuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    riot_puuid_78 = "aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uV1wX2yZ3aB4cD5eF6gH7iJ8kL9mN0oP1qR2sT3uV4wX5yZ6"
    assert len(riot_puuid_78) == 78
    riot_id_en = "PlayerName#NA1"
    riot_id_cn = "阿岚大乱斗#CN1"
    gemini_key = "AIza" + "SyTestSecretGeminiKey1234567890abcd"
    jwt_token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkFsaWNlIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    bearer_secret = "SecretBearerToken_abcdef1234567890"
    lcu_auth_short = "https://riot:pass@127.0.0.1:54321/lol"
    lcu_auth_long = "https://riot:MySuperLcuPass99@127.0.0.1:54321/lol"
    win_user_path = r"C:\Users\Alice\Desktop\secret.png"

    blacklisted_keys = ("puuid", "summonerName", "riotId", "api_key", "chat_text", "save_path")

    # Direct scrubber sanity check (must preserve legitimate non-PII tokens)
    direct = scrub_payload(
        {
            "puuid": uuid_puuid,
            "summonerName": "AliceSummoner",
            "riotId": riot_id_en,
            "api_key": gemini_key,
            "chat_text": "hello all chat",
            "save_path": win_user_path,
            "safe_note": f"match#42 hero=金克丝 {riot_id_en} {riot_puuid_78} {win_user_path}",
            "nested": {
                "puuid": riot_puuid_78,
                "chat_text": "secret chat",
                "auth_line": f"Bearer {bearer_secret} {jwt_token} {lcu_auth_short}",
            },
        }
    )
    keys_found: set[str] = set()
    _collect_all_keys(direct, keys_found)
    for bad_k in blacklisted_keys:
        assert bad_k not in keys_found
    assert "match#42" in direct["safe_note"]
    assert "金克丝" in direct["safe_note"]
    assert r"C:\Users\<USER>\Desktop\secret.png" in direct["safe_note"]

    devlab_dir = tmp_path / "devlab"
    recorder = DevLabFlightRecorder(
        root_dir=devlab_dir,
        enable_roi_snapshots=False,
        enable_debug_log=True,
        app_version="1.4.7+pii0001",
    )
    try:
        recorder.begin_match(
            match_key_hash=hash_match_key("987654321"),
            hero_id="222",
            hero_name="金克丝",
            dataset_tag="16.23/normal",
            game_mode="KIWI",
        )
        recorder.update_match_meta(
            puuid=uuid_puuid,
            summonerName="AliceSummoner",
            riotId=riot_id_en,
            api_key=gemini_key,
            chat_text="private chat line",
            save_path=win_user_path,
            extra_note=f"saw {riot_id_cn} at {win_user_path} auth={lcu_auth_short}",
        )
        recorder.emit(
            DOMAIN_GAME,
            "pii_probe_event",
            {
                "puuid": uuid_puuid,
                "summonerName": "AliceSummoner",
                "riotId": riot_id_en,
                "api_key": gemini_key,
                "chat_text": "leaked chat message",
                "save_path": win_user_path,
                "note_uuid": f"uuid={uuid_puuid}",
                "note_puuid78": f"id={riot_puuid_78}",
                "note_riot_id": f"player {riot_id_en} and {riot_id_cn}",
                "note_gemini": f"key={gemini_key}",
                "note_jwt": f"jwt={jwt_token}",
                "note_bearer": f"Authorization: Bearer {bearer_secret}",
                "note_lcu": f"url={lcu_auth_short} alt={lcu_auth_long}",
                "note_path": f"file={win_user_path}",
                "nested_list": [{"api_key": gemini_key, "desc": f"{riot_id_en} {win_user_path}"}],
            },
            game_time_s=42.0,
        )

        # Log DEBUG lines through isolated hex_helper logger
        hex_test_logger = logging.getLogger("hex_helper.devlab_pii_test")
        hex_test_logger.debug(
            "debug_pii uuid=%s puuid78=%s riot=%s cn=%s gem=%s jwt=%s bearer=Bearer %s lcu=%s lcu2=%s path=%s",
            uuid_puuid,
            riot_puuid_78,
            riot_id_en,
            riot_id_cn,
            gemini_key,
            jwt_token,
            bearer_secret,
            lcu_auth_short,
            lcu_auth_long,
            win_user_path,
        )

        recorder.end_match(
            won=1,
            duration_s=612,
            eog_source="eog_stats_block",
            summary_extra={
                "puuid": riot_puuid_78,
                "save_path": win_user_path,
                "note": f"end {riot_id_en} {gemini_key} {lcu_auth_short}",
            },
        )
        assert recorder.flush(timeout_s=3.0) is True
    finally:
        recorder.flush_and_close()

    match_dirs = list((devlab_dir / "matches").iterdir())
    assert len(match_dirs) == 1
    mdir = match_dirs[0]

    events_path = mdir / "events.jsonl"
    meta_path = mdir / "meta.json"
    debug_path = mdir / "debug_runtime.log"
    assert events_path.is_file()
    assert meta_path.is_file()
    assert debug_path.is_file()

    forbidden_plaintexts = (
        uuid_puuid,
        riot_puuid_78,
        riot_id_en,
        riot_id_cn,
        gemini_key,
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        bearer_secret,
        "riot:pass@",
        "riot:MySuperLcuPass99@",
        r"\Alice\\",
        r"Users\Alice",
        "AliceSummoner",
        "leaked chat message",
        "private chat line",
    )

    for artifact in (events_path, meta_path, debug_path):
        raw_text = artifact.read_text(encoding="utf-8")
        for secret in forbidden_plaintexts:
            assert secret not in raw_text, f"Plaintext PII {secret!r} leaked into {artifact.name}!"

    # Assert blacklisted keys completely stripped from events.jsonl and meta.json
    meta_obj = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_keys: set[str] = set()
    _collect_all_keys(meta_obj, meta_keys)
    for bad_k in blacklisted_keys:
        assert bad_k not in meta_keys

    for line in events_path.read_text(encoding="utf-8").splitlines():
        ev_obj = json.loads(line)
        ev_keys: set[str] = set()
        _collect_all_keys(ev_obj, ev_keys)
        for bad_k in blacklisted_keys:
            assert bad_k not in ev_keys

    # Verify debug_runtime.log actually recorded scrubbed debug content
    dbg_text = debug_path.read_text(encoding="utf-8")
    assert "debug_pii" in dbg_text
    assert "<PUUID_REDACTED>" in dbg_text
    assert "<RIOT_ID_REDACTED>" in dbg_text
    assert "<API_KEY_REDACTED>" in dbg_text
    assert "<JWT_REDACTED>" in dbg_text
    assert r"C:\Users\<USER>\Desktop\secret.png" in dbg_text

    # Assert devlab doctor reports clean PII scan
    analyzer = DevLabAnalyzer(root_dir=devlab_dir)
    diag = analyzer.doctor()
    assert diag["checks"]["pii_scan"]["status"] == "pass", diag["checks"]["pii_scan"]
    assert diag["checks"]["pii_scan"]["leak_count"] == 0
    assert diag["status"] == "pass", diag

    # Negative verification: raw leak appended directly MUST be caught by doctor()
    with debug_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\nUNSCRUBBED {riot_id_en} {gemini_key}\n")
    diag_dirty = analyzer.doctor()
    assert diag_dirty["checks"]["pii_scan"]["status"] == "fail"
    assert diag_dirty["checks"]["pii_scan"]["leak_count"] >= 2
    assert diag_dirty["status"] == "fail"


# ---------------------------------------------------------------------------
# 2. MatchActionTracker Diffs, Causal Attribution, 800G Override & PENDING_EOG
# ---------------------------------------------------------------------------


def test_match_action_tracker_champ_select_diffs_dodge_and_lock() -> None:
    emitted: list[tuple[str, str, dict[str, Any], float | None]] = []
    tracker = MatchActionTracker(lambda d, e, p, gt: emitted.append((d, e, p, gt)))

    # Scenario A: bench swap -> reroll -> spell change -> dodge
    cs1 = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="222",
        team_hero_ids=("222", "101"),
        bench_hero_ids=("10", "236"),
        rerolls_remaining=2,
        spell1_id=4,
        spell2_id=32,
        timer_phase="FINALIZATION",
        timer_remaining_ms=30000,
    )
    tracker.on_lcu_champ_select("ChampSelect", cs1, goal_mode="climb", best_swap_hero_id="10")
    # Identical poll is deduped (no duplicate champ_select_recommendation)
    tracker.on_lcu_champ_select("ChampSelect", cs1, goal_mode="climb", best_swap_hero_id="10")
    assert sum(1 for _, e, _, _ in emitted if e == "champ_select_recommendation") == 1

    # Bench swap from 222 -> 10 (best_swap)
    cs2 = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="10",
        team_hero_ids=("10", "101"),
        bench_hero_ids=("222", "236"),
        rerolls_remaining=2,
        spell1_id=4,
        spell2_id=32,
    )
    tracker.on_lcu_champ_select("ChampSelect", cs2, goal_mode="climb", best_swap_hero_id="10")

    # Reroll: rerolls_remaining drops 2 -> 1, hero changes 10 -> 99
    cs3 = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="99",
        team_hero_ids=("99", "101"),
        bench_hero_ids=("10", "222", "236"),
        rerolls_remaining=1,
        spell1_id=4,
        spell2_id=32,
    )
    tracker.on_lcu_champ_select("ChampSelect", cs3, goal_mode="climb", best_swap_hero_id="99")

    # Spell change: (4, 32) -> (4, 7)
    cs4 = ChampSelectState(
        queue_id=2400,
        game_mode="KIWI",
        is_mayhem=True,
        my_hero_id="99",
        team_hero_ids=("99", "101"),
        bench_hero_ids=("10", "222", "236"),
        rerolls_remaining=1,
        spell1_id=4,
        spell2_id=7,
    )
    tracker.on_lcu_champ_select("ChampSelect", cs4, goal_mode="climb", best_swap_hero_id="99")

    # Dodge -> Lobby
    tracker.on_lcu_champ_select("Lobby", None)

    actions = [p for _, e, p, _ in emitted if e == "champ_select_action"]
    assert [a["action_type"] for a in actions] == ["bench_swap", "reroll", "spell_change"]
    assert actions[0]["from_hero"] == "222" and actions[0]["to_hero"] == "10" and actions[0]["was_best_swap"] is True
    assert actions[1]["from_hero"] == "10" and actions[1]["to_hero"] == "99" and actions[1]["rerolls_remaining"] == 1
    assert actions[2]["from_spells"] == [4, 32] and actions[2]["to_spells"] == [4, 7]

    outcomes = [p for _, e, p, _ in emitted if e == "champ_select_outcome_delta"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "dodged"
    assert outcomes[0]["next_phase"] == "Lobby"
    assert outcomes[0]["action_count"] == 3

    # Scenario B: swap to best_swap and lock -> InProgress
    emitted.clear()
    tracker.on_lcu_champ_select("ChampSelect", cs1, goal_mode="climb", best_swap_hero_id="10")
    tracker.on_lcu_champ_select("ChampSelect", cs2, goal_mode="climb", best_swap_hero_id="10")
    tracker.on_lcu_champ_select("InProgress", None)

    lock_outcomes = [p for _, e, p, _ in emitted if e == "champ_select_outcome_delta"]
    assert len(lock_outcomes) == 1
    assert lock_outcomes[0]["outcome"] == "locked"
    assert lock_outcomes[0]["final_hero_id"] == "10"
    assert lock_outcomes[0]["followed_best_swap"] is True


def test_match_action_tracker_undo_filter_and_strict_causal_attribution() -> None:
    emitted: list[tuple[str, str, dict[str, Any], float | None]] = []
    now = [100.0]
    tracker = MatchActionTracker(
        lambda d, e, p, gt: emitted.append((d, e, p, gt)),
        clock=lambda: now[0],
        undo_window_s=2.0,
    )

    e1 = _make_player("亚托克斯", "Aatrox", team="CHAOS")

    def _snap(gt: float, gold: float, item_details: list[dict[str, Any]]) -> LiveMatchSnapshot:
        me = _make_player(
            "金克丝",
            "Jinx",
            team="ORDER",
            level=6,
            items=tuple(str(d["item_id"]) for d in item_details),
            item_details=item_details,
        )
        return LiveMatchSnapshot(gt, me, (me,), (e1,), gold)

    # 1) 2s Undo filter: buy item 3031, refund within 1.5s -> marked undo, NOT purchase adoption
    tracker.on_live_snapshot(_snap(20.0, 3500.0, []))
    now[0] = 101.0
    tracker.record_local_build_rec(25.0, [{"item_id": "3031"}, {"item_id": "3085"}])

    now[0] = 102.0
    tracker.on_live_snapshot(_snap(26.0, 100.0, [{"item_id": 3031, "count": 1, "price": 3400}]))
    assert not any(e == "item_purchase_and_plan_override_delta" for _, e, _, _ in emitted)

    now[0] = 103.5
    tracker.on_live_snapshot(_snap(27.5, 3500.0, []))
    tx_events = [p for _, e, p, _ in emitted if e == "item_transaction"]
    assert len(tx_events) == 1
    assert tx_events[0]["action"] == "undo"
    assert tx_events[0]["item_id"] == 3031
    assert not any(e == "item_purchase_and_plan_override_delta" for _, e, _, _ in emitted)

    # 2) Strict causal ordering: rec emitted AFTER purchase (even inside undo window) MUST NOT be attributed
    emitted.clear()
    tracker.reset()
    now[0] = 200.0
    tracker.on_live_snapshot(_snap(30.0, 3600.0, []))

    now[0] = 201.0
    tracker.on_live_snapshot(_snap(35.0, 950.0, [{"item_id": 3085, "count": 1, "price": 2650}]))

    # Emit BOTH local rec and coach advice AFTER the purchase of 3085
    now[0] = 201.4
    tracker.record_local_build_rec(35.4, [{"item_id": "3085"}, {"item_id": "3031"}])
    tracker.record_coach_advice(35.5, request_id="req-after", trigger_reason="manual_f6", recommended_item_ids=[3085])

    # Advance past 2s undo window -> confirm purchase 3085
    now[0] = 204.0
    tracker.on_live_snapshot(_snap(38.0, 950.0, [{"item_id": 3085, "count": 1, "price": 2650}]))
    deltas = [p for _, e, p, _ in emitted if e == "item_purchase_and_plan_override_delta"]
    assert len(deltas) == 1
    assert deltas[0]["item_id"] == 3085
    assert deltas[0]["matched_local_rank"] == 0
    assert deltas[0]["matched_coach"] is False
    assert deltas[0]["sided_with"] == "neither"
    assert deltas[0]["local_top_items"] == []
    assert deltas[0]["coach_rec_items"] == []

    # 3) Recommendation emitted BEFORE purchase accurately attributes coach vs local disagreement
    emitted.clear()
    now[0] = 210.0
    tracker.record_local_build_rec(45.0, [{"item_id": "3031"}, {"item_id": "3036"}])
    now[0] = 211.0
    tracker.record_coach_advice(
        46.0,
        request_id="req-disagree",
        trigger_reason="death_enemy_build_changed",
        recommended_item_ids=[3072, 3036],
        latency_ms=1120.0,
    )

    # Player sides with coach -> buys 3072
    now[0] = 215.0
    inv_coach = [
        {"item_id": 3085, "count": 1, "price": 2650},
        {"item_id": 3072, "count": 1, "price": 3400},
    ]
    tracker.on_live_snapshot(_snap(50.0, 100.0, inv_coach))
    now[0] = 218.0
    tracker.on_live_snapshot(_snap(53.0, 100.0, inv_coach))

    coach_deltas = [p for _, e, p, _ in emitted if e == "item_purchase_and_plan_override_delta"]
    assert len(coach_deltas) == 1
    assert coach_deltas[0]["item_id"] == 3072
    assert coach_deltas[0]["matched_local_rank"] == 0
    assert coach_deltas[0]["matched_coach"] is True
    assert coach_deltas[0]["local_vs_coach_disagreed"] is True
    assert coach_deltas[0]["sided_with"] == "coach"
    assert coach_deltas[0]["coach_request_id"] == "req-disagree"
    assert coach_deltas[0]["coach_advice_age_s"] == pytest.approx(4.0, abs=0.05)

    # Player next buys local top1 -> 3031
    emitted.clear()
    now[0] = 225.0
    inv_local = inv_coach + [{"item_id": 3031, "count": 1, "price": 3400}]
    tracker.on_live_snapshot(_snap(60.0, 100.0, inv_local))
    now[0] = 228.0
    tracker.on_live_snapshot(_snap(63.0, 100.0, inv_local))

    local_deltas = [p for _, e, p, _ in emitted if e == "item_purchase_and_plan_override_delta"]
    assert len(local_deltas) == 1
    assert local_deltas[0]["item_id"] == 3031
    assert local_deltas[0]["matched_local_rank"] == 1
    assert local_deltas[0]["matched_local_top1"] is True
    assert local_deltas[0]["matched_coach"] is False
    assert local_deltas[0]["local_vs_coach_disagreed"] is True
    assert local_deltas[0]["sided_with"] == "local"


def test_reconcile_match_build_plan_with_trace_800g_override_and_tracker(tmp_path: Path) -> None:
    store = _make_recipe_store(tmp_path)

    # Player owns B.F. Sword (1038, 1300G >= 800G) toward Infinity Edge (3031),
    # while committed plan was locked on Runaan's (3085, 0G invested).
    new_lock, new_plan, trace = reconcile_match_build_plan_with_trace(
        store=store,
        my_items=("1038",),
        committed_plan=["3085", "3036"],
        locked_next_item="3085",
        proposed_ids=["3031", "3085", "3036"],
        allow_overhaul=False,
        hero_id="222",
    )
    assert new_lock == "3031"
    assert new_plan[0] == "3031"
    assert trace["overridden"] is True
    assert trace["override_reason"] == "component_investment_gte_800g"
    assert trace["committed_gold"] >= 800
    assert 1038 in trace["committed_component_ids"]
    assert trace["previous_route"] == "3085->3036"
    assert trace["new_route"].startswith("3031->")

    emitted: list[tuple[str, str, dict[str, Any], float | None]] = []
    tracker = MatchActionTracker(lambda d, e, p, gt: emitted.append((d, e, p, gt)))
    tracker.record_plan_override(240.0, trace)

    override_evs = [p for _, e, p, _ in emitted if e == "item_purchase_and_plan_override_delta"]
    assert len(override_evs) == 1
    assert override_evs[0]["event_kind"] == "plan_override"
    assert override_evs[0]["override_triggered"] is True
    assert override_evs[0]["override_reason"] == "component_investment_gte_800g"
    assert override_evs[0]["committed_gold"] >= 800


def test_match_action_tracker_death_shop_window_and_unlock_silent_miss() -> None:
    emitted: list[tuple[str, str, dict[str, Any], float | None]] = []
    now = [300.0]
    tracker = MatchActionTracker(
        lambda d, e, p, gt: emitted.append((d, e, p, gt)),
        clock=lambda: now[0],
        undo_window_s=2.0,
        unlock_miss_timeout_s=45.0,
    )
    enemy = _make_player("劫", "Zed", team="CHAOS", level=8)

    # 1) Death-shop window full lifecycle (alive -> dead -> coach timely -> buy -> respawn)
    me_alive_1 = _make_player("金克丝", "Jinx", is_dead=False, level=6, items=())
    tracker.on_live_snapshot(LiveMatchSnapshot(180.0, me_alive_1, (me_alive_1,), (enemy,), 3400.0))

    now[0] = 320.0
    me_dead_1 = _make_player("金克丝", "Jinx", is_dead=True, level=6, deaths=1, items=())
    snap_dead_1 = LiveMatchSnapshot(
        200.0,
        me_dead_1,
        (me_dead_1,),
        (enemy,),
        3500.0,
        raw_events=[
            {
                "event_id": 10,
                "event_name": "ChampionKill",
                "event_time": 199.5,
                "killer_hero": "Zed",
                "victim_hero": "Jinx",
                "victim_is_me": True,
                "killer_is_ally": False,
                "victim_is_ally": True,
            }
        ],
    )
    tracker.on_live_snapshot(snap_dead_1)
    tracker.note_death_coach_decision(True, "")

    # Coach arrives BEFORE purchase
    now[0] = 322.0
    tracker.record_coach_advice(
        202.0,
        request_id="req-death-1",
        trigger_reason="death_enemy_build_changed",
        recommended_item_ids=[3031],
        latency_ms=1450.0,
    )

    # Buy while dead
    now[0] = 326.0
    me_dead_bought = _make_player(
        "金克丝",
        "Jinx",
        is_dead=True,
        level=6,
        deaths=1,
        items=("3031",),
        item_details=[{"item_id": 3031, "count": 1, "price": 3400}],
    )
    tracker.on_live_snapshot(LiveMatchSnapshot(206.0, me_dead_bought, (me_dead_bought,), (enemy,), 100.0))

    # Respawn
    now[0] = 340.0
    me_respawn = _make_player(
        "金克丝",
        "Jinx",
        is_dead=False,
        level=6,
        deaths=1,
        items=("3031",),
        item_details=[{"item_id": 3031, "count": 1, "price": 3400}],
    )
    tracker.on_live_snapshot(LiveMatchSnapshot(220.0, me_respawn, (me_respawn,), (enemy,), 150.0))

    dws = [p for _, e, p, _ in emitted if e == "death_shop_window"]
    assert len(dws) == 1
    dw = dws[0]
    assert dw["killer_hero"] == "Zed"
    assert dw["gold_at_death"] == 3500.0
    assert dw["gold_at_respawn"] == 150.0
    assert dw["death_duration_s"] == 20.0
    assert dw["items_bought"] == [3031]
    assert dw["bought_count"] == 1
    assert dw["coach_triggered"] is True
    assert dw["coach_arrived"] is True
    assert dw["coach_arrived_before_buy"] is True
    assert dw["coach_request_id"] == "req-death-1"
    assert dw["closed_by"] == "respawn"

    # Second death where player buys BEFORE coach arrives -> coach_arrived_before_buy is False
    now[0] = 360.0
    me_dead_2 = _make_player(
        "金克丝",
        "Jinx",
        is_dead=True,
        level=7,
        deaths=2,
        items=("3031",),
        item_details=[{"item_id": 3031, "count": 1, "price": 3400}],
    )
    tracker.on_live_snapshot(LiveMatchSnapshot(240.0, me_dead_2, (me_dead_2,), (enemy,), 3100.0))
    tracker.note_death_coach_decision(True, "")

    now[0] = 362.0
    me_dead_2_bought = _make_player(
        "金克丝",
        "Jinx",
        is_dead=True,
        level=7,
        deaths=2,
        items=("3031", "3085"),
        item_details=[
            {"item_id": 3031, "count": 1, "price": 3400},
            {"item_id": 3085, "count": 1, "price": 2650},
        ],
    )
    tracker.on_live_snapshot(LiveMatchSnapshot(242.0, me_dead_2_bought, (me_dead_2_bought,), (enemy,), 450.0))

    # Coach arrives late (after purchase)
    now[0] = 366.0
    tracker.record_coach_advice(
        246.0,
        request_id="req-death-late",
        trigger_reason="death_enemy_build_changed",
        recommended_item_ids=[3036],
        latency_ms=4200.0,
    )
    now[0] = 375.0
    me_respawn_2 = _make_player(
        "金克丝",
        "Jinx",
        is_dead=False,
        level=7,
        deaths=2,
        items=("3031", "3085"),
        item_details=[
            {"item_id": 3031, "count": 1, "price": 3400},
            {"item_id": 3085, "count": 1, "price": 2650},
        ],
    )
    tracker.on_live_snapshot(LiveMatchSnapshot(255.0, me_respawn_2, (me_respawn_2,), (enemy,), 500.0))
    dws2 = [p for _, e, p, _ in emitted if e == "death_shop_window"]
    assert len(dws2) == 2
    assert dws2[1]["coach_triggered"] is True
    assert dws2[1]["coach_arrived"] is True
    assert dws2[1]["coach_arrived_before_buy"] is False

    # 2) Level 3/7/11/15 unlock silent miss detection after 45s without picker opening
    emitted.clear()
    tracker.reset()
    now[0] = 500.0
    me_lv3 = _make_player("金克丝", "Jinx", level=3)
    tracker.on_live_snapshot(
        LiveMatchSnapshot(30.0, me_lv3, (me_lv3,), (enemy,), 1400.0),
        selected_augment_count=0,
        is_picker_open=False,
    )
    assert any(
        e == "level_and_skill_milestone" and p["milestone_level"] == 3 for _, e, p, _ in emitted
    )

    # 30s elapsed (< 45s) -> no alert yet
    now[0] = 530.0
    tracker.on_live_snapshot(
        LiveMatchSnapshot(60.0, me_lv3, (me_lv3,), (enemy,), 1400.0),
        selected_augment_count=0,
        is_picker_open=False,
    )
    assert not any(e == EV_AUGMENT_UNLOCK_SILENT_MISS for _, e, _, _ in emitted)

    # 46s elapsed (>= 45s) with no picker open & 0 augments -> alert fires!
    now[0] = 546.0
    tracker.on_live_snapshot(
        LiveMatchSnapshot(76.0, me_lv3, (me_lv3,), (enemy,), 1400.0),
        selected_augment_count=0,
        is_picker_open=False,
    )
    misses = [p for _, e, p, _ in emitted if e == EV_AUGMENT_UNLOCK_SILENT_MISS]
    assert len(misses) == 1
    assert misses[0]["milestone_level"] == 3
    assert misses[0]["expected_augment_slots"] == 1
    assert misses[0]["selected_augment_count"] == 0

    # Level 7 milestone where picker DOES open within 45s -> NO silent miss for Lv 7
    now[0] = 620.0
    me_lv7 = _make_player("金克丝", "Jinx", level=7)
    tracker.on_live_snapshot(
        LiveMatchSnapshot(150.0, me_lv7, (me_lv7,), (enemy,), 1400.0),
        selected_augment_count=1,
        is_picker_open=False,
    )
    tracker.note_picker_opened(160.0)
    now[0] = 680.0
    tracker.on_live_snapshot(
        LiveMatchSnapshot(210.0, me_lv7, (me_lv7,), (enemy,), 1400.0),
        selected_augment_count=1,
        is_picker_open=False,
    )
    misses_after = [p for _, e, p, _ in emitted if e == EV_AUGMENT_UNLOCK_SILENT_MISS]
    assert len(misses_after) == 1  # Still only Lv 3


def test_pending_eog_race_waiting_for_stats_reconnect_and_fallback(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(edition_mod, "_COMPILED_EDITION", "dev")
    monkeypatch.setenv("HEX_EDITION", "dev")
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "app_data"))
    monkeypatch.setenv("HEX_DEVLAB_DIR", str(tmp_path / "devlab"))
    _patch_headless_controller(monkeypatch)

    # Pre-write config with bridge & auto coach disabled so test stays offline & port-free
    app_data = tmp_path / "app_data"
    app_data.mkdir(parents=True, exist_ok=True)
    (app_data / "config.json").write_text(
        json.dumps(
            {
                "bridge_enabled": False,
                "auto_trigger_coach": False,
                "lobby_scout_enabled": False,
                "telemetry_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    ctrl = HexHelperController(
        qapp,
        show_lookup_on_start=False,
        devlab_flag=True,
        devlab_roi_flag=False,
        devlab_debug_flag=False,
    )
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        if getattr(ctrl, "_devlab_badge_timer", None) is not None:
            ctrl._devlab_badge_timer.stop()
        ctrl.store = _make_recipe_store(tmp_path)
        ctrl.matcher.update_catalog(ctrl.store.all_augments())

        # Mock LCU client methods cleanly (read-only)
        fake_lcu = MagicMock()
        fake_lcu.get_gameflow_game_id.return_value = (555001, 2400)
        fake_lcu.get_current_rune_page.return_value = {
            "primary_style_id": 8000,
            "sub_style_id": 8200,
            "selected_perk_ids": [8008, 9111],
        }
        fake_lcu.get_eog_snapshot.return_value = {
            "won": 1,
            "duration_s": 625,
            "kda": "12/3/18",
            "item_ids": ["3031", "3085"],
            "eog_source": "eog_stats_block",
        }
        fake_lcu.get_match_history_eog_fallback.return_value = None
        ctrl.lcu = fake_lcu
        ctrl.live = MagicMock(consecutive_failures=0)

        me = _make_player("金克丝", "Jinx", level=14, kills=12, deaths=3, assists=18, items=("3031", "3085"))
        enemy = _make_player("亚托克斯", "Aatrox", team="CHAOS", level=14, items=("3084",))

        # --- Sub-case 1: WaitingForStats race -> buffered in PENDING_EOG -> EndOfGame captures won=1 ---
        ctrl._apply_live_snapshot(LiveMatchSnapshot(600.0, me, (me,), (enemy,), 1800.0, game_mode="KIWI"))
        assert ctrl.sm.state == GameState.IN_GAME
        match1_sid = ctrl._devlab.get_status_snapshot()["active_match_session_id"]
        assert match1_sid != ""

        # 2999 drops while LCU reports WaitingForStats: first 2 polls debounced
        ctrl.live.consecutive_failures = 1
        ctrl._apply_lcu_poll("WaitingForStats", None)
        assert ctrl.sm.state == GameState.IN_GAME
        assert ctrl._pending_eog_since is None

        ctrl.live.consecutive_failures = 2
        ctrl._apply_lcu_poll("WaitingForStats", None)
        assert ctrl.sm.state == GameState.IN_GAME
        assert ctrl._pending_eog_since is None

        # 3rd failure enters PENDING_EOG buffer — match MUST stay intact, NOT prematurely finalized!
        ctrl.live.consecutive_failures = 3
        ctrl._apply_lcu_poll("WaitingForStats", None)
        assert ctrl.sm.state == GameState.IN_GAME
        assert ctrl._pending_eog_since is not None
        assert ctrl._devlab.get_status_snapshot()["active_match_session_id"] == match1_sid

        # Still inside 18s window -> remains buffered
        ctrl._apply_lcu_poll("WaitingForStats", None)
        assert ctrl.sm.state == GameState.IN_GAME

        # Now LCU transitions to EndOfGame -> captures real won=1!
        ctrl._apply_lcu_poll("EndOfGame", None)
        assert ctrl.sm.state == GameState.IDLE
        assert ctrl._pending_eog_since is None
        assert ctrl._devlab.flush(timeout_s=3.0) is True

        # --- Sub-case 2: Transient 2999 disconnect -> PENDING_EOG -> seamless reconnect recovery ---
        fake_lcu.get_gameflow_game_id.return_value = (555002, 2400)
        ctrl.live.consecutive_failures = 0
        ctrl._apply_live_snapshot(LiveMatchSnapshot(300.0, me, (me,), (enemy,), 1200.0, game_mode="KIWI"))
        match2_sid = ctrl._devlab.get_status_snapshot()["active_match_session_id"]
        assert match2_sid and match2_sid != match1_sid

        for fails in (1, 2, 3):
            ctrl.live.consecutive_failures = fails
            ctrl._apply_lcu_poll("InProgress", None)
        assert ctrl._pending_eog_since is not None
        assert ctrl.sm.state == GameState.IN_GAME

        # Live 2999 reconnects!
        ctrl.live.consecutive_failures = 0
        ctrl._apply_live_snapshot(LiveMatchSnapshot(310.0, me, (me,), (enemy,), 1350.0, game_mode="KIWI"))
        assert ctrl._pending_eog_since is None
        assert ctrl.sm.state == GameState.IN_GAME
        assert ctrl._devlab.get_status_snapshot()["active_match_session_id"] == match2_sid

        # --- Sub-case 3: PENDING_EOG timeout (>18s) -> resolves via match_history_fallback ---
        fake_lcu.get_match_history_eog_fallback.return_value = {
            "won": 0,
            "duration_s": 710,
            "kda": "5/7/12",
            "item_ids": ["3031"],
            "eog_source": "match_history_fallback",
        }
        for fails in (1, 2, 3):
            ctrl.live.consecutive_failures = fails
            ctrl._apply_lcu_poll("WaitingForStats", None)
        assert ctrl._pending_eog_since is not None

        # Simulate > 18s elapsed in PENDING_EOG without mutating global clock
        ctrl._pending_eog_since -= ctrl.PENDING_EOG_TIMEOUT_S + 1.5
        ctrl._apply_lcu_poll("WaitingForStats", None)
        assert ctrl.sm.state == GameState.IDLE
        assert ctrl._pending_eog_since is None
        assert ctrl._devlab.flush(timeout_s=3.0) is True
    finally:
        ctrl.shutdown()
        qapp.processEvents()

    # Verify persisted DevLab matches on disk
    analyzer = DevLabAnalyzer(root_dir=tmp_path / "devlab")
    matches = analyzer.list_matches()
    assert len(matches) == 2

    m1 = analyzer.replay_match(matches[0]["match_dir"], write_outputs=False)
    assert m1.won == 1
    assert m1.eog_source == "eog_stats_block"
    assert m1.duration_s >= 625

    m2 = analyzer.replay_match(matches[1]["match_dir"], write_outputs=False)
    assert m2.won == 0
    assert m2.eog_source == "match_history_fallback"
    assert m2.kpis["runtime"]["live_disconnect_recoveries"] >= 1


# ---------------------------------------------------------------------------
# 3. Async Writer Performance, ROI Quotas, FIFO Eviction & Zero LogRing Pollution
# ---------------------------------------------------------------------------


def test_recorder_async_perf_roi_quotas_fifo_and_zero_log_ring_pollution(tmp_path: Path) -> None:
    ring = MatchLogRing(tmp_path / "ring_logs", max_matches=5)
    ring_handler = MatchRingLogHandler(ring, level=logging.INFO)
    ring_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(ring_handler)

    devlab_dir = tmp_path / "devlab"
    recorder = DevLabFlightRecorder(
        root_dir=devlab_dir,
        enable_roi_snapshots=True,
        enable_debug_log=True,
        max_matches=4,
        max_roi_per_match=15,
        event_queue_max=4096,
        app_version="1.4.7+perf001",
    )
    try:
        ring_entry = ring.begin_match("ring_mk_1", hero_id="222", hero_name="金克丝")
        recorder.begin_match(match_key_hash=hash_match_key("perf_m1"), hero_id="222", hero_name="金克丝")

        # Benchmark 2,500 main-thread emit() calls
        latencies_ns: list[int] = []
        for idx in range(2500):
            t0 = time.perf_counter_ns()
            recorder.emit(
                DOMAIN_GAME,
                "perf_probe",
                {"i": idx, "tag": "bench", "val": idx * 0.25},
                game_time_s=float(idx) * 0.1,
            )
            latencies_ns.append(time.perf_counter_ns() - t0)

        latencies_ns.sort()
        p50_us = latencies_ns[int(len(latencies_ns) * 0.50)] / 1000.0
        p99_us = latencies_ns[int(len(latencies_ns) * 0.99)] / 1000.0
        print(f"[DevLab emit bench] n=2500 p50={p50_us:.2f}us p99={p99_us:.2f}us")
        assert p99_us < 200.0, f"emit() P99 too slow: p50={p50_us:.2f}us, p99={p99_us:.2f}us"

        # Oversized 1920x1080 full-screen frame MUST be rejected immediately
        oversized = np.zeros((1080, 1920, 3), dtype=np.uint8)
        assert recorder.enqueue_roi_snapshot("fullscreen_forbidden", oversized, ev_seq=1) is None

        # Enqueue 25 small 48x180x3 synthetic BGR crops -> per-match cap strictly <= 15
        accepted_paths: list[str] = []
        for idx in range(25):
            crop = np.full((48, 180, 3), fill_value=(idx * 9) % 255, dtype=np.uint8)
            crop[:, ::4, 1] = 200
            rel = recorder.enqueue_roi_snapshot(f"slot_anomaly_{idx}", crop, ev_seq=idx + 10)
            if rel is not None:
                accepted_paths.append(rel)
            if idx == 7:
                recorder.flush(timeout_s=2.0)

        assert len(accepted_paths) == 15

        # Emit DEBUG log on hex_helper logger -> MUST land in debug_runtime.log, NOT MatchLogRing
        probe_logger = logging.getLogger("hex_helper.devlab_isolation_probe")
        probe_logger.debug("DEVLAB_ONLY_DEBUG_MARKER_778899")
        assert recorder.flush(timeout_s=4.0) is True

        m1_dir = devlab_dir / "matches" / recorder.get_status_snapshot()["active_match_dir"]
        roi_files = list((m1_dir / "roi_snapshots").glob("*.png"))
        assert 1 <= len(roi_files) <= 15
        assert not any("fullscreen_forbidden" in f.name for f in roi_files)
        for rf in roi_files:
            assert rf.stat().st_size <= 64 * 1024

        dbg_content = (m1_dir / "debug_runtime.log").read_text(encoding="utf-8")
        assert "DEVLAB_ONLY_DEBUG_MARKER_778899" in dbg_content

        # Verify MatchLogRing received ZERO DevLab events or DEBUG lines
        ring_match_text = (tmp_path / "ring_logs" / ring_entry.file_name).read_text(encoding="utf-8")
        preamble_path = tmp_path / "ring_logs" / "session_preamble.log"
        preamble_text = preamble_path.read_text(encoding="utf-8") if preamble_path.is_file() else ""
        combined_ring = ring_match_text + "\n" + preamble_text
        assert "DEVLAB_ONLY_DEBUG_MARKER_778899" not in combined_ring
        assert "perf_probe" not in combined_ring

        # Record 5 more matches (total 6) with max_matches=4 -> FIFO evicts oldest 2 matches
        recorder.end_match(won=1, duration_s=300, eog_source="eog_stats_block")
        for seq_num in range(2, 7):
            recorder.begin_match(
                match_key_hash=hash_match_key(f"fifo_m{seq_num}"),
                hero_id=str(100 + seq_num),
                hero_name=f"Hero{seq_num}",
            )
            recorder.emit(DOMAIN_GAME, "fifo_marker", {"n": seq_num}, game_time_s=10.0)
            recorder.end_match(won=seq_num % 2, duration_s=240, eog_source="eog_stats_block")

        assert recorder.flush(timeout_s=4.0) is True
    finally:
        recorder.flush_and_close()
        root_logger.removeHandler(ring_handler)

    surviving_dirs = sorted(p.name for p in (devlab_dir / "matches").iterdir() if p.is_dir())
    assert len(surviving_dirs) == 4
    index_data = json.loads((devlab_dir / "index.json").read_text(encoding="utf-8"))
    assert [m["seq"] for m in index_data["matches"]] == [3, 4, 5, 6]


# ---------------------------------------------------------------------------
# 4. Offline Replay Analyzer, All 10 Issue Detectors & CLI
# ---------------------------------------------------------------------------


def _seed_synthetic_devlab_dataset(devlab_dir: Path) -> None:
    """Populate realistic recorded matches exercising all 10 detectors, non-Mayhem isolation, and A/B versions."""
    # Version A (1.4.7-old): faulty ARAM Mayhem match triggering all 10 detectors
    rec_a = DevLabFlightRecorder(
        root_dir=devlab_dir,
        enable_roi_snapshots=True,
        enable_debug_log=False,
        app_version="1.4.7-old",
    )
    try:
        rec_a.begin_match(
            match_key_hash=hash_match_key("det_match_1"),
            hero_id="222",
            hero_name="金克丝",
            dataset_tag="16.23/normal",
            game_mode="KIWI",
            is_non_mayhem=False,
        )
        # Save one small anomaly ROI crop to verify roi_index & evidence_roi_paths
        tiny_crop = np.full((32, 120, 3), 90, dtype=np.uint8)
        roi_rel = rec_a.enqueue_roi_snapshot("nearmiss_slot0", tiny_crop, ev_seq=1)

        # DET_OCR_NEARMISS_OR_PASS2_SHADOWED (near-miss + pass2 shadowed + 2-char substring)
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_OCR_ANOMALY,
            {
                "ocr_language_tag": "zh-Hans-CN",
                "resolution": "2560x1440",
                "dpr": 1.5,
                "roi_path": roi_rel or "",
                "near_misses": [
                    {
                        "raw_text": "珠光护手改",
                        "near_miss_top1_id": "1001",
                        "near_miss_top1_score": 0.51,
                        "slot": 0,
                    }
                ],
                "shadowed_pass2": [
                    {
                        "kept_pass1_text": "珠",
                        "dropped_pass2_text": "珠光护手",
                        "dx": 3,
                        "dy": 2,
                    }
                ],
                "slots": [
                    {
                        "slot": 1,
                        "augment_id": "1099",
                        "augment_name": "闪现",
                        "raw_text": "闪现",
                        "substring_hit": True,
                        "score": 0.64,
                        "margin": 0.04,
                    }
                ],
            },
            game_time_s=45.0,
        )

        # DET_OCR_SILENT_MISS_AT_UNLOCK_LEVEL (explicit Lv3 + derived Lv7)
        rec_a.emit(
            DOMAIN_GAME,
            EV_LEVEL_MILESTONE,
            {"level": 3, "milestone_level": 3, "expected_augment_slots": 1, "selected_augment_count": 0},
            game_time_s=30.0,
        )
        rec_a.emit(
            DOMAIN_VISION,
            EV_AUGMENT_UNLOCK_SILENT_MISS,
            {
                "milestone_level": 3,
                "current_level": 3,
                "expected_augment_slots": 1,
                "selected_augment_count": 0,
                "elapsed_since_unlock_s": 46.2,
            },
            game_time_s=76.2,
        )
        rec_a.emit(
            DOMAIN_GAME,
            EV_LEVEL_MILESTONE,
            {"level": 7, "milestone_level": 7, "expected_augment_slots": 2, "selected_augment_count": 1},
            game_time_s=220.0,
        )

        # DET_REROLL_DESYNC_OR_Y_THRESHOLD_CLIP
        rec_a.emit(
            DOMAIN_REC,
            EV_AUGMENT_REROLL_DELTA,
            {"slot": 0, "old_augment_id": "9999", "new_augment_id": "1002", "was_top1": True, "hex_gain": -0.4},
            game_time_s=110.0,
        )
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_CLICK_RESOLVED,
            {
                "chosen_slot": None,
                "chosen_source": "none",
                "cursor_rel_x": 0.50,
                "cursor_rel_y": 0.79,
                "in_reroll_zone": True,
                "reroll_y_threshold_rel": 0.77,
                "ms_since_last_reroll": 420.0,
            },
            game_time_s=110.4,
        )
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_USER_CORRECTION,
            {
                "action": "replace",
                "old_augment_id": "9999",
                "new_augment_id": "1002",
                "ms_since_last_reroll": 420.0,
            },
            game_time_s=112.0,
        )

        # DET_EMPTY_ICON_BANK_OR_HUD_MISALIGN
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_ICON_VERIFY,
            {"template_count": 0, "outcome": "skipped_empty_bank", "resolution": "2560x1440", "dpr": 1.5},
            game_time_s=115.0,
        )
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_ICON_VERIFY,
            {
                "template_count": 120,
                "outcome": "flat_patches",
                "slot_patch_stds": [2.0, 1.5, 3.1, 2.8],
                "resolution": "2560x1440",
                "dpr": 1.5,
            },
            game_time_s=120.0,
        )

        # DET_TAB_PASSIVE_HOVER_POLLUTION
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_TRANSMUTE_TRACE,
            {"channel": "passive_hud_hover", "cursor_zone": "tab_zone", "matched_ids": ["1048"]},
            game_time_s=140.0,
        )
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_USER_CORRECTION,
            {"action": "remove", "old_augment_id": "1048", "cursor_zone": "tab_zone"},
            game_time_s=143.0,
        )

        # DET_SCORING_TRAP_OR_PROFILE_CONFLICT (5x overrated trap aug + profile conflict + >=800G overrides)
        for rnd in range(5):
            rec_a.emit(
                DOMAIN_REC,
                EV_AUGMENT_OFFER_REC,
                {"top1_augment_id": "9999", "cards": ["9999", "1002", "1003"]},
                game_time_s=150.0 + rnd * 20.0,
            )
            if rnd < 3:
                rec_a.emit(
                    DOMAIN_REC,
                    EV_AUGMENT_REROLL_DELTA,
                    {"slot": 0, "old_augment_id": "9999", "new_augment_id": "1005", "was_top1": True},
                    game_time_s=152.0 + rnd * 20.0,
                )
            rec_a.emit(
                DOMAIN_REC,
                EV_AUGMENT_PICK_DELTA,
                {
                    "picked_augment_id": "1002",
                    "recommended_augment_id": "9999",
                    "matched_top1": False,
                    "hex_score_regret": 1.2,
                },
                game_time_s=155.0 + rnd * 20.0,
            )
        rec_a.emit(
            DOMAIN_REC,
            EV_LOCAL_BUILD_REC,
            {
                "top_item_ids": [3085, 3031, 3036],
                "hero_profile": "AD_CRIT",
                "combo_hero_profile": "AP_SPELL",
                "profile_conflict": True,
            },
            game_time_s=260.0,
        )
        for ov_idx in range(2):
            rec_a.emit(
                DOMAIN_REC,
                EV_ITEM_PURCHASE_DELTA,
                {
                    "event_kind": "plan_override",
                    "override_triggered": True,
                    "override_reason": "component_investment_gte_800g",
                    "committed_gold": 1300,
                    "previous_route": "3085->3036",
                    "new_route": f"3031->3036_{ov_idx}",
                },
                game_time_s=270.0 + ov_idx * 10.0,
            )

        # DET_COACH_TIMING_AND_HALLUCINATION
        rec_a.emit(
            DOMAIN_COACH,
            EV_COACH_TURN,
            {
                "request_id": "req-late-1",
                "trigger_reason": "augment_picking",
                "model": "gemini-4-hydrogen",
                "latency_ms": 4800.0,
                "total_tokens": 2400,
                "arrived_before_pick": False,
                "grounding_stats": {
                    "unknown_item_blocked": 2,
                    "already_owned_blocked": 1,
                    "full_build_7th_item_blocked": 0,
                    "invalid_route_blocked": 0,
                    "total_blocked": 3,
                },
            },
            game_time_s=310.0,
        )
        rec_a.emit(
            DOMAIN_COACH,
            EV_DEATH_SHOP_WINDOW,
            {
                "death_start_time": 320.0,
                "death_duration_s": 18.0,
                "killer_hero": "Zed",
                "gold_at_death": 3400.0,
                "gold_at_respawn": 100.0,
                "items_bought": [3031],
                "bought_count": 1,
                "coach_triggered": True,
                "coach_arrived": True,
                "coach_arrived_before_buy": False,
            },
            game_time_s=338.0,
        )
        rec_a.emit(
            DOMAIN_COACH,
            EV_SMART_PICKER_GATE,
            {"skipped": True, "top1_top2_gap": 0.021},
            game_time_s=350.0,
        )
        rec_a.emit(
            DOMAIN_UI,
            EV_UI_MANUAL_TRIGGER,
            {"trigger": "manual_f6", "prev_gate_skipped": True, "ms_since_gate_skip": 3500.0},
            game_time_s=353.5,
        )

        # DET_OVERLAY_IRRITATION_AND_OVERFLOW
        for k in range(3):
            rec_a.emit(
                DOMAIN_UI,
                EV_UI_OVERLAY_SESSION,
                {
                    "overlay": "edge_drawer",
                    "open_source": "auto_death_peek",
                    "close_source": "user_close",
                    "dwell_ms": 800,
                    "rapid_dismiss": True,
                },
                game_time_s=360.0 + k * 5.0,
                game_state="IN_GAME",
            )
            rec_a.emit(
                DOMAIN_UI,
                EV_UI_LAYOUT_ADJUSTED,
                {
                    "overlay": "edge_drawer",
                    "action": "drag",
                    "detached_edge": True,
                    "resolution": "2560x1440",
                    "dpr": 1.5,
                },
                game_time_s=361.0 + k * 5.0,
                game_state="IN_GAME",
            )
        rec_a.emit(
            DOMAIN_UI,
            EV_UI_OVERLAY_SESSION,
            {
                "overlay": "champ_select",
                "open_source": "auto_champ_select",
                "close_source": "user_close",
                "dwell_ms": 500,
                "ghost_repopup": True,
                "ms_since_last_user_close": 420,
            },
            game_time_s=380.0,
            game_state="CHAMP_SELECT",
        )
        rec_a.emit(
            DOMAIN_UI,
            EV_UI_SCROLL_OVERFLOW,
            {"scroll_area": "edge_drawer.panel", "max_overflow_px": 140, "max_scroll_px": 140},
            game_time_s=385.0,
            game_state="IN_GAME",
        )
        rec_a.emit(
            DOMAIN_UI,
            EV_UI_SCROLL_OVERFLOW,
            {"scroll_area": "combo_overlay.panel", "max_overflow_px": 96, "max_scroll_px": 96},
            game_time_s=390.0,
            game_state="IN_GAME",
        )

        # DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION
        rec_a.emit(
            DOMAIN_PERF,
            EV_MAIN_THREAD_STALL,
            {
                "window_s": 60.0,
                "total_ge_50ms_count": 14,
                "ops": {
                    "tick_vision": {"count": 120, "p50_ms": 24.0, "p95_ms": 135.0, "max_ms": 182.0, "ge_50ms_count": 14},
                    "click_timer_gap": {"count": 60, "p50_ms": 31.0, "p95_ms": 96.0, "max_ms": 128.0, "ge_50ms_count": 4},
                },
            },
            game_time_s=400.0,
            game_state="IN_GAME",
        )
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_LATENCY_SPIKE,
            {"picker_open": False, "ocr_p1_ms": 32.0, "ocr_p2_ms": 48.0, "in_game_idle_dual_pass": True},
            game_time_s=405.0,
            game_state="IN_GAME",
        )
        rec_a.emit(
            DOMAIN_VISION,
            EV_VISION_LATENCY_SPIKE,
            {"click_timer_max_gap_ms": 118.0, "in_augment_picking": True},
            game_time_s=410.0,
            game_state="AUGMENT_PICKING",
        )

        # DET_EOG_RACE_LOST: duration >= 180s with won=-1 and eog_source="timeout"
        rec_a.emit(
            DOMAIN_GAME,
            EV_MATCH_EOG,
            {"won": -1, "duration_s": 620, "eog_source": "timeout"},
            game_time_s=620.0,
            priority="high",
        )
        rec_a.end_match(won=-1, duration_s=620, eog_source="timeout")

        # Non-Mayhem practice match (must be isolated from main ARAM Mayhem winrate stats)
        rec_a.begin_match(
            match_key_hash=hash_match_key("non_mayhem_1"),
            hero_id="222",
            hero_name="金克丝",
            dataset_tag="16.23/normal",
            game_mode="PRACTICETOOL",
            is_non_mayhem=True,
        )
        rec_a.emit(DOMAIN_GAME, EV_MATCH_EOG, {"won": 0, "duration_s": 240, "eog_source": "eog_stats_block"}, game_time_s=240.0)
        rec_a.end_match(won=0, duration_s=240, eog_source="eog_stats_block")
        assert rec_a.flush(timeout_s=3.0) is True
    finally:
        rec_a.flush_and_close()

    # Version B (1.4.8-new): clean healthy ARAM Mayhem win for A/B compare_versions
    rec_b = DevLabFlightRecorder(
        root_dir=devlab_dir,
        enable_roi_snapshots=False,
        enable_debug_log=False,
        app_version="1.4.8-new",
    )
    try:
        rec_b.begin_match(
            match_key_hash=hash_match_key("clean_match_v2"),
            hero_id="222",
            hero_name="金克丝",
            dataset_tag="16.23/normal",
            game_mode="KIWI",
            is_non_mayhem=False,
        )
        rec_b.emit(DOMAIN_GAME, EV_LEVEL_MILESTONE, {"level": 3, "milestone_level": 3, "expected_augment_slots": 1, "selected_augment_count": 0}, game_time_s=30.0)
        rec_b.emit(DOMAIN_VISION, EV_VISION_PICKER_TRANSITION, {"transition": "open", "chosen_layout": "3card", "card_count": 3}, game_time_s=32.0)
        rec_b.emit(DOMAIN_REC, EV_AUGMENT_OFFER_REC, {"top1_augment_id": "1001", "cards": ["1001", "1002", "1003"]}, game_time_s=32.5)
        rec_b.emit(DOMAIN_REC, EV_AUGMENT_PICK_DELTA, {"picked_augment_id": "1001", "recommended_augment_id": "1001", "matched_top1": True, "hex_score_regret": 0.0, "ai_advice_arrived_before_pick": True, "coach_request_id": "req-clean-1"}, game_time_s=35.0)
        rec_b.emit(DOMAIN_COACH, EV_COACH_TURN, {"request_id": "req-clean-1", "trigger_reason": "augment_picking", "latency_ms": 950.0, "total_tokens": 1100, "arrived_before_pick": True, "grounding_stats": {"total_blocked": 0}}, game_time_s=34.0)
        rec_b.emit(DOMAIN_PERF, EV_MAIN_THREAD_STALL, {"window_s": 60.0, "total_ge_50ms_count": 0, "ops": {"tick_vision": {"count": 120, "p50_ms": 11.0, "p95_ms": 22.0, "max_ms": 34.0, "ge_50ms_count": 0}}}, game_time_s=300.0)
        rec_b.emit(DOMAIN_GAME, EV_MATCH_EOG, {"won": 1, "duration_s": 720, "eog_source": "eog_stats_block"}, game_time_s=720.0, priority="high")
        rec_b.end_match(won=1, duration_s=720, eog_source="eog_stats_block")
        assert rec_b.flush(timeout_s=3.0) is True
    finally:
        rec_b.flush_and_close()


def test_offline_analyzer_all_10_detectors_compare_doctor_and_cli(tmp_path: Path) -> None:
    devlab_dir = tmp_path / "devlab"
    _seed_synthetic_devlab_dataset(devlab_dir)

    analyzer = DevLabAnalyzer(root_dir=devlab_dir)
    matches = analyzer.list_matches()
    assert len(matches) == 3

    # 1) Single-match replay on match #1 (all 10 detectors fire!)
    m1_replay = analyzer.replay_match(matches[0]["match_dir"], write_outputs=True)
    assert m1_replay.timeline_path and Path(m1_replay.timeline_path).is_file()
    assert Path(m1_replay.timeline_path).read_text(encoding="utf-8").strip() != ""
    assert m1_replay.summary_path and Path(m1_replay.summary_path).is_file()

    # 2) Multi-match evolution report
    report = analyzer.generate_evolution_report(last_n=20)
    assert report.total_matches == 3
    assert report.mayhem_matches == 2
    assert report.non_mayhem_matches == 1
    # Non-Mayhem loss (won=0) MUST NOT pollute Mayhem winrate (only clean Mayhem match won=1 has known outcome)
    assert report.pillars["recommendation"]["mayhem_winrate"] == pytest.approx(1.0)

    fired_rules = {iss.detector_rule for iss in report.issues}
    missing_rules = set(ALL_10_DETECTORS) - fired_rules
    assert not missing_rules, f"Missing detector(s): {sorted(missing_rules)}"

    for iss in report.issues:
        assert iss.target_files_and_symbols, f"{iss.detector_rule} has empty target_files_and_symbols"
        assert iss.severity in ("P0", "P1", "P2")
        assert iss.sample_count >= 1
        assert iss.evidence_match_seqs

    assert Path(report.report_path).is_file()
    assert Path(report.report_path).read_text(encoding="utf-8").strip() != ""
    assert Path(report.backlog_path).is_file()
    backlog_obj = json.loads(Path(report.backlog_path).read_text(encoding="utf-8"))
    assert len(backlog_obj["issues"]) == 10
    assert Path(report.latest_report_path).is_file()
    assert Path(report.latest_backlog_path).is_file()

    # 3) Version A/B comparison
    diff = analyzer.compare_versions("1.4.7-old", "1.4.8-new")
    assert diff.matches_a == 2 and diff.mayhem_matches_a == 1
    assert diff.matches_b == 1 and diff.mayhem_matches_b == 1
    assert len(diff.issues_a) == 10
    assert len(diff.issues_b) == 0
    assert Path(diff.diff_path).is_file()
    assert Path(diff.diff_path).read_text(encoding="utf-8").strip() != ""

    # 4) Doctor health check
    diag = analyzer.doctor()
    assert diag["checks"]["pii_scan"]["status"] == "pass"
    assert diag["checks"]["directory"]["status"] == "pass"

    # 5) Subprocess CLI checks for all 5 subcommands
    cli_out = tmp_path / "cli_reports"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "hex_helper.devlab", *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )

    p_list = _run_cli("list", "--dir", str(devlab_dir), "--json")
    assert p_list.returncode == 0, p_list.stderr
    listed = json.loads(p_list.stdout)
    assert len(listed) == 3

    p_replay = _run_cli("replay", "latest", "--dir", str(devlab_dir), "--out", str(cli_out))
    assert p_replay.returncode == 0, p_replay.stderr
    assert any(cli_out.glob("match_*_timeline.md"))

    p_report = _run_cli("report", "--last", "20", "--dir", str(devlab_dir), "--out", str(cli_out), "--json")
    assert p_report.returncode == 0, p_report.stderr
    cli_backlog = json.loads(p_report.stdout)
    assert {i["detector_rule"] for i in cli_backlog["issues"]} == set(ALL_10_DETECTORS)
    assert any(cli_out.glob("evolution_report_*.md"))
    assert any(cli_out.glob("evolution_backlog_*.json"))

    p_compare = _run_cli("compare", "1.4.7-old", "1.4.8-new", "--dir", str(devlab_dir), "--out", str(cli_out))
    assert p_compare.returncode == 0, p_compare.stderr
    assert any(cli_out.glob("version_diff_*.md"))

    p_doctor = _run_cli("doctor", "--dir", str(devlab_dir), "--json")
    assert p_doctor.returncode == 0, p_doctor.stderr
    cli_diag = json.loads(p_doctor.stdout)
    assert cli_diag["checks"]["pii_scan"]["status"] == "pass"


# ---------------------------------------------------------------------------
# 5. Commercial Hard-Isolation, Spec & Release Gate Verification
# ---------------------------------------------------------------------------


def test_commercial_hard_isolation_edition_config_and_cli_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edition_mod, "_COMPILED_EDITION", "commercial")
    monkeypatch.setenv("HEX_EDITION", "dev")
    monkeypatch.setenv("HEX_DEVLAB", "1")
    monkeypatch.setenv("HEX_DEVLAB_ROI", "1")
    monkeypatch.setenv("HEX_DEVLAB_DEBUG", "1")

    assert edition_mod.is_commercial_edition() is True
    assert edition_mod.is_devlab_allowed() is False

    cfg = AppConfig.from_dict(
        {
            "devlab_enabled": True,
            "devlab_roi_snapshots": True,
            "devlab_debug_log": True,
            "bridge_enabled": True,
            "gemini_api_key": "AIzaSyFakeKeyForCommercialTest123456",
        }
    )
    assert cfg.devlab_enabled is False
    assert cfg.devlab_roi_snapshots is False
    assert cfg.devlab_debug_log is False
    assert cfg.bridge_enabled is False
    assert cfg.gemini_api_key == ""

    d = cfg.to_dict()
    assert d["devlab_enabled"] is False
    assert d["devlab_roi_snapshots"] is False
    assert d["devlab_debug_log"] is False

    # CLI flags in commercial edition must resolve unconditionally to False
    parsed = main_mod.build_arg_parser().parse_args(
        ["--devlab", "--devlab-roi-snapshots", "--devlab-debug"]
    )
    resolved = main_mod.resolve_devlab_flags(parsed)
    assert resolved == {
        "devlab": False,
        "devlab_roi_snapshots": False,
        "devlab_debug": False,
    }


def test_pyinstaller_spec_static_imports_and_release_bundle_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1) Parse packaging/hex_helper.spec to verify explicit excludes for devlab & agent_bridge
    spec_path = REPO_ROOT / "packaging" / "hex_helper.spec"
    spec_text = spec_path.read_text(encoding="utf-8")
    tree = ast.parse(spec_text, filename=str(spec_path))

    excludes_values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "excludes" and isinstance(node.value, ast.List):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            excludes_values.add(elt.value)

    assert "hex_helper.devlab" in excludes_values
    assert "hex_helper.bridge.agent_bridge" in excludes_values

    # 2) Verify ZERO static top-level or TYPE_CHECKING imports of hex_helper.devlab in non-devlab modules
    src_pkg = REPO_ROOT / "src" / "hex_helper"
    for py_file in src_pkg.rglob("*.py"):
        if "devlab" in py_file.relative_to(src_pkg).parts:
            continue
        mod_ast = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(mod_ast):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("hex_helper.devlab"), f"Static devlab import in {py_file}"
            elif isinstance(node, ast.ImportFrom):
                mod_name = node.module or ""
                assert not mod_name.startswith("hex_helper.devlab"), f"Static devlab import in {py_file}"

    # 3) Test scripts/build_windows_release.py::audit_commercial_bundle on dirty vs clean mock bundles
    builder_path = REPO_ROOT / "scripts" / "build_windows_release.py"
    spec = importlib.util.spec_from_file_location("hex_test_build_windows_release", builder_path)
    assert spec is not None and spec.loader is not None
    builder_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder_mod)

    # Install lightweight mock PyInstaller.archive.readers so audit_commercial_bundle inspects synthetic PYZ TOC
    pyz_modules_by_exe: dict[str, list[str]] = {}

    class _FakePyzReader:
        def __init__(self, mods: list[str]) -> None:
            self.toc = {m: (0, 0, 0) for m in mods}

    class _FakeCArchiveReader:
        def __init__(self, exe_str: str) -> None:
            self._exe = str(Path(exe_str).resolve())
            self.toc = {"PYZ-00.pyz": (0, 0, 0, 0, "z")}

        def open_embedded_archive(self, _name: str) -> _FakePyzReader:
            return _FakePyzReader(pyz_modules_by_exe[self._exe])

    fake_readers_mod = type(sys)("PyInstaller.archive.readers")
    fake_readers_mod.CArchiveReader = _FakeCArchiveReader  # type: ignore[attr-defined]
    fake_readers_mod.PKG_ITEM_PYZ = "z"  # type: ignore[attr-defined]
    fake_archive_mod = type(sys)("PyInstaller.archive")
    fake_archive_mod.readers = fake_readers_mod  # type: ignore[attr-defined]
    fake_pyi_mod = type(sys)("PyInstaller")
    fake_pyi_mod.archive = fake_archive_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "PyInstaller", fake_pyi_mod)
    monkeypatch.setitem(sys.modules, "PyInstaller.archive", fake_archive_mod)
    monkeypatch.setitem(sys.modules, "PyInstaller.archive.readers", fake_readers_mod)

    # Clean mock bundle
    clean_bundle = tmp_path / "clean_bundle"
    (clean_bundle / "_internal" / "hex_helper").mkdir(parents=True)
    (clean_bundle / "_internal" / "hex_helper" / "app.pyc").write_bytes(b"clean")
    clean_exe = clean_bundle / "HexHelper.exe"
    clean_exe.write_bytes(b"MZ")
    pyz_modules_by_exe[str(clean_exe.resolve())] = [
        "hex_helper.app",
        "hex_helper.edition",
        "hex_helper.bridge.protocol",
    ]

    clean_report = builder_mod.audit_commercial_bundle(clean_bundle)
    assert clean_report["devlab_clean"] is True
    assert clean_report["google_genai_clean"] is True

    # Dirty mock bundle containing fake hex_helper/devlab/recorder.pyc
    dirty_bundle = tmp_path / "dirty_bundle"
    (dirty_bundle / "_internal" / "hex_helper" / "devlab").mkdir(parents=True)
    (dirty_bundle / "_internal" / "hex_helper" / "devlab" / "recorder.pyc").write_bytes(b"leaked")
    dirty_exe = dirty_bundle / "HexHelper.exe"
    dirty_exe.write_bytes(b"MZ")
    pyz_modules_by_exe[str(dirty_exe.resolve())] = [
        "hex_helper.app",
        "hex_helper.edition",
    ]

    with pytest.raises(RuntimeError) as exc_info:
        builder_mod.audit_commercial_bundle(dirty_bundle)
    assert "devlab_clean=False" in str(exc_info.value)
    assert "hex_helper/devlab/recorder.pyc" in str(exc_info.value)

    # Also verify PYZ-only leak detection (even with clean disk files)
    pyz_modules_by_exe[str(clean_exe.resolve())] = [
        "hex_helper.app",
        "hex_helper.devlab.recorder",
    ]
    with pytest.raises(RuntimeError) as pyz_exc:
        builder_mod.audit_commercial_bundle(clean_bundle)
    assert "devlab_clean=False" in str(pyz_exc.value)
    assert "hex_helper.devlab.recorder" in str(pyz_exc.value)


def test_hextech_devlab_badge_palette_and_commercial_lookup_window_isolation(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1) Verify HextechDevLabBadge uses LOL V16.23 Hextech palette and ZERO Web orange tokens
    badge = HextechDevLabBadge()
    try:
        idle_qss = badge.styleSheet().upper()
        assert "#FF5A1F" not in idle_qss
        assert "#101113" not in idle_qss
        assert "#040E1A" in idle_qss
        assert "#091428" in idle_qss
        assert "#C8AA6E" in idle_qss
        assert "#785A28" in idle_qss
        assert badge.is_recording is False
        assert badge.indicator_color.upper() == "#785A28"

        badge.update_status(
            {
                "recording_match": True,
                "active_match_seq": 7,
                "event_count": 42,
                "queue_depth": 0,
                "max_queue_depth": 4,
                "dropped_events": 0,
                "dropped_roi": 0,
                "roi_count": 1,
                "roi_cap": 15,
                "roi_disk_bytes": 12000,
                "disk_bytes": 240000,
                "match_count": 3,
                "max_matches": 120,
            }
        )
        active_qss = badge.styleSheet().upper()
        assert "#FF5A1F" not in active_qss
        assert "#101113" not in active_qss
        assert "#040E1A" in active_qss
        assert "#C8AA6E" in active_qss
        assert "#0AC8B9" in active_qss
        assert badge.is_recording is True
        assert badge.indicator_color.upper() == "#0AC8B9"
        assert badge.text() == "◆ REC #0007 · 42 evt"
    finally:
        badge.close()

    # 2) Dev mode LookupWindow with devlab_active=True instantiates HextechDevLabBadge
    monkeypatch.setattr(edition_mod, "_COMPILED_EDITION", "dev")
    monkeypatch.setenv("HEX_EDITION", "dev")
    win_dev = LookupWindow(AppConfig(), devlab_active=True)
    try:
        assert isinstance(win_dev.devlab_badge, HextechDevLabBadge)
    finally:
        win_dev.close()

    # 3) Commercial mode LookupWindow NEVER instantiates HextechDevLabBadge even if devlab_active=True + HEX_EDITION=dev
    monkeypatch.setattr(edition_mod, "_COMPILED_EDITION", "commercial")
    monkeypatch.setenv("HEX_EDITION", "dev")
    monkeypatch.setenv("HEX_DEVLAB", "1")
    win_comm = LookupWindow(AppConfig(), devlab_active=True)
    try:
        assert win_comm.devlab_badge is None
    finally:
        win_comm.close()
        qapp.processEvents()

    # 4) Commercial HexHelperController mounts _NullFlightRecorder, creates ZERO devlab/ dir, and has no badge
    devlab_dir = tmp_path / "commercial_devlab_forbidden"
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "comm_appdata"))
    monkeypatch.setenv("HEX_DEVLAB_DIR", str(devlab_dir))
    monkeypatch.setenv("HEX_DEVLAB_ROI", "1")
    monkeypatch.setenv("HEX_DEVLAB_DEBUG", "1")
    _patch_headless_controller(monkeypatch)

    ctrl = HexHelperController(
        qapp,
        show_lookup_on_start=False,
        devlab_flag=True,
        devlab_roi_flag=True,
        devlab_debug_flag=True,
    )
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        if ctrl._wallet_heartbeat_timer is not None:
            ctrl._wallet_heartbeat_timer.stop()
        if ctrl._startup_update_timer is not None:
            ctrl._startup_update_timer.stop()
        assert ctrl._devlab_active is False
        assert ctrl.lookup_window.devlab_badge is None
        assert not devlab_dir.exists()
        assert not (tmp_path / "comm_appdata" / "devlab").exists()
    finally:
        ctrl.shutdown()
        qapp.processEvents()


# ---------------------------------------------------------------------------
# 6. WS-7 Calibrated DevLab Metrics, Fragment Merging & Hard-Kill Self-Heal
# ---------------------------------------------------------------------------


def test_ws7_calibrated_action_tracker_recorder_and_analyzer(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    from PySide6.QtWidgets import QScrollBar, QWidget
    from hex_helper.devlab.analyzer import _extract_resolution_key
    from hex_helper.devlab.ui_observer import OverlayVisibilityFilter, ScrollOverflowTracker

    # 1) MatchActionTracker calibrated attribution: is_complete_item_fn, flicker dedup,
    #    death-window trigger gating, keep-hero champ select, and death-gated Lv7/degraded silent miss
    emitted: list[tuple[str, str, dict[str, Any], float | None]] = []
    now = [100.0]
    complete_set = {"3031", "3085"}
    tracker = MatchActionTracker(
        lambda d, e, p, gt: emitted.append((d, e, p, gt)),
        clock=lambda: now[0],
        undo_window_s=2.0,
        unlock_miss_timeout_s=45.0,
        is_complete_item_fn=lambda iid: str(iid) in complete_set,
    )
    enemy = _make_player("亚托克斯", "Aatrox", team="CHAOS", level=7)

    # Keep-hero champ select: best_swap_hero_id="" -> followed_best_swap=True & top_recommended_hero=my_hero
    cs_keep = ChampSelectState(2400, "KIWI", True, "44", ("44", "101"), ("236",))
    tracker.on_lcu_champ_select("ChampSelect", cs_keep, goal_mode="climb", best_swap_hero_id="")
    tracker.on_lcu_champ_select("InProgress", None)
    cs_out = [p for _, e, p, _ in emitted if e == "champ_select_outcome_delta"]
    assert len(cs_out) == 1
    assert cs_out[0]["followed_best_swap"] is True
    assert cs_out[0]["top_recommended_hero"] == "44"

    # Component (1038) is filtered out while complete item (3031) & Tier-2 boots (3020) are attributed once
    emitted.clear()
    tracker.reset()
    tracker.record_local_build_rec(10.0, [{"item_id": "3031"}, {"item_id": "3085"}])
    me0 = _make_player("金克丝", "Jinx", level=6, items=())
    tracker.on_live_snapshot(LiveMatchSnapshot(15.0, me0, (me0,), (enemy,), 3400.0))

    now[0] = 101.0
    me_comp = _make_player(
        "金克丝", "Jinx", level=6, items=("1038", "3031", "3020"),
        item_details=[
            {"item_id": 1038, "count": 1, "price": 1300},
            {"item_id": 3031, "count": 1, "price": 3400},
            {"item_id": 3020, "count": 1, "price": 1100},
        ],
    )
    tracker.on_live_snapshot(LiveMatchSnapshot(20.0, me_comp, (me_comp,), (enemy,), 100.0))
    now[0] = 104.0
    tracker.on_live_snapshot(LiveMatchSnapshot(23.0, me_comp, (me_comp,), (enemy,), 100.0))
    purchase_ids = [p["item_id"] for _, e, p, _ in emitted if e == EV_ITEM_PURCHASE_DELTA and p.get("event_kind") != "plan_override"]
    assert 1038 not in purchase_ids
    assert 3031 in purchase_ids
    assert 3020 in purchase_ids

    # Flicker sell/re-buy of 3031 in same match must NOT emit duplicate attribution
    emitted.clear()
    now[0] = 110.0
    tracker.on_live_snapshot(LiveMatchSnapshot(30.0, me0, (me0,), (enemy,), 3400.0))
    now[0] = 111.0
    tracker.on_live_snapshot(LiveMatchSnapshot(31.0, me_comp, (me_comp,), (enemy,), 100.0))
    now[0] = 114.0
    tracker.on_live_snapshot(LiveMatchSnapshot(34.0, me_comp, (me_comp,), (enemy,), 100.0))
    assert not any(
        e == EV_ITEM_PURCHASE_DELTA and p.get("item_id") == 3031 for _, e, p, _ in emitted
    )

    # Death-shop window ignores non-death/non-F6 triggers (e.g. augment_selected)
    emitted.clear()
    now[0] = 130.0
    me_dead = _make_player("金克丝", "Jinx", is_dead=True, level=6, deaths=1, items=("3031",))
    tracker.on_live_snapshot(LiveMatchSnapshot(50.0, me_dead, (me_dead,), (enemy,), 2000.0))
    now[0] = 131.0
    tracker.record_coach_advice(51.0, request_id="req-aug-sel", trigger_reason="augment_selected", recommended_item_ids=[3085])
    now[0] = 140.0
    me_alive = _make_player("金克丝", "Jinx", is_dead=False, level=6, deaths=1, items=("3031",))
    tracker.on_live_snapshot(LiveMatchSnapshot(60.0, me_alive, (me_alive,), (enemy,), 2000.0))
    dws = [p for _, e, p, _ in emitted if e == EV_DEATH_SHOP_WINDOW]
    assert len(dws) == 1
    assert dws[0]["coach_arrived"] is False

    # Lv7 silent miss does NOT fire while player stays alive, nor in degraded_2999 mode
    emitted.clear()
    tracker.reset()
    now[0] = 200.0
    me_lv7_alive = _make_player("金克丝", "Jinx", is_dead=False, level=7)
    tracker.on_live_snapshot(LiveMatchSnapshot(100.0, me_lv7_alive, (me_lv7_alive,), (enemy,), 1400.0), selected_augment_count=1)
    now[0] = 260.0
    tracker.on_live_snapshot(LiveMatchSnapshot(160.0, me_lv7_alive, (me_lv7_alive,), (enemy,), 1400.0), selected_augment_count=1)
    assert not any(e == EV_AUGMENT_UNLOCK_SILENT_MISS for _, e, _, _ in emitted)

    # Degraded 2999 mode skips silent miss altogether even at Lv3 after >45s
    tracker.reset()
    now[0] = 300.0
    me_lv3 = _make_player("金克丝", "Jinx", is_dead=False, level=3)
    tracker.on_live_snapshot(LiveMatchSnapshot(1.0, me_lv3, (me_lv3,), (enemy,), 1400.0), selected_augment_count=0, is_degraded_2999=True)
    now[0] = 360.0
    tracker.on_live_snapshot(LiveMatchSnapshot(61.0, me_lv3, (me_lv3,), (enemy,), 1400.0), selected_augment_count=0, is_degraded_2999=True)
    assert not any(e == EV_AUGMENT_UNLOCK_SILENT_MISS for _, e, _, _ in emitted)

    # 2) Recorder roi_capped_count separation + per-match reset + hard-kill startup self-heal
    devlab_dir = tmp_path / "ws7_devlab"
    rec = DevLabFlightRecorder(
        root_dir=devlab_dir,
        enable_roi_snapshots=True,
        enable_debug_log=False,
        max_roi_per_match=3,
        app_version="1.4.7+ws7",
    )
    shared_hash = hash_match_key("restarted_match_999")
    try:
        # Match fragment 1 (5 ROI attempts with cap=3 -> roi_capped_count=2, dropped_roi=0)
        rec.begin_match(match_key_hash=shared_hash, hero_id="43", hero_name="卡尔玛", game_mode="KIWI")
        crop = np.full((32, 96, 3), 120, dtype=np.uint8)
        for i in range(5):
            rec.enqueue_roi_snapshot(f"cap_test_{i}", crop, ev_seq=i + 1)
        # Emit calibrated events: reroll excluded from picker_opens, 3171/unresolved separated,
        # coach overhaul vs player component override split, resolution @1.00x normalization, OCR field alignment
        rec.emit(DOMAIN_VISION, EV_VISION_PICKER_TRANSITION, {"transition": "open", "card_count": 3}, game_time_s=30.0)
        rec.emit(DOMAIN_VISION, EV_VISION_PICKER_TRANSITION, {"transition": "reroll", "card_count": 3}, game_time_s=32.0)
        rec.emit(DOMAIN_REC, EV_AUGMENT_PICK_DELTA, {"picked_augment_id": "3171", "recommended_augment_id": "1001", "matched_top1": False}, game_time_s=35.0)
        rec.emit(DOMAIN_REC, EV_AUGMENT_PICK_DELTA, {"picked_augment_id": "", "recommended_augment_id": "1001", "matched_top1": False}, game_time_s=36.0)
        rec.emit(DOMAIN_REC, EV_AUGMENT_PICK_DELTA, {"picked_augment_id": "1001", "recommended_augment_id": "1001", "matched_top1": True}, game_time_s=37.0)
        rec.emit(DOMAIN_COACH, EV_COACH_TURN, {"request_id": "r1", "trigger_reason": "augment_picking", "latency_ms": 1100.0, "arrived_before_pick": True, "grounding_stats": {"total_blocked": 0}}, game_time_s=34.0)
        rec.emit(DOMAIN_COACH, EV_COACH_TURN, {"request_id": "r2", "trigger_reason": "augment_selected", "latency_ms": 2500.0, "arrived_before_pick": False, "grounding_stats": {"total_blocked": 0}}, game_time_s=40.0)
        rec.emit(DOMAIN_REC, EV_ITEM_PURCHASE_DELTA, {"event_kind": "plan_override", "override_triggered": True, "override_reason": "allow_overhaul"}, game_time_s=100.0)
        rec.emit(DOMAIN_REC, EV_ITEM_PURCHASE_DELTA, {"event_kind": "plan_override", "override_triggered": True, "override_reason": "component_investment_gte_800g", "committed_gold": 1200}, game_time_s=110.0)
        rec.emit(
            DOMAIN_VISION,
            EV_VISION_OCR_ANOMALY,
            {
                "resolution": "2560x1440@1.00x",
                "near_misses": [{"raw_text": "珠光护", "near_miss_id": "1001", "near_miss_score": 0.50, "slot_idx": 0}],
                "slots": [{"slot_idx": 1, "matched_id": "3171", "matched_name": "力量", "raw_text": "力量", "substring_hit": True, "score": 0.65}],
            },
            game_time_s=42.0,
        )
        rec.emit(DOMAIN_PERF, EV_MAIN_THREAD_STALL, {"window_s": 60.0, "total_ge_50ms_count": 2, "ops": {"tick_vision": {"count": 100, "p95_ms": 20.0}}}, game_time_s=120.0)
        assert rec.flush(timeout_s=3.0) is True
        snap1 = rec.get_status_snapshot()
        assert snap1["roi_capped_count"] == 2
        assert snap1["dropped_roi"] == 0
        rec.end_match(won=-1, duration_s=150, eog_source="interrupted")

        # Match fragment 2 (same match_key_hash -> merges into 1 logical match; per-match roi_capped resets!)
        rec.begin_match(match_key_hash=shared_hash, hero_id="43", hero_name="卡尔玛", game_mode="KIWI")
        snap2 = rec.get_status_snapshot()
        assert snap2["roi_capped_count"] == 0
        assert snap2["dropped_roi"] == 0
        rec.emit(DOMAIN_PERF, EV_MAIN_THREAD_STALL, {"window_s": 60.0, "total_ge_50ms_count": 0, "ops": {"tick_vision": {"count": 300, "p95_ms": 60.0}}}, game_time_s=400.0)
        rec.emit(DOMAIN_GAME, EV_MATCH_EOG, {"won": 1, "duration_s": 650, "eog_source": "eog_stats_block"}, game_time_s=650.0)
        rec.end_match(won=1, duration_s=650, eog_source="eog_stats_block")
        assert rec.flush(timeout_s=3.0) is True
    finally:
        rec.flush_and_close()

    # Simulate hard-kill unclosed match on disk -> new recorder self-heals meta.json & index.json on startup
    idx_path = devlab_dir / "index.json"
    idx_data = json.loads(idx_path.read_text(encoding="utf-8"))
    first_mdir = sorted(p for p in (devlab_dir / "matches").iterdir() if p.is_dir())[0]
    meta_path = first_mdir / "meta.json"
    meta_broken = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_broken["phase"] = "in_game"
    meta_broken["ended_at"] = ""
    meta_broken["event_count"] = 0
    meta_path.write_text(json.dumps(meta_broken, ensure_ascii=False, indent=2), encoding="utf-8")
    idx_data["matches"][0]["ended_at"] = ""
    idx_data["matches"][0]["event_count"] = 0
    idx_path.write_text(json.dumps(idx_data, ensure_ascii=False, indent=2), encoding="utf-8")

    rec_heal = DevLabFlightRecorder(root_dir=devlab_dir, enable_roi_snapshots=False, enable_debug_log=False, app_version="1.4.7+ws7")
    rec_heal.flush_and_close()
    healed_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert healed_meta["event_count"] > 0
    assert healed_meta["ended_at"] != ""

    # Verify calibrated analyzer report & doctor
    assert _extract_resolution_key({"resolution": "2560x1440@1.00x"}) == "2560x1440"
    assert _extract_resolution_key({"resolution": "2560x1440", "dpr": 1.0}) == "2560x1440"
    assert _extract_resolution_key({"resolution": "2560x1440", "dpr": 1.5}) == "2560x1440@1.50x"

    analyzer = DevLabAnalyzer(root_dir=devlab_dir)
    diag = analyzer.doctor()
    assert diag["status"] == "pass", diag
    assert diag["checks"]["queue_drops"]["status"] == "pass"
    assert diag["checks"]["queue_drops"]["dropped_roi"] == 0
    assert diag["checks"]["queue_drops"]["roi_capped_count"] == 2

    report = analyzer.generate_evolution_report(last_n=20)
    # 2 folders with identical match_key_hash merge into 1 logical match!
    assert report.total_matches == 1
    assert report.mayhem_matches == 1
    p1 = report.pillars["recommendation"]
    p2 = report.pillars["vision"]
    p4 = report.pillars["coach"]
    p5 = report.pillars["runtime"]
    assert p1["mayhem_winrate"] == pytest.approx(1.0)
    assert p1["augment_top1_adoption_rate"] == pytest.approx(1.0)  # 3171 & unresolved excluded from denominator!
    assert p1["augment_unresolved_picks"] == 1
    assert p1["player_component_override_count"] == 1
    assert p1["coach_overhaul_count"] == 1
    assert p2["picker_opens"] == 1  # reroll NOT counted as picker_open!
    assert p2["ocr_nearmiss_count"] >= 1
    assert p4["picker_ai_timely_rate"] == pytest.approx(1.0)  # augment_selected excluded from picker_ai_timely!
    # Sample-weighted tick_vision P95: (100*20 + 300*60) / 400 = 50.0ms
    assert p5["tick_vision_p95_ms"] == pytest.approx(50.0, abs=0.5)
    assert p5["writer_roi_capped_count"] == 2

    # 3) OverlayVisibilityFilter excludes manual user opens from ghost_repopup & ScrollOverflowTracker debounces <=8px / 150ms
    ui_emitted: list[tuple[str, str, dict[str, Any]]] = []

    class _StubRec:
        def emit(self, domain: str, event: str, payload: dict[str, Any], **_kw: Any) -> None:
            ui_emitted.append((domain, event, payload))

    stub_rec = _StubRec()
    w = QWidget()
    ov_filter = OverlayVisibilityFilter("edge_drawer", stub_rec, parent=w)
    w.installEventFilter(ov_filter)
    sb = QScrollBar(w)
    scroll_tracker = ScrollOverflowTracker("edge_drawer.panel", sb, stub_rec, overlay_name="edge_drawer", debounce_ms=150, overflow_threshold_px=8, parent=w)
    ov_filter.attach_scroll_tracker(scroll_tracker)

    try:
        # User opens, closes via user_close, then immediately re-opens via lookup_btn -> NOT ghost_repopup!
        ov_filter.mark_open_source("lookup_btn")
        w.show()
        qapp.processEvents()
        # Transient 1-frame squeeze right after show (within 150ms grace, immediately settles back to <=8px)
        sb.setRange(0, 480)
        sb.setRange(0, 4)
        ov_filter.mark_close_source("user_close")
        w.hide()
        qapp.processEvents()

        ov_filter.mark_open_source("lookup_btn")
        w.show()
        qapp.processEvents()
        ov_filter.mark_close_source("user_close")
        w.hide()
        qapp.processEvents()

        sessions = [p for _, e, p in ui_emitted if e == EV_UI_OVERLAY_SESSION]
        assert len(sessions) == 2
        assert sessions[1]["open_source"] == "lookup_btn"
        assert sessions[1]["ghost_repopup"] is False
        # Transient 1-frame squeeze suppressed by 150ms debounce & 8px threshold!
        assert not any(e == EV_UI_SCROLL_OVERFLOW for _, e, _ in ui_emitted)
    finally:
        scroll_tracker.uninstall()
        w.close()
        qapp.processEvents()
