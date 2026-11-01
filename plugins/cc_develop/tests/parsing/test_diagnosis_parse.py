"""Tests for ``bin/diagnosis_parse.py``.

The script extracts the ``--diagnosis`` value from a single ``$ARGUMENTS`` string (accepting both ``--diagnosis=<path>``
and ``--diagnosis <path>`` forms) and prints the resolved path to stdout. Missing files trigger exit 1 with the
documented breaking diagnostic on stderr. No subprocess involvement — pure string parsing.
"""

from __future__ import annotations

from pathlib import Path

import diagnosis_parse  # type: ignore[import-not-found]
import pytest


@pytest.fixture
def outside_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from ``<tmp>/project`` and return an existing file in the sibling ``<tmp>/outside`` directory."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    (tmp_path / "outside").mkdir()
    leak = tmp_path / "outside" / "leak.md"
    leak.write_text("# outside\n")
    return leak


class TestDiagnosisPathExtraction:
    """The diagnosis path is parsed out of one ``$ARGUMENTS`` blob and printed when it lives under the cwd."""

    @pytest.mark.parametrize(
        ("arguments", "relative"),
        [
            pytest.param("--diagnosis={path}", "relative/diag.md", id="equals-form-absolute"),
            pytest.param("--diagnosis {path}", "relative/diag.md", id="space-form-absolute"),
            pytest.param("--diagnosis=relative/diag.md", "relative/diag.md", id="equals-form-relative"),
            pytest.param("--diagnosis 'relative path/diag.md'", "relative path/diag.md", id="quoted-path-with-space"),
            pytest.param("--mode fix --diagnosis={path} --team", "d.md", id="between-other-flags"),
        ],
    )
    def test_valid_forms_print_the_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        arguments: str,
        relative: str,
    ) -> None:
        """Supported diagnosis forms under cwd print the parsed path and exit 0.

        Cwd is pointed at ``tmp_path`` so the containment check passes (the script rejects paths outside ``Path.cwd()``
        to close the path-existence oracle). The inline form also proves surrounding options are not consumed.
        """
        monkeypatch.chdir(tmp_path)
        diag = tmp_path / relative
        diag.parent.mkdir(parents=True, exist_ok=True)
        diag.write_text("# diagnosis\n")
        rc = diagnosis_parse.main([arguments.format(path=diag.as_posix())])
        assert (rc, Path(capsys.readouterr().out.strip()).resolve()) == (0, diag)

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param([], id="no-argv-tokens"),
            pytest.param([""], id="empty-arguments"),
            pytest.param(["--mode fix --team"], id="unrelated-flags"),
            pytest.param(["--diagnosis"], id="bare-diagnosis-flag"),
            pytest.param(["--team --other"], id="dash-tokens-only"),
            pytest.param(["--diagnosis --other-flag"], id="next-flag-not-consumed-as-value"),
        ],
    )
    def test_no_diagnosis_value_prints_empty(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        """Arguments carrying no diagnosis value print an empty string and exit 0.

        Covers no argv at all, empty arguments, unrelated flags, a bare ``--diagnosis`` and a ``--diagnosis`` followed
        by another option, which must not be consumed as its value. A blob whose tokens are ``--``-shaped is passed
        opaquely to parse_diagnosis, not argparse: argparse would reject a bare ``--``-prefixed token as an unknown
        option (exit 2), so exit 0 with empty stdout proves the blob was never fed to argparse.
        """
        rc = diagnosis_parse.main(argv)
        # Print of "" still emits a newline; .strip() yields empty.
        assert (rc, capsys.readouterr().out.strip()) == (0, "")

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Print usage to stdout and exit 0 (argparse default).

        ``--help`` is the one flag argparse still owns; it must answer before any path handling.
        """
        with pytest.raises(SystemExit) as exc:
            diagnosis_parse.main(["--help"])
        assert (exc.value.code, "usage" in capsys.readouterr().out.lower()) == (0, True)


class TestDiagnosisRejection:
    """A diagnosis that is missing or escapes the project root is refused with exit 1."""

    @pytest.mark.parametrize(
        ("arguments", "missing_path"),
        [
            pytest.param("--diagnosis /nonexistent/diag/path.md", "/nonexistent/diag/path.md", id="space-form"),
            pytest.param("--diagnosis=/no/such/file.md", "/no/such/file.md", id="equals-form"),
        ],
    )
    def test_missing_file_exits_1(self, arguments: str, missing_path: str, capsys: pytest.CaptureFixture[str]) -> None:
        """Reject a missing diagnosis file, in either flag form, with the documented diagnostic.

        The stderr block begins with ``! BREAKING``, names the missing path and carries a ``Fix:`` line, so the user
        learns which file to create instead of seeing a bare traceback.
        """
        rc = diagnosis_parse.main([arguments])
        err = capsys.readouterr().err
        assert (rc, "! BREAKING" in err, missing_path in err, "Fix:" in err) == (1, True, True, True)

    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param("--diagnosis={outside}", id="absolute"),
            pytest.param("--diagnosis=../outside/leak.md", id="relative-parent"),
        ],
    )
    def test_path_outside_cwd_exits_1(
        self, outside_file: Path, capsys: pytest.CaptureFixture[str], arguments: str
    ) -> None:
        """Existing file outside cwd → exit 1 with ``! BREAKING`` (closes path-existence oracle).

        Without containment, ``--diagnosis /etc/passwd`` would exit 0 and echo the absolute path, leaking which system
        files exist. The containment check rejects any resolved path that does not live under ``Path.cwd()``.
        """
        rc = diagnosis_parse.main([arguments.format(outside=outside_file.as_posix())])
        assert (rc, "outside project root" in capsys.readouterr().err) == (1, True)

    def test_symlink_pointing_outside_cwd_exits_1(self, outside_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A symlink under cwd that resolves outside cwd is rejected.

        The link's own name is inside the project, so only resolving it before the containment check catches it.
        """
        Path("link.md").symlink_to(outside_file)
        rc = diagnosis_parse.main(["--diagnosis=link.md"])
        assert (rc, "outside project root" in capsys.readouterr().err) == (1, True)
