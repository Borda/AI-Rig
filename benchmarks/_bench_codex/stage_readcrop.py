"""Stage implementation for the source-anchored Codex ReadCrop benchmark.

The structural runner supplies only disposable A/B/C homes and native Codex event parsing. This stage owns its read-crop
prompt, strict answer envelope, source oracle, and separately reported tool-payload cost.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping


BENCHMARKS = Path(__file__).resolve().parents[1]
ROOT = BENCHMARKS.parent
TASKS_PATH = BENCHMARKS / "suites" / "tasks-readcrop.json"
METHODOLOGY_PATH = BENCHMARKS / "manifests" / "provider-parity-methodology.json"
STRUCTURAL_PATH = BENCHMARKS / "run-codex-structural.py"
ARMS = ("A_plain", "B_auto", "C_strict")
_NATIVE_ARMS = {"A_plain": "A_plain", "B_auto": "B_auto", "C_strict": "C_strict"}
_ANSWER_RE = re.compile(r"BEGIN_READ_CROP_JSON\s*(?P<payload>\{.*?\})\s*END_READ_CROP_JSON", re.DOTALL)

sys.path.insert(0, str(BENCHMARKS))
from _bench_common.provider_parity_contracts import (  # noqa: E402
    canonical_task_hash,
    fresh_input_tokens,
    load_task_suite,
    prompt_hash,
    semantic_suite_hash,
    token_accounting_inconsistent,
)
from _bench_common.readcrop_contracts import (  # noqa: E402
    ReadcropUsage,
    build_readcrop_contract,
    parse_readcrop_answer,
    score_readcrop_answer,
    validate_provider_binding,
)
from _bench_common.paid_lifecycle import (  # noqa: E402
    PaidStageCallbacks,
    paid_approval_matches,
    run_paid_stage,
    verify_checksums,
    write_checksums,
)
from . import runtime  # noqa: E402
from _bench_common.presentation import (  # noqa: E402
    format_artifact_block,
    format_probe_row,
    fmt_time,
    fmt_tok,
)


_STRUCTURAL_MODULE: Any = None


def _structural() -> Any:
    """Return the structural isolation adapter, loading it on first use.

    This used to run at module import (``_structural = _load_structural()``), so
    merely importing this stage executed the whole 5000-line structural runner —
    including its import-time side effects — even for callers that only wanted a
    prompt string or a scope hash. Loading is deferred here instead; the module
    is still resolved by source path rather than name, because test suites
    execute the runner under generated module names.
    """
    global _STRUCTURAL_MODULE
    if _STRUCTURAL_MODULE is not None:
        return _STRUCTURAL_MODULE
    for module in tuple(sys.modules.values()):
        module_file = getattr(module, "__file__", None)
        if module_file is not None and Path(module_file).resolve() == STRUCTURAL_PATH:
            _STRUCTURAL_MODULE = module
            return module
    spec = importlib.util.spec_from_file_location("_codex_readcrop_structural", STRUCTURAL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Codex structural isolation adapter")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _STRUCTURAL_MODULE = module
    return module


def load_readcrop_tasks(tasks_path: Path, manifest_path: Path, repo_path: Path) -> list[dict[str, Any]]:
    """Load locked tasks and attach each independent frozen-source contract."""
    raw_tasks = load_task_suite(tasks_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    suites = manifest.get("suites") if isinstance(manifest, Mapping) else None
    if not isinstance(suites, list):
        raise ValueError("methodology manifest requires suites")
    suite = next((item for item in suites if item.get("path") == "benchmarks/suites/tasks-readcrop.json"), None)
    if not isinstance(suite, Mapping):
        raise ValueError("methodology manifest lacks the read-crop suite")
    if suite.get("ordered_task_ids") != [task["id"] for task in raw_tasks]:
        raise ValueError("read-crop task order drifted")
    if suite.get("semantic_suite_sha256") != semantic_suite_hash(raw_tasks):
        raise ValueError("read-crop suite identity drifted")
    rows = {row.get("id"): row for row in suite.get("tasks", []) if isinstance(row, Mapping)}
    loaded: list[dict[str, Any]] = []
    for task in raw_tasks:
        row = rows.get(task["id"])
        if not isinstance(row, Mapping) or row.get("canonical_task_sha256") != canonical_task_hash(task):
            raise ValueError(f"read-crop task identity drifted for {task['id']}")
        if row.get("prompt_sha256") != prompt_hash(task):
            raise ValueError(f"read-crop prompt identity drifted for {task['id']}")
        source = extract_symbol_source(repo_path, str(task["primary_module"]), str(task["symbol"]))
        contract = build_readcrop_contract(task, source=source)
        loaded.append({"task": task, "contract": contract, "source": source})
    return loaded


def extract_symbol_source(repo_path: Path, module: str, symbol: str) -> str:
    """Return exact AST source for a module-qualified function or method."""
    path = _module_path(repo_path, module)
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    node: ast.AST = tree
    for part in symbol.split("."):
        node = next(
            (
                child
                for child in getattr(node, "body", [])
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == part
            ),
            None,
        )
        if node is None:
            raise ValueError(f"symbol {symbol!r} is unavailable in {path}")
    source = ast.get_source_segment(text, node)
    if not isinstance(source, str) or not source:
        raise ValueError(f"source is unavailable for {symbol!r}")
    return source


def _module_path(repo_path: Path, module: str) -> Path:
    """Resolve one importable module using the frozen repository's common layouts."""
    relative = Path(*module.split("."))
    candidates = (repo_path / "src" / relative.with_suffix(".py"), repo_path / relative.with_suffix(".py"))
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise ValueError(f"module {module!r} is unavailable under {repo_path}")
    return path


def readcrop_prompt(arm: str, task: Mapping[str, Any]) -> str:
    """Build one shared task prompt plus the arm-only availability supplement."""
    if arm not in ARMS:
        raise ValueError(f"unsupported read-crop arm {arm!r}")
    codemap_boundary = (
        " Codemap is a frozen static source-graph query tool: use it only for symbol, dependency, importer, or caller "
        "facts. Do not ask it to validate runtime behavior, execute tests, apply edits, or resolve facts once a compact "
        "query reports index.query_complete=true; use direct source reads for the remaining contract details."
    )
    supplements = {
        "A_plain": "Codemap is absent and inaccessible. Use ordinary repository tools.",
        "B_auto": "Codemap's direct CLI is available as $CODEMAP_BIN; use it when useful." + codemap_boundary,
    }
    supplement = supplements.get(arm)
    if arm == "C_strict":
        symbol = task.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("C_strict read-crop task requires a symbol")
        supplement = (
            _structural().arm_envelope("C_strict")
            + f' The required canonical query is exactly `"$CODEMAP_BIN" query --compact symbol {symbol}`.'
            + codemap_boundary
        )
    if supplement is None:
        # An `assert` here vanished under `python -O`, and the arm would then be
        # rendered into the prompt as the literal string "None". A new arm added
        # to ARMS without a matching supplement must fail loudly instead.
        raise ValueError(f"read-crop arm {arm!r} has no availability supplement")
    parameter_requirement = (
        "each exact required source parameter"
        if task.get("required_parameters") is not None
        else "every exact source parameter"
    )
    envelope = (
        "After completing any tools, return no prose or Markdown outside this exact envelope:\n"
        "BEGIN_READ_CROP_JSON\n"
        '{"signature":"full qualified signature","parameters":["exact parameter name"],"behavior":"non-empty contract summary"}\n'
        "END_READ_CROP_JSON\n"
        f"The JSON object must have exactly those three fields. `parameters` must list {parameter_requirement} name, "
        "and `behavior` must be a non-empty summary of the requested controls."
    )
    return f"{supplement}\n\n{task['prompt']}\n\n{envelope}"


def parse_readcrop_stream(stream: str | bytes, *, arm: str, contract: Any, skill_path: Path | None) -> dict[str, Any]:
    """Normalize native Codex events into a stage-local result record."""
    skill_sha256 = hashlib.sha256(skill_path.read_bytes()).hexdigest() if skill_path is not None else ""
    parsed = runtime.parse_codex_jsonl(stream, skill_path=skill_path, skill_sha256=skill_sha256)
    match = _ANSWER_RE.search(parsed.output_text)
    answer_error = ""
    score = None
    if match is None:
        answer_error = "missing strict read-crop answer envelope"
    else:
        try:
            score = score_readcrop_answer(contract, parse_readcrop_answer(match.group("payload")))
        except ValueError as exc:
            answer_error = str(exc)
    strict_query_conformance = (
        None if arm != "C_strict" else ["symbol", contract.symbol] in parsed.successful_query_arguments
    )
    # Declared field on CodexParseResult (default 0). The old getattr fallback
    # silently substituted a different metric, codemap_calls, whenever an object
    # lacked the attribute — which only ever happened for an incomplete test double.
    codemap_observed_calls = parsed.codemap_observed_calls
    contaminated = arm == "A_plain" and codemap_observed_calls > 0
    # Compliance measures only each arm's availability contract. The stricter
    # C query/Skill requirements remain independent diagnostics in telemetry.
    compliance = {
        "A_plain": not bool(codemap_observed_calls),
        "B_auto": True,
        "C_strict": bool(parsed.codemap_skill_compact_successful_calls),
    }[arm]
    return {
        "task_id": contract.task_id,
        "arm": arm,
        "success": parsed.success and not contaminated and not answer_error,
        "answer_error": answer_error,
        "primary_correct": score.primary_correct if score is not None else False,
        "quality_score": score.quality_score if score is not None else None,
        "quality_components": dict(score.quality_components) if score is not None else {},
        # POLICY — unscoreable cell: every recall field is None, none is zero.
        # Previously an unparsable answer wrote 0.0 into parameter_recall and
        # keyword_recall_diagnostic but None into the two behavior fields, so the
        # same failure was averaged INTO two means and omitted FROM the other two.
        # None is now uniform, matching quality_score/quality_components in this
        # same row: a 0.0 recall asserts a measurement that never happened, while
        # None says the answer could not be scored at all. None therefore means
        # "not scoreable OR not applicable"; `answer_error` and `quality_score`
        # disambiguate the two, and `success`/`primary_correct` (both False here)
        # carry the failure so it is never mistaken for a passing cell.
        # Downstream means must report the unscoreable count alongside the mean.
        "parameter_recall": score.parameter_recall if score is not None else None,
        "behavior_fact_recall": score.behavior_fact_recall if score is not None else None,
        "behavior_facts_correct": score.behavior_facts_correct if score is not None else None,
        "keyword_recall_diagnostic": score.keyword_recall if score is not None else None,
        "input_tokens": parsed.input_tokens,
        "cached_input_tokens": parsed.cached_input_tokens,
        "fresh_input_tokens": fresh_input_tokens(parsed.input_tokens, parsed.cached_input_tokens),
        "token_accounting_inconsistent": token_accounting_inconsistent(parsed.input_tokens, parsed.cached_input_tokens),
        "output_tokens": parsed.output_tokens,
        "reasoning_output_tokens": parsed.reasoning_output_tokens,
        # Nonzero means the provider reported usage the parser could not read, so
        # this row's cost is an undercount rather than a genuinely cheap cell.
        "malformed_usage": parsed.malformed_usage,
        "tool_result_tokens": parsed.tool_result_tokens,
        "command_calls": parsed.command_calls,
        "tool_elapsed_s": parsed.tool_elapsed_s,
        "codemap_used": codemap_observed_calls > 0,
        "codemap_calls": parsed.codemap_calls,
        "codemap_observed_calls": codemap_observed_calls,
        "compliance": compliance,
        "strict_query_conformance": strict_query_conformance,
        "successful_query_arguments": parsed.successful_query_arguments,
        "skill_delivery_observed": parsed.skill_delivery_observed,
        "codemap_skill_compact_successful_calls": parsed.codemap_skill_compact_successful_calls,
        "contaminated": contaminated,
        "output_text": parsed.output_text,
        "raw_events": parsed.raw_events,
        "raw_events_sha256": hashlib.sha256(
            json.dumps(parsed.raw_events, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "provider_binding": dict(contract.provider_binding()),
    }


def format_result_row(row: Mapping[str, Any], *, completed: int, total: int) -> str:
    """Render one flushed paid cell result with shared compact units."""
    status = "✓" if row["success"] else "✗"
    quality = row.get("quality_score")
    quality_text = f"{quality:.3f}" if isinstance(quality, (int, float)) else "?"
    return (
        f"({completed}/{total}) {status}  {row['task_id']:<6} {row['arm']:<9} "
        f"in={fmt_tok(row['input_tokens']):>6} out={fmt_tok(row['output_tokens']):>5} "
        f"cmd={row['command_calls']:>2} time={fmt_time(row['elapsed_s']):>5} quality={quality_text} "
        f"correct={'✓' if row['primary_correct'] else '✗'} "
        f"codemap={'✓' if row['codemap_used'] else '✗'}"
    )


def emit_progress(run_log: Path, line: str, *, arm: str | None = None) -> None:
    """Persist and flush one human-readable lifecycle or cell result line."""
    with run_log.open("a", encoding="utf-8") as handle:
        handle.write(f"{line}\n")
    if arm is None:
        print(line, flush=True)
        return
    runtime.print_arm_row(line, arm)


def rescore_results(
    source_run_dir: Path,
    output_dir: Path,
    tasks: list[dict[str, Any]],
) -> Path:
    """Reparse a checksummed completed calibration into a separate immutable artifact."""
    if output_dir.exists():
        raise FileExistsError(f"rescore output already exists: {output_dir}")
    verify_checksums(source_run_dir)
    source_metadata_path = source_run_dir / "run-metadata.json"
    source_telemetry_path = source_run_dir / "telemetry.jsonl"
    source_metadata = json.loads(source_metadata_path.read_text(encoding="utf-8"))
    if source_metadata.get("status") != "completed":
        raise ValueError("only completed read-crop runs can be rescored")
    if not source_telemetry_path.is_file():
        raise ValueError("read-crop result is missing telemetry.jsonl")
    contracts = {item["contract"].task_id: item["contract"] for item in tasks}
    source_rows = [json.loads(line) for line in source_telemetry_path.read_text(encoding="utf-8").splitlines() if line]
    if not source_rows:
        raise ValueError("read-crop result has no telemetry rows")
    output_dir.mkdir(parents=True)
    output_path = output_dir / "telemetry.jsonl"
    run_log = output_dir / "run.log"
    run_log.touch(exist_ok=False)
    persisted = 0
    try:
        with output_path.open("x", encoding="utf-8") as telemetry:
            for source_row in source_rows:
                task_id = source_row.get("task_id")
                arm = source_row.get("arm")
                raw_events = source_row.get("raw_events")
                if not isinstance(task_id, str) or task_id not in contracts or arm not in ARMS:
                    raise ValueError("read-crop source telemetry has an unknown task or arm")
                if not isinstance(raw_events, list) or not all(isinstance(event, Mapping) for event in raw_events):
                    raise ValueError("read-crop source telemetry lacks replayable raw events")
                contract = contracts[task_id]
                validate_provider_binding(contract, source_row.get("provider_binding", {}))
                skill_path = (
                    source_run_dir / "inputs" / "C_strict" / "codemap-py" / "codex-skills" / "query-code" / "SKILL.md"
                    if arm == "C_strict"
                    else None
                )
                if skill_path is not None and not skill_path.is_file():
                    raise ValueError("read-crop source snapshot lacks the frozen Codemap Skill")
                row = parse_readcrop_stream(
                    (json.dumps(event, sort_keys=True) for event in raw_events),
                    arm=arm,
                    contract=contract,
                    skill_path=skill_path,
                )
                ReadcropUsage(row["input_tokens"], row["tool_result_tokens"])
                for field in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "tool_result_tokens",
                    "command_calls",
                    "tool_elapsed_s",
                ):
                    if source_row.get(field) != row[field]:
                        raise ValueError(f"read-crop source telemetry changed native {field}")
                row["elapsed_s"] = source_row.get("elapsed_s")
                row["source_telemetry_row_sha256"] = hashlib.sha256(
                    json.dumps(source_row, separators=(",", ":"), sort_keys=True).encode("utf-8")
                ).hexdigest()
                del row["raw_events"]
                telemetry.write(json.dumps(row, sort_keys=True) + "\n")
                telemetry.flush()
                persisted += 1
                emit_progress(
                    run_log,
                    format_result_row(row, completed=persisted, total=len(source_rows)),
                    arm=str(arm),
                )
        metadata = {
            "status": "completed",
            "kind": "offline-readcrop-reparse-v1",
            "source_run_dir": str(source_run_dir.resolve()),
            "source_checksums_sha256": hashlib.sha256((source_run_dir / "checksums.sha256").read_bytes()).hexdigest(),
            "source_scope": source_metadata.get("scope"),
            "rescorer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "persisted_cells": persisted,
        }
        (output_dir / "run-metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        emit_progress(run_log, f"SUMMARY  status=completed  persisted_cells={persisted}/{len(source_rows)}")
    except BaseException:
        metadata = {"status": "failed", "kind": "offline-readcrop-reparse-v1", "persisted_cells": persisted}
        (output_dir / "run-metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        emit_progress(run_log, f"SUMMARY  status=failed  persisted_cells={persisted}/{len(source_rows)}")
        raise
    finally:
        write_checksums(output_dir)
    return output_dir


def dry_run(tasks: list[dict[str, Any]], *, observed_codemap: Mapping[str, bool]) -> list[str]:
    """Return deterministic no-model rows for every task and treatment.

    The stage banner is not a row: it is a section heading the caller renders through the shared
    presentation helper, so a terminal draws a rule where a redirected run keeps plain text.

    Args:
        tasks: Loaded read-crop tasks, each carrying its frozen-source contract.
        observed_codemap: Per-arm Codemap availability measured by :func:`preflight_isolation`.
            ``codemap`` reports this observation and ``use`` the arm's declared obligation, so the
            two disagree exactly when the environment fails an arm that requires Codemap. An arm the
            probe could not measure reports ``unknown`` rather than borrowing its label's
            expectation.
    """
    rows = []
    for arm in ARMS:
        measured = observed_codemap.get(arm)
        rows.append(
            format_probe_row(
                arm,
                {"codemap": "unknown" if measured is None else measured, "use": runtime.probe_use(arm)},
            )
        )
    for item in tasks:
        for arm in ARMS:
            rows.append(f"PLAN    {item['contract'].task_id:<6} rep=1  {arm}")
    return rows


def resolve_scope(
    tasks: list[dict[str, Any]], methodology_path: Path, treatment_manifest_path: Path, model: str
) -> dict[str, Any]:
    """Bind one selected ReadCrop calibration to every enforced immutable coordinate."""
    if model not in runtime.SUPPORTED_CODEX_MODELS:
        raise ValueError(f"supported Codex benchmark model required: {', '.join(runtime.SUPPORTED_CODEX_MODELS)}")
    scope = {
        "arms": ARMS,
        "manifest_sha256": hashlib.sha256(methodology_path.read_bytes()).hexdigest(),
        "model": model,
        "repetitions": 1,
        "stage_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "structural_runner_sha256": hashlib.sha256(STRUCTURAL_PATH.read_bytes()).hexdigest(),
        # This manifest locks the installed Codemap/Codex Rig treatment bytes
        # verified during preflight, so its digest must authorize paid execution.
        "treatment_manifest_sha256": hashlib.sha256(treatment_manifest_path.read_bytes()).hexdigest(),
        "source_contracts": {
            item["contract"].task_id: {
                "oracle_sha256": item["contract"].oracle_sha256,
                "source_sha256": item["contract"].source_sha256,
            }
            for item in tasks
        },
        "task_ids": [item["contract"].task_id for item in tasks],
        "total_cells": len(tasks) * len(ARMS),
    }
    scope["scope_sha256"] = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return scope


def _prepare_readcrop_scope(
    *,
    repo_path: Path,
    model: str,
    tasks_selector: str | None,
    tasks_path: Path,
    methodology_path: Path,
    structural_manifest_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load, select, and bind one ReadCrop request without model access."""
    tasks = load_readcrop_tasks(tasks_path, methodology_path, repo_path)
    if tasks_selector:
        selected = {item.strip() for item in tasks_selector.split(",") if item.strip()}
        tasks = [item for item in tasks if item["contract"].task_id in selected]
        if not tasks or len(tasks) != len(selected):
            raise ValueError("--tasks must select known read-crop IDs")
    return tasks, resolve_scope(tasks, methodology_path, structural_manifest_path, model)


def resolve_readcrop_stage_scope(
    *,
    repo_path: Path,
    model: str,
    tasks_selector: str | None,
    tasks_path: Path = TASKS_PATH,
    methodology_path: Path = METHODOLOGY_PATH,
    structural_manifest_path: Path = BENCHMARKS / "manifests" / "codex-integration.json",
) -> dict[str, Any]:
    """Resolve one ReadCrop selection without constructing a model adapter."""
    _, scope = _prepare_readcrop_scope(
        repo_path=repo_path,
        model=model,
        tasks_selector=tasks_selector,
        tasks_path=tasks_path,
        methodology_path=methodology_path,
        structural_manifest_path=structural_manifest_path,
    )
    return scope


def preflight_isolation(
    *,
    repo_path: Path,
    index_path: Path,
    marketplace_root: Path,
    codemap_bin: Path,
    model: str,
    structural_manifest_path: Path,
    index_relocation: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """Exercise the established disposable A/B/C homes without a model or auth.

    ``index_relocation`` is present only for a run outside the canonical clone, whose index is the locked graph with its
    scan root moved; each probe then proves that provenance rather than the byte hash, which reproduces at the canonical
    path alone.

    Returns:
        Each arm's observed Codemap availability, so a plan row can report what this probe actually
        measured instead of restating what the arm's own label implies.
    """
    adapter = _structural().CodexRunner(
        model,
        repo_path,
        index_path=index_path,
        marketplace_root=marketplace_root,
        codemap_bin=codemap_bin,
        manifest_path=structural_manifest_path,
        index_relocation=index_relocation,
    )
    observed: dict[str, bool] = {}
    try:
        for arm in _NATIVE_ARMS.values():
            evidence = adapter.probe_arm(arm)
            # A probe that reports nothing has measured nothing, so the arm stays out of the mapping
            # rather than being recorded as an availability this run never observed.
            if isinstance(evidence, Mapping):
                observed[arm] = bool(evidence.get("codemap_available"))
    finally:
        adapter.close()
    return observed


def run_paid(
    tasks: list[dict[str, Any]],
    *,
    scope: Mapping[str, Any],
    repo_path: Path,
    index_path: Path,
    marketplace_root: Path,
    codemap_bin: Path,
    auth_source: Path,
    run_dir: Path,
    model: str,
    structural_manifest_path: Path,
    index_relocation: Mapping[str, str] | None = None,
) -> Path:
    """Execute a human-authorized selected calibration in isolated native homes."""
    run_log = run_dir / "run.log"
    structural = _structural()
    adapter = structural.CodexRunner(
        model,
        repo_path,
        index_path=index_path,
        marketplace_root=marketplace_root,
        codemap_bin=codemap_bin,
        manifest_path=structural_manifest_path,
        auth_source=auth_source,
        index_relocation=index_relocation,
    )

    def prepare_run(destination: Path) -> None:
        """Create the plain run log and archive immutable stage inputs."""
        run_log.touch(exist_ok=False)
        adapter.create_input_snapshot(
            destination,
            tasks_path=TASKS_PATH,
            manifest_path=structural_manifest_path,
            runner_path=STRUCTURAL_PATH,
            invocation_launcher_path=STRUCTURAL_PATH,
            tasks=[item["task"] for item in tasks],
            arms=tuple(_NATIVE_ARMS.values()),
            additional_shared_files={
                "readcrop-contracts.py": ROOT / "benchmarks" / "_bench_common" / "readcrop_contracts.py",
                "codex-readcrop-stage.py": Path(__file__),
                "codex-runtime.py": BENCHMARKS / "_bench_codex" / "runtime.py",
            },
        )

    def run_cell(item: Mapping[str, Any], arm: str) -> Mapping[str, Any]:
        """Execute and parse one source-extraction arm in its verified home."""
        home = adapter.prepare_verified_home(_NATIVE_ARMS[arm])
        started = time.monotonic()
        try:
            stream = adapter.run_stream(adapter.build_command(readcrop_prompt(arm, item["task"])), home.env)
            row = parse_readcrop_stream(
                stream,
                arm=arm,
                contract=item["contract"],
                skill_path=home.codemap_skill_path if arm == "C_strict" else None,
            )
            row["elapsed_s"] = time.monotonic() - started
            row["source_sha256"] = hashlib.sha256(item["source"].encode("utf-8")).hexdigest()
            return row
        finally:
            try:
                if home.coordination_path is not None:
                    structural.cleanup_coordination_root(home.coordination_path)
            finally:
                home.cleanup()

    def validate_row(item: Mapping[str, Any], _arm: str, row: Mapping[str, Any]) -> None:
        """Reject malformed usage or provider-contract drift before telemetry persistence."""
        ReadcropUsage(row["input_tokens"], row["tool_result_tokens"])
        validate_provider_binding(item["contract"], row["provider_binding"])

    def persist_metadata(path: Path, payload: Mapping[str, Any]) -> None:
        """Write the durable stage state after each lifecycle transition."""
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def emit_lifecycle(event: str, values: Mapping[str, Any]) -> None:
        """Persist human-readable lifecycle lines without ANSI escape sequences."""
        if event == "artifacts":
            emit_progress(
                run_log, format_artifact_block(telemetry=values["telemetry_path"], metadata=values["metadata_path"])
            )
        else:
            emit_progress(
                run_log,
                f"SUMMARY  status={values['status']}  persisted_cells={values['persisted_cells']}/{values['total_cells']}",
            )

    return run_paid_stage(
        tasks=tasks,
        arms=ARMS,
        run_dir=run_dir,
        metadata={"stage_id": "readcrop", "scope": dict(scope)},
        callbacks=PaidStageCallbacks(
            run_cell=run_cell,
            validate_row=validate_row,
            prepare_run=prepare_run,
            persist_metadata=persist_metadata,
            emit_lifecycle=emit_lifecycle,
            emit_row=lambda row, completed, total, arm: emit_progress(
                run_log, format_result_row(row, completed=completed, total=total), arm=arm
            ),
            write_checksums=write_checksums,
            close_adapter=adapter.close,
        ),
    )


def run_stage(
    *,
    repo_path: Path,
    model: str,
    tasks_selector: str | None,
    dry_run_requested: bool,
    resolve_scope_requested: bool,
    auth_source: Path | None,
    run_dir: Path | None,
    paid_approval: str | None,
    index_path: Path | None = None,
    marketplace_root: Path | None = None,
    codemap_bin: Path | None = None,
    tasks_path: Path = TASKS_PATH,
    methodology_path: Path = METHODOLOGY_PATH,
    structural_manifest_path: Path = BENCHMARKS / "manifests" / "codex-integration.json",
    rescore_run_dir: Path | None = None,
    rescore_output_dir: Path | None = None,
    emit_authorization: bool = True,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Run, plan, resolve, or rescore the nonpoolable ReadCrop stage.

    ``index_relocation`` is present only for a run outside the canonical clone; it travels to every adapter this stage
    builds, because each one admits the source and index before touching them.
    """
    # Historical replay uses frozen source contracts and source_scope, never a fresh execution scope.
    if rescore_run_dir is not None and not resolve_scope_requested:
        if rescore_output_dir is None:
            raise ValueError("--rescore-run-dir requires --rescore-output-dir")
        tasks = load_readcrop_tasks(tasks_path, methodology_path, repo_path)
        if tasks_selector:
            selected = {item.strip() for item in tasks_selector.split(",") if item.strip()}
            tasks = [item for item in tasks if item["contract"].task_id in selected]
            if not tasks or len(tasks) != len(selected):
                raise ValueError("--tasks must select known read-crop IDs")
        output_path = rescore_results(rescore_run_dir, rescore_output_dir, tasks)
        print(f"rescored: {output_path}")
        return
    index_path = index_path or (repo_path / ".cache" / "codemap" / f"{repo_path.name}.json")
    marketplace_root = marketplace_root or ROOT
    codemap_bin = codemap_bin or (ROOT / "plugins" / "codemap-py" / "bin" / "codemap-py")
    tasks, scope = _prepare_readcrop_scope(
        repo_path=repo_path,
        model=model,
        tasks_selector=tasks_selector,
        tasks_path=tasks_path,
        methodology_path=methodology_path,
        structural_manifest_path=structural_manifest_path,
    )
    if resolve_scope_requested:
        print(json.dumps(scope, sort_keys=True))
        return
    if dry_run_requested:
        observed_codemap = preflight_isolation(
            repo_path=repo_path,
            index_path=index_path,
            marketplace_root=marketplace_root,
            codemap_bin=codemap_bin,
            model=model,
            structural_manifest_path=structural_manifest_path,
            index_relocation=index_relocation,
        )
        runtime.print_section_rule("READCROP PREFLIGHT (no model)")
        for row in dry_run(tasks, observed_codemap=observed_codemap):
            runtime.print_plan_row(row)
        if emit_authorization:
            print(f"SCOPE   {scope['scope_sha256']}")
        return
    required = {
        "--auth-source": auth_source,
        "--run-dir": run_dir,
    }
    missing = [flag for flag, value in required.items() if value is None]
    if missing:
        raise ValueError(f"paid read-crop execution requires {', '.join(missing)}")
    approval = None if paid_approval is None else str(paid_approval)
    if not paid_approval_matches(approval, scope["scope_sha256"]):
        task_ids = ",".join(item["contract"].task_id for item in tasks)
        raise ValueError(
            "paid read-crop scope inputs changed after aggregate approval.\n"
            f"approved child scope: {approval}\n"
            f"current child scope: {scope['scope_sha256']}\n"
            "The runner, manifest, lifecycle, or task contract changed while the earlier run was active. "
            "No model call was made. Run this no-model preflight and copy its emitted PAID_COMMAND exactly:\n"
            f"python3 benchmarks/run-codex-structural.py --repo-path {repo_path} "
            f"--manifest-path {structural_manifest_path} --index-path {index_path} "
            f"--marketplace-root {marketplace_root} --codemap-bin {codemap_bin} "
            f"--model {model} --tasks {task_ids} --dry-run\n"
            "Do not reuse the previous --paid-approval value or run directory."
        )
    assert auth_source is not None and run_dir is not None
    run_path = run_paid(
        tasks,
        scope=scope,
        repo_path=repo_path,
        index_path=index_path,
        marketplace_root=marketplace_root,
        codemap_bin=codemap_bin,
        auth_source=auth_source,
        run_dir=run_dir,
        model=model,
        structural_manifest_path=structural_manifest_path,
        index_relocation=index_relocation,
    )
    print(f"done: {run_path}")
