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
    assert "Start with a plain-English explanation of the stopped operation and its effect" in terminal_gate
    assert "`Reason:` with the classified failure before verification" in terminal_gate
    assert "worktree-preflight.json" in terminal_gate
    assert "invoking-worktree edits are not checkout overlap" in terminal_gate
    assert "review-worktree creation or source-state failure" in terminal_gate
    assert "For retryable `github-network`, `github-rate-limit`, or `command-timeout`" in skill
    assert "suggest filing a Codex Rig bug" in skill


@pytest.mark.installed_plugin
def test_pr_review_uses_active_profile_before_terminal_network_failure() -> None:
    """Keep PR collection inside the installed profile before an unavailable result."""
    skill = CODE_REVIEW_SKILL.read_text(encoding="utf-8")

    assert (
        "Run the direct owning collector under an active opted-in `github-read` profile or with runtime approval"
        in skill
    )
    assert "An unexpected runtime restriction or denial stops the collection attempt" in skill
    assert "without broadening access or retrying the denied command" in skill
    assert skill.index("Run the direct owning collector") < skill.index("**Terminal review-unavailable output gate:**")


@pytest.mark.installed_plugin
def test_pr_remediation_uses_active_profile_before_terminal_network_failure() -> None:
    """Keep PR remediation from escalating its collector's allowed reads."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert (
        "Run the direct owning collector under an active opted-in `github-read` profile or with runtime approval"
        in skill
    )
    assert "An unexpected runtime restriction or denial stops" in skill
    assert "without broadening access or retrying the denied command" in skill
    assert skill.index("Run the direct owning collector") < skill.index("Findings intake:")


def test_calibration_covers_missing_pr_profile() -> None:
    """Keep behavioral calibration aligned with explicit profile installation."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    case = cases["code-review-pr-profile-not-installed"]
    assert case["target"] == "code-review"
    assert case["expected_findings"] == [
        "plugin-add-assumed-profile-install",
        "profile-auto-installed-from-skill",
        "host-restriction-retried",
    ]


def test_calibration_covers_dirty_worktree_checkout_precision() -> None:
    """Keep unrelated dirty files and overwrite-risk diagnostics distinct."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    case = cases["code-review-pr-dirty-worktree-precision"]
    assert case["target"] == "code-review"
    assert case["expected_findings"] == [
        "unrelated-dirty-worktree-false-blocker",
        "dirty-worktree-overlap-reason-missing",
    ]
