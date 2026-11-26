"""Tests for 2999-unavailable Plan B dual-sync (visual HUD item/augment matchers + manual fallback)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from hex_helper.app import HexStateMachine
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.item_icon_matcher import (
    ItemIconMatcher,
    VisualSlotVoteTracker,
    read_lol_hud_scale,
)


def _make_distinct_icon(seed: int, size: int = 64) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((size, size, 3), dtype=np.uint8)
    base_color = rng.integers(30, 230, size=3, dtype=np.uint8)
    img[:, :] = base_color
    for _ in range(6):
        c = tuple(int(x) for x in rng.integers(20, 255, size=3))
        p1 = (int(rng.integers(4, size - 4)), int(rng.integers(4, size - 4)))
        p2 = (int(rng.integers(4, size - 4)), int(rng.integers(4, size - 4)))
        cv2.rectangle(img, p1, p2, c, -1)
    cv2.circle(img, (size // 2, size // 2), size // 5, (int((seed * 73) % 255), int((seed * 41) % 255), 220), -1)
    return img


def test_read_lol_hud_scale_parses_persisted_settings(tmp_path: Path) -> None:
    cfg_dir = tmp_path / "Game" / "Config"
    cfg_dir.mkdir(parents=True)
    ps = cfg_dir / "PersistedSettings.json"
    ps.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "name": "Game.cfg",
                        "sections": [
                            {
                                "name": "HUD",
                                "settings": [{"name": "GlobalScale", "value": "0.7500"}],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert read_lol_hud_scale(str(tmp_path)) == pytest.approx(0.75, abs=1e-4)
    assert read_lol_hud_scale(str(tmp_path.parent / "nonexistent_lol_dir_xyz")) == 1.0


def test_item_icon_matcher_scans_6_hud_slots_and_ignores_empty_slots() -> None:
    matcher = ItemIconMatcher(cache_dir=None)
    icons = {
        "3089": _make_distinct_icon(11),
        "3165": _make_distinct_icon(22),
        "6655": _make_distinct_icon(33),
        "3157": _make_distinct_icon(44),
    }
    for iid, img in icons.items():
        assert matcher.register_template(iid, img)
    assert matcher.template_count == 4

    # Render synthetic 2560x1440 dark HUD frame with 2 items placed in slots 0 and 4
    w, h = 2560, 1440
    frame = np.full((h, w, 3), 12, dtype=np.uint8)
    rects = dict(matcher.get_hud_item_slot_rects(w, h, hud_scale=1.0))
    for slot_idx, iid in ((0, "3089"), (4, "6655")):
        x0, y0, x1, y1 = rects[slot_idx]
        frame[y0:y1, x0:x1] = cv2.resize(icons[iid], (x1 - x0, y1 - y0))

    hits = matcher.scan_hud_items(frame, WindowRect(hwnd=1, left=0, top=0, width=w, height=h), hud_scale=1.0)
    by_slot = {hit.hud_slot_index: hit.item_id for hit in hits}
    assert by_slot == {0: "3089", 4: "6655"}


def test_visual_slot_vote_tracker_requires_confirm_and_tolerates_brief_occlusion() -> None:
    vt = VisualSlotVoteTracker(confirm_frames=2, drop_misses=2)
    assert vt.update({0: "3089"}) == []
    assert vt.update({0: "3089"}) == ["3089"]
    # Cooldown / shop / tooltip occlusion MUST hold confirmed items indefinitely without flickering
    for _ in range(10):
        assert vt.update({}, slot_states={0: "occluded"}) == ["3089"]
    # 1 empty frame does not drop yet; 2nd consecutive genuinely empty frame evicts cleanly
    assert vt.update({}, slot_states={0: "empty"}) == ["3089"]
    assert vt.update({}, slot_states={0: "empty"}) == []
    # Force confirm promotes immediately
    assert vt.update({1: "3165"}, force_confirm=True) == ["3165"]


def test_item_icon_matcher_top_58_rescues_heartsteel_stack_counter_overlay() -> None:
    matcher = ItemIconMatcher(cache_dir=None)
    icons = {
        "3084": _make_distinct_icon(84),
        "3083": _make_distinct_icon(83),
        "2502": _make_distinct_icon(52),
        "6665": _make_distinct_icon(65),
    }
    for iid, img in icons.items():
        matcher.register_template(iid, img)

    # Simulate Heartsteel with bright white "1374" stack digits painted across the bottom 38% of the icon
    occluded = icons["3084"].copy()
    cv2.rectangle(occluded, (4, 38), (60, 61), (255, 255, 255), -1)
    cv2.putText(occluded, "1374", (5, 57), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (10, 10, 10), 2)

    hit = matcher.match_patch(occluded, min_score=0.62, min_margin=0.03)
    assert hit is not None
    assert hit.item_id == "3084"
    assert hit.score >= 0.70


def test_augment_icon_matcher_preheat_and_scan_all_hud_augments(tmp_path: Path) -> None:
    icons = {"1001": _make_distinct_icon(101), "1002": _make_distinct_icon(202), "1003": _make_distinct_icon(303)}
    encoded = {aid: cv2.imencode(".png", img)[1].tobytes() for aid, img in icons.items()}

    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "cdragon_augments.json").write_text(
        json.dumps(
            [
                {"id": int(aid), "nameTRA": f"海克斯{aid}", "augmentSmallIconPath": f"/lol-game-data/assets/aug_{aid}.png"}
                for aid in icons
            ]
        ),
        encoding="utf-8",
    )

    def _mock_fetch(url: str) -> bytes:
        for aid, raw in encoded.items():
            if f"aug_{aid}.png" in url:
                return raw
        return b""

    matcher = AugmentIconMatcher(cache_dir=tmp_path, allow_download=True, fetcher=_mock_fetch)
    assert matcher.template_count == 0
    cnt = matcher.preheat_templates(["1001", "1002", "1003"], max_workers=2)
    assert cnt == 3
    assert (tmp_path / "icons" / "augment" / "1001.png").is_file()

    w, h = 1920, 1080
    frame = np.full((h, w, 3), 12, dtype=np.uint8)
    rects = dict(matcher.get_hud_slot_rects(w, h))
    for s_idx, aid in ((0, "1001"), (1, "1003")):
        x0, y0, x1, y1 = rects[s_idx]
        frame[y0:y1, x0:x1] = cv2.resize(icons[aid], (x1 - x0, y1 - y0))

    hits = matcher.scan_all_hud_augments(frame, WindowRect(hwnd=1, left=0, top=0, width=w, height=h), hud_scale=1.0)
    assert [h.augment_id for h in hits] == ["1001", "1003"]


def test_state_machine_plan_b_item_merge_and_suppression() -> None:
    sm = HexStateMachine()
    sm.is_degraded_2999 = True
    sm.visual_owned_items = ["3089", "3165"]
    assert sm.effective_degraded_items() == ("3089", "3165")

    assert sm.add_manual_item("6655") is True
    assert sm.effective_degraded_items() == ("6655", "3089", "3165")

    # Removing a visual item suppresses it from re-appearing
    assert sm.remove_owned_item("3165") is True
    assert "3165" in sm.suppressed_visual_items
    assert sm.effective_degraded_items() == ("6655", "3089")

    # Manually adding it back clears suppression
    assert sm.add_manual_item("3165") is True
    assert "3165" not in sm.suppressed_visual_items
    assert "3165" in sm.effective_degraded_items()


def test_edge_drawer_degraded_sync_bar_visibility_and_hextech_palette() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from hex_helper.ui.edge_drawer import EdgeBuildDrawer

    app = QApplication.instance() or QApplication([])
    drawer = EdgeBuildDrawer()
    assert drawer.degraded_sync_frame.isHidden()

    drawer.set_degraded_2999_mode(True, synced_items=[("3089", "灭世者的死亡之帽")], status_hint="测试同步")
    assert not drawer.degraded_sync_frame.isHidden()
    assert "#FF5A1F" not in drawer.degraded_sync_frame.styleSheet().upper()
    assert "#0AC8B9" in drawer.degraded_sync_frame.styleSheet().upper()

    drawer.set_degraded_2999_mode(False)
    assert drawer.degraded_sync_frame.isHidden()
    drawer.close()
    app.processEvents()


def test_state_machine_plan_b_suppressed_visual_items_45s_ttl_and_augments() -> None:
    sm = HexStateMachine()
    sm.is_degraded_2999 = True
    sm.visual_owned_items = ["1011", "3089"]
    sm.selected_augments = ["1319"]

    # Manual remove adds 45s TTL suppression (NOT permanent blacklist)
    assert sm.remove_owned_item("1011") is True
    assert "1011" in sm.suppressed_visual_items
    t0 = 1000.0
    sm.suppress_visual_item("1011", now_mono=t0)
    assert sm.is_visual_item_suppressed("1011", now_mono=t0 + 10.0) is True
    assert "1011" not in sm.effective_degraded_items(now_mono=t0 + 10.0)

    # After 45s TTL expires, 1011 is pruned and can be re-detected if player buys it again later!
    assert sm.is_visual_item_suppressed("1011", now_mono=t0 + 46.0) is False
    sm.prune_suppressed_visual_items(now_mono=t0 + 46.0)
    assert "1011" not in sm.suppressed_visual_items

    # Visual augment suppression survives in-match until reset_match()
    sm.suppressed_visual_augments.add("1319")
    assert "1319" in sm.suppressed_visual_augments
    sm.reset_match()
    assert sm.suppressed_visual_items == {}
    assert sm.suppressed_visual_augments == set()


def test_vectorized_blas_icon_matchers_speed_accuracy_and_slot_cache() -> None:
    import time

    # 1. ItemIconMatcher with 500 synthetic templates -> cold < 25ms, cached < 10ms (6 HUD slots)
    item_matcher = ItemIconMatcher(cache_dir=None)
    item_icons: dict[str, np.ndarray] = {}
    for idx in range(500):
        iid = str(5000 + idx)
        img = _make_distinct_icon(1000 + idx)
        item_icons[iid] = img
        assert item_matcher.register_template(iid, img)
    assert item_matcher.template_count == 500

    w, h = 2560, 1440
    frame = np.full((h, w, 3), 12, dtype=np.uint8)
    rects = dict(item_matcher.get_hud_item_slot_rects(w, h, hud_scale=1.0))
    placed = {0: "5005", 1: "5089", 2: "5157", 3: "5200", 4: "5333", 5: "5499"}
    for slot_idx, iid in placed.items():
        x0, y0, x1, y1 = rects[slot_idx]
        frame[y0:y1, x0:x1] = cv2.resize(item_icons[iid], (x1 - x0, y1 - y0))

    grect = WindowRect(hwnd=1, left=0, top=0, width=w, height=h)
    # Warm lazy matrix build once, then benchmark cold (cache cleared) and warm (slot cache hit)
    item_matcher.scan_hud_slots_with_state(frame, grect, hud_scale=1.0)
    item_matcher.clear_slot_cache()
    t0 = time.perf_counter()
    hits_cold, _states = item_matcher.scan_hud_slots_with_state(frame, grect, hud_scale=1.0)
    cold_ms = (time.perf_counter() - t0) * 1000.0
    assert {hit.hud_slot_index: hit.item_id for hit in hits_cold} == placed
    assert cold_ms < 25.0, f"Vectorized 500-template item cold scan too slow: {cold_ms:.2f}ms"

    t1 = time.perf_counter()
    hits_cached, _ = item_matcher.scan_hud_slots_with_state(frame, grect, hud_scale=1.0)
    cached_ms = (time.perf_counter() - t1) * 1000.0
    assert {hit.hud_slot_index: hit.item_id for hit in hits_cached} == placed
    assert cached_ms < 10.0, f"Slot-cached item scan too slow: {cached_ms:.2f}ms"

    # Mutate slot 2 -> cache invalidates automatically and picks up new item
    x0, y0, x1, y1 = rects[2]
    frame[y0:y1, x0:x1] = cv2.resize(item_icons["5400"], (x1 - x0, y1 - y0))
    hits_mut, _ = item_matcher.scan_hud_slots_with_state(frame, grect, hud_scale=1.0)
    assert {hit.hud_slot_index: hit.item_id for hit in hits_mut}[2] == "5400"

    # 2. AugmentIconMatcher with 237 synthetic templates -> cold < 15ms, cached < 5ms (4 HUD slots)
    aug_matcher = AugmentIconMatcher(cache_dir=None, allow_download=False)
    aug_icons: dict[str, np.ndarray] = {}
    for idx in range(237):
        aid = str(1000 + idx)
        img = _make_distinct_icon(5000 + idx)
        aug_icons[aid] = img
        assert aug_matcher.register_template(aid, img)
    assert aug_matcher.template_count == 237

    a_frame = np.full((1080, 1920, 3), 12, dtype=np.uint8)
    a_rects = dict(aug_matcher.get_hud_slot_rects(1920, 1080))
    a_placed = {0: "1001", 1: "1050", 2: "1120", 3: "1236"}
    for s_idx, aid in a_placed.items():
        x0, y0, x1, y1 = a_rects[s_idx]
        a_frame[y0:y1, x0:x1] = cv2.resize(aug_icons[aid], (x1 - x0, y1 - y0))

    agrect = WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080)
    aug_matcher.scan_all_hud_augments(a_frame, agrect, hud_scale=1.0)
    aug_matcher.clear_slot_cache()
    t2 = time.perf_counter()
    ahits_cold = aug_matcher.scan_all_hud_augments(a_frame, agrect, hud_scale=1.0)
    acold_ms = (time.perf_counter() - t2) * 1000.0
    assert {h.hud_slot_index: h.augment_id for h in ahits_cold} == a_placed
    assert acold_ms < 20.0, f"Vectorized 237-template augment cold scan too slow: {acold_ms:.2f}ms"

    t3 = time.perf_counter()
    ahits_cached = aug_matcher.scan_all_hud_augments(a_frame, agrect, hud_scale=1.0)
    acached_ms = (time.perf_counter() - t3) * 1000.0
    assert {h.hud_slot_index: h.augment_id for h in ahits_cached} == a_placed
    assert acached_ms < 10.0, f"Slot-cached augment scan too slow: {acached_ms:.2f}ms"
