#!/usr/bin/env python3
"""Claude-only Codemap structural benchmark across all real-code task series.

## What this measures

Two arms answer the same structural questions about the target repository:

  plain    — Grep / Bash / Read / Glob only; no scan-query; no Skill tool
  codemap  — same tools plus scan-query via PATH and the Skill tool

Task types:
  SE — symbol_extraction   (locate symbol lines in source)
  FN — fn_call_graph       (caller name recall for a function)
  RV — review_assistance   (doc/coverage/rdep metrics for code review)
  CQ — code_quality        (coupled, xrefs, combined health checks)
  BR — develop_blast_radius (caller recall >=70% before modifying a function)
  DG — debug_from_trace    (root-cause function + file from a traceback)
  FT — feature_scaffolding (files to create or modify for a new feature)
  RI — real_issue          (files relevant to a real GitHub issue)
  DI — diff_impact         (callers and tests affected by a staged change)
  GR — graph_reasoning     (centrality, paths, and transitive function impact)
  MB — module_blast_radius (importer recall for a changed module)

Primary metric:
  token_ratio = codemap_input_tokens / plain_input_tokens per task (lower = better for codemap)

Secondary:
  accuracy = fraction of tasks where key metric matches ground truth within tolerance

## Quick start

  # Build index once (excluded from timing)
  python plugins/codemap-py/bin/scan-index --root ./<repo-dir>

  # Run all tasks, both arms, haiku model
  python benchmarks/run-claude-structural.py --repo-path ./<repo-dir> --run-all

  # Single task, codemap arm only
  python benchmarks/run-claude-structural.py --repo-path ./<repo-dir> \\
      --tasks "['SE-01']" --arm codemap --model haiku

## Requirements

  - claude CLI on PATH
  - Pre-built codemap index in .cache/codemap/<proj>.json or .cache/scan/<proj>.json
  - pip install --group pyproject.toml:bench
"""

from __future__ import annotations

import subprocess  # noqa: F401
import sys
import tempfile  # noqa: F401
from pathlib import Path

import fire

# benchmarks/ is not a package; make its private shared packages importable
# regardless of how this script is launched (direct path, symlink, or any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bench_claude.structural.cli import _StructuralRunLoop, main  # noqa: F401
from _bench_claude.structural.config import (  # noqa: F401
    _ARM_ALLOWED,
    _CMD,
    _DIFF_IMPACT_TYPE,
    _EXTERNAL_TASK_TYPE,
    ARM_CONTRACTS,
    ARMS,
    LEGACY_EXPERIMENT_REVISION,
    PARITY_ARM_BY_LEGACY_ARM,
    PARITY_ARMS,
    PARITY_EXPERIMENT_REVISION,
    PARITY_MANIFEST_FILE,
    PRIMARY_SUITE_HASH,
    PRIMARY_SUITE_RAW_HASH,
    SandboxError,
    _arm_orders_by_task,
    _console,
    _pin_pytest_interpreter,
)
from _bench_claude.structural.evaluators import (  # noqa: F401
    _EVALUATORS,
    _SHARED_EVALUATORS,
    EvaluatorRegistry,
    _answer_region,
    _count_tol_detail,
    _evaluate_debug,
    _evaluate_develop_br,
    _evaluate_diff_impact,
    _evaluate_feature,
    _evaluate_graph_central,
    _evaluate_graph_fn_blast,
    _evaluate_graph_path,
    _evaluate_module_blast_radius,
    _evaluate_oss,
    _evaluate_real_issue,
    _evaluate_rv,
    _evaluate_shared_task,
    _evaluate_symbol,
    _evaluator_provenance,
    _extract_int,
    _int_close,
    _module_compatible,
    _module_first_pos,
    _module_mentioned,
    _ri_file_matches,
    _stem_matches,
    _wrap_bench_evaluator,
)
from _bench_claude.structural.models import BenchQuality, BenchRun  # noqa: F401
from _bench_claude.structural.prompts import _build_system_prompt  # noqa: F401
from _bench_claude.structural.report import (  # noqa: F401
    _arm_extracted,
    _arm_pairs,
    _effective_recall,
    _is_self_consistency,
    _paired_accuracy,
    _print_self_consistency,
    _print_summary,
    _safe_ratio,
    _save_results,
    _token_ratio_table,
    _workflow_type_of,
    asdict,
)
from _bench_claude.structural.runner import BenchRunner  # noqa: F401
from _bench_claude.structural.sandbox import (  # noqa: F401
    DiffImpactStager,
    DirtyTreeError,
    PatchSandbox,
    _extract_diff,
)
from _bench_claude.structural.tasks import (  # noqa: F401
    TaskSelection,
    _apply_profile,
    _correct_by_task,
    _gate_ri,
    _index_sha,
    _is_dev_task,
    _load_primary_parity_contract,
    _load_resume_cache,
    _load_tasks_file,
    _normalize_external_task,
    _prompt_hash,
    _repo_sha,
    _resume_key,
    _run_from_cached,
    _select_tasks,
    _task_hash,
    _tiered_tasks,
    _validate_primary_runtime,
)
from _bench_claude.structural.telemetry import (  # noqa: F401
    _SCAN_QUERY_SUBCOMMANDS,
    _is_contaminating_access,
    _max_turns_for_task,
    _parse_batch_subcommands,
    _parse_scan_query_subcommand,
)

if __name__ == "__main__":
    fire.Fire(main)
