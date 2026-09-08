"""Regression checks for terminal PR-evidence collection failures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"
BEHAVIORAL_CASES = PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json"


def _terminal_failure_gate() -> str:
    """Return the PR collection-failure contract without later review guidance.

    Example:
        >>> "Terminal review-unavailable output gate:" in _terminal_failure_gate()
        True
    """
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    start = skill.index("**Terminal review-unavailable output gate:**")
    end = skill.index("\n\nFor retryable `github-network`", start)
    return skill[start:end]


def test_terminal_pr_collection_failure_is_review_unavailable_not_merge_decision() -> None:
    """Keep a failed evidence collection separate from a PR review outcome.

    A T0 failure means no source review occurred. The user therefore needs a plain operational diagnostic, not a
    ``needs-more-work`` recommendation or any table that could be mistaken for PR findings.
    """
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")
    terminal_gate = _terminal_failure_gate()
    normalized = terminal_gate.lower()

    assert "t0 pr collection failure" in normalized
    assert "`pr review availability: unavailable`" in normalized
    assert "`merge decision: not made`" in normalized
    assert "plain diagnostic prose" in normalized
    assert "do not emit a markdown table" in normalized
    assert "## PR Evidence Collection Recovery" not in terminal_gate
    assert "| Operational area |" not in terminal_gate
    assert "Do not emit `needs-more-work`" in terminal_gate
    assert "neither `PR Evidence Collection Recovery` nor `Review Findings and Merge Blocks` applies" in terminal_gate
    assert "`review_status=unavailable`" in terminal_gate
    assert "`collection_failure=" in terminal_gate
    assert "For retryable `github-network`, `github-rate-limit`, or `command-timeout`" in skill
    assert "suggest filing a Codex Rig bug" in skill


@pytest.mark.installed_plugin
def test_pr_review_approves_the_complete_collector_before_terminal_network_failure() -> None:
    """Require network approval to cover the collector and its nested GitHub reads."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")

    assert "execute the complete collector command with approved external network access" in skill
    assert '`sandbox_permissions="require_escalated"`' in skill
    assert "never request a broad `python` approval prefix" in skill
    assert "A direct approval for `gh pr view` does not cover `gh` spawned by the collector" in skill
    assert "Only after that approved collector attempt fails" in skill
    assert skill.index("execute the complete collector command") < skill.index(
        "**Terminal review-unavailable output gate:**"
    )


@pytest.mark.installed_plugin
def test_pr_remediation_approves_the_complete_collector_before_terminal_network_failure() -> None:
    """Keep PR remediation from repeating the review collector's sandbox failure."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "execute the complete collector command with approved external network access" in skill
    assert '`sandbox_permissions="require_escalated"`' in skill
    assert "never request a broad `python` approval prefix" in skill
    assert "A direct approval for `gh pr view` does not cover `gh` spawned by the collector" in skill
    assert "Only after that approved collector attempt fails" in skill
    assert skill.index("execute the complete collector command") < skill.index("Findings intake:")


def test_calibration_covers_sandboxed_collector_network_approval() -> None:
    """Keep behavioral calibration aligned with the collector approval contract."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    case = cases["code-review-pr-sandboxed-collector-network-approval"]
    assert case["target"] == "code-review"
    assert case["expected_findings"] == [
        "complete-collector-network-approval-missing",
        "nested-github-read-sandboxed",
        "terminal-unavailable-before-approved-retry",
    ]
