"""Offscreen PySide6 regression & visual-proof tests for MatchRecapData, renderer, and MatchRecapDialog."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import pytest

from PySide6.QtGui import QColor, QGuiApplication, QImage
from PySide6.QtWidgets import QApplication, QFileDialog

from hex_helper.ui.match_recap_card import (
    CARD_DEFAULT_HEIGHT,
    CARD_DEFAULT_WIDTH,
    MatchRecapData,
    MatchRecapDialog,
    _wrap_text_max_lines,
    copy_recap_to_clipboard,
    render_match_recap_image,
)

LOCAL_ICON_CACHE = (
    Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    / "HexHelper" / "cache" / "icons"
)


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _local_hero_icon(hero_id: str) -> str:
    p = LOCAL_ICON_CACHE / "champion" / f"{hero_id}.png"
    return str(p) if p.is_file() else ""


def _local_item_icon(item_id: str) -> str:
    p = LOCAL_ICON_CACHE / "item" / f"{item_id}.png"
    return str(p) if p.is_file() else ""


def _sample_pixel_stats(img: QImage) -> dict[str, int]:
    """Sample pixels across the rendered card to assert richness and strict Hextech palette."""
    w, h = img.width(), img.height()
    unique_rgb: set[tuple[int, int, int]] = set()
    gold_hits = 0
    cyan_hits = 0
    prism_hits = 0
    web_orange_hits = 0

    step_x = max(1, w // 95)
    step_y = max(1, h // 65)
    for y in range(0, h, step_y):
        for x in range(0, w, step_x):
            c = img.pixelColor(x, y)
            r, g, b = c.red(), c.green(), c.blue()
            unique_rgb.add((r // 8, g // 8, b // 8))
            # Gold family (#C8AA6E / #F0E6D2 / #C89B3C)
            if r >= 165 and g >= 130 and b <= 215 and r > b + 20:
                gold_hits += 1
            # Hextech cyan family (#0AC8B9 / #5EEAD4 / #0397AB)
            if g >= 135 and b >= 145 and r <= 125:
                cyan_hits += 1
            # Prismatic magenta-violet (#D946EF)
            if r >= 150 and b >= 155 and g <= 135:
                prism_hits += 1
            # Forbidden Web Tactile Orange (#FF5A1F)
            if abs(r - 255) <= 12 and abs(g - 90) <= 12 and abs(b - 31) <= 12:
                web_orange_hits += 1

    return {
        "unique_buckets": len(unique_rgb),
        "gold_hits": gold_hits,
        "cyan_hits": cyan_hits,
        "prism_hits": prism_hits,
        "web_orange_hits": web_orange_hits,
    }


def _build_full_win_data() -> MatchRecapData:
    return MatchRecapData(
        hero_id="145",
        hero_name="卡莎",
        hero_icon_path=_local_hero_icon("145"),
        won=1,
        kda="16/7/28",
        duration_s=1122,  # 18:42
        team_kills="52 : 44",
        is_first_prismatic=True,
        augments=(
            ("双刀流", "prismatic"),
            ("踢踏舞", "prismatic"),
            ("物法皆修", "gold"),
            ("灵巧", "silver"),
        ),
        combo_title="双刀流特效机关枪 · 核爆成型",
        combo_completed=True,
        item_entries=(
            ("海妖杀手", _local_item_icon("6672")),
            ("鬼索的狂暴之刃", _local_item_icon("3124")),
            ("破败王者之刃", _local_item_icon("3153")),
            ("卢安娜的飓风", _local_item_icon("3085")),
            ("死亡之舞", _local_item_icon("6333")),
            ("狂战士胫甲", _local_item_icon("3006")),
        ),
        ai_highlight="AI 关键反制：敌21杀雏龙起飞，第12分钟果断变阵补死亡之舞反杀锁胜",
        dataset_patch="26.23",
    )


def test_match_recap_data_is_frozen_and_has_expected_defaults():
    d = MatchRecapData(hero_id="236", hero_name="卢锡安")
    assert d.hero_icon_path == ""
    assert d.won == -1
    assert d.kda == ""
    assert d.duration_s == 0
    assert d.team_kills == ""
    assert d.is_first_prismatic is False
    assert d.augments == ()
    assert d.combo_title == ""
    assert d.combo_completed is False
    assert d.item_entries == ()
    assert d.ai_highlight == ""
    assert d.dataset_patch == "26.23"

    with pytest.raises(FrozenInstanceError):
        d.won = 1  # type: ignore[misc]


def test_render_full_win_card_and_save_visual_proof(tmp_path: Path):
    _qt_app()
    data = _build_full_win_data()
    img = render_match_recap_image(data)

    assert not img.isNull()
    assert img.width() == CARD_DEFAULT_WIDTH
    assert img.height() == CARD_DEFAULT_HEIGHT

    stats = _sample_pixel_stats(img)
    assert stats["unique_buckets"] >= 45
    assert stats["gold_hits"] > 30
    assert stats["cyan_hits"] > 15
    assert stats["prism_hits"] > 5
    assert stats["web_orange_hits"] == 0

    # Save real rendered visual proof PNG into temp directory
    proof_path = tmp_path / "match_recap_card_demo.png"
    saved = img.save(str(proof_path), "PNG")
    assert saved is True
    assert proof_path.is_file()
    assert proof_path.stat().st_size > 15_000


def test_render_loss_and_unknown_status_cards_differ_visually():
    _qt_app()
    base_kwargs = dict(
        hero_id="236",
        hero_name="卢锡安",
        hero_icon_path=_local_hero_icon("236"),
        kda="11/9/19",
        duration_s=985,
        team_kills="39 : 46",
        augments=(("珠光护手", "prismatic"), ("大腕", "gold"), ("灵巧", "silver")),
        combo_title="暴击技能连招 · 差一件成型",
        combo_completed=False,
        item_entries=(
            ("无尽之刃", _local_item_icon("3031")),
            ("多米尼克领主的致意", _local_item_icon("3036")),
            ("饮血剑", _local_item_icon("3072")),
        ),
        ai_highlight="敌方双前排心之钢叠层过高，中期及时补轻语压低血线但末波团惜败。",
        dataset_patch="26.23",
    )

    win_img = render_match_recap_image(MatchRecapData(won=1, **base_kwargs))
    loss_img = render_match_recap_image(MatchRecapData(won=0, **base_kwargs))
    unk_img = render_match_recap_image(MatchRecapData(won=-1, **base_kwargs))

    for img in (win_img, loss_img, unk_img):
        assert not img.isNull()
        assert img.size().width() == CARD_DEFAULT_WIDTH
        assert img.size().height() == CARD_DEFAULT_HEIGHT
        st = _sample_pixel_stats(img)
        assert st["unique_buckets"] >= 35
        assert st["web_orange_hits"] == 0

    # Top-left badge region must visibly differ between win (gold) and loss (cyan)
    diff_pixels = 0
    for y in range(18, 44, 2):
        for x in range(26, 170, 3):
            if win_img.pixelColor(x, y) != loss_img.pixelColor(x, y):
                diff_pixels += 1
    assert diff_pixels > 40


def test_render_missing_icons_fallback_and_extreme_text_safety():
    _qt_app()
    data = MatchRecapData(
        hero_id="9999",
        hero_name="超级无敌极长名字测试英雄角色",
        hero_icon_path=r"Z:\nonexistent\missing_hero.png",
        won=0,
        kda="24/0/18",
        duration_s=0,
        team_kills="",
        is_first_prismatic=False,
        augments=(
            ("超长海克斯强化名称测试溢出截断保护", "prismatic"),
            ("黄金海克斯", "gold"),
            ("白银海克斯", "silver"),
            ("未知稀有度强化", "unknown_tier"),
        ),
        combo_title="",
        combo_completed=False,
        item_entries=(
            ("海妖杀手", r"Z:\nonexistent\item1.png"),
            ("鬼索的狂暴之刃", ""),
            ("破败王者之刃", r"Z:\nonexistent\item3.png"),
            ("死亡之舞", ""),
            ("守护天使", ""),
            ("水银弯刀", ""),
            ("第七件应被忽略", ""),
        ),
        ai_highlight=(
            "这是一段故意非常长的 AI 副驾战报高光文案用于测试两行自动换行与省略号截断逻辑，"
            "确保无论中英文混排 Gemini 2.5 Pro 输出多长都不会溢出深蓝面板区域或遮挡底部水印文字。"
        ),
        dataset_patch="26.23",
    )

    img = render_match_recap_image(data)
    assert not img.isNull()
    st = _sample_pixel_stats(img)
    assert st["unique_buckets"] >= 30
    assert st["gold_hits"] > 20
    assert st["cyan_hits"] > 10
    assert st["web_orange_hits"] == 0

    # Minimal empty-fields data must also render cleanly without crashing
    minimal_img = render_match_recap_image(MatchRecapData(hero_id="", hero_name=""))
    assert not minimal_img.isNull()
    assert _sample_pixel_stats(minimal_img)["unique_buckets"] >= 15

    # HiDPI 2x scaling must work cleanly
    hi_img = render_match_recap_image(data, width=1520, height=860)
    assert hi_img.width() == 1520
    assert hi_img.height() == 860


def test_wrap_text_max_lines_respects_two_line_limit():
    _qt_app()
    from PySide6.QtGui import QFontMetricsF
    from hex_helper.ui.match_recap_card import _sans_font

    fm = QFontMetricsF(_sans_font(13))
    long_text = "AI 关键反制：" + ("敌方高爆发阵容成型后及时补防御装完成反杀锁胜，" * 6)
    lines = _wrap_text_max_lines(long_text, fm, max_width=420.0, max_lines=2)
    assert len(lines) == 2
    for line in lines:
        assert fm.horizontalAdvance(line) <= 425.0


def test_copy_recap_to_clipboard_helper():
    app = _qt_app()
    data = _build_full_win_data()
    img = copy_recap_to_clipboard(data)
    app.processEvents()

    assert not img.isNull()
    cb = QGuiApplication.clipboard()
    assert cb is not None
    cb_img = cb.image()
    assert not cb_img.isNull()
    assert cb_img.width() == CARD_DEFAULT_WIDTH
    assert cb_img.height() == CARD_DEFAULT_HEIGHT


def test_match_recap_dialog_preview_copy_and_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _qt_app()
    data = _build_full_win_data()
    dlg = MatchRecapDialog(data)
    app.processEvents()

    assert dlg.card_image.width() == CARD_DEFAULT_WIDTH
    assert dlg.card_image.height() == CARD_DEFAULT_HEIGHT
    assert not dlg.preview_label.pixmap().isNull()
    assert dlg.copy_btn.text() == "📋 一键复制战报图"
    assert dlg.save_btn.text() == "💾 保存图片"
    assert dlg.close_btn.text() == "关闭"
    assert dlg.default_save_filename() == "HexHelper_卡莎_战报.png"

    # Trigger 1-click clipboard copy button
    dlg.copy_btn.click()
    app.processEvents()
    assert dlg.copy_btn.text() == "✓ 已复制，去群里 Ctrl+V 粘贴！"
    cb_img = QGuiApplication.clipboard().image()
    assert not cb_img.isNull()
    assert cb_img.width() == CARD_DEFAULT_WIDTH

    # Trigger save button via monkeypatched QFileDialog (headless safe)
    out_png = tmp_path / "HexHelper_卡莎_战报.png"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(out_png), "PNG 图片 (*.png)")),
    )
    dlg.save_btn.click()
    app.processEvents()

    assert out_png.is_file()
    assert out_png.stat().st_size > 15_000
    reloaded = QImage(str(out_png))
    assert not reloaded.isNull()
    assert reloaded.width() == CARD_DEFAULT_WIDTH
    assert reloaded.height() == CARD_DEFAULT_HEIGHT

    # Guard against forbidden Web Tactile orange in source file
    src_text = Path(__file__).resolve().parents[1] / "src" / "hex_helper" / "ui" / "match_recap_card.py"
    assert "#FF5A1F" not in src_text.read_text(encoding="utf-8").upper()

    dlg.close()
    app.processEvents()
