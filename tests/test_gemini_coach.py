"""Tests for the decoupled direct Gemini 4 Hydrogen (xhigh) coach integration."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload
from hex_helper.coach.gemini_coach import (
    COACH_RESPONSE_SCHEMA,
    GeminiCallStats,
    GeminiCoachClient,
    GeminiCoachWorker,
    build_coach_prompt_payload,
    ground_coach_advice,
)
from hex_helper.data.provider import DatasetDescriptor, HeroStat, ItemStaticInfo
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore


def _make_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "cache")
    static.items = {
        "3084": ItemStaticInfo(item_id="3084", name="心之钢", gold=3000, tags=("Health",), category="complete_equipment"),
        "3083": ItemStaticInfo(item_id="3083", name="狂徒铠甲", gold=3100, tags=("Health",), category="complete_equipment"),
        "3065": ItemStaticInfo(item_id="3065", name="振奋盔甲", gold=2700, tags=("SpellBlock",), category="complete_equipment"),
        "3033": ItemStaticInfo(item_id="3033", name="凡性的提醒", gold=3000, tags=("Damage",), category="complete_equipment"),
        "773111": ItemStaticInfo(item_id="773111", name="水银之靴", gold=1200, tags=("Boots",), category="boots"),
        "3111": ItemStaticInfo(item_id="3111", name="水银之靴", gold=1200, tags=("Boots",), category="boots"),
    }
    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "datasets/16.23/normal", "2026-11-21", True)
    store._heroes["36"] = HeroStat("36", "蒙多医生", "祖安狂人", 250000, 128175, 0.5127, 0.08, 40, False)
    return store


def test_gemini_client_calls_interactions_with_hydrogen_xhigh_and_grounds_items(tmp_path: Path):
    store = _make_store(tmp_path)
    captured_calls: list[dict] = []

    class _FakeInteractions:
        def create(self, **kwargs):
            captured_calls.append(kwargs)
            body = {
                "augment_pick_advice": "这轮不该保留（无offered）",
                "buy_now": "1600G先买巨人腰带+抗魔斗篷",
                "build_path": ["心之钢", "狂徒铠甲", "振奋盔甲"],
                "next_items": [
                    {"item_id": "3084", "name": "心之钢", "priority": "下件必出", "reason": "已拥有应被剔除"},
                    {"item_id": "", "name": "狂徒铠甲", "priority": "下件必出", "reason": "延续钢神双核续航"},
                    {"item_id": "3065", "name": "振奋盔甲", "priority": "次选", "reason": "敌方3AP补魔抗"},
                ],
                "tactical_summary": "站前排吃第一波技能，开R顶住等队友输出",
            }
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps(body, ensure_ascii=False),
                usage=SimpleNamespace(total_tokens=950, total_thought_tokens=720, total_output_tokens=230),
            )

    class _FakeGenAIClient:
        def __init__(self) -> None:
            self.interactions = _FakeInteractions()
            self.closed = False

        def close(self) -> None:
            self.closed = True

    factory_args: list[tuple[str, str, float]] = []
    fake = _FakeGenAIClient()

    def fake_factory(api_key: str, proxy_url: str, timeout_s: float):
        factory_args.append((api_key, proxy_url, timeout_s))
        return fake

    req = CoachRequestPayload(
        request_id="req-mundo-01",
        trigger_reason="death_enemy_build_changed",
        timestamp="2026-11-21T19:45:00+08:00",
        dataset_patch="26.23",
        my_hero={"hero_id": "36", "name": "蒙多医生", "gold": 1600, "items": ["心之钢", "水银之靴"]},
        offered_augments=[],  # empty -> augment_pick_advice must be stripped!
        committed_build_plan={"owned_complete": ["心之钢"], "allow_overhaul": False},
    )

    prompt_payload = build_coach_prompt_payload(req, store)
    assert "3033" in prompt_payload["item_glossary"]
    assert "3084" in prompt_payload["item_glossary"]

    client = GeminiCoachClient(client_factory=fake_factory)
    advice, stats = client.advise(
        req,
        store,
        api_key="AIzaTestKey",
        model="gemini-4-hydrogen",
        thinking_level="xhigh",
        adaptive_thinking=False,
        proxy_url="http://127.0.0.1:7900",
    )

    assert factory_args == [("AIzaTestKey", "http://127.0.0.1:7900", 90.0)]
    # Persistent pool keeps the warm client open until config changes or client.close()
    assert fake.closed is False
    # Second call with same key & proxy reuses the exact pooled client (factory called once!)
    advice2, stats2 = client.advise(
        req,
        store,
        api_key="AIzaTestKey",
        model="gemini-4-hydrogen",
        thinking_level="xhigh",
        adaptive_thinking=True,
        proxy_url="http://127.0.0.1:7900",
    )
    assert len(factory_args) == 1
    assert stats2.thinking_level == "medium"  # death_enemy_build_changed adapts xhigh -> medium!
    client.close()
    assert fake.closed is True

    assert len(captured_calls) == 2
    call = captured_calls[0]
    assert call["model"] == "gemini-4-hydrogen"
    assert call["generation_config"] == {"thinking_level": "medium"}
    assert captured_calls[1]["generation_config"] == {"thinking_level": "medium"}
    assert call["response_format"]["schema"] == COACH_RESPONSE_SCHEMA
    assert call["store"] is False

    # Grounding verification: 3084 already owned -> dropped; 狂徒铠甲 missing item_id -> canonicalized to 3083!
    assert [it.item_id for it in advice.next_items] == ["3083", "3065"]
    assert advice.next_items[0].name == "狂徒铠甲"
    # Build path removes already owned 心之钢
    assert advice.build_path == ["狂徒铠甲", "振奋盔甲"]
    # Empty offered_augments clears stale pick tip
    assert advice.augment_pick_advice == ""
    assert "gemini-4-hydrogen" in advice.source
    assert "medium" in advice.source
    assert stats.total_tokens == 950 and stats.thought_tokens == 720


def test_gemini_worker_latest_wins_drops_superseded_requests(tmp_path: Path):
    store = _make_store(tmp_path)
    started_first = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []
    results: list[tuple[CoachAdvicePayload, GeminiCallStats]] = []
    done_ev = threading.Event()

    class _SlowClient(GeminiCoachClient):
        def advise(self, req: CoachRequestPayload, store: DataStore | None, **kwargs):
            calls.append(req.request_id)
            if req.request_id == "r1":
                started_first.set()
                release_first.wait(timeout=3.0)
            adv = CoachAdvicePayload(
                request_id=req.request_id,
                next_items=[],
                buy_now=f"buy-{req.request_id}",
                source="Gemini (gemini-4-hydrogen · xhigh)",
            )
            st = GeminiCallStats("gemini-4-hydrogen", "xhigh", 12, 100, 80, 20)
            return adv, st

    worker = GeminiCoachWorker(
        on_advice=lambda adv, st: (results.append((adv, st)), done_ev.set() if adv.request_id == "r3" else None),
        client=_SlowClient(),
    )
    try:
        worker.submit(CoachRequestPayload("r1", "t", "ts", "26.23", {}), store, api_key="k", model="gemini-4-hydrogen", thinking_level="xhigh", proxy_url="")
        assert started_first.wait(timeout=2.0)
        # Rapidly supersede with r2 and r3 while r1 is in flight
        worker.submit(CoachRequestPayload("r2", "t", "ts", "26.23", {}), store, api_key="k", model="gemini-4-hydrogen", thinking_level="xhigh", proxy_url="")
        worker.submit(CoachRequestPayload("r3", "t", "ts", "26.23", {}), store, api_key="k", model="gemini-4-hydrogen", thinking_level="xhigh", proxy_url="")
        release_first.set()
        assert done_ev.wait(timeout=3.0)
        # Wait briefly to verify no r2 executed
        time.sleep(0.1)
        assert calls == ["r1", "r3"]
        # r1 was superseded before finishing so callback MUST only deliver r3!
        assert [adv.request_id for adv, _ in results] == ["r3"]
    finally:
        worker.stop()


def test_ground_coach_advice_normalizes_77_prefix_owned(tmp_path: Path):
    store = _make_store(tmp_path)
    req = CoachRequestPayload(
        request_id="req-77",
        trigger_reason="manual_f6",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "36", "name": "蒙多医生", "items": ["水银之靴"]},
        committed_build_plan={"owned_complete": ["水银之靴"]},
    )
    adv = ground_coach_advice(
        {
            "next_items": [
                {"item_id": "773111", "name": "水银之靴", "priority": "下件必出", "reason": "魔抗鞋"},
                {"item_id": "3084", "name": "心之钢", "priority": "下件必出", "reason": "钢神"},
            ],
            "build_path": ["水银之靴", "心之钢", "狂徒铠甲"],
            "buy_now": "900G先买巨人腰带",
            "tactical_summary": "站稳前排叠心之钢",
        },
        req,
        store,
    )
    assert [it.item_id for it in adv.next_items] == ["3084"]
    assert adv.build_path == ["心之钢", "狂徒铠甲"]


def test_ground_coach_advice_maps_legacy_3151_and_blocks_ghost_or_component_plan_items(tmp_path: Path):
    from hex_helper.scoring.matchup_optimizer import reconcile_match_build_plan

    store = _make_store(tmp_path)
    # Add 6653 (兰德里的折磨), 3135 (虚空之杖), 3916 (湮灭宝珠 800G component), 2139 (巫术合剂 500G consumable)
    from hex_helper.data.provider import ItemStaticInfo

    store.static.items["6653"] = ItemStaticInfo(item_id="6653", name="兰德里的折磨", gold=3000, category="complete_equipment", stats={"FlatMagicDamageMod": 70.0})
    store.static.items["3135"] = ItemStaticInfo(item_id="3135", name="虚空之杖", gold=3000, category="complete_equipment", stats={"FlatMagicDamageMod": 95.0})
    store.static.items["3916"] = ItemStaticInfo(item_id="3916", name="湮灭宝珠", gold=800, category="component", stats={"FlatMagicDamageMod": 25.0})
    store.static.items["2139"] = ItemStaticInfo(item_id="2139", name="巫术合剂", gold=500, category="consumable")

    req = CoachRequestPayload(
        request_id="req-3151",
        trigger_reason="manual_f6",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "21", "name": "厄运小姐", "items": ["兰德里的折磨"]},
        committed_build_plan={"owned_complete": ["兰德里的折磨"]},
    )
    adv = ground_coach_advice(
        {
            "next_items": [
                # Deleted S10 legacy ID 3151 -> maps to 6653 -> dropped because 兰德里的折磨 is already owned!
                {"item_id": "3151", "name": "大面具", "priority": "推荐", "reason": "灼烧"},
                # Completely fake ID 9999 -> MUST BE DROPPED (never becomes 装备#9999 / 装备#3!)
                {"item_id": "9999", "name": "虚构神器", "priority": "推荐", "reason": "幻觉"},
                # Real item with nickname -> canonicalized cleanly
                {"item_id": "", "name": "法穿棒", "priority": "下件必出", "reason": "穿透"},
            ],
            "build_path": ["大面具", "法穿棒", "湮灭宝珠", "巫术合剂", "不存在的装备"],
            "buy_now": "买爆裂魔杖(850G)",
            "tactical_summary": "打坦补法穿",
        },
        req,
        store,
    )
    assert [it.item_id for it in adv.next_items] == ["3135"]
    assert adv.next_items[0].name == "虚空之杖"
    assert adv.build_path == ["虚空之杖"]

    # reconcile_match_build_plan must ALSO purge any ghost IDs (3151/9999) or basic components (3916/2139)
    lock, plan = reconcile_match_build_plan(
        store=store,
        my_items=("6653",),
        committed_plan=["3135", "3151", "3916", "2139", "9999"],
        locked_next_item="3135",
        proposed_ids=["3135", "3084", "3916"],
        allow_overhaul=False,
    )
    assert lock == "3135"
    assert plan == ["3135", "3084"]


def test_full_six_slot_build_stops_pushing_7th_item_unless_justified_swap(tmp_path: Path):
    from hex_helper.data.provider import ItemStaticInfo
    from hex_helper.scoring.matchup_optimizer import (
        DynamicItemRec,
        build_next_buy_plan,
        reconcile_match_build_plan,
    )

    store = _make_store(tmp_path)
    items_meta = {
        "6617": ("月石再生器", 2200, "complete_equipment"),
        "3504": ("炽热香炉", 2200, "complete_equipment"),
        "6616": ("流水法杖", 2250, "complete_equipment"),
        "3107": ("救赎", 2300, "complete_equipment"),
        "3222": ("米凯尔的祝福", 2300, "complete_equipment"),
        "3089": ("灭世者的死亡之帽", 3500, "complete_equipment"),
        "3165": ("莫雷洛秘典", 2850, "complete_equipment"),
        "3102": ("女妖面纱", 3000, "complete_equipment"),
        "3020": ("法师之靴", 1100, "other"),
        "2139": ("巫术合剂", 500, "other"),
        "2052": ("魄罗佳肴", 0, "other"),
    }
    for iid, (nm, g, cat) in items_meta.items():
        st = {"FlatMagicDamageMod": 60.0} if cat == "complete_equipment" else {}
        store.static.items[iid] = ItemStaticInfo(item_id=iid, name=nm, gold=g, category=cat, stats=st)

    six_items = ("6617", "3504", "6616", "3107", "3222", "3089", "2052")
    dummy_rec = [DynamicItemRec("3102", "女妖面纱", 3000, False, 1500, 10.0, 0.55, 500, ("高胜率",), ())]

    # 1) Local engine at 6 complete items -> MUST recommend Elixir (2139), ZERO remaining_build_ids, clear lock!
    plan6 = build_next_buy_plan(store, "40", six_items, my_gold=1400.0, dynamic_recs=dummy_rec)
    assert plan6 is not None and plan6.is_full_build is True and plan6.full_build_mode == "six_complete"
    assert plan6.target.item_id == "2139"
    assert plan6.remaining_build_ids == ()
    assert plan6.backup is None
    assert reconcile_match_build_plan(store, six_items, ["3102"], "3102", ["3102"]) == ("", [])

    # 2) Local engine at 5 complete items + Tier-2 boots with only 900G -> MUST NOT ask to buy components!
    five_plus_boots = ("6617", "3504", "6616", "3107", "3222", "3020", "2052")
    plan5b_low = build_next_buy_plan(store, "40", five_plus_boots, my_gold=900.0, dynamic_recs=dummy_rec)
    assert plan5b_low is not None and plan5b_low.is_full_build is True and plan5b_low.full_build_mode == "five_plus_boots"
    assert plan5b_low.target.item_id == "2139"
    assert plan5b_low.backup is None
    # With 2400G (+ 770G selling Sorc Shoes = 3170G >= 3000G), optional boot-swap backup appears!
    plan5b_rich = build_next_buy_plan(store, "40", five_plus_boots, my_gold=2400.0, dynamic_recs=dummy_rec)
    assert plan5b_rich is not None and plan5b_rich.backup is not None and plan5b_rich.backup.item_id == "3102"

    # 3) Gemini grounding at 6 complete items:
    req6 = CoachRequestPayload(
        request_id="req-full6",
        trigger_reason="manual_f6",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={
            "hero_id": "40",
            "name": "迦娜",
            "gold": 1200,
            "items": ["月石再生器", "炽热香炉", "流水法杖", "救赎", "米凯尔的祝福", "灭世者的死亡之帽", "魄罗佳肴"],
        },
        committed_build_plan={"owned_complete": ["月石再生器", "炽热香炉", "流水法杖", "救赎", "米凯尔的祝福", "灭世者的死亡之帽"]},
    )
    # 3a) Unjustified casual 7th item -> MUST BE DROPPED and replaced by 500G Elixir!
    adv_unjustified = ground_coach_advice(
        {
            "next_items": [{"item_id": "3102", "name": "女妖面纱", "priority": "下件必出", "reason": "补点魔抗法强"}],
            "build_path": ["女妖面纱", "莫雷洛秘典"],
            "buy_now": "先买苍翠屏障",
            "tactical_summary": "站后排保C",
        },
        req6,
        store,
    )
    assert [it.item_id for it in adv_unjustified.next_items] == ["2139"]
    assert adv_unjustified.build_path == []
    assert "合剂" in adv_unjustified.buy_now

    # 3b) Justified mandatory swap ("卖【炽热香炉】换【莫雷洛秘典】") -> PRESERVED cleanly!
    adv_swap = ground_coach_advice(
        {
            "next_items": [
                {"item_id": "3165", "name": "莫雷洛秘典", "priority": "必须换装", "reason": "敌剑魔蒙多高回复0重伤，卖炽热香炉换鬼书"},
                {"item_id": "2139", "name": "巫术合剂", "priority": "满格合剂", "reason": "满格补50法强"},
            ],
            "build_path": ["莫雷洛秘典"],
            "buy_now": "卖炽热香炉换莫雷洛秘典",
            "tactical_summary": "挂重伤断剑魔回血",
        },
        req6,
        store,
    )
    assert [it.item_id for it in adv_swap.next_items] == ["3165", "2139"]
    assert adv_swap.build_path == []


def test_real_genai_sdk_client_constructs_with_proxy_without_network():
    import sys

    had_genai = "google.genai" in sys.modules
    client = GeminiCoachClient._default_client_factory(
        "AIzaDummyKeyForUnitCheck",
        "http://127.0.0.1:7900",
        90.0,
    )
    try:
        assert hasattr(client, "interactions")
    finally:
        client.close()
        if not had_genai:
            for mod_name in [k for k in list(sys.modules) if k == "google.genai" or k.startswith("google.genai.")]:
                sys.modules.pop(mod_name, None)


def test_pooled_client_reconnects_once_on_stale_socket_and_evicts_on_proxy_change(tmp_path: Path):
    store = _make_store(tmp_path)
    created: list[SimpleNamespace] = []

    def factory(api_key: str, proxy_url: str, timeout_s: float):
        idx = len(created)
        closed_flag = {"closed": False}

        class _Inter:
            def create(self, **kwargs):
                # First client succeeds once, then simulates an idle pooled socket reset on 2nd call!
                if idx == 0 and created[0].calls >= 1:
                    raise ConnectionResetError("pooled socket closed by peer")
                created[idx].calls += 1
                return SimpleNamespace(
                    status="completed",
                    output_text=json.dumps(
                        {
                            "augment_pick_advice": "",
                            "buy_now": "ok",
                            "build_path": ["心之钢"],
                            "next_items": [],
                            "tactical_summary": "ok",
                        }
                    ),
                    usage=None,
                )

        obj = SimpleNamespace(
            idx=idx,
            proxy=proxy_url,
            calls=0,
            interactions=_Inter(),
            closed_flag=closed_flag,
            close=lambda: closed_flag.__setitem__("closed", True),
        )
        created.append(obj)
        return obj

    client = GeminiCoachClient(client_factory=factory)
    req = CoachRequestPayload("r-pool", "augment_picking", "ts", "26.23", {})
    _, st1 = client.advise(req, store, api_key="k", proxy_url="http://127.0.0.1:7900")
    assert st1.thinking_level == "low"
    assert len(created) == 1

    # 2nd call hits ConnectionResetError on pooled client #0 -> evicts & retries once on fresh client #1!
    _, st2 = client.advise(req, store, api_key="k", proxy_url="http://127.0.0.1:7900")
    assert st2.thinking_level == "low"
    assert len(created) == 2
    assert created[0].closed_flag["closed"] is True
    assert created[1].closed_flag["closed"] is False

    # Changing proxy evicts client #1 and opens client #2
    client.advise(req, store, api_key="k", proxy_url="http://127.0.0.1:1082")
    assert len(created) == 3
    assert created[1].closed_flag["closed"] is True
    client.close()
    assert created[2].closed_flag["closed"] is True


