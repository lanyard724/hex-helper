"""Tests for v1.5.2 WinRT OCR TW->CN fallback state machine, live OCR recall >=96%, preflight & worker hot-reload."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytest

from hex_helper.config import NormalizedRect
from hex_helper.data.locale_pack import build_ocr_catalog, get_locale_catalog
from hex_helper.data.provider import AugmentInfo, DatasetDescriptor
from hex_helper.setup import preflight
from hex_helper.vision import ocr_winrt
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.detector import AugmentPickerDetector
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import WinRtOcrEngine
from hex_helper.vision.transmute import TransmuteRevealWatcher
from hex_helper.vision.worker import HexVisionWorker, VisionTickContext

LIVE_1623_NORMAL = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_1623_NORMAL",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal",
    )
)


def _find_cn_font(size: int) -> ImageFont.FreeTypeFont | None:
    for candidate in (
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return None


def test_winrt_ocr_fallback_state_machine_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeLanguage:
        def __init__(self, tag: str) -> None:
            self.language_tag = tag

    class _FakeInnerEngine:
        def __init__(self, tag: str) -> None:
            self.recognizer_language = _FakeLanguage(tag)

    class _FakeOcrRecognizer:
        supported: set[str] = {"zh-Hans-CN"}
        max_image_dimension = 2600

        @classmethod
        def is_language_supported(cls, lang: _FakeLanguage) -> bool:
            return lang.language_tag in cls.supported

        @classmethod
        def try_create_from_language(cls, lang: _FakeLanguage):
            if lang.language_tag in cls.supported:
                return _FakeInnerEngine(lang.language_tag)
            return None

        @classmethod
        def try_create_from_user_profile_languages(cls):
            return _FakeInnerEngine("zh-Hans-CN")

    import winrt.windows.globalization as winrt_glob
    import winrt.windows.media.ocr as winrt_ocr

    monkeypatch.setattr(winrt_glob, "Language", _FakeLanguage)
    monkeypatch.setattr(winrt_ocr, "OcrEngine", _FakeOcrRecognizer)

    # Case 1: Native zh-Hant-TW installed
    _FakeOcrRecognizer.supported = {"zh-Hant-TW", "zh-Hans-CN"}
    eng_native_tw = WinRtOcrEngine("zh-Hant-TW")
    assert eng_native_tw.requested_language_tag == "zh-Hant-TW"
    assert eng_native_tw.actual_language_tag == "zh-Hant-TW"
    assert eng_native_tw.effective_ocr_mode == "native_tw"
    assert eng_native_tw.needs_t2s_matching is False

    # Case 2: Mainland Windows without zh-Hant-TW, with zh-Hans-CN
    _FakeOcrRecognizer.supported = {"zh-Hans-CN"}
    eng_fb = WinRtOcrEngine("zh-Hant-TW")
    assert eng_fb.requested_language_tag == "zh-Hant-TW"
    assert eng_fb.actual_language_tag == "zh-Hans-CN"
    assert eng_fb.effective_ocr_mode == "sc_t2s_fallback"
    assert eng_fb.needs_t2s_matching is True

    # zh_my/zh_cn on zh-Hans-CN needs no T2S
    eng_cn = WinRtOcrEngine("zh-Hans-CN")
    assert eng_cn.effective_ocr_mode == "native_cn"
    assert eng_cn.needs_t2s_matching is False

    # Case 3: Neither explicit pack supported -> profile fallback
    _FakeOcrRecognizer.supported = set()
    eng_prof = WinRtOcrEngine("zh-Hant-TW")
    assert eng_prof.effective_ocr_mode == "profile_fallback"
    assert eng_prof.needs_t2s_matching is True


def test_preflight_locale_readiness_non_blocking_and_fod_tag_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight,
        "check_winrt_ocr",
        lambda tag="zh-Hans-CN": (tag.lower() == "zh-hans-cn", f"mock:{tag}"),
    )
    cn_status = preflight.check_locale_ocr_readiness("zh_cn")
    my_status = preflight.check_locale_ocr_readiness("zh_my")
    tw_status = preflight.check_locale_ocr_readiness("zh_tw")
    assert cn_status["ok"] is True and cn_status["mode"] == "native_cn"
    assert my_status["ok"] is True and my_status["mode"] == "native_cn"
    assert tw_status["ok"] is True
    assert tw_status["mode"] == "sc_t2s_fallback"
    assert tw_status["native_installed"] is False
    assert tw_status["can_install_tw"] is True

    # Native zh-Hant-TW installed clears can_install_tw
    monkeypatch.setattr(preflight, "check_winrt_ocr", lambda _tag="zh-Hans-CN": (True, "ok"))
    tw_native = preflight.check_locale_ocr_readiness("zh_tw")
    assert tw_native["ok"] is True
    assert tw_native["mode"] == "native_tw"
    assert tw_native["can_install_tw"] is False

    # Verify WinRT BCP-47 tag maps to DISM/FoD capability tag (zh-TW / zh-CN)
    assert preflight._normalize_ocr_capability_lang("zh-Hant-TW")[1] == "zh-TW"
    assert preflight._normalize_ocr_capability_lang("zh-Hans-CN")[1] == "zh-CN"

    captured: list[str] = []
    monkeypatch.setattr(preflight.os, "name", "nt")
    monkeypatch.setattr(
        preflight.ctypes.windll.shell32,
        "ShellExecuteW",
        lambda _hwnd, _op, _file, params, _dir, _show: (captured.append(str(params)), 42)[1],
    )
    ok, _msg = preflight.trigger_install_ocr_pack("zh-Hant-TW")
    assert ok is True
    assert any("Language.OCR~~~zh-TW~0.0.1.0" in p for p in captured)


def test_worker_set_locale_hot_reloads_components_without_restart() -> None:
    created_tags: list[str] = []

    class _FakeOcr:
        def __init__(self, lang_tag: str) -> None:
            created_tags.append(lang_tag)
            self.language_tag = lang_tag
            self.requested_language_tag = lang_tag
            self.actual_language_tag = "zh-Hans-CN"
            self.effective_ocr_mode = "sc_t2s_fallback" if lang_tag == "zh-Hant-TW" else "native_cn"
            self.needs_t2s_matching = lang_tag == "zh-Hant-TW"
            self.closed = False

        def recognize_lines(self, *_a: Any, **_kw: Any) -> list[Any]:
            return []

        def close(self) -> None:
            self.closed = True

    import hex_helper.vision.worker as worker_mod

    old_ocr = worker_mod.WinRtOcrEngine
    worker_mod.WinRtOcrEngine = _FakeOcr  # type: ignore[assignment]
    try:
        augs_cn = [AugmentInfo("1048", "珠光护手", "prismatic")]
        augs_tw = [AugmentInfo("1048", "珠光護手", "prismatic")]
        worker = HexVisionWorker(
            client_locale="zh_cn",
            initial_augments=augs_cn,
            card_band_roi=NormalizedRect(0.1, 0.2, 0.8, 0.5),
            title_band_roi=NormalizedRect(0.1, 0.35, 0.8, 0.1),
        )
        worker._ensure_ocr_and_detector(VisionTickContext())
        assert worker._ocr_engine is not None
        assert worker._detector is not None
        assert worker._transmute_watcher is not None
        assert worker._detector.client_locale == "zh_cn"
        assert worker._detector.matcher.use_t2s is False

        first_eng = worker._ocr_engine
        worker._handle_command("set_locale", {"client_locale": "zh_tw", "augments": augs_tw})
        assert first_eng.closed is True
        assert created_tags == ["zh-Hans-CN", "zh-Hant-TW"]
        assert worker._client_locale == "zh_tw"
        assert worker._detector.client_locale == "zh_tw"
        assert worker._detector.matcher.use_t2s is True
        assert worker._transmute_watcher._client_locale == "zh_tw"
        assert worker._transmute_watcher._use_t2s is True
        assert worker._detector.matcher.match_line("珠光护手").augment.augment_id == "1048"
    finally:
        worker_mod.WinRtOcrEngine = old_ocr  # type: ignore[assignment]


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_real_winrt_sc_ocr_overseas_recall_at_least_96_percent(tmp_path: Path) -> None:
    font = _find_cn_font(34)
    if font is None:
        pytest.skip("Chinese TrueType font missing")
    ok_cn, _ = preflight.check_winrt_ocr("zh-Hans-CN")
    if not ok_cn:
        pytest.skip("WinRT zh-Hans-CN language pack unavailable")

    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.data.store import DataStore

    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda _u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )
    active_augs = store.all_augments()
    assert len(active_augs) == 237

    eng = WinRtOcrEngine("zh-Hans-CN")
    try:
        for loc in ("zh_my", "zh_tw"):
            loc_augs = build_ocr_catalog(active_augs, loc)
            matcher = AugmentMatcher(loc_augs, use_t2s=(loc == "zh_tw"))
            matched_cnt = 0
            recovered_by_id: dict[str, str] = {}

            # Render 3 titles per strip to keep WinRT OCR batch fast while closely matching real card strips
            for idx in range(0, len(loc_augs), 3):
                batch = loc_augs[idx : idx + 3]
                W, H = 1600, 120
                img = Image.new("RGB", (W, H), (16, 22, 34))
                draw = ImageDraw.Draw(img)
                slot_xs = [280, 800, 1320]
                for aug, cx in zip(batch, slot_xs):
                    title = aug.name
                    bbox = draw.textbbox((0, 0), title, font=font)
                    tw = bbox[2] - bbox[0]
                    draw.text((cx - tw // 2, 34), title, fill=(246, 240, 224), font=font)
                bgr = cv2.cvtColor(np.asarray(img, dtype=np.uint8), cv2.COLOR_RGB2BGR)
                lines = eng.recognize_lines(bgr, dual_pass=True)

                for aug, cx in zip(batch, slot_xs):
                    slot_lines = [ln for ln in lines if abs(ln.cx - cx) <= 230]
                    best = None
                    for ln in slot_lines:
                        res = matcher.match_line(ln.text, rarity_hint=aug.rarity, min_score=0.72)
                        if res is not None and (best is None or res.score > best.score):
                            best = res
                    if best is not None and best.augment.augment_id == aug.augment_id:
                        matched_cnt += 1
                        recovered_by_id[aug.augment_id] = best.augment.augment_id

            recall = matched_cnt / float(len(loc_augs))
            assert recall >= 0.96, f"{loc} WinRT zh-Hans-CN OCR recall too low: {matched_cnt}/{len(loc_augs)} ({recall:.3%})"
            key_ids = ("1058", "1063", "1349", "1384", "1238") if loc == "zh_tw" else ("1058", "1063", "1349", "1238")
            for key_id in key_ids:
                assert recovered_by_id.get(key_id) == key_id, f"{loc} failed key augment {key_id}"
    finally:
        eng.close()
