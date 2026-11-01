"""Keep specialist Codemap reuse bounded to successful, fresh, matching answers."""

import functools
from pathlib import Path

import pytest
from _markdown_text import flat_text

_PLUGIN = Path(__file__).resolve().parents[1]


_flat = functools.partial(flat_text, _PLUGIN)


@pytest.mark.parametrize(
    "relative",
    [
        "skills/_shared/codemap-context.md",
        "agents/sw-engineer.md",
        "agents/qa-specialist.md",
        "agents/doc-scribe.md",
        "agents/challenger.md",
        "agents/perf-optimizer.md",
        "agents/solution-architect.md",
    ],
)
def test_reuse_preserves_failure_and_source_inspection(relative: str) -> None:
    """A complete-looking sibling/legacy flag must not erase failure, staleness or source work.

    Every specialist that reuses a supplied Codemap answer must keep the reuse gate: only a successful, fresh, matching
    answer is reused, and a valid empty list or an incomplete result still falls back to source inspection.
    """
    clauses = [
        "only when `query_complete` is absent",
        "`result.index`",
        "`ok: false`",
        "valid empty",
        "source-body",
        "same project",
        "current index",
        "query and flags",
        "`stale`",
    ]
    text = _flat(relative)
    assert [clause for clause in clauses if clause not in text] == []


def test_qa_separates_static_relationships_and_measurements() -> None:
    """A mocked dependency must not count as runtime-tested implementation.

    The QA agent reads Codemap's static test-caller relationships next to measured coverage; the definition must keep
    the two apart so a static edge is never reported as executed code.
    """
    clauses = ["not proof of implementation execution", "unknown, not zero", "Module scope is exact"]
    text = _flat("agents/qa-specialist.md")
    assert [clause for clause in clauses if clause not in text] == []
