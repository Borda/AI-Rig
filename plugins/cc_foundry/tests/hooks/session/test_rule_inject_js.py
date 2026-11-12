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
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest
from _hook_env import _contract_path_capability

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
HOOK = PLUGIN_ROOT / "hooks" / "rule-inject.js"
COMMIT_GUARD = PLUGIN_ROOT / "hooks" / "commit-guard.js"
SESSION_ID = "6b0f3c1e-2a7d-4e59-8c14-9d3b5a7e2f60"
OTHER_SESSION_ID = "0c2d4e6f-1a3b-4c5d-8e9f-0a1b2c3d4e5f"
SUBAGENT_ID = "a7f3c21"
SIBLING_ID = "b8e4d32"
VERIFIED_HEADER = "Skill contract, verified: written by this session; confirm the skill is still active before resuming"
UNVERIFIED_HEADER = "Skill contract, unverified: no matching owner stamp; confirm with the user before resuming"
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


def _spend_both_rules(plugin: Path, temp_dir: Path, **extra: str) -> None:
    """Consume the spawn and commit slots for one context; ``agent_id=...`` places them inside a subagent."""
    _run(_agent(**extra), plugin, temp_dir)
    _run(_bash("git add f", **extra), plugin, temp_dir)


# Owner-stamp planters: each takes the owner file path and the contract bytes and leaves the stamp in one state.
def _owner_absent(path: Path, raw: bytes) -> None:
    """Leave the contract without any owner stamp."""


def _owner_stamp(path: Path, raw: bytes, **changes: str) -> None:
    """Write a valid same-session stamp for ``raw``; ``changes`` override one field."""
    data = {
        "session_id": SESSION_ID,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "written_at": "2000-01-01T00:00:00Z",
        **changes,
    }
    path.write_text(json.dumps(data))


def _owner_literal(path: Path, raw: bytes, text: str) -> None:
    """Write ``text`` verbatim as the stamp, valid JSON or not."""
    path.write_text(text)


def _owner_without_session(path: Path, raw: bytes) -> None:
    """Write a stamp that carries a digest and timestamp but no session id."""
    path.write_text(json.dumps({"sha256": hashlib.sha256(raw).hexdigest(), "written_at": "2000-01-01T00:00:00Z"}))


def _owner_directory(path: Path, raw: bytes) -> None:
    """Make the stamp path a directory, which cannot be read as a file."""
    path.mkdir()


def _seed_contract(root: Path, raw: bytes, owner: Callable[[Path, bytes], None] = _owner_absent) -> Path:
    """Write ``raw`` as ``root``'s skill contract, stamp it with ``owner``, and return the contract path."""
    state = root / ".temp" / "state"
    state.mkdir(parents=True)
    contract = state / "skill-contract.md"
    contract.write_bytes(raw)
    owner(state / "skill-contract.owner", raw)
    return contract


@_requires_node
class TestSpawnRule:
    """The spawn rule injects on the first Agent call of each session and subagent."""

    def test_first_call_injects_body_without_frontmatter(self, plugin: Path, temp_dir: Path) -> None:
        """The first spawn gets the whole rule body, headed by its file name, and no frontmatter.

        The frontmatter is loader metadata; only the rule text is context for the model.
        """
        context = _context(_run(_agent(), plugin, temp_dir))

        assert context.startswith("foundry rule agent-spawn.md")
        assert context.endswith("SPAWN-BODY")
        assert "paths:" not in context

    def test_each_subagent_gets_the_rule_once(self, plugin: Path, temp_dir: Path) -> None:
        """A subagent has its own context: it gets the rule even after the main thread did, but only once.

        The main thread spawns first, so a sentinel keyed on the session alone would wrongly silence the subagent.
        """
        _run(_agent(), plugin, temp_dir)

        first = _run(_agent(agent_id=SUBAGENT_ID), plugin, temp_dir)
        second = _run(_agent(agent_id=SUBAGENT_ID), plugin, temp_dir)

        assert "SPAWN-BODY" in _context(first)
        assert second == ""

    def test_another_session_gets_its_own_injection(self, plugin: Path, temp_dir: Path) -> None:
        """Sentinels are scoped by session id, so a concurrent session is unaffected.

        Two sessions share one temp dir; the second must still receive the rule after the first consumed its slot.
        """
        _run(_agent(), plugin, temp_dir)

        out = _run(_agent(session_id=OTHER_SESSION_ID), plugin, temp_dir)

        assert "SPAWN-BODY" in _context(out)


@_requires_node
class TestInjectOnce:
    """A rule is already in context after its first injection, so a repeat adds nothing."""

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param(_agent(), _agent(), id="second-spawn"),
            pytest.param(_bash("git add a.py"), _bash("git push"), id="push-after-stage"),
            pytest.param(
                _bash("git commit -m x", event="PostToolUseFailure", error="Exit code 1"),
                _bash("git commit -m y"),
                id="success-after-failed-commit",
            ),
        ],
    )
    def test_repeat_is_silent(self, plugin: Path, temp_dir: Path, first: str, second: str) -> None:
        """The second activity of a kind in one session injects nothing.

        Covers a repeated spawn, a push after a stage (one commit rule for the whole git workflow), and a successful
        commit after a rejected one, whose failure event already delivered the rule.
        """
        _run(first, plugin, temp_dir)

        assert _run(second, plugin, temp_dir) == ""


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
    def test_commit_creating_invocation_injects(self, plugin: Path, temp_dir: Path, command: str) -> None:
        """Any shell segment that stages, commits, pushes or writes a commit or tag message delivers the commit rule.

        Grouping, a command prefix, quoting, nested shell source or a global option cannot hide the git call, and every
        subcommand that writes a message (merge, revert, cherry-pick, am, rebase, annotated tag) needs the rule that
        governs it. A detector reading only the first word of an operator-split segment missed these forms, so the rule
        never arrived for a session spelling the call this way.
        """
        context = _context(_run(_bash(command), plugin, temp_dir))

        assert context.startswith("foundry rule git-commit.md")
        assert context.endswith("COMMIT-BODY")

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
        _run(_agent(session_id=OTHER_SESSION_ID), plugin, temp_dir)

        _run(_session_start("compact"), plugin, temp_dir)

        assert _run(_agent(session_id=OTHER_SESSION_ID), plugin, temp_dir) == ""

    def test_subagent_pre_compact_clears_only_that_subagent(self, plugin: Path, temp_dir: Path) -> None:
        """A subagent compacting its own context gets its rules again; main and sibling subagents keep theirs.

        A subagent's auto-compaction is no SessionStart. PreCompact carrying ``agent_id`` is the one payload naming the
        compacting subagent; the hooks reference does not state that it fires there, so this pins the handler only.
        """
        _spend_both_rules(plugin, temp_dir)
        _spend_both_rules(plugin, temp_dir, agent_id=SUBAGENT_ID)
        _spend_both_rules(plugin, temp_dir, agent_id=SIBLING_ID)

        compact = _run(_pre_compact(agent_id=SUBAGENT_ID), plugin, temp_dir)

        assert compact == ""
        assert "SPAWN-BODY" in _context(_run(_agent(agent_id=SUBAGENT_ID), plugin, temp_dir))
        assert "COMMIT-BODY" in _context(_run(_bash("git add f", agent_id=SUBAGENT_ID), plugin, temp_dir))
        assert _run(_agent(), plugin, temp_dir) == ""
        assert _run(_agent(agent_id=SIBLING_ID), plugin, temp_dir) == ""

    def test_main_thread_pre_compact_changes_nothing(self, plugin: Path, temp_dir: Path) -> None:
        """Without ``agent_id`` PreCompact is the main thread's; the SessionStart compact after it does the clearing."""
        _run(_agent(), plugin, temp_dir)

        _run(_pre_compact(), plugin, temp_dir)

        assert _run(_agent(), plugin, temp_dir) == ""


@_requires_node
class TestOversizedRule:
    """A rule over the injection cap is replaced by a pointer, never cut mid-rule."""

    def test_injects_summary_and_paths(self, plugin: Path, temp_dir: Path) -> None:
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
        ("owner", "header"),
        [
            pytest.param(_owner_stamp, VERIFIED_HEADER, id="same-session"),
            pytest.param(_owner_absent, UNVERIFIED_HEADER, id="no-owner"),
            pytest.param(partial(_owner_stamp, sha256="0" * 64), UNVERIFIED_HEADER, id="sha-mismatch"),
            pytest.param(partial(_owner_literal, text="{broken"), UNVERIFIED_HEADER, id="malformed-owner"),
            pytest.param(partial(_owner_literal, text="null"), UNVERIFIED_HEADER, id="null-owner"),
            pytest.param(_owner_without_session, UNVERIFIED_HEADER, id="incomplete-owner"),
            pytest.param(_owner_directory, UNVERIFIED_HEADER, id="owner-is-directory"),
        ],
    )
    def test_restores_raw_text_under_owner_status_header(
        self, plugin: Path, temp_dir: Path, owner: Callable[[Path, bytes], None], header: str
    ) -> None:
        """Both provenance and raw-byte integrity are needed for an authoritative restore.

        Only a stamp from this session whose digest matches the bytes earns the verified header; every other readable
        state restores the same bytes under the advisory one so the user confirms before resuming.
        """
        raw = b"  CONTRACT: preserve whitespace\r\nnext: resume\n\n"
        _seed_contract(plugin, raw, owner)

        context = _context(_run(_session_start("compact"), plugin, temp_dir, cwd=plugin), "SessionStart")

        boundary = re.search(r"<(untrusted-[0-9a-f]{32})>", context)
        assert boundary is not None
        assert context == header + f"\n\n<{boundary.group(1)}>\n" + raw.decode("utf-8") + f"</{boundary.group(1)}>"
        assert context.count(header) == 1

    def test_foreign_session_owner_is_silent(self, plugin: Path, temp_dir: Path) -> None:
        """A stamp with a matching digest from another session restores nothing.

        The bytes are intact, but they belong to a different session's skill run, so resuming them would be wrong.
        """
        _seed_contract(plugin, b"CONTRACT\n", partial(_owner_stamp, session_id="foreign-session"))

        assert _run(_session_start("compact"), plugin, temp_dir, cwd=plugin) == ""

    @pytest.mark.parametrize("source", ["compact", "clear", "startup"])
    def test_absent_contract_is_silent(self, plugin: Path, temp_dir: Path, source: str) -> None:
        """A reset without skill state emits no additional context."""
        assert _run(_session_start(source), plugin, temp_dir, cwd=plugin) == ""

    def test_clear_never_restores(self, plugin: Path, temp_dir: Path) -> None:
        """Even a verified contract is not resumed after clear, while rule sentinels still reset.

        A clear starts a new conversation, so resuming the old skill run would be wrong; the spawn sentinel written
        before it must still be wiped.
        """
        _seed_contract(plugin, b"CONTRACT\n", _owner_stamp)
        _run(_agent(), plugin, temp_dir)

        out = _run(_session_start("clear"), plugin, temp_dir, cwd=plugin)

        assert out == ""
        assert list(temp_dir.iterdir()) == []

    @pytest.mark.parametrize(
        ("owner", "status"),
        [
            pytest.param(_owner_stamp, "verified", id="verified"),
            pytest.param(_owner_absent, "unverified", id="unverified"),
        ],
    )
    @pytest.mark.parametrize(
        "body_size",
        [pytest.param(INJECT_CAP - 50, id="body-near-cap"), pytest.param(INJECT_CAP + 1, id="body-over-cap")],
    )
    def test_over_cap_restores_header_and_read_path_only(
        self, plugin: Path, temp_dir: Path, owner: Callable[[Path, bytes], None], status: str, body_size: int
    ) -> None:
        """The cap includes the header; a large contract gets its status and Read path, never a body fragment.

        A body just under the cap still overflows once the header is added, so both sizes must fall back to the pointer.
        """
        contract = _seed_contract(plugin, ("CONTRACT-BODY" + "x" * body_size).encode(), owner)

        context = _context(_run(_session_start("compact"), plugin, temp_dir, cwd=plugin), "SessionStart")

        assert context.startswith(f"Skill contract, {status}:")
        assert str(contract) in context
        assert "Read " in context
        assert "CONTRACT-BODY" not in context
        assert len(context) < 1000

    @pytest.mark.integration
    def test_real_writer_restores_exact_utf8_bytes(self, plugin: Path, temp_dir: Path) -> None:
        """The actual producer and Node consumer agree on non-ASCII content and the trailing LF.

        The contract is written by the shipped ``write_skill_contract.py``, not seeded by hand, so a format drift on
        either side of the writer/reader seam fails here.
        """
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

        context = _context(_run(_session_start("compact"), plugin, temp_dir, cwd=plugin), "SessionStart")

        assert raw.endswith(b"\n")
        assert b"\r\n" not in raw
        assert context.startswith("Skill contract, verified:")
        fenced = context.split("\n\n", 1)[1]
        boundary, body = fenced.split("\n", 1)
        assert re.fullmatch(r"<untrusted-[0-9a-f]{32}>", boundary)
        assert body.endswith(boundary.replace("<", "</", 1))
        assert body.removesuffix(boundary.replace("<", "</", 1)).encode("utf-8") == raw

    def test_unreadable_contract_is_silent(self, plugin: Path, temp_dir: Path) -> None:
        """An inaccessible contract fails open without a nonzero exit.

        The contract path is a directory, so reading it throws; the hook must swallow that and stay silent.
        """
        (plugin / ".temp" / "state" / "skill-contract.md").mkdir(parents=True)

        assert _run(_session_start("compact"), plugin, temp_dir, cwd=plugin) == ""

    def test_sentinel_cleanup_failure_still_restores(self, plugin: Path, tmp_path: Path) -> None:
        """A missing sentinel directory cannot suppress an advisory compact restore.

        Sentinel cleanup and contract restore are independent; the TMPDIR here does not exist, so cleanup fails first.
        """
        _seed_contract(plugin, b"CONTRACT\n")

        out = _run(_session_start("compact"), plugin, tmp_path / "missing", cwd=plugin)

        context = _context(out, "SessionStart")
        assert context.startswith("Skill contract, unverified:")
        assert re.search(r"CONTRACT\n</untrusted-[0-9a-f]{32}>$", context)

    def test_sentinel_safe_id_collision_is_not_verified(self, plugin: Path, temp_dir: Path) -> None:
        """Session ids that collapse to the same sentinel-safe name do not verify each other's contract.

        ``session/one`` and ``session_one`` map to one sentinel suffix; ownership must compare the exact id.
        """
        _seed_contract(plugin, b"CONTRACT\n", partial(_owner_stamp, session_id="session/one"))

        assert _run(_session_start("compact", "session_one"), plugin, temp_dir, cwd=plugin) == ""

    def test_exact_session_id_verifies(self, plugin: Path, temp_dir: Path) -> None:
        """The session id that wrote the stamp, even with a path separator in it, gets the verified header.

        Counterpart of the collision case: exact comparison must still accept the true owner.
        """
        _seed_contract(plugin, b"CONTRACT\n", partial(_owner_stamp, session_id="session/one"))

        out = _run(_session_start("compact", "session/one"), plugin, temp_dir, cwd=plugin)

        assert _context(out, "SessionStart").startswith("Skill contract, verified:")


def _damage_oversize(path: Path, content: bytes, cap: int) -> None:
    """Replace ``path`` with ``content`` padded one byte past ``cap``."""
    path.write_bytes(content + b" " * (cap + 1 - len(content)))


def _damage_symlink(path: Path, content: bytes) -> None:
    """Replace ``path`` with a symlink to a sibling file holding ``content``."""
    backing = path.with_name("backing")
    backing.write_bytes(content)
    path.symlink_to(backing)


def _damage_fifo(path: Path, content: bytes) -> None:
    """Replace ``path`` with a FIFO, which blocks any reader that opens it naively."""
    os.mkfifo(path)


_requires_symlinks = pytest.mark.skipif(not _contract_path_capability("symlink"), reason="symlink creation unavailable")
_requires_fifo = pytest.mark.skipif(not _contract_path_capability("fifo"), reason="FIFO creation unavailable")


def _damage_cases(cap: int) -> list:
    """Return the unsafe-path shapes to plant, with the oversize shape sized one byte past ``cap``."""
    return [
        pytest.param(partial(_damage_oversize, cap=cap), id="oversize"),
        pytest.param(_damage_symlink, id="symlink", marks=_requires_symlinks),
        pytest.param(_damage_fifo, id="fifo", marks=_requires_fifo),
    ]


def _run_on_damaged_state(tmp_path: Path, damaged: str, damage: Callable[[Path, bytes], None]) -> str:
    """Seed a verified contract, replace its ``damaged`` file with an unsafe shape, run compact, return stdout."""
    project = tmp_path / "project"
    contract = _seed_contract(project, b"CONTRACT: continue safely\n", partial(_owner_stamp, session_id="safe-session"))
    path = contract.parent / damaged
    content = path.read_bytes()
    path.unlink()
    damage(path, content)
    transcript = project / "transcript.jsonl"
    transcript.write_bytes(b'{"type":"user","message":{"content":"hello"}}\n')
    payload = {
        "session_id": "safe-session",
        "hook_event_name": "SessionStart",
        "source": "compact",
        "transcript_path": str(transcript),
    }
    result = subprocess.run(
        ["node", str(HOOK)],
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
    return result.stdout


@_requires_node
class TestUnsafeContractPaths:
    """An oversize file, symlink or FIFO in the contract state never blocks or leaks through the compact restore."""

    @pytest.mark.parametrize("damage", _damage_cases(256 * 1024))
    def test_unsafe_contract_is_not_restored(self, tmp_path: Path, damage: Callable[[Path, bytes], None]) -> None:
        """An unsafe contract file is rejected and no contract text reaches the model.

        A symlink could point outside the project, a FIFO would hang the hook, and an oversize file would flood the
        context; each is dropped silently and the hook still exits 0.
        """
        output = _run_on_damaged_state(tmp_path, "skill-contract.md", damage)

        assert "CONTRACT:" not in output
        assert "Skill contract," not in output
        assert "## Skill Compaction Contract" not in output

    @pytest.mark.parametrize("damage", _damage_cases(4 * 1024))
    def test_unsafe_owner_keeps_advisory_contract(self, tmp_path: Path, damage: Callable[[Path, bytes], None]) -> None:
        """An unsafe owner stamp is ignored, so the intact contract is restored under the unverified header.

        The contract text is safe to show; only the unreadable stamp cannot vouch for it, so the user confirms.
        """
        output = _run_on_damaged_state(tmp_path, "skill-contract.owner", damage)

        assert "CONTRACT: continue safely" in output
        assert "Skill contract, unverified:" in output
        assert "Skill contract, verified:" not in output
        assert "verified for session" not in output


@pytest.mark.parametrize("hook", ["rule-inject.js", "task-log.js"])
def test_owner_stamp_comment_documents_workspace_writer_boundary(hook: str) -> None:
    """Both consumers explain that matching session hashes do not authenticate against workspace writers.

    The owner stamp binds bytes, not authors; the source comment is the only place that trust boundary is recorded.
    """
    source = (PLUGIN_ROOT / "hooks" / hook).read_text(encoding="utf-8")
    function = source.split("function skillContract(dir, sessionId) {", 1)[1].split("const file", 1)[0]

    assert "owner stamp binds session-writer bytes" in function
    assert "not authentic against a writer with workspace access" in function


def test_owner_stamp_contract_documents_workspace_writer_boundary() -> None:
    """The canonical ownership paragraph states the same trust boundary as both readers.

    Reads the shared contract line by line so a reflow of surrounding prose cannot break the assertion.
    """
    contract = (PLUGIN_ROOT / "skills" / "_shared" / "compaction-contract.md").read_text(encoding="utf-8")
    paragraph = next(line for line in contract.splitlines() if "skill-contract.owner" in line)

    assert "binds bytes produced by the session writer" in paragraph
    assert "does not authenticate the contract against a writer with workspace access" in paragraph
