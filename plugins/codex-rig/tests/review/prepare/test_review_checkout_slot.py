"""Exercise checkout-scoped review ownership with real Git worktrees and concurrent CLI processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

import pytest
from _git_fixtures import git_text
from _platform import DIRECTORY_SYMLINKS_AVAILABLE

HELPER = Path(__file__).resolve().parents[3] / "shared/review_checkout_slot.py"


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
                assert json.loads(peer_status.result(timeout=5)) == {**claim, "status": "busy"}
                assert queued.poll() is None
                assert _invoke("status", "--slot", slot) == {**claim, "status": "busy"}
                held = _invoke("hold", "--slot", slot, "--owner", "active")
                assert json.loads(pool.submit(queued.stderr.readline).result(timeout=5)) == held
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
