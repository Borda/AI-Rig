#!/usr/bin/env python
"""Append staged records to a growing ledger without ever rewriting it.

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

Ledger parents must already exist. Without ``--run-root``, ledger and source paths
are authorized only under ``.temp``, ``.reports``, ``.experiments``, ``.developments``
and ``.notes`` directly under the project root. The nearest ancestor of the ledger
containing ``.git`` is the project root, falling back to the current working directory.
Linked paths and parent traversal are refused. Optional ``--run-root`` replaces
these defaults with an existing run directory. Explicit ``--history-root`` directories
authorize additional cross-run ledger destinations, while ``--source-root`` directories
authorize additional read-only migration sources. Explicitly authorized ledgers must
still be in artifact directories or system temp storage.
The combined source input is limited to 64 MiB before any append or staged deletion.

Every appended chunk ends with exactly one newline it would otherwise lack: a record the
Write tool saved without a trailing newline would otherwise glue the next record onto it.

Usage: python append_ledger.py <ledger> [--run-root DIR] [--history-root DIR] [--source-root DIR] [--from FILE ...]
Exit codes: 0 = appended (or nothing staged) · 1 = ledger or source unreadable/unwritable · 2 = argument error
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import tempfile
from pathlib import Path

MAX_INPUT_BYTES = 64 * 1024 * 1024
ARTIFACT_ROOTS = (".temp", ".reports", ".experiments", ".developments", ".notes")


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
        "--run-root", type=Path, help="existing run directory replacing the default project artifact roots"
    )
    parser.add_argument(
        "--history-root", action="append", type=Path, default=[], help="additional ledger destination root (repeatable)"
    )
    parser.add_argument(
        "--source-root", action="append", type=Path, default=[], help="additional read-only source root (repeatable)"
    )
    parser.add_argument(
        "--from", dest="sources", nargs="+", default=[], metavar="FILE", help="existing files to append, kept in place"
    )
    return parser.parse_args(argv)


def _refuse_linked_path(path: Path) -> None:
    """Reject parent traversal and links below the project or system temp directory."""
    if ".." in path.parts:
        raise OSError(f"refusing parent traversal in path: {path}")
    trusted = {Path.cwd(), Path(tempfile.gettempdir()).absolute()}
    current = path.absolute()
    while current not in trusted:
        if current.is_symlink():
            raise OSError(f"refusing symlinked path: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent


def _read_source(path: Path, limit: int) -> bytes:
    """Read a source within the remaining byte budget without following links."""
    _refuse_linked_path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as handle:
        chunk = handle.read(limit + 1)
    if len(chunk) > limit:
        raise OSError(f"E_INPUT_TOO_LARGE: source inputs exceed {MAX_INPUT_BYTES} bytes: {path}")
    return chunk


def _default_artifact_roots(ledger: Path) -> list[Path]:
    """Authorize only direct artifact directories under the ledger's project root or cwd."""
    project = next((parent for parent in ledger.absolute().parents if (parent / ".git").exists()), Path.cwd())
    roots = [project / name for name in ARTIFACT_ROOTS]
    return [root for root in roots if root.is_dir() and not root.is_symlink()]


def _validate_paths(
    ledger: Path, sources: list[Path], run_root: Path | None, history_roots: list[Path], source_roots: list[Path]
) -> None:
    """Confine ledger writes and source reads to default artifact roots or explicit authorization."""
    explicit_run_roots = [] if run_root is None else [run_root]
    for root in [*explicit_run_roots, *history_roots, *source_roots]:
        _refuse_linked_path(root)
        if not root.is_dir():
            raise OSError(f"authorization root is not an existing directory: {root}")
    _refuse_linked_path(ledger)
    run_roots = explicit_run_roots if run_root is not None else _default_artifact_roots(ledger)
    target = ledger.resolve()
    if not any(target.is_relative_to(root.resolve()) for root in [*run_roots, *history_roots]):
        raise OSError(f"ledger outside authorized roots: {ledger}")
    allowed = any(parent.name in ARTIFACT_ROOTS for parent in target.parents)
    allowed = allowed or target.is_relative_to(Path(tempfile.gettempdir()).resolve())
    if not allowed:
        raise OSError(f"ledger is outside project artifact directories and system temp storage: {ledger}")
    for source in sources:
        _refuse_linked_path(source)
        if not any(source.resolve().is_relative_to(root.resolve()) for root in [*run_roots, *source_roots]):
            raise OSError(f"source outside authorized roots: {source}")


def main(argv: list[str] | None = None) -> int:
    """Append staged records (or ``--from`` files) to the ledger, consuming staged files."""
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    ledger = Path(args.ledger)
    sources = [Path(raw) for raw in args.sources]
    try:
        _validate_paths(ledger, sources, args.run_root, args.history_root, args.source_root)
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
        _validate_paths(ledger, staged, args.run_root, args.history_root, args.source_root)
        chunks = []
        remaining = MAX_INPUT_BYTES
        for path in inputs:
            chunk = _read_source(path, remaining)
            remaining -= len(chunk)
            chunks.append(terminated(chunk))
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
