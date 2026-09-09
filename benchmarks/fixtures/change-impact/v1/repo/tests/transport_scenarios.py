"""Transport scenarios representing test-side callsites."""

from impactlib.transport import connect as open_connection
import impactlib.transport as transport_api


def check_open_session() -> str:
    """Exercise session opening."""
    return open_connection("host", timeout=2)


def check_connect_worker() -> str:
    """Exercise worker connection."""
    return transport_api.connect("host", timeout=2)


def check_open_dashboard() -> str:
    """Exercise dashboard opening."""
    return open_connection("host")


def check_connect_monitor() -> str:
    """Exercise monitor connection."""
    return transport_api.connect("host")
