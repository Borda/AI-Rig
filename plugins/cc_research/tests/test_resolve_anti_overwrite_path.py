"""Tests for ``bin/resolve-anti-overwrite-path.py`` — first free ``<dir>/<stem>.md`` resolver.

Covers the free path, the ``-2``/``-3`` counter when names are taken (files and directories), the textual path form, and
rejection of empty arguments and ``..`` traversal.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "resolve-anti-overwrite-path.py"
_spec = importlib.util.spec_from_file_location("research_resolve_anti_overwrite_path", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

main = _mod.main


class TestResolve:
    """The printed path for a given directory state."""

    @pytest.mark.parametrize(
        ("taken", "expected"),
        [
            pytest.param([], "report.md", id="free"),
            pytest.param(["report.md"], "report-2.md", id="first-taken"),
            pytest.param(["report.md", "report-2.md"], "report-3.md", id="two-taken"),
            pytest.param(["report.md/"], "report-2.md", id="directory-squatting-on-name"),
        ],
    )
    def test_first_free_name(
        self, taken: list[str], expected: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The counter suffix starts at 2 and skips every existing entry, directories included.

        A directory on the target name must also push the counter forward, else the caller's Write fails on a path this
        script declared free.
        """
        for name in taken:
            target = tmp_path / name
            if name.endswith("/"):
                target.mkdir()
            else:
                target.write_text("x", encoding="utf-8")

        code = main(["resolve-anti-overwrite-path.py", str(tmp_path), "report"])

        assert code == 0
        assert capsys.readouterr().out.strip() == f"{tmp_path}/{expected}"

    def test_creates_nothing(self, tmp_path: Path) -> None:
        """Resolution is pure: the directory is left untouched, callers keep their own ``mkdir -p``."""
        main(["resolve-anti-overwrite-path.py", str(tmp_path / "absent"), "report"])

        assert not (tmp_path / "absent").exists()

    def test_relative_dir_is_printed_verbatim(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A ``./`` prefix survives, since callers capture the string as their report path."""
        main(["resolve-anti-overwrite-path.py", "./.reports/research", "no-such-stem-for-this-test"])

        assert capsys.readouterr().out.strip() == "./.reports/research/no-such-stem-for-this-test.md"


class TestRejected:
    """Arguments that exit 2 with nothing printed to stdout."""

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["resolve-anti-overwrite-path.py"], id="no-args"),
            pytest.param(["resolve-anti-overwrite-path.py", "dir"], id="no-stem"),
            pytest.param(["resolve-anti-overwrite-path.py", "", "stem"], id="empty-dir"),
            pytest.param(["resolve-anti-overwrite-path.py", "dir", ""], id="empty-stem"),
            pytest.param(["resolve-anti-overwrite-path.py", "../out", "stem"], id="dir-traversal"),
            pytest.param(["resolve-anti-overwrite-path.py", "dir", "a/../../b"], id="stem-traversal"),
            pytest.param(["resolve-anti-overwrite-path.py", "dir\\..\\out", "stem"], id="backslash-traversal"),
        ],
    )
    def test_invalid_arguments(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        """Missing, empty or ``..``-bearing arguments exit 2 and print no path."""
        code = main(argv)

        assert code == 2
        assert capsys.readouterr().out == ""
