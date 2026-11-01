"""Exercise the offline harness Git boundary with real repository reads."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
_requires_bash = pytest.mark.skipif(BASH is None, reason="offline harness requires Bash")
_BLOCKED_EXIT = 125


class HarnessGit(NamedTuple):
    """An isolated real repository, the environment that routes ``git`` through the harness wrapper, and the wrapper."""

    repository: Path
    environment: dict[str, str]
    wrapper: Path


def _git(real_git: str, repository: Path, environment: dict[str, str], *arguments: str) -> None:
    """Run one real Git command in ``repository`` and fail the fixture when it fails."""
    subprocess.run([real_git, *arguments], cwd=repository, env=environment, check=True, capture_output=True)


@pytest.fixture(scope="module")
def harness_git(tmp_path_factory: pytest.TempPathFactory) -> HarnessGit:
    """Create the harness's actual Git wrapper around an isolated real repository.

    Every test only reads the repository or is refused by the wrapper before Git runs, so one copy serves the module.
    """
    tmp_path = tmp_path_factory.mktemp("harness-git")
    real_git = shutil.which("git")
    assert real_git is not None
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    _git(real_git, repository, environment, "init", "-q")
    _git(real_git, repository, environment, "config", "user.name", "Fixture")
    _git(real_git, repository, environment, "config", "user.email", "fixture@example.invalid")
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(real_git, repository, environment, "add", "tracked.txt")
    _git(real_git, repository, environment, "commit", "-qm", "fixture")
    (repository / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")

    source = (ROOT / ".github" / "codex-harness.sh").read_text(encoding="utf-8")
    wrapper_text = source.split('cat >"$TMP_BIN/git" <<EOF\n', 1)[1].split("\nEOF\n", 1)[0]
    wrapper_text = wrapper_text.replace("$REAL_GIT", Path(real_git).as_posix()).replace("\\$", "$")
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    wrapper = bin_directory / "git"
    wrapper.write_text(wrapper_text + "\n", encoding="utf-8", newline="\n")
    wrapper.chmod(0o755)
    environment["PATH"] = str(bin_directory) + os.pathsep + environment["PATH"]
    return HarnessGit(repository, environment, wrapper)


def _run_wrapper(harness: HarnessGit, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the harness ``git`` wrapper with Bash inside the fixture repository."""
    return subprocess.run(
        [str(BASH), harness.wrapper.as_posix(), *arguments],
        cwd=harness.repository,
        env=harness.environment,
        capture_output=True,
        text=True,
        check=False,
    )


@_requires_bash
@pytest.mark.integration
class TestAllowedReads:
    """Canonical read-only Git calls that calibration needs pass through the wrapper to real Git."""

    def test_diff_binary_against_head(self, harness_git: HarnessGit) -> None:
        """The canonical ``diff --binary HEAD`` call returns the working-tree change.

        The repository has one tracked file edited after its commit. Calibration reads this diff offline, so the wrapper
        must forward it (with formatting-only ``-c core.quotePath``) to real Git.
        """
        completed = _run_wrapper(harness_git, "-c", "core.quotePath=true", "diff", "--binary", "HEAD")

        assert completed.returncode == 0, completed.stderr
        assert "-before\n+after" in completed.stdout

    def test_show_toplevel_with_literal_pathspecs(self, harness_git: HarnessGit) -> None:
        """The repository-root lookup returns the real repository directory.

        Calibration resolves the repository root with ``-C . --literal-pathspecs rev-parse --show-toplevel``.
        """
        completed = _run_wrapper(harness_git, "-C", ".", "--literal-pathspecs", "rev-parse", "--show-toplevel")

        assert completed.returncode == 0, completed.stderr
        assert Path(completed.stdout.strip()).resolve() == harness_git.repository.resolve()

    def test_lists_untracked_files(self, harness_git: HarnessGit) -> None:
        """The untracked-file listing returns exactly the one new file.

        Untracked files are part of the working tree calibration snapshots, so ``ls-files --others`` must pass through.
        """
        completed = _run_wrapper(harness_git, "-C", ".", "ls-files", "--others", "--exclude-standard")

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.splitlines() == ["untracked.txt"]


@_requires_bash
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["fetch"], id="fetch"),
        pytest.param(["push"], id="push"),
        pytest.param(["-C", ".", "fetch"], id="directory-fetch"),
        pytest.param(["-c", "alias.send=push", "send"], id="arbitrary-configuration"),
        pytest.param(["-c", "core.quotePath=true", "fetch"], id="formatting-config-fetch"),
        pytest.param(["-C", ".", "--literal-pathspecs", "fetch"], id="literal-path-fetch"),
    ],
)
def test_wrapper_blocks_network_git(harness_git: HarnessGit, arguments: list[str]) -> None:
    """Keep network operations and arbitrary configuration outside the harness allowlist.

    Allowed option prefixes (``-C``, ``--literal-pathspecs``, formatting ``-c``) must not smuggle a network subcommand
    or an aliased push past the offline boundary; each is refused with the harness exit code and message.
    """
    completed = _run_wrapper(harness_git, *arguments)

    assert completed.returncode == _BLOCKED_EXIT
    assert "blocked by offline Codex harness" in completed.stderr
