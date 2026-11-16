#!/usr/bin/env python
"""codex_state.py — read and bump the research:run Codex co-pilot counters, and guard the quota stop's lift.

The Codex co-pilot phase (``skills/run/modes/codex-copilot.md``) keeps its counters in a small JSON file,
``.experiments/state/<run-id>/codex-state``, because counters tracked in prose are lost to mid-run compaction and
silently re-open the whole Codex budget::

    {"codex_iter": 3, "noop_streak": 1}

A pass stopped by a harness-classified ``quota-exhausted`` bridge fault adds one stored flag, written only when true::

    {"codex_iter": 4, "noop_streak": 1, "codex_blocked": true}

``CODEX_DISABLED`` is derived on every read, never stored, so it cannot go stale: it is true when the no-op streak
reaches ``NOOP_STREAK_LIMIT`` (2) or when ``codex_blocked`` is set. The quota flag is the one stored fact, because no
counter can derive it; a file without the key (older runs, or a run never blocked) reads as not blocked.

Subcommands on the run's state file (``--state``):

* ``read`` prints ``CODEX_ITER=<n> CODEX_NOOP_STREAK=<n> CODEX_DISABLED=<true|false>`` — the gate check's one line —
  followed by `` CODEX_BLOCKED=true`` only when the quota flag is stored, so the gate can tell a quota stop from a
  no-op streak after a compaction.
* ``bump`` increments ``codex_iter`` and updates the state for the pass outcome: ``--outcome noop`` grows the no-op
  streak, ``--outcome productive`` resets it to 0, ``--outcome blocked`` leaves it unchanged and stores the quota flag.
  The flag is sticky: a later ``noop`` or ``productive`` bump keeps it. The file is rewritten atomically and the same
  line is printed for the new state. It re-reads the file itself, so an increment survives a compaction that landed
  mid-iteration.
* ``block`` stores the quota flag without counting a pass, for a stop carried in from outside the run
  (``/research:sweep`` seeds a judge quota stop into the run it starts). It creates the file when missing and is
  idempotent.
* ``unblock`` lifts a stored quota flag and keeps both counters, so the cost ceiling and a no-op-streak disable still
  hold. It is the only way the flag clears, and it lifts only with this session's approval (below). A refusal prints
  why and leaves the file untouched; a missing file or a file without the flag is left untouched, byte for byte. The
  current line is printed last in every case.

Lift approval. The user approves Codex again by resuming the run with ``--codex`` typed in that request. The skill
text that sees the request's own arguments is ``skills/run/SKILL.md`` (the host substitutes ``$ARGUMENTS`` there, never
in a mode file it loads), so it runs ``arm -- "$ARGUMENTS"`` once at the start of every resume:

* ``arm`` looks for a standalone ``--codex`` token (shell-style tokens, before any ``--``; text that cannot be
  tokenized counts as no token). Found: it creates this session's lift record holding ``armed`` — create-only, so
  re-running the same command keeps whatever record exists. Not found: it withdraws an ``armed`` record, so a resume
  without ``--codex`` never inherits an earlier approval, and keeps a ``spent`` one.
* Every recorded stop (``bump --outcome blocked`` and ``block``) turns an existing record into ``spent``; it never
  creates one, so a stop stored before this resume's ``arm`` stays liftable.
* ``unblock`` lifts only while the record reads exactly ``armed``. A stop recorded after the approval (a replay of the
  lift block after a compaction, or a lift block that first ran after a Codex pass) finds ``spent`` and stands.

The record is ``${TMPDIR:-/tmp}/research-run-codex-lift-${CSID}``, one per Claude Code session. Code cannot tell a
replay from a second genuine ``--resume --codex`` in the same session after a stop recorded since the first approval:
both are refused, and the refusal says to resume from a new session. Nothing in the session clears a ``spent`` record:
an arm run from skill text the host did not substitute passes empty arguments, so a no-flag arm must not be able to
reset it.

A missing file reads as 0/0, so an interrupted run resumes with a fresh budget rather than a hard stop. An unreadable
or malformed file (including a ``codex_blocked`` value that is not a JSON boolean) does the same, with a warning on
stderr (the former ``read_state_field.py ... || echo 0`` did so silently).

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/codex_state.py" read --state "$_CODEX_STATE"
    python "${CLAUDE_PLUGIN_ROOT}/bin/codex_state.py" bump --state "$_CODEX_STATE" --outcome noop|productive|blocked
    python "${CLAUDE_PLUGIN_ROOT}/bin/codex_state.py" block --state "$_CODEX_STATE"
    python "${CLAUDE_PLUGIN_ROOT}/bin/codex_state.py" arm -- "$ARGUMENTS"
    python "${CLAUDE_PLUGIN_ROOT}/bin/codex_state.py" unblock --state "$_CODEX_STATE"

Exit codes:
    0   ok (state line printed; a refused lift is a normal outcome)
    1   state file or lift record could not be written
    2   argument error, or ``--state`` outside the working directory and temp dir
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Sibling import: bin/ is not a package. Putting it on the path resolves the private writer whether this file runs as
#: a script, under the test conftest, or as a doctest module.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _sentinel_io import write_private_text  # noqa: E402

#: Consecutive Codex passes without code changes after which Codex is disabled for the rest of the run.
NOOP_STREAK_LIMIT = 2

#: The one invocation flag that approves Codex again on a resume.
CODEX_FLAG = "--codex"

#: Session lift record name; the session token is appended, as for every ``${TMPDIR:-/tmp}/<name>-${CSID}`` sentinel.
LIFT_RECORD_PREFIX = "research-run-codex-lift-"

#: A session token usable inside one file name: no separator can leave the temp dir.
_SESSION_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]+")

#: Exclusive create that refuses a symlink at the final path component, mode 0600; ``O_BINARY`` keeps LF on Windows.
_CREATE_ONLY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)


class Outcome(str, Enum):
    """Result of one Codex co-pilot pass, as recorded by ``bump``."""

    NOOP = "noop"
    PRODUCTIVE = "productive"
    BLOCKED = "blocked"


class LiftRecord(str, Enum):
    """Content of the session lift record: ``armed`` until a stop is recorded after the approval, then ``spent``."""

    ARMED = "armed"
    SPENT = "spent"


class LiftOutcome(str, Enum):
    """What one ``unblock`` call did, each with the line ``main`` prints for it (``NOT_BLOCKED`` prints none)."""

    LIFTED = "lifted"
    NOT_BLOCKED = "not-blocked"
    NOT_APPROVED = "not-approved"
    STOP_AFTER_APPROVAL = "stop-after-approval"


#: The line printed before the state line for each ``unblock`` outcome.
_LIFT_MESSAGES = {
    LiftOutcome.LIFTED: "codex_state: quota stop lifted — this resume typed --codex",
    LiftOutcome.NOT_APPROVED: (
        "codex_state: quota stop kept — no --codex typed with this resume (a flag restored from state.json never"
        " counts); resume with --codex to approve Codex again"
    ),
    LiftOutcome.STOP_AFTER_APPROVAL: (
        "codex_state: quota stop kept — it was recorded in this session after the --codex approval; to approve Codex"
        " again, resume from a new session with --codex"
    ),
}


@dataclass(frozen=True)
class CodexState:
    """Persisted co-pilot state for one research run.

    Attributes:
        codex_iter: Codex passes dispatched so far in the run.
        noop_streak: Consecutive passes that produced no code change.
        codex_blocked: Whether a harness-classified ``quota-exhausted`` fault stopped Codex for the run; sticky until
            ``unblock`` lifts it.

    Examples:
        >>> CodexState(2, 1).disabled
        False
        >>> CodexState(2, 2).disabled, CodexState(2, 0, codex_blocked=True).disabled
        (True, True)
    """

    codex_iter: int = 0
    noop_streak: int = 0
    codex_blocked: bool = False

    @property
    def disabled(self) -> bool:
        """Return whether Codex is off for the rest of the run (no-op streak at the limit, or a quota block)."""
        return self.codex_blocked or self.noop_streak >= NOOP_STREAK_LIMIT

    def to_json(self) -> str:
        """Serialize to the file's one JSON line, writing ``codex_blocked`` only when true.

        Examples:
            >>> CodexState(3, 0).to_json()
            '{"codex_iter": 3, "noop_streak": 0}'
            >>> CodexState(4, 1, codex_blocked=True).to_json()
            '{"codex_iter": 4, "noop_streak": 1, "codex_blocked": true}'
        """
        payload: dict[str, int | bool] = {"codex_iter": self.codex_iter, "noop_streak": self.noop_streak}
        if self.codex_blocked:
            payload["codex_blocked"] = True
        # Same bytes the former printf wrote for an unblocked run: json.dumps' default separators.
        return json.dumps(payload)


def _within(path: Path, roots: tuple[Path, ...]) -> bool:
    """Return whether ``path`` resolves under any of ``roots``."""
    resolved = path.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


def _parse_state(data: object) -> CodexState:
    """Build a ``CodexState`` from decoded JSON, raising on any unusable shape.

    Raises:
        TypeError: When ``data`` is not a JSON object, or ``codex_blocked`` is present but not a JSON boolean.
        ValueError: When a counter is not an integer.
    """
    if not isinstance(data, dict):
        raise TypeError(f"expected a JSON object, got {type(data).__name__}")
    blocked = data.get("codex_blocked", False)
    if not isinstance(blocked, bool):
        # bool("false") is True: only a real JSON boolean may set or clear the quota flag.
        raise TypeError(f"codex_blocked must be a boolean, got {type(blocked).__name__}")
    return CodexState(
        codex_iter=int(data.get("codex_iter", 0)),
        noop_streak=int(data.get("noop_streak", 0)),
        codex_blocked=blocked,
    )


def load_state(state: Path) -> CodexState:
    """Return the run's co-pilot state from ``state``, a fresh state when absent or unusable.

    Args:
        state: Path of the ``codex-state`` JSON file.

    Returns:
        The stored state. A missing file is the normal first-run case and stays silent; an unreadable file, invalid
        JSON, a non-object, a non-integer counter or a non-boolean ``codex_blocked`` also yields a fresh state but warns
        on stderr.

    Examples:
        >>> import tempfile
        >>> d = Path(tempfile.mkdtemp())
        >>> load_state(d / "absent")
        CodexState(codex_iter=0, noop_streak=0, codex_blocked=False)
        >>> _ = (d / "s").write_text('{"codex_iter": 3, "noop_streak": 1}')
        >>> load_state(d / "s")
        CodexState(codex_iter=3, noop_streak=1, codex_blocked=False)
    """
    try:
        return _parse_state(json.loads(state.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return CodexState()
    except (OSError, ValueError, ArithmeticError, RecursionError, TypeError, AttributeError) as exc:
        print(f"codex_state: {state} unusable ({exc}); reading as 0/0", file=sys.stderr)
        return CodexState()


def render(counters: CodexState) -> str:
    """Return the gate-check line for the given state.

    Examples:
        >>> render(CodexState(2, 2))
        'CODEX_ITER=2 CODEX_NOOP_STREAK=2 CODEX_DISABLED=true'
        >>> render(CodexState(2, 1))
        'CODEX_ITER=2 CODEX_NOOP_STREAK=1 CODEX_DISABLED=false'
        >>> render(CodexState(3, 0, codex_blocked=True))
        'CODEX_ITER=3 CODEX_NOOP_STREAK=0 CODEX_DISABLED=true CODEX_BLOCKED=true'
    """
    disabled = "true" if counters.disabled else "false"
    line = f"CODEX_ITER={counters.codex_iter} CODEX_NOOP_STREAK={counters.noop_streak} CODEX_DISABLED={disabled}"
    return f"{line} CODEX_BLOCKED=true" if counters.codex_blocked else line


def _next_state(current: CodexState, outcome: Outcome) -> CodexState:
    """Return the state after one more pass with ``outcome``; no outcome clears the quota flag (only ``unblock``
    does)."""
    if outcome == Outcome.BLOCKED:
        return CodexState(current.codex_iter + 1, current.noop_streak, codex_blocked=True)
    streak = current.noop_streak + 1 if outcome == Outcome.NOOP else 0
    return CodexState(current.codex_iter + 1, streak, codex_blocked=current.codex_blocked)


def _write_state(state: Path, new: CodexState) -> None:
    """Persist ``new`` to ``state`` atomically, creating parent directories when missing.

    Raises:
        OSError: When the file cannot be written.
    """
    state.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{state.name}.", suffix=".tmp", dir=state.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new.to_json() + "\n")
        os.replace(tmp_name, state)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def bump(state: Path, outcome: Outcome) -> CodexState:
    """Count one more Codex pass, update the state for ``outcome``, and persist atomically.

    Args:
        state: Path of the ``codex-state`` JSON file; parent directories are created when missing.
        outcome: ``NOOP`` grows the no-op streak, ``PRODUCTIVE`` resets it to 0, ``BLOCKED`` keeps it and stores the
            quota flag. A stored flag survives every later outcome; only ``unblock`` clears it.

    Returns:
        The new state.

    Raises:
        OSError: When the file cannot be written.

    Examples:
        >>> import tempfile
        >>> p = Path(tempfile.mkdtemp()) / "run" / "codex-state"
        >>> [(s.codex_iter, s.noop_streak) for s in (bump(p, Outcome.NOOP), bump(p, Outcome.NOOP))]
        [(1, 1), (2, 2)]
        >>> bump(p, Outcome.BLOCKED)
        CodexState(codex_iter=3, noop_streak=2, codex_blocked=True)
        >>> bump(p, Outcome.PRODUCTIVE)
        CodexState(codex_iter=4, noop_streak=0, codex_blocked=True)
    """
    new = _next_state(load_state(state), outcome)
    _write_state(state, new)
    return new


def block(state: Path) -> CodexState:
    """Store the quota flag without counting a pass, keeping both counters; idempotent.

    Args:
        state: Path of the ``codex-state`` JSON file; parent directories and the file are created when missing.

    Returns:
        The blocked state.

    Raises:
        OSError: When the file cannot be written.

    Examples:
        >>> import tempfile
        >>> p = Path(tempfile.mkdtemp()) / "run" / "codex-state"
        >>> block(p)
        CodexState(codex_iter=0, noop_streak=0, codex_blocked=True)
        >>> p.read_text()
        '{"codex_iter": 0, "noop_streak": 0, "codex_blocked": true}\\n'
    """
    current = load_state(state)
    blocked = CodexState(current.codex_iter, current.noop_streak, codex_blocked=True)
    if not current.codex_blocked:
        _write_state(state, blocked)
    return blocked


def typed_codex(arguments: list[str]) -> bool:
    """Return whether the invocation's argument text carries a standalone ``--codex`` token.

    The host substitutes the request's text into ``"$ARGUMENTS"``; quotes inside it can split that into several words,
    so the words are joined before shell-style tokenizing. Tokens after a ``--`` are operands, never flags. Text that
    cannot be tokenized (an unbalanced quote) counts as no token, so a lift fails closed.

    Args:
        arguments: The words that followed ``arm --`` on the command line.

    Returns:
        True only for an exact ``--codex`` token before any ``--``.

    Examples:
        >>> typed_codex(["--resume program.md --codex"]), typed_codex(["--resume program.md --codex-later"])
        (True, False)
        >>> typed_codex(["--resume program.md -- --codex"]), typed_codex(['--codex "unbalanced'])
        (False, False)
    """
    try:
        tokens = shlex.split(" ".join(arguments))
    except ValueError:
        return False
    options = tokens[: tokens.index("--")] if "--" in tokens else tokens
    return CODEX_FLAG in options


def lift_record_path() -> Path | None:
    """Return this session's lift record path, or ``None`` when the session token cannot name a file.

    Mirrors the shell's ``${TMPDIR:-/tmp}/research-run-codex-lift-${CSID}``; native Windows falls back to the platform
    temp dir.
    """
    token = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    if not _SESSION_TOKEN_RE.fullmatch(token) or token in {".", ".."}:
        return None
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback) / f"{LIFT_RECORD_PREFIX}{token}"


def read_lift_record(record: Path) -> LiftRecord | None:
    """Return the record's state: ``None`` when absent, ``ARMED`` only for the exact text, ``SPENT`` otherwise.

    A symlink, unreadable file or any other content reads as ``SPENT``, so an unexpected record never approves a lift.

    Examples:
        >>> import tempfile
        >>> d = Path(tempfile.mkdtemp())
        >>> read_lift_record(d / "absent") is None
        True
        >>> _ = (d / "r").write_text("armed\\n")
        >>> read_lift_record(d / "r")
        <LiftRecord.ARMED: 'armed'>
    """
    if record.is_symlink():
        return LiftRecord.SPENT
    try:
        text = record.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return LiftRecord.SPENT
    return LiftRecord.ARMED if text.strip() == LiftRecord.ARMED.value else LiftRecord.SPENT


def arm(record: Path, typed: bool) -> LiftRecord | None:
    """Record whether this resume typed ``--codex``: create ``armed`` only when absent, or withdraw an ``armed`` one.

    Args:
        record: This session's lift record.
        typed: Whether the invocation's own arguments carried ``--codex``.

    Returns:
        The record's state afterwards; ``None`` when there is none. Typed: an existing record is kept as it is, so
        replaying the same arm after a later stop cannot re-approve a lift. Not typed: an ``armed`` record is removed
        (a resume without ``--codex`` never inherits an earlier approval) and a ``spent`` one is kept, so an arm that
        ran from unsubstituted skill text (empty arguments) cannot clear the mark either.

    Raises:
        OSError: When the record cannot be created or removed.

    Examples:
        >>> import tempfile
        >>> r = Path(tempfile.mkdtemp()) / "lift"
        >>> arm(r, True), arm(r, True)
        (<LiftRecord.ARMED: 'armed'>, <LiftRecord.ARMED: 'armed'>)
        >>> arm(r, False), r.exists()
        (None, False)
        >>> _ = arm(r, True); spend_lift_record(r); arm(r, False)
        <LiftRecord.SPENT: 'spent'>
    """
    if not typed:
        if read_lift_record(record) == LiftRecord.ARMED:
            record.unlink(missing_ok=True)
        return read_lift_record(record)
    try:
        fd = os.open(record, _CREATE_ONLY_FLAGS, 0o600)
    except FileExistsError:
        return read_lift_record(record)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(LiftRecord.ARMED.value + "\n")
    return LiftRecord.ARMED


def spend_lift_record(record: Path) -> None:
    """Mark an existing lift record ``spent`` after a stop is recorded; never create one.

    Raises:
        OSError: When an existing record cannot be rewritten.

    Examples:
        >>> import tempfile
        >>> r = Path(tempfile.mkdtemp()) / "lift"
        >>> spend_lift_record(r); r.exists()
        False
        >>> _ = arm(r, True); spend_lift_record(r); read_lift_record(r)
        <LiftRecord.SPENT: 'spent'>
    """
    if record.is_symlink() or record.exists():
        write_private_text(record, LiftRecord.SPENT.value + "\n")


def unblock(state: Path, approval: LiftRecord | None) -> tuple[CodexState, LiftOutcome]:
    """Lift a stored quota flag when ``approval`` is ``ARMED``, keeping both counters, and persist atomically.

    The pass count and no-op streak are kept, so ``MAX_CODEX_RUNS`` and a streak-derived disable still apply. Nothing
    is written unless the flag is lifted: a missing file stays missing, and an unblocked, unreadable or refused file
    keeps its exact bytes.

    Args:
        state: Path of the ``codex-state`` JSON file.
        approval: This session's lift record state (``read_lift_record``).

    Returns:
        The state afterwards and what the call did.

    Raises:
        OSError: When a blocked file cannot be rewritten.

    Examples:
        >>> import tempfile
        >>> p = Path(tempfile.mkdtemp()) / "run" / "codex-state"
        >>> unblock(p, LiftRecord.ARMED)[1], p.exists()
        (<LiftOutcome.NOT_BLOCKED: 'not-blocked'>, False)
        >>> _ = block(p)
        >>> unblock(p, None)[1], unblock(p, LiftRecord.SPENT)[1]
        (<LiftOutcome.NOT_APPROVED: 'not-approved'>, <LiftOutcome.STOP_AFTER_APPROVAL: 'stop-after-approval'>)
        >>> unblock(p, LiftRecord.ARMED)
        (CodexState(codex_iter=0, noop_streak=0, codex_blocked=False), <LiftOutcome.LIFTED: 'lifted'>)
    """
    current = load_state(state)
    if not current.codex_blocked:
        return current, LiftOutcome.NOT_BLOCKED
    if approval is None:
        return current, LiftOutcome.NOT_APPROVED
    if approval != LiftRecord.ARMED:
        return current, LiftOutcome.STOP_AFTER_APPROVAL
    lifted = CodexState(current.codex_iter, current.noop_streak)
    _write_state(state, lifted)
    return lifted, LiftOutcome.LIFTED


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the five subcommands."""
    parser = argparse.ArgumentParser(prog="codex_state", description="Read or bump the Codex co-pilot counters.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("read", help="Print the gate-check line.").add_argument("--state", required=True)
    bump_parser = sub.add_parser("bump", help="Increment codex_iter and update the state for the pass outcome.")
    bump_parser.add_argument("--state", required=True)
    bump_parser.add_argument("--outcome", required=True, choices=[outcome.value for outcome in Outcome])
    sub.add_parser("block", help="Store a quota stop without counting a pass.").add_argument("--state", required=True)
    sub.add_parser("unblock", help="Lift a quota stop this session approved, keeping the counters.").add_argument(
        "--state", required=True
    )
    sub.add_parser("arm", help="Record whether this resume typed --codex.").add_argument(
        "arguments", nargs="*", help='The invocation\'s argument text, after "--".'
    )
    return parser


def _run_arm(arguments: list[str]) -> int:
    """Arm or clear this session's lift record from the invocation's arguments and print what happened."""
    record = lift_record_path()
    if record is None:
        print("codex_state: no usable session token; nothing armed, a stored quota stop stays")
        return 0
    typed = typed_codex(arguments)
    try:
        result = arm(record, typed)
    except OSError as exc:
        print(f"codex_state: cannot write the lift record {record}: {exc}", file=sys.stderr)
        return 1
    if not typed:
        print("codex_state: no --codex typed with this resume; a stored quota stop stays")
    elif result == LiftRecord.ARMED:
        print("codex_state: --codex typed with this resume; a stored quota stop from before it can be lifted")
    else:
        print("codex_state: --codex typed, but a quota stop was recorded in this session after its approval; it stays")
    return 0


def _run_unblock(state: Path) -> int:
    """Lift the stored quota stop if this session approved it, printing the outcome line and the state line."""
    record = lift_record_path()
    approval = read_lift_record(record) if record is not None else None
    try:
        counters, outcome = unblock(state, approval)
    except OSError as exc:
        print(f"codex_state: cannot write {state}: {exc}", file=sys.stderr)
        return 1
    if outcome in _LIFT_MESSAGES:
        print(_LIFT_MESSAGES[outcome])
    print(render(counters))
    return 0


def _record_stop(state: Path, command: str, outcome: str | None) -> int:
    """Run ``bump`` or ``block``, print the new line, and spend this session's lift approval when a stop was stored."""
    try:
        counters = block(state) if command == "block" else bump(state, Outcome(outcome))
    except OSError as exc:
        print(f"codex_state: cannot write {state}: {exc}", file=sys.stderr)
        return 1
    print(render(counters))
    record = lift_record_path()
    if command == "block" or Outcome(outcome) == Outcome.BLOCKED:
        try:
            if record is not None:
                spend_lift_record(record)
        except OSError as exc:
            print(f"codex_state: cannot mark the lift record {record} spent: {exc}", file=sys.stderr)
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand and return the process status.

    Args:
        argv: Optional argument list (tests); defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` on success (a refused lift included), ``1`` when the state file or lift record cannot be written, ``2``
        on an argument or path error.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.command == "arm":
        return _run_arm(args.arguments)

    # A write target, so confine it: the project the run executes in, or the session temp dir.
    state = Path(args.state)
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    # Both temp spellings stay roots: the shell's ${TMPDIR:-/tmp} and the platform dir that TEMP/TMP may name.
    roots = (
        Path.cwd(),
        Path(os.environ.get("TMPDIR") or tmp_fallback),
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir()),
    )
    if not _within(state, roots):
        print(f"codex_state: --state {state} is outside the working directory and temp dir", file=sys.stderr)
        return 2

    if args.command == "read":
        print(render(load_state(state)))
        return 0
    if args.command == "unblock":
        return _run_unblock(state)
    return _record_stop(state, args.command, getattr(args, "outcome", None))


if __name__ == "__main__":
    sys.exit(main())
