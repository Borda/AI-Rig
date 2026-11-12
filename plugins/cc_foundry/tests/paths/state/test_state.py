"""Tests for ``bin/state.py`` — cross-Bash-call shell value persistence.

Every test runs against a private ``TMPDIR`` with the session environment stripped, so state files never collide with
another session and the session token resolves to ``shared`` unless a test sets it.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
from pathlib import Path

import pytest
from _hook_env import _symlinks_supported

_CAN_SYMLINK = _symlinks_supported()


def _private_modes_supported() -> bool:
    """Probe whether this filesystem enforces the private POSIX modes used by state files."""
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "private"
        path.write_text("", encoding="utf-8")
        try:
            path.chmod(0o600)
            return stat.S_IMODE(path.stat().st_mode) == 0o600
        except OSError:
            return False


_CAN_PRIVATE_MODES = _private_modes_supported()

_MOD_PATH = Path(__file__).resolve().parents[3] / "bin" / "state.py"
_spec = importlib.util.spec_from_file_location("state", _MOD_PATH)
assert _spec
assert _spec.loader
st = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(st)


_SHIPPED_KEYS = st._KEY_ALLOWED_NAMES
_FIXTURE_KEYS = frozenset(
    {"A", "B", "C", "EMPTY", "ENVIRONMENT", "GOOD", "K", "LDAP_URL", "MSG", "MYPATH", "N", "PATHS", "URL"}
)


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect state files into a temp dir; strip session env so CSID resolves to 'shared'.

    The restorable-key allowlist is widened with this module's fixture names only; the shipped list stays narrow.
    """
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("CSID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(st, "_KEY_ALLOWED_NAMES", _SHIPPED_KEYS | _FIXTURE_KEYS)


def test_shipped_allowlist_names_only_workflow_values() -> None:
    """The restorable-key allowlist shipped with the helper holds only the documented workflow values.

    A test fixture name leaking into the shipped list would widen what ``load`` may emit for ``eval``.
    """
    assert _SHIPPED_KEYS == {"RUN_DIR", "SCOPE"}


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


@pytest.mark.parametrize("key", ["HOME", "CLAUDE_PLUGIN_ROOT", "GIT_SSH_COMMAND", "UNDECLARED_SETTING"])
def test_rejects_environment_keys_outside_supported_names(key: str) -> None:
    """Environment configuration cannot be persisted for a later eval."""
    assert st.set_values("ns", [f"{key}=untrusted"]) == 2
    assert not st.state_path("ns").exists()


@pytest.mark.parametrize("key", ["HOME", "CLAUDE_PLUGIN_ROOT", "GIT_SSH_COMMAND", "UNDECLARED_SETTING"])
def test_load_omits_environment_keys_from_tampered_file(key: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Only supported workflow state names reach emitted shell assignments."""
    st.state_path("ns").write_text(f"{key}=untrusted\nGOOD=2\n", encoding="utf-8")
    assert st.load_values("ns") == 0
    captured = capsys.readouterr()
    assert captured.out == "GOOD='2'\n"
    assert key in captured.err


def test_state_parent_is_private_per_user_directory(tmp_path: Path) -> None:
    """State files no longer share the world-writable temporary directory itself."""
    assert st.set_values("ns", ["GOOD=2"]) == 0
    path = st.state_path("ns")
    assert path.parent.parent == tmp_path
    assert path.parent.name.startswith("claude-shell-state-")


@pytest.mark.skipif(not _CAN_PRIVATE_MODES, reason="this filesystem does not enforce private POSIX file modes")
def test_state_directory_and_file_are_private() -> None:
    """Helper-created directories and files expose no group or other access."""
    assert st.set_values("ns", ["GOOD=2"]) == 0
    path = st.state_path("ns")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")
@pytest.mark.parametrize("without_nofollow", [False, True])
def test_set_refuses_symlink_without_touching_victim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, without_nofollow: bool
) -> None:
    """A planted state-file link cannot redirect writes, including without O_NOFOLLOW."""
    monkeypatch.setattr(st.os, "O_NOFOLLOW", 0 if without_nofollow else getattr(os, "O_NOFOLLOW", 0), raising=False)
    victim = tmp_path / "victim"
    victim.write_text("GOOD=private\n", encoding="utf-8")
    st.state_path("ns").symlink_to(victim)
    assert st.set_values("ns", ["GOOD=changed"]) == 2
    assert victim.read_text(encoding="utf-8") == "GOOD=private\n"
    assert st.state_path("ns").is_symlink()


@pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")
def test_load_refuses_symlink(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A planted state-file link cannot import arbitrary supported assignments."""
    victim = tmp_path / "victim"
    victim.write_text("GOOD=private\n", encoding="utf-8")
    st.state_path("ns").symlink_to(victim)
    assert st.load_values("ns") == 2
    assert capsys.readouterr().out == ""


def test_session_id_cannot_create_path_components(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment session token cannot escape the state directory."""
    monkeypatch.setenv("CSID", "../../outside/session")
    assert st.state_path("ns").name == "claude-state-ns-.._.._outside_session.env"


@pytest.mark.skipif(not _CAN_PRIVATE_MODES, reason="this filesystem does not enforce private POSIX file modes")
def test_existing_file_is_restricted_on_update() -> None:
    """Updating a legacy readable state file restores private file permissions."""
    path = st.state_path("ns")
    path.write_text("A=old\n", encoding="utf-8")
    path.chmod(0o644)
    assert st.set_values("ns", ["B=new"]) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert st._read(path) == {"A": "old", "B": "new"}


@pytest.mark.skipif(not _CAN_SYMLINK, reason="this process may not create symlinks")
def test_planted_private_directory_link_is_refused(tmp_path: Path) -> None:
    """A pre-existing link cannot redirect the private state directory outside its root."""
    directory = st.state_path("ns").parent
    directory.rmdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    directory.symlink_to(victim, target_is_directory=True)
    assert st.set_values("ns", ["GOOD=private"]) == 2
    assert list(victim.iterdir()) == []


@pytest.mark.skipif(not _CAN_PRIVATE_MODES, reason="this filesystem does not enforce private POSIX file modes")
def test_nonprivate_existing_directory_is_refused() -> None:
    """An unsafe directory is rejected rather than reused for private values."""
    directory = st.state_path("ns").parent
    directory.chmod(0o755)
    assert st.set_values("ns", ["GOOD=private"]) == 2
    assert list(directory.iterdir()) == []


def test_round_trip_without_posix_file_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native-platform fallbacks remain usable when POSIX identity and descriptor modes are absent."""
    monkeypatch.delattr(st.os, "getuid", raising=False)
    monkeypatch.delattr(st.os, "fchmod", raising=False)
    monkeypatch.delattr(st.os, "O_NOFOLLOW", raising=False)
    assert st.set_values("ns", ["GOOD=private"]) == 0
    assert st._read(st.state_path("ns")) == {"GOOD": "private"}
