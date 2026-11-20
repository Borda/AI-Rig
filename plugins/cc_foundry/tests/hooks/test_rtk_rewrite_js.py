"""Subprocess tests for ``hooks/rtk-rewrite.js``.

The hook is a ``PreToolUse`` gate that rewrites the one measured read-heavy Bash command that keeps every path
(``git status``) to its ``rtk <cmd>`` equivalent without approving execution. Its contract:

* **Permission-preserving rewrite** — eligible commands are
  rewritten without any ``permissionDecision``; everything else passes
  through unchanged (empty stdout, exit 0) so the real allow/deny matcher sees
  the original string.
* **No deny bypass** — because a rewrite mutates the command string (and thus
  dodges the settings.json deny list), mutating subcommands on mixed CLIs
  (``git push``, ``gh pr comment``, ``gh api -X POST``, ``docker rm`` …) must
  NEVER be rewritten. They passthrough to real permission checking.
* **No result corruption** — a verb whose rtk output drops information without a recovery hint (``git log``,
  ``git diff``, ``git show``, ``ls``, ``wc``, ``gh repo view``), is byte-identical (no saving), or was never
  measured passes through. ``diff`` is never rewritten (rtk alters its exit status / "identical" summary).

These tests require ``rtk`` to be resolvable on ``PATH``; when it is not, the
hook is a deliberate no-op and the behavioural contract cannot be exercised, so
the suite is skipped.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent.parent / "hooks" / "rtk-rewrite.js"
PERMISSIONS = HOOK.parent.parent / ".claude-plugin" / "permissions-allow.json"

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


def _expand_guard_prefixes(pattern: str) -> set[str]:
    """Expand the hook's finite alternatives while retaining exact-command anchors."""
    group = re.search(r"\((?:\?:)?([^()]*)\)(\?)?", pattern)
    if group is not None:
        choices = group[1].split("|") + ([""] if group[2] else [])
        return {
            prefix
            for choice in choices
            for prefix in _expand_guard_prefixes(pattern[: group.start()] + choice + pattern[group.end() :])
        }
    assert re.fullmatch(r"[\w *-]+\$?", pattern), f"Unsupported guard syntax: {pattern!r}"
    return {pattern.strip()}


def _rewritable_prefixes() -> set[str]:
    """Read command families from the hook tables instead of maintaining a second allowlist."""
    source = HOOK.read_text(encoding="utf-8")
    safe = json.loads(re.search(r"const SAFE_PREFIXES = (\[[\s\S]*?\]);", source)[1])
    guarded = json.loads(re.search(r"const GUARDED_PREFIXES = (\[[\s\S]*?\]);", source)[1])
    guards = source.split("const READ_ONLY_GUARDS = [", 1)[1].split("\n];", 1)[0]
    prefixes = set(safe)
    for line in guards.splitlines():
        if not line.strip().startswith("/^"):
            continue
        # Lookaheads constrain API arguments; wildcards retain the AWS service and verb families.
        pattern = line.strip()[2:-2].split("(?!", 1)[0]
        pattern = pattern.replace(r"\s+", " ").replace(r"\b", "")
        pattern = pattern.replace(r"\S+", "*").replace(r"[\w-]*", "*")
        expanded = _expand_guard_prefixes(pattern)
        assert all(prefix.split()[0] in guarded for prefix in expanded)
        prefixes.update(expanded)
    return {prefix for prefix in prefixes if not prefix.endswith("$") or prefix[:-1] not in prefixes}


def test_permissions_match_exactly_the_rewritable_prefixes() -> None:
    """Mirror entries cover eligible prefixes only within existing Bash permissions."""
    shipped = json.loads(PERMISSIONS.read_text(encoding="utf-8"))
    originals = {rule for rule in shipped if rule.startswith("Bash(") and not rule.startswith("Bash(rtk ")}
    rewritable = _rewritable_prefixes()
    allowed = {
        prefix for prefix in rewritable if any(_matches_bash_rule(prefix.removesuffix("$"), rule) for rule in originals)
    }
    # A family ending in a wildcard already covers bare verbs; adding :* would require more arguments.
    expected = {
        f"Bash(rtk {prefix.removesuffix('$')}{'' if prefix.endswith(('$', '*')) else ':*'})" for prefix in allowed
    }
    # A broad hook family such as gh api must retain the original rule's narrower argument shape.
    narrowed = {
        rule
        for rule in originals
        if any(
            _matches_bash_rule(rule.removeprefix("Bash(").removesuffix(")").removesuffix(":*"), f"Bash({prefix}:*)")
            for prefix in rewritable - allowed
            if not prefix.endswith(("$", "*"))
        )
    }
    expected.update(f"Bash(rtk {rule.removeprefix('Bash(')}" for rule in narrowed)
    mirrors = {rule for rule in shipped if rule.startswith("Bash(rtk ")}
    assert mirrors == expected


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize("prefix", sorted(_rewritable_prefixes()))
def test_derived_prefix_is_rewritten_without_allow(prefix: str) -> None:
    """Each table-derived permission family represents a command the real hook still rewrites."""
    command = prefix.rstrip("$").replace("*", "example") + ("" if prefix.endswith("$") else " example")
    result = _run(command)
    assert _rewritten_to(result) == f"rtk {command}"
    assert "permissionDecision" not in result["hookSpecificOutput"]


def _matches_bash_rule(command: str, rule: str) -> bool:
    """Apply Bash glob rules, including the documented single-wildcard bare-command exception."""
    pattern = rule.removeprefix("Bash(").removesuffix(")")
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    if pattern.endswith(" *") and pattern.count("*") == 1 and command == pattern[:-2]:
        return True
    return fnmatchcase(command, pattern)


def test_mirrors_require_an_allowed_original() -> None:
    """No RTK allow entry grants execution beyond an existing original Bash rule."""
    shipped = json.loads(PERMISSIONS.read_text(encoding="utf-8"))
    originals = {rule for rule in shipped if rule.startswith("Bash(") and not rule.startswith("Bash(rtk ")}
    uncovered = {
        mirror
        for mirror in shipped
        if mirror.startswith("Bash(rtk ")
        and not any(
            _matches_bash_rule(mirror.removeprefix("Bash(rtk ").removesuffix(")").replace(":*", " *"), rule)
            for rule in originals
        )
    }
    assert uncovered == set()


@_skip_rtk_unavailable
@_skip_node_unavailable
@pytest.mark.parametrize(
    "command",
    [
        pytest.param("git status", id="git-status"),
        pytest.param("git status --short", id="git-status-flags"),
    ],
)
def test_rewritten_command_matches_no_deny_rule(command: str) -> None:
    """A routed family never lands on a shipped deny rule through the ``rtk`` prefix."""
    rewritten = _rewritten_to(_run(command))
    assert rewritten == f"rtk {command}"
    denied = json.loads(PERMISSIONS.with_name("permissions-deny.json").read_text(encoding="utf-8"))
    assert not any(_matches_bash_rule(rewritten, rule) for rule in denied if rule.startswith("Bash("))


#: Every rtk mirror a release dropped; installed copies keep them until setup removes them via the retired list.
RETIRED_RTK_MIRRORS = {
    "Bash(rtk gh api graphql:*)",
    "Bash(rtk ls:*)",
    "Bash(rtk recall:*)",
    "Bash(rtk gh api repos/*)",
    "Bash(rtk gh api search/*)",
    "Bash(rtk gh auth status:*)",
    "Bash(rtk gh issue list:*)",
    "Bash(rtk gh issue view:*)",
    "Bash(rtk gh pr checks:*)",
    "Bash(rtk gh pr diff:*)",
    "Bash(rtk gh pr list:*)",
    "Bash(rtk gh pr view:*)",
    "Bash(rtk gh release list:*)",
    "Bash(rtk gh release view:*)",
    "Bash(rtk gh repo view:*)",
    "Bash(rtk gh run list:*)",
    "Bash(rtk gh run view:*)",
    "Bash(rtk git describe:*)",
    "Bash(rtk git diff:*)",
    "Bash(rtk git log:*)",
    "Bash(rtk git ls-files:*)",
    "Bash(rtk git merge-base:*)",
    "Bash(rtk git remote -v:*)",
    "Bash(rtk git remote get-url)",
    "Bash(rtk git remote show)",
    "Bash(rtk git remote)",
    "Bash(rtk git rev-list:*)",
    "Bash(rtk git rev-parse:*)",
    "Bash(rtk git shortlog:*)",
    "Bash(rtk git show:*)",
    "Bash(rtk git stash list:*)",
    "Bash(rtk git tag --list:*)",
    "Bash(rtk git tag -l:*)",
    "Bash(rtk git tag)",
    "Bash(rtk wc:*)",
    "Bash(rtk grep:*)",
}


def test_dropped_mirrors_are_retired_and_never_shipped_again() -> None:
    """Setup removes only listed rules, so every dropped rtk mirror is retired and none is also shipped."""
    shipped = set(json.loads(PERMISSIONS.read_text(encoding="utf-8")))
    retired = set(json.loads(PERMISSIONS.with_name("permissions-allow-retired.json").read_text(encoding="utf-8")))
    assert RETIRED_RTK_MIRRORS <= retired
    assert not retired & shipped


@_skip_rtk_unavailable
@_skip_node_unavailable
class TestRewrite:
    """Eligible commands get an ``rtk`` prefix while retaining normal permission checks."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git status", id="git-status"),
            pytest.param("git status --short", id="git-status-short"),
            pytest.param("git status --porcelain=v2", id="git-status-porcelain"),
            pytest.param("git status -uall", id="git-status-untracked"),
        ],
    )
    def test_command_is_rewritten_without_allow(self, command: str) -> None:
        """A measured command is rewritten to ``rtk <command>`` without an allow decision.

        Only ``git status`` earns the rewrite: its rtk output kept every path in the fidelity comparison against the
        command the agent runs; ``ls`` fails it, because inside the sandbox rtk cuts a listing and prints no recall
        hash. ``grep`` is not routed: the Bash tool's ``grep`` is a gitignore- aware shell function, which ``rtk grep``
        (the system grep) does not match. The exact rewritten string and the absence of a permission decision are both
        part of the contract.
        """
        result = _run(command)
        assert _rewritten_to(result) == f"rtk {command}"
        assert "permissionDecision" not in result["hookSpecificOutput"]

    @pytest.mark.parametrize(
        "command",
        [
            "grep -r foo .",
            "grep -rl foo src",
            'ls ""-l src',
            "ls ''-la src",
            "ls $'-l' src",
            "ls \\-l src",
            "ls $LSOPTS src",
            "ls ${X:--l} src",
            'ls "$DIR"',
            "ls -l src",
            "ls src -la",
            "ls *",
            "ls src/*.py",
            "ls src tests",
            "ls a b c",
            "ls",
            "ls src",
            "ls .",
            "ls ?",
            "git status --branch",
            "git status -b",
            "git status -sb",
            "git status -bs",
            "git status --short --branch",
            'git status "-sb"',
            "git status '-sb'",
            'git status -s "-b"',
            "git status -s '--branch'",
            "git status -s \\-b",
            'git status -s -"b"',
            "git status -s $B",
            "git status -s ${B}",
            "git status -s {-b,}",
            "git reflog expire --expire=now --all",
            "git reflog expire --all",
            "git reflog delete HEAD@{0}",
            "git reflog write HEAD a b",
            "git grep -Otouch needle",
            "git grep -Oless foo",
            "git grep -O foo",
            "git grep --open-files-in-pager=less foo",
            "git grep --open-files-in-pager foo",
            "git diff --output=result.txt",
            "git diff --output=x",
            "git diff --output x",
            'git diff "--output=x"',
            "git log --output=x",
            "tree --output=out",
            "tree -o result.txt",
            "tree -o out",
            "tree -oout",
            "tree -ao out",
            "pytest tests/",
            "ruff check .",
            "ruff check --fix .",
            "prettier --write .",
            "tsc",
            "lint --fix",
            "format .",
            "jest",
            "vitest",
            "rspec",
            "rubocop",
            "golangci-lint run",
            "mypy .",
        ],
    )
    def test_argument_side_effects_pass_through(self, command: str) -> None:
        """Commands whose arguments execute code or write files reach permission matching unchanged."""
        result = _run(command)
        assert result == {}


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
            pytest.param("git remote -v remove origin", id="mutating-git-remote-extra-words"),
            pytest.param("git remote add o https://example.invalid/x", id="mutating-git-remote-add"),
            pytest.param("gh api repos/o/r -F title=x", id="mutating-gh-api-typed-field"),
            pytest.param("gh api repos/o/r --raw-field title=x", id="mutating-gh-api-raw-field"),
            pytest.param("gh api --method=POST repos/o/r/issues", id="mutating-gh-api-method-equals"),
            pytest.param("rg --pre=cat needle", id="execution-rg-pre"),
            pytest.param("git log", id="lossy-git-log"),
            pytest.param("git log --oneline -5", id="lossy-git-log-oneline"),
            pytest.param("git log -n 20 --format=%h", id="lossy-git-log-format"),
            pytest.param("git diff HEAD", id="lossy-git-diff"),
            pytest.param("git show HEAD", id="lossy-git-show"),
            pytest.param("ls -la", id="lossy-ls-long"),
            pytest.param("ls -l src", id="lossy-ls-long-dir"),
            pytest.param("ls -a", id="reformat-ls-all"),
            pytest.param("wc -l src/a.py src/b.py", id="lossy-wc"),
            pytest.param("gh repo view", id="lossy-gh-repo-view"),
            pytest.param("git blame README.md", id="identical-git-blame"),
            pytest.param("git ls-files", id="identical-git-ls-files"),
            pytest.param("git reflog", id="identical-git-reflog"),
            pytest.param("git rev-parse HEAD", id="identical-git-rev-parse"),
            pytest.param("git grep foo", id="identical-git-grep"),
            pytest.param("git remote -v", id="identical-git-remote"),
            pytest.param("gh pr view 42", id="unmeasured-gh-pr-view"),
            pytest.param("gh issue list", id="unmeasured-gh-issue-list"),
            pytest.param("gh api repos/owner/repo", id="unmeasured-gh-api-get"),
            pytest.param("docker ps", id="unmeasured-docker"),
            pytest.param("kubectl get pods", id="unmeasured-kubectl"),
            pytest.param("aws ec2 describe-instances", id="unmeasured-aws"),
            pytest.param("aws s3 ls", id="unmeasured-aws-s3-ls"),
            pytest.param("tree -a", id="unmeasured-tree"),
            pytest.param("cargo tree", id="unmeasured-cargo-tree"),
            pytest.param("next info", id="unmeasured-next-info"),
        ],
    )
    def test_command_is_never_rewritten(self, command: str) -> None:
        """A mutating, chained, destructive, lossy, identical-output or unmeasured command is left untouched.

        Scenario: a rewrite edits the command string and so dodges the settings.json deny list. ``mutating-*`` cases
        are write verbs of mixed CLIs, including forms an older prefix pattern accepted; ``compound-*`` cases chain a
        read-only prefix to anything else; ``find-*`` is excluded outright because ``-delete``, ``-exec ... {} +`` and
        ``-fprintf`` carry no character ``SHELL_META`` catches; ``execution-*`` subcommands (``cargo check`` runs
        ``build.rs``, ``rg --pre`` runs a program) execute code; ``lossy-*`` verbs drop information without a recovery
        hint, ``reformat-*`` verbs reshape it, ``identical-*`` verbs gain nothing from rtk and ``unmeasured-*`` verbs
        have no fidelity evidence; ``excluded-*`` either corrupt results (``diff``) or have unprovable intent; an
        ``rtk``-prefixed command is not double-wrapped.
        """
        result = _run(command)
        assert result == {}, f"{command!r} was rewritten — deny bypass risk: {result}"

    def test_non_bash_tool_is_ignored(self) -> None:
        """A payload for a tool other than Bash produces no output and exits 0.

        The hook only understands shell commands; a Read payload must never be inspected or rewritten.
        """
        proc = _run_payload({"tool_name": "Read", "tool_input": {"file_path": "/x"}})
        assert (proc.returncode, proc.stdout.strip()) == (0, "")
