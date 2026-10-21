#!/usr/bin/env python
"""merge_settings_permissions.py — merge a plugin's shipped allow/deny rules into the user's settings.json.

Every plugin's ``/setup`` copies its ``.claude-plugin/permissions-allow.json`` and
``permissions-deny.json`` into ``~/.claude/settings.json``: Claude Code does not read them
from the plugin manifest, so without this merge they are inert. Each setup used to inline
the same ``jq --slurpfile … | unique`` filter plus a temp-file ``mv``; this script is that
merge, shipped byte-identical in every plugin (``propagate_shared.py`` MANIFEST) because a
plugin may be installed alone.

Merge semantics — the replaced jq filters, minus their re-sort:

- allow: ``(.permissions.allow // []) | map(select(. != <drop>…)) + <allow file>``, de-duplicated
- deny: ``((.permissions.deny // []) - <retired file>) + <deny file>``, de-duplicated

De-duplication keeps the first occurrence of each rule (jq equality, so ``true`` never equals
``1``), so the user's own rules keep the order they wrote them in and newly shipped rules are
appended in shipped-file order. jq ``unique`` also *sorted* the arrays, re-ordering every
user's hand-arranged list on each setup run; that re-sort is deliberately dropped. Every other
key in the file is left untouched and in place. Both arrays are written in one atomic replace,
so an allow merge is never left on disk without its deny half.

Write root: the settings file must sit directly in ``~/.claude/`` (``settings.json`` or
``settings.local.json``). Anything else — including the plugin cache deeper under
``~/.claude/`` — is refused. ``~`` is ``$HOME`` when set (as in the setup bash), else the
platform home directory. The root check reads the lexical path, so a ``settings.json``
symlinked into a dotfiles tree still passes; the replace then lands on the link's resolved
target, keeping the link and the target's existing permission bits (the shell ``mv`` this
replaces swapped the link for a regular file).

Usage:
    merge_settings_permissions.py --allow <file> [--drop-allow <rule>]...
    merge_settings_permissions.py --deny <file> [--retire-deny-file <file>]
    merge_settings_permissions.py --allow <file> --deny <file> [--settings <path>]

Options:
    --settings PATH           Settings file to edit (default ``~/.claude/settings.json``).
    --allow FILE              JSON array of allow rules to add.
    --drop-allow RULE         Existing allow rule to remove before adding (repeatable, exact match).
    --deny FILE               JSON array of deny rules to add.
    --retire-deny-file FILE   JSON array of deny rules to remove before adding (exact match).

Output (stdout), one line per merged key, for the setup report:
    permissions.allow: added N, already present M, dropped K
    permissions.deny: added N, already present M, retired K

Exit codes:
    0 — merged and written (also when nothing changed)
    1 — settings or source file missing, unreadable, invalid JSON, or wrong shape; settings unchanged
    2 — bad arguments (argparse), or neither --allow nor --deny given
    4 — settings path outside the ``~/.claude/`` write root
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Default target: the user-level settings file Claude Code reads.
_DEFAULT_SETTINGS = "~/.claude/settings.json"


class MergeError(Exception):
    """Input that cannot be merged; the settings file is left unchanged."""


@dataclass(frozen=True)
class MergeCounts:
    """How one permissions list changed: rules added, rules already present, rules removed first."""

    added: int
    present: int
    removed: int


def _jq_rank(value: Any) -> int:
    """Return jq's type rank for ordering: null < false < true < numbers < strings < arrays < objects."""
    if value is None:
        return 0
    if value is False:
        return 1
    if value is True:
        return 2
    if isinstance(value, (int, float)):
        return 3
    if isinstance(value, str):
        return 4
    if isinstance(value, list):
        return 5
    return 6


def _jq_compare(left: Any, right: Any) -> int:
    """Compare two JSON values the way jq ``sort``/``unique`` order them.

    Examples:
        >>> _jq_compare("Bash(a)", "Bash(b)")
        -1
        >>> _jq_compare(None, "x")
        -1
        >>> _jq_compare({"a": 1}, {"a": 1})
        0
    """
    left_rank, right_rank = _jq_rank(left), _jq_rank(right)
    if left_rank != right_rank:
        return -1 if left_rank < right_rank else 1
    if left_rank in (3, 4):
        return (left > right) - (left < right)
    if left_rank == 5:
        for a, b in zip(left, right):
            order = _jq_compare(a, b)
            if order:
                return order
        return (len(left) > len(right)) - (len(left) < len(right))
    if left_rank == 6:
        order = _jq_compare(sorted(left), sorted(right))
        if order:
            return order
        for key in sorted(left):
            order = _jq_compare(left[key], right[key])
            if order:
                return order
    return 0


def dedupe_in_order(values: list[Any]) -> list[Any]:
    """De-duplicate by jq equality, keeping each value's first occurrence and the input order.

    Examples:
        >>> dedupe_in_order(["Bash(b)", "Bash(a)", "Bash(b)"])
        ['Bash(b)', 'Bash(a)']
        >>> dedupe_in_order([True, 1, True])
        [True, 1]
        >>> dedupe_in_order([])
        []
    """
    result: list[Any] = []
    for value in values:
        if not _jq_contains(result, value):
            result.append(value)
    return result


def _jq_contains(values: list[Any], value: Any) -> bool:
    """Membership by jq equality, where ``true`` never equals ``1`` (Python's ``in`` says it does)."""
    return any(_jq_compare(candidate, value) == 0 for candidate in values)


def _existing_list(permissions: dict[str, Any], key: str) -> list[Any]:
    """Read ``.permissions.<key> // []``; jq's ``//`` treats both null and false as absent."""
    value = permissions.get(key)
    if value is None or value is False:
        return []
    if not isinstance(value, list):
        raise MergeError(f"permissions.{key} in settings is {type(value).__name__}, expected an array")
    return value


def _load_json(path: Path, label: str) -> Any:
    """Parse one JSON file, turning every read or parse failure into a :class:`MergeError`."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MergeError(f"{label} not found: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MergeError(f"{label} unreadable or invalid JSON: {path}: {exc}") from exc


def _load_rule_list(path: Path, label: str) -> list[Any]:
    """Load a shipped rule file; jq's ``X + null`` is X, so a JSON null contributes no rules."""
    value = _load_json(path, label)
    if value is None:
        return []
    if not isinstance(value, list):
        raise MergeError(f"{label} must hold a JSON array: {path}")
    return value


def merge_allow(permissions: dict[str, Any], shipped: list[Any], drop: list[str]) -> MergeCounts:
    """Apply the allow merge in place and return what changed.

    Examples:
        >>> perms = {"allow": ["Bash(gh api repos/*:*)", "Read"]}
        >>> merge_allow(perms, ["Bash(gh api repos/*)", "Read"], ["Bash(gh api repos/*:*)"])
        MergeCounts(added=1, present=1, removed=1)
        >>> perms["allow"]
        ['Read', 'Bash(gh api repos/*)']
    """
    existing = _existing_list(permissions, "allow")
    kept = [rule for rule in existing if not _jq_contains(list(drop), rule)]
    unique_shipped = dedupe_in_order(shipped)
    present = sum(1 for rule in unique_shipped if _jq_contains(kept, rule))
    permissions["allow"] = dedupe_in_order(kept + shipped)
    return MergeCounts(added=len(unique_shipped) - present, present=present, removed=len(existing) - len(kept))


def merge_deny(permissions: dict[str, Any], shipped: list[Any], retired: list[Any]) -> MergeCounts:
    """Apply the deny merge in place and return what changed.

    jq array subtraction removes every occurrence of each retired value before the add.

    Examples:
        >>> perms = {"deny": ["Bash(gh pr merge:*)", "Bash(rm -rf /:*)"]}
        >>> merge_deny(perms, ["Bash(rm -rf /:*)"], ["Bash(gh pr merge:*)"])
        MergeCounts(added=0, present=1, removed=1)
        >>> perms["deny"]
        ['Bash(rm -rf /:*)']
    """
    existing = _existing_list(permissions, "deny")
    kept = [rule for rule in existing if not _jq_contains(retired, rule)]
    unique_shipped = dedupe_in_order(shipped)
    present = sum(1 for rule in unique_shipped if _jq_contains(kept, rule))
    permissions["deny"] = dedupe_in_order(kept + shipped)
    return MergeCounts(added=len(unique_shipped) - present, present=present, removed=len(existing) - len(kept))


def _home_dir() -> Path:
    """Return the user's home directory, preferring ``$HOME`` over :meth:`Path.home`.

    The setup bash creates, backs up and validates ``~/.claude/settings.json`` through ``$HOME``. On native Windows
    ``Path.home()`` reads ``USERPROFILE`` instead (Git Bash sets only ``HOME``), so using it alone could merge into a
    different file than the one setup just backed up.
    """
    return Path(os.environ.get("HOME") or Path.home())


def _expand_home(path: Path) -> Path:
    """Expand a leading ``~`` through :func:`_home_dir`; ``~user`` is left to :meth:`Path.expanduser`.

    ``Path.expanduser`` reads ``USERPROFILE`` on Windows and would ignore ``$HOME`` the same way.
    """
    parts = path.parts
    if parts and parts[0] == "~":
        return _home_dir().joinpath(*parts[1:])
    return path.expanduser()


def _validate_settings_path(raw: str) -> Path:
    """Return the lexical absolute settings path, refusing anything not directly inside ``~/.claude``.

    Raises:
        ValueError: If the path is outside the write root or not a ``.json`` file.
    """
    claude_dir = Path(os.path.abspath(_home_dir() / ".claude"))
    target = Path(os.path.abspath(_expand_home(Path(raw))))
    if target.parent != claude_dir or target.suffix != ".json":
        raise ValueError(f"settings path must be a .json file directly in {claude_dir}: {target}")
    return target


def _atomic_replace(target: Path, text: str) -> None:
    """Write ``text`` beside the file ``target`` resolves to and rename it over that file, keeping its mode bits.

    Resolving first writes through a symlinked settings file: the link survives and its target is updated, where a
    replace at the lexical path would swap the link for a regular file.
    """
    destination = Path(os.path.realpath(target))
    mode = stat.S_IMODE(destination.stat().st_mode)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text.encode("utf-8"))
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _settings_permissions(settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the settings object and its ``permissions`` object, creating either where jq assignment would."""
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise MergeError(f"settings must hold a JSON object, found {type(settings).__name__}")
    permissions = settings.get("permissions")
    if permissions is None:
        permissions = {}
        settings["permissions"] = permissions
    if not isinstance(permissions, dict):
        raise MergeError(f"settings permissions is {type(permissions).__name__}, expected an object")
    return settings, permissions


def run(args: argparse.Namespace) -> int:
    """Load, merge and write; return the process exit code."""
    try:
        target = _validate_settings_path(args.settings)
    except ValueError as exc:
        print(f"! merge_settings_permissions: {exc}", file=sys.stderr)
        return 4
    try:
        settings, permissions = _settings_permissions(_load_json(target, "settings"))
        report: list[str] = []
        if args.allow:
            shipped = _load_rule_list(Path(args.allow), "allow file")
            counts = merge_allow(permissions, shipped, args.drop_allow)
            report.append(
                f"permissions.allow: added {counts.added}, already present {counts.present}, dropped {counts.removed}"
            )
        if args.deny:
            shipped = _load_rule_list(Path(args.deny), "deny file")
            retired: list[Any] = []
            if args.retire_deny_file:
                retired = _load_rule_list(Path(args.retire_deny_file), "retired deny file")
            counts = merge_deny(permissions, shipped, retired)
            report.append(
                f"permissions.deny: added {counts.added}, already present {counts.present}, retired {counts.removed}"
            )
        # jq prints two-space indented JSON with non-ASCII kept raw; the shell form added one trailing newline.
        _atomic_replace(target, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    except MergeError as exc:
        print(f"! merge_settings_permissions: {exc} — settings unchanged", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"! merge_settings_permissions: cannot write {target}: {exc} — settings unchanged", file=sys.stderr)
        return 1
    print("\n".join(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the merge.

    No doctest — filesystem- and argv-dependent; covered by pytest.
    """
    parser = argparse.ArgumentParser(
        prog="merge_settings_permissions.py",
        description="Merge a plugin's shipped permissions allow/deny rules into ~/.claude/settings.json.",
    )
    parser.add_argument("--settings", default=_DEFAULT_SETTINGS, help=f"Settings file (default {_DEFAULT_SETTINGS}).")
    parser.add_argument("--allow", metavar="FILE", help="JSON array of allow rules to add.")
    parser.add_argument(
        "--drop-allow",
        metavar="RULE",
        action="append",
        default=[],
        help="Existing allow rule removed before the add (repeatable, exact match).",
    )
    parser.add_argument("--deny", metavar="FILE", help="JSON array of deny rules to add.")
    parser.add_argument("--retire-deny-file", metavar="FILE", help="JSON array of deny rules removed before the add.")
    args = parser.parse_args(argv)
    if not args.allow and not args.deny:
        parser.error("give --allow, --deny, or both")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
