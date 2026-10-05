"""Arm tables, profile and tier definitions, and the frozen provider-parity manifest coordinates."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from _bench_common.benchmark_paths import TASKS_BENCH_FILE as TASKS_FILE
from _bench_common.presentation import (
    benchmark_console,
)
from _bench_common.provider_parity_contracts import (
    ARM_CONTRACTS,
    deterministic_arm_order,
    load_task_policies,
    load_task_suite,
    semantic_suite_hash,
)

_USE_COLOR = sys.stdout.isatty()
_GREEN = "\033[32m" if _USE_COLOR else ""
_RED = "\033[31m" if _USE_COLOR else ""
_BLUE = "\033[34m" if _USE_COLOR else ""
_RESET = "\033[0m" if _USE_COLOR else ""
_console = benchmark_console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# TASKS_FILE and RESULTS_DIR come from benchmark_paths (shared across runners).
PATCH_TASKS_FILE = Path(__file__).resolve().parents[2] / "suites" / "tasks-patch.json"

# Synthetic task type assigned to tasks loaded via ``--tasks-file`` that carry a `skill`
# field instead of a `type` field (e.g. tasks-code.json). No evaluator is registered for
# this type; tasks of this type are forced scoreable=False and contribute token-ratio and
# tool-count data only — never accuracy.
_EXTERNAL_TASK_TYPE = "develop_skill"

# Diff-impact tasks stage a scripted change around both arms, then ``DiffImpactStager`` reverts it.
_DIFF_IMPACT_TYPE = "diff_impact"

# real_issue (RI) series reproduces + locates files for a real GitHub issue; those runs routinely
# reach ~2M input tokens (the plain arm greps the whole tree). They therefore run only under the
# release profile or an explicit task selection, never in the fast dev / tiered-haiku default set.
_RI_TASK_TYPE = "real_issue"

# Cost profiles. `dev` selects the dev-tagged subset on haiku for a fast regression signal; `release`
# runs the full matrix including RI. Absent (None) → current behavior, unchanged.
_PROFILE_DEV = "dev"
_PROFILE_RELEASE = "release"
_PROFILES = (_PROFILE_DEV, _PROFILE_RELEASE)

# Per-task JSON tag: `"profiles": ["dev"]` marks membership in the stratified dev subset. Declared in
# tasks-bench.json (not hardcoded here) so the subset can be re-stratified without a code change.
_PROFILE_TAG_KEY = "profiles"

# Per-task JSON tag: `"self_consistency": true` marks a task whose ground truth is derived from the
# same scan-query index the codemap arm queries (uncovered / broken-xref counts). Such tasks still
# run and score, but are excluded from the headline accuracy aggregates and reported separately —
# scoring the codemap arm against index-derived truth would measure agreement with itself, not skill.
_SELF_CONSISTENCY_KEY = "self_consistency"

# Head-meta keys hashed into the index fingerprint (index_sha). These content-defining fields change
# whenever the index is rebuilt over a different tree / scan; `modules` is intentionally excluded so
# the fingerprint stays cheap to compute without loading the whole (large) index body.
_INDEX_META_KEYS = ("scan_version", "scanned_at", "git_sha", "project", "scan_root")

# MODELS and MODEL_TIMEOUT come from claude_transport (shared with run-claude-agentic).

# Tiered protocol (release companion). Each tier runs a progressively smaller task set:
#   haiku  → full suite         sonnet → dev-tagged subset        opus → disagreement adjudication
_TIER_HAIKU = "haiku"
_TIER_SONNET = "sonnet"
_TIER_OPUS = "opus"

ARMS = ("plain", "codemap")
PARITY_ARMS = tuple(ARM_CONTRACTS)
_RESULT_ARM_WIDTH = max(map(len, (*ARMS, *PARITY_ARMS))) + 1
PARITY_ARM_BY_LEGACY_ARM: dict[str, str] = {}
PARITY_MANIFEST_FILE = Path(__file__).resolve().parents[2] / "manifests" / "provider-parity-methodology.json"
LEGACY_EXPERIMENT_REVISION = "legacy-unversioned"
_PARITY_MANIFEST = json.loads(PARITY_MANIFEST_FILE.read_text(encoding="utf-8"))
_PRIMARY_SUITE_MANIFEST = next(
    suite for suite in _PARITY_MANIFEST["suites"] if suite["path"] == "benchmarks/suites/tasks-bench.json"
)
PRIMARY_SUITE_RAW_HASH = hashlib.sha256(TASKS_FILE.read_bytes()).hexdigest()
PRIMARY_SUITE_HASH = semantic_suite_hash(load_task_suite(TASKS_FILE))
_PRIMARY_TASK_IDENTITIES = {
    task["id"]: (task["canonical_task_sha256"], task["prompt_sha256"]) for task in _PRIMARY_SUITE_MANIFEST["tasks"]
}
_PRIMARY_TASK_IDS = tuple(_PRIMARY_TASK_IDENTITIES)
_PARITY_TASK_POLICIES = load_task_policies(PARITY_MANIFEST_FILE)
PARITY_EXPERIMENT_REVISION = next(iter(_PARITY_TASK_POLICIES.values())).experiment_revision


def _arm_orders_by_task(
    tasks: list[dict[str, Any]],
    arms: list[str] | tuple[str, ...],
    *,
    model: str,
    provider_parity: bool,
) -> dict[str, tuple[str, ...]]:
    """Return the execution arm order for each selected task.

    Provider-parity runs use the shared revision-bound coordinate policy with
    Claude's empty reasoning-effort coordinate. Legacy and explicitly
    single-arm runs preserve the caller's declared arm order.

    Args:
        tasks: Selected task dictionaries in execution order.
        arms: Arm labels available to each task.
        model: Claude model stratum used by the shared ordering policy.
        provider_parity: Whether to counterbalance the canonical A/B/C arms.

    Returns:
        Mapping from task ID to its ordered arm tuple.

    Raises:
        ValueError: If provider-parity scheduling does not receive exactly the
            canonical A/B/C arms.
    """
    if not provider_parity:
        declared_order = tuple(arms)
        return {task["id"]: declared_order for task in tasks}
    if set(arms) != set(PARITY_ARMS) or len(arms) != len(PARITY_ARMS):
        raise ValueError("provider-parity scheduling requires the canonical A/B/C arms")
    return {
        task["id"]: deterministic_arm_order(
            PARITY_EXPERIMENT_REVISION,
            "claude",
            model,
            task["id"],
            1,
            reasoning_effort="",
        )
        for task in tasks
    }


# ``--setting-sources project,local`` excludes USER-level config from the benchmark subprocess:
# the caveman plugin, the foundry Re:Anchor rules (box header + ▓ footer), user CLAUDE.md, and
# user hooks. Those shaped the agent's output (markdown/backtick decoration, footer prose) and
# inflated tokens equally on both arms — noise, not signal. scan-query still reaches the codemap
# arm via PATH (_subprocess_env), and the plain arm needs no plugins, so both arms run clean.
# Subscription auth is unaffected (auth is not a setting source). Applied to both arms identically.
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

_ARM_DISALLOWED: dict[str, list[str]] = {
    # plain: also block scan-query via Bash so the control arm can't use the index
    # Write/Edit/NotebookEdit blocked on both arms to prevent filesystem contamination during runs
    # Bash(python3:*)/Bash(python:*) blocked on both arms: prevents implement-validate spirals
    # in real_issue tasks where agents write repro scripts and edit source via heredoc.
    "plain": [
        "--disallowed-tools",
        "Skill,Write,Edit,NotebookEdit,mcp__semble__search,mcp__semble__find_related,Bash(scan-query:*),Bash(python3:*),Bash(python:*)",
    ],
    "codemap": [
        "--disallowed-tools",
        "Write,Edit,NotebookEdit,mcp__semble__search,mcp__semble__find_related,Bash(python3:*),Bash(python:*)",
    ],
    "A_plain": [
        "--disallowed-tools",
        "Skill,Write,Edit,NotebookEdit,mcp__semble__search,mcp__semble__find_related,Bash(scan-query:*),Bash(python3:*),Bash(python:*)",
    ],
    "B_auto": [
        "--disallowed-tools",
        "Write,Edit,NotebookEdit,mcp__semble__search,mcp__semble__find_related,Bash(python3:*),Bash(python:*)",
    ],
    "C_strict": [
        "--disallowed-tools",
        "Write,Edit,NotebookEdit,mcp__semble__search,mcp__semble__find_related,Bash(python3:*),Bash(python:*)",
    ],
}
_ARM_ALLOWED: dict[str, list[str]] = {
    "codemap": ["--allowedTools", "Bash(scan-query:*)"],
    "B_auto": ["--allowedTools", "Bash(scan-query:*)"],
    "C_strict": ["--allowedTools", "Bash(scan-query:*)"],
}

# ---------------------------------------------------------------------------
# Repo identity — populated from tasks-bench.json header in main()
# ---------------------------------------------------------------------------

_REPO_NAME: str = "the repository"
_REPO_NAMESPACE: list[str] = ["lightning", "examples"]
_REPO_LOCAL_PATH: str | None = None


class SandboxError(Exception):
    """Raised when a patch sandbox cannot be set up or torn down.

    Distinct from a failing test: a SandboxError means the harness could not
    create the worktree, check out the pre-fix commit, or apply the diff — the
    pass/fail signal is unobtainable, not that the patch is semantically wrong.
    """


# pytest's own exit codes. Only 0 (all passed) and 1 (tests failed) carry a test result;
# 2-5 mean pytest could not deliver one (interrupted, internal error, bad usage, nothing
# collected). Reading "not 0" as "tests failed" turned a missing plugin or a bad argument
# into a silent zero for every patch task, because the identical error recurred after the
# patch and was scored as an unfixed failure.
PYTEST_EXIT_ALL_PASSED = 0
PYTEST_EXIT_TESTS_FAILED = 1
_PYTEST_RESULT_EXIT_CODES = frozenset({PYTEST_EXIT_ALL_PASSED, PYTEST_EXIT_TESTS_FAILED})
_PYTEST_EXIT_MEANINGS = {
    2: "interrupted",
    3: "internal error",
    4: "usage error",
    5: "no tests collected",
}


def _pin_pytest_interpreter(argv: list[str]) -> list[str]:
    """Rewrite a leading bare ``pytest`` to ``sys.executable -m pytest``.

    Leaves any other command untouched, including an argv that already names an interpreter explicitly.
    """
    if argv and Path(argv[0]).name in {"pytest", "py.test"}:
        return [sys.executable, "-m", "pytest", *argv[1:]]
    return argv


def _describe_pytest_exit(returncode: int) -> str:
    """Describe one non-result pytest exit code for a sandbox error message."""
    return _PYTEST_EXIT_MEANINGS.get(returncode, "unknown pytest failure")
