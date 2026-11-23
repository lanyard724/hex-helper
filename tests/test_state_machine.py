from hex_helper.app import GameState, HexStateMachine
from hex_helper.data.provider import AugmentInfo
from hex_helper.game.models import ChampSelectState, LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.vision.capture import WindowRect
from hex_helper.vision.click_tracker import ClickTracker, build_slot_hit_boxes
from hex_helper.vision.detector import DetectedCard, PickerDetectionResult


def _make_card(slot: int, aid: str, name: str) -> DetectedCard:
    return DetectedCard(
        slot_index=slot,
        augment=AugmentInfo(aid, name, "gold"),
        confidence=0.95,
        screen_box=(400 + slot * 450, 580, 620 + slot * 450, 630),
        rarity_hint="gold",
    )


def test_full_match_state_machine_lifecycle():
    sm = HexStateMachine()
    assert sm.state == GameState.IDLE

    # 1. Enter ARAM Mayhem Champ Select
    cs = ChampSelectState(2400, "KIWI", True, "222", ("222", "101"), ("10", "236"))
    eff = sm.on_lcu_poll("ChampSelect", cs)
    assert eff.state_changed is True
    assert sm.state == GameState.CHAMP_SELECT
    assert sm.current_hero_id == "222"

    # 2. Enter live game (2999 API ready) -> triggers game_start coach snapshot
    me = PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 3, 0, 0, 0, ("1055",))
    e1 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 3, 0, 0, 0, ("1055",))
    snap1 = LiveMatchSnapshot(30.0, me, (me,), (e1,), 500.0)
    eff2 = sm.on_live_snapshot(snap1, "222", now_mono=100.0)
    assert sm.state == GameState.IN_GAME
    assert eff2.coach_trigger == "game_start"

    # 3. Augment picker appears stably -> AUGMENT_PICKING + coach trigger
    cards = (
        _make_card(0, "3058", "嗡嗡怪兽"),
        _make_card(1, "3081", "命运之武器"),
        _make_card(2, "3131", "屹立不倒"),
    )
    eff3 = sm.on_picker_result(PickerDetectionResult(True, True, False, cards), clicked_slot=None, now_mono=110.0)
    assert sm.state == GameState.AUGMENT_PICKING
    assert eff3.coach_trigger == "augment_picking"

    # 4. Player clicks slot 1 (Destiny's Weapon), then picker closes and stays closed >= 0.6s
    sm.on_picker_result(PickerDetectionResult(True, True, False, cards), clicked_slot=1, now_mono=111.0)
    eff_closing = sm.on_picker_result(PickerDetectionResult(False, False, False, ()), clicked_slot=None, now_mono=111.2)
    assert sm.state == GameState.AUGMENT_PICKING  # debouncing
    assert eff_closing.newly_selected_augment == ""

    eff_done = sm.on_picker_result(PickerDetectionResult(False, False, False, ()), clicked_slot=None, now_mono=112.0)
    assert sm.state == GameState.IN_GAME
    assert eff_done.newly_selected_augment == "3081"
    assert eff_done.coach_trigger == "augment_selected"
    assert sm.selected_augments == ["3081"]

    # 5. Player dies and enemy bought Thornmail -> auto-peek drawer + coach trigger
    me_dead = PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", True, 8, 2, 1, 3, ("3085",))
    e1_fed = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 9, 6, 1, 2, ("3075", "3084"))
    snap_dead = LiveMatchSnapshot(300.0, me_dead, (me_dead,), (e1_fed,), 3100.0)
    eff_dead = sm.on_live_snapshot(snap_dead, "222", now_mono=150.0)
    assert eff_dead.auto_peek_drawer is True
    assert eff_dead.coach_trigger == "death_enemy_build_changed"

    # 6. Player respawns -> auto-collapse drawer
    me_alive = PlayerLiveInfo("me", "金克丝", "Jinx", "ORDER", False, 8, 2, 1, 3, ("3085", "3033"))
    eff_alive = sm.on_live_snapshot(LiveMatchSnapshot(320.0, me_alive, (me_alive,), (e1_fed,), 100.0), "222", now_mono=170.0)
    assert eff_alive.auto_collapse_drawer is True

    # 7. Match ends -> returns to IDLE and resets selected augments
    sm.on_live_snapshot(None, None, now_mono=500.0)
    assert sm.state == GameState.IDLE
    assert sm.selected_augments == []


def test_reroll_resets_stale_click_and_slot_hitboxes_cover_full_columns():
    rect = WindowRect(hwnd=1, left=0, top=0, width=2560, height=1440)
    boxes = build_slot_hit_boxes(
        rect,
        {
            0: (560, 580, 820, 630),
            1: (1150, 580, 1410, 630),
            2: (1740, 580, 2000, 630),
        },
    )
    assert set(boxes.keys()) == {0, 1, 2}
    # Left, center, and right card clicks anywhere from art (y=400) to Select button (y=1100) must match
    for slot, cx in ((0, 690), (1, 1280), (2, 1870)):
        l, t, r, b = boxes[slot]
        assert l <= cx <= r
        assert t <= 400 and b >= 1100

    sm = HexStateMachine()
    me = PlayerLiveInfo("me", "伊芙琳", "Evelynn", "ORDER", True, 7, 2, 1, 3, ())
    e1 = PlayerLiveInfo("e1", "李青", "LeeSin", "CHAOS", False, 7, 3, 2, 2, ())
    sm.on_live_snapshot(LiveMatchSnapshot(180.0, me, (me,), (e1,), 1315.0), "28", now_mono=10.0)

    cards_v1 = (
        _make_card(0, "2091", "无尽大杀四方"),
        _make_card(1, "1332", "不祥契约"),
        _make_card(2, "1238", "质变：棱彩阶"),
    )
    sm.on_picker_result(PickerDetectionResult(True, True, False, cards_v1), clicked_slot=None, now_mono=20.0)
    # Click on slot 0 (e.g. reroll), then slot 0 rerolls into a new card
    sm.on_picker_result(PickerDetectionResult(True, True, False, cards_v1), clicked_slot=0, now_mono=20.4)
    cards_v2 = (
        _make_card(0, "3081", "命运之武器"),
        cards_v1[1],
        cards_v1[2],
    )
    sm.on_picker_result(PickerDetectionResult(True, True, True, cards_v2), clicked_slot=None, now_mono=21.0)
    # Without a new click after reroll, closing must NOT commit stale slot 0
    sm.on_picker_result(PickerDetectionResult(False, False, False, ()), clicked_slot=None, now_mono=21.5)
    eff_no_pick = sm.on_picker_result(PickerDetectionResult(False, False, False, ()), clicked_slot=None, now_mono=22.3)
    assert eff_no_pick.newly_selected_augment == ""
    assert sm.selected_augments == []
    assert [c.augment.augment_id for c in sm.last_offered_cards] == ["3081", "1332", "1238"]


def test_cursor_dwell_auto_commits_under_ace_blocked_click_and_ignores_stat_anvil():
    # Simulate Tencent ACE zeroing GetAsyncKeyState (clicked=False for every single sample!)
    tracker = ClickTracker()
    tracker.reset(now_mono=100.0)
    # Player scans slot 0 briefly at 101.0, then moves onto slot 1 ("没法力，没问题") at 102.0..102.55 and clicks at 102.55
    for dt in (0.0, 0.06, 0.12):
        tracker.record_cursor_sample(101.0 + dt, slot=0, clicked=False)
    for k in range(18):
        tracker.record_cursor_sample(102.0 + k * 0.03, slot=1, clicked=False)
    # After picker closes at 102.55, player whips mouse right (slot 2) at 103.05 to walk back to lane
    for k in range(10):
        tracker.record_cursor_sample(103.05 + k * 0.03, slot=2, clicked=False)

    slot, src = tracker.resolve_selection(close_ts=102.55, valid_slots={0, 1, 2})
    assert slot == 1
    assert src == "cursor_dwell"

    # State machine must also ignore Stat Anvil duplicate screen ("属性！" on 2 slots) and allow 1-click swap
    sm = HexStateMachine()
    me = PlayerLiveInfo("me", "凯尔", "Kayle", "ORDER", True, 3, 0, 0, 0, ())
    e1 = PlayerLiveInfo("e1", "艾克", "Ekko", "CHAOS", False, 3, 0, 0, 0, ())
    sm.on_live_snapshot(LiveMatchSnapshot(30.0, me, (me,), (e1,), 1400.0), "10", now_mono=10.0)

    anvil_cards = (_make_card(1, "1404", "属性！"), _make_card(2, "1404", "属性！"))
    sm.on_picker_result(PickerDetectionResult(True, True, False, anvil_cards), clicked_slot=None, now_mono=20.0)
    assert sm.state == GameState.IN_GAME  # Ignored duplicate Stat Anvil!

    assert sm.add_selected_augment("1028") is True
    assert sm.replace_last_augment("3059") is True
    assert sm.selected_augments == ["3059"]

    # Post-reroll accuracy: hovering slot 0 reroll strip for 0.6s then moving to slot 1 card for 0.25s must pick slot 1!
    t2 = ClickTracker()
    t2.reset(now_mono=200.0)
    for k in range(20):
        t2.record_cursor_sample(200.1 + k * 0.03, slot=0, clicked=False, in_reroll_zone=True)
    for k in range(8):
        t2.record_cursor_sample(200.78 + k * 0.03, slot=1, clicked=False, in_reroll_zone=False)
    slot2, src2 = t2.resolve_selection(close_ts=201.02, valid_slots={0, 1, 2})
    assert slot2 == 1 and src2 == "cursor_dwell"

    # 5th augment in Mayhem special rounds must also be accepted
    for aid in ("1111", "2222", "3333", "4444"):
        assert sm.add_selected_augment(aid) is True
    assert len(sm.selected_augments) == 5


def test_post_reroll_velocity_aware_pick_and_stat_anvil_block():
    # Reproduce exact real-log scenario (21:09:30): rerolled slot 1, lingered briefly on slot 1,
    # then swept fast across to slot 2 and paused/clicked slot 2 right before picker vanished!
    t = ClickTracker()
    t.reset(now_mono=300.0)
    t.mark_reroll(slot=1, now_mono=300.5)
    # Brief lingering on slot 1 around x=1280 after reroll (300.60 - 300.84)
    for k in range(9):
        t.record_cursor_sample(300.60 + k * 0.03, slot=1, clicked=False, pos=(1280, 620), in_reroll_zone=False)
    # Fast horizontal sweep from slot 1 to slot 2 (velocity > 1500 px/s)
    t.record_cursor_sample(300.87, slot=1, clicked=False, pos=(1480, 620), in_reroll_zone=False)
    t.record_cursor_sample(300.90, slot=2, clicked=False, pos=(1720, 620), in_reroll_zone=False)
    # Decelerates / pauses on slot 2 (1920, 640) to click right before picker closes at 301.10
    for k in range(6):
        t.record_cursor_sample(300.93 + k * 0.03, slot=2, clicked=False, pos=(1920 + k, 640), in_reroll_zone=False)

    slot, src = t.resolve_selection(close_ts=301.10, valid_slots={0, 1, 2}, rerolled_slot=1, reroll_ts=300.5)
    assert slot == 2 and src == "cursor_dwell"

    # Stat Anvil card (1404 属性！) mixed with another card must NEVER auto-commit into selected_augments!
    sm = HexStateMachine()
    me = PlayerLiveInfo("me", "厄运小姐", "MissFortune", "ORDER", False, 11, 4, 3, 12, ())
    e1 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 11, 5, 4, 8, ())
    sm.on_live_snapshot(LiveMatchSnapshot(600.0, me, (me,), (e1,), 1200.0), "21", now_mono=10.0)
    mixed_anvil = (_make_card(1, "3081", "命运之武器"), _make_card(2, "1404", "属性！"))
    sm.on_picker_result(PickerDetectionResult(True, True, False, mixed_anvil), clicked_slot=None, now_mono=20.0)
    sm.on_picker_result(PickerDetectionResult(False, False, False, ()), clicked_slot=2, now_mono=21.0)
    eff = sm.on_picker_result(PickerDetectionResult(False, False, False, ()), clicked_slot=2, now_mono=21.35)
    assert eff.newly_selected_augment == ""
    assert sm.selected_augments == []


def test_correcting_misrecognized_transformative_augment_rolls_back_build_plan_immediately():
    """If 3059 (没法力，没问题) was mis-recorded and locked a mana build (3003 大天使), correcting to
    1028 (侵蚀) via swap OR remove+add MUST immediately unlock/roll back the unbought mana plan!
    """
    import tempfile
    from pathlib import Path
    from hex_helper.data.provider import CoreBuild, HeroStat, ItemRecStat, ItemStaticInfo
    from hex_helper.data.static_assets import StaticAssetsManager
    from hex_helper.data.store import DataStore
    from hex_helper.scoring.matchup_optimizer import build_next_buy_plan, optimize_next_items, reconcile_match_build_plan

    with tempfile.TemporaryDirectory() as td:
        static = StaticAssetsManager(Path(td))
        static.items["3115"] = ItemStaticInfo(
            item_id="3115",
            name="纳什之牙",
            gold=3000,
            category="complete_equipment",
            stats={"FlatMagicDamageMod": 80.0, "PercentAttackSpeedMod": 0.50},
        )
        static.items["3003"] = ItemStaticInfo(
            item_id="3003",
            name="大天使之杖",
            gold=2900,
            category="complete_equipment",
            stats={"FlatMagicDamageMod": 70.0, "FlatMPPoolMod": 600.0},
        )
        static.items["2503"] = ItemStaticInfo(
            item_id="2503",
            name="黯炎火炬",
            gold=2800,
            category="complete_equipment",
            stats={"FlatMagicDamageMod": 80.0, "FlatMPPoolMod": 600.0},
        )
        static.items["3089"] = ItemStaticInfo(
            item_id="3089",
            name="灭世者的死亡之帽",
            gold=3500,
            category="complete_equipment",
            stats={"FlatMagicDamageMod": 130.0},
        )
        store = DataStore(static)
        store._heroes["10"] = HeroStat("10", "凯尔", "Kayle", 50000, 27500, 0.55, 0.06, 7)
        store._core_builds["10"] = [CoreBuild(("3115", "3089"), True, 20000, 11400, 0.57, 0.40)]
        store._item_recs["10"] = {
            "3115": ItemRecStat("3115", 35000, 19950, 0.57, False),
            "3089": ItemRecStat("3089", 25000, 14500, 0.58, False),
        }

        sm = HexStateMachine()
        me = PlayerLiveInfo("me", "凯尔", "Kayle", "ORDER", False, 6, 1, 0, 2, ())
        e1 = PlayerLiveInfo("e1", "亚托克斯", "Aatrox", "CHAOS", False, 6, 1, 1, 1, ())
        sm.on_live_snapshot(LiveMatchSnapshot(90.0, me, (me,), (e1,), 1400.0), "10", now_mono=10.0)

        # Step 1: Mis-recognized 3059 (没法力，没问题) locks mana plan onto 大天使之杖 (3003)
        sm.add_selected_augment("3059")
        sm.locked_next_item = "3003"
        sm.committed_plan = ["3003", "2503", "3089"]
        sm.plan_overhaul_reason = "海克斯变阵：没法力，没问题"
        sm.allow_plan_overhaul = False

        # Step 2: Player corrects 3059 -> 1028 (侵蚀) via 1-click swap!
        assert sm.replace_last_augment("1028") is True
        assert sm.selected_augments == ["1028"]
        assert sm.committed_plan == []
        assert sm.locked_next_item == ""
        assert sm.allow_plan_overhaul is True
        assert sm.plan_overhaul_reason == ""

        # Step 3: Re-running optimizer + reconcile immediately rolls back to Kayle's real 纳什之牙 (3115)!
        _, recs = optimize_next_items(store, "10", (), 1400.0, sm.selected_augments, (me,), (e1,), committed_plan=sm.committed_plan, locked_next_item=sm.locked_next_item)
        plan = build_next_buy_plan(store, "10", (), 1400.0, recs)
        assert plan is not None
        new_lock, new_plan = reconcile_match_build_plan(store, (), sm.committed_plan, sm.locked_next_item, list(plan.remaining_build_ids))
        assert new_lock == "3115"
        assert "3003" not in new_plan and "2503" not in new_plan

        # Step 3b: Even if reconcile receives non-empty old committed_plan with allow_overhaul=True, stale 3003/2503 must NOT leak into tail!
        ov_lock, ov_plan = reconcile_match_build_plan(
            store, (), ["3003", "2503"], "3003", ["3115", "3089"], allow_overhaul=True
        )
        assert ov_lock == "3115"
        assert ov_plan == ["3115", "3089"]

        # Step 4: Same rollback works via manual remove ('×' chip) too!
        sm.locked_next_item = "3003"
        sm.committed_plan = ["3003", "2503"]
        sm.plan_overhaul_reason = "海克斯变阵：没法力，没问题"
        sm.allow_plan_overhaul = False
        assert sm.remove_selected_augment(0) is True
        assert sm.committed_plan == []
        assert sm.locked_next_item == ""
        assert sm.allow_plan_overhaul is True
        assert sm.plan_overhaul_reason == ""

        # Step 5: Controller rollback rotates _latest_request_id so a late in-flight 没法力 Gemini reply is DROPPED,
        # and recomputes overhaul_reason strictly from remaining selected augments!
        from hex_helper.app import HexHelperController
        from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload

        store._augments["3059"] = AugmentInfo("3059", "没法力，没问题", "silver")
        store._augments["1028"] = AugmentInfo("1028", "侵蚀", "silver")
        store._augments["2045"] = AugmentInfo("2045", "珠光护手", "prismatic")

        class _DummyDrawer:
            def __init__(self) -> None:
                self.advice = "INIT"
                self.committed = None

            def set_coach_advice(self, adv) -> None:
                self.advice = adv

            def set_committed_plan(self, plan, lock, reason) -> None:
                self.committed = (list(plan), lock, reason)

        ctrl = HexHelperController.__new__(HexHelperController)
        ctrl.sm = sm
        ctrl.store = store
        ctrl.edge_drawer = _DummyDrawer()
        ctrl._latest_advice = CoachAdvicePayload(
            request_id="hex-old-mana",
            next_items=[CoachAdviceItem("3003", "大天使之杖", "下件必出", "没法力核心")],
            build_path=["大天使之杖", "黯炎火炬"],
        )
        ctrl._latest_request_id = "hex-old-mana"
        ctrl._last_live = None

        # Simulate mis-picked 3059 swapped -> 1028 while hex-old-mana is still in flight
        sm.selected_augments = ["1028"]
        ctrl._rollback_plan_and_advice_for_augment_correction()
        assert ctrl._latest_advice is None
        assert ctrl.edge_drawer.advice is None
        assert ctrl.sm.plan_overhaul_reason == ""
        assert ctrl._latest_request_id.startswith("rollback-")

        # Late reply from old 没法力 request MUST be dropped (must NOT re-lock 3003!)
        stale_reply = CoachAdvicePayload(
            request_id="hex-old-mana",
            next_items=[CoachAdviceItem("3003", "大天使之杖", "下件必出", "没法力核心")],
            build_path=["大天使之杖"],
        )
        ctrl._on_coach_advice(stale_reply)
        assert ctrl._latest_advice is None
        assert ctrl.sm.locked_next_item == ""

        # If player STILL owns another transformative augment (2045 珠光护手), rollback keeps THAT label only
        sm.selected_augments = ["2045", "1028"]
        ctrl._rollback_plan_and_advice_for_augment_correction()
        assert ctrl.sm.plan_overhaul_reason == "海克斯变阵：珠光护手"
