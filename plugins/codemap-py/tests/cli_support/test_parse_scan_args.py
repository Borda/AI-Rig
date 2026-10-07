"""Tests for ``bin/parse_scan_args.py`` — flag extraction from $ARGUMENTS strings."""

from __future__ import annotations

from pathlib import Path

import pytest
from parse_scan_args import main
from parse_scan_args import parse_scan_args as parse

# ---------------------------------------------------------------------------
# parse_scan_args() — pure function; returns list[str] of unquoted tokens
# ---------------------------------------------------------------------------


class TestParseScanArgs:
    """Extract the ``--root`` and ``--incremental`` flags from an ``$ARGUMENTS`` string into unquoted tokens.

    All three quoting styles are stripped and the root value is returned raw; the output order is fixed (``--root``
    first, then ``--incremental``); unrecognised or empty input yields no tokens; and the root option is found
    regardless of its position among surrounding noise.
    """

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("--root /abs/path", ["--root", "/abs/path"], id="root-unquoted"),
            pytest.param("--root '/abs/path'", ["--root", "/abs/path"], id="root-single-quoted"),
            pytest.param('--root "/abs/path"', ["--root", "/abs/path"], id="root-double-quoted"),
            pytest.param(
                "--root '/abs path/with spaces'", ["--root", "/abs path/with spaces"], id="root-single-quoted-spaces"
            ),
            pytest.param(
                '--root "/abs path/with spaces"', ["--root", "/abs path/with spaces"], id="root-double-quoted-spaces"
            ),
            pytest.param(
                "--root '/path/with$dollar'", ["--root", "/path/with$dollar"], id="root-special-chars-returned-raw"
            ),
            pytest.param("--incremental", ["--incremental"], id="incremental-alone"),
            pytest.param("--root /tmp/x", ["--root", "/tmp/x"], id="incremental-absent"),
            pytest.param(
                "--root /abs/path --incremental",
                ["--root", "/abs/path", "--incremental"],
                id="root-abs-path---incremental",
            ),
            # Output order is fixed: ``--root`` first, then ``--incremental``.
            pytest.param(
                "--incremental --root /tmp/x", ["--root", "/tmp/x", "--incremental"], id="incremental-then-root"
            ),
            pytest.param(
                "--root '/p with space' --incremental",
                ["--root", "/p with space", "--incremental"],
                id="both-with-quoted-root-containing-space",
            ),
            pytest.param("", [], id="empty-string"),
            pytest.param("--unknown foo --other bar", [], id="no-recognised-flags"),
            pytest.param("   ", [], id="only-whitespace"),
            pytest.param(
                "--incremental --root /abs/path",
                ["--root", "/abs/path", "--incremental"],
                id="incremental---root-abs-path",
            ),
            pytest.param("prefix-noise --root /abs/path", ["--root", "/abs/path"], id="prefix-noise---root-abs-path"),
            pytest.param("--root /abs/path trailing-noise", ["--root", "/abs/path"], id="root-abs-path-trailing-noise"),
        ],
    )
    def test_parse_returns_unquoted_tokens(self, arguments: str, expected: list[str]) -> None:
        assert parse(arguments) == expected


# ---------------------------------------------------------------------------
# main() — CLI entry point; default stdout is shell-quoted for legacy eval use
# ---------------------------------------------------------------------------


class TestMain:
    @pytest.mark.parametrize(
        ("argv", "expected_out"),
        [
            pytest.param(["--root /tmp/x --incremental"], "--root /tmp/x --incremental", id="with-arg"),
            pytest.param([""], "", id="with-empty-arg"),
            # Calling with no argv at all — must not crash; should print empty line.
            pytest.param([], "", id="with-no-args"),
            pytest.param(["--help"], "", id="leading-help-is-blob-not-flag"),
            pytest.param(
                ["--root /abs/proj --incremental"], "--root /abs/proj --incremental", id="blob-reaches-inner-parser"
            ),
            pytest.param(
                ["--root /abs/proj", "--unrecognised-outer"], "--root /abs/proj", id="unknown-outer-token-ignored"
            ),
        ],
    )
    def test_main_prints_inner_parsed_tokens(
        self, capsys: pytest.CaptureFixture[str], argv: list[str], expected_out: str
    ) -> None:
        """``main`` exits 0 and prints the tokens the inner parser extracted from the opaque blob argument.

        The blob feeds the inner parser rather than argparse, so a leading ``--help`` is blob content (empty output)
        rather than a flag, and an unrecognised OUTER flag is silently ignored (legacy strictness) instead of raising or
        exiting nonzero.
        """
        rc = main(argv)
        out = capsys.readouterr().out
        assert rc == 0
        assert out.strip() == expected_out

    def test_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print help successfully when requested as an outer option.

        arg[0] is the opaque $ARGUMENTS blob, so ``-h``/``--help`` is only an argparse flag when it follows the blob
        (here an empty blob); a leading ``--help`` is treated as blob content, not a flag.
        """
        with pytest.raises(SystemExit) as exc:
            main(["", "--help"])
        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()


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
