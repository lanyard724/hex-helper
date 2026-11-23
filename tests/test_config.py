from pathlib import Path

from hex_helper.config import (
    AppConfig,
    NormalizedRect,
    format_display_patch,
    load_config,
    resolve_thinking_level,
    save_config,
)


def test_config_roundtrip(tmp_path: Path):
    cfg_path = tmp_path / "config.json"
    cfg = AppConfig(
        proxy_url="",
        dataset_patch="16.23",
        dataset_mode="first-prismatic",
        lockfile_path=r"D:\WeGameApps\英雄联盟\LeagueClient\lockfile",
        coach_mode="cloud",
        gemini_model="gemini-4-hydrogen",
        gemini_thinking_level="xhigh",
        drawer_y_ratio=0.45,
        card_band_roi=NormalizedRect(0.15, 0.25, 0.70, 0.45),
    )
    save_config(cfg, cfg_path)
    loaded = load_config(cfg_path)
    assert loaded.dataset_patch == "16.23"
    assert loaded.dataset_mode == "first-prismatic"
    assert loaded.lockfile_path.endswith("lockfile")
    assert loaded.coach_mode == "cloud"
    assert loaded.gemini_model == "gemini-4-hydrogen"
    assert loaded.gemini_thinking_level == "xhigh"
    assert loaded.drawer_y_ratio == 0.45
    assert loaded.card_band_roi.to_pixels(2560, 1440) == (384, 360, 2176, 1008)


def test_config_clamping():
    cfg = AppConfig.from_dict(
        {
            "coach_mode": "invalid_mode",
            "gemini_thinking_level": "extreme_bogus",
            "drawer_y_ratio": 5.0,
            "card_band_roi": {"x": -0.5, "y": 0.99, "w": 2.0, "h": 0.01},
        }
    )
    assert cfg.coach_mode == "cloud"
    assert cfg.gemini_thinking_level == "medium"
    assert cfg.gemini_model == "gemini-4-hydrogen"
    assert cfg.drawer_y_ratio == 0.82
    assert cfg.card_band_roi.x == 0.0
    assert cfg.card_band_roi.y == 0.95
    assert cfg.card_band_roi.h == 0.05


def test_format_display_patch():
    assert format_display_patch("16.23") == "26.23（内部 16.23）"
    assert format_display_patch("16.22") == "26.22（内部 16.22）"
    assert format_display_patch("16.21-prehotfix") == "26.21-prehotfix（内部 16.21-prehotfix）"
    assert format_display_patch("14.24") == "14.24"


def test_resolve_thinking_level_adaptive_and_model_floor():
    # Adaptive scales down by trigger urgency, and legacy xhigh is capped to medium
    assert resolve_thinking_level("xhigh", "augment_picking", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("xhigh", "connection_test", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("xhigh", "game_start", adaptive=True, model="gemini-4-hydrogen") == "medium"
    assert resolve_thinking_level("xhigh", "death_enemy_build_changed", adaptive=True, model="gemini-4-hydrogen") == "medium"
    assert resolve_thinking_level("xhigh", "augment_selected", adaptive=True, model="gemini-4-hydrogen") == "medium"
    assert resolve_thinking_level("xhigh", "manual_f6", adaptive=True, model="gemini-4-hydrogen") == "medium"
    # Never exceeds user ceiling
    assert resolve_thinking_level("low", "manual_f6", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("low", "death_enemy_build_changed", adaptive=True, model="gemini-4-hydrogen") == "low"
    # Fixed mode keeps configured level while still capping legacy xhigh -> medium
    assert resolve_thinking_level("xhigh", "augment_picking", adaptive=False, model="gemini-4-hydrogen") == "medium"
    assert resolve_thinking_level("high", "augment_picking", adaptive=False, model="gemini-4-hydrogen") == "high"
    # Gemini 4 floor clamps 'minimal' -> 'low', while Flash allows 'minimal'
    assert resolve_thinking_level("minimal", "augment_picking", adaptive=True, model="gemini-4-hydrogen") == "low"
    assert resolve_thinking_level("minimal", "augment_picking", adaptive=True, model="gemini-3.8-flash") == "minimal"

