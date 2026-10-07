#!/usr/bin/env python3
"""Classify whether a judge program warrants architect and Codex review passes.

Purpose:
    Apply the deterministic complexity signals consumed by the research judge workflow
    before it spends extra review passes.

Scope:
    Read one local judge-program text file without executing it. Classify as complex
    when more than one scope-file line is present, ``agent_strategy: arch`` is declared,
    or a cross-domain keyword matches; otherwise classify as simple.

Usage:
    Run ``python detect-complexity.py <program-path>`` and read ``true`` or ``false``
    from stdout.

Outputs:
    Print one lowercase boolean verdict and return 0; print a diagnostic and return
    status 2 when the required path argument is absent.

Failure:
    Missing or unreadable program files are conservatively classified as simple; malformed
    text is decoded with replacement characters rather than aborting the judge workflow.

Used by:
    Research judge skills that gate optional architect/Codex passes on program scope.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# [ \t\r\f\v], not \s: grep matches within a line, where POSIX [[:space:]] cannot include the
# newline that Python's \s would. \S is safe as-is for the same reason.
#: Regex character class for horizontal whitespace, used instead of \s so matching never spans a line break.
_WS = r"[ \t\r\f\v]"
#: Matches a program line that lists one source file (py, ts, js, cpp, go or rs), optionally as a bullet.
_SCOPE_RE = re.compile(rf"^{_WS}*[-*]?{_WS}*\S+\.(py|ts|js|cpp|go|rs){_WS}*$")
#: Matches everything up to and including the agent_strategy: label, so substituting it away leaves the strategy value.
_STRATEGY_RE = re.compile(rf"^.*agent_strategy:{_WS}*")
#: Case-insensitive phrases in a program that signal cross-domain or multi-component work and mark it complex.
_CROSS_DOMAIN_RE = re.compile(
    r"cross.domain|multi.system|distributed|multiple.*component|pipeline.*stage",
    re.IGNORECASE,
)


def _lines(program: Path) -> list[str]:
    """Program lines without terminators; empty list when the file is missing or unreadable."""
    try:
        return program.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _strategy(lines: list[str]) -> str:
    """Return the first ``agent_strategy:`` value, preserving shell-compatible trimming.

    Examples:
        >>> _strategy(['agent_strategy: arch', 'agent_strategy: simple'])
        'arch'
        >>> _strategy(['# no strategy'])
        ''
    """
    for line in lines:
        if "agent_strategy:" in line:
            # Greedy `.*` matches through the LAST occurrence on the line, as sed does.
            return _STRATEGY_RE.sub("", line).replace("\r", "").replace("\n", "")
    return ""


def main(argv: list[str]) -> int:
    """Print the complexity verdict for argv's program path, or return 2 when absent.

    ``argv`` includes the executable name. Missing or unreadable files yield ``false`` through :func:`_lines`; the
    inspected program is never executed.
    """
    program = argv[1] if len(argv) > 1 else ""
    if not program:
        print("detect-complexity: missing <program-path> argument", file=sys.stderr)
        return 2

    lines = _lines(Path(program))
    scope_count = sum(1 for line in lines if _SCOPE_RE.search(line))
    is_complex = scope_count > 1 or _strategy(lines) == "arch" or any(_CROSS_DOMAIN_RE.search(line) for line in lines)

    print("true" if is_complex else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
