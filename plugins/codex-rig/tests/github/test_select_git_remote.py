"""Check local PR URL resolution before runtime-approved collection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SELECTOR = Path(__file__).resolve().parents[2] / "shared" / "select-git-remote.py"


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
def test_numeric_target_prefers_origin_over_configured_forks(tmp_path: Path) -> None:
    """Use the default repository for a bare PR number despite fork remotes."""
    repo = _repo_with_remotes(tmp_path, ["https://github.com/example/widget.git"])
    subprocess.run(["git", "-C", str(repo), "remote", "rename", "remote0", "origin"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "fork", "https://github.com/contributor/widget.git"],
        check=True,
    )

    result = _resolve(repo, "1510")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "https://github.com/example/widget/pull/1510\n"


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("origin_url", "git_args", "error"),
    [
        pytest.param(
            "https://gitlab.com/example/widget.git",
            ("remote", "add", "fork", "https://github.com/contributor/widget.git"),
            "no-github-origin",
            id="invalid-origin-with-one-fork",
        ),
        pytest.param(
            "https://github.com/example/widget.git",
            ("config", "--add", "remote.origin.url", "https://github.com/other/widget.git"),
            "ambiguous-github-origin",
            id="conflicting-origin-urls",
        ),
    ],
)
def test_numeric_target_rejects_unusable_origin(
    tmp_path: Path, origin_url: str, git_args: tuple[str, ...], error: str
) -> None:
    """Refuse a bare PR number when origin cannot give exactly one GitHub repository identity.

    Origin is renamed from the only configured remote and then changed by one more Git command: a fork remote must
    not silently take over from an invalid origin, and an origin carrying two different URLs is ambiguous.
    """
    repo = _repo_with_remotes(tmp_path, [origin_url])
    subprocess.run(["git", "-C", str(repo), "remote", "rename", "remote0", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo), *git_args], check=True)

    result = _resolve(repo, "1510")

    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr


@pytest.mark.installed_plugin
def test_explicit_pr_url_selects_named_fork_over_origin(tmp_path: Path) -> None:
    """Honor the requested repository when a full PR URL is supplied."""
    repo = _repo_with_remotes(tmp_path, ["https://github.com/example/widget.git"])
    subprocess.run(["git", "-C", str(repo), "remote", "rename", "remote0", "origin"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "fork", "https://github.com/contributor/widget.git"],
        check=True,
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SELECTOR),
            "--expected-url",
            "https://github.com/contributor/widget/pull/17",
            "--cwd",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["remote"] == "fork"


@pytest.mark.installed_plugin
@pytest.mark.parametrize(
    ("remote_urls", "target", "error"),
    [
        pytest.param(
            ["https://github.com/example/widget.git", "git@github.com:someone/widget.git"],
            "1510",
            "ambiguous-github-repositories",
            id="ambiguous-repositories-without-origin",
        ),
        pytest.param(["https://github.com/example/widget.git"], "0", "invalid-pr-number", id="zero-target"),
        pytest.param(["https://github.com/example/widget.git"], "-1", "invalid-pr-number", id="negative-target"),
        pytest.param(["https://github.com/example/widget.git"], "1/2", "invalid-pr-number", id="slash-target"),
        pytest.param(
            ["https://github.com/example/widget.git"],
            "https://github.com/example/widget/pull/1",
            "invalid-pr-number",
            id="url-target",
        ),
        pytest.param(
            ["https://gitlab.com/example/widget.git"], "1510", "no-github-repository", id="missing-github-remote"
        ),
        pytest.param(
            ["https://github.com/example/widget/extra.git"], "1510", "no-github-repository", id="malformed-remote-path"
        ),
        pytest.param(
            ["https://github.com:bad/example/widget.git"], "1510", "no-github-repository", id="invalid-url-port"
        ),
    ],
)
def test_canonicalization_rejects_unresolvable_input(
    tmp_path: Path, remote_urls: list[str], target: str, error: str
) -> None:
    """Never guess a repository or number: each unresolvable input exits non-zero with its named error.

    Covers a default that cannot be chosen when origin is absent and repositories conflict, the numeric-only preflight
    staying distinct from already validated URL input, and local Git configuration that cannot establish a repository
    (no GitHub remote, a truncated GitHub path, or a malformed URL port).
    """
    repo = _repo_with_remotes(tmp_path, remote_urls)

    result = _resolve(repo, target)

    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr


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
