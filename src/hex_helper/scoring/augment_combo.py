"""0ms Local Augment-to-Augment Synergy, High-Ceiling Gamble & Anti-Synergy Downgrade Engine.

Completely independent from Bayesian win-rate tier scoring (scoring/tier.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from hex_helper.data.augment_combos import (
    VALID_MECHANIC_AXES,
    AugmentComboRule,
    get_augment_combo_db,
)
from hex_helper.data.provider import AugmentInfo
from hex_helper.data.special_builds import SpecialAugmentBuild, get_special_augment_db
from hex_helper.data.store import DataStore
from hex_helper.scoring.build_synergy import normalize_item_id
from hex_helper.scoring.matchup_optimizer import (
    ENCHANTER_SUPPORT_ITEM_IDS,
    UPGRADE_AUGMENT_TARGET_ITEMS,
    _owned_component_credit,
    _owned_unstacked_tear_base_ids,
    analyze_inventory_slots,
    infer_unified_hero_profile,
)
from hex_helper.scoring.tier import score_hero_augment

AXIS_LABELS: dict[str, str] = {
    "AP_SPELL": "法术技能",
    "AD_ATTACK": "物理攻击",
    "AUTO_ONHIT": "普攻/攻速特效",
    "CRIT_ATTACK": "物理暴击",
    "CRIT_SPELL": "技能暴击",
    "HASTE_LOOP": "技能循环极速",
    "BURN_DOT": "持续灼烧",
    "TANK_HP": "坦克生命",
    "SHIELD_HEAL": "盾奶辅助",
    "ULT_CYCLE": "大招循环",
    "MOBILITY_DASH": "位移机动",
    "GAMBLE_HIGHROLL": "高上限赌狗",
    "UTILITY_SUSTAIN": "通用续航",
}

_HYBRID_BRIDGE_AUGMENT_IDS: frozenset[str] = frozenset({"1054", "1029", "1129", "1205", "1206"})
_META_NON_COMBAT_AXES: frozenset[str] = frozenset({"GAMBLE_HIGHROLL", "MOBILITY_DASH", "UTILITY_SUSTAIN"})
INSTANT_PRISMATIC_TRANSMUTE_IDS: frozenset[str] = frozenset({"1238"})
LATE_STACKER_AUGMENT_DISCOUNTS: dict[str, float] = {
    "1353": -8.5,  # 坦克引擎：第4轮/11.5分钟后叠层收益大幅折价
    "1373": -7.5,  # 缩小引擎：第4轮/11.5分钟后叠加层数窗口不足
    "1152": -24.0,  # 钢化你心：第4轮无心之钢视作白板陷阱（已持3084时豁免）
}
_MANALESS_HERO_IDS: frozenset[str] = frozenset(
    {
        "8", "23", "36", "55", "58", "64", "68", "82", "84", "85", "86", "92",
        "98", "102", "107", "150", "154", "157", "200", "233", "234", "238",
        "240", "266", "421", "777", "799", "875",
    }
)
_MANA_ITEM_FALLBACK_IDS: frozenset[str] = frozenset(
    {
        "3070", "3003", "3040", "3004", "3042", "3119", "3121", "2526", "2530",
        "6657", "6655", "2503", "3118", "3110", "4401", "3050",
    }
)
_HYDRA_QUEST_ITEMS: tuple[str, str, str] = ("3074", "3748", "6698")


def level_milestone_round(level: int) -> int:
    """Map champion level to minimum elapsed ARAM Mayhem augment round (3->1, 7->2, 11->3, 15->4)."""
    lv = int(level or 0)
    if lv >= 15:
        return 4
    if lv >= 11:
        return 3
    if lv >= 7:
        return 2
    if lv >= 3:
        return 1
    return 0


def effective_augment_round(
    selected_count: int,
    my_level: int = 0,
    explicit_round: int = 0,
) -> int:
    """Compute true augment round floored by champion level milestone (prevents mid-game join miscount)."""
    base = int(explicit_round) if int(explicit_round or 0) > 0 else (int(selected_count or 0) + 1)
    return max(1, min(4, max(base, level_milestone_round(my_level))))


def _is_manaless_hero(hero_id: str, my_stats: Mapping[str, Any] | None) -> bool:
    if my_stats:
        rtype = str(my_stats.get("resourceType") or my_stats.get("resource_type") or "").strip().upper()
        if rtype:
            return rtype != "MANA"
    return str(hero_id or "").strip() in _MANALESS_HERO_IDS


def evaluate_augment_round_decay(
    store: DataStore | None,
    hero_id: str,
    aid: str,
    my_items: Sequence[str],
    selected_augments: Sequence[str],
    *,
    round_index: int = 0,
    game_time_s: float = 0.0,
    my_gold: float = 0.0,
    my_level: int = 0,
    my_stats: Mapping[str, Any] | None = None,
    primary_profile: str = "",
) -> tuple[float, str, str] | None:
    """Return (decay_delta, short_label, reason) when a scaling/quest augment decays in R3/R4, else None."""
    aug_id = str(aid or "").strip()
    if not aug_id:
        return None
    owned_set = {str(a).strip() for a in selected_augments if str(a).strip()}
    eff_round = effective_augment_round(len(owned_set), my_level, round_index)
    t = float(game_time_s or 0.0)
    # Must check R4 BEFORE R3 so round_index==3 with t>=690s lands in R4!
    is_r4 = (eff_round >= 4) or (t >= 690.0)
    is_r3 = (not is_r4) and ((eff_round == 3) or (450.0 <= t < 690.0))
    if not is_r3 and not is_r4:
        return None

    owned_norm = {normalize_item_id(x) for x in my_items if str(x).strip()}
    inv = analyze_inventory_slots(store, list(my_items)) if store is not None else None
    comp_ids = [normalize_item_id(x) for x in (inv.complete_items if inv is not None else owned_norm)]
    n_comp = len(inv.complete_items) if inv is not None else len(owned_norm)
    has_boots_slot = 1 if (inv is not None and inv.tier2_boots_id) else 0
    occupied_cb = n_comp + has_boots_slot
    is_full = bool(inv.is_full_build) if inv is not None else (n_comp >= 6)

    def _credit(item_id: str) -> int:
        if store is None:
            return 0
        return _owned_component_credit(store, item_id, list(my_items))[0]

    # A. Multi-item & conditional kill quests
    if aug_id == "2141":  # 终极九头蛇
        hydra_set = set(_HYDRA_QUEST_ITEMS)
        owned_h = sum(1 for c in comp_ids if c in hydra_set)
        non_h_comp = sum(1 for c in comp_ids if c not in hydra_set)
        if owned_h >= 2 and non_h_comp <= 3:
            return None
        owns_tiamat = "3077" in owned_norm
        if is_r4:
            if non_h_comp >= 4 and owned_h == 0:
                return (-28.0, "格子锁死·废卡", "终极九头蛇需同时占用3格合体，4件其他成装下物理锁死且0即时战力")
            if non_h_comp >= 4 and owned_h == 1:
                return (-22.0, "格子锁死·废卡", "仅持1把九头蛇且剩余格子不足以同时容纳3把合体，第4轮切忌选")
            if owned_h == 0:
                return (-24.0, "末轮任务废卡", "第4轮0把九头蛇起手需净攒7050G集齐3把，对局剩余时间基本无法完成")
            return (-12.0, "末轮双九头慢", "第4轮仅1把九头蛇还差2把大件，合体偏慢")
        if owned_h == 0 and non_h_comp >= 2:
            return (-14.5, "中盘转型血亏", "已有2件非九头蛇成装再追3把九头蛇，经济与栏位严重冲突")
        if owned_h == 0 and non_h_comp <= 1:
            return (-8.5, "中盘任务偏慢", "第3轮0把九头蛇起手需7050G完赛，优先级低于即时战力")
        if owned_h >= 1 or owns_tiamat:
            return (-3.5, "中盘九头略慢", "已持九头蛇或提亚马特，仍需补齐其余九头蛇")
        return (-3.5, "中盘九头略慢", "中盘集齐3把九头蛇周期偏长")

    if aug_id == "1152":  # 钢化你心
        if "3084" in owned_norm:
            return None
        if is_r4:
            return (-24.0, "无心之钢·废卡", "第4轮未出心之钢选钢化你心无任何属性增益且难叠满，优先即时战力")
        if _credit("3084") >= 1200 and n_comp <= 1:
            return (-3.5, "无钢慎选·略慢", "虽已备心之钢散件，第3轮现叠心之钢仍略慢")
        return (-11.5, "无钢慎选·难叠", "第3轮尚未持有心之钢，现出现叠极难达成任务线")

    if aug_id == "1361":  # 艾卡西亚的陷落
        bami_set = {"3068", "6664"}
        owns_bami = bool(owned_norm & bami_set)
        other_cb = sum(1 for c in comp_ids if c not in bami_set) + has_boots_slot
        slot_locked = other_cb > 4 or (is_full and not bami_set.issubset(owned_norm))
        if owns_bami and not slot_locked:
            return None
        if is_r4:
            if slot_locked:
                return (-22.0, "格子锁死·废卡", "第4轮格子不足以同时容纳日炎与璀璨回响完成合体")
            if n_comp >= 4:
                return (-20.0, "末轮双献祭废", "第4轮已有4成装且缺献祭核心，难凑齐双日炎合体")
            return (-12.5, "末轮双献祭慢", "第4轮0件献祭装起手追双件合体偏慢")
        if n_comp >= 2:
            return (-4.5, "中盘双件偏慢", "已有2成装再起手追双献祭合体节奏偏慢")
        return None

    if aug_id == "1156":  # 沃格勒特的巫师帽
        hat_set = {"3089", "3157"}
        owns_hat_or_zh = bool(owned_norm & hat_set)
        both_owned = hat_set.issubset(owned_norm)
        other_cb = sum(1 for c in comp_ids if c not in hat_set) + has_boots_slot
        slot_locked = other_cb > 4 or (is_full and not both_owned)
        if owns_hat_or_zh and not slot_locked:
            return None
        if is_r4:
            if is_full and not owns_hat_or_zh:
                return (-22.0, "六神无帽·废卡", "六神满格且未出大帽与中娅，巫师帽任务无法凑齐")
            if slot_locked or (n_comp >= 4 and not owns_hat_or_zh):
                return (-18.5, "末轮双帽难凑", "第4轮栏位紧张且无大帽中娅，难完成任务")
            return (-11.0, "末轮双帽偏慢", "第4轮从零凑大帽与中娅周期偏长")
        if not owns_hat_or_zh and n_comp >= 3:
            return (-4.0, "双大件偏慢", "第3轮已3成装且无大帽中娅，双大件凑齐较慢")
        return None

    if aug_id == "1154":  # 海牛阿福的勇士
        if is_r4:
            return (
                -18.0 if is_full else -13.5,
                "末轮十助过慢",
                "第4轮选取需从零重新累计10次参与击杀才获金铲铲，期间0属性且易错失决胜团",
            )
        return (-5.0, "中盘任务真空", "选后需从零累计10次参与击杀，存在中盘战力真空期")

    # B. Permanent stacking & evolution questlines
    if aug_id == "1353":  # 坦克引擎
        if is_r4:
            return (
                -4.5 if "1204" in owned_set else -8.5,
                "末轮叠层折价",
                "第4轮/局末剩余团战波次少，坦克引擎叠层收益显著折价",
            )
        if "1204" in owned_set:
            return None
        return (-3.5, "中盘叠层偏慢", "第3轮坦克引擎叠层空间缩水，优先级略低于即时坦度")

    if aug_id == "1373":  # 缩小引擎 (V1-6)
        if is_r4:
            return (
                -4.0 if "1204" in owned_set else -7.5,
                "末轮叠层折价",
                "第4轮/局末剩余团战有限，缩小引擎参战叠层周期不足",
            )
        if "1204" in owned_set:
            return None
        return (-3.0, "中盘叠层偏慢", "第3轮缩小引擎启动偏晚，团战阵亡掉层风险高")

    if aug_id == "1204":  # 叠角龙
        has_stacker = (
            "3070" in owned_norm
            or "3084" in owned_norm
            or bool(_owned_unstacked_tear_base_ids(my_items))
        )
        if is_r4:
            return (
                -7.5 if has_stacker else -12.0,
                "末轮叠层过晚",
                "叠角龙不追溯已叠层数，第4轮才选仅剩2-3波团战加速价值有限",
            )
        return (
            -1.5 if has_stacker else -4.0,
            "中盘加速折价",
            "叠角龙不追溯前期层数，第3轮收益不如开局",
        )

    if aug_id == "2102":  # 高压锅
        return (-8.5, "末轮任务折价", "高压锅需贴脸持续灼烧从零刷层，局末剩余周期不足") if is_r4 else (
            -4.0, "中盘任务偏慢", "第3轮从零刷高压锅灼烧层数成型偏慢"
        )
    if aug_id == "3048":  # 弹跳蜜蜂
        return (-7.5, "末轮任务折价", "第4轮从零刷技能接普攻次数完成弹射进化周期不足") if is_r4 else (
            -3.5, "中盘任务偏慢", "第3轮弹跳蜜蜂需从零累计触发次数"
        )
    if aug_id == "2100":  # 急速之追求
        return (-7.5, "末轮任务折价", "第4轮从零刷技能命中任务解锁急速较慢") if is_r4 else (
            -3.5, "中盘任务偏慢", "第3轮任务流海克斯起步略慢"
        )
    if aug_id == "2103":  # 狙神飞星
        return (-7.5, "末轮任务折价", "第4轮从零刷远距命中解锁飞星周期不足") if is_r4 else (
            -3.5, "中盘任务偏慢", "第3轮狙神飞星需从零完成命中任务"
        )
    if aug_id == "2116":  # 威能之追求
        return (-7.0, "末轮任务折价", "第4轮技能进化任务滞后于即时爆发卡") if is_r4 else (
            -3.0, "中盘任务偏慢", "第3轮威能之追求成型略慢"
        )
    if aug_id == "2055":  # 多重射击
        return (-7.0, "末轮任务折价", "第4轮刷满分裂普攻任务前增益有限") if is_r4 else (
            -3.0, "中盘任务偏慢", "第3轮多重射击任务启动偏晚"
        )
    if aug_id == "3036":  # 翻倍时间
        has_mana_core = any(
            (store is not None and float((store.get_item_info(x).stats or {}).get("FlatMPPoolMod", 0.0) or 0.0) >= 240.0)
            or normalize_item_id(x) in {"3003", "3040", "3004", "3042", "3119", "3121", "2526", "2530", "6657", "6655", "2503", "3118", "3110"}
            for x in my_items
        )
        if is_r4:
            return (
                -3.0 if has_mana_core else -5.5,
                "末轮叠蓝偏少",
                "第4轮剩余时间较短，翻倍时间永久叠蓝量总量有限",
            )
        return None if has_mana_core else (-2.5, "中盘叠蓝偏慢", "无核心蓝量装下第3轮叠蓝偏慢")

    if aug_id == "2083":  # 牙仙子
        return (-5.5, "末轮叠穿偏慢", "第4轮剩余团战减半，捡牙叠双穿上限收窄") if is_r4 else (
            -2.0, "中盘叠穿折价", "第3轮牙仙子叠双穿周期减半"
        )

    if aug_id == "1390":  # 超凡邪恶
        if is_r4 and "1045" not in owned_set:
            return (-4.0, "末轮叠法偏少", "第4轮虽有追赶AP但后续技能命中叠加轮次有限")
        return None

    # C. Pure economy snowball, Upgrade single-item binds, Mana pool augments
    if aug_id == "1319":  # 升级：献祭
        owns_immolate_item = bool(owned_norm & {"3068", "6664"})
        owns_immolate_comp = owns_immolate_item or ("6660" in owned_norm)
        if is_r3:
            return None if (owns_immolate_comp and not is_full) else (-3.5, "中盘印钞偏晚", "无日炎/熔渣散件，中盘升级献祭打钱偏晚")
        if is_full:
            return (-14.0, "满装印钞无用", "六神满格后升级献祭的打钱经济词条价值归零")
        if owns_immolate_item:
            return (-6.5, "末轮打钱折价", "第4轮金币边际效用骤降，仅剩单日炎强化")
        return (-9.5, "末轮打钱折价", "第4轮无日炎且局末打钱收益骤降")

    if aug_id == "1384":  # 捐赠
        if is_r3:
            return (-3.5, "经济收益递减", "已成装3件以上，纯经济白板海克斯略逊战力卡") if n_comp >= 3 else None
        if is_full or (occupied_cb >= 5 and my_gold >= 1500):
            return (-26.0, "满装溢金·废卡", "接近/已满神装且不缺金币，第4轮选纯经济捐赠约等于零战力白板")
        if n_comp >= 4:
            return (-9.5, "末轮溢金折价", "已持有4件成装，第4轮纯打钱收益陡降")
        return (-4.0, "末轮经济折价", "第4轮局末直接战力优于纯经济海克斯")

    if aug_id == "1335":  # 夺金
        if is_r4:
            return (
                -10.5 if (is_full or n_comp >= 5) else -6.5,
                "末轮打钱折价",
                "第4轮打钱词条边际效用大幅下滑，仅剩伤害与急速收益",
            )
        return (-4.5 if n_comp >= 4 else -2.5, "中盘打钱折价", "中后期金币收益衰减")

    if aug_id == "1320":  # 升级：收集者
        owns_6676 = "6676" in owned_norm
        if is_r3:
            if owns_6676 and not is_full:
                return None
            if not owns_6676 and n_comp >= 2:
                return (-5.5, "未出收集者", "已有2成装但未出收集者，需现花3000G补齐才生效")
            return None
        if is_full and not owns_6676:
            return (-22.0, "满格无收集者", "六神满格且无收集者，升级：收集者完全无法触发")
        if not owns_6676 and n_comp >= 3:
            return (-14.0, "末轮无收集者", "第4轮未持收集者且格子紧张，难临时补3000G绑定装")
        if owns_6676 and is_full:
            return (-2.5, "满格打钱折价", "六神已满仅保留斩杀线提升，打钱词条无用")
        return None

    if aug_id in UPGRADE_AUGMENT_TARGET_ITEMS and aug_id not in ("1319", "1320"):
        targets = UPGRADE_AUGMENT_TARGET_ITEMS[aug_id]
        if any(t in owned_norm for t in targets):
            return None
        if is_full:
            return (-20.0, "满格缺绑定装", "六神满格且未持有对应绑定成装，无法空出格子触发升级")
        if is_r4:
            max_credit = max((_credit(t) for t in targets), default=0)
            if occupied_cb >= 5 and max_credit < 800:
                return (-5.5, "末轮缺绑定装", "仅剩1格空槽且无绑定装散件，第4轮补出代价较高")
        return None

    if aug_id in ("3059", "1056", "1311"):  # 没法力，没问题 / 由心及物 / 溢流 (V1-5)
        owns_mana = "3070" in owned_norm or any(
            normalize_item_id(x) in _MANA_ITEM_FALLBACK_IDS
            or (store is not None and float((store.get_item_info(x).stats or {}).get("FlatMPPoolMod", 0.0) or 0.0) >= 200.0)
            for x in my_items
        )
        if owns_mana:
            return None
        prof_u = (primary_profile or "").strip().upper()
        if _is_manaless_hero(hero_id, my_stats) or (aug_id == "3059" and prof_u in ("AD", "TANK")):
            if is_full or (is_r4 and n_comp >= 3):
                return (-18.0, "无蓝零装·废卡", "无蓝条/非AP流且未出蓝量装，法力系海克斯几乎无增益")
            return (-11.0, "无蓝条·废卡", "无蓝条或当前流派不契合法力池转换")
        if is_r4:
            if is_full:
                return (
                    (-6.5, "满格零蓝装", "虽吃15%总法强加成，但零蓝装丢失蓝转法强部分收益")
                    if aug_id == "3059"
                    else (-11.0, "满格无蓝装", "六神零蓝量装，仅靠基础蓝池收益大幅缩水")
                )
            if n_comp >= 3:
                return (-4.5, "无蓝装·折价", "3成装以上零蓝装，法力海克斯乘区受限")
        elif is_r3 and n_comp >= 2:
            return (-3.0, "无蓝装·略亏", "2成装以上尚无蓝量装，收益打折")
        return None

    # D. Early wave tempo & flat value decay
    if aug_id == "3049":  # 装甲冲击
        has_armor = any(
            store is not None
            and (
                store.get_item_info(x).category == "complete_equipment"
                or store.get_item_info(x).gold >= 2200
            )
            and float((store.get_item_info(x).stats or {}).get("FlatArmorMod", 0.0) or 0.0) >= 40.0
            for x in my_items
        )
        if has_armor:
            return None
        if is_r4 and n_comp >= 3:
            return (-6.5, "无护甲装·折价", "后期未出高护甲成装，装甲冲击伤害与护甲联动偏弱")
        if is_r3 and n_comp >= 2:
            return (-3.0, "缺护甲装", "尚无护甲成装支撑装甲冲击联动")
        return None

    if aug_id == "1389":  # 男爵之手
        return (-5.5, "末轮兵线折价", "后期外塔已破且双方清兵迅速，号令炮车推线红利大幅衰减") if is_r4 else (
            -2.5, "中盘推线折价", "中盘后兵线压塔收益随清线能力下降"
        )

    if aug_id == "1061":  # 回力OK镖
        return (-5.0, "后期刮痧·折价", "固定基础伤害随全员16-18级血量魔抗成长显著贬值") if is_r4 else (
            -2.5, "后期刮痧·折价", "中盘固定基础消耗伤害开始贬值"
        )

    if aug_id == "1013":  # 星界躯体 (V1-3 exact HP-scaling IDs, NO 3094!)
        prof_u = (primary_profile or "").strip().upper()
        if prof_u in ("TANK", "SUPPORT"):
            if owned_set & {"1041", "1001", "1181", "1353"}:
                return None
            return (-3.5, "末轮固定血折", "第4轮固定生命值相对满级基础血量占比下降") if is_r4 else (
                -1.5, "中盘固定血折", "中盘固定生命值加成边际稀释"
            )
        return (-8.5, "后期减伤血亏", "C位第4轮拿星界躯体降10%伤害且固定血量贬值，得不偿失") if is_r4 else (
            -4.5, "中盘减伤折价", "输出位承受10%伤害削减，后期性价比偏低"
        )

    return None

# P0-5: Pure crit-stat overflow guard constants (strictly targets 1047; exempts hybrid/mechanic crit cards)
PURE_STAT_CRIT_AUGMENT_ID = "1047"  # 关键暴击 (+50% pure crit chance)
UPGRADE_IE_AUGMENT_ID = "1336"  # 升级：无尽之刃
YUNTAL_ITEM_IDS: frozenset[str] = frozenset({"3032", "223032"})  # 育恩塔尔荒野箭 (FlatCritChanceMod=0 in DDragon)
DOUBLE_CRIT_HERO_IDS: frozenset[str] = frozenset({"157", "777"})  # 亚索 / 永恩 (2x crit passive)
SENNA_HERO_ID = "235"  # 赛娜 (overflow crit converts to lifesteal)
TRYNDAMERE_HERO_ID = "23"  # 泰达米尔 (fountain 0-fury floor)

AUGMENT_CRIT_CHANCE_MAP: dict[str, float] = {
    "1047": 0.50,  # 关键暴击
    "1048": 0.25,  # 珠光护手 (+0.045 * AP / 100)
    "1092": 0.25,  # 易损
    "1077": 0.25,  # 灵魂虹吸
    "1080": 0.25,  # 会心防御
    "1118": 0.25,  # 会心治疗
    "1328": 0.25,  # 暴击律动
    "3123": 0.25,  # 珠光手镯
}
EXEMPT_HYBRID_CRIT_AUGMENT_IDS: frozenset[str] = frozenset(
    {"1048", "1092", "1077", "1080", "1118", "1328", "3123"}
)

# P0-7: Local comp-aware tactical augment classification sets
_TANK_PROFILE_TOKENS: frozenset[str] = frozenset({"TANK", "VANGUARD", "WARDEN"})
_DIVE_MELEE_PROFILE_TOKENS: frozenset[str] = frozenset(
    {"ASSASSIN", "DIVER", "FIGHTER", "BRUISER", "SKIRMISHER", "MELEE"}
)
_RANGED_SQUISHY_PROFILE_TOKENS: frozenset[str] = frozenset(
    {"AP", "AD", "SUPPORT", "MAGE", "MARKSMAN", "ARTILLERY", "ENCHANTER", "RANGED", "POKE", "CRIT_SPELL"}
)

_COMP_ANTI_TANK_IDS: frozenset[str] = frozenset(
    {"1045", "1092", "1077", "1225", "1195", "1039", "1036", "2005", "2010", "1079"}
)
_COMP_ANTI_TANK_AXES: frozenset[str] = frozenset({"BURN_DOT", "AUTO_ONHIT", "HASTE_LOOP"})

_COMP_SURVIVAL_IDS: frozenset[str] = frozenset(
    {"1080", "1318", "2157", "1062", "1019", "1073", "1116", "1333", "1018", "1149", "1138", "2143", "1013"}
)
_COMP_SURVIVAL_AXES: frozenset[str] = frozenset({"TANK_HP", "MOBILITY_DASH"})

_COMP_POKE_SQUISHY_IDS: frozenset[str] = frozenset(
    {"1115", "1048", "3018", "1071", "1170", "1113", "3123", "1019", "1073", "1329"}
)
_COMP_POKE_SQUISHY_AXES: frozenset[str] = frozenset({"CRIT_SPELL", "MOBILITY_DASH"})

_COMP_HIGH_HEAL_IDS: frozenset[str] = frozenset(
    {"1048", "3018", "1045", "1133", "1388", "1349", "1092", "1325"}
)
_COMP_HIGH_HEAL_AXES: frozenset[str] = frozenset({"CRIT_SPELL", "BURN_DOT", "HASTE_LOOP", "ULT_CYCLE"})
_GLASS_CANNON_AUGMENT_ID = "1325"

_RARITY_PRIORITY: dict[str, float] = {
    "prismatic": 1.2,
    "gold": 0.7,
    "silver": 0.3,
}

_NAME_AXIS_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("暴击", "无尽", "收集者"), "CRIT_ATTACK"),
    (("珠光", "易损"), "CRIT_SPELL"),
    (("攻速", "射击", "双刀", "台风", "瞄准镜", "灵巧", "九头蛇", "扳机"), "AUTO_ONHIT"),
    (("大力", "物理", "焚天", "死亡之舞", "斩杀", "耀光"), "AD_ATTACK"),
    (("魔法", "法强", "巫师", "法力", "奥术", "风暴", "咒语", "飞弹", "法师"), "AP_SPELL"),
    (("循环", "急速", "尤里卡", "面包", "冲拳"), "HASTE_LOOP"),
    (("炼狱", "灼烧", "火", "侵蚀", "献祭", "蜜蜂"), "BURN_DOT"),
    (("坦克", "巨人", "钢化", "生命", "护甲", "巨像", "不动如山", "重量级", "坚不可破"), "TANK_HP"),
    (("护盾", "治疗", "急救", "屏障", "风语者", "蝴蝶", "死亡之环", "全心为你", "治愈", "拯救"), "SHIELD_HEAL"),
    (("终极", "大招", "杀戮时间", "精怪魔法"), "ULT_CYCLE"),
    (("身法", "闪现", "疾奔", "雪球", "逃跑", "快步", "练腿"), "MOBILITY_DASH"),
    (("质变", "潘朵拉", "属性", "捐赠", "夺金", "命运之武器", "大腕"), "GAMBLE_HIGHROLL"),
)


@dataclass(frozen=True)
class MatchBuildProfile:
    primary_profile: str  # "AP" | "AD" | "CRIT_SPELL" | "TANK" | "SUPPORT" | "HYBRID" | "BALANCED"
    direction_label: str
    favored_axes: tuple[str, ...]
    penalized_axes: tuple[str, ...]
    penalized_labels: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ActiveComboProgress:
    combo_id: str
    name: str
    gamble: bool
    owned_ids: tuple[str, ...]
    missing_core_ids: tuple[str, ...]
    missing_core_names: tuple[str, ...]
    completed: bool
    synergy_reason: str
    reroll_hint: str


@dataclass(frozen=True)
class ComboFollowUp:
    augment_id: str
    name: str
    rarity: str
    combo_id: str
    combo_name: str
    reason: str
    gamble: bool
    priority_score: float
    hero_signature: bool
    comp_tag: str = ""
    win_delta: float = 0.0
    tier_label: str = ""


@dataclass(frozen=True)
class ComboPlan:
    hero_id: str | None
    hero_name: str
    profile: MatchBuildProfile
    active_combos: tuple[ActiveComboProgress, ...]
    recommended_targets: tuple[ComboFollowUp, ...]
    opening_combos: tuple[ActiveComboProgress, ...]
    avoid_summary: str
    round_index: int = 1

    def to_coach_dict(self, picker_verdict: PickerComboVerdict | None = None) -> dict[str, Any]:
        return export_combo_coach_context(self, picker_verdict)


@dataclass(frozen=True)
class PickerCardComboAdvice:
    slot_index: int
    augment_id: str
    name: str
    rarity: str
    verdict: str  # "jackpot" | "gamble" | "synergy" | "neutral" | "conflict"
    verdict_label: str
    combo_delta: float
    reason: str
    combo_name: str
    gamble: bool


@dataclass(frozen=True)
class PickerComboVerdict:
    profile: MatchBuildProfile
    cards: tuple[PickerCardComboAdvice, ...]
    best_slot_index: int
    reroll_advice: str
    round_index: int = 1


def _aug_name_rarity(store: DataStore | None, augment_id: str) -> tuple[str, str]:
    aid = str(augment_id)
    if store is not None:
        info = store.get_augment(aid)
        if info is not None:
            return info.name, info.rarity
    sp = get_special_augment_db().get_by_id(aid)
    if sp is not None:
        return sp.name, sp.rarity
    return f"海克斯#{aid}", "gold"


def _is_aug_eligible(store: DataStore | None, hero_id: str, augment_id: str) -> bool:
    """Authoritative hero augment pool gate (fails open safely when store lacks hero pool data)."""
    if store is None or not hero_id:
        return True
    checker = getattr(store, "is_augment_eligible_for_hero", None)
    if callable(checker):
        return bool(checker(hero_id, augment_id))
    return True


def _rule_is_eligible_for_hero(store: DataStore | None, hero_id: str, rule: AugmentComboRule) -> bool:
    """Return True iff hero can legally roll the combo's anchor (if any) and >=2 core augments."""
    if not hero_id or store is None:
        return True
    if rule.required_anchor and not _is_aug_eligible(store, hero_id, rule.required_anchor):
        return False
    elig_cores = [aid for aid in rule.core_augments if _is_aug_eligible(store, hero_id, aid)]
    min_needed = min(2, len(rule.core_augments))
    return len(elig_cores) >= min_needed


def _resolve_special_for_hero(
    store: DataStore | None,
    augment_id: str,
    hero_id: str | None = None,
    *,
    data_band_mode: str = "all",
) -> SpecialAugmentBuild | None:
    """Resolve special augment build with P1-1 hero-level force_item_gate when hero_id is available."""
    sp_db = get_special_augment_db()
    if hero_id:
        resolver = getattr(sp_db, "resolve_for_hero", None)
        if callable(resolver):
            try:
                return resolver(store, augment_id, hero_id, band_mode=data_band_mode)
            except TypeError:
                return resolver(store, augment_id, hero_id)
    return sp_db.resolve(store, augment_id)


def estimate_effective_crit_chance(
    store: DataStore | None,
    hero_id: str | None,
    my_items: Sequence[str] = (),
    selected_augments: Sequence[str] = (),
    my_stats: Mapping[str, Any] | None = None,
    game_time_s: float = 0.0,
) -> float:
    """Dual-source live critical strike chance estimator (P0-5).

    Primary source: LiveClientData `my_stats["critChance"]` (normalized >1.5 by /100).
    Fallback/lower-bound: item FlatCritChanceMod + Yun Tal (3032/223032) + selected crit augments
    (1047: 0.50; 1048/1092/1077/1080/1118/1328/3123: 0.25; 1048 adds 0.045 * AP / 100),
    doubled for Yasuo (157) / Yone (777), with Tryndamere (23) fountain 0-fury floor (+0.35 when item crit >= 0.50).
    Returns max(live, fallback).
    """
    live = 0.0
    if my_stats:
        raw_live = my_stats.get("critChance")
        if raw_live is not None:
            try:
                live = float(raw_live)
            except (TypeError, ValueError):
                live = 0.0
            if live > 1.5:
                live /= 100.0
            live = max(0.0, live)

    hid = str(hero_id).strip() if hero_id else ""
    gt = float(game_time_s or 0.0)

    item_crit = 0.0
    for raw_iid in my_items:
        iid = str(raw_iid).strip()
        if not iid:
            continue
        nid = normalize_item_id(iid)
        if iid in YUNTAL_ITEM_IDS or nid in YUNTAL_ITEM_IDS:
            item_crit += 0.25 if gt >= 300.0 else 0.12
            continue
        if store is not None:
            info = store.get_item_info(iid)
            c = float((info.stats or {}).get("FlatCritChanceMod", 0.0) or 0.0)
            if c <= 0.0 and nid and nid != iid:
                ninfo = store.get_item_info(nid)
                c = float((ninfo.stats or {}).get("FlatCritChanceMod", 0.0) or 0.0)
            if c > 1.5:
                c /= 100.0
            if c > 0.0:
                item_crit += c

    ap_val = 0.0
    if my_stats:
        try:
            ap_val = max(0.0, float(my_stats.get("abilityPower", 0.0) or 0.0))
        except (TypeError, ValueError):
            ap_val = 0.0

    aug_crit = 0.0
    for aid in {str(a).strip() for a in selected_augments if str(a).strip()}:
        aug_crit += AUGMENT_CRIT_CHANCE_MAP.get(aid, 0.0)
        if aid == "1048" and ap_val > 0.0:
            aug_crit += 0.045 * (ap_val / 100.0)

    fallback = item_crit + aug_crit
    if hid in DOUBLE_CRIT_HERO_IDS:
        fallback *= 2.0
    if hid == TRYNDAMERE_HERO_ID and item_crit >= 0.50:
        fallback += 0.35

    return round(max(live, fallback), 4)


@dataclass(frozen=True)
class _CompSignals:
    active: bool
    is_multi_tank: bool
    is_dive_or_fragile: bool
    is_squishy_poke: bool
    is_high_heal: bool


def _extract_comp_signals(
    threat: Any | None,
    ally_profiles: Sequence[str],
    enemy_profiles: Sequence[str],
) -> _CompSignals:
    if threat is None and not ally_profiles and not enemy_profiles:
        return _CompSignals(False, False, False, False, False)

    ally_profs = [str(p).strip().upper() for p in ally_profiles if str(p).strip()]
    enemy_profs = [str(p).strip().upper() for p in enemy_profiles if str(p).strip()]

    enemy_tank_from_prof = sum(1 for p in enemy_profs if p in _TANK_PROFILE_TOKENS)
    enemy_tank_count = max(int(getattr(threat, "enemy_tank_count", 0) or 0), enemy_tank_from_prof)
    enemy_hp_score = float(getattr(threat, "enemy_hp_score", 0.0) or 0.0)
    enemy_armor_score = float(getattr(threat, "enemy_armor_score", 0.0) or 0.0)
    enemy_heal_score = float(getattr(threat, "enemy_heal_score", 0.0) or 0.0)
    enemy_cc_score = float(getattr(threat, "enemy_cc_score", 0.0) or 0.0)
    enemy_snowball_count = int(getattr(threat, "enemy_snowball_count", 0) or 0)
    enemy_ad_burst = float(getattr(threat, "enemy_ad_burst", 0.0) or 0.0)
    enemy_ap_burst = float(getattr(threat, "enemy_ap_burst", 0.0) or 0.0)

    raw_ally_tanks = getattr(threat, "ally_tank_count", None)
    if raw_ally_tanks is not None:
        ally_tank_count: int | None = int(raw_ally_tanks)
    elif ally_profs:
        ally_tank_count = sum(1 for p in ally_profs if p in _TANK_PROFILE_TOKENS)
    else:
        ally_tank_count = None

    enemy_dive_from_prof = sum(1 for p in enemy_profs if p in _DIVE_MELEE_PROFILE_TOKENS)
    enemy_dive_explicit = (
        enemy_dive_from_prof
        + int(getattr(threat, "enemy_dive_count", 0) or 0)
        + int(getattr(threat, "enemy_assassin_count", 0) or 0)
    )
    enemy_melee_count = max(
        int(getattr(threat, "enemy_melee_count", 0) or 0),
        enemy_dive_explicit,
        enemy_dive_from_prof + enemy_tank_from_prof,
        enemy_snowball_count,
    )
    enemy_ranged_count = max(
        int(getattr(threat, "enemy_ranged_count", 0) or 0),
        sum(1 for p in enemy_profs if p in _RANGED_SQUISHY_PROFILE_TOKENS),
    )

    is_multi_tank = (enemy_tank_count >= 2) or (enemy_hp_score >= 3.2) or (enemy_armor_score >= 3.0)
    is_dive_or_fragile = (
        enemy_ad_burst >= 2.2
        or enemy_ap_burst >= 2.2
        or enemy_snowball_count >= 3
        or enemy_dive_explicit >= 2
        or (
            ally_tank_count is not None
            and ally_tank_count == 0
            and (enemy_melee_count >= 2 or enemy_cc_score >= 2.2 or enemy_snowball_count >= 2)
        )
    )
    is_squishy_poke = (enemy_tank_count == 0) and (enemy_ranged_count >= 4) and (enemy_hp_score < 2.2)
    is_high_heal = enemy_heal_score >= 2.4

    return _CompSignals(
        active=bool(is_multi_tank or is_dive_or_fragile or is_squishy_poke or is_high_heal),
        is_multi_tank=is_multi_tank,
        is_dive_or_fragile=is_dive_or_fragile,
        is_squishy_poke=is_squishy_poke,
        is_high_heal=is_high_heal,
    )


def _compute_comp_adjustment(
    augment_id: str,
    axes: tuple[str, ...],
    signals: _CompSignals,
) -> tuple[float, str]:
    """Compute both-teams comp tactical weighting clamped to [-1.5, +2.8] and crisp comp_tag."""
    if not signals.active:
        return 0.0, ""

    aid = str(augment_id)
    axis_set = set(axes)
    contribs: list[tuple[float, str]] = []

    if signals.is_multi_tank:
        if aid in _COMP_ANTI_TANK_IDS:
            contribs.append((2.5, "打敌多坦"))
        elif axis_set & _COMP_ANTI_TANK_AXES:
            contribs.append((1.9, "打敌多坦"))

    if signals.is_dive_or_fragile:
        if aid in _COMP_SURVIVAL_IDS:
            contribs.append((2.4, "防突进保命"))
        elif axis_set & _COMP_SURVIVAL_AXES:
            contribs.append((1.8, "防突进保命"))
        elif aid == _GLASS_CANNON_AUGMENT_ID:
            contribs.append((-1.5, ""))

    if signals.is_squishy_poke:
        if aid in _COMP_POKE_SQUISHY_IDS:
            contribs.append((2.2, "打长手脆皮"))
        elif axis_set & _COMP_POKE_SQUISHY_AXES:
            contribs.append((1.7, "打长手脆皮"))

    if signals.is_high_heal:
        if aid in _COMP_HIGH_HEAL_IDS:
            contribs.append((1.8, "压高回复"))
        elif axis_set & _COMP_HIGH_HEAL_AXES:
            contribs.append((1.3, "压高回复"))

    if not contribs:
        return 0.0, ""

    positives = [c for c in contribs if c[0] > 0]
    negatives = [c for c in contribs if c[0] < 0]
    neg_sum = sum(c[0] for c in negatives)

    if positives:
        positives.sort(key=lambda x: x[0], reverse=True)
        primary_val, primary_tag = positives[0]
        extra = sum(v * 0.25 for v, _ in positives[1:])
        raw = primary_val + extra + neg_sum
        clamped = round(max(-1.5, min(2.8, raw)), 2)
        return clamped, (primary_tag if clamped >= 1.0 else "")

    clamped = round(max(-1.5, min(2.8, neg_sum)), 2)
    return clamped, ""


def _compute_empirical_calibration(
    store: DataStore | None,
    hero_id: str,
    augment_id: str,
    name: str,
    rarity: str,
    *,
    data_band_mode: str = "all",
) -> tuple[float, float, str, bool]:
    """Hero empirical big-data calibration: (emp_adj, win_delta, tier_label, heavily_demoted)."""
    if store is None or not hero_id:
        return 0.0, 0.0, "", False
    use_band = bool(data_band_mode and str(data_band_mode).strip().lower() != "all")
    try:
        st = (
            store.get_hero_augment_stat(hero_id, augment_id, band_mode=data_band_mode)
            if use_band
            else store.get_hero_augment_stat(hero_id, augment_id)
        )
    except TypeError:
        st = store.get_hero_augment_stat(hero_id, augment_id)
    try:
        hero = (
            store.get_hero(hero_id, band_mode=data_band_mode)
            if use_band
            else store.get_hero(hero_id)
        )
    except TypeError:
        hero = store.get_hero(hero_id)
    if st is None or st.games < 30 or hero is None:
        return 0.0, 0.0, "", False
    rdist = store.get_hero_rarity_distribution(hero_id, rarity)
    sc = score_hero_augment(
        augment_id,
        name,
        rarity,
        hero.win_rate,
        st.wins,
        st.games,
        low_sample=st.low_sample,
        rarity_dist=rdist,
    )
    emp_adj = max(-3.5, min(3.5, sc.delta * 80.0))
    heavily_demoted = bool(
        sc.percentile is not None and sc.percentile < 20.0 and sc.delta < -0.025
    )
    if heavily_demoted:
        emp_adj -= 8.0
    return round(emp_adj, 2), round(sc.delta, 5), sc.tier, heavily_demoted


def infer_augment_axes(
    store: DataStore | None,
    augment_id: str,
    *,
    hero_id: str | None = None,
    data_band_mode: str = "all",
) -> tuple[str, ...]:
    """Return mechanic axes for any augment (explicit DB -> special builds -> name keywords)."""
    aid = str(augment_id).strip()
    if not aid:
        return ()

    db = get_augment_combo_db()
    explicit = db.get_axes(aid)
    if explicit:
        return explicit

    found: list[str] = []

    def _add(ax: str) -> None:
        if ax in VALID_MECHANIC_AXES and ax not in found:
            found.append(ax)

    sp = _resolve_special_for_hero(store, aid, hero_id, data_band_mode=data_band_mode)
    if sp is not None:
        prof = sp.shifts_profile.upper()
        if prof == "AP":
            _add("AP_SPELL")
        elif prof == "AD":
            _add("AD_ATTACK")
        elif prof == "TANK":
            _add("TANK_HP")
        req = set(sp.required_tags)
        if "SpellDamage" in req:
            _add("AP_SPELL")
        if "Damage" in req:
            _add("AD_ATTACK")
        if "AttackSpeed" in req or "OnHit" in req:
            _add("AUTO_ONHIT")
        if "CriticalStrike" in req:
            _add("CRIT_ATTACK")
        if "Health" in req or "Armor" in req or "SpellBlock" in req:
            _add("TANK_HP")
        if "ManaRegen" in req:
            _add("SHIELD_HEAL")

    name, _ = _aug_name_rarity(store, aid)
    for keywords, axis in _NAME_AXIS_KEYWORDS:
        if any(kw in name for kw in keywords):
            _add(axis)

    return tuple(found)


_PREF_PROFILE_LABELS: dict[str, str] = {
    "AP": "AP法强",
    "AD": "物理AD",
    "TANK": "重装坦",
    "SUPPORT": "软辅",
    "HYBRID": "双修",
}


def infer_match_build_profile(
    store: DataStore | None,
    hero_id: str | None,
    selected_augments: Sequence[str],
    my_items: Sequence[str],
    *,
    my_stats: dict[str, Any] | Mapping[str, Any] | None = None,
    data_band_mode: str = "all",
    preferred_profile: str = "",
) -> MatchBuildProfile:
    """Infer live match build direction and anti-synergy axes from owned augments + inventory + hero."""
    db = get_augment_combo_db()
    owned_ids = [str(a).strip() for a in selected_augments if str(a).strip()]
    owned_set = set(owned_ids)
    hid = str(hero_id).strip() if hero_id else ""
    has_hybrid_bridge = bool(owned_set & _HYBRID_BRIDGE_AUGMENT_IDS)
    pref_prof = str(preferred_profile or "").strip().upper()
    has_manual_pref = pref_prof in ("AP", "AD", "TANK", "SUPPORT", "HYBRID")
    stats_dict: dict[str, Any] | None = (
        my_stats
        if isinstance(my_stats, dict)
        else (dict(my_stats) if my_stats is not None else None)
    )

    axis_scores: dict[str, float] = {}
    profile_scores: dict[str, float] = {
        "AP": 0.0,
        "AD": 0.0,
        "CRIT_SPELL": 0.0,
        "TANK": 0.0,
        "SUPPORT": 0.0,
    }
    anti_votes: dict[str, float] = {}
    evidence: list[str] = []
    if has_manual_pref:
        if pref_prof == "HYBRID":
            profile_scores["AP"] += 5.0
            profile_scores["AD"] += 5.0
        elif pref_prof in profile_scores:
            profile_scores[pref_prof] += 10.0
        evidence.append(f"已选定 {_PREF_PROFILE_LABELS.get(pref_prof, pref_prof)} 流派")

    def _bump_axis(ax: str, weight: float) -> None:
        axis_scores[ax] = axis_scores.get(ax, 0.0) + weight

    # 1. Strongest signal: already selected augments
    for aid in owned_ids:
        axes = infer_augment_axes(store, aid, hero_id=hid or None, data_band_mode=data_band_mode)
        nm, _ = _aug_name_rarity(store, aid)
        for ax in axes:
            _bump_axis(ax, 2.4)
            if ax in ("AP_SPELL", "BURN_DOT"):
                profile_scores["AP"] += 2.2
            elif ax in ("AD_ATTACK", "AUTO_ONHIT", "CRIT_ATTACK"):
                profile_scores["AD"] += 2.1
            elif ax == "CRIT_SPELL":
                profile_scores["CRIT_SPELL"] += 2.8
            elif ax == "TANK_HP":
                profile_scores["TANK"] += 2.4
            elif ax == "SHIELD_HEAL":
                profile_scores["SUPPORT"] += 2.6

        sp = _resolve_special_for_hero(store, aid, hid or None, data_band_mode=data_band_mode)
        if sp is not None and sp.shifts_profile:
            sp_prof = sp.shifts_profile.upper()
            if sp_prof in profile_scores:
                profile_scores[sp_prof] += 3.8
                evidence.append(f"已选【{nm}】锁定 {sp_prof}")

        for rule in db.combos_for_augment(aid):
            if aid in rule.core_augments:
                h_match = bool(hid and hid in rule.hero_bonus)
                bonus = 3.2 if h_match else 2.0
                if rule.profile in profile_scores:
                    profile_scores[rule.profile] += bonus
                for anti_ax in rule.anti_axes:
                    anti_votes[anti_ax] = anti_votes.get(anti_ax, 0.0) + (2.6 if h_match else 1.7)
                if len(evidence) < 3 and nm not in " ".join(evidence):
                    evidence.append(f"已选【{nm}】触发「{rule.name}」")

    # 2. Live inventory signal (detects AP Miss Fortune / AP Kai'Sa / Enchanter Taric / Tank pivots immediately)
    if store is not None:
        ap_item_w = 0.0
        ad_item_w = 0.0
        tank_item_w = 0.0
        support_item_w = 0.0
        for raw_iid in my_items:
            iid = str(raw_iid).strip()
            if not iid:
                continue
            info = store.get_item_info(iid)
            if not info.name or info.category == "consumable":
                continue
            nid = normalize_item_id(iid)
            stats = info.stats or {}
            tags = set(info.tags or ())
            if info.category == "boots":
                if nid == "3020":
                    ap_item_w += 0.6
                    _bump_axis("AP_SPELL", 0.6)
                elif nid == "3006":
                    ad_item_w += 0.6
                    _bump_axis("AUTO_ONHIT", 0.6)
                continue
            w = 1.8 if (info.category == "complete_equipment" or info.gold >= 2200) else 0.65
            ap_v = float(stats.get("FlatMagicDamageMod", 0.0) or 0.0)
            ad_v = float(stats.get("FlatPhysicalDamageMod", 0.0) or 0.0)
            as_v = float(stats.get("PercentAttackSpeedMod", 0.0) or 0.0)
            crit_v = float(stats.get("FlatCritChanceMod", 0.0) or 0.0)
            armor_v = float(stats.get("FlatArmorMod", 0.0) or 0.0)
            mr_v = float(stats.get("FlatSpellBlockMod", 0.0) or 0.0)
            hp_v = float(stats.get("FlatHPPoolMod", 0.0) or 0.0)

            is_enchanter = (nid in ENCHANTER_SUPPORT_ITEM_IDS) or (
                ("ManaRegen" in tags or "Heal" in tags) and ap_v <= 75 and ad_v <= 0
            )
            if is_enchanter:
                support_item_w += w
                _bump_axis("SHIELD_HEAL", w * 1.1)
                profile_scores["SUPPORT"] += w * 1.25
                if ap_v > 0:
                    profile_scores["AP"] += w * 0.25
                continue

            if ap_v > 0:
                ap_item_w += w
                _bump_axis("AP_SPELL", w)
                profile_scores["AP"] += w * 1.15
            if ad_v > 0 or ("Damage" in tags and ap_v <= 0):
                ad_item_w += w
                _bump_axis("AD_ATTACK", w)
                profile_scores["AD"] += w * 1.15
            if as_v > 0 and ap_v <= 0:
                _bump_axis("AUTO_ONHIT", w * 0.9)
                profile_scores["AD"] += w * 0.45
            if crit_v > 0 and ap_v <= 0:
                _bump_axis("CRIT_ATTACK", w * 0.9)
                profile_scores["AD"] += w * 0.45
            if (armor_v > 0 or mr_v > 0 or hp_v >= 350) and ap_v <= 0 and ad_v <= 0 and "Damage" not in tags:
                tank_item_w += w
                _bump_axis("TANK_HP", w)
                profile_scores["TANK"] += w * 1.1

        if support_item_w >= 1.8 and support_item_w >= max(ap_item_w, ad_item_w, tank_item_w):
            evidence.append("当前已出软辅盾奶装")
        elif ap_item_w >= 1.8 and ap_item_w >= ad_item_w + 1.2:
            evidence.append("当前已出 AP 装备")
        elif ad_item_w >= 1.8 and ad_item_w >= ap_item_w + 1.2:
            evidence.append("当前已出物理装备")
        elif tank_item_w >= 1.8:
            evidence.append("当前已出坦克防御装")

    # 3. Determine primary_profile via unified hero profile (preserving CRIT_SPELL combo specialization)
    ranked_profiles = sorted(profile_scores.items(), key=lambda kv: kv[1], reverse=True)
    top_prof, top_score = ranked_profiles[0]
    second_prof, second_score = ranked_profiles[1]

    has_live_commitment = bool(owned_ids) or bool(my_items)
    if has_manual_pref:
        primary_profile = pref_prof
    elif top_prof == "CRIT_SPELL" and top_score >= 2.6 and top_score >= second_score:
        primary_profile = "CRIT_SPELL"
    elif store is not None and (hid or owned_ids or my_items or stats_dict):
        try:
            primary_profile = infer_unified_hero_profile(
                hid,
                store,
                my_items=my_items,
                selected_augments=owned_ids,
                my_stats=stats_dict,
                data_band_mode=data_band_mode,
                preferred_profile=pref_prof,
            )
        except TypeError:
            primary_profile = infer_unified_hero_profile(
                hid,
                store,
                my_items=my_items,
                selected_augments=owned_ids,
                my_stats=stats_dict,
                data_band_mode=data_band_mode,
            )
    elif top_score < 1.6:
        primary_profile = "BALANCED"
    elif has_hybrid_bridge and top_prof in ("AP", "AD") and second_prof in ("AP", "AD") and second_score >= 1.5:
        primary_profile = "HYBRID"
    else:
        primary_profile = top_prof

    favored_axes = tuple(
        ax for ax, sc in sorted(axis_scores.items(), key=lambda kv: kv[1], reverse=True) if sc >= 1.2
    )[:5]

    penalized: list[str] = []

    def _penalize(ax: str) -> None:
        if ax in VALID_MECHANIC_AXES and ax not in penalized:
            # Never penalize an axis strongly supported by owned augments/items
            if axis_scores.get(ax, 0.0) >= 2.2:
                return
            penalized.append(ax)

    if has_live_commitment or has_manual_pref:
        if primary_profile == "AP" and not has_hybrid_bridge:
            for ax in ("AD_ATTACK", "AUTO_ONHIT", "CRIT_ATTACK"):
                _penalize(ax)
        elif primary_profile == "AD" and not has_hybrid_bridge:
            for ax in ("AP_SPELL", "SHIELD_HEAL"):
                _penalize(ax)
        elif primary_profile == "CRIT_SPELL":
            for ax in ("TANK_HP", "SHIELD_HEAL"):
                _penalize(ax)
        elif primary_profile == "TANK":
            for ax in ("CRIT_ATTACK", "AUTO_ONHIT"):
                _penalize(ax)
            if axis_scores.get("AP_SPELL", 0.0) < 1.2:
                _penalize("AP_SPELL")
        elif primary_profile == "SUPPORT":
            for ax in ("AD_ATTACK", "AUTO_ONHIT", "CRIT_ATTACK"):
                _penalize(ax)

    for ax, vote in sorted(anti_votes.items(), key=lambda kv: kv[1], reverse=True):
        if vote >= 1.7 and not (has_hybrid_bridge and ax in ("AP_SPELL", "AD_ATTACK", "AUTO_ONHIT")):
            _penalize(ax)

    direction_label = _format_direction_label(primary_profile, axis_scores)
    penalized_labels = tuple(AXIS_LABELS[ax] for ax in penalized if ax in AXIS_LABELS)
    return MatchBuildProfile(
        primary_profile=primary_profile,
        direction_label=direction_label,
        favored_axes=favored_axes,
        penalized_axes=tuple(penalized),
        penalized_labels=penalized_labels,
        evidence=tuple(evidence[:3]),
    )


def _format_direction_label(primary_profile: str, axis_scores: dict[str, float]) -> str:
    if primary_profile == "AP":
        if axis_scores.get("HASTE_LOOP", 0.0) >= 2.0:
            return "AP 技能循环流"
        if axis_scores.get("BURN_DOT", 0.0) >= 2.0:
            return "AP 灼烧消耗流"
        return "AP 法术爆发流"
    if primary_profile == "CRIT_SPELL":
        return "技能暴击核爆流"
    if primary_profile == "AD":
        if axis_scores.get("AUTO_ONHIT", 0.0) >= 2.0:
            return "AD 攻速特效流"
        if axis_scores.get("CRIT_ATTACK", 0.0) >= 2.0:
            return "AD 暴击射手流"
        return "物理输出流"
    if primary_profile == "TANK":
        return "重装坦克叠血流"
    if primary_profile == "SUPPORT":
        return "盾奶软辅联动流"
    if primary_profile == "HYBRID":
        return "双修特效混伤流"
    return "待定流派 · 开局博上限"


def _build_progress(
    store: DataStore | None,
    rule: AugmentComboRule,
    owned_set: set[str],
    hero_id: str = "",
) -> ActiveComboProgress:
    owned_in_combo = tuple(aid for aid in (*rule.core_augments, *rule.follow_ups) if aid in owned_set)
    missing_core_ids = tuple(
        aid
        for aid in rule.core_augments
        if aid not in owned_set and _is_aug_eligible(store, hero_id, aid)
    )
    missing_core_names = tuple(_aug_name_rarity(store, aid)[0] for aid in missing_core_ids)
    owned_core_count = sum(1 for aid in rule.core_augments if aid in owned_set)
    has_anchor = (not rule.required_anchor) or (rule.required_anchor in owned_set)
    completed = has_anchor and (
        owned_core_count >= 2
        or (len(rule.core_augments) <= 1 and owned_core_count >= 1 and len(owned_in_combo) >= 2)
    )
    return ActiveComboProgress(
        combo_id=rule.combo_id,
        name=rule.name,
        gamble=rule.gamble,
        owned_ids=owned_in_combo,
        missing_core_ids=missing_core_ids,
        missing_core_names=missing_core_names,
        completed=completed,
        synergy_reason=rule.synergy_reason,
        reroll_hint=rule.reroll_hint,
    )


def _rule_matches_profile(rule_profile: str, primary_profile: str) -> bool:
    if rule_profile == primary_profile:
        return True
    if rule_profile == "CRIT_SPELL" and primary_profile in ("AP", "AD", "HYBRID"):
        return True
    return False


def build_combo_plan(
    store: DataStore | None,
    hero_id: str | None,
    selected_augments: Sequence[str],
    my_items: Sequence[str],
    *,
    limit: int = 4,
    threat: Any | None = None,
    ally_profiles: Sequence[str] = (),
    enemy_profiles: Sequence[str] = (),
    my_stats: dict[str, Any] | Mapping[str, Any] | None = None,
    game_time_s: float = 0.0,
    data_band_mode: str = "all",
    preferred_profile: str = "",
) -> ComboPlan:
    """Build in-game combo roadmap: active combos, pool-guarded comp-aware follow-ups, and avoid list."""
    db = get_augment_combo_db()
    hid = str(hero_id).strip() if hero_id else ""
    use_band = bool(data_band_mode and str(data_band_mode).strip().lower() != "all")
    if store is not None and hid:
        try:
            hero = store.get_hero(hid, band_mode=data_band_mode) if use_band else store.get_hero(hid)
        except TypeError:
            hero = store.get_hero(hid)
    else:
        hero = None
    hero_name = hero.name if hero is not None else "未锁定英雄"
    owned_ids = [str(a).strip() for a in selected_augments if str(a).strip()]
    owned_set = set(owned_ids)
    round_index = min(4, len(owned_ids) + 1)

    profile = infer_match_build_profile(
        store,
        hid or None,
        owned_ids,
        my_items,
        my_stats=my_stats,
        data_band_mode=data_band_mode,
        preferred_profile=preferred_profile,
    )
    penalized_set = set(profile.penalized_axes)
    eff_crit = estimate_effective_crit_chance(
        store,
        hid or None,
        my_items,
        owned_ids,
        my_stats=my_stats,
        game_time_s=game_time_s,
    )
    comp_signals = _extract_comp_signals(threat, ally_profiles, enemy_profiles)

    # Active combos: require >=1 owned core augment; skip uncompletable combos with out-of-pool required anchor
    scored_active: list[tuple[float, AugmentComboRule, ActiveComboProgress]] = []
    for rule in db.entries:
        owned_cores = [aid for aid in rule.core_augments if aid in owned_set]
        owned_fups = [aid for aid in rule.follow_ups if aid in owned_set]
        if not owned_cores:
            continue
        has_anchor = (not rule.required_anchor) or (rule.required_anchor in owned_set)
        if not has_anchor and not _is_aug_eligible(store, hid, rule.required_anchor):
            continue
        h_match = bool(hid and hid in rule.hero_bonus)
        prog = _build_progress(store, rule, owned_set, hero_id=hid)
        rank_score = (
            len(owned_cores) * 10.0
            + len(owned_fups) * 3.0
            + (4.5 if h_match else 0.0)
            + (1.8 if rule.gamble else 0.0)
            + (2.5 if _rule_matches_profile(rule.profile, profile.primary_profile) else 0.0)
            - (12.0 if not has_anchor else 0.0)
        )
        scored_active.append((rank_score, rule, prog))

    scored_active.sort(key=lambda x: x[0], reverse=True)
    active_combos = tuple(p for _, _, p in scored_active[:3])

    # Gate A: Opening hero signature combos (strictly pool-filtered; dynamic top-eligible fallback)
    opening_combos: list[ActiveComboProgress] = []
    if not owned_set:
        hero_rules = [
            r
            for r in (db.combos_for_hero(hid) if hid else [])
            if _rule_is_eligible_for_hero(store, hid, r)
        ]
        hero_rules_sorted = sorted(
            hero_rules,
            key=lambda r: (
                1 if _rule_matches_profile(r.profile, profile.primary_profile) else 0,
                1 if r.gamble else 0,
                len(r.core_augments),
            ),
            reverse=True,
        )
        for rule in hero_rules_sorted[:3]:
            prog = _build_progress(store, rule, owned_set, hero_id=hid)
            if prog.missing_core_ids:
                opening_combos.append(prog)

        if not opening_combos and store is not None and hid:
            get_top_fn = getattr(store, "get_top_eligible_augments", None)
            top_elig = get_top_fn(hid, limit=8) if callable(get_top_fn) else []
            for aid, delta_v, pct_v in top_elig:
                if not _is_aug_eligible(store, hid, aid):
                    continue
                if aid == PURE_STAT_CRIT_AUGMENT_ID and eff_crit >= 0.75:
                    continue
                nm, _rar = _aug_name_rarity(store, aid)
                opening_combos.append(
                    ActiveComboProgress(
                        combo_id=f"data_top_{aid}",
                        name=f"大数据优选 · {nm}",
                        gamble=False,
                        owned_ids=(),
                        missing_core_ids=(aid,),
                        missing_core_names=(nm,),
                        completed=False,
                        synergy_reason=f"本英雄高胜率合法海克斯（{delta_v * 100:+.1f}% · P{int(round(pct_v))}）",
                        reroll_hint=f"开局可优先抓【{nm}】",
                    )
                )
                if len(opening_combos) >= 3:
                    break

    avoid_summary = (
        f"降权避坑：{' · '.join(profile.penalized_labels[:3])}"
        if profile.penalized_labels
        else ""
    )

    # 4/4 full slot clean clear: immediately return empty recommended_targets
    if len(owned_ids) >= 4:
        return ComboPlan(
            hero_id=hid or None,
            hero_name=hero_name,
            profile=profile,
            active_combos=active_combos,
            recommended_targets=(),
            opening_combos=tuple(opening_combos),
            avoid_summary=avoid_summary,
            round_index=round_index,
        )

    # Candidate follow-up augments to chase / save reroll dice for
    best_by_aug: dict[str, ComboFollowUp] = {}

    def _consider_target(
        aid: str,
        combo_id: str,
        combo_name: str,
        base_score: float,
        reason: str,
        is_core: bool,
        *,
        gamble: bool = False,
        hero_sig: bool = False,
    ) -> None:
        if aid in owned_set:
            return
        # Gate B: Authoritative pool filter (handles shared follow_ups cleanly)
        if not _is_aug_eligible(store, hid, aid):
            return
        # P0-5 left-overlay crit-overflow filter
        if aid == PURE_STAT_CRIT_AUGMENT_ID and eff_crit >= 0.75:
            return
        axes = infer_augment_axes(store, aid, hero_id=hid or None, data_band_mode=data_band_mode)
        comp_delta, comp_tag = _compute_comp_adjustment(aid, axes, comp_signals)
        # Skip non-core suggestions that conflict cleanly with locked profile
        if not is_core and axes and penalized_set and set(axes).issubset(penalized_set):
            return
        nm, rar = _aug_name_rarity(store, aid)
        emp_adj, win_delta, tier_label, heavily_demoted = _compute_empirical_calibration(
            store, hid, aid, nm, rar, data_band_mode=data_band_mode
        )
        # Do not introduce heavily demoted dynamic filler targets
        if not is_core and combo_id in ("comp_tactical", "hero_bigdata") and heavily_demoted:
            return
        score = (
            base_score
            + (3.2 if hero_sig else 0.0)
            + (1.6 if gamble else 0.0)
            + _RARITY_PRIORITY.get(rar, 0.3)
            + emp_adj
            + comp_delta
        )
        cand = ComboFollowUp(
            augment_id=aid,
            name=nm,
            rarity=rar,
            combo_id=combo_id,
            combo_name=combo_name,
            reason=reason,
            gamble=gamble,
            priority_score=round(score, 2),
            hero_signature=hero_sig,
            comp_tag=comp_tag,
            win_delta=win_delta,
            tier_label=tier_label,
        )
        prev = best_by_aug.get(aid)
        if prev is None or cand.priority_score > prev.priority_score:
            best_by_aug[aid] = cand

    for _rank, rule, _prog in scored_active[:4]:
        has_anchor = (not rule.required_anchor) or (rule.required_anchor in owned_set)
        owned_core_names = [_aug_name_rarity(store, a)[0] for a in rule.core_augments if a in owned_set]
        anchor_txt = "+".join(owned_core_names[:2]) if owned_core_names else ""
        h_sig = bool(hid and hid in rule.hero_bonus)
        if not has_anchor:
            req_nm = _aug_name_rarity(store, rule.required_anchor)[0]
            _consider_target(
                rule.required_anchor,
                rule.combo_id,
                rule.name,
                base_score=9.5,
                reason=f"补齐关键核心【{req_nm}】激活 · {rule.name}",
                is_core=True,
                gamble=rule.gamble,
                hero_sig=h_sig,
            )
            continue
        core_reason = (
            f"与【{anchor_txt}】核爆成型 · {rule.name}"
            if anchor_txt
            else f"补齐核心成型 · {rule.name}"
        )
        fup_reason = (
            f"强搭配【{anchor_txt}】 · 放大{rule.name}"
            if anchor_txt
            else f"强适配 · 放大{rule.name}"
        )
        for aid in rule.core_augments:
            _consider_target(
                aid,
                rule.combo_id,
                rule.name,
                base_score=11.0,
                reason=core_reason,
                is_core=True,
                gamble=rule.gamble,
                hero_sig=h_sig,
            )
        for idx, aid in enumerate(rule.follow_ups[:6]):
            _consider_target(
                aid,
                rule.combo_id,
                rule.name,
                base_score=6.8 - idx * 0.25,
                reason=fup_reason,
                is_core=False,
                gamble=rule.gamble,
                hero_sig=h_sig,
            )

    if not best_by_aug:
        # Round 1 opening targets from pool-legal hero signature or data-driven opening combos
        for prog in opening_combos:
            rule = db.get_by_id(prog.combo_id)
            if rule is not None:
                h_sig = bool(hid and hid in rule.hero_bonus)
                elig_cores = [a for a in rule.core_augments if _is_aug_eligible(store, hid, a)]
                for idx, aid in enumerate(elig_cores[:2]):
                    partner_ids = [x for x in elig_cores if x != aid]
                    partner_nm = _aug_name_rarity(store, partner_ids[0])[0] if partner_ids else ""
                    reason = (
                        f"开局可赌「{rule.name}」（配{partner_nm}）"
                        if (rule.gamble and partner_nm)
                        else f"开局优质核心 · {rule.name}"
                    )
                    _consider_target(
                        aid,
                        rule.combo_id,
                        rule.name,
                        base_score=8.0 - idx * 0.5,
                        reason=reason,
                        is_core=True,
                        gamble=rule.gamble,
                        hero_sig=h_sig,
                    )
            else:
                for idx, aid in enumerate(prog.missing_core_ids[:2]):
                    _consider_target(
                        aid,
                        prog.combo_id,
                        prog.name,
                        base_score=7.5 - idx * 0.4,
                        reason=prog.synergy_reason or f"开局优选 · {prog.name}",
                        is_core=False,
                        gamble=False,
                        hero_sig=False,
                    )

    # P0-7: Comp-aware & big-data eligible pool enrichment (0ms local re-ranking)
    if store is not None and hid and (comp_signals.active or len(best_by_aug) < max(1, limit)):
        get_top_fn = getattr(store, "get_top_eligible_augments", None)
        top_elig = get_top_fn(hid, limit=24) if callable(get_top_fn) else []
        candidate_ids: list[str] = [aid for aid, _d, _p in top_elig]
        if comp_signals.active:
            tactical_pool: list[str] = []
            if comp_signals.is_multi_tank:
                tactical_pool.extend(sorted(_COMP_ANTI_TANK_IDS))
            if comp_signals.is_dive_or_fragile:
                tactical_pool.extend(sorted(_COMP_SURVIVAL_IDS))
            if comp_signals.is_squishy_poke:
                tactical_pool.extend(sorted(_COMP_POKE_SQUISHY_IDS))
            if comp_signals.is_high_heal:
                tactical_pool.extend(sorted(_COMP_HIGH_HEAL_IDS))
            seen_c = set(candidate_ids)
            for aid in tactical_pool:
                if aid not in seen_c and _is_aug_eligible(store, hid, aid):
                    seen_c.add(aid)
                    candidate_ids.append(aid)

        for aid in candidate_ids:
            if aid in owned_set or not _is_aug_eligible(store, hid, aid):
                continue
            if aid == PURE_STAT_CRIT_AUGMENT_ID and eff_crit >= 0.75:
                continue
            axes = infer_augment_axes(store, aid, hero_id=hid, data_band_mode=data_band_mode)
            comp_delta, comp_tag = _compute_comp_adjustment(aid, axes, comp_signals)
            if comp_signals.active and comp_delta >= 1.2:
                nm, rar = _aug_name_rarity(store, aid)
                _emp, w_d, t_lbl, heavily_dem = _compute_empirical_calibration(
                    store, hid, aid, nm, rar, data_band_mode=data_band_mode
                )
                if heavily_dem:
                    continue
                reason_txt = (
                    f"{comp_tag} · 本英雄实测 {w_d * 100:+.1f}% ({t_lbl})"
                    if t_lbl
                    else f"{comp_tag} · 阵容精准对策"
                )
                _consider_target(
                    aid,
                    "comp_tactical",
                    f"阵容对策·{comp_tag}",
                    base_score=8.2,
                    reason=reason_txt,
                    is_core=False,
                    gamble=False,
                    hero_sig=False,
                )
            elif len(best_by_aug) < max(1, limit):
                nm, rar = _aug_name_rarity(store, aid)
                _emp, w_d, t_lbl, heavily_dem = _compute_empirical_calibration(
                    store, hid, aid, nm, rar, data_band_mode=data_band_mode
                )
                if heavily_dem:
                    continue
                reason_txt = (
                    f"本英雄大数据强势 {w_d * 100:+.1f}% ({t_lbl})"
                    if t_lbl
                    else "本英雄合法池优选"
                )
                _consider_target(
                    aid,
                    "hero_bigdata",
                    "大数据优选",
                    base_score=5.8,
                    reason=reason_txt,
                    is_core=False,
                    gamble=False,
                    hero_sig=False,
                )

    recommended_targets = tuple(
        sorted(best_by_aug.values(), key=lambda t: t.priority_score, reverse=True)[: max(1, limit)]
    )

    return ComboPlan(
        hero_id=hid or None,
        hero_name=hero_name,
        profile=profile,
        active_combos=active_combos,
        recommended_targets=recommended_targets,
        opening_combos=tuple(opening_combos),
        avoid_summary=avoid_summary,
        round_index=round_index,
    )


def evaluate_picker_combos(
    store: DataStore | None,
    hero_id: str | None,
    selected_augments: Sequence[str],
    my_items: Sequence[str],
    offered: Sequence[tuple[int, AugmentInfo | None]],
    *,
    my_stats: dict[str, Any] | Mapping[str, Any] | None = None,
    game_time_s: float = 0.0,
    data_band_mode: str = "all",
    preferred_profile: str = "",
    my_level: int = 0,
    my_gold: float = 0.0,
    round_index: int = 0,
) -> PickerComboVerdict:
    """Evaluate 3 live offered augments strictly on combo synergy, gamble upside, and conflict downgrade."""
    db = get_augment_combo_db()
    hid = str(hero_id).strip() if hero_id else ""
    owned_ids = [str(a).strip() for a in selected_augments if str(a).strip()]
    owned_set = set(owned_ids)
    eff_round = effective_augment_round(len(owned_ids), my_level, round_index)

    profile = infer_match_build_profile(
        store,
        hid or None,
        owned_ids,
        my_items,
        my_stats=my_stats,
        data_band_mode=data_band_mode,
        preferred_profile=preferred_profile,
    )
    favored_set = set(profile.favored_axes)
    penalized_set = set(profile.penalized_axes)
    eff_crit = estimate_effective_crit_chance(
        store,
        hid or None,
        my_items,
        owned_ids,
        my_stats=my_stats,
        game_time_s=game_time_s,
    )

    active_rules: list[AugmentComboRule] = [
        r for r in db.entries if any(a in owned_set for a in r.core_augments)
    ]

    def _eval_base_card_advice(
        slot_index: int,
        aug: AugmentInfo,
        cand_axes: tuple[str, ...],
    ) -> PickerCardComboAdvice:
        aid = str(aug.augment_id)
        nm = aug.name
        rar = aug.rarity

        # 0b. Pure crit-stat overflow guard (BEFORE jackpot): ONLY targets pure stat card 1047;
        #     strictly exempts hybrid/mechanic crit cards (1048/1092/1077/1080/1118/1328/3123).
        if aid == PURE_STAT_CRIT_AUGMENT_ID:
            wasted_ratio = max(0.0, min(1.0, (eff_crit + 0.50 - 1.0) / 0.50))
            if wasted_ratio > 0.10:
                crit_pct = int(round(eff_crit * 100))
                if UPGRADE_IE_AUGMENT_ID in owned_set and eff_crit >= 0.99:
                    v_lbl = "满暴递减"
                    c_delta = -4.5
                    c_reason = f"当前暴击{crit_pct}%已满，持有升级无尽可溢转但收益递减"
                elif hid in DOUBLE_CRIT_HERO_IDS and eff_crit >= 0.99:
                    v_lbl = "满暴溢转·偏弱"
                    c_delta = -14.0
                    c_reason = f"当前暴击{crit_pct}%已满，溢转攻击力收益远低于机制金卡"
                elif hid == SENNA_HERO_ID and eff_crit >= 0.99:
                    v_lbl = "暴击溢转·低效"
                    c_delta = -18.0
                    c_reason = f"当前暴击{crit_pct}%已满，赛娜溢转吸血收益低效"
                elif eff_crit >= 0.99:
                    v_lbl = "暴击溢出·废卡"
                    c_delta = -35.0
                    c_reason = f"当前暴击{crit_pct}%已满，纯暴击白板属性100%溢出"
                else:
                    v_lbl = "暴击半溢·折价"
                    c_delta = round(-18.0 * wasted_ratio, 1)
                    wasted_pct = int(round(wasted_ratio * 100))
                    c_reason = f"当前暴击{crit_pct}%，拿关键暴击将浪费{wasted_pct}%属性"
                return PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="conflict",
                    verdict_label=v_lbl,
                    combo_delta=c_delta,
                    reason=c_reason,
                    combo_name="",
                    gamble=False,
                )

        # 1. Check JACKPOT: completes/amplifies core of an active owned combo
        best_jackpot: tuple[float, AugmentComboRule, str] | None = None
        for rule in active_rules:
            if aid in rule.core_augments and aid not in owned_set:
                if rule.required_anchor and rule.required_anchor not in owned_set and aid != rule.required_anchor:
                    continue
                owned_cores = [_aug_name_rarity(store, a)[0] for a in rule.core_augments if a in owned_set]
                h_match = bool(hid and hid in rule.hero_bonus)
                delta = 9.5 + (2.0 if h_match else 0.0) + (1.0 if rule.gamble else 0.0)
                reason = f"与已选【{'+'.join(owned_cores[:2])}】核爆成型 · {rule.name}"
                if best_jackpot is None or delta > best_jackpot[0]:
                    best_jackpot = (delta, rule, reason)

        if best_jackpot is not None:
            delta, rule, reason = best_jackpot
            if eff_round >= 4:
                delta += 0.8
            v_lbl = "收官成型" if eff_round >= 4 else ("核爆绝配" if rule.gamble else "核心成型")
            return PickerCardComboAdvice(
                slot_index=slot_index,
                augment_id=aid,
                name=nm,
                rarity=rar,
                verdict="jackpot",
                verdict_label=v_lbl,
                combo_delta=round(delta, 1),
                reason=reason,
                combo_name=rule.name,
                gamble=rule.gamble,
            )

        # 2. Check active combo follow-up synergy
        best_followup: tuple[float, AugmentComboRule, str] | None = None
        for rule in active_rules:
            if aid in rule.follow_ups and aid not in owned_set:
                if rule.required_anchor and rule.required_anchor not in owned_set:
                    continue
                owned_cores = [_aug_name_rarity(store, a)[0] for a in rule.core_augments if a in owned_set]
                h_match = bool(hid and hid in rule.hero_bonus)
                delta = 6.2 + (1.3 if h_match else 0.0) + (0.6 if rule.gamble else 0.0)
                reason = f"强适配【{'+'.join(owned_cores[:2])}】 · {rule.name}"
                if best_followup is None or delta > best_followup[0]:
                    best_followup = (delta, rule, reason)

        if best_followup is not None:
            delta, rule, reason = best_followup
            return PickerCardComboAdvice(
                slot_index=slot_index,
                augment_id=aid,
                name=nm,
                rarity=rar,
                verdict="synergy",
                verdict_label="组合顺滑",
                combo_delta=round(delta, 1),
                reason=reason,
                combo_name=rule.name,
                gamble=rule.gamble,
            )

        # 3. Check ANTI-SYNERGY CONFLICT (ignore non-combat meta axes like GAMBLE_HIGHROLL/MOBILITY_DASH!)
        penalized_hits = [ax for ax in cand_axes if ax in penalized_set]
        favored_hits = [ax for ax in cand_axes if ax in favored_set and ax not in penalized_set]
        combat_favored_hits = [ax for ax in favored_hits if ax not in _META_NON_COMBAT_AXES]
        if penalized_hits and not combat_favored_hits and profile.primary_profile != "BALANCED":
            penalty = -4.5 - min(2.5, (len(penalized_hits) - 1) * 1.5)
            hit_lbl = "/".join(AXIS_LABELS.get(ax, ax) for ax in penalized_hits[:2])
            return PickerCardComboAdvice(
                slot_index=slot_index,
                augment_id=aid,
                name=nm,
                rarity=rar,
                verdict="conflict",
                verdict_label="流派冲突·降权",
                combo_delta=round(penalty, 1),
                reason=f"当前{profile.direction_label}，{hit_lbl}系降权",
                combo_name="",
                gamble=False,
            )

        # 4. Round-aware HIGH-CEILING GAMBLE OPENER
        if eff_round <= 3 and aid in INSTANT_PRISMATIC_TRANSMUTE_IDS:
            return PickerCardComboAdvice(
                slot_index=slot_index,
                augment_id=aid,
                name=nm,
                rarity=rar,
                verdict="gamble",
                verdict_label="保底棱彩·高上限",
                combo_delta=4.2,
                reason=f"第{eff_round}轮单卡即时质变 · 保底棱彩阶高上限（不依赖多卡凑齐）",
                combo_name="单卡质变：棱彩阶",
                gamble=True,
            )

        best_gamble: tuple[float, AugmentComboRule, str] | None = None
        if eff_round <= 3:
            for rule in db.combos_for_augment(aid):
                if aid not in rule.core_augments or not rule.gamble:
                    continue
                if profile.primary_profile in ("AP", "AD", "TANK", "SUPPORT") and rule.profile in (
                    "AP",
                    "AD",
                    "TANK",
                    "SUPPORT",
                ) and rule.profile != profile.primary_profile:
                    continue
                h_match = bool(hid and hid in rule.hero_bonus)
                if not h_match and rule.combo_id != "gacha_transmute_highroll":
                    continue
                if (
                    rule.required_anchor
                    and rule.required_anchor != aid
                    and rule.required_anchor not in owned_set
                    and not _is_aug_eligible(store, hid, rule.required_anchor)
                ):
                    continue
                missing_core_ids = [
                    x
                    for x in rule.core_augments
                    if x != aid and x not in owned_set and _is_aug_eligible(store, hid, x)
                ]
                if (
                    len(rule.core_augments) >= 2
                    and not missing_core_ids
                    and not any(x in owned_set for x in rule.core_augments if x != aid)
                ):
                    continue
                if eff_round == 3 and (not h_match or len(missing_core_ids) > 1):
                    continue
                partners = [_aug_name_rarity(store, x)[0] for x in missing_core_ids]
                partner_txt = "/".join(partners[:2])
                if eff_round == 1:
                    delta = 7.6 if h_match else 4.8
                    prefix = f"第1轮高上限可赌「{rule.name}」"
                elif eff_round == 2:
                    delta = 6.8 if h_match else 4.2
                    prefix = f"高上限可赌「{rule.name}」"
                else:
                    delta = 4.2
                    prefix = f"第3轮末班车可赌「{rule.name}」"
                reason = f"{prefix} · 后续追【{partner_txt}】" if partner_txt else prefix
                if best_gamble is None or delta > best_gamble[0]:
                    best_gamble = (delta, rule, reason)

        if best_gamble is not None:
            delta, rule, reason = best_gamble
            return PickerCardComboAdvice(
                slot_index=slot_index,
                augment_id=aid,
                name=nm,
                rarity=rar,
                verdict="gamble",
                verdict_label="高上限可赌" if eff_round <= 2 else "末班可赌",
                combo_delta=round(delta, 1),
                reason=reason,
                combo_name=rule.name,
                gamble=True,
            )

        # 4b. Round 4 finale anti-gamble guard: warn against unstarted multi-piece gamble seeds!
        if eff_round >= 4 and aid not in INSTANT_PRISMATIC_TRANSMUTE_IDS:
            unstarted_gamble = next(
                (
                    r
                    for r in db.combos_for_augment(aid)
                    if r.gamble
                    and aid in r.core_augments
                    and len(r.core_augments) >= 2
                    and not any(c in owned_set for c in r.core_augments)
                ),
                None,
            )
            if unstarted_gamble is not None and not combat_favored_hits:
                return PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="neutral",
                    verdict_label="末轮慎赌",
                    combo_delta=-1.5,
                    reason=f"第4轮收官无后续海克斯 · 难凑齐「{unstarted_gamble.name}」，优先即时战力",
                    combo_name="",
                    gamble=False,
                )

        # 5. Check same-axis positive alignment
        if favored_hits and not penalized_hits:
            hit_lbl = "/".join(AXIS_LABELS.get(ax, ax) for ax in favored_hits[:2])
            delta = 3.2 + min(1.5, (len(favored_hits) - 1) * 0.9)
            return PickerCardComboAdvice(
                slot_index=slot_index,
                augment_id=aid,
                name=nm,
                rarity=rar,
                verdict="synergy",
                verdict_label="流派顺滑",
                combo_delta=round(delta, 1),
                reason=f"契合当前{profile.direction_label}（{hit_lbl}）",
                combo_name="",
                gamble=False,
            )

        # 6. Check opening non-gamble hero signature core at 0 augments
        if not owned_set and hid:
            sig_rule = next(
                (
                    r
                    for r in db.combos_for_augment(aid)
                    if aid in r.core_augments and hid in r.hero_bonus and _rule_is_eligible_for_hero(store, hid, r)
                ),
                None,
            )
            if sig_rule is not None:
                partners = [
                    _aug_name_rarity(store, x)[0]
                    for x in sig_rule.core_augments
                    if x != aid and _is_aug_eligible(store, hid, x)
                ]
                p_txt = "/".join(partners[:2])
                return PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="synergy",
                    verdict_label="招牌起手",
                    combo_delta=5.0,
                    reason=f"适合开「{sig_rule.name}」· 后续配【{p_txt}】" if p_txt else f"适合开「{sig_rule.name}」",
                    combo_name=sig_rule.name,
                    gamble=sig_rule.gamble,
                )

        # 7. Neutral fallback
        return PickerCardComboAdvice(
            slot_index=slot_index,
            augment_id=aid,
            name=nm,
            rarity=rar,
            verdict="neutral",
            verdict_label="常规",
            combo_delta=0.0,
            reason="与当前流派无明显强联动或冲突，可参考上方胜率卡",
            combo_name="",
            gamble=False,
        )

    advices: list[PickerCardComboAdvice] = []
    for slot_index, aug in offered:
        if aug is None:
            continue
        aid = str(aug.augment_id)
        nm = aug.name
        rar = aug.rarity
        cand_axes = infer_augment_axes(store, aid, hero_id=hid or None, data_band_mode=data_band_mode)
        decay_res = evaluate_augment_round_decay(
            store,
            hid,
            aid,
            my_items,
            owned_ids,
            round_index=eff_round,
            game_time_s=game_time_s,
            my_gold=my_gold,
            my_level=my_level,
            my_stats=my_stats,
            primary_profile=profile.primary_profile,
        )
        if decay_res is not None and decay_res[0] <= -8.0:
            d_val, d_lbl, d_reason = decay_res
            advices.append(
                PickerCardComboAdvice(
                    slot_index=slot_index,
                    augment_id=aid,
                    name=nm,
                    rarity=rar,
                    verdict="round_decay",
                    verdict_label=d_lbl,
                    combo_delta=round(d_val, 1),
                    reason=d_reason,
                    combo_name="",
                    gamble=False,
                )
            )
            continue

        base_advice = _eval_base_card_advice(slot_index, aug, cand_axes)
        if decay_res is not None:
            d_val, d_lbl, d_reason = decay_res
            if base_advice.verdict in ("jackpot", "synergy") and base_advice.combo_delta > 0:
                net_delta = round(d_val + min(3.5, base_advice.combo_delta * 0.4), 1)
                if net_delta < 0.0:
                    advices.append(
                        PickerCardComboAdvice(
                            slot_index=slot_index,
                            augment_id=aid,
                            name=nm,
                            rarity=rar,
                            verdict="round_decay",
                            verdict_label=d_lbl,
                            combo_delta=net_delta,
                            reason=f"{d_reason}（含原联动抵扣）",
                            combo_name="",
                            gamble=False,
                        )
                    )
                else:
                    advices.append(
                        PickerCardComboAdvice(
                            slot_index=slot_index,
                            augment_id=aid,
                            name=nm,
                            rarity=rar,
                            verdict="synergy",
                            verdict_label="联动折价",
                            combo_delta=net_delta,
                            reason=f"{base_advice.reason}（{d_lbl}已折算）",
                            combo_name=base_advice.combo_name,
                            gamble=base_advice.gamble,
                        )
                    )
                continue
            elif base_advice.verdict == "conflict":
                if d_val < base_advice.combo_delta:
                    advices.append(
                        PickerCardComboAdvice(
                            slot_index=slot_index,
                            augment_id=aid,
                            name=nm,
                            rarity=rar,
                            verdict="round_decay",
                            verdict_label=d_lbl,
                            combo_delta=round(d_val, 1),
                            reason=d_reason,
                            combo_name="",
                            gamble=False,
                        )
                    )
                    continue
            else:
                advices.append(
                    PickerCardComboAdvice(
                        slot_index=slot_index,
                        augment_id=aid,
                        name=nm,
                        rarity=rar,
                        verdict="round_decay",
                        verdict_label=d_lbl,
                        combo_delta=round(d_val, 1),
                        reason=d_reason,
                        combo_name="",
                        gamble=False,
                    )
                )
                continue

        advices.append(base_advice)

    best_slot = -1
    best_delta = 2.99
    for item in advices:
        if item.combo_delta > best_delta:
            best_delta = item.combo_delta
            best_slot = item.slot_index

    reroll_advice = ""
    if best_slot < 0 and active_rules:
        # Gate C: Only chase pool-eligible unowned cores; never advise holding dice for unreachable augments
        eligible_chase_rules = [
            r
            for r in active_rules
            if (not r.required_anchor or r.required_anchor in owned_set or _is_aug_eligible(store, hid, r.required_anchor))
            and any(a not in owned_set and _is_aug_eligible(store, hid, a) for a in r.core_augments)
        ]
        if eligible_chase_rules:
            ranked_chase = sorted(
                eligible_chase_rules,
                key=lambda r: (
                    1 if (hid and hid in r.hero_bonus) else 0,
                    1 if r.gamble else 0,
                ),
                reverse=True,
            )
            top_chase = ranked_chase[0]
            missing = [
                _aug_name_rarity(store, a)[0]
                for a in top_chase.core_augments
                if a not in owned_set and _is_aug_eligible(store, hid, a)
            ]
            if missing:
                reroll_advice = f"本轮无强联动 · 可用骰子追【{' / '.join(missing[:2])}】（{top_chase.name}）"
            elif top_chase.reroll_hint:
                reroll_advice = f"本轮无强联动 · {top_chase.reroll_hint}"

    return PickerComboVerdict(
        profile=profile,
        cards=tuple(advices),
        best_slot_index=best_slot,
        reroll_advice=reroll_advice,
        round_index=eff_round,
    )


def export_combo_coach_context(
    plan: ComboPlan,
    picker_verdict: PickerComboVerdict | None = None,
) -> dict[str, Any]:
    """Serialize compact combo & anti-synergy summary for zero-extra-cost AI Coach prompt injection."""
    payload: dict[str, Any] = {
        "round_index": picker_verdict.round_index if picker_verdict is not None else plan.round_index,
        "direction": plan.profile.direction_label,
        "primary_profile": plan.profile.primary_profile,
        "penalized_axes": list(plan.profile.penalized_labels),
        "active_combos": [
            {
                "name": c.name,
                "gamble": c.gamble,
                "completed": c.completed,
                "chase": list(c.missing_core_names[:2]),
            }
            for c in plan.active_combos[:2]
        ],
        "top_targets": [
            {
                "id": t.augment_id,
                "name": t.name,
                "rarity": t.rarity,
                "tier": t.tier_label,
                "delta": round(t.win_delta, 4),
                "comp_tag": t.comp_tag,
                "combo": t.combo_name,
                "gamble": t.gamble,
            }
            for t in plan.recommended_targets[:4]
        ],
    }
    if picker_verdict is not None and picker_verdict.cards:
        payload["picker_verdicts"] = [
            {
                "slot": c.slot_index,
                "name": c.name,
                "verdict": c.verdict_label,
                "delta": c.combo_delta,
                "reason": c.reason,
            }
            for c in picker_verdict.cards
        ]
        if picker_verdict.reroll_advice:
            payload["reroll_advice"] = picker_verdict.reroll_advice
    return payload
