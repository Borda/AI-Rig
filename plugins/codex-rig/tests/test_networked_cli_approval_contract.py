"""Regression checks for intentionally networked CLI approval ownership."""

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
GLOBAL_INSTRUCTIONS = PLUGIN_ROOT / "assets" / "AGENTS.md"
BEHAVIORAL_CASES = PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json"
BEHAVIORAL_OBSERVATIONS = PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-observations.jsonl"
BENCHMARKS = PLUGIN_ROOT / "runtime" / "calibration" / "benchmarks.json"
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
        pytest.param("calibrate", "run_live_ab.py", id="calibrate"),
        pytest.param("kaggle", "kaggle competitions list -p 1", id="kaggle"),
        pytest.param("sync", "codex plugin marketplace upgrade", id="sync"),
    ],
)
def test_networked_cli_skills_require_complete_owning_command_approval(
    skill_name: str,
    network_marker: str,
) -> None:
    """Keep networked CLI outside the GitHub profile behind owning-command approval."""
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


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("skill_name", "helper"),
    [
        pytest.param("assess", "github_read.py", id="assess"),
        pytest.param("code-remediate", "collect_pr.py", id="code-remediate"),
        pytest.param("code-review", "collect_pr.py", id="code-review"),
        pytest.param("release", "github_read.py", id="release"),
    ],
)
def test_github_reads_use_profile_or_owning_command_approval(skill_name: str, helper: str) -> None:
    """Keep audited GitHub reads under either supported runtime permission boundary."""
    skill = (PLUGIN_ROOT / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")

    assert helper in skill
    assert "active opted-in `github-read` profile or request runtime approval for the complete owning command" in skill
    assert "native-skill-contract.md#github-read-execution" in skill
    assert "Runtime denial stops" in skill or "runtime restriction or denial stops" in skill


def test_user_questions_expose_answers_without_weakening_authorization() -> None:
    """Keep answer formats explicit while preserving native permission boundaries."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    question_guide = SHARED_CONTRACT.with_name("codex-user-questions.md")
    assert "[Codex User Questions](codex-user-questions.md)" in contract
    assert "codex-user-questions-details.md" in question_guide.read_text(encoding="utf-8")
    questions = SHARED_CONTRACT.with_name("codex-user-questions-details.md").read_text(encoding="utf-8")
    skill = CODE_REMEDIATE_SKILL.read_text(encoding="utf-8")

    assert "`Approve local merge` / `Deny local merge`" in questions
    assert "all feasible choices" in questions
    assert "states the expected value or format" in questions
    assert "Use the actual tool schema" in questions
    assert "Runtime permission requests use the dedicated runtime mechanism" in questions
    assert "do not duplicate them in prose" in questions
    assert "Do not re-ask a decision already supplied" in questions
    assert "Silence, skip, timeout, preselection, an empty result, an example answer" in questions
    assert "unrelated text grants no consent" in questions
    assert "`Authorize this local merge and commit?` with separate canonical options `Approve` and `Deny`" in skill
    assert "Generated repair questions follow the same native routing as the merge question" in skill
    assert "(approve / revise / parent-only)" in skill


def test_shared_contract_covers_known_networked_cli_families() -> None:
    """Prevent a new workflow from narrowing approval to a nested executable."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")

    assert "Networked CLI Approval" in contract
    assert "complete owning command" in contract
    assert '`sandbox_permissions="require_escalated"`' in contract
    assert "Never enable broader persistent workspace network access" in contract
    for marker in ("`gh`", "`kaggle`", "`git fetch`", "Codex Git marketplace", "`codex exec`"):
        assert marker in contract
    assert "GitHub reads through `github_read.py` and PR collection through `collect_pr.py`" in contract
    assert (
        "follow [GitHub Read Execution](#github-read-execution) under the selected session profile or an approved owning-command boundary"
        in contract
    )
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


def test_github_profile_has_behavioral_coverage() -> None:
    """Keep explicit profile setup and stricter host denial in calibration."""
    cases = {case["id"]: case for case in json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))["cases"]}
    installation = cases["sync-github-read-profile-installation"]
    denial = cases["code-review-github-read-host-denial"]

    assert installation["target"] == "sync"
    assert "profile-installed-from-skill" in installation["expected_findings"]
    assert "profile-domain-widening" in installation["expected_findings"]
    assert denial["target"] == "code-review"
    assert denial["expected_findings"] == [
        "github-read-runtime-escalation",
        "host-denial-retried",
        "broader-command-after-denial",
        "denial-outcome-misreported",
    ]


@pytest.mark.installed_plugin
def test_no_approval_cases_require_an_active_profile_in_the_current_session() -> None:
    """Reject fixture prompts that mistake installed profile availability for selection."""
    cases = {case["id"]: case for case in json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))["cases"]}

    for case_id in (
        "code-review-github-read-no-approval-escalation",
        "code-review-github-read-profile-no-extra-approval",
    ):
        prompt = cases[case_id]["prompt"]
        assert "current fresh session" in prompt
        assert "selected and loaded" in prompt


def test_unprofiled_github_reads_have_approval_and_denial_coverage() -> None:
    """Keep ordinary-session collector and reader approvals distinct from profile access."""
    cases = {case["id"]: case for case in json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))["cases"]}
    observations = {
        row["case_id"]: row
        for line in BEHAVIORAL_OBSERVATIONS.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))["source"] == "fixture-selftest"
    }

    for case_id, target, helper in (
        ("code-review-github-unprofiled-collector-approved", "code-review", "collect_pr.py"),
        ("assess-github-unprofiled-reader-approved", "assess", "github_read.py"),
        ("code-review-github-unprofiled-denial-stops", "code-review", "collect_pr.py"),
    ):
        case = cases[case_id]
        assert case["target"] == target
        assert helper in case["prompt"]
        assert "no active `github-read` profile" in case["prompt"]
        assert case["expected_findings"] == []
        assert observations[case_id]["reported_findings"] == []

    missing = cases["code-review-github-unprofiled-collector-approval-missing"]
    assert "complete-collector-network-approval-missing" in missing["expected_findings"]
    assert observations[missing["id"]]["reported_findings"] == missing["expected_findings"]


def test_profile_calibration_keeps_collection_failure_and_current_parent_route() -> None:
    """Reject scope selection after failed intake and obsolete delegation model targets."""
    cases = {case["id"]: case for case in json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))["cases"]}
    benchmarks = json.loads(BENCHMARKS.read_text(encoding="utf-8"))
    clean = cases["code-remediate-github-read-profile-routes-request"]["prompt"]
    patterns = benchmarks["agents"]["delegation-lead"]

    assert "collection as incomplete" in clean
    assert "scope question" not in clean
    assert "gpt-6-luna" in patterns
    assert "gpt-6-sol" in patterns
    assert all("gpt-5.6" not in pattern for pattern in patterns)
    for case_id in ("explicit-sol-automatic-route-rejected", "explicit-sol-advisory-boundary"):
        case = cases[case_id]
        assert "Terra" not in case["prompt"]
        assert "terra-parent" not in " ".join(case["expected_findings"])


@pytest.mark.installed_plugin
def test_pr_collector_uses_profile_and_stops_on_denial() -> None:
    """Keep PR collection direct while rejecting a retry after host denial."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    boundary = contract.split("## PR Collection Runtime Boundary\n", 1)[1].split("\n## ", 1)[0]

    assert "Run the owning collector directly" in boundary
    assert "An unexpected restriction or denial stops collection" in boundary
    assert "without broadening access or retrying the denied command" in boundary
    assert "Collection does not authorize remote mutation" in boundary


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
        "github-read-conflated-with-runtime-approval",
    ]


def test_sync_rejects_broad_codex_approval_prefix() -> None:
    """Keep marketplace lifecycle approval narrower than the whole Codex CLI."""
    sync_skill = (PLUGIN_ROOT / "skills" / "sync" / "SKILL.md").read_text(encoding="utf-8")

    assert "never request a broad `codex` approval prefix" in sync_skill
