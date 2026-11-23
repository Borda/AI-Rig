"""Exercise checkout-scoped review ownership with real Git worktrees and concurrent CLI processes."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace

import pytest
from _git_fixtures import git_text
from _modules import load_module
from _platform import DIRECTORY_SYMLINKS_AVAILABLE

HELPER = Path(__file__).resolve().parents[3] / "shared/review_checkout_slot.py"


def test_windows_peer_waits_through_recovery_measurement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verifier can hold the lock longer than the Windows CRT's ten attempts to acquire it."""
    scheduler = load_module("recovery_lock_wait", HELPER)
    attempts = []

    def try_lock(fd: int, mode: int, length: int) -> None:
        attempts.append((fd, mode, length))
        if len(attempts) == 1:
            raise OSError(errno.EDEADLK, "contended byte-range lock")

    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=try_lock, LK_LOCK=1))
        patch.setattr(os, "name", "nt")
        with scheduler._admission_lock(tmp_path):
            assert len(attempts) == 2


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a repository with a commit that can be checked out by a linked worktree."""
    git_text(tmp_path, "init", "-q")
    git_text(
        tmp_path,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture\n\nCo-authored-by: Codex <codex@openai.com>",
    )
    return tmp_path


def _invoke(*arguments: str | Path, code: int = 0) -> dict[str, str | None]:
    """Run the shipped CLI, retaining subprocess diagnostics on unexpected exit."""
    result = subprocess.run([sys.executable, str(HELPER), *map(str, arguments)], capture_output=True, check=False)
    assert result.returncode == code, (result.stdout, result.stderr)
    return json.loads(result.stdout)


@pytest.mark.integration
class TestCheckoutSlot:
    """Repository owners serialize across linked worktrees and release by their retained locator."""

    @pytest.mark.parametrize("held", [True, False])
    def test_proved_recovery_keeps_exclusive_owner(self, checkout: Path, held: bool) -> None:
        """A successful proof restores a held owner and preserves an already active exclusive owner."""
        scheduler = load_module("recovery_slot", HELPER)
        claim = _invoke("acquire", "--checkout", checkout, "--owner", "first")
        slot = Path(claim["slot"])
        state = _invoke("hold", "--slot", slot, "--owner", "first") if held else {**claim, "status": "busy"}
        with scheduler.recovery_claim(slot.parent, slot, "first"):
            assert _invoke("status", "--slot", slot) == state
        assert _invoke("acquire", "--slot", slot, "--owner", "peer", code=1) == {**claim, "status": "busy"}
        assert _invoke("release", "--slot", slot, "--owner", "first")["status"] == "released"

    def test_failed_proof_keeps_inspection_hold(self, checkout: Path) -> None:
        """An exception sealing the successor leaves the original hold intact."""
        scheduler = load_module("recovery_slot", HELPER)
        slot = Path(_invoke("acquire", "--checkout", checkout, "--owner", "first")["slot"])
        hold = _invoke("hold", "--slot", slot, "--owner", "first")
        with pytest.raises(OSError, match="could not seal"), scheduler.recovery_claim(slot.parent, slot, "first"):
            raise OSError("could not seal")
        assert _invoke("status", "--slot", slot) == hold

    @pytest.mark.parametrize("mismatch", ["owner", "common", "scope", "free"])
    def test_recovery_rejects_unbound_claim(self, checkout: Path, mismatch: str) -> None:
        """A proof can only use the current owner's exclusive claim in its authenticated common directory."""
        scheduler = load_module("recovery_slot", HELPER)
        common = (checkout / ".git").resolve()
        option = ["--local"] if mismatch == "scope" else []
        claim = _invoke("acquire", "--checkout", checkout, *option, "--owner", "first")
        slot = Path(claim["slot"])
        if mismatch == "free":
            _invoke("release", "--slot", slot, "--owner", "first")
        before = _invoke("status", "--slot", slot)
        with (
            pytest.raises(ValueError, match="recovery requires the original"),
            scheduler.recovery_claim(
                checkout if mismatch == "common" else common, slot, "peer" if mismatch == "owner" else "first"
            ),
        ):
            pytest.fail("unbound recovery entered the evidence-sealing section")
        assert _invoke("status", "--slot", slot) == before

    def test_linked_worktrees_share_ownership_and_independent_repositories_do_not(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A competing linked worktree waits while a separately initialized checkout acquires its own slot."""
        linked = checkout / "linked"
        git_text(checkout, "worktree", "add", "--detach", str(linked))
        independent = checkout / "independent"
        git_text(checkout, "init", "-q", str(independent))
        assert _invoke("status", "--checkout", checkout)["status"] == "free"
        claim = _invoke("acquire", "--checkout", checkout, "--owner", "first")
        slot = Path(claim["slot"])
        assert slot.is_absolute()
        assert claim == {"status": "acquired", "owner": "first", "slot": str(slot)}
        assert _invoke("acquire", "--checkout", linked, "--owner", "first") == claim
        assert _invoke("acquire", "--checkout", linked, "--owner", "second", code=1) == {**claim, "status": "busy"}
        assert _invoke("acquire", "--checkout", independent, "--owner", "second")["slot"] != str(slot)
        assert _invoke("release", "--slot", slot, "--owner", "second", code=1)["status"] == "not-owner"
        with monkeypatch.context() as patch:
            patch.setenv("PATH", "")
            assert _invoke("status", "--slot", slot) == {**claim, "status": "busy"}
            assert _invoke("release", "--slot", slot, "--owner", "first")["status"] == "released"
        assert _invoke("acquire", "--checkout", linked, "--owner", "second") == {**claim, "owner": "second"}
        assert _invoke("release", "--slot", slot, "--owner", "first", code=1)["status"] == "not-owner"

    def test_simultaneous_first_claims_admit_one_owner(self, checkout: Path) -> None:
        """Two processes racing to publish a fresh slot produce one consistent owner."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda owner: subprocess.run(
                        [sys.executable, str(HELPER), "acquire", "--checkout", str(checkout), "--owner", owner],
                        capture_output=True,
                        check=False,
                    ),
                    ("alpha", "beta"),
                )
            )
        assert sorted(result.returncode for result in results) == [0, 1]
        records = [json.loads(result.stdout) for result in results]
        assert {record["status"] for record in records} == {"acquired", "busy"}
        assert len({record["owner"] for record in records}) == 1
        assert _invoke("status", "--checkout", checkout)["owner"] == records[0]["owner"]

    def test_inspected_checkout_without_prior_claim_requires_free_slot(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Legacy recovery claims a free slot without Git and cannot adopt any preexisting owner marker."""
        common = Path(git_text(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        slot = common / "codex-review-slot"
        monkeypatch.setenv("PATH", "")
        claim = {"status": "acquired", "owner": "recovering", "slot": str(slot)}
        assert _invoke("acquire", "--slot", slot, "--owner", "recovering") == claim
        busy = {**claim, "status": "busy"}
        assert _invoke("acquire", "--slot", slot, "--owner", "peer", code=1) == busy
        assert _invoke("acquire", "--slot", slot, "--owner", "recovering", code=1) == busy
        held = {**claim, "status": "inspection-required"}
        assert _invoke("hold", "--slot", slot, "--owner", "recovering") == held
        assert _invoke("acquire", "--slot", slot, "--owner", "recovering", code=1) == held
        assert _invoke("acquire", "--slot", slot, "--owner", "peer", code=1) == held
        assert _invoke("status", "--slot", slot) == held

    def test_acquire_rejects_locator_outside_git_metadata(self, checkout: Path) -> None:
        """A recovery claim refuses a slot under an arbitrary directory and leaves that directory alone."""
        slot = checkout / "codex-review-slot"
        result = _invoke("acquire", "--slot", slot, "--owner", "recovering", code=2)
        assert result["status"] == "error"
        assert not slot.exists()

    def test_expired_wait_does_not_steal_interrupted_owner(self, checkout: Path) -> None:
        """A completed acquisition survives process exit and aging until its original owner releases it."""
        claim = _invoke("acquire", "--checkout", checkout, "--owner", "interrupted")
        os.utime(claim["slot"], (1, 1))
        assert _invoke("acquire", "--checkout", checkout, "--owner", "later", "--wait-seconds", "1", code=1) == {
            **claim,
            "status": "busy",
        }

    def test_queued_acquire_continues_after_owner_release(self, checkout: Path) -> None:
        """One bounded acquire waits for release and returns the new owner's locator."""
        claim = _invoke("acquire", "--checkout", checkout, "--owner", "active")
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(
                _invoke, "acquire", "--checkout", checkout, "--owner", "queued", "--wait-seconds", "10"
            )
            with pytest.raises(FutureTimeoutError):
                waiting.result(timeout=0.2)
            _invoke("release", "--slot", claim["slot"], "--owner", "active")
            assert waiting.result(timeout=12) == {**claim, "owner": "queued"}

    def test_queue_reports_peer_and_acquires_after_release(self, checkout: Path) -> None:
        """One cancellable command stays queued and publishes a claim only after peer release."""
        claim = _invoke("acquire", "--checkout", checkout, "--owner", "active")
        slot = claim["slot"]
        argv = [sys.executable, str(HELPER), "acquire", "--slot", slot, "--owner", "queued", "--queue"]
        with (
            subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as queued,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            try:
                peer_status = pool.submit(queued.stderr.readline)
                assert json.loads(peer_status.result(timeout=5)) == {**claim, "status": "busy", "queue": "waiting"}
                assert queued.poll() is None
                assert _invoke("status", "--slot", slot) == {**claim, "status": "busy"}
                held = _invoke("hold", "--slot", slot, "--owner", "active")
                assert json.loads(pool.submit(queued.stderr.readline).result(timeout=5)) == {**held, "queue": "waiting"}
                assert _invoke("release", "--inspected", "--slot", slot, "--owner", "active")["status"] == "released"
                stdout, stderr = queued.communicate(timeout=5)
                assert queued.returncode == 0, stderr
                assert json.loads(stdout) == {**claim, "owner": "queued"}
            finally:
                if queued.poll() is None:
                    queued.terminate()

    def test_queue_returns_unowned_busy_slot_for_inspection(self, checkout: Path) -> None:
        """An empty marker directory cannot keep an automated waiter indefinitely."""
        slot = Path(_invoke("status", "--checkout", checkout)["slot"])
        slot.mkdir()
        assert _invoke("acquire", "--slot", slot, "--owner", "queued", "--queue", code=1) == {
            "status": "busy",
            "owner": None,
            "slot": str(slot),
        }

    def test_empty_slot_is_blocked_until_owner_is_reconciled(self, checkout: Path) -> None:
        """Interrupted release leaves an empty slot that cannot admit an unrelated review."""
        slot = Path(_invoke("status", "--checkout", checkout)["slot"])
        slot.mkdir()
        assert _invoke("acquire", "--checkout", checkout, "--owner", "waiting", code=1) == {
            "status": "busy",
            "owner": None,
            "slot": str(slot),
        }
        assert _invoke("release", "--slot", slot, "--owner", "waiting", code=1)["status"] == "not-owner"
        assert slot.is_dir()

    def test_inspection_hold_stops_waiter_until_explicit_owner_release(
        self, checkout: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A queued peer resumes automatically only after the owner releases the inspected hold."""
        claim = _invoke("acquire", "--checkout", checkout, "--owner", "active")
        slot = claim["slot"]
        held = {**claim, "status": "inspection-required"}
        assert _invoke("hold", "--slot", slot, "--owner", "peer", code=1)["status"] == "not-owner"
        assert _invoke("hold", "--slot", slot, "--owner", "active") == held
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(_invoke, "acquire", "--slot", slot, "--owner", "peer", "--wait-seconds", "10")
            with pytest.raises(FutureTimeoutError):
                waiting.result(timeout=0.2)
            with monkeypatch.context() as patch:
                patch.setenv("PATH", "")
                assert _invoke("hold", "--slot", slot, "--owner", "active") == held
                assert _invoke("hold", "--slot", slot, "--owner", "active") == held
                assert _invoke("status", "--slot", slot) == held
                assert _invoke("release", "--slot", slot, "--owner", "active", code=1) == held
                assert (
                    _invoke("release", "--inspected", "--slot", slot, "--owner", "peer", code=1)["status"]
                    == "not-owner"
                )
            assert _invoke("acquire", "--slot", slot, "--owner", "active", code=1) == held
            assert _invoke("acquire", "--slot", slot, "--owner", "later", "--wait-seconds", "1", code=1) == held
            assert _invoke("release", "--inspected", "--slot", slot, "--owner", "active")["status"] == "released"
            assert waiting.result(timeout=12) == {**claim, "owner": "peer"}

    def test_release_rejects_an_unrelated_directory(self, checkout: Path) -> None:
        """A correct-looking slot name outside Git metadata cannot release filesystem state."""
        slot = checkout / "codex-review-slot"
        slot.mkdir()
        assert _invoke("release", "--slot", slot, "--owner", "first", code=2)["status"] == "error"
        assert slot.is_dir()

    @pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="directory symlink capability unavailable")
    def test_release_refuses_redirected_locator(self, checkout: Path) -> None:
        """A symlink in place of the retained slot cannot release the redirected owner."""
        first = _invoke("acquire", "--checkout", checkout, "--owner", "first")
        second = checkout / "independent"
        git_text(checkout, "init", "-q", str(second))
        target = _invoke("acquire", "--checkout", second, "--owner", "first")
        slot = Path(first["slot"])
        slot.rename(slot.with_suffix(".original"))
        slot.symlink_to(target["slot"], target_is_directory=True)
        assert _invoke("release", "--slot", slot, "--owner", "first", code=2)["status"] == "error"
        assert _invoke("status", "--slot", target["slot"])["owner"] == "first"


@pytest.mark.integration
def test_distinct_review_targets_share_checkout_until_protected_operation(checkout: Path) -> None:
    """Ordinary peers overlap, while an exclusive interval conflicts in both directions."""
    first = "https://github.com/acme/project/pull/1"
    second = "https://github.com/acme/project/pull/2"
    linked = checkout / "linked"
    git_text(checkout, "worktree", "add", "--detach", str(linked))
    a = _invoke("acquire", "--checkout", checkout, "--target", first, "--owner", "a")
    b = _invoke("acquire", "--checkout", linked, "--target", second, "--owner", "b")
    assert a["slot"] != b["slot"]
    assert _invoke("acquire", "--checkout", linked, "--target", first, "--owner", "c", code=1)["owner"] == "a"
    assert _invoke("acquire", "--checkout", checkout, "--owner", "c", code=1)["status"] == "busy"
    for claim in (a, b):
        _invoke("release", "--slot", claim["slot"], "--owner", claim["owner"])
    protected = _invoke("acquire", "--checkout", linked, "--owner", "protected")
    assert _invoke("acquire", "--slot", a["slot"], "--owner", "a", code=1)["owner"] == "protected"
    _invoke("hold", "--slot", protected["slot"], "--owner", "protected")
    assert (
        _invoke("acquire", "--checkout", linked, "--target", second, "--owner", "b", code=1)["status"]
        == "inspection-required"
    )


@pytest.mark.integration
def test_writers_bind_to_worktree_independently_of_review(checkout: Path) -> None:
    """A second writer cannot switch the invoking branch underneath an active remediation."""
    linked = checkout / "linked"
    git_text(checkout, "worktree", "add", "--detach", str(linked))
    writer = _invoke("acquire", "--checkout", checkout, "--writer", "--owner", "a")
    assert _invoke("acquire", "--checkout", checkout, "--writer", "--owner", "b", code=1)["owner"] == "a"
    assert _invoke("acquire", "--checkout", linked, "--writer", "--owner", "b")["slot"] != writer["slot"]
    assert _invoke("acquire", "--checkout", checkout, "--local", "--owner", "c", code=1)["owner"] == "a"
    _invoke("release", "--slot", writer["slot"], "--owner", "a")
    local = _invoke("acquire", "--checkout", checkout, "--local", "--owner", "c")
    assert _invoke("acquire", "--checkout", checkout, "--writer", "--owner", "a", code=1)["owner"] == "c"
    _invoke("release", "--slot", local["slot"], "--owner", "c")
    assert _invoke("acquire", "--checkout", checkout, "--owner", "a")["status"] == "acquired"


@pytest.mark.integration
def test_racing_protected_and_ordinary_acquisitions_never_both_enter(checkout: Path) -> None:
    """Conflicting requests issued together must select one winner, including first use of the scheduler."""
    target = "https://github.com/acme/project/pull/3"
    with ThreadPoolExecutor(max_workers=2) as pool:
        for attempt in range(4):
            processes = [
                pool.submit(
                    subprocess.run,
                    [sys.executable, str(HELPER), "acquire", "--checkout", str(checkout), *scope, "--owner", owner],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
                for scope, owner in (([], f"protected-{attempt}"), (["--target", target], f"review-{attempt}"))
            ]
            responses = [process.result() for process in processes]
            assert sorted(response.returncode for response in responses) == [0, 1]
            winner, loser = sorted(responses, key=lambda response: response.returncode)
            claim = json.loads(winner.stdout)
            peer = json.loads(loser.stdout)
            assert claim["status"] == "acquired"
            assert peer["status"] == "busy"
            assert peer["owner"] == claim["owner"]
            _invoke("release", "--slot", claim["slot"], "--owner", claim["owner"])
