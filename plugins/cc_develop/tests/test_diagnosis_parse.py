"""Tests for ``bin/diagnosis_parse.py``.

The script extracts the ``--diagnosis`` value from a single ``$ARGUMENTS`` string (accepting both ``--diagnosis=<path>``
and ``--diagnosis <path>`` forms) and prints the resolved path to stdout. Missing files trigger exit 1 with the
documented breaking diagnostic on stderr. No subprocess involvement — pure string parsing.
"""

from __future__ import annotations

from pathlib import Path

import diagnosis_parse  # type: ignore[import-not-found]
import pytest


@pytest.mark.parametrize(
    "arguments",
    ["--diagnosis={path}", "--diagnosis {path}", "--diagnosis=relative/diag.md", "--diagnosis 'relative path/diag.md'"],
)
def test_valid_diagnosis_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: str,
) -> None:
    """Supported diagnosis forms under cwd print the parsed path and exit 0.

    Cwd is pointed at ``tmp_path`` so the containment check passes (the script rejects paths outside ``Path.cwd()`` to
    close the path-existence oracle).
    """
    monkeypatch.chdir(tmp_path)
    diag = tmp_path / ("relative path/diag.md" if "relative path" in arguments else "relative/diag.md")
    diag.parent.mkdir(parents=True, exist_ok=True)
    diag.write_text("# diagnosis\n")
    rendered = arguments.format(path=diag.as_posix())
    rc = diagnosis_parse.main([rendered])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert Path(out).resolve() == diag


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param("", id="empty-arguments"),
        pytest.param("--mode fix --team", id="unrelated-flags"),
        pytest.param("--diagnosis", id="bare-diagnosis-flag"),
        pytest.param("--team --other", id="dash-tokens-only"),
        pytest.param("--diagnosis --other-flag", id="next-flag-not-consumed-as-value"),
    ],
)
def test_arguments_without_diagnosis_value_print_empty(arguments: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Arguments carrying no diagnosis value print an empty string and exit 0.

    Covers empty arguments, unrelated flags, a bare ``--diagnosis`` and a ``--diagnosis`` followed by another option,
    which must not be consumed as its value. A blob whose tokens are ``--``-shaped is passed opaquely to
    parse_diagnosis, not argparse: argparse would reject a bare ``--``-prefixed token as an unknown option (exit 2), so
    exit 0 with empty stdout proves the blob was never fed to argparse.
    """
    rc = diagnosis_parse.main([arguments])
    assert rc == 0
    # Print of "" still emits a newline; .strip() yields empty.
    assert capsys.readouterr().out.strip() == ""


def test_no_argv_at_all(capsys: pytest.CaptureFixture[str]) -> None:
    """Script invoked with no argv tokens at all → treated as empty arguments."""
    rc = diagnosis_parse.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


@pytest.mark.parametrize(
    ("arguments", "missing_path"),
    [
        pytest.param("--diagnosis /nonexistent/diag/path.md", "/nonexistent/diag/path.md", id="space-form"),
        pytest.param("--diagnosis=/no/such/file.md", "/no/such/file.md", id="equals-form"),
    ],
)
def test_missing_diagnosis_file_exits_1(arguments: str, missing_path: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Reject a missing diagnosis file, in either flag form, with the documented diagnostic.

    The stderr block begins with ``! BREAKING``, names the missing path and carries a ``Fix:`` line.
    """
    rc = diagnosis_parse.main([arguments])
    assert rc == 1
    err = capsys.readouterr().err
    assert "! BREAKING" in err
    assert missing_path in err
    assert "Fix:" in err


def test_combined_with_other_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Extract an inline diagnosis path without consuming surrounding options."""
    monkeypatch.chdir(tmp_path)
    diag = tmp_path / "d.md"
    diag.write_text("x")
    rc = diagnosis_parse.main([f"--mode fix --diagnosis={diag.as_posix()} --team"])
    assert rc == 0
    assert Path(capsys.readouterr().out.strip()) == diag


@pytest.mark.parametrize("form", ["absolute", "relative_parent"])
def test_rejects_path_outside_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], form: str
) -> None:
    """Existing file outside cwd → exit 1 with ``! BREAKING`` (closes path-existence oracle).

    Without containment, ``--diagnosis /etc/passwd`` would exit 0 and echo the absolute path, leaking which system files
    exist. The containment check rejects any resolved path that does not live under ``Path.cwd()``.
    """
    # Build an isolated cwd; the diag file lives in a sibling dir, deliberately outside cwd.
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    diag = outside / "leak.md"
    diag.write_text("# outside\n")
    arg = f"--diagnosis={diag.as_posix()}" if form == "absolute" else "--diagnosis=../outside/leak.md"
    rc = diagnosis_parse.main([arg])
    assert rc == 1
    err = capsys.readouterr().err
    assert "outside project root" in err


def test_rejects_symlink_inside_cwd_pointing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A symlink under cwd that resolves outside cwd is rejected."""
    cwd_dir = tmp_path / "project"
    cwd_dir.mkdir()
    monkeypatch.chdir(cwd_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "leak.md"
    target.write_text("# outside\n")
    link = cwd_dir / "link.md"
    link.symlink_to(target)
    rc = diagnosis_parse.main(["--diagnosis=link.md"])
    assert rc == 1
    assert "outside project root" in capsys.readouterr().err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Print usage to stdout and exit 0 (argparse default)."""
    with pytest.raises(SystemExit) as exc:
        diagnosis_parse.main(["--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()
