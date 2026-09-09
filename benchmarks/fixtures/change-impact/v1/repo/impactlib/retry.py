"""Retry helper used by controlled migration scenarios."""

from __future__ import annotations


def invoke(value: int, retries: int = 3) -> int:
    """Return a value scaled by its retry count."""
    return value * retries
