"""Command-line entry point for the Codex structural benchmark."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from _bench_common.mutation_isolation import (
    load_index_relocation,
    patch_test_runtime_identity,
)
from _bench_common.paid_lifecycle import paid_approval_matches, write_checksums

from _bench_codex import runtime
from _bench_codex.structural import manifest as manifest_module
from _bench_codex.structural import rescore, scoring
from _bench_codex.structural import runner as runner_module
from _bench_codex.structural import tasks as tasks_module
from _bench_codex.structural.arms import _manifest_arm_order, _print_result_block
from _bench_codex.structural.config import (
    _PROVENANCE_KEY,
    ARMS,
    BENCHMARKS_DIR,
    PARITY_CODEX_MODEL,
    PARITY_CODEX_REASONING_EFFORT,
    PARITY_MANIFEST_PATH,
    REPO_ROOT,
)
from _bench_codex.structural.diff_impact import (
    DiffImpactStageAdmission,
    _capture_diff_impact_stage,
    _validate_codex_stratum,
)
from _bench_codex.structural.manifest import (
    _resolve_structural_task_selection,
    _targeted_scope_sha256,
    _task_selection_contract,
    _validate_targeted_scope_request,
)
from _bench_codex.structural.provisioning import _validate_invocation_launcher
from _bench_codex.structural.rescore import rescore_results
from _bench_codex.structural.runner import (
    _append_run,
    _canonical_telemetry_path,
    _close_runner,
    _utc_now,
    _write_canonical_telemetry,
)
from _bench_codex.structural.scoring import (
    _diff_impact_stager,
    _infrastructure_failure_signature,
    _pooling_ineligibility_reasons,
)


@dataclass(frozen=True)
class _RunPlan:
    """The admitted scope of one invocation, resolved before any task is loaded."""

    targeted_scope: dict[str, Any] | None
    task_ids: list[str] | None
    repetitions: int
    cell_wall_clock_seconds: float


def _resolve_run_plan(
    *,
    model: str,
    reasoning_effort: str,
    manifest_path: Path,
    task_ids: list[str] | None,
    task_selectors: str | Sequence[str] | None,
    repetitions: int | None,
    arm: str,
    scope_sha256: str | None,
    dry_run: bool,
) -> _RunPlan:
    """Admit the stratum, selectors, and repetition count against the active manifest.

    Every rejection here happens before a task suite is read or a Codex home is built, so an
    unusable invocation costs nothing and can never reach a paid model call.

    Args:
        model: Codex model stratum requested on the command line.
        reasoning_effort: Reasoning effort requested alongside the model.
        manifest_path: Active provider-parity manifest.
        task_ids: Exact task IDs from ``--task-id``, or None.
        task_selectors: Family or exact-ID selectors from ``--tasks``, or None.
        repetitions: Explicit repetition count, or None to take the manifest default.
        arm: Arm selector for the run.
        scope_sha256: Caller-supplied scope identity for a targeted paid run.
        dry_run: True when planning only.

    Returns:
        The resolved :class:`_RunPlan`.

    Raises:
        ValueError: If the manifest is malformed or the requested scope is inadmissible.
    """
    _validate_codex_stratum(model, reasoning_effort, manifest_path)
    try:
        active_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        confirmatory_repetitions = active_manifest["preregistered_cells"]["confirmatory_repetitions"]
        cell_wall_clock_seconds = active_manifest["execution_controls"]["parity_timeout_seconds"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity execution controls are unavailable or malformed") from exc
    if type(confirmatory_repetitions) is not int or confirmatory_repetitions < 1:
        raise ValueError("confirmatory repetitions must be a positive integer")
    if type(cell_wall_clock_seconds) not in {int, float} or cell_wall_clock_seconds <= 0:
        raise ValueError("per-cell timeout must be positive")
    if task_ids and task_selectors is not None:
        raise ValueError("--task-id and --tasks cannot be combined")
    targeted_scope = (
        _resolve_structural_task_selection(Path(manifest_path), task_selectors) if task_selectors is not None else None
    )
    if targeted_scope is not None:
        task_ids = list(targeted_scope["task_ids"])
        repetitions = targeted_scope["repetitions"] if repetitions is None else repetitions
    repetitions = confirmatory_repetitions if repetitions is None else repetitions
    if repetitions < 1:
        raise ValueError("--repetitions must be a positive integer")
    if targeted_scope is not None:
        _validate_targeted_scope_request(
            targeted_scope,
            repetitions=repetitions,
            arm=arm,
            scope_sha256=scope_sha256,
            dry_run=dry_run,
        )
    manifest_module._validate_unscoped_paid_task_ids(
        manifest_path,
        task_ids,
        targeted=targeted_scope is not None,
        dry_run=dry_run,
    )
    return _RunPlan(
        targeted_scope=targeted_scope,
        task_ids=task_ids,
        repetitions=repetitions,
        cell_wall_clock_seconds=cell_wall_clock_seconds,
    )


def _load_selected_tasks(
    *,
    tasks_path: Path,
    manifest_path: Path,
    repo_path: Path,
    task_ids: list[str] | None,
) -> tuple[list[dict[str, Any]], str]:
    """Load the locked suite, narrow it to the requested IDs, and admit each diff-impact stage.

    Args:
        tasks_path: Locked task suite to read.
        manifest_path: Active manifest, used as the experiment-revision fallback.
        repo_path: Target repository each diff-impact stage is admitted against.
        task_ids: Exact task IDs to keep, or None to keep the whole suite.

    Returns:
        The selected tasks in suite order, and the experiment revision they were locked under.

    Raises:
        ValueError: If the suite is empty, an ID is duplicated, or an ID is unknown.
    """
    tasks = tasks_module.load_tasks_with_provenance(tasks_path, manifest_path)
    if not tasks:
        raise ValueError("locked task suite must contain at least one task")
    provenance = tasks[0].get(_PROVENANCE_KEY, {})
    experiment_revision = (
        str(provenance.get("experiment_revision", "")) if isinstance(provenance, Mapping) else ""
    ) or manifest_module._read_manifest_revision(manifest_path)
    if task_ids:
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("--task-id values must be unique")
        missing = set(task_ids) - {task["id"] for task in tasks}
        if missing:
            raise ValueError(f"unknown locked task IDs: {sorted(missing)}")
        selected_ids = set(task_ids)
        tasks = [task for task in tasks if task["id"] in selected_ids]
    for task in tasks:
        scoring._validate_diff_impact_stage(repo_path, task)
    return tasks, experiment_revision


def _render_arm_probes(runner: Any, *, arm: str, tasks: list[dict[str, Any]]) -> None:
    """Print one probe row per selected arm and run whatever preflights the runner exposes.

    Dry-run only. The runner is always closed afterwards, so a preflight that raises still
    releases the disposable Codex home instead of leaving it behind.

    Args:
        runner: Live runner for this invocation.
        arm: Arm selector, or ``"all"`` for every arm.
        tasks: Tasks the preflights are asked to plan.
    """
    selected_arms = ARMS if arm == "all" else (arm,)
    try:
        for selected in selected_arms:
            evidence = runner.probe_arm(selected)
            runtime.print_plan_row(
                runtime.format_probe_row(
                    selected,
                    {
                        "codemap": bool(evidence["codemap_available"]),
                        "use": runtime.probe_use(selected),
                        "codemap_python": evidence.get("codemap_python") or "absent",
                    },
                )
            )
        for attribute in ("preflight_expected_queries", "preflight_diff_impact_stages"):
            preflight = getattr(runner, attribute, None)
            if callable(preflight):
                preflight(tasks, selected_arms)
    finally:
        _close_runner(runner)


def main(
    *,
    repo_path: Path,
    model: str,
    reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT,
    tasks_path: Path,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    index_path: Path | None = None,
    marketplace_root: Path | None = None,
    codemap_bin: Path | None = None,
    auth_source: Path | None = None,
    invocation_launcher_path: Path | None = None,
    output_path: Path | None = None,
    metadata_path: Path | None = None,
    task_ids: list[str] | None = None,
    task_selectors: str | Sequence[str] | None = None,
    scope_sha256: str | None = None,
    repetitions: int | None = None,
    arm: str = "all",
    dry_run: bool = False,
    show_legend: bool = True,
    index_relocation_path: Path | None = None,
) -> None:
    """Validate, plan, and execute cells under the manifest's per-cell timeout."""
    manifest_path = Path(manifest_path)
    plan = _resolve_run_plan(
        model=model,
        reasoning_effort=reasoning_effort,
        manifest_path=manifest_path,
        task_ids=task_ids,
        task_selectors=task_selectors,
        repetitions=repetitions,
        arm=arm,
        scope_sha256=scope_sha256,
        dry_run=dry_run,
    )
    targeted_scope = plan.targeted_scope
    task_ids = plan.task_ids
    repetitions = plan.repetitions
    cell_wall_clock_seconds = plan.cell_wall_clock_seconds
    tasks, experiment_revision = _load_selected_tasks(
        tasks_path=tasks_path,
        manifest_path=manifest_path,
        repo_path=Path(repo_path),
        task_ids=task_ids,
    )
    explicit_selection = targeted_scope is not None
    if not dry_run:
        if output_path is None:
            raise ValueError("non-dry Codex runs require --output-path")
        metadata_path = metadata_path or output_path.with_name(f"{output_path.stem}-metadata.json")
        if output_path.exists():
            raise FileExistsError(output_path)
        if metadata_path.exists():
            raise FileExistsError(metadata_path)
        canonical_path = _canonical_telemetry_path(output_path)
        if canonical_path.exists():
            raise FileExistsError(canonical_path)
        manifest_module._validate_execution_manifest(manifest_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8"):
            pass
    invocation_launcher_sha256: str | None = None
    if invocation_launcher_path is not None:
        invocation_launcher_path = Path(invocation_launcher_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            invocation_launcher_sha256 = str(manifest["artifact_sha256"]["run_all"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("paid manifest lacks the invocation-launcher lock") from exc
        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
    runner = runner_module.CodexRunner(
        model,
        repo_path,
        reasoning_effort=reasoning_effort,
        index_path=index_path,
        marketplace_root=marketplace_root,
        codemap_bin=codemap_bin,
        manifest_path=manifest_path,
        index_relocation=load_index_relocation(index_relocation_path),
        auth_source=auth_source,
        targeted=explicit_selection,
        timeout=(
            float(targeted_scope["coordinate_timeout_seconds"])
            if targeted_scope is not None
            else float(cell_wall_clock_seconds)
        ),
    )
    if show_legend:
        runtime.print_structural_legend()
    if dry_run:
        _render_arm_probes(runner, arm=arm, tasks=tasks)
    print(f"CONTROL\tcell_wall_clock_seconds={runner.timeout:g}")
    if not dry_run:
        assert output_path is not None
        assert metadata_path is not None
        print(runtime.presentation.format_artifact_block(telemetry=output_path, metadata=metadata_path))
    task_arms = {
        (task["id"], repetition): (
            _manifest_arm_order(
                experiment_revision,
                model,
                task["id"],
                repetition,
                reasoning_effort,
                task_ordinal=(
                    int(task[_PROVENANCE_KEY]["task_ordinal"])
                    if isinstance(task.get(_PROVENANCE_KEY), Mapping)
                    and type(task[_PROVENANCE_KEY].get("task_ordinal")) is int
                    else None
                ),
            )
            if arm == "all"
            else (arm,)
        )
        for task in tasks
        for repetition in range(1, repetitions + 1)
    }
    if dry_run:
        for task in tasks:
            for repetition in range(1, repetitions + 1):
                for selected in task_arms[(task["id"], repetition)]:
                    runtime.print_plan_row(runtime.format_plan_row(task["id"], repetition, selected))
        return
    assert output_path is not None
    assert metadata_path is not None
    snapshot_builder = getattr(runner, "create_input_snapshot", None)
    preflight = getattr(runner, "preflight_expected_queries", None)
    try:
        if callable(preflight):
            preflight(tasks, tuple(dict.fromkeys(selected for arms in task_arms.values() for selected in arms)))
        input_snapshot = (
            snapshot_builder(
                output_path.parent,
                tasks_path=tasks_path,
                manifest_path=manifest_path,
                invocation_launcher_path=invocation_launcher_path,
                tasks=tasks,
                arms=tuple(dict.fromkeys(selected for arms in task_arms.values() for selected in arms)),
            )
            if callable(snapshot_builder)
            else None
        )
        metadata = rescore._initial_run_metadata(
            manifest_path=manifest_path,
            repo_path=repo_path,
            index_path=index_path,
            output_path=output_path,
            metadata_path=metadata_path,
            model=model,
            reasoning_effort=reasoning_effort,
            repetitions=repetitions,
            task_arms=task_arms,
            cell_wall_clock_seconds=runner.timeout,
            auth_provisioned=auth_source is not None,
            input_snapshot=input_snapshot,
            study_mode="targeted" if explicit_selection else "confirmatory",
            targeted_scope=targeted_scope,
        )
        canonical_path = _canonical_telemetry_path(output_path)
        task_order = tuple(str(task["id"]) for task in tasks)
        runner_module._write_run_metadata(metadata_path, metadata)
    except BaseException:
        _close_runner(runner)
        raise
    planned_cells = sum(len(arms) for arms in task_arms.values())
    printed_cells = 0
    pending_result_rows: list[tuple[str, str]] = []
    active_stager: Any | None = None
    active_diff_impact_stage: DiffImpactStageAdmission | None = None
    consecutive_infrastructure_signature = ""
    consecutive_infrastructure_failures = 0
    try:
        for task in tasks:
            for repetition in range(1, repetitions + 1):
                active_stager = _diff_impact_stager(Path(repo_path), task)
                if active_stager is not None:
                    runner._admit_runtime("A_plain")
                    try:
                        active_stager.__enter__()
                        active_diff_impact_stage = _capture_diff_impact_stage(Path(repo_path), task)
                    except BaseException:
                        try:
                            active_stager.__exit__(*sys.exc_info())
                        finally:
                            active_stager = None
                            runner._admit_runtime("A_plain")
                        raise
                pending_result_rows = []
                for selected in task_arms[(task["id"], repetition)]:
                    if invocation_launcher_path is not None and invocation_launcher_sha256 is not None:
                        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
                    run_kwargs: dict[str, Any] = {"repetition": repetition}
                    if active_diff_impact_stage is not None:
                        run_kwargs["diff_impact_stage"] = active_diff_impact_stage
                    run = runner.run(task, selected, **run_kwargs)
                    if invocation_launcher_path is not None and invocation_launcher_sha256 is not None:
                        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
                    _append_run(output_path, run, execution_index=int(metadata["persisted_cells"]))
                    metadata["persisted_cells"] = int(metadata["persisted_cells"]) + 1
                    outcomes = metadata["cell_outcomes"]
                    outcomes["successful" if run.success else "unsuccessful"] += 1
                    for outcome, failed in (
                        ("unscoreable", not run.scoreable),
                        ("incomplete", run.incomplete),
                        ("extraction_failed", run.extraction_failed),
                        ("contaminated", run.contaminated),
                        (
                            "compliance_failed",
                            run.arm in {"B_auto", "C_strict"} and not run.compliance,
                        ),
                        (
                            "locked_query_nonconforming",
                            run.arm in {"B_auto", "C_strict"} and run.locked_query_conformance is False,
                        ),
                        ("targeted", run.targeted),
                        ("token_accounting_inconsistent", run.token_accounting_inconsistent),
                    ):
                        if failed:
                            outcomes[outcome] += 1
                    pooling_reasons = metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"]
                    for reason in _pooling_ineligibility_reasons(run):
                        if reason not in pooling_reasons:
                            pooling_reasons.append(reason)
                    metadata["last_persisted_coordinate"] = {
                        "task_id": task["id"],
                        "repetition": repetition,
                        "arm": selected,
                    }
                    metadata["artifacts"]["canonical_telemetry_sha256"] = _write_canonical_telemetry(
                        output_path,
                        canonical_path,
                        task_order=task_order,
                    )
                    metadata["artifacts"]["canonical_telemetry_status"] = "partial"
                    runner_module._write_run_metadata(metadata_path, metadata)
                    status = "✓" if run.success else "✗"
                    quality = f"{run.quality_score:.3f}" if run.quality_score is not None else "?"
                    pending_result_rows.append(
                        (
                            selected,
                            runtime.format_structural_result_row(
                                status=status,
                                task_id=task["id"],
                                repetition=repetition,
                                arm=selected,
                                input_tokens=run.input_tokens,
                                cached_input_tokens=run.cached_input_tokens,
                                fresh_tokens=run.fresh_input_tokens,
                                output_tokens=run.output_tokens,
                                elapsed_s=run.elapsed_s,
                                quality=quality,
                                adherence=run.treatment_adherence,
                                codemap_used=run.codemap_observed_calls > 0,
                                query_conformance=run.locked_query_conformance,
                                headline_eligible=run.headline_eligible_v1,
                            ),
                        )
                    )
                    infrastructure_signature = _infrastructure_failure_signature(run)
                    if infrastructure_signature is None:
                        consecutive_infrastructure_signature = ""
                        consecutive_infrastructure_failures = 0
                    elif run.error_type == "authentication_failed":
                        raise RuntimeError(
                            "infrastructure failure: authentication failed; reauthenticate before resuming the benchmark"
                        )
                    elif infrastructure_signature == consecutive_infrastructure_signature:
                        consecutive_infrastructure_failures += 1
                    else:
                        consecutive_infrastructure_signature = infrastructure_signature
                        consecutive_infrastructure_failures = 1
                    if consecutive_infrastructure_failures >= 3:
                        raise RuntimeError(
                            "infrastructure failure recurred three times before a model response; "
                            "preserved partial artifacts and stopped scheduling"
                        )
                printed_cells = _print_result_block(
                    pending_result_rows, printed_cells=printed_cells, planned_cells=planned_cells
                )
                pending_result_rows = []
                if active_stager is not None:
                    try:
                        active_stager.__exit__(None, None, None)
                    finally:
                        active_stager = None
                        active_diff_impact_stage = None
                        runner._admit_runtime("A_plain")
    except BaseException as exc:
        if active_stager is not None:
            try:
                active_stager.__exit__(*sys.exc_info())
            finally:
                active_stager = None
                active_diff_impact_stage = None
                runner._admit_runtime("A_plain")
        if pending_result_rows:
            printed_cells = _print_result_block(
                pending_result_rows, printed_cells=printed_cells, planned_cells=planned_cells
            )
            pending_result_rows = []
        metadata["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        metadata["completed_at"] = _utc_now()
        metadata["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
        pooling_reasons = metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"]
        if "run_not_completed" not in pooling_reasons:
            pooling_reasons.append("run_not_completed")
        if canonical_path.exists():
            metadata["artifacts"]["canonical_telemetry_status"] = "partial"
            metadata["artifacts"]["canonical_telemetry_pooling_eligible"] = False
        runner_module._write_run_metadata(metadata_path, metadata)
        print(f"SUMMARY\tstatus={metadata['status']}\tpersisted_cells={metadata['persisted_cells']}")
        raise
    finally:
        try:
            _close_runner(runner)
        except BaseException as cleanup_exc:
            prior_error = metadata.get("error")
            metadata["status"] = "failed"
            metadata["completed_at"] = _utc_now()
            metadata["error"] = {
                "type": type(cleanup_exc).__name__,
                "message": f"runner credential cleanup failed: {cleanup_exc}"[:1000],
                "prior_error": prior_error,
            }
            metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
            pooling_reasons = metadata["artifacts"]["canonical_telemetry_pooling_ineligibility_reasons"]
            if "run_not_completed" not in pooling_reasons:
                pooling_reasons.append("run_not_completed")
            if canonical_path.exists():
                metadata["artifacts"]["canonical_telemetry_status"] = "partial"
                metadata["artifacts"]["canonical_telemetry_pooling_eligible"] = False
            runner_module._write_run_metadata(metadata_path, metadata)
            print(f"SUMMARY\tstatus=failed\tpersisted_cells={metadata['persisted_cells']}")
            raise
    if invocation_launcher_path is not None and invocation_launcher_sha256 is not None:
        _validate_invocation_launcher(invocation_launcher_path, invocation_launcher_sha256)
    metadata["status"] = "completed"
    metadata["completed_at"] = _utc_now()
    metadata["artifacts"]["telemetry_sha256"] = hashlib.sha256(output_path.read_bytes()).hexdigest()
    metadata["artifacts"]["canonical_telemetry_status"] = "complete"
    metadata["artifacts"]["canonical_telemetry_pooling_eligible"] = not metadata["artifacts"][
        "canonical_telemetry_pooling_ineligibility_reasons"
    ]
    runner_module._write_run_metadata(metadata_path, metadata)
    print(
        f"SUMMARY\tstatus=completed\tpersisted_cells={metadata['persisted_cells']}"
        f"\toutcomes={json.dumps(metadata['cell_outcomes'], sort_keys=True)}"
    )


def _cli_error(message: str) -> NoReturn:
    """Fail a CLI invocation with the usage status the previous parser reported.

    Args:
        message: Operator-facing reason, written to standard error verbatim.

    Raises:
        SystemExit: Always, with status ``2`` for usage errors.

    Examples:
        >>> import contextlib, io
        >>> stderr = io.StringIO()
        >>> with contextlib.redirect_stderr(stderr):
        ...     try:
        ...         _cli_error("boom")
        ...     except SystemExit as exc:
        ...         print(exc.code)
        2
        >>> stderr.getvalue().strip()
        'ERROR: boom'
    """
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(2)


def _optional_path(value: str | Path | None) -> Path | None:
    """Coerce an optional CLI string into a path, because fire never coerces.

    Args:
        value: Raw flag value, or ``None`` when the flag was not supplied.

    Returns:
        The coerced path, or ``None`` when nothing was supplied.

    Examples:
        >>> _optional_path(None) is None
        True
        >>> _optional_path("benchmarks/suites/tasks-bench.json").name
        'tasks-bench.json'
    """
    return None if value is None else Path(value)


def _print_rescore(run_dir: Path) -> None:
    """Print the offline rescore artifact path; fire must never see a return value.

    Args:
        run_dir: Completed run directory holding frozen telemetry and tasks.

    Examples:
        >>> _print_rescore.__name__
        '_print_rescore'
    """
    print(rescore_results(run_dir))


def _print_task_selection(manifest_path: Path, selectors: str | Sequence[str]) -> None:
    """Print the resolved targeted scope as canonical JSON on standard output.

    Args:
        manifest_path: Active benchmark manifest defining the locked task suite.
        selectors: Comma-separated exact task IDs or task families.

    Examples:
        >>> _print_task_selection.__name__
        '_print_task_selection'
    """
    print(json.dumps(manifest_module.resolve_task_selection(manifest_path, selectors), sort_keys=True))


def _validate_cli_modes(
    *,
    render_results: bool,
    rescore_results_dir: str | None,
    resolve_tasks: str | Sequence[str] | None,
    force_color: bool,
    hide_plan: bool,
) -> None:
    """Reject the mutually exclusive CLI mode combinations fire cannot express.

    Args:
        render_results: Whether the stream-rendering mode was requested.
        rescore_results_dir: Run directory for the offline rescore mode, if any.
        resolve_tasks: Selectors for the task-resolution mode, if any.
        force_color: Whether renderer coloring was forced.
        hide_plan: Whether renderer PLAN filtering was requested.

    Raises:
        SystemExit: When two exclusive modes or a renderer-only flag are combined.

    Examples:
        >>> _validate_cli_modes(
        ...     render_results=True,
        ...     rescore_results_dir=None,
        ...     resolve_tasks=None,
        ...     force_color=True,
        ...     hide_plan=True,
        ... ) is None
        True
    """
    if force_color and not render_results:
        _cli_error("--force-color requires --render-results")
    if hide_plan and not render_results:
        _cli_error("--hide-plan requires --render-results")
    if rescore_results_dir is not None and (render_results or resolve_tasks is not None):
        _cli_error("--rescore-results cannot be combined with rendering or task resolution")
    if resolve_tasks is not None and render_results:
        _cli_error("--resolve-tasks cannot be combined with --render-results")


def _require_execution_options(**options: object) -> None:
    """Require every execution-only option, reporting the missing flag by name.

    Args:
        **options: Parameter name to supplied value, in the order to report.

    Raises:
        SystemExit: When any supplied value is ``None``.

    Examples:
        >>> _require_execution_options(repo_path="repo", model="gpt", tasks_path="tasks.json") is None
        True
    """
    for option, value in options.items():
        if value is None:
            _cli_error(f"--{option.replace('_', '-')} is required unless --render-results is used")


def _resolve_execution_scope(
    *,
    selection: Mapping[str, Any],
    repo_path: Path,
    model: str,
    reasoning_effort: str,
    manifest_path: Path,
    index_path: Path,
    index_relocation: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind the selected stage partitions into one immutable execution scope.

    ``index_relocation`` is present only for a run outside the canonical clone; each executable stage admits the
    relocated graph on that provenance rather than on the byte hash the lock recorded.
    """
    from _bench_codex.stage_fix import resolve_fix_stage_scope
    from _bench_codex.stage_readcrop import resolve_readcrop_stage_scope

    _validate_codex_stratum(model, reasoning_effort, manifest_path)
    scoped_stages: list[dict[str, Any]] = []
    for stage in selection["stages"]:
        stage_id = str(stage["stage_id"])
        task_ids = list(stage["task_ids"])
        if stage_id == "structural":
            child_scope = {
                "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "task_ids": task_ids,
                "repetitions": stage["repetitions"],
                "arms": stage["arms"],
                "coordinate_timeout_seconds": _task_selection_contract(manifest_path)["coordinate_timeout_seconds"],
            }
            child_scope["scope_sha256"] = _targeted_scope_sha256(child_scope)
        elif stage_id == "readcrop":
            child_scope = resolve_readcrop_stage_scope(
                repo_path=repo_path,
                model=model,
                tasks_selector=",".join(task_ids),
                structural_manifest_path=manifest_path,
            )
        else:
            child_scope = resolve_fix_stage_scope(
                study=stage_id,
                repo_path=repo_path,
                selected=set(task_ids),
                model=model,
                index_path=index_path,
                index_relocation=index_relocation,
            )
        scoped_stages.append({**stage, "scope_sha256": child_scope["scope_sha256"]})
    aggregate = {
        "schema_version": "codex-unified-scope-v1",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "model": model,
        "reasoning_effort": reasoning_effort,
        "selection_mode": selection["selection_mode"],
        "task_ids": selection["task_ids"],
        "stages": scoped_stages,
        "total_tasks": selection["total_tasks"],
        "total_cells": selection["total_cells"],
    }
    aggregate["scope_sha256"] = hashlib.sha256(
        json.dumps(aggregate, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return aggregate


def _write_unified_metadata(path: Path, payload: Mapping[str, Any]) -> None:
    """Persist aggregate lifecycle state without stage-specific quality fields."""
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _annotate_stage_metadata(stage_dir: Path, stage_id: str) -> None:
    """Ensure every child artifact identifies its native scorer stage."""
    path = stage_dir / "run-metadata.json"
    if not path.is_file():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stage_id"] = stage_id
    _write_unified_metadata(path, payload)


def _run_unified_execution(
    *,
    repo_path: Path,
    model: str,
    reasoning_effort: str,
    tasks: str | Sequence[str] | None,
    manifest_path: Path,
    index_path: Path,
    marketplace_root: Path,
    codemap_bin: Path,
    auth_source: Path | None,
    invocation_launcher_path: Path | None,
    run_dir: Path | None,
    paid_approval: str | None,
    dry_run: bool,
    show_legend: bool,
    index_relocation_path: Path | None = None,
    show_paid_command: bool = True,
) -> None:
    """Plan or execute the selected stage partitions under one authorization.

    ``index_relocation_path`` is present only for a run outside the canonical clone, whose index is the locked graph
    with its scan root moved; admission then checks that provenance rather than the byte hash, which reproduces at the
    canonical path alone.
    """
    from _bench_codex.stage_fix import run_fix_stage
    from _bench_codex.stage_readcrop import run_stage as run_readcrop_stage

    paid_approval = None if paid_approval is None else str(paid_approval)
    relocation = load_index_relocation(index_relocation_path)
    selection = manifest_module.resolve_task_selection(manifest_path, tasks)
    if not dry_run:
        missing = [
            flag
            for flag, value in (
                ("--auth-source", auth_source),
                ("--run-dir", run_dir),
                ("--paid-approval", paid_approval),
            )
            if value is None
        ]
        if missing:
            raise ValueError(
                f"cannot start model execution; missing {', '.join(missing)}. "
                "Run the same command with --dry-run and copy its PAID_COMMAND exactly."
            )
        if not isinstance(paid_approval, str) or re.fullmatch(r"[0-9a-f]{16,64}", paid_approval) is None:
            raise ValueError(
                "paid approval must be the 16-character token printed by --dry-run "
                "or a longer matching lowercase SHA-256 prefix. No model call was made."
            )
    scope = _resolve_execution_scope(
        selection=selection,
        repo_path=repo_path,
        model=model,
        reasoning_effort=reasoning_effort,
        manifest_path=manifest_path,
        index_path=index_path,
        index_relocation=relocation,
    )
    if not dry_run:
        if not paid_approval_matches(paid_approval, str(scope["scope_sha256"])):
            raise ValueError(
                "paid approval does not match the current aggregate scope. No model call was made. "
                "Run the same command with --dry-run and copy its PAID_COMMAND exactly."
            )
        assert run_dir is not None
        if run_dir.exists():
            raise FileExistsError(
                f"run directory already exists: {run_dir}. Use the fresh --run-dir printed by --dry-run."
            )

    def run_stage(stage: Mapping[str, Any], *, child_dir: Path | None) -> None:
        """Dispatch one already-resolved partition to its native stage engine."""
        stage_id = str(stage["stage_id"])
        task_ids = list(stage["task_ids"])
        if stage_id == "structural":
            selected = selection["selection_mode"] == "selected"
            main(
                repo_path=repo_path,
                model=model,
                reasoning_effort=reasoning_effort,
                tasks_path=BENCHMARKS_DIR / "suites" / "tasks-bench.json",
                manifest_path=manifest_path,
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                auth_source=auth_source,
                invocation_launcher_path=invocation_launcher_path,
                output_path=None if child_dir is None else child_dir / "telemetry.jsonl",
                metadata_path=None if child_dir is None else child_dir / "run-metadata.json",
                task_ids=None if selected else task_ids,
                task_selectors=",".join(task_ids) if selected else None,
                scope_sha256=stage["scope_sha256"] if selected else None,
                repetitions=int(stage["repetitions"]),
                index_relocation_path=index_relocation_path,
                dry_run=dry_run,
                show_legend=show_legend,
            )
        elif stage_id == "readcrop":
            run_readcrop_stage(
                repo_path=repo_path,
                model=model,
                tasks_selector=",".join(task_ids),
                dry_run_requested=dry_run,
                resolve_scope_requested=False,
                auth_source=auth_source,
                run_dir=child_dir,
                paid_approval=stage["scope_sha256"],
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                structural_manifest_path=manifest_path,
                emit_authorization=False,
                index_relocation=relocation,
            )
        else:
            run_fix_stage(
                study=stage_id,
                repo_path=repo_path,
                selected=set(task_ids),
                dry_run=dry_run,
                resolve_scope=False,
                auth_source=auth_source,
                run_dir=child_dir,
                paid_approval=stage["scope_sha256"],
                model=model,
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                emit_authorization=False,
                index_relocation=relocation,
            )

    if dry_run:
        for stage in scope["stages"]:
            run_stage(stage, child_dir=None)
        print(f"DESIGN   {scope['total_tasks']} tasks × A/B/C = {scope['total_cells']} cells")
        print(f"SCOPE   {scope['scope_sha256']}")
        if show_paid_command:
            runtime.print_unified_paid_command(
                repo_path=repo_path,
                manifest_path=manifest_path,
                index_path=index_path,
                marketplace_root=marketplace_root,
                codemap_bin=codemap_bin,
                model=model,
                selectors=selection["selectors"],
                scope_sha256=scope["scope_sha256"],
                patch_pytest=(
                    str(patch_test_runtime_identity()["pytest_executable"])
                    if any(stage["stage_id"] == "patch" for stage in scope["stages"])
                    else None
                ),
                index_relocation_path=index_relocation_path,
            )
        return

    assert run_dir is not None
    metadata_path = run_dir / "run-metadata.json"
    metadata: dict[str, Any] = {
        "schema_version": "codex-unified-run-v1",
        "status": "running",
        "scope": scope,
        "stages": [],
        "started_at": _utc_now(),
    }
    stage_design = ", ".join(
        f"{stage['stage_id']}={len(stage['task_ids'])} tasks/{stage['total_cells']} cells" for stage in scope["stages"]
    )
    runtime.print_section_rule("CODEX UNIFIED A/B/C STUDY")
    print(f"→ aggregate: {scope['total_tasks']} tasks, {scope['total_cells']} cells")
    print(f"→ sequential stages: {stage_design}")
    run_dir.mkdir(parents=True, exist_ok=False)
    _write_unified_metadata(metadata_path, metadata)
    aggregate_completed = 0
    try:
        for stage_number, stage in enumerate(scope["stages"], start=1):
            stage_id = str(stage["stage_id"])
            child_dir = run_dir / stage_id
            stage_record = {"stage_id": stage_id, "status": "running", "path": stage_id}
            metadata["stages"].append(stage_record)
            _write_unified_metadata(metadata_path, metadata)
            runtime.print_section_rule(
                f"STAGE {stage_number}/{len(scope['stages'])}: {stage_id} "
                f"({len(stage['task_ids'])} tasks, {stage['total_cells']} cells)"
            )
            try:
                with runtime.progress_scope(
                    completed_offset=aggregate_completed,
                    total_cells=int(scope["total_cells"]),
                ):
                    run_stage(stage, child_dir=child_dir)
            finally:
                _annotate_stage_metadata(child_dir, stage_id)
                if child_dir.is_dir():
                    write_checksums(child_dir)
            stage_record["status"] = "completed"
            aggregate_completed += int(stage["total_cells"])
            _write_unified_metadata(metadata_path, metadata)
        metadata["status"] = "completed"
    except BaseException as exc:
        metadata["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        metadata["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        if metadata["stages"] and metadata["stages"][-1]["status"] == "running":
            metadata["stages"][-1]["status"] = metadata["status"]
        raise
    finally:
        metadata["completed_at"] = _utc_now()
        _write_unified_metadata(metadata_path, metadata)
        write_checksums(run_dir)
        print(f"SUMMARY  status={metadata['status']}  stages={len(metadata['stages'])}/{len(scope['stages'])}")
    print(f"done: {run_dir}")


def cli(
    render_results: bool = False,
    rescore_results: str | None = None,
    resolve_tasks: str | Sequence[str] | None = None,
    force_color: bool = False,
    hide_plan: bool = False,
    repo_path: str | None = None,
    model: str | None = PARITY_CODEX_MODEL,
    reasoning_effort: str = PARITY_CODEX_REASONING_EFFORT,
    manifest_path: str | Path = PARITY_MANIFEST_PATH,
    index_path: str | None = None,
    marketplace_root: str | None = None,
    codemap_bin: str | None = None,
    auth_source: str | None = None,
    invocation_launcher_path: str | None = None,
    tasks: str | Sequence[str] | None = None,
    dry_run: bool = False,
    no_legend: bool = False,
    no_paid_command: bool = False,
    run_dir: str | None = None,
    paid_approval: str | None = None,
    rescore_fix_run_dir: str | None = None,
    rescore_fix_output_dir: str | None = None,
    rescore_readcrop_run_dir: str | None = None,
    rescore_readcrop_output_dir: str | None = None,
    index_relocation_path: str | None = None,
) -> None:
    """Dispatch one unified Codex benchmark, rendering, resolution, or rescore mode.

    Benchmark execution is task-driven. Omitting ``tasks`` executes all 55
    structural, 6 ReadCrop, 4 Fix-Single, 3 Fix-Multi, and 5 Patch tasks: 73
    tasks and 219 A/B/C cells. Family selectors such as ``RC,FS,FM,PT`` and
    mixed exact IDs such as ``RC-01,FS-03,FM-02,PT-04`` route to their native stage scorers while
    retaining separate child artifacts. Absence of ``dry_run`` means model
    execution and therefore requires authentication, a fresh run directory,
    and the aggregate approval printed by the matching dry run.

    Exactly one non-execution mode may run per invocation: stream rendering,
    offline rescoring, or selector resolution. Every branch prints its own
    output and returns ``None`` because Fire echoes returned values and would
    corrupt machine-parsed standard output.

    Args:
        render_results: Render progress rows read from standard input.
        rescore_results: Completed run directory to replay into an immutable
            offline rescore artifact; prints the artifact path.
        resolve_tasks: Comma-separated exact task IDs or task families to resolve
            into a targeted scope; prints the scope as canonical JSON.
        force_color: Force terminal coloring in the renderer; requires
            ``--render-results``. Test-only.
        hide_plan: Drop human ``PLAN`` rows in the renderer; requires
            ``--render-results``. Test-only.
        repo_path: Target repository clone; required for execution.
        model: Codex model identifier; defaults to the current parent model.
        reasoning_effort: Locked Codex reasoning stratum; only
            ``PARITY_CODEX_REASONING_EFFORT`` is accepted.
        manifest_path: Active benchmark manifest defining the locked contract.
        index_path: Locked Codemap index consumed by the B and C arms.
        marketplace_root: Local plugin marketplace root for the C arm.
        codemap_bin: Direct Codemap launcher for the B arm.
        auth_source: User-owned ``auth.json`` copied into the disposable home.
        invocation_launcher_path: Recorded launcher whose digest is revalidated.
        tasks: Optional comma-separated task families or exact IDs. Omit it for
            the complete 73-task suite.
        dry_run: Validate locked inputs and print the cell plan without a model call.
        no_legend: Suppress the output legend block.
        no_paid_command: Suppress the dry run's PAID_COMMAND block; SCOPE still prints.
        run_dir: Fresh aggregate artifact directory required for model execution.
        paid_approval: Aggregate approval token printed by the matching dry run.

    Raises:
        SystemExit: With status ``2`` when flags are combined illegally, a
            required execution option is missing, or the reasoning stratum
            differs from the locked value.

    Examples:
        >>> cli.__name__
        'cli'
    """
    if render_results:
        # Fire has already identified renderer mode, but Windows subprocess tests
        # can lose the second bare Boolean. Preserve the explicit test-only flag.
        force_color = force_color or "--force-color" in sys.argv[1:]
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    _validate_cli_modes(
        render_results=render_results,
        rescore_results_dir=rescore_results,
        resolve_tasks=resolve_tasks,
        force_color=force_color,
        hide_plan=hide_plan,
    )
    if rescore_results is not None:
        _print_rescore(Path(rescore_results))
        return
    if resolve_tasks is not None:
        _print_task_selection(Path(manifest_path), resolve_tasks)
        return
    if render_results:
        runtime.render_result_rows(sys.stdin, sys.stdout, force_color=force_color, hide_plan=hide_plan)
        return
    if (rescore_readcrop_run_dir is None) != (rescore_readcrop_output_dir is None):
        _cli_error("--rescore-readcrop-run-dir requires --rescore-readcrop-output-dir")
    if rescore_readcrop_run_dir is not None:
        from _bench_codex.stage_readcrop import run_stage

        _require_execution_options(repo_path=repo_path)
        run_stage(
            repo_path=Path(str(repo_path)),
            model=str(model) if model is not None else PARITY_CODEX_MODEL,
            tasks_selector=None,
            dry_run_requested=False,
            resolve_scope_requested=False,
            auth_source=None,
            run_dir=None,
            paid_approval=None,
            structural_manifest_path=Path(manifest_path),
            rescore_run_dir=Path(str(rescore_readcrop_run_dir)),
            rescore_output_dir=Path(str(rescore_readcrop_output_dir)),
        )
        return
    if (rescore_fix_run_dir is None) != (rescore_fix_output_dir is None):
        _cli_error("--rescore-fix-run-dir requires --rescore-fix-output-dir")
    if rescore_fix_run_dir is not None:
        from _bench_codex.stage_fix import rescore_fix_stage

        _require_execution_options(repo_path=repo_path)
        output_dir = rescore_fix_stage(
            Path(str(rescore_fix_run_dir)),
            Path(str(rescore_fix_output_dir)),
            Path(str(repo_path)),
        )
        print(f"rescored: {output_dir}")
        return
    _validate_codex_stratum(str(model), reasoning_effort, Path(manifest_path))
    _require_execution_options(repo_path=repo_path, model=model)
    if reasoning_effort != PARITY_CODEX_REASONING_EFFORT:
        _cli_error(f"--reasoning-effort must be {PARITY_CODEX_REASONING_EFFORT!r}")
    resolved_repo = Path(str(repo_path))
    resolved_index = _optional_path(index_path) or resolved_repo / ".cache" / "codemap" / f"{resolved_repo.name}.json"
    _run_unified_execution(
        repo_path=resolved_repo,
        model=str(model),
        reasoning_effort=reasoning_effort,
        tasks=tasks,
        manifest_path=Path(manifest_path),
        index_path=resolved_index,
        marketplace_root=_optional_path(marketplace_root) or REPO_ROOT,
        codemap_bin=_optional_path(codemap_bin) or REPO_ROOT / "plugins" / "codemap-py" / "bin" / "codemap-py",
        auth_source=_optional_path(auth_source),
        invocation_launcher_path=_optional_path(invocation_launcher_path),
        run_dir=_optional_path(run_dir),
        paid_approval=paid_approval,
        index_relocation_path=_optional_path(index_relocation_path),
        dry_run=dry_run,
        show_legend=not no_legend,
        show_paid_command=not no_paid_command,
    )
