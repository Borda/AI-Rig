"""Check local PR URL resolution before runtime-approved collection.

These tests run the shipped selector as a subprocess against real, throwaway Git repositories: the remote configuration
Git reports is the contract, so neither Git nor the selector is mocked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SELECTOR = Path(__file__).resolve().parents[2] / "shared" / "select-git-remote.py"
ORIGIN_URL = "https://github.com/example/widget.git"
FORK_URL = "https://github.com/contributor/widget.git"


def _git(repo: Path, *arguments: str) -> None:
    """Run one Git command inside the throwaway repository."""
    subprocess.run(["git", "-C", str(repo), *arguments], check=True)


def _repo_with_remotes(tmp_path: Path, urls: list[str]) -> Path:
    """Create an isolated Git repository with the requested fetch remotes named remote0, remote1, and so on."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for index, url in enumerate(urls):
        _git(tmp_path, "remote", "add", f"remote{index}", url)
    return tmp_path


def _repo_with_origin(tmp_path: Path, origin_url: str = ORIGIN_URL) -> Path:
    """Create an isolated Git repository whose only remote is named origin."""
    repo = _repo_with_remotes(tmp_path, [origin_url])
    _git(repo, "remote", "rename", "remote0", "origin")
    return repo


def _run_selector(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped selector exactly as a skill would before collection."""
    return subprocess.run(
        [sys.executable, str(SELECTOR), *arguments, "--cwd", str(repo)], capture_output=True, text=True, check=False
    )


def _resolve(repo: Path, target: str) -> subprocess.CompletedProcess[str]:
    """Canonicalize a PR target through the selector."""
    return _run_selector(repo, "--canonical-pr-url", target)


@pytest.mark.installed_plugin
@pytest.mark.parametrize("separator", ["\n", "\r", "\u0085", "\u2028"])
def test_remote_selection_preserves_configured_url_boundaries(tmp_path: Path, separator: str) -> None:
    """A single malformed origin URL cannot supply a repository, while a valid sibling remains selectable."""
    repo = _repo_with_origin(tmp_path, ORIGIN_URL + separator + "unfinished")
    _git(repo, "remote", "add", "fork", FORK_URL)

    numeric = _resolve(repo, "17")
    explicit = _run_selector(repo, "--expected-url", "https://github.com/example/widget/pull/17")
    sibling = _run_selector(repo, "--expected-url", "https://github.com/contributor/widget/pull/17")

    assert numeric.returncode != 0
    assert "no-github-origin" in numeric.stderr
    assert explicit.returncode != 0
    assert "no-matching-remote" in explicit.stderr
    assert sibling.returncode == 0, sibling.stderr
    assert json.loads(sibling.stdout)["remote"] == "fork"


@pytest.mark.installed_plugin
@pytest.mark.parametrize("suffix", [";archive", "?", "#", "/nested"])
def test_malformed_origin_path_cannot_select_the_pr_repository(tmp_path: Path, suffix: str) -> None:
    """Validate the whole configured URL before preferring origin over a valid named remote."""
    repo = _repo_with_origin(tmp_path, ORIGIN_URL + suffix)
    _git(repo, "remote", "add", "upstream", ORIGIN_URL)

    numeric = _resolve(repo, "17")
    explicit = _run_selector(repo, "--expected-url", "https://github.com/example/widget/pull/17")

    assert numeric.returncode != 0
    assert "no-github-origin" in numeric.stderr
    assert explicit.returncode == 0, explicit.stderr
    assert json.loads(explicit.stdout)["remote"] == "upstream"
    assert json.loads(explicit.stdout)["matching_remotes"] == [{"name": "upstream", "url": ORIGIN_URL}]


@pytest.mark.installed_plugin
@pytest.mark.parametrize("name", [".github", "_widget", "-widget"])
def test_literal_repository_names_resolve_in_both_selector_modes(tmp_path: Path, name: str) -> None:
    """Numeric and explicit PR selection both retain literal repository names with leading punctuation."""
    url = f"https://github.com/example/{name}.git"
    target = f"https://github.com/example/{name}/pull/17"
    repo = _repo_with_origin(tmp_path, url)

    numeric = _resolve(repo, "17")
    explicit = _run_selector(repo, "--expected-url", target)

    assert (numeric.returncode, explicit.returncode) == (0, 0)
    assert numeric.stdout == target + "\n"
    assert json.loads(explicit.stdout)["matching_remotes"] == [{"name": "origin", "url": url}]


@pytest.mark.installed_plugin
@pytest.mark.parametrize("name", [".", ".."])
def test_dot_path_components_cannot_identify_a_repository(tmp_path: Path, name: str) -> None:
    """A dot segment stays invalid even when a fetch URL adds the conventional Git suffix."""
    repo = _repo_with_origin(tmp_path, f"https://github.com/example/{name}.git")
    numeric = _resolve(repo, "17")
    explicit = _run_selector(repo, "--expected-url", f"https://github.com/example/{name}/pull/17")
    assert numeric.returncode != 0
    assert explicit.returncode != 0


@pytest.mark.installed_plugin
def test_explicit_generic_pr_target_still_selects_its_remote(tmp_path: Path) -> None:
    """Full PR URL matching remains available for a configured non-GitHub host."""
    repo = _repo_with_origin(tmp_path, "git@git.example.org:team/widget.git")
    result = _run_selector(repo, "--expected-url", "https://git.example.org/team/widget/pull/17")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["remote"] == "origin"


@pytest.mark.installed_plugin
def test_repeated_effective_fetch_urls_remain_distinct(tmp_path: Path) -> None:
    """Git can return the same effective fetch URL for two genuine configuration records."""
    repo = _repo_with_origin(tmp_path)
    _git(repo, "config", "--add", "remote.origin.url", "local-widget")
    _git(repo, "config", f"url.{ORIGIN_URL}.insteadOf", "local-widget")

    numeric = _resolve(repo, "17")
    explicit = _run_selector(repo, "--expected-url", "https://github.com/example/widget/pull/17")

    assert numeric.returncode == 0, numeric.stderr
    assert numeric.stdout == "https://github.com/example/widget/pull/17\n"
    assert explicit.returncode == 0, explicit.stderr
    assert json.loads(explicit.stdout)["matching_remotes"] == [{"name": "origin", "url": ORIGIN_URL}] * 2


@pytest.mark.installed_plugin
class TestCanonicalPrUrl:
    """Turning a bare PR number into a canonical URL from local Git configuration."""

    def test_binds_number_to_the_one_local_repository_regardless_of_remote_names(self, tmp_path: Path) -> None:
        """A numeric target resolves to the single GitHub repository even when two remotes spell it differently.

        An SSH and an HTTPS remote that differ only in case describe one repository, so neither is ambiguous.
        """
        repo = _repo_with_remotes(tmp_path, ["git@github.com:Example/Widget.git", ORIGIN_URL])

        result = _resolve(repo, "1510")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "https://github.com/example/widget/pull/1510\n"

    def test_prefers_origin_over_configured_forks(self, tmp_path: Path) -> None:
        """A bare PR number uses the default repository even when fork remotes are configured."""
        repo = _repo_with_origin(tmp_path)
        _git(repo, "remote", "add", "fork", FORK_URL)

        result = _resolve(repo, "1510")

        assert result.returncode == 0, result.stderr
        assert result.stdout == "https://github.com/example/widget/pull/1510\n"

    @pytest.mark.parametrize(
        ("origin_url", "git_args", "error"),
        [
            pytest.param(
                "https://gitlab.com/example/widget.git",
                ("remote", "add", "fork", FORK_URL),
                "no-github-origin",
                id="invalid-origin-with-one-fork",
            ),
            pytest.param(
                ORIGIN_URL,
                ("config", "--add", "remote.origin.url", "https://github.com/other/widget.git"),
                "ambiguous-github-origin",
                id="conflicting-origin-urls",
            ),
        ],
    )
    def test_rejects_origin_without_one_github_identity(
        self, tmp_path: Path, origin_url: str, git_args: tuple[str, ...], error: str
    ) -> None:
        """A bare PR number is refused when origin cannot give exactly one GitHub repository identity.

        A fork remote must not silently take over from an invalid origin, and an origin carrying two different URLs is
        ambiguous.
        """
        repo = _repo_with_origin(tmp_path, origin_url)
        _git(repo, *git_args)

        result = _resolve(repo, "1510")

        assert result.returncode != 0
        assert result.stdout == ""
        assert error in result.stderr

    @pytest.mark.parametrize(
        ("remote_urls", "target", "error"),
        [
            pytest.param(
                [ORIGIN_URL, "git@github.com:someone/widget.git"],
                "1510",
                "ambiguous-github-repositories",
                id="ambiguous-repositories-without-origin",
            ),
            pytest.param([ORIGIN_URL], "0", "invalid-pr-number", id="zero-target"),
            pytest.param([ORIGIN_URL], "-1", "invalid-pr-number", id="negative-target"),
            pytest.param([ORIGIN_URL], "1/2", "invalid-pr-number", id="slash-target"),
            pytest.param(
                [ORIGIN_URL], "https://github.com/example/widget/pull/1", "invalid-pr-number", id="url-target"
            ),
            pytest.param(
                ["https://gitlab.com/example/widget.git"], "1510", "no-github-repository", id="no-github-remote"
            ),
            pytest.param(
                ["https://github.com/example/widget/extra.git"], "1510", "no-github-repository", id="malformed-path"
            ),
            pytest.param(
                ["https://github.com:bad/example/widget.git"], "1510", "no-github-repository", id="invalid-url-port"
            ),
        ],
    )
    def test_rejects_unresolvable_input(self, tmp_path: Path, remote_urls: list[str], target: str, error: str) -> None:
        """Never guess a repository or number: each unresolvable input exits non-zero with its named error.

        Covers a default that cannot be chosen when origin is absent and repositories conflict, the numeric-only
        preflight staying distinct from already validated URL input, and local Git configuration that cannot establish a
        repository (no GitHub remote, a truncated GitHub path, or a malformed URL port).
        """
        repo = _repo_with_remotes(tmp_path, remote_urls)

        result = _resolve(repo, target)

        assert result.returncode != 0
        assert result.stdout == ""
        assert error in result.stderr


@pytest.mark.installed_plugin
class TestExpectedUrl:
    """The collector's existing remote-selection interface for a full PR URL."""

    def test_selects_named_fork_over_origin(self, tmp_path: Path) -> None:
        """The requested repository is honoured when a full PR URL names a fork."""
        repo = _repo_with_origin(tmp_path)
        _git(repo, "remote", "add", "fork", FORK_URL)

        result = _run_selector(repo, "--expected-url", "https://github.com/contributor/widget/pull/17")

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["remote"] == "fork"

    def test_keeps_json_output_shape(self, tmp_path: Path) -> None:
        """The selector still prints the expected host and repository plus the chosen remote as JSON.

        The collector parses this payload, so a changed key or shape breaks collection even though the selection itself
        is correct.
        """
        repo = _repo_with_remotes(tmp_path, [ORIGIN_URL])

        result = _run_selector(repo, "--expected-url", "https://github.com/example/widget/pull/1510")

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["expected"] == {"host": "github.com", "repository": "example/widget"}
        assert payload["remote"] == "remote0"
