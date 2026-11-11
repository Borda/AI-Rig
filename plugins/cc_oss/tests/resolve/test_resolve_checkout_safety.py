"""Check PR admission, read-only preflight, and cache-write safety before checkout."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest
import resolve_pr_refs as rpr
import resolve_preflight as rp


def _metadata(**overrides: object) -> dict:
    """Return an editable open fork targeting the origin repository."""
    return {
        "headRefName": "feature",
        "baseRefName": "main",
        "isCrossRepository": True,
        "headRefOid": "a" * 40,
        "state": "OPEN",
        "maintainerCanModify": True,
        "headRepositoryOwner": {"login": "contributor"},
        "headRepository": {"name": "project"},
        "url": "https://github.com/upstream/project/pull/12",
        **overrides,
    }


def _wire(
    run: mock.Mock,
    meta: dict,
    origin: str = "git@github.com:upstream/project.git",
    *,
    api_responses: dict[tuple[str, ...], str | Exception] | None = None,
) -> None:
    """Answer local refs, PR metadata and explicit API reads at the subprocess boundary."""
    answers = {
        ("git", "symbolic-ref", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("git", "remote", "get-url", "origin"): origin,
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): "feature",
        ("git", "rev-parse", "HEAD"): "a" * 40,
        **(api_responses or {}),
    }

    def reply(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Serve known local reads and the PR JSON payload."""
        text = json.dumps(meta) if args[:3] == ["gh", "pr", "view"] else answers.get(tuple(args), "")
        if isinstance(text, Exception):
            raise text
        return subprocess.CompletedProcess(args, 0 if text else 1, text, "")

    run.side_effect = reply


@pytest.mark.parametrize("state", ["CLOSED", "MERGED", ""])
@mock.patch.object(rpr.subprocess, "run", autospec=True)
def test_non_open_pr_is_blocked_before_sentinels(run: mock.Mock, tmp_sentinels: Path, state: str) -> None:
    """Only explicitly open PRs can enter the checkout pipeline."""
    _wire(run, _metadata(state=state))
    assert rpr.main(["--pr", "12"]) == 1
    assert list(tmp_sentinels.glob("resolve-*")) == []


@pytest.mark.parametrize(
    "permission",
    [
        pytest.param("false", id="no-push-access"),
        pytest.param("", id="lookup-failed"),
        pytest.param(subprocess.TimeoutExpired("gh", 7), id="lookup-timeout"),
        pytest.param(OSError("unavailable"), id="lookup-unavailable"),
    ],
)
@mock.patch.object(rpr.subprocess, "run", autospec=True)
def test_non_editable_fork_is_blocked(run: mock.Mock, tmp_sentinels: Path, permission: str | Exception) -> None:
    """A fork without verified push access is refused before checkout."""
    _wire(
        run,
        _metadata(maintainerCanModify=False),
        api_responses={("gh", "api", "repos/contributor/project", "--jq", ".permissions.push"): permission},
    )
    assert rpr.main(["--pr", "12"]) == 1
    assert list(tmp_sentinels.glob("resolve-*")) == []


@mock.patch.object(rpr.subprocess, "run", autospec=True)
def test_fork_with_push_access_is_admitted(run: mock.Mock, tmp_sentinels: Path) -> None:
    """The caller's push access admits a fork even when maintainer edits are disabled."""
    _wire(
        run,
        _metadata(maintainerCanModify=False),
        api_responses={("gh", "api", "repos/contributor/project", "--jq", ".permissions.push"): "true"},
    )
    assert rpr.main(["--pr", "12", "--timeout", "7"]) == 0
    assert (tmp_sentinels / "resolve-head-ref-shared").read_text(encoding="utf-8") == "feature\n"
    run.assert_any_call(
        ["gh", "api", "repos/contributor/project", "--jq", ".permissions.push"],
        capture_output=True,
        text=True,
        timeout=7,
        check=False,
    )


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:contributor/project.git",
        "",
        "https://github.com/upstream/other.git",
    ],
)
@mock.patch.object(rpr.subprocess, "run", autospec=True)
def test_origin_must_match_pr_base_repository(run: mock.Mock, tmp_sentinels: Path, origin: str) -> None:
    """The hardcoded origin merge base cannot refer to a fork or an unverifiable repository."""
    _wire(run, _metadata(), origin)
    assert rpr.main(["--pr", "12"]) == 1
    assert list(tmp_sentinels.glob("resolve-*")) == []


@pytest.mark.parametrize(
    ("origin", "canonical_name"),
    [
        pytest.param("git@github.com:upstream/project.git", "", id="scp"),
        pytest.param("https://github.com/upstream/project.git", "", id="https"),
        pytest.param("ssh://git@github.com/upstream/project.git", "", id="ssh"),
        pytest.param("ssh://git@ssh.github.com:443/upstream/project.git", "", id="ssh-firewall"),
        pytest.param("git@github-work:upstream/project.git", "", id="ssh-alias"),
        pytest.param("https://elsewhere.invalid/upstream/project", "", id="matching-name"),
        pytest.param("https://github.com/oldowner/project.git", "Upstream/Project", id="redirected"),
    ],
)
@mock.patch.object(rpr.subprocess, "run", autospec=True)
def test_matching_origin_is_admitted(run: mock.Mock, tmp_sentinels: Path, origin: str, canonical_name: str) -> None:
    """Aliases and redirected names identify the same origin/base repository."""
    _wire(
        run,
        _metadata(),
        origin,
        api_responses={("gh", "api", "repos/oldowner/project", "--jq", ".full_name"): canonical_name},
    )
    assert rpr.main(["--pr", "12", "--timeout", "7"]) == 0
    assert (tmp_sentinels / "resolve-base-ref-shared").read_text(encoding="utf-8") == "main\n"


@pytest.mark.parametrize(
    "canonical_name",
    [
        pytest.param("contributor/project", id="different-repository"),
        pytest.param("", id="lookup-failed"),
        pytest.param(subprocess.TimeoutExpired("gh", 7), id="lookup-timeout"),
        pytest.param(OSError("unavailable"), id="lookup-unavailable"),
    ],
)
@mock.patch.object(rpr.subprocess, "run", autospec=True)
def test_unverified_canonical_origin_is_blocked(
    run: mock.Mock, tmp_sentinels: Path, canonical_name: str | Exception
) -> None:
    """A mismatched or unavailable canonical name cannot publish checkout sentinels."""
    _wire(
        run,
        _metadata(),
        "https://github.com/oldowner/project.git",
        api_responses={("gh", "api", "repos/oldowner/project", "--jq", ".full_name"): canonical_name},
    )
    assert rpr.main(["--pr", "12", "--timeout", "7"]) == 1
    assert list(tmp_sentinels.glob("resolve-*")) == []
    run.assert_any_call(
        ["gh", "api", "repos/oldowner/project", "--jq", ".full_name"],
        capture_output=True,
        text=True,
        timeout=7,
        check=False,
    )


@mock.patch.object(rp.subprocess, "run", autospec=True)
def test_preflight_fetch_never_pulls_current_branch(run: mock.Mock) -> None:
    """An upstream-ahead response must never trigger a merge or pull during preflight."""
    run.return_value = subprocess.CompletedProcess([], 0, "ahead\n", "")
    assert rp._sync_git_state("/fake/git") is None
    assert [call.args[0][1:] for call in run.call_args_list] == [["remote", "-v"], ["fetch", "origin"]]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(1, id="fetch-rejected"),
        pytest.param(subprocess.TimeoutExpired("git", 30), id="fetch-timeout"),
        pytest.param(OSError("unavailable"), id="fetch-os-error"),
    ],
)
@mock.patch.object(rp.subprocess, "run", autospec=True)
def test_fetch_failure_blocks_preflight(run: mock.Mock, failure: int | Exception) -> None:
    """Failure to refresh origin cannot leave a successful preflight over stale base refs."""
    run.side_effect = [
        subprocess.CompletedProcess([], 0, "", ""),
        failure if isinstance(failure, Exception) else subprocess.CompletedProcess([], failure, "", ""),
    ]
    assert rp._sync_git_state("/fake/git") == 1


def test_cache_marker_symlink_does_not_overwrite_target(tmp_path: Path) -> None:
    """Recording a passed probe replaces the marker entry without following its symlink."""
    target = tmp_path / "keep.txt"
    target.write_text("keep", encoding="utf-8")
    marker = tmp_path / "gh.ok"
    marker.symlink_to(target)
    rp._preflight_pass("gh", tmp_path)
    assert target.read_text(encoding="utf-8") == "keep"
    assert not marker.is_symlink()
    assert rp._preflight_ok("gh", tmp_path)


@pytest.mark.parametrize("nested", [False, True])
def test_cache_symlinked_directory_is_ignored(tmp_path: Path, nested: bool) -> None:
    """Neither a symlinked cache directory nor a symlinked ancestor can redirect marker writes."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "state"
    link.symlink_to(outside, target_is_directory=True)
    directory = link / "preflight" if nested else link
    rp._preflight_pass("gh", directory)
    assert list(outside.iterdir()) == []
    assert not rp._preflight_ok("gh", directory)
