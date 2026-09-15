"""Real-Git regressions for collector checkout recovery and source identity."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = PLUGIN_ROOT / "shared" / "collect_pr.py"


def _git(repository: Path, *arguments: str, expected: int = 0) -> str:
    """Run Git in a disposable repository and require its expected outcome."""
    result = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=False)
    assert result.returncode == expected, (arguments, result.returncode, result.stderr)
    return result.stdout.strip()


def _load_collector() -> ModuleType:
    """Load the standalone collector without installing the plugin."""
    specification = importlib.util.spec_from_file_location("collector_git_recovery", COLLECTOR)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _setup_repositories(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    """Create a local PR source, a stale branch, and a checked-out PR head."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Collector fixture")
    _git(source, "config", "user.email", "collector@example.invalid")
    (source / "module.py").write_text("value = 'base'\n", encoding="utf-8")
    (source / "notes.txt").write_text("keep me\n", encoding="utf-8")
    removed_directory = source / "removed"
    removed_directory.mkdir()
    (removed_directory / "nested.py").write_text("value = 'removed'\n", encoding="utf-8")
    _git(source, "add", "module.py", "notes.txt", "removed/nested.py")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "-b", "old-topic")
    (source / "module.py").write_text("value = 'old'\n", encoding="utf-8")
    _git(source, "commit", "-am", "old topic")
    old = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "-b", "topic", base)
    (source / "module.py").write_text("value = 'new'\n", encoding="utf-8")
    _git(source, "commit", "-am", "new topic")
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/pull/17/head", head)

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _git(worktree, "init", "-b", "main")
    _git(worktree, "config", "user.name", "Collector fixture")
    _git(worktree, "config", "user.email", "collector@example.invalid")
    _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/topic:refs/heads/topic")
    _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/old-topic:refs/heads/old-topic")
    _git(worktree, "checkout", "topic")
    _git(worktree, "config", "remote.origin.url", "https://github.com/example/project.git")
    _git(worktree, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    return source, worktree, base, old, head


def _payload(base: str, head: str, *, cross_repository: bool = True, state: str = "OPEN") -> dict[str, Any]:
    """Return the minimal PR metadata that binds the local Git fixture."""
    return {
        "number": 17,
        "title": "Fixture change",
        "body": "Verify source identity.",
        "url": "https://github.com/example/project/pull/17",
        "author": {"login": "contributor"},
        "baseRefName": "main",
        "baseRefOid": base,
        "headRefName": "topic",
        "headRefOid": head,
        "headRepository": {"nameWithOwner": "contributor/project" if cross_repository else "example/project"},
        "headRepositoryOwner": {"login": "contributor" if cross_repository else "example"},
        "isCrossRepository": cross_repository,
        "state": state,
        "isDraft": False,
        "comments": [],
        "reviews": [],
        "files": [{"path": "module.py"}],
        "statusCheckRollup": [],
    }


def _collect(
    module: ModuleType,
    source: Path,
    worktree: Path,
    output: Path,
    base: str,
    head: str,
    *,
    cross_repository: bool = True,
    state: str = "OPEN",
    mutate_after_checkout: bool = False,
    checkout_mode: str = "review",
    gh_checkout_fails: bool = True,
    mutate_after_gh_failure: bool = False,
    move_source_after_head_fetch: bool = False,
) -> tuple[int, list[list[str]]]:
    """Run the collector against real Git while replacing only GitHub responses."""
    payload = _payload(base, head, cross_repository=cross_repository, state=state)
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Route Git transport to the local source and return fixed GitHub metadata."""
        calls.append(arguments[:])
        if arguments[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(payload).encode(), stderr=b"")
        if arguments[:3] == ["gh", "api", "graphql"]:
            threads = {
                "data": {
                    "repository": {"pullRequest": {"reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}
                }
            }
            return subprocess.CompletedProcess(arguments, 0, stdout=json.dumps(threads).encode(), stderr=b"")
        if arguments[:3] == ["gh", "pr", "checkout"]:
            if gh_checkout_fails:
                if mutate_after_gh_failure:
                    (worktree / "module.py").write_text("value = 'partial-gh-checkout'\n", encoding="utf-8")
                return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=b"connection reset by peer")
            checkout = subprocess.run(["git", "checkout", "topic"], cwd=worktree, **kwargs)
            if checkout.returncode == 0:
                tracking_remote = "https://github.com/contributor/project.git" if cross_repository else "origin"
                _git(worktree, "config", "branch.topic.remote", tracking_remote)
                _git(worktree, "config", "branch.topic.merge", "refs/heads/topic")
            return checkout
        if arguments[:3] == ["git", "checkout", "--detach"]:
            result = subprocess.run(arguments, cwd=worktree, **kwargs)
            if mutate_after_checkout:
                (worktree / "module.py").write_text("value = 'raced'\n", encoding="utf-8")
            return result
        assert arguments[0] != "gh", arguments
        if arguments[:2] == ["git", "fetch"]:
            is_same_repo_head_fetch = arguments[-1] == "topic" and "--refmap=" in arguments
            arguments = [str(source) if argument == "origin" else argument for argument in arguments]
            result = subprocess.run(arguments, cwd=worktree, **kwargs)
            if move_source_after_head_fetch and is_same_repo_head_fetch:
                (source / "module.py").write_text("value = 'source moved'\n", encoding="utf-8")
                _git(source, "commit", "-am", "source moved")
            return result
        return subprocess.run(arguments, cwd=worktree, **kwargs)

    previous_which = module.shutil.which
    module.shutil.which = lambda command: f"/fixture/{command}"
    try:
        code = module.collect_pr(
            target="https://github.com/example/project/pull/17",
            output=output,
            checkout=True,
            checkout_mode=checkout_mode,
            timeout_seconds=10,
            run=run,
        )
    finally:
        module.shutil.which = previous_which
    return code, calls


@pytest.mark.parametrize(
    ("cross_repository", "state"),
    [
        pytest.param(False, "OPEN", id="same-repository-open"),
        pytest.param(True, "OPEN", id="fork-open"),
        pytest.param(False, "MERGED", id="historical-same-repository"),
    ],
)
def test_collect_pr_uses_destination_free_head_fetch_without_rewriting_stale_cache(
    tmp_path: Path, cross_repository: bool, state: str
) -> None:
    """Verify every Git route can inspect an exact PR head without updating a stale cache ref."""
    module = _load_collector()
    source, worktree, base, old, head = _setup_repositories(tmp_path)
    stale_ref = (
        "refs/remotes/origin/topic" if not cross_repository and state == "OPEN" else "refs/remotes/origin/pull/17/head"
    )
    _git(worktree, "update-ref", stale_ref, old)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=cross_repository,
        state=state,
    )

    assert code == 0
    assert _git(worktree, "rev-parse", stale_ref) == old
    assert any(arguments[:4] == ["git", "fetch", "--no-tags", "--refmap="] for arguments in calls)
    fetched = json.loads((tmp_path / "collected" / "pr-head-fetch.json").read_text(encoding="utf-8"))
    assert fetched["remote_ref"] == "FETCH_HEAD"
    assert fetched["local_head"] == head


def test_collect_pr_uses_destination_free_target_fetch_without_rewriting_stale_cache(tmp_path: Path) -> None:
    """Verify an old target cache cannot block a current exact PR checkout."""
    module = _load_collector()
    source, worktree, base, old, head = _setup_repositories(tmp_path)
    _git(worktree, "update-ref", "refs/remotes/origin/main", old)

    code, calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 0
    assert _git(worktree, "rev-parse", "refs/remotes/origin/main") == old
    target = json.loads((tmp_path / "collected" / "target-branch.json").read_text(encoding="utf-8"))
    assert target["remote_ref"] == base
    assert any(arguments[:4] == ["git", "fetch", "--no-tags", "--refmap="] for arguments in calls)


@pytest.mark.parametrize("state", ["unstaged", "staged", "unmerged"])
def test_collect_pr_rejects_pr_file_changes_or_unmerged_index_at_matching_head(tmp_path: Path, state: str) -> None:
    """Reject working-tree states that make matching commit identity insufficient for source review."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    if state in {"unstaged", "staged"}:
        (worktree / "module.py").write_text("value = 'local-unreviewed'\n", encoding="utf-8")
        if state == "staged":
            _git(worktree, "add", "module.py")
    else:
        _git(worktree, "merge", "--no-commit", "old-topic", expected=1)

    code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 2
    preflight = json.loads((tmp_path / "collected" / "worktree-preflight.json").read_text(encoding="utf-8"))
    assert preflight["pr_paths"] == ["module.py"]
    if state == "unmerged":
        assert preflight["unmerged_paths"] == ["module.py"]
    else:
        assert preflight["overlapping_pr_paths"] == ["module.py"]


def test_collect_pr_rejects_staged_pr_change_hidden_by_restored_worktree(tmp_path: Path) -> None:
    """Reject an index-only PR change when the worktree bytes have been restored to HEAD."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    staged_contents = "value = 'local-staged'\n"
    (worktree / "module.py").write_text(staged_contents, encoding="utf-8")
    _git(worktree, "add", "module.py")
    (worktree / "module.py").write_text("value = 'new'\n", encoding="utf-8")

    code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 2
    assert _git(worktree, "show", ":module.py") == staged_contents.strip()
    preflight = json.loads((tmp_path / "collected" / "worktree-preflight.json").read_text(encoding="utf-8"))
    assert preflight["dirty_paths"] == ["module.py"]
    assert preflight["overlapping_pr_paths"] == ["module.py"]
    assert not (tmp_path / "collected" / "local-checkout.json").exists()


def test_collect_pr_preserves_unrelated_edits_at_matching_head(tmp_path: Path) -> None:
    """Allow a local edit outside the PR diff without changing or misreporting it."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    (worktree / "notes.txt").write_text("keep local edit\n", encoding="utf-8")

    code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 0
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "keep local edit\n"


def test_collect_pr_preserves_unrelated_untracked_files_at_matching_head(tmp_path: Path) -> None:
    """Allow an untracked file that is outside the PR diff without expanding review scope."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    (worktree / "scratch.txt").write_text("keep local scratch\n", encoding="utf-8")

    code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 0
    preflight = json.loads((tmp_path / "collected" / "worktree-preflight.json").read_text(encoding="utf-8"))
    assert preflight["dirty_paths"] == ["scratch.txt"]
    assert preflight["overlapping_pr_paths"] == []
    assert (worktree / "scratch.txt").read_text(encoding="utf-8") == "keep local scratch\n"


@pytest.mark.parametrize("path", ["notes.txt", "removed/nested.py"])
def test_collect_pr_rejects_untracked_recreation_of_a_deleted_pr_path(tmp_path: Path, path: str) -> None:
    """Reject a user recreation of a file deleted by the PR before claiming verified local source."""
    module = _load_collector()
    source, worktree, base, _old, _head = _setup_repositories(tmp_path)
    _git(source, "rm", path)
    _git(source, "commit", "-m", f"delete {path}")
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/pull/17/head", head)
    _git(worktree, "fetch", "--no-tags", "--refmap=", str(source), "refs/heads/topic")
    _git(worktree, "checkout", "--detach", head)
    recreated = worktree / path
    recreated.parent.mkdir(parents=True, exist_ok=True)
    recreated.write_text("local unreviewed recreation\n", encoding="utf-8")

    code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 2
    assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == (
        "dirty-pr-worktree-before-pr-checkout\n"
    )
    preflight = json.loads((tmp_path / "collected" / "worktree-preflight.json").read_text(encoding="utf-8"))
    assert preflight["dirty_paths"] == [path]
    assert path in preflight["pr_paths"]
    assert preflight["overlapping_pr_paths"] == [path]
    assert not (tmp_path / "collected" / "local-checkout.json").exists()


def test_collect_pr_detaches_at_verified_head_without_changing_local_refs_or_config(tmp_path: Path) -> None:
    """Use the collector's native checkout argv while preserving local branches, refs, and remote configuration."""
    module = _load_collector()
    source, worktree, base, old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", old)
    _git(worktree, "update-ref", "refs/remotes/origin/topic", old)
    remote_url = _git(worktree, "config", "--get", "remote.origin.url")

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
    )

    assert code == 0
    assert ["git", "checkout", "--detach", head] in calls
    assert _git(worktree, "rev-parse", "HEAD") == head
    assert _git(worktree, "branch", "--show-current") == ""
    assert _git(worktree, "rev-parse", "refs/heads/local-diverged") == old
    assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == old
    assert _git(worktree, "config", "--get", "remote.origin.url") == remote_url


def test_collect_pr_remediation_keeps_github_attached_branch(tmp_path: Path) -> None:
    """Require the GitHub checkout route and its branch tracking for remediation."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
        gh_checkout_fails=False,
    )

    assert code == 0
    assert ["gh", "pr", "checkout", "https://github.com/example/project/pull/17"] in calls
    assert _git(worktree, "branch", "--show-current") == "topic"
    assert _git(worktree, "config", "--get", "branch.topic.remote") == "origin"
    assert _git(worktree, "config", "--get", "branch.topic.merge") == "refs/heads/topic"
    checkout = json.loads((tmp_path / "collected" / "local-checkout.json").read_text(encoding="utf-8"))
    assert checkout["checkout_mode"] == "remediate"


def test_collect_pr_remediation_records_fork_tracking_destination(tmp_path: Path) -> None:
    """Keep a fork PR's branch receipt bound to its contributor repository URL."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)

    code, _calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        checkout_mode="remediate",
        gh_checkout_fails=False,
    )

    assert code == 0
    assert _git(worktree, "config", "--get", "branch.topic.remote") == "https://github.com/contributor/project.git"
    assert _git(worktree, "config", "--get", "branch.topic.merge") == "refs/heads/topic"


def test_collect_pr_remediation_recovers_same_repo_existing_branch_after_gh_failure(tmp_path: Path) -> None:
    """Attach the verified existing PR branch after GitHub's checkout fails."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
    )

    assert code == 0
    assert ["git", "checkout", "--no-guess", "topic"] in calls
    assert _git(worktree, "branch", "--show-current") == "topic"
    assert _git(worktree, "rev-parse", "HEAD") == head
    checkout = json.loads((tmp_path / "collected" / "local-checkout.json").read_text(encoding="utf-8"))
    assert checkout["checkout_method"] == "git-original-branch-fallback"
    assert checkout["command"] == "git checkout --no-guess topic"


def test_collect_pr_remediation_creates_missing_same_repo_branch_from_verified_remote_ref(tmp_path: Path) -> None:
    """Create an absent branch only from the exact fetched selected-remote PR ref."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)
    _git(worktree, "branch", "-D", "topic")

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
    )

    assert code == 0
    assert ["git", "checkout", "--track", "-b", "topic", "origin/topic"] in calls
    assert ["git", "update-ref", "refs/remotes/origin/topic", head, "0" * 40] in calls
    assert _git(worktree, "branch", "--show-current") == "topic"
    assert _git(worktree, "rev-parse", "HEAD") == head


def test_collect_pr_remediation_rejects_stale_same_repo_branch_without_advancing_it(tmp_path: Path) -> None:
    """Stop before changing a stale local PR branch after a failed GitHub checkout."""
    module = _load_collector()
    source, worktree, base, old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)
    _git(worktree, "update-ref", "refs/heads/topic", old)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
    )

    assert code == 2
    assert _git(worktree, "rev-parse", "refs/heads/topic") == old
    assert ["git", "checkout", "--no-guess", "topic"] not in calls
    assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == (
        "remediation-existing-branch-not-at-pr-head\n"
    )


def test_collect_pr_remediation_updates_only_ancestor_tracking_ref_for_missing_branch(tmp_path: Path) -> None:
    """Allow the explicit remote-tracking update when its prior commit is a PR-head ancestor."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)
    _git(worktree, "branch", "-D", "topic")
    _git(worktree, "update-ref", "refs/remotes/origin/topic", base)

    code, _calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
    )

    assert code == 0
    assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == head
    assert _git(worktree, "rev-parse", "refs/heads/topic") == head


def test_collect_pr_remediation_uses_recorded_head_after_source_moves(tmp_path: Path) -> None:
    """Prevent a source rewrite after the verified fetch from changing the recovered branch ref."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)
    _git(worktree, "branch", "-D", "topic")
    _git(worktree, "update-ref", "refs/remotes/origin/topic", base)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
        move_source_after_head_fetch=True,
    )

    assert code == 0
    assert _git(source, "rev-parse", "HEAD") != head
    assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == head
    assert _git(worktree, "rev-parse", "refs/heads/topic") == head
    assert not any(arguments[-1] == "refs/heads/topic:refs/remotes/origin/topic" for arguments in calls)


def test_collect_pr_remediation_rejects_divergent_tracking_ref_without_creating_branch(tmp_path: Path) -> None:
    """Preserve a divergent cached tracking ref instead of allowing Git to non-fast-forward it."""
    module = _load_collector()
    source, worktree, base, old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)
    _git(worktree, "branch", "-D", "topic")
    _git(worktree, "update-ref", "refs/remotes/origin/topic", old)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
    )

    assert code == 2
    assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == old
    assert _git(worktree, "branch", "--list", "topic") == ""
    assert not any(arguments[-1] == "refs/heads/topic:refs/remotes/origin/topic" for arguments in calls)
    assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == (
        "remediation-tracking-branch-diverged\n"
    )


def test_collect_pr_remediation_requires_adversarial_recovery_for_fork_after_gh_failure(tmp_path: Path) -> None:
    """Leave a fork checkout unchanged when GitHub cannot establish its publication branch."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        checkout_mode="remediate",
    )

    assert code == 2
    assert _git(worktree, "branch", "--show-current") == "local-diverged"
    assert not any(arguments[:2] == ["git", "checkout"] for arguments in calls)
    assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == (
        "remediation-gh-checkout-recovery-required\n"
    )


def test_collect_pr_remediation_rechecks_dirty_partial_gh_failure_before_git_recovery(tmp_path: Path) -> None:
    """Protect PR files changed by a failed GitHub checkout before native recovery runs."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "local-diverged", base)

    code, calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        checkout_mode="remediate",
        mutate_after_gh_failure=True,
    )

    assert code == 2
    assert not any(arguments[:2] == ["git", "checkout"] for arguments in calls)
    assert (
        (tmp_path / "collected" / "pr-error.txt")
        .read_text(encoding="utf-8")
        .startswith("dirty-pr-worktree-after-pr-checkout")
    )


def test_collect_pr_rechecks_pr_source_after_checkout(tmp_path: Path) -> None:
    """Reject a PR file changed between checkout and the verified-source claim."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    _git(worktree, "checkout", "-b", "base-checkout", base)

    code, _calls = _collect(
        module,
        source,
        worktree,
        tmp_path / "collected",
        base,
        head,
        cross_repository=False,
        mutate_after_checkout=True,
    )

    assert code == 2
    assert (
        (tmp_path / "collected" / "pr-error.txt")
        .read_text(encoding="utf-8")
        .startswith("dirty-pr-worktree-after-pr-checkout")
    )


def test_collect_pr_clears_prior_target_before_an_invalid_retry(tmp_path: Path) -> None:
    """Prevent an invalid retry from retaining a previous PR identity artifact."""
    module = _load_collector()
    output = tmp_path / "collected"
    output.mkdir()
    (output / "pr-target.txt").write_text("17\n", encoding="utf-8")

    code = module.collect_pr(
        target="https://github.com/example/project/issues/17", output=output, checkout=False, timeout_seconds=5
    )

    assert code == 2
    assert not (output / "pr-target.txt").exists()
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "unsafe-pr-target\n"


def test_git_failure_reason_classifies_actual_non_fast_forward_fetch_stderr(tmp_path: Path) -> None:
    """Classify the indented rejection line emitted by a real failed destination fetch."""
    module = _load_collector()
    source, worktree, _base, old, _head = _setup_repositories(tmp_path)
    _git(worktree, "update-ref", "refs/remotes/origin/pull/17/head", old)

    result = subprocess.run(
        [
            "git",
            "fetch",
            "--no-tags",
            str(source),
            "refs/pull/17/head:refs/remotes/origin/pull/17/head",
        ],
        cwd=worktree,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert any(line.lstrip().startswith(b"! [rejected]") for line in result.stderr.splitlines())
    assert module._git_failure_reason(result.stderr) == "ref-update-rejected"


@pytest.mark.parametrize(
    ("stderr", "expected_reason"),
    [
        pytest.param(
            b"fatal: refusing to fetch into current branch\n   ! [rejected] topic -> origin/topic (non-fast-forward)\n",
            "ref-update-rejected",
            id="ref-update-rejected",
        ),
        pytest.param(
            b"fatal: Could not read from remote repository using github_pat_secret\n",
            "unknown",
            id="credential-like-unknown",
        ),
    ],
)
def test_run_classifies_git_failure_without_retaining_stderr(stderr: bytes, expected_reason: str) -> None:
    """Persist only a closed safe reason for Git failure diagnostics."""
    module = _load_collector()

    def run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Return a failed Git process with diagnostic text that must stay in memory."""
        return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=stderr)

    with pytest.raises(module.CollectionError, match="command-failed:pr-head-fetch") as error:
        module._run(run, ["git", "fetch", "origin", "topic"], 5, "pr-head-fetch")

    assert error.value.diagnostics == {
        "exit_code": 1,
        "failure_class": "command-failed",
        "failure_reason": expected_reason,
        "label": "pr-head-fetch",
    }
    assert "github_pat_secret" not in json.dumps(error.value.diagnostics)
