"""Verify terminal cleanup using disposable detached Git worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Barrier

import pytest
from _git_fixtures import git_text, historical_review_workspace
from _modules import load_module

SHARED = Path(__file__).resolve().parents[3] / "shared"


@pytest.fixture
def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Prepare real checkout ownership and isolate the independently tested report-completion gate."""
    monkeypatch.syspath_prepend(str(SHARED))
    helper = load_module("cleanup_test", SHARED / "review_worktree_cleanup.py")
    root = tmp_path / "project"
    root.mkdir()
    git_text(root, "init", "-q")
    (root / "file").write_text("tracked\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored\n", encoding="utf-8")
    git_text(root, "add", ".")
    git_text(root, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    head = git_text(root, "rev-parse", "HEAD").strip()
    worktree = tmp_path / "collection-review-worktree"
    git_text(root, "worktree", "add", "--detach", str(worktree), head)
    slot = root / ".git" / "codex-review-slot"
    helper.review_checkout_slot._operate("acquire", slot, "run-owner", False)
    run = tmp_path / "pr-7" / "run-001"
    run.mkdir(parents=True)
    checkout = {
        "pr_number": 7,
        "checkout_mode": "review",
        "checkout_method": "git-detached-review-worktree",
        "status": "checked-out",
        "local_branch": "",
        "local_head": head,
        "expected_head": head,
        "collection_run_dir": (tmp_path / "collection").as_posix(),
        "worktree_location": "report-adjacent",
        "source_worktree": root.as_posix(),
        "worktree": worktree.as_posix(),
        "command": f"git worktree add --detach {worktree} {head}",
        "created_worktree": helper.record_creation(worktree, root / ".git"),
    }
    (run / "local-checkout.json").write_text(json.dumps(checkout), encoding="utf-8")
    (run / "result.json").write_text('{"metadata": {}, "status": "pass"}', encoding="utf-8")
    monkeypatch.setattr(helper, "_verify_completion", lambda *args: None)
    monkeypatch.setattr(helper.review_worktree_replay, "verified_source_inputs", lambda *args: {})
    command = [
        "--run",
        str(run),
        "--worktree",
        str(worktree),
        "--slot",
        str(slot),
        "--owner",
        "run-owner",
        "--checkout-sha256",
        hashlib.sha256((run / "local-checkout.json").read_bytes()).hexdigest(),
        "--no-contributor-execution",
    ]
    return helper, command, run, root, worktree


@pytest.mark.integration
def test_removes_only_verified_clean_worktree_and_records_result(review) -> None:
    """Native removal retains the invoking checkout, result, and exact receipt for later replay."""
    helper, command, run, root, worktree = review
    assert helper.main(command) == 0
    assert not worktree.exists()
    assert str(worktree) not in git_text(root, "worktree", "list", "--porcelain")
    assert (root / "file").read_text(encoding="utf-8") == "tracked\n"
    receipt = json.loads((run / "review-worktree-cleanup.json").read_bytes())
    assert receipt["status"] == "removed"
    assert receipt["head"] == git_text(root, "rev-parse", "HEAD").strip()
    assert receipt["result_sha256"] == hashlib.sha256((run / "result.json").read_bytes()).hexdigest()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("number", "protected", "reason"),
    [
        pytest.param(7, False, None, id="matching-ordinary-pr"),
        pytest.param(7, True, "protected cleanup requires the repository-wide review slot", id="protected-pr"),
        pytest.param(8, False, "source checkout belongs to a different slot", id="other-pr"),
    ],
)
def test_cleanup_binds_ordinary_claim_to_pr_and_execution_scope(
    review, capsys: pytest.CaptureFixture[str], number: int, protected: bool, reason: str | None
) -> None:
    """Allow only the matching PR claim for ordinary cleanup and require exclusive ownership for integrity checks."""
    helper, command, run, root, worktree = review
    checkout_path = run / "local-checkout.json"
    record = json.loads(checkout_path.read_bytes())
    record["pr_url"] = "https://github.com/acme/widgets/pull/7"
    checkout_path.write_text(json.dumps(record), encoding="utf-8")
    command[command.index("--checkout-sha256") + 1] = hashlib.sha256(checkout_path.read_bytes()).hexdigest()
    locator = command.index("--slot") + 1
    scheduler = helper.review_checkout_slot
    assert scheduler._operate("release", Path(command[locator]), "run-owner", False)[0] == "released"
    slot = scheduler._target_slot(root / ".git", f"https://github.com/acme/widgets/pull/{number}")
    assert scheduler._schedule("acquire", slot, "run-owner", False, reuse_owner=True)[0] == "acquired"
    command[locator] = str(slot)
    if protected:
        (run / "integrity.json").write_text("{}", encoding="utf-8")
        command[-1:] = ["--integrity-sha256", "a" * 64]
    assert helper.main(command) == int(reason is not None)
    assert worktree.exists() is (reason is not None)
    assert json.loads(capsys.readouterr().out).get("reason") == reason


@pytest.mark.integration
def test_cleanup_preserves_worktree_for_stale_finalizer_digest(review, capsys: pytest.CaptureFixture[str]) -> None:
    """An accidentally stale result digest must be rejected before the witness or removal is published."""
    helper, command, run, root, worktree = review
    previous = hashlib.sha256(b"previous review output").hexdigest()
    assert helper.main([*command, "--review-result-sha256", previous]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "review result differs from producer digest"
    assert worktree.is_dir()
    assert not (run / helper.RECEIPT).exists()
    assert git_text(root, "for-each-ref", helper.review_worktree_replay.witness_ref(run)) == ""


@pytest.mark.integration
def test_historical_witness_resumes_without_authenticating_producer_result(review) -> None:
    """A schema-one witness interrupted before journaling can finish cleanup while preserving its original scope."""
    helper, command, run, root, worktree = review
    checkout_path = run / "local-checkout.json"
    result_path = run / "result.json"
    digest = hashlib.sha256(result_path.read_bytes()).hexdigest()
    legacy = {
        "schema_version": 1,
        "status": "removal-authorized",
        "run_dir": run.as_posix(),
        "worktree": worktree.as_posix(),
        "source_worktree": root.as_posix(),
        "head": json.loads(checkout_path.read_bytes())["expected_head"],
        "result_sha256": digest,
        "local_checkout_sha256": hashlib.sha256(checkout_path.read_bytes()).hexdigest(),
        "source_inputs": {},
    }
    replay = helper.review_worktree_replay
    replay.seal_source_witness(run, root, legacy)
    assert helper.main(command) == 0
    assert json.loads((run / helper.RECEIPT).read_bytes()) == {**legacy, "status": "removed"}
    assert replay.verified_archive(run, result_path) is not None
    assert replay.verified_archive(run, result_path, producer_result_sha256=digest) is None


@pytest.mark.integration
def test_cleanup_requires_execution_record_for_unknown_origin(review, capsys: pytest.CaptureFixture[str]) -> None:
    """A legacy invocation on an unknown origin must supply its execution decision before removal."""
    helper, command, run, _, worktree = review
    assert helper.main(command[:-1]) == 1
    assert "contributor integrity digest or confirmation" in json.loads(capsys.readouterr().out)["reason"]
    assert worktree.exists()
    assert not (run / "review-worktree-cleanup.json").exists()


@pytest.mark.integration
def test_ordinary_cleanup_preserves_worktree_without_producer_or_with_existing_interval(
    review, capsys: pytest.CaptureFixture[str]
) -> None:
    """An ordinary execution decision needs promotion proof and cannot close an existing protected interval."""
    helper, command, run, _, worktree = review
    ordinary = [*command[:-1], "--ordinary-execution"]
    assert helper.main(ordinary) == 1
    assert "promoting finalizer's result digest" in json.loads(capsys.readouterr().out)["reason"]

    (run / "integrity.json").write_text("{}", encoding="utf-8")
    digest = hashlib.sha256((run / "result.json").read_bytes()).hexdigest()
    assert helper.main([*ordinary, "--review-result-sha256", digest]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "original integrity digest required"
    assert worktree.is_dir()
    assert not (run / helper.RECEIPT).exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    "condition",
    [
        pytest.param("ignored", id="ignored-user-content"),
        pytest.param("loose", id="untracked-user-content"),
        pytest.param("file", id="tracked-edit"),
        pytest.param("empty", id="untracked-empty-directory"),
        pytest.param("hidden-index", id="index-masks-change"),
        pytest.param("hold", id="inspection-stop"),
        pytest.param("replaced", id="same-path-and-head-replacement"),
        pytest.param("attached", id="attached-branch"),
        pytest.param("mismatched-receipt", id="unbound-receipt"),
        pytest.param("pending", id="unfinished-candidate"),
        pytest.param("invalid-completion", id="real-validator-rejects-incomplete-run"),
        pytest.param("late-replacement", id="checkout-replaced-during-validation"),
        pytest.param("admin-file", id="worktree-admin-has-local-data"),
        pytest.param("different-run", id="later-slot-holder-cannot-remove-older-checkout"),
        pytest.param("no-anchor", id="lost-integrity-anchor"),
        pytest.param("wrong-anchor", id="invalid-integrity-anchor"),
        pytest.param(
            "mode",
            id="git-ignores-file-mode",
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits"),
        ),
    ],
)
def test_preserves_worktree_on_unverified_or_changed_state(
    review, condition: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every uncertain disposition preserves the checkout and excludes a removed receipt."""
    helper, command, run, root, worktree = review
    if condition in {"ignored", "loose", "file"}:
        (worktree / condition).write_text("preserve", encoding="utf-8")
    elif condition == "empty":
        (worktree / "empty").mkdir()
    elif condition == "hidden-index":
        git_text(worktree, "update-index", "--assume-unchanged", "file")
        (worktree / "file").write_text("preserve", encoding="utf-8")
    elif condition == "hold":
        helper.review_checkout_slot._operate("hold", root / ".git/codex-review-slot", "run-owner", False)
        monkeypatch.setenv("PATH", "")
    elif condition == "replaced":
        git_text(root, "worktree", "remove", str(worktree))
        git_text(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    elif condition == "attached":
        git_text(worktree, "switch", "-c", "new-owner")
    elif condition == "mismatched-receipt":
        (run / "local-checkout.json").write_bytes((run / "local-checkout.json").read_bytes() + b"\n")
    elif condition == "pending":
        (run / "result.candidate.json").write_text("{}", encoding="utf-8")
    elif condition == "late-replacement":

        def replace_checkout(*_args) -> None:
            git_text(root, "worktree", "remove", str(worktree))
            git_text(root, "worktree", "add", "--detach", str(worktree), "HEAD")

        monkeypatch.setattr(helper, "_verify_completion", replace_checkout)
    elif condition == "admin-file":
        admin = Path(git_text(worktree, "rev-parse", "--absolute-git-dir"))
        (admin / "user-notes").write_text("preserve", encoding="utf-8")
    elif condition == "different-run":
        slot = root / ".git/codex-review-slot"
        helper.review_checkout_slot._operate("release", slot, "run-owner", False)
        helper.review_checkout_slot._operate("acquire", slot, "new-owner", False)
        command[command.index("--owner") + 1] = "new-owner"
    elif condition in {"no-anchor", "wrong-anchor"}:
        (run / "integrity.json").write_text(
            json.dumps({"repo": str(worktree), "main_checkout": str(root), "watch_surface": True}), encoding="utf-8"
        )
        if condition == "wrong-anchor":
            command[-1:] = ["--integrity-sha256", "0" * 64]
    elif condition == "mode":
        git_text(root, "config", "core.fileMode", "false")
        (worktree / "file").chmod(0o755)
        assert git_text(worktree, "status", "--porcelain") == ""
    else:
        real = load_module("cleanup_real_test", SHARED / "review_worktree_cleanup.py")
        monkeypatch.setattr(helper, "_verify_completion", real._verify_completion)
    assert helper.main(command) != 0
    assert (worktree / ".git").exists()
    assert not (run / "review-worktree-cleanup.json").exists()
    if condition in {"ignored", "loose", "file"}:
        assert (worktree / condition).read_text(encoding="utf-8") == "preserve"


@pytest.mark.integration
def test_interrupted_final_write_retains_pre_removal_receipt(review, monkeypatch: pytest.MonkeyPatch) -> None:
    """If final bookkeeping fails after native removal, the verified source hashes survive for historical intake."""
    helper, command, run, root, worktree = review
    publish = helper._write_receipt

    def interrupt(folder: Path, data: dict) -> None:
        if data["status"] == "removed":
            raise OSError("interrupted final bookkeeping")
        publish(folder, data)

    monkeypatch.setattr(helper, "_write_receipt", interrupt)
    assert helper.main(command) != 0
    assert not worktree.exists()
    assert str(worktree) not in git_text(root, "worktree", "list", "--porcelain")
    receipt = json.loads((run / "review-worktree-cleanup.json").read_bytes())
    assert receipt["status"] == "removal-authorized"
    assert receipt["local_checkout_sha256"] == hashlib.sha256((run / "local-checkout.json").read_bytes()).hexdigest()


def test_existing_witness_with_missing_object_is_preserved_for_inspection(review) -> None:
    """A retry must detect missing proof bytes instead of recreating them from the writable report."""
    helper, command, run, root, _worktree = review
    assert helper.main(command) == 0
    receipt = json.loads((run / "review-worktree-cleanup.json").read_bytes())
    replay = helper.review_worktree_replay
    oid = git_text(root, "rev-parse", replay.witness_ref(run))
    blob = root / ".git" / "objects" / oid[:2] / oid[2:]
    blob.rename(blob.with_suffix(".held"))
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        replay.seal_source_witness(run, root, receipt)
    assert not blob.exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    "change",
    [
        pytest.param("none", id="only-authenticated-witness"),
        pytest.param("private-ref", id="unrelated-archive-ref"),
        pytest.param("tag", id="unexpected-tag"),
        pytest.param("config", id="shared-config-change"),
        pytest.param("changed-witness", id="wrong-witness-value"),
        pytest.param("wrong-token", id="different-parent-receipt"),
        pytest.param("report", id="changed-review-evidence"),
    ],
)
def test_retry_after_witness_publication_uses_original_integrity_anchor(
    review,
    change: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only the interrupted run's exact independently retained ref transition may differ from the original snapshot."""
    helper, command, run, root, worktree = review
    snapshot = run / "integrity.json"
    checker = [sys.executable, str(SHARED / "contributor_run_integrity.py")]
    captured = subprocess.run(
        [
            *checker,
            "snapshot",
            "--repo",
            str(worktree),
            "--main-checkout",
            str(root),
            "--watch-surface",
            "--out",
            str(snapshot),
        ],
        capture_output=True,
        check=True,
    )
    anchor = json.loads(captured.stdout)["sha256"]
    command[-1:] = ["--integrity-sha256", anchor]

    def interrupt(*_args) -> None:
        raise OSError("interrupted before journaling")

    with monkeypatch.context() as patch:
        patch.setattr(helper, "_write_receipt", interrupt)
        assert helper.main(command) != 0
    output = capsys.readouterr()
    assert "interrupted before journaling" in output.out
    ref = helper.review_worktree_replay.witness_ref(run)
    oid = git_text(root, "rev-parse", ref)
    strict = subprocess.run(
        [*checker, "verify", "--snapshot", str(snapshot), "--expect", anchor], capture_output=True, check=False
    )
    assert strict.returncode == 3
    assert json.loads(strict.stdout)["changed"] == ["common:refs"]
    assert helper.main(command) != 0
    assert worktree.is_dir()
    assert not (run / "review-worktree-cleanup.json").exists()
    assert json.loads(output.err)["retry_witness"] == oid
    if change == "private-ref":
        git_text(root, "update-ref", "refs/codex-rig/review-archives/other", oid)
    elif change == "tag":
        git_text(root, "tag", "other")
    elif change == "config":
        git_text(root, "config", "fixture.flag", "changed")
    elif change == "changed-witness":
        git_text(root, "update-ref", ref, "HEAD")
    elif change == "wrong-token":
        oid = "0" * len(oid)
    elif change == "report":
        (run / "result.json").write_bytes((run / "result.json").read_bytes() + b"\n")
    code = helper.main([*command, "--retry-witness", oid])
    assert code == (0 if change == "none" else 1)
    assert worktree.is_dir() == (change != "none")
    assert (run / "review-worktree-cleanup.json").exists() == (change == "none")
    if change != "none":
        reason = json.loads(capsys.readouterr().out.splitlines()[-1])["reason"]
        expected = {"config": "common:config", "report": "witness"}.get(change, "common:refs")
        assert expected in reason


def _inspected_cleanup_args(run: Path, root: Path, worktree: Path, ignored_file: Path) -> list[str]:
    """Create the two independently retained anchors around a real ignored-file observation."""
    checker = [sys.executable, str(SHARED / "contributor_run_integrity.py")]
    original = run / "integrity-retry.json"
    active = run / "integrity-restart.json"
    snapshot = subprocess.run(
        [
            *checker,
            "snapshot",
            "--repo",
            str(worktree),
            "--main-checkout",
            str(root),
            "--watch-surface",
            "--out",
            str(original),
        ],
        check=True,
        capture_output=True,
    )
    anchor = json.loads(snapshot.stdout)["sha256"]
    ignored_file.write_bytes(b"preserve this output")
    identity = ["--snapshot", str(original), "--expect", anchor]
    failed = subprocess.run([*checker, "verify", *identity], check=False, capture_output=True)
    assert failed.returncode == 3
    observed = json.loads(failed.stdout)["observed_sha256"]
    started = subprocess.run(
        [*checker, "restart-inspected", *identity, "--inspected-sha256", observed, "--out", str(active)],
        check=True,
        capture_output=True,
    )
    return [
        "--original-snapshot",
        str(original),
        "--integrity-sha256",
        anchor,
        "--inspected-snapshot",
        str(active),
        "--inspected-sha256",
        json.loads(started.stdout)["sha256"],
    ]


@pytest.mark.integration
def test_inspected_snapshot_supports_interrupted_cleanup_witness(
    review, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After inspection a witness-only transition can finish cleanup while preserving both interval records."""
    helper, command, run, root, worktree = review
    ignored = root / "ignored"
    command[-1:] = _inspected_cleanup_args(run, root, worktree, ignored)
    original = (run / "integrity-retry.json").read_bytes()
    active = (run / "integrity-restart.json").read_bytes()

    def interrupt(*_args) -> None:
        raise OSError("interrupted before journaling")

    with monkeypatch.context() as patch:
        patch.setattr(helper, "_write_receipt", interrupt)
        assert helper.main(command) == 1
    output = capsys.readouterr()
    assert "interrupted before journaling" in output.out
    oid = json.loads(output.err)["retry_witness"]
    assert helper.main([*command, "--retry-witness", oid]) == 0
    assert not worktree.exists()
    assert ignored.read_bytes() == b"preserve this output"
    assert (run / "integrity-retry.json").read_bytes() == original
    assert (run / "integrity-restart.json").read_bytes() == active


@pytest.mark.integration
@pytest.mark.parametrize("filename", ["integrity-retry.json", "integrity-restart.json"])
def test_inspected_cleanup_rejects_altered_anchor(review, filename: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Mutation of either retained interval record preserves the checkout before any cleanup witness is published."""
    helper, command, run, root, worktree = review
    command[-1:] = _inspected_cleanup_args(run, root, worktree, root / "ignored")
    record = run / filename
    altered = json.loads(record.read_bytes()) | {"marks": {}}
    record.write_text(json.dumps(altered), encoding="utf-8")
    assert helper.main(command) == 1
    assert "digest" in json.loads(capsys.readouterr().out)["reason"]
    assert worktree.exists()
    assert not (run / "review-worktree-cleanup.json").exists()


@pytest.mark.integration
def test_no_execution_cleanup_refuses_retained_alternate_interval(review, capsys: pytest.CaptureFixture[str]) -> None:
    """A missing default snapshot cannot turn an inspected run into a no-execution cleanup."""
    helper, command, run, root, worktree = review
    _inspected_cleanup_args(run, root, worktree, root / "ignored")
    assert not (run / "integrity.json").exists()
    assert helper.main(command) == 1
    assert "original integrity digest required" in json.loads(capsys.readouterr().out)["reason"]
    assert worktree.exists()


@pytest.mark.integration
def test_inspected_ignored_worktree_output_is_retained(review, capsys: pytest.CaptureFixture[str]) -> None:
    """Confirmation of a measured ignored output never authorizes deleting it during terminal cleanup."""
    helper, command, run, root, worktree = review
    ignored = worktree / "ignored"
    command[-1:] = _inspected_cleanup_args(run, root, worktree, ignored)
    assert helper.main(command) == 1
    assert "worktree contains changed, untracked or ignored data" in json.loads(capsys.readouterr().out)["reason"]
    assert ignored.read_bytes() == b"preserve this output"
    assert not (run / "review-worktree-cleanup.json").exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    "interval",
    [
        pytest.param(x, id=x)
        for x in (
            "original",
            "inspected",
            "no-contributor-execution",
            "same-repository",
            "ordinary-fork",
            "negative-verdict",
            "failed-hosted-ci",
            "private",
        )
    ],
)
def test_terminal_cli_removes_collected_worktree_and_keeps_report_intake(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, interval: str
) -> None:
    """Exercise collection, completion, cleanup, replay and remediation intake on one real detached checkout."""
    verify_integrity = interval in {"original", "inspected", "private"}
    validation = Path(__file__).resolve().parents[1] / "validation"
    completion = load_module("cleanup_completion_fixture", validation / "test_review_completion_gate.py")
    routing = load_module("cleanup_routing_fixture", validation / "test_code_review_routing_contract.py")
    project = tmp_path / "source"
    project.mkdir()
    git_text(project, "init", "-q")
    caller = project
    if interval == "private":
        # Keep a real peer claim alive throughout prepare, source collection, verification and cleanup.
        git_text(caller, "remote", "add", "origin", "https://github.com/acme/widgets.git")
        git_text(
            caller,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "caller",
        )
        # A 64-character workspace name under this test's own directory passes the 260-character Windows path limit
        # inside Git's object paths; a shared short parent keeps the retained workspace below it.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("s")))
        peer = [sys.executable, str(SHARED / "review_checkout_slot.py")]
        claim = subprocess.run(
            [*peer, "acquire", "--checkout", str(caller), "--owner", "peer"], capture_output=True, check=True
        )
        peer_slot = json.loads(claim.stdout)["slot"]
        subprocess.run([*peer, "hold", "--slot", peer_slot, "--owner", "peer"], check=True, capture_output=True)
        project = historical_review_workspace(caller, "https://github.com/acme/widgets/pull/123")
    for word in ("Wdiget", "Widget"):
        (project / "widget.txt").write_text(word + "\n", encoding="utf-8")
        git_text(project, "add", "widget.txt")
        git_text(project, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", word)
    slot = subprocess.run(
        [
            sys.executable,
            str(SHARED / "review_checkout_slot.py"),
            "acquire",
            "--checkout",
            str(project),
            "--owner",
            "terminal-fixture",
            *([] if verify_integrity else ["--target", "https://github.com/acme/widgets/pull/123"]),
        ],
        capture_output=True,
        check=True,
    )
    # Visible reports inside the invoking checkout exercise the collector's separate private fallback parent.
    private_temp = tmp_path / "collector-temp"
    private_temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private_temp))
    run = completion._write_assessed_pr(tmp_path if verify_integrity else project, project)
    monkeypatch.chdir(project)
    collector = load_module("cleanup_offline_collector", SHARED / "collect_pr.py")
    metadata = json.loads((run / "pr-routing.json").read_bytes())
    metadata.update(same_repo=interval == "same-repository", is_cross_repository=interval != "same-repository")
    # Reconstruct retained source evidence for an already collected historical run.
    # Fresh CLI collection rejects these repositories before fetching or checking out source.
    checkout = collector._review_worktree_checkout(
        collector.DEFAULT_RUN_COMMAND,
        30,
        run,
        metadata,
        base_oid=metadata["base_oid"],
        head_oid=metadata["head_oid"],
        prior_checkout={},
        source_context=(project, project / ".git") if interval == "private" else None,
    )
    worktree = Path(checkout["worktree"])
    if interval == "private":
        checkout["integrity_main_checkout"] = caller.as_posix()
        (run / "local-checkout.json").write_text(json.dumps(checkout), encoding="utf-8")
    assert worktree != project
    assert worktree.is_dir()
    assert checkout["worktree_location"] == ("report-adjacent" if verify_integrity else "temporary-fallback")
    if verify_integrity:
        # Yield before binding integrity. A peer can complete while the original invocation keeps its creation proof.
        scheduler = [sys.executable, str(SHARED / "review_checkout_slot.py")]
        locator = json.loads(slot.stdout)["slot"]
        for action, location, owner in (
            ("release", ["--slot", locator], "terminal-fixture"),
            ("acquire", ["--checkout", str(project)], "other-review"),
            ("release", ["--slot", locator], "other-review"),
            ("acquire", ["--checkout", str(project)], "terminal-fixture"),
        ):
            observed = subprocess.run(
                [*scheduler, action, *location, "--owner", owner], capture_output=True, check=True
            )
            assert json.loads(observed.stdout)["slot"] == locator
    gates = json.loads((run / "gates.json").read_bytes())
    routing._bind_gates_to_worktree(gates, checkout["worktree"], checkout["local_head"])
    (run / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    review_check = next(check for check in gates["checks"] if check["id"] == "review")
    (run / review_check["command_path"]).write_text(
        f"git diff --check {metadata['base_oid']}...{metadata['head_oid']}\n", encoding="utf-8"
    )
    execution = [] if interval == "same-repository" else ["--no-contributor-execution"]
    if interval == "ordinary-fork":
        monkeypatch.syspath_prepend(str(validation))
        producer = load_module("cleanup_safety_fixture", validation / "test_review_code_safety.py")
        result_digest = producer._promote(run, "unassessed", codex_home=tmp_path)
        execution = ["--ordinary-execution", "--review-result-sha256", result_digest]
    if interval == "inspected":
        with (project / ".git" / "info" / "exclude").open("a", encoding="utf-8") as stream:
            stream.write("\ninspection-output.txt\n")
        execution = _inspected_cleanup_args(run, project, worktree, project / "inspection-output.txt")
    elif verify_integrity:
        snapshot = subprocess.run(
            [
                sys.executable,
                str(SHARED / "contributor_run_integrity.py"),
                "snapshot",
                "--repo",
                str(worktree),
                "--main-checkout",
                str(caller),
                "--watch-surface",
                "--out",
                str(run / "integrity.json"),
            ],
            capture_output=True,
            check=True,
        )
        execution = ["--integrity-sha256", json.loads(snapshot.stdout)["sha256"]]
    if interval == "private":
        prepare = load_module("split_review_prepare", SHARED.parent / "skills/code-review/review_prepare.py")
        prepare._require_pr_dispatch_integrity(run, [str(run / "integrity.json"), execution[1]], None)
        wrong = run / "wrong-scope.json"
        digest = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    str(SHARED / "contributor_run_integrity.py"),
                    "snapshot",
                    "--repo",
                    str(worktree),
                    "--main-checkout",
                    str(project),
                    "--watch-surface",
                    "--out",
                    str(wrong),
                ]
            )
        )["sha256"]
        with pytest.raises(ValueError, match="integrity-surface-mismatch"):
            prepare._require_pr_dispatch_integrity(run, [str(wrong), digest], None)
    if interval in {"negative-verdict", "failed-hosted-ci"}:
        if interval == "negative-verdict":
            result, handoff = completion._fail_tests_gate(run)
            failed_gates = json.loads((run / "gates.json").read_bytes())
            routing._bind_gates_to_worktree(failed_gates, checkout["worktree"], checkout["local_head"])
            completion._save_json(run / "gates.json", failed_gates)
        else:
            pr = json.loads((run / "pr.json").read_bytes())
            pr["statusCheckRollup"][0]["conclusion"] = "FAILURE"
            completion._save_json(run / "pr.json", pr)
            tests = next(check for check in gates["checks"] if check["id"] == "tests")
            tests["reason"] = f"ci-failed: tests at {pr['headRefOid'][:12]}"
            (run / tests["command_path"]).write_text(f"not-applicable: {tests['reason']}\n", encoding="utf-8")
            completion._save_json(run / "gates.json", gates)
            result = json.loads((run / "result.json").read_bytes())
            handoff = json.loads((run / "final-handoff.json").read_bytes())
            result["status"] = "fail"
            result["metadata"]["review_validation"]["verification_readiness"] = "blocked"
            snapshot = handoff["tables"][0]
            snapshot["verification_readiness"] = "blocked"
            next(row for row in snapshot["rows"] if row["cells"][0] == "CI")["cells"][1] = "failing — tests"
            notes = run / "review-notes.md"
            notes.write_text(
                notes.read_text(encoding="utf-8")
                .replace("CI tests passed", "CI tests failed")
                .replace('"passed": 7', '"failure": "pytest returned 1"')
                .replace("Verification readiness: complete", "Verification readiness: blocked"),
                encoding="utf-8",
            )
        completion._add_test_failure_merge_block(run, result, handoff)
        completion._rerender(run, handoff, result)
        completion._save_json(run / "result.json", result)
    finder = [sys.executable, str(SHARED / "find-review-report.py")]
    complete = [*finder, "--complete-run", str(run), "--codex-home", str(tmp_path)]
    original = subprocess.run(complete, capture_output=True, check=False)
    assert original.returncode == 0, original.stderr.decode()
    receipt_sha = hashlib.sha256((run / "local-checkout.json").read_bytes()).hexdigest()
    cleanup_command = [
        sys.executable,
        str(SHARED / "review_worktree_cleanup.py"),
        "--run",
        str(run),
        "--worktree",
        str(worktree),
        "--checkout-sha256",
        receipt_sha,
        "--slot",
        json.loads(slot.stdout)["slot"],
        "--owner",
        "terminal-fixture",
        *execution,
        "--codex-home",
        str(tmp_path),
    ]
    if verify_integrity:
        cleanup = subprocess.run(cleanup_command, capture_output=True, check=False)
        assert cleanup.returncode == 0, (cleanup.stdout.decode(), cleanup.stderr.decode())
        assert json.loads(cleanup.stdout)["status"] == "removed"
    else:
        helper = load_module("interrupt_terminal_cleanup", SHARED / "review_worktree_cleanup.py")
        write = helper._write_receipt

        def interrupt_after_remove(directory: Path, data: dict) -> None:
            if data["status"] == "removed":
                raise OSError("interrupted after native Git removal")
            write(directory, data)

        with monkeypatch.context() as patch:
            patch.setattr(helper, "_write_receipt", interrupt_after_remove)
            assert helper.main(cleanup_command[2:]) == 1
    assert not worktree.exists()
    if verify_integrity:
        # Reachability keeps the small validation witness through normal Git maintenance.
        git_text(project, "gc", "--quiet")
        replay_helper = load_module("cleanup_witness_fixture", SHARED / "review_worktree_replay.py")
        assert git_text(project, "cat-file", "-t", replay_helper.witness_ref(run)) == "blob"
    else:
        assert worktree.parent.is_dir()
        assert json.loads((run / "review-worktree-cleanup.json").read_bytes())["status"] == "removal-authorized"
    journal_path = run / "review-worktree-cleanup.json"
    completed_journal = journal_path.read_bytes()
    prior_stat = journal_path.stat()
    if verify_integrity:
        wrong_anchor = list(cleanup_command)
        wrong_anchor[wrong_anchor.index("--integrity-sha256") + 1] = "0" * 64
        rejected = subprocess.run(wrong_anchor, capture_output=True, check=False)
        assert rejected.returncode == 1
        assert "integrity digest does not match" in json.loads(rejected.stdout)["reason"]
        rejected = subprocess.run([*cleanup_command, "--retry-witness", "0" * 40], capture_output=True, check=False)
        assert rejected.returncode == 1
        assert "witness does not match" in json.loads(rejected.stdout)["reason"]
    else:
        original_admin = Path(checkout["created_worktree"]["git_dir"])
        original_admin.mkdir(parents=True)
        (original_admin / "user-notes").write_bytes(b"preserve")
        rejected = subprocess.run(cleanup_command, capture_output=True, check=False)
        assert rejected.returncode == 1
        assert "original worktree registration is not absent" in json.loads(rejected.stdout)["reason"]
        assert (original_admin / "user-notes").read_bytes() == b"preserve"
        original_admin.rename(tmp_path / "saved-admin")
        # A different registration can claim the old path while its directory is missing.
        other = tmp_path / "other"
        git_text(project, "worktree", "add", "--detach", str(other), "HEAD")
        git_text(project, "worktree", "move", str(other), str(worktree))
        worktree.rename(other)
        rejected = subprocess.run(cleanup_command, capture_output=True, check=False)
        assert rejected.returncode == 1
        assert "worktree-still-registered" in json.loads(rejected.stdout)["detail"]
        assert journal_path.read_bytes() == completed_journal
        other.rename(worktree)
        git_text(project, "worktree", "remove", str(worktree))
        final = run / "final.md"
        bound_text = final.read_bytes()
        final.write_bytes(bound_text + b"changed\n")
        rejected = subprocess.run(cleanup_command, capture_output=True, check=False)
        assert rejected.returncode == 1
        assert "final" in json.loads(rejected.stdout)["detail"]
        assert journal_path.read_bytes() == completed_journal
        final.write_bytes(bound_text)
    retry = subprocess.run(cleanup_command, capture_output=True, check=False)
    assert retry.returncode == 0, (retry.stdout, retry.stderr)
    assert json.loads(retry.stdout)["status"] == "removed"
    assert json.loads(journal_path.read_bytes())["status"] == "removed"
    if verify_integrity:
        assert journal_path.read_bytes() == completed_journal
        assert journal_path.stat().st_ino == prior_stat.st_ino
        assert journal_path.stat().st_mtime_ns == prior_stat.st_mtime_ns
    else:
        assert worktree.parent.is_dir()
    replay = subprocess.run(complete, capture_output=True, check=False)
    assert replay.returncode == 0, replay.stderr.decode()
    assert replay.stdout == original.stdout == (run / "final.md").read_bytes()
    intake = subprocess.run(
        [*finder, "--result", str(run / "result.json"), "--codex-home", str(tmp_path)], capture_output=True, check=False
    )
    assert intake.returncode == 0, intake.stderr.decode()
    assert Path(intake.stdout.decode().strip()) == run / "result.json"
    if interval == "private":
        peer_status = json.loads(subprocess.check_output([*peer, "status", "--slot", peer_slot]))
        assert (peer_status["status"], peer_status["owner"]) == ("inspection-required", "peer")
    if verify_integrity:
        # Replacing report files and their sidecar digest together cannot change the independently pinned evidence.
        inventory = run / "files.txt"
        inventory.write_bytes(inventory.read_bytes() + b"\n")
        journal_path = run / "review-worktree-cleanup.json"
        journal = json.loads(journal_path.read_bytes())
        journal["source_inputs"]["files.txt"] = hashlib.sha256(inventory.read_bytes()).hexdigest()
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        altered = subprocess.run(complete, capture_output=True, check=False)
        assert altered.returncode != 0
        assert b"witness" in altered.stderr
        rejected = subprocess.run(cleanup_command, capture_output=True, check=False)
        assert rejected.returncode == 1
        assert "witness" in json.loads(rejected.stdout)["detail"]


@pytest.mark.integration
def test_two_prs_collect_linked_worktrees_before_either_releases_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two reviews with independent PR claims collect exact commits in the same repository at the same time."""
    caller = tmp_path / "caller"
    caller.mkdir()
    git_text(caller, "init", "-q")
    git_text(caller, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    commits = []
    for content in ("base", "first change", "second change"):
        (caller / "change.txt").write_text(content, encoding="utf-8")
        git_text(caller, "add", "change.txt")
        git_text(caller, "-c", "user.name=fixture", "-c", "user.email=test@example.invalid", "commit", "-qm", content)
        commits.append(git_text(caller, "rev-parse", "HEAD").strip())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    collector = load_module("concurrent_source_collector", SHARED / "collect_pr.py")
    scheduler = [sys.executable, str(SHARED / "review_checkout_slot.py")]
    (caller / ".git" / "info" / "exclude").write_text("/.reports/\n", encoding="utf-8")
    together = Barrier(2, timeout=30)

    def collect(number: int) -> tuple[Path, str]:
        url = f"https://github.com/acme/widgets/pull/{number}"
        workspace = Path(
            subprocess.check_output(
                [sys.executable, str(SHARED / "review_workspace.py"), "--checkout", str(caller), "--target", url],
                text=True,
            ).strip()
        )
        owner = f"review-{number}"
        claim = json.loads(
            subprocess.check_output(
                [*scheduler, "acquire", "--checkout", str(workspace), "--target", url, "--owner", owner]
            )
        )
        assert workspace == caller
        run = workspace / ".reports" / "codex" / "code-review" / f"pr-{number}" / "run-001"
        run.mkdir(parents=True)
        checkout = collector._review_worktree_checkout(
            partial(subprocess.run, cwd=workspace),
            30,
            run,
            {"pr_url": url, "pr_number": number},
            base_oid=commits[0],
            head_oid=commits[number],
            prior_checkout={},
        )
        assert checkout["source_worktree"] == caller.as_posix()
        assert checkout["created_worktree"]["slot_owner"] == owner
        tree = Path(checkout["worktree"])
        common = ("rev-parse", "--path-format=absolute", "--git-common-dir")
        assert git_text(tree, *common) == git_text(caller, *common)
        assert (tree / "change.txt").read_text(encoding="utf-8") == ("first change" if number == 1 else "second change")
        together.wait()
        subprocess.run(
            [*scheduler, "release", "--slot", claim["slot"], "--owner", owner], capture_output=True, check=True
        )
        return tree, claim["slot"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(collect, (1, 2))
    assert first[0] != second[0]
    assert first[1] != second[1]
    assert git_text(caller, "status", "--porcelain") == ""
    assert git_text(caller, "worktree", "list", "--porcelain").count("worktree ") == 3
