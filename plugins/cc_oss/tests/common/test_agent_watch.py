"""Tests for ``bin/agent_watch.py``.

Batch files are written the way the orchestrator writes them (Write tool, one TSV row per agent); the spawn time is the
file's modification time, which each test sets explicitly with ``os.utime`` so deadlines are deterministic. Nothing is
mocked: the script only reads files below ``tmp_path`` and compares times passed in as arguments.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import agent_watch as aw
import pytest

_SPAWNED = 1_000_000.0


@pytest.fixture
def write_batch(tmp_path: Path) -> Callable[[str, str], Path]:
    """Return a factory writing one ``agent-watch-<batch>.tsv`` file stamped with a fixed spawn time."""

    def write(batch: str, rows: str) -> Path:
        """Write ``rows`` as the batch file for ``batch`` and stamp its modification time with the spawn time."""
        path = tmp_path / f"agent-watch-{batch}.tsv"
        path.write_text(rows, encoding="utf-8", newline="\n")
        os.utime(path, (_SPAWNED, _SPAWNED))
        return path

    return write


class TestClassify:
    """Status of one agent row from its deliverable and deadline."""

    @pytest.mark.parametrize(
        ("deliverable", "now", "expected"),
        [
            pytest.param("-", _SPAWNED + 10, "awaiting-envelope", id="envelope-in-time"),
            pytest.param("-", _SPAWNED + 999, "timed_out", id="envelope-late"),
            pytest.param("missing.md", _SPAWNED + 10, "pending", id="file-in-time"),
            pytest.param("missing.md", _SPAWNED + 999, "timed_out", id="file-late"),
        ],
    )
    def test_status_without_deliverable(self, deliverable: str, now: float, expected: str) -> None:
        """An agent with no deliverable stays open until its deadline, then is timed out, never waited on further.

        ``-`` marks an envelope-only agent and a missing path a file deliverable not yet written; both are judged only
        by the deadline (300 s after spawn).
        """
        assert aw.classify(deliverable, _SPAWNED + 300, now) == expected

    @pytest.mark.parametrize(
        ("content", "deadline", "now", "expected"),
        [
            pytest.param("findings\n", _SPAWNED, _SPAWNED + 999, "done", id="written-done-even-past-deadline"),
            pytest.param("", _SPAWNED + 300, _SPAWNED + 10, "pending", id="empty-is-not-done"),
        ],
    )
    def test_status_of_existing_deliverable_file(
        self, tmp_path: Path, content: str, deadline: float, now: float, expected: str
    ) -> None:
        """Only a non-empty deliverable file counts as done; an empty placeholder stays pending.

        A late but complete deliverable is done, since the deadline only judges missing work. An empty file is a stalled
        agent's placeholder, not a deliverable.
        """
        out = tmp_path / "out.md"
        out.write_text(content, encoding="utf-8", newline="\n")

        assert aw.classify(str(out), deadline, now) == expected


class TestWatch:
    """Classification of every agent row across all batch files in a state directory."""

    def test_reports_every_batch_and_lists_timeouts(
        self, tmp_path: Path, write_batch: Callable[[str, str], Path]
    ) -> None:
        """Agents across batches are classified together; the timed-out names are listed for the timeout report.

        The conflict batch is envelope-only and late, the impl batch has one finished and one still-running group.
        """
        done = tmp_path / "phase2-envelope-core.json"
        done.write_text("{}\n", encoding="utf-8", newline="\n")
        write_batch("conflict", "conflict-resolver\t-\t60\n")
        write_batch("impl", f"impl-core\t{done}\t900\nimpl-docs\t{tmp_path / 'phase2-envelope-docs.json'}\t900\n")

        report = aw.watch(tmp_path, _SPAWNED + 120)

        assert [(agent["name"], agent["status"], agent["elapsed_s"]) for agent in report["agents"]] == [
            ("conflict-resolver", "timed_out", 120),
            ("impl-core", "done", 120),
            ("impl-docs", "pending", 120),
        ]
        assert (report["open"], report["pending"], report["timed_out"]) == (1, ["impl-docs"], ["conflict-resolver"])

    def test_malformed_row_surfaces_as_timed_out(self, tmp_path: Path, write_batch: Callable[[str, str], Path]) -> None:
        """A row the orchestrator mistyped is reported, never silently dropped from the watch.

        The deadline column holds ``soon`` instead of a number; the agent must still show up, as timed out.
        """
        write_batch("qa", "qa-specialist\tqa.md\tsoon\n")

        assert aw.watch(tmp_path, _SPAWNED)["timed_out"] == ["qa-specialist"]

    def test_deliverable_older_than_batch_is_not_done(
        self, tmp_path: Path, write_batch: Callable[[str, str], Path]
    ) -> None:
        """A same-path file left by an earlier run does not count as this batch's deliverable.

        Run dirs and deliverable paths repeat across iterations; a stale non-empty file written before the batch file
        would otherwise close the row as done before the new agent wrote anything.
        """
        stale = tmp_path / "review.md"
        stale.write_text("old findings\n", encoding="utf-8", newline="\n")
        os.utime(stale, (_SPAWNED - 600, _SPAWNED - 600))
        write_batch("review", f"curator\t{stale}\t900\n")

        assert aw.watch(tmp_path, _SPAWNED + 10)["pending"] == ["curator"]

    def test_unreadable_batch_file_surfaces_as_timed_out(
        self, tmp_path: Path, write_batch: Callable[[str, str], Path]
    ) -> None:
        """A batch file that cannot be decoded is reported as a timed-out row instead of crashing the watch.

        The batch holds bytes that are not valid UTF-8; its agents are unknown, so the batch is flagged as ``qa?``.
        """
        write_batch("qa", "").write_bytes(b"\xff\xfe\xfa")

        assert aw.watch(tmp_path, _SPAWNED)["timed_out"] == ["qa?"]

    def test_no_batch_files_is_an_empty_report(self, tmp_path: Path) -> None:
        """Before any spawn the report is empty rather than an error.

        The state directory exists but holds no ``agent-watch-*.tsv`` file yet.
        """
        assert aw.watch(tmp_path, _SPAWNED) == {"agents": [], "open": 0, "pending": [], "timed_out": []}


class TestMain:
    """The command-line entry point: one JSON line on success, an error object and non-zero exit on failure."""

    def test_prints_one_json_line(
        self, tmp_path: Path, write_batch: Callable[[str, str], Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI prints exactly one JSON object, readable in one call per wake-up.

        The orchestrator parses stdout once per notification, so extra lines would break the parse.
        """
        write_batch("intel", f"intel\t{tmp_path / 'pr-intelligence.md'}\t300\n")

        rc = aw.main(["--state-dir", str(tmp_path)])

        lines = capsys.readouterr().out.splitlines()
        assert (rc, len(lines)) == (0, 1)
        assert json.loads(lines[0])["agents"][0]["name"] == "intel"

    def test_state_dir_without_rows_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An existing dir with no batch rows is a lost or wrong run dir, reported with a non-zero exit.

        The watch only ever runs after a spawn, so an empty report would hide every agent as "nothing outstanding".
        """
        rc = aw.main(["--state-dir", str(tmp_path)])

        assert rc == 2
        assert "no agent-watch rows" in json.loads(capsys.readouterr().out)["error"]

    def test_missing_state_dir_fails(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A lost IMPL_DIR is an error, not an empty watch that would hide every agent.

        The state directory does not exist at all, which is distinct from an existing directory without rows.
        """
        rc = aw.main(["--state-dir", str(tmp_path / "gone")])

        assert rc == 1
        assert "state dir not found" in json.loads(capsys.readouterr().out)["error"]
