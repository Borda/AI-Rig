"""Portable acceptance checks for authoritative PR evidence collection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = PLUGIN_ROOT / "shared" / "collect_pr.py"
BASE_OID = "a" * 40
HEAD_OID = "b" * 40


def _load_collector() -> ModuleType:
    """Load the standalone collector without requiring package installation."""
    assert COLLECTOR.is_file(), COLLECTOR
    specification = importlib.util.spec_from_file_location("codex_rig_collect_pr", COLLECTOR)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.path.insert(0, str(COLLECTOR.parent))
    try:
        specification.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _pr_payload(*, state: str = "OPEN", cross_repository: bool = False) -> dict[str, Any]:
    """Return one complete same-repository PR metadata fixture.

    Example:
        >>> _pr_payload(state="MERGED")["state"]
        'MERGED'
    """
    return {
        "number": 17,
        "title": "Portable collector",
        "body": "Fix checkpoint resume behavior described by the contributor.",
        "url": "https://github.com/Borda/AI-Rig/pull/17",
        "author": {"login": "contributor"},
        "baseRefName": "main",
        "baseRefOid": BASE_OID,
        "headRefName": "portable-pr",
        "headRefOid": HEAD_OID,
        "headRepository": {"nameWithOwner": "contributor/AI-Rig" if cross_repository else "Borda/AI-Rig"},
        "headRepositoryOwner": {"login": "contributor" if cross_repository else "Borda"},
        "isCrossRepository": cross_repository,
        "state": state,
        "isDraft": False,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergeable": "MERGEABLE",
        "comments": [{"id": "comment-1"}],
        "reviews": [{"id": "review-1"}],
        "files": [{"path": "b.py"}, {"path": "a.py"}],
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
        ],
    }


def _public_pr_payload() -> dict[str, Any]:
    """Return the public REST representation available without GitHub CLI access.

    Example:
        >>> _public_pr_payload()["number"]
        17
    """
    return {
        "number": 17,
        "title": "Portable collector",
        "body": "Fix checkpoint resume behavior described by the contributor.",
        "html_url": "https://github.com/Borda/AI-Rig/pull/17",
        "user": {"login": "contributor"},
        "base": {"ref": "main", "sha": BASE_OID, "repo": {"full_name": "Borda/AI-Rig"}},
        "head": {
            "ref": "portable-pr",
            "sha": HEAD_OID,
            "repo": {"full_name": "Borda/AI-Rig", "owner": {"login": "Borda"}},
        },
        "state": "open",
        "draft": False,
    }


def _threads_payload(*, paginated: bool = False) -> dict[str, Any]:
    """Return one GraphQL review-thread response fixture.

    Example:
        >>> len(_threads_payload()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"])
        3
    """
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": paginated, "endCursor": "cursor"},
                        "nodes": [
                            {"id": "resolved", "isResolved": True, "isOutdated": False, "comments": {"nodes": []}},
                            {
                                "id": "active",
                                "isResolved": False,
                                "isOutdated": False,
                                "comments": {"nodes": []},
                            },
                            {
                                "id": "outdated",
                                "isResolved": False,
                                "isOutdated": True,
                                "comments": {"nodes": []},
                            },
                        ],
                    }
                }
            }
        }
    }


class FakeRunner:
    """Return deterministic process results while recording exact argv calls."""

    def __init__(
        self,
        *,
        paginated: bool = False,
        statistics_unavailable: bool = False,
        current_base_oid: str = BASE_OID,
        recorded_base_is_ancestor: bool = True,
        pr_state: str = "OPEN",
        named_head_ref_missing: bool = False,
        review_threads_failure: bool = False,
        current_head_oid: str = "d" * 40,
        cross_repository: bool = False,
        github_remotes: dict[str, list[str]] | None = None,
        dirty_paths: list[str] | None = None,
        checkout_paths: list[str] | None = None,
    ) -> None:
        """Initialize configurable process and GitHub-response fixtures."""
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.paginated = paginated
        self.statistics_unavailable = statistics_unavailable
        self.current_base_oid = current_base_oid
        self.recorded_base_is_ancestor = recorded_base_is_ancestor
        self.pr_state = pr_state
        self.named_head_ref_missing = named_head_ref_missing
        self.review_threads_failure = review_threads_failure
        self.local_head_oid = current_head_oid
        self.cross_repository = cross_repository
        self.github_remotes = github_remotes or {"origin": ["https://github.com/Borda/AI-Rig.git"]}
        self.dirty_paths = dirty_paths or []
        self.checkout_paths = checkout_paths or ["a.py"]

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Simulate the Git, GitHub CLI, and remote-selector commands."""
        assert isinstance(argv, list)
        assert kwargs.get("shell", False) is False
        self.calls.append((argv, kwargs))
        stdout = b""
        if argv == ["git", "diff", "--name-only", "-z", "HEAD", "--"]:
            stdout = b"".join(f"{path}\0".encode() for path in self.dirty_paths)
        elif argv[:4] == ["git", "diff", "--name-only", "-z"]:
            stdout = b"".join(f"{path}\0".encode() for path in self.checkout_paths)
        elif argv[:4] == ["git", "status", "--short", "--untracked-files=no"]:
            stdout = b""
        elif argv[:3] == ["git", "status", "--short"]:
            stdout = b" M local.txt\n"
        elif argv == ["git", "remote"]:
            stdout = "".join(f"{remote}\n" for remote in self.github_remotes).encode()
        elif argv[:4] == ["git", "remote", "get-url", "--all"]:
            remote = argv[4]
            if remote not in self.github_remotes:
                return subprocess.CompletedProcess(argv, 2, stdout=b"", stderr=b"unknown remote")
            stdout = "".join(f"{url}\n" for url in self.github_remotes[remote]).encode()
        elif argv[:3] == ["gh", "pr", "view"]:
            stdout = json.dumps(_pr_payload(state=self.pr_state, cross_repository=self.cross_repository)).encode()
        elif argv[:3] == ["gh", "api", "graphql"]:
            if self.review_threads_failure:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"connection reset by peer")
            stdout = json.dumps(_threads_payload(paginated=self.paginated)).encode()
        elif argv[:3] == ["gh", "pr", "diff"]:
            stdout = b"diff --git a/a.py b/a.py\n"
        elif argv[:2] == ["git", "diff"] and "--binary" in argv:
            stdout = b"diff --git a/a.py b/a.py\n"
        elif argv[:2] == ["git", "diff"] and "--stat" in argv:
            if self.statistics_unavailable:
                return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"binary patch has no stat\n")
            stdout = b" a.py | 1 +\n"
        elif argv[:2] == ["git", "diff"] and "--numstat" in argv:
            if self.statistics_unavailable:
                return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"binary patch has no numstat\n")
            stdout = b"1\t0\ta.py\n"
        elif argv[:2] == ["git", "apply"] and "--stat" in argv:
            if self.statistics_unavailable:
                return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"binary patch has no stat\n")
            stdout = b" a.py | 1 +\n"
        elif argv[:2] == ["git", "apply"] and "--numstat" in argv:
            if self.statistics_unavailable:
                return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"binary patch has no numstat\n")
            stdout = b"1\t0\ta.py\n"
        elif argv[0] == sys.executable and argv[1].endswith("select-git-remote.py"):
            if "--identity-only" in argv:
                stdout = b'{"repository":"Borda/AI-Rig","host":"github.com"}\n'
            else:
                stdout = b'{"remote":"origin","remote_url":"https://github.com/Borda/AI-Rig.git"}\n'
        elif argv[:3] == ["git", "fetch", "--no-tags"]:
            if self.named_head_ref_missing and "portable-pr" in argv[-1]:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"missing branch")
            stdout = b""
        elif argv[:2] == ["git", "rev-parse"]:
            reference = argv[2]
            stdout = (
                HEAD_OID if "portable-pr" in reference or "/pull/" in reference else self.current_base_oid
            ).encode() + b"\n"
            if reference == "HEAD":
                stdout = f"{self.local_head_oid}\n".encode()
        elif argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(
                argv,
                0 if self.recorded_base_is_ancestor else 1,
                stdout=b"",
                stderr=b"",
            )
        elif argv[:3] == ["gh", "pr", "checkout"]:
            self.local_head_oid = HEAD_OID
            stdout = b"checked out\n"
        elif argv[:3] == ["git", "checkout", "--detach"]:
            self.local_head_oid = HEAD_OID
            stdout = b"checked out\n"
        elif argv[:3] == ["git", "branch", "--show-current"]:
            stdout = b"portable-pr\n"
        else:  # pragma: no cover - makes unexpected production commands diagnostic
            raise AssertionError(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")


def _configure_collector(monkeypatch: pytest.MonkeyPatch, module: ModuleType, runner: FakeRunner) -> None:
    """Install deterministic command discovery and execution boundaries."""
    monkeypatch.setattr(module.shutil, "which", lambda command: f"/fixture/{command}")
    monkeypatch.setattr(module.subprocess, "run", runner)


def test_collect_pr_writes_complete_noncheckout_artifact_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Collect online review evidence with argv-only subprocess execution."""
    module = _load_collector()
    _runner = FakeRunner()
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5)

    assert result == 0, (output / "pr-error.txt").read_text() if (output / "pr-error.txt").exists() else ""
    assert {path.name for path in output.iterdir()} == {
        "comments.json",
        "diff.patch",
        "diffstat.txt",
        "files.txt",
        "numstat.txt",
        "online-review-summary.json",
        "pr-target.txt",
        "pr-routing.json",
        "pr.json",
        "review-threads.json",
        "review-threads.raw.json",
        "reviews.json",
        "status.txt",
        "unresolved-review-threads.json",
        "untracked.txt",
    }
    assert (output / "pr-target.txt").read_text(encoding="utf-8") == "17\n"
    assert (output / "files.txt").read_text(encoding="utf-8") == "a.py\nb.py\n"
    assert json.loads((output / "online-review-summary.json").read_text()) == {
        "pr_metadata_transport": "gh",
        "limited_data": False,
        "unavailable_evidence": [],
        "review_threads_status": "available",
        "review_threads_error": None,
        "review_thread_count": 3,
        "unresolved_review_thread_count": 2,
        "active_unresolved_review_thread_count": 1,
        "outdated_unresolved_review_thread_count": 1,
        "top_level_comment_count": 1,
        "review_count": 1,
    }
    routing = json.loads((output / "pr-routing.json").read_text())
    assert routing["base_repo"] == "Borda/AI-Rig"
    assert routing["same_repo"] is True
    assert routing["local_checkout_command"] == "gh pr checkout 17"
    collected_pr = json.loads((output / "pr.json").read_text())
    assert collected_pr["body"].startswith("Fix checkpoint")
    assert collected_pr["statusCheckRollup"] == [
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    assert "statusCheckRollup" in module.PR_FIELDS
    assert all(call[1]["timeout"] == 5 for call in _runner.calls)


def test_collect_pr_degrades_incomplete_review_thread_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep source review available while recording incomplete supplemental thread evidence."""
    module = _load_collector()
    _runner = FakeRunner(paginated=True)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="", output=output, checkout=False, timeout_seconds=5)

    assert result == 0
    assert not (output / "pr-error.txt").exists()
    assert (output / "review-threads-error.txt").read_text(encoding="utf-8") == (
        "review-thread-pagination-incomplete\n"
    )
    summary = json.loads((output / "online-review-summary.json").read_text())
    assert summary["review_threads_status"] == "unavailable"
    assert summary["review_threads_error"] == "review-thread-pagination-incomplete"
    assert json.loads((output / "review-threads.json").read_text()) == []


def test_collect_pr_does_not_require_fallback_identity_when_primary_gh_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the authenticated primary path independent of fallback-only remote trust checks."""
    module = _load_collector()
    _runner = FakeRunner(github_remotes={"origin": ["https://github.com/another/project.git"]})
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(
        target="https://github.com/Borda/AI-Rig/pull/17",
        output=output,
        checkout=False,
        timeout_seconds=5,
    )

    assert result == 0, (output / "pr-error.txt").read_text() if (output / "pr-error.txt").exists() else ""
    assert json.loads((output / "online-review-summary.json").read_text())["pr_metadata_transport"] == "gh"


def test_public_pr_metadata_normalizes_an_absent_optional_body() -> None:
    """Keep public PRs without a description eligible for limited fallback review."""
    module = _load_collector()
    payload = _public_pr_payload()
    payload["body"] = None

    normalized = module._normalized_public_pr(payload, module.PRTarget("Borda", "AI-Rig", 17))

    assert normalized["body"] == ""


@pytest.mark.parametrize(
    "target",
    ["https://github.com/Borda/AI-Rig/pull/17", "https://github.com/borda/ai-rig/pull/17", "17"],
)
@pytest.mark.parametrize(
    ("failure_kind", "stderr"),
    [
        pytest.param("network", b"connection reset by peer", id="network"),
        pytest.param("auth", b"authentication required", id="auth"),
        pytest.param("rate-limit", b"rate limit exceeded", id="rate-limit"),
        pytest.param("timeout", None, id="timeout"),
    ],
)
def test_collect_pr_uses_public_metadata_fallback_with_trusted_pr_identity(
    target: str,
    failure_kind: str,
    stderr: bytes | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep public PR context available only after canonical or uniquely configured identity verification."""
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    public_requests: list[str] = []
    _runner = FakeRunner()

    def _unavailable_gh(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail authenticated PR reads while preserving unrelated fake commands."""
        if argv[:3] in (["gh", "pr", "view"], ["gh", "api", "graphql"]):
            if failure_kind == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            assert stderr is not None
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=stderr)
        return _runner(argv, **kwargs)

    def _public_get(url: str, **kwargs: Any) -> bytes:
        """Record and satisfy the public PR metadata fallback request."""
        public_requests.append(url)
        return json.dumps(_public_pr_payload()).encode()

    _configure_collector(monkeypatch, module, _unavailable_gh)
    monkeypatch.setattr(reader, "public_github_get", _public_get)

    result = module.collect_pr(
        target=target,
        output=output,
        checkout=True,
        timeout_seconds=5,
    )

    assert result == 0, (output / "pr-error.txt").read_text() if (output / "pr-error.txt").exists() else ""
    assert public_requests == ["https://api.github.com/repos/Borda/AI-Rig/pulls/17"]
    assert any(
        argv == ["git", "fetch", "--no-tags", "origin", "refs/pull/17/head:refs/remotes/origin/pull/17/head"]
        for argv, _ in _runner.calls
    )
    assert any(argv == ["git", "checkout", "--detach", "refs/remotes/origin/pull/17/head"] for argv, _ in _runner.calls)
    assert json.loads((output / "pr.json").read_text(encoding="utf-8"))["number"] == 17
    summary = json.loads((output / "online-review-summary.json").read_text(encoding="utf-8"))
    assert summary["pr_metadata_transport"] == "public-https-fallback"
    assert summary["limited_data"] is True
    assert summary["unavailable_evidence"] == [
        "github_provided_file_list",
        "mergeability",
        "review_decision",
        "reviews",
        "top_level_comments",
    ]
    assert summary["review_threads_status"] == "unavailable"
    assert json.loads((output / "pr-routing.json").read_text(encoding="utf-8"))["pr_metadata_transport"] == (
        "public-https-fallback"
    )
    checkout = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    assert checkout["diff_source"] == "verified-local-checkout"
    assert checkout["head_matches_pr"] is True
    assert (output / "diff.patch").read_bytes() == b"diff --git a/a.py b/a.py\n"


@pytest.mark.parametrize(
    ("target", "stderr", "expected_error", "github_remotes"),
    [
        pytest.param(
            "17",
            b"connection reset by peer",
            "github-network:gh-pr-view",
            {
                "origin": ["https://github.com/Borda/AI-Rig.git"],
                "fork": ["https://github.com/contributor/AI-Rig.git"],
            },
            id="ambiguous-configured-remotes",
        ),
        pytest.param(
            "https://github.com/Borda/AI-Rig/pull/17",
            b"resource not accessible",
            "github-permission:gh-pr-view",
            {"origin": ["https://github.com/Borda/AI-Rig.git"]},
            id="permission-denied",
        ),
    ],
)
def test_collect_pr_keeps_ambiguous_or_permission_limited_metadata_fail_closed(
    target: str,
    stderr: bytes,
    expected_error: str,
    github_remotes: dict[str, list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not infer public identity or bypass GitHub's authenticated permission decision."""
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    _runner = FakeRunner(github_remotes=github_remotes)

    def _unavailable_gh(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return the configured authenticated PR failure and delegate other commands."""
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=stderr)
        return _runner(argv, **kwargs)

    def _public_get(*args: Any, **kwargs: Any) -> bytes:
        """Fail if an ambiguous authenticated result activates public fallback."""
        raise AssertionError("ambiguous selectors and permission failures must not activate public HTTPS fallback")

    _configure_collector(monkeypatch, module, _unavailable_gh)
    monkeypatch.setattr(reader, "public_github_get", _public_get)

    assert module.collect_pr(target=target, output=output, checkout=False, timeout_seconds=5) == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == f"{expected_error}\n"


def test_collect_pr_fails_closed_when_public_fallback_cannot_read_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not emit partial PR evidence when a canonical URL is private or unavailable publicly."""
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    _runner = FakeRunner()

    def _unavailable_gh(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return an unavailable authenticated PR response for fallback testing."""
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"connection reset by peer")
        return _runner(argv, **kwargs)

    def _public_get(*args: Any, **kwargs: Any) -> bytes:
        """Return the expected public-read failure without creating partial evidence."""
        raise reader.GitHubReadError("github-not-found:gh-pr-view")

    _configure_collector(monkeypatch, module, _unavailable_gh)
    monkeypatch.setattr(reader, "public_github_get", _public_get)

    assert (
        module.collect_pr(
            target="https://github.com/Borda/AI-Rig/pull/17",
            output=output,
            checkout=True,
            timeout_seconds=5,
        )
        == 2
    )
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "github-not-found:gh-pr-view\n"
    assert not (output / "pr.json").exists()


def test_collect_pr_keeps_graphql_missing_pr_out_of_public_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report a missing PR directly instead of retrying it as a network failure."""
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    _runner = FakeRunner()

    def _missing_pr(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return GitHub's semantic missing-PR response and delegate other commands."""
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout=b"",
                stderr=b"GraphQL: Could not resolve to a PullRequest with the number of 17.",
            )
        return _runner(argv, **kwargs)

    def _public_get(*args: Any, **kwargs: Any) -> bytes:
        """Fail if a semantic not-found response activates public fallback."""
        raise AssertionError("a semantic not-found response must not activate public fallback")

    _configure_collector(monkeypatch, module, _missing_pr)
    monkeypatch.setattr(reader, "public_github_get", _public_get)

    assert module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5) == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "github-not-found:gh-pr-view\n"
    assert not (output / "pr.json").exists()


def test_collect_pr_rejects_canonical_url_without_matching_configured_github_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a public URL from authorizing a different local repository's checkout evidence."""
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    _runner = FakeRunner(github_remotes={"origin": ["https://github.com/Borda/other-repository.git"]})

    def _unavailable_gh(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a network failure for authenticated metadata and delegate other commands."""
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"connection reset by peer")
        return _runner(argv, **kwargs)

    def _public_get(*args: Any, **kwargs: Any) -> bytes:
        """Fail if a remote URL without a matching local remote requests metadata."""
        raise AssertionError("a URL without matching configured remote must not request public metadata")

    _configure_collector(monkeypatch, module, _unavailable_gh)
    monkeypatch.setattr(reader, "public_github_get", _public_get)

    assert (
        module.collect_pr(
            target="https://github.com/Borda/AI-Rig/pull/17",
            output=output,
            checkout=True,
            timeout_seconds=5,
        )
        == 2
    )
    assert not (output / "pr.json").exists()


@pytest.mark.parametrize(
    "target",
    ["https://github.com/Borda/AI-Rig/pull/17?access_token=secret", "https://token@github.com/Borda/AI-Rig/pull/17"],
)
def test_collect_pr_rejects_unsafe_public_target_without_persisting_it(
    target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep query credentials and userinfo out of PR evidence artifacts and fallback requests."""
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"

    def _fail_if_called(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail if unsafe target input reaches command execution."""
        raise AssertionError(f"unsafe public target must be rejected before command execution: {argv}")

    def _public_get(*args: Any, **kwargs: Any) -> bytes:
        """Fail if unsafe target input reaches the public HTTPS fallback."""
        raise AssertionError("unsafe public target must not activate public HTTPS fallback")

    _configure_collector(monkeypatch, module, _fail_if_called)
    monkeypatch.setattr(reader, "public_github_get", _public_get)

    assert module.collect_pr(target=target, output=output, checkout=True, timeout_seconds=5) == 2
    target_artifact = output / "pr-target.txt"
    persisted_target = target_artifact.read_text(encoding="utf-8") if target_artifact.exists() else ""
    assert "secret" not in persisted_target
    assert "token@" not in persisted_target


def test_collect_pr_records_unavailable_diff_statistics_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep binary or rename-only PR evidence when derived statistics are unsupported."""
    module = _load_collector()
    _runner = FakeRunner(statistics_unavailable=True)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    assert (output / "diff.patch").read_bytes() == b"diff --git a/a.py b/a.py\n"
    assert (output / "diffstat.txt").read_text(encoding="utf-8") == "unavailable:command-failed:diff-stat\n"
    assert (output / "numstat.txt").read_text(encoding="utf-8") == "unavailable:command-failed:diff-numstat\n"
    assert json.loads((output / "local-checkout.json").read_text())["head_matches_pr"] is True
    assert not (output / "pr-error.txt").exists()


def test_collect_pr_uses_verified_local_diff_when_review_thread_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover a fork-style PR from supplemental integration failure through exact local source."""
    module = _load_collector()
    _runner = FakeRunner(review_threads_failure=True, cross_repository=True)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0, (output / "pr-error.txt").read_text() if (output / "pr-error.txt").exists() else ""
    assert json.loads((output / "pr.json").read_text())["body"].startswith("Fix checkpoint")
    assert (output / "review-threads-error.txt").read_text() == "github-network:gh-review-threads\n"
    assert (output / "diff.patch").read_bytes() == b"diff --git a/a.py b/a.py\n"
    assert any(argv == ["gh", "pr", "checkout", "17"] for argv, _ in _runner.calls)
    assert any(argv == ["git", "diff", "--binary", f"{BASE_OID}...{HEAD_OID}", "--"] for argv, _ in _runner.calls)
    assert not any(argv[:3] == ["gh", "pr", "diff"] for argv, _ in _runner.calls)
    checkout = json.loads((output / "local-checkout.json").read_text())
    assert checkout["diff_source"] == "verified-local-checkout"
    assert checkout["diff_base_oid"] == BASE_OID
    assert checkout["diff_head_oid"] == HEAD_OID


def test_collect_pr_reuses_already_exact_pr_head_for_local_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Avoid a fragile checkout call when the current fork branch already matches PR metadata."""
    module = _load_collector()
    _runner = FakeRunner(current_head_oid=HEAD_OID, cross_repository=True)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    assert not any(argv[:3] == ["gh", "pr", "checkout"] for argv, _ in _runner.calls)
    checkout = json.loads((output / "local-checkout.json").read_text())
    assert checkout["command"] == "not-run: already at expected PR head"
    assert checkout["head_matches_pr"] is True
    assert (output / "diff.patch").read_bytes() == b"diff --git a/a.py b/a.py\n"


def test_collect_pr_allows_dirty_paths_untouched_by_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unrelated tracked environment churn from blocking a verified PR checkout."""
    module = _load_collector()
    _runner = FakeRunner(dirty_paths=["uv.lock"], checkout_paths=["a.py"])
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    preflight = json.loads((output / "worktree-preflight.json").read_text(encoding="utf-8"))
    assert preflight["status"] == "safe-unrelated-dirty-paths"
    assert preflight["dirty_paths"] == ["uv.lock"]
    assert preflight["checkout_paths"] == ["a.py"]
    assert preflight["overlapping_paths"] == []
    assert any(argv == ["gh", "pr", "checkout", "17"] for argv, _ in _runner.calls)


def test_collect_pr_names_dirty_paths_that_checkout_would_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Block only tracked edits the requested checkout would overwrite."""
    module = _load_collector()
    _runner = FakeRunner(dirty_paths=["uv.lock", "a.py"], checkout_paths=["a.py", "src/app.py"])
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 2
    assert (output / "pr-error.txt").read_text(
        encoding="utf-8"
    ) == "dirty-tracked-worktree-overlap-before-pr-checkout\n"
    preflight = json.loads((output / "worktree-preflight.json").read_text(encoding="utf-8"))
    assert preflight["status"] == "blocked-overlapping-dirty-paths"
    assert preflight["overlapping_paths"] == ["a.py"]
    assert not any(argv[:3] == ["gh", "pr", "checkout"] for argv, _ in _runner.calls)


def test_collect_pr_allows_dirty_paths_when_already_at_pr_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not block a source review when no checkout is necessary."""
    module = _load_collector()
    _runner = FakeRunner(current_head_oid=HEAD_OID, cross_repository=True, dirty_paths=["uv.lock"])
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    preflight = json.loads((output / "worktree-preflight.json").read_text(encoding="utf-8"))
    assert preflight["status"] == "already-at-pr-head"
    assert preflight["dirty_paths"] == ["uv.lock"]
    assert preflight["checkout_paths"] == []
    assert not any(argv[:3] == ["gh", "pr", "checkout"] for argv, _ in _runner.calls)


def test_collect_pr_checkout_writes_verified_fetch_and_checkout_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bind target refresh and local checkout to PR metadata OIDs without force."""
    module = _load_collector()
    _runner = FakeRunner()
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0, (output / "pr-error.txt").read_text() if (output / "pr-error.txt").exists() else ""
    target = json.loads((output / "target-branch.json").read_text())
    assert target["local_head"] == BASE_OID
    assert target["base_matches_pr_metadata"] is True
    assert target["expected_base_is_ancestor"] is True
    assert target["base_relation"] == "matches-pr-metadata"
    head = json.loads((output / "pr-head-fetch.json").read_text())
    assert head["local_head"] == HEAD_OID
    assert head["head_matches_pr_metadata"] is True
    checkout = json.loads((output / "local-checkout.json").read_text())
    assert checkout["local_head"] == HEAD_OID
    assert checkout["head_matches_pr"] is True
    assert checkout["command"] == "gh pr checkout 17"
    assert checkout["diff_source"] == "verified-local-checkout"
    assert "no --force was used" in checkout["force_policy"]
    assert all("--force" not in argument for argv, _ in _runner.calls for argument in argv)


def test_collect_pr_records_target_branch_divergence_without_rejecting_verified_pr_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Allow historical target divergence while still requiring the exact PR head."""
    module = _load_collector()
    _runner = FakeRunner(current_base_oid="c" * 40, recorded_base_is_ancestor=False, pr_state="MERGED")
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    target = json.loads((output / "target-branch.json").read_text())
    assert target["local_head"] == "c" * 40
    assert target["expected_base_oid"] == BASE_OID
    assert target["base_matches_pr_metadata"] is False
    assert target["expected_base_is_ancestor"] is False
    assert target["base_relation"] == "diverged"
    assert json.loads((output / "local-checkout.json").read_text())["head_matches_pr"] is True


def test_collect_pr_accepts_open_pr_when_target_advanced_from_recorded_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep reviewing an exact PR head after its target branch advances."""
    module = _load_collector()
    _runner = FakeRunner(current_base_oid="c" * 40)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0, (output / "pr-error.txt").read_text() if (output / "pr-error.txt").exists() else ""
    target = json.loads((output / "target-branch.json").read_text())
    assert target["base_relation"] == "advanced"
    assert target["expected_base_is_ancestor"] is True
    assert json.loads((output / "local-checkout.json").read_text())["head_matches_pr"] is True


def test_collect_pr_rejects_open_pr_when_target_diverged_from_recorded_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when the fetched target no longer descends from the recorded base."""
    module = _load_collector()
    _runner = FakeRunner(current_base_oid="c" * 40, recorded_base_is_ancestor=False)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 2
    assert (output / "pr-error.txt").read_text().startswith("target-branch-diverged:")


def test_git_ancestry_probe_rejects_unverifiable_commits() -> None:
    """Keep a Git ancestry error distinct from a proven target divergence."""
    module = _load_collector()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return an ancestry command failure for the targeted probe."""
        return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"unknown revision")

    with pytest.raises(module.CollectionError, match="command-failed:target-branch-ancestry") as error:
        module._git_is_ancestor(_runner, 5, BASE_OID, "c" * 40)

    assert error.value.diagnostics == {
        "exit_code": 128,
        "failure_class": "command-failed",
        "label": "target-branch-ancestry",
    }


def test_collect_pr_rejects_unknown_pr_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed instead of treating an unrecognized GitHub state as historical evidence."""
    module = _load_collector()
    _runner = FakeRunner(pr_state="UNKNOWN")
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "unsupported-pr-state\n"


def test_collect_pr_preserves_checkout_started_state_after_checkout_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain conservative local-state evidence if checkout may already have changed files."""
    module = _load_collector()
    success_runner = FakeRunner()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail checkout while delegating all metadata commands to the successful runner."""
        if argv[:3] == ["gh", "pr", "checkout"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"checkout failed")
        return success_runner(argv, **kwargs)

    _configure_collector(monkeypatch, module, success_runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5, run=_runner)

    assert result == 2
    assert json.loads((output / "checkout-state.json").read_text()) == {
        "status": "checkout-command-started",
        "local_state": "changed-or-unknown",
    }
    assert (output / "pr.json").is_file()
    assert (output / "comments.json").is_file()
    assert (output / "review-threads.json").is_file()


def test_collect_pr_checks_out_merged_pr_when_its_named_head_branch_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use GitHub's pull ref and exact SHA check when a historical source branch is deleted."""
    module = _load_collector()
    _runner = FakeRunner(pr_state="MERGED", named_head_ref_missing=True)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    routing = json.loads((output / "pr-routing.json").read_text())
    assert routing["pr_state"] == "MERGED"
    head_fetch = json.loads((output / "pr-head-fetch.json").read_text())
    assert head_fetch["status"] == "fetched"
    checkout = json.loads((output / "local-checkout.json").read_text())
    assert checkout["head_matches_pr"] is True
    assert checkout["command"] == "git checkout --detach refs/remotes/origin/pull/17/head"
    assert not any(argv[:3] == ["git", "fetch", "--no-tags"] and "portable-pr" in argv[-1] for argv, _ in _runner.calls)


def test_collect_pr_timeout_is_bounded_and_records_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Convert a stalled external command into the stable collection failure contract."""
    module = _load_collector()
    monkeypatch.setattr(module.shutil, "which", lambda command: f"/fixture/{command}")

    def _timeout(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Timeout the authenticated PR read while returning success for other commands."""
        if argv[:3] == ["gh", "pr", "view"]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", _timeout)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=1)

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "command-timeout:gh-pr-view\n"


def test_collect_pr_removes_terminal_failure_markers_after_successful_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure a successful retry cannot be mistaken for the previous unavailable review."""
    module = _load_collector()
    success_runner = FakeRunner()
    attempts = 0

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail the first authenticated attempt, then delegate to the successful runner."""
        nonlocal attempts
        if argv[:3] == ["gh", "pr", "view"] and attempts == 0:
            attempts += 1
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"error connecting to api.github.com")
        return success_runner(argv, **kwargs)

    monkeypatch.setattr(module.shutil, "which", lambda command: f"/fixture/{command}")
    output = tmp_path / "pr"

    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=_runner) == 2
    assert (output / "pr-error.txt").is_file()
    assert (output / "command-failure.json").is_file()

    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=_runner) == 0
    assert not (output / "pr-error.txt").exists()
    assert not (output / "command-failure.json").exists()
    assert (output / "pr.json").is_file()


def test_collect_pr_removes_stale_diagnostic_before_nondiagnostic_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure a later parse failure cannot retain a stale GitHub failure classifier."""
    module = _load_collector()
    attempts = 0

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return the staged failure sequence used to test diagnostic cleanup."""
        nonlocal attempts
        if argv[:3] == ["git", "status", "--short"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:3] == ["gh", "pr", "view"]:
            if attempts == 0:
                attempts += 1
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"error connecting to api.github.com")
            return subprocess.CompletedProcess(argv, 0, stdout=b"not-json", stderr=b"")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(module.shutil, "which", lambda command: f"/fixture/{command}")
    output = tmp_path / "pr"

    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=_runner) == 2
    assert (output / "command-failure.json").is_file()

    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=_runner) == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "invalid-json:pr-view\n"
    assert not (output / "command-failure.json").exists()


def test_collect_pr_failure_clears_prior_attempt_before_retaining_current_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent stale source evidence while retaining diagnostics produced by the failed attempt."""
    module = _load_collector()
    output = tmp_path / "pr"
    monkeypatch.setattr(module.shutil, "which", lambda command: f"/fixture/{command}")

    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=FakeRunner()) == 0
    assert (output / "pr.json").is_file()
    assert (output / "diff.patch").is_file()

    def _failing_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return status success and a classified GitHub network failure for the retry."""
        if argv[:3] == ["git", "status", "--short"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"connection reset by peer")
        raise AssertionError(f"unexpected command: {argv}")

    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=_failing_runner) == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "github-network:gh-pr-view\n"
    retained = {filename for filename in module.COLLECTOR_EVIDENCE_ARTIFACTS if (output / filename).exists()}
    assert retained == {"status.txt"}


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        pytest.param(["gh", "pr", "merge", "17", "--merge"], "gh-pr-merge", id="gh-pr-merge-17---merge"),
        pytest.param(["gh", "auth", "status"], "gh-auth-status", id="gh-auth-status"),
        pytest.param(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                "owner=Borda",
                "-f",
                "name=AI-Rig",
                "-F",
                "number=17",
                "-f",
                "query=mutation { closePullRequest(input: {}) { pullRequest { id } } }",
            ],
            "gh-review-threads",
            id="gh-api-graphql--f-owner-borda--f-name-ai-rig--f-number-17--f-query-mutat",
        ),
    ],
)
def test_run_rejects_non_read_only_gh_command(argv: list[str], label: str) -> None:
    """Reject a GitHub CLI command outside the shared read-only boundary."""
    module = _load_collector()
    calls: list[list[str]] = []

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Record an unsafe command invocation if the read-only guard regresses."""
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    with pytest.raises(module.CollectionError, match=f"unsafe-gh-command:{label}"):
        module._run(_runner, argv, 5, label)

    assert calls == []


def test_run_preserves_classified_gh_failure_details() -> None:
    """Record a transport failure without exposing credentials."""
    module = _load_collector()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return a credential-bearing network error for failure sanitization checks."""
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=b"",
            stderr=b"error connecting to api.github.com with ghp_exampletoken\n",
        )

    with pytest.raises(module.CollectionError, match="github-network:gh-pr-view") as error:
        module._run(_runner, ["gh", "pr", "view", "17", "--json", module.PR_FIELDS], 5, "gh-pr-view")

    assert error.value.diagnostics == {
        "exit_code": 1,
        "failure_class": "github-network",
        "failure_reason": "network",
        "label": "gh-pr-view",
    }


def test_collect_pr_writes_classified_opaque_failure_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Publish actionable, credential-safe evidence when GitHub collection fails."""
    module = _load_collector()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Return status success and a credential-bearing GitHub failure response."""
        if argv[:3] == ["git", "status", "--short"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=b"",
            stderr=b"error connecting to api.github.com using github_pat_exampletoken\n",
        )

    monkeypatch.setattr(module.shutil, "which", lambda command: f"/fixture/{command}")
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=_runner)

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "github-network:gh-pr-view\n"
    assert json.loads((output / "command-failure.json").read_text(encoding="utf-8")) == {
        "exit_code": 1,
        "failure_class": "github-network",
        "failure_reason": "network",
        "label": "gh-pr-view",
    }


def test_collect_pr_does_not_ship_a_shell_compatibility_wrapper() -> None:
    """Keep the plugin collector surface Python-only across supported platforms."""
    assert not (PLUGIN_ROOT / "shared" / "collect-pr.sh").exists()
