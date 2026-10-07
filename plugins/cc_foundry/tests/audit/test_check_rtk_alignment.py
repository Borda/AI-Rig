"""Tests for check_rtk_alignment bin script.

Covers RTK_PREFIXES parsing, help-output parsing, the two-direction comparison, and the CLI skip paths (rtk absent, hook
absent, unparsable prefix list).
"""

from __future__ import annotations

from pathlib import Path

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


class TestHookPrefixes:
    """Covers parsing RTK_PREFIXES out of the hook source."""

    def test_reads_single_and_double_quoted_entries(self, tmp_path: Path) -> None:
        """Both quote styles inside the array are returned."""
        hook = tmp_path / "rtk-rewrite.js"
        hook.write_text(_HOOK_SRC, encoding="utf-8")
        assert cra.hook_prefixes(hook) == ["search", "read", "grep"]

    def test_missing_array_yields_empty(self, tmp_path: Path) -> None:
        """A hook without the array yields no prefixes rather than raising."""
        hook = tmp_path / "rtk-rewrite.js"
        hook.write_text("const OTHER = [];\n", encoding="utf-8")
        assert cra.hook_prefixes(hook) == []

    def test_unreadable_file_yields_empty(self, tmp_path: Path) -> None:
        """A path that does not exist yields no prefixes."""
        assert cra.hook_prefixes(tmp_path / "absent.js") == []


class TestHelpCommands:
    """Covers parsing subcommand names from `rtk --help`."""

    def test_extracts_indented_commands(self) -> None:
        """Two-to-four space indented names are picked up."""
        assert cra.help_commands(_HELP) == {"search", "read", "grep", "gain"}

    def test_ignores_unindented_lines(self) -> None:
        """A flush-left word is not a subcommand."""
        assert "usage" not in cra.help_commands("usage: rtk\n  search  x\n")


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
        """A filterable command the hook omits is reported as missing."""
        _, missing = cra.compare(["search"], _HELP)
        assert missing == ["grep", "read"]


class TestCli:
    """Covers the command-line skip and failure paths."""

    def test_rtk_absent_skips(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """No rtk binary means skip with exit 0."""
        monkeypatch.setattr(cra.shutil, "which", lambda _name: None)
        monkeypatch.setattr("sys.argv", ["check_rtk_alignment.py"])
        assert cra.main() == 0
        assert "rtk not installed" in capsys.readouterr().out

    def test_hook_absent_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing hook file means skip with exit 0."""
        monkeypatch.setattr(cra.shutil, "which", lambda _name: "/usr/bin/rtk")
        monkeypatch.setattr("sys.argv", ["check_rtk_alignment.py", "--hook", str(tmp_path / "absent.js")])
        assert cra.main() == 0
        assert "not found" in capsys.readouterr().out

    def test_unparseable_prefix_list_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hook without RTK_PREFIXES skips rather than failing."""
        hook = tmp_path / "rtk-rewrite.js"
        hook.write_text("// nothing here\n", encoding="utf-8")
        monkeypatch.setattr(cra.shutil, "which", lambda _name: "/usr/bin/rtk")
        monkeypatch.setattr("sys.argv", ["check_rtk_alignment.py", "--hook", str(hook)])
        assert cra.main() == 0
        assert "could not parse RTK_PREFIXES" in capsys.readouterr().out

    def test_rtk_help_timeout_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A hung `rtk --help` degrades to a skip instead of a traceback.

        The shell original could not crash here — it had only a `# timeout:` comment, with no enforcement — so an
        unhandled TimeoutExpired would be a regression.
        """
        hook = tmp_path / "rtk-rewrite.js"
        hook.write_text('const RTK_PREFIXES = ["search"];\n', encoding="utf-8")
        monkeypatch.setattr(cra.shutil, "which", lambda _name: "/usr/bin/rtk")
        monkeypatch.setattr("sys.argv", ["check_rtk_alignment.py", "--hook", str(hook)])

        def _hang(*_args: object, **_kwargs: object) -> None:
            raise cra.subprocess.TimeoutExpired(cmd="rtk", timeout=1.0)

        monkeypatch.setattr(cra.subprocess, "run", _hang)
        assert cra.main() == 0
        assert "⚠ SKIPPED: Check 10 — `rtk --help` failed or timed out" in capsys.readouterr().out

    def test_misaligned_prefixes_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unknown hook prefix is named on stdout; the exit stays 0 (report-only)."""
        hook = tmp_path / "rtk-rewrite.js"
        hook.write_text('const RTK_PREFIXES = ["bogus"];\n', encoding="utf-8")
        monkeypatch.setattr(cra.shutil, "which", lambda _name: "/usr/bin/rtk")
        monkeypatch.setattr("sys.argv", ["check_rtk_alignment.py", "--hook", str(hook)])

        class _Proc:
            stdout = _HELP
            stderr = ""

        monkeypatch.setattr(cra.subprocess, "run", lambda *_a, **_k: _Proc())
        assert cra.main() == 0
        out = capsys.readouterr().out
        assert "! INVALID hook prefix: 'bogus'" in out
