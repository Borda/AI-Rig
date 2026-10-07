"""Executable Fix-Single, Fix-Multi, and Patch benchmark stages.

The structural runner supplies the established native Codex transport and disposable-home isolation. This module owns
executable task contracts, prompts, patch scoring, offline rescoring, and executable-stage dispatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path, PurePath
from types import ModuleType
from typing import Any
from uuid import uuid4

#: The ``benchmarks/`` directory, one level above this package.
BENCHMARKS = Path(__file__).resolve().parents[1]
#: The repository root, the parent of the ``benchmarks/`` directory.
ROOT = BENCHMARKS.parent
#: Structural runner script the fix stages load; its hash is recorded in run provenance.
STRUCTURAL_PATH = BENCHMARKS / "run-codex-structural.py"
#: Codex integration manifest whose hash is recorded in run provenance and which pins the experiment revision.
PARITY_MANIFEST_PATH = BENCHMARKS / "manifests" / "codex-integration.json"
#: Task suite file for the single-file fix stage.
FIX_SINGLE_TASKS_PATH = BENCHMARKS / "suites" / "tasks-fix-single.json"
#: Task suite file for the multi-file fix stage.
FIX_MULTI_TASKS_PATH = BENCHMARKS / "suites" / "tasks-fix-multi.json"
#: Task suite file for the patch stage.
PATCH_TASKS_PATH = BENCHMARKS / "suites" / "tasks-patch.json"
#: JSON file locking the codemap index bindings for patch tasks; validated and hashed into run provenance.
PATCH_INDEX_LOCKS_PATH = BENCHMARKS / "suites" / "patch-index-locks.json"
#: Directory name of the managed provider-parity repository checkout, which the locked patch index expects.
_MANAGED_PARITY_REPO_NAME = "codemap-provider-parity-pl-2.6.5"
#: The canonical managed target is the *root* temp directory, not the per-user one:
#: `suites/patch-index-locks.json` locks `canonical_scan_root` to
#: `/private/tmp/codemap-provider-parity-pl-2.6.5`, which is what `/tmp` resolves to on the
#: canonical macOS host. `tempfile.gettempdir()` honours $TMPDIR and would silently point
#: at a different directory, so it cannot be used here. The env override keeps the path
#: overridable on a host where the root temp dir is wrong (Windows especially, where the
#: old `Path(os.sep) / "tmp"` resolved to the drive root).
_MANAGED_PARITY_REPO = Path(
    os.environ.get("CODEMAP_PARITY_REPO")
    or f"{os.sep}tmp{os.sep}{_MANAGED_PARITY_REPO_NAME}"  # portable-paths: canonical-target
).resolve()
#: Canonical arm labels the fix stages run: plain, optional Codemap, required Codemap.
ARMS = ("A_plain", "B_auto", "C_strict")
#: Map from stage arm label to the label used by the native structural runner; every arm maps to itself.
NATIVE_ARMS = {"A_plain": "A_plain", "B_auto": "B_auto", "C_strict": "C_strict"}
#: Per single-file fix task id, the locked ``scan-query`` arguments expected from the Codemap arms.
_FIX_SINGLE_QUERY_ARGUMENTS = {
    "FS-01": ("symbol", "EarlyStopping.__init__"),
    "FS-02": ("symbol", "EarlyStopping.__init__"),
    "FS-03": ("symbol", "ModelCheckpoint._save_checkpoint"),
    "FS-04": ("symbol", "ModelCheckpoint.__init__"),
}
#: Per multi-file fix task id, the locked ``scan-query`` arguments expected from the Codemap arms.
_FIX_MULTI_QUERY_ARGUMENTS = {
    "FM-01": (
        "fn-rdeps",
        "lightning.pytorch.callbacks.early_stopping::EarlyStopping._run_early_stopping_check",
        "--exclude-tests",
    ),
    "FM-02": (
        "fn-rdeps",
        "lightning.pytorch.callbacks.model_checkpoint::ModelCheckpoint._save_checkpoint",
        "--exclude-tests",
    ),
    "FM-03": ("find-symbol", r"Strategy\.setup_environment$", "--exclude-tests", "--limit", "0"),
}
#: Pattern extracting the fenced diff that follows the single-file fix answer marker.
_FIX_SINGLE_ANSWER_RE = re.compile(r"BEGIN_FIX_SINGLE_DIFF\s*(?P<diff>```diff\s*.*?```)", re.DOTALL)
#: Pattern extracting the fenced diff that follows the multi-file fix answer marker.
_FIX_MULTI_ANSWER_RE = re.compile(r"BEGIN_FIX_MULTI_DIFF\s*(?P<diff>```diff\s*.*?```)", re.DOTALL)
#: Pattern extracting the fenced diff that follows the patch answer marker.
_PATCH_ANSWER_RE = re.compile(r"BEGIN_PATCH_DIFF\s*(?P<diff>```diff\s*.*?```)", re.DOTALL)
#: Per patch task id, the locked ``scan-query`` arguments expected from the Codemap arms.
_PATCH_QUERY_ARGUMENTS = {
    "PT-01": ("symbol", "FitLoop.setup_data"),
    "PT-02": ("symbol", "DistributedSamplerWrapper"),
    "PT-03": ("symbol", "ThroughputMonitor._update"),
    "PT-04": ("symbol", "StochasticWeightAveraging.on_fit_start"),
    "PT-05": ("symbol", "_TrainingEpochLoop.advance"),
}

sys.path.insert(0, str(BENCHMARKS))

from _bench_common.edit_patch_contracts import (  # noqa: E402
    EditExecution,
    EditTaskContract,
    StageIdentity,
    assess_patch_answer,
    build_edit_task_contract,
    build_fix_multi_contract,
    build_fix_single_contract,
    build_patch_answer,
    score_edit_execution,
    stage_contract_sha256,
    validate_fix_multi_binding,
    validate_fix_single_binding,
    validate_patch_index_bundle,
    validate_provider_binding,
)
from _bench_common.mutation_isolation import (  # noqa: E402
    PATCH_PYTEST_ENV,
    create_executable_agent_workspace,
    execute_fix_multi_patch,
    execute_fix_single_patch,
    execute_patch_task_answer,
    patch_test_runtime_identity,
    stage_patch_task_agent_workspace,
)
from _bench_common.paid_lifecycle import (  # noqa: E402
    PaidStageCallbacks,
    paid_approval_matches,
    paid_approval_token,
    run_paid_stage,
    verify_checksums,
    write_checksums,
)
from _bench_common.presentation import format_paid_command_block, format_quality  # noqa: E402
from _bench_common.provider_parity_contracts import (  # noqa: E402
    fresh_input_tokens,
    load_task_suite,
    token_accounting_inconsistent,
)

from . import runtime  # noqa: E402


def _structural() -> ModuleType:
    """Return the already-loaded structural runner without importing another copy.

    Test suites frequently execute the runner under a generated module name, so module identity is established by its
    resolved source path instead of name.
    """
    structural_path = STRUCTURAL_PATH.resolve()
    for module in tuple(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if module_file is not None and Path(module_file).resolve() == structural_path:
            return module
    raise RuntimeError("the structural runner must be loaded before the executable stage")


def _executable_codemap_boundary() -> str:
    """Describe the frozen index boundary shared by executable task prompts."""
    return (
        " Codemap is a frozen static source-graph query tool: use it only for symbol, dependency, importer, or caller "
        "facts. Do not ask it to validate runtime behavior, execute tests, apply edits, or resolve facts once a compact "
        "query reports index.query_complete=true; use direct source reads and the task's tests for those remaining facts."
    )


def _task_prompt(
    arm: str, task: Mapping[str, Any], *, query_arguments: Mapping[str, tuple[str, ...]], study: str
) -> str:
    """Materialize one immutable executable prompt with its arm-only treatment supplement."""
    task_id = str(task["id"])
    if arm == "A_plain":
        supplement = "Codemap is absent and inaccessible. Use ordinary repository tools."
    elif arm == "B_auto":
        supplement = (
            "Codemap's direct CLI is available as $CODEMAP_BIN; use it when useful." + _executable_codemap_boundary()
        )
    elif arm == "C_strict":
        supplement = (
            _structural().arm_envelope("C_strict")
            + f' The required canonical query is exactly `"$CODEMAP_BIN" query --compact {shlex.join(query_arguments[task_id])}`.'
            + _executable_codemap_boundary()
        )
    else:
        raise ValueError(f"unknown {study} arm {arm}")
    return (
        f"{supplement}\n\n{task['prompt']}\n\n"
        "You are inside a benchmark-owned disposable checkout. Git metadata is intentionally inaccessible inside "
        "the agent sandbox. Do not invoke Git. The project dependencies are intentionally unavailable in this agent "
        "environment, so do not run project pytest; use bounded syntax and static validation. Implement the change "
        "directly in the checkout. "
        "Modify only the task's required source paths; do not commit, reset, clean, alter benchmark metadata, or return a diff. "
        "Finish with a concise summary only. The harness captures and validates the canonical Git diff after you finish."
    )


def fix_single_prompt(arm: str, task: Mapping[str, Any]) -> str:
    """Materialize a Fix-Single prompt for one treatment arm."""
    return _task_prompt(arm, task, query_arguments=_FIX_SINGLE_QUERY_ARGUMENTS, study="fix-single")


def fix_multi_prompt(arm: str, task: Mapping[str, Any]) -> str:
    """Materialize a Fix-Multi prompt for one treatment arm."""
    return _task_prompt(arm, task, query_arguments=_FIX_MULTI_QUERY_ARGUMENTS, study="fix-multi")


def patch_prompt(arm: str, task: Mapping[str, Any]) -> str:
    """Materialize a historical Patch task prompt for one treatment arm."""
    return _task_prompt(arm, task, query_arguments=_PATCH_QUERY_ARGUMENTS, study="patch")


def _sha256(value: Mapping[str, object]) -> str:
    """Return the canonical SHA-256 for one immutable contract payload."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    """Return the SHA-256 for one reviewed implementation or fixture file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _patch_snapshot_files(repo_path: Path, tasks: list[dict[str, Any]]) -> dict[str, Path]:
    """Return the complete reviewed Patch implementation and per-task input closure."""
    shared = {
        "codex-runtime.py": BENCHMARKS / "_bench_codex" / "runtime.py",
        "stage-fix.py": Path(__file__),
        "paid-lifecycle.py": BENCHMARKS / "_bench_common" / "paid_lifecycle.py",
        "edit-patch-contracts.py": BENCHMARKS / "_bench_common" / "edit_patch_contracts.py",
        "mutation-isolation.py": BENCHMARKS / "_bench_common" / "mutation_isolation.py",
        "patch-index-locks.json": PATCH_INDEX_LOCKS_PATH,
    }
    shared.update(
        {
            f"patch-index-{item['contract'].task_id}.json": _patch_index_path(repo_path, item["contract"].task_id)
            for item in tasks
        }
    )
    return shared


def assert_query_arguments_cover(
    task_ids: Iterable[str], query_arguments: Mapping[str, tuple[str, ...]], study: str
) -> None:
    """Fail closed when the C_strict query table does not cover every selected task.

    The table is a second source of truth beside the suite, and the prompt that reads it
    is rendered only once paid execution is already under way — so a task added to the
    suite without a matching row crashed mid-run, after spending. A dry run renders every
    prompt, which exercises this too, but the explicit check names the missing IDs.

    Args:
        task_ids: Task identifiers selected for this stage.
        query_arguments: The stage's locked C_strict query table.
        study: Stage name used in the error message.

    Raises:
        ValueError: If any selected task has no locked canonical query.

    Examples:
        >>> assert_query_arguments_cover(["A"], {"A": ("symbol", "X")}, "fix-single")
        >>> assert_query_arguments_cover(["B"], {"A": ("symbol", "X")}, "fix-single")
        Traceback (most recent call last):
        ValueError: fix-single C_strict query table is missing locked queries for: B
    """
    missing = sorted(set(task_ids) - set(query_arguments))
    if missing:
        raise ValueError(f"{study} C_strict query table is missing locked queries for: {', '.join(missing)}")


def load_fix_single_tasks(path: Path, selected: set[str]) -> list[dict[str, Any]]:
    """Load selected immutable Fix-Single tasks with their executable contracts."""
    loaded = [
        {"task": task, "contract": build_fix_single_contract(task)}
        for task in load_task_suite(path)
        if task["id"] in selected
    ]
    if {item["contract"].task_id for item in loaded} != selected:
        raise ValueError("--tasks must select known fix-single task IDs")
    assert_query_arguments_cover(selected, _FIX_SINGLE_QUERY_ARGUMENTS, "fix-single")
    return loaded


def load_fix_multi_tasks(path: Path, selected: set[str]) -> list[dict[str, Any]]:
    """Load selected immutable Fix-Multi tasks with their complete-caller contracts."""
    loaded = [
        {"task": task, "contract": build_fix_multi_contract(task)}
        for task in load_task_suite(path)
        if task["id"] in selected
    ]
    if {item["contract"].task_id for item in loaded} != selected:
        raise ValueError("--tasks must select known fix-multi task IDs")
    assert_query_arguments_cover(selected, _FIX_MULTI_QUERY_ARGUMENTS, "fix-multi")
    return loaded


def _patch_stage_identity(path: Path, contracts: list[EditTaskContract]) -> StageIdentity:
    """Bind Patch provider evidence to the exact selected suite and scorer contract."""
    return StageIdentity(
        stage="patch",
        revision="provider-parity-patch-v1",
        task_suite_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        contract_sha256=stage_contract_sha256(contracts),
    )


def load_patch_tasks(path: Path, selected: set[str]) -> list[dict[str, Any]]:
    """Load selected historical Patch tasks with stage-bound shared contracts."""
    raw_tasks = [task for task in load_task_suite(path) if task["id"] in selected]
    contracts = [build_edit_task_contract(task) for task in raw_tasks]
    if {contract.task_id for contract in contracts} != selected:
        raise ValueError("--tasks must select known patch task IDs")
    assert_query_arguments_cover(selected, _PATCH_QUERY_ARGUMENTS, "patch")
    identity = _patch_stage_identity(path, contracts)
    return [
        {
            "task": task,
            "contract": contract,
            "stage_identity": identity,
            "provider_binding": dict(contract.scientific_field_hashes(identity)),
        }
        for task, contract in zip(raw_tasks, contracts, strict=True)
    ]


def _provider_binding(item: Mapping[str, Any]) -> Mapping[str, str]:
    """Return the stage-specific immutable provider fields for one executable cell."""
    binding = item.get("provider_binding")
    if isinstance(binding, Mapping):
        return {str(key): str(value) for key, value in binding.items()}
    return item["contract"].provider_binding()


def _validate_binding(item: Mapping[str, Any], observed: Mapping[str, Any]) -> None:
    """Reject provider evidence that drifts from its selected executable contract."""
    contract = item.get("contract")
    identity = item.get("stage_identity")
    if isinstance(contract, EditTaskContract):
        if not isinstance(identity, StageIdentity):
            raise ValueError("patch task lacks its immutable stage identity")
        validate_provider_binding(contract, identity, observed)
        return
    if contract.task_id.startswith("FS-"):
        validate_fix_single_binding(contract, observed)
        return
    validate_fix_multi_binding(contract, observed)


def _validate_stage_binding(study: str, item: Mapping[str, Any], observed: Mapping[str, Any]) -> None:
    """Validate a row using the selected stage rather than fixture task-name conventions."""
    contract = item["contract"]
    if study == "fix-single":
        validate_fix_single_binding(contract, observed)
        return
    if study == "fix-multi":
        validate_fix_multi_binding(contract, observed)
        return
    _validate_binding(item, observed)


def normalize_fix_single_patch_wire(diff: str) -> str:
    """Restore presentation-omitted diff markers without changing candidate code."""
    normalized: list[str] = []
    in_hunk = False
    lines = diff.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("diff --git "):
            in_hunk = False
            normalized.append(line)
            if index + 1 < len(lines) and lines[index + 1].startswith("@@ "):
                parts = line.split()
                if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                    raise ValueError("diff --git header must name a/ and b/ paths")
                normalized.extend((f"--- {parts[2]}", f"+++ {parts[3]}"))
            continue
        if line.startswith("@@ "):
            in_hunk = True
        normalized.append(" " if in_hunk and not line else line)
    return "\n".join(normalized) + "\n"


def _parse_patch_cell(
    stream: str | bytes,
    *,
    arm: str,
    item: Mapping[str, Any],
    skill_path: Path | None,
    repo_path: Path,
    captured_diff: str | None,
    answer_re: re.Pattern[str],
    query_arguments: Mapping[str, tuple[str, ...]],
    execute_patch: Callable[[Path, Any, str], Any],
    agent_fixture_intact: bool | None = None,
) -> dict[str, Any]:
    """Normalize one native response and score its stage-owned executable patch."""
    skill_sha256 = hashlib.sha256(skill_path.read_bytes()).hexdigest() if skill_path else ""
    parsed = runtime.parse_codex_jsonl(stream, skill_path=skill_path, skill_sha256=skill_sha256)
    contract = item["contract"]
    match = answer_re.search(parsed.output_text)
    answer_error = ""
    # Harness-side failure, kept distinct from `answer_error` (model-side failure) so a
    # broken sandbox is never published as a wrong answer.
    infra_error = ""
    candidate_diff = normalized_diff = ""
    execution: dict[str, Any] = {
        "baseline_failed": False,
        "baseline_target_failed": False,
        "baseline_regressions_passed": False,
        "fixture_intact": False,
        "source_integrity": False,
        "index_integrity": None,
        "patch_applied": False,
        "changed_paths": [],
        "targeted_test_passed": False,
        "regression_test_passed": None,
        "recount_recoverable": False,
        "recount_oracle_passed": None,
        "cleanup_verified": False,
        "error": "answer unavailable",
    }
    executed: Any | None = None
    if captured_diff is None and match is None:
        answer_error = "missing exact fenced diff envelope"
    else:
        # Two different failures used to land in one field. A malformed model answer is
        # evidence about the model; a git/worktree failure inside the executor is evidence
        # about the harness. Collapsing them scored infrastructure breakage as a wrong
        # answer, and any non-ValueError aborted the whole paid run mid-flight.
        try:
            candidate_diff = (
                captured_diff if captured_diff is not None else assess_patch_answer(match.group("diff")).diff
            )
            if not candidate_diff.startswith("diff --git "):
                raise ValueError("captured executable patch must contain a unified diff")
            # Historical Patch tasks transport the exact worktree diff. Their
            # shared answer contract already validates it, so presentation
            # normalization would change the measured candidate bytes.
            normalized_diff = (
                candidate_diff
                if isinstance(contract, EditTaskContract)
                else normalize_fix_single_patch_wire(candidate_diff)
            )
        except ValueError as exc:
            answer_error = str(exc)
        if not answer_error:
            try:
                executed = execute_patch(repo_path, contract, normalized_diff)
                if isinstance(executed, EditExecution) and agent_fixture_intact is not None:
                    executed = replace(executed, fixture_intact=executed.fixture_intact and agent_fixture_intact)
                # Test doubles and the shared edit executor both expose the same
                # serialization contract; do not couple this adapter to one
                # concrete execution value type.
                execution = (
                    dict(executed.as_dict()) if callable(getattr(executed, "as_dict", None)) else dict(vars(executed))
                )
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                infra_error = f"{type(exc).__name__}: {exc}"
                execution = {**execution, "error": infra_error}
    # Declared field on CodexParseResult (default 0). The old getattr fallback
    # silently substituted a different metric, codemap_calls, whenever an object
    # lacked the attribute — which only ever happened for an incomplete test double.
    codemap_observed_calls = parsed.codemap_observed_calls
    codemap_used = codemap_observed_calls > 0
    contaminated = arm == "A_plain" and codemap_used
    compliance = {
        "A_plain": not contaminated,
        "B_auto": True,
        "C_strict": parsed.codemap_skill_compact_successful_calls > 0,
    }[arm]
    if isinstance(contract, EditTaskContract) and not answer_error:
        if not isinstance(executed, EditExecution):
            raise TypeError("patch executor must return EditExecution")
        scored = score_edit_execution(contract, build_patch_answer(normalized_diff), executed)
        path_ok = scored.changed_path_boundary_passed
        primary = scored.primary_correct
        pooling_eligible = scored.pooling_eligible
    else:
        path_ok = set(execution.get("changed_paths", ())) == set(contract.expected_paths)
        baseline_failed = execution.get("baseline_failed", execution.get("baseline_target_failed", False))
        primary = bool(
            baseline_failed and execution.get("patch_applied", False) and execution.get("targeted_test_passed", False)
        )
        # Mirrors the Patch stage exactly: `primary_correct` asks only whether the target
        # was fixed, and regression safety enters through pooling eligibility
        # (edit_patch_contracts `pooling_eligible = primary and safety and path`). A patch
        # that fixes its target while breaking every regression is no longer pooled.
        # Vacuously true for a contract declaring no regression commands, so this moves
        # no existing score.
        safety_passed = execution.get("regression_test_passed", True) is True
        pooling_eligible = primary and safety_passed and path_ok
    strict_query_conformance = (
        None if arm != "C_strict" else list(query_arguments[contract.task_id]) in parsed.successful_query_arguments
    )
    return {
        "task_id": contract.task_id,
        "arm": arm,
        "success": parsed.success
        and not answer_error
        and not infra_error
        and not contaminated
        and bool(execution["cleanup_verified"]),
        "primary_correct": primary,
        "pooling_eligible": bool(
            pooling_eligible
            and execution["cleanup_verified"]
            and not answer_error
            and not infra_error
            and compliance is not False
            and strict_query_conformance is not False
        ),
        "changed_path_boundary_passed": path_ok,
        "answer_error": answer_error,
        "infra_error": infra_error,
        "patch_wire_normalized": bool(
            captured_diff is None and match is not None and not answer_error and normalized_diff != candidate_diff
        ),
        "patch_transport": "agent_worktree" if captured_diff is not None else "response_envelope",
        # Persisted so an offline rescore can replay the live scoring inputs exactly.
        # It is observed in the agent's own workspace and is not recoverable from the
        # task item, so omitting it made a rescore score a different question.
        "agent_fixture_intact": agent_fixture_intact,
        "execution": execution,
        "input_tokens": parsed.input_tokens,
        "cached_input_tokens": parsed.cached_input_tokens,
        "fresh_input_tokens": fresh_input_tokens(parsed.input_tokens, parsed.cached_input_tokens),
        "token_accounting_inconsistent": token_accounting_inconsistent(parsed.input_tokens, parsed.cached_input_tokens),
        "output_tokens": parsed.output_tokens,
        "reasoning_output_tokens": parsed.reasoning_output_tokens,
        "tool_result_tokens": parsed.tool_result_tokens,
        "command_calls": parsed.command_calls,
        "tool_elapsed_s": parsed.tool_elapsed_s,
        "codemap_used": codemap_used,
        "codemap_calls": parsed.codemap_calls,
        "codemap_observed_calls": codemap_observed_calls,
        "codemap_successful_calls": parsed.codemap_successful_calls,
        "codemap_direct_compact_successful_calls": parsed.codemap_direct_compact_successful_calls,
        "codemap_skill_compact_successful_calls": parsed.codemap_skill_compact_successful_calls,
        "codemap_errors": parsed.codemap_errors,
        "skill_delivery_observed": parsed.skill_delivery_observed,
        "successful_query_arguments": parsed.successful_query_arguments,
        "compliance": compliance,
        "strict_query_conformance": strict_query_conformance,
        "contaminated": contaminated,
        "output_text": parsed.output_text,
        "raw_events": parsed.raw_events,
        "raw_events_sha256": hashlib.sha256(
            json.dumps(parsed.raw_events, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "provider_binding": dict(_provider_binding(item)),
    }


def parse_fix_single_cell(
    stream: str | bytes,
    *,
    arm: str,
    item: Mapping[str, Any],
    skill_path: Path | None,
    repo_path: Path,
    captured_diff: str | None = None,
) -> dict[str, Any]:
    """Parse and score one Fix-Single native cell."""
    return _parse_patch_cell(
        stream,
        arm=arm,
        item=item,
        skill_path=skill_path,
        repo_path=repo_path,
        captured_diff=captured_diff,
        agent_fixture_intact=None,
        answer_re=_FIX_SINGLE_ANSWER_RE,
        query_arguments=_FIX_SINGLE_QUERY_ARGUMENTS,
        execute_patch=execute_fix_single_patch,
    )


def parse_fix_multi_cell(
    stream: str | bytes,
    *,
    arm: str,
    item: Mapping[str, Any],
    skill_path: Path | None,
    repo_path: Path,
    captured_diff: str | None = None,
) -> dict[str, Any]:
    """Parse and score one Fix-Multi native cell."""
    return _parse_patch_cell(
        stream,
        arm=arm,
        item=item,
        skill_path=skill_path,
        repo_path=repo_path,
        captured_diff=captured_diff,
        agent_fixture_intact=None,
        answer_re=_FIX_MULTI_ANSWER_RE,
        query_arguments=_FIX_MULTI_QUERY_ARGUMENTS,
        execute_patch=execute_fix_multi_patch,
    )


def parse_patch_cell(
    stream: str | bytes,
    *,
    arm: str,
    item: Mapping[str, Any],
    skill_path: Path | None,
    repo_path: Path,
    captured_diff: str | None = None,
    agent_fixture_intact: bool | None = None,
    patch_test_runtime: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Parse a historical Patch cell and score it with the shared fixture oracle."""
    return _parse_patch_cell(
        stream,
        arm=arm,
        item=item,
        skill_path=skill_path,
        repo_path=repo_path,
        captured_diff=captured_diff,
        agent_fixture_intact=agent_fixture_intact,
        answer_re=_PATCH_ANSWER_RE,
        query_arguments=_PATCH_QUERY_ARGUMENTS,
        execute_patch=lambda source, contract, diff: execute_patch_task_answer(
            source,
            contract,
            build_patch_answer(diff),
            index_path=_patch_index_path(source, contract.task_id),
            runtime_identity=patch_test_runtime,
        ),
    )


def _stage_input_recovery(repo_path: Path) -> str:
    """Return the least-destructive recovery for a failed executable input preflight."""
    if repo_path.resolve() == _MANAGED_PARITY_REPO:
        return (
            "The managed target's Git metadata is invalid or its locked index is stale. Preserve it by moving it aside, "
            "then let the no-model launcher reconstruct and verify the canonical pair:\n"
            f"REPO_PATH={shlex.quote(str(repo_path))}\n"
            'mv "$REPO_PATH" "$REPO_PATH.invalid-$(date -u +%Y%m%dT%H%M%SZ)"\n'
            "bash benchmarks/run-all.sh codex --struct --tasks=FN-02 --dry-run"
        )
    return "Use the managed canonical repository and its manifest-locked index, then rerun this stage with --dry-run."


def _stage_source_binding(
    repo_path: Path, index_path: Path, index_relocation: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Validate and fingerprint the frozen source/index pair before paid admission.

    A run in its own worktree carries the locked graph by relocation rather than by byte identity, so the provenance
    travels into the same admission the canonical run uses. The fingerprint still records the bytes actually present,
    which is what binds this stage's scope to this run's inputs.
    """
    structural = _structural()
    try:
        structural._validate_locked_runtime(
            repo_path, index_path, "C_strict", PARITY_MANIFEST_PATH, None, index_relocation
        )
    except ValueError as exc:
        raise ValueError(
            f"executable-stage input preflight failed: {exc}\n"
            "No paid model call was started. " + _stage_input_recovery(repo_path)
        ) from exc
    return {
        "repo_path": str(repo_path.resolve()),
        "repo_sha256": hashlib.sha256(structural._repo_sha(repo_path).encode("utf-8")).hexdigest(),
        "index_path": str(index_path.resolve()),
        "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(PARITY_MANIFEST_PATH.read_bytes()).hexdigest(),
    }


def _patch_index_path(repo_path: Path, task_id: str) -> Path:
    """Return the immutable historical index paired with one Patch baseline."""
    return repo_path / ".cache" / "codemap" / "patch" / f"{task_id}.json"


def _patch_stage_source_binding(repo_path: Path, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Fingerprint every historical Patch baseline/index coordinate before admission."""
    structural = _structural()
    try:
        bindings = validate_patch_index_bundle(repo_path, PATCH_INDEX_LOCKS_PATH, [item["contract"] for item in tasks])
    except ValueError as exc:
        raise ValueError(
            f"patch-stage input preflight failed: {exc}.\n"
            "No paid model call was started. Rebuild the frozen patch coordinates, then rerun with --dry-run."
        ) from exc
    return {
        "repo_path": str(repo_path.resolve()),
        "repo_sha256": hashlib.sha256(structural._repo_sha(repo_path).encode("utf-8")).hexdigest(),
        "patch_coordinates": bindings,
        "manifest_sha256": hashlib.sha256(PARITY_MANIFEST_PATH.read_bytes()).hexdigest(),
    }


def _resolve_scope(tasks: list[dict[str, Any]], model: str, source_binding: Mapping[str, Any]) -> dict[str, Any]:
    """Bind one selected executable stage to immutable contracts, scorer, and source inputs."""
    if model not in runtime.SUPPORTED_CODEX_MODELS:
        raise ValueError(f"supported Codex benchmark model required: {', '.join(runtime.SUPPORTED_CODEX_MODELS)}")
    payload: dict[str, Any] = {
        "arms": ARMS,
        "model": model,
        "stage_runner_sha256": _file_sha256(Path(__file__)),
        "invocation_launcher_sha256": _file_sha256(STRUCTURAL_PATH),
        "lifecycle_sha256": _file_sha256(BENCHMARKS / "_bench_codex" / "runtime.py"),
        "implementation_sha256": {
            "stage_fix": _file_sha256(Path(__file__)),
            "structural_runner": _file_sha256(STRUCTURAL_PATH),
            "paid_lifecycle": _file_sha256(BENCHMARKS / "_bench_common" / "paid_lifecycle.py"),
        },
        "task_ids": [item["contract"].task_id for item in tasks],
        "contracts": {item["contract"].task_id: dict(_provider_binding(item)) for item in tasks},
        "source_binding": dict(source_binding),
    }
    if "patch_coordinates" in source_binding:
        payload["implementation_sha256"].update(
            {
                "edit_patch_contracts": _file_sha256(BENCHMARKS / "_bench_common" / "edit_patch_contracts.py"),
                "mutation_isolation": _file_sha256(BENCHMARKS / "_bench_common" / "mutation_isolation.py"),
                "patch_index_locks": _file_sha256(PATCH_INDEX_LOCKS_PATH),
            }
        )
        payload["patch_test_runtime"] = patch_test_runtime_identity()
    payload["scope_sha256"] = _sha256(payload)
    return payload


def _require_paid_stage_request(
    *,
    study: str,
    repo_path: Path,
    task_ids: list[str],
    model: str,
    auth_source: Path | None,
    run_dir: Path | None,
    paid_approval: str | None,
    expected_scope: str,
    explicit_selection: bool = True,
) -> None:
    """Fail closed with the exact recovery command for an invalid paid request.

    Scope approval binds the runner, shared lifecycle, task contracts, and model. A code or contract change therefore
    intentionally invalidates an earlier approval before a model call can start.
    """
    missing = [
        flag
        for flag, present in (
            ("--auth-source", auth_source is not None),
            ("--run-dir", run_dir is not None),
            ("--paid-approval", paid_approval is not None),
        )
        if not present
    ]
    if missing:
        raise ValueError(
            f"cannot start paid {study} run; missing {', '.join(missing)}.\n"
            "Required form: --auth-source <auth.json> --run-dir <new-run-dir> "
            "--paid-approval <fresh-scope>."
        )
    approval = None if paid_approval is None else str(paid_approval)
    if paid_approval_matches(approval, expected_scope):
        return
    task_selector = f" --tasks {','.join(task_ids)}" if explicit_selection else ""
    raise ValueError(
        f"paid {study} approval does not match the current immutable scope.\n"
        f"received --paid-approval: {approval}\n"
        f"current scope: {expected_scope}\n"
        "The runner, lifecycle, or task contract changed after the earlier approval. "
        "No model call was made. Run the no-model preflight, then copy its emitted PAID_COMMAND exactly:\n"
        f"python3 benchmarks/run-codex-structural.py --repo-path {repo_path} "
        f"--model {model}{task_selector} --dry-run\n"
        "Do not reuse the previous --paid-approval value."
    )


def _suggested_run_dir(study: str) -> Path:
    """Return a non-reserved, collision-resistant relative result directory for a paid command."""
    return Path("benchmarks") / "results" / f"codex-{study}-{uuid4().hex[:12]}"


def _repo_relative_argument(path: PurePath) -> str:
    """Render one repo-relative path as the emitted paid command will be typed.

    The printed command is a shell line whose repo-relative arguments are read by the
    runner as forward-slash paths. A native rendering would embed the host separator and
    hand a Windows operator a command the runner cannot resolve.

    Args:
        path: Repo-relative path to place in the emitted command.

    Returns:
        The path in POSIX form.

    Examples:
        >>> from pathlib import PurePosixPath, PureWindowsPath
        >>> _repo_relative_argument(PureWindowsPath(r"benchmarks\\results\\codex-fix"))
        'benchmarks/results/codex-fix'
        >>> _repo_relative_argument(PurePosixPath("benchmarks/results/codex-fix"))
        'benchmarks/results/codex-fix'
    """
    return PurePath(path).as_posix()


def _print_paid_command(
    *,
    study: str,
    repo_path: Path,
    index_path: Path,
    marketplace_root: Path,
    codemap_bin: Path,
    model: str,
    task_ids: list[str],
    scope_sha256: str,
    explicit_selection: bool,
    patch_pytest: str | None = None,
) -> None:
    """Print the exact user-owned paid command admitted by a successful no-model preflight."""
    run_dir = _suggested_run_dir(study)
    tasks = ",".join(task_ids)
    prefix = f"{PATCH_PYTEST_ENV}={shlex.quote(patch_pytest)} " if patch_pytest else ""
    lines = [
        f"{prefix}python3 benchmarks/run-codex-structural.py \\",
        f"  --repo-path {repo_path.resolve()} \\",
        f"  --index-path {index_path.resolve()} \\",
        f"  --marketplace-root {marketplace_root.resolve()} \\",
        f"  --codemap-bin {codemap_bin.resolve()} \\",
        f"  --model {model} \\",
    ]
    if explicit_selection:
        lines.append(f"  --tasks {tasks} \\")
    lines.append('  --auth-source "$HOME/.codex/auth.json" \\')
    lines.append(f"  --run-dir {_repo_relative_argument(run_dir)} \\")
    lines.append(f"  --paid-approval {paid_approval_token(scope_sha256)}")
    print(format_paid_command_block(lines))


def resolve_fix_single_scope(
    tasks: list[dict[str, Any]], model: str, source_binding: Mapping[str, str]
) -> dict[str, Any]:
    """Resolve Fix-Single's paid-execution scope authorization."""
    return _resolve_scope(tasks, model, source_binding)


def resolve_fix_multi_scope(
    tasks: list[dict[str, Any]], model: str, source_binding: Mapping[str, str]
) -> dict[str, Any]:
    """Resolve Fix-Multi's paid-execution scope authorization."""
    return _resolve_scope(tasks, model, source_binding)


def resolve_patch_scope(tasks: list[dict[str, Any]], model: str, source_binding: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve Patch paid execution against every immutable historical coordinate."""
    return _resolve_scope(tasks, model, source_binding)


def _load_fix_stage(
    study: str, selected: set[str] | None
) -> tuple[
    list[dict[str, Any]],
    Path,
    Callable[[str, Mapping[str, Any]], str],
    Callable[..., dict[str, Any]],
    Callable[[Any, Mapping[str, Any]], None],
]:
    """Load one executable stage and its native prompt, parser, and validator."""
    if study == "fix-single":
        # `is None` on purpose: an explicitly empty selection means "no tasks", but
        # truthiness turned it into "every task in the suite".
        selected = (
            {str(task["id"]) for task in load_task_suite(FIX_SINGLE_TASKS_PATH)} if selected is None else selected
        )
        return (
            load_fix_single_tasks(FIX_SINGLE_TASKS_PATH, selected),
            FIX_SINGLE_TASKS_PATH,
            fix_single_prompt,
            parse_fix_single_cell,
            validate_fix_single_binding,
        )
    if study == "fix-multi":
        selected = {str(task["id"]) for task in load_task_suite(FIX_MULTI_TASKS_PATH)} if selected is None else selected
        return (
            load_fix_multi_tasks(FIX_MULTI_TASKS_PATH, selected),
            FIX_MULTI_TASKS_PATH,
            fix_multi_prompt,
            parse_fix_multi_cell,
            validate_fix_multi_binding,
        )
    if study == "patch":
        selected = {str(task["id"]) for task in load_task_suite(PATCH_TASKS_PATH)} if selected is None else selected
        return (
            load_patch_tasks(PATCH_TASKS_PATH, selected),
            PATCH_TASKS_PATH,
            patch_prompt,
            parse_patch_cell,
            _validate_binding,
        )
    raise ValueError("executable stage must be 'fix-single', 'fix-multi', or 'patch'")


def resolve_fix_stage_scope(
    *,
    study: str,
    repo_path: Path,
    selected: set[str] | None,
    model: str,
    index_path: Path | None = None,
    index_relocation: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve one executable stage without constructing a model adapter.

    ``index_relocation`` is present only for a run outside the canonical clone, whose index is the locked graph with its
    scan root moved; scope resolution admits it the same way execution does.
    """
    tasks, _, _, _, _ = _load_fix_stage(study, selected)
    index_path = index_path or repo_path / ".cache" / "codemap" / f"{repo_path.name}.json"
    source_binding = (
        _patch_stage_source_binding(repo_path, tasks)
        if study == "patch"
        else _stage_source_binding(repo_path, index_path, index_relocation)
    )
    return _resolve_scope(tasks, model, source_binding)


def format_executable_result_row(row: Mapping[str, Any], completed: int, total: int) -> str:
    """Render one executable cell row while retaining detailed evidence on disk."""
    elapsed_s = row.get("elapsed_s")
    elapsed_text = runtime.fmt_time(float(elapsed_s)) if isinstance(elapsed_s, (int, float)) else "?"
    execution = row["execution"]
    recovery = ""
    if execution.get("recount_recoverable", False):
        recovery = " recount=✓"
        if execution.get("recount_oracle_passed") is not None:
            recovery += f" recount-oracle={'✓' if execution['recount_oracle_passed'] else '✗'}"
    quality = format_quality(1.0 if row["primary_correct"] else 0.0)
    return (
        f"({completed}/{total}) {'✓' if row['pooling_eligible'] else '✗'}  {row['task_id']} {row['arm']:<8} "
        f"in={runtime.fmt_tok(row['input_tokens']):>6} out={runtime.fmt_tok(row['output_tokens']):>5} cmd={row['command_calls']:>2} "
        f"time={elapsed_text:>5} quality={quality} "
        f"patch={'✓' if execution['patch_applied'] else '✗'} oracle={'✓' if execution['targeted_test_passed'] else '✗'} "
        f"codemap={'✓' if row['codemap_used'] else '✗'}{recovery}"
    )


def _print_executable_result_row(row: Mapping[str, Any], *, completed: int, total: int) -> None:
    """Route executable stage progress through the shared interactive arm renderer."""
    runtime.print_arm_row(format_executable_result_row(row, completed, total), str(row["arm"]))


def execute_executable_agent_cell(
    *,
    adapter: Any,
    source_repo: Path,
    source_index: Path,
    baseline_commit: str,
    native_arm: str,
    arm: str,
    prompt: str,
    item: Mapping[str, Any],
    parser: Callable[..., dict[str, Any]],
    index_relocation: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one writable agent cell and score the captured canonical Git diff.

    ``index_relocation`` is this run's own provenance when it works outside the canonical clone; it names the locked
    origin the cell's disposable copy still descends from. A historical Patch baseline keeps its own locked index hash.
    """
    structural = _structural()
    contract = item.get("contract")
    is_patch_task = isinstance(contract, EditTaskContract)
    frozen_origin = None if index_relocation is None else str(index_relocation["frozen_index_sha256"])
    workspace = (
        create_executable_agent_workspace(
            source_repo,
            source_index,
            baseline_commit,
            require_source_baseline=False,
        )
        if is_patch_task
        else structural.create_executable_agent_workspace(
            source_repo, source_index, baseline_commit, frozen_index_sha256=frozen_origin
        )
    )
    patch_workspace = None
    home = None
    row: dict[str, Any] | None = None
    started = time.monotonic()
    captured_diff = ""
    changed_paths: tuple[str, ...] = ()
    index_unchanged = False
    try:
        with structural.bind_executable_agent_workspace(adapter, workspace):
            home_kwargs: dict[str, Any] = {
                "writable_workspace": workspace.worktree,
                "denied_workspace": source_repo,
                "index_relocation": workspace.index_relocation,
            }
            historical_coordinate = item.get("historical_runtime_coordinate")
            if historical_coordinate is not None:
                if not isinstance(historical_coordinate, Mapping):
                    raise ValueError("Patch task historical runtime coordinate is malformed")
                home_kwargs["historical_runtime_coordinate"] = historical_coordinate
            home = adapter.prepare_verified_home(native_arm, **home_kwargs)
            if is_patch_task:
                patch_workspace = stage_patch_task_agent_workspace(
                    source_repo,
                    workspace,
                    contract,
                    runtime_identity=item.get("patch_test_runtime"),
                )
            stream = adapter.run_stream(adapter.build_command(prompt), home.env, working_directory=workspace.worktree)
            captured_diff = (
                patch_workspace.capture_answer().diff if patch_workspace is not None else workspace.capture_diff()
            )
            changed_paths = workspace.changed_paths()
            parser_kwargs: dict[str, Any] = {
                "arm": arm,
                "item": item,
                "skill_path": home.codemap_skill_path if arm == "C_strict" else None,
                "repo_path": source_repo,
                "captured_diff": captured_diff,
            }
            if patch_workspace is not None:
                parser_kwargs["agent_fixture_intact"] = patch_workspace.fixture_intact()
                parser_kwargs["patch_test_runtime"] = item.get("patch_test_runtime")
            row = parser(stream, **parser_kwargs)
            index_unchanged = workspace.index_unchanged()
    finally:
        try:
            if home is not None:
                if home.coordination_path is not None:
                    structural.cleanup_coordination_root(home.coordination_path)
                home.cleanup()
        finally:
            workspace_cleanup_verified = workspace.cleanup()
    source_unchanged = (
        patch_workspace.source_unchanged()
        if patch_workspace is not None
        else not structural._git_porcelain_status(source_repo) and structural._repo_sha(source_repo) == baseline_commit
    )
    if row is None:
        raise RuntimeError("executable agent cell did not produce a scoreable result")
    row.update(
        {
            "elapsed_s": time.monotonic() - started,
            "captured_diff": captured_diff,
            "captured_diff_sha256": hashlib.sha256(captured_diff.encode("utf-8")).hexdigest(),
            "agent_workspace": {
                "changed_paths": list(changed_paths),
                "cleanup_verified": workspace_cleanup_verified,
                "index_relocation": dict(workspace.index_relocation),
                "index_unchanged": index_unchanged,
                "source_unchanged": source_unchanged,
            },
        }
    )
    if not workspace_cleanup_verified or not source_unchanged or not index_unchanged:
        reason = next(
            value
            for value, condition in (
                ("agent workspace cleanup failed", not workspace_cleanup_verified),
                ("source repository changed", not source_unchanged),
                ("derived frozen index changed", not index_unchanged),
            )
            if condition
        )
        row["success"] = row["primary_correct"] = row["pooling_eligible"] = False
        row["answer_error"] = "; ".join(value for value in (row["answer_error"], reason) if value)
    return row


def preflight_executable_agent_workspace(
    adapter: Any,
    *,
    source_repo: Path,
    source_index: Path,
    baseline_commit: str,
    allow_historical_baseline: bool = False,
    historical_runtime_coordinate: Mapping[str, str] | None = None,
    patch_test_runtime: Mapping[str, str] | None = None,
    patch_contract: EditTaskContract | None = None,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Validate writable-worktree permissions for every arm without a model call.

    Historical Patch preflight supplies the reviewed per-task baseline/index coordinate; ordinary executable stages
    continue to use the active manifest. ``index_relocation`` is this run's own provenance when it works outside the
    canonical clone, and it names the locked origin the stage worktree's copy still descends from.
    """
    if patch_test_runtime is not None and dict(patch_test_runtime) != patch_test_runtime_identity():
        raise ValueError("Patch task pytest runtime changed after scope admission")
    structural = _structural()
    frozen_origin = None if index_relocation is None else str(index_relocation["frozen_index_sha256"])
    workspace = (
        # A historical Patch baseline carries its own locked index hash, which this run's relocation
        # says nothing about, so that branch keeps hashing the bytes it was handed.
        create_executable_agent_workspace(source_repo, source_index, baseline_commit, require_source_baseline=False)
        if allow_historical_baseline
        else structural.create_executable_agent_workspace(
            source_repo, source_index, baseline_commit, frozen_index_sha256=frozen_origin
        )
    )
    try:
        for native_arm in NATIVE_ARMS.values():
            with structural.bind_executable_agent_workspace(adapter, workspace):
                home_kwargs: dict[str, Any] = {
                    "writable_workspace": workspace.worktree,
                    "denied_workspace": source_repo,
                    "index_relocation": workspace.index_relocation,
                }
                if historical_runtime_coordinate is not None:
                    home_kwargs["historical_runtime_coordinate"] = historical_runtime_coordinate
                home = adapter.prepare_verified_home(native_arm, **home_kwargs)
            # The home is prepared purely to prove it can be, then torn down. The empty
            # `try: pass` / `finally:` that used to wrap this teardown expressed nothing.
            if home.coordination_path is not None:
                structural.cleanup_coordination_root(home.coordination_path)
            home.cleanup()
        if patch_contract is not None:
            stage_patch_task_agent_workspace(
                source_repo,
                workspace,
                patch_contract,
                runtime_identity=patch_test_runtime,
            )
    finally:
        if not workspace.cleanup():
            raise RuntimeError("executable agent workspace preflight cleanup failed")


def _preflight_stage_workspaces(
    adapter: Any,
    *,
    study: str,
    repo_path: Path,
    default_index_path: Path,
    tasks: list[dict[str, Any]],
    report_progress: Callable[[int, int, str, str], None] | None = None,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Preflight task baselines and every distinct workspace coordinate without a model call."""
    pairs: set[tuple[str, Path]] = set()
    coordinates: list[tuple[dict[str, Any], Path]] = []
    for item in tasks:
        contract = item["contract"]
        source_index = _patch_index_path(repo_path, contract.task_id) if study == "patch" else default_index_path
        pair = (contract.baseline_commit, source_index)
        if pair in pairs:
            continue
        pairs.add(pair)
        coordinates.append((item, source_index))
    for position, (item, source_index) in enumerate(coordinates, start=1):
        contract = item["contract"]
        if report_progress is not None:
            report_progress(position, len(coordinates), contract.task_id, "start")
        preflight_executable_agent_workspace(
            adapter,
            source_repo=repo_path,
            source_index=source_index,
            baseline_commit=contract.baseline_commit,
            allow_historical_baseline=study == "patch",
            historical_runtime_coordinate=(item.get("historical_runtime_coordinate") if study == "patch" else None),
            patch_test_runtime=item.get("patch_test_runtime") if study == "patch" else None,
            patch_contract=contract if study == "patch" else None,
            index_relocation=index_relocation,
        )
        if report_progress is not None:
            report_progress(position, len(coordinates), contract.task_id, "complete")


def run_fix_stage(
    *,
    study: str,
    repo_path: Path,
    selected: set[str] | None,
    dry_run: bool,
    resolve_scope: bool,
    auth_source: Path | None,
    run_dir: Path | None,
    paid_approval: str | None,
    model: str,
    index_path: Path | None = None,
    marketplace_root: Path | None = None,
    codemap_bin: Path | None = None,
    emit_authorization: bool = True,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Run one complete executable study or an explicitly selected task subset."""
    explicit_selection = selected is not None
    tasks, suite_path, prompt, parser, _validate = _load_fix_stage(study, selected)
    index_path = index_path or repo_path / ".cache" / "codemap" / f"{repo_path.name}.json"
    source_binding = (
        _patch_stage_source_binding(repo_path, tasks)
        if study == "patch"
        else _stage_source_binding(repo_path, index_path, index_relocation)
    )
    if study == "patch":
        patch_coordinates = source_binding.get("patch_coordinates")
        if not isinstance(patch_coordinates, Mapping):
            raise ValueError("patch-stage input preflight has no historical runtime coordinates")
        for item in tasks:
            task_id = item["contract"].task_id
            coordinate = patch_coordinates.get(task_id)
            if not isinstance(coordinate, Mapping):
                raise ValueError(f"patch-stage input preflight has no historical runtime coordinate for {task_id}")
            item["historical_runtime_coordinate"] = dict(coordinate)
    admitted = _resolve_scope(tasks, model, source_binding)
    if study == "patch":
        patch_test_runtime = admitted.get("patch_test_runtime")
        if not isinstance(patch_test_runtime, Mapping):
            raise ValueError("patch-stage scope has no designated pytest runtime")
        for item in tasks:
            item["patch_test_runtime"] = dict(patch_test_runtime)
    if resolve_scope:
        print(json.dumps(admitted, sort_keys=True))
        return
    if not dry_run:
        _require_paid_stage_request(
            study=study,
            repo_path=repo_path,
            task_ids=[item["contract"].task_id for item in tasks],
            model=model,
            auth_source=auth_source,
            run_dir=run_dir,
            paid_approval=paid_approval,
            expected_scope=admitted["scope_sha256"],
            explicit_selection=explicit_selection,
        )
    adapter = _structural().CodexRunner(
        model,
        repo_path,
        index_path=index_path,
        marketplace_root=marketplace_root or ROOT,
        codemap_bin=codemap_bin or ROOT / "plugins/codemap-py/bin/codemap-py",
        auth_source=auth_source,
        index_relocation=index_relocation,
    )
    if dry_run:
        try:

            def report_preflight_progress(position: int, total: int, task_id: str, status: str) -> None:
                """Render long-running Patch baseline admission without changing its outcome."""
                if study != "patch":
                    return
                if status == "start":
                    print(f"PREFLIGHT {position}/{total} {task_id} validating frozen baseline and tests...")
                else:
                    print(f"PREFLIGHT {position}/{total} {task_id} ✓")

            _preflight_stage_workspaces(
                adapter,
                study=study,
                repo_path=repo_path,
                default_index_path=index_path,
                tasks=tasks,
                report_progress=report_preflight_progress,
                index_relocation=index_relocation,
            )
        finally:
            adapter.close()
        for item in tasks:
            for arm in ARMS:
                runtime.print_plan_row(f"PLAN    {item['contract'].task_id} {arm}")
        if emit_authorization:
            print(f"SCOPE   {admitted['scope_sha256']}")
            _print_paid_command(
                study=study,
                repo_path=repo_path,
                index_path=index_path,
                marketplace_root=marketplace_root or ROOT,
                codemap_bin=codemap_bin or ROOT / "plugins/codemap-py/bin/codemap-py",
                model=model,
                task_ids=[item["contract"].task_id for item in tasks],
                scope_sha256=admitted["scope_sha256"],
                explicit_selection=explicit_selection,
                patch_pytest=(str(admitted["patch_test_runtime"]["pytest_executable"]) if study == "patch" else None),
            )
        return
    if run_dir is None:
        raise RuntimeError("run_dir must not be None")
    try:
        _preflight_stage_workspaces(
            adapter,
            study=study,
            repo_path=repo_path,
            default_index_path=index_path,
            tasks=tasks,
            index_relocation=index_relocation,
        )
    except BaseException:
        adapter.close()
        raise

    def prepare_run(destination: Path) -> None:
        """Archive immutable stage inputs before the first native cell."""
        shared_files = (
            _patch_snapshot_files(repo_path, tasks)
            if study == "patch"
            else {"codex-runtime.py": BENCHMARKS / "_bench_codex" / "runtime.py"}
        )
        adapter.create_input_snapshot(
            destination,
            tasks_path=suite_path,
            manifest_path=PARITY_MANIFEST_PATH,
            runner_path=Path(__file__),
            invocation_launcher_path=STRUCTURAL_PATH,
            tasks=[item["task"] for item in tasks],
            arms=tuple(NATIVE_ARMS.values()),
            additional_shared_files=shared_files,
        )
        if study == "patch":
            (destination / "inputs" / "patch-runtime.json").write_text(
                json.dumps(admitted["patch_test_runtime"], indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    def run_cell(item: Mapping[str, Any], arm: str) -> Mapping[str, Any]:
        """Execute one stage-owned task and arm inside the shared lifecycle."""
        return execute_executable_agent_cell(
            adapter=adapter,
            source_repo=repo_path,
            source_index=(_patch_index_path(repo_path, item["contract"].task_id) if study == "patch" else index_path),
            baseline_commit=item["contract"].baseline_commit,
            native_arm=NATIVE_ARMS[arm],
            arm=arm,
            prompt=prompt(arm, item["task"]),
            item=item,
            parser=parser,
            index_relocation=index_relocation,
        )

    def persist_metadata(path: Path, payload: Mapping[str, Any]) -> None:
        """Write the current executable-stage state as canonical JSON."""
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def emit_lifecycle(event: str, values: Mapping[str, Any]) -> None:
        """Print concise lifecycle evidence without adding ANSI escape codes."""
        if event == "artifacts":
            print(
                runtime.presentation.format_artifact_block(
                    telemetry=values["telemetry_path"], metadata=values["metadata_path"]
                )
            )
        else:
            print(
                f"SUMMARY  status={values['status']}  persisted_cells={values['persisted_cells']}/{values['total_cells']}"
            )

    run_paid_stage(
        tasks=tasks,
        arms=ARMS,
        run_dir=run_dir,
        metadata={"stage_id": study, "scope": admitted},
        callbacks=PaidStageCallbacks(
            run_cell=run_cell,
            validate_row=lambda item, _arm, row: _validate_stage_binding(study, item, row["provider_binding"]),
            prepare_run=prepare_run,
            persist_metadata=persist_metadata,
            emit_lifecycle=emit_lifecycle,
            emit_row=lambda row, completed, total, _arm: _print_executable_result_row(
                row, completed=completed, total=total
            ),
            write_checksums=write_checksums,
            close_adapter=adapter.close,
        ),
    )
    print(f"done: {run_dir}")


def rescore_fix_stage(source_dir: Path, output_dir: Path, repo_path: Path, *, study: str | None = None) -> Path:
    """Reparse one checksummed executable stage, inferring its persisted scorer."""
    if output_dir.exists():
        raise FileExistsError(f"rescore output already exists: {output_dir}")
    verify_checksums(source_dir)
    source_metadata = json.loads((source_dir / "run-metadata.json").read_text(encoding="utf-8"))
    if source_metadata.get("status") != "completed":
        raise ValueError("only completed executable-stage runs can be rescored")
    persisted_stage = source_metadata.get("stage_id")
    if study is None:
        study = persisted_stage
    if study not in {"fix-single", "fix-multi", "patch"} or persisted_stage not in {None, study}:
        raise ValueError("offline executable rescore requires one consistent fix stage_id")
    source_rows = [
        json.loads(line) for line in (source_dir / "telemetry.jsonl").read_text(encoding="utf-8").splitlines() if line
    ]
    if not source_rows:
        raise ValueError("executable-stage result has no telemetry rows")
    selected = {str(row.get("task_id")) for row in source_rows}
    if study == "fix-single":
        loader, suite_path, parser = load_fix_single_tasks, FIX_SINGLE_TASKS_PATH, parse_fix_single_cell
    elif study == "fix-multi":
        loader, suite_path, parser = load_fix_multi_tasks, FIX_MULTI_TASKS_PATH, parse_fix_multi_cell
    else:
        loader, suite_path, parser = load_patch_tasks, PATCH_TASKS_PATH, parse_patch_cell
    tasks = {item["contract"].task_id: item for item in loader(suite_path, selected)}
    for source_row in source_rows:
        captured_diff = source_row.get("captured_diff")
        if not isinstance(captured_diff, str) or not captured_diff.startswith("diff --git "):
            raise ValueError("source executable-stage telemetry lacks a captured Git diff")
    output_dir.mkdir(parents=True)
    print(
        runtime.presentation.format_artifact_block(
            telemetry=output_dir / "telemetry.jsonl",
            metadata=output_dir / "run-metadata.json",
        )
    )
    persisted = 0
    # A crash mid-write used to leave the run directory with no run-metadata.json at
    # all, so a half-written rescore was indistinguishable from one that never ran.
    # The readcrop sibling already had this shape; mirror it here.
    try:
        with (output_dir / "telemetry.jsonl").open("x", encoding="utf-8") as output:
            for source_row in source_rows:
                task_id, arm, raw_events = (
                    source_row.get("task_id"),
                    source_row.get("arm"),
                    source_row.get("raw_events"),
                )
                if task_id not in tasks or arm not in ARMS or not isinstance(raw_events, list):
                    raise ValueError("source executable-stage telemetry lacks a replayable task, arm, or native events")
                item = tasks[task_id]
                _validate_stage_binding(study, item, source_row.get("provider_binding", {}))
                skill_path = (
                    source_dir / "inputs/C_strict/codemap-py/codex-skills/query-code/SKILL.md"
                    if arm == "C_strict"
                    else None
                )
                if skill_path is not None and not skill_path.is_file():
                    raise ValueError("source executable-stage snapshot lacks the frozen Codemap Skill")
                captured_diff = source_row["captured_diff"]
                reparse_kwargs: dict[str, Any] = {}
                if study == "patch":
                    # The live run passes both of these; a rescore that omitted them was
                    # re-executing the oracle against different inputs and could therefore
                    # PASS a cell the live run FAILED.
                    reparse_kwargs["agent_fixture_intact"] = source_row.get("agent_fixture_intact")
                    reparse_kwargs["patch_test_runtime"] = item.get("patch_test_runtime")
                row = parser(
                    (json.dumps(event, sort_keys=True) for event in raw_events),
                    arm=arm,
                    item=item,
                    skill_path=skill_path,
                    repo_path=repo_path,
                    captured_diff=captured_diff,
                    **reparse_kwargs,
                )
                for field in ("input_tokens", "cached_input_tokens", "output_tokens", "tool_result_tokens"):
                    if source_row.get(field) != row[field]:
                        raise ValueError(f"source executable-stage native {field} changed during reparse")
                # The divergence guard covered only token fields, so a rescore could silently
                # publish a different verdict than the run it claims to reproduce.
                for field in ("primary_correct", "pooling_eligible", "success"):
                    if field in source_row and source_row.get(field) != row[field]:
                        raise ValueError(f"source executable-stage {field} changed during reparse")
                row["elapsed_s"] = source_row.get("elapsed_s")
                row["source_telemetry_row_sha256"] = hashlib.sha256(
                    json.dumps(source_row, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                output.write(json.dumps(row, sort_keys=True) + "\n")
                output.flush()
                persisted += 1
                _print_executable_result_row(row, completed=persisted, total=len(source_rows))
        (output_dir / "run-metadata.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "kind": f"offline-{study}-reparse-v2",
                    "source_run_dir": str(source_dir.resolve()),
                    "source_checksums_sha256": hashlib.sha256(
                        (source_dir / "checksums.sha256").read_bytes()
                    ).hexdigest(),
                    "rescorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "persisted_cells": persisted,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        write_checksums(output_dir)
    except BaseException:
        (output_dir / "run-metadata.json").write_text(
            json.dumps(
                {"status": "failed", "kind": f"offline-{study}-reparse-v2", "persisted_cells": persisted},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"SUMMARY  status=failed  persisted_cells={persisted}/{len(source_rows)}")
        write_checksums(output_dir)
        raise
    print(f"SUMMARY  status=completed  persisted_cells={persisted}/{len(source_rows)}")
    return output_dir
