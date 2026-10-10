"""Subprocess tests for ``hooks/commit-guard.js``.

The hook does NOT gate ``git commit`` at all — commit authorization is
prompt-discipline only (see ``rules/git-commit.md``), no runtime check.
The hook gates only pushes. A regular push runs only by spending a
single-use push token, ``claude-push-approval.json`` in the git common dir,
which ``approval-guard.js`` writes from the user's ``Approve`` to the
``git-push`` question: the token holds the approved branch, HEAD and a
15-minute expiry, never the command. Each test spins up a small disposable git
repo and runs the hook with ``CLAUDE_PROJECT_DIR`` bound to it, as the harness
does.

Behavioural areas covered:

* **Commit passthrough** — ``git commit`` always exits 0, token or not;
  the hook never inspects it.
* **Push gating** — force-push is blocked unconditionally on any branch
  (even with a valid token, even one approving the force command itself); a
  regular push needs a token for the current branch and HEAD, is never
  auto-armed by a ``"push"``-mentioning prompt, and spends the token through
  the library's rename claim, so of concurrent calls exactly one runs. A
  spawned agent's call and a call without ``tool_use_id`` never spend it.
  ``send-pack``/``http-push``, ``subtree … push`` and ``lfs … push`` are
  pushes, any case of the program name; a ``remote-<transport>`` helper, an
  enabling inline ``help.autocorrect`` and an unquoted glob argument read as
  force. A dry run (``--dry-run``, ``-n``) sends nothing: it needs no token.
* **Approved branch only** — a token covers one push of the current branch
  to a configured remote name: ``git push``, ``git push origin``, ``-u``,
  ``origin <branch>``, ``origin HEAD:<branch>``, a substitution printing the
  current branch. Ref deletion, ``--prune``, ``--all``, ``--tags``,
  ``--follow-tags``, another ref, a URL or path destination, a variable, an
  unknown option or abbreviation, ``cd``/``pushd``/``popd``, ``-C``/``GIT_DIR``,
  inline config redirecting the remote, and ``send-pack``/``http-push``/
  ``subtree push`` are never covered: blocked with or without a token, the
  token kept, and the message tells Claude the user runs it by hand.
* **Remote-write family** — ``git push`` and ``git lfs push`` spend one token
  once and are blocked without one.
* **Local Git and remote reads** — with no approval record present, staging,
  merges, cherry-picks, branch/worktree preparation, destructive local
  operations and ``fetch``/``pull``/``ls-remote`` pass both this hook and
  ``gh-write-guard.js``.
* **One push per command** — quoting, redirections, pipes and read-only git
  beside it pass; a second push, a loop, function, ``watch``, or another git
  command that can move HEAD or a branch before the push is blocked and the
  token kept.
* **Fail closed** — an exception anywhere on the push path exits 2, never 1.
* **Wipe** — SessionStart and ``/clear`` delete the token; SessionStart also
  deletes this repository's leftover sentinels of the former mechanism,
  which no longer authorize anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from _hook_env import _hook_tmp_base

GIT_UNAVAILABLE = subprocess.run(["git", "--version"], capture_output=True, timeout=5).returncode != 0
#: The push token's file name in the git common dir.
PUSH_RECORD = "claude-push-approval.json"
APPROVED = "git push origin main"


def _symlinks_supported(probe_dir: Path) -> bool:
    """Return True when this host can create a symbolic link (Windows without developer mode cannot)."""
    link = probe_dir / f".symlink-probe-{uuid.uuid4().hex[:8]}"
    try:
        link.symlink_to(probe_dir)
    except OSError:
        return False
    link.unlink()
    return True


_skip_without_symlinks = pytest.mark.skipif(
    not _symlinks_supported(Path(__file__).resolve().parent), reason="requires symbolic link support"
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    """Run one git command in ``repo`` with a throwaway identity and no ``GIT_*`` steering; return its stdout."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        encoding="utf-8",
        check=True,
        timeout=30,
        env=env,
    )
    return proc.stdout.strip()


@pytest.fixture(name="git_repo")
def _git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with a per-test unique name on branch ``main`` with one commit.

    The legacy sentinel the hook still wipes is keyed on the repo basename, in a machine-global directory. A fixed-
    length hex suffix keeps each name unique and never a prefix of another, so one pytest-xdist worker's SessionStart
    wipe (``claude-push-auth-<repo>-``) cannot reach a sibling test's file.

    ``origin`` and ``upstream`` name one local bare repository: a token covers a push to a configured remote name only,
    so the plain ``git push origin main`` needs one. The hooks never run a push; the bare repository keeps any stray one
    off the network.
    """
    repo = tmp_path / f"myrepo-{uuid.uuid4().hex[:12]}"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("test", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "remote", "add", "upstream", str(bare))
    return repo


@pytest.fixture(name="legacy_sentinel")
def _legacy_sentinel(git_repo: Path) -> Iterator[Path]:
    """Yield this repo's former push sentinel path, removing any leftover before AND after each test.

    Resolved through ``_hook_tmp_base()`` so the path follows the hook's own ``getSentinelDir()`` on Windows too. The
    name mirrors the former ``toSlug()`` over the repo basename and branch; the basename already slugs to itself.
    """
    path = _hook_tmp_base() / f"claude-push-auth-{git_repo.name}-main"
    path.unlink(missing_ok=True)
    yield path
    path.unlink(missing_ok=True)


def _push_payload(command: str, here: Path, **fields: object) -> dict:
    """Return a PreToolUse(Bash) payload as the harness sends it: a fresh ``tool_use_id`` per call.

    ``fields`` add or replace payload keys (``agent_id``, ``transcript_path``); a value of ``None`` removes the key, so
    a case can send a call that carries no ``tool_use_id``.
    """
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(here),
        "tool_use_id": f"toolu_{uuid.uuid4().hex}",
        **fields,
    }
    return {key: value for key, value in payload.items() if value is not None}


@pytest.fixture(name="guard")
def _guard(
    run_hook: Callable, git_repo: Path, approval_transcript: Callable[[Path], Path]
) -> Callable[..., subprocess.CompletedProcess]:
    """Return a callable running commit-guard on one Bash command in the session project ``git_repo``.

    The payload names the session transcript the token fixtures record the user's answers in, as the harness names
    its own: the spend proves the token through it.
    """

    def _run(
        command: str, cwd: Path | None = None, project: Path | None = None, **fields: object
    ) -> subprocess.CompletedProcess:
        """Run the PreToolUse(Bash) hook with ``cwd`` as the payload and process directory."""
        here = cwd or git_repo
        session = project or git_repo
        fields.setdefault("transcript_path", str(approval_transcript(session)))
        return run_hook(
            "commit-guard.js",
            _push_payload(command, here, **fields),
            cwd=here,
            env_extra={"CLAUDE_PROJECT_DIR": str(session)},
        )

    return _run


def _event(run_hook: Callable, repo: Path, payload: dict) -> subprocess.CompletedProcess:
    """Run a non-tool event payload in ``repo`` as the session project."""
    return run_hook(
        "commit-guard.js", {**payload, "cwd": str(repo)}, cwd=repo, env_extra={"CLAUDE_PROJECT_DIR": str(repo)}
    )


# Deleting a remote ref is no force push, and by the user's decision ("approved branch only") no token covers it: the
# user runs it by hand.
_REF_DELETIONS = [
    pytest.param("git push origin :feature", id="empty-source-refspec"),
    pytest.param("git push --delete origin feature", id="delete-long"),
    pytest.param("git push -d origin feature", id="delete-short"),
    pytest.param("git push --prune origin", id="prune"),
]

#: Marker of the never-covered message: no approval can cover the push, the user runs it.
BY_HAND = "by hand"


_skip_git_unavailable = pytest.mark.skipif(
    GIT_UNAVAILABLE,
    reason="requires functional git (XCode CLI tools or equivalent)",
)


# ── Tests ─────────────────────────────────────────────────────────────────────


@_skip_git_unavailable
class TestCommitGuard:
    """commit-guard.js: push-only token gate; commit is prompt-discipline only."""

    def test_commit_always_passes_through(self, guard: Callable) -> None:
        """Git commit exits 0 unconditionally — the hook never inspects it, no token involved."""
        result = guard("git commit -m 'test'")

        assert result.returncode == 0, result.stderr

    def test_session_start_deletes_push_token(self, run_hook: Callable, git_repo: Path, push_token: Callable) -> None:
        """SessionStart deletes a pending push token, so a prior session's approval never carries over."""
        token = push_token(git_repo)

        result = _event(run_hook, git_repo, {"hook_event_name": "SessionStart"})

        assert result.returncode == 0, result.stderr
        assert not token.exists()

    def test_session_start_deletes_legacy_sentinel(
        self, run_hook: Callable, git_repo: Path, legacy_sentinel: Path
    ) -> None:
        """SessionStart deletes this repository's leftover sentinel of the former push mechanism."""
        legacy_sentinel.touch()

        result = _event(run_hook, git_repo, {"hook_event_name": "SessionStart"})

        assert result.returncode == 0, result.stderr
        assert not legacy_sentinel.exists()

    @pytest.mark.parametrize(
        ("prompt", "kept"),
        [
            pytest.param("/clear", False, id="clear-deletes"),
            pytest.param("push this", True, id="other-prompt-keeps"),
        ],
    )
    def test_prompt_wipes_token_only_on_clear(
        self, run_hook: Callable, git_repo: Path, push_token: Callable, prompt: str, kept: bool
    ) -> None:
        """``/clear`` deletes the push token like a new session; any other prompt leaves it alone."""
        token = push_token(git_repo)

        result = _event(run_hook, git_repo, {"hook_event_name": "UserPromptSubmit", "prompt": prompt})

        assert result.returncode == 0, result.stderr
        assert token.exists() == kept

    def test_force_push_blocked_any_branch(self, guard: Callable) -> None:
        """Force-push with no token → exit 2 with a 'force'/'forbidden' message."""
        result = guard("git push --force")

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
            pytest.param("cat <<EOF\nEO\\\nF\n$'\\x67it' push -f origin main\nEOF", id="continued-terminator-line"),
        ],
    )
    def test_shell_dependent_quoting_blocked_with_its_own_reason(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str
    ) -> None:
        """Quoting bash and zsh read differently may hide a push, so it blocks even with a token.

        The reason names the quoting, not a force push: ``echo $"hello"`` pushes nothing, and a message claiming a force
        push would send the reader looking for one.
        """
        push_token(git_repo)

        result = guard(command)

        assert result.returncode == 2
        assert "bash and zsh read differently" in result.stderr
        assert "force-push" not in result.stderr

    @pytest.mark.parametrize(
        ("command", "construct"),
        [
            pytest.param("echo ${(U)HOME}", "zsh `${(flags)…}` parameter flags", id="zsh-parameter-flag"),
            pytest.param('echo $"x"', 'a `$"…"` locale string', id="locale-string"),
            pytest.param("echo .(e:'date':)", "a zsh glob qualifier that runs code", id="code-running-glob-qualifier"),
        ],
    )
    def test_unreadable_syntax_block_names_the_construct(self, guard: Callable, command: str, construct: str) -> None:
        """A command with no git at all, blocked for syntax the guards cannot read, is told which syntax — not a push.

        The block itself stays (the syntax may hide a push), but a message opening with "git push blocked" and a force
        ban sends the reader looking for a push that is not there.
        """
        result = guard(command)

        assert result.returncode == 2
        assert construct in result.stderr
        assert not result.stderr.startswith("git push blocked")
        assert "force" not in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --force", id="force-flag"),
            pytest.param("git push origin +main", id="plus-refspec"),
        ],
    )
    def test_force_push_blocked_even_with_its_own_token(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str
    ) -> None:
        """A token whose question showed the very force command does NOT authorize it — the force check runs first.

        Any spelling of a non-force push spends a token, so only the order of the checks stops a force push here.
        """
        token = push_token(git_repo, command)

        result = guard(command)

        assert result.returncode == 2
        assert "force-push is forbidden" in result.stderr
        assert token.exists()

    def test_push_blocked_without_token(self, guard: Callable) -> None:
        """Plain push with no token → exit 2; stderr points at the ``git-push`` AskUserQuestion."""
        result = guard(APPROVED)

        assert result.returncode == 2
        assert "AskUserQuestion" in result.stderr
        assert "git-push" in result.stderr

    def test_push_allowed_once_and_token_spent(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """The approved command at the approved branch and HEAD → exit 0, and the token is deleted as it is allowed."""
        token = push_token(git_repo)

        result = guard(APPROVED)

        assert result.returncode == 0, result.stderr
        assert not token.exists()

    def test_second_push_after_spending_is_blocked(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """One approval allows one push: the same command again finds no token (a failed push needs a new approval)."""
        push_token(git_repo)
        guard(APPROVED)

        result = guard(APPROVED)

        assert result.returncode == 2
        assert "AskUserQuestion" in result.stderr

    def test_shipped_block_with_comment_runs(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """A shipped block's ``# timeout: N`` comment is no second push: ``git push # timeout: 30000`` runs."""
        push_token(git_repo, "git push")

        result = guard("git push # timeout: 30000")

        assert result.returncode == 0, result.stderr

    def test_push_not_auto_armed_by_prompt(self, run_hook: Callable, git_repo: Path) -> None:
        """UserPromptSubmit mentioning 'push' must NOT create a push token."""
        result = _event(run_hook, git_repo, {"hook_event_name": "UserPromptSubmit", "user_message": "push this"})

        assert result.returncode == 0, result.stderr
        assert not (git_repo / ".git" / PUSH_RECORD).exists()

    def test_legacy_sentinel_no_longer_authorizes(self, guard: Callable, legacy_sentinel: Path) -> None:
        """A fresh sentinel of the former mechanism is ignored: without a token the push stays blocked."""
        legacy_sentinel.touch()

        result = guard(APPROVED)

        assert result.returncode == 2
        assert "AskUserQuestion" in result.stderr
        assert "touch" not in result.stderr


#: oss:resolve Step 10's former explicit-refspec fallback block, verbatim: one push among reads, a guard and an echo
#: whose text names a push. Its remote and ref are variables, which no token covers (decision "approved branch only"),
#: so Step 10 now hands that push to the user instead of running it.
RESOLVE_FALLBACK = (
    'export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"\n'
    'IFS= read -r FORK_REMOTE < "${TMPDIR:-/tmp}/resolve-fork-remote-${CSID}" 2>/dev/null || FORK_REMOTE=""\n'
    'IFS= read -r HEAD_REF < "${TMPDIR:-/tmp}/resolve-head-ref-${CSID}" 2>/dev/null || HEAD_REF=""\n'
    "# empty refspec → push to wrong ref\n"
    '[ -n "$FORK_REMOTE" ] && [ -n "$HEAD_REF" ] || { echo "⛔ Step 10 fallback: FORK_REMOTE/HEAD_REF unresolved — '
    'refusing explicit-refspec push"; exit 1; }\n'
    'git push "$FORK_REMOTE" HEAD:"$HEAD_REF" # timeout: 30000'
)


@_skip_git_unavailable
class TestOnePushPerCommand:
    """A valid token allows one push of the current branch at the approved HEAD to a configured remote, once.

    Plain spellings spend it: quoting, redirections, pipes, covered options, another configured remote,
    ``HEAD:<branch>`` and a substitution printing the current branch. A command that could push twice — a second push,
    a loop, a function, a repeating runner — or that runs git that can move HEAD or a branch before the push is blocked
    and keeps the token.
    """

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push", id="bare"),
            pytest.param("git push origin", id="remote-only"),
            pytest.param("git push -u origin HEAD", id="set-upstream-head"),
            pytest.param("git push origin HEAD:main", id="head-to-branch"),
            pytest.param("git push origin HEAD:refs/heads/main", id="head-to-full-ref"),
            pytest.param("git push origin refs/heads/main:refs/heads/main", id="full-ref-both-sides"),
            pytest.param("git push upstream HEAD:refs/heads/main", id="other-configured-remote"),
            pytest.param('git push origin "main"', id="quoted-word"),
            pytest.param("git push origin main 2>&1", id="redirection"),
            pytest.param("git push origin main > push.log", id="redirection-to-file"),
            pytest.param("git push origin main | cat", id="pipe"),
            pytest.param("git push --quiet --atomic -o ci.skip --no-verify origin main", id="covered-options"),
            pytest.param("git push -uq origin main", id="short-cluster"),
            pytest.param("git push origin -- main", id="end-of-options"),
            pytest.param("git -c user.name=x push origin main", id="inline-config-elsewhere"),
            pytest.param("git status && git log -1 --oneline && git push origin main", id="read-only-git-beside"),
            pytest.param("bash -c 'git push origin main'", id="bash-c"),
            pytest.param("GIT push origin main", id="upper-case-program"),
            pytest.param("git lfs push origin main", id="lfs-push"),
            pytest.param('git push -u origin "$(git branch --show-current)"', id="branch-substitution"),
            pytest.param("git push -u origin `git branch --show-current`", id="branch-substitution-backticks"),
            pytest.param('git push origin HEAD:"$(git branch --show-current)"', id="branch-substitution-as-dst"),
            pytest.param('git push origin "$(git rev-parse --abbrev-ref HEAD)"', id="rev-parse-substitution"),
        ],
    )
    def test_plain_spelling_spends_the_token(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str
    ) -> None:
        """One push of the current branch, from the approved checkout, runs and spends the token."""
        token = push_token(git_repo)

        result = guard(command)

        assert result.returncode == 0, f"{command!r} was blocked: {result.stderr}"
        assert not token.exists()

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            pytest.param("git push origin main; git push origin main", "more than one push", id="same-push-twice"),
            pytest.param("git push origin main && git push origin main", "more than one push", id="second-push"),
            pytest.param("git push origin main\ngit push origin main", "more than one push", id="next-line"),
            pytest.param(
                "git push origin main; git commit --allow-empty -m x; git push origin main",
                "more than one push",
                id="push-commit-push",
            ),
            pytest.param("bash -c 'git push origin main; git push'", "more than one push", id="bash-c-twice"),
            pytest.param(
                "git push origin main; git -c alias.q=push q origin main", "more than one push", id="alias-second-push"
            ),
            pytest.param(
                "git push origin main; git lfs push origin main", "more than one push", id="push-then-lfs-push"
            ),
            pytest.param('git push origin "$(git push origin main)"', "more than one push", id="push-in-substitution"),
            pytest.param("for r in a b; do git push origin main; done", "repeats", id="for-loop"),
            pytest.param("while true; do git push; done", "repeats", id="while-loop"),
            pytest.param("f() { git push origin main; }; f; f", "repeats", id="function"),
            pytest.param("watch git push origin main", "repeats", id="watch"),
            pytest.param("git commit -m wip && git push origin main", "git commit", id="commit-before"),
            pytest.param(
                "git update-ref refs/heads/main HEAD && git push origin main", "git update-ref", id="update-ref"
            ),
            pytest.param("git checkout other && git push origin HEAD:main", "git checkout", id="checkout-before"),
            pytest.param("git reset --hard HEAD && git push origin main", "git reset", id="reset-before"),
            pytest.param("git config remote.origin.mirror true && git push origin", "git config", id="config-write"),
            pytest.param("git remote set-url origin ../x && git push origin main", "git remote", id="remote-write"),
        ],
    )
    def test_blocked_and_token_kept(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str, reason: str
    ) -> None:
        """A command that could push more than once, or move HEAD before the push, keeps the token and says why.

        The token names the HEAD the user approved: a commit, reset or checkout earlier in the same command would push
        another commit, so it runs in its own Bash call first and the push needs a new approval.
        """
        token = push_token(git_repo)

        result = guard(command)

        assert result.returncode == 2, f"{command!r} was allowed: {result.stdout}{result.stderr}"
        assert reason in result.stderr
        assert token.exists()

    @pytest.mark.parametrize(
        ("command", "cause"),
        [
            pytest.param('git push origin "$(echo main)"', "substitution", id="other-substitution"),
            pytest.param("git push origin 'main other'", "another ref", id="quoted-space"),
        ],
    )
    def test_block_names_its_real_cause(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str, cause: str
    ) -> None:
        """One push the lexer's two readings split differently blocks for its own cause, never as a second push.

        The precise and coarse readings split a substitution and a quoted space differently; that difference is no
        second push, so the message must not claim one.
        """
        token = push_token(git_repo)

        result = guard(command)

        assert result.returncode == 2
        assert cause in result.stderr
        assert "more than one push" not in result.stderr
        assert token.exists()

    def test_glob_variant_hits_the_force_ban_first(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """An unquoted glob may expand to a planted ``-f``: it reads as force before the token is even looked up."""
        token = push_token(git_repo)

        result = guard("git push origin ma?n")

        assert result.returncode == 2
        assert "force" in result.stderr
        assert token.exists()


#: Pushes no approval covers (decision "approved branch only"), with the reason each names.
_NEVER_COVERED = [
    pytest.param("git push origin --delete main", "--delete", id="delete-long"),
    pytest.param("git push -d origin main", "-d", id="delete-short"),
    pytest.param("git push origin :main", "deletes", id="empty-source"),
    pytest.param("git push --prune origin", "--prune", id="prune"),
    pytest.param("git push --prune origin 'refs/heads/*:refs/heads/*'", "--prune", id="prune-glob"),
    pytest.param("git push origin --all", "--all", id="all"),
    pytest.param("git push origin --branches", "--branches", id="branches"),
    pytest.param("git push origin --tags", "--tags", id="tags"),
    pytest.param("git push --follow-tags origin main", "--follow-tags", id="follow-tags"),
    pytest.param("git push --del origin main", "--del", id="delete-abbreviated"),
    pytest.param("git push --pru origin", "--pru", id="prune-abbreviated"),
    pytest.param("git push --fol origin main", "--fol", id="follow-tags-abbreviated"),
    pytest.param("git push --d origin main", "--d", id="ambiguous-abbreviation"),
    pytest.param("git push origin other", "another ref", id="other-local-branch"),
    pytest.param("git push origin HEAD:other", "another remote ref", id="head-to-other-branch"),
    pytest.param("git push origin other:main", "another ref", id="other-branch-to-main"),
    pytest.param("git push origin refs/tags/v1", "another ref", id="tag-ref"),
    pytest.param("git push origin 'refs/heads/*:refs/heads/*'", "another ref", id="quoted-glob-refspec"),
    pytest.param("git push origin refs/heads/push", "another ref", id="ref-named-push"),
    pytest.param("git push -u origin feature", "another ref", id="set-upstream-other-branch"),
    pytest.param("git push https://example.invalid/x.git HEAD", "configured remote", id="url-destination"),
    pytest.param("git push ../other HEAD", "configured remote", id="path-destination"),
    pytest.param("git push nowhere main", "configured remote", id="unknown-remote-name"),
    pytest.param("git push --repo=../other main", "--repo", id="repo-option"),
    pytest.param("git push --receive-pack=x origin main", "--receive-pack", id="receive-pack"),
    pytest.param("git push --recurse-submodules=on-demand origin main", "--recurse-submodules", id="submodules"),
    pytest.param('git push "$REMOTE" main', "variable or substitution", id="variable-remote"),
    pytest.param('git push origin "$BRANCH"', "variable or substitution", id="variable-refspec"),
    pytest.param('git push origin HEAD:"$REF"', "variable or substitution", id="variable-dst"),
    pytest.param(RESOLVE_FALLBACK, "variable or substitution", id="oss-resolve-fallback-block"),
    pytest.param("cd ../other && git push origin main", "changes directory", id="cd"),
    pytest.param("(cd ../other && git push origin main)", "changes directory", id="subshell-cd"),
    pytest.param("pushd ../other; git push origin main", "changes directory", id="pushd"),
    pytest.param("popd; git push origin main", "changes directory", id="popd"),
    pytest.param("bash -c 'cd ../x; git push'", "changes directory", id="cd-in-bash-c"),
    pytest.param("command cd ../x && git push", "changes directory", id="command-cd"),
    pytest.param("git -C ../other push origin main", "another repository", id="dash-c"),
    pytest.param("git --git-dir=../other/.git push origin main", "another repository", id="git-dir"),
    pytest.param("git --work-tree ../other push origin main", "another repository", id="work-tree"),
    pytest.param("GIT_DIR=../other/.git git push origin main", "another repository", id="git-dir-variable"),
    pytest.param("git -c remote.origin.pushurl=../x push origin main", "remote.origin.pushurl", id="inline-pushurl"),
    pytest.param("git -c url.../x.pushInsteadOf=../ push origin main", "url.", id="inline-push-instead-of"),
    pytest.param("git -c push.default=matching push origin", "push.default", id="inline-push-default"),
    pytest.param("git --config-env=remote.origin.url=X push origin main", "remote.origin.url", id="config-env"),
    pytest.param(
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl GIT_CONFIG_VALUE_0=../x git push origin main",
        "GIT_CONFIG",
        id="config-environment",
    ),
    pytest.param("HOME=../elsewhere git push origin main", "HOME", id="home-swaps-global-config"),
    pytest.param("git send-pack origin main", "send-pack", id="send-pack"),
    pytest.param("git http-push https://example.invalid/r.git main", "http-push", id="http-push"),
    pytest.param("git subtree push --prefix=lib origin main", "subtree", id="subtree-push"),
    pytest.param("git lfs push --all origin", "--all", id="lfs-push-all"),
    pytest.param("git lfs push origin other", "another ref", id="lfs-push-other-branch"),
    pytest.param("git push origin '{-f,x}'", "another ref", id="quoted-braces-refspec"),
]


@_skip_git_unavailable
class TestApprovedBranchOnly:
    """A token covers one push of the current branch at the approved HEAD to a configured remote name — nothing else.

    By the user's decision a delete, a prune, every-ref and tag pushes, another ref, a URL or path destination, a
    variable or substitution naming the target, a directory change and the plumbing push programs are never covered:
    with or without a token the guard blocks, keeps the token, and tells Claude the user runs that push by hand.
    """

    @pytest.mark.parametrize(("command", "reason"), _NEVER_COVERED)
    def test_blocked_with_token_kept(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str, reason: str
    ) -> None:
        """The push names something the approval never covers: exit 2, the reason, the by-hand advice, token kept."""
        token = push_token(git_repo)

        result = guard(command)

        assert result.returncode == 2, f"{command!r} was allowed: {result.stdout}{result.stderr}"
        assert reason in result.stderr
        assert BY_HAND in result.stderr
        assert token.exists()

    @pytest.mark.parametrize(("command", "reason"), _NEVER_COVERED)
    def test_blocked_without_token_says_no_question_helps(self, guard: Callable, command: str, reason: str) -> None:
        """Without a token the advice is the same: a ``git-push`` answer would record a token that never fits.

        The scope check runs before the token lookup, so the reason is the push's own. None of these is a force push, so
        the force message must not appear either.
        """
        result = guard(command)

        assert result.returncode == 2
        assert reason in result.stderr
        assert BY_HAND in result.stderr
        assert "AskUserQuestion" not in result.stderr
        assert "force" not in result.stderr


@_skip_git_unavailable
class TestDryRun:
    """``git push --dry-run``/``-n`` sends nothing: it needs no token and never spends one."""

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --dry-run origin main", id="long"),
            pytest.param("git push -n origin main", id="short"),
            pytest.param("git push -nu origin main", id="short-cluster"),
            pytest.param("git push origin main --dry-run", id="after-positionals"),
            pytest.param("git push --dry-run --porcelain origin HEAD", id="porcelain"),
            pytest.param("git lfs push --dry-run origin main", id="lfs"),
        ],
    )
    def test_runs_without_token(self, guard: Callable, command: str) -> None:
        """A dry run passes with no token at all."""
        result = guard(command)

        assert result.returncode == 0, result.stderr

    def test_keeps_a_pending_token(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """A dry run before the real push leaves the user's approval for that push."""
        token = push_token(git_repo)

        result = guard("git push --dry-run origin main")

        assert result.returncode == 0, result.stderr
        assert token.exists()

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git push --dry-run --no-dry-run origin main", id="negated-later"),
            pytest.param("git push -o n origin main", id="n-is-a-push-option-value"),
            pytest.param("git push -on origin main", id="n-glued-to-push-option"),
            pytest.param("git push --push-option --dry-run origin main", id="dry-run-as-push-option-value"),
            pytest.param("git push --push-o --dry-run origin main", id="abbreviated-value-option"),
            pytest.param('git push --dry-run "$FLAGS" origin main', id="variable-may-undo-it"),
            pytest.param("git push origin -- --dry-run", id="after-end-of-options"),
            pytest.param("git push --dry-run origin main; git push origin main", id="beside-a-real-push"),
            pytest.param("git send-pack --dry-run origin main", id="plumbing"),
        ],
    )
    def test_real_push_is_no_dry_run(self, guard: Callable, command: str) -> None:
        """A spelling that still sends refs is no dry run: without a token it blocks."""
        result = guard(command)

        assert result.returncode == 2, f"{command!r} ran as a dry run"


def _spawn_guard(repo: Path, command: str, tool_use_id: str, transcript: Path) -> subprocess.Popen:
    """Start one commit-guard run on ``command`` without waiting for it, as a parallel tool call reaches the hook."""
    hook = Path(__file__).resolve().parents[2] / "hooks" / "commit-guard.js"
    payload = _push_payload(command, repo, tool_use_id=tool_use_id, transcript_path=str(transcript))
    proc = subprocess.Popen(
        ["node", str(hook)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=repo,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload).encode("utf-8"))
    proc.stdin.close()
    return proc


@_skip_git_unavailable
class TestSpendBoundaries:
    """Who may spend the token and how often: the lead session's own call, once, and only one of concurrent calls."""

    def test_spawned_agent_never_spends(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """A subagent or teammate payload carries ``agent_id``: the user's approval is not its to spend."""
        token = push_token(git_repo)

        result = guard(APPROVED, agent_id="agent-1")

        assert result.returncode == 2
        assert "spawned agent" in result.stderr
        assert token.exists()

    @pytest.mark.parametrize(
        ("token_fields", "call_fields"),
        [
            pytest.param({"question_id": "toolu_never_asked"}, {}, id="question-not-in-transcript"),
            pytest.param({}, {"transcript_path": None}, id="call-names-no-transcript"),
        ],
    )
    def test_unproven_token_never_spends(
        self, guard: Callable, git_repo: Path, push_token: Callable, token_fields: dict, call_fields: dict
    ) -> None:
        """A token the session transcript does not prove — a question never asked, or no transcript named — is kept.

        The spend checks that the user answered ``Approve`` to the very question the token names, so a token written by
        any other means than the writer buys nothing.
        """
        token = push_token(git_repo, **token_fields)

        result = guard(APPROVED, **call_fields)

        assert result.returncode == 2, result.stderr
        assert "AskUserQuestion" in result.stderr
        assert token.exists()

    def test_call_without_tool_use_id_never_spends(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """The spend is claimed per tool call; a payload naming none cannot claim it and keeps the token."""
        token = push_token(git_repo)

        result = guard(APPROVED, tool_use_id=None)

        assert result.returncode == 2
        assert "tool_use_id" in result.stderr
        assert token.exists()

    @pytest.mark.parametrize("trial", [pytest.param(n, id=f"trial-{n}") for n in range(1, 6)])
    def test_concurrent_calls_spend_one_token_once(
        self, git_repo: Path, push_token: Callable, approval_transcript: Callable[[Path], Path], trial: int
    ) -> None:
        """Three parallel push calls with one token: exactly one runs.

        Parallel Bash tool calls reach the hook at once. A plain unlink lets every caller "delete" the token on APFS;
        the library's rename claim has one winner. Several trials, because a race that is lost only sometimes would pass
        a single run.
        """
        token = push_token(git_repo)
        transcript = approval_transcript(git_repo)
        procs = [
            _spawn_guard(git_repo, APPROVED, f"toolu_{trial}_{n}_{uuid.uuid4().hex}", transcript) for n in range(3)
        ]

        codes = sorted(proc.wait(timeout=30) for proc in procs)

        assert codes == [0, 2, 2]
        assert not token.exists()


@_skip_git_unavailable
class TestFailClosed:
    """An exception on the push path exits 2: exit 1 is a non-blocking error, which would let the push run."""

    @pytest.fixture
    def throwing_hook(self, tmp_path: Path) -> Path:
        """Copy commit-guard.js with the real lexer and an approval library whose every function throws."""
        hooks = Path(__file__).resolve().parents[2] / "hooks"
        hook = tmp_path / "throwing-hooks" / "commit-guard.js"
        (hook.parent / "lib").mkdir(parents=True)
        hook.write_bytes((hooks / "commit-guard.js").read_bytes())
        (hook.parent / "lib" / "shell-git.js").write_bytes((hooks / "lib" / "shell-git.js").read_bytes())
        (hook.parent / "lib" / "approval-grants.js").write_text(
            "module.exports = new Proxy({}, { get: () => () => { throw new Error('library fault'); } });\n",
            encoding="utf-8",
            newline="\n",
        )
        return hook

    def test_library_exception_blocks_the_push(self, throwing_hook: Path, git_repo: Path) -> None:
        """A throw while reading the token or the checkout blocks with exit 2 and names the fault."""
        result = subprocess.run(
            ["node", str(throwing_hook)],
            input=json.dumps(_push_payload(APPROVED, git_repo)),
            cwd=git_repo,
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(git_repo)},
        )

        assert result.returncode == 2, result.stderr
        assert "library fault" in result.stderr


#: The remote writes a token can cover; ``send-pack``, ``http-push`` and ``subtree push`` never are (_NEVER_COVERED).
_REMOTE_WRITES = [
    pytest.param("git push origin main", id="push"),
    pytest.param("git lfs push origin main", id="lfs-push"),
]


@_skip_git_unavailable
class TestRemoteWriteFamily:
    """Every coverable remote write asks: one ``Approve`` token covers exactly one write.

    ``git lfs push`` uploads objects to the remote like any push; before it was read as a push it ran with no token. The
    plumbing programs take a URL or path rather than a configured remote name, and ``subtree push`` sends a split commit
    rather than HEAD, so no token covers them.
    """

    @pytest.mark.parametrize("command", _REMOTE_WRITES)
    def test_blocked_without_token(self, guard: Callable, command: str) -> None:
        """Without a token each remote-write spelling stops at the ``git-push`` question, never at the force ban."""
        result = guard(command)

        assert result.returncode == 2, f"{command!r} ran without approval"
        assert "git-push" in result.stderr
        assert "force-push is forbidden" not in result.stderr

    @pytest.mark.parametrize("command", _REMOTE_WRITES)
    def test_token_allows_it_once(self, guard: Callable, git_repo: Path, push_token: Callable, command: str) -> None:
        """A valid token lets the remote write run and is deleted as it is allowed."""
        token = push_token(git_repo, command)

        result = guard(command)

        assert result.returncode == 0, f"{command!r} was blocked: {result.stderr}"
        assert not token.exists()

    @pytest.mark.parametrize("command", _REMOTE_WRITES)
    def test_spent_token_allows_no_second_write(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str
    ) -> None:
        """The same remote write again, after the token was spent, needs a new approval."""
        push_token(git_repo, command)
        guard(command)

        result = guard(command)

        assert result.returncode == 2
        assert "git-push" in result.stderr


#: Local Git operations, destructive ones included, and remote reads; none writes to a remote.
_LOCAL_GIT_AND_REMOTE_READS = [
    pytest.param("git add f", id="add"),
    pytest.param("git merge feature", id="merge"),
    pytest.param("git merge --no-ff feature", id="merge-no-ff"),
    pytest.param("git cherry-pick abc", id="cherry-pick"),
    pytest.param("git switch -c x", id="switch-create"),
    pytest.param("git worktree add ../w -b x", id="worktree-add"),
    pytest.param("git reset --hard HEAD~1", id="reset-hard"),
    pytest.param("git clean -fd", id="clean"),
    pytest.param("git branch -D x", id="branch-force-delete"),
    pytest.param("git stash", id="stash"),
    pytest.param("git rebase main", id="rebase"),
    pytest.param("git fetch origin", id="fetch"),
    pytest.param("git pull", id="pull"),
    pytest.param("git ls-remote origin", id="ls-remote"),
]


@_skip_git_unavailable
class TestLocalGitPassesGuards:
    """No PreToolUse guard stops a local Git operation or a remote read when no approval record exists."""

    @pytest.mark.parametrize("hook", ["commit-guard.js", "gh-write-guard.js"])
    @pytest.mark.parametrize("command", _LOCAL_GIT_AND_REMOTE_READS)
    def test_passes_without_any_record(self, run_hook: Callable, git_repo: Path, hook: str, command: str) -> None:
        """The guard exits 0 with no deny and no message on stderr.

        The session project holds no push token and no grant, so nothing here is allowed by a record: only a remote
        write may stop at a guard, and these commands write nothing to a remote. The hooks read the command text and
        never run it; the dummy GitHub host keeps any stray gh call off the network.
        """
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(git_repo),
        }

        result = run_hook(
            hook,
            payload,
            cwd=git_repo,
            env_extra={"CLAUDE_PROJECT_DIR": str(git_repo), "GH_TOKEN": "dummy", "GH_HOST": "x.invalid"},
        )

        assert (result.returncode, result.stderr) == (0, ""), f"{hook} stopped {command!r}"
        assert '"deny"' not in result.stdout


@_skip_git_unavailable
class TestTokenValidity:
    """Only an unexpired token of a known version and scope, in the session project's own common dir, at the current
    branch and HEAD, is spent."""

    def test_new_commit_blocks(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """A commit made after the approval moves HEAD: the user approved pushing the old one."""
        token = push_token(git_repo)
        _git(git_repo, "commit", "--allow-empty", "-m", "later")

        result = guard(APPROVED)

        assert result.returncode == 2
        assert "HEAD moved" in result.stderr
        assert token.exists()

    def test_other_branch_blocks(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """The same push of HEAD on another branch at the same commit is not the approved push.

        ``HEAD`` names whatever branch is checked out, so the refspec rule passes it and the token's branch decides.
        """
        token = push_token(git_repo)
        _git(git_repo, "switch", "-c", "other")

        result = guard("git push origin HEAD")

        assert result.returncode == 2
        assert "the current branch is not the one the push was approved on" in result.stderr
        assert token.exists()

    def test_detached_head_blocks(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """A detached HEAD has no branch to match, even at the approved commit."""
        push_token(git_repo)
        _git(git_repo, "switch", "--detach")

        result = guard(APPROVED)

        assert result.returncode == 2
        assert "detached" in result.stderr

    def test_expired_token_blocks_and_is_deleted(self, guard: Callable, git_repo: Path, push_token: Callable) -> None:
        """A token past its 15 minutes is refused and removed, as the former sentinel was on expiry."""
        token = push_token(git_repo, expires_at="2000-01-01T00:00:00.000Z")

        result = guard(APPROVED)

        assert result.returncode == 2
        assert "expired" in result.stderr
        assert not token.exists()

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            pytest.param({"version": 2}, "unknown record version", id="unknown-version"),
            pytest.param({"scope": "gh-read"}, "unknown scope", id="other-scope"),
            pytest.param({"answer": "Deny"}, "does not hold answer", id="other-answer"),
            pytest.param({"head": None}, "lacks head", id="missing-head"),
            pytest.param({"branch": None}, "lacks branch", id="missing-branch"),
            pytest.param({"expires_at": "soon"}, "no ISO expires_at", id="malformed-expiry"),
        ],
    )
    def test_malformed_token_blocks(
        self, guard: Callable, git_repo: Path, push_token: Callable, overrides: dict, reason: str
    ) -> None:
        """A token another release wrote, or one hand-edited, fails closed with the reader's reason."""
        push_token(git_repo, **overrides)

        result = guard(APPROVED)

        assert result.returncode == 2
        assert reason in result.stderr

    def test_token_of_another_project_blocks(
        self, guard: Callable, git_repo: Path, push_token: Callable, tmp_path: Path
    ) -> None:
        """A push from a checkout outside the session project finds no token there, even holding a valid one."""
        other = tmp_path / "other"
        other.mkdir()
        _git(other, "init", "-b", "main")
        _git(other, "commit", "--allow-empty", "-m", "init")
        _git(other, "remote", "add", "origin", str(tmp_path / "remote.git"))
        push_token(other)

        result = guard(APPROVED, cwd=other, project=git_repo)

        assert result.returncode == 2
        assert "not the session project's" in result.stderr

    @_skip_without_symlinks
    def test_symlinked_token_blocks(
        self, guard: Callable, git_repo: Path, push_token: Callable, tmp_path: Path
    ) -> None:
        """A token path that is a link is refused, so a valid token elsewhere cannot be borrowed through it."""
        real = push_token(git_repo)
        moved = tmp_path / "elsewhere.json"
        real.rename(moved)
        real.symlink_to(moved)

        result = guard(APPROVED)

        assert result.returncode == 2
        assert "symbolic link" in result.stderr

    def test_linked_worktree_checks_its_own_branch(
        self, guard: Callable, git_repo: Path, push_token: Callable, tmp_path: Path
    ) -> None:
        """A linked worktree shares the token but is checked against its own branch, so main's token does not fit."""
        push_token(git_repo)
        worktree = tmp_path / "wt"
        _git(git_repo, "worktree", "add", "-b", "feature", str(worktree))

        result = guard("git push origin HEAD", cwd=worktree)

        assert result.returncode == 2
        assert "the current branch is not the one the push was approved on" in result.stderr


@_skip_git_unavailable
class TestForcePushSpelling:
    """The force-push block must gate the action, not one spelling of it.

    Each command below reaches the remote with a non-fast-forward update, so each must be blocked even when a valid push
    token is present — the force check runs before the token lookup.
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
        ],
    )
    def test_force_push_spellings_blocked_with_token(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str
    ) -> None:
        """Every force spelling is blocked even with a valid token present."""
        push_token(git_repo)

        result = guard(command)

        assert result.returncode == 2, f"{command!r} was not blocked: {result.stdout}{result.stderr}"
        assert "force" in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            "git push -u origin main",
            pytest.param("{ git push; }", id="brace-group"),
            pytest.param("if true; then git push; fi", id="if-then"),
            pytest.param("time git push", id="time"),
            pytest.param("sudo -u root git push", id="sudo-with-option-value"),
            pytest.param('bash -c "git push origin main"', id="bash-c"),
            pytest.param("git push --no-force-with-lease origin main", id="negated-force-option"),
            pytest.param("GIT push origin main", id="upper-case-git"),
            pytest.param("git -c alias.p=push p origin main", id="inline-alias"),
            pytest.param("cat <<<hi\nsudo GIT push origin main", id="push-after-here-string-line"),
        ],
    )
    def test_non_force_push_spellings_still_need_token(self, guard: Callable, command: str) -> None:
        """A push reached via a chain, a group, a prefix or nested shell source still needs a token.

        ``--no-force-with-lease`` and ``-u`` guard the force test against over-reach: an option that negates a force
        option, and a short option that is not f, must reach the token gate rather than the unconditional force block.
        """
        result = guard(command)

        assert result.returncode == 2, f"{command!r} bypassed the token gate"
        assert "AskUserQuestion" in result.stderr
        assert "force-push is forbidden" not in result.stderr

    @pytest.mark.parametrize(
        "command",
        [
            "git -C /some/path push",
            "cd /somewhere && git push",
            "git push --follow-tags origin main",
            pytest.param('git -C "dir with space" push', id="quoted-dir-with-space"),
            pytest.param("git push --fol origin main", id="follow-tags-abbreviated"),
            pytest.param("git send-pack origin main", id="send-pack"),
            pytest.param("git push origin '{-f,x}'", id="quoted-braces-are-a-literal-refspec"),
            pytest.param(
                "python3 -c \"import subprocess; subprocess.run(['git', 'push', 'origin', 'main'])\"", id="python-c"
            ),
            pytest.param("git subtree push --prefix=docs origin main", id="subtree-push"),
            pytest.param("git subtree -P docs push origin main", id="subtree-options-first"),
            pytest.param("git SUBTREE push --prefix=docs origin main", id="subtree-upper-case"),
            pytest.param("git -c alias.s=subtree s push --prefix=docs origin main", id="alias-to-subtree"),
            pytest.param("git push origin '?main'", id="single-quoted-glob-is-literal"),
            pytest.param('git push origin "?main"', id="double-quoted-glob-is-literal"),
            pytest.param("git push origin \\?main", id="escaped-glob-is-literal"),
        ],
    )
    def test_never_covered_spellings_are_no_force_push(self, guard: Callable, command: str) -> None:
        """A push no token covers is still no force push: it blocks with the by-hand advice, never the force ban.

        ``--follow-tags``/``--fol`` contain an f but are no force option; a quoted glob or brace word is a literal
        refspec. They guard the force test against over-reach while the approved-branch rule blocks them.
        """
        result = guard(command)

        assert result.returncode == 2, f"{command!r} bypassed the gate"
        assert BY_HAND in result.stderr
        assert "force-push is forbidden" not in result.stderr

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
    def test_non_push_commands_still_pass(self, guard: Callable, command: str) -> None:
        """Commands that are not a push bypass the gate regardless of token state.

        Quoted text is one word to the lexer, never a command, so ``echo 'git push --force'`` passes. The heredoc case
        is the commit flow this repo uses: a message body naming a force push mid-line, with an apostrophe in it, is
        data — the lexer reads the heredoc as a body, so the fail-closed scan never sees it. A substitution that really
        does run git (``echo $(git push ...)``) is caught, and is covered separately.
        """
        result = guard(command)

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param('git push "$FORK_REMOTE" HEAD:"$HEAD_REF"', 2, id="oss-resolve-fork-push"),
            pytest.param('git push -u origin "$(git branch --show-current)"', 0, id="branch-substitution"),
        ],
    )
    def test_push_with_run_time_values_is_no_force_push(
        self, guard: Callable, git_repo: Path, push_token: Callable, command: str, expected: int
    ) -> None:
        """A push whose remote or refspec is expanded at run time is never mistaken for a force push.

        A substitution printing the current branch spends the token; a variable naming the remote or ref may name any
        target, so it is never covered and blocks with the by-hand advice, never at the force block.
        """
        push_token(git_repo)

        result = guard(command)

        assert result.returncode == expected, result.stderr
        assert "force" not in result.stderr


@_skip_git_unavailable
class TestRefDeletion:
    """Deleting remote refs is no force push, and no token covers it: the user runs it by hand."""

    @pytest.mark.parametrize("command", _REF_DELETIONS)
    def test_by_hand_never_force_banned(self, guard: Callable, command: str) -> None:
        """Without a token the deletion blocks with the by-hand advice; the force message must not appear.

        The user can still delete a remote ref from their own shell, so the block names that path, never the force ban.
        """
        result = guard(command)

        assert result.returncode == 2
        assert BY_HAND in result.stderr
        assert "force" not in result.stderr

    @pytest.mark.parametrize("command", _REF_DELETIONS)
    def test_token_never_spent(self, guard: Callable, git_repo: Path, push_token: Callable, command: str) -> None:
        """Even a token whose question showed the very deletion is not spent on it: it covers the branch push only."""
        token = push_token(git_repo, command)

        result = guard(command)

        assert result.returncode == 2
        assert BY_HAND in result.stderr
        assert token.exists()


@_skip_git_unavailable
class TestMissingLibrary:
    """Without a library the guard cannot parse a command or read a token, so it blocks every push-looking one."""

    @pytest.fixture
    def bare_hook(self, tmp_path: Path) -> Path:
        """Copy commit-guard.js to a directory with no ``lib/`` beside it, as a broken install would leave it."""
        hook = tmp_path / "bare-hooks" / "commit-guard.js"
        hook.parent.mkdir()
        hook.write_bytes((Path(__file__).resolve().parents[2] / "hooks" / "commit-guard.js").read_bytes())
        return hook

    @pytest.fixture
    def lexer_only_hook(self, bare_hook: Path) -> Path:
        """Give the copied hook ``lib/shell-git.js`` but not ``lib/approval-grants.js``."""
        lib = bare_hook.parent / "lib"
        lib.mkdir()
        source = Path(__file__).resolve().parents[2] / "hooks" / "lib" / "shell-git.js"
        (lib / "shell-git.js").write_bytes(source.read_bytes())
        return bare_hook

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
            input=json.dumps(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
            ),
            cwd=git_repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )

        assert result.returncode == expected, result.stderr
        assert ("shell-git.js" in result.stderr) == (expected == 2)

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            pytest.param("git push origin main", 2, id="push-blocked"),
            pytest.param("git status", 0, id="non-push-passes"),
        ],
    )
    def test_token_unreadable_fails_closed(
        self, lexer_only_hook: Path, git_repo: Path, push_token: Callable, command: str, expected: int
    ) -> None:
        """With the lexer but no approval-record library, even a valid token cannot be read: every push blocks."""
        push_token(git_repo)
        result = subprocess.run(
            ["node", str(lexer_only_hook)],
            input=json.dumps(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
            ),
            cwd=git_repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
            env={**os.environ, "CLAUDE_PROJECT_DIR": str(git_repo)},
        )

        assert result.returncode == expected, result.stderr
        assert ("approval-grants.js" in result.stderr) == (expected == 2)
