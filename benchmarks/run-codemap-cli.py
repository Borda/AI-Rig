#!/usr/bin/env python3
"""Provider-neutral Codemap scan-query benchmark for accuracy, latency, and coverage.

## Motivation

The `codemap` plugin scans a Python codebase once (`ast.parse`) into a structural JSON index; agents answer
structural questions (rdeps, deps, centrality, import paths) with one `scan-query` call instead of many Glob/Grep
passes. This benchmarks the `scan-query` binary against cold grep baselines — NOT via the Claude API, agents,
or live tool-call counts.

## Goal

Quantify codemap's benefit (coverage, accuracy, latency, query-shape). Frozen task set:
benchmarks/suites/tasks-code.json — 15 tasks grouped by skill:
  B-01–B-05  bug/fix scenarios    (blast radius before touching faulty code)
  F-01–F-05  feature scenarios    (coupling risk before hooking in)
  R-01–R-05  refactor scenarios   (full structural picture before restructuring)

Suite C — Coverage gap: structural completeness of cold grep vs codemap.

  Code  Name                     What it measures                            Pass threshold
  ----  -----------------------  ------------------------------------------  ---------------
  C1    coverage-gap             codemap finds >=10% more importers          gap >= 10%
  C2    infeasible-path-fraction >=50% of 2+ hop paths not grep-             fraction >= 50%
                                 discoverable in 1 call
  C3    leverage-ratio           structural context / cold exploration       ratio >= 2.0x
                                 call ratio across all 15 tasks

Suite A — Accuracy: AST-verified rdeps precision + boundary-grep recall floor.

  Code  Name         What it measures                                   Pass threshold
  ----  -----------  -------------------------------------------------  ---------------
  A1    rdeps-high   AST precision + grep recall; tiers high /          precision >= 0.90
                     very-high / moderate-high; EVERY module must pass  recall    >= 0.85
  A2    rdeps-low    AST precision; ALL other tiers (low / low-moderate precision = 1.00
                     / moderate) — catch-all, so no task is ever
                     silently ungraded (A1 ∪ A2 = every task)
  A3    fp-rate      overall AST false-positive rate across all tasks   FP rate < 5%

Suite L — Latency: wall-clock cost of codemap queries vs cold grep pipelines.

  Code  Name         What it measures                                   Pass threshold
  ----  -----------  -------------------------------------------------  ---------------
  L1    central      median of 5 runs of scan-query central --top 5     median < 200 ms
  L2    rdeps        median of 5 runs of scan-query rdeps across 3      median < 100 ms
                     high-risk task modules
  L3    index-build  one scan-index run amortized over 10 invocations   amortized < 500 ms
                     (total build time / 10)
  L4    speedup      codemap (L1+L2) vs cold grep baseline               >= 2x faster

Suite Q — Query shape: validates the OUTPUT SHAPE of the scan-query commands a skill would inject; it
does NOT invoke the skill, exercise the SKILL.md injection block, or prove the context is wired into a
prompt. (check_injection.py separately audits SKILL.md/agent files for injection MARKERS.)

  Code        Skill queries       What is validated              Pass threshold
  ---------   ------------------  -----------------------------  ---------------
  Q_fix       develop:fix         per-task queries (5 tasks)     JSON valid
  Q_feature   develop:feature     per-task queries (5 tasks)     JSON valid
  Q_refactor  develop:refactor    per-task queries + rdeps/deps  all present,
                                  (5 tasks)                      rdeps+deps valid

Suites D, B, R, K, U are DETERMINISTIC CORRECTNESS checks (suite name "correctness"): each builds a
self-contained fixture repo in a tmp dir whose ground truth is KNOWN by construction (N importers, an
exactly-corrupted index, a single broken sphinx xref), so — unlike S/H/X — a pass is genuine
independent-oracle correctness, and they JOIN the primary verdict. They assert the user-visible CLI
contract against an arbitrary target repo (a product acceptance check), not the per-edge-case matrix
already unit-tested in plugins/codemap-py/tests/. Each needs scan-index to build its fixture; when it is
absent the suite skips (like S/H/X). They run OFFLINE — independent of the repo_path / index_path.

  Suite D — diff-impact: changed module/symbol detection, risk tiers (HIGH >=5 importers / MODERATE /
                         LOW), test-impact union, single coverage block, ``--base`` scoping, unmapped file.
  Suite B — batch: N valid + 1 invalid → exit 0, per-item order, invalid item top-level error + ok:false,
                   one shared coverage block, byte-equivalence of a batched result vs its standalone form.
  Suite R — src_roots: two configured roots → naming from each root, collision winner under a configured
                       root, src_roots meta recorded.
  Suite K — self-check: corrupt index variants (missing key / bad version / wrong type / truncated JSON)
                        → exit 3 + parseable JSON error, never a partial serve.
  Suite U — uncovered/xrefs: fixture with KNOWN counts (2 undocumented public fns, 1 broken sphinx xref)
                             → exact counts (replaces the LLM bench's circular scan-query-derived GT).

Suites S, H, X are SELF-CONSISTENCY / DETERMINISM checks, not independent-correctness: their ground
truth in tasks-bench.json is derived from scan-query's own output, so a pass confirms determinism /
index-version stability against a frozen snapshot, not correctness. They run on a separate track
EXCLUDED from the primary verdict (below), each passing on an exact/tolerant match to that snapshot:
  Suite S — Symbol lookup (SE-01..SE-05): scan-query symbol start_line within ±3 of gt; S2 = all pass.
  Suite H — Health (CQ-01..CQ-05): undocumented/uncovered total == gt.count; H1/H2 = all pass.
  Suite X — Xrefs broken (CQ-04): ``xrefs --broken`` count + target set == gt; X1 = all pass.

Index path resolution: .cache/codemap/ is checked before .cache/scan/ (``scan-index --root`` default).

## Requirements

  - Python 3.8+ (stdlib for C/A/L/Q/X; pandas+rich for reporting); git on PATH
  - A pytorch-lightning clone + pre-built index (python3 plugins/codemap-py/bin/scan-index --root <clone>)
  - scan-query on PATH or at plugins/codemap-py/bin/scan-query (found automatically)
  - benchmarks/suites/tasks-bench.json present for S/H/X (auto-skipped if absent)

## Quick start

    # Full benchmark + markdown report (C/A/L/Q always run; S/H/X when tasks-bench.json present)
    python benchmarks/run-codemap-cli.py --repo-path .sandbox/pytorch-lightning --report

    # Verify task modules exist in the index; non-default index via --index-path /path/to/index.json
    python benchmarks/run-codemap-cli.py --verify-tasks --repo-path .sandbox/pytorch-lightning

## Where the benchmark fits in the full flow

  A develop/oss skill injects a structural-context block (scan-query rdeps/deps) or skips it when the
  index/plugin is absent. This script runs AFTER that: Suite Q checks query output shape; C/A/L compare
  the WITH-codemap path against a cold Glob/Grep/Read baseline (agent USE of the context is out of scope).

## How each suite computes its metrics

  Each suite's exact formula lives in its function docstring (the authoritative source); thresholds
  are the tables above plus THRESHOLDS. Honesty points: Suite A judges precision against an INDEPENDENT
  AST resolver (aliased/relative/re-export importers not penalised) with grep only a recall FLOOR, and
  a scan-query failure FAILS the scenario (never precision 1.0); Suite L's L3 amortizes build over
  _QUERIES_PER_SESSION (stated assumption; expected to fail on large repos, verdict owns it) and L4
  reports warm-only (gate) plus build-inclusive speedup; Suite Q validates output SHAPE only.

## Output

  Default: stdout = human verdict + self-consistency lines + summary envelope (JSON) + report path
  (with ``--report``); stderr = progress; markdown report at benchmarks/results/code-YYYY-MM-DD.md.
  ``--json-only``: stdout = one compact JSON object per scenario (JSONL) then the summary envelope; human
  logs, progress bar, and report suppressed.

## JSON output schema (per-scenario lines + summary envelope)

  Each scenario line mirrors the ScenarioResult dataclass fields (see :func:`emit`): scenario, name,
  suite, passed, result (suite-specific measurement dict), threshold, notes.

  Final summary envelope (last stdout line): verdict (PRIMARY correctness), scenarios_passed/total,
  primary {passed,total} (verdict basis), self_consistency {verdict ∈ CONSISTENT|PARTIAL|INCONSISTENT|
  SKIPPED, passed, total} (S/H/X determinism track, NOT in the verdict), suites {<suite>:{passed,total}},
  hardware (platform/processor/cpu_count/python), and date/repo/index.

## Verdict thresholds (single source of truth — compute_verdict in source)

  SCENARIO-based over the PRIMARY suites (calls C, accuracy A, latency L, query-shape Q, and the
  deterministic correctness suites D/B/R/K/U under suite name "correctness"), each checked against an
  independent oracle. Self-consistency suites (symbol/health/xrefs) use frozen scan-query-derived GT on
  a separate track that NEVER contributes, so circular passes cannot float the verdict. With P/T =
  primary passed/total: PASS = P==T; PARTIAL = P/T >= 0.50; FAIL = P/T < 0.50 or T==0. FAIL headroom is
  real: A's AST oracle can mark codemap wrong, L3 fails on large repos, and a correctness suite fails on
  any CLI-contract regression against its fixture.

Full scenario definitions live in benchmarks/suites/*.json; pass criteria in compute_verdict below.
"""

from __future__ import annotations

# ``subprocess`` stays a module attribute here: a test patches ``run`` on it through this module.
import sys
from pathlib import Path

import fire

# benchmarks/ is not a package; make its private shared packages importable
# regardless of how this script is launched (direct path, symlink, or any cwd).
sys.path.insert(0, str(Path(__file__).resolve().parent))

import subprocess  # noqa: F401

from _bench_common.codemap_discovery import find_codemap_bin  # noqa: F401
from _bench_query.cli import main
from _bench_query.cold import _run, time_command, time_commands  # noqa: F401
from _bench_query.fixtures import (  # noqa: F401
    _Checklist,
    _correctness_scenario,
    _fixture_git,
    run_correctness_batch,
    run_correctness_diff_impact,
    run_correctness_self_check,
    run_correctness_src_roots,
    run_correctness_uncovered_xrefs,
)
from _bench_query.models import (  # noqa: F401
    TASKS_FILE,
    THRESHOLDS,
    AccuracyStats,
    Query,
    ScanResult,
    ScenarioResult,
    SuiteStats,
    Task,
    TimingStats,
    ValidationResult,
)
from _bench_query.output import _IS_RICH_AVAILABLE, _OUT, _console, _run_all_suites, emit, log  # noqa: F401
from _bench_query.paths import (  # noqa: F401
    _SELF_CONSISTENCY_MIN_VER,
    _index_scan_version,
    resolve_index_path,
    resolve_repo_path,
)
from _bench_query.queries import (  # noqa: F401
    codemap_rdeps_result,
    grep_importers_boundary,
    run_scan_query,
    run_scan_query_result,
    validate_central_json,
    validate_deps_json,
    validate_rdeps_json,
)
from _bench_query.report import (  # noqa: F401
    build_summary_envelope,
    render_report,
    resolve_report_path,
    write_report_file,
)
from _bench_query.scoring import (  # noqa: F401
    _PRIMARY_SUITES,
    _SELF_CONSISTENCY_SUITES,
    compute_precision_recall,
    compute_self_consistency,
    compute_verdict,
    score_rdeps_accuracy,
)
from _bench_query.sources import (  # noqa: F401
    _resolve_relative,
    file_imports_module,
    module_to_grep_pattern,
    module_to_package,
    module_to_source_file,
    path_to_module,
    verify_importer,
)
from _bench_query.suites import (  # noqa: F401
    _a1_scenario,
    _a2_scenario,
    _measure_infeasible_paths,
    _score_accuracy_tasks,
    run_measure_accuracy,
    run_measure_calls,
    run_measure_latency,
    run_measure_query_shape,
)
from _bench_query.tasks import load_oss_tasks, load_tasks  # noqa: F401

if __name__ == "__main__":
    fire.Fire(main)
