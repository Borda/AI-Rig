"""Agentic run reporting and per-run terminal rows."""

import statistics
from collections import defaultdict
from collections.abc import Mapping

import pandas as pd


from _bench_common.claude_transport import MODELS
from _bench_common import agentic_reporting
from _bench_common.agentic_reporting import cell_passes, cell_quality, summary_lines
from _bench_common.presentation import (
    fmt_time,
    fmt_tok,
)

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AGENTIC_ARMS,
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.

from _bench_claude.agentic.models import BenchmarkRun, Task
from _bench_claude.agentic.scope import run_cost_usd
from _bench_claude.agentic.tasks import _canonical_agentic_row
from _bench_claude.agentic.scoring import aggregate


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
