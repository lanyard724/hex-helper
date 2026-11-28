"""v1.5.2 regression & audit-fix verification suite (Wave A decay model + Wave B P0-1..4 + Wave C P1-5..8)."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from typing import Any

import pytest
from PySide6.QtCore import QAbstractNativeEventFilter
from PySide6.QtWidgets import QApplication

from hex_helper import app as app_mod
from hex_helper.app import GameState, HexHelperController, HexStateMachine
from hex_helper.bridge.protocol import CoachAdviceItem, CoachAdvicePayload, CoachRequestPayload
from hex_helper.cloud.client import CloudAdviseStats, CloudCoachWorker, CloudGatewayClient
from hex_helper.coach.gemini_coach import _canonical_item_lookup, ground_coach_advice
from hex_helper.data.locale_pack import build_ocr_catalog
from hex_helper.data.provider import (
    AugmentInfo,
    CoreBuild,
    DatasetDescriptor,
    HeroStat,
    ItemRecStat,
    ItemStaticInfo,
)
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.devlab.recorder import DevLabFlightRecorder
from hex_helper.game.lcu_client import LcuClient
from hex_helper.game.models import LiveMatchSnapshot, PlayerLiveInfo
from hex_helper.logging_ring import MatchLogRing
from hex_helper.scoring.augment_combo import (
    effective_augment_round,
    evaluate_augment_round_decay,
    evaluate_picker_combos,
)
from hex_helper.scoring.matchup_optimizer import (
    _is_item_off_active_profile,
    build_next_buy_plan,
    compute_late_item_decay,
    infer_unified_hero_profile,
    is_valid_plan_item,
    optimize_next_items,
    reconcile_match_build_plan,
    reconcile_match_build_plan_with_trace,
    resolve_item_id_by_name,
)
from hex_helper.scoring.tier import AugmentScore, rank_picker_cards
from hex_helper.vision.detector import DetectedCard
from hex_helper.vision.matcher import AugmentMatcher


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class _NoopCapturer:
    backend = "noop"
    consecutive_black_frames = 0

    def grab(self, **_kw: Any) -> Any:
        from hex_helper.vision.capture import CaptureResult

        return CaptureResult(frame_bgr=None, backend="noop", is_black=True, mean_luma=0.0)

    def close(self) -> None:
        return None


class _NoopHotkeys(QAbstractNativeEventFilter):
    last_failed_hotkeys: dict[str, int] = {}

    def nativeEventFilter(self, _eventType: Any, _message: Any) -> tuple[bool, int]:
        return False, 0

    def register(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def unregister_all(self) -> None:
        return None


def _patch_headless_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "ScreenCapturer", lambda **_kw: _NoopCapturer())
    monkeypatch.setattr(app_mod, "GlobalHotkeyManager", _NoopHotkeys)
    monkeypatch.setattr(app_mod, "WinRtOcrEngine", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_mod.HexHelperController, "start_async_sync", lambda self: None)
    monkeypatch.setattr(app_mod.HexHelperController, "_telemetry_worker_loop", lambda self: None)


def _cleanup_controller(ctrl: HexHelperController, qapp: QApplication) -> None:
    for attr in ("edge_drawer", "combo_overlay", "champ_overlay", "augment_overlay", "lookup_window"):
        w = getattr(ctrl, attr, None)
        if w is not None:
            try:
                w.close()
            except Exception:
                pass
    ctrl.shutdown()
    qapp.processEvents()


def _make_v152_store(tmp_path: Path) -> DataStore:
    static = StaticAssetsManager(tmp_path / "static_cache", fetcher=lambda _u: b"")
    static.items = {
        # Basic / sub-components
        "1004": ItemStaticInfo("1004", "仙女护符", 200, ("ManaRegen",), {}, "", "other"),
        "1026": ItemStaticInfo("1026", "爆裂魔杖", 850, ("SpellDamage",), {"FlatMagicDamageMod": 45.0}, "", "other"),
        "1036": ItemStaticInfo("1036", "长剑", 350, ("Damage",), {"FlatPhysicalDamageMod": 10.0}, "", "other"),
        "1038": ItemStaticInfo("1038", "暴风之剑", 1300, ("Damage",), {"FlatPhysicalDamageMod": 40.0}, "", "other"),
        "1058": ItemStaticInfo("1058", "无用大棒", 1200, ("SpellDamage",), {"FlatMagicDamageMod": 65.0}, "", "other"),
        "1028": ItemStaticInfo("1028", "红水晶", 400, ("Health",), {"FlatHPPoolMod": 150.0}, "", "other"),
        "3067": ItemStaticInfo("3067", "燃烧宝石", 800, ("Health", "CooldownReduction"), {"FlatHPPoolMod": 200.0}, "", "other"),
        "3070": ItemStaticInfo("3070", "女神之泪", 400, ("Mana",), {"FlatMPPoolMod": 240.0}, "", "other"),
        "3077": ItemStaticInfo("3077", "提亚马特", 1200, ("Damage",), {"FlatPhysicalDamageMod": 20.0}, "", "other"),
        "3803": ItemStaticInfo("3803", "万世催化石", 1300, ("Health", "Mana"), {"FlatHPPoolMod": 300.0, "FlatMPPoolMod": 300.0}, "", "other", components=("1028",)),
        "1011": ItemStaticInfo("1011", "巨人腰带", 900, ("Health",), {"FlatHPPoolMod": 350.0}, "", "other"),
        "3123": ItemStaticInfo("3123", "死刑宣告", 800, ("Damage",), {"FlatPhysicalDamageMod": 15.0}, "", "other", has_grievous=True),
        # Boots
        "3020": ItemStaticInfo("3020", "法师之靴", 1100, ("Boots",), {}, "", "boots"),
        "3111": ItemStaticInfo("3111", "水银之靴", 1300, ("Boots", "SpellBlock"), {"FlatSpellBlockMod": 20.0}, "", "boots"),
        "3172": ItemStaticInfo("3172", "灵风", 3100, ("Boots", "AttackSpeed"), {"PercentAttackSpeedMod": 0.45}, "", "boots"),
        "663172": ItemStaticInfo("663172", "灵风", 3100, ("Boots", "AttackSpeed"), {"PercentAttackSpeedMod": 0.45}, "", "complete_equipment"),
        # Scaling / stacker / snowball items
        "6657": ItemStaticInfo("6657", "时光之杖", 2600, ("Health", "SpellDamage", "Mana"), {"FlatMagicDamageMod": 50.0, "FlatHPPoolMod": 350.0, "FlatMPPoolMod": 500.0}, "", "complete_equipment", components=("3803", "1026")),
        "3084": ItemStaticInfo("3084", "心之钢", 3000, ("Health",), {"FlatHPPoolMod": 900.0}, "", "complete_equipment", components=("1011", "3803")),
        "3003": ItemStaticInfo("3003", "大天使之杖", 2900, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 70.0, "FlatMPPoolMod": 600.0}, "", "complete_equipment", components=("3070", "1058")),
        "3040": ItemStaticInfo("3040", "炽天使之拥", 2900, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 70.0, "FlatMPPoolMod": 1000.0}, "", "complete_equipment"),
        "3004": ItemStaticInfo("3004", "魔宗", 2900, ("Damage", "Mana"), {"FlatPhysicalDamageMod": 35.0, "FlatMPPoolMod": 500.0}, "", "complete_equipment", components=("3070",)),
        "3119": ItemStaticInfo("3119", "凛冬之临", 2400, ("Health", "Mana"), {"FlatHPPoolMod": 550.0, "FlatMPPoolMod": 500.0}, "", "complete_equipment", components=("3070", "1011")),
        "3121": ItemStaticInfo("3121", "末日寒冬", 2400, ("Health", "Mana"), {"FlatHPPoolMod": 550.0, "FlatMPPoolMod": 860.0}, "", "complete_equipment"),
        "2526": ItemStaticInfo("2526", "耳语头环", 2250, ("SpellDamage", "ManaRegen"), {"FlatMagicDamageMod": 30.0, "FlatMPPoolMod": 300.0}, "", "complete_equipment", components=("3070",)),
        "6697": ItemStaticInfo("6697", "狂妄", 3000, ("Damage",), {"FlatPhysicalDamageMod": 60.0}, "", "complete_equipment", components=("1036", "1036"), aram_available=False),
        "126697": ItemStaticInfo("126697", "狂妄", 3000, ("Damage",), {"FlatPhysicalDamageMod": 60.0}, "", "complete_equipment", components=("1036", "1036"), aram_available=True),
        "3032": ItemStaticInfo("3032", "育恩塔尔荒野箭", 3000, ("Damage", "AttackSpeed"), {"FlatPhysicalDamageMod": 55.0, "PercentAttackSpeedMod": 0.35}, "", "complete_equipment", components=("1038",)),
        "6676": ItemStaticInfo("6676", "收集者", 3000, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 50.0, "FlatCritChanceMod": 0.25}, "", "complete_equipment", components=("1036",)),
        # Cross-mode Arena / non-ARAM traps
        "2501": ItemStaticInfo("2501", "霸王血铠", 3300, ("Health", "Damage"), {"FlatPhysicalDamageMod": 30.0, "FlatHPPoolMod": 550.0}, "", "complete_equipment", components=("1011",), aram_available=True),
        "447111": ItemStaticInfo("447111", "霸王血铠", 3300, ("Health", "Damage"), {"FlatPhysicalDamageMod": 30.0, "FlatHPPoolMod": 550.0}, "", "complete_equipment", aram_available=False),
        "7111": ItemStaticInfo("7111", "恶魔之拥", 3000, ("SpellDamage",), {"FlatMagicDamageMod": 70.0}, "", "complete_equipment", aram_available=False),
        "4402": ItemStaticInfo("4402", "激发之匣", 3000, ("Health",), {"FlatHPPoolMod": 400.0}, "", "complete_equipment", aram_available=False),
        "773084": ItemStaticInfo("773084", "激发之匣", 2500, ("Health",), {"FlatHPPoolMod": 400.0}, "", "complete_equipment", aram_available=True),
        # Standard AP / AD / Tank / Aura items
        "4645": ItemStaticInfo("4645", "影焰", 3200, ("SpellDamage",), {"FlatMagicDamageMod": 115.0}, "", "complete_equipment"),
        "4646": ItemStaticInfo("4646", "风暴狂涌", 2800, ("SpellDamage",), {"FlatMagicDamageMod": 90.0}, "", "complete_equipment"),
        "3089": ItemStaticInfo("3089", "灭世者的死亡之帽", 3600, ("SpellDamage",), {"FlatMagicDamageMod": 130.0}, "", "complete_equipment", components=("1058", "1058")),
        "3157": ItemStaticInfo("3157", "中娅沙漏", 3250, ("SpellDamage", "Armor"), {"FlatMagicDamageMod": 105.0}, "", "complete_equipment"),
        "3135": ItemStaticInfo("3135", "虚空之杖", 3000, ("SpellDamage",), {"FlatMagicDamageMod": 95.0}, "", "complete_equipment"),
        "6653": ItemStaticInfo("6653", "兰德里的折磨", 3000, ("Health", "SpellDamage"), {"FlatMagicDamageMod": 60.0, "FlatHPPoolMod": 300.0}, "", "complete_equipment"),
        "3118": ItemStaticInfo("3118", "残疫", 2700, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 85.0, "FlatMPPoolMod": 600.0}, "", "complete_equipment"),
        "6655": ItemStaticInfo("6655", "卢登的伙伴", 2850, ("SpellDamage", "Mana"), {"FlatMagicDamageMod": 100.0, "FlatMPPoolMod": 600.0}, "", "complete_equipment"),
        "3068": ItemStaticInfo("3068", "日炎圣盾", 2700, ("Armor", "Aura", "Health"), {"FlatArmorMod": 50.0, "FlatHPPoolMod": 350.0}, "", "complete_equipment"),
        "6664": ItemStaticInfo("6664", "璀璨回响", 2900, ("Health", "SpellBlock", "Aura"), {"FlatSpellBlockMod": 40.0, "FlatHPPoolMod": 450.0}, "", "complete_equipment"),
        "6667": ItemStaticInfo("6667", "辉耀美德", 2700, ("Health", "Aura"), {"FlatHPPoolMod": 400.0}, "", "complete_equipment"),
        "3110": ItemStaticInfo("3110", "冰霜之心", 2500, ("Armor", "Aura", "Mana"), {"FlatArmorMod": 75.0, "FlatMPPoolMod": 400.0}, "", "complete_equipment"),
        "3190": ItemStaticInfo("3190", "钢铁烈阳之匣", 2200, ("Armor", "Aura", "Health"), {"FlatArmorMod": 25.0, "FlatHPPoolMod": 200.0}, "", "complete_equipment"),
        "3065": ItemStaticInfo("3065", "振奋盔甲", 2700, ("Health", "SpellBlock"), {"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 50.0}, "", "complete_equipment"),
        "2504": ItemStaticInfo("2504", "败魔", 2900, ("Health", "SpellBlock"), {"FlatHPPoolMod": 400.0, "FlatSpellBlockMod": 80.0}, "", "complete_equipment"),
        "3074": ItemStaticInfo("3074", "贪欲九头蛇", 3300, ("Damage", "LifeSteal"), {"FlatPhysicalDamageMod": 65.0}, "", "complete_equipment"),
        "3748": ItemStaticInfo("3748", "巨型九头蛇", 3300, ("Damage", "Health"), {"FlatPhysicalDamageMod": 40.0, "FlatHPPoolMod": 600.0}, "", "complete_equipment"),
        "6698": ItemStaticInfo("6698", "渎神九头蛇", 2850, ("Damage",), {"FlatPhysicalDamageMod": 55.0}, "", "complete_equipment"),
        "3031": ItemStaticInfo("3031", "无尽之刃", 3400, ("Damage", "CriticalStrike"), {"FlatPhysicalDamageMod": 70.0, "FlatCritChanceMod": 0.25}, "", "complete_equipment"),
        "3078": ItemStaticInfo("3078", "三相之力", 3333, ("Damage", "AttackSpeed"), {"FlatPhysicalDamageMod": 36.0}, "", "complete_equipment"),
        "3075": ItemStaticInfo("3075", "荆棘之甲", 2450, ("Health", "Armor"), {"FlatHPPoolMod": 150.0, "FlatArmorMod": 75.0}, "", "complete_equipment"),
    }
    store = DataStore(static)
    store._descriptor = DatasetDescriptor("16.23", "normal", "常规", "patches/16.23/normal", "", False)
    store._heroes = {
        "13": HeroStat("13", "瑞兹", "Ryze", 42000, 21420, 0.51, 0.05, 18, False),
        "31": HeroStat("31", "科加斯", "Chogath", 48000, 25440, 0.53, 0.06, 10, False),
        "36": HeroStat("36", "蒙多医生", "DrMundo", 52000, 27560, 0.53, 0.07, 8, False),
        "11": HeroStat("11", "易", "MasterYi", 45000, 22950, 0.51, 0.06, 20, False),
        "157": HeroStat("157", "亚索", "Yasuo", 65000, 32500, 0.50, 0.09, 25, False),
        "222": HeroStat("222", "金克丝", "Jinx", 70000, 36400, 0.52, 0.10, 12, False),
        "23": HeroStat("23", "泰达米尔", "Tryndamere", 30000, 15000, 0.50, 0.04, 30, False),
    }
    # Give Cho'Gath (31) a dominant TANK core-build prior + secondary AP template to test P1-6 prior attenuation
    store._core_builds = {
        "31": [
            CoreBuild(("3084", "3068", "3065"), True, 18000, 9720, 0.54, 0.45),
            CoreBuild(("4645", "4646", "3089"), True, 6000, 3240, 0.54, 0.18),
        ],
        "36": [CoreBuild(("3084", "3065", "2504"), True, 20000, 10800, 0.54, 0.55)],
        "13": [CoreBuild(("6657", "3003", "3089"), True, 16000, 8480, 0.53, 0.48)],
        "11": [CoreBuild(("3074", "3031", "6676"), True, 12000, 6240, 0.52, 0.36)],
    }
    store._item_recs = {
        "31": {
            "3084": ItemRecStat("3084", 22000, 11880, 0.54, False),
            "3068": ItemRecStat("3068", 15000, 8100, 0.54, False),
            "4645": ItemRecStat("4645", 8000, 4480, 0.56, False),
            "4646": ItemRecStat("4646", 4500, 2430, 0.54, False),
            "3089": ItemRecStat("3089", 6000, 3360, 0.56, False),
            "3157": ItemRecStat("3157", 5000, 2700, 0.54, False),
            "3190": ItemRecStat("3190", 900, 486, 0.54, False),
        },
        "13": {
            "6657": ItemRecStat("6657", 15000, 7950, 0.53, False),
            "3003": ItemRecStat("3003", 14000, 7420, 0.53, False),
            "3119": ItemRecStat("3119", 6000, 3120, 0.52, False),
            "3089": ItemRecStat("3089", 10000, 5500, 0.55, False),
        },
    }
    store._augments = {
        "1001": AugmentInfo("1001", "巨像的勇气", "prismatic", "护盾与生命值成长"),
        "1004": AugmentInfo("1004", "回归基本功", "gold", "基础技能增强"),
        "1006": AugmentInfo("1006", "嗜血饮者", "gold", "普攻吸血与续航"),
        "1013": AugmentInfo("1013", "星界躯体", "prismatic", "增加1250生命值"),
        "1036": AugmentInfo("1036", "魔法飞弹", "gold", "技能命中发射飞弹"),
        "1041": AugmentInfo("1041", "歌利亚巨人", "prismatic", "获得35%体型与生命值"),
        "1045": AugmentInfo("1045", "炼狱导管", "prismatic", "技能附带灼烧"),
        "1054": AugmentInfo("1054", "物法皆修", "gold", "双修转化"),
        "1056": AugmentInfo("1056", "由心及物", "silver", "蓝量转生命"),
        "1081": AugmentInfo("1081", "量子计算", "gold", "周期环绕斩击"),
        "1152": AugmentInfo("1152", "钢化你心", "prismatic", "心之钢叠层任务"),
        "1181": AugmentInfo("1181", "坚如磐石", "silver", "获得额外生命"),
        "1204": AugmentInfo("1204", "叠角龙", "gold", "永久叠加收益加速"),
        "1238": AugmentInfo("1238", "质变：棱彩阶", "gold", "随机获得棱彩符文"),
        "1311": AugmentInfo("1311", "溢流", "silver", "法力消耗提高伤害"),
        "1322": AugmentInfo("1322", "罪恶快感", "gold", "提供额外自适应属性"),
        "1323": AugmentInfo("1323", "闪光弹", "silver", "闪现致盲"),
        "1328": AugmentInfo("1328", "暴击律动", "gold", "技能暴击机制"),
        "1353": AugmentInfo("1353", "坦克引擎", "silver", "参与击杀叠加最大生命"),
        "1373": AugmentInfo("1373", "缩小引擎", "silver", "参与击杀叠加双抗体型"),
        "1402": AugmentInfo("1402", "属性锻造器", "silver", "属性碎片"),
        "1996": AugmentInfo("1996", "升级：耀光", "gold", "强化耀光系成装"),
        "2141": AugmentInfo("2141", "终极九头蛇", "prismatic", "三把九头蛇合体任务"),
        "3004": AugmentInfo("3004", "罪恶快感！", "silver", "暴击/移速联动"),
        "3059": AugmentInfo("3059", "没法力，没问题", "prismatic", "蓝量转法强"),
        "3060": AugmentInfo("3060", "急急小子", "silver", "获得急速"),
        "3094": AugmentInfo("3094", "重量级打击手", "gold", "生命转伤害"),
        "3150": AugmentInfo("3150", "升级：风暴狂涌", "silver", "强化风暴狂涌"),
        "12317": AugmentInfo("12317", "双刀流", "prismatic", "普攻双触发"),
    }
    store._item_global_bias = {"6697": 0.02, "3084": 0.01}
    return store


# ---------------------------------------------------------------------------
# Wave A: Full Late-Item Decay Matrix + V1-1 / V1-2 + Dual Reconcile Parity
# ---------------------------------------------------------------------------


def test_wave_a_late_item_decay_matrix_and_dual_reconcile_parity(tmp_path: Path) -> None:
    store = _make_v152_store(tmp_path)
    fed_me = PlayerLiveInfo("me", "泰隆", "Talon", "ORDER", False, 11, 7, 2, 4, ("1036",))
    behind_me = PlayerLiveInfo("me", "泰隆", "Talon", "ORDER", False, 11, 1, 5, 2, ("1036",))

    # 1. RoA (6657): V1-2 protects 305s (0 complete + 3070 + 1026), decays at 510s/2 comp, 1204 never exempts 6657
    pen_v12, _, _, _ = compute_late_item_decay(
        store, "6657", ("3070", "1026"), game_time=305.0, selected_augments=(), owned_complete_count=0
    )
    assert pen_v12 == 0.0

    pen_roa_late, sup_core_roa, sup_plan_roa, _ = compute_late_item_decay(
        store, "6657", ("3003", "3089"), game_time=510.0, selected_augments=("1204",), owned_complete_count=2
    )
    assert pen_roa_late == pytest.approx(-8.5)
    assert sup_core_roa is True and sup_plan_roa is True

    # 2. Heartsteel (3084): T1 (-3.2), T2 (-6.5), 1152/1204 exempt within window
    p_hs_t1, _, _, _ = compute_late_item_decay(store, "3084", ("3068",), game_time=300.0, owned_complete_count=1)
    assert p_hs_t1 == pytest.approx(-3.2)
    p_hs_t2, _, _, _ = compute_late_item_decay(store, "3084", ("3068", "3065"), game_time=510.0, owned_complete_count=2)
    assert p_hs_t2 == pytest.approx(-6.5)
    p_hs_1152, _, _, _ = compute_late_item_decay(
        store, "3084", ("3068", "3065"), game_time=600.0, selected_augments=("1152",), owned_complete_count=2
    )
    p_hs_1204, _, _, _ = compute_late_item_decay(
        store, "3084", ("3068", "3065"), game_time=540.0, selected_augments=("1204",), owned_complete_count=2
    )
    assert p_hs_1152 == 0.0 and p_hs_1204 == 0.0

    # 3. Tear 4-pack (3003/3004/3119/2526) + V1-1 stacked Tear protection
    for tid in ("3003", "3004", "3119", "2526"):
        assert compute_late_item_decay(store, tid, ("3070",), game_time=520.0, owned_complete_count=2)[0] == 0.0
    assert compute_late_item_decay(store, "2526", (), game_time=520.0, owned_complete_count=2)[0] == pytest.approx(-6.5)

    # V1-1: Owning stacked Seraph's (3040 / 323040) does NOT falsely lock Fimbulwinter (3119)
    assert compute_late_item_decay(store, "3119", ("3040", "3070"), game_time=500.0, owned_complete_count=1)[0] == 0.0
    assert compute_late_item_decay(store, "3119", ("323040", "3070"), game_time=500.0, owned_complete_count=1)[0] == 0.0
    # Unstacked Archangel's (3003 / 323003) DOES trigger mutual-exclusion -15.0 lock on 3119
    assert compute_late_item_decay(store, "3119", ("3003",), game_time=500.0, owned_complete_count=1)[0] == pytest.approx(-15.0)
    assert compute_late_item_decay(store, "3119", ("323003",), game_time=500.0, owned_complete_count=1)[0] == pytest.approx(-15.0)

    # 4. Hubris (6697), Yun Tal (3032), Collector (6676)
    assert compute_late_item_decay(store, "6697", ("3153",), game_time=400.0, owned_complete_count=1, my_player=fed_me)[0] == 0.0
    assert compute_late_item_decay(store, "6697", ("3153",), game_time=400.0, owned_complete_count=1, my_player=behind_me)[0] == pytest.approx(-1.8)
    assert compute_late_item_decay(store, "3032", ("3153",), game_time=300.0, owned_complete_count=1)[0] == 0.0
    assert compute_late_item_decay(store, "3032", ("3153", "3031", "6676"), game_time=750.0, owned_complete_count=3)[0] == pytest.approx(-2.8)
    assert compute_late_item_decay(store, "6676", ("3153", "3031", "3072"), game_time=720.0, owned_complete_count=3)[0] == pytest.approx(-2.4)
    assert compute_late_item_decay(store, "6676", ("3153", "3031", "3072"), game_time=720.0, selected_augments=("1320",), owned_complete_count=3)[0] == 0.0

    # 5. Dual reconcile parity: Mundo 10:00 zombie 3084 purged in BOTH commercial & DevLab without 1152, kept with 1152
    c_lock, c_plan = reconcile_match_build_plan(
        store,
        ("3065", "2504"),
        ["3084", "3075"],
        "3084",
        ["3075", "3068"],
        allow_overhaul=False,
        hero_id="36",
        selected_augments=("1041",),
        active_profile="TANK",
        game_time=600.0,
    )
    d_lock, d_plan, d_trace = reconcile_match_build_plan_with_trace(
        store,
        ("3065", "2504"),
        ["3084", "3075"],
        "3084",
        ["3075", "3068"],
        allow_overhaul=False,
        hero_id="36",
        selected_augments=("1041",),
        active_profile="TANK",
        game_time=600.0,
    )
    assert c_lock == d_lock == "3075"
    assert c_plan == d_plan
    assert "3084" not in d_plan
    assert d_trace["override_reason"] == "late_stacker_expired"

    k_lock, k_plan = reconcile_match_build_plan(
        store,
        ("3065", "2504"),
        ["3084", "3075"],
        "3084",
        ["3075", "3068"],
        allow_overhaul=False,
        hero_id="36",
        selected_augments=("1152",),
        active_profile="TANK",
        game_time=600.0,
    )
    dk_lock, dk_plan, dk_trace = reconcile_match_build_plan_with_trace(
        store,
        ("3065", "2504"),
        ["3084", "3075"],
        "3084",
        ["3075", "3068"],
        allow_overhaul=False,
        hero_id="36",
        selected_augments=("1152",),
        active_profile="TANK",
        game_time=600.0,
    )
    assert k_lock == dk_lock == "3084"
    assert k_plan == dk_plan and dk_trace["override_reason"] == ""


# ---------------------------------------------------------------------------
# Wave A: Round 3-4 Augment Decay + V1-3..6 + rank_picker_cards
# ---------------------------------------------------------------------------


def test_wave_a_round34_augment_decay_and_tier_ranking(tmp_path: Path) -> None:
    store = _make_v152_store(tmp_path)

    # 1. effective_augment_round + is_r4 checked strictly before is_r3 (t=695s, selected=2 -> R4)
    assert effective_augment_round(2, my_level=15) == 4
    res_t695 = evaluate_augment_round_decay(
        store, "11", "1373", ("3074",), ("1006", "1004"), round_index=3, game_time_s=695.0
    )
    assert res_t695 is not None and res_t695[0] == pytest.approx(-7.5)

    # 2. 2141 终极九头蛇: R1->None, R3->-14.5, R4 4-non-hydra->-28.0 overriding 1006 jackpot, 2 hydras->None
    assert evaluate_augment_round_decay(store, "11", "2141", (), (), round_index=1, game_time_s=120.0) is None
    r3_2141 = evaluate_augment_round_decay(
        store, "11", "2141", ("3031", "6676"), ("1006", "1004"), round_index=3, game_time_s=500.0
    )
    assert r3_2141 is not None and r3_2141[0] == pytest.approx(-14.5)
    assert evaluate_augment_round_decay(
        store, "11", "2141", ("3074", "3748"), ("1006", "1004", "1081"), round_index=4, game_time_s=760.0
    ) is None

    v_yi_3comp = evaluate_picker_combos(
        store,
        "11",
        selected_augments=("1006", "1004", "1081"),
        my_items=("3031", "6676", "3078", "3111"),
        offered=[(0, store.get_augment("2141")), (1, store.get_augment("1081")), (2, store.get_augment("1328"))],
        game_time_s=760.0,
        my_level=16,
    )
    c_2141_3comp = next(c for c in v_yi_3comp.cards if c.augment_id == "2141")
    assert c_2141_3comp.verdict == "round_decay"
    assert c_2141_3comp.combo_delta == pytest.approx(-24.0)

    v_yi = evaluate_picker_combos(
        store,
        "11",
        selected_augments=("1006", "1004", "1081"),
        my_items=("3031", "6676", "3078", "3075"),
        offered=[(0, store.get_augment("2141")), (1, store.get_augment("1081")), (2, store.get_augment("1328"))],
        game_time_s=760.0,
        my_level=16,
    )
    c_2141 = next(c for c in v_yi.cards if c.augment_id == "2141")
    assert c_2141.verdict == "round_decay"
    assert c_2141.combo_delta == pytest.approx(-28.0)

    # 3. 1152 钢化你心: R4 with 3084 restores jackpot/synergy, without 3084 -> -24.0 round_decay
    v_mundo_with = evaluate_picker_combos(
        store,
        "36",
        selected_augments=("1041", "1001", "1181"),
        my_items=("3084", "3065", "2504"),
        offered=[(0, store.get_augment("1152")), (1, store.get_augment("1081"))],
        game_time_s=750.0,
        my_level=16,
    )
    assert next(c for c in v_mundo_with.cards if c.augment_id == "1152").verdict in ("jackpot", "synergy")

    v_mundo_without = evaluate_picker_combos(
        store,
        "36",
        selected_augments=("1041", "1001", "1181"),
        my_items=("3065", "2504", "3075"),
        offered=[(0, store.get_augment("1152")), (1, store.get_augment("1081"))],
        game_time_s=750.0,
        my_level=16,
    )
    c_1152_no = next(c for c in v_mundo_without.cards if c.augment_id == "1152")
    assert c_1152_no.verdict == "round_decay"
    assert c_1152_no.combo_delta == pytest.approx(-24.0)

    # 4. V1-6 (1373) & V1-3 (1013 exact exemption vs 3094 non-exempt)
    assert evaluate_augment_round_decay(store, "36", "1373", (), ("1004", "1181"), round_index=3, game_time_s=500.0)[0] == pytest.approx(-3.0)
    assert evaluate_augment_round_decay(store, "36", "1373", (), ("1004", "1181", "1081"), round_index=4, game_time_s=720.0)[0] == pytest.approx(-7.5)
    assert evaluate_augment_round_decay(store, "36", "1013", (), ("1041",), round_index=4, game_time_s=720.0, primary_profile="TANK") is None
    assert evaluate_augment_round_decay(store, "36", "1013", (), ("3094",), round_index=4, game_time_s=720.0, primary_profile="TANK")[0] == pytest.approx(-3.5)

    # 5. V1-4: 1996 / 3150 upgrade augments (target owned=None, occupied_cb==4=None, occupied_cb==5=-5.5, full=-20.0)
    assert evaluate_augment_round_decay(store, "11", "1996", ("3078", "3031", "6676", "3074", "3111"), (), round_index=4, game_time_s=750.0) is None
    assert evaluate_augment_round_decay(store, "11", "1996", ("3031", "6676", "3074", "3111"), (), round_index=4, game_time_s=750.0) is None
    r4_1996_5 = evaluate_augment_round_decay(store, "31", "3150", ("4645", "3089", "3157", "3135", "3020"), (), round_index=4, game_time_s=750.0)
    assert r4_1996_5 is not None and r4_1996_5[0] == pytest.approx(-5.5)
    r4_3150_full = evaluate_augment_round_decay(store, "31", "3150", ("4645", "3089", "3157", "3135", "6653", "3020"), (), round_index=4, game_time_s=850.0)
    assert r4_3150_full is not None and r4_3150_full[0] == pytest.approx(-20.0)

    # 6. V1-5 manaless (-18.0) vs mana-bar AP (-4.5 at 3 comp R4) + Whitelist (0.0)
    assert evaluate_augment_round_decay(store, "23", "3059", ("3031", "6676", "3032"), (), round_index=4, game_time_s=720.0, primary_profile="AD")[0] == pytest.approx(-18.0)
    assert evaluate_augment_round_decay(store, "36", "1311", ("3084", "3065", "2504"), (), round_index=4, game_time_s=720.0, primary_profile="TANK")[0] == pytest.approx(-18.0)
    assert evaluate_augment_round_decay(store, "31", "1056", ("4645", "3089", "3157"), (), round_index=4, game_time_s=720.0, primary_profile="AP")[0] == pytest.approx(-4.5)
    for wid in ("12317", "1402", "1054", "1081", "1328", "1036"):
        assert evaluate_augment_round_decay(store, "13", wid, ("6657", "3003", "3089", "3157"), ("1004", "1036", "1081"), round_index=4, game_time_s=800.0) is None

    # 7. tier.py: round_decay <= -12.0 -> conflict, -12.0 < d <= -1.5 -> decay, no double-discount
    sc_severe = AugmentScore("1152", "钢化你心", "prismatic", "S", 0.56, 0.56, 0.03, 5000, False)
    sc_mod = AugmentScore("1373", "缩小引擎", "silver", "A", 0.54, 0.54, 0.01, 5000, False)
    c_map_tier = {
        0: v_mundo_without.cards[0],  # 1152 -> -24.0
        1: type("_VC", (), {"verdict": "round_decay", "verdict_label": "末轮叠层折价", "combo_delta": -7.5, "reason": "r4"})(),
    }
    decs, _ = rank_picker_cards([(0, sc_severe), (1, sc_mod)], combo_by_slot=c_map_tier, game_time_s=750.0, round_index=4)
    assert decs[0].shift_kind == "conflict" and decs[0].combo_shift == pytest.approx(-24.0)
    assert decs[1].shift_kind == "decay" and decs[1].combo_shift == pytest.approx(-7.5)


# ---------------------------------------------------------------------------
# Wave B: P0-1..4
# ---------------------------------------------------------------------------


def test_p0_1_item_resolver_blocks_arena_collisions(tmp_path: Path) -> None:
    store = _make_v152_store(tmp_path)
    assert resolve_item_id_by_name(store, "霸王血铠", require_valid_plan=True) == "2501"
    assert resolve_item_id_by_name(store, "狂妄", require_valid_plan=True) == "6697"
    assert resolve_item_id_by_name(store, "心之钢", require_valid_plan=True) == "3084"
    # 773084 strips to 3084 心之钢, so 激发之匣 MUST NOT resolve to 3084!
    assert resolve_item_id_by_name(store, "激发之匣", require_valid_plan=True) == ""
    assert resolve_item_id_by_name(store, "激发之匣", require_valid_plan=False) != "3084"

    for bad in ("447111", "7111", "4402", "773084", "3172"):
        assert is_valid_plan_item(store, bad) is False
    for good in ("2501", "6697", "126697", "663172"):
        assert is_valid_plan_item(store, good) is True

    _, by_name = _canonical_item_lookup(store)
    assert by_name.get("霸王血铠") == "2501"
    assert by_name.get("激发之匣") != "3084"

    req = CoachRequestPayload(
        request_id="req-p01",
        trigger_reason="death",
        timestamp="2026-11-28T20:00:00Z",
        dataset_patch="16.23",
        my_hero={"hero_id": "36", "hero_name": "蒙多医生", "profile": "TANK"},
    )
    adv = ground_coach_advice(
        {
            "next_items": [{"item_id": "447111", "name": "霸王血铠", "priority": "推荐", "reason": "血量转攻击"}],
            "build_path": ["霸王血铠", "狂妄"],
        },
        req,
        store,
    )
    assert adv.next_items[0].item_id == "2501"


def test_p0_2_lcu_stale_game_id_and_log_ring_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lcu = LcuClient()
    eog_payload = {"gameId": 99887766, "gameLength": 900, "queueId": 2400, "localPlayer": {"championId": 31, "stats": {"WIN": 1}}}
    cs_payload = {"myTeam": [{"cellId": 0, "championId": 53}], "localPlayerCellId": 0}
    session_payload = {"gameData": {"gameId": 99887766, "queue": {"id": 2400, "gameMode": "KIWI"}}}

    def _fake_get(path: str) -> Any:
        if path == "/lol-end-of-game/v1/eog-stats-block":
            return eog_payload
        if path == "/lol-gameflow/v1/gameflow-phase":
            return "ChampSelect"
        if path == "/lol-gameflow/v1/session":
            return session_payload
        if path == "/lol-champ-select/v1/session":
            return cs_payload
        return None

    monkeypatch.setattr(lcu, "_get_json", _fake_get)
    snap_eog = lcu.get_eog_snapshot()
    assert snap_eog["game_id"] == 99887766
    assert lcu._last_eog_game_id == 99887766
    phase, cs = lcu.poll()
    assert phase == "ChampSelect" and cs is not None
    assert cs.game_id == 0

    ring = MatchLogRing(tmp_path / "ring")
    e1 = ring.begin_match("gid:101", hero_id="31", hero_name="科加斯")
    ring.end_match(won=1, duration_s=900)
    # Ended session with same key or mismatched hero MUST start a new bucket
    e2 = ring.begin_match("gid:101", hero_id="31", hero_name="科加斯")
    assert e2.seq != e1.seq
    e3 = ring.begin_match("gid:202", hero_id="31", hero_name="科加斯")
    e4 = ring.begin_match("gid:202", hero_id="53", hero_name="布里茨")
    assert e4.seq != e3.seq
    ring.update_match_meta(match_key="gid:202-upd", dataset_mode="first-prismatic")
    assert ring.list_matches()[-1].match_key == "gid:202-upd"


def test_p0_3_p0_4_p1_7_app_controller_and_cloud_cancel(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEX_HELPER_DATA_DIR", str(tmp_path / "appdata"))
    _patch_headless_controller(monkeypatch)
    ctrl = HexHelperController(qapp, show_lookup_on_start=False)
    try:
        ctrl._vision_timer.stop()
        ctrl._heartbeat_timer.stop()
        store = _make_v152_store(tmp_path)
        ctrl.store = store
        ctrl.sm._store = store
        ctrl.cfg.auto_first_prismatic_mode = True
        ctrl.cfg.smart_picker_ai_gate = True
        ctrl.cfg.coach_mode = "antigravity"
        ctrl._devlab_active = True

        emitted: list[tuple[str, str, dict[str, Any]]] = []
        ctrl._devlab = type(
            "_StubDL",
            (),
            {
                "enable_roi_snapshots": False,
                "emit": lambda self, d, et, p=None, **_kw: emitted.append((d, et, dict(p or {}))) or 1,
                "update_match_meta": lambda self, **_kw: None,
                "end_match": lambda self, **_kw: None,
                "flush_and_close": lambda self, **_kw: None,
            },
        )()

        # P0-3a: Mid-game join Lv7 & 269s blocks _maybe_auto_switch_first_prismatic
        me_lv7 = PlayerLiveInfo("me", "泰达米尔", "Tryndamere", "ORDER", False, 7, 2, 1, 2, ("3078",))
        ctrl._last_live = LiveMatchSnapshot(269.0, me_lv7, (me_lv7,), ())
        prismatic_cards = (
            DetectedCard(0, store.get_augment("1001"), 0.96, (300, 200, 500, 300), "prismatic"),
            DetectedCard(1, store.get_augment("1013"), 0.95, (700, 200, 900, 300), "prismatic"),
            DetectedCard(2, store.get_augment("1152"), 0.94, (1100, 200, 1300, 300), "prismatic"),
        )
        assert ctrl._maybe_auto_switch_first_prismatic(prismatic_cards) is False

        # P0-3b: _finalize_match_eog backfills missing pre-join head [3060] while preserving picked 1323
        ctrl.sm.state = GameState.IN_GAME
        ctrl.sm.selected_augments = ["1323", "1238", "3150"]
        ctrl._match_is_first_prismatic = True
        ctrl._finalize_match_eog(
            phase="EndOfGame",
            eog_snap={
                "game_id": 55555,
                "won": 1,
                "duration_s": 880,
                "eog_augments_probed": [3060, 1004, 1238, 3150],
            },
            eog_source="eog_stats_block",
        )
        eog_evs = [p for _d, et, p in emitted if et == "match_eog_summary"]
        assert len(eog_evs) == 1
        assert eog_evs[0]["selected_augments"] == ["3060", "1323", "1238", "3150"]
        assert eog_evs[0]["is_first_prismatic"] is False

        # P0-4a & P1-7: Late post-close augment_picking dropped + AgentBridge latency & model backfilled
        ctrl.sm.state = GameState.IN_GAME
        ctrl._latest_request_id = "req-late-pick"
        ctrl._latest_request_trigger = "augment_picking"
        ctrl._last_coach_req_meta["req-late-pick"] = {
            "trigger_reason": "augment_picking",
            "local_top_item_id": "3089",
            "dispatched_mono": time.monotonic() - 0.12,
        }
        ctrl._on_coach_advice(CoachAdvicePayload(request_id="req-late-pick", tactical_summary="stale"))
        dropped_evs = [p for _d, et, p in emitted if et == "ai_coach_turn_telemetry" and p.get("request_id") == "req-late-pick"]
        assert len(dropped_evs) == 1
        assert dropped_evs[0]["stale_dropped"] is True
        assert dropped_evs[0]["arrived_before_pick"] is False
        assert dropped_evs[0]["model"] == "agent_bridge"
        assert dropped_evs[0]["latency_ms"] >= 100

        # Non-dropped request also backfills AgentBridge model & latency
        ctrl._latest_request_id = "req-ok-bridge"
        ctrl._latest_request_trigger = "manual_f6"
        ctrl._last_coach_req_meta["req-ok-bridge"] = {
            "trigger_reason": "manual_f6",
            "local_top_item_id": "3089",
            "dispatched_mono": time.monotonic() - 0.08,
        }
        ctrl._on_coach_advice(CoachAdvicePayload(request_id="req-ok-bridge", tactical_summary="ok"))
        ok_evs = [p for _d, et, p in emitted if et == "ai_coach_turn_telemetry" and p.get("request_id") == "req-ok-bridge"]
        assert ok_evs[-1]["stale_dropped"] is False
        assert ok_evs[-1]["model"] == "agent_bridge"
        assert ok_evs[-1]["latency_ms"] >= 60

        # P0-4b: _should_skip_picker_ai_call blocks Smart Picker Gate on partial_open
        ctrl._last_picker_partial_open = True
        ctrl._last_picker_expected_slots = 3
        skipped, _ = ctrl._should_skip_picker_ai_call(prismatic_cards)
        assert skipped is False
        assert ctrl._last_smart_gate_eval["reason"] == "partial_open_incomplete_slots"
    finally:
        _cleanup_controller(ctrl, qapp)

    # P0-4c: CloudCoachWorker.cancel_pending drops queued/in-flight augment_picking replies
    entered = threading.Event()
    release = threading.Event()
    delivered: list[str] = []

    class _SlowCloudClient:
        def advise(self, req: CoachRequestPayload, _store: DataStore | None, *, tier: str = "a") -> Any:
            entered.set()
            release.wait(timeout=2.0)
            return (
                CoachAdvicePayload(request_id=req.request_id, tactical_summary="ok"),
                CloudAdviseStats(tier=tier, model="m", thinking_level="low", latency_ms=10),
            )

    worker = CloudCoachWorker(
        client=_SlowCloudClient(),  # type: ignore[arg-type]
        on_advice=lambda adv, _st: delivered.append(adv.request_id),
    )
    try:
        r_pick = CoachRequestPayload("c-pick", "augment_picking", "ts", "16.23", {})
        worker.submit(r_pick, None)
        assert entered.wait(timeout=1.5)
        assert worker.cancel_pending({"augment_picking"}) == 1
        release.set()
        time.sleep(0.15)
        assert delivered == []
    finally:
        release.set()
        worker.stop()


# ---------------------------------------------------------------------------
# Wave C: P1-5, P1-6, P1-8
# ---------------------------------------------------------------------------


def test_wave_c_twin_matcher_chogath_ap_and_devlab_self_heal(tmp_path: Path) -> None:
    store = _make_v152_store(tmp_path)

    # P1-5: Exclamation tie-break (1322 vs 3004) + zh_sg rarity tie-break (2076 silver vs 3263 gold)
    m_cn = AugmentMatcher([store.get_augment("1322"), store.get_augment("3004")])  # type: ignore[list-item]
    hit_bang, _ = m_cn.match_line_with_diagnostics("罪 恶 快 感 ！", rarity_hint=None)
    hit_plain, _ = m_cn.match_line_with_diagnostics("罪 恶 快 感", rarity_hint=None)
    assert hit_bang is not None and hit_bang.augment.augment_id == "3004"
    assert hit_plain is not None and hit_plain.augment.augment_id == "1322"

    zh_sg_catalog = build_ocr_catalog(
        [
            AugmentInfo("2076", "坚定不移", "silver", ""),
            AugmentInfo("3263", "坚定不移", "gold", ""),
        ],
        locale="zh_sg",
    )
    m_sg = AugmentMatcher(zh_sg_catalog, use_t2s=True)
    hit_silver, _ = m_sg.match_line_with_diagnostics(zh_sg_catalog[0].name, rarity_hint="silver")
    hit_gold, _ = m_sg.match_line_with_diagnostics(zh_sg_catalog[0].name, rarity_hint="gold")
    assert hit_silver is not None and hit_silver.augment.augment_id == "2076"
    assert hit_gold is not None and hit_gold.augment.augment_id == "3263"

    # P1-6: AP Cho'Gath (4645+1004 and 4645+3067+1004) stays AP, Aura tank blocked, 3150->4646 boosted
    assert infer_unified_hero_profile("31", store, my_items=("4645", "1004"), selected_augments=("3150",)) == "AP"
    assert infer_unified_hero_profile("31", store, my_items=("4645", "3067", "1004"), selected_augments=("3150",)) == "AP"
    for aura_tank in ("3068", "6664", "6667", "3110"):
        assert _is_item_off_active_profile(store, aura_tank, "AP") is True
    assert _is_item_off_active_profile(store, "3190", "AP") is False

    me_cho = PlayerLiveInfo("me", "科加斯", "Chogath", "ORDER", False, 10, 4, 2, 5, ("4645", "3067", "1004"))
    enemy = PlayerLiveInfo("e1", "金克丝", "Jinx", "CHAOS", False, 10, 3, 2, 1, ("3032",))
    _, recs = optimize_next_items(
        store,
        "31",
        my_items=("4645", "3067", "1004"),
        my_gold=2600.0,
        selected_augments=("3150",),
        allies=(me_cho,),
        enemies=(enemy,),
        limit=4,
    )
    assert "4646" in [r.item_id for r in recs[:2]]

    # P1-8: DevLab startup self-heal closes flushed unfinished champ_select match (events_bytes == ev_size)
    dev_root = tmp_path / "devlab"
    mdir = dev_root / "matches" / "m_00028_20261128_000000_31"
    mdir.mkdir(parents=True, exist_ok=True)
    ev_line = json.dumps(
        {"event_type": "lcu_champ_select", "ts_utc": "2026-11-28T10:05:00.000Z", "hero_id": "31"},
        ensure_ascii=False,
    ) + "\n"
    (mdir / "events.jsonl").write_text(ev_line, encoding="utf-8")
    ev_bytes = (mdir / "events.jsonl").stat().st_size
    (mdir / "meta.json").write_text(
        json.dumps(
            {
                "schema_v": 1,
                "seq": 28,
                "dir_name": mdir.name,
                "phase": "champ_select",
                "ended_at": "",
                "event_count": 1,
                "events_bytes": ev_bytes,
                "hero_id": "31",
            }
        ),
        encoding="utf-8",
    )
    (dev_root / "index.json").write_text(
        json.dumps({"next_seq": 29, "matches": [{"seq": 28, "dir_name": mdir.name, "phase": "champ_select", "ended_at": "", "event_count": 1}]}),
        encoding="utf-8",
    )
    rec = DevLabFlightRecorder(root_dir=dev_root, enable_debug_log=False)
    try:
        healed_meta = json.loads((mdir / "meta.json").read_text(encoding="utf-8"))
        assert healed_meta["phase"] == "interrupted"
        assert healed_meta["ended_at"] == "2026-11-28T10:05:00.000Z"
    finally:
        rec.flush_and_close()
