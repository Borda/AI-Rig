"""Policy helper used by controlled migration scenarios."""

from __future__ import annotations


def check(value: str | None, allow_none: bool = False) -> bool:
    """Accept nonempty values and optionally accept null values."""
    return value is not None or allow_none
