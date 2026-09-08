"""Tests for ``bin/parse_scan_args.py`` — flag extraction from $ARGUMENTS strings."""

from __future__ import annotations

from pathlib import Path

import pytest


from parse_scan_args import main, parse_scan_args as parse  # noqa: E402


# ---------------------------------------------------------------------------
# parse_scan_args() — pure function; returns list[str] of unquoted tokens
# ---------------------------------------------------------------------------


class TestRootValueExtraction:
    """All three quoting styles must be stripped; returned value is the raw path."""

    def test_unquoted_path(self) -> None:
        assert parse("--root /abs/path") == ["--root", "/abs/path"]

    def test_single_quoted_path(self) -> None:
        assert parse("--root '/abs/path'") == ["--root", "/abs/path"]

    def test_double_quoted_path(self) -> None:
        assert parse('--root "/abs/path"') == ["--root", "/abs/path"]

    def test_single_quoted_path_with_spaces(self) -> None:
        assert parse("--root '/abs path/with spaces'") == ["--root", "/abs path/with spaces"]

    def test_double_quoted_path_with_spaces(self) -> None:
        assert parse('--root "/abs path/with spaces"') == ["--root", "/abs path/with spaces"]

    def test_path_with_special_chars_returned_raw(self) -> None:
        assert parse("--root '/path/with$dollar'") == ["--root", "/path/with$dollar"]


class TestIncrementalFlag:
    def test_incremental_alone(self) -> None:
        assert parse("--incremental") == ["--incremental"]

    def test_incremental_absent(self) -> None:
        assert parse("--root /tmp/x") == ["--root", "/tmp/x"]


class TestBothFlags:
    def test_root_then_incremental(self) -> None:
        assert parse("--root /abs/path --incremental") == ["--root", "/abs/path", "--incremental"]

    def test_incremental_then_root(self) -> None:
        # Output order is fixed: ``--root`` first, then ``--incremental``.
        assert parse("--incremental --root /tmp/x") == ["--root", "/tmp/x", "--incremental"]

    def test_both_with_quoted_root_containing_space(self) -> None:
        assert parse("--root '/p with space' --incremental") == ["--root", "/p with space", "--incremental"]


class TestEmptyAndNeither:
    def test_empty_string(self) -> None:
        assert parse("") == []

    def test_no_recognised_flags(self) -> None:
        assert parse("--unknown foo --other bar") == []

    def test_only_whitespace(self) -> None:
        assert parse("   ") == []


class TestRootPosition:
    """Detect the root option regardless of its position in the argument string."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param(
                "--root /abs/path --incremental",
                ["--root", "/abs/path", "--incremental"],
                id="root-abs-path---incremental",
            ),
            pytest.param(
                "--incremental --root /abs/path",
                ["--root", "/abs/path", "--incremental"],
                id="incremental---root-abs-path",
            ),
            pytest.param("prefix-noise --root /abs/path", ["--root", "/abs/path"], id="prefix-noise---root-abs-path"),
            pytest.param("--root /abs/path trailing-noise", ["--root", "/abs/path"], id="root-abs-path-trailing-noise"),
        ],
    )
    def test_position_variants(self, arguments: str, expected: list[str]) -> None:
        assert parse(arguments) == expected


# ---------------------------------------------------------------------------
# main() — CLI entry point; default stdout is shell-quoted for legacy eval use
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_with_arg(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main(["--root /tmp/x --incremental"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == "--root /tmp/x --incremental"

    def test_main_with_empty_arg(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = main([""])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == ""

    def test_main_with_no_args(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Calling with no argv at all — must not crash; should print empty line.
        rc = main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == ""

    def test_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print help successfully when requested as an outer option.

        arg[0] is the opaque $ARGUMENTS blob, so ``-h``/``--help`` is only an argparse flag when it follows the blob
        (here an empty blob); a leading ``--help`` is treated as blob content, not a flag.
        """
        with pytest.raises(SystemExit) as exc:
            main(["", "--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_leading_help_is_blob_not_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A leading ``--help`` is the blob positional (no ``--root``/``--incremental``) → empty output, exit 0."""
        rc = main(["--help"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == ""


class TestSkillCallSiteRegression:
    """Golden invocation matching scan-codebase SKILL.md: blob positional + outer ``--nul-output``."""

    def test_nul_output_golden(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exact SKILL.md argv shape writes NUL-delimited tokens; no stdout, exit 0."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        args_file = tmp_path / "codemap-scan-args-nul"
        rc = main(["--root /abs/proj --incremental", "--nul-output", str(args_file)])
        assert rc == 0
        assert capsys.readouterr().out == ""
        assert args_file.read_bytes() == b"--root\x00/abs/proj\x00--incremental\x00"

    def test_blob_reaches_inner_parser_unmangled(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A blob whose ``--root``/``--incremental`` tokens must feed the inner parser, not argparse."""
        rc = main(["--root /abs/proj --incremental"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == "--root /abs/proj --incremental"

    def test_unknown_outer_token_silently_ignored(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Legacy 'ignore' strictness: an unrecognised OUTER flag must not raise or exit nonzero."""
        rc = main(["--root /abs/proj", "--unrecognised-outer"])
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == "--root /abs/proj"
