"""Task loading, provenance hashing, parity-contract validation, resume cache, and selection."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from _bench_common.benchmark_paths import TASKS_BENCH_FILE as TASKS_FILE
from _bench_common.mutation_isolation import verify_index_relocation
from _bench_common.provider_parity_contracts import (
    TaskPolicy,
    canonical_task_hash,
    load_task_suite,
    prompt_hash,
    semantic_suite_hash,
)

from _bench_claude.structural.config import (
    _EXTERNAL_TASK_TYPE,
    _INDEX_META_KEYS,
    _PARITY_MANIFEST,
    _PARITY_TASK_POLICIES,
    _PRIMARY_TASK_IDENTITIES,
    _PRIMARY_TASK_IDS,
    _PROFILE_DEV,
    _PROFILE_RELEASE,
    _PROFILE_TAG_KEY,
    _RI_TASK_TYPE,
    _TIER_HAIKU,
    _TIER_OPUS,
    _TIER_SONNET,
    PRIMARY_SUITE_HASH,
)
from _bench_claude.structural.models import BenchQuality, BenchRun


def _normalize_external_task(task: dict) -> dict:
    """Normalize one task from a ``--tasks-file`` into the harness task schema.

    External task files use a different schema from tasks-bench.json: ``queries``
    instead of ``expected_queries``, a ``skill`` field instead of ``type``, and
    ``ground_truth_keys`` instead of materialized ``ground_truth`` values (e.g.
    tasks-code.json). Files that DO carry materialized ``ground_truth`` and an
    explicit ``scoreable: true`` (e.g. tasks-debug.json, tasks-feature.json,
    tasks-oss.json) are evaluated normally — their ``scoreable`` field is
    preserved so the registered evaluator runs.

    The original task dict is not mutated; a shallow copy is returned.

    Args:
        task: Raw task dict loaded from a ``--tasks-file``.

    Returns:
        A new task dict with ``expected_queries``, ``type``, and ``scoreable``
        keys populated for the harness.

    Examples:
        >>> t = _normalize_external_task(
        ...     {"id": "B-01", "prompt": "p", "skill": "fix",
        ...      "queries": [{"cmd": "rdeps", "args": ["m"]}]}
        ... )
        >>> t["type"], t["scoreable"], t["expected_queries"]
        ('develop_skill', False, [{'cmd': 'rdeps', 'args': ['m']}])
        >>> "queries" in t  # original key dropped after rename
        False
        >>> scored = _normalize_external_task(
        ...     {"id": "DBG-01", "type": "debug_from_trace", "scoreable": True,
        ...      "prompt": "p", "ground_truth": {"function": "f", "file": "a.py", "start_line": 1}}
        ... )
        >>> scored["scoreable"]
        True
    """
    norm = dict(task)
    if "queries" in norm and "expected_queries" not in norm:
        norm["expected_queries"] = norm.pop("queries")
    # Harness requires a `type` for BenchRun, logging, and summary grouping.
    if not norm.get("type"):
        norm["type"] = _EXTERNAL_TASK_TYPE
    # Only force scoreable=False when no materialized ground truth present.
    # Tasks with ground_truth + explicit scoreable=True are scored via their evaluator.
    if not norm.get("ground_truth"):
        norm["scoreable"] = False
    elif "scoreable" not in norm:
        norm["scoreable"] = True
    return norm


def _load_tasks_file(path: Path) -> list[dict]:
    """Load and normalize an additional task file passed via ``--tasks-file``.

    Accepts either a bare JSON list of tasks or a ``{"repo": ..., "tasks": [...]}``
    object (the tasks-bench.json shape). Every loaded task is run through
    :func:`_normalize_external_task`.

    Args:
        path: Path to the JSON task file.

    Returns:
        List of normalized task dicts.

    Raises:
        FileNotFoundError: When the file does not exist.
        ValueError: When the JSON is malformed or has an unexpected shape.
    """
    if not path.is_file():
        raise FileNotFoundError(f"--tasks-file not found: {path}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"--tasks-file {path} is not valid JSON: {exc}") from exc
    if isinstance(raw, dict):
        raw_tasks = raw.get("tasks", [])
    elif isinstance(raw, list):
        raw_tasks = raw
    else:
        raise ValueError(f"--tasks-file {path} must be a JSON list or object with a 'tasks' key")
    return [_normalize_external_task(t) for t in raw_tasks]


# ---------------------------------------------------------------------------
# Provenance + resume cache (cost lever: ``--resume``)
# ---------------------------------------------------------------------------

#: Result-line fields that identify one (task, arm, model) execution against a specific tree + index +
#: task definition. Two lines match iff all six agree — the resume key.
_RESUME_KEY_FIELDS = ("task_id", "arm", "model", "repo_sha", "index_sha", "task_hash")


def _repo_sha(repo_path: Path) -> str:
    """Return the repository HEAD SHA, or "unknown" when git is unavailable.

    Args:
        repo_path: Path to the target repository clone.

    Returns:
        The 40-char HEAD SHA, or "unknown" when ``repo_path`` is not a git work tree
        or git is not on PATH.

    Examples:
        >>> _repo_sha(Path("/definitely/not/a/repo/xyzzy"))
        'unknown'
    """
    try:
        out = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],  # noqa: S607 - git/tool resolved via PATH on purpose
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


def _index_sha(index_path: Path) -> str:
    """Fingerprint the index by hashing its content-defining head-meta fields.

    Only the small :data:`_INDEX_META_KEYS` subset is hashed (``scan_version``,
    ``scanned_at``, ``git_sha``, ``project``, ``scan_root``) — enough to distinguish
    two rebuilds without loading the large ``modules`` body. A missing or unreadable
    index yields "unknown" rather than raising, so provenance degrades gracefully.

    Args:
        index_path: Path to the codemap index JSON file.

    Returns:
        A hex sha256 digest of the canonicalised head-meta subset, or "unknown".

    Examples:
        >>> import json, tempfile
        >>> f = Path(tempfile.mkstemp(suffix=".json")[1])
        >>> _ = f.write_text(json.dumps({"scan_version": 5, "scanned_at": "t", "modules": [1, 2]}))
        >>> len(_index_sha(f))
        64
        >>> _index_sha(Path("/no/such/index.json"))
        'unknown'
    """
    try:
        raw = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "unknown"
    meta = {k: raw.get(k) for k in _INDEX_META_KEYS}
    payload = json.dumps(meta, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _task_hash(task: dict) -> str:
    """Return a stable sha256 of the task's JSON definition.

    The task dict is serialised with sorted keys so the digest is invariant to key
    order; any change to prompt, ground truth, or expected queries changes the hash,
    invalidating a stale resume match.

    Args:
        task: A task dict from tasks-bench.json.

    Returns:
        A hex sha256 digest of the canonicalised task JSON.

    Examples:
        >>> _task_hash({"id": "SE-01", "prompt": "p"}) == _task_hash({"prompt": "p", "id": "SE-01"})
        True
    """
    return canonical_task_hash(task)


def _prompt_hash(task: dict) -> str:
    """Return the locked UTF-8 prompt hash for one raw benchmark task.

    Args:
        task: Raw task loaded from a benchmark suite.

    Returns:
        Hexadecimal SHA-256 digest of the task prompt.
    """
    return prompt_hash(task)


def _load_primary_parity_contract() -> tuple[list[dict[str, Any]], Mapping[str, TaskPolicy]]:
    """Load the primary raw suite and its revision-bound shared task policies.

    Returns:
        Raw primary task objects and immutable policies from the locked manifest.
    """
    tasks = load_task_suite(TASKS_FILE)
    if tuple(task["id"] for task in tasks) != _PRIMARY_TASK_IDS:
        raise ValueError("primary suite task order does not match the locked manifest")
    for task in tasks:
        _validate_canonical_task(task)
    if semantic_suite_hash(tasks) != PRIMARY_SUITE_HASH:
        raise ValueError("primary semantic suite hash changed after initialization")
    return tasks, _PARITY_TASK_POLICIES


def _validate_primary_runtime(
    repo_path: Path,
    index_path: Path,
    index_relocation: Mapping[str, str] | None = None,
) -> None:
    """Reject a canonical run outside the manifest's locked target and index.

    Args:
        repo_path: Candidate target repository root.
        index_path: Candidate Codemap index for that target.
        index_relocation: Relocation provenance for a run whose index was moved into its own
            worktree. When supplied it replaces the locked byte-hash check, and only that check;
            when absent the index must still match the locked bytes exactly.

    Raises:
        ValueError: If the repository, worktree, index bytes, relocation provenance, or index
            metadata differ from the locked primary parity inputs.
    """
    target = _PARITY_MANIFEST["target_source"]
    expected_commit = target["commit"]
    if _repo_sha(repo_path) != expected_commit:
        raise ValueError(f"canonical run requires target commit {expected_commit}")
    try:
        status = subprocess.run(  # noqa: S603 - argv list, no shell
            ["git", "-C", str(repo_path), "status", "--porcelain"],  # noqa: S607 - git/tool resolved via PATH on purpose
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("canonical run could not verify target worktree cleanliness") from exc
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError("canonical run requires a clean target worktree")

    expected_index = _PARITY_MANIFEST["index"]
    index_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()
    if index_relocation is None and index_sha256 != expected_index["raw_sha256"]:
        raise ValueError("canonical run requires the locked index bytes")
    try:
        index_meta = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("canonical run index is not valid JSON") from exc
    if index_relocation is not None:
        verify_index_relocation(
            index_relocation,
            metadata=index_meta,
            index_sha256=index_sha256,
            repo_path=repo_path,
            frozen_index_sha256=expected_index["raw_sha256"],
        )
    for index_field in ("git_sha", "scan_version"):
        if index_meta.get(index_field) != expected_index[index_field]:
            raise ValueError(f"canonical run index {index_field} does not match the locked manifest")


def _validate_canonical_task(task: Mapping[str, Any]) -> None:
    """Reject a known primary task whose locked task or prompt identity changed."""
    task_id = task.get("id")
    expected = _PRIMARY_TASK_IDENTITIES.get(task_id) if isinstance(task_id, str) else None
    if expected is None:
        raise ValueError(f"no locked primary task identity for {task_id!r}")
    task_hash, expected_prompt_hash = expected
    if canonical_task_hash(task) != task_hash:
        raise ValueError(f"task hash mismatch for {task_id!r}")
    if prompt_hash(task) != expected_prompt_hash:
        raise ValueError(f"prompt hash mismatch for {task_id!r}")


def _resume_key(line: dict) -> tuple:
    """Build the resume-match key from a result line (or any dict with the key fields).

    Args:
        line: A dict carrying at least :data:`_RESUME_KEY_FIELDS`.

    Returns:
        A tuple of the six identifying values, in :data:`_RESUME_KEY_FIELDS` order.

    Examples:
        >>> _resume_key({"task_id": "SE-01", "arm": "plain", "model": "haiku",
        ...              "repo_sha": "a", "index_sha": "b", "task_hash": "c"})
        ('SE-01', 'plain', 'haiku', 'a', 'b', 'c')
    """
    return tuple(line.get(f, "unknown") for f in _RESUME_KEY_FIELDS)


def _load_resume_cache(results_dir: Path) -> dict[tuple, dict]:
    """Index every prior result line in *results_dir* by its resume key.

    Scans ``bench-*.jsonl`` files. Later files win on key collision, so a re-run's
    lines shadow an earlier partial run's. Malformed lines are skipped silently.

    Args:
        results_dir: Directory holding prior ``bench-*.jsonl`` result files.

    Returns:
        Mapping from resume key (see :func:`_resume_key`) to the stored line dict.
    """
    cache: dict[tuple, dict] = {}
    if not results_dir.is_dir():
        return cache
    for path in sorted(results_dir.glob("bench-*.jsonl")):
        try:
            text = path.read_text()
        except OSError:
            continue
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(line, dict):
                cache[_resume_key(line)] = line
    return cache


def _run_from_cached(line: dict) -> BenchRun:
    """Reconstruct a :class:`BenchRun` from a cached result line, flagged ``resumed``.

    Only the dataclass fields present in *line* are copied; unknown keys (from a newer
    or older schema) are ignored so a resume across schema drift still yields a usable
    run for reporting. ``quality`` is rebuilt into a :class:`BenchQuality`.

    Args:
        line: A prior result line dict (as written by :func:`_save_results`).

    Returns:
        A BenchRun equal to the cached run with ``resumed=True``.
    """
    field_names = {f.name for f in fields(BenchRun)}
    kwargs = {k: v for k, v in line.items() if k in field_names and k != "quality"}
    q_field_names = {f.name for f in fields(BenchQuality)}
    q_raw = line.get("quality") or {}
    q_kwargs = {k: v for k, v in q_raw.items() if k in q_field_names} if isinstance(q_raw, dict) else {}
    run = BenchRun(**kwargs)
    run.quality = BenchQuality(**q_kwargs)
    run.resumed = True
    return run


# ---------------------------------------------------------------------------
# Cost profiles + tiered protocol (cost levers: ``--profile``, ``--tiered``)
# ---------------------------------------------------------------------------


def _is_dev_task(task: dict) -> bool:
    """Return True when *task* is tagged for the stratified dev subset.

    Args:
        task: A task dict, possibly carrying a ``profiles`` list.

    Returns:
        True when ``_PROFILE_DEV`` is listed in the task's ``profiles`` tag.

    Examples:
        >>> _is_dev_task({"id": "SE-01", "profiles": ["dev"]})
        True
        >>> _is_dev_task({"id": "SE-02"})
        False
    """
    return _PROFILE_DEV in (task.get(_PROFILE_TAG_KEY) or [])


def _is_ri_task(task: dict) -> bool:
    """Return True when *task* belongs to the real_issue (RI) series.

    Args:
        task: A task dict.

    Returns:
        True when the task type is :data:`_RI_TASK_TYPE`.

    Examples:
        >>> _is_ri_task({"id": "RI-01", "type": "real_issue"})
        True
        >>> _is_ri_task({"id": "SE-01", "type": "symbol_extraction"})
        False
    """
    return task.get("type") == _RI_TASK_TYPE


def _gate_ri(tasks: list[dict], profile: str | None, explicit: bool) -> list[dict]:
    """Drop RI tasks unless the release profile is active or they were selected explicitly.

    RI runs are ~2M-token outliers; they are excluded from the fast dev / tiered-haiku default
    set. An explicit ``--tasks``/``--task-type`` selection or ``--profile release`` opts them back in.

    Args:
        tasks: The candidate task list after other selection.
        profile: Active profile (``dev``/``release``) or None.
        explicit: True when the caller selected tasks explicitly (``--tasks``/``--task-type``).

    Returns:
        The task list with RI tasks removed when gating applies, else unchanged.

    Examples:
        >>> ri = {"id": "RI-01", "type": "real_issue"}
        >>> se = {"id": "SE-01", "type": "symbol_extraction"}
        >>> [t["id"] for t in _gate_ri([ri, se], None, explicit=False)]
        ['SE-01']
        >>> [t["id"] for t in _gate_ri([ri, se], "release", explicit=False)]
        ['RI-01', 'SE-01']
        >>> [t["id"] for t in _gate_ri([ri, se], None, explicit=True)]
        ['RI-01', 'SE-01']
    """
    if profile == _PROFILE_RELEASE or explicit:
        return tasks
    return [t for t in tasks if not _is_ri_task(t)]


def _apply_profile(tasks: list[dict], profile: str | None) -> list[dict]:
    """Filter *tasks* down to the profile's subset.

    ``dev`` keeps only dev-tagged tasks; ``release`` keeps everything (RI included, gated
    separately). None leaves the list unchanged.

    Args:
        tasks: The candidate task list.
        profile: Active profile (``dev``/``release``) or None.

    Returns:
        The profile-filtered task list.

    Examples:
        >>> tasks = [{"id": "SE-01", "profiles": ["dev"]}, {"id": "SE-02"}]
        >>> [t["id"] for t in _apply_profile(tasks, "dev")]
        ['SE-01']
        >>> [t["id"] for t in _apply_profile(tasks, "release")]
        ['SE-01', 'SE-02']
        >>> [t["id"] for t in _apply_profile(tasks, None)]
        ['SE-01', 'SE-02']
    """
    if profile == _PROFILE_DEV:
        return [t for t in tasks if _is_dev_task(t)]
    return tasks


def _correct_by_task(results_dir: Path, model: str, repo_sha: str, index_sha: str) -> dict[str, bool]:
    """Read prior *model*-tier results and fold each task to a single correctness verdict.

    A task counts as correct for the tier only when every scored arm of that task in the
    matching results (same repo_sha + index_sha) was ``quality.correct``. Used by the tiered
    protocol to find haiku/sonnet disagreements for opus adjudication.

    Args:
        results_dir: Directory holding prior ``bench-*.jsonl`` result files.
        model: Tier model whose lines to read (e.g. ``haiku``).
        repo_sha: Provenance filter — only lines from this repo HEAD count.
        index_sha: Provenance filter — only lines from this index fingerprint count.

    Returns:
        Mapping task_id → conjunctive correctness across scored arms of that tier. Tasks with no
        scored arm are absent from the mapping.
    """
    scored: dict[str, list[bool]] = defaultdict(list)
    for line in _load_resume_cache(results_dir).values():
        if line.get("model") != model or line.get("repo_sha") != repo_sha or line.get("index_sha") != index_sha:
            continue
        quality = line.get("quality") or {}
        if not quality.get("scored") or quality.get("extraction_failed") or line.get("incomplete"):
            continue
        scored[line["task_id"]].append(bool(quality.get("correct")))
    return {tid: all(verdicts) for tid, verdicts in scored.items() if verdicts}


def _tiered_tasks(tasks: list[dict], model: str, results_dir: Path, repo_sha: str, index_sha: str) -> list[dict]:
    """Select the task subset for one tier of the tiered protocol.

    Sequencing (three invocations, one per model):

      * ``haiku``  → the full suite (RI gated separately by profile).
      * ``sonnet`` → the dev-tagged subset only.
      * ``opus``   → only tasks where the haiku and sonnet verdicts disagree (adjudication);
        requires both prior tiers' results in *results_dir*.

    Args:
        tasks: The candidate task list (already profile/RI filtered for the caller).
        model: The tier model being run.
        results_dir: Directory holding prior-tier ``bench-*.jsonl`` result files.
        repo_sha: Provenance filter for reading prior-tier verdicts.
        index_sha: Provenance filter for reading prior-tier verdicts.

    Returns:
        The task subset for this tier. Opus with no disagreements (or missing prior results)
        yields an empty list.
    """
    if model == _TIER_SONNET:
        return [t for t in tasks if _is_dev_task(t)]
    if model == _TIER_OPUS:
        haiku = _correct_by_task(results_dir, _TIER_HAIKU, repo_sha, index_sha)
        sonnet = _correct_by_task(results_dir, _TIER_SONNET, repo_sha, index_sha)
        disagree = {tid for tid in haiku.keys() & sonnet.keys() if haiku[tid] != sonnet[tid]}
        return [t for t in tasks if t["id"] in disagree]
    return tasks


@dataclass
class TaskSelection:
    """Inputs that determine which tasks run, bundled to keep helper signatures small.

    Attributes:
        all_tasks: Every loaded task (bench + any ``--tasks-file`` + patch).
        ids: Explicit ``--tasks`` id set, or None.
        task_type: Explicit ``--task-type`` filter, or None.
        run_all: True when ``--all`` was passed.
        external_ids: IDs supplied via ``--tasks-file``.
        patch_ids: IDs supplied via ``--patch``.
        profile: Active cost profile (``dev``/``release``) or None.
        tiered: True when the tiered protocol is active.
        model: Short model tier name (drives the tiered subset).
    """

    all_tasks: list[dict]
    ids: set[str] | None
    task_type: str | None
    run_all: bool
    external_ids: set[str]
    patch_ids: set[str]
    profile: str | None
    tiered: bool
    model: str


# gt_is_pending comes from benchmark_paths (shared with generate-tasks-bench).


def _base_task_list(sel: TaskSelection) -> list[dict] | None:
    """Apply the explicit/type/subset selection that predates the cost-lever flags.

    Args:
        sel: The bundled selection inputs.

    Returns:
        The base task list, or None when no selection was specified (caller reports the error).
        An empty list means a selector matched nothing.
    """
    if sel.ids is not None:
        return [t for t in sel.all_tasks if t["id"] in sel.ids]
    if sel.task_type:
        return [t for t in sel.all_tasks if t["type"] == sel.task_type]
    if sel.run_all:
        return list(sel.all_tasks)
    subset = sel.external_ids | sel.patch_ids
    if subset:
        return [t for t in sel.all_tasks if t["id"] in subset]
    return None


def _select_tasks(sel: TaskSelection, results_dir: Path, repo_sha: str, index_sha: str) -> list[dict] | None:
    """Resolve the final task list from base selection + profile + RI gating + tiered protocol.

    Order: base selection → profile subset → RI gating → tiered subset. The tiered step reads
    prior-tier results (for opus adjudication) via *results_dir* + provenance filters.

    Args:
        sel: The bundled selection inputs.
        results_dir: Directory holding prior ``bench-*.jsonl`` result files (tiered opus tier).
        repo_sha: Provenance filter for reading prior-tier verdicts.
        index_sha: Provenance filter for reading prior-tier verdicts.

    Returns:
        The final task list, None when no base selector was given, or an empty list when a
        selector matched nothing.
    """
    base = _base_task_list(sel)
    if base is None:
        return None
    explicit = sel.ids is not None or bool(sel.task_type)
    selected = _apply_profile(base, sel.profile)
    selected = _gate_ri(selected, sel.profile, explicit)
    if sel.tiered:
        selected = _tiered_tasks(selected, sel.model, results_dir, repo_sha, index_sha)
    return selected
