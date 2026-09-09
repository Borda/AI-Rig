"""Formatting helper used by controlled migration scenarios."""

from __future__ import annotations


def render(text: str, *, style: str = "plain") -> str:
    """Render text while accepting a presentation style."""
    del style
    return text.upper()
