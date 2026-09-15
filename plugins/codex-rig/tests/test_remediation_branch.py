"""Real-Git checks for retaining remediation work on its intended local branch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from test_collect_pr_git_recovery import _collect, _git, _load_collector, _setup_repositories


HELPER = Path(__file__).resolve().parents[1] / "shared" / "remediation_branch.py"


@pytest.mark.parametrize("cross_repository", [False, True])
@pytest.mark.parametrize(
    "invalid",
    [
        "none",
        "head",
        "ancestry",
        "worktree",
        "pr",
        "source-head",
        "branch",
        "upstream",
        "push",
        "existing",
        "operation",
    ],
)
def test_legacy_recovery_preserves_work_and_enables_topic_commits(
    tmp_path: Path, cross_repository: bool, invalid: str
) -> None:
    """Resume legacy runs only on the verified original destination without replaying checkout."""
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    pr_dir = tmp_path / "pr"
    assert (
        _collect(
            _load_collector(),
            source,
            worktree,
            pr_dir,
            base,
            head,
            cross_repository=cross_repository,
            checkout_mode="remediate",
            gh_checkout_fails=False,
        )[0]
        == 0
    )
    legacy = tmp_path / "legacy.json"
    checkout_path = pr_dir / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout.pop("checkout_mode", None)
    checkout.pop("checkout_method", None)
    checkout["command"] = "not-run: already at expected PR head"
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
    legacy_record = {
        "schema_version": 1,
        "status": "prepared",
        "pr_number": 17,
        "pr_url": "https://github.com/example/project/pull/17",
        "initial_head": head,
        "branch": "codex/pr-17-0123456789ab",
        "worktree": worktree.resolve().as_posix(),
    }
    _git(worktree, "commit", "--allow-empty", "-m", "authorized integration")
    expected = _git(worktree, "rev-parse", "HEAD")
    (worktree / "module.py").write_text("value = 'fixed'\n", encoding="utf-8")
    (worktree / "notes.txt").write_text("updated documentation\n", encoding="utf-8")
    (worktree / "user.txt").write_text("preserved user work\n", encoding="utf-8")
    receipt = tmp_path / "recovered.json"
    if invalid == "head":
        expected = head
    elif invalid == "ancestry":
        _git(worktree, "branch", "-m", "saved-topic")
        _git(worktree, "checkout", "--orphan", "topic")
        _git(worktree, "commit", "-am", "unrelated root")
        expected = _git(worktree, "rev-parse", "HEAD")
    elif invalid == "worktree":
        legacy_record["worktree"] = source.resolve().as_posix()
    elif invalid == "pr":
        legacy_record["pr_number"] = 18
    elif invalid == "source-head":
        legacy_record["initial_head"] = base
    elif invalid == "branch":
        _git(worktree, "branch", "-m", "wrong-destination")
    elif invalid == "upstream":
        _git(worktree, "config", "branch.topic.merge", "refs/heads/main")
    elif invalid == "push":
        _git(worktree, "config", "remote.wrong.url", "https://github.com/other/project.git")
        _git(worktree, "config", "branch.topic.pushRemote", "wrong")
    elif invalid == "existing":
        receipt.write_text("existing evidence\n", encoding="utf-8")
    elif invalid == "operation":
        (worktree / _git(worktree, "rev-parse", "--git-path", "MERGE_HEAD")).write_text(head, encoding="utf-8")
    legacy.write_text(json.dumps(legacy_record), encoding="utf-8")
    before = legacy.read_bytes()
    before_refs = _git(worktree, "show-ref")
    before_status = _git(worktree, "status", "--porcelain=v1")
    before_config = _git(worktree, "config", "--local", "--list")
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "recover",
            "--legacy-receipt",
            str(legacy),
            "--pr-dir",
            str(pr_dir),
            "--receipt",
            str(receipt),
            "--expected-head",
            expected,
        ],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if invalid == "none" else 2), result.stderr
    assert legacy.read_bytes() == before
    assert _git(worktree, "show-ref") == before_refs
    assert _git(worktree, "status", "--porcelain=v1") == before_status
    assert _git(worktree, "config", "--local", "--list") == before_config
    assert (worktree / "user.txt").read_text(encoding="utf-8") == "preserved user work\n"
    if invalid != "none":
        assert (
            receipt.read_text(encoding="utf-8") == "existing evidence\n"
            if invalid == "existing"
            else not receipt.exists()
        )
        return

    record = json.loads(receipt.read_text(encoding="utf-8"))
    assert record["schema_version"] == 2
    assert record["branch"] == "topic"
    assert record["initial_head"] == head
    assert record["head_repository"] == ("contributor/project" if cross_repository else "example/project")
    assert record["recovery"]["expected_head"] == expected
    assert record["recovery"]["legacy_branch"] == legacy_record["branch"]
    recovered_bytes = receipt.read_bytes()
    for path in ("module.py", "notes.txt"):
        for committed in (False, True):
            if committed:
                _git(worktree, "add", "--", path)
                _git(worktree, "commit", "-m", f"Fix {path}\n\nCo-authored-by: Codex <codex@openai.com>")
                assert _git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD") == path
                expected = _git(worktree, "rev-parse", "HEAD")
            checked = subprocess.run(
                [sys.executable, str(HELPER), "check", "--receipt", str(receipt), "--expected-head", expected],
                cwd=worktree,
                capture_output=True,
                text=True,
                check=False,
            )
            assert checked.returncode == 0, checked.stderr
    assert receipt.read_bytes() == recovered_bytes
    assert legacy.read_bytes() == before
    assert _git(worktree, "status", "--porcelain=v1") == "?? user.txt"


def test_prepare_retains_collected_github_branch(tmp_path: Path) -> None:
    """Never replace a PR branch with a generated branch before the user's next commit."""
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    pr_dir = tmp_path / "pr"
    assert (
        _collect(
            _load_collector(), source, worktree, pr_dir, base, head, checkout_mode="remediate", gh_checkout_fails=False
        )[0]
        == 0
    )
    before = _git(worktree, "show-ref")
    result = subprocess.run(
        [sys.executable, str(HELPER), "prepare", "--pr-dir", str(pr_dir), "--receipt", str(tmp_path / "branch.json")],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert _git(worktree, "branch", "--show-current") == "topic"
    assert _git(worktree, "show-ref") == before


@pytest.mark.parametrize("starting_state", ["detached", "unrelated", "topic"])
@pytest.mark.parametrize(
    ("cross_repository", "gh_checkout_fails", "missing_branch"),
    [
        pytest.param(False, False, False, id="same-repo-gh"),
        pytest.param(True, False, False, id="fork-gh"),
        pytest.param(False, True, False, id="same-repo-git-existing"),
        pytest.param(False, True, True, id="same-repo-git-new"),
    ],
)
def test_prepared_branch_retains_next_commit_and_original_pr_push_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    starting_state: str,
    cross_repository: bool,
    gh_checkout_fails: bool,
    missing_branch: bool,
) -> None:
    """Carry collection through commit and default push selection without changing unrelated branches."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("LC_ALL", "C")
    source, worktree, base, old, head = _setup_repositories(tmp_path)
    if missing_branch:
        _git(worktree, "branch", "-m", "saved-topic")
    elif gh_checkout_fails:
        _git(worktree, "config", "branch.topic.remote", "origin")
        _git(worktree, "config", "branch.topic.merge", "refs/heads/topic")
    if starting_state == "detached":
        _git(worktree, "checkout", "--detach", old)
    elif starting_state == "unrelated":
        _git(worktree, "checkout", "-b", "unrelated", head)
    code, _ = _collect(
        _load_collector(),
        source,
        worktree,
        tmp_path / "pr",
        base,
        head,
        cross_repository=cross_repository,
        checkout_mode="remediate",
        gh_checkout_fails=gh_checkout_fails,
    )
    assert code == 0
    (worktree / "notes.txt").write_text("user notes\n", encoding="utf-8")
    (worktree / "scratch.txt").write_text("user scratch\n", encoding="utf-8")
    original_refs = _git(worktree, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
    receipt = tmp_path / "branch.json"
    prepare = subprocess.run(
        [sys.executable, str(HELPER), "prepare", "--pr-dir", str(tmp_path / "pr"), "--receipt", str(receipt)],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert prepare.returncode == 0, prepare.stderr
    recorded = json.loads(receipt.read_text(encoding="utf-8"))
    branch = _git(worktree, "branch", "--show-current")
    assert branch == recorded["branch"]
    assert branch == "topic"
    assert recorded["head_ref"] == "topic"
    assert recorded["head_repository"] == ("contributor/project" if cross_repository else "example/project")
    assert _git(worktree, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/") == original_refs
    assert _git(worktree, "rev-parse", "HEAD") == head
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "user notes\n"
    assert (worktree / "scratch.txt").read_text(encoding="utf-8") == "user scratch\n"
    _git(worktree, "commit", "--allow-empty", "-m", "local correction\n\nCo-authored-by: Codex <codex@openai.com>")
    committed = _git(worktree, "rev-parse", "HEAD")
    assert _git(worktree, "for-each-ref", "--contains=HEAD", "--format=%(refname:short)", "refs/heads/") == branch
    for line in original_refs.splitlines():
        ref, oid = line.split()
        assert _git(worktree, "rev-parse", ref) == (committed if ref == f"refs/heads/{branch}" else oid)
    checked = subprocess.run(
        [sys.executable, str(HELPER), "check", "--receipt", str(receipt), "--expected-head", committed],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    source_refs = _git(source, "show-ref")
    destination = (
        "https://github.com/contributor/project.git" if cross_repository else "https://github.com/example/project.git"
    )
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
        ],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert pushed.returncode == 0, pushed.stderr
    assert "topic -> topic" in pushed.stderr
    assert "[new branch]" not in pushed.stderr
    assert _git(source, "show-ref") == source_refs
    stale = subprocess.run(
        [sys.executable, str(HELPER), "check", "--receipt", str(receipt), "--expected-head", head],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert stale.returncode == 2
    assert "head-mismatch" in stale.stderr
    _git(worktree, "checkout", "-b", "other-destination", committed)
    rejected = subprocess.run(
        [sys.executable, str(HELPER), "check", "--receipt", str(receipt), "--expected-head", committed],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "branch-mismatch" in rejected.stderr


@pytest.mark.parametrize(
    "mismatch", ["none", "branch", "worktree", "head", "ancestry", "legacy", "upstream", "remote", "push-remote"]
)
def test_receipt_checks_actual_state_without_repair(tmp_path: Path, mismatch: str) -> None:
    """Reject destination or revision drift without repairing or discarding local work."""
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    assert (
        _collect(
            _load_collector(),
            source,
            worktree,
            tmp_path / "pr",
            base,
            head,
            checkout_mode="remediate",
            gh_checkout_fails=False,
        )[0]
        == 0
    )
    receipt = tmp_path / "branch.json"
    prepared = subprocess.run(
        [sys.executable, str(HELPER), "prepare", "--pr-dir", str(tmp_path / "pr"), "--receipt", str(receipt)],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    record = json.loads(receipt.read_text(encoding="utf-8"))
    expected = head
    if mismatch == "branch":
        record["branch"] = "missing-destination"
    elif mismatch == "worktree":
        record["worktree"] = source.resolve().as_posix()
    elif mismatch == "head":
        expected = base
    elif mismatch == "ancestry":
        _git(worktree, "commit", "--allow-empty", "-m", "later revision")
        record["initial_head"] = _git(worktree, "rev-parse", "HEAD")
        _git(worktree, "checkout", "--detach", head)
        _git(worktree, "checkout", "-b", "earlier-destination")
        record["branch"] = "earlier-destination"
    elif mismatch == "legacy":
        record["schema_version"] = 1
    elif mismatch == "upstream":
        _git(worktree, "config", "branch.topic.merge", "refs/heads/main")
    elif mismatch == "remote":
        _git(worktree, "config", "branch.topic.remote", "origin")
    elif mismatch == "push-remote":
        _git(worktree, "config", "branch.topic.pushRemote", "origin")
    receipt.write_text(json.dumps(record), encoding="utf-8")
    before = receipt.read_bytes()
    branch = _git(worktree, "branch", "--show-current")
    result = subprocess.run(
        [sys.executable, str(HELPER), "check", "--receipt", str(receipt), "--expected-head", expected],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if mismatch == "none" else 2), result.stderr
    assert receipt.read_bytes() == before
    assert _git(worktree, "branch", "--show-current") == branch
    assert _git(worktree, "rev-parse", "HEAD") == head


def test_branch_preparation_rejects_changed_head_and_preserves_dirty_files(tmp_path: Path) -> None:
    """Never replace local commits or discard unrelated changes while preparing a branch."""
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    assert (
        _collect(
            _load_collector(),
            source,
            worktree,
            tmp_path / "pr",
            base,
            head,
            checkout_mode="remediate",
            gh_checkout_fails=False,
        )[0]
        == 0
    )
    (worktree / "notes.txt").write_text("keep user notes\n", encoding="utf-8")
    _git(worktree, "commit", "--allow-empty", "-m", "user commit\n\nCo-authored-by: Codex <codex@openai.com>")
    changed = _git(worktree, "rev-parse", "HEAD")
    receipt = tmp_path / "branch.json"
    result = subprocess.run(
        [sys.executable, str(HELPER), "prepare", "--pr-dir", str(tmp_path / "pr"), "--receipt", str(receipt)],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "head-mismatch" in result.stderr
    assert _git(worktree, "rev-parse", "HEAD") == changed
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "keep user notes\n"
    assert not receipt.exists()


@pytest.mark.parametrize("invalid", ["review", "detached", "missing-upstream", "pull-ref", "custom-push", "renamed"])
def test_prepare_rejects_unusable_destination_without_git_mutation(tmp_path: Path, invalid: str) -> None:
    """Do not approve editing when checkout evidence or the PR publication destination is missing."""
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    pr_dir = tmp_path / "pr"
    assert (
        _collect(
            _load_collector(),
            source,
            worktree,
            pr_dir,
            base,
            head,
            cross_repository=False,
            checkout_mode="remediate",
            gh_checkout_fails=False,
        )[0]
        == 0
    )
    if invalid == "review":
        checkout_path = pr_dir / "local-checkout.json"
        checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
        checkout["checkout_mode"] = "review"
        checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
    elif invalid == "detached":
        _git(worktree, "checkout", "--detach", head)
    elif invalid == "missing-upstream":
        _git(worktree, "config", "--unset", "branch.topic.merge")
    elif invalid == "pull-ref":
        _git(worktree, "config", "branch.topic.merge", "refs/pull/17/head")
    elif invalid == "custom-push":
        _git(worktree, "config", "remote.origin.push", "HEAD:refs/heads/main")
    else:
        _git(worktree, "branch", "-m", "contributor/topic")
        checkout_path = pr_dir / "local-checkout.json"
        checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
        checkout["local_branch"] = "contributor/topic"
        checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
        pushed = subprocess.run(
            ["git", "-c", "protocol.allow=never", "-c", "push.default=simple", "push", "--dry-run"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        assert pushed.returncode == 128
        assert "does not match" in pushed.stderr
    before_refs = _git(worktree, "show-ref")
    before_branch = _git(worktree, "branch", "--show-current")
    before_config = _git(worktree, "config", "--local", "--list")
    receipt = tmp_path / "branch.json"
    result = subprocess.run(
        [sys.executable, str(HELPER), "prepare", "--pr-dir", str(pr_dir), "--receipt", str(receipt)],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    if invalid == "renamed":
        assert "pr-branch-name-mismatch" in result.stderr
    assert not receipt.exists()
    assert _git(worktree, "show-ref") == before_refs
    assert _git(worktree, "branch", "--show-current") == before_branch
    assert _git(worktree, "config", "--local", "--list") == before_config


@pytest.mark.parametrize("invalid", ["fork", "missing-failure", "missing-gh-command", "wrong-command"])
def test_prepare_rejects_unverified_direct_branch_fallback(tmp_path: Path, invalid: str) -> None:
    """Require positive same-repository identity and an auditable direct checkout after gh failure."""
    source, worktree, base, _old, head = _setup_repositories(tmp_path)
    pr_dir = tmp_path / "pr"
    assert (
        _collect(
            _load_collector(),
            source,
            worktree,
            pr_dir,
            base,
            head,
            cross_repository=False,
            checkout_mode="remediate",
            gh_checkout_fails=False,
        )[0]
        == 0
    )
    checkout_path = pr_dir / "local-checkout.json"
    checkout = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout["checkout_method"] = "git-original-branch-fallback"
    checkout["command"] = "git checkout --no-guess topic"
    checkout["gh_checkout_failure"] = {
        "command": "gh pr checkout https://github.com/example/project/pull/17",
        "code": "local-pr-checkout-failed",
        "diagnostics": {},
    }
    if invalid == "fork":
        pr_path = pr_dir / "pr.json"
        payload = json.loads(pr_path.read_text(encoding="utf-8"))
        # Even a false same-repository flag cannot override the observed head identity.
        payload["headRepository"] = {"nameWithOwner": "contributor/project"}
        pr_path.write_text(json.dumps(payload), encoding="utf-8")
    elif invalid == "missing-failure":
        checkout["gh_checkout_failure"] = None
    elif invalid == "missing-gh-command":
        checkout["gh_checkout_failure"].pop("command")
    else:
        checkout["command"] = f"git checkout --detach {head}"
    checkout_path.write_text(json.dumps(checkout), encoding="utf-8")
    receipt = tmp_path / "branch.json"
    before = _git(worktree, "show-ref")
    result = subprocess.run(
        [sys.executable, str(HELPER), "prepare", "--pr-dir", str(pr_dir), "--receipt", str(receipt)],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "unverified-same-repo-checkout-fallback" in result.stderr
    assert not receipt.exists()
    assert _git(worktree, "show-ref") == before
