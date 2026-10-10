"""Subprocess tests for ``approval-guard.js``, ``lib/approval-grants.js`` and ``commit-guard.js --check-grant``.

Approval records stand for later operations once the user answered an approval question. One scope table in
``lib/approval-grants.js`` drives them: the local Git grant, the single-use push token and the single-use gh write
token. Three parts are covered:

* **Writer** (``PostToolUse`` on ``AskUserQuestion``) — only the user's ``Approve always`` answer to the exact
  ``git-approve`` question, whose ``Approve always`` description names the record file, writes
  ``<git common dir>/claude-git-approval.json``: one grant per project, seen from every worktree. ``Approve``, ``Deny``,
  other questions, shape drift, spawned-agent questions and rejected locations write nothing. ``git-push`` answered
  ``Approve`` writes the push token for one push of the current branch at the current HEAD; ``gh-write`` answered
  ``Approve`` writes the gh write token holding the one gh command the question names in an inline code span; ``Deny``
  to either removes a pending token. Whether the model pre-filled the answer is read from its own call in the session
  transcript, never from PostToolUse ``tool_input``. Writes are atomic and idempotent, so the four shipped copies leave
  one record, and a copy finding its answer already recorded stays silent. Real cases drive the push token against a
  local bare remote and the gh write token through ``gh-write-guard.js``, with a fake ``gh`` first on ``PATH``.
* **Grant check** (``node commit-guard.js --check-grant``) — prints ``grant .git/claude-git-approval.json@<created_at>``
  only for a valid record in the session's own common dir: git discovery variables removed, bare repositories refused,
  common dir named ``.git`` and linked back from its work tree, bound to ``CLAUDE_PROJECT_DIR`` when set. Unknown record
  versions and scopes fail closed.
* **Self-grant guard** (``PreToolUse`` on ``Bash|Write|Edit|MultiEdit|NotebookEdit``) — a record named as a write
  target, a write naming the common dir as a whole, a file-tool write to a record name or to a path named ``.git``, and
  any run of the writer exit 2. A mention (commit message, ``grep``, ``echo``), a scope id, a glob outside the common
  dir and a file-tool write elsewhere inside ``.git`` pass. Names are matched as the shell and filesystem resolve them;
  exclusion operands are not targets; content is never scanned.

Every case drives a hook the way the harness or the rule does — stdin payload or the ``--check-grant`` command line —
against disposable git checkouts under ``tmp_path``. Each subprocess runs with ``CLAUDE_PROJECT_DIR`` and every
``GIT_*`` variable removed unless the case sets one: pytest itself may run inside a Claude Code session.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parents[3]
HOOKS_DIR = PLUGINS_DIR / "cc_foundry" / "hooks"
HOOK = HOOKS_DIR / "approval-guard.js"
LIB = HOOKS_DIR / "lib" / "approval-grants.js"
COMMIT_GUARD = HOOKS_DIR / "commit-guard.js"
HOOK_PLUGINS = ["cc_foundry", "cc_oss", "cc_develop", "cc_research"]
GRANT_NAME = "claude-git-approval.json"
GRANT_RECORD = f"grant .git/{GRANT_NAME}@"
REVOKE = f'rm "$(git rev-parse --git-common-dir)/{GRANT_NAME}"'
QUESTION = "Commit 3 files on main as 'feat(hooks): add grant'?"
OPTIONS = ["Approve", "Approve always", "Deny"]
#: What the rule requires the ``Approve always`` option to tell the user: persistence, scope and the revoke path.
ALWAYS_DESCRIPTION = (
    "This commit, plus a standing grant for this project: later sessions in every worktree skip this question for "
    f"task-scoped non-destructive local Git operations (push still asks); revoke with {REVOKE}"
)
TOOL_USE_ID = "toolu_01GrantFixture"
#: Bytes the writer scans from the transcript tail before giving up (TRANSCRIPT_MAX_BYTES in the module).
TRANSCRIPT_MAX_BYTES = 16 * 1024 * 1024
LONG_S = "\N{LATIN SMALL LETTER LONG S}"
FULLWIDTH_G = "\N{FULLWIDTH LATIN SMALL LETTER G}"
#: Variables steering git discovery the module removes; the environment fixture removes them for every case.
DISCOVERY_ENV = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_INDEX_FILE",
)

_skip_node_or_git_unavailable = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("git") is None,
    reason="requires node and git to execute the hooks against a checkout",
)


def _symlinks_supported(probe_dir: Path) -> bool:
    """Return True when this host can create a symbolic link (Windows without developer mode cannot)."""
    link = probe_dir / ".symlink-probe"
    try:
        link.symlink_to(probe_dir)
    except OSError:
        return False
    link.unlink()
    return True


_skip_without_symlinks = pytest.mark.skipif(
    not _symlinks_supported(Path(__file__).resolve().parent), reason="requires symbolic link support"
)


def _clean_env(**overrides: str) -> dict[str, str]:
    """Return the test environment: no ``CLAUDE_PROJECT_DIR``, no ``GIT_*`` variable, then ``overrides``.

    Examples:
        >>> "CLAUDE_PROJECT_DIR" in _clean_env()
        False
        >>> _clean_env(GIT_DIR="x")["GIT_DIR"]
        'x'
    """
    env = {
        key: value for key, value in os.environ.items() if key != "CLAUDE_PROJECT_DIR" and not key.startswith("GIT_")
    }
    return {**env, **overrides}


def _git(*args: str, cwd: Path) -> None:
    """Run one git command in ``cwd`` with a throwaway identity, failing loudly."""
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        timeout=30,
        env=_clean_env(),
    )


def _new_checkout(path: Path) -> Path:
    """Create a git checkout with one commit at ``path``."""
    path.mkdir(parents=True)
    _git("init", "-b", "main", cwd=path)
    _git("commit", "--allow-empty", "-m", "init", cwd=path)
    return path


@pytest.fixture(name="checkout")
def _checkout(tmp_path: Path) -> Path:
    """Create a git checkout with one commit so worktrees can branch from it."""
    return _new_checkout(tmp_path / "repo")


def _grant(checkout: Path) -> Path:
    """Return the grant path of a main checkout: inside its git common dir, ``<checkout>/.git``."""
    return checkout / ".git" / GRANT_NAME


def _run_hook(payload: dict, hook: Path = HOOK, **env: str) -> subprocess.CompletedProcess:
    """Run ``approval-guard.js`` once with ``payload`` on stdin in the clean environment plus ``env``."""
    return subprocess.run(
        ["node", str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        env=_clean_env(**env),
    )


@pytest.fixture(name="run_hook")
def _run_hook_fixture() -> Callable[..., subprocess.CompletedProcess]:
    """Return a callable feeding one payload to the canonical hook."""
    return _run_hook


def _check(cwd: Path, **env: str) -> subprocess.CompletedProcess:
    """Run the rule's grant check, ``node commit-guard.js --check-grant``, in ``cwd``."""
    return subprocess.run(
        ["node", str(COMMIT_GUARD), "--check-grant"],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        env=_clean_env(**env),
    )


def _question(
    header: str = "git-approve",
    options: list[str] = OPTIONS,
    multi: bool = False,
    always: str = ALWAYS_DESCRIPTION,
    text: str = QUESTION,
) -> dict:
    """Build one AskUserQuestion question entry; ``always`` is the ``Approve always`` option description."""
    return {
        "question": text,
        "header": header,
        "options": [
            {"label": label, "description": always if label == "Approve always" else f"{label} effect"}
            for label in options
        ],
        "multiSelect": multi,
    }


def _call_line(block_input: dict, wire_input: dict | None = None, call_id: str = TOOL_USE_ID) -> str:
    """Build the assistant transcript line recording the model's own AskUserQuestion call."""
    block = {"type": "tool_use", "id": call_id, "name": "AskUserQuestion", "input": block_input}
    record = {
        "type": "assistant",
        "timestamp": _now_iso(),
        "message": {"role": "assistant", "content": [block]},
        "wireToolInputs": {call_id: block_input if wire_input is None else wire_input},
    }
    return json.dumps(record)


def _result_line(call_id: str = TOOL_USE_ID, asked: dict | None = None, answer: str = "Approve always") -> str:
    """Build the user transcript line carrying the tool result: the call id and the user's answer.

    Shaped as Claude Code writes it: ``message.content`` holds the ``tool_result`` block, and the top-level
    ``toolUseResult`` carries ``{questions, answers, annotations}`` with ``answers`` keyed by question text.
    """
    question = asked or _question()
    block = {"type": "tool_result", "tool_use_id": call_id, "content": "The user answered"}
    record = {
        "type": "user",
        "timestamp": _now_iso(),
        "message": {"role": "user", "content": [block]},
        "toolUseResult": {"questions": [question], "answers": {question["question"]: answer}, "annotations": {}},
    }
    return json.dumps(record)


def _now_iso() -> str:
    """Return the current instant as Claude Code stamps transcript lines: ISO 8601 UTC with milliseconds and ``Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _session_transcript(checkout: Path) -> Path:
    """Return the session transcript of a session in ``checkout``.

    The same file the conftest ``approval_transcript`` fixture names, so the ``answered`` fixture, the conftest token
    fixtures and the guard payloads of the real cases all read and write one transcript.

    Examples:
        >>> _session_transcript(Path("/w/repo")).name
        'repo-session.jsonl'
    """
    return checkout.parent / f"{checkout.name}-session.jsonl"


def _write_transcript(path: Path, lines: list[str]) -> Path:
    """Write JSONL transcript lines with LF endings and return the path."""
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    return path


def _progress_lines(count: int) -> list[str]:
    """Build later transcript lines with multi-byte text, about 750 bytes each."""
    return [json.dumps({"type": "progress", "text": "é✓" * 150, "n": n}, ensure_ascii=False) for n in range(count)]


@pytest.fixture(name="transcript")
def _transcript(tmp_path: Path) -> Path:
    """Session transcript in which the model asked the git-approve question without pre-filled answers."""
    prompt = json.dumps({"type": "user", "message": {"role": "user", "content": "commit it"}})
    return _write_transcript(
        _session_transcript(tmp_path / "repo"), [prompt, _call_line({"questions": [_question()]}), _result_line()]
    )


@pytest.fixture(name="answered")
def _answered(transcript: Path) -> Callable[..., dict]:
    """Return a builder of PostToolUse payloads for an answered AskUserQuestion call.

    Each payload gets a fresh ``tool_use_id`` whose call (the question, no pre-filled answer) and result (the user's
    answer) the builder appends to the session transcript, as the harness records them before the spend reads them. A
    case that supplies its own ``transcript_path`` or ``tool_use_id`` controls the transcript itself: nothing is
    appended, and the id defaults to ``TOOL_USE_ID``, the one its own lines use. ``extra`` overrides any top-level
    field.
    """
    calls = itertools.count(1)

    def _build(cwd: Path, answer: str, question: dict | None = None, **extra: object) -> dict:
        """Build one payload, recording its call and answer in the session transcript."""
        asked = question or _question()
        recorded = "transcript_path" not in extra and "tool_use_id" not in extra
        call_id = f"toolu_01Answer{next(calls):04d}" if recorded else TOOL_USE_ID
        if recorded:
            with transcript.open("a", encoding="utf-8", newline="\n") as log:
                log.write(f"{_call_line({'questions': [asked]}, call_id=call_id)}\n")
                log.write(f"{_result_line(call_id, asked, answer)}\n")
        return {
            "hook_event_name": "PostToolUse",
            "tool_name": "AskUserQuestion",
            "cwd": str(cwd),
            "session_id": "sess-grant",
            "transcript_path": str(transcript),
            "tool_use_id": call_id,
            "tool_input": {"questions": [asked]},
            "tool_response": {"questions": [asked], "answers": {asked["question"]: answer}, "annotations": {}},
            **extra,
        }

    return _build


def _pre(tool_name: str, tool_input: dict) -> dict:
    """Build a PreToolUse payload."""
    return {"hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input, "cwd": "."}


def _context(proc: subprocess.CompletedProcess) -> str:
    """Return the additionalContext the hook printed, or an empty string."""
    out = (proc.stdout or "").strip()
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def _valid_record(**overrides: object) -> str:
    """Return grant JSON as the writer records it, with ``overrides`` replacing fields."""
    record = {
        "version": 1,
        "scope": "local-git-non-destructive",
        "created_at": "2026-10-08T21:00:00.000Z",
        "question": QUESTION,
        "answer": "Approve always",
        **overrides,
    }
    return json.dumps(record) + "\n"


def _node_lib(expression: str) -> object:
    """Evaluate ``expression`` against the loaded module ``g`` in node and return its JSON value."""
    script = f"const g = require({json.dumps(str(LIB))}); process.stdout.write(JSON.stringify({expression}));"
    proc = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", check=True, timeout=15)
    return json.loads(proc.stdout)


# ── Scope table ───────────────────────────────────────────────────────────────


@_skip_node_or_git_unavailable
class TestScopeTable:
    """One table drives every scope: its rows carry what the writer, the reader and the Codex contract need."""

    def test_rows(self) -> None:
        """Each scope keeps the header, option labels, recording answer, file and lifetime the plan settled.

        These values are what the rules tell Claude to ask and what the Codex contract mirrors; drift on either side
        would make a question never record or a record never match its question.
        """
        rows = _node_lib(
            "g.SCOPES.map(r => [r.id, r.header, r.options, r.recordAnswer, r.file, r.fields, r.singleUse, r.ttlMs])"
        )
        assert rows == [
            [
                "local-git-non-destructive",
                "git-approve",
                ["Approve", "Approve always", "Deny"],
                "Approve always",
                "claude-git-approval.json",
                [],
                False,
                None,
            ],
            [
                "push-once",
                "git-push",
                ["Approve", "Deny"],
                "Approve",
                "claude-push-approval.json",
                ["question_id", "branch", "head", "expires_at"],
                True,
                15 * 60 * 1000,
            ],
            [
                "gh-write-once",
                "gh-write",
                ["Approve", "Deny"],
                "Approve",
                "claude-gh-write-approval.json",
                ["question_id", "command", "files_sha256", "expires_at"],
                True,
                15 * 60 * 1000,
            ],
        ]

    def test_record_family_and_common_fields(self) -> None:
        """Every record is one ``approval-record`` family at version 1 with the five common fields, in order."""
        assert _node_lib("[g.RECORD_FAMILY, g.RECORD_VERSION, g.COMMON_FIELDS]") == [
            "approval-record",
            1,
            ["version", "scope", "created_at", "question", "answer"],
        ]

    def test_every_row_is_written(self) -> None:
        """Each scope row has a field builder, so each question's recording answer writes its record."""
        assert _node_lib("Object.keys(g.RECORD_BUILDERS)") == [
            "local-git-non-destructive",
            "push-once",
            "gh-write-once",
        ]

    def test_only_a_token_deny_revokes(self) -> None:
        """Only the token questions' ``Deny`` removes a pending record; a skipped Git operation keeps its grant.

        A token stands for one answer, so the latest push or gh write answer decides; a standing grant outlives a
        ``Deny`` to one operation it never needed to cover.
        """
        assert _node_lib("g.SCOPES.map(r => [r.id, r.revokeAnswer])") == [
            ["local-git-non-destructive", None],
            ["push-once", "Deny"],
            ["gh-write-once", "Deny"],
        ]

    def test_every_covered_and_never_covered_list_is_non_empty(self) -> None:
        """A row without its covered and never-covered operations would leave the record's reach undefined."""
        assert _node_lib("g.SCOPES.filter(r => !r.covers.length || !r.neverCovers.length).map(r => r.id)") == []


# ── Writer ────────────────────────────────────────────────────────────────────


@_skip_node_or_git_unavailable
class TestGrantRecording:
    """Only the user's ``Approve always`` answer to the exact git-approve question records a grant."""

    def test_approve_always_writes_grant(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """``Approve always`` writes the versioned grant with the answer text inside the git common dir.

        This is the only path that creates standing authority, so every field the rule validates must be present.
        """
        proc = run_hook(answered(checkout, "Approve always"))
        grant = json.loads(_grant(checkout).read_text(encoding="utf-8"))
        assert proc.returncode == 0
        assert list(grant) == ["version", "scope", "created_at", "question", "answer"]
        assert {k: grant[k] for k in ("version", "scope", "question", "answer")} == {
            "version": 1,
            "scope": "local-git-non-destructive",
            "question": QUESTION,
            "answer": "Approve always",
        }
        assert datetime.fromisoformat(grant["created_at"].replace("Z", "+00:00")).tzinfo is not None
        assert GRANT_NAME in _context(proc)

    def test_grant_never_reaches_the_work_tree(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """The grant lives in git's own directory: no work-tree file, nothing to ignore, nothing git could track."""
        run_hook(answered(checkout, "Approve always"))
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--ignored"],
            cwd=checkout,
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=30,
            env=_clean_env(),
        )
        assert status.stdout == ""
        assert not (checkout / ".claude").exists()

    def test_answer_as_json_text_records(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """A ``tool_response`` delivered as JSON text is parsed like the object form."""
        payload = answered(checkout, "Approve always")
        payload["tool_response"] = json.dumps(payload["tool_response"])
        run_hook(payload)
        assert _grant(checkout).is_file()

    def test_subdirectory_session_writes_in_common_dir(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """A session running in a subdirectory still records the grant in the git common dir the check reads."""
        sub = checkout / "src" / "pkg"
        sub.mkdir(parents=True)
        run_hook(answered(sub, "Approve always"))
        assert _grant(checkout).is_file()

    @pytest.mark.parametrize(
        ("writer", "reader"),
        [
            pytest.param("worktree", "main", id="worktree-grant-seen-from-main"),
            pytest.param("main", "worktree", id="main-grant-seen-from-worktree"),
        ],
    )
    def test_one_grant_covers_every_worktree(
        self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path, writer: str, reader: str
    ) -> None:
        """A grant is per project: answered in one worktree, the grant check in every other worktree finds it.

        Linked worktrees share the main checkout's git common dir, so the user is asked once for the whole project; the
        session project is the main checkout in both directions.
        """
        trees = {"main": checkout, "worktree": tmp_path / "wt"}
        _git("worktree", "add", "-b", "feature", str(trees["worktree"]), cwd=checkout)
        run_hook(answered(trees[writer], "Approve always"), CLAUDE_PROJECT_DIR=str(checkout))
        proc = _check(trees[reader], CLAUDE_PROJECT_DIR=str(checkout))
        assert proc.stdout.startswith(GRANT_RECORD), proc.stdout
        assert proc.returncode == 0

    @pytest.mark.parametrize(
        ("answer", "question"),
        [
            pytest.param("Approve", None, id="approve-once"),
            pytest.param("Deny", None, id="deny"),
            pytest.param("yes, always", None, id="other-free-text"),
            pytest.param("Approve always", _question(header="Release"), id="other-header"),
            pytest.param("Approve always", _question(options=["Approve always", "Deny"]), id="missing-option"),
            pytest.param("Approve always", _question(options=[*OPTIONS, "Approve and push"]), id="extra-option"),
            pytest.param("Approve always", _question(multi=True), id="multi-select"),
            pytest.param("Approve, Approve always", _question(multi=True), id="multi-select-joined"),
            pytest.param("Approve", _question(header="git-push", options=OPTIONS), id="push-header-git-options"),
            pytest.param("Approve", _question(header="gh-write", options=OPTIONS), id="gh-write-header-git-options"),
        ],
    )
    def test_non_grant_answers_write_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, answer: str, question: dict | None
    ) -> None:
        """Only the exact question shape answered ``Approve always`` grants; everything else stays silent.

        ``Approve`` covers one operation, ``Deny`` skips it, and a drifted question shape must not turn an unrelated
        ``Approve always`` label into standing Git authority — nor a push or gh header carrying the Git option set into
        a push or gh write token.
        """
        proc = run_hook(answered(checkout, answer, question))
        assert proc.returncode == 0
        assert (proc.stdout or "") == ""
        assert [p.name for p in (checkout / ".git").iterdir() if "approval" in p.name] == []

    @pytest.mark.parametrize(
        "description",
        [
            pytest.param("Approve always effect", id="no-revoke-path"),
            pytest.param("Remember this for later sessions", id="persistence-only"),
            pytest.param("", id="empty"),
        ],
    )
    def test_undisclosed_always_option_refused(
        self, run_hook: Callable, answered: Callable, checkout: Path, description: str
    ) -> None:
        """An ``Approve always`` option whose description never names the grant file records nothing, and says why.

        The user grants standing authority only when told it persists, what it covers and how to revoke it; the grant
        file the revoke command deletes is the one part of that a hook can check, so a description lacking it refuses.
        """
        proc = run_hook(answered(checkout, "Approve always", _question(always=description)))
        assert not _grant(checkout).exists()
        assert "not recorded" in _context(proc)

    def test_spawned_agent_question_refused(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """A question asked from a spawned agent is reported and never recorded; grants belong to the lead session."""
        proc = run_hook(answered(checkout, "Approve always", agent_id="agent-123", agent_type="foundry:sw-engineer"))
        assert not _grant(checkout).exists()
        assert "spawned agent" in _context(proc)

    def test_outside_checkout_writes_nothing(self, run_hook: Callable, answered: Callable, tmp_path: Path) -> None:
        """Outside a git checkout there is nothing to grant, so no file is created anywhere."""
        plain = tmp_path / "plain"
        plain.mkdir()
        proc = run_hook(answered(plain, "Approve always"))
        assert list(plain.iterdir()) == []
        assert "not a git work tree" in _context(proc)

    @pytest.mark.parametrize(
        ("event", "tool"),
        [
            pytest.param("PostToolUse", "Bash", id="other-tool"),
            pytest.param("PreToolUse", "AskUserQuestion", id="pre-tool-use"),
        ],
    )
    def test_other_events_write_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, event: str, tool: str
    ) -> None:
        """The writer fires only after an AskUserQuestion call completes, never before the user answered."""
        payload = answered(checkout, "Approve always")
        payload.update(hook_event_name=event, tool_name=tool)
        proc = run_hook(payload)
        assert proc.returncode == 0
        assert not _grant(checkout).exists()


@_skip_node_or_git_unavailable
class TestWriterLocation:
    """The writer records only in the session project's own common dir, never through a link, atomically."""

    @_skip_without_symlinks
    def test_symlinked_grant_path_is_refused_and_target_untouched(
        self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path
    ) -> None:
        """A grant path that is a symbolic link is refused, and the file it points at keeps its content.

        Following the link would write grant JSON wherever the link points, and the check would then read a file outside
        git's own directory.
        """
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text("keep\n", encoding="utf-8")
        _grant(checkout).symlink_to(elsewhere)
        proc = run_hook(answered(checkout, "Approve always"))
        assert elsewhere.read_text(encoding="utf-8") == "keep\n"
        assert "symbolic link" in _context(proc)

    def test_directory_at_grant_path_is_refused(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """A directory occupying the grant name is no grant file; the writer reports it and leaves it alone."""
        _grant(checkout).mkdir()
        proc = run_hook(answered(checkout, "Approve always"))
        assert _grant(checkout).is_dir()
        assert "not a regular file" in _context(proc)

    def test_identical_grant_is_left_unchanged_in_silence(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """A second ``Approve always`` to the same question leaves the existing grant as it was, and says nothing.

        Four plugins ship the writer and each copy runs on the same answer; the later copies must find the record
        already holding that answer instead of rewriting it, and only the copy that wrote reports it, so the session
        does not read the same grant announced four times.
        """
        _grant(checkout).write_text(_valid_record(created_at="2000-01-01T00:00:00.000Z"), encoding="utf-8")
        proc = run_hook(answered(checkout, "Approve always"))
        grant = json.loads(_grant(checkout).read_text(encoding="utf-8"))
        assert grant["created_at"] == "2000-01-01T00:00:00.000Z"
        assert (proc.returncode, proc.stdout) == (0, "")

    @pytest.mark.parametrize(
        "existing",
        [
            pytest.param(_valid_record(question="An older question?"), id="other-question"),
            pytest.param(_valid_record(version=2), id="unknown-version"),
            pytest.param("{not json", id="malformed"),
        ],
    )
    def test_other_or_invalid_grant_is_replaced(
        self, run_hook: Callable, answered: Callable, checkout: Path, existing: str
    ) -> None:
        """A record for another question, or one the reader rejects, is replaced whole, leaving no temporary file."""
        _grant(checkout).write_text(existing, encoding="utf-8")
        run_hook(answered(checkout, "Approve always"))
        grant = json.loads(_grant(checkout).read_text(encoding="utf-8"))
        assert (grant["version"], grant["question"]) == (1, QUESTION)
        assert [p.name for p in _grant(checkout).parent.iterdir() if GRANT_NAME in p.name] == [GRANT_NAME]

    def test_every_shipped_copy_at_once_leaves_one_record(self, answered: Callable, checkout: Path) -> None:
        """The four plugin copies answering the same payload concurrently leave one valid record and no temp files.

        Nothing coordinates installed plugins: each registers its own writer, so writes must be atomic and idempotent.
        """
        payload = json.dumps(answered(checkout, "Approve always"))
        procs = [
            subprocess.Popen(
                ["node", str(PLUGINS_DIR / plugin / "hooks" / "approval-guard.js")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                env=_clean_env(),
            )
            for plugin in HOOK_PLUGINS
        ]
        codes = [proc.communicate(payload, timeout=30) and proc.returncode for proc in procs]
        grant = json.loads(_grant(checkout).read_text(encoding="utf-8"))
        assert codes == [0, 0, 0, 0]
        assert (grant["scope"], grant["answer"]) == ("local-git-non-destructive", "Approve always")
        assert [p.name for p in _grant(checkout).parent.iterdir() if GRANT_NAME in p.name] == [GRANT_NAME]

    def test_other_project_cwd_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path
    ) -> None:
        """An answer given while the session sits in another repository records nothing there or in the project.

        The hook binds the common dir to ``CLAUDE_PROJECT_DIR``: a ``cd`` into another checkout must not move standing
        authority into it.
        """
        other = _new_checkout(tmp_path / "other")
        proc = run_hook(answered(other, "Approve always"), CLAUDE_PROJECT_DIR=str(checkout))
        assert not _grant(other).exists()
        assert not _grant(checkout).exists()
        assert "not the session project's" in _context(proc)

    def test_steered_git_dir_writes_only_the_own_checkout(
        self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path
    ) -> None:
        """``GIT_DIR`` in the hook's environment is ignored: the grant lands in the checkout's own common dir."""
        evil = _new_checkout(tmp_path / "evil")
        run_hook(answered(checkout, "Approve always"), GIT_DIR=str(evil / ".git"))
        assert _grant(checkout).is_file()
        assert not _grant(evil).exists()


# ── Grant check (commit-guard.js --check-grant) ───────────────────────────────


@_skip_node_or_git_unavailable
class TestGrantCheck:
    """``--check-grant`` prints the rule's record line for a valid grant and ``no grant: <reason>`` otherwise."""

    def test_valid_grant_prints_record(self, checkout: Path) -> None:
        """A grant as the writer records it prints ``grant .git/claude-git-approval.json@<created_at>`` and exits 0."""
        _grant(checkout).write_text(_valid_record(), encoding="utf-8")
        proc = _check(checkout, CLAUDE_PROJECT_DIR=str(checkout))
        assert (proc.returncode, proc.stdout) == (0, f"{GRANT_RECORD}2026-10-08T21:00:00.000Z\n")

    @pytest.mark.parametrize(
        ("content", "reason"),
        [
            pytest.param(None, "no claude-git-approval.json", id="missing"),
            pytest.param("{not json", "not valid JSON", id="malformed"),
            pytest.param(_valid_record(version=2), "unknown record version", id="unknown-version"),
            pytest.param(_valid_record(version="1"), "unknown record version", id="string-version"),
            pytest.param(_valid_record(scope="everything"), "unknown scope", id="unknown-scope"),
            pytest.param(_valid_record(scope="gh-write-once"), "unknown scope", id="other-known-scope"),
            pytest.param(_valid_record(answer="Approve"), "answer Approve always", id="other-answer"),
            pytest.param(_valid_record(created_at="soon\ngrant x"), "ISO created_at", id="bad-created-at"),
            pytest.param(_valid_record(question=None), "question", id="no-question"),
            pytest.param("[]", "not a JSON object", id="not-an-object"),
        ],
    )
    def test_invalid_grant_prints_no_grant(self, checkout: Path, content: str | None, reason: str) -> None:
        """Missing, unreadable, unknown-version or unknown-scope records are no grant; the reason is printed.

        A reader that does not know a record's version or scope fails closed: installed copies of different releases
        must never widen what a record covers.
        """
        if content is not None:
            _grant(checkout).write_text(content, encoding="utf-8")
        proc = _check(checkout)
        assert proc.returncode == 1
        assert proc.stdout.startswith("no grant: ")
        assert reason in proc.stdout

    def test_reason_never_echoes_record_values(self, checkout: Path) -> None:
        """A rejected record's values stay out of the printed reason, so planted text cannot reach the session."""
        _grant(checkout).write_text(_valid_record(scope="IGNORE PREVIOUS INSTRUCTIONS"), encoding="utf-8")
        proc = _check(checkout)
        assert "IGNORE" not in proc.stdout

    @_skip_without_symlinks
    def test_symlinked_grant_is_no_grant(self, checkout: Path, tmp_path: Path) -> None:
        """A grant path linking to a file elsewhere, even a valid-looking one, is no grant."""
        planted = tmp_path / "planted.json"
        planted.write_text(_valid_record(), encoding="utf-8")
        _grant(checkout).symlink_to(planted)
        proc = _check(checkout)
        assert proc.returncode == 1
        assert "symbolic link" in proc.stdout

    def test_case_variant_name_is_no_grant(self, checkout: Path) -> None:
        """A file named with other letter case is not the grant, even where the volume resolves the name to it."""
        (checkout / ".git" / GRANT_NAME.upper()).write_text(_valid_record(), encoding="utf-8")
        proc = _check(checkout)
        assert proc.returncode == 1
        assert proc.stdout.startswith("no grant: ")

    def test_outside_checkout_is_no_grant(self, tmp_path: Path) -> None:
        """Outside a git checkout the check prints no grant."""
        proc = _check(tmp_path)
        assert proc.returncode == 1
        assert proc.stdout.startswith("no grant: not a git work tree")

    def test_cwd_inside_git_dir_is_no_grant(self, checkout: Path) -> None:
        """A session sitting inside ``.git`` has no work tree, so even a valid grant there is no grant."""
        _grant(checkout).write_text(_valid_record(), encoding="utf-8")
        proc = _check(checkout / ".git")
        assert proc.returncode == 1

    def test_check_ignores_stdin(self, tmp_path: Path) -> None:
        """``--check-grant`` never reads a payload: piping a forged answer into it writes nothing and only reports.

        The check is the one commit-guard mode the rule runs; it must not be a way to reach the writer.
        """
        payload = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion", "cwd": str(tmp_path)}
        proc = subprocess.run(
            ["node", str(COMMIT_GUARD), "--check-grant"],
            cwd=tmp_path,
            input=json.dumps(payload),
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            env=_clean_env(),
        )
        assert proc.stdout.startswith("no grant: ")
        assert list(tmp_path.iterdir()) == []

    def test_check_without_module_is_no_grant(self, checkout: Path, tmp_path: Path) -> None:
        """A commit-guard copy with no ``lib/approval-grants.js`` beside it reports no grant instead of crashing."""
        bare = tmp_path / "bare-hooks" / "commit-guard.js"
        bare.parent.mkdir()
        bare.write_bytes(COMMIT_GUARD.read_bytes())
        _grant(checkout).write_text(_valid_record(), encoding="utf-8")
        proc = subprocess.run(
            ["node", str(bare), "--check-grant"],
            cwd=checkout,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            env=_clean_env(),
        )
        assert (proc.returncode, proc.stdout) == (
            1,
            "no grant: commit-guard cannot load hooks/lib/approval-grants.js\n",
        )


@_skip_node_or_git_unavailable
class TestAmbientDiscovery:
    """Repository content and the environment cannot steer the check to a grant the user never gave (H2)."""

    @pytest.fixture(name="evil")
    def _evil(self, tmp_path: Path) -> Path:
        """Create another checkout holding a valid-looking grant."""
        evil = _new_checkout(tmp_path / "evil")
        _grant(evil).write_text(_valid_record(), encoding="utf-8")
        return evil

    @pytest.fixture(name="bare_fixture")
    def _bare_fixture(self, checkout: Path) -> Path:
        """Commit a bare-repository-shaped fixture holding a grant, as a contributed test tree might."""
        fixture = checkout / "tests" / "fixtures" / "repo"
        for sub in ("objects", "refs"):
            (fixture / sub).mkdir(parents=True)
            (fixture / sub / "keep").write_text("", encoding="utf-8")
        (fixture / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (fixture / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = true\n", encoding="utf-8")
        (fixture / GRANT_NAME).write_text(_valid_record(), encoding="utf-8")
        _git("add", "tests", cwd=checkout)
        _git("commit", "-m", "fixture", cwd=checkout)
        return fixture

    @pytest.mark.parametrize(
        ("where", "env"),
        [
            pytest.param("fixture", {}, id="fixture-root"),
            pytest.param("fixture-refs", {}, id="fixture-refs"),
            pytest.param("fixture", {"GIT_CONFIG_PARAMETERS": "'safe.bareRepository'='all'"}, id="config-env-all"),
        ],
    )
    def test_committed_bare_fixture_is_no_grant(self, bare_fixture: Path, where: str, env: dict) -> None:
        """A tracked bare-repository-shaped directory holding a grant is no grant.

        Git's discovery treats such a directory as an implicit bare repository unless ``safe.bareRepository`` is
        ``explicit``; the check passes that on its command line, which beats ``all`` injected through the environment.
        """
        cwd = bare_fixture / "refs" if where == "fixture-refs" else bare_fixture
        proc = _check(cwd, **env)
        assert proc.returncode == 1, proc.stdout

    @pytest.mark.parametrize(
        "project", [pytest.param(False, id="no-project-dir"), pytest.param(True, id="project-dir")]
    )
    def test_planted_gitfile_is_no_grant(self, checkout: Path, evil: Path, project: bool) -> None:
        """An untracked ``.git`` file pointing at another repository's git dir is no grant.

        Holds with or without the ``CLAUDE_PROJECT_DIR`` binding: that repository's git dir does not link back to this
        work tree, so the location check alone refuses it.
        """
        vendor = checkout / "vendor"
        vendor.mkdir()
        (vendor / ".git").write_text(f"gitdir: {evil / '.git'}\n", encoding="utf-8")
        proc = _check(vendor, **({"CLAUDE_PROJECT_DIR": str(checkout)} if project else {}))
        assert proc.returncode == 1, proc.stdout

    @pytest.mark.parametrize("variable", [pytest.param("GIT_DIR"), pytest.param("GIT_COMMON_DIR")])
    def test_steered_environment_is_no_grant(self, checkout: Path, evil: Path, variable: str) -> None:
        """``GIT_DIR``/``GIT_COMMON_DIR`` naming another repository's git dir is removed before git resolves."""
        proc = _check(checkout, **{variable: str(evil / ".git")})
        assert proc.returncode == 1, proc.stdout

    def test_other_repository_than_the_project_is_no_grant(self, checkout: Path, evil: Path) -> None:
        """A ``cd`` into another checkout holding a grant finds none: the common dir must be the session project's."""
        proc = _check(evil, CLAUDE_PROJECT_DIR=str(checkout))
        assert proc.returncode == 1
        assert "not the session project's" in proc.stdout

    def test_project_dir_outside_any_checkout_is_no_grant(self, checkout: Path, tmp_path: Path) -> None:
        """A session started outside a git checkout has no project common dir, so no checkout supplies a grant."""
        _grant(checkout).write_text(_valid_record(), encoding="utf-8")
        plain = tmp_path / "plain"
        plain.mkdir()
        proc = _check(checkout, CLAUDE_PROJECT_DIR=str(plain))
        assert proc.returncode == 1

    def test_separate_git_dir_is_no_grant(self, tmp_path: Path) -> None:
        """A checkout whose common dir is not named ``.git`` (``--separate-git-dir``) has no grant: fail closed."""
        work = tmp_path / "work"
        work.mkdir()
        _git("init", "-b", "main", "--separate-git-dir", str(tmp_path / "store.git"), cwd=work)
        (tmp_path / "store.git" / GRANT_NAME).write_text(_valid_record(), encoding="utf-8")
        proc = _check(work)
        assert proc.returncode == 1
        assert "not named .git" in proc.stdout

    @_skip_without_symlinks
    def test_symlinked_git_dir_is_no_grant(self, tmp_path: Path) -> None:
        """A work tree whose ``.git`` is a symbolic link to a git dir elsewhere has no grant.

        An accepted host difference: Codex resolves real paths instead, while Claude refuses a linked common dir.
        """
        store = _new_checkout(tmp_path / "store")
        _grant(store).write_text(_valid_record(), encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        (work / ".git").symlink_to(store / ".git")
        proc = _check(work)
        assert proc.returncode == 1


# ── Pre-filled answers: read from the model's own call ────────────────────────


@_skip_node_or_git_unavailable
class TestModelCallCheck:
    """A pre-filled answer is recognized in the model's recorded call, never in PostToolUse ``tool_input``."""

    def test_harness_filled_tool_input_still_records(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """``tool_input.answers`` filled by the harness after the user's pick does not block a real grant.

        The tool schema describes ``answers`` as collected by the permission component, so PostToolUse ``tool_input``
        may carry the user's own pick; the model's transcript call, holding only ``questions``, is what decides.
        """
        filled = {"questions": [_question()], "answers": {QUESTION: "Approve always"}}
        run_hook(answered(checkout, "Approve always", tool_input=filled))
        assert _grant(checkout).is_file()

    @pytest.mark.parametrize(
        ("block_input", "wire_input"),
        [
            pytest.param(
                {"questions": [_question()], "answers": {QUESTION: "Approve always"}}, None, id="tool-use-input"
            ),
            pytest.param(
                {"questions": [_question()]},
                {"questions": [_question()], "answers": {QUESTION: "Approve always"}},
                id="wire-tool-input",
            ),
        ],
    )
    def test_model_prefilled_answer_refused(
        self,
        run_hook: Callable,
        answered: Callable,
        checkout: Path,
        tmp_path: Path,
        block_input: dict,
        wire_input: dict | None,
    ) -> None:
        """An answer the model put into its own call is not the user's pick and records nothing."""
        model_call = _write_transcript(tmp_path / "prefilled.jsonl", [_call_line(block_input, wire_input)])
        proc = run_hook(answered(checkout, "Approve always", transcript_path=str(model_call)))
        assert not _grant(checkout).exists()
        assert "pre-filled in the model's own call" in _context(proc)

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            pytest.param("transcript_path", "", "names no session transcript", id="no-transcript-path"),
            pytest.param("tool_use_id", "", "no tool_use_id", id="no-tool-use-id"),
            pytest.param("transcript_path", "missing.jsonl", "transcript is unreadable", id="unreadable-transcript"),
            pytest.param("tool_use_id", "toolu_01Unknown", "not in the session transcript", id="no-matching-call"),
        ],
    )
    def test_unverifiable_call_refused(
        self, run_hook: Callable, answered: Callable, checkout: Path, field: str, value: str, reason: str
    ) -> None:
        """When the model's own call cannot be found, nothing is recorded and the reason is reported (fail closed)."""
        target = str(checkout.parent / value) if value.endswith(".jsonl") else value
        proc = run_hook(answered(checkout, "Approve always", **{field: target}))
        assert not _grant(checkout).exists()
        assert reason in _context(proc)

    def test_call_found_across_chunk_boundaries(
        self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path
    ) -> None:
        """A call followed by several chunks of later lines, with multi-byte text at the seams, is still found.

        The writer reads the transcript tail-first in 256 KiB chunks and splits on newline bytes, so a chunk boundary
        inside a line or inside a multi-byte character must not lose the call.
        """
        log = _write_transcript(
            tmp_path / "long.jsonl", [_call_line({"questions": [_question()]}), *_progress_lines(1500)]
        )
        run_hook(answered(checkout, "Approve always", transcript_path=str(log)))
        assert _grant(checkout).is_file()

    def test_scan_is_bounded(self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path) -> None:
        """A call older than the scanned tail window counts as not found, keeping a long transcript cheap to check."""
        filler = json.dumps({"type": "progress", "text": "x" * (1024 * 1024)})
        lines = [_call_line({"questions": [_question()]})] + [filler] * (TRANSCRIPT_MAX_BYTES // len(filler) + 2)
        log = _write_transcript(tmp_path / "huge.jsonl", lines)
        proc = run_hook(answered(checkout, "Approve always", transcript_path=str(log)))
        assert not _grant(checkout).exists()
        assert "not in the session transcript" in _context(proc)


# ── Self-grant guard ──────────────────────────────────────────────────────────


@_skip_node_or_git_unavailable
class TestSelfGrantGuard:
    """Agents cannot create or forge a record by naming it; they can still read and revoke it."""

    @pytest.mark.parametrize(
        ("tool", "tool_input"),
        [
            pytest.param("Write", {"file_path": f"/repo/.git/{GRANT_NAME}"}, id="write"),
            pytest.param("Edit", {"file_path": f"notes/{GRANT_NAME}"}, id="edit"),
            pytest.param("MultiEdit", {"file_path": f"notes/{GRANT_NAME}"}, id="multiedit"),
            pytest.param("NotebookEdit", {"notebook_path": f"notes/{GRANT_NAME}"}, id="notebookedit"),
            pytest.param("Write", {"file_path": "notes/Claude-Git-Approval.JSON"}, id="case-variant"),
            pytest.param("Write", {"file_path": f"C:\\repo\\notes\\{GRANT_NAME}"}, id="windows-separators"),
            pytest.param("Write", {"file_path": f"/tmp/staging/{GRANT_NAME}"}, id="other-directory"),
            pytest.param("Write", {"file_path": "notes/codex-git-approval.json"}, id="codex-grant"),
            pytest.param("Write", {"file_path": ".claude/local/git-approval.json"}, id="former-grant-name"),
            pytest.param("Write", {"file_path": "notes/claude-push-approval.json"}, id="push-token"),
            pytest.param("Write", {"file_path": "notes/claude-gh-write-approval.json"}, id="gh-write-token"),
            pytest.param("Write", {"file_path": f"notes/claude-git-approval.j{LONG_S}on"}, id="long-s-case-fold"),
            pytest.param("Write", {"file_path": f"notes/claude-{FULLWIDTH_G}it-approval.json"}, id="fullwidth-letter"),
            pytest.param("Write", {"file_path": f"notes/{GRANT_NAME}."}, id="win32-trailing-dot"),
            pytest.param("Write", {"file_path": f"notes/{GRANT_NAME} "}, id="win32-trailing-space"),
            pytest.param("Write", {"file_path": f"C:\\repo\\notes\\{GRANT_NAME}::$DATA"}, id="win32-data-stream"),
            pytest.param("Write", {"file_path": "C:\\repo\\notes\\CLAUDE~1.JSO"}, id="win32-short-name"),
            pytest.param("Write", {"file_path": "vendor/.git"}, id="planted-gitfile"),
            pytest.param("Write", {"file_path": "vendor/.GIT"}, id="planted-gitfile-case-variant"),
            pytest.param("Write", {"file_path": "vendor/.git/."}, id="planted-gitfile-dot-segment"),
            pytest.param("Write", {"file_path": "C:\\repo\\vendor\\GIT~1"}, id="planted-gitfile-short-name"),
            pytest.param(
                "Write", {"file_path": ".git/claude-gh-write-approval.json.spent-0123"}, id="spent-claim-name"
            ),
            pytest.param("Edit", {"file_path": f".git/{GRANT_NAME}.spent-{'0' * 32}"}, id="spent-claim-edit"),
            pytest.param("Write", {"file_path": ".git/.claude-push-approval.json.123.tmp"}, id="writer-temp-name"),
            pytest.param(
                "Write", {"file_path": f"staging/.{GRANT_NAME}.4242.a1b2c3d4e5f6.tmp"}, id="writer-temp-name-elsewhere"
            ),
        ],
    )
    def test_file_tool_write_blocked(self, run_hook: Callable, tool: str, tool_input: dict) -> None:
        """A file-tool write to a record name, its spent claim or writer temp name, or a path named ``.git`` exits 2.

        Matching a record name in any directory also covers a staging copy later moved into place and the Codex grant
        name; a claim (``<record>.spent-<id>``) and the writer's temp file (``.<record>.<pid>.<hex>.tmp``) are token
        state only the hooks create, blocked here as they are for Bash; a planted gitfile would steer git's discovery to
        another repository.
        """
        proc = run_hook(_pre(tool, tool_input))
        assert proc.returncode == 2
        assert "git-approve" in proc.stderr

    @pytest.mark.parametrize(
        "file_path",
        [
            pytest.param("plugins/cc_foundry/hooks/approval-guard.js", id="writer-source"),
            pytest.param("plugins/cc_foundry/hooks/lib/approval-grants.js", id="module-source"),
            pytest.param(f"docs/{GRANT_NAME}.md", id="name-prefix"),
            pytest.param(".github/workflows/ci.yml", id="github-dir"),
            pytest.param(".gitignore", id="gitignore"),
            pytest.param("repo.git/notes.txt", id="dot-git-suffix-dir"),
            pytest.param(".git/../README.md", id="parent-of-git-dir"),
            pytest.param(".git/info/exclude", id="inside-git-dir"),
            pytest.param(".git/COMMIT_EDITMSG", id="commit-message-file"),
            pytest.param(".git/worktrees/x/gitdir", id="worktree-link"),
            pytest.param(".git//hooks/post-commit", id="double-slash"),
            pytest.param("repo/.GIT/HEAD", id="git-dir-case-variant"),
            pytest.param("C:\\repo\\GIT~1\\HEAD", id="git-dir-short-name"),
            pytest.param(f"docs/{GRANT_NAME}.tmp.md", id="temp-like-name-prefix"),
        ],
    )
    def test_file_tool_other_paths_pass(self, run_hook: Callable, file_path: str) -> None:
        """Writes to other files pass silently: the writer's source, and any path inside ``.git`` but a record.

        A path inside the git dir is no planted gitfile and no record name; git's own files there are prompt discipline.
        A name merely starting with a record name (``….json.md``, ``….json.tmp.md``) is neither a claim nor a temp file.
        """
        proc = run_hook(_pre("Write", {"file_path": file_path}))
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    @pytest.mark.parametrize(
        ("tool", "target", "blocked"),
        [
            pytest.param("Write", "/h/.claude/projects/p/s.jsonl", True, id="write-transcript"),
            pytest.param("Edit", "/cfg/proj/agent-x.jsonl", True, id="edit-own-transcript-dir"),
            pytest.param("Bash", "echo '{}' >> ~/.claude/projects/p/s.jsonl", True, id="bash-append"),
            pytest.param("Bash", "cp fake.jsonl /cfg/proj/s.jsonl", True, id="bash-copy-into-own-dir"),
            pytest.param("Bash", "tee ~/.claude/projects/p/*.jsonl < x", True, id="bash-glob"),
            pytest.param("Write", "/h/.claude/projects/p/memory/note.md", False, id="memory-file-passes"),
            pytest.param("Bash", "grep -c tool_use ~/.claude/projects/p/s.jsonl", False, id="bash-read-passes"),
            pytest.param("Bash", "tail -5 /cfg/proj/s.jsonl", False, id="bash-tail-passes"),
            pytest.param("Bash", "sed -n 1,5p /cfg/proj/s.jsonl", False, id="bash-sed-read-passes"),
            pytest.param("Bash", "python3 -c 'print(1)' /cfg/proj/s.jsonl", False, id="bash-interpreter-read-passes"),
            pytest.param("Bash", "mkdir -p /cfg/proj/memory", False, id="bash-mkdir-memory-passes"),
            pytest.param("Bash", "cp note.md /cfg/proj/memory/", False, id="bash-copy-into-memory-passes"),
            pytest.param("Bash", "cp s.jsonl /cfg/proj/", True, id="bash-copy-into-transcript-dir"),
            pytest.param("Bash", "sed -i 's/Deny/Approve/' /cfg/proj/s.jsonl", True, id="bash-sed-in-place"),
            pytest.param("Bash", "sed --in-place 's/a/b/' /cfg/proj/s.jsonl", True, id="bash-sed-long-in-place"),
            pytest.param("Bash", "sort -o /cfg/proj/s.jsonl x", True, id="bash-sort-output"),
            pytest.param("Bash", "patch /cfg/proj/s.jsonl f.diff", True, id="bash-patch"),
            pytest.param("Bash", "cp /cfg/proj/s.jsonl /tmp/backup.jsonl", False, id="bash-copy-source-passes"),
            pytest.param("Bash", "rsync -a /cfg/proj/ backup/", False, id="bash-rsync-source-passes"),
        ],
    )
    def test_session_transcript_write_blocked(self, run_hook: Callable, tool: str, target: str, blocked: bool) -> None:
        """A write to a session transcript blocks; a read or a memory file passes.

        Token spends trust the transcript as the record of the user's answer, so writing a forged answer there would
        mint an approval. `/cfg/proj` is the payload's own transcript directory, which may sit outside `.claude/`.
        """
        tool_input = {"command": target} if tool == "Bash" else {"file_path": target}
        payload = {**_pre(tool, tool_input), "transcript_path": "/cfg/proj/s.jsonl"}
        proc = run_hook(payload)
        assert (proc.returncode == 2) is blocked, proc.stderr

    def test_file_tool_content_is_not_scanned(self, run_hook: Callable) -> None:
        """A Write whose content quotes the scope and file name passes: tests and docs here quote them on purpose."""
        content = '{"version": 1, "scope": "local-git-non-destructive"} -> .git/claude-git-approval.json'
        proc = run_hook(_pre("Write", {"file_path": "docs/grants.md", "content": content}))
        assert proc.returncode == 0

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f"echo '{{}}' > .git/{GRANT_NAME}", id="redirect"),
            pytest.param(f"echo '{{}}' >> .git/{GRANT_NAME}", id="append"),
            pytest.param(f"printf '{{}}' | tee .git/{GRANT_NAME}", id="tee"),
            pytest.param(f"cp /tmp/grant.json .git/{GRANT_NAME}", id="cp"),
            pytest.param(f"mv /tmp/grant.json .git/{GRANT_NAME}", id="mv"),
            pytest.param(f"touch .git/{GRANT_NAME}", id="touch"),
            pytest.param(f"ln -s /tmp/grant.json .git/{GRANT_NAME}", id="symlink"),
            pytest.param(f"sed -i '' 's/x/y/' .git/{GRANT_NAME}", id="sed-in-place"),
            pytest.param(f"python3 -c \"open('.git/{GRANT_NAME}', 'w').write('{{}}')\"", id="interpreter-one-liner"),
            pytest.param(f"cd .git && cat > {GRANT_NAME} <<'EOF'\n{{}}\nEOF", id="heredoc-after-cd"),
            pytest.param(f"F=.git/{GRANT_NAME}; printf '{{}}' | tee \"$F\"", id="variable-indirection"),
            pytest.param(f'cp grant.json "$(git rev-parse --git-common-dir)/{GRANT_NAME}"', id="common-dir-subst"),
            pytest.param(f"printf '{{}}' > \"`git rev-parse --git-common-dir`/{GRANT_NAME}\"", id="backtick-subst"),
            pytest.param("cp grant.json .git/codex-git-approval.json", id="codex-grant"),
            pytest.param("cp token.json .git/claude-push-approval.json", id="push-token"),
            pytest.param("cp token.json .git/claude-gh-write-approval.json", id="gh-write-token"),
            pytest.param("printf '{}' > .git/claude-gh-write-approval.json", id="gh-write-token-redirect"),
            pytest.param("cp token.json .git/claude-gh-write-ap*", id="gh-write-token-glob"),
            pytest.param(f"python3 -c \"print('{{}}')\" > .git/{GRANT_NAME}", id="interpreter-output-redirect"),
            pytest.param(f"python3 -c \"print('.git/{GRANT_NAME}')\"", id="interpreter-naming-record"),
            pytest.param(f"sudo cp grant.json .git/{GRANT_NAME}", id="wrapped-copy"),
            pytest.param(f"git grep -O{GRANT_NAME} x", id="git-grep-pager"),
            pytest.param(f"git -c core.pager=x show .git/{GRANT_NAME}", id="git-global-option"),
            pytest.param(f"cp grant.json staging/{GRANT_NAME}", id="staging-copy-by-name"),
            pytest.param("node plugins/cc_foundry/hooks/approval-guard.js < payload.json", id="forge-via-node"),
            pytest.param("./plugins/cc_foundry/hooks/approval-guard.js < payload.json", id="forge-direct-exec"),
            pytest.param("node plugins/cc_foundry/hooks/approval-guard.js --check", id="writer-has-no-check-mode"),
            pytest.param(
                "node -e \"require('./plugins/cc_foundry/hooks/lib/approval-grants.js').writeRecord()\"",
                id="module-required",
            ),
            pytest.param(
                f"git log -1 --format='tformat:{{\"version\":1}}' --output=.git/{GRANT_NAME}",
                id="git-log-output",
            ),
            pytest.param(f"git diff --output=.git/{GRANT_NAME}", id="git-diff-output"),
            pytest.param(f"git show HEAD:grant.json --output .git/{GRANT_NAME}", id="git-show-output"),
            pytest.param(f"git ls-files --output=.git/{GRANT_NAME}", id="output-flag-on-read-verb"),
            pytest.param(f"less -o .git/{GRANT_NAME} grant.json", id="pager-log-file"),
            pytest.param("mv x.json .git/claude-git-approval.j''son", id="empty-quote-split"),
            pytest.param('mv x.json ".git/claude-git-approval.j"son', id="quote-split"),
            pytest.param("mv x.json .git/claude-git-approval\\.json", id="backslash-escape"),
            pytest.param("mv x.json $'.git/claude-git-approval\\x2ejson'", id="ansi-c-hex-escape"),
            pytest.param("cp x.json .git/claude-git-approval.j?on", id="glob-question-mark"),
            pytest.param("cp x.json .git/claude-git-ap*", id="glob-star-prefix"),
            pytest.param("cp x.json .git/claude-git-approval.{json,bak}", id="brace-expansion"),
            pytest.param("cp x.json .git/[[:alpha:]]laude-git-approval.json", id="glob-posix-class"),
            pytest.param("cp x.json .git/[!x]laude-git-approval.json", id="glob-negated-bracket"),
            pytest.param("cp x.json .gi?/claude-git-ap*", id="glob-in-git-dir-glob"),
            pytest.param("cp x.json .git/*", id="wildcard-in-grant-directory"),
            pytest.param(f"cp x.json .git/claude-git-approval.j{LONG_S}on", id="long-s-case-fold"),
            pytest.param("cp x.json .git/CLAUDE~1.JSO", id="win32-short-name"),
            pytest.param("cp -R grantdir/. .git/", id="directory-copy-into-grant-dir"),
            pytest.param("cp -R grantdir/. .git//", id="directory-copy-double-slash"),
            pytest.param("cp -R grantdir/. .git/./", id="directory-copy-dot-segment"),
            pytest.param("rsync -a grantdir/ .git", id="directory-sync-into-grant-dir"),
            pytest.param('cp grantdir/* "$(git rev-parse --git-common-dir)"', id="glob-copy-into-common-dir"),
            pytest.param(
                'cp -R grantdir/. "$(git rev-parse --path-format=absolute --git-common-dir)"',
                id="copy-into-absolute-common-dir",
            ),
            pytest.param(f'cp grant.json "$(git rev-parse --git-path {GRANT_NAME})"', id="git-path-query"),
            pytest.param(
                "node ~/.claude/plugins/cache/borda-ai-rig/foundry/*/hooks/approval-gu*.js < payload.json",
                id="writer-glob",
            ),
            pytest.param("node plugins/cc_foundry/hooks/*.js < payload.json", id="writer-directory-wildcard"),
            pytest.param("node plugins/cc_foundry/hooks/approval-guard.j's' < payload.json", id="writer-quote-split"),
            pytest.param(
                "GIT_EXTERNAL_DIFF=node git diff plugins/cc_foundry/hooks/approval-guard.js < payload.json",
                id="writer-run-by-git-diff-env",
            ),
            pytest.param(f"LESSOPEN='|cp grant.json %s' cat .git/{GRANT_NAME}", id="env-before-read"),
            pytest.param("find . -path ./.git -exec sh -c 'cp p \"$0\"' {} ';'", id="find-with-exec-keeps-operands"),
            pytest.param('echo "gitdir: /evil/.git" > vendor/.git', id="gitfile-planted-by-redirect"),
            pytest.param("echo \\\\; cp -R payload/. .git/", id="escaped-backslash-still-ends-command"),
            pytest.param(
                "node --check --require ./x.js plugins/cc_foundry/hooks/approval-guard.js", id="node-check-with-option"
            ),
            pytest.param(
                "node -C dev plugins/cc_foundry/hooks/approval-guard.js < payload.json", id="node-conditions-not-check"
            ),
            pytest.param(
                "cd .git && tee claude-gh-write-approva{l,}.json < ../t.json", id="brace-word-after-cd-to-git-dir"
            ),
            pytest.param("tee gd/claude-gh-write-approva{l,}.json < t.json", id="brace-word-in-linked-dir"),
            pytest.param("cp t.json staging/claude-git-approva{l,x}.json", id="brace-word-staging-copy"),
            pytest.param("cp t.json staging/{a,b}/claude-push-approval.js{on,}", id="brace-words-nested-groups"),
            pytest.param("cp t.json staging/claude-gh-write-approva{l..m}.json", id="brace-letter-sequence"),
            pytest.param("cp t.json staging/claude~{1..3}.jso", id="brace-numeric-sequence-short-name"),
            pytest.param("cp t.json stag*/claude-gh-write-approval.json", id="record-name-under-glob-dir"),
            pytest.param("ln -s .git gd", id="symlink-to-git-dir"),
            pytest.param("ln -s .gi? gd", id="symlink-to-git-dir-glob"),
            pytest.param("ln -s $PWD/.g*t gd", id="symlink-to-git-dir-star-glob"),
            pytest.param("ln -sf .gi{t,} gd", id="symlink-to-git-dir-brace"),
            pytest.param("cp -R payload/. .git*", id="copy-into-git-dir-prefix-glob"),
            pytest.param("cp -R payload/. [.]git", id="copy-into-git-dir-bracket-glob"),
            pytest.param("rsync -a payload/ .gi?/", id="sync-into-git-dir-glob"),
            pytest.param("cp -t .git payload/x", id="copy-target-directory-option"),
            pytest.param("cp -rt .git payload/x", id="copy-target-directory-in-cluster"),
            pytest.param("cp --target-directory=.git payload/x", id="copy-target-directory-assigned"),
            pytest.param("cp -t.git payload/x", id="copy-target-directory-glued"),
            pytest.param("cp --target .git payload/x", id="copy-target-directory-abbreviated"),
            pytest.param("cp -R payload/. .git -S .bak", id="copy-destination-before-trailing-option"),
            pytest.param("mv payload/x .git", id="move-into-git-dir"),
            pytest.param("rsync -a --backup-dir=.git payload/ out/", id="sync-backup-dir-git-dir"),
            pytest.param("rsync -a -T .git payload/ out/", id="sync-temp-dir-git-dir"),
            pytest.param("tar -xf payload.tar -C .git", id="extract-into-git-dir"),
            pytest.param("tar --directory=.git -xf payload.tar", id="extract-into-git-dir-long"),
            pytest.param("unzip payload.zip -d .git", id="unzip-into-git-dir"),
            pytest.param("unzip -o payload.zip -d.git", id="unzip-into-git-dir-glued"),
            pytest.param("tar xfC payload.tar .git", id="extract-old-style-bundle"),
            pytest.param("git clone ../payload .git", id="clone-into-git-dir"),
            pytest.param("git -c core.pager='cp -R x/. .' -C .git log", id="git-config-program-with-git-dir"),
            pytest.param("GIT_EXTERNAL_DIFF=x GIT_DIR=.git git diff", id="git-env-program-with-git-dir"),
            pytest.param("python3 tools/sync.py payload .git", id="script-handed-git-dir"),
            pytest.param("sudo cp -R payload/. .git/", id="wrapped-copy-into-git-dir"),
            pytest.param('echo "gitdir: /evil/.git" > vendor/.gi?', id="gitfile-planted-by-redirect-glob"),
        ],
    )
    def test_bash_write_blocked(self, run_hook: Callable, command: str) -> None:
        """A record named as a write target, the common dir as a write destination, and every writer run exit 2.

        The name is tested as the shell and the filesystem resolve it: quotes and escapes removed, Unicode folded the
        way a case-insensitive volume compares names, brace words expanded (a literal record name counts in any
        directory), and any glob in the common dir that could expand to it. The common dir counts as a whole when it is
        a copy, move, sync, extract or clone destination, any operand of ``ln`` (a link is an alias the name tests
        cannot follow), or handed to any other command that is not a read; a glob word that can match ``.git`` counts as
        naming it. A command the guard cannot tell from a writer (an interpreter, a wrapper, git behind ``-c`` or a
        program-running env assignment) counts as one.
        """
        proc = run_hook(_pre("Bash", {"command": command}))
        assert proc.returncode == 2, proc.stderr
        assert "Approve always" in proc.stderr

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param(f"cat .git/{GRANT_NAME}", id="cat"),
            pytest.param(f'jq . "$(git rev-parse --git-common-dir)/{GRANT_NAME}"', id="jq-common-dir"),
            pytest.param(f"test -f .git/{GRANT_NAME} && echo present", id="test"),
            pytest.param(f"[ -f .git/{GRANT_NAME} ] || echo absent", id="bracket-test"),
            pytest.param(f"ls -la .git/{GRANT_NAME} 2>/dev/null", id="ls-dev-null"),
            pytest.param(REVOKE, id="rm-revoke"),
            pytest.param(f"rm -f .git/{GRANT_NAME}", id="rm-relative"),
            pytest.param("node plugins/cc_foundry/hooks/commit-guard.js --check-grant", id="rule-check"),
            pytest.param(
                "node plugins/cc_foundry/hooks/commit-guard.js --check-grant </dev/null", id="rule-check-devnull"
            ),
            pytest.param("git add plugins/cc_foundry/hooks/approval-guard.js", id="stage-writer-source"),
            pytest.param(
                "pre-commit run --files plugins/cc_foundry/hooks/lib/approval-grants.js", id="lint-module-source"
            ),
            pytest.param("git commit -F .temp/commit-message.txt", id="commit-message-file"),
            pytest.param("git status --short", id="unrelated"),
            pytest.param("ls -la .git", id="list-git-dir"),
            pytest.param("cat .git/HEAD", id="read-git-file"),
            pytest.param("cd .git && ls", id="cd-git-dir"),
            pytest.param("rm -f .git/index.lock", id="rm-git-lock"),
            pytest.param(f"cat '.git/{GRANT_NAME}'", id="quoted-read"),
            pytest.param("cp tests/fixtures/*.json /tmp/out/", id="unrelated-json-glob"),
            pytest.param("find . -name '*.json' -newer setup.py", id="find-json-pattern"),
            pytest.param("eslint plugins/*/hooks/*.js", id="lint-hook-glob"),
            pytest.param("cp -R .github/workflows /tmp/out", id="github-dir"),
            pytest.param("ROOT=$(git rev-parse --show-toplevel)", id="toplevel-assignment"),
            pytest.param('cp build.tar "$(git rev-parse --show-toplevel)/"', id="toplevel-destination"),
            pytest.param("BASE=$(git rev-parse HEAD)", id="rev-parse-commit"),
            pytest.param("grep -rn local-git-non-destructive plugins/", id="grep-scope-literal"),
            pytest.param("patch -p0 < p.diff", id="patch-names-nothing-known-pass"),
            pytest.param("find . -path ./.git -prune -o -name '*.py' -print", id="find-prune-git"),
            pytest.param("find . -not -path './.git/*' -type f", id="find-not-path-git"),
            pytest.param("tar --exclude=.git -czf out.tgz .", id="tar-exclude-git"),
            pytest.param(".venv/bin/python -m pytest --ignore=.git tests", id="pytest-ignore-git"),
            pytest.param("fd --exclude .git py", id="fd-exclude-git"),
            pytest.param("tree -I .git", id="tree-ignore-git"),
            pytest.param("zip -r out.zip . -x '.git/*'", id="zip-exclude-git"),
            pytest.param("du -sh .git", id="du-git-dir"),
            pytest.param(
                '_GITDIR=$(git rev-parse --git-common-dir 2>/dev/null || echo ".git")', id="common-dir-fallback-echo"
            ),
            pytest.param("node --check .claude/hooks/*.js 2>&1 | grep -v '^$' || true", id="node-syntax-check-glob"),
            pytest.param(
                'case "$IDS" in *[!0-9\\ ]*) echo "! BLOCKED — bad id"; exit 1 ;; esac', id="unclosed-bracket-word"
            ),
            pytest.param("jq -r '.items[] | .name' out.json", id="jq-array-iterator"),
            pytest.param("mv git-* out/", id="git-prefix-glob-misses-former-name"),
            pytest.param(
                'DOC=$(find "$ROOT" -maxdepth 3 \\( \\\n  -iname "MIGRATION*" -o -iname "CHANGELOG*" \\\n'
                '\\) -not -path "*/node_modules/*" -not -path "*/.git/*" \\\n| head -1)',
                id="find-escaped-parens-and-continuations",
            ),
            pytest.param(f'git commit -m "docs: describe .git/{GRANT_NAME}"', id="commit-message-naming-grant"),
            pytest.param("git log --grep=claude-push-approval.json", id="git-log-grep"),
            pytest.param(f"grep -rn {GRANT_NAME} plugins/ > hits.txt", id="grep-to-another-file"),
            pytest.param(f"cat .git/{GRANT_NAME} > /tmp/backup.json", id="read-to-another-file"),
            pytest.param("echo claude-gh-write-approval.json", id="echo-record-name"),
            pytest.param(
                "printf '%s' '{\"scope\": \"local-git-non-destructive\"}' > notes.json", id="scope-literal-local-git"
            ),
            pytest.param("echo push-once > token.json", id="scope-literal-push"),
            pytest.param("python3 -c \"print('gh-write-once')\"", id="scope-literal-gh-write"),
            pytest.param("echo LOCAL-GIT-NON-DESTRUCTIVE | tee notes.json", id="scope-literal-case-variant"),
            pytest.param("mv code* x/", id="prefix-glob-outside-git-dir"),
            pytest.param("cp x.json staging/[!x]laude-git-approval.json", id="glob-outside-git-dir"),
            pytest.param("cp x.json staging/claude-git-ap*", id="name-glob-outside-git-dir"),
            pytest.param("git --git-dir=.git log --oneline -3", id="git-dir-option-read"),
            pytest.param("GIT_DIR=.git git status", id="git-dir-env-read"),
            pytest.param("GIT_DIR=.git GIT_WORK_TREE=. git diff --stat", id="git-location-env-read"),
            pytest.param("git -C .git rev-parse --is-inside-git-dir", id="git-change-dir-rev-parse"),
            pytest.param("git --no-pager --git-dir .git show --stat HEAD", id="git-global-flags-read"),
            pytest.param("git --git-dir=.git fetch origin", id="git-dir-option-fetch"),
            pytest.param("git clone --mirror .git ../mirror.git", id="clone-git-dir-as-source"),
            pytest.param("tar czf backup.tgz .git", id="archive-git-dir"),
            pytest.param("tar -czf - .git > backup.tgz", id="archive-git-dir-to-stdout"),
            pytest.param("cp -R .git /tmp/repo-git-backup", id="copy-git-dir-out"),
            pytest.param("cp -R .git /tmp/repo-git-backup 2>/dev/null", id="copy-git-dir-out-quiet"),
            pytest.param("rsync -a --delete .git/ /tmp/backup/", id="sync-git-dir-out"),
            pytest.param("cp -R .git* /tmp/out/", id="copy-git-prefix-glob-out"),
            pytest.param("du -sh .git > size.txt", id="read-git-dir-to-file"),
            pytest.param("ls -d .git* .gi?", id="list-git-dir-globs"),
            pytest.param("cat .git/claude-gh-write-approva{l,}.json", id="brace-word-read"),
            pytest.param("mkdir -p src/{a,b}/tests && cp x.json src/{a,b}/", id="brace-words-without-record"),
            pytest.param("for i in {1..100000}; do echo $i; done", id="large-numeric-sequence"),
            pytest.param("f=claude-gh-write; cp t.json .git/${f}-approval.json", id="variable-built-name-known-pass"),
        ],
    )
    def test_bash_read_and_revoke_pass(self, run_hook: Callable, command: str) -> None:
        """Reading, mentioning or deleting a record, the rule's check, exclusion idioms and handling the writer pass.

        A mention is no write target: a commit message, ``grep``, ``echo`` or a redirect onto another file names the
        record without writing it, scope ids never count, and a glob counts only in the common dir. Local git naming the
        common dir as its repository (``-C``, ``--git-dir``, ``GIT_DIR=``) and the common dir as a copy, sync, archive
        or clone source are reads of it. ``patch -p0 < p.diff`` and a name built from a variable are pinned as known
        passes: the guard sees names, never the patch's content or a run-time value, which the rules say plainly
        instead of promising a block.
        """
        proc = run_hook(_pre("Bash", {"command": command}))
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    def test_many_wildcard_glob_is_judged_promptly(self, run_hook: Callable) -> None:
        """A glob of dozens of wildcards in the git dir is blocked within the hook's time, not backtracked.

        Each wildcard compiles to a repeat; tested naively against the name, 40 of them backtrack for hours and stall
        every Bash call behind the hook.
        """
        proc = run_hook(_pre("Bash", {"command": "cp x.json .git/c" + "*" * 40 + "n"}))
        assert proc.returncode == 2

    def test_interpreter_mention_blocked_with_workaround(self, run_hook: Callable) -> None:
        """A record named inside interpreter code is blocked; the error names the ``-F`` / file workaround.

        The guard cannot tell the code's mention from a write, so it fails closed on the name and tells Claude to pass
        such text by file.
        """
        proc = run_hook(_pre("Bash", {"command": f"node -e \"console.log('.git/{GRANT_NAME}')\""}))
        assert proc.returncode == 2
        assert "git commit -F <file>" in proc.stderr

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}, id="unguarded-tool"),
            pytest.param(_pre("Bash", {}), id="no-command"),
            pytest.param(_pre("Write", {}), id="no-path"),
        ],
    )
    def test_nothing_to_inspect_passes(self, run_hook: Callable, payload: dict) -> None:
        """A payload with nothing the guard reads is never a block."""
        assert run_hook(payload).returncode == 0

    def test_malformed_stdin_passes(self) -> None:
        """Malformed stdin never blocks or crashes the session."""
        proc = subprocess.run(
            ["node", str(HOOK)], input="{not json", capture_output=True, encoding="utf-8", timeout=15, env=_clean_env()
        )
        assert (proc.returncode, proc.stdout) == (0, "")


@_skip_node_or_git_unavailable
class TestMissingModule:
    """Without ``lib/approval-grants.js`` the hook records nothing and blocks every call naming a record."""

    @pytest.fixture(name="bare_hook")
    def _bare_hook(self, tmp_path: Path) -> Path:
        """Copy the hook to a directory with no ``lib/`` beside it, as a broken install would leave it."""
        hook = tmp_path / "bare-hooks" / "approval-guard.js"
        hook.parent.mkdir()
        hook.write_bytes(HOOK.read_bytes())
        return hook

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param(_pre("Bash", {"command": f"cp x .git/{GRANT_NAME}"}), 2, id="record-name-blocked"),
            pytest.param(_pre("Write", {"file_path": "notes/claude-push-approval.json"}), 2, id="file-tool-blocked"),
            pytest.param(_pre("Bash", {"command": "cp x .git/claude-gh-write-approval.json"}), 2, id="gh-write-token"),
            pytest.param(_pre("Bash", {"command": "ls"}), 0, id="other-passes"),
        ],
    )
    def test_fails_closed_on_record_names(self, bare_hook: Path, payload: dict, expected: int) -> None:
        """The degraded hook exits 2 on a record name; a crash would exit 1, which Claude Code does not block on."""
        proc = _run_hook(payload, hook=bare_hook)
        assert proc.returncode == expected, proc.stderr

    def test_records_nothing(self, bare_hook: Path, answered: Callable, checkout: Path) -> None:
        """With no module the writer is silent and nothing is written."""
        proc = _run_hook(answered(checkout, "Approve always"), hook=bare_hook)
        assert (proc.returncode, proc.stdout) == (0, "")
        assert not _grant(checkout).exists()


# ── Push token and gh write token (writer) ────────────────────────────────────

PUSH_RECORD = "claude-push-approval.json"
GH_WRITE_RECORD = "claude-gh-write-approval.json"
GH_COMMAND = "gh pr create --title fix --body-file body.md"
#: Fields of a gh write token, in the order the writer serializes them.
GH_WRITE_FIELDS = [
    "version",
    "scope",
    "created_at",
    "question",
    "answer",
    "question_id",
    "command",
    "files_sha256",
    "expires_at",
]
#: Bytes of file content one gh write command may bind (FILE_DIGEST_MAX_BYTES in the module).
FILE_DIGEST_MAX_BYTES = 1024 * 1024 * 1024
PUSH_COMMAND = "git push origin main"
PUSH_OPTIONS = ["Approve", "Deny"]
#: Fields of a push token, in the order the writer serializes them.
PUSH_FIELDS = ["version", "scope", "created_at", "question", "answer", "question_id", "branch", "head", "expires_at"]


def _push_question(command: str = PUSH_COMMAND, text: str | None = None) -> dict:
    """Build the ``git-push`` question showing ``command`` in backticks, or with question ``text`` as given."""
    asked = text if text is not None else f"Push 1 commit to origin/main: `{command}`?"
    return _question(header="git-push", options=PUSH_OPTIONS, text=asked)


def _gh_write_question(text: str = f"Open the PR on o/r: `{GH_COMMAND}`?") -> dict:
    """Build the ``gh-write`` question with question ``text``, which names the command the user approves."""
    return _question(header="gh-write", options=PUSH_OPTIONS, text=text)


def _gh_write_token(checkout: Path) -> Path:
    """Return the gh write token path of a main checkout."""
    return checkout / ".git" / GH_WRITE_RECORD


def _sparse(path: Path) -> Path:
    """Create an empty file at ``path`` for a case to extend sparsely with ``os.truncate``, and return the path."""
    path.write_bytes(b"")
    return path


def _push_token(checkout: Path) -> Path:
    """Return the push token path of a main checkout."""
    return checkout / ".git" / PUSH_RECORD


def _rev(checkout: Path, ref: str = "HEAD") -> str:
    """Return the commit id ``ref`` names in ``checkout``, or an empty string when it names none."""
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=checkout,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        env=_clean_env(),
    )
    return proc.stdout.strip()


def _instant(stamp: str) -> datetime:
    """Parse a writer timestamp (``…Z``) into an aware datetime."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


@_skip_node_or_git_unavailable
class TestPushTokenRecording:
    """``git-push`` answered ``Approve`` writes a single-use token for one push of this branch at this HEAD."""

    def test_approve_writes_the_token(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """The token holds the current branch and HEAD and expires 15 minutes after it was made; no command.

        Branch and HEAD are exactly what commit-guard compares before it lets a push run, so each must be recorded; the
        command the question showed stays in ``question`` only.
        """
        proc = run_hook(answered(checkout, "Approve", _push_question()))
        token = json.loads(_push_token(checkout).read_text(encoding="utf-8"))
        assert list(token) == PUSH_FIELDS
        assert {k: token[k] for k in ("scope", "answer", "branch", "head")} == {
            "scope": "push-once",
            "answer": "Approve",
            "branch": "main",
            "head": _rev(checkout),
        }
        assert _instant(token["expires_at"]) - _instant(token["created_at"]) == timedelta(minutes=15)
        assert "One non-force push of branch main" in _context(proc)

    def test_question_without_a_command_still_records(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """The command is shown for the user's eyes only: a plain-prose push question records the same token."""
        run_hook(answered(checkout, "Approve", _push_question(text="Push the 2 commits to origin/main?")))
        token = json.loads(_push_token(checkout).read_text(encoding="utf-8"))
        assert (token["branch"], token["head"]) == ("main", _rev(checkout))

    def test_question_without_the_branch_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """The question must name the branch it pushes: an answer to a question showing no branch records nothing.

        The token binds the branch checked out when the user answered; a question that never showed it would let the
        user approve a push of a branch they did not see.
        """
        proc = run_hook(answered(checkout, "Approve", _push_question(text="Push the 2 commits to origin: `git push`?")))
        assert not _push_token(checkout).exists()
        assert "does not name branch main" in _context(proc)

    def test_deny_writes_nothing(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """``Deny`` skips the push: no token, and with none pending nothing to report."""
        proc = run_hook(answered(checkout, "Deny", _push_question()))
        assert not _push_token(checkout).exists()
        assert (proc.returncode, proc.stdout) == (0, "")

    def test_deny_removes_a_pending_token(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """A later ``Deny`` cancels an earlier approval not yet spent: the latest push answer decides.

        Without this, a token approved earlier for the same command would still allow the push the user just refused.
        """
        run_hook(answered(checkout, "Approve", _push_question()))
        proc = run_hook(answered(checkout, "Deny", _push_question()))
        assert not _push_token(checkout).exists()
        assert "removed" in _context(proc)

    def test_latest_approve_replaces_a_pending_token(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """One file per project: a newer approval replaces the pending token, so only the newest answer stands."""
        run_hook(answered(checkout, "Approve", _push_question("git push origin main")))
        _git("commit", "--allow-empty", "-m", "second", cwd=checkout)
        later = _push_question("git push -u origin main")
        run_hook(answered(checkout, "Approve", later))
        token = json.loads(_push_token(checkout).read_text(encoding="utf-8"))
        assert (token["question"], token["head"]) == (later["question"], _rev(checkout))

    def test_second_copy_of_one_answer_is_silent(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """A second writer copy on the same answer keeps the first token as written and prints nothing.

        Each copy reads its own clock, so ``created_at`` and ``expires_at`` differ between copies; they still hold the
        same answer, and only the copy that wrote reports it.
        """
        payload = answered(checkout, "Approve", _push_question())
        run_hook(payload)
        first = _push_token(checkout).read_text(encoding="utf-8")
        proc = run_hook(payload)
        assert _push_token(checkout).read_text(encoding="utf-8") == first
        assert (proc.returncode, proc.stdout) == (0, "")

    def test_every_shipped_copy_at_once_leaves_one_token(self, answered: Callable, checkout: Path) -> None:
        """The four plugin copies answering one approval concurrently leave one valid token and no temporary file."""
        payload = json.dumps(answered(checkout, "Approve", _push_question()))
        procs = [
            subprocess.Popen(
                ["node", str(PLUGINS_DIR / plugin / "hooks" / "approval-guard.js")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                env=_clean_env(),
            )
            for plugin in HOOK_PLUGINS
        ]
        outputs = [proc.communicate(payload, timeout=30)[0] for proc in procs]
        token = json.loads(_push_token(checkout).read_text(encoding="utf-8"))
        assert (token["scope"], token["head"]) == ("push-once", _rev(checkout))
        assert any("recorded at" in out for out in outputs)
        assert [p.name for p in _push_token(checkout).parent.iterdir() if PUSH_RECORD in p.name] == [PUSH_RECORD]

    @pytest.mark.parametrize("attempt", [pytest.param(n, id=f"race-{n}") for n in range(3)])
    def test_only_the_writing_copy_reports_it(self, answered: Callable, checkout: Path, attempt: int) -> None:
        """Of four plugin copies racing on one answer, exactly one prints the "recorded" line.

        Every copy used to pass the unchanged-record check before any had written, so the session read the same approval
        announced up to four times; the answer's own temp file now admits one writer.
        """
        payload = json.dumps(answered(checkout, "Approve", _push_question()))
        procs = [
            subprocess.Popen(
                ["node", str(PLUGINS_DIR / plugin / "hooks" / "approval-guard.js")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                env=_clean_env(),
            )
            for plugin in HOOK_PLUGINS
        ]
        outputs = [proc.communicate(payload, timeout=30)[0] for proc in procs]
        assert sum("recorded at" in out for out in outputs) == 1, outputs
        assert [p.name for p in _push_token(checkout).parent.iterdir() if PUSH_RECORD in p.name] == [PUSH_RECORD]

    def test_token_names_the_approving_call(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """``question_id`` is the answered call's ``tool_use_id``: the spend proves the approval through it."""
        payload = answered(checkout, "Approve", _push_question())
        run_hook(payload)
        assert json.loads(_push_token(checkout).read_text(encoding="utf-8"))["question_id"] == payload["tool_use_id"]

    def test_detached_head_records_nothing(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """A push is approved on a named branch; with HEAD detached there is no branch to bind it to."""
        _git("switch", "--detach", cwd=checkout)
        proc = run_hook(answered(checkout, "Approve", _push_question()))
        assert not _push_token(checkout).exists()
        assert "detached" in _context(proc)

    def test_spawned_agent_question_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """A push approval asked from a spawned agent is refused: tokens belong to the lead session."""
        proc = run_hook(answered(checkout, "Approve", _push_question(), agent_id="agent-7"))
        assert not _push_token(checkout).exists()
        assert "spawned agent" in _context(proc)

    def test_prefilled_approve_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path
    ) -> None:
        """An ``Approve`` the model wrote into its own call is not the user's answer and records no token."""
        asked = _push_question()
        transcript = _write_transcript(
            tmp_path / "prefilled.jsonl",
            [_call_line({"questions": [asked], "answers": {asked["question"]: "Approve"}}), _result_line()],
        )
        proc = run_hook(answered(checkout, "Approve", asked, transcript_path=str(transcript)))
        assert not _push_token(checkout).exists()
        assert "pre-filled" in _context(proc)


@_skip_node_or_git_unavailable
class TestGhWriteTokenRecording:
    """``gh-write`` answered ``Approve`` writes a single-use token for the one gh command the question names."""

    def test_approve_writes_the_token(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """The token holds the named command and expires 15 minutes after it was made.

        ``command`` is what gh-write-guard compares with the whole Bash command before it allows the write, so it must
        be recorded exactly as the question showed it.
        """
        proc = run_hook(answered(checkout, "Approve", _gh_write_question()))
        token = json.loads(_gh_write_token(checkout).read_text(encoding="utf-8"))
        assert list(token) == GH_WRITE_FIELDS
        assert (token["scope"], token["answer"], token["command"]) == ("gh-write-once", "Approve", GH_COMMAND)
        assert _instant(token["expires_at"]) - _instant(token["created_at"]) == timedelta(minutes=15)
        assert "may run once" in _context(proc)

    @pytest.mark.parametrize(
        ("text", "command"),
        [
            pytest.param(
                "Post it: `gh issue comment 3 --body-file c.md`?", "gh issue comment 3 --body-file c.md", id="one-span"
            ),
            pytest.param(
                "Run `` gh pr comment 1 --body 'see `x`' `` on `o/r`?",
                "gh pr comment 1 --body 'see `x`'",
                id="double-backtick-span-with-a-backtick",
            ),
            pytest.param(
                "On `o/r` branch `fix`, run `gh pr merge 7 --squash`, then report.",
                "gh pr merge 7 --squash",
                id="span-among-other-spans",
            ),
            pytest.param("Run `gh release create v1 ` now?", "gh release create v1", id="trailing-space-trimmed"),
            pytest.param(
                'Run `gh pr comment 1 --body \'a; b && c | d $X > e\' --title "x \\"y\\""`?',
                'gh pr comment 1 --body \'a; b && c | d $X > e\' --title "x \\"y\\""',
                id="quoted-operators-and-escapes",
            ),
            pytest.param(
                "Upload: `gh release upload v1 'dist/a.whl#Wheel'`?",
                "gh release upload v1 'dist/a.whl#Wheel'",
                id="quoted-asset-label",
            ),
            pytest.param(
                "File it: `gh api repos/{owner}/{repo}/issues -f title=x`?",
                "gh api repos/{owner}/{repo}/issues -f title=x",
                id="gh-placeholder-braces",
            ),
        ],
    )
    def test_span_shapes_record_the_command(
        self, run_hook: Callable, answered: Callable, checkout: Path, text: str, command: str
    ) -> None:
        """The one inline span whose content starts with ``gh `` is the command, read as CommonMark reads spans.

        A double-backtick span carries a command holding a backtick; spans naming no gh command (a repository, a branch)
        are not counted.
        """
        run_hook(answered(checkout, "Approve", _gh_write_question(text)))
        assert json.loads(_gh_write_token(checkout).read_text(encoding="utf-8"))["command"] == command

    @pytest.mark.parametrize(
        ("text", "reason"),
        [
            pytest.param("Open the PR on o/r with gh pr create?", "names no gh command", id="no-span"),
            pytest.param("Run `gh pr view 1` then `gh pr merge 1`?", "more than one gh command", id="two-spans"),
            pytest.param(
                "Run `gh pr merge 1` twice: `gh pr merge 1`?", "more than one gh command", id="same-span-twice"
            ),
            pytest.param("Run this?\n```\ngh pr merge 1\n```", "fenced block", id="backtick-fence"),
            pytest.param("Run this?\n~~~\ngh pr merge 1\n~~~", "fenced block", id="tilde-fence"),
            pytest.param("Run `gh pr create\n--title x`?", "spans lines", id="multi-line-span"),
            pytest.param("Run \\`gh pr merge 1`?", "names no gh command", id="escaped-backtick"),
        ],
    )
    def test_no_single_command_span_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, text: str, reason: str
    ) -> None:
        """Zero or two gh spans, a fenced block or a span across lines record nothing and say why.

        The user must see exactly the one command the token will allow; anything else would approve text the guard could
        never match, or more than was shown.
        """
        proc = run_hook(answered(checkout, "Approve", _gh_write_question(text)))
        assert not _gh_write_token(checkout).exists()
        assert reason in _context(proc)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("Run `gh repo delete o/r --yes;\rgh issue close 1`?", id="carriage-return-in-span"),
            pytest.param("Run `gh issue close 1 \x1b[8m--comment x\x1b[0m`?", id="ansi-escape-in-span"),
            pytest.param("Run `gh issue close 1\t--comment x`?", id="tab-in-span"),
            pytest.param("Run `gh repo delete \N{RIGHT-TO-LEFT OVERRIDE}o/r --yes`?", id="bidi-override-in-span"),
            pytest.param(
                "Run `gh repo delete \N{LEFT-TO-RIGHT ISOLATE}o/r\N{POP DIRECTIONAL ISOLATE} --yes`?",
                id="bidi-isolate-in-span",
            ),
            pytest.param("Run `gh issue close 1\N{ZERO WIDTH SPACE}`?", id="zero-width-space-in-span"),
            pytest.param("Run `\N{ZERO WIDTH NO-BREAK SPACE}gh issue close 1`?", id="bom-in-span"),
            pytest.param("Run `gh issue close 1\u0085`?", id="c1-next-line-in-span"),
            pytest.param("Run `gh issue close 1\N{SOFT HYPHEN}`?", id="soft-hyphen-in-span"),
            pytest.param("Run `gh issue close\N{NO-BREAK SPACE}1`?", id="no-break-space-in-span"),
            pytest.param("Run `gh issue close 1\x7f`?", id="delete-in-span"),
            pytest.param("Delete `gh repo delete o/r --yes`\rClose issue 1?", id="carriage-return-in-question"),
            pytest.param("Close: `gh issue close 1` \N{RIGHT-TO-LEFT OVERRIDE}?", id="bidi-override-in-question"),
        ],
    )
    def test_hidden_characters_record_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, text: str
    ) -> None:
        """A question or command holding a control, format or invisible character records nothing.

        A terminal may hide such characters or redraw text over them, so the command the user saw could differ from the
        text the token would run under a hook ``allow``.
        """
        proc = run_hook(answered(checkout, "Approve", _gh_write_question(text)))
        assert not _gh_write_token(checkout).exists()
        assert "invisible or control character" in _context(proc)

    @pytest.mark.parametrize(
        ("command", "reason"),
        [
            pytest.param("gh issue close 1 && gh repo delete o/r --yes", "shell operator", id="and-list"),
            pytest.param("gh issue close 1; echo done", "shell operator", id="sequence"),
            pytest.param("gh pr comment 1 --body x | tee y", "shell operator", id="pipe"),
            pytest.param("gh pr comment 1 --body x &", "shell operator", id="background"),
            pytest.param("gh pr comment 1 --body x > out.txt", "shell operator", id="redirect"),
            pytest.param("gh pr comment 1 --body-file - < notes.md", "shell operator", id="stdin-redirect"),
            pytest.param('gh pr comment 1 --body "$(cat notes.md)"', "variable or substitution", id="substitution"),
            pytest.param('gh pr comment 1 --body "$BODY"', "variable or substitution", id="variable"),
            pytest.param("gh pr comment 1 --body $'a\\nb'", "variable or substitution", id="ansi-c-quote"),
            pytest.param("gh release upload v1 dist/*.whl", "glob", id="glob"),
            pytest.param("gh release upload v1 dist/{a,b}.whl", "glob", id="brace"),
            pytest.param("gh pr comment 1 --body-file ~/c.md", "glob", id="tilde"),
            pytest.param("gh pr comment 1 --body x # note", "glob", id="comment"),
            pytest.param("gh pr comment 1 --body 'open", "unterminated quote", id="unterminated-quote"),
            pytest.param("gh pr comment 1 --body-file -", "standard input", id="stdin-body-file"),
            pytest.param("gh api repos/o/r/issues -F body=@-", "standard input", id="stdin-field"),
        ],
    )
    def test_command_the_text_does_not_fully_show_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, command: str, reason: str
    ) -> None:
        """The approved text must be one plain gh command whose every value is visible in it.

        A second command, a redirection, a value the shell fills in at run time, a glob or stdin would let the run
        differ from what the user read, so the writer records nothing and says why.
        """
        proc = run_hook(answered(checkout, "Approve", _gh_write_question(f"Run ``{command}``?")))
        assert not _gh_write_token(checkout).exists()
        assert reason in _context(proc)

    def test_file_too_large_to_bind_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """A command naming files over the digest cap (1 GiB) records nothing: binding them would outlast the hook.

        Hashing is bounded so the spending guard answers well inside its timeout; a timed-out PreToolUse hook does not
        block, and shipped ``gh api`` allow rules would then run the write. The file is sparse, so the case is instant.
        """
        os.truncate(_sparse(checkout / "body.md"), FILE_DIGEST_MAX_BYTES + 1)
        proc = run_hook(answered(checkout, "Approve", _gh_write_question()))
        assert not _gh_write_token(checkout).exists()
        assert "too large" in _context(proc)

    def test_file_over_the_cap_has_no_digest(self, checkout: Path) -> None:
        """The digest refuses before reading a byte of an oversized file, so the spend fails closed at once."""
        os.truncate(_sparse(checkout / "asset.zip"), FILE_DIGEST_MAX_BYTES + 1)
        digest = _node_lib(f"g.commandFileDigest('gh release upload v1 asset.zip', {json.dumps(str(checkout))})")
        assert "too large" in digest["reason"]

    def test_token_names_the_approving_call_and_files(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """``question_id`` is the answered call's id and ``files_sha256`` the digest of the files the command names.

        The spend proves the approval through ``question_id`` and re-computes the digest, so both must be the values the
        library derives for this answer in this checkout.
        """
        (checkout / "body.md").write_text("approved body\n", encoding="utf-8")
        payload = answered(checkout, "Approve", _gh_write_question())
        run_hook(payload)
        token = json.loads(_gh_write_token(checkout).read_text(encoding="utf-8"))
        digest = _node_lib(f"g.commandFileDigest({json.dumps(GH_COMMAND)}, {json.dumps(str(checkout))})")
        assert (token["question_id"], token["files_sha256"]) == (payload["tool_use_id"], digest["digest"])

    def test_deny_removes_a_pending_token(self, run_hook: Callable, answered: Callable, checkout: Path) -> None:
        """A later ``Deny`` cancels an earlier approval not yet spent: the latest gh write answer decides."""
        run_hook(answered(checkout, "Approve", _gh_write_question()))
        proc = run_hook(answered(checkout, "Deny", _gh_write_question()))
        assert not _gh_write_token(checkout).exists()
        assert "removed" in _context(proc)

    def test_latest_approve_replaces_a_pending_token(
        self, run_hook: Callable, answered: Callable, checkout: Path
    ) -> None:
        """One file per project: a newer approval of another command replaces the pending one."""
        run_hook(answered(checkout, "Approve", _gh_write_question()))
        run_hook(answered(checkout, "Approve", _gh_write_question("Close it: `gh issue close 9`?")))
        assert json.loads(_gh_write_token(checkout).read_text(encoding="utf-8"))["command"] == "gh issue close 9"

    @pytest.mark.parametrize(
        ("extra", "reason"),
        [
            pytest.param({"agent_id": "agent-7"}, "spawned agent", id="spawned-agent"),
            pytest.param({"transcript_path": ""}, "names no session transcript", id="no-transcript"),
        ],
    )
    def test_refused_answer_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, extra: dict, reason: str
    ) -> None:
        """A spawned agent's question or an answer whose call cannot be checked writes no token."""
        proc = run_hook(answered(checkout, "Approve", _gh_write_question(), **extra))
        assert not _gh_write_token(checkout).exists()
        assert reason in _context(proc)

    def test_prefilled_approve_records_nothing(
        self, run_hook: Callable, answered: Callable, checkout: Path, tmp_path: Path
    ) -> None:
        """An ``Approve`` the model wrote into its own call is not the user's answer and records no token."""
        asked = _gh_write_question()
        transcript = _write_transcript(
            tmp_path / "prefilled.jsonl",
            [_call_line({"questions": [asked], "answers": {asked["question"]: "Approve"}}), _result_line()],
        )
        proc = run_hook(answered(checkout, "Approve", asked, transcript_path=str(transcript)))
        assert not _gh_write_token(checkout).exists()
        assert "pre-filled" in _context(proc)

    def test_outside_a_checkout_records_nothing(self, run_hook: Callable, answered: Callable, tmp_path: Path) -> None:
        """Outside a git repository there is no token location: nothing is written and the refusal says so."""
        plain = tmp_path / "plain"
        plain.mkdir()
        proc = run_hook(answered(plain, "Approve", _gh_write_question()))
        assert "not recorded" in _context(proc)
        assert [p.name for p in plain.iterdir()] == []


# ── Push token real case (writer → commit-guard → git push to a local bare remote) ──


def _guard_push(checkout: Path, command: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run commit-guard on one Bash command the way the harness does, in session project ``checkout``.

    Each run is a new tool call with its own ``tool_use_id`` and the session transcript the ``answered`` fixture records
    answers in, as the harness sends both with every call.
    """
    here = cwd or checkout
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": f"toolu_{uuid.uuid4().hex[:24]}",
        "transcript_path": str(_session_transcript(checkout)),
    }
    return subprocess.run(
        ["node", str(COMMIT_GUARD)],
        input=json.dumps({**payload, "cwd": str(here)}),
        cwd=here,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        env=_clean_env(CLAUDE_PROJECT_DIR=str(checkout)),
    )


@pytest.fixture(name="remote")
def _remote(checkout: Path, tmp_path: Path) -> Path:
    """Create a local bare repository and add it to ``checkout`` as ``origin``; never a network remote."""
    bare = tmp_path / "remote.git"
    _git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    _git("remote", "add", "origin", str(bare), cwd=checkout)
    return bare


@pytest.fixture(name="approve")
def _approve(run_hook: Callable, answered: Callable, checkout: Path) -> Callable[[str], subprocess.CompletedProcess]:
    """Return a callable recording the user's ``Approve`` to the ``git-push`` question naming one command."""

    def _run(command: str) -> subprocess.CompletedProcess:
        """Run the writer as the harness does after the user picked ``Approve``."""
        return run_hook(answered(checkout, "Approve", _push_question(command)), CLAUDE_PROJECT_DIR=str(checkout))

    return _run


@_skip_node_or_git_unavailable
class TestPushTokenRealCase:
    """The whole path on real git: the user's answer, the guard, and an actual push to a local bare remote."""

    def test_one_approval_lands_exactly_one_push(self, approve: Callable, checkout: Path, remote: Path) -> None:
        """Approve → the guard allows and spends the token → the push lands → the same push again is blocked.

        One scenario in three steps: the token is consumed by the allowed call, so the remote receives the approved
        commit and a repeat of the command finds nothing to spend.
        """
        approve(PUSH_COMMAND)
        allowed = _guard_push(checkout, PUSH_COMMAND)
        _git("push", "origin", "main", cwd=checkout)
        repeated = _guard_push(checkout, PUSH_COMMAND)
        assert allowed.returncode == 0, allowed.stderr
        assert _rev(remote, "main") == _rev(checkout)
        assert (repeated.returncode, "git-push" in repeated.stderr) == (2, True)
        assert not _push_token(checkout).exists()

    def test_a_new_commit_needs_a_new_approval(self, approve: Callable, checkout: Path, remote: Path) -> None:
        """A commit made after the approval moves HEAD: the guard blocks and keeps the token for the approved HEAD."""
        approve(PUSH_COMMAND)
        _git("commit", "--allow-empty", "-m", "after approval", cwd=checkout)
        blocked = _guard_push(checkout, PUSH_COMMAND)
        assert blocked.returncode == 2
        assert "HEAD moved" in blocked.stderr
        assert _rev(remote, "main") == ""

    def test_branch_substitution_spelling_lands(self, approve: Callable, checkout: Path, remote: Path) -> None:
        """A push naming the branch through ``$(git branch --show-current)`` spends the token, and that push lands.

        The substitution prints the branch the push runs on, so it is the approved branch's push; a second push of any
        spelling is refused.
        """
        approve(PUSH_COMMAND)
        allowed = _guard_push(checkout, 'git push -u origin "$(git branch --show-current)" # timeout: 30000')
        _git("push", "-u", "origin", "main", cwd=checkout)
        second = _guard_push(checkout, PUSH_COMMAND)
        assert allowed.returncode == 0, allowed.stderr
        assert _rev(remote, "main") == _rev(checkout)
        assert second.returncode == 2

    def test_variable_spelling_is_never_covered(self, approve: Callable, checkout: Path, remote: Path) -> None:
        """Oss:resolve's fallback spelling, a remote and ref in variables, may name any target: it is left to the user.

        The user's decision binds the approval to the current branch and a configured remote name, so the guard blocks
        with the by-hand advice, keeps the token for a plain spelling, and nothing reaches the remote.
        """
        approve(PUSH_COMMAND)
        blocked = _guard_push(checkout, 'git push "$FORK_REMOTE" HEAD:"$HEAD_REF" # timeout: 30000')
        assert blocked.returncode == 2
        assert "by hand" in blocked.stderr
        assert _push_token(checkout).exists()
        assert _rev(remote, "main") == ""

    def test_delete_is_never_covered(self, approve: Callable, checkout: Path, remote: Path) -> None:
        """Even an approval shown as ``git push origin --delete feature`` never lets the delete through.

        By the user's decision a token covers the approved branch's push only: the delete blocks with the by-hand
        advice, the token stays, and the remote branch survives.
        """
        _git("push", "origin", "main:feature", cwd=checkout)
        approve("git push origin --delete feature")
        blocked = _guard_push(checkout, "git push origin --delete feature")
        assert blocked.returncode == 2
        assert "by hand" in blocked.stderr
        assert _push_token(checkout).exists()
        assert _rev(remote, "feature") == _rev(checkout)

    def test_force_push_blocked_with_its_own_approval(self, approve: Callable, checkout: Path, remote: Path) -> None:
        """Even an approval whose question showed a force command is never spent on it: the force ban runs first."""
        approve("git push origin +main")
        blocked = _guard_push(checkout, "git push origin +main")
        assert blocked.returncode == 2
        assert "force-push is forbidden" in blocked.stderr
        assert _rev(remote, "main") == ""


# ── gh write token real case (writer → gh-write-guard, fake gh first on PATH) ──

GH_WRITE_GUARD = HOOKS_DIR / "gh-write-guard.js"
#: What gh-write-guard prints when this call spent the user's approval.
SPENT_ALLOW = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "one-time gh-write approval spent",
    }
}


@pytest.fixture(name="fake_gh")
def _fake_gh(tmp_path: Path) -> dict[str, str]:
    """Return environment overrides putting a recording fake ``gh`` first on ``PATH``, with a dummy token and host.

    The hooks only classify a command, never run it; the fake's marker file proves no step of the real case ran gh.
    """
    bindir = tmp_path / "fake-bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(f"#!/bin/sh\necho ran >> '{tmp_path / 'gh-ran'}'\nexit 99\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    return {"PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}", "GH_TOKEN": "dummy", "GH_HOST": "x.invalid"}


def _gh_guard_payload(checkout: Path, command: str, call_id: str) -> str:
    """Return the PreToolUse(Bash) payload of one tool call, as the harness sends it, as JSON text."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_use_id": call_id,
        "transcript_path": str(_session_transcript(checkout)),
        "cwd": str(checkout),
    }
    return json.dumps(payload)


def _guard_gh(checkout: Path, command: str, call_id: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run gh-write-guard on one Bash call in session project ``checkout``."""
    return subprocess.run(
        ["node", str(GH_WRITE_GUARD)],
        input=_gh_guard_payload(checkout, command, call_id),
        cwd=checkout,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
        env=_clean_env(CLAUDE_PROJECT_DIR=str(checkout), **env),
    )


@pytest.fixture(name="approve_gh")
def _approve_gh(
    run_hook: Callable, answered: Callable, checkout: Path, fake_gh: dict[str, str]
) -> Callable[[str], subprocess.CompletedProcess]:
    """Return a callable recording the user's ``Approve`` to the ``gh-write`` question naming one command."""

    def _run(command: str) -> subprocess.CompletedProcess:
        """Run the writer as the harness does after the user picked ``Approve``."""
        question = _gh_write_question(f"Run on o/r: `{command}`?")
        return run_hook(answered(checkout, "Approve", question), CLAUDE_PROJECT_DIR=str(checkout), **fake_gh)

    return _run


@_skip_node_or_git_unavailable
class TestGhWriteTokenRealCase:
    """The whole path: the user's answer, the real writer, the real guard, and never a run of gh."""

    def test_one_approval_allows_exactly_one_call(
        self, approve_gh: Callable, checkout: Path, fake_gh: dict[str, str], tmp_path: Path
    ) -> None:
        """Approve → the approved command's call is allowed and spends the token → the same command again is blocked.

        The second call carries another ``tool_use_id``, as a repeat would: it finds no token and no claim of its own.
        """
        approve_gh(GH_COMMAND)
        allowed = _guard_gh(checkout, GH_COMMAND, "toolu_first", fake_gh)
        repeated = _guard_gh(checkout, GH_COMMAND, "toolu_second", fake_gh)
        assert (allowed.returncode, json.loads(allowed.stdout)) == (0, SPENT_ALLOW)
        assert (repeated.returncode, "`gh-write`" in repeated.stderr) == (2, True)
        assert not _gh_write_token(checkout).exists()
        assert not (tmp_path / "gh-ran").exists()

    def test_every_shipped_copy_allows_the_same_call(
        self, approve_gh: Callable, checkout: Path, fake_gh: dict[str, str]
    ) -> None:
        """The four plugin copies judging one call at once all allow it, and the next call is blocked.

        Hooks run in parallel and a deny from any copy would beat the others' allow: one copy renames the token to the
        call's claim, the others find that claim by the same ``tool_use_id``.
        """
        approve_gh(GH_COMMAND)
        procs = [
            subprocess.Popen(
                ["node", str(PLUGINS_DIR / plugin / "hooks" / "gh-write-guard.js")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=checkout,
                encoding="utf-8",
                env=_clean_env(CLAUDE_PROJECT_DIR=str(checkout), **fake_gh),
            )
            for plugin in HOOK_PLUGINS
        ]
        payload = _gh_guard_payload(checkout, GH_COMMAND, "toolu_shared")
        results = [(proc.communicate(payload, timeout=30), proc.returncode) for proc in procs]
        later = _guard_gh(checkout, GH_COMMAND, "toolu_later", fake_gh)
        assert [(code, json.loads(out)) for (out, _), code in results] == [(0, SPENT_ALLOW)] * len(HOOK_PLUGINS)
        assert later.returncode == 2

    def test_other_text_keeps_the_token(self, approve_gh: Callable, checkout: Path, fake_gh: dict[str, str]) -> None:
        """A write differing from the approved text is blocked and keeps the token for the approved command."""
        approve_gh(GH_COMMAND)
        other = _guard_gh(checkout, f"{GH_COMMAND} --draft", "toolu_other", fake_gh)
        exact = _guard_gh(checkout, f"  {GH_COMMAND}\n", "toolu_exact", fake_gh)
        assert (other.returncode, "approves another command" in other.stderr) == (2, True)
        assert exact.returncode == 0, exact.stderr

    def test_session_start_wipes_token_and_claims(
        self, approve_gh: Callable, checkout: Path, fake_gh: dict[str, str]
    ) -> None:
        """Commit-guard's SessionStart removes a pending token and the claims spent tokens left in the common dir."""
        approve_gh(GH_COMMAND)
        _guard_gh(checkout, GH_COMMAND, "toolu_spent", fake_gh)
        approve_gh("gh issue close 9")
        subprocess.run(
            ["node", str(COMMIT_GUARD)],
            input=json.dumps({"hook_event_name": "SessionStart", "cwd": str(checkout)}),
            cwd=checkout,
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            env=_clean_env(CLAUDE_PROJECT_DIR=str(checkout)),
        )
        assert [p.name for p in (checkout / ".git").iterdir() if GH_WRITE_RECORD in p.name] == []

    def test_body_file_rewritten_after_approve_is_blocked(
        self, approve_gh: Callable, checkout: Path, fake_gh: dict[str, str]
    ) -> None:
        """Approve → the body file is rewritten → the approved command is blocked and keeps its token.

        The user approved ``--body-file body.md`` holding one text; posting another under the hook ``allow`` would
        publish content the user never saw.
        """
        (checkout / "body.md").write_text("approved body\n", encoding="utf-8")
        approve_gh(GH_COMMAND)
        (checkout / "body.md").write_text("other text\n", encoding="utf-8")
        blocked = _guard_gh(checkout, GH_COMMAND, "toolu_body", fake_gh)
        assert (blocked.returncode, "changed after the user approved" in blocked.stderr) == (2, True), blocked.stderr
        assert _gh_write_token(checkout).exists()


# ── Single-use spend: concurrent copies, claims ───────────────────────────────

#: A node script spending the planted gh write token for one call, after making one filesystem call of the location
#: check throw ENOENT once — as it does when another copy of the same call renames the token between ``lstat`` and that
#: call. The throwing patch first performs that rename, so the call's claim exists. argv: fs name, checkout, command,
#: call id, transcript.
_SPEND_RACE_SCRIPT = """
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const [fsName, checkout, command, callId, transcript] = process.argv.slice(1);
const token = path.join(checkout, ".git", "claude-gh-write-approval.json");
const digest = crypto.createHash("sha256").update(callId).digest("hex").slice(0, 32);
const claim = `${token}.spent-${digest}`;
const original = fsName === "realpath" ? fs.realpathSync.native : fs.readdirSync;
let armed = true;
const raced = function (target, ...rest) {
  if (armed && path.resolve(String(target)) === path.resolve(fsName === "realpath" ? token : path.dirname(token))) {
    armed = false;
    fs.renameSync(token, claim);
    throw Object.assign(new Error(`ENOENT: no such file or directory, ${fsName} '${target}'`), { code: "ENOENT" });
  }
  return original.call(this, target, ...rest);
};
if (fsName === "realpath") fs.realpathSync.native = raced;
else fs.readdirSync = raced;
const g = require(LIB);
const files = g.commandFileDigest(command, checkout);
try {
  const spent = g.spendToken("gh-write-once", {
    cwd: checkout,
    env: { ...process.env, CLAUDE_PROJECT_DIR: checkout },
    callId,
    transcriptPath: transcript,
    expected: { command, files_sha256: files.digest },
  });
  process.stdout.write(JSON.stringify({ armed, spent }));
} catch (error) {
  process.stdout.write(JSON.stringify({ armed, threw: error.message }));
}
"""


def _plant_claim(checkout: Path, number: int, age: timedelta) -> Path:
    """Write a claim an earlier gh write spend left in ``checkout``'s common dir, last modified ``age`` ago."""
    claim = checkout / ".git" / f"{GH_WRITE_RECORD}.spent-{number:032x}"
    claim.write_text("{}\n", encoding="utf-8")
    stamp = (datetime.now(timezone.utc) - age).timestamp()
    os.utime(claim, (stamp, stamp))
    return claim


@_skip_node_or_git_unavailable
class TestPushProof:
    """The push spend proves its approval through the transcript, like the gh write spend, and needs the branch
    named."""

    def test_token_whose_question_names_no_branch_is_no_approval(
        self, push_token: Callable, checkout: Path, remote: Path
    ) -> None:
        """A token approved by a question that never showed its branch is refused at the spend and kept.

        The writer refuses such an answer; this is the record written by other means, with a real transcript answer.
        """
        token = push_token(checkout, question="Push the commits to origin: `git push`?")
        blocked = _guard_push(checkout, PUSH_COMMAND)
        assert (blocked.returncode, "does not name the token's branch" in blocked.stderr) == (2, True), blocked.stderr
        assert token.exists()

    def test_token_without_its_question_in_the_transcript_is_no_approval(
        self, push_token: Callable, checkout: Path, remote: Path
    ) -> None:
        """A push token naming a question the transcript lacks — a forged record — allows nothing and is kept."""
        token = push_token(checkout, question_id="toolu_forged000000000000000")
        blocked = _guard_push(checkout, PUSH_COMMAND)
        assert (blocked.returncode, "not in the session transcript" in blocked.stderr) == (2, True), blocked.stderr
        assert token.exists()


@_skip_node_or_git_unavailable
class TestSpendToken:
    """``spendToken`` never throws on a concurrent spend, and a spend prunes the claims older spends left."""

    @pytest.mark.parametrize("fs_name", [pytest.param("realpath"), pytest.param("readdir")])
    def test_token_vanishing_mid_check_falls_back_to_the_claim(
        self, gh_write_token: Callable, approval_transcript: Callable, checkout: Path, fs_name: str
    ) -> None:
        """A token renamed by another copy of the same call while this copy checks it is accepted through the claim.

        Before the fix the ENOENT escaped ``spendToken``: commit-guard had no catch (exit 1, the push went through
        without a token) and gh-write-guard's catch denied the very call the winning copy allowed.
        """
        gh_write_token(checkout, GH_COMMAND)
        script = _SPEND_RACE_SCRIPT.replace("LIB", json.dumps(str(LIB)))
        argv = [fs_name, str(checkout), GH_COMMAND, "toolu_same", str(approval_transcript(checkout))]
        proc = subprocess.run(
            ["node", "-e", script, *argv],
            capture_output=True,
            encoding="utf-8",
            timeout=15,
            env=_clean_env(),
        )
        assert json.loads(proc.stdout) == {"armed": False, "spent": {"ok": True}}, proc.stdout + proc.stderr

    def test_spend_prunes_claims_older_than_the_token_lifetime(
        self, gh_write_token: Callable, checkout: Path, fake_gh: dict[str, str]
    ) -> None:
        """A spend removes this scope's claims older than 15 minutes and keeps its own claim and recent ones.

        Without foundry's SessionStart wipe, every spent gh write used to leave one claim file in ``.git`` for good.
        """
        stale = _plant_claim(checkout, 1, timedelta(hours=1))
        recent = _plant_claim(checkout, 2, timedelta(minutes=1))
        gh_write_token(checkout, GH_COMMAND)
        spent = _guard_gh(checkout, GH_COMMAND, "toolu_prune", fake_gh)
        left = [p.name for p in (checkout / ".git").iterdir() if p.name.startswith(f"{GH_WRITE_RECORD}.spent-")]
        assert spent.returncode == 0, spent.stderr
        assert (stale.exists(), recent.exists(), len(left)) == (False, True, 2)


# ── Registration and propagation ──────────────────────────────────────────────


def _registered_matchers(plugin: str, event: str) -> list[str]:
    """Return the matchers under which one plugin's ``hooks.json`` registers approval-guard.js for one event."""
    config = json.loads((PLUGINS_DIR / plugin / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return [
        entry.get("matcher", "")
        for entry in config["hooks"].get(event, [])
        if any(hook["command"].endswith('/hooks/approval-guard.js"; done; exit 0') for hook in entry["hooks"])
    ]


@pytest.mark.parametrize("plugin", HOOK_PLUGINS)
class TestShippedInEveryHookPlugin:
    """Each hook plugin carries the writer, the guard and the module, so a standalone install records and guards."""

    @pytest.mark.parametrize(
        ("event", "matcher"),
        [
            pytest.param("PostToolUse", "AskUserQuestion", id="writer"),
            pytest.param("PreToolUse", "Bash|Write|Edit|MultiEdit|NotebookEdit", id="guard"),
        ],
    )
    def test_registered_for_both_halves(self, plugin: str, event: str, matcher: str) -> None:
        """The writer runs after AskUserQuestion and the guard before every writing tool, each registered once."""
        assert _registered_matchers(plugin, event) == [matcher]

    @_skip_node_or_git_unavailable
    def test_copy_blocks_a_record_write_from_its_own_tree(self, plugin: str) -> None:
        """The plugin's own copy loads its own module: a record write through it is blocked with the full message."""
        proc = _run_hook(
            _pre("Bash", {"command": f"cp x .git/{GRANT_NAME}"}),
            hook=PLUGINS_DIR / plugin / "hooks" / "approval-guard.js",
        )
        assert proc.returncode == 2
        assert "cannot load" not in proc.stderr
