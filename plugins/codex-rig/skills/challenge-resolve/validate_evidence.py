#!/usr/bin/env python3
"""Validate reviewer provenance and frozen source evidence for one adversarial loop.

## Purpose

Bind every recorded adversarial-review round to the exact source snapshot, diff, review route, and reviewer output that
produced its ledger findings. This closes the gap intentionally left by ``adversarial_loop.py``: that helper checks
deterministic convergence structure but cannot prove a claimed reviewer inspected the retained material.

## Scope

Read only ``loop-ledger.json``, ``loop-evidence.json``, retained source snapshots, and completed code-review runs below
one challenge-resolve run. The validator delegates route, role, child-lineage, and returned-output checks to Code
Review's existing manifest-only validator; it creates no reviewer, modifies no artifact, and makes no network call. It
supports schema-five native inspection and schema-four App Server evidence because both retain an actual reviewer thread
identifier and frozen context bytes. Older schemas are rejected rather than reimplementing their provenance rules here.

## Usage

Run ``python validate_evidence.py --out <loop-run>`` after the source collector has written ``loop-evidence.json``.
``--codex-home`` identifies the local rollout store and otherwise uses the observed ``CODEX_HOME`` or canonical
``~/.codex`` location. Nonempty review rounds require the host's active ``CODEX_THREAD_ID`` to match loop author; never
override it from candidate evidence. Import ``validate_loop_evidence`` when a parent owns argument parsing.

## Outputs

A successful CLI invocation exits zero without changing files. A contract failure exits one with one stable error code
on stderr, including mismatched current worktree source, unsupported review evidence, missing full context, conflicting
reviewer findings, or a ledger that does not exactly reflect reviewer outputs.

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


def _findings_from_report(output: Path, source_digest: str, diff_digest: str) -> list[dict[str, Any]]:
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
    if not isinstance(payload, dict) or set(payload) != {"source_sha256", "diff_sha256", "findings"}:
        raise ValueError("loop-evidence-report-block-shape-invalid")
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
    ledger_report: Path,
    ledger_findings: list[dict[str, Any]],
) -> None:
    """Validate reviewer outputs and exact context text against one frozen loop round and ledger record."""
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
    try:
        source_text = source_bytes.decode("utf-8")
        diff_text = diff_bytes.decode("utf-8")
    except UnicodeDecodeError as exception:
        raise ValueError("loop-evidence-review-context-material-not-utf8") from exception
    for item in passes:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise ValueError("loop-evidence-review-passes-invalid")
        output, pass_identity, context = selector(manifest, review_run, item["role"])
        if pass_identity == author:
            raise ValueError("loop-evidence-reviewer-is-implementation-author")
        try:
            # Newline conversion would reject intact CRLF diffs and admit altered LF diffs.
            context_text = context.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exception:
            raise ValueError("loop-evidence-review-context-unreadable") from exception
        if source_text not in context_text or diff_text not in context_text:
            raise ValueError("loop-evidence-review-context-incomplete")
        for finding in _findings_from_report(output, _sha256(source_bytes), _sha256(diff_bytes)):
            signature = finding["signature"]
            if not isinstance(signature, str) or not signature:
                raise ValueError("loop-evidence-report-findings-invalid")
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
    ledger_by_signature = {item.get("signature"): item for item in ledger_findings if isinstance(item, dict)}
    if len(ledger_by_signature) != len(ledger_findings) or merged != ledger_by_signature:
        raise ValueError("loop-evidence-report-findings-ledger-mismatch")


def validate_loop_evidence(run_dir: Path, codex_home: Path) -> None:
    """Raise ``ValueError`` unless retained loop evidence authenticates every recorded review round."""
    run = run_dir.resolve()
    evidence = _json_object(run / "loop-evidence.json", "loop-evidence-invalid")
    if set(evidence) != _EVIDENCE_KEYS or evidence.get("schema_version") != 1:
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

    ledger = _json_object(run / "loop-ledger.json", "loop-evidence-ledger-invalid")
    errors = validate_ledger(ledger)
    if errors:
        raise ValueError("loop-evidence-ledger-invalid:" + ";".join(errors))
    summary = summarize_ledger(ledger)
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
            or set(entry) != _ROUND_EVIDENCE_KEYS
            or entry.get("index") != ledger_round["index"]
        ):
            raise ValueError("loop-evidence-round-shape-invalid")
        if not isinstance(entry.get("role"), str) or not entry["role"]:
            raise ValueError("loop-evidence-round-role-invalid")
        source_path = _run_path(run, entry.get("source_path"), "loop-evidence-source-path-invalid")
        source, source_bytes = _load_source_snapshot(source_path, repository, scopes)
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
            report,
            ledger_round["findings"],
        )


def main() -> int:
    """Run the read-only loop-evidence validation command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Adversarial-loop run directory.")
    configured_home = os.environ.get("CODEX_HOME")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(configured_home) if configured_home else Path.home() / ".codex",
        help="Codex home containing rollout session logs.",
    )
    args = parser.parse_args()
    try:
        validate_loop_evidence(args.out, args.codex_home)
    except ValueError as exception:
        print(str(exception), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
