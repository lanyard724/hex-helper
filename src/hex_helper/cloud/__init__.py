"""Commercial cloud authentication, DPAPI session persistence, AI Coach client, and self-updater."""

from hex_helper.cloud.client import (
    CloudAdviseResult,
    CloudAdviseStats,
    CloudCoachError,
    CloudCoachWorker,
    CloudGatewayClient,
)
from hex_helper.cloud.session_store import DpapiSessionStore
from hex_helper.cloud.updater import (
    UpdateError,
    UpdateInfo,
    UpdateVerificationError,
    check_for_update,
    download_and_verify_update,
    launch_update_installer,
    parse_update_info,
)

__all__ = [
    "CloudAdviseResult",
    "CloudAdviseStats",
    "CloudCoachError",
    "CloudCoachWorker",
    "CloudGatewayClient",
    "DpapiSessionStore",
    "UpdateError",
    "UpdateInfo",
    "UpdateVerificationError",
    "check_for_update",
    "download_and_verify_update",
    "launch_update_installer",
    "parse_update_info",
]
