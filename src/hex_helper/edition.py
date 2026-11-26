"""Application edition and release metadata for HexHelper."""

from __future__ import annotations

import os

APP_VERSION = "1.4.7"
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


def is_devlab_allowed() -> bool:
    """Public commercial client never enables local DevLab flight recording."""
    return False


def get_dev_build_tag() -> str:
    """Return canonical release version for public desktop builds."""
    return APP_VERSION
