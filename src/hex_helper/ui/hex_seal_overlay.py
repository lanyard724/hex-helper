"""Weathered Hextech Seal overlay, 6-state lifecycle engine, 720ms shatter choreography, and zero-disgust preview dialog.

All visuals strictly adhere to the LoL V16.23 Hextech desktop palette (deep navy, antique/weathered bronze,
hextech cyan, cream gold) with zero external images, zero CPU blur effects, and strict zero-focus stealing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import math
import random
import time
from typing import Any, Callable

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QEvent,
    QObject,
    QPointF,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from hex_helper.ui.account_dialogs import estimate_remaining_matches, format_balance_match_friendly
from hex_helper.ui.hextech_widgets import HextechActionButton, HextechModeTab, HextechPulseFrame
from hex_helper.ui.theme import (
    BASE_OVERLAY_QSS,
    CYAN_DEEP,
    CYAN_GLOW,
    CYAN_MID,
    GOLD_CREAM,
    GOLD_METAL,
    GOLD_PRIMARY,
    GREEN_ONLINE,
    SEAL_BRONZE_DARK,
    SEAL_BRONZE_DEEP,
    SEAL_BRONZE_MID,
    SEAL_BRONZE_WORN,
    SEAL_CORE_EXTINGUISHED,
    SEAL_CORE_RIM,
    SEAL_CRACK_DARK,
    SEAL_CYAN_BRIGHT,
    SEAL_CYAN_PEAK,
    SEAL_GOLD_FLASH,
    SEAL_GOLD_IGNITE,
    SEAL_PATINA_MIST,
    TEXT_MUTED,
    body_font,
    display_font,
    latin_serif_font,
    load_app_icon,
)


class CoreEnergyState(str, Enum):
    """Unified 6-stage commercial AI core energy lifecycle (+ system gates)."""

    SYSTEM_GATE = "system_gate"              # 503 / 426 / 403 hard system gate
    DISABLED_BY_USER = "disabled_by_user"    # coach_mode == "off"
    SYNCING = "syncing"                      # logged in, awaiting initial /v1/auth/me
    GUEST_UNCLAIMED = "guest_unclaimed"      # S0: unauthenticated, 450 bonus waiting to be claimed (NO rust lock!)
    TRIAL_ACTIVE = "trial_active"            # S1a: logged in, never paid, active trial credits
    TRIAL_LOW = "trial_low"                  # S1b: trial <= 1 estimated match remaining (amber soft warn)
    CORRODED_TRIAL_END = "corroded_trial"    # S2: trial exhausted (0 credits, never paid) -> Weathered Hextech Seal!
    CORRODED_RETURNING = "corroded_return"   # S2b: returning paid user at 0 credits -> clean dormant recharge slot
    READY_TO_UNSEAL = "ready_to_unseal"      # S3: freshly paid / charged, awaiting manual shatter ritual
    ACTIVE_PAID = "active_paid"              # S4: active paid user
    PAID_LOW = "paid_low"                    # S4-low: active paid user with <= 1 estimated match remaining


class SealMode(str, Enum):
    """Visual rendering state of a HextechSealOverlay."""

    UNLOCKED = "unlocked"                    # Hidden, 0 timers, 0 overhead
    GUEST_CLAIM = "guest_claim"              # Warm cyan-gold unclaimed crystal (zero rust)
    CORRODED = "corroded"                    # Weathered oxidized bronze rune seal with 3 radial fractures
    RETURNING_DORMANT = "returning_dormant"  # Clean low-friction dormant slot for returning payers
    CHARGED_READY = "charged_ready"          # Energy surging through fractures, awaiting click to shatter
    UNSEALING = "unsealing"                  # 720ms (or 380ms fast) 60fps shatter choreography


@dataclass(frozen=True)
class CoreLifecycleSnapshot:
    """Resolved lifecycle snapshot passed to all commercial UI surfaces."""

    state: CoreEnergyState
    username: str = ""
    user_key: str = ""
    tier: str = "auto"
    balance_credits: float | None = None
    est_matches: int = 0
    total_recharged_fen: int = 0
    paid_order_count: int = 0
    total_spent_micro_credits: int = 0
    signup_bonus_credits: int = 450
    snoozed: bool = False
    cutoff_code: str = ""
    cutoff_reason: str = ""

    @property
    def is_sealed_dormant(self) -> bool:
        return self.state in (
            CoreEnergyState.GUEST_UNCLAIMED,
            CoreEnergyState.CORRODED_TRIAL_END,
            CoreEnergyState.CORRODED_RETURNING,
        )

    @property
    def is_ai_usable(self) -> bool:
        return self.state in (
            CoreEnergyState.TRIAL_ACTIVE,
            CoreEnergyState.TRIAL_LOW,
            CoreEnergyState.READY_TO_UNSEAL,
            CoreEnergyState.ACTIVE_PAID,
            CoreEnergyState.PAID_LOW,
        )

    @property
    def is_low_energy(self) -> bool:
        return self.state in (CoreEnergyState.TRIAL_LOW, CoreEnergyState.PAID_LOW)

    @property
    def seal_mode(self) -> SealMode:
        if self.state == CoreEnergyState.GUEST_UNCLAIMED:
            return SealMode.GUEST_CLAIM
        if self.state == CoreEnergyState.CORRODED_TRIAL_END:
            return SealMode.CORRODED
        if self.state == CoreEnergyState.CORRODED_RETURNING:
            return SealMode.RETURNING_DORMANT
        if self.state == CoreEnergyState.READY_TO_UNSEAL:
            return SealMode.CHARGED_READY
        return SealMode.UNLOCKED


def resolve_core_energy_state(
    *,
    is_logged_in: bool,
    username: str | None,
    wallet: dict[str, Any] | None,
    tier: str = "auto",
    coach_mode: str = "cloud",
    cutoff_code: str = "",
    cutoff_reason: str = "",
    unsealed_user_ids: list[str] | tuple[str, ...] = (),
    seal_snooze_until: int = 0,
    force_ready_to_unseal: bool = False,
    local_spent_credits_delta: float = 0.0,
    now_ts: float | None = None,
) -> CoreLifecycleSnapshot:
    """Compute unified commercial AI core lifecycle state with zero backend dependency."""
    now = float(time.time() if now_ts is None else now_ts)
    snoozed = bool(seal_snooze_until and now < float(seal_snooze_until))
    uname = (username or "").strip()
    norm_tier = (tier or "auto").strip().lower()
    if norm_tier not in ("a", "b", "auto"):
        norm_tier = "auto"

    w = wallet if isinstance(wallet, dict) else None
    code = (cutoff_code or "").strip().upper()
    reason = (cutoff_reason or "").strip()

    if code in ("SERVICE_LOCKED", "CLIENT_UPDATE_REQUIRED", "ACCOUNT_DISABLED"):
        bal = float(w.get("balance_credits") or 0.0) if w and w.get("balance_credits") is not None else None
        return CoreLifecycleSnapshot(
            state=CoreEnergyState.SYSTEM_GATE,
            username=uname,
            tier=norm_tier,
            balance_credits=bal,
            est_matches=estimate_remaining_matches(bal or 0.0, norm_tier) if bal is not None else 0,
            snoozed=snoozed,
            cutoff_code=code,
            cutoff_reason=reason,
        )

    if str(coach_mode or "").strip().lower() == "off":
        bal = float(w.get("balance_credits") or 0.0) if w and w.get("balance_credits") is not None else None
        return CoreLifecycleSnapshot(
            state=CoreEnergyState.DISABLED_BY_USER,
            username=uname,
            tier=norm_tier,
            balance_credits=bal,
            est_matches=estimate_remaining_matches(bal or 0.0, norm_tier) if bal is not None else 0,
            snoozed=snoozed,
        )

    if not is_logged_in or not uname:
        return CoreLifecycleSnapshot(
            state=CoreEnergyState.GUEST_UNCLAIMED,
            username="",
            user_key="",
            tier=norm_tier,
            balance_credits=None,
            est_matches=0,
            snoozed=snoozed,
            cutoff_code="NOT_LOGGED_IN",
            cutoff_reason=reason,
        )

    if w is None and not reason:
        return CoreLifecycleSnapshot(
            state=CoreEnergyState.SYNCING,
            username=uname,
            user_key=uname,
            tier=norm_tier,
            balance_credits=None,
            est_matches=0,
            snoozed=snoozed,
        )

    w_safe = w or {}
    if str(w_safe.get("status") or "active").strip().lower() == "banned":
        return CoreLifecycleSnapshot(
            state=CoreEnergyState.SYSTEM_GATE,
            username=uname,
            user_key=str(w_safe.get("id") or w_safe.get("user_id") or uname),
            tier=norm_tier,
            balance_credits=float(w_safe.get("balance_credits") or 0.0),
            est_matches=0,
            snoozed=snoozed,
            cutoff_code="ACCOUNT_DISABLED",
            cutoff_reason=reason or "账号已停用",
        )

    bal = max(0.0, float(w_safe.get("balance_credits") or 0.0))
    est = estimate_remaining_matches(bal, norm_tier)
    recharged_fen = max(0, int(w_safe.get("total_recharged_fen") or 0))
    paid_orders = max(0, int(w_safe.get("paid_order_count") or (1 if recharged_fen > 0 else 0)))
    spent_micro = max(0, int(w_safe.get("total_spent_micro_credits") or 0)) + max(
        0, int(round(max(0.0, float(local_spent_credits_delta or 0.0)) * 1000.0))
    )
    user_key = str(w_safe.get("id") or w_safe.get("user_id") or uname).strip()
    is_paid_history = recharged_fen > 0 or paid_orders > 0
    ai_cutoff = bool(reason or w_safe.get("ai_cutoff", False) or not w_safe.get("ai_enabled", True))

    if ai_cutoff:
        state = CoreEnergyState.CORRODED_RETURNING if is_paid_history else CoreEnergyState.CORRODED_TRIAL_END
        return CoreLifecycleSnapshot(
            state=state,
            username=uname,
            user_key=user_key,
            tier=norm_tier,
            balance_credits=bal,
            est_matches=est,
            total_recharged_fen=recharged_fen,
            paid_order_count=paid_orders,
            total_spent_micro_credits=spent_micro,
            snoozed=snoozed,
            cutoff_code=code or "INSUFFICIENT_CREDITS",
            cutoff_reason=reason or "核心能量已耗尽",
        )

    unsealed_set = {str(x).strip() for x in (unsealed_user_ids or ()) if str(x).strip()}
    already_unsealed = bool(user_key and (user_key in unsealed_set or uname in unsealed_set))

    if force_ready_to_unseal:
        st = CoreEnergyState.READY_TO_UNSEAL
    elif is_paid_history and not already_unsealed and spent_micro <= 0:
        st = CoreEnergyState.READY_TO_UNSEAL
    elif is_paid_history:
        st = CoreEnergyState.PAID_LOW if (0 < est <= 1) else CoreEnergyState.ACTIVE_PAID
    else:
        st = CoreEnergyState.TRIAL_LOW if (0 < est <= 1) else CoreEnergyState.TRIAL_ACTIVE

    return CoreLifecycleSnapshot(
        state=st,
        username=uname,
        user_key=user_key,
        tier=norm_tier,
        balance_credits=bal,
        est_matches=est,
        total_recharged_fen=recharged_fen,
        paid_order_count=paid_orders,
        total_spent_micro_credits=spent_micro,
        snoozed=snoozed,
    )


def build_live_peephole_hint(
    *,
    state: CoreEnergyState,
    my_gold: float = 0.0,
    is_dead: bool = False,
    game_time_s: float = 0.0,
    special_augment_name: str = "",
    threat_tags: list[str] | tuple[str, ...] = (),
    top_threat_name: str = "",
    top_threat_kda: str = "",
    enemy_heal_high: bool = False,
    ally_has_grievous: bool = False,
) -> str:
    """Generate a zero-API-cost factual tactical tease from local 5ms engine signals."""
    verb = "领 450 积分唤醒" if state == CoreEnergyState.GUEST_UNCLAIMED else (
        "补能续接" if state == CoreEnergyState.CORRODED_RETURNING else "充能唤醒"
    )
    gold_i = int(round(max(0.0, float(my_gold or 0.0))))
    aug_txt = (special_augment_name or "").strip()
    if aug_txt:
        return f"检测到质变「{aug_txt[:10]}」· AI 全盘构筑重构待{verb}"

    if is_dead and gold_i >= 1100:
        if enemy_heal_high and not ally_has_grievous:
            return f"手握 {gold_i:,}G · 敌方高回血且缺重伤 · 精准散件买法待{verb}"
        if top_threat_name:
            kda_part = f" {top_threat_kda}" if top_threat_kda else ""
            return f"手握 {gold_i:,}G · 敌方「{top_threat_name[:8]}{kda_part}」领跑 · 反制散件待{verb}"
        return f"手握 {gold_i:,}G · 精准双散件凑单与下波团策略待{verb}"

    clean_tags = [str(t).strip() for t in (threat_tags or ()) if str(t).strip()][:2]
    if game_time_s <= 180.0 and clean_tags:
        joined = " / ".join(clean_tags)
        return f"敌方 {joined} · 本局 10 人开局 5 秒定盘待{verb}"

    if top_threat_name:
        kda_part = f" {top_threat_kda}" if top_threat_kda else ""
        return f"敌方「{top_threat_name[:8]}{kda_part}」领跑 · 实时反制与 F6 急救待{verb}"

    if state == CoreEnergyState.GUEST_UNCLAIMED:
        return "注册即赠 450 积分（约 2 局智能 AI）· 本地 5ms 出装永久免费"
    if state == CoreEnergyState.CORRODED_RETURNING:
        return "本地 5ms 出装持续护航 · 补能即刻恢复 10 人实时反制"
    return "本地 5ms 出装永久免费 · 5 元起充能唤醒 10 人定盘与精准散件"


@dataclass
class _ShardParticle:
    x: float
    y: float
    vx: float
    vy: float
    angle: float
    omega: float
    size: float
    color: QColor
    is_cyan: bool
    life_ms: float
    max_life_ms: float
    sides: int = 4


@dataclass
class _AmbientDust:
    x_norm: float
    y_norm: float
    speed: float
    phase: float
    radius: float
    alpha_max: int


def _ease(curve_type: QEasingCurve.Type, t: float, overshoot: float | None = None) -> float:
    clamped = max(0.0, min(1.0, float(t)))
    c = QEasingCurve(curve_type)
    if overshoot is not None:
        c.setOvershoot(overshoot)
    return float(c.valueForProgress(clamped))


class HextechSealOverlay(QWidget):
    """Non-intrusive pure-vector Weathered Hextech Seal overlay with 720ms 60fps shatter choreography.

    Mounted as a child widget over any target card so it automatically tracks resize & QScrollArea clipping,
    never steals game focus (NoFocus + WA_ShowWithoutActivating), and uses zero external textures or CPU blur.
    """

    seal_clicked = Signal(str, str)    # (seal_id, mode_value)
    unseal_settled = Signal(str)       # Fired at ~560ms (or ~300ms fast) to pulse host card
    unsealed = Signal(str)             # Fired at animation end before hiding

    FULL_DURATION_MS = 720.0
    FAST_DURATION_MS = 380.0
    HOVER_TRANSITION_MS = 180.0
    SHAKE_DURATION_MS = 160.0

    def __init__(
        self,
        target: QWidget,
        seal_id: str,
        title: str = "",
        *,
        subtitle: str = "",
        compact: bool = False,
        allow_idle_ambient: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent or target)
        self.seal_id = str(seal_id or "default")
        self._target = target
        self._title = str(title or "")
        self._subtitle = str(subtitle or "")
        self._cta_text = ""
        self._compact = bool(compact)
        self._allow_idle_ambient = bool(allow_idle_ambient)
        self._game_active = False
        self._snoozed = False
        self._mode = SealMode.UNLOCKED

        # Animation state
        self._hover_t: float = 0.0
        self._hover_target: float = 0.0
        self._shake_ms: float = self.SHAKE_DURATION_MS + 1.0
        self._ambient_ms: float = 0.0
        self._unseal_ms: float = 0.0
        self._unseal_duration_ms: float = self.FULL_DURATION_MS
        self._settled_emitted: bool = False
        self._last_tick_ms: float = 0.0
        self._particles: list[_ShardParticle] = []
        self._static_cache: QPixmap | None = None
        self._static_cache_key: tuple[int, int, int, str, str, str, str] | None = None

        # Deterministic per-seal scratches and ambient dust
        seed_int = int(hashlib.md5(self.seal_id.encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed_int)
        self._scratches: list[tuple[float, float, float, float]] = []
        for _ in range(4):
            sx = rng.uniform(0.10, 0.88)
            sy = rng.uniform(0.16, 0.84)
            ang = rng.uniform(-0.65, 0.65)
            ln = rng.uniform(0.06, 0.14)
            self._scratches.append((sx, sy, sx + math.cos(ang) * ln, sy + math.sin(ang) * ln))
        self._dust: list[_AmbientDust] = [
            _AmbientDust(
                x_norm=rng.uniform(0.08, 0.92),
                y_norm=rng.uniform(0.12, 0.92),
                speed=rng.uniform(0.018, 0.036),
                phase=rng.uniform(0.0, math.tau),
                radius=rng.uniform(0.9, 1.5),
                alpha_max=rng.randint(20, 44),
            )
            for _ in range(6)
        ]

        # Strict zero-focus-stealing attributes for LOL overlays
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._clock = QElapsedTimer()
        self._tick_timer = QTimer(self)
        self._tick_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick_timer.timeout.connect(self._on_tick)

        self._target.installEventFilter(self)
        self.setGeometry(self._target.rect())
        self.hide()

    @property
    def mode(self) -> SealMode:
        return self._mode

    @property
    def unseal_progress_ms(self) -> float:
        return self._unseal_ms

    def set_copy(self, *, title: str | None = None, subtitle: str | None = None, cta_text: str | None = None) -> None:
        changed = False
        if title is not None and title != self._title:
            self._title = title
            changed = True
        if subtitle is not None and subtitle != self._subtitle:
            self._subtitle = subtitle
            changed = True
        if cta_text is not None and cta_text != self._cta_text:
            self._cta_text = cta_text
            changed = True
        if changed:
            self._invalidate_static_cache()
            if self.isVisible():
                self.update()

    def set_snoozed(self, snoozed: bool) -> None:
        val = bool(snoozed)
        if val != self._snoozed:
            self._snoozed = val
            self._sync_timer_policy()
            if self.isVisible():
                self.update()

    def set_game_active(self, active: bool) -> None:
        """Freeze lobby idle dust timer whenever a live LOL game is running."""
        val = bool(active)
        if val != self._game_active:
            self._game_active = val
            self._sync_timer_policy()

    def set_seal_mode(
        self,
        mode: SealMode,
        *,
        subtitle: str | None = None,
        cta_text: str | None = None,
    ) -> None:
        if subtitle is not None:
            self._subtitle = subtitle
        if cta_text is not None:
            self._cta_text = cta_text
        if mode == SealMode.UNLOCKED:
            self._mode = SealMode.UNLOCKED
            self._tick_timer.stop()
            self._particles.clear()
            self._hover_t = 0.0
            self._hover_target = 0.0
            self.hide()
            return
        if self._mode == SealMode.UNSEALING and mode == SealMode.CHARGED_READY:
            return
        self._mode = mode
        self._particles.clear()
        self._invalidate_static_cache()
        self.setGeometry(self._target.rect())
        self.show()
        self.raise_()
        self._sync_timer_policy()
        self.update()

    def play_unseal(self, *, fast: bool = False) -> None:
        """Play the 720ms (or 380ms in-game fast) shatter & radial reveal choreography."""
        if self._mode == SealMode.UNSEALING:
            return
        self._mode = SealMode.UNSEALING
        self._unseal_duration_ms = self.FAST_DURATION_MS if fast else self.FULL_DURATION_MS
        self._unseal_ms = 0.0
        self._settled_emitted = False
        self._particles.clear()
        self._spawn_shards(count=12 if (self._compact or fast) else 16)
        self.setGeometry(self._target.rect())
        self.show()
        self.raise_()
        self._clock.restart()
        self._last_tick_ms = 0.0
        self._tick_timer.setInterval(16)
        self._tick_timer.start()
        self.update()

    def set_unseal_frame_for_test(self, elapsed_ms: float, *, fast: bool = False) -> None:
        """Deterministic frame setter for visual regression screenshots (e.g. 200ms / 400ms)."""
        self._unseal_duration_ms = self.FAST_DURATION_MS if fast else self.FULL_DURATION_MS
        self._mode = SealMode.UNSEALING
        self._tick_timer.stop()
        self._particles.clear()
        self._spawn_shards(count=12 if (self._compact or fast) else 16)
        self._unseal_ms = max(0.0, min(self._unseal_duration_ms, float(elapsed_ms)))
        # Advance shard kinematics deterministically from burst moment (120ms / 60ms)
        burst_start = 60.0 if fast else 120.0
        if self._unseal_ms > burst_start:
            dt_total = (self._unseal_ms - burst_start) / 1000.0
            steps = max(1, int(dt_total / 0.016))
            step_dt = dt_total / steps
            for _ in range(steps):
                self._step_particles(step_dt)
        self.setGeometry(self._target.rect())
        self.show()
        self.raise_()
        self.update()

    def _spawn_shards(self, count: int = 16) -> None:
        w = max(80.0, float(self.width() or self._target.width() or 260))
        h = max(48.0, float(self.height() or self._target.height() or 100))
        cx, cy = self._lock_center(w, h)
        rng = random.Random(int(hashlib.md5((self.seal_id + "_burst").encode()).hexdigest()[:8], 16))
        gold_colors = (QColor(SEAL_GOLD_IGNITE), QColor(SEAL_GOLD_FLASH), QColor(GOLD_METAL))
        cyan_colors = (QColor(SEAL_CYAN_PEAK), QColor(SEAL_CYAN_BRIGHT))
        for i in range(count):
            base_ang = (math.tau * i / count) + rng.uniform(-0.18, 0.18)
            speed = rng.uniform(135.0, 265.0)
            is_cyan = (i % 3 == 0)
            color = rng.choice(cyan_colors if is_cyan else gold_colors)
            self._particles.append(
                _ShardParticle(
                    x=cx + math.cos(base_ang) * rng.uniform(2.0, 7.0),
                    y=cy + math.sin(base_ang) * rng.uniform(2.0, 7.0),
                    vx=math.cos(base_ang) * speed,
                    vy=math.sin(base_ang) * speed - 35.0,
                    angle=rng.uniform(0.0, 360.0),
                    omega=rng.uniform(-420.0, 420.0),
                    size=rng.uniform(2.6, 5.8 if not self._compact else 4.6),
                    color=color,
                    is_cyan=is_cyan,
                    life_ms=0.0,
                    max_life_ms=rng.uniform(300.0, 410.0),
                    sides=3 if (i % 2 == 0) else 4,
                )
            )

    def _step_particles(self, dt: float) -> None:
        if dt <= 0.0:
            return
        drag = math.exp(-2.4 * dt)
        g = 340.0
        for pt in self._particles:
            pt.life_ms += dt * 1000.0
            pt.vx *= drag
            pt.vy = pt.vy * drag + g * dt
            pt.x += pt.vx * dt
            pt.y += pt.vy * dt
            pt.angle += pt.omega * dt

    def _invalidate_static_cache(self) -> None:
        self._static_cache = None
        self._static_cache_key = None

    def _sync_timer_policy(self) -> None:
        if not self.isVisible() or self._mode == SealMode.UNLOCKED:
            self._tick_timer.stop()
            return
        if self._mode == SealMode.UNSEALING:
            self._tick_timer.setInterval(16)
            if not self._tick_timer.isActive():
                self._clock.restart()
                self._last_tick_ms = 0.0
                self._tick_timer.start()
            return
        hover_animating = abs(self._hover_t - self._hover_target) > 0.005 or self._shake_ms < self.SHAKE_DURATION_MS
        if hover_animating:
            self._tick_timer.setInterval(16)
            if not self._tick_timer.isActive():
                self._clock.restart()
                self._last_tick_ms = 0.0
                self._tick_timer.start()
            return
        if self._mode == SealMode.CHARGED_READY and not self._snoozed:
            self._tick_timer.setInterval(50 if self._compact else 33)
            if not self._tick_timer.isActive():
                self._clock.restart()
                self._last_tick_ms = 0.0
                self._tick_timer.start()
            return
        if (
            self._allow_idle_ambient
            and not self._compact
            and not self._game_active
            and not self._snoozed
            and self._mode in (SealMode.CORRODED, SealMode.GUEST_CLAIM)
        ):
            self._tick_timer.setInterval(66)  # 15fps ultra-lightweight lobby ambient
            if not self._tick_timer.isActive():
                self._clock.restart()
                self._last_tick_ms = 0.0
                self._tick_timer.start()
            return
        self._tick_timer.stop()

    def _on_tick(self) -> None:
        now_ms = float(self._clock.elapsed())
        dt_ms = max(1.0, min(64.0, now_ms - self._last_tick_ms))
        self._last_tick_ms = now_ms
        dt_s = dt_ms / 1000.0

        if self._mode == SealMode.UNSEALING:
            self._unseal_ms += dt_ms
            fast = self._unseal_duration_ms <= self.FAST_DURATION_MS + 1.0
            burst_start = 60.0 if fast else 120.0
            settle_at = 285.0 if fast else 560.0
            if self._unseal_ms > burst_start:
                self._step_particles(dt_s)
            if not self._settled_emitted and self._unseal_ms >= settle_at:
                self._settled_emitted = True
                self.unseal_settled.emit(self.seal_id)
            if self._unseal_ms >= self._unseal_duration_ms:
                self._tick_timer.stop()
                self._particles.clear()
                self._mode = SealMode.UNLOCKED
                self.hide()
                self.unsealed.emit(self.seal_id)
                return
            self.update()
            return

        self._ambient_ms += dt_ms
        if self._shake_ms < self.SHAKE_DURATION_MS:
            self._shake_ms += dt_ms
        if abs(self._hover_t - self._hover_target) > 0.004:
            step = dt_ms / self.HOVER_TRANSITION_MS
            if self._hover_target > self._hover_t:
                self._hover_t = min(1.0, self._hover_t + step)
            else:
                self._hover_t = max(0.0, self._hover_t - step)
        else:
            self._hover_t = self._hover_target
            if self._shake_ms >= self.SHAKE_DURATION_MS:
                self._sync_timer_policy()
        self.update()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._target and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
            QEvent.Type.LayoutRequest,
        ):
            r = self._target.rect()
            if self.geometry() != r:
                self.setGeometry(r)
                self._invalidate_static_cache()
            if self.isVisible():
                self.raise_()
        return super().eventFilter(watched, event)

    def enterEvent(self, event) -> None:  # noqa: N802
        if self._mode not in (SealMode.UNLOCKED, SealMode.UNSEALING):
            self._hover_target = 1.0
            self._shake_ms = 0.0
            self._sync_timer_policy()
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._mode not in (SealMode.UNLOCKED, SealMode.UNSEALING):
            self._hover_target = 0.0
            self._sync_timer_policy()
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._mode not in (
            SealMode.UNLOCKED,
            SealMode.UNSEALING,
        ):
            event.accept()
            if self._mode == SealMode.CHARGED_READY:
                self.play_unseal(fast=False)
                self.seal_clicked.emit(self.seal_id, SealMode.CHARGED_READY.value)
            else:
                self.seal_clicked.emit(self.seal_id, self._mode.value)
            return
        super().mousePressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._sync_timer_policy()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._tick_timer.stop()
        super().hideEvent(event)

    def _lock_center(self, w: float, h: float) -> tuple[float, float]:
        if self._compact:
            if h <= 72.0:
                return 26.0, h * 0.5
            return w * 0.5, min(h * 0.36, 30.0)
        if h <= 96.0:
            return w * 0.5, 25.0
        return w * 0.5, h * 0.38

    def _ensure_static_cache(self, w: int, h: int) -> QPixmap:
        dpr = max(1, int(round(float(self.devicePixelRatioF() or 1.0))))
        key = (w, h, dpr, self._mode.value, self._title, self._subtitle, self._cta_text)
        if self._static_cache is not None and self._static_cache_key == key:
            return self._static_cache
        pm = QPixmap(max(1, w * dpr), max(1, h * dpr))
        pm.setDevicePixelRatio(float(dpr))
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._paint_seal_frame(p, float(w), float(h), hover_eased=0.0, breath=0.0, shake_dx=0.0)
        p.end()
        self._static_cache = pm
        self._static_cache_key = key
        return pm

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._mode == SealMode.UNLOCKED or self.width() <= 4 or self.height() <= 4:
            return
        w, h = float(self.width()), float(self.height())
        if self._mode == SealMode.UNSEALING:
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            self._paint_unseal_choreography(p, w, h)
            p.end()
            return

        # Fast path: static DPR QPixmap cache when completely idle (0 FPS in-game drawer)
        is_completely_idle = (
            not self._tick_timer.isActive()
            and self._hover_t <= 0.004
            and self._shake_ms >= self.SHAKE_DURATION_MS
            and self._mode in (SealMode.CORRODED, SealMode.GUEST_CLAIM, SealMode.RETURNING_DORMANT)
        )
        if is_completely_idle:
            pm = self._ensure_static_cache(self.width(), self.height())
            p = QPainter(self)
            p.drawPixmap(0, 0, pm)
            p.end()
            return

        hover_eased = _ease(QEasingCurve.Type.OutCubic, self._hover_t)
        period = 1800.0 if self._mode == SealMode.CHARGED_READY else 3600.0
        breath = 0.5 - 0.5 * math.cos(math.tau * (self._ambient_ms % period) / period)
        if self._snoozed and self._mode != SealMode.CHARGED_READY:
            breath = 0.0
        shake_dx = 0.0
        if self._shake_ms < self.SHAKE_DURATION_MS:
            st = self._shake_ms / self.SHAKE_DURATION_MS
            shake_dx = math.sin(st * 7.0 * math.pi) * 1.3 * (1.0 - st)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._paint_seal_frame(p, w, h, hover_eased=hover_eased, breath=breath, shake_dx=shake_dx)
        p.end()

    def _paint_veil_and_ornaments(
        self,
        p: QPainter,
        w: float,
        h: float,
        *,
        mode: SealMode,
        hover_eased: float,
        breath: float,
        cx: float,
        cy: float,
        clip_path: QPainterPath | None = None,
    ) -> None:
        p.save()
        if clip_path is not None:
            p.setClipPath(clip_path)

        # Translucent obsidian-navy veil letting ~18-23% real underlying card skeleton peek through
        veil = QLinearGradient(0.0, 0.0, 0.0, h)
        if mode == SealMode.GUEST_CLAIM:
            veil.setColorAt(0.0, QColor(4, 22, 34, 184))
            veil.setColorAt(0.55, QColor(6, 18, 34, 198))
            veil.setColorAt(1.0, QColor(2, 12, 22, 212))
        elif mode == SealMode.CHARGED_READY:
            veil.setColorAt(0.0, QColor(4, 24, 36, 188))
            veil.setColorAt(0.5, QColor(7, 20, 38, 204))
            veil.setColorAt(1.0, QColor(2, 12, 24, 218))
        elif mode == SealMode.RETURNING_DORMANT:
            veil.setColorAt(0.0, QColor(5, 16, 28, 192))
            veil.setColorAt(1.0, QColor(2, 10, 19, 214))
        else:
            veil.setColorAt(0.0, QColor(4, 14, 26, 202))
            veil.setColorAt(0.55, QColor(9, 20, 40, 216))
            veil.setColorAt(1.0, QColor(1, 10, 19, 228))
        p.fillRect(QRectF(0.0, 0.0, w, h), QBrush(veil))

        # Subtle patina / energy radial spotlight behind seal core
        spot_r = max(44.0, min(w, h) * 0.65)
        spot = QRadialGradient(cx, cy, spot_r)
        if mode == SealMode.CHARGED_READY:
            spot.setColorAt(0.0, QColor(10, 200, 185, int(68 + 42 * breath)))
            spot.setColorAt(0.65, QColor(0, 90, 130, int(26 + 18 * breath)))
            spot.setColorAt(1.0, QColor(0, 0, 0, 0))
        elif mode == SealMode.GUEST_CLAIM:
            spot.setColorAt(0.0, QColor(10, 200, 185, int(34 + 26 * breath + 28 * hover_eased)))
            spot.setColorAt(0.7, QColor(200, 170, 110, int(14 + 12 * hover_eased)))
            spot.setColorAt(1.0, QColor(0, 0, 0, 0))
        else:
            whisper = int(8 + 22 * breath + 44 * hover_eased)
            spot.setColorAt(0.0, QColor(10, 200, 185, whisper))
            spot.setColorAt(0.55, QColor(44, 62, 66, 28))
            spot.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(0.0, 0.0, w, h), QBrush(spot))

        # Deterministic weathered metallic scratches (corroded mode only)
        if mode == SealMode.CORRODED:
            p.setPen(QPen(QColor(91, 90, 86, 52), 0.85))
            for x1, y1, x2, y2 in self._scratches:
                p.drawLine(QPointF(x1 * w, y1 * h), QPointF(x2 * w, y2 * h))

        # Horizontal symmetric runic energy channels (skip in ultra-narrow compact strip)
        if not (self._compact and h <= 72.0):
            chan_y = cy
            lock_r = 16.0 if (self._compact or h <= 96.0) else 23.0
            margin = 10.0
            if cx - lock_r - 8.0 > margin:
                left_end = cx - lock_r - 4.0
                right_start = cx + lock_r + 4.0
                # Recessed bronze groove
                p.setPen(QPen(QColor(SEAL_BRONZE_DEEP), 1.5))
                p.drawLine(QPointF(margin, chan_y), QPointF(left_end, chan_y))
                p.drawLine(QPointF(right_start, chan_y), QPointF(w - margin, chan_y))
                p.setPen(QPen(QColor(120, 90, 40, 52), 0.9))
                p.drawLine(QPointF(margin, chan_y + 1.1), QPointF(left_end, chan_y + 1.1))
                p.drawLine(QPointF(right_start, chan_y + 1.1), QPointF(w - margin, chan_y + 1.1))

                # Energy fill along channels
                fill_ratio = (
                    1.0
                    if mode == SealMode.CHARGED_READY
                    else (0.42 + 0.35 * hover_eased if mode == SealMode.GUEST_CLAIM else 0.35 * hover_eased)
                )
                if fill_ratio > 0.02:
                    span_l = (left_end - margin) * fill_ratio
                    span_r = (w - margin - right_start) * fill_ratio
                    cyan_a = int(170 + 75 * breath) if mode == SealMode.CHARGED_READY else int(130 * fill_ratio + 60 * hover_eased)
                    p.setPen(QPen(QColor(10, 200, 185, min(255, cyan_a)), 1.4))
                    p.drawLine(QPointF(left_end - span_l, chan_y), QPointF(left_end, chan_y))
                    p.drawLine(QPointF(right_start, chan_y), QPointF(right_start + span_r, chan_y))

        # Outer border & 45-degree Art Deco corner brackets + diamond rivets
        if mode == SealMode.CHARGED_READY:
            border_c = QColor(SEAL_GOLD_IGNITE)
            bracket_c = QColor(SEAL_GOLD_FLASH)
        elif mode == SealMode.GUEST_CLAIM:
            border_c = QColor(CYAN_MID if hover_eased < 0.3 else CYAN_GLOW)
            bracket_c = QColor(GOLD_PRIMARY)
        else:
            border_c = QColor(SEAL_BRONZE_WORN if hover_eased > 0.25 else SEAL_BRONZE_DARK)
            bracket_c = QColor(SEAL_GOLD_IGNITE if hover_eased > 0.5 else SEAL_BRONZE_WORN)

        p.setPen(QPen(border_c, 1.15))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(0.6, 0.6, w - 1.2, h - 1.2))

        arm = 9.0 if self._compact else 13.0
        off = 3.0
        p.setPen(QPen(bracket_c, 1.25))
        # Top-left, Top-right, Bottom-left, Bottom-right brackets
        corners = (
            (off, off, 1.0, 1.0),
            (w - off, off, -1.0, 1.0),
            (off, h - off, 1.0, -1.0),
            (w - off, h - off, -1.0, -1.0),
        )
        for bx, by, sx, sy in corners:
            p.drawLine(QPointF(bx, by + sy * arm), QPointF(bx, by))
            p.drawLine(QPointF(bx, by), QPointF(bx + sx * arm, by))
            # Tiny diamond rivet
            rx, ry = bx + sx * 3.5, by + sy * 3.5
            rivet = QPolygonF(
                [
                    QPointF(rx, ry - 1.7),
                    QPointF(rx + 1.7, ry),
                    QPointF(rx, ry + 1.7),
                    QPointF(rx - 1.7, ry),
                ]
            )
            p.setBrush(QBrush(bracket_c))
            p.drawPolygon(rivet)
            p.setBrush(Qt.BrushStyle.NoBrush)

        # Ambient floating bronze dust motes (lobby only, 0 in-game)
        if self._allow_idle_ambient and not self._compact and not self._snoozed and not self._game_active:
            t_s = self._ambient_ms / 1000.0
            p.setPen(Qt.PenStyle.NoPen)
            for mote in self._dust:
                ny = (mote.y_norm - t_s * mote.speed) % 1.0
                nx = mote.x_norm + math.sin(t_s * 1.1 + mote.phase) * 0.014
                fade = math.sin(ny * math.pi)
                alpha = int(mote.alpha_max * max(0.0, fade))
                if alpha <= 2:
                    continue
                mote_c = QColor(SEAL_CYAN_PEAK if mode == SealMode.CHARGED_READY else SEAL_GOLD_IGNITE)
                mote_c.setAlpha(alpha)
                p.setBrush(QBrush(mote_c))
                p.drawEllipse(QPointF(nx * w, ny * h), mote.radius, mote.radius)

        p.restore()

    def _paint_rune_lock(
        self,
        p: QPainter,
        cx: float,
        cy: float,
        *,
        mode: SealMode,
        hover_eased: float,
        breath: float,
        scale: float = 1.0,
        inner_rot_deg: float = 0.0,
        outer_rot_deg: float = 0.0,
        split_px: float = 0.0,
        crack_glow: float = 0.0,
        overexpose: float = 0.0,
        alpha_mul: float = 1.0,
    ) -> None:
        if alpha_mul <= 0.01:
            return
        small_badge = self._compact or (self.height() > 0 and self.height() <= 96)
        r_outer = 14.5 if small_badge else 21.0
        r_inner = 10.0 if small_badge else 14.5
        cry_w = 5.2 if small_badge else 7.6
        cry_h = 9.0 if small_badge else 13.0

        p.save()
        p.setOpacity(max(0.0, min(1.0, alpha_mul)))
        p.translate(cx, cy)
        p.scale(scale, scale)

        # Pulsing diamond energy wave in CHARGED_READY mode
        if mode == SealMode.CHARGED_READY and split_px <= 0.1:
            wave_r = r_outer * (1.0 + 0.42 * breath)
            wave_alpha = int(155 * (1.0 - breath))
            if wave_alpha > 4:
                p.setPen(QPen(QColor(10, 200, 185, wave_alpha), 1.3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                oct_wave = self._regular_octagon(wave_r)
                p.drawPolygon(oct_wave)

        # 1. Outer forged bronze split dual-arc ring (splits outward on shatter!)
        outer_pen_c = QColor(
            SEAL_GOLD_IGNITE
            if (mode == SealMode.CHARGED_READY or hover_eased > 0.55)
            else (CYAN_MID if mode == SealMode.GUEST_CLAIM else SEAL_BRONZE_WORN)
        )
        outer_dark_c = QColor(SEAL_BRONZE_DARK if mode != SealMode.GUEST_CLAIM else CYAN_DEEP)
        arc_w = 2.4 if self._compact else 3.1

        # Left arc
        p.save()
        p.translate(-split_px, -split_px * 0.28)
        p.rotate(-outer_rot_deg - split_px * 1.15)
        p.setPen(QPen(outer_dark_c, arc_w + 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        arc_rect = QRectF(-r_outer, -r_outer, r_outer * 2.0, r_outer * 2.0)
        p.drawArc(arc_rect, 108 * 16, 144 * 16)
        p.setPen(QPen(outer_pen_c, arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_rect, 108 * 16, 144 * 16)
        # Radial notches along left arc
        p.setPen(QPen(QColor(SEAL_BRONZE_MID if mode == SealMode.CORRODED else GOLD_PRIMARY), 1.1))
        for deg in (128, 160, 200, 232):
            rad = math.radians(deg)
            p.drawLine(
                QPointF(math.cos(rad) * (r_outer - 2.2), -math.sin(rad) * (r_outer - 2.2)),
                QPointF(math.cos(rad) * (r_outer + 1.8), -math.sin(rad) * (r_outer + 1.8)),
            )
        p.restore()

        # Right arc
        p.save()
        p.translate(split_px, split_px * 0.28)
        p.rotate(-outer_rot_deg + split_px * 1.15)
        p.setPen(QPen(outer_dark_c, arc_w + 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_rect, -72 * 16, 144 * 16)
        p.setPen(QPen(outer_pen_c, arc_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        p.drawArc(arc_rect, -72 * 16, 144 * 16)
        p.setPen(QPen(QColor(SEAL_BRONZE_MID if mode == SealMode.CORRODED else GOLD_PRIMARY), 1.1))
        for deg in (-52, -20, 20, 52):
            rad = math.radians(deg)
            p.drawLine(
                QPointF(math.cos(rad) * (r_outer - 2.2), -math.sin(rad) * (r_outer - 2.2)),
                QPointF(math.cos(rad) * (r_outer + 1.8), -math.sin(rad) * (r_outer + 1.8)),
            )
        p.restore()

        # 2. Inner octagonal ring + top/bottom clasps
        p.save()
        p.rotate(inner_rot_deg)
        inner_c = QColor(
            SEAL_GOLD_FLASH
            if overexpose > 0.3
            else (
                SEAL_GOLD_IGNITE
                if mode == SealMode.CHARGED_READY
                else (CYAN_GLOW if mode == SealMode.GUEST_CLAIM else SEAL_BRONZE_MID)
            )
        )
        p.setPen(QPen(inner_c, 1.35))
        p.setBrush(QBrush(QColor(4, 14, 24, 185)))
        if split_px <= 0.1:
            p.drawPolygon(self._regular_octagon(r_inner))
        # Top and bottom triangular clasps (launch vertically on shatter)
        clasp_y = r_inner + split_px * 0.65
        clasp_h = 3.2 if self._compact else 4.4
        p.setBrush(QBrush(outer_pen_c))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(
            QPolygonF([QPointF(-3.2, -clasp_y - clasp_h), QPointF(3.2, -clasp_y - clasp_h), QPointF(0.0, -clasp_y + 1.0)])
        )
        p.drawPolygon(
            QPolygonF([QPointF(-3.2, clasp_y + clasp_h), QPointF(3.2, clasp_y + clasp_h), QPointF(0.0, clasp_y - 1.0)])
        )
        p.restore()

        # 3. Central faceted Hextech crystal core (splits in two halves on shatter)
        left_half = QPolygonF([QPointF(0.0, -cry_h * 0.5), QPointF(-cry_w * 0.5, 0.0), QPointF(0.0, cry_h * 0.5)])
        right_half = QPolygonF([QPointF(0.0, -cry_h * 0.5), QPointF(cry_w * 0.5, 0.0), QPointF(0.0, cry_h * 0.5)])

        if overexpose > 0.25:
            c_left = QColor("#FFFFFF")
            c_right = QColor(SEAL_CYAN_BRIGHT)
            c_rim = QColor("#FFFFFF")
        elif mode == SealMode.CHARGED_READY:
            c_left = QColor(SEAL_CYAN_BRIGHT)
            c_right = QColor(CYAN_MID)
            c_rim = QColor(SEAL_GOLD_FLASH)
        elif mode == SealMode.GUEST_CLAIM:
            c_left = QColor(CYAN_GLOW)
            c_right = QColor(CYAN_DEEP)
            c_rim = QColor(GOLD_PRIMARY)
        else:
            # Extinguished slate-cyan dead crystal
            c_left = QColor("#253339")
            c_right = QColor("#172126")
            c_rim = QColor(SEAL_CORE_RIM if hover_eased < 0.3 else SEAL_CYAN_PEAK)

        half_off = split_px * 0.42
        p.save()
        p.translate(-half_off, 0.0)
        p.setPen(QPen(c_rim, 0.9))
        p.setBrush(QBrush(c_left))
        p.drawPolygon(left_half)
        p.restore()

        p.save()
        p.translate(half_off, 0.0)
        p.setPen(QPen(c_rim, 0.9))
        p.setBrush(QBrush(c_right))
        p.drawPolygon(right_half)
        p.restore()

        # 4. Three radial weathered fracture cracks (corroded & charged modes; light up cyan on hover/charge)
        if mode in (SealMode.CORRODED, SealMode.CHARGED_READY) and split_px < 4.0:
            crack_scale = 1.0 + 0.35 * overexpose
            crack_paths = (
                [QPointF(0.0, -1.5), QPointF(-5.5 * crack_scale, -4.0), QPointF(-10.5 * crack_scale, -2.5)],
                [QPointF(1.0, 0.5), QPointF(6.0 * crack_scale, 3.2), QPointF(11.5 * crack_scale, 1.2)],
                [QPointF(-0.5, 2.0), QPointF(-2.5 * crack_scale, 6.8), QPointF(2.2 * crack_scale, 11.2 * crack_scale)],
            )
            eff_glow = max(crack_glow, 0.85 if mode == SealMode.CHARGED_READY else hover_eased * 0.92)
            for pts in crack_paths:
                path = QPainterPath(pts[0])
                for pt in pts[1:]:
                    path.lineTo(pt)
                p.setPen(QPen(QColor(SEAL_CRACK_DARK), 1.45, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
                if eff_glow > 0.05:
                    # Outer soft pseudo-glow stroke (zero CPU blur)
                    p.setPen(
                        QPen(
                            QColor(10, 200, 185, int(72 * eff_glow)),
                            3.2 + overexpose * 1.2,
                            Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                        )
                    )
                    p.drawPath(path)
                    inner_crack_c = QColor("#FFFFFF") if overexpose > 0.55 else QColor(10, 200, 185, int(235 * eff_glow))
                    p.setPen(
                        QPen(
                            inner_crack_c,
                            1.3 + overexpose * 0.8,
                            Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                        )
                    )
                    p.drawPath(path)

        p.restore()

    @staticmethod
    def _regular_octagon(radius: float) -> QPolygonF:
        pts = []
        for i in range(8):
            ang = math.radians(22.5 + i * 45.0)
            pts.append(QPointF(math.cos(ang) * radius, math.sin(ang) * radius))
        return QPolygonF(pts)

    def _paint_seal_frame(
        self,
        p: QPainter,
        w: float,
        h: float,
        *,
        hover_eased: float,
        breath: float,
        shake_dx: float,
    ) -> None:
        cx, cy = self._lock_center(w, h)
        self._paint_veil_and_ornaments(
            p,
            w,
            h,
            mode=self._mode,
            hover_eased=hover_eased,
            breath=breath,
            cx=cx,
            cy=cy,
        )
        self._paint_rune_lock(
            p,
            cx + shake_dx,
            cy,
            mode=self._mode,
            hover_eased=hover_eased,
            breath=breath,
            scale=1.0 + 0.055 * hover_eased,
            crack_glow=0.14 * breath if self._mode == SealMode.CORRODED else 0.0,
        )

        # Text typography below or right of the seal emblem
        if self._compact and h <= 72.0:
            # Horizontal compact layout (e.g. 52-60px OpeningBrief / CoachCard strip)
            tx = 48.0
            cta_w = 84.0 if self._cta_text else 0.0
            tw = max(40.0, w - tx - cta_w - 26.0)
            title_c = QColor(
                SEAL_GOLD_FLASH
                if self._mode == SealMode.CHARGED_READY
                else (GOLD_PRIMARY if self._mode == SealMode.GUEST_CLAIM else "#C8C3B8")
            )
            p.setFont(display_font(10, bold=True, letter_spacing=0.3))
            p.setPen(title_c)
            p.drawText(
                QRectF(tx, 6.0, tw, 18.0),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                p.fontMetrics().elidedText(self._title, Qt.TextElideMode.ElideRight, int(tw)),
            )
            if self._subtitle:
                p.setFont(body_font(9, bold=False))
                p.setPen(QColor(CYAN_GLOW if self._mode in (SealMode.GUEST_CLAIM, SealMode.CHARGED_READY) else TEXT_MUTED))
                elided = p.fontMetrics().elidedText(self._subtitle, Qt.TextElideMode.ElideRight, int(tw))
                p.drawText(
                    QRectF(tx, 24.0, tw, max(16.0, h - 28.0)),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    elided,
                )
            if self._cta_text:
                pill_r = QRectF(w - cta_w - 22.0, (h - 22.0) * 0.5, cta_w, 22.0)
                self._draw_cta_pill(p, pill_r, self._cta_text, hover_eased=hover_eased)
        else:
            # Centered vertical layout (Lobby 3-card shelf & expanded cards)
            small_v = self._compact or h <= 96.0
            lock_r = 16.0 if small_v else 23.0
            ty = cy + lock_r + (3.0 if small_v else 7.0)
            pad_x = 10.0
            tw = max(40.0, w - pad_x * 2.0)
            if self._title:
                p.setFont(display_font(10 if small_v else 11, bold=True, letter_spacing=0.4))
                title_c = QColor(
                    SEAL_GOLD_FLASH
                    if (self._mode == SealMode.CHARGED_READY or hover_eased > 0.35)
                    else (GOLD_PRIMARY if self._mode == SealMode.GUEST_CLAIM else "#D6CEBD")
                )
                p.setPen(title_c)
                p.drawText(
                    QRectF(pad_x, ty, tw, 16.0 if small_v else 18.0),
                    Qt.AlignmentFlag.AlignCenter,
                    p.fontMetrics().elidedText(self._title, Qt.TextElideMode.ElideRight, int(tw)),
                )
                ty += 16.0 if small_v else 17.0
            if self._subtitle:
                p.setFont(body_font(9 if small_v else 10, bold=False))
                sub_c = QColor(
                    SEAL_CYAN_BRIGHT
                    if self._mode == SealMode.CHARGED_READY
                    else (CYAN_GLOW if self._mode == SealMode.GUEST_CLAIM else TEXT_MUTED)
                )
                p.setPen(sub_c)
                sub_h = 15.0 if small_v else 18.0
                p.drawText(
                    QRectF(pad_x, ty, tw, sub_h),
                    Qt.AlignmentFlag.AlignCenter,
                    p.fontMetrics().elidedText(self._subtitle, Qt.TextElideMode.ElideRight, int(tw)),
                )
                ty += sub_h + 2.0
            if self._cta_text and (ty + 20.0 <= h - 3.0):
                pill_w = min(tw, max(88.0, float(len(self._cta_text) * 10 + 22)))
                pill_y = min(h - 23.0, ty + 1.0)
                self._draw_cta_pill(
                    p,
                    QRectF((w - pill_w) * 0.5, pill_y, pill_w, 19.0),
                    self._cta_text,
                    hover_eased=hover_eased,
                )

    def _draw_cta_pill(self, p: QPainter, rect: QRectF, text: str, *, hover_eased: float) -> None:
        is_charged = self._mode == SealMode.CHARGED_READY
        is_guest = self._mode == SealMode.GUEST_CLAIM
        bg = QColor(10, 56, 70, 225) if (is_charged or is_guest) else QColor(24, 30, 36, int(195 + 40 * hover_eased))
        bd = QColor(
            SEAL_CYAN_BRIGHT
            if is_charged
            else (CYAN_GLOW if is_guest else (GOLD_PRIMARY if hover_eased > 0.35 else SEAL_BRONZE_WORN))
        )
        fg = QColor("#FFFFFF" if (is_charged or hover_eased > 0.5) else GOLD_CREAM)
        p.setBrush(QBrush(bg))
        p.setPen(QPen(bd, 1.1))
        p.drawRoundedRect(rect, 2.0, 2.0)
        p.setFont(body_font(9, bold=True))
        p.setPen(fg)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _paint_unseal_choreography(self, p: QPainter, w: float, h: float) -> None:
        """4-phase 60fps pure-vector shatter & Odd-Even radial reveal choreography."""
        dur = self._unseal_duration_ms
        ms = max(0.0, min(dur, self._unseal_ms))
        scale_t = dur / self.FULL_DURATION_MS
        t1 = 120.0 * scale_t
        t2 = 280.0 * scale_t
        t3_start = 240.0 * scale_t
        t3_end = 560.0 * scale_t

        cx, cy = self._lock_center(w, h)
        max_r = math.hypot(max(cx, w - cx), max(cy, h - cy)) * 1.08

        # Phase 3 (240/280 - 560ms): Odd-Even radial curtain wipe uncovering real underlying Qt widgets!
        reveal_r = 0.0
        p3 = 0.0
        if ms >= t3_start:
            p3 = max(0.0, min(1.0, (ms - t3_start) / max(1.0, t3_end - t3_start)))
            reveal_r = max_r * _ease(QEasingCurve.Type.OutQuart, p3)

        if ms < t3_end:
            clip_path: QPainterPath | None = None
            if reveal_r > 1.0:
                outer_rect = QPainterPath()
                outer_rect.addRect(QRectF(0.0, 0.0, w, h))
                hole = QPainterPath()
                hole.addEllipse(QPointF(cx, cy), reveal_r, reveal_r)
                clip_path = outer_rect.subtracted(hole)
            self._paint_veil_and_ornaments(
                p,
                w,
                h,
                mode=SealMode.CHARGED_READY,
                hover_eased=1.0,
                breath=1.0,
                cx=cx,
                cy=cy,
                clip_path=clip_path,
            )

        # Phase 1 (0 - 120ms): Compress + torque + overexpose + jitter
        if ms <= t1:
            p1 = ms / max(1.0, t1)
            sc = 1.0 - 0.14 * _ease(QEasingCurve.Type.InBack, p1)
            charge = _ease(QEasingCurve.Type.InCubic, p1)
            jitter = math.sin(p1 * 18.0 * math.pi) * (0.6 + 1.4 * p1)
            # Central overexposure glow
            glow_r = 18.0 + 26.0 * charge
            rg = QRadialGradient(cx, cy, glow_r)
            rg.setColorAt(0.0, QColor(124, 248, 238, int(45 + 175 * charge)))
            rg.setColorAt(0.6, QColor(10, 200, 185, int(25 + 95 * charge)))
            rg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rg))
            p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

            self._paint_rune_lock(
                p,
                cx + jitter,
                cy,
                mode=SealMode.CHARGED_READY,
                hover_eased=1.0,
                breath=1.0,
                scale=sc,
                inner_rot_deg=14.0 * charge,
                outer_rot_deg=8.0 * charge,
                crack_glow=1.0,
                overexpose=charge,
            )

        # Phase 2 (120 - 280ms): Cross star flare + dual-arc shatter launch
        elif ms <= t2 + 40.0 * scale_t:
            p2 = max(0.0, min(1.0, (ms - t1) / max(1.0, t2 - t1)))
            ep2 = _ease(QEasingCurve.Type.OutExpo, p2)
            # 65ms white-cyan cross star flare at burst instant
            flare_dur = 65.0 * scale_t
            if ms - t1 <= flare_dur:
                ft = (ms - t1) / max(1.0, flare_dur)
                fa = int(245 * (1.0 - ft))
                flen = (46.0 if self._compact else 68.0) * (0.65 + 0.55 * ft)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(240, 252, 255, fa)))
                p.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(cx - flen * 0.5, cy),
                            QPointF(cx, cy - 2.4),
                            QPointF(cx + flen * 0.5, cy),
                            QPointF(cx, cy + 2.4),
                        ]
                    )
                )
                p.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(cx, cy - flen * 0.38),
                            QPointF(cx + 2.2, cy),
                            QPointF(cx, cy + flen * 0.38),
                            QPointF(cx - 2.2, cy),
                        ]
                    )
                )
            # Afterglow ring decay
            glow_r = 44.0 + 20.0 * ep2
            rg = QRadialGradient(cx, cy, glow_r)
            rg.setColorAt(0.0, QColor(124, 248, 238, int(205 * (1.0 - p2))))
            rg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(rg))
            p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)

            alpha_lock = max(0.0, 1.0 - p2 / 0.88)
            self._paint_rune_lock(
                p,
                cx,
                cy,
                mode=SealMode.CHARGED_READY,
                hover_eased=1.0,
                breath=1.0,
                scale=0.92 + 0.16 * ep2,
                split_px=24.0 * ep2,
                crack_glow=1.0,
                overexpose=max(0.0, 1.0 - p2 * 1.4),
                alpha_mul=alpha_lock,
            )

        # Phase 3 ripple front (3-layer cyan/gold refraction ring around reveal hole)
        if reveal_r > 2.0 and ms < t3_end + 30.0 * scale_t:
            fade = max(0.0, 1.0 - max(0.0, (ms - t3_start) / max(1.0, t3_end - t3_start)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            # Outer soft refraction band (zero blur)
            p.setPen(QPen(QColor(0, 90, 130, int(115 * fade)), 14.0))
            p.drawEllipse(QPointF(cx, cy), reveal_r + 4.0, reveal_r + 4.0)
            p.setPen(QPen(QColor(10, 200, 185, int(195 * fade)), 5.0))
            p.drawEllipse(QPointF(cx, cy), reveal_r + 1.5, reveal_r + 1.5)
            # Razor-sharp electric cyan arc
            p.setPen(QPen(QColor(124, 248, 238, int(245 * fade)), 1.8))
            p.drawEllipse(QPointF(cx, cy), reveal_r, reveal_r)
            # Lagging inner gold arc
            if reveal_r > 10.0:
                p.setPen(QPen(QColor(200, 170, 110, int(165 * fade)), 1.2))
                p.drawEllipse(QPointF(cx, cy), reveal_r - 7.0, reveal_r - 7.0)

        # Flying faceted crystal shards (active from t1 until max_life_ms)
        if ms > t1 and self._particles:
            p.setPen(Qt.PenStyle.NoPen)
            for pt in self._particles:
                if pt.life_ms >= pt.max_life_ms:
                    continue
                lr = pt.life_ms / max(1.0, pt.max_life_ms)
                a_mul = 1.0 if lr < 0.55 else max(0.0, 1.0 - _ease(QEasingCurve.Type.OutQuad, (lr - 0.55) / 0.45))
                if a_mul <= 0.02:
                    continue
                p.save()
                p.translate(pt.x, pt.y)
                p.rotate(pt.angle)
                if pt.is_cyan:
                    halo = QColor(pt.color.red(), pt.color.green(), pt.color.blue(), int(72 * a_mul))
                    p.setBrush(QBrush(halo))
                    p.drawEllipse(QPointF(0.0, 0.0), pt.size * 1.45, pt.size * 1.45)
                body_c = QColor(pt.color.red(), pt.color.green(), pt.color.blue(), int(245 * a_mul))
                p.setBrush(QBrush(body_c))
                s = pt.size
                if pt.sides == 3:
                    poly = QPolygonF([QPointF(0.0, -s), QPointF(s * 0.85, s * 0.7), QPointF(-s * 0.85, s * 0.7)])
                else:
                    poly = QPolygonF([QPointF(0.0, -s), QPointF(s * 0.65, 0.0), QPointF(0.0, s), QPointF(-s * 0.65, 0.0)])
                p.drawPolygon(poly)
                p.restore()

        # Phase 4 (560 - 720ms): Gilded border lock-in bounce + 45° diagonal sheen sweep
        if ms >= t3_end - 25.0 * scale_t:
            p4 = max(0.0, min(1.0, (ms - (t3_end - 25.0 * scale_t)) / max(1.0, dur - (t3_end - 25.0 * scale_t))))
            decay = 1.0 - _ease(QEasingCurve.Type.OutCubic, p4)
            pen_w = 1.2 + 1.5 * decay
            border_c = QColor(240, 230, 210, int(235 * decay)) if p4 < 0.4 else QColor(200, 170, 110, int(235 * decay))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(border_c, pen_w))
            p.drawRect(QRectF(1.0, 1.0, w - 2.0, h - 2.0))

            # 45-degree specular sheen sweep across unlocked card
            band_w = max(36.0, w * 0.24)
            sx = -band_w + (w + h + band_w * 2.0) * _ease(QEasingCurve.Type.OutQuad, p4)
            sheen_path = QPainterPath()
            sheen_path.moveTo(sx, 0.0)
            sheen_path.lineTo(sx + band_w, 0.0)
            sheen_path.lineTo(sx + band_w - h * 0.55, h)
            sheen_path.lineTo(sx - h * 0.55, h)
            sheen_path.closeSubpath()
            sg = QLinearGradient(sx, 0.0, sx + band_w, 0.0)
            sg.setColorAt(0.0, QColor(240, 230, 210, 0))
            sg.setColorAt(0.5, QColor(240, 230, 210, int(74 * decay)))
            sg.setColorAt(1.0, QColor(240, 230, 210, 0))
            p.fillPath(sheen_path, QBrush(sg))


class CoreInsightPreviewDialog(QDialog):
    """3-second zero-disgust comparison buffer dialog shown before personal QR checkout.

    Eliminates immediate payment shock by showing real Free 5ms vs AI 10-player side-by-side slices,
    trust anchors ("local forever free / never expires / ~0.3 CNY/match"), and a respectful 72h snooze exit.
    """

    action_chosen = Signal(str)  # "register" | "recharge" | "snooze"

    _TABS: tuple[tuple[str, str, str, str, str], ...] = (
        (
            "开局 5 秒定盘",
            "本地 5ms 常规胜率路线",
            "• 英雄常规推荐：星蚀 → 黑色切割者 → 死亡之舞\n"
            "• 仅展示全分段平均胜率出装顺序\n"
            "• 进局需自己按 Tab 盘点对面 5 个人伤害结构",
            "云端 AI 10 人实时开局简报",
            "• 【敌方威胁】敌方 3 名长手 AP 消耗 + 蒙多高回血\n"
            "• 【海克斯目标】优先抓突进/魔抗续航，避开纯站撸卡\n"
            "• 【出门对策】直接守护者号角 + 复用药水抗压发育",
        ),
        (
            "质变与焦灼选牌",
            "本地 5ms 胜率与流派提示",
            "• 候选 A「迷人蝴蝶」胜率 53.2% vs 候选 B「秘术冲拳」52.9%\n"
            "• 胜率差仅 0.3%，静态榜单难以定夺\n"
            "• 选到冷门质变海克斯后仍按常规装备推演",
            "云端 AI 破局决断 + 全盘重构",
            "• 结合敌方 3 短手战士与你已选首轮卡，直接拍板选 A\n"
            "• 触发 72 套质变重构：后续路线全盘改走攻速特效流\n"
            "• 严守已出成装连贯性，绝不推翻已花金币",
        ),
        (
            "阵亡精准散件",
            "本地 5ms 下一大件进度",
            "• 下一件核心：莫雷洛秘典（还差 950G）\n"
            "• 阵亡手握 1,650G，需自己心算先补哪两件小件\n"
            "• 不结合敌方最新 KDA 判断是否提前改出保命装",
            "云端 AI 精确到散件与团战站位",
            "• 【回城立刻买】湮灭宝珠 (800G) + 巨人腰带 (900G 差 50G 改红水晶)\n"
            "• 【实时反制】敌方剑魔已 8/2 成型且我方无人出重伤\n"
            "• 【下波团提醒】留控制打断剑魔二段 Q，勿先手交位移",
        ),
    )

    def __init__(
        self,
        *,
        state: CoreEnergyState = CoreEnergyState.CORRODED_TRIAL_END,
        last_ai_highlight: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self.chosen_action: str = ""
        self.setWindowTitle("HexHelper · 海克斯 AI 参谋实战对比")
        self.setWindowIcon(load_app_icon())
        self.setModal(True)
        self.setMinimumSize(580, 438)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        hdr = QLabel("◈ 永久免费本地引擎  vs  云端海克斯 AI 10 人参谋")
        hdr.setFont(display_font(13, bold=True, letter_spacing=0.9))
        hdr.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        root.addWidget(hdr)

        # Optional emotional recall strip for users who already experienced trial AI
        hl = (last_ai_highlight or "").strip()
        if hl:
            recall = QFrame(self)
            recall.setStyleSheet(
                "QFrame { background-color: rgba(46, 36, 16, 200); border: 1px solid #C8AA6E; "
                "border-left: 3px solid #F0E6D2; border-radius: 2px; }"
            )
            rl = QHBoxLayout(recall)
            rl.setContentsMargins(10, 5, 10, 5)
            r_lbl = QLabel(f"✦ 上次体验高光回顾：{hl}")
            r_lbl.setWordWrap(True)
            r_lbl.setStyleSheet("border: none; background: transparent; color: #FDE68A; font-size: 11px; font-weight: bold;")
            rl.addWidget(r_lbl)
            root.addWidget(recall)

        # 3-tab real tactical comparison switcher
        tab_row = QHBoxLayout()
        tab_row.setSpacing(18)
        self._tab_btns: list[HextechModeTab] = []
        for idx, item in enumerate(self._TABS):
            tb = HextechModeTab(item[0], active=(idx == 0))
            tb.setMinimumWidth(max(132, tb.sizeHint().width() + 20))
            tb.clicked.connect(lambda _=False, i=idx: self.select_tab(i))
            self._tab_btns.append(tb)
            tab_row.addWidget(tb)
        tab_row.addStretch(1)
        root.addLayout(tab_row)

        # Dual side-by-side comparison cards
        comp_row = QHBoxLayout()
        comp_row.setSpacing(10)

        self.free_card = QFrame(self)
        self.free_card.setStyleSheet(
            "QFrame { background-color: rgba(8, 22, 32, 220); border: 1px solid rgba(9, 194, 105, 130); "
            "border-top: 2px solid #09C269; border-radius: 2px; }"
        )
        fl = QVBoxLayout(self.free_card)
        fl.setContentsMargins(12, 10, 12, 10)
        fl.setSpacing(6)
        free_badge = QLabel("● 永久免费（永不锁定）")
        free_badge.setStyleSheet("border: none; background: transparent; color: #09C269; font-size: 10px; font-weight: bold;")
        self.free_title_lbl = QLabel("")
        self.free_title_lbl.setFont(display_font(11, bold=True))
        self.free_title_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
        self.free_body_lbl = QLabel("")
        self.free_body_lbl.setWordWrap(True)
        self.free_body_lbl.setStyleSheet("border: none; background: transparent; color: #CBD5E1; font-size: 11px; line-height: 1.45;")
        fl.addWidget(free_badge)
        fl.addWidget(self.free_title_lbl)
        fl.addWidget(self.free_body_lbl, 1)

        self.ai_card = QFrame(self)
        self.ai_card.setStyleSheet(
            "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
            "stop:0 rgba(6, 34, 48, 235), stop:1 rgba(10, 20, 38, 235)); "
            "border: 1px solid #0AC8B9; border-top: 2px solid #C8AA6E; border-radius: 2px; }"
        )
        al = QVBoxLayout(self.ai_card)
        al.setContentsMargins(12, 10, 12, 10)
        al.setSpacing(6)
        ai_badge = QLabel("◈ 云端 AI 参谋（按局透明计费 · 约 3 毛/局）")
        ai_badge.setStyleSheet("border: none; background: transparent; color: #0AC8B9; font-size: 10px; font-weight: bold;")
        self.ai_title_lbl = QLabel("")
        self.ai_title_lbl.setFont(display_font(11, bold=True))
        self.ai_title_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
        self.ai_body_lbl = QLabel("")
        self.ai_body_lbl.setWordWrap(True)
        self.ai_body_lbl.setStyleSheet("border: none; background: transparent; color: #F0E6D2; font-size: 11px; line-height: 1.45;")
        al.addWidget(ai_badge)
        al.addWidget(self.ai_title_lbl)
        al.addWidget(self.ai_body_lbl, 1)

        comp_row.addWidget(self.free_card, 1)
        comp_row.addWidget(self.ai_card, 1)
        root.addLayout(comp_row, 1)

        # Trust 3-anchor reassurance strip
        trust = QLabel(
            "◈ 英雄大数据 / 3-4 卡识别 / 本地 5ms 出装永久免费不锁   |   ◈ 积分永不过期 · 绝无包月自动续费   |   ◈ 5 元一瓶饮料打 15-25 局"
        )
        trust.setWordWrap(True)
        trust.setAlignment(Qt.AlignmentFlag.AlignCenter)
        trust.setStyleSheet(
            "background-color: rgba(9, 20, 40, 225); border: 1px solid #463714; "
            "border-radius: 2px; padding: 7px 10px; color: #C8AA6E; font-size: 11px; font-weight: bold;"
        )
        root.addWidget(trust)

        # Bottom action row with zero-friction snooze exit
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self.snooze_btn = QPushButton("暂不开启，继续使用永久免费版（72h 静默）")
        self.snooze_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.snooze_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #A09B8C; font-size: 11px; padding: 4px 6px; }"
            "QPushButton:hover { color: #F0E6D2; text-decoration: underline; }"
        )
        self.snooze_btn.clicked.connect(self._on_snooze)

        is_guest = state == CoreEnergyState.GUEST_UNCLAIMED
        cta_txt = "免费领 450 积分试跑 2 局（10 秒注册）" if is_guest else "充能唤醒 AI（¥5 起 · 永不过期）"
        self.primary_btn = HextechActionButton(
            cta_txt,
            variant="cyan" if is_guest else "gold",
            pointed_right=True,
            height=34,
        )
        self.primary_btn.clicked.connect(self._on_primary)

        btn_row.addWidget(self.snooze_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.primary_btn)
        root.addLayout(btn_row)

        self.select_tab(0)

    def select_tab(self, index: int) -> None:
        idx = max(0, min(len(self._TABS) - 1, int(index)))
        for i, tb in enumerate(self._tab_btns):
            tb.set_active(i == idx)
        _, ft, fb, at, ab = self._TABS[idx]
        self.free_title_lbl.setText(ft)
        self.free_body_lbl.setText(fb)
        self.ai_title_lbl.setText(at)
        self.ai_body_lbl.setText(ab)

    def _on_primary(self) -> None:
        self.chosen_action = "register" if self._state == CoreEnergyState.GUEST_UNCLAIMED else "recharge"
        self.action_chosen.emit(self.chosen_action)
        self.accept()

    def _on_snooze(self) -> None:
        self.chosen_action = "snooze"
        self.action_chosen.emit("snooze")
        self.reject()


class HextechSealSandboxDialog(QDialog):
    """Interactive Hextech Seal prototype & choreography tester (S0 -> S1 -> S2 -> S3 Domino Shatter -> S4)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("HexHelper · 海克斯枯朽封印与 720ms 破封动效沙盒")
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(940, 585)
        self.setStyleSheet(
            BASE_OVERLAY_QSS
            + """
            QDialog {
                background-color: #040E1A;
                border: 1px solid #C8AA6E;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        hdr_row = QHBoxLayout()
        ttl = QLabel("HEXHELPER · 海克斯封印心理学与 720ms 破封交互沙盒")
        ttl.setFont(display_font(13, bold=True, letter_spacing=1.0))
        ttl.setStyleSheet("color: #F0E6D2; background: transparent; border: none;")
        self.state_badge = QLabel("当前态：S2 体验耗尽 · 枯朽海克斯封印")
        self.state_badge.setStyleSheet(
            "background-color: rgba(9, 20, 40, 230); border: 1px solid #785A28; "
            "border-radius: 2px; padding: 3px 10px; color: #C8AA6E; font-size: 11px; font-weight: bold;"
        )
        hdr_row.addWidget(ttl)
        hdr_row.addStretch(1)
        hdr_row.addWidget(self.state_badge)
        root.addLayout(hdr_row)

        # Lobby 3-card shelf
        shelf_lbl = QLabel("一、大厅主控台 Tab 2 · 三大 AI 核心封印展示架（S3 充能态点击任意一卡触发 90ms 多米诺连锁 720ms 破封）")
        shelf_lbl.setStyleSheet("color: #C8AA6E; font-size: 11px; font-weight: bold; background: transparent; border: none;")
        root.addWidget(shelf_lbl)

        shelf_row = QHBoxLayout()
        shelf_row.setSpacing(10)
        self._lobby_cards: list[HextechPulseFrame] = []
        self._lobby_seals: list[HextechSealOverlay] = []
        specs = (
            ("opening_brief", "开局 10 人 5 秒定盘", "敌方威胁 · 海克斯目标 · 出门对策", "敌方 3AP + 蒙多回血\n优先抗压装 + 抓突进卡"),
            ("augment_decision", "72 质变重构与焦灼选牌", "胜率差 <1.5% 结合 10 人决断", "迷人蝴蝶 53.2% 胜出\n后续出装全盘切换特效流"),
            ("death_counter", "阵亡精确散件 & F6 急救", "按精确金币配双散件 + 团战提醒", "1,650G 先买湮灭宝珠+红水晶\n针对 8/2 剑魔留控断二段 Q"),
        )
        for sid, stitle, ssub, real_txt in specs:
            card = HextechPulseFrame(self)
            card.setFixedHeight(118)
            card.setStyleSheet(
                "QFrame { background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, "
                "stop:0 rgba(6, 32, 46, 235), stop:1 rgba(9, 20, 40, 235)); "
                "border: 1px solid #0AC8B9; border-top: 2px solid #C8AA6E; border-radius: 2px; }"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(10, 8, 10, 8)
            cl.setSpacing(4)
            ct = QLabel(f"◈ {stitle}")
            ct.setFont(display_font(10, bold=True))
            ct.setStyleSheet("border: none; background: transparent; color: #F0E6D2;")
            cb = QLabel(real_txt)
            cb.setWordWrap(True)
            cb.setStyleSheet("border: none; background: transparent; color: #CBD5E1; font-size: 11px;")
            cl.addWidget(ct)
            cl.addWidget(cb, 1)
            seal = HextechSealOverlay(
                card,
                seal_id=sid,
                title=stitle,
                subtitle=ssub,
                compact=False,
                allow_idle_ambient=True,
            )
            seal.unseal_settled.connect(lambda _s, c=card: c.trigger_pulse("#0AC8B9", 420))
            seal.seal_clicked.connect(self._on_sandbox_seal_clicked)
            self._lobby_cards.append(card)
            self._lobby_seals.append(seal)
            shelf_row.addWidget(card, 1)
        root.addLayout(shelf_row)

        # In-game drawer simulation (Local 5ms 100% full brightness + compact sealed AI slots)
        drawer_lbl = QLabel("二、局内右侧抽屉模拟 · 本地 5ms 出装永久 100% 高亮 + 紧凑 AI 封印槽（含零成本门缝实时战局剧透）")
        drawer_lbl.setStyleSheet("color: #C8AA6E; font-size: 11px; font-weight: bold; background: transparent; border: none;")
        root.addWidget(drawer_lbl)

        drawer_sim = QFrame(self)
        drawer_sim.setStyleSheet(
            "QFrame { background-color: rgba(4, 14, 26, 238); border: 1px solid #785A28; "
            "border-top: 2px solid #C8AA6E; border-radius: 2px; }"
        )
        dl = QVBoxLayout(drawer_sim)
        dl.setContentsMargins(10, 8, 10, 8)
        dl.setSpacing(6)

        self.sim_brief_card = HextechPulseFrame(drawer_sim)
        self.sim_brief_card.setFixedHeight(58)
        self.sim_brief_card.setStyleSheet(
            "QFrame { background-color: rgba(6, 28, 42, 235); border: 1px solid #0AC8B9; border-radius: 2px; }"
        )
        sbl = QVBoxLayout(self.sim_brief_card)
        sbl.setContentsMargins(8, 4, 8, 4)
        sbl.addWidget(QLabel("◈ 开局 10 人作战简报 · 敌方 3AP+蒙多回血 · 出门守护者号角抗压"))
        self.sim_brief_seal = HextechSealOverlay(
            self.sim_brief_card,
            seal_id="sim_brief",
            title="开局 10 人作战简报",
            subtitle="敌方多回血 / 高爆发 · 5 秒定盘待唤醒",
            compact=True,
            allow_idle_ambient=False,
        )
        self.sim_brief_seal.unseal_settled.connect(lambda _s: self.sim_brief_card.trigger_pulse("#0AC8B9", 360))
        self.sim_brief_seal.seal_clicked.connect(self._on_sandbox_seal_clicked)
        dl.addWidget(self.sim_brief_card)

        free_strip = QFrame(drawer_sim)
        free_strip.setStyleSheet(
            "QFrame { background-color: rgba(8, 38, 50, 238); border: 1px solid #0AC8B9; "
            "border-left: 3px solid #09C269; border-radius: 2px; }"
        )
        fsl = QHBoxLayout(free_strip)
        fsl.setContentsMargins(10, 6, 10, 6)
        fsl.addWidget(QLabel("● 永久免费本地 5ms 引擎：下一件 ▶ 莫雷洛秘典 (2,200G)  |  当前 1,650G 可买：湮灭宝珠 + 增幅典籍"))
        dl.addWidget(free_strip)

        self.sim_coach_card = HextechPulseFrame(drawer_sim)
        self.sim_coach_card.setFixedHeight(92)
        self.sim_coach_card.setStyleSheet(
            "QFrame { background-color: rgba(4, 30, 42, 235); border: 1px solid #0397AB; "
            "border-left: 3px solid #0AC8B9; border-radius: 2px; }"
        )
        scl = QVBoxLayout(self.sim_coach_card)
        scl.setContentsMargins(10, 6, 10, 6)
        scl.addWidget(QLabel("◈ AI 实时参谋：敌方剑魔 8/2 领跑且我方缺重伤 -> 1,650G 立即买湮灭宝珠+红水晶，留控断剑魔二段 Q"))
        self.sim_coach_seal = HextechSealOverlay(
            self.sim_coach_card,
            seal_id="sim_coach",
            title="海克斯 AI 实时参谋 · 能量枯朽",
            subtitle=build_live_peephole_hint(
                state=CoreEnergyState.CORRODED_TRIAL_END,
                my_gold=1650,
                is_dead=True,
                top_threat_name="亚托克斯",
                top_threat_kda="8/2",
                enemy_heal_high=True,
                ally_has_grievous=False,
            ),
            compact=True,
            allow_idle_ambient=False,
        )
        self.sim_coach_seal.unseal_settled.connect(lambda _s: self.sim_coach_card.trigger_pulse("#0AC8B9", 420))
        self.sim_coach_seal.seal_clicked.connect(self._on_sandbox_seal_clicked)
        dl.addWidget(self.sim_coach_card)
        root.addWidget(drawer_sim)

        # Interactive control deck (clean crisp labels, zero overlap at 940px)
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(8)
        for label, st_key in (
            ("S0 未登录", "s0"),
            ("S1 体验中", "s1"),
            ("S2 枯朽封印", "s2"),
            ("S3 充能破封", "s3"),
            ("局内自动快演", "fast"),
            ("3秒对比窗", "preview"),
        ):
            b = HextechActionButton(label, variant="cyan" if st_key in ("s3", "fast") else "gold", height=32)
            b.clicked.connect(lambda _=False, k=st_key: self._apply_sandbox_mode(k))
            ctrl_row.addWidget(b, 1)
        root.addLayout(ctrl_row)

        self._apply_sandbox_mode("s2")

    def _on_sandbox_seal_clicked(self, seal_id: str, mode_val: str) -> None:
        if mode_val == SealMode.CHARGED_READY.value:
            # Domino cascade across remaining lobby seals at 90ms stagger
            delay = 90
            for seal in self._lobby_seals:
                if seal.seal_id != seal_id and seal.mode == SealMode.CHARGED_READY:
                    QTimer.singleShot(delay, lambda s=seal: s.play_unseal(fast=False))
                    delay += 90
            if self.sim_brief_seal.mode == SealMode.CHARGED_READY:
                QTimer.singleShot(delay, lambda: self.sim_brief_seal.play_unseal(fast=True))
            if self.sim_coach_seal.mode == SealMode.CHARGED_READY:
                QTimer.singleShot(delay + 80, lambda: self.sim_coach_seal.play_unseal(fast=True))
            self.state_badge.setText("当前态：S4 已亲手击碎封印 · 海克斯核心全量激活！")
        elif mode_val in (SealMode.CORRODED.value, SealMode.GUEST_CLAIM.value):
            st = CoreEnergyState.GUEST_UNCLAIMED if mode_val == SealMode.GUEST_CLAIM.value else CoreEnergyState.CORRODED_TRIAL_END
            dlg = CoreInsightPreviewDialog(
                state=st,
                last_ai_highlight="上局第 3 件听从 AI 补湮灭宝珠，压制 9/2 剑魔成功翻盘" if st == CoreEnergyState.CORRODED_TRIAL_END else "",
                parent=self,
            )
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self._apply_sandbox_mode("s3")

    def _apply_sandbox_mode(self, key: str) -> None:
        if key == "preview":
            dlg = CoreInsightPreviewDialog(
                state=CoreEnergyState.CORRODED_TRIAL_END,
                last_ai_highlight="上局第 3 件听从 AI 补湮灭宝珠，压制 9/2 剑魔成功翻盘",
                parent=self,
            )
            dlg.exec()
            return
        if key == "fast":
            self._apply_sandbox_mode("s2")
            QTimer.singleShot(120, lambda: self.sim_brief_seal.play_unseal(fast=True))
            QTimer.singleShot(200, lambda: self.sim_coach_seal.play_unseal(fast=True))
            self.state_badge.setText("当前态：轨道 B 局内到账 · 380ms 免点击自动震碎 + 补发推演")
            return
        if key == "s0":
            self.state_badge.setText("当前态：S0 未登录访客 · 初始晶核 450 积分待认领（零锈蚀压迫）")
            for s in self._lobby_seals:
                s.set_seal_mode(SealMode.GUEST_CLAIM, cta_text="免费领 450 积分")
            self.sim_brief_seal.set_copy(title="开局 10 人作战简报")
            self.sim_brief_seal.set_seal_mode(
                SealMode.GUEST_CLAIM,
                subtitle="敌方 3AP + 高回血 · 注册领 450 积分免费试 2 局",
                cta_text="领 450 积分",
            )
            self.sim_coach_seal.set_copy(title="初始晶核 · 450 积分待认领")
            self.sim_coach_seal.set_seal_mode(
                SealMode.GUEST_CLAIM,
                subtitle="手握 1,650G · 注册赠 450 积分，零门槛免费试 AI 散件反制",
                cta_text="领 2 局体验",
            )
        elif key == "s1":
            self.state_badge.setText("当前态：S1 免费 450 积分体验中（约剩 2 局）· 全模块清晰亮起")
            for s in (*self._lobby_seals, self.sim_brief_seal, self.sim_coach_seal):
                s.set_seal_mode(SealMode.UNLOCKED)
        elif key == "s2":
            self.state_badge.setText("当前态：S2 体验耗尽 · 枯朽海克斯封印 + 本地 5ms 门缝实时剧透（点击先看对比窗）")
            for s in self._lobby_seals:
                s.set_seal_mode(SealMode.CORRODED, cta_text="查看实战差异")
            self.sim_brief_seal.set_copy(title="开局 10 人作战简报")
            self.sim_brief_seal.set_seal_mode(
                SealMode.CORRODED,
                subtitle="敌方多回血 / 高爆发 · 10 人开局定盘待充能唤醒",
                cta_text="5元充能",
            )
            self.sim_coach_seal.set_copy(title="海克斯 AI 实时参谋 · 能量枯朽")
            self.sim_coach_seal.set_seal_mode(
                SealMode.CORRODED,
                subtitle="手握 1,650G · 敌方「亚托克斯 8/2」且缺重伤 · 精准散件买法待充能唤醒",
                cta_text="查看差异",
            )
        elif key == "s3":
            self.state_badge.setText("当前态：S3 充值已到账 · 裂纹青光脉动，请亲手点击任意封印卡触发 720ms 多米诺破封！")
            for s in self._lobby_seals:
                s.set_seal_mode(SealMode.CHARGED_READY, subtitle="能量已注入 · 点击击碎封印", cta_text="✦ 点击破封")
            self.sim_brief_seal.set_copy(title="开局 10 人作战简报")
            self.sim_brief_seal.set_seal_mode(
                SealMode.CHARGED_READY,
                subtitle="能量已注入 · 点击破封立即激活定盘",
                cta_text="✦ 破封",
            )
            self.sim_coach_seal.set_copy(title="海克斯 AI 实时参谋 · 核心已充能")
            self.sim_coach_seal.set_seal_mode(
                SealMode.CHARGED_READY,
                subtitle="能量已注入 · 点击破封立即唤醒实时参谋",
                cta_text="✦ 点击破封",
            )
