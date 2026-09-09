"""Production retry call shapes."""

from impactlib.retry import invoke
import impactlib.retry as retry_api


def process_queue() -> int:
    """Process a queued item through the direct API."""
    return invoke(2, 4)


def recover_job() -> int:
    """Recover a job through the module API."""
    return retry_api.invoke(2, 4)


def record_result() -> int:
    """Record a completed result."""
    return invoke(2, retries=4)


def schedule_refresh() -> int:
    """Schedule a refresh through the module API."""
    return retry_api.invoke(2)
