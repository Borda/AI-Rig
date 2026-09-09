#!/usr/bin/env python3
"""Claude-only Codemap skill benchmark for agent exploration cost.

## What this measures

Four legacy arms run the same import-graph navigation tasks:

  plain    — developer with a minimal fix/feature/refactor/review skill; discovers structure via
             Grep / Glob / Bash
  codemap  — same skill extended with /codemap:query; uses the Skill tool for import-graph lookups
             instead of grepping for structural questions (semble MCP blocked via ``--disallowed-tools``)
  semble   — same skill extended with mcp__semble__search; uses the MCP tool for hybrid
             semantic + lexical search for import-graph questions instead of grepping
             (Skill tool blocked via ``--disallowed-tools``)
  combined — both /codemap:query and mcp__semble__search available; agent selects whichever
             tool is best suited for each question; no tools blocked

Core claim under test: one /codemap:query or mcp__semble__search call replaces many Grep passes,
reducing tool call count, elapsed time, and context consumption.

## What is NOT measured (excluded by design)

  scan-index  — builds the codemap import-graph index from the repo's Python sources. This is a
                one-time setup step that runs before the benchmark. Its cost (typically a few
                seconds) is intentionally excluded: it amortises over every subsequent query a
                developer makes and is not part of the per-task exploration loop.

  See: ``plugins/codemap-py/bin/scan-index --root <repo>``

## Metrics (per task × arm × model)

  Key metrics — headline savings signal:
  elapsed_s          — total wall-clock time for the run
  input_tokens (k)   — cumulative input tokens (system prompt + turns + results)

  Diagnostic metrics — explain how savings were achieved:
  tool_calls         — Grep / Glob / Bash / Skill invocations in the transcript
  tool_result_tokens (k) — tiktoken estimate of tokens in tool result content
  tool_elapsed_s     — wall-clock time inside tool execution (excludes LLM think)

## Savings formula (same as caveman evals)

  savings = 1 − (codemap_metric / plain_metric)   per task
  Reported as median / mean / min / max across tasks, per model tier.

## Quality scoring — exposure recall / report recall / skill coverage

  Purpose: assess whether the agent correctly identified the modules that import the task's primary_module
  (its "reverse dependencies", or rdeps). This is a proxy for blast-radius awareness — the core skill under test.

  Ground truth (deterministic, tool-independent):
    Derived from an independent AST scan of the repo, NOT from the codemap index the codemap
    arm queries. Every production .py file is parsed once; imports are inverted into an
    {imported_module: {importers}} map handling absolute, `from X import submodule`, aliased,
    and relative imports. Test modules (tests.*) excluded — blast-radius targets production
    callers only. The index-derived list is kept as a diagnostic: when the two disagree a
    per-task `[gt-divergence]` line is logged (missing_in_index = real importers the index lacks
    = potential plugin blind spot). Falls back to the index-derived list when no repo is scanned.

  Matching strategy — multi-form surface matching (v2):
    For each expected rdep, generate surface forms with 2+ path components:
      full dotted:   lightning.pytorch.trainer.trainer
      file path:     lightning/pytorch/trainer/trainer.py, src/lightning/pytorch/trainer/trainer.py
      2-suffix:      trainer.trainer, trainer/trainer, trainer/trainer.py
      3-suffix:      pytorch.trainer.trainer
    Bare leaf names (e.g., "trainer") are NEVER matched — minimum 2 components avoids false positives.
    All forms are word-boundary-aware and case-insensitive.

  Two-layer metrics:
    erec (exposure recall) — what the agent expressed in its own text:
      corpus = output_text only (agent-generated text; tool outputs excluded)
      erec = |{r in expected : any form matches in corpus}| / |expected|
      Arm-fair: all arms scored on identical corpus type; tool outputs excluded to avoid
      codemap erec being near-tautological (skill echoes rdep list → automatic credit).

    rrec (report recall) — what the agent told the user:
      corpus = output_text after the last tool_use/tool_result event
      rrec = |{r in expected : any form matches in corpus}| / |expected|
      Both arms measured equally on their final answer.

    delta = erec - rrec — information gap (agent saw it but did not report it)
    deff = erec_tp / max(tool_calls, 1) — discovery efficiency (rdeps found per tool call)
    erec_top10 — erec restricted to top-10 most-central rdeps by in-degree (meaningful for tasks with ≥5 rdeps)

    sc (skill coverage, codemap only) — index completeness:
      Parsed from the codemap:query rdeps skill result; measures whether the index contained the answer.

  Interpretation guidance:
    — erec high, rrec low → agent found the rdeps but answered in prose without repeating them
    — erec high for codemap, low for plain → codemap skill provided structural context the plain arm missed
    — sc = 100% + erec = 100% → index is complete AND agent processed the result
    — delta ≈ 0 → agent reported everything it found
    — deff higher for codemap → fewer tool calls needed for the same coverage

## Quick start

  # 1. Build the index once (excluded from benchmark timing)
  python plugins/codemap-py/bin/scan-index --root /path/to/repo

  # 2. Run all tasks across all model tiers
  python benchmarks/run-claude-agentic.py --repo-path /path/to/repo --all --report

  # 3. Spot-check one task in plain arm only
  python benchmarks/run-claude-agentic.py --repo-path /path/to/repo \\
      --tasks T01 --arm plain --model haiku

## Requirements

  - claude CLI on PATH (uses Claude Code subscription — no API key)
  - pip install --group pyproject.toml:bench  (deps in pyproject [dependency-groups] bench)
  - uv add semble  (alternative: uv add semble>=0.1.0)
  - Pre-built codemap index (see the first quick-start command above)

## Failure conditions

  A run is marked success=False when any of these occur:
    timeout          — claude subprocess exceeded its per-model wall-clock limit
                       (haiku 210 s / sonnet 420 s / opus 600 s; see MODEL_TIMEOUT)
    non-zero exit    — claude returned a non-success subtype in the result event; stderr is captured as error
    codemap no-call  — codemap arm completed without ever invoking the Skill tool; this means the agent fell
                       back to grep/bash entirely, defeating the purpose of the codemap arm
    semble no-call   — semble arm completed without ever calling mcp__semble__search or mcp__semble__find_related
    combined no-call  — combined arm completed without ever calling Skill or any semble MCP tool

  Cross-arm tool contamination is blocked at the CLI level (not just by instruction) via ``--disallowed-tools``:
    codemap arm      — mcp__semble__search and mcp__semble__find_related are hard-blocked
    semble arm       — Skill is hard-blocked

## Terminal output (one line per completed run)

    Each run prints a coloured summary line to stdout via tqdm.write:
    [NN/TT] TASK_ID (type/difficulty) | model  | arm       | elapsed=  NNN.Ns | tokens= NNN.Nk |
    calls= N (Gp= N; Gb= N; Bh= N; Sk= N; semble= N; blk= N; bfi= N)
    | erec= N% rrec= N%  sc= N%   ← legacy/noncanonical rows; quality=n/a when no ground truth
    | quality= N% exact=[!|✓|✗]   ← canonical A/B/C rows under agentic-graded-v2
  Quality fields:
    quality — admitted graded answer quality; shared gates force execution/incomplete cells to 0%
    exact   — ! execution/incomplete, ✓ full exact pass, ✗ completed non-pass
    erec  — exposure recall: rdeps found in output_text + codemap skill results (multi-form, 2+ components)
    rrec  — report recall: rdeps found in final answer text after last tool call
    sc    — skill coverage (codemap arm only): fraction of expected rdeps returned by the skill call;
             omitted on plain arm; measures index completeness, not agent verbosity
  Colour coding:
    yellow  — plain arm
    cyan    — codemap arm
    blue    — semble arm
    green   — combined arm (both tools available, agent chooses)
    red     — any arm where success=False (overrides arm colour)

## JSON output schema (benchmarks/results/code-YYYY-MM-DD.json)

  Written after every run (rolling snapshot) so partial results survive interruptions.

  {
    "metadata": {
      "date": "ISO-8601 timestamp",
      "models": "haiku, sonnet, opus",
      "repo": "/abs/path/to/repo",
      "index": "/abs/path/to/index.json",
      "task_count": N
    },
    "results": [
      {
        "arm": "plain" | "codemap" | "semble" | "combined",
        "task_id": "T01",
        "task_type": "fix" | "feature" | "refactor" | "review",
        "model": "haiku" | "sonnet" | "opus",
        "success": true | false,
        "tools": {"grep": N, "glob": N, "bash": N, "skill": N},
        "input_tokens": N,          ← sum of input + cache_creation + cache_read tokens
        "output_tokens": N,
        "tool_result_tokens": N,    ← tiktoken estimate of tool result content
        "elapsed_s": N.N,
        "tool_elapsed_s": N.N,      ← wall-clock inside tool execution only
        "error": "",                ← non-empty on failure
        "tool_log": ["Grep: pattern in path", ...],
        "output_text": "...",       ← full agent text output (used for quality scoring)
        "quality": {
          "scored": true | false,       ← false when task has no primary_module in index
          "erec": N.N,                  ← exposure recall: rdeps found in output_text + codemap results
          "erec_tp": N, "erec_fn": N,   ← multi-form true positives / false negatives on exposure corpus
          "rrec": N.N,                  ← report recall: rdeps found in final answer text
          "rrec_tp": N, "rrec_fn": N,   ← multi-form true positives / false negatives on report corpus
          "delta": N.N,                 ← erec - rrec: information seen but not reported
          "erec_top10": N.N,            ← erec on top-10 most-central rdeps by in-degree; equals erec when |rdeps|≤10
          "erec_top10_k": N,            ← k used: min(10, |expected|)
          "deff": N.N,                  ← erec_tp / max(tool_calls, 1): discovery efficiency
          "skill_coverage": N.N | null, ← codemap arm: fraction of expected rdeps in skill result; null for plain
          "skill_returned": N | null,   ← count of modules the skill call returned; null for plain
          "leaf_recall": N.N,           ← legacy: leaf-name recall on output_text
          "recall": N.N, "precision": N.N, "f1": N.N,  ← legacy aliases
          "tp": N, "fp": N, "fn": N, "leaf_tp": N, "leaf_fn": N, "ambiguous_leaves": N
        }
      }
    ]
  }

## Stream-JSON event parsing

  The benchmark invokes:
      claude -p --verbose --output-format stream-json --system-prompt "..." "task prompt"

  Events parsed:
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Grep",...}],...}}
      → increments tool counter; records tool_use_id + timestamp for elapsed tracking
      → text blocks are concatenated into output_text for quality scoring

    {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"...","content":"..."}]}}
      → records elapsed since matching tool_use; tokenises result content with tiktoken

    {"type":"result","usage":{"input_tokens":N,"output_tokens":N,...}}
      → captures final cumulative token usage (all cache partitions summed)
"""

import ast
import contextlib
import hashlib
import inspect
import json
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from types import SimpleNamespace

import fire
import pandas as pd

from rich.text import Text as _Text

# benchmarks/ is not a package; make its private shared package importable
# regardless of how this script is launched (direct path, symlink, or any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bench_common.benchmark_paths import RESULTS_DIR  # noqa: E402
from _bench_common.change_impact_stage import run_stage as run_change_impact_stage  # noqa: E402
from _bench_common.change_impact_contracts import source_fingerprint as change_impact_source_fingerprint  # noqa: E402
from _bench_common.claude_transport import MODEL_TIMEOUT, MODELS, parse_result_usage, stream_claude  # noqa: E402
from _bench_common.codemap_discovery import codemap_bin_on_path, resolve_index_path  # noqa: E402
from _bench_common import presentation  # noqa: E402
from _bench_common import agentic_reporting  # noqa: E402
from _bench_common.agentic_reporting import cell_passes, cell_quality, summary_lines, summarize_agentic  # noqa: E402
from _bench_common.presentation import (  # noqa: E402
    format_artifact_block,
    format_paid_command_block,
    format_quality,
    fmt_time,
    fmt_tok,
    make_progress,
)
from _bench_common.python_source import extract_import_targets, iter_py_files, module_from_init_chain  # noqa: E402

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.python_source import resolve_relative_base  # noqa: E402,F401
from _bench_common.agentic_contracts import (  # noqa: E402
    AGENTIC_ARMS,
    DEFAULT_REPETITIONS,
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
    answer_failure_details,
    assess_answer_response,
    build_oracle,
    materialize_agentic_prompt,
    score_answer,
    score_evidence_metrics,
)
from _bench_common.provider_parity_contracts import (  # noqa: E402
    ARM_CONTRACTS,
    PARITY_TIMEOUT_SECONDS,
    canonical_task_hash,
    deterministic_arm_order,
    fresh_input_tokens,
    load_task_policies,
    load_task_suite,
    materialize_task_prompt,
    semantic_suite_hash,
    token_accounting_inconsistent,
    treatment_adherence,
)
from _bench_common.edit_patch_contracts import (  # noqa: E402
    EditExecution,
    EditTaskContract,
    build_patch_answer,
    score_edit_execution,
    validate_patch_index_bundle,
)
from _bench_common.mutation_isolation import (  # noqa: E402
    PATCH_PYTEST_ENV,
    load_index_relocation,
    verify_index_relocation,
    create_patch_task_agent_workspace,
    create_executable_agent_workspace,
    execute_patch_task_answer,
    execute_fix_multi_patch,
    execute_fix_single_patch,
    patch_test_runtime_identity,
    relocate_frozen_index_for_worktree,
)
from _bench_common.paid_lifecycle import (  # noqa: E402
    PaidStageCallbacks,
    paid_approval_matches,
    paid_approval_token,
    run_paid_stage,
    write_checksums,
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (  # noqa: E402,F401
    FIX_MULTI_TASKS_PATH,
    FIX_SINGLE_ARMS,
    FIX_SINGLE_TASKS_PATH,
    FixMultiContract,
    FixSingleContract,
    PARITY_MANIFEST_PATH,
    PATCH_TASKS_PATH,
    PurePosixPath,
    READCROP_ARMS,
    READCROP_TASKS_PATH,
    ReadcropUsage,
    StageIdentity,
    _FIX_MULTI_QUERY_ARGUMENTS,
    _FIX_SINGLE_QUERY_ARGUMENTS,
    _PATCH_QUERY_ARGUMENTS,
    _READCROP_ANSWER_RE,
    _absolute_codemap_launchers,
    _claude_codemap_evidence,
    _claude_event_summary,
    _claude_message_blocks,
    _command_arguments,
    _compact_query_result_succeeded,
    _frozen_index_recovery_attempted,
    _is_compact_query,
    _is_inside_workspace,
    _load_claude_fix_tasks,
    _manifest_sha256,
    _native_tool_result_succeeded,
    _outside_workspace_path_evidence,
    _patch_index_path,
    _patch_stage_identity,
    _provider_binding,
    _query_arguments_from_bash,
    _query_command_tail,
    _readcrop_module_path,
    _resolve_claude_fix_scope,
    _study_query_arguments,
    _tool_input_strings,
    _tool_result_text,
    _workspace_containment_roots,
    build_edit_task_contract,
    build_fix_multi_contract,
    build_fix_single_contract,
    build_readcrop_contract,
    extract_readcrop_symbol_source,
    load_claude_fix_multi_tasks,
    load_claude_fix_single_tasks,
    load_claude_patch_tasks,
    load_claude_readcrop_tasks,
    parse_claude_readcrop_events,
    parse_readcrop_answer,
    prompt_hash,
    readcrop_prompt,
    resolve_claude_fix_multi_scope,
    resolve_claude_fix_single_scope,
    resolve_claude_patch_scope,
    resolve_readcrop_scope,
    score_readcrop_answer,
    stage_contract_sha256,
)

_console = presentation.benchmark_console()

PATCH_INDEX_LOCKS_PATH = Path(__file__).resolve().parent / "suites" / "patch-index-locks.json"
FIX_MULTI_ARMS = READCROP_ARMS
PATCH_ARMS = READCROP_ARMS
LEGACY_EXPERIMENT_REVISION = "legacy-unversioned"


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
                "claude_runner": _sha256_file(Path(__file__)),
                "paid_lifecycle": _sha256_file(Path(__file__).resolve().parent / "_bench_common" / "paid_lifecycle.py"),
                "claude_transport": _sha256_file(
                    Path(__file__).resolve().parent / "_bench_common" / "claude_transport.py"
                ),
                "mutation_isolation": _sha256_file(
                    Path(__file__).resolve().parent / "_bench_common" / "mutation_isolation.py"
                ),
                **(
                    {
                        "edit_patch_contracts": _sha256_file(
                            Path(__file__).resolve().parent / "_bench_common" / "edit_patch_contracts.py"
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
    import tempfile

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
    """Return the provider and shared implementation bytes frozen for a Claude Patch run."""
    benchmarks = Path(__file__).resolve().parent
    return {
        "claude-runner.py": Path(__file__),
        "paid-lifecycle.py": benchmarks / "_bench_common" / "paid_lifecycle.py",
        "edit-patch-contracts.py": benchmarks / "_bench_common" / "edit_patch_contracts.py",
        "mutation-isolation.py": benchmarks / "_bench_common" / "mutation_isolation.py",
        "patch-index-locks.json": PATCH_INDEX_LOCKS_PATH,
    }


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
        f"({completed}/{total}) {mark}  {str(row['task_id']):<6} {str(row['arm']):<8} "
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


def resolve_agentic_scope(
    manifest_path: Path = PARITY_MANIFEST_PATH,
    *,
    task_ids: Sequence[str] | None = None,
    arms: Sequence[str] = AGENTIC_ARMS,
    models: Sequence[str] | None = None,
    repetitions: int = DEFAULT_REPETITIONS,
) -> dict[str, object]:
    """Resolve one deterministic manifest-bound Claude agentic scope."""
    if repetitions < 1:
        raise ValueError("agentic repetitions must be at least 1")
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("provider-parity manifest is unavailable or malformed") from exc
    contract = manifest.get("agentic_execution_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("provider-parity manifest lacks the agentic execution contract")
    locked_task_ids = contract.get("task_ids")
    if not isinstance(locked_task_ids, list) or not all(isinstance(task_id, str) for task_id in locked_task_ids):
        raise ValueError("provider-parity manifest has invalid agentic task IDs")
    selected_task_ids = list(locked_task_ids if task_ids is None else task_ids)
    if not selected_task_ids or len(set(selected_task_ids)) != len(selected_task_ids):
        raise ValueError("agentic scope requires unique manifest-bound task IDs")
    if any(task_id not in locked_task_ids for task_id in selected_task_ids):
        raise ValueError("agentic scope includes a task outside the manifest")
    ordered_task_ids = [task_id for task_id in locked_task_ids if task_id in set(selected_task_ids)]
    selected_arms = list(arms)
    if (
        not selected_arms
        or len(set(selected_arms)) != len(selected_arms)
        or any(arm not in AGENTIC_ARMS for arm in selected_arms)
    ):
        raise ValueError("Claude agentic scope requires unique canonical A/B/C arms")
    selected_models = list(MODELS if models is None else models)
    if (
        not selected_models
        or len(set(selected_models)) != len(selected_models)
        or any(model not in MODELS for model in selected_models)
    ):
        raise ValueError("Claude agentic scope requires unique supported model aliases")
    coordinate_timeout_seconds = contract.get("coordinate_timeout_seconds")
    if coordinate_timeout_seconds != PARITY_TIMEOUT_SECONDS:
        raise ValueError("provider-parity manifest must lock the canonical coordinate timeout")
    total_cells = len(ordered_task_ids) * len(selected_arms) * len(selected_models) * repetitions
    payload: dict[str, object] = {
        "provider": "claude",
        "manifest_sha256": _manifest_sha256(Path(manifest_path)),
        "experiment_revision": manifest.get("experiment_revision"),
        "task_ids": ordered_task_ids,
        "arms": selected_arms,
        "models": selected_models,
        "repetitions": repetitions,
        "coordinate_timeout_seconds": coordinate_timeout_seconds,
        "total_cells": total_cells,
        "nonpoolable": True,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**payload, "scope_sha256": hashlib.sha256(encoded).hexdigest()}


def _delivered_task_prompt(task: Mapping[str, object], *, canonical: bool) -> str:
    """Return the exact provider-visible prompt for one task.

    Canonical cells add the shared JSON response contract after the materialized task prose. Explicit legacy arms retain
    their historical prose-only prompt.
    """
    if canonical:
        return materialize_agentic_prompt(task)
    return materialize_task_prompt(task)


def _delivered_prompt_hash(task: Mapping[str, object], *, canonical: bool) -> str:
    """Return the SHA-256 identity of the exact prompt delivered to Claude."""
    return hashlib.sha256(_delivered_task_prompt(task, canonical=canonical).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# RESULTS_DIR, MODELS, and MODEL_TIMEOUT come from shared benchmark_paths and claude_transport modules.

# Cost is the fair cross-arm metric because arms differ in how many tokens they burn to reach the
# same answer. We use Anthropic's own per-run total_cost_usd (captured from the stream-json result
# event) — current prices, cache-aware, per model, with no local price table to drift out of date
# (an earlier hand-maintained table silently carried Opus 4.1 prices after the models moved to 5).
# A run with no result event (crash/timeout) keeps cost_usd = 0.0 and its $ column is omitted.


def run_cost_usd(r: "BenchmarkRun") -> float:
    """Return the run's captured USD cost (Anthropic's total_cost_usd), or 0.0 when unavailable.

    The cost comes straight from the stream-json ``result`` event's ``total_cost_usd`` — current
    list prices, cache-aware, per model — so there is no local price table to drift. A run that
    produced no result event (crash/timeout) keeps ``cost_usd = 0.0``, and callers omit the $ column.

    Args:
        r: Completed benchmark run.

    Returns:
        The run's cost in USD, or 0.0 when the result event carried no total_cost_usd.

    Examples:
        >>> from types import SimpleNamespace as N
        >>> run_cost_usd(N(cost_usd=0.42))
        0.42
        >>> run_cost_usd(N(cost_usd=0.0))
        0.0
    """
    return getattr(r, "cost_usd", 0.0) or 0.0


# fmt_tok comes from presentation (shared with run-claude-structural).


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class QualityScore:
    """Quality score for a single benchmark run.

    Primary metrics (v2 — multi-form matching with 2+ component surface forms):
        ``erec``  — exposure recall: rdeps found in agent output_text (tool outputs excluded)
        ``rrec``  — report recall: rdeps found in agent's final answer after last tool call
        ``delta`` — erec - rrec: information gap (agent saw but did not report)
        ``deff``  — discovery efficiency: erec_tp / max(tool_calls, 1)

    Supplementary (codemap arm only):
        ``skill_coverage``, ``skill_returned``

    Semble-native lens (semble / combined arms only):
        ``chunk_hit_rate`` — expected rdeps whose module/file appears in any retrieved semble chunk

    Legacy fields (``leaf_recall``, ``precision``, ``recall``, ``f1``, ``tp``, ``fp``, ``fn``)
    are retained for backward compatibility; computed via leaf-name matching on output_text.
    """

    scored: bool = False  # False when no ground truth is available

    # ── Primary metrics (v2 — multi-form matching) ──
    erec: float = 0.0  # exposure recall: rdeps found in output_text + codemap results
    erec_tp: int = 0
    erec_fn: int = 0
    rrec: float = 0.0  # report recall: rdeps found in final answer text (after last tool call)
    rrec_tp: int = 0
    rrec_fn: int = 0
    delta: float = 0.0  # erec - rrec: information the agent saw but did not report
    deff: float = 0.0  # discovery efficiency: erec_tp / max(tool_calls, 1)
    erec_top10: float = 0.0  # erec restricted to top-10 rdeps by in-degree (reverse-dep) centrality
    erec_top10_k: int = 0  # actual k used (min(10, |expected|)); equals |expected| when ≤10

    # ── Skill result coverage (codemap arm only; None when not applicable) ──
    skill_coverage: Optional[float] = None
    skill_returned: Optional[int] = None

    # ── Semble-native lens (semble / combined arms only; None when not applicable) ──
    # chunk_hit_rate: fraction of expected rdep modules whose module/file appears in ANY semble
    # search chunk the arm retrieved. A fair semantic-search axis that does not require semble to
    # emit an exhaustive dotted rdep list; erec/rrec stay the codemap-native lens.
    chunk_hit_rate: Optional[float] = None

    # ── Targeted-test correctness signal (fix tasks that declare a test_target; None otherwise) ──
    # test_passed: outcome of running the task's declared pytest node on the post-edit sandbox.
    # A stronger correctness signal than keyword recall — recorded alongside erec, never replacing
    # it. None when the task declares no test or the test could not be launched.
    test_passed: Optional[bool] = None

    # ── Legacy fields (backward compat — leaf-name matching on output_text) ──
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    leaf_recall: float = 0.0
    leaf_tp: int = 0
    leaf_fn: int = 0
    ambiguous_leaves: int = 0


@dataclass
class ToolCounts:
    grep: int = 0
    glob: int = 0
    bash: int = 0
    skill: int = 0  # /codemap:query and other skill invocations via the Skill tool
    semble: int = 0  # mcp__semble__search and mcp__semble__find_related calls
    blocked: int = 0  # tool_use events that returned <tool_use_error> (permission-denied or disallowed)
    bash_for_imports: int = 0  # bash calls matching import-discovery patterns (grep/rg for import)
    index_reads: int = 0  # bash calls that read .cache/codemap/ or .cache/scan/ index files directly
    scan_query: int = 0  # Bash calls that invoke scan-query; diagnostic subset of bash
    codemap: int = 0  # Codemap Skill calls; diagnostic subset of skill

    @property
    def total(self) -> int:
        """Sum of all tool call counts across all arms.

        >>> ToolCounts(grep=3, bash=1, semble=2).total
        6
        >>> ToolCounts().total
        0
        """
        return self.grep + self.glob + self.bash + self.skill + self.semble


@dataclass
class BenchmarkRun:
    """Result of a single benchmark run (one task x arm x model).

    Renamed from RunResult; field names are unchanged, so ``asdict()`` output and serialised JSON remain identical.
    """

    arm: str
    task_id: str
    task_type: str
    model: str  # short tier name: haiku / sonnet / opus
    success: bool
    repetition: int = 1
    experiment_revision: str = ""
    parity_arm: str | None = None
    codemap_compliant: bool | None = None
    codemap_query_attempted: int = 0
    codemap_query_succeeded: int = 0
    codemap_compact_success: bool = False
    index_relocations: list[dict[str, str]] = field(default_factory=list)
    treatment_adherence: bool | None = None
    contaminated: bool = False
    incomplete: bool = False
    task_hash: str = ""
    prompt_hash: str = ""
    suite_hash: str = ""
    suite_raw_hash: str = ""
    evaluator_id: str = ""
    evaluator_hash: str = ""
    envelope_hash: str = ""
    arm_contract_hash: str = ""
    repo_sha: str = ""
    index_sha: str = ""
    oracle_class: str = ""
    headline_eligible_v1: bool = False
    scoreable: bool = True
    answer_scored: bool = False
    answer_quality_score: float | None = None
    answer_correct: bool | None = None
    answer_components: dict[str, float] = field(default_factory=dict)
    answer_graded_score: float | None = None
    answer_graded_components: dict[str, float] = field(default_factory=dict)
    answer_error: str = ""
    answer_failure_details: list[dict[str, Any]] = field(default_factory=list)
    answer_contract_valid: bool | None = None
    answer_diagnostic_only: bool = False
    answer_pooling_eligible: bool = False
    tools: ToolCounts = field(default_factory=ToolCounts)
    # Token metrics
    input_tokens: int = 0
    output_tokens: int = 0
    tool_result_tokens: int = 0  # tiktoken estimate of tool result content
    cache_read_tokens: int = 0  # cache-hit input tokens (billed ~0.1x) — for cache-aware cost
    cache_creation_tokens: int = 0  # cache-write input tokens (billed ~1.25x)
    cost_usd: float = 0.0  # Anthropic's total_cost_usd for this run (current prices); 0.0 if absent
    usage_complete: bool = False
    # Timing metrics (stored in seconds)
    elapsed_s: float = 0.0
    tool_elapsed_s: float = 0.0  # time inside tool execution only
    error: str = ""
    error_type: str = ""  # subtype from result event: error_max_turns | error_non_zero_exit | error_timeout | ""
    # Per-call log for post-run investigation: ["Bash: grep -r 'import'", "Skill: /codemap:query rdeps ..."]
    tool_log: list[str] = field(default_factory=list)
    # Raw tool_use_error payloads (first 2k chars each) for diagnosing skill failures
    tool_errors: list[str] = field(default_factory=list)
    # Full agent output text — captured for quality scoring
    output_text: str = ""
    quality: QualityScore = field(default_factory=QualityScore)
    # Internal — excluded from JSON (see _save_snapshot); populated for fix_single/fix_multicaller tasks
    agent_diff: str = field(default="", repr=False)  # unified diff of agent's edits vs original codebase
    # Transient carrier for the declared targeted-test outcome (run in the sandbox, before cleanup);
    # excluded from JSON — the persisted signal lives in quality.test_passed.
    targeted_test_passed: Optional[bool] = field(default=None, repr=False)
    # Internal fields excluded from JSON serialisation (see _save_snapshot)
    skill_result_text: str = field(default="", repr=False)  # all codemap:query rdeps results joined (for sc)
    codemap_results: list[str] = field(default_factory=list, repr=False)  # ALL codemap skill results (for erec)
    semble_results: list[str] = field(default_factory=list, repr=False)  # ALL semble MCP tool results (for erec)
    last_tool_text_offset: int = field(default=0, repr=False)  # output_text offset after last tool event
    raw_events: list[dict[str, Any]] = field(default_factory=list, repr=False)


@dataclass
class Task:
    id: str
    type: str
    prompt: str
    primary_module: str = ""
    difficulty: str = "unknown"
    skill: str = ""
    symbol: str = ""  # read_crop tasks: target symbol (e.g. "Trainer.fit")
    expected_keywords: list[str] = field(default_factory=list)  # read_crop tasks: keyword-recall ground truth
    requires_reset: bool = False  # fix tasks: snapshot/restore codebase around each arm run
    codebase_module: str = ""  # fix tasks: top-level module to snapshot (e.g. "lightning")
    expected_patch_keywords: list[str] = field(default_factory=list)  # fix tasks: strings expected in diff +lines
    expected_files: list[str] = field(default_factory=list)  # fix tasks: file-path fragments expected in diff
    test_target: str = ""  # fix tasks: pytest node id/path run on the post-edit sandbox for a correctness signal
    experiment_revision: str = ""
    task_hash: str = ""
    prompt_hash: str = ""
    suite_hash: str = ""
    suite_raw_hash: str = ""
    oracle_class: str = ""
    headline_eligible_v1: bool = False
    scoreable: bool = True
    answer_task: dict[str, object] = field(default_factory=dict, repr=False)


def parity_arm_identity(arm: str) -> str | None:
    """Return a canonical A/B/C arm only when that arm was explicitly executed.

    Legacy agentic labels have different historical no-call semantics, so they intentionally remain unlabelled rather
    than being retroactively mapped.
    """
    return arm if arm in ARM_CONTRACTS else None


def _canonical_agentic_row(result: BenchmarkRun) -> dict[str, Any]:
    """Normalize one prospective canonical Claude result for shared pass reporting.

    The Claude runner owns native event interpretation, while the shared reporter owns planned-coordinate denominators
    and paired comparisons. Absent terminal usage stays ``None`` here so it cannot look like measured zero consumption.
    """
    if result.parity_arm not in AGENTIC_ARMS:
        raise ValueError("canonical reporting requires a canonical A/B/C result")
    usage_available = result.usage_complete
    cached_input_tokens = result.cache_creation_tokens + result.cache_read_tokens
    return {
        "task_id": result.task_id,
        "repetition": result.repetition,
        "arm": result.parity_arm,
        "success": result.success,
        "answer_contract_valid": result.answer_contract_valid,
        "answer_pooling_eligible": result.answer_pooling_eligible,
        "diagnostic_only": result.answer_diagnostic_only,
        "incomplete": result.incomplete,
        "contaminated": result.contaminated,
        "treatment_adherence": result.treatment_adherence,
        "quality": {
            "correct": result.answer_correct,
            "quality_score": result.answer_quality_score,
            "components": dict(result.answer_components),
            "graded_score": result.answer_graded_score,
            "graded_components": dict(result.answer_graded_components),
        },
        "failure_details": [dict(detail) for detail in result.answer_failure_details],
        "answer_error": result.answer_error,
        "error_type": result.error_type,
        "usage_complete": usage_available,
        "token_accounting_inconsistent": (
            token_accounting_inconsistent(result.input_tokens, cached_input_tokens) if usage_available else None
        ),
        "input_tokens": result.input_tokens if usage_available else None,
        "cached_input_tokens": cached_input_tokens if usage_available else None,
        "fresh_input_tokens": (
            fresh_input_tokens(result.input_tokens, cached_input_tokens) if usage_available else None
        ),
        "output_tokens": result.output_tokens if usage_available else None,
        "elapsed_s": result.elapsed_s if usage_available or result.elapsed_s else None,
    }


def _locked_manifest_tasks(manifest_path: Path) -> tuple[dict[str, dict], list[dict]]:
    """Return manifest task rows and suites after structural validation."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"experiment manifest {manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("suites"), list):
        raise ValueError(f"experiment manifest {manifest_path} requires a suites list")

    task_rows: dict[str, dict] = {}
    suites: list[dict] = []
    for suite_index, suite in enumerate(manifest["suites"]):
        if not isinstance(suite, dict) or not isinstance(suite.get("tasks"), list):
            raise ValueError(f"experiment manifest {manifest_path} suite {suite_index} requires a tasks list")
        suites.append(suite)
        for task_index, task_row in enumerate(suite["tasks"]):
            task_id = task_row.get("id") if isinstance(task_row, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(
                    f"experiment manifest {manifest_path} suite {suite_index} task {task_index} requires an id"
                )
            if task_id in task_rows:
                raise ValueError(f"experiment manifest {manifest_path} contains duplicate task id {task_id!r}")
            task_rows[task_id] = task_row
    return task_rows, suites


def load_tasks_with_provenance(tasks_path: Path, manifest_path: Path = PARITY_MANIFEST_PATH) -> list[Task]:
    """Load agentic tasks with immutable shared policy and canonical identity.

    Args:
        tasks_path: Raw agentic suite path.
        manifest_path: Locked provider-parity manifest defining task policy.

    Returns:
        Agentic task projections carrying the shared revision, hash, and policy fields.

    Raises:
        ValueError: If a task is missing from the locked policy manifest.
    """
    raw_suite_hash = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    raw_tasks = load_task_suite(tasks_path)
    manifest_tasks, manifest_suites = _locked_manifest_tasks(manifest_path)
    policies = load_task_policies(manifest_path)
    for raw_task in raw_tasks:
        task_id = raw_task["id"]
        if task_id not in policies:
            raise ValueError(f"no locked task policy for {task_id!r}")
        manifest_task = manifest_tasks.get(task_id)
        if manifest_task is None:
            raise ValueError(f"no locked manifest task for {task_id!r}")
        if canonical_task_hash(raw_task) != manifest_task.get("canonical_task_sha256"):
            raise ValueError(f"task hash mismatch for {task_id!r}")
        if _delivered_prompt_hash(raw_task, canonical=True) != manifest_task.get("prompt_sha256"):
            raise ValueError(f"prompt hash mismatch for {task_id!r}")
    raw_task_ids = [task["id"] for task in raw_tasks]
    matching_suites = [
        suite for suite in manifest_suites if [task.get("id") for task in suite["tasks"]] == raw_task_ids
    ]
    if len(matching_suites) != 1:
        raise ValueError("ordered task IDs do not match exactly one locked manifest suite")
    suite_hash = semantic_suite_hash(raw_tasks)
    loaded: list[Task] = []
    for raw_task in raw_tasks:
        task_id = raw_task["id"]
        actual_task_hash = canonical_task_hash(raw_task)
        policy = policies[task_id]
        loaded.append(
            Task(
                id=task_id,
                type=raw_task["type"],
                prompt=_delivered_task_prompt(raw_task, canonical=True),
                primary_module=raw_task.get("primary_module", ""),
                difficulty=raw_task.get("difficulty", "unknown"),
                skill=raw_task.get("skill", ""),
                symbol=raw_task.get("symbol", ""),
                expected_keywords=raw_task.get("expected_keywords", []),
                requires_reset=raw_task.get("requires_reset", False),
                codebase_module=raw_task.get("codebase_module", ""),
                expected_patch_keywords=raw_task.get("expected_patch_keywords", []),
                expected_files=raw_task.get("expected_files", []),
                test_target=raw_task.get("test_target", ""),
                experiment_revision=policy.experiment_revision,
                task_hash=actual_task_hash,
                prompt_hash=_delivered_prompt_hash(raw_task, canonical=True),
                suite_hash=suite_hash,
                suite_raw_hash=raw_suite_hash,
                oracle_class=policy.oracle_class,
                headline_eligible_v1=policy.headline_eligible_v1,
                scoreable=policy.scoreable,
                answer_task=dict(raw_task),
            )
        )
    return loaded


def load_legacy_tasks(tasks_path: Path) -> list[Task]:
    """Load an unlocked legacy suite without assigning canonical parity provenance."""
    raw_tasks = load_task_suite(tasks_path)
    suite_hash = semantic_suite_hash(raw_tasks)
    suite_raw_hash = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    return [
        Task(
            id=raw_task["id"],
            type=raw_task["type"],
            prompt=_delivered_task_prompt(raw_task, canonical=False),
            primary_module=raw_task.get("primary_module", ""),
            difficulty=raw_task.get("difficulty", "unknown"),
            skill=raw_task.get("skill", ""),
            symbol=raw_task.get("symbol", ""),
            expected_keywords=raw_task.get("expected_keywords", []),
            requires_reset=raw_task.get("requires_reset", False),
            codebase_module=raw_task.get("codebase_module", ""),
            expected_patch_keywords=raw_task.get("expected_patch_keywords", []),
            expected_files=raw_task.get("expected_files", []),
            test_target=raw_task.get("test_target", ""),
            task_hash=canonical_task_hash(raw_task),
            prompt_hash=_delivered_prompt_hash(raw_task, canonical=False),
            suite_hash=suite_hash,
            suite_raw_hash=suite_raw_hash,
            answer_task=dict(raw_task),
        )
        for raw_task in raw_tasks
    ]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a concrete benchmark input file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repository_fingerprint(repo_path: Path) -> str:
    """Return the checked-out commit SHA, with a deterministic non-git test fallback."""
    completed = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return hashlib.sha256(str(repo_path.resolve()).encode("utf-8")).hexdigest()


def _validate_parity_runtime(
    repo_path: Path,
    index_path: Path,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Reject a canonical agentic run outside the locked target and index.

    ``index_relocation`` excuses the locked byte hash only: a run in its own worktree proves index identity through
    relocation provenance, while the commit, tree, worktree cleanliness, metadata, and module-count checks stay exactly
    as strict.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"experiment manifest {manifest_path} is not valid JSON: {exc}") from exc

    target = manifest["target_source"]
    if _repository_fingerprint(repo_path) != target["commit"]:
        raise ValueError(f"canonical run requires target commit {target['commit']}")
    tree = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "HEAD^{tree}"],
        capture_output=True,
        check=False,
        text=True,
    )
    if tree.returncode != 0 or tree.stdout.strip() != target["tree"]:
        raise ValueError(f"canonical run requires target tree {target['tree']}")
    status = subprocess.run(
        ["git", "-C", str(repo_path), "status", "--porcelain"],
        capture_output=True,
        check=False,
        text=True,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError("canonical run requires a clean target worktree")

    expected_index = manifest["index"]
    index_sha256 = _sha256_file(index_path)
    if index_relocation is None and index_sha256 != expected_index["raw_sha256"]:
        raise ValueError("canonical run requires the locked index bytes")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("canonical run index is not valid JSON") from exc
    if index_relocation is not None:
        verify_index_relocation(
            index_relocation,
            metadata=index,
            index_sha256=index_sha256,
            repo_path=repo_path,
            frozen_index_sha256=expected_index["raw_sha256"],
        )
    for index_field in ("git_sha", "scan_version"):
        if index.get(index_field) != expected_index[index_field]:
            raise ValueError(f"canonical run index {index_field} does not match the locked manifest")
    if len(index.get("modules", [])) != expected_index["module_count"]:
        raise ValueError("canonical run index module count does not match the locked manifest")


def _invokes_scan_query(command: str) -> bool:
    """Return whether a shell command executes scan-query at a command boundary."""
    boundary = r"(?:^|&&|\|\||;|\|)\s*"
    environment = r"(?:env\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    return re.search(rf"{boundary}{environment}(?:\S*/)?scan-query(?:\s|$)", command) is not None


def _codemap_use_attempted(tools: ToolCounts) -> bool:
    """Return whether telemetry contains a Codemap Skill or scan-query attempt."""
    return tools.codemap > 0 or tools.scan_query > 0


def _evaluator_provenance(task_type: str) -> tuple[str, str]:
    """Identify the actual task-family scorer and hash its implementation source."""
    if task_type == "read_crop":
        evaluator = score_read_crop
    elif task_type in ("fix_single", "fix_multicaller"):
        evaluator = score_fix
    else:
        evaluator = GroundTruth.score
    evaluator_id = f"{evaluator.__module__}.{evaluator.__qualname__}"
    return evaluator_id, hashlib.sha256(inspect.getsource(evaluator).encode("utf-8")).hexdigest()


def count_tokens(text: str) -> int:
    """Approximate token count using tiktoken o200k_base (matches caveman evals)."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text))
    except ImportError:
        return max(1, len(text) // 4)  # ~4 chars/token fallback


def find_index(repo_path: Path, explicit: Optional[Path]) -> Path:
    """Locate the pre-built codemap index for the target repo.

    Thin adapter over :func:`_bench_common.codemap_discovery.resolve_index_path`: exact ``<repo_name>.json``
    match (no ``-master``/``-main`` stripping), ``.cache/codemap/`` before ``.cache/scan/``,
    resolved paths, and a raise on miss. The index is built once by ``scan-index`` and
    excluded from benchmark timing; this only validates it exists before any run starts.

    Args:
        repo_path: Root of the repository to benchmark.
        explicit: Caller-supplied index path; returned resolved when provided.

    Returns:
        Resolved absolute path to the located index file.

    Raises:
        FileNotFoundError: If no index is found under ``.cache/codemap/`` or ``.cache/scan/``
            and ``explicit`` was not provided.
    """
    return resolve_index_path(repo_path, explicit, strip_suffixes=False, missing="raise")


def check_semble_mcp() -> None:
    """Verify semble is installed and configured as an MCP server before the run starts.

    Checks two things:
      1. The semble Python package is importable (the MCP server ships with it).
      2. `claude mcp get semble` exits 0 — works for all scopes (user / project / local).

    Raises RuntimeError with actionable instructions when either check fails.
    """
    try:
        import semble as _semble  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "semble package not installed — required for the semble arm.\n"
            "Install it:\n"
            "  pip install semble>=0.1.0\n"
            "  # or: uv add semble"
        )

    r = subprocess.run(
        ["claude", "mcp", "get", "semble"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "semble MCP server not configured — run once to register it:\n"
            "  claude mcp add semble -s user -- uvx --from 'semble[mcp]' semble\n"
            "Use -s project instead of -s user to scope to this repo only."
        )


def _unique_path(path: Path) -> Path:
    """Return path unchanged if it doesn't exist; otherwise append a counter suffix."""
    if not path.exists():
        return path
    n = 2
    while (path.parent / f"{path.stem}-{n}{path.suffix}").exists():
        n += 1
    return path.parent / f"{path.stem}-{n}{path.suffix}"


def _tool_key_arg(name: str, inp: dict) -> str:
    """Return a short human-readable argument string for tool call logging.

    >>> _tool_key_arg("Grep", {"pattern": "import auth", "path": "src/"})
    "'import auth' in src/"
    >>> _tool_key_arg("mcp__semble__search", {"query": "import checkpoint_connector", "repo": "repo", "top_k": 20})
    "query='import checkpoint_connector'"
    >>> _tool_key_arg("mcp__semble__find_related", {"query": "find related", "line": 42})
    "query='find related'"
    """
    if name == "Grep":
        pat = inp.get("pattern", "")
        loc = inp.get("path", "") or inp.get("glob", "")
        return f"{pat!r} in {loc}" if loc else repr(pat)
    if name == "Glob":
        return inp.get("pattern", "")
    if name == "Bash":
        return inp.get("command", "")[:120]
    if name == "Skill":
        return f"{inp.get('skill', '')} {inp.get('args', '')}".strip()
    if name in ("mcp__semble__search", "mcp__semble__find_related"):
        inp_q = inp.get("query", "")[:80]
        return f"query={inp_q!r}"
    return str(inp)[:80]


# ---------------------------------------------------------------------------
# Independent AST-based reverse-dependency scan (tool-independent ground truth)
# ---------------------------------------------------------------------------

_SKIP_DIR_PARTS = frozenset({"tests", "test"})  # top-level test trees excluded from production rdeps


def _iter_py_files(root: Path) -> Iterator[Path]:
    """Yield ``*.py`` files under ``root``, pruning hidden and test directories.

    Directories whose name starts with ``.`` (``.git``, ``.cache``, ``.venv``) and any
    directory named ``tests``/``test`` are skipped — the latter mirrors the index rule that
    blast-radius analysis targets production callers only.

    Args:
        root: Repository root to walk.

    Yields:
        Absolute paths to candidate Python source files.
    """
    yield from iter_py_files(root, skip=_SKIP_DIR_PARTS)


def _derive_module_name(py_path: Path, root: Path) -> Optional[str]:
    """Derive the dotted module name of a file in scan-index's namespace.

    A file inside a package (its parent holds an ``__init__.py``) is named via its ``__init__.py``
    chain (:func:`module_from_init_chain`, scan-index Strategy 2). A loose file with no package parent
    is named by its path relative to *root* (separators → dots, ``.py`` dropped) — the same way
    scan-index (``path_to_module``) names a file outside any ``__init__`` chain
    (``examples.pytorch.domain_templates.imagenet``, not the bare stem ``imagenet``). Aligning the two
    keeps the AST oracle and the scan-index oracle in one namespace, so a loose importer no longer
    fires both ``missing_in_index`` and ``missing_in_ast`` as a spurious ``gt-divergence``.

    Args:
        py_path: Absolute path to a ``.py`` file inside ``root``.
        root: Scan root the loose-file dotted name is taken relative to.

    Returns:
        Dotted module name (e.g. ``lightning.pytorch.trainer.trainer`` for an in-package file,
        ``examples.pytorch.domain_templates.imagenet`` for a loose file). ``None`` when no name can
        be derived.

    Examples:
        >>> import tempfile, pathlib
        >>> with tempfile.TemporaryDirectory() as d:
        ...     r = pathlib.Path(d)
        ...     _ = (r / "pkg").mkdir()
        ...     _ = (r / "pkg" / "__init__.py").write_text("")
        ...     _ = (r / "pkg" / "mod.py").write_text("")
        ...     _ = (r / "examples").mkdir()
        ...     _ = (r / "examples" / "demo.py").write_text("")
        ...     (
        ...         _derive_module_name(r / "pkg" / "mod.py", r),
        ...         _derive_module_name(r / "examples" / "demo.py", r),
        ...     )
        ('pkg.mod', 'examples.demo')
    """
    # In-package file: name via the __init__ chain (scan-index Strategy 2), unchanged.
    if (py_path.parent / "__init__.py").exists():
        return module_from_init_chain(py_path) or None
    # Loose file (no package parent): name relative to the scan root — matches how scan-index names
    # files outside any __init__ chain, so both oracles share one namespace instead of the bare stem.
    try:
        rel_dotted = ".".join(py_path.relative_to(root).with_suffix("").parts)
    except ValueError:
        return module_from_init_chain(py_path) or None
    return rel_dotted or None


# resolve_relative_base comes from python_source (shared with run-codemap-cli).


def _extract_import_targets(tree: ast.Module, package: str, all_modules: set[str]) -> set[str]:
    """Collect internal modules a parsed file imports (base and submodule forms).

    Handles ``import a.b.c``, ``import a.b as z``, ``from a.b import c`` (crediting both the
    package ``a.b`` and the submodule ``a.b.c`` when the latter is a real module), and relative
    imports resolved against ``package``. Only targets present in ``all_modules`` are returned,
    so external and symbol-only imports are dropped.

    Args:
        tree: Parsed module AST.
        package: Dotted package of the importing module (for relative resolution).
        all_modules: Set of internal dotted module names to filter targets against.

    Returns:
        Set of internal dotted module names the file depends on.
    """
    # keep=all_modules filters to internal modules; default resolve+credit matches this lane.
    return extract_import_targets(tree, package=package, keep=all_modules)


def _scan_repo_importers(root: Path) -> dict[str, set[str]]:
    """Build a tool-independent reverse-dependency map by AST-parsing every source file once.

    The walk resolves each production module's imports and inverts them into
    ``{imported_module: {importer_module, ...}}``. This is the ground-truth source for quality
    scoring: unlike the codemap index it is not the artefact the codemap arm queries,
    so index blind spots (e.g. ``from pkg import submodule``) surface as divergences instead of
    being invisible.

    Args:
        root: Repository root to scan.

    Returns:
        Mapping from each internal dotted module name to the set of production modules importing it.
    """
    file_module: dict[Path, str] = {}
    for py_file in _iter_py_files(root):
        name = _derive_module_name(py_file, root)
        if name:
            file_module[py_file] = name
    all_modules = set(file_module.values())
    importers: dict[str, set[str]] = defaultdict(set)
    for py_file, module in file_module.items():
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"), filename=str(py_file))
        except (SyntaxError, ValueError):
            continue
        package = module if py_file.stem == "__init__" else module.rpartition(".")[0]
        for target in _extract_import_targets(tree, package, all_modules):
            if target != module:
                importers[target].add(module)
    return importers


# ---------------------------------------------------------------------------
# Quality scoring — deterministic ground truth
# ---------------------------------------------------------------------------


class GroundTruth:
    """Tool-independent ground truth for quality scoring benchmark runs.

    Loads the codemap index once for centrality metadata, then (when a repo path is given) derives the authoritative
    expected rdep sets from an independent AST scan of the repo — not from the same index the codemap arm queries. The
    index-derived list is retained as a diagnostic; per-task divergences are logged so index blind spots become visible
    instead of invisible. Exposes a ``score()`` method for comparing agent output to truth.
    """

    @staticmethod
    def _build_module_regexes(packages: set[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
        """Build dotted-name and ``src/`` path regexes for the given top-level packages.

        The package set is derived from the benchmark's tasks (each ``primary_module``'s
        first dotted component), so the extractor stays repo-agnostic — no package name is
        hardcoded. For ``pytorch-lightning`` (``packages={"lightning"}``) this reproduces the
        legacy ``\\blightning(?:\\.…)+`` behaviour exactly.

        Args:
            packages: Top-level package names to match (e.g. ``{"lightning"}``).

        Returns:
            ``(module_re, path_re)`` — the dotted-name matcher and the ``src/<pkg>/…\\.py``
            matcher. When ``packages`` is empty, both patterns match nothing.

        Examples:
            >>> mod_re, _ = GroundTruth._build_module_regexes({"lightning"})
            >>> mod_re.findall("see lightning.pytorch.trainer.trainer here")
            ['lightning.pytorch.trainer.trainer']
        """
        if not packages:
            never = re.compile(r"(?!x)x")  # matches nothing
            return never, never
        alt = "|".join(re.escape(p) for p in sorted(packages))
        module_re = re.compile(rf"\b(?:{alt})(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+")
        path_re = re.compile(rf"\bsrc/((?:{alt})(?:/[a-zA-Z_][a-zA-Z0-9_]*)+)\.py\b")
        return module_re, path_re

    def __init__(self, index_path: Path, tasks: list[Task], repo_path: Optional[Path] = None) -> None:
        """Load the index and pre-compute expected rdep sets for each task.

        Args:
            index_path: Path to the pre-built codemap JSON index produced by ``scan-index``.
                Used for module inventory and dep-count centrality only, never as the rdep oracle.
            tasks: Task definitions used to derive per-task ground-truth rdep sets. Tasks
                without a ``primary_module`` are skipped silently.
            repo_path: Repository root. When provided, expected rdeps come from an independent
                AST scan and divergences from the index are logged. When ``None`` (unit tests that
                supply only an index), the index-derived list is used as a fallback.
        """
        with index_path.open() as f:
            index = json.load(f)
        self.all_modules: set[str] = {m["name"] for m in index.get("modules", []) if m.get("status") == "ok"}
        # Top-level package name(s) for the repo under test, derived from the tasks' primary
        # modules (first dotted component) — keeps module extraction repo-agnostic (no hardcode).
        self.packages: set[str] = {
            getattr(t, "primary_module", "").split(".")[0] for t in tasks if getattr(t, "primary_module", "")
        }
        self._module_re, self._path_re = self._build_module_regexes(self.packages)
        self.index_expected: dict[str, set[str]] = self._index_rdeps(index, tasks)
        # Independent oracle: {imported_module: {importer, ...}}; empty when no repo path supplied.
        self.ast_importers: dict[str, set[str]] = _scan_repo_importers(repo_path) if repo_path else {}
        self.expected, self.divergences = self._resolve_expected(tasks, used_ast=bool(repo_path))
        # in-degree = reverse-dependency count per module (how many modules import it), derived from
        # the index import graph. Tasks define "central" as "imported by the most modules" — i.e.
        # in-degree — so top-10 ranks by in-degree, NOT dep_count. dep_count is the forward import
        # count (out-degree) and would rank on the opposite axis, measuring recall on the wrong
        # modules.
        _in_degrees: dict[str, int] = defaultdict(int)
        for m in index.get("modules", []):
            if m.get("status") != "ok":
                continue
            for imported in m.get("direct_imports", []):
                _in_degrees[imported] += 1
        # Top-10 most-central rdeps per task (by in-degree descending); k=min(10, |rdeps|)
        self.top10_expected: dict[str, frozenset[str]] = {}
        for task in tasks:
            rdeps = self.expected.get(task.id, set())
            if rdeps:
                ranked = sorted(rdeps, key=lambda m: _in_degrees.get(m, 0), reverse=True)[:10]
                self.top10_expected[task.id] = frozenset(ranked)
        self.all_leaf_names: set[str] = {m.split(".")[-1] for m in self.all_modules}
        # Match patterns cover the index inventory plus any AST-only expected rdep, so an arm that
        # finds a real importer the index missed can still be credited in the corpus.
        pattern_targets = set(self.all_modules)
        for rdeps in self.expected.values():
            pattern_targets |= rdeps
        self._match_patterns: dict[str, list[re.Pattern]] = {m: self._generate_match_set(m) for m in pattern_targets}
        self._emit_divergences()

    @staticmethod
    def _index_rdeps(index: dict, tasks: list[Task]) -> dict[str, set[str]]:
        """Derive the diagnostic index-based rdep set per task from ``direct_imports``."""
        modules = index.get("modules", [])
        out: dict[str, set[str]] = {}
        for task in tasks:
            pm = getattr(task, "primary_module", "")
            if not pm:
                continue
            out[task.id] = {
                m["name"]
                for m in modules
                if pm in m.get("direct_imports", [])
                and m.get("status") == "ok"
                # is_test is the scanner's own classification and catches non-"tests." roots
                # like tests_pytorch.*; the name-prefix check stays for indexes predating the
                # flag. The AST oracle prunes tests dirs entirely, so without this the same
                # test importers surface as spurious missing_in_ast gt-divergences (BA-16).
                and not m.get("is_test")
                and not m["name"].startswith("tests.")
            }
        return out

    def _resolve_expected(self, tasks: list[Task], used_ast: bool) -> tuple[dict[str, set[str]], dict[str, dict]]:
        """Select expected rdeps (AST when scanned, else index) and record index divergences.

        Args:
            tasks: Task definitions to resolve expected rdeps for.
            used_ast: True when an AST scan ran; its result becomes authoritative.

        Returns:
            ``(expected, divergences)`` where divergences maps task_id to
            ``{"ast", "index", "missing_in_index", "missing_in_ast"}`` for tasks that disagree.
        """
        if not used_ast:
            return dict(self.index_expected), {}
        expected: dict[str, set[str]] = {}
        divergences: dict[str, dict] = {}
        for task in tasks:
            pm = getattr(task, "primary_module", "")
            if not pm:
                continue
            ast_set = {m for m in self.ast_importers.get(pm, set()) if not m.startswith("tests.")}
            expected[task.id] = ast_set
            index_set = self.index_expected.get(task.id, set())
            missing_in_index = ast_set - index_set
            missing_in_ast = index_set - ast_set
            if missing_in_index or missing_in_ast:
                divergences[task.id] = {
                    "ast": len(ast_set),
                    "index": len(index_set),
                    "missing_in_index": sorted(missing_in_index),
                    "missing_in_ast": sorted(missing_in_ast),
                }
        return expected, divergences

    def _emit_divergences(self) -> None:
        """Print one summary line when the AST oracle and index disagree on any task.

        Per-task detail stays in ``self.divergences`` for callers/tests; a non-empty ``missing_in_index`` means the AST
        scan found real importers the index lacks — a potential plugin blind spot and the harness's added diagnostic
        value.
        """
        if not self.divergences:
            return
        missing_in_index = sum(len(d["missing_in_index"]) for d in self.divergences.values())
        missing_in_ast = sum(len(d["missing_in_ast"]) for d in self.divergences.values())
        if missing_in_index and missing_in_ast:
            detail = "index has blind spots and extra entries vs AST oracle"
        elif missing_in_index:
            detail = "index misses real importers the AST oracle found"
        else:
            detail = "index has extra entries the AST oracle excludes (e.g. test modules)"
        print(f"[gt-divergence] {len(self.divergences)}/{len(self.expected)} tasks diverged vs AST oracle ({detail})")

    @staticmethod
    def _generate_match_set(module: str) -> list[re.Pattern]:
        """Generate multi-form regex patterns for a module name.

        Each pattern requires at least 2 path components to avoid bare leaf-name false positives.
        Forms generated: full dotted path, file path variants, 2-component and 3-component suffixes.
        """
        parts = module.split(".")
        forms: set[str] = set()
        # Full dotted path: lightning.pytorch.trainer.trainer
        forms.add(module)
        # File path forms: lightning/pytorch/trainer/trainer.py, src/...
        file_path = module.replace(".", "/") + ".py"
        forms.add(file_path)
        forms.add("src/" + file_path)
        # 2-component suffix (minimum specificity): trainer.trainer, trainer/trainer
        if len(parts) >= 2:
            s2 = ".".join(parts[-2:])
            forms.add(s2)
            forms.add("/".join(parts[-2:]))
            forms.add("/".join(parts[-2:]) + ".py")
        # 3-component suffix: pytorch.trainer.trainer
        if len(parts) >= 3:
            forms.add(".".join(parts[-3:]))
        return [re.compile(r"\b" + re.escape(f) + r"\b", re.IGNORECASE) for f in forms]

    def _rdep_found(self, rdep: str, corpus: str) -> bool:
        """Return True if any multi-form pattern for ``rdep`` matches in ``corpus``."""
        for pat in self._match_patterns.get(rdep, []):
            if pat.search(corpus):
                return True
        return False

    def score(
        self,
        task_id: str,
        output_text: str,
        exposure_corpus: str,
        report_corpus: str,
        tool_calls: int = 0,
        skill_result_text: str | None = None,
        semble_result_text: str | None = None,
    ) -> QualityScore:
        """Compute quality score using multi-form matching and optional coverage lenses.

        Primary metrics (v2):
            ``erec`` — exposure recall on ``exposure_corpus`` (agent output_text only; tool outputs excluded)
            ``rrec`` — report recall on ``report_corpus`` (final answer after last tool call)
            ``delta`` — erec - rrec
            ``deff`` — erec_tp / max(tool_calls, 1)

        Supplementary:
            ``skill_coverage`` — fraction of expected rdeps in the skill result (codemap only)
            ``chunk_hit_rate`` — fraction of expected rdeps whose module/file appears in any
                retrieved semble chunk (semble / combined only); ``None`` when no semble corpus

        Legacy:
            ``leaf_recall`` etc. — leaf-name matching on ``output_text`` for backward compat
        """
        exp = self.expected.get(task_id, set())
        if not exp:
            return QualityScore(scored=False)

        # ── Primary: multi-form matching (v2) ──
        erec_matched = {r for r in exp if self._rdep_found(r, exposure_corpus)}
        rrec_matched = {r for r in exp if self._rdep_found(r, report_corpus)}
        n_exp = len(exp)
        erec_tp = len(erec_matched)
        rrec_tp = len(rrec_matched)
        erec = erec_tp / n_exp
        rrec = rrec_tp / n_exp
        delta = erec - rrec
        deff = erec_tp / max(tool_calls, 1)

        # erec@10 — exposure recall on top-10 most-central rdeps
        top10 = self.top10_expected.get(task_id)
        if top10:
            top10_tp = sum(1 for r in top10 if self._rdep_found(r, exposure_corpus))
            erec_top10 = top10_tp / len(top10)
            erec_top10_k = len(top10)
        else:
            erec_top10 = erec
            erec_top10_k = n_exp

        # ── Skill coverage (codemap arm only) ──
        # Two capture paths:
        # 1. Agent ran scan-query via Bash → skill_result_text is raw JSON → parse imported_by.
        # 2. Agent used Skill tool → tool returns rendered markdown, one module per line →
        #    extract dotted module names via regex (require ≥1 dot to avoid YAML-key false-positives).
        # Prose error text (blocked, permission denied) → None (unscored), not sc=0%.
        skill_coverage: Optional[float] = None
        skill_returned: Optional[int] = None
        if skill_result_text:
            returned: Optional[set] = None
            # The corpus is usually SEVERAL one-line scan-query JSON objects joined by
            # newlines (one per rdeps call) — whole-text json.loads fails with "Extra data"
            # on the second object, which silently killed sc for every multi-call run.
            # Parse per line and union imported_by across all parseable objects first.
            union: set[str] = set()
            parsed_any = False
            for line in skill_result_text.splitlines():
                line = line.strip()
                if not (line.startswith("{") and line.endswith("}")):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("imported_by"), list):
                    union |= set(data["imported_by"])
                    parsed_any = True
            if parsed_any:
                returned = union
            else:
                try:
                    data = json.loads(skill_result_text)
                    if "imported_by" in data:
                        returned = set(data["imported_by"])
                except (json.JSONDecodeError, AttributeError, TypeError):
                    # Rendered markdown path — extract lines that look like dotted module paths.
                    modules = re.findall(r"^([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+)\s*$", skill_result_text, re.MULTILINE)
                    if modules:
                        returned = set(modules)
            if returned is not None:
                skill_returned = len(returned)
                skill_coverage = len(returned & exp) / n_exp

        # ── Semble chunk-hit rate (semble / combined arm only) ──
        # Module-file granularity: an expected rdep counts as hit if any of its surface forms
        # appears in the concatenated semble search chunks — semantic search need not enumerate
        # exact dotted rdeps to get credit.
        chunk_hit_rate: Optional[float] = None
        if semble_result_text:
            chunk_hits = sum(1 for r in exp if self._rdep_found(r, semble_result_text))
            chunk_hit_rate = chunk_hits / n_exp

        # ── Legacy: leaf-name matching on output_text ──
        expected_leaves = {m.split(".")[-1] for m in exp}
        ambiguous = sum(1 for leaf in expected_leaves if len(leaf) < 6)
        matched_leaves = {
            lf for lf in expected_leaves if re.search(r"\b" + re.escape(lf) + r"\b", output_text, re.IGNORECASE)
        }
        leaf_tp = len(matched_leaves)
        leaf_fn = len(expected_leaves) - leaf_tp
        leaf_recall = leaf_tp / len(expected_leaves) if expected_leaves else 0.0
        all_output_leaves = {
            lf for lf in self.all_leaf_names if re.search(r"\b" + re.escape(lf) + r"\b", output_text, re.IGNORECASE)
        }
        leaf_fp = len(all_output_leaves - expected_leaves)
        prec = leaf_tp / (leaf_tp + leaf_fp) if (leaf_tp + leaf_fp) > 0 else 0.0
        f1 = 2 * prec * leaf_recall / (prec + leaf_recall) if (prec + leaf_recall) > 0 else 0.0

        return QualityScore(
            scored=True,
            # v2 primary
            erec=erec,
            erec_tp=erec_tp,
            erec_fn=n_exp - erec_tp,
            rrec=rrec,
            rrec_tp=rrec_tp,
            rrec_fn=n_exp - rrec_tp,
            delta=delta,
            deff=deff,
            erec_top10=erec_top10,
            erec_top10_k=erec_top10_k,
            # Skill coverage
            skill_coverage=skill_coverage,
            skill_returned=skill_returned,
            # Semble-native lens
            chunk_hit_rate=chunk_hit_rate,
            # Legacy
            precision=prec,
            recall=leaf_recall,
            f1=f1,
            tp=leaf_tp,
            fp=leaf_fp,
            fn=leaf_fn,
            leaf_recall=leaf_recall,
            leaf_tp=leaf_tp,
            leaf_fn=leaf_fn,
            ambiguous_leaves=ambiguous,
        )

    def _extract_modules(self, text: str) -> set[str]:
        """Extract dotted package-namespaced module names from agent output.

        Matches only the repo's own top-level packages (``self.packages``, derived from the
        tasks' primary modules), so a non-lightning repo works without editing code.

        Handles two forms agents use:
        - Dotted: ``lightning.pytorch.trainer.trainer``
        - File path: ``src/lightning/pytorch/trainer/trainer.py`` -> converted to dotted
        """
        dotted = set(self._module_re.findall(text))
        from_paths = {m.replace("/", ".") for m in self._path_re.findall(text)}
        return dotted | from_paths


# ---------------------------------------------------------------------------
# Claude CLI runner
# ---------------------------------------------------------------------------


def _benchmark_evidence_roots(extra_roots: Sequence[Path] = ()) -> tuple[Path, ...]:
    """Return existing benchmark evidence roots that a Claude cell must not read.

    The parent launcher supplies the original evaluator checkout and result root through
    ``BENCHMARK_EVIDENCE_ROOTS``. Direct runner use falls back to this checkout, which
    contains the frozen runner, shared scorer, and prior result history.
    """
    encoded_roots = os.environ.get("BENCHMARK_EVIDENCE_ROOTS")
    if encoded_roots is None:
        raw_roots: list[object] = [str(Path(__file__).resolve().parents[1])]
    else:
        try:
            raw_roots = json.loads(encoded_roots)
        except json.JSONDecodeError as exc:
            raise ValueError("BENCHMARK_EVIDENCE_ROOTS must be a JSON list of absolute paths") from exc
        if not isinstance(raw_roots, list) or not raw_roots:
            raise ValueError("BENCHMARK_EVIDENCE_ROOTS must name at least one evidence root")

    roots: list[Path] = []
    for raw_path in [*raw_roots, *(str(path) for path in extra_roots)]:
        if not isinstance(raw_path, str):
            raise ValueError("benchmark evidence roots must be strings")
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            raise ValueError(f"benchmark evidence root must be absolute: {raw_path!r}")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"benchmark evidence root is unavailable: {candidate}") from exc
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _absolute_filetool_pattern(path: Path) -> str:
    """Render one absolute Claude file-tool pattern for a directory and descendants."""
    rendered = path.as_posix().lstrip("/")
    if not rendered:
        raise ValueError("benchmark evidence root cannot be the filesystem root")
    return f"//{rendered}/**"


def _claude_evidence_isolation_settings(
    cwd: Path,
    *,
    evidence_roots: Sequence[Path],
    runtime_paths: Sequence[Path],
) -> dict[str, object]:
    """Build Claude settings that deny benchmark evidence to file tools and sandboxed Bash.

    Claude ``Read`` denies block built-in file tools, including Grep and Glob. Its native sandbox applies ``denyRead``
    to Bash and every Bash child process; strict mode prevents the documented unsandboxed retry from reopening those
    paths.
    """
    try:
        worktree = cwd.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Claude worktree is unavailable for evidence isolation: {cwd}") from exc
    allowed_paths = [worktree]
    for runtime_path in runtime_paths:
        try:
            resolved = runtime_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Claude assigned runtime is unavailable: {runtime_path}") from exc
        if resolved not in allowed_paths:
            allowed_paths.append(resolved)

    denied_paths: list[Path] = []
    for root in evidence_roots:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Claude evidence root is unavailable: {root}") from exc
        if resolved == Path(resolved.anchor):
            raise ValueError("Claude evidence root cannot be the filesystem root")
        if resolved not in denied_paths:
            denied_paths.append(resolved)
    if not denied_paths:
        raise ValueError("Claude evidence isolation requires at least one denied root")
    for allowed_path in allowed_paths:
        for denied_path in denied_paths:
            if allowed_path == denied_path or allowed_path.is_relative_to(denied_path):
                raise ValueError("Claude worktree or assigned runtime cannot be inside a denied evidence root")
            if denied_path.is_relative_to(allowed_path):
                raise ValueError("Claude evidence root cannot be inside an allowed worktree or runtime path")

    filetool_denies = [f"Read({_absolute_filetool_pattern(root)})" for root in denied_paths]
    return {
        "permissions": {"deny": filetool_denies},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "disabled": False,
                "denyRead": [str(path) for path in denied_paths],
                "allowRead": [str(path) for path in allowed_paths],
            },
        },
    }


@contextlib.contextmanager
def _claude_evidence_settings_file(
    cwd: Path,
    *,
    evidence_roots: Sequence[Path],
    runtime_paths: Sequence[Path],
) -> Iterator[Path]:
    """Write one short-lived Claude settings file for a single isolated cell."""
    descriptor, name = tempfile.mkstemp(prefix="claude-benchmark-isolation-", suffix=".json")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                _claude_evidence_isolation_settings(
                    cwd,
                    evidence_roots=evidence_roots,
                    runtime_paths=runtime_paths,
                ),
                handle,
                sort_keys=True,
            )
            handle.write("\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


@contextlib.contextmanager
def _staged_codemap_runtime(arm: str, cwd: Path) -> Iterator[Path | None]:
    """Copy a structural-arm runtime outside evaluator evidence before Claude starts.

    The checked-out plugin is benchmark evidence because it lives under the evaluator checkout. Claude must load a
    disposable runtime copy instead of depending on a nested sandbox allow-list exception that conflicts with the
    evidence deny.
    """
    if arm not in ("codemap", "combined", "B_auto", "C_strict"):
        yield None
        return
    source_text = ModelRunner._codemap_plugin_dir()
    if source_text is None:
        raise RuntimeError(f"Codemap plugin fixture is required for {arm} but is unavailable")
    source = Path(source_text).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="claude-codemap-runtime-", dir=cwd.parent) as runtime_dir:
        runtime = Path(runtime_dir) / "codemap-py"
        shutil.copytree(
            source,
            runtime,
            symlinks=True,
            ignore=shutil.ignore_patterns(".cache", ".plans", ".reports", ".pytest_cache", "__pycache__", "tests"),
        )
        yield runtime


class ModelRunner:
    """Runs benchmark tasks against a specific Claude model tier.

    Encapsulates model identity, repo path, and timeout. The ``run()`` method launches a Claude subprocess and parses
    stream-json events into a ``BenchmarkRun`` result.

    Arm prompts and CLI constants live here — only ModelRunner constructs or launches claude; nothing else needs them.
    """

    # Base claude CLI invocation.
    # ``--setting-sources project,local`` excludes USER-level config (caveman plugin, foundry Re:Anchor
    # box+▓ footer, user CLAUDE.md, user hooks) so the agent's output is not shaped/inflated by the
    # operator's personal setup — identical isolation on every arm. Excluding user also drops the
    # codemap plugin and semble MCP, so they are re-supplied per arm via ``--plugin-dir`` and ``--mcp-config``
    # in _arm_isolation_flags (the tools under test must survive isolation). Subscription auth is
    # not a setting source, so it is unaffected.
    # ``--no-session-persistence`` makes every cell non-resumable, preventing conversational state reuse.
    _CMD = [
        "claude",
        "-p",
        "--no-session-persistence",
        "--verbose",
        "--output-format",
        "stream-json",
        "--setting-sources",
        "project,local",
    ]
    # Tools counted as exploration overhead
    EXPLORATION_TOOLS = {"Grep", "Glob", "Bash", "Skill", "mcp__semble__search", "mcp__semble__find_related"}
    # Tools blocked per arm via ``--disallowed-tools`` to enforce mutual exclusion
    # Bash is kept available for every non-plain arm (and plain) so each has the same read-only
    # shell fallback on a primary-tool error; blocking it for semble alone was an asymmetric
    # handicap. Only the primary discriminator differs: codemap blocks semble MCP,
    # semble blocks the Skill tool, plain blocks both structural entry points.
    _ARM_DISALLOWED: dict[str, list[str]] = {
        "codemap": ["--disallowed-tools", "mcp__semble__search,mcp__semble__find_related"],
        "B_auto": ["--disallowed-tools", "Agent,Task,mcp__semble__search,mcp__semble__find_related"],
        "C_strict": ["--disallowed-tools", "Agent,Task,mcp__semble__search,mcp__semble__find_related"],
        "semble": ["--disallowed-tools", "Skill"],
        "plain": ["--disallowed-tools", "Skill,mcp__semble__search,mcp__semble__find_related"],
        "A_plain": [
            "--disallowed-tools",
            "Skill,Agent,Task,mcp__semble__search,mcp__semble__find_related,Bash(scan-query:*)",
        ],
        "combined": [],
    }

    # Tools pre-approved per arm via ``--allowedTools``. In headless -p mode a tool the arm relies
    # on MUST be pre-approved here or it is permission-denied (returns <tool_use_error>). Canonical
    # P1 C treatment evidence is a completed direct ``codemap-py query`` Bash call. Match the
    # production Skill's absolute-launcher form (including its closing quote) as well as the PATH
    # form; a successful Skill wrapper alone does not prove its nested call used the frozen index.
    _CODEMAP_SKILLS = "Skill(codemap:query-code),Skill(codemap-py:query-code)"
    _ARM_ALLOWED: dict[str, list[str]] = {
        "codemap": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),{_CODEMAP_SKILLS}",
        ],
        "B_auto": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),{_CODEMAP_SKILLS}",
        ],
        "C_strict": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),{_CODEMAP_SKILLS}",
        ],
        "semble": ["--allowedTools", "mcp__semble__search,mcp__semble__find_related"],
        "combined": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),mcp__semble__search,mcp__semble__find_related,{_CODEMAP_SKILLS}",
        ],
    }

    # Arm system prompts -------------------------------------------------------
    # PLAIN arm:   minimal fix/feature/refactor/review skill, no codemap.
    # CODEMAP arm: same skill + /codemap:query instruction.
    _PLAIN_SKILLS: dict[str, str] = {
        "fix": (
            "You are a software engineer fixing a bug in a Python codebase. "
            "Before writing any fix, investigate the affected module: understand what "
            "other modules depend on it and what it depends on, so you know the full "
            "blast radius of any interface change."
        ),
        "feature": (
            "You are a software engineer adding a new feature to a Python codebase. "
            "Before writing any code, explore the relevant modules to identify "
            "integration points, coupling risks, and which files you will need to modify."
        ),
        "refactor": (
            "You are a software engineer refactoring a Python codebase. "
            "Before changing anything, map out every module that imports the code "
            "being restructured so you understand the full scope of the change."
        ),
        "review": (
            "You are a software engineer reviewing a code change in a Python codebase. "
            "Identify all modules that depend on the changed code, assess the blast "
            "radius, and flag the highest regression risks."
        ),
    }
    # Shared efficiency sentence — identical for every arm so tool-call count reflects the
    # agent's own choices, not asymmetric steering.
    _EFFICIENCY = "\n\nAnswer in as few tool calls as possible; do not re-verify results you already have."

    # Shared, arm-neutral answer format. This is the erec/rrec extraction target and MUST be
    # identical across all four arms — any per-arm wording here would bias the measured signal.
    # Placeholders are generic (no hardcoded corpus paths) so no arm is primed with example modules.
    _ANSWER_FORMAT = """

## Required answer format

Your final answer MUST end with this section:

## Reverse Dependencies Found

Count: <N> distinct modules found.

- <full.dotted.module.path>
- <full.dotted.module.path>
- ... (one line per module)

Rules:
- Write "Count: N distinct modules found." where N = the exact number in your list
- Full dotted paths only — no shortened names, no file paths, no aliases
- List every module you found — no omissions
- If nothing found: write "Count: 0" and "(none found)"
- This section must be the LAST thing in your answer"""

    # Per-arm supplements below carry tool availability + invocation syntax ONLY. No call caps,
    # no "do not verify" rules, no step protocols — that steering is the measured signal and would
    # manufacture the benchmark savings it claims to observe.
    _PLAIN_SUPPLEMENT = """

## Tools available

Grep, Glob, Bash, and Read are available for exploring the codebase and its import graph."""

    _CODEMAP_SUPPLEMENT = """

## Tools available

You have the /codemap:query-code skill (via the Skill tool). It answers import-graph questions
from a pre-built structural index.

Syntax — colon separator, never a space:
  codemap:query-code      (correct)
  codemap query-code      (wrong — fails silently)

Invocation:
  /codemap:query-code rdeps <primary_module> [--exclude-tests]

Grep, Glob, Bash, and Read remain available.

If /codemap:query-code returns <tool_use_error>, run one Grep/Bash fallback for the same query."""

    _C_STRICT_SUPPLEMENT = (
        "\n\nYou must use Codemap at least once for structural investigation. When the task supplies an exact "
        "`/codemap-py:query-code` invocation, load that Skill and complete at least one standalone successful "
        "`codemap-py query --compact` before using other source tools; do not prefix, assign, wrap, or combine the "
        "credited query with shell work; loading the Skill alone, or a query without `--compact`, does not satisfy "
        "the requirement."
    )

    _SEMBLE_SUPPLEMENT = """

## Tools available

You have the mcp__semble__search and mcp__semble__find_related tools. They perform hybrid
semantic + lexical search across the codebase and return ranked code chunks with file path
and line range.

Parameters:
  query (str)   — natural language or code query
  repo  (str)   — REQUIRED: absolute path to the repository: {repo_path}
  top_k (int)   — number of results (default 5; raise it for broader coverage)

Grep, Glob, Bash, and Read are available for reading source code; the Skill tool is not.

If mcp__semble__search returns <tool_use_error>, run one Grep/Bash fallback for the same query."""

    _COMBINED_SUPPLEMENT = """

## Tools available

You have both /codemap:query-code (Skill tool, deterministic index) and mcp__semble__search /
mcp__semble__find_related (semantic search). Choose whichever fits each question.

codemap syntax — colon separator, never a space:
  /codemap:query-code rdeps <primary_module> [--exclude-tests]

semble parameters:
  query (str), repo (str, REQUIRED: {repo_path}), top_k (int)

Grep, Glob, Bash, and Read remain available.

If a structural tool returns <tool_use_error>, run one Grep/Bash fallback for the same query."""

    # read_crop task family — measures READ cost: extract ONE symbol's contract using the
    # fewest tokens of file content. Headline metric is tool_result_tokens (codemap `symbol`
    # extraction vs plain whole-file Read); correctness via keyword recall (score_read_crop).
    _READCROP_BASE = (
        "You are a software engineer answering a precise question about ONE symbol "
        "(function / method / class) in a Python codebase. Find that symbol's source, then state "
        "its full contract — every parameter and what it does. Use the FEWEST tokens of file "
        "content possible: do NOT read unrelated code, and do NOT read an entire large module file "
        "when you only need one symbol."
    )
    _READCROP_PLAIN = (
        "\n\n## Reading tools\n"
        "Use Grep to locate the symbol's definition line, then Read ONLY the needed line range "
        "(pass offset/limit) — never read the whole file if the symbol is a small part of it."
    )
    _READCROP_CODEMAP = (
        "\n\n## Codemap integration\n"
        "The installed `/codemap-py:query-code` Skill can extract one symbol with its imports. Invoke the Skill when "
        "the treatment or unresolved structural question requires it, then follow its current `codemap-py query` "
        "syntax. Its completed symbol source is authoritative structural evidence; do not read an entire module "
        "when that result is complete."
    )
    _READCROP_SEMBLE = (
        "\n\n## semble installed — search then read the chunk\n"
        'Call mcp__semble__search with query naming the symbol and repo="{repo_path}", top_k=5. '
        "Read only the returned chunk's line range — do not read the whole file."
    )

    # fix_single task family — single-function / single-file bug fix; scored by diff keyword recall.
    _FIXSINGLE_BASE = (
        "You are a software engineer fixing a specific bug in a Python codebase. "
        "Read the relevant source file(s), understand the described issue, then apply the "
        "**minimal fix** using the Edit tool. Do not refactor unrelated code. "
        "The fix should be complete and correct — the scorer checks the diff for expected change markers."
    )
    _FIXSINGLE_PLAIN = (
        "\n\n## Tools\n"
        "Use Grep to locate the relevant class/function, then Read only the needed lines. "
        "Apply the fix with Edit."
    )
    _FIXSINGLE_CODEMAP = (
        "\n\n## Codemap integration\n"
        "The installed `/codemap-py:query-code` Skill can extract a target symbol without reading the whole file. "
        "Invoke it when the treatment or an unresolved structural question requires it, then follow its current "
        "`codemap-py query` syntax. For automatic use, retain the Skill's localized-edit skip rule."
    )
    _FIXSINGLE_SEMBLE = (
        "\n\n## semble installed\n"
        'Call mcp__semble__search with the symbol name and repo="{repo_path}" to locate the '
        "relevant code, then apply the fix with Edit."
    )

    # fix_multicaller task family — signature change that requires updating multiple callers;
    # scored by diff keyword recall + file recall. Codemap's rdeps is the decisive tool here.
    _FIXMULTI_BASE = (
        "You are a software engineer making a signature change that touches multiple call sites. "
        "Before writing any code: **find ALL callers of the function being changed**. "
        "Then edit the function definition AND every caller. Miss a caller = incomplete fix."
    )
    _FIXMULTI_PLAIN = (
        "\n\n## Tools\n"
        "Use grep/bash to find all callers of the function (search for the function name as a string). "
        "Edit the definition first, then each caller."
    )
    # Tool availability + syntax ONLY — no "do this FIRST", no "do NOT grep", no "decisive
    # advantage" framing. That steering was the measured signal and manufactured the tool-call
    # gap, mirroring the corresponding rdep-supplement treatment.
    _FIXMULTI_CODEMAP = (
        "\n\n## Codemap integration\n"
        "The installed `/codemap-py:query-code` Skill answers caller and import-graph questions from a pre-built "
        "structural index. Invoke the Skill when the treatment or unresolved affected-surface question requires it, "
        "then follow its current `codemap-py query` syntax. Grep, Glob, Bash, and Read remain available for distinct "
        "source/runtime facts."
    )
    _FIXMULTI_SEMBLE = (
        "\n\n## semble installed\n"
        'Call mcp__semble__search with the function name and repo="{repo_path}" to locate callers, '
        "then edit the definition and all callers."
    )

    def _system_prompt(self, task_type: str, arm: str) -> str:
        """Build the system prompt for one arm × task-type combination.

        The shared ``_EFFICIENCY`` sentence is appended for every task family (fix, read_crop, and rdep) so no arm is
        uniquely nudged toward more or fewer tool calls. Canonical parity tasks put their exact labelled JSON answer
        contract in the user prompt; legacy tasks retain their historical reverse-dependency output format.
        """
        if task_type == "fix_single":
            supplement = {
                "codemap": self._FIXSINGLE_CODEMAP,
                "B_auto": self._FIXSINGLE_CODEMAP,
                "C_strict": self._FIXSINGLE_CODEMAP + self._C_STRICT_SUPPLEMENT,
                "semble": self._FIXSINGLE_SEMBLE.format(repo_path=self.repo_path),
            }.get(arm, self._FIXSINGLE_PLAIN)
            return self._FIXSINGLE_BASE + supplement + self._EFFICIENCY
        if task_type == "fix_multicaller":
            supplement = {
                "codemap": self._FIXMULTI_CODEMAP,
                "B_auto": self._FIXMULTI_CODEMAP,
                "C_strict": self._FIXMULTI_CODEMAP + self._C_STRICT_SUPPLEMENT,
                "semble": self._FIXMULTI_SEMBLE.format(repo_path=self.repo_path),
            }.get(arm, self._FIXMULTI_PLAIN)
            return self._FIXMULTI_BASE + supplement + self._EFFICIENCY
        if task_type == "read_crop":
            supplement = {
                "codemap": self._READCROP_CODEMAP,
                "B_auto": self._READCROP_CODEMAP,
                "C_strict": self._READCROP_CODEMAP + self._C_STRICT_SUPPLEMENT,
                "semble": self._READCROP_SEMBLE.format(repo_path=self.repo_path),
            }.get(arm, self._READCROP_PLAIN)
            return self._READCROP_BASE + supplement + self._EFFICIENCY
        base = self._PLAIN_SKILLS.get(task_type, self._PLAIN_SKILLS["fix"])
        if arm in ("codemap", "B_auto"):
            supplement = self._CODEMAP_SUPPLEMENT
        elif arm == "C_strict":
            supplement = self._CODEMAP_SUPPLEMENT + self._C_STRICT_SUPPLEMENT
        elif arm == "semble":
            supplement = self._SEMBLE_SUPPLEMENT.format(repo_path=self.repo_path)
        elif arm == "combined":
            supplement = self._COMBINED_SUPPLEMENT.format(repo_path=self.repo_path)
        else:
            supplement = self._PLAIN_SUPPLEMENT
        # Canonical tasks carry their exact JSON contract in the user prompt. Keeping the legacy
        # format out of that path avoids contradictory output instructions while preserving it for
        # explicitly selected historical arms.
        answer_format = "" if parity_arm_identity(arm) else self._ANSWER_FORMAT
        return base + self._EFFICIENCY + supplement + answer_format

    def __init__(
        self,
        model_short: str,
        model_id: str,
        repo_path: Path,
        timeout: int = 300,
    ) -> None:
        self.model_short = model_short
        self.model_id = model_id
        self.repo_path = repo_path
        self.timeout = timeout

    def run_stage_events(
        self,
        *,
        prompt: str,
        system_prompt: str,
        arm: str,
        cwd: Path,
        writable: bool = False,
        evidence_roots: Sequence[Path] = (),
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        """Run one canonical stage prompt through the shared Claude transport.

        This method is the provider seam used by ReadCrop, Fix-Single, and
        Fix-Multi. It constructs Claude's isolated arm invocation, while
        ``stream_claude`` remains the sole subprocess/event loop. Raw events are
        returned losslessly for stage-owned normalization and artifact capture.

        Args:
            prompt: Exact user task and answer/edit contract.
            system_prompt: Arm-neutral role and execution constraints.
            arm: Canonical ``A_plain``, ``B_auto``, or ``C_strict`` treatment.
            cwd: Benchmark-owned disposable repository visible to Claude.
            writable: Use native edit-accepting mode for an isolated executable
                worktree; leave read-only studies in the default mode.
            evidence_roots: Additional live result roots that must remain hidden
                from this cell alongside the evaluator checkout.

        Returns:
            Raw events, elapsed seconds, and a bounded transport error or
            ``None`` after a normal provider exit.
        """
        permission_flags = ["--permission-mode", "acceptEdits"] if writable else []
        preamble_flag = Path(tempfile.gettempdir()) / f"codemap-preamble-{cwd.name}"
        preamble_flag.unlink(missing_ok=True)
        events: list[dict[str, Any]] = []
        denied_evidence = _benchmark_evidence_roots(evidence_roots)
        with _staged_codemap_runtime(arm, cwd) as runtime:
            runtime_paths = [runtime] if runtime is not None else []
            with _claude_evidence_settings_file(
                cwd,
                evidence_roots=denied_evidence,
                runtime_paths=runtime_paths,
            ) as settings_path:
                cmd = [
                    *self._CMD,
                    "--model",
                    self.model_id,
                    *permission_flags,
                    "--settings",
                    str(settings_path),
                    *self._arm_isolation_flags(arm, codemap_plugin_dir=runtime),
                    *self._ARM_DISALLOWED.get(arm, []),
                    *self._ARM_ALLOWED.get(arm, []),
                    "--system-prompt",
                    system_prompt,
                    prompt,
                ]
                outcome = stream_claude(
                    cmd,
                    timeout=self.timeout,
                    cwd=cwd,
                    env=self._subprocess_env(arm, codemap_plugin_dir=runtime),
                    on_event=lambda event, _timestamp: events.append(dict(event)),
                )
        error = outcome.error or (outcome.stderr.strip()[:300] if outcome.stderr and outcome.returncode else None)
        if outcome.exc_timeout or (outcome.returncode is not None and outcome.returncode < 0):
            error = error or f"timeout ({self.timeout}s)"
        return events, outcome.elapsed_s, error

    @contextlib.contextmanager
    def _effective_cwd(
        self,
        task: Task,
        arm: str,
        diff_capture: list[str],
        test_capture: list[Optional[bool]],
        index_relocations: list[dict[str, str]] | None = None,
    ) -> Iterator[Path]:
        """Yield an isolated sandbox copy of the repo for one run, capturing its aftermath.

        Args:
            task: The task about to run; ``requires_reset`` selects the fix-lane behaviour
                (post-run diff capture and optional targeted test).
            arm: Benchmark arm; the index-consuming arms get the prebuilt cache seeded.
            diff_capture: Accumulator the post-run ``diff -ru`` output is appended to
                (fix-lane tasks only).
            test_capture: Accumulator the targeted-test verdict is appended to when the task
                declares a ``test_target``.
            index_relocations: Optional accumulator for derived index provenance.

        Yields:
            Path of the sandbox repo copy, removed when the block exits.
        """
        # EVERY arm runs in an isolated copy of the repo so agent edits can never mutate
        # self.repo_path. Blocking Edit/Write is not enough: the codemap and combined arms keep
        # Bash, so an agent could still write through the shell — only a throwaway copy bounds
        # the blast radius. Query (non-reset) arms previously ran in-place, letting a stray edit
        # contaminate later runs; they now copy like every other arm.
        import shutil
        import tempfile

        prefix = "bench-fix-" if task.requires_reset else "bench-copy-"
        with tempfile.TemporaryDirectory(prefix=prefix) as tmpdir:
            tmp = Path(tmpdir)
            cwd = tmp / self.repo_path.name
            shutil.copytree(
                self.repo_path,
                cwd,
                ignore=shutil.ignore_patterns(".cache", ".git"),
                symlinks=True,
            )
            # codemap / combined arms need the prebuilt index present in the sandbox;
            # otherwise the /codemap:query-code Step 0 would build it inside the measured
            # window. The sandbox dir keeps the original repo name, so the
            # repo-name-derived index file (<repo>.json) resolves unchanged (git is absent →
            # resolve_proj_index falls back to the CWD basename). Plain and semble arms are
            # left index-free — plain for isolation, semble because it never queries the index.
            if arm in ("codemap", "combined", "B_auto", "C_strict"):
                relocated = self._seed_index_cache(cwd)
                if index_relocations is not None:
                    index_relocations.extend(relocated)
            yield cwd
            if task.requires_reset:
                import subprocess as _sp

                # The excludes mirror the copytree ignore list above. Without them the diff
                # reports every top-level entry the sandbox never received (.git) and every
                # cache tree the sandbox was seeded with selectively (.cache) as a
                # difference — artifact bloat at best, and a spurious `+` line in fix
                # scoring the moment anything writes under .cache during the run.
                proc = _sp.run(
                    [
                        "diff",
                        "-ru",
                        "--no-dereference",
                        "--exclude=.git",
                        "--exclude=.cache",
                        str(self.repo_path),
                        str(cwd),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                diff_capture.append(proc.stdout)
                # Opt-in correctness signal: run the task's declared pytest node on the sandbox
                # (post-edit, pre-cleanup) so a semantically wrong edit that merely emits the
                # right keywords does not score full recall unchecked.
                if task.test_target:
                    test_capture.append(self._run_targeted_test(cwd, task.test_target))

    def run(
        self,
        task: Task,
        arm: str,
        update_fn: Optional[Callable[[float, "BenchmarkRun"], None]] = None,
    ) -> BenchmarkRun:
        """Run one task in one arm and return the parsed metrics.

        Launches a ``claude`` subprocess in stream-json mode and delegates event parsing
        to ``_stream_events``. Retries up to two times when the API returns zero tokens
        (connectivity failure). On the third failure the result is returned as-is.

        Args:
            task: The task to execute.
            arm: Benchmark arm identifier (``plain``, ``codemap``, ``semble``, or
                ``combined``). Controls which tools are allowed/disallowed and which
                system prompt supplement is injected.
            update_fn: Optional live-progress callback invoked (at most every 0.5 s)
                with ``(elapsed_seconds, partial_result)``. Used by the rich Progress bar
                in ``Benchmark.run()``. Pass ``None`` to disable.

        Returns:
            Populated ``BenchmarkRun`` with tool counts, token metrics, timing, and
            raw output text ready for quality scoring.
        """
        system_prompt = self._system_prompt(task.skill or task.type, arm)
        disallow_flags = self._ARM_DISALLOWED.get(arm, [])
        allow_flags = self._ARM_ALLOWED.get(arm, [])
        # Codex has no equivalent public turn cap, so canonical parity arms use only the shared
        # wall-clock budget. Legacy agentic labels keep their original fixed 40-turn control.
        turn_flags = [] if parity_arm_identity(arm) else ["--max-turns", "40"]
        _diff_capture: list[str] = []
        _test_capture: list[Optional[bool]] = []

        _MAX_API_RETRIES = 2
        denied_evidence = _benchmark_evidence_roots()
        for attempt in range(_MAX_API_RETRIES + 1):
            # Every attempt gets its own sandbox. Reusing one copy across retries let a
            # failed attempt's edits — and any file it created — survive into the next
            # one, so a retry no longer started from the task's baseline tree and its
            # captured diff mixed both attempts' work.
            _diff_capture = []
            _test_capture = []
            index_relocations: list[dict[str, str]] = []
            with self._effective_cwd(task, arm, _diff_capture, _test_capture, index_relocations) as cwd:
                # Each benchmark task is an independent agent session. Clear the
                # inject-preamble session-once flag so each task receives the
                # codemap status line regardless of inter-task timing.
                _flag = Path(tempfile.gettempdir()) / f"codemap-preamble-{cwd.name}"
                _flag.unlink(missing_ok=True)

                with _staged_codemap_runtime(arm, cwd) as runtime:
                    runtime_paths = [runtime] if runtime is not None else []
                    with _claude_evidence_settings_file(
                        cwd,
                        evidence_roots=denied_evidence,
                        runtime_paths=runtime_paths,
                    ) as settings_path:
                        cmd = [
                            *self._CMD,
                            *turn_flags,
                            "--model",
                            self.model_id,
                            "--settings",
                            str(settings_path),
                            *self._arm_isolation_flags(arm, codemap_plugin_dir=runtime),
                            *disallow_flags,
                            *allow_flags,
                            "--system-prompt",
                            system_prompt,
                            task.prompt,
                        ]
                        result = BenchmarkRun(
                            arm=arm,
                            task_id=task.id,
                            task_type=task.type,
                            model=self.model_short,
                            success=False,
                            index_relocations=index_relocations,
                        )
                        self._stream_events(cmd, result, update_fn=update_fn, cwd=cwd, arm=arm)
                        evidence = _claude_codemap_evidence(result.raw_events)
                        result.codemap_query_attempted = int(evidence["codemap_query_attempted"])
                        result.codemap_query_succeeded = int(evidence["codemap_query_succeeded"])
                        result.codemap_compact_success = bool(evidence["codemap_compact_success"])
                # 0-token result = API connectivity failure (ConnectionRefused / FailedToOpenSocket);
                # retry up to 2 times before surfacing as error. A wall-clock kill also leaves
                # 0 tokens (the killed process never emits its `result` event) but has already
                # burned a full timeout of paid model work — never retry it.
                timed_out = result.elapsed_s >= self.timeout or (result.error or "").startswith("timeout")
                retrying = (
                    result.input_tokens == 0
                    and result.output_tokens == 0
                    and not timed_out
                    and attempt < _MAX_API_RETRIES
                )
            # The sandbox is torn down before the backoff so a retry never waits with the
            # previous attempt's copy still on disk.
            if retrying:
                result.error = f"api_failure_retry_{attempt + 1}"
                time.sleep(2**attempt)  # exponential backoff: 1s, 2s
                continue
            break
        if _diff_capture:
            result.agent_diff = _diff_capture[0]
        if _test_capture:
            result.targeted_test_passed = _test_capture[0]
        return result

    def _run_targeted_test(self, cwd: Path, test_target: str) -> Optional[bool]:
        """Run a task's declared pytest target on the post-edit sandbox and report pass/fail.

        Args:
            cwd: Sandbox repository root containing the agent's applied edits.
            test_target: pytest node id or path (e.g. ``tests/foo/test_bar.py::test_case``).

        Returns:
            ``True`` when pytest exits 0, ``False`` on any non-zero exit, and ``None`` when pytest
            could not be launched at all (missing binary / environment error) so a launch failure
            is never miscredited as a genuine test failure.
        """
        import subprocess as _sp

        try:
            proc = _sp.run(
                [sys.executable, "-m", "pytest", test_target, "-q", "-p", "no:cacheprovider"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, _sp.SubprocessError):
            return None
        return proc.returncode == 0

    def _seed_index_cache(self, cwd: Path) -> list[dict[str, str]]:
        """Copy prebuilt caches and relocate index roots to the disposable repository.

        Only ``.cache/codemap`` and ``.cache/scan`` are copied from the original repo (never the
        whole ``.cache``), so the structural index is present in the sandbox without dragging in
        unrelated cache trees. Missing source dirs are skipped silently.

        Args:
            cwd: Sandbox repository root the index should be seeded into.

        Returns:
            Original and derived hashes for each root-relocated index. Graph facts remain unchanged.
        """
        import shutil

        relocations: list[dict[str, str]] = []
        for sub in ("codemap", "scan"):
            source = self.repo_path / ".cache" / sub
            if source.is_dir():
                destination = cwd / ".cache" / sub
                shutil.copytree(source, destination, symlinks=True)
                for index_path in sorted(destination.glob("*.json")):
                    original = index_path.read_bytes()
                    payload = json.loads(original)
                    # Auxiliary cache metadata is not an index. Only relocate declared roots;
                    # the existing helper rejects a source-root mismatch without changing facts.
                    if not isinstance(payload, dict) or "scan_root" not in payload:
                        continue
                    derived, provenance = relocate_frozen_index_for_worktree(
                        original, source_root=self.repo_path, worktree_root=cwd
                    )
                    if index_path.is_symlink():
                        index_path.unlink()
                    index_path.write_bytes(derived)
                    relocations.append({**provenance, "index_path": index_path.relative_to(cwd).as_posix()})
        return relocations

    # Semble MCP definition, re-supplied under isolation (excluding user config drops the user's
    # semble server). Mirrors `claude mcp get semble` — a local stdio server, no auth/env.
    _SEMBLE_MCP: dict = {"mcpServers": {"semble": {"command": "uvx", "args": ["--from", "semble[mcp]", "semble"]}}}

    @staticmethod
    def _codemap_plugin_dir() -> Optional[str]:
        """Return the repository Codemap fixture, or None when it is incomplete.

        The benchmark must not inherit a mutable user plugin cache. The checked-out plugin is the
        deterministic fixture used by CI and local runs; a missing fixture is reported to the
        canonical arm instead of being treated as a valid no-plugin setup.

        Returns:
            Absolute path to the plugin root (the dir holding .claude-plugin/), or None.
        """
        plugin_root = Path(__file__).resolve().parents[1] / "plugins" / "codemap-py"
        required_files = (
            plugin_root / ".claude-plugin" / "plugin.json",
            plugin_root / "claude-skills" / "query-code" / "SKILL.md",
        )
        if all(path.is_file() for path in required_files):
            return str(plugin_root)
        return None

    @classmethod
    def _semble_mcp_config_path(cls) -> str:
        """Write the reconstructed semble MCP config once and return its path.

        Returns:
            Path to a JSON file suitable for ``--mcp-config`` describing the semble stdio server.
        """
        import tempfile

        path = Path(tempfile.gettempdir()) / "codemap-bench-semble-mcp.json"
        if not path.exists():
            path.write_text(json.dumps(cls._SEMBLE_MCP))
        return str(path)

    @classmethod
    def _arm_isolation_flags(cls, arm: str, *, codemap_plugin_dir: Path | None = None) -> list[str]:
        """Return the per-arm flags that re-supply the tools under test after user config is excluded.

        codemap/combined get the codemap plugin (``--plugin-dir``) for the Skill; semble/combined get the
        semble server (``--mcp-config``, ``--strict-mcp-config``). plain gets nothing — it is the control.

        Args:
            arm: Benchmark arm (plain / codemap / semble / combined).

        Returns:
            Flag list to splice into the claude command for *arm*.
        """
        flags: list[str] = []
        plugin_dir = codemap_plugin_dir or cls._codemap_plugin_dir()
        if arm in ("codemap", "combined", "B_auto", "C_strict") and plugin_dir is None:
            raise RuntimeError(f"Codemap plugin fixture is required for {arm} but is unavailable")
        if arm in ("codemap", "combined", "B_auto", "C_strict"):
            flags += ["--plugin-dir", str(plugin_dir)]
        if arm in ("semble", "combined"):
            flags += ["--mcp-config", cls._semble_mcp_config_path(), "--strict-mcp-config"]
        return flags

    @classmethod
    def _subprocess_env(cls, arm: str = "", *, codemap_plugin_dir: Path | None = None) -> dict[str, str]:
        """Return an arm-isolated environment with Codemap exposed only to its treatments.

        Plugin bin/ directories are not reliably added to PATH in ``claude -p`` mode, so the
        codemap ``bin/`` dir is injected explicitly to keep ``scan-query`` reachable inside skill
        Bash calls. For the codemap and combined arms ``SCAN_NO_AUTOBUILD=1`` is set so the
        /codemap-py:query-code Step 0 never runs ``scan-index --incremental`` inside the measured
        window — the benchmark builds the index out of band. A genuinely
        missing index then fails loudly instead of being silently rebuilt mid-task.

        ``CLAUDE_PLUGIN_ROOT`` is also exported for the codemap-consuming arms, pointed at the
        same deterministic fixture passed via ``--plugin-dir``. The ``claude`` CLI does not
        reliably propagate this var into a Skill's Bash execution context in headless ``-p``
        mode; without it, ``query-code/SKILL.md``'s ``${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}``
        fallback resolves to a relative path that doesn't exist in the copied sandbox repo, and
        the agent burns calls on ``find``/``which``/``printenv`` hunting for the binary instead
        (confirmed: 15/15 Claude C_strict cells hit this, 0/16 Codex — Codex's adapter resolves
        via an explicit ``CODEMAP_BIN`` env var instead of an implicit CLI-propagated one). If the
        CLI does set it correctly for a given invocation, this value is simply overridden per-Skill
        and is a no-op.

        Args:
            arm: Benchmark arm; only ``codemap`` / ``combined`` / ``B_auto`` / ``C_strict``
                receive the build opt-out and CLAUDE_PLUGIN_ROOT — never ``A_plain``/``plain``,
                whose contract is "Codemap is absent and inaccessible"; leaking availability
                there would break treatment isolation.

        Returns:
            A copy of the process environment with PATH (and, for structural arms, the opt-out
            and CLAUDE_PLUGIN_ROOT).
        """
        env = os.environ.copy()
        # The parent-only path list names evidence that controls benchmark scoring;
        # exposing it would itself disclose where an evaluator can look for answers.
        env.pop("BENCHMARK_EVIDENCE_ROOTS", None)
        for key in ("CLAUDE_PLUGIN_ROOT", "CODEMAP_BIN", "CODEMAP_PYTHON", "SCAN_NO_AUTOBUILD"):
            env.pop(key, None)
        if arm in ("codemap", "combined", "B_auto", "C_strict"):
            plugin_dir = codemap_plugin_dir or cls._codemap_plugin_dir()
            if plugin_dir is None:
                raise RuntimeError(f"Codemap plugin fixture is required for {arm} but is unavailable")
            codemap_bin_on_path(env, Path(plugin_dir))
            env["SCAN_NO_AUTOBUILD"] = "1"
            env["CLAUDE_PLUGIN_ROOT"] = str(plugin_dir)
            env["CODEMAP_PYTHON"] = cls._eligible_codemap_python()
        return env

    @staticmethod
    def _eligible_codemap_python() -> str:
        """Return the current external CPython only when it meets the staged launcher's range."""
        candidate = Path(sys.executable).resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError("Codemap treatment requires an executable CPython >=3.11,<3.15")
        probe = subprocess.run(
            [
                str(candidate),
                "-c",
                "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15) else 127)",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if probe.returncode != 0:
            raise RuntimeError("Codemap treatment requires CPython >=3.11,<3.15")
        return str(candidate)

    def _stream_events(
        self,
        cmd: list[str],
        result: BenchmarkRun,
        update_fn: Optional[Callable[[float, "BenchmarkRun"], None]] = None,
        cwd: Optional[Path] = None,
        arm: str = "",
    ) -> None:
        """Launch the claude subprocess, enforce wall-clock timeout, and parse stream-json events.

        Reads stdout line-by-line and routes each JSON event to ``_handle_event``.
        Calls ``update_fn(elapsed_s, result)`` at most every 0.5 s while the subprocess
        is running. Kills the subprocess via a ``threading.Timer`` at ``self.timeout``
        seconds and records the error on *result*.

        Args:
            cmd: Full ``claude`` CLI command list, constructed by ``run()``.
            result: Mutable ``BenchmarkRun`` populated in-place as events arrive.
            update_fn: Optional throttled callback; signature
                ``(elapsed_seconds: float, result: BenchmarkRun) -> None``.
                Invoked at most every 0.5 s. Pass ``None`` to disable.
            cwd: Working directory for the subprocess. Defaults to ``self.repo_path``.
                Plain-arm runs pass a symlink-based stripped copy without ``.cache/``.
            arm: Benchmark arm; forwarded to ``_subprocess_env`` so the codemap / combined
                arms receive the ``SCAN_NO_AUTOBUILD`` opt-out.
        """
        pending: dict[str, float] = {}
        pending_codemap_ids: set[str] = set()  # all codemap skill calls (for erec corpus)
        pending_rdeps_ids: set[str] = set()  # codemap rdeps calls specifically (for sc)
        pending_semble_ids: set[str] = set()  # all semble MCP calls (for erec corpus)

        def _on_event(event: dict, ts: float) -> None:
            result.raw_events.append(dict(event))
            self._handle_event(event, result, pending, pending_codemap_ids, pending_rdeps_ids, pending_semble_ids, ts)

        plugin_dir = None
        if "--plugin-dir" in cmd:
            plugin_dir = Path(cmd[cmd.index("--plugin-dir") + 1])

        outcome = stream_claude(
            cmd,
            timeout=self.timeout,
            cwd=cwd if cwd is not None else self.repo_path,
            env=self._subprocess_env(arm, codemap_plugin_dir=plugin_dir),
            on_event=_on_event,
            update_fn=(lambda elapsed: update_fn(elapsed, result)) if update_fn else None,
        )
        # Map mechanics onto the run, preserving this lane's error precedence:
        # stderr (on a non-success run) → timeout → any unexpected exception.
        result.elapsed_s = outcome.elapsed_s
        if not result.success and not result.error and outcome.stderr:
            result.error = outcome.stderr.strip()[:300]
        if outcome.returncode is not None and outcome.returncode < 0 and not result.error:
            result.error = f"timeout ({self.timeout}s)"
        if outcome.exc_timeout:
            result.error = f"timeout ({self.timeout}s)"
        if outcome.error and not result.error:
            result.error = outcome.error

    def _handle_event(
        self,
        event: dict,
        result: BenchmarkRun,
        pending: dict[str, float],
        pending_codemap_ids: set[str],
        pending_rdeps_ids: set[str],
        pending_semble_ids: set[str],
        ts: float,
    ) -> None:
        """Route native events while excluding unstructured diagnostics from tool accounting."""
        etype = event.get("type", "")
        blocks = _claude_message_blocks(event)

        if etype == "assistant":
            event_text_start = len(result.output_text)
            event_has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
            for block in blocks:
                self._on_tool_use(block, result, pending, ts)
                if block.get("type") == "text":
                    result.output_text += block.get("text", "")
                # Track codemap skill calls for erec corpus + skill_coverage
                if block.get("type") == "tool_use" and block.get("name") == "Skill":
                    tool_id = block.get("id", "")
                    skill_str = block.get("input", {}).get("skill", "")
                    args_str = block.get("input", {}).get("args", "")
                    if "codemap" in skill_str:
                        result.tools.codemap += 1
                        pending_codemap_ids.add(tool_id)
                        if "rdeps" in args_str or "rdeps" in skill_str:
                            pending_rdeps_ids.add(tool_id)
                elif block.get("type") == "tool_use" and block.get("name") == "Bash":
                    tool_id = block.get("id", "")
                    cmd = block.get("input", {}).get("command", "")
                    # In claude -p mode the skill sub-model never spawns; capture scan-query rdeps
                    # Bash calls as the equivalent fallback so sc/erec are meaningful.
                    # Match both "scan-query rdeps <m>" and "/path/to/scan-query rdeps <m>".
                    if re.search(r"(?:^|/)scan-query\s+rdeps\s+\S", cmd):
                        pending_codemap_ids.add(tool_id)
                        pending_rdeps_ids.add(tool_id)
                elif block.get("type") == "tool_use" and block.get("name") in (
                    "mcp__semble__search",
                    "mcp__semble__find_related",
                ):
                    tool_id = block.get("id", "")
                    result.tools.semble += 1
                    pending_semble_ids.add(tool_id)
            if not event_has_tool_use and len(result.output_text) > event_text_start:
                result.last_tool_text_offset = event_text_start
        elif etype == "user":
            # Tool results arrive as {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id", "")
                if tool_id in pending:
                    result.tool_elapsed_s += ts - pending.pop(tool_id)
                is_codemap = tool_id in pending_codemap_ids
                is_rdeps = tool_id in pending_rdeps_ids
                is_semble = tool_id in pending_semble_ids
                content_raw = block.get("content", "")
                if is_codemap:
                    pending_codemap_ids.discard(tool_id)
                if is_rdeps:
                    pending_rdeps_ids.discard(tool_id)
                if is_semble:
                    pending_semble_ids.discard(tool_id)
                # Detect blocked/permission-denied results and track separately
                _content_str = (
                    content_raw
                    if isinstance(content_raw, str)
                    else " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content_raw)
                )
                if "<tool_use_error>" in _content_str and (is_semble or is_codemap):
                    result.tools.blocked += 1
                    if not result.error_type:
                        result.error_type = "skill_blocked"
                self._on_tool_result(content_raw, result, is_codemap=is_codemap, is_rdeps=is_rdeps, is_semble=is_semble)
            # Advance report boundary: anything the model writes AFTER this tool_result event is the
            # final answer.  Without this, last_tool_text_offset stays 0 when the model emits no
            # pure-text event after its last tool call (Scenario-2 bug), making rrec corpus = full
            # output_text preamble and producing misleading rrec values.
            if any(b.get("type") == "tool_result" for b in blocks):
                result.last_tool_text_offset = len(result.output_text)
        elif etype == "result":
            u = parse_result_usage(event)
            result.usage_complete = True
            result.cache_creation_tokens = u.cache_creation_tokens
            result.cache_read_tokens = u.cache_read_tokens
            result.input_tokens = u.input_tokens
            result.output_tokens = u.output_tokens
            result.cost_usd = u.cost_usd
            result.success = u.success
            if not result.success:
                result.error_type = u.subtype  # e.g. "error_max_turns", "error_non_zero_exit"

    def _on_tool_use(
        self,
        block: dict,
        result: BenchmarkRun,
        pending: dict[str, float],
        ts: float,
    ) -> None:
        """Update tool counts, log, and timing for one tool_use content block."""
        if block.get("type") != "tool_use":
            return
        name = block.get("name", "")
        tool_id = block.get("id", "")
        inp = block.get("input", {})
        attr = name.lower()
        if hasattr(result.tools, attr):
            setattr(result.tools, attr, getattr(result.tools, attr) + 1)
        result.tool_log.append(f"{name}: {_tool_key_arg(name, inp)}")
        if name in self.EXPLORATION_TOOLS and tool_id:
            pending[tool_id] = ts
        if name == "Bash":
            cmd = inp.get("command", "")
            if _invokes_scan_query(cmd):
                result.tools.scan_query += 1
            # Patterns typical of manual import graph discovery (not file-reading)
            if re.search(r"\b(grep|rg)\b.*\bimport\b|\bgrep\b.*\bfrom\b|\bimport\b.*-r\b", cmd):
                result.tools.bash_for_imports += 1
            # Detect direct reads of the codemap index JSON (isolation violation in plain arm)
            if re.search(r"\.cache/codemap/|\.cache/scan/", cmd):
                result.tools.index_reads += 1

    @staticmethod
    def _on_tool_result(
        content: str | list,
        result: BenchmarkRun,
        is_codemap: bool = False,
        is_rdeps: bool = False,
        is_semble: bool = False,
    ) -> None:
        """Accumulate token count and capture codemap/semble results from a tool result content field.

        Skips content that contains ``<tool_use_error>`` or starts with
        ``"Launching skill:"`` (skill-executor status placeholders).

        Args:
            content: Raw ``content`` field from the ``tool_result`` event — either a plain
                string or a list of content blocks.
            result: The accumulating ``BenchmarkRun`` updated in-place.
            is_codemap: True when this result is from any codemap skill call. Not currently
                used for corpus capture (rdeps calls are distinguished by ``is_rdeps``);
                reserved for future per-call filtering.
            is_rdeps: True when this result is from a ``codemap:query rdeps`` call.
                Appends the text to ``result.codemap_results`` and ``result.skill_result_text``
                (used for exposure-recall corpus and skill-coverage scoring).
            is_semble: True when this result is from a semble MCP tool call
                (``mcp__semble__search`` or ``mcp__semble__find_related``). Appends the
                text to ``result.semble_results`` for the exposure-recall corpus.
        """
        for text in _iter_tool_result_texts(content):
            _capture_tool_result_text(text, result, is_rdeps=is_rdeps, is_semble=is_semble)


def _iter_tool_result_texts(content: str | list) -> Iterator[str]:
    """Yield each text payload carried by a ``tool_result`` event's ``content`` field.

    Accepts both event shapes: a plain string, or a list of blocks where each block is either
    a string or a dict carrying ``text`` / ``content``. Any other shape yields nothing.

    Args:
        content: Raw ``content`` field from the ``tool_result`` event.

    Yields:
        Each text payload, in event order.

    Examples:
        >>> list(_iter_tool_result_texts("plain"))
        ['plain']
        >>> list(_iter_tool_result_texts([{"text": "a"}, "b", {"content": "c"}, {"other": 1}]))
        ['a', 'b', 'c', '']
        >>> list(_iter_tool_result_texts([]))
        []
    """
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content") or ""
                if isinstance(text, str):
                    yield text
            elif isinstance(block, str):
                yield block


def _capture_tool_result_text(text: str, result: BenchmarkRun, *, is_rdeps: bool, is_semble: bool) -> None:
    """Bill one tool-result payload to *result* and file it into the matching corpus.

    Args:
        text: One tool-result text payload.
        result: The accumulating ``BenchmarkRun`` updated in place.
        is_rdeps: True when this result came from a ``codemap:query rdeps`` call.
        is_semble: True when this result came from a semble MCP tool call.

    Examples:
        >>> run = BenchmarkRun(arm="codemap", task_id="T1", task_type="t", model="haiku", success=True)
        >>> _capture_tool_result_text("modules", run, is_rdeps=True, is_semble=False)
        >>> run.codemap_results
        ['modules']
        >>> _capture_tool_result_text("<tool_use_error>boom", run, is_rdeps=True, is_semble=False)
        >>> run.codemap_results, run.tool_errors
        (['modules'], ['<tool_use_error>boom'])
    """
    result.tool_result_tokens += count_tokens(text)
    # Skip error responses and skill executor status placeholders from corpus
    if "<tool_use_error>" in text or text.startswith("Launching skill:"):
        if "<tool_use_error>" in text:
            result.tool_errors.append(text[:2000])
        return
    if is_rdeps:
        result.codemap_results.append(text)
        result.skill_result_text += ("\n" if result.skill_result_text else "") + text
    if is_semble:
        result.semble_results.append(text)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _normalize_match_text(text: str) -> str:
    """Normalise text for whitespace-tolerant keyword matching.

    Drops whitespace flanking operators and punctuation so brittle literals like ``"< 1"`` match
    ``"<1"`` (and ``"<  1"``), while preserving single spaces between word tokens so distinct
    identifiers are never merged into false matches. Case is folded to lower.

    Args:
        text: Raw diff / answer text, or a single expected keyword.

    Returns:
        The normalised, lower-cased string ready for substring comparison.

    Examples:
        >>> _normalize_match_text("if patience < 1:")
        'if patience<1:'
        >>> _normalize_match_text("< 1") in _normalize_match_text("guard patience<1 here")
        True
        >>> _normalize_match_text("raise Error")  # word-word spaces are preserved
        'raise error'
    """
    collapsed = re.sub(r"\s*([^\w\s])\s*", r"\1", text)
    return re.sub(r"\s+", " ", collapsed).strip().lower()


def score_read_crop(output_text: str, expected_keywords: list[str]) -> QualityScore:
    """Keyword-recall scorer for read_crop tasks (no rdeps ground truth).

    ``erec``/``rrec`` are reused as keyword recall so the metric flows through the existing
    report columns; the headline efficiency signal is ``tool_result_tokens`` (read cost),
    reported separately. A keyword is matched case-insensitively as a substring of the answer.

    Args:
        output_text: Full agent answer text.
        expected_keywords: Ground-truth identifiers the correct contract must mention.

    Returns:
        QualityScore with recall in ``erec``/``rrec``; ``scored=False`` when no keywords given.

    Examples:
        >>> s = score_read_crop("uses prog_bar and on_step", ["prog_bar", "on_step", "logger"])
        >>> round(s.erec, 2), s.erec_tp, s.erec_fn
        (0.67, 2, 1)
    """
    if not expected_keywords:
        return QualityScore(scored=False)
    haystack = _normalize_match_text(output_text)
    hits = sum(1 for k in expected_keywords if _normalize_match_text(k) in haystack)
    n = len(expected_keywords)
    rec = hits / n
    return QualityScore(scored=True, erec=rec, erec_tp=hits, erec_fn=n - hits, rrec=rec, rrec_tp=hits, rrec_fn=n - hits)


def score_fix(
    diff_text: str,
    expected_patch_keywords: list[str],
    expected_files: list[str],
    test_passed: Optional[bool] = None,
) -> QualityScore:
    """Keyword-recall scorer for fix_single / fix_multicaller tasks.

    Checks the unified diff of agent edits for expected change markers. ``erec`` measures
    keyword recall in added lines; ``rrec`` measures file recall (were the right files changed).
    Keyword matching is whitespace-tolerant (see ``_normalize_match_text``) so operator literals
    like ``"< 1"`` are not defeated by an agent writing ``"<1"``.

    ``test_passed`` carries a stronger, opt-in correctness signal recorded *alongside* erec (it
    never replaces the recall column): when the task declares a targeted test, the caller runs it
    on the post-edit sandbox and passes the outcome here. It stays ``None`` for tasks with no
    declared test, so tasks without one are unaffected.

    Args:
        diff_text: Output of ``diff -ru original copy`` after agent run.
        expected_patch_keywords: Strings expected in diff added lines (``+`` prefix).
        expected_files: Relative file-path fragments expected in ``+++ b/...`` headers.
        test_passed: Outcome of the task's declared targeted test (True/False), or ``None`` when
            the task declares no test or the test could not be launched.

    Returns:
        QualityScore with keyword recall in ``erec``, file-change recall in ``rrec``, and the
        opt-in ``test_passed`` correctness signal. Returns ``scored=False`` when no keywords given.

    Examples:
        >>> d = "+        if patience < 1:\\n+            raise MisconfigurationException('patience')"
        >>> s = score_fix(d, ["patience < 1", "MisconfigurationException"], ["early_stopping.py"])
        >>> s.erec
        1.0
        >>> s.rrec  # no +++ header in that diff snippet — file path absent
        0.0
    """
    if not expected_patch_keywords:
        return QualityScore(scored=False)
    added_lines = "\n".join(
        line[1:] for line in diff_text.splitlines() if line.startswith("+") and not line.startswith("+++")
    )
    haystack = _normalize_match_text(added_lines)
    hits = sum(1 for k in expected_patch_keywords if _normalize_match_text(k) in haystack)
    erec = round(hits / len(expected_patch_keywords), 3)
    # diff -ru produces "+++ /full/path\t<timestamp>"; git diff produces "+++ b/path"
    changed_files = set(re.findall(r"^\+\+\+ (?:b/)?(.+?)(?:\t.*)?$", diff_text, re.MULTILINE))
    rrec = 0.0
    if expected_files:
        file_hits = sum(1 for f in expected_files if any(f in cf for cf in changed_files))
        rrec = round(file_hits / len(expected_files), 3)
    return QualityScore(
        scored=True,
        erec=erec,
        erec_tp=hits,
        erec_fn=len(expected_patch_keywords) - hits,
        rrec=rrec,
        test_passed=test_passed,
    )


def _median_metrics(rlist: list[BenchmarkRun]) -> dict[str, float | None]:
    """Return one cell's success-only medians alongside its failure count and all-runs spend.

    Medians taken over successful runs only let a failure-heavy arm read as the cheap,
    fast one: a cell where two of three runs died at the wall-clock limit reported the
    survivor's cost and elapsed time, and the two that burned a full paid timeout each
    left no trace in the numbers. ``n_runs``/``n_failures`` and the ``*_all`` aggregates
    (which include failed runs) therefore accompany every cell, and a cell whose runs all
    failed now reports that spend instead of collapsing to an empty dict. Quality medians
    stay success-only — a failed run has no answer to score.
    """
    if not rlist:
        return {}
    ok = [r for r in rlist if r.success]
    spend: dict[str, float | None] = {
        "n_runs": len(rlist),
        "n_failures": len(rlist) - len(ok),
        "success_rate": len(ok) / len(rlist),
        "tool_calls_all": statistics.median([r.tools.total for r in rlist]),
        "input_tokens_all": statistics.median([r.input_tokens for r in rlist]),
        "cost_usd_all": statistics.median([run_cost_usd(r) for r in rlist]),
        "elapsed_s_all": statistics.median([r.elapsed_s for r in rlist]),
    }
    if not ok:
        return spend
    chunk_vals = [r.quality.chunk_hit_rate for r in ok if r.quality.chunk_hit_rate is not None]
    return {
        **spend,
        "tool_calls": statistics.median([r.tools.total for r in ok]),
        "input_tokens": statistics.median([r.input_tokens for r in ok]),
        "cost_usd": statistics.median([run_cost_usd(r) for r in ok]),
        "tool_result_tokens": statistics.median([r.tool_result_tokens for r in ok]),
        "tool_elapsed_s": statistics.median([r.tool_elapsed_s for r in ok]),
        "elapsed_s": statistics.median([r.elapsed_s for r in ok]),
        "rrec": statistics.median([r.quality.rrec for r in ok]),
        "erec": statistics.median([r.quality.erec for r in ok]),
        "delta": statistics.median([r.quality.delta for r in ok]),
        # None when no run in the cell carried a semble corpus (plain / codemap arms).
        "chunk_hit_rate": statistics.median(chunk_vals) if chunk_vals else None,
    }


def aggregate(
    results: list[BenchmarkRun],
    task_ids: list[str],
    model_short: str | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Return {task_id: {arm: {metric: value}}} optionally filtered to one model tier."""
    filtered = [r for r in results if model_short is None or r.model == model_short]
    by_task_arm: dict[str, dict[str, list[BenchmarkRun]]] = defaultdict(lambda: defaultdict(list))
    for r in filtered:
        by_task_arm[r.task_id][r.arm].append(r)

    out: dict[str, dict[str, dict[str, float]]] = {}
    for tid in task_ids:
        out[tid] = {}
        for arm, rlist in by_task_arm.get(tid, {}).items():
            out[tid][arm] = _median_metrics(rlist)
    return out


# ---------------------------------------------------------------------------
# Report renderer
# ---------------------------------------------------------------------------


class Report:
    """Renders a markdown benchmark report from a set of completed runs.

    Encapsulates the savings summary, per-task tables, and report assembly logic. All report-specific constants are
    class-level attributes.
    """

    #: Control arm names in preference order; the first one present in the results is the baseline.
    _BASELINE_ARMS = ("A_plain", "plain")
    #: Treatment arm names in the order their columns are rendered.
    _INJECTED_ARMS = ("C_strict", "B_auto", "codemap", "semble", "combined")
    _NO_PAIRS_MD = "_(no completed baseline + injected arm pairs)_"

    # Limitations appended verbatim to every report
    _LIMITATIONS_MD = [
        "## Limitations",
        "",
        "- Purely quantitative — answer quality / correctness is not scored",
        "- Tool time tracks wall-clock including I/O; LLM think time is not isolated",
        "- tiktoken o200k_base approximates Claude's tokeniser (not exact)",
        "- Cost ($) uses the fixed PRICES table (list prices, version-agnostic per tier); runs without a captured cache breakdown bill all input at full price = upper bound",
        "- Results vary across runs; model tier is the primary variance axis here",
        "- Tested on pytorch-lightning; generalisation to other corpora not assessed",
        "",
    ]

    @staticmethod
    def fmt_tokens(v: float) -> str:
        return f"{v / 1000:.1f}k"

    @staticmethod
    def _fmt_s(v: float) -> str:
        return f"{v:.1f}s"

    @staticmethod
    def _fmt_int(v: float) -> str:
        return f"{v:.0f}"

    @staticmethod
    def _fmt_usd(v: float) -> str:
        return f"${v:.3f}"

    @staticmethod
    def _fmt_pct(v: float) -> str:
        return f"{v:.0%}"

    # Task types scored by diff/keyword recall, not by a reverse-dependency list. Their
    # efficiency (token / tool-call) savings are suppressed because the arms differ in edit
    # workload, not in structural-discovery cost — a savings figure there would be biased
    # Quality (erec/rrec keyword recall) is still rendered for them.
    _FIX_TYPES = ("fix_single", "fix_multicaller")

    # Key metrics first — these are the headline savings signal.
    # Diagnostic metrics follow (tool breakdown, tool-only time).
    # cost_usd is the fair cross-arm metric (arms run different-priced models).
    _METRICS = [
        ("elapsed_s", "Elapsed (s)", _fmt_s),
        ("cost_usd", "Cost ($)", _fmt_usd),
        ("input_tokens", "Input tokens (k)", fmt_tokens),
        ("tool_calls", "Tool calls", _fmt_int),
        ("tool_result_tokens", "Tool result tokens (k)", fmt_tokens),
        ("tool_elapsed_s", "Tool time (s)", _fmt_s),
    ]

    # Quality lenses rendered as absolute per-arm medians (never as savings — higher is better).
    # chunk_hit_rate is the semble-native lens and is None (rendered "—") for plain / codemap.
    _QUALITY_METRICS = [
        ("erec", "Exposure recall (erec)", _fmt_pct),
        ("rrec", "Report recall (rrec)", _fmt_pct),
        ("chunk_hit_rate", "Chunk hit rate (semble lens)", _fmt_pct),
    ]

    def __init__(self, results: list[BenchmarkRun], tasks: list[Task], metadata: dict) -> None:
        self.results = results
        self.task_ids = [t.id for t in tasks]
        self.task_meta: dict[str, Task] = {t.id: t for t in tasks}
        self.metadata = metadata
        result_models = {r.model for r in results}
        self.model_tiers: list[str] = [m for m in MODELS if m in result_models]
        for m in result_models:
            if m not in self.model_tiers:
                self.model_tiers.append(m)

    def render(self) -> str:
        """Produce the full markdown report string."""
        reporting = self.metadata.get("agentic_reporting")
        summaries = reporting.get("summaries_by_model") if isinstance(reporting, Mapping) else None
        if (
            isinstance(summaries, Mapping)
            and summaries
            and all(isinstance(model, str) and isinstance(summary, Mapping) for model, summary in summaries.items())
        ):
            repeat = self.metadata.get("repeat", 1)
            lines = [
                f"# Codemap Skill Benchmark Report — {self.metadata.get('date', 'n/a')}",
                "",
                f"**Models**: {', '.join(summaries)}  ",
                f"**Repo**: {self.metadata.get('repo', 'n/a')}  ",
                f"**Index**: {self.metadata.get('index', 'n/a')}  ",
                f"**Tasks**: {len(self.task_ids)}  ",
                f"**Repeat runs**: {repeat}  ",
                "",
                "## Canonical graded quality summary",
                "",
                "> Graded quality is the all-assigned headline. Exact pass is a secondary diagnostic; efficiency compares only both-passing A_plain/C_strict cells.",
                "",
            ]
            for model, summary in summaries.items():
                lines += [f"### {model.capitalize()}", "", "```text"]
                lines.extend(line for line, _ in summary_lines(summary))
                lines += ["```", ""]
            return "\n".join(lines)

        models_label = ", ".join(self.model_tiers) if self.model_tiers else self.metadata.get("models", "n/a")

        repeat = self.metadata.get("repeat", 1)
        lines = [
            f"# Codemap Skill Benchmark Report — {self.metadata.get('date', 'n/a')}",
            "",
            f"**Models**: {models_label}  ",
            f"**Repo**: {self.metadata.get('repo', 'n/a')}  ",
            f"**Index**: {self.metadata.get('index', 'n/a')}  ",
            f"**Tasks**: {len(self.task_ids)}  ",
            f"**Repeat runs**: {repeat}  ",
            "",
            f"> Savings = 1 − (arm / {self._baseline_arm()}) per task; positive = arm needs less.",
            "",
        ]

        # ── Cross-model savings summary ──────────────────────────────────
        if len(self.model_tiers) > 1:
            lines += ["## Savings Summary by Model", ""]
            for m in self.model_tiers:
                agg = aggregate(self.results, self.task_ids, model_short=m)
                summary_rows = self._savings_summary(agg)
                lines.append(f"### {m.capitalize()}")
                lines.append("")
                if summary_rows:
                    lines.append(pd.DataFrame(summary_rows).to_markdown(index=False))
                else:
                    lines.append(self._NO_PAIRS_MD)
                lines.append("")
        else:
            m = self.model_tiers[0] if self.model_tiers else None
            agg = aggregate(self.results, self.task_ids, model_short=m)
            summary_rows = self._savings_summary(agg)
            lines += ["## Savings Summary", ""]
            if summary_rows:
                lines.append(pd.DataFrame(summary_rows).to_markdown(index=False))
            else:
                lines.append(self._NO_PAIRS_MD)
            lines.append("")

        # ── Per-model per-task tables ────────────────────────────────────
        for m in self.model_tiers:
            agg = aggregate(self.results, self.task_ids, model_short=m)
            lines.append(f"## Detail — {m.capitalize()}")
            lines.append("")
            lines += self._per_task_tables(agg)
            lines += [f"## Quality & reliability — {m.capitalize()}", ""]
            lines += self._per_task_quality_tables(agg)
            lines += self._success_table(m)
            lines += self._failures_section(m)

        lines += self._LIMITATIONS_MD

        return "\n".join(lines)

    def _arm_cells(self, arm: str, bv, iv, fmt, savings_applicable: bool = True) -> dict[str, str]:
        have_pair = savings_applicable and bv is not None and iv is not None and bv > 0
        if not savings_applicable:
            saved, arrow = "n/a", ""
        else:
            saved = f"{1.0 - iv / bv:.0%}" if have_pair else "—"
            arrow = ("↓" if iv < bv else "↑") if have_pair else ""
        return {
            arm.capitalize(): fmt(iv) if iv is not None else "—",
            f"{arm.capitalize()} savings": f"{saved} {arrow}".strip(),
        }

    def _baseline_arm(self) -> str:
        """Return the control arm these results were run with.

        Parity runs record ``A_plain`` while older runs record ``plain``; both are controls, so the renderer resolves
        the name from the results instead of assuming one and rendering every savings column as an unpaired dash.
        """
        present = {r.arm for r in self.results}
        return next((a for a in self._BASELINE_ARMS if a in present), self._BASELINE_ARMS[-1])

    def _efficiency_task_ids(self) -> list[str]:
        """Task ids eligible for efficiency savings — fix-family tasks are excluded."""
        return [tid for tid in self.task_ids if (t := self.task_meta.get(tid)) and t.type not in self._FIX_TYPES]

    def _savings_summary(self, agg: dict) -> list[dict]:
        """Build savings rows for one model's aggregated results, one row per arm × metric.

        Each row carries ``n`` — the number of tasks where BOTH the plain baseline and the arm succeeded — so the
        denominator behind every savings figure is visible. Fix-family tasks are excluded from the efficiency
        denominators.
        """
        baseline = self._baseline_arm()
        present_arms = {r.arm for r in self.results}
        injected_arms = [a for a in self._INJECTED_ARMS if a in present_arms]
        eligible = self._efficiency_task_ids()
        rows = []
        for arm in injected_arms:
            for key, label, _ in self._METRICS:
                savings_per_task = [
                    1.0 - iv / bv
                    for tid in eligible
                    for bv in [agg.get(tid, {}).get(baseline, {}).get(key)]
                    for iv in [agg.get(tid, {}).get(arm, {}).get(key)]
                    if bv and iv and bv > 0
                ]
                if not savings_per_task:
                    continue
                rows.append(
                    {
                        "Arm": arm,
                        "Metric": label,
                        "n": len(savings_per_task),
                        "Median savings": f"{statistics.median(savings_per_task):.0%}",
                        "Mean savings": f"{statistics.mean(savings_per_task):.0%}",
                        "Min savings": f"{min(savings_per_task):.0%}",
                        "Max savings": f"{max(savings_per_task):.0%}",
                    }
                )
        return rows

    def _per_task_tables(self, agg: dict) -> list[str]:
        """Return markdown lines for per-task metric tables, with dynamic columns for all injected arms."""
        baseline = self._baseline_arm()
        present_arms = {r.arm for r in self.results}
        injected_arms = [a for a in self._INJECTED_ARMS if a in present_arms]
        lines: list[str] = []
        for key, label, fmt in self._METRICS:
            rows = []
            for tid in self.task_ids:
                t = self.task_meta.get(tid)
                savings_ok = not (t and t.type in self._FIX_TYPES)
                bv = agg.get(tid, {}).get(baseline, {}).get(key)
                row = {
                    "Task": tid,
                    "Type": t.type if t else "?",
                    baseline.capitalize(): fmt(bv) if bv is not None else "—",
                }
                for arm in injected_arms:
                    iv = agg.get(tid, {}).get(arm, {}).get(key)
                    row.update(self._arm_cells(arm, bv, iv, fmt, savings_applicable=savings_ok))
                rows.append(row)
            lines += [f"### {label}", "", pd.DataFrame(rows).to_markdown(index=False), ""]
        return lines

    def _rendered_arms(self) -> list[str]:
        """Baseline plus every injected arm that produced at least one run, in canonical order."""
        present_arms = {r.arm for r in self.results}
        return [self._baseline_arm()] + [a for a in self._INJECTED_ARMS if a in present_arms]

    def _per_task_quality_tables(self, agg: dict) -> list[str]:
        """Render absolute per-arm quality medians (erec / rrec / chunk hit rate) — no savings.

        Quality is a correctness lens where higher is better, so it is shown as absolute percentages for every arm.
        ``chunk_hit_rate`` is the semble-native lens and renders "—" for arms that carry no semble corpus.
        """
        arms = self._rendered_arms()
        lines: list[str] = []
        for key, label, fmt in self._QUALITY_METRICS:
            rows = []
            for tid in self.task_ids:
                t = self.task_meta.get(tid)
                row = {"Task": tid, "Type": t.type if t else "?"}
                for arm in arms:
                    v = agg.get(tid, {}).get(arm, {}).get(key)
                    row[arm.capitalize()] = fmt(v) if v is not None else "—"
                rows.append(row)
            lines += [f"### {label}", "", pd.DataFrame(rows).to_markdown(index=False), ""]
        return lines

    def _cell_counts(self, model: str) -> dict[str, dict[str, list[int]]]:
        """Return ``{task_id: {arm: [n_total, n_success]}}`` for one model tier.

        Unlike ``aggregate`` (which drops all-failed cells), this keeps every cell so failures are countable — a cell
        where every run failed still reports ``[n_total, 0]``.
        """
        counts: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in self.results:
            if r.model != model:
                continue
            cell = counts[r.task_id][r.arm]
            cell[0] += 1
            if r.success:
                cell[1] += 1
        return counts

    def _success_table(self, model: str) -> list[str]:
        """Render a per-task success-rate table (successful / total runs) for every arm."""
        counts = self._cell_counts(model)
        arms = self._rendered_arms()
        rows = []
        for tid in self.task_ids:
            row = {"Task": tid}
            for arm in arms:
                n_total, n_ok = counts.get(tid, {}).get(arm, [0, 0])
                row[arm.capitalize()] = f"{n_ok}/{n_total}" if n_total else "—"
            rows.append(row)
        return ["### Success rate (successful / total runs)", "", pd.DataFrame(rows).to_markdown(index=False), ""]

    def _failures_section(self, model: str) -> list[str]:
        """List every failed run for one model tier so drops are visible, not silently omitted."""
        fails = [r for r in self.results if r.model == model and not r.success]
        if not fails:
            return ["### Failed runs", "", "_No failed runs._", ""]
        rows = [{"Task": r.task_id, "Arm": r.arm, "Error": (r.error_type or r.error or "failed")[:80]} for r in fails]
        return ["### Failed runs", "", pd.DataFrame(rows).to_markdown(index=False), ""]


# ---------------------------------------------------------------------------
# Run-loop helpers
# ---------------------------------------------------------------------------


# rich styles for run-line output — arm colors make quads easy to scan; matches README's documented
# canonical scheme (A_plain=yellow, B_auto=cyan, C_strict=magenta) and legacy quad colors.
_ARM_STYLE = {
    "plain": "yellow",
    "A_plain": "yellow",
    "codemap": "cyan",
    "B_auto": "cyan",
    "semble": "blue",
    "combined": "green",
    "C_strict": "magenta",
}
_FAIL_STYLE = "red"  # overrides arm color on failure


def _run_line(run_n: int, total_runs: int, task: Task, model_short: str, arm: str, result: BenchmarkRun) -> str:
    """Format the one-line progress summary printed after each run."""
    if not result.success:
        label = result.error_type or result.error or "failed"
        error_suffix = f" | ✗ {label}"
    else:
        error_suffix = ""
    tc = result.tools
    q = result.quality
    canonical_progress = (
        result.parity_arm in AGENTIC_ARMS and agentic_reporting.REPORTING_VERSION == "agentic-graded-v2"
    )
    if canonical_progress:
        canonical_row = _canonical_agentic_row(result)
        admitted_quality = cell_quality(canonical_row)
        quality_text = "n/a" if admitted_quality is None else f"{admitted_quality:.1%}"
        if not result.success or result.incomplete:
            exact_marker = "!"
        elif cell_passes(canonical_row):
            exact_marker = "✓"
        else:
            exact_marker = "✗"
        quality_suffix = f" | quality={quality_text} exact={exact_marker}"
    elif q.scored:
        erec_part = f"erec={q.erec:4.0%} rrec={q.rrec:4.0%}"
        sc_part = f"  sc={q.skill_coverage:4.0%}" if q.skill_coverage is not None else ""
        chr_part = f"  chr={q.chunk_hit_rate:4.0%}" if q.chunk_hit_rate is not None else ""
        top10_part = f"  e@10={q.erec_top10:4.0%}" if q.erec_top10_k >= 5 else ""
        quality_suffix = f" | {erec_part}{sc_part}{chr_part}{top10_part}"
    else:
        quality_suffix = " | quality=n/a"
    # Flag possibly-degenerate codemap runs (very few total calls with 0% quality)
    degenerate_note = ""
    if arm == "codemap" and result.tools.total < 6 and result.tools.skill > 0 and q.scored and q.erec == 0.0:
        degenerate_note = " ⚑degenerate?"
    # Keep response-wire failures visible without rewriting independent evidence recall.
    if result.answer_contract_valid is False:
        degenerate_note += " ⚑ans-parse"
    # One padded field, not two: padding id and difficulty separately opens a gap inside `(hard)`. Width 15 fits the
    # widest pair this suite produces, `BA-07 (extreme)`; every field after it is already padded, so an unpadded label
    # here shifted the whole rest of the row by the length of the difficulty word.
    task_label = f"{task.id.lstrip('T')} ({task.difficulty})"
    _cost = run_cost_usd(result)
    cost_part = f"${_cost:6.3f}" if _cost else "   $—  "  # omit $ when total_cost_usd absent
    query_suffix = ""
    if result.parity_arm == "C_strict":
        query_suffix = (
            f" | query={result.codemap_query_attempted}/{result.codemap_query_succeeded}/"
            f"{'✓' if result.codemap_compact_success else '✗'}"
        )
    return (
        f"({run_n:0{len(str(total_runs))}}/{total_runs}) {task_label:<15} | {model_short:<6} | {arm:<8}"
        f" | time={fmt_time(result.elapsed_s):>6} | {cost_part} | tok: in={fmt_tok(result.input_tokens):>6} out={fmt_tok(result.output_tokens):>6} |\tcalls={result.tools.total:2}"
        f" (Gp={tc.grep:2}; Gb={tc.glob:2}; Bh={tc.bash:2}; Sk={tc.skill:2}; Sm={tc.semble:2}; blk={tc.blocked:2}; bfi={tc.bash_for_imports:2}; idx={tc.index_reads:2})"
        f"{quality_suffix}{query_suffix}"
        f"{error_suffix}{degenerate_note}"
    )


def _agentic_arm_order(task: Task, model_short: str, arms: list[str], rep: int) -> tuple[str, ...]:
    """Return the execution order of *arms* for one task/model/repetition block.

    Only the canonical A/B/C set is counterbalanced, through the same revision-bound policy the structural lanes use.
    Any other arm set (a single arm, a legacy pair) is order-invariant or has no shared policy, so the caller's declared
    order stands.
    """
    if set(arms) != set(AGENTIC_ARMS) or len(arms) != len(AGENTIC_ARMS):
        return tuple(arms)
    return deterministic_arm_order(
        task.experiment_revision or LEGACY_EXPERIMENT_REVISION,
        "claude",
        model_short,
        task.id,
        rep + 1,
    )


def _iter_combos(
    tasks: list[Task],
    models: list[tuple[str, str]],
    arms: list[str],
    repeat: int,
) -> Iterator[tuple[Task, str, str, str, int]]:
    """Yield every (task, model, arm, repetition) cell in counterbalanced execution order.

    A fixed A→B→C sequence confounds arm with position: anything that drifts across a block — provider-side load, rate
    limiting, machine state — hits the arms in the same order every time, and the elapsed-time comparison inherits that
    drift as if it were an arm effect. The structural lanes already counterbalance via the shared revision-bound policy;
    the agentic lane now reuses it, keyed by repetition as well so repeated blocks of one cell do not replay a single
    order.
    """
    for task in tasks:
        for model_short, model_id in models:
            for rep in range(repeat):
                for arm in _agentic_arm_order(task, model_short, arms, rep):
                    yield task, model_short, model_id, arm, rep


# ---------------------------------------------------------------------------
# Benchmark orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AgenticSubProgressUpdate:
    """Live sub-progress callback for one benchmark run.

    Passed as ``ModelRunner.run(update_fn=...)``: each call rewrites this run's sub-bar with
    elapsed time and the running tool tallies.

    Attributes:
        progress: Active rich ``Progress`` instance.
        sub_id: Progress task id of this run's sub-bar.

    Examples:
        >>> update = _AgenticSubProgressUpdate(None, 7)
        >>> update.sub_id
        7
    """

    progress: Any
    sub_id: int

    def __call__(self, elapsed: float, run: BenchmarkRun) -> None:
        """Refresh the sub-bar description from the in-flight run.

        Args:
            elapsed: Seconds since the run's subprocess started.
            run: The ``BenchmarkRun`` being populated in place.
        """
        calls = run.tools.total
        tool_live = f"B={run.tools.bash} G={run.tools.grep} Sk={run.tools.skill} Sm={run.tools.semble}"
        self.progress.update(
            self.sub_id,
            description=f"  {fmt_time(elapsed)} calls={calls} {tool_live}",
        )


class Benchmark:
    """Orchestrates the full benchmark run: iterates tasks x arms x models.

    Constructs ``GroundTruth`` internally from the index, manages result accumulation, tool-call logging, and snapshot
    persistence.
    """

    def __init__(
        self,
        tasks: list[Task],
        arms: list[str],
        models: list[tuple[str, str]],
        repo_path: Path,
        index_path: Path,
        output_path: Path,
        log_path: Path,
        repeat: int = DEFAULT_REPETITIONS,
    ) -> None:
        self.tasks = tasks
        self.arms = arms
        self.models = models
        self.repo_path = repo_path
        self.repo_sha = _repository_fingerprint(repo_path)
        self.index_sha = _sha256_file(index_path)
        self.output_path = output_path
        self.log_path = log_path
        self.repeat = max(1, repeat)
        self.gt = GroundTruth(index_path, tasks, repo_path=repo_path)
        self.answer_oracles: dict[str, AgenticOracle] = {
            task.id: build_oracle(task.answer_task, repo_path)
            for task in tasks
            if any(parity_arm_identity(arm) for arm in arms) and task.answer_task.get("answer_contract") is not None
        }
        self.results: list[BenchmarkRun] = []

    def _iter_combos(self) -> Iterator[tuple[Task, str, str, str, int]]:
        return _iter_combos(self.tasks, self.models, self.arms, self.repeat)

    def _update_canonical_reporting(self, metadata: dict[str, Any]) -> None:
        """Store one per-model prospective pass summary in a canonical snapshot.

        Legacy rows retain their historical schema and report path. Canonical summaries are rebuilt from the complete
        assigned scope after each cell, so a rolling snapshot reports missing coordinates as unobserved failures.
        """
        if not all(parity_arm_identity(arm) for arm in self.arms):
            return
        task_ids = [task.id for task in self.tasks]
        summaries = {
            model_short: summarize_agentic(
                [_canonical_agentic_row(result) for result in self.results if result.model == model_short],
                task_ids=task_ids,
                repetitions=self.repeat,
                arms=self.arms,
            )
            for model_short, _ in self.models
        }
        reporting_path = Path(agentic_reporting.__file__).resolve()
        metadata["agentic_reporting"] = {
            "reporting_version": agentic_reporting.REPORTING_VERSION,
            "module": str(reporting_path),
            "module_sha256": _sha256_file(reporting_path),
            "summaries_by_model": summaries,
        }

    def _run_single(
        self,
        task: Task,
        model_short: str,
        model_id: str,
        arm: str,
        run_n: int,
        total_runs: int,
        print_fn: Callable[[_Text], None],
        metadata: dict,
        update_fn: Optional[Callable[[float, "BenchmarkRun"], None]] = None,
        repetition: int = 1,
    ) -> BenchmarkRun:
        run_timeout = PARITY_TIMEOUT_SECONDS if parity_arm_identity(arm) else MODEL_TIMEOUT.get(model_short, 300)
        runner = ModelRunner(model_short, model_id, self.repo_path, timeout=run_timeout)
        result = runner.run(task, arm, update_fn=update_fn)
        result.repetition = repetition
        result.parity_arm = parity_arm_identity(arm)
        result.experiment_revision = task.experiment_revision if result.parity_arm else LEGACY_EXPERIMENT_REVISION
        if result.parity_arm == "C_strict":
            result.codemap_compliant = result.codemap_compact_success
        result.task_hash = task.task_hash
        result.prompt_hash = task.prompt_hash
        result.suite_hash = task.suite_hash
        result.suite_raw_hash = task.suite_raw_hash
        result.evaluator_id, result.evaluator_hash = _evaluator_provenance(task.type)
        result.envelope_hash = hashlib.sha256(
            runner._system_prompt(task.skill or task.type, arm).encode("utf-8")
        ).hexdigest()
        result.arm_contract_hash = (
            ARM_CONTRACTS[result.parity_arm]["contract_sha256"]
            if result.parity_arm
            else hashlib.sha256(
                json.dumps(
                    {
                        "allowed": ModelRunner._ARM_ALLOWED.get(arm, []),
                        "arm": arm,
                        "disallowed": ModelRunner._ARM_DISALLOWED.get(arm, []),
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )
        result.repo_sha = self.repo_sha
        result.index_sha = self.index_sha
        result.oracle_class = task.oracle_class
        result.headline_eligible_v1 = task.headline_eligible_v1
        result.scoreable = task.scoreable
        # Build corpora for v2 quality scoring.
        # erec uses agent-text only — tool outputs excluded so codemap arm erec measures
        # agent comprehension, not whether the skill echoed the list back. The semble chunk
        # corpus feeds the semble-native chunk_hit_rate lens; erec/rrec stay the
        # codemap-native rdep-recall lens.
        exposure_corpus = result.output_text
        report_corpus = result.output_text[result.last_tool_text_offset :]
        semble_corpus = "\n".join(result.semble_results) or None
        result.quality = self.gt.score(
            task_id=task.id,
            output_text=result.output_text,
            exposure_corpus=exposure_corpus,
            report_corpus=report_corpus,
            tool_calls=result.tools.total,
            skill_result_text=result.skill_result_text or None,
            semble_result_text=semble_corpus,
        )
        answer_oracle = self.answer_oracles.get(task.id)
        if result.parity_arm and answer_oracle is not None:
            assessment = assess_answer_response(task.answer_task, report_corpus)
            evidence = score_evidence_metrics(
                answer_oracle,
                exposure_text=result.output_text,
                report_text=report_corpus,
                tool_calls=result.tools.total,
            )
            result.answer_contract_valid = assessment.strict_envelope_valid
            result.answer_diagnostic_only = assessment.diagnostic_only
            result.answer_pooling_eligible = assessment.pooling_eligible
            result.answer_error = assessment.error or ""
            result.quality.scored = True
            result.quality.erec = evidence.erec
            result.quality.rrec = evidence.rrec
            result.quality.deff = evidence.deff
            if assessment.answer is not None:
                answer_score = score_answer(
                    answer_oracle,
                    assessment.answer,
                    exposure_text=result.output_text,
                    report_text=report_corpus,
                    tool_calls=result.tools.total,
                )
                result.answer_scored = True
                result.answer_quality_score = answer_score.quality_score
                result.answer_correct = answer_score.correct
                result.answer_components = dict(answer_score.components)
                result.answer_graded_score = answer_score.graded_score
                result.answer_graded_components = dict(answer_score.graded_components)
                result.answer_failure_details = answer_failure_details(answer_oracle, assessment.answer)
        # read_crop tasks have no rdeps ground truth — score by keyword recall instead,
        # and exempt them from the codemap-skill-required guard (they use scan-query symbol
        # via Bash, not the Skill tool).
        if task.type == "read_crop":
            result.quality = score_read_crop(result.output_text, task.expected_keywords)
        if task.type in ("fix_single", "fix_multicaller"):
            result.quality = score_fix(
                result.agent_diff,
                task.expected_patch_keywords,
                task.expected_files,
                test_passed=result.targeted_test_passed,
            )
        # Degenerate-loop detection MUST precede the no-skill-call guard below. A codemap arm that
        # ignored the index and grepped its way through has skill == 0, which the no-call guard
        # would otherwise claim first (labelling it "codemap skill never called") — leaving the
        # ≥70% grep-ratio classification unreachable for blast-radius tasks. Ordering
        # it first lets a grep-heavy zero-skill run be labelled degenerate_grep_loop; a zero-skill
        # run that is NOT grep-heavy still falls through to the no-call guard.
        if result.arm == "codemap" and result.success:
            total_calls = result.tools.total
            grep_like = result.tools.grep + result.tools.bash_for_imports
            if total_calls > 0 and result.tools.skill == 0 and grep_like / total_calls >= 0.70:
                result.success = False
                result.error_type = "degenerate_grep_loop"
                result.error = (
                    f"codemap arm used no codemap skill; fell back to grep "
                    f"({grep_like}/{total_calls} grep-like calls = {grep_like / total_calls:.0%}); "
                    f"index not used"
                )
        # Codemap arm that never invoked the Skill tool (and did not already fail the degenerate
        # check above) is a failure — it fell back to grep/bash entirely, defeating the purpose.
        # fix tasks use Edit (not Skill), so exempt them from this guard.
        if (
            arm == "codemap"
            and result.tools.skill == 0
            and result.success
            and task.type not in ("read_crop", "fix_single", "fix_multicaller")
        ):
            result.success = False
            result.error = "codemap skill never called"
        # Semble arm: failure if never called semble, or all calls were permission-blocked.
        if arm == "semble" and result.success:
            effective_semble = result.tools.semble - result.tools.blocked
            if result.tools.semble == 0:
                result.success = False
                result.error = "semble tool never called"
            elif effective_semble <= 0:
                result.success = False
                result.error = "semble tool called but all invocations were blocked (permission denied)"
        # Combined arm: failure if no structural tool was ever called or all semble were blocked.
        if arm == "combined" and result.success:
            effective_semble = result.tools.semble - result.tools.blocked
            if result.tools.skill == 0 and result.tools.semble == 0:
                result.success = False
                result.error = "combined arm: neither codemap skill nor semble tool called"
            elif result.tools.skill == 0 and effective_semble <= 0:
                result.success = False
                result.error = "combined arm: all semble calls were blocked (permission denied)"
        # Skill-error failure: codemap/combined arm where the skill returned tool_use_error.
        # These runs fell back to grep — not measuring codemap benefit; exclude from metrics.
        if result.arm in ("codemap", "combined") and result.success and result.error_type == "skill_blocked":
            result.success = False
            result.error_type = "codemap_skill_errored"
            result.error = (
                "codemap skill returned <tool_use_error>; run fell back to grep — "
                "not a valid codemap measurement; re-run after fixing skill invocation"
            )
        # Plain arm isolation check: flag any run that read the codemap index JSON via Bash.
        # Currently low-yield (agents usually guess the wrong home-dir path) but the vector is
        # real — a single correct path read hands the agent the full index for free.
        if result.arm in ("plain", "A_plain") and result.tools.index_reads > 0:
            result.error_type = "plain_index_contamination"
            result.error = (
                f"plain arm read .cache/codemap/ or .cache/scan/ index via Bash "
                f"({result.tools.index_reads} read(s)) — isolation violated; exclude from baseline"
            )
            result.success = False
        result.contaminated = bool(
            result.contaminated or (result.parity_arm == "A_plain" and result.tools.index_reads > 0)
        )
        result.incomplete = not result.success and not result.usage_complete
        if result.parity_arm:
            result.treatment_adherence = treatment_adherence(
                result.parity_arm,
                codemap_use_compliance=result.codemap_compliant if result.parity_arm == "C_strict" else None,
                contaminated=result.contaminated,
            )
        self._write_tool_log(result)
        style = _FAIL_STYLE if not result.success else _ARM_STYLE.get(arm, "")
        print_fn(_Text(_run_line(run_n, total_runs, task, model_short, arm, result), style=style))
        return result

    def run(self, metadata: dict) -> list[BenchmarkRun]:
        """Execute all benchmark runs and return the accumulated results."""
        total_runs = len(self.tasks) * len(self.arms) * len(self.models) * self.repeat
        if all(parity_arm_identity(arm) for arm in self.arms):
            # Persist the assigned denominator before the first provider call so an
            # immediate interruption cannot turn a zero-observed canonical run into
            # a missing headline.
            self._update_canonical_reporting(metadata)
            self._save_snapshot(metadata)
        with make_progress(_console) as progress:
            outer = progress.add_task("running", total=total_runs)
            for run_n, (task, model_short, model_id, arm, rep) in enumerate(self._iter_combos(), start=1):
                sub = progress.add_task(f"  {task.id} | {model_short} | {arm}", total=None)
                progress.update(outer, description=f"{task.id} | {model_short} | {arm}")

                result = self._run_single(
                    task,
                    model_short,
                    model_id,
                    arm,
                    run_n,
                    total_runs,
                    # soft_wrap keeps the wide run line on one line; the console's own width frames
                    # legends and rules, and must not fold a row whose columns are meant to line up.
                    print_fn=lambda text: progress.console.print(text, markup=False, highlight=False, soft_wrap=True),
                    metadata=metadata,
                    update_fn=_AgenticSubProgressUpdate(progress, sub),
                    repetition=rep + 1,
                )
                progress.remove_task(sub)
                progress.advance(outer)
                self.results.append(result)
                # snapshot after append, not inside _run_single — a snapshot taken before
                # append always lags self.results by one entry, silently dropping the
                # last-iterated task (BA-16, last in tasks-agentic.json) from every output JSON
                self._update_canonical_reporting(metadata)
                self._save_snapshot(metadata)
            reporting = metadata.get("agentic_reporting")
            if isinstance(reporting, Mapping):
                summaries = reporting.get("summaries_by_model")
                if isinstance(summaries, Mapping):
                    for model_short, _ in self.models:
                        summary = summaries.get(model_short)
                        if not isinstance(summary, Mapping):
                            continue
                        for line, arm in summary_lines(summary):
                            progress.console.print(
                                _Text(f"MODEL {model_short}  {line}", style=_ARM_STYLE.get(arm, "")),
                                markup=False,
                                highlight=False,
                                soft_wrap=True,
                            )
        return self.results

    def _write_tool_log(self, result: BenchmarkRun) -> None:
        """Append one JSON line to the tool-call log for post-run investigation."""
        with self.log_path.open("a") as fh:
            fh.write(
                json.dumps(
                    {"task_id": result.task_id, "arm": result.arm, "model": result.model, "calls": result.tool_log}
                )
                + "\n"
            )

    def _save_snapshot(self, metadata: dict) -> None:
        """Atomically overwrite the results JSON with the current snapshot.

        Called after every run onto a single rolling file. The payload is written to a temp file in the output directory
        first, then ``os.replace`` swaps it into place — an atomic rename on POSIX. A SIGINT/kill mid-write therefore
        leaves the results file either fully old or fully new, never a truncated mix that would lose every accumulated
        run. The temp file is removed if serialisation fails so no ``.tmp`` residue accumulates.
        """
        import tempfile

        serialised = []
        for r in self.results:
            d = asdict(r)
            for key in (
                "skill_result_text",
                "codemap_results",
                "semble_results",
                "last_tool_text_offset",
                "targeted_test_passed",
            ):
                d.pop(key, None)
            serialised.append(d)
        payload = {"metadata": metadata, "results": serialised}
        out = self.output_path
        fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=f".{out.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp_name, out)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _run_from_snapshot_row(row: dict) -> BenchmarkRun:
    """Rebuild one :class:`BenchmarkRun` from a persisted snapshot row.

    Only fields the current dataclass declares are copied, so a snapshot written by an older or newer
    schema still yields a renderable run instead of raising on an unknown key.

    Args:
        row: One entry of a snapshot's ``results`` list.

    Returns:
        The reconstructed run, with its nested tool counts and quality score restored.
    """

    def _filtered(cls: type, raw: object) -> dict:
        names = {f.name for f in fields(cls)}
        return {k: v for k, v in raw.items() if k in names} if isinstance(raw, dict) else {}

    run = BenchmarkRun(**_filtered(BenchmarkRun, {k: v for k, v in row.items() if k not in ("tools", "quality")}))
    run.tools = ToolCounts(**_filtered(ToolCounts, row.get("tools")))
    run.quality = QualityScore(**_filtered(QualityScore, row.get("quality")))
    return run


def _render_report_from_snapshot(snapshot: Path, tasks_path: Path, output: Path = None) -> Path:
    """Re-render the markdown report for an already-recorded snapshot, without running any model.

    Reporting code evolves after a paid run has been recorded, and the snapshot carries every figure
    the report needs, so replaying it is the supported way to correct a report instead of re-spending
    on the same cells. The input snapshot and its original report are never overwritten.

    Args:
        snapshot: Path to a ``code-YYYY-MM-DD.json`` snapshot.
        tasks_path: Task suite the snapshot was run from, used for task ids and types.
        output: Markdown output path; defaults to ``<snapshot stem>-rerender.md`` beside the snapshot.

    Returns:
        Path to the written markdown report.
    """
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    runs = [_run_from_snapshot_row(row) for row in data.get("results", [])]
    if not runs:
        sys.exit(f"ERROR: no result rows in {snapshot}")
    tasks = load_tasks_with_provenance(Path(tasks_path))
    ran_ids = {r.task_id for r in runs}
    tasks = [t for t in tasks if t.id in ran_ids]
    metadata = dict(data.get("metadata") or {})
    metadata.setdefault("date", snapshot.stem.replace("code-", ""))
    report_path = Path(output) if output else snapshot.with_name(f"{snapshot.stem}-rerender.md")
    report_path.write_text(Report(runs, tasks, metadata).render(), encoding="utf-8")
    print(f"→ Report: {report_path}  ({len(runs)} rows replayed)")
    return report_path


def main(
    repo_path: Path = None,
    index: Path = None,
    tasks_file: Path = Path("benchmarks/suites/tasks-agentic.json"),
    readcrop_tasks_path: Path = READCROP_TASKS_PATH,
    fix_single_tasks_path: Path = FIX_SINGLE_TASKS_PATH,
    fix_multi_tasks_path: Path = FIX_MULTI_TASKS_PATH,
    patch_tasks_path: Path = PATCH_TASKS_PATH,
    manifest_path: Path = PARITY_MANIFEST_PATH,
    study: str = "agentic",
    model: str = None,
    arm: str = None,
    run_all: bool = False,
    tasks: list[str] = None,
    report: bool = False,
    output: Path = None,
    repeat: int = DEFAULT_REPETITIONS,
    scope_sha256: str = None,
    resolve_scope: bool = False,
    dry_run: bool = False,
    run_dir: Path = None,
    paid_approval: str = None,
    timeout: int = 600,
    render_report: Path = None,
    index_relocation_path: Path = None,
    change_impact_answers: Path = None,
) -> None:
    """Codemap skill benchmark — agent exploration cost with vs without structural context.

    Args:
        repo_path: Path to the indexed repo; omitted only for scope resolution.
        index: Explicit index path (auto-discovered if omitted).
        tasks_file: Task definition file.
        readcrop_tasks_path: Locked source-contract task suite for ``--study readcrop``.
        fix_single_tasks_path: Locked single-file executable suite for ``--study fix-single``.
        fix_multi_tasks_path: Locked complete-caller task suite for ``--study fix-multi``.
        patch_tasks_path: Locked historical executable task suite for ``--study patch``.
        manifest_path: Provider-neutral methodology lock defining the shared suite.
        study: ``agentic`` historical runner or canonical ``readcrop``/``fix-single``/``fix-multi``/``patch`` stage.
        model: Run a single model tier (default: all — haiku/sonnet/opus).
        arm: Run one canonical or legacy arm. The default runs canonical A/B/C.
        run_all: Run all tasks in the selected arms.
        tasks: Run specific task IDs only.
        report: Write markdown report alongside JSON.
        output: JSON output path (auto-named if omitted).
        repeat: Repeat runs per (task, arm, model) cell; median aggregated.
        scope_sha256: Exact derived scope hash required for nondefault repetitions.
        resolve_scope: Print the selected no-model scope as JSON and exit.
        dry_run: Print plan without running claude.
        run_dir: New immutable artifact directory required for a paid P1 stage.
        paid_approval: Scope-prefix token emitted by the current dry-run command.
        timeout: Per-cell timeout for the change-impact stage only.
        render_report: Re-render the markdown report for an existing snapshot JSON and exit. No model
            runs and no writes to the snapshot — the recorded rows are replayed through the current
            reporting code into a ``-rerender.md`` sibling (or ``--output``).
        index_relocation_path: Relocation provenance written when this run's index was moved into an
            isolated worktree; absent for a run at the canonical managed clone.
        change_impact_answers: Untrusted JSONL answers for the separate no-model change-impact diagnostic.
    """
    if study == "change-impact":
        impact_model = model or "sonnet"
        if impact_model not in MODELS:
            sys.exit(f"change-impact model must be one of {', '.join(MODELS)}")
        if (
            any(
                value is not None
                for value in (
                    tasks,
                    repo_path,
                    index,
                    arm,
                    output,
                    index_relocation_path,
                    render_report,
                )
            )
            or run_all
            or report
            or repeat != DEFAULT_REPETITIONS
            or (
                Path(tasks_file) != Path("benchmarks/suites/tasks-agentic.json")
                or Path(manifest_path) != PARITY_MANIFEST_PATH
                or Path(readcrop_tasks_path) != READCROP_TASKS_PATH
                or Path(fix_single_tasks_path) != FIX_SINGLE_TASKS_PATH
                or Path(fix_multi_tasks_path) != FIX_MULTI_TASKS_PATH
                or Path(patch_tasks_path) != PATCH_TASKS_PATH
            )
        ):
            sys.exit("change-impact supports only its full fixed task/arm matrix")
        run_change_impact_stage(
            provider="claude",
            dry_run=dry_run,
            resolve_scope_requested=resolve_scope,
            answers_file=change_impact_answers,
            output_dir=run_dir,
            model=impact_model,
            paid_approval=paid_approval,
            timeout=timeout,
            runtime_factory=impact_runtime,
            scope_sha256=scope_sha256,
        )
        return
    if timeout != 600:
        sys.exit("--timeout applies only to --study change-impact")
    if change_impact_answers is not None:
        sys.exit("diagnostic impact answers require --study change-impact")
    if render_report:
        _render_report_from_snapshot(Path(render_report), tasks_file, output)
        return
    # fire passes CLI string args regardless of type annotation — coerce Path args explicitly.
    if index is not None:
        index = Path(index)
    tasks_file = Path(tasks_file)
    readcrop_tasks_path = Path(readcrop_tasks_path)
    fix_single_tasks_path = Path(fix_single_tasks_path)
    fix_multi_tasks_path = Path(fix_multi_tasks_path)
    patch_tasks_path = Path(patch_tasks_path)
    manifest_path = Path(manifest_path)
    if repo_path is not None:
        repo_path = Path(repo_path)
    if run_dir is not None:
        run_dir = Path(run_dir)
    if output is not None:
        output = Path(output)
    try:
        index_relocation = load_index_relocation(None if index_relocation_path is None else Path(index_relocation_path))
    except ValueError as exc:
        sys.exit(str(exc))
    selected_tasks = [part.strip() for part in tasks.split(",") if part.strip()] if isinstance(tasks, str) else tasks

    if study in {"readcrop", "fix-single", "fix-multi", "patch"}:
        stage_tasks_path = {
            "readcrop": readcrop_tasks_path,
            "fix-single": fix_single_tasks_path,
            "fix-multi": fix_multi_tasks_path,
            "patch": patch_tasks_path,
        }[study]
        _run_claude_p1_stage(
            study=study,
            repo_path=repo_path,
            index=index,
            tasks_path=stage_tasks_path,
            manifest_path=manifest_path,
            selected_ids=selected_tasks,
            model=model,
            run_dir=run_dir,
            paid_approval=paid_approval,
            dry_run=dry_run,
            resolve_scope=resolve_scope,
            index_relocation=index_relocation,
        )
        return
    if study != "agentic":
        sys.exit("study must be 'agentic', 'readcrop', 'fix-single', 'fix-multi', or 'patch'.")

    if not run_all and not tasks and not arm and not dry_run and not resolve_scope:
        sys.exit("Specify --run_all to run everything, or narrow with --tasks / --arm.")
    if repeat < 1:
        sys.exit("Agentic repeat must be a positive integer.")

    # The default is the locked provider-parity matrix. Legacy labels remain
    # available as explicit one-arm compatibility runs without changing their
    # historical prompts, timeouts, or success semantics.
    arms = [arm] if arm else list(AGENTIC_ARMS)
    canonical_requested = any(candidate in ARM_CONTRACTS for candidate in arms)

    # ── Load tasks ────────────────────────────────────────────────────────
    if not tasks_file.exists():
        sys.exit(f"Tasks file not found: {tasks_file}")
    try:
        all_tasks = (
            load_tasks_with_provenance(tasks_file, manifest_path)
            if canonical_requested
            else load_legacy_tasks(tasks_file)
        )
    except ValueError as exc:
        sys.exit(str(exc))
    if selected_tasks:
        all_tasks = [t for t in all_tasks if t.id in selected_tasks]
    if not all_tasks:
        sys.exit("No tasks to run.")

    models_to_run: list[tuple[str, str]] = [(model, MODELS[model])] if model else list(MODELS.items())
    if canonical_requested:
        try:
            scope = resolve_agentic_scope(
                manifest_path,
                task_ids=[task.id for task in all_tasks],
                arms=arms,
                models=[model_name for model_name, _ in models_to_run],
                repetitions=repeat,
            )
        except (KeyError, ValueError) as exc:
            sys.exit(str(exc))
        if resolve_scope:
            print(json.dumps(scope, sort_keys=True))
            return
        if repeat != DEFAULT_REPETITIONS and scope_sha256 != scope["scope_sha256"]:
            sys.exit("Nondefault Claude agentic repetitions require the exact derived scope SHA-256.")
        if scope_sha256 is not None and scope_sha256 != scope["scope_sha256"]:
            sys.exit("Claude agentic scope SHA-256 does not match the selected coordinates.")
    elif resolve_scope or scope_sha256 is not None:
        sys.exit("Claude agentic scope hashes apply only to canonical A/B/C arms.")

    if repo_path is None:
        sys.exit("repo_path is required unless --resolve-scope is used.")
    repo_path = Path(repo_path)

    # ── Locate prerequisites (validated before any run starts) ──────────
    repo_path = repo_path.resolve()
    index_path = find_index(repo_path, index)

    if not arm:
        print("[→ note:        defaulting to canonical A_plain/B_auto/C_strict parity arms]")
    if canonical_requested:
        try:
            _validate_parity_runtime(repo_path, index_path, manifest_path, index_relocation)
        except (OSError, ValueError) as exc:
            sys.exit(str(exc))

    if "semble" in arms or "combined" in arms:
        check_semble_mcp()
    total_runs = len(all_tasks) * len(arms) * len(models_to_run) * repeat

    model_names = ", ".join(m for m, _ in models_to_run)

    # ── Output path + tool-call log ───────────────────────────────────────
    date_slug = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_path = output or (RESULTS_DIR / f"code-{date_slug}.json")
    # Tool-call log: one JSON line per run, for post-run investigation of bash commands
    log_dir = Path(".temp") / f"bench-{date_slug}"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tool-calls.jsonl"

    print(f"[→ repo:        {repo_path}]")
    print(f"[→ index:       {index_path}]")
    print(f"[→ models:      {model_names}]")
    print(f"[→ tasks:       {len(all_tasks)}, arms: {len(arms)}, models: {len(models_to_run)}, repeat: {repeat}]")
    print(f"[→ total runs:  {total_runs}]")
    print(f"[→ tool log:    {log_path}]")

    if dry_run:
        for task, model_short, _, arm, rep in _iter_combos(all_tasks, models_to_run, arms, repeat):
            print(f"  [DRY RUN] {task.id} ({task.type}) | {model_short} | {arm} | rep={rep + 1}/{repeat}")
        return

    if "codemap" in arms:
        _sample_runner = ModelRunner("haiku", MODELS["haiku"], repo_path)
        _sample = _sample_runner._system_prompt("fix", "codemap")
        print(f"[→ codemap arm:  skill + /codemap:query available ({len(_sample)} chars for fix type)]")
    if "combined" in arms:
        _sample_runner = ModelRunner("haiku", MODELS["haiku"], repo_path)
        _sample = _sample_runner._system_prompt("fix", "combined")
        print(f"[→ combined arm: both codemap + semble available ({len(_sample)} chars for fix type)]")
    output_path = _unique_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    metadata = {
        "date": datetime.now(timezone.utc).isoformat(),
        "experiment_revision": (
            all_tasks[0].experiment_revision if canonical_requested else LEGACY_EXPERIMENT_REVISION
        ),
        "models": model_names,
        "repo": str(repo_path),
        "index": str(index_path),
        "task_count": len(all_tasks),
        "repeat": repeat,
        "scope": scope if canonical_requested else None,
    }

    # ── Construct benchmark and show ground truth info ────────────────────
    benchmark = Benchmark(
        tasks=all_tasks,
        arms=arms,
        models=models_to_run,
        repo_path=repo_path,
        index_path=index_path,
        output_path=output_path,
        log_path=log_path,
        repeat=repeat,
    )
    print(f"[→ quality gt:   {len(benchmark.gt.expected)} tasks with rdep ground truth]")

    # ── Run ───────────────────────────────────────────────────────────────
    all_results = benchmark.run(metadata)

    # ── Report ────────────────────────────────────────────────────────────
    if report:
        report_obj = Report(all_results, all_tasks, {**metadata, "date": date_slug})
        report_md = report_obj.render()
        report_path = output_path.with_suffix(".md")
        report_path.write_text(report_md)
        print(f"\n→ Report: {report_path}")

    print(f"→ Data:   {output_path}")


if __name__ == "__main__":
    try:
        fire.Fire(main)
    except ValueError as exc:
        message = f"ERROR: {exc}"
        if _console.is_terminal:
            _console.print(_Text(message, style="bold red"))
        else:
            print(message, file=sys.stderr)
        raise SystemExit(2) from None
