"""Exercise explicit run authorization before ledger reads, appends, or staged deletion."""

from pathlib import Path

import append_ledger as al
import pytest


@pytest.mark.parametrize("directory", ["plugins/.reports", "nested/.notes", "source/.experiments"])
@pytest.mark.parametrize("history_authorized", [False, True])
def test_nested_artifacts_require_explicit_write_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    directory: str,
    history_authorized: bool,
) -> None:
    """Nested artifact names grant no authority, and history roots alone do not authorize staged reads."""
    monkeypatch.chdir(tmp_path)
    ledger = tmp_path / directory / "events.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record\n")
    args = [str(ledger), "--history-root", str(ledger.parent)] if history_authorized else [str(ledger)]

    assert al.main(args) == 1
    assert "outside authorized roots" in capsys.readouterr().err
    assert ledger.read_bytes() == b"earlier\n"
    assert stage.read_bytes() == b"record\n"


def test_explicit_run_root_appends_and_consumes_its_stage(tmp_path: Path) -> None:
    """Naming the active run permits the ordinary staged append without changing existing bytes."""
    run = tmp_path / "active"
    run.mkdir()
    ledger = run / "events.jsonl"
    ledger.write_bytes(b"earlier\n")
    stage = ledger.with_name(ledger.name + ".rec")
    stage.write_bytes(b"record")

    assert al.main([str(ledger), "--run-root", str(run)]) == 0
    assert ledger.read_bytes() == b"earlier\nrecord\n"
    assert not stage.exists()
