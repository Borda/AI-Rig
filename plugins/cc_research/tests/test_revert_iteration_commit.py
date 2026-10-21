"""Tests for ``bin/revert_iteration_commit.py`` — the double-revert guard shared by run Phase 5 and Phase 7.

Each test builds a throwaway git repository, so the guard is exercised against real ``git log`` / ``git revert``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import revert_iteration_commit as guard

_GIT_ENV = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return stdout."""
    cmd = ["git", *_GIT_ENV, *args]
    done = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(repo: Path, subject: str, content: str) -> None:
    """Commit ``content`` as ``f.txt`` with ``subject``."""
    (repo / "f.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", subject)


def _commit_raw_subject(repo: Path, subject: bytes, content: str) -> None:
    """Commit ``content`` as ``f.txt`` with a byte-exact ``subject``, which may not be valid UTF-8.

    ``git commit`` and ``git commit-tree`` both repair a non-UTF-8 message by re-encoding it, so the raw commit object
    is written with ``hash-object`` (bytes on stdin, which also works on Windows) and moved onto the branch.
    """
    (repo / "f.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", "f.txt")
    identity = b"t <t@example.com> 0 +0000"
    body = b"tree %b\nparent %b\nauthor %b\ncommitter %b\n\n%b\n" % (
        _git(repo, "write-tree").encode(),
        _git(repo, "rev-parse", "HEAD").encode(),
        identity,
        identity,
        subject,
    )
    made = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, input=body, capture_output=True, check=True
    )
    _git(repo, "update-ref", "HEAD", made.stdout.decode().strip())


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Repository with a baseline commit, set as the working directory."""
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }.items():
        monkeypatch.setenv(key, value)
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "baseline", "0\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_reverts_head_when_it_is_the_iteration_commit(repo: Path) -> None:
    """HEAD is ``experiment(optimize/i3): ...`` → reverted once."""
    _commit(repo, "experiment(optimize/i3): try x", "1\n")
    assert guard.main(["--iteration", "3", "--phase", "7"]) == 0
    assert _git(repo, "log", "-1", "--format=%s").startswith('Revert "experiment(optimize/i3)')
    assert (repo / "f.txt").read_text(encoding="utf-8") == "0\n"


def test_second_call_skips_instead_of_reverting_the_revert(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Phase 5 reverts, then Phase 7 asks again: the revert's subject embeds the original but must not match."""
    _commit(repo, "experiment(optimize/i3): try x", "1\n")
    assert guard.main(["--iteration", "3", "--phase", "5"]) == 0
    capsys.readouterr()
    assert guard.main(["--iteration", "3", "--phase", "7"]) == 0
    assert capsys.readouterr().out.strip() == (
        "Phase 7: HEAD is not iteration 3's experiment commit — Phase 5 already reverted; skipping double-revert."
    )
    assert (repo / "f.txt").read_text(encoding="utf-8") == "0\n"
    assert _git(repo, "rev-list", "--count", "HEAD") == "3"


@pytest.mark.parametrize(
    ("phase", "tail"),
    [
        pytest.param("5", "— already reverted; skipping double-revert.", id="phase-5"),
        pytest.param("7", "— Phase 5 already reverted; skipping double-revert.", id="phase-7"),
    ],
)
def test_skip_message_is_phase_specific(repo: Path, capsys: pytest.CaptureFixture[str], phase: str, tail: str) -> None:
    """HEAD already reverted (no experiment commit at HEAD) → skip, with the wording each phase documented.

    The skip path is the double-revert case; another iteration's experiment commit is refused instead (next test).
    """
    _commit(repo, 'Revert "experiment(optimize/i3): try x"', "2\n")
    assert guard.main(["--iteration", "3", "--phase", phase]) == 0
    assert capsys.readouterr().out.strip() == f"Phase {phase}: HEAD is not iteration 3's experiment commit {tail}"


@pytest.mark.parametrize("phase", [pytest.param("5", id="phase-5"), pytest.param("7", id="phase-7")])
def test_other_iterations_experiment_commit_is_blocked(
    repo: Path, capsys: pytest.CaptureFixture[str], phase: str
) -> None:
    """HEAD is ``experiment(optimize/i2)`` while the sentinel says iteration 3 → BLOCKED, nothing reverted.

    Phase 4 types the number into the subject; a mismatch with the Phase 0 sentinel used to skip silently and keep a
    change the loop had decided to revert.
    """
    _commit(repo, "experiment(optimize/i2): other", "1\n")
    assert guard.main(["--iteration", "3", "--phase", phase]) == 1
    out = capsys.readouterr().out.strip()
    assert out.startswith(
        f"! BLOCKED — Phase {phase}: HEAD is iteration 2's experiment commit, but this is iteration 3"
    )
    assert (repo / "f.txt").read_text(encoding="utf-8") == "1\n"
    assert _git(repo, "log", "-1", "--format=%s") == "experiment(optimize/i2): other"


def test_iteration_1_does_not_match_iteration_10(repo: Path) -> None:
    """The ``):`` closing the prefix keeps ``i1`` from matching an ``i10`` commit — it is never reverted.

    Being another iteration's experiment commit, it is now refused with exit 1 rather than skipped with exit 0.
    """
    _commit(repo, "experiment(optimize/i10): other", "1\n")
    assert guard.main(["--iteration", "1", "--phase", "7"]) == 1
    assert (repo / "f.txt").read_text(encoding="utf-8") == "1\n"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        pytest.param(b"experiment(optimize/i3): calf\xe9 try", True, id="own-iteration"),
        pytest.param(b"experiment(optimize/i2): calf\xe9 try", False, id="other-iteration"),
        pytest.param(b"calf\xe9 baseline", False, id="no-prefix"),
    ],
)
def test_non_utf8_subject_is_compared_not_raised(repo: Path, subject: bytes, expected: bool) -> None:
    """A commit subject that is not valid UTF-8 is still matched on its ASCII prefix instead of raising.

    A Latin-1 subject (``calf\\xe9``) at HEAD used to raise ``UnicodeDecodeError`` inside ``subprocess.run`` and end the
    guard in a traceback that the caller read as a failed revert; the old ``git log | grep -qE`` form matched bytes.
    """
    _commit_raw_subject(repo, subject, "1\n")

    assert guard.head_is_iteration_commit(shutil.which("git") or "git", "3") is expected


def test_non_utf8_iteration_commit_is_still_reverted(repo: Path) -> None:
    """The guard reverts this iteration's commit even when its subject carries a non-UTF-8 byte."""
    _commit_raw_subject(repo, b"experiment(optimize/i3): calf\xe9 try", "1\n")

    assert guard.main(["--iteration", "3", "--phase", "7"]) == 0
    assert (repo / "f.txt").read_text(encoding="utf-8") == "0\n"


def test_empty_iteration_is_blocked(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unset iteration number fails closed with the BLOCKED message and reverts nothing."""
    _commit(repo, "experiment(optimize/i3): try x", "1\n")
    assert guard.main(["--iteration", "", "--phase", "5"]) == 1
    assert capsys.readouterr().out.strip() == "! BLOCKED — Phase 5: iteration number unset, cannot scope revert guard"
    assert (repo / "f.txt").read_text(encoding="utf-8") == "1\n"


def test_outside_a_repository_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreadable history never authorises a revert."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert guard.main(["--iteration", "3", "--phase", "7"]) == 0


def test_invalid_phase_is_an_argument_error() -> None:
    """Only phases 5 and 7 exist."""
    assert guard.main(["--iteration", "3", "--phase", "6"]) == 2
