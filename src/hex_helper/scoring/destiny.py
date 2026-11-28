"""Destiny's Weapon (augment ID 3081) item recommendation ranking and concise card hint."""

from __future__ import annotations

from dataclasses import dataclass

from hex_helper.data.store import DataStore

DESTINY_AUGMENT_ID = "3081"
DESTINY_CONTROL_MIN_GAMES = 100
DESTINY_NEGATIVE_WARNING = "▽ 绑定收益偏弱 · 慎选"

_STYLE_ALIASES: dict[str, str] = {
    "ap": "AP",
    "magic": "AP",
    "mage": "AP",
    "法强": "AP",
    "魔法": "AP",
    "法师": "AP",
    "ad": "AD",
    "physical": "AD",
    "marksman": "AD",
    "fighter": "AD",
    "assassin": "AD",
    "物理": "AD",
    "射手": "AD",
    "战士": "AD",
    "刺客": "AD",
    "tank": "TANK",
    "坦克": "TANK",
    "前排": "TANK",
    "重装": "TANK",
    "support": "SUPPORT",
    "enchanter": "SUPPORT",
    "辅助": "SUPPORT",
    "软辅": "SUPPORT",
}
_UNSPLIT_STYLES: frozenset[str] = frozenset({"", "不拆", "none", "all", "default"})


def _normalize_style(raw: str) -> str:
    cleaned = str(raw or "").strip()
    if not cleaned:
        return ""
    low = cleaned.lower()
    if low in _UNSPLIT_STYLES:
        return ""
    return _STYLE_ALIASES.get(low, cleaned.upper())


@dataclass(frozen=True)
class DestinyItemRec:
    item_id: str
    name: str
    win_rate: float
    delta: float
    games: int
    low_sample: bool
    style: str = ""
    control_games: int = 0
    control_win_rate: float | None = None


@dataclass(frozen=True)
class DestinyCardHint:
    """Concise single-line 3081 card hint and optional negative payoff warning."""

    top_item_id: str
    top_item_name: str
    top_delta: float
    hint_line: str
    warning_line: str = ""
    is_negative_alert: bool = False
    style: str = ""


def _resolve_baseline_win_rate(
    store: DataStore,
    hero_id: str | int,
    data_baseline_games: int,
    data_baseline_wr: float,
) -> float:
    if data_baseline_games > 0:
        return data_baseline_wr
    hero = store.get_hero(hero_id)
    if hero is not None and hero.games > 0:
        return hero.win_rate
    return data_baseline_wr


def rank_destiny_items(
    store: DataStore,
    hero_id: str | int,
    limit: int = 4,
    profile: str = "",
) -> list[DestinyItemRec]:
    """Return top binding items for Destiny's Weapon (3081), preferring control WR when sample >= 100."""
    data = store.get_destiny_data(hero_id)
    if data is None or not data.items:
        return []

    baseline = _resolve_baseline_win_rate(store, hero_id, data.baseline_games, data.baseline_win_rate)
    target_style = _normalize_style(profile)
    ranked: list[tuple[bool, bool, float, int, DestinyItemRec]] = []

    for it in data.items:
        wr = it.win_rate
        ctrl_wr = it.control_win_rate if it.control_games >= DESTINY_CONTROL_MIN_GAMES else None
        ref_wr = ctrl_wr if ctrl_wr is not None else baseline
        item_style_norm = _normalize_style(it.style)
        style_matched = bool(target_style and item_style_norm and item_style_norm == target_style)
        rec = DestinyItemRec(
            item_id=it.item_id,
            name=it.name or store.get_item_info(it.item_id).name,
            win_rate=wr,
            delta=wr - ref_wr,
            games=it.games,
            low_sample=it.low_sample,
            style=it.style,
            control_games=it.control_games,
            control_win_rate=ctrl_wr,
        )
        ranked.append((not rec.low_sample, style_matched, rec.delta, rec.games, rec))

    ranked.sort(key=lambda row: (row[0], row[1], row[2], row[3]), reverse=True)
    return [row[4] for row in ranked[: max(0, int(limit))]]


def summarize_destiny_card_hint(
    store: DataStore,
    hero_id: str | int,
    aug_delta: float = 0.0,
    profile: str = "",
) -> DestinyCardHint | None:
    """Summarize top binding item hint and precise negative-payoff warning for 3081."""
    recs = rank_destiny_items(store, hero_id, limit=4, profile=profile)
    if not recs:
        return None

    top = recs[0]
    top_delta = top.delta

    # On unsplit ("不拆") single-item heroes (e.g. Rell/Sion/Nautilus/Leona), control WR reflects
    # an off-meta control item (Heartsteel/Jak'Sho without 3081) that itself loses vs hero WR;
    # keep hero-baseline check alongside control delta so genuinely weak 3081 bindings are flagged,
    # while skipping false warnings when both aug_delta and control delta are non-negative (e.g. Poppy).
    data = store.get_destiny_data(hero_id)
    has_unsplit_baseline = False
    unsplit_baseline_delta = top_delta
    if top.style == "不拆" and data is not None:
        baseline = _resolve_baseline_win_rate(store, hero_id, data.baseline_games, data.baseline_win_rate)
        if data.baseline_games > 0 or store.get_hero(hero_id) is not None:
            unsplit_baseline_delta = top.win_rate - baseline
            has_unsplit_baseline = True

    ctrl_negative_alert = bool(
        (aug_delta < -0.005 and top_delta < 0.0)
        or top_delta < -0.010
    )
    unsplit_negative_alert = bool(
        has_unsplit_baseline
        and (
            (aug_delta < -0.005 and unsplit_baseline_delta < 0.0)
            or (
                unsplit_baseline_delta < -0.010
                and (aug_delta < 0.0 or top_delta < 0.0)
            )
        )
    )
    is_negative_alert = ctrl_negative_alert or unsplit_negative_alert

    # Keep displayed delta and alert baseline consistent so a negative alert never shows
    # a contradictory positive "(+X.X%)" hint immediately above the red warning pill.
    use_unsplit_delta = bool(
        unsplit_negative_alert
        and (not ctrl_negative_alert or unsplit_baseline_delta < -0.010)
    )
    display_delta = unsplit_baseline_delta if use_unsplit_delta else top_delta
    hint_line = f"◈ 绑定首选：{top.name} ({display_delta * 100:+.1f}%)"
    warning_line = DESTINY_NEGATIVE_WARNING if is_negative_alert else ""

    return DestinyCardHint(
        top_item_id=top.item_id,
        top_item_name=top.name,
        top_delta=display_delta,
        hint_line=hint_line,
        warning_line=warning_line,
        is_negative_alert=is_negative_alert,
        style=top.style,
    )
