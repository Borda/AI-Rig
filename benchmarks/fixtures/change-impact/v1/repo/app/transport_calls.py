"""Production transport call shapes."""

from impactlib.transport import connect as open_connection
import impactlib.transport as transport_api


def open_session() -> str:
    """Open a session through the direct API."""
    return open_connection("host", timeout=2)


def connect_worker() -> str:
    """Connect a worker through the module API."""
    return transport_api.connect("host", timeout=2)


def open_dashboard() -> str:
    """Open a dashboard through the direct API."""
    return open_connection("host")


def connect_monitor() -> str:
    """Connect a monitor through the module API."""
    return transport_api.connect("host")
