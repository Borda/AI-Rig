"""The report stub helper never overwrites a final report and never turns a write failure into a stop."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import resolve_report_stub as stub


def test_later_mark_keeps_the_first_start_time(tmp_path: Path) -> None:
    """Refreshing the stub at a later step updates the step but keeps when the run first recorded one."""
    report = tmp_path / stub.REPORT_NAME
    assert stub.mark(tmp_path, "Step 9 — lint and QA gate started", "7", "/impl")[0] is stub.ReportState.INCOMPLETE
    started = next(line for line in report.read_text(encoding="utf-8").splitlines() if line.startswith("**Started**"))
    stub.mark(tmp_path, "Step 10 — push step reached", "7", "/impl")
    lines = report.read_text(encoding="utf-8").splitlines()
    assert started in lines
    assert "**Last step reached**: Step 10 — push step reached" in lines


def test_final_report_is_never_overwritten(tmp_path: Path) -> None:
    """A report whose first line is not the stub marker is final and stays byte-identical."""
    report = tmp_path / stub.REPORT_NAME
    report.write_text("## Resolve Report — PR #7\n", encoding="utf-8")
    assert stub.mark(tmp_path, "Step 10", "7", "") == (stub.ReportState.FINAL, None)
    assert report.read_text(encoding="utf-8") == "## Resolve Report — PR #7\n"


def test_run_without_pr_gets_a_local_heading(tmp_path: Path) -> None:
    """A report-mode run with no PR number is headed as a local run, never ``PR #n/a``."""
    stub.mark(tmp_path, "Step 9", "n/a", "")
    assert "## Resolve Report — local run" in (tmp_path / stub.REPORT_NAME).read_text(encoding="utf-8")


def test_unreadable_report_path_is_left_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A directory at the report path is reported and left untouched; the command still exits 0."""
    (tmp_path / stub.REPORT_NAME).mkdir()
    assert stub.main(["mark", "--run-dir", str(tmp_path), "--step", "Step 9"]) == 0
    assert capsys.readouterr().out.startswith("⚠ resolve-report stub not written")
    assert (tmp_path / stub.REPORT_NAME).is_dir()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
def test_unwritable_run_dir_warns_and_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A run dir the run cannot write is named in a warning, never a non-zero exit that would stop the gate."""
    tmp_path.chmod(0o500)
    try:
        assert stub.main(["mark", "--run-dir", str(tmp_path / "run"), "--step", "Step 9"]) == 0
    finally:
        tmp_path.chmod(0o700)
    assert capsys.readouterr().out.startswith("⚠ resolve-report stub not written")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(None, "missing", id="missing"),
        pytest.param(stub.STUB_MARKER + "\n\nbody\n", "incomplete", id="stub"),
        pytest.param("## Resolve Report — PR #7\n", "final", id="final"),
    ],
)
def test_status_names_what_the_report_path_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str | None, expected: str
) -> None:
    """``status`` prints the report state and path, so Step 11 can confirm the final write landed."""
    if content is not None:
        (tmp_path / stub.REPORT_NAME).write_text(content, encoding="utf-8")
    assert stub.main(["status", "--run-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"REPORT_STATE={expected}"
