"""Tests for ``bin/parse_scan_args.py`` — flag extraction from $ARGUMENTS strings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from parse_scan_args import main
from parse_scan_args import parse_scan_args as parse


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
            pytest.param(
                "--root /abs/path --incremental", ["--root", "/abs/path", "--incremental"], id="root-then-incremental"
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
            pytest.param("prefix-noise --root /abs/path", ["--root", "/abs/path"], id="leading-noise"),
            pytest.param("--root /abs/path trailing-noise", ["--root", "/abs/path"], id="trailing-noise"),
        ],
    )
    def test_returns_unquoted_tokens(self, arguments: str, expected: list[str]) -> None:
        """Recognised flags come back as unquoted tokens in fixed order; everything else is dropped.

        A skill passes the raw ``$ARGUMENTS`` blob, so quoting, whitespace-only input and unrelated words around the
        flags must neither leak into the result nor change which tokens are returned.
        """
        assert parse(arguments) == expected


class TestMain:
    """CLI entry point: the opaque ``$ARGUMENTS`` blob is parsed by the inner parser, not by argparse."""

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
    def test_prints_inner_parsed_tokens(
        self, capsys: pytest.CaptureFixture[str], argv: list[str], expected_out: str
    ) -> None:
        """``main`` exits 0 and prints the tokens the inner parser extracted from the opaque blob argument.

        The blob feeds the inner parser rather than argparse, so a leading ``--help`` is blob content (empty output)
        rather than a flag, and an unrecognised OUTER flag is silently ignored (legacy strictness) instead of raising or
        exiting nonzero.
        """
        rc = main(argv)

        assert rc == 0
        assert capsys.readouterr().out.strip() == expected_out

    def test_help_exits_0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print help successfully when requested as an outer option.

        arg[0] is the opaque $ARGUMENTS blob, so ``-h``/``--help`` is only an argparse flag when it follows the blob
        (here an empty blob); a leading ``--help`` is treated as blob content, not a flag.
        """
        with pytest.raises(SystemExit) as exc:
            main(["", "--help"])

        assert exc.value.code == 0
        assert "usage" in capsys.readouterr().out.lower()


class TestNulOutput:
    """The ``--nul-output`` file holds NUL-delimited tokens and opens only a path that passed the TMPDIR check."""

    def test_skill_call_site_argv_writes_nul_tokens(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exact SKILL.md argv shape (blob positional + outer ``--nul-output``) writes NUL tokens; no stdout, exit 0.

        This is the golden invocation scan-codebase makes; a drift in either the argv handling or the file format breaks
        the skill's shell read-back.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        args_file = tmp_path / "codemap-scan-args-nul"

        rc = main(["--root /abs/proj --incremental", "--nul-output", str(args_file)])

        assert rc == 0
        assert capsys.readouterr().out == ""
        assert args_file.read_bytes() == b"--root\x00/abs/proj\x00--incremental\x00"

    def test_directory_target_reports_error_instead_of_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A target inside TMPDIR that cannot be opened for writing exits 1 with a one-line stderr message.

        A directory planted at the expected sentinel name made the former bare ``open`` raise an uncaught
        ``IsADirectoryError``; the caller then saw a traceback rather than the documented non-zero exit.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        target = tmp_path / "codemap-scan-args-nul"
        target.mkdir()

        rc = main(["--root /abs/proj", "--nul-output", str(target)])

        assert rc == 1
        assert "cannot open --nul-output" in capsys.readouterr().err

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no O_NOFOLLOW")
    def test_symlink_swapped_in_after_check_is_not_followed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A symlink at the checked path is refused at open time, leaving its target untouched.

        ``Path.resolve`` is pinned to the identity so the containment check passes on the unresolved path, which is
        exactly the state after an attacker swaps a symlink in between the check and the open. The open must then fail
        rather than write through the link into a file the caller never named.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        link = tmp_path / "codemap-scan-args-nul"
        link.symlink_to(victim)
        monkeypatch.setattr(Path, "resolve", lambda self, strict=False: self)

        rc = main(["--root /abs/proj", "--nul-output", str(link)])

        assert rc == 1
        assert victim.read_text() == "keep"
        assert "cannot open --nul-output" in capsys.readouterr().err
