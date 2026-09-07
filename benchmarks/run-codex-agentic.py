#!/usr/bin/env python3
"""Codex agentic provider-parity runner for the locked shared agentic suite.

This module runs every manifest-locked agentic task in the A_plain, B_auto, and C_strict treatments. It reuses the
provider-neutral answer-contract scorer and the shared Codex native JSONL parser instead of copying either
implementation.

``--dry-run`` validates the locked shared provenance and prints the deterministic task × arm cell plan without reading
credentials, invoking a model, or creating result files. Paid execution is separately admitted only by the agentic
manifest and exact SHA approval; the structural manifest remains an isolated runtime adapter, never the agentic study
definition.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping, NoReturn, Sequence

_BENCHMARKS_DIR = Path(__file__).resolve().parent
if str(_BENCHMARKS_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_DIR))

from _bench_common.presentation import (  # noqa: E402
    LEGEND_CLOSE_RULE,
    LEGEND_OPEN_RULE,
    benchmark_console,
    fmt_time,
    fmt_tok,
    format_probe_row,
    print_arm_row,
    print_legend,
)
from _bench_codex import runtime as codex_runtime  # noqa: E402
from _bench_common.mutation_isolation import (  # noqa: E402
    load_index_relocation,
    verify_index_relocation,
)
from _bench_common.agentic_contracts import (  # noqa: E402
    AGENTIC_ARMS,
    DEFAULT_REPETITIONS,
    assess_answer_response,
    build_oracle,
    materialize_agentic_prompt,
    parse_labeled_answer as parse_labeled_answer,
    score_answer,
    score_evidence_metrics,
    validate_answer_contract,
)
from _bench_common.provider_parity_contracts import (  # noqa: E402
    ARM_CONTRACTS,
    canonical_result_rows,
    canonical_task_hash,
    fresh_input_tokens,
    semantic_suite_hash,
    token_accounting_inconsistent,
    treatment_adherence,
)


_TASKS_PATH = _BENCHMARKS_DIR / "suites" / "tasks-agentic.json"
_MANIFEST_PATH = _BENCHMARKS_DIR / "manifests" / "codex-agentic.json"
AGENTIC_DEFAULT_REPETITIONS = DEFAULT_REPETITIONS
_NATIVE_HOME_ARM = {
    "A_plain": "A_plain",
    "B_auto": "B_auto",
    "C_strict": "C_strict",
}
#: Legend body lines without their framing rules, so a terminal can panel them while the run log
#: keeps the plain framed form it has always archived.
_LEGEND_BODY = (
    "  treatments: A_plain=no Codemap, B_auto=CLI available and optional, "
    "C_strict=installed Codemap Skill with compact query required",
    "  metrics:",
    "      SCORE: mean semantic answer-component score; n/a when no answer can be recovered (higher is better)",
    "      EREC: expected-importer recall in all agent text (higher is better)",
    "      RREC: expected-importer recall in the final report (higher is better)",
    "      DEFF: unbounded expected-importer exposure hits per command (higher is better within the same task)",
    "  answer: ✓ strict envelope, △ diagnostic bare-JSON recovery (not poolable), ✗ absent or invalid",
    "  status: ✓ completed, ✗ failed",
    "  progress: N completed cells / manifest-scoped planned cells",
    "  treatment: ✓ assigned arm followed, ✗ assigned arm not followed",
    "  codemap-used: ✓ Codemap call observed; ✗ no call observed (A_plain expects none)",
    "  input tokens: gross total; cached and fresh details remain in telemetry only (lower is better at equal quality)",
)
_OUTPUT_LEGEND = "\n".join((LEGEND_OPEN_RULE, *_LEGEND_BODY, LEGEND_CLOSE_RULE))


def _load_sibling(module_name: str, filename: str) -> ModuleType:
    """Load one hyphenated benchmark sibling once by its stable local path."""
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        return loaded
    path = _BENCHMARKS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark sibling {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_structural = _load_sibling("_codex_agentic_structural", "run-codex-structural.py")


@dataclass(frozen=True)
class ArmProbe:
    """No-model evidence that one agentic treatment has the required isolation."""

    arm: str
    codemap_available: bool
    skill_required: bool


@dataclass
class AgenticRun:
    """Normalized no-model Codex agentic evidence for one completed native stream."""

    arm: str
    task_id: str
    repetition: int
    success: bool
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    command_calls: int
    codemap_calls: int
    codemap_successful_calls: int
    codemap_used: bool
    compliance: bool | None
    treatment_adherence: bool
    contaminated: bool
    incomplete: bool
    malformed_lines: int
    error: str
    error_type: str
    output_text: str
    report_text: str
    quality: Any | None
    evidence: Any | None = None
    answer_error: str = ""
    answer_contract_valid: bool | None = None
    diagnostic_only: bool = False
    answer_pooling_eligible: bool = False
    elapsed_s: float = 0.0
    retry_count: int = 0
    raw_events: list[dict[str, Any]] | None = None
    native_attempt_events: list[list[dict[str, Any]]] | None = None
    launcher_path: str | None = None


def probe_arm(arm: str) -> ArmProbe:
    """Return pure isolated-home policy evidence for one supported treatment.

    Actual homes are created only by the future paid runner after agentic admission; this pure preflight cannot touch
    credentials, plugin installation, or model state.  The availability assertions are still explicit and fail closed.
    """
    if arm not in AGENTIC_ARMS:
        raise ValueError(f"unsupported Codex agentic arm {arm!r}")
    if arm == "A_plain":
        return ArmProbe(arm, codemap_available=False, skill_required=False)
    return ArmProbe(arm, codemap_available=True, skill_required=arm == "C_strict")


def prepare_isolated_home(arm: str, **kwargs: Any) -> Any:
    """Create the structural runner's disposable home for a future paid cell.

    The underlying A/B/C homes already prevent host-plugin and credential inheritance. B_auto maps only to the home with
    direct Codemap availability; optional use remains an agentic admission rule, not a home capability. No-model dry
    runs probe the policy without creating a disposable home.
    """
    if arm not in AGENTIC_ARMS:
        raise ValueError(f"unsupported Codex agentic arm {arm!r}")
    return _structural.prepare_arm_home(_NATIVE_HOME_ARM[arm], **kwargs)


def probe_isolated_home(home: Any, arm: str) -> dict[str, Any]:
    """Probe one runner-owned home and reject capability drift before a paid cell."""
    expected = probe_arm(arm)
    evidence = _structural.probe_arm_home(home)
    if bool(evidence.get("codemap_available")) != expected.codemap_available:
        raise ValueError(f"{arm} isolated-home Codemap availability drifted")
    return evidence


def load_agentic_tasks(tasks_path: Path = _TASKS_PATH, manifest_path: Path = _MANIFEST_PATH) -> list[dict[str, Any]]:
    """Load every manifest-locked task after validating suite and task identities."""
    tasks_path = Path(tasks_path)
    manifest_path = Path(manifest_path)
    try:
        suite = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("agentic task suite is unavailable or malformed") from exc
    raw_tasks = suite.get("tasks") if isinstance(suite, Mapping) else None
    if not isinstance(raw_tasks, list) or not all(isinstance(task, dict) for task in raw_tasks):
        raise ValueError("agentic task suite requires a task object list")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        suite_lock = manifest["suite"]
        task_locks = manifest["tasks"]
        scope = manifest["preregistered_scope"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("agentic shared manifest is unavailable or malformed") from exc
    if not isinstance(task_locks, list) or not isinstance(scope, Mapping):
        raise ValueError("agentic shared manifest lacks task identities or scope")
    task_locks_by_id = {task.get("id"): task for task in task_locks if isinstance(task, Mapping)}
    task_ids = scope.get("task_ids")
    if not isinstance(task_ids, list) or task_ids != [task.get("id") for task in raw_tasks]:
        raise ValueError("agentic shared task order drifted")
    if (
        suite_lock.get("path") != "benchmarks/suites/tasks-agentic.json"
        or suite_lock.get("raw_sha256") != hashlib.sha256(tasks_path.read_bytes()).hexdigest()
        or suite_lock.get("semantic_suite_sha256") != semantic_suite_hash(raw_tasks)
    ):
        raise ValueError("agentic shared suite identity drifted")
    if set(task_locks_by_id) != set(task_ids):
        raise ValueError("agentic shared task identity set drifted")

    tasks: list[dict[str, Any]] = []
    for raw_task in raw_tasks:
        task_id = str(raw_task["id"])
        task_lock = task_locks_by_id[task_id]
        delivered_prompt = materialize_agentic_prompt(raw_task)
        if (
            task_lock.get("canonical_task_sha256") != canonical_task_hash(raw_task)
            or task_lock.get("prompt_sha256") != hashlib.sha256(delivered_prompt.encode("utf-8")).hexdigest()
        ):
            raise ValueError(f"agentic task identity drifted for {task_id}")
        if raw_task.get("type") != "blast_radius_analysis" or not task_lock.get("effective_scoreable"):
            raise ValueError(f"agentic task {task_id} must retain scoreable blast-radius semantics")
        validate_answer_contract(raw_task)
        tasks.append({**raw_task, "prompt": delivered_prompt})
    return tasks


def parse_agentic_stream(
    stream: str | bytes | Iterable[str | bytes],
    *,
    arm: str,
    task: Mapping[str, Any],
    oracle: Any | None = None,
    repetition: int = 1,
    skill_path: Path | None = None,
    launcher_path: Path | None = None,
    allow_equivalent_launcher: bool = False,
    ground_truth: Any | None = None,
) -> AgenticRun:
    """Normalize one native stream and score it with the shared Claude oracle.

    B_auto has optional Codemap use.  C_strict preserves a completed no-call row but records compliance/adherence false;
    aggregation must exclude that coordinate rather than erasing its raw evidence.
    """
    if arm not in AGENTIC_ARMS:
        raise ValueError(f"unsupported Codex agentic arm {arm!r}")
    if not isinstance(task.get("id"), str) or not task["id"]:
        raise ValueError("agentic task requires a non-empty id")
    if oracle is None:
        oracle = ground_truth
    if oracle is None:
        raise ValueError("agentic stream scoring requires a shared task oracle")
    if repetition < 1:
        raise ValueError("repetition must be at least 1")
    if arm == "C_strict" and skill_path is None:
        raise ValueError("C_strict requires the exact installed Codemap Skill path")
    if arm != "C_strict" and skill_path is not None:
        raise ValueError("only C_strict accepts a Codemap Skill path")

    skill_bytes = skill_path.read_bytes() if skill_path is not None else b""
    parsed = codex_runtime.parse_codex_jsonl(
        stream,
        launcher_path=launcher_path,
        skill_path=skill_path,
        skill_sha256=hashlib.sha256(skill_bytes).hexdigest() if skill_bytes else "",
        allow_equivalent_launcher=allow_equivalent_launcher,
    )
    codemap_used = getattr(parsed, "codemap_observed_calls", parsed.codemap_calls) > 0
    contaminated = arm == "A_plain" and codemap_used
    compliance = None
    if arm == "C_strict":
        compliance = bool(parsed.codemap_skill_compact_successful_calls > 0)
    adherence_arm = arm
    adherence = treatment_adherence(
        adherence_arm,
        codemap_use_compliance=compliance,
        contaminated=contaminated,
    )
    report_text = parsed.output_text[parsed.last_tool_text_offset :].lstrip("\n")
    quality: Any | None = None
    evidence: Any | None = None
    answer_error = ""
    answer_contract_valid: bool | None = None
    diagnostic_only = False
    answer_pooling_eligible = False
    if parsed.success:
        assessment = assess_answer_response(task, report_text)
        evidence = score_evidence_metrics(
            oracle,
            exposure_text=parsed.output_text,
            report_text=report_text,
            tool_calls=parsed.command_calls,
        )
        answer_error = assessment.error or ""
        answer_contract_valid = assessment.strict_envelope_valid
        diagnostic_only = assessment.diagnostic_only
        answer_pooling_eligible = assessment.pooling_eligible
        if assessment.answer is not None:
            quality = score_answer(
                oracle,
                assessment.answer,
                exposure_text=parsed.output_text,
                report_text=report_text,
                tool_calls=parsed.command_calls,
            )
    return AgenticRun(
        arm=arm,
        task_id=str(task["id"]),
        repetition=repetition,
        success=parsed.success,
        input_tokens=parsed.input_tokens,
        cached_input_tokens=parsed.cached_input_tokens,
        output_tokens=parsed.output_tokens,
        command_calls=parsed.command_calls,
        codemap_calls=parsed.codemap_calls,
        codemap_successful_calls=parsed.codemap_successful_calls,
        codemap_used=codemap_used,
        compliance=compliance,
        treatment_adherence=adherence,
        contaminated=contaminated,
        incomplete=parsed.incomplete,
        malformed_lines=parsed.malformed_lines,
        error=parsed.error or answer_error,
        error_type=parsed.error_type if not answer_error else "answer_contract_failed",
        output_text=parsed.output_text,
        report_text=report_text,
        quality=quality,
        evidence=evidence,
        answer_error=answer_error,
        answer_contract_valid=answer_contract_valid,
        diagnostic_only=diagnostic_only,
        answer_pooling_eligible=answer_pooling_eligible,
        raw_events=parsed.raw_events,
        launcher_path=str(launcher_path.resolve()) if launcher_path is not None else None,
    )


def _read_agentic_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load the dedicated agentic manifest as one validated JSON object."""
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Codex agentic manifest is unavailable or malformed") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Codex agentic manifest must be a JSON object")
    return manifest


def _manifest_sha256(manifest_path: Path) -> str:
    """Return the exact bytes hash required for explicit paid authorization."""
    try:
        return hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("Codex agentic manifest is unavailable") from exc


def resolve_agentic_model(manifest: Mapping[str, Any], manifest_path: Path, model: str | None) -> str | None:
    """Return the declared stratum this run replaces the manifest default with, or ``None``.

    The manifest names one default stratum and declares the others the same locked suite may run. Naming the default
    explicitly is still the default, so one physical study never has two identities: only a different declared stratum
    becomes an override, and only an override changes the scope hash and the approval the run demands.

    Args:
        manifest: Parsed Codex agentic manifest.
        manifest_path: Path the declared strata are validated against.
        model: Requested stratum name, or ``None`` to keep the manifest default.

    Returns:
        The overriding stratum name, or ``None`` when this run keeps the manifest default.

    Raises:
        ValueError: When the manifest lacks a model block, or the name is not a declared stratum.

    Examples:
        >>> resolve_agentic_model({"model": {"name": "gpt-x"}}, _MANIFEST_PATH, None) is None
        True
        >>> resolve_agentic_model({"model": {"name": "gpt-x"}}, _MANIFEST_PATH, "gpt-x") is None
        True
    """
    configured = manifest.get("model")
    if not isinstance(configured, Mapping) or not isinstance(configured.get("name"), str):
        raise ValueError("Codex agentic manifest lacks a model stratum")
    if model is None or model == configured["name"]:
        return None
    codex_runtime.validate_codex_stratum(str(model), str(configured.get("reasoning_effort", "")), Path(manifest_path))
    return str(model)


def _execution_order_contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Read the manifest-bound schedule policy, retaining historic fixed-order studies.

    A frozen result without this field predates order balancing. It remains interpretable under its original lexical
    order; only a new manifest may opt into the cyclic schedule used for future variance screening.
    """
    declared = manifest.get("execution_order")
    if declared is None:
        return {
            "strategy": "legacy-fixed-arm-order-v1",
            "arm_cycle": list(AGENTIC_ARMS),
            "index_basis": "task-order-then-repetition",
        }
    if not isinstance(declared, Mapping):
        raise ValueError("Codex agentic manifest execution order must be an object")
    contract = dict(declared)
    if (
        contract.get("strategy") != "cyclic-arm-order-v1"
        or contract.get("arm_cycle") != list(AGENTIC_ARMS)
        or contract.get("index_basis") != "locked-task-ordinal-plus-repetition-minus-one"
    ):
        raise ValueError("Codex agentic manifest has an unsupported execution-order contract")
    return contract


def _scheduled_coordinates(
    task_ids: Sequence[str],
    *,
    repetitions: int,
    cyclic: bool,
    task_ordinals: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Return the immutable execution order for one selected agentic scope.

    Cyclic rotation places each treatment first, second, and third exactly once per task across three repetitions. A
    single screening repetition is still useful diagnostic evidence but is explicitly not position-balanced alone.
    """
    coordinates: list[dict[str, Any]] = []
    for selected_ordinal, task_id in enumerate(task_ids):
        task_ordinal = task_ordinals.get(task_id, selected_ordinal) if task_ordinals is not None else selected_ordinal
        for repetition in range(1, repetitions + 1):
            rotation = (task_ordinal + repetition - 1) % len(AGENTIC_ARMS) if cyclic else 0
            arm_order = (*AGENTIC_ARMS[rotation:], *AGENTIC_ARMS[:rotation])
            coordinates.extend({"task_id": task_id, "repetition": repetition, "arm": arm} for arm in arm_order)
    return coordinates


def resolve_agentic_scope(
    manifest_path: Path = _MANIFEST_PATH,
    *,
    task_ids: Sequence[str] | None = None,
    repetitions: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Resolve and hash one manifest-bound Codex agentic coordinate scope.

    The manifest is the immutable source lock. A caller may explicitly narrow its ordered task set, increase positive
    repetitions, or select another declared model stratum, but the derived scope hash binds every resulting coordinate,
    its per-cell timeout, and the stratum that runs it.
    """
    manifest_path = Path(manifest_path)
    manifest = _read_agentic_manifest(manifest_path)
    model_override = resolve_agentic_model(manifest, manifest_path, model)
    preregistered = manifest.get("preregistered_scope")
    if not isinstance(preregistered, Mapping):
        raise ValueError("Codex agentic manifest lacks preregistered scope")
    repetitions = preregistered.get("repetitions") if repetitions is None else repetitions
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("agentic repetitions must be at least 1")
    locked_task_ids = preregistered.get("task_ids")
    if not isinstance(locked_task_ids, list) or not all(isinstance(task_id, str) for task_id in locked_task_ids):
        raise ValueError("Codex agentic manifest has invalid task IDs")
    selected_task_ids = list(locked_task_ids if task_ids is None else task_ids)
    if not selected_task_ids or len(set(selected_task_ids)) != len(selected_task_ids):
        raise ValueError("agentic scope requires unique manifest-bound task IDs")
    if any(task_id not in locked_task_ids for task_id in selected_task_ids):
        raise ValueError("agentic scope includes a task outside the manifest")
    ordered_task_ids = [task_id for task_id in locked_task_ids if task_id in set(selected_task_ids)]
    coordinate_timeout_seconds = preregistered.get("coordinate_timeout_seconds")
    if type(coordinate_timeout_seconds) is not int or coordinate_timeout_seconds < 1:
        raise ValueError("Codex agentic manifest must lock a positive per-cell timeout")
    total_cells = len(ordered_task_ids) * len(AGENTIC_ARMS) * repetitions
    execution_order = _execution_order_contract(manifest)
    coordinates = _scheduled_coordinates(
        ordered_task_ids,
        repetitions=repetitions,
        cyclic=execution_order["strategy"] == "cyclic-arm-order-v1",
        task_ordinals={task_id: ordinal for ordinal, task_id in enumerate(locked_task_ids)},
    )
    payload = {
        "manifest_sha256": _manifest_sha256(manifest_path),
        "experiment_revision": manifest.get("experiment_revision"),
        "task_ids": ordered_task_ids,
        "arms": list(AGENTIC_ARMS),
        "repetitions": repetitions,
        "coordinate_timeout_seconds": coordinate_timeout_seconds,
        "total_cells": total_cells,
        "nonpoolable": True,
        "execution_order": execution_order,
        "coordinates": coordinates,
    }
    # The default stratum leaves the payload as the manifest locked it, so a run that selects nothing keeps the
    # identity its manifest digest already authorizes; a selected stratum is a distinct study and hashes as one.
    if model_override is not None:
        payload["model"] = model_override
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "scope_sha256": hashlib.sha256(encoded).hexdigest()}


def validate_paid_admission(manifest_path: Path, approval_sha256: str) -> dict[str, Any]:
    """Fail closed unless the agentic manifest admits the default paid study."""
    manifest_path = Path(manifest_path)
    observed_hash = _manifest_sha256(manifest_path)
    if approval_sha256 != observed_hash:
        raise ValueError("paid Codex agentic execution requires the exact current manifest SHA-256 approval")
    manifest = _read_agentic_manifest(manifest_path)
    scope = manifest.get("preregistered_scope")
    model = manifest.get("model")
    admission = manifest.get("admission")
    if not isinstance(scope, Mapping) or not isinstance(model, Mapping) or not isinstance(admission, Mapping):
        raise ValueError("Codex agentic manifest lacks admission, model, or scope")
    task_ids = scope.get("task_ids")
    arms = scope.get("arms")
    repetitions = scope.get("repetitions")
    coordinate_timeout = scope.get("coordinate_timeout_seconds")
    expected_cells = (
        len(task_ids) * len(arms) * repetitions
        if isinstance(task_ids, list)
        and task_ids
        and all(isinstance(task_id, str) for task_id in task_ids)
        and len(task_ids) == len(set(task_ids))
        and isinstance(arms, list)
        and arms == list(AGENTIC_ARMS)
        and type(repetitions) is int
        and repetitions > 0
        and type(coordinate_timeout) is int
        and coordinate_timeout > 0
        else None
    )
    if (
        manifest.get("schema_version") != "codex-agentic-manifest-v1"
        or admission.get("paid_execution") != "admitted"
        or expected_cells is None
        or scope.get("total_cells") != expected_cells
        or not isinstance(model.get("name"), str)
        or not model.get("name")
        or not isinstance(model.get("reasoning_effort"), str)
        or not model.get("reasoning_effort")
        or model.get("strict_config") is not True
    ):
        raise ValueError("Codex agentic manifest does not admit the default shared paid scope")
    artifact_hashes = manifest.get("artifact_sha256")
    if not isinstance(artifact_hashes, Mapping):
        raise ValueError("Codex agentic manifest lacks artifact hashes")
    launcher_hash = artifact_hashes.get("run_all")
    if not isinstance(launcher_hash, str) or len(launcher_hash) != 64:
        raise ValueError("Codex agentic manifest lacks the locked run-all launcher hash")
    current_runner_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if artifact_hashes.get("codex_agentic_runner") != current_runner_hash:
        raise ValueError("Codex agentic runner bytes differ from the admitted manifest")
    return manifest


def _agentic_envelope(arm: str, task: Mapping[str, Any]) -> str:
    """Return the arm instruction and labelled answer contract for one task."""
    if arm == "A_plain":
        treatment = "Codemap is absent and inaccessible. Solve using ordinary provider tools only."
    elif arm == "B_auto":
        treatment = (
            "Codemap's direct CLI is available as $CODEMAP_BIN. Use it when useful, but ordinary reads and shell "
            "tools remain allowed and no Codemap call is required."
        )
    elif arm == "C_strict":
        treatment = (
            "The installed Codemap Skill is bound immutably for this treatment. Use its smallest complete-query "
            "guidance, then complete at least one standalone successful compact query through $CODEMAP_BIN or the "
            "exact installed Codemap launcher; do not prefix, assign, wrap, or combine the credited query with shell work. "
            "Other reads and shell commands remain allowed."
        )
    else:
        raise ValueError(f"unsupported Codex agentic arm {arm!r}")
    return treatment


class AgenticCodexRunner:
    """Execute agentic cells through the structural runner's isolated native homes.

    The dedicated agentic manifest controls agentic semantics and admission.  The structural manifest named in its
    ``runtime_isolation`` section is used only to obtain the existing disposable permissions, plugin, and credential
    path.
    """

    #: Relocation provenance for a run in its own worktree; ``None`` at the canonical managed clone.
    index_relocation: dict[str, str] | None = None

    def __init__(
        self,
        *,
        repo_path: Path,
        index_path: Path,
        marketplace_root: Path | None,
        codemap_bin: Path | None,
        auth_source: Path | None,
        adapter_manifest_path: Path,
        agentic_manifest: Mapping[str, Any],
        agentic_manifest_path: Path | None = None,
        index_relocation: Mapping[str, str] | None = None,
        model_name: str | None = None,
        transport: Callable[..., str | bytes | Iterable[str | bytes]] | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.index_path = Path(index_path).resolve()
        self.agentic_manifest = agentic_manifest
        self.agentic_manifest_path = Path(agentic_manifest_path or _MANIFEST_PATH)
        self.index_relocation = dict(index_relocation) if index_relocation is not None else None
        self.transport = transport
        model = agentic_manifest.get("model")
        scope = agentic_manifest.get("preregistered_scope")
        if not isinstance(model, Mapping) or not isinstance(scope, Mapping):
            raise ValueError("Codex agentic manifest lacks model or per-cell timeout")
        self.model_name = str(
            resolve_agentic_model(agentic_manifest, self.agentic_manifest_path, model_name) or model["name"]
        )
        self.adapter = _structural.CodexRunner(
            self.model_name,
            self.repo_path,
            reasoning_effort=str(model["reasoning_effort"]),
            index_path=self.index_path,
            timeout=float(scope["coordinate_timeout_seconds"]),
            marketplace_root=marketplace_root,
            codemap_bin=codemap_bin,
            manifest_path=adapter_manifest_path,
            index_relocation=self.index_relocation,
            auth_source=auth_source,
        )

    def close(self) -> None:
        """Release the adapter's private credential chain."""
        self.adapter.close()

    def preflight_snapshot_bound_admission(self) -> None:
        """Exercise initial and later snapshot-bound B/C admission without transport."""
        with tempfile.TemporaryDirectory(prefix="codex-agentic-dry-run-") as temporary_root:
            run_dir = Path(temporary_root)
            self.create_input_snapshot(
                run_dir,
                manifest_path=self.agentic_manifest_path,
                invocation_launcher_path=_BENCHMARKS_DIR / "run-all.sh",
            )
            for arm in ("B_auto", "C_strict"):
                home = self.adapter._prepare_verified_home(arm)
                try:
                    _structural.probe_arm_home(home)
                finally:
                    if home.coordination_path is not None:
                        _structural._cleanup_coordination_root(home.coordination_path)
                    home.cleanup()

    def _postflight(self, home: Any | None) -> str:
        """Return a concrete error when a native attempt changed locked runtime state."""
        try:
            _validate_agentic_runtime(self.agentic_manifest, self.repo_path, self.index_path, self.index_relocation)
            if home is not None and home.coordination_path is not None:
                _structural._validate_coordination_root(home.coordination_path)
        except ValueError as exc:
            return str(exc)
        return ""

    def create_input_snapshot(
        self, run_dir: Path, *, manifest_path: Path, invocation_launcher_path: Path
    ) -> dict[str, Any]:
        """Archive only immutable, non-secret agentic inputs and verified runtime bytes."""
        snapshot_root = Path(run_dir) / "inputs"
        if snapshot_root.exists():
            raise FileExistsError(snapshot_root)
        # Reserve evidence before the first disposable home so an admission
        # failure survives the home cleanup that records it.
        self.adapter._runtime_evidence_path = Path(run_dir) / "runtime-isolation.jsonl"
        self.adapter._runtime_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        self.adapter._runtime_evidence_path.touch(exist_ok=False)
        self.adapter._runtime_evidence_path.chmod(0o600)
        homes: list[Any] = []
        arm_archives: dict[str, dict[str, Path]] = {}
        arm_files: dict[str, dict[str, Path]] = {}
        operation_error: BaseException | None = None
        try:
            for arm in AGENTIC_ARMS:
                home = self.adapter._prepare_verified_home(_NATIVE_HOME_ARM[arm])
                homes.append(home)
                arm_files[arm] = {"config.toml": home.path / "config.toml"}
                if arm == "B_auto":
                    arm_archives[arm] = {"direct-cli": home.path / "direct-cli"}
                elif arm == "C_strict":
                    if home.codemap_plugin_path is None or home.codex_rig_path is None:
                        raise RuntimeError("C_strict snapshot lacks verified Codemap or Codex Rig package")
                    arm_archives[arm] = {"codemap-py": home.codemap_plugin_path, "codex-rig": home.codex_rig_path}
                    if home.codemap_context_path is not None:
                        arm_files[arm]["codemap-context.json"] = home.codemap_context_path
                    self.adapter._record_runtime_success(_NATIVE_HOME_ARM[arm], home)
            snapshot = _write_agentic_input_snapshot(
                snapshot_root,
                manifest_path=Path(manifest_path),
                tasks_path=_TASKS_PATH,
                runner_path=Path(__file__),
                invocation_launcher_path=invocation_launcher_path,
                index_path=self.index_path,
                auth_source=self.adapter.auth_source,
                arm_archives=arm_archives,
                arm_files=arm_files,
            )
            if isinstance(snapshot.get("path"), str):
                self.adapter._bind_runtime_snapshot(
                    snapshot_root,
                    {
                        "B_auto": {"direct-cli": snapshot_root / "B_auto" / "direct-cli"},
                        "C_strict": {
                            "codemap-py": snapshot_root / "C_strict" / "codemap-py",
                            "codex-rig": snapshot_root / "C_strict" / "codex-rig",
                        },
                    },
                )
            return snapshot
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            cleanup_errors: list[BaseException] = []
            cleaned_coordination_paths: set[Path] = set()
            for home in homes:
                try:
                    if self.adapter._auth_state is not None and home.auth_provisioned:
                        self.adapter._auth_state.refresh_from_home(home.path)
                except BaseException as exc:
                    cleanup_errors.append(exc)
                try:
                    coordination_path = home.coordination_path
                    if coordination_path is not None and coordination_path not in cleaned_coordination_paths:
                        cleaned_coordination_paths.add(coordination_path)
                        _structural._cleanup_coordination_root(coordination_path)
                except BaseException as exc:
                    cleanup_errors.append(exc)
                try:
                    home.cleanup()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if operation_error is None and cleanup_errors:
                raise cleanup_errors[0]

    def run(
        self,
        task: Mapping[str, Any],
        arm: str,
        *,
        repetition: int,
        oracle: Any | None = None,
        ground_truth: Any | None = None,
    ) -> AgenticRun:
        """Run one agentic coordinate, retrying only empty retryable transport failures."""
        if oracle is None:
            oracle = ground_truth
        if oracle is None:
            raise ValueError("agentic runner requires a shared task oracle")
        home: Any | None = None
        started = time.monotonic()
        attempts: list[list[dict[str, Any]]] = []
        result: AgenticRun | None = None
        postflight_error = ""
        auth_state_error = ""
        cleanup_error = ""
        if self.transport is None:
            home = self.adapter._prepare_verified_home(_NATIVE_HOME_ARM[arm])
        try:
            command = self.adapter.build_command(f"{_agentic_envelope(arm, task)}\n\n{task['prompt']}")
            for attempt in range(3):
                remaining = self.adapter.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    timeout_stream = json.dumps({"type": "error", "error": "cell timeout", "error_type": "timeout"})
                    result = parse_agentic_stream(
                        timeout_stream,
                        arm=arm,
                        task=task,
                        oracle=oracle,
                        repetition=repetition,
                        skill_path=home.codemap_skill_path if arm == "C_strict" and home is not None else None,
                        launcher_path=getattr(home, "codemap_launcher_path", None),
                        allow_equivalent_launcher=True,
                    )
                    break
                if self.transport is None:
                    assert home is not None
                    stream = self.adapter._subprocess(command, home.env, timeout=remaining)
                else:
                    stream = self.transport(command, arm=arm)
                result = parse_agentic_stream(
                    stream,
                    arm=arm,
                    task=task,
                    oracle=oracle,
                    repetition=repetition,
                    skill_path=home.codemap_skill_path if arm == "C_strict" and home is not None else None,
                    launcher_path=getattr(home, "codemap_launcher_path", None),
                    allow_equivalent_launcher=True,
                )
                attempts.append(result.raw_events or [])
                postflight_error = self._postflight(home)
                if postflight_error:
                    result.incomplete = True
                    result.success = False
                    result.error = f"runtime contamination: {postflight_error}"
                    result.error_type = "runtime_contamination"
                    break
                empty_retryable = (
                    result.input_tokens == 0
                    and result.output_tokens == 0
                    and not result.output_text.strip()
                    and result.error_type in {"turn_failed", "response_failed", "transport_error", "launch_os_error"}
                )
                if not empty_retryable or attempt == 2:
                    break
            assert result is not None
        finally:
            if home is not None:
                try:
                    if self.adapter._auth_state is not None and home.auth_provisioned:
                        self.adapter._auth_state.refresh_from_home(home.path)
                except (RuntimeError, ValueError) as exc:
                    auth_state_error = str(exc)
                try:
                    if home.coordination_path is not None:
                        _structural._cleanup_coordination_root(home.coordination_path)
                except ValueError as exc:
                    cleanup_error = str(exc)
                finally:
                    home.cleanup()
        result.elapsed_s = time.monotonic() - started
        result.retry_count = max(len(attempts) - 1, 0)
        result.native_attempt_events = attempts
        if auth_state_error:
            result.incomplete = True
            result.success = False
            result.error = "run auth state could not be refreshed"
            result.error_type = "authentication_state_failed"
        if cleanup_error:
            result.incomplete = True
            result.success = False
            result.error = f"runner cleanup failed: {cleanup_error}"
            result.error_type = "cleanup_failed"
        if result.contaminated:
            result.success = False
            result.error = result.error or "contaminated"
        return result


def _validate_agentic_runtime(
    manifest: Mapping[str, Any],
    repo_path: Path,
    index_path: Path,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Check the agentic target and frozen index without treating it as structural policy.

    ``index_relocation`` excuses the locked byte hash only: a run in its own worktree proves index identity through
    relocation provenance, while every other admission stays exactly as strict.
    """
    target = manifest.get("target_source")
    index = manifest.get("frozen_index_contract")
    if not isinstance(target, Mapping) or not isinstance(index, Mapping):
        raise ValueError("Codex agentic manifest lacks target or frozen-index contract")
    if _structural._repo_sha(repo_path) != target.get("commit"):
        raise ValueError("agentic Codex run requires the locked target commit")
    if _structural._git_porcelain_status(repo_path):
        raise ValueError("agentic Codex run requires a clean target worktree")
    if not index_path.is_file():
        raise ValueError("agentic Codex run requires the locked frozen index bytes")
    index_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()
    if index_relocation is None and index_sha256 != index.get("raw_sha256"):
        raise ValueError("agentic Codex run requires the locked frozen index bytes")
    try:
        metadata = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("agentic Codex frozen index is unavailable or malformed") from exc
    if index_relocation is not None:
        verify_index_relocation(
            index_relocation,
            metadata=metadata,
            index_sha256=index_sha256,
            repo_path=repo_path,
            frozen_index_sha256=index.get("raw_sha256"),
        )
    if metadata.get("git_sha") != index.get("git_sha") or metadata.get("scan_version") != index.get("scan_version"):
        raise ValueError("agentic Codex frozen index metadata drifted")


def _write_agentic_input_snapshot(
    snapshot_root: Path,
    *,
    manifest_path: Path,
    tasks_path: Path,
    runner_path: Path,
    invocation_launcher_path: Path,
    index_path: Path,
    auth_source: Path | None,
    arm_archives: Mapping[str, Mapping[str, Path]],
    arm_files: Mapping[str, Mapping[str, Path]],
) -> dict[str, Any]:
    """Write immutable non-secret agentic inputs with accurate runner provenance."""
    if snapshot_root.exists():
        raise FileExistsError(snapshot_root)
    snapshot_root.mkdir(parents=True, mode=0o700)
    entries: list[dict[str, Any]] = []
    shared = snapshot_root / "shared"
    for role, source, relative in (
        ("manifest", manifest_path, Path("manifest.json")),
        ("task_suite", tasks_path, Path("tasks-agentic.json")),
        ("agentic_runner", runner_path, Path("run-codex-agentic.py")),
        ("invocation_launcher", invocation_launcher_path, Path("run-all.sh")),
        ("locked_index", index_path, Path("locked-index.json")),
    ):
        _structural._archive_snapshot_file(
            source, shared / relative, role=role, archive_root=snapshot_root, entries=entries
        )
    for arm in AGENTIC_ARMS:
        for relative, source in sorted(arm_files.get(arm, {}).items()):
            _structural._archive_snapshot_file(
                source,
                snapshot_root / arm / relative,
                role=f"{arm}:{relative}",
                archive_root=snapshot_root,
                entries=entries,
            )
        for package_role, root in sorted(arm_archives.get(arm, {}).items()):
            _structural._archive_snapshot_tree(
                root, snapshot_root / arm / package_role, role=f"{arm}:{package_role}", entries=entries
            )
        if arm == "C_strict":
            _structural._write_frozen_marketplace(snapshot_root, arm, entries)
    entries.sort(key=lambda item: (str(item["role"]), str(item["archived_path"])))
    payload = {
        "schema_version": "codex-agentic-input-snapshot-v1",
        "files": entries,
        "auth_source": {"supplied": True, "archived": False} if auth_source is not None else None,
    }
    snapshot_path = snapshot_root / "input-snapshot.json"
    serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    snapshot_path.write_bytes(serialized)
    snapshot_path.chmod(0o600)
    payload["path"] = str(snapshot_path.resolve())
    payload["sha256"] = hashlib.sha256(serialized).hexdigest()
    payload["bytes"] = len(serialized)
    return payload


def _format_probe(probe: ArmProbe) -> str:
    """Render one deterministic no-model treatment probe.

    ``codemap`` stays the measured probe result, so a missing binary still shows as a failure rather than being masked
    by a word describing the contract. ``use`` states the arm's obligation beside it; it supersedes the former ``skill-
    required`` field, which was true on exactly the arm that now reads ``use=required``.
    """
    return format_probe_row(probe.arm, {"codemap": probe.codemap_available, "use": codex_runtime.probe_use(probe.arm)})


def _emit_output_legend(output: Any | None = None) -> None:
    """Emit the agentic legend as a panel on a terminal and as framed plain rules elsewhere.

    The plain branch of the shared helper writes through ``print``, so the destination is redirected for the duration of
    the call; that is a no-op when the destination already is standard output.
    """
    destination = sys.stdout if output is None else output
    with contextlib.redirect_stdout(destination):
        print_legend(_LEGEND_BODY, console=benchmark_console(file=destination))


def _format_plan(task_id: str, repetition: int, arm: str) -> str:
    """Render one deterministic dry-run coordinate."""
    return f"PLAN    {task_id:<5}  rep={repetition}  {arm}"


def dry_run(
    *,
    tasks_path: Path = _TASKS_PATH,
    manifest_path: Path = _MANIFEST_PATH,
    task_ids: Sequence[str] | None = None,
    repetitions: int = AGENTIC_DEFAULT_REPETITIONS,
    model: str | None = None,
) -> list[str]:
    """Validate one resolved scope and return its exact no-model cell plan."""
    scope = resolve_agentic_scope(manifest_path, task_ids=task_ids, repetitions=repetitions, model=model)
    tasks = load_agentic_tasks(tasks_path, manifest_path)
    tasks_by_id = {task["id"]: task for task in tasks}
    if set(AGENTIC_ARMS) != set(ARM_CONTRACTS):
        raise ValueError("agentic arm contracts drifted from the shared provider policy")
    lines = [_format_probe(probe_arm(arm)) for arm in AGENTIC_ARMS]
    for task_id in scope["task_ids"]:
        if task_id not in tasks_by_id:
            raise ValueError(f"agentic scope task {task_id} is not loadable")
    for coordinate in scope["coordinates"]:
        lines.append(_format_plan(coordinate["task_id"], coordinate["repetition"], coordinate["arm"]))
    return lines


# ``main``'s ``--dry-run`` flag binds the name ``dry_run`` inside its body, so the planner is
# reached there through this module-level alias; ``dry_run`` itself stays the public entry point.
_dry_run_plan = dry_run


def _append_telemetry(path: Path, run: AgenticRun, execution_index: int) -> None:
    """Append one immutable raw agentic row before updating derived evidence."""
    row = vars(run).copy()
    if run.quality is not None:
        row["quality"] = {**vars(run.quality), "components": dict(run.quality.components)}
    if run.evidence is not None:
        row["evidence"] = vars(run.evidence)
    row["execution_index"] = execution_index
    row["fresh_input_tokens"] = fresh_input_tokens(run.input_tokens, run.cached_input_tokens)
    row["token_accounting_inconsistent"] = token_accounting_inconsistent(run.input_tokens, run.cached_input_tokens)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_canonical_telemetry(raw_path: Path, task_ids: Sequence[str]) -> str:
    """Publish the derived agentic canonical order without rewriting raw JSONL."""
    rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line]
    canonical = canonical_result_rows(rows, task_order=task_ids, arm_order=AGENTIC_ARMS)
    payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in canonical).encode("utf-8")
    output = raw_path.with_name("telemetry-canonical.jsonl")
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    try:
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def _write_checksums(run_dir: Path) -> None:
    """Refresh result checksums while excluding the separately validated source archive."""
    source_root = run_dir / ".launcher" / "source"
    files = [
        path
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and path.name != "checksums.sha256" and not path.is_relative_to(source_root)
    ]
    payload = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(run_dir).as_posix()}\n" for path in files
    )
    (run_dir / "checksums.sha256").write_text(payload, encoding="utf-8")


def _row_number(row: Mapping[str, Any], field: str) -> float:
    """Read one non-negative numeric telemetry value without fabricating malformed data."""
    value = row.get(field)
    return float(value) if type(value) in {int, float} and value >= 0 else 0.0


def _row_quality(row: Mapping[str, Any]) -> float | None:
    """Return a semantic score only when the persisted result has one."""
    quality = row.get("quality")
    value = quality.get("quality_score") if isinstance(quality, Mapping) else None
    return float(value) if type(value) in {int, float} else None


def _row_fresh_tokens(row: Mapping[str, Any]) -> float | None:
    """Return fresh input only when the row has consistent token accounting."""
    if "fresh_input_tokens" in row:
        value = row.get("fresh_input_tokens")
        return float(value) if type(value) in {int, float} and value >= 0 else None
    gross, cached = row.get("input_tokens"), row.get("cached_input_tokens")
    if type(gross) not in {int, float} or type(cached) not in {int, float} or gross < cached or cached < 0:
        return None
    return float(gross - cached)


def _native_diagnostics(row: Mapping[str, Any]) -> dict[str, int | None | str]:
    """Extract command diagnostics from raw events without treating them as requests.

    Native streams do not promise command start records. In that case concurrency and waves are unavailable rather than
    inferred from completion order.
    """
    raw_events = row.get("raw_events")
    if not isinstance(raw_events, list):
        return {
            "help_discovery_calls": None,
            "captured_output_characters": None,
            "max_concurrent_commands": None,
            "serial_waves": None,
            "concurrency_status": "unavailable_native_events",
        }
    help_calls = 0
    output_characters = 0
    active: set[str] = set()
    starts_observed = False
    command_completions = 0
    max_concurrent = 0
    waves = 0
    for event in raw_events:
        if not isinstance(event, Mapping):
            continue
        item = event.get("item")
        if not isinstance(item, Mapping) or item.get("type") != "command_execution":
            continue
        event_type = event.get("type")
        item_id = item.get("id")
        if event_type == "item.started" and isinstance(item_id, str):
            if not active:
                waves += 1
            active.add(item_id)
            starts_observed = True
            max_concurrent = max(max_concurrent, len(active))
        elif event_type == "item.completed":
            command_completions += 1
            command = item.get("command")
            if isinstance(command, str) and ("help" in command or "doctor" in command):
                help_calls += 1
            output = item.get("aggregated_output", item.get("output", ""))
            if isinstance(output, str):
                output_characters += len(output)
            if isinstance(item_id, str):
                active.discard(item_id)
    has_command_content = command_completions == int(_row_number(row, "command_calls"))
    concurrency_complete = starts_observed and not active
    return {
        "help_discovery_calls": help_calls if has_command_content else None,
        "captured_output_characters": output_characters if has_command_content else None,
        "max_concurrent_commands": max_concurrent if concurrency_complete else None,
        "serial_waves": waves if concurrency_complete else None,
        "concurrency_status": "complete" if concurrency_complete else "unavailable_or_partial_native_events",
    }


def _aggregate_arm_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Total reported telemetry without replacing absent quality with a score."""
    qualities = [quality for row in rows if (quality := _row_quality(row)) is not None]
    cells = len(rows)
    diagnostics = [_native_diagnostics(row) for row in rows]
    concurrency = [value for diagnostic in diagnostics if (value := diagnostic["max_concurrent_commands"]) is not None]
    waves = [value for diagnostic in diagnostics if (value := diagnostic["serial_waves"]) is not None]
    help_counts = [value for diagnostic in diagnostics if (value := diagnostic["help_discovery_calls"]) is not None]
    output_characters = [
        value for diagnostic in diagnostics if (value := diagnostic["captured_output_characters"]) is not None
    ]
    return {
        "cells": cells,
        "gross_input_tokens": int(sum(_row_number(row, "input_tokens") for row in rows)),
        "cached_input_tokens": int(sum(_row_number(row, "cached_input_tokens") for row in rows)),
        "fresh_input_tokens": int(sum(fresh for row in rows if (fresh := _row_fresh_tokens(row)) is not None)),
        "fresh_input_ineligible_cells": sum(_row_fresh_tokens(row) is None for row in rows),
        "output_tokens": int(sum(_row_number(row, "output_tokens") for row in rows)),
        "runtime_seconds": sum(_row_number(row, "elapsed_s") for row in rows),
        "quality_mean": sum(qualities) / len(qualities) if qualities else None,
        "quality_scored_cells": len(qualities),
        "observed_use_cells": sum(row.get("codemap_used") is True for row in rows),
        "adherent_cells": sum(row.get("treatment_adherence") is True for row in rows),
        "native_command_calls": int(sum(_row_number(row, "command_calls") for row in rows)),
        "heuristic_help_discovery_calls": sum(help_counts) if cells and len(help_counts) == cells else None,
        "captured_output_characters": sum(output_characters) if cells and len(output_characters) == cells else None,
        "max_concurrent_commands": max(concurrency) if cells and len(concurrency) == cells else None,
        "serial_waves": sum(waves) if cells and len(waves) == cells else None,
        "native_event_diagnostics_ineligible_cells": sum(
            diagnostic["concurrency_status"] != "complete" for diagnostic in diagnostics
        ),
    }


def _win_loss(left: float | None, right: float | None, *, lower_is_better: bool) -> str:
    """Name one paired comparison outcome without hiding unavailable measurements."""
    if left is None or right is None:
        return "unavailable"
    if left == right:
        return "tie"
    return "C_strict" if (left < right if lower_is_better else left > right) else "A_plain"


def _summarize_telemetry(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Build transparent all-assigned and paired-adherent agentic summaries.

    B_auto remains a diagnostic optional-use canary. A/C comparisons only require adherent A and C cells at the same
    task/repetition; B cannot erase that pair. All-assigned totals retain every recorded cell and its cost.
    """
    by_arm = {arm: [row for row in rows if row.get("arm") == arm] for arm in AGENTIC_ARMS}
    by_coordinate: dict[tuple[str, int], dict[str, Mapping[str, Any]]] = {}
    for row in rows:
        task_id, repetition, arm = row.get("task_id"), row.get("repetition"), row.get("arm")
        if isinstance(task_id, str) and type(repetition) is int and arm in AGENTIC_ARMS:
            by_coordinate.setdefault((task_id, repetition), {})[arm] = row
    paired_a_c = [
        (task_id, repetition, arms)
        for (task_id, repetition), arms in by_coordinate.items()
        if {"A_plain", "C_strict"}.issubset(arms)
        and arms["A_plain"].get("treatment_adherence") is True
        and arms["C_strict"].get("treatment_adherence") is True
    ]
    matched_by_arm = {
        "A_plain": [arms["A_plain"] for _, _, arms in paired_a_c],
        "B_auto": [
            arms["B_auto"] for _, _, arms in paired_a_c if arms.get("B_auto", {}).get("treatment_adherence") is True
        ],
        "C_strict": [arms["C_strict"] for _, _, arms in paired_a_c],
    }
    quality_outcomes = {"C_strict": 0, "A_plain": 0, "tie": 0, "unavailable": 0}
    fresh_outcomes = dict(quality_outcomes)
    by_task: dict[str, dict[str, Any]] = {}
    for task_id, _repetition, arms in paired_a_c:
        quality = _win_loss(_row_quality(arms["C_strict"]), _row_quality(arms["A_plain"]), lower_is_better=False)
        fresh = _win_loss(_row_fresh_tokens(arms["C_strict"]), _row_fresh_tokens(arms["A_plain"]), lower_is_better=True)
        quality_outcomes[quality] += 1
        fresh_outcomes[fresh] += 1
        task_summary = by_task.setdefault(
            task_id,
            {
                "matched_repetitions": 0,
                "quality_win_loss": {key: 0 for key in quality_outcomes},
                "fresh_input_win_loss": {key: 0 for key in fresh_outcomes},
            },
        )
        task_summary["matched_repetitions"] += 1
        task_summary["quality_win_loss"][quality] += 1
        task_summary["fresh_input_win_loss"][fresh] += 1
    return {
        "all_assigned": {arm: _aggregate_arm_rows(by_arm[arm]) for arm in AGENTIC_ARMS},
        "matched_adherent": {arm: _aggregate_arm_rows(matched_by_arm[arm]) for arm in AGENTIC_ARMS},
        "task_comparisons": {
            "matched_a_c_coordinates": len(paired_a_c),
            "quality_win_loss": quality_outcomes,
            "fresh_input_win_loss": fresh_outcomes,
            "by_task": by_task,
        },
        "b_auto_diagnostic": True,
    }


def _summary_lines(summary: Mapping[str, Any]) -> list[tuple[str, str | None]]:
    """Format compact end-of-run rows while keeping arm output on the shared renderer."""
    lines: list[tuple[str, str | None]] = []
    for cohort in ("all_assigned", "matched_adherent"):
        for arm in AGENTIC_ARMS:
            values = summary[cohort][arm]
            score = "n/a" if values["quality_mean"] is None else f"{values['quality_mean']:.3f}"
            fresh = "n/a" if values["fresh_input_ineligible_cells"] else fmt_tok(values["fresh_input_tokens"])
            lines.append(
                (
                    f"SUMMARY  cohort={cohort}  {arm:<10} cells={values['cells']}"
                    f" gross={fmt_tok(values['gross_input_tokens'])} cache={fmt_tok(values['cached_input_tokens'])}"
                    f" fresh={fresh} out={fmt_tok(values['output_tokens'])}"
                    f" SCORE={score} time={fmt_time(values['runtime_seconds'])}"
                    f" use={values['observed_use_cells']}/{values['cells']}"
                    f" adherence={values['adherent_cells']}/{values['cells']}"
                    f" cmd={values['native_command_calls']}"
                    f" help~={values['heuristic_help_discovery_calls'] if values['heuristic_help_discovery_calls'] is not None else 'n/a'}"
                    f" chars={values['captured_output_characters'] if values['captured_output_characters'] is not None else 'n/a'}"
                    f" max-concurrent={values['max_concurrent_commands'] if values['max_concurrent_commands'] is not None else 'n/a'}"
                    f" waves={values['serial_waves'] if values['serial_waves'] is not None else 'n/a'}",
                    arm,
                )
            )
    comparisons = summary["task_comparisons"]
    lines.append(
        (
            "SUMMARY  comparisons=A_plain-vs-C_strict"
            f" matched={comparisons['matched_a_c_coordinates']}"
            f" quality={json.dumps(comparisons['quality_win_loss'], sort_keys=True)}"
            f" fresh={json.dumps(comparisons['fresh_input_win_loss'], sort_keys=True)}"
            " B_auto=diagnostic-optional-use",
            None,
        )
    )
    for task_id, outcomes in sorted(comparisons["by_task"].items()):
        lines.append(
            (
                f"SUMMARY  task={task_id} matched={outcomes['matched_repetitions']}"
                f" quality={json.dumps(outcomes['quality_win_loss'], sort_keys=True)}"
                f" fresh={json.dumps(outcomes['fresh_input_win_loss'], sort_keys=True)}",
                None,
            )
        )
    return lines


def _write_summary(run_dir: Path, summary: Mapping[str, Any]) -> Path:
    """Persist one derived report without rewriting immutable telemetry rows."""
    output = Path(run_dir) / "summary.json"
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, sort_keys=True)
        handle.write("\n")
    return output


def _load_replay_launcher_map(
    path: Path | None,
) -> tuple[dict[tuple[str, int, str], dict[str, Any]], dict[str, Any] | None]:
    """Load reviewed coordinate-specific launcher provenance without basename trust."""
    if path is None:
        return {}, None
    path = Path(path)
    try:
        payload_bytes = path.read_bytes()
        payload = json.loads(payload_bytes)
        coordinates = payload["coordinates"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("diagnostic replay launcher map is unavailable or malformed") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"coordinates"} or not isinstance(coordinates, list):
        raise ValueError("diagnostic replay launcher map requires a coordinate list")
    resolved: dict[tuple[str, int, str], dict[str, Any]] = {}
    for coordinate in coordinates:
        if not isinstance(coordinate, Mapping) or set(coordinate) - {
            "task_id",
            "repetition",
            "arm",
            "launcher_path",
            "source_evidence",
        }:
            raise ValueError("diagnostic replay launcher map coordinates must be objects")
        task_id, repetition, arm = coordinate.get("task_id"), coordinate.get("repetition"), coordinate.get("arm")
        launcher, evidence = coordinate.get("launcher_path"), coordinate.get("source_evidence")
        if (
            not isinstance(task_id, str)
            or type(repetition) is not int
            or arm not in AGENTIC_ARMS
            or launcher is not None
            and (
                not isinstance(launcher, str)
                or not codex_runtime.is_absolute_launcher_path(launcher)
                or not isinstance(evidence, Mapping)
                or not evidence
            )
        ):
            raise ValueError("diagnostic replay launcher map has an invalid coordinate provenance entry")
        key = (task_id, repetition, arm)
        if key in resolved:
            raise ValueError("diagnostic replay launcher map has a duplicate coordinate")
        resolved[key] = {"launcher_path": launcher, "source_evidence": evidence}
    return resolved, {"path": str(path.resolve()), "sha256": hashlib.sha256(payload_bytes).hexdigest()}


def _has_unproven_absolute_query(raw_events: Sequence[Mapping[str, Any]]) -> bool:
    """Identify an absolute compact command that needs launcher provenance before credit."""
    for event in raw_events:
        item = event.get("item")
        command = item.get("command") if isinstance(item, Mapping) else None
        query = codex_runtime._dedicated_compact_query(command) if isinstance(command, str) else None
        raw_tokens = codex_runtime._native_item_tokens(command, preserve_quotes=True)
        if query is not None and any(
            codex_runtime.is_absolute_launcher_path(candidate)
            for candidate in (query[0], raw_tokens[0] if raw_tokens else "")
        ):
            return True
    return False


def replay_diagnostic(source_telemetry_path: Path, output_path: Path, *, launcher_map_path: Path | None = None) -> Path:
    """Reclassify observed Codemap use from immutable raw events without changing source rows."""
    source_telemetry_path = Path(source_telemetry_path)
    output_path = Path(output_path)
    if output_path.resolve().is_relative_to(source_telemetry_path.resolve().parent):
        raise ValueError("diagnostic replay output must be outside the historical telemetry directory")
    if output_path.exists():
        raise FileExistsError(output_path)
    try:
        source_bytes = source_telemetry_path.read_bytes()
        source_rows = [json.loads(line) for line in source_bytes.splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("diagnostic replay requires a valid telemetry JSONL input") from exc
    if not all(isinstance(row, dict) for row in source_rows):
        raise ValueError("diagnostic replay telemetry rows must be JSON objects")
    launcher_map, launcher_map_identity = _load_replay_launcher_map(launcher_map_path)
    replay_rows: list[dict[str, Any]] = []
    for row in source_rows:
        raw_events = row.get("raw_events")
        recorded_launcher = row.get("launcher_path")
        if not isinstance(raw_events, list) or not all(isinstance(event, Mapping) for event in raw_events):
            raise ValueError("diagnostic replay requires raw native events in every telemetry row")
        key = (row.get("task_id"), row.get("repetition"), row.get("arm"))
        map_entry = launcher_map.get(key)
        launcher = (
            recorded_launcher
            if isinstance(recorded_launcher, str) and codex_runtime.is_absolute_launcher_path(recorded_launcher)
            else None
        )
        launcher_provenance: str | Mapping[str, Any] | None = "recorded_per_cell" if launcher is not None else None
        if launcher is None and map_entry is not None and isinstance(map_entry["launcher_path"], str):
            launcher = map_entry["launcher_path"]
            launcher_provenance = map_entry
        parsed = codex_runtime.parse_codex_jsonl(
            (json.dumps(event, sort_keys=True) for event in raw_events), launcher_path=launcher
        )
        observed_calls = getattr(parsed, "codemap_observed_calls", 0)
        unavailable = observed_calls == 0 and launcher is None and _has_unproven_absolute_query(raw_events)
        replay_rows.append(
            {
                "original": row,
                "corrected_observed_use": None if unavailable else observed_calls > 0,
                "corrected_observed_use_status": "unavailable_launcher_provenance"
                if unavailable
                else ("observed" if observed_calls else "not_observed"),
                "corrected_observed_calls": observed_calls,
                "corrected_observed_successful_calls": getattr(parsed, "codemap_observed_successful_calls", 0),
                "corrected_observed_complete_calls": getattr(parsed, "codemap_observed_complete_calls", 0),
                "launcher_provenance": launcher_provenance,
            }
        )
    metadata_path = source_telemetry_path.with_name("run-metadata.json")
    checksums_path = source_telemetry_path.with_name("checksums.sha256")
    detector_path = _BENCHMARKS_DIR / "_bench_codex" / "runtime.py"
    document = {
        "schema": "codex-agentic-observed-use-replay-v1",
        "source": {
            "telemetry_path": str(source_telemetry_path.resolve()),
            "telemetry_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "coordinate_launcher_map": launcher_map_identity,
            "run_metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest()
            if metadata_path.is_file()
            else None,
            "checksums_sha256": hashlib.sha256(checksums_path.read_bytes()).hexdigest()
            if checksums_path.is_file()
            else None,
        },
        "detector": {
            "name": "codemap-observed-use-v2",
            "path": str(detector_path.resolve()),
            "sha256": hashlib.sha256(detector_path.read_bytes()).hexdigest(),
        },
        "rows": replay_rows,
    }
    with output_path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, sort_keys=True)
        handle.write("\n")
    return output_path


def _runtime_plugin_identities_are_valid(identities: Mapping[str, Any]) -> bool:
    """Return whether identities exactly describe the two required locked plugins."""
    if set(identities) != {"codemap-py", "codex-rig"}:
        return False
    for identity in identities.values():
        if not isinstance(identity, Mapping):
            return False
        version = identity.get("version")
        manifest_sha256 = identity.get("manifest_sha256")
        if (
            not isinstance(version, str)
            or not version
            or not isinstance(manifest_sha256, str)
            or len(manifest_sha256) != 64
            or any(character not in "0123456789abcdef" for character in manifest_sha256)
        ):
            return False
    return True


def _attest_runtime_isolation(path: Path) -> None:
    """Require verified, non-secret runtime identity evidence before paid cells run."""
    try:
        rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("runtime isolation evidence is unavailable or malformed") from exc
    if not any(
        isinstance(row, Mapping)
        and row.get("arm") == "C_strict"
        and row.get("status") == "verified"
        and isinstance(expected := row.get("expected_plugin_identities"), Mapping)
        and isinstance(observed := row.get("observed_plugin_identities"), Mapping)
        and expected == observed
        and _runtime_plugin_identities_are_valid(expected)
        and _runtime_plugin_identities_are_valid(observed)
        for row in rows
    ):
        raise RuntimeError("runtime isolation evidence lacks a verified C expected/observed identity match")


def _progress_line(execution_index: int, total_cells: int, run: AgenticRun) -> str:
    """Render one compact agentic result after its immutable telemetry row is persisted."""
    status = "✓" if run.success else "✗"
    treatment = "✓" if run.treatment_adherence else "✗"
    used = "✓" if run.codemap_used else "✗"
    score = "n/a" if run.quality is None else f"{run.quality.quality_score:.3f}"
    answer = "✓" if run.answer_contract_valid else ("△" if run.diagnostic_only else "✗")
    erec = run.evidence.erec if run.evidence is not None else 0.0
    rrec = run.evidence.rrec if run.evidence is not None else 0.0
    deff = run.evidence.deff if run.evidence is not None else 0.0
    return (
        f"({execution_index}/{total_cells}) {status}  {run.task_id:<5}  rep={run.repetition}  {run.arm:<10}"
        f"  in={fmt_tok(run.input_tokens):>6}  out={fmt_tok(run.output_tokens):>6}"
        f"  time={fmt_time(run.elapsed_s):>5}  SCORE={score}  EREC={erec:.3f}"
        f"  RREC={rrec:.3f}  DEFF={deff:.3f}  answer:{answer}  treatment:{treatment}  codemap-used:{used}"
    )


def _emit_run_line(run_log: Path, line: str, *, arm: str | None = None) -> None:
    """Print and append one paid-run line, using the shared renderer for arm rows."""
    if arm is None:
        print(line)
    else:
        print_arm_row(line, arm, console=benchmark_console())
    with run_log.open("a", encoding="utf-8") as handle:
        with contextlib.redirect_stdout(handle):
            if arm is None:
                print(line)
            else:
                print_arm_row(line, arm, console=benchmark_console(file=handle))


def _initial_metadata(
    *,
    manifest_path: Path,
    approval_sha256: str,
    run_dir: Path,
    tasks: Sequence[Mapping[str, Any]],
    scope: Mapping[str, Any],
    invocation_launcher_path: Path,
    model_name: str | None = None,
) -> dict[str, Any]:
    """Build compact provenance before the first paid coordinate is scheduled."""
    manifest = _read_agentic_manifest(manifest_path)
    model = manifest["model"]
    executed_model = str(resolve_agentic_model(manifest, Path(manifest_path), model_name) or model["name"])
    return {
        "schema": "codex-agentic-run-v1",
        "status": "running",
        "started_at": _structural._utc_now(),
        "persisted_cells": 0,
        "last_persisted_coordinate": None,
        "error": None,
        "manifest": {"path": str(manifest_path.resolve()), "sha256": _manifest_sha256(manifest_path)},
        "approval_sha256": approval_sha256,
        "scope": dict(scope),
        "invocation_launcher": {"path": str(invocation_launcher_path.resolve())},
        "execution": {
            "model": executed_model,
            "reasoning_effort": model["reasoning_effort"],
            "codex_cli_observed_version": os.environ.get("CODEX_CLI_OBSERVED_VERSION"),
            "cell_wall_clock_seconds": scope["coordinate_timeout_seconds"],
            "execution_order": dict(scope["execution_order"]),
            "coordinates": list(scope["coordinates"]),
            "comparability_scope": {
                "model": executed_model,
                "reasoning_effort": model["reasoning_effort"],
                "repository_and_index": "manifest-locked",
                "plugin_bytes": "frozen-in-run-input-snapshot",
                "isolation": "per-cell-isolated-codex-home",
                "cache_policy": "provider-cache-state-observed; no supported per-cell reset",
                "repetitions_limit": "variance screening only; not a significance or universal-savings claim",
            },
            "pooling_eligible": False,
        },
        "artifacts": {
            "telemetry_jsonl": str((run_dir / "telemetry.jsonl").resolve()),
            "telemetry_canonical_jsonl": str((run_dir / "telemetry-canonical.jsonl").resolve()),
            "run_metadata": str((run_dir / "run-metadata.json").resolve()),
        },
    }


def _admit_run_directory(run_dir: Path, invocation_launcher_path: Path, launcher_hash: str) -> None:
    """Allow only run-all's verified launcher and frozen source before a paid run starts."""
    expected_launcher = run_dir / ".launcher" / "run-all.sh"
    source_root = expected_launcher.parent / "source"
    source_manifest = expected_launcher.parent / "source.sha256"
    if invocation_launcher_path.absolute() != expected_launcher.absolute():
        raise FileExistsError(run_dir)
    try:
        entries = {entry.name for entry in run_dir.iterdir()}
        launcher_entries = {entry.name for entry in expected_launcher.parent.iterdir()}
        source_metadata = source_root.lstat()
        source_manifest_metadata = source_manifest.lstat()
    except OSError as exc:
        raise FileExistsError(run_dir) from exc
    if (
        entries != {".launcher"}
        or launcher_entries != {"run-all.sh", "source", "source.sha256"}
        or not stat.S_ISDIR(source_metadata.st_mode)
        or not stat.S_ISREG(source_manifest_metadata.st_mode)
    ):
        raise FileExistsError(run_dir)
    _structural._validate_invocation_launcher(expected_launcher, launcher_hash)


def run_paid(
    *,
    repo_path: Path,
    index_path: Path,
    auth_source: Path,
    approval_sha256: str,
    run_dir: Path,
    manifest_path: Path = _MANIFEST_PATH,
    marketplace_root: Path | None = None,
    codemap_bin: Path | None = None,
    invocation_launcher_path: Path | None = None,
    task_ids: Sequence[str] | None = None,
    repetitions: int | None = None,
    scope_sha256: str | None = None,
    model: str | None = None,
    index_relocation: Mapping[str, str] | None = None,
    runner_factory: Callable[..., Any] | None = None,
) -> Path:
    """Execute one admitted shared-task scope with immutable partial evidence.

    Test fixtures may inject ``runner_factory``; production always uses the structural adapter for credential lifecycle,
    permissions, and native Codex transport.  The function never records the auth path or credential bytes.
    """
    if not auth_source:
        raise ValueError("paid Codex agentic execution requires an auth source")
    if invocation_launcher_path is None:
        raise ValueError("paid Codex agentic execution requires the invocation launcher path")
    scope = resolve_agentic_scope(manifest_path, task_ids=task_ids, repetitions=repetitions, model=model)
    repetitions = int(scope["repetitions"])
    loaded_manifest = _read_agentic_manifest(manifest_path)
    preregistered = loaded_manifest["preregistered_scope"]
    model_override = resolve_agentic_model(loaded_manifest, Path(manifest_path), model)
    # A selected stratum is as much a departure from the locked study as a task selection is, so it is admitted the
    # same way: by the scope hash that binds it, never by the manifest digest that names another model.
    is_default_scope = task_ids is None and repetitions == preregistered["repetitions"] and model_override is None
    if is_default_scope:
        manifest = validate_paid_admission(manifest_path, approval_sha256)
        if scope_sha256 is not None and scope_sha256 != scope["scope_sha256"]:
            raise ValueError("default agentic scope hash does not match the manifest-bound scope")
    else:
        manifest = validate_paid_admission(manifest_path, _manifest_sha256(Path(manifest_path)))
        if approval_sha256 != scope["scope_sha256"] or scope_sha256 != scope["scope_sha256"]:
            raise ValueError("nondefault agentic scope requires its exact derived scope SHA-256 approval")
    repo_path = Path(repo_path).resolve()
    index_path = Path(index_path).resolve()
    _validate_agentic_runtime(manifest, repo_path, index_path, index_relocation)
    runtime = manifest.get("runtime_isolation")
    if not isinstance(runtime, Mapping) or not isinstance(runtime.get("manifest"), str):
        raise ValueError("Codex agentic manifest lacks a structural runtime adapter manifest")
    adapter_manifest_path = (_BENCHMARKS_DIR.parent / runtime["manifest"]).resolve()
    if not adapter_manifest_path.is_file():
        raise ValueError("Codex agentic structural runtime adapter manifest is unavailable")
    launcher_hash = str(manifest["artifact_sha256"]["run_all"])
    invocation_launcher_path = Path(invocation_launcher_path)
    _structural._validate_invocation_launcher(invocation_launcher_path, launcher_hash)
    run_dir = Path(run_dir)
    _admit_run_directory(run_dir, invocation_launcher_path, launcher_hash)
    raw_path = run_dir / "telemetry.jsonl"
    raw_path.touch(exist_ok=False)
    run_log = run_dir / "run.log"
    run_log.touch(exist_ok=False)
    metadata_path = run_dir / "run-metadata.json"
    all_tasks = load_agentic_tasks(_TASKS_PATH, manifest_path)
    tasks_by_id = {task["id"]: task for task in all_tasks}
    tasks = [tasks_by_id[task_id] for task_id in scope["task_ids"]]
    metadata = _initial_metadata(
        manifest_path=Path(manifest_path),
        approval_sha256=approval_sha256,
        run_dir=run_dir,
        tasks=tasks,
        scope=scope,
        invocation_launcher_path=invocation_launcher_path,
        model_name=model,
    )
    _structural._write_run_metadata(metadata_path, metadata)
    _emit_output_legend()
    with run_log.open("a", encoding="utf-8") as handle:
        handle.write(_OUTPUT_LEGEND + "\n")
    factory = runner_factory or AgenticCodexRunner
    runner: Any | None = None
    try:
        oracles = {task["id"]: build_oracle(task, repo_path) for task in tasks}
        runner = factory(
            repo_path=repo_path,
            index_path=index_path,
            marketplace_root=marketplace_root,
            codemap_bin=codemap_bin,
            auth_source=Path(auth_source),
            index_relocation=index_relocation,
            adapter_manifest_path=adapter_manifest_path,
            agentic_manifest=manifest,
            agentic_manifest_path=Path(manifest_path),
            model_name=model,
        )
        snapshot_builder = getattr(runner, "create_input_snapshot", None)
        if not callable(snapshot_builder):
            raise RuntimeError("agentic runner must create and attest an immutable runtime snapshot")
        metadata["inputs"] = {
            "snapshot": snapshot_builder(
                run_dir, manifest_path=Path(manifest_path), invocation_launcher_path=invocation_launcher_path
            )
        }
        runtime_evidence = run_dir / "runtime-isolation.jsonl"
        _attest_runtime_isolation(runtime_evidence)
        metadata["artifacts"]["runtime_isolation_jsonl"] = str(runtime_evidence.resolve())
        metadata["artifacts"]["runtime_isolation_sha256"] = hashlib.sha256(runtime_evidence.read_bytes()).hexdigest()
        _structural._write_run_metadata(metadata_path, metadata)
        _write_checksums(run_dir)
        for coordinate in scope["coordinates"]:
            task = tasks_by_id[coordinate["task_id"]]
            repetition = int(coordinate["repetition"])
            arm = str(coordinate["arm"])
            run = runner.run(task, arm, repetition=repetition, oracle=oracles[task["id"]])
            _validate_agentic_runtime(manifest, repo_path, index_path, index_relocation)
            _structural._validate_invocation_launcher(invocation_launcher_path, launcher_hash)
            _append_telemetry(raw_path, run, int(metadata["persisted_cells"]))
            metadata["persisted_cells"] = int(metadata["persisted_cells"]) + 1
            metadata["last_persisted_coordinate"] = {
                "task_id": task["id"],
                "repetition": repetition,
                "arm": arm,
            }
            metadata["artifacts"]["canonical_telemetry_sha256"] = _write_canonical_telemetry(
                raw_path, scope["task_ids"]
            )
            _structural._write_run_metadata(metadata_path, metadata)
            _emit_run_line(
                run_log,
                _progress_line(int(metadata["persisted_cells"]), int(scope["total_cells"]), run),
                arm=arm,
            )
            _write_checksums(run_dir)
        metadata["status"] = "completed"
        metadata["completed_at"] = _structural._utc_now()
        summary = _summarize_telemetry(
            [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line]
        )
        summary_path = _write_summary(run_dir, summary)
        metadata["artifacts"]["summary_json"] = str(summary_path.resolve())
        metadata["artifacts"]["summary_sha256"] = hashlib.sha256(summary_path.read_bytes()).hexdigest()
        _structural._write_run_metadata(metadata_path, metadata)
        for line, arm in _summary_lines(summary):
            _emit_run_line(run_log, line, arm=arm)
        _emit_run_line(
            run_log,
            f"SUMMARY  status=completed  persisted_cells={metadata['persisted_cells']}/{scope['total_cells']}",
        )
        _write_checksums(run_dir)
        return run_dir
    except BaseException as exc:
        metadata["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        metadata["completed_at"] = _structural._utc_now()
        metadata["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        _structural._write_run_metadata(metadata_path, metadata)
        _emit_run_line(
            run_log,
            f"SUMMARY  status={metadata['status']}  persisted_cells={metadata['persisted_cells']}/{scope['total_cells']}",
        )
        _write_checksums(run_dir)
        raise
    finally:
        if runner is not None:
            _structural._close_runner(runner)


def _cli_error(message: str) -> NoReturn:
    """Reject one invocation exactly as the previous argparse parser did.

    Args:
        message: Human-readable reason the invocation cannot proceed.

    Raises:
        SystemExit: Always, carrying argparse's usage-error status 2.

    Examples:
        >>> try:
        ...     _cli_error("bad invocation")
        ... except SystemExit as exit_status:
        ...     exit_status.code
        2
    """
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _normalize_task_ids(task_id: str | Sequence[str] | None) -> list[str] | None:
    """Normalize one Fire ``--task-id`` value into an ordered task-ID list.

    The comma-separated ``--task-id BA-01,BA-02`` form is split here rather than
    relying on Fire: Fire only splits a comma-joined value when it parses as a
    Python literal, which ``BA-01,BA-02`` does not, so it arrives as one string.
    A value Fire did split (a sequence) is accepted unchanged. An empty token is
    rejected here rather than silently dropped, so a stray comma cannot quietly
    narrow the resolved scope.

    Args:
        task_id: Raw Fire value: ``None``, one comma-separated string, or a sequence.

    Returns:
        The selected task IDs, or ``None`` when no task was selected.

    Raises:
        SystemExit: When any comma-separated token is empty.

    Examples:
        >>> _normalize_task_ids(None) is None
        True
        >>> _normalize_task_ids("BA-01")
        ['BA-01']
        >>> _normalize_task_ids("BA-01,BA-02")
        ['BA-01', 'BA-02']
        >>> _normalize_task_ids(("BA-01", "BA-02"))
        ['BA-01', 'BA-02']
    """
    if task_id is None:
        return None
    values = task_id if isinstance(task_id, (list, tuple)) else [task_id]
    task_ids = [token.strip() for value in values for token in str(value).split(",")]
    if not task_ids or any(not selected for selected in task_ids):
        _cli_error("--task-id values cannot be empty")
    return task_ids


def _require_paid_arguments(**arguments: Any) -> None:
    """Reject a paid invocation that omits any required command-line argument.

    Args:
        **arguments: Parameter name to supplied value, in flag order; a ``None``
            value marks that flag as missing.

    Raises:
        SystemExit: When at least one required flag was not supplied.

    Examples:
        >>> _require_paid_arguments(repo_path=Path("."), run_dir=Path("."))
        >>> try:
        ...     _require_paid_arguments(repo_path=None, run_dir=Path("."))
        ... except SystemExit as exit_status:
        ...     exit_status.code
        2
    """
    missing = [f"--{name.replace('_', '-')}" for name, value in arguments.items() if value is None]
    if missing:
        _cli_error(f"paid Codex agentic execution requires {' '.join(missing)}")


def _require_dry_run_admission_arguments(**arguments: Any) -> None:
    """Reject a runtime dry run that cannot exercise isolated B/C admission."""
    missing = [f"--{name.replace('_', '-')}" for name, value in arguments.items() if value is None]
    if missing:
        _cli_error(f"Codex agentic dry-run admission requires {' '.join(missing)}")


def main(  # noqa: PLR0913 — fire CLI adapter: every param is a keyword flag with a default (0 required)
    dry_run: bool = False,
    resolve_scope: bool = False,
    replay_telemetry_path: Path | None = None,
    replay_output_path: Path | None = None,
    replay_launcher_map_path: Path | None = None,
    tasks_path: Path = _TASKS_PATH,
    manifest_path: Path = _MANIFEST_PATH,
    task_id: str | Sequence[str] | None = None,
    repetitions: int | None = None,
    repo_path: Path | None = None,
    index_path: Path | None = None,
    marketplace_root: Path | None = None,
    codemap_bin: Path | None = None,
    auth_source: Path | None = None,
    invocation_launcher_path: Path | None = None,
    run_dir: Path | None = None,
    paid_approval: str | None = None,
    scope_sha256: str | None = None,
    model: str | None = None,
    index_relocation_path: Path | None = None,
) -> None:
    """Run a no-model scope preflight or a separately admitted paid study.

    Args:
        dry_run: Print the resolved no-model cell plan and exit.
        resolve_scope: Print the resolved scope JSON and exit.
        replay_telemetry_path: Immutable telemetry JSONL to inspect without a model call.
        replay_output_path: New diagnostic JSON written by an offline replay.
        replay_launcher_map_path: Reviewed per-coordinate launcher provenance for legacy rows.
        tasks_path: Locked shared agentic task suite.
        manifest_path: Locked Codex agentic manifest.
        task_id: One manifest-bound task ID, or several as a single comma-separated
            value (``--task-id BA-01,BA-02``); absent selects the whole locked scope.
        repetitions: Positive repeat count per task and arm.
        repo_path: Locked agentic target repository root (paid execution only).
        index_path: Locked frozen Codemap index (paid execution only).
        marketplace_root: Optional verified plugin marketplace root.
        codemap_bin: Optional verified Codemap CLI path.
        auth_source: Codex credential source for the isolated homes (paid execution only).
        invocation_launcher_path: run-all launcher snapshot inside the run directory (paid execution only).
        run_dir: Empty run directory holding only that launcher snapshot (paid execution only).
        paid_approval: Exact SHA-256 of the reviewed admitted agentic manifest.
        scope_sha256: Exact SHA-256 of a nondefault resolved scope.
        model: Declared model stratum to run instead of the manifest default; the resulting scope hash binds it and
            is the approval a paid run of that stratum requires.
        index_relocation_path: Relocation provenance written when this run's index was moved into an
            isolated worktree; absent for a run at the canonical managed clone.

    Raises:
        SystemExit: When the invocation is rejected before any paid coordinate runs.

    Examples:
        >>> main.__name__
        'main'
    """
    # fire passes CLI strings through regardless of annotation — coerce every typed argument.
    task_ids = _normalize_task_ids(task_id)
    if (replay_telemetry_path is None) != (replay_output_path is None):
        _cli_error("diagnostic replay requires both --replay-telemetry-path and --replay-output-path")
    if replay_launcher_map_path is not None and replay_telemetry_path is None:
        _cli_error("--replay-launcher-map-path requires diagnostic replay input and output paths")
    if replay_telemetry_path is not None:
        try:
            replay_diagnostic(
                Path(replay_telemetry_path),
                Path(replay_output_path),
                launcher_map_path=None if replay_launcher_map_path is None else Path(replay_launcher_map_path),
            )
        except (FileExistsError, ValueError) as exc:
            _cli_error(str(exc))
        return
    manifest_path = Path(manifest_path)
    repetitions = None if repetitions is None else int(repetitions)
    paid_approval = None if paid_approval is None else str(paid_approval)
    scope_sha256 = None if scope_sha256 is None else str(scope_sha256)
    model = None if model is None else str(model)
    try:
        index_relocation = load_index_relocation(None if index_relocation_path is None else Path(index_relocation_path))
    except ValueError as exc:
        _cli_error(str(exc))
    try:
        scope = resolve_agentic_scope(manifest_path, task_ids=task_ids, repetitions=repetitions, model=model)
    except ValueError as exc:
        _cli_error(str(exc))
    repetitions = int(scope["repetitions"])
    if scope_sha256 is not None and scope_sha256 != scope["scope_sha256"]:
        _cli_error("--scope-sha256 does not match the resolved agentic scope")
    if resolve_scope:
        print(json.dumps(scope, sort_keys=True))
        return
    if dry_run:
        _require_dry_run_admission_arguments(
            repo_path=repo_path,
            index_path=index_path,
            marketplace_root=marketplace_root,
            codemap_bin=codemap_bin,
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _cli_error(f"agentic manifest is unavailable or malformed: {exc}")
        if not isinstance(manifest, Mapping):
            _cli_error("agentic manifest must be a JSON object")
        runtime = manifest.get("runtime_isolation")
        if not isinstance(runtime, Mapping) or not isinstance(runtime.get("manifest"), str):
            _cli_error("Codex agentic manifest lacks a structural runtime adapter manifest")
        adapter_manifest_path = (_BENCHMARKS_DIR.parent / runtime["manifest"]).resolve()
        if not adapter_manifest_path.is_file():
            _cli_error("Codex agentic structural runtime adapter manifest is unavailable")
        try:
            runner = AgenticCodexRunner(
                repo_path=Path(repo_path),
                index_path=Path(index_path),
                marketplace_root=Path(marketplace_root),
                codemap_bin=Path(codemap_bin),
                auth_source=None,
                adapter_manifest_path=adapter_manifest_path,
                agentic_manifest=manifest,
                agentic_manifest_path=manifest_path,
                index_relocation=index_relocation,
                model_name=model,
            )
            try:
                runner.preflight_snapshot_bound_admission()
            finally:
                runner.close()
        except ValueError as exc:
            _cli_error(str(exc))
        _emit_output_legend()
        for line in _dry_run_plan(
            tasks_path=Path(tasks_path),
            manifest_path=manifest_path,
            task_ids=task_ids,
            repetitions=repetitions,
            model=model,
        ):
            codex_runtime.print_plan_row(line)
        return
    _require_paid_arguments(
        repo_path=repo_path,
        index_path=index_path,
        auth_source=auth_source,
        invocation_launcher_path=invocation_launcher_path,
        run_dir=run_dir,
        paid_approval=paid_approval,
    )
    run_paid(
        repo_path=Path(repo_path),
        index_path=Path(index_path),
        auth_source=Path(auth_source),
        approval_sha256=paid_approval,
        run_dir=Path(run_dir),
        manifest_path=manifest_path,
        marketplace_root=None if marketplace_root is None else Path(marketplace_root),
        codemap_bin=None if codemap_bin is None else Path(codemap_bin),
        invocation_launcher_path=Path(invocation_launcher_path),
        task_ids=task_ids,
        repetitions=repetitions,
        scope_sha256=scope_sha256,
        model=model,
        index_relocation=index_relocation,
    )


if __name__ == "__main__":
    from fire import Fire

    Fire(main)
