#!/usr/bin/env python
"""parse_release_envelopes.py — validate the changelog-audit and contributors agent envelopes for oss:release.

Both delegated agents return a compact JSON envelope. This script fails the run when either reports a non-``done``
status, names a file that was never written, or names a file outside the working root, then re-persists the returned
paths — a subagent may canonicalize them, so the sentinels must carry the agent's own spelling, not the orchestrator's
guess.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/parse_release_envelopes.py" --envelope-a "$ENVELOPE_A" --envelope-b "$ENVELOPE_B"

Sentinels written to ``${TMPDIR:-/tmp}/<name>-${CSID}``:
    release-changelog-audit, release-contributors, release-changelog-file (only when envelope A carries one that
    resolves inside the working root and names a CHANGELOG file; otherwise dropped with a warning and the stale
    sentinel removed)

Exit codes:
    0 — both envelopes valid; summary printed. A rejected ``changelog_file`` degrades here, not to 1: it is optional
        and ``modes/prepare.md`` re-derives it, so the run continues with a warning on stdout.
    1 — either delegation failed, named a missing file, or named a file that resolves outside the working root
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def _within_root(path: Path, root: Path) -> bool:
    """Return ``True`` when ``path`` resolves at or under ``root``.

    ``root`` is resolved here rather than at the call site so a symlinked cwd cannot cause a
    spurious rejection.

    The allowed root is deliberately narrower than ``setup_release_dir.py``'s three-root allowlist
    (cwd, ``~/.claude``, the temp dir): that validator gates orchestrator-authored argv, this one
    gates delegate-authored envelope fields from an agent that parses untrusted commit subjects by
    contract. Admitting ``~/.claude`` here would let a compromised delegate name a config file that
    is later symlinked into a public release directory. The sibling's temp-dir root is a pytest
    ``tmp_path`` affordance and has no production caller here.

    Args:
        path: Candidate path.
        root: Directory the path must lie within.

    Returns:
        ``True`` when *path* is *root* or a descendant of it, or when either cannot be resolved
        (OSError) it returns ``False`` — fail closed.

    Examples:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     root = Path(tmp)
        ...     _within_root(root / "a" / "b.md", root)
        True
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     root = Path(tmp) / "work"
        ...     _within_root(Path(tmp) / "other" / "b.md", root)
        False
    """
    if ".." in path.parts:
        return False
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except OSError:
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


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


def validate(data: dict | None, label: str, root: Path) -> str:
    """Check one envelope's status, file existence, and containment within *root*.

    Args:
        data: Decoded envelope, or ``None``.
        label: Delegation name used in the failure message.
        root: Directory the named file must resolve inside — the delegate must not name a file
            elsewhere on disk (see :func:`_within_root`).

    Returns:
        The validated file path.

    Raises:
        SystemExit: exit code 1 when the status is not ``done``, the file is absent, or the file
            resolves outside *root*.
    """
    status = field(data, "status")
    path = field(data, "file")
    if status != "done" or not path or not Path(path).is_file() or not _within_root(Path(path), root):
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
    root = Path.cwd()
    changelog_audit_file = validate(envelope_a, "changelog-audit", root)
    contributors_file = validate(envelope_b, "contributors", root)

    _write_sentinel("release-changelog-audit", changelog_audit_file)
    _write_sentinel("release-contributors", contributors_file)
    changelog_file = field(envelope_a, "changelog_file", "")
    changelog_file_ok = (
        bool(changelog_file)
        and _within_root(Path(changelog_file), root)
        and Path(changelog_file).name.upper().startswith("CHANGELOG")
    )
    if changelog_file_ok:
        _write_sentinel("release-changelog-file", changelog_file)
    else:
        if _DRY_RUN:
            print("[dry-run] would remove release-changelog-file")
        else:
            _sentinel_path("release-changelog-file").unlink(missing_ok=True)
        if changelog_file:
            print(
                f"[release] ⚠ changelog_file rejected (outside {root} or not a CHANGELOG file): "
                f"{changelog_file} — sentinel not written, falling back to CHANGELOG search"
            )

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
