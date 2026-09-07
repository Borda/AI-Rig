#!/usr/bin/env python3
"""Build or verify the no-model Codex shared agentic manifest.

The manifest freezes the shared task/prompt/scoring identity before any paid Codex agentic run.  It intentionally
records no credential paths or auth material.  ``--check`` is read-only and fails closed on generated-byte drift.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
# Self-named so a rename cannot leave the stale-output hint pointing at a missing script.
REBUILD_COMMAND = f"uv run python {Path(__file__).resolve().relative_to(ROOT).as_posix()}"
MANIFESTS = BENCHMARKS / "manifests"
SOURCE_MANIFEST = MANIFESTS / "provider-parity-methodology.json"
TASKS_PATH = BENCHMARKS / "suites" / "tasks-agentic.json"
OUTPUT_MANIFEST = MANIFESTS / "codex-agentic.json"
OUTPUT_HUMAN_MANIFEST = MANIFESTS / "codex-agentic.md"
EXPERIMENT_ID = "codex-agentic"
EXPERIMENT_REVISION = "codex-agentic-verified-launcher-balanced-order-2026-09-07"
sys.path.insert(0, str(BENCHMARKS))
from _bench_common.agentic_contracts import AGENTIC_ARMS, DEFAULT_REPETITIONS, materialize_agentic_prompt  # noqa: E402
from _bench_common.provider_parity_contracts import canonical_task_hash, semantic_suite_hash  # noqa: E402


ARMS = AGENTIC_ARMS
REPETITIONS = DEFAULT_REPETITIONS
COORDINATE_TIMEOUT_SECONDS = 600


def _sha256(path: Path) -> str:
    """Return one required input's exact SHA-256 digest."""
    if not path.is_file():
        raise ValueError(f"required manifest input is missing: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object and reject another root shape."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON input: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON input must be an object: {path}")
    return payload


def _agentic_tasks() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return every raw and methodology-locked agentic row after identity checks."""
    suite = _load_json(TASKS_PATH)
    raw_tasks = suite.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("agentic suite requires a tasks list")
    if not all(isinstance(task, dict) for task in raw_tasks):
        raise ValueError("agentic suite tasks must be objects")
    source = _load_json(SOURCE_MANIFEST)
    suites = source.get("suites")
    if not isinstance(suites, list):
        raise ValueError("methodology manifest requires suites")
    locked_tasks: list[dict[str, Any]] | None = None
    suite_lock: dict[str, Any] | None = None
    for source_suite in suites:
        if source_suite.get("path") != "benchmarks/suites/tasks-agentic.json":
            continue
        locked_tasks = source_suite.get("tasks")
        suite_lock = source_suite
        break
    if not isinstance(locked_tasks, list) or suite_lock is None:
        raise ValueError("methodology manifest has no locked agentic suite")
    locked_by_id = {task.get("id"): task for task in locked_tasks if isinstance(task, dict)}
    raw_task_ids = [task.get("id") for task in raw_tasks]
    if raw_task_ids != suite_lock.get("ordered_task_ids") or set(locked_by_id) != set(raw_task_ids):
        raise ValueError("agentic task order or identity set drifted")
    for raw_task in raw_tasks:
        task_id = raw_task["id"]
        locked_task = locked_by_id[task_id]
        if canonical_task_hash(raw_task) != locked_task.get("canonical_task_sha256"):
            raise ValueError(f"{task_id} canonical task identity drifted")
        delivered_hash = hashlib.sha256(materialize_agentic_prompt(raw_task).encode("utf-8")).hexdigest()
        if delivered_hash != locked_task.get("prompt_sha256"):
            raise ValueError(f"{task_id} prompt identity drifted")
    if semantic_suite_hash(raw_tasks) != suite_lock.get("semantic_suite_sha256"):
        raise ValueError("agentic semantic suite identity drifted")
    return raw_tasks, [locked_by_id[task_id] for task_id in raw_task_ids]


def _artifact_hashes() -> dict[str, str]:
    """Lock the benchmark runner, shared scorer, plugin, and runtime bytes."""
    paths = {
        "codemap_plugin_manifest": "plugins/codemap-py/.codex-plugin/plugin.json",
        "codemap_query_skill": "plugins/codemap-py/codex-skills/query-code/SKILL.md",
        "codemap_launcher": "plugins/codemap-py/bin/codemap-py",
        "codex_rig_plugin_manifest": "plugins/codex-rig/.codex-plugin/plugin.json",
        "codex_rig_package_manifest": "plugins/codex-rig/package-manifest.json",
        "codex_rig_adapter": "plugins/codex-rig/shared/codemap_adapter.py",
        "codex_rig_contract": "plugins/codex-rig/shared/codemap-contract.md",
        "codex_agentic_runner": "benchmarks/run-codex-agentic.py",
        "codex_structural_runner": "benchmarks/run-codex-structural.py",
        "codex_structural_manifest": "benchmarks/manifests/codex-integration.json",
        "agentic_contracts": "benchmarks/_bench_common/agentic_contracts.py",
        "run_all": "benchmarks/run-all.sh",
    }
    return {name: _sha256(ROOT / relative_path) for name, relative_path in paths.items()}


def _build_manifest() -> dict[str, Any]:
    """Build one deterministic shared-suite manifest from locked repository inputs."""
    source = _load_json(SOURCE_MANIFEST)
    raw_tasks, locked_tasks = _agentic_tasks()
    suite_lock = next(suite for suite in source["suites"] if suite["path"] == "benchmarks/suites/tasks-agentic.json")
    artifact_hashes = _artifact_hashes()
    task_identities = [
        {
            "id": raw_task["id"],
            "canonical_task_sha256": locked_task["canonical_task_sha256"],
            "prompt_sha256": locked_task["prompt_sha256"],
            "oracle_class": locked_task["oracle_class"],
            "effective_scoreable": locked_task["effective_scoreable"],
            "headline_eligible_v1": locked_task["headline_eligible_v1"],
        }
        for raw_task, locked_task in zip(raw_tasks, locked_tasks, strict=True)
    ]
    cells = len(raw_tasks) * len(ARMS) * REPETITIONS
    return {
        "experiment_id": EXPERIMENT_ID,
        "experiment_revision": EXPERIMENT_REVISION,
        "schema_version": "codex-agentic-manifest-v1",
        "status": "review_ready_paid_execution_pending_human_launch",
        "model": {
            "name": "gpt-5.6-luna",
            "additional_strata": ["gpt-5.6-terra", "gpt-5.6-sol"],
            "reasoning_effort": "high",
            "strict_config": True,
        },
        "target_source": source["target_source"],
        "frozen_index_contract": {
            "path": source["index"]["path"],
            "raw_sha256": source["index"]["raw_sha256"],
            "git_sha": source["index"]["git_sha"],
            "scan_version": source["index"]["scan_version"],
            "module_count": source["index"]["module_count"],
        },
        "suite": {
            "path": "benchmarks/suites/tasks-agentic.json",
            "raw_sha256": suite_lock["raw_sha256"],
            "semantic_suite_sha256": suite_lock["semantic_suite_sha256"],
            "task_count": suite_lock["task_count"],
            "task_ids": suite_lock["ordered_task_ids"],
        },
        "tasks": task_identities,
        "arms": {
            "A_plain": {
                "codemap_available": False,
                "requirement": "No Codemap package, launcher, or Skill is available; no Codemap call is valid.",
                "pooling": "eligible only when all other admission checks pass",
            },
            "B_auto": {
                "codemap_available": True,
                "requirement": "Codemap is available through the plain CLI; use is optional and adoption is measured.",
                "no_call_valid": True,
                "pooling": "eligible only when all other admission checks pass",
            },
            "C_strict": {
                "codemap_available": True,
                "requirement": (
                    "Use the immutable installed Skill treatment for a standalone successful complete compact query "
                    "through the injected CODEMAP_BIN variable or exact installed launcher."
                ),
                "skill_path": "plugins/codemap-py/codex-skills/query-code/SKILL.md",
                "no_call_valid": False,
                "row_retained_on_noncompliance": True,
                "pooling": "ineligible when the required successful compact query is absent",
            },
        },
        "execution_order": {
            "strategy": "cyclic-arm-order-v1",
            "arm_cycle": list(ARMS),
            "index_basis": "locked-task-ordinal-plus-repetition-minus-one",
        },
        "preregistered_scope": {
            "task_ids": [task["id"] for task in raw_tasks],
            "arms": list(ARMS),
            "providers": ["codex"],
            "repetitions": REPETITIONS,
            "total_cells": cells,
            "coordinate_timeout_seconds": COORDINATE_TIMEOUT_SECONDS,
            "nonpoolable": True,
            "pooling_eligibility": "ineligible; exploratory evidence only",
            "arm_order": "cyclic task/repetition rotation; three repetitions balance each task across arm positions",
        },
        "scoring": {
            "provider": "provider_neutral_answer_contract",
            "implementation": {
                "path": "benchmarks/_bench_common/agentic_contracts.py",
                "sha256": artifact_hashes["agentic_contracts"],
                "semantic_symbol": "score_answer",
                "response_symbol": "assess_answer_response",
                "evidence_symbol": "score_evidence_metrics",
            },
            "metrics": {
                "SCORE": "mean semantic component score for each declared answer-contract field",
                "EREC": "expected-importer recall in all agent text, independent of answer-envelope validity",
                "RREC": "expected-importer recall in the final report, independent of answer-envelope validity",
                "DEFF": "unbounded expected-importer exposure hits per command",
            },
            "response_policy": (
                "A strict labelled envelope is pooling-eligible. One complete bare JSON object may be semantically "
                "recovered for diagnostic-only scoring; malformed or ambiguous responses remain semantically unscored."
            ),
        },
        "artifact_sha256": artifact_hashes,
        "plugin_runtime": {
            "codemap_version": _load_json(ROOT / "plugins/codemap-py/.codex-plugin/plugin.json")["version"],
            "codex_rig_version": _load_json(ROOT / "plugins/codex-rig/.codex-plugin/plugin.json")["version"],
            "source_hashes": artifact_hashes,
        },
        "admission": {
            "paid_execution": "admitted",
            "authorization": "caller must supply the exact reviewed machine-manifest SHA-256 as CODEX_AGENTIC_PAID_APPROVAL",
            "status": "review-ready; no model or auth used; human execution pending",
            "credentials": "no credential material, auth source, or credential path is part of this manifest",
            "required_before_paid_execution": [
                "Run --dry-run and verify the deterministic 48-cell plan.",
                "Validate target commit/tree and frozen index bytes without model or credentials.",
                "Validate A/B/C isolation and C Skill-before-query evidence without model or credentials.",
                "Caller supplies CODEX_AGENTIC_PAID_APPROVAL equal to the exact reviewed machine-manifest SHA-256.",
            ],
        },
        "artifact_package": {
            "required_files": [
                "run.log",
                "telemetry.jsonl (raw)",
                "telemetry-canonical.jsonl",
                "run-metadata.json",
                "summary.json",
                "inputs/ (frozen input snapshot)",
                "runtime-isolation.jsonl (0600 expected/observed plugin identity evidence; may be empty)",
                "checksums.sha256",
            ],
            "stop_behavior": (
                "Stop on the first runtime or admission-integrity failure; do not stop "
                "on an ordinary model, task, or treatment-nonadherence row; preserve every partial artifact and never pool "
                "partial or nonpoolable evidence."
            ),
        },
        "source_manifest": {"path": SOURCE_MANIFEST.relative_to(ROOT).as_posix(), "sha256": _sha256(SOURCE_MANIFEST)},
        "runner": "benchmarks/run-codex-agentic.py",
        "runtime_isolation": {
            "adapter": "benchmarks/run-codex-structural.py",
            "manifest": "benchmarks/manifests/codex-integration.json",
            "plugin_source_policy": (
                "Snapshot exact run-owned Codemap/Codex Rig source trees before the first paid cell; install C homes "
                "directly from those immutable paths without marketplace name resolution, and validate source bytes "
                "before every later cell."
            ),
            "mapping": {
                "A_plain": "A_plain",
                "B_auto": "B_auto capability home; use remains optional",
                "C_strict": "C_strict",
            },
        },
    }


def _json_bytes(manifest: dict[str, Any]) -> bytes:
    """Serialize the machine manifest deterministically."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _human_bytes(manifest: dict[str, Any], machine_sha256: str) -> bytes:
    """Render the concise human review record from machine content."""
    scope = manifest["preregistered_scope"]
    lines = [
        f"# `{manifest['experiment_id']}`",
        "",
        f"**Manifest SHA-256**: `{machine_sha256}`",
        "",
        "## Status",
        "",
        "- No model or credentials were used to build this manifest.",
        "- Review-ready; human execution is pending.",
        "- Paid execution is admitted only when the caller supplies the exact reviewed machine-manifest SHA-256.",
        "- The 48-cell default scope is exploratory and non-poolable.",
        "",
        "## Locked experiment",
        "",
        f"- Revision: `{manifest['experiment_revision']}`",
        f"- Model: `{manifest['model']['name']}`, effort `{manifest['model']['reasoning_effort']}`.",
        f"- Target: `{manifest['target_source']['tag']}` at `{manifest['target_source']['commit']}`.",
        f"- Frozen index SHA-256: `{manifest['frozen_index_contract']['raw_sha256']}`.",
        f"- Suite: `{manifest['suite']['path']}`; raw SHA-256 `{manifest['suite']['raw_sha256']}`.",
        "",
        "## Task identities",
        "",
        f"- Ordered task IDs: `{scope['task_ids']}`.",
        "- Each task locks canonical and prompt SHA-256 plus its provider-neutral answer contract.",
        "",
        "## Scope and arms",
        "",
        f"- Tasks: `{scope['task_ids']}`; repetitions: `{scope['repetitions']}`; arms: `{scope['arms']}`.",
        f"- Cells: `{scope['total_cells']}`; per-cell timeout: `{scope['coordinate_timeout_seconds']}s`, including retries.",
        f"- Arm order: {scope['arm_order']}. One repetition remains exploratory, not a significance test.",
        "- `A_plain`: Codemap absent; no-call is valid.",
        "- `B_auto`: Codemap CLI available; use is optional, and adoption is measured.",
        f"- `C_strict`: {manifest['arms']['C_strict']['requirement']} Noncompliant rows remain scored but are excluded from pooling.",
        "",
        "## Artifact and stop contract",
        "",
        "- Required completed package: `run.log`, raw `telemetry.jsonl`, `telemetry-canonical.jsonl`, `run-metadata.json`, `summary.json`, frozen `inputs/`, and `checksums.sha256`.",
        "- Stop on the first runtime or admission-integrity failure; ordinary model/task/treatment-nonadherence rows do not stop scheduling; preserve partial artifacts and never pool partial/nonpoolable evidence.",
        "",
        "## Shared scoring",
        "",
        "- `SCORE` is the mean semantic component score for every declared answer-contract field.",
        "- `EREC` and `RREC` are raw-text recall diagnostics independent of answer-envelope validity; `DEFF` is unbounded expected-importer exposure hits per command.",
        "- A strict labelled envelope is eligible under the response protocol. One complete bare JSON object is diagnostic-only and never poolable; malformed or ambiguous answers remain semantically unscored.",
        "",
        "## Runner",
        "",
        f"- `{manifest['runner']}`",
        "- No credential path or auth material is recorded in the machine lock; the caller supplies a private source at execution time.",
        "",
        "## Human execution and approval",
        "",
        "Review the no-model plan first:",
        "",
        "```bash",
        f"bash benchmarks/run-all.sh codex --agentic --models={manifest['model']['name']} --dry-run",
        "```",
        "",
        "Then run the exact reviewed scope; the launcher creates a fresh run directory:",
        "",
        "```bash",
        "CODEX_AGENTIC_PAID_APPROVAL=<MANIFEST_SHA256> \\",
        'CODEX_AUTH_SOURCE="$HOME/.codex/auth.json" \\',
        f"  bash benchmarks/run-all.sh codex --agentic --models={manifest['model']['name']}",
        "```",
        "",
        "Replace `<MANIFEST_SHA256>` with the machine-manifest SHA-256 shown above. The caller supplies authorization; no credential bytes are stored in this manifest.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _write_or_check(path: Path, expected: bytes, *, check: bool) -> None:
    """Write one generated artifact or fail closed when it differs."""
    if check:
        if not path.is_file() or path.read_bytes() != expected:
            raise ValueError(f"generated manifest is stale: {path}; run: {REBUILD_COMMAND}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)


def main(check: bool = False) -> None:
    """Build both manifest forms or verify their exact current bytes.

    Args:
        check: Fail with exit status 1 if the generated files are stale instead of
            rewriting them (CLI flag: ``--check``).

    Raises:
        SystemExit: With status 1 when ``check`` is set and a generated file is stale.

    Examples:
        >>> main.__name__
        'main'
    """
    manifest = _build_manifest()
    machine = _json_bytes(manifest)
    human = _human_bytes(manifest, hashlib.sha256(machine).hexdigest())
    try:
        _write_or_check(OUTPUT_MANIFEST, machine, check=check)
        _write_or_check(OUTPUT_HUMAN_MANIFEST, human, check=check)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"{'verified' if check else 'wrote'}: {OUTPUT_MANIFEST.relative_to(ROOT)}")
    print(f"{'verified' if check else 'wrote'}: {OUTPUT_HUMAN_MANIFEST.relative_to(ROOT)}")
    print(f"manifest sha256: {hashlib.sha256(machine).hexdigest()}")


if __name__ == "__main__":
    from fire import Fire

    Fire(main)
