"""Contributor-run integrity helper: Git metadata and the user's checkout stay unchanged around contributor code.

The checks run the shipped CLI against real repositories and a real linked worktree, the shape a code review uses,
because the failure they guard against is state written through the shared common Git directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "shared" / "contributor_run_integrity.py"
_ENV = os.environ | {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits, change times and symlinks")


def _git(repo: Path, *args: str) -> str:
    """Run one Git command that must succeed and return its output."""
    done = subprocess.run(["git", "-C", str(repo), *args], env=_ENV, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _helper(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the helper CLI and return its result without raising on a non-zero exit."""
    cmd = [sys.executable, "-I", str(HELPER), *args]
    return subprocess.run(cmd, env=_ENV, capture_output=True, text=True, check=False, cwd=cwd)


def _populate(directory: Path, count: int) -> None:
    """Create enough ordinary files to exercise the diagnostic storage and output limits."""
    for number in range(count):
        (directory / f"entry-{number}").touch()


def _populate_runs(family: Path, count: int) -> None:
    """Create report groups that exceed the checkout's review-run diagnostic budget."""
    for number in range(1, count + 1):
        run = family / f"run-{number:03}"
        run.mkdir(parents=True)
        (run / "gate.json").write_text("{}", encoding="utf-8")


class Baseline:
    """One snapshot of a repository together with the digest the caller keeps from it."""

    def __init__(self, out: Path) -> None:
        """Remember where the baseline file is written."""
        self.file = out
        self.expect = ""

    def take(self, repo: Path, *options: str) -> subprocess.CompletedProcess[str]:
        """Record the baseline for ``repo``, keep the printed digest and return the helper's result."""
        result = _helper("snapshot", "--repo", str(repo), *options, "--out", str(self.file))
        if result.returncode == 0:
            self.expect = json.loads(result.stdout)["sha256"]
        return result

    def verify(self) -> tuple[int, list[str]]:
        """Verify against the kept digest and return ``(exit code, changed labels)``."""
        result = _helper("verify", "--snapshot", str(self.file), "--expect", self.expect)
        changed = json.loads(result.stdout)["changed"] if result.stdout.strip() else []
        return result.returncode, changed


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create a user's checkout with one commit, an ignored interpreter environment and an ignored report tree."""
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".venv/\n.reports/\n", encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker").write_text("m\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.fixture
def baseline(checkout: Path, tmp_path: Path) -> Baseline:
    """Record the checkout's Git metadata, as taken before any contributor code runs."""
    snap = Baseline(tmp_path / "integrity.json")
    snap.take(checkout)
    return snap


@pytest.fixture
def review(checkout: Path, tmp_path: Path) -> Path:
    """Add a linked review worktree outside the user's checkout."""
    path = tmp_path / "review"
    _git(checkout, "worktree", "add", "-q", "--detach", str(path))
    return path


class TestVerify:
    """The verdict after contributor code has run."""

    def test_unchanged_metadata_passes(self, baseline: Baseline) -> None:
        """Nothing written between snapshot and verify exits 0 with an empty change list."""
        assert baseline.verify() == (0, [])

    def test_ignored_drift_identifies_paths_in_each_checkout(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A shared report edit and new review coverage data identify their source checkout and operation."""
        sibling = checkout / ".reports" / "sibling" / "evidence.json"
        sibling.parent.mkdir(parents=True)
        sibling.write_text("{}", encoding="utf-8")
        coverage = review / ".coverage"
        with (review / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write(".coverage\n")
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(review, "--main-checkout", str(checkout), "--watch-surface").returncode == 0
        sibling.write_text('{"changed":true}', encoding="utf-8")
        (checkout / ".venv" / "marker").unlink()
        coverage.write_bytes(b"data")
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout) == {
            "ok": False,
            "changed": ["main-checkout:ignored", "review:ignored"],
            "ignored_changes": {
                "main-checkout:ignored": {"modified": [".reports/sibling/evidence.json"], "removed": [".venv/marker"]},
                "review:ignored": {"added": [".coverage"]},
            },
        }

    def test_large_ignored_store_reports_directory_and_bounds_snapshot(self, checkout: Path, tmp_path: Path) -> None:
        """A large store reports its directory while a small sibling still identifies the exact changed file."""
        store = checkout / ".venv"
        _populate(store, 1024)
        report = checkout / ".reports" / "state.json"
        report.parent.mkdir()
        report.write_text("{}", encoding="utf-8")
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        assert snap.file.stat().st_size < 32_768
        (store / "entry-1023").write_bytes(b"changed")
        report.unlink()
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {"directories": [".venv"], "removed": [".reports/state.json"]}
        }

    def test_long_ignored_change_list_reports_omissions(self, checkout: Path, baseline: Baseline) -> None:
        """The change count stays visible if the number of exact path changes exceeds the output limit."""
        _populate(checkout / ".venv", 70)
        result = _helper("verify", "--snapshot", str(baseline.file), "--expect", baseline.expect)
        assert result.returncode == 3
        expected = sorted(f".venv/entry-{number}" for number in range(70))
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {"added": expected[:64], "omitted": 6}
        }

    def test_sibling_review_run_is_identified_in_large_report_archive(self, checkout: Path, tmp_path: Path) -> None:
        """Report archives can outgrow per-file detail while individual review runs remain distinguishable."""
        archive = checkout / ".reports" / "archive"
        archive.mkdir(parents=True)
        _populate(archive, 170)
        sibling = checkout / ".reports" / "codex" / "code-review" / "pr-27" / "run-002" / "gate.json"
        sibling.parent.mkdir(parents=True)
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        sibling.write_text("{}", encoding="utf-8")
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {"added": [sibling.relative_to(checkout).as_posix()]}
        }

    def test_overflow_preserves_captured_groups_without_reporting_false_removals(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """Inserting an earlier run can shift another into overflow while an unchanged file remains on disk."""
        family = checkout / ".reports" / "codex" / "code-review" / "pr-1"
        _populate_runs(family, 130)
        captured = family / "run-001" / "gate.json"
        snap = Baseline(tmp_path / "integrity.json")
        assert snap.take(checkout).returncode == 0
        captured.write_text('{"changed":true}', encoding="utf-8")
        earlier = family / "run-000"
        earlier.mkdir()
        (earlier / "new.json").touch()
        result = _helper("verify", "--snapshot", str(snap.file), "--expect", snap.expect)
        assert result.returncode == 3
        assert json.loads(result.stdout)["ignored_changes"] == {
            "main-checkout:ignored": {
                "directories": [".reports/codex/code-review"],
                "modified": [captured.relative_to(checkout).as_posix()],
            }
        }

    def test_historical_baseline_still_detects_ignored_writes(self, checkout: Path, baseline: Baseline) -> None:
        """A retained digest from the previous aggregate-only schema still verifies and stops on later writes."""
        body = json.loads(baseline.file.read_text(encoding="utf-8"))
        body.pop("ignored_details", None)
        body["schema"] = 3
        marker = checkout / ".venv" / "marker"
        st = marker.lstat()
        record = f"{marker}:{st.st_size}:{st.st_mode:o}:{st.st_ino}:{st.st_ctime_ns}:"
        body["marks"]["main-checkout:ignored"] = hashlib.sha256(record.encode()).hexdigest()
        baseline.file.write_text(json.dumps(body), encoding="utf-8")
        baseline.expect = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert baseline.verify() == (0, [])
        (checkout / ".venv" / "added").touch()
        assert baseline.verify() == (3, ["main-checkout:ignored"])

    @pytest.mark.parametrize(
        ("relative", "label"),
        [
            pytest.param(".git/config", "common:config", id="fsmonitor-in-config"),
            pytest.param(".git/hooks/post-checkout", "common:hooks/", id="planted-hook"),
            pytest.param(".git/codex-git-approval.json", "common:codex-git-approval.json", id="forged-grant"),
            pytest.param(".git/info/exclude", "common:info/", id="info-exclude"),
            pytest.param(".git/modules/sub/hooks/post-checkout", "common:modules", id="submodule-hook"),
        ],
    )
    def test_planted_git_state_fails_closed(
        self, checkout: Path, baseline: Baseline, relative: str, label: str
    ) -> None:
        """A write to the Git directory that a later host command would act on exits 3 and names the path.

        These are the paths the permission profiles leave writable to sandboxed code, so each is a way for contributor
        code to outlive the run.
        """
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write("[core]\n\tfsmonitor = /tmp/x\n")
        code, changed = baseline.verify()
        assert (code, label in changed) == (3, True)

    def test_moved_local_ref_fails_closed_but_a_fetched_remote_ref_does_not(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """A repointed branch is a change; a ``git fetch`` moving a remote-tracking ref is the user, not the run."""
        _git(checkout, "branch", "other")
        _git(checkout, "update-ref", "refs/remotes/origin/x", "HEAD")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        moved = _git(checkout, "commit-tree", "HEAD^{tree}", "-m", "moved")
        _git(checkout, "update-ref", "refs/remotes/origin/x", moved)
        fetched = snap.verify()
        _git(checkout, "update-ref", "refs/heads/other", moved)
        assert (fetched, snap.verify()) == ((0, []), (3, ["common:refs"]))

    def test_garbage_commondir_makes_git_fail_and_verify_never_passes(self, checkout: Path, baseline: Baseline) -> None:
        """A planted ``commondir`` that Git cannot follow stops the check with a failure code, which the contract treats
        as a stop."""
        (checkout / ".git" / "commondir").write_text("[core]\n", encoding="utf-8")
        code, _changed = baseline.verify()
        assert code in {1, 3}

    @_POSIX_ONLY
    def test_hook_mode_change_alone_fails_closed(self, checkout: Path, tmp_path: Path) -> None:
        """Making an existing hook executable changes no bytes but arms it, so file modes are fingerprinted."""
        hook = checkout / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o644)
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        hook.chmod(0o755)
        assert snap.verify() == (3, ["common:hooks/"])

    def test_include_target_edit_fails_closed_for_absolute_and_relative_paths(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """A file the Git configuration includes is part of the configuration, resolved against the file naming it."""
        absolute = tmp_path / "extra.gitconfig"
        absolute.write_text("[user]\n\tname = a\n", encoding="utf-8")
        relative = checkout / ".git" / "local.gitconfig"
        relative.write_text("[user]\n\temail = a@example.com\n", encoding="utf-8")
        _git(checkout, "config", "--add", "include.path", str(absolute))
        _git(checkout, "config", "--add", "include.path", "local.gitconfig")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        absolute.write_text("[core]\n\tfsmonitor = /tmp/x\n", encoding="utf-8")
        relative.write_text("[core]\n\tfsmonitor = /tmp/y\n", encoding="utf-8")
        assert snap.verify() == (3, sorted(["config:repo:include:local.gitconfig", f"config:repo:include:{absolute}"]))

    def test_edit_of_a_hooks_path_target_fails_closed(self, checkout: Path, tmp_path: Path) -> None:
        """A husky-style hooks directory named by ``core.hooksPath`` runs on the next commit, so its bytes count."""
        hooks = checkout / ".husky" / "_"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        _git(checkout, "config", "core.hooksPath", ".husky/_")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        assert snap.verify() == (3, ["config:repo:hooksPath"])

    def test_baseline_file_rewritten_by_contributor_code_is_rejected(self, checkout: Path, baseline: Baseline) -> None:
        """Re-recording a baseline over the changed state is caught: the kept digest no longer matches the file."""
        (checkout / ".git" / "hooks").mkdir(exist_ok=True)
        (checkout / ".git" / "hooks" / "post-checkout").write_text("evil\n", encoding="utf-8")
        forged = _helper("snapshot", "--repo", str(checkout), "--out", str(baseline.file))
        assert (forged.returncode, baseline.verify()) == (0, (3, ["snapshot-file"]))

    def test_unreadable_snapshot_is_a_failure_not_a_pass(self, tmp_path: Path) -> None:
        """A missing snapshot never reads as unchanged: verify exits 1 and says why."""
        result = _helper("verify", "--snapshot", str(tmp_path / "missing.json"), "--expect", "0" * 64)
        assert (result.returncode, "snapshot unreadable" in result.stderr) == (1, True)

    @_POSIX_ONLY
    def test_unreadable_fingerprinted_file_fails_closed(self, checkout: Path, baseline: Baseline) -> None:
        """A fingerprinted file made unreadable by contributor code is a change, never an uncaught traceback."""
        hook = checkout / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("x\n", encoding="utf-8")
        hook.chmod(0)
        code, changed = baseline.verify()
        hook.chmod(0o644)
        assert (code, "common:hooks/" in changed) == (3, True)

    @_POSIX_ONLY
    def test_a_file_already_unreadable_at_snapshot_stops_before_any_execution(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """A path that cannot be fingerprinted would stop every later verify, so the snapshot itself fails and records
        nothing."""
        hook = checkout / ".git" / "hooks" / "post-checkout"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("x\n", encoding="utf-8")
        hook.chmod(0)
        snap = Baseline(tmp_path / "integrity.json")
        result = snap.take(checkout)
        hook.chmod(0o644)
        assert (result.returncode, snap.file.exists()) == (1, False)


class TestUserCheckout:
    """The user's own checkout beside a review worktree."""

    def test_edit_to_a_tracked_file_is_caught_from_a_worktree_run(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """Contributor code in the review worktree that edits the user's own tree fails verification."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / "a.txt").write_text("tampered\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:dirty-content", "main-checkout:status"])

    def test_new_file_in_an_ignored_interpreter_environment_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A planted file in the ignored virtual environment runs at every interpreter start, so it is a change."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / ".venv" / "evil.pth").write_text("import os\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:ignored"])

    @_POSIX_ONLY
    @pytest.mark.parametrize(
        "name", [pytest.param("site.pth", id="executable-surface"), pytest.param("data.bin", id="other-ignored-file")]
    )
    def test_same_size_write_with_the_modification_time_restored_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path, name: str
    ) -> None:
        """Resetting a file's modification time hides nothing: bytes are hashed for surfaces, change time for the
        rest."""
        target = checkout / ".venv" / name
        target.write_text("import os; x = 1\n", encoding="utf-8")
        stamp = target.stat().st_mtime_ns
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        target.write_text("import os; x = 2\n", encoding="utf-8")
        os.utime(target, ns=(stamp, stamp))
        assert snap.verify() == (3, ["main-checkout:ignored"])

    def test_content_edit_of_an_already_untracked_file_is_caught(self, checkout: Path, tmp_path: Path) -> None:
        """Rewriting a file the status already lists as untracked leaves the status line equal, so bytes are hashed."""
        (checkout / "scratch.py").write_text("one\n", encoding="utf-8")
        review = tmp_path / "review"
        _git(checkout, "worktree", "add", "-q", "--detach", str(review))
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / "scratch.py").write_text("two\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:dirty-content"])

    def test_relative_main_checkout_is_resolved_at_snapshot_time(self, checkout: Path, tmp_path: Path) -> None:
        """``--main-checkout .`` verifies from any working directory instead of raising a false alarm."""
        snap = Baseline(tmp_path / "integrity.json")
        result = _helper(
            "snapshot", "--repo", str(checkout), "--main-checkout", ".", "--out", str(snap.file), cwd=checkout
        )
        snap.expect = json.loads(result.stdout)["sha256"]
        assert snap.verify() == (0, [])

    def test_workflow_writes_inside_an_ignored_report_tree_are_not_changes(self, checkout: Path) -> None:
        """The documented layout puts the run directory and review worktree under the checkout's ignored report tree.

        Run artifacts, the baseline itself and the test runner's caches are written there on purpose, so none of them
        may trip the user-checkout check, while the worktree keeps its own coverage through ``--watch-surface``.
        """
        reports = checkout / ".reports" / "codex" / "code-review" / "pr-1"
        run, worktree = reports / "run-001", reports / "run-001-review-worktree"
        run.mkdir(parents=True)
        _git(checkout, "worktree", "add", "-q", "--detach", str(worktree))
        snap = Baseline(run / "integrity.json")
        taken = snap.take(worktree, "--main-checkout", str(checkout), "--watch-surface")
        (run / "review-notes.md").write_text("notes\n", encoding="utf-8")
        (worktree / ".pytest_cache").mkdir()
        (worktree / ".pytest_cache" / "v").write_text("1\n", encoding="utf-8")
        (worktree / ".mypy_cache").mkdir()
        (worktree / ".mypy_cache" / "x.json").write_text("{}\n", encoding="utf-8")
        assert (taken.returncode, snap.verify()) == (0, (0, []))


class TestCheckoutUnderReview:
    """What a run may leave in the checkout under review, which the agent never edits during review."""

    def test_edit_to_a_tracked_non_surface_file_is_caught(self, review: Path, tmp_path: Path) -> None:
        """A rewritten test or helper module would run unreviewed under a later escalated command."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        (review / "a.txt").write_text("rewritten\n", encoding="utf-8")
        assert snap.verify() == (3, ["review:diff"])

    def test_new_untracked_file_is_caught(self, review: Path, tmp_path: Path) -> None:
        """A file the run wrote into the worktree outside caches is a change."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        (review / "helper.py").write_text("import os\n", encoding="utf-8")
        assert snap.verify() == (3, ["review:untracked"])

    def test_planted_auto_load_file_is_caught_by_name(self, review: Path, tmp_path: Path) -> None:
        """A conftest written into the checkout under review is named, so the report says which surface changed."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        (review / "conftest.py").write_text("import os\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "surface:conftest.py" in changed) == (3, True)


class TestLinkedWorktree:
    """A review worktree shares the common Git directory and has admin files of its own."""

    def test_worktree_commondir_redirect_is_caught(self, checkout: Path, review: Path, tmp_path: Path) -> None:
        """Repointing the worktree's ``commondir`` at a copy cannot hide an edit of the real configuration."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review)
        admin = next((checkout / ".git" / "worktrees").iterdir())
        copy = tmp_path / "copy.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(copy)], env=_ENV, check=True)
        (admin / "commondir").write_text(f"{copy}\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "git-dir-redirect:repo" in changed) == (3, True)

    def test_main_checkout_commondir_redirect_is_caught(self, checkout: Path, review: Path, tmp_path: Path) -> None:
        """A ``commondir`` file planted in the user's own ``.git`` redirects every later Git command there."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        copy = tmp_path / "copy.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(checkout), str(copy)], env=_ENV, check=True)
        (checkout / ".git" / "commondir").write_text(f"{copy}\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "git-dir-redirect:main" in changed) == (3, True)


class TestIndexFlagsAndRefs:
    """Git's own view of a checkout is steerable by contributor-writable state, so it is not trusted alone."""

    def test_assume_unchanged_edit_in_the_user_checkout_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """An index flag hides an edit from status and diff, so flagged files are fingerprinted by their own bytes."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        _git(checkout, "update-index", "--assume-unchanged", "a.txt")
        (checkout / "a.txt").write_text("tampered\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "main-checkout:index-flags" in changed, _git(checkout, "status", "--porcelain")) == (3, True, "")

    def test_skip_worktree_edit_in_the_review_worktree_is_caught(self, review: Path, tmp_path: Path) -> None:
        """A skip-worktree flag makes ``git diff HEAD`` empty, which must not make a rewritten test look unchanged."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        _git(review, "update-index", "--skip-worktree", "a.txt")
        (review / "a.txt").write_text("rewritten\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "review:index-flags" in changed) == (3, True)

    def test_replace_ref_is_a_changed_ref(self, checkout: Path, tmp_path: Path) -> None:
        """A ``refs/replace`` entry makes later Git reads return other bytes for a commit, so every namespace counts."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        other = _git(checkout, "commit-tree", "HEAD^{tree}", "-m", "planted")
        _git(checkout, "replace", "HEAD", other)
        assert snap.verify() == (3, ["common:refs"])

    def test_packing_refs_is_not_a_change(self, checkout: Path, tmp_path: Path) -> None:
        """``git pack-refs`` moves ref values into ``packed-refs`` without changing any, which is storage, not state."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        _git(checkout, "pack-refs", "--all")
        assert snap.verify() == (0, [])

    @_POSIX_ONLY
    def test_editing_the_target_of_a_hook_symlink_is_caught(self, checkout: Path, tmp_path: Path) -> None:
        """A hook that is a symlink runs its target, so the target's bytes are part of the hook."""
        target = checkout / ".git" / "impl" / "pre-commit"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hooks = checkout / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "pre-commit").symlink_to(target)
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        target.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        assert snap.verify() == (3, ["common:hooks/"])


class TestSubmodules:
    """A submodule's Git directory is read by the user's own ``git status`` in the superproject."""

    def test_submodule_commondir_redirect_is_caught(self, checkout: Path, tmp_path: Path) -> None:
        """Pointing a submodule Git directory at another one would make Git read a planted configuration."""
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git(upstream, "init", "-q", "-b", "main")
        (upstream / "u.txt").write_text("u\n", encoding="utf-8")
        _git(upstream, "add", "u.txt")
        _git(upstream, "commit", "-q", "-m", "u")
        _git(checkout, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
        _git(checkout, "commit", "-q", "-m", "add submodule")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (checkout / ".git" / "modules" / "sub" / "commondir").write_text(f"{tmp_path}\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "common:modules" in changed) == (3, True)


class TestNestedRepositoriesAndCaches:
    """What Git lists as one entry is walked, and tool caches are not mistaken for planted code."""

    def test_edit_inside_a_nested_repository_in_an_ignored_tree_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """An editable install cloned from VCS lists as a single directory entry, so its files are walked."""
        nested = checkout / ".venv" / "src" / "pkg"
        nested.mkdir(parents=True)
        _git(nested, "init", "-q", "-b", "main")
        (nested / "mod.py").write_text("X = 1\n", encoding="utf-8")
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (nested / "mod.py").write_text("X = 2\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:ignored"])

    def test_edit_in_another_linked_worktree_inside_the_checkout_is_caught(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """The user's own worktree nested in the checkout persists a planted edit into their next run of that branch."""
        other = checkout / "wt2"
        _git(checkout, "worktree", "add", "-q", "--detach", str(other))
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (other / "a.txt").write_text("planted\n", encoding="utf-8")
        code, changed = snap.verify()
        assert (code, "main-checkout:dirty-content" in changed) == (3, True)

    def test_bytecode_written_into_the_review_worktree_is_caught(self, review: Path, tmp_path: Path) -> None:
        """Python loads a matching ``.pyc`` instead of the scanned source, so a new bytecode file is a change."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--watch-surface")
        cache = review / ".venv" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "m.cpython-312.pyc").write_bytes(b"\0")
        code, changed = snap.verify()
        assert (code, "review:ignored" in changed) == (3, True)

    def test_tool_and_ide_caches_in_the_user_checkout_are_not_changes(
        self, checkout: Path, review: Path, tmp_path: Path
    ) -> None:
        """A language server or Finder writing caches during a long review must not force a restart."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(review, "--main-checkout", str(checkout))
        (checkout / ".DS_Store").write_bytes(b"\0")
        (checkout / ".mypy_cache").mkdir()
        (checkout / ".mypy_cache" / "x.json").write_text("{}\n", encoding="utf-8")
        (checkout / ".venv" / ".ruff_cache").mkdir()
        (checkout / ".venv" / ".ruff_cache" / "y").write_text("y\n", encoding="utf-8")
        assert snap.verify() == (0, [])

    def test_unignored_run_directory_is_not_a_status_change(self, checkout: Path, tmp_path: Path) -> None:
        """With every untracked file listed, the baseline written beside the run must not trip the first verify."""
        (checkout / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        _git(checkout, "commit", "-q", "-am", "stop ignoring reports")
        _git(checkout, "config", "status.showUntrackedFiles", "all")
        run = checkout / ".reports" / "run-001"
        run.mkdir(parents=True)
        worktree = checkout / ".reports" / "run-001-review-worktree"
        _git(checkout, "worktree", "add", "-q", "--detach", str(worktree))
        snap = Baseline(run / "integrity.json")
        snap.take(worktree, "--main-checkout", str(checkout), "--watch-surface")
        (run / "notes.md").write_text("notes\n", encoding="utf-8")
        assert snap.verify() == (0, [])


class TestRemediationInTheUserCheckout:
    """Remediation runs contributor code in the user's own checkout, where the agent edits tracked files on purpose."""

    def test_new_file_in_the_ignored_environment_is_caught_without_a_main_checkout(
        self, checkout: Path, tmp_path: Path
    ) -> None:
        """The interpreter environment beside the sources gets the same ignored-file coverage as in review."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (checkout / ".venv" / "evil.pth").write_text("import os\n", encoding="utf-8")
        assert snap.verify() == (3, ["main-checkout:ignored"])

    def test_the_agents_own_tracked_edit_is_not_a_change(self, checkout: Path, tmp_path: Path) -> None:
        """Remediation edits the checkout on purpose, so tracked content is not fingerprinted there."""
        snap = Baseline(tmp_path / "integrity.json")
        snap.take(checkout)
        (checkout / "a.txt").write_text("fixed\n", encoding="utf-8")
        assert snap.verify() == (0, [])
