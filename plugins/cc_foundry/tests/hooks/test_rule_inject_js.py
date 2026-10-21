"""Subprocess tests for ``hooks/rule-inject.js``.

Two foundry rules matter only during one activity: ``rules/agent-spawn.md`` while calling ``Agent()`` and ``rules/git-
commit.md`` while staging, committing or pushing. Neither loads at session start; the hook injects each one as
PostToolUse / PostToolUseFailure ``additionalContext`` the first time its activity runs, once per session and once per
subagent, and a SessionStart compact/clear makes them inject again because the rebuilt context dropped them. The post
events fire only for a call that ran, so a call blocked by a PreToolUse hook (commit-guard.js) never spends the slot.
Behavioural cases build a fake plugin root (``CLAUDE_PLUGIN_ROOT``) and a private temp dir (``TMPDIR``) so sentinels
never leave ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
HOOK = PLUGIN_ROOT / "hooks" / "rule-inject.js"
COMMIT_GUARD = PLUGIN_ROOT / "hooks" / "commit-guard.js"
SESSION_ID = "6b0f3c1e-2a7d-4e59-8c14-9d3b5a7e2f60"
SUBAGENT_ID = "a7f3c21"
# Must match INJECT_CAP in the hook: above it the hook sends a pointer instead of the body.
INJECT_CAP = 9500
SPAWN_RULE = "---\ndescription: SPAWN-SUMMARY\npaths:\n  - '**/skills/**/*.md'\n---\n\nSPAWN-BODY\n"
COMMIT_RULE = "---\ndescription: COMMIT-SUMMARY\npaths:\n  - '**/.git/COMMIT_EDITMSG'\n---\n\nCOMMIT-BODY\n"

_requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")
_requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git executable not available")


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    """Fake plugin root shipping both injected rules."""
    root = tmp_path / "plugin"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "agent-spawn.md").write_text(SPAWN_RULE, encoding="utf-8")
    (root / "rules" / "git-commit.md").write_text(COMMIT_RULE, encoding="utf-8")
    return root


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    """Private ``TMPDIR`` for the hook's sentinels."""
    path = tmp_path / "tmp"
    path.mkdir()
    return path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Empty git repo on branch ``main`` whose unique name keeps commit-guard's legacy sentinel wipe off siblings."""
    repo = tmp_path / f"inject-{uuid.uuid4().hex[:12]}"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, timeout=15)
    return repo


def _tool(tool_name: str, tool_input: dict, event: str = "PostToolUse", **extra: str) -> str:
    """Payload for one tool call in the test session; ``PostToolUse`` unless ``event`` says otherwise."""
    payload = {
        "hook_event_name": event,
        "session_id": SESSION_ID,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    return json.dumps({**payload, **extra})


def _agent(event: str = "PostToolUse", **extra: str) -> str:
    """Payload for an ``Agent`` call; ``agent_id=...`` places it inside a subagent."""
    return _tool("Agent", {"description": "lint pass", "prompt": "Lint pass"}, event, **extra)


def _bash(command: str, event: str = "PostToolUse", **extra: str) -> str:
    """Payload for a ``Bash`` call."""
    return _tool("Bash", {"command": command}, event, **extra)


def _session_start(source: str, session_id: str = SESSION_ID) -> str:
    """SessionStart payload for one ``source``."""
    return json.dumps({"hook_event_name": "SessionStart", "source": source, "session_id": session_id})


def _pre_compact(**extra: str) -> str:
    """PreCompact payload; ``agent_id=...`` marks a subagent's own compaction."""
    payload = {
        "hook_event_name": "PreCompact",
        "session_id": SESSION_ID,
        "trigger": "auto",
        "custom_instructions": None,
    }
    return json.dumps({**payload, **extra})


def _run(payload: str, root: Path, temp: Path, hook: Path = HOOK, cwd: Path | None = None) -> str:
    """Run the hook, assert it exited 0, and return its stdout."""
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(root), "TMPDIR": str(temp)}
    result = subprocess.run(
        ["node", str(hook)],
        input=payload,
        env=env,
        # Default to the temp dir: the hook reads `<cwd>/.temp/state/skill-contract.md`, so the repo cwd would let a
        # live contract from a real session in this checkout leak into SessionStart output. A test that deliberately
        # passes a missing temp dir runs from this test directory instead, never from a `tmp_path` parent, which a
        # concurrent pytest session's basetemp rotation can delete mid-run.
        cwd=cwd or (temp if temp.is_dir() else Path(__file__).parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _context(stdout: str, event: str = "PostToolUse") -> str:
    """Return the injected text, asserting the output is exactly the ``event`` additionalContext shape."""
    data = json.loads(stdout)
    assert list(data) == ["hookSpecificOutput"]
    assert sorted(data["hookSpecificOutput"]) == ["additionalContext", "hookEventName"]
    assert data["hookSpecificOutput"]["hookEventName"] == event
    return data["hookSpecificOutput"]["additionalContext"]


@_requires_node
class TestSpawnRule:
    """The spawn rule injects on the first Agent call of each session and subagent."""

    def test_first_agent_call_injects_the_rule_body_without_frontmatter(self, plugin: Path, temp_dir: Path) -> None:
        """The first spawn gets the whole rule body, headed by its file name, and no frontmatter."""
        context = _context(_run(_agent(), plugin, temp_dir))

        assert context.startswith("foundry rule agent-spawn.md")
        assert context.endswith("SPAWN-BODY")
        assert "paths:" not in context

    def test_second_agent_call_is_silent(self, plugin: Path, temp_dir: Path) -> None:
        """The rule is already in context after the first injection, so the next spawn adds nothing."""
        _run(_agent(), plugin, temp_dir)

        assert _run(_agent(), plugin, temp_dir) == ""

    def test_each_subagent_gets_the_rule_once(self, plugin: Path, temp_dir: Path) -> None:
        """A subagent has its own context: it gets the rule even after the main thread did, but only once."""
        _run(_agent(), plugin, temp_dir)

        first = _run(_agent(agent_id=SUBAGENT_ID), plugin, temp_dir)
        second = _run(_agent(agent_id=SUBAGENT_ID), plugin, temp_dir)

        assert "SPAWN-BODY" in _context(first)
        assert second == ""

    def test_another_session_gets_its_own_injection(self, plugin: Path, temp_dir: Path) -> None:
        """Sentinels are scoped by session id, so a concurrent session is unaffected."""
        _run(_agent(), plugin, temp_dir)

        out = _run(_agent(session_id="0c2d4e6f-1a3b-4c5d-8e9f-0a1b2c3d4e5f"), plugin, temp_dir)

        assert "SPAWN-BODY" in _context(out)


@_requires_node
class TestCommitRule:
    """The commit rule injects on the first Bash call that stages, commits, pushes or otherwise creates a commit."""

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m 'fix: x'",
            "git push origin main",
            "git add plugins/x.py",
            "pre-commit run --files x.py && git commit -m y",
            "cd repo; git push",
            "git -C /work/repo commit -m x",
            "env GIT_TRACE=1 git push",
            "/usr/bin/git add -p",
            "git commit -m \"$(cat <<'EOF'\nfeat: x\nEOF\n)\"",
        ],
    )
    def test_git_add_commit_or_push_injects(self, plugin: Path, temp_dir: Path, command: str) -> None:
        """Any shell segment invoking the three subcommands counts, whatever prefix or global option precedes it."""
        context = _context(_run(_bash(command), plugin, temp_dir))

        assert context.startswith("foundry rule git-commit.md")
        assert context.endswith("COMMIT-BODY")

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("{ git commit -m x; }", id="brace-group"),
            pytest.param("( git push )", id="subshell"),
            pytest.param("if true; then git commit -m x; fi", id="if-then"),
            pytest.param("while false; do :; done; ! git add f", id="negation"),
            pytest.param("time git commit -m x", id="time"),
            pytest.param("command git commit -m x", id="command"),
            pytest.param("nohup git push", id="nohup"),
            pytest.param("exec git add f", id="exec"),
            pytest.param("sudo -u root git push", id="sudo-with-option-value"),
            pytest.param('bash -c "git commit -m x"', id="bash-c"),
            pytest.param("sh -c 'cd repo && git push'", id="sh-c-compound"),
            pytest.param("eval 'git add f'", id="eval"),
            pytest.param("git ls-files -m | xargs git add", id="xargs"),
            pytest.param('git -C "dir with space" push', id="quoted-dir-with-space"),
            pytest.param('"git" commit -m x', id="quoted-argv0"),
            pytest.param('echo "$(git commit -m x)"', id="substitution-in-double-quotes"),
            pytest.param("GIT commit -m x", id="upper-case-git"),
            pytest.param("git -c alias.ci=commit ci -m x", id="inline-alias"),
            pytest.param("bash <<< 'git commit -m x'", id="here-string-to-shell"),
            pytest.param("cat <<< hi\ntime git commit -m x\nhi", id="here-string-then-next-line"),
            pytest.param("git -c help.autocorrect=immediate cmmit -m x", id="inline-autocorrect"),
            pytest.param("git --config-env help.autocorrect=V cmmit -m x", id="inline-autocorrect-run-time-value"),
            pytest.param("git 2>/dev/null commit -m x", id="redirection-before-subcommand"),
        ],
    )
    def test_grouped_wrapped_or_nested_invocation_injects(self, plugin: Path, temp_dir: Path, command: str) -> None:
        """Shell grouping, a command prefix, quoting or nested shell source cannot hide the git call.

        Each form runs ``git add``/``commit``/``push`` for real; a detector reading only the first word of an operator-
        split segment missed every one of them, so the rule never arrived for a session spelling it this way.
        """
        assert "COMMIT-BODY" in _context(_run(_bash(command), plugin, temp_dir))

    @pytest.mark.parametrize(
        "command",
        [
            "git merge --no-ff feature",
            "git revert abc123",
            "git cherry-pick abc123",
            "git am fix.patch",
            "git rebase main",
            pytest.param("git tag -a v1.0 -m 'release'", id="tag-annotated"),
            pytest.param("git tag -s v1.0", id="tag-signed"),
            pytest.param("git tag -m msg v1.0", id="tag-message"),
            pytest.param("git tag --annotate v1.0", id="tag-annotate-long"),
        ],
    )
    def test_commit_creating_subcommands_inject(self, plugin: Path, temp_dir: Path, command: str) -> None:
        """Every subcommand that writes a commit or tag message delivers the rule governing that message."""
        assert "COMMIT-BODY" in _context(_run(_bash(command), plugin, temp_dir))

    @pytest.mark.parametrize(
        "command",
        [
            "git status --short",
            "echo git commit-ish",
            "git commit-tree abc123",
            "git log --grep commit",
            "git stash push",
            "ls -la",
            pytest.param("echo 'git commit -m x'", id="quoted-text"),
            pytest.param("git tag v1.0", id="tag-lightweight"),
            pytest.param("git tag -l 'v*'", id="tag-list"),
            pytest.param("git merge-base main HEAD", id="merge-base"),
            pytest.param("cat <<< 'git commit -m x'", id="here-string-to-non-runner"),
            pytest.param("git -c help.autocorrect=never cmmit -m x", id="autocorrect-never"),
        ],
    )
    def test_other_commands_stay_silent(self, plugin: Path, temp_dir: Path, command: str) -> None:
        """Only a commit-creating git subcommand triggers; the words alone, or quoted as text, do not."""
        assert _run(_bash(command), plugin, temp_dir) == ""

    def test_second_commit_is_silent(self, plugin: Path, temp_dir: Path) -> None:
        """Staging then pushing in one session injects the commit rule once."""
        _run(_bash("git add a.py"), plugin, temp_dir)

        assert _run(_bash("git push"), plugin, temp_dir) == ""

    def test_commit_and_spawn_rules_are_tracked_separately(self, plugin: Path, temp_dir: Path) -> None:
        """A session that already spawned still gets the commit rule on its first commit."""
        _run(_agent(), plugin, temp_dir)

        assert "COMMIT-BODY" in _context(_run(_bash("git commit -m x"), plugin, temp_dir))


@_requires_node
class TestOnlyCallsThatRan:
    """The slot is spent only by a call that ran — PostToolUse or PostToolUseFailure, never PreToolUse."""

    def test_pre_tool_use_claims_nothing(self, plugin: Path, temp_dir: Path) -> None:
        """A PreToolUse payload neither injects nor creates a sentinel, so a later blocked call cannot burn the slot."""
        out = _run(_bash("git push", event="PreToolUse"), plugin, temp_dir)

        assert out == ""
        assert list(temp_dir.iterdir()) == []

    def test_failed_call_injects_under_its_own_event(self, plugin: Path, temp_dir: Path) -> None:
        """A commit rejected by pre-commit arrives as PostToolUseFailure; the rule is needed for the redraft."""
        payload = _bash("git commit -m x", event="PostToolUseFailure", error="Exit code 1\nruff failed")

        context = _context(_run(payload, plugin, temp_dir), event="PostToolUseFailure")

        assert context.endswith("COMMIT-BODY")

    def test_success_after_a_failed_call_is_silent(self, plugin: Path, temp_dir: Path) -> None:
        """The failed call already delivered the rule, so the successful retry adds nothing."""
        _run(_bash("git commit -m x", event="PostToolUseFailure", error="Exit code 1"), plugin, temp_dir)

        assert _run(_bash("git commit -m y"), plugin, temp_dir) == ""

    @_requires_git
    def test_push_blocked_by_commit_guard_leaves_the_slot_for_the_next_run(
        self, plugin: Path, temp_dir: Path, git_repo: Path
    ) -> None:
        """The first push of a session is blocked, so the rule still injects when a push later runs.

        Claude Code fires PreToolUse to both hooks; commit-guard.js exits 2 for a push nobody authorized, and a blocked
        call fires no PostToolUse or PostToolUseFailure. Registered on PreToolUse, rule-inject claimed its sentinel
        right there and the commit rule was lost for the rest of the session.
        """
        pre_push = _bash("git push origin main", event="PreToolUse")
        guard = subprocess.run(
            ["node", str(COMMIT_GUARD)],
            input=pre_push,
            cwd=git_repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        blocked_out = _run(pre_push, plugin, temp_dir)

        later_push = _run(_bash("git push origin main"), plugin, temp_dir)

        assert guard.returncode == 2, guard.stderr
        assert blocked_out == ""
        assert "COMMIT-BODY" in _context(later_push)


@_requires_node
class TestContextReset:
    """Compaction and /clear drop earlier injections, so the rules inject again afterwards."""

    @pytest.mark.parametrize("source", ["compact", "clear"])
    def test_reset_clears_the_session_and_writes_nothing(self, plugin: Path, temp_dir: Path, source: str) -> None:
        """The reset prints nothing (SessionStart stdout is model context) and the next spawn injects again."""
        _run(_agent(), plugin, temp_dir)
        _run(_agent(agent_id=SUBAGENT_ID), plugin, temp_dir)

        reset = _run(_session_start(source), plugin, temp_dir)

        assert reset == ""
        assert list(temp_dir.iterdir()) == []
        assert "SPAWN-BODY" in _context(_run(_agent(), plugin, temp_dir))

    def test_startup_keeps_sentinels(self, plugin: Path, temp_dir: Path) -> None:
        """Only sources that rebuild the context clear anything."""
        _run(_agent(), plugin, temp_dir)

        _run(_session_start("startup"), plugin, temp_dir)

        assert _run(_agent(), plugin, temp_dir) == ""

    def test_reset_leaves_other_sessions_alone(self, plugin: Path, temp_dir: Path) -> None:
        """Clearing matches this session's exact id suffix, never another session's sentinel."""
        other = "0c2d4e6f-1a3b-4c5d-8e9f-0a1b2c3d4e5f"
        _run(_agent(session_id=other), plugin, temp_dir)

        _run(_session_start("compact"), plugin, temp_dir)

        assert _run(_agent(session_id=other), plugin, temp_dir) == ""

    def test_subagent_pre_compact_clears_only_that_subagent(self, plugin: Path, temp_dir: Path) -> None:
        """A subagent compacting its own context gets its rules again; main and sibling subagents keep theirs.

        A subagent's auto-compaction is no SessionStart. PreCompact carrying ``agent_id`` is the one payload naming the
        compacting subagent; the hooks reference does not state that it fires there, so this pins the handler only.
        """
        for extra in ({}, {"agent_id": SUBAGENT_ID}, {"agent_id": "b8e4d32"}):
            _run(_agent(**extra), plugin, temp_dir)
            _run(_bash("git add f", **extra), plugin, temp_dir)

        compact = _run(_pre_compact(agent_id=SUBAGENT_ID), plugin, temp_dir)

        assert compact == ""
        assert "SPAWN-BODY" in _context(_run(_agent(agent_id=SUBAGENT_ID), plugin, temp_dir))
        assert "COMMIT-BODY" in _context(_run(_bash("git add f", agent_id=SUBAGENT_ID), plugin, temp_dir))
        assert _run(_agent(), plugin, temp_dir) == ""
        assert _run(_agent(agent_id="b8e4d32"), plugin, temp_dir) == ""

    def test_main_thread_pre_compact_changes_nothing(self, plugin: Path, temp_dir: Path) -> None:
        """Without ``agent_id`` PreCompact is the main thread's; the SessionStart compact after it does the clearing."""
        _run(_agent(), plugin, temp_dir)

        _run(_pre_compact(), plugin, temp_dir)

        assert _run(_agent(), plugin, temp_dir) == ""


@_requires_node
class TestOversizedRule:
    """A rule over the injection cap is replaced by a pointer, never cut mid-rule."""

    def test_oversized_rule_injects_summary_and_paths(self, plugin: Path, temp_dir: Path) -> None:
        """The model gets the frontmatter summary plus the rule and ``_full`` paths to Read instead of a cut body."""
        (plugin / "rules" / "agent-spawn.md").write_text(SPAWN_RULE + "x" * INJECT_CAP, encoding="utf-8")
        (plugin / "rules" / "_full").mkdir()
        (plugin / "rules" / "_full" / "agent-spawn.md").write_text("FULL\n", encoding="utf-8")

        context = _context(_run(_agent(), plugin, temp_dir))

        assert len(context) < 1000
        assert "too long to inject" in context
        assert "SPAWN-SUMMARY" in context
        assert str(plugin / "rules" / "agent-spawn.md") in context
        assert str(plugin / "rules" / "_full" / "agent-spawn.md") in context
        assert "SPAWN-BODY" not in context


@_requires_node
class TestFailOpen:
    """Every error path exits 0 with no output, so the hook can never block a tool."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("{not json", id="malformed-json"),
            pytest.param("", id="empty-stdin"),
            pytest.param(json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Agent"}), id="no-session-id"),
            pytest.param(
                json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": SESSION_ID, "prompt": "git commit"}),
                id="other-event",
            ),
        ],
    )
    def test_bad_or_foreign_payload_is_silent(self, plugin: Path, temp_dir: Path, payload: str) -> None:
        """Malformed, incomplete or foreign payloads produce nothing."""
        assert _run(payload, plugin, temp_dir) == ""

    def test_missing_rule_file_is_silent(self, tmp_path: Path, temp_dir: Path) -> None:
        """A plugin root without the rule injects nothing and claims no sentinel."""
        assert _run(_agent(), tmp_path / "absent", temp_dir) == ""
        assert list(temp_dir.iterdir()) == []

    def test_unwritable_temp_dir_is_silent(self, plugin: Path, tmp_path: Path) -> None:
        """No sentinel can be claimed, so nothing is injected rather than injecting on every call."""
        assert _run(_agent(), plugin, tmp_path / "missing-dir") == ""

    def test_missing_git_library_fails_open_for_bash_only(self, plugin: Path, temp_dir: Path, tmp_path: Path) -> None:
        """A hook copy without ``lib/shell-git.js`` stays silent on Bash, and Agent calls still inject.

        The library is required lazily inside the fail-open block; required at load time, its absence would crash the
        hook with exit 1 on every Agent and Bash call.
        """
        bare = tmp_path / "bare-hooks" / "rule-inject.js"
        bare.parent.mkdir()
        shutil.copyfile(HOOK, bare)

        bash_out = _run(_bash("git commit -m x"), plugin, temp_dir, hook=bare)
        agent_out = _run(_agent(), plugin, temp_dir, hook=bare)

        assert bash_out == ""
        assert "SPAWN-BODY" in _context(agent_out)


@_requires_node
class TestShippedRules:
    """The real rule files fit the injection cap, so the hook never falls back to the pointer for them."""

    @pytest.mark.parametrize(
        ("payload", "rule"),
        [
            pytest.param(_agent(), "agent-spawn.md", id="spawn"),
            pytest.param(_bash("git commit -m x"), "git-commit.md", id="commit"),
        ],
    )
    def test_shipped_rule_injects_in_full(self, temp_dir: Path, payload: str, rule: str) -> None:
        """Growing a shipped rule past the cap would silently swap its text for a pointer; this fails first."""
        body = (PLUGIN_ROOT / "rules" / rule).read_text(encoding="utf-8").split("\n---\n", 1)[1].strip()

        context = _context(_run(payload, PLUGIN_ROOT, temp_dir))

        assert len(context) <= INJECT_CAP
        assert context.endswith(body)


def test_hook_is_registered_on_calls_that_ran_and_on_context_resets() -> None:
    """Post events cover both triggering tools; PreToolUse must not, or a blocked call would spend the slot."""
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]

    def matchers(event: str) -> list[str]:
        return [
            group.get("matcher", "")
            for group in hooks.get(event, [])
            if any("hooks/rule-inject.js" in hook["command"] for hook in group["hooks"])
        ]

    assert matchers("PreToolUse") == []
    assert matchers("PostToolUse") == ["Agent|Bash"]
    assert matchers("PostToolUseFailure") == ["Agent|Bash"]
    assert matchers("SessionStart") == ["compact|clear"]
    assert matchers("PreCompact") == [""]


@_requires_node
class TestSkillContractRestore:
    """Compact restores only a current or explicitly advisory contract, preserving its raw text."""

    @pytest.mark.parametrize(
        ("stamp", "expected"),
        [
            pytest.param("verified", "verified", id="same-session"),
            pytest.param("absent", "unverified", id="no-owner"),
            pytest.param("stale", "unverified", id="sha-mismatch"),
            pytest.param("foreign", None, id="matching-sha-foreign-session"),
            pytest.param("malformed", "unverified", id="malformed-owner"),
            pytest.param("null", "unverified", id="null-owner"),
            pytest.param("missing-session", "unverified", id="incomplete-owner"),
            pytest.param("unreadable", "unverified", id="owner-is-directory"),
        ],
    )
    def test_owner_states(self, plugin: Path, temp_dir: Path, stamp: str, expected: str | None) -> None:
        """Both provenance and raw-byte integrity are needed for an authoritative restore."""
        state = plugin / ".temp" / "state"
        state.mkdir(parents=True)
        raw = b"  CONTRACT: preserve whitespace\r\nnext: resume\n\n"
        (state / "skill-contract.md").write_bytes(raw)
        owner = state / "skill-contract.owner"
        if stamp == "unreadable":
            owner.mkdir()
        elif stamp != "absent":
            data = {
                "session_id": "foreign-session" if stamp == "foreign" else SESSION_ID,
                "sha256": "0" * 64 if stamp == "stale" else hashlib.sha256(raw).hexdigest(),
                "written_at": "2000-01-01T00:00:00Z",
            }
            if stamp == "missing-session":
                del data["session_id"]
            owner.write_text("{broken" if stamp == "malformed" else json.dumps(None if stamp == "null" else data))
        out = _run(_session_start("compact"), plugin, temp_dir, cwd=plugin)
        if expected is None:
            assert out == ""
        else:
            header = (
                "Skill contract, verified: written by this session; confirm the skill is still active before resuming"
                if expected == "verified"
                else "Skill contract, unverified: no matching owner stamp; confirm with the user before resuming"
            )
            context = _context(out, "SessionStart")
            assert context == header + "\n\n" + raw.decode("utf-8")
            assert context.count(header) == 1

    @pytest.mark.parametrize("source", ["compact", "clear", "startup"])
    def test_absent_contract_is_silent(self, plugin: Path, temp_dir: Path, source: str) -> None:
        """A reset without skill state emits no additional context."""
        assert _run(_session_start(source), plugin, temp_dir, cwd=plugin) == ""

    def test_clear_never_restores(self, plugin: Path, temp_dir: Path) -> None:
        """Even a verified contract is not resumed after clear, while rule sentinels still reset."""
        state = plugin / ".temp" / "state"
        state.mkdir(parents=True)
        raw = b"CONTRACT\n"
        (state / "skill-contract.md").write_bytes(raw)
        (state / "skill-contract.owner").write_text(
            json.dumps({"session_id": SESSION_ID, "sha256": hashlib.sha256(raw).hexdigest()})
        )
        _run(_agent(), plugin, temp_dir)
        assert _run(_session_start("clear"), plugin, temp_dir, cwd=plugin) == ""
        assert list(temp_dir.iterdir()) == []

    @pytest.mark.parametrize("verified", [True, False])
    @pytest.mark.parametrize("body_size", [INJECT_CAP - 50, INJECT_CAP + 1])
    def test_over_cap_restores_header_and_read_path_only(
        self, plugin: Path, temp_dir: Path, verified: bool, body_size: int
    ) -> None:
        """The cap includes the header; a large contract gets its status and Read path, never a body fragment."""
        state = plugin / ".temp" / "state"
        state.mkdir(parents=True)
        raw = ("CONTRACT-BODY" + "x" * body_size).encode()
        contract = state / "skill-contract.md"
        contract.write_bytes(raw)
        if verified:
            (state / "skill-contract.owner").write_text(
                json.dumps({"session_id": SESSION_ID, "sha256": hashlib.sha256(raw).hexdigest()})
            )
        context = _context(_run(_session_start("compact"), plugin, temp_dir, cwd=plugin), "SessionStart")
        assert context.startswith(f"Skill contract, {'verified' if verified else 'unverified'}:")
        assert str(contract) in context
        assert "Read " in context
        assert "CONTRACT-BODY" not in context
        assert len(context) < 1000

    @pytest.mark.integration
    def test_real_writer_restores_exact_utf8_bytes(self, plugin: Path, temp_dir: Path) -> None:
        """The actual producer and Node consumer agree on non-ASCII content and the trailing LF."""
        subprocess.run(
            [
                sys.executable,
                str(PLUGIN_ROOT / "bin" / "write_skill_contract.py"),
                "sample:skill",
                "boundary",
                "n/a",
                "keep café → result",
                "resume",
            ],
            cwd=plugin,
            env={**os.environ, "CLAUDE_CODE_SESSION_ID": SESSION_ID},
            check=True,
            capture_output=True,
            timeout=15,
        )
        raw = (plugin / ".temp" / "state" / "skill-contract.md").read_bytes()
        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        context = _context(_run(_session_start("compact"), plugin, temp_dir, cwd=plugin), "SessionStart")
        assert context.startswith("Skill contract, verified:")
        assert context.split("\n\n", 1)[1].encode("utf-8") == raw

    def test_unreadable_contract_is_silent(self, plugin: Path, temp_dir: Path) -> None:
        """An inaccessible contract fails open without a nonzero exit."""
        (plugin / ".temp" / "state" / "skill-contract.md").mkdir(parents=True)
        assert _run(_session_start("compact"), plugin, temp_dir, cwd=plugin) == ""

    def test_sentinel_cleanup_failure_still_restores(self, plugin: Path, tmp_path: Path) -> None:
        """A missing sentinel directory cannot suppress an advisory compact restore."""
        state = plugin / ".temp" / "state"
        state.mkdir(parents=True)
        (state / "skill-contract.md").write_bytes(b"CONTRACT\n")
        context = _context(_run(_session_start("compact"), plugin, tmp_path / "missing", cwd=plugin), "SessionStart")
        assert context.startswith("Skill contract, unverified:")
        assert context.endswith("CONTRACT\n")

    def test_owner_uses_exact_session_id(self, plugin: Path, temp_dir: Path) -> None:
        """Sentinel-safe session IDs can collide without turning foreign ownership into verification."""
        state = plugin / ".temp" / "state"
        state.mkdir(parents=True)
        raw = b"CONTRACT\n"
        (state / "skill-contract.md").write_bytes(raw)
        (state / "skill-contract.owner").write_text(
            json.dumps({"session_id": "session/one", "sha256": hashlib.sha256(raw).hexdigest()})
        )
        assert _run(_session_start("compact", "session_one"), plugin, temp_dir, cwd=plugin) == ""
        context = _context(_run(_session_start("compact", "session/one"), plugin, temp_dir, cwd=plugin), "SessionStart")
        assert context.startswith("Skill contract, verified:")


def _contract_path_capability(kind: str) -> bool:
    """Probe special-file creation before collection, including Windows restrictions."""
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "candidate"
        try:
            if kind == "fifo":
                if not hasattr(os, "mkfifo"):
                    return False
                os.mkfifo(candidate)
            else:
                target = Path(directory) / "target"
                target.write_bytes(b"test")
                candidate.symlink_to(target)
        except OSError:
            return False
    return True


@pytest.mark.skipif(shutil.which("node") is None, reason="node executable not available")
@pytest.mark.parametrize("target", ["contract", "owner"])
@pytest.mark.parametrize(
    "kind",
    [
        "oversize",
        pytest.param(
            "symlink",
            marks=pytest.mark.skipif(not _contract_path_capability("symlink"), reason="symlink creation unavailable"),
        ),
        pytest.param(
            "fifo", marks=pytest.mark.skipif(not _contract_path_capability("fifo"), reason="FIFO creation unavailable")
        ),
    ],
)
def test_unsafe_contract_paths_fail_open(tmp_path: Path, target: str, kind: str) -> None:
    """Reject unsafe contract paths and retain advisory text for unsafe owner paths without blocking."""
    project = tmp_path / "project"
    state = project / ".temp" / "state"
    state.mkdir(parents=True)
    raw = b"CONTRACT: continue safely\n"
    contract = state / "skill-contract.md"
    owner = state / "skill-contract.owner"
    stamp = json.dumps({"session_id": "safe-session", "sha256": hashlib.sha256(raw).hexdigest()}).encode()
    contract.write_bytes(raw)
    owner.write_bytes(stamp)
    candidate = contract if target == "contract" else owner
    content = raw if target == "contract" else stamp
    candidate.unlink()
    if kind == "symlink":
        backing = state / "backing"
        backing.write_bytes(content)
        candidate.symlink_to(backing)
    elif kind == "fifo":
        os.mkfifo(candidate)
    else:
        cap = 256 * 1024 if target == "contract" else 4 * 1024
        candidate.write_bytes(content + b" " * (cap + 1 - len(content)))
    payload = {"session_id": "safe-session", "hook_event_name": "SessionStart", "source": "compact"}
    transcript = project / "transcript.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    payload["transcript_path"] = str(transcript)
    result = subprocess.run(
        ["node", str(PLUGIN_ROOT / "hooks" / "rule-inject.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=project,
        env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "TMPDIR": str(tmp_path)},
        # Include Node startup on loaded CI runners while retaining a finite deadline for FIFO regressions.
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout
    if target == "contract":
        assert "CONTRACT:" not in output
        assert "Skill contract," not in output
        assert "## Skill Compaction Contract" not in output
    else:
        assert "CONTRACT: continue safely" in output
        assert "Skill contract, unverified:" in output
        assert "Skill contract, verified:" not in output
        assert "verified for session" not in output


@pytest.mark.parametrize("hook", ["rule-inject.js", "task-log.js"])
def test_owner_stamp_comment_documents_workspace_writer_boundary(hook: str) -> None:
    """Both consumers explain that matching session hashes do not authenticate against workspace writers."""
    source = (PLUGIN_ROOT / "hooks" / hook).read_text(encoding="utf-8")
    function = source.split("function skillContract(dir, sessionId) {", 1)[1].split("const file", 1)[0]
    assert "owner stamp binds session-writer bytes" in function
    assert "not authentic against a writer with workspace access" in function


def test_owner_stamp_contract_documents_workspace_writer_boundary() -> None:
    """The canonical ownership paragraph states the same trust boundary as both readers."""
    contract = (PLUGIN_ROOT / "skills" / "_shared" / "compaction-contract.md").read_text(encoding="utf-8")
    paragraph = next(line for line in contract.splitlines() if "skill-contract.owner" in line)
    assert "binds bytes produced by the session writer" in paragraph
    assert "does not authenticate the contract against a writer with workspace access" in paragraph
