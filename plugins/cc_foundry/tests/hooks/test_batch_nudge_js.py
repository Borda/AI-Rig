"""Subprocess tests for ``hooks/batch-nudge.js``.

The hook counts, per agent, consecutive ``PostToolBatch`` events whose whole batch is one *batchable* read-only tool
call (Read/Grep/Glob, provably read-only Bash): the agent issued independent calls in separate turns instead of one
parallel batch. At ``NUDGE_THRESHOLD`` it injects a reminder as ``additionalContext`` (exit 0, never
``decision: block`` — a block would stop the agentic loop).

Behavioural areas covered:

* **Never blocks** — exit 0 and no ``decision``/``continue`` field at any streak length.
* **Streak accumulation** — single-call batches of a batchable tool increment the streak; below the threshold the hook
  stays silent.
* **Parallel batch resets** — a batch with 2+ calls is already parallel, so it clears the streak.
* **Non-batchable interruption** — a non-batchable tool (e.g. Edit) clears the streak, since it usually signals a real
  dependency, not a missed batch.
* **Nudge threshold and cooldown** — the batch that crosses ``NUDGE_THRESHOLD`` carries the reminder; the streak then
  restarts so the next nudge needs a full new streak.
* **Per-agent state** — subagents share the ``session_id`` and differ only by ``agent_id``; each keeps its own streak.
* **UserPromptSubmit reset** — a new user turn resets the main agent's streak, not a background subagent's.
* **Subagent lifecycle** — ``SubagentStart`` (spawn, resume, teammate's new message) and ``SubagentStop`` delete that
  agent's state file, so a streak never spans an agent's turns and finished agents leave no file; main is untouched.
* **Bash classification** — strict: single read-only commands count; mutating git subcommands, redirects, chained or
  substituted commands, writing ``find`` actions, and any command holding a character outside the plain allowlist
  (quotes, escapes, braces, parentheses, ``$``, globs) do not.
* **Fail-open input** — malformed JSON or a malformed ``tool_calls`` exits 0 silently; a failed state write leaves no
  temp file.
* **Wiring** — registered on ``PostToolBatch``, ``SubagentStart``, ``SubagentStop`` and ``UserPromptSubmit`` only, and
  the cited rule section exists.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from _hook_env import _hook_tmp_base

NUDGE_THRESHOLD = 4
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"
CLAUDE_CONFIG_RULE = PLUGIN_ROOT / "rules" / "claude-config.md"


@pytest.fixture(name="session_id")
def _session_id(request: pytest.FixtureRequest) -> Iterator[str]:
    """Provide an isolated session identifier and clean its state around each test.

    Sanitized like the hook's own ``sanitize()``: a parametrized name carries ``[...]``, which the hook writes as ``_``,
    so cleaning the raw name would miss the dir the hook actually created.
    """
    sid = re.sub(r"[^a-zA-Z0-9_-]", "_", f"test-batch-nudge-{request.node.name}")
    _rm(sid)
    yield sid
    _rm(sid)


def _rm(sid: str) -> None:
    """Remove the batch-nudge state directory for one isolated session."""
    import shutil

    shutil.rmtree(_hook_tmp_base() / f"claude-state-{sid}", ignore_errors=True)


def _batch(sid: str, *calls: tuple[str, dict], agent_id: str | None = None) -> dict:
    """Build a ``PostToolBatch`` payload from ``(tool_name, tool_input)`` pairs, optionally from a subagent.

    Examples:
        >>> _batch("s", ("Read", {"file_path": "x"}))["tool_calls"][0]["tool_name"]
        'Read'
        >>> _batch("s", ("Read", {}), agent_id="a1")["agent_id"]
        'a1'
    """
    payload = {
        "hook_event_name": "PostToolBatch",
        "session_id": sid,
        "tool_calls": [
            {"tool_name": name, "tool_input": tool_input, "tool_use_id": f"tu{i}", "tool_response": "x"}
            for i, (name, tool_input) in enumerate(calls)
        ],
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _user_prompt(sid: str) -> dict:
    """Build a ``UserPromptSubmit`` payload.

    Examples:
        >>> _user_prompt("s")
        {'hook_event_name': 'UserPromptSubmit', 'session_id': 's'}
    """
    return {"hook_event_name": "UserPromptSubmit", "session_id": sid}


def _subagent_event(sid: str, event: str, agent_id: str | None) -> dict:
    """Build a ``SubagentStart``/``SubagentStop`` payload; ``agent_id=None`` omits the field.

    Examples:
        >>> _subagent_event("s", "SubagentStop", "a1")["agent_id"]
        'a1'
        >>> "agent_id" in _subagent_event("s", "SubagentStop", None)
        False
    """
    payload = {"hook_event_name": event, "session_id": sid, "agent_type": "Explore"}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _state_dir(sid: str) -> Path:
    """Resolve the per-session batch state dir, tracking the hook's own ``getSentinelDir()``."""
    return _hook_tmp_base() / f"claude-state-{sid}" / "batch"


def _agent_file(sid: str, agent_id: str) -> Path:
    """Resolve one subagent's streak file (``agent-`` prefix; ids here need no sanitizing)."""
    return _state_dir(sid) / f"agent-{agent_id}.json"


def _run_singles(
    run_hook: Callable,
    sid: str,
    cwd: Path,
    n: int,
    tool_name: str = "Read",
    tool_input: dict | None = None,
    agent_id: str | None = None,
) -> list[subprocess.CompletedProcess]:
    """Fire ``n`` single-call ``PostToolBatch`` events for one tool and return each hook result.

    ``cwd`` is any existing directory — the hook keys its state on ``session_id`` under its own sentinel dir and never
    reads the working directory. Callers pass the ``tmp_path`` fixture, which exists on every platform.
    """
    payload = _batch(sid, (tool_name, tool_input or {}), agent_id=agent_id)
    return [run_hook("batch-nudge.js", payload, cwd=cwd) for _ in range(n)]


def _run_raw(stdin: str) -> subprocess.CompletedProcess:
    """Run the hook on raw stdin text, for inputs ``run_hook`` cannot express (it always sends valid JSON)."""
    return subprocess.run(
        ["node", str(PLUGIN_ROOT / "hooks" / "batch-nudge.js")],
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def _nudged(result: subprocess.CompletedProcess) -> bool:
    """Report whether one hook result carried the batching reminder."""
    return "Batching reminder" in result.stdout


def _output_keys(result: subprocess.CompletedProcess) -> set[str]:
    """Return the top-level JSON keys a hook result printed, empty when it printed nothing."""
    return set(json.loads(result.stdout)) if result.stdout else set()


@pytest.mark.usefixtures("session_id")
class TestBatchNudge:
    """batch-nudge.js: non-blocking streak-based batching reminder."""

    def test_never_blocks(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """Every batch exits 0 and no result carries a ``decision`` or ``continue`` field.

        A ``decision: "block"`` or ``continue: false`` on PostToolBatch stops the agentic loop, which an informational
        reminder must never do, even well past the threshold.
        """
        results = _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD * 2)

        assert [r.returncode for r in results] == [0] * (NUDGE_THRESHOLD * 2)
        assert all(not (_output_keys(r) & {"decision", "continue"}) for r in results)

    def test_no_nudge_below_threshold(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """Fewer than NUDGE_THRESHOLD single-call batches never trigger a nudge."""
        results = _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)

        assert [r.stdout for r in results] == [""] * (NUDGE_THRESHOLD - 1)

    def test_nudge_fires_at_threshold(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """The batch that crosses NUDGE_THRESHOLD carries the reminder as PostToolBatch additionalContext.

        Scenario: the model reads four files one turn at a time. Only the fourth batch gets output, and it is the
        documented ``hookSpecificOutput`` shape so Claude Code injects it before the next model call.
        """
        results = _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD)

        assert [_nudged(r) for r in results] == [False] * (NUDGE_THRESHOLD - 1) + [True]
        output = json.loads(results[-1].stdout)["hookSpecificOutput"]
        assert output["hookEventName"] == "PostToolBatch"
        assert "§Turn Batching" in output["additionalContext"]

    def test_streak_restarts_after_nudge(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """After a nudge the streak restarts, so the next nudge needs a full new streak."""
        results = _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD * 2)

        assert [_nudged(r) for r in results] == ([False] * (NUDGE_THRESHOLD - 1) + [True]) * 2

    @pytest.mark.parametrize(
        "calls",
        [
            pytest.param((("Read", {}), ("Read", {})), id="parallel-reads"),
            pytest.param((("Read", {}), ("Edit", {})), id="mixed-batch"),
        ],
    )
    def test_multi_call_batch_resets_streak(
        self, session_id: str, run_hook: Callable, tmp_path: Path, calls: tuple
    ) -> None:
        """A batch of two or more calls is already parallel and clears the streak."""
        _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)
        run_hook("batch-nudge.js", _batch(session_id, *calls), cwd=tmp_path)

        results = _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)

        assert [r.stdout for r in results] == [""] * (NUDGE_THRESHOLD - 1)

    def test_non_batchable_tool_resets_streak(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """An Edit batch mid-streak resets the counter — a dependency break, not a missed batch."""
        _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)
        _run_singles(run_hook, session_id, tmp_path, 1, tool_name="Edit")

        results = _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)

        assert [r.stdout for r in results] == [""] * (NUDGE_THRESHOLD - 1)

    def test_user_prompt_submit_resets_streak(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """A new user turn resets the main agent's streak regardless of what came before."""
        _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)
        run_hook("batch-nudge.js", _user_prompt(session_id), cwd=tmp_path)

        results = _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)

        assert [r.stdout for r in results] == [""] * (NUDGE_THRESHOLD - 1)

    def test_interleaved_agents_keep_separate_streaks(
        self, session_id: str, run_hook: Callable, tmp_path: Path
    ) -> None:
        """Two agents of one session alternating single calls never sum into one streak.

        Scenario: a lead fans out subagents that share the ``session_id`` and differ only by ``agent_id``. With A,B
        alternating, a shared counter nudged whichever agent fired fourth after only two calls of its own; each agent
        must instead reach the threshold on its own calls, so only A's fourth call nudges.
        """
        payload_a = _batch(session_id, ("Read", {}), agent_id="agent-a")
        payload_b = _batch(session_id, ("Read", {}), agent_id="agent-b")
        order = [payload_a, payload_b] * (NUDGE_THRESHOLD - 1) + [payload_a]

        results = [run_hook("batch-nudge.js", payload, cwd=tmp_path) for payload in order]

        assert [_nudged(r) for r in results] == [False] * (2 * (NUDGE_THRESHOLD - 1)) + [True]

    def test_other_agent_parallel_batch_keeps_streak(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """One agent's parallel batch does not reset another agent's streak."""
        _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1, agent_id="agent-b")
        run_hook("batch-nudge.js", _batch(session_id, ("Read", {}), ("Read", {}), agent_id="agent-a"), cwd=tmp_path)

        results = _run_singles(run_hook, session_id, tmp_path, 1, agent_id="agent-b")

        assert _nudged(results[0])

    def test_user_prompt_submit_keeps_subagent_streak(
        self, session_id: str, run_hook: Callable, tmp_path: Path
    ) -> None:
        """A user turn is no boundary for a background subagent, so its streak survives UserPromptSubmit."""
        _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1, agent_id="agent-b")
        run_hook("batch-nudge.js", _user_prompt(session_id), cwd=tmp_path)

        results = _run_singles(run_hook, session_id, tmp_path, 1, agent_id="agent-b")

        assert _nudged(results[0])

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param("SubagentStart", id="start-or-resume"),
            pytest.param("SubagentStop", id="stop"),
        ],
    )
    def test_subagent_boundary_resets_its_streak(
        self, session_id: str, run_hook: Callable, tmp_path: Path, event: str
    ) -> None:
        """A subagent's streak never spans its turns.

        Scenario: the subagent reads three files one at a time, finishes, and is later resumed (SubagentStart also
        fires when a teammate handles a new message). Its first single read of the new turn must not draw a nudge
        built on the previous turn's count.
        """
        _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1, agent_id="agent-b")
        run_hook("batch-nudge.js", _subagent_event(session_id, event, "agent-b"), cwd=tmp_path)

        results = _run_singles(run_hook, session_id, tmp_path, 1, agent_id="agent-b")

        assert not _nudged(results[0])

    @pytest.mark.parametrize(
        ("event", "agent_id"),
        [
            pytest.param("SubagentStart", "agent-b", id="other-agent"),
            pytest.param("SubagentStop", None, id="no-agent-id"),
            pytest.param("SubagentStop", "", id="empty-agent-id"),
            pytest.param("SubagentStart", "main", id="agent-id-main"),
        ],
    )
    def test_subagent_event_keeps_main_streak(
        self, session_id: str, run_hook: Callable, tmp_path: Path, event: str, agent_id: str | None
    ) -> None:
        """A subagent event never resets the main agent's streak, whatever its ``agent_id``.

        Scenario: internal agents such as prompt suggestions also fire SubagentStop, possibly without a usable id, and
        an id spelled ``main`` must not address the main agent's file.
        """
        _run_singles(run_hook, session_id, tmp_path, NUDGE_THRESHOLD - 1)
        run_hook("batch-nudge.js", _subagent_event(session_id, event, agent_id), cwd=tmp_path)

        results = _run_singles(run_hook, session_id, tmp_path, 1)

        assert _nudged(results[0])

    def test_subagent_stop_removes_state_file(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """A finished subagent leaves no state file behind, so per-agent files do not pile up over a session."""
        _run_singles(run_hook, session_id, tmp_path, 1, agent_id="agent-b")
        run_hook("batch-nudge.js", _subagent_event(session_id, "SubagentStop", "agent-b"), cwd=tmp_path)

        assert not _agent_file(session_id, "agent-b").exists()

    def test_failed_state_write_leaves_no_temp_file(self, session_id: str, run_hook: Callable, tmp_path: Path) -> None:
        """A state write whose rename fails exits 0 and removes its temp file.

        Scenario: the target path is occupied by a directory, so the rename over it fails; the hook must swallow the
        error without leaving a ``.tmp`` file in the state dir.
        """
        _agent_file(session_id, "agent-x").mkdir(parents=True)

        results = _run_singles(run_hook, session_id, tmp_path, 1, agent_id="agent-x")

        assert results[0].returncode == 0
        assert sorted(p.name for p in _state_dir(session_id).iterdir()) == [_agent_file(session_id, "agent-x").name]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git log -5", id="git-log"),
            pytest.param("git status", id="git-status"),
            pytest.param("find . -name x", id="find-name"),
            pytest.param("ls -la", id="ls"),
            pytest.param("git show HEAD:a.py", id="git-show-colon"),
            pytest.param("git log --format=%H,%an -1", id="git-log-format-percent-comma"),
            pytest.param("git log --author=a@b.c -1", id="git-log-author-at"),
            pytest.param("tail -n +2 a.py", id="tail-plus"),
            pytest.param("grep -n foo a.py", id="grep"),
            pytest.param("wc -l a.py", id="wc"),
        ],
    )
    def test_readonly_bash_counts_as_batchable(
        self, session_id: str, run_hook: Callable, tmp_path: Path, command: str
    ) -> None:
        """Accumulate repeated read-only shell commands like other read operations."""
        results = _run_singles(
            run_hook, session_id, tmp_path, NUDGE_THRESHOLD, tool_name="Bash", tool_input={"command": command}
        )

        assert _nudged(results[-1])

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git status && rm -rf x", id="compound-command"),
            pytest.param("rm x", id="writing-command"),
            pytest.param("git add a.py", id="git-add"),
            pytest.param("git commit -m fix", id="git-commit"),
            pytest.param("git push origin main", id="git-push"),
            pytest.param("git diff --output=patch.diff", id="git-diff-output"),
            pytest.param("cat a > b", id="redirect"),
            pytest.param("cat a\nrm b", id="newline-second-command"),
            pytest.param("cat $(ls)", id="command-substitution"),
            pytest.param("find . -name x -delete", id="find-delete"),
            pytest.param("find . -exec rm {} +", id="find-exec"),
            pytest.param('find . "-delete"', id="find-quoted-delete"),
            pytest.param("find . -name x '-exec' rm {} +", id="find-quoted-exec"),
            pytest.param("git log --'output'=x", id="git-quoted-output"),
            pytest.param('git "add" a.py', id="git-quoted-subcommand"),
            pytest.param("find . \\-delete", id="find-escaped-delete"),
            pytest.param("find . {-delete,-print}", id="find-brace-delete"),
            pytest.param("find . -name x {-exec,-print} rm {} +", id="find-brace-exec"),
            pytest.param("git log {--output=x,-1}", id="git-brace-output"),
            pytest.param("find . -name x$IFS-delete", id="find-ifs-delete"),
            pytest.param("git log -1 ${IFS}--output=x", id="git-ifs-output"),
            pytest.param("find . -name *.py", id="find-glob"),
            pytest.param("cat =(touch x)", id="zsh-process-substitution"),
            pytest.param("ls *(e:'touch x':)", id="zsh-glob-qualifier"),
            pytest.param("grep -E a(b)c f", id="parenthesis"),
            pytest.param('grep "foo" a.py', id="quoted-grep-strict"),
        ],
    )
    def test_non_batchable_bash_never_accumulates(
        self, session_id: str, run_hook: Callable, tmp_path: Path, command: str
    ) -> None:
        """A Bash command that is not provably read-only never accumulates a streak.

        Scenario: an ordered write sequence such as add → commit → push must never be told its calls were independent;
        chained, redirected or substituted commands may write, so they are never counted either.
        """
        results = _run_singles(
            run_hook, session_id, tmp_path, NUDGE_THRESHOLD, tool_name="Bash", tool_input={"command": command}
        )

        assert not any(_nudged(r) for r in results)

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param("PreToolUse", id="pre-tool-use"),
            pytest.param("PostToolUse", id="post-tool-use"),
        ],
    )
    def test_per_tool_events_are_ignored(self, session_id: str, run_hook: Callable, tmp_path: Path, event: str) -> None:
        """Per-tool events carry no batch boundary, so a stale registration on them never counts or nudges."""
        payload = {"hook_event_name": event, "tool_name": "Read", "tool_input": {}, "session_id": session_id}
        results = [run_hook("batch-nudge.js", payload, cwd=tmp_path) for _ in range(NUDGE_THRESHOLD * 2)]

        assert [(r.returncode, r.stdout) for r in results] == [(0, "")] * (NUDGE_THRESHOLD * 2)


class TestBatchNudgeFailOpen:
    """batch-nudge.js never crashes or speaks on input it cannot read."""

    @pytest.mark.parametrize(
        "stdin",
        [
            pytest.param("not json", id="malformed-json"),
            pytest.param('{"hook_event_name":"PostToolBatch","session_id":"SID"}', id="tool-calls-missing"),
            pytest.param('{"hook_event_name":"PostToolBatch","session_id":"SID","tool_calls":[null]}', id="null"),
            pytest.param('{"hook_event_name":"PostToolBatch","session_id":"SID","tool_calls":"x"}', id="string"),
        ],
    )
    def test_bad_input_exits_zero_silently(self, session_id: str, stdin: str) -> None:
        """Unreadable input exits 0 with no output, so a hook bug never blocks or confuses the agent.

        Payloads name the per-test session (``SID`` placeholder) so any state they write is removed by the fixture.
        """
        result = _run_raw(stdin.replace("SID", session_id))

        assert (result.returncode, result.stdout) == (0, "")


class TestBatchNudgeWiring:
    """hooks.json registration and rule citation of batch-nudge.js."""

    def test_registered_on_batch_and_boundary_events_only(self) -> None:
        """Register once per batch plus the turn-boundary resets, not once per tool call.

        Pre/PostToolUse registrations would spawn node twice for every tool call and bring back the timing guess the
        PostToolBatch event replaced; without the subagent events per-agent streaks would span turns.
        """
        hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]

        registered = sorted(event for event, groups in hooks.items() if "batch-nudge.js" in json.dumps(groups))

        assert registered == ["PostToolBatch", "SubagentStart", "SubagentStop", "UserPromptSubmit"]

    def test_cited_rule_section_exists(self) -> None:
        """The reminder cites §Turn Batching, so the shipped claude-config rule must carry that heading."""
        rule = CLAUDE_CONFIG_RULE.read_text(encoding="utf-8")

        assert "\n## Turn Batching" in rule
