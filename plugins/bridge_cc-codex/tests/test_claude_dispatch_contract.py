"""Guard Claude dispatch instructions that protect task transport, consent, and detached ownership."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "claude-skills"


@pytest.fixture(params=["advise", "implement", "review"])
def skill_lines(request: pytest.FixtureRequest) -> list[str]:
    """Read each dispatch skill as individual instruction lines."""
    return (SKILLS / request.param / "SKILL.md").read_text(encoding="utf-8").splitlines()


def test_untrusted_task_transport_grants_write_and_forbids_shell_construction(skill_lines: list[str]) -> None:
    """An untrusted delimiter or shell token must remain file data rather than executable Bash."""
    tools = next(line for line in skill_lines if line.startswith("allowed-tools:"))
    transport = next(line for line in skill_lines if "create the scratch file with the Write tool" in line)
    assert "Write" in tools.partition(":")[2].replace(",", " ").split()
    assert "never put unauthored text in a Bash heredoc, echo or printf argument" in transport
    assert "`--task-file <path>` instead of `--task`" in transport


def test_task_scratch_stays_outside_peer_workspace(skill_lines: list[str]) -> None:
    """The peer must not own its input brief or report scratch creation as a project edit."""
    resolver = next(line for line in skill_lines if "Run this fixed resolver block" in line)
    assert "absolute selected workspace" in resolver
    assert "stop before Write or bridge dispatch" in resolver
    assert 'python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_task_path.py" --workspace "<absolute workspace>"' in skill_lines
    transport = next(line for line in skill_lines if "create the scratch file" in line)
    assert "Write tool at the printed absolute path unchanged" in transport
    assert not any("select another temporary directory" in line for line in skill_lines)


def test_task_scratch_is_removed_after_transport_returns(skill_lines: list[str]) -> None:
    """Both dispatch paths dispose of untrusted task text after the bridge has consumed it."""
    cleanup = next(line for line in skill_lines if "best effort" in line)
    assert "once the call returns" in cleanup
    assert "only if this skill created it" in cleanup
    assert 'python "${CLAUDE_PLUGIN_ROOT}/bin/bridge_task_path.py" --cleanup "<printed scratch path>"' in skill_lines
    assert "without masking the bridge result" in cleanup


def test_caller_task_file_is_passed_through_without_cleanup(skill_lines: list[str]) -> None:
    """Existing orchestrator briefs must not be retranscribed or removed by the bridge skill."""
    transport = next(line for line in skill_lines if "A `--task-file` supplied in `$ARGUMENTS`" in line)
    assert "passed through unchanged and never deleted" in transport
    assert "create and delete a scratch file only for text you move out of `--task` yourself" in transport


def test_implement_confirmation_is_user_owned_and_available() -> None:
    """An orchestrator cannot approve its own write-capable dispatch on the user's behalf."""
    lines = (SKILLS / "implement/SKILL.md").read_text(encoding="utf-8").splitlines()
    tools = next(line for line in lines if line.startswith("allowed-tools:"))
    consent = next(line for line in lines if "For consent, caller means the user" in line)
    assert "AskUserQuestion" in tools.partition(":")[2].replace(",", " ").split()
    assert "A direct user `/bridge:implement` invocation consents only to its exact task" in consent
    assert "every other dispatch" in consent
    assert "use AskUserQuestion" in consent
    assert "exact change, absolute target workspace, and network mode" in consent
    assert "cite prior user approval covering that same scope, workspace, and network mode" in consent
    assert "Skill or agent invocation alone is not user approval" in consent


def test_orchestrator_approval_names_task_set_and_default_boundary() -> None:
    """Recorded task selection must authorize bounded unattended work without self-approval."""
    lines = (SKILLS / "implement/SKILL.md").read_text(encoding="utf-8").splitlines()
    consent = next(line for line in lines if "A citable approval" in line)
    assert "recorded user decision in the orchestrating skill that names the task set" in consent
    assert "workspace is the skill's absolute launch directory" in consent
    assert "network is `none` unless that decision names otherwise" in consent
    assert "brief carries the orchestrating skill's recorded approval" in consent


def test_missing_user_approval_without_question_tool_blocks_dispatch() -> None:
    """A subagent lacking both authority evidence and a user-question tool must stop."""
    lines = (SKILLS / "implement/SKILL.md").read_text(encoding="utf-8").splitlines()
    consent = next(line for line in lines if "AskUserQuestion is unavailable" in line)
    assert "return `blocked` and never dispatch" in consent


@pytest.mark.parametrize(
    "caller",
    [
        "cc_oss/skills/resolve/SKILL.md",
        "cc_oss/skills/resolve/modes/dispatch-prelude.md",
        "cc_research/skills/run/modes/codex-copilot.md",
        "cc_foundry/skills/audit/modes/fix.md",
        "cc_foundry/skills/_shared/codex-delegation.md",
    ],
)
def test_orchestrated_caller_carries_recorded_user_approval(caller: str) -> None:
    """Each write-capable caller must cite its user gate instead of approving its own dispatch."""
    lines = (SKILLS.parent.parent / caller).read_text(encoding="utf-8").splitlines()
    consent = next(line for line in lines if "recorded user decision" in line)
    assert "task set" in consent
    assert "absolute launch directory" in consent
    assert "network is `none` unless that decision names otherwise" in consent
    assert "brief carries the orchestrating skill's recorded approval" in consent
    assert "return `blocked` and never dispatch" in consent


def test_research_approval_uses_user_request_rather_than_availability_check() -> None:
    """An automatic plugin probe or restored flag cannot stand in for a user's task authorization."""
    lines = (SKILLS.parent.parent / "cc_research/skills/run/modes/codex-copilot.md").read_text().splitlines()
    consent = next(line for line in lines if "recorded user decision" in line)
    assert "direct user `/research:run --codex` request" in consent
    assert "R2 checks availability" in consent
    assert "restored flag or internal invocation alone is not approval" in consent


def test_detached_wait_keeps_cancellation_pending() -> None:
    """Cancellation acknowledgement cannot release ownership while the write-capable child exits."""
    lines = (SKILLS / "implement/SKILL.md").read_text(encoding="utf-8").splitlines()
    lifecycle = next(line for line in lines if "any state other than" in line)
    active = lifecycle.partition("any state other than")[2].partition("ends the wait")[0]
    assert set(re.findall(r"`([^`]+)`", active)) == {"queued", "running", "cancel_requested"}
    assert any("Do not edit task-named paths while any implement call is in flight" in line for line in lines)


def test_detached_launch_retains_absolute_workspace_without_an_option() -> None:
    """A later directory change must not turn a running detached job into a missing lookup."""
    lines = (SKILLS / "implement/SKILL.md").read_text(encoding="utf-8").splitlines()
    preparation = next(line for line in lines if "Resolve the target workspace" in line)
    lifecycle = next(line for line in lines if "If detached" in line)
    assert "absolute path before dispatch" in preparation
    assert "when `--workspace` is omitted, record the absolute launch directory" in preparation
    assert "return the job identifier together with that absolute workspace" in lifecycle
    assert "pass that absolute path as `--workspace` to every one of them" in lifecycle
    assert "even when the launch omitted `--workspace`" in lifecycle


def test_dispatch_arguments_remain_discoverable(skill_lines: list[str]) -> None:
    """Callers see correlation options without advertising an internal refusal-only override."""
    hint = next(line for line in skill_lines if line.startswith("argument-hint:"))
    parsing = next(line for line in skill_lines if line.startswith("Parse `$ARGUMENTS`"))
    assert "--depth" not in hint
    assert "`depth`" not in parsing
    assert "[--run-id ID]" in hint


@pytest.mark.parametrize("document", ["README.md", "docs/operations.md"])
def test_lifecycle_docs_keep_cancellation_pending(document: str) -> None:
    """Readers must wait for teardown after cancellation acknowledgement."""
    lines = (SKILLS.parent / document).read_text(encoding="utf-8").splitlines()
    lifecycle = next(line for line in lines if "any state other than" in line)
    assert "any state other than `queued`, `running` or `cancel_requested`" in lifecycle
