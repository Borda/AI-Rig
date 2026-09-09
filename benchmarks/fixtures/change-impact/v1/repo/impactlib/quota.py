"""Quota helper used by controlled migration scenarios."""

from __future__ import annotations


def apply(value: int, limit: int = 10) -> int:
    """Cap a value at its configured limit."""
    return min(value, limit)
