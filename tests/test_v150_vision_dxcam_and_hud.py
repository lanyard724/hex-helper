"""Tests for v1.5.0 WS-B: dxcam COM apartment fix in HexVisionWorker & unified hud_scale in AugmentIconMatcher."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import types
from typing import Any

import cv2
import numpy as np
import pytest

from hex_helper.data.provider import AugmentInfo
from hex_helper.vision import capture as capture_mod
from hex_helper.vision import worker as worker_mod
from hex_helper.vision.augment_icon_matcher import (
    DEFAULT_HUD_AUGMENT_SLOTS,
    AugmentIconMatcher,
    build_hud_icon_strip_crop,
)
from hex_helper.vision.capture import ScreenCapturer, WindowRect, get_primary_screen_rect
from hex_helper.vision.worker import (
    COINIT_APARTMENTTHREADED,
    COINIT_MULTITHREADED,
    HexVisionWorker,
    VisionFrameOutput,
    VisionTickContext,
    _resolve_worker_coinit_mode,
)


def _make_distinct_icon(seed: int, size: int = 64) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((size, size, 3), dtype=np.uint8)
    base_color = rng.integers(35, 225, size=3, dtype=np.uint8)
    img[:, :] = base_color
    for _ in range(7):
        c = tuple(int(x) for x in rng.integers(20, 255, size=3))
        p1 = (int(rng.integers(4, size - 4)), int(rng.integers(4, size - 4)))
        p2 = (int(rng.integers(4, size - 4)), int(rng.integers(4, size - 4)))
        cv2.rectangle(img, p1, p2, c, -1)
    cv2.circle(
        img,
        (size // 2, size // 2),
        size // 5,
        (int((seed * 73) % 255), int((seed * 41) % 255), 220),
        -1,
    )
    return img


def _paste_icons_at_scale(
    frame_bgr: np.ndarray,
    matcher: AugmentIconMatcher,
    slot_icons: dict[int, np.ndarray],
    *,
    hud_scale: float,
    game_rect: WindowRect | None = None,
) -> dict[int, tuple[int, int, int, int]]:
    ox = game_rect.left if game_rect is not None else 0
    oy = game_rect.top if game_rect is not None else 0
    fw = game_rect.width if game_rect is not None else frame_bgr.shape[1]
    fh = game_rect.height if game_rect is not None else frame_bgr.shape[0]
    rects = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=hud_scale))
    for slot_idx, icon in slot_icons.items():
        x0, y0, x1, y1 = rects[slot_idx]
        frame_bgr[oy + y0 : oy + y1, ox + x0 : ox + x1] = cv2.resize(
            icon,
            (x1 - x0, y1 - y0),
            interpolation=cv2.INTER_AREA,
        )
    return rects


# ---------------------------------------------------------------------------
# 1. AugmentIconMatcher unified hud_scale geometry & propagation
# ---------------------------------------------------------------------------


def test_hud_slot_rects_scaling_around_bottom_center_anchor() -> None:
    matcher = AugmentIconMatcher(cache_dir=None, allow_download=False)
    fw, fh = 2560, 1440

    rects_default = dict(matcher.get_hud_slot_rects(fw, fh))
    rects_10 = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=1.0))
    rects_098 = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=0.98))
    rects_05 = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=0.5))
    rects_00 = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=0.0))

    # Default, 1.0, and within <=0.02 identity tolerance (0.98 -> scale=0.99) match exact existing math
    assert rects_default == rects_10
    assert rects_098 == rects_10

    # Out-of-range hud_scale clamps cleanly to [0.0, 1.0]
    assert dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=-0.5)) == rects_00
    assert dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=1.5)) == rects_10

    anchor_x = fw * 0.5
    anchor_y = float(fh)
    for idx, (rx, ry, rw, rh) in enumerate(DEFAULT_HUD_AUGMENT_SLOTS):
        r10 = rects_10[idx]
        r05 = rects_05[idx]
        r00 = rects_00[idx]

        # Every slot stays square
        s10 = r10[2] - r10[0]
        s05 = r05[2] - r05[0]
        s00 = r00[2] - r00[0]
        assert s10 == r10[3] - r10[1]
        assert s05 == r05[3] - r05[1]
        assert s00 == r00[3] - r00[1]

        # Proportional shrink: hud_scale=0.5 -> scale=0.75, hud_scale=0.0 -> scale=0.50
        base_side = min(rw * fw, rh * fh)
        assert s10 == max(8, int(round(base_side)))
        assert s05 == max(8, int(round(base_side * 0.75)))
        assert s00 == max(8, int(round(base_side * 0.50)))
        assert s10 > s05 > s00

        # Centers shift monotonically toward bottom-center anchor (0.5, 1.0)
        cx10, cy10 = (r10[0] + r10[2]) * 0.5, (r10[1] + r10[3]) * 0.5
        cx05, cy05 = (r05[0] + r05[2]) * 0.5, (r05[1] + r05[3]) * 0.5
        cx00, cy00 = (r00[0] + r00[2]) * 0.5, (r00[1] + r00[3]) * 0.5
        assert cx10 < cx05 < cx00 < anchor_x
        assert cy10 < cy05 < cy00 < anchor_y

        # Exact normalized center math verification
        for scale, (cx_act, cy_act) in ((0.75, (cx05, cy05)), (0.50, (cx00, cy00))):
            exp_cx = (0.5 + ((rx + rw * 0.5) - 0.5) * scale) * fw
            exp_cy = (1.0 - (1.0 - (ry + rh * 0.5)) * scale) * fh
            assert abs(cx_act - exp_cx) <= 1.0
            assert abs(cy_act - exp_cy) <= 1.0


@pytest.mark.parametrize("hud_scale", [1.0, 0.5, 0.0])
def test_hud_scale_unified_across_patches_strip_verify_and_scan(hud_scale: float) -> None:
    matcher = AugmentIconMatcher(cache_dir=None, allow_download=False)
    icons = {
        "1001": _make_distinct_icon(11),
        "1002": _make_distinct_icon(22),
        "1003": _make_distinct_icon(33),
        "1004": _make_distinct_icon(44),
    }
    for aid, img in icons.items():
        assert matcher.register_template(aid, img, name=f"Aug_{aid}") is True

    fw, fh = 2560, 1440
    frame = np.full((fh, fw, 3), 14, dtype=np.uint8)
    game_rect = WindowRect(left=0, top=0, width=fw, height=fh, hwnd=1)

    # For scaled HUDs, paint distracting decoy stats-box pixels at unscaled (1.0) rects
    # to simulate the m_00013 bug where unscaled cropping hit champion stats instead of augments.
    if hud_scale < 1.0:
        unscaled_rects = dict(matcher.get_hud_slot_rects(fw, fh, hud_scale=1.0))
        for x0, y0, x1, y1 in unscaled_rects.values():
            frame[y0:y1, x0:x1] = (180, 175, 160)
            cv2.putText(frame, "147", (x0 + 4, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (20, 20, 20), 2)

    placed = {0: "1001", 1: "1002", 2: "1003", 3: "1004"}
    _paste_icons_at_scale(
        frame,
        matcher,
        {slot: icons[aid] for slot, aid in placed.items()},
        hud_scale=hud_scale,
    )

    # 1. extract_hud_augment_patches returns all 4 scaled square patches
    patches = matcher.extract_hud_augment_patches(frame, game_rect, hud_scale=hud_scale)
    assert [idx for idx, _ in patches] == [0, 1, 2, 3]
    for _idx, patch in patches:
        assert patch.shape[0] == patch.shape[1]

    # 2. Instance method & module-level build_hud_icon_strip_crop produce byte-identical strips
    strip_from_method = matcher.build_hud_icon_strip_crop(frame, game_rect, hud_scale=hud_scale)
    strip_from_func = build_hud_icon_strip_crop(frame, game_rect, hud_scale=hud_scale)
    strip_from_patches = build_hud_icon_strip_crop(patches)
    assert strip_from_method is not None and strip_from_method.shape == (28, 112, 3)
    assert strip_from_func is not None and np.array_equal(strip_from_method, strip_from_func)
    assert strip_from_patches is not None and np.array_equal(strip_from_method, strip_from_patches)

    # 3. verify_picked_augment accurately matches every slot at the exact hud_scale
    for slot_idx, expected_aid in placed.items():
        hit = matcher.verify_picked_augment(
            frame,
            game_rect,
            candidate_ids=list(icons.keys()),
            slot_index_hint=slot_idx,
            min_score=0.58,
            hud_scale=hud_scale,
        )
        assert hit is not None
        assert hit.augment_id == expected_aid
        assert hit.hud_slot_index == slot_idx
        assert hit.score >= 0.72
        diag = matcher.last_verify_diagnostics()
        assert diag["outcome"] == "matched"
        assert diag["hud_scale"] == pytest.approx(hud_scale, abs=1e-3)

    # 4. scan_all_hud_augments shares the exact same scaled geometry & hits all 4 slots
    scan_hits = matcher.scan_all_hud_augments(frame, game_rect, hud_scale=hud_scale)
    assert {h.hud_slot_index: h.augment_id for h in scan_hits} == placed

    # 5. Unscaled lookup (hud_scale=1.0) on a scaled HUD misses the real scaled icons
    if hud_scale < 1.0:
        unscaled_strip = matcher.build_hud_icon_strip_crop(frame, game_rect, hud_scale=1.0)
        assert unscaled_strip is not None
        assert not np.array_equal(unscaled_strip, strip_from_method)
        wrong_hit = matcher.verify_picked_augment(
            frame,
            game_rect,
            candidate_ids=list(icons.keys()),
            slot_index_hint=0,
            min_score=0.58,
            hud_scale=1.0,
        )
        assert wrong_hit is None


def test_hud_scale_works_with_offset_subwindow_game_rect() -> None:
    matcher = AugmentIconMatcher(cache_dir=None, allow_download=False)
    icon = _make_distinct_icon(77)
    assert matcher.register_template("2001", icon, name="ScaledSub") is True

    desktop = np.full((1440, 2560, 3), 16, dtype=np.uint8)
    game_rect = WindowRect(left=320, top=180, width=1920, height=1080, hwnd=9)
    _paste_icons_at_scale(desktop, matcher, {2: icon}, hud_scale=0.5, game_rect=game_rect)

    hit = matcher.verify_picked_augment(
        desktop,
        game_rect,
        candidate_ids=["2001"],
        slot_index_hint=2,
        hud_scale=0.5,
    )
    assert hit is not None
    assert hit.augment_id == "2001"
    assert hit.hud_slot_index == 2
    assert hit.score >= 0.72


# ---------------------------------------------------------------------------
# 2. ScreenCapturer._try_create_dxcam COM flags, E_ACCESSDENIED retry, warning log
# ---------------------------------------------------------------------------


def test_screen_capturer_sets_coinit_flags_when_comtypes_unimported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "comtypes", raising=False)
    monkeypatch.delitem(sys.modules, "dxcam", raising=False)
    monkeypatch.delattr(sys, "coinit_flags", raising=False)

    observed_flags: list[Any] = []
    fake_cam = object()
    fake_dxcam = types.ModuleType("dxcam")

    def _create(*, output_color: str = "RGB") -> Any:
        assert output_color == "BGR"
        observed_flags.append(getattr(sys, "coinit_flags", None))
        return fake_cam

    fake_dxcam.create = _create  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dxcam", fake_dxcam)

    cap = ScreenCapturer(prefer_dxcam=True, lazy=True)
    assert cap._try_create_dxcam() is True
    assert cap.backend == "dxcam"
    assert cap._dx_cam is fake_cam
    assert observed_flags == [0]
    assert getattr(sys, "coinit_flags", None) == 0


def test_screen_capturer_does_not_overwrite_coinit_flags_when_comtypes_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "comtypes", types.ModuleType("comtypes"))
    monkeypatch.delattr(sys, "coinit_flags", raising=False)

    fake_dxcam = types.ModuleType("dxcam")
    fake_dxcam.create = lambda output_color="BGR": object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dxcam", fake_dxcam)

    cap = ScreenCapturer(prefer_dxcam=True, lazy=True)
    assert cap._try_create_dxcam() is True
    assert not hasattr(sys, "coinit_flags")


def test_screen_capturer_retries_after_attach_default_desktop_on_e_accessdenied(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FakeComAccessDenied(OSError):
        def __init__(self) -> None:
            super().__init__(-2147024891, "Access is denied.")
            self.hresult = -2147024891

    attempts = 0
    attach_calls = 0
    fake_cam = object()

    def _fake_attach() -> bool:
        nonlocal attach_calls
        attach_calls += 1
        return True

    def _create(*, output_color: str = "BGR") -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _FakeComAccessDenied()
        assert attach_calls == 1
        return fake_cam

    fake_dxcam = types.ModuleType("dxcam")
    fake_dxcam.create = _create  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dxcam", fake_dxcam)
    monkeypatch.setattr(capture_mod, "attach_default_desktop", _fake_attach)

    with caplog.at_level(logging.WARNING, logger="hex_helper.vision.capture"):
        cap = ScreenCapturer(prefer_dxcam=True, lazy=True)
        assert cap._try_create_dxcam() is True

    assert attempts == 2
    assert attach_calls == 1
    assert cap.backend == "dxcam"
    assert cap._dx_cam is fake_cam
    assert len(caplog.records) == 0


def test_screen_capturer_logs_warning_once_on_mss_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    err_msg = "[WinError -2147417850] 无法在设置线程模式后对其加以更改。"
    attach_calls = 0

    def _fake_attach() -> bool:
        nonlocal attach_calls
        attach_calls += 1
        return True

    def _broken_create(*, output_color: str = "BGR") -> Any:
        raise OSError(err_msg)

    fake_dxcam = types.ModuleType("dxcam")
    fake_dxcam.create = _broken_create  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dxcam", fake_dxcam)
    monkeypatch.setattr(capture_mod, "attach_default_desktop", _fake_attach)

    cap = ScreenCapturer(prefer_dxcam=True, lazy=True)
    cap._ensure_mss = lambda *, set_active=True: setattr(cap, "_backend", "mss") if set_active else None  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="hex_helper.vision.capture"):
        cap._init_backends()
        # Subsequent retry failures on the same instance must NOT spam WARNING logs
        assert cap._try_create_dxcam() is False
        assert cap._try_create_dxcam() is False

    assert cap.backend == "mss"
    assert attach_calls == 0  # non-E_ACCESSDENIED must not spuriously invoke attach_default_desktop
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1
    assert err_msg in warn_records[0].getMessage()


# ---------------------------------------------------------------------------
# 3. HexVisionWorker COM apartment resolution & desktop attach guard
# ---------------------------------------------------------------------------


def test_worker_coinit_mode_resolution_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    # Case 1: comtypes NOT imported, coinit_flags unset -> sets 0 and chooses MTA
    monkeypatch.delitem(sys.modules, "comtypes", raising=False)
    monkeypatch.delattr(sys, "coinit_flags", raising=False)
    assert _resolve_worker_coinit_mode() == COINIT_MULTITHREADED
    assert getattr(sys, "coinit_flags") == 0

    # Case 2: comtypes already imported with default STA (coinit_flags unset) -> chooses STA
    monkeypatch.setitem(sys.modules, "comtypes", types.ModuleType("comtypes"))
    monkeypatch.delattr(sys, "coinit_flags", raising=False)
    assert _resolve_worker_coinit_mode() == COINIT_APARTMENTTHREADED

    # Case 3: comtypes already imported with coinit_flags=0 (MTA) -> chooses MTA
    monkeypatch.setattr(sys, "coinit_flags", 0, raising=False)
    assert _resolve_worker_coinit_mode() == COINIT_MULTITHREADED

    # Case 4: comtypes already imported with coinit_flags=2 (STA) -> chooses STA
    monkeypatch.setattr(sys, "coinit_flags", 2, raising=False)
    assert _resolve_worker_coinit_mode() == COINIT_APARTMENTTHREADED


def test_worker_thread_entry_guards_attach_default_desktop_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_attach = threading.Event()

    def _boom_attach() -> bool:
        called_attach.set()
        raise RuntimeError("headless stub desktop error")

    monkeypatch.setattr(worker_mod, "attach_default_desktop", _boom_attach)
    worker = HexVisionWorker(
        capturer=ScreenCapturer(prefer_dxcam=False, lazy=True),
        ocr_engine=None,
        tick_interval_s=0.05,
    )
    worker._ocr_init_attempted = True
    try:
        worker.start()
        assert called_attach.wait(timeout=2.0) is True
        assert worker.is_running() is True
    finally:
        worker.stop(timeout=2.0)
        assert worker.is_running() is False


# ---------------------------------------------------------------------------
# 4. Live Windows host verification: HexVisionWorker + dxcam + WinRtOcrEngine
# ---------------------------------------------------------------------------


def _has_windows_desktop() -> bool:
    if sys.platform != "win32":
        return False
    rect = get_primary_screen_rect()
    return rect.width >= 320 and rect.height >= 200


@pytest.mark.skipif(not _has_windows_desktop(), reason="Requires interactive Windows desktop")
def test_live_windows_vision_worker_dxcam_and_winrt_coexist() -> None:
    """Live Windows host test: verify HexVisionWorker uses dxcam (not mss) alongside WinRtOcrEngine."""
    # 1. Fresh child process mimicking real production startup (PySide6 QApplication STA on main thread,
    #    comtypes unimported before HexVisionWorker starts -> must select dxcam, never RPC_E_CHANGED_MODE).
    probe_code = """
import threading
from PySide6.QtWidgets import QApplication
from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.capture import get_primary_screen_rect
from hex_helper.vision.worker import HexVisionWorker, VisionTickContext

app = QApplication.instance() or QApplication([])
received = []
done = threading.Event()

def _on_out(out):
    received.append(out)
    done.set()

worker = HexVisionWorker(
    on_output=_on_out,
    prefer_dxcam=True,
    initial_augments=[AugmentInfo("1001", "泰坦的坚决", "prismatic")],
    find_window_fn=get_primary_screen_rect,
    is_foreground_fn=lambda _h: True,
    get_cursor_pos_fn=lambda: (100, 100),
)
try:
    worker.start()
    worker.update_context(VisionTickContext(vision_gen=1, game_state="IN_GAME", hero_id="98"))
    worker.enqueue_command("force_rescan")
    assert done.wait(timeout=8.0), "HexVisionWorker timed out on force_rescan"
    out = received[-1]
    assert out.error == "", f"unexpected worker error: {out.error}"
    assert out.capture_backend == "dxcam", f"expected dxcam backend, got {out.capture_backend!r}"
    assert worker._capturer is not None and worker._capturer.backend == "dxcam"
    assert worker._ocr_engine is not None, "WinRtOcrEngine failed to initialize on worker thread"
    assert out.is_black is False, "captured live desktop frame was black"
finally:
    worker.stop(timeout=3.0)
print("LIVE_SUBPROCESS_OK", received[-1].capture_backend, received[-1].timings_ms)
"""
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert proc.returncode == 0, (
        f"Live Qt+HexVisionWorker dxcam+WinRT subprocess failed:\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    assert "LIVE_SUBPROCESS_OK dxcam" in proc.stdout

    # 2. Direct in-process HexVisionWorker run (even if comtypes/dxcam were touched earlier in pytest)
    outputs: list[VisionFrameOutput] = []
    got_out = threading.Event()

    def _on_output(out: VisionFrameOutput) -> None:
        outputs.append(out)
        got_out.set()

    worker = HexVisionWorker(
        on_output=_on_output,
        prefer_dxcam=True,
        initial_augments=[AugmentInfo("1001", "泰坦的坚决", "prismatic")],
        find_window_fn=get_primary_screen_rect,
        is_foreground_fn=lambda _h: True,
        get_cursor_pos_fn=lambda: (100, 100),
    )
    try:
        worker.start()
        worker.update_context(VisionTickContext(vision_gen=2, game_state="IN_GAME", hero_id="98"))
        worker.enqueue_command("force_rescan")
        assert got_out.wait(timeout=8.0) is True
        out = outputs[-1]
        assert out.error == ""
        assert out.capture_backend == "dxcam"
        assert worker._capturer is not None and worker._capturer.backend == "dxcam"
        assert worker._ocr_engine is not None
        assert out.is_black is False
    finally:
        worker.stop(timeout=3.0)
