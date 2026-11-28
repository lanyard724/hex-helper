"""Direct Gemini 4 API Coach using the official google-genai Interactions SDK."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from hex_helper.bridge.protocol import (
    CoachAdviceItem,
    CoachAdvicePayload,
    CoachRequestPayload,
    normalize_opening_brief,
    utc_now_iso,
)
from hex_helper.config import resolve_thinking_level
from hex_helper.data.store import DataStore
from hex_helper.scoring.augment_combo import estimate_effective_crit_chance
from hex_helper.scoring.build_synergy import item_is_owned, normalize_item_id
from hex_helper.scoring.matchup_optimizer import (
    ELIXIR_IDS,
    GRIEVOUS_WOUNDS_ITEMS,
    HEAL_ITEM_IDS,
    HIGH_HEAL_HEROES,
    HIGH_SHIELD_HEROES,
    PROFILE_COUNTER_ITEMS,
    SHIELD_BREAK_ITEMS,
    SHIELD_ITEM_IDS,
    TIER2_BOOT_IDS,
    _classify_items_profile,
    _infer_hero_profile,
    _is_item_off_active_profile,
    _score_item_archetype,
    analyze_inventory_slots,
    canonicalize_item_id,
    get_hero_build_templates,
    infer_unified_hero_profile,
    is_valid_plan_item,
    resolve_item_id_by_name,
)

logger = logging.getLogger(__name__)

# Common CN item nicknames -> official DDragon 26.23 names so LLM shorthand grounds cleanly
_ITEM_NICKNAME_ALIASES: dict[str, str] = {
    "大面具": "兰德里的折磨",
    "面具": "兰德里的折磨",
    "兰德里": "兰德里的折磨",
    "兰德里的苦楚": "兰德里的折磨",
    "鬼书": "莫雷洛秘典",
    "大帽": "灭世者的死亡之帽",
    "帽子": "灭世者的死亡之帽",
    "法穿棒": "虚空之杖",
    "中娅": "中娅沙漏",
    "沙漏": "中娅沙漏",
    "金身": "中娅沙漏",
    "火炬": "黯炎火炬",
    "大天使": "大天使之杖",
    "炽天使": "炽天使之拥",
    "冰杖": "瑞莱的冰晶节杖",
    "反甲": "荆棘之甲",
    "狂徒": "狂徒铠甲",
    "绿甲": "振奋盔甲",
    "冰心": "冰霜之心",
    "无尽": "无尽之刃",
    "破败": "破败王者之刃",
    "飓风": "卢安娜的飓风",
    "轻语": "多米尼克领主的致意",
    "重伤弓": "凡性的提醒",
    "巨九": "巨型九头蛇",
    "贪九": "贪欲九头蛇",
    "血手": "斯特拉克的挑战护手",
    "复活甲": "守护天使",
    "黑切": "黑色切割者",
    "三项": "三相之力",
    "饮魔刀": "玛莫提乌斯之噬",
    "青龙刀": "朔极之矛",
    "推推棒": "海克斯科技火箭腰带",
    "时光": "时光之杖",
    "女妖": "女妖面纱",
    "星界": "星界驱驰",
    "卢登": "卢登的伙伴",
    "巫妖": "巫妖之祸",
    "纳什": "纳什之牙",
    "羊刀": "鬼索的狂暴之刃",
    "海妖": "海妖杀手",
    "电刀": "斯塔缇克电刃",
    "吸蓝刀": "夺萃之镰",
    "饮血": "饮血剑",
    "巨蛇": "巨蛇之牙",
    "兰顿": "兰顿之兆",
    "日炎": "日炎圣盾",
    "无终": "无终恨意",
    "深渊": "深渊面具",
    "千变者": "千变者贾修",
    "水银鞋": "水银之靴",
    "铁板鞋": "铁板靴",
    "布甲鞋": "铁板靴",
    "法穿鞋": "法师之靴",
    "攻速鞋": "狂战士胫甲",
    "明朗鞋": "明朗之靴",
    "CD鞋": "明朗之靴",
    "轻灵鞋": "轻灵之靴",
    "三速鞋": "轻灵之靴",
}

# Situational counter & boot anchor items guaranteed to be in reference glossary
_CORE_COUNTER_GLOSSARY_IDS: tuple[str, ...] = (
    "3033",  # 凡性的提醒 (AD重伤+穿甲)
    "3165",  # 莫雷洛秘典 (AP重伤)
    "6609",  # 炼金朋克链锯剑 (战士重伤)
    "3075",  # 荆棘之甲 (重装护甲+重伤)
    "6695",  # 巨蛇之牙 (破盾)
    "2504",  # 败魔 (坦对AP神装)
    "4401",  # 自然之力 (移速魔抗)
    "3156",  # 玛莫提乌斯之噬 (AD饮魔刀)
    "3157",  # 中娅沙漏 (AP金身)
    "3102",  # 女妖面纱 (AP抗爆发)
    "3026",  # 守护天使 (复活甲)
    "3139",  # 水银弯刀 (AD解控)
    "3036",  # 多米尼克领主的致意 (打坦穿甲)
    "3135",  # 虚空之杖 (AP法穿)
    "3089",  # 灭世者的死亡之帽
    "3084",  # 心之钢
    "3083",  # 狂徒铠甲
    "3065",  # 振奋盔甲
    "2502",  # 无终恨意
    "2501",  # 霸王血铠
    "3111",  # 水银之靴
    "3047",  # 铁板靴
    "3020",  # 法师之靴
    "3006",  # 狂战士胫甲
    "3158",  # 明朗之靴
    "3009",  # 轻灵之靴
    "2139",  # 巫术合剂 (AP/软辅满格)
    "2140",  # 愤怒合剂 (AD/战士满格)
    "2138",  # 钢铁合剂 (坦克满格)
)

# Profile-scoped Tier-2 boots (included in high-SNR glossary only when player has no Tier-2 boots)
_PROFILE_BOOTS_IDS: dict[str, tuple[str, ...]] = {
    "AD": ("3006", "3047", "3111", "3158"),
    "AP": ("3020", "3158", "3111", "3047"),
    "TANK": ("3111", "3047", "3009"),
    "SUPPORT": ("3158", "3111", "3009", "3020"),
    "HYBRID": ("3006", "3020", "3111", "3047", "3158"),
}
_CROSS_PROFILE_FORBIDDEN_IDS: dict[str, frozenset[str]] = {
    "TANK": frozenset({"3165", "3089", "3135", "3031", "3033", "3036", "4645", "6655", "3137", "3916", "3123"}),
    "AD": frozenset({"3165", "3089", "3135", "3137", "3916", "4645", "6655"}),
    "AP": frozenset({"3031", "3033", "3036", "6695", "3123", "3156", "3026", "3139"}),
    "SUPPORT": frozenset({"3031", "3033", "3036", "6695", "3123", "3156", "3026", "3139"}),
    "HYBRID": frozenset(),
}
_RARITY_SHORT_CN: dict[str, str] = {
    "prismatic": "彩",
    "gold": "金",
    "silver": "银",
}
_DEFENSIVE_ENEMY_SPELLS: frozenset[str] = frozenset(
    {"虚弱", "净化", "屏障", "治疗术", "Exhaust", "Cleanse", "Barrier", "Heal"}
)
_BRUISER_AD_ITEM_IDS: frozenset[str] = frozenset(
    {"3071", "3053", "6610", "6333", "3748", "6631", "3078", "3161", "2517", "3074", "6692", "6609"}
)
_GW_ITEM_IDS_ALL: frozenset[str] = GRIEVOUS_WOUNDS_ITEMS | frozenset({"3076"})
_ROLE_ORDER: tuple[str, ...] = ("坦", "战", "AP", "AD", "辅", "混")
_LOBBY_SCOUT_SLIM_KEYS: tuple[str, ...] = (
    "top_enemy_champion",
    "top_enemy_rating",
    "top_enemy_tag",
    "enemy_premades",
    "ally_mvp_champion",
    "ally_mvp_rating",
)

COACH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "augment_pick_advice": {"type": "string"},
        "next_augment_goal": {"type": "string"},
        "buy_now": {"type": "string"},
        "build_path": {
            "type": "array",
            "items": {"type": "string"},
        },
        "next_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "name": {"type": "string"},
                    "priority": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["item_id", "name", "priority", "reason"],
            },
        },
        "tactical_summary": {"type": "string"},
        "opening_brief": {
            "type": "object",
            "properties": {
                "enemy_threat": {"type": "string"},
                "augment_goal": {"type": "string"},
                "opening_buy": {"type": "string"},
            },
        },
    },
    "required": [
        "augment_pick_advice",
        "buy_now",
        "build_path",
        "next_items",
        "tactical_summary",
    ],
}

COACH_SYSTEM_INSTRUCTION = """LOL海克斯大乱斗(26.23)狭长窗参谋。思考直切核心，禁复述或冗长自检。

【铁律】
1.零脑补：仅据selected_augments联动，禁假设已选上轮推荐。
2a.选卡：仅offered_augments非空填augment_pick_advice(<=20字,据win_rate/delta/流派)，否则""。
2b.下轮追卡：非选卡且selected_augments<4且next_aug_pool非空填next_augment_goal(<=18字,严禁脱离next_aug_pool编造)；满4槽/选卡填""。
3.路线：守committed_build_plan及selected_archetype，禁推owned_complete；allow_overhaul=false禁改locked_next_item，true按新海克斯或流派重构。
4.特殊流派：special_augment_builds非空扣force/core避avoid。
5.大数据：主依hero_big_data_top_builds/local_engine_top_items/comp_summary；敌高回复/厚盾/极肥补重伤/破盾/抗性/保命。
6.控字ID：item_id/name守输入或item_glossary("名|金",4位)；next_items[0]("下件必出",reason<=14字),[1]("次选")；buy_now<=24字结合gold散件；build_path仅未出大件名(2-5件,禁鞋散件)；tactical_summary<=28字纯团战(禁装备金币)。
7.满格：inventory_status为full_6_complete/full_5_plus_boots禁推第7件，默认合剂(2139巫术/2140愤怒/2138钢铁,priority="满格合剂")、build_path=[]；刚需换装(priority="必须换装",reason="卖X换Y+理由")。
8.开局：仅trigger_reason=="game_start"填opening_brief(enemy_threat<=22字结合lobby_scout/comp_summary,augment_goal<=22字,opening_buy<=24字1400G出门)；否则{}。"""

PICKER_FAST_SYSTEM_INSTRUCTION = """你是LOL海克斯大乱斗(26.23)极速选卡参谋。思考直切核心，禁冗长自检，仅据输入快速给出海克斯三/四选一结论，禁复述输入。
要求：
1.augment_pick_advice<=20字：结合offered_augments的win_rate/delta/combo_label、selected_augments、crit_chance_pct及comp_summary/threat_summary直给最优卡与简短对比理由。
2.next_augment_goal填""。
3.buy_now<=18字：仅结合金币与committed_build_plan一句话提示下步。
4.build_path：仅填未出成装大件名(严禁鞋子与散件)，无变动填[]。
5.next_items：无需变阵可填[]。
6.tactical_summary<=22字：1句当前流派团战要点，禁提装备金币。"""


@dataclass(frozen=True)
class GeminiCallStats:
    model: str
    thinking_level: str
    latency_ms: int
    total_tokens: int = 0
    thought_tokens: int = 0
    output_tokens: int = 0
    grounding_stats: dict[str, int] = field(default_factory=dict, hash=False)
    input_tokens: int = 0


def _strip_json_fences(raw_text: str) -> str:
    txt = (raw_text or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
        txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def _extract_interaction_text(interaction: Any) -> str:
    out_txt = getattr(interaction, "output_text", None)
    if isinstance(out_txt, str) and out_txt.strip():
        return out_txt
    chunks: list[str] = []
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", "") == "model_output":
            for blk in getattr(step, "content", None) or []:
                if getattr(blk, "type", "") == "text" and isinstance(getattr(blk, "text", None), str):
                    chunks.append(blk.text)
    return "".join(chunks)


def _extract_usage_tokens(interaction: Any) -> tuple[int, int, int]:
    usage = getattr(interaction, "usage", None)
    if usage is None:
        return 0, 0, 0
    total = int(getattr(usage, "total_tokens", 0) or 0)
    thought = int(getattr(usage, "total_thought_tokens", 0) or getattr(usage, "thought_tokens", 0) or 0)
    output = int(getattr(usage, "total_output_tokens", 0) or getattr(usage, "output_tokens", 0) or 0)
    return total, thought, output


def _canonical_item_lookup(store: DataStore | None) -> tuple[dict[str, str], dict[str, str]]:
    """Return (id->name, normalized_name->canonical_id) prioritizing ARAM shop items over Arena/dead aliases."""
    id_to_name: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    if store is None or not store.static.items:
        return id_to_name, name_to_id

    for raw_id, info in store.static.items.items():
        if not info.name:
            continue
        raw_s = str(raw_id).strip()
        is_non_aram = len(raw_s) == 6 and raw_s[:2] in ("44", "77")
        if not is_non_aram or raw_s not in id_to_name:
            id_to_name[raw_s] = info.name
        if not is_non_aram:
            norm_id = normalize_item_id(raw_s)
            if len(raw_s) == 4 or norm_id not in id_to_name:
                id_to_name[norm_id] = info.name
        if info.name not in name_to_id:
            resolved = resolve_item_id_by_name(store, info.name, require_valid_plan=False)
            if resolved:
                name_to_id[info.name] = resolved
            elif not is_non_aram:
                name_to_id[info.name] = raw_s
    return id_to_name, name_to_id


_PAREN_ITEM_ID_RE = re.compile(r"\((\d+)\)$")
_NOISE_ITEM_NAMES: frozenset[str] = frozenset({"魄罗佳肴", "Poro-Snax"})
# Strategic low-cost components (<1200G) that remain tactically relevant on allies/enemies
_STRATEGIC_ROSTER_ITEM_IDS: frozenset[str] = frozenset(
    {
        "3070",  # 女神之泪 (Tear)
        "3123",  # 死刑宣告 (Executioner's Calling - GW)
        "3916",  # 湮灭宝珠 (Oblivion Orb - GW)
        "3140",  # 水银饰带 (QSS)
        "3076",  # 棘刺背心 (Bramble Vest - GW)
        "2420",  # 探索者的护臂 / 秒表 (Stopwatch / Armguard)
    }
)
_VERBOSE_SPECIAL_BUILD_KEYS: frozenset[str] = frozenset({"mechanic", "playstyle_tip"})
_LOBBY_SCOUT_PII_KEYS: frozenset[str] = frozenset(
    {"puuid", "puuids", "summoner_name", "summonername", "riot_id", "riotid", "account_id", "accountid"}
)


def _compact_lobby_scout(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Sanitize and prune lobby_scout context while stripping any accidental PII keys."""
    if not isinstance(raw, dict) or not raw:
        return {}
    sanitized = {
        k: v
        for k, v in raw.items()
        if str(k).strip().lower() not in _LOBBY_SCOUT_PII_KEYS
    }
    cleaned = _prune_empty(sanitized)
    return cleaned if isinstance(cleaned, dict) else {}


def _extract_item_id_token(raw: Any) -> str:
    """Extract raw numeric item ID from plain IDs ('3153') or labelled strings ('破败王者之刃(3153)')."""
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _PAREN_ITEM_ID_RE.search(text)
    if m:
        return m.group(1)
    return text


def _clean_item_names(items: Any) -> list[str]:
    """Copy item name list while stripping ARAM trinket noise such as '魄罗佳肴'."""
    if not isinstance(items, (list, tuple)):
        return []
    cleaned: list[str] = []
    for item in items:
        name = str(item or "").strip()
        if not name or name in _NOISE_ITEM_NAMES:
            continue
        cleaned.append(name)
    return cleaned


def _prune_empty(obj: Any) -> Any:
    """Recursively strip None, empty strings, empty lists, and empty dicts while preserving bools and numbers."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            cleaned = _prune_empty(v)
            if cleaned is None or cleaned == "" or cleaned == [] or cleaned == {}:
                continue
            out[k] = cleaned
        return out
    if isinstance(obj, list):
        out_list: list[Any] = []
        for item in obj:
            cleaned = _prune_empty(item)
            if cleaned is None or cleaned == "" or cleaned == [] or cleaned == {}:
                continue
            out_list.append(cleaned)
        return out_list
    if isinstance(obj, str):
        return obj.strip()
    return obj


def _is_low_value_roster_component(
    item_name: str,
    store: DataStore | None,
    name_to_id: dict[str, str],
) -> bool:
    """Return True iff item_name is a cheap base component (<1200G) with no macro build signal."""
    if store is None or not name_to_id:
        return False
    raw_id = name_to_id.get(item_name)
    if not raw_id:
        return False
    nid = normalize_item_id(raw_id)
    if nid in _STRATEGIC_ROSTER_ITEM_IDS or nid in TIER2_BOOT_IDS:
        return False
    info = store.get_item_info(raw_id)
    if not info.name or info.name.startswith("装备#"):
        return False
    if info.gold <= 0:
        return True
    if info.gold >= 1200:
        return False
    if info.category in ("boots", "complete_equipment"):
        return False
    if info.has_grievous:
        return False
    if "Boots" in (info.tags or ()):
        return False
    return True


def _compact_roster_entry(
    entry: dict[str, Any],
    *,
    store: DataStore | None,
    name_to_id: dict[str, str],
    strip_spells: bool = False,
) -> dict[str, Any]:
    """Slim an ally/enemy entry by dropping cheap base components and volatile respawn timers."""
    out: dict[str, Any] = {}
    for k, v in entry.items():
        if k == "respawn_s":
            continue
        if k == "spells" and strip_spells:
            continue
        if k == "items":
            filtered = [
                nm
                for nm in _clean_item_names(v)
                if not _is_low_value_roster_component(nm, store, name_to_id)
            ]
            out["items"] = filtered
        elif v is None or v == "" or v == [] or v == {}:
            continue
        else:
            out[k] = list(v) if isinstance(v, list) else v
    if "items" not in out:
        out["items"] = []
    return out


def _compact_special_augment_builds(
    builds: list[dict[str, Any]],
    *,
    trigger_reason: str,
) -> list[dict[str, Any]]:
    """Compact special augment build hints; drop verbose prose on routine death build-check turns."""
    is_routine_death = (trigger_reason or "").strip().lower() == "death_enemy_build_changed"
    compacted: list[dict[str, Any]] = []
    for sp in builds or []:
        if not isinstance(sp, dict):
            continue
        raw_entry = {
            k: v
            for k, v in sp.items()
            if not (is_routine_death and k in _VERBOSE_SPECIAL_BUILD_KEYS)
        }
        cleaned = _prune_empty(raw_entry)
        if cleaned:
            compacted.append(cleaned)
    return compacted


def _compact_augment_combo_context(
    ctx: dict[str, Any],
    *,
    trigger_reason: str,
    has_offered: bool,
) -> dict[str, Any]:
    """Prune empty containers and trim augment-picking-only fields on routine death buy turns."""
    if not isinstance(ctx, dict) or not ctx:
        return {}
    is_routine_death = (trigger_reason or "").strip().lower() == "death_enemy_build_changed" and not has_offered
    staged: dict[str, Any] = {}
    for k, v in ctx.items():
        if is_routine_death and k in ("penalized_axes", "top_targets"):
            continue
        if k == "active_combos" and isinstance(v, list):
            combos: list[dict[str, Any]] = []
            for c in v:
                if not isinstance(c, dict):
                    continue
                ce: dict[str, Any] = {}
                if c.get("name"):
                    ce["name"] = str(c["name"]).strip()
                if c.get("completed") is True:
                    ce["completed"] = True
                if c.get("gamble") is True:
                    ce["gamble"] = True
                if not is_routine_death and c.get("chase"):
                    ce["chase"] = list(c["chase"])
                for ck, cv in c.items():
                    if ck not in ("name", "completed", "gamble", "chase"):
                        ce[ck] = cv
                pruned_ce = _prune_empty(ce)
                if pruned_ce:
                    combos.append(pruned_ce)
            if combos:
                staged["active_combos"] = combos
        else:
            staged[k] = v
    return _prune_empty(staged)


def _norm_hero_token(text: Any) -> str:
    return "".join(ch.lower() for ch in str(text or "") if ch.isalnum())


def _build_self_hero_tokens(my_hero: dict[str, Any], store: DataStore | None) -> tuple[str, set[str]]:
    hid = str(my_hero.get("hero_id") or "").strip()
    tokens: set[str] = set()
    for field_key in ("name", "title", "champion", "hero_name"):
        tok = _norm_hero_token(my_hero.get(field_key))
        if tok:
            tokens.add(tok)

    hero_obj = None
    if store is not None:
        if hid:
            hero_obj = store.get_hero(hid)
        if hero_obj is None:
            raw_name = str(my_hero.get("name") or my_hero.get("champion") or "").strip()
            if raw_name:
                hero_obj = store.find_hero_by_name(raw_name)
                if hero_obj is not None and not hid:
                    hid = hero_obj.hero_id

    if hero_obj is not None:
        for val in (
            hero_obj.name,
            hero_obj.title,
            f"{hero_obj.title}{hero_obj.name}",
            f"{hero_obj.name}{hero_obj.title}",
        ):
            tok = _norm_hero_token(val)
            if tok:
                tokens.add(tok)
    if store is not None and hid:
        for val in (
            store.static.get_champion_alias(hid),
            store.static.hero_cn_names.get(hid, ""),
        ):
            tok = _norm_hero_token(val)
            if tok:
                tokens.add(tok)
    return hid, tokens


def _clean_roster_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in entry.items():
        if k == "items":
            out["items"] = _clean_item_names(v)
        elif v is None or v == "" or v == []:
            continue
        else:
            out[k] = list(v) if isinstance(v, list) else v
    if "items" not in out:
        out["items"] = []
    return out


def _dedup_allies(
    allies_raw: list[dict[str, Any]],
    my_hero: dict[str, Any],
    my_items_clean: list[str],
    store: DataStore | None,
) -> tuple[list[dict[str, Any]], str]:
    """Deduplicate self out of allies list and return (cleaned_allies, self_kda)."""
    cleaned = [_clean_roster_entry(a) for a in (allies_raw or []) if isinstance(a, dict)]
    if not cleaned:
        return [], ""

    hid, self_tokens = _build_self_hero_tokens(my_hero, store)

    self_idx: int | None = None
    for idx, ally in enumerate(cleaned):
        if ally.get("is_self") or ally.get("is_me"):
            self_idx = idx
            break
        ally_hid = str(ally.get("hero_id") or "").strip()
        if hid and ally_hid and ally_hid == hid:
            self_idx = idx
            break
        raw_champ = str(ally.get("champion") or ally.get("name") or "").strip()
        ally_tok = _norm_hero_token(raw_champ)
        if ally_tok and ally_tok in self_tokens:
            self_idx = idx
            break
        if store is not None and hid and raw_champ:
            found = store.find_hero_by_name(raw_champ)
            if found is not None and found.hero_id == hid:
                self_idx = idx
                break

    if self_idx is None:
        my_kda = str(my_hero.get("kda") or "").strip()
        my_spells = [str(s).strip() for s in (my_hero.get("spells") or []) if str(s).strip()]
        my_respawn = int(my_hero.get("respawn_s") or 0)

        if my_kda:
            kda_matches = [
                idx
                for idx, ally in enumerate(cleaned)
                if str(ally.get("kda") or "").strip() == my_kda
                and (ally.get("items") or []) == my_items_clean
                and (bool(my_items_clean) or my_kda != "0/0/0")
            ]
            if len(kda_matches) == 1:
                self_idx = kda_matches[0]

        if self_idx is None and my_items_clean:
            item_matches: list[int] = []
            for idx, ally in enumerate(cleaned):
                if (ally.get("items") or []) != my_items_clean:
                    continue
                ally_spells = [str(s).strip() for s in (ally.get("spells") or []) if str(s).strip()]
                if my_spells and ally_spells and ally_spells != my_spells:
                    continue
                ally_respawn = int(ally.get("respawn_s") or 0)
                if my_respawn > 0 and ally_respawn > 0 and abs(ally_respawn - my_respawn) > 2:
                    continue
                if len(my_items_clean) >= 2 or (my_spells and ally_spells == my_spells):
                    item_matches.append(idx)
            if len(item_matches) == 1:
                self_idx = item_matches[0]

    if self_idx is None:
        return cleaned, ""

    self_kda = str(cleaned[self_idx].get("kda") or "").strip()
    deduped = [a for idx, a in enumerate(cleaned) if idx != self_idx]
    return deduped, self_kda


def _is_item_profile_compatible(
    store: DataStore | None,
    item_id: str,
    profile: str,
) -> bool:
    """Return True iff item_id is not cross-profile garbage for the hero's damage profile."""
    prof = (profile or "AP").strip().upper()
    nid = normalize_item_id(item_id)
    if not nid:
        return False
    if nid in _CROSS_PROFILE_FORBIDDEN_IDS.get(prof, frozenset()):
        return False
    if store is None or not store.static.items or prof == "HYBRID":
        return True
    info = store.get_item_info(nid)
    if not info.name or info.name.startswith("装备#"):
        return True
    st = info.stats or {}
    ap_v = float(st.get("FlatMagicDamageMod", 0.0) or 0.0)
    ad_v = float(st.get("FlatPhysicalDamageMod", 0.0) or 0.0)
    crit_v = float(st.get("FlatCritChanceMod", 0.0) or 0.0)
    hp_v = float(st.get("FlatHPPoolMod", 0.0) or 0.0)
    arm_v = float(st.get("FlatArmorMod", 0.0) or 0.0)
    mr_v = float(st.get("FlatSpellBlockMod", 0.0) or 0.0)
    if prof == "TANK":
        if crit_v > 0.0:
            return False
        if ap_v >= 45.0 and arm_v <= 0.0 and mr_v <= 0.0 and hp_v < 350.0:
            return False
    elif prof == "AD":
        if ap_v >= 45.0 and ad_v <= 0.0:
            return False
    elif prof in ("AP", "SUPPORT"):
        if crit_v > 0.0 and ap_v <= 0.0:
            return False
        if ad_v >= 35.0 and ap_v <= 0.0:
            return False
    return True


def _resolve_owned_item_ids(
    item_names: list[str],
    name_to_id: dict[str, str],
) -> list[str]:
    ids: list[str] = []
    for nm in item_names:
        if nm in name_to_id:
            ids.append(name_to_id[nm])
        else:
            tok = _extract_item_id_token(nm)
            if tok.isdigit():
                ids.append(normalize_item_id(tok))
    return ids


def _compute_crit_chance_pct(
    raw_my: dict[str, Any],
    my_items_clean: list[str],
    selected_augments: list[dict[str, Any]],
    store: DataStore | None,
    hid: str,
    name_to_id: dict[str, str],
) -> int:
    """Resolve effective crit chance as integer percentage (0 when <=0)."""
    explicit = raw_my.get("crit_chance_pct")
    if explicit is not None and not isinstance(explicit, bool):
        try:
            val = float(explicit)
            if 0.0 < val <= 1.5:
                val *= 100.0
            if val > 0.5:
                return int(round(val))
        except (TypeError, ValueError):
            pass

    stats_src: dict[str, Any] | None = None
    if isinstance(raw_my.get("my_stats"), dict):
        stats_src = dict(raw_my["my_stats"])
    elif isinstance(raw_my.get("stats"), dict):
        stats_src = dict(raw_my["stats"])
    elif raw_my.get("critChance") is not None:
        stats_src = {"critChance": raw_my.get("critChance")}

    owned_ids = _resolve_owned_item_ids(my_items_clean, name_to_id)
    sel_augs = [
        str(a.get("id", "")).strip()
        for a in (selected_augments or [])
        if isinstance(a, dict) and str(a.get("id", "")).strip()
    ]
    if not stats_src and not owned_ids and not sel_augs:
        return 0
    try:
        eff = estimate_effective_crit_chance(
            store,
            hid or None,
            my_items=owned_ids,
            selected_augments=sel_augs,
            my_stats=stats_src,
            game_time_s=float(raw_my.get("game_time_s") or 0.0),
        )
    except Exception:
        return 0
    return int(round(eff * 100)) if eff > 0.005 else 0


def _parse_kda_triple(kda_str: str) -> tuple[int, int, int]:
    parts = [p.strip() for p in str(kda_str or "").split("/")]
    if len(parts) != 3:
        return 0, 0, 0
    try:
        return max(0, int(parts[0])), max(0, int(parts[1])), max(0, int(parts[2]))
    except (TypeError, ValueError):
        return 0, 0, 0


def _short_hero_name(raw_champ: str, store: DataStore | None) -> tuple[str, str]:
    """Return (short_display_name, hero_id) using DataStore hero lookup when available."""
    text = str(raw_champ or "").strip()
    if not text or store is None:
        return text, ""
    hero = store.find_hero_by_name(text)
    if hero is not None:
        return (hero.name or text), hero.hero_id
    return text, ""


def _is_boot_item_name(item_name: str, store: DataStore | None, name_to_id: dict[str, str]) -> bool:
    raw_id = name_to_id.get(item_name, "")
    if raw_id:
        nid = normalize_item_id(raw_id)
        if nid in TIER2_BOOT_IDS or nid == "1001":
            return True
        if store is not None:
            info = store.get_item_info(raw_id)
            if info.category == "boots" or "Boots" in (info.tags or ()):
                return True
    return item_name.endswith("靴") or item_name.endswith("胫甲")


def _classify_roster_player(
    entry: dict[str, Any],
    store: DataStore | None,
    name_to_id: dict[str, str],
) -> dict[str, Any]:
    """0ms local roster feature extractor for comp_summary and single-line ally/enemy formatting."""
    raw_champ = str(entry.get("champion") or entry.get("name") or "").strip()
    short_name, hid = _short_hero_name(raw_champ, store)
    if not hid:
        hid = str(entry.get("hero_id") or "").strip()

    kda_str = str(entry.get("kda") or "").strip()
    kills, deaths, assists = _parse_kda_triple(kda_str)
    items_clean = _clean_item_names(entry.get("items"))
    item_ids = _resolve_owned_item_ids(items_clean, name_to_id)

    spells_raw = [str(s).strip() for s in (entry.get("spells") or []) if str(s).strip()]
    defensive_spells = [s for s in spells_raw if s in _DEFENSIVE_ENEMY_SPELLS]
    has_snowball = any(s in ("雪球", "标记", "Snowball", "Mark") for s in spells_raw)

    # Filter completed + strategic counter items (drop boots & cheap components)
    gw_or_sb_names: list[str] = []
    other_sig_names: list[str] = []
    ad_power = 0.0
    ap_power = 0.0
    tank_pts = 0.0
    has_bruiser_item = False
    heal_score = 0.0
    shield_score = 0.0
    has_gw = False
    has_shield_break = False

    if short_name in HIGH_HEAL_HEROES or raw_champ in HIGH_HEAL_HEROES:
        heal_score += 1.4
    if short_name in HIGH_SHIELD_HEROES or raw_champ in HIGH_SHIELD_HEROES:
        shield_score += 1.4
    if store is not None and hid:
        alias = store.static.get_champion_alias(hid)
        if alias in HIGH_HEAL_HEROES and heal_score == 0.0:
            heal_score += 1.4
        if alias in HIGH_SHIELD_HEROES and shield_score == 0.0:
            shield_score += 1.4

    for nm in items_clean:
        raw_id = name_to_id.get(nm, "")
        nid = normalize_item_id(raw_id) if raw_id else ""
        info = store.get_item_info(raw_id) if (store is not None and raw_id) else None

        if nid in _GW_ITEM_IDS_ALL or (info is not None and info.has_grievous):
            has_gw = True
        if nid in SHIELD_BREAK_ITEMS or (info is not None and info.has_shield_break):
            has_shield_break = True
        if nid in HEAL_ITEM_IDS:
            heal_score += 0.65
        if nid in SHIELD_ITEM_IDS:
            shield_score += 0.65
        if nid in _BRUISER_AD_ITEM_IDS:
            has_bruiser_item = True

        if info is not None and info.name and not info.name.startswith("装备#"):
            st = info.stats or {}
            ad_power += (
                float(st.get("FlatPhysicalDamageMod", 0.0) or 0.0)
                + float(st.get("FlatCritChanceMod", 0.0) or 0.0) * 120.0
                + float(st.get("PercentAttackSpeedMod", 0.0) or 0.0) * 80.0
            )
            ap_power += float(st.get("FlatMagicDamageMod", 0.0) or 0.0) * 0.85
            if float(st.get("PercentLifeStealMod", 0.0) or 0.0) > 0.0:
                heal_score += 0.55
            _ad_p, _ap_p, tk_p, _sp_p = _score_item_archetype(info, is_owned_item=True)
            tank_pts += tk_p

        if _is_boot_item_name(nm, store, name_to_id):
            continue
        if _is_low_value_roster_component(nm, store, name_to_id):
            continue
        is_func = (
            nid in _GW_ITEM_IDS_ALL
            or nid in SHIELD_BREAK_ITEMS
            or (info is not None and (info.has_grievous or info.has_shield_break))
        )
        if is_func:
            if nm not in gw_or_sb_names:
                gw_or_sb_names.append(nm)
        elif nm not in other_sig_names:
            other_sig_names.append(nm)

    # Infer compact 1-2 char role tag
    if store is not None and (hid or item_ids):
        prof = infer_unified_hero_profile(hid or None, store, my_items=item_ids)
    else:
        prof = "AP" if ap_power > ad_power * 1.15 else ("TANK" if tank_pts >= 55.0 else "AD")

    if prof == "TANK":
        role = "坦"
    elif prof == "SUPPORT":
        role = "辅"
    elif prof == "AP":
        role = "AP"
    elif prof == "HYBRID":
        role = "混"
    else:
        is_fighter = has_bruiser_item or tank_pts >= 22.0
        if not is_fighter and store is not None and hid and not item_ids:
            cb_ids = [iid for cb in store.get_core_builds(hid)[:2] for iid in cb.items]
            if any(normalize_item_id(i) in _BRUISER_AD_ITEM_IDS for i in cb_ids):
                is_fighter = True
        role = "战" if is_fighter else "AD"

    if ad_power <= 0.0 and ap_power <= 0.0:
        if role == "AP":
            ap_power = 75.0
        elif role in ("AD", "战"):
            ad_power = 75.0
        elif role == "辅":
            ap_power = 45.0
        elif role == "坦":
            ad_power = 25.0
            ap_power = 25.0
        else:
            ad_power = 40.0
            ap_power = 40.0

    return {
        "name": short_name or raw_champ,
        "role": role,
        "profile": prof,
        "kda": kda_str,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "completed_items": gw_or_sb_names + other_sig_names,
        "defensive_spells": defensive_spells,
        "has_snowball": has_snowball,
        "ad_power": ad_power,
        "ap_power": ap_power,
        "heal_score": heal_score,
        "shield_score": shield_score,
        "has_gw": has_gw,
        "has_shield_break": has_shield_break,
    }


def _format_role_counts(roles: list[str]) -> str:
    counts: dict[str, int] = {}
    for r in roles:
        counts[r] = counts.get(r, 0) + 1
    return "".join(f"{counts[r]}{r}" for r in _ROLE_ORDER if counts.get(r, 0) > 0)


def _build_comp_summary(
    my_info: dict[str, Any] | None,
    ally_infos: list[dict[str, Any]],
    enemy_infos: list[dict[str, Any]],
    threat_summary: list[str],
    *,
    is_game_start: bool = False,
) -> str:
    """Build 0ms pre-digested 1-line comp_summary (~65 chars) from both teams."""
    if not ally_infos and not enemy_infos:
        return ""

    all_allies = ([my_info] if my_info is not None else []) + ally_infos
    ally_roles_str = _format_role_counts([a["role"] for a in all_allies])
    enemy_roles_str = _format_role_counts([e["role"] for e in enemy_infos])

    ally_has_gw = any(a["has_gw"] for a in all_allies)
    ally_has_sb = any(a["has_shield_break"] for a in all_allies)

    total_ad = sum(e["ad_power"] for e in enemy_infos)
    total_ap = sum(e["ap_power"] for e in enemy_infos)
    enemy_heal = sum(e["heal_score"] for e in enemy_infos)
    enemy_shield = sum(e["shield_score"] for e in enemy_infos)
    snowball_cnt = sum(1 for e in enemy_infos if e["has_snowball"])

    threat_blob = " ".join(str(t) for t in (threat_summary or []))
    if ("高回复" in threat_blob or "回血" in threat_blob) and enemy_heal < 2.2:
        enemy_heal = 2.4
    if ("厚护盾" in threat_blob or "高护盾" in threat_blob or "多盾" in threat_blob) and enemy_shield < 2.2:
        enemy_shield = 2.4

    ally_gaps: list[str] = []
    if not ally_has_gw and (enemy_heal >= 1.4 or "缺重伤" in threat_blob):
        ally_gaps.append("缺重伤")
    if not ally_has_sb and (enemy_shield >= 1.4 or "缺破盾" in threat_blob):
        ally_gaps.append("缺破盾")

    enemy_notes: list[str] = []
    if (total_ad + total_ap) > 0:
        ad_pct = int(round(100.0 * total_ad / (total_ad + total_ap)))
        ap_pct = 100 - ad_pct
        enemy_notes.append(f"AD{ad_pct}%/AP{ap_pct}%")
    if enemy_heal >= 2.2:
        enemy_notes.append("高回复")
    elif enemy_heal >= 1.3:
        enemy_notes.append("中回复")
    if enemy_shield >= 2.2:
        enemy_notes.append("高护盾")
    elif enemy_shield >= 1.3:
        enemy_notes.append("中护盾")
    if snowball_cnt >= 3:
        enemy_notes.append(f"{snowball_cnt}雪球")

    if not is_game_start and enemy_infos:
        fed_cand = max(
            enemy_infos,
            key=lambda e: (e["kills"] - e["deaths"] * 0.6 + e["assists"] * 0.15, e["kills"]),
        )
        if fed_cand["kills"] >= 6 or (fed_cand["kills"] >= 4 and (fed_cand["kills"] - fed_cand["deaths"]) >= 2):
            enemy_notes.append(f"最肥:{fed_cand['name']}{fed_cand['kills']}/{fed_cand['deaths']}")

    parts: list[str] = []
    if ally_roles_str:
        ally_txt = f"我方:{ally_roles_str}"
        if ally_gaps:
            ally_txt += f"({'/'.join(ally_gaps)})"
        parts.append(ally_txt)
    if enemy_roles_str:
        enemy_txt = f"敌方:{enemy_roles_str}"
        if enemy_notes:
            enemy_txt += f"({','.join(enemy_notes)})"
        parts.append(enemy_txt)
    return ";".join(parts)


def _format_single_line_enemy(info: dict[str, Any], *, game_start: bool = False) -> str:
    if game_start:
        return f"{info['name']}({info['role']})"
    header = f"{info['name']}({info['kda']})" if info["kda"] else info["name"]
    if info["defensive_spells"]:
        header += f"[{'/'.join(info['defensive_spells'])}]"
    items_str = ",".join(info["completed_items"][:6])
    return f"{header}:{items_str}" if items_str else header


def _format_single_line_ally(info: dict[str, Any], *, game_start: bool = False) -> str:
    if game_start:
        return f"{info['name']}({info['role']})"
    tag = f"{info['role']},{info['kda']}" if info["kda"] else info["role"]
    items_str = ",".join(info["completed_items"][:4])
    return f"{info['name']}({tag}):{items_str}" if items_str else f"{info['name']}({tag})"


def _format_pipe_local_item(rec: Any, my_gold: int) -> str:
    """Format a local engine top item recommendation into a single-line pipe string."""
    if isinstance(rec, str):
        return rec.strip()
    if not isinstance(rec, dict):
        return ""
    raw_id = str(rec.get("item_id") or "").strip()
    nid = normalize_item_id(raw_id) if raw_id else ""
    name = str(rec.get("name") or "").strip()
    if not nid and not name:
        return ""

    eff_cost = rec.get("effective_cost")
    if eff_cost is None:
        eff_cost = rec.get("price")
    cost_seg = ""
    if eff_cost is not None and not isinstance(eff_cost, bool):
        try:
            cost_int = int(round(float(eff_cost)))
            gap = max(0, cost_int - my_gold)
            if rec.get("affordable") is True or gap == 0:
                cost_seg = f"可直买({cost_int}G)"
            else:
                cost_seg = f"差{gap}G"
        except (TypeError, ValueError):
            pass

    buy_hint = str(rec.get("buy_now_hint") or "").strip().replace("【", "").replace("】", "")
    # Skip redundant buy_hint that merely echoes direct synthesis of the same item
    if buy_hint and cost_seg.startswith("可直买") and name and name in buy_hint and "先买" not in buy_hint:
        buy_hint = ""

    raw_reasons = [str(r).strip() for r in (rec.get("reasons") or []) if str(r).strip()]
    reason_seg = raw_reasons[0] if raw_reasons else ""
    if len(raw_reasons) >= 2:
        r2 = raw_reasons[1]
        if any(kw in r2 for kw in ("重伤", "破盾", "回复", "护盾", "魔抗", "护甲", "穿甲", "法穿")) and r2 not in reason_seg:
            reason_seg = f"{reason_seg};{r2}" if reason_seg else r2

    segs = [s for s in (nid, name, cost_seg, buy_hint, reason_seg) if s]
    return "|".join(segs)


def _format_compact_special_builds(builds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep ONLY {name, force, core[:3], avoid}, dropping verbose mechanic/playstyle_tip prose."""
    out: list[dict[str, Any]] = []
    for sp in builds or []:
        if not isinstance(sp, dict):
            continue
        slim: dict[str, Any] = {}
        name = str(sp.get("name") or "").strip()
        if name:
            slim["name"] = name
        force = [str(x).strip() for x in (sp.get("force_items") or sp.get("force") or []) if str(x).strip()]
        if force:
            slim["force"] = force
        core = [str(x).strip() for x in (sp.get("core_items") or sp.get("core") or []) if str(x).strip()][:3]
        if core:
            slim["core"] = core
        avoid = str(sp.get("avoid_note") or sp.get("avoid") or "").strip()
        if avoid:
            slim["avoid"] = avoid
        if slim:
            out.append(slim)
    return out


def _format_next_aug_pool(
    ctx: dict[str, Any],
    store: DataStore | None,
    hid: str,
    selected_augments: list[dict[str, Any]],
    *,
    max_items: int = 4,
) -> list[str]:
    """Format Top 3-4 verified pool+comp-weighted next augment candidates as compact strings."""
    if len(selected_augments or []) >= 4:
        return []
    owned_ids = {
        str(a.get("id") or "").strip()
        for a in (selected_augments or [])
        if isinstance(a, dict) and str(a.get("id") or "").strip()
    }
    pool: list[str] = []
    seen_names: set[str] = set()

    raw_targets = (ctx or {}).get("top_targets") or (ctx or {}).get("next_aug_pool") or []
    for item in raw_targets:
        if len(pool) >= max_items:
            break
        if isinstance(item, str):
            txt = item.strip()
            if txt and txt not in seen_names:
                seen_names.add(txt)
                pool.append(txt)
            continue
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "").strip()
        if aid and aid in owned_ids:
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        meta: list[str] = []
        rar = _RARITY_SHORT_CN.get(str(item.get("rarity") or "").strip().lower(), "")
        if rar:
            meta.append(rar)
        tier = str(item.get("tier") or "").strip()
        if tier:
            meta.append(tier)
        raw_delta = item.get("delta")
        if raw_delta is not None and not isinstance(raw_delta, bool):
            try:
                dv = float(raw_delta)
                if abs(dv) >= 0.0005 or tier:
                    pct_val = dv * 100.0 if abs(dv) <= 1.0 else dv
                    meta.append(f"{pct_val:+.1f}%")
            except (TypeError, ValueError):
                pass
        comp_tag = str(item.get("comp_tag") or "").strip()
        if comp_tag:
            meta.append(comp_tag)
        pool.append(f"{name}({','.join(meta)})" if meta else name)

    if not pool and store is not None and hid:
        get_top_fn = getattr(store, "get_top_eligible_augments", None)
        if callable(get_top_fn):
            for aid, delta_v, _pct_v in get_top_fn(hid, limit=max_items + len(owned_ids)):
                if len(pool) >= max_items:
                    break
                if str(aid) in owned_ids:
                    continue
                aug = store.get_augment(str(aid))
                if aug is None or not aug.name or aug.name in seen_names:
                    continue
                seen_names.add(aug.name)
                rar = _RARITY_SHORT_CN.get((aug.rarity or "").strip().lower(), "")
                meta = [p for p in (rar, f"{delta_v * 100:+.1f}%") if p]
                pool.append(f"{aug.name}({','.join(meta)})" if meta else aug.name)

    return pool


def _format_compact_selected_augments(selected_augments: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for aug in selected_augments or []:
        if isinstance(aug, str):
            s = aug.strip()
            if s:
                out.append(s)
            continue
        if not isinstance(aug, dict):
            continue
        nm = str(aug.get("name") or aug.get("id") or "").strip()
        if not nm:
            continue
        rar = _RARITY_SHORT_CN.get(str(aug.get("rarity") or "").strip().lower(), "")
        out.append(f"{nm}({rar})" if rar else nm)
    return out


def _slim_lobby_scout(raw: dict[str, Any] | None) -> dict[str, Any]:
    cleaned = _compact_lobby_scout(raw)
    if not cleaned:
        return {}
    slim = {k: cleaned[k] for k in _LOBBY_SCOUT_SLIM_KEYS if k in cleaned}
    return slim or cleaned


def _build_profile_scoped_glossary(
    *,
    req: CoachRequestPayload,
    store: DataStore | None,
    hid: str,
    profile: str,
    inv_status: dict[str, Any],
    owned_complete_ids: set[str],
    local_top_ids: set[str],
    include_sig_hint: bool,
    is_game_start: bool = False,
) -> dict[str, str]:
    """Build profile-scoped, 4-fold deduplicated item_glossary (~150-220 chars)."""
    if store is None or not store.static.items:
        return {}

    prof = (profile or "AP").strip().upper()
    if prof not in PROFILE_COUNTER_ITEMS:
        prof = "AP"
    is_building = inv_status.get("status", "building") == "building"
    has_t2_boots = bool(inv_status.get("has_tier2_boots", False))
    rec_elixir_id = normalize_item_id(str((inv_status.get("recommended_elixir") or {}).get("item_id") or ""))

    glossary: dict[str, str] = {}

    def _add(raw_id: Any, *, allow_off_profile: bool = False) -> None:
        extracted = _extract_item_id_token(raw_id)
        if not extracted:
            return
        nid = normalize_item_id(extracted)
        if not nid or nid in glossary:
            return
        # 1) Exclude already owned complete items
        if nid in owned_complete_ids:
            return
        # 2) Exclude item IDs already present in local_engine_top_items
        if nid in local_top_ids:
            return
        info = store.get_item_info(nid)
        if (not info.name or info.name.startswith("装备#")) and extracted != nid:
            alt = store.get_item_info(extracted)
            if alt.name and not alt.name.startswith("装备#"):
                nid = extracted
                info = alt
        if not info.name or info.name.startswith("装备#") or nid in glossary:
            return
        # 3) Exclude all boots when player already owns Tier-2 boots (or on game_start full-item focus)
        is_boot = nid in TIER2_BOOT_IDS or info.category == "boots" or "Boots" in (info.tags or ())
        if has_t2_boots and is_boot:
            return
        # 4) Exclude all 3 elixirs when building; when full-build, keep ONLY the single matching elixir
        if nid in ELIXIR_IDS:
            if is_building or is_game_start or nid != rec_elixir_id:
                return
        if not allow_off_profile and not is_boot and nid not in ELIXIR_IDS:
            if not _is_item_profile_compatible(store, nid, prof):
                return
        glossary[nid] = f"{info.name}|{info.gold}"

    # Static prefix items FIRST (prefix-cache friendly)
    sel_arch = str((req.committed_build_plan or {}).get("selected_archetype") or "").strip().upper()
    if hid and sel_arch:
        for tmpl in get_hero_build_templates(store, hid):
            if tmpl.profile == prof:
                for iid in tmpl.items:
                    _add(iid)
        for b in store.get_core_builds(hid):
            if _classify_items_profile(store, b.items) == prof:
                for iid in b.items:
                    if not _is_item_off_active_profile(store, str(iid), prof):
                        _add(iid)
                break
    elif hid:
        for b in store.get_core_builds(hid)[:2]:
            for iid in b.items:
                _add(iid)
    if include_sig_hint and isinstance(req.hero_signature_hint, dict):
        for key in ("starter_items", "signature_items"):
            for iid in req.hero_signature_hint.get(key) or []:
                if sel_arch and _is_item_off_active_profile(store, str(iid), prof):
                    continue
                _add(iid)
    for iid in PROFILE_COUNTER_ITEMS.get(prof, PROFILE_COUNTER_ITEMS["AP"]):
        _add(iid)
    if not has_t2_boots and is_building and not is_game_start:
        for iid in _PROFILE_BOOTS_IDS.get(prof, ("3111", "3047", "3158")):
            _add(iid)
    if not is_building and not is_game_start and rec_elixir_id:
        _add(rec_elixir_id, allow_off_profile=True)

    # Dynamic turn special builds at tail
    for sp in req.special_augment_builds or []:
        if not isinstance(sp, dict):
            continue
        for iid in sp.get("force_items") or sp.get("force") or []:
            _add(iid, allow_off_profile=True)
        for iid in (sp.get("core_items") or sp.get("core") or [])[:3]:
            _add(iid, allow_off_profile=False)

    return glossary


def _build_compact_core_builds(store: DataStore | None, hid: str) -> list[str]:
    if store is None or not hid:
        return []
    routes: list[str] = []
    for b in store.get_core_builds(hid)[:2]:
        names = [store.get_item_info(iid).name for iid in b.items if store.get_item_info(iid).name]
        if names:
            routes.append(f"{'>'.join(names)}({b.win_rate * 100:.1f}%)")
    return routes


def _resolve_hero_and_inventory(
    req: CoachRequestPayload,
    store: DataStore | None,
) -> tuple[str, str, float, dict[str, str], list[str], str, dict[str, Any], set[str]]:
    """Resolve hero static metadata, canonical name_to_id map, damage profile, and 6-slot inventory status."""
    raw_my = dict(req.my_hero or {})
    hid = str(raw_my.get("hero_id") or "").strip()
    hero_obj = store.get_hero(hid) if (store is not None and hid) else None
    if hero_obj is None and store is not None and raw_my.get("name"):
        hero_obj = store.find_hero_by_name(str(raw_my["name"]))
        if hero_obj is not None and not hid:
            hid = hero_obj.hero_id

    hero_name = str(raw_my.get("name") or (hero_obj.name if hero_obj else "") or "")
    if "win_rate" in raw_my and raw_my["win_rate"] is not None:
        hero_win_rate = float(raw_my["win_rate"])
    elif hero_obj is not None:
        hero_win_rate = round(hero_obj.win_rate, 4)
    else:
        hero_win_rate = 0.5

    _, name_to_id = _canonical_item_lookup(store) if (store is not None and store.static.items) else ({}, {})
    my_items_clean = _clean_item_names(raw_my.get("items"))
    owned_ids = _resolve_owned_item_ids(my_items_clean, name_to_id)
    sel_augs = [
        str(a.get("id", "")).strip()
        for a in (req.selected_augments or [])
        if isinstance(a, dict) and str(a.get("id", "")).strip()
    ]

    # Resolve damage profile (honoring explicit player-selected archetype override first)
    raw_plan_cfg = req.committed_build_plan if isinstance(req.committed_build_plan, dict) else {}
    sel_arch = str(raw_plan_cfg.get("selected_archetype") or "").strip().upper()
    if sel_arch in PROFILE_COUNTER_ITEMS:
        prof = sel_arch
    else:
        ctx_prof = str((req.augment_combo_context or {}).get("primary_profile") or "").strip().upper()
        if ctx_prof == "CRIT_SPELL":
            ctx_prof = "AP"
        if store is not None and store.static.items and (hid or owned_ids):
            prof = infer_unified_hero_profile(hid or None, store, my_items=owned_ids, selected_augments=sel_augs)
        elif ctx_prof in PROFILE_COUNTER_ITEMS:
            prof = ctx_prof
        else:
            prof = "AP"

    owned_complete_ids: set[str] = set()
    for nm in _clean_item_names((req.committed_build_plan or {}).get("owned_complete")):
        if nm in name_to_id:
            owned_complete_ids.add(normalize_item_id(name_to_id[nm]))

    inv_status: dict[str, Any] = {
        "status": "building",
        "complete_count": len(_clean_item_names((req.committed_build_plan or {}).get("owned_complete"))),
        "has_tier2_boots": False,
        "boots_name": "",
        "boots_sell_gold": 0,
        "recommended_elixir": {"item_id": "2139", "name": "巫术合剂", "gold": 500},
    }
    if store is not None and store.static.items:
        inv = analyze_inventory_slots(store, owned_ids, profile=prof)
        for cid in inv.complete_items:
            owned_complete_ids.add(normalize_item_id(cid))
        inv_status = {
            "status": (
                "full_6_complete"
                if inv.is_six_complete
                else ("full_5_plus_boots" if inv.is_full_build_with_boots else "building")
            ),
            "complete_count": len(inv.complete_items),
            "has_tier2_boots": bool(inv.tier2_boots_id),
            "boots_name": inv.tier2_boots_name,
            "boots_sell_gold": int(inv.tier2_boots_gold * 0.70),
            "recommended_elixir": {
                "item_id": inv.recommended_elixir_id,
                "name": inv.recommended_elixir_name,
                "gold": 500,
            },
        }
    return hid, hero_name, hero_win_rate, name_to_id, my_items_clean, prof, inv_status, owned_complete_ids


def _build_game_start_high_snr_payload(
    req: CoachRequestPayload,
    store: DataStore | None,
) -> dict[str, Any]:
    """Route 3 (game_start): ~1,000-1,350 chars opening brief payload (strips items/KDA/elixirs)."""
    raw_my = dict(req.my_hero or {})
    hid, hero_name, hero_win_rate, name_to_id, my_items_clean, prof, inv_status, _owned_comp = (
        _resolve_hero_and_inventory(req, store)
    )
    hero_static: dict[str, Any] = {
        "hero_id": hid,
        "name": hero_name,
        "win_rate": hero_win_rate,
    }

    big_data_builds = _build_compact_core_builds(store, hid)
    compact_sig_hint = _prune_empty(req.hero_signature_hint) if req.hero_signature_hint else {}

    local_top_ids: set[str] = set()
    pipe_local_items: list[str] = []
    my_gold = int(raw_my.get("gold") or 1400)
    for rec in (req.local_engine_top_items or [])[:3]:
        if isinstance(rec, dict) and rec.get("item_id"):
            local_top_ids.add(normalize_item_id(str(rec["item_id"])))
        elif isinstance(rec, str) and "|" in rec:
            local_top_ids.add(normalize_item_id(rec.split("|", 1)[0]))
        line = _format_pipe_local_item(rec, my_gold)
        if line:
            pipe_local_items.append(line)

    glossary = _build_profile_scoped_glossary(
        req=req,
        store=store,
        hid=hid,
        profile=prof,
        inv_status=inv_status,
        owned_complete_ids=set(),
        local_top_ids=local_top_ids,
        include_sig_hint=bool(compact_sig_hint),
        is_game_start=True,
    )

    deduped_allies_raw, _self_kda = _dedup_allies(req.allies or [], raw_my, my_items_clean, store)
    cleaned_enemies_raw = [_clean_roster_entry(e) for e in (req.enemies or []) if isinstance(e, dict)]

    my_roster_info = _classify_roster_player(
        {"champion": hero_name, "hero_id": hid, "items": []},
        store,
        name_to_id,
    )
    ally_infos = [_classify_roster_player(a, store, name_to_id) for a in deduped_allies_raw]
    enemy_infos = [_classify_roster_player(e, store, name_to_id) for e in cleaned_enemies_raw]

    comp_summary = _build_comp_summary(
        my_roster_info,
        ally_infos,
        enemy_infos,
        list(req.threat_summary or []),
        is_game_start=True,
    )
    allies_lines = [_format_single_line_ally(a, game_start=True) for a in ally_infos]
    enemies_lines = [_format_single_line_enemy(e, game_start=True) for e in enemy_infos]

    # Strip items, KDA, components, is_dead, respawn_s on game_start
    my_hero_out: dict[str, Any] = {}
    if hero_name:
        my_hero_out["name"] = hero_name
    elif hid:
        my_hero_out["hero_id"] = hid
    my_hero_out["gold"] = my_gold
    if raw_my.get("keystone"):
        my_hero_out["keystone"] = str(raw_my["keystone"]).strip()

    next_aug_pool = _format_next_aug_pool(
        req.augment_combo_context or {},
        store,
        hid,
        req.selected_augments or [],
        max_items=4,
    )
    combo_out: dict[str, Any] = {}
    direction = str((req.augment_combo_context or {}).get("direction") or "").strip()
    if direction:
        combo_out["direction"] = direction
    if next_aug_pool:
        combo_out["next_aug_pool"] = next_aug_pool

    slim_scout = _slim_lobby_scout(req.lobby_scout)

    # Static prefix FIRST
    data: dict[str, Any] = {
        "dataset_patch": req.dataset_patch,
        "hero_static": hero_static,
        "hero_big_data_top_builds": big_data_builds,
        "item_glossary": glossary,
    }
    if compact_sig_hint:
        data["hero_signature_hint"] = compact_sig_hint

    data["trigger_reason"] = req.trigger_reason
    data["my_hero"] = my_hero_out
    raw_gs_plan = req.committed_build_plan if isinstance(req.committed_build_plan, dict) else {}
    gs_sel_arch = str(raw_gs_plan.get("selected_archetype") or "").strip().upper()
    if gs_sel_arch:
        gs_plan_out: dict[str, Any] = {"selected_archetype": gs_sel_arch}
        gs_avails = [str(a).strip() for a in (raw_gs_plan.get("available_archetypes") or []) if str(a).strip()][:3]
        if len(gs_avails) >= 2:
            gs_plan_out["available_archetypes"] = gs_avails
        data["committed_build_plan"] = gs_plan_out
    if combo_out:
        data["augment_combo_context"] = combo_out
    if pipe_local_items:
        data["local_engine_top_items"] = pipe_local_items
    if comp_summary:
        data["comp_summary"] = comp_summary
    if slim_scout:
        data["lobby_scout"] = slim_scout
    if allies_lines:
        data["allies"] = allies_lines
    if enemies_lines:
        data["enemies"] = enemies_lines
    return data


def _build_midgame_high_snr_payload(
    req: CoachRequestPayload,
    store: DataStore | None,
) -> dict[str, Any]:
    """Route 2 (death/warmup/augment_selected/manual_f6): high-SNR compact payload (<=2,150 chars)."""
    raw_my = dict(req.my_hero or {})
    hid, hero_name, hero_win_rate, name_to_id, my_items_clean, prof, inv_status, owned_complete_ids = (
        _resolve_hero_and_inventory(req, store)
    )
    hero_static: dict[str, Any] = {
        "hero_id": hid,
        "name": hero_name,
        "win_rate": hero_win_rate,
    }

    raw_plan = dict(req.committed_build_plan or {})
    allow_overhaul_val = bool(raw_plan.get("allow_overhaul", False))
    compact_special_builds = _format_compact_special_builds(req.special_augment_builds or [])

    # Omit hero_signature_hint when complete_count >= 2 and not allow_overhaul OR when special_augment_builds active
    omit_sig_hint = bool(compact_special_builds) or (
        int(inv_status.get("complete_count", 0)) >= 2 and not allow_overhaul_val
    )
    compact_sig_hint = {} if omit_sig_hint else (_prune_empty(req.hero_signature_hint) if req.hero_signature_hint else {})

    my_gold = int(raw_my.get("gold") or 0)
    local_top_ids: set[str] = set()
    pipe_local_items: list[str] = []
    for rec in (req.local_engine_top_items or [])[:4]:
        if isinstance(rec, dict) and rec.get("item_id"):
            local_top_ids.add(normalize_item_id(str(rec["item_id"])))
        elif isinstance(rec, str) and "|" in rec:
            local_top_ids.add(normalize_item_id(rec.split("|", 1)[0]))
        line = _format_pipe_local_item(rec, my_gold)
        if line:
            pipe_local_items.append(line)

    big_data_builds = _build_compact_core_builds(store, hid)
    glossary = _build_profile_scoped_glossary(
        req=req,
        store=store,
        hid=hid,
        profile=prof,
        inv_status=inv_status,
        owned_complete_ids=owned_complete_ids,
        local_top_ids=local_top_ids,
        include_sig_hint=bool(compact_sig_hint),
        is_game_start=False,
    )

    deduped_allies_raw, self_kda = _dedup_allies(req.allies or [], raw_my, my_items_clean, store)
    cleaned_enemies_raw = [_clean_roster_entry(e) for e in (req.enemies or []) if isinstance(e, dict)]

    my_roster_info = _classify_roster_player(
        {"champion": hero_name, "hero_id": hid, "kda": raw_my.get("kda") or self_kda, "items": my_items_clean},
        store,
        name_to_id,
    )
    ally_infos = [_classify_roster_player(a, store, name_to_id) for a in deduped_allies_raw]
    enemy_infos = [_classify_roster_player(e, store, name_to_id) for e in cleaned_enemies_raw]

    comp_summary = _build_comp_summary(
        my_roster_info,
        ally_infos,
        enemy_infos,
        list(req.threat_summary or []),
        is_game_start=False,
    )
    allies_lines = [_format_single_line_ally(a, game_start=False) for a in ally_infos]
    enemies_lines = [_format_single_line_enemy(e, game_start=False) for e in enemy_infos]

    my_hero_out: dict[str, Any] = {}
    if hero_name:
        my_hero_out["name"] = hero_name
    if hid and hero_name in ("", "未锁定", "未知"):
        my_hero_out["hero_id"] = hid
    for k in ("gold", "is_dead", "kda", "level", "game_time_s", "team_kills", "turrets_lost", "max_skill", "keystone"):
        v = raw_my.get(k)
        if v is None or v == "" or v == []:
            continue
        my_hero_out[k] = v
    if self_kda and "kda" not in my_hero_out:
        my_hero_out["kda"] = self_kda
    crit_pct = _compute_crit_chance_pct(
        raw_my,
        my_items_clean,
        req.selected_augments or [],
        store,
        hid,
        name_to_id,
    )
    if crit_pct > 0:
        my_hero_out["crit_chance_pct"] = crit_pct
    my_hero_out["items"] = my_items_clean

    if inv_status["status"] == "building":
        compact_inv: dict[str, Any] = {
            "status": "building",
            "complete_count": inv_status["complete_count"],
            "has_tier2_boots": inv_status["has_tier2_boots"],
        }
        if inv_status["boots_name"]:
            compact_inv["boots_name"] = inv_status["boots_name"]
    else:
        compact_inv = {
            "status": inv_status["status"],
            "complete_count": inv_status["complete_count"],
            "has_tier2_boots": inv_status["has_tier2_boots"],
            "recommended_elixir": inv_status["recommended_elixir"],
        }
        if inv_status["has_tier2_boots"]:
            if inv_status["boots_name"]:
                compact_inv["boots_name"] = inv_status["boots_name"]
            if inv_status["boots_sell_gold"]:
                compact_inv["boots_sell_gold"] = inv_status["boots_sell_gold"]

    compact_committed_plan: dict[str, Any] = {}
    if raw_plan:
        owned_complete_clean = _clean_item_names(raw_plan.get("owned_complete"))
        if owned_complete_clean:
            compact_committed_plan["owned_complete"] = owned_complete_clean
        locked_clean = _prune_empty(raw_plan.get("locked_next_item"))
        if locked_clean:
            compact_committed_plan["locked_next_item"] = locked_clean
        remaining_clean = _prune_empty(raw_plan.get("remaining_plan"))
        if isinstance(remaining_clean, list) and remaining_clean:
            compact_committed_plan["remaining_plan"] = remaining_clean[:4]
        if "allow_overhaul" in raw_plan:
            compact_committed_plan["allow_overhaul"] = allow_overhaul_val
        if "transformative_overhaul" in raw_plan:
            compact_committed_plan["transformative_overhaul"] = bool(raw_plan["transformative_overhaul"])
        overhaul_reason = str(raw_plan.get("overhaul_reason") or "").strip()
        if overhaul_reason and allow_overhaul_val:
            compact_committed_plan["overhaul_reason"] = overhaul_reason
        rec_boots = str(raw_plan.get("recommended_boots") or "").strip()
        if rec_boots and not inv_status["has_tier2_boots"]:
            compact_committed_plan["recommended_boots"] = rec_boots
        sel_arch = str(raw_plan.get("selected_archetype") or "").strip().upper()
        if sel_arch:
            compact_committed_plan["selected_archetype"] = sel_arch
        raw_avails = raw_plan.get("available_archetypes")
        if isinstance(raw_avails, list) and len(raw_avails) >= 2:
            compact_avails = [str(a).strip() for a in raw_avails if str(a).strip()][:3]
            if len(compact_avails) >= 2:
                compact_committed_plan["available_archetypes"] = compact_avails

    # P0-7 + P0-8: Omit augment_combo_context completely when 4/4 augments;
    # otherwise emit compact direction + next_aug_pool (Top 3-4 verified pool+comp candidates)
    sel_aug_list = list(req.selected_augments or [])
    compact_combo_ctx: dict[str, Any] = {}
    if len(sel_aug_list) < 4:
        raw_ctx = req.augment_combo_context or {}
        direction = str(raw_ctx.get("direction") or "").strip()
        if direction:
            compact_combo_ctx["direction"] = direction
        active_compact: list[str] = []
        for c in raw_ctx.get("active_combos") or []:
            if isinstance(c, dict) and c.get("name"):
                nm = str(c["name"]).strip()
                if c.get("completed") is True:
                    active_compact.append(f"{nm}(已成型)")
                else:
                    chase = [str(x).strip() for x in (c.get("chase") or []) if str(x).strip()][:2]
                    active_compact.append(f"{nm}(追{'/'.join(chase)})" if chase else nm)
            elif isinstance(c, str) and c.strip():
                active_compact.append(c.strip())
        if active_compact:
            compact_combo_ctx["active_combos"] = active_compact[:2]
        if not req.offered_augments:
            next_pool = _format_next_aug_pool(raw_ctx, store, hid, sel_aug_list, max_items=4)
            if next_pool:
                compact_combo_ctx["next_aug_pool"] = next_pool

    compact_selected_augs = _format_compact_selected_augments(sel_aug_list)
    slim_scout = _slim_lobby_scout(req.lobby_scout)

    # Keep only non-redundant threat tags not already surfaced in comp_summary
    extra_threats: list[str] = []
    if req.threat_summary:
        for tag in req.threat_summary:
            t = str(tag or "").strip()
            if not t:
                continue
            if comp_summary and (
                ("重AP" in t and "AP" in comp_summary)
                or ("重AD" in t and "AD" in comp_summary)
                or ("雪球" in t and "雪球" in comp_summary)
                or ("头号威胁" in t and "最肥:" in comp_summary)
                or ("高回复" in t and "高回复" in comp_summary)
                or ("护盾" in t and "护盾" in comp_summary)
            ):
                continue
            extra_threats.append(t)

    # Assemble payload: match-invariant static prefix FIRST for Prefix Cache hit rate
    data: dict[str, Any] = {
        "dataset_patch": req.dataset_patch,
        "hero_static": hero_static,
        "hero_big_data_top_builds": big_data_builds,
        "item_glossary": glossary,
    }
    if compact_sig_hint:
        data["hero_signature_hint"] = compact_sig_hint

    data["trigger_reason"] = req.trigger_reason
    data["my_hero"] = my_hero_out
    data["inventory_status"] = compact_inv
    data["selected_augments"] = compact_selected_augs
    if req.offered_augments:
        data["offered_augments"] = list(req.offered_augments)
    if compact_special_builds:
        data["special_augment_builds"] = compact_special_builds
    if compact_combo_ctx:
        data["augment_combo_context"] = compact_combo_ctx
    if compact_committed_plan:
        data["committed_build_plan"] = compact_committed_plan
    if pipe_local_items:
        data["local_engine_top_items"] = pipe_local_items
    if comp_summary:
        data["comp_summary"] = comp_summary
    if extra_threats:
        data["threat_summary"] = extra_threats[:3]
    if slim_scout:
        data["lobby_scout"] = slim_scout
    if allies_lines:
        data["allies"] = allies_lines
    if enemies_lines:
        data["enemies"] = enemies_lines
    return data


def build_high_snr_prompt_payload(
    req: CoachRequestPayload,
    store: DataStore | None,
) -> dict[str, Any]:
    """3-Tier Trigger-Aware High-SNR Payload Slicer (P0-8).

    Routes by trigger_reason:
    - Route 1 (`augment_picking`): fast picker payload with crit_chance_pct (>0) & 1-line comp_summary.
    - Route 3 (`game_start`): lean opening brief payload stripping items/KDA/elixirs.
    - Route 2 (death / warmup / augment_selected / manual_f6): profile-scoped 4-fold deduped glossary,
      single-line allies/enemies/local_items/core_builds, compact special builds, and P0-7 next_aug_pool.
    """
    reason = (req.trigger_reason or "").strip().lower()
    if reason == "augment_picking":
        return build_picker_fast_prompt_payload(req, store, compact_comp=True)
    if reason == "game_start":
        return _build_game_start_high_snr_payload(req, store)
    return _build_midgame_high_snr_payload(req, store)


def build_coach_prompt_payload(
    req: CoachRequestPayload,
    store: DataStore | None,
    *,
    high_snr: bool = False,
) -> dict[str, Any]:
    """Assemble prefix-cache-friendly, compact, ID-grounded match snapshot JSON for LLM reasoning.

    Pass `high_snr=True` (or call `build_high_snr_prompt_payload`) for the v1.5.1 P0-8 3-Tier
    Trigger-Aware High-SNR Payload Slicer. Default `high_snr=False` preserves v1.5.0 dict schema
    for backward-compatible call sites.
    """
    if high_snr:
        return build_high_snr_prompt_payload(req, store)
    raw_my = dict(req.my_hero or {})
    hid = str(raw_my.get("hero_id") or "").strip()
    hero_obj = store.get_hero(hid) if (store is not None and hid) else None
    if hero_obj is None and store is not None and raw_my.get("name"):
        hero_obj = store.find_hero_by_name(str(raw_my["name"]))
        if hero_obj is not None and not hid:
            hid = hero_obj.hero_id

    hero_name = str(raw_my.get("name") or (hero_obj.name if hero_obj else "") or "")
    if "win_rate" in raw_my and raw_my["win_rate"] is not None:
        hero_win_rate = raw_my["win_rate"]
    elif hero_obj is not None:
        hero_win_rate = round(hero_obj.win_rate, 4)
    else:
        hero_win_rate = 0.5

    hero_static: dict[str, Any] = {
        "hero_id": hid,
        "name": hero_name,
        "win_rate": hero_win_rate,
    }

    big_data_builds: list[dict[str, Any]] = []
    glossary: dict[str, str] = {}

    def _add_glossary_item(raw_id: Any) -> None:
        if store is None or not store.static.items:
            return
        extracted = _extract_item_id_token(raw_id)
        if not extracted:
            return
        nid = normalize_item_id(extracted)
        if not nid or nid in glossary:
            return
        info = store.get_item_info(nid)
        if (not info.name or info.name.startswith("装备#")) and extracted != nid:
            alt = store.get_item_info(extracted)
            if alt.name and not alt.name.startswith("装备#"):
                nid = extracted
                info = alt
        if info.name and not info.name.startswith("装备#") and nid not in glossary:
            glossary[nid] = f"{info.name}|{info.gold}"

    if store is not None and (store.is_loaded or store.static.items):
        # 1) Match-invariant static glossary entries first (prefix-cache stable across turns)
        if hid:
            for b in store.get_core_builds(hid)[:3]:
                names = []
                for iid in b.items:
                    _add_glossary_item(iid)
                    names.append(f"{store.get_item_info(iid).name}({normalize_item_id(iid)})")
                big_data_builds.append(
                    {
                        "items": names,
                        "win_rate": round(b.win_rate, 4),
                        "pick_rate": round(b.pick_rate, 4),
                        "games": b.games,
                    }
                )
        for iid in (req.hero_signature_hint or {}).get("starter_items") or []:
            _add_glossary_item(iid)
        for iid in (req.hero_signature_hint or {}).get("signature_items") or []:
            _add_glossary_item(iid)
        for iid in (req.hero_signature_hint or {}).get("avoid_passive") or []:
            _add_glossary_item(iid)
        for iid in _CORE_COUNTER_GLOSSARY_IDS:
            _add_glossary_item(iid)

        # 2) Dynamic turn entries appended at the tail of item_glossary
        for sp in req.special_augment_builds or []:
            if not isinstance(sp, dict):
                continue
            for iid in sp.get("force_items") or []:
                _add_glossary_item(iid)
            for iid in sp.get("core_items") or []:
                _add_glossary_item(iid)
        for rec in (req.local_engine_top_items or [])[:4]:
            if isinstance(rec, dict):
                _add_glossary_item(rec.get("item_id", ""))

    # Resolve canonical item lookup once for both inventory analysis and roster cheap-component filtering
    _, name_to_id = _canonical_item_lookup(store) if (store is not None and store.static.items) else ({}, {})

    # Clean items & deduplicate self out of allies BEFORE stripping cheap components (preserves fallback item match)
    my_items_clean = _clean_item_names(raw_my.get("items"))
    deduped_allies_raw, self_kda = _dedup_allies(req.allies or [], raw_my, my_items_clean, store)
    cleaned_enemies_raw = [_clean_roster_entry(e) for e in (req.enemies or []) if isinstance(e, dict)]

    is_routine_death = (req.trigger_reason or "").strip().lower() == "death_enemy_build_changed"
    deduped_allies = [
        _compact_roster_entry(a, store=store, name_to_id=name_to_id, strip_spells=is_routine_death)
        for a in deduped_allies_raw
    ]
    cleaned_enemies = [
        _compact_roster_entry(e, store=store, name_to_id=name_to_id, strip_spells=False)
        for e in cleaned_enemies_raw
    ]

    my_hero_out: dict[str, Any] = {}
    if hero_name:
        my_hero_out["name"] = hero_name
    if hid and hero_name in ("", "未锁定", "未知"):
        my_hero_out["hero_id"] = hid
    for k, v in raw_my.items():
        if k in ("hero_id", "win_rate", "name", "items"):
            continue
        if v is None or v == "" or v == []:
            continue
        my_hero_out[k] = list(v) if isinstance(v, list) else v
    if self_kda and "kda" not in my_hero_out:
        my_hero_out["kda"] = self_kda
    # Keep my_hero.items intact (including cheap components) so buy_now computes exact upgrade cost
    my_hero_out["items"] = my_items_clean

    # Attach exact 6-slot inventory status so LLM never pushes an unjustified 7th item
    inv_status: dict[str, Any] = {
        "status": "building",
        "complete_count": len(_clean_item_names((req.committed_build_plan or {}).get("owned_complete"))),
        "has_tier2_boots": False,
        "boots_name": "",
        "boots_sell_gold": 0,
        "recommended_elixir": {"item_id": "2139", "name": "巫术合剂", "gold": 500},
    }
    if store is not None and store.static.items:
        owned_ids = [name_to_id[nm] for nm in my_items_clean if nm in name_to_id]
        sel_augs = [str(a.get("id", "")) for a in (req.selected_augments or []) if a.get("id")]
        prof = _infer_hero_profile(store, hid, owned_ids, sel_augs) if hid else "AP"
        inv = analyze_inventory_slots(store, owned_ids, profile=prof)
        inv_status = {
            "status": (
                "full_6_complete"
                if inv.is_six_complete
                else ("full_5_plus_boots" if inv.is_full_build_with_boots else "building")
            ),
            "complete_count": len(inv.complete_items),
            "has_tier2_boots": bool(inv.tier2_boots_id),
            "boots_name": inv.tier2_boots_name,
            "boots_sell_gold": int(inv.tier2_boots_gold * 0.70),
            "recommended_elixir": {
                "item_id": inv.recommended_elixir_id,
                "name": inv.recommended_elixir_name,
                "gold": 500,
            },
        }
    if inv_status["status"] == "building":
        compact_inv: dict[str, Any] = {
            "status": "building",
            "complete_count": inv_status["complete_count"],
            "has_tier2_boots": inv_status["has_tier2_boots"],
        }
        if inv_status["boots_name"]:
            compact_inv["boots_name"] = inv_status["boots_name"]
    else:
        compact_inv = {
            "status": inv_status["status"],
            "complete_count": inv_status["complete_count"],
            "has_tier2_boots": inv_status["has_tier2_boots"],
            "recommended_elixir": inv_status["recommended_elixir"],
        }
        if inv_status["has_tier2_boots"]:
            if inv_status["boots_name"]:
                compact_inv["boots_name"] = inv_status["boots_name"]
            if inv_status["boots_sell_gold"]:
                compact_inv["boots_sell_gold"] = inv_status["boots_sell_gold"]

    raw_plan = dict(req.committed_build_plan or {})
    compact_committed_plan: dict[str, Any] = {}
    if raw_plan:
        owned_complete_clean = _clean_item_names(raw_plan.get("owned_complete"))
        if owned_complete_clean:
            compact_committed_plan["owned_complete"] = owned_complete_clean
        locked_clean = _prune_empty(raw_plan.get("locked_next_item"))
        if locked_clean:
            compact_committed_plan["locked_next_item"] = locked_clean
        remaining_clean = _prune_empty(raw_plan.get("remaining_plan"))
        if remaining_clean:
            compact_committed_plan["remaining_plan"] = remaining_clean
        allow_overhaul_val = bool(raw_plan["allow_overhaul"]) if "allow_overhaul" in raw_plan else False
        if "allow_overhaul" in raw_plan:
            compact_committed_plan["allow_overhaul"] = allow_overhaul_val
        if "transformative_overhaul" in raw_plan:
            compact_committed_plan["transformative_overhaul"] = bool(raw_plan["transformative_overhaul"])
        overhaul_reason = str(raw_plan.get("overhaul_reason") or "").strip()
        if overhaul_reason and (allow_overhaul_val or "allow_overhaul" not in raw_plan):
            compact_committed_plan["overhaul_reason"] = overhaul_reason
        rec_boots = str(raw_plan.get("recommended_boots") or "").strip()
        if rec_boots:
            compact_committed_plan["recommended_boots"] = rec_boots
        for k, v in raw_plan.items():
            if k not in (
                "owned_complete",
                "locked_next_item",
                "remaining_plan",
                "allow_overhaul",
                "transformative_overhaul",
                "overhaul_reason",
                "recommended_boots",
            ):
                cleaned_v = _prune_empty(v)
                if cleaned_v not in ("", None, [], {}):
                    compact_committed_plan[k] = cleaned_v

    compact_local_items: list[dict[str, Any]] = []
    for rec in (req.local_engine_top_items or [])[:4]:
        if not isinstance(rec, dict):
            continue
        entry: dict[str, Any] = {}
        if rec.get("item_id") is not None:
            entry["item_id"] = str(rec["item_id"])
        if rec.get("name"):
            entry["name"] = str(rec["name"])
        price = rec.get("price")
        eff_cost = rec.get("effective_cost")
        if eff_cost is not None and price is not None and eff_cost == price:
            entry["effective_cost"] = eff_cost
        else:
            if price is not None:
                entry["price"] = price
            if eff_cost is not None:
                entry["effective_cost"] = eff_cost
        if rec.get("affordable") is True:
            entry["affordable"] = True
        buy_hint = str(rec.get("buy_now_hint") or "").strip()
        if buy_hint:
            entry["buy_now_hint"] = buy_hint
        raw_reasons = [str(r).strip() for r in (rec.get("reasons") or []) if str(r).strip()]
        if raw_reasons:
            entry["reasons"] = raw_reasons[:2]
        for k, v in rec.items():
            if k not in (
                "item_id",
                "name",
                "price",
                "effective_cost",
                "affordable",
                "buy_now_hint",
                "reasons",
            ):
                cleaned_v = _prune_empty(v)
                if cleaned_v not in ("", None, [], {}):
                    entry[k] = cleaned_v
        if entry:
            compact_local_items.append(entry)

    compact_sig_hint = _prune_empty(req.hero_signature_hint) if req.hero_signature_hint else {}
    compact_special_builds = _compact_special_augment_builds(
        req.special_augment_builds or [],
        trigger_reason=req.trigger_reason,
    )
    compact_combo_ctx = _compact_augment_combo_context(
        req.augment_combo_context or {},
        trigger_reason=req.trigger_reason,
        has_offered=bool(req.offered_augments),
    )
    compact_lobby_scout = _compact_lobby_scout(req.lobby_scout)

    # Assemble final payload: static match-invariant prefix FIRST, volatile state at tail
    data: dict[str, Any] = {
        "dataset_patch": req.dataset_patch,
        "hero_static": hero_static,
        "hero_big_data_top_builds": big_data_builds,
    }
    if compact_sig_hint:
        data["hero_signature_hint"] = compact_sig_hint
    data["item_glossary"] = glossary

    data["trigger_reason"] = req.trigger_reason
    data["my_hero"] = my_hero_out
    data["inventory_status"] = compact_inv
    data["selected_augments"] = list(req.selected_augments or [])
    if req.offered_augments:
        data["offered_augments"] = list(req.offered_augments)
    if compact_special_builds:
        data["special_augment_builds"] = compact_special_builds
    if compact_combo_ctx:
        data["augment_combo_context"] = compact_combo_ctx
    data["committed_build_plan"] = compact_committed_plan
    data["local_engine_top_items"] = compact_local_items
    data["threat_summary"] = list(req.threat_summary or [])
    if compact_lobby_scout:
        data["lobby_scout"] = compact_lobby_scout
    data["allies"] = deduped_allies
    data["enemies"] = cleaned_enemies
    return data


def build_picker_fast_prompt_payload(
    req: CoachRequestPayload,
    store: DataStore | None = None,
    *,
    compact_comp: bool = False,
) -> dict[str, Any]:
    """Assemble an ultra-compact (~500-680 token) augment-picking payload for sub-2s Flash inference.

    Keeps ONLY decision-critical augment picker context: hero_static, selected_augments,
    3/4 offered_augments with tier/win_rate/delta/combo hints, my_hero.crit_chance_pct (>0),
    compact combo/special build direction, and 1-line comp_summary — stripping item_glossary,
    hero_big_data_top_builds, full 10-player item histories, and verbose prose.
    """
    raw_my = dict(req.my_hero or {})
    hid = str(raw_my.get("hero_id") or "").strip()
    hero_obj = store.get_hero(hid) if (store is not None and hid) else None
    if hero_obj is None and store is not None and raw_my.get("name"):
        hero_obj = store.find_hero_by_name(str(raw_my["name"]))
        if hero_obj is not None and not hid:
            hid = hero_obj.hero_id

    hero_name = str(raw_my.get("name") or (hero_obj.name if hero_obj else "") or "")
    if "win_rate" in raw_my and raw_my["win_rate"] is not None:
        hero_win_rate = raw_my["win_rate"]
    elif hero_obj is not None:
        hero_win_rate = round(hero_obj.win_rate, 4)
    else:
        hero_win_rate = 0.5

    hero_static: dict[str, Any] = {
        "hero_id": hid,
        "name": hero_name,
        "win_rate": hero_win_rate,
    }
    archetype = str((req.hero_signature_hint or {}).get("archetype") or "").strip()
    if archetype:
        hero_static["archetype"] = archetype

    _, name_to_id = _canonical_item_lookup(store) if (store is not None and store.static.items) else ({}, {})
    my_items_clean = _clean_item_names(raw_my.get("items"))
    my_compact: dict[str, Any] = {}
    if hero_name:
        my_compact["name"] = hero_name
    elif hid:
        my_compact["hero_id"] = hid
    if raw_my.get("level") is not None:
        my_compact["level"] = raw_my["level"]
    if raw_my.get("gold") is not None:
        my_compact["gold"] = raw_my["gold"]
    if raw_my.get("kda"):
        my_compact["kda"] = str(raw_my["kda"]).strip()
    crit_pct = _compute_crit_chance_pct(
        raw_my,
        my_items_clean,
        req.selected_augments or [],
        store,
        hid,
        name_to_id,
    )
    if crit_pct > 0:
        my_compact["crit_chance_pct"] = crit_pct
    if my_items_clean:
        my_compact["items"] = my_items_clean[:6]

    selected_compact: list[dict[str, Any]] = []
    for aug in req.selected_augments or []:
        if not isinstance(aug, dict):
            continue
        entry = _prune_empty(
            {
                "id": str(aug.get("id") or ""),
                "name": str(aug.get("name") or ""),
                "rarity": str(aug.get("rarity") or ""),
            }
        )
        if entry:
            selected_compact.append(entry)

    offered_compact: list[dict[str, Any]] = []
    for card in (req.offered_augments or [])[:4]:
        if not isinstance(card, dict):
            continue
        cleaned_card = _prune_empty(card)
        if cleaned_card:
            offered_compact.append(cleaned_card)

    combo_compact = _compact_augment_combo_context(
        req.augment_combo_context or {},
        trigger_reason="augment_picking",
        has_offered=bool(offered_compact),
    )

    special_compact = _format_compact_special_builds(req.special_augment_builds or [])

    raw_plan = req.committed_build_plan if isinstance(req.committed_build_plan, dict) else {}
    plan_compact: dict[str, Any] = {}
    owned_comp = _clean_item_names(raw_plan.get("owned_complete"))
    if owned_comp:
        plan_compact["owned_complete"] = owned_comp
    locked_next = _prune_empty(raw_plan.get("locked_next_item"))
    if locked_next:
        plan_compact["locked_next_item"] = locked_next

    deduped_allies, self_kda = _dedup_allies(req.allies or [], raw_my, my_items_clean, store)
    if self_kda and "kda" not in my_compact:
        my_compact["kda"] = self_kda

    cleaned_enemies_raw = [_clean_roster_entry(e) for e in (req.enemies or []) if isinstance(e, dict)]
    my_roster_info = (
        _classify_roster_player(
            {"champion": hero_name, "hero_id": hid, "kda": my_compact.get("kda", ""), "items": my_items_clean},
            store,
            name_to_id,
        )
        if (hero_name or hid)
        else None
    )
    ally_infos = [_classify_roster_player(a, store, name_to_id) for a in deduped_allies]
    enemy_infos = [_classify_roster_player(e, store, name_to_id) for e in cleaned_enemies_raw]
    comp_summary = _build_comp_summary(
        my_roster_info,
        ally_infos,
        enemy_infos,
        list(req.threat_summary or []),
        is_game_start=False,
    )

    enemy_champs = [
        str(e.get("champion") or e.get("name") or "").strip()
        for e in (req.enemies or [])
        if isinstance(e, dict) and str(e.get("champion") or e.get("name") or "").strip()
    ][:5]
    ally_champs = [
        str(a.get("champion") or a.get("name") or "").strip()
        for a in deduped_allies
        if isinstance(a, dict) and str(a.get("champion") or a.get("name") or "").strip()
    ][:4]
    threats = [str(t).strip() for t in (req.threat_summary or []) if str(t).strip()][:6]

    data: dict[str, Any] = {
        "dataset_patch": req.dataset_patch,
        "hero_static": hero_static,
        "trigger_reason": req.trigger_reason,
    }
    if my_compact:
        data["my_hero"] = my_compact
    data["selected_augments"] = selected_compact
    data["offered_augments"] = offered_compact
    if combo_compact:
        data["augment_combo_context"] = combo_compact
    if special_compact:
        data["special_augment_builds"] = special_compact
    if plan_compact:
        data["committed_build_plan"] = plan_compact
    if comp_summary:
        data["comp_summary"] = comp_summary
    if threats:
        data["threat_summary"] = threats
    if not compact_comp:
        if enemy_champs:
            data["enemy_champs"] = enemy_champs
        if ally_champs:
            data["ally_champs"] = ally_champs
    return data


def _is_explicit_swap_item(priority: str, reason: str) -> bool:
    """Return True iff the coach explicitly justified selling an owned item/boots to swap in a mandatory counter."""
    p = priority or ""
    r = reason or ""
    return ("换" in p) or ("卖" in p) or ("卖" in r) or ("换" in r) or ("替换" in r)


def _harvest_local_top_item_catalog(
    local_items: list[Any] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Extract (id->name, name->id) fallback catalog from dict or pipe-delimited local_engine_top_items."""
    id_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    for rec in local_items or []:
        iid = ""
        nm = ""
        if isinstance(rec, dict):
            iid = canonicalize_item_id(str(rec.get("item_id") or "").strip())
            nm = str(rec.get("name") or "").strip()
        elif isinstance(rec, str) and "|" in rec:
            parts = [p.strip() for p in rec.split("|")]
            if len(parts) >= 2:
                iid = canonicalize_item_id(parts[0])
                nm = parts[1]
        if iid and nm and not nm.startswith("装备#"):
            id_map.setdefault(iid, nm)
            name_map.setdefault(nm, iid)
    return id_map, name_map


_BUILD_PATH_DICT_NAME_RE = re.compile(r"""['"]name['"]\s*:\s*['"]([^'"]+)['"]""")
_BUILD_PATH_DICT_ID_RE = re.compile(r"""['"](?:item_id|id)['"]\s*:\s*['"]?([^'",}\s]+)['"]?""")


def _parse_build_path_step(step: Any) -> tuple[str, str]:
    """Extract (canonical_pipe_id, name_or_raw_text) from str, dict, repr-dict, or pipe token."""
    raw_id_part = ""
    raw_name_part = ""
    if isinstance(step, dict):
        raw_id_part = str(step.get("item_id") or step.get("id") or "").strip()
        raw_name_part = str(step.get("name") or "").strip()
    else:
        raw_str = str(step).strip()
        if not raw_str:
            return "", ""
        if raw_str.startswith("{") and raw_str.endswith("}"):
            try:
                parsed = json.loads(raw_str)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                return _parse_build_path_step(parsed)
            m_name = _BUILD_PATH_DICT_NAME_RE.search(raw_str)
            m_id = _BUILD_PATH_DICT_ID_RE.search(raw_str)
            if m_name or m_id:
                raw_name_part = m_name.group(1).strip() if m_name else ""
                raw_id_part = m_id.group(1).strip() if m_id else ""
            else:
                raw_name_part = raw_str
        else:
            raw_name_part = raw_str

    if "|" in raw_id_part:
        raw_id_part = raw_id_part.split("|", 1)[0].strip()
    pipe_id = canonicalize_item_id(_extract_item_id_token(raw_id_part)) if raw_id_part else ""

    if "|" in raw_name_part:
        parts = [p.strip() for p in raw_name_part.split("|") if p.strip()]
        if len(parts) >= 2 and parts[0].isdigit():
            if not pipe_id:
                pipe_id = canonicalize_item_id(parts[0])
            s_raw = parts[1]
        elif parts:
            s_raw = parts[0]
        else:
            s_raw = ""
    else:
        s_raw = raw_name_part or raw_id_part
    return pipe_id, s_raw


def ground_coach_advice(
    raw_json: dict[str, Any],
    req: CoachRequestPayload,
    store: DataStore | None,
    model: str = "gemini-4-hydrogen",
    thinking_level: str = "medium",
    stats_out: dict[str, int] | None = None,
) -> CoachAdvicePayload:
    """Validate, canonicalize item IDs/names against DataStore, drop owned duplicates, and guard 6-slot full builds."""
    id_to_name, name_to_id = _canonical_item_lookup(store)
    local_id_to_name, local_name_to_id = _harvest_local_top_item_catalog(req.local_engine_top_items)
    for lid, lnm in local_id_to_name.items():
        id_to_name.setdefault(lid, lnm)
    for lnm, lid in local_name_to_id.items():
        name_to_id.setdefault(lnm, lid)

    unknown_item_blocked = 0
    already_owned_blocked = 0
    full_build_7th_item_blocked = 0
    invalid_route_blocked = 0

    # Resolve owned complete items so Gemini can never recommend finishing an already-owned completed item
    owned_complete_norm: set[str] = set()
    owned_complete_names: set[str] = set()
    raw_owned_list = [str(x) for x in ((req.my_hero or {}).get("items") or [])]
    raw_owned_names = set(raw_owned_list)
    for nm in (req.committed_build_plan or {}).get("owned_complete") or []:
        owned_complete_names.add(str(nm))
        if nm in name_to_id:
            owned_complete_norm.add(normalize_item_id(name_to_id[nm]))

    inv_full_build = False
    default_elixir_id = "2139"
    default_elixir_name = "巫术合剂"
    if store is not None and store.static.items:
        resolved_owned_ids: list[str] = []
        for nm in raw_owned_list:
            cand_id = name_to_id.get(nm)
            if cand_id:
                resolved_owned_ids.append(cand_id)
                info = store.get_item_info(cand_id)
                if info.category == "complete_equipment" or info.gold >= 2200:
                    owned_complete_names.add(info.name)
                    owned_complete_norm.add(normalize_item_id(cand_id))
        hid = str((req.my_hero or {}).get("hero_id") or "")
        sel_augs = [str(a.get("id", "")) for a in (req.selected_augments or []) if isinstance(a, dict) and a.get("id")]
        prof = _infer_hero_profile(store, hid, resolved_owned_ids, sel_augs) if hid else "AP"
        inv = analyze_inventory_slots(store, resolved_owned_ids, profile=prof)
        inv_full_build = inv.is_full_build or len(owned_complete_names) >= 6
        default_elixir_id = inv.recommended_elixir_id
        default_elixir_name = inv.recommended_elixir_name
    elif len(owned_complete_names) >= 6:
        inv_full_build = True

    grounded_items: list[CoachAdviceItem] = []
    seen_item_ids: set[str] = set()
    has_store_catalog = bool(store is not None and store.static.items) or bool(id_to_name)
    for it in raw_json.get("next_items") or []:
        if not isinstance(it, dict):
            unknown_item_blocked += 1
            continue
        raw_id_str = str(it.get("item_id", "")).strip()
        if "|" in raw_id_str:
            raw_id_str = raw_id_str.split("|", 1)[0].strip()
        raw_id = canonicalize_item_id(_extract_item_id_token(raw_id_str))
        raw_name_orig = str(it.get("name", "")).strip()
        if "|" in raw_name_orig:
            parts_nm = [p.strip() for p in raw_name_orig.split("|") if p.strip()]
            if parts_nm and parts_nm[0].isdigit() and len(parts_nm) >= 2:
                if not raw_id:
                    raw_id = canonicalize_item_id(parts_nm[0])
                raw_name_orig = parts_nm[1]
            elif parts_nm:
                raw_name_orig = parts_nm[0]
        raw_name_clean = _PAREN_ITEM_ID_RE.sub("", raw_name_orig).strip()
        if not raw_id:
            m_id = _PAREN_ITEM_ID_RE.search(raw_name_orig)
            if m_id:
                raw_id = canonicalize_item_id(m_id.group(1))
        raw_name = _ITEM_NICKNAME_ALIASES.get(raw_name_clean, raw_name_clean)
        if (
            raw_id
            and raw_id in id_to_name
            and not (len(raw_id) == 4 and raw_id.startswith("71") and raw_name and raw_name in name_to_id)
        ):
            canon_id = raw_id
            canon_name = id_to_name[canon_id]
        elif raw_name and raw_name in name_to_id:
            canon_id = canonicalize_item_id(name_to_id[raw_name])
            canon_name = id_to_name.get(canon_id, raw_name)
        elif has_store_catalog:
            # Drop hallucinated/deleted item IDs (e.g. legacy 3151 if unmapped, or non-existent shop items)
            unknown_item_blocked += 1
            logger.debug("Dropping ungrounded Gemini item: id=%r name=%r", raw_id, raw_name_orig)
            continue
        else:
            canon_id = raw_id
            canon_name = raw_name or raw_id
        if not canon_name or canon_name.startswith("装备#"):
            unknown_item_blocked += 1
            continue
        if (canon_id and canon_id in owned_complete_norm) or (canon_name in owned_complete_names):
            already_owned_blocked += 1
            continue
        priority_str = str(it.get("priority") or ("下件必出" if not grounded_items else "次选")).strip()
        reason_str = str(it.get("reason", "")).strip()
        if inv_full_build and canon_id not in ELIXIR_IDS and not _is_explicit_swap_item(priority_str, reason_str):
            # Refuse unjustified 7th major item when all 6 slots are already occupied!
            full_build_7th_item_blocked += 1
            logger.debug("Dropping unjustified 7th item at full build: %s (%s)", canon_name, reason_str)
            continue
        if canon_id and canon_id in seen_item_ids:
            continue
        if canon_id:
            seen_item_ids.add(canon_id)
        grounded_items.append(
            CoachAdviceItem(
                item_id=canon_id,
                name=canon_name,
                priority=priority_str,
                reason=reason_str,
            )
        )

    buy_now_out = str(raw_json.get("buy_now", "")).strip()
    if inv_full_build and not grounded_items:
        grounded_items.append(
            CoachAdviceItem(
                item_id=default_elixir_id,
                name=default_elixir_name,
                priority="满格合剂",
                reason="六神装已满，优先磕合剂",
            )
        )
        my_gold = float((req.my_hero or {}).get("gold") or 0.0)
        buy_now_out = (
            f"已满六神 · 优先磕【{default_elixir_name}】(500G)"
            if my_gold >= 500
            else f"已满六神 · 差 {max(0, 500 - int(my_gold))}G 磕【{default_elixir_name}】"
        )

    build_path: list[str] = []
    raw_build_path = raw_json.get("build_path") or []
    if not inv_full_build:
        for step in raw_build_path:
            pipe_id, s_raw = _parse_build_path_step(step)
            s = _ITEM_NICKNAME_ALIASES.get(s_raw, s_raw)
            if not s or s.startswith("装备#"):
                if str(step).strip():
                    invalid_route_blocked += 1
                continue
            if has_store_catalog and store is not None and store.static.items:
                cid = name_to_id.get(s) or ""
                if (
                    not cid
                    and pipe_id
                    and pipe_id in id_to_name
                    and not (len(pipe_id) == 4 and pipe_id.startswith("71") and s in name_to_id)
                ):
                    cid = pipe_id
                if not cid and s.isdigit():
                    cid = canonicalize_item_id(s)
                if not cid:
                    m_pid = _PAREN_ITEM_ID_RE.search(s)
                    if m_pid:
                        cand_cid = canonicalize_item_id(m_pid.group(1))
                        if cand_cid in id_to_name:
                            cid = cand_cid
                if cid:
                    norm_cid = normalize_item_id(cid)
                    cinfo = store.get_item_info(cid)
                    if (
                        norm_cid in TIER2_BOOT_IDS
                        or cinfo.category == "boots"
                        or "Boots" in (cinfo.tags or ())
                    ):
                        # Tier-2/boots belong in buy_now/boots hint rather than build_path;
                        # strip silently without counting as an invalid_route_blocked hallucination.
                        continue
                if not cid or not is_valid_plan_item(store, cid):
                    invalid_route_blocked += 1
                    continue
                s = id_to_name.get(cid, s)
                if cid in owned_complete_norm:
                    already_owned_blocked += 1
                    continue
            if s in owned_complete_names:
                already_owned_blocked += 1
                continue
            if s in build_path:
                invalid_route_blocked += 1
                continue
            build_path.append(s)
    else:
        for step in raw_build_path:
            if str(step).strip():
                invalid_route_blocked += 1

    if stats_out is not None:
        total_blocked = (
            unknown_item_blocked
            + already_owned_blocked
            + full_build_7th_item_blocked
            + invalid_route_blocked
        )
        stats_out.update(
            {
                "unknown_item_blocked": unknown_item_blocked,
                "already_owned_blocked": already_owned_blocked,
                "full_build_7th_item_blocked": full_build_7th_item_blocked,
                "invalid_route_blocked": invalid_route_blocked,
                "total_blocked": total_blocked,
            }
        )

    pick_advice = str(raw_json.get("augment_pick_advice", "")).strip()
    if not req.offered_augments:
        pick_advice = ""

    raw_next_goal = raw_json.get("next_augment_goal")
    next_aug_goal = "" if raw_next_goal is None else str(raw_next_goal).strip()
    reason_norm = (req.trigger_reason or "").strip().lower()
    if len(req.selected_augments or []) >= 4 or reason_norm == "augment_picking" or bool(req.offered_augments):
        next_aug_goal = ""

    return CoachAdvicePayload(
        request_id=req.request_id,
        next_items=grounded_items[:3],
        buy_now=buy_now_out,
        build_path=build_path[:6],
        augment_pick_advice=pick_advice,
        tactical_summary=str(raw_json.get("tactical_summary", "")).strip(),
        updated_at=utc_now_iso(),
        source=f"Gemini ({model} · {thinking_level})",
        opening_brief=normalize_opening_brief(raw_json.get("opening_brief")),
        next_augment_goal=next_aug_goal,
    )


_TRANSIENT_CONN_ERROR_NAMES = (
    "RemoteProtocolError",
    "ReadError",
    "ConnectError",
    "WriteError",
    "CloseError",
    "ConnectionResetError",
    "BrokenPipeError",
    "EOFError",
)


_UNSUPPORTED_MINIMAL_MODELS_LOCK = threading.Lock()
_UNSUPPORTED_MINIMAL_MODELS: set[str] = set()

_COACH_TRIGGER_TIMEOUTS_S: dict[str, float] = {
    "augment_picking": 12.0,
    "pre_death_warmup": 18.0,
    "death_enemy_build_changed": 20.0,
}
_DEFAULT_COACH_TIMEOUT_S: float = 30.0


def resolve_coach_timeout_s(trigger_reason: str) -> float:
    """Return trigger-scoped wall-clock HTTP timeout (seconds) for Gemini coach requests."""
    reason = (trigger_reason or "").strip().lower()
    return _COACH_TRIGGER_TIMEOUTS_S.get(reason, _DEFAULT_COACH_TIMEOUT_S)


def _normalize_model_key(model: str) -> str:
    norm = (model or "").strip().lower()
    if norm.startswith("models/"):
        norm = norm[len("models/") :].strip()
    return norm


def _is_model_cached_no_minimal(model: str) -> bool:
    norm = _normalize_model_key(model)
    raw = (model or "").strip()
    with _UNSUPPORTED_MINIMAL_MODELS_LOCK:
        return (norm in _UNSUPPORTED_MINIMAL_MODELS) or (raw in _UNSUPPORTED_MINIMAL_MODELS)


def _cache_model_no_minimal(model: str) -> None:
    norm = _normalize_model_key(model)
    if not norm:
        return
    with _UNSUPPORTED_MINIMAL_MODELS_LOCK:
        _UNSUPPORTED_MINIMAL_MODELS.add(norm)


def _extract_error_status_code(exc: BaseException) -> int | None:
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for attr in ("status_code", "code"):
            val = getattr(cur, attr, None)
            if isinstance(val, int) and not isinstance(val, bool) and 100 <= val < 600:
                return val
        resp = getattr(cur, "response", None)
        if resp is not None:
            r_status = getattr(resp, "status_code", None)
            if isinstance(r_status, int) and not isinstance(r_status, bool) and 100 <= r_status < 600:
                return r_status
        cur = cur.__cause__ or cur.__context__
    return None


def _is_thinking_level_400_error(exc: Exception) -> bool:
    """Return True iff exc is an HTTP 400 / BadRequest rejecting thinking_level='minimal'."""
    status = _extract_error_status_code(exc)
    if status is not None and status != 400:
        return False
    is_bad_request = status == 400
    parts: list[str] = []
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        cname = type(cur).__name__
        if cname in ("BadRequestError", "ClientError", "InvalidArgument"):
            is_bad_request = True
        parts.append(f"{cname}: {cur}")
        body = getattr(cur, "body", None)
        if body is not None:
            parts.append(str(body))
        cur = cur.__cause__ or cur.__context__
    msg = " ".join(parts)
    if not is_bad_request:
        if not re.search(r"(?:\b400\b|INVALID_ARGUMENT|BadRequest)", msg, re.IGNORECASE):
            return False
    upper = msg.upper()
    if "THINKING_LEVEL_MINIMAL" in upper:
        return True
    lower = msg.lower()
    has_thinking_level = ("thinking level" in lower) or ("thinking_level" in lower)
    has_unsupported = (
        ("not supported" in lower)
        or ("unsupported" in lower)
        or ("not allowed" in lower)
        or ("invalid" in lower)
    )
    return has_thinking_level and has_unsupported


def _is_transient_connection_error(exc: Exception) -> bool:
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _TRANSIENT_CONN_ERROR_NAMES or isinstance(cur, (ConnectionError, EOFError)):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class GeminiCoachClient:
    """Persistent-connection wrapper around the official google-genai Interactions API with proxy routing."""

    def __init__(self, client_factory: Callable[[str, str, float], Any] | None = None) -> None:
        self._client_factory = client_factory
        self._pool_lock = threading.Lock()
        self._cached_key: tuple[str, str, int] | None = None
        self._cached_client: Any = None

    @staticmethod
    def _default_client_factory(api_key: str, proxy_url: str, timeout_s: float) -> Any:
        from google import genai
        from google.genai import types

        http_kwargs: dict[str, Any] = {"timeout": max(5000, int(timeout_s * 1000))}
        clean_proxy = (proxy_url or "").strip()
        if clean_proxy:
            http_kwargs["client_args"] = {"proxy": clean_proxy}
        return genai.Client(
            api_key=api_key.strip(),
            http_options=types.HttpOptions(**http_kwargs),
        )

    @staticmethod
    def _safe_close(client: Any) -> None:
        close_fn = getattr(client, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:
                pass

    def _get_pooled_client(self, api_key: str, proxy_url: str, timeout_s: float) -> tuple[Any, bool]:
        """Return (client, reused_from_pool), keeping TLS/HTTP2 tunnel warm across match events."""
        key = (api_key.strip(), (proxy_url or "").strip(), int(round(timeout_s)))
        with self._pool_lock:
            if self._cached_client is not None and self._cached_key == key:
                return self._cached_client, True
            old = self._cached_client
            self._cached_client = None
            self._cached_key = None
        if old is not None:
            self._safe_close(old)

        factory = self._client_factory or self._default_client_factory
        fresh = factory(key[0], key[1], float(timeout_s))
        redundant: Any = None
        with self._pool_lock:
            if self._cached_client is not None and self._cached_key == key:
                redundant = fresh
                selected = self._cached_client
                reused = True
            else:
                self._cached_client = fresh
                self._cached_key = key
                selected = fresh
                reused = False
        if redundant is not None:
            self._safe_close(redundant)
        return selected, reused

    def _evict_client(self, client: Any) -> None:
        with self._pool_lock:
            if self._cached_client is client:
                self._cached_client = None
                self._cached_key = None
        self._safe_close(client)

    def close(self) -> None:
        with self._pool_lock:
            old = self._cached_client
            self._cached_client = None
            self._cached_key = None
        if old is not None:
            self._safe_close(old)

    def _create_interaction_resilient(
        self,
        *,
        api_key: str,
        proxy_url: str,
        timeout_s: float,
        create_kwargs: dict[str, Any],
    ) -> Any:
        client, reused = self._get_pooled_client(api_key, proxy_url, timeout_s)
        try:
            return client.interactions.create(**create_kwargs)
        except Exception as exc:
            if reused and _is_transient_connection_error(exc):
                logger.info("Pooled Gemini socket reset (%s); reconnecting once", type(exc).__name__)
                self._evict_client(client)
                fresh, _ = self._get_pooled_client(api_key, proxy_url, timeout_s)
                return fresh.interactions.create(**create_kwargs)
            raise

    def advise(
        self,
        req: CoachRequestPayload,
        store: DataStore | None,
        *,
        api_key: str,
        model: str = "gemini-4-hydrogen",
        thinking_level: str = "medium",
        adaptive_thinking: bool = True,
        proxy_url: str = "",
        timeout_s: float | None = None,
        fast_picker_model: str = "",
        use_fast_picker_payload: bool | None = None,
        fast_death: bool = False,
    ) -> tuple[CoachAdvicePayload, GeminiCallStats]:
        if not api_key or not api_key.strip():
            raise ValueError("未配置 Gemini API Key")

        reason_norm = (req.trigger_reason or "").strip().lower()
        clean_fast_picker = (fast_picker_model or "").strip()
        is_picker_fast = (
            reason_norm == "augment_picking"
            and (
                use_fast_picker_payload is True
                or (use_fast_picker_payload is None and bool(clean_fast_picker))
            )
        )
        eff_model = clean_fast_picker if (reason_norm == "augment_picking" and clean_fast_picker) else model

        plan = req.committed_build_plan if isinstance(req.committed_build_plan, dict) else {}
        is_transformative: bool | None = (
            bool(plan.get("allow_overhaul", False))
            if reason_norm == "augment_selected" and "allow_overhaul" in plan
            else None
        )
        eff_thinking = resolve_thinking_level(
            thinking_level,
            req.trigger_reason,
            adaptive=adaptive_thinking,
            model=eff_model,
            is_transformative=is_transformative,
            fast_death=fast_death,
        )
        if eff_thinking == "minimal" and _is_model_cached_no_minimal(eff_model):
            eff_thinking = "low"
        if is_picker_fast:
            prompt_obj = build_picker_fast_prompt_payload(req, store, compact_comp=True)
            sys_instruction = PICKER_FAST_SYSTEM_INSTRUCTION
        else:
            prompt_obj = build_high_snr_prompt_payload(req, store)
            sys_instruction = COACH_SYSTEM_INSTRUCTION
        prompt_text = (
            "据以下对局JSON按Schema返回唯一JSON：\n"
            + json.dumps(prompt_obj, ensure_ascii=False, separators=(",", ":"))
        )

        # Keep pooled client timeout stable (90s default) so TLS/HTTP2 sockets stay warm across
        # triggers while enforcing trigger-scoped per-request timeout on interactions.create().
        pool_timeout_s = 90.0 if timeout_s is None else max(1.0, float(timeout_s))
        req_timeout_s = (
            resolve_coach_timeout_s(req.trigger_reason)
            if timeout_s is None
            else max(1.0, float(timeout_s))
        )

        t0 = time.monotonic()
        gen_cfg: dict[str, Any] = {"thinking_level": eff_thinking} if eff_thinking else {}
        create_kwargs: dict[str, Any] = {
            "model": eff_model,
            "system_instruction": sys_instruction,
            "input": prompt_text,
            "generation_config": gen_cfg or None,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": COACH_RESPONSE_SCHEMA,
            },
            "store": False,
            "timeout": req_timeout_s,
        }
        try:
            interaction = self._create_interaction_resilient(
                api_key=api_key.strip(),
                proxy_url=proxy_url,
                timeout_s=pool_timeout_s,
                create_kwargs=create_kwargs,
            )
        except Exception as exc:
            if eff_thinking == "minimal" and _is_thinking_level_400_error(exc):
                logger.warning(
                    "Gemini model %s rejected thinking_level=minimal (400); caching and retrying once with low",
                    eff_model,
                )
                _cache_model_no_minimal(eff_model)
                eff_thinking = "low"
                retry_kwargs = dict(create_kwargs)
                retry_kwargs["generation_config"] = {"thinking_level": "low"}
                interaction = self._create_interaction_resilient(
                    api_key=api_key.strip(),
                    proxy_url=proxy_url,
                    timeout_s=pool_timeout_s,
                    create_kwargs=retry_kwargs,
                )
            else:
                raise

        latency_ms = int((time.monotonic() - t0) * 1000)
        raw_text = _extract_interaction_text(interaction)
        status = getattr(interaction, "status", "completed")
        if not raw_text.strip():
            raise RuntimeError(f"Gemini 未返回有效文本 (status={status})")

        parsed = json.loads(_strip_json_fences(raw_text))
        grounding_stats: dict[str, int] = {}
        advice = ground_coach_advice(
            parsed,
            req,
            store,
            model=eff_model,
            thinking_level=eff_thinking,
            stats_out=grounding_stats,
        )
        total_tok, thought_tok, out_tok = _extract_usage_tokens(interaction)
        usage = getattr(interaction, "usage", None)
        input_tok = (
            int(getattr(usage, "total_input_tokens", 0) or getattr(usage, "input_tokens", 0) or 0)
            if usage is not None
            else 0
        )
        stats = GeminiCallStats(
            model=eff_model,
            thinking_level=eff_thinking,
            latency_ms=latency_ms,
            total_tokens=total_tok,
            thought_tokens=thought_tok,
            output_tokens=out_tok,
            grounding_stats=grounding_stats,
            input_tokens=input_tok,
        )
        return advice, stats

    def test_connection(
        self,
        *,
        api_key: str,
        model: str = "gemini-4-hydrogen",
        thinking_level: str = "medium",
        adaptive_thinking: bool = True,
        proxy_url: str = "",
        timeout_s: float = 45.0,
    ) -> tuple[bool, str, int]:
        """Run a real API roundtrip verifying API key, proxy, model, and warming up the connection pool."""
        if not api_key or not api_key.strip():
            return False, "请先填入 Gemini API Key（或设置 GEMINI_API_KEY 环境变量）", 0

        eff_thinking = resolve_thinking_level(
            thinking_level,
            "connection_test",
            adaptive=adaptive_thinking,
            model=model,
        )
        if eff_thinking == "minimal" and _is_model_cached_no_minimal(model):
            eff_thinking = "low"
        eff_max = resolve_thinking_level(
            thinking_level,
            "manual_f6",
            adaptive=adaptive_thinking,
            model=model,
        )
        t0 = time.monotonic()
        try:
            gen_cfg: dict[str, Any] = {"thinking_level": eff_thinking} if eff_thinking else {}
            create_kwargs: dict[str, Any] = {
                "model": model,
                "input": 'Reply with valid JSON: {"ok": true, "model": "' + model + '"}',
                "generation_config": gen_cfg or None,
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}, "model": {"type": "string"}},
                        "required": ["ok", "model"],
                    },
                },
                "store": False,
                "timeout": timeout_s,
            }
            try:
                interaction = self._create_interaction_resilient(
                    api_key=api_key.strip(),
                    proxy_url=proxy_url,
                    timeout_s=timeout_s,
                    create_kwargs=create_kwargs,
                )
            except Exception as exc:
                if eff_thinking == "minimal" and _is_thinking_level_400_error(exc):
                    logger.warning(
                        "Gemini model %s rejected thinking_level=minimal during connection_test (400); retrying once with low",
                        model,
                    )
                    _cache_model_no_minimal(model)
                    eff_thinking = "low"
                    retry_kwargs = dict(create_kwargs)
                    retry_kwargs["generation_config"] = {"thinking_level": "low"}
                    interaction = self._create_interaction_resilient(
                        api_key=api_key.strip(),
                        proxy_url=proxy_url,
                        timeout_s=timeout_s,
                        create_kwargs=retry_kwargs,
                    )
                else:
                    raise
            latency_ms = int((time.monotonic() - t0) * 1000)
            txt = _extract_interaction_text(interaction)
            data = json.loads(_strip_json_fences(txt))
            if data.get("ok"):
                total_tok, thought_tok, _ = _extract_usage_tokens(interaction)
                mode_tag = (
                    f"自检 {eff_thinking} · 局内自适应 low/{eff_max}"
                    if adaptive_thinking and eff_thinking != eff_max
                    else eff_thinking
                )
                return (
                    True,
                    f"连通成功：{model} ({mode_tag}) · {latency_ms}ms · tokens={total_tok} (thought={thought_tok}) · 长连接已预热",
                    latency_ms,
                )
            return False, f"响应异常：{txt[:120]}", latency_ms
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return False, f"连接失败 ({type(exc).__name__}): {exc}", latency_ms


_PREEMPTABLE_INFLIGHT_TRIGGERS: frozenset[str] = frozenset(
    {"augment_picking", "pre_death_warmup"}
)
_PREEMPTIVE_NEW_TRIGGERS: frozenset[str] = frozenset(
    {"augment_selected", "manual_f6"}
)


@dataclass
class _PendingGeminiJob:
    req: CoachRequestPayload
    store: DataStore | None
    api_key: str
    model: str
    thinking_level: str
    adaptive_thinking: bool
    proxy_url: str
    gen: int = 0
    fast_picker_model: str = ""
    use_fast_picker_payload: bool | None = None
    fast_death: bool = False


@dataclass
class _InFlightGeminiJob:
    gen: int
    req: CoachRequestPayload
    cancelled: bool = False


class GeminiCoachWorker:
    """Up-to-2-concurrency cancellable latest-wins executor preventing head-of-line blocking."""

    def __init__(
        self,
        on_advice: Callable[[CoachAdvicePayload, GeminiCallStats], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        client: GeminiCoachClient | None = None,
        *,
        on_superseded: Callable[[CoachRequestPayload, GeminiCallStats], None] | None = None,
        max_workers: int = 2,
    ) -> None:
        self._on_advice = on_advice
        self._on_error = on_error
        self._on_superseded = on_superseded
        self._client = client or GeminiCoachClient()
        self._max_workers = max(1, min(4, int(max_workers)))
        self._cond = threading.Condition()
        self._pending: _PendingGeminiJob | None = None
        self._active_jobs: dict[int, _InFlightGeminiJob] = {}
        self._generation: int = 0
        self._latest_submitted_id: str = ""
        self._stopped = False
        self.last_stats: GeminiCallStats | None = None
        self.last_error: str = ""

        self._threads: list[threading.Thread] = []
        for idx in range(self._max_workers):
            t = threading.Thread(
                target=self._run_loop,
                name=f"HexGeminiCoachWorker-{idx + 1}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        self._thread = self._threads[0]

    @property
    def is_busy(self) -> bool:
        with self._cond:
            has_live_active = any(not j.cancelled for j in self._active_jobs.values())
            return has_live_active or (self._pending is not None)

    @property
    def active_count(self) -> int:
        with self._cond:
            return len(self._active_jobs)

    def has_inflight_trigger(self, trigger_reason: str) -> bool:
        """Return True iff a pending or non-cancelled active job matches trigger_reason."""
        target = (trigger_reason or "").strip().lower()
        if not target:
            return False
        with self._cond:
            if (
                self._pending is not None
                and (self._pending.req.trigger_reason or "").strip().lower() == target
            ):
                return True
            return any(
                not j.cancelled and (j.req.trigger_reason or "").strip().lower() == target
                for j in self._active_jobs.values()
            )

    def _can_dispatch_pending_locked(self) -> bool:
        if self._pending is None:
            return False
        active_n = len(self._active_jobs)
        if active_n == 0:
            return True
        if active_n >= self._max_workers:
            return False
        # Allow a 2nd concurrent thread immediately when active job(s) are cancelled or
        # preemptable (e.g. dying augment_picking / pre_death_warmup), OR when a high-priority
        # trigger (augment_selected / manual_f6) arrives so it never stalls behind a slow call.
        if all(j.cancelled for j in self._active_jobs.values()):
            return True
        if all(
            (j.req.trigger_reason or "").strip().lower() in _PREEMPTABLE_INFLIGHT_TRIGGERS
            for j in self._active_jobs.values()
        ):
            return True
        pending_trig = (self._pending.req.trigger_reason or "").strip().lower()
        if pending_trig in _PREEMPTIVE_NEW_TRIGGERS:
            return True
        return False

    def submit(
        self,
        req: CoachRequestPayload,
        store: DataStore | None,
        *,
        api_key: str,
        model: str,
        thinking_level: str,
        adaptive_thinking: bool = True,
        proxy_url: str,
        fast_picker_model: str = "",
        use_fast_picker_payload: bool | None = None,
        fast_death: bool = False,
    ) -> None:
        """Queue request overwriting any unstarted older snapshot and bumping generation."""
        with self._cond:
            self._generation += 1
            gen = self._generation
            self._latest_submitted_id = req.request_id
            self._pending = _PendingGeminiJob(
                req=req,
                store=store,
                api_key=api_key,
                model=model,
                thinking_level=thinking_level,
                adaptive_thinking=adaptive_thinking,
                proxy_url=proxy_url,
                gen=gen,
                fast_picker_model=fast_picker_model,
                use_fast_picker_payload=use_fast_picker_payload,
                fast_death=fast_death,
            )
            self._cond.notify_all()

    def cancel_pending(self, trigger_reasons: set[str] | None = None) -> int:
        """Cancel queued/in-flight requests matching trigger_reasons (or all when None).

        Marked in-flight calls are dropped upon completion and reported via on_superseded
        without invoking on_advice or blocking a newly arriving request.
        """
        norm_reasons: set[str] | None = (
            {str(r).strip().lower() for r in trigger_reasons if str(r).strip()}
            if trigger_reasons is not None
            else None
        )
        with self._cond:
            cancelled_count = 0
            if self._pending is not None:
                p_trig = (self._pending.req.trigger_reason or "").strip().lower()
                if norm_reasons is None or p_trig in norm_reasons:
                    self._pending = None
                    cancelled_count += 1
            for active in self._active_jobs.values():
                if active.cancelled:
                    continue
                a_trig = (active.req.trigger_reason or "").strip().lower()
                if norm_reasons is None or a_trig in norm_reasons:
                    active.cancelled = True
                    cancelled_count += 1
            if cancelled_count > 0:
                self._generation += 1
                self._cond.notify_all()
            return cancelled_count

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._pending = None
            for active in self._active_jobs.values():
                active.cancelled = True
            self._generation += 1
            self._cond.notify_all()
        deadline = time.monotonic() + 1.5
        for t in self._threads:
            if t.is_alive():
                rem = max(0.05, deadline - time.monotonic())
                t.join(timeout=rem)
        self._client.close()

    def _run_loop(self) -> None:
        while True:
            with self._cond:
                while not self._stopped and not self._can_dispatch_pending_locked():
                    self._cond.wait(timeout=1.0)
                if self._stopped:
                    return
                job = self._pending
                self._pending = None
                if job is None:
                    continue
                in_flight = _InFlightGeminiJob(gen=job.gen, req=job.req)
                self._active_jobs[job.gen] = in_flight

            advise_kwargs: dict[str, Any] = {
                "api_key": job.api_key,
                "model": job.model,
                "thinking_level": job.thinking_level,
                "adaptive_thinking": job.adaptive_thinking,
                "proxy_url": job.proxy_url,
            }
            if job.fast_picker_model:
                advise_kwargs["fast_picker_model"] = job.fast_picker_model
            if job.use_fast_picker_payload is not None:
                advise_kwargs["use_fast_picker_payload"] = job.use_fast_picker_payload
            if job.fast_death:
                advise_kwargs["fast_death"] = True

            try:
                advice, stats = self._client.advise(job.req, job.store, **advise_kwargs)
                with self._cond:
                    self._active_jobs.pop(job.gen, None)
                    stopped = self._stopped
                    superseded = (
                        in_flight.cancelled
                        or job.gen != self._generation
                        or job.req.request_id != self._latest_submitted_id
                    )
                    self._cond.notify_all()
                if not superseded or self.last_stats is None:
                    self.last_stats = stats
                if not superseded:
                    self.last_error = ""
                if superseded:
                    logger.info(
                        "Dropping superseded Gemini advice for %s (trigger=%s)",
                        job.req.request_id,
                        job.req.trigger_reason,
                    )
                    if not stopped and self._on_superseded is not None:
                        try:
                            self._on_superseded(job.req, stats)
                        except Exception as cb_exc:
                            logger.debug("on_superseded callback error: %s", cb_exc)
                    continue
                if not stopped and self._on_advice is not None:
                    self._on_advice(advice, stats)
            except Exception as exc:
                with self._cond:
                    self._active_jobs.pop(job.gen, None)
                    stopped = self._stopped
                    superseded = (
                        in_flight.cancelled
                        or job.gen != self._generation
                        or job.req.request_id != self._latest_submitted_id
                    )
                    self._cond.notify_all()
                err_msg = f"{type(exc).__name__}: {exc}"
                if not superseded:
                    self.last_error = err_msg
                logger.warning("Gemini coach call failed for %s: %s", job.req.request_id, err_msg)
                if not superseded and not stopped and self._on_error is not None:
                    self._on_error(job.req.request_id, err_msg)
