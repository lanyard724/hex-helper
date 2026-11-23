"""Application edition and release metadata for HexHelper."""

from __future__ import annotations

import os

APP_VERSION = "1.3.3"
APP_NAME = "HexHelper"
_COMPILED_EDITION = "commercial"

EDITION: str = "commercial"
IS_COMMERCIAL: bool = True
DEFAULT_CLOUD_API_BASE: str = os.environ.get(
    "HEX_CLOUD_API_BASE",
    "https://api.priswebtool.site",
).rstrip("/")


def is_commercial_edition() -> bool:
    """Public desktop client always runs in unified cloud-connected mode."""
    return True
