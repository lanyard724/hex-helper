"""Tests for v1.5.2 hero resolution priority, scoring parity, localized Anvil/Transmute cues & Riot lockfile hint."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

from hex_helper import app as app_mod
from hex_helper.app import HexHelperController
from hex_helper.data.locale_pack import get_locale_catalog
from hex_helper.data.provider import AugmentInfo, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.lol_locator import is_riot_overseas_lockfile
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.scoring.augment_combo import build_combo_plan
from hex_helper.scoring.matchup_optimizer import optimize_next_items
from hex_helper.vision.capture import CaptureResult, WindowRect
from hex_helper.vision.detector import _is_stat_anvil_lines
from hex_helper.vision.ocr_winrt import OcrLine
from hex_helper.vision.transmute import PendingTransmute, TransmuteRevealWatcher

LIVE_1623_NORMAL = Path(
    os.environ.get(
        "HEX_HELPER_LIVE_1623_NORMAL",
        r"C:\Users\Administrator\AppData\Local\HexHelper\cache\datasets\16.23\normal",
    )
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


@pytest.mark.skipif(
    not (LIVE_1623_NORMAL / "heroes.json").is_file(),
    reason="Real 16.23/normal dataset not present locally",
)
def test_overseas_173_heroes_resolve_and_scoring_parity(tmp_path: Path) -> None:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda _u: b"{}")
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False),
        LIVE_1623_NORMAL,
    )
    all_heroes = store.all_heroes()
    assert len(all_heroes) == 173

    for loc in ("zh_my", "zh_tw"):
        hero_map = get_locale_catalog(loc)["heroes"]
        for h in all_heroes:
            loc_name = hero_map[h.hero_id]
            by_cn = store.find_hero_by_name(h.name)
            by_alias = store.find_hero_by_name(loc_name)
            assert by_cn is not None and by_cn.hero_id == h.hero_id
            assert by_alias is not None and by_alias.hero_id == h.hero_id, (
                f"{loc} alias failed: {h.hero_id} {loc_name}"
            )

    # Numeric scoring parity across locales (zero mutation of internal CN engine)
    enemies = (
        PlayerLiveInfo("e1", "奧恩", "Ornn", "CHAOS", False, 10, 2, 1, 5, ("3084", "3068")),
        PlayerLiveInfo("e2", "雷茲", "Ryze", "CHAOS", False, 10, 4, 2, 4, ("3040", "3089")),
    )
    snap = LiveMatchSnapshot(
        game_time=480.0,
        my_player=PlayerLiveInfo("me", "阿璃", "Ahri", "ORDER", False, 10, 3, 1, 6, ("6655",)),
        allies=(),
        enemies=enemies,
        current_gold=2600.0,
    )

    store.set_client_locale("zh_cn")
    cn_threat, cn_recs = optimize_next_items(store, "103", ("6655",), 2600.0, [], snap.allies, snap.enemies)
    cn_plan = build_combo_plan(store, "103", ["1048"], ("6655",), threat=cn_threat)

    for loc in ("zh_my", "zh_tw"):
        store.set_client_locale(loc)
        loc_threat, loc_recs = optimize_next_items(store, "103", ("6655",), 2600.0, [], snap.allies, snap.enemies)
        loc_plan = build_combo_plan(store, "103", ["1048"], ("6655",), threat=loc_threat)
        assert [r.item_id for r in loc_recs] == [r.item_id for r in cn_recs]
        assert [round(r.score, 4) for r in loc_recs] == [round(r.score, 4) for r in cn_recs]
        assert [t.augment_id for t in loc_plan.recommended_targets] == [
            t.augment_id for t in cn_plan.recommended_targets
        ]


def test_app_resolves_raw_champion_name_before_localized_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp: QApplication
) -> None:
    class _NoopCapturer:
        backend = "noop"
        consecutive_black_frames = 0

        def grab(self, **_kw):
            return CaptureResult(frame_bgr=None, backend="noop", is_black=True, mean_luma=0.0)

        def close(self):
            return None

    class _NoopHotkeys(QAbstractNativeEventFilter):
        last_failed_hotkeys = {}

        def nativeEventFilter(self, *_a):
            return False, 0

        def register(self, *_a, **_kw):
            return True

        def unregister_all(self):
            return None

    from hex_helper.config import AppConfig

    monkeypatch.setattr(app_mod, "get_app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(app_mod, "load_config", lambda *_a, **_kw: AppConfig())
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)

    ctrl = HexHelperController(
        qt_app=qapp,
        show_lookup_on_start=False,
        devlab_enabled=False,
        devlab_roi_snapshots=False,
        devlab_debug_log=False,
    )
    try:
        ctrl.store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "", "", False)
        ctrl.store._heroes = {
            "103": HeroStat("103", "阿狸", "九尾妖狐", 50000, 26000, 0.52, 0.05, 10, False),
            "11": HeroStat("11", "易", "无极剑圣", 40000, 20000, 0.50, 0.04, 50, False),
        }
        ctrl.store._augments = {"1048": AugmentInfo("1048", "珠光护手", "prismatic")}
        ctrl.store._hero_name_index = {
            "阿狸": "103",
            "ahri": "103",
            "阿璃": "103",
            "易": "11",
            "masteryi": "11",
            "ambiguousname": "11",
        }

        # Both raw_champion_name and champion_name match -> raw_champion_name wins first ("103", NOT "11")
        p = PlayerLiveInfo("me", "AmbiguousName", "Ahri", "ORDER", False, 6, 1, 0, 1, ())
        resolved = ctrl._resolve_hero_from_live_player(p)
        assert resolved is not None and resolved.hero_id == "103"

        # Empty raw_champion_name falls back cleanly to secondary TW/MY alias index
        p_tw = PlayerLiveInfo("e1", "阿璃", "", "CHAOS", False, 6, 1, 0, 1, ())
        resolved_tw = ctrl._resolve_hero_from_live_player(p_tw)
        assert resolved_tw is not None and resolved_tw.hero_id == "103"

        # Riot overseas lockfile path helper + diagnostics hint
        assert is_riot_overseas_lockfile(r"C:\Riot Games\League of Legends\lockfile") is True
        assert is_riot_overseas_lockfile(r"D:\WeGameApps\英雄联盟\LeagueClient\lockfile") is False
        ctrl.cfg.lockfile_path = r"C:\Riot Games\League of Legends\lockfile"
        ctrl.cfg.client_locale = "zh_cn"
        ctrl._refresh_diagnostics()
        assert "Riot Games" in ctrl.lookup_window.diag_text.toPlainText()
    finally:
        ctrl.shutdown()
        qapp.processEvents()


def test_stat_anvil_and_transmute_cues_across_my_and_tw() -> None:
    def _line(txt: str, y: float = 100.0) -> OcrLine:
        return OcrLine(text=txt, x=100.0, y=y, w=220.0, h=28.0)

    # Stat anvil cues across CN / MY / TW
    assert _is_stat_anvil_lines([_line("属性锻造器")], "zh_cn") is True
    assert _is_stat_anvil_lines([_line("属性铁砧")], "zh_my") is True
    assert _is_stat_anvil_lines([_line("能力值鐵砧")], "zh_tw") is True
    assert _is_stat_anvil_lines([_line("屬性鐵砧")], "zh_tw") is True
    assert _is_stat_anvil_lines([_line("物理 碎片", 100.0), _line("護甲 碎片", 130.0)], "zh_tw") is True
    assert _is_stat_anvil_lines([_line("珠光護手")], "zh_tw") is False

    # Transmute cues + Shinjitai/Traditional WinRT zh-Hans-CN fallback matching
    class _DummyOcr:
        def recognize_lines(self, *_a, **_kw):
            return []

    watcher_tw = TransmuteRevealWatcher(
        _DummyOcr(),  # type: ignore[arg-type]
        [
            AugmentInfo("1238", "質變：稜鏡", "gold"),
            AugmentInfo("1349", "大絕覺醒", "prismatic"),
            AugmentInfo("1048", "珠光護手", "prismatic"),
        ],
        client_locale="zh_tw",
        use_t2s=True,
    )
    assert watcher_tw._infer_rarity_hint([_line("稜鏡 增幅裝置")]) == "prismatic"
    assert watcher_tw._infer_rarity_hint([_line("金級")]) == "gold"
    assert watcher_tw._infer_rarity_hint([_line("銀級")]) == "silver"

    pending_tw = PendingTransmute(
        source_augment_id="1238",
        source_name="質變：稜鏡",
        expected_count=1,
        target_rarity="prismatic",
        started_mono=1.0,
    )
    chat_hits = watcher_tw._scan_chat_lines(
        [_line("系統：獲得了 【大 絶 覺 醒】")],
        watcher_tw._all_augments,
        matcher=SimpleNamespace(match_line=lambda *_a, **_k: None),  # not needed when direct substring matches
        pending=pending_tw,
        now_mono=2.0,
    )
    assert len(chat_hits) == 1 and chat_hits[0].augment.augment_id == "1349"

    # Passive hover tooltip without rarity label still passes on '增幅裝置' noun cue
    from hex_helper.vision.matcher import AugmentMatcher

    m_tw = AugmentMatcher(watcher_tw._all_augments, use_t2s=True)
    tooltip_lines = [
        _line("大 絶 覺 醒", y=100.0),
        _line("增幅裝置 · 獲得終極技能冷卻縮減並重置", y=140.0),
    ]
    res_tip = watcher_tw._scan_reveal_or_tooltip_lines(
        tooltip_lines, m_tw, pending_tw, "hover_tooltip", now_mono=3.0, cursor_zone="bottom_hud"
    )
    assert len(res_tip) == 1 and res_tip[0].augment.augment_id == "1349"

    watcher_my = TransmuteRevealWatcher(
        _DummyOcr(),  # type: ignore[arg-type]
        [AugmentInfo("1048", "珠光护手", "prismatic")],
        client_locale="zh_my",
        use_t2s=False,
    )
    assert watcher_my._infer_rarity_hint([_line("银色 增幅装置")]) == "silver"
    assert watcher_my._infer_rarity_hint([_line("金色 增幅装置")]) == "gold"
    assert watcher_my._infer_rarity_hint([_line("棱彩 增幅装置")]) == "prismatic"
