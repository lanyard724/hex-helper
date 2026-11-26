"""Windows native WinRT zh-Hans-CN OCR wrapper with CLAHE/contrast preprocessing."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OcrLine:
    text: str
    x: float  # left in input image pixels
    y: float  # top in input image pixels
    w: float
    h: float
    pass_id: int = 1

    @property
    def cx(self) -> float:
        return self.x + self.w * 0.5

    @property
    def cy(self) -> float:
        return self.y + self.h * 0.5


class WinRtOcrEngine:
    """Zero-model-footprint Windows native zh-Hans-CN OCR engine."""

    def __init__(self, language_tag: str = "zh-Hans-CN") -> None:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        self.language_tag = language_tag
        self.actual_language_tag: str = ""
        self.last_shadowed_pass2: list[dict[str, Any]] = []
        self.last_pass_timings_ms: dict[str, float] = {"ocr_p1_ms": 0.0, "ocr_p2_ms": 0.0}
        lang = Language(language_tag)
        self._engine = OcrEngine.try_create_from_language(lang)
        if self._engine is None:
            self._engine = OcrEngine.try_create_from_user_profile_languages()
        if self._engine is None:
            raise RuntimeError(f"Windows WinRT OCR unavailable for {language_tag}")
        try:
            rec_lang = getattr(self._engine, "recognizer_language", None)
            tag = getattr(rec_lang, "language_tag", None) if rec_lang is not None else None
            self.actual_language_tag = str(tag or "")
        except Exception:
            self.actual_language_tag = ""
        self._max_dim = int(OcrEngine.max_image_dimension or 2600)
        self._loop = asyncio.new_event_loop()

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:
            pass

    async def _recognize_bgra_async(self, bgra: np.ndarray, pass_id: int = 1) -> list[OcrLine]:
        from winrt.windows.graphics.imaging import (
            BitmapAlphaMode,
            BitmapPixelFormat,
            SoftwareBitmap,
        )
        from winrt.windows.storage.streams import DataWriter

        h, w = bgra.shape[:2]
        writer = DataWriter()
        writer.write_bytes(np.ascontiguousarray(bgra).tobytes())
        buf = writer.detach_buffer()
        sb = SoftwareBitmap.create_copy_from_buffer(
            buf,
            BitmapPixelFormat.BGRA8,
            int(w),
            int(h),
            BitmapAlphaMode.PREMULTIPLIED,
        )
        result = await self._engine.recognize_async(sb)
        lines: list[OcrLine] = []
        for line in result.lines:
            text = (line.text or "").strip()
            if not text:
                continue
            x0, y0, x1, y1 = float("inf"), float("inf"), 0.0, 0.0
            for word in line.words:
                r = word.bounding_rect
                x0 = min(x0, float(r.x))
                y0 = min(y0, float(r.y))
                x1 = max(x1, float(r.x + r.width))
                y1 = max(y1, float(r.y + r.height))
            if x1 <= x0 or y1 <= y0:
                continue
            lines.append(OcrLine(text=text, x=x0, y=y0, w=x1 - x0, h=y1 - y0, pass_id=pass_id))
        return lines

    def _run_bgra(self, bgra: np.ndarray, scale: float, pass_id: int = 1) -> list[OcrLine]:
        raw_lines = self._loop.run_until_complete(self._recognize_bgra_async(bgra, pass_id=pass_id))
        if scale == 1.0:
            return raw_lines
        inv = 1.0 / scale
        return [
            OcrLine(
                text=l.text,
                x=l.x * inv,
                y=l.y * inv,
                w=l.w * inv,
                h=l.h * inv,
                pass_id=l.pass_id,
            )
            for l in raw_lines
        ]

    def _prepare_variants(
        self,
        image_bgr: np.ndarray,
        *,
        need_inverted: bool = True,
    ) -> tuple[float, np.ndarray, np.ndarray | None]:
        h, w = image_bgr.shape[:2]
        # Scale small/medium title bands up for crisp Chinese glyphs while respecting max_dim
        target_h = 340.0
        scale = max(1.0, min(2.0, target_h / max(1, h)))
        if max(w, h) * scale > self._max_dim:
            scale = self._max_dim / max(w, h)

        if abs(scale - 1.0) > 0.02:
            resized = cv2.resize(
                image_bgr,
                (max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                interpolation=cv2.INTER_CUBIC,
            )
        else:
            scale = 1.0
            resized = image_bgr

        # Variant 1: CLAHE enhanced grayscale -> BGRA
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        v1 = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGRA)

        if not need_inverted:
            return scale, v1, None

        # Variant 2: Inverted dark-text-on-bright-background (helps white/gold text on dark Hextech cards)
        inv = 255 - enhanced
        v2 = cv2.cvtColor(inv, cv2.COLOR_GRAY2BGRA)
        return scale, v1, v2

    def recognize_lines(
        self,
        image_bgr: np.ndarray,
        dual_pass: bool = True,
        force_dual_pass: bool = False,
    ) -> list[OcrLine]:
        """Run WinRT OCR on a BGR crop and return bounding-boxed lines in input pixel coordinates."""
        self.last_shadowed_pass2 = []
        self.last_pass_timings_ms = {"ocr_p1_ms": 0.0, "ocr_p2_ms": 0.0}
        if image_bgr is None or image_bgr.size == 0:
            return []
        allow_p2 = bool(dual_pass or force_dual_pass)
        scale, v1, v2 = self._prepare_variants(image_bgr, need_inverted=allow_p2)
        t_p1 = time.perf_counter()
        lines1 = self._run_bgra(v1, scale, pass_id=1)
        self.last_pass_timings_ms["ocr_p1_ms"] = round((time.perf_counter() - t_p1) * 1000.0, 3)
        if not force_dual_pass and (not dual_pass or len(lines1) >= 3):
            return lines1
        if v2 is None:
            return lines1

        # Second pass on inverted image for glowing white-on-dark titles; merge deduplicated lines
        t_p2 = time.perf_counter()
        lines2 = self._run_bgra(v2, scale, pass_id=2)
        self.last_pass_timings_ms["ocr_p2_ms"] = round((time.perf_counter() - t_p2) * 1000.0, 3)
        merged = list(lines1)
        shadowed: list[dict[str, Any]] = []
        for cand in lines2:
            keeper = next(
                (ex for ex in merged if abs(cand.cx - ex.cx) < 40 and abs(cand.cy - ex.cy) < 24),
                None,
            )
            if keeper is None:
                merged.append(cand)
            elif keeper.pass_id == 1:
                k_compact = "".join(keeper.text.split())
                c_compact = "".join(cand.text.split())
                if c_compact and c_compact != k_compact:
                    shadowed.append(
                        {
                            "kept_pass1_text": keeper.text,
                            "dropped_pass2_text": cand.text,
                            "kept_bbox": [
                                round(keeper.x, 1),
                                round(keeper.y, 1),
                                round(keeper.w, 1),
                                round(keeper.h, 1),
                            ],
                            "dropped_bbox": [
                                round(cand.x, 1),
                                round(cand.y, 1),
                                round(cand.w, 1),
                                round(cand.h, 1),
                            ],
                            "dx": round(abs(cand.cx - keeper.cx), 1),
                            "dy": round(abs(cand.cy - keeper.cy), 1),
                        }
                    )
        self.last_shadowed_pass2 = shadowed
        return merged

    def recognize_bgr(
        self,
        image_bgr: np.ndarray,
        dual_pass: bool = True,
        force_dual_pass: bool = False,
    ) -> list[OcrLine]:
        """Alias of `recognize_lines` accepting a BGR image crop."""
        return self.recognize_lines(image_bgr, dual_pass=dual_pass, force_dual_pass=force_dual_pass)
