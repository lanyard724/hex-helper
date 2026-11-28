"""G7 Verification Tests: P0-7 next_augment_goal + P0-8 Trigger-Aware High-SNR Payload Slicer & Dual-End Sync."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload
from hex_helper.coach.gemini_coach import (
    COACH_RESPONSE_SCHEMA,
    COACH_SYSTEM_INSTRUCTION,
    GeminiCoachClient,
    build_coach_prompt_payload,
    build_high_snr_prompt_payload,
    build_picker_fast_prompt_payload,
    ground_coach_advice,
)
from hex_helper.data.provider import (
    AugmentInfo,
    CoreBuild,
    DatasetDescriptor,
    HeroAugmentStat,
    HeroStat,
    ItemStaticInfo,
)
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.scoring.matchup_optimizer import ELIXIR_IDS, TIER2_BOOT_IDS

SERVER_ROOT = Path(__file__).resolve().parent.parent / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from hex_server import coach_gateway as server_gw  # noqa: E402

REAL_REQUEST_PATH = Path(r"C:\Users\Administrator\AppData\Local\HexHelper\bridge\request.json")
REAL_CACHE_DIR = Path(r"C:\Users\Administrator\AppData\Local\HexHelper\cache")
REAL_DATASET_DIR = REAL_CACHE_DIR / "datasets" / "16.23" / "normal"


def _make_rich_test_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "cache")
    static.items = {
        # Tank items
        "3084": ItemStaticInfo("3084", "心之钢", 3000, tags=("Health",), stats={"FlatHPPoolMod": 900.0}, category="complete_equipment"),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, tags=("Health", "SpellBlock"), stats={"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 50.0}, category="complete_equipment"),
        "2502": ItemStaticInfo("2502", "无终恨意", 2800, tags=("Health", "Armor"), stats={"FlatHPPoolMod": 400.0, "FlatArmorMod": 55.0}, category="complete_equipment"),
        "2504": ItemStaticInfo("2504", "败魔", 2900, tags=("Health", "SpellBlock"), stats={"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 80.0}, category="complete_equipment"),
        "3119": ItemStaticInfo("3119", "凛冬之临", 2400, tags=("Health", "Mana"), stats={"FlatHPPoolMod": 550.0}, category="complete_equipment"),
        "3083": ItemStaticInfo("3083", "狂徒铠甲", 3100, tags=("Health",), stats={"FlatHPPoolMod": 1000.0}, category="complete_equipment"),
        "3143": ItemStaticInfo("3143", "兰顿之兆", 2700, tags=("Health", "Armor"), stats={"FlatHPPoolMod": 350.0, "FlatArmorMod": 75.0}, category="complete_equipment"),
        "4401": ItemStaticInfo("4401", "自然之力", 2800, tags=("Health", "SpellBlock"), stats={"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 55.0}, category="complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, tags=("Armor", "Health"), stats={"FlatArmorMod": 75.0, "FlatHPPoolMod": 150.0}, category="complete_equipment", has_grievous=True),
        "3110": ItemStaticInfo("3110", "冰霜之心", 2500, tags=("Armor", "Mana"), stats={"FlatArmorMod": 75.0}, category="complete_equipment"),
        "1011": ItemStaticInfo("1011", "巨人腰带", 900, tags=("Health",), stats={"FlatHPPoolMod": 380.0}, category="component"),
        # Bruiser / AD / Crit items
        "6333": ItemStaticInfo("6333", "死亡之舞", 3300, tags=("Damage", "Armor"), stats={"FlatPhysicalDamageMod": 60.0, "FlatArmorMod": 50.0}, category="complete_equipment"),
        "3161": ItemStaticInfo("3161", "朔极之矛", 3100, tags=("Damage", "Health"), stats={"FlatPhysicalDamageMod": 45.0, "FlatHPPoolMod": 450.0}, category="complete_equipment"),
        "2517": ItemStaticInfo("2517", "无穷饥渴", 3000, tags=("Damage", "Health"), stats={"FlatPhysicalDamageMod": 55.0, "FlatHPPoolMod": 300.0}, category="complete_equipment"),
        "6676": ItemStaticInfo("6676", "收集者", 3000, tags=("Damage", "CriticalStrike"), stats={"FlatPhysicalDamageMod": 50.0, "FlatCritChanceMod": 0.25}, category="complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3400, tags=("Damage", "CriticalStrike"), stats={"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25}, category="complete_equipment"),
        "3036": ItemStaticInfo("3036", "多米尼克领主的致意", 3000, tags=("Damage", "CriticalStrike"), stats={"FlatPhysicalDamageMod": 35.0, "FlatCritChanceMod": 0.25}, category="complete_equipment"),
        "3033": ItemStaticInfo("3033", "凡性的提醒", 3200, tags=("Damage", "CriticalStrike"), stats={"FlatPhysicalDamageMod": 35.0, "FlatCritChanceMod": 0.25}, category="complete_equipment", has_grievous=True),
        "3123": ItemStaticInfo("3123", "死刑宣告", 800, tags=("Damage",), stats={"FlatPhysicalDamageMod": 15.0}, category="component", has_grievous=True),
        "6695": ItemStaticInfo("6695", "巨蛇之牙", 2500, tags=("Damage",), stats={"FlatPhysicalDamageMod": 55.0}, category="complete_equipment", has_shield_break=True),
        "3153": ItemStaticInfo("3153", "破败王者之刃", 3200, tags=("Damage", "AttackSpeed"), stats={"FlatPhysicalDamageMod": 40.0, "PercentAttackSpeedMod": 0.25}, category="complete_equipment"),
        "3156": ItemStaticInfo("3156", "玛莫提乌斯之噬", 3100, tags=("Damage", "SpellBlock"), stats={"FlatPhysicalDamageMod": 60.0, "FlatSpellBlockMod": 40.0}, category="complete_equipment"),
        "3026": ItemStaticInfo("3026", "守护天使", 3200, tags=("Damage", "Armor"), stats={"FlatPhysicalDamageMod": 55.0, "FlatArmorMod": 45.0}, category="complete_equipment"),
        "3091": ItemStaticInfo("3091", "智慧末刃", 2800, tags=("AttackSpeed", "SpellBlock"), stats={"PercentAttackSpeedMod": 0.50, "FlatSpellBlockMod": 45.0}, category="complete_equipment"),
        "3139": ItemStaticInfo("3139", "水银弯刀", 3200, tags=("Damage", "SpellBlock"), stats={"FlatPhysicalDamageMod": 40.0, "FlatSpellBlockMod": 40.0}, category="complete_equipment"),
        # AP items
        "3165": ItemStaticInfo("3165", "莫雷洛秘典", 2850, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 75.0}, category="complete_equipment", has_grievous=True),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 130.0}, category="complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 95.0}, category="complete_equipment"),
        "3137": ItemStaticInfo("3137", "蜕生", 2850, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 60.0}, category="complete_equipment"),
        "6653": ItemStaticInfo("6653", "兰德里的折磨", 3000, tags=("SpellDamage", "Health"), stats={"FlatMagicDamageMod": 70.0, "FlatHPPoolMod": 300.0}, category="complete_equipment"),
        "4645": ItemStaticInfo("4645", "影焰", 3200, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 110.0}, category="complete_equipment"),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, tags=("SpellDamage", "Armor"), stats={"FlatMagicDamageMod": 105.0, "FlatArmorMod": 50.0}, category="complete_equipment"),
        "3102": ItemStaticInfo("3102", "女妖面纱", 3000, tags=("SpellDamage", "SpellBlock"), stats={"FlatMagicDamageMod": 105.0, "FlatSpellBlockMod": 40.0}, category="complete_equipment"),
        "3916": ItemStaticInfo("3916", "湮灭宝珠", 800, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 25.0}, category="component", has_grievous=True),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 100.0}, category="complete_equipment"),
        # Boots
        "3111": ItemStaticInfo("3111", "水银之靴", 1250, tags=("Boots",), category="boots"),
        "3047": ItemStaticInfo("3047", "铁板靴", 1200, tags=("Boots",), category="boots"),
        "3009": ItemStaticInfo("3009", "轻灵之靴", 1000, tags=("Boots",), category="boots"),
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, tags=("Boots",), category="boots"),
        "3006": ItemStaticInfo("3006", "狂战士胫甲", 1100, tags=("Boots",), category="boots"),
        "3158": ItemStaticInfo("3158", "明朗之靴", 900, tags=("Boots",), category="boots"),
        # Elixirs & Poro-Snax
        "2138": ItemStaticInfo("2138", "钢铁合剂", 500, tags=("Consumable",), category="consumable"),
        "2139": ItemStaticInfo("2139", "巫术合剂", 500, tags=("Consumable",), category="consumable"),
        "2140": ItemStaticInfo("2140", "愤怒合剂", 500, tags=("Consumable",), category="consumable"),
        "2052": ItemStaticInfo("2052", "魄罗佳肴", 0, tags=(), category="other"),
        # Cheap components
        "1036": ItemStaticInfo("1036", "长剑", 350, tags=("Damage",), stats={"FlatPhysicalDamageMod": 10.0}, category="component"),
        "1052": ItemStaticInfo("1052", "增幅典籍", 400, tags=("SpellDamage",), stats={"FlatMagicDamageMod": 20.0}, category="component"),
    }
    static.hero_aliases.update({"72": "Skarner", "51": "Caitlyn", "99": "Lux", "36": "DrMundo", "266": "Aatrox"})
    static.hero_cn_names.update({"72": "上古领主", "51": "皮城女警", "99": "光辉女郎", "36": "祖安狂人", "266": "暗裔剑魔"})

    store = DataStore(static)
    store.descriptor = DatasetDescriptor("16.23", "normal", "常规", "datasets/16.23/normal", "2026-11-27", True)
    store._heroes["72"] = HeroStat("72", "斯卡纳", "上古领主", 150000, 70500, 0.47, 0.03, 110, False)
    store._heroes["51"] = HeroStat("51", "凯特琳", "皮城女警", 500000, 249150, 0.4983, 0.11, 50, False)
    store._heroes["99"] = HeroStat("99", "拉克丝", "光辉女郎", 600000, 312000, 0.52, 0.12, 20, False)
    store._heroes["36"] = HeroStat("36", "蒙多医生", "祖安狂人", 250000, 128000, 0.512, 0.06, 35, False)
    store._heroes["266"] = HeroStat("266", "亚托克斯", "暗裔剑魔", 300000, 153000, 0.51, 0.07, 40, False)

    store._core_builds["72"] = [
        CoreBuild(("3084", "2502", "3083"), True, 4200, 2041, 0.486, 0.24),
        CoreBuild(("3084", "3083", "2502"), True, 3100, 1457, 0.470, 0.18),
        CoreBuild(("3084", "3065", "2502"), True, 2000, 920, 0.460, 0.12),
    ]
    store._core_builds["51"] = [
        CoreBuild(("6676", "3031", "3036"), True, 9000, 4608, 0.512, 0.30),
        CoreBuild(("6676", "3036", "3031"), True, 6000, 3030, 0.505, 0.20),
    ]
    store._core_builds["99"] = [
        CoreBuild(("6655", "3089", "3135"), True, 8000, 4280, 0.535, 0.28),
        CoreBuild(("6653", "3089", "3157"), True, 5000, 2600, 0.520, 0.19),
    ]

    store._augments.update(
        {
            "1077": AugmentInfo("1077", "灵魂虹吸", "gold"),
            "1225": AugmentInfo("1225", "双刀流", "prismatic"),
            "1080": AugmentInfo("1080", "会心防御", "silver"),
            "1048": AugmentInfo("1048", "珠光护手", "prismatic"),
            "1045": AugmentInfo("1045", "炼狱导管", "prismatic"),
        }
    )
    store._hero_augments["51"] = {
        "1077": HeroAugmentStat("1077", 15000, 7800, 0.520, False),
        "1225": HeroAugmentStat("1225", 12000, 6360, 0.530, False),
        "1080": HeroAugmentStat("1080", 14000, 7100, 0.507, False),
        "1048": HeroAugmentStat("1048", 11000, 5610, 0.510, False),
    }
    return store


def _load_real_store_if_available() -> DataStore | None:
    if not (REAL_REQUEST_PATH.is_file() and REAL_DATASET_DIR.is_dir()):
        return None
    static = StaticAssetsManager(REAL_CACHE_DIR)
    static.load_cached()
    if not static.items:
        return None
    store = DataStore(static)
    store.load_from_dir(
        DatasetDescriptor("16.23", "normal", "常规", "datasets/16.23/normal", "2026-11-27", True),
        REAL_DATASET_DIR,
    )
    return store


def test_real_request_json_high_snr_payload_diet_and_glossary_purity() -> None:
    """G7 gate on real C:\\Users\\Administrator\\AppData\\Local\\HexHelper\\bridge\\request.json."""
    store = _load_real_store_if_available()
    if store is None:
        pytest.skip("Real local HexHelper request.json or 16.23/normal dataset not present")

    raw = json.loads(REAL_REQUEST_PATH.read_text(encoding="utf-8"))
    is_skarner_ref = str((raw.get("my_hero") or {}).get("hero_id") or "") == "72"

    req_warmup = CoachRequestPayload.from_dict(raw)
    legacy = build_coach_prompt_payload(req_warmup, store)
    legacy_json = json.dumps(legacy, ensure_ascii=False, separators=(",", ":"))
    assert len(legacy_json) >= (4000 if is_skarner_ref else 2000)

    for trigger in ("pre_death_warmup", "death_enemy_build_changed"):
        req_var = CoachRequestPayload.from_dict({**raw, "trigger_reason": trigger})
        diet = build_high_snr_prompt_payload(req_var, store)
        diet_via_flag = build_coach_prompt_payload(req_var, store, high_snr=True)
        assert diet == diet_via_flag

        leg_var = build_coach_prompt_payload(req_var, store)
        leg_var_json = json.dumps(leg_var, ensure_ascii=False, separators=(",", ":"))
        diet_json = json.dumps(diet, ensure_ascii=False, separators=(",", ":"))
        reduction = 1.0 - (len(diet_json) / len(leg_var_json))
        assert len(diet_json) <= 2150, f"{trigger} payload too large: {len(diet_json)} chars"
        assert reduction >= (0.50 if is_skarner_ref else 0.45), (
            f"{trigger} reduction {reduction:.1%} below threshold"
        )

        # Prefix-cache static keys MUST stay strictly before trigger_reason
        keys = list(diet.keys())
        trig_idx = keys.index("trigger_reason")
        assert keys[:4] == ["dataset_patch", "hero_static", "hero_big_data_top_builds", "item_glossary"]
        assert keys.index("item_glossary") < trig_idx

        glossary = diet["item_glossary"]
        # Zero duplicates with local_engine_top_items
        local_ids = {
            str(line).split("|", 1)[0].strip()
            for line in diet.get("local_engine_top_items") or []
            if "|" in str(line)
        }
        assert not (set(glossary.keys()) & local_ids), (
            f"Duplicate local_engine items in glossary: {set(glossary.keys()) & local_ids}"
        )
        # Zero elixirs when building
        if (diet.get("inventory_status") or {}).get("status") == "building":
            for elx_id in ELIXIR_IDS:
                assert elx_id not in glossary, f"Elixir {elx_id} leaked during building stage"

        if is_skarner_ref:
            # Item glossary purity on Tank Skarner (building, 5 completes, no T2 boots)
            for forbidden_id in ("3165", "3089", "3031", "3135", "3033", "4645", "6655"):
                assert forbidden_id not in glossary, f"Cross-profile garbage {forbidden_id} leaked into Tank glossary"
            for local_id in ("3083", "3143", "6333", "4401"):
                assert local_id not in glossary, f"Duplicate local_engine item {local_id} in glossary"
            for owned_id in ("3084", "3065", "2502", "2504", "3119"):
                assert owned_id not in glossary, f"Owned item {owned_id} leaked into glossary"
            assert "hero_signature_hint" not in diet

        # Multi-block compaction assertions
        assert len(diet["hero_big_data_top_builds"]) <= 2
        assert all(isinstance(r, str) and ">" in r for r in diet["hero_big_data_top_builds"])
        if len(raw.get("selected_augments") or []) >= 4:
            assert "augment_combo_context" not in diet  # 4/4 augments -> omitted!

        for sp in diet.get("special_augment_builds") or []:
            assert set(sp.keys()).issubset({"name", "force", "core", "avoid"})
            assert "mechanic" not in sp and "playstyle_tip" not in sp
            if "core" in sp:
                assert len(sp["core"]) <= 3

        assert all(isinstance(line, str) and "|" in line for line in (diet.get("local_engine_top_items") or []))
        assert isinstance(diet["comp_summary"], str) and "我方:" in diet["comp_summary"] and "敌方:" in diet["comp_summary"]
        assert all(isinstance(a, str) for a in diet["allies"]) and len(diet["allies"]) <= 4  # self deduped
        assert all(isinstance(e, str) for e in diet["enemies"]) and len(diet["enemies"]) <= 5
        assert all("闪现" not in a and "雪球" not in a for a in diet["allies"])
        assert all("闪现" not in e and "雪球" not in e for e in diet["enemies"])

        # Server-side slicer achieves >=45-50% reduction on legacy payload & is idempotent on diet payload
        srv_from_legacy = server_gw.slice_prompt_payload_high_snr(leg_var, trigger)
        srv_legacy_json = json.dumps(srv_from_legacy, ensure_ascii=False, separators=(",", ":"))
        assert len(srv_legacy_json) <= 2150, f"Server sliced legacy too large: {len(srv_legacy_json)} chars"
        assert (1.0 - len(srv_legacy_json) / len(leg_var_json)) >= (0.50 if is_skarner_ref else 0.45)
        srv_from_diet = server_gw.slice_prompt_payload_high_snr(diet, trigger)
        assert len(json.dumps(srv_from_diet, ensure_ascii=False, separators=(",", ":"))) <= 2150


def test_glossary_boots_and_full_build_single_profile_elixir(tmp_path: Path) -> None:
    store = _make_rich_test_store(tmp_path)

    # 1) has_tier2_boots=True + building -> ZERO boots AND ZERO elixirs in item_glossary
    req_boots = CoachRequestPayload(
        request_id="r-boots",
        trigger_reason="death_enemy_build_changed",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "51", "name": "凯特琳", "gold": 1200, "items": ["收集者", "狂战士胫甲"]},
        committed_build_plan={"owned_complete": ["收集者"], "allow_overhaul": False},
        local_engine_top_items=[
            {"item_id": "3031", "name": "无尽之刃", "price": 3400, "effective_cost": 3400, "reasons": ["暴击核爆"]}
        ],
    )
    p_boots = build_high_snr_prompt_payload(req_boots, store)
    g_boots = p_boots["item_glossary"]
    assert not (set(g_boots.keys()) & TIER2_BOOT_IDS), f"Boots leaked despite has_tier2_boots=True: {g_boots}"
    assert not (set(g_boots.keys()) & ELIXIR_IDS), f"Elixirs leaked during building: {g_boots}"
    assert "6676" not in g_boots  # owned
    assert "3031" not in g_boots  # already in local_engine_top_items
    assert "3165" not in g_boots and "3089" not in g_boots and "3135" not in g_boots  # cross-profile AP garbage

    # 2) Full 6-slot build -> ONLY the single matching profile elixir (Tank=2138, AD=2140, AP=2139)
    cases = [
        ("72", "斯卡纳", ["心之钢", "振奋盔甲", "无终恨意", "败魔", "凛冬之临", "狂徒铠甲"], "2138"),
        ("51", "凯特琳", ["收集者", "无尽之刃", "多米尼克领主的致意", "凡性的提醒", "破败王者之刃", "守护天使"], "2140"),
        ("99", "拉克丝", ["卢登的伙伴", "灭世者的死亡之帽", "虚空之杖", "中娅沙漏", "影焰", "兰德里的折磨"], "2139"),
    ]
    for hid, hname, six_items, expected_elixir in cases:
        req_full = CoachRequestPayload(
            request_id=f"r-full-{hid}",
            trigger_reason="death_enemy_build_changed",
            timestamp="ts",
            dataset_patch="26.23",
            my_hero={"hero_id": hid, "name": hname, "gold": 900, "items": six_items},
            committed_build_plan={"owned_complete": six_items, "allow_overhaul": False},
        )
        p_full = build_high_snr_prompt_payload(req_full, store)
        assert p_full["inventory_status"]["status"] == "full_6_complete"
        elx_in_glossary = set(p_full["item_glossary"].keys()) & ELIXIR_IDS
        assert elx_in_glossary == {expected_elixir}, f"Hero {hname}: expected only {expected_elixir}, got {elx_in_glossary}"


def test_game_start_route_strips_items_kda_elixirs_and_stays_under_1500_chars(tmp_path: Path) -> None:
    store = _make_rich_test_store(tmp_path)
    req = CoachRequestPayload(
        request_id="r-gs-1",
        trigger_reason="game_start",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "51", "name": "凯特琳", "gold": 1400, "kda": "0/0/0", "items": ["长剑", "魄罗佳肴"]},
        allies=[
            {"champion": "皮城女警", "kda": "0/0/0", "items": ["长剑", "魄罗佳肴"], "spells": ["闪现", "治疗术"]},
            {"champion": "斯卡纳", "kda": "0/0/0", "items": ["巨人腰带"], "spells": ["闪现", "雪球"]},
            {"champion": "拉克丝", "kda": "0/0/0", "items": ["增幅典籍"], "spells": ["闪现", "清晰术"]},
        ],
        enemies=[
            {"champion": "蒙多医生", "kda": "0/0/0", "items": ["巨人腰带"], "spells": ["闪现", "雪球"]},
            {"champion": "亚托克斯", "kda": "0/0/0", "items": ["长剑"], "spells": ["闪现", "雪球"]},
        ],
        local_engine_top_items=[
            {"item_id": "6676", "name": "收集者", "price": 3000, "effective_cost": 3000, "reasons": ["高胜率首件"]}
        ],
        hero_signature_hint={"archetype": "暴击长手射手", "starter_items": ["收集者(6676)"]},
        lobby_scout={
            "puuid": "must-strip",
            "top_enemy_champion": "亚托克斯",
            "top_enemy_rating": 3100,
            "top_enemy_tag": "钻一·WR68%",
            "enemy_premades": ["2黑(亚托克斯+蒙多医生)"],
            "extra_noise_field": "drop_me",
        },
    )
    p = build_high_snr_prompt_payload(req, store)
    serialized = json.dumps(p, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= 1500, f"game_start payload too large: {len(serialized)} chars"
    assert "items" not in p["my_hero"]
    assert "kda" not in p["my_hero"]
    assert "inventory_status" not in p
    assert not (set(p["item_glossary"].keys()) & ELIXIR_IDS)
    assert "comp_summary" in p and "最肥:" not in p["comp_summary"]
    assert all(":" not in a and "0/0/0" not in a for a in p["allies"])
    assert all(":" not in e and "0/0/0" not in e for e in p["enemies"])
    assert len(p["hero_big_data_top_builds"]) <= 2
    assert len(p["augment_combo_context"]["next_aug_pool"]) == 4
    assert "puuid" not in p["lobby_scout"] and "extra_noise_field" not in p["lobby_scout"]
    assert p["lobby_scout"]["top_enemy_champion"] == "亚托克斯"


def test_augment_picking_route_includes_crit_chance_pct_and_comp_summary(tmp_path: Path) -> None:
    store = _make_rich_test_store(tmp_path)

    # 1) Crit > 0 (Caitlyn with Collector + IE = 50% crit, or live my_stats critChance=1.0 -> 100%)
    req_crit = CoachRequestPayload(
        request_id="r-pick-crit",
        trigger_reason="augment_picking",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={
            "hero_id": "51",
            "name": "凯特琳",
            "gold": 1200,
            "items": ["收集者", "无尽之刃"],
            "my_stats": {"critChance": 1.0},
        },
        selected_augments=[{"id": "1047", "name": "关键暴击", "rarity": "gold"}],
        offered_augments=[
            {"id": "1077", "name": "灵魂虹吸", "rarity": "gold", "delta": 0.021},
            {"id": "1047", "name": "关键暴击", "rarity": "gold", "delta": -0.017},
        ],
        allies=[{"champion": "斯卡纳", "kda": "2/3/9", "items": ["心之钢"]}],
        enemies=[
            {"champion": "蒙多医生", "kda": "7/2/8", "items": ["心之钢", "狂徒铠甲"], "spells": ["闪现", "虚弱"]},
            {"champion": "亚托克斯", "kda": "5/3/6", "items": ["死亡之舞"], "spells": ["闪现", "雪球"]},
        ],
    )
    p_route1 = build_high_snr_prompt_payload(req_crit, store)
    assert p_route1["my_hero"]["crit_chance_pct"] == 100
    assert "comp_summary" in p_route1 and "蒙多医生" in p_route1["comp_summary"]
    assert "enemy_champs" not in p_route1  # replaced by 1-line comp_summary in Route 1!
    assert "item_glossary" not in p_route1

    # 2) Crit == 0 (Lux with AP items) -> crit_chance_pct MUST be omitted
    req_zero = CoachRequestPayload(
        request_id="r-pick-ap",
        trigger_reason="augment_picking",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "99", "name": "拉克丝", "gold": 1500, "items": ["卢登的伙伴"]},
        offered_augments=[{"id": "1048", "name": "珠光护手", "rarity": "prismatic", "delta": 0.032}],
        enemies=[{"champion": "蒙多医生", "kda": "3/3/5", "items": ["心之钢"]}],
    )
    p_zero = build_picker_fast_prompt_payload(req_zero, store, compact_comp=True)
    assert "crit_chance_pct" not in p_zero["my_hero"]
    assert "comp_summary" in p_zero


def test_p07_next_aug_pool_under_4_vs_full_4_and_grounding_and_protocol(tmp_path: Path) -> None:
    store = _make_rich_test_store(tmp_path)

    # 1) Protocol backward compatibility & roundtrip for next_augment_goal
    legacy_adv = CoachAdvicePayload.from_dict({"request_id": "r-old", "buy_now": "买无尽"})
    assert legacy_adv.next_augment_goal == ""
    assert legacy_adv.to_dict()["next_augment_goal"] == ""

    adv_goal = CoachAdvicePayload.from_dict(
        {"request_id": "r-new", "buy_now": "买凡性的提醒", "next_augment_goal": "  下轮追灵魂虹吸/双刀流打敌双坦  "}
    )
    assert adv_goal.next_augment_goal == "下轮追灵魂虹吸/双刀流打敌双坦"
    assert CoachAdvicePayload.from_dict(adv_goal.to_dict()).next_augment_goal == "下轮追灵魂虹吸/双刀流打敌双坦"

    # 2) <4 augments on death_enemy_build_changed -> includes next_aug_pool (<=4 items)
    combo_ctx = {
        "round_index": 3,
        "direction": "AD 暴击射手流",
        "primary_profile": "AD",
        "top_targets": [
            {"id": "1077", "name": "灵魂虹吸", "rarity": "gold", "tier": "S", "delta": 0.021, "comp_tag": "打敌多坦"},
            {"id": "1225", "name": "双刀流", "rarity": "prismatic", "tier": "S", "delta": 0.032, "comp_tag": "打敌多坦"},
            {"id": "1080", "name": "会心防御", "rarity": "silver", "tier": "A", "delta": 0.009, "comp_tag": "防突进保命"},
            {"id": "1048", "name": "珠光护手", "rarity": "prismatic", "tier": "A", "delta": 0.012, "comp_tag": ""},
            {"id": "1045", "name": "炼狱导管", "rarity": "prismatic", "tier": "B", "delta": 0.005, "comp_tag": ""},
        ],
    }
    req_2aug = CoachRequestPayload(
        request_id="r-2aug",
        trigger_reason="death_enemy_build_changed",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "51", "name": "凯特琳", "gold": 1100, "items": ["收集者"]},
        selected_augments=[
            {"id": "1001", "name": "卡1", "rarity": "silver"},
            {"id": "1002", "name": "卡2", "rarity": "gold"},
        ],
        augment_combo_context=combo_ctx,
        local_engine_top_items=[
            "3033|凡性的提醒|差2100G|可先买死刑宣告(800G)|敌高回复无重伤",
        ],
    )
    p_2aug = build_high_snr_prompt_payload(req_2aug, store)
    pool = p_2aug["augment_combo_context"]["next_aug_pool"]
    assert len(pool) == 4
    assert pool[0] == "灵魂虹吸(金,S,+2.1%,打敌多坦)"
    assert pool[1] == "双刀流(彩,S,+3.2%,打敌多坦)"
    assert pool[2] == "会心防御(银,A,+0.9%,防突进保命)"

    # ground_coach_advice preserves next_augment_goal on <4 augments death AND validates items against compact/pipe local_engine_top_items!
    grounded_2 = ground_coach_advice(
        {
            "augment_pick_advice": "",
            "next_augment_goal": "下轮追灵魂虹吸/双刀流打敌双坦",
            "buy_now": "先买死刑宣告(800G)挂重伤",
            "build_path": ["凡性的提醒", "无尽之刃"],
            "next_items": [
                {"item_id": "3033", "name": "凡性的提醒", "priority": "下件必出", "reason": "敌方蒙多剑魔高回复缺重伤"}
            ],
            "tactical_summary": "站后排夹子封走位风筝前排",
        },
        req_2aug,
        store,
    )
    assert grounded_2.next_augment_goal == "下轮追灵魂虹吸/双刀流打敌双坦"
    assert [it.item_id for it in grounded_2.next_items] == ["3033"]
    assert grounded_2.build_path == ["凡性的提醒", "无尽之刃"]

    # Also verify grounding with store=None & pipe-delimited local_engine_top_items!
    grounded_nostore = ground_coach_advice(
        {
            "augment_pick_advice": "",
            "next_augment_goal": "下轮追灵魂虹吸",
            "buy_now": "买死刑宣告",
            "build_path": ["凡性的提醒"],
            "next_items": [{"item_id": "3033", "name": "凡性的提醒", "priority": "下件必出", "reason": "重伤"}],
            "tactical_summary": "拉扯输出",
        },
        req_2aug,
        store=None,
    )
    assert [it.item_id for it in grounded_nostore.next_items] == ["3033"]
    assert grounded_nostore.next_augment_goal == "下轮追灵魂虹吸"

    # 3) 4/4 augments -> omits augment_combo_context AND forces next_augment_goal == ""
    req_4aug = CoachRequestPayload(
        request_id="r-4aug",
        trigger_reason="death_enemy_build_changed",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "51", "name": "凯特琳", "gold": 3100, "items": ["收集者"]},
        selected_augments=[{"id": str(i), "name": f"卡{i}", "rarity": "gold"} for i in range(4)],
        augment_combo_context=combo_ctx,
    )
    p_4aug = build_high_snr_prompt_payload(req_4aug, store)
    assert "augment_combo_context" not in p_4aug

    grounded_4 = ground_coach_advice(
        {
            "augment_pick_advice": "",
            "next_augment_goal": "不应该出现的4槽追卡",
            "buy_now": "直接合无尽",
            "build_path": ["无尽之刃"],
            "next_items": [{"item_id": "3031", "name": "无尽之刃", "priority": "下件必出", "reason": "核心暴击"}],
            "tactical_summary": "打满输出",
        },
        req_4aug,
        store,
    )
    assert grounded_4.next_augment_goal == ""

    # 4) augment_picking -> forces next_augment_goal == ""
    req_pick = CoachRequestPayload(
        request_id="r-pick-clear",
        trigger_reason="augment_picking",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "51", "name": "凯特琳", "gold": 1200, "items": ["收集者"]},
        selected_augments=[{"id": "1001", "name": "卡1", "rarity": "silver"}],
        offered_augments=[{"id": "1077", "name": "灵魂虹吸", "rarity": "gold"}],
    )
    grounded_pick = ground_coach_advice(
        {
            "augment_pick_advice": "选灵魂虹吸打双坦",
            "next_augment_goal": "选卡期应清空",
            "buy_now": "留钱合无尽",
            "build_path": ["无尽之刃"],
            "next_items": [],
            "tactical_summary": "拉扯输出",
        },
        req_pick,
        store,
    )
    assert grounded_pick.augment_pick_advice == "选灵魂虹吸打双坦"
    assert grounded_pick.next_augment_goal == ""


def test_dual_end_system_instruction_schema_and_gateway_sync(tmp_path: Path) -> None:
    # 1) Client & server COACH_SYSTEM_INSTRUCTION are synced & slimmed (<= 1,000 chars)
    assert COACH_SYSTEM_INSTRUCTION == server_gw.COACH_SYSTEM_INSTRUCTION
    assert len(COACH_SYSTEM_INSTRUCTION) <= 1000
    assert len(server_gw.COACH_SYSTEM_INSTRUCTION) <= 1000
    for kw in ("next_augment_goal", "next_aug_pool", "comp_summary", "opening_brief", "game_start", "lobby_scout"):
        assert kw in COACH_SYSTEM_INSTRUCTION

    # 2) Client & server COACH_RESPONSE_SCHEMA are 100% identical with optional next_augment_goal
    assert COACH_RESPONSE_SCHEMA == server_gw.COACH_RESPONSE_SCHEMA
    assert "next_augment_goal" in COACH_RESPONSE_SCHEMA["properties"]
    assert "next_augment_goal" not in COACH_RESPONSE_SCHEMA["required"]

    # 3) Server normalize_advice_json parses next_augment_goal cleanly
    norm_missing = server_gw.normalize_advice_json(
        {"buy_now": "买无尽", "build_path": ["无尽之刃"], "next_items": [], "tactical_summary": "站后排"}
    )
    assert norm_missing["next_augment_goal"] == ""
    norm_present = server_gw.normalize_advice_json(
        {
            "buy_now": "买无尽",
            "next_augment_goal": " 追灵魂虹吸 ",
            "build_path": ["无尽之刃"],
            "next_items": [],
            "tactical_summary": "站后排",
        }
    )
    assert norm_present["next_augment_goal"] == "追灵魂虹吸"

    # 4) GeminiCoachClient.advise uses high-SNR payload on live calls & grounds next_augment_goal
    store = _make_rich_test_store(tmp_path)
    captured_inputs: list[str] = []

    class _FakeInter:
        def create(self, **kwargs: Any):
            captured_inputs.append(kwargs["input"])
            return SimpleNamespace(
                status="completed",
                output_text=json.dumps(
                    {
                        "augment_pick_advice": "",
                        "next_augment_goal": "下轮追灵魂虹吸打蒙多",
                        "buy_now": "先买死刑宣告(800G)",
                        "build_path": ["凡性的提醒", "无尽之刃"],
                        "next_items": [{"item_id": "3033", "name": "凡性的提醒", "priority": "下件必出", "reason": "压高回复"}],
                        "tactical_summary": "站后排拉扯",
                    },
                    ensure_ascii=False,
                ),
                usage=SimpleNamespace(total_tokens=600, total_thought_tokens=150, total_output_tokens=100),
            )

    fake_cli = SimpleNamespace(interactions=_FakeInter(), close=lambda: None)
    coach = GeminiCoachClient(client_factory=lambda _k, _p, _t: fake_cli)
    try:
        req = CoachRequestPayload(
            request_id="r-e2e-diet",
            trigger_reason="death_enemy_build_changed",
            timestamp="ts",
            dataset_patch="26.23",
            my_hero={"hero_id": "51", "name": "凯特琳", "gold": 1000, "items": ["收集者"]},
            selected_augments=[{"id": "1001", "name": "银卡", "rarity": "silver"}],
            allies=[{"champion": "斯卡纳", "kda": "2/3/8", "items": ["心之钢"], "spells": ["闪现", "雪球"]}],
            enemies=[{"champion": "蒙多医生", "kda": "8/2/6", "items": ["心之钢", "狂徒铠甲"], "spells": ["闪现", "虚弱"]}],
            augment_combo_context={
                "direction": "AD暴击",
                "top_targets": [{"id": "1077", "name": "灵魂虹吸", "rarity": "gold", "tier": "S", "delta": 0.021, "comp_tag": "打敌多坦"}],
            },
        )
        adv, _stats = coach.advise(req, store, api_key="AIzaDummy", model="gemini-4-hydrogen")
        assert adv.next_augment_goal == "下轮追灵魂虹吸打蒙多"
        assert [it.item_id for it in adv.next_items] == ["3033"]
        assert '"comp_summary":' in captured_inputs[0]
        assert '"next_aug_pool":' in captured_inputs[0]
        assert '"3165":' not in captured_inputs[0]
    finally:
        coach.close()

    # 5) ServerCoachGateway enforces next_augment_goal=="" on 4/4 augments & sends sliced payload to DeepSeek
    ds_sent: list[dict[str, Any]] = []

    def ds_handler(request: httpx.Request) -> httpx.Response:
        ds_sent.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "augment_pick_advice": "",
                                    "next_augment_goal": "4槽应被服务端清空",
                                    "buy_now": "合狂徒",
                                    "build_path": ["狂徒铠甲"],
                                    "next_items": [{"item_id": "3083", "name": "狂徒铠甲", "priority": "下件必出", "reason": "续航"}],
                                    "tactical_summary": "前排开团",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
            },
        )

    gw = server_gw.ServerCoachGateway(
        gemini_keys=(),
        deepseek_keys=("ds-k1",),
        deepseek_http_factory=lambda timeout_s: httpx.Client(transport=httpx.MockTransport(ds_handler), timeout=timeout_s),
    )
    try:
        res = gw.advise(
            {
                "dataset_patch": "26.23",
                "hero_static": {"hero_id": "72", "name": "斯卡纳", "win_rate": 0.47},
                "item_glossary": {"3075": "荆棘之甲|2450", "3165": "莫雷洛秘典|2850", "2138": "钢铁合剂|500"},
                "trigger_reason": "death_enemy_build_changed",
                "my_hero": {"name": "斯卡纳", "gold": 2500, "items": ["心之钢", "无终恨意"]},
                "inventory_status": {"status": "building", "complete_count": 2, "has_tier2_boots": False},
                "selected_augments": [{"id": str(i), "name": f"卡{i}", "rarity": "gold"} for i in range(4)],
                "augment_combo_context": {"direction": "坦克", "primary_profile": "TANK", "top_targets": [{"name": "不应保留"}]},
            },
            "death_enemy_build_changed",
            tier="a",
        )
        assert res.advice_json["next_augment_goal"] == ""
        sent_user_msg = ds_sent[0]["messages"][1]["content"]
        assert '"3165":' not in sent_user_msg  # Morello pruned on Tank
        assert '"2138":' not in sent_user_msg  # Elixir pruned while building
        assert '"augment_combo_context":' not in sent_user_msg  # 4/4 augments omitted
        assert '{"augment_pick_advice"' in ds_sent[0]["messages"][0]["content"]  # DeepSeek gets JSON schema footer
    finally:
        gw.close()


def test_cloud_gateway_client_sends_high_snr_payload_across_all_routes(tmp_path: Path) -> None:
    """Verify CloudGatewayClient.advise() sends high-SNR payloads (not legacy ~4.4k bloat)."""
    from hex_helper.cloud.client import CloudGatewayClient
    from hex_helper.cloud.session_store import DpapiSessionStore

    store = _make_rich_test_store(tmp_path)
    sent_bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        sent_bodies.append(body)
        return httpx.Response(
            200,
            json={
                "request_id": body["request_id"],
                "tier": "a",
                "routed_tier": "a",
                "model": "deepseek-flash",
                "thinking_level": "",
                "latency_ms": 50,
                "advice_json": {
                    "augment_pick_advice": "选灵魂虹吸" if body["trigger_reason"] == "augment_picking" else "",
                    "next_augment_goal": "下轮追灵魂虹吸打蒙多" if body["trigger_reason"] != "augment_picking" else "",
                    "buy_now": "先买死刑宣告(800G)",
                    "build_path": ["凡性的提醒", "无尽之刃"],
                    "next_items": [{"item_id": "3033", "name": "凡性的提醒", "priority": "下件必出", "reason": "压高回复"}],
                    "tactical_summary": "站后排拉扯输出",
                },
                "usage": {"input_tokens": 500, "output_tokens": 90, "thought_tokens": 0, "total_tokens": 590},
                "wallet": {"cost_credits": 1.7, "balance_credits": 998.3},
            },
        )

    client = CloudGatewayClient(
        base_url="https://cloud.test",
        session_store=DpapiSessionStore(tmp_path / "sess.bin"),
        http_client_factory=lambda base, timeout: httpx.Client(
            base_url=base, transport=httpx.MockTransport(_handler), timeout=timeout
        ),
    )
    client._session = {"access_token": "tok-1", "refresh_token": "rt-1", "username": "Alan"}

    # 1) death_enemy_build_changed: MUST include next_aug_pool & comp_summary & profile-scoped glossary
    req_death = CoachRequestPayload(
        request_id="r-cloud-death",
        trigger_reason="death_enemy_build_changed",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "51", "name": "凯特琳", "gold": 1100, "items": ["收集者"]},
        selected_augments=[{"id": "1001", "name": "银卡", "rarity": "silver"}],
        allies=[{"champion": "斯卡纳", "kda": "2/3/8", "items": ["心之钢"], "spells": ["闪现", "雪球"]}],
        enemies=[{"champion": "蒙多医生", "kda": "8/2/6", "items": ["心之钢", "狂徒铠甲"], "spells": ["闪现", "虚弱"]}],
        augment_combo_context={
            "direction": "AD暴击",
            "primary_profile": "AD",
            "top_targets": [{"id": "1077", "name": "灵魂虹吸", "rarity": "gold", "tier": "S", "delta": 0.021, "comp_tag": "打敌多坦"}],
        },
    )
    adv_death, _ = client.advise(req_death, store, tier="a")
    assert adv_death.next_augment_goal == "下轮追灵魂虹吸打蒙多"
    sent_death = sent_bodies[-1]["prompt_payload"]
    assert "comp_summary" in sent_death
    assert "next_aug_pool" in (sent_death.get("augment_combo_context") or {})
    assert "3165" not in (sent_death.get("item_glossary") or {})  # No AP Morello leaked for AD Caitlyn
    assert all(isinstance(e, str) for e in sent_death["enemies"])

    # 2) augment_picking: MUST use Route-1 fast payload (no item_glossary, has comp_summary & crit_chance_pct)
    req_pick = CoachRequestPayload(
        request_id="r-cloud-pick",
        trigger_reason="augment_picking",
        timestamp="ts",
        dataset_patch="26.23",
        my_hero={"hero_id": "51", "name": "凯特琳", "gold": 1200, "items": ["收集者", "无尽之刃"], "my_stats": {"critChance": 0.5}},
        selected_augments=[{"id": "1001", "name": "银卡", "rarity": "silver"}],
        offered_augments=[{"id": "1077", "name": "灵魂虹吸", "rarity": "gold", "delta": 0.021}],
        enemies=[{"champion": "蒙多医生", "kda": "6/2/5", "items": ["心之钢"]}],
    )
    client.advise(req_pick, store, tier="a")
    sent_pick = sent_bodies[-1]["prompt_payload"]
    assert "item_glossary" not in sent_pick
    assert "comp_summary" in sent_pick
    assert sent_pick["my_hero"]["crit_chance_pct"] == 50


def test_server_slicer_preserves_high_snr_item_glossary_losslessly_across_all_profiles(tmp_path: Path) -> None:
    """Regression test: slice_prompt_payload_high_snr MUST NOT wipe item_glossary on high-SNR payloads."""
    store = _load_real_store_if_available() or _make_rich_test_store(tmp_path)
    is_real = store.get_hero("40") is not None  # Janna present in real 16.23 dataset

    cases: list[tuple[str, str, str, list[str], list[str]]] = [
        ("51", "凯特琳", "game_start", [], ["3033", "3036"]),
        ("51", "凯特琳", "death_enemy_build_changed", ["育恩塔尔荒野箭"], ["3033", "3036"]),
        ("72", "斯卡纳", "game_start", [], ["3075", "2504"]),
        ("72", "斯卡纳", "death_enemy_build_changed", ["璀璨虚空"], ["3075", "2504"]),
        ("99", "拉克丝", "game_start", [], ["3089", "6655"]),
        ("99", "拉克丝", "death_enemy_build_changed", ["影焰"], ["3089", "6655"]),
    ]
    if is_real:
        cases.append(("40", "迦娜", "game_start", [], ["6617", "3107"]))

    for hid, hname, trigger, my_items, must_keep_if_present in cases:
        req = CoachRequestPayload(
            request_id=f"r-srv-{hid}-{trigger}",
            trigger_reason=trigger,
            timestamp="ts",
            dataset_patch="26.23",
            my_hero={"hero_id": hid, "name": hname, "gold": 1400, "items": my_items},
            allies=[{"champion": "拉克丝", "kda": "2/2/6", "items": ["卢登的伙伴"] if trigger != "game_start" else []}],
            enemies=[{"champion": "蒙多医生", "kda": "5/2/4", "items": ["心之钢"] if trigger != "game_start" else []}],
        )
        diet = build_high_snr_prompt_payload(req, store)
        diet_glossary = dict(diet["item_glossary"])
        assert len(diet_glossary) > 0, f"Client high-SNR glossary unexpectedly empty for {hname}/{trigger}"

        sliced = server_gw.slice_prompt_payload_high_snr(diet, trigger)
        assert sliced["item_glossary"] == diet_glossary, (
            f"Server slicer corrupted high-SNR item_glossary for {hname}/{trigger}: "
            f"{len(diet_glossary)} -> {len(sliced['item_glossary'])} "
            f"(dropped={set(diet_glossary) - set(sliced['item_glossary'])})"
        )
        for iid in must_keep_if_present:
            if iid in diet_glossary:
                assert iid in sliced["item_glossary"], f"{hname}/{trigger} lost {iid}"

        # Double-slicing must be strictly idempotent
        assert server_gw.slice_prompt_payload_high_snr(sliced, trigger) == sliced

        # Legacy payload for the same hero must also preserve that hero's core items & never collapse to 0
        legacy = build_coach_prompt_payload(req, store, high_snr=False)
        sliced_legacy = server_gw.slice_prompt_payload_high_snr(legacy, trigger)
        assert len(sliced_legacy["item_glossary"]) > 0, f"Legacy slice wiped glossary for {hname}/{trigger}"
        for iid in must_keep_if_present:
            if iid in legacy["item_glossary"] and iid in diet_glossary:
                assert iid in sliced_legacy["item_glossary"], f"Legacy slice lost {iid} for {hname}/{trigger}"


def test_server_slicer_preserves_client_recommended_elixir_on_mixed_6_slot_full_builds(tmp_path: Path) -> None:
    """Regression test: slice_prompt_payload_high_snr MUST prioritize raw_inv.recommended_elixir over server heuristic."""
    # 1) Synthetic mixed AP+Tank 6-slot build (client DataStore selected 2139; server item-name heuristic guesses TANK/2138)
    mixed_ap_tank_payload = {
        "dataset_patch": "26.23",
        "hero_static": {"hero_id": "8", "name": "弗拉基米尔", "win_rate": 0.51},
        "hero_big_data_top_builds": ["灭世者的死亡之帽>影焰>振奋盔甲(52.1%)"],
        "item_glossary": {"2139": "巫术合剂|500", "3135": "虚空之杖|3000"},
        "trigger_reason": "death_enemy_build_changed",
        "my_hero": {
            "name": "弗拉基米尔",
            "gold": 900,
            "items": ["裂隙制造者", "灭世者的死亡之帽", "星界驱驰", "心之钢", "影焰", "振奋盔甲"],
        },
        "inventory_status": {
            "status": "full_6_complete",
            "complete_count": 6,
            "has_tier2_boots": False,
            "recommended_elixir": {"item_id": "2139", "name": "巫术合剂", "gold": 500},
        },
        "selected_augments": ["珠光护手(彩)"],
        "comp_summary": "我方:1坦3AP1AD;敌方:2坦2AP1AD(AD40%/AP60%)",
    }
    sliced_mixed = server_gw.slice_prompt_payload_high_snr(mixed_ap_tank_payload, "death_enemy_build_changed")
    assert set(sliced_mixed["item_glossary"]) & ELIXIR_IDS == {"2139"}
    assert server_gw.slice_prompt_payload_high_snr(sliced_mixed, "death_enemy_build_changed") == sliced_mixed

    # 2) Legacy 6-slot payload with all 3 elixirs in glossary + explicit client recommended_elixir=2139 -> keeps ONLY 2139
    legacy_mixed = {
        "dataset_patch": "26.23",
        "hero_static": {"hero_id": "22", "name": "艾希", "win_rate": 0.44},
        "item_glossary": {
            "2138": "钢铁合剂|500",
            "2139": "巫术合剂|500",
            "2140": "愤怒合剂|500",
            "3165": "莫雷洛秘典|2850",
        },
        "trigger_reason": "death_enemy_build_changed",
        "my_hero": {
            "name": "艾希",
            "gold": 750,
            "items": ["残疫", "帝国指令", "视界专注", "公理圆弧", "破败王者之刃", "卢安娜的飓风"],
        },
        "inventory_status": {
            "status": "full_6_complete",
            "complete_count": 6,
            "has_tier2_boots": False,
            "recommended_elixir": {"item_id": "2139", "name": "巫术合剂", "gold": 500},
        },
        "selected_augments": [],
    }
    sliced_legacy_mixed = server_gw.slice_prompt_payload_high_snr(legacy_mixed, "death_enemy_build_changed")
    assert set(sliced_legacy_mixed["item_glossary"]) & ELIXIR_IDS == {"2139"}

    # 3) Fallback when recommended_elixir is absent -> still resolves from _PROFILE_ELIXIR_ID[prof]
    no_elixir_hint = {
        "dataset_patch": "26.23",
        "hero_static": {"hero_id": "72", "name": "斯卡纳", "win_rate": 0.47},
        "item_glossary": {"2138": "钢铁合剂|500", "2139": "巫术合剂|500", "2140": "愤怒合剂|500"},
        "trigger_reason": "death_enemy_build_changed",
        "my_hero": {"name": "斯卡纳", "gold": 900, "items": ["心之钢", "狂徒铠甲", "无终恨意", "败魔", "振奋盔甲", "兰顿之兆"]},
        "inventory_status": {"status": "full_6_complete", "complete_count": 6, "has_tier2_boots": False},
        "selected_augments": [],
    }
    sliced_fallback = server_gw.slice_prompt_payload_high_snr(no_elixir_hint, "death_enemy_build_changed")
    assert set(sliced_fallback["item_glossary"]) & ELIXIR_IDS == {"2138"}

    # 4) Full real 16.23/normal sweep across ALL 173 champions at 6-slot full build (including 887/102/16/57/79/8/54/53/20/22)
    store = _load_real_store_if_available()
    if store is None:
        return

    checked = 0
    for h in store.all_heroes():
        recs = store._item_recs.get(h.hero_id, {})
        six_completes: list[str] = []
        for iid, _st in sorted(recs.items(), key=lambda kv: kv[1].games, reverse=True):
            info = store.get_item_info(iid)
            if info.category == "complete_equipment" and info.name not in six_completes:
                six_completes.append(info.name)
            if len(six_completes) >= 6:
                break
        if len(six_completes) < 6:
            continue
        checked += 1
        req = CoachRequestPayload(
            request_id=f"r-full6-{h.hero_id}",
            trigger_reason="death_enemy_build_changed",
            timestamp="ts",
            dataset_patch="26.23",
            my_hero={"hero_id": h.hero_id, "name": h.name, "gold": 900, "items": six_completes},
            committed_build_plan={"owned_complete": six_completes, "allow_overhaul": False},
        )
        diet = build_high_snr_prompt_payload(req, store)
        diet_elx = set(diet["item_glossary"]) & ELIXIR_IDS
        assert len(diet_elx) == 1, f"{h.hero_id} {h.name} client 6-slot missing elixir"
        sliced = server_gw.slice_prompt_payload_high_snr(diet, "death_enemy_build_changed")
        sliced_elx = set(sliced["item_glossary"]) & ELIXIR_IDS
        assert sliced_elx == diet_elx, (
            f"{h.hero_id} {h.name} server slicer dropped elixir: {diet_elx} -> {sliced_elx}"
        )
    assert checked >= 150
