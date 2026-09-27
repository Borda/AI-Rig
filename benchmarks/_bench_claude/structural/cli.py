"""Command-line entry point for the Claude structural benchmark."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


from _bench_common.benchmark_paths import RESULTS_DIR, TASKS_BENCH_FILE as TASKS_FILE, gt_is_pending
from _bench_common.claude_transport import MODEL_TIMEOUT, MODELS
from _bench_common.presentation import (
    fmt_time,
    fmt_tok,
    make_progress,
    print_legend,
    print_plan_row,
    print_section_rule,
)
from _bench_common.mutation_isolation import (
    load_index_relocation,
)
from _bench_common.provider_parity_contracts import (
    ARM_CONTRACTS,
    PARITY_TIMEOUT_SECONDS,
    TaskPolicy,
    load_task_suite,
)

from _bench_claude.structural.config import (
    ARMS,
    PARITY_ARMS,
    PATCH_TASKS_FILE,
    PRIMARY_SUITE_HASH,
    PRIMARY_SUITE_RAW_HASH,
    SandboxError,
    _DIFF_IMPACT_TYPE,
    _PROFILES,
    _PROFILE_DEV,
    _RESULT_ARM_WIDTH,
    _TIER_HAIKU,
    _arm_orders_by_task,
    _console,
)
from _bench_claude.structural.models import BenchRun
from _bench_claude.structural.prompts import _resolve_index
from _bench_claude.structural.tasks import (
    TaskSelection,
    _index_sha,
    _load_primary_parity_contract,
    _load_resume_cache,
    _load_tasks_file,
    _repo_sha,
    _select_tasks,
    _validate_primary_runtime,
)
from _bench_claude.structural.telemetry import _max_turns_for_task
from _bench_claude.structural.sandbox import DiffImpactStager, DirtyTreeError, PatchSandbox, _extract_diff
from _bench_claude.structural.runner import BenchRunner
from _bench_claude.structural.report import (
    _effective_recall,
    _print_report_only,
    _print_summary,
    _run_correct_symbol,
    _save_results,
)


@dataclass(frozen=True)
class _RichSubProgressUpdate:
    """Live sub-progress callback for one (task, arm) combo.

    Instances are passed as ``BenchRunner.run(update_fn=...)``: each call refreshes the outer
    bar's description and the per-run sub-bar with the current turn count and tool tallies.

    Attributes:
        progress: Active rich ``Progress`` instance.
        outer_id: Progress task id of the outer (whole-run) bar.
        sub_id: Progress task id of this combo's sub-bar.
        task_id: Benchmark task id shown in the outer description.
        arm_name: Arm label shown in the outer description.

    Examples:
        >>> update = _RichSubProgressUpdate(None, 0, 1, "SE-01", "codemap")
        >>> update.task_id, update.arm_name
        ('SE-01', 'codemap')
    """

    progress: Any
    outer_id: Any
    sub_id: Any
    task_id: str
    arm_name: str

    def __call__(self, elapsed: float, run: BenchRun) -> None:
        """Refresh both progress rows from the in-flight run.

        Args:
            elapsed: Seconds since the run's subprocess started.
            run: The ``BenchRun`` being populated in place.
        """
        calls = run.grep_calls + run.bash_calls + run.skill_calls
        sk = run.skill_calls
        tool_live = f"B={run.bash_calls} G={run.grep_calls} R={run.read_calls} SQ={run.scan_query_calls} Sk={sk}"
        self.progress.update(self.outer_id, description=f"{self.task_id} {self.arm_name}")
        self.progress.update(
            self.sub_id,
            completed=run.turn_count,
            description=f"  {fmt_time(elapsed)} calls={calls} {tool_live}",
        )


@dataclass
class _StructuralRunLoop:
    """Per-run execution state shared by the combo and task-arm drivers.

    Bundles what a single ``main()`` invocation needs to execute one (task, arm) combo, so the
    drivers are module-level methods instead of closures over ``main``'s locals.

    Attributes:
        runner: Configured ``BenchRunner`` executing each combo.
        repo_path: Root of the target repository clone (sandbox + staging root).
        arm_orders: Per-task arm labels in their actual execution order.
        patch_ids: Task ids that carry a patch reference and may be sandbox-scored.
        runs: Accumulator every completed run is appended to, in completion order.

    Examples:
        >>> loop = _StructuralRunLoop(None, Path("."), {"SE-01": ("codemap",)}, {"PT-01"}, [])
        >>> loop.arm_orders["SE-01"], sorted(loop.patch_ids)
        (('codemap',), ['PT-01'])
    """

    runner: Any
    repo_path: Path
    arm_orders: Mapping[str, tuple[str, ...]]
    patch_ids: set[str]
    runs: list[BenchRun] = field(default_factory=list)

    def run_combo(self, task: dict, arm: str, log_fn: Any, update_fn: Optional[Any] = None) -> BenchRun:
        """Execute one (task, arm) combo, record it, and log its one-line result.

        Args:
            task: Task dict to run.
            arm: Arm label to run it under.
            log_fn: Single-argument printer for the result line (rich console or ``print``).
            update_fn: Optional live-progress callback forwarded to the runner.

        Returns:
            The completed ``BenchRun``, already appended to ``self.runs``.
        """
        run = self.runner.run(task, arm, update_fn=update_fn)
        # Tier E: for scoreable patch tasks, extract the agent diff and execute it in a
        # sandbox to record whether the failing test passes. Non-scoreable stubs (placeholder
        # SHA / no reference) skip the sandbox and report structural GT only.
        if task["id"] in self.patch_ids and task.get("scoreable") is not False and run.success:
            diff_text = _extract_diff(run.output_text)
            if diff_text is not None:
                sandbox = PatchSandbox(self.repo_path, task)
                try:
                    run.patch_pass = sandbox.run(diff_text)
                except SandboxError as exc:
                    run.error = run.error or f"sandbox_error: {exc}"
                    run.patch_pass = None
                run.mutation_evidence = sandbox.last_mutation_evidence
            else:
                # No diff block in output — agent produced prose only; scores as a fail.
                run.patch_pass = False
        self.runs.append(run)
        status = "✓" if run.success else "✗"
        correct = _run_correct_symbol(run)
        # in/out token split (k/M via shared fmt_tok) plus Anthropic's own per-run cost. The $ is
        # omitted (in/out only) when the run carried no total_cost_usd — no price table to go stale.
        cost_str = f" ${run.cost_usd:.3f}" if run.cost_usd else ""
        _eff = _effective_recall(run)
        # Three distinct states, never conflated:
        #   number  — scored & parsed: recall in [0, 1] (0.000 = wrong answer, real miss)
        #   !parse  — scored but answer unparsable: parser-coverage issue, NOT degradation
        #   ?unscored — not scored (for example, contaminated or non-evaluable)
        if _eff is not None:
            q_str = f"{_eff:.3f}"
        elif run.quality.scored and run.quality.extraction_failed:
            q_str = "!parse"
        else:
            q_str = "?unscored"
        # Sk (Skill-tool calls) omitted from the terminal line: the codemap arm queries
        # scan-query via Bash (counted in SQ), never the Skill tool, so it is always 0 here.
        # The raw skill_counts field is still recorded in the results JSONL if it ever fires.
        tool_summary = f"B={run.bash_calls:2d} G={run.grep_calls:2d} R={run.read_calls:2d} SQ={run.scan_query_calls:2d}"
        # Ranking evaluators retain their complete oracle in telemetry; the terminal total is
        # the number of expected rows, matching the scalar-count meaning used by other tasks.
        metric_expected = run.quality.metric_expected
        metric_total = len(metric_expected) if isinstance(metric_expected, list) else metric_expected
        metric_total_str = "?" if metric_total is None else str(metric_total)
        log_fn(
            f"  {status}{correct} {task['id']} {arm:<{_RESULT_ARM_WIDTH}}"
            f"\ttok: in={fmt_tok(run.input_tokens):>6} out={fmt_tok(run.output_tokens):>5}{cost_str}"
            f" time={fmt_time(run.elapsed_s):<6} recall={q_str:<9}"
            f"\ttotal={metric_total_str:>4}\t{tool_summary}"
        )
        return run

    def _run_arms(self, task: dict, progress: Any, outer: Any) -> None:
        """Run every selected arm for one task against the current (possibly staged) tree.

        Args:
            task: Task dict.
            progress: Active rich Progress instance.
            outer: Outer progress task id.
        """
        for arm_name in self.arm_orders[task["id"]]:
            task_max_turns = _max_turns_for_task(task)
            sub = progress.add_task("  0s calls=0", total=task_max_turns)
            progress.update(outer, description=f"{task['id']} {arm_name}")
            self.run_combo(
                task,
                arm_name,
                # soft_wrap keeps the wide result row on one line; the console's own width frames
                # legends and rules, and must not fold a row whose columns are meant to line up.
                lambda row: progress.console.print(row, soft_wrap=True),
                update_fn=_RichSubProgressUpdate(progress, outer, sub, task["id"], arm_name),
            )
            progress.remove_task(sub)
            progress.advance(outer)

    def run_task_arms(self, task: dict, progress: Any, outer: Any) -> None:
        """Run every selected arm for one task, staging a diff-impact change around both arms.

        A ``diff_impact`` task with a ``stage`` spec applies the synthetic change once (via
        :class:`DiffImpactStager`), runs BOTH arms against the staged tree, then reverts on block exit —
        so both arms see the identical change and the tree is restored regardless of per-arm outcome.
        Every other task runs its arms directly. Progress bookkeeping is unchanged.

        Args:
            task: Task dict.
            progress: Active rich Progress instance.
            outer: Outer progress task id.
        """
        stage_spec = task.get("stage") if task.get("type") == _DIFF_IMPACT_TYPE else None
        if stage_spec:
            with DiffImpactStager(self.repo_path, stage_spec):
                self._run_arms(task, progress, outer)
        else:
            self._run_arms(task, progress, outer)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(  # noqa: PLR0913 — fire CLI adapter: every param is a keyword flag with a default (0 required)
    repo_path: Path = None,
    index_path: Path = None,
    tasks: list[str] = None,
    tasks_file: list[str] = None,
    task_type: str = None,
    arm: str = "all",
    model: str = "haiku",
    run_all: bool = False,
    patch: bool = False,
    no_save: bool = False,
    timeout: int = None,
    resume: bool = False,
    profile: str = None,
    tiered: bool = False,
    dry_run: bool = False,
    provider_parity: bool = False,
    report: str = "",
    index_relocation_path: Path = None,
) -> None:
    """Entry point: load tasks, run selected arms, print summary.

    Args:
        repo_path: Path to the target repository clone.
        index_path: Path to codemap index JSON.
        tasks: Task IDs to run as a Python list literal, e.g. ``--tasks "['SE-01', 'FN-02']"``.
        tasks_file: Additional task JSON file(s) to load alongside tasks-bench.json (repeatable).
        task_type: Run tasks of this type only.
        arm: Which arm(s) to run (default: all).
        model: Model to use (default: haiku).
        run_all: Run all tasks (CLI flag: ``--run-all``).
        patch: Run patch tasks from tasks-patch.json.
        no_save: Skip writing JSONL results.
        timeout: Per-run timeout in seconds.
        resume: Reuse matching prior results (same task/arm/model + repo/index/task provenance)
            from the results dir instead of re-executing them (CLI flag: ``--resume``).
        profile: Cost profile ``dev`` (haiku-only stratified subset, fast regression signal) or
            ``release`` (full matrix incl. RI). Absent → current behavior, unchanged.
        tiered: Tiered protocol (release companion): run haiku full, sonnet on the dev subset, and
            opus only on haiku/sonnet disagreements. Select the tier via ``--model`` per invocation.
        dry_run: Validate the locked inputs and print canonical A/B/C planned cells without Claude execution.
        provider_parity: Run the canonical A/B/C arms together in the shared deterministic order.
        report: Re-render the summary for an existing ``bench-*.jsonl`` results file and exit. No
            model runs, no writes — the stored rows are replayed through the current reporting code.
        index_relocation_path: Relocation provenance written when this run's index was moved into an
            isolated worktree; absent for a run at the canonical managed clone.
    """
    global _REPO_NAME, _REPO_NAMESPACE, _REPO_LOCAL_PATH

    if report:
        _print_report_only(Path(report))
        return

    if profile is not None and profile not in _PROFILES:
        print(f"ERROR: --profile must be one of {_PROFILES}, got {profile!r}")
        sys.exit(1)
    # The dev profile is a haiku-only fast signal — pin the model regardless of ``--model``.
    if profile == _PROFILE_DEV:
        model = _TIER_HAIKU

    # fire passes CLI string args regardless of type annotation — coerce Path args explicitly.
    if repo_path is not None:
        repo_path = Path(repo_path)
    if index_path is not None:
        index_path = Path(index_path)

    # Load raw tasks first — legacy arms intentionally remain usable when a later
    # parity revision no longer matches this checked-out source suite.
    try:
        with TASKS_FILE.open() as f:
            _raw = json.load(f)
        all_tasks = load_task_suite(TASKS_FILE)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(f"ERROR: cannot read {TASKS_FILE}: {exc}")
        sys.exit(1)
    task_policies: Mapping[str, TaskPolicy] | None = None

    if isinstance(_raw, dict):
        repo_meta = _raw.get("repo", {})
    else:
        repo_meta = {}

    # Populate repo identity globals from header (evaluators consume these)
    if repo_meta.get("name"):
        _REPO_NAME = repo_meta["name"]
    if repo_meta.get("namespace"):
        _REPO_NAMESPACE = list(repo_meta["namespace"])
    _REPO_LOCAL_PATH = repo_meta.get("local_path")

    # Append tasks from any ``--tasks-file``; scoreable depends on whether ground_truth is present.
    external_ids: list[str] = []
    if tasks_file:
        for tf in tasks_file:
            try:
                extra = _load_tasks_file(Path(tf))
            except (FileNotFoundError, ValueError) as exc:
                print(f"ERROR: {exc}")
                sys.exit(1)
            known_ids = {t["id"] for t in all_tasks}
            dupes = [t["id"] for t in extra if t["id"] in known_ids]
            if dupes:
                print(f"ERROR: --tasks-file {tf} has task IDs already loaded: {sorted(set(dupes))}")
                sys.exit(1)
            all_tasks.extend(extra)
            external_ids.extend(t["id"] for t in extra)
            n_scored = sum(1 for t in extra if t.get("scoreable") is not False)
            print(f"Loaded {len(extra)} task(s) from {tf} ({n_scored} scoreable)")

    # Append patch tasks (Tier E) when ``--patch`` is set. Unlike ``--tasks-file`` tasks,
    # patch tasks keep their own scoreable flag and are sandbox-executed in _run_combo.
    patch_ids: list[str] = []
    if patch:
        try:
            with PATCH_TASKS_FILE.open() as f:
                _patch_raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"ERROR: cannot read {PATCH_TASKS_FILE}: {exc}")
            sys.exit(1)
        patch_tasks = _patch_raw.get("tasks", []) if isinstance(_patch_raw, dict) else _patch_raw
        known_ids = {t["id"] for t in all_tasks}
        dupes = [t["id"] for t in patch_tasks if t["id"] in known_ids]
        if dupes:
            print(f"ERROR: tasks-patch.json has task IDs already loaded: {sorted(set(dupes))}")
            sys.exit(1)
        all_tasks.extend(patch_tasks)
        patch_ids.extend(t["id"] for t in patch_tasks)
        print(f"Loaded {len(patch_tasks)} patch task(s) from {PATCH_TASKS_FILE.name}")

    patch_id_set = set(patch_ids)

    # Resolve repo path
    if not repo_path:
        _cands: list[Path] = []
        if _REPO_LOCAL_PATH:
            _cands.append(Path(_REPO_LOCAL_PATH))  # header local_path = .sandbox/pytorch-lightning (run from root)
        for cand in _cands:
            if cand.is_dir():
                repo_path = cand
                break
        if not repo_path:
            print("ERROR: cannot find repo. Pass --repo-path.")
            sys.exit(1)
    if not repo_path.is_dir():
        print(f"ERROR: --repo-path {repo_path} is not a directory")
        sys.exit(1)

    # Resolve index
    try:
        index_path = _resolve_index(repo_path, index_path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    # Provenance fingerprints — shared by task selection (tiered) and the runner (resume + stamping).
    repo_sha = _repo_sha(repo_path)
    index_sha = _index_sha(index_path)

    # Task IDs must exist before selection filters run so a typo fails loudly.
    if tasks:
        ids = set(tasks)
        missing = ids - {t["id"] for t in all_tasks}
        if missing:
            print(f"ERROR: task IDs not found: {sorted(missing)}")
            sys.exit(1)
    else:
        ids = None

    selection = TaskSelection(
        all_tasks=all_tasks,
        ids=ids,
        task_type=task_type,
        run_all=run_all,
        external_ids=set(external_ids),
        patch_ids=patch_id_set,
        profile=profile,
        tiered=tiered,
        model=model,
    )
    task_list = _select_tasks(selection, RESULTS_DIR, repo_sha, index_sha)
    if task_list is None:
        print("Specify --tasks, --task-type, --tasks-file, --patch, --all, or --profile")
        sys.exit(1)

    if not task_list:
        print("No tasks matched.")
        sys.exit(1)

    # Exclude tasks whose ground truth is still a placeholder (gt_pending): their stage anchors and
    # expected callers were authored without this target repo, so staging and scoring are unreliable
    # until `generate-tasks-bench.py --update` materialises them. The generator's validator already
    # honours this flag; the runner must too, or a stale DI anchor derails the run.
    pending = [t for t in task_list if gt_is_pending(t)]
    if pending:
        ids = ", ".join(t["id"] for t in pending)
        print(
            f"⊘ Skipping {len(pending)} task(s) with pending ground truth — run "
            f"`generate-tasks-bench.py --update` against the target repo to materialise them: {ids}"
        )
        task_list = [t for t in task_list if not gt_is_pending(t)]
    if not task_list:
        print("No runnable tasks after excluding pending ground truth.")
        sys.exit(1)

    # Determine arms. Provider-parity and dry runs plan the current canonical A/B/C
    # matrix; normal runs retain the historical legacy defaults until a caller
    # selects A/B/C.
    if provider_parity and arm != "all":
        print("ERROR: --provider-parity cannot be combined with a specific --arm")
        sys.exit(1)
    allowed_arms = {*ARMS, *PARITY_ARMS}
    canonical_matrix = provider_parity or (dry_run and arm == "all")
    arms_to_run = list(PARITY_ARMS) if canonical_matrix else (list(ARMS) if arm == "all" else [arm])
    invalid_arms = sorted(set(arms_to_run) - allowed_arms)
    if invalid_arms:
        print(f"ERROR: unsupported arm labels: {invalid_arms}")
        sys.exit(1)

    canonical_requested = dry_run or provider_parity or any(arm_name in ARM_CONTRACTS for arm_name in arms_to_run)
    if canonical_requested:
        try:
            _, task_policies = _load_primary_parity_contract()
            index_relocation = load_index_relocation(
                None if index_relocation_path is None else Path(index_relocation_path)
            )
            _validate_primary_runtime(repo_path, index_path, index_relocation)
        except (OSError, ValueError) as exc:
            print(f"ERROR: cannot start canonical parity run: {exc}")
            sys.exit(1)

    try:
        arm_orders = _arm_orders_by_task(
            task_list,
            arms_to_run,
            model=model,
            provider_parity=canonical_matrix,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    if dry_run:
        for task in task_list:
            for arm_name in arm_orders[task["id"]]:
                print_plan_row(f"PLAN\t{task['id']}\t{arm_name}", console=_console)
        return

    # Build runner
    model_short = model
    model_id = MODELS[model_short]
    run_timeout = (
        timeout
        if timeout is not None
        else PARITY_TIMEOUT_SECONDS
        if canonical_requested
        else MODEL_TIMEOUT[model_short]
    )
    resume_cache = _load_resume_cache(RESULTS_DIR) if resume else None
    runner = BenchRunner(
        model_short=model_short,
        model_id=model_id,
        repo_path=repo_path,
        index_path=index_path,
        timeout=run_timeout,
        resume_cache=resume_cache,
        task_policies=task_policies,
        suite_hash=PRIMARY_SUITE_HASH if canonical_requested else None,
        suite_raw_hash=PRIMARY_SUITE_RAW_HASH if canonical_requested else None,
    )

    print()
    print_section_rule(f"▶ RUN START — model={model_short}", console=_console)
    print(f"Codemap benchmark: {len(task_list)} tasks × {len(arms_to_run)} arm(s) × model={model_short}")
    print(f"  index: {index_path}")
    print(f"  repo:  {repo_path}")
    print()
    print_legend(
        [
            "  task series:",
            "    SE  symbol_extraction     — locate symbol definition (file + line)",
            "    FN  fn_call_graph         — unique callers of a function (static graph)",
            "    RV  review_assistance     — doc-gap / rdep / coverage counts for review",
            "    CQ  code_quality          — coupling, broken xrefs, doc+coverage health",
            "    BR  develop_blast_radius  — enumerate direct callers before a change (recall ≥ 0.70)",
            "    DG  debug_from_trace      — identify fn + file from traceback/log (word-boundary match)",
            "    FT  feature_scaffolding   — identify files to create/modify for a feature (word-boundary match)",
            "    RI  real_issue            — reproduce + locate files for a GitHub issue (recall ≥ 0.70)",
            "    MB  module_blast_radius   — enumerate modules that import a target (importer recall ≥ 0.70)",
        ],
        console=_console,
    )
    print()

    runs: list[BenchRun] = []
    combos = [(task, arm) for task in task_list for arm in arm_orders[task["id"]]]
    run_loop = _StructuralRunLoop(
        runner=runner,
        repo_path=repo_path,
        arm_orders=arm_orders,
        patch_ids=patch_id_set,
        runs=runs,
    )

    with make_progress(_console) as progress:
        total = len(combos)
        outer = progress.add_task("running", total=total)
        _dirty_skips: list[tuple[str, str]] = []  # DI tasks skipped (un-stageable), reported after the run
        for task in task_list:
            try:
                run_loop.run_task_arms(task, progress, outer)
            except DirtyTreeError as exc:
                # This diff-impact task could not stage its synthetic change — either the tree is
                # dirty, or a find-anchor is stale vs the current target repo. The stager already
                # reverted any partial edit (see __enter__), so skip THIS task and continue: one
                # un-stageable task must never abort the whole run or discard the summary and the
                # results already gathered for every other task.
                progress.console.print(f"⚠ skipped DI task {task['id']} (cannot stage): {exc}", style="yellow")
                _dirty_skips.append((task["id"], str(exc)))

    _print_summary(runs, model_short)

    if not no_save:
        out = _save_results(runs, model_short)
        print(f"\nResults → {out}")

    if _dirty_skips:
        print(
            f"\n{len(_dirty_skips)} diff-impact task(s) skipped — could not stage (dirty tree or stale find-anchor vs the target repo):"
        )
        for tid, why in _dirty_skips:
            print(f"  {tid}: {why}")

    failed = [r for r in runs if not r.success]
    if failed:
        print(f"\n{len(failed)} run(s) failed:")
        for r in failed:
            print(f"  {r.task_id}/{r.arm}: {r.error}")
        sys.exit(1)
