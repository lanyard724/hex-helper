"""Offscreen PySide6 regression & visual-proof tests for MatchRecapData, renderer, and MatchRecapDialog."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
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

LOCAL_ICON_CACHE = Path(r"C:\Users\Administrator\AppData\Local\HexHelper\cache\icons")
VISUAL_PROOF_PATH = Path(
    r"C:\Users\Administrator\.gemini\antigravity\brain\c6d79029-fe63-4195-8605-30bd8143d2a5\audit_visual\match_recap_card_demo.png"
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


def test_render_full_win_card_and_save_visual_proof():
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

    # Save real rendered visual proof PNG for inspection
    VISUAL_PROOF_PATH.parent.mkdir(parents=True, exist_ok=True)
    saved = img.save(str(VISUAL_PROOF_PATH), "PNG")
    assert saved is True
    assert VISUAL_PROOF_PATH.is_file()
    assert VISUAL_PROOF_PATH.stat().st_size > 15_000


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


def test_recap_dialog_live_update_data_and_poro_snax_filter_and_eog_snapshot(tmp_path: Path) -> None:
    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.game.lcu_client import LcuClient
    from hex_helper.scoring.matchup_optimizer import TRINKET_AND_FREE_ITEM_IDS

    # 1. ARAM Mayhem Poro-Snax 220013 must be filtered out of 6-slot builds
    assert "220013" in TRINKET_AND_FREE_ITEM_IDS

    # 2. StaticAssetsManager.ensure_icon falls back from 6-digit ARAM ID to 4-digit base ID
    app = _qt_app()

    def _make_png(path: Path, color: str) -> Path:
        img = QImage(32, 32, QImage.Format.Format_ARGB32)
        img.fill(QColor(color))
        assert img.save(str(path), "PNG")
        return path

    fetched_urls: list[str] = []
    png_stub = _make_png(tmp_path / "stub3114.png", "#3B82F6").read_bytes()

    def fake_fetch(url: str) -> bytes:
        fetched_urls.append(url)
        if url.endswith("/item/3114.png"):
            return png_stub
        raise RuntimeError("404")

    sm = StaticAssetsManager(tmp_path / "cache", fetcher=fake_fetch)
    resolved = sm.ensure_icon("item", "223114")
    assert resolved is not None and resolved.is_file()
    assert any(u.endswith("/item/223114.png") for u in fetched_urls)
    assert any(u.endswith("/item/3114.png") for u in fetched_urls)

    # 3. LcuClient.get_eog_snapshot extracts authoritative ITEM0..ITEM5, KDA, won, duration
    lcu = LcuClient()
    lcu._get_json = lambda _path: {  # type: ignore[method-assign]
        "gameLength": 969,
        "localPlayer": {
            "teamId": 100,
            "stats": {
                "WIN": 1,
                "CHAMPIONS_KILLED": 5,
                "NUM_DEATHS": 9,
                "ASSISTS": 53,
                "ITEM0": 2530,
                "ITEM1": 6617,
                "ITEM2": 6621,
                "ITEM3": 6616,
                "ITEM4": 3504,
                "ITEM5": 3114,
            },
        },
    }
    snap = lcu.get_eog_snapshot()
    assert snap["won"] == 1
    assert snap["item_ids"] == ("2530", "6617", "6621", "6616", "3504", "3114")
    assert snap["kda"] == "5/9/53"
    assert snap["duration_s"] == 969

    # 4. MatchRecapDialog.update_data live re-renders once missing item icon arrives
    data_before = MatchRecapData(
        hero_id="37",
        hero_name="娑娜",
        won=1,
        kda="5/9/53",
        duration_s=969,
        item_entries=(("禁忌雕像", ""),),
        item_ids=("3114",),
    )
    dlg = MatchRecapDialog(data_before)
    app.processEvents()
    before_bytes = dlg.card_image.bits().tobytes()

    icon_3114 = _make_png(tmp_path / "3114.png", "#22C55E")
    data_after = MatchRecapData(
        hero_id="37",
        hero_name="娑娜",
        won=1,
        kda="5/9/53",
        duration_s=969,
        item_entries=(("禁忌雕像", str(icon_3114)),),
        item_ids=("3114",),
    )
    dlg.update_data(data_after)
    app.processEvents()
    after_bytes = dlg.card_image.bits().tobytes()
    assert after_bytes != before_bytes
    assert dlg.data is data_after
    dlg.close()
    app.processEvents()


def test_v2_compute_match_rating_and_normalize_final_stats() -> None:
    from hex_helper.ui.match_recap_card import compute_match_rating, normalize_final_stats

    # Default canvas upgraded to 760x480 for unobstructed 6-item labels + telemetry dashboard
    assert CARD_DEFAULT_WIDTH == 760
    assert CARD_DEFAULT_HEIGHT == 480

    # High-carry victory -> S+ & MVP
    sc_mvp, gr_mvp, bd_mvp = compute_match_rating(
        kda="18/3/26", won=1, damage_share=0.38, damage_taken_share=0.22, kp=0.82, gold_per_min=1150.0
    )
    assert 12.8 <= sc_mvp <= 16.0
    assert gr_mvp == "S+"
    assert bd_mvp == "MVP"

    # High-carry defeat -> S+/S & SVP
    sc_svp, gr_svp, bd_svp = compute_match_rating(
        kda="16/4/24", won=0, damage_share=0.36, damage_taken_share=0.20, kp=0.80, gold_per_min=1100.0
    )
    assert sc_svp >= 12.0
    assert gr_svp in ("S+", "S")
    assert bd_svp == "SVP"

    # Modest KDA & shares -> lower grade, no MVP/SVP badge
    sc_low, gr_low, bd_low = compute_match_rating(
        kda="2/10/5", won=0, damage_share=0.09, damage_taken_share=0.10, kp=0.25, gold_per_min=620.0
    )
    assert 1.0 <= sc_low < 9.5
    assert gr_low in ("A", "B")
    assert bd_low == ""

    # Empty metrics -> zeroed gracefully
    assert compute_match_rating(kda="", won=-1) == (0.0, "", "")

    # normalize_final_stats handles LiveClient camelCase, canonical keys, and filters NaN/negative
    norm = normalize_final_stats(
        {
            "attackDamage": 542.4,
            "abilityPower": 120.0,
            "maxHealth": 9240.0,
            "abilityHaste": 165.0,
            "armor": 310.0,
            "magicResist": 245.0,
            "attackSpeed": 2.65,
            "moveSpeed": 430.0,
            "bad_neg": -5.0,
            "ad_nan": float("nan"),
        }
    )
    assert norm == {
        "ad": 542.4,
        "ap": 120.0,
        "hp": 9240.0,
        "ah": 165.0,
        "armor": 310.0,
        "mr": 245.0,
        "as": 2.65,
        "ms": 430.0,
    }


def test_v2_card_renders_final_stats_eog_telemetry_augment_icons_and_no_ai_highlight(tmp_path: Path) -> None:
    _qt_app()

    def _stub_icon(name: str, color: str) -> str:
        p = tmp_path / f"{name}.png"
        im = QImage(48, 48, QImage.Format.Format_ARGB32)
        im.fill(QColor(color))
        assert im.save(str(p), "PNG")
        return str(p)

    aug_icons = (
        _stub_icon("aug0", "#D946EF"),
        _stub_icon("aug1", "#C8AA6E"),
        _stub_icon("aug2", "#0AC8B9"),
        _stub_icon("aug3", "#94A3B8"),
    )
    item_icons = tuple(
        (nm, _stub_icon(f"item_{i}", "#3B82F6"))
        for i, nm in enumerate(
            ("海妖杀手", "鬼索的狂暴之刃", "破败王者之刃", "卢安娜的飓风", "死亡之舞", "多米尼克领主的致意")
        )
    )

    base_kw = dict(
        hero_id="6",
        hero_name="厄加特",
        hero_icon_path=_stub_icon("hero_6", "#10B981"),
        won=1,
        kda="17/5/23",
        duration_s=1040,
        team_kills="54:40",
        is_first_prismatic=True,
        augments=(
            ("双刀流", "prismatic"),
            ("歌利亚巨人", "prismatic"),
            ("回归基本功", "gold"),
            ("灵巧", "silver"),
        ),
        augment_icon_paths=aug_icons,
        combo_title="巨像特效战神 · 满配成型",
        combo_completed=True,
        item_entries=item_icons,
        final_stats={
            "attackDamage": 512.0,
            "abilityPower": 85.0,
            "maxHealth": 8850.0,
            "abilityHaste": 152.0,
            "armor": 320.0,
            "magicResist": 235.0,
            "attackSpeed": 2.75,
        },
        damage_dealt=68420,
        damage_share=0.34,
        damage_taken=59310,
        heal_and_shield=18400,
        gold_earned=21400,
        kill_participation=0.74,
        match_score=14.2,
        rating_grade="S+",
        mvp_badge="MVP",
        dataset_patch="26.23",
    )

    img_without_ai = render_match_recap_image(MatchRecapData(**base_kw, ai_highlight=""))
    img_with_ai = render_match_recap_image(
        MatchRecapData(**base_kw, ai_highlight="旧版 AI 副驾高光文案绝不应绘制到 v2 战报图上！")
    )

    assert img_without_ai.width() == 760 and img_without_ai.height() == 480
    # Strictly ZERO legacy "AI 副驾高光" box rendered: passing ai_highlight produces identical pixels!
    assert img_without_ai.bits().tobytes() == img_with_ai.bits().tobytes()

    src_text = (Path(__file__).resolve().parents[1] / "src" / "hex_helper" / "ui" / "match_recap_card.py").read_text(
        encoding="utf-8"
    )
    assert "AI 副驾高光" not in src_text
    assert "#FF5A1F" not in src_text.upper()

    # Verify augment icon regions actually reflect loaded local PNG pixels (differs from no-icon render)
    img_no_aug_icons = render_match_recap_image(MatchRecapData(**{**base_kw, "augment_icon_paths": ()}))
    assert img_without_ai.bits().tobytes() != img_no_aug_icons.bits().tobytes()

    # Verify telemetry dashboard region differs when stats + EOG data are populated vs empty
    img_no_stats = render_match_recap_image(
        MatchRecapData(
            **{
                **base_kw,
                "final_stats": {},
                "damage_dealt": 0,
                "damage_share": 0.0,
                "damage_taken": 0,
                "heal_and_shield": 0,
                "gold_earned": 0,
            }
        )
    )
    assert img_without_ai.bits().tobytes() != img_no_stats.bits().tobytes()

    st = _sample_pixel_stats(img_without_ai)
    assert st["unique_buckets"] >= 50
    assert st["gold_hits"] > 40
    assert st["cyan_hits"] > 20
    assert st["web_orange_hits"] == 0
