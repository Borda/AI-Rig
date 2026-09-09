"""Retry scenarios representing test-side callsites."""

from impactlib.retry import invoke
import impactlib.retry as retry_api


def check_process_queue() -> int:
    """Exercise queued-item processing."""
    return invoke(2, 4)


def check_recover_job() -> int:
    """Exercise job recovery."""
    return retry_api.invoke(2, 4)


def check_record_result() -> int:
    """Exercise result recording."""
    return invoke(2, retries=4)


def check_schedule_refresh() -> int:
    """Exercise refresh scheduling."""
    return retry_api.invoke(2)
