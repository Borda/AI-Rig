"""Run isolated change-impact predictions through existing provider transports.

Scopes bind model, fixture, semantic index and executing code independently of the graph-query suite. Every paid cell
gets a fresh source/index copy outside the denied evidence tree; the hidden behavioral oracle never enters that copy.
Existing provider adapters own native execution and isolation. The common paid lifecycle owns immutable artifacts, while
common agentic reporting retains graded quality, exact correctness and both-passing paired efficiency. Offline answers
remain explicitly diagnostic because they have no trusted transport, isolation or usage provenance.
"""

from __future__ import annotations

import hashlib
import json
import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from . import change_impact_contracts as contracts
from .paid_lifecycle import (
    PaidStageCallbacks,
    paid_approval_matches,
    paid_approval_token,
    run_paid_stage,
    write_checksums,
)
from .agentic_reporting import cell_passes, cell_quality, summarize_agentic, summary_lines
from .edit_patch_contracts import semantic_index_sha256
from .presentation import benchmark_console, print_arm_row


_BENCHMARKS = Path(__file__).resolve().parents[1]
_TASKS = _BENCHMARKS / "suites/tasks-change-impact.json"
_FIXTURE = _BENCHMARKS / "fixtures/change-impact/v1/repo"
_ARMS = ("A_plain", "B_auto", "C_strict")
_PLUGIN = _BENCHMARKS.parent / "plugins/codemap-py"


def resolve_scope(provider: str, *, model: str | None = None, timeout: int = 600) -> dict[str, Any]:
    """Bind immutable fixture/index/runtime identity and one model without reading credentials."""
    if provider not in {"claude", "codex"}:
        raise ValueError("change-impact provider must be claude or codex")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("change-impact timeout must be a positive integer")
    model = model or ("gpt-5.6-terra" if provider == "codex" else "sonnet")
    if not isinstance(model, str) or not model.strip() or any(char.isspace() for char in model):
        raise ValueError("change-impact requires one explicit model identifier")
    tasks = contracts.load_change_impact_tasks(_TASKS)
    provider_cli = shutil.which(provider)
    cli_version = None
    if provider_cli is not None:
        version = subprocess.run([provider_cli, "--version"], capture_output=True, text=True, timeout=30, check=False)
        if version.returncode == 0:
            cli_version = version.stdout.strip()
    with _fixture_workspace() as (source_root, index_path):
        index = json.loads(index_path.read_bytes())
        index_sha256 = semantic_index_sha256(index, source_root)
    scope: dict[str, Any] = {
        "study": "change-impact",
        "contract_version": "change-impact-v1",
        "provider": provider,
        "model": model,
        "timeout_seconds": timeout,
        "provider_cli_version": cli_version,
        "task_ids": [task["id"] for task in tasks],
        "arms": list(_ARMS),
        "repetitions": 1,
        "planned_cells": len(tasks) * len(_ARMS),
        "fixture_sha256": contracts.source_fingerprint(_FIXTURE),
        "suite_sha256": hashlib.sha256(_TASKS.read_bytes()).hexdigest(),
        "scorer_sha256": hashlib.sha256(Path(contracts.__file__).read_bytes()).hexdigest(),
        "stage_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "implementation_sha256": _implementation_hashes(),
        "semantic_index_sha256": index_sha256,
        "scan_version": index["scan_version"],
        "paid_admission": "exact_scope_approval_and_native_runtime_preflight_required",
        "measurement_scope": "controlled_api_change_impact_prediction",
        "held_out_limit": "New relative to graph-query calibration; model-training exclusion and difficulty unverified.",
    }
    scope["scope_sha256"] = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return scope


def _implementation_hashes() -> dict[str, str]:
    """Fingerprint shipped runtime and evaluator inputs, excluding tests and generated artifacts."""
    paths = list(_BENCHMARKS.glob("run-*.py"))
    for directory in (_BENCHMARKS / "_bench_common", _BENCHMARKS / "_bench_codex"):
        paths.extend(directory.rglob("*.py"))
    for plugin in (_PLUGIN, _BENCHMARKS.parent / "plugins/codex-rig"):
        paths.extend(
            path
            for path in plugin.rglob("*")
            if path.is_file()
            and not any(
                part
                in {
                    "tests",
                    "__pycache__",
                    ".pytest_cache",
                    ".cache",
                    ".reports",
                    ".plans",
                    ".git",
                    ".venv",
                    ".ruff_cache",
                }
                for part in path.relative_to(plugin).parts
            )
            and (path.suffix in {".py", ".md", ".json", ".toml", ".sh"} or "bin" in path.relative_to(plugin).parts)
        )
    return {
        path.relative_to(_BENCHMARKS.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(paths))
    }


@contextlib.contextmanager
def _fixture_workspace() -> Iterator[tuple[Path, Path]]:
    """Build a fresh graph from source-only fixtures in a benchmark-owned disposable directory."""
    with tempfile.TemporaryDirectory(prefix="codemap-impact-") as directory:
        source_root = Path(directory).resolve() / "repo"
        source_root.mkdir()
        for source in sorted(_FIXTURE.rglob("*.py")):
            if "__pycache__" in source.parts or source.is_symlink():
                raise ValueError("fixture source cannot contain symlinked Python files or cache artifacts")
            destination = source_root / source.relative_to(_FIXTURE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        # Index construction must not inherit a user's index override, module path or telemetry destination.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CODEMAP_", "PYTHON", "GIT_")) and key != "VIRTUAL_ENV"
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, str(_PLUGIN / "bin/scan-index"), "--root", str(source_root)],
            cwd=source_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"impact fixture index build failed: {result.stderr[-1000:]}")
        index_path = source_root / ".cache/codemap/repo.json"
        if not index_path.is_file():
            raise ValueError("impact scanner did not write the declared index coordinate")
        yield source_root, index_path


def _fixture_coordinate(source_root: Path, index_path: Path, scope: Mapping[str, Any]) -> dict[str, Any]:
    """Validate copied source and semantic graph before binding root-dependent runtime bytes."""
    contracts.verify_source_fingerprint(source_root, scope["fixture_sha256"])
    payload = json.loads(index_path.read_bytes())
    if (
        payload.get("scan_version") != scope["scan_version"]
        or payload.get("scan_root") != str(source_root)
        or semantic_index_sha256(payload, source_root) != scope["semantic_index_sha256"]
    ):
        raise ValueError("impact fixture index differs from approved scope")
    return {
        "source_fingerprint": scope["fixture_sha256"],
        "raw_index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
        "scan_version": scope["scan_version"],
        "index_scan_root": payload["scan_root"],
    }


def run_stage(
    *,
    provider: str,
    dry_run: bool = False,
    resolve_scope_requested: bool = False,
    answers_file: Path | None = None,
    output_dir: Path | None = None,
    model: str | None = None,
    paid_approval: str | None = None,
    timeout: int = 600,
    runtime_factory: Callable[..., Any] | None = None,
    scope_sha256: str | None = None,
) -> None:
    """Preflight, diagnose, or execute an explicitly approved isolated impact study.

    Preflight and diagnostic actions never call models. Paid execution requires a current scope approval and a provider-
    owned native runtime. Diagnostic JSONL rows contain exactly task_id, arm, and response; missing assigned cells
    receive zero credit. Existing inputs and results are never rewritten. Behavior-oracle execution uses only trusted
    benchmark-owned fixture source, and diagnostic text never substitutes for native runtime evidence.
    """
    scope = resolve_scope(provider, model=model, timeout=timeout)
    if scope_sha256 is not None and scope_sha256 != scope["scope_sha256"]:
        raise ValueError("change-impact scope SHA does not match current inputs")
    tasks = contracts.load_change_impact_tasks(_TASKS)
    oracles = {task["id"]: contracts.build_change_impact_oracle(task, _FIXTURE) for task in tasks}
    for task in tasks:
        contracts.verify_change_impact_oracle(task, _FIXTURE, oracles[task["id"]])
    contracts.verify_source_fingerprint(_FIXTURE, scope["fixture_sha256"])
    if resolve_scope_requested:
        if dry_run or answers_file is not None or output_dir is not None or paid_approval is not None:
            raise ValueError("scope resolution cannot be combined with another change-impact action")
        print(json.dumps(scope, sort_keys=True))
        return
    if dry_run:
        if answers_file is not None or paid_approval is not None:
            raise ValueError("dry run cannot be combined with diagnostic scoring or paid approval")
        print(f"change-impact fixture preflight: {scope['planned_cells']} cells; model={scope['model']}")
        if runtime_factory is None:
            print("paid_admission=blocked: provider runtime preflight is unavailable")
        else:
            with tempfile.TemporaryDirectory(prefix="impact-preflight-evidence-") as evidence:
                with _fixture_workspace() as (source_root, index_path):
                    coordinate = _fixture_coordinate(source_root, index_path, scope)
                    with runtime_factory(
                        model=scope["model"],
                        source_root=source_root,
                        index_path=index_path,
                        run_dir=Path(evidence).resolve(),
                        timeout=timeout,
                        dry_run=True,
                        fixture_runtime_coordinate=coordinate,
                    ):
                        pass
            print("Provider runtime preflight passed; hidden baseline/changed behavior checks passed.")
        for task in tasks:
            for arm in _ARMS:
                print_arm_row(f"PLAN    {task['id']} rep=1 {arm}", arm, console=benchmark_console())
        if runtime_factory is not None:
            destination = output_dir or (_BENCHMARKS / "results" / f"{provider}-impact-{scope['scope_sha256'][:16]}")
            command = [
                sys.executable,
                str(_BENCHMARKS / f"run-{provider}-agentic.py"),
                "--study=change-impact",
                f"--model={scope['model']}",
                f"--timeout={timeout}",
                f"--run-dir={destination}",
                f"--paid-approval={paid_approval_token(scope['scope_sha256'])}",
            ]
            auth = (
                ' --auth-source "${CODEX_AUTH_SOURCE:?set CODEX_AUTH_SOURCE to your Codex auth file}"'
                if provider == "codex"
                else ""
            )
            print("PAID_COMMAND:\n" + shlex.join(command) + auth)
        return
    if answers_file is not None:
        if paid_approval is not None:
            raise ValueError("diagnostic answers cannot be combined with paid approval")
        if output_dir is None:
            raise ValueError("diagnostic answers require a new output directory")
        _score_diagnostic(Path(answers_file), Path(output_dir), tasks, oracles, scope)
        return
    if not paid_approval_matches(paid_approval, scope["scope_sha256"]):
        raise ValueError("change-impact paid execution requires current scope approval; use --dry-run")
    if runtime_factory is None or output_dir is None:
        raise ValueError("paid change-impact requires provider runtime and a new output directory")
    destination = Path(output_dir).resolve()
    if destination.is_relative_to(_FIXTURE.resolve()):
        raise ValueError("impact output must stay outside the model-visible fixture")
    _run_paid(destination, tasks, oracles, scope, runtime_factory)


def _inventory(root: Path) -> dict[str, str]:
    """Detect mutations and added evidence files without reading symlink targets."""
    return {
        path.relative_to(root).as_posix(): (
            "symlink:" + os.readlink(path) if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() or path.is_symlink()
    }


def _verify_scope_inputs(scope: Mapping[str, Any]) -> None:
    """Reject live evaluator/source changes instead of silently mixing benchmark coordinates."""
    if (
        _implementation_hashes() != scope["implementation_sha256"]
        or hashlib.sha256(_TASKS.read_bytes()).hexdigest() != scope["suite_sha256"]
    ):
        raise ValueError("impact runtime or task suite changed after scope approval")
    contracts.verify_source_fingerprint(_FIXTURE, scope["fixture_sha256"])


def _run_paid(
    output_dir: Path,
    tasks: list[dict[str, Any]],
    oracles: dict[str, Any],
    scope: dict[str, Any],
    runtime_factory: Callable[..., Any],
) -> None:
    """Use the existing paid lifecycle with per-cell source isolation and shared quality reporting."""
    rows: list[dict[str, Any]] = []

    def _persist(path: Path, payload: Mapping[str, Any]) -> None:
        """Write deterministic metadata and summaries without platform newline translation."""
        path.write_bytes((json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())

    def _prepare(run_dir: Path) -> None:
        """Archive evaluator-only input bytes; model sources live in separate disposable roots."""
        _verify_scope_inputs(scope)
        inputs = run_dir / "inputs"
        inputs.mkdir()
        shutil.copytree(_FIXTURE, inputs / "fixture", ignore=shutil.ignore_patterns("__pycache__"))
        (inputs / "tasks.json").write_bytes(_TASKS.read_bytes())
        for relative in scope["implementation_sha256"]:
            target = inputs / "implementation" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((_BENCHMARKS.parent / relative).read_bytes())
        _persist(inputs / "scope.json", scope)

    def _run_cell(task: dict[str, Any], arm: str) -> Mapping[str, Any]:
        """Collect native facts only, then score against the hidden immutable oracle."""
        started = time.monotonic()
        _verify_scope_inputs(scope)
        with _fixture_workspace() as (source_root, index_path):
            coordinate = _fixture_coordinate(source_root, index_path, scope)
            baseline = _inventory(source_root)
            evidence = output_dir / "cells" / task["id"] / arm
            evidence.mkdir(parents=True)
            (evidence / "index.json").write_bytes(index_path.read_bytes())
            _persist(evidence / "fixture-coordinate.json", coordinate)
            with runtime_factory(
                model=scope["model"],
                source_root=source_root,
                index_path=index_path,
                run_dir=output_dir,
                timeout=scope["timeout_seconds"],
                dry_run=False,
                fixture_runtime_coordinate=coordinate,
            ) as adapter:
                native = dict(adapter.run_cell(task, arm, contracts.materialize_change_impact_prompt(task)))
                # Retain raw reported spend even if later validation refuses this row as trusted telemetry.
                _persist(evidence / "native.json", native)
                if native.get("task_id", task["id"]) != task["id"] or native.get("arm", arm) != arm:
                    raise ValueError("impact native result belongs to another coordinate")
            if _inventory(source_root) != baseline:
                native.update(contaminated=True, error_type="fixture_mutation")
            _verify_scope_inputs(scope)
        assessment = contracts.assess_change_impact_response(task, native.get("report_text", ""))
        score = contracts.score_change_impact_answer(oracles[task["id"]], assessment.answer)
        row = {
            **native,
            "task_id": task["id"],
            "arm": arm,
            "repetition": 1,
            "observed": True,
            "provider": scope["provider"],
            "model": scope["model"],
            "cell_wall_time_s": time.monotonic() - started,
            "diagnostic_only": False,
            "answer_contract_valid": assessment.valid,
            "answer_pooling_eligible": assessment.pooling_eligible,
            "answer_error": assessment.error,
            "quality": {
                "correct": score.correct,
                "quality_score": score.quality_score,
                "graded_score": score.graded_score,
                "components": dict(score.components),
                "graded_components": dict(score.graded_components),
            },
            "failure_details": contracts.change_impact_failure_details(oracles[task["id"]], assessment.answer)
            if assessment.answer is not None
            else [],
        }
        row.update(exact_pass=cell_passes(row), admitted_quality=cell_quality(row))
        return row

    def _validate(task: dict[str, Any], arm: str, row: Mapping[str, Any]) -> None:
        """Require explicit native gate evidence and the assigned coordinate before persistence."""
        if row.get("task_id") != task["id"] or row.get("arm") != arm:
            raise ValueError("impact native row has a foreign coordinate")
        for field in ("success", "incomplete", "contaminated", "usage_complete", "treatment_adherence"):
            if type(row.get(field)) is not bool:
                raise ValueError(f"impact native row lacks explicit {field}")
        rows.append(dict(row))

    def _emit(line: str, arm: str | None = None) -> None:
        """Persist ANSI-free lines and use the shared terminal renderer for arm output."""
        with (output_dir / "run.log").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        if arm is None:
            print(line)
        else:
            print_arm_row(line, arm, console=benchmark_console())

    def _lifecycle(event: str, payload: Mapping[str, Any]) -> None:
        """Publish progress and all-assigned summaries on success, interruption or failure."""
        _emit(f"{event} " + json.dumps(dict(payload), sort_keys=True))
        if event == "summary":
            summary = summarize_agentic(rows, task_ids=scope["task_ids"], repetitions=1)
            _persist(output_dir / "summary.json", {**summary, "scope": scope})
            for line, arm in summary_lines(summary):
                _emit(line, arm)

    def _emit_row(row: Mapping[str, Any], completed: int, total: int, arm: str) -> None:
        """Render graded quality without repurposing importer metrics for impact prediction."""
        outcome = "!" if not row["success"] or row["incomplete"] else ("✓" if row["exact_pass"] else "✗")
        _emit(
            f"({completed}/{total}) {outcome} {row['task_id']} rep=1 {arm} "
            f"quality={row['admitted_quality']:.1%} component={row['quality']['quality_score']:.3f}",
            arm,
        )

    run_paid_stage(
        tasks=tasks,
        arms=_ARMS,
        run_dir=output_dir,
        metadata={"scope": scope, "study": "change-impact"},
        callbacks=PaidStageCallbacks(
            run_cell=_run_cell,
            validate_row=_validate,
            prepare_run=_prepare,
            persist_metadata=_persist,
            emit_lifecycle=_lifecycle,
            emit_row=_emit_row,
            write_checksums=write_checksums,
            close_adapter=lambda: None,
        ),
    )


def _score_diagnostic(
    answers_file: Path,
    output_dir: Path,
    tasks: list[dict[str, Any]],
    oracles: dict[str, Any],
    scope: dict[str, Any],
) -> None:
    """Persist all assigned diagnostic coordinates after validating the complete input."""
    if output_dir.resolve().is_relative_to(_FIXTURE.resolve()):
        raise ValueError("diagnostic output must stay outside the model-visible fixture")
    source = answers_file.read_bytes()
    submitted: dict[tuple[str, str], str] = {}
    for line in source.decode("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != {"task_id", "arm", "response"}:
            raise ValueError("diagnostic coordinate requires exactly task_id, arm, response")
        if not all(isinstance(value, str) for value in row.values()):
            raise ValueError("diagnostic coordinate values must be strings")
        key = (row["task_id"], row["arm"])
        if key[0] not in oracles or key[1] not in _ARMS or key in submitted:
            raise ValueError("unknown or duplicate diagnostic coordinate")
        submitted[key] = row["response"]
    rows = []
    for task in tasks:
        for arm in _ARMS:
            key = (task["id"], arm)
            assessment = contracts.assess_change_impact_response(task, submitted.get(key, ""))
            score = contracts.score_change_impact_answer(oracles[task["id"]], assessment.answer)
            rows.append(
                {
                    "task_id": task["id"],
                    "arm": arm,
                    "diagnostic_only": True,
                    "observed": key in submitted,
                    "quality": score.graded_score,
                    "exact_answer": score.correct,
                    "components": dict(score.components),
                    "answer_error": assessment.error,
                    "failure_details": (
                        contracts.change_impact_failure_details(oracles[task["id"]], assessment.answer)
                        if assessment.answer is not None
                        else [{"category": "formatting_failure" if key in submitted else "unobserved"}]
                    ),
                }
            )
    summary = {
        "diagnostic_only": True,
        "scope": scope,
        "assigned_cells": len(rows),
        "observed_cells": len(submitted),
        "unobserved_cells": len(rows) - len(submitted),
        "quality": sum(row["quality"] for row in rows) / len(rows),
        "source_answers_sha256": hashlib.sha256(source).hexdigest(),
    }
    # Validate serialization before creating an immutable output directory.
    telemetry_bytes = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode()
    summary_bytes = (json.dumps(summary, sort_keys=True, indent=2) + "\n").encode()
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "answers.jsonl").write_bytes(source)
    (output_dir / "telemetry.jsonl").write_bytes(telemetry_bytes)
    (output_dir / "summary.json").write_bytes(summary_bytes)
    with (output_dir / "run.log").open("x", encoding="utf-8", newline="\n") as log:
        for row in rows:
            line = f"DIAGNOSTIC {row['task_id']} {row['arm']} quality={row['quality']:.1%} observed={row['observed']}"
            log.write(line + "\n")
            print_arm_row(line, row["arm"], console=benchmark_console())
    write_checksums(output_dir)
