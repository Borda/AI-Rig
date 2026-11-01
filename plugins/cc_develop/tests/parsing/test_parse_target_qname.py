"""Tests for ``bin/parse_target_qname.py`` — split a ``module::function`` suspect out of arguments.

Covers:
* Qualified-name extraction, including a leading-dash argument string
* Empty and malformed suspects collapsing to three empty fields
* ``--query-kind`` default fallback and its warning line
* Sentinel filenames, values, and the trailing newline the reload contract needs
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _develop_text import load_module

_mod = load_module(Path(__file__).resolve().parents[2] / "bin" / "parse_target_qname.py", "develop_parse_target_qname")

split_qname = _mod.split_qname
main = _mod.main

_SENTINELS = ("codemap-query-kind", "target-module", "target-fn", "target-qualified")


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated directory under a fixed session token."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "testsess")
    return tmp_path


def _read(tmp_path: Path, name: str) -> str:
    """Return the content of one ``dev-fix-<name>`` sentinel written under the ``testsess`` session."""
    return (tmp_path / f"dev-fix-{name}-testsess").read_text(encoding="utf-8")


class TestSplitQname:
    """The first ``module::function`` token in an arguments string is split into module, function and both."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            pytest.param("pkg.mod::do_thing", ("pkg.mod", "do_thing", "pkg.mod::do_thing"), id="plain"),
            pytest.param(
                "--issue 42 pkg.mod::do_thing extra", ("pkg.mod", "do_thing", "pkg.mod::do_thing"), id="leading-flag"
            ),
            pytest.param("a.b::f then c.d::g", ("a.b", "f", "a.b::f"), id="first-of-two"),
            pytest.param("no suspect here", ("", "", ""), id="absent"),
            pytest.param("", ("", "", ""), id="empty"),
            pytest.param("::orphan", ("", "", ""), id="malformed"),
        ],
    )
    def test_splits_first_suspect(self, arguments: str, expected: tuple[str, str, str]) -> None:
        """The suspect is extracted wherever it sits; an absent or malformed one yields three empty fields.

        Callers write the three fields to sentinels, so "nothing found" must be empty strings, never ``None``.
        """
        assert split_qname(arguments) == expected


class TestMain:
    """``main`` writes the codemap sentinels the fix skill reloads in later blocks."""

    def test_writes_each_sentinel_with_trailing_newline(self, session: Path) -> None:
        """Every sentinel holds its value plus the newline the ``IFS= read -r`` reload contract needs.

        A value without the newline makes ``read`` return non-zero, which the reload block treats as a missing file.
        """
        rc = main(["--query-kind", "callers", "--", "pkg.mod::do_thing"])
        written = {name: _read(session, name) for name in _SENTINELS}
        assert (rc, written) == (
            0,
            {
                "codemap-query-kind": "callers\n",
                "target-module": "pkg.mod\n",
                "target-fn": "do_thing\n",
                "target-qualified": "pkg.mod::do_thing\n",
            },
        )

    def test_writes_exactly_the_four_sentinels(self, session: Path) -> None:
        """Exactly the four documented sentinel files exist after a run, none missing and none extra."""
        main(["--query-kind", "skip", "--", "x.y::z"])
        assert sorted(path.name for path in session.iterdir()) == sorted(
            f"dev-fix-{name}-testsess" for name in _SENTINELS
        )

    def test_empty_query_kind_falls_back_to_standard(self, session: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """An empty ``--query-kind`` becomes ``standard`` and prints the unresolved warning line.

        The warning tells the user the structural-context default was used instead of their (missing) choice.
        """
        rc = main(["--query-kind", "", "--", "no suspect"])
        warning = "! CODEMAP_QUERY_KIND unresolved — using standard structural context"
        assert (rc, warning in capsys.readouterr().out, _read(session, "codemap-query-kind")) == (0, True, "standard\n")

    def test_leading_dash_arguments_do_not_break_parsing(self, session: Path) -> None:
        """``$ARGUMENTS`` routinely starts with a flag; the ``--`` separator must absorb it.

        Without the separator argparse would reject ``--issue`` as an unknown option and the skill would stop early.
        """
        rc = main(["--", "--issue 42 --team"])
        assert (rc, _read(session, "target-module")) == (0, "\n")

    def test_session_token_falls_back_to_claude_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without ``CSID`` the sentinel suffix comes from ``CLAUDE_CODE_SESSION_ID``.

        Sentinels must stay session-scoped even when only the harness variable is exported.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.delenv("CSID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-claude")
        rc = main(["--", "a.b::c"])
        written = (tmp_path / "dev-fix-target-qualified-from-claude").read_text(encoding="utf-8")
        assert (rc, written) == (0, "a.b::c\n")

    def test_unwritable_sentinel_dir_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sentinel directory that does not exist makes the run exit 1 instead of raising.

        The caller turns a non-zero status into a visible stop; a traceback would be swallowed by the shell block.
        """
        monkeypatch.setenv("TMPDIR", str(tmp_path / "does" / "not" / "exist"))
        monkeypatch.setenv("CSID", "testsess")
        assert main(["--", "a.b::c"]) == 1
