"""Protect report outputs and growing baselines from linked filesystem paths."""

from __future__ import annotations

from pathlib import Path

import _context_composition
import cost_analyzer
import efficiency_measured
import pytest
from _hook_env import _symlinks_supported

_requires_symlinks = pytest.mark.skipif(not _symlinks_supported(), reason="host cannot create symlinks")


@_requires_symlinks
def test_cost_report_replaces_link_without_overwriting_target(tmp_path: Path) -> None:
    """Writing a report never modifies the file named by an existing output symlink."""
    protected = tmp_path / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    output = tmp_path / "report.md"
    output.symlink_to(protected)
    cost_analyzer._write_report(["report"], str(output))
    assert protected.read_text(encoding="utf-8") == "keep"
    assert not output.is_symlink()
    assert output.read_text(encoding="utf-8") == "report\n"


@_requires_symlinks
def test_cost_report_refuses_linked_parent(tmp_path: Path) -> None:
    """A linked output directory cannot redirect report writes into another directory."""
    protected = tmp_path / "protected"
    protected.mkdir()
    output = tmp_path / "reports"
    output.symlink_to(protected, target_is_directory=True)
    with pytest.raises(OSError, match="symlink"):
        cost_analyzer._write_report(["report"], str(output / "report.md"))
    assert list(protected.iterdir()) == []


@_requires_symlinks
def test_baseline_append_refuses_linked_ledger(tmp_path: Path) -> None:
    """Appending a baseline cannot follow a ledger link and modify its target."""
    protected = tmp_path / "protected.jsonl"
    protected.write_text("keep", encoding="utf-8")
    ledger = tmp_path / "baseline.jsonl"
    ledger.symlink_to(protected)
    with pytest.raises(OSError, match="symlink"):
        efficiency_measured.append_record(ledger, "/example:run", {"sessions": 1})
    assert protected.read_text(encoding="utf-8") == "keep"


def test_validated_transcript_rows_are_streamed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A consumer can read the first validated row before a later row is decoded."""
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type":"user"}\nnot-json\n{"type":"assistant"}\n', encoding="utf-8")
    rows = _context_composition._read_numbered_rows(transcript, cost_analyzer)
    assert iter(rows) is rows
    assert next(rows) == (1, {"type": "user"})
    assert capsys.readouterr().err == ""
    assert next(rows) == (3, {"type": "assistant"})
    assert "malformed JSON" in capsys.readouterr().err
    assert list(rows) == []
