"""Telemetry appends reject redirected filesystem leaves."""

import json
import time
from pathlib import Path

import pytest
from _host_skips import skip_file_symlink_unavailable
from codemap_py import telemetry


@skip_file_symlink_unavailable
def test_cli_log_preserves_symlink_victim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A planted CLI shard symlink never appends telemetry into its victim."""
    logs = tmp_path / "logs" / "direct"
    logs.mkdir(parents=True)
    victim = tmp_path / "private.txt"
    victim.write_text("private data")
    (logs / "cli_append-test.jsonl").symlink_to(victim)
    monkeypatch.setenv("CODEMAP_LOGGING", "true")
    monkeypatch.setenv("CODEMAP_RUNTIME", "direct")
    monkeypatch.setenv("CODEMAP_TELEMETRY_SESSION", "append-test")
    monkeypatch.setenv("CODEMAP_LOG_DIR", str(logs.parent))

    telemetry.log_cli("query", [], {}, time.time(), root=tmp_path)

    assert victim.read_text() == "private data"


def test_cli_log_still_appends_multiple_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Protected append preserves preceding complete records and writes the next complete record."""
    monkeypatch.setenv("CODEMAP_LOGGING", "true")
    monkeypatch.setenv("CODEMAP_RUNTIME", "direct")
    monkeypatch.setenv("CODEMAP_TELEMETRY_SESSION", "append-test")
    monkeypatch.setenv("CODEMAP_LOG_DIR", str(tmp_path / "logs"))
    telemetry.log_cli("first", [], {}, time.time(), root=tmp_path)
    telemetry.log_cli("second", [], {}, time.time(), root=tmp_path)
    records = (tmp_path / "logs/direct/cli_append-test.jsonl").read_text().splitlines()
    assert [json.loads(record)["cmd"] for record in records] == ["first", "second"]


@skip_file_symlink_unavailable
def test_default_cli_log_stays_inside_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A default cache directory symlink cannot redirect CLI log creation outside the project."""
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".cache").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("CODEMAP_LOGGING", "true")
    monkeypatch.setenv("CODEMAP_RUNTIME", "direct")
    monkeypatch.delenv("CODEMAP_LOG_DIR", raising=False)

    telemetry.log_cli("query", [], {}, time.time(), root=project)

    assert list(outside.iterdir()) == []
