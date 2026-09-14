"""Exercise adversarial-loop reviewer evidence against real Code Review artifact validation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = PLUGIN_ROOT / "skills" / "adversarial-loop" / "validate_evidence.py"


@pytest.fixture(autouse=True)
def _native_runtime_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the external host identity used by the native inspection fixtures."""
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")


def _module(path: Path) -> ModuleType:
    """Load one plugin helper by file path without creating package dependencies."""
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _validator() -> ModuleType:
    """Load the production loop evidence validator under test."""
    return _module(VALIDATOR_PATH)


def _inspection_fixture(tmp_path: Path, run_dir: Path) -> dict[str, object]:
    """Reuse the real schema-five Code Review inspection artifact constructor."""
    return _module(Path(__file__).with_name("test_review_inspection.py"))._inspection_run(tmp_path, run_dir=run_dir)


def _repository(tmp_path: Path) -> Path:
    """Create one committed local repository for source-snapshot acceptance evidence."""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "widget.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    for arguments in (
        ("init", "-q"),
        ("add", "widget.py"),
        ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"),
    ):
        subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)
    return repository


def _write_json(path: Path, value: object) -> None:
    """Write one fixture JSON artifact using portable text bytes."""
    path.write_text(json.dumps(value), encoding="utf-8", newline="\n")


def _rewrite_native_outputs(
    fixture: dict[str, object],
    source_bytes: bytes,
    diff_bytes: bytes,
    findings: list[dict[str, object]],
    *,
    full: bool,
    before_block: str = "",
    after_block: str = "",
) -> None:
    """Rebind schema-five contexts, rollout messages, and outputs to frozen loop material."""
    run = fixture["run"]
    sessions = fixture["sessions"] / "sessions"
    manifest = fixture["manifest"]
    plan = fixture["plan"]
    assert isinstance(run, Path) and isinstance(sessions, Path)
    assert isinstance(manifest, dict) and isinstance(plan, dict)
    parent_path = sessions / "rollout-parent-thread.jsonl"
    parent_rows = [json.loads(line) for line in parent_path.read_text(encoding="utf-8").splitlines()]
    source_text = source_bytes.decode("utf-8")
    diff_text = diff_bytes.decode("utf-8")
    for item in manifest["passes"]:
        role = item["role"]
        attempt = item["attempts"][item["selected_attempt"] - 1]
        context_path = run / attempt["context_path"]
        card = (PLUGIN_ROOT / "roles" / role / "ROLE.md").read_text(encoding="utf-8")
        context = f"{card}\nFrozen source:\n{source_text}\n"
        if full:
            context += f"Frozen diff:\n{diff_text}\n"
        context_path.write_text(context, encoding="utf-8", newline="\n")
        context_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
        old_path = attempt["agent_path"]
        new_path = f"/root/review_{role.replace('-', '_')}_{context_hash[:12]}_a1"
        attempt.update(context_sha256=context_hash, agent_path=new_path)
        for entry in plan["contexts"]:
            if entry["role_id"] == role:
                entry["context_sha256"] = context_hash
        header = (
            f"<!-- codex-review-provenance role={role} run={manifest['review_run_id']} "
            f"input={manifest['review_input_sha256']} context={context_hash} attempt=1 -->"
        )
        report = {
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
            "findings": findings,
        }
        message = f"{header}\n{before_block}```adversarial-loop\n{json.dumps(report, sort_keys=True)}\n```{after_block}"
        output_path = run / attempt["output_path"]
        output_path.write_text(message + "\n", encoding="utf-8", newline="\n")
        attempt["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
        child_path = sessions / f"rollout-{attempt['agent_thread_id']}.jsonl"
        child_rows = [json.loads(line) for line in child_path.read_text(encoding="utf-8").splitlines()]
        child_rows[0]["payload"]["agent_path"] = new_path
        child_rows[0]["payload"]["source"]["subagent"]["thread_spawn"]["agent_path"] = new_path
        child_rows[-1]["payload"]["last_agent_message"] = message
        child_path.write_text("".join(json.dumps(row) + "\n" for row in child_rows), encoding="utf-8", newline="\n")
        for row in parent_rows:
            payload = row.get("payload", {})
            if payload.get("type") == "function_call" and payload.get("call_id") == attempt["spawn_call_id"]:
                payload["arguments"] = json.dumps(
                    {"message": context, "task_name": Path(new_path).name, "fork_turns": "none"}
                )
            if row.get("type") == "event_msg" and payload.get("item", {}).get("agent_path") == old_path:
                payload["item"]["agent_path"] = new_path
            if payload.get("type") == "agent_message" and payload.get("author") == old_path:
                payload["author"] = new_path
                payload["content"][0]["text"] = f"Message Type: FINAL_ANSWER\nPayload:\n{message}"
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in parent_rows), encoding="utf-8", newline="\n")
    plan_path = run / "inspection-plan.json"
    _write_json(plan_path, plan)
    manifest["inspection_execution"]["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    _write_json(run / "specialist-manifest.json", manifest)


def _loop_evidence_run(
    root_path: Path, *, run_dir: Path | None = None, findings: list[dict[str, object]] | None = None
) -> dict[str, object]:
    """Build a clean native loop fixture for public artifact-validation consumers."""
    validator = _validator()
    run = run_dir or root_path / "loop"
    fixture_root = root_path / "evidence-fixture"
    fixture = _inspection_fixture(fixture_root, run / "review")
    repository = _repository(fixture_root)
    reported_findings = findings or []
    source = validator.capture_source_snapshot(repository, ["widget.py"])
    source_bytes = validator._canonical_source_bytes(source)
    for name in ("current-source.json", "source-1.json"):
        (run / name).write_bytes(source_bytes)
    diff_bytes = (run / "review" / "diff.patch").read_bytes()
    (run / "round-1.diff").write_bytes(diff_bytes)
    _rewrite_native_outputs(fixture, source_bytes, diff_bytes, reported_findings, full=True)
    manifest = fixture["manifest"]
    assert isinstance(manifest, dict)
    selected = manifest["passes"][0]["attempts"][0]
    report_path = run / "review-1.md"
    report_path.write_bytes((run / "review" / selected["output_path"]).read_bytes())
    digest = hashlib.sha256(diff_bytes).hexdigest()
    ledger = {
        "schema_version": 1,
        "implementation_author": "parent-thread",
        "current_snapshot": {"revision": source["revision"], "diff_digest": digest},
        "rounds": [
            {
                "index": 1,
                "reviewer": {"identity": selected["agent_thread_id"], "independent": True},
                "snapshot": {"revision": source["revision"], "diff_digest": digest},
                "report_path": report_path.name,
                "findings": reported_findings,
            }
        ],
    }
    _write_json(run / "loop-ledger.json", ledger)
    _write_json(
        run / "loop-evidence.json",
        {
            "schema_version": 1,
            "repository": repository.resolve().as_posix(),
            "scope_paths": ["widget.py"],
            "current_source_path": "current-source.json",
            "rounds": [{"index": 1, "source_path": "source-1.json", "review_run": "review", "role": "qa-specialist"}],
        },
    )
    return {
        "run": run,
        "repository": repository,
        "codex_home": fixture["sessions"],
        "fixture": fixture,
        "validator": validator,
    }


def _bound_loop(tmp_path: Path) -> tuple[ModuleType, Path, dict[str, object]]:
    """Create one clean loop fixture for the local focused acceptance tests."""
    evidence = _loop_evidence_run(tmp_path)
    return evidence["validator"], evidence["run"], evidence["fixture"]


def _app_server_loop(
    tmp_path: Path, *, diff_newline: str = "\n", context_diff_newline: str | None = None
) -> tuple[ModuleType, Path, Path]:
    """Bind App Server evidence to explicit diff bytes, optionally altering only the supplied context."""
    validator = _validator()
    run = tmp_path / "loop"
    review = _module(Path(__file__).with_name("test_app_server_review_integration.py")).isolated_review.__wrapped__(
        run / "review"
    )
    repository_root = tmp_path / "repository-root"
    repository_root.mkdir()
    repository = _repository(repository_root)
    source = validator.capture_source_snapshot(repository, ["widget.py"])
    source_bytes = validator._canonical_source_bytes(source)
    for name in ("current-source.json", "source-1.json"):
        (run / name).write_bytes(source_bytes)
    diff_bytes = (review / "diff.patch").read_bytes().replace(b"\r\n", b"\n").replace(b"\n", diff_newline.encode())
    (review / "diff.patch").write_bytes(diff_bytes)
    (run / "round-1.diff").write_bytes(diff_bytes)
    manifest = json.loads((review / "specialist-manifest.json").read_text(encoding="utf-8"))
    execution = manifest["app_server_execution"]
    plan_path = Path(execution["plan_path"])
    evidence_path = Path(execution["evidence_path"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    for payload in (manifest, plan, evidence):
        payload["review_input_sha256"] = hashlib.sha256(diff_bytes).hexdigest()
    source_text = source_bytes.decode("utf-8")
    diff_text = diff_bytes.decode("utf-8")
    if context_diff_newline is not None:
        diff_text = diff_text.replace(diff_newline, context_diff_newline)
    for node in plan["nodes"]:
        context_path = plan_path.parent / node["context_path"]
        card = (PLUGIN_ROOT / "roles" / node["role_id"] / "ROLE.md").read_text(encoding="utf-8")
        context = f"{card}\nFrozen source:\n{source_text}\nFrozen diff:\n{diff_text}\n"
        context_path.write_text(context, encoding="utf-8", newline="\n")
        node["context_sha256"] = hashlib.sha256(context_path.read_bytes()).hexdigest()
    _write_json(plan_path, plan)
    evidence["plan_sha256"] = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    for node in evidence["nodes"]:
        plan_node = next(item for item in plan["nodes"] if item["role_id"] == node["role_id"])
        node["context_sha256"] = plan_node["context_sha256"]
        output_path = evidence_path.parent / node["output_path"]
        report = {
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "diff_sha256": hashlib.sha256(diff_bytes).hexdigest(),
            "findings": [],
        }
        output_path.write_text(
            f"```adversarial-loop\n{json.dumps(report, sort_keys=True)}\n```\n", encoding="utf-8", newline="\n"
        )
        node["output_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    _write_json(evidence_path, evidence)
    execution["evidence_sha256"] = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    _write_json(review / "specialist-manifest.json", manifest)
    selected = next(node for node in evidence["nodes"] if node["role_id"] == "qa-specialist")
    report_path = run / "review-1.md"
    report_path.write_bytes((evidence_path.parent / selected["output_path"]).read_bytes())
    digest = hashlib.sha256(diff_bytes).hexdigest()
    _write_json(
        run / "loop-ledger.json",
        {
            "schema_version": 1,
            "implementation_author": "thread",
            "current_snapshot": {"revision": source["revision"], "diff_digest": digest},
            "rounds": [
                {
                    "index": 1,
                    "reviewer": {"identity": selected["thread_id"], "independent": True},
                    "snapshot": {"revision": source["revision"], "diff_digest": digest},
                    "report_path": "review-1.md",
                    "findings": [],
                }
            ],
        },
    )
    _write_json(
        run / "loop-evidence.json",
        {
            "schema_version": 1,
            "repository": repository.resolve().as_posix(),
            "scope_paths": ["widget.py"],
            "current_source_path": "current-source.json",
            "rounds": [
                {
                    "index": 1,
                    "source_path": "source-1.json",
                    "review_run": review.relative_to(run).as_posix(),
                    "role": "qa-specialist",
                }
            ],
        },
    )
    return validator, run, tmp_path / "unused-codex-home"


def test_validates_native_review_against_frozen_source_diff_and_output(tmp_path: Path) -> None:
    """Accept only a real Code Review schema-five route that binds all required evidence."""
    validator, run, fixture = _bound_loop(tmp_path)

    validator.validate_loop_evidence(run, fixture["sessions"])


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_validates_app_server_review_against_frozen_source_diff_and_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newline: str
) -> None:
    """Accept exact LF or CRLF frozen diffs through the parent-observed App Server route."""
    validator, run, codex_home = _app_server_loop(tmp_path, diff_newline=newline)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")

    validator.validate_loop_evidence(run, codex_home)


@pytest.mark.parametrize(
    ("diff_newline", "context_newline"),
    [pytest.param("\n", "\r\n", id="lf-diff-crlf-context"), pytest.param("\r\n", "\n", id="crlf-diff-lf-context")],
)
def test_rejects_app_server_context_with_altered_diff_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, diff_newline: str, context_newline: str
) -> None:
    """Reject newline-altered context even when its own provenance hashes are valid."""
    validator, run, codex_home = _app_server_loop(
        tmp_path, diff_newline=diff_newline, context_diff_newline=context_newline
    )
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")

    with pytest.raises(ValueError, match="^loop-evidence-review-context-incomplete$"):
        validator.validate_loop_evidence(run, codex_home)


def test_rejects_authenticated_finding_omitted_from_loop_ledger(tmp_path: Path) -> None:
    """Keep a reviewer-reported finding from disappearing in a clean loop ledger."""
    finding = {
        "signature": "missing-guard",
        "tier": "high",
        "structural": False,
        "disposition": "open",
        "evidence": ["The supplied widget source lacks the required guard."],
    }
    evidence = _loop_evidence_run(tmp_path, findings=[finding])
    run = evidence["run"]
    assert isinstance(run, Path)
    ledger = json.loads((run / "loop-ledger.json").read_text(encoding="utf-8"))
    ledger["rounds"][0]["findings"] = []
    _write_json(run / "loop-ledger.json", ledger)

    with pytest.raises(ValueError, match="loop-evidence-report-findings-ledger-mismatch"):
        evidence["validator"].validate_loop_evidence(run, evidence["codex_home"])


def test_rejects_implementation_author_not_observed_review_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prevent a declared implementation author from replacing the authenticated review parent."""
    evidence = _loop_evidence_run(tmp_path)
    run = evidence["run"]
    assert isinstance(run, Path)
    ledger = json.loads((run / "loop-ledger.json").read_text(encoding="utf-8"))
    ledger["implementation_author"] = "forged-parent"
    monkeypatch.setenv("CODEX_THREAD_ID", "forged-parent")
    _write_json(run / "loop-ledger.json", ledger)

    with pytest.raises(ValueError, match="loop-evidence-implementation-author-mismatch"):
        evidence["validator"].validate_loop_evidence(run, evidence["codex_home"])


@pytest.mark.parametrize("owner", ["missing", "blank", "reviewer", "foreign-parent"])
def test_rejects_borrowed_review_without_matching_active_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """Reject genuine prior review evidence when it is reused by a different executing loop owner."""
    evidence = _loop_evidence_run(tmp_path)
    run = evidence["run"]
    ledger = json.loads((run / "loop-ledger.json").read_text(encoding="utf-8"))
    if owner == "missing":
        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    elif owner == "blank":
        monkeypatch.setenv("CODEX_THREAD_ID", " ")
    elif owner == "reviewer":
        monkeypatch.setenv("CODEX_THREAD_ID", ledger["rounds"][0]["reviewer"]["identity"])
    else:
        monkeypatch.setenv("CODEX_THREAD_ID", "another-parent-thread")

    with pytest.raises(ValueError, match="loop-evidence-active-owner-"):
        evidence["validator"].validate_loop_evidence(run, evidence["codex_home"])


@pytest.mark.parametrize(
    ("before_block", "after_block"),
    [
        pytest.param("High finding: required guard is missing.\n", "", id="finding-before-block"),
        pytest.param("", "\nHigh finding: required guard is missing.", id="finding-after-block"),
        pytest.param("", '\n```json\n{"finding": "missing guard"}\n```', id="second-response-envelope"),
    ],
)
def test_rejects_authenticated_content_outside_canonical_response(
    tmp_path: Path, before_block: str, after_block: str
) -> None:
    """Reject authentic finding prose even when the accompanying structured findings list is empty."""
    evidence = _loop_evidence_run(tmp_path)
    run = evidence["run"]
    fixture = evidence["fixture"]
    _rewrite_native_outputs(
        fixture,
        (run / "source-1.json").read_bytes(),
        (run / "round-1.diff").read_bytes(),
        [],
        full=True,
        before_block=before_block,
        after_block=after_block,
    )
    selected = fixture["manifest"]["passes"][0]["attempts"][0]
    (run / "review-1.md").write_bytes((run / "review" / selected["output_path"]).read_bytes())

    with pytest.raises(ValueError, match="loop-evidence-report-envelope-invalid"):
        evidence["validator"].validate_loop_evidence(run, evidence["codex_home"])


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param("forged-identity", id="forged-identity"),
        pytest.param("changed-report", id="changed-report"),
        pytest.param("changed-source", id="changed-source"),
        pytest.param("recaptured-unreviewed-source", id="recaptured-unreviewed-source"),
        pytest.param("missing-context", id="missing-context"),
    ],
)
def test_rejects_forged_or_incomplete_loop_evidence(tmp_path: Path, mutation: str) -> None:
    """Reject forged reviewer identity, returned output, worktree source, and frozen-context coverage."""
    validator, run, fixture = _bound_loop(tmp_path)
    if mutation == "forged-identity":
        ledger = json.loads((run / "loop-ledger.json").read_text(encoding="utf-8"))
        ledger["rounds"][0]["reviewer"]["identity"] = "forged-thread"
        _write_json(run / "loop-ledger.json", ledger)
        expected = "loop-evidence-reviewer-identity-mismatch"
    elif mutation == "changed-report":
        (run / "review-1.md").write_text("forged report\n", encoding="utf-8", newline="\n")
        expected = "loop-evidence-report-bytes-mismatch"
    elif mutation == "changed-source":
        repository = Path(json.loads((run / "loop-evidence.json").read_text(encoding="utf-8"))["repository"])
        (repository / "widget.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
        expected = "loop-evidence-current-source-mismatch"
    elif mutation == "recaptured-unreviewed-source":
        repository = Path(json.loads((run / "loop-evidence.json").read_text(encoding="utf-8"))["repository"])
        (repository / "widget.py").write_text("VALUE = 2\n", encoding="utf-8", newline="\n")
        current = validator.capture_source_snapshot(repository, ["widget.py"])
        (run / "current-source.json").write_bytes(validator._canonical_source_bytes(current))
        expected = "loop-evidence-final-source-mismatch"
    else:
        source_bytes = (run / "source-1.json").read_bytes()
        diff_bytes = (run / "round-1.diff").read_bytes()
        _rewrite_native_outputs(fixture, source_bytes, diff_bytes, [], full=False)
        manifest = fixture["manifest"]
        assert isinstance(manifest, dict)
        selected = manifest["passes"][0]["attempts"][0]
        (run / "review-1.md").write_bytes((run / "review" / selected["output_path"]).read_bytes())
        expected = "loop-evidence-review-context-incomplete"

    with pytest.raises(ValueError, match=expected):
        validator.validate_loop_evidence(run, fixture["sessions"])
