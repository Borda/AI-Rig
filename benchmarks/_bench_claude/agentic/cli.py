"""Command-line entry point and run loop for the Claude agentic benchmark."""

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


from rich.text import Text as _Text

from _bench_common.benchmark_paths import RESULTS_DIR
from _bench_common.change_impact_stage import run_stage as run_change_impact_stage
from _bench_common.claude_transport import MODEL_TIMEOUT, MODELS
from _bench_common import agentic_reporting
from _bench_common.agentic_reporting import summary_lines, summarize_agentic
from _bench_common.presentation import (
    fmt_time,
    make_progress,
)

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AGENTIC_ARMS,
    DEFAULT_REPETITIONS,
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
    answer_failure_details,
    assess_answer_response,
    build_oracle,
    score_answer,
    score_evidence_metrics,
)
from _bench_common.provider_parity_contracts import (
    ARM_CONTRACTS,
    PARITY_TIMEOUT_SECONDS,
    deterministic_arm_order,
    treatment_adherence,
)
from _bench_common.mutation_isolation import (
    load_index_relocation,
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (
    FIX_MULTI_TASKS_PATH,
    FIX_SINGLE_TASKS_PATH,
    PARITY_MANIFEST_PATH,
    PATCH_TASKS_PATH,
    READCROP_TASKS_PATH,
)

from _bench_claude.agentic.config import LEGACY_EXPERIMENT_REVISION, _console
from _bench_claude.agentic.models import BenchmarkRun, QualityScore, Task, ToolCounts, parity_arm_identity
from _bench_claude.agentic.provenance import (
    _evaluator_provenance,
    _repository_fingerprint,
    _sha256_file,
    _validate_parity_runtime,
)
from _bench_claude.agentic.discovery import _unique_path, check_semble_mcp, find_index
from _bench_claude.agentic.scope import resolve_agentic_scope
from _bench_claude.agentic.tasks import _canonical_agentic_row, load_legacy_tasks, load_tasks_with_provenance
from _bench_claude.agentic.ground_truth import GroundTruth
from _bench_claude.agentic.scoring import score_fix, score_read_crop
from _bench_claude.agentic.report import Report, _ARM_STYLE, _FAIL_STYLE, _run_line
from _bench_claude.agentic.paid import _run_claude_p1_stage, impact_runtime
from _bench_claude.agentic.runner import ModelRunner


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
