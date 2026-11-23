"""Compact coach prompt payload builder and item-grounding helpers for HexHelper."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
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
from hex_helper.scoring.build_synergy import item_is_owned, normalize_item_id
from hex_helper.scoring.matchup_optimizer import (
    ELIXIR_IDS,
    TIER2_BOOT_IDS,
    _infer_hero_profile,
    analyze_inventory_slots,
    canonicalize_item_id,
    is_valid_plan_item,
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

COACH_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "augment_pick_advice": {"type": "string"},
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

COACH_SYSTEM_INSTRUCTION = """你是LOL海克斯大乱斗(26.23)实时参谋，渲染于狭长悬浮窗。思考直切核心取舍，禁复述输入、列举词典或冗长自检。

【八条铁律】
1.海克斯零脑补：仅据selected_augments联动，禁假设已选上轮推荐。
2.三选一：仅offered_augments非空时填augment_pick_advice(<=20字，据win_rate/delta/流派直给结论对比)，否则""。
3.路线连贯：严守committed_build_plan。延续owned_complete且禁推已有大件；allow_overhaul=false禁改locked_next_item，仅微调未动工槽位(重伤/穿透/保命)；true时立刻按新selected_augments与大数据重构后续槽位。
4.特殊流派：special_augment_builds非空时紧扣force_items/core_items并避avoid_note。
5.大数据为主：以hero_big_data_top_builds与local_engine_top_items为主干，低权重看hero_signature_hint防冷门挂件；敌极肥或高回复缺重伤时果断补重伤/抗性/保命。
6.控字与ID：
- item_id与name严守输入ID/item_glossary("名|金",优先4位ID)。
- next_items[0]唯一明确下件(priority="下件必出",reason<=14字写敌我证据)；[1]备选("次选")。
- buy_now<=24字：结合my_hero.gold与已有散件给此刻购买。
- build_path：未出大件名(2-5件,含未升二级鞋)。
- tactical_summary<=28字：1句纯团战/站位/技能提醒，禁提装备与金币。
7.六神满格：inventory_status.status为full_6_complete或full_5_plus_boots时，禁推第7件或散件；默认next_items[0]推500G合剂(2139巫术/2140愤怒/2138钢铁,priority="满格合剂")、build_path=[]、buy_now提示磕合剂；仅胜负刚需(0重伤打高回复/频被秒/钱够卖鞋换大件)可换装(priority="必须换装",reason写"卖X换Y+理由")。
8.开局简报：仅trigger_reason=="game_start"时填opening_brief(enemy_threat<=22字敌核心威胁/伤害结构,augment_goal<=22字目标海克斯流派,opening_buy<=24字1400G出门与首件)；否则省略或{}。"""


@dataclass(frozen=True)
class GeminiCallStats:
    model: str
    thinking_level: str
    latency_ms: int
    total_tokens: int = 0
    thought_tokens: int = 0
    output_tokens: int = 0


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
    """Return (id->name, normalized_name->canonical_id) prioritizing base 4-digit items."""
    id_to_name: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    if store is None or not store.static.items:
        return id_to_name, name_to_id

    for raw_id, info in store.static.items.items():
        if not info.name:
            continue
        id_to_name[str(raw_id)] = info.name
        norm_id = normalize_item_id(str(raw_id))
        if norm_id not in id_to_name:
            id_to_name[norm_id] = info.name
        # Prefer shorter canonical 4-digit ID over 6-digit Mayhem copies for name->id
        existing = name_to_id.get(info.name)
        if existing is None or (len(str(raw_id)) == 4 and len(existing) != 4):
            name_to_id[info.name] = str(raw_id)
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


def build_coach_prompt_payload(req: CoachRequestPayload, store: DataStore | None) -> dict[str, Any]:
    """Assemble prefix-cache-friendly, compact, ID-grounded match snapshot JSON for LLM reasoning.

    Key ordering places match-invariant static context first (dataset_patch, hero_static,
    hero_big_data_top_builds, hero_signature_hint, item_glossary) and volatile per-turn state
    at the tail, excluding request_id and timestamp so prompt prefix caching stays warm across turns.
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
    data["allies"] = deduped_allies
    data["enemies"] = cleaned_enemies
    return data


def _is_explicit_swap_item(priority: str, reason: str) -> bool:
    """Return True iff the coach explicitly justified selling an owned item/boots to swap in a mandatory counter."""
    p = priority or ""
    r = reason or ""
    return ("换" in p) or ("卖" in p) or ("卖" in r) or ("换" in r) or ("替换" in r)


def ground_coach_advice(
    raw_json: dict[str, Any],
    req: CoachRequestPayload,
    store: DataStore | None,
    model: str = "gemini-4-hydrogen",
    thinking_level: str = "medium",
) -> CoachAdvicePayload:
    """Validate, canonicalize item IDs/names against DataStore, drop owned duplicates, and guard 6-slot full builds."""
    id_to_name, name_to_id = _canonical_item_lookup(store)

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
        sel_augs = [str(a.get("id", "")) for a in (req.selected_augments or []) if a.get("id")]
        prof = _infer_hero_profile(store, hid, resolved_owned_ids, sel_augs) if hid else "AP"
        inv = analyze_inventory_slots(store, resolved_owned_ids, profile=prof)
        inv_full_build = inv.is_full_build or len(owned_complete_names) >= 6
        default_elixir_id = inv.recommended_elixir_id
        default_elixir_name = inv.recommended_elixir_name
    elif len(owned_complete_names) >= 6:
        inv_full_build = True

    grounded_items: list[CoachAdviceItem] = []
    seen_item_ids: set[str] = set()
    has_store_catalog = bool(id_to_name)
    for it in raw_json.get("next_items") or []:
        if not isinstance(it, dict):
            continue
        raw_id = canonicalize_item_id(str(it.get("item_id", "")).strip())
        raw_name_orig = str(it.get("name", "")).strip()
        raw_name = _ITEM_NICKNAME_ALIASES.get(raw_name_orig, raw_name_orig)
        if raw_id and raw_id in id_to_name:
            canon_id = raw_id
            canon_name = id_to_name[canon_id]
        elif raw_name and raw_name in name_to_id:
            canon_id = canonicalize_item_id(name_to_id[raw_name])
            canon_name = id_to_name.get(canon_id, raw_name)
        elif has_store_catalog:
            # Drop hallucinated/deleted item IDs (e.g. legacy 3151 if unmapped, or non-existent shop items)
            logger.debug("Dropping ungrounded Gemini item: id=%r name=%r", raw_id, raw_name_orig)
            continue
        else:
            canon_id = raw_id
            canon_name = raw_name or raw_id
        if not canon_name or canon_name.startswith("装备#"):
            continue
        if (canon_id and canon_id in owned_complete_norm) or (canon_name in owned_complete_names):
            continue
        priority_str = str(it.get("priority") or ("下件必出" if not grounded_items else "次选")).strip()
        reason_str = str(it.get("reason", "")).strip()
        if inv_full_build and canon_id not in ELIXIR_IDS and not _is_explicit_swap_item(priority_str, reason_str):
            # Refuse unjustified 7th major item when all 6 slots are already occupied!
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
    if not inv_full_build:
        for step in raw_json.get("build_path") or []:
            s_raw = str(step).strip()
            s = _ITEM_NICKNAME_ALIASES.get(s_raw, s_raw)
            if not s or s.startswith("装备#"):
                continue
            if has_store_catalog and store is not None:
                cid = name_to_id.get(s) or (canonicalize_item_id(s) if s.isdigit() else "")
                if not cid or not is_valid_plan_item(store, cid):
                    continue
                s = id_to_name.get(cid, s)
                if cid in owned_complete_norm:
                    continue
            if s in owned_complete_names or s in build_path:
                continue
            build_path.append(s)

    pick_advice = str(raw_json.get("augment_pick_advice", "")).strip()
    if not req.offered_augments:
        pick_advice = ""

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


def _is_transient_connection_error(exc: Exception) -> bool:
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ in _TRANSIENT_CONN_ERROR_NAMES or isinstance(cur, (ConnectionError, EOFError)):
            return True
        cur = cur.__cause__ or cur.__context__
    return False
