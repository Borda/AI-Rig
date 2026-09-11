#!/usr/bin/env python
"""find_judge_verdict.py — locate the newest judge verdict and gate ablation on an approved baseline.

Extracted from ``skills/fortify/SKILL.md`` step F1. Picks the most recent
``.reports/research/judge-*.md``, pulls its ``Verdict:`` and ``Program:`` fields, and refuses to
continue unless that verdict was issued for the experiment currently being fortified — otherwise
fortify could ablate against some other program's approval.

Only the ``Verdict:`` field of the judge report is authoritative; ``methodology_rating: sound``
is one input to a verdict, not the verdict.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_judge_verdict.py" --state-dir-base DIR --run-id ID

Sentinels written (newline-terminated — the ``IFS= read -r`` reload contract needs it):
    ${TMPDIR}/fortify-program-file-${CSID}   — consumed by F6
    ${TMPDIR}/fortify-judge-verdict-${CSID}  — consumed by the gate-on-sentinel.py block

Exit codes:
    0 — verdict and program resolved, sentinels written
    1 — BLOCKED: no verdict file, no Program field, program mismatch, or program missing on disk
    2 — argument error (argparse default)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_VERDICT_LINE_RE = re.compile(r"^\**verdict\**:", re.IGNORECASE)
_PROGRAM_LINE_RE = re.compile(r"^\**(program(_file)?|program file)\**:", re.IGNORECASE)
#: Case-sensitive on purpose — faithful to the ``sed -E 's/.*[Vv]erdict[: ]+//'`` it replaces,
#: which leaves an all-caps ``VERDICT:`` label in place.
_VERDICT_STRIP_RE = re.compile(r".*[Vv]erdict[: ]+")
_PROGRAM_STRIP_RE = re.compile(r".*:[ \t\r\f\v]*")
_TRAILING_WS_RE = re.compile(r"[ \t\r\f\v]*$")

_REPORTS_DIR = Path(".reports/research")


def newest_verdict_file(reports_dir: Path) -> Path | None:
    """Return the most recently modified ``judge-*.md`` report, as ``ls -t | head -1`` did.

    Args:
        reports_dir: Directory holding judge reports.

    Returns:
        The newest report path, or ``None`` when the directory holds none.
    """
    candidates = [p for p in reports_dir.glob("judge-*.md") if p.is_file()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: (-p.stat().st_mtime, p.name))[0]


def extract_field(lines: list[str], line_re: re.Pattern[str], strip_re: re.Pattern[str]) -> str:
    """Pull one labelled field out of a judge report.

    Reproduces the original pipeline order: match the first labelled line, drop every ``**``,
    strip greedily through the label, then trim trailing whitespace only — internal spaces are
    kept so a verdict like ``NEEDS REVISION`` survives.

    Args:
        lines: Report lines.
        line_re: Pattern selecting the labelled line.
        strip_re: Greedy pattern removing the label prefix.

    Returns:
        The field value, or an empty string when the label is absent.

    Examples:
        >>> extract_field(["**Verdict**: APPROVED"], _VERDICT_LINE_RE, _VERDICT_STRIP_RE)
        'APPROVED'
        >>> extract_field(["Program: prog.md  "], _PROGRAM_LINE_RE, _PROGRAM_STRIP_RE)
        'prog.md'
        >>> extract_field(["nothing here"], _VERDICT_LINE_RE, _VERDICT_STRIP_RE)
        ''
    """
    for line in lines:
        if line_re.search(line):
            value = strip_re.sub("", line.replace("**", ""), count=1)
            return _TRAILING_WS_RE.sub("", value)
    return ""


def state_program_file(state_json: Path) -> str:
    """Return ``.program_file`` from a run's ``state.json``, or an empty string.

    Args:
        state_json: Path to the source run's ``state.json``.

    Returns:
        The recorded program path, empty when unreadable or absent.
    """
    try:
        record = json.loads(state_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = record.get("program_file") if isinstance(record, dict) else None
    return value if isinstance(value, str) else ""


def _real_or_raw(path: str) -> str:
    """Resolve a path, falling back to the raw string — ``realpath`` fails on a missing path too."""
    try:
        return os.path.realpath(path, strict=True)
    except OSError:
        return path


def _sentinel_dir() -> Path:
    """Return the session temp directory (never a hardcoded ``/tmp`` — absent on native Windows)."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _session_token() -> str:
    """Return the sentinel session suffix; ``os.getppid()`` would name the calling shell, not the session."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _check_program_match(program_file: str, state_program: str) -> int:
    """Return 0 when the verdict's program matches the current experiment, else 1 after reporting."""
    if state_program and _real_or_raw(program_file) != _real_or_raw(state_program):
        print(
            f"! BLOCKED — judge verdict references program '{program_file}' "
            f"but current experiment is for '{state_program}'"
        )
        print(f"Run: /research:judge {state_program}")
        return 1
    if not Path(program_file).is_file():
        print(f"! BLOCKED — program file {program_file} referenced by judge verdict not found on disk")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Resolve the newest judge verdict and persist it for the approval gate.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when the verdict was resolved, ``1`` on any BLOCKED condition.
    """
    parser = argparse.ArgumentParser(
        prog="find_judge_verdict.py",
        description="Locate the newest judge verdict and gate ablation on an approved baseline.",
    )
    parser.add_argument("--state-dir-base", default=".experiments/state", help="Run-state base directory.")
    parser.add_argument("--run-id", default="", help="Source run id being fortified.")
    parser.add_argument("--reports-dir", default=str(_REPORTS_DIR), help="Directory holding judge-*.md reports.")
    args = parser.parse_args(argv)

    verdict_file = newest_verdict_file(Path(args.reports_dir))
    if verdict_file is None:
        print("fortify: BLOCKED — no judge verdict found in .reports/research/.")
        print("Ablation studies require an approved baseline. Run: /research:judge <program.md>")
        return 1

    lines = verdict_file.read_text(encoding="utf-8").splitlines()
    verdict = extract_field(lines, _VERDICT_LINE_RE, _VERDICT_STRIP_RE)
    program_file = extract_field(lines, _PROGRAM_LINE_RE, _PROGRAM_STRIP_RE)
    if not program_file:
        print(
            "fortify: BLOCKED — judge verdict missing Program: field; "
            "cannot verify verdict applies to current experiment."
        )
        print("Re-run: /research:judge <program.md> to generate a fresh verdict with required metadata.")
        return 1

    state_json = Path(args.state_dir_base) / args.run_id / "state.json"
    blocked = _check_program_match(program_file, state_program_file(state_json))
    if blocked:
        return blocked

    tmp = _sentinel_dir()
    csid = _session_token()
    try:
        (tmp / f"fortify-program-file-{csid}").write_text(program_file + "\n", encoding="utf-8", newline="\n")
        (tmp / f"fortify-judge-verdict-{csid}").write_text(verdict + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"find_judge_verdict: cannot write sentinel: {exc}", file=sys.stderr)
        return 1

    print(f"verdict={verdict or '-'} program={program_file} source={verdict_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
