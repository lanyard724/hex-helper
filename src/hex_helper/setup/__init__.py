"""Windows dependency preflight checks and offline seed extraction for HexHelper."""

from hex_helper.setup.preflight import (
    PreflightItem,
    PreflightReport,
    check_vcredist_x64,
    check_winrt_zh_ocr,
    extract_bundled_seed_cache,
    get_bundle_root,
    run_windows_preflight,
    trigger_install_zh_ocr_pack,
)

__all__ = [
    "PreflightItem",
    "PreflightReport",
    "check_vcredist_x64",
    "check_winrt_zh_ocr",
    "extract_bundled_seed_cache",
    "get_bundle_root",
    "run_windows_preflight",
    "trigger_install_zh_ocr_pack",
]
