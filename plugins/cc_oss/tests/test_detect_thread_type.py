"""Tests for ``bin/detect_thread_type.py``.

Arg-parsing, URL extraction, and drift-computation tests run without any subprocess calls. Subprocess-dependent paths
monkeypatch ``subprocess.run`` and ``which`` so no real ``gh`` invocation occurs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import detect_thread_type as dtt


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        """Store the status and output consumed by the detector."""
        self.returncode = returncode
        self.stdout = stdout


# ---------------------------------------------------------------------------
# parse_number — URL formats and #N variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("123", "123", id="bare-int"),
        pytest.param("#123", "123", id="hash-prefix"),
        pytest.param("  42  ", "42", id="surrounding-whitespace"),
        pytest.param("https://github.com/owner/repo/issues/7", "7", id="issue-url"),
        pytest.param("https://github.com/owner/repo/pull/9", "9", id="pr-url"),
        pytest.param("https://github.com/owner/repo/discussions/3", "3", id="discussion-url"),
        pytest.param("http://github.com/owner/repo/issues/15", "15", id="http-scheme"),
    ],
)
def test_parse_number_accepts_numeric_and_url(raw: str, expected: str) -> None:
    """parse_number returns the numeric ID for bare numbers and recognised URLs."""
    assert dtt.parse_number(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["not-a-number", "https://github.com/owner/repo/wiki/Home", "https://gitlab.com/owner/repo/issues/3", ""],
)
def test_parse_number_rejects_unrecognised(raw: str) -> None:
    """parse_number returns None for non-numeric, non-thread inputs."""
    assert dtt.parse_number(raw) is None


# ---------------------------------------------------------------------------
# parse_iso_to_epoch — ISO 8601 conversion
# ---------------------------------------------------------------------------


def test_parse_iso_to_epoch_round_trip() -> None:
    """A valid ISO timestamp converts to the expected epoch seconds."""
    assert dtt.parse_iso_to_epoch("2024-01-01T00:00:00Z") == 1704067200


@pytest.mark.parametrize("iso", ["", "not-a-date", "2024-01-01"])
def test_parse_iso_to_epoch_returns_none_on_failure(iso: str) -> None:
    """Empty or malformed timestamps return None."""
    assert dtt.parse_iso_to_epoch(iso) is None


# ---------------------------------------------------------------------------
# compute_drift — drift logic
# ---------------------------------------------------------------------------


def test_compute_drift_no_mtime_returns_false() -> None:
    """Drift check skipped (mtime=None) → False — caller passes mtime only when ``--report-mtime`` is set."""
    assert dtt.compute_drift("2024-01-01T00:00:00Z", None) is False


def test_compute_drift_thread_newer_than_report() -> None:
    """Thread updated AFTER report mtime → drift=True (report stale)."""
    assert dtt.compute_drift("2024-01-01T00:00:00Z", 1704067100) is True


def test_compute_drift_thread_older_than_report() -> None:
    """Thread updated BEFORE report mtime → drift=False (report still fresh)."""
    assert dtt.compute_drift("2024-01-01T00:00:00Z", 1704067300) is False


def test_compute_drift_equal_timestamp_is_not_drifted() -> None:
    """Updated-at exactly equal to report mtime is still fresh."""
    assert dtt.compute_drift("2024-01-01T00:00:00Z", 1704067200) is False


@pytest.mark.parametrize("iso", ["", "bogus"])
def test_compute_drift_parse_failure_is_conservative(iso: str) -> None:
    """Parse failures bias toward refetch (DRIFT=true) — never miss real updates."""
    assert dtt.compute_drift(iso, 1704067200) is True


# ---------------------------------------------------------------------------
# main — CLI surface
# Temp files written to ${TMPDIR}/oss-detect-{type,updated-at,drift}-shared
# (CLAUDE_CODE_SESSION_ID/CSID stripped by conftest.py autouse fixture → "shared")
# ---------------------------------------------------------------------------


def test_missing_number_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify command-line option behavior.

    ``--number`` is required.
    """
    rc = dtt.main([])
    assert rc != 0


def test_unparseable_number_emits_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-numeric, non-URL input → TYPE=unknown written to temp file; exit 0."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    rc = dtt.main(["--number", "not-a-number"])
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "unknown"
    assert (tmp_path / "oss-detect-updated-at-shared").exists()
    assert (tmp_path / "oss-detect-drift-shared").read_text() == "false"


def test_missing_gh_emits_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gh binary absent → graceful unknown output to temp file, error on stderr."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: None)
    rc = dtt.main(["--number", "123"])
    captured = capsys.readouterr()
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "unknown"
    assert "executable not found" in captured.err


def test_issue_detection_without_report_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue API returns plain issue JSON → TYPE=issue, DRIFT=false (no mtime)."""
    issue_payload = json.dumps({"number": 123, "updated_at": "2024-01-01T00:00:00Z"})
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(
        dtt.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=issue_payload),
    )
    rc = dtt.main(["--number", "123"])
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "issue"
    assert (tmp_path / "oss-detect-updated-at-shared").read_text() == "2024-01-01T00:00:00Z"
    assert (tmp_path / "oss-detect-drift-shared").read_text() == "false"


def test_pr_detection_via_pull_request_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue API returns JSON with .pull_request set → TYPE=pr."""
    pr_payload = json.dumps(
        {
            "number": 456,
            "updated_at": "2024-02-02T00:00:00Z",
            "pull_request": {"url": "..."},
        }
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(
        dtt.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=pr_payload),
    )
    rc = dtt.main(["--number", "456"])
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "pr"
    assert (tmp_path / "oss-detect-updated-at-shared").read_text() == "2024-02-02T00:00:00Z"


def test_discussion_detection_via_graphql_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issues API empty → GraphQL discussion lookup succeeds → TYPE=discussion."""
    calls: list[list[str]] = []
    disc_payload = json.dumps(
        {"data": {"repository": {"discussion": {"title": "Q&A", "updatedAt": "2024-03-03T00:00:00Z"}}}}
    )

    def _fake_run(cmd: list[str], **_: object) -> _FakeCompleted:
        """Return issue-empty and discussion-success responses for the fallback."""
        calls.append(cmd)
        if "graphql" in cmd:
            return _FakeCompleted(returncode=0, stdout=disc_payload)
        return _FakeCompleted(returncode=0, stdout="")

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(dtt.subprocess, "run", _fake_run)
    rc = dtt.main(["--number", "789"])
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "discussion"
    assert (tmp_path / "oss-detect-updated-at-shared").read_text() == "2024-03-03T00:00:00Z"
    assert len(calls) == 2  # both endpoints probed


def test_unknown_when_both_endpoints_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither issues API nor discussion GraphQL returns a record → TYPE=unknown."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(
        dtt.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=1, stdout=""),
    )
    rc = dtt.main(["--number", "9999"])
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "unknown"
    assert (tmp_path / "oss-detect-updated-at-shared").exists()
    assert (tmp_path / "oss-detect-drift-shared").read_text() == "false"


@pytest.mark.parametrize(
    ("issue_stdout", "graphql_stdout", "expected_type"),
    [
        pytest.param("not-json", "", "unknown", id="invalid-issue-json"),
        pytest.param(json.dumps({"number": 1}), "", "issue", id="missing-updated-at"),
        pytest.param(
            json.dumps({"number": 1, "updated_at": "2024-01-01T00:00:00Z", "pull_request": {}}),
            "",
            "issue",
            id="empty-pull-request",
        ),
        pytest.param("", json.dumps({"data": {"repository": {}}}), "unknown", id="graphql-without-discussion"),
    ],
)
def test_malformed_success_payloads_emit_conservative_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    issue_stdout: str,
    graphql_stdout: str,
    expected_type: str,
) -> None:
    def _fake_run(cmd: list[str], **_: object) -> _FakeCompleted:
        """Return each parametrized payload through the corresponding endpoint."""
        if "graphql" in cmd:
            return _FakeCompleted(returncode=0, stdout=graphql_stdout)
        return _FakeCompleted(returncode=0, stdout=issue_stdout)

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(dtt.subprocess, "run", _fake_run)
    rc = dtt.main(["--number", "1", "--report-mtime", "1704067200"])

    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == expected_type
    assert (tmp_path / "oss-detect-drift-shared").read_text() in {"false", "true"}


def test_drift_true_when_thread_newer_than_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify command-line option behavior.

    ``--report-mtime`` older than updatedAt → DRIFT=true.
    """
    issue_payload = json.dumps({"number": 1, "updated_at": "2024-01-01T00:00:00Z"})
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(
        dtt.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=issue_payload),
    )
    rc = dtt.main(["--number", "1", "--report-mtime", "1700000000"])
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "issue"
    assert (tmp_path / "oss-detect-drift-shared").read_text() == "true"


def test_drift_false_when_report_newer_than_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify command-line option behavior.

    ``--report-mtime`` later than updatedAt → DRIFT=false (cached report still valid).
    """
    issue_payload = json.dumps({"number": 1, "updated_at": "2024-01-01T00:00:00Z"})
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(
        dtt.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=issue_payload),
    )
    rc = dtt.main(["--number", "1", "--report-mtime", "1800000000"])
    assert rc == 0
    assert (tmp_path / "oss-detect-drift-shared").read_text() == "false"


def test_url_input_normalised_before_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """URL passed as ``--number`` → number extracted, detection proceeds normally."""
    issue_payload = json.dumps({"number": 7, "updated_at": "2024-01-01T00:00:00Z"})
    captured_cmds: list[list[str]] = []

    def _fake_run(cmd: list[str], **_: object) -> _FakeCompleted:
        """Return the fixed issue payload while recording the requested command."""
        captured_cmds.append(cmd)
        return _FakeCompleted(returncode=0, stdout=issue_payload)

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(dtt.subprocess, "run", _fake_run)
    rc = dtt.main(["--number", "https://github.com/owner/repo/issues/7"])
    assert rc == 0
    assert (tmp_path / "oss-detect-type-shared").read_text() == "issue"
    # The extracted /7 path must appear in the gh API call — not the full URL.
    assert any("/issues/7" in part for part in captured_cmds[0])


def test_temp_files_contain_no_shell_metacharacters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All three temp files must be safe for bash cat-assignment — no metacharacters."""
    issue_payload = json.dumps({"number": 1, "updated_at": "2024-01-01T00:00:00Z"})
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    monkeypatch.setattr(
        dtt.subprocess,
        "run",
        lambda *a, **k: _FakeCompleted(returncode=0, stdout=issue_payload),
    )
    dtt.main(["--number", "1"])
    for fname in ("oss-detect-type-shared", "oss-detect-updated-at-shared", "oss-detect-drift-shared"):
        val = (tmp_path / fname).read_text()
        assert ";" not in val
        assert "`" not in val
        assert "$(" not in val
