"""Subprocess tests for ``hooks/commit-guard.js``.

The hook does NOT gate ``git commit`` at all — commit authorization is
prompt-discipline only (see ``rules/git-commit.md``), no runtime check.
The hook gates only ``git push``, behind a per-repo / per-branch sentinel
file named ``claude-push-auth-<repo-slug>-<branch-slug>`` under the hook's own
sentinel dir (``/tmp`` on POSIX, ``os.tmpdir()`` on Windows). Each test
spins up a small disposable git repo so the hook can resolve
``git rev-parse --show-toplevel`` and ``git branch --show-current``.

Behavioural areas covered:

* **Commit passthrough** — ``git commit`` always exits 0, sentinel or not;
  the hook never inspects it.
* **Push gating** — force-push is blocked unconditionally on any branch
  (even with a valid sentinel); a regular push requires a fresh push
  sentinel and is never auto-armed by a ``"push"``-mentioning prompt.
* **SessionStart wipe** — clears leftover push sentinels from prior runs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from _hook_env import _hook_tmp_base

GIT_UNAVAILABLE = subprocess.run(["git", "--version"], capture_output=True, timeout=5).returncode != 0

# Repo/branch slugs come from the hook's own toSlug() over `git rev-parse` and
# `git branch --show-current`; the git_repo fixture pins them to myrepo/main.
_SENTINEL_NAME = "claude-push-auth-myrepo-main"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(name="git_repo")
def _git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo named ``myrepo`` on branch ``main`` with one commit."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("test", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    return repo


@pytest.fixture(name="push_sentinel")
def _push_sentinel() -> Iterator[Path]:
    """Yield the push sentinel path, removing any leftover before AND after each test.

    Resolved through ``_hook_tmp_base()`` rather than a module-level ``/tmp`` literal so the path follows the hook's own
    ``getSentinelDir()`` on Windows too. Kept lazy — the base is computed inside the fixture, after the module-level git
    skipif has run.
    """
    path = _hook_tmp_base() / _SENTINEL_NAME
    path.unlink(missing_ok=True)
    yield path
    path.unlink(missing_ok=True)


# ── Payload helpers ──────────────────────────────────────────────────────────


def _bash_commit(cmd: str = "git commit -m 'test'") -> dict:
    """Build a ``PreToolUse(Bash)`` payload for a commit-style command.

    Examples:
        >>> _bash_commit("git commit -m x")["tool_input"]["command"]
        'git commit -m x'
    """
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
    }


def _bash_push(cmd: str = "git push") -> dict:
    """Build a ``PreToolUse(Bash)`` payload for a push-style command.

    Examples:
        >>> _bash_push()["tool_input"]["command"]
        'git push'
    """
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
    }


def _session_start() -> dict:
    """Build a ``SessionStart`` payload.

    Examples:
        >>> _session_start()
        {'hook_event_name': 'SessionStart'}
    """
    return {"hook_event_name": "SessionStart"}


def _user_prompt(prompt_text: str) -> dict:
    """Build a ``UserPromptSubmit`` payload.

    Examples:
        >>> _user_prompt("continue")["user_message"]
        'continue'
    """
    return {"hook_event_name": "UserPromptSubmit", "user_message": prompt_text}


# ── Tests ─────────────────────────────────────────────────────────────────────


_skip_git_unavailable = pytest.mark.skipif(
    GIT_UNAVAILABLE,
    reason="requires functional git (XCode CLI tools or equivalent)",
)


@_skip_git_unavailable
@pytest.mark.usefixtures("push_sentinel")
class TestCommitGuard:
    """commit-guard.js: push-only sentinel gate; commit is prompt-discipline only."""

    def test_commit_always_passes_through(self, git_repo: Path, run_hook) -> None:
        """Git commit exits 0 unconditionally — the hook never inspects it, no sentinel involved."""
        result = run_hook("commit-guard.js", _bash_commit(), cwd=git_repo)

        assert result.returncode == 0, result.stderr

    def test_non_push_bash_passes_through(self, git_repo: Path, run_hook) -> None:
        """Non-push Bash command bypasses the gate regardless of sentinel state."""
        result = run_hook("commit-guard.js", _bash_push(cmd="echo hello"), cwd=git_repo)

        assert result.returncode == 0, result.stderr

    def test_session_start_clears_push_sentinel(self, git_repo: Path, run_hook, push_sentinel: Path) -> None:
        """SessionStart wipes any leftover push sentinel from a prior session."""
        push_sentinel.touch()
        assert push_sentinel.exists()

        result = run_hook("commit-guard.js", _session_start(), cwd=git_repo)

        assert result.returncode == 0, result.stderr
        assert not push_sentinel.exists()

    def test_force_push_blocked_any_branch(self, git_repo: Path, run_hook) -> None:
        """Force-push with no sentinel → exit 2 with a 'force'/'forbidden' message."""
        result = run_hook("commit-guard.js", _bash_push(cmd="git push --force"), cwd=git_repo)

        assert result.returncode == 2
        assert "force" in result.stderr
        assert "forbidden" in result.stderr

    def test_force_push_blocked_even_with_sentinel(self, git_repo: Path, run_hook, push_sentinel: Path) -> None:
        """A valid push sentinel does NOT bypass the force block — force check runs first → exit 2."""
        push_sentinel.touch()

        result = run_hook("commit-guard.js", _bash_push(cmd="git push --force"), cwd=git_repo)

        assert result.returncode == 2

    def test_push_blocked_without_sentinel(self, git_repo: Path, run_hook) -> None:
        """Plain push with no sentinel → exit 2 and stderr points at AskUserQuestion."""
        result = run_hook("commit-guard.js", _bash_push(), cwd=git_repo)

        assert result.returncode == 2
        assert "AskUserQuestion" in result.stderr

    def test_push_allowed_with_fresh_sentinel(self, git_repo: Path, run_hook, push_sentinel: Path) -> None:
        """Plain push with a fresh push sentinel (< 15-min TTL) → exit 0."""
        push_sentinel.touch()

        result = run_hook("commit-guard.js", _bash_push(), cwd=git_repo)

        assert result.returncode == 0, result.stderr

    def test_push_not_auto_armed_by_prompt(self, git_repo: Path, run_hook, push_sentinel: Path) -> None:
        """UserPromptSubmit mentioning 'push' must NOT auto-arm the push sentinel."""
        result = run_hook("commit-guard.js", _user_prompt("push this"), cwd=git_repo)

        assert result.returncode == 0, result.stderr
        assert not push_sentinel.exists()


@_skip_git_unavailable
@pytest.mark.usefixtures("push_sentinel")
class TestForcePushSpelling:
    """The force-push block must gate the action, not one spelling of it.

    Each command below reaches the remote with a non-fast-forward update, so each must be blocked even when a valid push
    sentinel is present — the force check runs before the sentinel lookup.
    """

    @pytest.mark.parametrize(
        "command",
        [
            "git push --force",
            "git push -f",
            "git push --force-with-lease",
            "git push --force-if-includes origin main",
            "git -C /some/path push --force",
            "git --git-dir=/some/.git push --force",
            "git -c user.name=x push --force",
            "cd /somewhere && git push --force",
            "echo hi; git push --force",
            "git push origin +main",
            "git -C /some/path push origin +main",
            "git push -fu origin main",
            "git push -uf origin main",
            "/usr/bin/git push --force origin main",
            "env git push --force origin main",
            "env -i PATH=/bin git push --force origin main",
            "GIT_TRACE=1 git push --force origin main",
            "echo $(git push --force origin main)",
        ],
    )
    def test_force_push_spellings_blocked_with_sentinel(
        self, git_repo: Path, run_hook, push_sentinel: Path, command: str
    ) -> None:
        """Every force spelling is blocked even with a fresh sentinel present."""
        push_sentinel.touch()

        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 2, f"{command!r} was not blocked: {result.stdout}{result.stderr}"
        assert "force" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /some/path push",
            "cd /somewhere && git push",
            "git push --follow-tags origin main",
            "git push -u origin main",
        ],
    )
    def test_non_force_push_spellings_still_need_sentinel(self, git_repo: Path, run_hook, command: str) -> None:
        """A push reached via a global flag or a chain is still a push — sentinel required.

        ``--follow-tags`` and ``-u`` guard the force test against over-reach: a long option that merely contains an f,
        and a short option that is not f, must reach the sentinel gate rather than the unconditional force block.
        """
        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 2, f"{command!r} bypassed the sentinel gate"
        assert "AskUserQuestion" in result.stderr

    @pytest.mark.parametrize("command", ["git log --oneline", "git status", "echo 'git push --force'"])
    def test_non_push_commands_still_pass(self, git_repo: Path, run_hook, command: str) -> None:
        """Commands that are not a push are untouched.

        ``echo 'git push --force'`` is quoted text, not a push. It passes because no segment resolves to a ``git``
        argv[0] — quoting is not parsed, so the protection here is incidental rather than a quoting guarantee. A
        substitution that really does run git (``echo $(git push ...)``) is caught, and is covered separately.
        """
        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 0, result.stderr
