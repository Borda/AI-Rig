"""Exercise cleanup and offline replay for workflow-owned local reviews in real Git repositories."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _git_fixtures import git_text
from _modules import load_module

SHARED = Path(__file__).resolve().parents[3] / "shared"


@pytest.fixture
def local_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Acquire an ordinary local claim while preserving a deliberately mixed caller checkout."""
    monkeypatch.syspath_prepend(str(SHARED))
    helper = load_module("local_cleanup_test", SHARED / "review_worktree_cleanup.py")
    source = tmp_path / "source"
    source.mkdir()
    git_text(source, "init", "-q")
    (source / "tracked").write_bytes(b"base\n")
    (source / ".gitignore").write_bytes(b"*.cache\n")
    git_text(source, "add", ".")
    git_text(source, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    (source / "tracked").write_bytes(b"staged\n")
    git_text(source, "add", "tracked")
    (source / "tracked").write_bytes(b"unstaged\n")
    (source / "new").write_bytes(b"new\x00file")
    (source / "mine.cache").write_bytes(b"caller cache")
    run = tmp_path / "code-review" / "run"
    run.mkdir(parents=True)
    slot = helper.review_checkout_slot._checkout_slot(source, local=True)
    helper.review_checkout_slot._schedule("acquire", slot, "owner", False, reuse_owner=True)
    command = ["--run", str(run), "--slot", str(slot), "--owner", "owner"]
    return helper, source, run, command


@pytest.mark.integration
@pytest.mark.parametrize("scope", ["working-tree", "path", "commit"])
def test_local_cleanup_removes_only_its_owned_checkout(
    local_source, scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain the caller’s staged, unstaged, untracked and ignored data while removing the disposable checkout."""
    helper, source, run, command = local_source
    head = git_text(source, "rev-parse", "HEAD").strip()
    create = ["--create-local", scope, "--repository", str(source), *command]
    assert helper.main(create + (["--head", head] if scope == "commit" else [])) == 0
    path = run / "local-worktree-owner.json"
    checkout = json.loads(path.read_bytes())
    mirror = Path(checkout["worktree"])
    assert mirror.is_dir()
    (run / "result.json").write_text(json.dumps({"status": "pass", "metadata": {"scope": scope}}), encoding="utf-8")
    prepare = load_module("local_plan_replay_test", SHARED.parent / "skills/code-review/review_prepare.py")
    selected = ["new", "tracked"] if scope == "working-tree" else ["tracked"]
    (run / "diff.patch").write_bytes((run / "local-source/diff.patch").read_bytes() if scope != "commit" else b"")
    (run / "files.txt").write_bytes(b"tracked\n" if scope != "commit" else b"")
    (run / "untracked.txt").write_bytes(b"new\n" if scope == "working-tree" else b"")
    plan = {
        "source_root": mirror.as_posix(),
        "scope_path": "tracked" if scope == "path" else None,
        "expected_head": head if scope == "commit" else None,
        "expected_diff_base": head if scope == "commit" else None,
        "source_snapshot": prepare.collect_diff.capture_source_snapshot(mirror, selected),
    }
    plan_path = run / "direct-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    live = prepare.validate_plan_source(run, plan_path, plan, plan, selected)
    (run / "specialist-manifest.json").write_bytes(b"{}")
    monkeypatch.setattr(helper, "_verify_completion", lambda *_: None)
    cleanup = [*command, "--worktree", str(mirror), "--checkout-sha256", hashlib.sha256(path.read_bytes()).hexdigest()]
    assert helper.main(cleanup) == 0
    assert not mirror.exists()
    assert str(mirror) not in git_text(source, "worktree", "list", "--porcelain")
    assert git_text(source, "show", ":tracked") == "staged"
    assert (source / "tracked").read_bytes() == b"unstaged\n"
    assert (source / "new").read_bytes() == b"new\x00file"
    assert (source / "mine.cache").read_bytes() == b"caller cache"
    assert helper.main(cleanup) == 0
    with prepare.result_source_validation(run, run / "result.json"):
        assert prepare.validate_plan_source(run, plan_path, plan, plan, selected) == live
    (run / "diff.patch").write_bytes(b"changed")
    with pytest.raises(ValueError, match="source-evidence-changed"):
        helper.review_worktree_replay.verified_archive(run, run / "result.json")


@pytest.mark.integration
@pytest.mark.parametrize("changed", ["new", "ignored.cache", "tracked"])
def test_local_cleanup_preserves_changed_mirror(local_source, changed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve fresh ignored files and new edits to either originally tracked or copied untracked files."""
    helper, source, run, command = local_source
    assert helper.main(["--create-local", "working-tree", "--repository", str(source), *command]) == 0
    path = run / "local-worktree-owner.json"
    mirror = Path(json.loads(path.read_bytes())["worktree"])
    (mirror / changed).write_bytes(b"new user data")
    (run / "result.json").write_bytes(b'{"status":"pass","metadata":{"scope":"working-tree"}}')
    (run / "diff.patch").write_bytes(b"")
    (run / "specialist-manifest.json").write_bytes(b"{}")
    monkeypatch.setattr(helper, "_verify_completion", lambda *_: None)
    assert (
        helper.main(
            [*command, "--worktree", str(mirror), "--checkout-sha256", hashlib.sha256(path.read_bytes()).hexdigest()]
        )
        == 1
    )
    assert (mirror / changed).read_bytes() == b"new user data"
    # Git prints forward slashes on every platform, `str(mirror)` carries backslashes on Windows.
    assert mirror.as_posix() in git_text(source, "worktree", "list", "--porcelain")


@pytest.mark.integration
def test_local_completion_replays_after_native_cleanup(
    local_source, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliver byte-identical validated local review text after native removal without recreating its worktree."""
    helper, source, _, command = local_source
    tests = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(tests / "validation"))
    reports = load_module("local_cleanup_report", tests / "handoff/test_find_review_report.py")
    result_path = reports._assessed_local.__wrapped__(tmp_path)
    run = result_path.parent
    (run / "local-checkout.json").unlink()
    command[command.index("--run") + 1] = str(run)
    assert helper.main(["--create-local", "working-tree", "--repository", str(source), *command]) == 0
    owner = run / "local-worktree-owner.json"
    checkout = json.loads(owner.read_bytes())
    head = checkout["expected_head"]
    (run / "reviewed-commit.txt").write_text(head + "\n", encoding="utf-8")
    result = json.loads(result_path.read_bytes())
    result["metadata"]["reviewed_commit_sha"] = head
    handoff = json.loads((run / "final-handoff.json").read_bytes())
    handoff["tables"][0]["rows"][-2]["cells"][1] = head + " (working-tree base)"
    reports._render_and_bind(run, handoff, result)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    complete = [sys.executable, str(SHARED / "find-review-report.py"), "--complete-run", str(run)]
    original = subprocess.run(complete, capture_output=True, check=False)
    assert original.returncode == 0, original.stderr
    digest = hashlib.sha256(owner.read_bytes()).hexdigest()
    assert helper.main([*command, "--worktree", checkout["worktree"], "--checkout-sha256", digest]) == 0
    replay = subprocess.run(complete, capture_output=True, check=False)
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout == original.stdout == (run / "final.md").read_bytes()
    assert not Path(checkout["worktree"]).exists()


@pytest.mark.integration
def test_interrupted_overlay_cleanup_protects_new_entries_and_resumes(
    local_source, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume from a sealed partial overlay while preserving files created after the interruption."""
    helper, source, run, command = local_source
    assert helper.main(["--create-local", "working-tree", "--repository", str(source), *command]) == 0
    path = run / "local-worktree-owner.json"
    mirror = Path(json.loads(path.read_bytes())["worktree"])
    (run / "result.json").write_bytes(b'{"status":"pass","metadata":{"scope":"working-tree"}}')
    (run / "diff.patch").write_bytes(b"")
    (run / "specialist-manifest.json").write_bytes(b"{}")
    monkeypatch.setattr(helper, "_verify_completion", lambda *_: None)
    cleanup = [*command, "--worktree", str(mirror), "--checkout-sha256", hashlib.sha256(path.read_bytes()).hexdigest()]
    unlink = Path.unlink

    def interrupt(path: Path, *args, **kwargs):
        if path == mirror / "new":
            raise OSError("interrupted")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", interrupt)
        assert helper.main(cleanup) == 1
    assert json.loads((run / helper.RECEIPT).read_bytes())["status"] == "removal-authorized"
    assert not (mirror / "tracked").exists()
    (mirror / "new.cache").write_bytes(b"created after interruption")
    assert helper.main(cleanup) == 1
    assert (mirror / "new.cache").read_bytes() == b"created after interruption"
    (mirror / "new.cache").rename(source / "saved.cache")
    assert helper.main(cleanup) == 0
    assert not mirror.exists()
    assert (source / "tracked").read_bytes() == b"unstaged\n"
