"""A review parent's code-safety assessment applies only to its validated PR and unchanged execution inputs."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module, load_module_registered
from _platform import DIRECTORY_SYMLINKS_AVAILABLE
from test_code_review_routing_contract import _bind_gates_to_worktree
from test_review_completion_gate import (
    _current_closed_terminal,
    _current_unavailable_terminal,
    _write_assessed_pr,
    git_text,
)

SHARED = Path(__file__).resolve().parents[3] / "shared"
URL = "https://github.com/acme/widgets/pull/123"


def _invoke(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a trusted shipped helper as a separate process and retain its diagnostic."""
    return subprocess.run([sys.executable, "-B", str(SHARED / args[0]), *args[1:]], capture_output=True, text=True)


def _check(*args: str) -> dict:
    """Read the safety route which an actual remediation gate would consume."""
    result = _invoke("review_code_safety.py", "check", *args)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def review_inputs(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, Path, Path, str, str]:
    """Make a real PR fixture, with reports in the caller and separate, clean review and remediation checkouts."""
    caller = tmp_path / "caller"
    caller.mkdir()
    git_text(caller, "init", "-q")
    if getattr(request, "param", None) == "flagged":
        (caller / "checked.py").write_text('module = __import__("subprocess")\n', encoding="utf-8")
        git_text(caller, "add", "checked.py")
    for text in ("Wdiget", "Widget"):
        (caller / "widget.txt").write_text(text + "\n", encoding="utf-8")
        git_text(caller, "add", "widget.txt")
        git_text(caller, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", text)
    review, remediation = tmp_path / "review", tmp_path / "remediation"
    git_text(caller, "worktree", "add", "-q", "--detach", str(review))
    git_text(caller, "worktree", "add", "-q", str(remediation), "-b", "fix")
    in_place = getattr(request, "param", None) == "in-place"
    run = _write_assessed_pr(tmp_path / "review-evidence" if in_place else caller, caller)
    checkout = run / "local-checkout.json"
    checkout.write_text(json.dumps(dict(json.loads(checkout.read_bytes()), worktree=str(review))), encoding="utf-8")
    return (
        run,
        review,
        caller if in_place else remediation,
        git_text(caller, "rev-parse", "HEAD").strip(),
        git_text(caller, "rev-parse", "HEAD^").strip(),
    )


def _promote(run: Path, assessment: str, *, codex_home: Path | None = None) -> str:
    """Produce an assessed review through the published finalizer and retain its completion digest."""
    result = run / "result.json"
    original = json.loads(result.read_bytes())
    metadata = original["metadata"]
    metadata.pop("final_handoff")
    metadata["code_safety"] = {"assessment": "safe", "supplied_by": "untrusted draft"}
    result.unlink()
    draft = run / "draft-metadata.json"
    draft.write_text(json.dumps(metadata), encoding="utf-8")
    handoff = json.loads((run / "final-handoff.json").read_bytes())
    authored = run / "draft-handoff.json"
    authored.write_text(
        json.dumps({k: v for k, v in handoff.items() if k not in {"verification", "confidence"}}),
        encoding="utf-8",
    )
    proc = _invoke(
        "remediation_finalize.py",
        "finalize",
        "--skill",
        "code-review",
        "--parent-thread-id",
        "thread",
        "--run",
        str(run),
        "--metadata",
        str(draft),
        "--handoff",
        str(authored),
        "--status",
        original["status"],
        "--confidence",
        str(original["confidence"]),
        "--extra-checks-failed",
        ",".join(original.get("checks_failed", [])),
        "--artifact-path",
        str(result),
        "--promote",
        "--code-safety",
        assessment,
        *(["--codex-home", str(codex_home)] if codex_home else []),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    digest = json.loads(proc.stdout)["review_result_sha256"]
    assert digest == hashlib.sha256(result.read_bytes()).hexdigest()
    return digest


@pytest.mark.integration
def test_safe_review_requires_fresh_matching_execution_inputs(review_inputs: tuple) -> None:
    """Reuse a verified clean review, honor explicit confinement, and inspect changed or unauthenticated inputs."""
    run, _, remediation, head, base = review_inputs
    digest = _promote(run, "safe")
    assert "## Code Safety\n\nCode safety: safe\n" in (run / "review-notes.md").read_text(encoding="utf-8")
    record = json.loads((run / "result.json").read_bytes())["metadata"]["code_safety"]
    assert record["assessment"] == "safe"
    assert record["head"] == head
    assert record["base"] == base
    args = ["--review-run", str(run), "--checkout", str(remediation), "--pr-url", URL, "--head", head, "--base", base]
    assert _check(*args)["route"] == "inspect"
    args += ["--review-result-sha256", digest]
    assert _check(*args)["route"] == "ordinary"
    assert _check(*args, "--sandbox")["route"] == "sandbox"
    (remediation / "widget.txt").write_text("remediated widget\n", encoding="utf-8")
    assert _check(*args)["route"] == "inspect"
    git_text(remediation, "restore", "--", "widget.txt")
    assert _check(*args, "--pr-url", URL + "4")["route"] == "inspect"
    assert _check(*args, "--head", base)["route"] == "inspect"
    assert _check(*args, "--base", head)["route"] == "inspect"
    info_exclude = Path(
        git_text(remediation, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip()
    )
    with info_exclude.open("a", encoding="utf-8") as stream:
        stream.write("\nstartup.pth\n")
    ignored = remediation / "startup.pth"
    ignored.touch()
    assert _check(*args)["route"] == "inspect"
    ignored.unlink()
    git_text(remediation, "update-index", "--assume-unchanged", "widget.txt")
    (remediation / "widget.txt").write_bytes(b"unreviewed")
    assert _check(*args)["route"] == "inspect"


@pytest.mark.integration
@pytest.mark.parametrize("assessment", ["unsafe", "unassessed"])
def test_nonpositive_review_routes_current_inputs_to_inspection(review_inputs: tuple, assessment: str) -> None:
    """An unsafe or unassessed review requires inspection even when its untrusted draft supplied a safe judgment."""
    run, _, remediation, head, base = review_inputs
    digest = _promote(run, assessment)
    assert json.loads((run / "result.json").read_bytes())["metadata"]["code_safety"]["assessment"] == assessment
    assert (
        _check(
            "--review-run",
            str(run),
            "--checkout",
            str(remediation),
            "--pr-url",
            URL,
            "--head",
            head,
            "--base",
            base,
            "--review-result-sha256",
            digest,
        )["route"]
        == "inspect"
    )


@pytest.mark.integration
def test_nonreview_bytes_cannot_authorize_a_forged_safe_report(review_inputs: tuple) -> None:
    """A forged assessment after promotion cannot use the returned digest or the completion validators alone."""
    run, _, remediation, head, base = review_inputs
    anchor = _promote(run, "unsafe")
    report = run / "result.json"
    payload = json.loads(report.read_bytes())
    payload["metadata"]["code_safety"]["assessment"] = "safe"
    report.write_text(json.dumps(payload), encoding="utf-8")
    args = ["--review-run", str(run), "--checkout", str(remediation), "--pr-url", URL, "--head", head, "--base", base]
    assert _check(*args, "--review-result-sha256", anchor)["reason"] == "trusted-result-digest-mismatch"
    assert _check(*args)["route"] == "inspect"


def test_sandbox_takes_precedence_over_even_missing_evidence(tmp_path: Path) -> None:
    """An explicit sandbox request wins before reading a review, inspecting a checkout, or starting Git."""
    assert _check(
        "--review-run",
        str(tmp_path / "absent"),
        "--checkout",
        str(tmp_path / "missing"),
        "--pr-url",
        "unknown",
        "--head",
        "unknown",
        "--base",
        "unknown",
        "--sandbox",
    ) == {"route": "sandbox", "reason": "explicit-sandbox"}


@pytest.mark.integration
@pytest.mark.parametrize("review_inputs", ["flagged"], indirect=True)
def test_new_scanner_risk_overrides_historical_safe_assessment(review_inputs: tuple) -> None:
    """A historically clean classification for an identical SHA cannot override today's whole-tree scan finding."""
    run, _, remediation, head, base = review_inputs
    _promote(run, "unsafe")
    result = run / "result.json"
    payload = json.loads(result.read_bytes())
    record = payload["metadata"]["code_safety"]
    assert record["scan"]["risk_level"] == "borderline"
    # Model a completed historical producer whose scanner treated the same code as clean.
    record["assessment"] = "safe"
    record["scan"]["risk_level"] = "none"
    result.write_text(json.dumps(payload), encoding="utf-8")
    notes = run / "review-notes.md"
    notes.write_text(
        notes.read_text(encoding="utf-8").replace("Code safety: unsafe", "Code safety: safe"), encoding="utf-8"
    )
    anchor = hashlib.sha256(result.read_bytes()).hexdigest()
    assert _check(
        "--review-run",
        str(run),
        "--checkout",
        str(remediation),
        "--pr-url",
        URL,
        "--head",
        head,
        "--base",
        base,
        "--review-result-sha256",
        anchor,
    ) == {"route": "inspect", "reason": "fresh-scan-does-not-confirm-reviewed-inputs"}


@pytest.mark.integration
@pytest.mark.parametrize("review_inputs", ["flagged"], indirect=True)
def test_incomplete_safe_evidence_finishes_review_unassessed(review_inputs: tuple) -> None:
    """An incomplete safe claim keeps the review deliverable and sends the current command inputs to inspection."""
    run, _, remediation, head, base = review_inputs
    digest = _promote(run, "safe")
    record = json.loads((run / "result.json").read_bytes())["metadata"]["code_safety"]
    assert record["assessment"] == "unassessed"
    assert record["requested_assessment"] == "safe"
    assert record["scan"]["risk_level"] == "borderline"
    assert record["reason"] in (run / "review-notes.md").read_text(encoding="utf-8")
    assert (
        _check(
            "--review-run",
            str(run),
            "--checkout",
            str(remediation),
            "--pr-url",
            URL,
            "--head",
            head,
            "--base",
            base,
            "--review-result-sha256",
            digest,
        )["route"]
        == "inspect"
    )


@pytest.mark.integration
def test_admitted_producer_coordinates_reach_completion(
    review_inputs: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the admitted evidence home and reject a different thread through the real completion validator."""
    run, _, checkout, head, base = review_inputs
    codex_home = tmp_path / "trusted-producer-home"
    codex_home.mkdir()
    digest = _promote(run, "safe", codex_home=codex_home)
    safety = load_module("review_code_safety", SHARED / "review_code_safety.py")
    run_process = subprocess.run
    completions = []

    def capture(command: list[str], **kwargs) -> subprocess.CompletedProcess:
        """Record argv while leaving both production validators and Git inspection active."""
        if str(SHARED / "find-review-report.py") in command:
            completions.append(command)
        return run_process(command, **kwargs)

    monkeypatch.setattr(safety.subprocess, "run", capture)
    options = {"expect": digest, "sandbox": False, "codex_home": codex_home}
    assert safety.decision(run, URL, head, base, checkout, parent_thread_id="thread", **options)["route"] == "ordinary"
    assert completions[0][completions[0].index("--codex-home") + 1] == str(codex_home)
    assert safety.decision(run, URL, head, base, checkout, parent_thread_id="other-thread", **options) == {
        "route": "inspect",
        "reason": "review-completion-validation-failed",
    }


@pytest.mark.integration
@pytest.mark.parametrize("review_inputs", [pytest.param("in-place", id="original-pr-checkout")], indirect=True)
def test_in_place_remediation_uses_external_allocation_and_promoted_evidence(
    review_inputs: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep PR files and copied findings outside the execution checkout through real allocation and promotion."""
    run, _, checkout, head, base = review_inputs
    retained_home = tmp_path / "retained-home"
    retained_home.mkdir()
    digest = _promote(run, "safe", codex_home=retained_home)
    assert git_text(checkout, "branch", "--show-current").strip()
    git_text(checkout, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root_args = ("review_code_safety.py", "report-root", "--checkout", str(checkout), "--pr-url", URL)
    root_result = _invoke(*root_args)
    assert root_result.returncode == 0, root_result.stderr
    root = Path(root_result.stdout.strip())
    assert root.is_dir()
    assert root == checkout / ".git" / "codex-rig" / "reports"
    assert not (root / ".git").exists()
    assert not (checkout / ".git" / "codex-review-locations").exists()
    assert not (tmp_path / "state").exists()
    assert root_result.stdout == _invoke(*root_args).stdout
    creator = ("create_run.py", "--skill", "code-remediate", "--root", str(root))
    created = _invoke(*creator, "--pr-number", "123")
    assert created.returncode == 0, created.stderr
    selected = Path(created.stdout.strip())
    (selected / "pr").mkdir()
    for artifact in ["pr.json", "diff.patch", "pr-routing.json", "files.txt"]:
        (selected / "pr" / artifact).write_bytes((run / artifact).read_bytes())
    (selected / "findings-input.txt").write_bytes((run / "result.json").read_bytes())
    promoted = _invoke(*creator, "--promote-pr-run", str(selected))
    assert promoted.returncode == 0, promoted.stderr
    assert Path(promoted.stdout.strip()) == root / "code-remediate" / "pr-123" / "run-001"
    assert (Path(promoted.stdout.strip()) / "pr" / "diff.patch").read_bytes() == (run / "diff.patch").read_bytes()
    args = [
        "--review-run",
        str(run),
        "--checkout",
        str(checkout),
        "--pr-url",
        URL,
        "--head",
        head,
        "--base",
        base,
        "--review-result-sha256",
        digest,
        "--parent-thread-id",
        "thread",
        "--codex-home",
        str(retained_home),
    ]
    assert _check(*args)["route"] == "ordinary"
    assert _check(*args, "--sandbox")["route"] == "sandbox"
    old_root = checkout / ".reports" / "codex"
    legacy = _invoke("create_run.py", "--skill", "code-remediate", "--root", str(old_root), "--pr-number", "123")
    assert legacy.returncode == 0, legacy.stderr
    (Path(legacy.stdout.strip()) / "findings-input.txt").write_bytes((run / "result.json").read_bytes())
    assert _check(*args)["route"] == "inspect"
    monkeypatch.setenv("XDG_STATE_HOME", str(checkout / "in-tree-state"))
    assert _invoke(*root_args).stdout == root_result.stdout
    assert not (checkout / "in-tree-state").exists()


@pytest.mark.integration
def test_separate_git_dir_uses_caller_local_reports(tmp_path: Path) -> None:
    """A separate Git directory lacks a reverse registration and receives no report writes."""
    caller = tmp_path / "source"
    caller.mkdir()
    metadata = tmp_path / "metadata"
    git_text(caller, "init", "-q", "--separate-git-dir", str(metadata))
    git_text(caller, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    following = _invoke("review_code_safety.py", "report-root", "--checkout", str(caller), "--pr-url", URL)
    assert following.returncode == 0, following.stderr
    assert Path(following.stdout.strip()) == caller / ".reports" / "codex"
    assert not (metadata / "codex-rig").exists()


@pytest.mark.integration
@pytest.mark.parametrize("review_inputs", [pytest.param("in-place", id="completed-standalone-review")], indirect=True)
@pytest.mark.parametrize(
    ("linked_caller", "use_producer_digest", "expected_route"),
    [
        pytest.param(False, True, "ordinary", id="main-caller"),
        pytest.param(True, True, "ordinary", id="linked-caller"),
        pytest.param(False, False, "inspect", id="unattested-archive"),
    ],
)
def test_new_session_reuses_only_its_own_sealed_review(
    review_inputs: tuple,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked_caller: bool,
    use_producer_digest: bool,
    expected_route: str,
) -> None:
    """A later consumer may select ordinary execution only for cleanup bound to the producing finalizer."""
    fixture_run, _, caller, head, base = review_inputs
    if linked_caller:
        linked = tmp_path / "linked-caller"
        git_text(caller, "worktree", "add", "-q", "-b", "another", str(linked), head)
        caller = linked
    git_text(caller, "remote", "add", "origin", "https://github.com/acme/widgets.git")
    following = _invoke("review_code_safety.py", "report-root", "--checkout", str(caller), "--pr-url", URL)
    assert following.returncode == 0, following.stderr
    external_root = Path(following.stdout.strip())
    own = Path(git_text(caller, "rev-parse", "--absolute-git-dir").strip())
    assert external_root == own / "codex-rig" / "reports"
    slot = _invoke(
        "review_checkout_slot.py", "acquire", "--checkout", str(caller), "--target", URL, "--owner", "assessment"
    )
    assert slot.returncode == 0, slot.stderr
    monkeypatch.chdir(caller)
    create = ("create_run.py", "--skill", "code-review", "--root", str(external_root))
    created = _invoke(*create, "--pr-number", "123", "--pr-url", URL)
    assert created.returncode == 0, created.stderr
    stage = Path(created.stdout.strip()).resolve()
    (stage / "pr.json").write_bytes((fixture_run / "pr.json").read_bytes())
    allocated = _invoke(*create, "--promote-pr-run", str(stage))
    assert allocated.returncode == 0, allocated.stderr
    run = Path(allocated.stdout.strip()).resolve()
    _write_assessed_pr(tmp_path, caller, run=run)
    collector = load_module("caller_bound_archived_safety_collection", SHARED / "collect_pr.py")
    routing = json.loads((run / "pr-routing.json").read_bytes())
    checkout = collector._review_worktree_checkout(
        collector.DEFAULT_RUN_COMMAND, 30, run, routing, base_oid=base, head_oid=head, prior_checkout={}
    )
    assert checkout["source_worktree"] == caller.as_posix()
    common = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    assert git_text(Path(checkout["worktree"]), *common) == git_text(caller, *common)
    receipt_hash = hashlib.sha256((run / "local-checkout.json").read_bytes()).hexdigest()
    gates = json.loads((run / "gates.json").read_bytes())
    _bind_gates_to_worktree(gates, checkout["worktree"], head)
    (run / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    review_gate = next(check for check in gates["checks"] if check["id"] == "review")
    (run / review_gate["command_path"]).write_text(f"git diff --check {base}...{head}\n", encoding="utf-8")
    result_digest = _promote(run, "safe")
    args = ["--review-run", str(run), "--checkout", str(caller), "--pr-url", URL, "--head", head, "--base", base]
    assert _check(*args) == {"route": "inspect", "reason": "missing-authoritative-result-anchor"}
    retained_slot = json.loads(slot.stdout)["slot"]
    cleanup = _invoke(
        "review_worktree_cleanup.py",
        "--run",
        str(run),
        "--worktree",
        checkout["worktree"],
        "--checkout-sha256",
        receipt_hash,
        "--slot",
        retained_slot,
        "--owner",
        "assessment",
        "--no-contributor-execution",
        *(["--review-result-sha256", result_digest] if use_producer_digest else []),
    )
    assert cleanup.returncode == 0, cleanup.stderr + cleanup.stdout
    assert json.loads(cleanup.stdout)["status"] == "removed"
    assert not Path(checkout["worktree"]).exists()
    released = _invoke("review_checkout_slot.py", "release", "--slot", retained_slot, "--owner", "assessment")
    assert released.returncode == 0, released.stderr
    monkeypatch.chdir(caller)
    found = _invoke("find-review-report.py", "--target", URL)
    assert found.returncode == 0, found.stderr
    assert Path(found.stdout.strip()) == run / "result.json"
    admitted = _invoke("find-review-report.py", "--result", str(run / "result.json"))
    assert admitted.returncode == 0, admitted.stderr
    assert Path(admitted.stdout.strip()) == run / "result.json"
    assert _check(*args)["route"] == expected_route
    assert _check(*args, "--sandbox")["route"] == "sandbox"
    destination = tmp_path / "registered-delivery"
    git_text(caller, "worktree", "add", "-q", "-b", "topic", str(destination), head)
    assert git_text(destination, *common) == git_text(caller, *common)
    assert _check(*args, "--checkout", str(destination))["route"] == expected_route
    if DIRECTORY_SYMLINKS_AVAILABLE and use_producer_digest:
        alias = tmp_path / "delivery-parent-alias"
        alias.symlink_to(destination.parent, target_is_directory=True)
        redirected = str(alias / destination.name)
        for anchor in ([], ["--review-result-sha256", result_digest]):
            assert _check(*args, "--checkout", redirected, *anchor) == {
                "route": "inspect",
                "reason": "redirected-execution-checkout",
            }
    (destination / "unreviewed.txt").write_bytes(b"local input\n")
    assert _check(*args, "--checkout", str(destination))["route"] == "inspect"
    unrelated = tmp_path / "independent-clone"
    git_text(caller, "clone", "--quiet", str(caller), str(unrelated))
    assert git_text(unrelated, "rev-parse", "HEAD").strip() == head
    assert _check(*args, "--checkout", str(unrelated)) == {
        "route": "inspect",
        "reason": "missing-authoritative-result-anchor",
    }
    if use_producer_digest:
        replay = load_module("nested_cleanup_witness", SHARED / "review_worktree_replay.py")
        assert git_text(caller, "cat-file", "-t", replay.witness_ref(run)) == "blob"
        journal = run / "review-worktree-cleanup.json"
        retained = journal.read_bytes()
        checkout_file = run / "local-checkout.json"
        original = checkout_file.read_bytes()
        # Removing the journal and pointing to a live checkout must still consult the witness in the caller's Git.
        journal.rename(tmp_path / "saved-cleanup-journal")
        checkout_file.write_text(json.dumps(dict(checkout, worktree=str(caller))), encoding="utf-8")
        assert _check(*args) == {"route": "inspect", "reason": "review-cleanup-witness-mismatch"}
        checkout_file.write_text(json.dumps(dict(checkout, pr_url=URL + "4")), encoding="utf-8")
        changed = dict(
            json.loads(retained), local_checkout_sha256=hashlib.sha256(checkout_file.read_bytes()).hexdigest()
        )
        journal.write_text(json.dumps(changed), encoding="utf-8")
        assert _check(*args)["route"] == "inspect"
        checkout_file.write_text(json.dumps(dict(checkout, source_worktree=unrelated.as_posix())), encoding="utf-8")
        changed.update(
            source_worktree=unrelated.as_posix(),
            local_checkout_sha256=hashlib.sha256(checkout_file.read_bytes()).hexdigest(),
        )
        journal.write_text(json.dumps(changed), encoding="utf-8")
        assert _check(*args) == {"route": "inspect", "reason": "review-cleanup-witness-repository-mismatch"}
        journal.write_bytes(retained)
        checkout_file.write_bytes(original)
        if DIRECTORY_SYMLINKS_AVAILABLE:
            alias = tmp_path / "alias-to-review-report"
            alias.symlink_to(run, target_is_directory=True)
            assert _check(*args, "--review-run", str(alias)) == {
                "route": "inspect",
                "reason": "redirected-review-run",
            }
    (run / "result.json").write_bytes((run / "result.json").read_bytes() + b"\n")
    assert _check(*args) == {"route": "inspect", "reason": "review-cleanup-receipt-invalid"}


@pytest.mark.integration
@pytest.mark.parametrize(
    "terminal",
    [
        pytest.param(_current_closed_terminal, id="closed"),
        pytest.param(_current_unavailable_terminal, id="unavailable"),
    ],
)
def test_terminal_finalization_cannot_issue_a_safety_assessment(
    tmp_path: Path, terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed or unavailable PR keeps its strict terminal record even if the parent requests a positive assessment."""
    result, original = terminal(tmp_path)
    if original["metadata"]["review_status"] == "unavailable":
        # A historical v8 writer accepts its old confidence and handoff; the current finalizer requires current inputs.
        fixtures = Path(__file__).parents[1] / "pr"
        monkeypatch.syspath_prepend(str(fixtures))
        current = load_module_registered(
            "safety_unavailable_current", fixtures / "test_code_review_unavailable_artifact_contract.py"
        )
        current._judged_profile(original["metadata"], checkout_started=True)
        result.write_text(json.dumps(original), encoding="utf-8")
        handoffs = load_module("safety_unavailable_handoff", fixtures / "test_unavailable_multi_confidence.py")
        handoff = handoffs.unavailable_handoff(handoffs.CHECKOUT_STARTED, command_failure=False)
        (tmp_path / "final-handoff.json").write_text(json.dumps(handoff), encoding="utf-8")
    _promote(tmp_path, "safe")
    metadata = json.loads(result.read_bytes())["metadata"]
    assert metadata["review_status"] == original["metadata"]["review_status"]
    assert "code_safety" not in metadata
