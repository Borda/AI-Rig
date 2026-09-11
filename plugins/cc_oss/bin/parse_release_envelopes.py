#!/usr/bin/env python
"""parse_release_envelopes.py — validate the changelog-audit and contributors agent envelopes for oss:release.

Both delegated agents return a compact JSON envelope. This script fails the run when either reports a non-``done``
status or names a file that was never written, then re-persists the returned paths — a subagent may canonicalize them,
so the sentinels must carry the agent's own spelling, not the orchestrator's guess.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/parse_release_envelopes.py" --envelope-a "$ENVELOPE_A" --envelope-b "$ENVELOPE_B"

Sentinels written to ``${TMPDIR:-/tmp}/<name>-${CSID}``:
    release-changelog-audit, release-contributors, release-changelog-file (only when envelope A carries one)

Exit codes:
    0 — both envelopes valid; summary printed
    1 — either delegation failed or named a missing file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def _sentinel_path(name: str) -> Path:
    """Build the session-scoped sentinel path for ``name``.

    Args:
        name: Sentinel base name, without the trailing session token.

    Returns:
        Path of the form ``<tmpdir>/<name>-<csid>``.

    Examples:
        >>> _sentinel_path("release-contributors").name.startswith("release-contributors-")
        True
    """
    csid = os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"
    tmpdir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    return Path(tmpdir) / f"{name}-{csid}"


#: Set by ``--dry-run``. Suppresses every sentinel write for the process.
_DRY_RUN = False


def _set_dry_run(enabled: bool) -> None:
    """Enable or disable dry-run mode for this process."""
    global _DRY_RUN  # noqa: PLW0603 — one process-wide switch, set once from argv
    _DRY_RUN = enabled


def _write_sentinel(name: str, value: str) -> None:
    """Write ``value`` plus a trailing newline to the sentinel named ``name``.

    Readers use ``IFS= read -r VAR < file``, which exits non-zero on a file with no final newline and silently falls
    back to its default; ``newline="\\n"`` stops Windows from appending a carriage return inside the value.


    Sentinels are live session state, not scratch output: they are named for the current ``CSID`` and are what the
    skill's later steps and its PreToolUse hooks read. Running this script by hand to inspect its output therefore
    forges state for whatever session is running — one observed case wrote an ``analyse-report-file`` naming a report
    that was never produced, and the resulting hook denial blocked an unrelated question. Pass ``--dry-run`` for any
    invocation that is not a real skill step.

    Args:
        name: Sentinel base name, without the trailing session token.
        value: Payload to persist.
    """
    if _DRY_RUN:
        print(f"[dry-run] would write {name}={value}")
        return
    _sentinel_path(name).write_text(f"{value}\n", encoding="utf-8", newline="\n")


def load_envelope(raw: str) -> dict | None:
    """Parse one agent envelope.

    Args:
        raw: Raw envelope text as returned by the agent.

    Returns:
        The decoded object, or ``None`` when the text is not a JSON object.

    Examples:
        >>> load_envelope('{"status": "done"}')
        {'status': 'done'}
        >>> load_envelope("not json") is None
        True
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def field(data: dict | None, key: str, absent: str = "null") -> str:
    """Read one envelope field the way ``jq -r`` renders it.

    An unparsable envelope yields an empty string (``jq`` writes nothing to stdout); a parsed envelope missing the key
    yields ``absent`` — ``null`` for a bare ``.key`` lookup, or the alternative operand for ``.key // X``.

    Args:
        data: Decoded envelope, or ``None``.
        key: Field name to read.
        absent: Value to return when the field is missing or null.

    Returns:
        The field rendered as a string.

    Examples:
        >>> field({"status": "done"}, "status")
        'done'
        >>> field({}, "file")
        'null'
        >>> field({}, "added", "0")
        '0'
        >>> field(None, "status")
        ''
    """
    if data is None:
        return ""
    value = data.get(key)
    return absent if value is None else str(value)


def validate(data: dict | None, label: str) -> str:
    """Check one envelope's status and the existence of the file it names.

    Args:
        data: Decoded envelope, or ``None``.
        label: Delegation name used in the failure message.

    Returns:
        The validated file path.

    Raises:
        SystemExit: exit code 1 when the status is not ``done`` or the file is absent.
    """
    status = field(data, "status")
    path = field(data, "file")
    if status != "done" or not path or not Path(path).is_file():
        print(f"Error: {label} delegation failed — status={status}, file={path}", file=sys.stderr)
        raise SystemExit(1)
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list override for testing. Defaults to ``sys.argv[1:]``.

    Returns:
        Exit code 0 when both envelopes validate; raises ``SystemExit(1)`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="parse_release_envelopes.py",
        description="Validate the changelog-audit and contributors agent envelopes for oss:release.",
    )
    parser.add_argument("--envelope-a", default="", help="Changelog-audit agent envelope (JSON).")
    parser.add_argument("--envelope-b", default="", help="Contributors agent envelope (JSON).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print, but write no sentinels — use for any run that is not a real skill step.",
    )
    args = parser.parse_args(argv)
    _set_dry_run(args.dry_run)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]

    envelope_a = load_envelope(args.envelope_a)
    envelope_b = load_envelope(args.envelope_b)
    changelog_audit_file = validate(envelope_a, "changelog-audit")
    contributors_file = validate(envelope_b, "contributors")

    _write_sentinel("release-changelog-audit", changelog_audit_file)
    _write_sentinel("release-contributors", contributors_file)
    changelog_file = field(envelope_a, "changelog_file", "")
    if changelog_file:
        _write_sentinel("release-changelog-file", changelog_file)

    added = field(envelope_a, "added", "0")
    flagged = field(envelope_a, "flagged", "0")
    scope_flagged = field(envelope_a, "scope_flagged", "0")
    count = field(envelope_b, "count", "0")
    print(
        f"Phases 5–6 delegated: {added} changelog entries added, {flagged} flagged, "
        f"{scope_flagged} scope-flagged (non-PR branch merge); {count} contributors extracted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
