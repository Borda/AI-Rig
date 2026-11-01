"""Whitespace-insensitive text helpers for tests that read Markdown skill and contract files.

Reading prose on whitespace-collapsed text keeps an assertion independent of where a paragraph wraps. Importable from
every test module because ``conftest.py`` puts this directory on ``sys.path``.
"""

from __future__ import annotations

from pathlib import Path


def flat(text: str) -> str:
    """Collapse all whitespace runs, so assertions never depend on where prose wraps.

    >>> flat("a\\n  b   c")
    'a b c'
    """
    return " ".join(text.split())


def flat_file(path: Path) -> str:
    """Read a Markdown/text file with every whitespace run collapsed to one space (wrap-insensitive)."""
    return flat(path.read_text(encoding="utf-8"))


def between(text: str, start: str, end: str) -> str:
    """Return the text after the first *start* marker and before the next *end* marker."""
    return text.split(start, 1)[1].split(end, 1)[0]


def missing(text: str, phrases: tuple[str, ...]) -> list[str]:
    """Return the *phrases* absent from *text*, so a failing assertion names exactly what disappeared."""
    return [phrase for phrase in phrases if phrase not in text]
