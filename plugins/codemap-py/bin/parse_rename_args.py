#!/usr/bin/env python
"""parse_rename_args.py — parse ``rename-refs`` ``$ARGUMENTS`` once and write every session sentinel.

``rename-refs`` Step 0 used to re-split the substituted ``$ARGUMENTS`` blob with ``awk``, ``grep -q``
substring tests and ``sed``, plus a second regex parser for ``--deprecate``. Two parsers saw different
tokens: a flag spelled inside a value fired (``--since "--dry-run"``), a quoted path was mis-split,
``--no-deprecate`` tripped the ``--deprecate`` conflict check, and nothing validated the references
before they reached ``git mv``. This helper tokenizes the blob once with :func:`shlex.split`, validates
it, and writes the eleven sentinels later steps read back with ``IFS= read -r``.

Usage:
    export CSID="${CLAUDE_CODE_SESSION_ID:-$PPID}"
    _CM_PROJ=$(git rev-parse --show-toplevel 2>/dev/null | xargs basename 2>/dev/null || echo "cm")
    python "${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}/bin/parse_rename_args.py" "$ARGUMENTS" \\
        --output-prefix "codemap-${_CM_PROJ}" || exit 1

The ``$ARGUMENTS`` blob is the first positional argument and is taken before ``argparse`` sees the rest,
so a blob that begins with ``--`` is never mistaken for an outer flag.

Grammar::

    symbol <old_qname> <new_qname> [--dry-run] [--deprecate[=<decorator>] | --no-deprecate]
           [--since <ver>] [--removed-in <ver>] [--remove-if-no-callers]
    module <old_module_path> <new_module_path> [--dry-run]

Sentinels written to ``<tmpdir>/<prefix>-rename-<NAME>-<CSID>`` (one line each): ``SUBCOMMAND``,
``OLD_REF``, ``NEW_REF``, ``DRY_RUN``, ``REMOVE_IF_ZERO``, ``SINCE_VER``, ``REMOVED_IN_VER``,
``OLD_NAME``, ``NEW_NAME``, ``DEPRECATE``, ``DEPRECATE_DECORATOR``. Nothing is written when the
arguments are rejected, so the caller must stop on a non-zero exit.

Exit codes:
    0 — arguments valid; every sentinel written
    1 — arguments rejected (the user-facing message is on stderr, print it unchanged)
    2 — outer command-line usage error
    3 — unsafe ``--output-prefix``
    4 — a sentinel could not be written (for example a pre-planted symlink)
"""

from __future__ import annotations

import argparse
import keyword
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

#: This ``bin/`` directory, put on ``sys.path`` so the bare ``resolve_index_env`` import also resolves when
#: ``pytest --doctest-modules`` imports this file by path (a script run already has its own directory on the path).
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from resolve_index_env import (  # noqa: E402
    _resolve_csid,
    _resolve_tmpdir,
    _validate_output_prefix,
    _write_sentinel_file,
)

_USAGE = "Usage: /codemap-py:rename-refs symbol <old> <new> [flags] | module <old_path> <new_path> [--dry-run]"
#: Every flag the skill accepts; ``--since``/``--removed-in`` take the next token (or ``=value``) as value.
_BOOL_FLAGS = ("--dry-run", "--remove-if-no-callers", "--no-deprecate")
_VALUE_FLAGS = ("--since", "--removed-in")
_SUPPORTED = "--dry-run, --deprecate[=<decorator>], --no-deprecate, --since, --removed-in, --remove-if-no-callers"
#: Flags that only make sense for a symbol rename; ``module`` accepts ``--dry-run`` alone.
_SYMBOL_ONLY = ("--deprecate", "--no-deprecate", "--since", "--removed-in", "--remove-if-no-callers")


class RenameArgsError(ValueError):
    """The ``$ARGUMENTS`` blob was rejected; ``str(exc)`` is the exact message for the user."""


@dataclass(frozen=True)
class RenameArgs:
    """Validated ``rename-refs`` arguments."""

    subcommand: str
    old_ref: str
    new_ref: str
    dry_run: bool = False
    remove_if_zero: bool = False
    since: str = ""
    removed_in: str = ""
    deprecate: bool = False
    decorator: str = ""

    def sentinels(self) -> dict[str, str]:
        """Return ``{sentinel_name: value}`` for every value later steps read back.

        Examples:
            >>> RenameArgs("symbol", "Cls.old", "Cls.new").sentinels()["rename-OLD_NAME"]
            'old'
        """
        return {
            "rename-SUBCOMMAND": self.subcommand,
            "rename-OLD_REF": self.old_ref,
            "rename-NEW_REF": self.new_ref,
            "rename-DRY_RUN": _flag(self.dry_run),
            "rename-REMOVE_IF_ZERO": _flag(self.remove_if_zero),
            "rename-SINCE_VER": self.since,
            "rename-REMOVED_IN_VER": self.removed_in,
            "rename-OLD_NAME": self.old_ref.rsplit(".", 1)[-1],
            "rename-NEW_NAME": self.new_ref.rsplit(".", 1)[-1],
            "rename-DEPRECATE": _flag(self.deprecate),
            "rename-DEPRECATE_DECORATOR": self.decorator,
        }


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _tokenize(arguments: str) -> list[str]:
    """Split the blob the way a POSIX shell would; an unbalanced quote is rejected, never guessed."""
    try:
        return shlex.split(arguments)
    except ValueError as exc:
        raise RenameArgsError(f"! Could not parse arguments ({exc}) — check quoting and re-invoke") from exc


def _validate_ref(role: str, ref: str) -> None:
    """Reject a reference that is not a dotted Python identifier (``::``, ``/``, ``..``, keywords).

    Examples:
        >>> _validate_ref("NEW_REF", "pkg.new_mod")
        >>> try:
        ...     _validate_ref("NEW_REF", "../evil")
        ... except RenameArgsError as exc:
        ...     print(exc)
        ! NEW_REF '../evil' is not a dotted Python name (e.g. pkg.mod or MyClass.method)
    """
    if ref.startswith("-"):
        raise RenameArgsError(f"! {role} '{ref}' looks like a flag, not a target — check argument order")
    if "::" in ref:
        raise RenameArgsError(
            f"! {role} '{ref}' uses the module::symbol form — pass the symbol-local qualified name "
            "(e.g. MyClass.method); find-symbol does not accept module::symbol"
        )
    parts = ref.split(".")
    if not all(part.isidentifier() and not keyword.iskeyword(part) for part in parts):
        raise RenameArgsError(f"! {role} '{ref}' is not a dotted Python name (e.g. pkg.mod or MyClass.method)")


def _parse_flags(subcommand: str, tokens: list[str]) -> dict[str, object]:
    """Consume the flag tokens that follow the three positionals."""
    found: dict[str, object] = {}
    unknown: list[str] = []
    it = iter(tokens)
    for token in it:
        name, has_value, value = token.partition("=")
        if token in _BOOL_FLAGS:
            found[token] = True
        elif name == "--deprecate":
            # first --deprecate=<value> supplies the decorator; a bare flag never overrides it
            found.setdefault("--deprecate", value if has_value else "")
            if has_value and not found["--deprecate"]:
                found["--deprecate"] = value
        elif name in _VALUE_FLAGS:
            if not has_value:
                value = next(it, "")
                if not value or value.startswith("--"):
                    raise RenameArgsError(f"! {name} requires a value")
            found[name] = value
        elif token.startswith("--"):
            unknown.append(token)
        else:
            raise RenameArgsError(f"! Unexpected argument '{token}' — quote a decorator that contains spaces\n{_USAGE}")
    if unknown:
        raise RenameArgsError(
            f"! Unknown flag(s): {' '.join(unknown)}. Supported flags: {_SUPPORTED}. Re-invoke with corrected flags."
        )
    if subcommand == "module":
        symbol_only = [flag for flag in _SYMBOL_ONLY if flag in found]
        if symbol_only:
            raise RenameArgsError(f"! {', '.join(symbol_only)}: symbol-only — module accepts only --dry-run")
    return found


def parse_rename_args(arguments: str) -> RenameArgs:
    """Parse and validate the ``rename-refs`` ``$ARGUMENTS`` blob.

    Args:
        arguments: Raw ``$ARGUMENTS`` string.

    Returns:
        The validated arguments.

    Raises:
        RenameArgsError: if the blob is rejected; the message is meant to be shown unchanged.

    Examples:
        >>> parse_rename_args("symbol old_fn new_fn --dry-run").dry_run
        True
        >>> a = parse_rename_args("symbol Old New --deprecate='@deprecated_class(target=New, x=1)' --since 1.2")
        >>> (a.deprecate, a.decorator, a.since)
        (True, '@deprecated_class(target=New, x=1)', '1.2')
        >>> try:
        ...     parse_rename_args("symbol a b --since --dry-run")
        ... except RenameArgsError as exc:
        ...     print(exc)
        ! --since requires a value
        >>> parse_rename_args("symbol a b --no-deprecate --remove-if-no-callers").remove_if_zero
        True
    """
    tokens = _tokenize(arguments)
    subcommand = tokens[0] if tokens else ""
    if subcommand not in ("symbol", "module"):
        raise RenameArgsError(_USAGE)
    if len(tokens) < 3:
        raise RenameArgsError(
            f"! Usage: /codemap-py:rename-refs {subcommand} <old> <new> [flags] — old or new ref missing"
        )
    old_ref, new_ref = tokens[1], tokens[2]
    _validate_ref("OLD_REF", old_ref)
    _validate_ref("NEW_REF", new_ref)
    found = _parse_flags(subcommand, tokens[3:])
    deprecate = "--deprecate" in found and "--no-deprecate" not in found
    remove_if_zero = bool(found.get("--remove-if-no-callers"))
    if deprecate and remove_if_zero:
        raise RenameArgsError(
            "⚠ conflicting flags: --deprecate creates alias, --remove-if-no-callers deletes target; "
            "these are incompatible"
        )
    return RenameArgs(
        subcommand=subcommand,
        old_ref=old_ref,
        new_ref=new_ref,
        dry_run=bool(found.get("--dry-run")),
        remove_if_zero=remove_if_zero,
        since=str(found.get("--since", "")),
        removed_in=str(found.get("--removed-in", "")),
        deprecate=deprecate,
        decorator=str(found["--deprecate"]) if deprecate else "",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — returns the process exit code (see module docstring)."""
    args = sys.argv[1:] if argv is None else argv
    arguments, rest = (args[0], args[1:]) if args else ("", [])
    parser = argparse.ArgumentParser(prog="parse_rename_args.py", description=__doc__.splitlines()[0])
    parser.add_argument("--output-prefix", required=True, help="Sentinel filename prefix, e.g. codemap-<project>.")
    outer = parser.parse_args(rest)
    try:
        prefix = _validate_output_prefix(outer.output_prefix)
    except ValueError as exc:
        sys.stderr.write(f"parse_rename_args: {exc}\n")
        return 3
    try:
        parsed = parse_rename_args(arguments)
    except RenameArgsError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    tmpdir, csid = _resolve_tmpdir(), _resolve_csid()
    try:
        for name, value in parsed.sentinels().items():
            _write_sentinel_file(Path(tmpdir, f"{prefix}-{name}-{csid}"), f"{value}\n")
    except OSError as exc:
        sys.stderr.write(f"! parse_rename_args: cannot write sentinel — {exc}\n")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
