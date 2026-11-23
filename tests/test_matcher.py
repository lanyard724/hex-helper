from hex_helper.data.provider import AugmentInfo
from hex_helper.vision.detector import filter_excluded_lines
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import OcrLine


def _sample_catalog() -> list[AugmentInfo]:
    return [
        AugmentInfo("1001", "泰坦的坚决", "prismatic"),
        AugmentInfo("1002", "尖端发明家", "gold"),
        AugmentInfo("1004", "回归基本功", "gold"),
        AugmentInfo("3081", "命运之武器", "gold"),
        AugmentInfo("3131", "屹立不倒", "gold"),
        AugmentInfo("3058", "嗡嗡怪兽", "gold"),
        AugmentInfo("2119", "过量延伸者", "gold"),
        AugmentInfo("1205", "物理转魔法", "silver"),
    ]


def test_matcher_exact_and_noisy_ocr():
    m = AugmentMatcher(_sample_catalog())

    r1 = m.match_line("命运之武器")
    assert r1 is not None and r1.augment.augment_id == "3081" and r1.score == 1.0

    # Noisy character substitutions & punctuation
    r2 = m.match_line("太坦的坚决!")
    assert r2 is not None and r2.augment.augment_id == "1001"

    r3 = m.match_line("回归基木功", rarity_hint="gold")
    assert r3 is not None and r3.augment.augment_id == "1004"

    # Garbage should not match
    assert m.match_line("123456") is None
    assert m.match_line("完全无关的一句话") is None


def test_overlay_exclusion_drops_self_rendered_lines():
    lines = [
        OcrLine("命运之武器", x=500, y=20, w=120, h=30),   # inside excluded overlay rect
        OcrLine("命运之武器", x=500, y=180, w=120, h=30),  # real game card below overlay
    ]
    kept = filter_excluded_lines(
        lines,
        band_origin_x=300,
        band_origin_y=400,
        exclude_rects=[(750, 400, 980, 480)],
    )
    assert len(kept) == 1
    assert kept[0].y == 180
