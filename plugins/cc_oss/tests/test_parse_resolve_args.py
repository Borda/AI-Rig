"""Tests for ``bin/parse-resolve-args.py`` — oss:resolve argument parser.

Covers:
* ``parse_resolve_args`` — all four documented modes:
  - PR number (bare digits, with leading ``#``, with trailing ``report``)
  - GitHub PR URL (with and without trailing ``report``)
  - Bare ``report``
  - Comment-dispatch (fallthrough; leading ``#`` stripped exactly once)
* Doctest hookup for embedded examples
* ``_emit`` shell-quoting safety (hostile input round-trip)
* ``main()`` end-to-end via subprocess
"""

from __future__ import annotations

import subprocess
import sys
import shlex
from pathlib import Path

import pytest

from parse_resolve_args import _emit, parse_resolve_args  # loaded by conftest.py

_BIN = Path(__file__).resolve().parents[1] / "bin" / "parse-resolve-args.py"


# ---------------------------------------------------------------------------
# parse_resolve_args — mode routing
# ---------------------------------------------------------------------------


class TestPrNumberMode:
    """parse_resolve_args: PR-number inputs → mode 'pr' or 'pr+report'."""

    def test_bare_digits(self) -> None:
        """Plain integer string → PR_NUMBER set, MODE='pr'."""
        result = parse_resolve_args("42")
        assert result["PR_NUMBER"] == "42"
        assert result["PR_URL"] == ""
        assert result["MODE"] == "pr"
        assert result["ARGUMENTS"] == "42"

    def test_hash_prefixed(self) -> None:
        """Leading '#' is stripped from PR_NUMBER; ARGUMENTS preserved verbatim."""
        result = parse_resolve_args("#42")
        assert result["PR_NUMBER"] == "42"
        assert result["MODE"] == "pr"
        assert result["ARGUMENTS"] == "#42"

    def test_with_report_suffix(self) -> None:
        """'42 report' → MODE='pr+report'."""
        result = parse_resolve_args("42 report")
        assert result["PR_NUMBER"] == "42"
        assert result["MODE"] == "pr+report"

    def test_hash_with_report_suffix(self) -> None:
        """'#42 report' → MODE='pr+report', PR_NUMBER='42'."""
        result = parse_resolve_args("#42 report")
        assert result["PR_NUMBER"] == "42"
        assert result["MODE"] == "pr+report"

    def test_leading_whitespace_ignored(self) -> None:
        """Leading whitespace before digits is tolerated."""
        result = parse_resolve_args("  7")
        assert result["PR_NUMBER"] == "7"
        assert result["MODE"] == "pr"


class TestPrUrlMode:
    """parse_resolve_args: GitHub PR URL inputs → mode 'pr' or 'pr+report'."""

    def test_bare_url(self) -> None:
        """Full GitHub PR URL → PR_URL set, MODE='pr'."""
        url = "https://github.com/owner/repo/pull/7"
        result = parse_resolve_args(url)
        assert result["PR_URL"] == url
        assert result["PR_NUMBER"] == ""
        assert result["MODE"] == "pr"

    def test_url_with_report_suffix(self) -> None:
        """GitHub URL + ' report' → MODE='pr+report'."""
        url = "https://github.com/owner/repo/pull/7"
        result = parse_resolve_args(f"{url} report")
        assert result["PR_URL"] == url
        assert result["MODE"] == "pr+report"


class TestReportMode:
    """parse_resolve_args: bare 'report' keyword → mode 'report'."""

    def test_bare_report(self) -> None:
        """'report' alone → MODE='report', PR_NUMBER and PR_URL empty."""
        result = parse_resolve_args("report")
        assert result["MODE"] == "report"
        assert result["PR_NUMBER"] == ""
        assert result["PR_URL"] == ""

    def test_report_with_surrounding_whitespace(self) -> None:
        """'  report  ' (whitespace only) → still MODE='report'."""
        result = parse_resolve_args("  report  ")
        assert result["MODE"] == "report"


class TestCommentDispatchMode:
    """parse_resolve_args: fallthrough inputs → mode 'comment-dispatch'."""

    def test_plain_prose(self) -> None:
        """Prose without PR number/URL → comment-dispatch, ARGUMENTS preserved."""
        result = parse_resolve_args("please review the logic")
        assert result["MODE"] == "comment-dispatch"
        assert result["ARGUMENTS"] == "please review the logic"
        assert result["PR_NUMBER"] == ""
        assert result["PR_URL"] == ""

    def test_hash_prefix_stripped_once(self) -> None:
        """Exactly one leading '#' is stripped from ARGUMENTS in comment-dispatch."""
        result = parse_resolve_args("#42 looks wrong")
        assert result["MODE"] == "comment-dispatch"
        assert result["ARGUMENTS"] == "42 looks wrong"

    def test_empty_string_routes_to_comment_dispatch(self) -> None:
        """Empty input falls through to comment-dispatch with empty ARGUMENTS."""
        result = parse_resolve_args("")
        assert result["MODE"] == "comment-dispatch"
        assert result["ARGUMENTS"] == ""

    def test_hash_only_stripped_once_not_recursively(self) -> None:
        """'## heading' has exactly one '#' stripped → ARGUMENTS='# heading'."""
        result = parse_resolve_args("## heading")
        assert result["MODE"] == "comment-dispatch"
        assert result["ARGUMENTS"] == "# heading"


# ---------------------------------------------------------------------------
# _emit — shell-quoting safety
# ---------------------------------------------------------------------------


def test_emit_produces_shell_quoted_assignments() -> None:
    """_emit wraps values with shlex.quote; assignments are VAR=value lines."""
    parsed = {"PR_NUMBER": "42", "PR_URL": "", "MODE": "pr", "ARGUMENTS": "42"}
    output = _emit(parsed)
    lines = output.strip().splitlines()
    assert len(lines) == 4
    for line in lines:
        assert "=" in line


def test_emit_hostile_value_is_quoted() -> None:
    """Hostile value is shell-quoted so that eval cannot execute injected commands."""
    hostile = "'; touch /tmp/pwned; echo 'x"
    parsed = {"PR_NUMBER": "", "PR_URL": "", "MODE": "comment-dispatch", "ARGUMENTS": hostile}
    output = _emit(parsed)
    # shlex.quote wraps in single quotes with internal quotes escaped; the raw
    # semicolon must not appear unquoted in the assignment value.
    assignments = dict(line.split("=", 1) for line in output.strip().splitlines())
    value = assignments["ARGUMENTS"]
    assert value.startswith("'"), f"value should be single-quoted, got: {value!r}"


@pytest.mark.parametrize(
    "value",
    ["plain text", "'; touch /tmp/pwned; echo 'x", "", "line1\nline2", "$(touch /tmp/pwned)"],
)
def test_emit_shell_round_trip_for_hostile_values(value: str) -> None:
    parsed = {"PR_NUMBER": "", "PR_URL": "", "MODE": "comment-dispatch", "ARGUMENTS": value}
    assignments = dict(token.split("=", 1) for token in shlex.split(_emit(parsed)))
    assert assignments["ARGUMENTS"] == value


# ---------------------------------------------------------------------------
# Doctest hookup
# ---------------------------------------------------------------------------


def test_module_doctests_pass() -> None:
    """Doctest examples embedded in parse-resolve-args.py must not regress."""
    import doctest
    import parse_resolve_args as _mod

    results = doctest.testmod(_mod, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"


# ---------------------------------------------------------------------------
# main() — subprocess end-to-end
# ---------------------------------------------------------------------------


def test_main_via_subprocess_pr_number() -> None:
    """Subprocess invocation for PR number produces parseable shell assignments."""
    result = subprocess.run(
        [sys.executable, str(_BIN), "42"],
        capture_output=True,
        text=True,
        check=True,
    )
    assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert assignments["PR_NUMBER"] == "42"
    assert assignments["MODE"] == "pr"


def test_main_via_subprocess_hostile_input_is_eval_safe() -> None:
    """Hostile ARGUMENTS value is shell-quoted — no unquoted metacharacters emitted."""
    hostile = "'; touch /tmp/pwned; echo 'x"
    result = subprocess.run(
        [sys.executable, str(_BIN), hostile],
        capture_output=True,
        text=True,
        check=True,
    )
    out = result.stdout
    for line in out.strip().splitlines():
        key, _, value = line.partition("=")
        is_empty_quoted = value == "''"
        is_single_quoted = value.startswith("'") and value.endswith("'")
        is_bare_safe = all(c.isalnum() or c in "_-+/.:" for c in value)
        assert is_empty_quoted or is_single_quoted or is_bare_safe, f"unsafe shell value for {key}: {value!r}"


@pytest.mark.parametrize(
    ("argv", "expected_mode"),
    [
        pytest.param(["99"], "pr", id="99"),
        pytest.param(["#99", "report"], "pr+report", id="99-report"),
        pytest.param(["https://github.com/o/r/pull/1"], "pr", id="https-github.com-o-r-pull-1"),
        pytest.param(["https://github.com/o/r/pull/1", "report"], "pr+report", id="https-github.com-o-r-pull-1-report"),
        pytest.param(["report"], "report", id="report"),
        pytest.param(["some prose comment"], "comment-dispatch", id="some-prose-comment"),
    ],
)
def test_main_mode_routing_via_subprocess(argv: list[str], expected_mode: str) -> None:
    """All six documented mode branches are reachable via subprocess invocation."""
    result = subprocess.run(
        [sys.executable, str(_BIN), *argv],
        capture_output=True,
        text=True,
        check=True,
    )
    assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    # MODE value is shell-quoted ('pr', 'pr+report', etc.); strip surrounding quotes.
    raw_mode = assignments["MODE"].strip("'")
    assert raw_mode == expected_mode, f"argv={argv!r}: expected MODE={expected_mode!r}, got {raw_mode!r}"


def test_help_flag_exits_zero_via_subprocess() -> None:
    """Print usage and exit 0 (argparse)."""
    result = subprocess.run(
        [sys.executable, str(_BIN), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout


def test_dash_leading_prose_forwarded_not_misparsed_as_flag() -> None:
    """Blob-forward safety: a comment starting with ``-`` reaches the regex parser as comment-dispatch.

    argparse must NOT intercept dash-leading blob content as an unknown flag — the ``$ARGUMENTS`` blob is forwarded
    verbatim to ``parse_resolve_args``.
    """
    result = subprocess.run(
        [sys.executable, str(_BIN), "-x is broken"],
        capture_output=True,
        text=True,
        check=True,
    )
    assignments = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    assert assignments["MODE"].strip("'") == "comment-dispatch"
