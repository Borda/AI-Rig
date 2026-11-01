"""Real-Git checks for retaining remediation work on its intended local branch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from test_collect_pr_git_recovery import (
    RepositoryTemplate,
    _build_repository_template,
    _collect,
    _git,
    _load_collector,
    _setup_repositories,
)

HELPER = Path(__file__).resolve().parents[3] / "shared" / "remediation_branch.py"
PR_URL = "https://github.com/example/project/pull/17"
SAME_REPO_REMOTE = "https://github.com/example/project.git"
FORK_REMOTE = "https://github.com/contributor/project.git"
CO_AUTHOR = "\n\nCo-authored-by: Codex <codex@openai.com>"


@pytest.fixture(scope="session")
def repository_template(tmp_path_factory: pytest.TempPathFactory) -> RepositoryTemplate:
    """Build the shared PR repositories once per test process; tests receive private copies."""
    return _build_repository_template(tmp_path_factory.mktemp("remediation-git-template"))


@dataclass
class _Collected:
    """Repositories after a successful remediate-mode PR collection."""

    source: Path
    worktree: Path
    base: str
    old: str
    head: str
    pr_dir: Path
    calls: list[list[str]]


def _collected(tmp_path: Path, template: RepositoryTemplate, **options: Any) -> _Collected:
    """Set up private repositories and run PR collection in remediate mode, asserting it succeeds."""
    source, worktree, base, old, head = _setup_repositories(tmp_path, template)
    pr_dir = tmp_path / "pr"
    options = {"checkout_mode": "remediate", "gh_checkout_fails": False, **options}
    code, calls = _collect(_load_collector(), source, worktree, pr_dir, base, head, **options)
    assert code == 0
    return _Collected(source, worktree, base, old, head, pr_dir, calls)


def _run_helper(worktree: Path, *args: str, flags: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    """Run the branch helper as a real subprocess from inside the worktree."""
    return subprocess.run(
        [sys.executable, *flags, str(HELPER), *args], cwd=worktree, capture_output=True, text=True, check=False
    )


def _prepare(c: _Collected, receipt: Path) -> subprocess.CompletedProcess[str]:
    """Run ``prepare`` for the collected PR and write the receipt to ``receipt``."""
    return _run_helper(c.worktree, "prepare", "--pr-dir", str(c.pr_dir), "--receipt", str(receipt))


def _check(worktree: Path, receipt: Path, expected_head: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run ``check`` of a receipt against the expected head."""
    return _run_helper(worktree, "check", "--receipt", str(receipt), "--expected-head", expected_head, **kwargs)


def _prepared(c: _Collected, receipt: Path) -> dict[str, Any]:
    """Run ``prepare`` expecting success and return the receipt record."""
    prepared = _prepare(c, receipt)
    assert prepared.returncode == 0, prepared.stderr
    return json.loads(receipt.read_text(encoding="utf-8"))


def _snapshot(worktree: Path) -> tuple[str, str, str]:
    """Capture refs, working-tree status and local config so tests can prove nothing was mutated."""
    return (
        _git(worktree, "show-ref"),
        _git(worktree, "status", "--porcelain=v1"),
        _git(worktree, "config", "--local", "--list"),
    )


class TestNativeCheckout:
    """Receipts for a PR checked out natively with ``gh pr checkout``."""

    @pytest.mark.parametrize("branch", ["topic", "contributor/topic"])
    @pytest.mark.parametrize("tracking", ["pull-ref", "contributor-branch"])
    def test_prepares_and_checks_local_fix_without_push_destination(
        self, tmp_path: Path, repository_template: RepositoryTemplate, branch: str, tracking: str
    ) -> None:
        """A native fork checkout is retained through a local fix without any publication configuration.

        Unrelated push settings (``pushRemote``, ``remote.origin.push``) are present so the helper is shown to ignore
        them: it must neither read them as the destination nor rewrite the local config or the source repository.
        """
        c = _collected(
            tmp_path, repository_template, gh_local_branch=branch, gh_pull_ref_tracking=tracking == "pull-ref"
        )
        assert ["gh", "pr", "checkout", PR_URL] in c.calls
        _git(c.worktree, "config", f"branch.{branch}.pushRemote", "origin")
        _git(c.worktree, "config", "remote.origin.push", "HEAD:refs/heads/unrelated")
        before_config = _git(c.worktree, "config", "--local", "--list")
        before_source = _git(c.source, "show-ref")
        receipt = tmp_path / "branch.json"
        record = _prepared(c, receipt)
        (c.worktree / "module.py").write_text("value = 'corrected'\n", encoding="utf-8")
        _git(c.worktree, "add", "module.py")
        _git(c.worktree, "commit", "-m", "local correction" + CO_AUTHOR)
        checked = _check(c.worktree, receipt, _git(c.worktree, "rev-parse", "HEAD"))
        assert (record["branch"], record["head_repository"], record["initial_head"], record["schema_version"]) == (
            branch,
            "contributor/project",
            c.head,
            2,
        )
        assert checked.returncode == 0, checked.stderr
        assert json.loads(checked.stdout) == record
        assert _git(c.worktree, "branch", "--show-current") == branch
        assert _git(c.worktree, "config", "--local", "--list") == before_config
        assert _git(c.source, "show-ref") == before_source

    @staticmethod
    def _change_pull_number(worktree: Path, record: dict[str, Any]) -> None:
        """Point the branch merge ref at a different pull request."""
        _git(worktree, "config", "branch.topic.merge", "refs/pull/18/head")

    @staticmethod
    def _change_base_remote(worktree: Path, record: dict[str, Any]) -> None:
        """Repoint ``origin`` at a different repository."""
        _git(worktree, "remote", "set-url", "origin", "https://github.com/other/project.git")

    @staticmethod
    def _advance_head(worktree: Path, record: dict[str, Any]) -> None:
        """Add a commit the receipt never recorded."""
        _git(worktree, "commit", "--allow-empty", "-m", "unrecorded change")

    @staticmethod
    def _change_pr_url(worktree: Path, record: dict[str, Any]) -> None:
        """Edit the receipt to name a different pull request URL."""
        record["pr_url"] = "https://github.com/example/project/pull/18"

    @staticmethod
    def _claim_recovery(worktree: Path, record: dict[str, Any]) -> None:
        """Edit the receipt to claim a recovery the native checkout never performed."""
        record["recovery"] = {"legacy_receipt": "retained.json"}

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param(_change_pull_number, id="pull-number"),
            pytest.param(_change_base_remote, id="base-remote"),
            pytest.param(_advance_head, id="head"),
            pytest.param(_change_pr_url, id="pr-url"),
            pytest.param(_claim_recovery, id="recovered"),
        ],
    )
    def test_rejects_changed_source_identity(
        self, tmp_path: Path, repository_template: RepositoryTemplate, change: Callable[[Path, dict[str, Any]], None]
    ) -> None:
        """A successful native checkout does not authorize a different PR, repository, or revision.

        Each case alters one identity fact after ``prepare``; ``check`` must exit 2 and leave refs and config alone.
        """
        c = _collected(tmp_path, repository_template, gh_pull_ref_tracking=True)
        receipt = tmp_path / "branch.json"
        record = _prepared(c, receipt)
        change(c.worktree, record)
        receipt.write_text(json.dumps(record), encoding="utf-8")
        before = _snapshot(c.worktree)

        checked = _check(c.worktree, receipt, c.head)

        assert checked.returncode == 2
        assert _snapshot(c.worktree) == before


class TestPrepare:
    """``prepare`` retains the collected PR branch and refuses unusable destinations."""

    def test_retains_collected_github_branch(self, tmp_path: Path, repository_template: RepositoryTemplate) -> None:
        """Never replace a PR branch with a generated branch before the user's next commit.

        After a normal collection ``prepare`` must leave the checked-out PR branch and every ref exactly as found.
        """
        c = _collected(tmp_path, repository_template)
        before = _git(c.worktree, "show-ref")

        result = _prepare(c, tmp_path / "branch.json")

        assert result.returncode == 0, result.stderr
        assert _git(c.worktree, "branch", "--show-current") == "topic"
        assert _git(c.worktree, "show-ref") == before

    def test_rejects_changed_head_and_preserves_dirty_files(
        self, tmp_path: Path, repository_template: RepositoryTemplate
    ) -> None:
        """Never replace local commits or discard unrelated changes while preparing a branch.

        The worktree holds an unrecorded user commit and an untracked notes file; ``prepare`` reports a head mismatch.
        """
        c = _collected(tmp_path, repository_template)
        (c.worktree / "notes.txt").write_text("keep user notes\n", encoding="utf-8")
        _git(c.worktree, "commit", "--allow-empty", "-m", "user commit" + CO_AUTHOR)
        changed = _git(c.worktree, "rev-parse", "HEAD")
        receipt = tmp_path / "branch.json"

        result = _prepare(c, receipt)

        assert result.returncode == 2
        assert "head-mismatch" in result.stderr
        assert _git(c.worktree, "rev-parse", "HEAD") == changed
        assert (c.worktree / "notes.txt").read_text(encoding="utf-8") == "keep user notes\n"
        assert not receipt.exists()

    @staticmethod
    def _review_mode(worktree: Path, pr_dir: Path) -> None:
        """Rewrite the checkout evidence as a review-mode checkout."""
        checkout_path = pr_dir / "local-checkout.json"
        checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
        checkout["checkout_mode"] = "review"
        checkout_path.write_text(json.dumps(checkout), encoding="utf-8")

    @staticmethod
    def _detach(worktree: Path, pr_dir: Path) -> None:
        """Detach HEAD at the PR head."""
        _git(worktree, "checkout", "--detach", _git(worktree, "rev-parse", "HEAD"))

    @staticmethod
    def _drop_upstream(worktree: Path, pr_dir: Path) -> None:
        """Remove the branch upstream merge ref."""
        _git(worktree, "config", "--unset", "branch.topic.merge")

    @staticmethod
    def _track_pull_ref(worktree: Path, pr_dir: Path) -> None:
        """Track the pull ref instead of the contributor branch."""
        _git(worktree, "config", "branch.topic.merge", "refs/pull/17/head")

    @staticmethod
    def _wrong_fetch_remote(worktree: Path, pr_dir: Path) -> None:
        """Repoint ``origin`` at a different repository."""
        _git(worktree, "remote", "set-url", "origin", "https://github.com/other/project.git")

    @staticmethod
    def _rename_branch(worktree: Path, pr_dir: Path) -> None:
        """Rename the checked-out branch to a local alias."""
        _git(worktree, "branch", "-m", "contributor/topic")

    @pytest.mark.parametrize(
        ("damage", "stderr_part"),
        [
            pytest.param(_review_mode, "", id="review"),
            pytest.param(_detach, "", id="detached"),
            pytest.param(_drop_upstream, "", id="missing-upstream"),
            pytest.param(_track_pull_ref, "", id="pull-ref"),
            pytest.param(_wrong_fetch_remote, "", id="wrong-fetch-remote"),
            pytest.param(_rename_branch, "branch-mismatch-after-pr-checkout", id="renamed"),
        ],
    )
    def test_rejects_unusable_destination_without_git_mutation(
        self,
        tmp_path: Path,
        repository_template: RepositoryTemplate,
        damage: Callable[[Path, Path], None],
        stderr_part: str,
    ) -> None:
        """Do not approve editing when checkout evidence or the PR source identity is missing.

        Every case damages one fact after a good collection; ``prepare`` must exit 2, write no receipt, and leave refs,
        branch and config untouched.
        """
        c = _collected(tmp_path, repository_template, cross_repository=False)
        damage(c.worktree, c.pr_dir)
        before = (_snapshot(c.worktree), _git(c.worktree, "branch", "--show-current"))
        receipt = tmp_path / "branch.json"

        result = _prepare(c, receipt)

        assert result.returncode == 2, result.stderr
        assert stderr_part in result.stderr
        assert not receipt.exists()
        assert (_snapshot(c.worktree), _git(c.worktree, "branch", "--show-current")) == before

    @staticmethod
    def _alias_branch(worktree: Path, pr_dir: Path, checkout: dict[str, Any], head: str) -> None:
        """Claim a fallback checkout onto a local alias branch."""
        _git(worktree, "branch", "-m", "contributor/topic")
        checkout["local_branch"] = "contributor/topic"

    @staticmethod
    def _fork_head(worktree: Path, pr_dir: Path, checkout: dict[str, Any], head: str) -> None:
        """Make the observed head repository a fork; a same-repository claim cannot override it."""
        pr_path = pr_dir / "pr.json"
        payload = json.loads(pr_path.read_text(encoding="utf-8"))
        payload["headRepository"] = {"nameWithOwner": "contributor/project"}
        pr_path.write_text(json.dumps(payload), encoding="utf-8")

    @staticmethod
    def _no_failure_record(worktree: Path, pr_dir: Path, checkout: dict[str, Any], head: str) -> None:
        """Drop the recorded ``gh`` checkout failure."""
        checkout["gh_checkout_failure"] = None

    @staticmethod
    def _no_gh_command(worktree: Path, pr_dir: Path, checkout: dict[str, Any], head: str) -> None:
        """Drop the recorded failed ``gh`` command."""
        checkout["gh_checkout_failure"].pop("command")

    @staticmethod
    def _detached_command(worktree: Path, pr_dir: Path, checkout: dict[str, Any], head: str) -> None:
        """Record a detached checkout instead of the audited direct branch checkout."""
        checkout["command"] = f"git checkout --detach {head}"

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(_fork_head, id="fork"),
            pytest.param(_no_failure_record, id="missing-failure"),
            pytest.param(_no_gh_command, id="missing-gh-command"),
            pytest.param(_detached_command, id="wrong-command"),
            pytest.param(_alias_branch, id="local-alias"),
        ],
    )
    def test_rejects_unverified_direct_branch_fallback(
        self,
        tmp_path: Path,
        repository_template: RepositoryTemplate,
        damage: Callable[[Path, Path, dict[str, Any], str], None],
    ) -> None:
        """Require positive same-repository identity and an auditable direct checkout after gh failure.

        The checkout evidence claims a ``git checkout --no-guess topic`` fallback; each case breaks one of the facts
        that make such a fallback verifiable, and ``prepare`` must refuse with no receipt and no ref change.
        """
        c = _collected(tmp_path, repository_template, cross_repository=False)
        checkout_path = c.pr_dir / "local-checkout.json"
        checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
        checkout["checkout_method"] = "git-original-branch-fallback"
        checkout["command"] = "git checkout --no-guess topic"
        checkout["gh_checkout_failure"] = {
            "command": f"gh pr checkout {PR_URL}",
            "code": "local-pr-checkout-failed",
            "diagnostics": {},
        }
        damage(c.worktree, c.pr_dir, checkout, c.head)
        checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
        receipt = tmp_path / "branch.json"
        before = _git(c.worktree, "show-ref")

        result = _prepare(c, receipt)

        assert result.returncode == 2, result.stderr
        assert "unverified-same-repo-checkout-fallback" in result.stderr
        assert not receipt.exists()
        assert _git(c.worktree, "show-ref") == before


def _no_prior_change(worktree: Path) -> None:
    """Leave the collected branch state as the template made it."""


def _track_topic_on_origin(worktree: Path) -> None:
    """Give the pre-existing local ``topic`` branch the contributor upstream."""
    _git(worktree, "config", "branch.topic.remote", "origin")
    _git(worktree, "config", "branch.topic.merge", "refs/heads/topic")


def _save_topic_away(worktree: Path) -> None:
    """Rename ``topic`` so the direct fallback has to create it."""
    _git(worktree, "branch", "-m", "saved-topic")


def _start_detached(worktree: Path, old: str, head: str) -> None:
    """Start collection from a detached HEAD at an older commit."""
    _git(worktree, "checkout", "--detach", old)


def _start_unrelated(worktree: Path, old: str, head: str) -> None:
    """Start collection from an unrelated branch."""
    _git(worktree, "checkout", "-b", "unrelated", head)


def _start_on_topic(worktree: Path, old: str, head: str) -> None:
    """Start collection from the PR branch itself."""


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(_start_detached, id="detached"),
        pytest.param(_start_unrelated, id="unrelated"),
        pytest.param(_start_on_topic, id="topic"),
    ],
)
@pytest.mark.parametrize(
    ("cross_repository", "gh_checkout_fails", "prior", "gh_local_branch", "push_args", "push_line"),
    [
        pytest.param(False, False, _no_prior_change, "topic", [], "topic -> topic", id="same-repo-gh"),
        pytest.param(True, False, _no_prior_change, "topic", [], "topic -> topic", id="fork-gh"),
        pytest.param(
            True,
            False,
            _no_prior_change,
            "contributor/topic",
            [FORK_REMOTE, "HEAD:refs/heads/topic"],
            "HEAD -> topic",
            id="fork-gh-local-alias",
        ),
        pytest.param(False, True, _track_topic_on_origin, "topic", [], "topic -> topic", id="same-repo-git-existing"),
        pytest.param(False, True, _save_topic_away, "topic", [], "topic -> topic", id="same-repo-git-new"),
    ],
)
def test_prepared_branch_retains_next_commit_and_original_pr_push_destination(
    tmp_path: Path,
    repository_template: RepositoryTemplate,
    monkeypatch: pytest.MonkeyPatch,
    start: Callable[[Path, str, str], None],
    cross_repository: bool,
    gh_checkout_fails: bool,
    prior: Callable[[Path], None],
    gh_local_branch: str,
    push_args: list[str],
    push_line: str,
) -> None:
    """Carry collection through commit and verify the original remote ref, including fork aliases.

    From each starting state and checkout route, ``prepare`` lands on the PR branch without moving any ref or touching
    user files, the next commit extends only that branch, a dry-run push targets the original PR ref, and stale or
    switched-branch receipts are rejected.
    """
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("LC_ALL", "C")
    source, worktree, base, old, head = _setup_repositories(tmp_path, repository_template)
    prior(worktree)
    start(worktree, old, head)
    pr_dir = tmp_path / "pr"
    code, _ = _collect(
        _load_collector(),
        source,
        worktree,
        pr_dir,
        base,
        head,
        cross_repository=cross_repository,
        checkout_mode="remediate",
        gh_checkout_fails=gh_checkout_fails,
        gh_local_branch=gh_local_branch,
    )
    assert code == 0
    (worktree / "notes.txt").write_text("user notes\n", encoding="utf-8")
    (worktree / "scratch.txt").write_text("user scratch\n", encoding="utf-8")
    original_refs = _git(worktree, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
    receipt = tmp_path / "branch.json"
    prepare = _run_helper(worktree, "prepare", "--pr-dir", str(pr_dir), "--receipt", str(receipt))
    assert prepare.returncode == 0, prepare.stderr
    recorded = json.loads(receipt.read_text(encoding="utf-8"))
    branch = _git(worktree, "branch", "--show-current")
    assert (branch, branch, recorded["head_ref"]) == (recorded["branch"], gh_local_branch, "topic")
    assert recorded["head_repository"] == ("contributor/project" if cross_repository else "example/project")
    assert _git(worktree, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/") == original_refs
    assert _git(worktree, "rev-parse", "HEAD") == head
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "user notes\n"
    assert (worktree / "scratch.txt").read_text(encoding="utf-8") == "user scratch\n"
    _git(worktree, "commit", "--allow-empty", "-m", "local correction" + CO_AUTHOR)
    committed = _git(worktree, "rev-parse", "HEAD")
    assert _git(worktree, "for-each-ref", "--contains=HEAD", "--format=%(refname:short)", "refs/heads/") == branch
    expected_refs = "\n".join(
        f"{ref} {committed if ref == f'refs/heads/{branch}' else oid}"
        for ref, oid in (line.split() for line in original_refs.splitlines())
    )
    assert _git(worktree, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/") == expected_refs
    checked = _check(worktree, receipt, committed)
    assert checked.returncode == 0, checked.stderr
    source_refs = _git(source, "show-ref")
    destination = FORK_REMOTE if cross_repository else SAME_REPO_REMOTE
    pushed = subprocess.run(
        [
            "git",
            "-c",
            f"url.{source.as_posix()}.insteadOf={destination}",
            "-c",
            "push.default=simple",
            "-c",
            "push.autoSetupRemote=false",
            "push",
            "--dry-run",
            *push_args,
        ],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pushed.returncode == 0, pushed.stderr
    assert push_line in pushed.stderr
    assert "[new branch]" not in pushed.stderr
    assert _git(source, "show-ref") == source_refs
    stale = _check(worktree, receipt, head)
    assert stale.returncode == 2
    assert "head-mismatch" in stale.stderr
    _git(worktree, "checkout", "-b", "other-destination", committed)
    rejected = _check(worktree, receipt, committed, flags=("-X", "faulthandler"))
    assert rejected.returncode == 2, rejected.stderr
    assert "branch-mismatch" in rejected.stderr


@dataclass
class _Receipt:
    """A prepared receipt plus the facts a mismatch case may alter before ``check``."""

    source: Path
    worktree: Path
    base: str
    head: str
    local_branch: str
    record: dict[str, Any]
    expected: str


def _no_mismatch(r: _Receipt) -> None:
    """Leave the receipt and repository untouched."""


def _missing_branch(r: _Receipt) -> None:
    """Name a branch that does not exist."""
    r.record["branch"] = "missing-destination"


def _foreign_worktree(r: _Receipt) -> None:
    """Name a different worktree."""
    r.record["worktree"] = r.source.resolve().as_posix()


def _stale_expected_head(r: _Receipt) -> None:
    """Expect an older head than the receipt recorded."""
    r.expected = r.base


def _unrelated_ancestry(r: _Receipt) -> None:
    """Record an initial head that the current branch does not descend from."""
    _git(r.worktree, "commit", "--allow-empty", "-m", "later revision")
    r.record["initial_head"] = _git(r.worktree, "rev-parse", "HEAD")
    _git(r.worktree, "checkout", "--detach", r.head)
    _git(r.worktree, "checkout", "-b", "earlier-destination")
    r.record["branch"] = "earlier-destination"


def _legacy_schema(r: _Receipt) -> None:
    """Downgrade the receipt to the legacy schema version."""
    r.record["schema_version"] = 1


def _other_upstream(r: _Receipt) -> None:
    """Track ``main`` instead of the PR branch."""
    _git(r.worktree, "config", f"branch.{r.local_branch}.merge", "refs/heads/main")


def _other_remote(r: _Receipt) -> None:
    """Point the branch remote at ``origin``."""
    _git(r.worktree, "config", f"branch.{r.local_branch}.remote", "origin")


def _push_remote(r: _Receipt) -> None:
    """Configure a push remote, which the check must ignore."""
    _git(r.worktree, "config", f"branch.{r.local_branch}.pushRemote", "origin")


@pytest.mark.parametrize("gh_local_branch", ["topic", "contributor/topic"])
@pytest.mark.parametrize(
    ("mismatch", "exit_code"),
    [
        pytest.param(_no_mismatch, 0, id="none"),
        pytest.param(_missing_branch, 2, id="branch"),
        pytest.param(_foreign_worktree, 2, id="worktree"),
        pytest.param(_stale_expected_head, 2, id="head"),
        pytest.param(_unrelated_ancestry, 2, id="ancestry"),
        pytest.param(_legacy_schema, 2, id="legacy"),
        pytest.param(_other_upstream, 2, id="upstream"),
        pytest.param(_other_remote, 2, id="remote"),
        pytest.param(_push_remote, 0, id="push-remote"),
    ],
)
def test_receipt_checks_actual_state_without_repair(
    tmp_path: Path,
    repository_template: RepositoryTemplate,
    mismatch: Callable[[_Receipt], None],
    exit_code: int,
    gh_local_branch: str,
) -> None:
    """Reject destination or revision drift without repairing or discarding local work.

    One fact of a prepared receipt or its repository drifts; ``check`` exits 2 for every real drift (and 0 when nothing
    relevant changed) and never rewrites the receipt, the branch, or HEAD.
    """
    c = _collected(tmp_path, repository_template, gh_local_branch=gh_local_branch)
    receipt = tmp_path / "branch.json"
    r = _Receipt(c.source, c.worktree, c.base, c.head, gh_local_branch, _prepared(c, receipt), c.head)
    mismatch(r)
    receipt.write_text(json.dumps(r.record), encoding="utf-8")
    before = receipt.read_bytes()
    branch = _git(c.worktree, "branch", "--show-current")

    result = _check(c.worktree, receipt, r.expected)

    assert result.returncode == exit_code, result.stderr
    assert receipt.read_bytes() == before
    assert _git(c.worktree, "branch", "--show-current") == branch
    assert _git(c.worktree, "rev-parse", "HEAD") == c.head


@dataclass
class _Legacy:
    """A legacy-receipt recovery scenario with real user work in the worktree."""

    source: Path
    worktree: Path
    base: str
    head: str
    pr_dir: Path
    legacy: Path
    receipt: Path
    record: dict[str, Any]
    expected: str


def _legacy_scenario(tmp_path: Path, template: RepositoryTemplate, cross_repository: bool, host: str) -> _Legacy:
    """Collect a PR, rewrite its evidence to the legacy form on ``host``, and add an authorized commit and user work."""
    c = _collected(tmp_path, template, cross_repository=cross_repository)
    checkout_path = c.pr_dir / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    pr_path = c.pr_dir / "pr.json"
    pr = json.loads(pr_path.read_text(encoding="utf-8"))
    pr["url"] = f"https://{host}/example/project/pull/17"
    pr_path.write_text(json.dumps(pr), encoding="utf-8")
    checkout["pr_url"] = pr["url"]
    checkout.pop("checkout_mode", None)
    checkout.pop("checkout_method", None)
    checkout["command"] = "not-run: already at expected PR head"
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
    record = {
        "schema_version": 1,
        "status": "prepared",
        "pr_number": 17,
        "pr_url": pr["url"],
        "initial_head": c.head,
        "branch": "codex/pr-17-0123456789ab",
        "worktree": c.worktree.resolve().as_posix(),
    }
    _git(c.worktree, "commit", "--allow-empty", "-m", "authorized integration")
    expected = _git(c.worktree, "rev-parse", "HEAD")
    (c.worktree / "module.py").write_text("value = 'fixed'\n", encoding="utf-8")
    (c.worktree / "notes.txt").write_text("updated documentation\n", encoding="utf-8")
    (c.worktree / "user.txt").write_text("preserved user work\n", encoding="utf-8")
    return _Legacy(
        c.source,
        c.worktree,
        c.base,
        c.head,
        c.pr_dir,
        tmp_path / "legacy.json",
        tmp_path / "recovered.json",
        record,
        expected,
    )


def _attempt_recovery(s: _Legacy) -> tuple[subprocess.CompletedProcess[str], bytes, tuple[str, str, str]]:
    """Write the legacy receipt, snapshot the repository, and run ``recover``."""
    s.legacy.write_text(json.dumps(s.record), encoding="utf-8")
    legacy_bytes = s.legacy.read_bytes()
    before = _snapshot(s.worktree)
    result = _run_helper(
        s.worktree,
        "recover",
        "--legacy-receipt",
        str(s.legacy),
        "--pr-dir",
        str(s.pr_dir),
        "--receipt",
        str(s.receipt),
        "--expected-head",
        s.expected,
    )
    return result, legacy_bytes, before


def _commit_each_fix_and_check(s: _Legacy) -> tuple[list[int], list[str]]:
    """Check the receipt before and after committing each fixed file; return exit codes and committed file names."""
    codes: list[int] = []
    committed_files: list[str] = []
    for path in ("module.py", "notes.txt"):
        codes.append(_check(s.worktree, s.receipt, s.expected).returncode)
        _git(s.worktree, "add", "--", path)
        _git(s.worktree, "commit", "-m", f"Fix {path}{CO_AUTHOR}")
        committed_files.append(_git(s.worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"))
        s.expected = _git(s.worktree, "rev-parse", "HEAD")
        codes.append(_check(s.worktree, s.receipt, s.expected).returncode)
    return codes, committed_files


def _legacy_unchanged(s: _Legacy) -> None:
    """Keep the legacy scenario as built."""


def _wrong_push_remote(s: _Legacy) -> None:
    """Configure an unrelated push remote, which recovery must tolerate."""
    _git(s.worktree, "config", "remote.wrong.url", "https://github.com/other/project.git")
    _git(s.worktree, "config", "branch.topic.pushRemote", "wrong")


def _expect_original_head(s: _Legacy) -> None:
    """Expect the original head, so the authorized commit is not accounted for."""
    s.expected = s.head


def _unrelated_root(s: _Legacy) -> None:
    """Replace ``topic`` with an orphan branch sharing no history with the legacy head."""
    _git(s.worktree, "branch", "-m", "saved-topic")
    _git(s.worktree, "checkout", "--orphan", "topic")
    _git(s.worktree, "commit", "-am", "unrelated root")
    s.expected = _git(s.worktree, "rev-parse", "HEAD")


def _foreign_legacy_worktree(s: _Legacy) -> None:
    """Make the legacy receipt name the source repository as its worktree."""
    s.record["worktree"] = s.source.resolve().as_posix()


def _other_pr_number(s: _Legacy) -> None:
    """Make the legacy receipt name another pull request."""
    s.record["pr_number"] = 18


def _other_source_head(s: _Legacy) -> None:
    """Make the legacy receipt record a different initial head."""
    s.record["initial_head"] = s.base


def _renamed_destination(s: _Legacy) -> None:
    """Rename the checked-out branch away from the PR destination."""
    _git(s.worktree, "branch", "-m", "wrong-destination")


def _main_upstream(s: _Legacy) -> None:
    """Track ``main`` instead of the PR branch."""
    _git(s.worktree, "config", "branch.topic.merge", "refs/heads/main")


def _existing_receipt(s: _Legacy) -> None:
    """Pre-create the output receipt so recovery would overwrite evidence."""
    s.receipt.write_text("existing evidence\n", encoding="utf-8")


def _merge_in_progress(s: _Legacy) -> None:
    """Leave an unfinished merge operation in the worktree."""
    (s.worktree / _git(s.worktree, "rev-parse", "--git-path", "MERGE_HEAD")).write_text(s.head, encoding="utf-8")


@pytest.mark.parametrize("cross_repository", [False, True])
@pytest.mark.parametrize("host", ["github.com", "GITHUB.COM"])
class TestLegacyRecovery:
    """Recovery of runs prepared before receipts recorded the PR destination."""

    @pytest.mark.parametrize(
        "tamper", [pytest.param(_legacy_unchanged, id="none"), pytest.param(_wrong_push_remote, id="push")]
    )
    def test_preserves_work_and_enables_topic_commits(
        self,
        tmp_path: Path,
        repository_template: RepositoryTemplate,
        cross_repository: bool,
        host: str,
        tamper: Callable[[_Legacy], None],
    ) -> None:
        """Resume legacy runs only on the verified original destination without replaying checkout.

        Recovery writes a schema-2 receipt for the original ``topic`` destination, leaves the legacy file, refs, status
        and config untouched, and the receipt keeps validating as each fixed file is committed. Re-tracking the pull ref
        afterwards is rejected without rewriting the receipt.
        """
        s = _legacy_scenario(tmp_path, repository_template, cross_repository, host)
        tamper(s)

        result, legacy_bytes, before = _attempt_recovery(s)

        assert result.returncode == 0, result.stderr
        assert s.legacy.read_bytes() == legacy_bytes
        assert _snapshot(s.worktree) == before
        assert (s.worktree / "user.txt").read_text(encoding="utf-8") == "preserved user work\n"
        record = json.loads(s.receipt.read_text(encoding="utf-8"))
        assert record["schema_version"] == 2
        assert record["branch"] == "topic"
        assert record["initial_head"] == s.head
        assert record["head_repository"] == ("contributor/project" if cross_repository else "example/project")
        assert record["recovery"]["expected_head"] == s.expected
        assert record["recovery"]["legacy_branch"] == s.record["branch"]
        recovered_bytes = s.receipt.read_bytes()
        codes, committed_files = _commit_each_fix_and_check(s)
        assert (codes, committed_files) == ([0, 0, 0, 0], ["module.py", "notes.txt"])
        assert s.receipt.read_bytes() == recovered_bytes
        assert s.legacy.read_bytes() == legacy_bytes
        assert _git(s.worktree, "status", "--porcelain=v1") == "?? user.txt"
        _git(s.worktree, "config", "branch.topic.remote", "origin")
        _git(s.worktree, "config", "branch.topic.merge", "refs/pull/17/head")
        changed_tracking = _check(s.worktree, s.receipt, s.expected)
        assert changed_tracking.returncode == 2
        assert "pr-upstream-mismatch" in changed_tracking.stderr
        assert s.receipt.read_bytes() == recovered_bytes

    @pytest.mark.parametrize(
        ("tamper", "receipt_text"),
        [
            pytest.param(_expect_original_head, None, id="head"),
            pytest.param(_unrelated_root, None, id="ancestry"),
            pytest.param(_foreign_legacy_worktree, None, id="worktree"),
            pytest.param(_other_pr_number, None, id="pr"),
            pytest.param(_other_source_head, None, id="source-head"),
            pytest.param(_renamed_destination, None, id="branch"),
            pytest.param(_main_upstream, None, id="upstream"),
            pytest.param(_existing_receipt, "existing evidence\n", id="existing"),
            pytest.param(_merge_in_progress, None, id="operation"),
        ],
    )
    def test_rejects_unverified_state_without_changes(
        self,
        tmp_path: Path,
        repository_template: RepositoryTemplate,
        cross_repository: bool,
        host: str,
        tamper: Callable[[_Legacy], None],
        receipt_text: str | None,
    ) -> None:
        """Recovery refuses a legacy run whose branch, head, PR or worktree cannot be verified.

        Each case breaks one fact; ``recover`` exits 2, writes no receipt (an existing one stays as it was), and leaves
        the legacy file, refs, status, config and user work exactly as found.
        """
        s = _legacy_scenario(tmp_path, repository_template, cross_repository, host)
        tamper(s)

        result, legacy_bytes, before = _attempt_recovery(s)

        assert result.returncode == 2, result.stderr
        assert s.legacy.read_bytes() == legacy_bytes
        assert _snapshot(s.worktree) == before
        assert (s.worktree / "user.txt").read_text(encoding="utf-8") == "preserved user work\n"
        assert (s.receipt.read_text(encoding="utf-8") if s.receipt.exists() else None) == receipt_text
