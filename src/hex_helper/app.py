"""HexHelper state machine and PySide6 application controller."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
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
from hex_helper.data.special_builds import get_special_augment_db
from hex_helper.data.static_assets import StaticAssetsManager
from hex_helper.data.store import DataStore
from hex_helper.edition import APP_VERSION, is_commercial_edition
from hex_helper.setup.preflight import extract_bundled_seed_cache

if TYPE_CHECKING:
    from hex_helper.coach.gemini_coach import GeminiCallStats
from hex_helper.game.lcu_client import LcuClient
from hex_helper.game.live_client import LiveClientReader
from hex_helper.game.models import ChampSelectState, LiveMatchSnapshot
from hex_helper.scoring.augment_combo import (
    ComboPlan,
    PickerComboVerdict,
    build_combo_plan,
    evaluate_picker_combos,
    export_combo_coach_context,
)
from hex_helper.scoring.build_synergy import is_transformative_augment, item_is_owned, normalize_item_id
from hex_helper.scoring.destiny import DESTINY_AUGMENT_ID, rank_destiny_items
from hex_helper.scoring.matchup_optimizer import (
    TIER2_BOOT_IDS,
    TRINKET_AND_FREE_ITEM_IDS,
    DynamicItemRec,
    MatchupThreatProfile,
    NextBuyPlan,
    build_next_buy_plan,
    canonicalize_item_id,
    is_valid_plan_item,
    optimize_next_items,
    reconcile_match_build_plan,
)
from hex_helper.scoring.tier import AugmentScore, score_hero_augment
from hex_helper.ui.augment_combo_overlay import AugmentComboOverlay
from hex_helper.ui.augment_overlay import AugmentBadgeRenderItem, AugmentBadgesOverlay
from hex_helper.ui.calibration_ui import CalibrationOverlay
from hex_helper.ui.champ_select_ui import ChampSelectOverlay
from hex_helper.ui.edge_drawer import EdgeBuildDrawer
from hex_helper.ui.hotkey_settings import HotkeySettingsDialog, format_hotkeys_summary
from hex_helper.ui.lookup_window import LookupWindow
from hex_helper.ui.match_recap_card import MatchRecapData, MatchRecapDialog
from hex_helper.ui.theme import load_app_icon
from hex_helper.ui.win32_window import (
    GlobalHotkeyManager,
    UiBridgeInvoker,
    normalize_hotkey,
    physical_rect_to_logical,
    screen_for_physical_rect,
)
from hex_helper.vision.augment_icon_matcher import AugmentIconMatcher, IconMatchResult
from hex_helper.vision.capture import (
    ScreenCapturer,
    find_game_window,
    find_lcu_window,
    get_primary_screen_rect,
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


class HexStateMachine:
    """Pure, deterministic state machine governing match lifecycle and coach debounce."""

    PICKER_CLOSE_DEBOUNCE_S = 0.25
    DEATH_COACH_COOLDOWN_S = 50.0
    DEATH_SHOPPING_GOLD_THRESHOLD = 1100.0
    DEATH_FALLING_BEHIND_DIFF = 5

    def __init__(self, store: DataStore | None = None) -> None:
        self._store: DataStore | None = store
        self.state: GameState = GameState.IDLE
        self.current_hero_id: str | None = None
        self.manual_hero_override: str | None = None
        self.selected_augments: list[str] = []
        self.pending_transmute: PendingTransmute | None = None
        self.committed_plan: list[str] = []
        self.locked_next_item: str = ""
        self.allow_plan_overhaul: bool = False
        self.plan_overhaul_reason: str = ""
        self.active_cards: tuple[DetectedCard, ...] = ()
        self.last_offered_cards: tuple[DetectedCard, ...] = ()
        self._active_card_ids: tuple[str, ...] = ()
        self._picker_closed_since: float | None = None
        self._last_clicked_slot: int | None = None
        self._was_dead: bool = False
        self._last_enemy_items_sig: str = ""
        self._last_death_coach_mono: float = -999.0
        self._match_started_notified: bool = False

    def reset_match(self) -> None:
        self.current_hero_id = None
        self.selected_augments.clear()
        self.pending_transmute = None
        self.committed_plan.clear()
        self.locked_next_item = ""
        self.allow_plan_overhaul = False
        self.plan_overhaul_reason = ""
        self.active_cards = ()
        self.last_offered_cards = ()
        self._active_card_ids = ()
        self._picker_closed_since = None
        self._last_clicked_slot = None
        self._was_dead = False
        self._last_enemy_items_sig = ""
        self._last_death_coach_mono = -999.0
        self._match_started_notified = False

    def add_selected_augment(self, aug_id: str) -> bool:
        aid = str(aug_id)
        if aid and aid not in self.selected_augments and len(self.selected_augments) < 6:
            self.selected_augments.append(aid)
            return True
        return False

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
            self.invalidate_build_plan()
            return True
        return False

    def replace_last_augment(self, aug_id: str) -> bool:
        aid = str(aug_id)
        if not aid or aid in self.selected_augments:
            return False
        if self.selected_augments:
            old = self.selected_augments[-1]
            self.selected_augments[-1] = aid
            if self.pending_transmute and self.pending_transmute.source_augment_id == old:
                self.pending_transmute = None
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
            if aid not in self.selected_augments and len(self.selected_augments) < 6:
                self.selected_augments.append(aid)

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
        """Build enemy signature tracking ONLY complete items (>=2200G) and key tactical counters."""
        eff_store = store if store is not None else self._store
        return "|".join(
            f"{e.champion_name}:{','.join(sorted(normalize_item_id(iid) for iid in e.item_ids if is_significant_enemy_item(iid, eff_store)))}"
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
            eff.coach_trigger = "game_start"

        is_dead = bool(snap.my_player and snap.my_player.is_dead)
        if is_dead and not self._was_dead:
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
                cooldown_elapsed = now_mono - self._last_death_coach_mono
                if cooldown_elapsed < self.DEATH_COACH_COOLDOWN_S:
                    eff.death_skip_reason = (
                        f"cooldown ({cooldown_elapsed:.1f}s < {self.DEATH_COACH_COOLDOWN_S:.0f}s)"
                    )
                elif enemy_changed or has_shopping_gold or falling_behind:
                    self._last_enemy_items_sig = enemy_sig
                    self._last_death_coach_mono = now_mono
                    eff.coach_trigger = "death_enemy_build_changed"
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
        self._was_dead = is_dead
        return eff

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
            # Ignore non-augment Stat Anvil popups where duplicate IDs appear across slots (e.g. 属性！/属性！)
            unique_ids = {c.augment.augment_id for c in result.cards}
            if len(unique_ids) < 2:
                return eff
            self._picker_closed_since = None
            new_ids = tuple(c.augment.augment_id for c in result.cards)
            if self.state != GameState.AUGMENT_PICKING:
                self.state = GameState.AUGMENT_PICKING
                self._last_clicked_slot = None
                eff.state_changed = True
            if new_ids != self._active_card_ids or result.rerolled:
                self.active_cards = result.cards
                self.last_offered_cards = result.cards
                self._active_card_ids = new_ids
                self._last_clicked_slot = None
                eff.coach_trigger = "augment_picking"
            return eff

        if not result.picker_visible and self.state == GameState.AUGMENT_PICKING:
            if self._picker_closed_since is None:
                self._picker_closed_since = now_mono
                return eff
            if (now_mono - self._picker_closed_since) >= self.PICKER_CLOSE_DEBOUNCE_S:
                # Commit clicked augment if tracked (never auto-commit Stat Anvils 1402/1403/1404!)
                chosen_id = ""
                if self._last_clicked_slot is not None:
                    for c in self.active_cards:
                        if c.slot_index == self._last_clicked_slot:
                            chosen_id = c.augment.augment_id
                            break
                if chosen_id in ("1402", "1403", "1404"):
                    chosen_id = ""
                if chosen_id and self.add_selected_augment(chosen_id):
                    eff.newly_selected_augment = chosen_id
                    if is_transmute_augment(chosen_id):
                        fading = tuple(c.augment.augment_id for c in self.active_cards if c.augment.augment_id != chosen_id)
                        self.start_transmute_if_applicable(chosen_id, now_mono=now_mono, fading_ids=fading)
                        eff.transmute_started = True
                    else:
                        eff.coach_trigger = "augment_selected"
                self.state = GameState.IN_GAME
                self.active_cards = ()
                self._active_card_ids = ()
                self._picker_closed_since = None
                self._last_clicked_slot = None
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


class HexHelperController:
    """Main desktop runtime integrating data sync, telemetry, WinRT OCR, UI overlays, and AI coach."""

    PICKER_COACH_DEBOUNCE_MS = 1800
    DEATH_COACH_DEFER_MS = 2500
    ICON_VERIFY_DELAY_MS = 850
    ICON_VERIFY_CONF_THRESHOLD = 0.65
    SMART_PICKER_GATE_DELTA_GAP = 0.018
    TELEMETRY_MIN_DURATION_S = 180

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
    ) -> None:
        self.qt_app = qt_app
        self._commercial = is_commercial_edition()
        self.data_dir = get_app_data_dir()
        self.cfg: AppConfig = load_config()
        if self._commercial:
            self.cfg.proxy_url = ""
            self.cfg.bridge_enabled = False
            if self.cfg.coach_mode != "off":
                self.cfg.coach_mode = "cloud"
        self.ui_invoker = UiBridgeInvoker()
        self.sm = HexStateMachine()

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
        self.store = DataStore(self.static_assets)
        self.datasets: list[DatasetDescriptor] = []

        # Telemetry
        self.lcu = LcuClient(self.cfg.lockfile_path, force_all_modes=self.cfg.force_all_modes)
        self.live = LiveClientReader()
        self._last_live: LiveMatchSnapshot | None = None
        self._last_threat: MatchupThreatProfile | None = None
        self._last_dyn_recs: list[DynamicItemRec] = []
        self._last_next_plan: NextBuyPlan | None = None
        self._overlays_hidden_by_user = False

        # Vision
        self.capturer = ScreenCapturer(prefer_dxcam=True)
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
                allow_download=False,
            )
        except Exception as exc:
            logger.debug("AugmentIconMatcher init skipped: %s", exc)
        try:
            self.ocr_engine = WinRtOcrEngine("zh-Hans-CN")
            self.detector = AugmentPickerDetector(
                ocr_engine=self.ocr_engine,
                matcher=self.matcher,
                card_band_roi=self.cfg.card_band_roi,
                title_band_roi=self.cfg.title_band_roi,
            )
            self.transmute_watcher = TransmuteRevealWatcher(self.ocr_engine, [])
        except Exception as exc:
            logger.error("WinRT OCR initialization failed: %s", exc)

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
        self._match_state_path = self.data_dir / "match_state.json"
        self._combo_hidden_by_user: bool = False
        self._last_combo_plan: ComboPlan | None = None
        self._last_combo_verdict: PickerComboVerdict | None = None
        self._pre_picker_drawer_expanded: bool | None = None

        # Per-match telemetry & mode-switching closed-loop state
        self.ai_calls_this_match: int = 0
        self.ai_recommended_item_ids_shown: set[str] = set()
        self.user_swapped_this_match: int = 0
        self._match_max_game_time_s: float = 0.0
        self._match_last_my_items: tuple[str, ...] = ()
        self._match_last_hero_id: str = ""
        self._match_last_hero_name: str = ""
        self._match_telemetry_sent_key: str = ""
        self._match_is_first_prismatic: bool = False
        self._match_won_result: int = -1
        self._auto_switched_first_prismatic: bool = False
        self._last_kda: str = ""
        self._last_team_kills: str = ""
        self._best_ai_highlight: str = ""
        self._last_recap_data: MatchRecapData | None = None
        self._active_recap_dialog: MatchRecapDialog | None = None
        self._pending_icon_verify_candidates: tuple[str, ...] = ()
        self._pending_icon_verify_committed_id: str = ""
        self._pending_icon_verify_slot_hint: int | None = None
        self._local_spent_credits_delta: float = 0.0
        self._pending_lobby_unseal: bool = False
        self._last_402_trigger_reason: str = ""
        self._last_picker_gate_skipped_hint: str = ""
        self._core_lifecycle = None
        self._hotkey_os_occupied: dict[str, str] = {}

        # UI suite
        self.champ_overlay = ChampSelectOverlay(
            on_pos_changed=self._on_champ_pos_changed,
            on_size_changed=self._on_champ_size_changed,
            initial_size=self.cfg.champ_window_size,
            on_core_action=self._handle_core_action,
        )
        if self.cfg.champ_window_pos and len(self.cfg.champ_window_pos) == 2:
            self.champ_overlay.move(int(self.cfg.champ_window_pos[0]), int(self.cfg.champ_window_pos[1]))

        self.augment_overlay = AugmentBadgesOverlay()
        self.combo_overlay = AugmentComboOverlay(
            on_pos_changed=self._on_combo_pos_changed,
            on_size_changed=self._on_combo_size_changed,
            on_closed_by_user=self._on_combo_closed_by_user,
            initial_pos=self.cfg.combo_drawer_pos,
            initial_size=self.cfg.combo_drawer_size,
        )
        self.edge_drawer = EdgeBuildDrawer(
            on_select_augment=self._on_manual_add_augment,
            on_remove_augment=self._on_manual_remove_augment,
            on_replace_last_augment=self._on_replace_last_augment,
            on_resolve_transmute=lambda aid: self._on_resolve_transmute_augment(aid, source="manual"),
            on_dismiss_transmute=self._on_dismiss_transmute,
            on_call_coach=lambda: self.trigger_coach_request("manual_f6"),
            on_switch_dataset=self._on_switch_dataset,
            on_manual_hero=self._on_manual_hero,
            on_pos_changed=self._on_drawer_pos_changed,
            on_size_changed=self._on_drawer_size_changed,
            on_closed_by_user=self._on_drawer_closed_by_user,
            on_switch_tier=self._on_switch_tier,
            on_recharge_click=self.open_recharge_dialog,
            on_login_click=self.open_login_dialog,
            on_check_update=self._on_check_update_clicked,
            on_core_action=self._handle_core_action,
            on_opening_brief_seal_dismissed=self._on_opening_brief_seal_dismissed,
            initial_tier=self.cfg.coach_tier,
            initial_pos=self.cfg.drawer_pos,
            initial_size=self.cfg.drawer_size,
            y_ratio=self.cfg.drawer_y_ratio,
        )
        self.lookup_window = LookupWindow(
            cfg=self.cfg,
            on_save_settings=self._on_save_settings,
            on_sync_now=self.start_async_sync,
            on_open_calibration=self.open_calibrator,
            on_test_coach=lambda: self.trigger_coach_request("manual_f6"),
            on_test_gemini=self._on_test_gemini_connection,
            on_toggle_drawer=self._toggle_drawer_from_tray,
            on_toggle_combo=self._toggle_combo_from_tray,
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
        )
        self.calibrator = CalibrationOverlay(
            initial_title_roi=self.cfg.title_band_roi,
            on_calibrated=self._on_rois_calibrated,
        )

        # Tray & global hotkeys
        self._setup_tray()
        self.hotkeys = GlobalHotkeyManager()
        self.qt_app.installNativeEventFilter(self.hotkeys)
        self._register_hotkeys()

        # Instant boot from cached dataset (extract bundled seed first if needed)
        self._load_cached_on_startup()
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

        self._icon_verify_timer = QTimer()
        self._icon_verify_timer.setSingleShot(True)
        self._icon_verify_timer.setInterval(self.ICON_VERIFY_DELAY_MS)
        self._icon_verify_timer.timeout.connect(self._on_delayed_icon_verify)

        if show_lookup_on_start:
            self.lookup_window.show()

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(load_app_icon(), self.qt_app)
        self.tray.setToolTip("HexHelper · 海克斯大乱斗小助手")
        menu = QMenu()
        self._tray_act_lookup = QAction("打开速查与设置", menu)
        self._tray_act_lookup.triggered.connect(self._show_lookup)
        self._tray_act_drawer = QAction("显示/切换边缘出装条 (F3)", menu)
        self._tray_act_drawer.triggered.connect(self._toggle_drawer_from_tray)
        self._tray_act_combo = QAction("显示/切换海克斯联动浮窗 (F7)", menu)
        self._tray_act_combo.triggered.connect(self._toggle_combo_from_tray)
        self._tray_act_sidebars = QAction("左右侧边栏同步收起/展开 (F9)", menu)
        self._tray_act_sidebars.triggered.connect(self._toggle_sidebars_hotkey)
        self._tray_act_coach = QAction("立即呼叫 AI 参谋 (F6)", menu)
        self._tray_act_coach.triggered.connect(lambda: self.trigger_coach_request("manual_f6"))
        self._tray_act_cal = QAction("校准三选一区域 (F8)", menu)
        self._tray_act_cal.triggered.connect(self.open_calibrator)
        self._tray_act_hotkeys = QAction("自定义全局快捷键...", menu)
        self._tray_act_hotkeys.triggered.connect(self.open_hotkey_settings_dialog)
        act_sync = QAction("检查 26.23 数据更新", menu)
        act_sync.triggered.connect(self.start_async_sync)
        act_quit = QAction("退出 HexHelper", menu)
        act_quit.triggered.connect(self.shutdown)
        for a in (
            self._tray_act_lookup,
            self._tray_act_drawer,
            self._tray_act_combo,
            self._tray_act_sidebars,
            self._tray_act_coach,
            self._tray_act_cal,
            self._tray_act_hotkeys,
            act_sync,
        ):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_lookup() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        self.tray.show()

    def _register_hotkeys(self) -> None:
        self._reload_hotkeys()

    def _reload_hotkeys(self) -> dict[str, str]:
        hk = self.cfg.hotkeys
        self.hotkeys.unregister_all()
        bindings: tuple[tuple[str, str, Callable[[], None]], ...] = (
            ("force_scan", hk.force_scan, lambda: self._tick_vision(force=True)),
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
            ok = self.hotkeys.register(canonical, cb)
            if is_real_mgr and not ok:
                occupied[field_name] = f"「{canonical}」已被其他程序占用"
        self._hotkey_os_occupied = occupied
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
            self.edge_drawer.show_overlay()
        if self.cfg.combo_overlay_enabled and not self._is_full_locked():
            self._combo_hidden_by_user = False
            self._refresh_combo_plan_view()
            self.combo_overlay.set_expanded(True)
            if not self.combo_overlay.isVisible():
                self.combo_overlay.show_overlay()

    def _toggle_lookup_hotkey(self) -> None:
        if self.lookup_window.isVisible() and not self.lookup_window.isMinimized():
            self.lookup_window.hide()
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

    def _toggle_drawer_from_tray(self) -> None:
        self._overlays_hidden_by_user = False
        if self.edge_drawer.isVisible():
            self.edge_drawer.hide()
        else:
            self.edge_drawer.set_expanded(True)
            self.edge_drawer.show_overlay()

    def _toggle_drawer_hotkey(self) -> None:
        self._overlays_hidden_by_user = False
        if not self.edge_drawer.isVisible():
            self.edge_drawer.set_expanded(True)
            self.edge_drawer.show_overlay()
        else:
            self.edge_drawer.toggle_expanded()

    def _on_drawer_pos_changed(self, pos: list[int]) -> None:
        self.cfg.drawer_pos = pos
        save_config(self.cfg)

    def _on_drawer_size_changed(self, size: list[int] | None) -> None:
        self.cfg.drawer_size = size
        save_config(self.cfg)

    def _on_drawer_closed_by_user(self) -> None:
        self._overlays_hidden_by_user = True

    def _toggle_combo_from_tray(self) -> None:
        if self._is_full_locked():
            self.combo_overlay.hide()
            return
        self._combo_hidden_by_user = False
        if self.combo_overlay.isVisible():
            self.combo_overlay.hide()
            self._combo_hidden_by_user = True
        else:
            self._refresh_combo_plan_view()
            self.combo_overlay.set_expanded(True)
            self.combo_overlay.show_overlay()

    def _toggle_combo_hotkey(self) -> None:
        if self._is_full_locked():
            self.combo_overlay.hide()
            return
        self._combo_hidden_by_user = False
        if not self.combo_overlay.isVisible():
            self._refresh_combo_plan_view()
            self.combo_overlay.set_expanded(True)
            self.combo_overlay.show_overlay()
        else:
            self.combo_overlay.toggle_expanded()

    def _on_combo_pos_changed(self, pos: list[int]) -> None:
        self.cfg.combo_drawer_pos = pos
        save_config(self.cfg)

    def _on_combo_size_changed(self, size: list[int] | None) -> None:
        self.cfg.combo_drawer_size = size
        save_config(self.cfg)

    def _on_combo_closed_by_user(self) -> None:
        self._combo_hidden_by_user = True

    def _toggle_all_overlays(self) -> None:
        self._overlays_hidden_by_user = not self._overlays_hidden_by_user
        if self._overlays_hidden_by_user:
            self.champ_overlay.hide()
            self.augment_overlay.hide()
            self.edge_drawer.hide()
            self.combo_overlay.hide()
        else:
            if self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
                self.edge_drawer.show_overlay()
                if self.cfg.combo_overlay_enabled and not self._combo_hidden_by_user and not self._is_full_locked():
                    self.combo_overlay.show_overlay()

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
        if cached is not None:
            desc, ddir = cached
            self.store.load_from_dir(desc, ddir, band=self.cfg.dataset_band)
            self._preload_first_prismatic_from_cache(desc, ddir)
            all_augs = self.store.all_augments()
            self.matcher.update_catalog(all_augs)
            if self.transmute_watcher is not None:
                self.transmute_watcher.update_catalog(all_augs)
            self.lookup_window.bind_store(self.store)
            self.edge_drawer.populate_catalogs(self.store, [desc])

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
                    all_augs = self.store.all_augments()
                    self.matcher.update_catalog(all_augs)
                    if self.transmute_watcher is not None:
                        self.transmute_watcher.update_catalog(all_augs)
                    self.lookup_window.set_datasets(datasets)
                    self.lookup_window.bind_store(self.store)
                    self.edge_drawer.populate_catalogs(self.store, datasets)
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
        if self._commercial:
            cfg.proxy_url = ""
            cfg.bridge_enabled = False
            if cfg.coach_mode != "off":
                cfg.coach_mode = "cloud"
        self.cfg = cfg
        save_config(cfg)
        self.syncer.proxy_url = "" if self._commercial else cfg.proxy_url
        self.static_assets.proxy_url = "" if self._commercial else cfg.proxy_url
        self.lcu.preferred_lockfile = cfg.lockfile_path
        self.lcu.force_all_modes = cfg.force_all_modes
        self.lcu.creds = None
        if cfg.auto_first_prismatic_mode:
            self._preload_first_prismatic_from_cache(self.store.descriptor, self.store.dataset_dir)
        elif self._auto_switched_first_prismatic:
            self._restore_configured_dataset_mode()
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
            self._refresh_combo_plan_view()
            if not self.combo_overlay.isVisible():
                self.combo_overlay.show_overlay()
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

    def _on_rois_calibrated(self, card_roi: NormalizedRect, title_roi: NormalizedRect) -> None:
        self.cfg.card_band_roi = card_roi
        self.cfg.title_band_roi = title_roi
        save_config(self.cfg)
        if self.detector is not None:
            self.detector.update_rois(card_roi, title_roi)
        self._refresh_diagnostics()

    def open_calibrator(self) -> None:
        self.calibrator.open_calibrator()

    def _on_manual_hero(self, hero_id: str) -> None:
        self.sm.manual_hero_override = hero_id
        self._rollback_plan_and_advice_for_augment_correction()
        self._refresh_drawer_view()

    def _recompute_overhaul_reason_from_selected(self) -> str:
        """Derive active overhaul label strictly from currently owned augments (drops removed/corrected ones)."""
        for aid in reversed(self.sm.selected_augments):
            if is_transformative_augment(self.store, aid):
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
        if is_transformative_augment(self.store, aug_id):
            aug = self.store.get_augment(aug_id)
            reason = f"海克斯变阵：{aug.name if aug else aug_id}"
            self.sm.invalidate_build_plan(reason=reason)
            self._latest_advice = None
            self._latest_request_id = f"rollback-{uuid.uuid4().hex[:8]}"
            self.edge_drawer.set_coach_advice(None)
            logger.info("Transformative augment acquired -> rolling build plan overhaul: %s", reason)
        elif str(aug_id) == DESTINY_AUGMENT_ID or get_special_augment_db().resolve(self.store, aug_id) is not None:
            reason = self._recompute_overhaul_reason_from_selected()
            self.sm.invalidate_build_plan(reason=reason)
            self._latest_advice = None
            self._latest_request_id = f"rollback-{uuid.uuid4().hex[:8]}"
            self.edge_drawer.set_coach_advice(None)
            logger.info("Special synergy augment acquired -> rolling unbought build plan (aug=%s)", aug_id)

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
        self._on_coach_advice(advice, latency_ms=stats.latency_ms)
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
        self._on_coach_advice(advice, latency_ms=stats.latency_ms)
        if self._latest_request_id == advice.request_id:
            self.lookup_window.set_test_connection_result(
                True,
                f"推演完成：{stats.model} ({stats.thinking_level}) · {stats.latency_ms}ms · tokens={stats.total_tokens} (thought={stats.thought_tokens})",
            )
        self._refresh_diagnostics()

    def _on_gemini_error(self, request_id: str, err: str) -> None:
        if self._latest_request_id and request_id and request_id != self._latest_request_id:
            return
        self.edge_drawer.set_coach_error(err)
        self.lookup_window.set_test_connection_result(False, err)
        self._refresh_diagnostics()

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
        if self.sm.add_selected_augment(aug_id):
            if is_transmute_augment(aug_id):
                self.sm.start_transmute_if_applicable(aug_id, now_mono=time.monotonic())
                if self.transmute_watcher is not None:
                    self.transmute_watcher.reset()
                self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
                self._save_match_state()
                self._refresh_drawer_view()
                return
            self._flag_if_transformative_augment(aug_id)
            self._save_match_state()
            self._refresh_drawer_view()
            if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                self.trigger_coach_request("augment_selected")

    def _on_manual_remove_augment(self, idx: int) -> None:
        if self.sm.remove_selected_augment(idx):
            self._rollback_plan_and_advice_for_augment_correction()
            self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
            self._save_match_state()
            self._refresh_drawer_view()
            if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                self.trigger_coach_request("augment_selected")

    def _on_replace_last_augment(self, aug_id: str) -> None:
        if self.sm.replace_last_augment(aug_id):
            self.user_swapped_this_match = getattr(self, "user_swapped_this_match", 0) + 1
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
                return
            if self.transmute_watcher is not None:
                self.transmute_watcher.reset()
            self.edge_drawer.set_pending_transmute(self.sm.pending_transmute, self.store)
            self._flag_if_transformative_augment(aug_id)
            self._save_match_state()
            self._refresh_drawer_view()
            if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                self.trigger_coach_request("augment_selected")

    def _on_coach_online(self, _online: bool) -> None:
        self._refresh_coach_online_ui()
        self._refresh_diagnostics()

    def _on_coach_advice(self, advice: CoachAdvicePayload, latency_ms: int = 0) -> None:
        # Timeline sync guard: ignore late replies belonging to superseded requests!
        if self._latest_request_id and advice.request_id and advice.request_id != self._latest_request_id:
            logger.info(
                "Dropping stale coach advice for %s (latest=%s)",
                advice.request_id,
                self._latest_request_id,
            )
            return

        # Reconcile Coach's proposed items against our Committed Match Build Plan (strict DDragon complete items only!)
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        proposed_ids: list[str] = []
        shown_next_ids: list[str] = []
        for it in advice.next_items:
            iid = canonicalize_item_id(it.item_id) if it.item_id else ""
            if (not iid or not is_valid_plan_item(self.store, iid)) and it.name:
                for k, info in self.store.static.items.items():
                    if info.name == it.name:
                        iid = canonicalize_item_id(k)
                        break
            if iid and iid not in proposed_ids and is_valid_plan_item(self.store, iid):
                proposed_ids.append(iid)
                shown_next_ids.append(normalize_item_id(iid))
        if shown_next_ids and hasattr(self, "ai_recommended_item_ids_shown"):
            self.ai_recommended_item_ids_shown.update(shown_next_ids)
        for nm in advice.build_path:
            for k, info in self.store.static.items.items():
                if info.name == nm:
                    nk = canonicalize_item_id(k)
                    if nk not in proposed_ids and is_valid_plan_item(self.store, nk):
                        proposed_ids.append(nk)
                    break

        if proposed_ids and self.store.is_loaded:
            # First real coach plan of the match or transformative-augment overhaul is allowed to lock/pivot the plan
            first_coach_lock = self._latest_advice is None
            new_lock, new_plan = reconcile_match_build_plan(
                store=self.store,
                my_items=my_items,
                committed_plan=self.sm.committed_plan,
                locked_next_item=self.sm.locked_next_item,
                proposed_ids=proposed_ids,
                allow_overhaul=self.sm.allow_plan_overhaul or first_coach_lock,
            )
            self.sm.locked_next_item = new_lock
            self.sm.committed_plan = new_plan
            if self.sm.allow_plan_overhaul:
                self.sm.allow_plan_overhaul = False
            self._save_match_state()

        self._latest_advice = advice
        self._update_best_ai_highlight(advice)
        self.edge_drawer.set_committed_plan(
            self.sm.committed_plan,
            self.sm.locked_next_item,
            self.sm.plan_overhaul_reason,
        )
        allow_brief_expand = (
            not self._overlays_hidden_by_user
            and not self._is_full_locked()
            and self.sm.state != GameState.AUGMENT_PICKING
        )
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

    def _telemetry_worker_loop(self) -> None:
        """Background daemon polling LCU and 127.0.0.1:2999 with ZERO Qt GUI main-thread blocking."""
        while not self._stop_event.is_set():
            try:
                if not self.store.is_loaded:
                    self._stop_event.wait(0.5)
                    continue

                live_snap = self.live.poll()
                if live_snap is not None:
                    self.ui_invoker.invoke.emit(lambda s=live_snap: self._apply_live_snapshot(s))
                    self._stop_event.wait(1.0)
                    continue

                phase, cs = self.lcu.poll()
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
                "committed_plan": list(self.sm.committed_plan),
                "locked_next_item": self.sm.locked_next_item,
                "plan_overhaul_reason": self.sm.plan_overhaul_reason,
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
                str(a) for a in (data.get("selected_augments") or []) if str(a) and str(a) not in ("1402", "1403", "1404")
            ]
            if restored_augs and not self.sm.selected_augments:
                self.sm.selected_augments = restored_augs[:6]
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
        """During Round 1 (0 selected augments), if all 3 detected cards are prismatic, switch to first-prismatic."""
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
        if lvl in (3, 7, 11, 15):
            return True
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
            if reason:
                logger.info("Cancelled deferred death_enemy_build_changed coach call: %s", reason)

    def _on_death_coach_defer_timeout(self) -> None:
        if self.sm.state != GameState.IN_GAME:
            return
        if self.cfg.auto_trigger_coach and self._is_any_coach_online():
            self.trigger_coach_request("death_enemy_build_changed")

    def _schedule_picker_coach_trigger(self) -> None:
        if not self.cfg.auto_trigger_coach or not self._is_any_coach_online():
            return
        self._picker_coach_debounce_timer.start(self.PICKER_COACH_DEBOUNCE_MS)

    def _cancel_picker_coach_debounce(self) -> None:
        if self._picker_coach_debounce_timer.isActive():
            self._picker_coach_debounce_timer.stop()

    def _on_picker_coach_debounce_timeout(self) -> None:
        if self.sm.state != GameState.AUGMENT_PICKING or not self.sm.active_cards:
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
        if not self.cfg.smart_picker_ai_gate or len(cards) < 2:
            return False, ""
        hid = self._active_hero_id()
        if not hid or not self.store.is_loaded:
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
            return False, ""
        valid.sort(key=lambda item: item[1].delta, reverse=True)
        top1_card, top1_sc = valid[0]
        top2_card, top2_sc = valid[1]

        # Do not call it a clear winner if even #1 is below rarity par (reroll candidate)
        top1_below_par = bool(top1_sc.is_below_rarity_par) if top1_sc.percentile is not None else (top1_sc.delta < 0.0)
        if top1_below_par:
            return False, ""

        delta_gap = top1_sc.delta - top2_sc.delta
        if delta_gap < self.SMART_PICKER_GATE_DELTA_GAP:
            return False, ""

        # Check combo verdict contradiction
        if verdict is None:
            my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
            verdict = self._last_combo_verdict or evaluate_picker_combos(
                store=self.store,
                hero_id=hid,
                selected_augments=self.sm.selected_augments,
                my_items=my_items,
                offered=[(c.slot_index, c.augment) for c in cards],
            )
        combo_by_slot = {vc.slot_index: vc for vc in verdict.cards}
        top1_combo = combo_by_slot.get(top1_card.slot_index)
        if top1_combo is not None and top1_combo.verdict == "conflict":
            return False, ""
        for vc in verdict.cards:
            if vc.slot_index == top1_card.slot_index:
                continue
            if vc.verdict in ("jackpot", "gamble") and not (top1_combo and top1_combo.verdict == "jackpot"):
                return False, ""
        if (
            verdict.best_slot_index >= 0
            and verdict.best_slot_index != top1_card.slot_index
            and (alt_vc := combo_by_slot.get(verdict.best_slot_index)) is not None
            and alt_vc.verdict in ("jackpot", "gamble", "synergy")
            and alt_vc.combo_delta >= 2.0
            and (top1_combo is None or alt_vc.combo_delta > top1_combo.combo_delta + 1.0)
        ):
            return False, ""

        # Check transformative augment tie (let AI weigh transformative pivot when gap isn't overwhelming)
        top1_tx = is_transformative_augment(self.store, top1_card.augment.augment_id)
        for other_card, other_sc in scored_cards:
            if other_card.slot_index == top1_card.slot_index:
                continue
            other_tx = (
                is_transformative_augment(self.store, other_card.augment.augment_id)
                or is_transmute_augment(other_card.augment.augment_id)
            )
            if other_tx:
                if top1_tx or other_sc.games < 30 or (top1_sc.delta - other_sc.delta) < 0.035:
                    return False, ""

        detail = f"{top1_sc.name} delta={top1_sc.delta * 100:+.2f}% gap={delta_gap * 100:+.2f}%"
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
            return
        game_rect = find_game_window()
        if game_rect is None:
            return
        cap = self.capturer.grab(region=game_rect.as_bbox())
        if cap.is_black or cap.frame_bgr is None:
            return
        self._verify_picked_augment_from_frame(
            cap.frame_bgr,
            game_rect=game_rect,
            candidate_ids=candidates,
            committed_id=committed_id,
            slot_index_hint=slot_hint,
        )

    def _verify_picked_augment_from_frame(
        self,
        frame_bgr: np.ndarray,
        *,
        game_rect,
        candidate_ids: tuple[str, ...],
        committed_id: str,
        slot_index_hint: int | None = None,
    ) -> IconMatchResult | None:
        if self.icon_matcher is None or self.icon_matcher.template_count == 0:
            return None
        hit = self.icon_matcher.verify_picked_augment(
            frame_bgr,
            game_rect,
            candidate_ids=candidate_ids,
            slot_index_hint=slot_index_hint,
        )
        if hit is None:
            return None
        if committed_id:
            if hit.augment_id == committed_id:
                logger.info(
                    "HUD icon matcher confirmed committed augment: %s (score=%.3f, slot=%d)",
                    hit.augment_id,
                    hit.score,
                    hit.hud_slot_index,
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
                )
                if not is_transmute_augment(hit.augment_id) and self.cfg.auto_trigger_coach and self._is_any_coach_online():
                    self.trigger_coach_request("augment_selected")
            return hit

        # ClickTracker returned None -> recover missed augment directly from HUD icon!
        if hit.augment_id not in ("1402", "1403", "1404") and self.sm.add_selected_augment(hit.augment_id):
            logger.info(
                "HUD icon matcher recovered missed augment pick: %s (score=%.3f, slot=%d)",
                hit.augment_id,
                hit.score,
                hit.hud_slot_index,
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
            )
            if not is_transmute_augment(hit.augment_id) and self.cfg.auto_trigger_coach and self._is_any_coach_online():
                self.trigger_coach_request("augment_selected")
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

        det_metrics = self.detector.get_match_metrics() if self.detector is not None else {}
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
        if static is None:
            return ""
        icons_dir = static.icons_dir / kind
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

    def build_match_recap_data(self, won: int = -1) -> MatchRecapData | None:
        """Assemble offline MatchRecapData from live match state when duration >= 180s."""
        duration_s = int(round(self._match_max_game_time_s))
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
        for aid in self.sm.selected_augments[:4]:
            aug = self.store.get_augment(aid)
            augments.append((aug.name if aug else str(aid), aug.rarity if aug else "gold"))

        combo_plan: ComboPlan | None = None
        if self.store.is_loaded and hid:
            fresh_plan = build_combo_plan(
                store=self.store,
                hero_id=hid,
                selected_augments=self.sm.selected_augments,
                my_items=self._match_last_my_items,
                limit=4,
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

        item_entries: list[tuple[str, str]] = []
        for raw_iid in self._match_last_my_items:
            raw_s = str(raw_iid or "").strip()
            if not raw_s:
                continue
            norm_s = normalize_item_id(raw_s)
            if raw_s in TRINKET_AND_FREE_ITEM_IDS or norm_s in TRINKET_AND_FREE_ITEM_IDS:
                continue
            info = self.store.get_item_info(raw_s)
            if info.category == "consumable":
                continue
            ico = self._resolve_local_cached_icon("item", norm_s, raw_s)
            item_entries.append((info.name, ico))
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
        return MatchRecapData(
            hero_id=str(hid or ""),
            hero_name=hero_name,
            hero_icon_path=hero_icon_path,
            won=won_val,
            kda=self._last_kda,
            duration_s=duration_s,
            team_kills=self._last_team_kills,
            is_first_prismatic=bool(
                self._match_is_first_prismatic or self.store.active_match_mode == "first-prismatic"
            ),
            augments=tuple(augments),
            combo_title=combo_title,
            combo_completed=combo_completed,
            item_entries=tuple(item_entries),
            ai_highlight=ai_hl,
            dataset_patch=patch_disp or "26.23",
        )

    def _capture_post_match_recap(self, won: int = -1) -> MatchRecapData | None:
        """Build & store post-match recap card, unlock LookupWindow button, and politely notify tray."""
        recap = self.build_match_recap_data(won=won)
        if recap is None:
            return None
        self._last_recap_data = recap
        res_lbl = "胜利" if recap.won == 1 else ("激战" if recap.won == 0 else "对局结束")
        summary_parts = [recap.hero_name, res_lbl]
        if recap.kda:
            summary_parts.append(recap.kda)
        if recap.combo_title:
            summary_parts.append(recap.combo_title)
        summary = " · ".join(p for p in summary_parts if p)
        self.lookup_window.set_recap_available(True, summary)
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
        dlg = MatchRecapDialog(
            self._last_recap_data,
            parent=self.lookup_window,
            lifecycle_state=(self._core_lifecycle.state if (self._commercial and self._core_lifecycle is not None) else None),
            snoozed=bool(self._core_lifecycle.snoozed if self._core_lifecycle is not None else False),
            on_core_action=self._handle_core_action if self._commercial else None,
        )
        dlg.setModal(False)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self._active_recap_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        return dlg

    def _apply_live_snapshot(self, live_snap: LiveMatchSnapshot) -> None:
        self._last_live = live_snap
        self._last_cs = None
        resolved_hid = None
        if live_snap.my_player:
            h = self.store.find_hero_by_name(live_snap.my_player.champion_name) or self.store.find_hero_by_name(
                live_snap.my_player.raw_champion_name
            )
            if h is not None:
                resolved_hid = h.hero_id
        eff = self.sm.on_live_snapshot(live_snap, resolved_hid, time.monotonic(), store=self.store)
        if eff.state_changed and self.sm.state == GameState.IN_GAME:
            self._overlays_hidden_by_user = False
            self._combo_hidden_by_user = False
            self._latest_advice = None
            self._latest_request_id = ""
            self._last_combo_verdict = None
            self._cancel_picker_coach_debounce()
            self._cancel_death_coach_defer()
            self._cancel_pending_icon_verify()
            self._restore_configured_dataset_mode()
            self._reset_match_telemetry_counters()
            if self.detector is not None:
                self.detector.reset_match_metrics()
            self.edge_drawer.reset_opening_brief_state()
            self.edge_drawer.set_coach_advice(None)
            self.edge_drawer.set_recent_offered([])
            self.edge_drawer.set_picker_active(False)
            self.edge_drawer.set_pending_transmute(None, self.store)
            self.combo_overlay.exit_picker_mode()
            if self.transmute_watcher is not None:
                self.transmute_watcher.reset()
            self._active_match_key = self._compute_match_key(live_snap, resolved_hid)
            self._restore_match_state_if_matching(self._active_match_key)
        elif self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING) and not self._active_match_key:
            self._active_match_key = self._compute_match_key(live_snap, resolved_hid)
            self._restore_match_state_if_matching(self._active_match_key)

        self._match_max_game_time_s = max(self._match_max_game_time_s, float(live_snap.game_time or 0.0))
        if live_snap.game_end_result in (0, 1):
            self._match_won_result = int(live_snap.game_end_result)
        if live_snap.my_player:
            self._match_last_my_items = tuple(live_snap.my_player.item_ids)
            self._last_kda = (
                f"{live_snap.my_player.kills}/{live_snap.my_player.deaths}/{live_snap.my_player.assists}"
            )
        if live_snap.my_team_kills or live_snap.enemy_team_kills:
            self._last_team_kills = f"{int(live_snap.my_team_kills)}:{int(live_snap.enemy_team_kills)}"
        active_hid = self._active_hero_id()
        if active_hid:
            self._match_last_hero_id = active_hid
            h_info = self.store.get_hero(active_hid)
            if h_info is not None:
                self._match_last_hero_name = h_info.name

        self.lookup_window.set_game_active(True)
        self.edge_drawer.set_live_vital_state(
            is_dead=bool(live_snap.my_player and live_snap.my_player.is_dead),
            game_time_s=float(live_snap.game_time or 0.0),
        )
        self.champ_overlay.hide()
        self._refresh_drawer_view()

        game_rect = find_game_window()
        if game_rect is not None:
            game_scr = screen_for_physical_rect((game_rect.left, game_rect.top, game_rect.width, game_rect.height))
            if game_scr is not None:
                self.edge_drawer.set_target_screen(game_scr)
                self.combo_overlay.set_target_screen(game_scr)
        game_fg = True if game_rect is None else is_hwnd_foreground(game_rect.hwnd)
        if not self._overlays_hidden_by_user and game_fg:
            if eff.auto_peek_drawer and self.cfg.auto_peek_on_death:
                self.edge_drawer.set_expanded(True)
            elif eff.auto_collapse_drawer and self.cfg.auto_peek_on_death:
                self.edge_drawer.set_expanded(False)
            if not self.edge_drawer.isVisible():
                self.edge_drawer.show_overlay()
            if (
                self.cfg.combo_overlay_enabled
                and not self._combo_hidden_by_user
                and not self._is_full_locked()
                and not self.combo_overlay.isVisible()
            ):
                self.combo_overlay.show_overlay()
        elif not game_fg:
            if self.edge_drawer.isVisible():
                self.edge_drawer.hide()
            if self.augment_overlay.isVisible():
                self.augment_overlay.hide()
            if self.combo_overlay.isVisible():
                self.combo_overlay.hide()

        if eff.death_detected and not eff.coach_trigger and eff.death_skip_reason:
            logger.info("Skipping death_enemy_build_changed: %s", eff.death_skip_reason)

        if eff.coach_trigger and self.cfg.auto_trigger_coach and self._is_any_coach_online():
            if eff.coach_trigger == "death_enemy_build_changed":
                my_gold = float(live_snap.current_gold or 0.0)
                falling_behind = bool(
                    live_snap.my_player is not None
                    and live_snap.my_player.deaths >= live_snap.my_player.kills + self.sm.DEATH_FALLING_BEHIND_DIFF
                )
                if not eff.enemy_sig_changed and my_gold < self.sm.DEATH_SHOPPING_GOLD_THRESHOLD and not falling_behind:
                    logger.info(
                        "Skipping death_enemy_build_changed: neither significant enemy item change nor gold>=%dG nor falling behind",
                        int(self.sm.DEATH_SHOPPING_GOLD_THRESHOLD),
                    )
                elif self.sm.state == GameState.AUGMENT_PICKING:
                    logger.info("Skipping death_enemy_build_changed: AUGMENT_PICKING already active")
                elif self._should_defer_death_coach(live_snap):
                    self._schedule_death_coach_defer()
                else:
                    self.trigger_coach_request(eff.coach_trigger)
            else:
                self.trigger_coach_request(eff.coach_trigger)

    def _apply_lcu_poll(self, phase: str, cs: ChampSelectState | None) -> None:
        self._last_live = None
        if self.sm.state in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            resolved_won = self._match_won_result
            if resolved_won not in (0, 1) and phase in ("EndOfGame", "PreEndOfGame"):
                try:
                    resolved_won = self.lcu.get_eog_won()
                except Exception:
                    resolved_won = -1
            self._maybe_submit_match_telemetry(won=resolved_won)
            self._capture_post_match_recap(won=resolved_won)
            self._cancel_picker_coach_debounce()
            self._cancel_death_coach_defer()
            self._cancel_pending_icon_verify()
            self.sm.on_live_snapshot(None, None, time.monotonic())
            self._restore_configured_dataset_mode()
            self._reset_match_telemetry_counters()
            self._clear_match_state()
            self._latest_advice = None
            self._latest_request_id = ""
            self._last_combo_verdict = None
            self._last_combo_plan = None
            self.edge_drawer.reset_opening_brief_state()
            self.edge_drawer.set_coach_advice(None)
            self.edge_drawer.set_recent_offered([])
            self.edge_drawer.set_picker_active(False)
            self.edge_drawer.set_pending_transmute(None, self.store)
            self.augment_overlay.hide()
            self.edge_drawer.hide()
            self.combo_overlay.exit_picker_mode()
            self.combo_overlay.hide()
            self.lookup_window.set_game_active(False)
            self._last_picker_gate_skipped_hint = ""
            if self.detector:
                self.detector.reset()
                self.detector.reset_match_metrics()
            if self.transmute_watcher is not None:
                self.transmute_watcher.reset()
        elif phase in ("EndOfGame", "PreEndOfGame", "Lobby", "None") and self._auto_switched_first_prismatic:
            self._restore_configured_dataset_mode()

        self.sm.on_lcu_poll(phase, cs)
        self._last_cs = cs if self.sm.state == GameState.CHAMP_SELECT else None
        if self.sm.state == GameState.CHAMP_SELECT and cs is not None and not self._overlays_hidden_by_user and not self._is_full_locked():
            self.champ_overlay.update_state(cs, self.store)
            if not self.cfg.champ_window_pos:
                lcu_rect = find_lcu_window()
                if lcu_rect is not None:
                    self.champ_overlay.position_beside_lcu(
                        (lcu_rect.left, lcu_rect.top, lcu_rect.width, lcu_rect.height)
                    )
            if not self.champ_overlay.isVisible():
                self.champ_overlay.show_overlay()
        else:
            if self.champ_overlay.isVisible():
                self.champ_overlay.hide()

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

    def _check_fast_picker_vanish(self, ts: float) -> None:
        """<1ms 16Hz visual check detecting the exact 60ms frame the 3 cards close (or 1 card rerolls)."""
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
            # Exact picker-close instant caught within ±30ms! Freeze cursor/click pick IMMEDIATELY.
            self._visual_vanish_mono = ts
            valid_slots = {c.slot_index for c in self.sm.active_cards}
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
        elif len(changed) == 1 and all(d < 16.0 for s, d in diffs.items() if s != changed[0]):
            # Only 1 card flipped while the other 2 stayed intact -> single-slot Reroll!
            rslot = changed[0]
            if rslot != self._last_rerolled_slot or (ts - self._last_reroll_mono) > 0.45:
                self._last_rerolled_slot = rslot
                self._last_reroll_mono = ts
                self.click_tracker.mark_reroll(rslot, now_mono=ts)

    def _tick_click_sample(self) -> None:
        """33Hz lightweight read-only cursor + 16Hz fast visual vanish sampler active only during AUGMENT_PICKING."""
        if self.sm.state != GameState.AUGMENT_PICKING or not self.sm.active_cards:
            self._click_timer.stop()
            return
        game_rect = find_game_window() or get_primary_screen_rect()
        card_boxes = {c.slot_index: c.screen_box for c in self.sm.active_cards}
        hit_boxes = build_slot_hit_boxes(game_rect, card_boxes)
        reroll_y = int(game_rect.top + game_rect.height * 0.77)
        ts = time.monotonic()
        prev = self.click_tracker.last_clicked_slot
        slot = self.click_tracker.poll(hit_boxes, now_mono=ts, reroll_y_threshold=reroll_y)
        if slot is not None and slot != prev:
            chosen_name = next((c.augment.name for c in self.sm.active_cards if c.slot_index == slot), str(slot))
            logger.info("Augment picker click tracked: slot=%s (%s) pos=%s", slot, chosen_name, self.click_tracker.last_click_pos)
        self._click_sample_counter += 1
        if (self._click_sample_counter % 2) == 0:
            self._check_fast_picker_vanish(ts)

    def _tick_vision(self, force: bool = False) -> None:
        if not self.store.is_loaded or self.detector is None:
            return
        if not force and self.sm.state not in (GameState.IN_GAME, GameState.AUGMENT_PICKING):
            return

        game_rect = find_game_window()
        if game_rect is None:
            if not force:
                return
            game_rect = get_primary_screen_rect()
        elif not force and not is_hwnd_foreground(game_rect.hwnd):
            return

        now_mono = time.monotonic()
        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards:
            card_boxes = {c.slot_index: c.screen_box for c in self.sm.active_cards}
            reroll_y = int(game_rect.top + game_rect.height * 0.77)
            self.click_tracker.poll(
                build_slot_hit_boxes(game_rect, card_boxes),
                now_mono=now_mono,
                reroll_y_threshold=reroll_y,
            )

        cap = self.capturer.grab(region=game_rect.as_bbox())
        if cap.is_black or cap.frame_bgr is None:
            if self.capturer.consecutive_black_frames >= 5:
                self.edge_drawer.set_warning("检测到游戏画面全黑，请将游戏视频模式设为「无边框窗口」")
            return
        if self.capturer.consecutive_black_frames == 0:
            self.edge_drawer.set_warning("")

        was_picking = self.sm.state == GameState.AUGMENT_PICKING
        res = self.detector.process_frame(
            cap.frame_bgr,
            game_rect=game_rect,
            exclude_rects=self._get_all_overlay_exclude_rects(),
            force=force,
        )

        # Freeze click / cursor-dwell selection AT THE EXACT FIRST FRAME cards vanish (before cursor moves to lane!)
        old_cards_by_slot = {c.slot_index: c.augment.augment_id for c in self.sm.active_cards}
        resolved_slot: int | None = None
        if was_picking and self.sm.active_cards:
            if res.picker_visible and res.cards_seen_on_frame:
                self._last_cards_seen_mono = now_mono
                self._visual_vanish_mono = None
                self._frozen_pick_slot = None
                self._frozen_pick_source = ""
                self._frozen_pick_confidence = 0.0
                if res.cards:
                    self._capture_picker_slot_refs(cap.frame_bgr, game_rect, res.cards)
                resolved_slot = self.click_tracker.last_clicked_slot
            else:
                if self._frozen_pick_slot is None:
                    valid_slots = {c.slot_index for c in self.sm.active_cards}
                    # Prefer exact 60ms visual-vanish ts; fallback to recent pre-OCR close instant (never stale reroll ts!)
                    fallback_close = max(self._last_cards_seen_mono, now_mono - 0.14) if self._last_cards_seen_mono > 0 else now_mono
                    est_close = self._visual_vanish_mono if self._visual_vanish_mono is not None else fallback_close
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

        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards:
            self._cancel_death_coach_defer("AUGMENT_PICKING opened")
            self._cancel_pending_icon_verify()
            if not was_picking:
                self._last_cards_seen_mono = now_mono
                self._visual_vanish_mono = None
                self._frozen_pick_slot = None
                self._frozen_pick_source = ""
                self._frozen_pick_confidence = 0.0
                self._last_rerolled_slot = None
                self._last_reroll_mono = 0.0
                self._click_sample_counter = 0
                self._capture_picker_slot_refs(cap.frame_bgr, game_rect, self.sm.active_cards)
                self.click_tracker.reset(now_mono=now_mono)
                self._click_timer.start()
                self.edge_drawer.set_picker_active(True)
                # Auto-collapse right drawer on compact logical widths (<1800px, e.g. 1080p@125%/150%) so it NEVER covers card #3
                log_game, _ = physical_rect_to_logical(
                    (game_rect.left, game_rect.top, game_rect.width, game_rect.height)
                )
                if log_game.width() < 1800 and self.edge_drawer._expanded:
                    self._pre_picker_drawer_expanded = True
                    self.edge_drawer.set_expanded(False)
                else:
                    self._pre_picker_drawer_expanded = None
                logger.info(
                    "Augment picker opened: %s",
                    [(c.slot_index, c.augment.name) for c in self.sm.active_cards],
                )
            elif res.rerolled and res.stable:
                self._last_cards_seen_mono = now_mono
                self._visual_vanish_mono = None
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
                self._capture_picker_slot_refs(cap.frame_bgr, game_rect, self.sm.active_cards)
                self.click_tracker.mark_reroll(slot=self._last_rerolled_slot, now_mono=now_mono)
                logger.info(
                    "Augment picker rerolled: %s",
                    [(c.slot_index, c.augment.name) for c in self.sm.active_cards],
                )
            self._maybe_auto_switch_first_prismatic(self.sm.active_cards)
            if not self._overlays_hidden_by_user and not self._is_full_locked():
                self._render_augment_badges(self.sm.active_cards, game_rect)
            else:
                if self.augment_overlay.isVisible():
                    self.augment_overlay.hide()
                if self.combo_overlay.mode == "picker":
                    self.combo_overlay.exit_picker_mode()
        else:
            if self._click_timer.isActive():
                self._click_timer.stop()
            self._picker_slot_refs.clear()
            self._picker_ref_band_bbox = None
            self._visual_vanish_mono = None
            self._last_rerolled_slot = None
            if self.augment_overlay.isVisible():
                self.augment_overlay.hide()
            if was_picking:
                self._cancel_picker_coach_debounce()
                pick_conf = self._frozen_pick_confidence
                self._last_combo_verdict = None
                self.combo_overlay.exit_picker_mode()
                self.edge_drawer.set_picker_active(False)
                if self._pre_picker_drawer_expanded is not None:
                    restore_dr = self._pre_picker_drawer_expanded
                    self._pre_picker_drawer_expanded = None
                    if restore_dr and not self._overlays_hidden_by_user:
                        self.edge_drawer.set_expanded(True)
                if eff.newly_selected_augment:
                    self._flag_if_transformative_augment(eff.newly_selected_augment)
                    self._save_match_state()
                    self.edge_drawer.flash_new_augment(eff.newly_selected_augment)
                    aug = self.store.get_augment(eff.newly_selected_augment)
                    logger.info(
                        "Augment auto-committed (%s, conf=%.2f): %s (%d/4)",
                        self._frozen_pick_source or "click",
                        pick_conf,
                        aug.name if aug else eff.newly_selected_augment,
                        len(self.sm.selected_augments),
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
                    # Show tiny 1-click swap bar in case rare edge case mis-tracked
                    self.edge_drawer.set_recent_offered(
                        [c.augment for c in self.sm.last_offered_cards],
                        auto_committed_id=eff.newly_selected_augment,
                    )
                elif self.sm.last_offered_cards:
                    logger.info(
                        "Augment picker closed without cursor/click inside cards; surfacing 1-click chips"
                    )
                    self.edge_drawer.set_recent_offered([c.augment for c in self.sm.last_offered_cards])
                if (
                    (resolved_slot is None or pick_conf < self.ICON_VERIFY_CONF_THRESHOLD)
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

        # Secondary OCR + visual icon matching loop for Transmute / Pandora augments
        if self.sm.state == GameState.IN_GAME and self.sm.pending_transmute is not None:
            if self.transmute_watcher is not None:
                hits = self.transmute_watcher.process_frame(
                    cap.frame_bgr,
                    game_rect=game_rect,
                    pending=self.sm.pending_transmute,
                    already_owned_ids=set(self.sm.selected_augments),
                    exclude_rects=self._get_all_overlay_exclude_rects(),
                    cursor_pos=get_cursor_pos(),
                    now_mono=now_mono,
                )
                for hit in hits:
                    self._on_resolve_transmute_augment(hit.augment.augment_id, source=hit.channel)
            if self.sm.pending_transmute is not None:
                self._try_resolve_transmute_via_icon(cap.frame_bgr, game_rect)
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
            and len(self.sm.selected_augments) < 4
            and self.transmute_watcher is not None
        ):
            # Passive always-on HUD / Tab hover auto-sync (recovers missed augments without clicking '+'!)
            phit = self.transmute_watcher.scan_passive_hud_hover(
                cap.frame_bgr,
                game_rect=game_rect,
                already_owned_ids=set(self.sm.selected_augments),
                exclude_rects=self._get_all_overlay_exclude_rects(),
                cursor_pos=get_cursor_pos(),
                now_mono=now_mono,
            )
            if phit is not None and self.sm.add_selected_augment(phit.augment.augment_id):
                self._flag_if_transformative_augment(phit.augment.augment_id)
                self._save_match_state()
                logger.info(
                    "Augment auto-synced via passive HUD hover: %s (%d/4)",
                    phit.augment.name,
                    len(self.sm.selected_augments),
                )
                self._refresh_drawer_view()
                self.edge_drawer.flash_new_augment(phit.augment.augment_id)
                if self.cfg.auto_trigger_coach and self._is_any_coach_online():
                    self.trigger_coach_request("augment_selected")

        if eff.newly_selected_augment:
            self._refresh_drawer_view()

        if eff.coach_trigger and self.cfg.auto_trigger_coach and self._is_any_coach_online():
            if eff.coach_trigger == "augment_picking":
                self._schedule_picker_coach_trigger()
            else:
                self.trigger_coach_request(eff.coach_trigger)

    def _render_augment_badges(self, cards: tuple[DetectedCard, ...], game_rect=None) -> None:
        if self._is_full_locked():
            if self.augment_overlay.isVisible():
                self.augment_overlay.hide()
            if self.combo_overlay.isVisible():
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

        # Evaluate independent combo verdict first so we know if any card has jackpot/gamble upside
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        verdict = evaluate_picker_combos(
            store=self.store,
            hero_id=hid,
            selected_augments=self.sm.selected_augments,
            my_items=my_items,
            offered=[(c.slot_index, c.augment) for c in cards],
        )
        self._last_combo_verdict = verdict
        combo_by_slot = {vc.slot_index: vc for vc in verdict.cards}

        best_idx = -1
        best_delta = -999.0
        any_valid_sample = False
        valid_scores: list[AugmentScore] = []
        for i, (c, sc) in enumerate(scored_cards):
            if sc.games >= 30:
                any_valid_sample = True
                valid_scores.append(sc)
                if sc.delta > best_delta:
                    best_delta = sc.delta
                    best_idx = i

        all_below_par = bool(valid_scores) and all(
            (bool(sc.is_below_rarity_par) if sc.percentile is not None else (sc.delta < 0.0))
            for sc in valid_scores
        )
        has_combo_upside = any(vc.verdict in ("jackpot", "gamble") for vc in verdict.cards)
        reroll_banner = ""
        if best_idx >= 0 and all_below_par and not has_combo_upside:
            # All offered cards are below rarity par (or negative delta when dist unavailable) and have no combo upside:
            # do NOT mislead player with a gold "首选" crown — surface a clear reroll-dice warning!
            best_tier = scored_cards[best_idx][1].tier
            best_idx = -1
            reroll_banner = f"本轮三卡均低于同阶均线（最高仅 {best_tier} 级）· 建议用骰子刷新"
        elif best_idx >= 0 and all_below_par and has_combo_upside:
            # Prefer the combo jackpot/gamble card rather than a below-par non-synergy card
            best_combo_slot = verdict.best_slot_index
            best_idx = next(
                (i for i, (c, _sc) in enumerate(scored_cards) if c.slot_index == best_combo_slot),
                -1,
            )
        elif not any_valid_sample and verdict.reroll_advice:
            reroll_banner = verdict.reroll_advice

        sp_db = get_special_augment_db()
        render_items: list[AugmentBadgeRenderItem] = []
        for i, (c, sc) in enumerate(scored_cards):
            aid = c.augment.augment_id
            d_items = tuple(rank_destiny_items(self.store, hid, limit=2)) if (hid and aid == DESTINY_AUGMENT_ID) else ()
            sp = sp_db.get_by_id(aid)
            arch_hint = ""
            if sp is not None:
                key_items = [self.store.get_item_info(iid).name for iid in (sp.force_items or sp.core_items)[:2]]
                key_items = [n for n in key_items if n and not n.startswith("装备#")]
                short_arch = sp.archetype[:10]
                arch_hint = f"{short_arch} · {'+'.join(n[:4] for n in key_items)}" if key_items else short_arch
            render_items.append(
                AugmentBadgeRenderItem(
                    slot_index=c.slot_index,
                    score=sc,
                    screen_box=c.screen_box,
                    is_best=(i == best_idx),
                    destiny_items=d_items,
                    uncertain=(c.confidence < 0.72),
                    archetype_hint=arch_hint,
                    is_transmute=is_transmute_augment(aid),
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
        self.augment_overlay.render_cards(
            render_items,
            screen_rect=(rect.left, rect.top, rect.width, rect.height),
            coach_augment_tip=tip,
            coach_pending=self.edge_drawer._coach_pending,
            reroll_banner=reroll_banner,
            gate_skipped_hint=gate_hint,
        )

        if (
            self.cfg.combo_overlay_enabled
            and not self._combo_hidden_by_user
            and not self._overlays_hidden_by_user
            and not self._is_full_locked()
        ):
            self.combo_overlay.enter_picker_mode(verdict)
            if not self.combo_overlay.isVisible():
                self.combo_overlay.show_overlay()

    def _refresh_drawer_view(self) -> None:
        hid = self._active_hero_id()
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        my_gold = self._last_live.current_gold if self._last_live else 0.0
        allies = self._last_live.allies if self._last_live else ()
        enemies = self._last_live.enemies if self._last_live else ()

        threat: MatchupThreatProfile | None = None
        dyn_recs: list[DynamicItemRec] = []
        next_plan: NextBuyPlan | None = None
        if hid and self.store.is_loaded:
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
            next_plan = build_next_buy_plan(
                store=self.store,
                hero_id=hid,
                my_items=my_items,
                my_gold=my_gold,
                dynamic_recs=dyn_recs,
                threat=threat,
                game_time=self._last_live.game_time if self._last_live else 0.0,
                selected_augments=self.sm.selected_augments,
            )
            if next_plan is not None:
                prev_lock, prev_plan = self.sm.locked_next_item, list(self.sm.committed_plan)
                new_lock, new_plan = reconcile_match_build_plan(
                    store=self.store,
                    my_items=my_items,
                    committed_plan=self.sm.committed_plan,
                    locked_next_item=self.sm.locked_next_item,
                    proposed_ids=list(next_plan.remaining_build_ids),
                    allow_overhaul=self.sm.allow_plan_overhaul and not self._is_any_coach_online(),
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
        self.edge_drawer.set_committed_plan(
            self.sm.committed_plan,
            self.sm.locked_next_item,
            self.sm.plan_overhaul_reason,
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
        self._refresh_combo_plan_view()

    def _refresh_combo_plan_view(self) -> ComboPlan:
        hid = self._active_hero_id()
        my_items = self._last_live.my_player.item_ids if (self._last_live and self._last_live.my_player) else ()
        plan = build_combo_plan(
            store=self.store,
            hero_id=hid,
            selected_augments=self.sm.selected_augments,
            my_items=my_items,
            limit=4,
        )
        self._last_combo_plan = plan
        self.combo_overlay.set_plan(plan)
        return plan

    def trigger_coach_request(self, reason: str = "manual_f6") -> CoachRequestPayload | None:
        """Build full 10-player + augment match snapshot and publish to cloud/Gemini/bridge coach."""
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

        # Cancel superseded pending debounce/deferral timers on explicit/newer triggers
        if reason in ("manual_f6", "augment_selected", "augment_picking"):
            self._cancel_death_coach_defer(f"superseded by {reason}")
        if reason in ("manual_f6", "augment_selected"):
            self._cancel_picker_coach_debounce()

        # Smart local picker gate: skip automatic augment_picking cloud call when local 0ms winner is decisive
        if reason == "augment_picking" and self.cfg.smart_picker_ai_gate and self.sm.active_cards:
            should_skip, skip_detail = self._should_skip_picker_ai_call(self.sm.active_cards)
            if should_skip:
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

        combo_plan = build_combo_plan(
            store=self.store,
            hero_id=hid,
            selected_augments=self.sm.selected_augments,
            my_items=my_items,
            limit=4,
        )
        self._last_combo_plan = combo_plan
        picker_verdict = (
            self._last_combo_verdict
            or (
                evaluate_picker_combos(
                    store=self.store,
                    hero_id=hid,
                    selected_augments=self.sm.selected_augments,
                    my_items=my_items,
                    offered=[(c.slot_index, c.augment) for c in self.sm.active_cards],
                )
                if self.sm.active_cards
                else None
            )
            if self.sm.state == GameState.AUGMENT_PICKING
            else None
        )

        my_hero_dict: dict[str, Any] = {
            "hero_id": hid or "",
            "name": hero.name if hero else "未锁定",
            "win_rate": round(hero.win_rate, 4) if hero else 0.5,
            "gold": int(my_gold),
            "is_dead": bool(self._last_live and self._last_live.my_player and self._last_live.my_player.is_dead),
            "items": [self.store.get_item_info(i).name for i in my_items],
        }
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
            "overhaul_reason": self.sm.plan_overhaul_reason,
        }
        if self._last_next_plan and self._last_next_plan.recommended_boots_name:
            committed_plan_payload["recommended_boots"] = (
                f"{self._last_next_plan.recommended_boots_name}({self._last_next_plan.recommended_boots_reason})"
            )

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
            threat_summary=list(self._last_threat.summary_tags) if self._last_threat else [],
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
            ),
            hero_signature_hint=(
                sig.to_coach_dict(self.store)
                if (hid and (sig := get_hero_signature_db().get(hid, hero.name if hero else "")) is not None)
                else {}
            ),
            augment_combo_context=export_combo_coach_context(combo_plan, picker_verdict),
        )
        self._latest_request_id = req.request_id
        self._latest_request_trigger = reason
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
                self.edge_drawer.set_coach_pending(True, pending_label=f"⏳ 云端 {tier_lbl} 推演中…")
                self.cloud_coach.submit(req, self.store, tier=tier)
                dispatched = True
        else:
            if self.cfg.bridge_enabled and self.bridge is not None:
                self.bridge.publish_request(req)
                if self.bridge.is_coach_online():
                    dispatched = True
            if self._is_direct_gemini_active() and self.gemini_coach is not None:
                eff_lvl = resolve_thinking_level(
                    self.cfg.gemini_thinking_level,
                    req.trigger_reason,
                    adaptive=self.cfg.gemini_adaptive_thinking,
                    model=self.cfg.gemini_model,
                )
                short_m = self.cfg.gemini_model.replace("gemini-", "G")
                self.edge_drawer.set_coach_pending(True, pending_label=f"⏳ {short_m} ({eff_lvl}) 推演中…")
                self.gemini_coach.submit(
                    req,
                    self.store,
                    api_key=self.cfg.resolved_gemini_api_key(),
                    model=self.cfg.gemini_model,
                    thinking_level=self.cfg.gemini_thinking_level,
                    adaptive_thinking=self.cfg.gemini_adaptive_thinking,
                    proxy_url=self.cfg.proxy_url,
                )
                dispatched = True
            elif self.cfg.coach_mode == "gemini":
                self.edge_drawer.set_coach_error("请先在「AI 参谋」页填入 Gemini API Key")
            elif self._is_any_coach_online():
                self.edge_drawer.set_coach_pending(True)
        if dispatched:
            self.ai_calls_this_match += 1
        if self.sm.state == GameState.AUGMENT_PICKING and self.sm.active_cards and not self._overlays_hidden_by_user:
            self._render_augment_badges(self.sm.active_cards)
        return req

    def _refresh_diagnostics(self) -> None:
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
            lines = [
                f"状态机阶段：{self.sm.state.value}",
                f"当前数据集：{format_display_patch(self.store.descriptor.patch) if self.store.descriptor else '未加载'} / {mode_tag}",
                f"缓存目录：{self.data_dir}",
                f"LCU 连接：{'已连接 port=' + str(self.lcu.creds.port) + ' (' + self.lcu.creds.source + ')' if self.lcu.creds else '未连接 (' + (self.lcu.last_error or '等待客户端') + ')'}",
                f"2999 局内接口：{'活跃' if self._last_live else '未在对局中'}",
                f"截屏后端：{self.capturer.backend}（连续黑帧={self.capturer.consecutive_black_frames}）",
                f"WinRT 中文 OCR：{'就绪 (zh-Hans-CN)' if self.ocr_engine else '不可用'}",
                f"云端 AI 参谋：{cloud_state}",
                f"全局热键：{format_hotkeys_summary(self.cfg.hotkeys)}",
            ]
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
        lines = [
            f"状态机阶段：{self.sm.state.value}",
            f"当前数据集：{format_display_patch(self.store.descriptor.patch) if self.store.descriptor else '未加载'} / {mode_tag}",
            f"缓存目录：{self.data_dir}",
            f"LCU 连接：{'已连接 port=' + str(self.lcu.creds.port) + ' (' + self.lcu.creds.source + ')' if self.lcu.creds else '未连接 (' + (self.lcu.last_error or '等待客户端') + ')'}",
            f"2999 局内接口：{'活跃' if self._last_live else '未在对局中'}",
            f"截屏后端：{self.capturer.backend}（连续黑帧={self.capturer.consecutive_black_frames}）",
            f"WinRT 中文 OCR：{'就绪 (zh-Hans-CN)' if self.ocr_engine else '不可用'}",
            f"AI 参谋模式：{self.cfg.coach_mode} | Gemini API：{gemini_state}",
            f"外部参谋桥接（可选）：http://127.0.0.1:{self.bridge.bound_port if self.bridge else 18421}（{'在线' if (self.bridge and self.bridge.is_coach_online()) else '待接入'}）",
            f"全局热键：{format_hotkeys_summary(self.cfg.hotkeys)}",
        ]
        if self._hotkey_os_occupied:
            lines.append(f"热键冲突警告：{'；'.join(self._hotkey_os_occupied.values())}")
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
        self._vision_timer.stop()
        self._click_timer.stop()
        self._picker_coach_debounce_timer.stop()
        self._death_coach_defer_timer.stop()
        self._icon_verify_timer.stop()
        self.hotkeys.unregister_all()
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
        self.capturer.close()
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
        self.tray.hide()
        self.qt_app.quit()
