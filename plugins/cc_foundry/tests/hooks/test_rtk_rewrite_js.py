"""Subprocess tests for ``hooks/rtk-rewrite.js``.

The hook is a ``PreToolUse`` gate that rewrites read-heavy Bash commands to
their ``rtk <cmd>`` equivalents and auto-approves them. Its security contract:

* **Read-only rewrite** — only commands the hook can prove read-only are
  rewritten with ``permissionDecision:"allow"``; everything else passes
  through unchanged (empty stdout, exit 0) so the real allow/deny matcher sees
  the original string.
* **No deny bypass** — because a rewrite mutates the command string (and thus
  dodges the settings.json deny list), mutating subcommands on mixed CLIs
  (``git push``, ``gh pr comment``, ``gh api -X POST``, ``docker rm`` …) must
  NEVER be rewritten. They passthrough to real permission checking.
* **No result corruption** — ``diff`` is never rewritten (rtk alters its exit
  status / "identical" summary).

These tests require ``rtk`` to be resolvable on ``PATH``; when it is not, the
hook is a deliberate no-op and the behavioural contract cannot be exercised, so
the suite is skipped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent.parent / "hooks" / "rtk-rewrite.js"

_skip_node_unavailable = pytest.mark.skipif(shutil.which("node") is None, reason="requires node to execute the hook")
_skip_rtk_unavailable = pytest.mark.skipif(
    shutil.which("rtk") is None, reason="hook is a no-op without rtk on PATH — contract not exercisable"
)


def _run_payload(payload: dict) -> subprocess.CompletedProcess:
    """Feed one PreToolUse payload to the hook and return the finished process."""
    return subprocess.run(["node", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=10)


def _run(command: str) -> dict:
    """Invoke the hook with a Bash tool payload and return parsed stdout (or {})."""
    proc = _run_payload({"tool_name": "Bash", "tool_input": {"command": command}})
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _rewritten_to(result: dict) -> str | None:
    """Extract the rewritten command from a hook result, or None on passthrough.

    Examples:
        >>> _rewritten_to({"hookSpecificOutput": {"updatedInput": {"command": "rtk git status"}}})
        'rtk git status'
        >>> _rewritten_to({}) is None
        True
    """
    try:
        return result["hookSpecificOutput"]["updatedInput"]["command"]
    except (KeyError, TypeError):
        return None


@_skip_rtk_unavailable
@_skip_node_unavailable
class TestRewrite:
    """Provably read-only commands get an ``rtk`` prefix and an allow decision."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git status", id="git-status"),
            pytest.param("git diff HEAD", id="git-diff"),
            pytest.param("git log --oneline -5", id="git-log"),
            pytest.param("git show HEAD", id="git-show"),
            pytest.param("gh pr view 42", id="gh-pr-view"),
            pytest.param("gh issue list", id="gh-issue-list"),
            pytest.param("gh api repos/owner/repo", id="gh-api-get"),
            pytest.param("docker ps", id="docker-ps"),
            pytest.param("kubectl get pods", id="kubectl-get"),
            pytest.param("aws ec2 describe-instances", id="aws-describe"),
            pytest.param("aws s3 ls", id="aws-s3-ls"),
            pytest.param("pytest tests/", id="pytest"),
            pytest.param("ruff check .", id="ruff-check"),
            pytest.param("grep -r foo .", id="grep"),
            pytest.param("ls -la", id="ls"),
            pytest.param("cargo tree", id="cargo-tree"),
            pytest.param("cargo metadata --no-deps", id="cargo-metadata"),
            pytest.param("next info", id="next-info"),
        ],
    )
    def test_readonly_command_is_rewritten_and_allowed(self, command: str) -> None:
        """A provably read-only command is rewritten to ``rtk <command>`` with an allow decision.

        Plain read verbs of mixed CLIs and the inspection subcommands of cargo/next (whose execution subcommands pass
        through) earn the rewrite; the decision and the exact rewritten string are both part of the contract.
        """
        result = _run(command)
        assert (result["hookSpecificOutput"]["permissionDecision"], _rewritten_to(result)) == (
            "allow",
            f"rtk {command}",
        )


@_skip_rtk_unavailable
@_skip_node_unavailable
class TestPassthrough:
    """Everything the hook cannot prove read-only reaches the real permission matcher unchanged."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push origin main", id="mutating-git-push"),
            pytest.param("git branch -D feature", id="mutating-git-branch-delete"),
            pytest.param("git branch newfeature", id="mutating-git-branch-create"),
            pytest.param("git reset --hard HEAD", id="mutating-git-reset"),
            pytest.param("git commit -m x", id="mutating-git-commit"),
            pytest.param("gh pr comment 42 --body hi", id="mutating-gh-pr-comment"),
            pytest.param("gh pr create --title x", id="mutating-gh-pr-create"),
            pytest.param("gh issue close 42", id="mutating-gh-issue-close"),
            pytest.param("gh release create v1", id="mutating-gh-release-create"),
            pytest.param("gh api -X POST repos/o/r/issues", id="mutating-gh-api-post"),
            pytest.param("gh api --method DELETE repos/o/r/x", id="mutating-gh-api-delete"),
            pytest.param("docker rm -f box", id="mutating-docker-rm"),
            pytest.param("kubectl delete pod x", id="mutating-kubectl-delete"),
            pytest.param("aws s3 rm s3://bucket/key", id="mutating-aws-s3-rm"),
            pytest.param("git status && git push origin main", id="compound-and-push"),
            pytest.param("git status; git push", id="compound-semicolon-push"),
            pytest.param("git status $(git push)", id="compound-substitution"),
            pytest.param("git diff HEAD `git push`", id="compound-backticks"),
            pytest.param("git status && rm -rf /tmp/x", id="compound-and-rm"),
            pytest.param("gh pr view 42 && gh pr merge 42", id="compound-gh-merge"),
            pytest.param("aws ec2 describe-instances && aws s3 rm s3://b/k", id="compound-aws-rm"),
            pytest.param("pytest tests/ && rm -rf build", id="compound-pytest-rm"),
            pytest.param("git log | tee /tmp/out", id="compound-pipe-tee"),
            pytest.param("git log > /tmp/out", id="compound-redirect"),
            pytest.param("find . -name '*.py' -delete", id="find-delete"),
            pytest.param("find . -type f -exec rm {} +", id="find-exec"),
            pytest.param("find . -fprintf /etc/target payload", id="find-fprintf"),
            pytest.param("find . -name '*.py'", id="find-read-only-spelling"),
            pytest.param("cargo install ripgrep", id="execution-cargo-install"),
            pytest.param("cargo run --release", id="execution-cargo-run"),
            pytest.param("cargo check", id="execution-cargo-check"),
            pytest.param("next build", id="execution-next-build"),
            pytest.param("next dev", id="execution-next-dev"),
            pytest.param("diff a.txt b.txt", id="excluded-diff"),
            pytest.param("curl -X POST https://api.example.com", id="excluded-curl"),
            pytest.param("wget --post-data=x https://example.com", id="excluded-wget"),
            pytest.param("psql -c 'DROP TABLE users'", id="excluded-psql"),
            pytest.param("rtk git status", id="already-prefixed"),
        ],
    )
    def test_command_is_never_rewritten(self, command: str) -> None:
        """A mutating, chained, destructive, result-corrupting or already-wrapped command is left untouched.

        Scenario: a rewrite edits the command string and so dodges the settings.json deny list. ``mutating-*`` cases
        are write verbs of mixed CLIs; ``compound-*`` cases chain a read-only prefix to anything else; ``find-*`` is
        excluded outright because ``-delete``, ``-exec ... {} +`` and ``-fprintf`` carry no character ``SHELL_META``
        catches; ``execution-*`` subcommands (``cargo check`` runs ``build.rs``) execute project code; ``excluded-*``
        either corrupt results (``diff``) or have unprovable intent; an ``rtk``-prefixed command is not double-wrapped.
        """
        result = _run(command)
        assert result == {}, f"{command!r} was rewritten — deny bypass risk: {result}"

    def test_non_bash_tool_is_ignored(self) -> None:
        """A payload for a tool other than Bash produces no output and exits 0.

        The hook only understands shell commands; a Read payload must never be inspected or rewritten.
        """
        proc = _run_payload({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
        assert (proc.returncode, proc.stdout.strip()) == (0, "")
