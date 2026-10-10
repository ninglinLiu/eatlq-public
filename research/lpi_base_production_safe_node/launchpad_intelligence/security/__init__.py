from .network import NetworkFailClosed
from .paths import assert_under_workspace, is_under_workspace
from .secrets import scan_path_for_secrets, scan_text_for_secrets
from .auth_gate import is_capability_authorized, require_capability, CAP_REDIS_HEALTH

__all__ = [
    "NetworkFailClosed",
    "assert_under_workspace",
    "is_under_workspace",
    "scan_path_for_secrets",
    "scan_text_for_secrets",
    "is_capability_authorized",
    "require_capability",
    "CAP_REDIS_HEALTH",
]
