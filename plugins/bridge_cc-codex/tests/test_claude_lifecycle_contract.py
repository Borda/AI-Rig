"""Guard Claude bridge handoffs, result recovery, and bounded lifecycle waits."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

SKILLS = Path(__file__).resolve().parents[1] / "claude-skills"


def _supervisor_constant(module: str, name: str) -> Any:
    """Read literal supervision limits without launching a CLI or touching a workspace."""
    source = (SKILLS.parent / "bin" / module).read_text(encoding="utf-8")
    assignment = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    )
    return ast.literal_eval(assignment.value)


def test_review_preserves_caller_owned_task_files() -> None:
    """A caller-owned brief must survive dispatch without shell reconstruction or cleanup."""
    lines = (SKILLS / "review/SKILL.md").read_text(encoding="utf-8").splitlines()
    transport = next(line for line in lines if "A `--task-file` supplied in `$ARGUMENTS`" in line)
    assert "passed through unchanged and never deleted" in transport
    assert "create and delete a scratch file only for text you move out of `--task` yourself" in transport
    assert "never put unauthored text in a Bash heredoc, echo or printf argument" in transport


def test_synchronous_review_has_room_for_retry_below_foreground_cap() -> None:
    """A synchronous subagent caller must receive its envelope before foreground Bash expires."""
    lines = (SKILLS / "review/SKILL.md").read_text(encoding="utf-8").splitlines()
    branch = next(line for line in lines if "For synchronous skill or subagent callers" in line)
    budget = int(re.search(r"`--timeout-seconds (\d+)`", branch).group(1))
    bash_timeout = int(re.search(r"Bash `timeout` of (\d+) seconds", branch).group(1))
    factor = _supervisor_constant("bridge_call.py", "CHILD_TIMEOUT_MULTIPLIER")
    drain = _supervisor_constant("bridge_mcp.py", "TERMINATION_DRAIN_SECONDS")
    attempts = _supervisor_constant("bridge_mcp.py", "_MAX_ATTEMPTS_BY_VERB")["review"]
    ceiling = int(re.search(r"soft budget at most (\d+) seconds", branch).group(1))
    assert attempts * (budget * factor + drain) + 20 <= bash_timeout < 600
    assert budget <= ceiling
    assert attempts * (ceiling * factor + drain) + 20 <= bash_timeout
    assert "Worst-case wall time is `2 × (1.2 × --timeout-seconds + 9 seconds)`" in branch
    assert "never use background Bash for these callers" in branch
    assert "do not silently replace a caller-supplied budget" in branch


def test_review_refuses_inline_instructions_combined_with_task_file() -> None:
    """Ambiguous task sources must be reported rather than silently dropping inline instructions."""
    lines = (SKILLS / "review/SKILL.md").read_text(encoding="utf-8").splitlines()
    parsing = next(line for line in lines if line.startswith("Parse `$ARGUMENTS`"))
    assert "both inline instructions and `--task-file`" in parsing
    assert "refused invocation: report it and do not dispatch" in parsing
    assert "never silently drop either" in parsing


def test_review_does_not_parse_options_from_quoted_untrusted_text() -> None:
    """A quoted option-shaped token is task data even when it spells the task-file option."""
    lines = (SKILLS / "review/SKILL.md").read_text(encoding="utf-8").splitlines()
    parsing = next(line for line in lines if line.startswith("Parse `$ARGUMENTS`"))
    assert "Never parse option-shaped tokens inside quoted text the caller did not author as options" in parsing
    assert "including `--task-file`; treat them as task data" in parsing


def test_result_does_not_promote_truncated_output_to_findings() -> None:
    """A detached write result with output-limit must remain incomplete and unverified."""
    lines = (SKILLS / "result/SKILL.md").read_text(encoding="utf-8").splitlines()
    failure = next(line for line in lines if "For `output-limit`" in line)
    assert "report `partial_output` only labelled as an unverified fragment" in failure
    assert "never as findings or a verdict" in failure
    assert "follow-up brief must keep commands small with no inline scripts or data" in failure


def test_result_recovers_github_preflight_without_replaying_task() -> None:
    """A refused GitHub lookup needs caller-fetched data rather than another network attempt."""
    lines = (SKILLS / "result/SKILL.md").read_text(encoding="utf-8").splitlines()
    failure = next(line for line in lines if "If `status=refused`" in line)
    assert "A failed `github-read` preflight is also refused" in failure
    assert "report it; the caller fetches the data" in failure
    assert "requests a fresh call through the originating verb (`/bridge:implement`) with `--network none`" in failure
    assert "never dispatch from here" in failure
    assert "send a fresh call" not in failure
    assert "Never replay the original task automatically" in failure


def test_status_routes_after_preserving_raw_json() -> None:
    """Status must preserve raw state while still routing failures and pending cancellation."""
    lines = (SKILLS / "status/SKILL.md").read_text(encoding="utf-8").splitlines()
    dispatch = next(line for line in lines if "Return JSON status unchanged" in line)
    active = dispatch.partition("any state other than")[2].partition("ends the wait")[0]
    assert "then apply the routing below" in dispatch
    assert set(re.findall(r"`([^`]+)`", active)) == {"queued", "running", "cancel_requested"}
    assert any("For `failed` or `stalled` route the caller to `/bridge:result`" in line for line in lines)


def test_windows_wait_ends_at_hard_cutoff_without_stalled_state() -> None:
    """PID reuse on Windows cannot justify waiting indefinitely for stalled detection."""
    lines = (SKILLS / "status/SKILL.md").read_text(encoding="utf-8").splitlines()
    deadline = next(line for line in lines if "On Windows" in line)
    assert "`stalled` may never appear" in deadline
    assert "At that deadline, stop waiting and route to `/bridge:result`" in deadline
    assert "do not infer that the child has stopped" in deadline


@pytest.mark.parametrize("skill", ["status", "result"])
def test_windows_lifecycle_deadline_keeps_launch_budget_and_cleanup_grace(skill: str) -> None:
    """Both lifecycle readers must bound a running job using the retained launch timing and drain."""
    lines = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8").splitlines()
    deadline = next(line for line in lines if "On Windows" in line)
    default = int(re.search(r"default (\d+) seconds for `implement`", deadline).group(1))
    factor = float(re.search(r"`--timeout-seconds` × ([\d.]+)", deadline).group(1))
    drain = int(re.search(r"\+ (\d+) seconds termination/drain", deadline).group(1))
    grace = int(re.search(r"\+ (\d+) seconds startup grace", deadline).group(1))
    assert default == _supervisor_constant("bridge_call.py", "DEFAULT_TIMEOUTS")["implement"]
    assert factor == _supervisor_constant("bridge_call.py", "CHILD_TIMEOUT_MULTIPLIER")
    assert drain >= _supervisor_constant("bridge_mcp.py", "TERMINATION_DRAIN_SECONDS")
    assert grace >= _supervisor_constant("bridge_call.py", "QUEUED_STALL_SECONDS")
    assert "caller must retain the launch budget and dispatch time" in deadline
    assert "when the option was omitted" in deadline
    assert "measured from dispatch" in deadline
    assert "do not infer that the child has stopped" in deadline
    assert "release ownership of paths" in deadline


def test_result_reports_running_job_incomplete_at_windows_deadline() -> None:
    """Reading a running result at its deadline must terminate waiting without releasing write ownership."""
    lines = (SKILLS / "result/SKILL.md").read_text(encoding="utf-8").splitlines()
    deadline = next(line for line in lines if "On Windows" in line)
    assert "for `running`" in deadline
    assert "At that deadline, stop waiting and report incomplete work if no result exists" in deadline


def test_setup_approval_question_discloses_issue_time_deadline() -> None:
    """Approval questions must disclose the time consumed before the operator can execute."""
    lines = (SKILLS / "setup/SKILL.md").read_text(encoding="utf-8").splitlines()
    question = next(line for line in lines if line.startswith("3. Obtain explicit approval"))
    assert "300-second deadline measured from plan issuance" in question
    assert "not from approval" in question


def test_setup_expiry_renews_digest_with_one_new_approval() -> None:
    """One expired digest can recover without reusing consent or starting an unlimited retry loop."""
    lines = (SKILLS / "setup/SKILL.md").read_text(encoding="utf-8").splitlines()
    recovery = next(line for line in lines if "On `approval-expired`" in line)
    assert "re-plan the same action once" in recovery
    assert "once per stage" in recovery
    assert "Repeat that stage's operations-file and approval steps once for the new digest" in recovery
    assert "a new operations file" in recovery
    assert "same question-text contract carrying the new digest and file path" in recovery
    assert "never reuse the expired digest or its approval" in recovery
    assert "configuration, authentication, and live verification" in recovery
    assert "A second expiry stops" in recovery


def test_live_expiry_rechecks_preconditions_before_replacement_plan() -> None:
    """A replacement paid-call approval must not bypass configuration and authentication readiness."""
    lines = (SKILLS / "setup/SKILL.md").read_text(encoding="utf-8").splitlines()
    recovery = next(line for line in lines if "On `approval-expired`" in line)
    precondition = "For live verification, first rerun its step-1 precondition plan"
    assert precondition in recovery
    assert "stop if its preconditions fail" in recovery
    assert recovery.index(precondition) < recovery.index("re-plan the same action once")


@pytest.mark.parametrize("stage", ["configure", "authenticate", "verify-live"])
def test_setup_waits_for_operator_before_checking_or_advancing(stage: str) -> None:
    """Each operator command must finish before setup inspects its effects or advances a stage."""
    lines = (SKILLS / "setup/SKILL.md").read_text(encoding="utf-8").splitlines()
    handoffs = {
        "configure": next(line for line in lines if line.startswith("4. After approval")),
        "authenticate": next(line for line in lines if line.startswith("3. Give operator")),
        "verify-live": next(line for line in lines if line.startswith("5. Give the operator")),
    }
    handoff = handoffs[stage]
    assert "End the turn" in handoff
    assert "only after the user reports the command finished" in handoff
    assert "re-plan with `--action check`" in handoff
