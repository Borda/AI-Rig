#!/usr/bin/env python
"""remove_hook_from_registry.py — strip a named hook from a Claude Code hooks registry.

Reads a JSON file that has a top-level ``hooks`` object whose values are arrays
of hook-group entries, each containing a ``hooks`` array of command objects.
Drops every command whose ``command`` field matches a caller-provided regex
substring, then prunes any group or top-level event that becomes empty.

Used by ``/foundry:manage delete hook <name>`` to remove an entry from both
``.claude/settings.json`` and the plugin's ``.claude-plugin/hooks.json``.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/remove_hook_from_registry.py" \\
        --json-file <path> \\
        --hook-name <name> \\
        [--match path|basename]

The default match builds a safe regex from ``--hook-name`` (escaped via ``re.escape``)
per ``--match``: ``path`` -> ``\\.claude/hooks/<name>\\.js`` (default), ``basename`` ->
``<name>\\.js``. ``--path-pattern`` is an escape hatch — a raw Python regex
(``re.search``-style, matched case-insensitively), used verbatim instead of the
``--match`` form when supplied. A hand-built pattern is the caller's own responsibility
to escape: an unescaped metacharacter (e.g. a literal ``.``) over-matches, and this tool
rewrites the target file in place. Typical usage:

    --hook-name rtk-rewrite --match path

Atomic write — writes to ``<path>.tmp`` then renames over the target. On any
failure the temp file is removed and the original file is left untouched.

Exit codes:
    0  Success (file rewritten, or target was already clean)
    1  Target file missing
    2  Invalid JSON in target file
    3  Bad CLI args (missing required flag, malformed regex)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any


def filter_hooks(
    registry: dict[str, Any],
    pattern: re.Pattern[str],
) -> dict[str, Any]:
    """Return a copy of ``registry`` with every matching hook command removed.

    The shape mirrors Claude Code's hooks JSON: the top-level ``hooks`` key
    maps event names (``PreToolUse``, ``PostToolUse``, …) to arrays of group
    objects. Each group has its own inner ``hooks`` array of command objects
    with a ``command`` field. Any command whose ``command`` field matches
    ``pattern`` is dropped. A group with no remaining commands is dropped, and
    an event with no remaining groups is dropped.

    Args:
        registry: Parsed JSON object — typically the full ``settings.json``.
        pattern: Compiled regex applied via ``pattern.search`` to each
            ``command`` string.

    Returns:
        A new dict with the same top-level keys as ``registry``, but with the
        ``hooks`` sub-tree filtered. Non-``hooks`` keys are passed through
        unchanged. ``hooks`` is always present in the output (possibly empty).

    Examples:
        >>> import re
        >>> reg = {
        ...     "hooks": {
        ...         "PreToolUse": [
        ...             {"matcher": "*", "hooks": [
        ...                 {"command": ".claude/hooks/foo.js"},
        ...                 {"command": ".claude/hooks/bar.js"},
        ...             ]},
        ...         ],
        ...     },
        ... }
        >>> out = filter_hooks(reg, re.compile(r"foo\\.js", re.IGNORECASE))
        >>> out["hooks"]["PreToolUse"][0]["hooks"]
        [{'command': '.claude/hooks/bar.js'}]
    """
    out = dict(registry)
    hooks_section = out.get("hooks") or {}
    new_hooks: dict[str, list[dict[str, Any]]] = {}
    for event, groups in hooks_section.items():
        if not isinstance(groups, list):
            new_hooks[event] = groups
            continue
        kept_groups: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept_commands = [
                cmd for cmd in inner if not (isinstance(cmd, dict) and pattern.search(str(cmd.get("command", ""))))
            ]
            if not kept_commands:
                continue
            new_group = dict(group)
            new_group["hooks"] = kept_commands
            kept_groups.append(new_group)
        if kept_groups:
            new_hooks[event] = kept_groups
    out["hooks"] = new_hooks
    return out


def count_matches(registry: dict[str, Any], pattern: re.Pattern[str]) -> int:
    """Count hook commands in ``registry`` whose ``command`` field matches ``pattern``.

    Used as a post-write verification — caller can assert the rewrite was
    complete (returns 0) without re-parsing the file structure.

    Args:
        registry: Parsed JSON object.
        pattern: Compiled regex applied via ``pattern.search``.

    Returns:
        Number of matching command entries anywhere under the ``hooks`` tree.

    Examples:
        >>> import re
        >>> reg = {"hooks": {"PreToolUse": [
        ...     {"hooks": [{"command": ".claude/hooks/foo.js"}]},
        ... ]}}
        >>> count_matches(reg, re.compile(r"foo\\.js"))
        1
        >>> count_matches(reg, re.compile(r"missing\\.js"))
        0
    """
    n = 0
    hooks_section = registry.get("hooks") or {}
    for groups in hooks_section.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for cmd in inner:
                if isinstance(cmd, dict) and pattern.search(str(cmd.get("command", ""))):
                    n += 1
    return n


def run(json_file: Path, hook_name: str, path_pattern: str) -> int:
    """Apply the filter to ``json_file`` atomically and return an exit code.

    Reads the file, applies :func:`filter_hooks` with the compiled
    ``path_pattern`` (case-insensitive), serialises the result with 2-space
    indentation to ``<json_file>.tmp`` and renames over the original. On any
    failure the tmp file is removed and the original is left untouched.

    Args:
        json_file: Target JSON file (``settings.json`` or plugin
            ``hooks.json``).
        hook_name: Hook basename, used only in error messages — the actual
            match is driven by ``path_pattern``.
        path_pattern: Python regex (``re.search``-style) matched
            case-insensitively against each ``command`` field.

    Returns:
        0 on success (including no-op when the pattern matched nothing),
        1 if the target file is missing, 2 on JSON parse error, 3 on a bad
        regex.
    """
    if not json_file.is_file():
        print(f"! target not found: {json_file}", file=sys.stderr)
        return 1
    try:
        pattern = re.compile(path_pattern, re.IGNORECASE)
    except re.error as exc:
        print(f"! invalid --path-pattern regex: {exc}", file=sys.stderr)
        return 3
    try:
        registry = json.loads(json_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"! invalid JSON in {json_file}: {exc}", file=sys.stderr)
        return 2
    if not isinstance(registry, dict):
        print(f"! top-level JSON in {json_file} must be an object", file=sys.stderr)
        return 2

    filtered = filter_hooks(registry, pattern)
    tmp = json_file.with_suffix(json_file.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")
        shutil.move(str(tmp), str(json_file))
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(f"! failed to write {json_file}: {exc}", file=sys.stderr)
        return 2

    remaining = count_matches(filtered, pattern)
    if remaining:
        print(
            f"⚠ {remaining} entries for {hook_name!r} still present in {json_file}",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Remove the requested hook registration."""
    parser = argparse.ArgumentParser(
        prog="remove_hook_from_registry.py",
        description="Remove a named hook from a Claude Code hooks JSON registry.",
    )
    parser.add_argument("--json-file", required=True, type=Path, help="JSON file to rewrite in place.")
    parser.add_argument("--hook-name", required=True, help="Hook basename (used in error messages).")
    parser.add_argument(
        "--match",
        choices=("path", "basename"),
        default="path",
        help="Build the regex from --hook-name (escaped): 'path' -> \\.claude/hooks/<name>\\.js, "
        "'basename' -> <name>\\.js. Ignored if --path-pattern is also given.",
    )
    parser.add_argument(
        "--path-pattern",
        required=False,
        default=None,
        help="Escape hatch: raw Python regex, used verbatim instead of the safer --match form. "
        "Prefer --match — a metacharacter in a hand-built pattern over-matches, and this tool "
        "rewrites the file in place.",
    )
    args = parser.parse_args(argv)

    if args.path_pattern:
        effective_pattern = args.path_pattern
    else:
        escaped_name = re.escape(args.hook_name)
        effective_pattern = rf"\.claude/hooks/{escaped_name}\.js" if args.match == "path" else rf"{escaped_name}\.js"

    return run(args.json_file, args.hook_name, effective_pattern)


if __name__ == "__main__":
    sys.exit(main())
