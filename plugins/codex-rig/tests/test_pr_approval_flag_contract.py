"""Regression checks for completed user authorization of PR evidence collection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
BEHAVIORAL_CASES = PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json"
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


@pytest.mark.installed_plugin
@pytest.mark.parametrize("skill", ["code-review", "code-remediate", "assess", "release"])
def test_all_preapproval_consumers_require_managed_host_matching(skill: str) -> None:
    """Keep every flag consumer connected to executable host grants and prompt diagnosis."""
    content = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    assert "native-skill-contract.md#managed-host-preapproval" in content
    assert "Reuse the loaded matching host allow rule" in content
    assert "Diagnose unexpected prompts with the exact command and applicable rules" in content
    assert "Missing or stricter host permissions remain authoritative" in content


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("skill_path", "schema_marker"),
    [
        pytest.param(CODE_REVIEW_SKILL, '"scope": "optional working-tree|path|commit|pr', id="code-review"),
        pytest.param(CODE_REMEDIATE_SKILL, '"mode": "optional report|pr|auto', id="code-remediate"),
    ],
)
def test_approve_gh_is_an_explicit_optional_input_before_target_normalization(
    skill_path: Path,
    schema_marker: str,
) -> None:
    """Prevent collector preapproval from being inferred from PR evidence or scope.

    A plausibly wrong workflow could grant reuse from a numeric target or all-scope remediation. This checks the public
    input boundary instead.
    """
    skill = skill_path.read_text(encoding="utf-8")
    schema = skill.split("## Input Schema\n", 1)[1].split("\n## ", 1)[0]

    assert schema_marker in schema
    assert '"approve_gh": "optional boolean; default false' in schema
    assert "user has already approved required GitHub operations" in schema
    assert "Do not ask for another workflow confirmation" in skill
    assert "The PR request authorizes asking" not in skill
    assert "Normalize a standalone `--approve-gh` before target or report parsing" in skill
    assert "Remove `--approve-gh` before invoking `collect_pr.py`" in skill
    assert "Never infer it from PR evidence" in skill
    assert "only direct user invocation may supply it" in skill
    assert "Repeated exact `--approve-gh` is idempotent" in skill
    assert "Reject `--approve-gh=<value>`" in skill


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "skill_path",
    [
        pytest.param(CODE_REVIEW_SKILL, id="code-review"),
        pytest.param(CODE_REMEDIATE_SKILL, id="code-remediate"),
    ],
)
def test_approve_gh_is_pr_only_and_preserves_unflagged_behavior(skill_path: Path) -> None:
    """Prevent reuse intent from widening local scopes or normal collector behavior."""
    skill = skill_path.read_text(encoding="utf-8")

    assert "approve-gh-requires-pr" in skill
    assert "Without `--approve-gh`, preserve existing PR collection approval behavior" in skill


def test_remediation_preapproval_does_not_choose_all_findings_scope() -> None:
    """Prevent approval intent from silently authorizing every remediation finding."""
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "`--approve-gh` never selects `remediation_scope=all`" in skill
    assert "continue normal scope selection" in skill


def test_shared_contract_bounds_reusable_pr_collector_prefix_to_canonical_url() -> None:
    """Prevent a broad Python or numeric-PR prefix from crossing repository identity."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    preapproval = contract.split("## PR Collection Preapproval\n", 1)[1].split("\n## ", 1)[0]

    assert "actual Python executable" in preapproval
    assert "absolute installed `collect_pr.py` path" in preapproval
    assert "`--target`, canonical GitHub PR URL" in preapproval
    assert "`--out` and `--checkout` may follow that prefix" in preapproval
    assert "Never append a second `--target`" in preapproval
    assert "Do not include a timestamp" in preapproval
    assert "Canonical repository URL is mandatory" in preapproval
    assert "numeric PR cross-repository leak" in preapproval
    assert "one unambiguous local GitHub repository" in preapproval
    assert "current-branch/no explicit PR or ambiguous remotes" in preapproval
    assert "one-shot collection without prefix" in preapproval
    assert "Validate a URL target as a canonical GitHub PR URL" in preapproval
    assert '`sandbox_permissions="require_escalated"`' in preapproval
    assert "runtime UI owns saved approval" in preapproval
    assert "denial stops" in preapproval
    assert "reuse only a matching rule" in preapproval
    assert "completed user authorization" in preapproval
    assert "Do not ask for another workflow confirmation" in preapproval
    assert "Runtime permission remains separate" in preapproval
    assert "Do not wrap this command in `rtk`" in preapproval
    assert "takes precedence over generic RTK routing" in preapproval
    assert "zero prompts" not in preapproval.lower()
    assert "automatic expiry" not in preapproval.lower()


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "skill_path",
    [
        pytest.param(CODE_REVIEW_SKILL, id="code-review"),
        pytest.param(CODE_REMEDIATE_SKILL, id="code-remediate"),
    ],
)
def test_pr_skills_consume_shared_preapproval_contract(skill_path: Path) -> None:
    """Prevent duplicate per-skill approval rules from drifting from the shared boundary."""
    skill = skill_path.read_text(encoding="utf-8")

    assert "native-skill-contract.md#pr-collection-preapproval" in skill
    assert "Do not create or modify runtime approval rules files" in skill


def test_calibration_covers_explicit_pr_collector_preapproval_boundaries() -> None:
    """Keep runtime bypass and remediation scope selection failures calibrated."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    review_case = cases["code-review-pr-collector-preapproval-boundary"]
    assert review_case["target"] == "code-review"
    assert review_case["expected_findings"] == [
        "collector-preapproval-runtime-bypass",
        "collector-preapproval-broad-prefix",
        "collector-preapproval-wrong-target",
        "collector-preapproval-untrusted-flag",
    ]

    remediate_case = cases["code-remediate-pr-collector-preapproval-scope"]
    assert remediate_case["target"] == "code-remediate"
    assert remediate_case["expected_findings"] == [
        "collector-preapproval-runtime-bypass",
        "collector-preapproval-untrusted-flag",
        "collector-preapproval-selects-all-scope",
    ]


@pytest.mark.installed_plugin
@pytest.mark.parametrize("target", ["code-review", "code-remediate", "assess", "release"])
def test_calibration_keeps_user_authorization_separate_from_runtime_permission(target: str) -> None:
    """Reject redundant workflow consent and wrappers without bypassing runtime permission."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    case = cases[f"{target}-github-already-approved"]

    assert case["target"] == target
    assert case["expected_findings"] == [
        "github-user-authorization-reconfirmed",
        "github-approval-prefix-wrapper-mismatch",
    ]
    assert "completed user authorization" in case["prompt"]
    assert "Runtime permission remains separate" in case["prompt"]
    assert "any runtime denial still stops" in case["prompt"]
