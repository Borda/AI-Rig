#!/usr/bin/env python3
"""Render one framed benchmark surface for callers that cannot import the presentation layer.

The batch launcher is a shell script, so it cannot call :mod:`_bench_common.presentation` directly. This entrypoint
gives it the two surfaces it needs: a phase header rendered exactly the way every Python runner renders its own, and a
legend rendered the same way a runner renders its own. Both come out framed on a terminal and in their plain, ANSI-free
form when the run's output is redirected into a log, which is what run logs and downstream readers parse.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bench_common.presentation import (
    LEGEND_CLOSE_RULE,
    LEGEND_OPEN_RULE,
    benchmark_console,
    print_legend,
    print_section_rule,
)

#: Name the parser reports in its usage and error messages, independent of the invoking path.
PROGRAM_NAME = "render_cli.py"


def render_rule(title: str) -> None:
    """Print one phase header, degrading to its plain form when rich is unavailable.

    The header is decoration on a run that may be paid and long-lived, and the launcher runs under
    ``set -e``, so a missing optional dependency must not end the run. Rich is imported lazily by
    the console builder; when that import fails, the redirected form is printed instead, which is
    the same line the launcher printed before it rendered headers through this layer.

    Args:
        title: The phase name, without its surrounding marks.

    Examples:
        >>> render_rule("PREPARE frozen parity index")
        == PREPARE frozen parity index ==
    """
    try:
        print_section_rule(title, console=benchmark_console())
    except ImportError:
        print(f"== {title} ==")


def render_legend(body_lines: Sequence[str]) -> None:
    """Print one legend, degrading to its plain framed form when rich is unavailable.

    A legend explains the columns of the rows around it, so losing it to a missing optional
    dependency would cost a reader more than the panel border is worth. The fallback prints the same
    rules and body a redirected run writes to its log.

    Args:
        body_lines: The legend's lines, without the framing rules.

    Examples:
        >>> render_legend(["  treatments: A_plain=no Codemap", "  status: done"])
        =================================== LEGEND ===================================
          treatments: A_plain=no Codemap
          status: done
        ================================= END LEGEND =================================
    """
    try:
        print_legend(body_lines, console=benchmark_console())
    except ImportError:
        print(LEGEND_OPEN_RULE)
        print("\n".join(line.rstrip("\r\n") for line in body_lines))
        print(LEGEND_CLOSE_RULE)


def read_body_lines(stream: Sequence[str] | None = None) -> list[str]:
    """Read a legend body from a stream, dropping the trailing newline the shell adds.

    The launcher pipes a heredoc in, and a heredoc always ends in a newline, which would otherwise
    render as one blank line inside the panel.

    Args:
        stream: Iterable of body lines; ``None`` reads standard input.

    Returns:
        The body lines, without their line endings.

    Examples:
        >>> read_body_lines(["  metrics:\\n", "      EREC: expected recall\\n"])
        ['  metrics:', '      EREC: expected recall']
    """
    source = sys.stdin if stream is None else stream
    return "".join(source).splitlines()


def build_parser() -> argparse.ArgumentParser:
    """Build the parser accepting one rendering subcommand.

    Returns:
        A parser that fails with a usage message when the subcommand or the title is missing.

    Examples:
        >>> build_parser().parse_args(["rule", "QUERY (no model)"]).title
        'QUERY (no model)'
        >>> build_parser().parse_args(["legend"]).command
        'legend'
    """
    parser = argparse.ArgumentParser(
        prog=PROGRAM_NAME,
        description="Render one benchmark surface through the shared presentation layer.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    rule = subcommands.add_parser("rule", help="announce one run phase as a titled rule")
    rule.add_argument("title", help="phase name, without its surrounding marks")
    subcommands.add_parser("legend", help="frame a legend whose body lines arrive on standard input")
    return parser


def main(argv: Sequence[str]) -> int:
    """Render the surface named by the command line.

    Args:
        argv: Command-line arguments, without the program name.

    Returns:
        The process exit status; an unknown subcommand or a missing title exits through the
        parser's own usage error instead.

    Examples:
        >>> main(["rule", "BUILD generated benchmark manifests (no model)"])
        == BUILD generated benchmark manifests (no model) ==
        0
    """
    arguments = build_parser().parse_args(list(argv))
    if arguments.command == "legend":
        render_legend(read_body_lines())
        return 0
    render_rule(arguments.title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
