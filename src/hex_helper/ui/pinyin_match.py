"""Zero-dependency Chinese pinyin-initial & multi-tier augment query matcher."""

from __future__ import annotations

from functools import lru_cache

from hex_helper.data.locale_pack import t2s_normalize

# GB2312 Level-1 (0xB0A1 - 0xD7F9) pinyin initial boundary table
_GB2312_INITIAL_BOUNDARIES: tuple[tuple[int, str], ...] = (
    (0xB0A1, "a"),
    (0xB0C5, "b"),
    (0xB2C1, "c"),
    (0xB4EE, "d"),
    (0xB6EA, "e"),
    (0xB7A2, "f"),
    (0xB8C1, "g"),
    (0xB9FE, "h"),
    (0xBBF7, "j"),
    (0xBFA6, "k"),
    (0xC0AC, "l"),
    (0xC2E8, "m"),
    (0xC4C3, "n"),
    (0xC5B6, "o"),
    (0xC5BE, "p"),
    (0xC6DA, "q"),
    (0xC8BB, "r"),
    (0xC8F6, "s"),
    (0xCBFA, "t"),
    (0xCDDA, "w"),
    (0xCEF4, "x"),
    (0xD1B9, "y"),
    (0xD4D1, "z"),
)

# Patch table for GB2312 Level-2 / non-Level-1 characters & LoL / augment vocabulary
_HANZI_INITIAL_PATCH: dict[str, str] = {
    # Prompt-required & core Hextech/LoL characters
    "巫": "w",
    "妖": "y",
    "铠": "k",
    "娅": "y",
    "婕": "j",
    "璐": "l",
    "烬": "j",
    "璨": "c",
    "曜": "y",
    "霆": "t",
    "魄": "p",
    "魅": "m",
    "焰": "y",
    "噬": "s",
    "翎": "l",
    "刃": "r",
    "蛰": "z",
    "穹": "q",
    "渊": "y",
    "罡": "g",
    "飓": "j",
    "淬": "c",
    "弈": "y",
    # Non-Level-1 characters verified across full 16.23 augments/items/heroes datasets
    "亵": "x",
    "冥": "m",
    "匕": "b",
    "叽": "j",
    "咪": "m",
    "喵": "m",
    "嘹": "l",
    "坩": "g",
    "垭": "y",
    "埚": "g",
    "娑": "s",
    "嫖": "p",
    "岚": "l",
    "崽": "z",
    "弑": "s",
    "弩": "n",
    "彗": "h",
    "戟": "j",
    "撸": "l",
    "斐": "f",
    "殇": "s",
    "沌": "d",
    "渎": "d",
    "湮": "y",
    "漩": "x",
    "炖": "d",
    "炫": "x",
    "熵": "s",
    "狩": "s",
    "珏": "j",
    "璀": "c",
    "癫": "d",
    "皎": "j",
    "睥": "p",
    "睨": "n",
    "筝": "z",
    "籁": "l",
    "绯": "f",
    "缇": "t",
    "羁": "j",
    "肴": "y",
    "胫": "j",
    "芙": "f",
    "芮": "r",
    "芸": "y",
    "菈": "l",
    "萃": "c",
    "萦": "y",
    "蓓": "b",
    "蔻": "k",
    "薇": "w",
    "虔": "q",
    "蚱": "z",
    "蜥": "x",
    "蜴": "y",
    "蝠": "f",
    "螂": "l",
    "螳": "t",
    "谕": "y",
    "迦": "j",
    "钴": "g",
    "锢": "g",
    "镕": "r",
    "镖": "b",
    "镯": "z",
    "阈": "y",
    "阱": "j",
    "雯": "w",
    "飙": "b",
    "馑": "j",
    "骰": "t",
    "魇": "y",
    "鲛": "j",
    "鲨": "s",
    "鳄": "e",
    "黏": "n",
    "黛": "d",
    "黯": "a",
    # Extra common LoL / gaming Level-2 characters
    "曦": "x",
    "凛": "l",
    "冽": "l",
    "煌": "h",
    "灼": "z",
    "烁": "s",
    "陨": "y",
    "啸": "x",
    "泯": "m",
    "涅": "n",
    "槃": "p",
    "琉": "l",
    "璃": "l",
    "璇": "x",
    "珀": "p",
    "煞": "s",
    "巅": "d",
    "嵘": "r",
    "峥": "z",
    "澜": "l",
    "汐": "x",
    "沧": "c",
    "霖": "l",
    "霄": "x",
    "霹": "p",
    "雳": "l",
    "麟": "l",
    "凰": "h",
    "獠": "l",
    "枭": "x",
    "胧": "l",
    "秘": "m",
    "折": "z",
    "藏": "c",
}

_RARITY_CN_KEYWORDS: frozenset[str] = frozenset(
    {
        "银",
        "金",
        "彩",
        "白银",
        "黄金",
        "棱彩",
        "銀",
        "银色",
        "銀色",
        "白銀",
        "银级",
        "銀級",
        "金色",
        "金级",
        "金級",
        "黃金",
        "棱镜",
        "稜鏡",
    }
)


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _strip_punct(text: str) -> str:
    return "".join(ch for ch in text if _is_cjk(ch) or (ch.isascii() and ch.isalnum()))


@lru_cache(maxsize=2048)
def _char_pinyin_initial(ch: str) -> str:
    patched = _HANZI_INITIAL_PATCH.get(ch)
    if patched:
        return patched
    if ch.isascii() and ch.isalnum():
        return ch.lower()
    if not _is_cjk(ch):
        return ""
    try:
        raw = ch.encode("gb2312")
    except UnicodeEncodeError:
        return ""
    if len(raw) != 2:
        return ""
    code = (raw[0] << 8) | raw[1]
    if code < 0xB0A1 or code > 0xD7F9:
        return ""
    for boundary, initial in reversed(_GB2312_INITIAL_BOUNDARIES):
        if code >= boundary:
            return initial
    return ""


@lru_cache(maxsize=1024)
def to_pinyin_initials(text: str) -> str:
    """Return lowercase pinyin initials for Chinese characters (plus ASCII alphanumerics), folding Traditional."""
    if not text:
        return ""
    folded = t2s_normalize(text)
    return "".join(_char_pinyin_initial(ch) for ch in folded)


def _matches_single_augment_name(name: str, rarity_zh: str, query: str) -> tuple[bool, int]:
    """Score a single augment name against a user query (folding T2S for bilingual matching)."""
    q_raw = (query or "").strip().lower()
    if not q_raw:
        return True, 0

    nm_raw = (name or "").strip().lower()
    nm_fold = t2s_normalize(nm_raw).lower()
    q_fold = t2s_normalize(q_raw).lower()
    rar_raw = (rarity_zh or "").strip().lower()
    rar_fold = t2s_normalize(rar_raw).lower()
    q_clean = _strip_punct(q_raw)
    q_fold_clean = _strip_punct(q_fold)
    nm_clean = _strip_punct(nm_raw)
    nm_fold_clean = _strip_punct(nm_fold)

    # 1. Chinese exact prefix (raw or T2S-folded)
    if nm_raw.startswith(q_raw) or (q_fold and nm_fold.startswith(q_fold)):
        exact = nm_raw == q_raw or nm_fold == q_fold
        score = 550 if exact else 520 + max(0, 20 - len(nm_raw))
        return True, score
    if (q_clean and nm_clean.startswith(q_clean)) or (q_fold_clean and nm_fold_clean.startswith(q_fold_clean)):
        return True, 500 + max(0, 15 - len(nm_clean))

    # 2. Chinese substring (raw or T2S-folded)
    if q_raw in nm_raw:
        pos = nm_raw.find(q_raw)
        return True, 430 - min(20, pos)
    if q_fold and q_fold in nm_fold:
        pos = nm_fold.find(q_fold)
        return True, 425 - min(20, pos)
    if q_clean and q_clean in nm_clean:
        pos = nm_clean.find(q_clean)
        return True, 410 - min(10, pos)
    if q_fold_clean and q_fold_clean in nm_fold_clean:
        pos = nm_fold_clean.find(q_fold_clean)
        return True, 405 - min(10, pos)

    # 3 & 4. Pinyin initial prefix / substring (only when query has no CJK characters)
    has_cjk = any(_is_cjk(ch) for ch in q_raw)
    if not has_cjk:
        q_py = "".join(ch for ch in q_raw if ch.isascii() and ch.isalnum())
        if q_py:
            initials = to_pinyin_initials(name)
            if initials:
                if initials.startswith(q_py):
                    if initials == q_py:
                        return True, 330
                    return True, 300 + max(0, 25 - max(0, len(initials) - len(q_py)))
                pos = initials.find(q_py)
                if pos >= 0:
                    return True, 225 - min(25, pos)

    # 5. Rarity Chinese keyword (银/金/彩/白银/黄金/棱彩/銀級/金級/稜鏡/银色/金色)
    if has_cjk and rar_raw:
        if (q_raw in _RARITY_CN_KEYWORDS and q_raw in rar_raw) or (
            q_fold in _RARITY_CN_KEYWORDS and q_fold in rar_fold
        ):
            exact_r = q_raw == rar_raw or q_fold == rar_fold
            return True, 120 if exact_r else 100
        if rar_raw.startswith(q_raw) or (q_fold and rar_fold.startswith(q_fold)):
            return True, 110

    return False, 0


def matches_augment_query(
    name: str,
    rarity_zh: str,
    query: str,
    *,
    alt_name: str = "",
) -> tuple[bool, int]:
    """Match an augment against a user search query and return `(matched, priority_score)`.

    Supports primary localized name + optional bilingual fallback `alt_name` (e.g. canonical CN).
    """
    matched, score = _matches_single_augment_name(name, rarity_zh, query)
    if alt_name and alt_name.strip() != (name or "").strip():
        alt_matched, alt_score = _matches_single_augment_name(alt_name, rarity_zh, query)
        if alt_matched and (not matched or alt_score > score):
            return True, alt_score
    return matched, score
