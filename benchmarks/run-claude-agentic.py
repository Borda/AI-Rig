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

from __future__ import annotations

import json  # noqa: F401
import os  # noqa: F401
import subprocess  # noqa: F401
import sys
import time  # noqa: F401
from pathlib import Path

import fire

# benchmarks/ is not a package; make its private shared packages importable
# regardless of how this script is launched (direct path, symlink, or any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bench_claude.agentic.cli import (  # noqa: F401
    Benchmark,
    _iter_combos,
    deterministic_arm_order,
    main,
    score_answer,
    summarize_agentic,
)
from _bench_claude.agentic.config import (  # noqa: F401
    _FIX_MULTI_QUERY_ARGUMENTS,
    _FIX_SINGLE_QUERY_ARGUMENTS,
    _PATCH_QUERY_ARGUMENTS,
    FIX_MULTI_TASKS_PATH,
    FIX_SINGLE_TASKS_PATH,
    PARITY_MANIFEST_PATH,
    READCROP_TASKS_PATH,
    AgenticOracle,
    _claude_codemap_evidence,
    _claude_event_summary,
    _console,
    _frozen_index_recovery_attempted,
    _is_inside_workspace,
    _outside_workspace_path_evidence,
    _query_arguments_from_bash,
    _workspace_containment_roots,
    build_readcrop_contract,
    load_claude_fix_multi_tasks,
    load_claude_fix_single_tasks,
    parse_claude_readcrop_events,
    presentation,
    readcrop_prompt,
    resolve_claude_fix_multi_scope,
    resolve_claude_fix_single_scope,
    resolve_readcrop_scope,
    resolve_relative_base,
)
from _bench_claude.agentic.discovery import (  # noqa: F401
    _derive_module_name,
    _scan_repo_importers,
    _tool_key_arg,
    check_semble_mcp,
    count_tokens,
    find_index,
)
from _bench_claude.agentic.evidence import (  # noqa: F401
    _absolute_filetool_pattern,
    _claude_evidence_isolation_settings,
)
from _bench_claude.agentic.ground_truth import GroundTruth  # noqa: F401
from _bench_claude.agentic.models import (  # noqa: F401
    ARM_CONTRACTS,
    BenchmarkRun,
    QualityScore,
    Task,
    ToolCounts,
    parity_arm_identity,
)
from _bench_claude.agentic.paid import (  # noqa: F401
    PATCH_PYTEST_ENV,
    _claude_fix_prompt,
    _format_claude_stage_row,
    _patch_snapshot_files,
    _print_claude_paid_command,
    _require_claude_paid_request,
    _resolve_claude_paid_scope,
    change_impact_source_fingerprint,
    execute_fix_multi_patch,
    execute_fix_single_patch,
    impact_runtime,
    run_claude_paid_stage,
)
from _bench_claude.agentic.provenance import (  # noqa: F401
    _codemap_use_attempted,
    _invokes_scan_query,
    _sha256_file,
    _validate_parity_runtime,
)
from _bench_claude.agentic.report import Report, _run_line, agentic_reporting  # noqa: F401
from _bench_claude.agentic.runner import ModelRunner  # noqa: F401
from _bench_claude.agentic.scope import (  # noqa: F401
    AGENTIC_ARMS,
    MODELS,
    _delivered_prompt_hash,
    materialize_agentic_prompt,
    resolve_agentic_scope,
    run_cost_usd,
)
from _bench_claude.agentic.scoring import aggregate, score_fix, score_read_crop  # noqa: F401
from _bench_claude.agentic.tasks import (  # noqa: F401
    _canonical_agentic_row,
    canonical_task_hash,
    load_legacy_tasks,
    load_task_suite,
    load_tasks_with_provenance,
    semantic_suite_hash,
)

if __name__ == "__main__":
    fire.Fire(main)
