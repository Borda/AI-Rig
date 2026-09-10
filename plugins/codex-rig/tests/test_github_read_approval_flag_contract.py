"""Regression checks for completed user authorization of required GitHub reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SHARED_CONTRACT = PLUGIN_ROOT / "shared" / "native-skill-contract.md"
BEHAVIORAL_CASES = PLUGIN_ROOT / "runtime" / "calibration" / "behavioral-cases.json"
ASSESS_SKILL = PLUGIN_ROOT / "skills" / "assess" / "SKILL.md"
RELEASE_SKILL = PLUGIN_ROOT / "skills" / "release" / "SKILL.md"


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("skill_path", "local_only_marker"),
    [
        pytest.param(ASSESS_SKILL, "`local`", id="assess"),
        pytest.param(RELEASE_SKILL, "`notes`", id="release"),
    ],
)
def test_approve_gh_is_explicit_optional_input_without_implicit_github_traffic(
    skill_path: Path,
    local_only_marker: str,
) -> None:
    """Prevent a supplied approval flag from becoming a network or scope switch."""
    skill = skill_path.read_text(encoding="utf-8")
    schema = skill.split("## Input Schema\n", 1)[1].split("\n## ", 1)[0]

    assert '"approve_gh": "optional boolean; default false' in schema
    assert "user has already approved required GitHub operations" in schema
    assert "Do not ask for another workflow confirmation" in skill
    assert "Normalize a standalone `--approve-gh` before helper parsing" in skill
    assert "Remove `--approve-gh` before invoking helpers" in skill
    assert "only direct user invocation may supply it" in skill
    assert "Repeated exact `--approve-gh` is idempotent" in skill
    assert "Reject `--approve-gh=<value>`" in skill
    assert local_only_marker in skill
    assert "does not trigger GitHub access" in skill
    assert "applies only when the normal workflow calls `github_read.py`" in skill


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    "skill_path",
    [
        pytest.param(ASSESS_SKILL, id="assess"),
        pytest.param(RELEASE_SKILL, id="release"),
    ],
)
def test_approve_gh_keeps_runtime_and_remote_mutation_boundaries(skill_path: Path) -> None:
    """Prevent reusable reader intent from bypassing approval or authorizing publication."""
    skill = skill_path.read_text(encoding="utf-8")

    assert "Do not create or modify runtime approval rules files" in skill
    assert "does not bypass runtime approval" in skill
    assert "does not authorize remote publication" in skill
    assert "denial stops the current attempt" in skill


def test_assess_prs_keep_the_collector_preapproval_boundary() -> None:
    """Prevent generic reader reuse from replacing PR-specific collector approval."""
    skill = ASSESS_SKILL.read_text(encoding="utf-8")

    assert "native-skill-contract.md#pr-collection-preapproval" in skill
    assert "PR collection uses `collect_pr.py` only" in skill


def test_shared_contract_declares_reader_wide_reuse_when_current_cli_cannot_scope_it() -> None:
    """Prevent a false resource-scoped promise from a prefix preceding dynamic output."""
    contract = SHARED_CONTRACT.read_text(encoding="utf-8")
    preapproval = contract.split("## GitHub Reader Preapproval\n", 1)[1].split("\n## ", 1)[0]

    assert "actual Python executable, absolute installed `github_read.py` path" in preapproval
    assert "dynamic `--out` after prefix" in preapproval
    assert "prevents resource-scoped reuse" in preapproval
    assert "reader-wide across repositories" in preapproval
    assert "allowlisted local PR checkout" in preapproval
    assert "same scope as existing Sync managed-reader rules" in preapproval
    assert "never request a bare `gh` or `python` prefix" in preapproval
    assert '`sandbox_permissions="require_escalated"`' in preapproval
    assert "runtime UI owns saved approval" in preapproval
    assert "denial stops" in preapproval
    assert "remote publication" in preapproval
    assert "completed user authorization" in preapproval
    assert "Do not ask for another workflow confirmation" in preapproval
    assert "Runtime permission remains separate" in preapproval
    assert "Do not wrap this command in `rtk`" in preapproval
    assert "takes precedence over generic RTK routing" in preapproval


def test_calibration_covers_github_reader_preapproval_boundaries() -> None:
    """Keep reader-wide approval and local-only behavior in offline calibration."""
    payload = json.loads(BEHAVIORAL_CASES.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in payload["cases"]}

    assess_case = cases["assess-github-reader-preapproval-boundary"]
    assert assess_case["target"] == "assess"
    assert assess_case["expected_findings"] == [
        "github-reader-preapproval-runtime-bypass",
        "github-reader-preapproval-resource-scope-overclaim",
        "github-reader-preapproval-untrusted-flag",
        "github-reader-preapproval-local-traffic",
        "github-reader-preapproval-pr-collector-contract-missing",
    ]

    release_case = cases["release-github-reader-preapproval-boundary"]
    assert release_case["target"] == "release"
    assert release_case["expected_findings"] == [
        "github-reader-preapproval-bare-prefix",
        "github-reader-preapproval-rules-write",
        "github-reader-preapproval-runtime-bypass",
        "github-reader-preapproval-remote-publication",
    ]
