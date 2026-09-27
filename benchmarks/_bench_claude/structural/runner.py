"""The per-task, per-arm execution engine."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional


from _bench_common.claude_transport import parse_result_usage, stream_claude
from _bench_common.provider_parity_contracts import (
    ARM_CONTRACTS,
    TaskPolicy,
    capability_strata,
    materialize_task_prompt,
    treatment_adherence,
)

from _bench_claude.structural.config import (
    LEGACY_EXPERIMENT_REVISION,
    PARITY_EXPERIMENT_REVISION,
    PRIMARY_SUITE_HASH,
    _ARM_ALLOWED,
    _ARM_DISALLOWED,
    _CMD,
    _REPO_NAME,
    _SELF_CONSISTENCY_KEY,
)
from _bench_claude.structural.models import BenchQuality, BenchRun, _BenchEvaluationResult
from _bench_claude.structural.prompts import _build_system_prompt, _transport_arm
from _bench_claude.structural.tasks import (
    _index_sha,
    _prompt_hash,
    _repo_sha,
    _run_from_cached,
    _task_hash,
    _validate_canonical_task,
)
from _bench_claude.structural.telemetry import (
    _BATCH_SUBCOMMAND,
    _CONTAMINATION_MARKERS,
    _embedded_json_objects,
    _is_contaminating_access,
    _max_turns_for_task,
    _parse_batch_subcommands,
    _parse_scan_query_subcommand,
    _subprocess_env,
)
from _bench_claude.structural.evaluators import _SHARED_EVALUATORS, _arm_contract_hash, _evaluator_provenance


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class BenchRunner:
    """Run benchmark tasks against Claude CLI in plain or codemap arm.

    Args:
        model_short: Short model tier name (e.g. "haiku").
        model_id: Full Claude model identifier.
        repo_path: Root of the target repository clone.
        index_path: Path to the pre-built codemap index.
        timeout: Wall-clock seconds per run before killing the subprocess.
        resume_cache: When non-None, a prior-results cache (see :func:`_load_resume_cache`);
            a matching (task, arm, model, repo_sha, index_sha, task_hash) tuple is reused
            instead of re-executing the claude subprocess. None disables resume (default).
        task_policies: Locked task policies used to stamp current-revision parity provenance.
    """

    def __init__(
        self,
        model_short: str,
        model_id: str,
        repo_path: Path,
        index_path: Path,
        timeout: int = 300,
        resume_cache: Optional[dict[tuple, dict]] = None,
        task_policies: Mapping[str, TaskPolicy] | None = None,
        suite_hash: str | None = None,
        suite_raw_hash: str | None = None,
    ) -> None:
        self.model_short = model_short
        self.model_id = model_id
        self.repo_path = repo_path
        self.index_path = index_path
        self.timeout = timeout
        self.resume_cache = resume_cache
        self.task_policies = task_policies
        self.suite_hash = suite_hash
        self.suite_raw_hash = suite_raw_hash
        # Provenance stamped on every run so results can be matched on a later ``--resume`` pass.
        self.repo_sha = _repo_sha(repo_path)
        self.index_sha = _index_sha(index_path)

    def _stamp_provenance(self, result: BenchRun, task: dict, task_hash: str) -> None:
        """Attach provenance + self-consistency metadata to *result* in place.

        Args:
            result: The BenchRun to annotate.
            task: The task dict being run.
            task_hash: Precomputed sha256 of the task JSON.
        """
        result.repo_sha = self.repo_sha
        result.index_sha = self.index_sha
        result.task_hash = task_hash
        result.prompt_hash = _prompt_hash(task)
        result.prompt_sha256 = result.prompt_hash
        result.suite_hash = self.suite_hash
        result.suite_raw_hash = self.suite_raw_hash
        result.evaluator_id, result.evaluator_hash = _evaluator_provenance(task)
        envelope = _build_system_prompt(result.arm, _REPO_NAME, str(self.repo_path), str(self.index_path))
        result.envelope_hash = hashlib.sha256(envelope.encode("utf-8")).hexdigest()
        result.arm_contract_hash = _arm_contract_hash(result.arm)
        result.self_consistency = bool(task.get(_SELF_CONSISTENCY_KEY))
        result.scoreable = task.get("scoreable") is not False
        if result.arm not in ARM_CONTRACTS:
            result.experiment_revision = LEGACY_EXPERIMENT_REVISION
            result.parity_arm = ""
            result.oracle_class = "legacy"
            result.headline_eligible_v1 = False
            return
        policy = self.task_policies.get(result.task_id) if self.task_policies is not None else None
        if policy is None:
            raise ValueError(f"no locked task policy for canonical task {result.task_id!r}")
        result.experiment_revision = policy.experiment_revision
        result.parity_arm = result.arm
        result.oracle_class = policy.oracle_class
        result.headline_eligible_v1 = policy.headline_eligible_v1
        result.scoreable = policy.scoreable

    def run(self, task: dict, arm: str, update_fn: Optional[Any] = None) -> BenchRun:
        """Run one task in one arm; parse stream-json for metrics.

        When a resume cache is active and holds a matching prior result for this
        (task, arm, model, repo_sha, index_sha, task_hash) tuple, that line is reused
        (``resumed=True``) and the claude subprocess is skipped entirely.

        Args:
            task: Task dict from tasks-bench.json.
            arm: "plain" or "codemap".
            update_fn: Optional ``(elapsed_s, run)`` callback forwarded to ``_stream``
                for live sub-progress display.

        Returns:
            BenchRun with all metrics filled.
        """
        task_hash = _task_hash(task)
        if arm in ARM_CONTRACTS:
            if self.suite_hash != PRIMARY_SUITE_HASH:
                raise ValueError("canonical run requires the locked primary suite hash")
            _validate_canonical_task(task)
        if self.resume_cache is not None:
            key = (task["id"], arm, self.model_short, self.repo_sha, self.index_sha, task_hash)
            cached = self.resume_cache.get(key)
            expected_revision = PARITY_EXPERIMENT_REVISION if arm in ARM_CONTRACTS else LEGACY_EXPERIMENT_REVISION
            expected_contract_hash = _arm_contract_hash(arm)
            cached_revision = cached.get("experiment_revision", LEGACY_EXPERIMENT_REVISION) if cached else None
            cached_contract_hash = cached.get("arm_contract_hash", "") if cached else None
            if (
                cached is not None
                and cached_revision == expected_revision
                and cached_contract_hash == expected_contract_hash
            ):
                run = _run_from_cached(cached)
                self._stamp_provenance(run, task, task_hash)
                return run
        result = self._execute(task, arm, update_fn=update_fn)
        self._stamp_provenance(result, task, task_hash)
        return result

    def _execute(self, task: dict, arm: str, update_fn: Optional[Any] = None) -> BenchRun:
        """Execute one (task, arm) via the claude subprocess and score the output.

        Split out of :meth:`run` so the resume fast-path stays a thin guard. Contains the
        subprocess launch, retry loop, incomplete/scoreable handling, and contamination guards.

        Args:
            task: Task dict from tasks-bench.json.
            arm: "plain" or "codemap".
            update_fn: Optional live-progress callback forwarded to ``_stream``.

        Returns:
            A freshly executed BenchRun (provenance stamped by the caller).
        """
        system = _build_system_prompt(arm, _REPO_NAME, str(self.repo_path), str(self.index_path))
        disallow_flags = _ARM_DISALLOWED.get(arm, [])
        allow_flags = _ARM_ALLOWED.get(arm, [])
        # Codex has no equivalent public turn cap, so parity arms use only their shared wall clock.
        # Legacy labels retain their exact historical per-task cap.
        turn_flags = [] if arm in ARM_CONTRACTS else ["--max-turns", str(_max_turns_for_task(task))]
        cmd = [
            *_CMD,
            *turn_flags,
            "--model",
            self.model_id,
            *disallow_flags,
            *allow_flags,
            "--system-prompt",
            system,
            materialize_task_prompt(task),
        ]
        # workflow_type groups tasks at a coarser level than task_type; default to task_type
        # so legacy task files (no workflow_type field) still group sensibly.
        workflow_type = task.get("workflow_type") or task["type"]
        task_capability_strata = capability_strata(task)
        result = BenchRun(
            arm=arm,
            task_id=task["id"],
            task_type=task["type"],
            model=self.model_short,
            success=False,
            workflow_type=workflow_type,
            capability_strata=task_capability_strata,
        )
        _MAX_RETRIES = 2
        for attempt in range(_MAX_RETRIES + 1):
            result = BenchRun(
                arm=arm,
                task_id=task["id"],
                task_type=task["type"],
                model=self.model_short,
                success=False,
                workflow_type=workflow_type,
                capability_strata=task_capability_strata,
            )
            self._stream(cmd, result, arm, update_fn=update_fn)
            if result.input_tokens == 0 and result.output_tokens == 0 and attempt < _MAX_RETRIES:
                result.error = f"api_failure_retry_{attempt + 1}"
                time.sleep(2**attempt)  # exponential backoff: 1s, 2s
                continue
            break

        # Treat budget-exhaustion as incomplete rather than a zero-recall failure.
        # Agent never produced a final answer — scoring partial output_text would measure
        # token luck, not blast-radius comprehension.
        if result.error == "error_max_turns":
            result.incomplete = True
        elif task.get("scoreable") is False:
            # Task explicitly opted out of scoring (scoreable=false) — e.g. tasks-code.json,
            # RI-05 (wrong repo layout). Record token ratio + tool counts only; exclude from
            # accuracy denominator.
            result.quality = BenchQuality(scored=False)
        else:
            shared_evaluation = _SHARED_EVALUATORS.evaluate(task, result.output_text)
            if not isinstance(shared_evaluation, _BenchEvaluationResult):
                raise TypeError("shared evaluator did not return Claude benchmark diagnostics")
            result.quality = shared_evaluation.bench_quality
            result.quality_components = shared_evaluation.components
            # Contamination guards:
            #  - plain arm: detect codemap binary OR prebuilt-index access that bypassed the disallow
            #    list — either invoked via python3 path instead of bare scan-query, or a raw
            #    Read/cat of .cache/{codemap,scan}/*.json (the full structural answer). The primary
            #    signal is result.contamination_hits, counted in _handle against the FULL untruncated
            #    tool input; the truncated tool_log scan is kept as a fallback.
            #  - either arm: detect reads of ground-truth answer files (tasks-bench.json,
            #    benchmark results) which would let the agent copy the expected answer.
            _ANSWER_MARKERS = ("tasks-bench", "benchmarks/results", "/benchmarks/")
            _log_contaminated = any(marker in entry for entry in result.tool_log for marker in _CONTAMINATION_MARKERS)
            if _transport_arm(arm) == "plain" and (result.contamination_hits > 0 or _log_contaminated):
                result.error = "contaminated"
                result.quality = BenchQuality(scored=False)
            elif any(marker in entry for entry in result.tool_log for marker in _ANSWER_MARKERS):
                result.error = "answer_file_read"
                result.quality = BenchQuality(scored=False)

        if arm == "C_strict":
            result.compliance = result.scan_query_calls > 0
        result.contaminated = result.error in {"contaminated", "answer_file_read"}
        if arm in ARM_CONTRACTS:
            result.treatment_adherence = treatment_adherence(
                arm,
                codemap_use_compliance=result.compliance,
                contaminated=result.contaminated,
            )
        return result

    def _env(self, arm: str) -> dict[str, str]:
        """Return subprocess environment; arm-aware to avoid control-arm contamination.

        Args:
            arm: "plain" or "codemap". Plain arm gets no CODEMAP_* vars or bin PATH.

        Returns:
            Dict suitable for subprocess.Popen env argument.
        """
        if _transport_arm(arm) == "plain":
            env = os.environ.copy()
            env.pop("CODEMAP_INDEX", None)
            env.pop("CODEMAP_ENABLED", None)
            return env
        return _subprocess_env(self.index_path)

    def _stream(
        self,
        cmd: list[str],
        result: BenchRun,
        arm: str,
        update_fn: Optional[Any] = None,
    ) -> None:
        """Launch claude subprocess and parse stream-json events into result.

        Args:
            cmd: Full claude CLI command list.
            result: BenchRun to populate in-place.
            arm: "plain" or "codemap"; forwarded to _env for isolation.
            update_fn: Optional ``(elapsed_s, run)`` callback called ≤2× per second
                with live subprocess state for sub-progress displays.
        """
        pending: dict[str, float] = {}

        def _on_event(event: dict, ts: float) -> None:
            self._handle(event, result, pending, ts)

        outcome = stream_claude(
            cmd,
            timeout=self.timeout,
            cwd=self.repo_path,
            env=self._env(arm),
            on_event=_on_event,
            update_fn=(lambda elapsed: update_fn(elapsed, result)) if update_fn is not None else None,
        )
        # Map mechanics onto the run, preserving this lane's error precedence and the incomplete flag:
        # stderr (on a non-success run) → timeout (marks incomplete) → any unexpected exception.
        result.elapsed_s = outcome.elapsed_s
        if not result.success and not result.error and outcome.stderr:
            result.error = outcome.stderr.strip()[:300]
        if outcome.returncode is not None and outcome.returncode < 0 and not result.error:
            result.error = f"timeout ({self.timeout}s)"
            result.incomplete = True
        if outcome.exc_timeout:
            result.error = f"timeout ({self.timeout}s)"
            result.incomplete = True
        if outcome.error and not result.error:
            result.error = outcome.error

    @staticmethod
    def _extract_codemap_meta(block: dict, result: "BenchRun") -> None:
        """Parse a tool_result block for scan-query index metadata.

        Extracts ``index.method`` and ``index.not_covered`` from any JSON
        content that looks like a scan-query response, accumulating unique
        values into *result*.

        Args:
            block: A tool_result content block from the Claude stream.
            result: BenchRun to update in-place.
        """
        content = block.get("content", [])
        # content may be a string or a list of content blocks
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for cb in content:
                if isinstance(cb, dict) and cb.get("type") == "text":
                    texts.append(cb.get("text", ""))
        # Scan for the scan-query JSON object even when it is embedded in prose, truncated, or
        # concatenated — requiring the whole tool_result to be pure JSON silently dropped index
        # metadata (~16/17 codemap runs recorded no method despite running rdeps).
        for raw in texts:
            for parsed in _embedded_json_objects(raw):
                idx = parsed.get("index")
                if not isinstance(idx, dict):
                    continue
                method = idx.get("method", "")
                if method and method not in result.codemap_methods:
                    result.codemap_methods.append(method)
                for nc in idx.get("not_covered", []):
                    if nc not in result.codemap_not_covered:
                        result.codemap_not_covered.append(nc)

    @staticmethod
    def _record_tool_use(name: str, inp: dict, result: BenchRun) -> None:
        """Record one tool_use block into *result*: per-tool counters, contamination, and tool_log.

        Args:
            name: Tool name (e.g. "Grep", "Bash", "Read", "Skill").
            inp: The tool_use ``input`` dict.
            result: BenchRun to update in-place.
        """
        if name == "Grep":
            result.grep_calls += 1
            result.tool_log.append(f"Grep: {inp.get('pattern', '')[:60]!r}")
        elif name == "Bash":
            result.bash_calls += 1
            cmd = inp.get("command", "")
            if "scan-query" in cmd or "codemap-py/bin" in cmd:
                result.scan_query_calls += 1
                sub = _parse_scan_query_subcommand(cmd)
                if sub is not None:
                    result.scan_query_subcommands[sub] = result.scan_query_subcommands.get(sub, 0) + 1
                # In batch mode, attribute each inner {cmd} to its own subcommand
                # counter so a batched fn-rdeps counts as fn-rdeps, and flag used_batch for the run.
                # The outer `batch` counter above is kept so total batch invocations stay visible.
                if sub == _BATCH_SUBCOMMAND:
                    result.used_batch = True
                    for inner in _parse_batch_subcommands(cmd):
                        result.scan_query_subcommands[inner] = result.scan_query_subcommands.get(inner, 0) + 1
            # Count index/binary access on the FULL command (plain arm only).
            if _transport_arm(result.arm) == "plain" and _is_contaminating_access(cmd):
                result.contamination_hits += 1
            result.tool_log.append(f"Bash: {cmd[:80]}")
        elif name == "Read":
            result.read_calls += 1
            file_path = inp.get("file_path", "")
            # A plain-arm Read of the prebuilt index is contamination; check the FULL
            # untruncated path before it is clipped for the display log.
            if _transport_arm(result.arm) == "plain" and _is_contaminating_access(file_path):
                result.contamination_hits += 1
            result.tool_log.append(f"Read: {file_path[:60]}")
        elif name == "Skill":
            result.skill_calls += 1
            _sk = inp.get("skill", "") or ""
            _sk_short = _sk.split(":")[-1] if ":" in _sk else _sk
            result.skill_counts[_sk_short] = result.skill_counts.get(_sk_short, 0) + 1
            result.tool_log.append(f"Skill: {_sk} {inp.get('args', '')}".strip())
        else:
            first_val = next((v for v in inp.values() if isinstance(v, str)), "") if inp else ""
            result.tool_log.append(f"{name}: {first_val[:50]}" if first_val else name)

    def _handle(self, event: dict, result: BenchRun, pending: dict[str, float], ts: float) -> None:
        """Route a single stream-json event to the appropriate handler.

        Args:
            event: Parsed JSON event dict.
            result: BenchRun to populate in-place.
            pending: Map of tool_use_id → start timestamp (for elapsed tracking).
            ts: Current monotonic timestamp.
        """
        etype = event.get("type", "")

        if etype == "assistant":
            result.turn_count += 1
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    result.output_text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    pending[block.get("id", "")] = ts
                    self._record_tool_use(block.get("name", ""), block.get("input", {}) or {}, result)

        elif etype == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id", "")
                pending.pop(tool_id, None)
                if _transport_arm(result.arm) == "codemap":
                    self._extract_codemap_meta(block, result)

        elif etype == "result":
            # Anthropic's own cost (total_cost_usd) is captured here — current prices, cache-aware,
            # per model. When the result event omits it the $ column is dropped (in/out tokens remain).
            u = parse_result_usage(event)
            result.input_tokens = u.input_tokens
            result.output_tokens = u.output_tokens
            result.cost_usd = u.cost_usd
            result.success = u.success
            if not result.success and not result.error:
                result.error = u.subtype
