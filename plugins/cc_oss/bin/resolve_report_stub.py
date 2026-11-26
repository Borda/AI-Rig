#!/usr/bin/env python
"""Keep a durable ``resolve-report.md`` in the run directory from the step that creates it until the final report.

Purpose: An ``oss:resolve`` run creates its report directory at the lint and QA gate, but used to write the report only
at its very last step, so a run that stopped later (left idle at the push question, cancelled, out of context) left a
directory with no record of how far it got. This helper writes a small stub marked ``incomplete`` as soon as the
directory exists and rewrites it at each later step, so every run directory names its last step reached. The final
report, written with the Write tool over the same path, replaces the stub.

Scope: Writes one file, ``<run-dir>/resolve-report.md``, and only while that file is absent or still a stub. A file
whose first line is not the stub marker is a final report and is never overwritten. The stub says how far the run got;
it never claims the run failed or was aborted, because the step after the last one recorded may still be in progress.

Usage:
    resolve_report_stub.py mark --run-dir DIR --step TEXT [--pr N] [--impl-dir DIR]
    resolve_report_stub.py status --run-dir DIR

Outputs: ``mark`` prints ``REPORT_STATE=incomplete`` after writing the stub, ``REPORT_STATE=final`` when a final
report is already there (left untouched), or a ``⚠`` line when the stub cannot be written. ``status`` prints
``REPORT_STATE=missing|incomplete|final|unreadable`` (``unreadable``: the path exists but cannot be read, such as a
directory or a file without read permission). Both print ``RESOLVE_REPORT=<path>``.

Failure: Both subcommands exit 0 on every file-system outcome, because the report record must never turn into a new
stop in the run; argparse rejects bad arguments with exit 2.

Used by: ``skills/resolve/modes/lint-qa-gate.md`` (Step 9.1), ``modes/push.md`` (Step 10) and
``modes/final-report.md`` (Step 11).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

#: First line of every stub; a report whose first line differs is final and is never overwritten.
STUB_MARKER = "<!-- resolve-report-status: incomplete -->"
#: File name of the report inside the run directory.
REPORT_NAME = "resolve-report.md"
#: Longest step or PR text kept in the stub, so a runaway value cannot bloat the file.
_MAX_FIELD = 200
#: Characters stripped from caller values before they reach the Markdown stub.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
#: Line of an existing stub that records when the first step was marked.
_STARTED_LINE = re.compile(r"^\*\*Started\*\*: (?P<when>\S+)$", re.M)


class ReportState(str, Enum):
    """What the run directory holds at the report path."""

    MISSING = "missing"
    INCOMPLETE = "incomplete"
    FINAL = "final"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class StubFields:
    """Values rendered into the stub.

    Attributes:
        step: The last step the run reached, in plain words.
        pr: PR number, or empty for a run without a PR.
        impl_dir: The run's working directory holding its state files, or empty when unknown.
        started: UTC timestamp of the first mark.
        updated: UTC timestamp of this mark.
    """

    step: str
    pr: str
    impl_dir: str
    started: str
    updated: str


def clean_field(value: str) -> str:
    """Return a caller value as one safe line of bounded length.

    Args:
        value: Raw text from the command line.

    Returns:
        The text with control characters replaced by spaces, trimmed and cut to the field limit.

    Examples:
        >>> clean_field("Step 9\\nlint")
        'Step 9 lint'
        >>> len(clean_field("x" * 500))
        200
    """
    return _CONTROL.sub(" ", value).strip()[:_MAX_FIELD]


def report_state(report: Path) -> ReportState:
    """Classify the file at the report path.

    Args:
        report: Path of ``resolve-report.md``.

    Returns:
        ``MISSING`` when no file exists, ``INCOMPLETE`` for a stub, ``UNREADABLE`` when the path cannot be read (a
        directory, no permission), ``FINAL`` for any other file.

    Examples:
        >>> report_state(Path("/nonexistent/resolve-report.md")).value
        'missing'
    """
    try:
        with report.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline().rstrip("\r\n")
    except FileNotFoundError:
        return ReportState.MISSING
    except OSError:
        return ReportState.UNREADABLE
    return ReportState.INCOMPLETE if first == STUB_MARKER else ReportState.FINAL


def render_stub(fields: StubFields) -> str:
    """Render the stub text.

    Args:
        fields: Values to show.

    Returns:
        Markdown text starting with :data:`STUB_MARKER`.

    Examples:
        >>> text = render_stub(StubFields("Step 9 — lint and QA gate started", "42", "", "t0", "t1"))
        >>> text.splitlines()[0] == STUB_MARKER, "## Resolve Report — PR #42" in text
        (True, True)
        >>> "incomplete" in text and "aborted" not in text
        True
    """
    heading = f"## Resolve Report — PR #{fields.pr}" if fields.pr.isdigit() else "## Resolve Report — local run"
    state_line = f"Run state: `{fields.impl_dir}`" if fields.impl_dir else "Run state: not recorded"
    return (
        f"{STUB_MARKER}\n\n"
        f"{heading}\n\n"
        "**Status**: incomplete — this run has not written its final report yet. The step below is the last one it "
        "recorded; the run may still be in progress, or it stopped after that step.\n\n"
        f"**Last step reached**: {fields.step}\n\n"
        f"**Started**: {fields.started}\n\n"
        f"**Updated**: {fields.updated}\n\n"
        f"{state_line}\n\n"
        "The final report replaces this file when the run reaches its last step.\n"
    )


def _now() -> str:
    """Return the current UTC time in the run-directory timestamp format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _previous_start(report: Path) -> str | None:
    """Return the ``Started`` timestamp of an existing stub, or ``None`` when there is none to keep."""
    try:
        match = _STARTED_LINE.search(report.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group("when") if match else None


def mark(run_dir: Path, step: str, pr: str, impl_dir: str) -> tuple[ReportState, str | None]:
    """Write or refresh the stub unless a final report is already in place.

    The stub is written to a temporary sibling and moved over the report path, so a reader never sees half a file.

    Args:
        run_dir: The run's report directory; created when missing.
        step: The step just reached.
        pr: PR number, ``n/a`` or empty.
        impl_dir: The run's working directory, or empty.

    Returns:
        ``(state, warning)``: the state after the call and a warning when the stub could not be written.
    """
    report = run_dir / REPORT_NAME
    state = report_state(report)
    if state is ReportState.FINAL:
        return state, None
    if state is ReportState.UNREADABLE:
        return state, f"⚠ resolve-report stub not written: {report} exists but cannot be read; left untouched"
    updated = _now()
    started = (_previous_start(report) if state is ReportState.INCOMPLETE else None) or updated
    fields = StubFields(clean_field(step), clean_field(pr), clean_field(impl_dir), started, updated)
    staging = run_dir / f".{REPORT_NAME}.{os.getpid()}.tmp"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        staging.write_text(render_stub(fields), encoding="utf-8", newline="\n")
        os.replace(staging, report)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        return state, f"⚠ resolve-report stub not written at {report}: {exc.strerror or exc}"
    return ReportState.INCOMPLETE, None


def main(argv: list[str] | None = None) -> int:
    """Run the ``mark`` or ``status`` subcommand.

    Args:
        argv: Command-line arguments; ``None`` reads ``sys.argv``.

    Returns:
        Always 0 once arguments parse (see the module docstring).
    """
    parser = argparse.ArgumentParser(description="Keep an incomplete resolve-report.md until the final report.")
    sub = parser.add_subparsers(dest="mode", required=True)
    mark_parser = sub.add_parser("mark", help="write or refresh the incomplete stub")
    mark_parser.add_argument("--run-dir", required=True, help="the run's report directory")
    mark_parser.add_argument("--step", required=True, help="the step just reached, in plain words")
    mark_parser.add_argument("--pr", default="", help="PR number; n/a or empty for a run without a PR")
    mark_parser.add_argument("--impl-dir", default="", help="the run's working directory holding its state files")
    status_parser = sub.add_parser("status", help="print what the report path holds")
    status_parser.add_argument("--run-dir", required=True, help="the run's report directory")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    run_dir = Path(args.run_dir)
    report = run_dir / REPORT_NAME
    if args.mode == "mark":
        state, warning = mark(run_dir, args.step, args.pr, args.impl_dir)
        if warning:
            print(warning)
        else:
            print(f"REPORT_STATE={state.value}")
    else:
        print(f"REPORT_STATE={report_state(report).value}")
    print(f"RESOLVE_REPORT={report.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
