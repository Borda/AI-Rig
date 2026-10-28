"""Exercise pinned scoped validation with real Git and synthetic host records."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_adversarial_loop_artifacts import _bind_real_chunk_run, _collect_chunk_sessions, _write_loop
from test_scoped_interactions import _write_authenticated_verdict

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "skills/challenge-resolve/chunk_diff.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECKER), *args], capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


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
    from test_loop_review_evidence import _rewrite_native_outputs

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
    verdicts = []
    for req in requirements:
        verdict = {"id": req["id"], "verdict": "reviewed", "evidence": ["Inspected the assigned source."]}
        if req["id"] == "interaction-inventory-audit":
            verdict["unit_verdicts"] = [{"id": unit, "verdict": "reviewed"} for unit in req["unit_ids"]]
        verdicts.append(verdict)
    _write_authenticated_verdict(native, verdicts)
    (run / "result.json").write_bytes(candidate.read_bytes())
    _collect_chunk_sessions(evidence, sessions)
    return native


@pytest.mark.timeout(300)
def test_text_only_scoped_review_and_authenticated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate authenticated scoped review and rejection of altered evidence.

    Windows coverage runs approach the global two-minute limit, whose thread timer kills the whole worker. Keep this
    multi-phase CLI flow bounded while allowing its complete tamper-and-restore checks to finish under runner load.
    """
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "producer.py").write_text("VALUE = 1\n")
    (repository / "consumer.py").write_text("from producer import VALUE\n")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    (repository / "producer.py").write_text("VALUE = 2\n")
    _git(repository, "add", "producer.py")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "changed")
    (repository / "producer.py").write_text("VALUE = 4\n")
    (repository / "new.py").write_text("NEW = 3\n")
    origin = repository
    receipt = tmp_path / "receipt"
    collected = subprocess.run(
        [
            sys.executable,
            str(ROOT / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(origin),
            "--out",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    repository = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    root = tmp_path / "chunks"
    request = {"goal": "Review changes", "specification": "Review producer and consumer", "done_when": "Reviewed"}
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
        request["goal"],
        "--specification",
        request["specification"],
        "--done-when",
        request["done_when"],
    )
    assert planned.returncode == 0, planned.stderr
    assignment = {
        "interactions": [
            {
                "chunk_id": "chunk-001",
                "requirement": {
                    "id": "producer-consumer",
                    "source_paths": ["consumer.py", "producer.py"],
                    "claim": "Check use of VALUE.",
                },
            }
        ],
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
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("scoped_checker_control", CHECKER)
    checker = module_from_spec(spec)
    spec.loader.exec_module(checker)
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
        [assignment["interactions"][0]["requirement"]],
        "component",
        sessions,
        base,
    )
    audit = manifest["audit_assignments"][0]
    audit_requirements = checker._scoped_audit_requirements(manifest, audit)
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
    checked = _run(
        "check", "--manifest", str(root / "scoped-chunks.json"), "--results", str(root / "results.json"), "--summary"
    )
    assert checked.returncode == 0, checked.stderr
    assert len(json.loads(checked.stdout)["runs"]) == 2

    def check() -> subprocess.CompletedProcess[str]:
        return _run("check", "--manifest", str(root / "scoped-chunks.json"), "--results", str(root / "results.json"))

    # Child evidence must bind the exact pinned parent, even if another valid commit exists.
    child_evidence = root / "runs/component/loop-evidence.json"
    before = child_evidence.read_bytes()
    wrong_base = json.loads(before)
    wrong_base["diff_base_oid"] = _git_output = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    assert _git_output != base
    child_evidence.write_text(json.dumps(wrong_base))
    mismatch = check()
    assert mismatch.returncode != 0
    assert "chunk-result-diff-base-mismatch" in mismatch.stderr
    child_evidence.write_bytes(before)
    assert check().returncode == 0

    # Original caller drift blocks acceptance even while the mirror stays frozen.
    original_source = origin / "producer.py"
    before = original_source.read_bytes()
    original_source.write_text("VALUE = 999\n")
    stale_origin = check()
    assert stale_origin.returncode != 0
    assert "chunk-origin-source-changed" in stale_origin.stderr
    original_source.write_bytes(before)
    assert check().returncode == 0

    # Unsupported NUL-bearing binary support cannot enter this text-only profile.
    before_original = (origin / "consumer.py").read_bytes()
    before_mirror = (repository / "consumer.py").read_bytes()
    (origin / "consumer.py").write_bytes(b"\x00consumer")
    (repository / "consumer.py").write_bytes(b"\x00consumer")
    binary = check()
    assert binary.returncode != 0
    assert "chunk-origin-source-changed" in binary.stderr
    (origin / "consumer.py").write_bytes(before_original)
    (repository / "consumer.py").write_bytes(before_mirror)
    assert check().returncode == 0

    def save_review(native: dict) -> dict[Path, bytes]:
        review = native["run"]
        manifest_path = review / "specialist-manifest.json"
        native["manifest"] = json.loads(manifest_path.read_text())
        native["sessions"] = tmp_path / "home"
        attempt = native["manifest"]["passes"][0]["attempts"][0]
        paths = [
            manifest_path,
            review / attempt["output_path"],
            review.parent / "review-1.md",
            sessions / f"rollout-{attempt['agent_thread_id']}.jsonl",
            sessions / "rollout-parent-thread.jsonl",
        ]
        return {path: path.read_bytes() for path in paths}

    def restore(files: dict[Path, bytes]) -> None:
        for path, contents in files.items():
            path.write_bytes(contents)

    # An actual authenticated-shaped child result must explicitly resolve the assigned interaction.
    saved = save_review(component)
    _write_authenticated_verdict(component, [])
    omitted = check()
    assert omitted.returncode != 0
    assert "chunk-result-validation-failed" in omitted.stderr
    restore(saved)
    assert check().returncode == 0

    # An audit cannot account for only a subset of the assigned changed units.
    saved = save_review(auditor)
    audit_verdicts = []
    for requirement in audit_requirements:
        verdict = {"id": requirement["id"], "verdict": "reviewed", "evidence": ["Inspected."]}
        if requirement["id"] == "interaction-inventory-audit":
            verdict["unit_verdicts"] = [{"id": unit, "verdict": "reviewed"} for unit in requirement["unit_ids"][:-1]]
        audit_verdicts.append(verdict)
    _write_authenticated_verdict(auditor, audit_verdicts)
    omitted_unit = check()
    assert omitted_unit.returncode != 0
    assert "chunk-result-validation-failed" in omitted_unit.stderr
    restore(saved)
    assert check().returncode == 0

    # A changed unchanged consumer invalidates the source-bound plan.
    consumer = repository / "consumer.py"
    before = consumer.read_bytes()
    consumer.write_text("VALUE = 999\n")
    stale = check()
    assert stale.returncode != 0
    consumer.write_bytes(before)
    assert check().returncode == 0

    # Replacing the delivered context in the parent receipt must not pass provenance validation.
    parent_path = sessions / "rollout-parent-thread.jsonl"
    before = parent_path.read_bytes()
    rows = [json.loads(line) for line in before.splitlines()]
    attempt = component["manifest"]["passes"][0]["attempts"][0]
    for row in rows:
        payload = row.get("payload", {})
        if payload.get("type") == "function_call" and payload.get("call_id") == attempt["spawn_call_id"]:
            arguments = json.loads(payload["arguments"])
            assert "VALUE = 4" in arguments["message"]
            arguments["message"] = arguments["message"].replace("VALUE = 4", "VALUE = 9")
            payload["arguments"] = json.dumps(arguments)
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    altered = check()
    assert altered.returncode != 0
    assert "chunk-result-validation-failed" in altered.stderr
    parent_path.write_bytes(before)
    assert check().returncode == 0

    # A forged auditor identity matching the component must fail authenticated lineage.
    ledger = root / "runs/audit/loop-rounds.jsonl"
    before = ledger.read_bytes()
    rows = [json.loads(line) for line in before.splitlines()]
    rows[-1]["reviewer"]["identity"] = "component-child-2"
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    forged = check()
    assert forged.returncode != 0
    assert "chunk-result-validation-failed" in forged.stderr
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
    """Git index flags cannot hide changed original files from a scoped plan."""
    original = tmp_path / "original"
    original.mkdir()
    (original / "visible.py").write_bytes(b"value = 1\n")
    (original / "hidden.py").write_bytes(b"hidden = 1\n")
    _git(original, "init", "-q")
    _git(original, "add", ".")
    _git(original, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    base = subprocess.check_output(["git", "-C", str(original), "rev-parse", "HEAD"], text=True).strip()
    (original / "visible.py").write_bytes(b"value = 2\n")
    receipt = tmp_path / "receipt"
    collected = subprocess.run(
        [
            sys.executable,
            str(ROOT / "shared/collect_diff.py"),
            "--review-worktree",
            "--repository",
            str(original),
            "--out",
            str(receipt),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stderr
    mirror = Path(json.loads((receipt / "review-worktree.json").read_bytes())["review_worktree"])
    _git(original, "update-index", flag, "--", "hidden.py")
    (original / "hidden.py").write_bytes(b"hidden = 2\n")
    flags = subprocess.check_output(["git", "-C", str(original), "ls-files", "-v", "--", "hidden.py"], text=True)
    assert flags.startswith(marker + " ")
    changed = subprocess.check_output(["git", "-C", str(original), "diff", "--name-only", base], text=True).splitlines()
    assert "visible.py" in changed
    assert "hidden.py" not in changed
    out = tmp_path / "chunks"
    planned = _run(
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
    )
    result = planned
    if planned.returncode == 0:
        assignment = tmp_path / "assignments.json"
        assignment.write_text(
            json.dumps({"interactions": [], "audit_assignments": [{"id": "audit", "chunk_ids": ["chunk-001"]}]})
        )
        scoped = out / "scoped.json"
        result = _run(
            "scoped-plan",
            "--manifest",
            str(out / "chunks.json"),
            "--assignments",
            str(assignment),
            "--out",
            str(scoped),
        )
        if result.returncode == 0:
            result = _run("check", "--manifest", str(scoped))
    assert result.returncode != 0
    assert "chunk-origin-index-flags-unsupported" in result.stderr


def test_unbound_scoped_plan_stays_unverified(tmp_path: Path) -> None:
    """A legacy plan without a pinned origin cannot enter the new profile."""
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "source.py").write_bytes(b"value = 1\n")
    _git(repository, "init", "-q")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
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
    assert checked.returncode != 0
    assert "scoped-comparison-profile-unverified" in checked.stderr


def test_pinned_continuation_stays_unverified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prior-finding continuation cannot enter the first-run-only profile."""
    from test_prior_support_origin import _plan

    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    _, _, scoped = _plan(tmp_path)
    checked = _run("check", "--manifest", str(scoped))
    assert checked.returncode != 0
    assert "scoped-comparison-profile-unverified" in checked.stderr
