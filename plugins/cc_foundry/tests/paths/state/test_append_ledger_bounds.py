"""Exercise bounded ledger reads and explicit run/history authorization."""

from pathlib import Path

import append_ledger as al
import pytest


@pytest.mark.parametrize("staged", [True, False])
def test_oversized_input_preserves_ledger_and_stage(
    tmp_path: Path, staged: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """An oversized staged or existing source fails before any ledger bytes are appended."""
    ledger = tmp_path / "events.jsonl"
    ledger.write_bytes(b"earlier\n")
    source = tmp_path / ("events.jsonl.rec" if staged else "source.jsonl")
    with source.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    args = [str(ledger)] if staged else [str(ledger), "--from", str(source)]

    assert al.main(["--run-root", str(tmp_path), *args]) == 1
    assert "E_INPUT_TOO_LARGE" in capsys.readouterr().err
    assert ledger.read_bytes() == b"earlier\n"
    assert source.exists()


def test_run_root_rejects_sibling_ledger(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An explicit run root cannot authorize a ledger in another run directory."""
    run = tmp_path / "active"
    run.mkdir()
    ledger = tmp_path / "other.jsonl"
    ledger.with_suffix(".jsonl.rec").write_bytes(b"record\n")

    assert al.main([str(ledger), "--run-root", str(run)]) == 1
    assert "outside authorized roots" in capsys.readouterr().err
    assert not ledger.exists()


def test_run_root_rejects_sibling_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The source of a same-run append cannot escape into another run."""
    run = tmp_path / "active"
    run.mkdir()
    ledger = run / "events.jsonl"
    source = tmp_path / "other.jsonl"
    source.write_bytes(b"other run\n")

    assert al.main([str(ledger), "--run-root", str(run), "--from", str(source)]) == 1
    assert "outside authorized roots" in capsys.readouterr().err
    assert not ledger.exists()


def test_history_root_authorizes_cross_run_append(tmp_path: Path) -> None:
    """A separately named history directory admits a run result while preserving its source."""
    run = tmp_path / "active"
    run.mkdir()
    history = tmp_path / "history"
    history.mkdir()
    source = run / "result.jsonl"
    source.write_bytes(b"result\n")
    ledger = history / "all.jsonl"

    assert al.main([str(ledger), "--run-root", str(run), "--history-root", str(history), "--from", str(source)]) == 0
    assert ledger.read_bytes() == b"result\n"
    assert source.read_bytes() == b"result\n"


def test_unapproved_history_root_is_refused(tmp_path: Path) -> None:
    """A history destination requires its own authorization even with a valid run source."""
    run = tmp_path / "active"
    run.mkdir()
    source = run / "result.jsonl"
    source.write_bytes(b"result\n")
    ledger = tmp_path / "all.jsonl"

    assert al.main([str(ledger), "--run-root", str(run), "--from", str(source)]) == 1
    assert not ledger.exists()


def test_input_budget_is_shared_before_any_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Several small sources cannot bypass the batch budget or leave a partially appended ledger."""
    monkeypatch.setattr(al, "MAX_INPUT_BYTES", 8)
    ledger = tmp_path / "all.jsonl"
    ledger.write_bytes(b"earlier\n")
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_bytes(b"12345")
    second.write_bytes(b"6789")

    assert al.main([str(ledger), "--run-root", str(tmp_path), "--from", str(first), str(second)]) == 1
    assert "E_INPUT_TOO_LARGE" in capsys.readouterr().err
    assert ledger.read_bytes() == b"earlier\n"


def test_exact_input_budget_allows_missing_newline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The read limit counts input bytes, allowing the helper to supply its final newline."""
    monkeypatch.setattr(al, "MAX_INPUT_BYTES", 8)
    ledger = tmp_path / "all.jsonl"
    ledger.with_suffix(".jsonl.rec").write_bytes(b"12345678")

    assert al.main([str(ledger), "--run-root", str(tmp_path)]) == 0
    assert ledger.read_bytes() == b"12345678\n"


def test_source_root_cannot_authorize_a_ledger(tmp_path: Path) -> None:
    """A legacy migration source root provides no authority to write legacy history."""
    run = tmp_path / "active"
    run.mkdir()
    ledger = tmp_path / "legacy.jsonl"
    ledger.with_suffix(".jsonl.rec").write_bytes(b"record\n")

    assert al.main([str(ledger), "--run-root", str(run), "--source-root", str(tmp_path)]) == 1
    assert not ledger.exists()


def test_missing_run_root_refuses_nonartifact_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omitting a run root cannot authorize an ordinary project file or system temp storage."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / "all.jsonl"
    stage = ledger.with_suffix(".jsonl.rec")
    stage.write_bytes(b"record\n")

    assert al.main([str(ledger)]) == 1
    assert "outside authorized roots" in capsys.readouterr().err
    assert not ledger.exists()
    assert stage.read_bytes() == b"record\n"


def test_history_destination_does_not_authorize_staged_sources(tmp_path: Path) -> None:
    """A history write root cannot silently authorize reading and deleting stages outside the run."""
    run = tmp_path / "active"
    run.mkdir()
    ledger = tmp_path / "all.jsonl"
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_suffix(".jsonl.rec")
    stage.write_bytes(b"record\n")

    assert al.main([str(ledger), "--run-root", str(run), "--history-root", str(tmp_path)]) == 1
    assert ledger.read_bytes() == b"earlier\n"
    assert stage.read_bytes() == b"record\n"


def test_history_migration_reads_only_the_explicit_source_root(tmp_path: Path) -> None:
    """The calibration migration can copy legacy history without authorizing writes to its source tree."""
    run = tmp_path / "active"
    run.mkdir()
    history = tmp_path / "history"
    history.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    source = legacy / "all.jsonl"
    source.write_bytes(b"legacy\n")
    ledger = history / "all.jsonl"

    assert (
        al.main(
            [
                str(ledger),
                "--run-root",
                str(run),
                "--history-root",
                str(history),
                "--source-root",
                str(legacy),
                "--from",
                str(source),
            ]
        )
        == 0
    )
    assert ledger.read_bytes() == b"legacy\n"
    assert source.read_bytes() == b"legacy\n"
