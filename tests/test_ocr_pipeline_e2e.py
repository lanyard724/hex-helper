import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hex_helper.config import NormalizedRect
from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.detector import AugmentPickerDetector
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import WinRtOcrEngine


def _find_cn_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    raise RuntimeError("No Chinese TrueType font found in C:\\Windows\\Fonts")


def _render_mock_mayhem_2k(card_titles: tuple[str, str, str], border_rgb: tuple[int, int, int] = (210, 165, 65)) -> np.ndarray:
    """Synthesize a realistic 2560x1440 ARAM Mayhem 3-card augment selection screen."""
    W, H = 2560, 1440
    img = Image.new("RGB", (W, H), (12, 18, 28))
    draw = ImageDraw.Draw(img)
    title_font = _find_cn_font(38)
    desc_font = _find_cn_font(22)

    card_centers = [780, 1280, 1780]
    card_w, card_h = 410, 560
    top_y = 380

    for cx, title in zip(card_centers, card_titles):
        left = cx - card_w // 2
        right = cx + card_w // 2
        bottom = top_y + card_h
        # Card dark interior + thick rarity border
        draw.rectangle([left, top_y, right, bottom], fill=(20, 28, 44), outline=border_rgb, width=8)
        # Header ornament bar
        draw.rectangle([left + 12, top_y + 12, right - 12, top_y + 52], fill=border_rgb)
        # Centered augment title inside TITLE_BAND (y ~ 570..640 -> ~0.40..0.44 of 1440)
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
        draw.text((cx - tw // 2, 585), title, fill=(246, 240, 224), font=title_font)
        draw.text((left + 32, 680), "获得一项强化效果", fill=(160, 175, 195), font=desc_font)

    rgb = np.asarray(img, dtype=np.uint8)
    # Add light Gaussian noise to simulate game rendering / bloom
    rng = np.random.default_rng(42)
    noise = rng.integers(-6, 7, size=rgb.shape, dtype=np.int16)
    noisy = np.clip(rgb.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return cv2.cvtColor(noisy, cv2.COLOR_RGB2BGR)


def test_real_winrt_ocr_2k_3card_and_reroll():
    catalog = [
        AugmentInfo("3058", "嗡嗡怪兽", "gold"),
        AugmentInfo("3081", "命运之武器", "gold"),
        AugmentInfo("3131", "屹立不倒", "gold"),
        AugmentInfo("2119", "过量延伸者", "gold"),
        AugmentInfo("1001", "泰坦的坚决", "prismatic"),
        AugmentInfo("1002", "尖端发明家", "gold"),
        AugmentInfo("1205", "物理转魔法", "silver"),
    ]
    ocr = WinRtOcrEngine("zh-Hans-CN")
    try:
        matcher = AugmentMatcher(catalog)
        detector = AugmentPickerDetector(
            ocr_engine=ocr,
            matcher=matcher,
            card_band_roi=NormalizedRect(0.14, 0.24, 0.72, 0.48),
            title_band_roi=NormalizedRect(0.14, 0.31, 0.72, 0.25),
        )
        game_rect = WindowRect(0, 0, 2560, 1440, 0)
        frame1 = _render_mock_mayhem_2k(("嗡嗡怪兽", "命运之武器", "屹立不倒"))

        t0 = time.perf_counter()
        res1 = detector.process_frame(frame1, game_rect, force=True)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        assert res1.picker_visible is True
        assert res1.stable is True
        assert len(res1.cards) == 3
        assert [c.augment.augment_id for c in res1.cards] == ["3058", "3081", "3131"]
        assert all(c.rarity_hint == "gold" for c in res1.cards)
        assert elapsed_ms < 1500.0

        # Reroll left card -> dHash changes and detector immediately reads the new card
        frame2 = _render_mock_mayhem_2k(("过量延伸者", "命运之武器", "屹立不倒"))
        res2 = detector.process_frame(frame2, game_rect, force=False)
        assert res2.picker_visible is True
        assert [c.augment.augment_id for c in res2.cards] == ["2119", "3081", "3131"]
    finally:
        ocr.close()
