"""Tests for ``bin/detect_thread_type.py``.

Arg-parsing, URL extraction, and drift-computation tests run without any subprocess calls. Subprocess-dependent paths
replace ``subprocess.run`` and ``which`` so no real ``gh`` invocation occurs. The CLI writes three temp files under
``${TMPDIR}`` (``oss-detect-{type,updated-at,drift}-<CSID>``); the ``tests/conftest.py`` autouse fixture strips the
session variables so the suffix is always ``shared``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import detect_thread_type as dtt
import pytest

_UPDATED = "2024-01-01T00:00:00Z"
_REPORT_MTIME = 1704067200  # epoch of _UPDATED
_ISSUE = json.dumps({"number": 123, "updated_at": _UPDATED})


def _reply(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build the ``CompletedProcess`` a fake ``gh`` call returns."""
    return subprocess.CompletedProcess([], returncode, stdout=stdout)


@pytest.fixture
def gh_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``TMPDIR`` at ``tmp_path`` and pretend ``gh`` is installed; return the temp dir."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(dtt, "which", lambda _: "/fake/gh")
    return tmp_path


@pytest.fixture
def script_gh(gh_env: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Return a factory scripting ``gh``: ``issue`` answers the issues API, ``graphql`` the discussion lookup.

    The factory returns the list of recorded argv lists; unscripted endpoints answer with empty successful output.
    """

    def install(
        *,
        issue: subprocess.CompletedProcess[str] | None = None,
        graphql: subprocess.CompletedProcess[str] | None = None,
    ) -> list[list[str]]:
        """Route each ``gh`` call to its endpoint's reply, recording its argv."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            """Return the graphql reply for a graphql call and the issue reply otherwise."""
            calls.append(cmd)
            return (graphql if "graphql" in cmd else issue) or _reply()

        monkeypatch.setattr(dtt.subprocess, "run", fake_run)
        return calls

    return install


def _outputs(tmp_dir: Path) -> dict[str, str]:
    """Read back the three temp files the CLI writes, keyed by field name."""
    return {key: (tmp_dir / f"oss-detect-{key}-shared").read_text() for key in ("type", "updated-at", "drift")}


class TestParseNumber:
    """``parse_number``: URL formats and ``#N`` variants."""

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
    def test_accepts_numeric_and_url(self, raw: str, expected: str) -> None:
        """Bare numbers, ``#N`` and issue, PR or discussion URLs all reduce to the numeric thread ID."""
        assert dtt.parse_number(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["not-a-number", "https://github.com/owner/repo/wiki/Home", "https://gitlab.com/owner/repo/issues/3", ""]
    )
    def test_rejects_unrecognised(self, raw: str) -> None:
        """Non-numeric text, non-thread GitHub URLs, other hosts and the empty string yield ``None``."""
        assert dtt.parse_number(raw) is None


class TestParseIsoToEpoch:
    """``parse_iso_to_epoch``: ISO 8601 conversion."""

    def test_round_trip(self) -> None:
        """A valid ``Z``-suffixed ISO timestamp converts to the expected epoch seconds."""
        assert dtt.parse_iso_to_epoch(_UPDATED) == _REPORT_MTIME

    @pytest.mark.parametrize("iso", ["", "not-a-date", "2024-01-01"])
    def test_returns_none_on_failure(self, iso: str) -> None:
        """Empty, non-date and date-only strings return ``None`` instead of raising."""
        assert dtt.parse_iso_to_epoch(iso) is None


class TestComputeDrift:
    """``compute_drift``: whether the cached report is stale relative to the thread."""

    @pytest.mark.parametrize(
        ("report_mtime", "expected"),
        [
            pytest.param(None, False, id="no-mtime-skips-drift-check"),
            pytest.param(1704067100, True, id="thread-newer-than-report"),
            pytest.param(1704067300, False, id="thread-older-than-report"),
            pytest.param(_REPORT_MTIME, False, id="equal-timestamp-is-not-drifted"),
        ],
    )
    def test_compares_thread_update_with_report_mtime(self, report_mtime: int | None, expected: bool) -> None:
        """Drift is true only when the thread was updated strictly AFTER the report mtime (report stale).

        The thread updated-at is fixed at epoch 1704067200. An mtime of ``None`` means the drift check is skipped — the
        caller passes mtime only when ``--report-mtime`` is set. An older or exactly equal report mtime is still fresh.
        """
        assert dtt.compute_drift(_UPDATED, report_mtime) is expected

    @pytest.mark.parametrize("iso", ["", "bogus"])
    def test_parse_failure_is_conservative(self, iso: str) -> None:
        """An unparsable thread timestamp reports drift, biasing toward refetch so real updates are never missed."""
        assert dtt.compute_drift(iso, _REPORT_MTIME) is True


class TestMain:
    """CLI surface: temp-file outputs for each detection outcome."""

    def test_missing_number_exits_nonzero(self) -> None:
        """Omitting the required ``--number`` flag exits non-zero.

        Detection cannot start without a thread reference, so the run must fail rather than write empty outputs.
        """
        assert dtt.main([]) != 0

    def test_missing_gh_emits_unknown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An absent ``gh`` binary yields ``TYPE=unknown`` in the temp file, exit 0, and an error on stderr.

        Later skill steps read the temp files, so they must exist even when detection cannot run.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setattr(dtt, "which", lambda _: None)
        rc = dtt.main(["--number", "123"])
        assert rc == 0
        assert (tmp_path / "oss-detect-type-shared").read_text() == "unknown"
        assert "executable not found" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("number", "issue", "expected"),
        [
            pytest.param(
                "not-a-number",
                _reply(),
                {"type": "unknown", "updated-at": "", "drift": "false"},
                id="unparseable-number",
            ),
            pytest.param(
                "123", _reply(_ISSUE), {"type": "issue", "updated-at": _UPDATED, "drift": "false"}, id="plain-issue"
            ),
            pytest.param(
                "456",
                _reply(
                    json.dumps({"number": 456, "updated_at": "2024-02-02T00:00:00Z", "pull_request": {"url": "..."}})
                ),
                {"type": "pr", "updated-at": "2024-02-02T00:00:00Z", "drift": "false"},
                id="pull-request-field",
            ),
            pytest.param(
                "9999",
                _reply(returncode=1),
                {"type": "unknown", "updated-at": "", "drift": "false"},
                id="both-endpoints-empty",
            ),
        ],
    )
    def test_detects_thread_type(
        self,
        gh_env: Path,
        script_gh: Callable[..., list[list[str]]],
        number: str,
        issue: subprocess.CompletedProcess[str],
        expected: dict[str, str],
    ) -> None:
        """The issues API reply (or its absence) decides TYPE, UPDATED_AT and DRIFT without ``--report-mtime``.

        A plain issue payload is ``issue``, a payload with ``pull_request`` is ``pr``, and a non-numeric number or two
        empty endpoints give ``unknown``. With no mtime supplied the drift flag is always ``false``.
        """
        script_gh(issue=issue, graphql=_reply(returncode=1))
        rc = dtt.main(["--number", number])
        assert rc == 0
        assert _outputs(gh_env) == expected

    def test_discussion_detection_via_graphql_fallback(
        self, gh_env: Path, script_gh: Callable[..., list[list[str]]]
    ) -> None:
        """An empty issues API reply falls back to the GraphQL discussion lookup and yields TYPE=discussion.

        Both endpoints must be probed, in that order.
        """
        discussion = {"data": {"repository": {"discussion": {"title": "Q&A", "updatedAt": "2024-03-03T00:00:00Z"}}}}
        calls = script_gh(graphql=_reply(json.dumps(discussion)))
        rc = dtt.main(["--number", "789"])
        assert rc == 0
        assert _outputs(gh_env) == {"type": "discussion", "updated-at": "2024-03-03T00:00:00Z", "drift": "false"}
        assert len(calls) == 2

    @pytest.mark.parametrize(
        ("issue_stdout", "graphql_stdout", "expected"),
        [
            pytest.param(
                "not-json", "", {"type": "unknown", "updated-at": "", "drift": "false"}, id="invalid-issue-json"
            ),
            pytest.param(
                json.dumps({"number": 1}),
                "",
                {"type": "issue", "updated-at": "", "drift": "true"},
                id="missing-updated-at-drifts-conservatively",
            ),
            pytest.param(
                json.dumps({"number": 1, "updated_at": _UPDATED, "pull_request": {}}),
                "",
                {"type": "issue", "updated-at": _UPDATED, "drift": "false"},
                id="empty-pull-request-is-issue",
            ),
            pytest.param(
                "",
                json.dumps({"data": {"repository": {}}}),
                {"type": "unknown", "updated-at": "", "drift": "false"},
                id="graphql-without-discussion",
            ),
        ],
    )
    def test_malformed_success_payloads_emit_conservative_results(
        self,
        gh_env: Path,
        script_gh: Callable[..., list[list[str]]],
        issue_stdout: str,
        graphql_stdout: str,
        expected: dict[str, str],
    ) -> None:
        """Successful but malformed payloads degrade to a conservative TYPE and DRIFT instead of crashing.

        Invalid JSON and a discussion-less GraphQL reply are ``unknown``; a missing ``updated_at`` still types the
        thread but reports drift so the report is refetched; an empty ``pull_request`` object does not make it a PR. The
        report mtime equals the thread timestamp, so a parsed timestamp is never drifted.
        """
        script_gh(issue=_reply(issue_stdout), graphql=_reply(graphql_stdout))
        rc = dtt.main(["--number", "1", "--report-mtime", str(_REPORT_MTIME)])
        assert rc == 0
        assert _outputs(gh_env) == expected

    @pytest.mark.parametrize(
        ("report_mtime", "expected_drift"),
        [
            pytest.param("1700000000", "true", id="report-older-than-thread-drifted"),
            pytest.param("1800000000", "false", id="report-newer-than-thread-still-valid"),
        ],
    )
    def test_report_mtime_decides_drift(
        self, gh_env: Path, script_gh: Callable[..., list[list[str]]], report_mtime: str, expected_drift: str
    ) -> None:
        """``--report-mtime`` older than updatedAt gives DRIFT=true; later than updatedAt gives DRIFT=false.

        A report newer than the thread is a still-valid cached report.
        """
        script_gh(issue=_reply(_ISSUE))
        rc = dtt.main(["--number", "1", "--report-mtime", report_mtime])
        assert rc == 0
        outputs = _outputs(gh_env)
        assert outputs["type"] == "issue"
        assert outputs["drift"] == expected_drift

    def test_url_input_normalised_before_detection(
        self, gh_env: Path, script_gh: Callable[..., list[list[str]]]
    ) -> None:
        """A thread URL passed as ``--number`` is reduced to its number before the issues API is called.

        The extracted ``/issues/7`` path, not the full URL, must reach the first ``gh`` call.
        """
        calls = script_gh(issue=_reply(json.dumps({"number": 7, "updated_at": _UPDATED})))
        rc = dtt.main(["--number", "https://github.com/owner/repo/issues/7"])
        assert rc == 0
        assert _outputs(gh_env)["type"] == "issue"
        assert any("/issues/7" in part for part in calls[0])

    @pytest.mark.parametrize(
        "fname", ["oss-detect-type-shared", "oss-detect-updated-at-shared", "oss-detect-drift-shared"]
    )
    def test_temp_file_contains_no_shell_metacharacters(
        self, gh_env: Path, script_gh: Callable[..., list[list[str]]], fname: str
    ) -> None:
        """Each temp file is safe for the skill's bash read-back — no ``;``, backtick or ``$(``.

        The skill assigns these files' contents to shell variables, so a metacharacter would become command injection.
        """
        script_gh(issue=_reply(_ISSUE))
        dtt.main(["--number", "1"])
        value = (gh_env / fname).read_text()
        assert not any(token in value for token in (";", "`", "$("))
