"""Tests for the append_ledger bin script.

Covers the append-only contract: staged ``.rec`` files land at the end of the ledger, earlier records are never
rewritten, staged files are consumed, and ``--from`` sources are appended but kept.
"""

from __future__ import annotations

from pathlib import Path

import append_ledger as al
import pytest


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """Ledger holding one earlier record, as a run would have after its first iteration."""
    path = tmp_path / "experiments.jsonl"
    path.write_bytes(b'{"iteration":0}\n')
    return path


class TestStagedRecords:
    """Covers the ``<ledger>.rec`` staging path that skills use for model-valued records."""

    def test_appends_after_existing_records(self, ledger: Path) -> None:
        """A staged record lands after the earlier ones, which stay byte-identical.

        This is the whole point of the ledger contract: history is never re-emitted, so it cannot be dropped or
        reworded.
        """
        ledger.with_name("experiments.jsonl.rec").write_bytes(b'{"iteration":1}\n')
        assert al.main([str(ledger)]) == 0
        assert ledger.read_bytes() == b'{"iteration":0}\n{"iteration":1}\n'

    def test_consumes_staged_file(self, ledger: Path) -> None:
        """The staged file is deleted, so the next Write creates it fresh instead of overwriting an unread file."""
        staged = ledger.with_name("experiments.jsonl.rec")
        staged.write_bytes(b'{"iteration":1}\n')
        al.main([str(ledger)])
        assert not staged.exists()

    def test_terminates_record_missing_newline(self, ledger: Path) -> None:
        """A record saved without a trailing newline cannot glue the next record onto its line.

        The Write tool does not guarantee a final newline; two flushes in a row must still yield two JSONL lines.
        """
        staged = ledger.with_name("experiments.jsonl.rec")
        staged.write_bytes(b'{"iteration":1}')
        al.main([str(ledger)])
        staged.write_bytes(b'{"iteration":2}')
        al.main([str(ledger)])
        assert ledger.read_bytes().splitlines() == [b'{"iteration":0}', b'{"iteration":1}', b'{"iteration":2}']

    def test_sweeps_parallel_writer_files_in_name_order(self, ledger: Path) -> None:
        """Each parallel writer stages its own file; one call appends all of them, sorted by writer name."""
        ledger.with_name("experiments.jsonl.b.rec").write_bytes(b'{"w":"b"}\n')
        ledger.with_name("experiments.jsonl.a.rec").write_bytes(b'{"w":"a"}\n')
        al.main([str(ledger)])
        assert ledger.read_bytes().splitlines()[1:] == [b'{"w":"a"}', b'{"w":"b"}']

    def test_creates_absent_ledger(self, tmp_path: Path) -> None:
        """The first staged record creates the ledger, so callers need no separate initial Write."""
        ledger = tmp_path / "nested" / "log.jsonl"
        ledger.parent.mkdir()
        ledger.with_name("log.jsonl.rec").write_bytes(b"x\n")
        assert al.main([str(ledger)]) == 0
        assert ledger.read_bytes() == b"x\n"

    def test_nothing_staged_is_a_clean_no_op(self, ledger: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A flush with no staged record exits 0 and leaves the ledger untouched."""
        assert al.main([str(ledger)]) == 0
        assert ledger.read_bytes() == b'{"iteration":0}\n'
        assert "nothing staged" in capsys.readouterr().out


class TestFromSources:
    """Covers ``--from``, used when the records already exist as files on disk."""

    def test_appends_sources_and_keeps_them(self, ledger: Path, tmp_path: Path) -> None:
        """Source files are appended in argument order and left in place for their own run directory."""
        first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        first.write_bytes(b'{"t":"a"}')
        second.write_bytes(b'{"t":"b"}\n')
        assert al.main([str(ledger), "--from", str(first), str(second)]) == 0
        assert ledger.read_bytes().splitlines()[1:] == [b'{"t":"a"}', b'{"t":"b"}']
        assert first.exists()

    def test_skips_missing_source(self, ledger: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An unmatched shell glob arrives as a literal path; it is reported, not fatal."""
        assert al.main([str(ledger), "--from", str(tmp_path / "*" / "result.jsonl")]) == 0
        assert "skipped missing source" in capsys.readouterr().err

    def test_leaves_staged_records_alone(self, ledger: Path, tmp_path: Path) -> None:
        """A ``--from`` call never consumes a ``.rec`` another step staged for its own flush."""
        staged = ledger.with_name("experiments.jsonl.rec")
        staged.write_bytes(b"pending\n")
        source = tmp_path / "r.jsonl"
        source.write_bytes(b"r\n")
        al.main([str(ledger), "--from", str(source)])
        assert staged.exists()
