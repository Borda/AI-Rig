"""Bound regex query execution and report malformed standalone index records."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from codemap_py.query import symbols


@pytest.mark.parametrize("pattern", [".*Auth.*Token.*Cache.*", r"get_\w+_by_\w+_and_\w+_or_\w+"])
def test_regular_multi_repeat_patterns_are_searchable(pattern: str) -> None:
    """Ordinary name filters are accepted rather than rejected by quantifier count."""
    assert not symbols._is_dangerous_regex(pattern)


def test_default_find_symbol_deadline_stops_nested_alternation(tmp_path: Path) -> None:
    """A valid pattern missed by structural heuristics still exits before the subprocess deadline."""
    index = tmp_path / "index.json"
    index.write_text(json.dumps(_index_with_name("a" * 80 + "!")), encoding="utf-8")

    result = _query(index, "find-symbol", "^((a|aa))+$")

    assert result.returncode == 2
    assert "timed out" in result.stderr
    assert "Traceback" not in result.stderr


def test_standalone_query_reports_malformed_record(tmp_path: Path) -> None:
    """The direct scan-query launcher emits the same structured self-check error as the dispatcher."""
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"scan_version": 11, "modules": [{"name": "m", "rdep_count": "bad"}]}))

    result = _query(index, "central")

    assert result.returncode == 3
    assert json.loads(result.stdout)["error"] == "index failed self-check"
    assert "TypeError" in result.stderr
    assert "Traceback" not in result.stderr


def test_regex_worker_deadline_needs_no_alarm(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The portable worker terminates a pathological regex without any SIGALRM facility."""
    monkeypatch.delattr(signal, "SIGALRM", raising=False)

    with pytest.raises(SystemExit) as error:
        symbols._bounded_regex_matches("^((a|aa))+$", ["a" * 80 + "!"], 0.15)

    assert error.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "timeout"


def test_worker_returns_large_match_sets_without_waiting_on_a_full_pipe() -> None:
    """The parent receives matches before joining the producer of a result larger than the pipe buffer."""
    assert symbols._bounded_regex_matches("Auth", ["AuthTokenCache"] * 10_000, 5) == list(range(10_000))


def _index_with_name(name: str) -> dict:
    """Build a validated symbol whose name exercises the regex engine directly."""
    return {
        "scan_version": 11,
        "modules": [
            {
                "name": "m",
                "path": "m.py",
                "status": "ok",
                "symbols": [{"name": name, "qualified_name": name, "type": "function", "start_line": 1, "end_line": 1}],
            }
        ],
    }


def _query(index: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the standalone entrypoint with auto-heal and telemetry disabled."""
    executable = Path(symbols.__file__).resolve().parents[3] / "bin/scan-query"
    return subprocess.run(
        [sys.executable, str(executable), "--index", str(index), "--no-heal", *arguments],
        cwd=index.parent,
        capture_output=True,
        text=True,
        timeout=8,
        env={**os.environ, "CODEMAP_LOGGING": "false", "SCAN_NO_AUTOBUILD": "1"},
        check=False,
    )
