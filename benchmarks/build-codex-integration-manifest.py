#!/usr/bin/env python3
"""Build or relock the Codex plain/CLI/Skill manifest; never run models or tasks.

The output derives canonical benchmark identities from the committed structural methodology manifest. ``--check`` fails
closed if generated records no longer match their source inputs; the default mode writes the deterministic machine and
human review records.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
# Self-named so a rename cannot leave the stale-output hint pointing at a missing script.
REBUILD_COMMAND = f"uv run python {Path(__file__).resolve().relative_to(ROOT).as_posix()}"
MANIFESTS = BENCHMARKS / "manifests"
SOURCE_MANIFEST = MANIFESTS / "provider-parity-methodology.json"
OUTPUT_MANIFEST = MANIFESTS / "codex-integration.json"
OUTPUT_HUMAN_MANIFEST = MANIFESTS / "codex-integration.md"
EXPERIMENT_ID = "codex-integration-v1"
EXPERIMENT_REVISION = "codex-integration-unified-task-cli-2026-08-11"
TELEMETRY_CONTRACT_ID = "installed-skill-binding-locked-query-components-v3"
CANONICAL_QUERY_FORM = '"$CODEMAP_BIN" query --compact <subcommand> [arguments]'
ARM_ORDER_POLICY = (
    "deterministic six-permutation counterbalancing by frozen structural task ordinal; "
    "across the 55-task single-repetition execution suite, every arm occupies every ordinal 18 or 19 times"
)
TASK_SELECTION_REPETITIONS = 3
TASK_SELECTION_COORDINATE_TIMEOUT_SECONDS = 600
REVIEWED_CODEX_CLI_VERSION = "codex-cli 0.146.1"


def _sha256(path: Path) -> str:
    """Return the SHA-256 identity for one required local file."""
    if not path.is_file():
        raise ValueError(f"required relock input is missing or not a file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    """Load one required JSON object while rejecting non-object roots."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON input: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _plugin_version(path: Path, expected_name: str) -> str:
    """Read and validate one plugin candidate identity before it is frozen."""
    payload = _load_json(path)
    if payload.get("name") != expected_name or not isinstance(payload.get("version"), str):
        raise ValueError(f"invalid {expected_name} plugin manifest: {path}")
    return payload["version"]


def _codex_cli_identity() -> dict[str, str | bool]:
    """Return reviewed CLI provenance without making it an admission pin."""
    return {
        "available": True,
        "path": "<codex-cli>",
        "reviewed_version": REVIEWED_CODEX_CLI_VERSION,
    }


def _artifact_hashes() -> dict[str, str]:
    """Lock candidate package, runtime, adapter, and runner bytes used by the arms."""
    paths = {
        "codemap_candidate_manifest": "plugins/codemap-py/.codex-plugin/plugin.json",
        "codemap_query_skill": "plugins/codemap-py/codex-skills/query-code/SKILL.md",
        "codemap_runtime_cli": "plugins/codemap-py/bin/codemap-py",
        "codemap_runtime_entrypoint": "plugins/codemap-py/src/codemap_py/cli.py",
        "codemap_runtime_graph": "plugins/codemap-py/src/codemap_py/graph.py",
        "codemap_runtime_integration": "plugins/codemap-py/src/codemap_py/integration.py",
        "codemap_runtime_query": "plugins/codemap-py/src/codemap_py/query.py",
        "codex_rig_adapter": "plugins/codex-rig/shared/codemap_adapter.py",
        "codex_rig_contract": "plugins/codex-rig/shared/codemap-contract.md",
        "codex_rig_integration_host": "plugins/codex-rig/shared/codemap-py-integration.md",
        "codex_rig_package_manifest": "plugins/codex-rig/package-manifest.json",
        "codex_rig_plugin_manifest": "plugins/codex-rig/.codex-plugin/plugin.json",
        "run_all": "benchmarks/run-all.sh",
        "run_codex_structural": "benchmarks/run-codex-structural.py",
        "codex_stage_readcrop": "benchmarks/_bench_codex/stage_readcrop.py",
        "codex_stage_fix": "benchmarks/_bench_codex/stage_fix.py",
        "codex_stage_runtime": "benchmarks/_bench_codex/runtime.py",
        "paid_lifecycle": "benchmarks/_bench_common/paid_lifecycle.py",
        "presentation": "benchmarks/_bench_common/presentation.py",
        "prepare_codex_index": "benchmarks/prepare-codex-index.py",
    }
    return {name: _sha256(ROOT / relative_path) for name, relative_path in paths.items()}


def _direct_cli_runtime() -> dict[str, Any]:
    """Lock the exact source closure staged into B's disposable home."""
    runtime_root = ROOT / "plugins" / "codemap-py"
    relative_paths = [
        Path("bin/codemap-py"),
        Path("bin/_exclusions.py"),
        Path("scripts/codemap_py_entry.py"),
        *sorted(path.relative_to(runtime_root) for path in (runtime_root / "src" / "codemap_py").rglob("*.py")),
    ]
    files = {path.as_posix(): _sha256(runtime_root / path) for path in relative_paths}
    aggregate_payload = "".join(f"{path}\0{sha256}\n" for path, sha256 in sorted(files.items()))
    return {
        "aggregate_sha256": hashlib.sha256(aggregate_payload.encode("utf-8")).hexdigest(),
        "files": files,
        "staged_root": "<disposable-CODEX_HOME>/direct-cli",
    }


def _codemap_package_manifest_sha256() -> str:
    """Build the deterministic Codemap candidate and return its package identity."""
    builder = ROOT / "plugins/codemap-py/scripts/build_package.py"
    validator = ROOT / "plugins/codemap-py/scripts/validate_package.py"
    mode_map_args: list[str] = []
    if os.environ.get("CODEX_LAUNCHER_SNAPSHOT_ACTIVE") == "1":
        mode_map = ROOT / "benchmarks/manifests/codemap-package-mode-map.json"
        if not mode_map.is_file():
            raise ValueError("Codemap package freeze failed: paid source snapshot lacks its package mode map")
        mode_map_args = ["--mode-map", str(mode_map)]
    with tempfile.TemporaryDirectory(prefix="codex-integration-codemap-package-") as temporary:
        candidate = Path(temporary) / "candidate"
        for command in (
            [sys.executable, str(builder), "--out", str(candidate), *mode_map_args],
            [sys.executable, str(validator), "--package", str(candidate)],
            [sys.executable, str(builder), "--out", str(candidate), "--check", *mode_map_args],
        ):
            completed = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ValueError(f"Codemap package freeze failed: {detail}")
        return _sha256(candidate / "package-manifest.json")


def _arms() -> dict[str, dict[str, Any]]:
    """Return the only allowed Codex treatments and their enforceable use rules."""
    return {
        "A_plain": {
            "installed_packages": [],
            "requirement": "Codemap is absent; solve with ordinary Codex tools.",
            "treatment": "plain",
        },
        "B_auto": {
            "installed_packages": [],
            "requirement": (
                "Run at least one successful compact direct query in its own native command item containing exactly "
                f"{CANONICAL_QUERY_FORM}; "
                "additional reads and shell work are allowed as separate items."
            ),
            "treatment": "direct_cli",
        },
        "C_strict": {
            "installed_packages": ["codemap-py", "codex-rig"],
            "requirement": (
                "Use the immutable installed $codemap-py:query-code Skill binding and complete one successful "
                "canonical compact query as its entire separate item; "
                "additional reads and shell work are allowed."
            ),
            "treatment": "packaged_skill",
        },
    }


def _codex_permission_profiles(source: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the frozen permission contract for direct-CLI and skill treatments."""
    profiles = copy.deepcopy(source["codex_permission_profiles"])
    host_roots = ["<host-home>/.agents", "<host-home>/.claude", "<host-home>/.codex"]
    profiles["treatment"]["arms"] = ["B_auto", "C_strict"]
    profiles["treatment_runtime"]["scope"] = ["B_auto", "C_strict"]
    profiles["plain"]["filesystem_overrides"].update({root: "deny" for root in [*host_roots, "<marketplace-root>"]})
    profiles["treatment"]["filesystem_overrides"].update({root: "deny" for root in host_roots})
    profiles["host_tooling_isolation"] = {
        "access": "deny",
        "arms": ["A_plain", "B_auto", "C_strict"],
        "roots": host_roots,
        "verification": "no-model directory enumeration must fail without emitting entries",
    }
    profiles["marketplace_source_access"] = {
        "A_plain": "deny",
        "B_auto": "deny after the locked direct runtime is staged inside its disposable home",
        "C_strict": "deny",
    }
    profiles["preflight"] = [
        "record the observed Codex CLI identity without version-based admission",
        "reject profile parse or selection failure",
        "prove source-tree write denial",
        "prove copied auth.json read denial without disclosing credential bytes",
        "prove host .agents, .claude, and .codex roots cannot be enumerated without emitting entries",
        "for every arm, prove the local marketplace source root cannot be enumerated",
        "for A/B, prove CODEMAP_SKILL_FILE is absent even when the host exports it",
        "for A, prove the locked index directory is unreadable and no Codemap plugin or launcher is exposed",
        "for B/C, prove write access only in the initialized index-local .index-rw coordination root",
        "for B/C, prove the locked CODEMAP_PYTHON executable reports Python 3.11 before model execution",
        "for B, stage only the exact locked direct CLI runtime, verify every staged file hash, deny source reads, and execute one task-shaped compact query",
        "for C, install the exact locked codemap-py then codex-rig packages and verify their installed bytes",
        "for C, bind CODEMAP_SKILL_FILE to the exact installed manifest-locked query Skill",
        "for C, preserve and re-verify exactly both enabled plugin registrations after final permission composition",
        "for C, execute the installed Codex Rig adapter through the installed provider launcher and persist one compact-query context",
    ]
    return profiles


def _execution_controls(source: dict[str, Any]) -> dict[str, Any]:
    """Lock the retry-inclusive per-cell timeout and execution controls."""
    controls = copy.deepcopy(source["execution_controls"])
    controls["applies_to"] = ["codex"]
    controls["codex_permission_profiles"] = (
        "A uses provider-parity-plain; B uses a locked direct CLI without plugins; "
        "C installs locked codemap-py and codex-rig packages and validates their adapter bridge. "
        "All profiles extend :read-only, deny auth.json, host-agent-root, and local marketplace-source reads, disable "
        "network, and inherit no shell environment. B stages only the locked direct CLI runtime inside its disposable "
        "home; C uses the installed package pair."
    )
    controls["codex_transport"] = (
        "run-codex-structural.py with A_plain, B_auto, and C_strict; no Codex agentic adapter is registered"
    )
    controls["coordinate_timeout_scope"] = (
        "one total 600-second budget shared by the initial attempt and at most two eligible retries"
    )
    controls["arm_order"] = ARM_ORDER_POLICY
    controls["token_prompt_cache_policy"] = (
        "Console and primary efficiency reports use gross provider input tokens only. "
        "Cached and fresh input counts are retained as raw telemetry diagnostics. "
        "The Codex CLI exposes no supported per-cell provider prompt-cache reset or disable control. "
        "Deterministic arm-order counterbalancing mitigates order exposure without claiming cache elimination."
    )
    controls["retry"] = (
        "at most 2 retries only after retryable transport failure with zero input and output tokens; "
        "retries receive only the unspent coordinate budget"
    )
    return controls


def _telemetry_admission() -> dict[str, Any]:
    """Declare the canonical native-item evidence contract for paid Codex runs."""
    return {
        "telemetry_contract_id": TELEMETRY_CONTRACT_ID,
        "run_metadata_schema": "codex-structural-run-metadata-v2",
        "auxiliary_item_policy": (
            "B/C may use additional reads and shell commands as separate native items. "
            "They are ignored for query attribution."
        ),
        "raw_result_policy": (
            "Raw JSONL cells are immutable. Parser corrections produce a separately versioned derived evaluation."
        ),
        "skill_binding": {
            "arm": "C_strict",
            "environment_binding": ("runner-owned immutable exact installed query Skill path; absent from A and B"),
            "manual_read_telemetry": "skill_delivery_observed is diagnostic-only and never a query-credit prerequisite",
        },
        "query": {
            "item_scope": "dedicated native command item",
            "accepted_form": CANONICAL_QUERY_FORM,
            "required_exit_code": 0,
            "required_output": "one JSON document with index.query_complete=true and index.compact=true",
        },
        "locked_query_components": {
            "conformance": "locked_query_conformance",
            "overall": "locked_query_fitness",
            "endpoint": "locked_query_endpoint_fitness",
            "target": "locked_query_target_fitness",
            "options": "locked_query_option_fitness",
            "semantics": "Conformance is exact tuple agreement; fitness is continuous component-level Jaccard similarity.",
        },
        "treatment_attribution": {
            "B_auto": "direct CLI reachable; the model decides whether to query",
            "C_strict": "at least one successful canonical compact query in the immutable installed-Skill treatment",
        },
        "rejected_evidence": [
            "launcher inspection without query execution",
            "aliases, assignments, conditionals, compound shell, redirections, substitutions, or nested shells",
            "query output that is not one JSON document with complete compact index evidence",
            "exact-path or non-CODEMAP_BIN launcher forms",
        ],
    }


def _preregistered_cells(source: dict[str, Any]) -> dict[str, Any]:
    """Reuse the frozen structural task selections with the new three-arm design."""
    cells = copy.deepcopy(source["preregistered_cells"])
    cells["arms"] = list(_arms())
    cells["providers"] = ["codex"]
    cells["smoke_task_ids"] = ["FN-02"]
    cells["confirmatory_repetitions"] = 1
    cells["arm_order"] = ARM_ORDER_POLICY
    return cells


def _task_selection_contract(source: dict[str, Any]) -> dict[str, Any]:
    """Describe the unified task-driven selection contract for every Codex stage."""
    cells = _preregistered_cells(source)
    suite_ids = {suite["path"]: list(suite["ordered_task_ids"]) for suite in source["suites"]}
    stage_ids = {
        "structural": list(cells["structural_execution_task_ids"]),
        "readcrop": suite_ids["benchmarks/suites/tasks-readcrop.json"],
        "fix-single": suite_ids["benchmarks/suites/tasks-fix-single.json"],
        "fix-multi": suite_ids["benchmarks/suites/tasks-fix-multi.json"],
        "patch": suite_ids["benchmarks/suites/tasks-patch.json"],
    }
    task_ids = [task_id for stage_task_ids in stage_ids.values() for task_id in stage_task_ids]
    families = list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in task_ids))
    stages = {
        stage: {
            "allowed_families": list(dict.fromkeys(task_id.split("-", 1)[0] for task_id in selected_ids)),
            "allowed_task_ids": selected_ids,
            "arms": list(_arms()) if stage == "structural" else ["A_plain", "B_auto", "C_strict"],
            "default_repetitions": 1,
            "selected_repetitions": TASK_SELECTION_REPETITIONS if stage == "structural" else 1,
        }
        for stage, selected_ids in stage_ids.items()
    }
    return {
        "allowed_families": families,
        "allowed_task_ids": task_ids,
        "default_total_cells": len(task_ids) * 3,
        "stage_order": list(stage_ids),
        "stages": stages,
        "confirmatory_product_acceptance": "ineligible",
        "coordinate_timeout_seconds": TASK_SELECTION_COORDINATE_TIMEOUT_SECONDS,
        "nonpoolable": True,
        "pooling_eligibility": "ineligible",
        "purpose": (
            "Task-family or exact-task selection across the frozen structural, ReadCrop, Fix-Single, Fix-Multi, and Patch "
            "stages; omitted selectors execute every supported task once."
        ),
        "resolution_policy": {
            "exact_id_first": True,
            "family_match": "a family token selects every matching allowed ID",
            "order": "resolved IDs preserve structural, ReadCrop, Fix-Single, Fix-Multi, then Patch stage order",
            "deduplicate": "selector tokens and overlapping expanded IDs are evaluated once",
            "reject": ["empty tokens", "unknown task IDs", "unknown families"],
        },
        "scope_digest": {
            "canonical_fields": [
                "active manifest SHA-256",
                "selection mode",
                "resolved ordered task IDs",
                "ordered stage partitions with repetitions, arms, and cell counts",
                "total tasks and cells",
            ],
            "runtime_derived": True,
            "stored_in_manifest": False,
        },
        "aggregate_execution_scope": {
            "canonical_fields": [
                "schema version",
                "active manifest SHA-256",
                "model and reasoning effort",
                "selection mode and ordered task IDs",
                "ordered stage partitions and their native scope SHA-256 values",
                "total tasks and cells",
            ],
            "approval_option": "--paid-approval",
            "dry_run_marker": "SCOPE",
        },
        "separator": ",",
        "selector_option": "--tasks",
    }


def _build_manifest() -> dict[str, Any]:
    """Build one deterministic experiment record from immutable source identities."""
    source = _load_json(SOURCE_MANIFEST)
    required_source_keys = (
        "codex_permission_profiles",
        "evaluation_contract",
        "execution_controls",
        "headline_structural_v1",
        "implementation_contract",
        "index",
        "oracle_remediation",
        "preregistered_cells",
        "suite_integrity",
        "suites",
        "target_source",
        "validation",
    )
    missing = [key for key in required_source_keys if key not in source]
    if missing:
        raise ValueError(f"structural methodology is missing identities: {', '.join(missing)}")

    codemap_manifest = ROOT / "plugins/codemap-py/.codex-plugin/plugin.json"
    codex_rig_manifest = ROOT / "plugins/codex-rig/.codex-plugin/plugin.json"
    artifact_sha256 = _artifact_hashes()
    codemap_package_manifest_sha256 = _codemap_package_manifest_sha256()
    implementation_contract = copy.deepcopy(source["implementation_contract"])
    implementation_contract["artifact_sha256"] = artifact_sha256
    implementation_contract["codex_model_stratum"] = {
        "model": "gpt-5.6-luna",
        "additional_strata": ["gpt-5.6-terra", "gpt-5.6-sol"],
        "reasoning_effort": "high",
        "strict_config": True,
    }
    return {
        "arms": _arms(),
        "artifact_sha256": artifact_sha256,
        "codex_cli": _codex_cli_identity(),
        "codex_permission_profiles": _codex_permission_profiles(source),
        "codemap_candidate": {
            "package_manifest_sha256": codemap_package_manifest_sha256,
            "plugin_manifest": "plugins/codemap-py/.codex-plugin/plugin.json",
            "query_skill": "plugins/codemap-py/codex-skills/query-code/SKILL.md",
            "version": _plugin_version(codemap_manifest, "codemap-py"),
        },
        "codex_rig_candidate": {
            "package_manifest": "plugins/codex-rig/package-manifest.json",
            "plugin_manifest": "plugins/codex-rig/.codex-plugin/plugin.json",
            "version": _plugin_version(codex_rig_manifest, "codex-rig"),
        },
        "codex_rig_integration_admission": {
            "adapter": "plugins/codex-rig/shared/codemap_adapter.py",
            "probe_category": "analysis",
            "probe_target": "lightning.pytorch.trainer.call",
            "contract": "plugins/codex-rig/shared/codemap-contract.md",
            "required_before_paid_smoke": [
                "C installs exactly the locked codemap-py then codex-rig package roster.",
                "The Codex Rig adapter resolves the public CODEMAP_BIN launcher before PATH.",
                "One persisted compact-query context is available without Codemap cache or source reads.",
            ],
        },
        "direct_cli_admission": {
            "probe_subcommand": "fn-rdeps",
            "probe_target": "lightning.pytorch.trainer.call::_call_lightning_module_hook",
            "required_before_paid_execution": (
                "Execute this compact query through B's staged launcher under the treatment permission profile; "
                "require exit 0, index.query_complete=true, and unchanged locked-index bytes."
            ),
        },
        "direct_cli_runtime": _direct_cli_runtime(),
        "evaluation_contract": source["evaluation_contract"],
        "estimands": {
            "C_strict-A_plain": "product effect",
            "B_auto-A_plain": "direct CLI availability effect",
            "C_strict-B_auto": "integration effect",
        },
        "experiment_id": EXPERIMENT_ID,
        "experiment_revision": EXPERIMENT_REVISION,
        "execution_controls": _execution_controls(source),
        "headline_structural_v1": source["headline_structural_v1"],
        "implementation_contract": implementation_contract,
        "index": source["index"],
        "model": {
            "name": "gpt-5.6-luna",
            # Additional strata are run as separate, nonpoolable studies, exactly as Claude runs
            # haiku/sonnet/opus. "name" stays the default so a single-model invocation is unchanged.
            # This list and the methodology manifest's models_by_provider["codex"] are the same
            # declaration read from two sides: --models resolves a name against the methodology
            # list, and the runner admits a stratum against this one, so a name missing here fails
            # at the preflight after the selection already resolved.
            "additional_strata": ["gpt-5.6-terra", "gpt-5.6-sol"],
            "reasoning_effort": "high",
            "strict_config": True,
        },
        "no_model_validation": {
            "checks": {
                "a_plain_isolation": "required",
                "b_staged_runtime_hashes_and_task_shaped_query": "required",
                "c_installed_package_locks": "required",
                "c_installed_rig_adapter_context": "required",
                "claude_preflight_non_regression": "required",
                "manifest_and_suite_identity": "required",
                "permission_profiles": "required",
                "target_and_index_identity": "required",
            },
            "evidence": [
                {
                    "command": (
                        "python3 benchmarks/run-codex-structural.py --repo-path <locked-target> "
                        "--manifest-path benchmarks/manifests/codex-integration.json --index-path <locked-index> "
                        "--marketplace-root <repository> --codemap-bin "
                        "<repository>/plugins/codemap-py/bin/codemap-py --model gpt-5.6-luna --tasks FN-02 --dry-run"
                    ),
                    "required_result": (
                        "exit 0; A absent; B exact staged runtime plus task-shaped compact query; C locked provider-then-consumer "
                        "install plus exact CODEMAP_SKILL_FILE binding and installed-adapter compact context; "
                        "deterministic three-arm plan"
                    ),
                },
                {
                    "command": "REPO=<locked-target> bash benchmarks/run-all.sh smoke",
                    "required_result": (
                        "exit 0; frozen index unchanged; Claude preflights unchanged; "
                        "Codex plain/CLI/skill preflight passed"
                    ),
                },
            ],
            "model_or_auth_used": False,
            "query_diagnostic": (
                "run-codemap-cli.py remains non-admission diagnostic evidence; retain each run's output separately because "
                "its latency counts are environment-sensitive"
            ),
            "status": "runtime_smoke_required_before_paid_execution",
        },
        "oracle_remediation": source["oracle_remediation"],
        "package_roster": ["codemap-py", "codex-rig"],
        "task_selection": _task_selection_contract(source),
        "preregistered_cells": _preregistered_cells(source),
        "schema_version": "codex-integration-manifest-v3",
        "source_manifest": {
            "path": SOURCE_MANIFEST.relative_to(ROOT).as_posix(),
            "sha256": _sha256(SOURCE_MANIFEST),
        },
        "suite_integrity": source["suite_integrity"],
        "suites": source["suites"],
        "target_source": source["target_source"],
        "telemetry_admission": _telemetry_admission(),
        "validation": source["validation"],
    }


def _json_bytes(manifest: dict[str, Any]) -> bytes:
    """Serialize one deterministic machine record with a final newline."""
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _human_bytes(manifest: dict[str, Any], machine_sha256: str) -> bytes:
    """Render the short review record from the same immutable machine content."""
    structural_tasks = len(manifest["preregistered_cells"]["structural_execution_task_ids"])
    repetitions = manifest["preregistered_cells"]["confirmatory_repetitions"]
    arm_count = len(manifest["preregistered_cells"]["arms"])
    task_selection = manifest["task_selection"]
    execution_tasks = len(task_selection["allowed_task_ids"])
    total_cells = task_selection["default_total_cells"]
    lines = [
        f"# `{EXPERIMENT_ID}`",
        "",
        f"**Manifest SHA-256**: `{machine_sha256}`",
        "",
        "## Purpose",
        "",
        "Codex-only A/B/C experiment over the immutable provider-parity task and scoring identities.",
        "",
        "## Arms",
        "",
        "- `A_plain`: no Codemap package or query access.",
        f"- `B_auto`: one dedicated successful `{CANONICAL_QUERY_FORM}` command item.",
        "- `C_strict`: immutable installed Skill binding and one dedicated canonical compact query item.",
        "- B/C may use additional reads and shell commands as separate items; those actions are ignored for attribution.",
        "",
        "## Estimands",
        "",
        "- `C_strict-A_plain`: product effect.",
        "- `B_auto-A_plain`: direct CLI availability effect.",
        "- `C_strict-B_auto`: integration effect.",
        "",
        "## Execution controls",
        "",
        "- The 600-second coordinate budget is shared by the initial attempt and any eligible zero-token transport retries.",
        "- The manifest's per-cell timeout includes the initial attempt and all eligible zero-token transport retries.",
        "- Arm order uses deterministic six-permutation counterbalancing by frozen structural task ordinal; across the "
        "55-task single-repetition execution suite, every arm occupies every ordinal 18 or 19 times.",
        "- Console and primary efficiency reports use gross provider input tokens only. Cached and fresh input counts "
        "are retained as raw telemetry diagnostics.",
        "- The Codex CLI exposes no supported per-cell provider prompt-cache reset or disable control. Deterministic "
        "arm-order counterbalancing mitigates order exposure without claiming cache elimination.",
        "",
        "## Credential safety",
        "",
        "- Use an immutable, user-owned `0600` auth source. Do not run a concurrent Codex session with it; use an independently authenticated benchmark credential instead.",
        "- The runner keeps private run state and atomically propagates valid refreshes between cells. A private sequential refresh can invalidate an unchanged source, so reauthenticate after the run if needed.",
        "- Known refresh-token authentication failures stop immediately; three matching unknown zero-token pre-response failures preserve partial artifacts and stop scheduling.",
        "",
        "## Locked candidates",
        "",
        f"- `codemap-py` `{manifest['codemap_candidate']['version']}`.",
        f"  Package manifest SHA-256: `{manifest['codemap_candidate']['package_manifest_sha256']}`.",
        f"- `codex-rig` `{manifest['codex_rig_candidate']['version']}`.",
        f"- Codex CLI: `{manifest['codex_cli']}`.",
        f"- Source manifest: `{manifest['source_manifest']['path']}` SHA-256 `{manifest['source_manifest']['sha256']}`.",
        "",
        "## Unified execution scope",
        "",
        f"- Default execution: `{execution_tasks}` tasks and `{total_cells}` A/B/C cells.",
        "- Stage partitions: `55` structural, `6` ReadCrop, `4` Fix-Single, `3` Fix-Multi, and `5` historical Patch tasks.",
        "- Omit `--tasks` to execute the complete supported suite once.",
        "- Structural, ReadCrop, Fix-Single, Fix-Multi, and Patch retain separate child artifacts, native scorers, and nonpoolable quality records.",
        "- Model-cell failures are recorded by their native stage; integrity and interruption failures preserve completed child artifacts and stop later stages.",
        "",
        "## Structural methodology",
        "",
        f"- Structural execution tasks: `{structural_tasks}`.",
        f"- Independently scored headline tasks: `{len(manifest['preregistered_cells']['structural_confirmatory_task_ids'])}`.",
        f"- Diagnostic tasks: `{len(manifest['preregistered_cells']['structural_diagnostic_task_ids'])}`.",
        f"- Repetitions: `{repetitions}`.",
        f"- Structural default cells: `{structural_tasks * repetitions * arm_count}` (`{structural_tasks} tasks × {repetitions} repetition × {arm_count} arms`).",
        "",
        "## Selected-task scope",
        "",
        "- Use `--tasks=DI,GR` for family selection, `--tasks=DI-01,GR-03` for exact IDs, or `--tasks=DI,GR-03` for a mixed selection.",
        "- Exact task IDs are resolved before family tokens; family tokens select all matching frozen IDs.",
        "- Empty or unknown selectors fail closed; duplicate tokens and overlapping expansions are evaluated once.",
        "- Resolved IDs always follow frozen manifest order, independent of selector order.",
        "- Omit `--tasks` for all 73 supported tasks; selected structural tasks retain three repetitions while RC/FS/FM/PT tasks retain one repetition.",
        f"- Every stage uses three native arms and a `{task_selection['coordinate_timeout_seconds']}`-second coordinate limit.",
        "- Selected-task runs are explicitly nonpoolable and ineligible for confirmatory or product acceptance.",
        "- Task resolution prints a selection identity for inspection. Model execution is authorized only by the aggregate `SCOPE` printed by the matching full dry run.",
        "",
        "## Paid selected-task command",
        "",
        "Replace `RC,FS,FM,PT` with an approved family, exact-ID, or mixed selector. The same selector must be used for dry-run and paid execution.",
        "",
        "Run the matching no-model dry run first. It validates every selected stage and prints one exact `PAID_COMMAND` containing the aggregate approval and a fresh run directory.",
        "",
        "```bash",
        "python3 benchmarks/run-codex-structural.py --repo-path <locked-target> --index-path <locked-index> --marketplace-root <marketplace> --codemap-bin <absolute-launcher> --model gpt-5.6-luna --tasks RC,FS,FM,PT --dry-run",
        "```",
        "",
        "## Full execution",
        "",
        "Run the exact no-model unified plan first:",
        "",
        "```bash",
        f"bash benchmarks/run-all.sh codex --struct --models={manifest['model']['name']} --dry-run",
        "```",
        "",
        "After reviewing the 73-task/219-cell plan, copy the exact `PAID_COMMAND` printed by that dry run. Do not substitute the machine-manifest or selector-resolution digest for its aggregate approval.",
        "",
        "The aggregate approval binds the manifest, model, ordered tasks, and every native child scope. The runner rejects stale approval or an existing run directory before any model call. Runtime logs, telemetry, metadata, and checksums stay under the ignored `benchmarks/results/` directory unless deliberately exported for review.",
        "",
        "## Status",
        "",
        "Runtime smoke and exact coordinate-plan validation are required before paid execution. This manifest rebuild "
        "used no model cell or authentication source.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _write_or_check(path: Path, expected: bytes, check: bool) -> None:
    """Write one generated record or reject any byte drift in check mode."""
    if check:
        if not path.is_file() or path.read_bytes() != expected:
            raise ValueError(f"generated record is stale: {path}; run: {REBUILD_COMMAND}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)


def main(check: bool = False) -> None:
    """Build the relock files or verify them without any model/task invocation.

    Args:
        check: Fail with exit status 1 if the generated records differ from the current
            inputs instead of rewriting them (CLI flag: ``--check``).

    Raises:
        SystemExit: With status 1 when ``check`` is set and a generated record is stale.

    Examples:
        >>> main.__name__
        'main'
    """
    try:
        manifest = _build_manifest()
        machine_bytes = _json_bytes(manifest)
        machine_sha256 = hashlib.sha256(machine_bytes).hexdigest()
        _write_or_check(OUTPUT_MANIFEST, machine_bytes, check)
        _write_or_check(OUTPUT_HUMAN_MANIFEST, _human_bytes(manifest, machine_sha256), check)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    action = "verified" if check else "wrote"
    print(f"{action}: {OUTPUT_MANIFEST.relative_to(ROOT)}")
    print(f"{action}: {OUTPUT_HUMAN_MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    from fire import Fire

    Fire(main)
