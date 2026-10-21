"""Tests for ``bin/state.py`` — cross-Bash-call shell value persistence."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parent.parent.parent / "bin" / "state.py"
_spec = importlib.util.spec_from_file_location("state", _MOD_PATH)
assert _spec
assert _spec.loader
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect state files into a temp dir; strip session env so CSID resolves to 'shared'."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def test_set_then_load_round_trip() -> None:
    """Values written by set() are recoverable verbatim by load()."""
    assert st.set_values("ns1", ["RUN_DIR=.reports/x", "N=5"]) == 0
    values = st._read(st.state_path("ns1"))
    assert values == {"RUN_DIR": ".reports/x", "N": "5"}


def test_set_merges_and_updates() -> None:
    """A second set() merges new keys and overwrites existing ones."""
    st.set_values("ns2", ["A=1", "B=2"])
    st.set_values("ns2", ["B=9", "C=3"])
    assert st._read(st.state_path("ns2")) == {"A": "1", "B": "9", "C": "3"}


def test_value_with_equals_and_empty() -> None:
    """A value containing '=' is preserved; an empty value is allowed."""
    st.set_values("ns3", ["URL=a=b=c", "EMPTY="])
    values = st._read(st.state_path("ns3"))
    assert values["URL"] == "a=b=c"
    assert values["EMPTY"] == ""


def test_load_quotes_single_quotes(capsys: pytest.CaptureFixture[str]) -> None:
    """Emit shell-safe single-quote-escaped lines."""
    st.set_values("ns4", ["MSG=a b'c"])
    st.load_values("ns4")
    out = capsys.readouterr().out
    assert out == "MSG='a b'\\''c'\n"


def test_malformed_assignment_returns_2() -> None:
    """An assignment without '=' is a usage error (exit 2)."""
    assert st.set_values("ns5", ["NOEQUALS"]) == 2


@pytest.mark.parametrize("assignment", ["X; malicious=1", "1LEADING_DIGIT=x", "has-dash=x", "$(id)=x"])
def test_set_rejects_unsafe_key(assignment: str) -> None:
    """A KEY outside the shell-identifier allowlist is a usage error (exit 2)."""
    assert st.set_values("ns9", [assignment]) == 2


def test_documented_caller_keys_accepted() -> None:
    """Every KEY used by this module's docs, bin-authoring-guide.md, and these tests stays accepted."""
    assignments = ["RUN_DIR=a", "SCOPE=b", "K=c", "N=1", "A=1", "B=2", "C=3", "URL=u", "EMPTY=", "MSG=m"]
    assert st.set_values("ns11", assignments) == 0
    assert len(st._read(st.state_path("ns11"))) == 10


def test_load_skips_unsafe_key_from_state_file(capsys: pytest.CaptureFixture[str]) -> None:
    """A key that never passed set() (legacy or concurrently written file) is skipped, not emitted for eval."""
    st.state_path("ns10").write_text("X; malicious=1\nGOOD=2\n", encoding="utf-8")
    assert st.load_values("ns10") == 0
    captured = capsys.readouterr()
    assert captured.out == "GOOD='2'\n"
    assert "malicious" in captured.err


@pytest.mark.parametrize(
    "assignment",
    [
        pytest.param("A=x\nPATH=/evil", id="newline"),
        pytest.param("A=x\rPATH=/evil", id="carriage-return"),
        pytest.param("A=x\x00PATH=/evil", id="nul"),
        pytest.param("A=x\x85PATH=/evil", id="next-line-char"),
        pytest.param("A=x\N{LINE SEPARATOR}PATH=/evil", id="unicode-line-separator"),
    ],
)
def test_set_rejects_value_with_line_break(assignment: str) -> None:
    """A value that the line-oriented file would re-read as a second KEY=VALUE line is refused.

    Without this, ``set ns "A=x<newline>PATH=/evil"`` stored a PATH line that ``load`` then emitted for the caller's
    ``eval``; nothing may be written when the assignment is refused.
    """
    assert st.set_values("ns12", [assignment]) == 2
    assert not st.state_path("ns12").exists()


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("PATH", id="path"),
        pytest.param("IFS", id="ifs"),
        pytest.param("BASH_ENV", id="bash-env"),
        pytest.param("LD_PRELOAD", id="ld-preload"),
        pytest.param("DYLD_INSERT_LIBRARIES", id="dyld-insert-libraries"),
    ],
)
def test_set_rejects_reserved_key(key: str) -> None:
    """Shell and loader variables cannot be stored, since ``eval`` of ``PATH='…'`` is code execution."""
    assert st.set_values("ns13", [f"{key}=x"]) == 2
    assert not st.state_path("ns13").exists()


def test_reserved_key_lookalikes_accepted() -> None:
    """Names that merely resemble a reserved variable stay usable, so the denylist does not over-block."""
    assert st.set_values("ns14", ["PATHS=a", "MYPATH=b", "LDAP_URL=c", "ENVIRONMENT=d"]) == 0
    assert set(st._read(st.state_path("ns14"))) == {"PATHS", "MYPATH", "LDAP_URL", "ENVIRONMENT"}


def test_load_skips_reserved_key_from_state_file(capsys: pytest.CaptureFixture[str]) -> None:
    """A reserved key that reached the file some other way (legacy file, hand edit) is skipped, not emitted."""
    st.state_path("ns15").write_text("PATH=/evil\nGOOD=2\nLD_PRELOAD=/x.so\n", encoding="utf-8")
    assert st.load_values("ns15") == 0
    captured = capsys.readouterr()
    assert captured.out == "GOOD='2'\n"
    assert "PATH" in captured.err
    assert "LD_PRELOAD" in captured.err


def test_clear_removes_file() -> None:
    """Remove persisted state completely when clearing it."""
    st.set_values("ns6", ["A=1"])
    assert st.state_path("ns6").is_file()
    assert st.clear("ns6") == 0
    assert not st.state_path("ns6").is_file()


def test_load_absent_namespace_is_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Loading a never-set namespace prints nothing and succeeds."""
    assert st.load_values("never") == 0
    assert capsys.readouterr().out == ""


def test_namespace_sanitized() -> None:
    """Unsafe namespace chars are sanitized into the filename."""
    assert st.state_path("a/b:c").name == "claude-state-a_b_c-shared.env"


def test_session_scoping_distinct_csids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two different CSID values yield distinct state paths for the same namespace."""
    monkeypatch.setenv("CSID", "sess-a")
    path_a = st.state_path("ns7")
    monkeypatch.setenv("CSID", "sess-b")
    path_b = st.state_path("ns7")
    assert path_a != path_b
    assert path_a.name == "claude-state-ns7-sess-a.env"
    assert path_b.name == "claude-state-ns7-sess-b.env"


def test_session_scoping_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """CSID wins over CLAUDE_CODE_SESSION_ID; with neither set the token degrades to 'shared'."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-1")
    assert st.state_path("ns8").name == "claude-state-ns8-sid-1.env"
    monkeypatch.setenv("CSID", "csid-1")
    assert st.state_path("ns8").name == "claude-state-ns8-csid-1.env"
    monkeypatch.delenv("CSID")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert st.state_path("ns8").name == "claude-state-ns8-shared.env"
