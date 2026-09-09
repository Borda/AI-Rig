"""Production policy call shapes."""

from impactlib.policy import check
import impactlib.policy as policy_api


def validate_request() -> bool:
    """Validate a request through the direct API."""
    return check("value")


def validate_record() -> bool:
    """Validate a record through the module API."""
    return policy_api.check("value")


def validate_event() -> bool:
    """Validate an event through the direct API."""
    return check("value", allow_none=False)


def validate_export() -> bool:
    """Validate an export through the module API."""
    return policy_api.check("value", False)
