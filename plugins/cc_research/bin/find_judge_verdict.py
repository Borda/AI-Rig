#!/usr/bin/env python
"""find_judge_verdict.py — locate the newest judge verdict and gate ablation on an approved baseline.

Extracted from ``skills/fortify/SKILL.md`` step F1. Picks the most recent matching
``.reports/research/judge-*.md``, pulls its ``Verdict:`` and ``Program:`` fields, and refuses to
continue unless that verdict was issued for the experiment currently being fortified — otherwise
fortify could ablate against some other program's approval.
When the report carries ``Program SHA256:``, it must match the current program bytes;
older reports without it are admitted with a note.

Only the ``Verdict:`` field of the judge report is authoritative; ``methodology_rating: sound``
is one input to a verdict, not the verdict.

Usage:
    python "${CLAUDE_PLUGIN_ROOT}/bin/find_judge_verdict.py" --state-dir-base DIR --run-id ID

Sentinels written (newline-terminated — the ``IFS= read -r`` reload contract needs it):
    ${TMPDIR}/fortify-program-file-${CSID}   — consumed by F6
    ${TMPDIR}/fortify-judge-verdict-${CSID}  — consumed by the gate-on-sentinel.py block

Exit codes:
    0 — verdict and program resolved, sentinels written
    1 — BLOCKED: no verdict file, no Program field, source run without program_file, program mismatch,
        or program missing on disk
    2 — argument error (argparse default)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

#: Sibling import: bin/ is not a package. Putting it on the path resolves the private writer whether this file runs as
#: a script, under the test conftest, or as a doctest module.
_BIN_DIR = str(Path(__file__).resolve().parent)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

from _sentinel_io import write_private_text  # noqa: E402

#: Matches a judge report line that starts with a Verdict label, allowing Markdown bold markers and any letter case.
_VERDICT_LINE_RE = re.compile(r"^\**verdict\**:", re.IGNORECASE)
#: Matches a judge report line that starts with a Program, program_file or program file label.
_PROGRAM_LINE_RE = re.compile(r"^\**(program(_file)?|program file)\**:", re.IGNORECASE)
_PROGRAM_HASH_LINE_RE = re.compile(r"^\**program[ _-]sha-?256\**:", re.IGNORECASE)
#: Case-sensitive on purpose — faithful to the ``sed -E 's/.*[Vv]erdict[: ]+//'`` it replaces,
#: which leaves an all-caps ``VERDICT:`` label in place.
_VERDICT_STRIP_RE = re.compile(r".*[Vv]erdict[: ]+")
#: Strips everything through the first colon and following whitespace, leaving the program file value.
_PROGRAM_STRIP_RE = re.compile(r".*:[ \t\r\f\v]*")
#: Matches trailing horizontal whitespace so it can be removed from an extracted value.
_TRAILING_WS_RE = re.compile(r"[ \t\r\f\v]*$")

#: Default directory searched for judge reports, used when --reports-dir is not given.
_REPORTS_DIR = Path(".reports/research")
MAX_JSONL_BYTES = 64 * 1024 * 1024


def _read_limited_text(path: Path) -> str:
    """Read UTF-8 metadata within the run-ledger byte ceiling."""
    with path.open("rb") as stream:
        content = stream.read(MAX_JSONL_BYTES + 1)
    if len(content) > MAX_JSONL_BYTES:
        raise ValueError(f"{path} exceeds {MAX_JSONL_BYTES} bytes")
    return content.decode("utf-8")


def newest_verdict_file(reports_dir: Path, program_file: str | None = None) -> Path | None:
    """Return the most recently modified report matching the optional program path.

    Args:
        reports_dir: Directory holding judge reports.
        program_file: Restrict selection to reports for this program when provided.

    Returns:
        The newest report path, or ``None`` when the directory holds none.
    """
    candidates = [p for p in reports_dir.glob("judge-*.md") if p.is_file()]
    for path in sorted(candidates, key=lambda p: (-p.stat().st_mtime, p.name)):
        if program_file is None:
            return path
        program = extract_field(_read_limited_text(path).splitlines(), _PROGRAM_LINE_RE, _PROGRAM_STRIP_RE)
        if program and _real_or_raw(program) == _real_or_raw(program_file):
            return path
    return None


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
        record = json.loads(_read_limited_text(state_json))
    except (OSError, ValueError, ArithmeticError, RecursionError):
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
    tmp_fallback = tempfile.gettempdir() if os.name == "nt" else "/tmp"  # noqa: S108 - shell default of ${TMPDIR:-/tmp}
    return Path(os.environ.get("TMPDIR") or tmp_fallback)


def _session_token() -> str:
    """Return the sentinel session suffix; ``os.getppid()`` would name the calling shell, not the session."""
    return os.environ.get("CSID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "shared"


def _check_program_match(program_file: str, state_program: str) -> int:
    """Return 0 when the verdict's program matches the current experiment, else 1 after reporting.

    An empty ``state_program`` (text-goal run with ``program_file: null``, or an unreadable
    ``state.json``) blocks: with nothing to cross-match, the newest judge report of any other
    program would otherwise satisfy the approval gate.
    """
    if not state_program:
        print(
            f"! BLOCKED — source run records no program_file (text-goal run or unreadable state.json); "
            f"cannot confirm judge verdict for '{program_file}' applies to it"
        )
        print("Fortify needs a program-file run: /research:run <program.md>, then /research:judge <program.md>")
        return 1
    if _real_or_raw(program_file) != _real_or_raw(state_program):
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


def _check_program_hash(program_file: str, lines: list[str]) -> bool:
    """Validate content binding when present, admitting older reports with a note."""
    if not any(_PROGRAM_HASH_LINE_RE.search(line) for line in lines):
        print(
            "fortify: note — judge report predates content binding; program edits since judgment are not detected",
            file=sys.stderr,
        )
        return True
    expected = extract_field(lines, _PROGRAM_HASH_LINE_RE, _PROGRAM_STRIP_RE)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        print("fortify: BLOCKED — judge verdict missing valid Program SHA256: metadata; re-run /research:judge")
        return False
    digest = hashlib.sha256()
    try:
        with Path(program_file).open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        print(f"fortify: BLOCKED — cannot hash program: {exc}")
        return False
    if digest.hexdigest() != expected.lower():
        print(f"fortify: BLOCKED — program content changed since judgment; re-run /research:judge {program_file}")
        return False
    return True


def _state_path(state_dir_base: str, run_id: str) -> Path:
    """Resolve a single run's state inside the project or its experiments directory."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id) or ".." in run_id or run_id == ".":
        raise ValueError("invalid run identity")
    root = Path.cwd().resolve()
    roots = (root, (root / ".experiments").resolve())
    base = Path(state_dir_base).resolve()
    state = (base / run_id / "state.json").resolve()
    if not all(any(path.is_relative_to(allowed) for allowed in roots) for path in (base, state)):
        raise ValueError("run state escapes project root")
    return state


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
    csid = _session_token()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", csid) or ".." in csid:
        print("find_judge_verdict: invalid sentinel session identity", file=sys.stderr)
        return 2

    try:
        state_json = _state_path(args.state_dir_base, args.run_id)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"find_judge_verdict: {exc}", file=sys.stderr)
        return 2
    state_program = state_program_file(state_json)
    try:
        reports = Path(args.reports_dir)
        verdict_file = newest_verdict_file(reports, state_program or None) or newest_verdict_file(reports)
        lines = _read_limited_text(verdict_file).splitlines() if verdict_file else []
    except (OSError, ValueError) as exc:
        print(f"fortify: BLOCKED — cannot read judge report: {exc}")
        return 1
    if verdict_file is None:
        print("fortify: BLOCKED — no judge verdict found in .reports/research/.")
        print("Ablation studies require an approved baseline. Run: /research:judge <program.md>")
        return 1

    verdict = extract_field(lines, _VERDICT_LINE_RE, _VERDICT_STRIP_RE)
    program_file = extract_field(lines, _PROGRAM_LINE_RE, _PROGRAM_STRIP_RE)
    if not program_file:
        print(
            "fortify: BLOCKED — judge verdict missing Program: field; "
            "cannot verify verdict applies to current experiment."
        )
        print("Re-run: /research:judge <program.md> to generate a fresh verdict with required metadata.")
        return 1

    blocked = _check_program_match(program_file, state_program)
    if blocked:
        return blocked
    if not _check_program_hash(program_file, lines):
        return 1

    tmp = _sentinel_dir()
    try:
        write_private_text(tmp / f"fortify-program-file-{csid}", program_file + "\n")
        write_private_text(tmp / f"fortify-judge-verdict-{csid}", verdict + "\n")
    except OSError as exc:
        print(f"find_judge_verdict: cannot write sentinel: {exc}", file=sys.stderr)
        return 1

    print(f"verdict={verdict or '-'} program={program_file} source={verdict_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
