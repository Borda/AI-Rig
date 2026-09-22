"""Check local PR URL resolution before runtime-approved collection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SELECTOR = Path(__file__).resolve().parents[1] / "shared" / "select-git-remote.py"


def _repo_with_remotes(tmp_path: Path, urls: list[str]) -> Path:
    """Create an isolated Git repository with the requested fetch remotes."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for index, url in enumerate(urls):
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", f"remote{index}", url], check=True)
    return tmp_path


def _resolve(repo: Path, target: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped selector exactly as a skill would before collection."""
    return subprocess.run(
        [sys.executable, str(SELECTOR), "--canonical-pr-url", target, "--cwd", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.installed_plugin
def test_numeric_target_becomes_canonical_url_from_one_repository(tmp_path: Path) -> None:
    """Bind a numeric target to the one local GitHub repository, regardless of remote names."""
    repo = _repo_with_remotes(
        tmp_path,
        ["git@github.com:Example/Widget.git", "https://github.com/example/widget.git"],
    )

    result = _resolve(repo, "1510")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://github.com/example/widget/pull/1510\n"


@pytest.mark.installed_plugin
def test_numeric_target_rejects_ambiguous_repositories(tmp_path: Path) -> None:
    """Do not bind the same PR number to either a base repository or a fork by guess."""
    repo = _repo_with_remotes(
        tmp_path,
        ["https://github.com/example/widget.git", "git@github.com:someone/widget.git"],
    )

    result = _resolve(repo, "1510")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "ambiguous-github-repositories" in result.stderr


@pytest.mark.installed_plugin
@pytest.mark.parametrize("target", ["0", "-1", "1/2", "https://github.com/example/widget/pull/1"])
def test_canonicalization_rejects_non_numeric_targets(tmp_path: Path, target: str) -> None:
    """Keep the numeric-only preflight distinct from already validated URL input."""
    repo = _repo_with_remotes(tmp_path, ["https://github.com/example/widget.git"])

    result = _resolve(repo, target)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "invalid-pr-number" in result.stderr


@pytest.mark.installed_plugin
def test_numeric_target_rejects_missing_github_remote(tmp_path: Path) -> None:
    """Do not invent a repository when local Git configuration cannot establish one."""
    repo = _repo_with_remotes(tmp_path, ["https://gitlab.com/example/widget.git"])

    result = _resolve(repo, "1510")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "no-github-repository" in result.stderr


@pytest.mark.installed_plugin
def test_numeric_target_ignores_malformed_remote_instead_of_guessing(tmp_path: Path) -> None:
    """Reject a truncated GitHub path that would otherwise look like a valid base repository."""
    repo = _repo_with_remotes(tmp_path, ["https://github.com/example/widget/extra.git"])

    result = _resolve(repo, "1510")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "no-github-repository" in result.stderr


@pytest.mark.installed_plugin
def test_numeric_target_rejects_invalid_url_port(tmp_path: Path) -> None:
    """A malformed GitHub URL cannot establish the repository identity."""
    repo = _repo_with_remotes(tmp_path, ["https://github.com:bad/example/widget.git"])

    result = _resolve(repo, "1510")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "no-github-repository" in result.stderr


@pytest.mark.installed_plugin
def test_existing_expected_url_selector_keeps_json_output(tmp_path: Path) -> None:
    """Keep the collector's existing remote-selection interface unchanged."""
    repo = _repo_with_remotes(tmp_path, ["https://github.com/example/widget.git"])

    result = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--expected-url",
            "https://github.com/example/widget/pull/1510",
            "--cwd",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["expected"] == {"host": "github.com", "repository": "example/widget"}
    assert payload["remote"] == "remote0"
