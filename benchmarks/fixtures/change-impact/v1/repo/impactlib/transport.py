"""Transport helper used by controlled migration scenarios."""

from __future__ import annotations


def connect(host: str, timeout: int = 1) -> str:
    """Return a deterministic connection descriptor."""
    return f"{host}:{timeout}"
