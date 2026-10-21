#!/usr/bin/env python
"""dev_load_shared.py — print one document from the develop plugin's own ``skills/_shared`` directory.

Collapses the skill idiom

.. code-block:: bash

    export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
    IFS= read -r _DEV_SHARED < "${TMPDIR:-/tmp}/dev-shared-${CSID}" 2>/dev/null || _DEV_SHARED=""
    [ -z "$_DEV_SHARED" ] && _DEV_SHARED="plugins/cc_develop/skills/_shared"
    cat "$_DEV_SHARED/plan-inline.md"

into the ``CSID`` export plus one call. It is the plugin-owned loader ``plugins/CLAUDE.md`` §Loading Shared Docs accepts
as an equivalent of resolve + ``cat``: one Bash call, command-matched by ``Bash(python:*)``, never the Read tool.

Resolution mirrors the idiom, so a migrated block reads the same directory as one that still uses ``$_DEV_SHARED``:

* **Warm** — the ``dev-shared-<CSID>`` sentinel ``dev_shared_resolve.py --init`` wrote at skill start, read through
  ``dev_run_dir.read_sentinel`` (same name derivation and ownership check as the writer). Used when it names an existing
  directory.
* **Cold** — ``dev_shared_resolve.resolve_shared_path()``: the newest non-orphaned cached develop version, then the
  source-tree ``plugins/cc_develop/skills/_shared``. Reached when the sentinel is missing, untrusted, or stale. The
  source tree is relative to the working directory, so it is used only inside the develop source repository
  (``dev_shared_resolve.source_repo_marker_present``); anywhere else the load prints one ``! BREAKING`` line and exits
  1, with or without ``--optional``, instead of printing another repository's text as protocol.

The sentinel name carries ``$CSID``, else ``$CLAUDE_CODE_SESSION_ID``, else ``shared``; the cold start runs with
``CSID`` exported, so the calling block exports it too.

Output: the document's bytes on stdout, unchanged (same as ``cat``). A missing document prints one
``! BREAKING`` line and exits 1; with ``--optional`` it prints one ``⚠ WARNING … — skipping`` line and exits 0, for a
step the caller already skips when its document is absent. A document name that is not a plain file name exits 2.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_develop}/bin/dev_load_shared.py" [--optional] <file.md>

<!-- file: dev_load_shared.py — consumers: develop skills that load a skills/_shared document -->
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dev_run_dir import read_sentinel  # sibling bin/ script; session-scoped, ownership-checked sentinel reads
from dev_shared_resolve import (  # sibling bin/ script; cold resolution of this plugin's _shared
    SOURCE_FALLBACK,
    resolve_shared_path,
    source_repo_marker_present,
)

#: Sentinel base name ``dev_shared_resolve.py --init`` writes the resolved ``skills/_shared`` path under.
_SHARED_SENTINEL = "dev-shared"
#: Remediation shown when a required document is missing: every loaded document ships inside this plugin.
_REINSTALL_HINT = "reinstall develop@borda-ai-rig"


class UntrustedSharedDirError(Exception):
    """Only a working-directory source tree resolved, and the working directory is not the develop source repo."""


def validate_doc_name(name: str) -> None:
    """Reject anything but a plain file name, so the loaded file always sits directly in the shared directory.

    Args:
        name: Document name supplied on the command line.

    Raises:
        ValueError: When ``name`` is empty, contains a path separator, or is a traversal segment.

    Examples:
        >>> validate_doc_name("plan-inline.md")
        >>> validate_doc_name("../SKILL.md")
        Traceback (most recent call last):
            ...
        ValueError: document '../SKILL.md' must be a plain file name (no path separators)
        >>> validate_doc_name("")
        Traceback (most recent call last):
            ...
        ValueError: document name must not be empty
    """
    if not name:
        raise ValueError("document name must not be empty")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"document {name!r} must be a plain file name (no path separators)")


def shared_dir() -> Path:
    """Return the ``skills/_shared`` directory this run loads from: the warm sentinel when usable, else a cold resolve.

    Returns:
        Directory path; relative for the source-tree fallback, exactly as the shell idiom used it.

    Raises:
        UntrustedSharedDirError: When the cold resolve falls back to the working-directory source tree and the working
            directory is not the develop source repository.

    Examples:
        >>> import os, tempfile
        >>> from unittest import mock
        >>> with tempfile.TemporaryDirectory() as scratch:
        ...     _ = (Path(scratch) / "dev-shared-doc").write_text(scratch + "\\n", encoding="utf-8")
        ...     with mock.patch.dict(os.environ, {"TMPDIR": scratch, "CSID": "doc"}):
        ...         shared_dir() == Path(scratch)
        True
    """
    warm = read_sentinel(_SHARED_SENTINEL)
    if warm and Path(warm).is_dir():
        return Path(warm)
    cold = resolve_shared_path()
    # The cold resolver returns the cwd source tree unchecked; outside the source repo it is another repository's text.
    if cold == SOURCE_FALLBACK and not source_repo_marker_present():
        raise UntrustedSharedDirError(
            "no installed develop plugin, and the working directory is not the develop plugin source repository "
            f"(no plugins/cc_develop/.claude-plugin/plugin.json naming develop) — refusing to read {cold}"
        )
    return Path(cold)


def _emit(line: str) -> None:
    """Write one status line as UTF-8 bytes, so the ``⚠`` marker survives a non-UTF-8 Windows console encoding."""
    sys.stdout.buffer.write(f"{line}\n".encode())
    sys.stdout.buffer.flush()


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Returns:
        Configured :class:`argparse.ArgumentParser`.

    Examples:
        >>> _build_parser().parse_args(["--optional", "codex-prepass.md"]).optional
        True
    """
    parser = argparse.ArgumentParser(
        prog="dev_load_shared",
        description="Print one document from the develop plugin's own skills/_shared directory.",
    )
    parser.add_argument("doc", help="Document file name inside skills/_shared (plain name, e.g. plan-inline.md).")
    parser.add_argument(
        "--optional",
        action="store_true",
        help="A missing document prints one warning line and exits 0 instead of failing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 printed (or optional document skipped), 1 document missing or shared directory untrusted,
        2 invalid document name.
    """
    args = _build_parser().parse_args(argv)
    try:
        validate_doc_name(args.doc)
    except ValueError as exc:
        print(f"dev_load_shared: {exc}", file=sys.stderr)
        return 2

    try:
        directory = shared_dir()
    except UntrustedSharedDirError as exc:
        _emit(f"! BREAKING: {exc} — {_REINSTALL_HINT}")
        return 1
    target = directory / args.doc
    try:
        # Read directly rather than checking first: a directory or an unreadable file fails here the same way.
        content = target.read_bytes()
    except OSError:
        if args.optional:
            _emit(f"⚠ WARNING: {args.doc} not found in {target.parent.as_posix()} — skipping")
            return 0
        _emit(f"! BREAKING: {args.doc} not found in {target.parent.as_posix()} — {_REINSTALL_HINT}")
        return 1

    # Byte-for-byte passthrough — matches `cat`, no encoding or newline rewriting.
    sys.stdout.buffer.write(content)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
