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

import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from parse_resolve_args import _emit, parse_resolve_args  # loaded by conftest.py

_BIN = Path(__file__).resolve().parents[2] / "bin" / "parse-resolve-args.py"


# ---------------------------------------------------------------------------
# parse_resolve_args — mode routing
# ---------------------------------------------------------------------------


class TestPrNumberMode:
    """parse_resolve_args: PR-number inputs → mode 'pr' or 'pr+report'."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("42", {"PR_NUMBER": "42", "PR_URL": "", "MODE": "pr", "ARGUMENTS": "42"}, id="bare-digits"),
            pytest.param(
                "#42",
                {"PR_NUMBER": "42", "MODE": "pr", "ARGUMENTS": "#42"},
                id="hash-prefix-stripped-arguments-verbatim",
            ),
            pytest.param("42 report", {"PR_NUMBER": "42", "MODE": "pr+report"}, id="report-suffix"),
            pytest.param("#42 report", {"PR_NUMBER": "42", "MODE": "pr+report"}, id="hash-with-report-suffix"),
            pytest.param("report 42", {"PR_NUMBER": "42", "MODE": "pr+report"}, id="report-leads-bare"),
            pytest.param("report #42", {"PR_NUMBER": "42", "MODE": "pr+report"}, id="report-leads-hash"),
            pytest.param("  report   42 ", {"PR_NUMBER": "42", "MODE": "pr+report"}, id="report-leads-whitespace"),
            pytest.param("  7", {"PR_NUMBER": "7", "MODE": "pr"}, id="leading-whitespace-ignored"),
        ],
    )
    def test_pr_number_input_routes_to_pr_mode(self, arguments: str, expected: dict[str, str]) -> None:
        """A PR number routes to MODE='pr', or 'pr+report' when 'report' is present — in either order.

        Plain integers and a leading '#' (stripped from PR_NUMBER, ARGUMENTS preserved verbatim) set PR_NUMBER with
        MODE='pr'; '42 report' and '#42 report' give MODE='pr+report'. 'report 42' routes exactly like '42 report':
        before this contract the leading form fell through to comment-dispatch with an empty PR_NUMBER, silently
        treating the request as free-text comment content. Leading whitespace before digits is tolerated.
        """
        result = parse_resolve_args(arguments)
        assert expected.items() <= result.items()


class TestPrUrlMode:
    """parse_resolve_args: GitHub PR URL inputs → mode 'pr' or 'pr+report'."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param(
                "https://github.com/owner/repo/pull/7",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr"},
                id="bare-url",
            ),
            pytest.param(
                "report https://github.com/owner/repo/pull/7",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr+report"},
                id="report-prefix-before-url",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7 report",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr+report"},
                id="url-with-report-suffix",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7/files",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr"},
                id="pasted-tail-files-tab",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7/commits",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr"},
                id="pasted-tail-commits-tab",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7/",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr"},
                id="pasted-tail-trailing-slash",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7#discussion_r1",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr"},
                id="pasted-tail-thread-anchor",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7?diff=split",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr"},
                id="pasted-tail-query-string",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7/files#r99",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr"},
                id="pasted-tail-tab-plus-anchor",
            ),
            pytest.param(
                "https://github.com/owner/repo/pull/7/files report",
                {"PR_URL": "https://github.com/owner/repo/pull/7", "PR_NUMBER": "7", "MODE": "pr+report"},
                id="pasted-tail-with-report-suffix",
            ),
            pytest.param(
                "https://github.com/owner/repo",
                {"PR_URL": "https://github.com/owner/repo", "PR_NUMBER": "", "MODE": "pr"},
                id="plain-repo-link-without-pull-segment",
            ),
            pytest.param(
                "https://github.com/owner/repo/issues/7",
                {"PR_URL": "https://github.com/owner/repo/issues/7", "PR_NUMBER": "", "MODE": "pr"},
                id="issue-link-without-pull-segment",
            ),
        ],
    )
    def test_github_url_input_routes_to_pr_mode(self, arguments: str, expected: dict[str, str]) -> None:
        """A GitHub URL routes to MODE='pr' (or 'pr+report' with 'report' in either position) with PR_URL set.

        PR_NUMBER must be extracted from '/pull/N': the reject-gate check and the report-source lookup are both scoped
        on PR_NUMBER, and an empty value routes them onto 'n/a' — invisible to both, regardless of PR_URL being set.
        Browser copies carry '/files', '/commits', '/', '#discussion_r…', '?diff=…' tails; routing them to comment
        dispatch would hand the URL to an implementer instead of resolving the PR, so the tail is dropped and PR_URL is
        canonical. The leading 'report <PR URL>' form previously fell through to comment-dispatch; the URL and number
        regexes now accept `report` in either position. A GitHub URL lacking '/pull/N' keeps MODE='pr' with PR_URL set
        and PR_NUMBER empty; downstream gates then report 'n/a' for the missing number, and the URL must never fall
        through to comment dispatch, which forwards its argument to a write-capable implementer.
        """
        result = parse_resolve_args(arguments)
        assert expected.items() <= result.items()


class TestReportMode:
    """parse_resolve_args: bare 'report' keyword → mode 'report'."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("report", {"MODE": "report", "PR_NUMBER": "", "PR_URL": ""}, id="bare-report"),
            pytest.param("  report  ", {"MODE": "report"}, id="report-with-surrounding-whitespace"),
        ],
    )
    def test_bare_report_routes_to_report_mode(self, arguments: str, expected: dict[str, str]) -> None:
        """'report' alone → MODE='report' with PR_NUMBER and PR_URL empty; surrounding whitespace is still 'report'."""
        result = parse_resolve_args(arguments)
        assert expected.items() <= result.items()


class TestCommentDispatchMode:
    """parse_resolve_args: fallthrough inputs → mode 'comment-dispatch'."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param(
                "please review the logic",
                {
                    "MODE": "comment-dispatch",
                    "ARGUMENTS": "please review the logic",
                    "PR_NUMBER": "",
                    "PR_URL": "",
                },
                id="plain-prose-arguments-preserved",
            ),
            pytest.param(
                "#42 looks wrong",
                {"MODE": "comment-dispatch", "ARGUMENTS": "42 looks wrong"},
                id="hash-prefix-stripped-once",
            ),
            pytest.param("", {"MODE": "comment-dispatch", "ARGUMENTS": ""}, id="empty-string"),
            pytest.param(
                "## heading",
                {"MODE": "comment-dispatch", "ARGUMENTS": "# heading"},
                id="hash-stripped-once-not-recursively",
            ),
        ],
    )
    def test_fallthrough_input_routes_to_comment_dispatch(self, arguments: str, expected: dict[str, str]) -> None:
        """Input without a PR number/URL or 'report' → comment-dispatch; exactly one leading '#' is stripped.

        Prose keeps ARGUMENTS verbatim with empty PR_NUMBER/PR_URL; '#42 looks wrong' loses one '#'; '## heading'
        becomes '# heading' (not stripped recursively); empty input falls through with empty ARGUMENTS.
        """
        result = parse_resolve_args(arguments)
        assert expected.items() <= result.items()


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
    ("argv", "expected_mode", "expected_pr_number"),
    [
        pytest.param(["99"], "pr", "99", id="99"),
        pytest.param(["#99", "report"], "pr+report", "99", id="99-report"),
        pytest.param(["https://github.com/o/r/pull/1"], "pr", "1", id="https-github.com-o-r-pull-1"),
        pytest.param(
            ["https://github.com/o/r/pull/1", "report"], "pr+report", "1", id="https-github.com-o-r-pull-1-report"
        ),
        pytest.param(["report"], "report", "", id="report"),
        pytest.param(["some prose comment"], "comment-dispatch", "", id="some-prose-comment"),
        pytest.param(["-x is broken"], "comment-dispatch", "", id="dash-leading-prose-not-misparsed-as-flag"),
    ],
)
def test_main_mode_routing_via_subprocess(argv: list[str], expected_mode: str, expected_pr_number: str) -> None:
    """All six documented mode branches are reachable via subprocess invocation, with PR_NUMBER emitted.

    Blob-forward safety: a comment starting with ``-`` reaches the regex parser as comment-dispatch — argparse must NOT
    intercept dash-leading blob content as an unknown flag, since the ``$ARGUMENTS`` blob is forwarded verbatim to
    ``parse_resolve_args``.
    """
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
    assert assignments["PR_NUMBER"].strip("'") == expected_pr_number


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
