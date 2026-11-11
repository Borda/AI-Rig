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
    A missing or unreadable program file prints a stderr diagnostic, prints ``true`` and
    returns status 2. ``true`` fails closed: a "simple" verdict would skip the architect and
    Codex passes and let the judge rate methodology sound with no program read at all,
    while consumers capture stdout only (``X=$(python detect-complexity.py ...)``) and never
    see the exit status. Malformed text is decoded with replacement characters rather than
    aborting the judge workflow.

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
#: Matches the scope_files label and captures its optional inline entries.
_SCOPE_RE = re.compile(rf"^{_WS}*scope_files:{_WS}*(.*)$")
#: Matches everything up to and including the agent_strategy: label, so substituting it away leaves the strategy value.
_STRATEGY_RE = re.compile(rf"^.*agent_strategy:{_WS}*")
#: Case-insensitive phrases in a program that signal cross-domain or multi-component work and mark it complex.
_CROSS_DOMAIN_RE = re.compile(
    r"cross.domain|multi.system|distributed",
    re.IGNORECASE,
)


def _lines(program: Path) -> list[str] | None:
    """Program lines without terminators; ``None`` when the file is missing or unreadable."""
    try:
        return program.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


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


def _scope_count(lines: list[str]) -> int:
    """Count declared scope entries regardless of file extension or glob spelling."""
    count = 0
    in_scope = False
    for line in lines:
        label = _SCOPE_RE.match(line)
        if label:
            value = label.group(1).split("#", 1)[0].strip()
            count += sum(bool(part.strip()) for part in value.strip("[]").split(","))
            in_scope = not value
            continue
        stripped = line.strip()
        if not in_scope or not stripped or stripped.startswith("#"):
            continue
        if re.match(rf"[-*]{_WS}+", stripped):
            count += 1
        else:
            in_scope = False
    return count


def _cross_domain(line: str) -> bool:
    """Recognize ordered keyword pairs without rescanning every repeated prefix."""
    if _CROSS_DOMAIN_RE.search(line):
        return True
    lower = line.lower()
    return any(
        (start := lower.find(prefix)) >= 0 and lower.find(suffix, start + len(prefix)) >= 0
        for prefix, suffix in (("multiple", "component"), ("pipeline", "stage"))
    )


def main(argv: list[str]) -> int:
    """Print the complexity verdict for argv's program path, or return 2 when absent.

    ``argv`` includes the executable name. A missing or unreadable file prints ``true`` (fail closed — run the full
    review) and returns 2; the inspected program is never executed.
    """
    program = argv[1] if len(argv) > 1 else ""
    if not program:
        print("detect-complexity: missing <program-path> argument", file=sys.stderr)
        return 2

    lines = _lines(Path(program))
    if lines is None:
        print(f"detect-complexity: cannot read program file {program!r}; treating as complex", file=sys.stderr)
        print("true")
        return 2
    scope_count = _scope_count(lines)
    is_complex = scope_count > 1 or _strategy(lines) == "arch" or any(_cross_domain(line) for line in lines)

    print("true" if is_complex else "false")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
