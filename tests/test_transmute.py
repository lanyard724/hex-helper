"""End-to-end WinRT zh-Hans-CN OCR & state-machine tests for Transmute secondary recognition."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hex_helper.app import GameState, HexStateMachine
from hex_helper.data.provider import AugmentInfo
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.detector import DetectedCard, PickerDetectionResult
from hex_helper.vision.ocr_winrt import WinRtOcrEngine
from hex_helper.vision.transmute import PendingTransmute, TransmuteRevealWatcher


def _load_cn_font(size: int) -> ImageFont.FreeTypeFont:
    for p in (
        Path(r"C:\Windows\Fonts\msyhbd.ttc"),
        Path(r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
    ):
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def _make_catalog() -> list[AugmentInfo]:
    return [
        AugmentInfo("1237", "质变：黄金阶", "silver"),
        AugmentInfo("1238", "质变：棱彩阶", "gold"),
        AugmentInfo("1243", "质变：混沌", "prismatic"),
        AugmentInfo("12317", "潘朵拉的盒子", "prismatic"),
        AugmentInfo("1015", "死亡之环", "prismatic"),
        AugmentInfo("1048", "珠光护手", "prismatic"),
        AugmentInfo("1045", "炼狱导管", "prismatic"),
        AugmentInfo("3163", "迷人蝴蝶", "gold"),
        AugmentInfo("3049", "装甲冲击", "gold"),
        AugmentInfo("3059", "没法力，没问题", "silver"),
        AugmentInfo("1007", "大力", "silver"),
        AugmentInfo("1323", "残忍", "prismatic"),
    ]


def _render_canvas(width: int = 2560, height: int = 1440) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (width, height), (14, 20, 32))
    draw = ImageDraw.Draw(img)
    return img, draw


def _pil_to_bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def test_transmute_real_winrt_ocr_chat_banner_and_hover_tooltip():
    ocr = WinRtOcrEngine("zh-Hans-CN")
    catalog = _make_catalog()
    watcher = TransmuteRevealWatcher(ocr, catalog)
    rect = WindowRect(hwnd=1, left=0, top=0, width=2560, height=1440)

    try:
        # 1) Channel 1: Bottom-left chat announcement after picking 质变：棱彩阶 (1238)
        img_chat, draw_chat = _render_canvas()
        draw_chat.rectangle([40, 860, 820, 1140], fill=(10, 16, 26), outline=(80, 70, 45), width=2)
        draw_chat.text((70, 920), "系统提示：你获得了 【死亡之环】", fill=(245, 232, 198), font=_load_cn_font(34))
        pending_prism = PendingTransmute(
            source_augment_id="1238",
            source_name="质变：棱彩阶",
            expected_count=1,
            target_rarity="prismatic",
            started_mono=10.0,
            excluded_fading_ids=("3049", "3163"),
        )
        hits = watcher.process_frame(
            _pil_to_bgr(img_chat),
            rect,
            pending=pending_prism,
            already_owned_ids={"1238"},
            now_mono=11.0,
        )
        assert len(hits) == 1
        assert hits[0].augment.augment_id == "1015"
        assert hits[0].channel == "chat"

        # 2) Channel 2: Center reveal toast after picking 质变：黄金阶 (1237)
        watcher.reset()
        img_banner, draw_banner = _render_canvas()
        draw_banner.rectangle([940, 340, 1620, 620], fill=(12, 22, 38), outline=(200, 160, 80), width=3)
        draw_banner.text((1160, 390), "迷人蝴蝶", fill=(250, 235, 190), font=_load_cn_font(44))
        draw_banner.text((1000, 470), "黄金阶 强化符文：治疗护盾积聚蝴蝶齐射", fill=(210, 220, 235), font=_load_cn_font(28))
        pending_gold = PendingTransmute(
            source_augment_id="1237",
            source_name="质变：黄金阶",
            expected_count=1,
            target_rarity="gold",
            started_mono=20.0,
            excluded_fading_ids=("3049",),  # fading unpicked gold card
        )
        hits_gold = watcher.process_frame(
            _pil_to_bgr(img_banner),
            rect,
            pending=pending_gold,
            already_owned_ids={"1237"},
            now_mono=21.0,
        )
        assert len(hits_gold) == 1
        assert hits_gold[0].augment.augment_id == "3163"
        assert hits_gold[0].channel == "reveal_banner"

        # Verify rarity filter blocks prismatic 死亡之环 when pending_gold expects ONLY gold!
        watcher.reset()
        hits_wrong_rarity = watcher.process_frame(
            _pil_to_bgr(img_chat),
            rect,
            pending=pending_gold,
            already_owned_ids={"1237"},
            now_mono=21.2,
        )
        assert hits_wrong_rarity == []

        # 3) Channel 3: Live Cursor Hover-Tooltip after 15s (post-reveal fallback) for 质变：混沌 (2 augments!)
        watcher.reset()
        pending_chaos = PendingTransmute(
            source_augment_id="1243",
            source_name="质变：混沌",
            expected_count=2,
            target_rarity=None,
            started_mono=30.0,
        )

        # Hover over Shop Item first -> MUST BE REJECTED because of "点击以购买"
        img_shop, draw_shop = _render_canvas()
        draw_shop.rectangle([600, 960, 1080, 1210], fill=(10, 18, 30), outline=(160, 130, 70), width=2)
        draw_shop.text((640, 985), "珠光护手", fill=(250, 235, 190), font=_load_cn_font(36))
        draw_shop.text((640, 1045), "点击以购买 · 出售价格 2100", fill=(210, 220, 235), font=_load_cn_font(26))
        shop_hits = watcher.process_frame(
            _pil_to_bgr(img_shop),
            rect,
            pending=pending_chaos,
            already_owned_ids={"1243"},
            cursor_pos=(840, 1240),
            now_mono=46.0,
        )
        assert shop_hits == []

        # Hover over Augment Slot #1 -> resolves 珠光护手 (1048)
        img_h1, draw_h1 = _render_canvas()
        draw_h1.rectangle([600, 960, 1080, 1210], fill=(10, 18, 30), outline=(210, 90, 240), width=2)
        draw_h1.text((640, 985), "珠光护手", fill=(250, 235, 190), font=_load_cn_font(36))
        draw_h1.text((640, 1045), "棱彩阶 技能伤害可以暴击并获得暴击几率", fill=(210, 220, 235), font=_load_cn_font(26))
        h1_hits = watcher.process_frame(
            _pil_to_bgr(img_h1),
            rect,
            pending=pending_chaos,
            already_owned_ids={"1243"},
            cursor_pos=(840, 1240),
            now_mono=47.0,
        )
        assert len(h1_hits) == 1
        assert h1_hits[0].augment.augment_id == "1048"
        assert h1_hits[0].channel == "hover_tooltip"

        # Simulate state update after 1st augment resolved, then hover Slot #2 -> resolves 炼狱导管 (1045)
        pending_chaos.resolved_ids.append("1048")
        img_h2, draw_h2 = _render_canvas()
        draw_h2.rectangle([680, 960, 1180, 1210], fill=(10, 18, 30), outline=(210, 90, 240), width=2)
        draw_h2.text((720, 985), "炼狱导管", fill=(250, 235, 190), font=_load_cn_font(36))
        draw_h2.text((720, 1045), "棱彩阶 技能命中施加可无限叠加的灼烧", fill=(210, 220, 235), font=_load_cn_font(26))
        h2_hits = watcher.process_frame(
            _pil_to_bgr(img_h2),
            rect,
            pending=pending_chaos,
            already_owned_ids={"1048"},
            cursor_pos=(920, 1240),
            now_mono=48.0,
        )
        assert len(h2_hits) == 1
        assert h2_hits[0].augment.augment_id == "1045"
        assert h2_hits[0].channel == "hover_tooltip"
    finally:
        ocr.close()


def test_state_machine_holds_coach_on_transmute_and_resolves_chaos_two_augments():
    sm = HexStateMachine()
    me = PlayerLiveInfo("me", "布兰德", "Brand", "ORDER", False, 6, 1, 1, 4, ())
    enemy = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 1, 2, ())
    snap = LiveMatchSnapshot(game_time=120.0, my_player=me, allies=(me,), enemies=(enemy,), current_gold=1400.0)
    sm.on_live_snapshot(snap, "63", now_mono=10.0)
    assert sm.state == GameState.IN_GAME

    cards = (
        DetectedCard(0, AugmentInfo("1243", "质变：混沌", "prismatic"), 0.99, (400, 450, 700, 500), "prismatic"),
        DetectedCard(1, AugmentInfo("1015", "死亡之环", "prismatic"), 0.99, (1100, 450, 1400, 500), "prismatic"),
        DetectedCard(2, AugmentInfo("1048", "珠光护手", "prismatic"), 0.99, (1800, 450, 2100, 500), "prismatic"),
    )
    sm.on_picker_result(PickerDetectionResult(True, True, False, cards), clicked_slot=None, now_mono=20.0)
    assert sm.state == GameState.AUGMENT_PICKING

    # Player clicks slot 0 (质变：混沌) and picker closes
    sm.on_picker_result(PickerDetectionResult(False, False, False, (), 0, False, False), clicked_slot=0, now_mono=21.0)
    eff = sm.on_picker_result(PickerDetectionResult(False, False, False, (), 0, False, False), clicked_slot=0, now_mono=21.35)
    assert eff.newly_selected_augment == "1243"
    assert eff.transmute_started is True
    assert eff.coach_trigger == ""  # Held back until secondary recognition resolves!
    assert sm.selected_augments == ["1243"]
    assert sm.pending_transmute is not None
    assert sm.pending_transmute.expected_count == 2
    assert sm.pending_transmute.excluded_fading_ids == ("1015", "1048")

    # 1st augment resolves -> replaces placeholder 1243 in-place, still waiting for 2nd!
    acc1, done1 = sm.resolve_transmute_augment("1045")
    assert (acc1, done1) == (True, False)
    assert sm.selected_augments == ["1045"]
    assert sm.pending_transmute is not None and sm.pending_transmute.remaining_count == 1

    # Duplicate or another transmute rejected cleanly
    assert sm.resolve_transmute_augment("1045") == (False, False)
    assert sm.resolve_transmute_augment("1238") == (False, False)

    # 2nd augment resolves -> appends & completes transmute!
    acc2, done2 = sm.resolve_transmute_augment("3163")
    assert (acc2, done2) == (True, True)
    assert sm.selected_augments == ["1045", "3163"]
    assert sm.pending_transmute is None


def test_passive_hud_hover_auto_sync_recovers_missing_augment_and_rejects_skills():
    ocr = WinRtOcrEngine("zh-Hans-CN")
    catalog = _make_catalog()
    watcher = TransmuteRevealWatcher(ocr, catalog)
    rect = WindowRect(hwnd=1, left=0, top=0, width=2560, height=1440)
    try:
        # 1) Champion skill tooltip on bottom HUD (has 法力消耗 & no augment rarity) -> MUST BE REJECTED
        img_skill, draw_skill = _render_canvas()
        draw_skill.rectangle([680, 960, 1180, 1210], fill=(10, 18, 30), outline=(140, 120, 80), width=2)
        draw_skill.text((720, 985), "迷人蝴蝶", fill=(250, 235, 190), font=_load_cn_font(36))
        draw_skill.text((720, 1045), "法力消耗 80 · 黄金阶 发射子弹造成魔法伤害", fill=(210, 220, 235), font=_load_cn_font(24))
        # First sample registers steady start, second after 0.25s triggers scan
        assert watcher.scan_passive_hud_hover(_pil_to_bgr(img_skill), rect, set(), cursor_pos=(920, 1240), now_mono=10.0) is None
        assert watcher.scan_passive_hud_hover(_pil_to_bgr(img_skill), rect, set(), cursor_pos=(920, 1240), now_mono=10.35) is None

        # 2) Real augment tooltip on bottom HUD -> MUST AUTO-SYNC missing augment 迷人蝴蝶 (3163)
        img_aug, draw_aug = _render_canvas()
        draw_aug.rectangle([680, 960, 1180, 1210], fill=(10, 18, 30), outline=(210, 170, 80), width=2)
        draw_aug.text((720, 985), "迷人蝴蝶", fill=(250, 235, 190), font=_load_cn_font(36))
        draw_aug.text((720, 1045), "黄金阶 强化符文：治疗和护盾积聚蝴蝶齐射", fill=(210, 220, 235), font=_load_cn_font(24))
        # Move to next HUD icon and pause 0.25s
        assert watcher.scan_passive_hud_hover(_pil_to_bgr(img_aug), rect, set(), cursor_pos=(980, 1240), now_mono=11.0) is None
        hit = watcher.scan_passive_hud_hover(_pil_to_bgr(img_aug), rect, set(), cursor_pos=(980, 1240), now_mono=11.35)
        assert hit is not None
        assert hit.augment.augment_id == "3163"
        assert hit.channel == "hud_hover_sync"
    finally:
        ocr.close()
