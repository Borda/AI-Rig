"""Tests for ``bin/agent_watch.py``.

Batch files are written the way the orchestrator writes them (Write tool, one TSV row per agent); the spawn time is the
file's modification time, which each test sets explicitly with ``os.utime`` so deadlines are deterministic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import agent_watch as aw
import pytest

_SPAWNED = 1_000_000.0


@pytest.fixture
def write_batch(tmp_path: Path):
    """Return a factory writing one ``agent-watch-<batch>.tsv`` file stamped with a fixed spawn time."""

    def _write(batch: str, rows: str) -> Path:
        path = tmp_path / f"agent-watch-{batch}.tsv"
        path.write_text(rows, encoding="utf-8", newline="\n")
        os.utime(path, (_SPAWNED, _SPAWNED))
        return path

    return _write


class TestClassify:
    @pytest.mark.parametrize(
        ("deliverable", "now", "expected"),
        [
            pytest.param("-", _SPAWNED + 10, "awaiting-envelope", id="envelope-in-time"),
            pytest.param("-", _SPAWNED + 999, "timed_out", id="envelope-late"),
            pytest.param("missing.md", _SPAWNED + 10, "pending", id="file-in-time"),
            pytest.param("missing.md", _SPAWNED + 999, "timed_out", id="file-late"),
        ],
    )
    def test_status_without_a_deliverable(self, deliverable: str, now: float, expected: str) -> None:
        """An agent with no deliverable stays open until its deadline, then is timed out, never waited on further."""
        assert aw.classify(deliverable, _SPAWNED + 300, now) == expected

    def test_written_deliverable_is_done_even_past_deadline(self, tmp_path: Path) -> None:
        """A late but complete deliverable counts as done; the deadline only judges missing work."""
        out = tmp_path / "out.md"
        out.write_text("findings\n", encoding="utf-8", newline="\n")
        assert aw.classify(str(out), _SPAWNED, _SPAWNED + 999) == "done"

    def test_empty_deliverable_is_not_done(self, tmp_path: Path) -> None:
        """An empty file is a stalled agent's placeholder, not a deliverable."""
        out = tmp_path / "out.md"
        out.write_text("", encoding="utf-8", newline="\n")
        assert aw.classify(str(out), _SPAWNED + 300, _SPAWNED + 10) == "pending"


class TestWatch:
    def test_reports_every_batch_and_lists_timeouts(self, tmp_path: Path, write_batch) -> None:
        """Agents across batches are classified together; the timed-out names are listed for the ⏱ report.

        The conflict batch is envelope-only and late, the impl batch has one finished and one still-running group.
        """
        done = tmp_path / "phase2-envelope-core.json"
        done.write_text("{}\n", encoding="utf-8", newline="\n")
        write_batch("conflict", "conflict-resolver\t-\t60\n")
        write_batch("impl", f"impl-core\t{done}\t900\nimpl-docs\t{tmp_path / 'phase2-envelope-docs.json'}\t900\n")
        report = aw.watch(tmp_path, _SPAWNED + 120)
        assert [(agent["name"], agent["status"]) for agent in report["agents"]] == [
            ("conflict-resolver", "timed_out"),
            ("impl-core", "done"),
            ("impl-docs", "pending"),
        ]
        assert (report["open"], report["pending"], report["timed_out"]) == (1, ["impl-docs"], ["conflict-resolver"])
        assert [agent["elapsed_s"] for agent in report["agents"]] == [120, 120, 120]

    def test_malformed_row_surfaces_as_timed_out(self, tmp_path: Path, write_batch) -> None:
        """A row the orchestrator mistyped is reported, never silently dropped from the watch."""
        write_batch("qa", "qa-specialist\tqa.md\tsoon\n")
        assert aw.watch(tmp_path, _SPAWNED)["timed_out"] == ["qa-specialist"]

    def test_no_batch_files_is_an_empty_report(self, tmp_path: Path) -> None:
        """Before any spawn the report is empty rather than an error."""
        assert aw.watch(tmp_path, _SPAWNED) == {"agents": [], "open": 0, "pending": [], "timed_out": []}


class TestMain:
    def test_prints_one_json_line(self, tmp_path: Path, write_batch, capsys: pytest.CaptureFixture) -> None:
        """The CLI prints exactly one JSON object, readable in one call per wake-up."""
        write_batch("intel", f"intel\t{tmp_path / 'pr-intelligence.md'}\t300\n")
        assert aw.main(["--state-dir", str(tmp_path)]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["agents"][0]["name"] == "intel"

    def test_missing_state_dir_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A lost IMPL_DIR is an error, not an empty watch that would hide every agent."""
        assert aw.main(["--state-dir", str(tmp_path / "gone")]) == 1
        assert "state dir not found" in json.loads(capsys.readouterr().out)["error"]
