"""Tests for ``bin/check_base_fresh.py`` against real throwaway repositories.

Each scenario builds a bare ``origin``, a clone working on a PR branch, and a seed repository that can advance the
target branch on ``origin`` after the clone last fetched it — the race that leaves a resolved PR behind its target.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import check_base_fresh as cbf

_skip_no_git = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available")
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    """Run one git command in a fixture repository and return its stripped stdout."""
    env = {**os.environ, **_GIT_ENV}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    """Add one file and commit it with the file name as the subject."""
    (repo / name).write_text(name, encoding="utf-8", newline="\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@pytest.fixture
def repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Build seed + bare origin + clone on branch ``feature``; cwd is the clone."""
    for key, value in _GIT_ENV.items():
        monkeypatch.setenv(key, value)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, "base.txt")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    _git(seed, "remote", "add", "origin", str(origin))
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "checkout", "-q", "-b", "feature")
    _commit(clone, "feature.txt")
    monkeypatch.chdir(clone)
    return clone, seed


def _advance_origin(seed: Path) -> None:
    """Land a new target commit on ``origin/main`` after the clone's last fetch."""
    _commit(seed, "target-new.txt")
    _git(seed, "push", "-q", "origin", "main")


@_skip_no_git
@pytest.mark.integration
class TestCheckBaseFresh:
    """Freshness verdicts for the PR branch against the target branch."""

    def test_reports_fresh_when_target_merged(self, repos: tuple[Path, Path], capsys: pytest.CaptureFixture) -> None:
        """A branch that contains the target tip is fresh and behind by zero.

        Baseline contract: right after Step 5's merge nothing has moved, so Step 9 must not trigger a re-merge.
        """
        code = cbf.main(["--base-ref", "main"])

        out = capsys.readouterr().out
        assert code == 0
        assert "BASE_FETCH=ok\nBASE_FRESH=yes" in out
        assert "BASE_BEHIND=0" in out

    @pytest.mark.parametrize(
        ("flags", "fetch", "fresh"),
        [
            pytest.param([], "ok", "no", id="fetch-sees-new-target"),
            pytest.param(["--no-fetch"], "skipped", "yes", id="no-fetch-trusts-stale-ref"),
        ],
    )
    def test_target_advanced_after_last_fetch(
        self, repos: tuple[Path, Path], capsys: pytest.CaptureFixture, flags: list[str], fetch: str, fresh: str
    ) -> None:
        """Only a fresh fetch reveals a target commit that landed after the clone last fetched.

        This is the observed failure: the target moved minutes after the merge, and a check that only reads the
        stale remote-tracking ref would wrongly call the branch fresh.
        """
        _advance_origin(repos[1])

        code = cbf.main(["--base-ref", "main", *flags])

        out = capsys.readouterr().out
        assert code == 0
        assert f"BASE_FETCH={fetch}\nBASE_FRESH={fresh}" in out

    def test_lists_missing_target_commits(self, repos: tuple[Path, Path], capsys: pytest.CaptureFixture) -> None:
        """A stale branch reports the missing commit count and their subjects for the push confirmation."""
        _advance_origin(repos[1])

        cbf.main(["--base-ref", "main"])

        out = capsys.readouterr().out
        assert "BASE_BEHIND=1" in out
        assert out.rstrip().endswith("target-new.txt")

    def test_failed_fetch_still_compares_last_known_ref(
        self, repos: tuple[Path, Path], capsys: pytest.CaptureFixture
    ) -> None:
        """An unreachable remote does not hide drift that another tool's fetch already recorded.

        The remote-tracking ref was refreshed earlier (e.g. by an IDE), then the network went away; the verdict must
        still be ``no`` from the last-known ref rather than ``unknown``.
        """
        clone, seed = repos
        _advance_origin(seed)
        _git(clone, "fetch", "-q", "origin")
        _git(clone, "remote", "set-url", "origin", str(clone.parent / "missing.git"))

        code = cbf.main(["--base-ref", "main"])

        out = capsys.readouterr().out
        assert code == 0
        assert "BASE_FETCH=failed\nBASE_FRESH=no" in out

    @pytest.mark.parametrize(
        "base_ref",
        [
            pytest.param("absent", id="no-remote-tracking-ref"),
            pytest.param("-x", id="dash-leading-ref"),
            pytest.param("bad..ref", id="invalid-branch-name"),
        ],
    )
    def test_unknowable_verdict_exits_one(
        self, repos: tuple[Path, Path], capsys: pytest.CaptureFixture, base_ref: str
    ) -> None:
        """A target that cannot be resolved yields ``unknown`` and exit 1, never a false ``yes``."""
        code = cbf.main([f"--base-ref={base_ref}", "--no-fetch"])

        assert code == 1
        assert "BASE_FRESH=unknown" in capsys.readouterr().out


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("skills/resolve/modes/lint-qa-gate.md", id="step9-gate"),
        pytest.param("skills/resolve/SKILL.md", id="step10-scope"),
    ],
)
def test_resolve_invokes_drift_check(path: str) -> None:
    """Both late checkpoints — Step 9 entry and the Step 10 scope block — call the drift helper.

    Removing either silently re-opens the stale-target push: the Step 5 merge alone cannot see later target commits.
    """
    text = (Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8")

    assert 'bin/check_base_fresh.py" --base-ref "$BASE_REF"' in text
