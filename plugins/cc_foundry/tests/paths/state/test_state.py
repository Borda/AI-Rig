"""Tests for ``bin/state.py`` — cross-Bash-call shell value persistence.

Every test runs against a private ``TMPDIR`` with the session environment stripped, so state files never collide with
another session and the session token resolves to ``shared`` unless a test sets it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[3] / "bin" / "state.py"
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


class TestSet:
    """``set_values``: validated KEY=VALUE persistence into a per-namespace state file."""

    def test_round_trips_values(self) -> None:
        """Values written by ``set_values`` are read back verbatim.

        A caller persists run directories and counters in one Bash call and reads them in the next, so the stored text
        must equal what was written.
        """
        assert st.set_values("ns", ["RUN_DIR=.reports/x", "N=5"]) == 0
        assert st._read(st.state_path("ns")) == {"RUN_DIR": ".reports/x", "N": "5"}

    def test_merges_and_overwrites(self) -> None:
        """A second call merges new keys and overwrites existing ones.

        Later phases add keys to the same namespace and update earlier ones, so a write must never drop other keys.
        """
        st.set_values("ns", ["A=1", "B=2"])
        st.set_values("ns", ["B=9", "C=3"])
        assert st._read(st.state_path("ns")) == {"A": "1", "B": "9", "C": "3"}

    def test_keeps_equals_signs_and_empty_values(self) -> None:
        """A value containing ``=`` is preserved whole and an empty value is allowed.

        URLs and query strings carry ``=``, so only the first ``=`` separates key from value.
        """
        st.set_values("ns", ["URL=a=b=c", "EMPTY="])
        values = st._read(st.state_path("ns"))
        assert (values["URL"], values["EMPTY"]) == ("a=b=c", "")

    def test_rejects_assignment_without_equals(self) -> None:
        """An assignment without ``=`` is a usage error (exit 2)."""
        assert st.set_values("ns", ["NOEQUALS"]) == 2

    @pytest.mark.parametrize("assignment", ["X; malicious=1", "1LEADING_DIGIT=x", "has-dash=x", "$(id)=x"])
    def test_rejects_unsafe_key(self, assignment: str) -> None:
        """A KEY outside the shell-identifier allowlist is a usage error (exit 2).

        Keys are later emitted for the caller's ``eval``, so anything that is not a plain identifier could run code.
        """
        assert st.set_values("ns", [assignment]) == 2

    def test_accepts_documented_caller_keys(self) -> None:
        """Every KEY used by this module's docs, bin-authoring-guide.md, and these tests stays accepted.

        Tightening the key allowlist must not lock out a key a skill already relies on.
        """
        assignments = ["RUN_DIR=a", "SCOPE=b", "K=c", "N=1", "A=1", "B=2", "C=3", "URL=u", "EMPTY=", "MSG=m"]
        assert st.set_values("ns", assignments) == 0
        assert len(st._read(st.state_path("ns"))) == 10

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
    def test_rejects_value_with_line_break(self, assignment: str) -> None:
        """A value that the line-oriented file would re-read as a second KEY=VALUE line is refused.

        Without this, ``set ns "A=x<newline>PATH=/evil"`` stored a PATH line that ``load`` then emitted for the caller's
        ``eval``; nothing may be written when the assignment is refused.
        """
        assert st.set_values("ns", [assignment]) == 2
        assert not st.state_path("ns").exists()

    @pytest.mark.parametrize("key", ["PATH", "IFS", "BASH_ENV", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"])
    def test_rejects_reserved_key(self, key: str) -> None:
        """Shell and loader variables cannot be stored, since ``eval`` of ``PATH='…'`` is code execution."""
        assert st.set_values("ns", [f"{key}=x"]) == 2
        assert not st.state_path("ns").exists()

    def test_accepts_reserved_key_lookalikes(self) -> None:
        """Names that merely resemble a reserved variable stay usable, so the denylist does not over-block."""
        assert st.set_values("ns", ["PATHS=a", "MYPATH=b", "LDAP_URL=c", "ENVIRONMENT=d"]) == 0
        assert set(st._read(st.state_path("ns"))) == {"PATHS", "MYPATH", "LDAP_URL", "ENVIRONMENT"}


class TestLoad:
    """``load_values``: emit stored pairs as shell-safe ``KEY='VALUE'`` lines."""

    def test_quotes_single_quotes(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A value holding a single quote is emitted as shell-safe single-quote-escaped text."""
        st.set_values("ns", ["MSG=a b'c"])
        st.load_values("ns")
        assert capsys.readouterr().out == "MSG='a b'\\''c'\n"

    def test_skips_unsafe_key_from_state_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A key that never passed ``set`` (legacy or concurrently written file) is skipped, not emitted for eval."""
        st.state_path("ns").write_text("X; malicious=1\nGOOD=2\n", encoding="utf-8")
        assert st.load_values("ns") == 0
        captured = capsys.readouterr()
        assert captured.out == "GOOD='2'\n"
        assert "malicious" in captured.err

    def test_skips_reserved_key_from_state_file(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A reserved key that reached the file some other way (legacy file, hand edit) is skipped, not emitted."""
        st.state_path("ns").write_text("PATH=/evil\nGOOD=2\nLD_PRELOAD=/x.so\n", encoding="utf-8")
        assert st.load_values("ns") == 0
        captured = capsys.readouterr()
        assert captured.out == "GOOD='2'\n"
        assert "PATH" in captured.err
        assert "LD_PRELOAD" in captured.err

    def test_absent_namespace_is_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Loading a never-set namespace prints nothing and succeeds."""
        assert st.load_values("never") == 0
        assert capsys.readouterr().out == ""


class TestClear:
    """``clear``: remove a namespace's persisted state."""

    def test_removes_file(self) -> None:
        """Clearing a namespace deletes its state file and reports success."""
        st.set_values("ns", ["A=1"])
        assert st.state_path("ns").is_file()
        assert st.clear("ns") == 0
        assert not st.state_path("ns").is_file()


class TestStatePath:
    """``state_path``: namespace sanitising and per-session scoping of the state filename."""

    def test_sanitizes_namespace(self) -> None:
        """Unsafe namespace characters are replaced in the filename, so a namespace cannot escape the temp dir."""
        assert st.state_path("a/b:c").name == "claude-state-a_b_c-shared.env"

    def test_distinct_csids_yield_distinct_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two different CSID values give distinct state paths for the same namespace.

        Concurrent sessions running the same skill must not read each other's state.
        """
        monkeypatch.setenv("CSID", "sess-a")
        path_a = st.state_path("ns")
        monkeypatch.setenv("CSID", "sess-b")
        path_b = st.state_path("ns")
        assert (path_a.name, path_b.name) == ("claude-state-ns-sess-a.env", "claude-state-ns-sess-b.env")

    @pytest.mark.parametrize(
        ("csid", "session_id", "token"),
        [
            pytest.param("", "sid-1", "sid-1", id="session-id-without-csid"),
            pytest.param("csid-1", "sid-1", "csid-1", id="csid-wins"),
            pytest.param("", "", "shared", id="neither-degrades-to-shared"),
        ],
    )
    def test_session_token_fallback_chain(
        self, monkeypatch: pytest.MonkeyPatch, csid: str, session_id: str, token: str
    ) -> None:
        """CSID wins over CLAUDE_CODE_SESSION_ID; with neither set the token degrades to 'shared'.

        An empty value counts as unset, which is how the calling Bash block exports an unknown token.
        """
        monkeypatch.setenv("CSID", csid)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
        assert st.state_path("ns").name == f"claude-state-ns-{token}.env"
