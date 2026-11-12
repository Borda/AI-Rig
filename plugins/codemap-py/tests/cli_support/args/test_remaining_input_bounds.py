"""Keep scan argument coordinates and test-impact input reads bounded."""

from __future__ import annotations

import io
import json
import shlex
from pathlib import Path

import parse_scan_args
import pytest
import summarize_test_impact


@pytest.mark.parametrize("root", ["/project's tree", "/project's", r"C:\Users\dev's work\repo"])
def test_formatted_root_round_trips_apostrophes(root: str) -> None:
    """A formatted root remains one exact argument when passed back to the parser."""
    raw = parse_scan_args.format_scan_args(["--root", root, "--incremental"])

    assert parse_scan_args.validate_scan_args(raw) is None
    assert parse_scan_args.parse_scan_args(raw) == ["--root", root, "--incremental"]
    assert shlex.split(raw) == ["--root", root, "--incremental"]


def test_oversized_query_stdout_is_not_an_empty_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Oversized valid JSON fails before the helper persists an incomplete test selection."""
    raw = json.dumps({"test_files": ["tests/test_" + "a" * 80 + ".py"]})
    monkeypatch.setattr(summarize_test_impact, "MAX_INPUT_SIZE", 32, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "bounds")

    assert summarize_test_impact.main(["--rc", "0", "--output-prefix", "codemap-p"]) == 1

    result = capsys.readouterr()
    assert result.out == ""
    assert "too large" in result.err
    assert list(tmp_path.iterdir()) == []


def test_query_stdout_read_has_a_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A streaming producer cannot force the helper to request all of stdin."""
    stream = io.StringIO("{}")
    monkeypatch.setattr(summarize_test_impact, "MAX_INPUT_SIZE", 32, raising=False)
    monkeypatch.setattr("sys.stdin", stream)
    reads: list[int] = []
    original = stream.read

    def bounded_read(size: int = -1) -> str:
        """Record the requested size without allocating an oversized input."""
        reads.append(size)
        return original(size)

    monkeypatch.setattr(stream, "read", bounded_read)
    summarize_test_impact.main(["--rc", "2", "--output-prefix", "codemap-p"])

    assert reads == [33]


def test_stderr_capture_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure diagnostics read only a capped prefix of a provider's stderr file."""
    source = tmp_path / "stderr.log"
    source.write_text("x" * 80, encoding="utf-8")
    monkeypatch.setattr(summarize_test_impact, "MAX_STDERR_SIZE", 32, raising=False)

    result = summarize_test_impact._read_stderr(str(source))

    assert result.startswith("x" * 32)
    assert "truncated" in result
    assert len(result) < 80
