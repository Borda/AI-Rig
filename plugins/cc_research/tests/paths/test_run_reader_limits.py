"""Run metadata and metric readers reject excessive input without losing valid history."""

from __future__ import annotations

import os
from pathlib import Path

import find_run_id
import pytest
import run_metric_stats as stats


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"count":' + "9" * 5000 + "}", id="integer-digit-limit"),
        pytest.param("[" * 10000 + "0" + "]" * 10000, id="deep-json"),
    ],
)
def test_unusable_run_state_does_not_hide_completed_run(tmp_path: Path, payload: str) -> None:
    """Unparsable candidate metadata is skipped while a valid completed run remains discoverable."""
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "state.json").write_text(payload, encoding="utf-8")
    completed = tmp_path / "completed"
    completed.mkdir()
    (completed / "state.json").write_text('{"status":"completed"}', encoding="utf-8")
    os.utime(completed, (100, 100))
    os.utime(invalid, (200, 200))

    assert find_run_id.find_run_id(tmp_path) == "completed"


def test_oversized_run_state_is_unusable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid JSON object beyond the cap cannot supply a keep configuration."""
    monkeypatch.setattr(stats, "MAX_JSONL_BYTES", 8, raising=False)
    (tmp_path / "state.json").write_bytes(b'{"config":{}}')

    assert stats.load_state(tmp_path) is None


def test_state_at_byte_cap_remains_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is inclusive and measures encoded bytes."""
    monkeypatch.setattr(stats, "MAX_JSONL_BYTES", 13, raising=False)
    (tmp_path / "state.json").write_bytes(b'{"config":{}}')

    assert stats.load_state(tmp_path) == {"config": {}}


def test_oversized_metric_ledger_blocks_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An oversized history cannot appear empty or produce a keep measurement."""
    monkeypatch.setattr(stats, "MAX_JSONL_BYTES", 24, raising=False)
    (tmp_path / "state.json").write_bytes(b"{}")
    (tmp_path / "metric-runs.jsonl").write_bytes(b'{"iteration":1,"metric":2}\n')

    assert stats.main(["status", str(tmp_path), "--iteration", "1"]) == 1
    output = capsys.readouterr().out
    assert "exceeds 24 bytes" in output
    assert "RUN_METRIC_NEXT_RUN" not in output
    assert "RUN_METRIC_DONE" not in output


def test_metric_ledger_at_byte_cap_remains_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A complete record exactly at the ceiling remains part of its iteration history."""
    record = b'{"iteration":1,"metric":2}\n'
    monkeypatch.setattr(stats, "MAX_JSONL_BYTES", len(record), raising=False)
    (tmp_path / "metric-runs.jsonl").write_bytes(record)

    assert stats.iteration_runs(tmp_path, 1) == [{"iteration": 1, "metric": 2}]
