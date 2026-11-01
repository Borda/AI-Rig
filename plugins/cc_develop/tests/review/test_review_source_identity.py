"""Keep local review provenance bound before source inspection and separate from readiness."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from _develop_text import bash_blocks

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/review/SKILL.md"
_BASH = shutil.which("bash")
_skip_no_bash = pytest.mark.skipif(_BASH is None, reason="bash unavailable")
_UNAVAILABLE = "unavailable (non-Git directory or unborn HEAD)"


def _scope() -> str:
    """Return the Step 1 text of the review skill that precedes its scope pre-check."""
    return (
        _SKILL.read_text(encoding="utf-8").split("## Step 1: Identify scope", 1)[1].split("### Scope pre-check", 1)[0]
    )


def _capture_block() -> str:
    """Return the bash block that records the reviewed commit SHA."""
    blocks = bash_blocks(_scope())
    return next(block for block in blocks if "dev-review-commit-sha-" in block)


def _run_capture(cwd: Path, tmp_path: Path, csid: str) -> subprocess.CompletedProcess[str]:
    """Run the capture block in ``cwd`` with sentinels redirected into ``tmp_path``."""
    env = {**os.environ, "TMPDIR": str(tmp_path), "CLAUDE_CODE_SESSION_ID": csid}
    return subprocess.run(
        [_BASH, "-c", _capture_block()], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _git(cwd: Path, *args: str) -> None:
    """Run a quiet git command in ``cwd``, failing the test if git rejects it."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _write_target_arg(tmp_path: Path, csid: str, target: Path) -> None:
    """Write the clean-args sentinel the way the shipped Bash writer does: forward slashes and LF on every host."""
    (tmp_path / f"dev-review-clean-args-{csid}").write_bytes((target.as_posix() + "\n").encode("utf-8"))


def _cwd_non_git(tmp_path: Path) -> Path:
    """Return a plain directory that is not a Git repository."""
    return tmp_path


def _cwd_unborn(tmp_path: Path) -> Path:
    """Return a freshly initialised repository that has no commit yet."""
    _git(tmp_path, "init", "-q")
    return tmp_path


def _cwd_external_target(tmp_path: Path) -> Path:
    """Return a repository cwd while the review target is a file outside any repository."""
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    _write_target_arg(tmp_path, "identity-test", target)
    return _PLUGIN


@pytest.fixture
def repository_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a committed repository with ``module.py`` plus a detached worktree; return (repository, worktree)."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "module.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "add", "module.py")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    worktree = tmp_path / "worktree"
    _git(repository, "worktree", "add", "--detach", str(worktree))
    return repository, worktree


class TestIdentityCapture:
    """The shipped capture recipe records the actual full base or an explicit unavailable reason."""

    @_skip_no_bash
    @pytest.mark.parametrize(
        "plant_cwd",
        [
            pytest.param(_cwd_non_git, id="non-git"),
            pytest.param(_cwd_unborn, id="unborn"),
            pytest.param(_cwd_external_target, id="external-target"),
        ],
    )
    def test_records_unavailable_reason_without_a_committed_base(
        self, tmp_path: Path, plant_cwd: Callable[[Path], Path]
    ) -> None:
        """Without a commit that covers the target the sentinel names why no SHA is available.

        A non-Git directory, an unborn HEAD and a target outside the repository must each record the explicit
        unavailable reason instead of a SHA that would overstate what was reviewed.
        """
        cwd = plant_cwd(tmp_path)
        result = _run_capture(cwd, tmp_path, "identity-test")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "dev-review-commit-sha-identity-test").read_text().strip() == _UNAVAILABLE

    @_skip_no_bash
    def test_records_head_of_a_repository_target(self, tmp_path: Path) -> None:
        """Inside a repository the sentinel holds the full HEAD SHA and the output flags a working-tree base.

        The SHA is bound before any source inspection so the report cites the base the review started from.
        """
        result = _run_capture(_PLUGIN, tmp_path, "identity-test")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_PLUGIN, text=True).strip()
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "dev-review-commit-sha-identity-test").read_text().strip() == head
        assert "working-tree base" in result.stdout

    def test_capture_precedes_target_resolution(self) -> None:
        """The capture recipe runs before ``resolve_review_target.py`` so inspection cannot change the recorded base."""
        scope = _scope()
        assert scope.index("dev-review-commit-sha-") < scope.index("resolve_review_target.py")

    @_skip_no_bash
    @pytest.mark.integration
    @pytest.mark.parametrize(
        "target_in",
        [pytest.param("worktree", id="existing-worktree"), pytest.param("repository", id="outside-worktree")],
    )
    def test_worktree_flag_does_not_certify_committed_target(
        self, tmp_path: Path, repository_with_worktree: tuple[Path, Path], target_in: str
    ) -> None:
        """Requested isolation cannot turn dirty explicit targets into committed-source evidence.

        A real repository and detached worktree hold an uncommitted edit to ``module.py``; with the worktree flag set
        the capture must still describe a working-tree base and never claim committed worktree source.
        """
        repository, worktree = repository_with_worktree
        target = {"worktree": worktree, "repository": repository}[target_in] / "module.py"
        target.write_text("value = 2\n", encoding="utf-8")
        _write_target_arg(tmp_path, "worktree-identity", target)
        (tmp_path / "dev-review-worktree-worktree-identity").write_bytes(b"true\n")
        result = _run_capture(worktree, tmp_path, "worktree-identity")
        assert result.returncode == 0, result.stderr
        assert "working-tree base; reviewed files may include uncommitted changes" in result.stdout
        assert "committed worktree source" not in result.stdout
        assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_consolidator_retains_bound_sha_and_supported_source_judgment() -> None:
    """Presentation must retain source evidence even when execution remains blocked.

    The skill must hand the consolidator the bound SHA and its kind, and the prompt and report template must keep the
    commit line, the supported-source judgment and the no-tests-run disclosure.
    """
    skill = _SKILL.read_text(encoding="utf-8")
    prompt = (_PLUGIN / "skills/review/templates/consolidator-prompt.md").read_text(encoding="utf-8")
    report = (_PLUGIN / "skills/review/templates/review-report.md").read_text(encoding="utf-8")
    assert "REVIEWED_COMMIT_SHA=<" in skill
    assert "REVIEWED_COMMIT_KIND=<" in skill
    assert "Commit SHA:" in report
    assert "`Commit SHA:`" in prompt
    assert "Preserve a supported source judgment" in prompt
    assert "independence" in prompt
    assert "runs no tests" in report
