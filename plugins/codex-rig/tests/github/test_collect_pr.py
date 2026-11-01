"""Portable acceptance checks for authoritative PR evidence collection."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = PLUGIN_ROOT / "shared" / "collect_pr.py"
BASE_OID = "a" * 40
HEAD_OID = "b" * 40
CONNECTION_RESET = b"connection reset by peer"
PR_17_URL = "https://github.com/Borda/AI-Rig/pull/17"


def _review_worktree_add(output: Path) -> list[str]:
    """Return the exact detached-worktree command for a collector report."""
    return ["git", "worktree", "add", "--detach", str(output.with_name(f"{output.name}-review-worktree")), HEAD_OID]


def _load_collector() -> ModuleType:
    """Load the standalone collector without requiring package installation."""
    assert COLLECTOR.is_file(), COLLECTOR
    sys.path.insert(0, str(COLLECTOR.parent))
    try:
        return load_module("codex_rig_collect_pr", COLLECTOR)
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param(["--target", "18"], id="separate-target-override"),
        pytest.param(["--target=18"], id="joined-target-override"),
        pytest.param(["--tar", "18"], id="abbreviated-target-override"),
    ],
)
def test_collector_rejects_approval_target_override(suffix: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """Prevent later arguments from overriding a PR-bound approval prefix."""
    module = _load_collector()
    with pytest.raises(SystemExit) as error:
        module.parse_args(["--target", "https://github.com/example/project/pull/17", "--out", "report", *suffix])
    assert error.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_collector_accepts_one_approval_target() -> None:
    """Keep dynamic report destinations and checkout behind one stable target prefix."""
    module = _load_collector()
    arguments = module.parse_args(
        [
            "--target",
            "https://github.com/example/project/pull/17",
            "--out",
            "report",
            "--checkout",
            "--checkout-mode",
            "remediate",
        ]
    )
    assert arguments.target == "https://github.com/example/project/pull/17"
    assert arguments.out == Path("report")
    assert arguments.checkout is True
    assert arguments.checkout_mode == "remediate"


def _pr_payload(
    *, state: str = "OPEN", cross_repository: bool = False, head_ref: str = "portable-pr"
) -> dict[str, Any]:
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
        "headRefName": head_ref,
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
                            {
                                "id": "resolved",
                                "isResolved": True,
                                "isOutdated": False,
                                "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                            },
                            {
                                "id": "active",
                                "isResolved": False,
                                "isOutdated": False,
                                "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                            },
                            {
                                "id": "outdated",
                                "isResolved": False,
                                "isOutdated": True,
                                "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
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
        nested_paginated: bool = False,
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
        pr_paths: list[str] | None = None,
        gh_paths: list[str] | None = None,
        unmerged_paths: list[str] | None = None,
        untracked_paths: list[str] | None = None,
        staged_paths: list[str] | None = None,
        gh_checkout_fails: bool = True,
        head_ref: str = "portable-pr",
    ) -> None:
        """Initialize configurable process and GitHub-response fixtures."""
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.paginated = paginated
        self.nested_paginated = nested_paginated
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
        self.pr_paths = pr_paths or ["a.py"]
        self.gh_paths = gh_paths
        self.unmerged_paths = unmerged_paths or []
        self.untracked_paths = untracked_paths or []
        self.staged_paths = staged_paths or []
        self.gh_checkout_fails = gh_checkout_fails
        self.head_ref = head_ref

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Simulate the Git, GitHub CLI, and remote-selector commands."""
        assert isinstance(argv, list)
        assert kwargs.get("shell", False) is False
        self.calls.append((argv, kwargs))
        if "core.quotePath=true" in argv:
            start = argv.index("core.quotePath=true") - 1
            assert argv[start : start + 3] == ["-c", "core.quotePath=true", "diff"]
            boundary = argv.index("--", start)
            presentation = [
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--src-prefix=a/",
                "--dst-prefix=b/",
                "--unified=3",
                "--inter-hunk-context=0",
                "--diff-algorithm=myers",
                "--no-renames",
                "--no-relative",
                "--ignore-submodules=none",
            ]
            assert argv[boundary - len(presentation) : boundary] == presentation
            assert "GIT_DIFF_OPTS" not in kwargs["env"]
            argv = [*argv[:start], *argv[start + 2 : boundary - len(presentation)], *argv[boundary:]]
        stdout = b""
        if argv[:2] == ["git", "-C"]:
            review = Path(argv[2])
            operation = argv[3:]
            if operation == ["rev-parse", "--git-common-dir"]:
                stdout = f"{Path.cwd() / '.git'}\n".encode()
            elif operation == ["rev-parse", "--show-toplevel"]:
                stdout = f"{review}\n".encode()
            elif operation == ["rev-parse", "HEAD"]:
                stdout = f"{HEAD_OID}\n".encode()
            elif operation == ["branch", "--show-current"]:
                stdout = b""
            elif operation == ["status", "--porcelain", "-z"]:
                stdout = b""
            elif operation[:4] == ["diff", "--name-only", "-z", f"{BASE_OID}...{HEAD_OID}"]:
                stdout = b"".join(f"{path}\0".encode() for path in self.pr_paths)
            elif operation[:2] == ["diff", "--binary"]:
                stdout = b"diff --git a/a.py b/a.py\n"
            elif operation[:2] == ["diff", "--stat"]:
                if self.statistics_unavailable:
                    return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"binary patch has no stat\n")
                stdout = b" a.py | 1 +\n"
            elif operation[:2] == ["diff", "--numstat"]:
                if self.statistics_unavailable:
                    return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"binary patch has no numstat\n")
                stdout = b"1\t0\ta.py\n"
            else:  # pragma: no cover - makes unsupported review commands diagnostic
                raise AssertionError(f"unexpected review command: {argv}")
        elif argv[:3] == ["git", "worktree", "add"]:
            Path(argv[-2]).mkdir(parents=True)
            stdout = b""
        elif argv == ["git", "diff", "--name-only", "-z", "HEAD", "--"]:
            stdout = b"".join(f"{path}\0".encode() for path in self.dirty_paths)
        elif argv == ["git", "diff", "--cached", "--name-only", "-z", "HEAD", "--"]:
            stdout = b"".join(f"{path}\0".encode() for path in self.staged_paths)
        elif argv in (
            ["git", "ls-files", "--others", "-z"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        ):
            # Fixture untracked files are unignored; real ignored trees are exercised by the real-Git recovery suite.
            stdout = b"".join(f"{path}\0".encode() for path in self.untracked_paths)
        elif argv == ["git", "diff", "--name-only", "-z", "--diff-filter=U", "--"]:
            stdout = b"".join(f"{path}\0".encode() for path in self.unmerged_paths)
        elif argv[:4] == ["git", "diff", "--name-only", "-z"] and "..." in argv[4]:
            stdout = b"".join(f"{path}\0".encode() for path in self.pr_paths)
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
            payload = _pr_payload(state=self.pr_state, cross_repository=self.cross_repository, head_ref=self.head_ref)
            if self.gh_paths is not None:
                payload["files"] = [{"path": path} for path in self.gh_paths]
            stdout = json.dumps(payload).encode()
        elif argv[:3] == ["gh", "api", "graphql"]:
            if self.review_threads_failure:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"connection reset by peer")
            threads_payload = _threads_payload(paginated=self.paginated)
            if self.nested_paginated:
                threads_payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"][0]["comments"][
                    "pageInfo"
                ]["hasNextPage"] = True
            stdout = json.dumps(threads_payload).encode()
        elif argv[:3] == ["gh", "pr", "diff"]:
            stdout = b"diff --git a/a.py b/a.py\n"
        elif argv[:3] == ["gh", "pr", "checkout"]:
            if self.gh_checkout_fails:
                return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"connection reset by peer")
            self.local_head_oid = HEAD_OID
            stdout = b"checked out\n"
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
            elif reference == "--show-toplevel":
                stdout = f"{Path.cwd()}\n".encode()
            elif reference == "--git-common-dir":
                stdout = f"{Path.cwd() / '.git'}\n".encode()
            elif reference == "FETCH_HEAD":
                fetch = next(call for call, _ in reversed(self.calls[:-1]) if call[:2] == ["git", "fetch"])
                stdout = (self.current_base_oid if fetch[-1] == "main" else HEAD_OID).encode() + b"\n"
        elif argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(
                argv,
                0 if self.recorded_base_is_ancestor else 1,
                stdout=b"",
                stderr=b"",
            )
        elif argv[:3] == ["git", "checkout", "--detach"]:
            self.local_head_oid = HEAD_OID
            stdout = b"checked out\n"
        elif argv[:3] == ["git", "branch", "--show-current"]:
            stdout = b"portable-pr\n"
        else:  # pragma: no cover - makes unexpected production commands diagnostic
            raise AssertionError(f"unexpected command: {argv}")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")


def _fixture_which(command: str) -> str:
    """Report every external command as installed under a fixed fake prefix."""
    return f"/fixture/{command}"


def _stub_which(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> None:
    """Make command discovery deterministic so tests never depend on the host's installed tools."""
    monkeypatch.setattr(module.shutil, "which", _fixture_which)


def _configure_collector(monkeypatch: pytest.MonkeyPatch, module: ModuleType, runner: Callable[..., Any]) -> None:
    """Install deterministic command discovery and execution boundaries."""
    _stub_which(monkeypatch, module)
    monkeypatch.setattr(module.subprocess, "run", runner)


def _error_text(output: Path) -> str:
    """Return the collector's recorded error, or an empty string, for use as an assertion message."""
    error = output / "pr-error.txt"
    return error.read_text(encoding="utf-8") if error.exists() else ""


def _read_json_if_present(path: Path) -> Any:
    """Return the parsed JSON document at a path, or None when the collector did not write it."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _gh_pr_view_fails(delegate: Callable[..., Any], stderr: bytes) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    """Return a runner that fails only `gh pr view` with the given stderr and delegates every other command."""

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail the authenticated PR read or pass the command to the delegate runner."""
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=stderr)
        return delegate(argv, **kwargs)

    return run


def test_collect_pr_writes_complete_noncheckout_artifact_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Collect online review evidence with argv-only subprocess execution."""
    module = _load_collector()
    _runner = FakeRunner()
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5)

    assert result == 0, _error_text(output)
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
    assert routing["local_checkout_command"] == " ".join(_review_worktree_add(output))
    collected_pr = json.loads((output / "pr.json").read_text())
    assert collected_pr["body"].startswith("Fix checkpoint")
    assert collected_pr["statusCheckRollup"] == [
        {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}
    ]
    assert "statusCheckRollup" in module.PR_FIELDS
    assert all(call[1]["timeout"] == 5 for call in _runner.calls)


@pytest.mark.parametrize(
    ("runner_options", "expected_error"),
    [
        pytest.param({"paginated": True}, "review-thread-pagination-incomplete", id="thread-page"),
        pytest.param({"nested_paginated": True}, "review-comment-pagination-incomplete", id="inline-comment-page"),
    ],
)
def test_collect_pr_degrades_incomplete_pagination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_options: dict[str, bool], expected_error: str
) -> None:
    """Source review stays available while incomplete supplemental thread evidence is recorded as unavailable.

    GitHub reports another page of review threads, or of comments inside a thread. The collector cannot prove it saw
    everything, so it must succeed without threads rather than present a partial list as complete.
    """
    module = _load_collector()
    _configure_collector(monkeypatch, module, FakeRunner(**runner_options))
    output = tmp_path / "pr"

    result = module.collect_pr(target="", output=output, checkout=False, timeout_seconds=5)

    assert result == 0
    assert not (output / "pr-error.txt").exists()
    assert (output / "review-threads-error.txt").read_text(encoding="utf-8") == f"{expected_error}\n"
    summary = json.loads((output / "online-review-summary.json").read_text())
    assert summary["review_threads_status"] == "unavailable"
    assert summary["review_threads_error"] == expected_error
    assert json.loads((output / "review-threads.json").read_text()) == []


def test_graphql_query_requests_page_info_for_nested_comments() -> None:
    """The review-thread query asks for pagination metadata so incomplete comment pages can be detected."""
    assert "pageInfo { hasNextPage endCursor }" in _load_collector().GRAPHQL_QUERY


def _thread_list(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the object holding the review-thread node array."""
    return payload["data"]["repository"]["pullRequest"]["reviewThreads"]


def _comment_list(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the object holding the first thread's comment node array."""
    return _thread_list(payload)["nodes"][0]["comments"]


def _add_graphql_errors(payload: dict[str, Any]) -> None:
    """Mark the response as partial by attaching a GraphQL errors entry."""
    payload["errors"] = [{"message": "One requested field is unavailable."}]


def _drop_thread_page_info(payload: dict[str, Any]) -> None:
    """Remove the thread-level pagination metadata."""
    del _thread_list(payload)["pageInfo"]


def _drop_comment_page_info(payload: dict[str, Any]) -> None:
    """Remove the nested comment pagination metadata."""
    del _comment_list(payload)["pageInfo"]


NODE_LEVELS = [pytest.param(_thread_list, id="threads"), pytest.param(_comment_list, id="comments")]


@pytest.mark.parametrize(
    ("damage", "match"),
    [
        pytest.param(_add_graphql_errors, None, id="graphql-errors"),
        pytest.param(_drop_thread_page_info, None, id="missing-thread-page-info"),
        pytest.param(_drop_comment_page_info, "invalid-json:review-thread-comments", id="missing-comment-page-info"),
    ],
)
def test_review_threads_rejects_partial_graphql_response(
    damage: Callable[[dict[str, Any]], None], match: str | None
) -> None:
    """Partial GraphQL results are never labeled complete supplemental evidence.

    A response that cannot prove whether threads or inline comments are complete must be rejected instead of read.
    """
    module = _load_collector()
    payload = _threads_payload()
    damage(payload)

    with pytest.raises(module.CollectionError, match=match):
        module._review_threads(payload)


@pytest.mark.parametrize("container_of", NODE_LEVELS)
def test_review_threads_rejects_missing_nodes(container_of: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """A missing node array is distinguished from a genuinely empty one."""
    module = _load_collector()
    payload = _threads_payload()
    container_of(payload).pop("nodes")

    with pytest.raises(module.CollectionError, match="invalid-json:review-thread"):
        module._review_threads(payload)


@pytest.mark.parametrize("container_of", NODE_LEVELS)
@pytest.mark.parametrize(
    "nodes", [pytest.param(None, id="null"), pytest.param({}, id="object"), pytest.param([None], id="invalid-node")]
)
def test_review_threads_rejects_malformed_nodes(
    container_of: Callable[[dict[str, Any]], dict[str, Any]], nodes: object
) -> None:
    """A node array that is null, an object, or holds a non-object entry is rejected."""
    module = _load_collector()
    payload = _threads_payload()
    container_of(payload)["nodes"] = nodes

    with pytest.raises(module.CollectionError, match="invalid-json:review-thread"):
        module._review_threads(payload)


def test_review_threads_accepts_explicit_empty_thread_list() -> None:
    """An explicitly empty thread list is complete evidence, not an error."""
    module = _load_collector()
    payload = _threads_payload()
    _thread_list(payload)["nodes"] = []

    assert module._review_threads(payload) == []


def test_review_threads_accepts_explicit_empty_comment_list() -> None:
    """Threads whose comment lists are explicitly empty are returned unchanged as available evidence."""
    module = _load_collector()
    payload = _threads_payload()

    assert module._review_threads(payload) == _thread_list(payload)["nodes"]


@pytest.mark.parametrize("level", ["threads", "comments"])
def test_collect_pr_marks_missing_nodes_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """Propagate missing node evidence into the ordinary collector's availability summary."""
    module = _load_collector()
    runner = FakeRunner()

    def external_response(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Supply a partial external GraphQL result at the subprocess boundary."""
        result = runner(argv, **kwargs)
        if argv[:3] == ["gh", "api", "graphql"]:
            payload = json.loads(result.stdout)
            container = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
            if level == "comments":
                container = container["nodes"][0]["comments"]
            container.pop("nodes")
            result.stdout = json.dumps(payload).encode()
        return result

    _configure_collector(monkeypatch, module, external_response)
    output = tmp_path / "pr"
    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5) == 0
    summary = json.loads((output / "online-review-summary.json").read_text())
    assert summary["review_threads_status"] == "unavailable"
    assert summary["review_threads_error"] == (
        "invalid-json:review-threads" if level == "threads" else "invalid-json:review-thread-comments"
    )
    assert summary["review_thread_count"] == 0
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

    assert result == 0, _error_text(output)
    assert json.loads((output / "online-review-summary.json").read_text())["pr_metadata_transport"] == "gh"


def test_public_pr_metadata_normalizes_an_absent_optional_body() -> None:
    """Keep public PRs without a description eligible for limited fallback review."""
    module = _load_collector()
    payload = _public_pr_payload()
    payload["body"] = None

    normalized = module._normalized_public_pr(payload, module.PRTarget("Borda", "AI-Rig", 17))

    assert normalized["body"] == ""


@pytest.mark.parametrize(
    ("owner", "repository", "number", "accepted"),
    [
        pytest.param("borda", "ai-rig", 17, True, id="same-identity-lowercase"),
        pytest.param("BORDA", "AI-RIG", 17, True, id="same-identity-uppercase"),
        pytest.param("other", "ai-rig", 17, False, id="different-owner"),
        pytest.param("borda", "other", 17, False, id="different-repository"),
        pytest.param("borda", "ai-rig", 18, False, id="different-number"),
    ],
)
def test_public_pr_identity_case_equivalence(owner: str, repository: str, number: int, accepted: bool) -> None:
    """Public metadata must match the requested PR independent of repository capitalization."""
    module = _load_collector()
    payload = _public_pr_payload()
    original = json.dumps(payload, sort_keys=True)
    target = module.PRTarget(owner, repository, number)
    if accepted:
        normalized = module._normalized_public_pr(payload, target)
        assert normalized["url"] == target.url
        assert normalized["number"] == number
    else:
        with pytest.raises(module.CollectionError, match=r"public-pr-(base-repository|identity)-mismatch"):
            module._normalized_public_pr(payload, target)
    assert json.dumps(payload, sort_keys=True) == original


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

    assert result == 0, _error_text(output)
    assert public_requests == ["https://api.github.com/repos/Borda/AI-Rig/pulls/17"]
    assert any(
        argv == ["git", "fetch", "--no-tags", "--refmap=", "origin", "refs/pull/17/head"] for argv, _ in _runner.calls
    )
    assert any(argv == _review_worktree_add(output) for argv, _ in _runner.calls)
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
    ("target", "stderr", "expected_error", "github_remotes", "checkout"),
    [
        pytest.param(
            "17",
            b"connection reset by peer",
            "github-network:gh-pr-view",
            {
                "origin": ["https://github.com/Borda/AI-Rig.git"],
                "fork": ["https://github.com/contributor/AI-Rig.git"],
            },
            False,
            id="ambiguous-configured-remotes",
        ),
        pytest.param(
            "https://github.com/Borda/AI-Rig/pull/17",
            b"resource not accessible",
            "github-permission:gh-pr-view",
            {"origin": ["https://github.com/Borda/AI-Rig.git"]},
            False,
            id="permission-denied",
        ),
        pytest.param(
            "17",
            b"GraphQL: Could not resolve to a PullRequest with the number of 17.",
            "github-not-found:gh-pr-view",
            {"origin": ["https://github.com/Borda/AI-Rig.git"]},
            True,
            id="graphql-missing-pr",
        ),
    ],
)
def test_collect_pr_keeps_ambiguous_denied_or_missing_metadata_out_of_public_fallback(
    target: str,
    stderr: bytes,
    expected_error: str,
    github_remotes: dict[str, list[str]],
    checkout: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never activate the public fallback for an ambiguous, permission-limited, or semantically missing PR.

    Public identity must not be inferred from ambiguous configured remotes, GitHub's authenticated permission decision
    must not be bypassed, and a missing PR must be reported directly instead of being retried as a network failure.
    """
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    _configure_collector(monkeypatch, module, _gh_pr_view_fails(FakeRunner(github_remotes=github_remotes), stderr))
    public_get = mock.Mock(
        side_effect=AssertionError(
            "ambiguous selectors and permission failures must not activate public HTTPS fallback"
        )
    )
    monkeypatch.setattr(reader, "public_github_get", public_get)

    assert module.collect_pr(target=target, output=output, checkout=checkout, timeout_seconds=5) == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == f"{expected_error}\n"
    assert not (output / "pr.json").exists()


@pytest.mark.parametrize(
    ("failure_class", "diagnostic"),
    [
        pytest.param("github-not-found", None, id="not-found-keeps-no-diagnostic"),
        pytest.param(
            "github-network",
            {
                "exit_code": 1,
                "failure_class": "github-network",
                "failure_reason": "connection-reset",
                "label": "gh-pr-view",
            },
            id="network-records-diagnostic",
        ),
    ],
)
def test_collect_pr_fails_closed_when_public_fallback_cannot_read_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_class: str, diagnostic: dict[str, Any] | None
) -> None:
    """No partial PR evidence is emitted when a canonical URL is private or unavailable publicly.

    The authenticated read fails with a network error and the public fallback then reports the given failure class; only
    a network failure leaves a command-failure diagnostic behind.
    """
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    _configure_collector(monkeypatch, module, _gh_pr_view_fails(FakeRunner(), CONNECTION_RESET))
    public_get = mock.Mock(side_effect=reader.GitHubReadError(f"{failure_class}:gh-pr-view"))
    monkeypatch.setattr(reader, "public_github_get", public_get)

    assert (
        module.collect_pr(
            target="https://github.com/Borda/AI-Rig/pull/17",
            output=output,
            checkout=True,
            timeout_seconds=5,
        )
        == 2
    )
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == f"{failure_class}:gh-pr-view\n"
    assert not (output / "pr.json").exists()
    assert _read_json_if_present(output / "command-failure.json") == diagnostic


def test_collect_pr_rejects_canonical_url_without_matching_configured_github_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a public URL from authorizing a different local repository's checkout evidence."""
    module = _load_collector()
    reader = sys.modules["github_read"]
    output = tmp_path / "pr"
    _runner = FakeRunner(github_remotes={"origin": ["https://github.com/Borda/other-repository.git"]})
    _configure_collector(monkeypatch, module, _gh_pr_view_fails(_runner, CONNECTION_RESET))
    public_get = mock.Mock(
        side_effect=AssertionError("a URL without matching configured remote must not request public metadata")
    )
    monkeypatch.setattr(reader, "public_github_get", public_get)

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

    assert result == 0, _error_text(output)
    assert json.loads((output / "pr.json").read_text())["body"].startswith("Fix checkpoint")
    assert (output / "review-threads-error.txt").read_text() == "github-network:gh-review-threads\n"
    assert (output / "diff.patch").read_bytes() == b"diff --git a/a.py b/a.py\n"
    assert any(argv == _review_worktree_add(output) for argv, _ in _runner.calls)
    assert any(
        argv
        == [
            "git",
            "-C",
            output.with_name(f"{output.name}-review-worktree").as_posix(),
            "-c",
            "core.quotePath=true",
            "diff",
            "--binary",
            f"{BASE_OID}...{HEAD_OID}",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            "--inter-hunk-context=0",
            "--diff-algorithm=myers",
            "--no-renames",
            "--no-relative",
            "--ignore-submodules=none",
            "--",
        ]
        for argv, _ in _runner.calls
    )
    assert not any(argv[:3] == ["gh", "pr", "diff"] for argv, _ in _runner.calls)
    checkout = json.loads((output / "local-checkout.json").read_text())
    assert checkout["diff_source"] == "verified-local-checkout"
    assert checkout["diff_base_oid"] == BASE_OID
    assert checkout["diff_head_oid"] == HEAD_OID


def test_collect_pr_fetches_fork_head_before_creating_review_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make a fresh fork commit available before isolated worktree creation."""
    module = _load_collector()
    runner = FakeRunner(cross_repository=True)
    fork_fetch = ["git", "fetch", "--no-tags", "--refmap=", "origin", "refs/pull/17/head"]
    review_add = _review_worktree_add(tmp_path / "pr")

    def run_with_unfetched_fork(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Model Git rejecting a worktree commit that has not yet been fetched."""
        if argv == review_add and not any(call == fork_fetch for call, _ in runner.calls):
            return subprocess.CompletedProcess(argv, 128, stdout=b"", stderr=b"unknown revision")
        return runner(argv, **kwargs)

    _configure_collector(monkeypatch, module, runner)
    monkeypatch.setattr(module.subprocess, "run", run_with_unfetched_fork)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0, _error_text(output)
    calls = [argv for argv, _ in runner.calls]
    target_fetch = ["git", "fetch", "--no-tags", "--refmap=", "origin", "main"]
    assert calls.index(target_fetch) < calls.index(fork_fetch) < calls.index(review_add)
    head = json.loads((output / "pr-head-fetch.json").read_text(encoding="utf-8"))
    assert head["status"] == "fetched"
    assert head["local_head"] == head["expected_head_oid"] == HEAD_OID
    assert head["head_matches_pr_metadata"] is True
    assert not any("--force" in argv or argv[:2] == ["git", "pull"] for argv in calls)


def test_collect_pr_isolates_review_even_when_main_is_already_at_pr_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give review a dedicated worktree even when the invoking branch matches PR metadata."""
    module = _load_collector()
    _runner = FakeRunner(current_head_oid=HEAD_OID, cross_repository=True)
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    assert not any(argv[:3] == ["gh", "pr", "checkout"] for argv, _ in _runner.calls)
    checkout = json.loads((output / "local-checkout.json").read_text())
    assert checkout["command"] == " ".join(_review_worktree_add(output))
    assert checkout["worktree"] == output.with_name(f"{output.name}-review-worktree").as_posix()
    assert checkout["head_matches_pr"] is True
    assert (output / "diff.patch").read_bytes() == b"diff --git a/a.py b/a.py\n"


def test_collect_pr_remediation_always_uses_gh_checkout_at_matching_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Require GitHub's branch checkout even when the local revision already matches."""
    module = _load_collector()
    runner = FakeRunner(current_head_oid=HEAD_OID, cross_repository=True, gh_checkout_fails=False)
    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    result = module.collect_pr(
        target="17",
        output=output,
        checkout=True,
        checkout_mode="remediate",
        timeout_seconds=5,
    )

    assert result == 0
    assert ["gh", "pr", "checkout", "https://github.com/Borda/AI-Rig/pull/17"] in [argv for argv, _ in runner.calls]
    checkout = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    assert checkout["checkout_mode"] == "remediate"
    assert checkout["command"] == "gh pr checkout https://github.com/Borda/AI-Rig/pull/17"
    assert checkout["local_branch"] == "portable-pr"


def test_collect_pr_remediation_stops_when_gh_checkout_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent remediation from silently replacing a failed GitHub checkout with detached HEAD."""
    module = _load_collector()
    runner = FakeRunner(cross_repository=True, gh_checkout_fails=True)
    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    result = module.collect_pr(
        target="17",
        output=output,
        checkout=True,
        checkout_mode="remediate",
        timeout_seconds=5,
    )

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "remediation-gh-checkout-recovery-required\n"
    assert any(argv[:3] == ["gh", "pr", "checkout"] for argv, _ in runner.calls)
    assert not any(argv[:3] == ["git", "checkout", "--detach"] for argv, _ in runner.calls)
    assert not (output / "local-checkout.json").exists()


def test_collect_pr_remediation_rejects_inconsistent_cross_repository_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when routing says same-repository but PR metadata says it is a fork."""
    module = _load_collector()
    runner = FakeRunner(cross_repository=True, gh_checkout_fails=True)
    _configure_collector(monkeypatch, module, runner)
    original_review_artifacts = module._review_artifacts

    def _inconsistent_review_artifacts(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """Return deliberately inconsistent routing to exercise the recovery guard."""
        routing = original_review_artifacts(*args, **kwargs)
        routing["same_repo"] = True
        return routing

    monkeypatch.setattr(module, "_review_artifacts", _inconsistent_review_artifacts)
    output = tmp_path / "pr"

    result = module.collect_pr(
        target="17",
        output=output,
        checkout=True,
        checkout_mode="remediate",
        timeout_seconds=5,
    )

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "remediation-gh-checkout-recovery-required\n"
    assert not any(argv[:2] == ["git", "checkout"] for argv, _ in runner.calls)


@pytest.mark.parametrize(
    ("runner_options", "expected_error"),
    [
        pytest.param(
            {"gh_checkout_fails": True, "head_ref": "--orphan"},
            "missing-remediation-head-branch",
            id="option-shaped-head-branch",
        ),
        pytest.param(
            {"pr_state": "CLOSED", "gh_checkout_fails": False},
            "remediation-requires-open-pr",
            id="closed-pr",
        ),
    ],
)
def test_collect_pr_remediation_rejects_unsafe_pr_before_gh_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_options: dict[str, Any], expected_error: str
) -> None:
    """Refuse a remediation checkout that would parse a branch name as an option or target a closed PR.

    The direct native checkout must never see a PR branch name shaped like an option, and remediation checkout is
    limited to an open pull request with a live publication destination.
    """
    module = _load_collector()
    runner = FakeRunner(**runner_options)
    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    result = module.collect_pr(
        target="17",
        output=output,
        checkout=True,
        checkout_mode="remediate",
        timeout_seconds=5,
    )

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == f"{expected_error}\n"
    assert not any(argv[:3] == ["gh", "pr", "checkout"] for argv, _ in runner.calls)


def test_collect_pr_rejects_remediation_mode_without_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Require the remediation mode to invoke its mandatory GitHub branch checkout."""
    module = _load_collector()
    runner = FakeRunner(gh_checkout_fails=False)
    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    result = module.collect_pr(
        target="17",
        output=output,
        checkout=False,
        checkout_mode="remediate",
        timeout_seconds=5,
    )

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "remediation-requires-checkout\n"
    assert runner.calls == []


def test_collect_pr_review_uses_detached_worktree_without_gh_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep exact-commit review independent of GitHub branch checkout."""
    module = _load_collector()
    runner = FakeRunner(gh_checkout_fails=True)
    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    calls = [argv for argv, _ in runner.calls]
    assert _review_worktree_add(output) in calls
    assert not any(call[:3] == ["gh", "pr", "checkout"] for call in calls)
    checkout = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    assert checkout["checkout_mode"] == "review"
    assert checkout["command"] == " ".join(_review_worktree_add(output))
    assert checkout["gh_checkout_failure"] is None


def test_collect_pr_review_records_detached_worktree_in_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep routing metadata aligned with the isolated review worktree."""
    module = _load_collector()
    runner = FakeRunner(gh_checkout_fails=False)
    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    routing = json.loads((output / "pr-routing.json").read_text(encoding="utf-8"))
    assert routing["local_checkout_command"] == " ".join(_review_worktree_add(output))
    assert routing["checkout_method"] == "git-detached-review-worktree"


def test_collect_pr_records_unrelated_main_dirtiness_without_blocking_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep unrelated tracked environment churn from blocking isolated review."""
    module = _load_collector()
    _runner = FakeRunner(dirty_paths=["uv.lock"], checkout_paths=["a.py"])
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    preflight = json.loads((output / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert preflight["status"] == "safe-unrelated-dirty-paths"
    assert preflight["dirty_paths"] == ["uv.lock"]
    assert preflight["phase"] == "source-worktree-context"
    assert preflight["checkout_paths"] == []
    assert preflight["overlapping_paths"] == []
    assert any(argv == _review_worktree_add(output) for argv, _ in _runner.calls)


def _collect_with_scan_failure(module: ModuleType, monkeypatch: pytest.MonkeyPatch, output: Path, failure: str) -> int:
    """Run a checkout collection where one untracked-file or status scan returns oversized or failing output.

    `failure` is `oversized-main`, `oversized-review` or `failed-main`; every other command succeeds as usual.
    """
    runner = FakeRunner()
    oversized = b"cache/file\0" * (module.MAX_OUTPUT_BYTES // len(b"cache/file\0") + 1)
    scans = {
        "oversized-main": (["git", "ls-files", "--others", "-z"], 0, oversized, b""),
        "failed-main": (["git", "ls-files", "--others", "-z"], 128, b"", b"fatal: unable to read index"),
    }

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Supply command output at the informational or required source boundary."""
        result = runner(argv, **kwargs)
        review_scan = argv[:2] == ["git", "-C"] and argv[3:] == ["status", "--porcelain", "-z"]
        if failure == "oversized-review" and review_scan:
            return subprocess.CompletedProcess(argv, 0, stdout=oversized, stderr=b"")
        scan_argv, returncode, stdout, stderr = scans.get(failure, (None, 0, b"", b""))
        if argv == scan_argv:
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
        return result

    _stub_which(monkeypatch, module)
    return module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5, run=run)


def test_review_tolerates_output_limit_in_informational_main_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real output cap is only a soft limit for the invoking tree's context scan.

    An oversized untracked-file listing in the main checkout is recorded as unavailable context; review of the isolated
    worktree still completes.
    """
    output = tmp_path / "pr"

    result = _collect_with_scan_failure(_load_collector(), monkeypatch, output, "oversized-main")

    assert result == 0
    context = json.loads((output / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert context["status"] == "unavailable"
    assert context["reason"] == "command-output-oversized:source-worktree-context-untracked-worktree-paths"
    checkout = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    assert checkout["local_head"] == HEAD_OID
    assert not (output / "pr-error.txt").exists()


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        pytest.param(
            "oversized-review", "command-output-oversized:review-worktree-status", id="required-review-source"
        ),
        pytest.param(
            "failed-main", "command-failed:source-worktree-context-untracked-worktree-paths", id="other-main-error"
        ),
    ],
)
def test_review_fails_on_required_scan_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, expected_error: str
) -> None:
    """An oversized review-worktree status, or any non-limit failure of the main scan, still fails collection.

    Only the output cap on informational main-tree context is tolerated; the isolated review source is required
    evidence, and a genuine Git error is never downgraded to unavailable.
    """
    output = tmp_path / "pr"

    result = _collect_with_scan_failure(_load_collector(), monkeypatch, output, failure)

    assert result == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == f"{expected_error}\n"
    assert not (output / "local-checkout.json").exists()


def test_collect_pr_records_main_dirty_paths_overlapping_pr_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record overlapping main edits without blocking isolated review."""
    module = _load_collector()
    _runner = FakeRunner(dirty_paths=["uv.lock", "a.py"], checkout_paths=["a.py", "src/app.py"])
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    preflight = json.loads((output / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert preflight["status"] == "blocked-pr-dirty-paths"
    assert preflight["overlapping_pr_paths"] == ["a.py"]
    assert not any(argv[:3] == ["gh", "pr", "checkout"] for argv, _ in _runner.calls)
    assert any(argv == _review_worktree_add(output) for argv, _ in _runner.calls)


def test_collect_pr_preserves_main_dirty_paths_when_already_at_pr_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Create isolated source without blocking on current-head main edits."""
    module = _load_collector()
    _runner = FakeRunner(current_head_oid=HEAD_OID, cross_repository=True, dirty_paths=["uv.lock"])
    _configure_collector(monkeypatch, module, _runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0
    preflight = json.loads((output / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert preflight["status"] == "safe-unrelated-dirty-paths"
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

    assert result == 0, _error_text(output)
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
    assert checkout["command"] == " ".join(_review_worktree_add(output))
    assert checkout["diff_source"] == "verified-local-checkout"
    assert "no --force was used" in checkout["force_policy"]
    assert all("--force" not in argument for argv, _ in _runner.calls for argument in argv)


def test_checkout_file_inventory_uses_complete_verified_git_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain late workflow and spaced paths omitted from a hundred-file CLI response."""
    module = _load_collector()
    first_hundred = [f"src/part_{index:03d}.py" for index in range(100)]
    late_paths = ["src/production code.py", ".github/workflows/ci-tests.yml"]
    actual_paths = [*first_hundred, *late_paths]
    runner = FakeRunner(gh_paths=first_hundred, pr_paths=actual_paths)
    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5)

    assert result == 0, _error_text(output)
    assert (output / "files.txt").read_text(encoding="utf-8").splitlines() == sorted(actual_paths)
    assert any(argv[-5:] == ["diff", "--name-only", "-z", f"{BASE_OID}...{HEAD_OID}", "--"] for argv, _ in runner.calls)
    routing = load_module("review_routing_complete_pr", PLUGIN_ROOT / "skills/code-review/review_routing.py")
    tier, evidence, mandatory = routing.derive_mechanical_risk(output)
    assert tier == "HIGH_RISK"
    assert "axis_cicd_steward" in mandatory
    assert "files=102" in evidence


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

    assert result == 0, _error_text(output)
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
    """Retain conservative isolated-path evidence if worktree creation fails."""
    module = _load_collector()
    success_runner = FakeRunner()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail checkout while delegating all metadata commands to the successful runner."""
        if argv[:3] == ["git", "worktree", "add"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"worktree add failed")
        return success_runner(argv, **kwargs)

    _configure_collector(monkeypatch, module, success_runner)
    output = tmp_path / "pr"

    result = module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5, run=_runner)

    assert result == 2
    checkout_state = json.loads((output / "checkout-state.json").read_text())
    assert checkout_state["status"] == "worktree-create-started"
    assert checkout_state["local_state"] == "main-worktree-unchanged; new-worktree-unknown"
    assert checkout_state["worktree"] == output.with_name(f"{output.name}-review-worktree").as_posix()
    assert (output / "pr.json").is_file()
    assert (output / "comments.json").is_file()
    assert (output / "review-threads.json").is_file()


def test_collect_pr_omits_verified_source_artifact_when_checkout_head_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep fetch and checkout-state diagnostics without claiming an unverified local source."""
    module = _load_collector()
    runner = FakeRunner()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Report a different isolated HEAD despite successful worktree creation."""
        if argv[:2] == ["git", "-C"] and argv[3:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, stdout=("d" * 40).encode(), stderr=b"")
        return runner(argv, **kwargs)

    _configure_collector(monkeypatch, module, runner)
    output = tmp_path / "pr"

    assert module.collect_pr(target="17", output=output, checkout=True, timeout_seconds=5, run=_runner) == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "local-checkout-head-mismatch\n"
    checkout_state = json.loads((output / "checkout-state.json").read_text(encoding="utf-8"))
    assert checkout_state["status"] == "worktree-create-succeeded-unverified"
    assert checkout_state["local_state"] == "main-worktree-unchanged; new-worktree-unverified"
    assert not (output / "local-checkout.json").exists()
    assert (output / "pr-routing.json").is_file()
    assert (output / "pr-head-fetch.json").is_file()
    assert (output / "source-worktree-context.json").is_file()


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
    assert checkout["command"] == " ".join(_review_worktree_add(output))
    assert not any(argv[:3] == ["git", "fetch", "--no-tags"] and "portable-pr" in argv[-1] for argv, _ in _runner.calls)


def test_collect_pr_timeout_is_bounded_and_records_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Convert a stalled external command into the stable collection failure contract."""
    module = _load_collector()
    _stub_which(monkeypatch, module)

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

    _stub_which(monkeypatch, module)
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

    _stub_which(monkeypatch, module)
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
    _stub_which(monkeypatch, module)

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


def test_collect_pr_fails_when_initial_git_status_cannot_be_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when the collector cannot establish the initial worktree state."""
    module = _load_collector()
    success_runner = FakeRunner()

    def _runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """Fail only the required initial status read with text that must not be retained."""
        if argv == ["git", "status", "--short"]:
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"status failed github_pat_secret")
        return success_runner(argv, **kwargs)

    _stub_which(monkeypatch, module)
    output = tmp_path / "pr"

    assert module.collect_pr(target="17", output=output, checkout=False, timeout_seconds=5, run=_runner) == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "command-failed:git-status\n"
    assert json.loads((output / "command-failure.json").read_text(encoding="utf-8")) == {
        "exit_code": 1,
        "failure_class": "command-failed",
        "failure_reason": "unknown",
        "label": "git-status",
    }
    assert not (output / "status.txt").exists()


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        pytest.param(["gh", "pr", "merge", "17", "--merge"], "gh-pr-merge", id="pr-merge"),
        pytest.param(["gh", "auth", "status"], "gh-auth-status", id="auth-status"),
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
            id="graphql-mutation",
        ),
    ],
)
def test_run_rejects_non_read_only_gh_command(argv: list[str], label: str) -> None:
    """A GitHub CLI command outside the shared read-only boundary is refused before it can run.

    The collector's own runner wrapper must apply the same allowlist as the shared reader, so a regression there would
    let a merge, credential query or GraphQL mutation reach the process boundary.
    """
    module = _load_collector()
    runner = mock.Mock()

    with pytest.raises(module.CollectionError, match=f"unsafe-gh-command:{label}"):
        module._run(runner, argv, 5, label)

    runner.assert_not_called()


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

    _stub_which(monkeypatch, module)
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
