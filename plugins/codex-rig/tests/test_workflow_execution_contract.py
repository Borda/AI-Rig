"""Keep routine workflow execution and recoverable command errors under existing authority."""

from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_local_workflow_commands_use_effective_permissions() -> None:
    """Prevent local helpers, tests, and Git writes from inventing new approval requirements."""
    contract = (PLUGIN_ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    section = contract.split("## Authorized Workflow Execution", 1)[1].split("\n## ", 1)[0]
    for required in (
        "all Codex skills",
        "`use_default`",
        "`git add`",
        "`git commit`",
        "`.git`",
        "packaged helpers",
        "configured checks",
        "current effective permissions",
        "do not infer a restriction from a command being state-changing",
        "does not grant arbitrary unsandboxed shell",
        "existing authorization",
    ):
        assert required in section


@pytest.mark.parametrize("relative", ["shared/commit-response-template.md", "assets/AGENTS.md"])
def test_commit_recovery_keeps_authority_and_checks_state(relative: str) -> None:
    """Require an evidence-bound parse repair without authorizing replay after a real denial."""
    contract = " ".join((PLUGIN_ROOT / relative).read_text(encoding="utf-8").split())
    for required in (
        "pre-execution syntax failure",
        "HEAD",
        "index",
        "existing authorization",
        "denial",
    ):
        assert required in contract
    assert "On denial, failure, or mismatch, do not retry automatically" not in contract


def test_commit_transport_requires_syntax_check_and_bounded_recovery() -> None:
    """Catch quoting mistakes before Git and keep recovery bounded to the original reviewed commit."""
    contract = " ".join((PLUGIN_ROOT / "shared/commit-response-template.md").read_text(encoding="utf-8").split())
    for required in (
        "`shlex.join`",
        "`bash -n`",
        "same reviewed paths and message",
        "one corrected attempt",
        "do not create a duplicate commit",
        "continue independent authorized checks",
    ):
        assert required in contract


def test_required_gate_and_review_do_not_invent_consent_holds() -> None:
    """Keep required checks authorized while retaining real runtime capability boundaries."""
    contract = (PLUGIN_ROOT / "shared/native-skill-contract.md").read_text(encoding="utf-8")
    skill = (PLUGIN_ROOT / "skills/code-remediate/SKILL.md").read_text(encoding="utf-8")
    assert "Runnable plugin code blocks are preapproved workflow recipes" in contract
    assert "Existing grants or an applicable preapproval satisfy that capability" in contract
    assert "A timestamp-specific whole-command prefix is not reusable approval" in contract
    assert "`run_gates.py` and its configured checks are required authorized workflow work" in contract
    assert "`+review` selects report intake; it does not forbid" in skill
    assert "Never use `--skip-review` solely because" in skill


def test_git_reads_and_local_configuration_reuse_authorization() -> None:
    """Prevent harmless remote reads and local settings from being mistaken for remote publication."""
    policy = (PLUGIN_ROOT / "assets/AGENTS.md").read_text(encoding="utf-8")
    git = policy.split("- `git` CLI", 1)[1].split("- Never run `git`/`gh`", 1)[0]
    for authorized in (
        "task-scoped local repository operations and read-only remote access",
        "`ls-remote`",
        "`clone`",
        "`fetch`",
        "`remote update`",
        "`submodule update --remote`",
        "local config, upstream, and tracking changes",
        "existing grants or the configured native automatic approval reviewer",
        "only a verified missing capability",
    ):
        assert authorized in git
    assert "no push, clone, remote update, ls-remote" not in git
    assert "it still needs owning command's network approval" not in git
    for boundary in (
        "Never use `git` for remote repository mutation",
        "no push",
        "authorized `git pull --ff-only`",
        "verified upstream and confirmed clean index/worktree",
        "Do not merge, rebase, reset, discard user changes, or manually change tracking configuration merely to refresh",
        "PR-specific verified checkout and recovery protections",
    ):
        assert boundary in git
    assert "Destructive operations retain action-specific consent" in git
    assert "A denial or stricter host restriction" in policy
    assert "Never run `git`/`gh` with `--force`, `--force-with-lease`" in policy
    marketplace = (PLUGIN_ROOT / "skills/sync/SKILL.md").read_text(encoding="utf-8")
    assert "Never use `git clone`, edit marketplace configuration" in marketplace


@pytest.mark.parametrize("relative", ["shared/native-skill-contract.md", "skills/sync/SKILL.md"])
def test_setup_guidance_names_current_generated_default(relative: str) -> None:
    """Prevent callers from expecting the retired generated workspace-only profile."""
    text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
    assert "`local-workflow` when no default exists" in text
    assert "supplying `:workspace` when no default exists" not in text
    assert 'adds `default_permissions = ":workspace"` when no default exists' not in text
