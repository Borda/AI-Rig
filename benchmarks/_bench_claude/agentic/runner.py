"""The per-task, per-arm model execution engine."""

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.
from _bench_common.claude_stages import (
    _claude_codemap_evidence,
    _claude_message_blocks,
)
from _bench_common.claude_transport import parse_result_usage, stream_claude
from _bench_common.codemap_discovery import codemap_bin_on_path
from _bench_common.mutation_isolation import (
    relocate_frozen_index_for_worktree,
)

from _bench_claude.agentic.config import REPO_ROOT
from _bench_claude.agentic.discovery import _tool_key_arg
from _bench_claude.agentic.evidence import (
    _benchmark_evidence_roots,
    _claude_evidence_settings_file,
    _staged_codemap_runtime,
)
from _bench_claude.agentic.models import BenchmarkRun, Task, parity_arm_identity
from _bench_claude.agentic.provenance import _invokes_scan_query
from _bench_claude.agentic.scoring import _capture_tool_result_text, _iter_tool_result_texts


class ModelRunner:
    """Runs benchmark tasks against a specific Claude model tier.

    Encapsulates model identity, repo path, and timeout. The ``run()`` method launches a Claude subprocess and parses
    stream-json events into a ``BenchmarkRun`` result.

    Arm prompts and CLI constants live here — only ModelRunner constructs or launches claude; nothing else needs them.
    """

    # Base claude CLI invocation.
    # ``--setting-sources project,local`` excludes USER-level config (caveman plugin, foundry Re:Anchor
    # box+▓ footer, user CLAUDE.md, user hooks) so the agent's output is not shaped/inflated by the
    # operator's personal setup — identical isolation on every arm. Excluding user also drops the
    # codemap plugin and semble MCP, so they are re-supplied per arm via ``--plugin-dir`` and ``--mcp-config``
    # in _arm_isolation_flags (the tools under test must survive isolation). Subscription auth is
    # not a setting source, so it is unaffected.
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
    # Tools counted as exploration overhead
    EXPLORATION_TOOLS = {"Grep", "Glob", "Bash", "Skill", "mcp__semble__search", "mcp__semble__find_related"}
    # Tools blocked per arm via ``--disallowed-tools`` to enforce mutual exclusion
    # Bash is kept available for every non-plain arm (and plain) so each has the same read-only
    # shell fallback on a primary-tool error; blocking it for semble alone was an asymmetric
    # handicap. Only the primary discriminator differs: codemap blocks semble MCP,
    # semble blocks the Skill tool, plain blocks both structural entry points.
    _ARM_DISALLOWED: dict[str, list[str]] = {
        "codemap": ["--disallowed-tools", "mcp__semble__search,mcp__semble__find_related"],
        "B_auto": ["--disallowed-tools", "Agent,Task,mcp__semble__search,mcp__semble__find_related"],
        "C_strict": ["--disallowed-tools", "Agent,Task,mcp__semble__search,mcp__semble__find_related"],
        "semble": ["--disallowed-tools", "Skill"],
        "plain": ["--disallowed-tools", "Skill,mcp__semble__search,mcp__semble__find_related"],
        "A_plain": [
            "--disallowed-tools",
            "Skill,Agent,Task,mcp__semble__search,mcp__semble__find_related,Bash(scan-query:*)",
        ],
        "combined": [],
    }

    # Tools pre-approved per arm via ``--allowedTools``. In headless -p mode a tool the arm relies
    # on MUST be pre-approved here or it is permission-denied (returns <tool_use_error>). Canonical
    # P1 C treatment evidence is a completed direct ``codemap-py query`` Bash call. Match the
    # production Skill's absolute-launcher form (including its closing quote) as well as the PATH
    # form; a successful Skill wrapper alone does not prove its nested call used the frozen index.
    _CODEMAP_SKILLS = "Skill(codemap:query-code),Skill(codemap-py:query-code)"
    _ARM_ALLOWED: dict[str, list[str]] = {
        "codemap": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),{_CODEMAP_SKILLS}",
        ],
        "B_auto": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),{_CODEMAP_SKILLS}",
        ],
        "C_strict": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),{_CODEMAP_SKILLS}",
        ],
        "semble": ["--allowedTools", "mcp__semble__search,mcp__semble__find_related"],
        "combined": [
            "--allowedTools",
            f"Bash(scan-query:*),Bash(codemap-py query:*),Bash(*/bin/codemap-py* query:*),mcp__semble__search,mcp__semble__find_related,{_CODEMAP_SKILLS}",
        ],
    }

    # Arm system prompts -------------------------------------------------------
    # PLAIN arm:   minimal fix/feature/refactor/review skill, no codemap.
    # CODEMAP arm: same skill + /codemap:query instruction.
    _PLAIN_SKILLS: dict[str, str] = {
        "fix": (
            "You are a software engineer fixing a bug in a Python codebase. "
            "Before writing any fix, investigate the affected module: understand what "
            "other modules depend on it and what it depends on, so you know the full "
            "blast radius of any interface change."
        ),
        "feature": (
            "You are a software engineer adding a new feature to a Python codebase. "
            "Before writing any code, explore the relevant modules to identify "
            "integration points, coupling risks, and which files you will need to modify."
        ),
        "refactor": (
            "You are a software engineer refactoring a Python codebase. "
            "Before changing anything, map out every module that imports the code "
            "being restructured so you understand the full scope of the change."
        ),
        "review": (
            "You are a software engineer reviewing a code change in a Python codebase. "
            "Identify all modules that depend on the changed code, assess the blast "
            "radius, and flag the highest regression risks."
        ),
    }
    # Shared efficiency sentence — identical for every arm so tool-call count reflects the
    # agent's own choices, not asymmetric steering.
    _EFFICIENCY = "\n\nAnswer in as few tool calls as possible; do not re-verify results you already have."

    # Shared, arm-neutral answer format. This is the erec/rrec extraction target and MUST be
    # identical across all four arms — any per-arm wording here would bias the measured signal.
    # Placeholders are generic (no hardcoded corpus paths) so no arm is primed with example modules.
    _ANSWER_FORMAT = """

## Required answer format

Your final answer MUST end with this section:

## Reverse Dependencies Found

Count: <N> distinct modules found.

- <full.dotted.module.path>
- <full.dotted.module.path>
- ... (one line per module)

Rules:
- Write "Count: N distinct modules found." where N = the exact number in your list
- Full dotted paths only — no shortened names, no file paths, no aliases
- List every module you found — no omissions
- If nothing found: write "Count: 0" and "(none found)"
- This section must be the LAST thing in your answer"""

    # Per-arm supplements below carry tool availability + invocation syntax ONLY. No call caps,
    # no "do not verify" rules, no step protocols — that steering is the measured signal and would
    # manufacture the benchmark savings it claims to observe.
    _PLAIN_SUPPLEMENT = """

## Tools available

Grep, Glob, Bash, and Read are available for exploring the codebase and its import graph."""

    _CODEMAP_SUPPLEMENT = """

## Tools available

You have the /codemap:query-code skill (via the Skill tool). It answers import-graph questions
from a pre-built structural index.

Syntax — colon separator, never a space:
  codemap:query-code      (correct)
  codemap query-code      (wrong — fails silently)

Invocation:
  /codemap:query-code rdeps <primary_module> [--exclude-tests]

Grep, Glob, Bash, and Read remain available.

If /codemap:query-code returns <tool_use_error>, run one Grep/Bash fallback for the same query."""

    _C_STRICT_SUPPLEMENT = (
        "\n\nYou must use Codemap at least once for structural investigation. When the task supplies an exact "
        "`/codemap-py:query-code` invocation, load that Skill and complete at least one standalone successful "
        "`codemap-py query --compact` before using other source tools; do not prefix, assign, wrap, or combine the "
        "credited query with shell work; loading the Skill alone, or a query without `--compact`, does not satisfy "
        "the requirement."
    )

    _SEMBLE_SUPPLEMENT = """

## Tools available

You have the mcp__semble__search and mcp__semble__find_related tools. They perform hybrid
semantic + lexical search across the codebase and return ranked code chunks with file path
and line range.

Parameters:
  query (str)   — natural language or code query
  repo  (str)   — REQUIRED: absolute path to the repository: {repo_path}
  top_k (int)   — number of results (default 5; raise it for broader coverage)

Grep, Glob, Bash, and Read are available for reading source code; the Skill tool is not.

If mcp__semble__search returns <tool_use_error>, run one Grep/Bash fallback for the same query."""

    _COMBINED_SUPPLEMENT = """

## Tools available

You have both /codemap:query-code (Skill tool, deterministic index) and mcp__semble__search /
mcp__semble__find_related (semantic search). Choose whichever fits each question.

codemap syntax — colon separator, never a space:
  /codemap:query-code rdeps <primary_module> [--exclude-tests]

semble parameters:
  query (str), repo (str, REQUIRED: {repo_path}), top_k (int)

Grep, Glob, Bash, and Read remain available.

If a structural tool returns <tool_use_error>, run one Grep/Bash fallback for the same query."""

    # read_crop task family — measures READ cost: extract ONE symbol's contract using the
    # fewest tokens of file content. Headline metric is tool_result_tokens (codemap `symbol`
    # extraction vs plain whole-file Read); correctness via keyword recall (score_read_crop).
    _READCROP_BASE = (
        "You are a software engineer answering a precise question about ONE symbol "
        "(function / method / class) in a Python codebase. Find that symbol's source, then state "
        "its full contract — every parameter and what it does. Use the FEWEST tokens of file "
        "content possible: do NOT read unrelated code, and do NOT read an entire large module file "
        "when you only need one symbol."
    )
    _READCROP_PLAIN = (
        "\n\n## Reading tools\n"
        "Use Grep to locate the symbol's definition line, then Read ONLY the needed line range "
        "(pass offset/limit) — never read the whole file if the symbol is a small part of it."
    )
    _READCROP_CODEMAP = (
        "\n\n## Codemap integration\n"
        "The installed `/codemap-py:query-code` Skill can extract one symbol with its imports. Invoke the Skill when "
        "the treatment or unresolved structural question requires it, then follow its current `codemap-py query` "
        "syntax. Its completed symbol source is authoritative structural evidence; do not read an entire module "
        "when that result is complete."
    )
    _READCROP_SEMBLE = (
        "\n\n## semble installed — search then read the chunk\n"
        'Call mcp__semble__search with query naming the symbol and repo="{repo_path}", top_k=5. '
        "Read only the returned chunk's line range — do not read the whole file."
    )

    # fix_single task family — single-function / single-file bug fix; scored by diff keyword recall.
    _FIXSINGLE_BASE = (
        "You are a software engineer fixing a specific bug in a Python codebase. "
        "Read the relevant source file(s), understand the described issue, then apply the "
        "**minimal fix** using the Edit tool. Do not refactor unrelated code. "
        "The fix should be complete and correct — the scorer checks the diff for expected change markers."
    )
    _FIXSINGLE_PLAIN = (
        "\n\n## Tools\n"
        "Use Grep to locate the relevant class/function, then Read only the needed lines. "
        "Apply the fix with Edit."
    )
    _FIXSINGLE_CODEMAP = (
        "\n\n## Codemap integration\n"
        "The installed `/codemap-py:query-code` Skill can extract a target symbol without reading the whole file. "
        "Invoke it when the treatment or an unresolved structural question requires it, then follow its current "
        "`codemap-py query` syntax. For automatic use, retain the Skill's localized-edit skip rule."
    )
    _FIXSINGLE_SEMBLE = (
        "\n\n## semble installed\n"
        'Call mcp__semble__search with the symbol name and repo="{repo_path}" to locate the '
        "relevant code, then apply the fix with Edit."
    )

    # fix_multicaller task family — signature change that requires updating multiple callers;
    # scored by diff keyword recall + file recall. Codemap's rdeps is the decisive tool here.
    _FIXMULTI_BASE = (
        "You are a software engineer making a signature change that touches multiple call sites. "
        "Before writing any code: **find ALL callers of the function being changed**. "
        "Then edit the function definition AND every caller. Miss a caller = incomplete fix."
    )
    _FIXMULTI_PLAIN = (
        "\n\n## Tools\n"
        "Use grep/bash to find all callers of the function (search for the function name as a string). "
        "Edit the definition first, then each caller."
    )
    # Tool availability + syntax ONLY — no "do this FIRST", no "do NOT grep", no "decisive
    # advantage" framing. That steering was the measured signal and manufactured the tool-call
    # gap, mirroring the corresponding rdep-supplement treatment.
    _FIXMULTI_CODEMAP = (
        "\n\n## Codemap integration\n"
        "The installed `/codemap-py:query-code` Skill answers caller and import-graph questions from a pre-built "
        "structural index. Invoke the Skill when the treatment or unresolved affected-surface question requires it, "
        "then follow its current `codemap-py query` syntax. Grep, Glob, Bash, and Read remain available for distinct "
        "source/runtime facts."
    )
    _FIXMULTI_SEMBLE = (
        "\n\n## semble installed\n"
        'Call mcp__semble__search with the function name and repo="{repo_path}" to locate callers, '
        "then edit the definition and all callers."
    )

    def _system_prompt(self, task_type: str, arm: str) -> str:
        """Build the system prompt for one arm × task-type combination.

        The shared ``_EFFICIENCY`` sentence is appended for every task family (fix, read_crop, and rdep) so no arm is
        uniquely nudged toward more or fewer tool calls. Canonical parity tasks put their exact labelled JSON answer
        contract in the user prompt; legacy tasks retain their historical reverse-dependency output format.
        """
        if task_type == "fix_single":
            supplement = {
                "codemap": self._FIXSINGLE_CODEMAP,
                "B_auto": self._FIXSINGLE_CODEMAP,
                "C_strict": self._FIXSINGLE_CODEMAP + self._C_STRICT_SUPPLEMENT,
                "semble": self._FIXSINGLE_SEMBLE.format(repo_path=self.repo_path),
            }.get(arm, self._FIXSINGLE_PLAIN)
            return self._FIXSINGLE_BASE + supplement + self._EFFICIENCY
        if task_type == "fix_multicaller":
            supplement = {
                "codemap": self._FIXMULTI_CODEMAP,
                "B_auto": self._FIXMULTI_CODEMAP,
                "C_strict": self._FIXMULTI_CODEMAP + self._C_STRICT_SUPPLEMENT,
                "semble": self._FIXMULTI_SEMBLE.format(repo_path=self.repo_path),
            }.get(arm, self._FIXMULTI_PLAIN)
            return self._FIXMULTI_BASE + supplement + self._EFFICIENCY
        if task_type == "read_crop":
            supplement = {
                "codemap": self._READCROP_CODEMAP,
                "B_auto": self._READCROP_CODEMAP,
                "C_strict": self._READCROP_CODEMAP + self._C_STRICT_SUPPLEMENT,
                "semble": self._READCROP_SEMBLE.format(repo_path=self.repo_path),
            }.get(arm, self._READCROP_PLAIN)
            return self._READCROP_BASE + supplement + self._EFFICIENCY
        base = self._PLAIN_SKILLS.get(task_type, self._PLAIN_SKILLS["fix"])
        if arm in ("codemap", "B_auto"):
            supplement = self._CODEMAP_SUPPLEMENT
        elif arm == "C_strict":
            supplement = self._CODEMAP_SUPPLEMENT + self._C_STRICT_SUPPLEMENT
        elif arm == "semble":
            supplement = self._SEMBLE_SUPPLEMENT.format(repo_path=self.repo_path)
        elif arm == "combined":
            supplement = self._COMBINED_SUPPLEMENT.format(repo_path=self.repo_path)
        else:
            supplement = self._PLAIN_SUPPLEMENT
        # Canonical tasks carry their exact JSON contract in the user prompt. Keeping the legacy
        # format out of that path avoids contradictory output instructions while preserving it for
        # explicitly selected historical arms.
        answer_format = "" if parity_arm_identity(arm) else self._ANSWER_FORMAT
        return base + self._EFFICIENCY + supplement + answer_format

    def __init__(
        self,
        model_short: str,
        model_id: str,
        repo_path: Path,
        timeout: int = 300,
    ) -> None:
        self.model_short = model_short
        self.model_id = model_id
        self.repo_path = repo_path
        self.timeout = timeout

    def run_stage_events(
        self,
        *,
        prompt: str,
        system_prompt: str,
        arm: str,
        cwd: Path,
        writable: bool = False,
        evidence_roots: Sequence[Path] = (),
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        """Run one canonical stage prompt through the shared Claude transport.

        This method is the provider seam used by ReadCrop, Fix-Single, and
        Fix-Multi. It constructs Claude's isolated arm invocation, while
        ``stream_claude`` remains the sole subprocess/event loop. Raw events are
        returned losslessly for stage-owned normalization and artifact capture.

        Args:
            prompt: Exact user task and answer/edit contract.
            system_prompt: Arm-neutral role and execution constraints.
            arm: Canonical ``A_plain``, ``B_auto``, or ``C_strict`` treatment.
            cwd: Benchmark-owned disposable repository visible to Claude.
            writable: Use native edit-accepting mode for an isolated executable
                worktree; leave read-only studies in the default mode.
            evidence_roots: Additional live result roots that must remain hidden
                from this cell alongside the evaluator checkout.

        Returns:
            Raw events, elapsed seconds, and a bounded transport error or
            ``None`` after a normal provider exit.
        """
        permission_flags = ["--permission-mode", "acceptEdits"] if writable else []
        preamble_flag = Path(tempfile.gettempdir()) / f"codemap-preamble-{cwd.name}"
        preamble_flag.unlink(missing_ok=True)
        events: list[dict[str, Any]] = []
        denied_evidence = _benchmark_evidence_roots(evidence_roots)
        with _staged_codemap_runtime(arm, cwd) as runtime:
            runtime_paths = [runtime] if runtime is not None else []
            with _claude_evidence_settings_file(
                cwd,
                evidence_roots=denied_evidence,
                runtime_paths=runtime_paths,
            ) as settings_path:
                cmd = [
                    *self._CMD,
                    "--model",
                    self.model_id,
                    *permission_flags,
                    "--settings",
                    str(settings_path),
                    *self._arm_isolation_flags(arm, codemap_plugin_dir=runtime),
                    *self._ARM_DISALLOWED.get(arm, []),
                    *self._ARM_ALLOWED.get(arm, []),
                    "--system-prompt",
                    system_prompt,
                    prompt,
                ]
                outcome = stream_claude(
                    cmd,
                    timeout=self.timeout,
                    cwd=cwd,
                    env=self._subprocess_env(arm, codemap_plugin_dir=runtime),
                    on_event=lambda event, _timestamp: events.append(dict(event)),
                )
        error = outcome.error or (outcome.stderr.strip()[:300] if outcome.stderr and outcome.returncode else None)
        if outcome.exc_timeout or (outcome.returncode is not None and outcome.returncode < 0):
            error = error or f"timeout ({self.timeout}s)"
        return events, outcome.elapsed_s, error

    @contextlib.contextmanager
    def _effective_cwd(
        self,
        task: Task,
        arm: str,
        diff_capture: list[str],
        test_capture: list[bool | None],
        index_relocations: list[dict[str, str]] | None = None,
    ) -> Iterator[Path]:
        """Yield an isolated sandbox copy of the repo for one run, capturing its aftermath.

        Args:
            task: The task about to run; ``requires_reset`` selects the fix-lane behaviour
                (post-run diff capture and optional targeted test).
            arm: Benchmark arm; the index-consuming arms get the prebuilt cache seeded.
            diff_capture: Accumulator the post-run ``diff -ru`` output is appended to
                (fix-lane tasks only).
            test_capture: Accumulator the targeted-test verdict is appended to when the task
                declares a ``test_target``.
            index_relocations: Optional accumulator for derived index provenance.

        Yields:
            Path of the sandbox repo copy, removed when the block exits.
        """
        # EVERY arm runs in an isolated copy of the repo so agent edits can never mutate
        # self.repo_path. Blocking Edit/Write is not enough: the codemap and combined arms keep
        # Bash, so an agent could still write through the shell — only a throwaway copy bounds
        # the blast radius. Query (non-reset) arms previously ran in-place, letting a stray edit
        # contaminate later runs; they now copy like every other arm.
        import tempfile

        prefix = "bench-fix-" if task.requires_reset else "bench-copy-"
        with tempfile.TemporaryDirectory(prefix=prefix) as tmpdir:
            tmp = Path(tmpdir)
            cwd = tmp / self.repo_path.name
            shutil.copytree(
                self.repo_path,
                cwd,
                ignore=shutil.ignore_patterns(".cache", ".git"),
                symlinks=True,
            )
            # codemap / combined arms need the prebuilt index present in the sandbox;
            # otherwise the /codemap:query-code Step 0 would build it inside the measured
            # window. The sandbox dir keeps the original repo name, so the
            # repo-name-derived index file (<repo>.json) resolves unchanged (git is absent →
            # resolve_proj_index falls back to the CWD basename). Plain and semble arms are
            # left index-free — plain for isolation, semble because it never queries the index.
            if arm in ("codemap", "combined", "B_auto", "C_strict"):
                relocated = self._seed_index_cache(cwd)
                if index_relocations is not None:
                    index_relocations.extend(relocated)
            yield cwd
            if task.requires_reset:
                import subprocess as _sp

                # The excludes mirror the copytree ignore list above. Without them the diff
                # reports every top-level entry the sandbox never received (.git) and every
                # cache tree the sandbox was seeded with selectively (.cache) as a
                # difference — artifact bloat at best, and a spurious `+` line in fix
                # scoring the moment anything writes under .cache during the run.
                proc = _sp.run(
                    [
                        "diff",
                        "-ru",
                        "--no-dereference",
                        "--exclude=.git",
                        "--exclude=.cache",
                        str(self.repo_path),
                        str(cwd),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                diff_capture.append(proc.stdout)
                # Opt-in correctness signal: run the task's declared pytest node on the sandbox
                # (post-edit, pre-cleanup) so a semantically wrong edit that merely emits the
                # right keywords does not score full recall unchecked.
                if task.test_target:
                    test_capture.append(self._run_targeted_test(cwd, task.test_target))

    def run(
        self,
        task: Task,
        arm: str,
        update_fn: Callable[[float, "BenchmarkRun"], None] | None = None,
    ) -> BenchmarkRun:
        """Run one task in one arm and return the parsed metrics.

        Launches a ``claude`` subprocess in stream-json mode and delegates event parsing
        to ``_stream_events``. Retries up to two times when the API returns zero tokens
        (connectivity failure). On the third failure the result is returned as-is.

        Args:
            task: The task to execute.
            arm: Benchmark arm identifier (``plain``, ``codemap``, ``semble``, or
                ``combined``). Controls which tools are allowed/disallowed and which
                system prompt supplement is injected.
            update_fn: Optional live-progress callback invoked (at most every 0.5 s)
                with ``(elapsed_seconds, partial_result)``. Used by the rich Progress bar
                in ``Benchmark.run()``. Pass ``None`` to disable.

        Returns:
            Populated ``BenchmarkRun`` with tool counts, token metrics, timing, and
            raw output text ready for quality scoring.
        """
        system_prompt = self._system_prompt(task.skill or task.type, arm)
        disallow_flags = self._ARM_DISALLOWED.get(arm, [])
        allow_flags = self._ARM_ALLOWED.get(arm, [])
        # Codex has no equivalent public turn cap, so canonical parity arms use only the shared
        # wall-clock budget. Legacy agentic labels keep their original fixed 40-turn control.
        turn_flags = [] if parity_arm_identity(arm) else ["--max-turns", "40"]
        _diff_capture: list[str] = []
        _test_capture: list[bool | None] = []

        _MAX_API_RETRIES = 2
        denied_evidence = _benchmark_evidence_roots()
        for attempt in range(_MAX_API_RETRIES + 1):
            # Every attempt gets its own sandbox. Reusing one copy across retries let a
            # failed attempt's edits — and any file it created — survive into the next
            # one, so a retry no longer started from the task's baseline tree and its
            # captured diff mixed both attempts' work.
            _diff_capture = []
            _test_capture = []
            index_relocations: list[dict[str, str]] = []
            with self._effective_cwd(task, arm, _diff_capture, _test_capture, index_relocations) as cwd:
                # Each benchmark task is an independent agent session. Clear the
                # inject-preamble session-once flag so each task receives the
                # codemap status line regardless of inter-task timing.
                _flag = Path(tempfile.gettempdir()) / f"codemap-preamble-{cwd.name}"
                _flag.unlink(missing_ok=True)

                with _staged_codemap_runtime(arm, cwd) as runtime:
                    runtime_paths = [runtime] if runtime is not None else []
                    with _claude_evidence_settings_file(
                        cwd,
                        evidence_roots=denied_evidence,
                        runtime_paths=runtime_paths,
                    ) as settings_path:
                        cmd = [
                            *self._CMD,
                            *turn_flags,
                            "--model",
                            self.model_id,
                            "--settings",
                            str(settings_path),
                            *self._arm_isolation_flags(arm, codemap_plugin_dir=runtime),
                            *disallow_flags,
                            *allow_flags,
                            "--system-prompt",
                            system_prompt,
                            task.prompt,
                        ]
                        result = BenchmarkRun(
                            arm=arm,
                            task_id=task.id,
                            task_type=task.type,
                            model=self.model_short,
                            success=False,
                            index_relocations=index_relocations,
                        )
                        self._stream_events(cmd, result, update_fn=update_fn, cwd=cwd, arm=arm)
                        evidence = _claude_codemap_evidence(result.raw_events)
                        result.codemap_query_attempted = int(evidence["codemap_query_attempted"])
                        result.codemap_query_succeeded = int(evidence["codemap_query_succeeded"])
                        result.codemap_compact_success = bool(evidence["codemap_compact_success"])
                # 0-token result = API connectivity failure (ConnectionRefused / FailedToOpenSocket);
                # retry up to 2 times before surfacing as error. A wall-clock kill also leaves
                # 0 tokens (the killed process never emits its `result` event) but has already
                # burned a full timeout of paid model work — never retry it.
                timed_out = result.elapsed_s >= self.timeout or (result.error or "").startswith("timeout")
                retrying = (
                    result.input_tokens == 0
                    and result.output_tokens == 0
                    and not timed_out
                    and attempt < _MAX_API_RETRIES
                )
            # The sandbox is torn down before the backoff so a retry never waits with the
            # previous attempt's copy still on disk.
            if retrying:
                result.error = f"api_failure_retry_{attempt + 1}"
                time.sleep(2**attempt)  # exponential backoff: 1s, 2s
                continue
            break
        if _diff_capture:
            result.agent_diff = _diff_capture[0]
        if _test_capture:
            result.targeted_test_passed = _test_capture[0]
        return result

    def _run_targeted_test(self, cwd: Path, test_target: str) -> bool | None:
        """Run a task's declared pytest target on the post-edit sandbox and report pass/fail.

        Args:
            cwd: Sandbox repository root containing the agent's applied edits.
            test_target: pytest node id or path (e.g. ``tests/foo/test_bar.py::test_case``).

        Returns:
            ``True`` when pytest exits 0, ``False`` on any non-zero exit, and ``None`` when pytest
            could not be launched at all (missing binary / environment error) so a launch failure
            is never miscredited as a genuine test failure.
        """
        import subprocess as _sp

        try:
            proc = _sp.run(
                [sys.executable, "-m", "pytest", test_target, "-q", "-p", "no:cacheprovider"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, _sp.SubprocessError):
            return None
        return proc.returncode == 0

    def _seed_index_cache(self, cwd: Path) -> list[dict[str, str]]:
        """Copy prebuilt caches and relocate index roots to the disposable repository.

        Only ``.cache/codemap`` and ``.cache/scan`` are copied from the original repo (never the
        whole ``.cache``), so the structural index is present in the sandbox without dragging in
        unrelated cache trees. Missing source dirs are skipped silently.

        Args:
            cwd: Sandbox repository root the index should be seeded into.

        Returns:
            Original and derived hashes for each root-relocated index. Graph facts remain unchanged.
        """
        relocations: list[dict[str, str]] = []
        for sub in ("codemap", "scan"):
            source = self.repo_path / ".cache" / sub
            if source.is_dir():
                destination = cwd / ".cache" / sub
                shutil.copytree(source, destination, symlinks=True)
                for index_path in sorted(destination.glob("*.json")):
                    original = index_path.read_bytes()
                    payload = json.loads(original)
                    # Auxiliary cache metadata is not an index. Only relocate declared roots;
                    # the existing helper rejects a source-root mismatch without changing facts.
                    if not isinstance(payload, dict) or "scan_root" not in payload:
                        continue
                    derived, provenance = relocate_frozen_index_for_worktree(
                        original, source_root=self.repo_path, worktree_root=cwd
                    )
                    if index_path.is_symlink():
                        index_path.unlink()
                    index_path.write_bytes(derived)
                    relocations.append({**provenance, "index_path": index_path.relative_to(cwd).as_posix()})
        return relocations

    # Semble MCP definition, re-supplied under isolation (excluding user config drops the user's
    # semble server). Mirrors `claude mcp get semble` — a local stdio server, no auth/env.
    _SEMBLE_MCP: dict = {"mcpServers": {"semble": {"command": "uvx", "args": ["--from", "semble[mcp]", "semble"]}}}

    @staticmethod
    def _codemap_plugin_dir() -> str | None:
        """Return the repository Codemap fixture, or None when it is incomplete.

        The benchmark must not inherit a mutable user plugin cache. The checked-out plugin is the
        deterministic fixture used by CI and local runs; a missing fixture is reported to the
        canonical arm instead of being treated as a valid no-plugin setup.

        Returns:
            Absolute path to the plugin root (the dir holding .claude-plugin/), or None.
        """
        plugin_root = REPO_ROOT / "plugins" / "codemap-py"
        required_files = (
            plugin_root / ".claude-plugin" / "plugin.json",
            plugin_root / "claude-skills" / "query-code" / "SKILL.md",
        )
        if all(path.is_file() for path in required_files):
            return str(plugin_root)
        return None

    @classmethod
    def _semble_mcp_config_path(cls) -> str:
        """Write the reconstructed semble MCP config once and return its path.

        Returns:
            Path to a JSON file suitable for ``--mcp-config`` describing the semble stdio server.
        """
        import tempfile

        path = Path(tempfile.gettempdir()) / "codemap-bench-semble-mcp.json"
        if not path.exists():
            path.write_text(json.dumps(cls._SEMBLE_MCP))
        return str(path)

    @classmethod
    def _arm_isolation_flags(cls, arm: str, *, codemap_plugin_dir: Path | None = None) -> list[str]:
        """Return the per-arm flags that re-supply the tools under test after user config is excluded.

        codemap/combined get the codemap plugin (``--plugin-dir``) for the Skill; semble/combined get the
        semble server (``--mcp-config``, ``--strict-mcp-config``). plain gets nothing — it is the control.

        Args:
            arm: Benchmark arm (plain / codemap / semble / combined).

        Returns:
            Flag list to splice into the claude command for *arm*.
        """
        flags: list[str] = []
        plugin_dir = codemap_plugin_dir or cls._codemap_plugin_dir()
        if arm in ("codemap", "combined", "B_auto", "C_strict") and plugin_dir is None:
            raise RuntimeError(f"Codemap plugin fixture is required for {arm} but is unavailable")
        if arm in ("codemap", "combined", "B_auto", "C_strict"):
            flags += ["--plugin-dir", str(plugin_dir)]
        if arm in ("semble", "combined"):
            flags += ["--mcp-config", cls._semble_mcp_config_path(), "--strict-mcp-config"]
        return flags

    @classmethod
    def _subprocess_env(cls, arm: str = "", *, codemap_plugin_dir: Path | None = None) -> dict[str, str]:
        """Return an arm-isolated environment with Codemap exposed only to its treatments.

        Plugin bin/ directories are not reliably added to PATH in ``claude -p`` mode, so the
        codemap ``bin/`` dir is injected explicitly to keep ``scan-query`` reachable inside skill
        Bash calls. For the codemap and combined arms ``SCAN_NO_AUTOBUILD=1`` is set so the
        /codemap-py:query-code Step 0 never runs ``scan-index --incremental`` inside the measured
        window — the benchmark builds the index out of band. A genuinely
        missing index then fails loudly instead of being silently rebuilt mid-task.

        ``CLAUDE_PLUGIN_ROOT`` is also exported for the codemap-consuming arms, pointed at the
        same deterministic fixture passed via ``--plugin-dir``. The ``claude`` CLI does not
        reliably propagate this var into a Skill's Bash execution context in headless ``-p``
        mode; without it, ``query-code/SKILL.md``'s ``${CLAUDE_PLUGIN_ROOT:-plugins/codemap-py}``
        fallback resolves to a relative path that doesn't exist in the copied sandbox repo, and
        the agent burns calls on ``find``/``which``/``printenv`` hunting for the binary instead
        (confirmed: 15/15 Claude C_strict cells hit this, 0/16 Codex — Codex's adapter resolves
        via an explicit ``CODEMAP_BIN`` env var instead of an implicit CLI-propagated one). If the
        CLI does set it correctly for a given invocation, this value is simply overridden per-Skill
        and is a no-op.

        Args:
            arm: Benchmark arm; only ``codemap`` / ``combined`` / ``B_auto`` / ``C_strict``
                receive the build opt-out and CLAUDE_PLUGIN_ROOT — never ``A_plain``/``plain``,
                whose contract is "Codemap is absent and inaccessible"; leaking availability
                there would break treatment isolation.

        Returns:
            A copy of the process environment with PATH (and, for structural arms, the opt-out
            and CLAUDE_PLUGIN_ROOT).
        """
        env = os.environ.copy()
        # The parent-only path list names evidence that controls benchmark scoring;
        # exposing it would itself disclose where an evaluator can look for answers.
        env.pop("BENCHMARK_EVIDENCE_ROOTS", None)
        for key in ("CLAUDE_PLUGIN_ROOT", "CODEMAP_BIN", "CODEMAP_PYTHON", "SCAN_NO_AUTOBUILD"):
            env.pop(key, None)
        if arm in ("codemap", "combined", "B_auto", "C_strict"):
            plugin_dir = codemap_plugin_dir or cls._codemap_plugin_dir()
            if plugin_dir is None:
                raise RuntimeError(f"Codemap plugin fixture is required for {arm} but is unavailable")
            codemap_bin_on_path(env, Path(plugin_dir))
            env["SCAN_NO_AUTOBUILD"] = "1"
            env["CLAUDE_PLUGIN_ROOT"] = str(plugin_dir)
            env["CODEMAP_PYTHON"] = cls._eligible_codemap_python()
        return env

    @staticmethod
    def _eligible_codemap_python() -> str:
        """Return the current external CPython only when it meets the staged launcher's range."""
        candidate = Path(sys.executable).resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise RuntimeError("Codemap treatment requires an executable CPython >=3.11,<3.15")
        probe = subprocess.run(
            [
                str(candidate),
                "-c",
                "import sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15) else 127)",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if probe.returncode != 0:
            raise RuntimeError("Codemap treatment requires CPython >=3.11,<3.15")
        return str(candidate)

    def _stream_events(
        self,
        cmd: list[str],
        result: BenchmarkRun,
        update_fn: Callable[[float, "BenchmarkRun"], None] | None = None,
        cwd: Path | None = None,
        arm: str = "",
    ) -> None:
        """Launch the claude subprocess, enforce wall-clock timeout, and parse stream-json events.

        Reads stdout line-by-line and routes each JSON event to ``_handle_event``.
        Calls ``update_fn(elapsed_s, result)`` at most every 0.5 s while the subprocess
        is running. Kills the subprocess via a ``threading.Timer`` at ``self.timeout``
        seconds and records the error on *result*.

        Args:
            cmd: Full ``claude`` CLI command list, constructed by ``run()``.
            result: Mutable ``BenchmarkRun`` populated in-place as events arrive.
            update_fn: Optional throttled callback; signature
                ``(elapsed_seconds: float, result: BenchmarkRun) -> None``.
                Invoked at most every 0.5 s. Pass ``None`` to disable.
            cwd: Working directory for the subprocess. Defaults to ``self.repo_path``.
                Plain-arm runs pass a symlink-based stripped copy without ``.cache/``.
            arm: Benchmark arm; forwarded to ``_subprocess_env`` so the codemap / combined
                arms receive the ``SCAN_NO_AUTOBUILD`` opt-out.
        """
        pending: dict[str, float] = {}
        pending_codemap_ids: set[str] = set()  # all codemap skill calls (for erec corpus)
        pending_rdeps_ids: set[str] = set()  # codemap rdeps calls specifically (for sc)
        pending_semble_ids: set[str] = set()  # all semble MCP calls (for erec corpus)

        def _on_event(event: dict, ts: float) -> None:
            result.raw_events.append(dict(event))
            self._handle_event(event, result, pending, pending_codemap_ids, pending_rdeps_ids, pending_semble_ids, ts)

        plugin_dir = None
        if "--plugin-dir" in cmd:
            plugin_dir = Path(cmd[cmd.index("--plugin-dir") + 1])

        outcome = stream_claude(
            cmd,
            timeout=self.timeout,
            cwd=cwd if cwd is not None else self.repo_path,
            env=self._subprocess_env(arm, codemap_plugin_dir=plugin_dir),
            on_event=_on_event,
            update_fn=(lambda elapsed: update_fn(elapsed, result)) if update_fn else None,
        )
        # Map mechanics onto the run, preserving this lane's error precedence:
        # stderr (on a non-success run) → timeout → any unexpected exception.
        result.elapsed_s = outcome.elapsed_s
        if not result.success and not result.error and outcome.stderr:
            result.error = outcome.stderr.strip()[:300]
        if outcome.returncode is not None and outcome.returncode < 0 and not result.error:
            result.error = f"timeout ({self.timeout}s)"
        if outcome.exc_timeout:
            result.error = f"timeout ({self.timeout}s)"
        if outcome.error and not result.error:
            result.error = outcome.error

    def _handle_event(
        self,
        event: dict,
        result: BenchmarkRun,
        pending: dict[str, float],
        pending_codemap_ids: set[str],
        pending_rdeps_ids: set[str],
        pending_semble_ids: set[str],
        ts: float,
    ) -> None:
        """Route native events while excluding unstructured diagnostics from tool accounting."""
        etype = event.get("type", "")
        blocks = _claude_message_blocks(event)

        if etype == "assistant":
            event_text_start = len(result.output_text)
            event_has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
            for block in blocks:
                self._on_tool_use(block, result, pending, ts)
                if block.get("type") == "text":
                    result.output_text += block.get("text", "")
                # Track codemap skill calls for erec corpus + skill_coverage
                if block.get("type") == "tool_use" and block.get("name") == "Skill":
                    tool_id = block.get("id", "")
                    skill_str = block.get("input", {}).get("skill", "")
                    args_str = block.get("input", {}).get("args", "")
                    if "codemap" in skill_str:
                        result.tools.codemap += 1
                        pending_codemap_ids.add(tool_id)
                        if "rdeps" in args_str or "rdeps" in skill_str:
                            pending_rdeps_ids.add(tool_id)
                elif block.get("type") == "tool_use" and block.get("name") == "Bash":
                    tool_id = block.get("id", "")
                    cmd = block.get("input", {}).get("command", "")
                    # In claude -p mode the skill sub-model never spawns; capture scan-query rdeps
                    # Bash calls as the equivalent fallback so sc/erec are meaningful.
                    # Match both "scan-query rdeps <m>" and "/path/to/scan-query rdeps <m>".
                    if re.search(r"(?:^|/)scan-query\s+rdeps\s+\S", cmd):
                        pending_codemap_ids.add(tool_id)
                        pending_rdeps_ids.add(tool_id)
                elif block.get("type") == "tool_use" and block.get("name") in (
                    "mcp__semble__search",
                    "mcp__semble__find_related",
                ):
                    tool_id = block.get("id", "")
                    result.tools.semble += 1
                    pending_semble_ids.add(tool_id)
            if not event_has_tool_use and len(result.output_text) > event_text_start:
                result.last_tool_text_offset = event_text_start
        elif etype == "user":
            # Tool results arrive as {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                tool_id = block.get("tool_use_id", "")
                if tool_id in pending:
                    result.tool_elapsed_s += ts - pending.pop(tool_id)
                is_codemap = tool_id in pending_codemap_ids
                is_rdeps = tool_id in pending_rdeps_ids
                is_semble = tool_id in pending_semble_ids
                content_raw = block.get("content", "")
                if is_codemap:
                    pending_codemap_ids.discard(tool_id)
                if is_rdeps:
                    pending_rdeps_ids.discard(tool_id)
                if is_semble:
                    pending_semble_ids.discard(tool_id)
                # Detect blocked/permission-denied results and track separately
                _content_str = (
                    content_raw
                    if isinstance(content_raw, str)
                    else " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content_raw)
                )
                if "<tool_use_error>" in _content_str and (is_semble or is_codemap):
                    result.tools.blocked += 1
                    if not result.error_type:
                        result.error_type = "skill_blocked"
                self._on_tool_result(content_raw, result, is_codemap=is_codemap, is_rdeps=is_rdeps, is_semble=is_semble)
            # Advance report boundary: anything the model writes AFTER this tool_result event is the
            # final answer.  Without this, last_tool_text_offset stays 0 when the model emits no
            # pure-text event after its last tool call (Scenario-2 bug), making rrec corpus = full
            # output_text preamble and producing misleading rrec values.
            if any(b.get("type") == "tool_result" for b in blocks):
                result.last_tool_text_offset = len(result.output_text)
        elif etype == "result":
            u = parse_result_usage(event)
            result.usage_complete = True
            result.cache_creation_tokens = u.cache_creation_tokens
            result.cache_read_tokens = u.cache_read_tokens
            result.input_tokens = u.input_tokens
            result.output_tokens = u.output_tokens
            result.cost_usd = u.cost_usd
            result.success = u.success
            if not result.success:
                result.error_type = u.subtype  # e.g. "error_max_turns", "error_non_zero_exit"

    def _on_tool_use(
        self,
        block: dict,
        result: BenchmarkRun,
        pending: dict[str, float],
        ts: float,
    ) -> None:
        """Update tool counts, log, and timing for one tool_use content block."""
        if block.get("type") != "tool_use":
            return
        name = block.get("name", "")
        tool_id = block.get("id", "")
        inp = block.get("input", {})
        attr = name.lower()
        if hasattr(result.tools, attr):
            setattr(result.tools, attr, getattr(result.tools, attr) + 1)
        result.tool_log.append(f"{name}: {_tool_key_arg(name, inp)}")
        if name in self.EXPLORATION_TOOLS and tool_id:
            pending[tool_id] = ts
        if name == "Bash":
            cmd = inp.get("command", "")
            if _invokes_scan_query(cmd):
                result.tools.scan_query += 1
            # Patterns typical of manual import graph discovery (not file-reading)
            if re.search(r"\b(grep|rg)\b.*\bimport\b|\bgrep\b.*\bfrom\b|\bimport\b.*-r\b", cmd):
                result.tools.bash_for_imports += 1
            # Detect direct reads of the codemap index JSON (isolation violation in plain arm)
            if re.search(r"\.cache/codemap/|\.cache/scan/", cmd):
                result.tools.index_reads += 1

    @staticmethod
    def _on_tool_result(
        content: str | list,
        result: BenchmarkRun,
        is_codemap: bool = False,
        is_rdeps: bool = False,
        is_semble: bool = False,
    ) -> None:
        """Accumulate token count and capture codemap/semble results from a tool result content field.

        Skips content that contains ``<tool_use_error>`` or starts with
        ``"Launching skill:"`` (skill-executor status placeholders).

        Args:
            content: Raw ``content`` field from the ``tool_result`` event — either a plain
                string or a list of content blocks.
            result: The accumulating ``BenchmarkRun`` updated in-place.
            is_codemap: True when this result is from any codemap skill call. Not currently
                used for corpus capture (rdeps calls are distinguished by ``is_rdeps``);
                reserved for future per-call filtering.
            is_rdeps: True when this result is from a ``codemap:query rdeps`` call.
                Appends the text to ``result.codemap_results`` and ``result.skill_result_text``
                (used for exposure-recall corpus and skill-coverage scoring).
            is_semble: True when this result is from a semble MCP tool call
                (``mcp__semble__search`` or ``mcp__semble__find_related``). Appends the
                text to ``result.semble_results`` for the exposure-recall corpus.
        """
        for text in _iter_tool_result_texts(content):
            _capture_tool_result_text(text, result, is_rdeps=is_rdeps, is_semble=is_semble)
