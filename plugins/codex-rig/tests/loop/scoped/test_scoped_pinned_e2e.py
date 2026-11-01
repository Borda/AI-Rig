"""Exercise pinned scoped validation with real Git and synthetic host records.

The end-to-end test builds a pinned plan, native reviewer and audit runs and shared host session records, then tampers
with each piece of evidence in turn and restores it; the remaining tests cover plans that must never be admitted to the
pinned profile (hidden index flags, unbound origin, prior-finding continuation).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from test_adversarial_loop_artifacts import _bind_real_chunk_run, _collect_chunk_sessions, _write_loop
from test_loop_review_evidence import _rewrite_native_outputs
from test_prior_support_origin import _plan as _prior_support_plan
from test_scoped_interactions import _write_authenticated_verdict

ROOT = Path(__file__).resolve().parents[3]
CHECKER = ROOT / "skills/challenge-resolve/chunk_diff.py"
COLLECTOR = ROOT / "shared/collect_diff.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped chunk coordinator CLI."""
    return subprocess.run([sys.executable, str(CHECKER), *args], capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> None:
    """Run real Git against ``repo`` and require success."""
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def _rev_parse(repo: Path, revision: str = "HEAD") -> str:
    """Resolve ``revision`` in ``repo`` to a full object id."""
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", revision], text=True).strip()


def _commit(repo: Path, message: str) -> None:
    """Commit the index of ``repo`` with a fixed identity."""
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", message)


def _collect_mirror(origin: Path, receipt: Path) -> Path:
    """Collect a review worktree mirror of ``origin`` and return the mirror path."""
    collected = subprocess.run(
        [sys.executable, str(COLLECTOR), "--review-worktree", "--repository", str(origin), "--out", str(receipt)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    return Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])


def _load_checker() -> ModuleType:
    """Load the coordinator as a module to reuse its audit-requirement derivation as the control."""
    spec = spec_from_file_location("scoped_checker_control", CHECKER)
    checker = module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker


def _first_failure(*stages: Callable[[], subprocess.CompletedProcess[str]]) -> subprocess.CompletedProcess[str]:
    """Run ``stages`` in order, stopping at the first failing one, and return the last result obtained."""
    result = None
    for stage in stages:
        result = stage()
        if result.returncode != 0:
            break
    return result


def _expect_rejected(result: subprocess.CompletedProcess[str], message: str) -> None:
    """Assert a coordinator run failed with ``message`` on stderr."""
    assert result.returncode != 0
    assert message in result.stderr


def _reviewed_verdicts(requirements: list[dict[str, Any]], *, omitted_units: int = 0) -> list[dict[str, Any]]:
    """Return a reviewed verdict per requirement, with the audit's last ``omitted_units`` unit verdicts left out."""
    verdicts = [
        {"id": req["id"], "verdict": "reviewed", "evidence": ["Inspected the assigned source."]} for req in requirements
    ]
    for req, verdict in zip(requirements, verdicts, strict=True):
        if req["id"] == "interaction-inventory-audit":
            unit_ids = req["unit_ids"][: len(req["unit_ids"]) - omitted_units]
            verdict["unit_verdicts"] = [{"id": unit, "verdict": "reviewed"} for unit in unit_ids]
    return verdicts


def _child(
    run: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
    paths: list[str],
    source: bytes,
    diff: bytes,
    request: dict[str, str],
    requirements: list[dict[str, object]],
    identity: str,
    sessions: Path,
    base: str,
    prior_finding: dict | None = None,
    extra_support: list[str] | None = None,
) -> dict:
    """Bind a real native reviewer run, its supporting source and assigned verdicts, and collect its sessions."""
    run.mkdir(parents=True)
    evidence = {}
    findings = [prior_finding] if prior_finding else []
    candidate = _write_loop(run, monkeypatch, evidence_out=evidence, findings=findings)
    assigned_request = request | {
        "specification": request["specification"]
        + "\nInteraction assessment:\n"
        + json.dumps(requirements, sort_keys=True, ensure_ascii=True)
    }
    _bind_real_chunk_run(run, evidence, repository, paths, source, diff, assigned_request, identity, findings)
    native = evidence["fixture"]
    native["manifest"] = json.loads((run / "review/specialist-manifest.json").read_text())
    native["plan"] = json.loads((run / "review/inspection-plan.json").read_text())
    fixture_validator = evidence["validator"]
    support = sorted(
        ({path for req in requirements for path in req["source_paths"]} | set(extra_support or [])) - set(paths)
    )
    supporting = None
    if support:
        supporting = fixture_validator._canonical_source_bytes(
            fixture_validator.capture_source_snapshot(repository, support)
        )
        for filename in ("current-supporting.json", "supporting-1.json"):
            (run / filename).write_bytes(supporting)
    loop = json.loads((run / "loop-evidence.json").read_text())
    loop.update(
        schema_version=3,
        diff_base_oid=base,
        interaction_requirements=requirements,
        supporting_paths=support,
        current_supporting_source_path="current-supporting.json" if support else None,
    )
    loop["rounds"][0]["supporting_source_path"] = "supporting-1.json" if support else None
    (run / "loop-evidence.json").write_text(json.dumps(loop))
    _rewrite_native_outputs(
        native,
        source,
        diff,
        findings,
        full=True,
        request=assigned_request,
        supporting_bytes=supporting,
        coverage_paths=sorted(set(paths + support)),
    )
    _write_authenticated_verdict(native, _reviewed_verdicts(requirements))
    (run / "result.json").write_bytes(candidate.read_bytes())
    _collect_chunk_sessions(evidence, sessions)
    return native


def _save_review(native: dict[str, Any], home: Path, sessions: Path) -> dict[Path, bytes]:
    """Capture every file a rewritten reviewer verdict touches so it can be restored byte for byte."""
    review = native["run"]
    manifest_path = review / "specialist-manifest.json"
    native["manifest"] = json.loads(manifest_path.read_text())
    native["sessions"] = home
    attempt = native["manifest"]["passes"][0]["attempts"][0]
    paths = [
        manifest_path,
        review / attempt["output_path"],
        review.parent / "review-1.md",
        sessions / f"rollout-{attempt['agent_thread_id']}.jsonl",
        sessions / "rollout-parent-thread.jsonl",
    ]
    return {path: path.read_bytes() for path in paths}


def _restore(files: dict[Path, bytes]) -> None:
    """Write back files captured by ``_save_review``."""
    for path, contents in files.items():
        path.write_bytes(contents)


def _alter_delivered_context(parent_path: Path, spawn_call_id: str) -> None:
    """Replace the delivered source text in the parent receipt's spawn message."""
    rows = [json.loads(line) for line in parent_path.read_bytes().splitlines()]
    altered = 0
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "function_call" and payload.get("call_id") == spawn_call_id:
            arguments = json.loads(payload["arguments"])
            assert "VALUE = 4" in arguments["message"]
            arguments["message"] = arguments["message"].replace("VALUE = 4", "VALUE = 9")
            payload["arguments"] = json.dumps(arguments)
            altered += 1
    assert altered
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.timeout(300)
def test_text_only_scoped_review_and_authenticated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate authenticated scoped review and rejection of altered evidence.

    A pinned plan with one reviewer and one independent audit is accepted, then each piece of evidence (child base,
    original checkout, binary support, reviewer verdict, audit units, supporting source, delivered context and auditor
    identity) is tampered with, must be rejected, and is restored so the next phase starts from a passing state. The
    expensive real arrangement is shared across phases on purpose; splitting it would repeat it for every tamper.

    Windows coverage runs approach the global two-minute limit, whose thread timer kills the whole worker. Keep this
    multi-phase CLI flow bounded while allowing its complete tamper-and-restore checks to finish under runner load.
    """
    origin = tmp_path / "repo"
    origin.mkdir()
    (origin / "producer.py").write_text("VALUE = 1\n")
    (origin / "consumer.py").write_text("from producer import VALUE\n")
    _git(origin, "init", "-q")
    _git(origin, "add", ".")
    _commit(origin, "base")
    base = _rev_parse(origin)
    (origin / "producer.py").write_text("VALUE = 2\n")
    _git(origin, "add", "producer.py")
    _commit(origin, "changed")
    (origin / "producer.py").write_text("VALUE = 4\n")
    (origin / "new.py").write_text("NEW = 3\n")
    repository = _collect_mirror(origin, tmp_path / "receipt")
    root = tmp_path / "chunks"
    planned = _run(
        "plan",
        "--repository",
        str(repository),
        "--origin-repository",
        str(origin),
        "--diff-base",
        base,
        "--out",
        str(root),
        "--budget-bytes",
        "1000000",
        "--goal",
        "Review changes",
        "--specification",
        "Review producer and consumer",
        "--done-when",
        "Reviewed",
    )
    assert planned.returncode == 0, planned.stderr
    interaction = {
        "id": "producer-consumer",
        "source_paths": ["consumer.py", "producer.py"],
        "claim": "Check use of VALUE.",
    }
    assignment = {
        "interactions": [{"chunk_id": "chunk-001", "requirement": interaction}],
        "audit_assignments": [{"id": "audit-001", "chunk_ids": ["chunk-001"]}],
    }
    assign_path = tmp_path / "assignment.json"
    assign_path.write_text(json.dumps(assignment))
    scoped = _run(
        "scoped-plan",
        "--manifest",
        str(root / "chunks.json"),
        "--assignments",
        str(assign_path),
        "--out",
        str(root / "scoped-chunks.json"),
    )
    assert scoped.returncode == 0, scoped.stderr
    preflight = _run("check", "--manifest", str(root / "scoped-chunks.json"))
    assert preflight.returncode == 0, preflight.stderr
    manifest = json.loads((root / "scoped-chunks.json").read_text())
    request = manifest["request"]
    assert len(manifest["chunks"]) == 1
    chunk = manifest["chunks"][0]
    sessions = tmp_path / "home/sessions"
    sessions.mkdir(parents=True)
    source = (root / chunk["source_path"]).read_bytes()
    diff = (root / chunk["diff_path"]).read_bytes()
    component = _child(
        root / "runs/component",
        monkeypatch,
        repository,
        chunk["scope_paths"],
        source,
        diff,
        request,
        [interaction],
        "component",
        sessions,
        base,
    )
    audit_requirements = _load_checker()._scoped_audit_requirements(manifest, manifest["audit_assignments"][0])
    auditor = _child(
        root / "runs/audit",
        monkeypatch,
        repository,
        chunk["scope_paths"],
        source,
        diff,
        request,
        audit_requirements,
        "audit",
        sessions,
        base,
    )
    results = {
        "schema_version": 2,
        "chunks": [{"id": "chunk-001", "result_path": "runs/component/result.json"}],
        "audits": [{"id": "audit-001", "result_path": "runs/audit/result.json"}],
    }
    (root / "results.json").write_text(json.dumps(results))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    scoped_manifest = str(root / "scoped-chunks.json")
    results_path = str(root / "results.json")

    def check() -> subprocess.CompletedProcess[str]:
        return _run("check", "--manifest", scoped_manifest, "--results", results_path)

    checked = _run("check", "--manifest", scoped_manifest, "--results", results_path, "--summary")
    assert checked.returncode == 0, checked.stderr
    assert len(json.loads(checked.stdout)["runs"]) == 2

    # Child evidence must bind the exact pinned parent, even if another valid commit exists.
    child_evidence = root / "runs/component/loop-evidence.json"
    before = child_evidence.read_bytes()
    wrong_base = json.loads(before)
    wrong_base["diff_base_oid"] = _rev_parse(repository)
    assert wrong_base["diff_base_oid"] != base
    child_evidence.write_text(json.dumps(wrong_base))
    _expect_rejected(check(), "chunk-result-diff-base-mismatch")
    child_evidence.write_bytes(before)
    assert check().returncode == 0

    # Original caller drift blocks acceptance even while the mirror stays frozen.
    original_source = origin / "producer.py"
    before = original_source.read_bytes()
    original_source.write_text("VALUE = 999\n")
    _expect_rejected(check(), "chunk-origin-source-changed")
    original_source.write_bytes(before)
    assert check().returncode == 0

    # Unsupported NUL-bearing binary support cannot enter this text-only profile.
    before_original = (origin / "consumer.py").read_bytes()
    before_mirror = (repository / "consumer.py").read_bytes()
    (origin / "consumer.py").write_bytes(b"\x00consumer")
    (repository / "consumer.py").write_bytes(b"\x00consumer")
    _expect_rejected(check(), "chunk-origin-source-changed")
    (origin / "consumer.py").write_bytes(before_original)
    (repository / "consumer.py").write_bytes(before_mirror)
    assert check().returncode == 0

    # An actual authenticated-shaped child result must explicitly resolve the assigned interaction.
    saved = _save_review(component, tmp_path / "home", sessions)
    _write_authenticated_verdict(component, [])
    _expect_rejected(check(), "chunk-result-validation-failed")
    _restore(saved)
    assert check().returncode == 0

    # An audit cannot account for only a subset of the assigned changed units.
    saved = _save_review(auditor, tmp_path / "home", sessions)
    _write_authenticated_verdict(auditor, _reviewed_verdicts(audit_requirements, omitted_units=1))
    _expect_rejected(check(), "chunk-result-validation-failed")
    _restore(saved)
    assert check().returncode == 0

    # A changed unchanged consumer invalidates the source-bound plan.
    consumer = repository / "consumer.py"
    before = consumer.read_bytes()
    consumer.write_text("VALUE = 999\n")
    assert check().returncode != 0
    consumer.write_bytes(before)
    assert check().returncode == 0

    # Replacing the delivered context in the parent receipt must not pass provenance validation.
    parent_path = sessions / "rollout-parent-thread.jsonl"
    before = parent_path.read_bytes()
    _alter_delivered_context(parent_path, component["manifest"]["passes"][0]["attempts"][0]["spawn_call_id"])
    _expect_rejected(check(), "chunk-result-validation-failed")
    parent_path.write_bytes(before)
    assert check().returncode == 0

    # A forged auditor identity matching the component must fail authenticated lineage.
    ledger = root / "runs/audit/loop-rounds.jsonl"
    before = ledger.read_bytes()
    rows = [json.loads(line) for line in before.splitlines()]
    rows[-1]["reviewer"]["identity"] = "component-child-2"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    _expect_rejected(check(), "chunk-result-validation-failed")
    ledger.write_bytes(before)
    assert check().returncode == 0


@pytest.mark.parametrize(
    ("flag", "marker"),
    [
        pytest.param("--assume-unchanged", "h", id="assume-unchanged"),
        pytest.param("--skip-worktree", "S", id="skip-worktree"),
    ],
)
def test_original_hidden_git_index_flags_cannot_omit_changes(tmp_path: Path, flag: str, marker: str) -> None:
    """Git index flags cannot hide changed original files from a scoped plan.

    ``hidden.py`` is marked assume-unchanged or skip-worktree and then edited, so ``git diff`` omits it. The pipeline
    (plan, scoped-plan, check) must fail closed with the unsupported-index-flags error at whichever stage detects it.
    """
    original = tmp_path / "original"
    original.mkdir()
    (original / "visible.py").write_bytes(b"value = 1\n")
    (original / "hidden.py").write_bytes(b"hidden = 1\n")
    _git(original, "init", "-q")
    _git(original, "add", ".")
    _commit(original, "base")
    base = _rev_parse(original)
    (original / "visible.py").write_bytes(b"value = 2\n")
    mirror = _collect_mirror(original, tmp_path / "receipt")
    _git(original, "update-index", flag, "--", "hidden.py")
    (original / "hidden.py").write_bytes(b"hidden = 2\n")
    flags = subprocess.check_output(["git", "-C", str(original), "ls-files", "-v", "--", "hidden.py"], text=True)
    assert flags.startswith(marker + " ")
    changed = subprocess.check_output(["git", "-C", str(original), "diff", "--name-only", base], text=True).splitlines()
    assert "visible.py" in changed
    assert "hidden.py" not in changed
    out = tmp_path / "chunks"
    assignment = tmp_path / "assignments.json"
    assignment.write_text(
        json.dumps({"interactions": [], "audit_assignments": [{"id": "audit", "chunk_ids": ["chunk-001"]}]})
    )
    scoped = out / "scoped.json"

    result = _first_failure(
        lambda: _run(
            "plan",
            "--repository",
            str(mirror),
            "--origin-repository",
            str(original),
            "--diff-base",
            base,
            "--out",
            str(out),
            "--goal",
            "Review",
            "--specification",
            "Review changes",
            "--done-when",
            "Reviewed",
        ),
        lambda: _run(
            "scoped-plan",
            "--manifest",
            str(out / "chunks.json"),
            "--assignments",
            str(assignment),
            "--out",
            str(scoped),
        ),
        lambda: _run("check", "--manifest", str(scoped)),
    )

    _expect_rejected(result, "chunk-origin-index-flags-unsupported")


def test_unbound_scoped_plan_stays_unverified(tmp_path: Path) -> None:
    """A legacy plan without a pinned origin cannot enter the new profile.

    The plan and scoped-plan steps succeed for such a repository, but the final check must still report the comparison
    profile as unverified.
    """
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "source.py").write_bytes(b"value = 1\n")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _commit(repository, "base")
    (repository / "source.py").write_bytes(b"value = 2\n")
    out = tmp_path / "chunks"
    planned = _run(
        "plan",
        "--repository",
        str(repository),
        "--out",
        str(out),
        "--goal",
        "Review",
        "--specification",
        "Review changes",
        "--done-when",
        "Reviewed",
    )
    assert planned.returncode == 0, planned.stderr
    assignment = tmp_path / "assignments.json"
    assignment.write_text(
        json.dumps({"interactions": [], "audit_assignments": [{"id": "audit", "chunk_ids": ["chunk-001"]}]})
    )
    scoped = out / "scoped.json"
    generated = _run(
        "scoped-plan", "--manifest", str(out / "chunks.json"), "--assignments", str(assignment), "--out", str(scoped)
    )
    assert generated.returncode == 0, generated.stderr

    checked = _run("check", "--manifest", str(scoped))

    _expect_rejected(checked, "scoped-comparison-profile-unverified")


def test_pinned_continuation_stays_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prior-finding continuation cannot enter the first-run-only profile.

    The plan cites a prior finding's supporting caller, which makes it a continuation; the pinned profile is for first
    runs only, so the final check must report it as unverified.
    """
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    planned = _prior_support_plan(tmp_path, supporting_path="caller.py")

    checked = _run("check", "--manifest", str(planned.scoped))

    _expect_rejected(checked, "scoped-comparison-profile-unverified")
