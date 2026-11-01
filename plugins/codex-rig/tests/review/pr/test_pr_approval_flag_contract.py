"""Check PR collection uses runtime permission without invocation approval flags."""

from __future__ import annotations

from pathlib import Path

import pytest
from _contract_text import h2_section
from _skill_text import flat, read_with_references

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
# Skills whose instructions are split across SKILL.md and trigger-loaded reference files.
SKILL_REFERENCES = {
    "code-review": ("source-and-scope.md",),
    "code-remediate": ("intake.md", "selection.md", "implementation.md", "completion.md"),
}


def _skill(name: str) -> str:
    """Return a skill's SKILL.md joined with its trigger-loaded references, whitespace collapsed."""
    skill_md = PLUGIN_ROOT / "skills" / name / "SKILL.md"
    return flat(read_with_references(skill_md, *SKILL_REFERENCES.get(name, ())))


@pytest.fixture(scope="module")
def contract_text() -> str:
    """Shared native-skill contract with its original line structure, for section lookup."""
    return SHARED_CONTRACT.read_text(encoding="utf-8")


@pytest.mark.installed_plugin
@pytest.mark.parametrize("skill", ["code-review", "code-remediate", "assess", "release"])
def test_github_workflows_use_opted_in_profile_without_flag(skill: str) -> None:
    """No GitHub workflow restores an invocation-level read approval flag.

    Each skill relies on the current effective grants and links the shared read execution rules; neither the `--approve-
    gh` flag nor an `approve_gh` input may reappear in its instructions.
    """
    content = _skill(skill)

    assert "--approve-gh" not in content
    assert "approve_gh" not in content
    assert "current effective" in content
    assert "native-skill-contract.md#github-read-execution" in content


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("skill", "scope_phrase"),
    [
        pytest.param("code-review", "scope=pr", id="review"),
        pytest.param("code-remediate", "Continue normal remediation scope selection", id="remediate"),
    ],
)
def test_pr_skills_use_collector_runtime_boundary(skill: str, scope_phrase: str) -> None:
    """Collector execution and workflow scope stay distinct from read permission.

    Both PR skills link the shared collection boundary, forbid editing runtime approval rule files and stop the attempt
    on a denial. Review then continues with `scope=pr`; remediation continues normal scope selection.
    """
    content = _skill(skill)

    assert "native-skill-contract.md#pr-collection-runtime-boundary" in content
    assert "Do not create or modify runtime approval rules files" in content
    assert "runtime denial or non-overridable restriction stops the collection attempt" in content
    assert scope_phrase in content


@pytest.mark.installed_plugin
def test_pr_collector_binds_canonical_target_before_runtime_approval(contract_text: str) -> None:
    """A reusable numeric target cannot read another repository's PR.

    Before the first collector call the target is bound to one canonical GitHub PR URL taken from `origin`, the sole
    configured repository or a user-supplied URL. The boundary then picks the default sandboxed call or the escalated
    one.
    """
    boundary = h2_section(contract_text, "PR Collection Runtime Boundary")

    assert "Before the first `collect_pr.py` invocation" in boundary
    assert "use the one valid GitHub repository configured as `origin`" in boundary
    assert "if `origin` is absent, use the sole configured GitHub repository" in boundary
    assert "A user-supplied canonical URL takes precedence over `origin`" in boundary
    assert "canonical GitHub PR URL" in boundary
    assert "Replace the numeric target with that locally bound canonical URL" in boundary
    assert "Never pass numeric user input as the collector target after canonicalization" in boundary
    assert (
        "When GitHub Read Execution establishes network and path grants, use the default sandboxed tool call"
        in boundary
    )
    assert (
        'Only when required capability is unavailable and runtime policy permits approval, give the required brief and request external access for the complete collector with `sandbox_permissions="require_escalated"`'
        in boundary
    )
    assert "An explicit denial or non-overridable restriction stops" in boundary
    assert "remote mutation" in boundary
    assert "--approve-gh" not in boundary


@pytest.mark.installed_plugin
def test_shared_read_execution_has_no_workflow_consent_gate(contract_text: str) -> None:
    """A conversational prompt cannot block an otherwise allowed GitHub read.

    With network and path grants in place the helper runs without escalation fields or separate workflow consent; only
    an explicit denial stops it, and local-only work stays local.
    """
    execution = h2_section(contract_text, "GitHub Read Execution")

    assert (
        "With network access enabled and required paths writable, omit `sandbox_permissions` and `justification`"
        in execution
    )
    assert "need no separate workflow consent" in execution
    assert "explicit denial or non-overridable restriction stops the attempt" in execution
    assert "If a required capability is explicitly disabled or a required write is outside allowed roots" in execution
    assert "use the existing owning-command approval boundary only if runtime policy permits" in execution
    assert "Local-only work stays local" in execution
    assert "remote mutation" in execution.lower()
    assert "--approve-gh" not in execution
