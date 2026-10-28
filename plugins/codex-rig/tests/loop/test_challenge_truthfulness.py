"""Guard challenge results against unverified clean and prior-finding claims."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from test_loop_review_evidence import (
    REQUEST,
    _ledger_digest,
    _loop_evidence_run,
    _read_ledger,
    _repository,
    _rewrite_native_outputs,
    _validator,
    _write_json,
    _write_ledger,
)


@pytest.fixture(autouse=True)
def _native_runtime_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind the controlled native-review receipts to their parent thread."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")


def test_clean_ledger_rejects_reviewer_nonclean_source_rating(tmp_path: Path) -> None:
    """A rating of four cannot certify a zero-finding challenge as clean."""
    current = _loop_evidence_run(tmp_path, current_contract=True)
    run = current["run"]
    fixture = current["fixture"]
    assert isinstance(run, Path)
    assert isinstance(fixture, dict)
    manifest = fixture["manifest"]
    attempt = manifest["passes"][0]["attempts"][0]
    output_path = run / "review" / attempt["output_path"]
    original = output_path.read_text(encoding="utf-8")
    assert '"rating": 1' in original or '"rating": 4' in original
    output = original.replace('"rating": 1', '"rating": 4')
    output_path.write_text(output, encoding="utf-8", newline="\n")
    attempt["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    _write_json(run / "review" / "specialist-manifest.json", manifest)
    (run / "review-1.md").write_bytes(output_path.read_bytes())
    message = output.removesuffix("\n")
    sessions = fixture["sessions"] / "sessions"
    child_path = sessions / f"rollout-{attempt['agent_thread_id']}.jsonl"
    child = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
    child[-1]["payload"]["last_agent_message"] = message
    child_path.write_text("".join(json.dumps(row) + "\n" for row in child), encoding="utf-8", newline="\n")
    parent_path = sessions / "rollout-parent-thread.jsonl"
    parent = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    for row in parent:
        payload = row.get("payload", {})
        if payload.get("type") == "agent_message" and payload.get("author") == attempt["agent_path"]:
            payload["content"][0]["text"] = f"Message Type: FINAL_ANSWER\nPayload:\n{message}"
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in parent), encoding="utf-8", newline="\n")
    assert '"rating": 4' in output

    with pytest.raises(ValueError, match="loop-evidence-report-clean-rating-mismatch"):
        current["validator"].validate_loop_evidence(run, current["codex_home"])


def test_prior_finding_needs_its_own_raw_reviewer_verdict(tmp_path: Path) -> None:
    """Parent triage cannot turn an unrelated reviewed defect into closure of a prior one."""
    prior_finding = {
        "signature": "missing-guard",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["The widget lacks a guard."],
    }
    unrelated = {
        "signature": "different-bug",
        "tier": "high",
        "structural": False,
        "disposition": "verified-fixed",
        "evidence": ["A different bug was fixed."],
    }
    prior_root = tmp_path / "prior-root"
    current_root = tmp_path / "current-root"
    prior_root.mkdir()
    current_root.mkdir()
    prior = _loop_evidence_run(prior_root, findings=[prior_finding])
    current = _loop_evidence_run(current_root, findings=[unrelated], current_contract=True)
    run, prior_run, fixture = current["run"], prior["run"], current["fixture"]
    assert isinstance(run, Path)
    assert isinstance(prior_run, Path)
    assert isinstance(fixture, dict)
    prior_block = "Prior findings to reassess:\n" + json.dumps([prior_finding], sort_keys=True, ensure_ascii=True)
    request = REQUEST | {"specification": REQUEST["specification"] + "\n" + prior_block}
    _rewrite_native_outputs(
        fixture,
        (run / "source-1.json").read_bytes(),
        (run / "round-1.diff").read_bytes(),
        [unrelated],
        full=True,
        request=request,
        coverage_paths=["widget.py"],
    )
    selected = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((run / "review" / selected["output_path"]).read_bytes())
    ledger = _read_ledger(run / "loop-ledger.json")
    ledger["rounds"][0]["findings"][0]["signature"] = "missing-guard"
    _write_ledger(run / "loop-ledger.json", ledger)
    evidence_path = run / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["request"] = request
    evidence["origin"] = {
        "kind": "continuation",
        "caller_run": prior_run.resolve().as_posix(),
        "prior_ledger_sha256": _ledger_digest(prior_run / "loop-ledger.json"),
    }
    evidence["rounds"][0]["triage"] = [
        {
            "reported_signature": "different-bug",
            "signature": "missing-guard",
            "tier": "high",
            "reason": "Claim these unrelated findings are the same.",
            "evidence": ["Parent asserts sameness."],
        }
    ]
    _write_json(evidence_path, evidence)

    with pytest.raises(ValueError, match="loop-evidence-prior-raw-verdict-missing:missing-guard"):
        current["validator"].validate_loop_evidence(run, current["codex_home"])


def _bind_scope(current: dict[str, object], scope: str, reviewed_paths: list[str]) -> dict[str, object]:
    """Bind authentic-shaped review receipts to a fresh literal-scope snapshot and Git diff."""
    run = current["run"]
    repository = current["repository"]
    fixture = current["fixture"]
    validator = current["validator"]
    assert isinstance(run, Path)
    assert isinstance(repository, Path)
    assert isinstance(fixture, dict)
    source = validator.capture_source_snapshot(repository, [scope])
    source_bytes = validator._canonical_source_bytes(source)
    for name in ("current-source.json", "source-1.json"):
        (run / name).write_bytes(source_bytes)
    diff = subprocess.run(
        ["git", "--literal-pathspecs", "diff", "HEAD", "--binary", "--no-renames", "--", scope],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    digest = hashlib.sha256(diff).hexdigest()
    (run / "round-1.diff").write_bytes(diff)
    (run / "review" / "diff.patch").write_bytes(diff)
    fixture["manifest"]["review_input_sha256"] = digest
    fixture["plan"]["review_input_sha256"] = digest
    ledger = _read_ledger(run / "loop-ledger.json")
    ledger["current_snapshot"]["diff_digest"] = digest
    ledger["rounds"][0]["snapshot"]["diff_digest"] = digest
    _write_ledger(run / "loop-ledger.json", ledger)
    evidence_path = run / "loop-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["scope_paths"] = [scope]
    _write_json(evidence_path, evidence)
    _rewrite_native_outputs(
        fixture, source_bytes, diff, [], full=True, request=evidence["request"], coverage_paths=reviewed_paths
    )
    selected = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((run / "review" / selected["output_path"]).read_bytes())
    return source


def test_directory_scope_requires_each_captured_file_in_review_coverage(tmp_path: Path) -> None:
    """Naming a directory must not conceal an omitted captured file."""
    current = _loop_evidence_run(tmp_path, current_contract=True)
    source = _bind_scope(current, ".", ["."])
    assert [record["path"] for record in source["files"]] == ["widget.py"]

    with pytest.raises(ValueError, match="loop-evidence-report-coverage-missing"):
        current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


def test_directory_scope_accepts_complete_concrete_file_coverage(tmp_path: Path) -> None:
    """A reviewer naming each captured file need not invent a directory-level finding."""
    current = _loop_evidence_run(tmp_path, current_contract=True)
    source = _bind_scope(current, ".", ["widget.py"])
    assert [record["path"] for record in source["files"]] == ["widget.py"]

    current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


@pytest.mark.parametrize(
    "scope",
    [pytest.param("never-existed.py", id="nonexistent"), pytest.param("ignored.py", id="ignored")],
)
def test_unrepresented_literal_primary_scope_cannot_be_reviewed(tmp_path: Path, scope: str) -> None:
    """A missing or ignored source file must not receive a fabricated clean review."""
    current = _loop_evidence_run(tmp_path, current_contract=True)
    repository = current["repository"]
    assert isinstance(repository, Path)
    if scope == "ignored.py":
        (repository / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        (repository / scope).write_text("VALUE = 1\n", encoding="utf-8")
    source = _bind_scope(current, scope, [scope])
    assert source["files"] == []

    with pytest.raises(ValueError, match="loop-evidence-primary-source-incomplete"):
        current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


def test_tracked_deletion_remains_reviewable_as_a_captured_missing_record(tmp_path: Path) -> None:
    """Keep valid source deletions reviewable even though the worktree path is absent."""
    current = _loop_evidence_run(tmp_path, current_contract=True)
    repository = current["repository"]
    assert isinstance(repository, Path)
    (repository / "widget.py").unlink()
    source = _bind_scope(current, "widget.py", ["widget.py"])
    assert [(record["path"], record["kind"]) for record in source["files"]] == [("widget.py", "missing")]

    current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


def test_binary_source_requires_review_limitation(tmp_path: Path) -> None:
    """Encoded binary data must not be reported as source reviewed with no limitation."""
    current = _loop_evidence_run(tmp_path, current_contract=True)
    repository = current["repository"]
    assert isinstance(repository, Path)
    (repository / "widget.py").write_bytes(b"\xff\x00binary")
    source = _bind_scope(current, "widget.py", ["widget.py"])
    assert source["files"][0]["encoding"] == "base64"

    with pytest.raises(ValueError, match="loop-evidence-binary-source-limit-missing"):
        current["validator"].validate_loop_evidence(current["run"], current["codex_home"])


def test_preflight_rejects_unrepresented_primary_scope_before_dispatch(tmp_path: Path) -> None:
    """An empty literal source capture must fail before sending a reviewer any context."""
    validator = _validator()
    repository = _repository(tmp_path)
    scope = ["never-existed.py"]
    snapshot = validator.capture_source_snapshot(repository, scope)
    assert snapshot["files"] == []
    source = validator._canonical_source_bytes(snapshot)
    diff = b""
    request_text = json.dumps(REQUEST, sort_keys=True, ensure_ascii=True).encode()
    context = b"Review request:\n" + request_text + b"\nFrozen source:\n" + source + b"\nFrozen diff:\n\n"
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir()
    (plan_dir / "source.json").write_bytes(source)
    (plan_dir / "diff.patch").write_bytes(diff)
    (plan_dir / "context.txt").write_bytes(context)
    _write_json(
        plan_dir / "plan.json",
        {
            "source_path": "source.json",
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "diff_path": "diff.patch",
            "diff_sha256": hashlib.sha256(diff).hexdigest(),
            "nodes": [
                {
                    "role_id": "challenger",
                    "context_path": "context.txt",
                    "context_sha256": hashlib.sha256(context).hexdigest(),
                }
            ],
        },
    )
    _write_json(
        plan_dir / "request.json",
        {
            "schema_version": 2,
            "origin": {"kind": "first-run", "caller_run": None, "prior_ledger_sha256": None},
            "repository": repository.resolve().as_posix(),
            "scope_paths": scope,
            "current_source_path": "source.json",
            "request": REQUEST,
            "supporting_paths": [],
            "current_supporting_source_path": None,
        },
    )

    with pytest.raises(ValueError, match="loop-evidence-primary-source-incomplete"):
        validator.preflight_review_plan(plan_dir / "plan.json", plan_dir / "request.json", None)
