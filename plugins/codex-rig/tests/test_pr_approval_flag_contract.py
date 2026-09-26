"""Check PR collection uses runtime permission without invocation approval flags."""

from __future__ import annotations

from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


@pytest.mark.installed_plugin
@pytest.mark.parametrize("skill", ["code-review", "code-remediate", "assess", "release"])
def test_github_workflows_use_opted_in_profile_without_flag(skill: str) -> None:
    """Prevent skill inputs or prose from restoring invocation-level read approval."""
    content = (PLUGIN_ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")

    assert "--approve-gh" not in content
    assert "approve_gh" not in content
    assert (
        "active opted-in `github-read` profile or request runtime approval for the complete owning command" in content
    )
    assert "native-skill-contract.md#github-read-execution" in content


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "skill_path", [pytest.param(CODE_REVIEW_SKILL, id="review"), pytest.param(CODE_REMEDIATE_SKILL, id="remediate")]
)
def test_pr_skills_use_collector_runtime_boundary(skill_path: Path) -> None:
    """Keep collector execution and remediation scope distinct from read permission."""
    skill = skill_path.read_text(encoding="utf-8")

    assert "native-skill-contract.md#pr-collection-runtime-boundary" in skill
    assert "Do not create or modify runtime approval rules files" in skill
    assert "runtime restriction or denial stops the collection attempt" in skill
    if skill_path == CODE_REMEDIATE_SKILL:
        assert "Continue normal remediation scope selection" in skill
    else:
        assert "scope=pr" in skill


@pytest.mark.installed_plugin
def test_pr_collector_binds_canonical_target_before_runtime_approval() -> None:
    """Prevent a reusable numeric target from reading another repository's PR."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    boundary = contract.split("## PR Collection Runtime Boundary\n", 1)[1].split("\n## ", 1)[0]

    assert "Before the first `collect_pr.py` invocation" in boundary
    assert "use the one valid GitHub repository configured as `origin`" in boundary
    assert "if `origin` is absent, use the sole configured GitHub repository" in boundary
    assert "A user-supplied canonical URL takes precedence over `origin`" in boundary
    assert "canonical GitHub PR URL" in boundary
    assert "Replace the numeric target with that locally bound canonical URL" in boundary
    assert "Never pass numeric user input as the collector target after canonicalization" in boundary
    assert (
        'otherwise give the required brief and request external access for the complete collector with `sandbox_permissions="require_escalated"`'
        in boundary
    )
    assert "An unexpected restriction or denial stops" in boundary
    assert "remote mutation" in boundary
    assert "--approve-gh" not in boundary


@pytest.mark.installed_plugin
def test_shared_read_execution_has_no_workflow_consent_gate() -> None:
    """Prevent a conversational prompt from blocking an otherwise allowed read."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    execution = contract.split("## GitHub Read Execution\n", 1)[1].split("\n## ", 1)[0]

    assert "opted-in `github-read` session" in execution
    assert "need no separate workflow consent" in execution
    assert "unexpected restriction or denial stops the attempt" in execution
    assert "request runtime approval for the complete owning helper" in execution
    assert "Local-only work does not trigger GitHub access" in execution
    assert "remote mutation" in execution
    assert "--approve-gh" not in execution
