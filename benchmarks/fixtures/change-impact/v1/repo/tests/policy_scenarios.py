"""Policy scenarios representing test-side callsites."""

import impactlib.policy as policy_api
from impactlib.policy import check


def check_request() -> bool:
    """Exercise request validation."""
    return check("value")


def check_record() -> bool:
    """Exercise record validation."""
    return policy_api.check("value")


def check_event() -> bool:
    """Exercise event validation."""
    return check("value", allow_none=False)


def check_export() -> bool:
    """Exercise export validation."""
    return policy_api.check("value", False)
