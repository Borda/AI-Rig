#!/usr/bin/env python
"""dev_run_dir.py — create ``.developments/<UTC-timestamp>/`` run directory.

With ``--sentinel <name>``, also touches ``<sentinel-dir>/<name>-<ts>``.
Sentinel name is sanitized to ``[a-zA-Z0-9_-]+`` to prevent path traversal.

With ``--persist <skill>``, also writes the run-dir path to ``<sentinel-dir>/dev-<skill>-dev-dir-<CSID>`` — the sentinel
every later block of that skill reads back with ``IFS= read -r DEV_DIR`` — so the caller needs no ``echo`` of its own.
``CSID`` is ``$CSID``, else ``$CLAUDE_CODE_SESSION_ID``, else ``shared``.

Sentinel dir honors ``$TMPDIR`` when set, else ``/tmp`` (``tempfile.gettempdir()`` on native Windows) — the Python
equivalent of the shell ``${TMPDIR:-/tmp}`` idiom callers use to poll the sentinel.

Usage:
    python dev_run_dir.py [--sentinel <name>] [--persist <skill>]

Output (stdout):
    Relative path of the created run directory.

Exit codes:
    0 — always (matches bash behaviour)
    2 — bad/missing required argument (argparse default)
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

#: Matches every character outside letters, digits, underscore and hyphen, so it can be stripped from sentinel names.
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sentinel_dir() -> Path:
    """Return ``$TMPDIR`` when set, else ``/tmp``, or ``tempfile.gettempdir()`` on native Windows.

    Matches the shell ``${TMPDIR:-/tmp}`` idiom used by callers that poll this sentinel, so both sides resolve to the
    same directory. ``tempfile.gettempdir()`` is not that fallback on POSIX: it honours ``TEMP`` and ``TMP`` before
    ``/tmp``, so with ``TMPDIR`` unset and ``TEMP`` set the script and the shell would read different files. The hooks'
    ``getSentinelDir()`` already uses ``/tmp`` on POSIX. ``os.environ`` is read first because ``tempfile.gettempdir()``
    caches its result on first call and would not observe a later ``TMPDIR`` change.
    """
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback)


def sentinel_path(name: str) -> Path:
    """Return the session-scoped sentinel file ``<sentinel-dir>/<name>-<CSID>`` that shell blocks read back.

    ``CSID`` is ``$CSID``, else ``$CLAUDE_CODE_SESSION_ID``, else ``shared``, stripped to ``[a-zA-Z0-9_-]``.

    Args:
        name: Sentinel base name, without the ``-<CSID>`` suffix.

    Returns:
        Absolute-or-relative path per ``$TMPDIR``; the file need not exist.

    Examples:
        >>> sentinel_path("dev-pytest-cmd").name.startswith("dev-pytest-cmd-")
        True
    """
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    return _sentinel_dir() / f"{name}-{_SAFE_NAME_RE.sub('', csid)}"


def _owned_by_caller(status: os.stat_result) -> bool:
    """Tell whether a file belongs to the current user; always ``True`` where the OS has no numeric user ids.

    Sentinel names are predictable and ``/tmp`` is shared on Linux, so another local user can create one first. An
    atomic write cannot replace a file another user owns in a sticky directory, which would leave that planted value in
    place for the next read. Native Windows has no ``os.getuid``, so the check degrades to accepting the file there,
    where the per-user temp directory already keeps other users out.

    Args:
        status: ``os.stat``/``os.fstat`` result of the opened file.

    Returns:
        ``True`` when the file is the caller's, or ownership cannot be checked on this platform.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryFile() as handle:
        ...     _owned_by_caller(os.fstat(handle.fileno()))
        True
    """
    getuid = getattr(os, "getuid", None)
    return getuid is None or status.st_uid == getuid()


def read_sentinel(name: str) -> str:
    """Return the first line of a sentinel (what ``IFS= read -r`` yields), or ``""`` when it cannot be trusted.

    A sentinel that is missing, unreadable, or owned by another user reads as ``""``: callers treat that the same as an
    unset value, so a file planted at the predictable name in a shared temp dir never feeds a command or a path.

    Args:
        name: Sentinel base name, without the ``-<CSID>`` suffix.

    Returns:
        First line without its line ending.
    """
    try:
        with sentinel_path(name).open(encoding="utf-8", errors="replace") as handle:
            # fstat the open handle, not the path, so a swap between the check and the read cannot slip past it
            if not _owned_by_caller(os.fstat(handle.fileno())):
                return ""
            return handle.readline().rstrip("\r\n")
    except OSError:
        return ""


def _refuse_foreign_destination(path: Path) -> None:
    """Raise when ``path`` already exists as an entry another user owns.

    ``lstat`` inspects the entry itself, so a symlink another user planted is refused as well, without following it. A
    missing entry is fine: the atomic write creates it as the caller's own.

    Args:
        path: Destination about to be replaced.

    Raises:
        PermissionError: When the existing entry belongs to another user.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     _refuse_foreign_destination(Path(d) / "absent")
    """
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return
    if not _owned_by_caller(status):
        raise PermissionError(
            errno.EPERM,
            "sentinel is owned by another user; remove it, or point TMPDIR at a directory only you can write",
            str(path),
        )


def make_private_dirs(path: Path) -> Path:
    """Create ``path`` and any missing parents, giving each directory this call creates mode 0700.

    Works like ``mkdir -p`` except for the mode: ``Path.mkdir(parents=True)`` creates parents with the default mode,
    which leaves new sentinel directories listable by other users. Directories that already exist are left exactly as
    they are, because the temp dir itself (``/tmp`` on Linux) belongs to root and must never be re-moded. The ``chmod``
    is best-effort: native Windows ignores POSIX mode bits, where the per-user temp dir already keeps other users out.

    Args:
        path: Directory to ensure.

    Returns:
        ``path``, for chaining.

    Raises:
        OSError: When a missing directory cannot be created, or a non-directory sits in the way.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     make_private_dirs(Path(d) / "a" / "b").is_dir()
        True
    """
    # Only the missing tail is created; walking existing ancestors would mkdir a filesystem root, which some
    # platforms answer with PermissionError rather than FileExistsError.
    missing = [directory for directory in (path, *path.parents) if not directory.exists()]
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue  # created concurrently by someone else; not this call's to re-mode
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)  # mkdir's mode is masked by the umask; restate it exactly
    if not path.is_dir():
        raise NotADirectoryError(errno.ENOTDIR, "expected a directory", str(path))
    return path


def write_atomic(path: Path, value: str) -> None:
    """Write ``value`` plus one newline to ``path`` atomically, never through a link planted at ``path``.

    The write goes to a private temp file beside ``path`` that is then moved over the final name, so the file is never
    half-written and a symlink pre-created at a predictable name is replaced rather than followed. An entry another
    user already owns at ``path`` is refused rather than raced: in a sticky temp dir the move would fail anyway, and
    elsewhere it would silently replace a file this user did not create. Bytes with a bare newline keep the file free
    of the CRLF a Windows text-mode write would add, which ``read -r`` would otherwise return as part of the value.

    Args:
        path: Destination file; its directory must already exist.
        value: Single-line value to record.

    Raises:
        PermissionError: When ``path`` exists and belongs to another user; nothing is written.
        OSError: When the temp file cannot be created or moved into place; no partial file is left behind.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as d:
        ...     target = Path(d) / "value"
        ...     write_atomic(target, "x")
        ...     target.read_bytes()
        b'x\\n'
    """
    _refuse_foreign_destination(path)
    fd, temporary_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(f"{value}\n".encode())
        os.replace(temporary_path, path)
    except BaseException:
        Path(temporary_path).unlink(missing_ok=True)
        raise


def write_sentinel(name: str, value: str) -> Path | None:
    """Write ``value`` plus one newline to a session-scoped sentinel, replacing whatever is there.

    Goes through :func:`write_atomic`, so the sentinel is never half-written and a pre-planted link at that predictable
    name is replaced rather than followed.

    Args:
        name: Sentinel base name, without the ``-<CSID>`` suffix.
        value: Single-line value to record.

    Returns:
        The sentinel path, or ``None`` when the write failed.
    """
    target = sentinel_path(name)
    try:
        write_atomic(target, value)
    except OSError:
        return None
    return target


def persist_dev_dir(skill: str, run_dir: Path) -> Path | None:
    """Write ``run_dir`` to the skill's ``dev-<skill>-dev-dir-<CSID>`` sentinel.

    Args:
        skill: Skill slug; characters outside ``[a-zA-Z0-9_-]`` are stripped.
        run_dir: Run directory path to record.

    Returns:
        The sentinel path, or ``None`` when the slug is empty or the write failed.

    Examples:
        >>> persist_dev_dir("///", Path(".developments/x")) is None
        True
    """
    slug = _SAFE_NAME_RE.sub("", skill)
    return write_sentinel(f"dev-{slug}-dev-dir", run_dir.as_posix()) if slug else None


def main(argv: list[str] | None = None) -> int:
    """Create a run directory and optional sentinel, then print its path.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        ``0`` always; argparse exits ``2`` on bad args.

    No doctest — creates directories and touches sentinel files; covered by pytest.
    """
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(
        prog="dev_run_dir.py",
        description="Create a timestamped .developments/ run directory, optionally with a sentinel.",
    )
    # nargs="?" so a bare ``--sentinel`` (no name) is accepted and simply skips the
    # sentinel touch — preserves the legacy bash contract where the name was optional.
    parser.add_argument("--sentinel", nargs="?", default="", help="Sentinel base name (sanitized to [a-zA-Z0-9_-]).")
    parser.add_argument(
        "--persist", metavar="SKILL", default="", help="Also write dev-<SKILL>-dev-dir-<CSID> sentinel."
    )
    args = parser.parse_args(argv)

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dir = Path(".developments") / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    if args.persist and persist_dev_dir(args.persist, run_dir) is None:
        print(f"dev_run_dir: could not persist dev-dir sentinel for {args.persist!r}", file=sys.stderr)

    if args.sentinel:
        sentinel_name = _SAFE_NAME_RE.sub("", args.sentinel)
        if sentinel_name:
            sentinel_path = _sentinel_dir() / f"{sentinel_name}-{ts}"
            try:
                fd, temporary_path = tempfile.mkstemp(prefix=".sentinel-", dir=sentinel_path.parent)
            except OSError:
                pass  # sentinel skipped — never abort the caller's always-exit-0 contract
            else:
                try:
                    os.close(fd)
                    # `link` atomically creates a new final entry, so an attacker link cannot be followed.
                    os.link(temporary_path, sentinel_path)
                except OSError:
                    pass  # sentinel skipped — never abort the caller's always-exit-0 contract
                finally:
                    try:
                        os.unlink(temporary_path)
                    except OSError:
                        pass  # best-effort cleanup preserves the caller's always-exit-0 contract

    sys.stdout.write(run_dir.as_posix() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
