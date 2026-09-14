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
        if arguments[:3] == ["git", "checkout", "--detach"]:
            result = subprocess.run(arguments, cwd=worktree, **kwargs)
            if mutate_after_checkout:
                (worktree / "module.py").write_text("value = 'raced'\n", encoding="utf-8")
            return result
        assert arguments[0] != "gh", arguments
        if arguments[:2] == ["git", "fetch"]:
            arguments = [str(source) if argument == "origin" else argument for argument in arguments]
        return subprocess.run(arguments, cwd=worktree, **kwargs)

    previous_which = module.shutil.which
    module.shutil.which = lambda command: f"/fixture/{command}"
    try:
        code = module.collect_pr(
            target="https://github.com/example/project/pull/17",
            output=output,
            checkout=True,
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
