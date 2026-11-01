"""Text helpers shared by the foundry tests that read shipped Markdown rule, skill and agent files.

Deliberately a uniquely-named module rather than ``conftest``, for the reason ``_hook_env.py`` gives. These are plain
functions the tests call directly, so they are imported by name; ``conftest.py`` puts this directory on ``sys.path``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def flat_text(root: Path, relative: str) -> str:
    """Return ``root / relative`` with whitespace runs collapsed, so a reflow cannot split a phrase.

    Examples:
        >>> from tempfile import TemporaryDirectory
        >>> with TemporaryDirectory() as directory:
        ...     _ = (Path(directory) / "a.md").write_text("one\\n  two   three\\n", encoding="utf-8")
        ...     flat_text(Path(directory), "a.md")
        'one two three'
    """
    return " ".join((root / relative).read_text(encoding="utf-8").split())


def missing(text: str, fragments: Iterable[str]) -> list[str]:
    """Return the fragments that ``text`` does not contain, so a failure names every absent one.

    Examples:
        >>> missing("alpha beta", ["alpha", "gamma"])
        ['gamma']
    """
    return [fragment for fragment in fragments if fragment not in text]


def found(text: str, fragments: Iterable[str]) -> list[str]:
    """Return the fragments that ``text`` still contains, so a failure names every leftover one.

    Examples:
        >>> found("alpha beta", ["alpha", "gamma"])
        ['alpha']
    """
    return [fragment for fragment in fragments if fragment in text]
