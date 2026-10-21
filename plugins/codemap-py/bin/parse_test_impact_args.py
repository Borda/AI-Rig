#!/usr/bin/env python3
"""Parse the test-impact skill's ``$ARGUMENTS`` into its target and ``--no-mocks`` sentinels.

The skill used to split ``$ARGUMENTS`` with ``awk '{print $1}'`` and test ``--no-mocks`` with a substring ``grep``.
That sent ``--no-mocks`` itself as the target when the flag came first (``--no-mocks pkg::f``), matched the flag
inside unrelated text, and broke on quoted values. This helper tokenizes once with :func:`shlex.split`, compares
flags as exact tokens, and takes the first non-flag token as the target.

The whole argument string arrives through the ``--arguments=`` form (equals sign, no space): a bare positional
whose text starts with ``--`` (``--no-mocks pkg::f``) would otherwise be read by argparse as a flag.

Usage:
    python parse_test_impact_args.py --output-prefix "codemap-${_CM_PROJ}" --arguments="$ARGUMENTS"

Sentinels (``${TMPDIR}/<prefix>-ti-<name>-<CSID>``, one line each, always rewritten so a stale value from an
earlier run is never reused):
    ti-qname   the target, empty on any failure
    ti-mocks   ``--no-mocks`` or empty

Exit codes:
    0 — target parsed; stdout names it (plus the one-symbol warning when extra targets were given)
    2 — malformed input: unknown flag, unbalanced quote, or a flag-shaped target; or a sentinel could not be written
        safely (planted symlink, file owned by another user, unsafe ``--output-prefix``)
    3 — no target given; the skill asks the user for one
"""

from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

#: This ``bin/`` directory, put on ``sys.path`` so the sibling ``resolve_index_env`` import also resolves when
#: ``pytest --doctest-modules`` imports this file by path.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from resolve_index_env import (  # noqa: E402  (needs the sys.path insert above)
    _resolve_csid,
    _resolve_tmpdir,
    _validate_output_prefix,
    _write_sentinel_file,
)

#: The only flag the test-impact skill accepts.
NO_MOCKS_FLAG = "--no-mocks"

#: Exit status for malformed arguments.
EXIT_BAD_INPUT = 2

#: Exit status when ``$ARGUMENTS`` names no target at all.
EXIT_NO_TARGET = 3


class ArgumentsError(ValueError):
    """The argument string cannot be turned into one test-impact target."""


@dataclass(frozen=True)
class ImpactArgs:
    """Parsed test-impact arguments.

    Attributes:
        qname: First non-flag token, or ``""`` when none was given.
        no_mocks: Whether ``--no-mocks`` appeared as an exact token.
        extra_targets: Non-flag tokens after the first; the skill handles one target per run.
    """

    qname: str
    no_mocks: bool
    extra_targets: tuple[str, ...]


def parse_arguments(raw: str) -> ImpactArgs:
    """Split the raw ``$ARGUMENTS`` text into a target, the mocks flag and any surplus targets.

    Args:
        raw: The skill's ``$ARGUMENTS`` text, exactly as substituted.

    Returns:
        The parsed arguments; ``qname`` is empty when only flags (or nothing) were given.

    Raises:
        ArgumentsError: On an unbalanced quote or any flag other than ``--no-mocks``.

    Examples:
        >>> parse_arguments("--no-mocks pkg.mod::fn")
        ImpactArgs(qname='pkg.mod::fn', no_mocks=True, extra_targets=())
        >>> parse_arguments("pkg.a::f pkg.b::g").extra_targets
        ('pkg.b::g',)
        >>> parse_arguments("pkg.no-mocks-helper").no_mocks
        False
    """
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ArgumentsError(f"cannot split arguments ({exc}): {raw}") from exc
    unknown = [token for token in tokens if token.startswith("-") and token != NO_MOCKS_FLAG]
    if unknown:
        raise ArgumentsError(f"Unknown flag(s): {' '.join(unknown)}. Supported: {NO_MOCKS_FLAG}")
    targets = [token for token in tokens if not token.startswith("-")]
    return ImpactArgs(
        qname=targets[0] if targets else "",
        no_mocks=NO_MOCKS_FLAG in tokens,
        extra_targets=tuple(targets[1:]),
    )


def _sentinel(prefix: str, name: str) -> Path:
    """Return the session-scoped sentinel path the skill's later blocks read back.

    ``TMPDIR`` and ``CSID`` go through the shared resolvers: an untrusted or unsafe ``TMPDIR`` and a ``CSID`` that is
    not a plain filename component are replaced rather than used to place the file.
    """
    return Path(_resolve_tmpdir()) / f"{_validate_output_prefix(prefix)}-ti-{name}-{_resolve_csid()}"


def write_sentinels(prefix: str, parsed: ImpactArgs) -> None:
    """Write the target and mocks sentinels, replacing whatever an earlier run left.

    Writes never follow a planted symlink or rewrite a file another user owns (see ``_write_sentinel_file``).

    Args:
        prefix: Sentinel name prefix, ``codemap-<project>``.
        parsed: Arguments to persist; an empty ``qname`` clears the target.

    Raises:
        OSError: When a sentinel path is a symlink or a file owned by another user.
        ValueError: When ``prefix`` is not a bare filename prefix.
    """
    values = {"qname": parsed.qname, "mocks": NO_MOCKS_FLAG if parsed.no_mocks else ""}
    for name, value in values.items():
        _write_sentinel_file(_sentinel(prefix, name), value + "\n")


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for this helper's own command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-prefix", required=True, help="Sentinel name prefix, e.g. codemap-<project>")
    parser.add_argument("--arguments", default="", help='Raw $ARGUMENTS text; pass as --arguments="$ARGUMENTS"')
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse ``--arguments``, write the sentinels and report the target.

    Args:
        argv: Override ``sys.argv[1:]`` (mainly for testing).

    Returns:
        ``0`` on a parsed target, ``2`` on malformed input, ``3`` when no target was given.
    """
    args = _build_parser().parse_args(argv)
    try:
        parsed = parse_arguments(args.arguments)
    except ArgumentsError as exc:
        parsed, error = ImpactArgs(qname="", no_mocks=False, extra_targets=()), exc
    else:
        error = None
    try:
        write_sentinels(args.output_prefix, parsed)
    except (OSError, ValueError) as exc:
        print(f"! test-impact sentinels not written: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if error is not None:
        print(f"! {error}", file=sys.stderr)
        return EXIT_BAD_INPUT
    if not parsed.qname:
        print("test-impact: no target given — ask which function or module changed", file=sys.stderr)
        return EXIT_NO_TARGET
    print(f"target: {parsed.qname}")
    print(f"mocks: {'excluded (--no-mocks)' if parsed.no_mocks else 'included'}")
    if parsed.extra_targets:
        print(
            f"⚠ test-impact accepts one symbol at a time — using first token only: {parsed.qname}. "
            "Run separately for each remaining symbol."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
