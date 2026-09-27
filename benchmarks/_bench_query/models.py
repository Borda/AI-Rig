"""Frozen result types and pass thresholds shared by every codemap-cli benchmark suite."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# ---- TYPES ----


@dataclass
class Query:
    """A single scan-query invocation specification."""

    cmd: str  # "rdeps", "deps", "central", "coupled", "path"
    args: list[str]  # positional args after the command


@dataclass
class Task:
    """A benchmark task loaded from tasks-code.json."""

    id: str  # "B-01", "F-03", "R-05"
    skill: str  # "fix", "feature", "refactor"
    prompt: str  # developer-facing scenario description
    primary_module: str  # dotted module name
    risk_tier: str  # "high", "moderate", "low", "very-high", etc.
    queries: list[Query]  # ordered scan-query calls for this task
    ground_truth_keys: list[str]  # expected JSON keys in query results

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        """Construct a Task from a raw JSON dict."""
        return cls(
            id=d["id"],
            skill=d["skill"],
            prompt=d["prompt"],
            primary_module=d["primary_module"],
            risk_tier=d["risk_tier"],
            queries=[Query(**q) for q in d["queries"]],
            ground_truth_keys=d["ground_truth_keys"],
        )


@dataclass
class ScenarioResult:
    """Result of a single benchmark scenario evaluation."""

    scenario: str  # "C1", "A1", "L2", "Q_fix", etc.
    name: str  # human label e.g. "coverage-gap"
    suite: str  # "calls", "accuracy", "latency", "query-shape", "symbol", "health", "xrefs"
    passed: bool
    result: dict  # suite-specific measurement values
    threshold: dict  # the threshold values applied
    notes: str = ""


@dataclass
class TimingStats:
    """Timing measurement results from repeated command runs."""

    min_ms: float
    median_ms: float
    max_ms: float
    n: int
    # Runs excluded from the statistics above, reported rather than folded in.
    # `failed` = the command exited non-zero, so its latency measures how fast it broke.
    # `timed_out` = the run hit the subprocess deadline; that is censored data (the true
    # duration is only known to exceed the limit), so substituting the deadline as if it
    # were an observation biases the median toward the deadline.
    failed: int = 0
    timed_out: int = 0

    @property
    def measured(self) -> int:
        """Return the number of runs that actually produced a latency observation."""
        return self.n - self.failed - self.timed_out


@dataclass
class AccuracyStats:
    """Per-task precision/recall results for rdeps accuracy."""

    precision: float
    recall: float
    tp: int
    fp: int
    fn: int
    fp_modules: list[str]
    fn_modules: list[str]


@dataclass
class SuiteStats:
    """Aggregate pass/fail counters for a single benchmark suite."""

    total: int = 0
    passed: int = 0
    failed: int = 0


@dataclass
class ValidationResult:
    """Outcome of a structural JSON validation check."""

    ok: bool
    reason: str  # empty string if ok; error description if not


@dataclass
class ScanResult:
    """Outcome of a scan-query invocation: parsed data or a distinct error reason.

    Separates a genuine empty/negative result (``data`` present, ``error`` is
    ``None``) from a tool failure — non-zero exit, timeout, or undecodable output
    (``data`` is ``None``, ``error`` set).  Callers use this to avoid scoring a
    crashed or absent-module query as a passing empty result.

    Examples:
        >>> ScanResult(data={"imported_by": []}, error=None).ok
        True
        >>> ScanResult(data=None, error="timeout after 30s").ok
        False
    """

    data: dict | None
    error: str | None  # None on success; short failure reason otherwise

    @property
    def ok(self) -> bool:
        """Return ``True`` when the query succeeded and ``data`` is available."""
        return self.error is None


# ---- CONFIG ----

TASKS_FILE = Path(__file__).resolve().parents[1] / "suites" / "tasks-code.json"
# OSS_TASKS_FILE (tasks-bench.json) comes from benchmark_paths as TASKS_BENCH_FILE.

THRESHOLDS = {
    # Coverage gap suite (C): structural completeness of cold grep vs codemap
    "C1": {"coverage_gap_min": 0.10},  # codemap finds >=10% more importers than grep
    "C2": {"infeasible_path_fraction_min": 0.50},  # >=50% of 2+ hop paths not grep-discoverable in 1 call
    "C3": {"leverage_ratio_min": 2.0},  # structural context tokens / cold exploration tokens >= 2x
    # Accuracy suite (A): rdeps precision/recall vs grep ground truth
    "A1": {"precision_min": 0.90, "recall_min": 0.85},  # high-risk tasks (B-01,B-03,B-05,F-02,F-05,R-01,R-02)
    "A2": {"precision_min": 1.00},  # low-risk tasks (B-04, R-05)
    "A3": {"fp_rate_max": 0.05},  # overall across all 15 tasks
    # Query-shape suite (Q): scan-query output shape is valid for each skill group (NOT the injection path)
    "Q_fix": {"block_present": True, "json_valid": True},
    "Q_feature": {"block_present": True, "json_valid": True},
    "Q_refactor": {"block_present": True, "json_valid": True, "has_rdeps": True, "has_deps": True},
    # Latency suite (L): unchanged
    "L1": {"median_ms_max": 200},
    "L2": {"median_ms_max": 100},
    "L3": {"amortized_ms_max": 500},
    "L4": {"speedup_min": 2.0},
    # Symbol suite (S): symbol command returns correct line range
    "S1": {"symbol_found": True, "start_line_ok": True},
    "S2": {"symbol_found": True},
    # Health suite (H): undocumented / uncovered counts match tasks-bench.json ground truth
    "H1": {"count_match": True},  # undocumented tasks
    "H2": {"count_match": True},  # uncovered tasks
    # Xrefs suite (X): ``xrefs --broken`` count matches tasks-bench.json ground truth
    "X1": {"count_match": True},
    # Deterministic correctness suites (D/B/R/K/U): fixture repos with KNOWN ground truth,
    # constructed independently of scan-query output — genuine independent-oracle checks that
    # join the primary verdict. Each scenario's threshold is a boolean contract the fixture
    # is built to satisfy exactly; a mismatch is a real correctness regression in the CLI.
    "D_diff_impact": {"contract_holds": True},  # changed module/symbol, risk tiers, test-impact union
    "B_batch": {"contract_holds": True},  # per-item order, invalid-item error, one coverage, byte-equivalence
    "R_src_roots": {"contract_holds": True},  # multi-root naming, collision winner, src_roots meta
    "K_self_check": {"contract_holds": True},  # corrupt index → exit 3 + parseable JSON, never partial-serve
    "U_uncovered_xrefs": {"contract_holds": True},  # exact undocumented count + one broken sphinx xref
}
