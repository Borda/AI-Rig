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
  ``send-pack``/``http-push`` and ``subtree … push`` are pushes, any case of
  the program name; a ``remote-<transport>`` helper, an enabling inline
  ``help.autocorrect`` and an unquoted glob argument read as force; ref
  deletion is a regular push.
* **SessionStart wipe** — clears leftover push sentinels from prior runs.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from _hook_env import _hook_tmp_base

GIT_UNAVAILABLE = subprocess.run(["git", "--version"], capture_output=True, timeout=5).returncode != 0

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(name="git_repo")
def _git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with a per-test unique name on branch ``main`` with one commit.

    The hook keys its push sentinel on the repo basename, in a machine-global directory. A fixed name made every pytest-
    xdist worker share one sentinel file, so one worker's SessionStart wipe or fixture teardown deleted another worker's
    freshly armed sentinel. A fixed-length hex suffix keeps each name unique and never a prefix of another, so the
    hook's prefix-scoped wipe (``claude-push-auth-<repo>-``) cannot reach a sibling test's file either.
    """
    repo = tmp_path / f"myrepo-{uuid.uuid4().hex[:12]}"
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
def _push_sentinel(git_repo: Path) -> Iterator[Path]:
    """Yield this test's push sentinel path, removing any leftover before AND after each test.

    Resolved through ``_hook_tmp_base()`` rather than a module-level ``/tmp`` literal so the path follows the hook's own
    ``getSentinelDir()`` on Windows too. Kept lazy — the base is computed inside the fixture, after the module-level git
    skipif has run. The name mirrors the hook's ``toSlug()`` over the repo basename and branch; the basename is already
    lowercase alphanumerics and hyphens, so it slugs to itself.
    """
    path = _hook_tmp_base() / f"claude-push-auth-{git_repo.name}-main"
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


# Deleting a remote ref is a regular push by the user's decision: sentinel-gated, never force-blocked.
_REF_DELETIONS = [
    pytest.param("git push origin :feature", id="empty-source-refspec"),
    pytest.param("git push --delete origin feature", id="delete-long"),
    pytest.param("git push -d origin feature", id="delete-short"),
    pytest.param("git push --prune origin", id="prune"),
]


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

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git $'\\x{70}ush' -f origin main", id="hex-brace-escape-subcommand"),
            pytest.param("$'\\x{67}it' push", id="hex-brace-escape-program"),
            pytest.param('echo $"hello"', id="locale-string-without-git"),
            pytest.param('echo "${ git push -f origin main; }"', id="bash-5-3-command-brace-in-double-quotes"),
            pytest.param("cat <<EOF\n${ git push -f origin main; }\nEOF", id="bash-5-3-command-brace-in-heredoc"),
            pytest.param('gh pr view "${|git push -f origin main;}"', id="bash-5-3-value-brace-as-an-argument"),
            pytest.param("$\\\n'\\x67it' push -f origin main", id="continued-ansi-c-program"),
            pytest.param("echo $\\\n'\\''; $'\\x67it' push -f origin main #'", id="continued-ansi-c-quote"),
        ],
    )
    def test_shell_dependent_quoting_blocked_with_its_own_reason(
        self, git_repo: Path, run_hook, push_sentinel: Path, command: str
    ) -> None:
        """Quoting bash and zsh read differently may hide a push, so it blocks even with a sentinel.

        The reason names the quoting, not a force push: ``echo $"hello"`` pushes nothing, and a message claiming a force
        push would send the reader looking for one.
        """
        push_sentinel.touch()

        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 2
        assert "bash and zsh read differently" in result.stderr

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
            "git push --mirror origin",
            "git push -fu origin main",
            "git push -uf origin main",
            "/usr/bin/git push --force origin main",
            "env git push --force origin main",
            "env -i PATH=/bin git push --force origin main",
            "GIT_TRACE=1 git push --force origin main",
            "echo $(git push --force origin main)",
            pytest.param("{ git push --force; }", id="brace-group"),
            pytest.param("( git push --force )", id="subshell"),
            pytest.param("if true; then git push --force; fi", id="if-then"),
            pytest.param("while true; do git push -f; done", id="loop-body"),
            pytest.param("! git push --force", id="negation"),
            pytest.param("f() { git push -f; }; f", id="function-body"),
            pytest.param("time git push --force", id="time"),
            pytest.param("command git push -f", id="command"),
            pytest.param("nohup git push --force", id="nohup"),
            pytest.param("exec git push -f", id="exec"),
            pytest.param("sudo -u root git push -f", id="sudo-with-option-value"),
            pytest.param("env -u HOME git push --force", id="env-unset-value"),
            pytest.param("echo main | xargs git push -f origin", id="xargs"),
            pytest.param('bash -c "git push -f"', id="bash-c"),
            pytest.param("sh -c 'cd /x && git push --force'", id="sh-c-compound"),
            pytest.param("eval 'git push -f'", id="eval"),
            pytest.param("env -S 'git push -f'", id="env-split-string"),
            pytest.param("bash <<'EOF'\ngit push -f\nEOF", id="heredoc-to-shell"),
            pytest.param('echo "$(git push --force)"', id="substitution-in-double-quotes"),
            pytest.param('git -C "dir with space" push --force', id="quoted-dir-with-space"),
            pytest.param('git -C "a;b" push -f', id="quoted-dir-with-operator"),
            pytest.param("git --future-opt value push -f", id="unknown-global-option-with-value"),
            pytest.param("git commit -m 'unbalanced; echo git push -f", id="unbalanced-quote-fails-closed"),
            pytest.param("echo run git push -f", id="unquoted-prose-fails-closed"),
            # S1: the program name ignores case, as macOS and Windows file systems do
            pytest.param("GIT push --force", id="upper-case-git"),
            pytest.param("Git push -f", id="mixed-case-git"),
            pytest.param("/usr/bin/GIT push -f", id="upper-case-git-path"),
            pytest.param("GIT.EXE push -f", id="upper-case-git-exe"),
            # S2: a push under another name
            pytest.param("git -c alias.yolo='push --force' yolo", id="inline-alias-with-force"),
            pytest.param("git -c alias.p=push p -f", id="inline-alias"),
            pytest.param("git -c 'alias.x=!git push -f' x", id="inline-shell-alias"),
            pytest.param("git --config-env=alias.q=FOO q", id="config-env-alias"),
            pytest.param("git send-pack --force origin main", id="send-pack-force"),
            pytest.param("git send-pack --mirror origin", id="send-pack-mirror"),
            pytest.param("git http-push --force https://example.invalid/repo main", id="http-push-force"),
            # M3: abbreviations and brace expansion
            pytest.param("git push --mir origin", id="mirror-abbreviated"),
            pytest.param("git push --m origin", id="mirror-one-letter"),
            pytest.param("git push --force-w origin main", id="force-with-lease-abbreviated"),
            pytest.param("git push --for origin main", id="ambiguous-force-prefix"),
            pytest.param("git push {-f,origin} main", id="brace-expanded-flag"),
            pytest.param("git push origin {+,}main", id="brace-expanded-refspec"),
            pytest.param("git {push,} -f", id="brace-expanded-subcommand"),
            pytest.param("{git,} push -f", id="brace-expanded-program"),
            # L1: interpreter code
            pytest.param("python3 -c \"import os; os.system('git push -f')\"", id="python-c"),
            pytest.param("perl -e 'system(\"git push --force\")'", id="perl-e"),
            pytest.param("node -e \"require('child_process').execSync('git push -f')\"", id="node-e"),
            # Scope D: here-strings feed shell source like heredocs, and never swallow the lines after them
            pytest.param("bash <<< 'git push -f'", id="here-string-to-shell"),
            pytest.param('sh <<<"git push origin +main"', id="here-string-glued-refspec"),
            pytest.param("<<< 'git push -f' bash", id="here-string-before-runner"),
            pytest.param("cat <<< hi\ntime git push -f\nhi", id="here-string-then-next-line"),
            # Scope D: inline help.autocorrect runs git's guess for a typo, so any subcommand may push
            pytest.param("git -c help.autocorrect=immediate pussh --force origin main", id="autocorrect-immediate"),
            pytest.param("git -c help.autocorrect=1 pussh origin main", id="autocorrect-delay"),
            pytest.param("time git -c help.autoCorrect=immediate pus -f", id="autocorrect-key-case"),
            pytest.param("git --config-env help.autocorrect=V pussh -f", id="autocorrect-run-time-value"),
            # Scope D: push-family programs; the name is matched lower-cased (macOS runs case variants)
            pytest.param(
                "printf 'push +refs/heads/main:refs/heads/main\\n\\n' | git remote-https origin https://example.invalid/r",
                id="remote-helper-stdin",
            ),
            pytest.param("git remote-http origin http://example.invalid/r", id="remote-helper-http"),
            pytest.param("git remote-ext origin 'ext::sh -c x'", id="remote-helper-ext"),
            pytest.param("git REMOTE-HTTPS origin https://example.invalid/r", id="remote-helper-upper-case"),
            pytest.param("git HTTP-PUSH --force https://example.invalid/repo main", id="http-push-upper-case"),
            pytest.param("git SEND-PACK --force origin main", id="send-pack-upper-case"),
            pytest.param("git subtree push --prefix=docs origin +main", id="subtree-plus-refspec-fails-closed"),
            # Scope D: an unquoted glob may expand to a planted file named like a flag or refspec
            pytest.param("git push origin -?", id="glob-flag"),
            pytest.param("git push origin ?main", id="glob-refspec"),
            pytest.param('git push origin "?main', id="glob-on-unbalanced-quote-fails-closed"),
            pytest.param("git push origin ?main {1..300}", id="glob-on-oversized-braces-fails-closed"),
            # A redirection is never an argument: one before the subcommand no longer reads as the subcommand
            pytest.param("git 2>/dev/null push -f origin main", id="redirection-before-subcommand"),
            pytest.param("git > x push -f origin main", id="separate-redirection-before-subcommand"),
            pytest.param("{ git 2>/dev/null push -f origin main; }", id="grouped-redirection-before-subcommand"),
            pytest.param("echo 'a>'&git push -f origin main", id="quoted-operator-before-background"),
            pytest.param("git -c alias.p='push > -f' p origin main", id="plain-alias-angle-bracket-is-an-argument"),
            # The push words reach git through another command
            pytest.param("echo push -f origin main | xargs -n9 git", id="xargs-supplies-subcommand"),
            pytest.param("echo -f | xargs git push origin main", id="xargs-appends-to-push"),
            pytest.param("echo 'git push -f origin main' | sh", id="echo-piped-to-sh"),
            pytest.param("echo 'git push -f origin main' | xargs -0 sh -c", id="xargs-supplies-sh-c-string"),
            pytest.param("trap 'git push -f origin main' EXIT", id="trap-handler"),
            pytest.param("source /dev/stdin <<< 'git push -f origin main'", id="source-stdin-here-string"),
            pytest.param("printf -v 'a[$(git push -f origin main)]' x", id="printf-v-subscript"),
            pytest.param("cat <<EOF\nEO\\\nF\n$'\\x67it' push -f origin main\nEOF", id="continued-terminator-line"),
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
            pytest.param("{ git push; }", id="brace-group"),
            pytest.param("if true; then git push; fi", id="if-then"),
            pytest.param("time git push", id="time"),
            pytest.param("sudo -u root git push", id="sudo-with-option-value"),
            pytest.param('bash -c "git push origin main"', id="bash-c"),
            pytest.param('git -C "dir with space" push', id="quoted-dir-with-space"),
            pytest.param("git push --fol origin main", id="follow-tags-abbreviated"),
            pytest.param("git push --no-force-with-lease origin main", id="negated-force-option"),
            pytest.param("GIT push origin main", id="upper-case-git"),
            pytest.param("git -c alias.p=push p origin main", id="inline-alias"),
            pytest.param("git send-pack origin main", id="send-pack"),
            pytest.param("git push origin '{-f,x}'", id="quoted-braces-are-a-literal-refspec"),
            pytest.param(
                "python3 -c \"import subprocess; subprocess.run(['git', 'push', 'origin', 'main'])\"", id="python-c"
            ),
            pytest.param("cat <<<hi\nsudo GIT push origin main", id="push-after-here-string-line"),
            pytest.param("git subtree push --prefix=docs origin main", id="subtree-push"),
            pytest.param("git subtree -P docs push origin main", id="subtree-options-first"),
            pytest.param("git SUBTREE push --prefix=docs origin main", id="subtree-upper-case"),
            pytest.param("git -c alias.s=subtree s push --prefix=docs origin main", id="alias-to-subtree"),
            pytest.param("git push origin '?main'", id="single-quoted-glob-is-literal"),
            pytest.param('git push origin "?main"', id="double-quoted-glob-is-literal"),
            pytest.param("git push origin \\?main", id="escaped-glob-is-literal"),
        ],
    )
    def test_non_force_push_spellings_still_need_sentinel(self, git_repo: Path, run_hook, command: str) -> None:
        """A push reached via a global flag, a chain, a group, a prefix or nested shell source still needs a sentinel.

        ``--follow-tags``/``--fol``, ``--no-force-with-lease`` and ``-u`` guard the force test against over-reach: a
        long option that merely contains an f or negates a force option, and a short option that is not f, must reach
        the sentinel gate rather than the unconditional force block.
        """
        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 2, f"{command!r} bypassed the sentinel gate"
        assert "AskUserQuestion" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            "echo hello",
            'echo "pid $$"',
            "git log --oneline",
            "git status",
            "echo 'git push --force'",
            pytest.param('echo "git push --force"', id="double-quoted-text"),
            pytest.param('git log --grep "push --force"', id="quoted-grep-pattern"),
            pytest.param(
                "git commit -m \"$(cat <<'EOF'\nfix: block git push --force bypass\n\nDon't allow it; never git push -f.\n"
                'EOF\n)"',
                id="commit-message-heredoc-naming-force-push",
            ),
            pytest.param("node -e \"const a = []; a.push(1); require('./lib/shell-git.js')\"", id="array-push-in-node"),
            pytest.param("GIT status", id="upper-case-git-non-push"),
            pytest.param("cat <<< 'git push -f'", id="here-string-to-non-runner"),
            pytest.param("git -c help.autocorrect=never pussh origin main", id="autocorrect-never"),
            pytest.param("git -c help.autocorrect=no status", id="autocorrect-no"),
            pytest.param("git remote -v", id="remote-is-not-a-helper"),
            pytest.param("git subtree split --prefix=docs", id="subtree-split"),
            pytest.param("printf '%s\\0' a b | xargs -0 -r git add --", id="oss-resolve-xargs-git-add"),
        ],
    )
    def test_non_push_commands_still_pass(self, git_repo: Path, run_hook, command: str) -> None:
        """Commands that are not a push bypass the gate regardless of sentinel state.

        Quoted text is one word to the lexer, never a command, so ``echo 'git push --force'`` passes. The heredoc case
        is the commit flow this repo uses: a message body naming a force push mid-line, with an apostrophe in it, is
        data — the lexer reads the heredoc as a body, so the fail-closed scan never sees it. A substitution that really
        does run git (``echo $(git push ...)``) is caught, and is covered separately.
        """
        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param('git push -u origin "$(git branch --show-current)"', id="branch-substitution"),
            pytest.param('git push "$FORK_REMOTE" HEAD:"$HEAD_REF"', id="oss-resolve-fork-push"),
        ],
    )
    def test_push_with_run_time_values_passes_with_sentinel(
        self, git_repo: Path, run_hook, push_sentinel: Path, command: str
    ) -> None:
        """An authorized push whose remote or refspec is expanded at run time is not mistaken for a force push.

        Values the shell computes are beyond string inspection (header LIMITS). Blocking every ``$`` argument would
        break shipped skills that push to a computed remote and ref, such as ``oss:resolve``.
        """
        push_sentinel.touch()

        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 0, result.stderr


@_skip_git_unavailable
@pytest.mark.usefixtures("push_sentinel")
class TestRefDeletion:
    """Deleting remote refs is a regular push: kept sentinel-gated, not banned like a force push (user decision)."""

    @pytest.mark.parametrize("command", _REF_DELETIONS)
    def test_needs_the_sentinel_like_any_push(self, git_repo: Path, run_hook, command: str) -> None:
        """Without a sentinel the deletion is stopped at the confirmation gate, never by the force block.

        The force message must not appear: a deletion the user confirmed must stay possible once the sentinel exists.
        """
        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 2
        assert "AskUserQuestion" in result.stderr
        assert "force" not in result.stderr

    @pytest.mark.parametrize("command", _REF_DELETIONS)
    def test_passes_with_the_sentinel(self, git_repo: Path, run_hook, push_sentinel: Path, command: str) -> None:
        """A fresh sentinel — created by the user after AskUserQuestion — lets the deletion through."""
        push_sentinel.touch()

        result = run_hook("commit-guard.js", _bash_push(cmd=command), cwd=git_repo)

        assert result.returncode == 0, result.stderr


@_skip_git_unavailable
class TestMissingLibrary:
    """Without ``lib/shell-git.js`` the guard cannot parse a command, so it blocks every push-looking one."""

    @pytest.fixture
    def bare_hook(self, tmp_path: Path) -> Path:
        """Copy commit-guard.js to a directory with no ``lib/`` beside it, as a broken install would leave it."""
        hook = tmp_path / "bare-hooks" / "commit-guard.js"
        hook.parent.mkdir()
        hook.write_bytes((Path(__file__).resolve().parents[2] / "hooks" / "commit-guard.js").read_bytes())
        return hook

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("git push origin main", 2, id="push-blocked"),
            pytest.param("{ git push; }", 2, id="grouped-push-blocked"),
            pytest.param("GIT PUSH origin main", 2, id="upper-case-push-blocked"),
            pytest.param("git send-pack origin main", 2, id="send-pack-blocked"),
            pytest.param("git remote-https origin https://example.invalid/r", 2, id="remote-helper-blocked"),
            pytest.param("git status", 0, id="non-push-passes"),
            pytest.param("git remote -v", 0, id="remote-listing-passes"),
        ],
    )
    def test_push_fails_closed(self, bare_hook: Path, git_repo: Path, command: str, expected: int) -> None:
        """A crash would exit 1, which Claude Code treats as non-blocking; the guard exits 2 for a push instead."""
        result = subprocess.run(
            ["node", str(bare_hook)],
            input=json.dumps(_bash_push(cmd=command)),
            cwd=git_repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

        assert result.returncode == expected, result.stderr
        assert ("shell-git.js" in result.stderr) == (expected == 2)
