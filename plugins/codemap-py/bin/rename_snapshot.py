#!/usr/bin/env python
"""Per-edit rollback journal for the ``rename-refs`` skills.

Consumers: ``claude-skills/rename-refs/SKILL.md`` and ``codex-skills/rename-refs/SKILL.md``. Both skills edit source
files with the agent's own ``Edit`` tool or ``git mv``, so there is no Python edit loop to wrap; instead the skill calls
this helper around every source write and the helper keeps a journal of what the run changed:

``before --file X``
    Copy X (bytes and mode) and record its pre-edit SHA-256 the first time the run touches it. A later ``before`` on a
    journaled file only verifies that the file still holds the run's last recorded state. A file journaled for the
    first time is refused when it changed after the journal opened (the run's preflight point), because its snapshot
    would then capture an edit the run never recorded. ``before`` with no file only opens the journal.
``after --file X`` / ``after --move SRC DST``
    Record the post-edit SHA-256 of X, or a completed ``git mv`` from SRC to DST. ``after`` on a file without a prior
    ``before`` is refused and remembered, so ``restore`` reports it.
``restore [--journal DIR]``
    Undo the run all-or-nothing, in two phases. The check phase walks the journal in reverse with no writes, judging
    each operation against the state the undos before it would leave: a file whose hash equals its last recorded
    post-edit hash gets its original bytes and mode back; a file the run created is deleted only while it still holds
    the recorded created bytes; a recorded ``git mv`` is undone with ``git mv`` back, never ``git reset``, so the
    user's staged state survives. Any other file is a conflict. One conflict means nothing is restored: every file keeps
    its current bytes, every conflict is reported and the journal is retained, so the original bytes stay recoverable
    and ``restore --journal`` can run again once the conflicts are resolved. Only a fully clean plan is executed, and
    each write re-checks its file first; a file that changes between the two phases stops the restore at that
    operation and retains the journal. ``--journal`` restores a journal set aside earlier (the ``RETAINED_JOURNAL``
    path); it must be a rename journal directly under the temporary base, and it stays at that path on a conflict.
``discard [--retain]``
    Remove the journal after verified success. With ``--retain`` a leftover journal is set aside so a new run starts
    clean without losing an abandoned run's backups; when it holds recorded operations the command prints its
    ``RETAINED_JOURNAL`` path and exits 5, so the caller stops instead of opening a new journal over unfinished edits.
    A leftover journal with no operations is removed with exit 0.

Every path is validated when it is recorded and again when it is restored: it must lie inside the project root, have
no symlink or Windows junction component, and be a regular file whose ``os.lstat`` identity matches the descriptor the
helper reads (``os.path.samestat``). The journal directory lives under ``$TMPDIR`` (``tempfile.gettempdir()`` on
native Windows), is private (0700 on POSIX), is refused when it is a link, and is keyed exactly like the hook layer's
``session_key``: the project name plus ``CSID``/``CLAUDE_CODE_SESSION_ID`` on Claude or ``CODEX_THREAD_ID`` on Codex,
falling back to a project-scoped ``shared`` and never to a bare machine-global one. The project name comes from the
project root rather than the working directory, so a call from a subdirectory reaches the same journal.

Exit codes: ``0`` done; ``2`` usage error; ``3`` refused (containment, changed file, unjournaled edit, unsafe journal);
``4`` restore found conflicts and kept every file as it is (or stopped when a file changed during the restore), journal
retained; ``5`` ``discard --retain`` set aside a leftover journal that holds a previous run's operations.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath

#: Exit status for a refused operation; the skill treats it as a failure of the step that called the helper.
EXIT_REFUSED = 3
#: Exit status for a restore that found a conflict, restored nothing and retained the journal.
EXIT_CONFLICTS = 4
#: Exit status for ``discard --retain`` setting aside a journal that holds a previous run's operations.
EXIT_LEFTOVER = 5
#: Restore outcomes, in report order.
_OUTCOMES = ("restored", "removed", "moved_back", "unchanged")
#: Characters a journal key may keep; everything else becomes ``-`` exactly as in ``hooks/_hookutil.py``.
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9_-]")
_RUNTIMES = ("claude", "codex")
_JOURNAL_PREFIX = "codemap-rename-journal-"
_JOURNAL_FILE = "journal.jsonl"
_BLOB_DIR = "blobs"
_GIT_TIMEOUT_S = 60


class Refused(Exception):
    """An operation the journal will not perform; the message names the path and the reason."""


@dataclass
class Segment:
    """One file's journaled history from its first ``before`` until restore or a recorded move away."""

    path: str
    existed: bool
    pre_sha: str | None
    blob: str | None
    mode: int | None
    post_sha: str | None = None
    edited: bool = False
    moved_away: bool = False

    def expected(self) -> str | None:
        """Return the hash the file should hold now: the last recorded edit, or the original when none was."""
        return self.post_sha if self.edited else self.pre_sha


@dataclass
class Move:
    """A completed ``git mv`` recorded by ``after --move``."""

    src: str
    dst: str
    sha256: str | None


@dataclass
class Unjournaled:
    """A file reported through ``after`` without a prior ``before``; never restorable, always a conflict."""

    path: str


@dataclass
class Journal:
    """The replayed journal of one run: its root, opening time and ordered operations."""

    directory: Path
    root: Path
    started_ns: int
    ops: list[Segment | Move | Unjournaled] = field(default_factory=list)

    def active(self, path: str) -> Segment | None:
        """Return the newest segment for *path* that has not been moved away."""
        for op in reversed(self.ops):
            if isinstance(op, Segment) and op.path == path and not op.moved_away:
                return op
        return None

    def moved_into(self, path: str) -> Move | None:
        """Return the newest recorded move whose destination is *path*."""
        for op in reversed(self.ops):
            if isinstance(op, Move) and op.dst == path:
                return op
        return None


def session_key(root: Path, runtime: str) -> str:
    """Return the journal key, derived like ``hooks/_hookutil.session_key`` with no host session id.

    Args:
        root: Project root; its name scopes the key the way the hook layer uses the working directory name.
        runtime: ``"claude"`` or ``"codex"``; selects which session variable identifies the run.

    Returns:
        A filename-safe ``<project>-<session>`` key, never a bare ``shared``.

    Examples:
        >>> session_key(Path("/work/my proj"), "codex").startswith("my-proj-")
        True
    """
    if runtime == "codex":
        csid = os.environ.get("CODEX_THREAD_ID") or "shared"
    else:
        csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    return f"{_UNSAFE_KEY.sub('-', root.name)}-{_UNSAFE_KEY.sub('-', csid)}"


def temp_base() -> Path:
    """Return the base directory shared with the skills' ``${TMPDIR:-/tmp}`` sentinels.

    Returns:
        ``$TMPDIR`` when set, else ``tempfile.gettempdir()`` on native Windows and ``/tmp`` elsewhere.

    Examples:
        >>> isinstance(temp_base(), Path)
        True
    """
    return Path(os.environ.get("TMPDIR") or (tempfile.gettempdir() if os.name == "nt" else "/tmp"))  # noqa: S108


def _lexical_relative(root: PurePath, candidate: PurePath) -> PurePosixPath | None:
    """Return *candidate* relative to *root* after lexical normalization, or ``None`` when it leaves the root.

    Works on pure paths so Windows drive and separator rules can be checked on any host. The root itself is not a
    journaled file and also returns ``None``.

    Args:
        root: Absolute, normalized project root.
        candidate: Absolute path, or a path relative to *root*.

    Returns:
        The POSIX-form relative path, or ``None``.

    Examples:
        >>> _lexical_relative(PurePosixPath("/p"), PurePosixPath("pkg/../a.py"))
        PurePosixPath('a.py')
        >>> _lexical_relative(PurePosixPath("/p"), PurePosixPath("/p/../q/a.py")) is None
        True
    """
    full = candidate if candidate.is_absolute() else root / candidate
    parts: list[str] = []
    for part in full.parts[1:]:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in ("", "."):
            parts.append(part)
    try:
        relative = type(full)(full.anchor, *parts).relative_to(root)
    except ValueError:
        return None
    return PurePosixPath(*relative.parts) if relative.parts else None


def _is_link_like(status: os.stat_result | object) -> bool:
    """Return whether an ``lstat`` result is a symlink or a Windows reparse point such as a junction.

    Examples:
        >>> from types import SimpleNamespace
        >>> _is_link_like(SimpleNamespace(st_mode=stat.S_IFREG | 0o644))
        False
    """
    mode = getattr(status, "st_mode", 0)
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _relative_via_alias(root: Path, candidate: Path) -> PurePosixPath | None:
    """Return *candidate* relative to an ancestor that is the project root under another spelling.

    An editor may pass ``/var/...`` while git reports ``/private/var/...`` for the same directory. Ancestors are tried
    from the filesystem root downward, so the outermost match wins and a link inside the project is never taken for the
    root; the component walk in :func:`contained` then still refuses links below it.
    """
    normalized = Path(os.path.normpath(candidate))
    try:
        root_status = os.stat(root)
    except OSError:
        return None
    for ancestor in reversed(normalized.parents):
        with contextlib.suppress(OSError):
            if os.path.samestat(os.stat(ancestor), root_status):
                relative = normalized.relative_to(ancestor)
                return PurePosixPath(*relative.parts) if relative.parts else None
    return None


def contained(root: Path, raw: str) -> tuple[str, Path]:
    """Validate *raw* against the project root and return its journal key and absolute path.

    Args:
        root: Absolute project root.
        raw: Path as the skill passes it, relative to the root or absolute.

    Returns:
        ``(relative POSIX key, absolute path)``.

    Raises:
        Refused: The path leaves the root, crosses a symlink or junction, has no parent directory, resolves outside
            the root, or names something other than a regular file.
    """
    relative = _lexical_relative(root, Path(raw))
    if relative is None and Path(raw).is_absolute():
        relative = _relative_via_alias(root, Path(raw))
    if relative is None:
        raise Refused(f"{raw} — outside the project root")
    key = relative.as_posix()
    current = root
    for part in relative.parts:
        current = current / part
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            break
        if _is_link_like(status):
            raise Refused(f"{key} — symlink or junction component {current.relative_to(root).as_posix()}")
    target = root / Path(*relative.parts)
    try:
        parent = target.parent.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise Refused(f"{key} — parent directory missing") from exc
    if not parent.is_relative_to(root.resolve()):
        raise Refused(f"{key} — resolves outside the project root")
    with contextlib.suppress(FileNotFoundError):
        if not stat.S_ISREG(os.lstat(target).st_mode):
            raise Refused(f"{key} — not a regular file")
    return key, target


def read_identity(target: Path) -> tuple[str, bytes, int] | None:
    """Read a regular file through a descriptor whose identity matches its ``lstat`` entry.

    Args:
        target: Absolute path already validated by :func:`contained`.

    Returns:
        ``(sha256, bytes, permission bits)``, or ``None`` when the file is absent.

    Raises:
        Refused: The entry is not a regular file or changed identity between ``lstat`` and open.
    """
    try:
        entry = os.lstat(target)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode):
        raise Refused(f"{target.name} — not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise Refused(f"{target.name} — unreadable: {exc.strerror}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        if not os.path.samestat(entry, os.fstat(handle.fileno())):
            raise Refused(f"{target.name} — file identity changed while reading")
        data = handle.read()
    return hashlib.sha256(data).hexdigest(), data, stat.S_IMODE(entry.st_mode)


def current_sha(target: Path) -> str | None:
    """Return the SHA-256 of *target*, or ``None`` when absent."""
    found = read_identity(target)
    return None if found is None else found[0]


def _private_directory(path: Path, create: bool) -> bool:
    """Ensure *path* is a private, non-link directory; return whether it exists afterwards."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        if not create:
            return False
        os.mkdir(path, 0o700)
        if os.name != "nt":
            os.chmod(path, 0o700)
        return True
    if _is_link_like(status) or not stat.S_ISDIR(status.st_mode):
        raise Refused(f"journal directory {path} is a link or not a directory")
    if os.name != "nt" and (status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) & 0o077):
        raise Refused(f"journal directory {path} is not private to this user")
    return True


def _append(journal_dir: Path, event: dict[str, object]) -> None:
    """Append one event line to the journal and flush it to disk."""
    with (journal_dir / _JOURNAL_FILE).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _apply_event(journal: Journal, event: dict) -> None:
    """Fold one journal event into the replayed state."""
    kind = event["event"]
    if kind == "before":
        journal.ops.append(Segment(event["path"], event["existed"], event["sha256"], event["blob"], event["mode"]))
    elif kind == "after":
        segment = journal.active(event["path"])
        if segment is None:
            raise Refused(f"journal names {event['path']} in `after` without `before`")
        segment.post_sha, segment.edited = event["sha256"], True
    elif kind == "move":
        segment = journal.active(event["src"])
        if segment is not None:
            segment.moved_away = True
        journal.ops.append(Move(event["src"], event["dst"], event["sha256"]))
    elif kind == "unjournaled":
        journal.ops.append(Unjournaled(event["path"]))


def _load(journal_dir: Path, root: Path) -> Journal:
    """Replay the journal file in *journal_dir* for the project *root*."""
    text = (journal_dir / _JOURNAL_FILE).read_text(encoding="utf-8")
    lines = text.split("\n")
    complete = lines[:-1]  # a final line without a newline is a torn append and is ignored
    try:
        events = [json.loads(line) for line in complete if line]
        start = events[0]
        if start.get("event") != "start" or start.get("root") != str(root):
            raise Refused(f"journal {journal_dir} belongs to another project root: {start.get('root')}")
        journal = Journal(journal_dir, root, int(start["started_ns"]))
        for event in events[1:]:
            _apply_event(journal, event)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise Refused(f"journal {journal_dir} is unreadable: {exc}") from exc
    return journal


def journal_directory(root: Path, runtime: str) -> Path:
    """Return the session-scoped journal directory path (it may not exist yet)."""
    return temp_base() / f"{_JOURNAL_PREFIX}{session_key(root, runtime)}"


def open_journal(root: Path, runtime: str, create: bool) -> Journal | None:
    """Load the run's journal, creating it (the run's preflight point) when *create* is set."""
    directory = journal_directory(root, runtime)
    if not _private_directory(directory, create):
        return None
    if not (directory / _JOURNAL_FILE).is_file():
        _private_directory(directory / _BLOB_DIR, True)
        _append(directory, {"event": "start", "root": str(root), "started_ns": time.time_ns(), "version": 1})
        print(f"JOURNAL_OPEN {directory}")
    _private_directory(directory / _BLOB_DIR, True)
    return _load(directory, root)


def _save_blob(journal: Journal, data: bytes) -> str:
    """Store original bytes in a new private blob file and return its journal-relative name."""
    name = f"{_BLOB_DIR}/{len(journal.ops):05d}.bin"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(journal.directory / name, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return name


def cmd_before(journal: Journal, raw: str) -> None:
    """Journal one file before the skill writes it; refuse when its state cannot be trusted."""
    key, target = contained(journal.root, raw)
    found = read_identity(target)
    sha = None if found is None else found[0]
    segment = journal.active(key)
    if segment is not None:
        if sha != segment.expected():
            raise Refused(f"{key} — changed since its last journaled state")
        print(f"JOURNALED {key}")
        return
    move = journal.moved_into(key)
    moved_here = move is not None and move.sha256 == sha
    if found is not None and not moved_here and os.lstat(target).st_mtime_ns > journal.started_ns:
        raise Refused(f"{key} — changed after the rename journal opened; it may hold an unjournaled edit")
    blob = None if found is None else _save_blob(journal, found[1])
    event = {"event": "before", "path": key, "existed": found is not None, "sha256": sha, "blob": blob}
    event["mode"] = None if found is None else found[2]
    _append(journal.directory, event)
    journal.ops.append(Segment(key, found is not None, sha, blob, event["mode"]))
    print(f"JOURNALED {key}")


def cmd_after(journal: Journal, raw: str) -> None:
    """Record the post-edit hash of a journaled file; remember and refuse an unjournaled one."""
    key, target = contained(journal.root, raw)
    sha = current_sha(target)
    segment = journal.active(key)
    if segment is None:
        _append(journal.directory, {"event": "unjournaled", "path": key, "sha256": sha})
        raise Refused(f"{key} — edited without `before`; left as is, restore reports it")
    _append(journal.directory, {"event": "after", "path": key, "sha256": sha})
    segment.post_sha, segment.edited = sha, True
    print(f"RECORDED {key}")


def cmd_after_move(journal: Journal, raw_src: str, raw_dst: str) -> None:
    """Record a completed ``git mv``; the destination must hold exactly the source's journaled bytes."""
    src, src_path = contained(journal.root, raw_src)
    dst, dst_path = contained(journal.root, raw_dst)
    segment = journal.active(src)
    sha = current_sha(dst_path)
    problem = None
    if segment is None or not segment.existed:
        problem = f"{src} — moved without a `before` snapshot of an existing file"
    elif current_sha(src_path) is not None:
        problem = f"{src} — still present after the move"
    elif sha is None or sha != segment.expected():
        problem = f"{dst} — does not hold the journaled bytes of {src}"
    if problem is not None:
        # an unrecorded move would leave restore silent while the `git mv` stays; record it so restore reports it
        _append(journal.directory, {"event": "unjournaled", "path": dst, "sha256": sha})
        raise Refused(f"{problem}; left as is, restore reports {dst}")
    _append(journal.directory, {"event": "move", "src": src, "dst": dst, "sha256": sha})
    segment.moved_away = True
    journal.ops.append(Move(src, dst, sha))
    print(f"RECORDED_MOVE {src} -> {dst}")


def _write_back(target: Path, data: bytes, mode: int | None, expected: str | None) -> None:
    """Atomically put original bytes back, re-checking the target's identity and hash just before replacing it."""
    descriptor, temporary = tempfile.mkstemp(prefix=".rename-restore-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if current_sha(target) != expected:
            raise Refused(f"{target.name} — changed during restore")
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


@dataclass
class _View:
    """File hashes and git-index membership as the restore steps checked so far would leave them.

    The check phase consults these overrides before the disk, because each undo in the reverse replay changes what the
    next one sees: before any write, a moved and then edited file still holds its edit and the move's source is empty.
    With no overrides the view reads the disk, which is how the execution phase re-checks each step.
    """

    root: Path
    shas: dict[str, str | None] = field(default_factory=dict)
    index: dict[str, bool] = field(default_factory=dict)

    def sha(self, key: str, target: Path) -> str | None:
        """Return the hash *key* holds in this view, ``None`` when absent."""
        return self.shas[key] if key in self.shas else current_sha(target)

    def tracked(self, key: str) -> bool:
        """Return whether git's index holds *key* in this view."""
        return self.index[key] if key in self.index else _tracked(self.root, key)


def _check_segment(journal: Journal, segment: Segment, view: _View) -> str:
    """Return how restore treats a segment, or raise :class:`Refused` naming the conflict; never writes."""
    _, target = contained(journal.root, segment.path)
    sha = view.sha(segment.path, target)
    if sha == segment.pre_sha:
        return "unchanged"
    if not segment.edited:
        raise Refused(f"{segment.path} — changed with no recorded edit by this run (another writer)")
    if sha != segment.post_sha:
        raise Refused(f"{segment.path} — changed after the run's last recorded edit")
    if not segment.existed:
        return "removed"
    if not (journal.directory / str(segment.blob)).is_file():
        raise Refused(f"{segment.path} — original bytes missing from the journal")
    return "restored"


def _tracked(root: Path, key: str) -> bool:
    """Return whether git's index holds *key*."""
    done = _git(root, "ls-files", "--error-unmatch", "--", key)
    return done.returncode == 0


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in the project root."""
    return subprocess.run(  # noqa: S603 - argv list, no shell; journaled repo-relative paths follow `--`
        ["git", "-C", str(root), *args],  # noqa: S607 - git resolved via PATH on purpose, like the skill blocks
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )


def _check_move(journal: Journal, move: Move, view: _View) -> str:
    """Return how restore treats a recorded ``git mv``, or raise :class:`Refused` naming the conflict; never writes."""
    _, src_path = contained(journal.root, move.src)
    _, dst_path = contained(journal.root, move.dst)
    dst_sha, src_sha = view.sha(move.dst, dst_path), view.sha(move.src, src_path)
    if dst_sha is None and src_sha == move.sha256:
        return "unchanged"
    if dst_sha != move.sha256:
        raise Refused(f"{move.dst} — moved file changed after the run's last recorded state")
    if not view.tracked(move.dst):
        raise Refused(f"{move.dst} — moved file is no longer in git's index, so `git mv` back cannot run")
    if src_sha is not None or view.tracked(move.src):
        raise Refused(f"{move.src} — original path is occupied")
    return "moved_back"


def _check(journal: Journal, op: Segment | Move | Unjournaled, view: _View) -> tuple[str, str]:
    """Return ``(outcome, label)`` for undoing one operation in *view*, or raise :class:`Refused`; never writes."""
    if isinstance(op, Unjournaled):
        raise Refused(f"{op.path} — written or moved without a matching `before`; not restorable")
    if isinstance(op, Move):
        return _check_move(journal, op, view), f"{op.dst} -> {op.src}"
    return _check_segment(journal, op, view), op.path


def _project(op: Segment | Move | Unjournaled, view: _View) -> None:
    """Advance *view* to the state undoing *op* leaves, so later checks see it (and a refused op adds no echoes)."""
    if isinstance(op, Move):
        view.shas[op.src], view.shas[op.dst] = op.sha256, None
        view.index[op.src], view.index[op.dst] = True, False
    elif isinstance(op, Segment):
        view.shas[op.path] = op.pre_sha


def _apply(journal: Journal, op: Segment | Move | Unjournaled, outcome: str) -> None:
    """Perform one undo that :func:`_check` just cleared against the disk."""
    if isinstance(op, Move) and outcome == "moved_back":
        done = _git(journal.root, "mv", "--", op.dst, op.src)
        if done.returncode != 0:
            raise Refused(f"{op.dst} — git mv back failed: {done.stderr.strip()[:300]}")
    elif isinstance(op, Segment) and outcome in ("restored", "removed"):
        _, target = contained(journal.root, op.path)
        if outcome == "removed":
            os.unlink(target)
        else:
            _write_back(target, (journal.directory / str(op.blob)).read_bytes(), op.mode, op.post_sha)


def _plan_conflicts(journal: Journal) -> list[tuple[Segment | Move | Unjournaled, str]]:
    """Check every operation in reverse with no writes and return each conflict with its reason."""
    view = _View(journal.root)
    conflicts: list[tuple[Segment | Move | Unjournaled, str]] = []
    for op in reversed(journal.ops):
        try:
            _check(journal, op, view)
        except (Refused, OSError) as exc:
            conflicts.append((op, str(exc)))
        _project(op, view)
    return conflicts


def _execute(
    journal: Journal, counts: dict[str, int], lines: list[str]
) -> tuple[Segment | Move | Unjournaled, str] | None:
    """Undo every operation in reverse, re-checking each against the disk; return the operation that stopped it."""
    for op in reversed(journal.ops):
        try:
            outcome, label = _check(journal, op, _View(journal.root))
            _apply(journal, op, outcome)
        except (Refused, OSError) as exc:
            return op, f"{exc}; restore stopped here, earlier journal operations left as they are"
        counts[outcome] += 1
        if outcome != "unchanged":
            lines.append(f"{outcome.upper()} {label}")
    return None


def _set_aside(directory: Path) -> Path:
    """Rename a journal directory to a retained sibling so a new run starts with a fresh journal."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    retained = directory.with_name(f"{directory.name}-retained-{stamp}-{time.time_ns() % 1_000_000:06d}")
    os.replace(directory, retained)
    return retained


def cmd_restore(journal: Journal, retained: bool = False) -> int:
    """Undo the run all-or-nothing; on any conflict restore nothing, keep the journal and report every conflict.

    Args:
        journal: The loaded journal to replay.
        retained: The journal was already set aside (``restore --journal``); keep it at its path on a conflict.

    Returns:
        ``0`` when every operation was undone, else :data:`EXIT_CONFLICTS`.
    """
    counts = dict.fromkeys(_OUTCOMES, 0)
    lines: list[str] = []
    conflicts = _plan_conflicts(journal)
    if conflicts:
        lines.append(
            "NOTHING_RESTORED — a conflict keeps every file as it is; resolve each CONFLICT, then restore again"
        )
    else:
        stopped = _execute(journal, counts, lines)
        conflicts = [] if stopped is None else [stopped]
    kept = None
    if conflicts:
        kept = journal.directory if retained else _set_aside(journal.directory)
    else:
        shutil.rmtree(journal.directory)
    for op, reason in conflicts:
        backup = f"; original bytes: {kept / op.blob}" if kept and isinstance(op, Segment) and op.blob else ""
        path = op.dst if isinstance(op, Move) else op.path
        lines.append(f"CONFLICT {path} — {reason}{backup}")
    print("\n".join(lines))
    summary = " ".join(f"{name}={value}" for name, value in counts.items())
    print(f"RESTORE_SUMMARY {summary} conflicts={len(conflicts)}")
    if kept is not None:
        print(f"RETAINED_JOURNAL {kept}")
        return EXIT_CONFLICTS
    return 0


def cmd_discard(root: Path, runtime: str, retain: bool) -> int:
    """Remove the journal; with *retain*, set aside one holding operations and return :data:`EXIT_LEFTOVER`."""
    journal = open_journal(root, runtime, create=False)
    if journal is None:
        print("NO_JOURNAL")
        return 0
    if retain and journal.ops:
        retained = _set_aside(journal.directory)
        print(f"RETAINED_JOURNAL {retained}")
        print(
            f"! rename journal leftover — a previous rename left unfinished edits; its journal is retained at {retained}."
            f" Stop before a new run; undo them with `restore --journal {retained}`.",
            file=sys.stderr,
        )
        return EXIT_LEFTOVER
    shutil.rmtree(journal.directory)
    print(f"DISCARDED {journal.directory}")
    return 0


def open_retained(root: Path, raw: str) -> Journal:
    """Load a journal set aside earlier, refusing any directory that is not a private rename journal under the base.

    Args:
        root: Project root the journal must belong to.
        raw: The ``RETAINED_JOURNAL`` path.

    Returns:
        The replayed journal.

    Raises:
        Refused: The directory is missing, outside the temporary base, not a rename journal, a link, not private, or
            recorded for another project root.
    """
    directory = Path(os.path.abspath(raw))
    try:
        same_base = os.path.samestat(os.stat(directory.parent), os.stat(temp_base()))
    except OSError:
        same_base = False
    if not same_base or not directory.name.startswith(_JOURNAL_PREFIX):
        raise Refused(f"{raw} — not a rename journal directly under {temp_base()}")
    if not _private_directory(directory, create=False):
        raise Refused(f"{raw} — no journal at this path")
    return _load(directory, root)


def _project_root(explicit: str | None) -> Path:
    """Return the project root: ``--root``, else the git top level, else the working directory."""
    if explicit:
        return Path(os.path.abspath(explicit))
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        done = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607 - git resolved via PATH on purpose
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        if done.returncode == 0 and done.stdout.strip():
            return Path(os.path.abspath(done.stdout.strip()))
    return Path.cwd()


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Per-edit rollback journal for rename-refs.",
        epilog=(
            "Exit codes: 0 done; 2 usage error; 3 refused; 4 restore found conflicts, restored nothing and retained the"
            " journal; 5 discard --retain set aside a journal holding a previous run's operations."
        ),
    )
    parser.add_argument("--root", help="project root (default: git top level, else the working directory)")
    default_runtime = os.environ.get("CODEMAP_RUNTIME", "").strip().lower()
    parser.add_argument(
        "--runtime", choices=_RUNTIMES, default=default_runtime if default_runtime in _RUNTIMES else "claude"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    before = commands.add_parser("before", help="journal files before a write; no file only opens the journal")
    before.add_argument("--file", action="append", default=[], dest="files")
    after = commands.add_parser("after", help="record a completed edit or git mv")
    target = after.add_mutually_exclusive_group(required=True)
    target.add_argument("--file", action="append", dest="files")
    target.add_argument("--move", nargs=2, metavar=("SRC", "DST"))
    restore = commands.add_parser("restore", help="undo the run's journaled writes all-or-nothing")
    restore.add_argument("--journal", help="restore a journal set aside earlier (its RETAINED_JOURNAL path)")
    discard = commands.add_parser("discard", help="remove the journal after verified success")
    discard.add_argument(
        "--retain",
        action="store_true",
        help="set a leftover journal aside instead of deleting; exit 5 when it holds recorded operations",
    )
    return parser


def _dispatch(args: argparse.Namespace, root: Path) -> int:
    """Run the parsed command and return its exit status."""
    if args.command == "discard":
        return cmd_discard(root, args.runtime, args.retain)
    if args.command == "restore" and args.journal:
        return cmd_restore(open_retained(root, args.journal), retained=True)
    journal = open_journal(root, args.runtime, create=args.command != "restore")
    if journal is None:
        print("NO_JOURNAL")
        return 0
    if args.command == "restore":
        return cmd_restore(journal)
    if args.command == "after" and args.move:
        cmd_after_move(journal, *args.move)
        return 0
    for raw in args.files:
        (cmd_before if args.command == "before" else cmd_after)(journal, raw)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, run one journal command and map refusals to exit status 3.

    Args:
        argv: Command-line arguments without the program name; ``None`` reads ``sys.argv``.

    Returns:
        The process exit status.

    Examples:
        >>> main(["--help"])  # doctest: +SKIP
    """
    args = _parser().parse_args(argv)
    root = _project_root(args.root)
    try:
        return _dispatch(args, root)
    except Refused as exc:
        print(f"! rename journal refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"! rename journal refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
