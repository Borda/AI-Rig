"""Keep specialist Codemap reuse bounded to successful, fresh, matching answers."""

from pathlib import Path

import pytest


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
def test_codemap_reuse_preserves_failure_and_source_inspection(relative: str) -> None:
    """A complete-looking sibling/legacy flag must not erase failure, staleness or source work."""
    text = (Path(__file__).resolve().parents[1] / relative).read_text(encoding="utf-8")
    for clause in (
        "only when `query_complete` is absent",
        "`result.index`",
        "`ok: false`",
        "valid empty",
        "source-body",
        "same project",
        "current index",
        "query and flags",
        "`stale`",
    ):
        assert clause in text


def test_qa_separates_static_relationships_and_measurements() -> None:
    """A mocked dependency must not count as runtime-tested implementation."""
    text = (Path(__file__).resolve().parents[1] / "agents/qa-specialist.md").read_text(encoding="utf-8")
    assert "not proof of implementation execution" in text
    assert "unknown, not zero" in text
    assert "Module scope is exact" in text
