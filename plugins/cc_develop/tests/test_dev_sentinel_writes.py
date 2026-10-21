"""Tests for the link-safe sentinel writes and subprocess timeouts of the develop ``bin/`` wrappers.

The wrappers persist values at predictable names in a shared temp dir, so a symlink pre-created at such a name must be
replaced instead of followed, a file another user planted there must read as unset, and a hung child process must end
the wrapper instead of stalling the skill.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import dev_codemap_gate
import dev_issue_fetch_wrap
import dev_run_dir
import dev_setup_worktree_wrap
import pytest

_CSID = "sentinel-write-test"


@pytest.fixture(name="tmp_env")
def _tmp_env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated ``$TMPDIR`` plus a fixed ``CSID`` so sentinel names are predictable."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", _CSID)
    return tmp_path


def _hung_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Stand in for ``subprocess.run`` on a child that outlives its ``timeout``."""
    raise subprocess.TimeoutExpired(cmd="child", timeout=float(kwargs["timeout"]))  # type: ignore[arg-type]


class TestWriteAtomic:
    """``write_atomic`` swaps the directory entry instead of writing through whatever is already there."""

    def test_writes_value_and_newline(self, tmp_path: Path) -> None:
        """The file holds exactly the value plus a bare newline, with no temp file left behind."""
        target = tmp_path / "value"

        dev_run_dir.write_atomic(target, "hello")

        assert target.read_bytes() == b"hello\n"
        assert [p.name for p in tmp_path.iterdir()] == ["value"]

    def test_replaces_a_planted_symlink_without_touching_its_target(self, tmp_path: Path) -> None:
        """A symlink at the predictable name is replaced, so the file it pointed at keeps its content.

        This is the attack a plain ``write_text`` loses: an attacker links the sentinel name to a file the user owns and
        the next sentinel write overwrites it.
        """
        victim = tmp_path / "victim"
        victim.write_text("precious\n", encoding="utf-8")
        target = tmp_path / "sentinel"
        target.symlink_to(victim)

        dev_run_dir.write_atomic(target, "payload")

        assert victim.read_text(encoding="utf-8") == "precious\n"
        assert not target.is_symlink()
        assert target.read_bytes() == b"payload\n"

    def test_missing_directory_raises_oserror(self, tmp_path: Path) -> None:
        """A destination directory that does not exist surfaces as ``OSError`` for the caller to report."""
        with pytest.raises(OSError, match="No such file"):
            dev_run_dir.write_atomic(tmp_path / "absent" / "value", "x")


class TestReadSentinelOwnership:
    """``read_sentinel`` trusts only a sentinel the current user owns."""

    def test_own_sentinel_returns_first_line(self, tmp_env: Path) -> None:
        """A sentinel this user wrote reads back as its first line, the value ``IFS= read -r`` would yield.

        Normal use: one block writes the value, a later Python helper reads it; nothing about ownership changes that.
        """
        dev_run_dir.write_sentinel("dev-owner-test", "value")

        assert dev_run_dir.read_sentinel("dev-owner-test") == "value"

    def test_foreign_owned_sentinel_reads_as_unset(self, tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A sentinel owned by another user reads as ``""``, the same as a missing one.

        On a shared ``/tmp`` another user can create the predictable name first; the atomic write then cannot replace
        it, so the read side must refuse the planted value instead of feeding it to a command. The foreign owner is
        simulated by reporting a different current uid; ``raising=False`` installs ``getuid`` on hosts without one.
        """
        sentinel = tmp_env / f"dev-owner-test-{_CSID}"
        sentinel.write_text("planted\n", encoding="utf-8")
        monkeypatch.setattr(dev_run_dir.os, "getuid", lambda: sentinel.stat().st_uid + 1, raising=False)

        assert dev_run_dir.read_sentinel("dev-owner-test") == ""

    def test_platform_without_uids_still_reads(self, tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Where the OS has no ``os.getuid`` (native Windows) the ownership check is skipped and the value reads back.

        The per-user temp dir there already keeps other users out; refusing every sentinel would break each skill.
        """
        dev_run_dir.write_sentinel("dev-owner-test", "value")
        monkeypatch.delattr(dev_run_dir.os, "getuid", raising=False)

        assert dev_run_dir.read_sentinel("dev-owner-test") == "value"


class TestWrapperSentinelsAreLinkSafe:
    """The issue and worktree wrappers persist through ``write_atomic`` rather than ``write_text``."""

    def test_issue_body_sentinel_replaces_planted_symlink(self, tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A symlink planted at the shared issue-body sentinel is replaced and its target left intact."""
        victim = tmp_env / "victim"
        victim.write_text("precious\n", encoding="utf-8")
        (tmp_env / f"dev-issue-body-{_CSID}").symlink_to(victim)
        monkeypatch.setattr(dev_issue_fetch_wrap, "_fetch", lambda *_a: ("body", 0))

        assert dev_issue_fetch_wrap.main(["wrap", "debug", "123"]) == 0

        assert victim.read_text(encoding="utf-8") == "precious\n"
        assert not (tmp_env / f"dev-issue-body-{_CSID}").is_symlink()


class TestSubprocessTimeouts:
    """A child that never returns ends the wrapper with a failure instead of hanging the skill."""

    def test_codemap_resolver_timeout_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A timed-out codemap resolver reports exit 124 so the gate blocks rather than assuming codemap is usable."""
        monkeypatch.setattr(dev_codemap_gate.subprocess, "run", _hung_run)

        assert dev_codemap_gate._run_codemap_resolve(Path("."), "auto") == ("", 124)

    def test_issue_fetch_timeout_returns_exit_124(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A stalled ``gh`` fetch yields an empty body and exit 124 so callers warn and proceed without context."""
        monkeypatch.setattr(dev_issue_fetch_wrap.subprocess, "run", _hung_run)

        assert dev_issue_fetch_wrap._fetch(Path("."), "123", "") == ("", 124)

    def test_worktree_setup_timeout_returns_no_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A hung worktree setup yields no timestamp, which the wrapper turns into its exit-1 guard."""
        monkeypatch.setattr(dev_setup_worktree_wrap.subprocess, "run", _hung_run)

        assert dev_setup_worktree_wrap._run_setup(Path(".")) == ""


class TestPlanNamespaceFence:
    """The plan skill's namespace sentinel becomes a write directory, so it must stay a plan dir under the temp dir."""

    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param("../elsewhere/dev-plan-1-x", id="outside-temp-dir"),
            pytest.param("not-a-plan-dir", id="wrong-prefix"),
        ],
    )
    def test_foreign_namespace_is_rejected(self, tmp_env: Path, relative: str) -> None:
        """A namespace outside the temp dir, or without the ``dev-plan-`` prefix, aborts with exit 1."""
        (tmp_env / f"dev-plan-ns-current-{_CSID}").write_text(f"{tmp_env / relative}\n", encoding="utf-8")

        with pytest.raises(SystemExit) as raised:
            dev_codemap_gate._resolve_paths("plan", tmp_env, _CSID)

        assert raised.value.code == 1

    def test_plan_namespace_under_temp_dir_is_accepted(self, tmp_env: Path) -> None:
        """A ``dev-plan-*`` directory directly under the temp dir resolves to its codemap files."""
        namespace = tmp_env / f"dev-plan-42-{_CSID}"
        (tmp_env / f"dev-plan-ns-current-{_CSID}").write_text(f"{namespace}\n", encoding="utf-8")

        assert dev_codemap_gate._resolve_paths("plan", tmp_env, _CSID) == (
            namespace / "codemap-raw",
            namespace / "codemap-enabled",
        )
