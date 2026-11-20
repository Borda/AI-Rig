"""Tests for check_rtk_alignment bin script.

Covers prefix-array parsing, the invalid-prefix comparison, and the CLI skip paths (rtk absent, hook absent, unparsable
prefix arrays). The ``rtk`` binary and its ``--help`` call are the only external boundaries; both are mocked, so no test
spawns a process.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import check_rtk_alignment as cra
import pytest

_HOOK_SRC = """
const SAFE_PREFIXES = ["ls", 'grep'];
const GUARDED_PREFIXES = ["git"];
"""

_HELP = """
Usage: rtk <command>

Commands:
  ls        list things
  git       git things
  grep      grep things
  gain      meta
"""

_RTK_PATH = "/usr/bin/rtk"


@pytest.fixture
def hook_file(tmp_path: Path) -> Callable[[str], Path]:
    """Return a factory that writes ``rtk-rewrite.js`` with the given source and returns its path."""

    def write(source: str) -> Path:
        hook = tmp_path / "rtk-rewrite.js"
        hook.write_text(source, encoding="utf-8")
        return hook

    return write


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch) -> Callable[..., int]:
    """Return a runner that sets ``sys.argv`` to the given arguments and returns ``main()``'s exit code."""

    def run(*args: str) -> int:
        monkeypatch.setattr("sys.argv", ["check_rtk_alignment.py", *args])
        return cra.main()

    return run


class TestHookPrefixes:
    """Covers parsing the routed prefixes out of the hook source."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param(_HOOK_SRC, ["ls", "grep", "git"], id="both-arrays-both-quote-styles"),
            pytest.param('const SAFE_PREFIXES = ["ls"];\n', ["ls"], id="only-safe-array"),
            pytest.param('const GUARDED_PREFIXES = ["git"];\n', ["git"], id="only-guarded-array"),
            pytest.param("const OTHER = [];\n", [], id="missing-arrays"),
        ],
    )
    def test_reads_entries_or_yields_empty(self, hook_file: Callable[[str], Path], source: str, expected: list) -> None:
        """Entries of both arrays are returned in order; a hook lacking both arrays yields no prefixes.

        The hook is JavaScript source, so the arrays are read textually; a hook without them must give an empty list
        rather than raise.
        """
        assert cra.hook_prefixes(hook_file(source)) == expected

    def test_shipped_hook_prefixes_are_parsed(self) -> None:
        """The plugin's own hook yields its routed programs, so the default ``--hook`` path is never a silent skip."""
        assert {"git"} == set(cra.hook_prefixes(cra._DEFAULT_HOOK))

    def test_unreadable_file_yields_empty(self, tmp_path: Path) -> None:
        """A path that does not exist yields no prefixes.

        The CLI decides what a missing hook means; the parser itself must not raise on it.
        """
        assert cra.hook_prefixes(tmp_path / "absent.js") == []


class TestInvalidPrefixes:
    """Covers the one-direction comparison."""

    def test_aligned_lists_report_nothing(self) -> None:
        """Hook prefixes that RTK's help names produce no findings.

        RTK subcommands the hook does not route (here ``gain``) are measured exclusions, never reported.
        """
        assert cra.invalid_prefixes(["ls", "grep", "git"], _HELP) == []

    @pytest.mark.parametrize(
        ("prefixes", "help_text", "expected_invalid"),
        [
            pytest.param(["ls", "bogus"], _HELP, ["bogus"], id="unknown-prefix"),
            # Word matching does not degrade to substring: `gi` is not `git`.
            pytest.param(["gi"], _HELP, ["gi"], id="substring-alone"),
            # `grep -w` treats `-` as a boundary, so `bar` matches inside `foo-bar`.
            pytest.param(["bar"], "  foo-bar   does things\n", [], id="hyphen-is-a-word-boundary"),
            # A prefix mentioned anywhere in help is valid, as `grep -qw` treated it.
            pytest.param(
                ["lint", "build"],
                "Usage: rtk <cmd>\n\n  build   build stuff\n\nSee also: lint for linting\n",
                [],
                id="prefix-named-outside-the-command-list",
            ),
        ],
    )
    def test_prefix_validity_is_word_matching_against_help(
        self, prefixes: list[str], help_text: str, expected_invalid: list[str]
    ) -> None:
        """A hook prefix is invalid unless RTK's help names it as a whole word.

        Restricting validity to the indented command list would turn every prose mention into a spurious high-severity
        finding, while degrading to substring matching would accept ``gi`` for ``git``; ``-`` counts as a word boundary.
        """
        assert cra.invalid_prefixes(prefixes, help_text) == expected_invalid


class TestCli:
    """Covers the command-line skip and failure paths."""

    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=None)
    def test_rtk_absent_skips(self, mock_which: mock.MagicMock, run_cli, capsys: pytest.CaptureFixture[str]) -> None:
        """No rtk binary means skip with exit 0.

        Machines without RTK installed must not fail the audit; the skip is announced on stdout.
        """
        assert run_cli() == 0
        assert "rtk not installed" in capsys.readouterr().out

    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_hook_absent_skips(
        self, mock_which: mock.MagicMock, tmp_path: Path, run_cli, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing hook file means skip with exit 0.

        RTK is installed but the ``--hook`` path does not exist; there is nothing to align, so the run is a skip.
        """
        assert run_cli("--hook", str(tmp_path / "absent.js")) == 0
        assert "not found" in capsys.readouterr().out

    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_unparseable_prefix_arrays_skip(
        self, mock_which: mock.MagicMock, hook_file: Callable[[str], Path], run_cli, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hook without the prefix arrays skips rather than failing.

        The hook exists but carries no readable prefix array, so the comparison cannot run and must say so.
        """
        assert run_cli("--hook", str(hook_file("// nothing here\n"))) == 0
        assert "could not parse SAFE_PREFIXES or GUARDED_PREFIXES" in capsys.readouterr().out

    @mock.patch.object(
        cra.subprocess, "run", autospec=True, side_effect=subprocess.TimeoutExpired(cmd="rtk", timeout=1.0)
    )
    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_rtk_help_timeout_skips(
        self,
        mock_which: mock.MagicMock,
        mock_run: mock.MagicMock,
        hook_file: Callable[[str], Path],
        run_cli,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A hung `rtk --help` degrades to a skip instead of a traceback.

        The shell original could not crash here — it had only a `# timeout:` comment, with no enforcement — so an
        unhandled TimeoutExpired would be a regression.
        """
        hook = hook_file('const SAFE_PREFIXES = ["ls"];\n')
        assert run_cli("--hook", str(hook)) == 0
        assert "⚠ SKIPPED: Check 10 — `rtk --help` failed or timed out" in capsys.readouterr().out

    @mock.patch.object(
        cra.subprocess,
        "run",
        autospec=True,
        return_value=subprocess.CompletedProcess(args=["rtk", "--help"], returncode=0, stdout=_HELP, stderr=""),
    )
    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_invalid_prefix_reported(
        self,
        mock_which: mock.MagicMock,
        mock_run: mock.MagicMock,
        hook_file: Callable[[str], Path],
        run_cli,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An unknown hook prefix is named on stdout; the exit stays 0 (report-only).

        The hook lists ``bogus``, which RTK's help never mentions; the audit reports it without failing the run.
        """
        hook = hook_file('const SAFE_PREFIXES = ["bogus"];\n')
        assert run_cli("--hook", str(hook)) == 0
        assert "! INVALID hook prefix: 'bogus'" in capsys.readouterr().out

    @mock.patch.object(
        cra.subprocess,
        "run",
        autospec=True,
        return_value=subprocess.CompletedProcess(args=["rtk", "--help"], returncode=0, stdout=_HELP, stderr=""),
    )
    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_unrouted_rtk_subcommands_are_not_findings(
        self,
        mock_which: mock.MagicMock,
        mock_run: mock.MagicMock,
        hook_file: Callable[[str], Path],
        run_cli,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """RTK subcommands the hook never routes produce an OK line, not a missing-prefix finding.

        Scenario: the hook routes only ``ls`` while RTK also offers ``git`` and ``grep``. The hook routes verbs whose
        output passed a fidelity comparison, so the omission is deliberate.
        """
        hook = hook_file('const SAFE_PREFIXES = ["ls"];\n')
        assert run_cli("--hook", str(hook)) == 0
        out = capsys.readouterr().out
        assert "✓ OK: Check 10" in out
        assert "MISSING" not in out

    @mock.patch.object(
        cra.subprocess,
        "run",
        autospec=True,
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=_HELP, stderr=""),
    )
    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_help_runs_the_absolute_resolved_executable(
        self,
        mock_which: mock.Mock,
        mock_run: mock.Mock,
        hook_file: Callable[[str], Path],
        run_cli,
    ) -> None:
        """The checked executable is passed by absolute path so Windows cannot search cwd again."""
        hook = hook_file('const SAFE_PREFIXES = ["ls"];\n')

        assert run_cli("--hook", str(hook)) == 0
        assert mock_run.call_args.args[0] == [str(Path(_RTK_PATH).resolve()), "--help"]

    @mock.patch.object(
        cra.subprocess,
        "run",
        autospec=True,
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=_HELP, stderr=""),
    )
    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_default_hook_is_the_plugin_hook(
        self, mock_which: mock.Mock, mock_run: mock.Mock, run_cli, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without ``--hook`` the shipped hook is checked, not a project-local ``.claude/hooks`` copy.

        Scenario: the audit runs from a project that has no ``.claude/hooks/rtk-rewrite.js``; the plugin's own hook is
        parsed and compared, so the run is neither skipped nor unparsable.
        """
        assert run_cli() == 0
        out = capsys.readouterr().out
        assert "SKIPPED" not in out


class TestExecutableResolution:
    """Executable lookup excludes the audited checkout before consulting the platform finder."""

    @mock.patch.object(cra.shutil, "which", autospec=True)
    def test_windows_lookup_checks_pathext_candidates(
        self, mock_which: mock.Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Python finders that require an exact filename can locate external Windows executables."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        external = tmp_path / "tools"
        external.mkdir()
        executable = external / "rtk.EXE"
        monkeypatch.chdir(checkout)
        monkeypatch.setattr(cra.sys, "platform", "win32")
        monkeypatch.setattr(cra.os, "pathsep", ";")
        monkeypatch.setattr(cra.os, "get_exec_path", lambda: [str(checkout), str(external)])
        monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
        mock_which.side_effect = lambda candidate: candidate if candidate == str(executable) else None

        assert cra.resolve_rtk() == str(executable)
        assert mock_which.call_args_list == [
            mock.call(str(external / "rtk")),
            mock.call(str(external / "rtk.COM")),
            mock.call(str(executable)),
        ]

    @mock.patch.object(cra.shutil, "which", autospec=True)
    def test_cwd_path_entries_are_never_searched(self, mock_which: mock.Mock, tmp_path: Path, monkeypatch) -> None:
        """Even a Windows finder that prepends cwd receives only an explicit external candidate path."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        external = tmp_path / "tools"
        external.mkdir()
        monkeypatch.chdir(checkout)
        monkeypatch.setenv("PATH", str(checkout) + cra.os.pathsep + str(external))
        executable = external / "rtk.exe"
        mock_which.return_value = str(executable)

        assert cra.resolve_rtk() == str(executable)
        mock_which.assert_called_once_with(str(external / "rtk"))

    @mock.patch.object(cra.shutil, "which", autospec=True)
    def test_resolved_executable_inside_checkout_is_rejected(
        self, mock_which: mock.Mock, tmp_path: Path, monkeypatch
    ) -> None:
        """A finder result pointing back into the checkout is never admitted for execution."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        external = tmp_path / "tools"
        external.mkdir()
        monkeypatch.chdir(checkout)
        monkeypatch.setenv("PATH", str(external))
        mock_which.return_value = str(checkout / "rtk.exe")

        assert cra.resolve_rtk() is None
