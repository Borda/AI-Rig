"""Regression checks for intentionally networked CLI approval ownership."""

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
GLOBAL_INSTRUCTIONS = PLUGIN_ROOT / "assets" / "AGENTS.md"
BEHAVIORAL_CASES = PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json"
CODE_REVIEW_SKILL = PLUGIN_ROOT / "skills" / "code-review" / "SKILL.md"
CODE_REMEDIATE_SKILL = PLUGIN_ROOT / "skills" / "code-remediate" / "SKILL.md"


APPROVAL_BRIEF_FIELDS = (
    "Action and purpose",
    "External capability",
    "Credential behavior",
    "Filesystem and worktree effects",
    "Retry policy and safe denial outcome",
)


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("skill_name", "network_marker"),
    [
        pytest.param("change-analysis", "github_read.py", id="change-analysis"),
        pytest.param("calibrate", "run_live_ab.py", id="calibrate"),
        pytest.param("code-remediate", "collect_pr.py", id="code-remediate"),
        pytest.param("code-review", "collect_pr.py", id="code-review"),
        pytest.param("kaggle", "kaggle competitions list -p 1", id="kaggle"),
        pytest.param("release", "github_read.py", id="release"),
        pytest.param("sync", "codex plugin marketplace upgrade", id="sync"),
    ],
)
def test_networked_cli_skills_require_complete_owning_command_approval(
    skill_name: str,
    network_marker: str,
) -> None:
    """Keep every designed shell-network path behind one owning-command approval."""
    skill = (PLUGIN_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert network_marker in skill
    assert "complete owning command" in skill or "complete collector command" in skill
    assert "native-skill-contract.md" in skill
    approval_paragraphs = [paragraph for paragraph in skill.split("\n\n") if "Action and purpose" in paragraph]

    assert len(approval_paragraphs) == 1
    approval_paragraph = approval_paragraphs[0]
    for field in APPROVAL_BRIEF_FIELDS:
        assert field in approval_paragraph
    assert "denial" in approval_paragraph.lower()


def test_user_questions_expose_answers_without_weakening_authorization() -> None:
    """Keep answer formats explicit while preserving native permission boundaries."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    questions = contract.split("## User Questions\n", 1)[1].split("\n## ", 1)[0]
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "Authorize this local merge and commit? (yes / no)" in questions
    assert "show all supported choices" in questions
    assert "state the expected value or format" in questions
    assert "use the actual supported options in that control" in questions
    assert (
        "Do not add a conflicting yes/no suffix, duplicate its choices in another prompt, "
        "or imply that a chat answer bypasses runtime approval."
    ) in questions
    assert "Do not re-ask a decision already supplied" in questions
    assert "Never treat silence, a preselected option, an example answer, or an unrelated reply as consent" in questions
    assert "Authorize this local merge and commit? (yes / no)" in skill
    assert "(approve / revise / parent-only)" in skill


def test_shared_contract_covers_known_networked_cli_families() -> None:
    """Prevent a new workflow from narrowing approval to a nested executable."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")

    assert "Networked CLI Approval" in contract
    assert "complete owning command" in contract
    assert '`sandbox_permissions="require_escalated"`' in contract
    assert "never enable persistent workspace network access" in contract.lower()
    for marker in ("`gh`", "`kaggle`", "`git fetch`", "Codex Git marketplace", "`codex exec`"):
        assert marker in contract
    assert "marketplace add/upgrade" in contract
    assert "`codex plugin add` from a configured marketplace snapshot" in contract


def test_shared_contract_defines_approval_brief_and_denial_turn_recovery() -> None:
    """Require a complete, plugin-owned brief before intentional approval requests."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")

    assert "## Approval Brief" in contract
    for field in APPROVAL_BRIEF_FIELDS:
        assert field in contract
    assert "Denial aborts the active tool call and may end the assistant turn" in contract
    assert "Do not issue an equivalent approval request in the current turn" in contract
    assert "Do not switch to a broader command" in contract
    assert "Ask the user to send a new message to resume" in contract


@pytest.mark.parametrize(
    ("skill_path", "start_marker", "end_marker"),
    [
        pytest.param(
            CODE_REVIEW_SKILL, "In runtimes with network sandboxing", "\n\nPR evidence has two tiers.", id="code-review"
        ),
        pytest.param(
            CODE_REMEDIATE_SKILL, "In runtimes with network sandboxing", "\n\n`github_read.py`", id="code-remediate"
        ),
    ],
)
def test_pr_collector_owning_boundary_explains_approval_brief_and_denial_recovery(
    skill_path: Path,
    start_marker: str,
    end_marker: str,
) -> None:
    """Keep PR collector approval guidance usable at the request boundary."""
    skill = skill_path.read_text(encoding="utf-8")
    start = skill.index(start_marker)
    end = skill.index(end_marker, start)
    approval_boundary = skill[start:end]

    assert "outer collector command" in approval_boundary
    for field in APPROVAL_BRIEF_FIELDS:
        assert field in approval_boundary
    assert "Apply the other shared runtime and denial boundaries" in approval_boundary
    assert "before any user approval request or denial" in approval_boundary
    assert "after the user denies approval" in approval_boundary
    assert "retry is forbidden" in approval_boundary


def test_missing_kaggle_cli_remains_user_owned_setup() -> None:
    """Never turn a missing optional CLI into a workflow-owned installation."""
    kaggle_skill = (PLUGIN_ROOT / "skills" / "kaggle" / "SKILL.md").read_text(encoding="utf-8")
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    instructions = GLOBAL_INSTRUCTIONS.read_text(encoding="utf-8")
    kaggle_policy = kaggle_skill.lower()
    assert "do not install it" in kaggle_policy
    assert "ask the user to install and authenticate" in kaggle_policy
    for text in (kaggle_skill, contract, instructions):
        assert "pip install kaggle" not in text
        assert "approved Kaggle package installation" not in text
        assert "approved package installation" not in text
        assert "approved package-install" not in text


def test_shipped_global_instructions_keep_network_blocked_by_default() -> None:
    """Keep the installed agent policy aligned with the skill-level contract."""
    instructions = GLOBAL_INSTRUCTIONS.read_text(encoding="utf-8")

    assert "complete owning command" in instructions
    assert '`sandbox_permissions="require_escalated"`' in instructions
    assert "never enable persistent workspace network access" in instructions


def test_calibration_rejects_persistent_or_nested_only_network_approval() -> None:
    """Keep the generalized approval contract in behavioral calibration."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    case = cases["implement-networked-cli-owning-command-approval"]
    assert case["target"] == "implement"
    assert case["expected_findings"] == [
        "complete-owning-command-network-approval-missing",
        "missing-kaggle-cli-setup-not-user-owned",
        "persistent-workspace-network-enabled",
        "nested-network-cli-approval-scope-invalid",
    ]


def test_calibration_covers_approval_brief_and_denial_turn_recovery() -> None:
    """Keep calibration aligned with the reproduced host-denial clarity gap."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    case = cases["code-review-approval-denial-turn-recovery"]
    assert case["target"] == "code-review"
    assert case["expected_findings"] == [
        "approval-brief-missing",
        "denial-turn-abort-guidance-missing",
        "safe-new-message-resume-missing",
        "equivalent-approval-reprompt",
        "broader-command-after-denial",
        "denial-outcome-misreported",
    ]


def test_sync_rejects_broad_codex_approval_prefix() -> None:
    """Keep marketplace lifecycle approval narrower than the whole Codex CLI."""
    sync_skill = (PLUGIN_ROOT / "skills" / "sync" / "SKILL.md").read_text(encoding="utf-8")

    assert "never request a broad `codex` approval prefix" in sync_skill
