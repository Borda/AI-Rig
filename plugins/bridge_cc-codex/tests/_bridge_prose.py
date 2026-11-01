"""Whitespace-insensitive Markdown reading shared by the Bridge prose-contract tests."""

from __future__ import annotations

from pathlib import Path


def read_prose(path: Path) -> str:
    """Read a Markdown file with all whitespace runs collapsed so a reflowed paragraph cannot break a phrase check."""
    return " ".join(path.read_text(encoding="utf-8").split())
