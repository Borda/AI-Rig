"""Tests for ``bin/audit_churn.py`` — git-recurrence signal parsing (Phase-5 Layer 3).

Covers commit-subject classification, per-file churn counting from a ``git log --name-only`` dump, and the one-line hint
naming the dominant commit type. No git process is spawned; every input is a literal string or ``Counter``.
"""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent.parent / "bin" / "audit_churn.py"
_spec = importlib.util.spec_from_file_location("audit_churn", _MOD_PATH)
assert _spec
assert _spec.loader
ch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ch)


class TestClassifyCommit:
    """Mapping of a commit subject to its conventional-commit type."""

    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            pytest.param("fix(oss): repair x", "fix", id="fix-oss-repair-x"),
            pytest.param("feat: add y", "feat", id="feat-add-y"),
            pytest.param("refactor!: breaking", "refactor", id="refactor-breaking"),
            pytest.param("refine(foundry): tweak", "refine", id="refine-foundry-tweak"),
            pytest.param("chore: bump", "chore", id="chore-bump"),
            pytest.param("plain english subject", "other", id="plain-english-subject"),
            pytest.param("wip: not a known type", "other", id="wip-not-a-known-type"),
            pytest.param("", "other", id="empty"),
        ],
    )
    def test_maps_subject_to_type(self, subject: str, expected: str) -> None:
        """Known conventional-commit types are recognised; anything else, including an empty subject, is `other`.

        The scope in parentheses and a breaking `!` marker must not change the type, and an unknown prefix such as `wip`
        must not be reported as a type of its own, or the recurrence counts would fragment.
        """
        assert ch.classify_commit(subject) == expected


class TestParseChurn:
    """Per-file commit counts from a blank-line separated name-only dump."""

    @pytest.mark.parametrize(
        ("dump", "path_prefix", "expected"),
        [
            pytest.param(
                "a.py\nb.py\n\na.py\n\nc.py\na.py\n",
                None,
                {"a.py": 3, "b.py": 1, "c.py": 1},
                id="counts-per-commit-block",
            ),
            pytest.param(
                "plugins/x.py\ndocs/y.md\nplugins/z.py\n",
                "plugins/",
                {"plugins/x.py": 1, "plugins/z.py": 1},
                id="path-prefix-filters",
            ),
        ],
    )
    def test_counts_files_per_commit(self, dump: str, path_prefix: str | None, expected: dict[str, int]) -> None:
        """Each file is counted once per commit block, and a path prefix restricts counting to matching files.

        A commit block is a run of file names ended by a blank line; churn is the number of blocks naming a file, and
        the optional prefix scopes the signal to one part of the tree.
        """
        assert dict(ch.parse_churn(dump, path_prefix)) == expected


class TestRecurringTheme:
    """The hint naming the dominant commit type in a file's history."""

    def test_names_dominant_type(self) -> None:
        """The dominant type and its share of all commits are surfaced in the hint.

        Five fixes against one feature must read as a fix-dominated history, with the 5/6 share shown.
        """
        hint = ch.recurring_theme(Counter({"fix": 5, "feat": 1}))
        assert "fix dominates" in hint
        assert "5/6" in hint

    def test_empty_history_gives_empty_hint(self) -> None:
        """No history yields an empty hint instead of raising.

        A file with no recorded commits has no dominant type, and the report must still render its row.
        """
        assert ch.recurring_theme(Counter()) == ""
