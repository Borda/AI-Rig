"""Tests for ``bin/heal_git_artifacts.py`` — stale lock and worktree reclamation.

Covers:
* PID liveness on **both** platform branches, exercised on every host (the
  project's recurrent cross-OS defect guard: the Windows branch is simulated
  with a monkeypatched ``ctypes.windll`` rather than skipped)
* Lock classification: dead holder, live holder, age override, malformed
* Worktree tiering: protected / live / dirty / removable / orphan
* Filesystem sweeps and CLI exit-code contract
* Windows-portability invariants of the source itself
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

_MODULE_NAME = "foundry_heal_git_artifacts"
_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "heal_git_artifacts.py"
_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
# Register before exec: @dataclass resolves cls.__module__ through sys.modules
# while processing the class body, and a missing entry raises AttributeError.
sys.modules[_MODULE_NAME] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

LockVerdict = _mod.LockVerdict
WorktreeTier = _mod.WorktreeTier


class _FakeKernel32:
    """Minimal Win32 stand-in exposing only what the Windows branch calls."""

    def __init__(self, *, handle: int, last_error: int = 0) -> None:
        """Store the configured Win32 handle and last-error result."""
        self._handle = handle
        self._last_error = last_error
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:  # noqa: N802 - Win32 name
        """Return the configured fake process handle."""
        return self._handle

    def CloseHandle(self, handle: int) -> bool:  # noqa: N802 - Win32 name
        """Record closure of ``handle`` and report Win32 success."""
        self.closed.append(handle)
        return True

    def GetLastError(self) -> int:  # noqa: N802 - Win32 name
        """Return the configured Win32 last-error value."""
        return self._last_error


class _FakeWindll:
    """Minimal ``ctypes.windll`` stand-in exposing the supplied ``kernel32`` fake."""

    def __init__(self, kernel32: _FakeKernel32) -> None:
        """Expose ``kernel32`` through the fake DLL namespace."""
        self.kernel32 = kernel32


class TestPidLivenessPosix:
    def test_probes_with_signal_zero(self, monkeypatch):
        """A live process answers True, and the probe is signal 0 — never a real signal.

        ``os.kill`` is patched rather than invoked for real because signal 0 is not
        inert everywhere: on Windows it is ``CTRL_C_EVENT``, so a genuine call would
        deliver Ctrl+C to the console process group and abort the whole pytest run.
        Patching keeps this assertion running on every host instead of skipping it
        on the one platform where the mistake actually bites.
        """
        calls = []

        monkeypatch.setattr(_mod.os, "kill", lambda pid, sig: calls.append((pid, sig)))
        assert _mod._pid_alive_posix(4242) is True
        assert calls == [(4242, 0)]

    def test_permission_error_counts_as_alive(self, monkeypatch):
        """Another user's process exists — EPERM answers the question yes."""

        def _raise_perm(pid, sig):
            """Raise the permission error representing another user's live process."""
            raise PermissionError

        monkeypatch.setattr(_mod.os, "kill", _raise_perm)
        assert _mod._pid_alive_posix(12345) is True

    def test_missing_process_is_dead(self, monkeypatch):
        def _raise_lookup(pid, sig):
            """Raise the missing-process error for this liveness case."""
            raise ProcessLookupError

        monkeypatch.setattr(_mod.os, "kill", _raise_lookup)
        assert _mod._pid_alive_posix(12345) is False

    def test_oserror_is_dead(self, monkeypatch):
        def _raise_os(pid, sig):
            """Raise the generic OS error treated as a dead process."""
            raise OSError

        monkeypatch.setattr(_mod.os, "kill", _raise_os)
        assert _mod._pid_alive_posix(12345) is False


class TestSimulatedWindowsPidLiveness:
    """Runs on every host — the Win32 surface is supplied, never skipped."""

    def test_open_process_success_is_alive(self, monkeypatch):
        kernel = _FakeKernel32(handle=777)
        monkeypatch.setattr(ctypes, "windll", _FakeWindll(kernel), raising=False)
        assert _mod._pid_alive_windows(4242) is True
        assert kernel.closed == [777], "handle must be closed to avoid a leak"

    def test_access_denied_is_alive(self, monkeypatch):
        kernel = _FakeKernel32(handle=0, last_error=_mod._WIN_ERROR_ACCESS_DENIED)
        monkeypatch.setattr(ctypes, "windll", _FakeWindll(kernel), raising=False)
        assert _mod._pid_alive_windows(4242) is True

    def test_other_error_is_dead(self, monkeypatch):
        kernel = _FakeKernel32(handle=0, last_error=87)  # ERROR_INVALID_PARAMETER
        monkeypatch.setattr(ctypes, "windll", _FakeWindll(kernel), raising=False)
        assert _mod._pid_alive_windows(4242) is False

    def test_absent_windll_degrades_to_false(self, monkeypatch):
        """Regression: prove absence is handled, not merely untested.

        ``ctypes.windll`` does not exist on POSIX hosts; deleting it makes the assertion meaningful on Windows too,
        where it does.
        """
        monkeypatch.delattr(ctypes, "windll", raising=False)
        assert _mod._pid_alive_windows(4242) is False

    def test_dispatcher_routes_to_simulated_windows_branch(self, monkeypatch):
        kernel = _FakeKernel32(handle=99)
        monkeypatch.setattr(ctypes, "windll", _FakeWindll(kernel), raising=False)
        monkeypatch.setattr(_mod.os, "name", "nt")
        assert _mod.pid_alive(4242) is True

    def test_simulated_windows_branch_never_uses_os_kill(self, monkeypatch):
        """Avoid probing a live Windows process through a disruptive signal."""

        def _forbidden(*_args, **_kwargs):
            """Fail if the simulated Windows branch probes through ``os.kill``."""
            raise AssertionError("os.kill must not be called on the Windows branch")

        kernel = _FakeKernel32(handle=5)
        monkeypatch.setattr(ctypes, "windll", _FakeWindll(kernel), raising=False)
        monkeypatch.setattr(_mod.os, "kill", _forbidden)
        monkeypatch.setattr(_mod.os, "name", "nt")
        assert _mod.pid_alive(4242) is True


class TestPidAliveGuards:
    @pytest.mark.parametrize("pid", [pytest.param(None, id="none"), 0, -1])
    def test_unverifiable_pid_is_not_alive(self, pid):
        assert _mod.pid_alive(pid) is False


class TestParseLockPid:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("33410 2026-08-17T18:10:04Z\n", 33410, id="pid-and-stamp"),
            pytest.param("33410\n", 33410, id="pid-only"),
            pytest.param("33410 x\nsecond line\n", 33410, id="multiline"),
            pytest.param("", None, id="empty"),
            pytest.param("   ", None, id="blank"),
            pytest.param("nope 2026-08-17T18:10:04Z", None, id="non-numeric"),
            pytest.param("-1 x", None, id="negative"),
            pytest.param("0 x", None, id="zero"),
        ],
    )
    def test_parse(self, text, expected):
        assert _mod.parse_lock_pid(text) == expected


class TestClassifyLock:
    def test_dead_holder_reclaims_immediately(self):
        """The fast path age alone cannot provide."""
        assert _mod.classify_lock(42, alive=False, age_minutes=0.1, max_age_minutes=30) is LockVerdict.STALE_DEAD

    def test_live_holder_holds(self):
        assert _mod.classify_lock(42, alive=True, age_minutes=5, max_age_minutes=30) is LockVerdict.HELD

    def test_age_overrides_live_holder(self):
        """Secondary guard for PID reuse and locks copied between machines."""
        assert _mod.classify_lock(42, alive=True, age_minutes=31, max_age_minutes=30) is LockVerdict.STALE_AGED

    def test_age_boundary_is_inclusive(self):
        assert _mod.classify_lock(42, alive=True, age_minutes=30, max_age_minutes=30) is LockVerdict.STALE_AGED

    def test_fresh_unparseable_is_held_not_stolen(self):
        assert _mod.classify_lock(None, alive=False, age_minutes=1, max_age_minutes=30) is LockVerdict.MALFORMED

    def test_aged_unparseable_is_reclaimable(self):
        assert _mod.classify_lock(None, alive=False, age_minutes=99, max_age_minutes=30) is LockVerdict.STALE_AGED

    @pytest.mark.parametrize("verdict", [LockVerdict.STALE_DEAD, LockVerdict.STALE_AGED])
    def test_reclaimable_set(self, verdict):
        assert verdict in _mod.RECLAIMABLE_LOCKS

    @pytest.mark.parametrize("verdict", [LockVerdict.HELD, LockVerdict.MALFORMED])
    def test_non_reclaimable_set(self, verdict):
        assert verdict not in _mod.RECLAIMABLE_LOCKS


class TestClassifyWorktree:
    BASE = dict(
        is_main=False,
        registered=True,
        dirty_files=0,
        age_days=99.0,
        min_age_days=14.0,
        managed_prefixes=("agent-", "oss-review-"),
    )

    def _tier(self, name: str, **over):
        """Classify a worktree using this test's baseline fields and overrides."""
        return _mod.classify_worktree(name, **{**self.BASE, **over})

    def test_main_tree_never_touched(self):
        assert self._tier("Borda.local", is_main=True) is WorktreeTier.MAIN

    def test_unmanaged_name_protected(self):
        """A worktree a human made by hand must never be auto-removed."""
        assert self._tier("my-experiment") is WorktreeTier.PROTECTED

    def test_dev_prefix_protected(self):
        """worktree-isolation.md contracts dev-* as a user-reviewed deliverable."""
        assert self._tier("dev-review-auth") is WorktreeTier.PROTECTED

    def test_recent_activity_outranks_everything(self):
        assert self._tier("agent-abc", age_days=1) is WorktreeTier.LIVE

    def test_recent_activity_protects_even_when_clean_and_registered(self):
        assert self._tier("agent-abc", age_days=13.9) is WorktreeTier.LIVE

    def test_dirty_reported_never_removed_at_any_age(self):
        assert self._tier("agent-abc", dirty_files=17, age_days=9999) is WorktreeTier.DIRTY

    def test_unregistered_clean_aged_is_orphan(self):
        assert self._tier("agent-abc", registered=False) is WorktreeTier.ORPHAN

    def test_registered_clean_aged_is_removable(self):
        assert self._tier("agent-abc") is WorktreeTier.REMOVABLE

    def test_oss_review_prefix_managed(self):
        assert self._tier("oss-review-1301") is WorktreeTier.REMOVABLE

    @pytest.mark.parametrize("tier", [WorktreeTier.DIRTY, WorktreeTier.PROTECTED, WorktreeTier.LIVE, WorktreeTier.MAIN])
    def test_protected_tiers_not_reclaimable(self, tier):
        assert tier not in _mod.RECLAIMABLE_WORKTREES


class TestSweepLocks:
    def _write(self, tmp_path: Path, name: str, body: str, age_minutes: float = 0.0) -> Path:
        """Write one lock fixture, optionally backdating its modification time."""
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        if age_minutes:
            past = time.time() - age_minutes * 60
            os.utime(path, (past, past))
        return path

    def test_live_holder_is_held(self, tmp_path):
        self._write(tmp_path, "oss-resolve-main.lock", f"{os.getpid()} 2026-08-17T18:10:04Z\n")
        states = _mod.sweep_locks(tmp_path, "oss-resolve-*.lock", 30, time.time())
        assert [s.verdict for s in states] == [LockVerdict.HELD]

    def test_dead_holder_is_stale(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "pid_alive", lambda pid: False)
        self._write(tmp_path, "oss-resolve-main.lock", "424242 2026-08-17T18:10:04Z\n")
        states = _mod.sweep_locks(tmp_path, "oss-resolve-*.lock", 30, time.time())
        assert states[0].verdict is LockVerdict.STALE_DEAD
        assert states[0].pid == 424242

    def test_glob_is_not_branch_scoped(self, tmp_path, monkeypatch):
        """The 27-day leak survived because only the current branch was checked."""
        monkeypatch.setattr(_mod, "pid_alive", lambda pid: False)
        self._write(tmp_path, "oss-resolve-main.lock", "1 x")
        self._write(tmp_path, "oss-resolve-feat-abandoned.lock", "2 x")
        self._write(tmp_path, "unrelated.lock", "3 x")
        states = _mod.sweep_locks(tmp_path, "oss-resolve-*.lock", 30, time.time())
        assert {s.path.name for s in states} == {"oss-resolve-main.lock", "oss-resolve-feat-abandoned.lock"}

    def test_aged_live_holder_reclaimed(self, tmp_path):
        self._write(tmp_path, "oss-resolve-main.lock", f"{os.getpid()} x", age_minutes=60)
        states = _mod.sweep_locks(tmp_path, "oss-resolve-*.lock", 30, time.time())
        assert states[0].verdict is LockVerdict.STALE_AGED

    def test_no_matches_is_empty(self, tmp_path):
        assert _mod.sweep_locks(tmp_path, "oss-resolve-*.lock", 30, time.time()) == []

    def test_directory_matching_glob_ignored(self, tmp_path):
        (tmp_path / "oss-resolve-dir.lock").mkdir()
        assert _mod.sweep_locks(tmp_path, "oss-resolve-*.lock", 30, time.time()) == []


class TestSweepWorktrees:
    def test_orphan_directory_detected(self, tmp_path, monkeypatch):
        """Git worktree prune cannot see this case — it removes the inverse."""
        root = tmp_path / ".claude" / "worktrees"
        (root / "agent-orphan").mkdir(parents=True)
        past = time.time() - 60 * 86400
        os.utime(root / "agent-orphan", (past, past))
        monkeypatch.setattr(
            _mod, "_git", lambda args, cwd=None: f"worktree {tmp_path}" if args[0] == "worktree" else ""
        )
        states = _mod.sweep_worktrees(tmp_path, root, 14, ("agent-",), time.time())
        assert [(s.path.name, s.tier) for s in states] == [("agent-orphan", WorktreeTier.ORPHAN)]

    def test_missing_root_is_empty(self, tmp_path):
        assert _mod.sweep_worktrees(tmp_path, tmp_path / "nope", 14, ("agent-",), time.time()) == []

    def test_dirty_registered_worktree_reported_not_removable(self, tmp_path, monkeypatch):
        root = tmp_path / ".claude" / "worktrees"
        wt = root / "agent-dirty"
        wt.mkdir(parents=True)
        past = time.time() - 60 * 86400
        os.utime(wt, (past, past))

        def _fake_git(args, cwd=None):
            """Return registered worktree and dirty-status fixture output."""
            if args[0] == "worktree":
                return f"worktree {tmp_path}\nworktree {wt.resolve()}"
            if args[0] == "status":
                return " M a.py\n M b.py"
            return ""

        monkeypatch.setattr(_mod, "_git", _fake_git)
        states = _mod.sweep_worktrees(tmp_path, root, 14, ("agent-",), time.time())
        assert states[0].tier is WorktreeTier.DIRTY
        assert states[0].dirty_files == 2


class TestCli:
    def _run(self, argv, monkeypatch, tmp_path, capsys):
        """Run the CLI with repository discovery stubbed to ``tmp_path``."""
        monkeypatch.setattr(_mod, "_git", lambda args, cwd=None: str(tmp_path) if args[0] == "rev-parse" else "")
        rc = _mod.main(argv)
        return rc, capsys.readouterr().out

    def test_reclaimable_without_apply_exits_1(self, tmp_path, monkeypatch, capsys):
        """Report-only must be distinguishable from clean by exit code alone."""
        (tmp_path / "oss-resolve-x.lock").write_text("424242 x", encoding="utf-8")
        monkeypatch.setattr(_mod, "pid_alive", lambda pid: False)
        rc, out = self._run(["locks", "--pattern", "oss-resolve-*.lock"], monkeypatch, tmp_path, capsys)
        assert rc == 1
        assert "reclaimable" in out
        assert (tmp_path / "oss-resolve-x.lock").exists(), "report-only must not delete"

    def test_apply_removes_and_exits_0(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "oss-resolve-x.lock").write_text("424242 x", encoding="utf-8")
        monkeypatch.setattr(_mod, "pid_alive", lambda pid: False)
        rc, out = self._run(["locks", "--pattern", "oss-resolve-*.lock", "--apply"], monkeypatch, tmp_path, capsys)
        assert rc == 0
        assert "reclaimed" in out
        assert not (tmp_path / "oss-resolve-x.lock").exists()

    def test_held_lock_exits_0_and_survives(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "oss-resolve-x.lock").write_text(f"{os.getpid()} x", encoding="utf-8")
        rc, out = self._run(["locks", "--pattern", "oss-resolve-*.lock", "--apply"], monkeypatch, tmp_path, capsys)
        assert rc == 0
        assert "held" in out
        assert (tmp_path / "oss-resolve-x.lock").exists()

    def test_quiet_suppresses_output_not_exit_code(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "oss-resolve-x.lock").write_text("424242 x", encoding="utf-8")
        monkeypatch.setattr(_mod, "pid_alive", lambda pid: False)
        rc, out = self._run(["locks", "--pattern", "oss-resolve-*.lock", "--quiet"], monkeypatch, tmp_path, capsys)
        assert rc == 1
        assert out == ""

    def test_wildcard_prefix_manages_every_child(self, tmp_path, monkeypatch, capsys):
        """Skill-private roots (fortify variants) have arbitrary child names."""
        root = tmp_path / "variants"
        (root / "lr-1e-4").mkdir(parents=True)
        past = time.time() - 5 * 86400
        os.utime(root / "lr-1e-4", (past, past))
        monkeypatch.setattr(_mod, "_git", lambda args, cwd=None: str(tmp_path) if args[0] == "rev-parse" else "")
        rc = _mod.main(["worktrees", "--root", str(root), "--managed-prefix", "*", "--min-age-days", "1"])
        assert rc == 1
        assert "removable" in capsys.readouterr().out

    def test_unmanaged_name_untouched_without_wildcard(self, tmp_path, monkeypatch, capsys):
        root = tmp_path / "variants"
        (root / "lr-1e-4").mkdir(parents=True)
        past = time.time() - 5 * 86400
        os.utime(root / "lr-1e-4", (past, past))
        monkeypatch.setattr(_mod, "_git", lambda args, cwd=None: str(tmp_path) if args[0] == "rev-parse" else "")
        rc = _mod.main(["worktrees", "--root", str(root), "--min-age-days", "1"])
        assert rc == 0
        assert "protected" in capsys.readouterr().out

    def test_outside_git_repo_exits_2(self, monkeypatch, capsys):
        monkeypatch.setattr(_mod, "_git", lambda args, cwd=None: "")
        assert _mod.main(["locks", "--pattern", "*.lock"]) == 2

    def test_mode_is_required(self):
        with pytest.raises(SystemExit):
            _mod.main([])

    def test_pattern_is_required(self):
        with pytest.raises(SystemExit):
            _mod.main(["locks"])
