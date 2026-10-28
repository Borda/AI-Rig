"""Exercise the offline harness Git boundary with real repository reads."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


@pytest.fixture
def harness_git(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Create the harness's actual Git wrapper around an isolated real repository."""
    real_git = shutil.which("git")
    assert real_git is not None
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    for arguments in (
        ["init", "-q"],
        ["config", "user.name", "Fixture"],
        ["config", "user.email", "fixture@example.invalid"],
    ):
        subprocess.run([real_git, *arguments], cwd=repository, env=environment, check=True, capture_output=True)
    (repository / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run([real_git, "add", "tracked.txt"], cwd=repository, env=environment, check=True)
    subprocess.run([real_git, "commit", "-qm", "fixture"], cwd=repository, env=environment, check=True)
    (repository / "tracked.txt").write_text("after\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("new\n", encoding="utf-8")

    source = (ROOT / ".github" / "codex-harness.sh").read_text(encoding="utf-8")
    wrapper = source.split('cat >"$TMP_BIN/git" <<EOF\n', 1)[1].split("\nEOF\n", 1)[0]
    wrapper = wrapper.replace("$REAL_GIT", Path(real_git).as_posix()).replace("\\$", "$")
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    git_wrapper = bin_directory / "git"
    git_wrapper.write_text(wrapper + "\n", encoding="utf-8", newline="\n")
    git_wrapper.chmod(0o755)
    environment["PATH"] = str(bin_directory) + os.pathsep + environment["PATH"]
    return repository, environment


@pytest.mark.skipif(BASH is None, reason="offline harness requires Bash")
@pytest.mark.integration
def test_offline_harness_reads_real_working_tree(harness_git: tuple[Path, dict[str, str]]) -> None:
    """Allow canonical diff and repository-root reads needed by calibration."""
    repository, environment = harness_git
    wrapper = Path(environment["PATH"].split(os.pathsep)[0]) / "git"
    outputs = []
    for arguments in (
        ["-c", "core.quotePath=true", "diff", "--binary", "HEAD"],
        ["-C", ".", "--literal-pathspecs", "rev-parse", "--show-toplevel"],
        ["-C", ".", "ls-files", "--others", "--exclude-standard"],
    ):
        completed = subprocess.run(
            [str(BASH), wrapper.as_posix(), *arguments],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout)
    assert "-before\n+after" in outputs[0]
    assert Path(outputs[1].strip()).resolve() == repository.resolve()
    assert outputs[2].splitlines() == ["untracked.txt"]


@pytest.mark.skipif(BASH is None, reason="offline harness requires Bash")
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
def test_offline_harness_still_blocks_network_git(
    harness_git: tuple[Path, dict[str, str]], arguments: list[str]
) -> None:
    """Keep network operations and arbitrary configuration outside the harness allowlist."""
    repository, environment = harness_git
    wrapper = Path(environment["PATH"].split(os.pathsep)[0]) / "git"
    completed = subprocess.run(
        [str(BASH), wrapper.as_posix(), *arguments],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 125
    assert "blocked by offline Codex harness" in completed.stderr
