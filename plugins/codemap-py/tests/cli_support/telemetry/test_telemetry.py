"""Tests for ``bin/_telemetry.py`` — shared per-session cli.jsonl logging.

Covers:
* log_path_for — per-session filename, fallback, unsafe-char sanitization
* session_id — reads the seeded tmpfile; empty when absent
* log_cli — writes a per-session record; falls back to cli.jsonl with no session
* CODEMAP_LOGGING=false disables all writes
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import _telemetry as t
import pytest


@pytest.fixture(name="logdir")
def _logdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run in a private cwd/TMPDIR with logging enabled and return the (not yet created) log directory."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("CODEMAP_LOGGING", raising=False)
    return tmp_path / "logs"


@pytest.mark.parametrize(
    ("session", "filename"),
    [
        pytest.param("abc", "cli_abc.jsonl", id="plain-session"),
        pytest.param("", "cli.jsonl", id="no-session-unsuffixed"),
        pytest.param("a/b c", "cli_a-b-c.jsonl", id="unsafe-characters-sanitized"),
    ],
)
def test_log_path_for(session: str, filename: str) -> None:
    """The shard name carries the session id, falls back to ``cli.jsonl`` and never keeps path-unsafe characters.

    Sessions come from the host environment, so a separator or space in one must not escape or split the log file.
    """
    assert t.log_path_for(session, Path("/l")).name == filename


@mock.patch("_telemetry.subprocess.check_output", autospec=True, side_effect=OSError("no git"))
def test_unseeded_session_logs_to_unsuffixed_file(mock_git: mock.Mock, logdir: Path) -> None:
    """With no seeded session the id is empty and the record lands in the unsuffixed ``cli.jsonl``.

    Git lookup is made unavailable so the session id can only come from the seeded marker file in TMPDIR.
    """
    session = t.session_id()
    t.log_cli("central", ["central"], {"x": 1}, 0.0, log_dir=logdir)

    assert session == ""
    assert (logdir / "cli.jsonl").exists()


@mock.patch("_telemetry.subprocess.check_output", autospec=True, side_effect=OSError("no git"))
def test_seeded_session_logs_per_session_file(mock_git: mock.Mock, logdir: Path, tmp_path: Path) -> None:
    """A seeded session gives a per-session filename and populates the record's session field.

    The marker ``codemap-<project>-session`` in TMPDIR is what the session-seeding hook writes; project is the cwd
    basename here because git is unavailable.
    """
    (tmp_path / f"codemap-{tmp_path.name}-session").write_text("sess9")

    session = t.session_id()
    t.log_cli("rdeps", ["rdeps", "m"], {"y": 2}, 0.0, log_dir=logdir)

    assert session == "sess9"
    rec = json.loads((logdir / "cli_sess9.jsonl").read_text().strip())
    assert rec["cmd"] == "rdeps"
    assert rec["session"] == "sess9"
    assert rec["layer"] == "cli"
    assert rec["result"] == {"y": 2}


def test_logging_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``CODEMAP_LOGGING=false`` disables every write, including creating the log directory."""
    monkeypatch.setenv("CODEMAP_LOGGING", "false")
    logdir = tmp_path / "logs"

    t.log_cli("x", [], {}, 0.0, log_dir=logdir)

    assert not logdir.exists()


@mock.patch("_telemetry.subprocess.check_output", autospec=True, side_effect=OSError("no git"))
def test_source_tag_from_env(mock_git: mock.Mock, logdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CODEMAP_TELEMETRY_SOURCE stamps records so debrief can drop scripted load.

    A tagged run (benchmarks, scripts) must be distinguishable from organic usage. Every record also stamps the plugin
    version for before/after release comparison.
    """
    monkeypatch.setenv("CODEMAP_TELEMETRY_SOURCE", "bench")

    t.log_cli("central", ["central"], {"x": 1}, 0.0, log_dir=logdir)

    (record,) = [json.loads(line) for line in (logdir / "cli.jsonl").read_text().splitlines()]
    assert record["source"] == "bench"
    assert record["v"] not in ("", "?")


@mock.patch("_telemetry.subprocess.check_output", autospec=True, side_effect=OSError("no git"))
def test_no_source_tag_by_default(mock_git: mock.Mock, logdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Untagged (organic) records carry no source field at all.

    An empty ``source`` key would make organic usage look like a tagged run in the debrief statistics.
    """
    monkeypatch.delenv("CODEMAP_TELEMETRY_SOURCE", raising=False)

    t.log_cli("central", ["central"], {"x": 1}, 0.0, log_dir=logdir)

    (record,) = [json.loads(line) for line in (logdir / "cli.jsonl").read_text().splitlines()]
    assert "source" not in record
