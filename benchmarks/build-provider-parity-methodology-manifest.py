#!/usr/bin/env python3
"""Build or verify the provider-neutral benchmark methodology lock.

This utility derives the current lock from the committed policy seed and task suites. Test fixtures cover malformed or
adversarial cases separately; prior benchmark runs are not inputs to the current methodology. ``--check`` writes nothing
and fails closed on byte drift.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
# Self-named so a rename cannot leave the stale-output hint pointing at a missing script.
REBUILD_COMMAND = f"uv run python {Path(__file__).resolve().relative_to(ROOT).as_posix()}"
OUTPUT_MANIFEST = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
POLICY_SEED = BENCHMARKS / "policy" / "provider-parity-methodology.json"
POLICY_SEED_SHA256 = "1e5b1cad389513db9402ca2da39f58c1ff9b7cb36b0fdc4a23ce03886e12f1f1"
EXPERIMENT_REVISION = "provider-parity-agentic-nested-package-imports-2026-09-09"
TASKS_BENCH = "benchmarks/suites/tasks-bench.json"
TASKS_AGENTIC = "benchmarks/suites/tasks-agentic.json"
# Root temp dir, not the per-user one: patch-index-locks.json locks canonical_scan_root
# to /private/tmp/codemap-provider-parity-pl-2.6.5. tempfile.gettempdir() honours $TMPDIR
# and would name a different directory.
CANONICAL_TARGET = Path(
    os.environ.get("CODEMAP_PARITY_REPO")
    or f"{os.sep}tmp{os.sep}codemap-provider-parity-pl-2.6.5"  # portable-paths: canonical-target
).resolve()
INDEX_LOCK = {
    "change_reason": (
        "Scanner schema 13 preserves static reverse-import edges for relative imports and known from-package "
        "submodule imports; production-only centrality remains derived from production importers."
    ),
    "git_sha": "be98784a1a03581b7051a355ae1084fd352d7cea",
    "module_count": 645,
    "path": str(CANONICAL_TARGET / ".cache/codemap/codemap-provider-parity-pl-2.6.5.json"),
    "project": "codemap-provider-parity-pl-2.6.5",
    "raw_sha256": "3c5840893e9c939baa61a6c5ce95994ff69ffe4a67d225aeb412c73deb61e0c1",
    "semantic_sha256": "4086690e2b7bed8ff4fc95ed37606228caa236200ccad2e9d8c5dfb9dda1062e",
    "scan_root": str(CANONICAL_TARGET),
    "scan_version": 13,
    "scanned_at": "2026-08-06T09:04:54.432797+00:00",
}
PRODUCT_ACCEPTANCE_POLICY = {
    "efficiency_path": {
        "c_a_gross_input_ratio_95_upper": "< 1.00",
        "c_a_paired_quality_mean": ">= 0.00",
        "c_a_paired_quality_95_lower": ">= -0.02",
    },
    "quality_path": {
        "c_a_gross_input_ratio_95_upper": "<= 1.05",
        "c_a_paired_quality_95_lower": "> 0",
    },
    "task_family_block": "Any repeated task-family mean C_strict-A_plain quality difference < -0.10 blocks acceptance.",
    "historical_evidence": "Historical nonpoolable evidence cannot satisfy this prospective policy.",
}

# These descriptions are current suite metadata, not snapshots of a previous
# run. Task IDs, ordering, hashes, and policy rows are rebuilt from each suite.
SUITE_METADATA: dict[str, dict[str, str]] = {
    TASKS_AGENTIC: {
        "current_consumer": "run-claude-agentic.py and run-codex-agentic.py defaults",
        "generation_provenance": "committed_static_prompts_runtime_ast_oracle",
        "root_shape": "object_with_tasks",
    },
    TASKS_BENCH: {
        "current_consumer": "run-claude-structural.py and run-codex-structural.py defaults",
        "generation_provenance": "committed_prompts_and_ground_truth; generate-tasks-bench.py validates_or_updates_ground_truth_only",
        "root_shape": "object_with_tasks",
    },
    "benchmarks/suites/tasks-code.json": {
        "current_consumer": "run-cli.py; optional external input to run-claude-structural.py",
        "generation_provenance": "committed_cli_query_specs_without_materialized_llm_ground_truth",
        "root_shape": "bare_list",
    },
    "benchmarks/suites/tasks-fix-multi.json": {
        "current_consumer": "run-codex-structural.py task family FM; run-claude-agentic.py --study fix-multi",
        "generation_provenance": "committed_static_prompts_external_complete_caller_oracle",
        "root_shape": "bare_list",
    },
    "benchmarks/suites/tasks-fix-single.json": {
        "current_consumer": "run-codex-structural.py task family FS; run-claude-agentic.py --study fix-single",
        "generation_provenance": "committed_static_prompts_external_executable_oracle",
        "root_shape": "bare_list",
    },
    "benchmarks/suites/tasks-patch.json": {
        "current_consumer": "run-codex-structural.py task family PT; run-claude-agentic.py --study patch",
        "generation_provenance": "committed_real_issue_prompts_hidden_reference_patch_test_fixture_and_executable_oracle",
        "root_shape": "object_with_tasks",
    },
    "benchmarks/suites/tasks-readcrop.json": {
        "current_consumer": "run-codex-structural.py task family RC; run-claude-agentic.py --study readcrop",
        "generation_provenance": "committed_static_prompts_source_validated_answer_contract",
        "root_shape": "bare_list",
    },
}
STATIC_REFERENCE_TYPES = frozenset({"symbol_extraction", "real_issue"})

sys.path.insert(0, str(BENCHMARKS))
import _bench_common.provider_parity_contracts as core  # noqa: E402


def _sha256(path: Path) -> str:
    """Return one required file's exact SHA-256 identity."""
    if not path.is_file():
        raise ValueError(f"required methodology input is missing or not a file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    """Load one required JSON object."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid methodology JSON input: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"methodology JSON input must be an object: {path}")
    return payload


def _load_policy_seed() -> dict[str, Any]:
    """Load the immutable policy without trusting generated output."""
    if _sha256(POLICY_SEED) != POLICY_SEED_SHA256:
        raise ValueError("immutable methodology policy seed SHA-256 changed")
    policy = _load_json(POLICY_SEED)
    expected_fields = {
        "codex_permission_profiles",
        "evaluation_contract",
        "execution_controls",
        "headline_structural_v1",
        "implementation_contract",
        "oracle_remediation",
        "preregistered_cells",
        "target_source",
        "validation",
    }
    if set(policy) != expected_fields:
        raise ValueError("methodology policy seed has unexpected passthrough fields")
    return policy


def _current_suite_tasks(path: str) -> list[dict[str, Any]]:
    """Load one committed suite using the shared raw-task contract."""
    return core.load_task_suite(ROOT / path)


def _task_row(
    source_task: Mapping[str, Any],
    suite_path: str,
    headline_ids: set[str],
    diagnostic_ids: set[str],
    diagnostic_reason: str,
) -> dict[str, Any]:
    """Derive one current policy row from a raw committed task."""
    task_id = source_task["id"]
    raw_type = source_task.get("type")
    raw_scoreable = source_task.get("scoreable")
    self_consistency = False

    if suite_path == TASKS_BENCH:
        if not isinstance(raw_type, str) or not raw_type:
            raise ValueError(f"tasks-bench task {task_id!r} requires type")
        effective_type = raw_type
        effective_scoreable = raw_scoreable if isinstance(raw_scoreable, bool) else True
        self_consistency = bool(source_task.get("self_consistency", False))
        if not effective_scoreable:
            oracle_class = "unscoreable"
        elif self_consistency:
            oracle_class = "self_consistency"
        elif raw_type in STATIC_REFERENCE_TYPES:
            oracle_class = "static_reference"
        else:
            oracle_class = "independent"
        validation_status = "pass"
    elif suite_path.endswith("tasks-code.json"):
        effective_type = "develop_skill"
        effective_scoreable = False
        oracle_class = "unscoreable"
        validation_status = "not_in_tasks_bench_validator"
    else:
        if not isinstance(raw_type, str) or not raw_type:
            raise ValueError(f"task {task_id!r} in {suite_path} requires type")
        effective_type = raw_type
        effective_scoreable = True
        oracle_class = "independent" if suite_path == TASKS_AGENTIC else "static_reference"
        validation_status = "not_in_tasks_bench_validator"

    row: dict[str, Any] = {
        "difficulty": source_task.get("difficulty"),
        "effective_scoreable": effective_scoreable,
        "effective_type": effective_type,
        "headline_eligible_v1": task_id in headline_ids,
        "id": task_id,
        "oracle_class": oracle_class,
        "profiles": copy.deepcopy(source_task.get("profiles", [])),
        "providers": ["claude", "codex"],
        "raw_scoreable": raw_scoreable,
        "raw_type": raw_type,
        "self_consistency": self_consistency,
        "validation_status": validation_status,
        "canonical_task_sha256": core.canonical_task_hash(source_task),
        "prompt_sha256": core.prompt_hash(source_task),
    }
    if "answer_contract" in source_task:
        row["answer_contract"] = copy.deepcopy(source_task["answer_contract"])
    if task_id in diagnostic_ids:
        row["oracle_limit"] = diagnostic_reason
    return row


def _build_suites(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build current suite metadata and policy rows from committed task inputs."""
    headline_ids = set(policy["headline_structural_v1"]["task_ids"])
    diagnostic_ids = set(policy["headline_structural_v1"]["diagnostic_independent_ids"])
    diagnostic_reason = policy["headline_structural_v1"]["diagnostic_reason"]
    suites: list[dict[str, Any]] = []
    for path, metadata in SUITE_METADATA.items():
        source_tasks = _current_suite_tasks(path)
        task_ids = [task["id"] for task in source_tasks]
        rows = [_task_row(task, path, headline_ids, diagnostic_ids, diagnostic_reason) for task in source_tasks]
        suites.append(
            {
                **metadata,
                "path": path,
                "ordered_task_ids": task_ids,
                "raw_sha256": _sha256(ROOT / path),
                "semantic_suite_sha256": core.semantic_suite_hash(source_tasks),
                "task_count": len(rows),
                "tasks": rows,
            }
        )
    bench_ids = {task["id"] for task in _current_suite_tasks(TASKS_BENCH)}
    if not headline_ids <= bench_ids:
        raise ValueError("headline policy names a task outside tasks-bench.json")
    return suites


def _artifact_hashes() -> dict[str, str]:
    """Lock shared provider-neutral implementation bytes used by both runners."""
    paths = {
        "agentic_contracts": "benchmarks/_bench_common/agentic_contracts.py",
        "agentic_reporting": "benchmarks/_bench_common/agentic_reporting.py",
        "claude_query_skill": "plugins/codemap-py/claude-skills/query-code/SKILL.md",
        "codemap_graph": "plugins/codemap-py/src/codemap_py/graph.py",
        "codemap_query": "plugins/codemap-py/src/codemap_py/query.py",
        "codex_query_skill": "plugins/codemap-py/codex-skills/query-code/SKILL.md",
        "provider_parity_contracts": "benchmarks/_bench_common/provider_parity_contracts.py",
        "edit_patch_contracts": "benchmarks/_bench_common/edit_patch_contracts.py",
        "mutation_isolation": "benchmarks/_bench_common/mutation_isolation.py",
        "paid_lifecycle": "benchmarks/_bench_common/paid_lifecycle.py",
        "presentation": "benchmarks/_bench_common/presentation.py",
        "patch_index_locks": "benchmarks/suites/patch-index-locks.json",
        "run_all": "benchmarks/run-all.sh",
        "run_claude_agentic": "benchmarks/run-claude-agentic.py",
        "run_claude_structural": "benchmarks/run-claude-structural.py",
        "run_codex_agentic": "benchmarks/run-codex-agentic.py",
        "run_codex_structural": "benchmarks/run-codex-structural.py",
    }
    return {name: _sha256(ROOT / relative_path) for name, relative_path in paths.items()}


def _suite_integrity(suites: list[dict[str, Any]]) -> dict[str, Any]:
    """Return recomputed global cardinality, uniqueness, and semantic identities."""
    tasks = [task for suite in suites for task in suite["tasks"]]
    task_ids = [task["id"] for task in tasks]
    canonical_hashes = [task["canonical_task_sha256"] for task in tasks]
    return {
        "all_tasks_have_id_and_prompt": True,
        "semantic_suite_sha256": {suite["path"]: suite["semantic_suite_sha256"] for suite in suites},
        "suite_count": len(suites),
        "task_count": len(tasks),
        "unique_canonical_task_hashes": len(canonical_hashes) == len(set(canonical_hashes)),
        "unique_ids_within_and_across_suites": len(task_ids) == len(set(task_ids)),
    }


def _evaluation_contract(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute capability strata counts from the current structural suite."""
    contract = copy.deepcopy(policy["evaluation_contract"])
    tasks = _current_suite_tasks(TASKS_BENCH)
    counts = Counter(stratum for task in tasks for stratum in core.capability_strata(task))
    contract["capability_strata_counts"] = dict(sorted(counts.items()))
    contract["prospective_product_acceptance"] = copy.deepcopy(PRODUCT_ACCEPTANCE_POLICY)
    return contract


def _structural_execution_cells(policy: Mapping[str, Any], suites: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Derive the frozen execution and diagnostic selections from current structural rows.

    The five real-issue rows remain outside the provider-neutral structural execution suite. Every other committed
    structural row is included in suite order, with the headline policy partitioning the ten diagnostics.
    """
    headline_ids = list(policy["headline_structural_v1"]["task_ids"])
    headline_id_set = set(headline_ids)
    structural_suite = next(suite for suite in suites if suite["path"] == TASKS_BENCH)
    structural_tasks = structural_suite["tasks"]
    execution_ids = [task["id"] for task in structural_tasks if task["effective_type"] != "real_issue"]
    diagnostic_ids = [task_id for task_id in execution_ids if task_id not in headline_id_set]
    excluded_ids = [task["id"] for task in structural_tasks if task["effective_type"] == "real_issue"]

    if len(headline_ids) != 45 or len(headline_id_set) != len(headline_ids):
        raise ValueError("headline structural policy must contain 45 unique task IDs")
    if excluded_ids != ["RI-01", "RI-02", "RI-03", "RI-04", "RI-05"]:
        raise ValueError("structural execution must exclude only RI-01 through RI-05")
    if len(execution_ids) != 55 or len(diagnostic_ids) != 10:
        raise ValueError("structural execution must contain 55 IDs with 10 diagnostics")
    if not headline_id_set <= set(execution_ids):
        raise ValueError("structural execution must include every headline task")
    if set(execution_ids) != headline_id_set | set(diagnostic_ids):
        raise ValueError("structural execution must partition headline and diagnostic IDs")
    return execution_ids, diagnostic_ids


def _build_manifest() -> dict[str, Any]:
    """Build the only accepted current provider-neutral methodology record."""
    policy = _load_policy_seed()
    suites = _build_suites(policy)
    manifest = copy.deepcopy(policy)
    execution_ids, diagnostic_ids = _structural_execution_cells(policy, suites)
    manifest["preregistered_cells"]["structural_execution_task_ids"] = execution_ids
    manifest["preregistered_cells"]["structural_diagnostic_task_ids"] = diagnostic_ids
    manifest["experiment_revision"] = EXPERIMENT_REVISION
    manifest["evaluation_contract"] = _evaluation_contract(policy)
    manifest["execution_controls"]["codex_transport"] = (
        "run-codex-structural.py and run-codex-agentic.py provider adapters"
    )
    agentic_suite = next(suite for suite in suites if suite["path"] == TASKS_AGENTIC)
    manifest["agentic_execution_contract"] = {
        "measurement_scope": "static_graph_query",
        "quality_claim_limit": "Declared static graph facts only; no behavioral change-impact or implementation correctness claim.",
        "arms": list(core.ARM_CONTRACTS),
        "coordinate_timeout_seconds": core.PARITY_TIMEOUT_SECONDS,
        "default_repetitions": 1,
        "default_total_cells_by_provider": {
            "claude": len(agentic_suite["ordered_task_ids"]) * len(core.ARM_CONTRACTS) * 3,
            "codex": len(agentic_suite["ordered_task_ids"]) * len(core.ARM_CONTRACTS),
        },
        "models_by_provider": {
            "claude": ["haiku", "sonnet", "opus"],
            "codex": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"],
        },
        "providers": ["claude", "codex"],
        "repeat_override": (
            "Each provider adapter admits a nondefault positive repeat only with a deterministic scope hash binding "
            "the provider, current manifest, ordered tasks, arms, models, repetitions, total cells, and per-cell timeout."
        ),
        "scoring": (
            "quality is graded admitted credit / all assigned cells; counts use min/max proportional credit and "
            "rankings use longest-common-subsequence overlap divided by maximum list length. Invalid execution, "
            "format, treatment and unobserved cells score zero; unknown grading is unavailable. Exact "
            "pass is fully correct, completed, valid, uncontaminated and treatment-adherent cells / all assigned cells, "
            "including failed and unobserved cells. component is the secondary macro mean across required answer_contract "
            "components with an explicit scored denominator. Efficiency pairs require both cells to pass and report "
            "per-metric eligibility; pass improvements, regressions, both-fail and unobserved pairs remain separate. Exactly one "
            "complete bare JSON object may be scored as diagnostic-only and is never pooling-eligible; malformed or "
            "ambiguous answers remain semantically unscored. EREC and RREC are raw-text recall diagnostics independent "
            "of the answer protocol, and DEFF is their unbounded expected-importer exposure-hit count per command."
        ),
        "reporting_version": "agentic-graded-v2",
        "task_ids": agentic_suite["ordered_task_ids"],
    }
    manifest["implementation_contract"]["artifact_sha256"] = _artifact_hashes()
    manifest["index"] = copy.deepcopy(INDEX_LOCK)
    manifest["patch_index_contract"] = _load_json(BENCHMARKS / "suites" / "patch-index-locks.json")
    manifest["suite_integrity"] = _suite_integrity(suites)
    manifest["suites"] = suites
    return manifest


def _manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    """Serialize the deterministic machine-readable methodology lock."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_or_check(path: Path, expected: bytes, *, check: bool) -> None:
    """Write generated output or fail closed when its exact bytes are stale."""
    if check:
        if not path.is_file() or path.read_bytes() != expected:
            raise ValueError(f"generated methodology record is stale: {path}; run: {REBUILD_COMMAND}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)


def main(check: bool = False) -> None:
    """Build the methodology source or verify it without model or authentication access.

    Args:
        check: Fail with exit status 1 if the generated methodology differs from its
            inputs instead of rewriting it (CLI flag: ``--check``).

    Raises:
        SystemExit: With status 1 when ``check`` is set and the generated bytes differ.

    Examples:
        >>> main.__name__
        'main'
    """
    try:
        _write_or_check(OUTPUT_MANIFEST, _manifest_bytes(_build_manifest()), check=check)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"{'verified' if check else 'wrote'}: {OUTPUT_MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    from fire import Fire

    Fire(main)
