"""Tests for ``bin/parse_target_qname.py`` — split a ``module::function`` suspect out of arguments.

Covers:
* Qualified-name extraction, including a leading-dash argument string
* Empty and malformed suspects collapsing to three empty fields
* ``--query-kind`` default fallback and its warning line
* Sentinel filenames, values, and the trailing newline the reload contract needs
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "parse_target_qname.py"
_spec = importlib.util.spec_from_file_location("develop_parse_target_qname", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

split_qname = _mod.split_qname
main = _mod.main

_SENTINELS = ("codemap-query-kind", "target-module", "target-fn", "target-qualified")


@pytest.fixture()
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point sentinel writes at an isolated directory under a fixed session token."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", "testsess")
    return tmp_path


def _read(tmp_path: Path, name: str) -> str:
    return (tmp_path / f"dev-fix-{name}-testsess").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("pkg.mod::do_thing", ("pkg.mod", "do_thing", "pkg.mod::do_thing")),
        ("--issue 42 pkg.mod::do_thing extra", ("pkg.mod", "do_thing", "pkg.mod::do_thing")),
        ("a.b::f then c.d::g", ("a.b", "f", "a.b::f")),
        ("no suspect here", ("", "", "")),
        ("", ("", "", "")),
        ("::orphan", ("", "", "")),
    ],
    ids=["plain", "leading-flag", "first-of-two", "absent", "empty", "malformed"],
)
def test_split_qname(arguments: str, expected: tuple[str, str, str]) -> None:
    assert split_qname(arguments) == expected


def test_sentinels_written_with_trailing_newline(session: Path) -> None:
    assert main(["--query-kind", "callers", "--", "pkg.mod::do_thing"]) == 0
    assert _read(session, "codemap-query-kind") == "callers\n"
    assert _read(session, "target-module") == "pkg.mod\n"
    assert _read(session, "target-fn") == "do_thing\n"
    assert _read(session, "target-qualified") == "pkg.mod::do_thing\n"


def test_empty_query_kind_falls_back_to_standard(session: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--query-kind", "", "--", "no suspect"]) == 0
    out = capsys.readouterr().out
    assert "! CODEMAP_QUERY_KIND unresolved — using standard structural context" in out
    assert _read(session, "codemap-query-kind") == "standard\n"


def test_leading_dash_arguments_do_not_break_parsing(session: Path) -> None:
    """``$ARGUMENTS`` routinely starts with a flag; the ``--`` separator must absorb it."""
    assert main(["--", "--issue 42 --semble"]) == 0
    assert _read(session, "target-module") == "\n"


def test_all_four_sentinels_exist(session: Path) -> None:
    main(["--query-kind", "skip", "--", "x.y::z"])
    for name in _SENTINELS:
        assert (session / f"dev-fix-{name}-testsess").is_file()


def test_session_token_falls_back_to_claude_session_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "from-claude")
    assert main(["--", "a.b::c"]) == 0
    assert (tmp_path / "dev-fix-target-qualified-from-claude").read_text(encoding="utf-8") == "a.b::c\n"


def test_unwritable_sentinel_dir_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "does" / "not" / "exist"))
    monkeypatch.setenv("CSID", "testsess")
    assert main(["--", "a.b::c"]) == 1
