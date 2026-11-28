"""Comprehensive tests for multi-template match recap cards, interactive switcher dialog, and config persistence."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QRadialGradient
from PySide6.QtWidgets import QApplication, QWidget

from hex_helper import app as app_mod
from hex_helper.config import (
    DEFAULT_RECAP_TEMPLATE as CFG_DEFAULT_TEMPLATE,
    RECAP_TEMPLATES as CFG_RECAP_TEMPLATES,
    AppConfig,
    load_config,
    save_config,
)
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.ui.match_recap_card import (
    CARD_DEFAULT_HEIGHT,
    CARD_DEFAULT_WIDTH,
    DEFAULT_RECAP_TEMPLATE,
    RECAP_TEMPLATE_ORDER,
    RECAP_TEMPLATES,
    MatchRecapData,
    MatchRecapDialog,
    copy_recap_to_clipboard,
    get_recap_template_spec,
    normalize_recap_template,
    render_match_recap_image,
)
from hex_helper.vision.capture import CaptureResult, WindowRect


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_stub_png(path: Path, color: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(48, 48, QImage.Format.Format_ARGB32)
    img.fill(QColor(color))
    assert img.save(str(path), "PNG")
    return str(path)


def _make_splash_jpg(path: Path, width: int = 1215, height: int = 717) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    p = QPainter(img)
    bg = QLinearGradient(0.0, 0.0, float(width), float(height))
    bg.setColorAt(0.0, QColor("#1E1038"))
    bg.setColorAt(0.55, QColor("#5B21B6"))
    bg.setColorAt(1.0, QColor("#0F766E"))
    p.fillRect(img.rect(), bg)
    focal = QRadialGradient(width * 0.62, height * 0.30, width * 0.32)
    focal.setColorAt(0.0, QColor("#FDE68A"))
    focal.setColorAt(0.45, QColor("#A855F7"))
    focal.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.fillRect(img.rect(), focal)
    p.end()
    assert img.save(str(path), "JPG", 92)
    return str(path)


def _sample_data(tmp_path: Path, *, with_splash: bool = True, augment_count: int = 5) -> MatchRecapData:
    aug_pool = (
        ("巫妖大脑", "silver"),
        ("歌利亚巨人", "prismatic"),
        ("泰坦的坚决", "prismatic"),
        ("坦克引擎", "gold"),
        ("史莱姆时间", "silver"),
        ("珠光护手", "prismatic"),
    )
    aug_icons = tuple(_make_stub_png(tmp_path / f"aug_{i}.png", "#C8AA6E") for i in range(6))
    items = tuple(
        (nm, _make_stub_png(tmp_path / f"item_{i}.png", "#3B82F6"))
        for i, nm in enumerate(
            ("裂隙制造者", "瑞莱的冰晶节杖", "兰德里的折磨", "时光之杖", "振奋盔甲", "铁板靴")
        )
    )
    splash = _make_splash_jpg(tmp_path / "82006.jpg") if with_splash else ""
    return MatchRecapData(
        hero_id="82",
        hero_name="莫德凯撒",
        hero_alias="Mordekaiser",
        hero_icon_path=_make_stub_png(tmp_path / "hero_82.png", "#0AC8B9"),
        won=1,
        kda="16/4/22",
        duration_s=1042,
        team_kills="48 : 37",
        is_first_prismatic=True,
        augments=aug_pool[:augment_count],
        augment_icon_paths=aug_icons[:augment_count],
        combo_title="巫妖血怒法坦 · 歌利亚巨人",
        combo_completed=True,
        item_entries=items,
        final_stats={
            "ad": 195.0,
            "ap": 886.0,
            "hp": 9640.0,
            "ah": 135.0,
            "armor": 248.0,
            "mr": 186.0,
            "as": 1.38,
            "ms": 435.0,
        },
        damage_dealt=78400,
        damage_share=0.35,
        damage_taken=92100,
        heal_and_shield=31400,
        gold_earned=21200,
        kill_participation=0.79,
        match_score=14.8,
        rating_grade="S+",
        mvp_badge="MVP",
        skin_id=82006,
        skin_name="暗星 莫德凯撒",
        skin_splash_path=splash,
    )


def _assert_no_web_orange(img: QImage) -> None:
    for y in range(0, img.height(), 5):
        for x in range(0, img.width(), 5):
            c = img.pixelColor(x, y)
            assert not (abs(c.red() - 255) <= 12 and abs(c.green() - 90) <= 12 and abs(c.blue() - 31) <= 12)


# ---------------------------------------------------------------------------
# 1. Template Registry & Config Persistence
# ---------------------------------------------------------------------------


def test_template_registry_and_config_roundtrip(tmp_path: Path) -> None:
    assert DEFAULT_RECAP_TEMPLATE == CFG_DEFAULT_TEMPLATE == "editorial_glass"
    assert tuple(RECAP_TEMPLATE_ORDER) == tuple(CFG_RECAP_TEMPLATES)
    assert len(RECAP_TEMPLATE_ORDER) == 4
    assert CARD_DEFAULT_WIDTH == 760 and CARD_DEFAULT_HEIGHT == 480

    # Aliases and invalid normalization
    assert normalize_recap_template("endfield_split") == "endfield_split"
    assert normalize_recap_template("VARIANT_B") == "cinematic_banner"
    assert normalize_recap_template(" 03 ") == "diagonal_hologram"
    assert normalize_recap_template("d") == "editorial_glass"
    assert normalize_recap_template("nonexistent_tpl") == DEFAULT_RECAP_TEMPLATE
    assert normalize_recap_template(None) == DEFAULT_RECAP_TEMPLATE

    assert get_recap_template_spec("endfield_split").logical_width == 820
    assert get_recap_template_spec("cinematic_banner").logical_height == 500

    cfg_path = tmp_path / "config.json"
    for tpl in CFG_RECAP_TEMPLATES:
        cfg = AppConfig(recap_template=tpl)
        save_config(cfg, cfg_path)
        loaded = load_config(cfg_path)
        assert loaded.recap_template == tpl

    clamped = AppConfig.from_dict({"recap_template": "invalid_weird_template"})
    assert clamped.recap_template == DEFAULT_RECAP_TEMPLATE


# ---------------------------------------------------------------------------
# 2. All 4 Templates Render Cleanly Across Full Data & Edge Cases
# ---------------------------------------------------------------------------


def test_all_four_templates_render_distinctly_and_safely(tmp_path: Path) -> None:
    _qt_app()
    full_data = _sample_data(tmp_path, with_splash=True, augment_count=5)
    no_splash_data = _sample_data(tmp_path, with_splash=False, augment_count=5)

    rendered_full: dict[str, QImage] = {}
    for tpl_id in RECAP_TEMPLATE_ORDER:
        spec = RECAP_TEMPLATES[tpl_id]
        img = render_match_recap_image(full_data, template=tpl_id)
        assert not img.isNull()
        assert img.width() == spec.logical_width
        assert img.height() == spec.logical_height
        _assert_no_web_orange(img)
        rendered_full[tpl_id] = img

        # Legacy ai_highlight must NEVER leak into any template
        with_ai = render_match_recap_image(
            MatchRecapData(**{**full_data.__dict__, "ai_highlight": "不应出现的废弃AI文本框"}),
            template=tpl_id,
        )
        assert with_ai.bits().tobytes() == img.bits().tobytes()

        # Skin splash backdrop produces clearly visible difference in all 4 templates
        img_no_splash = render_match_recap_image(no_splash_data, template=tpl_id)
        assert img_no_splash.bits().tobytes() != img.bits().tobytes()

        # Aspect-preserving custom scaling (2x)
        hi = render_match_recap_image(
            full_data,
            width=spec.logical_width * 2,
            height=spec.logical_height * 2,
            template=tpl_id,
        )
        assert hi.width() == spec.logical_width * 2
        assert hi.height() == spec.logical_height * 2

        # Edge cases: 0/4/6 augments, defeat/unknown state, empty telemetry, minimal data
        for n_augs in (0, 4, 6):
            d_aug = _sample_data(tmp_path, with_splash=(n_augs > 0), augment_count=n_augs)
            im_aug = render_match_recap_image(d_aug, template=tpl_id)
            assert im_aug.size() == img.size()
            _assert_no_web_orange(im_aug)

        d_loss = MatchRecapData(
            **{
                **full_data.__dict__,
                "won": 0,
                "mvp_badge": "SVP",
                "final_stats": {},
                "damage_dealt": 0,
                "damage_share": 0.0,
            }
        )
        im_loss = render_match_recap_image(d_loss, template=tpl_id)
        assert im_loss.size() == img.size()

        im_min = render_match_recap_image(MatchRecapData(hero_id="", hero_name=""), template=tpl_id)
        assert im_min.size() == img.size()

    # Same-dimension templates (endfield_split vs diagonal_hologram, both 820x480) must differ visibly
    assert rendered_full["endfield_split"].bits().tobytes() != rendered_full["diagonal_hologram"].bits().tobytes()


# ---------------------------------------------------------------------------
# 3. Interactive Template Switcher in MatchRecapDialog
# ---------------------------------------------------------------------------


def test_dialog_interactive_template_switching_and_export(tmp_path: Path) -> None:
    app = _qt_app()
    data = _sample_data(tmp_path, with_splash=True, augment_count=6)
    changed_events: list[str] = []

    dlg = MatchRecapDialog(
        data,
        template="editorial_glass",
        on_template_changed=changed_events.append,
    )
    try:
        app.processEvents()
        assert dlg.current_template == "editorial_glass"
        assert tuple(dlg.template_buttons.keys()) == RECAP_TEMPLATE_ORDER
        assert dlg.template_buttons["editorial_glass"].isChecked() is True
        assert (dlg.card_image.width(), dlg.card_image.height()) == (760, 480)

        # Zero Web Tactile Orange in dialog QSS
        all_qss = dlg.styleSheet() + " ".join(w.styleSheet() for w in dlg.findChildren(QWidget))
        assert "#FF5A1F" not in all_qss.upper()

        # Click through every template and verify live image + size + callback + export
        for tpl_id in ("endfield_split", "cinematic_banner", "diagonal_hologram"):
            dlg.template_buttons[tpl_id].click()
            app.processEvents()
            spec = RECAP_TEMPLATES[tpl_id]
            assert dlg.current_template == tpl_id
            assert dlg.template_buttons[tpl_id].isChecked() is True
            assert (dlg.card_image.width(), dlg.card_image.height()) == (spec.logical_width, spec.logical_height)
            assert str(spec.logical_width) in dlg.template_size_label.text()

            dlg.copy_to_clipboard()
            cb_img = QGuiApplication.clipboard().image()
            assert (cb_img.width(), cb_img.height()) == (spec.logical_width, spec.logical_height)

            out_file = tmp_path / f"export_{tpl_id}.png"
            assert dlg.save_to_path(out_file) is True
            reloaded = QImage(str(out_file))
            assert (reloaded.width(), reloaded.height()) == (spec.logical_width, spec.logical_height)

        assert changed_events == ["endfield_split", "cinematic_banner", "diagonal_hologram"]

        # Async update_data MUST preserve the user's selected template
        dlg.set_template("cinematic_banner", emit_change=False)
        refreshed = MatchRecapData(**{**data.__dict__, "combo_title": "水银法坦成型"})
        dlg.update_data(refreshed)
        app.processEvents()
        assert dlg.current_template == "cinematic_banner"
        assert (dlg.card_image.width(), dlg.card_image.height()) == (780, 500)

        # Direct copy helper respects explicit template parameter
        clip = copy_recap_to_clipboard(data, template="diagonal_hologram")
        assert (clip.width(), clip.height()) == (820, 480)
    finally:
        dlg.close()
        app.processEvents()


# ---------------------------------------------------------------------------
# 4. Controller Persistence Integration
# ---------------------------------------------------------------------------


def test_controller_persists_selected_recap_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _qt_app()

    class _NoopCapturer:
        backend = "noop"
        consecutive_black_frames = 0

        def grab(self, **_kw):
            return CaptureResult(frame_bgr=None, backend="noop", is_black=True, mean_luma=0.0)

        def close(self) -> None:
            return None

    class _NoopHotkeys(QAbstractNativeEventFilter):
        def nativeEventFilter(self, _eventType, _message):
            return False, 0

        def register(self, *_a, **_kw) -> None:
            return None

        def unregister_all(self) -> None:
            return None

    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "app_data"))
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)
    monkeypatch.setattr(app_mod, "find_game_window", lambda: WindowRect(hwnd=1, left=0, top=0, width=1920, height=1080))
    monkeypatch.setattr(app_mod, "is_hwnd_foreground", lambda _hwnd: True)

    ctrl = app_mod.HexHelperController(app, show_lookup_on_start=False)
    ctrl._vision_timer.stop()
    ctrl._heartbeat_timer.stop()
    if ctrl._wallet_heartbeat_timer is not None:
        ctrl._wallet_heartbeat_timer.stop()
    if ctrl._startup_update_timer is not None:
        ctrl._startup_update_timer.stop()

    try:
        ctrl.store.static.hero_aliases["82"] = "Mordekaiser"
        me = PlayerLiveInfo("me", "莫德凯撒", "Mordekaiser", "ORDER", False, 18, 16, 4, 22, ("4633",))
        e1 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 17, 6, 8, 9, ("3071",))
        ctrl._apply_live_snapshot(
            LiveMatchSnapshot(920.0, me, (me,), (e1,), 1800.0, my_team_kills=48, enemy_team_kills=37, game_end_result=1)
        )
        ctrl._capture_post_match_recap(won=1)
        assert ctrl._last_recap_data is not None
        assert ctrl._last_recap_data.hero_alias == "Mordekaiser"

        # Open recap dialog -> switch to Template 01 (endfield_split) -> config persisted
        dlg1 = ctrl._open_last_match_recap()
        assert dlg1 is not None
        assert dlg1.current_template == DEFAULT_RECAP_TEMPLATE
        dlg1.template_buttons["endfield_split"].click()
        app.processEvents()

        assert ctrl.cfg.recap_template == "endfield_split"
        reloaded_cfg = load_config()
        assert reloaded_cfg.recap_template == "endfield_split"

        # Re-open dialog -> remembers user's saved selection
        dlg2 = ctrl._open_last_match_recap()
        assert dlg2 is not None
        assert dlg2.current_template == "endfield_split"
        assert (dlg2.card_image.width(), dlg2.card_image.height()) == (820, 480)
        dlg2.close()
    finally:
        ctrl.shutdown()
        app.processEvents()


def _make_loading_jpg(path: Path, *, with_dark_border: bool) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = QImage(308, 560, QImage.Format.Format_ARGB32)
    img.fill(QColor("#050505" if with_dark_border else "#FBCFE8"))
    p = QPainter(img)
    inner = QLinearGradient(0.0, 0.0, 308.0, 560.0)
    inner.setColorAt(0.0, QColor("#C084FC"))
    inner.setColorAt(1.0, QColor("#38BDF8"))
    if with_dark_border:
        p.fillRect(10, 12, 288, 536, inner)
    else:
        p.fillRect(img.rect(), inner)
    p.end()
    assert img.save(str(path), "JPG", 92)
    return str(path)


def _make_biased_splash_jpg(path: Path, subject_x_frac: float) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    w, h = 1215, 717
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor("#0F172A"))
    p = QPainter(img)
    cx = int(w * subject_x_frac)
    cy = int(h * 0.28)
    rad = QRadialGradient(cx, cy, 170.0)
    rad.setColorAt(0.0, QColor("#FDE68A"))
    rad.setColorAt(0.45, QColor("#E879F9"))
    rad.setColorAt(1.0, QColor(15, 23, 42, 0))
    p.fillRect(img.rect(), rad)
    p.setPen(QColor("#38BDF8"))
    for d in range(-70, 75, 18):
        p.drawLine(cx + d, cy - 80, cx - d, cy + 95)
    p.end()
    assert img.save(str(path), "JPG", 92)
    return str(path)


def test_smart_focal_loading_border_trim_and_variant_rendering(tmp_path: Path) -> None:
    from hex_helper.ui.match_recap_card import _estimate_subject_focal, _trim_loading_card_border

    _qt_app()
    framed_path = _make_loading_jpg(tmp_path / "framed_loading.jpg", with_dark_border=True)
    clean_path = _make_loading_jpg(tmp_path / "clean_loading.jpg", with_dark_border=False)

    trimmed = _trim_loading_card_border(QImage(framed_path))
    untouched = _trim_loading_card_border(QImage(clean_path))
    assert trimmed.width() < 308 and trimmed.height() < 560
    assert (untouched.width(), untouched.height()) == (308, 560)

    left_fx, _left_fy = _estimate_subject_focal(QImage(_make_biased_splash_jpg(tmp_path / "left_hero.jpg", 0.30)))
    right_fx, _right_fy = _estimate_subject_focal(QImage(_make_biased_splash_jpg(tmp_path / "right_hero.jpg", 0.72)))
    assert left_fx < 0.44
    assert right_fx > 0.56

    base = _sample_data(tmp_path, with_splash=False, augment_count=4)
    center_path = _make_biased_splash_jpg(tmp_path / "82006_center.jpg", 0.50)
    with_variants = MatchRecapData(
        **{
            **base.__dict__,
            "final_stats": {**base.final_stats, "hp": 6012.0, "ah": 222.0, "as": 2.26, "ms": 350.0},
            "skin_loading_path": framed_path,
            "skin_center_path": center_path,
            "skin_splash_path": _make_biased_splash_jpg(tmp_path / "82006_splash.jpg", 0.35),
        }
    )

    for tpl_id in RECAP_TEMPLATE_ORDER:
        spec = RECAP_TEMPLATES[tpl_id]
        im_var = render_match_recap_image(with_variants, template=tpl_id)
        im_empty = render_match_recap_image(base, template=tpl_id)
        assert (im_var.width(), im_var.height()) == (spec.logical_width, spec.logical_height)
        assert im_var.bits().tobytes() != im_empty.bits().tobytes()
        _assert_no_web_orange(im_var)

        # Diagonal hologram center stats gutter at x=550..556 must remain uncluttered dark dock background
        if tpl_id == "diagonal_hologram":
            gutter_bright = 0
            for y in range(384, 462, 3):
                for x in (548, 550, 556, 558):
                    c = im_var.pixelColor(x, y)
                    luma = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
                    if luma > 75.0:
                        gutter_bright += 1
            assert gutter_bright == 0
