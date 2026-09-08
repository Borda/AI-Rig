"""Tests for ``bin/parse_audit_json.py`` — pip-audit JSON summariser."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from parse_audit_json import main, summarize


# ---------------------------------------------------------------------------
# summarize() — pure function
# ---------------------------------------------------------------------------


class TestSummarize:
    """Summarize: output format and edge cases."""

    def test_empty_dependencies(self) -> None:
        """Zero deps and zero vulns produces '0 deps, 0 vulns'."""
        assert summarize({"dependencies": []}) == "0 deps, 0 vulns"

    def test_single_dep_no_vulns(self) -> None:
        """One clean dep produces '1 deps, 0 vulns'."""
        assert summarize({"dependencies": [{"vulns": []}]}) == "1 deps, 0 vulns"

    def test_multiple_vulns_across_deps(self) -> None:
        """Vuln counts are summed across all deps."""
        payload = {"dependencies": [{"vulns": []}, {"vulns": [{}, {}]}]}
        assert summarize(payload) == "2 deps, 2 vulns"

    def test_single_dep_single_vuln(self) -> None:
        """One dep with one vuln."""
        assert summarize({"dependencies": [{"vulns": [{}]}]}) == "1 deps, 1 vulns"

    def test_missing_dependencies_key(self) -> None:
        """Missing top-level key treated as empty list (graceful fallback)."""
        assert summarize({}) == "0 deps, 0 vulns"

    def test_missing_vulns_key_in_dep(self) -> None:
        """Dep entry without 'vulns' key is treated as zero vulns."""
        assert summarize({"dependencies": [{}]}) == "1 deps, 0 vulns"

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            pytest.param({"dependencies": [{"vulns": [{}] * n}]}, f"1 deps, {n} vulns", id=f"{n}-vulns")
            for n in [0, 1, 5, 10]
        ],
    )
    def test_vuln_counts(self, payload: dict, expected: str) -> None:
        """Vuln count matches length of 'vulns' list."""
        assert summarize(payload) == expected


# ---------------------------------------------------------------------------
# main() — CLI entry point with stdin
# ---------------------------------------------------------------------------


class TestMain:
    """Verify command-line output and exit codes for standard input."""

    def test_valid_json_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Valid pip-audit JSON → exit code 0 and summary on stdout."""
        payload = {"dependencies": [{"vulns": []}, {"vulns": [{}]}]}
        fake_stdin = io.StringIO(json.dumps(payload))
        with patch("sys.stdin", fake_stdin):
            rc = main()
        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip() == "2 deps, 1 vulns"

    def test_empty_deps_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty dependency list is valid JSON → exit code 0."""
        fake_stdin = io.StringIO(json.dumps({"dependencies": []}))
        with patch("sys.stdin", fake_stdin):
            rc = main()
        assert rc == 0
        assert capsys.readouterr().out.strip() == "0 deps, 0 vulns"

    def test_invalid_json_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Malformed JSON → exit code 1 and error on stderr."""
        fake_stdin = io.StringIO("not json {{")
        with patch("sys.stdin", fake_stdin):
            rc = main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "invalid JSON" in err

    def test_os_error_exits_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """OSError on stdin read → exit code 1 and error on stderr."""

        class _BrokenStdin:
            """Raise an operating-system error when stdin is read."""

            def read(self, *_):
                """Raise the injected stdin failure when the parser reads input."""
                raise OSError("broken pipe")

        with patch("sys.stdin", _BrokenStdin()):
            rc = main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "stdin read error" in err

    def test_argv_override_unused(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Accept but ignore arguments retained for signature compatibility."""
        fake_stdin = io.StringIO(json.dumps({"dependencies": []}))
        with patch("sys.stdin", fake_stdin):
            rc = main(["--ignored"])
        assert rc == 0

    def test_golden_invocation_stdin_pipe(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Documented call site (``pip-audit --format=json | parse_audit_json.py``) — stdin-only."""
        payload = {"dependencies": [{"vulns": []}, {"vulns": [{}]}]}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            rc = main()
        assert rc == 0
        assert capsys.readouterr().out.strip() == "2 deps, 1 vulns"


# ---------------------------------------------------------------------------
# Doctest hookup
# ---------------------------------------------------------------------------


def test_doctests_pass() -> None:
    """Doctest examples in parse_audit_json.py must not regress."""
    import doctest

    import parse_audit_json as _mod

    results = doctest.testmod(_mod, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"
