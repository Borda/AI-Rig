"""Paid-run lifecycle: scope approval, stage execution, and frozen input snapshots."""

import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from _bench_common import presentation

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)
from _bench_common.artifact_hashing import runner_sha256
from _bench_common.change_impact_contracts import source_fingerprint as change_impact_source_fingerprint

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (
    FIX_SINGLE_ARMS,
    PARITY_MANIFEST_PATH,
    PATCH_TASKS_PATH,
    READCROP_ARMS,
    _claude_codemap_evidence,
    _claude_event_summary,
    _frozen_index_recovery_attempted,
    _outside_workspace_path_evidence,
    _patch_index_path,
    _provider_binding,
    _study_query_arguments,
    load_claude_fix_multi_tasks,
    load_claude_fix_single_tasks,
    load_claude_patch_tasks,
    load_claude_readcrop_tasks,
    parse_claude_readcrop_events,
    readcrop_prompt,
    resolve_claude_fix_multi_scope,
    resolve_claude_fix_single_scope,
    resolve_claude_patch_scope,
    resolve_readcrop_scope,
)
from _bench_common.claude_transport import MODEL_TIMEOUT, MODELS, parse_result_usage
from _bench_common.edit_patch_contracts import (
    EditExecution,
    EditTaskContract,
    build_patch_answer,
    score_edit_execution,
    validate_patch_index_bundle,
)
from _bench_common.mutation_isolation import (
    PATCH_PYTEST_ENV,
    create_executable_agent_workspace,
    create_patch_task_agent_workspace,
    execute_fix_multi_patch,
    execute_fix_single_patch,
    execute_patch_task_answer,
    patch_test_runtime_identity,
    relocate_frozen_index_for_worktree,
)
from _bench_common.paid_lifecycle import (
    PaidStageCallbacks,
    paid_approval_matches,
    paid_approval_token,
    run_paid_stage,
    write_checksums,
)
from _bench_common.presentation import (
    fmt_time,
    fmt_tok,
    format_artifact_block,
    format_paid_command_block,
    format_quality,
)
from _bench_common.provider_parity_contracts import (
    ARM_CONTRACTS,
    fresh_input_tokens,
    token_accounting_inconsistent,
    treatment_adherence,
)

from _bench_claude.agentic.config import (
    BENCHMARKS_DIR,
    FIX_MULTI_ARMS,
    PACKAGE_DIR,
    PATCH_ARMS,
    PATCH_INDEX_LOCKS_PATH,
    RUNNER_PATH,
    _console,
)
from _bench_claude.agentic.discovery import find_index
from _bench_claude.agentic.evidence import (
    _benchmark_evidence_roots,
    _claude_evidence_settings_file,
    _staged_codemap_runtime,
)
from _bench_claude.agentic.provenance import _repository_fingerprint, _sha256_file, _validate_parity_runtime
from _bench_claude.agentic.runner import ModelRunner


def _resolve_claude_paid_scope(
    *,
    base_scope: Mapping[str, Any],
    repo_path: Path,
    index_path: Path,
    model: str,
    index_relocation: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind one Claude paid stage to runtime, transport, and treatment bytes."""
    if model not in MODELS:
        raise ValueError(f"Claude paid stage model must be one of {', '.join(MODELS)}")
    repo_path = repo_path.resolve(strict=True)
    index_path = index_path.resolve(strict=True)
    _validate_parity_runtime(repo_path, index_path, PARITY_MANIFEST_PATH, index_relocation)
    plugin_root_text = ModelRunner._codemap_plugin_dir()
    if plugin_root_text is None:
        raise ValueError("canonical Claude paid stage requires the repository Codemap plugin fixture")
    plugin_root = Path(plugin_root_text)
    treatment_paths = (
        plugin_root / ".claude-plugin" / "plugin.json",
        plugin_root / "claude-skills" / "query-code" / "SKILL.md",
        plugin_root / "bin" / "codemap-py",
        plugin_root / "bin" / "scan-query",
    )
    if any(not path.is_file() for path in treatment_paths):
        raise ValueError("canonical Claude paid stage treatment artifact is incomplete")
    payload = {key: value for key, value in base_scope.items() if key != "scope_sha256"}
    source_binding: dict[str, Any] = {
        "repo_path": str(repo_path),
        "commit": _repository_fingerprint(repo_path),
        "index_path": str(index_path),
        "index_sha256": _sha256_file(index_path),
    }
    if base_scope.get("study") == "patch":
        historical_baselines = base_scope.get("historical_baselines")
        if not isinstance(historical_baselines, Mapping):
            raise ValueError("Claude patch scope lacks its contract-bound historical baselines")
        loaded = load_claude_patch_tasks(
            PATCH_TASKS_PATH,
            PARITY_MANIFEST_PATH,
            [str(task_id) for task_id in base_scope["task_ids"]],
        )
        try:
            coordinates = validate_patch_index_bundle(
                repo_path, PATCH_INDEX_LOCKS_PATH, [item["contract"] for item in loaded]
            )
        except ValueError as exc:
            raise ValueError(
                f"Claude patch stage input preflight failed: {exc}. No model call was made; "
                "rebuild the frozen patch coordinates and rerun --dry-run."
            ) from exc
        if {task_id: coordinate["baseline_commit"] for task_id, coordinate in coordinates.items()} != dict(
            historical_baselines
        ):
            raise ValueError("Claude patch index coordinates changed contract-bound historical baselines")
        source_binding["patch_coordinates"] = coordinates
        payload["patch_test_runtime"] = patch_test_runtime_identity()
    payload.update(
        {
            "model": model,
            "model_id": MODELS[model],
            "source_binding": source_binding,
            "implementation_sha256": {
                "claude_runner": runner_sha256(RUNNER_PATH, PACKAGE_DIR),
                "paid_lifecycle": _sha256_file(BENCHMARKS_DIR / "_bench_common" / "paid_lifecycle.py"),
                "claude_transport": _sha256_file(BENCHMARKS_DIR / "_bench_common" / "claude_transport.py"),
                "mutation_isolation": _sha256_file(BENCHMARKS_DIR / "_bench_common" / "mutation_isolation.py"),
                **(
                    {
                        "edit_patch_contracts": _sha256_file(
                            BENCHMARKS_DIR / "_bench_common" / "edit_patch_contracts.py"
                        ),
                        "patch_index_locks": _sha256_file(PATCH_INDEX_LOCKS_PATH),
                    }
                    if base_scope.get("study") == "patch"
                    else {}
                ),
            },
            "treatment_sha256": {
                path.relative_to(plugin_root).as_posix(): _sha256_file(path) for path in treatment_paths
            },
        }
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "scope_sha256": hashlib.sha256(encoded).hexdigest()}


def _suggested_claude_run_dir(study: str) -> Path:
    """Return a collision-resistant result path without reserving it."""
    from uuid import uuid4

    return Path("benchmarks") / "results" / f"claude-{study}-{uuid4().hex[:12]}"


def _print_claude_paid_command(
    *,
    study: str,
    repo_path: Path,
    index_path: Path,
    model: str,
    task_ids: Sequence[str],
    scope_sha256: str,
    patch_pytest: str | None = None,
) -> None:
    """Print the exact paid command admitted by the current immutable scope."""
    prefix = f"{PATCH_PYTEST_ENV}={shlex.quote(patch_pytest)} " if patch_pytest else ""
    print(
        format_paid_command_block(
            [
                f"{prefix}python3 benchmarks/run-claude-agentic.py \\",
                f"  --study {study} \\",
                f"  --repo-path {repo_path.resolve()} \\",
                f"  --index {index_path.resolve()} \\",
                f"  --model {model} \\",
                f"  --tasks {','.join(task_ids)} \\",
                f"  --run-dir {_suggested_claude_run_dir(study)} \\",
                f"  --paid-approval {paid_approval_token(scope_sha256)}",
            ]
        )
    )


def _require_claude_paid_request(
    *,
    study: str,
    run_dir: Path | None,
    paid_approval: str | None,
    scope: Mapping[str, Any],
    repo_path: Path,
    index_path: Path,
    model: str,
) -> None:
    """Reject incomplete, stale, or overwrite-prone paid requests before Claude starts."""
    paid_approval = None if paid_approval is None else str(paid_approval)
    expected = str(scope["scope_sha256"])
    task_ids = [str(task_id) for task_id in scope["task_ids"]]
    patch_runtime = scope.get("patch_test_runtime")
    patch_pytest = (
        str(patch_runtime["pytest_executable"]) if study == "patch" and isinstance(patch_runtime, Mapping) else None
    )
    prefix = f"{PATCH_PYTEST_ENV}={shlex.quote(patch_pytest)} " if patch_pytest else ""
    approval_matches = paid_approval_matches(paid_approval, expected)
    if run_dir is None or not approval_matches or Path(run_dir).exists():
        reasons: list[str] = []
        if run_dir is None:
            reasons.append("--run-dir is missing")
        elif Path(run_dir).exists():
            reasons.append(f"--run-dir already exists: {run_dir}")
        if not approval_matches:
            reasons.append("stale or missing --paid-approval")
        preflight_command = (
            f"{prefix}python3 benchmarks/run-claude-agentic.py \\\n"
            f"  --study {study} \\\n"
            f"  --repo-path {repo_path.resolve()} \\\n"
            f"  --index {index_path.resolve()} \\\n"
            f"  --model {model} \\\n"
            f"  --tasks {','.join(task_ids)} \\\n"
            "  --dry-run"
        )
        fresh_run_dir = _suggested_claude_run_dir(study)
        paid_command = (
            f"{prefix}python3 benchmarks/run-claude-agentic.py \\\n"
            f"  --study {study} \\\n"
            f"  --repo-path {repo_path.resolve()} \\\n"
            f"  --index {index_path.resolve()} \\\n"
            f"  --model {model} \\\n"
            f"  --tasks {','.join(task_ids)} \\\n"
            f"  --run-dir {fresh_run_dir} \\\n"
            f"  --paid-approval {paid_approval_token(expected)}"
        )
        raise ValueError(
            f"ERROR: cannot start paid Claude {study} stage.\n"
            f"Reasons:\n{chr(10).join(f'  - {reason}' for reason in reasons)}\n"
            f"  - received: {paid_approval or '(missing)'}\n"
            f"  - required token: {paid_approval_token(expected)}\n"
            "No model call was made.\n\n"
            "Updated paid command (copy as-is):\n"
            f"{paid_command}\n\n"
            "No-model preflight (copy as-is):\n"
            f"{preflight_command}"
        )


def _run_claude_p1_stage(
    *,
    study: str,
    repo_path: Path | None,
    index: Path | None,
    tasks_path: Path,
    manifest_path: Path,
    selected_ids: Sequence[str] | None,
    model: str | None,
    run_dir: Path | None,
    paid_approval: str | None,
    dry_run: bool,
    resolve_scope: bool,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Dispatch one canonical Claude P1 stage without duplicating its execution loop."""
    if study == "readcrop":
        if repo_path is None:
            raise ValueError("Claude ReadCrop requires --repo-path for its source-bound oracle")
        loaded = load_claude_readcrop_tasks(repo_path, tasks_path, manifest_path, selected_ids)
        base_scope = resolve_readcrop_scope(loaded, manifest_path, tasks_path)
        arms = READCROP_ARMS
    elif study == "fix-single":
        loaded = load_claude_fix_single_tasks(tasks_path, manifest_path, selected_ids)
        base_scope = resolve_claude_fix_single_scope(loaded, manifest_path, tasks_path)
        arms = FIX_SINGLE_ARMS
    elif study == "fix-multi":
        loaded = load_claude_fix_multi_tasks(tasks_path, manifest_path, selected_ids)
        base_scope = resolve_claude_fix_multi_scope(loaded, manifest_path, tasks_path)
        arms = FIX_MULTI_ARMS
    elif study == "patch":
        loaded = load_claude_patch_tasks(tasks_path, manifest_path, selected_ids)
        base_scope = resolve_claude_patch_scope(loaded, manifest_path, tasks_path)
        arms = PATCH_ARMS
    else:
        raise ValueError(f"unsupported Claude stage {study!r}")

    full_scope: Mapping[str, Any] | None = None
    index_path: Path | None = None
    if repo_path is not None and model is not None:
        index_path = find_index(repo_path, index)
        full_scope = _resolve_claude_paid_scope(
            base_scope=base_scope,
            repo_path=repo_path,
            index_path=index_path,
            model=model,
            index_relocation=index_relocation,
        )
    visible_scope = full_scope or base_scope
    if resolve_scope:
        print(json.dumps(dict(visible_scope), sort_keys=True))
        return
    if dry_run:
        presentation.print_section_rule(f"{study.upper()} PREFLIGHT (no model)", console=_console)
        for item in loaded:
            for arm in arms:
                presentation.print_plan_row(f"PLAN    {item['contract'].task_id:<6} rep=1  {arm}", console=_console)
        print(f"SCOPE   {visible_scope['scope_sha256']}")
        if full_scope is not None and repo_path is not None and index_path is not None and model is not None:
            _print_claude_paid_command(
                study=study,
                repo_path=repo_path,
                index_path=index_path,
                model=model,
                task_ids=[str(item["contract"].task_id) for item in loaded],
                scope_sha256=str(full_scope["scope_sha256"]),
                patch_pytest=(str(full_scope["patch_test_runtime"]["pytest_executable"]) if study == "patch" else None),
            )
        return
    if repo_path is None or index_path is None or model is None or full_scope is None:
        raise ValueError(
            f"Claude {study} paid execution requires --repo-path, --index, and --model. "
            "No model call was made; rerun with those inputs and --dry-run for the exact command."
        )
    _require_claude_paid_request(
        study=study,
        run_dir=run_dir,
        paid_approval=paid_approval,
        scope=full_scope,
        repo_path=repo_path,
        index_path=index_path,
        model=model,
    )
    assert run_dir is not None
    run_claude_paid_stage(
        study=study,
        tasks=loaded,
        repo_path=repo_path,
        index_path=index_path,
        manifest_path=manifest_path,
        tasks_path=tasks_path,
        model=model,
        run_dir=run_dir,
        scope=full_scope,
    )


@contextlib.contextmanager
def _claude_readcrop_workspace(
    repo_path: Path, index_path: Path, arm: str
) -> Iterator[tuple[Path, Mapping[str, str] | None]]:
    """Yield an index-free A copy or root-relocated B/C copy for ReadCrop."""
    import shutil

    with tempfile.TemporaryDirectory(prefix="claude-readcrop-") as temporary:
        cwd = Path(temporary) / repo_path.name
        shutil.copytree(repo_path, cwd, ignore=shutil.ignore_patterns(".cache", ".git"), symlinks=True)
        relocation: Mapping[str, str] | None = None
        if arm != "A_plain":
            derived_bytes, relocation = relocate_frozen_index_for_worktree(
                index_path.read_bytes(), source_root=repo_path, worktree_root=cwd
            )
            derived_path = cwd / ".cache" / "codemap" / f"{cwd.name}.json"
            derived_path.parent.mkdir(parents=True, exist_ok=True)
            derived_path.write_bytes(derived_bytes)
        yield cwd, relocation


def _claude_fix_prompt(study: str, arm: str, item: Mapping[str, Any]) -> str:
    """Materialize one canonical executable Claude prompt with arm-only treatment text."""
    task = item["task"]
    task_id = str(item["contract"].task_id)
    query_map = _study_query_arguments(study)
    query = " ".join(query_map[task_id])
    treatments = {
        "A_plain": "Codemap is absent and inaccessible. Use ordinary repository tools.",
        "B_auto": (
            "Codemap is installed through /codemap-py:query-code and may be used for static symbol, caller, importer, "
            "or dependency facts when useful."
        ),
        "C_strict": (
            "Codemap is installed. Before any source read or edit, invoke the exact Skill query "
            f"`/codemap-py:query-code {query}`. Then run `codemap-py query {query}` in Bash and wait for "
            "its successful result. A C_strict cell is accepted only after the harness observes that exact completed "
            "Codemap query; loading the Skill alone does not satisfy the treatment. Use direct source reads for runtime "
            "facts or final confirmation."
        ),
    }
    try:
        treatment = treatments[arm]
    except KeyError as exc:
        raise ValueError(f"unsupported Claude {study} arm {arm!r}") from exc
    return (
        f"{treatment}\n\n{task['prompt']}\n\n"
        "You are inside a benchmark-owned disposable checkout. Implement the minimal complete change directly in "
        "the checkout. Do not invoke Git, commit, reset, clean, change benchmark metadata, or return a diff. Modify "
        "only task-required source paths. Codemap is a frozen static graph: never use it to validate runtime behavior, "
        "execute tests, or apply edits. Finish with a concise summary; the harness captures and scores the Git diff."
    )


def _impact_arm_envelope(arm: str) -> str:
    """Return the generic A/B/C availability instruction for source-impact predictions."""
    try:
        contract = ARM_CONTRACTS[arm]["contract"]
    except KeyError as exc:
        raise ValueError(f"unknown Claude change-impact arm {arm!r}") from exc
    return f"{contract} Do not edit source files; report the requested source-level compatibility prediction."


def _native_change_impact_preflight(source_root: Path, run_dir: Path) -> None:
    """Verify the installed Claude launcher and every isolated arm profile without a model request."""
    launcher = shutil.which("claude")
    if launcher is None:
        raise RuntimeError("Claude change-impact preflight requires an installed claude launcher")
    version = subprocess.run([launcher, "--version"], capture_output=True, text=True, timeout=30, check=False)
    if version.returncode != 0:
        raise RuntimeError(f"Claude change-impact preflight failed: {version.stderr.strip()[:300]}")
    denied_evidence = _benchmark_evidence_roots((run_dir,))
    for arm in READCROP_ARMS:
        with _staged_codemap_runtime(arm, source_root) as runtime:
            runtime_paths = [runtime] if runtime is not None else []
            with _claude_evidence_settings_file(
                source_root,
                evidence_roots=denied_evidence,
                runtime_paths=runtime_paths,
            ):
                pass


@contextlib.contextmanager
def impact_runtime(
    *,
    model: str,
    source_root: Path,
    index_path: Path,
    run_dir: Path,
    timeout: int,
    dry_run: bool,
    fixture_runtime_coordinate: Mapping[str, Any],
) -> Iterator[SimpleNamespace | None]:
    """Yield one Claude change-impact cell adapter over a parent-isolated fixture.

    The paid lifecycle owns fixture creation, source/index inventory, approval, and scoring. This transport-only adapter
    validates its immutable fixture coordinate, then records only native Claude stream facts for one no-edit analysis
    cell. ``dry_run`` validates the coordinate but deliberately exposes no callable.
    """
    if model not in MODELS:
        raise ValueError(f"Claude change-impact model must be one of {', '.join(MODELS)}")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("Claude change-impact timeout must be a positive integer")
    try:
        source_root = source_root.resolve(strict=True)
        index_path = index_path.resolve(strict=True)
        run_dir = run_dir.resolve(strict=True)
    except OSError as exc:
        raise ValueError("Claude change-impact runtime coordinate is unavailable") from exc
    if run_dir == source_root or run_dir.is_relative_to(source_root) or source_root.is_relative_to(run_dir):
        raise ValueError("Claude change-impact run directory must be outside the source fixture")
    expected_index = source_root / ".cache" / "codemap" / f"{source_root.name}.json"
    if index_path != expected_index:
        raise ValueError("Claude change-impact index must be the fixture's canonical Codemap cache path")
    required = {"source_fingerprint", "raw_index_sha256", "scan_version", "index_scan_root"}
    if set(fixture_runtime_coordinate) != required:
        raise ValueError("Claude change-impact fixture runtime coordinate has unexpected fields")
    source_digest = fixture_runtime_coordinate["source_fingerprint"]
    raw_index_digest = fixture_runtime_coordinate["raw_index_sha256"]
    scan_version = fixture_runtime_coordinate["scan_version"]
    scan_root = fixture_runtime_coordinate["index_scan_root"]
    if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", source_digest):
        raise ValueError("Claude change-impact source fingerprint must be a SHA-256 hex digest")
    if not isinstance(raw_index_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_index_digest):
        raise ValueError("Claude change-impact raw index SHA-256 must be a hex digest")
    if not isinstance(scan_version, int) or isinstance(scan_version, bool) or scan_version < 1:
        raise ValueError("Claude change-impact scan version must be a positive integer")
    if not isinstance(scan_root, str) or not scan_root:
        raise ValueError("Claude change-impact index scan root must be a non-empty string")
    if change_impact_source_fingerprint(source_root) != source_digest:
        raise ValueError("Claude change-impact source fixture fingerprint drifted")
    if _sha256_file(index_path) != raw_index_digest:
        raise ValueError("Claude change-impact frozen index fingerprint drifted")
    try:
        index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Claude change-impact frozen index is unavailable or malformed") from exc
    if not isinstance(index_payload, Mapping):
        raise ValueError("Claude change-impact frozen index must be an object")
    if index_payload.get("scan_version") != scan_version or index_payload.get("scan_root") != scan_root:
        raise ValueError("Claude change-impact frozen index metadata drifted")
    if dry_run:
        _native_change_impact_preflight(source_root, run_dir)
        yield None
        return

    runner = ModelRunner(model, MODELS[model], source_root, timeout=timeout)

    def run_cell(task: Mapping[str, Any], arm: str, prompt: str) -> Mapping[str, Any]:
        """Run one immutable change-impact coordinate through Claude's isolated transport."""
        if not isinstance(task, Mapping) or not isinstance(task.get("id"), str) or not task["id"]:
            raise ValueError("Claude change-impact cell requires a task with a non-empty id")
        if arm not in READCROP_ARMS:
            raise ValueError(f"unsupported Claude change-impact arm {arm!r}")
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("Claude change-impact prompt must be non-empty")
        events, elapsed_s, transport_error = runner.run_stage_events(
            prompt=prompt,
            system_prompt=(
                f"{_impact_arm_envelope(arm)} Analyze only the benchmark-owned source fixture. Do not edit files, "
                "run tests, use Git, or inspect paths outside the fixture. Return the requested strict answer envelope."
            ),
            arm=arm,
            cwd=source_root,
            writable=False,
            evidence_roots=(run_dir,),
        )
        summary = _claude_event_summary(events)
        usage = summary["usage"]
        terminal_result = next((event for event in reversed(events) if event.get("type") == "result"), None)
        terminal_usage = terminal_result.get("usage") if isinstance(terminal_result, Mapping) else None
        usage_complete = isinstance(terminal_usage, Mapping) and all(
            isinstance(terminal_usage.get(field), (int, float))
            and not isinstance(terminal_usage.get(field), bool)
            and math.isfinite(terminal_usage[field])
            and terminal_usage[field] >= 0
            for field in ("input_tokens", "output_tokens")
        )
        codemap = _claude_codemap_evidence(events)
        codemap_calls = int(codemap["codemap_calls"])
        attempted_outside_paths, outside_paths = _outside_workspace_path_evidence(events, source_root)
        recovery_attempted = _frozen_index_recovery_attempted(events)
        contaminated = bool(
            outside_paths
            or recovery_attempted
            or (arm == "A_plain" and (codemap_calls > 0 or int(codemap["codemap_skill_launches"]) > 0))
        )
        codemap_compliance = None if arm == "A_plain" else bool(codemap["codemap_compact_success"])
        adherence = treatment_adherence(
            arm,
            codemap_use_compliance=codemap_compliance,
            contaminated=contaminated,
        )
        error = transport_error or (None if usage.success else usage.subtype or "missing Claude result")
        return {
            "success": bool(usage.success and transport_error is None and not contaminated),
            "incomplete": terminal_result is None,
            "contaminated": contaminated,
            "report_text": summary["output_text"],
            "raw_events": summary["raw_events"],
            "input_tokens": usage.input_tokens if usage_complete else None,
            "cached_input_tokens": (usage.cache_creation_tokens + usage.cache_read_tokens) if usage_complete else None,
            "output_tokens": usage.output_tokens if usage_complete else None,
            "usage_complete": usage_complete,
            "elapsed_s": elapsed_s,
            "command_calls": summary["command_calls"],
            "codemap_calls": codemap_calls,
            "codemap_used": bool(codemap["codemap_compact_success"]),
            "codemap_query_attempted": codemap["codemap_query_attempted"],
            "codemap_query_succeeded": codemap["codemap_query_succeeded"],
            "codemap_compact_success": codemap["codemap_compact_success"],
            "treatment_adherence": adherence,
            "error": error,
            "error_type": None if error is None else ("transport" if transport_error is not None else "provider"),
            "launcher_path": None,
        }

    yield SimpleNamespace(run_cell=run_cell)


def _parse_claude_fix_cell(
    *,
    study: str,
    item: Mapping[str, Any],
    arm: str,
    events: Sequence[Mapping[str, Any]],
    elapsed_s: float,
    transport_error: str | None,
    execution: Mapping[str, Any],
    workspace_cleanup_verified: bool,
    index_unchanged: bool,
    source_unchanged: bool,
    model: str,
    workspace_root: Path,
    captured_diff: str | None = None,
) -> dict[str, Any]:
    """Combine Claude transport facts with provider-neutral patch execution evidence."""
    summary = _claude_event_summary(events)
    usage = summary.pop("usage")
    codemap = _claude_codemap_evidence(events)
    codemap_calls = int(codemap["codemap_calls"])
    attempted_outside_paths, outside_paths = _outside_workspace_path_evidence(events, workspace_root)
    recovery_attempted = _frozen_index_recovery_attempted(events)
    contaminated = bool(
        (arm == "A_plain" and (codemap_calls > 0 or int(codemap["codemap_skill_launches"]) > 0))
        or outside_paths
        or recovery_attempted
    )
    expected_query = list(_study_query_arguments(study)[item["contract"].task_id])
    strict_query = None if arm != "C_strict" else expected_query in codemap["successful_query_arguments"]
    compliance = {
        "A_plain": not contaminated,
        "B_auto": True,
        "C_strict": bool(codemap["codemap_query_skill_launches"])
        and bool(codemap["codemap_successful_calls"])
        and bool(strict_query),
    }[arm]
    contract = item["contract"]
    if isinstance(contract, EditTaskContract):
        if captured_diff is None:
            raise ValueError("Claude Patch telemetry requires its captured candidate diff")
        scored = score_edit_execution(contract, build_patch_answer(captured_diff), EditExecution(**dict(execution)))
        path_ok = scored.changed_path_boundary_passed
        primary = scored.primary_correct
        score_pooling_eligible = scored.pooling_eligible
    else:
        path_ok = set(execution["changed_paths"]) == set(contract.expected_paths)
        primary = bool(
            execution["baseline_failed"] and execution["patch_applied"] and execution["targeted_test_passed"]
        )
        score_pooling_eligible = primary and path_ok
    success = bool(
        usage.success and transport_error is None and compliance and workspace_cleanup_verified and not contaminated
    )
    pooling_eligible = bool(
        success
        and score_pooling_eligible
        and execution["cleanup_verified"]
        and index_unchanged
        and source_unchanged
        and not contaminated
    )
    return {
        "study": study,
        "task_id": item["contract"].task_id,
        "arm": arm,
        "model": model,
        "success": success,
        "primary_correct": primary,
        "quality_score": 1.0 if primary else 0.0,
        "pooling_eligible": pooling_eligible,
        "input_tokens": usage.input_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cached_input_tokens": usage.cache_creation_tokens + usage.cache_read_tokens,
        "fresh_input_tokens": fresh_input_tokens(
            usage.input_tokens, usage.cache_creation_tokens + usage.cache_read_tokens
        ),
        "token_accounting_inconsistent": token_accounting_inconsistent(
            usage.input_tokens, usage.cache_creation_tokens + usage.cache_read_tokens
        ),
        "output_tokens": usage.output_tokens,
        "tool_result_tokens": None,
        "cost_usd": usage.cost_usd,
        "elapsed_s": elapsed_s,
        "command_calls": summary["command_calls"],
        "codemap_calls": codemap_calls,
        "codemap_successful_calls": codemap["codemap_successful_calls"],
        "codemap_skill_launches": codemap["codemap_skill_launches"],
        "codemap_query_skill_launches": codemap["codemap_query_skill_launches"],
        "codemap_attempted": codemap_calls > 0,
        "codemap_used": bool(codemap["codemap_successful_calls"]),
        "codemap_query_attempted": codemap["codemap_query_attempted"],
        "codemap_query_succeeded": codemap["codemap_query_succeeded"],
        "codemap_compact_success": codemap["codemap_compact_success"],
        "successful_query_arguments": codemap["successful_query_arguments"],
        "strict_query_conformance": strict_query,
        "compliance": compliance,
        "contaminated": contaminated,
        "attempted_outside_workspace_paths": attempted_outside_paths,
        "outside_workspace_paths": outside_paths,
        "frozen_index_recovery_attempted": recovery_attempted,
        "transport_error": transport_error,
        "native_subtype": usage.subtype,
        "execution": dict(execution),
        "patch_generated": bool(execution["changed_paths"]),
        "changed_path_boundary_passed": path_ok,
        "workspace_cleanup_verified": workspace_cleanup_verified,
        "index_unchanged": index_unchanged,
        "source_unchanged": source_unchanged,
        "provider_binding": dict(_provider_binding(item)),
        **summary,
    }


def _source_pair_unchanged(
    repo_path: Path, index_path: Path, scope: Mapping[str, Any], *, task_id: str | None = None
) -> bool:
    """Return whether one cell preserved its frozen source commit, status, and index bytes."""
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"], capture_output=True, text=True, check=False
    )
    expected = scope["source_binding"]
    patch_coordinates = expected.get("patch_coordinates")
    if task_id is not None and isinstance(patch_coordinates, Mapping):
        coordinate = patch_coordinates.get(task_id)
        if not isinstance(coordinate, Mapping):
            return False
        expected_index_sha256 = coordinate.get("index_sha256")
    else:
        expected_index_sha256 = expected["index_sha256"]
    return bool(
        status.returncode == 0
        and not status.stdout.strip()
        and _repository_fingerprint(repo_path) == expected["commit"]
        and _sha256_file(index_path) == expected_index_sha256
    )


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace one durable JSON artifact in its existing directory."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _patch_snapshot_files() -> dict[str, Path]:
    """Return the provider and shared implementation bytes frozen for a Claude Patch run.

    ``claude-runner.py`` keeps its existing key and still holds the entrypoint, but that file is
    now a re-export shim, so archiving it alone would freeze none of the code that actually ran.
    Every module of the implementation package is archived alongside it under
    ``claude-runner/``, additively: no existing key changes meaning.
    """
    benchmarks = BENCHMARKS_DIR
    snapshot = {
        "claude-runner.py": RUNNER_PATH,
        "paid-lifecycle.py": benchmarks / "_bench_common" / "paid_lifecycle.py",
        "edit-patch-contracts.py": benchmarks / "_bench_common" / "edit_patch_contracts.py",
        "mutation-isolation.py": benchmarks / "_bench_common" / "mutation_isolation.py",
        "patch-index-locks.json": PATCH_INDEX_LOCKS_PATH,
    }
    for module in sorted(PACKAGE_DIR.rglob("*.py"), key=lambda p: p.relative_to(PACKAGE_DIR).as_posix()):
        if "__pycache__" in module.parts:
            continue
        snapshot[f"claude-runner/{module.relative_to(PACKAGE_DIR).as_posix()}"] = module
    return snapshot


def _format_claude_stage_row(row: Mapping[str, Any], completed: int, total: int) -> str:
    """Render one compact canonical Claude stage row."""
    # ``success`` records transport/compliance completion. The leading glyph is
    # intentionally stricter: it is the comparable-result admission signal.
    # Retain the fallback for immutable telemetry created before paid stages
    # recorded ``pooling_eligible``.
    mark = "✓" if row.get("pooling_eligible", row["success"]) else "✗"
    quality_text = format_quality(row.get("quality_score"))
    usage_complete = row.get("usage_complete", True)
    input_text = fmt_tok(int(row["input_tokens"]))
    if not usage_complete:
        input_text = f">{input_text}" if row["input_tokens"] else "?"
    output_text = fmt_tok(int(row["output_tokens"])) if usage_complete else "?"
    base = (
        f"({completed}/{total}) {mark}  {row['task_id']!s:<6} {row['arm']!s:<8} "
        f"in={input_text:>6} out={output_text:>5} "
        f"cmd={int(row['command_calls']):>2} time={fmt_time(float(row['elapsed_s'])):>5} quality={quality_text}"
    )
    if row["study"] == "readcrop":
        return f"{base} correct={'✓' if row['primary_correct'] else '✗'} codemap={'✓' if row['codemap_used'] else '✗'}"
    execution = row["execution"]
    return (
        f"{base} patch={'✓' if execution['patch_applied'] else '✗'} "
        f"oracle={'✓' if execution['targeted_test_passed'] else '✗'} codemap={'✓' if row['codemap_used'] else '✗'}"
    )


def run_claude_paid_stage(
    *,
    study: str,
    tasks: Sequence[Mapping[str, Any]],
    repo_path: Path,
    index_path: Path,
    manifest_path: Path,
    tasks_path: Path,
    model: str,
    run_dir: Path,
    scope: Mapping[str, Any],
) -> Path:
    """Execute one Claude P1 stage through the shared paid lifecycle.

    ReadCrop uses a stripped disposable source copy. Executable stages use a benchmark-owned Git worktree for model
    edits and a second clean worktree for ordinary patch application plus the independent oracle. All stages persist
    native raw events and null tool-result tokens when Claude does not expose that usage partition.
    """
    runner = ModelRunner(model, MODELS[model], repo_path, timeout=MODEL_TIMEOUT[model])

    def run_cell(item: Mapping[str, Any], arm: str) -> Mapping[str, Any]:
        if study == "readcrop":
            with _claude_readcrop_workspace(repo_path, index_path, arm) as (cwd, relocation):
                events, elapsed_s, transport_error = runner.run_stage_events(
                    prompt=readcrop_prompt(arm, item["task"]),
                    system_prompt="Extract only the requested Python source contract with minimal repository reads.",
                    arm=arm,
                    cwd=cwd,
                    evidence_roots=(run_dir,),
                )
            row = parse_claude_readcrop_events(events, arm=arm, contract=item["contract"], workspace_root=cwd)
            row.update(
                study=study,
                model=model,
                elapsed_s=elapsed_s,
                cost_usd=parse_result_usage(
                    next((dict(e) for e in reversed(events) if e.get("type") == "result"), {})
                ).cost_usd,
                transport_error=transport_error,
                source_unchanged=_source_pair_unchanged(repo_path, index_path, scope),
                index_relocation=dict(relocation) if relocation is not None else None,
            )
            row["success"] = bool(row["success"] and transport_error is None and row["source_unchanged"])
            row["pooling_eligible"] = row["success"]
            return row

        contract = item["contract"]
        patch_workspace = None
        patch_test_runtime = scope.get("patch_test_runtime") if study == "patch" else None
        source_index = _patch_index_path(repo_path, contract.task_id) if study == "patch" else index_path
        if study == "patch":
            if not isinstance(contract, EditTaskContract):
                raise RuntimeError("patch stage requires EditTaskContract values")
            patch_workspace = create_patch_task_agent_workspace(
                repo_path,
                source_index,
                contract,
                runtime_identity=patch_test_runtime,
            )
            workspace = patch_workspace.workspace
        else:
            workspace = create_executable_agent_workspace(repo_path, source_index, contract.baseline_commit)
        if arm == "A_plain":
            workspace.index_path.unlink(missing_ok=True)
        events: list[dict[str, Any]] = []
        elapsed_s = 0.0
        transport_error: str | None = None
        diff = ""
        execution = None
        agent_fixture_intact = True
        agent_source_unchanged = True
        index_unchanged = arm == "A_plain"
        workspace_cleanup_verified = False
        try:
            events, elapsed_s, transport_error = runner.run_stage_events(
                prompt=_claude_fix_prompt(study, arm, item),
                system_prompt="Implement the requested source fix in the disposable checkout and avoid unrelated edits.",
                arm=arm,
                cwd=workspace.worktree,
                writable=True,
                evidence_roots=(run_dir,),
            )
            diff = workspace.capture_diff()
            index_unchanged = index_unchanged or workspace.index_unchanged()
            if study == "patch":
                assert patch_workspace is not None
                answer = patch_workspace.capture_answer()
                diff = answer.diff
                agent_source_unchanged = patch_workspace.source_unchanged()
                execution = execute_patch_task_answer(
                    repo_path,
                    contract,
                    answer,
                    index_path=source_index,
                    runtime_identity=patch_test_runtime,
                )
                agent_fixture_intact = patch_workspace.fixture_intact()
                execution = replace(execution, fixture_intact=execution.fixture_intact and agent_fixture_intact)
            elif study == "fix-single":
                execution = execute_fix_single_patch(repo_path, contract, diff)
            else:
                execution = execute_fix_multi_patch(repo_path, contract, diff)
        finally:
            workspace_cleanup_verified = workspace.cleanup()
        assert execution is not None
        row = _parse_claude_fix_cell(
            study=study,
            item=item,
            arm=arm,
            events=events,
            elapsed_s=elapsed_s,
            transport_error=transport_error,
            execution=execution.as_dict(),
            workspace_cleanup_verified=workspace_cleanup_verified,
            index_unchanged=index_unchanged,
            source_unchanged=(
                agent_source_unchanged
                and _source_pair_unchanged(repo_path, source_index, scope, task_id=contract.task_id)
                if study == "patch"
                else _source_pair_unchanged(repo_path, source_index, scope)
            ),
            model=model,
            workspace_root=workspace.worktree,
            captured_diff=diff,
        )
        # Preserve the actual candidate patch that the independent clean
        # workspace scored. This is additive telemetry: existing JSON readers
        # retain their schema while rescoring can verify identical input bytes.
        row.update(
            captured_diff=diff,
            captured_diff_sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        )
        return row

    def validate_row(item: Mapping[str, Any], arm: str, row: Mapping[str, Any]) -> None:
        if row.get("task_id") != item["contract"].task_id or row.get("arm") != arm:
            raise ValueError("Claude paid stage returned a row for the wrong immutable cell")
        if dict(row.get("provider_binding", {})) != dict(_provider_binding(item)):
            raise ValueError("Claude paid stage changed provider-neutral contract fields")
        if row.get("tool_result_tokens") is not None:
            raise ValueError("Claude paid stage must retain unavailable tool-result tokens as null")
        if study != "readcrop":
            captured_diff = row.get("captured_diff")
            if not isinstance(captured_diff, str):
                raise ValueError("Claude executable stage must persist its captured candidate diff")
            if row.get("captured_diff_sha256") != hashlib.sha256(captured_diff.encode("utf-8")).hexdigest():
                raise ValueError("Claude executable stage captured-diff SHA-256 does not match its bytes")

    def prepare_run(path: Path) -> None:
        inputs = path / "inputs"
        inputs.mkdir()
        (inputs / manifest_path.name).write_bytes(manifest_path.read_bytes())
        (inputs / tasks_path.name).write_bytes(tasks_path.read_bytes())
        _write_json_atomic(inputs / "scope.json", scope)
        if study == "patch":
            patch_indexes = inputs / "patch-indexes"
            patch_indexes.mkdir()
            for item in tasks:
                task_id = str(item["contract"].task_id)
                (patch_indexes / f"{task_id}.json").write_bytes(_patch_index_path(repo_path, task_id).read_bytes())
            shared = inputs / "shared"
            shared.mkdir()
            for name, source in _patch_snapshot_files().items():
                (shared / name).write_bytes(source.read_bytes())
            _write_json_atomic(inputs / "patch-runtime.json", scope["patch_test_runtime"])
        prompts = {
            item["contract"].task_id: {
                arm: (
                    readcrop_prompt(arm, item["task"]) if study == "readcrop" else _claude_fix_prompt(study, arm, item)
                )
                for arm in READCROP_ARMS
            }
            for item in tasks
        }
        _write_json_atomic(inputs / "prompts.json", prompts)

    def emit_lifecycle(kind: str, payload: Mapping[str, Any]) -> None:
        if kind == "artifacts":
            print(format_artifact_block(telemetry=payload["telemetry_path"], metadata=payload["metadata_path"]))
        else:
            print(
                f"SUMMARY  status={payload['status']} persisted_cells={payload['persisted_cells']}/{payload['total_cells']}"
            )

    def emit_row(row: Mapping[str, Any], completed: int, total: int, arm: str) -> None:
        text = _format_claude_stage_row(row, completed, total)
        presentation.print_arm_row(text, arm, console=_console)

    metadata = {
        "provider": "claude",
        "study": study,
        "model": model,
        "model_id": MODELS[model],
        "scope_sha256": scope["scope_sha256"],
        "scope": dict(scope),
        "planned_cells": len(tasks) * len(READCROP_ARMS),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return run_paid_stage(
        tasks=tasks,
        arms=READCROP_ARMS,
        run_dir=run_dir,
        metadata=metadata,
        callbacks=PaidStageCallbacks(
            run_cell=run_cell,
            validate_row=validate_row,
            prepare_run=prepare_run,
            persist_metadata=_write_json_atomic,
            emit_lifecycle=emit_lifecycle,
            emit_row=emit_row,
            write_checksums=write_checksums,
            close_adapter=lambda: None,
        ),
    )
