"""Tests for ``bin/parse-resolve-args.py`` — oss:resolve argument parser.

Covers:
* ``parse_resolve_args`` — all four documented modes:
  - PR number (bare digits, with leading ``#``, with trailing ``report``)
  - GitHub PR URL (with and without trailing ``report``)
  - Bare ``report``
  - Comment-dispatch (fallthrough; leading ``#`` stripped exactly once)
* Doctest hookup for embedded examples
* ``_emit`` shell-quoting safety (hostile input round-trip)
* ``main()`` mode routing in-process, plus the script end to end via subprocess
* The shipped ``SKILL.md`` intake that consumes the parser output
"""

from __future__ import annotations

import doctest
import io
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import parse_resolve_args as parser_module  # loaded by conftest.py
import pytest
from _oss_shell_plugin import render_shell
from _resolve_source import read_resolve_source
from parse_resolve_args import _emit, main, parse_resolve_args

_PLUGIN = Path(__file__).resolve().parents[2]
_BIN = _PLUGIN / "bin" / "parse-resolve-args.py"
_SKILL = _PLUGIN / "skills" / "resolve" / "SKILL.md"
_BASH = shutil.which("bash")
_HOSTILE = "'; touch /tmp/pwned; echo 'x"

_skip_without_bash = pytest.mark.skipif(not _BASH, reason="resolve Step 1 runs under Bash")


def test_repeated_pull_paths_do_not_stall_argument_parsing() -> None:
    """A long malformed PR argument returns within the CLI's bounded parsing window."""
    blob = "https://github.com/owner/repo" + "/pull/7" * 8000 + " not a report"
    # Keep the full stress payload without exceeding Windows' process-command-line limit.
    runner = "import runpy, sys; sys.argv = [sys.argv[1], sys.stdin.read()]; runpy.run_path(sys.argv[0], run_name='__main__')"
    result = subprocess.run(
        [sys.executable, "-c", runner, str(_BIN)],
        input=blob,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=2,
    )
    assert result.returncode == 0
    assert _assignments(result.stdout) == {"PR_NUMBER": "", "PR_URL": "", "MODE": "comment-dispatch", "ARGUMENTS": blob}


def _assignments(stdout: str) -> dict[str, str]:
    """Parse the parser's ``VAR=value`` lines, undoing shell quoting."""
    return dict(token.split("=", 1) for token in shlex.split(stdout))


def _unquoted_values(stdout: str) -> list[str]:
    """Return the ``VAR=value`` lines whose value is neither empty-quoted, single-quoted nor shell-safe bare text."""
    bad = []
    for line in stdout.strip().splitlines():
        value = line.partition("=")[2]
        single_quoted = value.startswith("'") and value.endswith("'")
        if not (value == "''" or single_quoted or all(c.isalnum() or c in "_-+/.:" for c in value)):
            bad.append(line)
    return bad


def test_main_emits_lf_under_windows_newline_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bash reads assignments without carriage returns even when stdout defaults to CRLF.

    On Windows a text-mode stdout translates ``\\n`` to ``\\r\\n``; a stray ``\\r`` would end up inside every value the
    skill's intake reads, so the parser must write bare LF whatever the stream's newline setting.
    """
    output = io.BytesIO()
    stream = io.TextIOWrapper(output, encoding="utf-8", newline="\r\n", write_through=True)
    monkeypatch.setattr(sys, "stdout", stream)
    assert main(["1542"]) == 0
    assert output.getvalue() == b"PR_NUMBER=1542\nPR_URL=''\nMODE=pr\nARGUMENTS=1542\n"


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


class TestEmit:
    """``_emit``: render parsed values as shell assignments that are safe to read back as data."""

    def test_produces_one_assignment_per_key(self) -> None:
        """Every parsed key becomes one ``VAR=value`` line.

        The skill's intake reads these lines one by one, so a missing or merged line would lose a variable silently.
        """
        output = _emit({"PR_NUMBER": "42", "PR_URL": "", "MODE": "pr", "ARGUMENTS": "42"})
        lines = output.strip().splitlines()
        assert len(lines) == 4
        assert all("=" in line for line in lines)

    def test_hostile_value_is_single_quoted(self) -> None:
        """A hostile value is shell-quoted so that eval cannot execute injected commands.

        ``shlex.quote`` wraps the value in single quotes with internal quotes escaped; the raw semicolons must never
        appear unquoted in the assignment.
        """
        output = _emit({"PR_NUMBER": "", "PR_URL": "", "MODE": "comment-dispatch", "ARGUMENTS": _HOSTILE})
        value = dict(line.split("=", 1) for line in output.strip().splitlines())["ARGUMENTS"]
        assert value.startswith("'"), f"value should be single-quoted, got: {value!r}"

    @pytest.mark.parametrize("value", ["plain text", _HOSTILE, "", "line1\nline2", "$(touch /tmp/pwned)"])
    def test_shell_round_trip_preserves_hostile_values(self, value: str) -> None:
        """A shell reader recovers every ARGUMENTS value exactly, whatever metacharacters it carries.

        Quotes, empty text, embedded newlines and command substitutions all come back verbatim as data instead of being
        evaluated or truncated.
        """
        parsed = {"PR_NUMBER": "", "PR_URL": "", "MODE": "comment-dispatch", "ARGUMENTS": value}
        assert _assignments(_emit(parsed))["ARGUMENTS"] == value


class TestMainRouting:
    """``main``: argv → shell assignments, run in-process."""

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
    def test_emits_mode_and_pr_number(
        self, capsys: pytest.CaptureFixture[str], argv: list[str], expected_mode: str, expected_pr_number: str
    ) -> None:
        """All six documented mode branches emit their MODE and PR_NUMBER assignments.

        Blob-forward safety: a comment starting with ``-`` reaches the regex parser as comment-dispatch — argparse must
        NOT intercept dash-leading blob content as an unknown flag, since the ``$ARGUMENTS`` blob is forwarded verbatim
        to ``parse_resolve_args``.
        """
        assert main(argv) == 0
        assignments = _assignments(capsys.readouterr().out)
        assert (assignments["MODE"], assignments["PR_NUMBER"]) == (expected_mode, expected_pr_number)


class TestScript:
    """``parse-resolve-args.py`` run as the shipped executable, the way the skill invokes it."""

    @staticmethod
    def _run(*argv: str) -> subprocess.CompletedProcess[str]:
        """Run the script with ``argv`` and require exit 0."""
        return subprocess.run([sys.executable, str(_BIN), *argv], capture_output=True, text=True, check=True)

    def test_hostile_input_is_eval_safe(self) -> None:
        """Hostile ARGUMENTS value is shell-quoted — no unquoted metacharacters emitted.

        This is the exact text the skill's intake reads, so every emitted value must be quoted or provably shell-safe.
        """
        assert _unquoted_values(self._run(_HOSTILE).stdout) == []

    def test_help_flag_prints_usage(self) -> None:
        """``--help`` prints argparse usage and exits 0.

        A real subprocess proves the file works as a script entry point, not only as an imported module.
        """
        assert "usage:" in self._run("--help").stdout


def test_module_doctests_pass() -> None:
    """Doctest examples embedded in parse-resolve-args.py must not regress.

    The examples are the parser's own documentation of each mode, so a drifted example is a stale contract.
    """
    results = doctest.testmod(parser_module, verbose=False)
    assert results.failed == 0, f"{results.failed} doctest(s) failed"


class TestSkillIntake:
    """The shipped ``resolve/SKILL.md`` Step 1 that consumes the parser output."""

    def test_reads_only_known_keys_as_data(self) -> None:
        """The shipped intake validates parser output and never runs it as shell source.

        A value carrying outside text must stay data: the intake reads NUL-delimited key/value pairs and assigns only
        the four known keys, never sourcing or evaluating the file.
        """
        skill = read_resolve_source(_SKILL)
        intake = skill.split("tmpenv=$(mktemp)", 1)[1].split("# sets:", 1)[0]
        assert '--null "$ARGUMENTS"' in intake
        assert "exit 1" in intake
        assert '. "$tmpenv"' not in intake
        assert "eval " not in intake
        assert "while IFS= read -r -d '' _K && IFS= read -r -d '' _V; do" in intake
        assert 'case "$_K" in' in intake
        assert re.findall(r"([A-Z_]+)\) ([A-Z_]+)=\$_V;;", intake) == [
            ("PR_NUMBER", "PR_NUMBER"),
            ("PR_URL", "PR_URL"),
            ("MODE", "MODE"),
            ("ARGUMENTS", "ARGUMENTS"),
        ]
        assert 'done < "$tmpenv"' in intake

    @_skip_without_bash
    @pytest.mark.parametrize(
        ("pr_number", "blocked"),
        [pytest.param("", True, id="issue-url-blocks"), pytest.param("7", False, id="pull-url-continues")],
    )
    def test_blocks_pr_mode_without_number(self, pr_number: str, blocked: bool) -> None:
        """Resolve stops with a clear block when a GitHub URL carries no pull-request number.

        The parser deliberately keeps an issue or repo URL as MODE=pr with an empty PR_NUMBER so it never reaches
        comment dispatch; every later PR step keys on PR_NUMBER, so the skill must stop right after parsing. The guard
        line is run under real bash.
        """
        guard = next(line for line in read_resolve_source(_SKILL).splitlines() if "not a pull-request URL" in line)
        result = subprocess.run(
            [
                _BASH,
                "-c",
                render_shell(
                    f"MODE=pr; PR_NUMBER='{pr_number}'; PR_URL=https://github.com/o/r/issues/7\n{guard}", _PLUGIN, "oss"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 1) is blocked
        assert ("! BLOCKED" in result.stdout) is blocked
