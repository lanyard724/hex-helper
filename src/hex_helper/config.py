"""Application configuration, path helpers, and patch version formatting."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from hex_helper.edition import DEFAULT_CLOUD_API_BASE, is_commercial_edition


@dataclass
class NormalizedRect:
    """Normalized [0..1] rectangle relative to the game client area."""

    x: float
    y: float
    w: float
    h: float

    def clamp(self) -> NormalizedRect:
        x = max(0.0, min(0.95, float(self.x)))
        y = max(0.0, min(0.95, float(self.y)))
        w = max(0.05, min(1.0 - x, float(self.w)))
        h = max(0.05, min(1.0 - y, float(self.h)))
        return NormalizedRect(x=x, y=y, w=w, h=h)

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """Return (left, top, right, bottom) in pixels."""
        c = self.clamp()
        left = int(round(c.x * width))
        top = int(round(c.y * height))
        right = int(round((c.x + c.w) * width))
        bottom = int(round((c.y + c.h) * height))
        return left, top, max(left + 1, right), max(top + 1, bottom)


@dataclass
class HotkeyConfig:
    force_scan: str = "F2"
    toggle_drawer: str = "F3"
    toggle_all: str = "F4"
    call_coach: str = "F6"
    toggle_combo: str = "F7"
    calibrate_roi: str = "F8"
    toggle_sidebars: str = "F9"
    toggle_lookup: str = ""
    open_recap: str = ""


COACH_MODES = ("cloud", "off")
COACH_TIERS = ("a", "b", "auto")
GEMINI_MODELS = (
    "gemini-4-hydrogen",
    "gemini-4-argon",
    "gemini-4-carbon",
    "gemini-4-neon",
    "gemini-3.8-flash",
)
GEMINI_THINKING_LEVELS = ("xhigh", "high", "medium", "low", "minimal")
_THINKING_RANK: dict[str, int] = {
    "minimal": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
}
_RANK_TO_THINKING: dict[int, str] = {v: k for k, v in _THINKING_RANK.items()}


def resolve_thinking_level(
    configured_level: str,
    trigger_reason: str = "manual_f6",
    *,
    adaptive: bool = True,
    model: str = "gemini-4-hydrogen",
    is_transformative: bool | None = None,
    fast_death: bool = False,
) -> str:
    """Resolve effective Gemini thinking_level based on trigger urgency and model floor.

    - Caps legacy 'xhigh' -> 'medium' so local direct Gemini never stalls ~50s in live ARAM.
    - Gemini 4 models enforce minimum level 'low' ('minimal' is Flash-only).
    - When adaptive=True, caps level by real-time latency budget without ever exceeding configured_level:
      * augment_picking -> <= 'minimal' (clamped to 'low' on Gemini 4, 'minimal' on Flash)
      * connection_test / pre_death_warmup -> <= 'low'
      * death_enemy_build_changed -> <= 'low' when fast_death=True (or Flash model); <= 'medium' otherwise
      * augment_selected -> <= 'low' when is_transformative is False; <= 'medium' for transformative pivots or unspecified
      * game_start -> <= 'medium'
      * manual_f6 (explicit user deep analysis) -> configured_level (with 'xhigh' capped to 'medium')
    """
    lvl = (configured_level or "medium").strip().lower()
    if lvl not in _THINKING_RANK or lvl == "xhigh":
        lvl = "medium"
    is_gemini4 = (model or "").strip().lower().startswith("gemini-4")
    min_rank = 1 if is_gemini4 else 0
    rank = max(min_rank, _THINKING_RANK[lvl])

    if adaptive:
        reason = (trigger_reason or "").strip().lower()
        if reason == "augment_picking":
            rank = min(rank, max(min_rank, _THINKING_RANK["minimal"]))
        elif reason in ("connection_test", "pre_death_warmup"):
            rank = min(rank, max(min_rank, _THINKING_RANK["low"]))
        elif reason == "death_enemy_build_changed":
            target = "low" if (fast_death or not is_gemini4) else "medium"
            rank = min(rank, max(min_rank, _THINKING_RANK[target]))
        elif reason == "augment_selected":
            target = "low" if is_transformative is False else "medium"
            rank = min(rank, max(min_rank, _THINKING_RANK[target]))
        elif reason == "game_start":
            rank = min(rank, max(min_rank, _THINKING_RANK["medium"]))
    return _RANK_TO_THINKING[rank]


def mask_api_key(key: str) -> str:
    """Return safely masked preview of an API key for logs and UI labels."""
    s = (key or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}••••{s[-4:]}"


def _default_proxy_url() -> str:
    return ""


def _default_bridge_enabled() -> bool:
    return not is_commercial_edition()


def _default_coach_mode() -> str:
    return "cloud" if is_commercial_edition() else "auto"


def _default_coach_tier() -> str:
    return "auto" if is_commercial_edition() else "a"


def _default_cloud_api_base() -> str:
    env_url = os.environ.get("HEX_CLOUD_API_BASE", "").strip().rstrip("/")
    if env_url:
        return env_url
    return DEFAULT_CLOUD_API_BASE if is_commercial_edition() else ""


@dataclass
class AppConfig:
    proxy_url: str = field(default_factory=_default_proxy_url)
    dataset_patch: str = "latest"
    dataset_mode: str = "normal"
    dataset_band: str = ""
    lockfile_path: str = ""
    lol_install_dir: str = ""
    first_run_completed: bool = False
    auto_peek_on_death: bool = True
    force_all_modes: bool = False
    bridge_enabled: bool = field(default_factory=_default_bridge_enabled)
    bridge_port: int = 18421
    auto_trigger_coach: bool = True
    coach_mode: str = field(default_factory=_default_coach_mode)  # "auto" | "gemini" | "antigravity" | "cloud" | "off"
    coach_tier: str = field(default_factory=_default_coach_tier)  # "a" | "b" | "auto"
    cloud_api_base: str = field(default_factory=_default_cloud_api_base)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-4-hydrogen"
    gemini_picker_fast_model: str = "gemini-3.8-flash"
    gemini_thinking_level: str = "medium"
    gemini_adaptive_thinking: bool = True
    telemetry_enabled: bool = True
    devlab_enabled: bool = False
    devlab_roi_snapshots: bool = False
    devlab_debug_log: bool = False
    devlab_max_matches: int = 120
    auto_first_prismatic_mode: bool = True
    smart_picker_ai_gate: bool = True
    card_band_roi: NormalizedRect = field(
        default_factory=lambda: NormalizedRect(x=0.08, y=0.24, w=0.84, h=0.48)
    )
    title_band_roi: NormalizedRect = field(
        default_factory=lambda: NormalizedRect(x=0.08, y=0.385, w=0.84, h=0.052)
    )
    champ_window_pos: list[int] | None = None
    champ_window_size: list[int] | None = None
    drawer_pos: list[int] | None = None
    drawer_size: list[int] | None = None
    drawer_scale: float | None = None
    drawer_y_ratio: float = 0.16
    combo_drawer_pos: list[int] | None = None
    combo_drawer_size: list[int] | None = None
    combo_drawer_scale: float | None = None
    combo_overlay_enabled: bool = True
    combo_auto_popup_on_picker: bool = False
    champ_select_goal_mode: str = "climb"  # "climb" | "fun"
    lobby_scout_enabled: bool = True
    unsealed_user_ids: list[str] = field(default_factory=list)
    seal_snooze_until: int = 0
    newbie_banner_dismissed: bool = False
    opening_brief_seal_dismissed_count: int = 0
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)

    def resolved_gemini_api_key(self) -> str:
        """Return configured API key or fall back to GEMINI_API_KEY env var."""
        if is_commercial_edition():
            return ""
        return (self.gemini_api_key or "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()

    def resolved_cloud_api_base(self) -> str:
        """Return effective cloud API base URL without trailing slash."""
        val = (self.cloud_api_base or "").strip().rstrip("/")
        if val:
            return val
        env_url = os.environ.get("HEX_CLOUD_API_BASE", "").strip().rstrip("/")
        return env_url or DEFAULT_CLOUD_API_BASE

    def to_dict(self) -> dict:
        data = asdict(self)
        if is_commercial_edition():
            data["devlab_enabled"] = False
            data["devlab_roi_snapshots"] = False
            data["devlab_debug_log"] = False
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> AppConfig:
        cfg = cls()
        for key in (
            "proxy_url",
            "dataset_patch",
            "dataset_mode",
            "dataset_band",
            "lockfile_path",
            "lol_install_dir",
            "first_run_completed",
            "auto_peek_on_death",
            "force_all_modes",
            "bridge_enabled",
            "bridge_port",
            "auto_trigger_coach",
            "coach_mode",
            "coach_tier",
            "cloud_api_base",
            "gemini_api_key",
            "gemini_model",
            "gemini_picker_fast_model",
            "gemini_thinking_level",
            "gemini_adaptive_thinking",
            "telemetry_enabled",
            "devlab_enabled",
            "devlab_roi_snapshots",
            "devlab_debug_log",
            "devlab_max_matches",
            "auto_first_prismatic_mode",
            "smart_picker_ai_gate",
            "champ_window_pos",
            "champ_window_size",
            "drawer_pos",
            "drawer_size",
            "drawer_scale",
            "drawer_y_ratio",
            "combo_drawer_pos",
            "combo_drawer_size",
            "combo_drawer_scale",
            "combo_overlay_enabled",
            "combo_auto_popup_on_picker",
            "champ_select_goal_mode",
            "lobby_scout_enabled",
            "unsealed_user_ids",
            "seal_snooze_until",
            "newbie_banner_dismissed",
            "opening_brief_seal_dismissed_count",
        ):
            if key in raw:
                setattr(cfg, key, raw[key])

        cfg.combo_overlay_enabled = bool(cfg.combo_overlay_enabled)
        cfg.combo_auto_popup_on_picker = bool(cfg.combo_auto_popup_on_picker)
        goal_mode = str(cfg.champ_select_goal_mode or "climb").strip().lower()
        cfg.champ_select_goal_mode = goal_mode if goal_mode in ("climb", "fun") else "climb"
        cfg.lobby_scout_enabled = bool(cfg.lobby_scout_enabled)
        cfg.telemetry_enabled = bool(cfg.telemetry_enabled)
        cfg.devlab_enabled = bool(cfg.devlab_enabled)
        cfg.devlab_roi_snapshots = bool(cfg.devlab_roi_snapshots)
        cfg.devlab_debug_log = bool(cfg.devlab_debug_log)
        try:
            cfg.devlab_max_matches = max(10, min(500, int(cfg.devlab_max_matches or 120)))
        except (TypeError, ValueError):
            cfg.devlab_max_matches = 120
        cfg.auto_first_prismatic_mode = bool(cfg.auto_first_prismatic_mode)
        cfg.smart_picker_ai_gate = bool(cfg.smart_picker_ai_gate)
        if isinstance(cfg.unsealed_user_ids, list):
            cleaned_uids: list[str] = []
            for item in cfg.unsealed_user_ids:
                s = str(item or "").strip()
                if s and s not in cleaned_uids:
                    cleaned_uids.append(s)
            cfg.unsealed_user_ids = cleaned_uids[:64]
        else:
            cfg.unsealed_user_ids = []
        try:
            cfg.seal_snooze_until = max(0, int(cfg.seal_snooze_until or 0))
        except (TypeError, ValueError):
            cfg.seal_snooze_until = 0
        cfg.newbie_banner_dismissed = bool(cfg.newbie_banner_dismissed)
        try:
            cfg.opening_brief_seal_dismissed_count = max(0, int(cfg.opening_brief_seal_dismissed_count or 0))
        except (TypeError, ValueError):
            cfg.opening_brief_seal_dismissed_count = 0

        cfg.lol_install_dir = str(cfg.lol_install_dir or "").strip()
        cfg.first_run_completed = bool(cfg.first_run_completed)
        cfg.cloud_api_base = str(cfg.cloud_api_base or "").strip().rstrip("/")
        default_tier = _default_coach_tier()
        tier = str(cfg.coach_tier or default_tier).strip().lower()
        cfg.coach_tier = tier if tier in COACH_TIERS else default_tier

        if is_commercial_edition():
            cfg.proxy_url = ""
            cfg.bridge_enabled = False
            cfg.coach_mode = "off" if str(cfg.coach_mode) == "off" else "cloud"
            cfg.gemini_api_key = ""
            cfg.devlab_enabled = False
            cfg.devlab_roi_snapshots = False
            cfg.devlab_debug_log = False
        else:
            if str(cfg.coach_mode) not in COACH_MODES:
                cfg.coach_mode = "auto"
            cfg.gemini_api_key = str(cfg.gemini_api_key or "").strip()

        cfg.gemini_model = str(cfg.gemini_model or "gemini-4-hydrogen").strip() or "gemini-4-hydrogen"
        cfg.gemini_picker_fast_model = str(cfg.gemini_picker_fast_model or "").strip()
        lvl = str(cfg.gemini_thinking_level or "medium").strip().lower()
        cfg.gemini_thinking_level = lvl if lvl in GEMINI_THINKING_LEVELS else "medium"

        def _is_legacy_3card_roi(r: dict, lx: float, ly: float, lw: float, lh: float) -> bool:
            try:
                return (
                    abs(float(r.get("x", -1)) - lx) < 1e-4
                    and abs(float(r.get("y", -1)) - ly) < 1e-4
                    and abs(float(r.get("w", -1)) - lw) < 1e-4
                    and abs(float(r.get("h", -1)) - lh) < 1e-4
                )
            except (TypeError, ValueError):
                return False

        if isinstance(raw.get("card_band_roi"), dict):
            r = raw["card_band_roi"]
            if _is_legacy_3card_roi(r, 0.14, 0.24, 0.72, 0.48):
                cfg.card_band_roi = NormalizedRect(x=0.08, y=0.24, w=0.84, h=0.48)
            else:
                cfg.card_band_roi = NormalizedRect(
                    x=r.get("x", 0.08),
                    y=r.get("y", 0.24),
                    w=r.get("w", 0.84),
                    h=r.get("h", 0.48),
                ).clamp()

        if isinstance(raw.get("title_band_roi"), dict):
            r = raw["title_band_roi"]
            if (
                _is_legacy_3card_roi(r, 0.14, 0.31, 0.72, 0.25)
                or _is_legacy_3card_roi(r, 0.08, 0.31, 0.84, 0.25)
            ):
                cfg.title_band_roi = NormalizedRect(x=0.08, y=0.385, w=0.84, h=0.052)
            else:
                cfg.title_band_roi = NormalizedRect(
                    x=r.get("x", 0.08),
                    y=r.get("y", 0.385),
                    w=r.get("w", 0.84),
                    h=r.get("h", 0.052),
                ).clamp()

        if isinstance(raw.get("hotkeys"), dict):
            hk = raw["hotkeys"]
            cfg.hotkeys = HotkeyConfig(
                force_scan=str(hk.get("force_scan", "F2") or "").strip(),
                toggle_drawer=str(hk.get("toggle_drawer", "F3") or "").strip(),
                toggle_all=str(hk.get("toggle_all", "F4") or "").strip(),
                call_coach=str(hk.get("call_coach", "F6") or "").strip(),
                toggle_combo=str(hk.get("toggle_combo", "F7") or "").strip(),
                calibrate_roi=str(hk.get("calibrate_roi", "F8") or "").strip(),
                toggle_sidebars=str(hk.get("toggle_sidebars", "F9") or "").strip(),
                toggle_lookup=str(hk.get("toggle_lookup", "") or "").strip(),
                open_recap=str(hk.get("open_recap", "") or "").strip(),
            )

        cfg.drawer_y_ratio = max(0.08, min(0.82, float(cfg.drawer_y_ratio)))
        for scale_attr in ("drawer_scale", "combo_drawer_scale"):
            raw_sc = getattr(cfg, scale_attr, None)
            if raw_sc is None:
                setattr(cfg, scale_attr, None)
            else:
                try:
                    val_sc = float(raw_sc)
                    setattr(cfg, scale_attr, round(max(0.85, min(2.20, val_sc)), 3))
                except (TypeError, ValueError):
                    setattr(cfg, scale_attr, None)
        return cfg


def get_app_data_dir() -> Path:
    """Return %LOCALAPPDATA%\\HexHelper (creating it if needed)."""
    override = os.environ.get("HEX_HELPER_DATA_DIR")
    if override:
        base = Path(override)
    else:
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            base = Path(local_app) / "HexHelper"
        else:
            base = Path.home() / ".hex_helper"
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_config_path() -> Path:
    return get_app_data_dir() / "config.json"


def get_devlab_dir() -> Path:
    """Return the local DevLab Flight Recorder directory without creating it."""
    override = os.environ.get("HEX_DEVLAB_DIR", "").strip()
    if override:
        return Path(override)
    return get_app_data_dir() / "devlab"


def load_config(path: Path | None = None) -> AppConfig:
    target = path or get_config_path()
    if not target.exists():
        return AppConfig()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return AppConfig.from_dict(raw)
    except Exception:
        pass
    return AppConfig()


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    target = path or get_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


_INTERNAL_PATCH_RE = re.compile(r"^1(\d)\.(\d+)(.*)$")


def format_display_patch(internal_patch: str) -> str:
    """Convert Riot internal patch like '16.23' to player-facing '26.23（内部 16.23）'."""
    text = (internal_patch or "").strip()
    m = _INTERNAL_PATCH_RE.match(text)
    if not m:
        return text or "未知版本"
    season_digit = int(m.group(1))
    minor = m.group(2)
    suffix = m.group(3)
    # 15.x -> 25.x, 16.x -> 26.x, etc.
    if season_digit >= 5:
        public = f"2{season_digit}.{minor}{suffix}"
        return f"{public}（内部 {text}）"
    return text
