"""Tests for ``bin/diagnosis_parse.py``.

The script extracts the ``--diagnosis`` value from a single ``$ARGUMENTS`` string (accepting both ``--diagnosis=<path>``
and ``--diagnosis <path>`` forms) and prints the resolved path to stdout. Missing files trigger exit 1 with the
documented breaking diagnostic on stderr. No subprocess involvement — pure string parsing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import diagnosis_parse  # type: ignore[import-not-found]


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


def test_no_diagnosis(capsys: pytest.CaptureFixture[str]) -> None:
    """Empty arguments → prints empty string, exits 0."""
    rc = diagnosis_parse.main([""])
    assert rc == 0
    # Print of "" still emits a newline; .strip() yields empty.
    assert capsys.readouterr().out.strip() == ""


def test_no_diagnosis_unrelated_flags(capsys: pytest.CaptureFixture[str]) -> None:
    """Arguments without ``--diagnosis`` → prints empty string, exits 0."""
    rc = diagnosis_parse.main(["--mode fix --team"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_no_argv_at_all(capsys: pytest.CaptureFixture[str]) -> None:
    """Script invoked with no argv tokens at all → treated as empty arguments."""
    rc = diagnosis_parse.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_file_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    """Reject a missing diagnosis file with the documented diagnostic.

    The stderr block begins with ``! BREAKING``.
    """
    rc = diagnosis_parse.main(["--diagnosis /nonexistent/diag/path.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "! BREAKING" in err
    assert "/nonexistent/diag/path.md" in err
    assert "Fix:" in err


def test_next_flag_not_treated_as_value(capsys: pytest.CaptureFixture[str]) -> None:
    """Avoid consuming another option as the diagnosis value."""
    rc = diagnosis_parse.main(["--diagnosis --other-flag"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


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


def test_equals_form_missing_file_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Reject a nonexistent inline diagnosis path."""
    rc = diagnosis_parse.main(["--diagnosis=/no/such/file.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "! BREAKING" in err
    assert "/no/such/file.md" in err


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """Print usage to stdout and exit 0 (argparse default)."""
    with pytest.raises(SystemExit) as exc:
        diagnosis_parse.main(["--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


@pytest.mark.parametrize("blob", ["--mode fix --team", "--diagnosis", "--team --other"])
def test_blob_dash_tokens_reach_inner_parser_unmangled(blob: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A blob whose tokens are ``--``-shaped is passed opaquely to parse_diagnosis, not argparse.

    argparse would reject a bare ``--``-prefixed token as an unknown option (exit 2). The script must instead hand the
    whole blob to the inner scanner, which finds no diagnosis value and exits 0 with empty stdout — proving the blob was
    never fed to argparse.
    """
    rc = diagnosis_parse.main([blob])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""
