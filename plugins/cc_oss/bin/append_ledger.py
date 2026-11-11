#!/usr/bin/env python
"""append_ledger.py — append staged records to a growing ledger without ever rewriting it.

A ledger (``.jsonl`` log, diary, journal, results file) only grows. A skill stages each
model-valued record as ``<ledger>.rec`` with the Write tool, then runs this script with the
ledger path as fixed command text. The script appends every staged file to the ledger and
deletes it, so the next Write creates a fresh ``.rec`` instead of overwriting one the
harness has not read.

Why a script and not ``cat "$L.rec" >> "$L" && rm -f "$L.rec"``: ``rm`` has no plugin
allow entry beyond ``.temp/state/*`` and the blueprint manifest drops any block that runs
it, so the shell form prompts on every append. ``python`` is allowed in every plugin.

Staged sources, consumed in sorted order:
  ``<ledger>.rec``          one writer
  ``<ledger>.<writer>.rec`` parallel writers, one file each, swept in a single call

``--from FILE...`` appends existing files (e.g. per-target result files already on disk)
and leaves them in place. Missing ``--from`` paths are reported and skipped, so an
unmatched shell glob does not abort the append.

Ledger parents must already exist. Ledgers stay under the project's ``.reports``,
``.temp``, ``.notes``, ``.experiments`` or ``.developments`` directories, or in system
temp storage. Linked ledger and source paths are refused.

Every appended chunk ends with exactly one newline it would otherwise lack: a record the
Write tool saved without a trailing newline would otherwise glue the next record onto it.

Usage: python append_ledger.py <ledger> [--from FILE ...]
Exit codes: 0 = appended (or nothing staged) · 1 = ledger or source unreadable/unwritable · 2 = argument error
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import tempfile
from pathlib import Path


def staged_records(ledger: Path) -> list[Path]:
    """List the staged ``.rec`` files waiting to be appended to ``ledger``.

    The ledger name is matched literally, so ``[``, ``]`` or ``*`` in it cannot widen the match onto another ledger's
    staged files. A symlinked ``.rec`` is skipped: it would copy its target's content into the ledger.

    Args:
        ledger: Path of the ledger file.

    Returns:
        ``<ledger>.rec`` first when present, then ``<ledger>.<writer>.rec`` files sorted by name.

    Examples:
        >>> staged_records(Path("no-such-dir") / "log.jsonl")
        []
    """
    single = ledger.with_name(ledger.name + ".rec")
    parallel = sorted(ledger.parent.glob(glob.escape(ledger.name) + ".*.rec")) if ledger.parent.is_dir() else []
    return [path for path in [single, *parallel] if path.is_file() and not path.is_symlink()]


def terminated(chunk: bytes) -> bytes:
    """Return ``chunk`` ending in a newline, adding one only when it is missing.

    Args:
        chunk: Raw bytes of one staged or source file.

    Returns:
        ``chunk`` unchanged when empty or already newline-terminated, else ``chunk + b"\\n"``.

    Examples:
        >>> terminated(b'{"a":1}')
        b'{"a":1}\\n'
        >>> terminated(b'{"a":1}\\n')
        b'{"a":1}\\n'
        >>> terminated(b"")
        b''
    """
    return chunk if not chunk or chunk.endswith(b"\n") else chunk + b"\n"


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse the ledger path and optional ``--from`` sources."""
    parser = argparse.ArgumentParser(
        prog="append_ledger.py", description="Append staged .rec files (or --from files) to a growing ledger."
    )
    parser.add_argument("ledger", help="ledger file to append to; created when absent")
    parser.add_argument(
        "--from", dest="sources", nargs="+", default=[], metavar="FILE", help="existing files to append, kept in place"
    )
    return parser.parse_args(argv)


def _refuse_linked_path(path: Path) -> None:
    """Reject links along a ledger or source path below the project or system temp directory."""
    trusted = {Path.cwd(), Path(tempfile.gettempdir()).absolute()}
    current = path.absolute()
    while current not in trusted:
        if current.is_symlink():
            raise OSError(f"refusing symlinked path: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _read_source(path: Path) -> bytes:
    """Read one regular source without following a source or parent symlink."""
    _refuse_linked_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        return terminated(handle.read())


def _validate_paths(ledger: Path, sources: list[Path]) -> None:
    """Keep ledger writes in project artifact directories or external temp storage and refuse linked paths."""
    _refuse_linked_path(ledger)
    target = ledger.resolve()
    artifact_roots = (".reports", ".temp", ".notes", ".experiments", ".developments")
    allowed = any(parent.name in artifact_roots for parent in target.parents)
    allowed = allowed or target.is_relative_to(Path(tempfile.gettempdir()).resolve())
    if not allowed:
        raise OSError(f"ledger is outside project artifact directories and system temp storage: {ledger}")
    for source in sources:
        _refuse_linked_path(source)


def main(argv: list[str] | None = None) -> int:
    """Append staged records (or ``--from`` files) to the ledger, consuming staged files."""
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    ledger = Path(args.ledger)
    sources = [Path(raw) for raw in args.sources]
    try:
        _validate_paths(ledger, sources)
    except OSError as error:
        print(f"append_ledger: {error}", file=sys.stderr)
        return 1
    for missing in [path for path in sources if not path.is_file()]:
        print(f"append_ledger: skipped missing source {missing.as_posix()}", file=sys.stderr)
    sources = [path for path in sources if path.is_file()]
    staged = [] if args.sources else staged_records(ledger)
    inputs = sources or staged
    if not inputs:
        print(f"append_ledger: nothing staged for {ledger.as_posix()}")
        return 0
    try:
        chunks = [_read_source(path) for path in inputs]
        fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(b"".join(chunks))
        for path in staged:
            path.unlink()
    except OSError as error:
        print(f"append_ledger: {error}", file=sys.stderr)
        return 1
    print(f"append_ledger: appended {len(inputs)} file(s) to {ledger.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
