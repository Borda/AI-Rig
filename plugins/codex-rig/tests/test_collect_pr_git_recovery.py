"""Real-Git regressions for collector checkout recovery and source identity."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = PLUGIN_ROOT / "shared" / "collect_pr.py"


def _directory_symlinks_available() -> bool:
    """Probe whether this host can create a directory symlink."""
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary) / "destination"
        destination.mkdir()
        try:
            (Path(temporary) / "link").symlink_to(destination, target_is_directory=True)
        except OSError:
            return False
    return True


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
            return result
        if arguments[:3] == ["git", "worktree", "add"]:
            result = subprocess.run(arguments, cwd=worktree, **kwargs)
            if result.returncode == 0 and mutate_after_checkout:
                (Path(arguments[-2]) / "module.py").write_text("value = 'raced'\n", encoding="utf-8")
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


def test_review_isolates_exact_pr_head_from_dirty_main_checkout(tmp_path: Path) -> None:
    """Inspect the fetched PR commit in a detached worktree without moving local work."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    _git(main, "checkout", "-b", "ongoing", base)
    (main / "module.py").write_text("value = 'local work'\n", encoding="utf-8")
    main_status = _git(main, "status", "--short")
    output = tmp_path / "collected"

    code, calls = _collect(module, source, main, output, base, head)

    assert code == 0
    checkout = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    review = Path(checkout["worktree"])
    assert review == tmp_path / "collected-review-worktree"
    assert review != main
    assert _git(review, "rev-parse", "HEAD") == head
    assert _git(review, "branch", "--show-current") == ""
    assert (review / "module.py").read_text(encoding="utf-8") == "value = 'new'\n"
    assert _git(main, "rev-parse", "HEAD") == base
    assert _git(main, "branch", "--show-current") == "ongoing"
    assert _git(main, "status", "--short") == main_status
    assert (main / "module.py").read_text(encoding="utf-8") == "value = 'local work'\n"
    assert not any(arguments[:3] == ["gh", "pr", "checkout"] for arguments in calls)
    assert (output / "diff.patch").read_text(encoding="utf-8").strip() == _git(
        review, "diff", "--binary", f"{base}...{head}", "--"
    )


def test_review_reuses_only_clean_exact_registered_worktree(tmp_path: Path) -> None:
    """Allow a repeated collection without replacing or deleting the verified worktree."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    output = tmp_path / "collected"
    first_code, _ = _collect(module, source, main, output, base, head)
    assert first_code == 0
    review = tmp_path / "collected-review-worktree"

    second_code, second_calls = _collect(module, source, main, output, base, head)

    assert second_code == 0
    assert not any(call[:3] == ["git", "worktree", "add"] for call in second_calls)
    assert _git(review, "rev-parse", "HEAD") == head
    receipt = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    assert receipt["command"] == "not-run: existing exact clean review worktree"


def test_review_preserves_dirty_registered_worktree_on_retry(tmp_path: Path) -> None:
    """Do not reset or replace local review notes during another collection attempt."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    output = tmp_path / "collected"
    first_code, _ = _collect(module, source, main, output, base, head)
    assert first_code == 0
    review = tmp_path / "collected-review-worktree"
    (review / "module.py").write_text("value = 'review note'\n", encoding="utf-8")

    second_code, second_calls = _collect(module, source, main, output, base, head)

    assert second_code == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "review-worktree-dirty\n"
    assert (review / "module.py").read_text(encoding="utf-8") == "value = 'review note'\n"
    assert not any(call[:3] == ["git", "worktree", "add"] for call in second_calls)
    assert not (output / "local-checkout.json").exists()


def test_review_receipt_survives_report_promotion(tmp_path: Path) -> None:
    """Keep the isolated source address valid after the report directory moves."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    output = tmp_path / "timestamped"
    code, _ = _collect(module, source, main, output, base, head)
    assert code == 0
    promoted = tmp_path / "pr-17" / "run-001"
    promoted.parent.mkdir()

    output.rename(promoted)

    receipt = json.loads((promoted / "local-checkout.json").read_text(encoding="utf-8"))
    review = Path(receipt["worktree"])
    assert review == tmp_path / "timestamped-review-worktree"
    assert _git(review, "rev-parse", "HEAD") == head
    assert (promoted / "diff.patch").is_file()


def test_review_worktree_can_live_beside_ignored_report_inside_source(tmp_path: Path) -> None:
    """Keep report-adjacent review source invisible to the main repository status."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    _git(main, "config", "core.excludesFile", str(tmp_path / "global-ignore"))
    (tmp_path / "global-ignore").write_text(".reports/\n", encoding="utf-8")
    output = main / ".reports" / "code-review" / "run-001"

    code, _ = _collect(module, source, main, output, base, head)

    assert code == 0
    review = main / ".reports" / "code-review" / "run-001-review-worktree"
    assert _git(review, "rev-parse", "HEAD") == head
    assert _git(main, "status", "--short") == ""


def test_review_uses_temporary_worktree_when_report_path_is_visible_to_main_git(tmp_path: Path) -> None:
    """Move isolated review source out of the main tree when reports are not ignored."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    output = main / "reports" / "run-001"

    code, calls = _collect(module, source, main, output, base, head)

    assert code == 0
    receipt = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(output.resolve().as_posix().encode("utf-8")).hexdigest()[:16]
    review = Path(receipt["worktree"])
    assert receipt["worktree_location"] == "temporary-fallback"
    assert review == Path(tempfile.gettempdir()).resolve() / f"codex-pr-review-{digest}-review-worktree"
    assert _git(review, "rev-parse", "HEAD") == head
    assert ["git", "worktree", "add", "--detach", str(review), head] in calls
    assert not (main / "reports" / "run-001-review-worktree").exists()


def test_review_preserves_occupied_temporary_fallback_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never replace an unregistered path at the deterministic temporary address."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(external))
    output = main / "reports" / "run-001"
    digest = hashlib.sha256(output.resolve().as_posix().encode("utf-8")).hexdigest()[:16]
    occupied = external / f"codex-pr-review-{digest}-review-worktree"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("not collector-owned\n", encoding="utf-8")

    code, calls = _collect(module, source, main, output, base, head)

    assert code == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "review-worktree-path-not-registered\n"
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "not collector-owned\n"
    assert not any(call[:3] == ["git", "worktree", "add"] for call in calls)


def test_review_preserves_occupied_or_dirty_worktree_path(tmp_path: Path) -> None:
    """Fail closed without replacing files at a report-adjacent path."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    review = tmp_path / "collected-review-worktree"
    review.mkdir()
    (review / "keep.txt").write_text("user data\n", encoding="utf-8")
    output = tmp_path / "collected"

    occupied_code, _ = _collect(module, source, main, output, base, head)

    assert occupied_code == 2
    assert (output / "pr-error.txt").read_text(encoding="utf-8") == "review-worktree-path-not-registered\n"
    assert (review / "keep.txt").read_text(encoding="utf-8") == "user data\n"


@pytest.mark.skipif(not _directory_symlinks_available(), reason="directory symlinks unavailable")
def test_review_rejects_symlink_worktree_path(tmp_path: Path) -> None:
    """Do not follow a user-owned symlink when selecting the detached worktree path."""
    module = _load_collector()
    source, main, base, _old, head = _setup_repositories(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    review = tmp_path / "collected-review-worktree"
    review.symlink_to(destination, target_is_directory=True)

    code, _ = _collect(module, source, main, tmp_path / "collected", base, head)

    assert code == 2
    assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == "review-worktree-path-is-symlink\n"
    assert review.is_symlink()
    assert list(destination.iterdir()) == []


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
def test_collect_pr_isolates_pr_file_changes_or_unmerged_index_at_matching_head(tmp_path: Path, state: str) -> None:
    """Preserve conflicted or changed main files while reviewing an isolated exact commit."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    if state in {"unstaged", "staged"}:
        (worktree / "module.py").write_text("value = 'local-unreviewed'\n", encoding="utf-8")
        if state == "staged":
            _git(worktree, "add", "module.py")
    else:
        _git(worktree, "merge", "--no-commit", "old-topic", expected=1)

    code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 0
    source_context = json.loads((tmp_path / "collected" / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert source_context["pr_paths"] == ["module.py"]
    if state == "unmerged":
        assert source_context["unmerged_paths"] == ["module.py"]
    else:
        assert source_context["overlapping_pr_paths"] == ["module.py"]
    isolated = Path(json.loads((tmp_path / "collected" / "local-checkout.json").read_text())["worktree"])
    assert _git(isolated, "rev-parse", "HEAD") == head
    assert _git(isolated, "status", "--short") == ""


def test_collect_pr_isolates_staged_pr_change_hidden_by_restored_worktree(tmp_path: Path) -> None:
    """Keep a staged local change while reviewing clean PR source elsewhere."""
    module = _load_collector()
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    staged_contents = "value = 'local-staged'\n"
    (worktree / "module.py").write_text(staged_contents, encoding="utf-8")
    _git(worktree, "add", "module.py")
    (worktree / "module.py").write_text("value = 'new'\n", encoding="utf-8")

    code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

    assert code == 0
    assert _git(worktree, "show", ":module.py") == staged_contents.strip()
    preflight = json.loads((tmp_path / "collected" / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert preflight["dirty_paths"] == ["module.py"]
    assert preflight["overlapping_pr_paths"] == ["module.py"]
    isolated = Path(json.loads((tmp_path / "collected" / "local-checkout.json").read_text())["worktree"])
    assert _git(isolated, "status", "--short") == ""


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
    preflight = json.loads((tmp_path / "collected" / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert preflight["dirty_paths"] == ["scratch.txt"]
    assert preflight["overlapping_pr_paths"] == []
    assert (worktree / "scratch.txt").read_text(encoding="utf-8") == "keep local scratch\n"


@pytest.mark.parametrize("path", ["notes.txt", "removed/nested.py"])
def test_collect_pr_isolates_untracked_recreation_of_a_deleted_pr_path(tmp_path: Path, path: str) -> None:
    """Preserve a recreated local path while reviewing the exact deletion in isolation."""
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

    assert code == 0
    preflight = json.loads((tmp_path / "collected" / "source-worktree-context.json").read_text(encoding="utf-8"))
    assert preflight["dirty_paths"] == [path]
    assert path in preflight["pr_paths"]
    assert preflight["overlapping_pr_paths"] == [path]
    isolated = Path(json.loads((tmp_path / "collected" / "local-checkout.json").read_text())["worktree"])
    assert not (isolated / path).exists()
    assert recreated.read_text(encoding="utf-8") == "local unreviewed recreation\n"


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
    assert ["git", "worktree", "add", "--detach", str(tmp_path / "collected-review-worktree"), head] in calls
    assert _git(worktree, "rev-parse", "HEAD") == old
    assert _git(worktree, "branch", "--show-current") == "local-diverged"
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
    """Reject an isolated PR file changed between creation and source verification."""
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
    assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == "review-worktree-dirty\n"


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
