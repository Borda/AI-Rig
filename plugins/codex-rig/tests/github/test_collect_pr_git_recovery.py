"""Real-Git regressions for collector checkout recovery and source identity."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = PLUGIN_ROOT / "shared" / "collect_pr.py"


def _case_insensitive_filesystem() -> bool | None:
    """Probe native filename case identity on the temporary-file filesystem."""
    try:
        with tempfile.TemporaryDirectory() as temporary:
            lower = Path(temporary) / "collector-case-probe"
            lower.write_bytes(b"probe")
            return (Path(temporary) / "COLLECTOR-CASE-PROBE").exists()
    except OSError:
        return None


_CASE_INSENSITIVE_FILESYSTEM = _case_insensitive_filesystem()
_skip_without_case_aliases = pytest.mark.skipif(
    _CASE_INSENSITIVE_FILESYSTEM is not True, reason="temporary filesystem does not alias filename case"
)
_skip_without_distinct_case_names = pytest.mark.skipif(
    _CASE_INSENSITIVE_FILESYSTEM is not False, reason="temporary filesystem does not preserve distinct case names"
)


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


def _file_links_available(kind: str) -> bool:
    """Probe whether this host can create the requested kind of file link."""
    with tempfile.TemporaryDirectory() as temporary:
        target = Path(temporary) / "target"
        target.write_bytes(b"probe")
        link = Path(temporary) / "link"
        try:
            if kind == "hardlink":
                link.hardlink_to(target)
            else:
                link.symlink_to(target)
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
    return load_module("collector_git_recovery", COLLECTOR)


@dataclass(frozen=True)
class RepositoryTemplate:
    """Committed PR source and worktree pair built once per session, never mutated after the build.

    Building the pair costs about twenty Git processes, which dominates these tests on Windows. Each test copies the
    pair into its own directory instead, so a test can mutate its copy freely while every test still starts from byte-
    identical repositories and the same commit identities.
    """

    root: Path
    base: str
    old: str
    head: str


def _build_repository_template(root: Path) -> RepositoryTemplate:
    """Create copyable PR repositories with background maintenance disabled before writes."""
    source = root / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "maintenance.auto", "false")
    _git(source, "config", "core.autocrlf", "false")
    _git(source, "config", "user.name", "Collector fixture")
    _git(source, "config", "user.email", "collector@example.invalid")
    (source / "module.py").write_text("value = 'base'\n", encoding="utf-8", newline="\n")
    (source / "notes.txt").write_text("keep me\n", encoding="utf-8", newline="\n")
    removed_directory = source / "removed"
    removed_directory.mkdir()
    (removed_directory / "nested.py").write_text("value = 'removed'\n", encoding="utf-8", newline="\n")
    _git(source, "add", "module.py", "notes.txt", "removed/nested.py")
    _git(source, "commit", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "-b", "old-topic")
    (source / "module.py").write_text("value = 'old'\n", encoding="utf-8", newline="\n")
    _git(source, "commit", "-am", "old topic")
    old = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "-b", "topic", base)
    (source / "module.py").write_text("value = 'new'\n", encoding="utf-8", newline="\n")
    _git(source, "commit", "-am", "new topic")
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "update-ref", "refs/pull/17/head", head)

    worktree = root / "worktree"
    worktree.mkdir()
    _git(worktree, "init", "-b", "main")
    _git(worktree, "config", "maintenance.auto", "false")
    _git(worktree, "config", "core.autocrlf", "false")
    _git(worktree, "config", "user.name", "Collector fixture")
    _git(worktree, "config", "user.email", "collector@example.invalid")
    _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/topic:refs/heads/topic")
    _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/old-topic:refs/heads/old-topic")
    _git(worktree, "checkout", "topic")
    _git(worktree, "config", "remote.origin.url", "https://github.com/example/project.git")
    _git(worktree, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    return RepositoryTemplate(root=root, base=base, old=old, head=head)


@pytest.fixture(scope="session")
def repository_template(tmp_path_factory: pytest.TempPathFactory) -> RepositoryTemplate:
    """Build the shared PR repositories once per test process; tests receive private copies."""
    return _build_repository_template(tmp_path_factory.mktemp("collector-git-template"))


def _setup_repositories(tmp_path: Path, template: RepositoryTemplate) -> tuple[Path, Path, str, str, str]:
    """Copy the PR source and its checked-out worktree into this test's private directory.

    The copies keep file bytes, Git objects, refs, and local configuration; only filesystem stat data differs, which Git
    revalidates by content on its next index refresh.
    """
    source = tmp_path / "source"
    worktree = tmp_path / "worktree"
    shutil.copytree(template.root / "source", source, symlinks=True)
    shutil.copytree(template.root / "worktree", worktree, symlinks=True)
    return source, worktree, template.base, template.old, template.head


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
    switch_source_after_checkout: bool = False,
    checkout_mode: str = "review",
    gh_checkout_fails: bool = True,
    gh_local_branch: str = "topic",
    gh_pull_ref_tracking: bool = False,
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
            if gh_local_branch != "topic":
                _git(worktree, "branch", gh_local_branch, head)
            checkout = subprocess.run(["git", "checkout", gh_local_branch], cwd=worktree, **kwargs)
            if checkout.returncode == 0:
                tracking_remote = (
                    "https://github.com/contributor/project.git"
                    if cross_repository and not gh_pull_ref_tracking
                    else "origin"
                )
                _git(worktree, "config", f"branch.{gh_local_branch}.remote", tracking_remote)
                _git(
                    worktree,
                    "config",
                    f"branch.{gh_local_branch}.merge",
                    "refs/pull/17/head" if gh_pull_ref_tracking else "refs/heads/topic",
                )
            return checkout
        if arguments[:3] == ["git", "checkout", "--detach"]:
            result = subprocess.run(arguments, cwd=worktree, **kwargs)
            return result
        if arguments[:3] == ["git", "worktree", "add"]:
            result = subprocess.run(arguments, cwd=worktree, **kwargs)
            if result.returncode == 0 and switch_source_after_checkout:
                _git(worktree, "switch", "old-topic")
            if result.returncode == 0 and mutate_after_checkout:
                (Path(arguments[-2]) / "module.py").write_text("value = 'raced'\n", encoding="utf-8")
            return result
        assert arguments[0] != "gh", arguments
        if arguments[:2] in (["git", "fetch"], ["git", "ls-remote"]):
            is_same_repo_head_fetch = arguments[-1] == head and "--refmap=" in arguments
            arguments = [str(source) if argument == "origin" else argument for argument in arguments]
            result = subprocess.run(arguments, cwd=worktree, **kwargs)
            if move_source_after_head_fetch and is_same_repo_head_fetch:
                (source / "module.py").write_text("value = 'source moved'\n", encoding="utf-8")
                _git(source, "commit", "-am", "source moved")
            return result
        return subprocess.run(arguments, cwd=worktree, **kwargs)

    with mock.patch.object(module.shutil, "which", side_effect=lambda command: f"/fixture/{command}"):
        code = module.collect_pr(
            target="https://github.com/example/project/pull/17",
            output=output,
            checkout=True,
            checkout_mode=checkout_mode,
            timeout_seconds=10,
            run=run,
        )
    return code, calls


def _edit_module(worktree: Path) -> None:
    """Leave an unstaged local edit to the file the PR changes."""
    (worktree / "module.py").write_text("value = 'local-unreviewed'\n", encoding="utf-8")


def _stage_module_edit(worktree: Path) -> None:
    """Stage a local edit to the file the PR changes."""
    _edit_module(worktree)
    _git(worktree, "add", "module.py")


def _start_conflicting_merge(worktree: Path) -> None:
    """Leave an unfinished merge that conflicts on the file the PR changes."""
    _git(worktree, "merge", "--no-commit", "old-topic", expected=1)


def _remediate(*arguments: Any, **options: Any) -> tuple[int, list[list[str]]]:
    """Run ``_collect`` in remediation mode; the other options stay visible at each call site."""
    return _collect(*arguments, checkout_mode="remediate", **options)


_IGNORED_TREE_FILES = 400


def _write_ignored_tree(worktree: Path, pattern: str) -> None:
    """Create a large ignored environment tree, the shape that once filled preflight records with ~20K paths."""
    (worktree / ".git" / "info" / "exclude").write_text(pattern, encoding="utf-8")
    package = worktree / ".venv" / "lib" / "site-packages"
    package.mkdir(parents=True)
    for index in range(_IGNORED_TREE_FILES):
        (package / f"module_{index:04d}.py").write_text("x = 1\n", encoding="utf-8")


class TestReviewWorktree:
    """Review-mode checkout in a detached worktree beside the report, against real Git repositories."""

    def test_isolates_exact_pr_head_from_dirty_main_checkout(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Inspect the fetched PR commit in a detached worktree without moving local work.

        The main checkout sits on a local branch with an uncommitted edit while the PR head exists only in a fetched
        commit. Review must check the exact head out in a separate detached worktree and leave the branch, HEAD and edit
        of the main checkout untouched, without ever calling gh pr checkout.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
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

    def test_reuses_only_clean_exact_registered_worktree(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Allow a repeated collection without replacing or deleting the verified worktree.

        A first collection created the review worktree. Collecting again with the same output path must keep that clean,
        exact worktree rather than adding or deleting one, and the receipt says the command was not run.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
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

    def test_preserves_dirty_registered_worktree_on_retry(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Do not reset or replace local review notes during another collection attempt.

        A reviewer edited a file in the review worktree after the first collection. A second collection must stop with
        review-worktree-dirty and leave the note in place, because resetting would destroy local review work.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
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

    def test_receipt_survives_report_promotion(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Keep the isolated source address valid after the report directory moves.

        Reports are collected into a timestamped directory and later renamed into a run folder. The receipt must still
        point at the isolated worktree, which lives beside the original report name and keeps the head.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
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

    def test_lives_beside_ignored_report_inside_source(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Keep report-adjacent review source invisible to the main repository status.

        The report directory sits inside the main repository but is ignored through a global excludes file. The review
        worktree may then live next to it, and the main repository status must stay empty so it does not look dirty.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(main, "config", "core.excludesFile", str(tmp_path / "global-ignore"))
        (tmp_path / "global-ignore").write_text(".reports/\n", encoding="utf-8")
        output = main / ".reports" / "code-review" / "run-001"

        code, _ = _collect(module, source, main, output, base, head)

        assert code == 0
        review = main / ".reports" / "code-review" / "run-001-review-worktree"
        assert _git(review, "rev-parse", "HEAD") == head
        assert _git(main, "status", "--short") == ""

    def test_falls_back_to_temporary_when_report_is_visible(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Move isolated review source out of the main tree when reports are not ignored.

        The report directory is inside the main repository and not ignored, so a sibling worktree there would show up in
        git status. The worktree must move to a deterministic temporary path derived from the report path and the
        receipt records the fallback.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
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

    def test_preserves_occupied_temporary_fallback(
        self, tmp_path: Path, repository_template: RepositoryTemplate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never replace an unregistered path at the deterministic temporary address.

        Something already exists at the deterministic temporary path and Git does not know it as a worktree. The
        collector must fail with review-worktree-path-not-registered and keep the unrelated file, never delete a path it
        did not create.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
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

    def test_preserves_occupied_report_adjacent_path(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Fail closed without replacing files at a report-adjacent path.

        A directory with user data already sits where the review worktree would go. Review must fail closed with review-
        worktree-path-not-registered and leave that data intact.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
        review = tmp_path / "collected-review-worktree"
        review.mkdir()
        (review / "keep.txt").write_text("user data\n", encoding="utf-8")
        output = tmp_path / "collected"

        occupied_code, _ = _collect(module, source, main, output, base, head)

        assert occupied_code == 2
        assert (output / "pr-error.txt").read_text(encoding="utf-8") == "review-worktree-path-not-registered\n"
        assert (review / "keep.txt").read_text(encoding="utf-8") == "user data\n"

    @pytest.mark.skipif(not _directory_symlinks_available(), reason="directory symlinks unavailable")
    def test_rejects_symlink_path(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Do not follow a user-owned symlink when selecting the detached worktree path.

        The report-adjacent worktree path is a user symlink to another directory. Following it could write PR files into
        the link target, so the collector fails with review-worktree-path-is-symlink and leaves both untouched.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
        destination = tmp_path / "destination"
        destination.mkdir()
        review = tmp_path / "collected-review-worktree"
        review.symlink_to(destination, target_is_directory=True)

        code, _ = _collect(module, source, main, tmp_path / "collected", base, head)

        assert code == 2
        assert (tmp_path / "collected" / "pr-error.txt").read_text(
            encoding="utf-8"
        ) == "review-worktree-path-is-symlink\n"
        assert review.is_symlink()
        assert list(destination.iterdir()) == []

    @pytest.mark.parametrize("mutate_after_checkout", [False, True])
    def test_oversized_context_keeps_isolated_source_checks(
        self,
        tmp_path: Path,
        repository_template: RepositoryTemplate,
        monkeypatch: pytest.MonkeyPatch,
        mutate_after_checkout: bool,
    ) -> None:
        """Retain oversized main-tree context while still rejecting dirty isolated source.

        The main tree holds a large ignored environment and Git output is capped at 8 KB, so the main-tree context
        record remains complete and sampled. Review must still isolate the exact head; a copy modified immediately after
        creation must still be rejected as review-worktree-dirty.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _write_ignored_tree(worktree, ".venv/\n")
        # Exercise the real Git output boundary without creating a machine-sized environment in every test.
        monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 8192)
        main_head = _git(worktree, "rev-parse", "HEAD")
        output = tmp_path / "collected"

        code, calls = _collect(
            module, source, worktree, output, base, head, mutate_after_checkout=mutate_after_checkout
        )

        assert code == (2 if mutate_after_checkout else 0)
        context = json.loads((output / "source-worktree-context.json").read_text(encoding="utf-8"))
        assert context["status"] == "safe-unrelated-dirty-paths"
        assert context["dirty_path_counts"]["ignored"] == _IGNORED_TREE_FILES
        assert module.dirty_path_record_is_consistent(context)
        assert any(call[:3] == ["git", "worktree", "add"] for call in calls)
        assert _git(worktree, "rev-parse", "HEAD") == main_head
        assert len(list((worktree / ".venv/lib/site-packages").iterdir())) == _IGNORED_TREE_FILES
        if mutate_after_checkout:
            assert (output / "pr-error.txt").read_text(encoding="utf-8") == "review-worktree-dirty\n"
            assert not (output / "local-checkout.json").exists()
        else:
            checkout = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
            review = Path(checkout["worktree"])
            assert _git(review, "rev-parse", "HEAD") == head
            assert _git(review, "status", "--short") == ""
            assert (output / "diff.patch").read_text(encoding="utf-8").strip() == _git(
                review, "diff", "--binary", f"{base}...{head}", "--"
            )

    def test_detaches_at_verified_head_leaving_refs_and_config(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Use the collector's native checkout argv while preserving local branches, refs, and remote configuration.

        The main tree sits on a diverged local branch and the cached tracking ref is stale. Review adds a detached
        worktree at the verified head and must not move any local branch or ref or change the remote configuration.
        """
        module = _load_collector()
        source, worktree, base, old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", old)
        _git(worktree, "update-ref", "refs/remotes/origin/topic", old)
        remote_url = _git(worktree, "config", "--get", "remote.origin.url")

        code, calls = _collect(module, source, worktree, tmp_path / "collected", base, head, cross_repository=False)

        assert code == 0
        assert ["git", "worktree", "add", "--detach", str(tmp_path / "collected-review-worktree"), head] in calls
        assert _git(worktree, "rev-parse", "HEAD") == old
        assert _git(worktree, "branch", "--show-current") == "local-diverged"
        assert _git(worktree, "rev-parse", "refs/heads/local-diverged") == old
        assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == old
        assert _git(worktree, "config", "--get", "remote.origin.url") == remote_url

    def test_rechecks_source_after_checkout(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Reject an isolated PR file changed between creation and source verification.

        A PR file in the freshly created review worktree is modified right after creation (a race). Source verification
        must reject it with review-worktree-dirty rather than review changed bytes.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
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


class TestPrPatch:
    """The collected PR patch, checked through the review-preparation producer."""

    @pytest.mark.parametrize("driver", ["external", "textconv"])
    def test_ignores_configured_diff_drivers(
        self, tmp_path: Path, repository_template: RepositoryTemplate, monkeypatch: pytest.MonkeyPatch, driver: str
    ) -> None:
        """Collect the exact PR patch without executing configured external or text conversion drivers.

        The repository configures an external diff program or a textconv filter that would write a marker file if run.
        The collector must produce the canonical patch with --no-ext-diff --no-textconv, so the marker never appears and
        the patch still binds to the reviewed source snapshot.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
        marker = tmp_path / "diff-driver-ran"
        script = tmp_path / "diff-driver.py"
        script.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "print('diff --git a/module.py b/module.py\\n--- a/module.py\\n+++ b/module.py\\n"
            "@@ -1 +1 @@\\n-value = 1\\n+value = 99')\n",
            encoding="utf-8",
            newline="\n",
        )
        command = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
        _git(main, "config", "diff.external" if driver == "external" else "diff.fixture.textconv", command)
        if driver == "textconv":
            (main / ".git/info/attributes").write_text("module.py diff=fixture\n", encoding="utf-8", newline="\n")
        output = tmp_path / "collected"

        code, _ = _collect(module, source, main, output, base, head)

        assert code == 0
        checkout = json.loads((output / "local-checkout.json").read_bytes())
        review = Path(checkout["worktree"])
        expected = _git(review, "diff", "--binary", "--no-ext-diff", "--no-textconv", f"{base}...{head}", "--")
        assert (output / "diff.patch").read_text(encoding="utf-8").strip() == expected
        assert "value = 'new'" in expected
        assert "--no-ext-diff --no-textconv" in checkout["diff_command"]
        monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
        monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "skills/code-review"))
        producer = importlib.import_module("review_prepare")
        snapshot, changed, comparison_base = producer._source_snapshot(output, review, head, ["module.py"], None, None)
        assert changed == {"module.py"}
        assert comparison_base == base
        assert snapshot["files"][0]["content"] == "value = 'new'\n"
        assert not marker.exists()

    @pytest.mark.parametrize(
        ("setting", "value"),
        [
            pytest.param("diff.noprefix", "true", id="no-prefix"),
            pytest.param("color.diff", "always", id="forced-color"),
        ],
    )
    def test_is_canonical_through_preparation(
        self,
        tmp_path: Path,
        repository_template: RepositoryTemplate,
        monkeypatch: pytest.MonkeyPatch,
        setting: str,
        value: str,
    ) -> None:
        """Preserve verified PR patch inventory and source binding despite Git display configuration.

        The PR adds a binary file and a mode change, and the main repository sets display options (no-prefix paths,
        forced colour) that would change plain git diff output. The stored patch must stay plain, keep every path
        section, and be rejected as stale once its content no longer matches the head.
        """
        module = _load_collector()
        source, main, base, _old, head = _setup_repositories(tmp_path, repository_template)
        (source / "binary.dat").write_bytes(b"\x00new binary")
        _git(source, "add", "binary.dat")
        _git(source, "update-index", "--chmod=+x", "notes.txt")
        _git(source, "commit", "-m", "binary and mode changes")
        head = _git(source, "rev-parse", "HEAD")
        _git(source, "update-ref", "refs/pull/17/head", head)
        paths = ["module.py", "binary.dat", "notes.txt"]
        _git(main, "config", setting, value)
        output = tmp_path / "collected"
        code, calls = _collect(module, source, main, output, base, head)
        assert code == 0
        checkout = json.loads((output / "local-checkout.json").read_bytes())
        review = Path(checkout["worktree"])
        monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
        monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "skills/code-review"))
        producer = importlib.import_module("review_prepare")
        patch = (output / "diff.patch").read_bytes()
        assert b"\x1b[" not in patch
        assert set(producer._diff_sections(patch)) == set(paths)
        assert producer.review_routing.read_review_paths(output, "files.txt") == set(paths)
        assert b"GIT binary patch" in patch
        assert b"old mode 100644\nnew mode 100755" in patch
        diff_call = next(call for call in calls if "--binary" in call)
        assert checkout["diff_command"] == " ".join(diff_call)
        assert producer._source_snapshot(output, review, head, paths, None, None)[1] == set(paths)
        _git(main, "config", "diff.noprefix", "false")
        _git(main, "config", "color.diff", "never")
        assert producer._source_snapshot(output, review, head, paths, None, None)[1] == set(paths)
        (output / "diff.patch").write_bytes(patch.replace(b"+value = 'new'", b"+value = 'wrong'"))
        with pytest.raises(ValueError, match="review-source-diff-stale"):
            producer._source_snapshot(output, review, head, paths, None, None)


class TestStaleCacheFetch:
    """Exact PR fetches that must never rewrite a stale cached ref."""

    @pytest.mark.parametrize(
        ("cross_repository", "state"),
        [
            pytest.param(False, "OPEN", id="same-repository-open"),
            pytest.param(True, "OPEN", id="fork-open"),
            pytest.param(False, "MERGED", id="historical-same-repository"),
        ],
    )
    def test_head_fetch_leaves_stale_ref(
        self, tmp_path: Path, repository_template: RepositoryTemplate, cross_repository: bool, state: str
    ) -> None:
        """Verify every Git route can inspect an exact PR head without updating a stale cache ref.

        The cached tracking ref for the PR points at an older commit. For a same-repository open PR, a fork, and a
        merged PR alike, the advertised head is fetched without a destination or FETCH_HEAD write. The stale cache ref
        remains unchanged while the exact head is inspected.
        """
        module = _load_collector()
        source, worktree, base, old, head = _setup_repositories(tmp_path, repository_template)
        stale_ref = (
            "refs/remotes/origin/topic"
            if not cross_repository and state == "OPEN"
            else "refs/remotes/origin/pull/17/head"
        )
        _git(worktree, "update-ref", stale_ref, old)

        code, calls = _collect(
            module, source, worktree, tmp_path / "collected", base, head, cross_repository=cross_repository, state=state
        )

        assert code == 0
        assert _git(worktree, "rev-parse", stale_ref) == old
        assert any(arguments[:4] == ["git", "fetch", "--no-tags", "--refmap="] for arguments in calls)
        fetched = json.loads((tmp_path / "collected" / "pr-head-fetch.json").read_text(encoding="utf-8"))
        assert fetched["remote_ref"] == head
        assert fetched["local_head"] == head

    def test_target_fetch_leaves_stale_ref(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Verify an old target cache cannot block a current exact PR checkout.

        The cached origin/main ref is stale. The base commit must be fetched destination-free and recorded in the
        target-branch artifact so an old cache cannot block the checkout or be rewritten.
        """
        module = _load_collector()
        source, worktree, base, old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "update-ref", "refs/remotes/origin/main", old)

        code, calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

        assert code == 0
        assert _git(worktree, "rev-parse", "refs/remotes/origin/main") == old
        target = json.loads((tmp_path / "collected" / "target-branch.json").read_text(encoding="utf-8"))
        assert target["remote_ref"] == base
        assert any(arguments[:4] == ["git", "fetch", "--no-tags", "--refmap="] for arguments in calls)


class TestSourceWorktreeContext:
    """Main-tree state recorded before the PR source is isolated, with the main tree left untouched."""

    @pytest.mark.parametrize(
        ("dirty", "record_key"),
        [
            pytest.param(_edit_module, "overlapping_pr_paths", id="unstaged"),
            pytest.param(_stage_module_edit, "overlapping_pr_paths", id="staged"),
            pytest.param(_start_conflicting_merge, "unmerged_paths", id="unmerged"),
        ],
    )
    def test_isolates_changed_or_unmerged_pr_file(
        self, tmp_path: Path, repository_template: RepositoryTemplate, dirty: Callable[[Path], None], record_key: str
    ) -> None:
        """Preserve conflicted or changed main files while reviewing an isolated exact commit.

        The main tree already has the PR head checked out but module.py, a file in the PR diff, is edited, staged or in
        an unmerged merge. The context record lists the overlap (or the unmerged path) and the review happens in a clean
        isolated worktree.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        dirty(worktree)

        code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

        assert code == 0
        source_context = json.loads(
            (tmp_path / "collected" / "source-worktree-context.json").read_text(encoding="utf-8")
        )
        assert source_context["pr_paths"] == ["module.py"]
        assert source_context[record_key] == ["module.py"]
        isolated = Path(json.loads((tmp_path / "collected" / "local-checkout.json").read_text())["worktree"])
        assert _git(isolated, "rev-parse", "HEAD") == head
        assert _git(isolated, "status", "--short") == ""

    def test_isolates_staged_change_hidden_by_restored_file(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Keep a staged local change while reviewing clean PR source elsewhere.

        A different version of module.py is staged while the working file was restored to the PR content, so git status
        looks only partly dirty. The staged blob must be preserved and reported as dirty and overlapping while review
        runs elsewhere.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
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

    def test_preserves_unrelated_edit(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Allow a local edit outside the PR diff without changing or misreporting it.

        The main tree has the PR head checked out with a local edit to notes.txt, which is not part of the PR diff.
        Collection succeeds and the edit stays byte for byte.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        (worktree / "notes.txt").write_text("keep local edit\n", encoding="utf-8")

        code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

        assert code == 0
        assert (worktree / "notes.txt").read_text(encoding="utf-8") == "keep local edit\n"

    def test_preserves_unrelated_untracked_file(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Allow an untracked file that is outside the PR diff without expanding review scope.

        An untracked scratch file exists in the main tree outside the PR diff. Collection succeeds, the record lists it
        as dirty with no overlap, and the file remains.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        (worktree / "scratch.txt").write_text("keep local scratch\n", encoding="utf-8")

        code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

        assert code == 0
        preflight = json.loads((tmp_path / "collected" / "source-worktree-context.json").read_text(encoding="utf-8"))
        assert preflight["dirty_paths"] == ["scratch.txt"]
        assert preflight["overlapping_pr_paths"] == []
        assert (worktree / "scratch.txt").read_text(encoding="utf-8") == "keep local scratch\n"

    def test_bounds_sample_for_large_ignored_tree(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Count a large ignored tree instead of listing it, while still comparing every path for overlap.

        The record keeps the unrelated user scratch file ahead of ignored environment files, lists at most the sample
        limit, and accounts for every other dirty path in its counts.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _write_ignored_tree(worktree, ".venv/\n")
        (worktree / "scratch.txt").write_text("keep local scratch\n", encoding="utf-8")

        code, _calls = _collect(module, source, worktree, tmp_path / "collected", base, head)

        record_path = tmp_path / "collected" / "source-worktree-context.json"
        preflight = json.loads(record_path.read_text(encoding="utf-8"))
        assert code == 0
        assert (preflight["schema_version"], preflight["status"]) == (1, "safe-unrelated-dirty-paths")
        assert preflight["dirty_path_counts"] == {
            "tracked": 0,
            "staged": 0,
            "untracked_unignored": 1,
            "ignored": _IGNORED_TREE_FILES,
            "total": _IGNORED_TREE_FILES + 1,
        }
        assert len(preflight["dirty_paths"]) == module.DIRTY_PATH_SAMPLE_LIMIT
        assert "scratch.txt" in preflight["dirty_paths"]
        assert preflight["dirty_paths_omitted"] == _IGNORED_TREE_FILES + 1 - module.DIRTY_PATH_SAMPLE_LIMIT
        assert module.dirty_path_record_is_consistent(preflight)
        assert record_path.stat().st_size < 8 * 1024

    @pytest.mark.parametrize("path", ["notes.txt", "removed/nested.py"])
    def test_isolates_recreation_of_deleted_pr_path(
        self, tmp_path: Path, repository_template: RepositoryTemplate, path: str
    ) -> None:
        """Preserve a recreated local path while reviewing the exact deletion in isolation.

        The PR deletes a path, and the main tree has the head checked out and then recreates that path locally. The
        recreation is reported as dirty and overlapping, kept in the main tree, and absent from the isolated worktree
        that shows the deletion.
        """
        module = _load_collector()
        source, worktree, base, _old, _head = _setup_repositories(tmp_path, repository_template)
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

    @pytest.mark.parametrize("checkout_mode", ["review", "remediate"])
    def test_preserves_ignored_nested_worktree(
        self, tmp_path: Path, repository_template: RepositoryTemplate, checkout_mode: str
    ) -> None:
        """Allow an unrelated prior review worktree reported by Git with a directory suffix.

        An ignored directory holds a nested worktree that Git lists with a trailing slash among untracked entries and
        that contains a private file. Neither mode may disturb it; remediation also moves the main tree onto the PR
        branch, review keeps the base.
        """
        module = _load_collector()
        source, worktree, base, old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        nested = worktree / "cache" / "review"
        _git(worktree, "worktree", "add", "--detach", str(nested), old)
        (worktree / ".git" / "info" / "exclude").write_text("cache/\n", encoding="utf-8")
        preserved = nested / "private.txt"
        preserved.write_bytes(b"USER BYTES\n")
        assert "cache/review/\0" in _git(worktree, "ls-files", "--others", "-z")
        pr_checkout = ["gh", "pr", "checkout", "https://github.com/example/project/pull/17"]
        output = tmp_path / "collected"

        code, calls = _collect(
            module,
            source,
            worktree,
            output,
            base,
            head,
            cross_repository=False,
            checkout_mode=checkout_mode,
            gh_checkout_fails=False,
        )

        assert code == 0
        assert preserved.read_bytes() == b"USER BYTES\n"
        assert _git(nested, "rev-parse", "HEAD") == old
        checkout = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
        assert checkout["checkout_mode"] == checkout_mode
        assert _git(worktree, "rev-parse", "HEAD") == {"remediate": head, "review": base}[checkout_mode]
        assert (
            _git(worktree, "branch", "--show-current")
            == {"remediate": "topic", "review": "local-diverged"}[checkout_mode]
        )
        assert (pr_checkout in calls) is (checkout_mode == "remediate")

    @pytest.mark.parametrize("pr_path", ["cache", "cache/review", "cache/review/new.py"])
    @pytest.mark.parametrize("checkout_mode", ["review", "remediate"])
    def test_blocks_checkout_over_nested_worktree(
        self, tmp_path: Path, repository_template: RepositoryTemplate, pr_path: str, checkout_mode: str
    ) -> None:
        """Retain directory overlap protection for Git's collapsed nested worktree entry.

        The PR adds a file at, under or at the parent of the path holding an ignored nested worktree. Remediation must
        refuse before any checkout and keep index, HEAD and the worktree intact; review still isolates and records the
        same overlap.
        """
        module = _load_collector()
        source, worktree, base, old, _head = _setup_repositories(tmp_path, repository_template)
        incoming = source / pr_path
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(b"PR BYTES\n")
        _git(source, "add", "--", pr_path)
        _git(source, "commit", "-m", "add nested checkout collision")
        head = _git(source, "rev-parse", "HEAD")
        _git(source, "update-ref", "refs/pull/17/head", head)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        nested = worktree / "cache" / "review"
        _git(worktree, "worktree", "add", "--detach", str(nested), old)
        (worktree / ".git" / "info" / "exclude").write_text("cache/\n", encoding="utf-8")
        preserved = nested / "private.txt"
        preserved.write_bytes(b"USER BYTES\n")
        original_index = (worktree / ".git" / "index").read_bytes()
        output = tmp_path / "collected"

        code, calls = _collect(
            module,
            source,
            worktree,
            output,
            base,
            head,
            cross_repository=False,
            checkout_mode=checkout_mode,
            gh_checkout_fails=False,
        )

        assert code == (2 if checkout_mode == "remediate" else 0)
        assert not any(call[:3] == ["gh", "pr", "checkout"] for call in calls)
        assert not any(call[:2] == ["git", "checkout"] for call in calls)
        assert preserved.read_bytes() == b"USER BYTES\n"
        assert _git(nested, "rev-parse", "HEAD") == old
        assert _git(worktree, "rev-parse", "HEAD") == base
        assert (worktree / ".git" / "index").read_bytes() == original_index
        artifact = "worktree-preflight.json" if checkout_mode == "remediate" else "source-worktree-context.json"
        preflight = json.loads((output / artifact).read_text(encoding="utf-8"))
        assert preflight["overlapping_pr_paths"] == ["cache/review/"]
        assert preflight["status"].startswith("blocked-")


class TestRemediation:
    """Remediation checkout onto the attached PR branch, with GitHub's checkout routed to a local fake."""

    def test_large_inventory_preserves_files_and_allows_checkout(
        self, tmp_path: Path, repository_template: RepositoryTemplate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Complete preservation checks beyond the general output cap without dropping local files."""
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _write_ignored_tree(worktree, ".venv/\n")
        monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 8192)
        output = tmp_path / "collected"

        code, calls = _remediate(
            module, source, worktree, output, base, head, cross_repository=False, gh_checkout_fails=False
        )

        assert code == 0
        assert any(call[:3] == ["gh", "pr", "checkout"] for call in calls)
        assert (output / "local-checkout.json").exists()
        assert len(list((worktree / ".venv/lib/site-packages").iterdir())) == _IGNORED_TREE_FILES
        preflight = json.loads((output / "worktree-preflight.json").read_text(encoding="utf-8"))
        assert preflight["dirty_path_counts"]["ignored"] == _IGNORED_TREE_FILES
        assert module.dirty_path_record_is_consistent(preflight)

    def test_blocks_collision_inside_large_ignored_tree(
        self, tmp_path: Path, repository_template: RepositoryTemplate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Block an attached checkout over one ignored file even when it hides among hundreds of sampled-out paths.

        Sampling only shapes the record; the overlap check still compares every ignored path, and the colliding one is
        always listed because it is the evidence for the block.
        """
        module = _load_collector()
        monkeypatch.setattr(module, "MAX_OUTPUT_BYTES", 8192)
        source, worktree, base, _old, _head = _setup_repositories(tmp_path, repository_template)
        colliding = ".venv/lib/site-packages/module_0399.py"
        incoming = source / colliding
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(b"PR BYTES\n")
        _git(source, "add", "--force", "--", colliding)
        _git(source, "commit", "-m", "add PR path inside ignored tree")
        head = _git(source, "rev-parse", "HEAD")
        _git(source, "update-ref", "refs/pull/17/head", head)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/topic:refs/heads/topic")
        _write_ignored_tree(worktree, ".venv/\n")
        output = tmp_path / "collected"

        code, calls = _remediate(
            module, source, worktree, output, base, head, cross_repository=False, gh_checkout_fails=False
        )

        preflight = json.loads((output / "worktree-preflight.json").read_text(encoding="utf-8"))
        assert code == 2
        assert not any(call[:3] == ["gh", "pr", "checkout"] for call in calls)
        assert (worktree / colliding).read_text(encoding="utf-8") == "x = 1\n"
        assert preflight["status"] == "blocked-pr-dirty-paths"
        assert preflight["overlapping_pr_paths"] == [colliding]
        assert colliding in preflight["dirty_paths"]
        assert preflight["dirty_path_counts"]["ignored"] == _IGNORED_TREE_FILES
        assert len(preflight["dirty_paths"]) <= module.DIRTY_PATH_SAMPLE_LIMIT + 1
        assert module.dirty_path_record_is_consistent(preflight)

    def test_keeps_github_attached_branch(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Require the GitHub checkout route and its branch tracking for remediation.

        Remediation must use GitHub's own checkout so the branch tracks the PR. After gh pr checkout the main tree is on
        topic with origin as remote and the receipt names the remediate mode.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)

        code, calls = _remediate(
            module,
            source,
            worktree,
            tmp_path / "collected",
            base,
            head,
            cross_repository=False,
            gh_checkout_fails=False,
        )

        assert code == 0
        assert ["gh", "pr", "checkout", "https://github.com/example/project/pull/17"] in calls
        assert _git(worktree, "branch", "--show-current") == "topic"
        assert _git(worktree, "config", "--get", "branch.topic.remote") == "origin"
        assert _git(worktree, "config", "--get", "branch.topic.merge") == "refs/heads/topic"
        checkout = json.loads((tmp_path / "collected" / "local-checkout.json").read_text(encoding="utf-8"))
        assert checkout["checkout_mode"] == "remediate"

    @pytest.mark.parametrize(
        ("local_path", "pr_path"),
        [
            pytest.param("private.local", "private.local", id="same-file"),
            pytest.param("cache/private.local", "cache", id="pr-file-replaces-ignored-directory"),
            pytest.param("private.local", "private.local/nested.py", id="pr-directory-replaces-ignored-file"),
            pytest.param(
                "PRIVATE.local", "private.local", id="native-case-file-alias", marks=_skip_without_case_aliases
            ),
            pytest.param(
                "CACHE/private.local",
                "cache",
                id="native-case-directory-replacement",
                marks=_skip_without_case_aliases,
            ),
            pytest.param(
                "PRIVATE.local",
                "private.local/nested.py",
                id="native-case-file-ancestor",
                marks=_skip_without_case_aliases,
            ),
        ],
    )
    def test_blocks_ignored_checkout_collision(
        self, tmp_path: Path, repository_template: RepositoryTemplate, local_path: str, pr_path: str
    ) -> None:
        """Block checkout before Git can overwrite ignored user files or their parent directory.

        An ignored local file would be overwritten by the PR, or a PR directory would replace an ignored file, including
        through filesystem case aliases. Remediation must stop before checkout with the user bytes, index and branch
        unchanged and a blocked preflight record.
        """
        module = _load_collector()
        source, worktree, base, _old, _head = _setup_repositories(tmp_path, repository_template)
        incoming = source / pr_path
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(b"PR BYTES\n")
        _git(source, "add", "--", pr_path)
        _git(source, "commit", "-m", "add colliding PR path")
        head = _git(source, "rev-parse", "HEAD")
        _git(source, "update-ref", "refs/pull/17/head", head)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/topic:refs/heads/topic")
        (worktree / ".git" / "info" / "exclude").write_text("*.local\ncache/\n", encoding="utf-8")
        local = worktree / local_path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"USER BYTES\n")
        assert _git(worktree, "check-ignore", "--", local_path) == local_path
        original_index = (worktree / ".git" / "index").read_bytes()
        output = tmp_path / "collected"

        code, calls = _remediate(
            module, source, worktree, output, base, head, cross_repository=False, gh_checkout_fails=False
        )

        assert code == 2
        assert not any(call[:3] == ["gh", "pr", "checkout"] for call in calls)
        assert not any(call[:2] == ["git", "checkout"] for call in calls)
        assert local.read_bytes() == b"USER BYTES\n"
        assert _git(worktree, "branch", "--show-current") == "local-diverged"
        assert _git(worktree, "rev-parse", "HEAD") == base
        assert (worktree / ".git" / "index").read_bytes() == original_index
        assert not (output / "local-checkout.json").exists()
        preflight = json.loads((output / "worktree-preflight.json").read_text(encoding="utf-8"))
        assert local_path in preflight["dirty_paths"]
        assert preflight["status"].startswith("blocked-")

    @pytest.mark.parametrize(
        ("local_path", "pr_path"),
        [
            pytest.param("private.local", "incoming.py", id="unrelated-file"),
            pytest.param("private.local", "private.locality/nested.py", id="distinct-prefix"),
            pytest.param(
                "PRIVATE.local", "private.local", id="distinct-case-files", marks=_skip_without_distinct_case_names
            ),
            pytest.param(
                "CACHE/private.local",
                "cache",
                id="distinct-case-directory",
                marks=_skip_without_distinct_case_names,
            ),
            pytest.param(
                "PRIVATE.local",
                "private.local/nested.py",
                id="distinct-case-ancestor",
                marks=_skip_without_distinct_case_names,
            ),
        ],
    )
    def test_preserves_unrelated_ignored_file(
        self, tmp_path: Path, repository_template: RepositoryTemplate, local_path: str, pr_path: str
    ) -> None:
        """Allow checkout while retaining ignored user bytes outside the changed paths.

        An ignored local file shares only a prefix or a differently cased name with an incoming PR path, never the same
        path. Checkout proceeds onto the PR branch, the PR file lands and the user bytes survive.
        """
        module = _load_collector()
        source, worktree, base, _old, _head = _setup_repositories(tmp_path, repository_template)
        incoming = source / pr_path
        incoming.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(b"PR BYTES\n")
        _git(source, "add", "--", pr_path)
        _git(source, "commit", "-m", "add unrelated PR path")
        head = _git(source, "rev-parse", "HEAD")
        _git(source, "update-ref", "refs/pull/17/head", head)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/topic:refs/heads/topic")
        (worktree / ".git" / "info" / "exclude").write_text("*.local\nCACHE/\n", encoding="utf-8")
        local = worktree / local_path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"USER BYTES\n")
        assert _git(worktree, "check-ignore", "--", local_path) == local_path
        output = tmp_path / "collected"

        code, calls = _remediate(
            module, source, worktree, output, base, head, cross_repository=False, gh_checkout_fails=False
        )

        assert code == 0
        assert ["gh", "pr", "checkout", "https://github.com/example/project/pull/17"] in calls
        assert local.read_bytes() == b"USER BYTES\n"
        assert (worktree / pr_path).read_bytes() == b"PR BYTES\n"
        assert _git(worktree, "branch", "--show-current") == "topic"
        assert _git(worktree, "rev-parse", "HEAD") == head
        receipt = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
        assert receipt["checkout_mode"] == "remediate"

    @pytest.mark.parametrize(
        "kind",
        [
            pytest.param(
                "hardlink",
                id="distinct-hardlink-name",
                marks=pytest.mark.skipif(not _file_links_available("hardlink"), reason="file hardlinks unavailable"),
            ),
            pytest.param(
                "symlink",
                id="distinct-symlink-name",
                marks=pytest.mark.skipif(not _file_links_available("symlink"), reason="file symlinks unavailable"),
            ),
        ],
    )
    def test_preserves_distinct_ignored_link_name(
        self, tmp_path: Path, repository_template: RepositoryTemplate, kind: str
    ) -> None:
        """Allow replacing a tracked path while preserving a distinct ignored link entry.

        An ignored hard link or symlink to a tracked file has a different name from the file the PR replaces. Checkout
        replaces the tracked file but must keep the link entry itself, so a hard link still holds the old bytes and a
        symlink still points at notes.txt.
        """
        module = _load_collector()
        source, worktree, base, _old, _head = _setup_repositories(tmp_path, repository_template)
        (source / "notes.txt").write_bytes(b"PR BYTES\n")
        _git(source, "commit", "-am", "replace tracked link target")
        head = _git(source, "rev-parse", "HEAD")
        _git(source, "update-ref", "refs/pull/17/head", head)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "fetch", "--no-tags", str(source), "refs/heads/topic:refs/heads/topic")
        (worktree / ".git" / "info" / "exclude").write_text("*.local\n", encoding="utf-8")
        local = worktree / "private.local"
        if kind == "hardlink":
            local.hardlink_to(worktree / "notes.txt")
        else:
            local.symlink_to("notes.txt")
        assert _git(worktree, "check-ignore", "--", "private.local") == "private.local"
        output = tmp_path / "collected"

        code, calls = _remediate(
            module, source, worktree, output, base, head, cross_repository=False, gh_checkout_fails=False
        )

        assert code == 0
        assert ["gh", "pr", "checkout", "https://github.com/example/project/pull/17"] in calls
        if kind == "hardlink":
            assert local.read_bytes() == b"keep me\n"
        else:
            assert local.is_symlink()
            assert str(local.readlink()) == "notes.txt"
        assert (worktree / "notes.txt").read_bytes() == b"PR BYTES\n"
        assert _git(worktree, "branch", "--show-current") == "topic"
        assert _git(worktree, "rev-parse", "HEAD") == head
        receipt = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
        assert receipt["checkout_mode"] == "remediate"

    def test_records_fork_tracking_destination(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Keep a fork PR's branch receipt bound to its contributor repository URL.

        A fork PR is checked out through the fake gh route. The local branch must track the contributor repository URL
        and the topic ref rather than origin, which is where its publication would go.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)

        code, _calls = _remediate(module, source, worktree, tmp_path / "collected", base, head, gh_checkout_fails=False)

        assert code == 0
        assert _git(worktree, "config", "--get", "branch.topic.remote") == "https://github.com/contributor/project.git"
        assert _git(worktree, "config", "--get", "branch.topic.merge") == "refs/heads/topic"

    def test_recovers_existing_branch_after_gh_failure(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Attach the verified existing PR branch after GitHub's checkout fails.

        gh pr checkout fails with a network error for a same-repository PR whose local topic branch already points at
        the PR head. The collector attaches that branch with git checkout --no-guess and records the fallback method.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)

        code, calls = _remediate(module, source, worktree, tmp_path / "collected", base, head, cross_repository=False)

        assert code == 0
        assert ["git", "checkout", "--no-guess", "topic"] in calls
        assert _git(worktree, "branch", "--show-current") == "topic"
        assert _git(worktree, "rev-parse", "HEAD") == head
        checkout = json.loads((tmp_path / "collected" / "local-checkout.json").read_text(encoding="utf-8"))
        assert checkout["checkout_method"] == "git-original-branch-fallback"
        assert checkout["command"] == "git checkout --no-guess topic"

    def test_creates_missing_branch_from_verified_remote_ref(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Create an absent branch only from the exact fetched selected-remote PR ref.

        gh fails and the local topic branch was deleted. The branch is created from the fetched, verified remote ref and
        tracking ref is created with an all-zero expected old value, so nothing is overwritten.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "branch", "-D", "topic")

        code, calls = _remediate(module, source, worktree, tmp_path / "collected", base, head, cross_repository=False)

        assert code == 0
        assert ["git", "checkout", "--track", "-b", "topic", "origin/topic"] in calls
        assert ["git", "update-ref", "refs/remotes/origin/topic", head, "0" * 40] in calls
        assert _git(worktree, "branch", "--show-current") == "topic"
        assert _git(worktree, "rev-parse", "HEAD") == head

    def test_rejects_stale_branch_without_advancing_it(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Stop before changing a stale local PR branch after a failed GitHub checkout.

        gh fails and the local topic branch exists but points at an older commit. The collector must stop with
        remediation-existing-branch-not-at-pr-head and leave that branch where the user had it.
        """
        module = _load_collector()
        source, worktree, base, old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "update-ref", "refs/heads/topic", old)

        code, calls = _remediate(module, source, worktree, tmp_path / "collected", base, head, cross_repository=False)

        assert code == 2
        assert _git(worktree, "rev-parse", "refs/heads/topic") == old
        assert ["git", "checkout", "--no-guess", "topic"] not in calls
        assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == (
            "remediation-existing-branch-not-at-pr-head\n"
        )

    def test_updates_only_ancestor_tracking_ref(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Allow the explicit remote-tracking update when its prior commit is a PR-head ancestor.

        The branch is missing and the cached tracking ref points at an ancestor of the PR head. Only that explicit
        ancestor update is allowed, after which both refs point at the head.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "branch", "-D", "topic")
        _git(worktree, "update-ref", "refs/remotes/origin/topic", base)

        code, _calls = _remediate(module, source, worktree, tmp_path / "collected", base, head, cross_repository=False)

        assert code == 0
        assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == head
        assert _git(worktree, "rev-parse", "refs/heads/topic") == head

    def test_uses_recorded_head_after_source_moves(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Prevent a source rewrite after the verified fetch from changing the recovered branch ref.

        The source repository moves its ref right after the verified head fetch. The recovered branch must use the head
        recorded at verification, not whatever the source says by the time refs are written.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "branch", "-D", "topic")
        _git(worktree, "update-ref", "refs/remotes/origin/topic", base)

        code, calls = _remediate(
            module,
            source,
            worktree,
            tmp_path / "collected",
            base,
            head,
            cross_repository=False,
            move_source_after_head_fetch=True,
        )

        assert code == 0
        assert _git(source, "rev-parse", "HEAD") != head
        assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == head
        assert _git(worktree, "rev-parse", "refs/heads/topic") == head
        assert not any(arguments[-1] == "refs/heads/topic:refs/remotes/origin/topic" for arguments in calls)

    def test_rejects_divergent_tracking_ref(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Preserve a divergent cached tracking ref instead of allowing Git to non-fast-forward it.

        The branch is missing and the cached tracking ref points at a commit that is not an ancestor of the head.
        Updating it would be a non-fast-forward, so the collector stops with remediation-tracking- branch-diverged and
        creates nothing.
        """
        module = _load_collector()
        source, worktree, base, old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)
        _git(worktree, "branch", "-D", "topic")
        _git(worktree, "update-ref", "refs/remotes/origin/topic", old)

        code, calls = _remediate(module, source, worktree, tmp_path / "collected", base, head, cross_repository=False)

        assert code == 2
        assert _git(worktree, "rev-parse", "refs/remotes/origin/topic") == old
        assert _git(worktree, "branch", "--list", "topic") == ""
        assert not any(arguments[-1] == "refs/heads/topic:refs/remotes/origin/topic" for arguments in calls)
        assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == (
            "remediation-tracking-branch-diverged\n"
        )

    def test_fork_needs_adversarial_recovery_after_gh_failure(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Leave a fork checkout unchanged when GitHub cannot establish its publication branch.

        gh pr checkout fails for a fork PR. No local branch can be trusted to publish to the contributor, so the
        collector stops with remediation-gh-checkout-recovery-required and does not run any git checkout.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)

        code, calls = _remediate(module, source, worktree, tmp_path / "collected", base, head)

        assert code == 2
        assert _git(worktree, "branch", "--show-current") == "local-diverged"
        assert not any(arguments[:2] == ["git", "checkout"] for arguments in calls)
        assert (tmp_path / "collected" / "pr-error.txt").read_text(encoding="utf-8") == (
            "remediation-gh-checkout-recovery-required\n"
        )

    def test_rechecks_partial_gh_failure_before_git_recovery(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Protect PR files changed by a failed GitHub checkout before native recovery runs.

        The failed gh checkout left a modified PR file behind. The preflight must run again and stop with dirty-pr-
        worktree-after-pr-checkout before native git recovery could overwrite that change.
        """
        module = _load_collector()
        source, worktree, base, _old, head = _setup_repositories(tmp_path, repository_template)
        _git(worktree, "checkout", "-b", "local-diverged", base)

        code, calls = _remediate(
            module,
            source,
            worktree,
            tmp_path / "collected",
            base,
            head,
            cross_repository=False,
            mutate_after_gh_failure=True,
        )

        assert code == 2
        assert not any(arguments[:2] == ["git", "checkout"] for arguments in calls)
        assert (
            (tmp_path / "collected" / "pr-error.txt")
            .read_text(encoding="utf-8")
            .startswith("dirty-pr-worktree-after-pr-checkout")
        )


class TestCollectErrors:
    """Collector failures reported through the error artifact."""

    def test_clears_prior_target_on_invalid_retry(self, tmp_path: Path) -> None:
        """Prevent an invalid retry from retaining a previous PR identity artifact.

        A previous run left pr-target.txt in the output directory and the retry is given an issue URL. The collector
        exits 2 with unsafe-pr-target and removes the stale target so no old PR identity remains.
        """
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


class TestGitFailureReason:
    """Mapping of Git failure output to a closed, safe failure reason."""

    def test_classifies_real_non_fast_forward_stderr(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Classify the indented rejection line emitted by a real failed destination fetch.

        Git itself is asked to fetch into a cached ref that is not an ancestor of the new value. The real stderr
        contains an indented rejection line, and the classifier must map it to ref-update-rejected.
        """
        module = _load_collector()
        source, worktree, _base, old, _head = _setup_repositories(tmp_path, repository_template)
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
    def test_run_keeps_only_closed_failure_reason(self, stderr: bytes, expected_reason: str) -> None:
        """Persist only a closed safe reason for Git failure diagnostics.

        A failing Git command writes either a rejection line or a token-like string to stderr. The raised error carries
        only a safe closed reason and no stderr text, so credentials in diagnostics are never persisted.
        """
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


@pytest.mark.integration
def test_isolated_review_survives_concurrent_source_branch_switch(
    tmp_path: Path, repository_template: RepositoryTemplate
) -> None:
    """A different task switching the invoking checkout must not invalidate exact detached PR source."""
    module = _load_collector()
    source, main, base, old, head = _setup_repositories(tmp_path, repository_template)
    output = tmp_path / "concurrent-review"

    code, _calls = _collect(module, source, main, output, base, head, switch_source_after_checkout=True)

    assert code == 0, (output / "pr-error.txt").read_text(encoding="utf-8")
    receipt = json.loads((output / "local-checkout.json").read_text(encoding="utf-8"))
    assert _git(Path(receipt["worktree"]), "rev-parse", "HEAD") == head
    assert _git(main, "rev-parse", "HEAD") == old
    assert _git(main, "branch", "--show-current") == "old-topic"
    assert receipt["head_matches_pr"] is True
    checkout_state = json.loads((output / "checkout-state.json").read_text(encoding="utf-8"))
    assert checkout_state["source_observation_version"] == 1
    assert checkout_state["source_head_before"] == head
    assert checkout_state["source_head_after"] == old
    assert checkout_state["local_state"] == "isolated-exact-pr-head; main-worktree-head-changed"
    assert not (output / "pr-error.txt").exists()


@pytest.mark.integration
def test_interleaved_fetches_keep_their_own_remote_commit(
    tmp_path: Path, repository_template: RepositoryTemplate
) -> None:
    """Two collectors sharing Git metadata must not read each other's FETCH_HEAD."""
    module = _load_collector()
    source, main, _base, old, head = _setup_repositories(tmp_path, repository_template)
    second_head = None

    def ordinary_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Execute the second collector against the same real Git directory."""
        return subprocess.run(arguments, cwd=main, **kwargs)

    def interleaved_run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Finish another real fetch between the first collector's fetch and receipt read."""
        nonlocal second_head
        result = subprocess.run(arguments, cwd=main, **kwargs)
        if arguments[:2] == ["git", "fetch"] and result.returncode == 0:
            second_head = module._fetch_exact_ref(ordinary_run, 10, str(source), "old-topic", "other-fetch")
        return result

    first_head = module._fetch_exact_ref(interleaved_run, 10, str(source), "topic", "first-fetch")

    assert first_head == head
    assert second_head == old


@pytest.mark.integration
def test_failed_fetch_does_not_accept_a_preexisting_commit(
    tmp_path: Path, repository_template: RepositoryTemplate
) -> None:
    """A local object cannot substitute for a successful fetch of the advertised remote commit."""
    module = _load_collector()
    source, main, _base, _old, head = _setup_repositories(tmp_path, repository_template)
    assert _git(main, "rev-parse", "HEAD") == head

    def run(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """Resolve the actual remote but fail its object transfer at the transport boundary."""
        if arguments[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(arguments, 128, stdout=b"", stderr=b"transport unavailable")
        return subprocess.run(arguments, cwd=main, **kwargs)

    with pytest.raises(module.CollectionError, match="command-failed:probe"):
        module._fetch_exact_ref(run, 10, str(source), "topic", "probe")
