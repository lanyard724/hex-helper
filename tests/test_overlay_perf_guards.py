"""Performance regression guards preventing overlay-induced FPS drops on Windows DWM / DirectFlip."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QObject, QVariantAnimation
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from hex_helper.data.provider import AugmentInfo, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import MatchBuildProfile, PickerCardComboAdvice, PickerComboVerdict
from hex_helper.scoring.matchup_optimizer import DynamicItemRec, NextBuyPlan
from hex_helper.scoring.tier import AugmentScore
from hex_helper.ui import win32_window
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.hextech_widgets import HextechGoldRingIcon, HextechShimmerBar
from hex_helper.vision.capture import ScreenCapturer


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _EventCounter(QObject):
    def __init__(self, target_types: set[QEvent.Type]) -> None:
        super().__init__()
        self._types = target_types
        self.count = 0

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802
        if event.type() in self._types:
            self.count += 1
        return False


def test_screen_capturer_preserves_dxcam_on_none_and_reuses_cached_frame() -> None:
    full_frame = np.full((1080, 1920, 3), 96, dtype=np.uint8)
    full_frame[300:420, 300:1500] = 180
    fresh_frame = np.full((1080, 1920, 3), 120, dtype=np.uint8)

    class _FakeDxCam:
        width = 1920
        height = 1080

        def __init__(self) -> None:
            self.seq = [full_frame, None, None, fresh_frame]
            self.calls = 0

        def grab(self, region=None):
            idx = min(self.calls, len(self.seq) - 1)
            self.calls += 1
            return self.seq[idx]

    cap = ScreenCapturer(prefer_dxcam=True, lazy=True)
    cap._initialized = True
    cap._dx_cam = _FakeDxCam()
    cap._backend = "dxcam"

    mss_calls = 0

    def _fail_if_mss(_region):
        nonlocal mss_calls
        mss_calls += 1
        return full_frame

    cap._ensure_mss = lambda *, set_active=True: None  # type: ignore[method-assign]
    cap._grab_mss = _fail_if_mss  # type: ignore[method-assign]

    r1 = cap.grab(region=(0, 0, 1920, 1080))
    assert r1.backend == "dxcam" and not r1.is_black and r1.frame_bgr.shape == (1080, 1920, 3)

    # Desktop unchanged -> dxcam returns None -> MUST stay on dxcam & reuse cached full frame (zero GDI!)
    r2 = cap.grab(region=(0, 0, 1920, 1080))
    assert cap.backend == "dxcam" and r2.backend == "dxcam"
    assert r2.frame_bgr is full_frame

    # Sub-region band grab when desktop unchanged -> MUST slice cached full frame (zero GDI!)
    r3 = cap.grab(region=(300, 300, 1500, 420))
    assert cap.backend == "dxcam" and r3.backend == "dxcam"
    assert r3.frame_bgr is not None and r3.frame_bgr.shape == (120, 1200, 3)
    assert int(r3.frame_bgr[10, 10, 0]) == 180

    # Next GPU flip -> fresh dxcam frame
    r4 = cap.grab(region=(0, 0, 1920, 1080))
    assert cap.backend == "dxcam" and r4.frame_bgr is fresh_frame
    assert mss_calls == 0
    assert cap.cached_frame_hits == 2


def test_screen_capturer_recovers_dxcam_after_transient_access_lost() -> None:
    fallback_frame = np.full((720, 1280, 3), 80, dtype=np.uint8)
    recovered_frame = np.full((720, 1280, 3), 140, dtype=np.uint8)

    class _BrokenOnceCam:
        width = 1280
        height = 720

        def grab(self, region=None):
            raise RuntimeError("DXGI_ERROR_ACCESS_LOST")

    class _RecoveredCam:
        width = 1280
        height = 720

        def grab(self, region=None):
            return recovered_frame

    cap = ScreenCapturer(prefer_dxcam=True, lazy=True)
    cap._initialized = True
    cap._dx_cam = _BrokenOnceCam()
    cap._backend = "dxcam"
    cap._ensure_mss = lambda *, set_active=True: setattr(cap, "_backend", "mss") if set_active else None  # type: ignore[method-assign]
    cap._grab_mss = lambda _region: fallback_frame  # type: ignore[method-assign]

    # Single Alt-Tab ACCESS_LOST falls back for 1 frame ONLY without permanently killing dxcam
    r1 = cap.grab(region=(0, 0, 1280, 720))
    assert r1.backend == "mss" and not r1.is_black
    assert cap.backend == "dxcam"
    assert cap._dx_cam is None

    # After cooldown expires, dxcam auto-recovers cleanly
    def _recreate() -> bool:
        cap._dx_cam = _RecoveredCam()
        cap._backend = "dxcam"
        cap._dx_fail_count = 0
        return True

    cap._try_create_dxcam = _recreate  # type: ignore[method-assign]
    cap._dx_retry_after_mono = 0.0
    r2 = cap.grab(region=(0, 0, 1280, 720))
    assert r2.backend == "dxcam" and r2.frame_bgr is recovered_frame


def test_augment_overlay_tight_bbox_dedup_and_zero_idle_paints() -> None:
    app = _qt_app()
    ov = AugmentBadgesOverlay()
    items = [
        AugmentBadgeRenderItem(
            slot_index=i,
            score=AugmentScore(str(1000 + i), f"符文{i}", "gold", "S", 0.55, 0.54, 0.03, 12000, False, percentile=90.0),
            screen_box=(400 + i * 750, 420, 660 + i * 750, 460),
            is_best=(i == 1),
        )
        for i in range(3)
    ]

    ov.render_cards(items, screen_rect=(0, 0, 2560, 1440), coach_pending=True)
    app.processEvents()

    # 1. Tight-cropped layered surface (<25% of full screen instead of 100% 14.06MB fullscreen!)
    full_area = 2560 * 1440
    ov_area = ov.width() * ov.height()
    assert 0 < ov_area < full_area * 0.25, f"AugmentBadgesOverlay not tight-cropped: {ov.width()}x{ov.height()}"
    assert len(ov.get_exclude_rects()) == 4  # 3 cards + 1 banner

    # 2. Repeat render_cards with identical signature MUST no-op (zero widget destruction/recreation!)
    badge_ids_before = [id(b) for b in ov._badges]
    banner_id_before = id(ov._coach_banner)
    ov.render_cards(items, screen_rect=(0, 0, 2560, 1440), coach_pending=True)
    assert [id(b) for b in ov._badges] == badge_ids_before
    assert id(ov._coach_banner) == banner_id_before

    # 3. Switching layouts detaches old cards immediately (zero deleteLater ghost cards!)
    ov.render_cards(items[:2], screen_rect=(0, 0, 2560, 1440), coach_augment_tip="首选测试")
    from PySide6.QtWidgets import QFrame as _QFrame

    assert len(ov.findChildren(_QFrame, "PanelRoot")) == 2

    # 4. Zero 60fps UpdateLayeredWindow paint floods while AI thinking banner is displayed
    ov.render_cards(items, screen_rect=(0, 0, 2560, 1440), coach_pending=True)
    for _ in range(4):
        app.processEvents()
        time.sleep(0.01)
    paint_counter = _EventCounter({QEvent.Type.Paint})
    ov.installEventFilter(paint_counter)
    t_end = time.perf_counter() + 0.30
    while time.perf_counter() < t_end:
        app.processEvents()
        time.sleep(0.01)
    assert paint_counter.count == 0, f"Expected 0 idle/shimmer paints on AugmentBadgesOverlay, got {paint_counter.count}"

    ov.removeEventFilter(paint_counter)
    ov.close()
    app.processEvents()


def test_edge_drawer_instant_expand_and_zero_idle_ring_paints() -> None:
    app = _qt_app()
    with tempfile.TemporaryDirectory() as td:
        static = StaticAssetsManager(Path(td), fetcher=lambda _url: b"")
        static.items["3089"] = ItemStaticInfo(item_id="3089", name="灭世者的死亡之帽", gold=3500, category="complete_equipment")
        store = DataStore(static)
        store._heroes["63"] = HeroStat("63", "布兰德", "Brand", 10000, 5200, 0.52, 0.05, 10)
        store._augments["3163"] = AugmentInfo("3163", "迷人蝴蝶", "gold")

        drawer = EdgeBuildDrawer()
        target = DynamicItemRec("3089", "灭世者的死亡之帽", 3500, True, 0, 10.0, 0.54, 1000, ("核心",), ())
        plan = NextBuyPlan(target, 3500, True, 0, "可直接合成死亡之帽", ("灭世者的死亡之帽",), ("3089",))
        drawer.show()
        drawer.refresh_match_view(store, "63", ["3163"], (), 3600.0, None, [target], next_plan=plan)
        app.processEvents()

        # 1. Affordable gold ring MUST NOT spin an infinite 60fps breathing loop
        assert drawer.pill_item_ring.ready is True
        assert drawer.pill_item_ring._breath_anim.state() != QVariantAnimation.State.Running
        ring_paints = _EventCounter({QEvent.Type.Paint})
        drawer.pill_item_ring.installEventFilter(ring_paints)
        t_end = time.perf_counter() + 0.30
        while time.perf_counter() < t_end:
            app.processEvents()
            time.sleep(0.01)
        assert ring_paints.count == 0, f"Expected 0 idle paints on ready gold ring, got {ring_paints.count}"
        drawer.pill_item_ring.removeEventFilter(ring_paints)

        # 2. Expand snaps in 1 frame (<=2 resize events, NOT 14x 60fps slide resizes!)
        resize_counter = _EventCounter({QEvent.Type.Resize})
        drawer.installEventFilter(resize_counter)
        drawer.set_expanded(True)
        t_end = time.perf_counter() + 0.22
        while time.perf_counter() < t_end:
            app.processEvents()
            time.sleep(0.01)
        assert drawer._slide_anim.state() != QVariantAnimation.State.Running
        screen = drawer.screen() or app.primaryScreen()
        geo = screen.geometry()
        expected_w = EdgeBuildDrawer.compute_default_expanded_width(
            geo.width(), geo.height(), max(1.0, float(screen.devicePixelRatio() or 1.0))
        )
        assert drawer.width() == expected_w
        assert resize_counter.count <= 2, f"Expected <=2 resizes on instant expand, got {resize_counter.count}"

        # 3. Repeated 1Hz refresh_match_view with identical state causes 0 window resizes
        resize_counter.count = 0
        for _ in range(10):
            drawer.refresh_match_view(store, "63", ["3163"], (), 3600.0, None, [target], next_plan=plan)
            app.processEvents()
        assert resize_counter.count == 0, f"Unchanged 1Hz ticks caused {resize_counter.count} redundant resizes"

        # 4. Shimmer bar defaults to zero-flush static mode
        shimmer = HextechShimmerBar(height=3)
        shimmer.set_active(True)
        assert shimmer.is_active() is True
        assert shimmer._anim.state() != QVariantAnimation.State.Running

        drawer.removeEventFilter(resize_counter)
        drawer.close()
        app.processEvents()


def test_combo_overlay_picker_dedup_and_instant_expand() -> None:
    app = _qt_app()
    combo = AugmentComboOverlay()
    combo.show()
    app.processEvents()

    prof = MatchBuildProfile("AP", "AP 法术爆发流", ("AP_SPELL",), (), (), ())
    verdict = PickerComboVerdict(
        profile=prof,
        cards=tuple(
            PickerCardComboAdvice(
                slot_index=i,
                augment_id=str(1000 + i),
                name=f"测试符文{i}",
                rarity="gold",
                verdict="synergy" if i == 0 else "neutral",
                verdict_label="组合顺滑" if i == 0 else "常规备选",
                combo_delta=3.5 if i == 0 else 0.0,
                reason="联动测试",
                combo_name="测试流派",
                gamble=False,
            )
            for i in range(3)
        ),
        best_slot_index=0,
        reroll_advice="",
    )

    combo.enter_picker_mode(verdict)
    app.processEvents()
    row_ids = [id(r) for r in combo._picker_rows]
    # Identical 400ms tick MUST NOT destroy & recreate picker rows
    combo.enter_picker_mode(verdict)
    assert [id(r) for r in combo._picker_rows] == row_ids

    # Collapse & expand MUST snap cleanly with zero running slide animation
    combo.set_expanded(False)
    assert combo._slide_anim.state() != QVariantAnimation.State.Running
    combo.set_expanded(True)
    assert combo._slide_anim.state() != QVariantAnimation.State.Running

    combo.close()
    app.processEvents()


def test_show_no_activate_skips_redundant_win32_framechanged(monkeypatch) -> None:
    app = _qt_app()
    w = QWidget()
    w.resize(120, 60)
    win32_window.show_no_activate(w, click_through=True)
    app.processEvents()

    set_long_calls = 0
    set_pos_calls = 0
    real_user32 = win32_window._user32

    class _SpyUser32:
        def GetWindowLongW(self, *args):  # noqa: N802
            return real_user32.GetWindowLongW(*args)

        def SetWindowLongW(self, *args):  # noqa: N802
            nonlocal set_long_calls
            set_long_calls += 1
            return real_user32.SetWindowLongW(*args)

        def SetWindowPos(self, *args):  # noqa: N802
            nonlocal set_pos_calls
            set_pos_calls += 1
            return real_user32.SetWindowPos(*args)

    monkeypatch.setattr(win32_window, "_user32", _SpyUser32())
    for _ in range(5):
        win32_window.show_no_activate(w, click_through=True)

    assert set_long_calls == 0
    assert set_pos_calls == 0

    w.close()
    app.processEvents()
