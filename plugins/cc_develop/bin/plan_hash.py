#!/usr/bin/env python
"""Detect plan drift and validate challenge provenance for develop skills.

``.plans/`` is gitignored, so a plan rewritten mid-build leaves no diff. ``record`` stores the plan's SHA-256 in a
session sentinel; ``check`` re-hashes it and prints a ``! BLOCKED`` line when the plan changed or vanished. Detection
only: nothing is reverted and the exit status is always 0 so a mismatch never aborts the report.

Usage:
    python plan_hash.py record <plan-file>   # empty path clears the sentinel (no plan this run)
    python plan_hash.py check
    python plan_hash.py challenge-digest <plan-file>
    python plan_hash.py challenge-record <plan-file> --status passed --sha256 <reviewed-digest>
    python plan_hash.py challenge-check <plan-file>

Challenge provenance reserves a top-level ``Challenge:`` field and excludes that field from the reviewed digest.
Capture the digest before review, then record that same digest with the resulting status. Recording refuses a plan
changed during review. Only a ``passed`` status with a matching current digest permits reuse; editing the plan after
recording invalidates reuse. This field records the caller's verdict, not proof that an independent reviewer ran.

Output (stdout):
    ``check`` prints ``! BLOCKED — plan changed during build: <path>`` on mismatch, nothing otherwise.

Exit codes:
    0 — drift checks always; successful challenge operations
    1 — challenge metadata missing, nonpassing or stale; unreadable plan; review/record revision mismatch
    2 — bad arguments (argparse default)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from dev_run_dir import read_sentinel, sentinel_path, write_atomic

#: Separates the stored digest from the plan path inside the sentinel file.
_SEP = "  "
_CHALLENGE_FIELD = re.compile(rb"Challenge: (passed|blocked|skipped) sha256=([a-f0-9]{64})")


def _sentinel() -> Path:
    """Return the session sentinel path, ``$TMPDIR/dev-plan-sha-<session>``."""
    return sentinel_path("dev-plan-sha")


def _digest(path: Path) -> str | None:
    """Return the SHA-256 hex digest of ``path``, or ``None`` when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def record(plan: str) -> None:
    """Store the plan digest, or clear the sentinel when ``plan`` is empty or unreadable."""
    sentinel = _sentinel()
    digest = _digest(Path(plan)) if plan else None
    if digest is None:
        sentinel.unlink(missing_ok=True)
        return
    write_atomic(sentinel, f"{digest}{_SEP}{plan}")


def check() -> None:
    """Report when the plan recorded in an owned session sentinel changed or disappeared."""
    line = read_sentinel("dev-plan-sha")
    digest, _, plan = line.partition(_SEP)
    if digest and plan and _digest(Path(plan)) != digest:
        print(f"! BLOCKED — plan changed during build: {plan}")


def _challenge_revision(path: Path) -> tuple[bytes, bytes]:
    """Read the plan bytes excluding its reserved, top-level challenge metadata field."""
    lines = path.read_bytes().splitlines(keepends=True)
    fields = [line.rstrip(b"\r\n") for line in lines if line.startswith(b"Challenge:")]
    if len(fields) > 1:
        raise ValueError("multiple Challenge fields")
    body = b"".join(line for line in lines if not line.startswith(b"Challenge:"))
    return body, fields[0] if fields else b""


def challenge_command(args: argparse.Namespace) -> int:
    """Record or validate a challenge verdict against the exact revision supplied to its reviewer."""
    try:
        path = Path(args.plan)
        body, field = _challenge_revision(path)
        digest = hashlib.sha256(body).hexdigest()
        if args.cmd == "challenge-digest":
            print(digest)
            return 0
        if args.cmd == "challenge-record":
            if args.sha256 != digest:
                raise ValueError("plan changed since the reviewed digest was captured")
            field = f"Challenge: {args.status} sha256={digest}".encode("ascii")
            path.write_bytes(field + b"\n" + body)
            print(field.decode("ascii"))
            return 0
        match = _CHALLENGE_FIELD.fullmatch(field)
        if match is None or match[1] != b"passed" or match[2].decode("ascii") != digest:
            raise ValueError("challenge missing, nonpassing, malformed or stale")
        print(field.decode("ascii"))
        return 0
    except (OSError, ValueError) as exc:
        print(f"plan-hash: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Run drift detection or bind a challenge verdict to its reviewed plan revision."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("record").add_argument("plan", nargs="?", default="")
    sub.add_parser("check")
    sub.add_parser("challenge-digest").add_argument("plan")
    sub.add_parser("challenge-check").add_argument("plan")
    challenge_record = sub.add_parser("challenge-record")
    challenge_record.add_argument("plan")
    challenge_record.add_argument("--status", choices=("passed", "blocked", "skipped"), required=True)
    challenge_record.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    if args.cmd.startswith("challenge-"):
        return challenge_command(args)
    if args.cmd == "record":
        record(args.plan)
    else:
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
