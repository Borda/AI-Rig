#!/usr/bin/env python3
"""Validate reviewer provenance and frozen source evidence for one adversarial loop.

## Purpose

Bind every recorded adversarial-review round to the exact request, source snapshot, diff, review route, and reviewer
output behind its ledger findings. This closes the gap left by ``adversarial_loop.py``: it checks deterministic
convergence structure but cannot prove a claimed reviewer inspected the retained material or that parent triage retained
every reported finding.

## Scope

Read only ``loop-ledger.json``, ``loop-evidence.json``, retained source and supporting snapshots, and completed review
runs below one challenge-resolve run. The validator delegates route, role, child-lineage, and output checks to Code
Review's existing manifest-only validator; it creates no reviewer, modifies no artifact, and makes no network call. It
supports schema-five native inspection and schema-four App Server evidence because both retain an actual reviewer thread
identifier and frozen context bytes. Loop evidence schema one remains readable with its historical request-coverage
limit; current schema two binds task criteria, declared unchanged callers or consumers, continuation lineage,
machine-readable reviewer-stated coverage, and an explicit parent mapping for every reviewer signature. Loop evidence
schema three is unsupported. Older review schemas are rejected.

## Usage

Run ``python validate_evidence.py --out <loop-run>`` after the source collector has written ``loop-evidence.json``.
Before an isolated paid review, run ``--preflight-plan <plan> --request-evidence <loop-evidence.json>
--supporting-source <supporting.json>`` to check exact frozen context delivery without a model turn.
``--codex-home`` identifies the local rollout store and otherwise uses the observed ``CODEX_HOME`` or canonical
``~/.codex`` location. Nonempty review rounds require the host's active ``CODEX_THREAD_ID`` to match loop author; never
override it from candidate evidence. Import ``validate_loop_evidence`` when a parent owns argument parsing.

## Outputs

A successful CLI invocation exits zero without changing files. A contract failure exits one with one stable error code
on stderr, including mismatched current worktree source, unsupported review evidence, missing full context, conflicting
reviewer findings, or a ledger that omits reviewer findings or changes fields outside recorded parent triage.

## Failure

Any path escaping the owning run, changed bytes, malformed evidence, substituted reviewer, absent triggered pass, or
non-identical source/diff binding raises ``ValueError``. The parent must preserve the failed evidence and obtain a fresh
permitted review rather than repairing provenance declarations after the fact.

## Used by

The challenge-resolve skill's parent-owned closure gate invokes this module before it accepts a clean review loop;
focused artifact tests exercise valid native evidence and forged identity, report, source, and context mutations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_DIRECTORY = Path(__file__).resolve().parent
PLUGIN_ROOT = SKILL_DIRECTORY.parents[1]
SHARED_DIRECTORY = PLUGIN_ROOT / "shared"
if str(SHARED_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SHARED_DIRECTORY))

from adversarial_loop import summarize_ledger, validate_ledger  # noqa: E402
from collect_diff import capture_source_snapshot  # noqa: E402


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FINDING_KEYS = {"signature", "tier", "structural", "disposition", "evidence"}
_EVIDENCE_KEYS = {"schema_version", "repository", "scope_paths", "current_source_path", "rounds"}
_ROUND_EVIDENCE_KEYS = {"index", "source_path", "review_run", "role"}
_BOUND_EVIDENCE_KEYS = _EVIDENCE_KEYS | {"request", "supporting_paths", "current_supporting_source_path"}
_CONTINUED_EVIDENCE_KEYS = _BOUND_EVIDENCE_KEYS | {"origin"}
_BOUND_ROUND_KEYS = _ROUND_EVIDENCE_KEYS | {"supporting_source_path"}
_TRIAGED_ROUND_KEYS = _BOUND_ROUND_KEYS | {"triage"}
_TRIAGE_KEYS = {"reported_signature", "signature", "tier", "reason", "evidence"}
_REQUEST_KEYS = {"goal", "specification", "done_when"}
_REPORT_BLOCK = re.compile(
    r"(?:<!-- codex-review-provenance role=[a-z-]+ run=\S+ input=[0-9a-f]{64} "
    r"context=[0-9a-f]{64} attempt=[1-9][0-9]* -->\n)?```adversarial-loop\n(.*?)\n```",
    re.DOTALL,
)


def _canonical_source_bytes(snapshot: dict[str, Any]) -> bytes:
    """Serialize source evidence with the collector's immutable portable representation."""
    return (json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    """Return one lowercase SHA-256 digest for retained opaque artifact bytes."""
    return hashlib.sha256(content).hexdigest()


def _json_object(path: Path, error: str) -> dict[str, Any]:
    """Load exactly one UTF-8 JSON object or raise the supplied stable contract error."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exception:
        raise ValueError(error) from exception
    if not isinstance(value, dict):
        raise ValueError(error)
    return value


def _run_path(run_dir: Path, raw: object, error: str) -> Path:
    """Resolve one required relative evidence path while preventing run-directory escapes."""
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ValueError(error)
    resolved = (run_dir / raw).resolve()
    try:
        resolved.relative_to(run_dir)
    except ValueError as exception:
        raise ValueError(error) from exception
    return resolved


def _review_path(review_run: Path, raw: object, error: str) -> Path:
    """Resolve a retained review path while allowing Code Review's contained absolute form."""
    if not isinstance(raw, str | Path) or not str(raw):
        raise ValueError(error)
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (review_run / path).resolve()
    try:
        resolved.relative_to(review_run)
    except ValueError as exception:
        raise ValueError(error) from exception
    return resolved


def _require_sha256(value: object, error: str) -> str:
    """Return a strict digest field or reject malformed digest declarations."""
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(error)
    return value


def _load_source_snapshot(
    path: Path, expected_repository: str, expected_scope: list[str]
) -> tuple[dict[str, Any], bytes]:
    """Load one retained source capture and bind its canonical bytes and declared coordinates."""
    snapshot = _json_object(path, "loop-evidence-source-invalid")
    bytes_value = path.read_bytes()
    if bytes_value != _canonical_source_bytes(snapshot):
        raise ValueError("loop-evidence-source-noncanonical")
    if snapshot.get("repository") != expected_repository or snapshot.get("scope_paths") != expected_scope:
        raise ValueError("loop-evidence-source-coordinate-mismatch")
    if not isinstance(snapshot.get("revision"), str) or not snapshot["revision"]:
        raise ValueError("loop-evidence-source-revision-invalid")
    return snapshot, bytes_value


def _require_supporting_source_records(snapshot: dict[str, Any], paths: list[str]) -> None:
    """Require every supporting path to have a captured file or deletion record."""
    records = snapshot.get("files")
    if not records:
        raise ValueError("loop-evidence-supporting-source-empty")
    if not isinstance(records, list) or any(
        not isinstance(record, dict)
        or not isinstance(record.get("path"), str)
        or record.get("kind") not in {"file", "symlink", "missing"}
        for record in records
    ):
        raise ValueError("loop-evidence-supporting-source-incomplete")
    recorded_paths = {record["path"] for record in records}
    if not set(paths).issubset(recorded_paths):
        raise ValueError("loop-evidence-supporting-source-incomplete")


def _current_scoped_diff(
    repository_path: Path, scopes: list[str], expected_source: dict[str, Any], capture_error: str
) -> bytes:
    """Read the current tracked patch and reject a concurrent scoped source change."""
    try:
        diff = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(repository_path),
                "--literal-pathspecs",
                "diff",
                "HEAD",
                "--binary",
                "--no-renames",
                "--",
                *scopes,
            ],
            capture_output=True,
            check=True,
        ).stdout
        if capture_source_snapshot(repository_path, scopes) != expected_source:
            raise ValueError("loop-evidence-current-source-mismatch")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exception:
        raise ValueError(capture_error) from exception
    return diff


def _check_context_material(
    context: bytes, source_bytes: bytes, diff_bytes: bytes, request_text: str | None, supporting_bytes: bytes | None
) -> None:
    """Check exact source, diff, request and supporting labels in one reviewer context."""
    try:
        context_text = context.decode("utf-8")
        source_text = source_bytes.decode("utf-8")
        diff_text = diff_bytes.decode("utf-8")
        supporting_text = supporting_bytes.decode("utf-8") if supporting_bytes is not None else None
    except UnicodeDecodeError as exception:
        raise ValueError("loop-evidence-review-context-material-not-utf8") from exception
    if (
        len(re.findall(r"(?m)^Frozen source:\n", context_text)) != 1
        or len(re.findall(r"(?m)^Frozen diff:\n", context_text)) != 1
    ):
        raise ValueError("loop-evidence-review-context-incomplete")
    supporting_labels = len(re.findall(r"(?m)^Supporting source:\n", context_text))
    if supporting_bytes is None and supporting_labels:
        raise ValueError("loop-evidence-review-context-incomplete")
    if supporting_bytes is not None and supporting_labels != 1:
        raise ValueError("loop-evidence-review-supporting-source-incomplete")
    if request_text is not None and len(re.findall(r"(?m)^Review request:\n", context_text)) != 1:
        raise ValueError("loop-evidence-review-request-incomplete")
    # Each material section must end at the next declared label (or context end).
    # Prefix matching would let reviewer-visible forged bytes follow a valid retained prefix.
    source_pattern = r"(?m)^Frozen source:\n" + re.escape(source_text) + r"\n(?=Frozen diff:\n)"
    next_label = r"Supporting source:\n|" if supporting_bytes is not None else ""
    diff_pattern = r"(?m)^Frozen diff:\n" + re.escape(diff_text) + r"\n(?=" + next_label + r"\Z)"
    if re.search(source_pattern, context_text) is None:
        raise ValueError("loop-evidence-review-context-incomplete")
    if (
        request_text is not None
        and re.search(r"(?m)^Review request:\n" + re.escape(request_text) + r"\n(?=Frozen source:\n)", context_text)
        is None
    ):
        raise ValueError("loop-evidence-review-request-incomplete")
    if (
        supporting_text is not None
        and re.search(r"(?m)^Supporting source:\n" + re.escape(supporting_text) + r"\n(?=\Z)", context_text) is None
    ):
        raise ValueError("loop-evidence-review-supporting-source-incomplete")
    if re.search(diff_pattern, context_text) is None:
        raise ValueError("loop-evidence-review-context-incomplete")
    if request_text is not None:
        # New request-bound reviews allow only the role card before evidence. Schema-one
        # reviews retain their historical preface readability but cannot promote current work.
        preface = context_text.split("Review request:\n", maxsplit=1)[0]
        challenger_card = (PLUGIN_ROOT / "roles" / "challenger" / "ROLE.md").read_text(encoding="utf-8")
        if preface not in {"", challenger_card + "\n"}:
            raise ValueError("loop-evidence-review-preface-invalid")


def _prior_findings_for_origin(origin: object, run: Path) -> dict[str, dict[str, Any]]:
    """Validate current-run lineage and load any retained prior findings."""
    if not isinstance(origin, dict) or set(origin) != {"kind", "caller_run", "prior_ledger_sha256"}:
        raise ValueError("loop-evidence-origin-invalid")
    if origin["kind"] == "first-run":
        if origin["caller_run"] is not None or origin["prior_ledger_sha256"] is not None:
            raise ValueError("loop-evidence-origin-invalid")
        return {}
    if origin["kind"] != "continuation":
        raise ValueError("loop-evidence-origin-invalid")
    caller = origin["caller_run"]
    prior_digest = origin["prior_ledger_sha256"]
    if (
        not isinstance(caller, str)
        or not caller
        or not isinstance(prior_digest, str)
        or not _SHA256.fullmatch(prior_digest)
    ):
        raise ValueError("loop-evidence-origin-invalid")
    caller_path = Path(caller)
    if not caller_path.is_absolute() or caller_path.resolve() == run or caller_path.resolve().as_posix() != caller:
        raise ValueError("loop-evidence-origin-invalid")
    try:
        prior_bytes = (caller_path / "loop-ledger.json").read_bytes()
        prior_ledger = json.loads(prior_bytes)
    except (OSError, ValueError) as exception:
        raise ValueError("loop-evidence-prior-ledger-invalid") from exception
    if (
        _sha256(prior_bytes) != prior_digest
        or not isinstance(prior_ledger, dict)
        or validate_ledger(prior_ledger)
        or not prior_ledger["rounds"]
    ):
        raise ValueError("loop-evidence-prior-ledger-invalid")
    return {item["signature"]: item for item in prior_ledger["rounds"][-1]["findings"]}


def _require_prior_request(prior_findings: dict[str, dict[str, Any]], request: dict[str, str]) -> None:
    """Require a continuation request to end with every retained final-round finding."""
    prior_block = "Prior findings to reassess:\n" + json.dumps(
        sorted(prior_findings.values(), key=lambda item: item["signature"]), sort_keys=True, ensure_ascii=True
    )
    if not request["specification"].endswith("\n" + prior_block):
        raise ValueError("loop-evidence-prior-request-incomplete")


def preflight_review_plan(plan_path: Path, request_evidence_path: Path, supporting_path: Path | None) -> None:
    """Reject a frozen challenger plan whose exact context would fail the later challenge evidence gate."""
    plan_dir = plan_path.resolve().parent
    plan = _json_object(plan_path, "loop-evidence-preflight-plan-invalid")
    evidence = _json_object(request_evidence_path, "loop-evidence-preflight-request-invalid")
    repository = evidence.get("repository")
    scopes = evidence.get("scope_paths")
    supporting_scopes = evidence.get("supporting_paths")
    request = evidence.get("request")
    if (
        type(evidence.get("schema_version")) is not int
        or evidence["schema_version"] != 2
        or not isinstance(repository, str)
        or not isinstance(scopes, list)
        or not all(isinstance(path, str) for path in scopes)
        or not isinstance(supporting_scopes, list)
        or not all(isinstance(path, str) for path in supporting_scopes)
        or not isinstance(request, dict)
        or set(request) != _REQUEST_KEYS
        or any(not isinstance(value, str) or not value.strip() for value in request.values())
    ):
        raise ValueError("loop-evidence-preflight-request-invalid")
    prior_findings = _prior_findings_for_origin(evidence.get("origin"), request_evidence_path.resolve().parent)
    if evidence["origin"]["kind"] == "continuation":
        _require_prior_request(prior_findings, request)
    source_path = _run_path(plan_dir, plan.get("source_path"), "loop-evidence-preflight-source-path-invalid")
    evidence_dir = request_evidence_path.resolve().parent
    current_source_path = _run_path(
        evidence_dir, evidence.get("current_source_path"), "loop-evidence-preflight-source-path-mismatch"
    )
    source, source_bytes = _load_source_snapshot(source_path, repository, scopes)
    try:
        _, evidence_source_bytes = _load_source_snapshot(current_source_path, repository, scopes)
    except ValueError as exception:
        raise ValueError("loop-evidence-preflight-source-path-mismatch") from exception
    if source_bytes != evidence_source_bytes:
        raise ValueError("loop-evidence-preflight-source-path-mismatch")
    if _sha256(source_bytes) != plan.get("source_sha256"):
        raise ValueError("loop-evidence-preflight-source-digest-mismatch")
    diff_path = _run_path(plan_dir, plan.get("diff_path"), "loop-evidence-preflight-diff-path-invalid")
    try:
        diff_bytes = diff_path.read_bytes()
    except OSError as exception:
        raise ValueError("loop-evidence-preflight-diff-unreadable") from exception
    if _sha256(diff_bytes) != plan.get("diff_sha256") or plan.get(
        "review_input_sha256", plan.get("diff_sha256")
    ) != plan.get("diff_sha256"):
        raise ValueError("loop-evidence-preflight-diff-digest-mismatch")
    repository_path = Path(repository)
    try:
        current_source = capture_source_snapshot(repository_path, scopes)
    except (OSError, RuntimeError, ValueError) as exception:
        raise ValueError("loop-evidence-current-source-capture-failed") from exception
    if source != current_source:
        raise ValueError("loop-evidence-current-source-mismatch")
    current_diff = _current_scoped_diff(
        repository_path, scopes, current_source, "loop-evidence-preflight-diff-capture-failed"
    )
    if diff_bytes != current_diff:
        raise ValueError("loop-evidence-preflight-diff-current-mismatch")
    supporting_bytes: bytes | None = None
    if supporting_scopes:
        if supporting_path is None:
            raise ValueError("loop-evidence-preflight-supporting-missing")
        current_supporting_path = _run_path(
            evidence_dir,
            evidence.get("current_supporting_source_path"),
            "loop-evidence-preflight-supporting-path-mismatch",
        )
        supporting, supporting_bytes = _load_source_snapshot(supporting_path, repository, supporting_scopes)
        try:
            _, evidence_supporting_bytes = _load_source_snapshot(current_supporting_path, repository, supporting_scopes)
        except ValueError as exception:
            raise ValueError("loop-evidence-preflight-supporting-path-mismatch") from exception
        if supporting_bytes != evidence_supporting_bytes:
            raise ValueError("loop-evidence-preflight-supporting-path-mismatch")
        _require_supporting_source_records(supporting, supporting_scopes)
        if supporting.get("revision") != source.get("revision"):
            raise ValueError("loop-evidence-supporting-revision-mismatch")
        try:
            current_supporting = capture_source_snapshot(repository_path, supporting_scopes)
        except (OSError, RuntimeError, ValueError) as exception:
            raise ValueError("loop-evidence-current-supporting-capture-failed") from exception
        if supporting != current_supporting:
            raise ValueError("loop-evidence-current-supporting-mismatch")
    elif supporting_path is not None or evidence.get("current_supporting_source_path") is not None:
        raise ValueError("loop-evidence-preflight-supporting-unexpected")
    nodes = plan.get("nodes")
    if (
        not isinstance(nodes, list)
        or len(nodes) != 1
        or not isinstance(nodes[0], dict)
        or nodes[0].get("role_id") != "challenger"
    ):
        raise ValueError("loop-evidence-preflight-nodes-invalid")
    node = nodes[0]
    context_path = _run_path(plan_dir, node.get("context_path"), "loop-evidence-preflight-context-path-invalid")
    try:
        context_bytes = context_path.read_bytes()
    except OSError as exception:
        raise ValueError("loop-evidence-review-context-unreadable") from exception
    if _sha256(context_bytes) != node.get("context_sha256"):
        raise ValueError("loop-evidence-preflight-context-digest-mismatch")
    _check_context_material(
        context_bytes,
        source_bytes,
        diff_bytes,
        json.dumps(request, sort_keys=True, ensure_ascii=True),
        supporting_bytes,
    )


def _validate_manifest_only(review_run: Path, codex_home: Path, parent_thread_id: str) -> dict[str, Any]:
    """Delegate route and output provenance verification to Code Review's authoritative validator."""
    validator = PLUGIN_ROOT / "skills" / "code-review" / "validate_artifacts.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--out",
            str(review_run),
            "--manifest-only",
            "--challenge-only",
            "--codex-home",
            str(codex_home),
            "--parent-thread-id",
            parent_thread_id,
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = detail[-1] if detail else "unknown"
        raise ValueError(f"loop-evidence-review-manifest-invalid:{suffix}")
    return _json_object(review_run / "specialist-manifest.json", "loop-evidence-review-manifest-invalid")


def _findings_from_report(
    output: Path, source_digest: str, diff_digest: str, coverage_paths: list[str] | None = None
) -> list[dict[str, Any]]:
    """Require one complete structured response so no finding-bearing prose is silently discarded."""
    try:
        text = output.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exception:
        raise ValueError("loop-evidence-report-unreadable") from exception
    block = _REPORT_BLOCK.fullmatch(text.strip())
    if block is None and not text.lstrip().startswith("{"):
        raise ValueError("loop-evidence-report-envelope-invalid")
    if block is not None and "\n```" in block.group(1):
        raise ValueError("loop-evidence-report-envelope-invalid")
    try:
        payload = json.loads(block.group(1) if block is not None else text)
    except json.JSONDecodeError as exception:
        raise ValueError("loop-evidence-report-block-invalid") from exception
    base_keys = {"source_sha256", "diff_sha256", "findings"}
    if not isinstance(payload, dict) or set(payload) not in (base_keys, base_keys | {"assessment"}):
        raise ValueError("loop-evidence-report-block-shape-invalid")
    if "assessment" in payload:
        assessment = payload["assessment"]
        if (
            not isinstance(assessment, dict)
            or set(assessment) != {"rating", "rationale"}
            or type(assessment["rating"]) is not int
            or assessment["rating"] not in range(1, 6)
            or not isinstance(assessment["rationale"], str)
            or not assessment["rationale"].strip()
        ):
            raise ValueError("loop-evidence-report-assessment-invalid")
    if coverage_paths is not None:
        assessment = payload.get("assessment")
        rationale = assessment.get("rationale", "") if isinstance(assessment, dict) else ""
        try:
            coverage = json.loads(rationale)
        except json.JSONDecodeError as exception:
            raise ValueError("loop-evidence-report-coverage-missing") from exception
        if (
            not isinstance(coverage, dict)
            or set(coverage) != {"reviewed_paths", "unreviewed_paths", "limits", "judgment"}
            or coverage["reviewed_paths"] != sorted(set(coverage_paths))
            or coverage["unreviewed_paths"] != []
            or not isinstance(coverage["limits"], str)
            or not coverage["limits"].strip()
            or not isinstance(coverage["judgment"], str)
            or not coverage["judgment"].strip()
        ):
            raise ValueError("loop-evidence-report-coverage-missing")
    if payload["source_sha256"] != source_digest or payload["diff_sha256"] != diff_digest:
        raise ValueError("loop-evidence-report-block-digest-mismatch")
    findings = payload["findings"]
    if not isinstance(findings, list) or any(
        not isinstance(item, dict) or set(item) != _FINDING_KEYS for item in findings
    ):
        raise ValueError("loop-evidence-report-findings-invalid")
    return findings


def _selected_native_pass(manifest: dict[str, Any], review_run: Path, role: str) -> tuple[Path, str, Path]:
    """Return selected schema-five output, reviewer thread, and frozen context for one exact role."""
    if manifest.get("schema_version") != 5:
        raise ValueError("loop-evidence-review-schema-unsupported")
    passes = manifest.get("passes")
    if not isinstance(passes, list):
        raise ValueError("loop-evidence-review-passes-invalid")
    by_role = {item.get("role"): item for item in passes if isinstance(item, dict)}
    selected = by_role.get(role)
    if len(by_role) != len(passes) or not isinstance(selected, dict) or selected.get("mode") != "inspection":
        raise ValueError("loop-evidence-review-role-invalid")
    attempts = selected.get("attempts")
    position = selected.get("selected_attempt")
    if not isinstance(attempts, list) or not isinstance(position, int) or not 1 <= position <= len(attempts):
        raise ValueError("loop-evidence-review-selected-attempt-invalid")
    attempt = attempts[position - 1]
    if not isinstance(attempt, dict) or not isinstance(attempt.get("agent_thread_id"), str):
        raise ValueError("loop-evidence-review-selected-attempt-invalid")
    return (
        _run_path(review_run, attempt.get("output_path"), "loop-evidence-review-output-path-invalid"),
        attempt["agent_thread_id"],
        _run_path(review_run, attempt.get("context_path"), "loop-evidence-review-context-path-invalid"),
    )


def _selected_app_server_pass(manifest: dict[str, Any], review_run: Path, role: str) -> tuple[Path, str, Path]:
    """Return selected schema-four output, observed thread, and frozen context for one exact role."""
    if manifest.get("schema_version") != 4:
        raise ValueError("loop-evidence-review-schema-unsupported")
    execution = manifest.get("app_server_execution")
    if not isinstance(execution, dict):
        raise ValueError("loop-evidence-review-app-server-invalid")
    evidence_path = _review_path(review_run, execution.get("evidence_path"), "loop-evidence-review-app-server-invalid")
    evidence = _json_object(evidence_path, "loop-evidence-review-app-server-invalid")
    nodes = evidence.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("loop-evidence-review-app-server-invalid")
    matches = [node for node in nodes if isinstance(node, dict) and node.get("role_id") == role]
    if len(matches) != 1 or not isinstance(matches[0].get("thread_id"), str):
        raise ValueError("loop-evidence-review-role-invalid")
    node = matches[0]
    plan_path = _review_path(review_run, execution.get("plan_path"), "loop-evidence-review-app-server-invalid")
    return (
        _review_path(
            review_run,
            evidence_path.parent / str(node.get("output_path", "")),
            "loop-evidence-review-output-path-invalid",
        ),
        node["thread_id"],
        _review_path(
            review_run,
            plan_path.parent / str(node.get("context_path", "")),
            "loop-evidence-review-context-path-invalid",
        ),
    )


def _validate_review_round(
    review_run: Path,
    codex_home: Path,
    expected_input_digest: str,
    selected_role: str,
    author: str,
    ledger_reviewer_identity: object,
    source_bytes: bytes,
    diff_bytes: bytes,
    request_text: str | None,
    supporting_bytes: bytes | None,
    ledger_report: Path,
    ledger_findings: list[dict[str, Any]],
    triage: list[dict[str, Any]] | None,
    coverage_paths: list[str] | None = None,
) -> None:
    """Bind one frozen review to its ledger, applying explicit parent identity and severity triage when present."""
    preflight_manifest = _json_object(review_run / "specialist-manifest.json", "loop-evidence-review-manifest-invalid")
    parent_thread_id = preflight_manifest.get("parent_thread_id")
    if not isinstance(parent_thread_id, str) or not parent_thread_id:
        raise ValueError("loop-evidence-review-parent-invalid")
    if parent_thread_id != author:
        raise ValueError("loop-evidence-implementation-author-mismatch")
    manifest = _validate_manifest_only(review_run, codex_home, parent_thread_id)
    if manifest.get("review_input_sha256") != expected_input_digest:
        raise ValueError("loop-evidence-review-input-digest-mismatch")
    if (review_run / "diff.patch").read_bytes() != diff_bytes:
        raise ValueError("loop-evidence-review-diff-mismatch")
    schema = manifest.get("schema_version")
    if schema == 5:
        selected_output, reviewer_identity, _ = _selected_native_pass(manifest, review_run, selected_role)
        selector = _selected_native_pass
    elif schema == 4:
        selected_output, reviewer_identity, _ = _selected_app_server_pass(manifest, review_run, selected_role)
        selector = _selected_app_server_pass
    else:
        raise ValueError("loop-evidence-review-schema-unsupported")
    if reviewer_identity == author:
        raise ValueError("loop-evidence-reviewer-is-implementation-author")
    if ledger_reviewer_identity != reviewer_identity:
        raise ValueError("loop-evidence-reviewer-identity-mismatch")
    if ledger_report.read_bytes() != selected_output.read_bytes():
        raise ValueError("loop-evidence-report-bytes-mismatch")

    passes = manifest.get("passes")
    if not isinstance(passes, list) or not passes:
        raise ValueError("loop-evidence-review-passes-invalid")
    merged: dict[str, dict[str, Any]] = {}
    for item in passes:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise ValueError("loop-evidence-review-passes-invalid")
        output, pass_identity, context = selector(manifest, review_run, item["role"])
        if pass_identity == author:
            raise ValueError("loop-evidence-reviewer-is-implementation-author")
        try:
            # Newline conversion would reject intact CRLF diffs and admit altered LF diffs.
            context_bytes = context.read_bytes()
        except OSError as exception:
            raise ValueError("loop-evidence-review-context-unreadable") from exception
        _check_context_material(context_bytes, source_bytes, diff_bytes, request_text, supporting_bytes)
        reported_signatures: set[str] = set()
        for finding in _findings_from_report(output, _sha256(source_bytes), _sha256(diff_bytes), coverage_paths):
            signature = finding["signature"]
            if not isinstance(signature, str) or not signature:
                raise ValueError("loop-evidence-report-findings-invalid")
            if triage is not None and signature in reported_signatures:
                raise ValueError("loop-evidence-triage-raw-duplicate")
            reported_signatures.add(signature)
            evidence = finding["evidence"]
            if (
                not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(entry, str) or not entry.strip() for entry in evidence)
            ):
                raise ValueError("loop-evidence-report-findings-invalid")
            prior = merged.get(signature)
            if prior is not None:
                if any(prior[key] != finding[key] for key in ("tier", "structural", "disposition")):
                    raise ValueError(f"loop-evidence-report-finding-conflict:{signature}")
                # Corroborating reviewers may supply different proof for the same verdict.
                # Preserve exact strings in first-seen order; never choose one reviewer's evidence.
                prior["evidence"] = list(dict.fromkeys([*prior["evidence"], *evidence]))
            else:
                merged[signature] = finding
    if triage is not None:
        if not isinstance(triage, list):
            raise ValueError("loop-evidence-triage-invalid")
        mapped: dict[str, dict[str, Any]] = {}
        seen_raw: set[str] = set()
        for item in triage:
            if not isinstance(item, dict) or set(item) != _TRIAGE_KEYS:
                raise ValueError("loop-evidence-triage-invalid")
            reported_signature = item["reported_signature"]
            signature = item["signature"]
            tier = item["tier"]
            reason = item["reason"]
            proof = item["evidence"]
            if (
                not isinstance(reported_signature, str)
                or not reported_signature.strip()
                or not isinstance(signature, str)
                or not signature.strip()
                or not isinstance(tier, str)
                or not isinstance(reason, str)
                or not isinstance(proof, list)
                or any(not isinstance(value, str) or not value.strip() for value in proof)
            ):
                raise ValueError("loop-evidence-triage-invalid")
            if reported_signature in seen_raw:
                raise ValueError("loop-evidence-triage-raw-duplicate")
            seen_raw.add(reported_signature)
            if signature in mapped:
                raise ValueError("loop-evidence-triage-signature-duplicate")
            raw = merged.get(reported_signature)
            if raw is None:
                raise ValueError("loop-evidence-triage-coverage-invalid")
            corrected = signature != reported_signature or tier != raw["tier"]
            if corrected and (not reason.strip() or not proof):
                raise ValueError("loop-evidence-triage-correction-proof-required")
            if not corrected and (reason or proof):
                raise ValueError("loop-evidence-triage-unnecessary-proof")
            mapped[signature] = {**raw, "signature": signature, "tier": tier}
        if seen_raw != merged.keys():
            raise ValueError("loop-evidence-triage-coverage-invalid")
        merged = mapped
    ledger_by_signature = {item.get("signature"): item for item in ledger_findings if isinstance(item, dict)}
    if len(ledger_by_signature) != len(ledger_findings) or merged != ledger_by_signature:
        raise ValueError("loop-evidence-report-findings-ledger-mismatch")


def validate_loop_evidence(run_dir: Path, codex_home: Path) -> None:
    """Raise ``ValueError`` unless retained loop evidence authenticates every recorded review round."""
    run = run_dir.resolve()
    evidence = _json_object(run / "loop-evidence.json", "loop-evidence-invalid")
    version = evidence.get("schema_version")
    if (
        (version == 1 and set(evidence) != _EVIDENCE_KEYS)
        or (version == 2 and set(evidence) not in (_BOUND_EVIDENCE_KEYS, _CONTINUED_EVIDENCE_KEYS))
        or type(version) is not int
        or version not in (1, 2)
    ):
        raise ValueError("loop-evidence-invalid")
    repository = evidence.get("repository")
    scopes = evidence.get("scope_paths")
    if (
        not isinstance(repository, str)
        or not isinstance(scopes, list)
        or not all(isinstance(item, str) for item in scopes)
    ):
        raise ValueError("loop-evidence-invalid")
    repository_path = Path(repository)
    if not repository_path.is_absolute() or repository_path.resolve().as_posix() != repository:
        raise ValueError("loop-evidence-repository-invalid")
    current_path = _run_path(run, evidence.get("current_source_path"), "loop-evidence-current-source-path-invalid")
    current, current_bytes = _load_source_snapshot(current_path, repository, scopes)
    try:
        actual = capture_source_snapshot(repository_path, scopes)
    except (OSError, RuntimeError, ValueError) as exception:
        raise ValueError("loop-evidence-current-source-capture-failed") from exception
    if current != actual:
        raise ValueError("loop-evidence-current-source-mismatch")
    request_text: str | None = None
    supporting_paths: list[str] = []
    current_supporting_bytes: bytes | None = None
    if version == 2:
        request = evidence["request"]
        supporting_paths = evidence["supporting_paths"]
        if (
            not isinstance(request, dict)
            or set(request) != _REQUEST_KEYS
            or any(not isinstance(value, str) or not value.strip() for value in request.values())
            or not isinstance(supporting_paths, list)
            or any(not isinstance(path, str) or not path for path in supporting_paths)
            or len(set(supporting_paths)) != len(supporting_paths)
        ):
            raise ValueError("loop-evidence-request-invalid")
        request_text = json.dumps(request, sort_keys=True, ensure_ascii=True)
        if supporting_paths:
            path = _run_path(run, evidence["current_supporting_source_path"], "loop-evidence-supporting-path-invalid")
            supporting, current_supporting_bytes = _load_source_snapshot(path, repository, supporting_paths)
            _require_supporting_source_records(supporting, supporting_paths)
            try:
                actual_supporting = capture_source_snapshot(repository_path, supporting_paths)
            except (OSError, RuntimeError, ValueError) as exception:
                raise ValueError("loop-evidence-current-supporting-capture-failed") from exception
            if supporting != actual_supporting:
                raise ValueError("loop-evidence-current-supporting-mismatch")
        elif evidence["current_supporting_source_path"] is not None:
            raise ValueError("loop-evidence-supporting-path-invalid")

    ledger = _json_object(run / "loop-ledger.json", "loop-evidence-ledger-invalid")
    errors = validate_ledger(ledger)
    if errors:
        raise ValueError("loop-evidence-ledger-invalid:" + ";".join(errors))
    summary = summarize_ledger(ledger)
    has_origin = "origin" in evidence
    if has_origin:
        prior_findings = _prior_findings_for_origin(evidence["origin"], run)
        if evidence["origin"]["kind"] == "continuation":
            # With no review there is no verdict to check; the bound prior findings stay open.
            if ledger["rounds"]:
                current_findings = {item["signature"]: item for item in ledger["rounds"][-1]["findings"]}
                for signature, prior in prior_findings.items():
                    current_finding = current_findings.get(signature)
                    if (
                        current_finding is None
                        or current_finding["tier"] != prior["tier"]
                        or current_finding["structural"] != prior["structural"]
                        or (
                            summary["reason"] == "clean"
                            and current_finding["disposition"] not in {"verified-fixed", "rejected"}
                        )
                    ):
                        raise ValueError(f"loop-evidence-prior-signature-unresolved:{signature}")
            _require_prior_request(prior_findings, evidence["request"])
    if current.get("revision") != ledger["current_snapshot"]["revision"]:
        raise ValueError("loop-evidence-current-source-revision-mismatch")
    rounds = ledger["rounds"]
    entries = evidence.get("rounds")
    if not isinstance(entries, list) or len(entries) != len(rounds):
        raise ValueError("loop-evidence-round-count-mismatch")
    author = ledger["implementation_author"]
    assert isinstance(author, str)
    if rounds:
        active_owner = os.environ.get("CODEX_THREAD_ID", "")
        if not active_owner.strip():
            raise ValueError("loop-evidence-active-owner-missing")
        if active_owner != author:
            raise ValueError("loop-evidence-active-owner-mismatch")
    for ledger_round, entry in zip(rounds, entries, strict=True):
        if (
            not isinstance(entry, dict)
            or set(entry) != (_TRIAGED_ROUND_KEYS if version == 2 else _ROUND_EVIDENCE_KEYS)
            or entry.get("index") != ledger_round["index"]
        ):
            raise ValueError("loop-evidence-round-shape-invalid")
        if entry.get("role") != "challenger":
            raise ValueError("loop-evidence-round-role-invalid")
        source_path = _run_path(run, entry.get("source_path"), "loop-evidence-source-path-invalid")
        source, source_bytes = _load_source_snapshot(source_path, repository, scopes)
        supporting_bytes: bytes | None = None
        if supporting_paths:
            supporting_path = _run_path(run, entry["supporting_source_path"], "loop-evidence-supporting-path-invalid")
            supporting, supporting_bytes = _load_source_snapshot(supporting_path, repository, supporting_paths)
            _require_supporting_source_records(supporting, supporting_paths)
            if supporting["revision"] != source["revision"]:
                raise ValueError("loop-evidence-supporting-revision-mismatch")
            if (
                summary["reason"] == "clean"
                and ledger_round is rounds[-1]
                and supporting_bytes != current_supporting_bytes
            ):
                raise ValueError("loop-evidence-final-supporting-mismatch")
        elif version == 2 and entry["supporting_source_path"] is not None:
            raise ValueError("loop-evidence-supporting-path-invalid")
        if source.get("revision") != ledger_round["snapshot"]["revision"]:
            raise ValueError("loop-evidence-source-revision-mismatch")
        if summary["reason"] == "clean" and ledger_round is rounds[-1] and source_bytes != current_bytes:
            raise ValueError("loop-evidence-final-source-mismatch")
        diff_path = run / f"round-{ledger_round['index']}.diff"
        if not diff_path.is_file():
            raise ValueError("loop-evidence-round-diff-missing")
        diff_bytes = diff_path.read_bytes()
        digest = _require_sha256(ledger_round["snapshot"].get("diff_digest"), "loop-evidence-round-digest-invalid")
        if _sha256(diff_bytes) != digest:
            raise ValueError("loop-evidence-round-diff-digest-mismatch")
        review_run = _run_path(run, entry.get("review_run"), "loop-evidence-review-run-path-invalid")
        report = _run_path(run, ledger_round.get("report_path"), "loop-evidence-report-path-invalid")
        _validate_review_round(
            review_run,
            codex_home.resolve(),
            digest,
            entry["role"],
            author,
            ledger_round["reviewer"].get("identity"),
            source_bytes,
            diff_bytes,
            request_text,
            supporting_bytes,
            report,
            ledger_round["findings"],
            entry["triage"] if version == 2 else None,
            sorted(set([*scopes, *supporting_paths])) if has_origin else None,
        )
        if version == 2 and summary["reason"] == "clean" and ledger_round is rounds[-1]:
            current_diff = _current_scoped_diff(
                repository_path, scopes, actual, "loop-evidence-final-diff-capture-failed"
            )
            if diff_bytes != current_diff:
                raise ValueError("loop-evidence-final-diff-current-mismatch")


def main() -> int:
    """Run the read-only loop-evidence validation command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", type=Path, help="Adversarial-loop run directory.")
    mode.add_argument(
        "--preflight-plan", type=Path, help="Frozen isolated reviewer plan, checked without a model turn."
    )
    parser.add_argument(
        "--request-evidence", type=Path, help="Bound loop-evidence JSON holding review request and scope."
    )
    parser.add_argument("--supporting-source", type=Path, help="Frozen supporting source JSON, when declared.")
    configured_home = os.environ.get("CODEX_HOME")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(configured_home) if configured_home else Path.home() / ".codex",
        help="Codex home containing rollout session logs.",
    )
    args = parser.parse_args()
    if args.preflight_plan is not None and args.request_evidence is None:
        parser.error("--preflight-plan requires --request-evidence")
    if args.out is not None and (args.request_evidence is not None or args.supporting_source is not None):
        parser.error("--request-evidence and --supporting-source require --preflight-plan")
    try:
        if args.preflight_plan is not None:
            preflight_review_plan(args.preflight_plan, args.request_evidence, args.supporting_source)
        else:
            validate_loop_evidence(args.out, args.codex_home)
    except ValueError as exception:
        print(str(exception), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
