"""Shared test configuration for foundry plugin tests.

Auto-loads all ``bin/`` Python scripts as importable modules so tests can ``from find_polluter import main`` directly.

JS hook helpers ``run_hook``, ``state_dir``, ``push_token``, ``gh_write_token``, ``approval_transcript`` and
``approval_answer`` are exposed as pytest fixtures so test methods receive them as parameters — no explicit imports
required.

Non-fixture host-capability helpers (``_hook_tmp_base``, ``_bash_runs_posix_script``) live in ``_hook_env.py``, not
here. Nothing may import this module by the bare name ``conftest``: ``ini_options.testpaths`` spans ``benchmarks`` and
``plugins``, every tree has its own ``conftest.py``, and under ``--import-mode=importlib`` the bare name resolves to
whichever one loaded first — ``benchmarks/conftest.py`` in a full run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_BIN_DIR = _TESTS_DIR.parent / "bin"
_HOOKS_DIR = _TESTS_DIR.parent / "hooks"
_APPROVAL_LIB = _HOOKS_DIR / "lib" / "approval-grants.js"
#: Option labels of the single-use token questions (``git-push``, ``gh-write``).
_TOKEN_OPTIONS = ("Approve", "Deny")


def _load_bin_modules() -> None:
    """Load Foundry bin scripts into ``sys.modules`` for direct test imports."""
    for script in sorted(_BIN_DIR.glob("*.py")):
        module_name = script.stem.replace("-", "_")
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, script)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]


_load_bin_modules()


@pytest.fixture(name="run_hook")
def _run_hook() -> Callable[..., subprocess.CompletedProcess]:
    """Return callable that spawns a foundry hook via ``node``.

    Strips ``OPENAI_API_KEY`` and ``ANTHROPIC_API_KEY`` so ``agent-router.js`` falls through to tier-3 fallback without
    live API calls.
    """

    def _run(
        hook: str,
        payload: dict,
        *,
        cwd: Path | None = None,
        home: Path | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run one hook with the test's isolated environment and JSON payload."""
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(_HOOKS_DIR.parent)}
        if home is not None:
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(k, None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["node", str(_HOOKS_DIR / hook)],
            input=json.dumps(payload),
            capture_output=True,
            # Explicit UTF-8, never bare text=True: that decodes with the parent's locale codec,
            # and cp1252 has no mapping for 0x8f — the VS-16 byte of the statusline's emoji
            # markers. The pipe reader thread dies mid-decode and stdout silently becomes None.
            encoding="utf-8",
            env=env,
            cwd=str(cwd) if cwd else None,
        )

    return _run


@pytest.fixture(name="state_dir")
def _state_dir() -> Callable[[str], Path]:
    """Return callable that maps a session id to its ``claude-state-<sid>`` path.

    Base comes from :func:`_hook_tmp_base`, so the path tracks the hook's own ``getSentinelDir()`` on every platform
    instead of assuming ``/tmp``.
    """
    from _hook_env import _hook_tmp_base  # local: _TESTS_DIR is on sys.path only after this module loads

    def _state_dir(sid: str) -> Path:
        """Return the hook state directory for one session id."""
        return _hook_tmp_base() / f"claude-state-{sid}"

    return _state_dir


def _iso(moment: datetime) -> str:
    """Return ``moment`` as the writer stamps it: ISO 8601 UTC with milliseconds and ``Z``.

    Examples:
        >>> _iso(datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc))
        '2026-10-10T08:00:00.000Z'
    """
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _git_line(repo: Path, *args: str) -> str:
    """Return the first stdout line of one git command in ``repo``, with no ``GIT_*`` variable steering it."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, encoding="utf-8", check=True, timeout=15, env=env
    )
    return proc.stdout.strip()


def _session_transcript(repo: Path) -> Path:
    """Return the session transcript beside ``repo`` that the token fixtures record the user's answers in.

    Examples:
        >>> _session_transcript(Path("/w/repo")).name
        'repo-session.jsonl'
    """
    return repo.parent / f"{repo.name}-session.jsonl"


def _answer_lines(
    header: str,
    question: object,
    answer: str,
    *,
    answered_at: datetime,
    prefilled: bool = False,
    is_error: bool = False,
) -> tuple[str, list[str]]:
    """Return a fresh call id and the two transcript lines of one answered ``AskUserQuestion`` call.

    Shaped as Claude Code writes them (grounded on a real session transcript): an assistant line whose
    ``message.content`` holds the ``tool_use`` block and whose ``wireToolInputs`` repeats its input, then a user line
    whose ``message.content`` holds the ``tool_result`` block and whose top-level ``toolUseResult`` carries
    ``{questions, answers, annotations}`` with ``answers`` keyed by question text. ``prefilled`` puts the answer into
    the model's own input; ``is_error`` marks the result as an error.
    """
    call_id = f"toolu_{uuid.uuid4().hex[:24]}"
    asked = {
        "question": question,
        "header": header,
        "options": [{"label": label, "description": f"{label} effect"} for label in _TOKEN_OPTIONS],
        "multiSelect": False,
    }
    model_input = {"questions": [asked], **({"answers": {str(question): answer}} if prefilled else {})}
    use_uuid = str(uuid.uuid4())
    call = {
        "type": "assistant",
        "uuid": use_uuid,
        "timestamp": _iso(answered_at),
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": call_id, "name": "AskUserQuestion", "input": model_input}],
        },
        "wireToolInputs": {call_id: model_input},
    }
    block = {"type": "tool_result", "tool_use_id": call_id, "content": f'The user answered: "{question}"="{answer}"'}
    result = {
        "type": "user",
        "uuid": str(uuid.uuid4()),
        "timestamp": _iso(answered_at),
        "sourceToolAssistantUUID": use_uuid,
        "message": {"role": "user", "content": [{**block, "is_error": True} if is_error else block]},
        "toolUseResult": {"questions": [asked], "answers": {str(question): answer}, "annotations": {}},
    }
    return call_id, [json.dumps(call), json.dumps(result)]


def _record_answer(repo: Path, header: str, question: object, answer: str = "Approve", **shape: object) -> str:
    """Append one answered token question to ``repo``'s session transcript and return its call id."""
    answered_at = shape.pop("answered_at", None) or datetime.now(timezone.utc)
    call_id, lines = _answer_lines(header, question, answer, answered_at=answered_at, **shape)  # type: ignore[arg-type]
    with _session_transcript(repo).open("a", encoding="utf-8", newline="\n") as log:
        log.write("".join(f"{line}\n" for line in lines))
    return call_id


def _file_digest(command: str, cwd: Path) -> str | None:
    """Return the ``files_sha256`` the gh write writer records for ``command`` run in ``cwd``, or None when refused.

    Asks the library itself, so a planted token binds the same file states the real writer would.
    """
    script = (
        f"const g = require({json.dumps(str(_APPROVAL_LIB))});"
        "process.stdout.write(JSON.stringify(g.commandFileDigest(process.argv[1], process.argv[2])));"
    )
    proc = subprocess.run(
        ["node", "-e", script, command, str(cwd)], capture_output=True, encoding="utf-8", check=True, timeout=15
    )
    return json.loads(proc.stdout).get("digest")


@pytest.fixture(name="approval_transcript")
def _approval_transcript() -> Callable[[Path], Path]:
    """Return a callable mapping a checkout to the session transcript the token fixtures record answers in.

    A spend proves the token through the transcript, so every ``PreToolUse`` payload spending a planted token carries
    ``"transcript_path": str(approval_transcript(repo))``.
    """
    return _session_transcript


@pytest.fixture(name="approval_answer")
def _approval_answer() -> Callable[..., str]:
    """Return a callable recording one answered token question in a checkout's session transcript.

    ``approval_answer(repo, header, question, answer="Approve", answered_at=..., prefilled=False, is_error=False)``
    returns the call id; pass it as ``question_id`` to a token fixture to plant a token approved by that answer.
    """
    return _record_answer


@pytest.fixture(name="push_token")
def _push_token() -> Callable[..., Path]:
    """Return a callable writing a push token into a main checkout's git common dir, ``<repo>/.git``.

    Shaped as ``approval-guard.js`` records the user's ``Approve`` to the ``git-push`` question: the question text
    naming the branch and showing ``command``, the approving call's id as ``question_id``, the checkout's current branch
    and HEAD, and an ``expires_at`` 15 minutes after ``created_at``. The question and the user's ``Approve`` are
    recorded in the session transcript (``approval_transcript``), which the spend checks. ``overrides`` replace any
    field, so a case can plant an expired, foreign, malformed or unproven token; the transcript records the question
    text after overrides.
    """

    def _write(repo: Path, command: str = "git push origin main", **overrides: object) -> Path:
        """Write the token approving the push ``command`` shows in ``repo`` and return its path."""
        now = datetime.now(timezone.utc)
        branch = _git_line(repo, "symbolic-ref", "--short", "HEAD")
        question = overrides.get("question", f"Push branch {branch} now: `{command}`?")
        record = {
            "version": 1,
            "scope": "push-once",
            "created_at": _iso(now),
            "question": question,
            "answer": "Approve",
            "question_id": _record_answer(repo, "git-push", question),
            "branch": branch,
            "head": _git_line(repo, "rev-parse", "HEAD"),
            "expires_at": _iso(now + timedelta(minutes=15)),
            **overrides,
        }
        token = repo / ".git" / "claude-push-approval.json"
        token.write_text(json.dumps(record) + "\n", encoding="utf-8", newline="\n")
        return token

    return _write


@pytest.fixture(name="gh_write_token")
def _gh_write_token() -> Callable[..., Path]:
    """Return a callable writing a gh write token into a main checkout's git common dir, ``<repo>/.git``.

    Shaped as ``approval-guard.js`` records the user's ``Approve`` to the ``gh-write`` question: the question naming
    ``approved`` in an inline code span, the approving call's id as ``question_id``, that command as ``command``, the
    digest of the files it names as they are now in ``repo`` (``files_sha256``), and an ``expires_at`` 15 minutes after
    ``created_at``. The question and the user's ``Approve`` are recorded in the session transcript
    (``approval_transcript``). ``overrides`` replace any field, ``command`` included, so a case can plant an expired,
    foreign, malformed or unproven token; the transcript records the question text after overrides.
    """

    def _write(repo: Path, approved: str = "gh pr create --title fix --body-file body.md", **overrides: object) -> Path:
        """Write the token approving the command ``approved`` in ``repo`` and return its path."""
        now = datetime.now(timezone.utc)
        question = overrides.get("question", f"Open the PR: `{approved}`?")
        record = {
            "version": 1,
            "scope": "gh-write-once",
            "created_at": _iso(now),
            "question": question,
            "answer": "Approve",
            "question_id": _record_answer(repo, "gh-write", question),
            "command": approved,
            "files_sha256": _file_digest(approved, repo),
            "expires_at": _iso(now + timedelta(minutes=15)),
            **overrides,
        }
        token = repo / ".git" / "claude-gh-write-approval.json"
        token.write_text(json.dumps(record) + "\n", encoding="utf-8", newline="\n")
        return token

    return _write
