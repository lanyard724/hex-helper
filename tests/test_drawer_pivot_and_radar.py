"""Tests for explicit build-pivot banner, zero-new-window lobby radar auto-collapse, generalized augment items, and app wiring."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication

from hex_helper.bridge.protocol import CoachRequestPayload
from hex_helper.config import AppConfig
from hex_helper.data.provider import DatasetDescriptor, DestinyData, DestinyItemEntry, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.game.lobby_scout import LobbyScoutSnapshot, PlayerScoutCard
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.scoring.destiny import rank_destiny_items
from hex_helper.scoring.matchup_optimizer import (
    DynamicItemRec,
    MatchupThreatProfile,
    NextBuyPlan,
    build_next_buy_plan,
    optimize_next_items,
)
from hex_helper.ui.edge_drawer import EdgeBuildDrawer


def _qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _ResizeCounter(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self.count += 1
        return False


def _make_store_with_multi_augment_items(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(cache_dir=tmp_path, fetcher=lambda u: b"{}")
    static.items = {
        "3085": ItemStaticInfo("3085", "卢安娜的飓风", 2650, ("AttackSpeed", "CriticalStrike"), {"PercentAttackSpeedMod": 0.4, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3600, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3153": ItemStaticInfo("3153", "破败王者之刃", 3200, ("Damage", "AttackSpeed", "OnHit"), {"FlatPhysicalDamageMod": 40, "PercentAttackSpeedMod": 0.25}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130}, "", "complete_equipment"),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 100, "FlatMPPoolMod": 600}, "", "complete_equipment"),
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 110}, "", "complete_equipment"),
        "3110": ItemStaticInfo("3110", "冰霜之心", 2500, ("Armor", "Mana"), {"FlatArmorMod": 75, "FlatMPPoolMod": 400}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Health", "Armor"), {"FlatArmorMod": 75, "FlatHPPoolMod": 150}, "", "complete_equipment"),
        "6617": ItemStaticInfo("6617", "月石再生器", 2200, ("Health", "SpellDamage", "ManaRegen"), {"FlatMagicDamageMod": 25, "FlatHPPoolMod": 200}, "", "complete_equipment"),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900}, "", "complete_equipment"),
    }
    ds_dir = tmp_path / "16.23" / "normal"
    ds_dir.mkdir(parents=True, exist_ok=True)
    (ds_dir / "heroes.json").write_text(
        json.dumps({
            "222": {"name": "金克丝", "title": "暴走萝莉", "games": 100000, "wins": 50000, "win_rate": 0.50, "pick_rate": 0.05, "rank": 20},
            "99": {"name": "拉克丝", "title": "光辉女郎", "games": 100000, "wins": 51000, "win_rate": 0.51, "pick_rate": 0.05, "rank": 15},
            "36": {"name": "蒙多医生", "title": "祖安狂人", "games": 100000, "wins": 51000, "win_rate": 0.51, "pick_rate": 0.04, "rank": 25},
        }),
        encoding="utf-8",
    )
    (ds_dir / "augments.json").write_text(
        json.dumps({
            "3081": {"name": "命运之武器", "rarity": "gold"},
            "1205": {"name": "物理转魔法", "rarity": "prismatic"},
            "3049": {"name": "装甲冲击", "rarity": "gold"},
            "3163": {"name": "迷人蝴蝶", "rarity": "gold"},
            "1045": {"name": "炼狱导管", "rarity": "prismatic"},
            "1152": {"name": "钢化你心", "rarity": "gold"},
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_augments.json").write_text(json.dumps({"222": {}, "99": {}, "36": {}}), encoding="utf-8")
    (ds_dir / "core_builds.json").write_text(
        json.dumps({
            "222": [{"items": ["3085", "3031"], "ordered": True, "games": 50000, "wins": 26000, "win": 0.52, "pick": 0.3}],
            "99": [{"items": ["6655", "4645", "3089"], "ordered": True, "games": 50000, "wins": 26000, "win": 0.52, "pick": 0.3}],
            "36": [{"items": ["3084", "3075"], "ordered": True, "games": 50000, "wins": 26000, "win": 0.52, "pick": 0.3}],
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_item_recommendations.json").write_text(
        json.dumps({
            "222": {
                "3085": {"games": 20000, "wins": 10200, "win_rate": 0.51, "low_sample": False},
                "3031": {"games": 18000, "wins": 9360, "win_rate": 0.52, "low_sample": False},
                "3089": {"games": 1000, "wins": 560, "win_rate": 0.56, "low_sample": False},
            },
            "99": {
                "6655": {"games": 20000, "wins": 10400, "win_rate": 0.52, "low_sample": False},
                "4645": {"games": 18000, "wins": 9360, "win_rate": 0.52, "low_sample": False},
                "3089": {"games": 15000, "wins": 8100, "win_rate": 0.54, "low_sample": False},
            },
            "36": {
                "3084": {"games": 20000, "wins": 10400, "win_rate": 0.52, "low_sample": False},
            },
        }),
        encoding="utf-8",
    )
    (ds_dir / "hero_augment_items.json").write_text(
        json.dumps({
            "222": {
                "3081": {
                    "baseline": {"games": 100000, "wins": 50000},
                    "items": [
                        {"item": "3153", "name": "破败王者之刃", "games": 40000, "wins": 22000, "low_sample": False},
                    ],
                },
                "1205": {
                    "baseline": {"games": 12000, "wins": 6000},
                    "items": [
                        {"item": "3089", "name": "灭世者的死亡之帽", "games": 5000, "wins": 3100, "low_sample": False},
                        {"item": "6655", "games": 4000, "wins": 2300, "low_sample": False},
                    ],
                },
            },
            "99": {
                "3049": {
                    "baseline": {"games": 8000, "wins": 4000},
                    "items": [
                        {"item": "3110", "name": "冰霜之心", "games": 3200, "wins": 1850, "low_sample": False},
                    ],
                },
            },
        }),
        encoding="utf-8",
    )
    store = DataStore(static)
    store.load_from_dir(DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False), ds_dir)
    return store


def test_store_generalizes_hero_augment_items_while_preserving_destiny_api(tmp_path: Path) -> None:
    store = _make_store_with_multi_augment_items(tmp_path)

    # Destiny (3081) remains 100% compatible via both get_destiny_data and get_augment_item_data
    d_old = store.get_destiny_data("222")
    d_new = store.get_augment_item_data("222", "3081")
    assert d_old is not None and d_old == d_new
    assert d_old.items[0].item_id == "3153"
    assert rank_destiny_items(store, "222")[0].item_id == "3153"

    # Non-3081 augment_ids are indexed and queryable for any (hero_id, augment_id)
    ap_jinx = store.get_augment_item_data(222, 1205)
    assert ap_jinx is not None
    assert ap_jinx.baseline_games == 12000
    assert [it.item_id for it in ap_jinx.items] == ["3089", "6655"]
    assert ap_jinx.items[1].name == "卢登的伙伴"

    tank_lux = store.get_augment_item_data("99", "3049")
    assert tank_lux is not None and tank_lux.items[0].item_id == "3110"
    assert store.get_augment_item_data("99", "99999") is None


def test_build_next_buy_plan_surfaces_pivot_banner_only_on_true_archetype_shift(tmp_path: Path) -> None:
    store = _make_store_with_multi_augment_items(tmp_path)
    enemies = [PlayerLiveInfo("e1", "艾克", "Ekko", "CHAOS", False, 6, 1, 1, 1, ())]

    # 1. AD Jinx + 物理转魔法 (1205, shifts_profile=AP) -> explicit AD -> AP pivot banner
    threat_ap, recs_ap = optimize_next_items(store, "222", (), 1600.0, ["1205"], [], enemies, limit=4)
    plan_ap = build_next_buy_plan(
        store, "222", (), 1600.0, recs_ap, threat=threat_ap, selected_augments=["1205"]
    )
    assert plan_ap is not None
    assert plan_ap.pivot_from_label == "物理"
    assert "法强" in plan_ap.pivot_to_label
    assert "物理转魔法" in plan_ap.pivot_banner_text
    assert "法强" in plan_ap.pivot_banner_text
    assert "物理" in plan_ap.pivot_banner_text

    # 2. AP Lux + 装甲冲击 (3049, shifts_profile=TANK) -> explicit AP -> TANK pivot banner
    threat_tk, recs_tk = optimize_next_items(store, "99", (), 2500.0, ["3049"], [], enemies, limit=4)
    plan_tk = build_next_buy_plan(
        store, "99", (), 2500.0, recs_tk, threat=threat_tk, selected_augments=["3049"]
    )
    assert plan_tk is not None
    assert plan_tk.pivot_from_label == "法强"
    assert "坦克" in plan_tk.pivot_to_label
    assert "装甲冲击" in plan_tk.pivot_banner_text
    assert "坦克" in plan_tk.pivot_banner_text

    # 3. AP Lux + 迷人蝴蝶 (3163, transformative off-profile force_items=6617) -> explicit pivot banner
    threat_bf, recs_bf = optimize_next_items(store, "99", (), 2200.0, ["3163"], [], enemies, limit=4)
    plan_bf = build_next_buy_plan(
        store, "99", (), 2200.0, recs_bf, threat=threat_bf, selected_augments=["3163"]
    )
    assert plan_bf is not None
    assert "迷人蝴蝶" in plan_bf.pivot_banner_text

    # 4. AP Lux + 炼狱导管 (1045, shifts_profile=AP == default AP) -> NO false pivot banner
    threat_same, recs_same = optimize_next_items(store, "99", (), 2200.0, ["1045"], [], enemies, limit=4)
    plan_same = build_next_buy_plan(
        store, "99", (), 2200.0, recs_same, threat=threat_same, selected_augments=["1045"]
    )
    assert plan_same is not None
    assert plan_same.pivot_banner_text == ""

    # 5. TANK Mundo + 钢化你心 (1152, shifts_profile=TANK == default TANK, 3084 in core) -> NO false pivot banner
    threat_m, recs_m = optimize_next_items(store, "36", (), 2200.0, ["1152"], [], enemies, limit=4)
    plan_m = build_next_buy_plan(
        store, "36", (), 2200.0, recs_m, threat=threat_m, selected_augments=["1152"]
    )
    assert plan_m is not None
    assert plan_m.pivot_banner_text == ""


def test_edge_drawer_renders_pivot_banner_and_auto_collapses_radar_after_90s() -> None:
    app = _qt_app()
    drawer = EdgeBuildDrawer()
    drawer.set_expanded(True)
    drawer.show()

    mvp = PlayerScoutCard("p1", "人马", "120", "战争之影", "ORDER", 3970, "大师", 0.72, 18, 4.8, "6连胜", "", False, True)
    enemy = PlayerScoutCard("e1", "辛德拉", "134", "暗黑元首", "CHAOS", 3420, "钻一", 0.65, 20, 3.9, "", "车队A", False, False)
    snap = LobbyScoutSnapshot(
        my_team=(mvp,),
        enemy_team=(enemy,),
        mvp_card=mvp,
        mvp_lead_gap=550,
        top_enemy_card=enemy,
        premade_notes=("敌方2人车队",),
        degraded_count=0,
    )

    drawer.set_lobby_scout(snap, game_time_s=15.0)
    app.processEvents()
    assert not drawer.radar_card.isHidden()
    assert "3970" in drawer.radar_summary_lbl.text()

    # Signature deduplication: repeated identical set_lobby_scout causes ZERO resizes
    rc = _ResizeCounter()
    drawer.installEventFilter(rc)
    for _ in range(10):
        drawer.set_lobby_scout(snap, game_time_s=20.0)
        app.processEvents()
    assert rc.count == 0
    drawer.removeEventFilter(rc)

    # Toggle detail expands compact roster inside the same drawer (zero new window!)
    assert drawer.radar_detail_wrap.isHidden()
    drawer.radar_detail_btn.click()
    app.processEvents()
    assert not drawer.radar_detail_wrap.isHidden()

    # After 90s into live match, radar card auto-collapses so drawer stays razor-compact
    drawer.set_lobby_scout(snap, game_time_s=95.0)
    app.processEvents()
    assert drawer.radar_card.isHidden()
    assert "暗黑元首" in drawer.top_threat_lbl.text()

    # Live fed KDA threat takes priority over static lobby scout tag in top_threat_lbl
    drawer._threat = MatchupThreatProfile(
        enemy_armor_score=1.0,
        enemy_hp_score=1.0,
        enemy_mr_score=1.0,
        enemy_ad_ratio=0.6,
        enemy_ap_ratio=0.4,
        enemy_heal_score=0.5,
        enemy_shield_score=0.5,
        enemy_cc_score=0.5,
        ally_has_grievous=False,
        ally_has_shield_break=False,
        top_threat_name="影流之主",
        top_threat_kda="14/2/5",
        top_threat_type="AD",
        summary_tags=("头号威胁:影流之主(14/2/5)",),
    )

    # Build pivot banner surfaces clearly when special augment shifts archetype
    rec = DynamicItemRec("3089", "灭世者的死亡之帽", 3600, False, 1200, 9.5, 0.628, 20500, ("大法师核心",), ("AP",))
    plan = NextBuyPlan(
        target=rec,
        effective_cost=3600,
        affordable_now=False,
        gold_gap=1200,
        buy_now_hint="先买无用大棒(1200G)",
        remaining_build_path=("灭世者的死亡之帽", "残疫", "帝国指令"),
        remaining_build_ids=("3089", "3118", "4005"),
        pivot_banner_text="⚡ 已按【大法师】切换法强流（替代常规暴击）",
    )
    drawer._next_plan = plan
    drawer._render_decision_cards()
    app.processEvents()
    assert not drawer.pivot_banner_lbl.isHidden()
    assert "大法师" in drawer.pivot_banner_lbl.text()
    assert "影流之主" in drawer.top_threat_lbl.text()

    # Clearing lobby scout resets radar cleanly
    drawer.set_lobby_scout(None)
    app.processEvents()
    assert drawer.radar_card.isHidden()

    drawer.close()
    app.processEvents()


def test_app_runs_background_lobby_scout_once_per_match_and_attaches_coach_context(
    tmp_path: Path, monkeypatch
) -> None:
    import hex_helper.app as app_mod

    app = _qt_app()
    store = _make_store_with_multi_augment_items(tmp_path)

    mvp = PlayerScoutCard("p1", "我方金克丝", "222", "金克丝", "ORDER", 3620, "大师", 0.68, 16, 4.4, "4连胜", "", False, True)
    enemy = PlayerScoutCard("e1", "敌方拉克丝", "99", "拉克丝", "CHAOS", 3310, "钻一", 0.63, 18, 3.8, "", "车队A", False, False)
    fake_snap = LobbyScoutSnapshot(
        my_team=(mvp,),
        enemy_team=(enemy,),
        mvp_card=mvp,
        mvp_lead_gap=310,
        top_enemy_card=enemy,
        premade_notes=("敌方2人车队(拉克丝+蒙多医生)",),
        degraded_count=0,
    )

    scout_calls: list[tuple[object, object]] = []
    done_ev = threading.Event()

    def _fake_scout(lcu, st, my_puuid="", pace_s=0.08):
        scout_calls.append((lcu, st))
        done_ev.set()
        return fake_snap

    monkeypatch.setattr(app_mod, "scout_lobby_readonly", _fake_scout)

    # Lightweight controller instance without booting full tray/OCR/network timers
    ctrl = app_mod.HexHelperController.__new__(app_mod.HexHelperController)
    ctrl.cfg = AppConfig(lobby_scout_enabled=True, coach_mode="off", bridge_enabled=False)
    ctrl._commercial = False
    ctrl.store = store
    ctrl.lcu = object()  # type: ignore[assignment]
    ctrl.sm = app_mod.HexStateMachine()
    ctrl.sm.state = app_mod.GameState.IN_GAME
    ctrl.sm.current_hero_id = "222"
    ctrl._active_match_key = "222|ORDER:金克丝|CHAOS:拉克丝"
    ctrl._lobby_scout = None
    ctrl._lobby_scout_match_key = ""
    ctrl._stop_event = threading.Event()
    ctrl._last_live = LiveMatchSnapshot(
        game_time=20.0,
        my_player=PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 3, 0, 0, 0, ()),
        allies=(PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 3, 0, 0, 0, ()),),
        enemies=(PlayerLiveInfo("e1", "拉克丝", "Lux", "CHAOS", False, 3, 0, 0, 0, ()),),
        current_gold=1400.0,
    )
    ctrl._last_threat = None
    ctrl._last_dyn_recs = []
    ctrl._last_next_plan = None
    ctrl._last_combo_verdict = None
    ctrl._last_combo_plan = None
    ctrl._cloud_ai_cutoff = False
    ctrl._latest_advice = None
    ctrl._latest_request_id = ""
    ctrl._latest_request_trigger = ""
    ctrl._last_picker_gate_skipped_hint = ""
    ctrl.bridge = None
    ctrl.gemini_coach = None
    ctrl.cloud_client = None
    ctrl.cloud_coach = None
    ctrl.ui_invoker = app_mod.UiBridgeInvoker()
    ctrl.edge_drawer = EdgeBuildDrawer()
    ctrl.lookup_window = type("_DummyLookup", (), {"coach_page_body": type("_Lbl", (), {"text": lambda self: ""})()})()
    ctrl._cancel_death_coach_defer = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._cancel_picker_coach_debounce = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._is_direct_gemini_active = lambda: False  # type: ignore[method-assign]

    # Trigger twice for same match_key -> scout runs strictly ONCE
    ctrl._maybe_start_lobby_scout(ctrl._active_match_key)
    ctrl._maybe_start_lobby_scout(ctrl._active_match_key)
    assert done_ev.wait(timeout=2.0)
    deadline = time.monotonic() + 2.0
    while ctrl._lobby_scout is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert len(scout_calls) == 1
    assert ctrl._lobby_scout == fake_snap
    assert not ctrl.edge_drawer.radar_card.isHidden()
    assert "3620" in ctrl.edge_drawer.radar_summary_lbl.text()

    # CoachRequestPayload receives stripped zero-PII lobby_scout context
    req = ctrl.trigger_coach_request("manual_f6")
    assert isinstance(req, CoachRequestPayload)
    assert req.lobby_scout == fake_snap.to_coach_context()
    assert req.lobby_scout["top_enemy_champion"] == "拉克丝"
    assert req.lobby_scout["top_enemy_rating"] == 3310

    # Crossing 90s via set_live_vital_state auto-collapses radar card cleanly
    ctrl.edge_drawer.set_live_vital_state(is_dead=False, game_time_s=92.0)
    app.processEvents()
    assert ctrl.edge_drawer.radar_card.isHidden()
    assert "拉克丝" in ctrl.edge_drawer.top_threat_lbl.text()

    # Stale scout callback from an old match_key is safely ignored
    ctrl._active_match_key = "different_match_key"
    ctrl._on_lobby_scout_ready("old_match_key", fake_snap)
    assert ctrl.edge_drawer.radar_card.isHidden()

    ctrl.edge_drawer.close()
    app.processEvents()


def _make_lightweight_controller(tmp_path: Path, store: DataStore, *, lcu: object):
    import hex_helper.app as app_mod
    from PySide6.QtCore import QTimer

    ctrl = app_mod.HexHelperController.__new__(app_mod.HexHelperController)
    ctrl.cfg = AppConfig(
        lobby_scout_enabled=True,
        auto_trigger_coach=True,
        auto_peek_on_death=False,
        combo_overlay_enabled=False,
        coach_mode="off",
        bridge_enabled=False,
    )
    ctrl._commercial = False
    ctrl.store = store
    ctrl.lcu = lcu  # type: ignore[assignment]
    ctrl._lcu_lock = threading.RLock()
    ctrl.sm = app_mod.HexStateMachine()
    ctrl._active_match_key = ""
    ctrl._lobby_scout = None
    ctrl._lobby_scout_match_key = ""
    ctrl._lobby_scout_in_flight = False
    ctrl._pending_game_start_match_key = ""
    ctrl._stop_event = threading.Event()
    ctrl._last_live = None
    ctrl._last_cs = None
    ctrl._last_threat = None
    ctrl._last_dyn_recs = []
    ctrl._last_next_plan = None
    ctrl._last_combo_verdict = None
    ctrl._last_combo_plan = None
    ctrl._overlays_hidden_by_user = True
    ctrl._combo_hidden_by_user = True
    ctrl._cloud_ai_cutoff = False
    ctrl._latest_advice = None
    ctrl._latest_request_id = ""
    ctrl._latest_request_trigger = ""
    ctrl._last_picker_gate_skipped_hint = ""
    ctrl._match_max_game_time_s = 0.0
    ctrl._match_won_result = -1
    ctrl._match_last_my_items = ()
    ctrl._match_last_hero_id = ""
    ctrl._match_last_hero_name = ""
    ctrl._last_kda = ""
    ctrl._last_team_kills = ""
    ctrl._auto_switched_first_prismatic = False
    ctrl.detector = None
    ctrl.transmute_watcher = None
    ctrl.bridge = None
    ctrl.gemini_coach = None
    ctrl.cloud_client = None
    ctrl.cloud_coach = None
    ctrl.ui_invoker = app_mod.UiBridgeInvoker()
    ctrl.edge_drawer = EdgeBuildDrawer()
    ctrl.champ_overlay = type("_DummyOverlay", (), {"hide": lambda self: None, "isVisible": lambda self: False})()
    ctrl.combo_overlay = type(
        "_DummyCombo",
        (),
        {
            "exit_picker_mode": lambda self: None,
            "hide": lambda self: None,
            "isVisible": lambda self: False,
            "set_target_screen": lambda self, _s: None,
            "set_plan": lambda self, _p: None,
        },
    )()
    ctrl.augment_overlay = type("_DummyAug", (), {"hide": lambda self: None, "isVisible": lambda self: False})()
    ctrl.lookup_window = type(
        "_DummyLookup",
        (),
        {
            "coach_page_body": type("_Lbl", (), {"text": lambda self: "", "setText": lambda self, _t: None})(),
            "set_game_active": lambda self, _b: None,
        },
    )()
    ctrl.log_ring = type(
        "_DummyLogRing",
        (),
        {
            "begin_match": lambda self, *_a, **_kw: None,
            "update_match_meta": lambda self, *_a, **_kw: None,
            "end_match": lambda self, *_a, **_kw: None,
        },
    )()
    ctrl._picker_coach_debounce_timer = QTimer()
    ctrl._picker_coach_debounce_timer.setSingleShot(True)
    ctrl._death_coach_defer_timer = QTimer()
    ctrl._death_coach_defer_timer.setSingleShot(True)
    ctrl._game_start_coach_defer_timer = QTimer()
    ctrl._game_start_coach_defer_timer.setSingleShot(True)
    ctrl._game_start_coach_defer_timer.setInterval(ctrl.GAME_START_SCOUT_WAIT_MS)
    ctrl._game_start_coach_defer_timer.timeout.connect(ctrl._on_game_start_coach_defer_timeout)
    ctrl._cancel_pending_icon_verify = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._restore_configured_dataset_mode = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._reset_match_telemetry_counters = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._restore_match_state_if_matching = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._save_match_state = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._clear_match_state = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._maybe_submit_match_telemetry = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._capture_post_match_recap = lambda *_a, **_kw: None  # type: ignore[method-assign]
    ctrl._is_any_coach_online = lambda: True  # type: ignore[method-assign]
    ctrl._is_direct_gemini_active = lambda: False  # type: ignore[method-assign]
    ctrl._is_full_locked = lambda: False  # type: ignore[method-assign]
    return ctrl


def _sample_live_snap() -> LiveMatchSnapshot:
    me = PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 3, 0, 0, 0, ())
    enemy = PlayerLiveInfo("e1", "拉克丝", "Lux", "CHAOS", False, 3, 0, 0, 0, ())
    return LiveMatchSnapshot(
        game_time=8.0,
        my_player=me,
        allies=(me,),
        enemies=(enemy,),
        current_gold=1400.0,
    )


def _sample_scout_snap() -> LobbyScoutSnapshot:
    mvp = PlayerScoutCard("p1", "我方金克丝", "222", "金克丝", "ORDER", 3620, "大师", 0.68, 16, 4.4, "4连胜", "", False, True)
    enemy = PlayerScoutCard("e1", "敌方拉克丝", "99", "拉克丝", "CHAOS", 3310, "钻一", 0.63, 18, 3.8, "", "车队A", False, False)
    return LobbyScoutSnapshot(
        my_team=(mvp,),
        enemy_team=(enemy,),
        mvp_card=mvp,
        mvp_lead_gap=310,
        top_enemy_card=enemy,
        premade_notes=("敌方2人车队(拉克丝+蒙多医生)",),
        degraded_count=0,
    )


def test_game_start_defers_until_lobby_scout_ready_and_populates_payload(tmp_path: Path, monkeypatch) -> None:
    import hex_helper.app as app_mod

    app = _qt_app()
    store = _make_store_with_multi_augment_items(tmp_path)
    fake_snap = _sample_scout_snap()
    release_scout = threading.Event()
    scout_started = threading.Event()

    def _slow_scout(_lcu, _st, my_puuid="", pace_s=0.08):
        scout_started.set()
        release_scout.wait(timeout=2.0)
        return fake_snap

    monkeypatch.setattr(app_mod, "scout_lobby_readonly", _slow_scout)
    monkeypatch.setattr(app_mod, "find_game_window", lambda: None)

    ctrl = _make_lightweight_controller(tmp_path, store, lcu=object())
    captured_reqs: list[CoachRequestPayload] = []
    orig_trigger = ctrl.trigger_coach_request

    def _spy_trigger(reason: str = "manual_f6"):
        req = orig_trigger(reason)
        if req is not None:
            captured_reqs.append(req)
        return req

    ctrl.trigger_coach_request = _spy_trigger  # type: ignore[method-assign]

    # First live snapshot enters IN_GAME with eff.coach_trigger == "game_start"
    ctrl._apply_live_snapshot(_sample_live_snap())
    assert scout_started.wait(timeout=1.0)
    # MUST NOT fire game_start prematurely while LobbyScout is still running!
    assert captured_reqs == []
    assert ctrl._game_start_coach_defer_timer.isActive()

    # Release LobbyScout -> _on_lobby_scout_ready immediately flushes game_start with full lobby_scout context
    release_scout.set()
    deadline = time.monotonic() + 2.0
    while not captured_reqs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert len(captured_reqs) == 1
    assert captured_reqs[0].trigger_reason == "game_start"
    assert captured_reqs[0].lobby_scout == fake_snap.to_coach_context()
    assert captured_reqs[0].lobby_scout["top_enemy_champion"] == "拉克丝"
    assert not ctrl._game_start_coach_defer_timer.isActive()

    ctrl.edge_drawer.close()
    app.processEvents()


def test_game_start_flushes_immediately_when_lobby_scout_returns_none(tmp_path: Path, monkeypatch) -> None:
    import hex_helper.app as app_mod

    app = _qt_app()
    store = _make_store_with_multi_augment_items(tmp_path)
    release_scout = threading.Event()

    def _empty_scout(_lcu, _st, my_puuid="", pace_s=0.08):
        release_scout.wait(timeout=2.0)
        return None

    monkeypatch.setattr(app_mod, "scout_lobby_readonly", _empty_scout)
    monkeypatch.setattr(app_mod, "find_game_window", lambda: None)

    ctrl = _make_lightweight_controller(tmp_path, store, lcu=object())
    ctrl.GAME_START_SCOUT_WAIT_MS = 5000  # Long timeout proves flush came from callback, not timer
    captured_reqs: list[CoachRequestPayload] = []
    orig_trigger = ctrl.trigger_coach_request

    def _spy_trigger(reason: str = "manual_f6"):
        req = orig_trigger(reason)
        if req is not None:
            captured_reqs.append(req)
        return req

    ctrl.trigger_coach_request = _spy_trigger  # type: ignore[method-assign]

    t0 = time.monotonic()
    ctrl._apply_live_snapshot(_sample_live_snap())
    assert captured_reqs == []
    release_scout.set()

    deadline = time.monotonic() + 1.5
    while not captured_reqs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    elapsed = time.monotonic() - t0
    assert len(captured_reqs) == 1
    assert captured_reqs[0].trigger_reason == "game_start"
    assert captured_reqs[0].lobby_scout == {}
    assert elapsed < 1.5  # Flushed immediately on None without waiting for 5s timer
    assert not ctrl._game_start_coach_defer_timer.isActive()

    ctrl.edge_drawer.close()
    app.processEvents()


def test_game_start_timeout_fallback_fires_once_and_late_scout_does_not_duplicate(
    tmp_path: Path, monkeypatch
) -> None:
    import hex_helper.app as app_mod

    app = _qt_app()
    store = _make_store_with_multi_augment_items(tmp_path)
    fake_snap = _sample_scout_snap()
    release_scout = threading.Event()

    def _hanging_scout(_lcu, _st, my_puuid="", pace_s=0.08):
        release_scout.wait(timeout=3.0)
        return fake_snap

    monkeypatch.setattr(app_mod, "scout_lobby_readonly", _hanging_scout)
    monkeypatch.setattr(app_mod, "find_game_window", lambda: None)

    ctrl = _make_lightweight_controller(tmp_path, store, lcu=object())
    ctrl.GAME_START_SCOUT_WAIT_MS = 90
    captured_reqs: list[CoachRequestPayload] = []
    orig_trigger = ctrl.trigger_coach_request

    def _spy_trigger(reason: str = "manual_f6"):
        req = orig_trigger(reason)
        if req is not None:
            captured_reqs.append(req)
        return req

    ctrl.trigger_coach_request = _spy_trigger  # type: ignore[method-assign]

    ctrl._apply_live_snapshot(_sample_live_snap())
    assert captured_reqs == []

    # Wait for bounded timeout fallback to fire game_start with empty lobby_scout
    deadline = time.monotonic() + 1.5
    while not captured_reqs and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert len(captured_reqs) == 1
    assert captured_reqs[0].trigger_reason == "game_start"
    assert captured_reqs[0].lobby_scout == {}

    # When hanging scout eventually finishes later, drawer updates radar, but game_start MUST NOT fire twice!
    release_scout.set()
    deadline = time.monotonic() + 1.5
    while ctrl._lobby_scout is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert ctrl._lobby_scout == fake_snap
    assert len(captured_reqs) == 1

    ctrl.edge_drawer.close()
    app.processEvents()


def test_game_start_defer_cancelled_by_manual_f6_and_match_end(tmp_path: Path, monkeypatch) -> None:
    import hex_helper.app as app_mod

    app = _qt_app()
    store = _make_store_with_multi_augment_items(tmp_path)
    release_scout = threading.Event()

    def _slow_scout(_lcu, _st, my_puuid="", pace_s=0.08):
        release_scout.wait(timeout=2.0)
        return _sample_scout_snap()

    monkeypatch.setattr(app_mod, "scout_lobby_readonly", _slow_scout)
    monkeypatch.setattr(app_mod, "find_game_window", lambda: None)

    ctrl = _make_lightweight_controller(tmp_path, store, lcu=object())
    ctrl.GAME_START_SCOUT_WAIT_MS = 120
    reasons: list[str] = []
    orig_trigger = ctrl.trigger_coach_request

    def _spy_trigger(reason: str = "manual_f6"):
        reasons.append(reason)
        return orig_trigger(reason)

    ctrl.trigger_coach_request = _spy_trigger  # type: ignore[method-assign]

    # 1. Manual F6 cancels deferred game_start
    ctrl._apply_live_snapshot(_sample_live_snap())
    assert ctrl._game_start_coach_defer_timer.isActive()
    ctrl.trigger_coach_request("manual_f6")
    assert not ctrl._game_start_coach_defer_timer.isActive()
    release_scout.set()
    t_end = time.monotonic() + 0.22
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.01)
    assert reasons == ["manual_f6"]

    # 2. Match end (_apply_lcu_poll) cancels deferred game_start
    release_scout.clear()
    ctrl.sm.reset_match()
    ctrl.sm.state = app_mod.GameState.IDLE
    ctrl._active_match_key = ""
    ctrl._apply_live_snapshot(_sample_live_snap())
    assert ctrl._game_start_coach_defer_timer.isActive()
    ctrl._apply_lcu_poll("Lobby", None)
    assert not ctrl._game_start_coach_defer_timer.isActive()
    release_scout.set()
    t_end = time.monotonic() + 0.22
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.01)
    assert reasons == ["manual_f6"]

    ctrl.edge_drawer.close()
    app.processEvents()


def test_lobby_scout_uses_isolated_lcu_client_and_closes_cleanly(tmp_path: Path, monkeypatch) -> None:
    import hex_helper.app as app_mod
    from hex_helper.game.lcu_client import LcuClient

    app = _qt_app()
    store = _make_store_with_multi_augment_items(tmp_path)
    shared_lcu = LcuClient(preferred_lockfile="custom_lockfile", force_all_modes=True)

    seen_lcus: list[object] = []
    closed_lcus: list[object] = []
    done_ev = threading.Event()

    orig_close = LcuClient.close

    def _spy_close(self) -> None:
        closed_lcus.append(self)
        orig_close(self)

    def _fake_scout(lcu, _st, my_puuid="", pace_s=0.08):
        seen_lcus.append(lcu)
        # Simulate concurrent main/telemetry thread resetting shared_lcu while scout runs
        with ctrl._lcu_lock:
            shared_lcu.creds = None
            shared_lcu.close()
        done_ev.set()
        return _sample_scout_snap()

    monkeypatch.setattr(LcuClient, "close", _spy_close)
    monkeypatch.setattr(app_mod, "scout_lobby_readonly", _fake_scout)

    ctrl = _make_lightweight_controller(tmp_path, store, lcu=shared_lcu)
    ctrl.sm.state = app_mod.GameState.IN_GAME
    ctrl._active_match_key = "match-iso-1"
    ctrl._maybe_start_lobby_scout("match-iso-1")

    assert done_ev.wait(timeout=2.0)
    deadline = time.monotonic() + 2.0
    while (ctrl._lobby_scout is None or len(seen_lcus) == 0) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    # Wait briefly for finally: scout_lcu.close() to complete
    deadline = time.monotonic() + 1.0
    while seen_lcus[0] not in closed_lcus and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(seen_lcus) == 1
    scout_lcu = seen_lcus[0]
    assert isinstance(scout_lcu, LcuClient)
    assert scout_lcu is not shared_lcu
    assert scout_lcu.preferred_lockfile == "custom_lockfile"
    assert scout_lcu.force_all_modes is True
    assert scout_lcu in closed_lcus

    ctrl.edge_drawer.close()
    app.processEvents()


def test_lcu_proxy_serializes_injected_lcu_and_shutdown_cancels_game_start_timer(
    tmp_path: Path, monkeypatch
) -> None:
    import hex_helper.app as app_mod

    app = _qt_app()
    store = _make_store_with_multi_augment_items(tmp_path)

    active_calls = 0
    overlap_detected = False
    state_lock = threading.Lock()

    class _ConcurrentCheckLcu:
        def get_gameflow_session(self) -> dict:
            nonlocal active_calls, overlap_detected
            with state_lock:
                active_calls += 1
                if active_calls > 1:
                    overlap_detected = True
            time.sleep(0.03)
            with state_lock:
                active_calls -= 1
            return {"gameData": {"teamOne": [], "teamTwo": []}}

        def poll(self):
            nonlocal active_calls, overlap_detected
            with state_lock:
                active_calls += 1
                if active_calls > 1:
                    overlap_detected = True
            time.sleep(0.03)
            with state_lock:
                active_calls -= 1
            return "InProgress", None

        def get_ranked_stats(self, _puuid: str):
            return None

        def get_match_history(self, _puuid: str, count: int = 15):
            return []

    fake_lcu = _ConcurrentCheckLcu()
    ctrl = _make_lightweight_controller(tmp_path, store, lcu=fake_lcu)
    proxy, should_close = ctrl._make_lobby_scout_lcu()
    assert should_close is False

    t1 = threading.Thread(target=proxy.get_gameflow_session)

    def _poll_under_lock() -> None:
        with ctrl._get_lcu_lock():
            ctrl.lcu.poll()

    t2 = threading.Thread(target=_poll_under_lock)
    t1.start()
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert overlap_detected is False

    # Verify shutdown/cancel stops active deferred game_start timer cleanly
    ctrl._schedule_game_start_coach_defer("match-shutdown-1")
    assert ctrl._game_start_coach_defer_timer.isActive()
    ctrl._cancel_game_start_coach_defer("shutdown")
    assert not ctrl._game_start_coach_defer_timer.isActive()
    assert ctrl._pending_game_start_match_key == ""

    ctrl.edge_drawer.close()
    app.processEvents()

