"""Tests for ``bin/audit_churn.py`` — git-recurrence signal parsing (Phase-5 Layer 3)."""

from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent / "bin" / "audit_churn.py"
_spec = importlib.util.spec_from_file_location("audit_churn", _MOD_PATH)
assert _spec and _spec.loader
ch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ch)


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
def test_classify_commit(subject: str, expected: str) -> None:
    """Conventional-commit types are classified; unknown/plain → 'other'."""
    assert ch.classify_commit(subject) == expected


def test_parse_churn_counts_per_file() -> None:
    """Each file is counted once per commit block (blank-line separated)."""
    dump = "a.py\nb.py\n\na.py\n\nc.py\na.py\n"
    assert dict(ch.parse_churn(dump)) == {"a.py": 3, "b.py": 1, "c.py": 1}


def test_parse_churn_path_prefix_filters() -> None:
    """A path prefix restricts counting to matching files."""
    dump = "plugins/x.py\ndocs/y.md\nplugins/z.py\n"
    assert dict(ch.parse_churn(dump, "plugins/")) == {"plugins/x.py": 1, "plugins/z.py": 1}


def test_recurring_theme_names_dominant_type() -> None:
    """The dominant type and its share are surfaced in the hint."""
    hint = ch.recurring_theme(Counter({"fix": 5, "feat": 1}))
    assert "fix dominates" in hint and "5/6" in hint


def test_recurring_theme_empty_history() -> None:
    """No history → empty hint (never crashes)."""
    assert ch.recurring_theme(Counter()) == ""
