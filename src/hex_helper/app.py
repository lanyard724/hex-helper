"""HexHelper state machine and PySide6 application controller."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QSystemTrayIcon

from hex_helper.bridge.protocol import CoachAdvicePayload, CoachRequestPayload, utc_now_iso
from hex_helper.cloud.client import CloudAdviseStats, CloudCoachError, CloudCoachWorker, CloudGatewayClient
from hex_helper.cloud.session_store import DpapiSessionStore
from hex_helper.cloud.updater import UpdateInfo, check_for_update, launch_update_installer, parse_update_info
from hex_helper.config import (
    DATA_BAND_MODES,
    AppConfig,
    NormalizedRect,
    format_display_patch,
    get_app_data_dir,
    load_config,
    mask_api_key,
    resolve_thinking_level,
    save_config,
)
from hex_helper.data.aramhex_sync import AramHexSyncer
from hex_helper.data.hero_signatures import get_hero_signature_db
from hex_helper.data.provider import DatasetDescriptor
from hex_helper.data.locale_pack import (
    CLIENT_LOCALE_LABELS,
    desired_ocr_language_tag,
    normalize_client_locale,
)
from hex_helper.data.special_builds import get_special_augment_db
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.edition import APP_VERSION, is_commercial_edition, is_devlab_allowed
from hex_helper.setup.preflight import check_locale_ocr_readiness, extract_bundled_seed_cache

if TYPE_CHECKING:
    from hex_helper.coach.gemini_coach import GeminiCallStats
from hex_helper.game.action_tracker import MatchActionTracker
from hex_helper.game.lcu_client import LcuClient
from hex_helper.game.live_client import LiveClientReader
from hex_helper.game.lol_locator import is_riot_overseas_lockfile
from hex_helper.game.lobby_scout import LobbyScoutSnapshot, scout_lobby_readonly
from hex_helper.game.models import ChampSelectState, LiveMatchSnapshot
from hex_helper.logging_ring import install_match_log_handler
from hex_helper.scoring.augment_combo import (
    ComboPlan,
    PickerComboVerdict,
    build_combo_plan,
    effective_augment_round,
    estimate_effective_crit_chance,
    evaluate_picker_combos,
    export_combo_coach_context,
)
from hex_helper.scoring.build_synergy import is_transformative_augment, item_is_owned, normalize_item_id
from hex_helper.scoring.destiny import DESTINY_AUGMENT_ID, rank_destiny_items, summarize_destiny_card_hint
from hex_helper.scoring.matchup_optimizer import (
    TIER2_BOOT_IDS,
    TRINKET_AND_FREE_ITEM_IDS,
    DynamicItemRec,
    MatchupThreatProfile,
    NextBuyPlan,
    _infer_static_default_profile,
    analyze_matchup_threats,
    build_next_buy_plan,
    canonicalize_item_id,
    get_hero_build_templates,
    infer_hero_profile,
    infer_unified_hero_profile,
    is_valid_plan_item,
    optimize_next_items,
    reconcile_match_build_plan,
    reconcile_match_build_plan_with_trace,
    resolve_item_id_by_name,
)
from hex_helper.scoring.team_comp import TeamCompAnalysis, analyze_champ_select_pool
from hex_helper.scoring.tier import AugmentScore, rank_picker_cards, score_hero_augment, score_hero_tier
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay
from hex_helper.ui.calibration_ui import CalibrationOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.feedback_dialog import FeedbackDialog
from hex_helper.ui.hotkey_settings import HotkeySettingsDialog, format_hotkeys_summary
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.match_recap_card import (
    MatchRecapData,
    MatchRecapDialog,
    compute_match_rating,
    normalize_final_stats,
    normalize_recap_template,
)
from hex_helper.ui.theme import load_app_icon
from hex_helper.ui.win32_window import (
    GlobalHotkeyManager,
    UiBridgeInvoker,
    normalize_hotkey,
    physical_rect_to_logical,
    restore_foreground_window,
    screen_for_physical_rect,
)
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher, IconMatchResult
from hex_helper.vision.item_icon_matcher import ItemIconMatcher, VisualSlotVoteTracker, read_lol_hud_scale
from hex_helper.vision.capture import (
    ScreenCapturer,
    find_game_window,
    find_lcu_window,
    get_primary_screen_rect,
    is_game_or_overlay_foreground,
    is_hwnd_foreground,
)
from hex_helper.vision.click_tracker import ClickTracker, build_slot_hit_boxes
from hex_helper.vision.detector import AugmentPickerDetector, DetectedCard, PickerDetectionResult
from hex_helper.vision.matcher import AugmentMatcher
from hex_helper.vision.ocr_winrt import WinRtOcrEngine
from hex_helper.vision.transmute import (
    NON_TARGET_AUGMENT_IDS,
    PendingTransmute,
    TransmuteRevealWatcher,
    get_cursor_pos,
    get_transmute_spec,
    is_transmute_augment,
)
from hex_helper.vision.worker import HexVisionWorker, VisionFrameOutput, VisionTickContext

_ORIG_SCREEN_CAPTURER = ScreenCapturer
_ORIG_WINRT_OCR_ENGINE = WinRtOcrEngine
MAX_AUGMENT_SLOTS: int = 6
_STAT_ANVIL_IDS: frozenset[str] = frozenset({"1402", "1403", "1404"})
_BONUS_EVENT_CHOICE_IDS: frozenset[str] = frozenset({"3170", "3171", "3172", "3173"})
_NON_AUGMENT_CHOICE_IDS: frozenset[str] = _STAT_ANVIL_IDS | _BONUS_EVENT_CHOICE_IDS

logger = logging.getLogger(__name__)


def _normalize_coach_tier(tier: str | None) -> str:
    raw = (tier or "a").strip().lower()
    return raw if raw in ("a", "b", "auto") else "a"


def expected_augment_slots_for_level(level: int) -> int:
    """Expected ARAM Mayhem augment slot count unlocked by champion level (3 / 7 / 11 / 15)."""
    lvl = int(level or 0)
    if lvl >= 15:
        return 4
    if lvl >= 11:
        return 3
    if lvl >= 7:
        return 2
    if lvl >= 3:
        return 1
    return 0


SIGNIFICANT_TACTICAL_ITEM_IDS: frozenset[str] = frozenset(
    TIER2_BOOT_IDS | {"3123", "3916", "3076", "3140", "3191", "2420"}
)
_INSIGNIFICANT_MID_COMPONENT_IDS: frozenset[str] = frozenset(
    {
        "1001",  # Boots (300G)
        "2421",  # Shattered Armguard
        "3024",  # Glacial Buckler
        "3044",  # Phage
        "3057",  # Sheen
        "3067",  # Kindlegem
        "3070",  # Tear of the Goddess
        "3077",  # Tiamat
        "3082",  # Warden's Mail
        "3086",  # Zeal
        "3108",  # Fiendish Codex
        "3112",  # Guardian's Orb
        "3113",  # Aether Wisp
        "3133",  # Caulfield's Warhammer
        "3134",  # Serrated Dirk
        "3144",  # Scout's Slingshot
        "3145",  # Hextech Alternator
        "3155",  # Hexdrinker
        "3177",  # Guardian's Blade
        "3184",  # Guardian's Hammer
        "3211",  # Spectre's Cowl
        "3802",  # Lost Chapter
        "6660",  # Bami's Cinder
    }
)


def is_significant_enemy_item(item_id: str, store: DataStore | None = None) -> bool:
    """Return True if an enemy item is a complete item (>=2200G) or key tactical counter.

    Filters out cheap base components (Long Sword 350G, Ruby Crystal 400G, Amp Tome 435G,
    Poro-Snax, Tier-1 boots) so routine early-game shopping never burns wasted death AI calls.
    """
    raw = str(item_id or "").strip()
    if not raw:
        return False
    norm = normalize_item_id(raw)
    if norm in TRINKET_AND_FREE_ITEM_IDS or raw in TRINKET_AND_FREE_ITEM_IDS:
        return False
    if norm in SIGNIFICANT_TACTICAL_ITEM_IDS or raw in SIGNIFICANT_TACTICAL_ITEM_IDS:
        return True
    if store is not None and getattr(store, "static", None) is not None:
        items_map = store.static.items
        if raw in items_map or norm in items_map:
            info = store.get_item_info(raw)
            if info.category == "consumable":
                return False
            if info.category == "complete_equipment" or info.gold >= 2200:
                return True
            if info.has_grievous or info.has_shield_break:
                return True
            return False
    # Graceful fallback when store is absent or does not contain this item ID
    if norm in _INSIGNIFICANT_MID_COMPONENT_IDS:
        return False
    if norm.isdigit():
        num = int(norm)
        if 1000 <= num <= 1099 or 2000 <= num <= 2099:
            return False
    return True


class GameState(str, Enum):
    IDLE = "IDLE"
    CHAMP_SELECT = "CHAMP_SELECT"
    IN_GAME = "IN_GAME"
    AUGMENT_PICKING = "AUGMENT_PICKING"


@dataclass
class StateStepEffects:
    state_changed: bool = False
    coach_trigger: str = ""
    auto_peek_drawer: bool = False
    auto_collapse_drawer: bool = False
    newly_selected_augment: str = ""
    transmute_started: bool = False
    death_detected: bool = False
    enemy_sig_changed: bool = False
    death_skip_reason: str = ""
    is_bonus_event_offer: bool = False
    phantom_rolled_back_id: str = ""
    phantom_rollback_elapsed_s: float = 0.0


class HexStateMachine:
    """Pure, deterministic state machine governing match lifecycle and coach debounce."""

    PICKER_CLOSE_DEBOUNCE_S = 0.25
    DEATH_COACH_COOLDOWN_S = 50.0
    DEATH_SHOPPING_GOLD_THRESHOLD = 1100.0
    DEATH_FALLING_BEHIND_DIFF = 5
    VISUAL_ITEM_SUPPRESS_TTL_S = 45.0
    PRE_DEATH_WARMUP_COOLDOWN_S = 90.0
    PRE_DEATH_WARMUP_GOLD_THRESHOLD = 1400.0
    PHANTOM_ROLLBACK_WINDOW_S = 45.0
    MAX_AUGMENT_SLOTS = MAX_AUGMENT_SLOTS

    def __init__(self, store: DataStore | None = None) -> None:
        self._store: DataStore | None = store
        self.state: GameState = GameState.IDLE
        self.current_hero_id: str | None = None
        self.manual_hero_override: str | None = None
        self.selected_augments: list[str] = []
        self.manual_owned_items: list[str] = []
        self.visual_owned_items: list[str] = []
        self.suppressed_visual_items: dict[str, float] = {}
        self.suppressed_visual_augments: set[str] = set()
        self.is_degraded_2999: bool = False
        self.pending_transmute: PendingTransmute | None = None
        self.committed_plan: list[str] = []
        self.locked_next_item: str = ""
        self.allow_plan_overhaul: bool = False
        self.plan_overhaul_reason: str = ""
        self.manual_profile_override: str = ""
        self.active_cards: tuple[DetectedCard, ...] = ()
        self.last_offered_cards: tuple[DetectedCard, ...] = ()
        self._active_card_ids: tuple[str, ...] = ()
        self._picker_closed_since: float | None = None
        self._last_clicked_slot: int | None = None
        self._recent_offered_rounds: deque[tuple[str, ...]] = deque(maxlen=2)
        self._recent_offered_pool: list[str] = []
        self._last_auto_commit_snapshot: (
            tuple[str, frozenset[str], float, list[str], str, bool, str] | None
        ) = None
        self._was_dead: bool = False
        self._last_enemy_items_sig: str = ""
        self._last_death_coach_mono: float = -999.0
        self._last_warmup_coach_mono: float = -999.0
        self._last_warmup_enemy_sig: str = ""
        self._last_warmup_state_sig: tuple[Any, ...] = ()
        self._warmed_up_this_life: bool = False
        self._match_started_notified: bool = False

    def reset_match(self) -> None:
        self.current_hero_id = None
        self.selected_augments.clear()
        self.manual_owned_items.clear()
        self.visual_owned_items.clear()
        self.suppressed_visual_items.clear()
        self.suppressed_visual_augments.clear()
        self.is_degraded_2999 = False
        self.pending_transmute = None
        self.committed_plan.clear()
        self.locked_next_item = ""
        self.allow_plan_overhaul = False
        self.plan_overhaul_reason = ""
        self.manual_profile_override = ""
        self.active_cards = ()
        self.last_offered_cards = ()
        self._active_card_ids = ()
        self._picker_closed_since = None
        self._last_clicked_slot = None
        self._recent_offered_rounds.clear()
        self._recent_offered_pool.clear()
        self._last_auto_commit_snapshot = None
        self._was_dead = False
        self._last_enemy_items_sig = ""
        self._last_death_coach_mono = -999.0
        self._last_warmup_coach_mono = -999.0
        self._last_warmup_enemy_sig = ""
        self._last_warmup_state_sig = ()
        self._warmed_up_this_life = False
        self._match_started_notified = False

    def prune_suppressed_visual_items(self, now_mono: float | None = None) -> set[str]:
        now = time.monotonic() if now_mono is None else float(now_mono)
        if not isinstance(self.suppressed_visual_items, dict):
            # Defensive upgrade if legacy code assigned a set
            self.suppressed_visual_items = {
                str(k): now + self.VISUAL_ITEM_SUPPRESS_TTL_S for k in self.suppressed_visual_items
            }
        expired = [k for k, exp in self.suppressed_visual_items.items() if float(exp) <= now]
        for k in expired:
            self.suppressed_visual_items.pop(k, None)
        return set(self.suppressed_visual_items.keys())

    def suppress_visual_item(
        self,
        item_id: str,
        ttl_s: float = VISUAL_ITEM_SUPPRESS_TTL_S,
        now_mono: float | None = None,
    ) -> None:
        iid = canonicalize_item_id(str(item_id or "").strip())
        if not iid:
            return
        now = time.monotonic() if now_mono is None else float(now_mono)
        self.prune_suppressed_visual_items(now_mono=now)
        self.suppressed_visual_items[iid] = now + max(1.0, float(ttl_s))

    def is_visual_item_suppressed(self, item_id: str, now_mono: float | None = None) -> bool:
        iid = canonicalize_item_id(str(item_id or "").strip())
        if not iid:
            return False
        now = time.monotonic() if now_mono is None else float(now_mono)
        if not isinstance(self.suppressed_visual_items, dict):
            return iid in self.suppressed_visual_items
        exp = self.suppressed_visual_items.get(iid)
        if exp is None:
            return False
        if float(exp) <= now:
            self.suppressed_visual_items.pop(iid, None)
            return False
        return True

    def _rebuild_recent_offered_pool(self) -> None:
        selected = set(self.selected_augments)
        out: list[str] = []
        seen: set[str] = set()
        for rnd in reversed(self._recent_offered_rounds):
            for raw_aid in rnd:
                aid = str(raw_aid or "").strip()
                if not aid or aid in seen or aid in selected or aid in _NON_AUGMENT_CHOICE_IDS:
                    continue
                seen.add(aid)
                out.append(aid)
        self._recent_offered_pool = out

    def _record_recent_offered_round(self, card_ids: tuple[str, ...], *, same_round: bool = False) -> None:
        cleaned = tuple(
            dict.fromkeys(
                str(cid or "").strip()
                for cid in card_ids
                if str(cid or "").strip() and str(cid or "").strip() not in _NON_AUGMENT_CHOICE_IDS
            )
        )
        if not cleaned:
            return
        if same_round and self._recent_offered_rounds:
            merged = tuple(dict.fromkeys([*cleaned, *self._recent_offered_rounds[-1]]))
            self._recent_offered_rounds[-1] = merged
        elif not self._recent_offered_rounds or self._recent_offered_rounds[-1] != cleaned:
            self._recent_offered_rounds.append(cleaned)
        self._rebuild_recent_offered_pool()

    def recent_unrecorded_augments(self) -> list[str]:
        self._rebuild_recent_offered_pool()
        return list(self._recent_offered_pool)

    def add_selected_augment(self, aug_id: str) -> bool:
        aid = str(aug_id or "").strip()
        if (
            aid
            and aid not in _NON_AUGMENT_CHOICE_IDS
            and aid not in self.selected_augments
            and len(self.selected_augments) < self.MAX_AUGMENT_SLOTS
        ):
            self.selected_augments.append(aid)
            self._rebuild_recent_offered_pool()
            return True
        return False

    def add_manual_item(self, item_id: str) -> bool:
        iid = canonicalize_item_id(str(item_id or "").strip())
        if not iid or iid in ("0", "2052", "220013"):
            return False
        if isinstance(self.suppressed_visual_items, dict):
            self.suppressed_visual_items.pop(iid, None)
        else:
            self.suppressed_visual_items.discard(iid)
        if iid not in self.manual_owned_items and len(self.effective_degraded_items()) < 6:
            self.manual_owned_items.append(iid)
            return True
        return False

    def remove_owned_item(self, item_id: str) -> bool:
        iid = canonicalize_item_id(str(item_id or "").strip())
        if not iid:
            return False
        changed = False
        if iid in self.manual_owned_items:
            self.manual_owned_items = [x for x in self.manual_owned_items if x != iid]
            changed = True
        if iid in self.visual_owned_items:
            self.visual_owned_items = [x for x in self.visual_owned_items if x != iid]
            changed = True
        self.suppress_visual_item(iid)
        return changed

    def effective_degraded_items(self, now_mono: float | None = None) -> tuple[str, ...]:
        active_suppressed = self.prune_suppressed_visual_items(now_mono=now_mono)
        merged: list[str] = []
        seen: set[str] = set()
        for raw in (*self.manual_owned_items, *self.visual_owned_items):
            iid = canonicalize_item_id(str(raw or "").strip())
            if not iid or iid in seen or iid in active_suppressed:
                continue
            seen.add(iid)
            merged.append(iid)
            if len(merged) >= 6:
                break
        return tuple(merged)

    def invalidate_build_plan(self, reason: str = "") -> None:
        """Clear unbought committed plan & lock so build recommendations roll immediately to live augments/items."""
        self.committed_plan.clear()
        self.locked_next_item = ""
        self.allow_plan_overhaul = True
        self.plan_overhaul_reason = reason

    def remove_selected_augment(self, index: int) -> bool:
        if 0 <= index < len(self.selected_augments):
            removed = self.selected_augments.pop(index)
            if self.pending_transmute and self.pending_transmute.source_augment_id == removed:
                self.pending_transmute = None
            self._last_auto_commit_snapshot = None
            self._rebuild_recent_offered_pool()
            self.invalidate_build_plan()
            return True
        return False

    def replace_last_augment(self, aug_id: str) -> bool:
        aid = str(aug_id)
        if not aid or aid in _NON_AUGMENT_CHOICE_IDS or aid in self.selected_augments:
            return False
        if self.selected_augments:
            old = self.selected_augments[-1]
            self.selected_augments[-1] = aid
            if self.pending_transmute and self.pending_transmute.source_augment_id == old:
                self.pending_transmute = None
            self._last_auto_commit_snapshot = None
            self._rebuild_recent_offered_pool()
            self.invalidate_build_plan()
            return True
        return self.add_selected_augment(aid)

    def start_transmute_if_applicable(
        self,
        chosen_id: str,
        now_mono: float,
        fading_ids: tuple[str, ...] = (),
    ) -> PendingTransmute | None:
        spec = get_transmute_spec(chosen_id)
        if spec is None:
            return None
        exp_count = max(1, len(self.selected_augments)) if spec.replaces_all else spec.expected_count
        self.pending_transmute = PendingTransmute(
            source_augment_id=spec.augment_id,
            source_name=spec.name,
            expected_count=exp_count,
            target_rarity=spec.target_rarity,
            started_mono=now_mono,
            replaces_all=spec.replaces_all,
            excluded_fading_ids=fading_ids,
        )
        return self.pending_transmute

    def resolve_transmute_augment(self, new_aug_id: str) -> tuple[bool, bool]:
        """Resolve one rolled augment for an active PendingTransmute. Returns (accepted, fully_completed)."""
        pt = self.pending_transmute
        aid = str(new_aug_id)
        if pt is None or not aid or is_transmute_augment(aid):
            return False, False
        if aid in pt.resolved_ids:
            return False, False
        if not pt.replaces_all and aid in self.selected_augments:
            return False, False

        if not pt.placeholder_replaced:
            if pt.replaces_all:
                self.selected_augments = [aid]
                self.invalidate_build_plan()
            elif pt.source_augment_id in self.selected_augments:
                idx = self.selected_augments.index(pt.source_augment_id)
                self.selected_augments[idx] = aid
            else:
                self.add_selected_augment(aid)
            pt.placeholder_replaced = True
        else:
            if aid not in _NON_AUGMENT_CHOICE_IDS and aid not in self.selected_augments and len(self.selected_augments) < self.MAX_AUGMENT_SLOTS:
                self.selected_augments.append(aid)

        self._last_auto_commit_snapshot = None
        self._rebuild_recent_offered_pool()
        pt.resolved_ids.append(aid)
        if len(pt.resolved_ids) >= pt.expected_count:
            self.pending_transmute = None
            return True, True
        return True, False

    def dismiss_pending_transmute(self) -> bool:
        if self.pending_transmute is None:
            return False
        self.pending_transmute = None
        return True

    def on_lcu_poll(self, phase: str, cs: ChampSelectState | None) -> StateStepEffects:
        eff = StateStepEffects()
        if phase == "ChampSelect" and cs is not None and cs.is_mayhem:
            if self.state != GameState.CHAMP_SELECT:
                self.reset_match()
                self.state = GameState.CHAMP_SELECT
                eff.state_changed = True
            if cs.my_hero_id:
                self.current_hero_id = cs.my_hero_id
            return eff

        if phase in ("Lobby", "None", "EndOfGame", "PreEndOfGame", "Disconnected"):
            if self.state == GameState.CHAMP_SELECT:
                self.state = GameState.IDLE
                eff.state_changed = True
        return eff

    def _enemy_build_signature(
        self,
        snap: LiveMatchSnapshot,
        store: DataStore | None = None,
    ) -> str:
        """Build enemy signature tracking ONLY major complete items (>=2200G) and tactical counters (excluding Tier-2 boots)."""
        eff_store = store if store is not None else self._store
        return "|".join(
            f"{e.champion_name}:{','.join(sorted(normalize_item_id(iid) for iid in e.item_ids if normalize_item_id(iid) not in TIER2_BOOT_IDS and is_significant_enemy_item(iid, eff_store)))}"
            for e in snap.enemies
        )

    def on_live_snapshot(
        self,
        snap: LiveMatchSnapshot | None,
        resolved_hero_id: str | None,
        now_mono: float,
        *,
        store: DataStore | None = None,
    ) -> StateStepEffects:
        eff = StateStepEffects()
        if snap is None:
            if self.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
                self.reset_match()
                self.state = GameState.IDLE
                eff.state_changed = True
            return eff

        if self.state in (GameState.IDLE, GameState.CHAMP_SELECT):
            if self.state == GameState.IDLE:
                self.reset_match()
            self.state = GameState.IN_GAME
            eff.state_changed = True

        if resolved_hero_id:
            self.current_hero_id = resolved_hero_id
            self.manual_hero_override = None

        enemy_sig = self._enemy_build_signature(snap, store=store)

        if not self._match_started_notified and snap.enemies:
            self._match_started_notified = True
            self._last_enemy_items_sig = enemy_sig
            self._last_warmup_coach_mono = now_mono
            self._last_warmup_enemy_sig = enemy_sig
            self._last_warmup_state_sig = ()
            eff.coach_trigger = "game_start"

        eff_store = store if store is not None else self._store
        owned_complete_ids: list[str] = []
        owned_complete_count = 0
        if snap.my_player is not None:
            for iid in snap.my_player.item_ids:
                raw_iid = str(iid or "").strip()
                norm_iid = normalize_item_id(raw_iid)
                if not raw_iid or raw_iid in TRINKET_AND_FREE_ITEM_IDS or norm_iid in TRINKET_AND_FREE_ITEM_IDS:
                    continue
                if eff_store is not None and getattr(eff_store, "static", None) is not None:
                    if is_valid_plan_item(eff_store, raw_iid) or norm_iid in TIER2_BOOT_IDS:
                        owned_complete_count += 1
                        owned_complete_ids.append(norm_iid or raw_iid)
                elif norm_iid in TIER2_BOOT_IDS or (norm_iid.isdigit() and int(norm_iid) >= 2500):
                    owned_complete_count += 1
                    owned_complete_ids.append(norm_iid or raw_iid)

        is_dead = bool(snap.my_player and snap.my_player.is_dead)
        if is_dead and not self._was_dead:
            self._last_warmup_state_sig = ()
            self._warmed_up_this_life = False
            eff.auto_peek_drawer = True
            eff.death_detected = True
            if eff.coach_trigger != "game_start":
                enemy_changed = enemy_sig != self._last_enemy_items_sig
                eff.enemy_sig_changed = enemy_changed
                my_gold = float(snap.current_gold or 0.0)
                has_shopping_gold = my_gold >= self.DEATH_SHOPPING_GOLD_THRESHOLD
                falling_behind = bool(
                    snap.my_player is not None
                    and snap.my_player.deaths >= snap.my_player.kills + self.DEATH_FALLING_BEHIND_DIFF
                )
                # Suppress wasted falling_behind / routine shopping death calls once full 6 completed items are owned
                if owned_complete_count >= 6 and not enemy_changed:
                    falling_behind = False
                    has_shopping_gold = False
                cooldown_elapsed = now_mono - self._last_death_coach_mono
                if cooldown_elapsed < self.DEATH_COACH_COOLDOWN_S:
                    eff.death_skip_reason = (
                        f"cooldown ({cooldown_elapsed:.1f}s < {self.DEATH_COACH_COOLDOWN_S:.0f}s)"
                    )
                elif enemy_changed or has_shopping_gold or falling_behind:
                    self._last_enemy_items_sig = enemy_sig
                    self._last_death_coach_mono = now_mono
                    eff.coach_trigger = "death_enemy_build_changed"
                elif owned_complete_count >= 6 and not enemy_changed:
                    eff.death_skip_reason = "full_build_6_items_no_enemy_change"
                else:
                    kda_str = (
                        f"{snap.my_player.kills}/{snap.my_player.deaths}/{snap.my_player.assists}"
                        if snap.my_player is not None
                        else "0/0/0"
                    )
                    eff.death_skip_reason = (
                        f"no significant enemy item change, gold={int(my_gold)}G < "
                        f"{int(self.DEATH_SHOPPING_GOLD_THRESHOLD)}G, kda={kda_str}"
                    )
        elif not is_dead and self._was_dead:
            eff.auto_collapse_drawer = True
        elif (
            not is_dead
            and not self._was_dead
            and eff.coach_trigger == ""
            and self.state == GameState.IN_GAME
            and snap.enemies
            and float(snap.game_time or 0.0) >= 90.0
            and owned_complete_count < 6
        ):
            # WS-3 / v1.5.2 Pre-Death Warmup: single warmup per life unless enemy major items or augments change
            warmup_cd_ok = (
                (now_mono - self._last_warmup_coach_mono) >= self.PRE_DEATH_WARMUP_COOLDOWN_S
                and (now_mono - self._last_death_coach_mono) >= self.PRE_DEATH_WARMUP_COOLDOWN_S
            )
            if warmup_cd_ok:
                my_gold = float(snap.current_gold or 0.0)
                warmup_enemy_changed = (
                    enemy_sig != self._last_enemy_items_sig
                    and enemy_sig != self._last_warmup_enemy_sig
                )
                cur_warmup_sig = (
                    enemy_sig,
                    tuple(sorted(owned_complete_ids)),
                    tuple(self.selected_augments),
                )
                first_this_life = not self._warmed_up_this_life
                augs_changed_since_warmup = (
                    bool(self._last_warmup_state_sig)
                    and len(self._last_warmup_state_sig) >= 3
                    and tuple(self.selected_augments) != self._last_warmup_state_sig[2]
                )
                should_warmup = (
                    first_this_life
                    and (my_gold >= self.PRE_DEATH_WARMUP_GOLD_THRESHOLD or warmup_enemy_changed)
                ) or (
                    not first_this_life
                    and (warmup_enemy_changed or augs_changed_since_warmup)
                )
                if should_warmup and cur_warmup_sig != self._last_warmup_state_sig:
                    self._last_warmup_coach_mono = now_mono
                    self._last_warmup_enemy_sig = enemy_sig
                    self._last_warmup_state_sig = cur_warmup_sig
                    self._warmed_up_this_life = True
                    eff.coach_trigger = "pre_death_warmup"
        self._was_dead = is_dead
        return eff

    def _is_stat_anvil_offer(self, cards: tuple[DetectedCard, ...] | list[DetectedCard]) -> bool:
        if not cards:
            return False
        unique_ids = {c.augment.augment_id for c in cards}
        if len(unique_ids) < 2:
            return True
        if "1404" in unique_ids and len(unique_ids) < len(cards):
            return True
        anvil_cnt = sum(1 for c in cards if c.augment.augment_id in _STAT_ANVIL_IDS)
        return anvil_cnt >= 2 or (len(self.selected_augments) >= 4 and anvil_cnt >= 1)

    @staticmethod
    def _is_bonus_event_offer(cards: tuple[DetectedCard, ...] | list[DetectedCard]) -> bool:
        return bool(cards) and any(str(c.augment.augment_id) in _BONUS_EVENT_CHOICE_IDS for c in cards)

    def _maybe_apply_phantom_rollback(
        self,
        new_ids: tuple[str, ...],
        *,
        now_mono: float,
        eff: StateStepEffects,
    ) -> None:
        snap = self._last_auto_commit_snapshot
        self._last_auto_commit_snapshot = None
        if snap is None:
            return
        (
            snap_aid,
            snap_offered,
            snap_mono,
            snap_plan,
            snap_lock,
            snap_allow_overhaul,
            snap_overhaul_reason,
        ) = snap
        new_id_set = frozenset(str(x) for x in new_ids if x)
        elapsed_s = max(0.0, float(now_mono) - float(snap_mono))
        if (
            elapsed_s <= self.PHANTOM_ROLLBACK_WINDOW_S
            and snap_aid in new_id_set
            and len(new_id_set & snap_offered) >= 2
            and self.selected_augments
            and self.selected_augments[-1] == snap_aid
        ):
            self.selected_augments.pop()
            if self.pending_transmute and self.pending_transmute.source_augment_id == snap_aid:
                self.pending_transmute = None
            self.committed_plan = list(snap_plan)
            self.locked_next_item = snap_lock
            self.allow_plan_overhaul = bool(snap_allow_overhaul)
            self.plan_overhaul_reason = snap_overhaul_reason
            eff.phantom_rolled_back_id = snap_aid
            eff.phantom_rollback_elapsed_s = elapsed_s
            self._rebuild_recent_offered_pool()

    def on_picker_result(
        self,
        result: PickerDetectionResult,
        clicked_slot: int | None,
        now_mono: float,
    ) -> StateStepEffects:
        eff = StateStepEffects()
        if self.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            return eff

        if clicked_slot is not None and self.state == GameState.AUGMENT_PICKING:
            self._last_clicked_slot = clicked_slot

        if result.picker_visible and result.stable and result.cards:
            # Ignore non-augment Stat Anvil popups (1402/1403/1404 & duplicate IDs)
            if self._is_stat_anvil_offer(result.cards):
                return eff
            # Isolate bonus event choices (e.g. 3170-3173 Pentakill bonus) from augment state/selection/pools
            if self._is_bonus_event_offer(result.cards):
                eff.is_bonus_event_offer = True
                return eff
            self._picker_closed_since = None
            new_ids = tuple(c.augment.augment_id for c in result.cards)
            prev_active_ids = self._active_card_ids
            was_already_picking = self.state == GameState.AUGMENT_PICKING
            if not was_already_picking:
                self._maybe_apply_phantom_rollback(new_ids, now_mono=now_mono, eff=eff)
                self.state = GameState.AUGMENT_PICKING
                self._last_clicked_slot = None
                eff.state_changed = True
            if new_ids != prev_active_ids or result.rerolled:
                self.active_cards = result.cards
                self.last_offered_cards = result.cards
                self._active_card_ids = new_ids
                self._last_clicked_slot = None
                same_round = bool(
                    was_already_picking
                    and prev_active_ids
                    and (set(prev_active_ids) & set(new_ids))
                )
                self._record_recent_offered_round(new_ids, same_round=same_round)
                eff.coach_trigger = "augment_picking"
            return eff

        if not result.picker_visible and self.state == GameState.AUGMENT_PICKING:
            if self._picker_closed_since is None:
                self._picker_closed_since = now_mono
                return eff
            if (now_mono - self._picker_closed_since) >= self.PICKER_CLOSE_DEBOUNCE_S:
                chosen_id = ""
                if self._last_clicked_slot is not None:
                    for c in self.active_cards:
                        if c.slot_index == self._last_clicked_slot:
                            chosen_id = c.augment.augment_id
                            break
                if chosen_id in _NON_AUGMENT_CHOICE_IDS:
                    chosen_id = ""
                prev_plan = list(self.committed_plan)
                prev_lock = self.locked_next_item
                prev_allow_overhaul = bool(self.allow_plan_overhaul)
                prev_overhaul_reason = str(self.plan_overhaul_reason or "")
                offered_id_set = frozenset(c.augment.augment_id for c in self.active_cards if c.augment.augment_id)
                if chosen_id and self.add_selected_augment(chosen_id):
                    eff.newly_selected_augment = chosen_id
                    self._last_auto_commit_snapshot = (
                        chosen_id,
                        offered_id_set,
                        float(now_mono),
                        prev_plan,
                        prev_lock,
                        prev_allow_overhaul,
                        prev_overhaul_reason,
                    )
                    if is_transmute_augment(chosen_id):
                        fading = tuple(c.augment.augment_id for c in self.active_cards if c.augment.augment_id != chosen_id)
                        self.start_transmute_if_applicable(chosen_id, now_mono=now_mono, fading_ids=fading)
                        eff.transmute_started = True
                    else:
                        eff.coach_trigger = "augment_selected"
                else:
                    self._last_auto_commit_snapshot = None
                self.state = GameState.IN_GAME
                self.active_cards = ()
                self._active_card_ids = ()
                self._picker_closed_since = None
                self._last_clicked_slot = None
                self._rebuild_recent_offered_pool()
                eff.state_changed = True
        return eff


def _build_tray_icon() -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#0B1220"))
    p.setPen(QColor("#C8AA6E"))
    p.drawRoundedRect(2, 2, 28, 28, 6, 6)
    p.setPen(QColor("#0AC8B9"))
    p.drawText(pm.rect(), 0x0084, "H")
    p.end()
    return QIcon(pm)


class _ThreadSafeLcuProxy:
    """Per-call synchronized read-only LCU proxy used when tests inject or patch controller.lcu."""

    def __init__(self, target: Any, lock: threading.RLock) -> None:
        self._target = target
        self._lock = lock

    def get_gameflow_session(self) -> dict[str, Any] | None:
        with self._lock:
            return self._target.get_gameflow_session()

    def get_current_summoner(self) -> dict[str, Any] | None:
        with self._lock:
            fn = getattr(self._target, "get_current_summoner", None)
            return fn() if callable(fn) else None

    def get_ranked_stats(self, puuid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._target.get_ranked_stats(puuid)

    def get_match_history(self, puuid: str, count: int = 15) -> list[dict[str, Any]]:
        with self._lock:
            return self._target.get_match_history(puuid, count=count)


_NON_MAYHEM_GAME_MODES: frozenset[str] = frozenset(
    {"CLASSIC", "PRACTICETOOL", "CUSTOM", "TUTORIAL", "TFT", "CHERRY", "ARENA", "URF", "ONEFORALL"}
)
_MAYHEM_QUEUE_IDS: frozenset[int] = frozenset({2400, 2401, 2402, 450})


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class _NullFlightRecorder:
    """Zero-overhead no-op Flight Recorder mounted in commercial or non-DevLab sessions."""

    active: bool = False
    enable_roi_snapshots: bool = False
    enable_debug_log: bool = False
    root_dir: Path | None = None

    def start(self) -> None:
        return None

    def emit(
        self,
        domain: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        game_time_s: float | None = None,
        game_state: str | None = None,
        hero_id: str | None = None,
        dataset_tag: str | None = None,
        priority: str = "normal",
    ) -> int:
        return 0

    def enqueue_roi_snapshot(self, tag: str, bgr_crop: Any, ev_seq: int = 0) -> str | None:
        return None

    def begin_champ_select(
        self,
        hero_id: str = "",
        dataset_tag: str = "",
        match_key_hash: str = "",
    ) -> str:
        return ""

    def begin_match(
        self,
        match_key_hash: str = "",
        hero_id: str = "",
        hero_name: str = "",
        dataset_tag: str = "",
        game_mode: str = "",
        is_non_mayhem: bool = False,
    ) -> str:
        return ""

    def update_match_meta(self, **kwargs: Any) -> None:
        return None

    def end_match(
        self,
        won: int = -1,
        duration_s: int = 0,
        eog_source: str = "",
        summary_extra: dict[str, Any] | None = None,
    ) -> None:
        return None

    def record_latency_sample(self, op: str, elapsed_ms: float) -> None:
        return None

    def flush_latency_summary(self, *, force: bool = False) -> bool:
        return False

    def set_context(self, **kwargs: Any) -> None:
        return None

    def get_recent_events(self, since_seq: int = 0) -> list[dict[str, Any]]:
        return []

    def get_recent_vision_traces(self, since_seq: int = 0) -> list[dict[str, Any]]:
        return []

    def get_status_snapshot(self) -> dict[str, Any]:
        return {
            "active": False,
            "recording_match": False,
            "active_match_seq": None,
            "event_count": 0,
            "queue_depth": 0,
            "max_queue_depth": 0,
            "dropped_events": 0,
            "dropped_roi": 0,
            "roi_count": 0,
            "roi_cap": 15,
            "disk_bytes": 0,
            "roi_disk_bytes": 0,
            "match_count": 0,
            "max_matches": 120,
            "enable_roi_snapshots": False,
            "enable_debug_log": False,
            "root_dir": "",
            "app_version": APP_VERSION,
        }

    def set_roi_snapshots_enabled(self, enabled: bool) -> None:
        return None

    def flush(self, timeout_s: float = 2.0) -> bool:
        return True

    def flush_and_close(self, timeout_s: float = 3.0) -> None:
        return None


_NULL_FLIGHT_RECORDER = _NullFlightRecorder()


class HexHelperController:
    """Main desktop runtime integrating data sync, telemetry, WinRT OCR, UI overlays, and AI coach."""

    PICKER_COACH_DEBOUNCE_MS = 600
    PICKER_COACH_REROLL_DEBOUNCE_MS = 1400
    DEATH_COACH_DEFER_MS = 4000
    AUGMENT_REMOVE_DEFER_MS = 2500
    GAME_START_SCOUT_WAIT_MS = 2500
    ICON_VERIFY_DELAY_MS = 850
    ICON_VERIFY_CONF_THRESHOLD = 0.65
    SMART_PICKER_GATE_DELTA_GAP = 0.012
    SMART_PICKER_GATE_DECISIVE_DELTA = 0.025
    MANUAL_F6_DEDUP_WINDOW_S = 10.0
    AUGMENT_SELECTED_DEDUP_WINDOW_S = 15.0
    TELEMETRY_MIN_DURATION_S = 180
    PENDING_EOG_TIMEOUT_S = 18.0
    LIVE_DISCONNECT_DEBOUNCE_FAILS = 3
    LCU_2999_DEGRADED_GRACE_FAILURES = 12
    WARMUP_COVERS_DEATH_WINDOW_S = 120.0
    WARMUP_INFLIGHT_REUSE_WINDOW_S = 15.0

    # Safe class-level defaults for lightweight unit tests instantiating via __new__
    _commercial: bool = False
    _cached_non_overlay_hwnds: tuple[int, ...] = ()
    _match_session_seq: int = 0
    _last_cs_game_id: int = 0
    _last_cs_queue_id: int = 0
    _last_finished_game_id: int = 0
    _last_picker_partial_open: bool = False
    _last_picker_expected_slots: int = 0
    _hud_wide_diag_saved_this_match: bool = False
    _death_defer_superseded_by_picker: bool = False
    _death_superseded_req_id: str = ""
    _match_last_skin_id: int = 0
    _match_last_skin_name: str = ""
    _skin_splash_fetching: Any = None
    _skin_prefetched_keys: Any = None
    _devlab_active: bool = False
    _devlab_roi_enabled: bool = False
    _devlab_debug_enabled: bool = False
    _devlab: Any = _NULL_FLIGHT_RECORDER
    _action_tracker: MatchActionTracker | None = None
    _ui_observers: Any = None
    _devlab_badge_timer: QTimer | None = None
    _vision_worker: HexVisionWorker | None = None
    _vision_gen: int = 0
    _vision_backend: str = ""
    _vision_consec_black: int = 0
    _vision_ocr_ready: bool = False
    _visual_is_dead: bool | None = None
    _is_mocked_vision: bool = False
    _orig_capturer_id: int = -1
    _match_last_my_stats: dict[str, float] | None = None
    _last_drawer_refresh_sig: tuple[Any, ...] | None = None
    _last_manual_f6_sig: tuple[Any, ...] | None = None
    _last_manual_f6_mono: float = 0.0
    _last_warmup_advice_mono: float = 0.0
    _last_warmup_advice_enemy_sig: str = ""
    _last_warmup_dispatched_mono: float = 0.0
    _last_warmup_dispatched_enemy_sig: str = ""
    _last_augment_selected_sig: tuple[Any, ...] | None = None
    _last_augment_selected_mono: float = 0.0
    _augment_remove_coach_timer: QTimer | None = None
    _pending_manual_f6_after_plan_b: bool = False
    _hotkey_conflict_toast_shown: bool = False
    _vision_window_start_mono: float = 0.0
    _vision_window_total_frames: int = 0
    _vision_window_idle_dual_frames: int = 0
    _last_fast_vanish_diffs: dict[int, float] | None = None
    _port_2999_conflict_detected: bool = False
    _port_2999_conflict_probed: bool = False
    _item_vote_tracker: VisualSlotVoteTracker | None = None
    _aug_vote_tracker: VisualSlotVoteTracker | None = None
    _last_plan_b_scan_mono: float = 0.0
    _hud_scale: float = 1.0
    _pending_eog_since: float | None = None
    _pending_eog_phase: str = ""
    _lcu_no_live_polls: int = 0
    _last_lcu_phase: str = ""
    _active_game_id: int = 0
    _active_queue_id: int = 0
    _match_is_non_mayhem: bool = False
    _match_game_mode: str = ""
    _champ_closed_by_user_in_session: bool = False
    _last_capture_backend: str = ""
    _last_capture_black: bool = False
    _picker_opened_mono: float = 0.0
    _last_picker_game_rect: Any = None
    _last_picker_reroll_y: int | None = None
    _last_offer_rec_snapshot: dict[str, Any] | None = None
    _last_offer_emitted_sig: tuple[Any, ...] | None = None
    _pending_reroll_prev_offer: dict[str, Any] | None = None
    _pending_reroll_slots: list[int] | None = None
    _click_last_tick_mono: float = 0.0
    _click_max_gap_ms_in_picker: float = 0.0
    _picker_opened_since_level: bool = False
    _last_live_level: int = 0
    _last_smart_gate_skip_mono: float = 0.0
    _last_smart_gate_skip_detail: str = ""
    _last_advice_mono: float = 0.0
    _active_picker_coach_req_id: str = ""
    _active_picker_coach_arrived: bool = False
    _active_picker_coach_aug_advice: str = ""
    _last_idle_dual_pass_emit_mono: float = 0.0
    _devlab_report_in_flight: bool = False

    @property
    def config(self) -> AppConfig:
        return self.cfg

    @config.setter
    def config(self, value: AppConfig) -> None:
        self.cfg = value

    def __init__(
        self,
        qt_app: QApplication,
        show_lookup_on_start: bool = True,
        *,
        cloud_http_client_factory: Callable[..., Any] | None = None,
        devlab_flag: bool = False,
        devlab_roi_flag: bool = False,
        devlab_debug_flag: bool = False,
        devlab_enabled: bool | None = None,
        devlab_roi_snapshots: bool | None = None,
        devlab_debug_log: bool | None = None,
    ) -> None:
        self.qt_app = qt_app
        self._commercial = is_commercial_edition()
        self.data_dir = get_app_data_dir()
        self.log_ring = install_match_log_handler(data_dir=self.data_dir)
        self.cfg: AppConfig = load_config()
        if self._commercial:
            self.cfg.proxy_url = ""
            self.cfg.bridge_enabled = False
            self.cfg.devlab_enabled = False
            self.cfg.devlab_roi_snapshots = False
            self.cfg.devlab_debug_log = False
            if self.cfg.coach_mode != "off":
                self.cfg.coach_mode = "cloud"

        # Lazy dynamic DevLab mount (ZERO static hex_helper.devlab imports in commercial builds)
        self._devlab: Any = _NullFlightRecorder()
        self._devlab_active: bool = False
        self._devlab_roi_enabled: bool = False
        self._devlab_debug_enabled: bool = False
        self._action_tracker: MatchActionTracker | None = None
        self._ui_observers: Any = None
        self._devlab_badge_timer: QTimer | None = None
        self._devlab_report_in_flight: bool = False
        self._init_devlab_recorder(
            devlab_flag=bool(devlab_flag or (devlab_enabled is True)),
            devlab_roi_flag=bool(devlab_roi_flag or (devlab_roi_snapshots is True)),
            devlab_debug_flag=bool(devlab_debug_flag or (devlab_debug_log is True)),
        )

        self.ui_invoker = UiBridgeInvoker()

        if self._commercial:
            mirror_base = f"{self.cfg.resolved_cloud_api_base()}/v1/updates/dataset"
            self.static_assets = StaticAssetsManager(
                self.data_dir / "cache",
                proxy_url="",
                mirror_base_url=mirror_base,
            )
            self.syncer = AramHexSyncer(
                self.data_dir / "cache",
                proxy_url="",
                base_url=mirror_base,
            )
        else:
            self.static_assets = StaticAssetsManager(self.data_dir / "cache", proxy_url=self.cfg.proxy_url)
            self.syncer = AramHexSyncer(self.data_dir / "cache", proxy_url=self.cfg.proxy_url)
        self.cfg.client_locale = normalize_client_locale(getattr(self.cfg, "client_locale", "zh_cn"))
        self.store = DataStore(self.static_assets)
        self.store.set_client_locale(self.cfg.client_locale)
        self._applied_client_locale: str = self.cfg.client_locale
        self.sm = HexStateMachine(store=self.store)
        self.datasets: list[DatasetDescriptor] = []

        # Telemetry
        self._lcu_lock = threading.RLock()
        self.lcu = LcuClient(self.cfg.lockfile_path, force_all_modes=self.cfg.force_all_modes)
        self.live = LiveClientReader()
        self._last_live: LiveMatchSnapshot | None = None
        self._last_threat: MatchupThreatProfile | None = None
        self._last_dyn_recs: list[DynamicItemRec] = []
        self._last_next_plan: NextBuyPlan | None = None
        self._applied_data_band_mode: str = self._current_data_band_mode()
        self._overlays_hidden_by_user = False

        # Vision (WS-1: offload ScreenCapturer/WinRT OCR/Plan B onto persistent MTA HexVisionWorker)
        self._is_mocked_vision = (ScreenCapturer is not _ORIG_SCREEN_CAPTURER) or (
            WinRtOcrEngine is not _ORIG_WINRT_OCR_ENGINE
        )
        self.capturer = ScreenCapturer(prefer_dxcam=True)
        self._orig_capturer_id = id(self.capturer)
        self.ocr_engine: WinRtOcrEngine | None = None
        self.matcher = AugmentMatcher([])
        self.detector: AugmentPickerDetector | None = None
        self.transmute_watcher: TransmuteRevealWatcher | None = None
        self.click_tracker = ClickTracker()
        self.icon_matcher: AugmentIconMatcher | None = None
        try:
            self.icon_matcher = AugmentIconMatcher(
                cache_dir=self.data_dir / "cache",
                proxy_url="" if self._commercial else self.cfg.proxy_url,
                allow_download=not self._commercial,
            )
        except Exception as exc:
            logger.debug("AugmentIconMatcher init skipped: %s", exc)
        self.item_icon_matcher: ItemIconMatcher | None = None
        try:
            self.item_icon_matcher = ItemIconMatcher(cache_dir=self.data_dir / "cache")
        except Exception as exc:
            logger.debug("ItemIconMatcher init skipped: %s", exc)
        self._item_vote_tracker = VisualSlotVoteTracker(confirm_frames=2, drop_misses=3)
        self._aug_vote_tracker = VisualSlotVoteTracker(confirm_frames=3, drop_misses=6)
        self._hud_scale: float = read_lol_hud_scale(
            getattr(self.cfg, "lol_install_dir", ""),
            getattr(self.cfg, "lockfile_path", ""),
        )
        self._last_plan_b_scan_mono: float = 0.0
        self._aug_preheat_started: bool = False
        self._vision_gen: int = 0
        self._vision_backend: str = ""
        self._vision_consec_black: int = 0
        self._vision_ocr_ready: bool = False
        self._visual_is_dead: bool | None = None
        self._vision_worker: HexVisionWorker | None = None
        self._last_fast_vanish_diffs: dict[int, float] = {}
        self._vision_window_start_mono: float = time.monotonic()
        self._vision_window_total_frames: int = 0
        self._vision_window_idle_dual_frames: int = 0
        req_lang = desired_ocr_language_tag(self.cfg.client_locale)
        if self._is_mocked_vision:
            try:
                try:
                    self.ocr_engine = WinRtOcrEngine(req_lang)
                except TypeError:
                    self.ocr_engine = WinRtOcrEngine()
                if self.ocr_engine is not None:
                    use_t2s = bool(getattr(self.ocr_engine, "needs_t2s_matching", False))
                    self.matcher.update_catalog([], use_t2s=use_t2s)
                    self.detector = AugmentPickerDetector(
                        ocr_engine=self.ocr_engine,
                        matcher=self.matcher,
                        card_band_roi=self.cfg.card_band_roi,
                        title_band_roi=self.cfg.title_band_roi,
                        client_locale=self.cfg.client_locale,
                    )
                    self.transmute_watcher = TransmuteRevealWatcher(
                        self.ocr_engine,
                        [],
                        client_locale=self.cfg.client_locale,
                        use_t2s=use_t2s,
                    )
            except Exception as exc:
                logger.error("WinRT OCR initialization failed: %s", exc)
        else:
            self._vision_worker = HexVisionWorker(
                on_output=lambda out: self.ui_invoker.invoke.emit(
                    lambda o=out: self._on_vision_worker_output(o)
                ),
                prefer_dxcam=True,
                client_locale=self.cfg.client_locale,
                language_tag=req_lang,
                cache_dir=self.data_dir / "cache",
                card_band_roi=self.cfg.card_band_roi,
                title_band_roi=self.cfg.title_band_roi,
                find_window_fn=lambda: find_game_window(),
                is_foreground_fn=lambda hwnd: self._is_game_or_overlay_fg_worker(hwnd),
                primary_rect_fn=lambda: get_primary_screen_rect(),
            )

        # AI Coach backend: Commercial Cloud Gateway OR Dev Direct Gemini + Loopback Bridge
        self._cloud_ai_cutoff: bool = False
        self._cloud_cutoff_code: str = ""
        self._cloud_cutoff_reason: str = ""
        self._cloud_cutoff_policy: str = "ai_only"
        self._service_locked: bool = False
        self._service_lock_reason: str = ""
        self._force_update_required: bool = False
        self._force_update_reason: str = ""
        self._wallet_ai_cutoff: bool = False
        self._wallet_cutoff_code: str = ""
        self._wallet_cutoff_reason: str = ""
        self._pending_update_info: UpdateInfo | None = None
        self._dismissed_optional_version: str = ""
        self._cloud_state_lock = threading.Lock()
        self._wallet_fetch_seq: int = 0
        self._applied_wallet_seq: int = 0
        self._gate_fetch_seq: int = 0
        self._applied_gate_seq: int = 0
        self._last_cs: ChampSelectState | None = None
        self.session_store: DpapiSessionStore | None = None
        self.cloud_client: CloudGatewayClient | None = None
        self.cloud_coach: CloudCoachWorker | None = None
        self._gemini_client = None
        self.gemini_coach = None
        self.bridge = None
        self._wallet_heartbeat_timer: QTimer | None = None
        self._wallet_retry_timer: QTimer | None = None
        self._wallet_retry_backoff_s: int = 8
        self._startup_update_timer: QTimer | None = None
        self._update_prompt_open: bool = False
        self._update_check_in_flight: bool = False
        self._update_check_manual_pending: bool = False

        if self._commercial:
            self.session_store = DpapiSessionStore(self.data_dir / "session.dat")
            self.cloud_client = CloudGatewayClient(
                base_url=self.cfg.resolved_cloud_api_base(),
                session_store=self.session_store,
                http_client_factory=cloud_http_client_factory,
            )
            self.cloud_client.restore_session()
            self.cloud_coach = CloudCoachWorker(
                client=self.cloud_client,
                on_advice=lambda adv, st: self.ui_invoker.invoke.emit(lambda a=adv, s=st: self._on_cloud_advice(a, s)),
                on_cutoff=lambda code, msg, w: self.ui_invoker.invoke.emit(
                    lambda c=code, m=msg, wl=w: self._on_cloud_cutoff(c, m, wl)
                ),
                on_error=lambda rid, err: self.ui_invoker.invoke.emit(lambda r=rid, e=err: self._on_cloud_error(r, e)),
            )
        else:
            from hex_helper.bridge.agent_bridge import AgentBridgeServer
            from hex_helper.coach.gemini_coach import GeminiCoachClient, GeminiCoachWorker

            self._gemini_client = GeminiCoachClient()
            self.gemini_coach = GeminiCoachWorker(
                on_advice=lambda adv, st: self.ui_invoker.invoke.emit(lambda a=adv, s=st: self._on_gemini_advice(a, s)),
                on_error=lambda rid, err: self.ui_invoker.invoke.emit(lambda r=rid, e=err: self._on_gemini_error(r, e)),
                on_superseded=lambda req, st: self.ui_invoker.invoke.emit(
                    lambda r=req, s=st: self._on_gemini_coach_superseded(r, s)
                ),
                client=self._gemini_client,
                max_workers=2,
            )
            self.bridge = AgentBridgeServer(
                bridge_dir=self.data_dir / "bridge",
                port=self.cfg.bridge_port,
                on_advice=lambda adv: self.ui_invoker.invoke.emit(lambda: self._on_coach_advice(adv)),
                on_online_changed=lambda ok: self.ui_invoker.invoke.emit(lambda: self._on_coach_online(ok)),
                dev_events_provider=(self._devlab.get_recent_events if self._devlab_active else None),
                dev_vision_provider=(self._devlab.get_recent_vision_traces if self._devlab_active else None),
            )

        self._latest_advice: CoachAdvicePayload | None = None
        self._latest_request_id: str = ""
        self._latest_request_trigger: str = ""
        self._last_cards_seen_mono: float = 0.0
        self._frozen_pick_slot: int | None = None
        self._frozen_pick_source: str = ""
        self._frozen_pick_confidence: float = 0.0
        self._picker_slot_refs: dict[int, tuple[tuple[int, int, int, int], np.ndarray]] = {}
        self._picker_ref_band_bbox: tuple[int, int, int, int] | None = None
        self._visual_vanish_mono: float | None = None
        self._last_rerolled_slot: int | None = None
        self._last_reroll_mono: float = 0.0
        self._click_sample_counter: int = 0
        self._active_match_key: str = ""
        self._lobby_scout: LobbyScoutSnapshot | None = None
        self._lobby_scout_match_key: str = ""
        self._lobby_scout_in_flight: bool = False
        self._pending_game_start_match_key: str = ""
        self._match_state_path = self.data_dir / "match_state.json"
        self._combo_hidden_by_user: bool = False
        self._last_combo_plan: ComboPlan | None = None
        self._last_combo_verdict: PickerComboVerdict | None = None
        self._pre_picker_drawer_expanded: bool | None = None

        # Per-match telemetry, PENDING_EOG buffer & DevLab tracking state
        self.ai_calls_this_match: int = 0
        self.ai_recommended_item_ids_shown: set[str] = set()
        self.user_swapped_this_match: int = 0
        self._match_max_game_time_s: float = 0.0
        self._match_last_my_items: tuple[str, ...] = ()
        self._match_last_my_stats: dict[str, float] = {}
        self._match_last_hero_id: str = ""
        self._match_last_hero_name: str = ""
        self._last_drawer_refresh_sig: tuple[Any, ...] | None = None
        self._last_manual_f6_sig: tuple[Any, ...] | None = None
        self._last_manual_f6_mono: float = 0.0
        self._last_warmup_advice_mono: float = 0.0
        self._last_warmup_advice_enemy_sig: str = ""
        self._last_warmup_dispatched_mono: float = 0.0
        self._last_warmup_dispatched_enemy_sig: str = ""
        self._last_augment_selected_sig: tuple[Any, ...] | None = None
        self._last_augment_selected_mono: float = 0.0
        self._pending_manual_f6_after_plan_b: bool = False
        self._hotkey_conflict_toast_shown: bool = False
        self._port_2999_conflict_detected: bool = False
        self._match_telemetry_sent_key: str = ""
        self._match_is_first_prismatic: bool = False
        self._match_won_result: int = -1
        self._auto_switched_first_prismatic: bool = False
        self._last_kda: str = ""
        self._last_team_kills: str = ""
        self._best_ai_highlight: str = ""
        self._last_recap_data: MatchRecapData | None = None
        self._active_recap_dialog: MatchRecapDialog | None = None
        self._recap_icon_fetching: set[tuple[str, str]] = set()
        self._cached_non_overlay_hwnds: tuple[int, ...] = ()
        self._match_session_seq: int = 0
        self._last_cs_game_id: int = 0
        self._last_cs_queue_id: int = 0
        self._last_finished_game_id: int = 0
        self._last_picker_partial_open: bool = False
        self._last_picker_expected_slots: int = 0
        self._hud_wide_diag_saved_this_match: bool = False
        self._death_defer_superseded_by_picker: bool = False
        self._death_superseded_req_id: str = ""
        self._match_last_skin_id: int = 0
        self._match_last_skin_name: str = ""
        self._skin_splash_fetching: set[tuple[str, int]] = set()
        self._skin_prefetched_keys: set[tuple[str, int]] = set()
        self._pending_icon_verify_candidates: tuple[str, ...] = ()
        self._pending_icon_verify_committed_id: str = ""
        self._pending_icon_verify_slot_hint: int | None = None
        self._local_spent_credits_delta: float = 0.0
        self._pending_lobby_unseal: bool = False
        self._last_402_trigger_reason: str = ""
        self._last_picker_gate_skipped_hint: str = ""
        self._core_lifecycle = None
        self._hotkey_os_occupied: dict[str, str] = {}

        # EOG race protection, ChampSelect close suppression & DevLab per-match buffers
        self._pending_eog_since: float | None = None
        self._pending_eog_phase: str = ""
        self._lcu_no_live_polls: int = 0
        self._last_lcu_phase: str = ""
        self._active_game_id: int = 0
        self._active_queue_id: int = 0
        self._match_is_non_mayhem: bool = False
        self._match_game_mode: str = ""
        self._champ_closed_by_user_in_session: bool = False
        self._last_capture_backend: str = ""
        self._last_capture_black: bool = False
        self._picker_opened_mono: float = 0.0
        self._last_picker_game_rect: Any = None
        self._last_picker_reroll_y: int | None = None
        self._last_offer_rec_snapshot: dict[str, Any] | None = None
        self._last_offer_emitted_sig: tuple[Any, ...] | None = None
        self._pending_reroll_prev_offer: dict[str, Any] | None = None
        self._pending_reroll_slots: list[int] | None = None
        self._last_ocr_anomaly_mono: dict[int, float] = {}
        self._click_last_tick_mono: float = 0.0
        self._click_max_gap_ms_in_picker: float = 0.0
        self._picker_opened_since_level: bool = False
        self._last_live_level: int = 0
        self._last_smart_gate_skip_mono: float = 0.0
        self._last_smart_gate_skip_detail: str = ""
        self._last_advice_mono: float = 0.0
        self._last_manual_trigger_mono: dict[str, float] = {}
        self._manual_trigger_repeat_count: dict[str, int] = {}
        self._last_coach_req_meta: dict[str, dict[str, Any]] = {}
        self._aug_source_by_id: dict[str, dict[str, Any]] = {}
        self._active_picker_coach_req_id: str = ""
        self._active_picker_coach_arrived: bool = False
        self._active_picker_coach_aug_advice: str = ""
        self._last_idle_dual_pass_emit_mono: float = 0.0

        # UI suite (unified single-fire on_geometry_changed eliminates triple save_config() per resize)
        self.champ_overlay = ChampSelectOverlay(
            initial_size=self.cfg.champ_window_size,
            on_core_action=self._handle_core_action,
            on_goal_mode_changed=self._on_champ_goal_mode_changed,
            initial_goal_mode=self.cfg.champ_select_goal_mode,
            on_closed_by_user=self._on_champ_closed_by_user,
            on_geometry_changed=self._on_overlay_geometry_changed,
            on_subcard_action=lambda card, act, meta: self._on_overlay_subcard_action("champ_select", card, act, meta),
        )
        if self.cfg.champ_window_pos and len(self.cfg.champ_window_pos) == 2:
            self.champ_overlay.move(int(self.cfg.champ_window_pos[0]), int(self.cfg.champ_window_pos[1]))

        self.augment_overlay = AugmentBadgesOverlay()
        self.combo_overlay = AugmentComboOverlay(
            on_closed_by_user=self._on_combo_closed_by_user,
            on_geometry_changed=self._on_overlay_geometry_changed,
            on_subcard_action=lambda card, act, meta: self._on_overlay_subcard_action("combo_overlay", card, act, meta),
            on_toggle_screen_recording=self._on_toggle_screen_recording,
            initial_pos=self.cfg.combo_drawer_pos,
            initial_size=self.cfg.combo_drawer_size,
            initial_scale=self.cfg.combo_drawer_scale,
        )
        self.combo_overlay.set_locale_store(self.store)
        self.edge_drawer = EdgeBuildDrawer(
            on_select_augment=self._on_manual_add_augment,
            on_remove_augment=self._on_manual_remove_augment,
            on_replace_last_augment=self._on_replace_last_augment,
            on_resolve_transmute=lambda aid: self._on_resolve_transmute_augment(aid, source="manual"),
            on_dismiss_transmute=self._on_dismiss_transmute,
            on_call_coach=lambda: self.trigger_coach_request("manual_f6"),
            on_switch_dataset=self._on_switch_dataset,
            on_manual_hero=self._on_manual_hero,
            on_select_archetype=self._on_select_archetype,
            on_closed_by_user=self._on_drawer_closed_by_user,
            on_switch_tier=self._on_switch_tier,
            on_recharge_click=self.open_recharge_dialog,
            on_login_click=self.open_login_dialog,
            on_check_update=self._on_check_update_clicked,
            on_core_action=self._handle_core_action,
            on_opening_brief_seal_dismissed=self._on_opening_brief_seal_dismissed,
            on_geometry_changed=self._on_overlay_geometry_changed,
            on_subcard_action=lambda card, act, meta: self._on_overlay_subcard_action("edge_drawer", card, act, meta),
            on_manual_add_item=self._on_manual_add_item,
            on_manual_remove_item=self._on_manual_remove_item,
            on_plan_b_rescan=self._on_plan_b_rescan,
            on_toggle_screen_recording=self._on_toggle_screen_recording,
            on_request_restore_game_focus=self._restore_game_focus,
            initial_tier=self.cfg.coach_tier,
            initial_pos=self.cfg.drawer_pos,
            initial_size=self.cfg.drawer_size,
            initial_scale=self.cfg.drawer_scale,
            y_ratio=self.cfg.drawer_y_ratio,
        )
        self.lookup_window = LookupWindow(
            cfg=self.cfg,
            on_save_settings=self._on_save_settings,
            on_sync_now=self.start_async_sync,
            on_open_calibration=self.open_calibrator,
            on_test_coach=lambda: self.trigger_coach_request("manual_f6"),
            on_test_gemini=self._on_test_gemini_connection,
            on_toggle_drawer=self._toggle_drawer_from_lookup,
            on_toggle_combo=self._toggle_combo_from_lookup,
            on_switch_dataset=self._on_switch_dataset,
            on_login_click=self.open_login_dialog,
            on_logout_click=self._on_logout_clicked,
            on_recharge_click=self.open_recharge_dialog,
            on_switch_tier=self._on_switch_tier,
            on_check_update=self._on_check_update_clicked,
            on_open_recap=self._open_last_match_recap,
            on_core_action=self._handle_core_action,
            on_unseal_complete=self._on_lobby_unseal_complete,
            on_dismiss_newbie_banner=self._on_dismiss_newbie_banner,
            on_open_hotkeys=self.open_hotkey_settings_dialog,
            on_open_feedback=self.open_feedback_dialog,
            on_reset_overlay_sizes=self._reset_overlay_sizes,
            devlab_active=(self._devlab_active and not self._commercial),
            on_devlab_badge_clicked=(self._open_devlab_folder if self._devlab_active else None),
            on_search_settled=(self._on_lookup_search_settled if self._devlab_active else None),
            on_tab_changed=(self._on_lookup_tab_changed if self._devlab_active else None),
            on_settings_changed=(self._on_lookup_settings_changed if self._devlab_active else None),
        )
        self.calibrator = CalibrationOverlay(
            initial_title_roi=self.cfg.title_band_roi,
            on_calibrated=self._on_rois_calibrated,
            on_calibration_finished=(self._on_calibration_finished if self._devlab_active else None),
        )

        # Install per-widget UI visibility & scrollbar overflow observers ONLY in active DevLab mode
        if self._devlab_active and not self._commercial:
            self._install_devlab_ui_observers()

        # Tray & global hotkeys
        self._setup_tray()
        self.hotkeys = GlobalHotkeyManager()
        self.qt_app.installNativeEventFilter(self.hotkeys)
        self._register_hotkeys()

        # Instant boot from cached dataset (extract bundled seed first if needed)
        self._load_cached_on_startup()
        if self._vision_worker is not None:
            self._push_vision_context()
            self._vision_worker.start()
        if not self._commercial and self.bridge is not None and self.cfg.bridge_enabled:
            try:
                self.bridge.start()
            except Exception as exc:
                logger.warning("Bridge server failed on port %s: %s", self.cfg.bridge_port, exc)

        self._refresh_coach_online_ui()
        if self._commercial:
            if self.cloud_client is not None and self.cloud_client.is_logged_in:
                self._poll_cloud_wallet_async()
            else:
                self._on_cloud_cutoff("NOT_LOGGED_IN", "请先登录账号启用云端 AI 参谋", {})
            self._sync_cloud_pricing_async()

        # Start async incremental data sync
        self.start_async_sync()

        # Off-thread telemetry worker (prevents 127.0.0.1:2999 / LCU polling from EVER blocking Qt GUI thread!)
        self._stop_event = threading.Event()
        self._telemetry_thread = threading.Thread(
            target=self._telemetry_worker_loop, name="HexTelemetryWorker", daemon=True
        )
        self._telemetry_thread.start()

        # Lightweight 1s UI heartbeat check (dev bridge only) + 60s cloud wallet heartbeat (commercial)
        self._heartbeat_timer = QTimer()
        self._heartbeat_timer.setInterval(1000)
        if not self._commercial and self.bridge is not None:
            self._heartbeat_timer.timeout.connect(self.bridge.check_online_timeout)
            self._heartbeat_timer.start()

        if self._commercial:
            self._wallet_heartbeat_timer = QTimer()
            self._wallet_heartbeat_timer.setInterval(60_000)
            self._wallet_heartbeat_timer.timeout.connect(self._poll_cloud_wallet_async)
            self._wallet_heartbeat_timer.start()

            self._startup_update_timer = QTimer()
            self._startup_update_timer.setSingleShot(True)
            self._startup_update_timer.setInterval(3000)
            self._startup_update_timer.timeout.connect(self._check_updates_on_startup)
            self._startup_update_timer.start()

        if self._devlab_active and not self._commercial:
            self._devlab_badge_timer = QTimer()
            self._devlab_badge_timer.setInterval(2000)
            self._devlab_badge_timer.timeout.connect(self._refresh_devlab_badge_if_visible)
            self._devlab_badge_timer.start()
            self.lookup_window.update_devlab_badge(self._devlab.get_status_snapshot())

        self._vision_timer = QTimer()
        self._vision_timer.setInterval(400)
        self._vision_timer.timeout.connect(self._tick_vision)
        self._vision_timer.start()

        # High-frequency 30ms click sampler active ONLY while AUGMENT_PICKING (never misses 50ms clicks!)
        self._click_timer = QTimer()
        self._click_timer.setInterval(30)
        self._click_timer.timeout.connect(self._tick_click_sample)

        # Anti-burst AI coach debounce / collision deferral & delayed HUD icon verification timers
        self._picker_coach_debounce_timer = QTimer()
        self._picker_coach_debounce_timer.setSingleShot(True)
        self._picker_coach_debounce_timer.setInterval(self.PICKER_COACH_DEBOUNCE_MS)
        self._picker_coach_debounce_timer.timeout.connect(self._on_picker_coach_debounce_timeout)

        self._death_coach_defer_timer = QTimer()
        self._death_coach_defer_timer.setSingleShot(True)
        self._death_coach_defer_timer.setInterval(self.DEATH_COACH_DEFER_MS)
        self._death_coach_defer_timer.timeout.connect(self._on_death_coach_defer_timeout)

        self._augment_remove_coach_timer = QTimer()
        self._augment_remove_coach_timer.setSingleShot(True)
        self._augment_remove_coach_timer.setInterval(self.AUGMENT_REMOVE_DEFER_MS)
        self._augment_remove_coach_timer.timeout.connect(self._on_augment_remove_coach_timeout)

        self._game_start_coach_defer_timer = QTimer()
        self._game_start_coach_defer_timer.setSingleShot(True)
        self._game_start_coach_defer_timer.setInterval(self.GAME_START_SCOUT_WAIT_MS)
        self._game_start_coach_defer_timer.timeout.connect(self._on_game_start_coach_defer_timeout)

        self._icon_verify_timer = QTimer()
        self._icon_verify_timer.setSingleShot(True)
        self._icon_verify_timer.setInterval(self.ICON_VERIFY_DELAY_MS)
        self._icon_verify_timer.timeout.connect(self._on_delayed_icon_verify)

        self._sync_screen_recording_overlays(bool(getattr(self.cfg, "allow_screen_recording_overlays", False)))

        if show_lookup_on_start:
            self.lookup_window.show()
        self._refresh_non_overlay_hwnds_cache()

    # ------------------------------------------------------------------
    # Screen Recording Toggle, Overlay Foreground & Skin Splash Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_widget_hwnd(w: Any) -> int | None:
        if w is None:
            return None
        try:
            vis_fn = getattr(w, "isVisible", None)
            if callable(vis_fn) and not vis_fn():
                return None
            wid_fn = getattr(w, "internalWinId", None)
            if callable(wid_fn):
                wid = wid_fn()
            else:
                win_id_fn = getattr(w, "winId", None)
                wid = win_id_fn() if callable(win_id_fn) else None
            if not wid:
                return None
            val = int(wid)
            return val if val > 0 else None
        except Exception:
            return None

    def _non_overlay_hwnds(self) -> set[int]:
        hwnds: set[int] = set()
        for attr in ("lookup_window", "_active_recap_dialog", "_hotkey_dlg", "_feedback_dlg"):
            val = self._collect_widget_hwnd(getattr(self, attr, None))
            if val is not None:
                hwnds.add(val)
        try:
            modal = QApplication.activeModalWidget()
        except Exception:
            modal = None
        if modal is not None:
            val = self._collect_widget_hwnd(modal)
            if val is not None:
                hwnds.add(val)
        return hwnds

    def _refresh_non_overlay_hwnds_cache(self) -> tuple[int, ...]:
        cached = tuple(sorted(self._non_overlay_hwnds()))
        self._cached_non_overlay_hwnds = cached
        return cached

    def _is_game_or_overlay_foreground(self, game_hwnd: int) -> bool:
        excl = self._refresh_non_overlay_hwnds_cache()
        if is_hwnd_foreground(game_hwnd):
            return True
        return bool(is_game_or_overlay_foreground(game_hwnd, exclude_hwnds=excl))

    def _is_game_or_overlay_fg_worker(self, game_hwnd: int) -> bool:
        if is_hwnd_foreground(game_hwnd):
            return True
        excl = tuple(getattr(self, "_cached_non_overlay_hwnds", ()) or ())
        return bool(is_game_or_overlay_foreground(game_hwnd, exclude_hwnds=excl))

    def _sync_screen_recording_overlays(self, allowed: bool) -> None:
        val = bool(allowed)
        for ov_attr in ("champ_overlay", "augment_overlay", "combo_overlay", "edge_drawer"):
            ov = getattr(self, ov_attr, None)
            if ov is None:
                continue
            fn = getattr(ov, "set_screen_recording_allowed", None)
            if callable(fn):
                try:
                    fn(val)
                except Exception:
                    pass
        lw = getattr(self, "lookup_window", None)
        if lw is not None:
            sync_fn = getattr(lw, "sync_screen_recording_checkbox", None)
            if callable(sync_fn):
                try:
                    sync_fn(val)
                except Exception:
                    pass

    def _on_toggle_screen_recording(self, allowed: bool) -> None:
        val = bool(allowed)
        self.cfg.allow_screen_recording_overlays = val
        save_config(self.cfg)
        self._sync_screen_recording_overlays(val)

    def _restore_game_focus(self) -> None:
        try:
            game_rect = find_game_window()
        except Exception:
            game_rect = None
        if game_rect is not None and int(getattr(game_rect, "hwnd", 0) or 0) > 0:
            restore_foreground_window(int(game_rect.hwnd))

    def _push_recent_unrecorded_augments(self) -> None:
        drawer = getattr(self, "edge_drawer", None)
        sm = getattr(self, "sm", None)
        if drawer is None or sm is None:
            return
        setter = getattr(drawer, "set_recent_unrecorded_augments", None)
        pool_fn = getattr(sm, "recent_unrecorded_augments", None)
        if callable(setter) and callable(pool_fn):
            try:
                setter(pool_fn())
            except Exception:
                pass

    @staticmethod
    def _should_fetch_skin_variants(static: Any) -> bool:
        """Skip loading/center variant fetches when tests stub ensure_skin_splash on a live instance without variants."""
        inst_dict = getattr(static, "__dict__", None)
        if isinstance(inst_dict, dict) and "ensure_skin_splash" in inst_dict:
            return "ensure_skin_loading" in inst_dict or "ensure_skin_center" in inst_dict
        return True

    def _prefetch_my_skin_splash(self, hero_id: str, skin_id: int) -> None:
        hid = str(hero_id or "").strip()
        sid = int(skin_id or 0)
        if not hid or getattr(self._stop_event, "is_set", lambda: False)():
            return
        store = getattr(self, "store", None)
        static = getattr(store, "static", None) if store is not None else None
        if static is None:
            return
        fetching = getattr(self, "_skin_splash_fetching", None)
        if not isinstance(fetching, set):
            fetching = set()
            self._skin_splash_fetching = fetching
        prefetched = getattr(self, "_skin_prefetched_keys", None)
        if not isinstance(prefetched, set):
            prefetched = set()
            self._skin_prefetched_keys = prefetched
        key = (hid, sid)
        if key in prefetched or key in fetching:
            return
        allow_variants = self._should_fetch_skin_variants(static)
        has_splash = False
        resolve_fn = getattr(static, "resolve_cached_skin_splash", None)
        if callable(resolve_fn):
            try:
                cached_path, _ = resolve_fn(hid, sid)
                has_splash = bool(cached_path and Path(cached_path).is_file())
            except Exception:
                has_splash = False

        has_variants = not allow_variants
        if allow_variants:
            resolve_vars_fn = getattr(static, "resolve_cached_skin_variants", None)
            if callable(resolve_vars_fn):
                try:
                    c_load, c_cent = resolve_vars_fn(hid, sid)
                    has_variants = bool(c_load and Path(c_load).is_file()) and bool(c_cent and Path(c_cent).is_file())
                except Exception:
                    has_variants = False

        if has_splash and has_variants:
            prefetched.add(key)
            return

        ensure_fn = getattr(static, "ensure_skin_splash", None)
        ensure_load_fn = getattr(static, "ensure_skin_loading", None) if allow_variants else None
        ensure_cent_fn = getattr(static, "ensure_skin_center", None) if allow_variants else None
        if not callable(ensure_fn) and not callable(ensure_load_fn) and not callable(ensure_cent_fn):
            return
        fetching.add(key)

        def _worker() -> None:
            try:
                if not has_splash and callable(ensure_fn):
                    ensure_fn(hid, sid)
                if allow_variants:
                    if callable(ensure_load_fn):
                        try:
                            ensure_load_fn(hid, sid)
                        except Exception:
                            pass
                    if callable(ensure_cent_fn):
                        try:
                            ensure_cent_fn(hid, sid)
                        except Exception:
                            pass
                prefetched.add(key)
            except Exception:
                pass
            finally:
                fetching.discard(key)

        threading.Thread(target=_worker, name="HexSkinSplashPrefetch", daemon=True).start()

    # ------------------------------------------------------------------
    # DevLab Lazy Mounting, Telemetry Helpers & Tray Actions
    # ------------------------------------------------------------------

    def _init_devlab_recorder(
        self,
        *,
        devlab_flag: bool = False,
        devlab_roi_flag: bool = False,
        devlab_debug_flag: bool = False,
    ) -> None:
        if self._commercial or not is_devlab_allowed():
            self._devlab = _NullFlightRecorder()
            self._devlab_active = False
            self._action_tracker = None
            return
        active = bool(devlab_flag or _env_truthy("HEX_DEVLAB") or bool(self.cfg.devlab_enabled))
        if not active:
            self._devlab = _NullFlightRecorder()
            self._devlab_active = False
            self._action_tracker = None
            return
        roi_on = bool(devlab_roi_flag or _env_truthy("HEX_DEVLAB_ROI") or bool(self.cfg.devlab_roi_snapshots))
        dbg_on = bool(devlab_debug_flag or _env_truthy("HEX_DEVLAB_DEBUG") or bool(self.cfg.devlab_debug_log))
        try:
            rec_mod = importlib.import_module("hex_helper.devlab.recorder")
            recorder = rec_mod.DevLabFlightRecorder(
                enable_roi_snapshots=roi_on,
                enable_debug_log=dbg_on,
                max_matches=int(getattr(self.cfg, "devlab_max_matches", 120) or 120),
            )
            if not hasattr(recorder, "active"):
                recorder.active = True
            self._devlab = recorder
            self._devlab_active = True
            self._devlab_roi_enabled = roi_on
            self._devlab_debug_enabled = dbg_on
            self._action_tracker = MatchActionTracker(
                self._on_action_tracker_emit,
                is_complete_item_fn=lambda iid: bool(
                    getattr(self, "store", None) is not None and is_valid_plan_item(self.store, iid)
                ),
            )
            logger.info(
                "DevLab Flight Recorder mounted (dir=%s, roi=%s, debug_log=%s)",
                recorder.root_dir,
                roi_on,
                dbg_on,
            )
        except Exception as exc:
            logger.warning("DevLab Flight Recorder mount failed (falling back to Null): %s", exc)
            self._devlab = _NullFlightRecorder()
            self._devlab_active = False
            self._action_tracker = None

    def _install_devlab_ui_observers(self) -> None:
        if not self._devlab_active or self._commercial:
            return
        try:
            obs_mod = importlib.import_module("hex_helper.devlab.ui_observer")
            overlays = {
                "edge_drawer": self.edge_drawer,
                "combo_overlay": self.combo_overlay,
                "champ_select": self.champ_overlay,
                "augment_badges": self.augment_overlay,
            }
            scrollbars: dict[str, Any] = {}
            for w in (self.edge_drawer, self.combo_overlay, self.champ_overlay, self.lookup_window):
                fn = getattr(w, "get_tracked_scrollbars", None)
                if callable(fn):
                    try:
                        scrollbars.update(fn())
                    except Exception:
                        pass
            self._ui_observers = obs_mod.install_devlab_ui_observers(overlays, scrollbars, self._devlab)
        except Exception as exc:
            logger.debug("DevLab UI observer install skipped: %s", exc)
            self._ui_observers = None

    def _current_dataset_tag(self) -> str:
        store = getattr(self, "store", None)
        desc = getattr(store, "descriptor", None)
        if desc is None:
            return ""
        mode = getattr(store, "active_match_mode", "") or desc.mode or "normal"
        return f"{desc.patch}/{mode}"

    def _current_game_time_s(self) -> float | None:
        live = getattr(self, "_last_live", None)
        if live is not None:
            return float(getattr(live, "game_time", 0.0) or 0.0)
        return None

    def _emit_devlab(
        self,
        domain: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        game_time_s: float | None = None,
        priority: str = "normal",
    ) -> int:
        if not getattr(self, "_devlab_active", False):
            return 0
        rec = getattr(self, "_devlab", _NULL_FLIGHT_RECORDER)
        gt = self._current_game_time_s() if game_time_s is None else game_time_s
        try:
            return int(rec.emit(domain, event_type, payload or {}, game_time_s=gt, priority=priority) or 0)
        except Exception:
            return 0

    def _on_action_tracker_emit(
        self,
        domain: str,
        event_type: str,
        payload: dict[str, Any],
        game_time_s: float | None,
    ) -> None:
        if not getattr(self, "_devlab_active", False):
            return
        try:
            self._devlab.emit(domain, event_type, payload, game_time_s=game_time_s)
        except Exception:
            pass

    def _mark_overlay_open(self, overlay_name: str, source: str) -> None:
        obs = getattr(self, "_ui_observers", None)
        if obs is not None:
            try:
                obs.mark_open(overlay_name, source)
            except Exception:
                pass

    def _mark_overlay_close(self, overlay_name: str, source: str) -> None:
        obs = getattr(self, "_ui_observers", None)
        if obs is not None:
            try:
                obs.mark_close(overlay_name, source)
            except Exception:
                pass

    def _refresh_devlab_badge_if_visible(self) -> None:
        if not getattr(self, "_devlab_active", False) or self._commercial:
            return
        lw = getattr(self, "lookup_window", None)
        if lw is None or not lw.isVisible():
            return
        try:
            lw.update_devlab_badge(self._devlab.get_status_snapshot())
        except Exception:
            pass

    def _open_devlab_folder(self) -> None:
        if not getattr(self, "_devlab_active", False) or self._commercial:
            return
        root = getattr(self._devlab, "root_dir", None)
        if root is None:
            return
        try:
            Path(root).mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(root))  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("Failed opening DevLab directory: %s", exc)

    def _toggle_devlab_roi_snapshots(self, checked: bool) -> None:
        if not getattr(self, "_devlab_active", False) or self._commercial:
            return
        self._devlab_roi_enabled = bool(checked)
        self._devlab.set_roi_snapshots_enabled(bool(checked))
        self.cfg.devlab_roi_snapshots = bool(checked)
        save_config(self.cfg)
        self._emit_devlab(
            "ui",
            "ui_hotkey_and_tray_dispatch",
            {"source": "tray", "action": "devlab_toggle_roi_snapshots", "enabled": bool(checked)},
        )
        self._refresh_devlab_badge_if_visible()

    def _show_devlab_session_stats(self) -> None:
        if not getattr(self, "_devlab_active", False) or self._commercial:
            return
        st = self._devlab.get_status_snapshot()
        seq = st.get("active_match_seq")
        state_txt = f"录制中 #{int(seq):04d}" if seq is not None else "待机中"
        disk_mb = float(st.get("disk_bytes") or 0) / (1024.0 * 1024.0)
        msg = (
            f"{state_txt} · 事件 {st.get('event_count', 0)} 条 · "
            f"队列 {st.get('queue_depth', 0)} (丢弃 {st.get('dropped_events', 0)}) · "
            f"ROI {st.get('roi_count', 0)}/{st.get('roi_cap', 15)} · "
            f"占用 {disk_mb:.2f}MB ({st.get('match_count', 0)} 局)"
        )
        try:
            if self.tray is not None:
                self.tray.showMessage(
                    "HexHelper · DevLab 黑匣子统计",
                    msg,
                    QSystemTrayIcon.MessageIcon.Information,
                    5000,
                )
        except Exception:
            pass
        self._emit_devlab("ui", "ui_hotkey_and_tray_dispatch", {"source": "tray", "action": "devlab_show_stats"})

    def _generate_devlab_report_async(self, last_n: int = 20) -> None:
        if not getattr(self, "_devlab_active", False) or self._commercial:
            return
        if self._devlab_report_in_flight:
            return
        self._devlab_report_in_flight = True
        root_dir = getattr(self._devlab, "root_dir", None)
        self._emit_devlab("ui", "ui_hotkey_and_tray_dispatch", {"source": "tray", "action": "devlab_generate_report"})

        def _worker() -> None:
            try:
                self._devlab.flush(timeout_s=2.0)
                an_mod = importlib.import_module("hex_helper.devlab.analyzer")
                analyzer = an_mod.DevLabAnalyzer(root_dir=root_dir)
                rep = analyzer.generate_evolution_report(last_n=last_n)
                fname = Path(rep.report_path).name if rep.report_path else "latest_evolution_report.md"
                summary = (
                    f"已分析 {rep.total_matches} 局（海克斯 {rep.mayhem_matches} 局），"
                    f"检出 {len(rep.issues)} 项演进问题 -> reports/{fname}"
                )

                def _notify_ok() -> None:
                    self._devlab_report_in_flight = False
                    if self.tray is not None:
                        try:
                            self.tray.showMessage(
                                "HexHelper · DevLab 演进报告已生成",
                                summary,
                                QSystemTrayIcon.MessageIcon.Information,
                                6000,
                            )
                        except Exception:
                            pass

                if not self._stop_event.is_set():
                    self.ui_invoker.invoke.emit(_notify_ok)
            except Exception as exc:
                err_msg = str(exc)[:140]

                def _notify_err() -> None:
                    self._devlab_report_in_flight = False
                    if self.tray is not None:
                        try:
                            self.tray.showMessage(
                                "HexHelper · DevLab 演进报告失败",
                                f"生成失败：{err_msg}",
                                QSystemTrayIcon.MessageIcon.Warning,
                                5000,
                            )
                        except Exception:
                            pass

                if not self._stop_event.is_set():
                    self.ui_invoker.invoke.emit(_notify_err)

        threading.Thread(target=_worker, name="HexDevLabReport", daemon=True).start()

    def _on_overlay_geometry_changed(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        overlay = str(payload.get("overlay") or "").strip()
        action = str(payload.get("action") or "").strip()
        pos = payload.get("pos")
        size = payload.get("size")
        scale = payload.get("scale")

        if overlay == "edge_drawer":
            if action == "reset_edge":
                self.cfg.drawer_pos = None
            elif isinstance(pos, list) and len(pos) == 2:
                self.cfg.drawer_pos = [int(pos[0]), int(pos[1])]
            if action in ("resize", "reset_size"):
                self.cfg.drawer_size = [int(size[0]), int(size[1])] if (isinstance(size, list) and len(size) == 2) else None
                self.cfg.drawer_scale = float(scale) if scale is not None else None
        elif overlay == "combo_overlay":
            if action == "reset_edge":
                self.cfg.combo_drawer_pos = None
            elif isinstance(pos, list) and len(pos) == 2:
                self.cfg.combo_drawer_pos = [int(pos[0]), int(pos[1])]
            if action in ("resize", "reset_size"):
                self.cfg.combo_drawer_size = [int(size[0]), int(size[1])] if (isinstance(size, list) and len(size) == 2) else None
                self.cfg.combo_drawer_scale = float(scale) if scale is not None else None
        elif overlay == "champ_select":
            if isinstance(pos, list) and len(pos) == 2:
                self.cfg.champ_window_pos = [int(pos[0]), int(pos[1])]
            if action in ("resize", "reset_size"):
                self.cfg.champ_window_size = [int(size[0]), int(size[1])] if (isinstance(size, list) and len(size) == 2) else None

        save_config(self.cfg)

        if self._devlab_active:
            sr = payload.get("screen_rect")
            res_str = ""
            if isinstance(sr, (list, tuple)) and len(sr) == 4:
                res_str = f"{int(sr[2])}x{int(sr[3])}"
            snapped = str(payload.get("snapped_edge") or "")
            self._emit_devlab(
                "ui",
                "ui_overlay_layout_adjusted",
                {
                    "overlay": overlay,
                    "action": action,
                    "snapped_edge": snapped,
                    "detached_edge": snapped == "detached",
                    "screen_rect": list(sr) if isinstance(sr, (list, tuple)) else [],
                    "resolution": res_str,
                    "screen_index": int(payload.get("screen_index") or 0),
                    "screen_count": int(payload.get("screen_count") or 1),
                    "dpr": float(payload.get("dpr") or 1.0),
                    "norm_rect": payload.get("norm_rect"),
                    "prev_norm_rect": payload.get("prev_norm_rect"),
                    "width": int(payload.get("width") or 0),
                    "height": int(payload.get("height") or 0),
                    "scale": scale,
                    "expanded": bool(payload.get("expanded", True)),
                },
            )

    def _on_overlay_subcard_action(self, overlay: str, card: str, action: str, meta: dict[str, Any]) -> None:
        if not self._devlab_active:
            return
        self._emit_devlab(
            "ui",
            "ui_subcard_interact_or_dismiss",
            {
                "overlay": overlay,
                "subcard": str(card or ""),
                "action": str(action or ""),
                "meta": dict(meta) if isinstance(meta, dict) else {},
            },
        )

    def _on_champ_closed_by_user(self) -> None:
        self._champ_closed_by_user_in_session = True
        self._mark_overlay_close("champ_select", "user_close")

    def _on_lookup_search_settled(self, query_len: int, result_count: int, zero_hit_query: str) -> None:
        if not self._devlab_active:
            return
        self._emit_devlab(
            "ui",
            "ui_lookup_navigation_and_search",
            {
                "action": "search",
                "query_len": int(query_len),
                "result_count": int(result_count),
                "zero_hit": bool(result_count == 0 and query_len > 0),
                "query": str(zero_hit_query or "")[:24],
            },
        )

    def _on_lookup_tab_changed(self, tab_index: int, tab_name: str) -> None:
        if not self._devlab_active:
            return
        self._emit_devlab(
            "ui",
            "ui_lookup_navigation_and_search",
            {
                "action": "tab_switch",
                "tab_index": int(tab_index),
                "tab_name": str(tab_name or ""),
            },
        )

    def _on_lookup_settings_changed(self, changed: dict[str, Any]) -> None:
        if not self._devlab_active:
            return
        self._emit_devlab(
            "ui",
            "ui_lookup_navigation_and_search",
            {
                "action": "settings_changed",
                "changed": dict(changed) if isinstance(changed, dict) else {},
            },
        )

    def _on_calibration_finished(self, info: dict[str, Any]) -> None:
        if not self._devlab_active or not isinstance(info, dict):
            return
        res = info.get("resolution")
        res_str = f"{int(res[0])}x{int(res[1])}" if (isinstance(res, (list, tuple)) and len(res) == 2) else ""
        outcome = str(info.get("outcome") or "")
        self._emit_devlab(
            "ui",
            "ui_calibration_attempt",
            {
                "outcome": outcome,
                "result": outcome,
                "screen_index": int(info.get("screen_index") or 0),
                "resolution": res_str,
                "dpr": float(info.get("dpr") or 1.0),
                "custom_rect_norm": info.get("custom_rect_norm"),
            },
        )

    def _on_recap_action(self, action: str, ok: bool) -> None:
        if not self._devlab_active:
            return
        self._emit_devlab(
            "ui",
            "ui_match_recap_interaction",
            {"action": str(action or ""), "ok": bool(ok)},
        )

    def _record_manual_trigger(self, trigger_name: str) -> None:
        if not self._devlab_active:
            return
        now = time.monotonic()
        last_map = getattr(self, "_last_manual_trigger_mono", None)
        rep_map = getattr(self, "_manual_trigger_repeat_count", None)
        if last_map is None or rep_map is None:
            return
        prev_ts = last_map.get(trigger_name, 0.0)
        if prev_ts > 0.0 and (now - prev_ts) <= 0.5:
            rep_map[trigger_name] = rep_map.get(trigger_name, 1) + 1
            last_map[trigger_name] = now
            return
        repeat_count = rep_map.get(trigger_name, 1)
        rep_map[trigger_name] = 1
        last_map[trigger_name] = now

        last_adv_ts = float(getattr(self, "_last_advice_mono", 0.0) or 0.0)
        s_since_adv = round(now - last_adv_ts, 2) if last_adv_ts > 0.0 else None
        advice_stale = (self._latest_advice is None) or (s_since_adv is not None and s_since_adv > 45.0)
        last_skip_ts = float(getattr(self, "_last_smart_gate_skip_mono", 0.0) or 0.0)
        ms_since_skip = round((now - last_skip_ts) * 1000.0, 1) if last_skip_ts > 0.0 else None
        prev_gate_skipped = bool(ms_since_skip is not None and 0.0 <= ms_since_skip <= 10_000.0)

        self._emit_devlab(
            "ui",
            "ui_manual_coach_or_scan_trigger",
            {
                "trigger": trigger_name,
                "repeat_count": repeat_count,
                "advice_stale": advice_stale,
                "s_since_last_advice": s_since_adv,
                "prev_gate_skipped": prev_gate_skipped,
                "ms_since_gate_skip": ms_since_skip if prev_gate_skipped else None,
            },
        )

    def _reload_icon_matcher_templates(self) -> int:
        matcher = getattr(self, "icon_matcher", None)
        if matcher is None:
            return 0
        fn = getattr(matcher, "reload_cached_icons", None)
        if not callable(fn):
            return int(getattr(matcher, "template_count", 0) or 0)
        try:
            cnt = int(fn())
            logger.debug("AugmentIconMatcher reloaded %d cached icon templates", cnt)
            return cnt
        except Exception as exc:
            logger.debug("AugmentIconMatcher reload skipped: %s", exc)
            return 0

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(load_app_icon(), self.qt_app)
        tooltip = f"HexHelper v{APP_VERSION} · 海克斯大乱斗小助手"
        if self._devlab_active and not self._commercial:
            tooltip += " · LAB REC"
        self.tray.setToolTip(tooltip)
        menu = QMenu()
        self._tray_menu = menu
        self._tray_devlab_menu: QMenu | None = None

        def _tray_wrap(action_name: str, fn: Callable[[], None]) -> Callable[[], None]:
            def _invoke() -> None:
                if self._devlab_active:
                    self._emit_devlab("ui", "ui_hotkey_and_tray_dispatch", {"source": "tray", "action": action_name})
                fn()
            return _invoke

        self._tray_act_lookup = QAction("打开速查与设置", menu)
        self._tray_act_lookup.triggered.connect(_tray_wrap("show_lookup", self._show_lookup))
        self._tray_act_drawer = QAction("显示/切换边缘出装条 (F3)", menu)
        self._tray_act_drawer.triggered.connect(_tray_wrap("toggle_drawer", self._toggle_drawer_from_tray))
        self._tray_act_combo = QAction("显示/切换海克斯联动浮窗 (F7)", menu)
        self._tray_act_combo.triggered.connect(_tray_wrap("toggle_combo", self._toggle_combo_from_tray))
        self._tray_act_sidebars = QAction("左右侧边栏同步收起/展开 (F9)", menu)
        self._tray_act_sidebars.triggered.connect(_tray_wrap("toggle_sidebars", self._toggle_sidebars_hotkey))
        self._tray_act_coach = QAction("立即呼叫 AI 参谋 (F6)", menu)
        self._tray_act_coach.triggered.connect(_tray_wrap("call_coach_f6", lambda: self.trigger_coach_request("manual_f6")))
        self._tray_act_cal = QAction("校准三选一区域 (F8)", menu)
        self._tray_act_cal.triggered.connect(_tray_wrap("calibrate_roi", self.open_calibrator))
        self._tray_act_hotkeys = QAction("自定义全局快捷键...", menu)
        self._tray_act_hotkeys.triggered.connect(_tray_wrap("open_hotkeys", self.open_hotkey_settings_dialog))
        self._tray_act_feedback = QAction("问题反馈与日志上报...", menu)
        self._tray_act_feedback.triggered.connect(_tray_wrap("open_feedback", self.open_feedback_dialog))
        act_sync = QAction("检查 26.23 数据更新", menu)
        act_sync.triggered.connect(_tray_wrap("sync_dataset", self.start_async_sync))
        act_quit = QAction("退出 HexHelper", menu)
        act_quit.triggered.connect(_tray_wrap("quit", self.shutdown))
        for a in (
            self._tray_act_lookup,
            self._tray_act_drawer,
            self._tray_act_combo,
            self._tray_act_sidebars,
            self._tray_act_coach,
            self._tray_act_cal,
            self._tray_act_hotkeys,
            self._tray_act_feedback,
            act_sync,
        ):
            menu.addAction(a)
        if self._devlab_active and not self._commercial:
            menu.addSeparator()
            dev_menu = menu.addMenu("◆ DevLab 黑匣子")
            self._tray_devlab_menu = dev_menu
            act_dev_open = QAction("打开录制目录", dev_menu)
            act_dev_open.triggered.connect(self._open_devlab_folder)
            act_dev_report = QAction("一键生成演进报告 (最近20局)", dev_menu)
            act_dev_report.triggered.connect(lambda: self._generate_devlab_report_async(last_n=20))
            act_dev_roi = QAction("异常 ROI 切片开关", dev_menu)
            act_dev_roi.setCheckable(True)
            act_dev_roi.setChecked(bool(getattr(self._devlab, "enable_roi_snapshots", False)))
            act_dev_roi.toggled.connect(self._toggle_devlab_roi_snapshots)
            self._tray_act_devlab_roi = act_dev_roi
            act_dev_stats = QAction("查看本局黑匣子统计", dev_menu)
            act_dev_stats.triggered.connect(self._show_devlab_session_stats)
            for da in (act_dev_open, act_dev_report, act_dev_roi, act_dev_stats):
                dev_menu.addAction(da)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_lookup() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        self.tray.show()

    def _register_hotkeys(self) -> None:
        self._reload_hotkeys()
        failed_map = getattr(self.hotkeys, "last_failed_hotkeys", None)
        if (
            isinstance(failed_map, dict)
            and len(failed_map) >= 3
            and not getattr(self, "_hotkey_conflict_toast_shown", False)
        ):
            self._hotkey_conflict_toast_shown = True
            keys_preview = "/".join(list(failed_map.keys())[:4])
            err_codes = sorted({int(v) for v in failed_map.values() if v})
            err_tag = f" (Win32 err={','.join(str(c) for c in err_codes)})" if err_codes else ""
            msg = (
                f"检测到 {len(failed_map)} 个默认热键（{keys_preview} 等）被其他程序占用{err_tag}，"
                "点击此通知或在「设置 -> 自定义全局快捷键」重新绑定。"
            )
            logger.warning("Global hotkey batch conflict at startup: %s", failed_map)
            tray = getattr(self, "tray", None)
            if tray is not None:
                try:
                    if not getattr(self, "_hotkey_toast_click_bound", False):
                        self._hotkey_toast_click_bound = True
                        tray.messageClicked.connect(self._on_tray_message_clicked_for_hotkeys)
                    self._hotkey_toast_pending_click = True
                    tray.showMessage(
                        "HexHelper · 全局热键冲突提示",
                        msg,
                        QSystemTrayIcon.MessageIcon.Warning,
                        7000,
                    )
                except Exception as exc:
                    logger.debug("Hotkey conflict tray toast skipped: %s", exc)

    def _on_tray_message_clicked_for_hotkeys(self) -> None:
        if getattr(self, "_hotkey_toast_pending_click", False):
            self._hotkey_toast_pending_click = False
            self.open_hotkey_settings_dialog()

    def _reload_hotkeys(self) -> dict[str, str]:
        hk = self.cfg.hotkeys
        self.hotkeys.unregister_all()

        def _force_scan_cb() -> None:
            self._record_manual_trigger("force_rescan_f2")
            self._tick_vision(force=True)

        bindings: tuple[tuple[str, str, Callable[[], None]], ...] = (
            ("force_scan", hk.force_scan, _force_scan_cb),
            ("toggle_drawer", hk.toggle_drawer, self._toggle_drawer_hotkey),
            ("toggle_all", hk.toggle_all, self._toggle_all_overlays),
            ("call_coach", hk.call_coach, lambda: self.trigger_coach_request("manual_f6")),
            ("toggle_combo", hk.toggle_combo, self._toggle_combo_hotkey),
            ("calibrate_roi", hk.calibrate_roi, self.open_calibrator),
            ("toggle_sidebars", hk.toggle_sidebars, self._toggle_sidebars_hotkey),
            ("toggle_lookup", hk.toggle_lookup, self._toggle_lookup_hotkey),
            ("open_recap", hk.open_recap, self._open_recap_hotkey),
        )
        is_real_mgr = isinstance(self.hotkeys, GlobalHotkeyManager)
        occupied: dict[str, str] = {}
        for field_name, raw_key, cb in bindings:
            canonical = normalize_hotkey(raw_key)
            if not canonical:
                continue

            def _make_hk_cb(fn_name: str, key_label: str, target_cb: Callable[[], None]) -> Callable[[], None]:
                def _wrapped() -> None:
                    if self._devlab_active:
                        act_tag = fn_name
                        if fn_name == "toggle_all" and not self._overlays_hidden_by_user:
                            act_tag = "f4_hide_all"
                        elif fn_name == "force_scan":
                            act_tag = "f2_force_rescan"
                        self._emit_devlab(
                            "ui",
                            "ui_hotkey_and_tray_dispatch",
                            {"source": "hotkey", "action": act_tag, "hotkey": key_label},
                        )
                    target_cb()
                return _wrapped

            ok = self.hotkeys.register(canonical, _make_hk_cb(field_name, canonical, cb))
            if is_real_mgr and not ok:
                occupied[field_name] = f"「{canonical}」已被其他程序占用"
        self._hotkey_os_occupied = occupied
        if occupied and self._devlab_active:
            self._emit_devlab(
                "ui",
                "ui_hotkey_and_tray_dispatch",
                {"source": "system", "action": "os_conflict", "occupied_fields": sorted(occupied.keys())},
            )
        self._sync_hotkey_ui_labels()
        return occupied

    def _sync_hotkey_ui_labels(self) -> None:
        hk = self.cfg.hotkeys

        def _fmt(base: str, raw_key: str) -> str:
            k = normalize_hotkey(raw_key)
            return f"{base} ({k})" if k else base

        if hasattr(self, "_tray_act_lookup"):
            self._tray_act_lookup.setText(_fmt("打开速查与设置", hk.toggle_lookup))
            self._tray_act_drawer.setText(_fmt("显示/切换边缘出装条", hk.toggle_drawer))
            self._tray_act_combo.setText(_fmt("显示/切换海克斯联动浮窗", hk.toggle_combo))
            self._tray_act_sidebars.setText(_fmt("左右侧边栏同步收起/展开", hk.toggle_sidebars))
            self._tray_act_coach.setText(_fmt("立即呼叫 AI 参谋", hk.call_coach))
            self._tray_act_cal.setText(_fmt("校准三选一区域", hk.calibrate_roi))
        self.lookup_window.update_hotkeys_display(hk, occupied=self._hotkey_os_occupied)
        self.edge_drawer.update_hotkey_labels(hk)
        self.combo_overlay.update_hotkey_labels(hk)

    def _set_hotkeys_suspended_for_capture(self, suspended: bool) -> None:
        set_susp = getattr(self.hotkeys, "set_suspended", None)
        if suspended:
            if callable(set_susp):
                set_susp(True)
            self.hotkeys.unregister_all()
        else:
            if callable(set_susp):
                set_susp(False)
            self._reload_hotkeys()

    def open_hotkey_settings_dialog(self) -> None:
        dlg = HotkeySettingsDialog(
            self.cfg.hotkeys,
            os_occupied=self._hotkey_os_occupied,
            probe_available=getattr(self.hotkeys, "probe_available", None),
            on_suspend_hotkeys=self._set_hotkeys_suspended_for_capture,
            parent=self._modal_dialog_parent(),
        )
        if dlg.exec() == int(QDialog.DialogCode.Accepted) and dlg.result_hotkeys is not None:
            self.cfg.hotkeys = dlg.result_hotkeys
            save_config(self.cfg)
            self._reload_hotkeys()
            self._refresh_diagnostics()

    def _build_feedback_diag_snapshot(self) -> dict[str, Any]:
        ring_stats = self.log_ring.summary_stats()
        with self._get_lcu_lock():
            lcu_creds = self.lcu.creds
        return {
            "state": self.sm.state.value,
            "edition": "commercial" if self._commercial else "dev",
            "dataset_patch": self.store.descriptor.patch if self.store.descriptor else "",
            "dataset_mode": self.store.active_match_mode if self.store.descriptor else "",
            "lcu_connected": bool(lcu_creds is not None),
            "lcu_source": str(lcu_creds.source) if lcu_creds else "",
            "live_active": bool(self._last_live is not None),
            "capture_backend": str(getattr(self, "_vision_backend", "") or self.capturer.backend),
            "black_frames": int(
                getattr(self, "_vision_consec_black", 0) or self.capturer.consecutive_black_frames
            ),
            "ocr_ready": bool(
                self.ocr_engine is not None
                or getattr(self, "_vision_ocr_ready", False)
                or getattr(getattr(self, "_vision_worker", None), "_ocr_engine", None) is not None
            ),
            "coach_mode": str(self.cfg.coach_mode),
            "coach_tier": str(self.cfg.coach_tier),
            "logged_in": bool(self.cloud_client is not None and self.cloud_client.is_logged_in),
            "log_ring_matches": int(ring_stats.get("match_count", 0)),
            "log_ring_max": int(ring_stats.get("max_matches", 20)),
            "log_ring_errors": int(ring_stats.get("total_errors", 0)),
            "log_ring_warns": int(ring_stats.get("total_warns", 0)),
        }

    def open_feedback_dialog(self) -> None:
        username = (
            self.cloud_client.current_username
            if (self.cloud_client is not None and self.cloud_client.is_logged_in)
            else ""
        )
        diag = self._build_feedback_diag_snapshot()

        def _submit(payload: dict[str, Any]) -> dict[str, Any]:
            if self.cloud_client is not None:
                return self.cloud_client.submit_feedback(**payload)
            temp_client = CloudGatewayClient(base_url=self.cfg.resolved_cloud_api_base())
            try:
                return temp_client.submit_feedback(**payload)
            finally:
                temp_client.close()

        dlg = FeedbackDialog(
            log_ring=self.log_ring,
            submit_fn=_submit,
            diag_snapshot=diag,
            dataset_patch=self.store.descriptor.patch if self.store.descriptor else "",
            dataset_mode=self.store.active_match_mode if self.store.descriptor else "",
            username=username,
            parent=self._modal_dialog_parent(),
        )
        dlg.exec()

    def _toggle_sidebars_hotkey(self) -> None:
        right_expanded = self.edge_drawer.isVisible() and self.edge_drawer.is_expanded
        left_expanded = self.combo_overlay.isVisible() and self.combo_overlay.is_expanded
        if right_expanded or left_expanded:
            if right_expanded:
                self.edge_drawer.set_expanded(False)
            if left_expanded:
                self.combo_overlay.set_expanded(False)
            return
        self._overlays_hidden_by_user = False
        self.edge_drawer.set_expanded(True)
        if not self.edge_drawer.isVisible():
            self._mark_overlay_open("edge_drawer", "hotkey_show")
            self.edge_drawer.show_overlay()
        if self.cfg.combo_overlay_enabled and not self._is_full_locked():
            self._combo_hidden_by_user = False
            self._refresh_combo_plan_view()
            self.combo_overlay.set_expanded(True)
            if not self.combo_overlay.isVisible():
                self._mark_overlay_open("combo_overlay", "hotkey_show")
                self.combo_overlay.show_overlay()

    def _toggle_lookup_hotkey(self) -> None:
        if self.lookup_window.isVisible() and not self.lookup_window.isMinimized():
            self.lookup_window.hide()
            self._refresh_non_overlay_hwnds_cache()
        else:
            self._show_lookup()

    def _open_recap_hotkey(self) -> None:
        if self._last_recap_data is not None:
            self._open_last_match_recap()
        else:
            self._show_lookup()

    def _show_lookup(self) -> None:
        self._refresh_diagnostics()
        self.lookup_window.show()
        self.lookup_window.raise_()
        self.lookup_window.activateWindow()
        self._refresh_non_overlay_hwnds_cache()
        self._refresh_devlab_badge_if_visible()

    def _toggle_drawer_from_tray(self) -> None:
        self._overlays_hidden_by_user = False
        if self.edge_drawer.isVisible():
            self._mark_overlay_close("edge_drawer", "tray_hide")
            self.edge_drawer.hide()
        else:
            self._mark_overlay_open("edge_drawer", "tray_show")
            self.edge_drawer.set_expanded(True)
            self.edge_drawer.show_overlay()

    def _toggle_drawer_from_lookup(self) -> None:
        if self._devlab_active:
            self._emit_devlab(
                "ui",
                "ui_hotkey_and_tray_dispatch",
                {"source": "lookup_ui", "action": "toggle_drawer"},
            )
        self._overlays_hidden_by_user = False
        if self.edge_drawer.isVisible():
            self._mark_overlay_close("edge_drawer", "lookup_btn_hide")
            self.edge_drawer.hide()
        else:
            self._mark_overlay_open("edge_drawer", "lookup_btn")
            self.edge_drawer.set_expanded(True)
            self.edge_drawer.show_overlay()

    def _toggle_combo_from_lookup(self) -> None:
        if self._devlab_active:
            self._emit_devlab(
                "ui",
                "ui_hotkey_and_tray_dispatch",
                {"source": "lookup_ui", "action": "toggle_combo"},
            )
        if self._is_full_locked():
            self.combo_overlay.hide()
            return
        self._combo_hidden_by_user = False
        if self.combo_overlay.isVisible():
            self._mark_overlay_close("combo_overlay", "lookup_btn_hide")
            self.combo_overlay.hide()
            self._combo_hidden_by_user = True
        else:
            self._refresh_combo_plan_view()
            self._mark_overlay_open("combo_overlay", "lookup_btn")
            self.combo_overlay.set_expanded(True)
            self.combo_overlay.show_overlay()

    def _toggle_drawer_hotkey(self) -> None:
        self._overlays_hidden_by_user = False
        if not self.edge_drawer.isVisible():
            self._mark_overlay_open("edge_drawer", "hotkey_show")
            self.edge_drawer.set_expanded(True)
            self.edge_drawer.show_overlay()
        else:
            self.edge_drawer.toggle_expanded()

    def _on_drawer_pos_changed(self, pos: list[int]) -> None:
        self.cfg.drawer_pos = pos
        save_config(self.cfg)

    def _on_drawer_size_changed(self, size: list[int] | None) -> None:
        self.cfg.drawer_size = size
        if size is None:
            self.cfg.drawer_scale = None
        save_config(self.cfg)

    def _on_drawer_scale_changed(self, scale: float | None) -> None:
        self.cfg.drawer_scale = scale
        save_config(self.cfg)

    def _on_drawer_closed_by_user(self) -> None:
        self._overlays_hidden_by_user = True
        self._mark_overlay_close("edge_drawer", "user_close")

    def _toggle_combo_from_tray(self) -> None:
        if self._is_full_locked():
            self.combo_overlay.hide()
            return
        self._combo_hidden_by_user = False
        if self.combo_overlay.isVisible():
            self._mark_overlay_close("combo_overlay", "tray_hide")
            self.combo_overlay.hide()
            self._combo_hidden_by_user = True
        else:
            self._refresh_combo_plan_view()
            self._mark_overlay_open("combo_overlay", "tray_show")
            self.combo_overlay.set_expanded(True)
            self.combo_overlay.show_overlay()

    def _toggle_combo_hotkey(self) -> None:
        if self._is_full_locked():
            self.combo_overlay.hide()
            return
        self._combo_hidden_by_user = False
        sm = getattr(self, "sm", None)
        verdict = getattr(self, "_last_combo_verdict", None)
        in_picker = sm is not None and sm.state == GameState.AUGMENT_PICKING and verdict is not None
        if not self.combo_overlay.isVisible():
            self._refresh_combo_plan_view()
            self._mark_overlay_open("combo_overlay", "hotkey_show")
            if in_picker:
                # Enter picker mode BEFORE expanding so pre-picker collapsed preference is preserved cleanly
                self.combo_overlay.enter_picker_mode(verdict)
            else:
                self.combo_overlay.set_expanded(True)
            self.combo_overlay.show_overlay()
        elif not self.combo_overlay.is_expanded:
            if in_picker:
                self.combo_overlay.enter_picker_mode(verdict)
            else:
                self.combo_overlay.set_expanded(True)
        else:
            self.combo_overlay.set_expanded(False)

    def _on_combo_pos_changed(self, pos: list[int]) -> None:
        self.cfg.combo_drawer_pos = pos
        save_config(self.cfg)

    def _on_combo_size_changed(self, size: list[int] | None) -> None:
        self.cfg.combo_drawer_size = size
        if size is None:
            self.cfg.combo_drawer_scale = None
        save_config(self.cfg)

    def _on_combo_scale_changed(self, scale: float | None) -> None:
        self.cfg.combo_drawer_scale = scale
        save_config(self.cfg)

    def _reset_overlay_sizes(self) -> None:
        self.edge_drawer.reset_default_size()
        self.combo_overlay.reset_default_size()
        self.champ_overlay.reset_default_size()
        self.cfg.drawer_size = None
        self.cfg.drawer_scale = None
        self.cfg.combo_drawer_size = None
        self.cfg.combo_drawer_scale = None
        self.cfg.champ_window_size = None
        save_config(self.cfg)

    def _on_combo_closed_by_user(self) -> None:
        self._combo_hidden_by_user = True
        self._mark_overlay_close("combo_overlay", "user_close")

    def _toggle_all_overlays(self) -> None:
        self._overlays_hidden_by_user = not self._overlays_hidden_by_user
        if self._overlays_hidden_by_user:
            self._mark_overlay_close("champ_select", "hotkey_hide")
            self._mark_overlay_close("augment_badges", "hotkey_hide")
            self._mark_overlay_close("edge_drawer", "hotkey_hide")
            self._mark_overlay_close("combo_overlay", "hotkey_hide")
            self.champ_overlay.hide()
            self.augment_overlay.hide()
            self.edge_drawer.hide()
            self.combo_overlay.hide()
        else:
            self._champ_closed_by_user_in_session = False
            if self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
                self._mark_overlay_open("edge_drawer", "hotkey_show")
                self.edge_drawer.show_overlay()
                if self.cfg.combo_overlay_enabled and not self._combo_hidden_by_user and not self._is_full_locked():
                    self._mark_overlay_open("combo_overlay", "hotkey_show")
                    self.combo_overlay.show_overlay()
            elif self.sm.state == GameState.CHAMP_SELECT and self._last_cs is not None and not self._is_full_locked():
                self._mark_overlay_open("champ_select", "hotkey_show")
                self.champ_overlay.show_overlay()

    def _preload_first_prismatic_from_cache(self, desc: DatasetDescriptor | None, ddir: Path | None) -> bool:
        if not self.cfg.auto_first_prismatic_mode or self.store.has_first_prismatic:
            return self.store.has_first_prismatic
        candidates: list[Path] = []
        if ddir is not None:
            candidates.append(Path(ddir).parent / "first-prismatic")
        if desc is not None and desc.patch:
            candidates.append(self.syncer.datasets_dir / desc.patch / "first-prismatic")
        if self.syncer.datasets_dir.exists():
            for patch_dir in sorted(self.syncer.datasets_dir.iterdir(), reverse=True):
                if patch_dir.is_dir():
                    candidates.append(patch_dir / "first-prismatic")
        seen: set[Path] = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            if (cand / "heroes.json").is_file():
                if self.store.preload_alternate_mode(cand, "first-prismatic", band=self.cfg.dataset_band):
                    logger.info("Preloaded sibling first-prismatic dataset from %s", cand)
                    return True
        return False

    def _load_cached_on_startup(self) -> None:
        cached = self.syncer.find_any_cached_dataset()
        if cached is None:
            extract_bundled_seed_cache(self.data_dir)
            cached = self.syncer.find_any_cached_dataset()
        self._reload_icon_matcher_templates()
        if cached is not None:
            desc, ddir = cached
            self.store.load_from_dir(desc, ddir, band=self.cfg.dataset_band)
            self._preload_first_prismatic_from_cache(desc, ddir)
            ocr_augs = self.store.build_ocr_augments(self.cfg.client_locale)
            self.matcher.update_catalog(ocr_augs)
            if self.transmute_watcher is not None:
                self.transmute_watcher.update_catalog(ocr_augs)
            if getattr(self, "_vision_worker", None) is not None:
                self._vision_worker.enqueue_command("update_catalog", {"augments": ocr_augs})
                self._vision_worker.enqueue_command("reload_icons")
            self.lookup_window.bind_store(self.store)
            self.edge_drawer.populate_catalogs(self.store, [desc])
            self._preheat_augment_icon_bank_async()

    def _preheat_augment_icon_bank_async(self) -> None:
        """Background non-blocking preheat of CDragon augment PNGs so Plan B HUD visual sync works."""
        if self._commercial or self._is_mocked_vision or self.icon_matcher is None or self._aug_preheat_started:
            return
        if self.icon_matcher.template_count >= 120:
            return
        self._aug_preheat_started = True
        aug_ids = [a.augment_id for a in self.store.all_augments()]

        def _worker() -> None:
            try:
                if self.icon_matcher is None:
                    return
                cnt = self.icon_matcher.preheat_templates(aug_ids, max_workers=6)
                logger.info("Preheated AugmentIconMatcher template bank: %d icons ready", cnt)
                if getattr(self, "_vision_worker", None) is not None:
                    self._vision_worker.enqueue_command("reload_icons")
            except Exception as exc:
                logger.debug("AugmentIconMatcher preheat skipped: %s", exc)

        threading.Thread(target=_worker, name="HexAugIconPreheat", daemon=True).start()

    def start_async_sync(self) -> None:
        def worker() -> None:
            try:
                datasets = self.syncer.fetch_index()
                desc, ddir = self.syncer.sync_dataset(
                    patch=self.cfg.dataset_patch,
                    mode=self.cfg.dataset_mode,
                    band=self.cfg.dataset_band,
                )
                self.store.load_from_dir(desc, ddir, band=self.cfg.dataset_band)
                self.static_assets.sync_all(dataset_item_meta=self.store.get_dataset_item_meta())
                self.store.load_from_dir(desc, ddir, band=self.cfg.dataset_band)
                if self.cfg.auto_first_prismatic_mode and desc.mode == "normal":
                    if not self._preload_first_prismatic_from_cache(desc, ddir):
                        fp_desc = next(
                            (ds for ds in datasets if ds.patch == desc.patch and ds.mode == "first-prismatic"),
                            next((ds for ds in datasets if ds.mode == "first-prismatic"), None),
                        )
                        if fp_desc is not None:
                            try:
                                _, fp_dir = self.syncer.sync_dataset(
                                    patch=fp_desc.patch,
                                    mode="first-prismatic",
                                    band=self.cfg.dataset_band,
                                )
                                self.store.preload_alternate_mode(fp_dir, "first-prismatic", band=self.cfg.dataset_band)
                            except Exception as fp_exc:
                                logger.debug("Optional first-prismatic sibling preload skipped: %s", fp_exc)
                self.datasets = datasets

                def on_ui() -> None:
                    self._reload_icon_matcher_templates()
                    if self.item_icon_matcher is not None:
                        self.item_icon_matcher.reload_cached_icons()
                    all_augs = self.store.build_ocr_augments(self.cfg.client_locale)
                    self.matcher.update_catalog(all_augs)
                    if self.transmute_watcher is not None:
                        self.transmute_watcher.update_catalog(all_augs)
                    if getattr(self, "_vision_worker", None) is not None:
                        self._vision_worker.enqueue_command("update_catalog", {"augments": all_augs})
                        self._vision_worker.enqueue_command("reload_icons")
                    self.lookup_window.set_datasets(datasets)
                    self.lookup_window.bind_store(self.store)
                    self.edge_drawer.populate_catalogs(self.store, datasets)
                    self._preheat_augment_icon_bank_async()
                    self._refresh_drawer_view()
                    self._refresh_diagnostics()

                self.ui_invoker.invoke.emit(on_ui)
            except Exception as exc:
                logger.warning("Background data sync failed (staying on local cache): %s", exc)
                self.ui_invoker.invoke.emit(self._refresh_diagnostics)

        threading.Thread(target=worker, name="HexDataSync", daemon=True).start()

    def _on_switch_dataset(self, patch: str, mode: str, band: str) -> None:
        self.cfg.dataset_patch = patch
        self.cfg.dataset_mode = mode
        self.cfg.dataset_band = band
        save_config(self.cfg)
        self.start_async_sync()

    def _on_save_settings(self, cfg: AppConfig) -> None:
        prev_band_mode = getattr(self, "_applied_data_band_mode", "all")
        prev_locale = normalize_client_locale(getattr(self, "_applied_client_locale", "zh_cn"))
        if self._commercial:
            cfg.proxy_url = ""
            cfg.bridge_enabled = False
            cfg.devlab_enabled = False
            cfg.devlab_roi_snapshots = False
            cfg.devlab_debug_log = False
            if cfg.coach_mode != "off":
                cfg.coach_mode = "cloud"
        raw_band = str(getattr(cfg, "data_band_mode", "all") or "all").strip().lower()
        cfg.data_band_mode = raw_band if raw_band in DATA_BAND_MODES else "all"
        band_mode_changed = cfg.data_band_mode != prev_band_mode
        cfg.client_locale = normalize_client_locale(getattr(cfg, "client_locale", "zh_cn"))
        locale_changed = cfg.client_locale != prev_locale
        self.cfg = cfg
        self._applied_data_band_mode = cfg.data_band_mode
        self._applied_client_locale = cfg.client_locale
        save_config(cfg)
        self.syncer.proxy_url = "" if self._commercial else cfg.proxy_url
        self.static_assets.proxy_url = "" if self._commercial else cfg.proxy_url
        with self._get_lcu_lock():
            self.lcu.preferred_lockfile = cfg.lockfile_path
            self.lcu.force_all_modes = cfg.force_all_modes
            self.lcu.creds = None
            self.lcu.close()
        if cfg.auto_first_prismatic_mode:
            self._preload_first_prismatic_from_cache(self.store.descriptor, self.store.dataset_dir)
        elif self._auto_switched_first_prismatic:
            self._restore_configured_dataset_mode()
        if locale_changed:
            self.store.set_client_locale(cfg.client_locale)
            ocr_augs = self.store.build_ocr_augments(cfg.client_locale)
            if self._is_mocked_vision:
                req_lang = desired_ocr_language_tag(cfg.client_locale)
                try:
                    if hasattr(self.ocr_engine, "close"):
                        self.ocr_engine.close()
                except Exception:
                    pass
                try:
                    self.ocr_engine = WinRtOcrEngine(req_lang)
                except TypeError:
                    try:
                        self.ocr_engine = WinRtOcrEngine()
                    except Exception:
                        pass
                except Exception as exc:
                    logger.warning("Mocked WinRT OCR locale switch (%s) failed: %s", req_lang, exc)
                use_t2s = bool(getattr(self.ocr_engine, "needs_t2s_matching", False)) if self.ocr_engine is not None else False
                self.matcher.update_catalog(ocr_augs, use_t2s=use_t2s)
                if self.detector is not None:
                    if self.ocr_engine is not None:
                        self.detector.ocr = self.ocr_engine
                    self.detector.set_client_locale(cfg.client_locale)
                    self.detector.reset()
                if self.transmute_watcher is not None:
                    if self.ocr_engine is not None:
                        self.transmute_watcher.ocr = self.ocr_engine
                    self.transmute_watcher.set_locale(cfg.client_locale, use_t2s=use_t2s)
                    self.transmute_watcher.update_catalog(ocr_augs)
            elif getattr(self, "_vision_worker", None) is not None:
                self._vision_worker.enqueue_command(
                    "set_locale",
                    {"client_locale": cfg.client_locale, "augments": ocr_augs},
                )
            self.lookup_window.bind_store(self.store)
            self.edge_drawer.populate_catalogs(self.store, self.datasets)
            if hasattr(self.combo_overlay, "set_locale_store"):
                self.combo_overlay.set_locale_store(self.store)
            if self._last_cs is not None and self.champ_overlay.isVisible():
                self.champ_overlay.update_state(self._last_cs, self.store)
            if self.sm.is_degraded_2999:
                self._refresh_degraded_sync_bar()
        if band_mode_changed or locale_changed:
            self._last_drawer_refresh_sig = None
            if hasattr(self.lookup_window, "_refresh_data_band_hint"):
                self.lookup_window._refresh_data_band_hint()
            self._refresh_drawer_view()
            if (
                self.sm.state == GameState.AUGMENT_PICKING
                and self.sm.active_cards
                and not self._overlays_hidden_by_user
                and not self._is_full_locked()
            ):
                self._render_augment_badges(self.sm.active_cards)
        if not cfg.combo_overlay_enabled:
            self.combo_overlay.exit_picker_mode()
            if self.combo_overlay.isVisible():
                self.combo_overlay.hide()
        elif (
            self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING)
            and not self._overlays_hidden_by_user
            and not self._combo_hidden_by_user
            and not self._is_full_locked()
        ):
            if not band_mode_changed:
                self._refresh_combo_plan_view()
            if not self.combo_overlay.isVisible():
                self.combo_overlay.show_overlay()
        self._sync_screen_recording_overlays(bool(getattr(cfg, "allow_screen_recording_overlays", False)))
        self._reload_hotkeys()
        self._refresh_coach_online_ui()
        self._refresh_diagnostics()

    def _on_test_gemini_connection(self, cfg: AppConfig) -> None:
        if self._commercial or self._gemini_client is None:
            return
        def worker() -> None:
            ok, msg, _ms = self._gemini_client.test_connection(
                api_key=cfg.resolved_gemini_api_key(),
                model=cfg.gemini_model,
                thinking_level=cfg.gemini_thinking_level,
                adaptive_thinking=cfg.gemini_adaptive_thinking,
                proxy_url=cfg.proxy_url,
            )
            self.ui_invoker.invoke.emit(lambda: self.lookup_window.set_test_connection_result(ok, msg))

        threading.Thread(target=worker, name="HexGeminiTest", daemon=True).start()

    def _on_champ_pos_changed(self, pos: list[int]) -> None:
        self.cfg.champ_window_pos = pos
        save_config(self.cfg)

    def _on_champ_size_changed(self, size: list[int] | None) -> None:
        self.cfg.champ_window_size = size
        save_config(self.cfg)

    def _on_champ_goal_mode_changed(self, mode: str) -> None:
        norm = "fun" if str(mode or "climb").strip().lower() == "fun" else "climb"
        if self.cfg.champ_select_goal_mode != norm:
            self.cfg.champ_select_goal_mode = norm
            save_config(self.cfg)

    def _on_rois_calibrated(self, card_roi: NormalizedRect, title_roi: NormalizedRect) -> None:
        self.cfg.card_band_roi = card_roi
        self.cfg.title_band_roi = title_roi
        save_config(self.cfg)
        if self.detector is not None:
            self.detector.update_rois(card_roi, title_roi)
        if getattr(self, "_vision_worker", None) is not None:
            self._vision_worker.enqueue_command(
                "update_rois",
                {"card_band_roi": card_roi, "title_band_roi": title_roi},
            )
            self._push_vision_context()
        self._refresh_diagnostics()

    def open_calibrator(self) -> None:
        self.calibrator.open_calibrator()

    def _on_manual_hero(self, hero_id: str) -> None:
        self.sm.manual_hero_override = hero_id
        self.sm.manual_profile_override = ""
        self._rollback_plan_and_advice_for_augment_correction()
        self._refresh_drawer_view()

    def _on_select_archetype(self, profile: str) -> None:
        prof = str(profile or "").strip().upper()
        hid = self._active_hero_id()
        band_mode = self._current_data_band_mode()
        prev_pref = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
        if prev_pref and prev_pref == prof:
            self.sm.manual_profile_override = ""
            reason = "流派自动适配"
        else:
            self.sm.manual_profile_override = prof
            templates = (
                get_hero_build_templates(self.store, hid, data_band_mode=band_mode)
                if (hid and self.store.is_loaded)
                else []
            )
            lbl = next((t.label for t in templates if t.profile == prof), prof)
            reason = f"手动流派：{lbl}"
        self.sm.invalidate_build_plan(reason=reason)
        self._latest_advice = None
        self._latest_request_id = f"arch-{uuid.uuid4().hex[:8]}"
        self.edge_drawer.set_coach_advice(None)
        self._last_drawer_refresh_sig = None
        self._save_match_state()
        self._refresh_drawer_view()
        if (
            self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING)
            and hid
            and self._is_any_coach_online()
            and self._last_live is not None
            and self._last_live.enemies
        ):
            self.trigger_coach_request("archetype_switched")

    def _recompute_overhaul_reason_from_selected(self) -> str:
        """Derive active overhaul label strictly from currently owned augments (drops removed/corrected ones)."""
        hid = self._active_hero_id()
        band_mode = self._current_data_band_mode()
        for aid in reversed(self.sm.selected_augments):
            if is_transformative_augment(self.store, aid, hero_id=hid, band_mode=band_mode):
                aug = self.store.get_augment(aid)
                return f"海克斯变阵：{aug.name if aug else aid}"
        return ""

    def _rollback_plan_and_advice_for_augment_correction(self) -> None:
        """Purge stale unbought plan lock, cached advice & in-flight request id so HUD rolls immediately."""
        reason = self._recompute_overhaul_reason_from_selected()
        self.sm.invalidate_build_plan(reason=reason)
        self._latest_advice = None
        self._latest_request_id = f"rollback-{uuid.uuid4().hex[:8]}"
        self.edge_drawer.set_coach_advice(None)

    def _flag_if_transformative_augment(self, aug_id: str) -> None:
        hid = self._active_hero_id()
        band_mode = self._current_data_band_mode()
        last_live = getattr(self, "_last_live", None)
        my_items_now = last_live.my_player.item_ids if (last_live and last_live.my_player) else ()
        owned_complete_cnt = (
            sum(
                1
                for iid in my_items_now
                if (self.store.get_item_info(iid).category == "complete_equipment" or self.store.get_item_info(iid).gold >= 2200)
            )
            if self.store.is_loaded
            else 0
        )
        if is_transformative_augment(self.store, aug_id, hero_id=hid, band_mode=band_mode):
            aug = self.store.get_augment(aug_id)
            reason = f"海克斯变阵：{aug.name if aug else aug_id}"
            self.sm.invalidate_build_plan(reason=reason)
            self._latest_advice = None
            self._latest_request_id = f"rollback-{uuid.uuid4().hex[:8]}"
            self.edge_drawer.set_coach_advice(None)
            logger.info("Transformative augment acquired -> rolling build plan overhaul: %s", reason)
        elif str(aug_id) == DESTINY_AUGMENT_ID or (
            get_special_augment_db().resolve_for_hero(self.store, aug_id, hid, band_mode=band_mode) is not None
        ):
            # WS-4: Non-transformative special synergy augment may re-order slots 2-5 via allow_plan_overhaul,
            # but MUST NOT unconditionally wipe locked_next_item so >=800G committed components at slot 1 stay protected!
            self.sm.allow_plan_overhaul = True
            self.sm.plan_overhaul_reason = self._recompute_overhaul_reason_from_selected()
            self._last_drawer_refresh_sig = None
            logger.info(
                "Special synergy augment acquired -> soft plan reorder preserving >=800G slot-1 lock (aug=%s)",
                aug_id,
            )
        elif owned_complete_cnt == 0 and hid and self.store.is_loaded and not getattr(self.sm, "manual_profile_override", ""):
            templates = get_hero_build_templates(self.store, hid, data_band_mode=band_mode)
            new_prof = infer_unified_hero_profile(
                hid,
                self.store,
                my_items=my_items_now,
                selected_augments=self.sm.selected_augments,
                data_band_mode=band_mode,
            )
            default_prof = _infer_static_default_profile(self.store, hid, data_band_mode=band_mode)
            if len(templates) >= 2 and new_prof != default_prof:
                aug = self.store.get_augment(aug_id)
                reason = f"海克斯变阵：{aug.name if aug else aug_id}"
                self.sm.invalidate_build_plan(reason=reason)
                self._latest_advice = None
                self._latest_request_id = f"rollback-{uuid.uuid4().hex[:8]}"
                self.edge_drawer.set_coach_advice(None)
                logger.info(
                    "Round-1 multi-archetype pivot (%s -> %s) -> build plan overhaul: %s",
                    default_prof,
                    new_prof,
                    reason,
                )
            elif len(self.sm.selected_augments) <= 1:
                self.sm.allow_plan_overhaul = True
                self._last_drawer_refresh_sig = None
                logger.info("Round-1 augment acquired at 0 complete items -> allow_plan_overhaul=True (aug=%s)", aug_id)

    def _is_direct_gemini_active(self) -> bool:
        if self._commercial:
            return False
        if self.cfg.coach_mode in ("off", "antigravity"):
            return False
        return bool(self.cfg.resolved_gemini_api_key())

    def _is_cloud_coach_ready(self) -> bool:
        if not self._commercial or self.cloud_client is None:
            return False
        if self.cfg.coach_mode == "off":
            return False
        if not self.cloud_client.is_logged_in:
            return False
        return not self._cloud_ai_cutoff

    def _is_any_coach_online(self) -> bool:
        if self.cfg.coach_mode == "off":
            return False
        if self._commercial:
            return self._is_cloud_coach_ready()
        if self._is_direct_gemini_active():
            return True
        if self.cfg.coach_mode in ("auto", "antigravity") and self.bridge is not None:
            return self.bridge.is_coach_online()
        return False

    @staticmethod
    def _parse_semver_tuple(ver: str) -> tuple[int, ...] | None:
        import re

        cleaned = (ver or "").strip()
        m = re.fullmatch(r"[vV]?(\d+(?:\.\d+)*)(?:[-+].*)?", cleaned)
        if not m:
            return None
        parts = [int(seg) for seg in m.group(1).split(".")]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)

    def _is_full_locked(self) -> bool:
        return bool(
            self._commercial
            and (
                self._service_locked
                or self._force_update_required
                or (self._cloud_ai_cutoff and self._cloud_cutoff_policy == "lock_all")
            )
        )

    def _recompute_cloud_cutoff_state(self) -> None:
        if self._service_locked:
            self._cloud_ai_cutoff = True
            self._cloud_cutoff_code = "SERVICE_LOCKED"
            self._cloud_cutoff_reason = self._service_lock_reason or "服务维护升级中，请稍候或更新至最新版本"
            return
        if self._force_update_required:
            self._cloud_ai_cutoff = True
            self._cloud_cutoff_code = "CLIENT_UPDATE_REQUIRED"
            self._cloud_cutoff_reason = self._force_update_reason or "当前版本已停用，请立即更新至最新版本"
            return
        if self._wallet_ai_cutoff:
            self._cloud_ai_cutoff = True
            self._cloud_cutoff_code = self._wallet_cutoff_code or "INSUFFICIENT_CREDITS"
            self._cloud_cutoff_reason = self._wallet_cutoff_reason or "余额不足 · AI 已暂停"
            return
        if self.cloud_client is None or not self.cloud_client.is_logged_in:
            self._cloud_ai_cutoff = True
            self._cloud_cutoff_code = "NOT_LOGGED_IN"
            self._cloud_cutoff_reason = "请先登录账号启用云端 AI 参谋"
            return
        self._cloud_ai_cutoff = False
        self._cloud_cutoff_code = ""
        self._cloud_cutoff_reason = ""

    def _modal_dialog_parent(self):
        return (
            self.lookup_window
            if (self.lookup_window.isVisible() and not self.lookup_window.isMinimized())
            else None
        )

    def _refresh_coach_online_ui(self) -> None:
        if self._commercial:
            tier = _normalize_coach_tier(self.cfg.coach_tier)
            tier_short = {"auto": "智能混合", "b": "B档旗舰", "a": "A档极速"}[tier]
            upd_avail = bool(self._pending_update_info and self._pending_update_info.update_available)
            self.edge_drawer.set_coach_tier(tier)
            self.lookup_window.set_coach_tier(tier)

            if self.cloud_client is None or not self.cloud_client.is_logged_in:
                gate_active = self._service_locked or self._force_update_required
                eff_code = self._cloud_cutoff_code if gate_active else "NOT_LOGGED_IN"
                eff_reason = self._cloud_cutoff_reason if gate_active else "请先登录账号启用云端 AI 参谋"
                lbl_txt = (
                    "● 服务维护中"
                    if eff_code == "SERVICE_LOCKED"
                    else ("● 需强制更新" if eff_code == "CLIENT_UPDATE_REQUIRED" else "● 未登录")
                )
                self.edge_drawer.set_coach_loading(False)
                self.edge_drawer.set_coach_online(False, label=lbl_txt, ready_hint=eff_reason)
                self.edge_drawer.set_ai_cutoff(
                    True,
                    reason=eff_reason,
                    balance_credits=None,
                    code=eff_code,
                    cutoff_policy=self._cloud_cutoff_policy,
                    update_available=upd_avail,
                )
                self.lookup_window.set_account_state(
                    None,
                    None,
                    tier=tier,
                    cutoff_reason=eff_reason,
                    cutoff_code=eff_code,
                    cutoff_policy=self._cloud_cutoff_policy,
                    update_available=upd_avail,
                )
                if self._is_full_locked():
                    self.champ_overlay.hide()
                    self.augment_overlay.hide()
                self._compute_and_broadcast_core_lifecycle()
                return

            username = self.cloud_client.current_username or "已登录"
            wallet = self.cloud_client.last_wallet
            bal = float(wallet.get("balance_credits")) if isinstance(wallet, dict) and wallet.get("balance_credits") is not None else None
            if self._cloud_ai_cutoff:
                code = self._cloud_cutoff_code or "INSUFFICIENT_CREDITS"
                reason = self._cloud_cutoff_reason or "余额不足 · AI 已暂停"
                self.edge_drawer.set_coach_loading(False)
                self.edge_drawer.set_coach_online(False, label="● AI 已暂停", ready_hint=reason)
                self.edge_drawer.set_ai_cutoff(
                    True,
                    reason=reason,
                    balance_credits=bal,
                    code=code,
                    cutoff_policy=self._cloud_cutoff_policy,
                    update_available=upd_avail,
                )
                self.lookup_window.set_account_state(
                    username,
                    wallet,
                    tier=tier,
                    cutoff_reason=reason,
                    cutoff_code=code,
                    cutoff_policy=self._cloud_cutoff_policy,
                    update_available=upd_avail,
                )
            elif self.cfg.coach_mode == "off":
                self.edge_drawer.set_coach_loading(False)
                self.edge_drawer.set_ai_cutoff(
                    False,
                    balance_credits=bal,
                    cutoff_policy=self._cloud_cutoff_policy,
                )
                self.edge_drawer.set_coach_online(False, label="● 纯本地引擎", ready_hint="已关闭 AI 参谋 · 纯本地 5ms 大数据引擎")
                self.lookup_window.set_account_state(
                    username,
                    wallet,
                    tier=tier,
                    cutoff_policy=self._cloud_cutoff_policy,
                )
            elif wallet is None:
                self.edge_drawer.set_coach_loading(False)
                self.edge_drawer.set_ai_cutoff(
                    False,
                    balance_credits=None,
                    cutoff_policy=self._cloud_cutoff_policy,
                )
                self.edge_drawer.set_coach_loading(True, label="● 云端连接中...", hint="正在同步账号与积分余额…")
                self.lookup_window.set_account_state(
                    username,
                    None,
                    tier=tier,
                    cutoff_policy=self._cloud_cutoff_policy,
                )
            else:
                self.edge_drawer.set_coach_loading(False)
                self.edge_drawer.set_coach_online(
                    True,
                    label=f"● 云端 · {tier_short}",
                    ready_hint=f"云端 AI ({tier_short}) 已就绪 · 选牌/阵亡/F6 自动推演",
                )
                self.edge_drawer.set_ai_cutoff(
                    False,
                    balance_credits=bal,
                    cutoff_policy=self._cloud_cutoff_policy,
                )
                self.lookup_window.set_account_state(
                    username,
                    wallet,
                    tier=tier,
                    cutoff_policy=self._cloud_cutoff_policy,
                )

            if self._is_full_locked():
                self.champ_overlay.hide()
                self.augment_overlay.hide()
                self.combo_overlay.set_full_locked(True)
                self.combo_overlay.hide()
            self._compute_and_broadcast_core_lifecycle()
            return

        if self._is_direct_gemini_active():
            short_m = self.cfg.gemini_model.replace("gemini-", "G")
            lvl_desc = (
                f"自适应(最高 {self.cfg.gemini_thinking_level})"
                if self.cfg.gemini_adaptive_thinking
                else self.cfg.gemini_thinking_level
            )
            lbl = f"● Gemini · {short_m}"
            hint = f"{self.cfg.gemini_model} ({lvl_desc}) 已就绪 · 选牌/阵亡/F6 自动推演"
            self.edge_drawer.set_coach_online(True, label=lbl, ready_hint=hint)
            self.lookup_window.set_coach_online(
                True,
                badge_text=f"● Gemini 直连 · {'自适应' if self.cfg.gemini_adaptive_thinking else self.cfg.gemini_thinking_level}",
                banner_text=f"GEMINI API 直连参谋 · ● {self.cfg.gemini_model} ({lvl_desc}) 已就绪",
            )
        elif self.cfg.coach_mode in ("auto", "antigravity") and self.bridge is not None and self.bridge.is_coach_online():
            self.edge_drawer.set_coach_online(True, label="● 外部参谋在线")
            self.lookup_window.set_coach_online(
                True,
                badge_text="● 外部参谋在线",
                banner_text="外部本地桥接 (18421) · ● 在线已联通",
            )
        else:
            mode_tip = (
                "已关闭 AI 参谋 · 纯本地 5ms 大数据引擎"
                if self.cfg.coach_mode == "off"
                else "尚未填入 Gemini API Key（可在「AI 参谋」页填入后独立运行）"
            )
            self.edge_drawer.set_coach_online(False, label="● 参谋未配置" if self.cfg.coach_mode != "off" else "● 纯本地引擎", ready_hint=mode_tip)
            self.lookup_window.set_coach_online(
                False,
                badge_text="● 本地 5ms 引擎运行中",
                banner_text=f"AI 参谋 · {mode_tip}",
            )

    def _sync_overlays_for_cutoff_change(self) -> None:
        locked = self._is_full_locked()
        self.combo_overlay.set_full_locked(locked)
        if locked:
            self.champ_overlay.hide()
            self.augment_overlay.hide()
            self.combo_overlay.hide()
            return
        if not self._overlays_hidden_by_user:
            if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards:
                self._render_augment_badges(self.sm.active_cards)
            elif (
                self.sm.state == GameState.IN_GAME
                and self.cfg.combo_overlay_enabled
                and not self._combo_hidden_by_user
            ):
                self._refresh_combo_plan_view()
                if not self.combo_overlay.isVisible():
                    self.combo_overlay.show_overlay()
            if self.sm.state == GameState.CHAMP_SELECT and self._last_cs is not None:
                self.champ_overlay.update_state(self._last_cs, self.store)
                if not self.champ_overlay.isVisible():
                    self.champ_overlay.show_overlay()

    def _begin_wallet_fetch(self) -> int:
        with self._cloud_state_lock:
            self._wallet_fetch_seq += 1
            return self._wallet_fetch_seq

    def _begin_gate_fetch(self) -> int:
        with self._cloud_state_lock:
            self._gate_fetch_seq += 1
            return self._gate_fetch_seq

    def _begin_wallet_and_gate_fetch(self) -> tuple[int, int]:
        with self._cloud_state_lock:
            self._wallet_fetch_seq += 1
            self._gate_fetch_seq += 1
            return self._wallet_fetch_seq, self._gate_fetch_seq

    def _accept_wallet_seq(self, seq: int | None) -> bool:
        with self._cloud_state_lock:
            if seq is None:
                self._wallet_fetch_seq += 1
                self._applied_wallet_seq = self._wallet_fetch_seq
                return True
            if seq < self._applied_wallet_seq:
                return False
            self._applied_wallet_seq = seq
            return True

    def _accept_gate_seq(self, seq: int | None) -> bool:
        with self._cloud_state_lock:
            if seq is None:
                self._gate_fetch_seq += 1
                self._applied_gate_seq = self._gate_fetch_seq
                return True
            if seq < self._applied_gate_seq:
                return False
            self._applied_gate_seq = seq
            return True

    def _apply_cloud_wallet(
        self,
        wallet: dict[str, Any] | None,
        *,
        cutoff_policy: str | None = None,
        seq: int | None = None,
    ) -> None:
        if not self._commercial or not self._accept_wallet_seq(seq):
            return
        w = wallet if isinstance(wallet, dict) else {}
        policy = str(cutoff_policy or w.get("cutoff_policy") or "").strip().lower()
        if policy in ("ai_only", "lock_all"):
            self._cloud_cutoff_policy = policy

        ai_cutoff = bool(w.get("ai_cutoff", False) or not w.get("ai_enabled", True))
        if ai_cutoff:
            code = "ACCOUNT_DISABLED" if w.get("status") == "banned" else "INSUFFICIENT_CREDITS"
            reason = "账号已停用" if code == "ACCOUNT_DISABLED" else "余额不足 · AI 已暂停"
            self._wallet_ai_cutoff = True
            self._wallet_cutoff_code = code
            self._wallet_cutoff_reason = reason
        else:
            self._wallet_ai_cutoff = False
            self._wallet_cutoff_code = ""
            self._wallet_cutoff_reason = ""
        self._recompute_cloud_cutoff_state()
        self._refresh_coach_online_ui()
        self._sync_overlays_for_cutoff_change()
        self._refresh_diagnostics()

    def _fetch_update_info_sync(self) -> UpdateInfo | None:
        base_url = self.cfg.resolved_cloud_api_base()
        if not base_url:
            return None
        if self.cloud_client is not None and self.cloud_client._http_client_factory is not None:
            data = self.cloud_client.check_update_manifest(APP_VERSION)
            return UpdateInfo.from_manifest(data, fallback_current_version=APP_VERSION)
        return check_for_update(base_url, APP_VERSION)

    def _sync_update_status_label(self, update_info: UpdateInfo, *, from_manual_check: bool) -> None:
        if update_info.update_available:
            patch_note = f"（最新数据包 {update_info.dataset_patch}）" if update_info.dataset_patch else ""
            self.lookup_window.set_update_status(f"发现新版本 v{update_info.latest_version}{patch_note}")
        elif from_manual_check:
            if self._force_update_required and update_info.min_supported_version:
                self.lookup_window.set_update_status(
                    f"当前版本 v{APP_VERSION} 低于最低要求 v{update_info.min_supported_version}，暂未检测到新安装包，请稍后重试",
                    is_error=True,
                )
            else:
                patch_note = f" · 数据镜像 {update_info.dataset_patch}" if update_info.dataset_patch else ""
                self.lookup_window.set_update_status(f"已是最新版本 v{APP_VERSION}{patch_note}")

    def _apply_service_lock_and_update(
        self,
        service_lock: dict[str, Any] | None,
        update_info: UpdateInfo | None,
        *,
        from_manual_check: bool = False,
        gate_code: str = "",
        gate_message: str = "",
        seq: int | None = None,
    ) -> None:
        if not self._commercial:
            return
        if not self._accept_gate_seq(seq):
            if from_manual_check and update_info is not None:
                self._sync_update_status_label(update_info, from_manual_check=True)
                if update_info.update_available and not self._stop_event.is_set() and not self._update_prompt_open:
                    dialog_info = (
                        replace(update_info, force_update=True)
                        if (self._force_update_required and not update_info.force_update)
                        else update_info
                    )
                    self._prompt_update_dialog(dialog_info)
            elif from_manual_check:
                self.lookup_window.set_update_status("")
            return

        if isinstance(service_lock, dict):
            locked = bool(service_lock.get("locked", False))
            lock_reason = str(service_lock.get("reason") or "").strip()
        elif update_info is not None:
            locked = bool(update_info.service_locked)
            lock_reason = str(update_info.service_lock_reason or "").strip()
        elif gate_code == "SERVICE_LOCKED":
            locked = True
            lock_reason = gate_message
        else:
            locked = False
            lock_reason = ""

        self._service_locked = locked
        self._service_lock_reason = (
            lock_reason or gate_message or "服务维护升级中，请稍候或更新至最新版本"
            if locked
            else ""
        )

        is_forced = False
        if update_info is not None:
            self._pending_update_info = update_info if update_info.update_available else None
            too_old = False
            if update_info.min_supported_version:
                cur_t = self._parse_semver_tuple(APP_VERSION)
                min_t = self._parse_semver_tuple(update_info.min_supported_version)
                if cur_t is not None and min_t is not None:
                    too_old = cur_t < min_t
            is_forced = bool(
                (update_info.update_available and update_info.force_update)
                or too_old
                or gate_code == "CLIENT_UPDATE_REQUIRED"
            )
            self._force_update_required = is_forced
            if is_forced:
                latest_v = (update_info.latest_version or "").strip()
                min_v = (update_info.min_supported_version or "").strip()
                lt_t = self._parse_semver_tuple(latest_v)
                mn_t = self._parse_semver_tuple(min_v)
                if min_v and (
                    not latest_v
                    or latest_v == APP_VERSION
                    or (lt_t is not None and mn_t is not None and mn_t > lt_t)
                ):
                    target_v = min_v
                else:
                    target_v = latest_v or min_v
                self._force_update_reason = gate_message or (
                    f"当前版本 v{APP_VERSION} 已停用，请更新至 v{target_v}"
                    if target_v
                    else "当前版本已停用，请立即更新至最新版本"
                )
            else:
                self._force_update_reason = ""
        elif gate_code == "CLIENT_UPDATE_REQUIRED":
            is_forced = True
            self._force_update_required = True
            self._force_update_reason = gate_message or "当前版本已停用，请立即更新至最新版本"

        self._recompute_cloud_cutoff_state()
        self._refresh_coach_online_ui()
        self._sync_overlays_for_cutoff_change()
        self._refresh_diagnostics()

        if update_info is not None:
            self._sync_update_status_label(update_info, from_manual_check=from_manual_check)

        if self._stop_event.is_set() or self._update_prompt_open:
            return

        if update_info is not None and update_info.update_available:
            if is_forced:
                dialog_info = replace(update_info, force_update=True) if not update_info.force_update else update_info
                self._prompt_update_dialog(dialog_info)
            elif from_manual_check or (
                update_info.latest_version and update_info.latest_version != self._dismissed_optional_version
            ):
                self._prompt_update_dialog(update_info)

    def _poll_cloud_wallet_sync(self) -> None:
        if not self._commercial or self.cloud_client is None:
            return
        if not self.cloud_client.is_logged_in:
            g_seq = self._begin_gate_fetch()
            try:
                upd_info = self._fetch_update_info_sync()
                if upd_info is not None:
                    self._apply_service_lock_and_update(
                        {"locked": upd_info.service_locked, "reason": upd_info.service_lock_reason},
                        upd_info,
                        seq=g_seq,
                    )
            except Exception as exc:
                logger.debug("Unauthenticated heartbeat update check skipped: %s", exc)
            return
        w_seq, g_seq = self._begin_wallet_and_gate_fetch()
        try:
            me = self.cloud_client.get_me()
            wallet = me.get("user") if isinstance(me, dict) else None
            pol = me.get("cutoff_policy") if isinstance(me, dict) else None
            self._apply_cloud_wallet(wallet, cutoff_policy=pol, seq=w_seq)
            svc_lock = me.get("service_lock") if isinstance(me, dict) else None
            upd_info = parse_update_info(me.get("update") if isinstance(me, dict) else None, APP_VERSION)
            self._apply_service_lock_and_update(svc_lock, upd_info, seq=g_seq)
        except CloudCoachError as exc:
            if exc.code in (
                "NOT_LOGGED_IN",
                "ACCOUNT_DISABLED",
                "INSUFFICIENT_CREDITS",
                "SERVICE_LOCKED",
                "CLIENT_UPDATE_REQUIRED",
            ) or exc.status_code in (401, 402, 403, 426, 503):
                payload = dict(exc.wallet or {})
                if exc.extra:
                    payload.update(exc.extra)
                self._on_cloud_cutoff(exc.code, exc.message, payload, wallet_seq=w_seq, gate_seq=g_seq)
        except Exception as exc:
            logger.debug("Cloud wallet heartbeat skipped: %s", exc)

    def _poll_cloud_wallet_async(self) -> None:
        if not self._commercial or self.cloud_client is None:
            return

        if not self.cloud_client.is_logged_in:
            g_seq = self._begin_gate_fetch()

            def unlogged_worker() -> None:
                try:
                    upd_info = self._fetch_update_info_sync()
                    if upd_info is not None:
                        self.ui_invoker.invoke.emit(
                            lambda ui=upd_info, gs=g_seq: self._apply_service_lock_and_update(
                                {"locked": ui.service_locked, "reason": ui.service_lock_reason},
                                ui,
                                seq=gs,
                            )
                        )
                except Exception as exc:
                    logger.debug("Async unauthenticated heartbeat skipped: %s", exc)

            threading.Thread(target=unlogged_worker, name="HexUnloggedHeartbeat", daemon=True).start()
            return

        w_seq, g_seq = self._begin_wallet_and_gate_fetch()

        def worker() -> None:
            try:
                me = self.cloud_client.get_me()
                wallet = me.get("user") if isinstance(me, dict) else None
                pol = me.get("cutoff_policy") if isinstance(me, dict) else None
                svc_lock = me.get("service_lock") if isinstance(me, dict) else None
                upd_info = parse_update_info(me.get("update") if isinstance(me, dict) else None, APP_VERSION)

                def apply_on_ui() -> None:
                    self._wallet_retry_backoff_s = 8
                    if self._wallet_retry_timer is not None and self._wallet_retry_timer.isActive():
                        self._wallet_retry_timer.stop()
                    self._apply_cloud_wallet(wallet, cutoff_policy=pol, seq=w_seq)
                    self._apply_service_lock_and_update(svc_lock, upd_info, seq=g_seq)

                self.ui_invoker.invoke.emit(apply_on_ui)
            except CloudCoachError as exc:
                if exc.code in (
                    "NOT_LOGGED_IN",
                    "ACCOUNT_DISABLED",
                    "INSUFFICIENT_CREDITS",
                    "SERVICE_LOCKED",
                    "CLIENT_UPDATE_REQUIRED",
                ) or exc.status_code in (401, 402, 403, 426, 503):
                    payload = dict(exc.wallet or {})
                    if exc.extra:
                        payload.update(exc.extra)
                    self.ui_invoker.invoke.emit(
                        lambda c=exc.code, m=exc.message, wl=payload, ws=w_seq, gs=g_seq: self._on_cloud_cutoff(
                            c, m, wl, wallet_seq=ws, gate_seq=gs
                        )
                    )
                else:
                    self.ui_invoker.invoke.emit(lambda m=exc.message: self._on_wallet_network_transient(m))
            except Exception as exc:
                logger.debug("Async cloud wallet heartbeat skipped: %s", exc)
                self.ui_invoker.invoke.emit(lambda m=str(exc): self._on_wallet_network_transient(m))

        threading.Thread(target=worker, name="HexWalletHeartbeat", daemon=True).start()

    def _on_wallet_network_transient(self, _err: str) -> None:
        if not self._commercial or self._stop_event.is_set():
            return
        if self.cloud_client is not None and self.cloud_client.is_logged_in and self.cloud_client.last_wallet is None:
            delay_s = min(60, max(8, int(self._wallet_retry_backoff_s)))
            self.edge_drawer.set_coach_loading(
                True,
                label="● 云端重连中...",
                hint=f"云端网络波动，{delay_s}秒后自动重试（本地 5ms 引擎正常可用）",
            )
            if self._wallet_retry_timer is None:
                self._wallet_retry_timer = QTimer()
                self._wallet_retry_timer.setSingleShot(True)
                self._wallet_retry_timer.timeout.connect(self._poll_cloud_wallet_async)
            if not self._wallet_retry_timer.isActive():
                self._wallet_retry_timer.start(delay_s * 1000)
                self._wallet_retry_backoff_s = min(60, delay_s * 2)

    def _sync_cloud_pricing_async(self) -> None:
        if not self._commercial or self.cloud_client is None:
            return
        client = self.cloud_client

        def worker() -> None:
            try:
                data = client.get_packages()
                ps = data.get("pricing_summary") if isinstance(data, dict) else None
                if isinstance(ps, dict):
                    self.ui_invoker.invoke.emit(lambda p=ps: self.lookup_window.update_pricing_summary(p))
            except Exception:
                pass

        threading.Thread(target=worker, name="HexPricingSync", daemon=True).start()

    def _on_cloud_advice(self, advice: CoachAdvicePayload, stats: CloudAdviseStats) -> None:
        if getattr(stats, "cost_credits", 0.0):
            try:
                self._local_spent_credits_delta += max(0.0, float(stats.cost_credits or 0.0))
            except (TypeError, ValueError):
                pass
        self._last_402_trigger_reason = ""
        self._on_coach_advice(advice, latency_ms=stats.latency_ms, stats=stats)
        if stats.wallet:
            self._apply_cloud_wallet(stats.wallet)
        else:
            self._refresh_diagnostics()

    def _on_cloud_cutoff(
        self,
        code: str,
        message: str,
        wallet: dict[str, Any],
        *,
        wallet_seq: int | None = None,
        gate_seq: int | None = None,
    ) -> None:
        self.edge_drawer.set_coach_pending(False)
        if code in ("SERVICE_LOCKED", "CLIENT_UPDATE_REQUIRED"):
            payload = wallet if isinstance(wallet, dict) else {}
            svc_lock = payload.get("service_lock") if isinstance(payload.get("service_lock"), dict) else None
            upd_raw = payload.get("update") if isinstance(payload.get("update"), dict) else None
            if code == "SERVICE_LOCKED" and svc_lock is None:
                svc_lock = {"locked": True, "reason": message}
            upd_info = parse_update_info(upd_raw, APP_VERSION)
            self._apply_service_lock_and_update(
                svc_lock,
                upd_info,
                gate_code=code,
                gate_message=message,
                seq=gate_seq,
            )
            return

        if not self._accept_wallet_seq(wallet_seq):
            return
        self._wallet_ai_cutoff = True
        self._wallet_cutoff_code = code or "INSUFFICIENT_CREDITS"
        self._wallet_cutoff_reason = message or "余额不足 · AI 已暂停"
        if self._wallet_cutoff_code == "INSUFFICIENT_CREDITS" and self._latest_request_trigger:
            self._last_402_trigger_reason = self._latest_request_trigger
        if isinstance(wallet, dict):
            pol = str(wallet.get("cutoff_policy") or "").strip().lower()
            if pol in ("ai_only", "lock_all"):
                self._cloud_cutoff_policy = pol
        self._recompute_cloud_cutoff_state()
        self._refresh_coach_online_ui()
        self._sync_overlays_for_cutoff_change()
        self._refresh_diagnostics()

    def _on_cloud_error(self, request_id: str, err: str) -> None:
        if getattr(self, "_devlab_active", False):
            req_meta = (getattr(self, "_last_coach_req_meta", None) or {}).get(request_id or "", {})
            self._emit_devlab(
                "coach",
                "ai_coach_turn_telemetry",
                {
                    "request_id": str(request_id or ""),
                    "trigger_reason": str(req_meta.get("trigger_reason") or self._latest_request_trigger or ""),
                    "error": str(err or "")[:200],
                    "stale_dropped": bool(self._latest_request_id and request_id and request_id != self._latest_request_id),
                },
            )
        if self._latest_request_id and request_id and request_id != self._latest_request_id:
            return
        self.edge_drawer.set_coach_error(err)
        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards and not self._overlays_hidden_by_user and not self._is_full_locked():
            self._render_augment_badges(self.sm.active_cards)
        self._refresh_diagnostics()

    def _on_switch_tier(self, tier: str) -> None:
        norm = _normalize_coach_tier(tier)
        self.cfg.coach_tier = norm
        save_config(self.cfg)
        self._refresh_coach_online_ui()
        self._refresh_diagnostics()

    def _compute_and_broadcast_core_lifecycle(self) -> None:
        if not self._commercial:
            self._core_lifecycle = None
            return
        from hex_helper.ui.hex_seal_overlay import resolve_core_energy_state

        is_logged_in = bool(self.cloud_client is not None and self.cloud_client.is_logged_in)
        username = self.cloud_client.current_username if (is_logged_in and self.cloud_client is not None) else ""
        wallet = self.cloud_client.last_wallet if (is_logged_in and self.cloud_client is not None) else None
        if isinstance(wallet, dict) and wallet.get("total_spent_micro_credits") is not None:
            # Full wallet snapshot received -> local delta can be synced safely
            self._local_spent_credits_delta = 0.0
        snap = resolve_core_energy_state(
            is_logged_in=is_logged_in,
            username=username,
            wallet=wallet,
            tier=_normalize_coach_tier(self.cfg.coach_tier),
            coach_mode=self.cfg.coach_mode,
            cutoff_code=self._cloud_cutoff_code if self._cloud_ai_cutoff else "",
            cutoff_reason=self._cloud_cutoff_reason if self._cloud_ai_cutoff else "",
            unsealed_user_ids=self.cfg.unsealed_user_ids,
            seal_snooze_until=self.cfg.seal_snooze_until,
            force_ready_to_unseal=self._pending_lobby_unseal,
            local_spent_credits_delta=self._local_spent_credits_delta,
        )
        self._core_lifecycle = snap
        self.lookup_window.set_core_lifecycle(snap)
        self.edge_drawer.set_core_lifecycle(
            snap,
            dismissed_count=self.cfg.opening_brief_seal_dismissed_count,
        )
        self.champ_overlay.set_core_lifecycle(snap)

    def _record_current_user_unsealed(self) -> None:
        if self._core_lifecycle is None:
            return
        ukey = (self._core_lifecycle.user_key or self._core_lifecycle.username or "").strip()
        if ukey and ukey not in self.cfg.unsealed_user_ids:
            self.cfg.unsealed_user_ids.append(ukey)
            save_config(self.cfg)

    def _on_lobby_unseal_complete(self) -> None:
        self._pending_lobby_unseal = False
        self._record_current_user_unsealed()
        self._compute_and_broadcast_core_lifecycle()

    def _on_dismiss_newbie_banner(self) -> None:
        self.cfg.newbie_banner_dismissed = True
        save_config(self.cfg)

    def _on_opening_brief_seal_dismissed(self) -> None:
        self.cfg.opening_brief_seal_dismissed_count = int(self.cfg.opening_brief_seal_dismissed_count or 0) + 1
        save_config(self.cfg)

    def _handle_core_action(self, action: str) -> None:
        if not self._commercial:
            return
        act = (action or "").strip().lower()
        if act == "register":
            self.open_login_dialog(initial_mode="register")
        elif act == "login":
            self.open_login_dialog(initial_mode="login")
        elif act == "recharge":
            self.open_recharge_dialog()
        elif act == "unseal_lobby":
            self._show_lookup()
            self.lookup_window._switch_page(2, force=True)
        elif act == "unsealed_ingame":
            self._pending_lobby_unseal = False
            self._record_current_user_unsealed()
            self._compute_and_broadcast_core_lifecycle()
            if self._active_hero_id():
                self.trigger_coach_request(self._last_402_trigger_reason or "manual_f6")
        elif act == "preview":
            from hex_helper.ui.hex_seal_overlay import CoreEnergyState, CoreInsightPreviewDialog

            st = (
                self._core_lifecycle.state
                if self._core_lifecycle is not None
                else CoreEnergyState.CORRODED_TRIAL_END
            )
            dlg = CoreInsightPreviewDialog(
                state=st,
                last_ai_highlight=self._best_ai_highlight,
                parent=self._modal_dialog_parent(),
            )
            dlg.exec()
            if dlg.chosen_action == "register":
                self.open_login_dialog(initial_mode="register")
            elif dlg.chosen_action == "recharge":
                self.open_recharge_dialog()
            elif dlg.chosen_action == "snooze":
                self.cfg.seal_snooze_until = int(time.time()) + 86400 * 3
                save_config(self.cfg)
                self._compute_and_broadcast_core_lifecycle()

    def open_login_dialog(self, initial_mode: str | None = None) -> None:
        if not self._commercial or self.cloud_client is None:
            return
        from hex_helper.ui.account_dialogs import LoginRegisterDialog, SignupBonusDialog

        mode = initial_mode
        if mode is None:
            mode = "login" if self.cloud_client.current_username else "register"
        dlg = LoginRegisterDialog(
            self.cloud_client,
            parent=self._modal_dialog_parent(),
            initial_mode=mode,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and isinstance(dlg.auth_result, dict):
            user_wallet = dlg.auth_result.get("user")
            self._pending_lobby_unseal = False
            self._apply_cloud_wallet(user_wallet if isinstance(user_wallet, dict) else None)
            self._poll_cloud_wallet_async()
            if dlg.was_registration and dlg.signup_bonus_credits > 0:
                uname = str((user_wallet or {}).get("username") or self.cloud_client.current_username or "")
                bonus_dlg = SignupBonusDialog(
                    uname,
                    dlg.signup_bonus_credits,
                    parent=self._modal_dialog_parent(),
                )
                if bonus_dlg.exec() == QDialog.DialogCode.Accepted:
                    if self.sm.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
                        self._pending_lobby_unseal = True
                        self._compute_and_broadcast_core_lifecycle()
                        self._show_lookup()
                        self.lookup_window._switch_page(2, force=True)
                        self.lookup_window.trigger_domino_unseal()

    def _on_logout_clicked(self) -> None:
        if not self._commercial or self.cloud_client is None:
            return
        self._pending_lobby_unseal = False
        self._local_spent_credits_delta = 0.0
        self.cloud_client.logout()
        self._on_cloud_cutoff("NOT_LOGGED_IN", "已退出登录，请重新登录以使用云端 AI", {})

    def open_recharge_dialog(self) -> None:
        if not self._commercial or self.cloud_client is None:
            return
        if not self.cloud_client.is_logged_in:
            self.open_login_dialog(initial_mode="register")
            if not self.cloud_client.is_logged_in:
                return
        from hex_helper.ui.account_dialogs import RechargeDialog

        wallet = self.cloud_client.last_wallet
        bal = (
            float(wallet.get("balance_credits"))
            if isinstance(wallet, dict) and wallet.get("balance_credits") is not None
            else None
        )
        in_game = self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING)
        prev_recharged = int((wallet or {}).get("total_recharged_fen") or 0) if isinstance(wallet, dict) else 0
        prev_orders = int((wallet or {}).get("paid_order_count") or 0) if isinstance(wallet, dict) else 0
        is_first = prev_recharged == 0 and prev_orders == 0
        self._recharge_was_first_for_session = is_first
        dlg = RechargeDialog(
            self.cloud_client,
            parent=self._modal_dialog_parent(),
            on_paid=self._on_recharge_paid,
            tier=_normalize_coach_tier(self.cfg.coach_tier),
            current_balance=bal,
            in_game=in_game,
            is_first_recharge=is_first,
        )
        dlg.exec()
        if dlg.unseal_requested and not in_game and self._pending_lobby_unseal:
            self._show_lookup()
            self.lookup_window._switch_page(2, force=True)
        elif in_game and dlg._paid_notified:
            # Return focus gracefully to live LOL window after in-game recharge
            try:
                game_rect = find_game_window()
                if game_rect is not None and getattr(game_rect, "hwnd", 0):
                    import ctypes

                    ctypes.windll.user32.SetForegroundWindow(int(game_rect.hwnd))
            except Exception:
                pass

    def _on_recharge_paid(self, order_data: dict[str, Any]) -> None:
        wallet = order_data.get("wallet") if isinstance(order_data, dict) else None
        in_game = self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING)
        is_first = bool(getattr(self, "_recharge_was_first_for_session", False))
        # Reset any active snooze since user just recharged intentionally
        if self.cfg.seal_snooze_until:
            self.cfg.seal_snooze_until = 0
            save_config(self.cfg)

        if in_game:
            # Track B: Zero-friction in-game auto-shatter + immediate retry of blocked 402 request
            self._pending_lobby_unseal = False
            if isinstance(wallet, dict):
                self._apply_cloud_wallet(wallet)
            else:
                self._poll_cloud_wallet_sync()
            self._record_current_user_unsealed()
            self._compute_and_broadcast_core_lifecycle()
            self.edge_drawer.play_ingame_auto_unseal()
            if self._active_hero_id():
                retry_reason = self._last_402_trigger_reason or "manual_f6"
                self._last_402_trigger_reason = ""
                QTimer.singleShot(220, lambda r=retry_reason: self.trigger_coach_request(r))
        else:
            # Track A: Lobby first-recharge arms the 3-card Hextech Seal for hand-click domino shatter
            self._pending_lobby_unseal = is_first
            if isinstance(wallet, dict):
                self._apply_cloud_wallet(wallet)
            else:
                self._poll_cloud_wallet_sync()
            if not is_first:
                self.lookup_window.status_box.trigger_pulse("#0AC8B9", 480)

    def _resolve_app_dir(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return self.data_dir / "app_install"

    def _check_updates_on_startup(self) -> None:
        if not self._commercial or self._stop_event.is_set():
            return
        self._run_update_check(silent=True)

    def _on_check_update_clicked(self) -> None:
        if not self._commercial:
            return
        self.lookup_window.set_update_status("⏳ 正在检查版本更新...")
        self._run_update_check(silent=False)

    def _run_update_check(self, *, silent: bool) -> None:
        if self._stop_event.is_set():
            return
        if self._update_check_in_flight:
            if not silent:
                self._update_check_manual_pending = True
            return
        base_url = self.cfg.resolved_cloud_api_base()
        if not base_url:
            if not silent:
                self.lookup_window.set_update_status("暂未配置云端更新服务地址", is_error=True)
            return

        self._update_check_in_flight = True
        self._update_check_manual_pending = not silent
        g_seq = self._begin_gate_fetch()

        def worker() -> None:
            try:
                info = self._fetch_update_info_sync()
                self.ui_invoker.invoke.emit(
                    lambda i=info, s=silent, gs=g_seq: self._on_update_check_result(i, silent=s, gate_seq=gs)
                )
            except Exception as exc:
                self.ui_invoker.invoke.emit(
                    lambda e=str(exc), s=silent: self._on_update_check_error(e, silent=s)
                )

        threading.Thread(target=worker, name="HexCheckUpdate", daemon=True).start()

    def _on_update_check_result(
        self,
        info: UpdateInfo | None,
        *,
        silent: bool,
        gate_seq: int | None = None,
    ) -> None:
        self._update_check_in_flight = False
        eff_silent = silent and not self._update_check_manual_pending
        self._update_check_manual_pending = False
        if self._stop_event.is_set():
            return
        if info is None:
            if not eff_silent:
                self.lookup_window.set_update_status("暂未配置云端更新服务地址", is_error=True)
            return
        svc_lock = {"locked": info.service_locked, "reason": info.service_lock_reason}
        self._apply_service_lock_and_update(
            svc_lock,
            info,
            from_manual_check=not eff_silent,
            seq=gate_seq,
        )

    def _on_update_check_error(self, err: str, *, silent: bool) -> None:
        self._update_check_in_flight = False
        eff_silent = silent and not self._update_check_manual_pending
        self._update_check_manual_pending = False
        if self._stop_event.is_set():
            return
        if eff_silent:
            logger.debug("Startup update check skipped: %s", err)
            return
        self.lookup_window.set_update_status(f"检查更新失败：{err}", is_error=True)

    def _prompt_update_dialog(self, info: UpdateInfo) -> None:
        if self._stop_event.is_set() or self._update_prompt_open:
            return
        self._update_prompt_open = True
        artifact: Path | None = None
        result = QDialog.DialogCode.Rejected
        try:
            from hex_helper.ui.account_dialogs import UpdatePromptDialog

            dlg = UpdatePromptDialog(
                info,
                self.cfg.resolved_cloud_api_base(),
                self.data_dir / "updates",
                parent=self._modal_dialog_parent(),
            )
            result = dlg.exec()
            artifact = dlg.downloaded_artifact_path
        finally:
            self._update_prompt_open = False

        if result == QDialog.DialogCode.Accepted and artifact is not None:
            try:
                launch_update_installer(artifact, self._resolve_app_dir(), os.getpid())
                self.shutdown()
            except Exception as exc:
                logger.exception("Failed launching update installer")
                self.lookup_window.set_update_status(f"启动更新安装程序失败：{exc}", is_error=True)
                if info.force_update:
                    self.shutdown()
        elif info.force_update:
            self.shutdown()
        elif info.latest_version:
            self._dismissed_optional_version = info.latest_version

    def _on_gemini_advice(self, advice: CoachAdvicePayload, stats: GeminiCallStats) -> None:
        self._on_coach_advice(advice, latency_ms=stats.latency_ms, stats=stats)
        if self._latest_request_id == advice.request_id:
            self.lookup_window.set_test_connection_result(
                True,
                f"推演完成：{stats.model} ({stats.thinking_level}) · {stats.latency_ms}ms · tokens={stats.total_tokens} (thought={stats.thought_tokens})",
            )
        self._refresh_diagnostics()

    def _on_gemini_error(self, request_id: str, err: str) -> None:
        stale = bool(self._latest_request_id and request_id and request_id != self._latest_request_id)
        if getattr(self, "_devlab_active", False):
            meta = (getattr(self, "_last_coach_req_meta", None) or {}).get(request_id, {})
            self._emit_devlab(
                "coach",
                "ai_coach_turn_telemetry",
                {
                    "request_id": str(request_id or ""),
                    "trigger_reason": str(meta.get("trigger_reason") or self._latest_request_trigger or ""),
                    "model": str(self.cfg.gemini_model or ""),
                    "stale_dropped": stale,
                    "error": str(err or "")[:160],
                },
            )
        if stale:
            return
        self.edge_drawer.set_coach_error(err)
        self.lookup_window.set_test_connection_result(False, err)
        self._refresh_diagnostics()

    def _on_gemini_coach_superseded(self, req: CoachRequestPayload, stats: GeminiCallStats) -> None:
        """Record superseded/cancelled in-flight Gemini calls (with real token usage) in DevLab."""
        if not getattr(self, "_devlab_active", False):
            return
        req_id = str(getattr(req, "request_id", "") or "")
        req_meta = (getattr(self, "_last_coach_req_meta", None) or {}).get(req_id, {})
        trig_reason = str(getattr(req, "trigger_reason", "") or req_meta.get("trigger_reason") or "")
        sup_payload: dict[str, Any] = {
            "request_id": req_id,
            "trigger_reason": trig_reason,
            "model": str(getattr(stats, "model", "") or ""),
            "thinking_level": str(getattr(stats, "thinking_level", "") or ""),
            "latency_ms": int(getattr(stats, "latency_ms", 0) or 0),
            "input_tokens": int(getattr(stats, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(stats, "output_tokens", 0) or 0),
            "thought_tokens": int(getattr(stats, "thought_tokens", 0) or 0),
            "total_tokens": int(getattr(stats, "total_tokens", 0) or 0),
            "stale_dropped": True,
            "superseded": True,
            "grounding_stats": dict(getattr(stats, "grounding_stats", None) or {}),
        }
        if trig_reason == "augment_picking":
            sup_payload["arrived_before_pick"] = False
        self._emit_devlab("coach", "ai_coach_turn_telemetry", sup_payload)

    def _get_all_overlay_exclude_rects(self) -> list[tuple[int, int, int, int]]:
        rects = list(self.augment_overlay.get_exclude_rects())
        dr = self.edge_drawer.get_exclude_rect()
        if dr is not None:
            rects.append(dr)
        cr = self.combo_overlay.get_exclude_rect()
        if cr is not None:
            rects.append(cr)
        return rects

    def _on_resolve_transmute_augment(self, new_aug_id: str, source: str = "ocr") -> None:
        accepted, completed = self.sm.resolve_transmute_augment(new_aug_id)
        if not accepted:
            return
        src_map = getattr(self, "_aug_source_by_id", None)
        if isinstance(src_map, dict):
            src_map[str(new_aug_id)] = {"source": f"transmute_{source}"}
        if source == "manual" and getattr(self, "_devlab_active", False):
            self._emit_devlab(
                "vision",
                "vision.pick.user_correction",
                {
                    "action": "manual_transmute_resolve",
                    "new_augment_id": str(new_aug_id),
                    "source": "manual",
                    "completed": bool(completed),
                },
            )
        self._flag_if_transformative_augment(new_aug_id)
        aug = self.store.get_augment(new_aug_id)
        logger.info(
            "Transmute secondary-resolved via %s: %s (completed=%s, selected=%s)",
            source,
            aug.name if aug else new_aug_id,
            completed,
            self.sm.selected_augments,
        )
        if completed and self.transmute_watcher is not None:
            self.transmute_watcher.reset()
        self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
        self._save_match_state()
        self._refresh_drawer_view()
        self._push_recent_unrecorded_augments()
        self.edge_drawer.flash_new_augment(new_aug_id)
        if completed and self.cfg.auto_trigger_coach and self._is_any_coach_online():
            self.trigger_coach_request("augment_selected")

    def _on_dismiss_transmute(self) -> None:
        had_pending = self.sm.dismiss_pending_transmute()
        if self.transmute_watcher is not None:
            self.transmute_watcher.reset()
        self.edge_drawer.set_pending_transmute(None, self.store)
        if had_pending and self.cfg.auto_trigger_coach and self._is_any_coach_online():
            self.trigger_coach_request("augment_selected")

    def _on_manual_add_augment(self, aug_id: str) -> None:
        self._cancel_augment_remove_coach_defer("manual_add_augment")
        self.sm.suppressed_visual_augments.discard(str(aug_id))
        if self.sm.add_selected_augment(aug_id):
            src_map = getattr(self, "_aug_source_by_id", None)
            if isinstance(src_map, dict):
                src_map[str(aug_id)] = {"source": "manual_add"}
            if getattr(self, "_devlab_active", False):
                self._emit_devlab(
                    "vision",
                    "vision.pick.user_correction",
                    {
                        "action": "manual_add",
                        "new_augment_id": str(aug_id),
                        "selected_count": len(self.sm.selected_augments),
                    },
                )
            if is_transmute_augment(aug_id):
                self.sm.start_transmute_if_applicable(aug_id, now_mono=time.monotonic())
                if self.transmute_watcher is not None:
                    self.transmute_watcher.reset()
                self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
                self._save_match_state()
                self._refresh_drawer_view()
                self._push_recent_unrecorded_augments()
                self._push_vision_context()
                return
            self._flag_if_transformative_augment(aug_id)
            self._save_match_state()
            self._refresh_drawer_view()
            self._push_recent_unrecorded_augments()
            self._push_vision_context()
            if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                self.trigger_coach_request("augment_selected")

    def _on_manual_remove_augment(self, idx: int) -> None:
        removed_id = (
            str(self.sm.selected_augments[idx])
            if 0 <= idx < len(self.sm.selected_augments)
            else ""
        )
        if self.sm.remove_selected_augment(idx):
            if removed_id:
                self.sm.suppressed_visual_augments.add(removed_id)
                if getattr(self, "_aug_vote_tracker", None) is not None:
                    self._aug_vote_tracker.reset()
            src_map = getattr(self, "_aug_source_by_id", None)
            meta = src_map.pop(removed_id, {}) if isinstance(src_map, dict) else {}
            if getattr(self, "_devlab_active", False):
                czone = str(meta.get("cursor_zone") or "")
                self._emit_devlab(
                    "vision",
                    "vision.pick.user_correction",
                    {
                        "action": "manual_remove",
                        "old_augment_id": removed_id,
                        "index": int(idx),
                        "prev_source": str(meta.get("source") or ""),
                        "cursor_zone": czone,
                        "was_tab_zone": czone == "tab_zone",
                    },
                )
            self._rollback_plan_and_advice_for_augment_correction()
            self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
            self._save_match_state()
            self._refresh_drawer_view()
            self._push_recent_unrecorded_augments()
            self._push_vision_context()
            if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                rem_timer = getattr(self, "_augment_remove_coach_timer", None)
                if rem_timer is not None:
                    rem_timer.start(self.AUGMENT_REMOVE_DEFER_MS)
                else:
                    self.trigger_coach_request("augment_selected")

    def _on_replace_last_augment(self, aug_id: str) -> None:
        self._cancel_augment_remove_coach_defer("manual_replace_augment")
        old_id = str(self.sm.selected_augments[-1]) if self.sm.selected_augments else ""
        if self.sm.replace_last_augment(aug_id):
            self.user_swapped_this_match = getattr(self, "user_swapped_this_match", 0) + 1
            src_map = getattr(self, "_aug_source_by_id", None)
            prev_meta = src_map.pop(old_id, {}) if isinstance(src_map, dict) else {}
            if isinstance(src_map, dict):
                src_map[str(aug_id)] = {"source": "manual_replace"}
            if getattr(self, "_devlab_active", False):
                corr_payload: dict[str, Any] = {
                    "action": "manual_replace",
                    "old_augment_id": old_id,
                    "new_augment_id": str(aug_id),
                    "prev_source": str(prev_meta.get("source") or ""),
                    "prev_confidence": prev_meta.get("confidence"),
                }
                if prev_meta.get("ms_since_last_reroll") is not None:
                    corr_payload["ms_since_last_reroll"] = prev_meta["ms_since_last_reroll"]
                self._emit_devlab("vision", "vision.pick.user_correction", corr_payload)
            aug = self.store.get_augment(aug_id)
            logger.info("Augment swapped by user to: %s", aug.name if aug else aug_id)
            self._rollback_plan_and_advice_for_augment_correction()
            if is_transmute_augment(aug_id):
                fading = tuple(c.augment.augment_id for c in self.sm.last_offered_cards if c.augment.augment_id != aug_id)
                self.sm.start_transmute_if_applicable(aug_id, now_mono=time.monotonic(), fading_ids=fading)
                if self.transmute_watcher is not None:
                    self.transmute_watcher.reset()
                self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
                self._save_match_state()
                self._refresh_drawer_view()
                self._push_recent_unrecorded_augments()
                return
            if self.transmute_watcher is not None:
                self.transmute_watcher.reset()
            self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
            self._flag_if_transformative_augment(aug_id)
            self._save_match_state()
            self._refresh_drawer_view()
            self._push_recent_unrecorded_augments()
            if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                self.trigger_coach_request("augment_selected")

    def _reset_plan_b_trackers(self, *, clear_sm_visual: bool = False) -> None:
        """Reset all Plan B vote trackers, matcher slot caches, visual death state, and bump vision_gen."""
        item_vt = getattr(self, "_item_vote_tracker", None)
        if item_vt is not None:
            item_vt.reset()
        aug_vt = getattr(self, "_aug_vote_tracker", None)
        if aug_vt is not None:
            aug_vt.reset()
        for m_attr in ("item_icon_matcher", "icon_matcher"):
            m = getattr(self, m_attr, None)
            clr = getattr(m, "clear_slot_cache", None) if m is not None else None
            if callable(clr):
                try:
                    clr()
                except Exception:
                    pass
        self._last_plan_b_scan_mono = 0.0
        src_map = getattr(self, "_aug_source_by_id", None)
        if isinstance(src_map, dict):
            src_map.clear()
        self._visual_is_dead = None
        self._last_advice_mono = 0.0
        self._match_last_my_stats = {}
        self._last_drawer_refresh_sig = None
        self._last_manual_f6_sig = None
        self._last_manual_f6_mono = 0.0
        self._last_warmup_advice_mono = 0.0
        self._last_warmup_advice_enemy_sig = ""
        self._last_warmup_dispatched_mono = 0.0
        self._last_warmup_dispatched_enemy_sig = ""
        self._last_augment_selected_sig = None
        self._last_augment_selected_mono = 0.0
        self._cancel_augment_remove_coach_defer("reset")
        self._pending_manual_f6_after_plan_b = False
        self._port_2999_conflict_detected = False
        self._port_2999_conflict_probed = False
        if clear_sm_visual and getattr(self, "sm", None) is not None:
            self.sm.visual_owned_items.clear()
            self.sm.suppressed_visual_items.clear()
            self.sm.suppressed_visual_augments.clear()
        self._vision_gen = int(getattr(self, "_vision_gen", 0) or 0) + 1
        worker = getattr(self, "_vision_worker", None)
        if worker is not None:
            worker.enqueue_command("reset_match")
            self._push_vision_context()

    def _hydrate_degraded_snapshot(self, snap: LiveMatchSnapshot) -> LiveMatchSnapshot:
        """Merge Plan B (visual + manual) owned items and visual gray-screen death into degraded snapshot."""
        if snap.my_player is None:
            return snap
        eff_items = self.sm.effective_degraded_items()
        details: list[dict[str, Any]] = []
        for idx, iid in enumerate(eff_items):
            info = self.store.static.items.get(iid)
            details.append(
                {
                    "item_id": int(iid) if iid.isdigit() else 0,
                    "count": 1,
                    "slot": idx,
                    "price": int(getattr(info, "gold", 0) or 0) if info is not None else 0,
                    "consumable": False,
                    "can_use": False,
                }
            )
        vis_dead = getattr(self, "_visual_is_dead", None)
        if vis_dead is not None:
            new_me = replace(snap.my_player, item_ids=eff_items, item_details=details, is_dead=bool(vis_dead))
        else:
            new_me = replace(snap.my_player, item_ids=eff_items, item_details=details)
        new_allies = tuple(new_me if p.riot_id == snap.my_player.riot_id else p for p in snap.allies)
        return replace(snap, my_player=new_me, allies=new_allies)

    @staticmethod
    def _probe_port_2999_conflict() -> bool:
        """Return True only when 127.0.0.1:2999 is unreachable AND local bind fails (WSAEACCES/WSAEADDRINUSE)."""
        import socket

        # If 127.0.0.1:2999 accepts TCP connections (e.g. LoL LiveClient warming up during loading screen),
        # port 2999 is actively listening rather than blocked by an ephemeral/excluded port conflict.
        try:
            with socket.create_connection(("127.0.0.1", 2999), timeout=0.15):
                return False
        except OSError:
            pass

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", 2999))
            return False
        except OSError as exc:
            winerr = getattr(exc, "winerror", 0)
            return winerr in (10013, 10048)
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _refresh_degraded_sync_bar(self, status_hint: str = "") -> None:
        if not self.sm.is_degraded_2999:
            if getattr(self.edge_drawer, "_degraded_2999_active", False):
                self.edge_drawer.set_degraded_2999_mode(False)
            return
        eff_items = self.sm.effective_degraded_items()
        pairs: list[tuple[str, str]] = []
        for iid in eff_items:
            info = self.store.static.items.get(iid)
            fb = info.name if info is not None and info.name else f"装备#{iid}"
            pairs.append((iid, self.store.display_item_name(iid, fb)))
        if not status_hint:
            if getattr(self, "_port_2999_conflict_detected", False):
                status_hint = "2999端口被其它程序(如Clash代理)占用 · 重启代理可恢复精准同步，当前Plan B护航"
            else:
                ic_cnt = self.item_icon_matcher.template_count if self.item_icon_matcher is not None else 0
                aug_cnt = self.icon_matcher.template_count if self.icon_matcher is not None else 0
                status_hint = f"2999未监听 · 视觉库({ic_cnt}装/{aug_cnt}海克斯)+手动同步中"
        self.edge_drawer.set_degraded_2999_mode(True, synced_items=pairs, status_hint=status_hint)

    def _on_manual_add_item(self, item_id: str) -> None:
        if self.sm.add_manual_item(item_id):
            if self._last_live is not None and self.sm.is_degraded_2999:
                self._last_live = self._hydrate_degraded_snapshot(self._last_live)
                if getattr(self, "_devlab_active", False) and self._action_tracker is not None:
                    self._action_tracker.on_live_snapshot(
                        self._last_live,
                        selected_augment_count=len(self.sm.selected_augments),
                        is_picker_open=(self.sm.state == GameState.AUGMENT_PICKING),
                        picker_ever_opened_since_level=bool(getattr(self, "_picker_opened_since_level", False)),
                        is_degraded_2999=True,
                    )
            self._last_drawer_refresh_sig = None
            self._save_match_state()
            self._refresh_degraded_sync_bar()
            self._refresh_drawer_view()
            self._push_recent_unrecorded_augments()
            self._push_vision_context()
            if getattr(self, "_devlab_active", False):
                self._emit_devlab(
                    "ui",
                    "ui_subcard_interact_or_dismiss",
                    {"overlay": "edge_drawer", "subcard": "plan_b_items", "action": "manual_add_item", "item_id": str(item_id)},
                )

    def _on_manual_remove_item(self, item_id: str) -> None:
        if self.sm.remove_owned_item(item_id):
            if self._last_live is not None and self.sm.is_degraded_2999:
                self._last_live = self._hydrate_degraded_snapshot(self._last_live)
            self._last_drawer_refresh_sig = None
            self._save_match_state()
            self._refresh_degraded_sync_bar()
            self._refresh_drawer_view()
            self._push_recent_unrecorded_augments()
            self._push_vision_context()
            if getattr(self, "_devlab_active", False):
                self._emit_devlab(
                    "ui",
                    "ui_subcard_interact_or_dismiss",
                    {"overlay": "edge_drawer", "subcard": "plan_b_items", "action": "manual_remove_item", "item_id": str(item_id)},
                )

    def _apply_plan_b_hits(
        self,
        item_hits: list[Any],
        slot_states: dict[int, str],
        aug_hits: list[Any],
        *,
        force_confirm: bool = False,
    ) -> tuple[int, int]:
        """Apply Plan B item & augment visual matches, feeding MatchActionTracker BEFORE UI refresh (WS-7)."""
        items_added = 0
        augs_added = 0
        changed = False
        active_suppressed_items = self.sm.prune_suppressed_visual_items()

        if (self.sm.is_degraded_2999 or force_confirm) and self._item_vote_tracker is not None:
            if item_hits or slot_states or force_confirm:
                slot_map = {
                    h.hud_slot_index: canonicalize_item_id(h.item_id)
                    for h in item_hits
                    if canonicalize_item_id(h.item_id) not in active_suppressed_items
                }
                confirmed = self._item_vote_tracker.update(
                    slot_map,
                    force_confirm=force_confirm,
                    slot_states=slot_states or None,
                )
                new_visual = [iid for iid in confirmed if iid not in active_suppressed_items]
                if new_visual != self.sm.visual_owned_items:
                    items_added = len(set(new_visual) - set(self.sm.visual_owned_items))
                    self.sm.visual_owned_items = new_visual
                    changed = True

        if len(self.sm.selected_augments) < MAX_AUGMENT_SLOTS and self._aug_vote_tracker is not None and (aug_hits or force_confirm):
            aug_slot_map = {
                h.hud_slot_index: str(h.augment_id)
                for h in aug_hits
                if str(h.augment_id) not in self.sm.suppressed_visual_augments
            }
            confirmed_augs = self._aug_vote_tracker.update(aug_slot_map, force_confirm=force_confirm)
            for aid in confirmed_augs:
                if (
                    aid not in self.sm.suppressed_visual_augments
                    and aid not in self.sm.selected_augments
                    and self.sm.add_selected_augment(aid)
                ):
                    augs_added += 1
                    changed = True
                    self._flag_if_transformative_augment(aid)
                    if getattr(self, "_devlab_active", False) and isinstance(getattr(self, "_aug_source_by_id", None), dict):
                        self._aug_source_by_id[str(aid)] = {"source": "plan_b_hud_icon"}

        if changed:
            if self._last_live is not None and self.sm.is_degraded_2999:
                self._last_live = self._hydrate_degraded_snapshot(self._last_live)
                # WS-7 CRITICAL CAUSAL ORDER: feed hydrated _last_live to MatchActionTracker BEFORE _refresh_drawer_view()
                # so completed item purchases attribute against the pre-purchase Top4 recommendation snapshot!
                if getattr(self, "_devlab_active", False) and self._action_tracker is not None:
                    self._action_tracker.on_live_snapshot(
                        self._last_live,
                        selected_augment_count=len(self.sm.selected_augments),
                        is_picker_open=(self.sm.state == GameState.AUGMENT_PICKING),
                        picker_ever_opened_since_level=bool(getattr(self, "_picker_opened_since_level", False)),
                        is_degraded_2999=True,
                    )
            self._last_drawer_refresh_sig = None
            self._save_match_state()
            self._refresh_degraded_sync_bar()
            self._refresh_drawer_view()
            self._push_recent_unrecorded_augments()
            self._push_vision_context()
            if getattr(self, "_devlab_active", False):
                self._emit_devlab(
                    "vision",
                    "vision.plan_b_hud_sync",
                    {
                        "force_confirm": bool(force_confirm),
                        "visual_items": list(self.sm.visual_owned_items),
                        "selected_augments": list(self.sm.selected_augments),
                        "items_added": items_added,
                        "augs_added": augs_added,
                        "hud_scale": self._hud_scale,
                    },
                )
        return items_added, augs_added

    def _run_plan_b_hud_visual_sync(
        self,
        frame_bgr: np.ndarray,
        game_rect: Any,
        *,
        force_confirm: bool = False,
    ) -> tuple[int, int]:
        """Run lightweight (<3ms) Plan B visual scan of bottom-HUD 6 item slots & 4 augment slots."""
        item_hits: list[Any] = []
        slot_states: dict[int, str] = {}
        aug_hits: list[Any] = []

        # 1. Scan 6 bottom-HUD item slots when 2999 is unavailable
        if self.sm.is_degraded_2999 and self.item_icon_matcher is not None:
            if self.item_icon_matcher.template_count == 0:
                self.item_icon_matcher.reload_cached_icons()
            if self.item_icon_matcher.template_count > 0:
                item_hits, slot_states = self.item_icon_matcher.scan_hud_slots_with_state(
                    frame_bgr,
                    game_rect,
                    min_score=0.58 if force_confirm else 0.62,
                    min_margin=0.03,
                    hud_scale=self._hud_scale,
                )

        # 2. Scan bottom-HUD augment slots whenever slots are missing (2999 down or mid-game restart)
        if len(self.sm.selected_augments) < MAX_AUGMENT_SLOTS and self.icon_matcher is not None and self.icon_matcher.template_count > 0:
            aug_hits = self.icon_matcher.scan_all_hud_augments(
                frame_bgr,
                game_rect,
                min_score=0.58 if force_confirm else 0.62,
                min_margin=0.04,
                hud_scale=self._hud_scale,
            )

        return self._apply_plan_b_hits(item_hits, slot_states, aug_hits, force_confirm=force_confirm)

    def _on_plan_b_rescan(self) -> None:
        """User clicked '扫画面' on the degraded sync bar: enqueue async Plan B scan on HexVisionWorker."""
        worker = getattr(self, "_vision_worker", None)
        if worker is not None and worker.is_running():
            self._push_vision_context()
            worker.enqueue_command("plan_b_rescan")
            return
        game_rect = find_game_window()
        if game_rect is None:
            self._refresh_degraded_sync_bar("未检测到游戏画面，请切回游戏后重试")
            return
        cap = self.capturer.grab((game_rect.left, game_rect.top, game_rect.right, game_rect.bottom))
        if cap is None or cap.frame_bgr is None:
            self._refresh_degraded_sync_bar("截帧失败，请确保游戏处于无边框窗口模式")
            return
        items_added, augs_added = self._run_plan_b_hud_visual_sync(cap.frame_bgr, game_rect, force_confirm=True)
        total_items = len(self.sm.effective_degraded_items())
        total_augs = len(self.sm.selected_augments)
        self._refresh_degraded_sync_bar(
            f"扫描完成：已同步 {total_items} 件装备 / {total_augs} 个海克斯（本次新增 +{items_added}装/+{augs_added}海克斯）"
        )

    def _on_coach_online(self, _online: bool) -> None:
        self._refresh_coach_online_ui()
        self._refresh_diagnostics()

    def _extract_coach_recommended_augment_id(self, advice: CoachAdvicePayload) -> str:
        tip = str(getattr(advice, "augment_pick_advice", "") or "").strip()
        if not tip:
            return ""
        cards = self.sm.active_cards or self.sm.last_offered_cards
        best_pos = 10**9
        best_id = ""
        for c in cards:
            nm = (c.augment.name or "").strip()
            if nm:
                pos = tip.find(nm)
                if 0 <= pos < best_pos:
                    best_pos = pos
                    best_id = c.augment.augment_id
        return best_id

    def _on_coach_advice(
        self,
        advice: CoachAdvicePayload,
        latency_ms: int = 0,
        *,
        stats: Any = None,
    ) -> None:
        devlab_on = bool(getattr(self, "_devlab_active", False))
        req_meta_map = getattr(self, "_last_coach_req_meta", None)
        req_meta = req_meta_map.get(advice.request_id, {}) if isinstance(req_meta_map, dict) else {}
        trig_reason = str(req_meta.get("trigger_reason") or getattr(self, "_latest_request_trigger", "") or "")
        grounding_stats = dict(getattr(stats, "grounding_stats", None) or {}) if stats is not None else {}

        eff_latency_ms = int(latency_ms or getattr(stats, "latency_ms", 0) or 0)
        if eff_latency_ms <= 0 and isinstance(req_meta, dict) and req_meta.get("dispatched_mono"):
            eff_latency_ms = max(
                1,
                int(round((time.monotonic() - float(req_meta["dispatched_mono"])) * 1000.0)),
            )
        eff_model = str(getattr(stats, "model", "") or getattr(advice, "model", "") or "")
        if not eff_model and str(getattr(getattr(self, "cfg", None), "coach_mode", "") or "") == "antigravity":
            eff_model = "agent_bridge"

        # Timeline sync guard + late picker close guard: drop superseded or post-close picker replies!
        is_superseded = bool(self._latest_request_id and advice.request_id and advice.request_id != self._latest_request_id)
        is_late_picker = bool(trig_reason == "augment_picking" and self.sm.state != GameState.AUGMENT_PICKING)
        if is_superseded or is_late_picker:
            logger.info(
                "Dropping stale coach advice for %s (latest=%s, trigger=%s, state=%s)",
                advice.request_id,
                self._latest_request_id,
                trig_reason,
                self.sm.state.value,
            )
            if devlab_on:
                stale_payload: dict[str, Any] = {
                    "request_id": str(advice.request_id or ""),
                    "trigger_reason": trig_reason,
                    "model": eff_model,
                    "thinking_level": str(getattr(stats, "thinking_level", "") or ""),
                    "latency_ms": eff_latency_ms,
                    "input_tokens": int(getattr(stats, "input_tokens", 0) or 0),
                    "output_tokens": int(getattr(stats, "output_tokens", 0) or 0),
                    "thought_tokens": int(getattr(stats, "thought_tokens", 0) or 0),
                    "total_tokens": int(getattr(stats, "total_tokens", 0) or 0),
                    "stale_dropped": True,
                    "grounding_stats": grounding_stats,
                }
                if trig_reason == "augment_picking":
                    stale_payload["arrived_before_pick"] = False
                self._emit_devlab("coach", "ai_coach_turn_telemetry", stale_payload)
            return

        # Reconcile Coach's proposed items against our Committed Match Build Plan (strict DDragon complete items only!)
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        proposed_ids: list[str] = []
        shown_next_ids: list[str] = []
        for it in advice.next_items:
            iid = canonicalize_item_id(it.item_id) if it.item_id else ""
            if (not iid or not is_valid_plan_item(self.store, iid)) and it.name:
                iid = resolve_item_id_by_name(self.store, it.name, require_valid_plan=True)
            if iid and iid not in proposed_ids and is_valid_plan_item(self.store, iid):
                proposed_ids.append(iid)
                shown_next_ids.append(normalize_item_id(iid))
        if shown_next_ids and hasattr(self, "ai_recommended_item_ids_shown"):
            self.ai_recommended_item_ids_shown.update(shown_next_ids)
        for nm in advice.build_path:
            nk = resolve_item_id_by_name(self.store, nm, require_valid_plan=True)
            if nk and nk not in proposed_ids and is_valid_plan_item(self.store, nk):
                proposed_ids.append(nk)

        if proposed_ids and self.store.is_loaded:
            # First real coach plan of the match or transformative-augment overhaul is allowed to lock/pivot the plan
            first_coach_lock = self._latest_advice is None
            is_tx_overhaul = bool(self.sm.allow_plan_overhaul and self.sm.plan_overhaul_reason)
            hid_active = self._active_hero_id() or ""
            band_mode = self._current_data_band_mode()
            gt_now = float(self._current_game_time_s() or 0.0)
            pref_prof = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
            active_prof = pref_prof or infer_unified_hero_profile(
                hid_active or None,
                self.store,
                my_items=my_items,
                selected_augments=self.sm.selected_augments,
                data_band_mode=band_mode,
            )
            if devlab_on:
                prev_lock = self.sm.locked_next_item
                new_lock, new_plan, trace = reconcile_match_build_plan_with_trace(
                    store=self.store,
                    my_items=my_items,
                    committed_plan=self.sm.committed_plan,
                    locked_next_item=self.sm.locked_next_item,
                    proposed_ids=proposed_ids,
                    allow_overhaul=self.sm.allow_plan_overhaul or first_coach_lock,
                    hero_id=hid_active,
                    selected_augments=self.sm.selected_augments,
                    transformative_overhaul=is_tx_overhaul,
                    active_profile=active_prof,
                    game_time=gt_now,
                )
                if trace.get("overridden") and self._action_tracker is not None:
                    tr_copy = dict(trace)
                    tr_copy["old_locked_item"] = prev_lock
                    tr_copy["new_locked_item"] = new_lock
                    tr_copy["source"] = "coach"
                    self._action_tracker.record_plan_override(gt_now, tr_copy)
            else:
                new_lock, new_plan = reconcile_match_build_plan(
                    store=self.store,
                    my_items=my_items,
                    committed_plan=self.sm.committed_plan,
                    locked_next_item=self.sm.locked_next_item,
                    proposed_ids=proposed_ids,
                    allow_overhaul=self.sm.allow_plan_overhaul or first_coach_lock,
                    hero_id=hid_active,
                    selected_augments=self.sm.selected_augments,
                    transformative_overhaul=is_tx_overhaul,
                    active_profile=active_prof,
                    game_time=gt_now,
                )
            self.sm.locked_next_item = new_lock
            self.sm.committed_plan = new_plan
            if self.sm.allow_plan_overhaul:
                self.sm.allow_plan_overhaul = False
            self._save_match_state()

        if len(self.sm.selected_augments) >= 4 and getattr(advice, "next_augment_goal", ""):
            advice = replace(advice, next_augment_goal="")
        self._latest_advice = advice
        self._last_advice_mono = time.monotonic()
        self._last_drawer_refresh_sig = None
        if trig_reason == "pre_death_warmup":
            self._last_warmup_dispatched_mono = 0.0
            self._last_warmup_advice_mono = self._last_advice_mono
            self._last_warmup_advice_enemy_sig = (
                self.sm._enemy_build_signature(self._last_live, store=self.store)
                if self._last_live is not None
                else ""
            )

        if devlab_on:
            local_top_item = str(req_meta.get("local_top_item_id") or "")
            coach_top_item = shown_next_ids[0] if shown_next_ids else ""
            local_vs_coach_disagreed = bool(
                local_top_item
                and coach_top_item
                and normalize_item_id(local_top_item) != normalize_item_id(coach_top_item)
            )
            rec_aug_id = self._extract_coach_recommended_augment_id(advice)
            in_picker = self.sm.state == GameState.AUGMENT_PICKING
            if advice.request_id and advice.request_id == getattr(self, "_active_picker_coach_req_id", ""):
                self._active_picker_coach_arrived = in_picker
                self._active_picker_coach_aug_advice = rec_aug_id
            turn_payload: dict[str, Any] = {
                "request_id": str(advice.request_id or ""),
                "trigger_reason": trig_reason,
                "model": eff_model,
                "thinking_level": str(getattr(stats, "thinking_level", "") or ""),
                "latency_ms": eff_latency_ms,
                "input_tokens": int(getattr(stats, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(stats, "output_tokens", 0) or 0),
                "thought_tokens": int(getattr(stats, "thought_tokens", 0) or 0),
                "total_tokens": int(getattr(stats, "total_tokens", 0) or 0),
                "stale_dropped": False,
                "local_vs_coach_disagreed": local_vs_coach_disagreed,
                "local_top_item_id": local_top_item,
                "coach_top_items": shown_next_ids[:4],
                "recommended_augment_id": rec_aug_id,
                "grounding_stats": grounding_stats,
            }
            if trig_reason == "augment_picking":
                turn_payload["arrived_before_pick"] = in_picker
            self._emit_devlab("coach", "ai_coach_turn_telemetry", turn_payload)
            if self._action_tracker is not None:
                self._action_tracker.record_coach_advice(
                    float(self._current_game_time_s() or 0.0),
                    request_id=str(advice.request_id or ""),
                    trigger_reason=trig_reason,
                    recommended_item_ids=shown_next_ids,
                    recommended_augment_id=rec_aug_id,
                    latency_ms=float(eff_latency_ms),
                )
                req_id_str = str(advice.request_id or "")
                sup_req_id = str(getattr(self, "_death_superseded_req_id", "") or "")
                dw = getattr(self._action_tracker, "_death_window", None)
                if (
                    req_id_str
                    and sup_req_id
                    and req_id_str == sup_req_id
                    and dw is not None
                    and not getattr(dw, "coach_arrived", False)
                ):
                    dw.coach_triggered = True
                    dw.coach_skip_reason = ""
                    dw.coach_arrived = True
                    dw.coach_request_id = req_id_str
                    dw.coach_latency_ms = float(eff_latency_ms)
                    if not getattr(dw, "first_buy_seen", False):
                        dw.coach_arrived_before_buy = True
                    self._death_superseded_req_id = ""
                    self._death_defer_superseded_by_picker = False

        if advice.opening_brief:
            self._cancel_game_start_coach_defer("opening_brief received")
        self._update_best_ai_highlight(advice)
        self.edge_drawer.set_committed_plan(
            self.sm.committed_plan,
            self.sm.locked_next_item,
            self.sm.plan_overhaul_reason,
        )
        allow_brief_expand = (
            trig_reason != "pre_death_warmup"
            and not self._overlays_hidden_by_user
            and not self._is_full_locked()
            and self.sm.state != GameState.AUGMENT_PICKING
        )
        if allow_brief_expand and advice.opening_brief:
            self._mark_overlay_open("edge_drawer", "auto_brief_expand")
        self.edge_drawer.set_coach_advice(advice, allow_auto_expand=allow_brief_expand)
        self.lookup_window.set_coach_advice(advice)
        hid = self._active_hero_id()
        hero = self.store.get_hero(hid) if hid else None
        self.lookup_window.append_coach_history(
            hero.name if hero else "未锁定",
            self._latest_request_trigger,
            advice,
            latency_ms=latency_ms,
        )
        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards:
            self._render_augment_badges(self.sm.active_cards)

    def _active_hero_id(self) -> str | None:
        return self.sm.manual_hero_override or self.sm.current_hero_id

    def _current_data_band_mode(self) -> str:
        cfg = getattr(self, "cfg", None)
        raw = str(getattr(cfg, "data_band_mode", "all") or "all").strip().lower()
        return raw if raw in DATA_BAND_MODES else "all"

    def _current_my_stats(self) -> dict[str, float] | None:
        live = getattr(self, "_last_live", None)
        live_stats = getattr(live, "my_stats", None) if live is not None else None
        if isinstance(live_stats, dict) and live_stats:
            return live_stats
        cached = getattr(self, "_match_last_my_stats", None)
        if isinstance(cached, dict) and cached:
            return cached
        return None

    def _resolve_hero_from_live_player(self, player: Any) -> Any | None:
        """Resolve canonical HeroStat prioritizing invariant raw_champion_name before localized champion_name."""
        if player is None or not self.store.is_loaded:
            return None
        raw_nm = str(getattr(player, "raw_champion_name", "") or "").strip()
        if raw_nm:
            hero = self.store.find_hero_by_name(raw_nm)
            if hero is not None:
                return hero
        champ_nm = str(getattr(player, "champion_name", "") or "").strip()
        if champ_nm:
            return self.store.find_hero_by_name(champ_nm)
        return None

    def _infer_player_comp_profile(
        self,
        player: Any,
        *,
        band_mode: str,
        hero_id_override: str | None = None,
        selected_augments: tuple[str, ...] | list[str] = (),
        my_stats: dict[str, float] | None = None,
    ) -> str:
        """Infer a player's compact comp profile token for build_combo_plan() team weighting."""
        p_hid = str(hero_id_override).strip() if hero_id_override else ""
        if not p_hid and self.store.is_loaded:
            h_obj = self._resolve_hero_from_live_player(player)
            if h_obj is not None:
                p_hid = str(h_obj.hero_id)

        item_ids = tuple(getattr(player, "item_ids", ()) or ())
        has_known_item = False
        has_bruiser_item = False
        has_crit_item = False
        if self.store.is_loaded and item_ids:
            for iid in item_ids:
                info = self.store.get_item_info(iid)
                st = info.stats or {}
                if st or (info.name and not info.name.startswith("装备#")):
                    has_known_item = True
                ad_v = float(st.get("FlatPhysicalDamageMod", 0.0) or 0.0)
                hp_v = float(st.get("FlatHPPoolMod", 0.0) or 0.0)
                arm_v = float(st.get("FlatArmorMod", 0.0) or 0.0)
                crit_v = float(st.get("FlatCritChanceMod", 0.0) or 0.0)
                if ad_v >= 20.0 and (hp_v >= 200.0 or arm_v >= 30.0):
                    has_bruiser_item = True
                if crit_v > 0.0:
                    has_crit_item = True

        # Avoid fabricating phantom AD/ranged profiles from synthetic stub players with unknown names and no items
        if not p_hid and not has_known_item:
            return ""

        try:
            prof = infer_unified_hero_profile(
                p_hid or None,
                self.store if self.store.is_loaded else None,
                my_items=item_ids,
                selected_augments=selected_augments,
                my_stats=my_stats,
                data_band_mode=band_mode,
            )
        except TypeError:
            prof = infer_unified_hero_profile(
                p_hid or None,
                self.store if self.store.is_loaded else None,
                my_items=item_ids,
                selected_augments=selected_augments,
                my_stats=my_stats,
            )

        if prof == "AD":
            if not has_bruiser_item and self.store.is_loaded and p_hid and not item_ids:
                try:
                    cbs = self.store.get_core_builds(p_hid, band_mode=band_mode)[:2]
                except TypeError:
                    cbs = self.store.get_core_builds(p_hid)[:2]
                for cb in cbs:
                    for iid in cb.items:
                        st = self.store.get_item_info(iid).stats or {}
                        ad_v = float(st.get("FlatPhysicalDamageMod", 0.0) or 0.0)
                        hp_v = float(st.get("FlatHPPoolMod", 0.0) or 0.0)
                        arm_v = float(st.get("FlatArmorMod", 0.0) or 0.0)
                        if ad_v >= 20.0 and (hp_v >= 200.0 or arm_v >= 30.0):
                            has_bruiser_item = True
                            break
                    if has_bruiser_item:
                        break
            spells = tuple(getattr(player, "summoner_spells", ()) or ())
            has_snowball = any(str(s).strip() in ("雪球", "标记", "Snowball", "Mark") for s in spells)
            if has_bruiser_item or (has_snowball and not has_crit_item):
                return "FIGHTER"
        return prof

    def _current_team_profiles(
        self,
        *,
        band_mode: str | None = None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Derive live (ally_profiles, enemy_profiles) for comp-aware augment planning."""
        live = getattr(self, "_last_live", None)
        if live is None:
            return (), ()
        bmode = band_mode or self._current_data_band_mode()
        my_p = getattr(live, "my_player", None)
        my_hid = self._active_hero_id()
        my_stats = self._current_my_stats()

        ally_players = list(getattr(live, "allies", ()) or ())
        if my_p is not None:
            my_rid = str(getattr(my_p, "riot_id", "") or "").strip()
            my_cname = str(getattr(my_p, "champion_name", "") or "").strip()
            has_self = any(
                (p is my_p)
                or (my_rid and str(getattr(p, "riot_id", "") or "").strip() == my_rid)
                or (my_cname and str(getattr(p, "champion_name", "") or "").strip() == my_cname)
                for p in ally_players
            )
            if not has_self:
                ally_players.insert(0, my_p)

        ally_profs: list[str] = []
        for p in ally_players:
            is_self = my_p is not None and (
                (p is my_p)
                or (
                    bool(getattr(my_p, "riot_id", ""))
                    and getattr(p, "riot_id", "") == getattr(my_p, "riot_id", "")
                )
                or (
                    bool(getattr(my_p, "champion_name", ""))
                    and getattr(p, "champion_name", "") == getattr(my_p, "champion_name", "")
                )
            )
            prof = self._infer_player_comp_profile(
                p,
                band_mode=bmode,
                hero_id_override=(my_hid if is_self else None),
                selected_augments=(self.sm.selected_augments if is_self else ()),
                my_stats=(my_stats if is_self else None),
            )
            if prof:
                ally_profs.append(prof)

        enemy_profs: list[str] = []
        for p in getattr(live, "enemies", ()) or ():
            prof = self._infer_player_comp_profile(p, band_mode=bmode)
            if prof:
                enemy_profs.append(prof)

        return tuple(ally_profs), tuple(enemy_profs)

    def _telemetry_worker_loop(self) -> None:
        """Background daemon polling LCU and 127.0.0.1:2999 with ZERO Qt GUI main-thread blocking."""
        lcu_fallback_started_mono: float = 0.0
        live_2999_seen_for_match: bool = False
        fallback_logged: bool = False
        last_degraded_enemy_count: int = -1

        def _resolve_hero_names(cid_str: str) -> tuple[str, str]:
            hero = self.store.get_hero(cid_str)
            if hero is not None:
                alias = getattr(hero, "alias", "") or getattr(hero, "en_name", "") or hero.name
                return hero.name, str(alias)
            return cid_str, cid_str

        while not self._stop_event.is_set():
            try:
                if not self.store.is_loaded:
                    self._stop_event.wait(0.5)
                    continue

                live_snap = self.live.poll()
                if live_snap is not None:
                    live_2999_seen_for_match = True
                    lcu_fallback_started_mono = 0.0
                    fallback_logged = False
                    last_degraded_enemy_count = -1
                    self.ui_invoker.invoke.emit(lambda s=live_snap: self._apply_live_snapshot(s, degraded_2999=False))
                    self._stop_event.wait(1.0)
                    continue

                with self._get_lcu_lock():
                    phase, cs = self.lcu.poll()

                if phase != "InProgress":
                    lcu_fallback_started_mono = 0.0
                    live_2999_seen_for_match = False
                    fallback_logged = False
                    last_degraded_enemy_count = -1
                    if self.sm.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
                        reset_fn = getattr(self.live, "reset_failures", None)
                        if callable(reset_fn):
                            reset_fn()
                elif (
                    not live_2999_seen_for_match
                    and (
                        fallback_logged
                        or self.live.consecutive_failures >= int(self.LCU_2999_DEGRADED_GRACE_FAILURES)
                    )
                    and find_game_window() is not None
                ):
                    now_m = time.monotonic()
                    if lcu_fallback_started_mono <= 0.0:
                        lcu_fallback_started_mono = now_m
                    elapsed_s = max(1.0, now_m - lcu_fallback_started_mono)
                    with self._get_lcu_lock():
                        fb_snap = self.lcu.get_in_game_fallback_snapshot(
                            hero_resolver=_resolve_hero_names,
                            game_time_s=elapsed_s,
                            allow_all_modes=bool(self.cfg.force_all_modes),
                        )
                    if fb_snap is not None:
                        cur_enemies = len(fb_snap.enemies)
                        if not fallback_logged:
                            fallback_logged = True
                            last_degraded_enemy_count = cur_enemies
                            logger.warning(
                                "LiveClient 127.0.0.1:2999 unavailable during InProgress (fails=%d); "
                                "entering LCU degraded in-game mode (hero=%s, allies=%d, enemies=%d)",
                                int(getattr(self.live, "consecutive_failures", 0) or 0),
                                fb_snap.my_player.champion_name if fb_snap.my_player else "?",
                                len(fb_snap.allies),
                                cur_enemies,
                            )
                            self._emit_devlab(
                                "game",
                                "gameflow_phase_transition",
                                {
                                    "from_phase": "IDLE",
                                    "to_phase": "IN_GAME",
                                    "reason": "lcu_2999_degraded_fallback",
                                    "live_2999_available": False,
                                },
                                game_time_s=elapsed_s,
                            )
                        elif cur_enemies > last_degraded_enemy_count:
                            logger.info(
                                "LCU degraded enemy roster refreshed: %d -> %d enemies (allies=%d)",
                                last_degraded_enemy_count,
                                cur_enemies,
                                len(fb_snap.allies),
                            )
                            last_degraded_enemy_count = cur_enemies
                        self.ui_invoker.invoke.emit(lambda s=fb_snap: self._apply_live_snapshot(s, degraded_2999=True))
                        self._stop_event.wait(1.0)
                        continue

                self.ui_invoker.invoke.emit(lambda p=phase, c=cs: self._apply_lcu_poll(p, c))
            except Exception as exc:
                logger.debug("Telemetry worker error: %s", exc)
            self._stop_event.wait(1.0)

    @staticmethod
    def _compute_match_key(live_snap: LiveMatchSnapshot, hero_id: str | None) -> str:
        roster = sorted(
            f"{p.team}:{p.champion_name}"
            for p in (*live_snap.allies, *live_snap.enemies)
            if p.champion_name
        )
        return f"{hero_id or ''}|{'|'.join(roster)}"

    def _save_match_state(self) -> None:
        if not self._active_match_key:
            return
        try:
            payload = {
                "match_key": self._active_match_key,
                "hero_id": self._active_hero_id() or "",
                "selected_augments": list(self.sm.selected_augments),
                "manual_owned_items": list(self.sm.manual_owned_items),
                "visual_owned_items": list(self.sm.visual_owned_items),
                "suppressed_visual_items": sorted(self.sm.prune_suppressed_visual_items()),
                "suppressed_visual_augments": sorted(self.sm.suppressed_visual_augments),
                "committed_plan": list(self.sm.committed_plan),
                "locked_next_item": self.sm.locked_next_item,
                "plan_overhaul_reason": self.sm.plan_overhaul_reason,
                "manual_profile_override": getattr(self.sm, "manual_profile_override", ""),
                "saved_at": time.time(),
            }
            self._match_state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._match_state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._match_state_path)
        except Exception as exc:
            logger.debug("Failed saving match_state.json: %s", exc)

    def _restore_match_state_if_matching(self, match_key: str) -> bool:
        if not match_key or not self._match_state_path.exists():
            return False
        try:
            data = json.loads(self._match_state_path.read_text(encoding="utf-8"))
            if data.get("match_key") != match_key:
                return False
            if (time.time() - float(data.get("saved_at") or 0.0)) > 5400.0:
                return False
            restored_augs = [
                str(a) for a in (data.get("selected_augments") or []) if str(a) and str(a) not in _NON_AUGMENT_CHOICE_IDS
            ]
            if restored_augs and not self.sm.selected_augments:
                self.sm.selected_augments = restored_augs[:MAX_AUGMENT_SLOTS]
                self.sm._rebuild_recent_offered_pool()
            restored_manual_items = [
                canonicalize_item_id(str(i)) for i in (data.get("manual_owned_items") or []) if str(i).strip()
            ]
            if restored_manual_items and not self.sm.manual_owned_items:
                self.sm.manual_owned_items = restored_manual_items[:6]
            restored_visual_items = [
                canonicalize_item_id(str(i)) for i in (data.get("visual_owned_items") or []) if str(i).strip()
            ]
            if restored_visual_items and not self.sm.visual_owned_items:
                self.sm.visual_owned_items = restored_visual_items[:6]
            raw_supp = data.get("suppressed_visual_items") or []
            if isinstance(raw_supp, dict):
                raw_supp = list(raw_supp.keys())
            for raw_si in raw_supp:
                si = canonicalize_item_id(str(raw_si))
                if si:
                    self.sm.suppress_visual_item(si)
            restored_supp_augs = {
                str(a).strip() for a in (data.get("suppressed_visual_augments") or []) if str(a).strip()
            }
            if restored_supp_augs:
                self.sm.suppressed_visual_augments.update(restored_supp_augs)
            restored_plan = [
                canonicalize_item_id(i)
                for i in (data.get("committed_plan") or [])
                if is_valid_plan_item(self.store, i)
            ]
            if restored_plan and not self.sm.committed_plan:
                self.sm.committed_plan = restored_plan[:5]
            lock_id = canonicalize_item_id(str(data.get("locked_next_item") or ""))
            if lock_id and is_valid_plan_item(self.store, lock_id) and not self.sm.locked_next_item:
                self.sm.locked_next_item = lock_id
            restored_prof = str(data.get("manual_profile_override") or "").strip().upper()
            if restored_prof and not self.sm.manual_profile_override:
                self.sm.manual_profile_override = restored_prof
            self.sm.plan_overhaul_reason = self._recompute_overhaul_reason_from_selected()
            logger.info(
                "Restored live match state across restart: augs=%s plan=%s",
                self.sm.selected_augments,
                self.sm.committed_plan,
            )
            return True
        except Exception as exc:
            logger.debug("Failed restoring match_state.json: %s", exc)
            return False

    def _clear_match_state(self) -> None:
        self._active_match_key = ""
        try:
            if self._match_state_path.exists():
                self._match_state_path.unlink()
        except Exception:
            pass

    def _reset_match_telemetry_counters(self) -> None:
        self.ai_calls_this_match = 0
        if hasattr(self, "ai_recommended_item_ids_shown") and isinstance(self.ai_recommended_item_ids_shown, set):
            self.ai_recommended_item_ids_shown.clear()
        self.user_swapped_this_match = 0
        self._match_max_game_time_s = 0.0
        self._match_last_my_items = ()
        self._match_last_hero_id = ""
        self._match_last_hero_name = ""
        self._match_telemetry_sent_key = ""
        self._match_is_first_prismatic = False
        self._match_won_result = -1
        self._last_kda = ""
        self._last_team_kills = ""
        self._best_ai_highlight = ""
        self._active_game_id = 0
        self._active_queue_id = 0
        self._match_is_non_mayhem = False
        self._match_game_mode = ""
        self._match_session_seq = int(getattr(self, "_match_session_seq", 0) or 0) + 1
        self._hud_wide_diag_saved_this_match = False
        self._death_defer_superseded_by_picker = False
        self._death_superseded_req_id = ""
        self._match_last_skin_id = 0
        self._match_last_skin_name = ""
        self._last_offer_rec_snapshot = None
        self._last_offer_emitted_sig = None
        self._pending_reroll_prev_offer = None
        self._pending_reroll_slots = None
        self._picker_opened_since_level = False
        self._click_last_tick_mono = 0.0
        self._click_max_gap_ms_in_picker = 0.0
        self._last_smart_gate_skip_mono = 0.0
        self._last_smart_gate_skip_detail = ""
        self._active_picker_coach_req_id = ""
        self._active_picker_coach_arrived = False
        self._active_picker_coach_aug_advice = ""
        self._last_picker_partial_open = False
        self._last_picker_expected_slots = 0
        for attr in ("_last_ocr_anomaly_mono", "_last_coach_req_meta", "_aug_source_by_id"):
            d = getattr(self, attr, None)
            if isinstance(d, dict):
                d.clear()
        self._reset_plan_b_trackers()

    def _restore_configured_dataset_mode(self) -> bool:
        target_mode = (self.cfg.dataset_mode or "normal").strip() or "normal"
        switched = False
        if self._auto_switched_first_prismatic or (
            self.cfg.auto_first_prismatic_mode and self.store.active_match_mode != target_mode
        ):
            if self.store.switch_match_mode(target_mode):
                switched = True
                logger.info("Match ended -> restored dataset mode to %s", target_mode)
        self._auto_switched_first_prismatic = False
        self._match_is_first_prismatic = False
        self.edge_drawer.set_match_mode_badge("")
        return switched

    def _maybe_auto_switch_first_prismatic(self, cards: tuple[DetectedCard, ...]) -> bool:
        """During Round 1 (0 selected augments & level < 7 & < 180s), if all 3 detected cards are prismatic, switch to first-prismatic."""
        cur_lvl = (
            int(getattr(self._last_live.my_player, "level", 0) or 0)
            if (getattr(self, "_last_live", None) and self._last_live.my_player)
            else 0
        )
        gt_s = float(self._current_game_time_s() or 0.0) if hasattr(self, "_current_game_time_s") else 0.0
        if cur_lvl >= 7 or gt_s >= 180.0:
            return False
        if (
            len(self.sm.selected_augments) == 0
            and len(cards) >= 3
            and all(c.augment.rarity == "prismatic" for c in cards)
        ):
            self._match_is_first_prismatic = True
            if self.cfg.auto_first_prismatic_mode:
                if self.store.active_match_mode != "first-prismatic":
                    if self.store.switch_match_mode("first-prismatic"):
                        self._auto_switched_first_prismatic = True
                        self.log_ring.update_match_meta(dataset_mode="first-prismatic")
                        if self._devlab_active:
                            self._devlab.update_match_meta(dataset_tag=self._current_dataset_tag())
                        logger.info(
                            "Round 1 all-prismatic detected -> auto-switched dataset mode to first-prismatic"
                        )
                        self.edge_drawer.set_match_mode_badge("首棱彩局")
                        self._refresh_drawer_view()
                        self._refresh_diagnostics()
                        return True
                else:
                    self.edge_drawer.set_match_mode_badge("首棱彩局")
        return False

    def _should_defer_death_coach(self, live_snap: LiveMatchSnapshot | None) -> bool:
        if live_snap is None or live_snap.my_player is None:
            return False
        lvl = int(live_snap.my_player.level or 0)
        if lvl >= 3 and len(self.sm.selected_augments) < expected_augment_slots_for_level(lvl):
            return True
        return False

    def _schedule_death_coach_defer(self) -> None:
        lvl = int(self._last_live.my_player.level) if (self._last_live and self._last_live.my_player) else 0
        logger.info(
            "Deferring death_enemy_build_changed coach call by %.1fs (level=%d, selected=%d/%d)",
            self.DEATH_COACH_DEFER_MS / 1000.0,
            lvl,
            len(self.sm.selected_augments),
            expected_augment_slots_for_level(lvl),
        )
        self._death_coach_defer_timer.start(self.DEATH_COACH_DEFER_MS)

    def _cancel_death_coach_defer(self, reason: str = "") -> None:
        if self._death_coach_defer_timer.isActive():
            self._death_coach_defer_timer.stop()
            r_low = str(reason or "").lower()
            if "augment_picking" in r_low or "augment_selected" in r_low or "manual_f6" in r_low:
                self._death_defer_superseded_by_picker = True
            if self._action_tracker is not None and reason:
                self._action_tracker.note_death_coach_decision(False, f"deferred_cancelled:{reason}")
            if reason:
                logger.info("Cancelled deferred death_enemy_build_changed coach call: %s", reason)

    def _on_death_coach_defer_timeout(self) -> None:
        if self.sm.state != GameState.IN_GAME:
            return
        self._death_defer_superseded_by_picker = False
        if self.cfg.auto_trigger_coach and self._is_any_coach_online():
            if self._action_tracker is not None:
                self._action_tracker.note_death_coach_decision(True, "")
            self.trigger_coach_request("death_enemy_build_changed")

    def _cancel_augment_remove_coach_defer(self, reason: str = "") -> None:
        timer = getattr(self, "_augment_remove_coach_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
            if reason:
                logger.info("Cancelled deferred post-remove augment coach call: %s", reason)

    def _on_augment_remove_coach_timeout(self) -> None:
        if self.sm.state != GameState.IN_GAME:
            return
        if self.cfg.auto_trigger_coach and self._is_any_coach_online():
            self.trigger_coach_request("augment_selected")

    def _schedule_game_start_coach_defer(self, match_key: str) -> None:
        self._pending_game_start_match_key = match_key
        logger.info(
            "Deferring game_start coach call up to %.1fs while LobbyScout runs",
            self.GAME_START_SCOUT_WAIT_MS / 1000.0,
        )
        timer = getattr(self, "_game_start_coach_defer_timer", None)
        if timer is not None:
            timer.start(self.GAME_START_SCOUT_WAIT_MS)

    def _cancel_game_start_coach_defer(self, reason: str = "") -> None:
        timer = getattr(self, "_game_start_coach_defer_timer", None)
        had_pending = bool(getattr(self, "_pending_game_start_match_key", "")) or bool(
            timer is not None and timer.isActive()
        )
        self._pending_game_start_match_key = ""
        if timer is not None and timer.isActive():
            timer.stop()
        if had_pending and reason:
            logger.info("Cancelled deferred game_start coach call: %s", reason)

    def _flush_pending_game_start_coach(self, match_key: str, *, source: str) -> None:
        pending_key = getattr(self, "_pending_game_start_match_key", "")
        if not pending_key:
            return
        if match_key and pending_key != match_key:
            return
        self._cancel_game_start_coach_defer()
        if self._stop_event.is_set():
            return
        if pending_key != self._active_match_key:
            return
        if self.sm.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            return
        if self.cfg.auto_trigger_coach and self._is_any_coach_online():
            logger.info("Flushing deferred game_start coach call (%s)", source)
            self.trigger_coach_request("game_start")

    def _on_game_start_coach_defer_timeout(self) -> None:
        self._flush_pending_game_start_coach(self._active_match_key, source="timeout")

    def _schedule_picker_coach_trigger(self, *, after_reroll: bool = False) -> None:
        if not self.cfg.auto_trigger_coach or not self._is_any_coach_online():
            return
        delay_ms = self.PICKER_COACH_REROLL_DEBOUNCE_MS if after_reroll else self.PICKER_COACH_DEBOUNCE_MS
        self._picker_coach_debounce_timer.start(delay_ms)

    def _cancel_picker_coach_debounce(self) -> None:
        timer = getattr(self, "_picker_coach_debounce_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()

    def _cancel_inflight_picker_coach(self) -> None:
        """Immediately stop pending picker debounce and cancel dying in-flight augment_picking HTTP calls."""
        self._cancel_picker_coach_debounce()
        for worker_attr in ("gemini_coach", "cloud_coach"):
            worker = getattr(self, worker_attr, None)
            cancel_fn = getattr(worker, "cancel_pending", None) if worker is not None else None
            if callable(cancel_fn):
                try:
                    cancel_fn({"augment_picking"})
                except Exception:
                    pass

    def _on_picker_coach_debounce_timeout(self) -> None:
        if self.sm.state != GameState.AUGMENT_PICKING or not self.sm.active_cards:
            return
        if (
            getattr(self, "_visual_vanish_mono", None) is not None
            or getattr(self, "_frozen_pick_slot", None) is not None
            or getattr(self.sm, "_picker_closed_since", None) is not None
        ):
            return
        if not self.cfg.auto_trigger_coach or not self._is_any_coach_online():
            return
        self.trigger_coach_request("augment_picking")

    def _should_skip_picker_ai_call(
        self,
        cards: tuple[DetectedCard, ...],
        scored_cards: list[tuple[DetectedCard, AugmentScore]] | None = None,
        verdict: PickerComboVerdict | None = None,
    ) -> tuple[bool, str]:
        """Smart local gate: skip automatic augment_picking AI call when local 0ms winner is decisive."""
        self._last_smart_gate_eval: dict[str, Any] = {
            "skipped": False,
            "reason": "gate_disabled_or_few_cards",
            "top1_top2_gap": None,
            "threshold": self.SMART_PICKER_GATE_DELTA_GAP,
        }
        if not self.cfg.smart_picker_ai_gate or len(cards) < 2:
            return False, ""
        exp_slots = int(getattr(self, "_last_picker_expected_slots", 0) or 0)
        if bool(getattr(self, "_last_picker_partial_open", False)) or (exp_slots >= 3 and len(cards) < exp_slots):
            self._last_smart_gate_eval["reason"] = "partial_open_incomplete_slots"
            return False, ""
        hid = self._active_hero_id()
        if not hid or not self.store.is_loaded:
            self._last_smart_gate_eval["reason"] = "no_hero_or_store"
            return False, ""
        hero = self.store.get_hero(hid)
        hero_wr = hero.win_rate if hero else 0.50

        if scored_cards is None:
            scored_cards = []
            for c in cards:
                st = self.store.get_hero_augment_stat(hid, c.augment.augment_id)
                rdist = self.store.get_hero_rarity_distribution(hid, c.augment.rarity)
                sc = score_hero_augment(
                    augment_id=c.augment.augment_id,
                    name=c.augment.name,
                    rarity=c.augment.rarity,
                    hero_win_rate=hero_wr,
                    wins=st.wins if st else 0,
                    games=st.games if st else 0,
                    low_sample=st.low_sample if st else True,
                    rarity_dist=rdist,
                )
                scored_cards.append((c, sc))

        valid = [(c, sc) for c, sc in scored_cards if sc.games >= 30]
        if len(valid) < 2:
            self._last_smart_gate_eval["reason"] = "low_sample"
            return False, ""
        valid.sort(key=lambda item: item[1].delta, reverse=True)
        top1_card, top1_sc = valid[0]
        top2_card, top2_sc = valid[1]
        delta_gap = top1_sc.delta - top2_sc.delta
        self._last_smart_gate_eval.update(
            {
                "top1_id": top1_card.augment.augment_id,
                "top2_id": top2_card.augment.augment_id,
                "top1_delta": round(top1_sc.delta, 4),
                "top2_delta": round(top2_sc.delta, 4),
                "top1_top2_gap": round(delta_gap, 4),
            }
        )

        # Do not call it a clear winner if even #1 is below rarity par (reroll candidate)
        top1_below_par = bool(top1_sc.is_below_rarity_par) if top1_sc.percentile is not None else (top1_sc.delta < 0.0)
        if top1_below_par:
            self._last_smart_gate_eval["reason"] = "top1_below_rarity_par"
            return False, ""

        is_decisive_gap = delta_gap >= self.SMART_PICKER_GATE_DELTA_GAP
        is_decisive_top1 = top1_sc.delta >= self.SMART_PICKER_GATE_DECISIVE_DELTA
        if not is_decisive_gap and not is_decisive_top1:
            self._last_smart_gate_eval["reason"] = "gap_below_threshold"
            return False, ""

        # Check combo verdict contradiction
        if verdict is None:
            my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
            offered_ids = tuple(c.augment.augment_id for c in cards)
            cached_v = self._last_combo_verdict
            if cached_v is not None and tuple(vc.augment_id for vc in cached_v.cards) != offered_ids:
                cached_v = None
            band_mode = self._current_data_band_mode()
            my_lvl = (
                int(getattr(self._last_live.my_player, "level", 0) or 0)
                if (getattr(self, "_last_live", None) and self._last_live.my_player)
                else 0
            )
            my_gold = float(getattr(self._last_live, "current_gold", 0.0) or 0.0) if getattr(self, "_last_live", None) else 0.0
            pref_prof = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
            if cached_v is not None:
                verdict = cached_v
            else:
                try:
                    verdict = evaluate_picker_combos(
                        store=self.store,
                        hero_id=hid,
                        selected_augments=self.sm.selected_augments,
                        my_items=my_items,
                        offered=[(c.slot_index, c.augment) for c in cards],
                        my_stats=self._current_my_stats(),
                        game_time_s=float(self._current_game_time_s() or 0.0),
                        data_band_mode=band_mode,
                        preferred_profile=pref_prof,
                        my_level=my_lvl,
                        my_gold=my_gold,
                    )
                except TypeError:
                    verdict = evaluate_picker_combos(
                        store=self.store,
                        hero_id=hid,
                        selected_augments=self.sm.selected_augments,
                        my_items=my_items,
                        offered=[(c.slot_index, c.augment) for c in cards],
                        my_stats=self._current_my_stats(),
                        game_time_s=float(self._current_game_time_s() or 0.0),
                    )
        combo_by_slot = {vc.slot_index: vc for vc in verdict.cards}
        top1_combo = combo_by_slot.get(top1_card.slot_index)
        if top1_combo is not None and top1_combo.verdict in ("conflict", "round_decay"):
            self._last_smart_gate_eval["reason"] = "combo_conflict"
            return False, ""
        for vc in verdict.cards:
            if vc.slot_index == top1_card.slot_index:
                continue
            if vc.verdict in ("jackpot", "gamble") and not (top1_combo and top1_combo.verdict == "jackpot"):
                self._last_smart_gate_eval["reason"] = "combo_jackpot_or_gamble"
                return False, ""
        if (
            verdict.best_slot_index >= 0
            and verdict.best_slot_index != top1_card.slot_index
            and (alt_vc := combo_by_slot.get(verdict.best_slot_index)) is not None
            and alt_vc.verdict in ("jackpot", "gamble", "synergy")
            and alt_vc.combo_delta >= 2.0
            and (top1_combo is None or alt_vc.combo_delta > top1_combo.combo_delta + 1.0)
        ):
            self._last_smart_gate_eval["reason"] = "combo_override"
            return False, ""

        # Check transformative augment tie (let AI weigh transformative pivot when gap isn't overwhelming)
        band_mode = self._current_data_band_mode()
        top1_tx = is_transformative_augment(
            self.store, top1_card.augment.augment_id, hero_id=hid, band_mode=band_mode
        )
        for other_card, other_sc in scored_cards:
            if other_card.slot_index == top1_card.slot_index:
                continue
            other_tx = (
                is_transformative_augment(
                    self.store, other_card.augment.augment_id, hero_id=hid, band_mode=band_mode
                )
                or is_transmute_augment(other_card.augment.augment_id)
            )
            if other_tx:
                if top1_tx or other_sc.games < 30 or (top1_sc.delta - other_sc.delta) < 0.035:
                    self._last_smart_gate_eval["reason"] = "transformative_tie"
                    return False, ""

        detail = f"{top1_sc.name} delta={top1_sc.delta * 100:+.2f}% gap={delta_gap * 100:+.2f}%"
        self._last_smart_gate_eval["skipped"] = True
        self._last_smart_gate_eval["reason"] = "decisive_local_winner"
        return True, detail

    def _cancel_pending_icon_verify(self) -> None:
        if self._icon_verify_timer.isActive():
            self._icon_verify_timer.stop()
        self._pending_icon_verify_candidates = ()
        self._pending_icon_verify_committed_id = ""
        self._pending_icon_verify_slot_hint = None

    def _schedule_delayed_icon_verify(self, committed_id: str, pick_confidence: float) -> None:
        if self.icon_matcher is None or not self.sm.last_offered_cards:
            return
        cand_ids = tuple(
            c.augment.augment_id
            for c in self.sm.last_offered_cards
            if c.augment.augment_id not in ("1402", "1403", "1404")
        )
        if not cand_ids:
            return
        if committed_id:
            slot_hint = max(0, min(3, len(self.sm.selected_augments) - 1))
        else:
            slot_hint = max(0, min(3, len(self.sm.selected_augments)))
        self._pending_icon_verify_candidates = cand_ids
        self._pending_icon_verify_committed_id = committed_id
        self._pending_icon_verify_slot_hint = slot_hint
        logger.info(
            "Scheduling %.2fs HUD icon verification (conf=%.2f, committed=%s, candidates=%s, slot_hint=%s)",
            self.ICON_VERIFY_DELAY_MS / 1000.0,
            pick_confidence,
            committed_id or "none",
            cand_ids,
            slot_hint,
        )
        self._icon_verify_timer.start(self.ICON_VERIFY_DELAY_MS)

    def _on_delayed_icon_verify(self) -> None:
        candidates = self._pending_icon_verify_candidates
        committed_id = self._pending_icon_verify_committed_id
        slot_hint = self._pending_icon_verify_slot_hint
        self._pending_icon_verify_candidates = ()
        self._pending_icon_verify_committed_id = ""
        self._pending_icon_verify_slot_hint = None
        if self.sm.state != GameState.IN_GAME or not candidates or self.icon_matcher is None:
            return
        if self.icon_matcher.template_count == 0:
            self._reload_icon_matcher_templates()
        if self.icon_matcher.template_count == 0:
            if getattr(self, "_devlab_active", False):
                g_rect = find_game_window()
                res_str = f"{g_rect.width}x{g_rect.height}" if g_rect is not None else ""
                self._emit_devlab(
                    "vision",
                    "vision.icon_verify.outcome",
                    {
                        "template_count": 0,
                        "outcome": "skipped_empty_bank",
                        "committed_id": committed_id,
                        "candidate_ids": list(candidates),
                        "slot_index_hint": slot_hint,
                        "resolution": res_str,
                        "action_taken": "none",
                    },
                )
            return
        game_rect = find_game_window()
        if game_rect is None:
            return
        frame_bgr: np.ndarray | None = None
        worker = getattr(self, "_vision_worker", None)
        if (
            worker is not None
            and worker.is_running()
            and not getattr(self, "_is_mocked_vision", False)
            and getattr(worker, "_last_frame_bgr", None) is not None
            and (time.monotonic() - float(getattr(worker, "_last_full_tick_mono", 0.0) or 0.0)) <= 0.6
        ):
            frame_bgr = worker._last_frame_bgr
            if getattr(worker, "_last_game_rect", None) is not None:
                game_rect = worker._last_game_rect
        if frame_bgr is None:
            cap = self.capturer.grab(region=game_rect.as_bbox())
            if cap.is_black or cap.frame_bgr is None:
                return
            frame_bgr = cap.frame_bgr
        self._verify_picked_augment_from_frame(
            frame_bgr,
            game_rect=game_rect,
            candidate_ids=candidates,
            committed_id=committed_id,
            slot_index_hint=slot_hint,
        )

    def _emit_icon_verify_devlab(
        self,
        frame_bgr: np.ndarray | None,
        game_rect: Any,
        *,
        committed_id: str,
        candidate_ids: tuple[str, ...],
        slot_index_hint: int | None,
        action_taken: str,
        hit: IconMatchResult | None,
    ) -> None:
        if not getattr(self, "_devlab_active", False) or self.icon_matcher is None:
            return
        diag_fn = getattr(self.icon_matcher, "last_verify_diagnostics", None)
        vdiag = dict(diag_fn()) if callable(diag_fn) else {}
        wide_crop = vdiag.pop("hud_wide_diag_crop", None)
        vdiag.setdefault("template_count", int(getattr(self.icon_matcher, "template_count", 0) or 0))
        if vdiag["template_count"] == 0:
            vdiag.setdefault("outcome", "skipped_empty_bank")
        elif hit is not None:
            vdiag.setdefault("outcome", "matched")
        else:
            vdiag.setdefault("outcome", "below_threshold")
        if game_rect is not None:
            vdiag["resolution"] = f"{int(game_rect.width)}x{int(game_rect.height)}"
            vdiag["game_rect"] = [int(game_rect.left), int(game_rect.top), int(game_rect.width), int(game_rect.height)]
        vdiag["committed_id"] = committed_id
        vdiag["candidate_ids"] = list(candidate_ids)
        vdiag["slot_index_hint"] = slot_index_hint
        vdiag["action_taken"] = action_taken
        hud_scale = float(getattr(self, "_hud_scale", 1.0) or 1.0)
        vdiag.setdefault("hud_scale", round(hud_scale, 3))
        ev_seq = self._emit_devlab("vision", "vision.icon_verify.outcome", vdiag)

        if (
            bool(vdiag.get("hud_strip_blind"))
            and wide_crop is not None
            and not getattr(self, "_hud_wide_diag_saved_this_match", False)
            and getattr(self._devlab, "enable_roi_snapshots", False)
        ):
            try:
                enq_res = self._devlab.enqueue_roi_snapshot("hud_wide_diag", wide_crop, ev_seq=ev_seq)
                if enq_res is not None:
                    self._hud_wide_diag_saved_this_match = True
            except Exception:
                pass

        # Capture compact 4x28x28 HUD strip crop on anomaly / low-margin / empty bank / flat patches
        outcome = str(vdiag.get("outcome") or "")
        margin = float(vdiag.get("margin") if vdiag.get("margin") is not None else 1.0)
        if (
            frame_bgr is not None
            and getattr(self._devlab, "enable_roi_snapshots", False)
            and (outcome != "matched" or margin < 0.08 or action_taken in ("corrected", "recovered"))
        ):
            strip_fn = getattr(self.icon_matcher, "build_hud_icon_strip_crop", None)
            if callable(strip_fn):
                try:
                    try:
                        strip = strip_fn(frame_bgr, game_rect, hud_scale=hud_scale)
                    except TypeError:
                        strip = strip_fn(frame_bgr, game_rect)
                    if strip is not None:
                        self._devlab.enqueue_roi_snapshot("hud_icon_strip", strip, ev_seq=ev_seq)
                except Exception:
                    pass

    def _verify_picked_augment_from_frame(
        self,
        frame_bgr: np.ndarray,
        *,
        game_rect,
        candidate_ids: tuple[str, ...],
        committed_id: str,
        slot_index_hint: int | None = None,
    ) -> IconMatchResult | None:
        if self.icon_matcher is None:
            return None
        if self.icon_matcher.template_count == 0:
            self._emit_icon_verify_devlab(
                frame_bgr,
                game_rect,
                committed_id=committed_id,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_index_hint,
                action_taken="none",
                hit=None,
            )
            return None
        hud_scale = float(getattr(self, "_hud_scale", 1.0) or 1.0)
        try:
            hit = self.icon_matcher.verify_picked_augment(
                frame_bgr,
                game_rect,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_index_hint,
                hud_scale=hud_scale,
            )
        except TypeError:
            hit = self.icon_matcher.verify_picked_augment(
                frame_bgr,
                game_rect,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_index_hint,
            )
        if hit is None:
            self._emit_icon_verify_devlab(
                frame_bgr,
                game_rect,
                committed_id=committed_id,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_index_hint,
                action_taken="none",
                hit=None,
            )
            return None
        if committed_id:
            if hit.augment_id == committed_id:
                logger.info(
                    "HUD icon matcher confirmed committed augment: %s (score=%.3f, slot=%d)",
                    hit.augment_id,
                    hit.score,
                    hit.hud_slot_index,
                )
                self._emit_icon_verify_devlab(
                    frame_bgr,
                    game_rect,
                    committed_id=committed_id,
                    candidate_ids=candidate_ids,
                    slot_index_hint=slot_index_hint,
                    action_taken="confirmed",
                    hit=hit,
                )
                return hit
            # Correct low-confidence mis-pick in-place
            if (
                self.sm.selected_augments
                and self.sm.selected_augments[-1] == committed_id
                and self.sm.replace_last_augment(hit.augment_id)
            ):
                logger.info(
                    "HUD icon matcher corrected low-conf pick: %s -> %s (score=%.3f, slot=%d)",
                    committed_id,
                    hit.augment_id,
                    hit.score,
                    hit.hud_slot_index,
                )
                self._emit_icon_verify_devlab(
                    frame_bgr,
                    game_rect,
                    committed_id=committed_id,
                    candidate_ids=candidate_ids,
                    slot_index_hint=slot_index_hint,
                    action_taken="corrected",
                    hit=hit,
                )
                self._rollback_plan_and_advice_for_augment_correction()
                if is_transmute_augment(hit.augment_id):
                    fading = tuple(c.augment.augment_id for c in self.sm.last_offered_cards if c.augment.augment_id != hit.augment_id)
                    self.sm.start_transmute_if_applicable(hit.augment_id, now_mono=time.monotonic(), fading_ids=fading)
                    if self.transmute_watcher is not None:
                        self.transmute_watcher.reset()
                    self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
                else:
                    self._flag_if_transformative_augment(hit.augment_id)
                self._save_match_state()
                self._refresh_drawer_view()
                self.edge_drawer.flash_new_augment(hit.augment_id)
                self.edge_drawer.set_recent_offered(
                    [c.augment for c in self.sm.last_offered_cards],
                    auto_committed_id=hit.augment_id,
                    confidence=float(hit.score),
                )
                if not is_transmute_augment(hit.augment_id) and self.cfg.auto_trigger_coach and self._is_any_coach_online():
                    self.trigger_coach_request("augment_selected")
            else:
                self._emit_icon_verify_devlab(
                    frame_bgr,
                    game_rect,
                    committed_id=committed_id,
                    candidate_ids=candidate_ids,
                    slot_index_hint=slot_index_hint,
                    action_taken="none",
                    hit=hit,
                )
            return hit

        # ClickTracker returned None -> recover missed augment directly from HUD icon!
        if hit.augment_id not in ("1402", "1403", "1404") and self.sm.add_selected_augment(hit.augment_id):
            logger.info(
                "HUD icon matcher recovered missed augment pick: %s (score=%.3f, slot=%d)",
                hit.augment_id,
                hit.score,
                hit.hud_slot_index,
            )
            self._emit_icon_verify_devlab(
                frame_bgr,
                game_rect,
                committed_id=committed_id,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_index_hint,
                action_taken="recovered",
                hit=hit,
            )
            if is_transmute_augment(hit.augment_id):
                fading = tuple(c.augment.augment_id for c in self.sm.last_offered_cards if c.augment.augment_id != hit.augment_id)
                self.sm.start_transmute_if_applicable(hit.augment_id, now_mono=time.monotonic(), fading_ids=fading)
                if self.transmute_watcher is not None:
                    self.transmute_watcher.reset()
                self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
            else:
                self._flag_if_transformative_augment(hit.augment_id)
            self._save_match_state()
            self._refresh_drawer_view()
            self.edge_drawer.flash_new_augment(hit.augment_id)
            self.edge_drawer.set_recent_offered(
                [c.augment for c in self.sm.last_offered_cards],
                auto_committed_id=hit.augment_id,
                confidence=float(hit.score),
            )
            if not is_transmute_augment(hit.augment_id) and self.cfg.auto_trigger_coach and self._is_any_coach_online():
                self.trigger_coach_request("augment_selected")
        else:
            self._emit_icon_verify_devlab(
                frame_bgr,
                game_rect,
                committed_id=committed_id,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_index_hint,
                action_taken="none",
                hit=hit,
            )
        return hit

    def _try_resolve_transmute_via_icon(self, frame_bgr: np.ndarray, game_rect) -> IconMatchResult | None:
        """Fast visual icon-matching channel for resolving pending Transmute augments from HUD slots."""
        pt = self.sm.pending_transmute
        if pt is None or self.icon_matcher is None or self.icon_matcher.template_count == 0:
            return None
        owned_or_excluded = (
            set(NON_TARGET_AUGMENT_IDS)
            | set(self.sm.selected_augments)
            | set(pt.excluded_fading_ids)
            | set(pt.resolved_ids)
        )
        candidate_ids: list[str] = []
        for aug in self.store.all_augments():
            aid = aug.augment_id
            if aid in owned_or_excluded:
                continue
            if pt.target_rarity and aug.rarity != pt.target_rarity:
                continue
            if self.icon_matcher.has_template(aid):
                candidate_ids.append(aid)
        if not candidate_ids:
            return None
        slot_hint: int | None = None
        if pt.source_augment_id in self.sm.selected_augments:
            idx = self.sm.selected_augments.index(pt.source_augment_id)
            if 0 <= idx < 4:
                slot_hint = idx
        hud_scale = float(getattr(self, "_hud_scale", 1.0) or 1.0)
        try:
            hit = self.icon_matcher.verify_picked_augment(
                frame_bgr,
                game_rect,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_hint,
                min_score=0.68,
                hud_scale=hud_scale,
            )
        except TypeError:
            hit = self.icon_matcher.verify_picked_augment(
                frame_bgr,
                game_rect,
                candidate_ids=candidate_ids,
                slot_index_hint=slot_hint,
                min_score=0.68,
            )
        if hit is not None:
            self._on_resolve_transmute_augment(hit.augment_id, source="hud_icon")
        return hit

    def build_match_telemetry_record(self, won: int = -1) -> dict[str, Any] | None:
        """Assemble strict zero-PII MatchTelemetryRequest payload if match lasted >= 180s."""
        duration_s = int(round(self._match_max_game_time_s))
        if duration_s < self.TELEMETRY_MIN_DURATION_S:
            return None
        hid = self._active_hero_id() or self._match_last_hero_id
        hero = self.store.get_hero(hid) if hid else None
        hero_name = (hero.name if hero else self._match_last_hero_name) or ""
        patch_str = self.store.descriptor.patch if self.store.descriptor else ""
        mode_str = self.store.active_match_mode or (self.store.descriptor.mode if self.store.descriptor else "normal")

        active_det = self.detector
        if active_det is None and getattr(self, "_vision_worker", None) is not None:
            active_det = getattr(self._vision_worker, "_detector", None)
        det_metrics = active_det.get_match_metrics() if active_det is not None else {}
        ocr_missed = max(
            0,
            int(det_metrics.get("partial_open_count", 0)) - int(det_metrics.get("recovered_slot_count", 0)),
        )
        adopted = sum(
            1
            for rec_id in self.ai_recommended_item_ids_shown
            if item_is_owned(rec_id, self._match_last_my_items)
        )
        raw_key = self._active_match_key or f"{hid}:{duration_s}:{','.join(self.sm.selected_augments)}"
        match_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32] if raw_key else ""
        won_val = int(won) if int(won) in (-1, 0, 1) else -1
        return {
            "match_key": match_key_hash,
            "hero_id": str(hid or "")[:32],
            "hero_name": str(hero_name)[:64],
            "dataset_patch": str(patch_str)[:32],
            "dataset_mode": str(mode_str)[:32],
            "is_first_prismatic": bool(self._match_is_first_prismatic or mode_str == "first-prismatic"),
            "selected_augments": [str(a) for a in self.sm.selected_augments[:12]],
            "ocr_missed_cards": min(100, max(0, ocr_missed)),
            "user_swapped_count": min(100, max(0, int(self.user_swapped_this_match))),
            "ai_calls_count": min(500, max(0, int(self.ai_calls_this_match))),
            "ai_recs_shown": min(500, max(0, len(self.ai_recommended_item_ids_shown))),
            "ai_recs_adopted": min(500, max(0, int(adopted))),
            "won": won_val,
            "duration_s": min(14400, max(0, duration_s)),
            "client_version": str(APP_VERSION)[:32],
        }

    def _maybe_submit_match_telemetry(self, won: int = -1) -> bool:
        """Fire non-blocking daemon thread submitting anonymous post-match telemetry (never raises)."""
        if not self.cfg.telemetry_enabled or self.cloud_client is None:
            return False
        record = self.build_match_telemetry_record(won=won)
        if record is None:
            return False
        mkey = record.get("match_key", "")
        if mkey and mkey == self._match_telemetry_sent_key:
            return False
        self._match_telemetry_sent_key = mkey
        client = self.cloud_client

        def _worker() -> None:
            try:
                client.submit_match_telemetry(record)
            except Exception as exc:
                logger.debug("Match telemetry submit failed (ignored): %s", exc)

        threading.Thread(target=_worker, name="HexMatchTelemetry", daemon=True).start()
        return True

    def _update_best_ai_highlight(self, advice: CoachAdvicePayload) -> None:
        """Capture the strongest AI tactical callout or counter-item reason for post-match recap."""
        cand = (advice.tactical_summary or "").strip()
        counter_kws = ("重伤", "破盾", "水银", "金身", "魔抗", "护甲", "穿透", "反制", "变阵")
        if not cand and advice.next_items:
            for it in advice.next_items:
                r_txt = (it.reason or "").strip()
                if it.name and r_txt:
                    cand = f"关键推荐「{it.name}」：{r_txt}"
                    break
        elif advice.next_items and len(cand) < 12:
            for it in advice.next_items[:2]:
                r_txt = (it.reason or "").strip()
                if it.name and r_txt and any(kw in r_txt for kw in counter_kws):
                    cand = f"关键对策「{it.name}」：{r_txt}"
                    break
        if not cand and advice.opening_brief:
            parts = [
                (advice.opening_brief.get("enemy_threat") or "").strip(),
                (advice.opening_brief.get("opening_buy") or "").strip(),
            ]
            cand = " · ".join(p for p in parts if p)
        if cand:
            if not self._best_ai_highlight or len(cand) >= int(len(self._best_ai_highlight) * 0.75):
                self._best_ai_highlight = cand

    def _resolve_local_cached_icon(self, kind: str, *keys: str) -> str:
        """Return local cached icon file path if already on disk (>64 bytes), otherwise ''."""
        static = getattr(self.store, "static", None)
        if static is None or getattr(static, "icons_dir", None) is None:
            return ""
        icons_dir = Path(static.icons_dir) / kind
        seen: set[str] = set()
        for raw_key in keys:
            safe_key = "".join(ch for ch in str(raw_key or "") if ch.isalnum() or ch in ("_", "-"))
            if not safe_key or safe_key in seen:
                continue
            seen.add(safe_key)
            p = icons_dir / f"{safe_key}.png"
            try:
                if p.is_file() and p.stat().st_size > 64:
                    return str(p)
            except OSError:
                continue
        return ""

    @staticmethod
    def _parse_kda_triple(kda_str: str) -> tuple[int, int, int] | None:
        parts = [p.strip() for p in str(kda_str or "").split("/")]
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return int(parts[0]), int(parts[1]), int(parts[2])
        return None

    def build_match_recap_data(
        self,
        won: int = -1,
        eog_snap: dict[str, Any] | None = None,
    ) -> MatchRecapData | None:
        """Assemble offline MatchRecapData v2 from live match state + EOG snapshot when duration >= 180s."""
        duration_s = int(round(self._match_max_game_time_s))
        if isinstance(eog_snap, dict):
            eog_dur = int(eog_snap.get("duration_s") or 0)
            if eog_dur > duration_s:
                duration_s = eog_dur
        if duration_s < self.TELEMETRY_MIN_DURATION_S:
            return None
        hid = self._active_hero_id() or self._match_last_hero_id
        hero = self.store.get_hero(hid) if hid else None
        hero_name = (hero.name if hero else self._match_last_hero_name) or str(hid or "")
        if not hid and not hero_name:
            return None

        hero_alias = self.store.static.get_champion_alias(hid) if (hid and getattr(self.store, "static", None)) else ""
        hero_icon_path = self._resolve_local_cached_icon("champion", hid or "", hero_alias)

        augments: list[tuple[str, str]] = []
        augment_icon_paths: list[str] = []
        for aid in self.sm.selected_augments[:MAX_AUGMENT_SLOTS]:
            aug = self.store.get_augment(aid)
            augments.append((aug.name if aug else str(aid), aug.rarity if aug else "gold"))
            augment_icon_paths.append(self._resolve_local_cached_icon("augment", str(aid)))

        combo_plan: ComboPlan | None = None
        if self.store.is_loaded and hid:
            band_mode = self._current_data_band_mode()
            ally_profiles, enemy_profiles = self._current_team_profiles(band_mode=band_mode)
            try:
                fresh_plan = build_combo_plan(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=self._match_last_my_items,
                    limit=4,
                    threat=getattr(self, "_last_threat", None),
                    ally_profiles=ally_profiles,
                    enemy_profiles=enemy_profiles,
                    my_stats=self._current_my_stats(),
                    game_time_s=float(duration_s),
                    data_band_mode=band_mode,
                )
            except TypeError:
                fresh_plan = build_combo_plan(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=self._match_last_my_items,
                    limit=4,
                    threat=getattr(self, "_last_threat", None),
                    ally_profiles=ally_profiles,
                    enemy_profiles=enemy_profiles,
                    my_stats=self._current_my_stats(),
                    game_time_s=float(duration_s),
                )
            if fresh_plan.active_combos:
                combo_plan = fresh_plan
            elif self._last_combo_plan is not None and self._last_combo_plan.active_combos:
                combo_plan = self._last_combo_plan
            else:
                combo_plan = fresh_plan
        else:
            combo_plan = self._last_combo_plan

        combo_title = ""
        combo_completed = False
        if combo_plan is not None:
            completed_c = next((c for c in combo_plan.active_combos if c.completed), None)
            if completed_c is not None:
                combo_title = completed_c.name
                combo_completed = True
            elif combo_plan.active_combos:
                combo_title = combo_plan.active_combos[0].name
            elif combo_plan.profile.primary_profile not in ("", "BALANCED") and combo_plan.profile.direction_label:
                combo_title = combo_plan.profile.direction_label

        eog_items = tuple(eog_snap.get("item_ids") or ()) if isinstance(eog_snap, dict) else ()
        src_items = eog_items if eog_items else self._match_last_my_items
        item_entries: list[tuple[str, str]] = []
        kept_item_ids: list[str] = []
        for raw_iid in src_items:
            raw_s = str(raw_iid or "").strip()
            if not raw_s:
                continue
            norm_s = normalize_item_id(raw_s)
            if raw_s in TRINKET_AND_FREE_ITEM_IDS or norm_s in TRINKET_AND_FREE_ITEM_IDS:
                continue
            info = self.store.get_item_info(raw_s)
            if info.category == "consumable":
                continue
            ico = self._resolve_local_cached_icon("item", raw_s, norm_s)
            item_entries.append((info.name, ico))
            kept_item_ids.append(raw_s)
            if len(item_entries) >= 6:
                break

        ai_hl = (self._best_ai_highlight or "").strip()
        if not ai_hl and self._latest_advice is not None:
            ai_hl = (self._latest_advice.tactical_summary or "").strip()
            if not ai_hl and self._latest_advice.next_items:
                first_it = self._latest_advice.next_items[0]
                if first_it.name and first_it.reason:
                    ai_hl = f"关键推荐「{first_it.name}」：{first_it.reason.strip()}"

        patch_disp = (
            format_display_patch(self.store.descriptor.patch).split("（")[0]
            if self.store.descriptor
            else "26.23"
        )
        won_val = int(won) if int(won) in (-1, 0, 1) else -1
        if won_val not in (0, 1) and isinstance(eog_snap, dict) and int(eog_snap.get("won", -1)) in (0, 1):
            won_val = int(eog_snap["won"])

        kda_str = str(self._last_kda or "")
        team_kills_str = str(self._last_team_kills or "")
        final_stats = normalize_final_stats(getattr(self, "_match_last_my_stats", None))

        damage_dealt = 0
        damage_share = 0.0
        damage_taken = 0
        damage_taken_share = 0.0
        heal_and_shield = 0
        gold_earned = 0
        kill_participation = 0.0
        mvp_badge = ""

        if isinstance(eog_snap, dict):
            if eog_snap.get("kda"):
                kda_str = str(eog_snap["kda"])
            damage_dealt = max(0, int(eog_snap.get("total_damage_dealt_to_champions") or 0))
            damage_taken = max(0, int(eog_snap.get("total_damage_taken") or 0))
            heal_and_shield = max(0, int(eog_snap.get("total_heal") or 0)) + max(
                0, int(eog_snap.get("total_damage_shielded_on_teammates") or 0)
            )
            gold_earned = max(0, int(eog_snap.get("gold_earned") or 0))

            all_players = eog_snap.get("all_players_final")
            if isinstance(all_players, list) and all_players:
                my_row = next((p for p in all_players if isinstance(p, dict) and p.get("is_me")), None)
                if my_row is None and hid:
                    my_row = next(
                        (p for p in all_players if isinstance(p, dict) and str(p.get("hero_id") or "") == str(hid)),
                        None,
                    )
                if isinstance(my_row, dict):
                    if damage_dealt <= 0:
                        damage_dealt = max(0, int(my_row.get("damage_to_champions") or 0))
                    if damage_taken <= 0:
                        damage_taken = max(0, int(my_row.get("damage_taken") or 0))
                    if gold_earned <= 0:
                        gold_earned = max(0, int(my_row.get("gold_earned") or 0))
                    if not kda_str and my_row.get("kda"):
                        kda_str = str(my_row["kda"])
                my_team_id = int(my_row.get("team_id") or 0) if isinstance(my_row, dict) else 0
                ally_rows = [
                    p
                    for p in all_players
                    if isinstance(p, dict)
                    and (bool(p.get("is_ally")) or (my_team_id > 0 and int(p.get("team_id") or 0) == my_team_id))
                ]
                enemy_rows = [
                    p
                    for p in all_players
                    if isinstance(p, dict) and p not in ally_rows
                ]
                ally_dmg_total = sum(max(0, int(p.get("damage_to_champions") or 0)) for p in ally_rows)
                ally_taken_total = sum(max(0, int(p.get("damage_taken") or 0)) for p in ally_rows)
                ally_kills_total = sum(max(0, int(p.get("kills") or 0)) for p in ally_rows)
                enemy_kills_total = sum(max(0, int(p.get("kills") or 0)) for p in enemy_rows)
                if ally_dmg_total > 0 and damage_dealt > 0:
                    damage_share = min(1.0, damage_dealt / float(ally_dmg_total))
                if ally_taken_total > 0 and damage_taken > 0:
                    damage_taken_share = min(1.0, damage_taken / float(ally_taken_total))
                if not team_kills_str and (ally_kills_total > 0 or enemy_kills_total > 0):
                    team_kills_str = f"{ally_kills_total}:{enemy_kills_total}"
                kda_t = self._parse_kda_triple(kda_str)
                if kda_t is not None and ally_kills_total > 0:
                    kill_participation = min(1.0, (kda_t[0] + kda_t[2]) / float(ally_kills_total))

        if kill_participation <= 0.0 and kda_str and team_kills_str:
            kda_t = self._parse_kda_triple(kda_str)
            tk_parts = [p.strip() for p in team_kills_str.replace(" ", "").split(":")]
            if kda_t is not None and len(tk_parts) == 2 and tk_parts[0].isdigit() and int(tk_parts[0]) > 0:
                kill_participation = min(1.0, (kda_t[0] + kda_t[2]) / float(int(tk_parts[0])))

        gpm = (gold_earned / (duration_s / 60.0)) if (gold_earned > 0 and duration_s > 0) else 0.0
        match_score, rating_grade, mvp_badge = compute_match_rating(
            kda=kda_str,
            won=won_val,
            damage_share=damage_share,
            damage_taken_share=damage_taken_share,
            kp=kill_participation,
            gold_per_min=gpm,
        )

        # Refine MVP/SVP against full ally team scores when all_players_final is available
        if isinstance(eog_snap, dict) and isinstance(eog_snap.get("all_players_final"), list):
            all_p = eog_snap["all_players_final"]
            my_r = next((p for p in all_p if isinstance(p, dict) and p.get("is_me")), None)
            my_tid = int(my_r.get("team_id") or 0) if isinstance(my_r, dict) else 0
            allies_p = [
                p
                for p in all_p
                if isinstance(p, dict)
                and (bool(p.get("is_ally")) or (my_tid > 0 and int(p.get("team_id") or 0) == my_tid))
            ]
            if len(allies_p) >= 2 and match_score > 0.0:
                t_dmg = sum(max(0, int(p.get("damage_to_champions") or 0)) for p in allies_p)
                t_tkn = sum(max(0, int(p.get("damage_taken") or 0)) for p in allies_p)
                t_kls = sum(max(0, int(p.get("kills") or 0)) for p in allies_p)
                ally_scores: list[float] = []
                for ap in allies_p:
                    ap_k = max(0, int(ap.get("kills") or 0))
                    ap_d = max(0, int(ap.get("deaths") or 0))
                    ap_a = max(0, int(ap.get("assists") or 0))
                    ap_kda = str(ap.get("kda") or f"{ap_k}/{ap_d}/{ap_a}")
                    ap_ds = (max(0, int(ap.get("damage_to_champions") or 0)) / float(t_dmg)) if t_dmg > 0 else 0.0
                    ap_ts = (max(0, int(ap.get("damage_taken") or 0)) / float(t_tkn)) if t_tkn > 0 else 0.0
                    ap_kp = min(1.0, (ap_k + ap_a) / float(t_kls)) if t_kls > 0 else 0.0
                    ap_gpm = (
                        (max(0, int(ap.get("gold_earned") or 0)) / (duration_s / 60.0))
                        if duration_s > 0
                        else 0.0
                    )
                    sc_val, _, _ = compute_match_rating(
                        kda=ap_kda,
                        won=won_val,
                        damage_share=ap_ds,
                        damage_taken_share=ap_ts,
                        kp=ap_kp,
                        gold_per_min=ap_gpm,
                    )
                    ally_scores.append(sc_val)
                top_ally_score = max(ally_scores) if ally_scores else 0.0
                if match_score >= top_ally_score - 0.05 and match_score >= 9.5:
                    mvp_badge = "MVP" if won_val == 1 else ("SVP" if won_val == 0 else "")
                elif top_ally_score > match_score + 0.05:
                    mvp_badge = ""

        skin_id = int(getattr(self, "_match_last_skin_id", 0) or 0)
        skin_name = str(getattr(self, "_match_last_skin_name", "") or "").strip()
        skin_splash_path = ""
        skin_loading_path = ""
        skin_center_path = ""
        static_mgr = getattr(self.store, "static", None)
        resolve_skin_fn = getattr(static_mgr, "resolve_cached_skin_splash", None) if static_mgr is not None else None
        if hid and callable(resolve_skin_fn):
            try:
                skin_splash_path, resolved_skin_name = resolve_skin_fn(hid, skin_id)
                if not skin_name and resolved_skin_name:
                    skin_name = str(resolved_skin_name)
            except Exception:
                pass
        resolve_vars_fn = getattr(static_mgr, "resolve_cached_skin_variants", None) if static_mgr is not None else None
        if hid and callable(resolve_vars_fn):
            try:
                skin_loading_path, skin_center_path = resolve_vars_fn(hid, skin_id)
            except Exception:
                pass

        return MatchRecapData(
            hero_id=str(hid or ""),
            hero_name=hero_name,
            hero_icon_path=hero_icon_path,
            won=won_val,
            kda=kda_str,
            duration_s=duration_s,
            team_kills=team_kills_str,
            is_first_prismatic=bool(
                self._match_is_first_prismatic or self.store.active_match_mode == "first-prismatic"
            ),
            augments=tuple(augments),
            combo_title=combo_title,
            combo_completed=combo_completed,
            item_entries=tuple(item_entries),
            item_ids=tuple(kept_item_ids),
            ai_highlight=ai_hl,
            dataset_patch=patch_disp or "26.23",
            augment_icon_paths=tuple(augment_icon_paths),
            final_stats=final_stats,
            damage_dealt=damage_dealt,
            damage_share=round(damage_share, 4),
            damage_taken=damage_taken,
            heal_and_shield=heal_and_shield,
            gold_earned=gold_earned,
            kill_participation=round(kill_participation, 4),
            match_score=match_score,
            rating_grade=rating_grade,
            mvp_badge=mvp_badge,
            skin_id=skin_id,
            skin_name=skin_name,
            skin_splash_path=skin_splash_path,
            skin_loading_path=skin_loading_path,
            skin_center_path=skin_center_path,
            hero_alias=hero_alias,
        )

    def _prefetch_my_item_icons(self, item_ids: tuple[str, ...]) -> None:
        """Silently cache equipped item icons (including components like 3114) during live matches."""
        static = getattr(self.store, "static", None)
        if static is None or not item_ids:
            return
        to_fetch: list[str] = []
        for raw_iid in item_ids:
            raw_s = str(raw_iid or "").strip()
            if not raw_s:
                continue
            norm_s = normalize_item_id(raw_s)
            if raw_s in TRINKET_AND_FREE_ITEM_IDS or norm_s in TRINKET_AND_FREE_ITEM_IDS:
                continue
            if self._resolve_local_cached_icon("item", raw_s, norm_s):
                continue
            key = ("item", raw_s)
            if key in self._recap_icon_fetching:
                continue
            self._recap_icon_fetching.add(key)
            to_fetch.append(raw_s)
        if not to_fetch:
            return

        def _worker() -> None:
            for raw_s in to_fetch:
                try:
                    norm_s = normalize_item_id(raw_s)
                    p = static.ensure_icon("item", raw_s)
                    if p is None and norm_s and norm_s != raw_s:
                        static.ensure_icon("item", norm_s)
                except Exception:
                    pass
                finally:
                    self._recap_icon_fetching.discard(("item", raw_s))

        threading.Thread(target=_worker, name="HexItemIconPrefetch", daemon=True).start()

    def _hydrate_recap_icons_async(self, recap: MatchRecapData) -> None:
        """Download any missing hero/item/skin assets for a recap card and refresh open dialog live."""
        static = getattr(self.store, "static", None)
        if static is None:
            return
        allow_variants = self._should_fetch_skin_variants(static)
        need_hero = bool(recap.hero_id) and not (recap.hero_icon_path and Path(recap.hero_icon_path).is_file())
        need_skin = bool(recap.hero_id) and not (recap.skin_splash_path and Path(recap.skin_splash_path).is_file())
        need_skin_loading = (
            allow_variants
            and bool(recap.hero_id)
            and callable(getattr(static, "ensure_skin_loading", None))
            and not (recap.skin_loading_path and Path(recap.skin_loading_path).is_file())
        )
        need_skin_center = (
            allow_variants
            and bool(recap.hero_id)
            and callable(getattr(static, "ensure_skin_center", None))
            and not (recap.skin_center_path and Path(recap.skin_center_path).is_file())
        )
        missing_item_indices: list[tuple[int, str]] = []
        for idx, (_name, ico_path) in enumerate(recap.item_entries):
            if ico_path and Path(ico_path).is_file():
                continue
            if idx < len(recap.item_ids) and recap.item_ids[idx]:
                missing_item_indices.append((idx, recap.item_ids[idx]))
        if not need_hero and not need_skin and not need_skin_loading and not need_skin_center and not missing_item_indices:
            return

        def _worker() -> None:
            changed = False
            new_hero_path = recap.hero_icon_path
            if need_hero and recap.hero_id:
                try:
                    p = static.ensure_icon("champion", recap.hero_id)
                    if p is not None and p.is_file():
                        new_hero_path = str(p)
                        changed = True
                except Exception:
                    pass

            new_skin_path = recap.skin_splash_path
            new_skin_name = recap.skin_name
            skin_arg = int(getattr(recap, "skin_id", 0) or 0)
            if need_skin and recap.hero_id and hasattr(static, "ensure_skin_splash"):
                try:
                    p_skin, fetched_sname = static.ensure_skin_splash(recap.hero_id, skin_arg)
                    if p_skin is not None and p_skin.is_file():
                        new_skin_path = str(p_skin)
                        changed = True
                    if fetched_sname and not new_skin_name:
                        new_skin_name = str(fetched_sname)
                        changed = True
                except Exception:
                    pass

            new_loading_path = recap.skin_loading_path
            if need_skin_loading and recap.hero_id:
                try:
                    p_load = static.ensure_skin_loading(recap.hero_id, skin_arg)
                    if p_load is not None and p_load.is_file():
                        new_loading_path = str(p_load)
                        changed = True
                except Exception:
                    pass

            new_center_path = recap.skin_center_path
            if need_skin_center and recap.hero_id:
                try:
                    p_cent = static.ensure_skin_center(recap.hero_id, skin_arg)
                    if p_cent is not None and p_cent.is_file():
                        new_center_path = str(p_cent)
                        changed = True
                except Exception:
                    pass

            new_entries = list(recap.item_entries)
            for idx, raw_s in missing_item_indices:
                try:
                    norm_s = normalize_item_id(raw_s)
                    p = static.ensure_icon("item", raw_s)
                    if (p is None or not p.is_file()) and norm_s and norm_s != raw_s:
                        p = static.ensure_icon("item", norm_s)
                    if p is not None and p.is_file() and idx < len(new_entries):
                        new_entries[idx] = (new_entries[idx][0], str(p))
                        changed = True
                except Exception:
                    pass

            if not changed:
                return

            refreshed = replace(
                recap,
                hero_icon_path=new_hero_path,
                item_entries=tuple(new_entries),
                skin_splash_path=new_skin_path,
                skin_loading_path=new_loading_path,
                skin_center_path=new_center_path,
                skin_name=new_skin_name,
            )

            def _apply_on_ui() -> None:
                if self._last_recap_data is recap:
                    self._last_recap_data = refreshed
                dlg = self._active_recap_dialog
                if dlg is not None and dlg.data is recap:
                    try:
                        dlg.update_data(refreshed)
                    except Exception:
                        pass

            self.ui_invoker.invoke.emit(_apply_on_ui)

        threading.Thread(target=_worker, name="HexRecapIconHydrate", daemon=True).start()

    def _capture_post_match_recap(
        self,
        won: int = -1,
        eog_snap: dict[str, Any] | None = None,
    ) -> MatchRecapData | None:
        """Build & store post-match recap card v2, unlock LookupWindow button, and politely notify tray."""
        recap = self.build_match_recap_data(won=won, eog_snap=eog_snap)
        if recap is None:
            return None
        self._last_recap_data = recap
        self._hydrate_recap_icons_async(recap)
        res_lbl = "胜利" if recap.won == 1 else ("激战" if recap.won == 0 else "对局结束")
        summary_parts = [recap.hero_name, res_lbl]
        if recap.kda:
            summary_parts.append(recap.kda)
        if recap.combo_title:
            summary_parts.append(recap.combo_title)
        summary = " · ".join(p for p in summary_parts if p)
        self.lookup_window.set_recap_available(True, summary)
        if getattr(self, "_devlab_active", False):
            self._emit_devlab(
                "ui",
                "ui_match_recap_interaction",
                {
                    "action": "generated",
                    "ok": True,
                    "won": int(recap.won),
                    "combo_completed": bool(recap.combo_completed),
                    "duration_s": int(recap.duration_s),
                },
            )
        if recap.won == 1 or recap.combo_completed:
            try:
                if self.tray is not None:
                    self.tray.showMessage(
                        "HexHelper · 海克斯战报卡",
                        "🏆 本局战报卡已生成，打开主界面点「战报卡」一键复制发群！",
                        QSystemTrayIcon.MessageIcon.Information,
                        5000,
                    )
            except Exception as exc:
                logger.debug("Tray recap notification skipped: %s", exc)
        return recap

    def _on_recap_template_changed(self, template_id: str) -> None:
        tpl = normalize_recap_template(template_id)
        if self.cfg.recap_template != tpl:
            self.cfg.recap_template = tpl
            try:
                save_config(self.cfg)
            except Exception as exc:
                logger.debug("Failed to persist recap_template=%s: %s", tpl, exc)
        if getattr(self, "_devlab_active", False):
            self._emit_devlab(
                "ui",
                "ui_match_recap_interaction",
                {"action": "template_change", "ok": True, "template": tpl},
            )

    def _open_last_match_recap(self) -> MatchRecapDialog | None:
        """Open the non-blocking Hextech MatchRecapDialog for the most recently finished match."""
        if self._last_recap_data is None:
            return None
        if self._active_recap_dialog is not None:
            try:
                self._active_recap_dialog.close()
                self._active_recap_dialog.deleteLater()
            except Exception:
                pass
            self._active_recap_dialog = None
        devlab_on = bool(getattr(self, "_devlab_active", False))
        dlg = MatchRecapDialog(
            self._last_recap_data,
            parent=self.lookup_window,
            template=self.cfg.recap_template,
            lifecycle_state=(self._core_lifecycle.state if (self._commercial and self._core_lifecycle is not None) else None),
            snoozed=bool(self._core_lifecycle.snoozed if self._core_lifecycle is not None else False),
            on_core_action=self._handle_core_action if self._commercial else None,
            on_recap_action=(self._on_recap_action if devlab_on else None),
            on_template_changed=self._on_recap_template_changed,
        )
        dlg.setModal(False)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._active_recap_dialog = dlg
        self._hydrate_recap_icons_async(self._last_recap_data)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._refresh_non_overlay_hwnds_cache()
        if devlab_on:
            self._emit_devlab("ui", "ui_match_recap_interaction", {"action": "open", "ok": True})
        return dlg

    def _get_lcu_lock(self) -> threading.RLock:
        lock = getattr(self, "_lcu_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._lcu_lock = lock
        return lock

    def _make_lobby_scout_lcu(self) -> tuple[Any, bool]:
        """Return (lcu_handle, should_close) isolating background LobbyScout from shared self.lcu."""
        lock = self._get_lcu_lock()
        with lock:
            lcu = self.lcu
            if type(lcu) is LcuClient and not any(
                name in lcu.__dict__
                for name in (
                    "_get_json",
                    "get_gameflow_session",
                    "get_current_summoner",
                    "get_ranked_stats",
                    "get_match_history",
                )
            ):
                return (
                    LcuClient(
                        preferred_lockfile=lcu.preferred_lockfile,
                        force_all_modes=lcu.force_all_modes,
                    ),
                    True,
                )
            return _ThreadSafeLcuProxy(lcu, lock), False

    def _maybe_start_lobby_scout(self, match_key: str) -> None:
        """Run strictly read-only LCU 10-player LobbyScout once per match in a daemon thread."""
        if not self.cfg.lobby_scout_enabled or not match_key:
            return
        if self._lobby_scout_match_key == match_key:
            return
        self._lobby_scout_match_key = match_key
        self._lobby_scout = None
        self._lobby_scout_in_flight = True
        self.edge_drawer.set_lobby_scout(None)
        scout_lcu, should_close = self._make_lobby_scout_lcu()

        def _worker(mk: str, lcu_handle: Any, close_after: bool) -> None:
            try:
                snap = scout_lobby_readonly(lcu_handle, self.store)
            except Exception as exc:
                logger.debug("Background LobbyScout failed: %s", exc)
                snap = None
            finally:
                if close_after:
                    try:
                        lcu_handle.close()
                    except Exception:
                        pass
            if not self._stop_event.is_set():
                self.ui_invoker.invoke.emit(lambda s=snap, key=mk: self._on_lobby_scout_ready(key, s))

        threading.Thread(
            target=_worker,
            args=(match_key, scout_lcu, should_close),
            name="HexLobbyScout",
            daemon=True,
        ).start()

    def _on_lobby_scout_ready(self, match_key: str, snap: LobbyScoutSnapshot | None) -> None:
        if self._stop_event.is_set():
            return
        if not match_key or match_key != self._active_match_key:
            return
        self._lobby_scout_in_flight = False
        if self.sm.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            self._cancel_game_start_coach_defer("not in game")
            return
        self._lobby_scout = snap
        game_time_s = float(self._last_live.game_time or 0.0) if self._last_live else 0.0
        self.edge_drawer.set_lobby_scout(snap, game_time_s=game_time_s)
        if getattr(self, "_devlab_active", False):
            if snap is not None:
                ally_r = [int(c.hex_rating) for c in snap.my_team]
                enemy_r = [int(c.hex_rating) for c in snap.enemy_team]
                ally_avg = round(sum(ally_r) / len(ally_r), 1) if ally_r else 0.0
                enemy_avg = round(sum(enemy_r) / len(enemy_r), 1) if enemy_r else 0.0
                self._emit_devlab(
                    "game",
                    "lobby_scout_completed",
                    {
                        "ok": True,
                        "ally_avg_rating": ally_avg,
                        "enemy_avg_rating": enemy_avg,
                        "rating_gap": round(ally_avg - enemy_avg, 1),
                        "mvp_rating": int(snap.mvp_card.hex_rating) if snap.mvp_card is not None else 0,
                        "mvp_champion": str(snap.mvp_card.champion_name) if snap.mvp_card is not None else "",
                        "mvp_lead_gap": int(snap.mvp_lead_gap or 0),
                        "top_enemy_rating": int(snap.top_enemy_card.hex_rating) if snap.top_enemy_card is not None else 0,
                        "top_enemy_champion": str(snap.top_enemy_card.champion_name) if snap.top_enemy_card is not None else "",
                        "premade_count": len(snap.premade_notes),
                        "degraded_count": int(snap.degraded_count or 0),
                        "my_team_count": len(snap.my_team),
                        "enemy_team_count": len(snap.enemy_team),
                    },
                    game_time_s=game_time_s,
                )
            else:
                self._emit_devlab(
                    "game",
                    "lobby_scout_completed",
                    {
                        "ok": False,
                        "ally_avg_rating": 0.0,
                        "enemy_avg_rating": 0.0,
                        "rating_gap": 0.0,
                        "premade_count": 0,
                        "degraded_count": 0,
                        "my_team_count": 0,
                        "enemy_team_count": 0,
                    },
                    game_time_s=game_time_s,
                )
        self._flush_pending_game_start_coach(
            match_key,
            source="lobby_scout_ready" if snap is not None else "lobby_scout_empty",
        )

    def _safe_get_lcu_game_id(self) -> tuple[int, int]:
        lcu = getattr(self, "lcu", None)
        fn = getattr(lcu, "get_gameflow_game_id", None) if lcu is not None else None
        if not callable(fn):
            return 0, 0
        try:
            with self._get_lcu_lock():
                res = fn()
            if isinstance(res, tuple) and len(res) >= 2:
                return max(0, int(res[0] or 0)), max(0, int(res[1] or 0))
        except Exception:
            pass
        return 0, 0

    def _safe_get_current_rune_page(self) -> dict[str, Any]:
        lcu = getattr(self, "lcu", None)
        fn = getattr(lcu, "get_current_rune_page", None) if lcu is not None else None
        if not callable(fn):
            return {}
        try:
            with self._get_lcu_lock():
                res = fn()
            if isinstance(res, dict):
                return dict(res)
        except Exception:
            pass
        return {}

    def _fetch_eog_snapshot_safe(self) -> dict[str, Any] | None:
        lcu = getattr(self, "lcu", None)
        if lcu is None:
            return None
        try:
            with self._get_lcu_lock():
                snap_fn = getattr(lcu, "get_eog_snapshot", None)
                if callable(snap_fn):
                    res = snap_fn()
                    if isinstance(res, dict):
                        return dict(res)
                won_fn = getattr(lcu, "get_eog_won", None)
                if callable(won_fn):
                    w = int(won_fn())
                    return {"won": w, "eog_source": "eog_won_legacy"}
        except Exception:
            pass
        return None

    def _fetch_match_history_fallback_safe(self) -> dict[str, Any] | None:
        lcu = getattr(self, "lcu", None)
        fn = getattr(lcu, "get_match_history_eog_fallback", None) if lcu is not None else None
        if not callable(fn):
            return None
        gid = int(getattr(self, "_active_game_id", 0) or 0)
        hid_raw = self._active_hero_id() or getattr(self, "_match_last_hero_id", "") or ""
        try:
            hid_int = int(hid_raw) if str(hid_raw).isdigit() else 0
        except Exception:
            hid_int = 0
        try:
            with self._get_lcu_lock():
                res = fn(game_id=gid, hero_id=hid_int)
            if isinstance(res, dict):
                return dict(res)
        except Exception:
            pass
        return None

    @staticmethod
    def _is_non_mayhem_mode(game_mode: str, queue_id: int) -> bool:
        mode_u = str(game_mode or "").strip().upper()
        if mode_u in _NON_MAYHEM_GAME_MODES:
            return True
        if queue_id > 0 and queue_id not in _MAYHEM_QUEUE_IDS:
            return True
        if mode_u and mode_u not in ("ARAM", "KIWI", "MAYHEM"):
            return True
        return False

    def _start_or_attach_match_session(self, live_snap: LiveMatchSnapshot, resolved_hid: str | None) -> None:
        self._match_session_seq = int(getattr(self, "_match_session_seq", 0) or 0) + 1
        session_seq = self._match_session_seq
        last_fin_gid = int(getattr(self, "_last_finished_game_id", 0) or 0)
        game_id = int(getattr(self, "_last_cs_game_id", 0) or getattr(self, "_active_game_id", 0) or 0)
        if game_id > 0 and game_id == last_fin_gid:
            game_id = 0
        queue_id = int(getattr(self, "_last_cs_queue_id", 0) or getattr(self, "_active_queue_id", 0) or 0)
        self._active_game_id = game_id
        self._active_queue_id = queue_id
        game_mode = str(getattr(live_snap, "game_mode", "") or "").strip()
        self._match_game_mode = game_mode
        self._match_is_non_mayhem = self._is_non_mayhem_mode(game_mode, queue_id)

        roster_key = self._compute_match_key(live_snap, resolved_hid)
        match_key = f"gid:{game_id}" if game_id > 0 else roster_key
        self._active_match_key = match_key
        self._restore_match_state_if_matching(match_key)
        _hero_obj = self.store.get_hero(resolved_hid) if resolved_hid else None
        hero_name = _hero_obj.name if _hero_obj is not None else (
            live_snap.my_player.champion_name if live_snap.my_player else ""
        )
        self.log_ring.begin_match(
            match_key,
            hero_id=resolved_hid or "",
            hero_name=hero_name,
            dataset_patch=self.store.descriptor.patch if self.store.descriptor else "",
            dataset_mode=self.store.active_match_mode if self.store.descriptor else "",
        )
        if getattr(self, "_devlab_active", False):
            raw_hash_src = str(game_id) if game_id > 0 else match_key
            mkh = hashlib.sha256(raw_hash_src.encode("utf-8")).hexdigest()[:16] if raw_hash_src else ""
            self._devlab.begin_match(
                match_key_hash=mkh,
                hero_id=resolved_hid or "",
                hero_name=hero_name,
                dataset_tag=self._current_dataset_tag(),
                game_mode=game_mode,
                is_non_mayhem=self._match_is_non_mayhem,
            )
            if self._action_tracker is not None:
                self._action_tracker.on_lcu_champ_select("InProgress", None)
            self._refresh_devlab_badge_if_visible()
        self._maybe_start_lobby_scout(match_key)

        if getattr(self._stop_event, "is_set", lambda: False)():
            return

        devlab_need_runes = bool(getattr(self, "_devlab_active", False))

        def _async_meta_worker() -> None:
            if getattr(self._stop_event, "is_set", lambda: False)():
                return
            try:
                lcu_gid, lcu_qid = self._safe_get_lcu_game_id()
            except Exception:
                lcu_gid, lcu_qid = 0, 0
            rune_page: dict[str, Any] = {}
            if devlab_need_runes and not getattr(self._stop_event, "is_set", lambda: False)():
                try:
                    rune_page = self._safe_get_current_rune_page() or {}
                except Exception:
                    rune_page = {}
            if getattr(self._stop_event, "is_set", lambda: False)():
                return
            try:
                self.ui_invoker.invoke.emit(
                    lambda s_seq=session_seq, mk=match_key, ls=live_snap, gid=lcu_gid, qid=lcu_qid, rp=rune_page: (
                        self._apply_async_match_session_meta(s_seq, mk, ls, gid, qid, rp)
                    )
                )
            except Exception:
                pass

        threading.Thread(target=_async_meta_worker, name="HexMatchMeta", daemon=True).start()

    def _apply_async_match_session_meta(
        self,
        session_seq: int,
        expected_match_key: str,
        live_snap: LiveMatchSnapshot,
        lcu_game_id: int,
        lcu_queue_id: int,
        rune_page: dict[str, Any],
    ) -> None:
        if getattr(self._stop_event, "is_set", lambda: False)():
            return
        if int(session_seq) != int(getattr(self, "_match_session_seq", 0) or 0):
            return
        if self._active_match_key != expected_match_key:
            return
        if self.sm.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            return

        last_fin_gid = int(getattr(self, "_last_finished_game_id", 0) or 0)
        lcu_gid_clean = int(lcu_game_id or 0)
        if lcu_gid_clean > 0 and lcu_gid_clean == last_fin_gid:
            lcu_gid_clean = 0
        eff_gid = lcu_gid_clean or int(getattr(self, "_active_game_id", 0) or 0)
        if eff_gid > 0 and eff_gid == last_fin_gid:
            eff_gid = 0
        eff_qid = int(lcu_queue_id or 0) or int(getattr(self, "_active_queue_id", 0) or 0)
        self._active_game_id = eff_gid
        self._active_queue_id = eff_qid
        if eff_gid > 0:
            new_mk = f"gid:{eff_gid}"
            if new_mk != self._active_match_key:
                self._active_match_key = new_mk
                self.log_ring.update_match_meta(match_key=new_mk)
        game_mode = str(getattr(live_snap, "game_mode", "") or self._match_game_mode or "").strip()
        self._match_game_mode = game_mode
        self._match_is_non_mayhem = self._is_non_mayhem_mode(game_mode, eff_qid)

        if not getattr(self, "_devlab_active", False):
            return

        upd_kwargs: dict[str, Any] = {"is_non_mayhem": self._match_is_non_mayhem}
        if eff_gid > 0:
            upd_kwargs["match_key_hash"] = hashlib.sha256(str(eff_gid).encode("utf-8")).hexdigest()[:16]
        try:
            self._devlab.update_match_meta(**upd_kwargs)
        except Exception:
            pass

        rp = rune_page if isinstance(rune_page, dict) else {}
        live_runes = dict(getattr(live_snap, "active_runes", None) or {})
        my_p = live_snap.my_player
        keystone_id = int(
            live_runes.get("keystone_id")
            or (getattr(my_p, "keystone_id", 0) if my_p is not None else 0)
            or 0
        )
        primary_style_id = int(
            rp.get("primary_style_id")
            or live_runes.get("primary_tree_id")
            or (getattr(my_p, "primary_tree_id", 0) if my_p is not None else 0)
            or 0
        )
        sub_style_id = int(
            rp.get("sub_style_id")
            or live_runes.get("secondary_tree_id")
            or (getattr(my_p, "secondary_tree_id", 0) if my_p is not None else 0)
            or 0
        )
        selected_perks = [int(x) for x in (rp.get("selected_perk_ids") or []) if int(x or 0) > 0]
        if not selected_perks and keystone_id > 0:
            selected_perks = [keystone_id]
        ally_loadouts = [
            {
                "champion": p.champion_name,
                "keystone_id": int(getattr(p, "keystone_id", 0) or 0),
                "primary_tree_id": int(getattr(p, "primary_tree_id", 0) or 0),
                "spells": list(p.summoner_spells),
            }
            for p in live_snap.allies
        ]
        enemy_loadouts = [
            {
                "champion": p.champion_name,
                "keystone_id": int(getattr(p, "keystone_id", 0) or 0),
                "primary_tree_id": int(getattr(p, "primary_tree_id", 0) or 0),
                "spells": list(p.summoner_spells),
            }
            for p in live_snap.enemies
        ]
        self._emit_devlab(
            "game",
            "match_runes_and_loadout_locked",
            {
                "primary_style_id": primary_style_id,
                "sub_style_id": sub_style_id,
                "selected_perk_ids": selected_perks,
                "keystone_id": keystone_id,
                "keystone_name": str(live_snap.my_keystone or ""),
                "my_spells": list(my_p.summoner_spells) if my_p is not None else [],
                "ally_loadouts": ally_loadouts,
                "enemy_loadouts": enemy_loadouts,
                "game_mode": game_mode,
                "map_number": int(getattr(live_snap, "map_number", 0) or 0),
                "queue_id": eff_qid,
                "is_non_mayhem": self._match_is_non_mayhem,
            },
            game_time_s=float(live_snap.game_time or 0.0),
        )
        self._refresh_devlab_badge_if_visible()

    def _apply_live_snapshot(self, live_snap: LiveMatchSnapshot, *, degraded_2999: bool = False) -> None:
        t0 = time.perf_counter()
        devlab_on = bool(getattr(self, "_devlab_active", False))
        self._lcu_no_live_polls = 0
        was_degraded = bool(getattr(self.sm, "is_degraded_2999", False))
        prev_live = self._last_live
        degraded_roster_expanded = False
        if degraded_2999:
            live_obj = getattr(self, "live", None)
            live_fails = int(getattr(live_obj, "consecutive_failures", 0) or 0) if live_obj is not None else 0
            if (
                not getattr(self, "_port_2999_conflict_probed", False)
                and live_fails >= int(self.LCU_2999_DEGRADED_GRACE_FAILURES)
            ):
                self._port_2999_conflict_probed = True
                self._port_2999_conflict_detected = self._probe_port_2999_conflict()
                if self._port_2999_conflict_detected:
                    logger.warning(
                        "Detected TCP port 2999 conflict (WSAEACCES/WSAEADDRINUSE) — another process (e.g. proxy/Clash) "
                        "bound ephemeral port 2999 before League of Legends started."
                    )
            self.sm.is_degraded_2999 = True
            if (
                was_degraded
                and prev_live is not None
                and prev_live.my_player is not None
                and live_snap.my_player is not None
                and prev_live.my_player.champion_name == live_snap.my_player.champion_name
            ):
                if len(live_snap.enemies) > len(prev_live.enemies):
                    degraded_roster_expanded = True
                elif (
                    len(live_snap.enemies) < len(live_snap.allies)
                    and len(prev_live.enemies) > len(live_snap.enemies)
                ):
                    live_snap = replace(live_snap, enemies=prev_live.enemies)
            live_snap = self._hydrate_degraded_snapshot(live_snap)
            if degraded_roster_expanded:
                self._last_drawer_refresh_sig = None
        elif was_degraded:
            self._port_2999_conflict_detected = False
            self._port_2999_conflict_probed = False
            self._visual_is_dead = None
            self.sm.is_degraded_2999 = False
            self.sm.manual_owned_items.clear()
            self.sm.visual_owned_items.clear()
            self.sm.suppressed_visual_items.clear()
            if getattr(self, "_item_vote_tracker", None) is not None:
                self._item_vote_tracker.reset()
            self._last_drawer_refresh_sig = None
            self.edge_drawer.set_degraded_2999_mode(False)
            logger.info("LiveClient 127.0.0.1:2999 recovered; exited Plan B degraded sync mode")
        if getattr(self, "_pending_eog_since", None) is not None:
            waited_ms = round((time.monotonic() - float(self._pending_eog_since or 0.0)) * 1000.0, 1)
            self._pending_eog_since = None
            self._pending_eog_phase = ""
            if devlab_on:
                self._devlab.set_context(
                    game_state=self.sm.state.value,
                    game_time_s=float(live_snap.game_time or 0.0),
                )
                self._emit_devlab(
                    "game",
                    "gameflow_phase_transition",
                    {
                        "from_phase": "PENDING_EOG",
                        "to_phase": self.sm.state.value,
                        "reason": "live_reconnected",
                        "reconnected": True,
                        "live_recovered": True,
                        "waited_ms": waited_ms,
                    },
                    game_time_s=float(live_snap.game_time or 0.0),
                )

        self._last_live = live_snap
        self._last_cs = None
        resolved_hid = None
        if live_snap.my_player:
            h = self._resolve_hero_from_live_player(live_snap.my_player)
            if h is not None:
                resolved_hid = h.hero_id
        prev_level = int(getattr(self, "_last_live_level", 0) or 0)
        cur_level = int(live_snap.my_player.level or 0) if live_snap.my_player is not None else 0
        if cur_level > prev_level and expected_augment_slots_for_level(cur_level) > expected_augment_slots_for_level(prev_level):
            self._picker_opened_since_level = False
        if cur_level > 0:
            self._last_live_level = cur_level

        eff = self.sm.on_live_snapshot(live_snap, resolved_hid, time.monotonic(), store=self.store)
        if degraded_2999:
            self.sm.is_degraded_2999 = True
        if eff.state_changed and self.sm.state == GameState.IN_GAME:
            self._overlays_hidden_by_user = False
            self._combo_hidden_by_user = False
            self._champ_closed_by_user_in_session = False
            self._latest_advice = None
            self._latest_request_id = ""
            self._last_combo_verdict = None
            self._cancel_inflight_picker_coach()
            self._cancel_death_coach_defer()
            self._cancel_augment_remove_coach_defer("new match")
            self._cancel_game_start_coach_defer("new match")
            self._cancel_pending_icon_verify()
            self._restore_configured_dataset_mode()
            self._reset_match_telemetry_counters()
            self._reset_plan_b_trackers()
            self._last_live_level = cur_level
            if self.detector is not None:
                self.detector.reset_match_metrics()
            self.edge_drawer.reset_opening_brief_state()
            self._lobby_scout = None
            self._lobby_scout_match_key = ""
            self._lobby_scout_in_flight = False
            self.edge_drawer.set_lobby_scout(None)
            self.edge_drawer.set_coach_advice(None)
            self.edge_drawer.set_recent_offered([])
            self.edge_drawer.set_picker_active(False)
            self.edge_drawer.set_pending_transmute(None, self.store)
            self.combo_overlay.exit_picker_mode()
            if self.transmute_watcher is not None:
                self.transmute_watcher.reset()
            self._start_or_attach_match_session(live_snap, resolved_hid)
            if degraded_2999:
                self.sm.is_degraded_2999 = True
                live_snap = self._hydrate_degraded_snapshot(live_snap)
                self._last_live = live_snap
        elif self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING) and not self._active_match_key:
            self._start_or_attach_match_session(live_snap, resolved_hid)
            if degraded_2999:
                self.sm.is_degraded_2999 = True
                live_snap = self._hydrate_degraded_snapshot(live_snap)
                self._last_live = live_snap

        self._match_max_game_time_s = max(self._match_max_game_time_s, float(live_snap.game_time or 0.0))
        if live_snap.game_end_result in (0, 1):
            self._match_won_result = int(live_snap.game_end_result)
        raw_my_stats = getattr(live_snap, "my_stats", None)
        if isinstance(raw_my_stats, dict) and any(
            isinstance(v, (int, float)) and float(v) > 0.0 for v in raw_my_stats.values()
        ):
            self._match_last_my_stats = dict(raw_my_stats)
        if live_snap.my_player:
            new_my_items = tuple(live_snap.my_player.item_ids)
            if new_my_items != self._match_last_my_items:
                self._match_last_my_items = new_my_items
                self._prefetch_my_item_icons(new_my_items)
            self._last_kda = (
                f"{live_snap.my_player.kills}/{live_snap.my_player.deaths}/{live_snap.my_player.assists}"
            )
            my_sid = int(getattr(live_snap.my_player, "skin_id", 0) or 0)
            my_sname = str(getattr(live_snap.my_player, "skin_name", "") or "").strip()
            if my_sid > 0:
                self._match_last_skin_id = my_sid
            if my_sname:
                self._match_last_skin_name = my_sname
            if not live_snap.my_player.is_dead:
                self._death_defer_superseded_by_picker = False
                self._death_superseded_req_id = ""
        if live_snap.my_team_kills or live_snap.enemy_team_kills:
            self._last_team_kills = f"{int(live_snap.my_team_kills)}:{int(live_snap.enemy_team_kills)}"
        active_hid = self._active_hero_id()
        if active_hid:
            self._match_last_hero_id = active_hid
            h_info = self.store.get_hero(active_hid)
            if h_info is not None:
                self._match_last_hero_name = h_info.name
                self.log_ring.update_match_meta(hero_id=active_hid, hero_name=h_info.name)
            self._prefetch_my_skin_splash(active_hid, int(getattr(self, "_match_last_skin_id", 0) or 0))

        if devlab_on:
            self._devlab.set_context(
                game_state=self.sm.state.value,
                hero_id=active_hid or "",
                dataset_tag=self._current_dataset_tag(),
                game_time_s=float(live_snap.game_time or 0.0),
            )
            if self._action_tracker is not None:
                self._action_tracker.on_live_snapshot(
                    live_snap,
                    selected_augment_count=len(self.sm.selected_augments),
                    is_picker_open=(self.sm.state == GameState.AUGMENT_PICKING),
                    picker_ever_opened_since_level=bool(getattr(self, "_picker_opened_since_level", False)),
                    is_degraded_2999=bool(self.sm.is_degraded_2999),
                )

        self.lookup_window.set_game_active(True)
        self.edge_drawer.set_live_vital_state(
            is_dead=bool(live_snap.my_player and live_snap.my_player.is_dead),
            game_time_s=float(live_snap.game_time or 0.0),
        )
        if self.champ_overlay.isVisible():
            self._mark_overlay_close("champ_select", "match_start")
        self.champ_overlay.hide()
        self._refresh_degraded_sync_bar()

        # WS-8: Dirty-signature cache skips redundant Qt drawer/combo rebuilds when live state is unchanged
        raw_crit_v = 0.0
        if isinstance(raw_my_stats, dict):
            try:
                raw_crit_v = float(raw_my_stats.get("critChance", 0.0) or 0.0)
            except (TypeError, ValueError):
                raw_crit_v = 0.0
        crit_sig_pct = int(round(raw_crit_v if raw_crit_v > 1.5 else raw_crit_v * 100.0))
        drawer_sig = (
            active_hid or "",
            cur_level,
            int(float(live_snap.current_gold or 0.0) // 50.0),
            tuple(live_snap.my_player.item_ids) if live_snap.my_player else (),
            tuple((a.champion_name, tuple(a.item_ids)) for a in live_snap.allies),
            tuple((e.champion_name, tuple(e.item_ids), e.kills, e.deaths) for e in live_snap.enemies),
            tuple(self.sm.selected_augments),
            tuple(self.sm.committed_plan),
            self.sm.locked_next_item,
            bool(self.sm.allow_plan_overhaul),
            bool(live_snap.my_player and live_snap.my_player.is_dead),
            bool(self.sm.is_degraded_2999),
            str(self._latest_advice.request_id) if self._latest_advice is not None else "",
            str(getattr(self.store, "active_match_mode", "") or ""),
            self._current_data_band_mode(),
            crit_sig_pct,
        )
        if eff.state_changed or drawer_sig != getattr(self, "_last_drawer_refresh_sig", None):
            self._last_drawer_refresh_sig = drawer_sig
            self._refresh_drawer_view()
        self._push_vision_context()

        game_rect = find_game_window()
        if game_rect is not None:
            game_scr = screen_for_physical_rect((game_rect.left, game_rect.top, game_rect.width, game_rect.height))
            if game_scr is not None:
                self.edge_drawer.set_target_screen(game_scr)
                self.combo_overlay.set_target_screen(game_scr)
        game_fg = True if game_rect is None else self._is_game_or_overlay_foreground(game_rect.hwnd)
        if not self._overlays_hidden_by_user and game_fg:
            if eff.auto_peek_drawer and self.cfg.auto_peek_on_death:
                self._mark_overlay_open("edge_drawer", "auto_death_peek")
                self.edge_drawer.set_expanded(True)
            elif eff.auto_collapse_drawer and self.cfg.auto_peek_on_death:
                self.edge_drawer.set_expanded(False)
            if not self.edge_drawer.isVisible():
                self._mark_overlay_open("edge_drawer", "auto_match_start")
                self.edge_drawer.show_overlay()
            if (
                self.cfg.combo_overlay_enabled
                and not self._combo_hidden_by_user
                and not self._is_full_locked()
                and not self.combo_overlay.isVisible()
            ):
                self._mark_overlay_open("combo_overlay", "auto_match_start")
                self.combo_overlay.show_overlay()
        elif not game_fg:
            text_input_active = bool(getattr(self.edge_drawer, "_text_input_active", False))
            if self.edge_drawer.isVisible() and not text_input_active:
                self._mark_overlay_close("edge_drawer", "game_unfocused")
                self.edge_drawer.hide()
            if self.augment_overlay.isVisible():
                self._mark_overlay_close("augment_badges", "game_unfocused")
                self.augment_overlay.hide()
            if self.combo_overlay.isVisible() and not text_input_active:
                self._mark_overlay_close("combo_overlay", "game_unfocused")
                self.combo_overlay.hide()

        if eff.death_detected and not eff.coach_trigger and eff.death_skip_reason:
            logger.info("Skipping death_enemy_build_changed: %s", eff.death_skip_reason)
            if self._action_tracker is not None:
                self._action_tracker.note_death_coach_decision(False, eff.death_skip_reason)
        elif eff.death_detected and eff.coach_trigger == "death_enemy_build_changed" and not (
            self.cfg.auto_trigger_coach and self._is_any_coach_online()
        ):
            if self._action_tracker is not None:
                self._action_tracker.note_death_coach_decision(False, "coach_offline_or_disabled")

        if eff.coach_trigger and self.cfg.auto_trigger_coach and self._is_any_coach_online():
            if eff.coach_trigger == "death_enemy_build_changed":
                now_m = time.monotonic()
                cur_enemy_sig = self.sm._enemy_build_signature(live_snap, store=self.store)
                warmup_age = now_m - float(getattr(self, "_last_warmup_advice_mono", 0.0) or 0.0)
                warmup_covers_death = (
                    self._latest_advice is not None
                    and float(getattr(self, "_last_warmup_advice_mono", 0.0) or 0.0) > 0.0
                    and warmup_age <= float(self.WARMUP_COVERS_DEATH_WINDOW_S)
                    and cur_enemy_sig == getattr(self, "_last_warmup_advice_enemy_sig", "")
                )
                inflight_warmup_mono = float(getattr(self, "_last_warmup_dispatched_mono", 0.0) or 0.0)
                inflight_warmup_age = now_m - inflight_warmup_mono
                has_active_warmup = any(
                    w.has_inflight_trigger("pre_death_warmup") is True
                    for w in (getattr(self, "cloud_coach", None), getattr(self, "gemini_coach", None))
                    if w is not None and callable(getattr(w, "has_inflight_trigger", None))
                )
                warmup_inflight_covers_death = (
                    not warmup_covers_death
                    and has_active_warmup
                    and inflight_warmup_mono > 0.0
                    and 0.0 <= inflight_warmup_age <= float(self.WARMUP_INFLIGHT_REUSE_WINDOW_S)
                    and cur_enemy_sig == getattr(self, "_last_warmup_dispatched_enemy_sig", "")
                )
                my_gold = float(live_snap.current_gold or 0.0)
                falling_behind = bool(
                    live_snap.my_player is not None
                    and live_snap.my_player.deaths >= live_snap.my_player.kills + self.sm.DEATH_FALLING_BEHIND_DIFF
                )
                if warmup_covers_death:
                    logger.info(
                        "Death-shop served instantly in 0ms from fresh pre_death_warmup cache (age=%.1fs)",
                        warmup_age,
                    )
                    if self._action_tracker is not None:
                        try:
                            self._action_tracker.note_death_coach_decision(
                                False,
                                "pre_death_warmup_cached",
                                warmup_served=True,
                            )
                        except TypeError:
                            self._action_tracker.note_death_coach_decision(False, "pre_death_warmup_cached")
                elif warmup_inflight_covers_death:
                    logger.info(
                        "Death-shop reusing in-flight pre_death_warmup (age=%.1fs <= %.0fs)",
                        inflight_warmup_age,
                        float(self.WARMUP_INFLIGHT_REUSE_WINDOW_S),
                    )
                    if self._latest_advice is None and not self._cloud_ai_cutoff:
                        self.edge_drawer.set_coach_pending(True, pending_label="⏳ 复用进行中预热推演…")
                    if self._action_tracker is not None:
                        try:
                            self._action_tracker.note_death_coach_decision(
                                False,
                                "pre_death_warmup_inflight",
                                warmup_served=True,
                            )
                        except TypeError:
                            self._action_tracker.note_death_coach_decision(False, "pre_death_warmup_inflight")
                elif not eff.enemy_sig_changed and my_gold < self.sm.DEATH_SHOPPING_GOLD_THRESHOLD and not falling_behind:
                    logger.info(
                        "Skipping death_enemy_build_changed: neither significant enemy item change nor gold>=%dG nor falling behind",
                        int(self.sm.DEATH_SHOPPING_GOLD_THRESHOLD),
                    )
                    if self._action_tracker is not None:
                        self._action_tracker.note_death_coach_decision(False, "below_gold_and_sig_threshold")
                elif self.sm.state == GameState.AUGMENT_PICKING:
                    logger.info("Skipping death_enemy_build_changed: AUGMENT_PICKING already active")
                    if self._action_tracker is not None:
                        self._action_tracker.note_death_coach_decision(False, "augment_picking_active")
                elif self._should_defer_death_coach(live_snap):
                    self._schedule_death_coach_defer()
                else:
                    if self._action_tracker is not None:
                        self._action_tracker.note_death_coach_decision(True, "")
                    self.trigger_coach_request(eff.coach_trigger)
            elif eff.coach_trigger == "pre_death_warmup":
                if self.sm.state == GameState.IN_GAME:
                    self.trigger_coach_request("pre_death_warmup")
            elif (
                eff.coach_trigger == "game_start"
                and self.cfg.lobby_scout_enabled
                and float(live_snap.game_time or 0.0) < 90.0
                and self._lobby_scout is None
                and getattr(self, "_lobby_scout_in_flight", False)
                and self._lobby_scout_match_key == self._active_match_key
            ):
                self._schedule_game_start_coach_defer(self._active_match_key)
            else:
                self.trigger_coach_request(eff.coach_trigger)

        if devlab_on:
            self._devlab.record_latency_sample("apply_live_snapshot", (time.perf_counter() - t0) * 1000.0)
            self._devlab.flush_latency_summary()

    def _finalize_match_eog(
        self,
        *,
        phase: str,
        eog_snap: dict[str, Any] | None = None,
        eog_source: str = "",
        waited_ms: float = 0.0,
    ) -> None:
        resolved_won = self._match_won_result
        fin_gid = int(
            (eog_snap.get("game_id") if isinstance(eog_snap, dict) else 0)
            or getattr(self, "_active_game_id", 0)
            or 0
        )
        if fin_gid > 0:
            self._last_finished_game_id = fin_gid
        if isinstance(eog_snap, dict):
            if resolved_won not in (0, 1) and int(eog_snap.get("won", -1)) in (0, 1):
                resolved_won = int(eog_snap["won"])
            eog_items = tuple(eog_snap.get("item_ids") or ())
            if eog_items:
                self._match_last_my_items = eog_items
            if eog_snap.get("kda"):
                self._last_kda = str(eog_snap["kda"])
            if int(eog_snap.get("duration_s") or 0) > self._match_max_game_time_s:
                self._match_max_game_time_s = float(eog_snap["duration_s"])
            probed_raw = [
                str(int(a))
                for a in (eog_snap.get("eog_augments_probed") or [])
                if str(a).lstrip("-").isdigit() and int(a) > 0
            ]
            if len(probed_raw) > len(self.sm.selected_augments):
                if len(self.sm.selected_augments) > 0:
                    missing_head = len(probed_raw) - len(self.sm.selected_augments)
                    backfilled: list[str] = []
                    for p_aid in probed_raw[:missing_head]:
                        if p_aid not in self.sm.selected_augments and p_aid not in backfilled:
                            backfilled.append(p_aid)
                    if backfilled:
                        self.sm.selected_augments = (backfilled + list(self.sm.selected_augments))[:MAX_AUGMENT_SLOTS]
                else:
                    self.sm.selected_augments = probed_raw[:MAX_AUGMENT_SLOTS]
                if self.sm.selected_augments and self.store.is_loaded:
                    first_aug = self.store.get_augment(self.sm.selected_augments[0])
                    if first_aug is not None:
                        self._match_is_first_prismatic = first_aug.rarity == "prismatic"

        devlab_on = bool(getattr(self, "_devlab_active", False))
        if devlab_on:
            if self._action_tracker is not None:
                try:
                    self._action_tracker.flush_pending_transactions()
                except Exception:
                    pass
            eog_payload: dict[str, Any] = {
                "won": int(resolved_won) if resolved_won in (0, 1) else -1,
                "kda": str(self._last_kda or ""),
                "item_ids": list(self._match_last_my_items),
                "duration_s": int(round(self._match_max_game_time_s)),
                "selected_augments": list(self.sm.selected_augments),
                "eog_source": str(eog_source or (eog_snap.get("eog_source") if isinstance(eog_snap, dict) else "") or ""),
                "lcu_phase": str(phase or ""),
                "waited_ms": round(float(waited_ms), 1),
                "game_mode": str(getattr(self, "_match_game_mode", "") or ""),
                "queue_id": int(getattr(self, "_active_queue_id", 0) or 0),
                "is_non_mayhem": bool(getattr(self, "_match_is_non_mayhem", False)),
                "is_first_prismatic": bool(self._match_is_first_prismatic),
                "ai_calls_count": int(self.ai_calls_this_match),
                "user_swapped_count": int(self.user_swapped_this_match),
            }
            if isinstance(eog_snap, dict):
                for k in (
                    "total_damage_dealt_to_champions",
                    "total_damage_taken",
                    "total_heal",
                    "total_damage_shielded_on_teammates",
                    "time_ccing_others",
                    "gold_earned",
                    "total_time_spent_dead",
                    "game_ended_in_early_surrender",
                    "queue_id",
                    "all_players_final",
                    "eog_augments_probed",
                ):
                    if k in eog_snap and eog_snap[k] is not None:
                        eog_payload[k] = eog_snap[k]
            self._emit_devlab(
                "game",
                "match_eog_summary",
                eog_payload,
                game_time_s=float(self._match_max_game_time_s),
                priority="high",
            )
            self._emit_devlab(
                "game",
                "gameflow_phase_transition",
                {
                    "from_phase": "PENDING_EOG" if getattr(self, "_pending_eog_since", None) is not None else self.sm.state.value,
                    "to_phase": GameState.IDLE.value,
                    "lcu_phase": str(phase or ""),
                    "reason": f"match_finalized:{eog_payload['eog_source'] or 'eog'}",
                    "waited_ms": round(float(waited_ms), 1),
                },
                game_time_s=float(self._match_max_game_time_s),
                priority="high",
            )
        self._maybe_submit_match_telemetry(won=resolved_won)
        self._capture_post_match_recap(won=resolved_won, eog_snap=eog_snap)
        if devlab_on:
            self._devlab.end_match(
                won=int(resolved_won) if resolved_won in (0, 1) else -1,
                duration_s=int(round(self._match_max_game_time_s)),
                eog_source=str(eog_payload["eog_source"]),
                summary_extra={
                    "kda": str(self._last_kda or ""),
                    "selected_augments": list(self.sm.selected_augments),
                    "is_non_mayhem": bool(getattr(self, "_match_is_non_mayhem", False)),
                },
            )
            if self._action_tracker is not None:
                self._action_tracker.reset()

        self.log_ring.end_match(
            won=resolved_won,
            duration_s=int(self._match_max_game_time_s),
        )
        self._pending_eog_since = None
        self._pending_eog_phase = ""
        self._lcu_no_live_polls = 0
        self._last_live = None
        self._last_live_level = 0
        self._cancel_inflight_picker_coach()
        self._cancel_death_coach_defer()
        self._cancel_augment_remove_coach_defer("match ended")
        self._cancel_game_start_coach_defer("match ended")
        self._cancel_pending_icon_verify()
        self.sm.on_live_snapshot(None, None, time.monotonic())
        self._restore_configured_dataset_mode()
        self._active_game_id = 0
        self._last_cs_game_id = 0
        self._reset_match_telemetry_counters()
        self._reset_plan_b_trackers(clear_sm_visual=True)
        self._clear_match_state()
        self._latest_advice = None
        self._latest_request_id = ""
        self._last_combo_verdict = None
        self._last_combo_plan = None
        self._lobby_scout = None
        self._lobby_scout_match_key = ""
        self._lobby_scout_in_flight = False
        self.edge_drawer.reset_opening_brief_state()
        self.edge_drawer.set_lobby_scout(None)
        self.edge_drawer.set_coach_advice(None)
        self.edge_drawer.set_recent_offered([])
        self.edge_drawer.set_picker_active(False)
        self.edge_drawer.set_pending_transmute(None, self.store)
        if self.augment_overlay.isVisible():
            self._mark_overlay_close("augment_badges", "match_end")
        self.augment_overlay.hide()
        if self.edge_drawer.isVisible():
            self._mark_overlay_close("edge_drawer", "match_end")
        self.edge_drawer.hide()
        self.combo_overlay.exit_picker_mode()
        if self.combo_overlay.isVisible():
            self._mark_overlay_close("combo_overlay", "match_end")
        self.combo_overlay.hide()
        self.lookup_window.set_game_active(False)
        self._last_picker_gate_skipped_hint = ""
        if self.detector:
            self.detector.reset()
            self.detector.reset_match_metrics()
        if self.transmute_watcher is not None:
            self.transmute_watcher.reset()
        if devlab_on:
            self._refresh_devlab_badge_if_visible()

    def _apply_lcu_poll(self, phase: str, cs: ChampSelectState | None) -> None:
        devlab_on = bool(getattr(self, "_devlab_active", False))
        prev_lcu_phase = str(getattr(self, "_last_lcu_phase", "") or "")
        if phase != prev_lcu_phase:
            self._last_lcu_phase = str(phase or "")
            if devlab_on and (prev_lcu_phase or phase):
                self._emit_devlab(
                    "game",
                    "gameflow_phase_transition",
                    {
                        "from_phase": prev_lcu_phase or "None",
                        "to_phase": str(phase or "None"),
                        "reason": "lcu_poll",
                    },
                )

        if self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            if phase in ("EndOfGame", "PreEndOfGame"):
                eog_snap = self._fetch_eog_snapshot_safe()
                if (
                    self._match_won_result not in (0, 1)
                    and (not isinstance(eog_snap, dict) or int(eog_snap.get("won", -1)) not in (0, 1))
                ):
                    fb = self._fetch_match_history_fallback_safe()
                    if isinstance(fb, dict):
                        eog_snap = fb
                waited_ms = (
                    (time.monotonic() - float(self._pending_eog_since)) * 1000.0
                    if getattr(self, "_pending_eog_since", None) is not None
                    else 0.0
                )
                src = str((eog_snap or {}).get("eog_source") or "eog_stats_block")
                self._finalize_match_eog(phase=phase, eog_snap=eog_snap, eog_source=src, waited_ms=waited_ms)
            elif phase in ("Lobby", "None", "Matchmaking", "ReadyCheck", "ChampSelect"):
                eog_snap = self._fetch_match_history_fallback_safe()
                waited_ms = (
                    (time.monotonic() - float(self._pending_eog_since)) * 1000.0
                    if getattr(self, "_pending_eog_since", None) is not None
                    else 0.0
                )
                src = str((eog_snap or {}).get("eog_source") or f"lcu_phase:{phase}")
                self._finalize_match_eog(phase=phase, eog_snap=eog_snap, eog_source=src, waited_ms=waited_ms)
            else:
                # InProgress / WaitingForStats / Reconnect / GameStart / Disconnected while 2999 did not respond:
                # 3-failure debounce + 18s PENDING_EOG buffer prevents premature match reset on transient 2999 blips.
                self._lcu_no_live_polls = int(getattr(self, "_lcu_no_live_polls", 0) or 0) + 1
                live_obj = getattr(self, "live", None)
                live_fails = int(getattr(live_obj, "consecutive_failures", 0) or 0) if live_obj is not None else 0
                effective_fails = max(live_fails, self._lcu_no_live_polls)
                now_mono = time.monotonic()
                if getattr(self, "_pending_eog_since", None) is None:
                    if effective_fails < self.LIVE_DISCONNECT_DEBOUNCE_FAILS:
                        return
                    self._pending_eog_since = now_mono
                    self._pending_eog_phase = str(phase or "")
                    if getattr(self, "_click_timer", None) is not None and self._click_timer.isActive():
                        self._click_timer.stop()
                    if self.augment_overlay.isVisible():
                        self._mark_overlay_close("augment_badges", "pending_eog")
                        self.augment_overlay.hide()
                    if devlab_on:
                        self._devlab.set_context(game_state="PENDING_EOG")
                        self._emit_devlab(
                            "game",
                            "gameflow_phase_transition",
                            {
                                "from_phase": self.sm.state.value,
                                "to_phase": "PENDING_EOG",
                                "lcu_phase": str(phase or ""),
                                "consecutive_failures": effective_fails,
                                "reason": "live_2999_disconnect",
                            },
                        )
                    return
                waited_s = now_mono - float(self._pending_eog_since)
                if waited_s < self.PENDING_EOG_TIMEOUT_S:
                    return
                eog_snap = self._fetch_match_history_fallback_safe()
                src = str((eog_snap or {}).get("eog_source") or "timeout")
                self._finalize_match_eog(
                    phase=phase,
                    eog_snap=eog_snap,
                    eog_source=src,
                    waited_ms=waited_s * 1000.0,
                )
        else:
            self._last_live = None
            if phase in ("EndOfGame", "PreEndOfGame", "Lobby", "None") and self._auto_switched_first_prismatic:
                self._restore_configured_dataset_mode()

        prev_sm_state = self.sm.state
        self.sm.on_lcu_poll(phase, cs)
        self._last_cs = cs if self.sm.state == GameState.CHAMP_SELECT else None
        if self.sm.state != GameState.CHAMP_SELECT:
            self._champ_closed_by_user_in_session = False
            if prev_sm_state == GameState.CHAMP_SELECT and devlab_on:
                if self._action_tracker is not None:
                    self._action_tracker.on_lcu_champ_select(phase, None)
                if phase in ("Lobby", "None", "Matchmaking", "ReadyCheck"):
                    self._devlab.end_match(
                        won=-1,
                        duration_s=0,
                        eog_source=f"champ_select_dodged:{phase}",
                    )
                    self._refresh_devlab_badge_if_visible()

        if self.sm.state == GameState.CHAMP_SELECT and cs is not None:
            if prev_sm_state != GameState.CHAMP_SELECT:
                self._reset_plan_b_trackers(clear_sm_visual=True)
                self._match_last_skin_id = 0
                self._match_last_skin_name = ""
                self._last_cs_game_id = 0
                self._last_cs_queue_id = 0
                # WS-5: Automatically hide Lobby-leftover edge_drawer & combo_overlay when entering ChampSelect
                if self.edge_drawer.isVisible():
                    self._mark_overlay_close("edge_drawer", "champ_select_enter")
                    self.edge_drawer.hide()
                if self.combo_overlay.isVisible():
                    self._mark_overlay_close("combo_overlay", "champ_select_enter")
                    self.combo_overlay.hide()
            last_fin_gid = int(getattr(self, "_last_finished_game_id", 0) or 0)
            cs_gid_val = int(getattr(cs, "game_id", 0) or 0)
            if cs_gid_val > 0 and cs_gid_val == last_fin_gid:
                cs_gid_val = 0
            cs_qid_val = int(getattr(cs, "queue_id", 0) or 0)
            if cs_gid_val > 0:
                self._last_cs_game_id = cs_gid_val
            if cs_qid_val > 0:
                self._last_cs_queue_id = cs_qid_val
            cs_skin_id = int(getattr(cs, "my_skin_id", 0) or 0)
            if cs_skin_id > 0:
                self._match_last_skin_id = cs_skin_id
                if cs.my_hero_id:
                    self._prefetch_my_skin_splash(str(cs.my_hero_id), cs_skin_id)
            comp_analysis: TeamCompAnalysis | None = None
            if self.store.is_loaded:
                try:
                    comp_analysis = analyze_champ_select_pool(
                        my_hero_id=cs.my_hero_id,
                        team_hero_ids=cs.team_hero_ids,
                        bench_hero_ids=cs.bench_hero_ids,
                        store=self.store,
                        goal_mode=self.cfg.champ_select_goal_mode,
                    )
                except Exception as exc:
                    logger.debug("ChampSelect TeamCompAnalysis skipped: %s", exc)
                    comp_analysis = None

            if devlab_on:
                cs_mkh = hashlib.sha256(str(cs_gid_val).encode("utf-8")).hexdigest()[:16] if cs_gid_val > 0 else ""
                self._devlab.begin_champ_select(
                    match_key_hash=cs_mkh,
                    hero_id=str(cs.my_hero_id or ""),
                    dataset_tag=self._current_dataset_tag(),
                )
                self._devlab.set_context(
                    game_state="CHAMP_SELECT",
                    hero_id=str(cs.my_hero_id or ""),
                    dataset_tag=self._current_dataset_tag(),
                    game_time_s=None,
                )
                if self._action_tracker is not None:
                    comp_dict: dict[str, Any] | None = None
                    best_swap_id = ""
                    if comp_analysis is not None:
                        best_swap_id = comp_analysis.best_swap.hero_id if comp_analysis.best_swap is not None else ""
                        comp_dict = {
                            "goal_mode": comp_analysis.goal_mode,
                            "current_score": round(float(comp_analysis.current_score), 3),
                            "best_swap_hero_id": best_swap_id,
                            "best_swap_delta": round(float(comp_analysis.best_swap.score_delta), 3)
                            if comp_analysis.best_swap is not None
                            else 0.0,
                            "best_swap_reason": comp_analysis.best_swap.reason
                            if comp_analysis.best_swap is not None
                            else "",
                            "best_five_hero_ids": list(comp_analysis.best_five.hero_ids)
                            if comp_analysis.best_five is not None
                            else [],
                            "needed_roles": sorted(comp_analysis.needed_roles),
                            "is_warn": bool(comp_analysis.is_warn),
                        }
                    hero_ratings: dict[str, float] = {}
                    if self.store.is_loaded:
                        pool_ids = list(
                            dict.fromkeys(
                                [str(x) for x in ([cs.my_hero_id] if cs.my_hero_id else []) + list(cs.team_hero_ids) + list(cs.bench_hero_ids) if x]
                            )
                        )
                        for phid in pool_ids:
                            h_obj = self.store.get_hero(phid)
                            if h_obj is not None:
                                try:
                                    hero_ratings[phid] = round(float(h_obj.win_rate) * 100.0, 2)
                                except Exception:
                                    pass
                    self._action_tracker.on_lcu_champ_select(
                        phase,
                        cs,
                        team_comp_analysis=comp_dict,
                        goal_mode=self.cfg.champ_select_goal_mode,
                        best_swap_hero_id=best_swap_id,
                        hero_ratings=hero_ratings,
                    )
                if prev_sm_state != GameState.CHAMP_SELECT:
                    self._refresh_devlab_badge_if_visible()

            if (
                not self._overlays_hidden_by_user
                and not self._is_full_locked()
                and not getattr(self, "_champ_closed_by_user_in_session", False)
            ):
                try:
                    self.champ_overlay.update_state(cs, self.store, team_comp_analysis=comp_analysis)
                except TypeError:
                    self.champ_overlay.update_state(cs, self.store)
                if not self.cfg.champ_window_pos:
                    lcu_rect = find_lcu_window()
                    if lcu_rect is not None:
                        self.champ_overlay.position_beside_lcu(
                            (lcu_rect.left, lcu_rect.top, lcu_rect.width, lcu_rect.height)
                        )
                if not self.champ_overlay.isVisible():
                    self._mark_overlay_open("champ_select", "auto_champ_select")
                    self.champ_overlay.show_overlay()
            else:
                if self.champ_overlay.isVisible():
                    self._mark_overlay_close("champ_select", "hidden_or_locked")
                    self.champ_overlay.hide()
        else:
            if self.champ_overlay.isVisible():
                self._mark_overlay_close("champ_select", "phase_exit")
                self.champ_overlay.hide()

    def _push_vision_context(self) -> VisionTickContext:
        """Push an immutable VisionTickContext snapshot atomically to HexVisionWorker (<0.1ms)."""
        sm = getattr(self, "sm", None)
        if sm is not None and hasattr(sm, "prune_suppressed_visual_items"):
            active_supp_items = frozenset(sm.prune_suppressed_visual_items())
        else:
            active_supp_items = frozenset(getattr(sm, "suppressed_visual_items", ()) or ())
        active_supp_augs = frozenset(getattr(sm, "suppressed_visual_augments", ()) or ())
        exclude_rects: tuple[tuple[int, int, int, int], ...] = ()
        try:
            exclude_rects = tuple(self._get_all_overlay_exclude_rects())
        except Exception:
            exclude_rects = ()
        cfg = getattr(self, "cfg", None)
        ctx = VisionTickContext(
            vision_gen=int(getattr(self, "_vision_gen", 0) or 0),
            game_state=sm.state.value if sm is not None else GameState.IDLE.value,
            hero_id=str(self._active_hero_id() or "") if sm is not None else "",
            game_time_s=float(self._current_game_time_s() or 0.0),
            selected_augments=tuple(sm.selected_augments) if sm is not None else (),
            is_degraded_2999=bool(sm.is_degraded_2999) if sm is not None else False,
            pending_transmute_active=bool(sm.pending_transmute is not None) if sm is not None else False,
            exclude_rects=exclude_rects,
            hud_scale=float(getattr(self, "_hud_scale", 1.0) or 1.0),
            suppressed_visual_items=active_supp_items,
            suppressed_visual_augments=active_supp_augs,
            devlab_active=bool(getattr(self, "_devlab_active", False)),
            devlab_roi_snapshots=bool(getattr(self._devlab, "enable_roi_snapshots", False)),
            card_band_roi=getattr(cfg, "card_band_roi", None),
            title_band_roi=getattr(cfg, "title_band_roi", None),
            cursor_pos=None,
            pending_transmute=sm.pending_transmute if sm is not None else None,
            active_cards=tuple(sm.active_cards) if sm is not None else (),
        )
        worker = getattr(self, "_vision_worker", None)
        if worker is not None:
            worker.update_context(ctx)
        return ctx

    def _capture_picker_slot_refs(
        self,
        frame_bgr: np.ndarray,
        game_rect,
        cards: tuple[DetectedCard, ...],
    ) -> None:
        """Snapshot tiny 48x16 grayscale strips of each detected card title for 60ms vanish/reroll checks."""
        if frame_bgr is None or frame_bgr.size == 0 or len(cards) < 2:
            return
        fh, fw = frame_bgr.shape[:2]
        refs: dict[int, tuple[tuple[int, int, int, int], np.ndarray]] = {}
        min_l, min_t, max_r, max_b = 10**9, 10**9, 0, 0
        for c in cards:
            l, t, r, b = c.screen_box
            x0 = max(0, min(fw - 1, l - game_rect.left - 8))
            y0 = max(0, min(fh - 1, t - game_rect.top - 6))
            x1 = max(x0 + 8, min(fw, r - game_rect.left + 8))
            y1 = max(y0 + 8, min(fh, b - game_rect.top + 6))
            crop = frame_bgr[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            thumb = cv2.resize(gray, (48, 16), interpolation=cv2.INTER_AREA)
            scr_box = (game_rect.left + x0, game_rect.top + y0, game_rect.left + x1, game_rect.top + y1)
            refs[c.slot_index] = (scr_box, thumb)
            min_l = min(min_l, scr_box[0])
            min_t = min(min_t, scr_box[1])
            max_r = max(max_r, scr_box[2])
            max_b = max(max_b, scr_box[3])
        if len(refs) >= 2 and max_r > min_l and max_b > min_t:
            self._picker_slot_refs = refs
            self._picker_ref_band_bbox = (min_l, min_t, max_r, max_b)

    def _handle_fast_picker_vanish(self, ts: float, diffs: dict[int, float]) -> None:
        """Freeze ClickTracker and cancel dying picker AI requests immediately on 60ms vanish."""
        if self.sm.state != GameState.AUGMENT_PICKING or not self.sm.active_cards:
            return
        if self._visual_vanish_mono is not None:
            return
        self._visual_vanish_mono = ts
        self._last_fast_vanish_diffs = dict(diffs)
        # WS-3: Immediately cancel picker debounce & dying in-flight augment_picking HTTP call!
        self._cancel_inflight_picker_coach()
        note_diffs_fn = getattr(self.click_tracker, "note_fast_vanish_diffs", None)
        if callable(note_diffs_fn):
            note_diffs_fn(diffs, vanish_ts=ts)
        valid_slots = {c.slot_index for c in self.sm.active_cards}
        try:
            self._frozen_pick_slot, self._frozen_pick_source = self.click_tracker.resolve_selection(
                close_ts=ts,
                valid_slots=valid_slots,
                rerolled_slot=self._last_rerolled_slot,
                reroll_ts=self._last_reroll_mono,
                game_rect=getattr(self, "_last_picker_game_rect", None),
                fast_vanish_diffs=diffs,
                reroll_y_threshold=getattr(self, "_last_picker_reroll_y", None),
            )
        except TypeError:
            self._frozen_pick_slot, self._frozen_pick_source = self.click_tracker.resolve_selection(
                close_ts=ts,
                valid_slots=valid_slots,
                rerolled_slot=self._last_rerolled_slot,
                reroll_ts=self._last_reroll_mono,
            )
        self._frozen_pick_confidence = float(getattr(self.click_tracker, "last_resolve_confidence", 0.0))
        if self._frozen_pick_slot is not None:
            cname = next(
                (c.augment.name for c in self.sm.active_cards if c.slot_index == self._frozen_pick_slot),
                str(self._frozen_pick_slot),
            )
            logger.info(
                "Augment pick frozen via %s (fast-60ms): slot=%s (%s)",
                self._frozen_pick_source,
                self._frozen_pick_slot,
                cname,
            )

    def _handle_fast_picker_reroll(self, ts: float, rslot: int) -> None:
        """Record a single-slot visual reroll caught by the 60ms worker band-grab probe."""
        if self.sm.state != GameState.AUGMENT_PICKING or not self.sm.active_cards:
            return
        if rslot != self._last_rerolled_slot or (ts - self._last_reroll_mono) > 0.45:
            self._last_rerolled_slot = rslot
            self._last_reroll_mono = ts
            self.click_tracker.mark_reroll(rslot, now_mono=ts)
            note_reroll_fn = getattr(self.click_tracker, "note_reroll_event", None)
            if callable(note_reroll_fn):
                note_reroll_fn(ts_mono=ts, slot=rslot)

    def _check_fast_picker_vanish(self, ts: float) -> None:
        """Legacy sync fallback helper for direct unit tests (production uses HexVisionWorker 60ms probe)."""
        if not self._picker_slot_refs or self._picker_ref_band_bbox is None or self._visual_vanish_mono is not None:
            return
        bl, bt, br, bb = self._picker_ref_band_bbox
        cap = self.capturer.grab(region=(bl, bt, br, bb))
        if cap.is_black or cap.frame_bgr is None or cap.frame_bgr.size == 0:
            return
        bh, bw = cap.frame_bgr.shape[:2]
        diffs: dict[int, float] = {}
        for slot, (sbox, ref_thumb) in self._picker_slot_refs.items():
            x0 = max(0, min(bw - 1, sbox[0] - bl))
            y0 = max(0, min(bh - 1, sbox[1] - bt))
            x1 = max(x0 + 4, min(bw, sbox[2] - bl))
            y1 = max(y0 + 4, min(bh, sbox[3] - bt))
            sub = cap.frame_bgr[y0:y1, x0:x1]
            if sub.size == 0:
                continue
            cur = cv2.resize(cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY), (48, 16), interpolation=cv2.INTER_AREA)
            diffs[slot] = float(np.mean(np.abs(cur.astype(np.int16) - ref_thumb.astype(np.int16))))
        if len(diffs) < 2:
            return
        changed = [s for s, d in diffs.items() if d >= 22.0]
        if len(changed) >= 2 or len(changed) == len(diffs):
            self._handle_fast_picker_vanish(ts, diffs)
        elif len(changed) == 1 and all(d < 16.0 for s, d in diffs.items() if s != changed[0]):
            self._handle_fast_picker_reroll(ts, changed[0])

    def _tick_click_sample(self) -> None:
        """33Hz strictly non-blocking cursor + key state sampler (<0.3ms, zero screen capture on main thread)."""
        if self.sm.state != GameState.AUGMENT_PICKING or not self.sm.active_cards:
            self._click_timer.stop()
            return
        game_rect = find_game_window() or get_primary_screen_rect()
        card_boxes = {c.slot_index: c.screen_box for c in self.sm.active_cards}
        hit_boxes = build_slot_hit_boxes(game_rect, card_boxes)
        reroll_y = int(game_rect.top + game_rect.height * 0.77)
        self._last_picker_game_rect = game_rect
        self._last_picker_reroll_y = reroll_y
        ts = time.monotonic()
        prev_tick = float(getattr(self, "_click_last_tick_mono", 0.0) or 0.0)
        if prev_tick > 0.0:
            gap_ms = (ts - prev_tick) * 1000.0
            if gap_ms > float(getattr(self, "_click_max_gap_ms_in_picker", 0.0) or 0.0):
                self._click_max_gap_ms_in_picker = gap_ms
        self._click_last_tick_mono = ts
        prev = self.click_tracker.last_clicked_slot
        try:
            slot = self.click_tracker.poll(hit_boxes, now_mono=ts, reroll_y_threshold=reroll_y, game_rect=game_rect)
        except TypeError:
            slot = self.click_tracker.poll(hit_boxes, now_mono=ts, reroll_y_threshold=reroll_y)
        if slot is not None and slot != prev:
            chosen_name = next((c.augment.name for c in self.sm.active_cards if c.slot_index == slot), str(slot))
            logger.info("Augment picker click tracked: slot=%s (%s) pos=%s", slot, chosen_name, self.click_tracker.last_click_pos)
        self._click_sample_counter += 1

    def _on_vision_worker_output(self, output: VisionFrameOutput) -> None:
        """Main-thread (<5ms) consumer of immutable VisionFrameOutput emitted by HexVisionWorker."""
        if getattr(self, "_stop_event", None) is not None and self._stop_event.is_set():
            return
        if int(output.vision_gen) != int(getattr(self, "_vision_gen", 0) or 0):
            return

        t_cb0 = time.perf_counter()
        devlab_on = bool(getattr(self, "_devlab_active", False))

        if output.capture_backend:
            self._vision_backend = output.capture_backend
        self._vision_consec_black = int(output.consecutive_black_frames or 0)
        if output.picker_result is not None:
            self._vision_ocr_ready = True

        # 1. Fast 60ms vanish / single-slot reroll callback (zero wait for 350ms OCR tick!)
        if output.fast_vanish_only:
            if output.fast_vanish_mono is not None:
                self._handle_fast_picker_vanish(
                    ts=float(output.fast_vanish_mono),
                    diffs=dict(output.fast_vanish_diffs or {}),
                )
            elif output.fast_reroll_slot is not None:
                self._handle_fast_picker_reroll(
                    ts=float(output.now_mono),
                    rslot=int(output.fast_reroll_slot),
                )
            return

        # 2. Degraded-mode visual gray-screen death detection (WS-6)
        if self.sm.is_degraded_2999 and output.visual_is_dead is not None:
            prev_vis = getattr(self, "_visual_is_dead", None)
            new_vis = bool(output.visual_is_dead)
            self._visual_is_dead = new_vis
            if prev_vis != new_vis and self._last_live is not None:
                self._apply_live_snapshot(self._last_live, degraded_2999=True)

        # 3. Manual Plan B rescan completion (and optional pending manual_f6 resume!)
        if output.plan_b_forced:
            if output.error == "no_game_window":
                self._refresh_degraded_sync_bar("未检测到游戏画面，请切回游戏后重试")
                if getattr(self, "_pending_manual_f6_after_plan_b", False):
                    self._pending_manual_f6_after_plan_b = False
                    self.trigger_coach_request("manual_f6", _skip_plan_b_prescan=True)
                return
            if output.error == "black_frame" or output.is_black:
                self._refresh_degraded_sync_bar("截帧失败，请确保游戏处于无边框窗口模式")
                if getattr(self, "_pending_manual_f6_after_plan_b", False):
                    self._pending_manual_f6_after_plan_b = False
                    self.trigger_coach_request("manual_f6", _skip_plan_b_prescan=True)
                return
            items_added, augs_added = self._apply_plan_b_hits(
                output.plan_b_item_hits,
                output.plan_b_item_states,
                output.plan_b_aug_hits,
                force_confirm=True,
            )
            total_items = len(self.sm.effective_degraded_items())
            total_augs = len(self.sm.selected_augments)
            self._refresh_degraded_sync_bar(
                f"扫描完成：已同步 {total_items} 件装备 / {total_augs} 个海克斯（本次新增 +{items_added}装/+{augs_added}海克斯）"
            )
            if getattr(self, "_pending_manual_f6_after_plan_b", False):
                self._pending_manual_f6_after_plan_b = False
                self.trigger_coach_request("manual_f6", _skip_plan_b_prescan=True)
            if output.picker_result is None:
                return

        game_rect = output.game_rect
        res_str = f"{int(game_rect.width)}x{int(game_rect.height)}" if game_rect is not None else ""
        timings = dict(output.timings_ms or {})
        capture_ms = float(timings.get("capture_ms", 0.0) or 0.0)
        cur_backend = str(output.capture_backend or self._vision_backend or "")
        cur_black = bool(output.is_black)

        if devlab_on and (
            cur_backend != getattr(self, "_last_capture_backend", "")
            or cur_black != bool(getattr(self, "_last_capture_black", False))
        ):
            prev_be = str(getattr(self, "_last_capture_backend", "") or "")
            self._last_capture_backend = cur_backend
            self._last_capture_black = cur_black
            self._emit_devlab(
                "vision",
                "vision.capture.state_change",
                {
                    "backend": cur_backend,
                    "prev_backend": prev_be,
                    "is_black": cur_black,
                    "consecutive_black_frames": int(output.consecutive_black_frames or 0),
                    "resolution": res_str,
                    "capture_ms": round(capture_ms, 2),
                },
            )
        if cur_black:
            if int(output.consecutive_black_frames or 0) >= 5:
                self.edge_drawer.set_warning("检测到游戏画面全黑，请将游戏视频模式设为「无边框窗口」")
            if devlab_on:
                self._devlab.record_latency_sample("tick_vision", (time.perf_counter() - t_cb0) * 1000.0)
            return
        if int(output.consecutive_black_frames or 0) == 0:
            self.edge_drawer.set_warning("")

        if output.picker_result is None or game_rect is None:
            return

        self._apply_vision_frame_results(
            res=output.picker_result,
            game_rect=game_rect,
            now_mono=float(output.now_mono),
            force=False,
            capture_ms=capture_ms,
            timings=timings,
            roi_crops=dict(output.roi_crops or {}),
            transmute_hits=list(output.transmute_hits or []),
            passive_hover_hit=output.passive_hover_hit,
            transmute_trace=dict(output.transmute_trace or {}),
            plan_b_ran=bool(output.plan_b_ran),
            plan_b_item_hits=list(output.plan_b_item_hits or []),
            plan_b_item_states=dict(output.plan_b_item_states or {}),
            plan_b_aug_hits=list(output.plan_b_aug_hits or []),
            frame_bgr=None,
            t_main0=t_cb0,
            from_worker=True,
        )

    def _tick_vision(self, force: bool = False) -> None:
        worker = getattr(self, "_vision_worker", None)
        if (
            worker is not None
            and worker.is_running()
            and id(self.capturer) == getattr(self, "_orig_capturer_id", -1)
            and self.detector is None
        ):
            if not self.store.is_loaded:
                return
            self._push_vision_context()
            if force:
                worker.enqueue_command("force_rescan")
            return
        self._run_vision_cycle_sync(force=force, exclude_rects=self._get_all_overlay_exclude_rects())

    def _run_vision_cycle_sync(
        self,
        *,
        force: bool = False,
        exclude_rects: list[tuple[int, int, int, int]] | None = None,
    ) -> None:
        """Synchronous vision tick fallback used by deterministic unit tests and non-worker mode."""
        if not self.store.is_loaded or self.detector is None:
            return
        if not force and self.sm.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            return

        game_rect = find_game_window()
        if game_rect is None:
            if not force:
                return
            game_rect = get_primary_screen_rect()
        elif not force and not self._is_game_or_overlay_foreground(game_rect.hwnd):
            return

        t_tick0 = time.perf_counter()
        devlab_on = bool(getattr(self, "_devlab_active", False))
        res_str = f"{int(game_rect.width)}x{int(game_rect.height)}"
        now_mono = time.monotonic()
        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards:
            card_boxes = {c.slot_index: c.screen_box for c in self.sm.active_cards}
            reroll_y = int(game_rect.top + game_rect.height * 0.77)
            self._last_picker_game_rect = game_rect
            self._last_picker_reroll_y = reroll_y
            try:
                self.click_tracker.poll(
                    build_slot_hit_boxes(game_rect, card_boxes),
                    now_mono=now_mono,
                    reroll_y_threshold=reroll_y,
                    game_rect=game_rect,
                )
            except TypeError:
                self.click_tracker.poll(
                    build_slot_hit_boxes(game_rect, card_boxes),
                    now_mono=now_mono,
                    reroll_y_threshold=reroll_y,
                )

        t_cap0 = time.perf_counter()
        cap = self.capturer.grab(region=game_rect.as_bbox())
        capture_ms = (time.perf_counter() - t_cap0) * 1000.0
        cur_backend = str(getattr(self.capturer, "backend", "") or "")
        cur_black = bool(cap.is_black or cap.frame_bgr is None)
        if devlab_on and (
            cur_backend != getattr(self, "_last_capture_backend", "")
            or cur_black != bool(getattr(self, "_last_capture_black", False))
        ):
            prev_be = str(getattr(self, "_last_capture_backend", "") or "")
            self._last_capture_backend = cur_backend
            self._last_capture_black = cur_black
            self._emit_devlab(
                "vision",
                "vision.capture.state_change",
                {
                    "backend": cur_backend,
                    "prev_backend": prev_be,
                    "is_black": cur_black,
                    "consecutive_black_frames": int(getattr(self.capturer, "consecutive_black_frames", 0) or 0),
                    "resolution": res_str,
                    "capture_ms": round(capture_ms, 2),
                },
            )
        if cur_black:
            if self.capturer.consecutive_black_frames >= 5:
                self.edge_drawer.set_warning("检测到游戏画面全黑，请将游戏视频模式设为「无边框窗口」")
            if devlab_on:
                self._devlab.record_latency_sample("tick_vision", (time.perf_counter() - t_tick0) * 1000.0)
            return
        if self.capturer.consecutive_black_frames == 0:
            self.edge_drawer.set_warning("")

        excl = list(exclude_rects) if exclude_rects is not None else self._get_all_overlay_exclude_rects()
        t_det0 = time.perf_counter()
        if devlab_on:
            try:
                res = self.detector.process_frame(
                    cap.frame_bgr,
                    game_rect=game_rect,
                    exclude_rects=excl,
                    force=force,
                    capture_ms=capture_ms,
                    capture_crops=bool(getattr(self._devlab, "enable_roi_snapshots", False)),
                )
            except TypeError:
                res = self.detector.process_frame(
                    cap.frame_bgr,
                    game_rect=game_rect,
                    exclude_rects=excl,
                    force=force,
                )
        else:
            res = self.detector.process_frame(
                cap.frame_bgr,
                game_rect=game_rect,
                exclude_rects=excl,
                force=force,
            )
        detect_ms = (time.perf_counter() - t_det0) * 1000.0
        diag_raw = getattr(res, "diagnostics", None)
        roi_crops = diag_raw.pop("_roi_crops", None) if isinstance(diag_raw, dict) else None

        transmute_hits: list[Any] = []
        passive_hover_hit: Any = None
        transmute_trace: dict[str, Any] = {}
        passive_hover_ms = 0.0
        if (
            self.sm.state == GameState.IN_GAME
            and not res.picker_visible
            and len(res.cards) == 0
            and self.transmute_watcher is not None
        ):
            if self.sm.pending_transmute is not None:
                t_tr0 = time.perf_counter()
                transmute_hits = self.transmute_watcher.process_frame(
                    cap.frame_bgr,
                    game_rect=game_rect,
                    pending=self.sm.pending_transmute,
                    already_owned_ids=set(self.sm.selected_augments),
                    exclude_rects=excl,
                    cursor_pos=get_cursor_pos(),
                    now_mono=now_mono,
                )
                passive_hover_ms = (time.perf_counter() - t_tr0) * 1000.0
                tr_fn = getattr(self.transmute_watcher, "last_trace_diagnostics", None)
                if callable(tr_fn):
                    transmute_trace = dict(tr_fn())
            elif len(self.sm.selected_augments) < MAX_AUGMENT_SLOTS:
                t_hov0 = time.perf_counter()
                passive_hover_hit = self.transmute_watcher.scan_passive_hud_hover(
                    cap.frame_bgr,
                    game_rect=game_rect,
                    already_owned_ids=set(self.sm.selected_augments),
                    exclude_rects=excl,
                    cursor_pos=get_cursor_pos(),
                    now_mono=now_mono,
                )
                passive_hover_ms = (time.perf_counter() - t_hov0) * 1000.0
                tr_fn = getattr(self.transmute_watcher, "last_trace_diagnostics", None)
                if callable(tr_fn):
                    transmute_trace = dict(tr_fn())

        plan_b_ms = 0.0
        plan_b_ran = False
        plan_b_item_hits: list[Any] = []
        plan_b_item_states: dict[int, str] = {}
        plan_b_aug_hits: list[Any] = []
        cur_gt_s = float(self._current_game_time_s() or 0.0)
        if (
            self.sm.state == GameState.IN_GAME
            and self.sm.pending_transmute is None
            and not res.picker_visible
            and cur_gt_s >= 10.0
            and (self.sm.is_degraded_2999 or len(self.sm.selected_augments) == 0)
            and (now_mono - float(getattr(self, "_last_plan_b_scan_mono", 0.0) or 0.0)) >= 2.5
        ):
            self._last_plan_b_scan_mono = now_mono
            t_pb0 = time.perf_counter()
            if self.sm.is_degraded_2999 and self.item_icon_matcher is not None:
                if self.item_icon_matcher.template_count == 0:
                    self.item_icon_matcher.reload_cached_icons()
                if self.item_icon_matcher.template_count > 0:
                    plan_b_item_hits, plan_b_item_states = self.item_icon_matcher.scan_hud_slots_with_state(
                        cap.frame_bgr,
                        game_rect,
                        min_score=0.62,
                        min_margin=0.03,
                        hud_scale=self._hud_scale,
                    )
            if len(self.sm.selected_augments) < MAX_AUGMENT_SLOTS and self.icon_matcher is not None and self.icon_matcher.template_count > 0:
                plan_b_aug_hits = self.icon_matcher.scan_all_hud_augments(
                    cap.frame_bgr,
                    game_rect,
                    min_score=0.62,
                    min_margin=0.04,
                    hud_scale=self._hud_scale,
                )
            plan_b_ms = (time.perf_counter() - t_pb0) * 1000.0
            plan_b_ran = True

        self._apply_vision_frame_results(
            res=res,
            game_rect=game_rect,
            now_mono=now_mono,
            force=force,
            capture_ms=capture_ms,
            timings={
                "capture_ms": round(capture_ms, 3),
                "detect_ms": round(detect_ms, 3),
                "plan_b_ms": round(plan_b_ms, 3),
                "passive_hover_ms": round(passive_hover_ms, 3),
            },
            roi_crops=dict(roi_crops) if isinstance(roi_crops, dict) else {},
            transmute_hits=transmute_hits,
            passive_hover_hit=passive_hover_hit,
            transmute_trace=transmute_trace,
            plan_b_ran=plan_b_ran,
            plan_b_item_hits=plan_b_item_hits,
            plan_b_item_states=plan_b_item_states,
            plan_b_aug_hits=plan_b_aug_hits,
            frame_bgr=cap.frame_bgr,
            t_main0=t_tick0,
            from_worker=False,
        )

    def _apply_vision_frame_results(
        self,
        *,
        res: PickerDetectionResult,
        game_rect: Any,
        now_mono: float,
        force: bool,
        capture_ms: float,
        timings: dict[str, float],
        roi_crops: dict[str, Any],
        transmute_hits: list[Any],
        passive_hover_hit: Any,
        transmute_trace: dict[str, Any],
        plan_b_ran: bool,
        plan_b_item_hits: list[Any],
        plan_b_item_states: dict[int, str],
        plan_b_aug_hits: list[Any],
        frame_bgr: np.ndarray | None,
        t_main0: float,
        from_worker: bool,
    ) -> None:
        devlab_on = bool(getattr(self, "_devlab_active", False))
        res_str = f"{int(game_rect.width)}x{int(game_rect.height)}"
        was_picking = self.sm.state == GameState.AUGMENT_PICKING
        diag: dict[str, Any] = dict(getattr(res, "diagnostics", None) or {})
        diag.pop("_roi_crops", None)

        if devlab_on and (res.picker_visible or was_picking or force or len(res.cards) >= 1):
            diag_slots = list(diag.get("slots") or [])
            near_misses = list(diag.get("near_misses") or [])
            shadowed_p2 = list(diag.get("shadowed_pass2") or [])
            rec_failed = list(diag.get("recovery_failed_slots") or [])
            low_conf_slots = [c.slot_index for c in res.cards if float(getattr(c, "confidence", 1.0)) < 0.72]
            unmatched_slots = [
                int(s.get("slot_idx", s.get("slot", -1)))
                for s in diag_slots
                if isinstance(s, dict)
                and (not s.get("matched_id") or s.get("matched") is False or s.get("low_conf") is True)
            ]
            anomaly_slots = sorted({s for s in (*low_conf_slots, *unmatched_slots, *rec_failed) if isinstance(s, int) and s >= 0})
            all_slots_high_conf = (
                len(res.cards) >= 3
                and not anomaly_slots
                and not rec_failed
                and all(float(getattr(c, "confidence", 0.0)) >= 0.78 for c in res.cards)
            )
            has_anomaly = bool(anomaly_slots or shadowed_p2 or rec_failed or (near_misses and not all_slots_high_conf))
            if has_anomaly:
                dedupe_map = getattr(self, "_last_ocr_anomaly_mono", None)
                emit_keys = anomaly_slots if anomaly_slots else [-1]
                should_emit_ocr = bool(force or res.rerolled)
                if not should_emit_ocr and isinstance(dedupe_map, dict):
                    should_emit_ocr = any((now_mono - dedupe_map.get(k, 0.0)) >= 10.0 for k in emit_keys)
                if should_emit_ocr:
                    if isinstance(dedupe_map, dict):
                        for k in emit_keys:
                            dedupe_map[k] = now_mono
                    ev_seq = self._emit_devlab(
                        "vision",
                        "vision.ocr.low_conf_or_nearmiss",
                        {
                            "ocr_language_tag": str(diag.get("ocr_language_tag") or getattr(self.ocr_engine, "language_tag", "") or ""),
                            "chosen_layout": str(diag.get("chosen_layout") or ""),
                            "slots": diag_slots,
                            "near_misses": near_misses,
                            "shadowed_pass2": shadowed_p2,
                            "recovery_failed_slots": rec_failed,
                            "recovered_slots": list(diag.get("recovered_slots") or []),
                            "low_conf_slots": low_conf_slots,
                            "resolution": res_str,
                            "force": bool(force),
                            "rerolled": bool(res.rerolled),
                        },
                    )
                    # WS-7: Skip description-line ROIs on 3/3 high-confidence frames & break immediately at cap (15)
                    if (
                        isinstance(roi_crops, dict)
                        and getattr(self._devlab, "enable_roi_snapshots", False)
                        and not all_slots_high_conf
                    ):
                        enq_cnt = 0
                        for crop_tag, crop_img in roi_crops.items():
                            if enq_cnt >= 2:
                                break
                            enq_res = self._devlab.enqueue_roi_snapshot(str(crop_tag), crop_img, ev_seq=ev_seq)
                            if enq_res is None:
                                st = self._devlab.get_status_snapshot()
                                if int(st.get("roi_count", 0) or 0) >= int(st.get("roi_cap", 15) or 15):
                                    break
                                continue
                            enq_cnt += 1

        # P0-2: Disjoint back-to-back augment rounds (e.g. 3094 double-offer):
        # Resolve & commit Round A cleanly and reset transient picker runtime BEFORE Round B opens.
        old_cards = self.sm.active_cards
        old_cards_by_slot = {c.slot_index: c.augment.augment_id for c in old_cards}
        prev_closed_since = self.sm._picker_closed_since
        resolved_slot: int | None = None
        if (
            was_picking
            and old_cards
            and res.picker_visible
            and res.stable
            and res.cards
            and not self.sm._is_stat_anvil_offer(res.cards)
            and not self.sm._is_bonus_event_offer(res.cards)
        ):
            old_ids_set = {c.augment.augment_id for c in old_cards if c.augment.augment_id}
            new_ids_set = {c.augment.augment_id for c in res.cards if c.augment.augment_id}
            if len(old_ids_set) >= 2 and len(new_ids_set) >= 2 and not (old_ids_set & new_ids_set):
                prev_slot = self._frozen_pick_slot
                prev_source = self._frozen_pick_source
                prev_conf = self._frozen_pick_confidence
                if prev_slot is None:
                    prev_valid_slots = {c.slot_index for c in old_cards}
                    fallback_close = max(self._last_cards_seen_mono, now_mono - 0.14) if self._last_cards_seen_mono > 0 else now_mono
                    est_close = self._visual_vanish_mono if self._visual_vanish_mono is not None else fallback_close
                    vanish_diffs = getattr(self, "_last_fast_vanish_diffs", None) or None
                    try:
                        prev_slot, prev_source = self.click_tracker.resolve_selection(
                            close_ts=est_close,
                            valid_slots=prev_valid_slots,
                            rerolled_slot=self._last_rerolled_slot,
                            reroll_ts=self._last_reroll_mono,
                            game_rect=game_rect,
                            fast_vanish_diffs=vanish_diffs,
                            reroll_y_threshold=getattr(self, "_last_picker_reroll_y", None),
                        )
                    except TypeError:
                        prev_slot, prev_source = self.click_tracker.resolve_selection(
                            close_ts=est_close,
                            valid_slots=prev_valid_slots,
                            rerolled_slot=self._last_rerolled_slot,
                            reroll_ts=self._last_reroll_mono,
                        )
                    prev_conf = float(getattr(self.click_tracker, "last_resolve_confidence", 0.0))
                    if prev_slot is None:
                        lc_slot = getattr(self.click_tracker, "last_clicked_slot", None)
                        if lc_slot in prev_valid_slots:
                            prev_slot = lc_slot
                            prev_source = prev_source or "click"
                            prev_conf = max(prev_conf, 0.85)
                self.sm._picker_closed_since = now_mono - self.sm.PICKER_CLOSE_DEBOUNCE_S - 0.01
                close_eff = self.sm.on_picker_result(
                    PickerDetectionResult(
                        picker_visible=False,
                        stable=False,
                        rerolled=False,
                        cards=(),
                        cards_seen_on_frame=False,
                    ),
                    clicked_slot=prev_slot,
                    now_mono=now_mono,
                )
                self._finalize_picker_close_commit(
                    eff=close_eff,
                    resolved_slot=prev_slot,
                    pick_conf=prev_conf,
                    pick_source=prev_source or ("click" if prev_slot is not None else "none"),
                    res_str=res_str,
                    now_mono=now_mono,
                    back_to_back=True,
                )
                if close_eff.newly_selected_augment:
                    self._last_drawer_refresh_sig = None
                    self._refresh_drawer_view()
                if close_eff.coach_trigger and self.cfg.auto_trigger_coach and self._is_any_coach_online():
                    self.trigger_coach_request(close_eff.coach_trigger)
                was_picking = False
                old_cards_by_slot = {}
                prev_closed_since = None
                self._frozen_pick_slot = None
                self._frozen_pick_source = ""
                self._frozen_pick_confidence = 0.0
                self._last_offer_rec_snapshot = None
                self._last_offer_emitted_sig = None
                self._pending_reroll_prev_offer = None
                self._pending_reroll_slots = None

        # Freeze click / cursor-dwell selection AT THE EXACT FIRST FRAME cards vanish (before cursor moves to lane!)
        if was_picking and self.sm.active_cards:
            if res.picker_visible and res.cards_seen_on_frame:
                self._last_cards_seen_mono = now_mono
                self._visual_vanish_mono = None
                self._last_fast_vanish_diffs = {}
                self._frozen_pick_slot = None
                self._frozen_pick_source = ""
                self._frozen_pick_confidence = 0.0
                if res.cards and frame_bgr is not None:
                    self._capture_picker_slot_refs(frame_bgr, game_rect, res.cards)
                resolved_slot = self.click_tracker.last_clicked_slot
            else:
                if self._frozen_pick_slot is None:
                    valid_slots = {c.slot_index for c in self.sm.active_cards}
                    fallback_close = max(self._last_cards_seen_mono, now_mono - 0.14) if self._last_cards_seen_mono > 0 else now_mono
                    est_close = self._visual_vanish_mono if self._visual_vanish_mono is not None else fallback_close
                    vanish_diffs = getattr(self, "_last_fast_vanish_diffs", None) or None
                    try:
                        self._frozen_pick_slot, self._frozen_pick_source = self.click_tracker.resolve_selection(
                            close_ts=est_close,
                            valid_slots=valid_slots,
                            rerolled_slot=self._last_rerolled_slot,
                            reroll_ts=self._last_reroll_mono,
                            game_rect=game_rect,
                            fast_vanish_diffs=vanish_diffs,
                            reroll_y_threshold=getattr(self, "_last_picker_reroll_y", None),
                        )
                    except TypeError:
                        self._frozen_pick_slot, self._frozen_pick_source = self.click_tracker.resolve_selection(
                            close_ts=est_close,
                            valid_slots=valid_slots,
                            rerolled_slot=self._last_rerolled_slot,
                            reroll_ts=self._last_reroll_mono,
                        )
                    self._frozen_pick_confidence = float(getattr(self.click_tracker, "last_resolve_confidence", 0.0))
                    if self._frozen_pick_slot is not None:
                        cname = next(
                            (c.augment.name for c in self.sm.active_cards if c.slot_index == self._frozen_pick_slot),
                            str(self._frozen_pick_slot),
                        )
                        logger.info(
                            "Augment pick frozen via %s (conf=%.2f): slot=%s (%s)",
                            self._frozen_pick_source,
                            self._frozen_pick_confidence,
                            self._frozen_pick_slot,
                            cname,
                        )
                resolved_slot = self._frozen_pick_slot

        eff = self.sm.on_picker_result(res, clicked_slot=resolved_slot, now_mono=now_mono)

        if eff.phantom_rolled_back_id:
            src_map = getattr(self, "_aug_source_by_id", None)
            if isinstance(src_map, dict):
                src_map.pop(str(eff.phantom_rolled_back_id), None)
            if self.transmute_watcher is not None and self.sm.pending_transmute is None:
                self.transmute_watcher.reset()
            self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
            self._last_drawer_refresh_sig = None
            self._save_match_state()
            self._refresh_drawer_view()
            self._push_recent_unrecorded_augments()
            if devlab_on:
                self._emit_devlab(
                    "vision",
                    "vision.pick.phantom_rollback",
                    {
                        "rolled_back_augment_id": str(eff.phantom_rolled_back_id),
                        "elapsed_s": round(float(eff.phantom_rollback_elapsed_s), 3),
                        "restored_plan": list(self.sm.committed_plan),
                        "restored_locked_item": str(self.sm.locked_next_item or ""),
                        "remaining_selected_count": len(self.sm.selected_augments),
                    },
                )
            logger.info(
                "Phantom picker rollback reverted augment %s (%.1fs <= %.0fs); restored plan=%s",
                eff.phantom_rolled_back_id,
                eff.phantom_rollback_elapsed_s,
                self.sm.PHANTOM_ROLLBACK_WINDOW_S,
                self.sm.committed_plan,
            )

        # WS-3: On the very first frame _picker_closed_since activates, cancel picker debounce & dying augment_picking HTTP call!
        if prev_closed_since is None and self.sm._picker_closed_since is not None:
            self._cancel_inflight_picker_coach()

        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards:
            self._cancel_death_coach_defer("AUGMENT_PICKING opened")
            self._cancel_augment_remove_coach_defer("AUGMENT_PICKING opened")
            self._cancel_pending_icon_verify()
            if not was_picking:
                self._push_recent_unrecorded_augments()
                self._last_cards_seen_mono = now_mono
                self._picker_opened_mono = now_mono
                self._picker_opened_since_level = True
                self._click_last_tick_mono = 0.0
                self._click_max_gap_ms_in_picker = 0.0
                self._last_offer_emitted_sig = None
                self._pending_reroll_prev_offer = None
                self._pending_reroll_slots = None
                self._active_picker_coach_req_id = ""
                self._active_picker_coach_arrived = False
                self._active_picker_coach_aug_advice = ""
                self._visual_vanish_mono = None
                self._last_fast_vanish_diffs = {}
                self._frozen_pick_slot = None
                self._frozen_pick_source = ""
                self._frozen_pick_confidence = 0.0
                self._last_rerolled_slot = None
                self._last_reroll_mono = 0.0
                self._click_sample_counter = 0
                if frame_bgr is not None:
                    self._capture_picker_slot_refs(frame_bgr, game_rect, self.sm.active_cards)
                self.click_tracker.reset(now_mono=now_mono)
                self._click_timer.start()
                self.edge_drawer.set_picker_active(True)
                # WS-6: Real physical right-edge collision check (also collapses on 4-card layouts)
                should_collapse_drawer = False
                if getattr(self.edge_drawer, "_expanded", False):
                    if len(self.sm.active_cards) >= 4:
                        should_collapse_drawer = True
                    else:
                        max_card_r = max((int(c.screen_box[2]) for c in self.sm.active_cards), default=0)
                        drawer_rect = self.edge_drawer.get_exclude_rect()
                        if drawer_rect is not None and max_card_r > 0:
                            if int(drawer_rect[0]) <= max_card_r + 24:
                                should_collapse_drawer = True
                        else:
                            log_game, _ = physical_rect_to_logical(
                                (game_rect.left, game_rect.top, game_rect.width, game_rect.height)
                            )
                            if log_game.width() < 1800:
                                should_collapse_drawer = True
                if should_collapse_drawer:
                    self._pre_picker_drawer_expanded = True
                    self._mark_overlay_close("edge_drawer", "auto_picker_collapse")
                    self.edge_drawer.set_expanded(False)
                else:
                    self._pre_picker_drawer_expanded = None
                layout_open_s = str(diag.get("chosen_layout") or "")
                self._last_picker_expected_slots = 4 if "4" in layout_open_s else (3 if layout_open_s else 0)
                self._last_picker_partial_open = bool(diag.get("partial_open", False))
                if devlab_on:
                    self._devlab.set_context(game_state=self.sm.state.value)
                    if self._action_tracker is not None:
                        self._action_tracker.note_picker_opened(float(self._current_game_time_s() or 0.0))
                    self._emit_devlab(
                        "vision",
                        "vision.picker.transition",
                        {
                            "transition": "open",
                            "is_open": True,
                            "chosen_layout": str(diag.get("chosen_layout") or ""),
                            "card_count": len(self.sm.active_cards),
                            "cards": [
                                {
                                    "slot": c.slot_index,
                                    "augment_id": c.augment.augment_id,
                                    "name": c.augment.name,
                                    "rarity": c.augment.rarity,
                                    "confidence": round(float(c.confidence), 3),
                                }
                                for c in self.sm.active_cards
                            ],
                            "partial_open": bool(diag.get("partial_open", len(self.sm.active_cards) < 3)),
                            "recovered_slots": list(diag.get("recovered_slots") or []),
                            "recovery_failed_slots": list(diag.get("recovery_failed_slots") or []),
                            "slots": list(diag.get("slots") or []),
                            "near_misses": list(diag.get("near_misses") or []),
                            "shadowed_pass2": list(diag.get("shadowed_pass2") or []),
                            "resolution": res_str,
                        },
                    )
                logger.info(
                    "Augment picker opened: %s",
                    [(c.slot_index, c.augment.name) for c in self.sm.active_cards],
                )
                self._push_vision_context()
            elif res.rerolled and res.stable:
                self._last_cards_seen_mono = now_mono
                self._visual_vanish_mono = None
                self._last_fast_vanish_diffs = {}
                self._frozen_pick_slot = None
                self._frozen_pick_source = ""
                self._frozen_pick_confidence = 0.0
                changed_slots = [
                    c.slot_index
                    for c in self.sm.active_cards
                    if old_cards_by_slot.get(c.slot_index) != c.augment.augment_id
                ]
                if len(changed_slots) == 1:
                    self._last_rerolled_slot = changed_slots[0]
                    self._last_reroll_mono = now_mono
                if frame_bgr is not None:
                    self._capture_picker_slot_refs(frame_bgr, game_rect, self.sm.active_cards)
                self.click_tracker.mark_reroll(slot=self._last_rerolled_slot, now_mono=now_mono)
                note_reroll_fn = getattr(self.click_tracker, "note_reroll_event", None)
                if callable(note_reroll_fn):
                    note_reroll_fn(ts_mono=now_mono, slot=self._last_rerolled_slot)
                layout_reroll_s = str(diag.get("chosen_layout") or "")
                self._last_picker_expected_slots = 4 if "4" in layout_reroll_s else (3 if layout_reroll_s else 0)
                self._last_picker_partial_open = bool(diag.get("partial_open", False))
                if devlab_on:
                    self._pending_reroll_prev_offer = self._last_offer_rec_snapshot
                    self._pending_reroll_slots = list(changed_slots)
                    self._last_offer_emitted_sig = None
                    self._emit_devlab(
                        "vision",
                        "vision.picker.transition",
                        {
                            "transition": "reroll",
                            "is_open": True,
                            "changed_slots": list(changed_slots),
                            "rerolled_slot": self._last_rerolled_slot,
                            "chosen_layout": str(diag.get("chosen_layout") or ""),
                            "card_count": len(self.sm.active_cards),
                            "cards": [
                                {
                                    "slot": c.slot_index,
                                    "augment_id": c.augment.augment_id,
                                    "name": c.augment.name,
                                    "rarity": c.augment.rarity,
                                    "confidence": round(float(c.confidence), 3),
                                }
                                for c in self.sm.active_cards
                            ],
                            "partial_open": bool(diag.get("partial_open", len(self.sm.active_cards) < 3)),
                            "recovered_slots": list(diag.get("recovered_slots") or []),
                            "recovery_failed_slots": list(diag.get("recovery_failed_slots") or []),
                            "slots": list(diag.get("slots") or []),
                            "near_misses": list(diag.get("near_misses") or []),
                            "shadowed_pass2": list(diag.get("shadowed_pass2") or []),
                            "resolution": res_str,
                        },
                    )
                logger.info(
                    "Augment picker rerolled: %s",
                    [(c.slot_index, c.augment.name) for c in self.sm.active_cards],
                )
                self._push_recent_unrecorded_augments()
                self._push_vision_context()
            self._maybe_auto_switch_first_prismatic(self.sm.active_cards)
            if not self._overlays_hidden_by_user and not self._is_full_locked():
                self._render_augment_badges(self.sm.active_cards, game_rect)
            else:
                if self.augment_overlay.isVisible():
                    self._mark_overlay_close("augment_badges", "hidden_or_locked")
                    self.augment_overlay.hide()
                if self.combo_overlay.mode == "picker":
                    self.combo_overlay.exit_picker_mode()
        elif eff.is_bonus_event_offer and res.cards:
            if not self._overlays_hidden_by_user and not self._is_full_locked():
                self._render_augment_badges(res.cards, game_rect)
            elif self.augment_overlay.isVisible():
                self._mark_overlay_close("augment_badges", "hidden_or_locked")
                self.augment_overlay.hide()
        else:
            if self._click_timer.isActive():
                self._click_timer.stop()
            self._picker_slot_refs.clear()
            self._picker_ref_band_bbox = None
            self._visual_vanish_mono = None
            self._last_fast_vanish_diffs = {}
            self._last_rerolled_slot = None
            if self.augment_overlay.isVisible():
                self._mark_overlay_close("augment_badges", "auto_picker_close")
                self.augment_overlay.hide()
            if was_picking:
                self._finalize_picker_close_commit(
                    eff=eff,
                    resolved_slot=resolved_slot,
                    pick_conf=self._frozen_pick_confidence,
                    pick_source=self._frozen_pick_source or ("click" if resolved_slot is not None else "none"),
                    res_str=res_str,
                    now_mono=now_mono,
                    back_to_back=False,
                )
                if (
                    not eff.newly_selected_augment
                    and getattr(self, "_death_defer_superseded_by_picker", False)
                    and self._last_live is not None
                    and self._last_live.my_player is not None
                    and self._last_live.my_player.is_dead
                ):
                    self._death_defer_superseded_by_picker = False
                    if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                        self._on_death_coach_defer_timeout()

        # Secondary OCR + visual icon matching loop for Transmute / Pandora augments
        if self.sm.state == GameState.IN_GAME and self.sm.pending_transmute is not None:
            if devlab_on and (transmute_hits or transmute_trace.get("matched_ids") or float(transmute_trace.get("ocr_ms") or 0.0) > 0.0):
                td = dict(transmute_trace)
                td["resolution"] = res_str
                self._emit_devlab("vision", "vision.transmute.channel_trace", td)
            for hit in transmute_hits:
                self._on_resolve_transmute_augment(hit.augment.augment_id, source=hit.channel)
            if self.sm.pending_transmute is not None and frame_bgr is not None:
                self._try_resolve_transmute_via_icon(frame_bgr, game_rect)
            pt = self.sm.pending_transmute
            if (
                pt is not None
                and pt.resolved_ids
                and not pt.partial_coach_fired
                and (now_mono - pt.started_mono) >= 16.0
            ):
                pt.partial_coach_fired = True
                if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                    self.trigger_coach_request("augment_selected")
        elif (
            self.sm.state == GameState.IN_GAME
            and self.sm.pending_transmute is None
            and not res.picker_visible
            and len(res.cards) == 0
            and len(self.sm.selected_augments) < MAX_AUGMENT_SLOTS
        ):
            if devlab_on and (passive_hover_hit is not None or transmute_trace.get("matched_ids")):
                td = dict(transmute_trace)
                td["resolution"] = res_str
                self._emit_devlab("vision", "vision.transmute.channel_trace", td)
            if (
                passive_hover_hit is not None
                and str(passive_hover_hit.augment.augment_id) not in self.sm.suppressed_visual_augments
                and self.sm.add_selected_augment(passive_hover_hit.augment.augment_id)
            ):
                if devlab_on and isinstance(getattr(self, "_aug_source_by_id", None), dict):
                    self._aug_source_by_id[str(passive_hover_hit.augment.augment_id)] = {
                        "source": "passive_hud_hover",
                        "cursor_zone": str(getattr(passive_hover_hit, "cursor_zone", "") or "other"),
                    }
                self._flag_if_transformative_augment(passive_hover_hit.augment.augment_id)
                self._save_match_state()
                logger.info(
                    "Augment auto-synced via passive HUD hover: %s (%d/%d)",
                    passive_hover_hit.augment.name,
                    len(self.sm.selected_augments),
                    MAX_AUGMENT_SLOTS,
                )
                self._refresh_drawer_view()
                self._push_recent_unrecorded_augments()
                self.edge_drawer.flash_new_augment(passive_hover_hit.augment.augment_id)
                self._push_vision_context()
                if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                    self.trigger_coach_request("augment_selected")

        if plan_b_ran and self.sm.state == GameState.IN_GAME and not res.picker_visible:
            self._apply_plan_b_hits(
                plan_b_item_hits,
                plan_b_item_states,
                plan_b_aug_hits,
                force_confirm=False,
            )

        if eff.newly_selected_augment:
            self._last_drawer_refresh_sig = None
            self._refresh_drawer_view()

        if eff.coach_trigger and self.cfg.auto_trigger_coach and self._is_any_coach_online():
            if eff.coach_trigger == "augment_picking":
                self._schedule_picker_coach_trigger(after_reroll=bool(res.rerolled))
            else:
                self.trigger_coach_request(eff.coach_trigger)

        if devlab_on:
            callback_ms = (time.perf_counter() - t_main0) * 1000.0
            worker_total_ms = float(timings.get("total_ms", callback_ms) or callback_ms)
            self._devlab.record_latency_sample("tick_vision", callback_ms)
            if from_worker:
                self._devlab.record_latency_sample("vision_worker_tick", worker_total_ms)
            ocr_p2_ms = float(diag.get("ocr_p2_ms") or 0.0)
            is_idle_dual = bool(not res.picker_visible and not was_picking and ocr_p2_ms > 0.0)
            # WS-7: Unbiased 60s vision frame counter (records true denominator for idle_dual_pass_rate)
            win_start = float(getattr(self, "_vision_window_start_mono", 0.0) or 0.0)
            if win_start <= 0.0:
                self._vision_window_start_mono = now_mono
                win_start = now_mono
            self._vision_window_total_frames = int(getattr(self, "_vision_window_total_frames", 0) or 0) + 1
            if is_idle_dual:
                self._vision_window_idle_dual_frames = int(getattr(self, "_vision_window_idle_dual_frames", 0) or 0) + 1
            if (now_mono - win_start) >= 60.0 and self._vision_window_total_frames > 0:
                tot_f = self._vision_window_total_frames
                idle_f = self._vision_window_idle_dual_frames
                self._emit_devlab(
                    "vision",
                    "vision.pipeline.window_stats",
                    {
                        "window_s": round(now_mono - win_start, 1),
                        "total_frames": tot_f,
                        "idle_dual_pass_frames": idle_f,
                        "idle_dual_pass_rate": round(idle_f / float(tot_f), 4),
                        "async_worker": bool(from_worker),
                        "resolution": res_str,
                    },
                )
                self._vision_window_start_mono = now_mono
                self._vision_window_total_frames = 0
                self._vision_window_idle_dual_frames = 0

            idle_dual_emit = (
                is_idle_dual
                and (now_mono - float(getattr(self, "_last_idle_dual_pass_emit_mono", 0.0) or 0.0)) >= 5.0
            )
            spike_ms = worker_total_ms if from_worker else callback_ms
            if spike_ms >= 120.0 or callback_ms >= 50.0 or idle_dual_emit:
                if idle_dual_emit:
                    self._last_idle_dual_pass_emit_mono = now_mono
                self._emit_devlab(
                    "vision",
                    "vision.pipeline.latency_spike",
                    {
                        "total_ms": round(spike_ms, 2),
                        "callback_ms": round(callback_ms, 2),
                        "worker_total_ms": round(worker_total_ms, 2),
                        "capture_ms": round(capture_ms, 2),
                        "detect_ms": round(float(timings.get("detect_ms", 0.0) or 0.0), 2),
                        "plan_b_ms": round(float(timings.get("plan_b_ms", 0.0) or 0.0), 2),
                        "passive_hover_ms": round(float(timings.get("passive_hover_ms", 0.0) or 0.0), 2),
                        "ocr_p1_ms": round(float(diag.get("ocr_p1_ms") or 0.0), 2),
                        "ocr_p2_ms": round(ocr_p2_ms, 2),
                        "recovery_ocr_ms": round(float(diag.get("recovery_ocr_ms") or 0.0), 2),
                        "match_ms": round(float(diag.get("match_ms") or 0.0), 2),
                        "picker_open": bool(res.picker_visible or self.sm.state == GameState.AUGMENT_PICKING),
                        "in_augment_picking": bool(self.sm.state == GameState.AUGMENT_PICKING),
                        "dual_pass_ocr_ran": bool(ocr_p2_ms > 0.0),
                        "async_worker": bool(from_worker),
                        "resolution": res_str,
                    },
                )

    def _finalize_picker_close_commit(
        self,
        *,
        eff: StateStepEffects,
        resolved_slot: int | None,
        pick_conf: float,
        pick_source: str,
        res_str: str,
        now_mono: float,
        back_to_back: bool = False,
    ) -> None:
        devlab_on = bool(getattr(self, "_devlab_active", False))
        self._cancel_inflight_picker_coach()
        click_gap_ms = float(getattr(self, "_click_max_gap_ms_in_picker", 0.0) or 0.0)
        picker_dur_ms = (
            round((now_mono - float(getattr(self, "_picker_opened_mono", 0.0) or 0.0)) * 1000.0, 1)
            if float(getattr(self, "_picker_opened_mono", 0.0) or 0.0) > 0.0
            else 0.0
        )
        self._last_combo_verdict = None
        self.combo_overlay.exit_picker_mode()
        self.edge_drawer.set_picker_active(False)
        if not back_to_back and self._pre_picker_drawer_expanded is not None:
            restore_dr = self._pre_picker_drawer_expanded
            self._pre_picker_drawer_expanded = None
            if restore_dr and not self._overlays_hidden_by_user:
                self._mark_overlay_open("edge_drawer", "auto_picker_restore")
                self.edge_drawer.set_expanded(True)

        if devlab_on:
            self._devlab.set_context(game_state=self.sm.state.value)
            close_payload: dict[str, Any] = {
                "transition": "close",
                "is_open": False,
                "duration_ms": picker_dur_ms,
                "committed_augment_id": str(eff.newly_selected_augment or ""),
                "resolved_slot": resolved_slot,
                "resolution": res_str,
            }
            if back_to_back:
                close_payload["back_to_back"] = True
            self._emit_devlab("vision", "vision.picker.transition", close_payload)
            diag_fn = getattr(self.click_tracker, "last_resolve_diagnostics", None)
            click_diag = dict(diag_fn()) if callable(diag_fn) else {}
            click_diag.setdefault("source", pick_source)
            click_diag.setdefault("confidence", round(float(pick_conf), 3))
            click_diag["committed_augment_id"] = str(eff.newly_selected_augment or "")
            click_diag["resolved_slot"] = resolved_slot
            click_diag["resolution"] = res_str
            click_diag["click_timer_max_gap_ms"] = round(click_gap_ms, 1)
            self._emit_devlab("vision", "vision.click.selection_resolved", click_diag)
            if eff.newly_selected_augment and isinstance(getattr(self, "_aug_source_by_id", None), dict):
                self._aug_source_by_id[str(eff.newly_selected_augment)] = {
                    "source": str(click_diag.get("source") or pick_source),
                    "confidence": round(float(pick_conf), 3),
                    "ms_since_last_reroll": click_diag.get("ms_since_last_reroll"),
                    "cursor_zone": "reroll" if click_diag.get("in_reroll_zone") else "picker_card",
                }
            snap_offer = getattr(self, "_last_offer_rec_snapshot", None)
            if isinstance(snap_offer, dict):
                cards_list = list(snap_offer.get("cards") or [])
                top1_id = str(snap_offer.get("top1_augment_id") or "")
                picked_id = str(eff.newly_selected_augment or "")
                top1_card = next((x for x in cards_list if str(x.get("augment_id")) == top1_id), None)
                picked_card = next((x for x in cards_list if str(x.get("augment_id")) == picked_id), None)
                top1_hex = float(top1_card.get("hex_score", 0.0)) if isinstance(top1_card, dict) else 0.0
                picked_hex = float(picked_card.get("hex_score", 0.0)) if isinstance(picked_card, dict) else 0.0
                top1_wrd = float(top1_card.get("wr_delta", 0.0)) if isinstance(top1_card, dict) else 0.0
                picked_wrd = float(picked_card.get("wr_delta", 0.0)) if isinstance(picked_card, dict) else 0.0
                coach_aug = str(getattr(self, "_active_picker_coach_aug_advice", "") or "")
                local_vs_coach_disagreed = bool(coach_aug and top1_id and coach_aug != top1_id)
                if not picked_id:
                    sided_with = "unresolved"
                elif local_vs_coach_disagreed:
                    if picked_id == coach_aug:
                        sided_with = "coach"
                    elif picked_id == top1_id:
                        sided_with = "local"
                    else:
                        sided_with = "other"
                else:
                    sided_with = "both" if picked_id == top1_id else "other"
                picker_req_id = str(getattr(self, "_active_picker_coach_req_id", "") or "")
                arrived_before_pick_val: bool | None = (
                    bool(getattr(self, "_active_picker_coach_arrived", False))
                    if picker_req_id
                    else None
                )
                self._emit_devlab(
                    "rec",
                    "augment_pick_decision_delta",
                    {
                        "picked_augment_id": picked_id,
                        "top1_augment_id": top1_id,
                        "matched_top1": bool(picked_id and picked_id == top1_id),
                        "hex_score_regret": round(top1_hex - picked_hex, 2) if (picked_id and top1_card and picked_card) else 0.0,
                        "wr_delta_regret": round(top1_wrd - picked_wrd, 4) if (picked_id and top1_card and picked_card) else 0.0,
                        "combo_flipped": bool(snap_offer.get("combo_flipped", False)),
                        "is_stat_anvil": bool(snap_offer.get("is_stat_anvil", False)),
                        "source": str(click_diag.get("source") or pick_source),
                        "confidence": round(float(pick_conf), 3),
                        "ai_advice_arrived_before_pick": arrived_before_pick_val,
                        "coach_request_id": picker_req_id,
                        "coach_recommended_augment_id": coach_aug,
                        "local_vs_coach_disagreed": local_vs_coach_disagreed,
                        "sided_with": sided_with,
                        "duration_ms": picker_dur_ms,
                    },
                )
            if not back_to_back and click_gap_ms >= 90.0:
                self._emit_devlab(
                    "vision",
                    "vision.pipeline.latency_spike",
                    {
                        "click_timer_max_gap_ms": round(click_gap_ms, 1),
                        "in_augment_picking": True,
                        "picker_open": True,
                        "resolution": res_str,
                    },
                )

        if eff.newly_selected_augment:
            self._flag_if_transformative_augment(eff.newly_selected_augment)
            self._save_match_state()
            self.edge_drawer.flash_new_augment(eff.newly_selected_augment)
            aug = self.store.get_augment(eff.newly_selected_augment)
            logger.info(
                "Augment auto-committed (%s, conf=%.2f): %s (%d/%d)",
                pick_source or "click",
                pick_conf,
                aug.name if aug else eff.newly_selected_augment,
                len(self.sm.selected_augments),
                MAX_AUGMENT_SLOTS,
            )
            if eff.transmute_started and self.sm.pending_transmute is not None:
                if self.transmute_watcher is not None:
                    self.transmute_watcher.reset()
                self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
                logger.info(
                    "Transmute secondary OCR armed: source=%s expected=%d rarity=%s",
                    self.sm.pending_transmute.source_name,
                    self.sm.pending_transmute.expected_count,
                    self.sm.pending_transmute.target_rarity or "any",
                )
            # WS-6: Pass click confidence so high-conf (>=0.90) swap bar auto-dismisses after 15s!
            self.edge_drawer.set_recent_offered(
                [c.augment for c in self.sm.last_offered_cards],
                auto_committed_id=eff.newly_selected_augment,
                confidence=float(pick_conf),
            )
        elif self.sm.last_offered_cards:
            logger.info(
                "Augment picker closed without cursor/click inside cards; surfacing 1-click chips"
            )
            self.edge_drawer.set_recent_offered([c.augment for c in self.sm.last_offered_cards])
        self._push_recent_unrecorded_augments()
        if (
            not back_to_back
            and (resolved_slot is None or pick_conf < self.ICON_VERIFY_CONF_THRESHOLD)
            and self.sm.last_offered_cards
            and not eff.transmute_started
        ):
            self._schedule_delayed_icon_verify(
                committed_id=eff.newly_selected_augment,
                pick_confidence=pick_conf,
            )
        self._frozen_pick_slot = None
        self._frozen_pick_source = ""
        self._frozen_pick_confidence = 0.0
        self._active_picker_coach_req_id = ""
        self._active_picker_coach_arrived = False
        self._active_picker_coach_aug_advice = ""
        self._last_picker_partial_open = False
        self._last_picker_expected_slots = 0
        self._push_vision_context()

    def _render_augment_badges(self, cards: tuple[DetectedCard, ...], game_rect=None) -> None:
        t0 = time.perf_counter()
        devlab_on = bool(getattr(self, "_devlab_active", False))
        if self._is_full_locked():
            if self.augment_overlay.isVisible():
                self._mark_overlay_close("augment_badges", "locked")
                self.augment_overlay.hide()
            if self.combo_overlay.isVisible():
                self._mark_overlay_close("combo_overlay", "locked")
                self.combo_overlay.hide()
            return
        hid = self._active_hero_id()
        hero = self.store.get_hero(hid) if hid else None
        hero_wr = hero.win_rate if hero else 0.50

        scored_cards: list[tuple[DetectedCard, AugmentScore]] = []
        for c in cards:
            st = self.store.get_hero_augment_stat(hid, c.augment.augment_id) if hid else None
            rdist = self.store.get_hero_rarity_distribution(hid, c.augment.rarity) if hid else None
            sc = score_hero_augment(
                augment_id=c.augment.augment_id,
                name=c.augment.name,
                rarity=c.augment.rarity,
                hero_win_rate=hero_wr,
                wins=st.wins if st else 0,
                games=st.games if st else 0,
                low_sample=st.low_sample if st else True,
                rarity_dist=rdist,
            )
            scored_cards.append((c, sc))

        # WS-2: Filter Stat Anvil popups (>=2 anvil cards OR any anvil card once 4 augments are already owned)
        anvil_cnt = sum(1 for c in cards if c.augment.augment_id in _STAT_ANVIL_IDS)
        is_stat_anvil_offer = bool(cards) and (
            anvil_cnt >= 2 or (len(self.sm.selected_augments) >= 4 and anvil_cnt >= 1)
        )
        if is_stat_anvil_offer:
            if self.augment_overlay.isVisible():
                self._mark_overlay_close("augment_badges", "stat_anvil_filtered")
                self.augment_overlay.hide()
            return

        # Evaluate independent combo verdict first so we know if any card has jackpot/gamble upside
        is_bonus_offer = self.sm._is_bonus_event_offer(cards)
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        my_stats = self._current_my_stats()
        my_lvl = (
            int(getattr(self._last_live.my_player, "level", 0) or 0)
            if (self._last_live and self._last_live.my_player)
            else 0
        )
        my_gold_val = float(getattr(self._last_live, "current_gold", 0.0) or 0.0) if self._last_live else 0.0
        gt_s = float(self._current_game_time_s() or 0.0)
        band_mode = self._current_data_band_mode()
        pref_prof = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
        round_idx = effective_augment_round(len(self.sm.selected_augments), my_lvl)
        if is_bonus_offer:
            verdict = None
            combo_by_slot: dict[int, Any] = {}
        else:
            try:
                verdict = evaluate_picker_combos(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=my_items,
                    offered=[(c.slot_index, c.augment) for c in cards],
                    my_stats=my_stats,
                    game_time_s=gt_s,
                    data_band_mode=band_mode,
                    preferred_profile=pref_prof,
                    my_level=my_lvl,
                    my_gold=my_gold_val,
                    round_index=round_idx,
                )
            except TypeError:
                try:
                    verdict = evaluate_picker_combos(
                        store=self.store,
                        hero_id=hid,
                        selected_augments=self.sm.selected_augments,
                        my_items=my_items,
                        offered=[(c.slot_index, c.augment) for c in cards],
                        my_stats=my_stats,
                        game_time_s=gt_s,
                        data_band_mode=band_mode,
                    )
                except TypeError:
                    verdict = evaluate_picker_combos(
                        store=self.store,
                        hero_id=hid,
                        selected_augments=self.sm.selected_augments,
                        my_items=my_items,
                        offered=[(c.slot_index, c.augment) for c in cards],
                        my_stats=my_stats,
                        game_time_s=gt_s,
                    )
            self._last_combo_verdict = verdict
            combo_by_slot = {vc.slot_index: vc for vc in verdict.cards}

        decisions, reroll_banner = rank_picker_cards(
            [(c.slot_index, sc) for c, sc in scored_cards],
            combo_by_slot=combo_by_slot,
            game_time_s=gt_s,
            round_index=round_idx,
        )
        any_valid_sample = any(sc.games >= 30 for _c, sc in scored_cards)
        if not reroll_banner and not any_valid_sample and verdict is not None and verdict.reroll_advice:
            reroll_banner = verdict.reroll_advice

        if devlab_on and not is_bonus_offer:
            offer_cards: list[dict[str, Any]] = []
            top1_aug_id = ""
            combo_flipped = False
            for (c, sc), dec in zip(scored_cards, decisions):
                if dec.is_best and not top1_aug_id:
                    top1_aug_id = c.augment.augment_id
                if dec.overridden_by_combo:
                    combo_flipped = True
                offer_cards.append(
                    {
                        "slot": int(c.slot_index),
                        "augment_id": str(c.augment.augment_id),
                        "name": str(c.augment.name),
                        "rarity": str(c.augment.rarity),
                        "tier": str(sc.tier),
                        "hex_score": round(float(dec.final_hex_score), 1),
                        "raw_win_rate": round(float(sc.raw_win_rate), 4),
                        "wr_delta": round(float(sc.delta), 4),
                        "effective_delta": round(float(dec.effective_delta), 4),
                        "combo_shift": round(float(dec.combo_shift), 2),
                        "shift_kind": str(dec.shift_kind or ""),
                        "games": int(sc.games),
                        "is_best": bool(dec.is_best),
                        "confidence": round(float(c.confidence), 3),
                    }
                )
            if not top1_aug_id and offer_cards:
                best_c = max(offer_cards, key=lambda x: (x["hex_score"], x["wr_delta"]))
                top1_aug_id = str(best_c["augment_id"])
            is_stat_anvil = bool(cards) and (
                sum(1 for c in cards if c.augment.augment_id in _STAT_ANVIL_IDS) >= 2
                or (len(self.sm.selected_augments) >= 4 and any(c.augment.augment_id in _STAT_ANVIL_IDS for c in cards))
            )
            offer_snap: dict[str, Any] = {
                "hero_id": str(hid or ""),
                "top1_augment_id": top1_aug_id,
                "cards": offer_cards,
                "combo_flipped": combo_flipped,
                "reroll_banner": str(reroll_banner or ""),
                "is_stat_anvil": is_stat_anvil,
                "selected_count": len(self.sm.selected_augments),
            }
            self._last_offer_rec_snapshot = offer_snap
            offer_sig = (
                top1_aug_id,
                tuple((x["slot"], x["augment_id"], x["hex_score"], x["is_best"]) for x in offer_cards),
            )
            if offer_sig != getattr(self, "_last_offer_emitted_sig", None):
                self._last_offer_emitted_sig = offer_sig
                self._emit_devlab("rec", "augment_offer_recommendation", offer_snap)

            prev_offer = getattr(self, "_pending_reroll_prev_offer", None)
            if isinstance(prev_offer, dict):
                prev_by_slot = {int(x["slot"]): x for x in (prev_offer.get("cards") or []) if isinstance(x, dict) and "slot" in x}
                cur_by_slot = {int(x["slot"]): x for x in offer_cards}
                prev_top1 = str(prev_offer.get("top1_augment_id") or "")
                cand_slots = list(getattr(self, "_pending_reroll_slots", None) or [])
                if not cand_slots:
                    cand_slots = [
                        sl for sl in cur_by_slot
                        if sl in prev_by_slot and prev_by_slot[sl].get("augment_id") != cur_by_slot[sl].get("augment_id")
                    ]
                for sl in cand_slots:
                    old_c = prev_by_slot.get(sl)
                    new_c = cur_by_slot.get(sl)
                    if old_c is None or new_c is None:
                        continue
                    old_hex = float(old_c.get("hex_score", 0.0))
                    new_hex = float(new_c.get("hex_score", 0.0))
                    self._emit_devlab(
                        "rec",
                        "augment_reroll_delta",
                        {
                            "slot": int(sl),
                            "old_augment_id": str(old_c.get("augment_id") or ""),
                            "new_augment_id": str(new_c.get("augment_id") or ""),
                            "old_tier": str(old_c.get("tier") or ""),
                            "new_tier": str(new_c.get("tier") or ""),
                            "was_top1": bool(prev_top1 and str(old_c.get("augment_id") or "") == prev_top1),
                            "old_hex_score": round(old_hex, 1),
                            "new_hex_score": round(new_hex, 1),
                            "reroll_hex_gain": round(new_hex - old_hex, 2),
                        },
                    )
                self._pending_reroll_prev_offer = None
                self._pending_reroll_slots = None

        sp_db = get_special_augment_db()
        band_mode = self._current_data_band_mode()
        pref_prof = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
        hero_prof = (
            infer_unified_hero_profile(
                hid,
                self.store,
                my_items=my_items,
                selected_augments=self.sm.selected_augments,
                my_stats=my_stats,
                data_band_mode=band_mode,
                preferred_profile=pref_prof,
            )
            if (hid and self.store.is_loaded)
            else ""
        )
        render_items: list[AugmentBadgeRenderItem] = []
        for (c, sc), dec in zip(scored_cards, decisions):
            aid = c.augment.augment_id
            raw_d_items = (
                list(rank_destiny_items(self.store, hid, limit=2, profile=hero_prof))
                if (hid and aid == DESTINY_AUGMENT_ID)
                else []
            )
            d_items = tuple(
                replace(d, name=self.store.display_item_name(d.item_id, d.name))
                for d in raw_d_items
            )
            d_card_hint = (
                summarize_destiny_card_hint(
                    self.store,
                    hid,
                    aug_delta=sc.delta,
                    profile=hero_prof,
                )
                if (hid and aid == DESTINY_AUGMENT_ID)
                else None
            )
            d_hint_txt = d_card_hint.hint_line if d_card_hint is not None else ""
            if d_hint_txt and raw_d_items and normalize_client_locale(self.cfg.client_locale) != "zh_cn":
                top_raw = raw_d_items[0]
                top_disp = self.store.display_item_name(top_raw.item_id, top_raw.name)
                if top_raw.name and top_disp and top_disp != top_raw.name:
                    d_hint_txt = d_hint_txt.replace(top_raw.name, top_disp, 1)
            d_warn_txt = d_card_hint.warning_line if d_card_hint is not None else ""
            sp = sp_db.resolve_for_hero(self.store, aid, hid, band_mode=band_mode)
            arch_hint = ""
            if sp is not None:
                key_items = [self.store.display_item_name(iid) for iid in (sp.force_items or sp.core_items)[:2]]
                key_items = [n for n in key_items if n and not n.startswith("装备#")]
                short_arch = sp.archetype[:10]
                arch_hint = f"{short_arch} · {'+'.join(n[:4] for n in key_items)}" if key_items else short_arch
            render_items.append(
                AugmentBadgeRenderItem(
                    slot_index=c.slot_index,
                    score=sc,
                    screen_box=c.screen_box,
                    is_best=dec.is_best,
                    destiny_items=d_items,
                    uncertain=(c.confidence < 0.72),
                    archetype_hint=arch_hint,
                    is_transmute=is_transmute_augment(aid),
                    unified=dec,
                    destiny_hint=d_hint_txt,
                    destiny_warning=d_warn_txt,
                )
            )

        rect = game_rect or find_game_window() or get_primary_screen_rect()
        tip = self._latest_advice.augment_pick_advice if self._latest_advice else ""
        # Show transparent "saved credits" notice ONLY for active paid/trial users (NEVER for free/0-credit users)
        raw_gate_hint = str(getattr(self, "_last_picker_gate_skipped_hint", "") or "")
        gate_hint = (
            raw_gate_hint
            if (raw_gate_hint and self._is_any_coach_online() and not self._cloud_ai_cutoff)
            else ""
        )
        if not self.augment_overlay.isVisible():
            self._mark_overlay_open("augment_badges", "auto_picker")
        self.augment_overlay.render_cards(
            render_items,
            screen_rect=(rect.left, rect.top, rect.width, rect.height),
            coach_augment_tip=tip,
            coach_pending=self.edge_drawer._coach_pending,
            reroll_banner=reroll_banner,
            gate_skipped_hint=gate_hint,
        )

        if (
            not is_bonus_offer
            and self.cfg.combo_overlay_enabled
            and not self._combo_hidden_by_user
            and not self._overlays_hidden_by_user
            and not self._is_full_locked()
            and (self.cfg.combo_auto_popup_on_picker or self.combo_overlay.is_expanded)
        ):
            self.combo_overlay.enter_picker_mode(verdict)
            if not self.combo_overlay.isVisible():
                self._mark_overlay_open("combo_overlay", "auto_picker")
                self.combo_overlay.show_overlay()

        if devlab_on:
            self._devlab.record_latency_sample("render_augment_badges", (time.perf_counter() - t0) * 1000.0)

    def _refresh_drawer_view(self) -> None:
        devlab_on = bool(getattr(self, "_devlab_active", False))
        hid = self._active_hero_id()
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        my_gold = self._last_live.current_gold if self._last_live else 0.0
        allies = self._last_live.allies if self._last_live else ()
        enemies = self._last_live.enemies if self._last_live else ()
        band_mode = self._current_data_band_mode()
        pref_prof = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
        game_time = float(self._current_game_time_s() or 0.0)

        threat: MatchupThreatProfile | None = None
        dyn_recs: list[DynamicItemRec] = []
        next_plan: NextBuyPlan | None = None
        active_prof = pref_prof
        if hid and self.store.is_loaded:
            try:
                threat, dyn_recs = optimize_next_items(
                    store=self.store,
                    hero_id=hid,
                    my_items=my_items,
                    my_gold=my_gold,
                    selected_augments=self.sm.selected_augments,
                    allies=allies,
                    enemies=enemies,
                    limit=4,
                    committed_plan=self.sm.committed_plan,
                    locked_next_item=self.sm.locked_next_item,
                    my_player=self._last_live.my_player if self._last_live else None,
                    collect_breakdown=devlab_on,
                    data_band_mode=band_mode,
                    preferred_profile=pref_prof,
                    game_time=game_time,
                )
            except TypeError:
                try:
                    threat, dyn_recs = optimize_next_items(
                        store=self.store,
                        hero_id=hid,
                        my_items=my_items,
                        my_gold=my_gold,
                        selected_augments=self.sm.selected_augments,
                        allies=allies,
                        enemies=enemies,
                        limit=4,
                        committed_plan=self.sm.committed_plan,
                        locked_next_item=self.sm.locked_next_item,
                        my_player=self._last_live.my_player if self._last_live else None,
                        collect_breakdown=devlab_on,
                        data_band_mode=band_mode,
                    )
                except TypeError:
                    try:
                        threat, dyn_recs = optimize_next_items(
                            store=self.store,
                            hero_id=hid,
                            my_items=my_items,
                            my_gold=my_gold,
                            selected_augments=self.sm.selected_augments,
                            allies=allies,
                            enemies=enemies,
                            limit=4,
                            committed_plan=self.sm.committed_plan,
                            locked_next_item=self.sm.locked_next_item,
                            my_player=self._last_live.my_player if self._last_live else None,
                            data_band_mode=band_mode,
                        )
                    except TypeError:
                        threat, dyn_recs = optimize_next_items(
                            store=self.store,
                            hero_id=hid,
                            my_items=my_items,
                            my_gold=my_gold,
                            selected_augments=self.sm.selected_augments,
                            allies=allies,
                            enemies=enemies,
                            limit=4,
                            committed_plan=self.sm.committed_plan,
                            locked_next_item=self.sm.locked_next_item,
                            my_player=self._last_live.my_player if self._last_live else None,
                        )
            try:
                next_plan = build_next_buy_plan(
                    store=self.store,
                    hero_id=hid,
                    my_items=my_items,
                    my_gold=my_gold,
                    dynamic_recs=dyn_recs,
                    threat=threat,
                    game_time=game_time,
                    selected_augments=self.sm.selected_augments,
                    data_band_mode=band_mode,
                    preferred_profile=pref_prof,
                )
            except TypeError:
                try:
                    next_plan = build_next_buy_plan(
                        store=self.store,
                        hero_id=hid,
                        my_items=my_items,
                        my_gold=my_gold,
                        dynamic_recs=dyn_recs,
                        threat=threat,
                        game_time=game_time,
                        selected_augments=self.sm.selected_augments,
                        data_band_mode=band_mode,
                    )
                except TypeError:
                    next_plan = build_next_buy_plan(
                        store=self.store,
                        hero_id=hid,
                        my_items=my_items,
                        my_gold=my_gold,
                        dynamic_recs=dyn_recs,
                        threat=threat,
                        game_time=game_time,
                        selected_augments=self.sm.selected_augments,
                    )
            active_prof = (
                pref_prof
                or (getattr(next_plan, "active_profile", "") if next_plan is not None else "")
                or infer_unified_hero_profile(
                    hid,
                    self.store,
                    my_items=my_items,
                    selected_augments=self.sm.selected_augments,
                    data_band_mode=band_mode,
                )
            )
            if next_plan is not None:
                prev_lock, prev_plan = self.sm.locked_next_item, list(self.sm.committed_plan)
                is_tx_overhaul = bool(self.sm.allow_plan_overhaul and self.sm.plan_overhaul_reason)
                if devlab_on:
                    new_lock, new_plan, trace = reconcile_match_build_plan_with_trace(
                        store=self.store,
                        my_items=my_items,
                        committed_plan=self.sm.committed_plan,
                        locked_next_item=self.sm.locked_next_item,
                        proposed_ids=list(next_plan.remaining_build_ids),
                        allow_overhaul=self.sm.allow_plan_overhaul and not self._is_any_coach_online(),
                        hero_id=hid or "",
                        selected_augments=self.sm.selected_augments,
                        transformative_overhaul=is_tx_overhaul,
                        active_profile=active_prof,
                        game_time=game_time,
                    )
                    if trace.get("overridden") and self._action_tracker is not None:
                        tr_copy = dict(trace)
                        tr_copy["old_locked_item"] = prev_lock
                        tr_copy["new_locked_item"] = new_lock
                        tr_copy["source"] = "local"
                        self._action_tracker.record_plan_override(float(self._current_game_time_s() or 0.0), tr_copy)
                else:
                    new_lock, new_plan = reconcile_match_build_plan(
                        store=self.store,
                        my_items=my_items,
                        committed_plan=self.sm.committed_plan,
                        locked_next_item=self.sm.locked_next_item,
                        proposed_ids=list(next_plan.remaining_build_ids),
                        allow_overhaul=self.sm.allow_plan_overhaul and not self._is_any_coach_online(),
                        hero_id=hid or "",
                        selected_augments=self.sm.selected_augments,
                        transformative_overhaul=is_tx_overhaul,
                        active_profile=active_prof,
                        game_time=game_time,
                    )
                self.sm.locked_next_item = new_lock
                self.sm.committed_plan = new_plan
                if self.sm.allow_plan_overhaul and not self._is_any_coach_online():
                    self.sm.allow_plan_overhaul = False
                if new_lock != prev_lock or new_plan != prev_plan:
                    self._save_match_state()
        self._last_threat = threat
        self._last_dyn_recs = dyn_recs
        self._last_next_plan = next_plan
        if (
            len(self.sm.selected_augments) >= 4
            and self._latest_advice is not None
            and getattr(self._latest_advice, "next_augment_goal", "")
        ):
            self._latest_advice = replace(self._latest_advice, next_augment_goal="")
            self.edge_drawer.set_coach_advice(self._latest_advice, allow_auto_expand=False)
        self.edge_drawer.set_committed_plan(
            self.sm.committed_plan,
            self.sm.locked_next_item,
            self.sm.plan_overhaul_reason,
        )
        arch_templates = (
            get_hero_build_templates(self.store, hid, data_band_mode=band_mode)
            if (hid and self.store.is_loaded)
            else []
        )
        self.edge_drawer.set_archetype_options(
            arch_templates,
            active_prof,
            is_manual=bool(pref_prof and any(t.profile == pref_prof for t in arch_templates)),
        )
        self.edge_drawer.refresh_match_view(
            store=self.store,
            hero_id=hid,
            selected_augments=self.sm.selected_augments,
            my_items=my_items,
            my_gold=my_gold,
            threat=threat,
            dynamic_recs=dyn_recs,
            next_plan=next_plan,
        )
        combo_plan = self._refresh_combo_plan_view()
        if devlab_on and self._action_tracker is not None and hid and dyn_recs:
            try:
                recs_summary = [
                    {
                        "item_id": r.item_id,
                        "name": r.name,
                        "price": int(r.price),
                        "score": round(float(r.score), 3),
                        "affordable": bool(r.affordable),
                        "reasons": list(r.reasons[:4]),
                        "score_breakdown": dict(getattr(r, "score_breakdown", None) or {}),
                    }
                    for r in dyn_recs[:4]
                ]
                hero_prof = infer_unified_hero_profile(
                    hid,
                    self.store,
                    my_items=my_items,
                    selected_augments=self.sm.selected_augments,
                    my_stats=self._current_my_stats(),
                    data_band_mode=band_mode,
                )
                combo_prof = (
                    str(combo_plan.profile.primary_profile or "")
                    if (combo_plan is not None and getattr(combo_plan, "profile", None) is not None)
                    else ""
                )
                boots_raw = str(next_plan.recommended_boots_id or "") if next_plan is not None else ""
                boots_rec = int(boots_raw) if boots_raw.isdigit() else 0
                pivot_banner = str(next_plan.pivot_banner_text or "") if next_plan is not None else ""
                self._action_tracker.record_local_build_rec(
                    float(self._current_game_time_s() or 0.0),
                    recs_summary,
                    hero_profile=hero_prof,
                    combo_hero_profile=combo_prof,
                    boots_rec=boots_rec,
                    pivot_banner=pivot_banner,
                    source="local",
                )
            except Exception as exc:
                logger.debug("DevLab local build rec snapshot skipped: %s", exc)

    def _set_combo_overlay_plan(self, plan: ComboPlan, selected_count: int) -> None:
        overlay = getattr(self, "combo_overlay", None)
        if overlay is None:
            return
        try:
            overlay.set_plan(plan, selected_count=selected_count)
        except TypeError:
            overlay.set_plan(plan)

    def _refresh_combo_plan_view(self) -> ComboPlan:
        hid = self._active_hero_id()
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        my_stats = self._current_my_stats()
        gt_s = float(self._current_game_time_s() or 0.0)
        band_mode = self._current_data_band_mode()
        pref_prof = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
        ally_profiles, enemy_profiles = self._current_team_profiles(band_mode=band_mode)
        try:
            plan = build_combo_plan(
                store=self.store,
                hero_id=hid,
                selected_augments=self.sm.selected_augments,
                my_items=my_items,
                limit=4,
                threat=getattr(self, "_last_threat", None),
                ally_profiles=ally_profiles,
                enemy_profiles=enemy_profiles,
                my_stats=my_stats,
                game_time_s=gt_s,
                data_band_mode=band_mode,
                preferred_profile=pref_prof,
            )
        except TypeError:
            try:
                plan = build_combo_plan(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=my_items,
                    limit=4,
                    threat=getattr(self, "_last_threat", None),
                    ally_profiles=ally_profiles,
                    enemy_profiles=enemy_profiles,
                    my_stats=my_stats,
                    game_time_s=gt_s,
                    data_band_mode=band_mode,
                )
            except TypeError:
                plan = build_combo_plan(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=my_items,
                    limit=4,
                    threat=getattr(self, "_last_threat", None),
                    ally_profiles=ally_profiles,
                    enemy_profiles=enemy_profiles,
                    my_stats=my_stats,
                    game_time_s=gt_s,
                )
        self._last_combo_plan = plan
        self._set_combo_overlay_plan(plan, len(self.sm.selected_augments))
        return plan

    def trigger_coach_request(
        self,
        reason: str = "manual_f6",
        *,
        _skip_plan_b_prescan: bool = False,
    ) -> CoachRequestPayload | None:
        """Build full 10-player + augment match snapshot and publish to cloud/Gemini/bridge coach."""
        devlab_on = bool(getattr(self, "_devlab_active", False))
        if devlab_on and reason == "manual_f6" and not _skip_plan_b_prescan:
            self._record_manual_trigger("manual_f6")

        hid = self._active_hero_id()
        # Hard guard: never burn AI tokens/credits when hero is not locked (except explicit connection_test)
        if reason != "connection_test" and not hid:
            hint = "请进入对局锁定英雄后再呼叫参谋"
            logger.info("Unlocked hero guard: skipping coach request (%s)", reason)
            if not self._cloud_ai_cutoff:
                self.edge_drawer.set_coach_pending(False)
                self.edge_drawer.coach_content_lbl.setText(hint)
                self.edge_drawer.coach_content_lbl.setToolTip(hint)
            if hasattr(self.lookup_window, "coach_page_body") and (
                "暂未收到参谋回写" in self.lookup_window.coach_page_body.text() or not self._latest_advice
            ):
                self.lookup_window.coach_page_body.setText(hint)
            return None

        # WS-3: Block blind manual_f6 during CHAMP_SELECT when enemy roster is unknown
        if (
            reason == "manual_f6"
            and self.sm.state == GameState.CHAMP_SELECT
            and not (self._last_live and self._last_live.enemies)
        ):
            hint = "选人阶段暂未知敌方阵容，进入对局后将自动开局定盘"
            logger.info("ChampSelect empty-enemy guard: skipping blind manual_f6 coach request")
            if not self._cloud_ai_cutoff:
                self.edge_drawer.set_coach_pending(False)
                self.edge_drawer.coach_content_lbl.setText(hint)
                self.edge_drawer.coach_content_lbl.setToolTip(hint)
            return None

        # WS-3: In 2999-degraded mode, trigger a fresh Plan B pre-scan BEFORE sending manual_f6
        if (
            reason == "manual_f6"
            and not _skip_plan_b_prescan
            and self.sm.is_degraded_2999
            and self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING)
        ):
            worker = getattr(self, "_vision_worker", None)
            if worker is not None and worker.is_running():
                self._pending_manual_f6_after_plan_b = True
                self._push_vision_context()
                worker.enqueue_command("plan_b_rescan")
                if not self._cloud_ai_cutoff:
                    self.edge_drawer.set_coach_pending(True, pending_label="⏳ 扫画面同步后推演…")
                return None
            game_rect = find_game_window()
            if game_rect is not None and getattr(self, "capturer", None) is not None:
                try:
                    cap = self.capturer.grab((game_rect.left, game_rect.top, game_rect.right, game_rect.bottom))
                    if cap is not None and not cap.is_black and cap.frame_bgr is not None:
                        self._run_plan_b_hud_visual_sync(cap.frame_bgr, game_rect, force_confirm=True)
                except Exception:
                    pass

        # WS-3: 10s same-state signature dedup on repeated manual_f6 clicks during live match to avoid burning duplicate tokens
        if (
            reason == "manual_f6"
            and self._last_live is not None
            and self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING)
        ):
            my_items_now = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
            my_gold_now = float(self._last_live.current_gold or 0.0) if self._last_live else 0.0
            enemy_sig_now = self.sm._enemy_build_signature(self._last_live, store=self.store) if self._last_live else ""
            f6_sig = (
                hid or "",
                self.sm.state.value,
                tuple(my_items_now),
                tuple(self.sm.selected_augments),
                tuple(c.augment.augment_id for c in self.sm.active_cards),
                enemy_sig_now,
                int(my_gold_now // 300.0),
                getattr(self.sm, "manual_profile_override", ""),
            )
            now_m = time.monotonic()
            if (
                self._latest_advice is not None
                and f6_sig == getattr(self, "_last_manual_f6_sig", None)
                and (now_m - float(getattr(self, "_last_manual_f6_mono", 0.0) or 0.0)) < self.MANUAL_F6_DEDUP_WINDOW_S
            ):
                logger.info("Skipping duplicate manual_f6 within %.0fs (unchanged match state)", self.MANUAL_F6_DEDUP_WINDOW_S)
                if not self._cloud_ai_cutoff:
                    self.edge_drawer.set_coach_pending(False)
                    self.edge_drawer.coach_content_lbl.setToolTip("局势与 10 秒前一致，当前建议已是最新")
                return None
            self._last_manual_f6_sig = f6_sig
            self._last_manual_f6_mono = now_m

        # WS-3 / v1.5.2: 15s same-state dedup on back-to-back augment_selected triggers
        if (
            reason == "augment_selected"
            and self._last_live is not None
            and self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING)
        ):
            my_items_sel = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
            aug_sel_sig = (
                hid or "",
                tuple(self.sm.selected_augments),
                tuple(my_items_sel),
                bool(self.sm.allow_plan_overhaul),
                str(self.sm.plan_overhaul_reason or ""),
                getattr(self.sm, "manual_profile_override", ""),
            )
            now_m_sel = time.monotonic()
            if (
                aug_sel_sig == getattr(self, "_last_augment_selected_sig", None)
                and (now_m_sel - float(getattr(self, "_last_augment_selected_mono", 0.0) or 0.0))
                < self.AUGMENT_SELECTED_DEDUP_WINDOW_S
            ):
                logger.info(
                    "Skipping duplicate augment_selected within %.0fs (unchanged match state)",
                    self.AUGMENT_SELECTED_DEDUP_WINDOW_S,
                )
                return None
            self._last_augment_selected_sig = aug_sel_sig
            self._last_augment_selected_mono = now_m_sel

        # Cancel superseded pending debounce/deferral timers on explicit/newer triggers
        if reason in ("manual_f6", "augment_selected", "augment_picking"):
            self._cancel_death_coach_defer(f"superseded by {reason}")
            self._cancel_augment_remove_coach_defer(f"superseded by {reason}")
        if reason in ("manual_f6", "augment_selected"):
            self._cancel_inflight_picker_coach()
        if reason in ("manual_f6", "game_start"):
            self._cancel_game_start_coach_defer(f"superseded by {reason}")

        # Smart local picker gate: skip automatic augment_picking cloud call when local 0ms winner is decisive
        if reason == "augment_picking" and self.cfg.smart_picker_ai_gate and self.sm.active_cards:
            should_skip, skip_detail = self._should_skip_picker_ai_call(self.sm.active_cards)
            if devlab_on:
                gate_eval = dict(getattr(self, "_last_smart_gate_eval", None) or {})
                gate_eval["skipped"] = bool(should_skip)
                gate_eval["detail"] = str(skip_detail or "")
                gate_eval.setdefault("threshold", round(float(self.SMART_PICKER_GATE_DELTA_GAP), 4))
                self._emit_devlab("coach", "smart_picker_gate_decision", gate_eval)
            if should_skip:
                if devlab_on:
                    self._last_smart_gate_skip_mono = time.monotonic()
                    self._last_smart_gate_skip_detail = str(skip_detail or "")
                logger.info("Smart picker gate: clear local winner (%s); skipping redundant cloud AI", skip_detail)
                self.edge_drawer.set_coach_pending(False)
                self._last_picker_gate_skipped_hint = "◈ 胜率断层领先 · AI 自动跳过（已省积分）"
                if (
                    self.sm.state == GameState.AUGMENT_PICKING
                    and self.sm.active_cards
                    and not self._overlays_hidden_by_user
                    and not self._is_full_locked()
                ):
                    self._render_augment_badges(self.sm.active_cards)
                return None
        self._last_picker_gate_skipped_hint = ""

        hero = self.store.get_hero(hid) if hid else None
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        my_gold = self._last_live.current_gold if self._last_live else 0.0

        selected_augs = []
        for aid in self.sm.selected_augments:
            aug = self.store.get_augment(aid)
            selected_augs.append({"id": aid, "name": aug.name if aug else aid, "rarity": aug.rarity if aug else ""})

        offered_augs = []
        for c in self.sm.active_cards:
            st = self.store.get_hero_augment_stat(hid, c.augment.augment_id) if hid else None
            rdist = self.store.get_hero_rarity_distribution(hid, c.augment.rarity) if hid else None
            sc = score_hero_augment(
                c.augment.augment_id,
                c.augment.name,
                c.augment.rarity,
                hero.win_rate if hero else 0.5,
                st.wins if st else 0,
                st.games if st else 0,
                st.low_sample if st else True,
                rarity_dist=rdist,
            )
            offered_augs.append(
                {
                    "id": c.augment.augment_id,
                    "name": c.augment.name,
                    "rarity": c.augment.rarity,
                    "tier": sc.tier,
                    "win_rate": round(sc.raw_win_rate, 4),
                    "delta": round(sc.delta, 4),
                    "games": sc.games,
                }
            )

        allies = []
        enemies = []
        if self._last_live:
            for p in self._last_live.allies:
                a_entry: dict[str, Any] = {
                    "champion": p.champion_name,
                    "kda": f"{p.kills}/{p.deaths}/{p.assists}",
                    "items": [self.store.get_item_info(i).name for i in p.item_ids],
                }
                if p.summoner_spells:
                    a_entry["spells"] = list(p.summoner_spells)
                if p.is_dead and p.respawn_timer > 0:
                    a_entry["respawn_s"] = int(round(p.respawn_timer))
                allies.append(a_entry)
            for p in self._last_live.enemies:
                e_entry: dict[str, Any] = {
                    "champion": p.champion_name,
                    "kda": f"{p.kills}/{p.deaths}/{p.assists}",
                    "items": [self.store.get_item_info(i).name for i in p.item_ids],
                }
                if p.summoner_spells:
                    e_entry["spells"] = list(p.summoner_spells)
                if p.is_dead and p.respawn_timer > 0:
                    e_entry["respawn_s"] = int(round(p.respawn_timer))
                enemies.append(e_entry)

        owned_complete_names: list[str] = []
        for iid in my_items:
            info = self.store.get_item_info(iid)
            if (info.category == "complete_equipment" or info.gold >= 2200) and info.name not in owned_complete_names:
                owned_complete_names.append(info.name)

        locked_item_payload: dict = {}
        if self.sm.locked_next_item and not item_is_owned(self.sm.locked_next_item, my_items):
            linfo = self.store.get_item_info(self.sm.locked_next_item)
            locked_item_payload = {"item_id": self.sm.locked_next_item, "name": linfo.name}

        my_stats = self._current_my_stats()
        gt_s = float(self._current_game_time_s() or 0.0)
        band_mode = self._current_data_band_mode()
        pref_prof = str(getattr(self.sm, "manual_profile_override", "") or "").strip().upper()
        ally_profiles, enemy_profiles = self._current_team_profiles(band_mode=band_mode)
        threat_ctx = getattr(self, "_last_threat", None)
        if (
            threat_ctx is None
            and self._last_live is not None
            and self.store.is_loaded
            and (self._last_live.allies or self._last_live.enemies)
        ):
            threat_ctx = analyze_matchup_threats(self.store, self._last_live.allies, self._last_live.enemies)
        try:
            combo_plan = build_combo_plan(
                store=self.store,
                hero_id=hid,
                selected_augments=self.sm.selected_augments,
                my_items=my_items,
                limit=4,
                threat=threat_ctx,
                ally_profiles=ally_profiles,
                enemy_profiles=enemy_profiles,
                my_stats=my_stats,
                game_time_s=gt_s,
                data_band_mode=band_mode,
                preferred_profile=pref_prof,
            )
        except TypeError:
            try:
                combo_plan = build_combo_plan(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=my_items,
                    limit=4,
                    threat=threat_ctx,
                    ally_profiles=ally_profiles,
                    enemy_profiles=enemy_profiles,
                    my_stats=my_stats,
                    game_time_s=gt_s,
                    data_band_mode=band_mode,
                )
            except TypeError:
                combo_plan = build_combo_plan(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=my_items,
                    limit=4,
                    threat=threat_ctx,
                    ally_profiles=ally_profiles,
                    enemy_profiles=enemy_profiles,
                    my_stats=my_stats,
                    game_time_s=gt_s,
                )
        self._last_combo_plan = combo_plan
        if reason != "augment_picking" and self.sm.state != GameState.AUGMENT_PICKING:
            self._set_combo_overlay_plan(combo_plan, len(self.sm.selected_augments))

        active_card_ids = tuple(c.augment.augment_id for c in self.sm.active_cards)
        cached_picker_verdict = self._last_combo_verdict
        if (
            cached_picker_verdict is not None
            and tuple(vc.augment_id for vc in cached_picker_verdict.cards) != active_card_ids
        ):
            cached_picker_verdict = None
        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards:
            if cached_picker_verdict is not None:
                picker_verdict = cached_picker_verdict
            else:
                my_lvl_tc = (
                    int(getattr(self._last_live.my_player, "level", 0) or 0)
                    if (self._last_live and self._last_live.my_player)
                    else 0
                )
                try:
                    picker_verdict = evaluate_picker_combos(
                        store=self.store,
                        hero_id=hid,
                        selected_augments=self.sm.selected_augments,
                        my_items=my_items,
                        offered=[(c.slot_index, c.augment) for c in self.sm.active_cards],
                        my_stats=my_stats,
                        game_time_s=gt_s,
                        data_band_mode=band_mode,
                        preferred_profile=pref_prof,
                        my_level=my_lvl_tc,
                        my_gold=my_gold,
                    )
                except TypeError:
                    try:
                        picker_verdict = evaluate_picker_combos(
                            store=self.store,
                            hero_id=hid,
                            selected_augments=self.sm.selected_augments,
                            my_items=my_items,
                            offered=[(c.slot_index, c.augment) for c in self.sm.active_cards],
                            my_stats=my_stats,
                            game_time_s=gt_s,
                            data_band_mode=band_mode,
                        )
                    except TypeError:
                        picker_verdict = evaluate_picker_combos(
                            store=self.store,
                            hero_id=hid,
                            selected_augments=self.sm.selected_augments,
                            my_items=my_items,
                            offered=[(c.slot_index, c.augment) for c in self.sm.active_cards],
                            my_stats=my_stats,
                            game_time_s=gt_s,
                        )
        else:
            picker_verdict = None

        my_hero_dict: dict[str, Any] = {
            "hero_id": hid or "",
            "name": hero.name if hero else "未锁定",
            "win_rate": round(hero.win_rate, 4) if hero else 0.5,
            "gold": int(my_gold),
            "is_dead": bool(self._last_live and self._last_live.my_player and self._last_live.my_player.is_dead),
            "items": [self.store.get_item_info(i).name for i in my_items],
        }
        eff_crit = estimate_effective_crit_chance(
            self.store,
            hid,
            my_items=my_items,
            selected_augments=self.sm.selected_augments,
            my_stats=my_stats,
            game_time_s=gt_s,
        )
        crit_pct = int(round(eff_crit * 100.0))
        if crit_pct > 0:
            my_hero_dict["crit_chance_pct"] = crit_pct
        if self._last_live:
            if self._last_live.game_time > 0:
                my_hero_dict["game_time_s"] = int(self._last_live.game_time)
            if self._last_live.my_maxed_ability:
                my_hero_dict["max_skill"] = self._last_live.my_maxed_ability
            if self._last_live.my_keystone:
                my_hero_dict["keystone"] = self._last_live.my_keystone
            if self._last_live.my_player and self._last_live.my_player.summoner_spells:
                my_hero_dict["spells"] = list(self._last_live.my_player.summoner_spells)
            if self._last_live.my_player and self._last_live.my_player.is_dead and self._last_live.my_player.respawn_timer > 0:
                my_hero_dict["respawn_s"] = int(round(self._last_live.my_player.respawn_timer))
            if self._last_live.my_team_kills or self._last_live.enemy_team_kills:
                my_hero_dict["team_kills"] = f"{self._last_live.my_team_kills}:{self._last_live.enemy_team_kills}"
            if self._last_live.ally_turrets_lost or self._last_live.enemy_turrets_lost:
                my_hero_dict["turrets_lost"] = f"{self._last_live.ally_turrets_lost}:{self._last_live.enemy_turrets_lost}"

        committed_plan_payload: dict[str, Any] = {
            "owned_complete": owned_complete_names,
            "locked_next_item": locked_item_payload,
            "remaining_plan": [
                self.store.get_item_info(i).name
                for i in self.sm.committed_plan
                if not item_is_owned(i, my_items)
            ],
            "allow_overhaul": bool(self.sm.allow_plan_overhaul),
            "transformative_overhaul": bool(self.sm.allow_plan_overhaul and self.sm.plan_overhaul_reason),
            "overhaul_reason": self.sm.plan_overhaul_reason,
        }
        if self._last_next_plan and self._last_next_plan.recommended_boots_name:
            committed_plan_payload["recommended_boots"] = (
                f"{self._last_next_plan.recommended_boots_name}({self._last_next_plan.recommended_boots_reason})"
            )
        active_prof_payload = (
            pref_prof
            or (self._last_next_plan.active_profile if getattr(self, "_last_next_plan", None) is not None else "")
            or (
                infer_unified_hero_profile(
                    hid,
                    self.store,
                    my_items=my_items,
                    selected_augments=self.sm.selected_augments,
                    my_stats=my_stats,
                    data_band_mode=band_mode,
                )
                if (hid and self.store.is_loaded)
                else ""
            )
        )
        if active_prof_payload:
            committed_plan_payload["selected_archetype"] = active_prof_payload
        if hid and self.store.is_loaded:
            arch_templates = get_hero_build_templates(self.store, hid, data_band_mode=band_mode)
            if len(arch_templates) >= 2:
                committed_plan_payload["available_archetypes"] = [t.label for t in arch_templates]

        patch_disp = format_display_patch(self.store.descriptor.patch) if self.store.descriptor else "未知"
        req = CoachRequestPayload(
            request_id=f"hex-{uuid.uuid4().hex[:10]}",
            trigger_reason=reason,
            timestamp=utc_now_iso(),
            dataset_patch=patch_disp,
            my_hero=my_hero_dict,
            selected_augments=selected_augs,
            offered_augments=offered_augs,
            allies=allies,
            enemies=enemies,
            threat_summary=list(threat_ctx.summary_tags) if threat_ctx else [],
            local_engine_top_items=[
                {
                    "item_id": r.item_id,
                    "name": r.name,
                    "price": r.price,
                    "effective_cost": (
                        self._last_next_plan.effective_cost
                        if (idx == 0 and self._last_next_plan and self._last_next_plan.target.item_id == r.item_id)
                        else r.price
                    ),
                    "affordable": r.affordable,
                    "buy_now_hint": (
                        self._last_next_plan.buy_now_hint
                        if (idx == 0 and self._last_next_plan and self._last_next_plan.target.item_id == r.item_id)
                        else ""
                    ),
                    "reasons": list(r.reasons),
                }
                for idx, r in enumerate(self._last_dyn_recs)
            ],
            committed_build_plan=committed_plan_payload,
            special_augment_builds=get_special_augment_db().export_coach_context(
                self.store,
                [*self.sm.selected_augments, *(c.augment.augment_id for c in self.sm.active_cards)],
                hero_id=hid,
                band_mode=band_mode,
            ),
            hero_signature_hint=(
                sig.to_coach_dict(self.store)
                if (hid and (sig := get_hero_signature_db().get(hid, hero.name if hero else "")) is not None)
                else {}
            ),
            augment_combo_context=export_combo_coach_context(combo_plan, picker_verdict),
            lobby_scout=self._lobby_scout.to_coach_context() if self._lobby_scout is not None else {},
        )
        self._latest_request_id = req.request_id
        self._latest_request_trigger = reason
        if devlab_on:
            meta_map = getattr(self, "_last_coach_req_meta", None)
            if isinstance(meta_map, dict):
                local_top_id = str(self._last_dyn_recs[0].item_id) if self._last_dyn_recs else ""
                meta_map[req.request_id] = {
                    "trigger_reason": reason,
                    "local_top_item_id": local_top_id,
                    "dispatched_mono": time.monotonic(),
                }
                while len(meta_map) > 16:
                    oldest_k = next(iter(meta_map))
                    meta_map.pop(oldest_k, None)
        dispatched = False
        if self._commercial:
            if self.cfg.coach_mode == "off":
                return req
            if self.cloud_client is None or not self.cloud_client.is_logged_in:
                self._on_cloud_cutoff("NOT_LOGGED_IN", "请先登录账号启用云端 AI 参谋", {})
                return req
            if self._cloud_ai_cutoff:
                self._refresh_coach_online_ui()
                return req
            if self.cloud_coach is not None:
                tier = _normalize_coach_tier(self.cfg.coach_tier)
                tier_lbl = {"auto": "智能混合", "b": "B档旗舰", "a": "A档极速"}[tier]
                if reason != "pre_death_warmup" or self._latest_advice is None:
                    self.edge_drawer.set_coach_pending(True, pending_label=f"⏳ 云端 {tier_lbl} 推演中…")
                self.cloud_coach.submit(req, self.store, tier=tier)
                dispatched = True
        else:
            if self.cfg.bridge_enabled and self.bridge is not None:
                self.bridge.publish_request(req)
                if self.bridge.is_coach_online():
                    dispatched = True
            if self._is_direct_gemini_active() and self.gemini_coach is not None:
                fast_picker_model = str(getattr(self.cfg, "gemini_picker_fast_model", "") or "gemini-3.8-flash")
                is_picker_fast = req.trigger_reason == "augment_picking"
                eff_model = fast_picker_model if (is_picker_fast and fast_picker_model) else self.cfg.gemini_model
                is_tx_overhaul = bool(self.sm.allow_plan_overhaul and self.sm.plan_overhaul_reason)
                eff_lvl = resolve_thinking_level(
                    self.cfg.gemini_thinking_level,
                    req.trigger_reason,
                    adaptive=self.cfg.gemini_adaptive_thinking,
                    model=eff_model,
                    is_transformative=(is_tx_overhaul if req.trigger_reason == "augment_selected" else None),
                    fast_death=True,
                )
                short_m = eff_model.replace("gemini-", "G")
                if reason != "pre_death_warmup" or self._latest_advice is None:
                    self.edge_drawer.set_coach_pending(True, pending_label=f"⏳ {short_m} ({eff_lvl}) 推演中…")
                self.gemini_coach.submit(
                    req,
                    self.store,
                    api_key=self.cfg.resolved_gemini_api_key(),
                    model=self.cfg.gemini_model,
                    thinking_level=self.cfg.gemini_thinking_level,
                    adaptive_thinking=self.cfg.gemini_adaptive_thinking,
                    proxy_url=self.cfg.proxy_url,
                    fast_picker_model=fast_picker_model,
                    use_fast_picker_payload=(True if is_picker_fast else None),
                    fast_death=True,
                )
                dispatched = True
            elif self.cfg.coach_mode == "gemini":
                self.edge_drawer.set_coach_error("请先在「AI 参谋」页填入 Gemini API Key")
            elif self._is_any_coach_online():
                if reason != "pre_death_warmup" or self._latest_advice is None:
                    self.edge_drawer.set_coach_pending(True)
        if dispatched:
            self.ai_calls_this_match += 1
            if reason == "pre_death_warmup":
                self._last_warmup_dispatched_mono = time.monotonic()
                self._last_warmup_dispatched_enemy_sig = (
                    self.sm._enemy_build_signature(self._last_live, store=self.store)
                    if self._last_live is not None
                    else ""
                )
            if devlab_on and reason == "augment_picking":
                self._active_picker_coach_req_id = req.request_id
                self._active_picker_coach_arrived = False
                self._active_picker_coach_aug_advice = ""
            if reason in ("augment_selected", "manual_f6") and getattr(self, "_death_defer_superseded_by_picker", False):
                is_dead_now = bool(self._last_live and self._last_live.my_player and self._last_live.my_player.is_dead)
                dw_open = bool(
                    self._action_tracker is not None
                    and getattr(self._action_tracker, "_death_window", None) is not None
                )
                if is_dead_now or dw_open:
                    self._death_superseded_req_id = str(req.request_id or "")
                    if self._action_tracker is not None:
                        self._action_tracker.note_death_coach_decision(True, "")
        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards and not self._overlays_hidden_by_user:
            self._render_augment_badges(self.sm.active_cards)
        return req

    def _format_ocr_diag_line(self) -> str:
        loc = normalize_client_locale(getattr(self.cfg, "client_locale", "zh_cn"))
        worker_eng = getattr(getattr(self, "_vision_worker", None), "_ocr_engine", None)
        eng = self.ocr_engine or worker_eng
        if eng is not None:
            mode = str(getattr(eng, "effective_ocr_mode", "") or "")
            actual = str(getattr(eng, "actual_language_tag", "") or getattr(eng, "language_tag", "") or "")
            if mode == "native_tw":
                return f"WinRT OCR：就绪 ({actual or 'zh-Hant-TW'} 原生繁中)"
            if mode == "sc_t2s_fallback":
                return f"WinRT OCR：就绪 ({actual or 'zh-Hans-CN'} · 简转繁免装兼容)"
            if mode == "profile_fallback":
                return f"WinRT OCR：兼容回退 ({actual or 'profile'})"
            return f"WinRT OCR：就绪 ({actual or 'zh-Hans-CN'})"
        if getattr(self, "_vision_ocr_ready", False):
            return f"WinRT OCR：就绪 ({desired_ocr_language_tag(loc)})"
        readiness = check_locale_ocr_readiness(loc)
        if readiness.get("ok"):
            return f"WinRT OCR：{readiness.get('summary', '待机')} ({readiness.get('mode', '')})"
        return f"WinRT OCR：不可用 ({readiness.get('summary', '')})"

    def _refresh_diagnostics(self) -> None:
        loc_code = normalize_client_locale(getattr(self.cfg, "client_locale", "zh_cn"))
        loc_label = CLIENT_LOCALE_LABELS.get(loc_code, loc_code)
        ocr_diag_line = self._format_ocr_diag_line()
        if self._commercial:
            if self.cloud_client is not None and self.cloud_client.is_logged_in:
                tier_code = _normalize_coach_tier(self.cfg.coach_tier)
                tier = {
                    "auto": "智能混合 · 日常A/关键B",
                    "b": "B档 · 高智旗舰",
                    "a": "A档 · 极速经济",
                }[tier_code]
                wallet = self.cloud_client.last_wallet
                if wallet is None and not self._cloud_ai_cutoff:
                    bal_txt = "余额同步中..."
                    state_suffix = "（连接云端中）"
                else:
                    bal = float((wallet or {}).get("balance_credits") or 0.0)
                    bal_txt = f"余额 {bal:,.0f} 积分"
                    if self._cloud_ai_cutoff:
                        state_suffix = f"（已暂停：{self._cloud_cutoff_reason}）"
                    elif self.cfg.coach_mode == "off":
                        state_suffix = "（纯本地引擎）"
                    else:
                        state_suffix = "（就绪）"
                cloud_state = f"已登录 {self.cloud_client.current_username} | {tier} | {bal_txt}{state_suffix}"
                if self.cloud_coach is not None and self.cloud_coach.last_stats is not None:
                    st = self.cloud_coach.last_stats
                    cloud_state += f" · 上次 {st.latency_ms}ms"
            else:
                if self._service_locked:
                    cloud_state = f"服务维护锁定（{self._service_lock_reason}）"
                elif self._force_update_required:
                    cloud_state = f"版本过低需强制更新（{self._force_update_reason}）"
                else:
                    cloud_state = (
                        "未登录（全功能已锁定）"
                        if self._cloud_cutoff_policy == "lock_all"
                        else "未登录（本地 5ms 大数据引擎正常运作）"
                    )
            mode_tag = f"{self.store.active_match_mode}{'（首棱彩局自动）' if self._auto_switched_first_prismatic else ''}" if self.store.descriptor else "-"
            rs = self.log_ring.summary_stats()
            with self._get_lcu_lock():
                lcu_creds = self.lcu.creds
                lcu_err = self.lcu.last_error
            lines = [
                f"状态机阶段：{self.sm.state.value}",
                f"客户端语言：{loc_label}",
                f"当前数据集：{format_display_patch(self.store.descriptor.patch) if self.store.descriptor else '未加载'} / {mode_tag}",
                f"缓存目录：{self.data_dir}",
                f"滚动运行日志：{rs['match_count']} / {rs['max_matches']} 局（超出自动覆盖最旧 · 约 {rs['total_bytes'] / 1024:.1f} KB）",
                f"LCU 连接：{'已连接 port=' + str(lcu_creds.port) + ' (' + lcu_creds.source + ')' if lcu_creds else '未连接 (' + (lcu_err or '等待客户端') + ')'}",
                f"2999 局内接口：{'活跃' if self._last_live else '未在对局中'}",
                f"截屏后端：{getattr(self, '_vision_backend', '') or self.capturer.backend}（连续黑帧={getattr(self, '_vision_consec_black', 0) or self.capturer.consecutive_black_frames}）",
                ocr_diag_line,
                f"云端 AI 参谋：{cloud_state}",
                f"全局热键：{format_hotkeys_summary(self.cfg.hotkeys)}",
            ]
            if loc_code == "zh_cn" and (
                is_riot_overseas_lockfile(self.cfg.lockfile_path)
                or is_riot_overseas_lockfile(getattr(lcu_creds, "source", ""))
            ):
                lines.append("外服客户端提示：检测到 Riot Games 客户端路径，若局内为新加坡简体或台服繁体，请在设置中切换「客户端语言」")
            if self._hotkey_os_occupied:
                lines.append(f"热键冲突警告：{'；'.join(self._hotkey_os_occupied.values())}")
            self.lookup_window.update_diagnostics(lines)
            return

        eff_key = self.cfg.resolved_gemini_api_key()
        th_mode = f"自适应<= {self.cfg.gemini_thinking_level}" if self.cfg.gemini_adaptive_thinking else self.cfg.gemini_thinking_level
        gemini_state = (
            f"直连就绪 ({mask_api_key(eff_key)} | {self.cfg.gemini_model} | {th_mode})"
            if eff_key
            else "未配置 Key"
        )
        if self.gemini_coach is not None and self.gemini_coach.last_stats is not None:
            st = self.gemini_coach.last_stats
            gemini_state += f" · 上次 {st.latency_ms}ms / tok={st.total_tokens}"
        elif self.gemini_coach is not None and self.gemini_coach.last_error:
            gemini_state += f" · 最近异常={self.gemini_coach.last_error[:60]}"
        mode_tag = f"{self.store.active_match_mode}{'（首棱彩局自动）' if self._auto_switched_first_prismatic else ''}" if self.store.descriptor else "-"
        rs = self.log_ring.summary_stats()
        with self._get_lcu_lock():
            lcu_creds = self.lcu.creds
            lcu_err = self.lcu.last_error
        lines = [
            f"状态机阶段：{self.sm.state.value}",
            f"客户端语言：{loc_label}",
            f"当前数据集：{format_display_patch(self.store.descriptor.patch) if self.store.descriptor else '未加载'} / {mode_tag}",
            f"缓存目录：{self.data_dir}",
            f"滚动运行日志：{rs['match_count']} / {rs['max_matches']} 局（超出自动覆盖最旧 · 约 {rs['total_bytes'] / 1024:.1f} KB）",
            f"LCU 连接：{'已连接 port=' + str(lcu_creds.port) + ' (' + lcu_creds.source + ')' if lcu_creds else '未连接 (' + (lcu_err or '等待客户端') + ')'}",
            f"2999 局内接口：{'活跃' if self._last_live else '未在对局中'}",
            f"截屏后端：{getattr(self, '_vision_backend', '') or self.capturer.backend}（连续黑帧={getattr(self, '_vision_consec_black', 0) or self.capturer.consecutive_black_frames}）",
            ocr_diag_line,
            f"AI 参谋模式：{self.cfg.coach_mode} | Gemini API：{gemini_state}",
            f"外部参谋桥接（可选）：http://127.0.0.1:{self.bridge.bound_port if self.bridge else 18421}（{'在线' if (self.bridge and self.bridge.is_coach_online()) else '待接入'}）",
            f"全局热键：{format_hotkeys_summary(self.cfg.hotkeys)}",
        ]
        if loc_code == "zh_cn" and (
            is_riot_overseas_lockfile(self.cfg.lockfile_path)
            or is_riot_overseas_lockfile(getattr(lcu_creds, "source", ""))
        ):
            lines.append("外服客户端提示：检测到 Riot Games 客户端路径，若局内为新加坡简体或台服繁体，请在设置中切换「客户端语言」")
        if self._hotkey_os_occupied:
            lines.append(f"热键冲突警告：{'；'.join(self._hotkey_os_occupied.values())}")
        if getattr(self, "_devlab_active", False) and not self._commercial:
            st = self._devlab.get_status_snapshot()
            seq = st.get("active_match_seq")
            st_lbl = f"录制中 #{int(seq):04d}" if seq is not None else "待机"
            disk_mb = float(st.get("disk_bytes") or 0) / (1024.0 * 1024.0)
            lines.append(
                f"DevLab 黑匣子：{st_lbl}（事件 {st.get('event_count', 0)} 条 · "
                f"ROI {st.get('roi_count', 0)}/{st.get('roi_cap', 15)} · "
                f"{st.get('match_count', 0)} 局 / {disk_mb:.2f} MB）"
            )
        self.lookup_window.update_diagnostics(lines)

    def shutdown(self) -> None:
        self._stop_event.set()
        self._heartbeat_timer.stop()
        if self._wallet_heartbeat_timer is not None:
            self._wallet_heartbeat_timer.stop()
        if self._wallet_retry_timer is not None:
            self._wallet_retry_timer.stop()
        if self._startup_update_timer is not None:
            self._startup_update_timer.stop()
        if getattr(self, "_devlab_badge_timer", None) is not None:
            self._devlab_badge_timer.stop()
        self._vision_timer.stop()
        self._click_timer.stop()
        self._picker_coach_debounce_timer.stop()
        self._death_coach_defer_timer.stop()
        self._cancel_augment_remove_coach_defer("shutdown")
        self._cancel_game_start_coach_defer("shutdown")
        self._icon_verify_timer.stop()
        self.hotkeys.unregister_all()
        try:
            self.qt_app.removeNativeEventFilter(self.hotkeys)
        except Exception:
            pass
        if getattr(self, "_ui_observers", None) is not None:
            try:
                self._ui_observers.uninstall()
            except Exception:
                pass
            self._ui_observers = None
        if getattr(self, "_devlab_active", False):
            if self._action_tracker is not None:
                try:
                    self._action_tracker.flush_pending_transactions()
                except Exception:
                    pass
            if (
                self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING, GameState.CHAMP_SELECT)
                or getattr(self, "_pending_eog_since", None) is not None
                or bool(self._active_match_key)
            ):
                try:
                    self._devlab.end_match(
                        won=self._match_won_result,
                        duration_s=int(self._match_max_game_time_s),
                        eog_source="app_shutdown",
                    )
                except Exception:
                    pass
            try:
                self._devlab.flush_and_close()
            except Exception:
                pass
        if self._active_match_key:
            try:
                self.log_ring.end_match(
                    won=self._match_won_result,
                    duration_s=int(self._match_max_game_time_s),
                )
            except Exception:
                pass
        if self.cloud_coach is not None:
            self.cloud_coach.stop()
        if self.cloud_client is not None:
            self.cloud_client.close()
        self.syncer.close()
        self.static_assets.close()
        if self.gemini_coach is not None:
            self.gemini_coach.stop()
        if self.bridge is not None:
            self.bridge.stop()
        if getattr(self, "_vision_worker", None) is not None:
            try:
                self._vision_worker.stop(timeout=2.0)
            except Exception as exc:
                logger.debug("HexVisionWorker stop skipped: %s", exc)
        self.capturer.close()
        with self._get_lcu_lock():
            self.lcu.close()
        self.live.close()
        if self.ocr_engine:
            self.ocr_engine.close()
        if self._active_recap_dialog is not None:
            try:
                self._active_recap_dialog.close()
                self._active_recap_dialog.deleteLater()
            except Exception:
                pass
            self._active_recap_dialog = None
        for attr in ("edge_drawer", "combo_overlay", "champ_overlay", "augment_overlay", "lookup_window"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
        self.tray.hide()
        self.qt_app.quit()
