"""Pure read-only LCU 10-player LobbyScout (strength rating, streak/premade detection, zero-PII AI export)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import logging
import math
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hex_helper.data.store import DataStore
    from hex_helper.game.lcu_client import LcuClient

logger = logging.getLogger(__name__)

ARAM_SCOUT_QUEUE_IDS: frozenset[int] = frozenset({2400, 2401, 2402, 450})
ARAM_SCOUT_GAME_MODES: frozenset[str] = frozenset({"ARAM", "KIWI", "MAYHEM"})

DEFAULT_UNRANKED_RATING = 1900
MIN_HEX_RATING = 1200
MAX_HEX_RATING = 4200

_TIER_BASE_RATINGS: dict[str, int] = {
    "IRON": 1350,
    "BRONZE": 1550,
    "SILVER": 1750,
    "GOLD": 2000,
    "PLATINUM": 2250,
    "EMERALD": 2500,
    "DIAMOND": 2850,
    "MASTER": 3250,
    "GRANDMASTER": 3550,
    "CHALLENGER": 3850,
}

_APEX_TIERS: frozenset[str] = frozenset({"MASTER", "GRANDMASTER", "CHALLENGER"})

_DIVISION_OFFSETS: dict[str, int] = {
    "IV": -60,
    "4": -60,
    "III": -20,
    "3": -20,
    "II": 20,
    "2": 20,
    "I": 60,
    "1": 60,
}

_TIER_SHORT_CN: dict[str, str] = {
    "IRON": "铁",
    "BRONZE": "铜",
    "SILVER": "银",
    "GOLD": "金",
    "PLATINUM": "铂",
    "EMERALD": "翡翠",
    "DIAMOND": "钻",
    "MASTER": "大师",
    "GRANDMASTER": "宗师",
    "CHALLENGER": "王者",
}

_TIER_FULL_CN: dict[str, str] = {
    "IRON": "黑铁",
    "BRONZE": "青铜",
    "SILVER": "白银",
    "GOLD": "黄金",
    "PLATINUM": "铂金",
    "EMERALD": "翡翠",
    "DIAMOND": "钻石",
    "MASTER": "大师",
    "GRANDMASTER": "宗师",
    "CHALLENGER": "王者",
}

_DIVISION_CN: dict[str, str] = {
    "I": "一",
    "1": "一",
    "II": "二",
    "2": "二",
    "III": "三",
    "3": "三",
    "IV": "四",
    "4": "四",
}

_PREMADE_LABELS: tuple[str, ...] = ("车队A", "车队B", "车队C", "车队D")


@dataclass(frozen=True)
class PlayerScoutCard:
    """Read-only pre-game strength profile for a single lobby participant."""

    puuid: str
    summoner_name: str
    champion_id: str
    champion_name: str
    team: str  # "ORDER" | "CHAOS"
    hex_rating: int
    rank_label: str
    aram_wr: float
    aram_games: int
    kda: float
    streak_tag: str
    premade_group: str
    hidden_history: bool
    is_mvp_carry: bool = False


def _card_rank_key(card: PlayerScoutCard) -> tuple[int, float, int, float]:
    return (card.hex_rating, card.aram_wr, card.aram_games, card.kda)


def _format_enemy_tag(card: PlayerScoutCard) -> str:
    parts: list[str] = []
    if card.rank_label:
        parts.append(card.rank_label)
    if card.aram_games > 0:
        parts.append(f"WR{int(round(card.aram_wr * 100))}%({card.aram_games}场)")
    elif card.hidden_history:
        parts.append("隐藏战绩")
    if card.streak_tag:
        parts.append(card.streak_tag)
    if card.premade_group:
        parts.append(card.premade_group)
    return "·".join(parts)


@dataclass(frozen=True)
class LobbyScoutSnapshot:
    """10-player read-only lobby intelligence snapshot with zero-PII coach export."""

    my_team: tuple[PlayerScoutCard, ...]
    enemy_team: tuple[PlayerScoutCard, ...]
    mvp_card: PlayerScoutCard | None
    mvp_lead_gap: int
    top_enemy_card: PlayerScoutCard | None
    premade_notes: tuple[str, ...]
    degraded_count: int

    def to_coach_context(self) -> dict[str, Any]:
        """Export compact tactical lobby context strictly stripped of raw PUUIDs and summoner names."""
        top_enemy = self.top_enemy_card
        if top_enemy is None and self.enemy_team:
            top_enemy = max(self.enemy_team, key=_card_rank_key)

        ally_mvp = max(self.my_team, key=_card_rank_key) if self.my_team else None

        enemy_groups: dict[str, list[str]] = defaultdict(list)
        for card in self.enemy_team:
            if card.premade_group:
                enemy_groups[card.premade_group].append(card.champion_name or f"英雄#{card.champion_id}")

        enemy_premades: list[str] = []
        for grp in sorted(enemy_groups.keys()):
            members = enemy_groups[grp]
            if len(members) >= 2:
                enemy_premades.append(f"{len(members)}黑({'+'.join(members)})")

        return {
            "top_enemy_champion": top_enemy.champion_name if top_enemy is not None else "",
            "top_enemy_rating": int(top_enemy.hex_rating) if top_enemy is not None else 0,
            "top_enemy_tag": _format_enemy_tag(top_enemy) if top_enemy is not None else "",
            "enemy_premades": enemy_premades,
            "ally_mvp_champion": ally_mvp.champion_name if ally_mvp is not None else "",
            "ally_mvp_rating": int(ally_mvp.hex_rating) if ally_mvp is not None else 0,
        }


def _extract_ranked_entry(ranked_stats: dict[str, Any] | None) -> tuple[str, str]:
    if not isinstance(ranked_stats, dict):
        return "", ""

    queue_map = ranked_stats.get("queueMap")
    candidates: list[Any] = []
    if isinstance(queue_map, dict):
        candidates.append(queue_map.get("RANKED_SOLO_5x5"))
        candidates.append(queue_map.get("RANKED_FLEX_SR"))
    candidates.append(ranked_stats.get("highestRankedEntrySR"))
    candidates.append(ranked_stats.get("highestRankedEntry"))

    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        tier = str(entry.get("tier") or "").strip().upper()
        if tier in _TIER_BASE_RATINGS:
            division = str(entry.get("division") or "").strip().upper()
            return tier, division
    return "", ""


def _compute_tier_base_and_label(ranked_stats: dict[str, Any] | None) -> tuple[int, str]:
    tier, division = _extract_ranked_entry(ranked_stats)
    if not tier:
        return DEFAULT_UNRANKED_RATING, "未定级"

    base = _TIER_BASE_RATINGS[tier]
    if tier in _APEX_TIERS:
        return base, _TIER_FULL_CN[tier]

    div_offset = _DIVISION_OFFSETS.get(division, 0)
    div_cn = _DIVISION_CN.get(division, "")
    label = f"{_TIER_SHORT_CN[tier]}{div_cn}" if div_cn else _TIER_FULL_CN[tier]
    return base + div_offset, label


def _safe_int(val: Any, default: int = 0) -> int:
    """Safely parse untrusted LCU JSON values into int, rejecting bool and non-finite floats."""
    if val is None or isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val) if math.isfinite(val) else default
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return default
        try:
            return int(text)
        except ValueError:
            try:
                fval = float(text)
            except ValueError:
                return default
            return int(fval) if math.isfinite(fval) else default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _order_games_newest_first(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timestamps: list[int] = []
    for g in games:
        ts = _safe_int(g.get("gameCreation"), 0)
        if ts > 0:
            timestamps.append(ts)
    if len(timestamps) >= 2 and len(set(timestamps)) > 1:
        return sorted(games, key=lambda g: _safe_int(g.get("gameCreation"), 0), reverse=True)
    return list(games)


def _extract_player_stats(game: dict[str, Any], puuid: str) -> dict[str, Any] | None:
    participants = game.get("participants")
    if not isinstance(participants, list) or not participants:
        return None

    if len(participants) == 1:
        first = participants[0]
        if isinstance(first, dict):
            stats = first.get("stats")
            return stats if isinstance(stats, dict) else None
        return None

    target_pid: int | None = None
    identities = game.get("participantIdentities")
    if isinstance(identities, list) and puuid:
        for ident in identities:
            if not isinstance(ident, dict):
                continue
            player = ident.get("player")
            if isinstance(player, dict) and str(player.get("puuid") or "").strip() == puuid:
                pid_val = ident.get("participantId")
                if isinstance(pid_val, int) and not isinstance(pid_val, bool):
                    target_pid = pid_val
                    break

    for part in participants:
        if not isinstance(part, dict):
            continue
        if target_pid is not None and part.get("participantId") == target_pid:
            stats = part.get("stats")
            return stats if isinstance(stats, dict) else None
        if puuid and str(part.get("puuid") or "").strip() == puuid:
            stats = part.get("stats")
            return stats if isinstance(stats, dict) else None
    return None


def _analyze_match_history(
    games: list[dict[str, Any]],
    puuid: str,
) -> tuple[float, int, float, str, set[int]]:
    """Return (aram_wr, aram_games, kda, streak_tag, recent_game_ids)."""
    if not games:
        return 0.0, 0, 0.0, "", set()

    ordered = _order_games_newest_first(games)
    game_ids: set[int] = set()
    outcomes: list[bool] = []
    kills_sum = 0
    deaths_sum = 0
    assists_sum = 0

    for game in ordered:
        gid_int = _safe_int(game.get("gameId"), 0)
        if gid_int > 0:
            game_ids.add(gid_int)

        qid_int = _safe_int(game.get("queueId"), 0)
        mode_str = str(game.get("gameMode") or "").strip().upper()
        if qid_int not in ARAM_SCOUT_QUEUE_IDS and mode_str not in ARAM_SCOUT_GAME_MODES:
            continue

        stats = _extract_player_stats(game, puuid)
        if not stats:
            continue
        if "win" not in stats:
            continue
        outcomes.append(bool(stats.get("win")))
        kills_sum += max(0, _safe_int(stats.get("kills"), 0))
        deaths_sum += max(0, _safe_int(stats.get("deaths"), 0))
        assists_sum += max(0, _safe_int(stats.get("assists"), 0))

    aram_games = len(outcomes)
    if aram_games == 0:
        return 0.0, 0, 0.0, "", game_ids

    aram_wr = round(sum(1 for w in outcomes if w) / aram_games, 4)
    kda = round((kills_sum + assists_sum) / max(1, deaths_sum), 2)

    first_outcome = outcomes[0]
    streak = 0
    for outcome in outcomes:
        if outcome == first_outcome:
            streak += 1
        else:
            break
    streak_tag = ""
    if streak >= 3:
        streak_tag = f"{streak}连胜" if first_outcome else f"{streak}连败"

    return aram_wr, aram_games, kda, streak_tag, game_ids


def _estimate_hex_rating(base_rating: int, aram_wr: float, aram_games: int, kda: float) -> int:
    if aram_games <= 0:
        return int(max(MIN_HEX_RATING, min(MAX_HEX_RATING, base_rating)))
    confidence = min(1.0, aram_games / 10.0)
    wr_adj = (aram_wr - 0.50) * 900.0 * confidence
    kda_adj = min(240.0, max(-160.0, (kda - 2.6) * 65.0)) * confidence
    raw = base_rating + wr_adj + kda_adj
    return int(round(max(MIN_HEX_RATING, min(MAX_HEX_RATING, raw))))


def _resolve_champion_name(
    champion_id: str,
    member: dict[str, Any],
    store: DataStore | None,
) -> str:
    if store is not None and champion_id:
        try:
            hero = store.get_hero(champion_id)
            if hero is not None and hero.name:
                return hero.name
        except Exception:
            pass
        static = getattr(store, "static", None)
        if static is not None:
            cn_map = getattr(static, "hero_cn_names", None)
            if isinstance(cn_map, dict) and cn_map.get(champion_id):
                return str(cn_map[champion_id])
            alias_fn = getattr(static, "get_champion_alias", None)
            if callable(alias_fn):
                alias = str(alias_fn(champion_id) or "").strip()
                if alias:
                    return alias

    explicit_name = str(member.get("championName") or "").strip()
    if explicit_name:
        return explicit_name
    if champion_id:
        return f"英雄#{champion_id}"
    return "未知英雄"


def _extract_summoner_name(member: dict[str, Any]) -> str:
    for key in ("summonerName", "riotIdGameName", "gameName", "summonerInternalName"):
        val = str(member.get(key) or "").strip()
        if val:
            return val
    return ""


def _build_selection_lookup(game_data: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    by_puuid: dict[str, str] = {}
    by_sid: dict[str, str] = {}
    for sel in game_data.get("playerChampionSelections") or []:
        if not isinstance(sel, dict):
            continue
        cid = _safe_int(sel.get("championId"), 0)
        if cid <= 0:
            continue
        cid_str = str(cid)
        puuid = str(sel.get("puuid") or "").strip()
        if puuid:
            by_puuid[puuid] = cid_str
        sid = sel.get("summonerId")
        if sid is not None and str(sid).strip():
            by_sid[str(sid).strip()] = cid_str
    return by_puuid, by_sid


def _assign_team_premades(
    cards: list[PlayerScoutCard],
    game_ids_by_idx: list[set[int]],
) -> tuple[list[PlayerScoutCard], list[list[PlayerScoutCard]]]:
    n = len(cards)
    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    for i in range(n):
        if len(game_ids_by_idx[i]) < 2:
            continue
        for j in range(i + 1, n):
            if len(game_ids_by_idx[j]) < 2:
                continue
            if len(game_ids_by_idx[i] & game_ids_by_idx[j]) >= 2:
                _union(i, j)

    comps: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        comps[_find(idx)].append(idx)

    multi_comps = sorted((idxs for idxs in comps.values() if len(idxs) >= 2), key=lambda idxs: idxs[0])
    label_by_idx: dict[int, str] = {}
    for grp_i, idxs in enumerate(multi_comps):
        label = _PREMADE_LABELS[grp_i] if grp_i < len(_PREMADE_LABELS) else f"车队{grp_i + 1}"
        for idx in idxs:
            label_by_idx[idx] = label

    updated_cards = [
        replace(card, premade_group=label_by_idx.get(idx, ""))
        for idx, card in enumerate(cards)
    ]
    groups = [[updated_cards[idx] for idx in idxs] for idxs in multi_comps]
    return updated_cards, groups


def scout_lobby_readonly(
    lcu: LcuClient,
    store: DataStore | None,
    my_puuid: str = "",
    pace_s: float = 0.08,
) -> LobbyScoutSnapshot | None:
    """Scout all 10 lobby players using strictly read-only LCU GET calls with graceful degradation."""
    try:
        session = lcu.get_gameflow_session()
    except Exception as exc:
        logger.debug("LobbyScout get_gameflow_session failed: %s", exc)
        return None
    if not isinstance(session, dict):
        return None

    game_data = session.get("gameData")
    if not isinstance(game_data, dict):
        return None

    team_one_raw = [m for m in (game_data.get("teamOne") or []) if isinstance(m, dict)]
    team_two_raw = [m for m in (game_data.get("teamTwo") or []) if isinstance(m, dict)]
    if not team_one_raw and not team_two_raw:
        return None

    resolved_my_puuid = str(my_puuid or "").strip()
    if not resolved_my_puuid:
        get_curr = getattr(lcu, "get_current_summoner", None)
        if callable(get_curr):
            try:
                curr = get_curr()
                if isinstance(curr, dict):
                    resolved_my_puuid = str(curr.get("puuid") or "").strip()
            except Exception:
                resolved_my_puuid = ""

    sel_by_puuid, sel_by_sid = _build_selection_lookup(game_data)
    degraded_count = 0
    total_players = len(team_one_raw) + len(team_two_raw)
    scouted_idx = 0

    def _scout_member(member: dict[str, Any], side: str) -> tuple[PlayerScoutCard, set[int]]:
        nonlocal degraded_count, scouted_idx
        puuid = str(member.get("puuid") or "").strip()
        summoner_name = _extract_summoner_name(member)

        cid_int = _safe_int(member.get("championId"), 0)
        champ_id = str(cid_int) if cid_int > 0 else ""
        if not champ_id and puuid:
            champ_id = sel_by_puuid.get(puuid, "")
        if not champ_id and member.get("summonerId") is not None:
            champ_id = sel_by_sid.get(str(member.get("summonerId")).strip(), "")
        champ_name = _resolve_champion_name(champ_id, member, store)

        ranked_stats: dict[str, Any] | None = None
        history: list[dict[str, Any]] = []
        if puuid:
            try:
                ranked_stats = lcu.get_ranked_stats(puuid)
            except Exception as exc:
                logger.debug("LobbyScout ranked stats failed for slot: %s", exc)
            try:
                raw_hist = lcu.get_match_history(puuid, count=15)
                if isinstance(raw_hist, list):
                    history = [g for g in raw_hist if isinstance(g, dict)]
            except Exception as exc:
                logger.debug("LobbyScout match history failed for slot: %s", exc)

        scouted_idx += 1
        if pace_s > 0 and scouted_idx < total_players:
            time.sleep(pace_s)

        base_rating, rank_label = _compute_tier_base_and_label(ranked_stats)
        hidden_history = len(history) == 0
        if hidden_history:
            degraded_count += 1

        try:
            aram_wr, aram_games, kda, streak_tag, game_ids = _analyze_match_history(history, puuid)
        except Exception as exc:
            logger.debug("LobbyScout history analysis failed for slot: %s", exc)
            aram_wr, aram_games, kda, streak_tag, game_ids = 0.0, 0, 0.0, "", set()
            if not hidden_history:
                hidden_history = True
                degraded_count += 1
        hex_rating = _estimate_hex_rating(base_rating, aram_wr, aram_games, kda)

        card = PlayerScoutCard(
            puuid=puuid,
            summoner_name=summoner_name,
            champion_id=champ_id,
            champion_name=champ_name,
            team=side,
            hex_rating=hex_rating,
            rank_label=rank_label,
            aram_wr=aram_wr,
            aram_games=aram_games,
            kda=kda,
            streak_tag=streak_tag,
            premade_group="",
            hidden_history=hidden_history,
            is_mvp_carry=False,
        )
        return card, game_ids

    team_one_cards: list[PlayerScoutCard] = []
    team_one_gids: list[set[int]] = []
    for member in team_one_raw:
        c, gids = _scout_member(member, "ORDER")
        team_one_cards.append(c)
        team_one_gids.append(gids)

    team_two_cards: list[PlayerScoutCard] = []
    team_two_gids: list[set[int]] = []
    for member in team_two_raw:
        c, gids = _scout_member(member, "CHAOS")
        team_two_cards.append(c)
        team_two_gids.append(gids)

    team_one_cards, team_one_premades = _assign_team_premades(team_one_cards, team_one_gids)
    team_two_cards, team_two_premades = _assign_team_premades(team_two_cards, team_two_gids)

    local_on_team_two = bool(
        resolved_my_puuid and any(c.puuid == resolved_my_puuid for c in team_two_cards)
    )
    if local_on_team_two:
        my_cards_raw, enemy_cards_raw = team_two_cards, team_one_cards
        my_premades, enemy_premades = team_two_premades, team_one_premades
    else:
        my_cards_raw, enemy_cards_raw = team_one_cards, team_two_cards
        my_premades, enemy_premades = team_one_premades, team_two_premades

    # Sort each team descending by strength for clean UI display
    my_sorted = sorted(my_cards_raw, key=_card_rank_key, reverse=True)
    enemy_sorted = sorted(enemy_cards_raw, key=_card_rank_key, reverse=True)

    # Identify global MVP across all players and mark is_mvp_carry=True on that exact slot
    combined: list[tuple[str, int, PlayerScoutCard]] = [
        ("my", idx, c) for idx, c in enumerate(my_sorted)
    ] + [("enemy", idx, c) for idx, c in enumerate(enemy_sorted)]
    combined_ranked = sorted(combined, key=lambda item: _card_rank_key(item[2]), reverse=True)

    mvp_card: PlayerScoutCard | None = None
    mvp_lead_gap = 0
    if combined_ranked:
        mvp_side, mvp_idx, mvp_raw = combined_ranked[0]
        marked_mvp = replace(mvp_raw, is_mvp_carry=True)
        if mvp_side == "my":
            my_sorted[mvp_idx] = marked_mvp
        else:
            enemy_sorted[mvp_idx] = marked_mvp
        mvp_card = marked_mvp
        if len(combined_ranked) >= 2:
            mvp_lead_gap = max(0, marked_mvp.hex_rating - combined_ranked[1][2].hex_rating)

    top_enemy_card = enemy_sorted[0] if enemy_sorted else None

    premade_notes_list: list[str] = []
    for grp in enemy_premades:
        names = "+".join(m.champion_name for m in grp if m.champion_name)
        premade_notes_list.append(f"敌方{len(grp)}人车队({names})" if names else f"敌方{len(grp)}人车队")
    for grp in my_premades:
        names = "+".join(m.champion_name for m in grp if m.champion_name)
        premade_notes_list.append(f"我方{len(grp)}人车队({names})" if names else f"我方{len(grp)}人车队")

    return LobbyScoutSnapshot(
        my_team=tuple(my_sorted),
        enemy_team=tuple(enemy_sorted),
        mvp_card=mvp_card,
        mvp_lead_gap=mvp_lead_gap,
        top_enemy_card=top_enemy_card,
        premade_notes=tuple(premade_notes_list),
        degraded_count=degraded_count,
    )
