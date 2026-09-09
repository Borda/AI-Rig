"""Production quota call shapes."""

from impactlib.quota import apply, apply as quota_apply


def batch_preview() -> int:
    """Build a bounded preview for a batch."""
    return apply(5, 4)


def ledger_snapshot() -> int:
    """Build a bounded snapshot for a ledger."""
    return quota_apply(5, 4)


def client_summary() -> int:
    """Build a client summary."""
    return apply(5, limit=4)


def daily_digest() -> int:
    """Build a daily digest."""
    return apply(5)


def local_preview() -> int:
    """Build a local preview with a private helper."""

    def apply(value: int, limit: int) -> int:
        return value + limit

    return apply(5, 4)
