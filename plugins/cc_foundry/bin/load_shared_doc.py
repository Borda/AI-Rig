#!/usr/bin/env python3
"""load_shared_doc.py — resolve a plugin's shared-doc dir and emit one file from it.

Collapses the two-line skill idiom

.. code-block:: bash

    _FS=$(python "$ROOT/bin/resolve_shared_path.py" foundry skills/_shared 2>/dev/null \
        || echo "plugins/cc_foundry/skills/_shared")
    cat "$_FS/file-handoff-protocol.md"

into a single call. Resolution is **not** reimplemented here — the cascade is
imported from :mod:`resolve_shared_path` so the two can never drift. That
cascade already ends in a source-tree tier, which is what the inline ``|| echo``
fallback provided; its "using source-tree fallback" stderr note is suppressed
here because every call site passed ``2>/dev/null``.

Dropping the ``$(...)`` capture is also why this is preferred inside spawned-agent
prompts: command substitution triggers a "Contains expansion" permission prompt,
a bare ``python ...`` invocation does not.

Failure behaviour: a ``! BREAKING`` line on stdout and exit 1 — where the inline
form leaked a raw ``cat: No such file or directory`` to stderr with the same
non-zero exit.

Usage:
    python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_foundry}/bin/load_shared_doc.py" <plugin> <subdir> <file.md>

Arguments:
    plugin  Plugin short-name (e.g. ``foundry``, ``oss``) — no ``cc_`` prefix.
    subdir  Subdir under the plugin install (e.g. ``skills/_shared``).
    file    File name inside that subdirectory (plain name; no path separators).

Exit codes:
    0  Success — file content written to stdout
    1  Subdirectory or file not found
    2  Argument error
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from load_mode import validate_file_name  # noqa: E402
from resolve_shared_path import _validate_plugin, _validate_subdir  # noqa: E402
from resolve_shared_path import resolve as resolve_shared  # noqa: E402

_NOT_FOUND_TIER = -1


def locate(plugin: str, subdir: str) -> Path | None:
    """Resolve ``<plugin>/<subdir>`` through the shared-path cascade.

    Args:
        plugin: Plugin short-name (already validated).
        subdir: Subdir under the plugin install.

    Returns:
        The resolved directory, or ``None`` when even the source-tree tier misses.

    Examples:
        >>> locate("__nonexistent__", "skills/_shared") is None
        True
    """
    path, tier = resolve_shared(plugin, subdir)
    return None if tier == _NOT_FOUND_TIER else Path(path)


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser.

    Returns:
        Configured :class:`argparse.ArgumentParser`.

    Examples:
        >>> _build_parser().parse_args(["foundry", "skills/_shared", "file-handoff-protocol.md"]).plugin
        'foundry'
    """
    parser = argparse.ArgumentParser(
        prog="load_shared_doc",
        description="Resolve a plugin's shared-doc dir and emit one file from it.",
    )
    parser.add_argument("plugin", help="Plugin short-name (e.g. foundry, oss).")
    parser.add_argument("subdir", help="Subdir under the plugin install (e.g. skills/_shared).")
    parser.add_argument("file", help="File name inside that subdirectory (plain name).")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 success, 1 not found, 2 argument error).
    """
    args = _build_parser().parse_args(argv)

    try:
        _validate_plugin(args.plugin)
        _validate_subdir(args.subdir)
        validate_file_name(args.file)
    except ValueError as exc:
        print(f"load_shared_doc: {exc}", file=sys.stderr)
        return 2

    resolved = locate(args.plugin, args.subdir)
    if resolved is None:
        print(f"! BREAKING: {args.plugin}/{args.subdir} not found — re-install the {args.plugin} plugin")
        return 1

    target = resolved / args.file
    if not target.is_file():
        print(f"! BREAKING: {args.file} not found in {target.parent.as_posix()} — re-install the {args.plugin} plugin")
        return 1

    # Byte-for-byte passthrough — matches `cat`, no encoding or newline rewriting.
    sys.stdout.buffer.write(target.read_bytes())
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
