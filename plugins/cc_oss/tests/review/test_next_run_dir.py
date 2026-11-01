"""Tests for ``bin/next_run_dir.py``.

Allocation runs against real directories under ``tmp_path`` — no mocking of the filesystem except to simulate a lost
race, since the whole point of the module is atomic filesystem behavior.
"""

from __future__ import annotations

from pathlib import Path

import next_run_dir as nrd
import pytest


class TestNextIndex:
    """``next_index`` — computing the next run number from existing siblings."""

    def test_returns_one_for_empty_directory(self, tmp_path: Path) -> None:
        """An unpopulated ``pr-<N>`` directory starts its run sequence at 1.

        Scenario: the first review of a PR; no ``run-NNN`` sibling exists yet.
        """
        assert nrd.next_index(tmp_path) == 1

    def test_returns_max_plus_one_with_a_gap(self, tmp_path: Path) -> None:
        """A hand-deleted run leaving a gap must not make the next index collide with a survivor.

        Only run-002 exists (run-001 was deleted); counting sibling directories would wrongly compute 2 (1 sibling + 1)
        and collide with the still-present run-002.
        """
        (tmp_path / "run-002").mkdir()
        assert nrd.next_index(tmp_path) == 3

    def test_ignores_malformed_sibling(self, tmp_path: Path) -> None:
        """A stray non-numeric or short-suffix directory does not block allocation.

        Scenario: ``run-abc`` and ``run-01`` (fewer than 3 digits, not a valid run id) sit beside the real ``run-005``.
        """
        (tmp_path / "run-abc").mkdir()
        (tmp_path / "run-01").mkdir()
        (tmp_path / "run-005").mkdir()
        assert nrd.next_index(tmp_path) == 6

    def test_ignores_non_directory_sibling(self, tmp_path: Path) -> None:
        """A file named like a run directory does not count toward the sequence.

        Only directories are runs; a stray ``run-003`` file must not push the next run to 4.
        """
        (tmp_path / "run-003").write_text("not a directory", encoding="utf-8")
        assert nrd.next_index(tmp_path) == 1


class TestAllocateRunDir:
    """``allocate_run_dir`` — exclusive directory creation."""

    def test_creates_first_run_directory(self, tmp_path: Path) -> None:
        """A fresh PR directory gets ``run-001`` on its first allocation.

        Scenario: the PR directory does not exist yet, so the call creates the parent and the first run in one step.
        """
        pr_dir = tmp_path / "pr-1481"
        created = nrd.allocate_run_dir(pr_dir)
        assert created == pr_dir / "run-001"
        assert created.is_dir()
        assert pr_dir.is_dir()

    def test_skips_index_contested_by_a_concurrent_allocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A run directory created between the index read and the mkdir call is not overwritten.

        Simulates two concurrent /oss:review invocations racing for the same PR: both read next_index()==1, but only one
        may win run-001. The loser must retry onto run-002 instead of silently reusing (and overwriting the contents of)
        run-001.
        """
        pr_dir = tmp_path / "pr-1481"
        pr_dir.mkdir()
        real_mkdir = Path.mkdir
        contested = {"run_001": False}

        def racing_mkdir(self: Path, *args: object, **kwargs: object) -> None:
            """Let a rival create ``run-001`` first, once, then behave normally."""
            if self.name == "run-001" and not contested["run_001"]:
                contested["run_001"] = True
                real_mkdir(pr_dir / "run-001")  # a concurrent process wins first
                raise FileExistsError(self)
            real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", racing_mkdir)
        created = nrd.allocate_run_dir(pr_dir)
        assert created == pr_dir / "run-002"

    def test_raises_when_every_attempt_is_contested(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exhausting the retry budget raises instead of looping forever or silently overwriting.

        Scenario: every ``mkdir`` of a run directory reports it already exists, so no attempt can ever succeed.
        """
        pr_dir = tmp_path / "pr-1481"
        pr_dir.mkdir()

        def always_exists(self: Path, *args: object, **kwargs: object) -> None:
            """Fail every run-directory creation; let the parent-creating ``exist_ok`` call through."""
            if kwargs.get("exist_ok"):
                return  # the pr_dir-creation call at the top of allocate_run_dir — let it pass
            raise FileExistsError(self)

        monkeypatch.setattr(Path, "mkdir", always_exists)
        monkeypatch.setattr(nrd, "next_index", lambda _pr_dir: 1)
        with pytest.raises(RuntimeError, match="could not allocate"):
            nrd.allocate_run_dir(pr_dir)


class TestMain:
    """CLI entry point."""

    def test_prints_created_path_and_returns_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """A successful allocation prints the new directory path and exits 0.

        The skill reads the printed path as the run directory, so stdout must hold only that path.
        """
        pr_dir = tmp_path / "pr-1481"
        assert nrd.main(["--pr-dir", str(pr_dir)]) == 0
        assert capsys.readouterr().out.strip() == str(pr_dir / "run-001")

    def test_returns_one_on_allocation_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An allocation failure is reported on stderr with a non-zero exit, not a traceback.

        Scenario: the allocator exhausts its retries; the CLI turns the ``RuntimeError`` into exit 1 and a message.
        """

        def boom(_pr_dir: Path) -> Path:
            """Fail like an exhausted retry budget."""
            raise RuntimeError("could not allocate a run directory under x after 50 attempts")

        monkeypatch.setattr(nrd, "allocate_run_dir", boom)
        assert nrd.main(["--pr-dir", str(tmp_path / "pr-1481")]) == 1
        assert "could not allocate" in capsys.readouterr().err
