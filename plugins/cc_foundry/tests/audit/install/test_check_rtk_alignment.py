"""Tests for check_rtk_alignment bin script.

Covers RTK_PREFIXES parsing, help-output parsing, the two-direction comparison, and the CLI skip paths (rtk absent, hook
absent, unparsable prefix list). The ``rtk`` binary and its ``--help`` call are the only external boundaries; both are
mocked, so no test spawns a process.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import check_rtk_alignment as cra
import pytest

_HOOK_SRC = """
const RTK_PREFIXES = [
  "search",
  "read",
  'grep',
];
"""

_HELP = """
Usage: rtk <command>

Commands:
  search    search things
  read      read things
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
    """Covers parsing RTK_PREFIXES out of the hook source."""

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            pytest.param(_HOOK_SRC, ["search", "read", "grep"], id="single-and-double-quoted-entries"),
            pytest.param("const OTHER = [];\n", [], id="missing-array"),
        ],
    )
    def test_reads_entries_or_yields_empty(self, hook_file: Callable[[str], Path], source: str, expected: list) -> None:
        """Both quote styles inside the array are returned; a hook without the array yields no prefixes.

        The hook is JavaScript source, so the array is read textually; a hook lacking ``RTK_PREFIXES`` must give an
        empty list rather than raise.
        """
        assert cra.hook_prefixes(hook_file(source)) == expected

    def test_unreadable_file_yields_empty(self, tmp_path: Path) -> None:
        """A path that does not exist yields no prefixes.

        The CLI decides what a missing hook means; the parser itself must not raise on it.
        """
        assert cra.hook_prefixes(tmp_path / "absent.js") == []


class TestHelpCommands:
    """Covers parsing subcommand names from `rtk --help`."""

    @pytest.mark.parametrize(
        ("help_text", "expected"),
        [
            pytest.param(_HELP, {"search", "read", "grep", "gain"}, id="indented-commands"),
            pytest.param("usage: rtk\n  search  x\n", {"search"}, id="unindented-word-ignored"),
        ],
    )
    def test_extracts_only_indented_names(self, help_text: str, expected: set[str]) -> None:
        """Indented names are subcommands; a flush-left word such as ``usage`` is not.

        The help layout puts the command list under an indented heading, so only that indentation marks a command.
        """
        assert cra.help_commands(help_text) == expected


class TestCompare:
    """Covers the two-direction comparison."""

    def test_aligned_lists_report_nothing(self) -> None:
        """Hook prefixes matching the non-meta commands produce no findings.

        The meta command ``gain`` is not expected in the hook, so it is never reported as missing.
        """
        invalid, missing = cra.compare(["search", "read", "grep"], _HELP)
        assert invalid == []
        assert missing == []

    @pytest.mark.parametrize(
        ("prefixes", "help_text", "expected_invalid"),
        [
            pytest.param(["search", "bogus"], _HELP, ["bogus"], id="unknown-prefix"),
            # Word matching does not degrade to substring: `sea` is not `search`.
            pytest.param(["sea"], _HELP, ["sea"], id="substring-alone"),
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
        finding, while degrading to substring matching would accept ``sea`` for ``search``; ``-`` counts as a word
        boundary.
        """
        invalid, _ = cra.compare(prefixes, help_text)
        assert invalid == expected_invalid

    def test_unlisted_command_is_missing(self) -> None:
        """A filterable command the hook omits is reported as missing.

        With only ``search`` in the hook, ``grep`` and ``read`` are filterable commands it fails to cover.
        """
        _, missing = cra.compare(["search"], _HELP)
        assert missing == ["grep", "read"]


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
    def test_unparseable_prefix_list_skips(
        self, mock_which: mock.MagicMock, hook_file: Callable[[str], Path], run_cli, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hook without RTK_PREFIXES skips rather than failing.

        The hook exists but carries no readable prefix array, so the comparison cannot run and must say so.
        """
        assert run_cli("--hook", str(hook_file("// nothing here\n"))) == 0
        assert "could not parse RTK_PREFIXES" in capsys.readouterr().out

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
        hook = hook_file('const RTK_PREFIXES = ["search"];\n')
        assert run_cli("--hook", str(hook)) == 0
        assert "⚠ SKIPPED: Check 10 — `rtk --help` failed or timed out" in capsys.readouterr().out

    @mock.patch.object(
        cra.subprocess,
        "run",
        autospec=True,
        return_value=subprocess.CompletedProcess(args=["rtk", "--help"], returncode=0, stdout=_HELP, stderr=""),
    )
    @mock.patch.object(cra.shutil, "which", autospec=True, return_value=_RTK_PATH)
    def test_misaligned_prefixes_reported(
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
        hook = hook_file('const RTK_PREFIXES = ["bogus"];\n')
        assert run_cli("--hook", str(hook)) == 0
        assert "! INVALID hook prefix: 'bogus'" in capsys.readouterr().out

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
        hook = hook_file('const RTK_PREFIXES = ["search"];\n')

        assert run_cli("--hook", str(hook)) == 0
        assert mock_run.call_args.args[0] == [str(Path(_RTK_PATH).resolve()), "--help"]


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
