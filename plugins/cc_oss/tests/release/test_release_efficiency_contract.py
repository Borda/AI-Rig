"""Guard the oss:release efficiency changes without losing any user ask the committed baseline flow had.

Measured release runs spent ~19 task calls and ~2.3 polling calls (``ScheduleWakeup``, ``ListAgents``) per run. These
tests pin the riding/no-polling rules and every baseline ask, mapped by id, so a future removal fails.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

_RELEASE = Path(__file__).resolve().parents[2] / "skills/release"

#: (file, marker that only exists while the baseline question is still asked there)
_ASKS = [
    pytest.param("SKILL.md", "**Approve the proposed classification**", id="classification-approval-kept"),
    pytest.param("SKILL.md", "**Unknown flags**: if any", id="unknown-flags-kept"),
    pytest.param("SKILL.md", "No stable tags found. Range base is initial commit", id="no-stable-tags-kept"),
    pytest.param("SKILL.md", "Ready to run demo script `$DEMO_OUT`?", id="demo-run-kept"),
    pytest.param("SKILL.md", "Demo still failing after 3 attempts", id="demo-failure-kept"),
    pytest.param("modes/demo.md", "invoke `AskUserQuestion` with `## Demo attempts` log", id="demo-assets-kept"),
    pytest.param(
        "modes/release-draft-template.md",
        "DRAFT.md already exists — overwrite, append, or abort?",
        id="draft-overwrite-kept",
    ),
    pytest.param(
        "modes/release-draft-template.md", "Ready to prepend to `$CHANGELOG_FILE`?", id="changelog-prepend-kept"
    ),
    pytest.param(
        "modes/classify-truth-check.md",
        "proposed final category through `AskUserQuestion`",
        id="breaking-promotion-kept",
    ),
    pytest.param(
        "templates/audit-checks.md",
        "pip-audit not installed — CVE dependency scan will be skipped",
        id="pip-audit-kept",
    ),
]


@functools.cache
def _skill() -> str:
    """Read the release skill once."""
    return (_RELEASE / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(("relative", "marker"), _ASKS)
def test_baseline_ask_kept_next_to_tool_name(relative: str, marker: str) -> None:
    """Every question the baseline release flow asked is still asked, on the same line as the tool name.

    No release ask moved: each depends on information (draft, demo result, classification evidence) that exists only
    at its own step, so front-loading would change what the user sees when answering. The check looks one line either
    side of the marker, so it does not depend on how a paragraph is wrapped.
    """
    lines = (_RELEASE / relative).read_text(encoding="utf-8").splitlines()
    hits = [number for number, line in enumerate(lines) if marker in line]
    assert hits, marker
    assert "AskUserQuestion" in "\n".join(lines[max(0, hits[0] - 1) : hits[0] + 2])


@pytest.mark.parametrize(
    "rule",
    [
        "**Zero bookkeeping-only turns**",
        "Never `ScheduleWakeup`, `ListAgents`, a `Monitor` loop, a `sleep` or a no-op call to wait.",
        'agent_watch.py" --state-dir "${TMPDIR:-/tmp}/release-setup-${CSID}"',
        "A ⏱ only informs; it never answers or skips a user question.",
        "git_state_snapshot.py",
    ],
)
def test_efficiency_rule_stated(rule: str) -> None:
    """Riding bookkeeping, deadline-based agent waits and one-call state checks are stated in the skill.

    Dropping any of these lines brings back the per-run polling and bookkeeping-only turns the rules removed.
    """
    assert rule in _skill()


@pytest.mark.parametrize("batch", ["gather", "changelog-audit", "adversarial", "shepherd"])
def test_spawn_batch_named(batch: str) -> None:
    """Each release spawn has a named watch batch, so no agent wait falls back to polling.

    An unnamed batch has no deadline file, which is what lets an agent wait degrade into a poll loop.
    """
    assert f"`{batch}`" in _skill()
