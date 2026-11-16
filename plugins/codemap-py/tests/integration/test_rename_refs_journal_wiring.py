"""Both ``rename-refs`` runtimes wire the rollback journal around every source write.

The journal helper (``bin/rename_snapshot.py``) only protects a run when the skill calls it: ``before`` ahead of the
first write, ``after`` once each write lands, ``restore`` on any post-edit failure and ``discard`` only on success.
These checks read the skill text on whitespace-collapsed lines, so reflowing a paragraph never breaks them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _codemap_skill_text import between, flat, missing

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE = _PLUGIN_ROOT / "claude-skills" / "rename-refs" / "SKILL.md"
_CODEX = _PLUGIN_ROOT / "codex-skills" / "rename-refs" / "SKILL.md"
_CLAUDE_CALL = 'python "$PLUGIN_ROOT/bin/rename_snapshot.py"'
_CODEX_CALL = '"PLUGIN_ROOT/bin/python" "PLUGIN_ROOT/bin/rename_snapshot.py" --runtime codex'
_RUNTIMES = [
    pytest.param(_CLAUDE, _CLAUDE_CALL, "## Step 2", "## Step 7", id="claude"),
    pytest.param(_CODEX, _CODEX_CALL, "### 2. Resolve targets", "### 7. Summary", id="codex"),
]


@pytest.mark.parametrize(("skill", "call", "first_query", "summary"), _RUNTIMES)
def test_journal_opens_before_first_source_write(skill: Path, call: str, first_query: str, summary: str) -> None:
    """A fresh journal opens (after setting a leftover one aside) before any query or source write.

    Opening the journal is the run's preflight point: ``before`` refuses a late file changed after it, so it must
    precede every step that can write.
    """
    text = skill.read_text(encoding="utf-8")

    assert text.index(f"{call} discard --retain") < text.index(f"{call} before") < text.index(first_query)


@pytest.mark.parametrize(("skill", "call", "first_query", "summary"), _RUNTIMES)
def test_leftover_journal_stops_before_first_query(skill: Path, call: str, first_query: str, summary: str) -> None:
    """A journal left by an earlier run stops Step 1 with an offer to restore it, before any index query.

    ``discard --retain`` exits 5 when it sets aside a journal holding operations. The skill must not open a new journal
    over that half-renamed tree; it names the retained path and the ``restore --journal`` command instead.
    """
    text = skill.read_text(encoding="utf-8")
    preflight = text[: text.index(first_query)]

    assert f"{call} restore --journal" in preflight
    assert missing(flat(preflight), ("previous rename left unfinished edits", "RETAINED_JOURNAL")) == []


@pytest.mark.parametrize(("skill", "call", "first_query", "summary"), _RUNTIMES)
def test_restore_described_as_all_or_nothing(skill: Path, call: str, first_query: str, summary: str) -> None:
    """Both skills tell the user that a restore conflict restores nothing and keeps every file as it is."""
    prose = flat(skill.read_text(encoding="utf-8"))

    assert missing(prose, ("all-or-nothing", "keeps every file as it is")) == []


@pytest.mark.parametrize(("skill", "call", "first_query", "summary"), _RUNTIMES)
def test_every_write_kind_is_journaled(skill: Path, call: str, first_query: str, summary: str) -> None:
    """Edits, the hard-delete and ``git mv`` each have a ``before`` and a recording ``after``.

    The per-file ``before`` must appear ahead of its ``after`` and of the move record, so the instruction order matches
    the order the run executes them.
    """
    text = skill.read_text(encoding="utf-8")
    prose = flat(text)

    assert text.index(f"{call} before --file") < text.index(f"{call} after --file") < text.index(f"{call} after --move")
    assert missing(prose, ("journal `before`", "hard-delete", "`git mv`")) == []


@pytest.mark.parametrize(("skill", "call", "first_query", "summary"), _RUNTIMES)
def test_post_edit_failure_restores_automatically(skill: Path, call: str, first_query: str, summary: str) -> None:
    """Every post-edit failure routes to ``restore`` without a question; only success runs ``discard``.

    The stop routing names the restore, and the summary step runs ``restore`` for an incomplete rename before it offers
    ``discard`` for a verified one.
    """
    text = skill.read_text(encoding="utf-8")
    routing = flat(between(text, "After the first source edit", "\n"))
    closing = text.split(summary, 1)[1]

    assert "restore" in routing
    assert "without a question" in flat(text)
    assert closing.index(f"{call} restore") < closing.index(f"{call} discard")


@pytest.mark.parametrize(("skill", "call", "first_query", "summary"), _RUNTIMES)
def test_no_best_effort_rollback_claim(skill: Path, call: str, first_query: str, summary: str) -> None:
    """The skill no longer promises best-effort edits without rollback."""
    prose = flat(skill.read_text(encoding="utf-8"))

    assert [phrase for phrase in ("without automatic rollback", "best-effort") if phrase in prose.lower()] == []
