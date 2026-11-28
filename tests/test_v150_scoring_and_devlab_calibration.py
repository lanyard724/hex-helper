"""WS-D v1.5.0 regression tests:
1. Shen 1-second build-plan ping-pong fix (matchup_optimizer.py)
2. ActionTracker 6-digit augment-item & pre-death warmup-served accounting (action_tracker.py)
3. DevLab worker-vs-main-thread stall separation, warmup timely rate & 6-digit item adoption calibration
   (recorder.py + analyzer.py, including historical log backward compatibility)
4. EdgeBuildDrawer +20px breathing cushion & QTimer.singleShot(0) post-layout height sync (native Windows Qt)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload, utc_now_iso
from hex_helper.data.provider import AugmentInfo, CoreBuild, DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.devlab.analyzer import (
    DevLabAnalyzer,
    _is_augment_granted_special_item_id,
    _is_background_perf_op as analyzer_is_bg_perf_op,
)
from hex_helper.devlab.recorder import (
    DevLabFlightRecorder,
    _is_background_perf_op as recorder_is_bg_perf_op,
)
from hex_helper.devlab.schema import (
    DOMAIN_COACH,
    DOMAIN_GAME,
    DOMAIN_PERF,
    DOMAIN_REC,
    EV_DEATH_SHOP_WINDOW,
    EV_ITEM_PURCHASE_DELTA,
    EV_MAIN_THREAD_STALL,
    EV_MATCH_EOG,
    hash_match_key,
)
from hex_helper.game.action_tracker import MatchActionTracker, _is_augment_granted_special_item
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.scoring.matchup_optimizer import (
    DynamicItemRec,
    MatchupThreatProfile,
    NextBuyPlan,
    reconcile_match_build_plan,
    reconcile_match_build_plan_with_trace,
)
from hex_helper.ui.edge_drawer import EdgeBuildDrawer


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def _make_shen_tank_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", fetcher=lambda _u: b"")
    static.items = {
        # Components for 2502 (Unending Despair): Chain Vest 1031 (800G) + Kindlegem 3067 (800G) = 1600G
        "1031": ItemStaticInfo("1031", "锁子甲", 800, ("Armor",), {"FlatArmorMod": 40.0}, "", "other"),
        "3067": ItemStaticInfo("3067", "燃烧宝石", 800, ("Health",), {"FlatHPPoolMod": 200.0}, "", "other"),
        "1028": ItemStaticInfo("1028", "红水晶", 400, ("Health",), {"FlatHPPoolMod": 150.0}, "", "other"),
        "1058": ItemStaticInfo("1058", "无用大棒", 1200, ("SpellDamage",), {"FlatMagicDamageMod": 65.0}, "", "other"),
        # Tank complete items
        "2502": ItemStaticInfo(
            "2502",
            "无终恨意",
            2800,
            ("Health", "Armor"),
            {"FlatHPPoolMod": 400.0, "FlatArmorMod": 60.0},
            "",
            "complete_equipment",
            components=("1031", "3067", "1028"),
        ),
        "2504": ItemStaticInfo(
            "2504",
            "败魔",
            2900,
            ("Health", "SpellBlock"),
            {"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 80.0},
            "",
            "complete_equipment",
            components=("1028",),
        ),
        "3748": ItemStaticInfo(
            "3748",
            "巨型九头蛇",
            3300,
            ("Health", "Damage"),
            {"FlatHPPoolMod": 600.0, "FlatPhysicalDamageMod": 40.0},
            "",
            "complete_equipment",
            components=("1028",),
        ),
        "3068": ItemStaticInfo(
            "3068",
            "日炎圣盾",
            2700,
            ("Health", "Armor"),
            {"FlatHPPoolMod": 350.0, "FlatArmorMod": 50.0},
            "",
            "complete_equipment",
            components=("1031",),
        ),
        "4401": ItemStaticInfo(
            "4401",
            "自然之力",
            2800,
            ("Health", "SpellBlock"),
            {"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 55.0},
            "",
            "complete_equipment",
            components=("1028",),
        ),
        "3089": ItemStaticInfo(
            "3089",
            "灭世者的死亡之帽",
            3600,
            ("SpellDamage",),
            {"FlatMagicDamageMod": 130.0},
            "",
            "complete_equipment",
            components=("1058", "1058"),
        ),
        "3083": ItemStaticInfo(
            "3083",
            "狂徒铠甲",
            3100,
            ("Health",),
            {"FlatHPPoolMod": 1000.0},
            "",
            "complete_equipment",
            components=("1028",),
        ),
        "3047": ItemStaticInfo("3047", "铁板靴", 1200, ("Boots", "Armor"), {"FlatArmorMod": 25.0}, "", "boots"),
    }
    store = DataStore(static)
    store._descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes["98"] = HeroStat("98", "慎", "暮光之眼", 42000, 22260, 0.53, 0.05, 18, False)
    store._core_builds["98"] = [CoreBuild(("2502", "2504", "3748"), True, 9000, 4800, 0.533, 0.35)]
    store._augments["1319"] = AugmentInfo("1319", "升级：献祭", "gold", "升级日炎圣盾")
    store._augments["1001"] = AugmentInfo("1001", "珠光护手", "prismatic", "技能可暴击")
    store._augments["1002"] = AugmentInfo("1002", "尤里卡", "prismatic", "法强转急速")
    return store


def _make_player(
    champion: str,
    raw_name: str,
    team: str = "ORDER",
    *,
    is_dead: bool = False,
    level: int = 10,
    kills: int = 2,
    deaths: int = 1,
    assists: int = 4,
    items: tuple[str, ...] = (),
    item_details: list[dict[str, Any]] | None = None,
) -> PlayerLiveInfo:
    return PlayerLiveInfo(
        riot_id="anon",
        champion_name=champion,
        raw_champion_name=raw_name,
        team=team,
        is_dead=is_dead,
        level=level,
        kills=kills,
        deaths=deaths,
        assists=assists,
        item_ids=items,
        item_details=item_details or [],
    )


# ---------------------------------------------------------------------------
# 1. Shen 1-second build-plan ping-pong fix
# ---------------------------------------------------------------------------


def test_shen_1600g_component_lock_prevents_overhaul_ping_pong(tmp_path: Path) -> None:
    store = _make_shen_tank_store(tmp_path)

    # Exact m_00013 Shen state: player holds Chain Vest (1031, 800G) + Kindlegem (3067, 800G) = 1600G
    # for cur_lock="2502". Coach returns allow_overhaul=True with ["2504", "2502", "3748"]
    # ("2502" is STILL in clean_proposed).
    my_items = ("1031", "3067")
    committed = ["2502", "2504", "4401", "3748", "3068"]
    proposed = ["2504", "2502", "3748"]

    for tx_flag in (False, True):
        lock_plain, plan_plain = reconcile_match_build_plan(
            store=store,
            my_items=my_items,
            committed_plan=committed,
            locked_next_item="2502",
            proposed_ids=proposed,
            allow_overhaul=True,
            transformative_overhaul=tx_flag,
        )
        lock_tr, plan_tr, trace = reconcile_match_build_plan_with_trace(
            store=store,
            my_items=my_items,
            committed_plan=committed,
            locked_next_item="2502",
            proposed_ids=proposed,
            allow_overhaul=True,
            hero_id="98",
            transformative_overhaul=tx_flag,
        )
        assert (lock_plain, plan_plain) == (lock_tr, plan_tr)
        assert lock_tr == "2502"
        assert plan_tr == ["2502", "2504", "3748"]
        assert trace["overridden"] is False
        assert trace["override_reason"] == ""
        assert trace["committed_gold"] == 1600

        # 1 second later local refresh (allow_overhaul=False) -> zero flip-flop / zero override!
        lock_next, plan_next, trace_next = reconcile_match_build_plan_with_trace(
            store=store,
            my_items=my_items,
            committed_plan=plan_tr,
            locked_next_item=lock_tr,
            proposed_ids=["2502", "2504", "3748", "3068"],
            allow_overhaul=False,
            hero_id="98",
        )
        assert lock_next == "2502"
        assert plan_next[0] == "2502"
        assert trace_next["overridden"] is False

    # Boundary: exact 1200G (1058 Rod toward 3089) with cur_lock still in proposed protects slot 0 even under transformative_overhaul=True
    lock_1200, plan_1200, tr_1200 = reconcile_match_build_plan_with_trace(
        store=store,
        my_items=("1058",),
        committed_plan=["3089", "2504"],
        locked_next_item="3089",
        proposed_ids=["2504", "3089", "3748"],
        allow_overhaul=True,
        transformative_overhaul=True,
    )
    lock_1200_p, plan_1200_p = reconcile_match_build_plan(
        store=store,
        my_items=("1058",),
        committed_plan=["3089", "2504"],
        locked_next_item="3089",
        proposed_ids=["2504", "3089", "3748"],
        allow_overhaul=True,
        transformative_overhaul=True,
    )
    assert (lock_1200_p, plan_1200_p) == (lock_1200, plan_1200)
    assert lock_1200 == "3089"
    assert plan_1200 == ["3089", "2504", "3748"]
    assert tr_1200["overridden"] is False

    # Contrast: if cur_lock is DROPPED from proposed under transformative_overhaul=True, genuine pivot still succeeds
    lock_piv, plan_piv, tr_piv = reconcile_match_build_plan_with_trace(
        store=store,
        my_items=("1058",),
        committed_plan=["3089", "2504"],
        locked_next_item="3089",
        proposed_ids=["2504", "3748"],
        allow_overhaul=True,
        transformative_overhaul=True,
    )
    lock_piv_p, plan_piv_p = reconcile_match_build_plan(
        store=store,
        my_items=("1058",),
        committed_plan=["3089", "2504"],
        locked_next_item="3089",
        proposed_ids=["2504", "3748"],
        allow_overhaul=True,
        transformative_overhaul=True,
    )
    assert (lock_piv_p, plan_piv_p) == (lock_piv, plan_piv)
    assert lock_piv == "2504"
    assert plan_piv == ["2504", "3748"]
    assert tr_piv["overridden"] is True


# ---------------------------------------------------------------------------
# 2. ActionTracker warmup-served & 6-digit augment-granted special item accounting
# ---------------------------------------------------------------------------


def test_action_tracker_warmup_served_and_6digit_augment_granted_items() -> None:
    # Helper unit checks
    assert _is_augment_granted_special_item(223069) is True
    assert _is_augment_granted_special_item("223069") is True
    assert _is_augment_granted_special_item(994403, price=2500) is True
    assert _is_augment_granted_special_item("994403") is True
    assert _is_augment_granted_special_item(3068) is False
    assert _is_augment_granted_special_item("3031") is False
    assert _is_augment_granted_special_item(220013) is False  # ARAM Mayhem Poro-Snax
    assert _is_augment_granted_special_item(123031) is False
    assert _is_augment_granted_special_item("") is False

    emitted: list[tuple[str, str, dict[str, Any], float | None]] = []
    now = [100.0]
    complete_set = {"2502", "2504", "3068", "3748"}
    tracker = MatchActionTracker(
        lambda d, e, p, gt: emitted.append((d, e, p, gt)),
        clock=lambda: now[0],
        undo_window_s=2.0,
        is_complete_item_fn=lambda iid: str(iid) in complete_set,
    )
    enemy = _make_player("厄加特", "Urgot", team="CHAOS", level=10)

    # Seed baseline alive state + local rec
    me_alive = _make_player("慎", "Shen", is_dead=False, items=("3068",), item_details=[{"item_id": 3068, "count": 1, "price": 2700}])
    tracker.on_live_snapshot(LiveMatchSnapshot(150.0, me_alive, (me_alive,), (enemy,), 2000.0))
    now[0] = 105.0
    tracker.record_local_build_rec(155.0, [{"item_id": "2502"}, {"item_id": "2504"}, {"item_id": "3748"}])

    # Case A: Death served by pre_death_warmup_cached (auto-inferred warmup_served=True -> arrived_before_buy=True at 0ms)
    now[0] = 120.0
    me_dead = _make_player("慎", "Shen", is_dead=True, items=("3068",), item_details=[{"item_id": 3068, "count": 1, "price": 2700}])
    tracker.on_live_snapshot(LiveMatchSnapshot(180.0, me_dead, (me_dead,), (enemy,), 2800.0))
    tracker.note_death_coach_decision(False, "pre_death_warmup_cached")
    assert tracker._death_warmup_served is True

    # Player immediately buys 2502 during death window
    now[0] = 121.0
    inv_bought = [
        {"item_id": 3068, "count": 1, "price": 2700},
        {"item_id": 2502, "count": 1, "price": 2800},
    ]
    me_dead_buy = _make_player("慎", "Shen", is_dead=True, items=("3068", "2502"), item_details=inv_bought)
    tracker.on_live_snapshot(LiveMatchSnapshot(181.0, me_dead_buy, (me_dead_buy,), (enemy,), 0.0))

    # Respawn -> death_shop_window emitted with warmup_served=True and coach_arrived_before_buy=True
    now[0] = 135.0
    me_respawn = _make_player("慎", "Shen", is_dead=False, items=("3068", "2502"), item_details=inv_bought)
    tracker.on_live_snapshot(LiveMatchSnapshot(195.0, me_respawn, (me_respawn,), (enemy,), 100.0))

    dws = [p for _, e, p, _ in emitted if e == EV_DEATH_SHOP_WINDOW]
    assert len(dws) == 1
    assert dws[0]["warmup_served"] is True
    assert dws[0]["coach_arrived"] is True
    assert dws[0]["coach_arrived_before_buy"] is True
    assert dws[0]["coach_triggered"] is False
    assert dws[0]["coach_skip_reason"] == "pre_death_warmup_cached"
    assert dws[0]["bought_count"] == 1

    # Case B: Pending death decision called BEFORE death snapshot arrives + explicit warmup_served kwarg
    now[0] = 150.0
    tracker.note_death_coach_decision(False, "custom_cache_hit", warmup_served=True)
    now[0] = 150.5
    me_dead_2 = _make_player("慎", "Shen", is_dead=True, items=("3068", "2502"), item_details=inv_bought)
    tracker.on_live_snapshot(LiveMatchSnapshot(210.0, me_dead_2, (me_dead_2,), (enemy,), 3000.0))
    now[0] = 151.0
    inv_bought_2 = inv_bought + [{"item_id": 2504, "count": 1, "price": 2900}]
    me_dead_2_buy = _make_player("慎", "Shen", is_dead=True, items=("3068", "2502", "2504"), item_details=inv_bought_2)
    tracker.on_live_snapshot(LiveMatchSnapshot(211.0, me_dead_2_buy, (me_dead_2_buy,), (enemy,), 100.0))
    now[0] = 165.0
    me_respawn_2 = _make_player("慎", "Shen", is_dead=False, items=("3068", "2502", "2504"), item_details=inv_bought_2)
    tracker.on_live_snapshot(LiveMatchSnapshot(225.0, me_respawn_2, (me_respawn_2,), (enemy,), 200.0))

    dws = [p for _, e, p, _ in emitted if e == EV_DEATH_SHOP_WINDOW]
    assert len(dws) == 2
    assert dws[1]["warmup_served"] is True
    assert dws[1]["coach_arrived_before_buy"] is True

    # Case C: Explicit warmup_served=False overrides skip_reason="pre_death_warmup_cached"
    now[0] = 180.0
    me_dead_3 = _make_player("慎", "Shen", is_dead=True, items=("3068", "2502", "2504"), item_details=inv_bought_2)
    tracker.on_live_snapshot(LiveMatchSnapshot(240.0, me_dead_3, (me_dead_3,), (enemy,), 500.0))
    tracker.note_death_coach_decision(False, "pre_death_warmup_cached", warmup_served=False)
    now[0] = 190.0
    me_respawn_3 = _make_player("慎", "Shen", is_dead=False, items=("3068", "2502", "2504"), item_details=inv_bought_2)
    tracker.on_live_snapshot(LiveMatchSnapshot(250.0, me_respawn_3, (me_respawn_3,), (enemy,), 600.0))
    dws = [p for _, e, p, _ in emitted if e == EV_DEATH_SHOP_WINDOW]
    assert len(dws) == 3
    assert dws[2]["warmup_served"] is False
    assert dws[2]["coach_arrived_before_buy"] is False

    # Case D: 6-digit augment-upgraded item (223069) & Prismatic anvil item (994403) tagged augment_granted=True
    now[0] = 210.0
    inv_special = [
        {"item_id": 223069, "count": 1, "price": 6000},  # Upgraded Sunfire (升级：献祭) replacing 3068
        {"item_id": 2502, "count": 1, "price": 2800},
        {"item_id": 2504, "count": 1, "price": 2900},
        {"item_id": 994403, "count": 1, "price": 2500},  # Prismatic Anvil item
    ]
    me_special = _make_player(
        "慎",
        "Shen",
        is_dead=False,
        items=("223069", "2502", "2504", "994403"),
        item_details=inv_special,
    )
    tracker.on_live_snapshot(LiveMatchSnapshot(270.0, me_special, (me_special,), (enemy,), 200.0))
    now[0] = 214.0
    tracker.on_live_snapshot(LiveMatchSnapshot(274.0, me_special, (me_special,), (enemy,), 250.0))

    purchase_deltas = [
        p for _, e, p, _ in emitted if e == EV_ITEM_PURCHASE_DELTA and p.get("event_kind") == "item_purchase"
    ]
    by_item = {int(p["item_id"]): p for p in purchase_deltas}
    assert by_item[2502]["augment_granted"] is False
    assert by_item[2504]["augment_granted"] is False
    assert by_item[223069]["augment_granted"] is True
    assert by_item[994403]["augment_granted"] is True


# ---------------------------------------------------------------------------
# 3. DevLab recorder + analyzer calibration (worker stalls, warmup timely, 6-digit items)
# ---------------------------------------------------------------------------


def test_devlab_recorder_and_analyzer_worker_stalls_warmup_and_6digit_calibration(tmp_path: Path) -> None:
    assert recorder_is_bg_perf_op("vision_worker_tick") is True
    assert recorder_is_bg_perf_op("vision_worker_ocr") is True
    assert recorder_is_bg_perf_op("tick_vision") is False
    assert analyzer_is_bg_perf_op("vision_worker_tick") is True
    assert analyzer_is_bg_perf_op("apply_live_snapshot") is False
    assert _is_augment_granted_special_item_id(223069) is True
    assert _is_augment_granted_special_item_id("994403") is True
    assert _is_augment_granted_special_item_id(2502) is False
    assert _is_augment_granted_special_item_id(220013) is False

    devlab_dir = tmp_path / "devlab"
    rec = DevLabFlightRecorder(
        root_dir=devlab_dir,
        enable_roi_snapshots=False,
        enable_debug_log=False,
        app_version="1.5.0-test",
    )
    try:
        # Match 1: Recorder live latency separation + new-format warmup & 6-digit item events
        rec.begin_match(
            match_key_hash=hash_match_key("ws_d_m1"),
            hero_id="98",
            hero_name="慎",
            dataset_tag="16.23/normal",
            game_mode="KIWI",
        )
        # Main-thread ops: tick_vision (all < 1ms -> 0 stalls), apply_live_snapshot (1 stall >=50ms)
        for v in (0.12, 0.18, 0.25, 0.31):
            rec.record_latency_sample("tick_vision", v)
        for v in (2.5, 8.0, 56.4):
            rec.record_latency_sample("apply_live_snapshot", v)
        # Background worker op: 4 frames >= 50ms -> MUST go to worker_ge_50ms_count, NOT total_ge_50ms_count!
        for v in (52.0, 68.0, 88.0, 104.0):
            rec.record_latency_sample("vision_worker_tick", v)
        assert rec.flush_latency_summary(force=True) is True

        # Also emit a HISTORICAL polluted EV_MAIN_THREAD_STALL (where total_ge_50ms_count wrongly included 69 worker stalls)
        rec.emit(
            DOMAIN_PERF,
            EV_MAIN_THREAD_STALL,
            {
                "window_s": 60.0,
                "total_ge_50ms_count": 70,  # legacy polluted sum (69 worker + 1 main)
                "ops": {
                    "tick_vision": {"count": 120, "p50_ms": 0.05, "p95_ms": 0.25, "max_ms": 12.0, "ge_50ms_count": 0},
                    "apply_live_snapshot": {"count": 60, "p50_ms": 0.4, "p95_ms": 14.0, "max_ms": 54.0, "ge_50ms_count": 1},
                    "vision_worker_tick": {"count": 120, "p50_ms": 54.0, "p95_ms": 92.0, "max_ms": 110.0, "ge_50ms_count": 69},
                },
            },
            game_time_s=240.0,
        )

        # Death shop windows:
        # 1) Historical pre_death_warmup_cached (coach_triggered=False, coach_arrived_before_buy=False, no warmup_served key)
        rec.emit(
            DOMAIN_COACH,
            EV_DEATH_SHOP_WINDOW,
            {
                "death_start_time": 180.0,
                "death_duration_s": 15.0,
                "items_bought": [1031, 3067],
                "bought_count": 2,
                "coach_triggered": False,
                "coach_arrived": False,
                "coach_arrived_before_buy": False,
                "coach_skip_reason": "pre_death_warmup_cached",
            },
            game_time_s=195.0,
        )
        # 2) New-format warmup_served=True with bought_count=1
        rec.emit(
            DOMAIN_COACH,
            EV_DEATH_SHOP_WINDOW,
            {
                "death_start_time": 300.0,
                "death_duration_s": 18.0,
                "items_bought": [2502],
                "bought_count": 1,
                "coach_triggered": False,
                "coach_arrived": True,
                "coach_arrived_before_buy": True,
                "warmup_served": True,
                "coach_skip_reason": "pre_death_warmup_cached",
            },
            game_time_s=318.0,
        )
        # 3) Warmup hit with bought_count=0 (no shopping) -> NOT counted in buy windows
        rec.emit(
            DOMAIN_COACH,
            EV_DEATH_SHOP_WINDOW,
            {
                "death_start_time": 360.0,
                "death_duration_s": 14.0,
                "items_bought": [],
                "bought_count": 0,
                "coach_triggered": False,
                "coach_arrived": True,
                "coach_arrived_before_buy": True,
                "warmup_served": True,
                "coach_skip_reason": "pre_death_warmup_cached",
            },
            game_time_s=374.0,
        )
        # 4) Triggered death coach that arrived late -> 1 late window
        rec.emit(
            DOMAIN_COACH,
            EV_DEATH_SHOP_WINDOW,
            {
                "death_start_time": 420.0,
                "death_duration_s": 20.0,
                "items_bought": [2504],
                "bought_count": 1,
                "coach_triggered": True,
                "coach_arrived": True,
                "coach_arrived_before_buy": False,
                "coach_skip_reason": "",
            },
            game_time_s=440.0,
        )

        # Item purchases: 2 real shop buys (rank 1 + rank 2) + 2 augment-granted 6-digit items (historical & new)
        rec.emit(
            DOMAIN_REC,
            EV_ITEM_PURCHASE_DELTA,
            {
                "event_kind": "item_purchase",
                "item_id": 2502,
                "matched_local_rank": 1,
                "matched_local_top1": True,
                "local_top_items": [2502, 2504, 3748],
                "augment_granted": False,
            },
            game_time_s=310.0,
        )
        rec.emit(
            DOMAIN_REC,
            EV_ITEM_PURCHASE_DELTA,
            {
                "event_kind": "item_purchase",
                "item_id": 2504,
                "matched_local_rank": 2,
                "matched_local_top1": False,
                "local_top_items": [3748, 2504, 3068],
                "augment_granted": False,
            },
            game_time_s=430.0,
        )
        # Historical 223069 without augment_granted key -> still excluded by 6-digit ID check!
        rec.emit(
            DOMAIN_REC,
            EV_ITEM_PURCHASE_DELTA,
            {
                "event_kind": "item_purchase",
                "item_id": 223069,
                "matched_local_rank": 0,
                "matched_local_top1": False,
                "local_top_items": [3748, 2504, 3068],
            },
            game_time_s=460.0,
        )
        # New-format 994403 with augment_granted=True -> excluded from adoption denominator!
        rec.emit(
            DOMAIN_REC,
            EV_ITEM_PURCHASE_DELTA,
            {
                "event_kind": "item_purchase",
                "item_id": 994403,
                "matched_local_rank": 0,
                "matched_local_top1": False,
                "local_top_items": [3748, 2504, 3068],
                "augment_granted": True,
            },
            game_time_s=500.0,
        )

        rec.emit(
            DOMAIN_GAME,
            EV_MATCH_EOG,
            {"won": 1, "duration_s": 620, "eog_source": "eog_stats_block"},
            game_time_s=620.0,
            priority="high",
        )
        rec.end_match(won=1, duration_s=620, eog_source="eog_stats_block")
        assert rec.flush(timeout_s=3.0) is True
    finally:
        rec.flush_and_close()

    analyzer = DevLabAnalyzer(root_dir=devlab_dir)
    matches = analyzer.list_matches()
    assert len(matches) == 1

    # Verify recorder's emitted EV_MAIN_THREAD_STALL payload directly on disk
    _meta, raw_events = analyzer.load_match_events(matches[0]["match_dir"])
    stall_evs = [e["payload"] for e in raw_events if e.get("event_type") == EV_MAIN_THREAD_STALL]
    assert len(stall_evs) >= 2
    rec_stall = stall_evs[0]
    assert rec_stall["total_ge_50ms_count"] == 1
    assert rec_stall["worker_ge_50ms_count"] == 4
    assert rec_stall["ops"]["vision_worker_tick"]["ge_50ms_count"] == 4

    # Verify single-match replay calibration
    replay = analyzer.replay_match(matches[0]["match_dir"], write_outputs=False)
    assert replay.kpis["runtime"]["main_thread_ge_50ms_stalls"] == 2  # 1 (recorder) + 1 (historical) — NOT 75!
    assert replay.kpis["runtime"]["vision_worker_ge_50ms_count"] == 73  # 4 + 69
    assert replay.kpis["runtime"]["vision_worker_p95_ms"] > 80.0
    assert replay.kpis["runtime"]["tick_vision_p95_ms"] < 1.0
    assert replay.facts["vision_worker_ge_50ms_count"] == 73
    assert replay.facts["vision_worker_p95_ms"] > 80.0

    # Warmup timely rate: 2 warmup hits + 1 late = 2/3 timely (66.7%)
    assert replay.facts["death_warmup_hit_count"] == 2
    assert replay.facts["death_ai_buy_windows"] == 3
    assert replay.facts["death_ai_late_windows"] == 1
    assert replay.kpis["coach"]["death_warmup_hit_count"] == 2
    assert replay.kpis["coach"]["death_ai_timely_rate"] == pytest.approx(2.0 / 3.0, abs=0.001)

    # 6-digit augment-granted items excluded from item adoption denominator: 2/2 = 100% Top4, 1/2 = 50% Top1!
    assert replay.kpis["recommendation"]["item_purchases"] == 2
    assert replay.kpis["recommendation"]["augment_granted_items"] == 2
    assert replay.kpis["recommendation"]["item_top4_adoption_rate"] == pytest.approx(1.0)
    assert replay.kpis["recommendation"]["item_top1_adoption_rate"] == pytest.approx(0.5)

    # Multi-match evolution report + markdown formatting + no false main-thread stall alert!
    report = analyzer.generate_evolution_report(last_n=10)
    p1 = report.pillars["recommendation"]
    p4 = report.pillars["coach"]
    p5 = report.pillars["runtime"]
    assert p1["item_top4_adoption_rate"] == pytest.approx(1.0)
    assert p4["death_warmup_hit_count"] == 2
    assert p4["death_ai_timely_rate"] == pytest.approx(2.0 / 3.0, abs=0.001)
    assert p5["main_thread_ge_50ms_stalls"] == 2
    assert p5["vision_worker_ge_50ms_count"] == 73
    assert p5["vision_worker_p95_ms"] > 80.0
    assert "vision_worker" in report.report_markdown
    assert "0ms预热命中" in report.report_markdown
    fired = {iss.detector_rule for iss in report.issues}
    assert "DET_MAIN_THREAD_STALL_AND_CLICK_STARVATION" not in fired


# ---------------------------------------------------------------------------
# 4. EdgeBuildDrawer +20px breathing cushion & QTimer.singleShot(0) post-layout height sync
# ---------------------------------------------------------------------------


def test_edge_drawer_auto_height_cushion_and_post_layout_sync_zero_overflow(
    qapp: QApplication,
    tmp_path: Path,
) -> None:
    store = _make_shen_tank_store(tmp_path)
    drawer = EdgeBuildDrawer()
    try:
        assert EdgeBuildDrawer._AUTO_HEIGHT_CUSHION_PX == 20
        drawer.show()
        drawer.set_expanded(True)

        r1 = DynamicItemRec("2502", "无终恨意", 2800, False, 1200, 92.0, 0.56, 7500, ("已购锁子甲与燃烧宝石共1600G，先合无终恨意续航抗压",), ("核心",))
        r2 = DynamicItemRec("2504", "败魔", 2900, False, 2900, 88.0, 0.55, 6200, ("敌方双AP消耗极高，第二件补败魔抗住消耗",), ("魔抗",))
        r3 = DynamicItemRec("3748", "巨型九头蛇", 3300, False, 3300, 84.0, 0.54, 4800, ("补足团战清线与嘲讽爆发",), ("血量",))
        plan = NextBuyPlan(
            target=r1,
            effective_cost=1200,
            affordable_now=False,
            gold_gap=400,
            buy_now_hint="先买红水晶 (400G)，下波回家直接合成无终恨意，顺便关注铁板靴时机",
            remaining_build_path=("无终恨意", "败魔", "巨型九头蛇"),
            remaining_build_ids=("2502", "2504", "3748"),
            backup=r2,
            recommended_boots_id="3047",
            recommended_boots_name="铁板靴",
            recommended_boots_reason="敌重AD减普攻",
        )
        threat = MatchupThreatProfile(
            enemy_armor_score=1.4,
            enemy_hp_score=2.2,
            enemy_mr_score=1.1,
            enemy_ad_ratio=0.58,
            enemy_ap_ratio=0.42,
            enemy_heal_score=1.9,
            enemy_shield_score=0.8,
            enemy_cc_score=2.3,
            ally_has_grievous=False,
            ally_has_shield_break=False,
            top_threat_name="厄加特",
            top_threat_kda="9/2/7",
            top_threat_type="AD",
            summary_tags=("敌高回复(缺重伤)", "头号威胁:厄加特(9/2/7)"),
        )
        drawer.refresh_match_view(
            store,
            "98",
            ["1319", "1001", "1002"],
            ("1031", "3067"),
            800.0,
            threat,
            [r1, r2, r3],
            next_plan=plan,
        )

        # Inject rich multi-line coach advice (opening brief + long tactical summary + buy_now) & committed plan overhaul
        advice = CoachAdvicePayload(
            request_id="req-ws-d-drawer",
            updated_at=utc_now_iso(),
            next_items=[
                CoachAdviceItem("2502", "无终恨意", "下件必出", "已投入1600G散件，先合无终恨意把团战坦度与回血拉满"),
                CoachAdviceItem("2504", "败魔", "备选", "敌方双法核消耗高，第二件直接转败魔稳住血线"),
            ],
            build_path=["无终恨意", "败魔", "巨型九头蛇", "自然之力"],
            buy_now="当前800G先买红水晶(400G)+草鞋(300G)，留钱下波阵亡立刻合成无终恨意",
            tactical_summary="敌方强开与消耗兼具，团战优先用W规避普攻爆发并盯防厄加特进场，先合完无终恨意再补败魔撑魔抗。",
            opening_brief={
                "enemy_threat": "敌方厄加特+双APpoke成型快，前排若无魔抗极易被消耗残血",
                "augment_goal": "优先抓取坦克回血、双抗与技能急速类海克斯强化持续前排能力",
                "opening_buy": "出门守护者号角加红水晶，优先做出锁子甲与燃烧宝石平滑过渡无终恨意",
            },
        )
        drawer.set_committed_plan(["2502", "2504", "3748"], "2502", overhaul_reason="海克斯质变已锁定无终恨意先合")
        drawer.set_coach_advice(advice, allow_auto_expand=True)

        # Pump Qt event loop so QTimer.singleShot(0, ...) post-layout height sync executes
        deadline = time.perf_counter() + 0.25
        while time.perf_counter() < deadline:
            qapp.processEvents()
            time.sleep(0.01)

        screen = drawer._active_screen()
        assert screen is not None
        geo = screen.geometry()
        max_h = max(EdgeBuildDrawer.EXPANDED_MIN_H, geo.height() - int(geo.height() * 0.25) - 24)
        assert drawer.height() <= max_h
        # Zero scroll overflow when content fits within screen budget!
        if drawer.height() < max_h:
            assert drawer.panel_scroll.verticalScrollBar().maximum() == 0

        # Strictly clamped on a small synthetic screen (800x600)
        small_geo = QRect(0, 0, 800, 600)
        small_max_h = max(EdgeBuildDrawer.EXPANDED_MIN_H, small_geo.height() - int(small_geo.height() * 0.25) - 24)
        clamped_h = drawer._compute_auto_expanded_height(drawer.width(), small_geo)
        assert clamped_h <= small_max_h
    finally:
        drawer.close()
        qapp.processEvents()
