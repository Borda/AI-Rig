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

Every appended chunk ends with exactly one newline it would otherwise lack: a record the
Write tool saved without a trailing newline would otherwise glue the next record onto it.

Usage: python append_ledger.py <ledger> [--from FILE ...]
Exit codes: 0 = appended (or nothing staged) · 1 = ledger or source unreadable/unwritable · 2 = argument error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def staged_records(ledger: Path) -> list[Path]:
    """List the staged ``.rec`` files waiting to be appended to ``ledger``.

    Args:
        ledger: Path of the ledger file.

    Returns:
        ``<ledger>.rec`` first when present, then ``<ledger>.<writer>.rec`` files sorted by name.

    Examples:
        >>> staged_records(Path("no-such-dir") / "log.jsonl")
        []
    """
    single = ledger.with_name(ledger.name + ".rec")
    parallel = sorted(ledger.parent.glob(ledger.name + ".*.rec")) if ledger.parent.is_dir() else []
    return ([single] if single.is_file() else []) + [path for path in parallel if path.is_file()]


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


def main(argv: list[str] | None = None) -> int:
    """Append staged records (or ``--from`` files) to the ledger, consuming staged files."""
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    ledger = Path(args.ledger)
    sources = [Path(raw) for raw in args.sources]
    for missing in [path for path in sources if not path.is_file()]:
        print(f"append_ledger: skipped missing source {missing.as_posix()}", file=sys.stderr)
    sources = [path for path in sources if path.is_file()]
    staged = [] if args.sources else staged_records(ledger)
    inputs = sources or staged
    if not inputs:
        print(f"append_ledger: nothing staged for {ledger.as_posix()}")
        return 0
    try:
        chunks = [terminated(path.read_bytes()) for path in inputs]
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("ab") as handle:
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
