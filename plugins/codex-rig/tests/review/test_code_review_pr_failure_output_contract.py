"""Regression checks for terminal PR-evidence collection failures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
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
@pytest.mark.parametrize(
    ("skill_path", "denial_stop", "later_section"),
    [
        pytest.param(
            CODE_REVIEW_SKILL,
            "An explicit runtime denial or non-overridable restriction stops the collection attempt",
            "**Terminal review-unavailable output gate:**",
            id="review",
        ),
        pytest.param(
            CODE_REMEDIATE_SKILL,
            "An explicit runtime denial or non-overridable restriction stops",
            "Findings intake:",
            id="remediation",
        ),
    ],
)
def test_pr_workflow_uses_effective_grants_before_terminal_network_failure(
    skill_path: Path, denial_stop: str, later_section: str
) -> None:
    """Use current permissions for PR collection before an unavailable result or findings intake.

    Review collects under its effective grants before reporting unavailability, and remediation keeps its collector from
    escalating allowed reads before findings intake.
    """
    skill = skill_path.read_text(encoding="utf-8")

    assert (
        "Run the direct owning collector under current effective grants per GitHub Read Execution or with runtime approval"
        in skill
    )
    assert denial_stop in skill
    assert "without broadening access or retrying the denied command" in skill
    assert skill.index("Run the direct owning collector") < skill.index(later_section)


@pytest.mark.parametrize(
    ("case_id", "expected_findings"),
    [
        pytest.param(
            "code-review-pr-profile-not-installed",
            [
                "plugin-add-assumed-profile-install",
                "profile-auto-installed-from-skill",
                "host-restriction-retried",
            ],
            id="missing-pr-profile",
        ),
        pytest.param(
            "code-review-pr-dirty-worktree-precision",
            ["unrelated-dirty-worktree-false-blocker", "dirty-worktree-overlap-reason-missing"],
            id="dirty-worktree-checkout-precision",
        ),
    ],
)
def test_calibration_covers_pr_checkout_and_profile_cases(case_id: str, expected_findings: list[str]) -> None:
    """Keep behavioral calibration aligned with explicit profile installation and precise dirty-worktree diagnostics.

    The missing-profile case covers explicit profile installation, and the dirty-worktree case keeps unrelated dirty
    files and overwrite-risk diagnostics distinct.
    """
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    case = cases[case_id]
    assert case["target"] == "code-review"
    assert case["expected_findings"] == expected_findings
