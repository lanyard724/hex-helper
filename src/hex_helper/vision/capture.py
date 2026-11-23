"""DXGI (dxcam) + MSS screen capture with black-frame assertion and game window geometry."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    width: int
    height: int
    hwnd: int = 0

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_bbox(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


@dataclass(frozen=True)
class CaptureResult:
    frame_bgr: np.ndarray | None
    backend: str
    is_black: bool
    mean_luma: float


def attach_default_desktop() -> bool:
    """Ensure current thread is attached to WinSta0\\Default when running from isolated subshells."""
    try:
        h_desk = _user32.OpenDesktopW("Default", 0, False, 0x10000000)
        if h_desk:
            _user32.SetThreadDesktop(h_desk)
            return True
    except Exception:
        pass
    return False


def get_primary_screen_rect() -> WindowRect:
    w = int(_user32.GetSystemMetrics(0))
    h = int(_user32.GetSystemMetrics(1))
    return WindowRect(0, 0, max(1, w), max(1, h), 0)


def _client_rect_screen(hwnd: int) -> WindowRect | None:
    if not hwnd or not _user32.IsWindowVisible(hwnd):
        return None
    rect = _RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    w = int(rect.right - rect.left)
    h = int(rect.bottom - rect.top)
    if w < 320 or h < 200:
        return None
    pt = _POINT(0, 0)
    if not _user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return None
    return WindowRect(int(pt.x), int(pt.y), w, h, int(hwnd))


def find_game_window() -> WindowRect | None:
    """Find the active League of Legends in-game window client rect."""
    for title in ("League of Legends (TM) Client", "League of Legends"):
        hwnd = _user32.FindWindowW("RiotWindowClass", title)
        if not hwnd:
            hwnd = _user32.FindWindowW(None, title)
        rect = _client_rect_screen(hwnd)
        if rect is not None:
            return rect
    return None


def find_lcu_window() -> WindowRect | None:
    """Find the League Client lobby/champ-select window rect for overlay docking."""
    for title in ("League of Legends", "英雄联盟"):
        hwnd = _user32.FindWindowW("RCLIENT", title)
        if not hwnd:
            hwnd = _user32.FindWindowW(None, title)
        rect = _client_rect_screen(hwnd)
        if rect is not None:
            return rect
    return None


def is_hwnd_foreground(hwnd: int) -> bool:
    if not hwnd:
        return False
    fg = int(_user32.GetForegroundWindow())
    return fg == int(hwnd)


def is_black_frame(frame_bgr: np.ndarray | None) -> tuple[bool, float]:
    """Assert non-empty, non-black frame per DirectX swapchain reality check."""
    if frame_bgr is None or frame_bgr.size == 0:
        return True, 0.0
    max_val = int(np.max(frame_bgr))
    mean_val = float(np.mean(frame_bgr))
    return (max_val <= 0 or mean_val <= 1.0), mean_val


class ScreenCapturer:
    """DXGI Desktop Duplication (dxcam) primary screen capturer with MSS fallback (lazy-initialized)."""

    def __init__(self, prefer_dxcam: bool = True, lazy: bool = True) -> None:
        self._prefer_dxcam = prefer_dxcam
        self._dx_cam = None
        self._mss = None
        self._backend = "lazy"
        self._initialized = False
        self._consecutive_black = 0
        if not lazy:
            self._init_backends()

    def _init_backends(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        if self._prefer_dxcam:
            try:
                import dxcam

                self._dx_cam = dxcam.create(output_color="BGR")
                if self._dx_cam is not None:
                    self._backend = "dxcam"
                    return
            except Exception as exc:
                logger.debug("dxcam init unavailable, falling back to mss: %s", exc)
                self._dx_cam = None
        self._ensure_mss()

    def _ensure_mss(self) -> None:
        if self._mss is None:
            import mss

            self._mss = mss.mss()
        self._backend = "mss"

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def consecutive_black_frames(self) -> int:
        return self._consecutive_black

    def grab(self, region: tuple[int, int, int, int] | None = None) -> CaptureResult:
        """Capture region=(left, top, right, bottom) as BGR ndarray with black-frame validation."""
        if not self._initialized:
            self._init_backends()
        frame: np.ndarray | None = None
        used = self._backend

        if self._backend == "dxcam" and self._dx_cam is not None:
            try:
                frame = self._dx_cam.grab(region=region)
                # dxcam returns None when desktop has not changed since last frame -> fall back to mss for static frame
                if frame is None:
                    self._ensure_mss()
                    frame = self._grab_mss(region)
                    used = "mss"
            except Exception as exc:
                logger.debug("dxcam grab failed, switching to mss: %s", exc)
                self._dx_cam = None
                self._ensure_mss()
                frame = self._grab_mss(region)
                used = "mss"
        else:
            self._ensure_mss()
            frame = self._grab_mss(region)
            used = "mss"

        black, mean_luma = is_black_frame(frame)
        if black:
            self._consecutive_black += 1
            # Auto-switch backend after consecutive black frames
            if self._consecutive_black >= 3 and used == "dxcam":
                self._ensure_mss()
                frame2 = self._grab_mss(region)
                black2, mean2 = is_black_frame(frame2)
                if not black2:
                    self._consecutive_black = 0
                    return CaptureResult(frame2, "mss", False, mean2)
        else:
            self._consecutive_black = 0

        return CaptureResult(frame, used, black, mean_luma)

    def _grab_mss(self, region: tuple[int, int, int, int] | None) -> np.ndarray | None:
        if self._mss is None:
            return None
        if region is None:
            mon = self._mss.monitors[1]
        else:
            l, t, r, b = region
            mon = {"left": int(l), "top": int(t), "width": max(1, int(r - l)), "height": max(1, int(b - t))}
        raw = self._mss.grab(mon)
        arr = np.asarray(raw, dtype=np.uint8)
        if arr.ndim == 3 and arr.shape[2] == 4:
            return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        return arr

    def close(self) -> None:
        if self._dx_cam is not None:
            try:
                del self._dx_cam
            except Exception:
                pass
            self._dx_cam = None
        if self._mss is not None:
            try:
                self._mss.close()
            except Exception:
                pass
            self._mss = None
