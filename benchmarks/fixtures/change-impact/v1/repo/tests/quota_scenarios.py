"""Quota scenarios representing test-side callsites."""

from impactlib.quota import apply, apply as quota_apply


def check_batch_preview() -> int:
    """Exercise the batch-preview scenario."""
    return apply(5, 4)


def check_ledger_snapshot() -> int:
    """Exercise the ledger-snapshot scenario."""
    return quota_apply(5, 4)


def check_client_summary() -> int:
    """Exercise the client-summary scenario."""
    return apply(5, limit=4)


def check_daily_digest() -> int:
    """Exercise the daily-digest scenario."""
    return apply(5)
