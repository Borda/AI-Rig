#!/usr/bin/env python3
"""Codemap skill benchmark — agent exploration cost with vs without structural context.

## What this measures

Two arms run the same import-graph navigation tasks:

  plain    — developer with a minimal fix/feature/refactor/review skill; discovers structure via
             Grep / Glob / Bash
  codemap  — same skill extended with /codemap:query; uses the Skill tool for structural lookups
             instead of grepping

Core claim under test: one /codemap:query call replaces many Grep passes, reducing tool call count,
elapsed time, and context consumption.

## What is NOT measured (excluded by design)

  scan-index  — builds the codemap import-graph index from the repo's Python sources. This is a
                one-time setup step that runs before the benchmark. Its cost (typically a few
                seconds) is intentionally excluded: it amortises over every subsequent query a
                developer makes and is not part of the per-task exploration loop.

  See: plugins/codemap/bin/scan-index --root <repo>

## Metrics (per task × arm × model)

  Key metrics — headline savings signal:
  elapsed_s          — total wall-clock time for the run
  input_tokens (k)   — cumulative input tokens (system prompt + turns + results)

  Diagnostic metrics — explain how savings were achieved:
  tool_calls         — Grep / Glob / Bash / Skill invocations in the transcript
  tool_result_tokens (k) — tiktoken estimate of tokens in tool result content
  tool_elapsed_s     — wall-clock time inside tool execution (excludes LLM think)

## Savings formula (same as caveman evals)

  savings = 1 − (codemap_metric / plain_metric)   per task
  Reported as median / mean / min / max across tasks, per model tier.

## Quality scoring — exposure recall / report recall / skill coverage

  Purpose: assess whether the agent correctly identified the modules that import the task's primary_module
  (its "reverse dependencies", or rdeps). This is a proxy for blast-radius awareness — the core skill under test.

  Ground truth (deterministic, index-derived):
    expected = { m.name for m in index.modules
                 if primary_module in m.direct_imports }
    Reproducible across runs as long as the index does not change.

  Matching strategy — multi-form surface matching (v2):
    For each expected rdep, generate surface forms with 2+ path components:
      full dotted:   lightning.pytorch.trainer.trainer
      file path:     lightning/pytorch/trainer/trainer.py, src/lightning/pytorch/trainer/trainer.py
      2-suffix:      trainer.trainer, trainer/trainer, trainer/trainer.py
      3-suffix:      pytorch.trainer.trainer
    Bare leaf names (e.g., "trainer") are NEVER matched — minimum 2 components avoids false positives.
    All forms are word-boundary-aware and case-insensitive.

  Two-layer metrics:
    erec (exposure recall) — what the agent had access to:
      corpus = output_text + codemap skill result text (no grep/glob results)
      erec = |{r in expected : any form matches in corpus}| / |expected|
      Levels the playing field: codemap arm gets credit for its structured answer;
      plain arm does NOT get inflated by grep result echoes.

    rrec (report recall) — what the agent told the user:
      corpus = output_text after the last tool_use/tool_result event
      rrec = |{r in expected : any form matches in corpus}| / |expected|
      Both arms measured equally on their final answer.

    delta = erec - rrec — information gap (agent saw it but did not report it)
    deff = erec_tp / max(tool_calls, 1) — discovery efficiency (rdeps found per tool call)

    sc (skill coverage, codemap only) — index completeness:
      Parsed from the codemap:query rdeps skill result; measures whether the index contained the answer.

  Interpretation guidance:
    — erec high, rrec low → agent found the rdeps but answered in prose without repeating them
    — erec high for codemap, low for plain → codemap skill provided structural context the plain arm missed
    — sc = 100% + erec = 100% → index is complete AND agent processed the result
    — delta ≈ 0 → agent reported everything it found
    — deff higher for codemap → fewer tool calls needed for the same coverage

## Quick start

  # 1. Build the index once (excluded from benchmark timing)
  python plugins/codemap/bin/scan-index --root /path/to/repo

  # 2. Run all tasks across all model tiers
  python benchmarks/run-codemap-agentic.py --repo-path /path/to/repo --all --report

  # 3. Spot-check one task in plain arm only
  python benchmarks/run-codemap-agentic.py --repo-path /path/to/repo \\
      --tasks T01 --arm plain --model haiku

## Requirements

  - claude CLI on PATH (uses Claude Code subscription — no API key)
  - pip install -r benchmarks/requirements.txt  (tiktoken pandas tabulate rich tqdm)
  - Pre-built codemap index (see step 1 above)

## Failure conditions

  A run is marked success=False when any of these occur:
    timeout          — claude subprocess exceeded the 300 s wall-clock limit
    non-zero exit    — claude returned a non-success subtype in the result event; stderr is captured as error
    codemap no-call  — codemap arm completed without ever invoking the Skill tool; this means the agent fell
                       back to grep/bash entirely, defeating the purpose of the codemap arm

## Terminal output (one line per completed run)

  Each run prints a coloured summary line to stdout via tqdm.write:
    [NN/TT] TASK_ID (type) | model  | arm       | elapsed=  NNN.Ns | tokens= NNN.Nk |
    calls= N (grep= N; glob= N; bash= N; skill= N)
    | erec= N% rrec= N%  sc= N%   ← quality=n/a when no ground truth
  Quality fields:
    erec  — exposure recall: rdeps found in output_text + codemap skill results (multi-form, 2+ components)
    rrec  — report recall: rdeps found in final answer text after last tool call
    sc    — skill coverage (codemap arm only): fraction of expected rdeps returned by the skill call;
             omitted on plain arm; measures index completeness, not agent verbosity
  Colour coding:
    yellow  — plain arm
    cyan    — codemap arm
    red     — any arm where success=False (overrides arm colour)

## JSON output schema (benchmarks/results/code-YYYY-MM-DD.json)

  Written after every run (rolling snapshot) so partial results survive interruptions.

  {
    "metadata": {
      "date": "ISO-8601 timestamp",
      "models": "haiku, sonnet, opus",
      "repo": "/abs/path/to/repo",
      "index": "/abs/path/to/index.json",
      "task_count": N
    },
    "results": [
      {
        "arm": "plain" | "codemap",
        "task_id": "T01",
        "task_type": "fix" | "feature" | "refactor" | "review",
        "model": "haiku" | "sonnet" | "opus",
        "success": true | false,
        "tools": {"grep": N, "glob": N, "bash": N, "skill": N},
        "input_tokens": N,          ← sum of input + cache_creation + cache_read tokens
        "output_tokens": N,
        "tool_result_tokens": N,    ← tiktoken estimate of tool result content
        "elapsed_s": N.N,
        "tool_elapsed_s": N.N,      ← wall-clock inside tool execution only
        "error": "",                ← non-empty on failure
        "tool_log": ["Grep: pattern in path", ...],
        "output_text": "...",       ← full agent text output (used for quality scoring)
        "quality": {
          "scored": true | false,       ← false when task has no primary_module in index
          "erec": N.N,                  ← exposure recall: rdeps found in output_text + codemap results
          "erec_tp": N, "erec_fn": N,   ← multi-form true positives / false negatives on exposure corpus
          "rrec": N.N,                  ← report recall: rdeps found in final answer text
          "rrec_tp": N, "rrec_fn": N,   ← multi-form true positives / false negatives on report corpus
          "delta": N.N,                 ← erec - rrec: information seen but not reported
          "deff": N.N,                  ← erec_tp / max(tool_calls, 1): discovery efficiency
          "skill_coverage": N.N | null, ← codemap arm: fraction of expected rdeps in skill result; null for plain
          "skill_returned": N | null,   ← count of modules the skill call returned; null for plain
          "leaf_recall": N.N,           ← legacy: leaf-name recall on output_text
          "recall": N.N, "precision": N.N, "f1": N.N,  ← legacy aliases
          "tp": N, "fp": N, "fn": N, "leaf_tp": N, "leaf_fn": N, "ambiguous_leaves": N
        }
      }
    ]
  }

## Stream-JSON event parsing

  The benchmark invokes:
      claude -p --verbose --output-format stream-json --system-prompt "..." "task prompt"

  Events parsed:
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Grep",...}],...}}
      → increments tool counter; records tool_use_id + timestamp for elapsed tracking
      → text blocks are concatenated into output_text for quality scoring

    {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"...","content":"..."}]}}
      → records elapsed since matching tool_use; tokenises result content with tiktoken

    {"type":"result","usage":{"input_tokens":N,"output_tokens":N,...}}
      → captures final cumulative token usage (all cache partitions summed)
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from tqdm.auto import tqdm

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESULTS_DIR = Path("benchmarks/results")

# Model tiers: short name → full model ID (ascending capability / cost)
MODELS: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class QualityScore:
    """Quality score for a single benchmark run.

    Primary metrics (v2 — multi-form matching with 2+ component surface forms):
        ``erec``  — exposure recall: rdeps found in output_text + codemap skill results
        ``rrec``  — report recall: rdeps found in agent's final answer after last tool call
        ``delta`` — erec - rrec: information gap (agent saw but did not report)
        ``deff``  — discovery efficiency: erec_tp / max(tool_calls, 1)

    Supplementary (codemap arm only):
        ``skill_coverage``, ``skill_returned``

    Legacy fields (``leaf_recall``, ``precision``, ``recall``, ``f1``, ``tp``, ``fp``, ``fn``)
    are retained for backward compatibility; computed via leaf-name matching on output_text.
    """

    scored: bool = False  # False when no ground truth is available

    # ── Primary metrics (v2 — multi-form matching) ──
    erec: float = 0.0  # exposure recall: rdeps found in output_text + codemap results
    erec_tp: int = 0
    erec_fn: int = 0
    rrec: float = 0.0  # report recall: rdeps found in final answer text (after last tool call)
    rrec_tp: int = 0
    rrec_fn: int = 0
    delta: float = 0.0  # erec - rrec: information the agent saw but did not report
    deff: float = 0.0  # discovery efficiency: erec_tp / max(tool_calls, 1)

    # ── Skill result coverage (codemap arm only; None when not applicable) ──
    skill_coverage: Optional[float] = None
    skill_returned: Optional[int] = None

    # ── Legacy fields (backward compat — leaf-name matching on output_text) ──
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    leaf_recall: float = 0.0
    leaf_tp: int = 0
    leaf_fn: int = 0
    ambiguous_leaves: int = 0


@dataclass
class ToolCounts:
    grep: int = 0
    glob: int = 0
    bash: int = 0
    skill: int = 0  # /codemap:query and other skill invocations via the Skill tool

    @property
    def total(self) -> int:
        return self.grep + self.glob + self.bash + self.skill


@dataclass
class BenchmarkRun:
    """Result of a single benchmark run (one task x arm x model).

    Renamed from RunResult; field names are unchanged, so ``asdict()`` output and serialised JSON
    remain identical.
    """

    arm: str
    task_id: str
    task_type: str
    model: str  # short tier name: haiku / sonnet / opus
    success: bool
    tools: ToolCounts = field(default_factory=ToolCounts)
    # Token metrics
    input_tokens: int = 0
    output_tokens: int = 0
    tool_result_tokens: int = 0  # tiktoken estimate of tool result content
    # Timing metrics (stored in seconds)
    elapsed_s: float = 0.0
    tool_elapsed_s: float = 0.0  # time inside tool execution only
    error: str = ""
    # Per-call log for post-run investigation: ["Bash: grep -r 'import'", "Skill: /codemap:query rdeps ..."]
    tool_log: list[str] = field(default_factory=list)
    # Full agent output text — captured for quality scoring
    output_text: str = ""
    quality: QualityScore = field(default_factory=QualityScore)
    # Internal fields excluded from JSON serialisation (see _save_snapshot)
    skill_result_text: str = field(default="", repr=False)  # first codemap:query rdeps result (for sc)
    codemap_results: list[str] = field(default_factory=list, repr=False)  # ALL codemap skill results (for erec)
    last_tool_text_offset: int = field(default=0, repr=False)  # output_text offset after last tool event


@dataclass
class Task:
    id: str
    type: str
    prompt: str
    primary_module: str = ""


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def count_tokens(text: str) -> int:
    """Approximate token count using tiktoken o200k_base (matches caveman evals)."""
    try:
        import tiktoken

        enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text))
    except ImportError:
        return max(1, len(text) // 4)  # ~4 chars/token fallback


def find_index(repo_path: Path, explicit: Optional[Path]) -> Path:
    """Locate the pre-built codemap index for the target repo.

    The index is built once by scan-index and excluded from benchmark timing. This function only
    validates it exists before the run starts.
    """
    if explicit:
        return explicit.resolve()
    scan_dir = repo_path / ".cache" / "scan"
    preferred = scan_dir / f"{repo_path.name}.json"
    if preferred.exists():
        return preferred.resolve()
    candidates = sorted(scan_dir.glob("*.json"))
    if candidates:
        return candidates[0].resolve()
    raise FileNotFoundError(
        f"No codemap index found in {scan_dir}.\n"
        f"Build it first (one-time, not measured):\n"
        f"  python plugins/codemap/bin/scan-index --root {repo_path}"
    )


def _unique_path(path: Path) -> Path:
    """Return path unchanged if it doesn't exist; otherwise append a counter suffix."""
    if not path.exists():
        return path
    n = 2
    while (path.parent / f"{path.stem}-{n}{path.suffix}").exists():
        n += 1
    return path.parent / f"{path.stem}-{n}{path.suffix}"


def _tool_key_arg(name: str, inp: dict) -> str:
    """Return a short human-readable argument string for tool call logging."""
    if name == "Grep":
        pat = inp.get("pattern", "")
        loc = inp.get("path", "") or inp.get("glob", "")
        return f"{pat!r} in {loc}" if loc else repr(pat)
    if name == "Glob":
        return inp.get("pattern", "")
    if name == "Bash":
        return inp.get("command", "")[:120]
    if name == "Skill":
        return f"{inp.get('skill', '')} {inp.get('args', '')}".strip()
    return str(inp)[:80]


# ---------------------------------------------------------------------------
# Quality scoring — deterministic, index-derived ground truth
# ---------------------------------------------------------------------------


class GroundTruth:
    """Index-derived ground truth for quality scoring benchmark runs.

    Loads the codemap index once, pre-computes expected rdep sets per task, and exposes a
    ``score()`` method for comparing agent output against truth.
    """

    _MODULE_RE = re.compile(r"\blightning(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+")
    _PATH_RE = re.compile(r"\bsrc/(lightning(?:/[a-zA-Z_][a-zA-Z0-9_]*)+)\.py\b")

    def __init__(self, index_path: Path, tasks: list[Task]) -> None:
        with index_path.open() as f:
            index = json.load(f)
        self.all_modules: set[str] = {m["name"] for m in index.get("modules", []) if m.get("status") == "ok"}
        self.expected: dict[str, set[str]] = {}
        for task in tasks:
            pm = getattr(task, "primary_module", "")
            if not pm:
                continue
            rdeps = {
                m["name"]
                for m in index.get("modules", [])
                if pm in m.get("direct_imports", []) and m.get("status") == "ok"
            }
            self.expected[task.id] = rdeps
        self.all_leaf_names: set[str] = {m.split(".")[-1] for m in self.all_modules}
        # Precompute multi-form match patterns for every module in the index
        self._match_patterns: dict[str, list[re.Pattern]] = {m: self._generate_match_set(m) for m in self.all_modules}

    @staticmethod
    def _generate_match_set(module: str) -> list[re.Pattern]:
        """Generate multi-form regex patterns for a module name.

        Each pattern requires at least 2 path components to avoid bare leaf-name false positives.
        Forms generated: full dotted path, file path variants, 2-component and 3-component suffixes.
        """
        parts = module.split(".")
        forms: set[str] = set()
        # Full dotted path: lightning.pytorch.trainer.trainer
        forms.add(module)
        # File path forms: lightning/pytorch/trainer/trainer.py, src/...
        file_path = module.replace(".", "/") + ".py"
        forms.add(file_path)
        forms.add("src/" + file_path)
        # 2-component suffix (minimum specificity): trainer.trainer, trainer/trainer
        if len(parts) >= 2:
            s2 = ".".join(parts[-2:])
            forms.add(s2)
            forms.add("/".join(parts[-2:]))
            forms.add("/".join(parts[-2:]) + ".py")
        # 3-component suffix: pytorch.trainer.trainer
        if len(parts) >= 3:
            forms.add(".".join(parts[-3:]))
        return [re.compile(r"\b" + re.escape(f) + r"\b", re.IGNORECASE) for f in forms]

    def _rdep_found(self, rdep: str, corpus: str) -> bool:
        """Return True if any multi-form pattern for ``rdep`` matches in ``corpus``."""
        for pat in self._match_patterns.get(rdep, []):
            if pat.search(corpus):
                return True
        return False

    def score(
        self,
        task_id: str,
        output_text: str,
        exposure_corpus: str,
        report_corpus: str,
        tool_calls: int = 0,
        skill_result_text: str | None = None,
    ) -> QualityScore:
        """Compute quality score using multi-form matching and optional skill coverage.

        Primary metrics (v2):
            ``erec`` — exposure recall on ``exposure_corpus`` (output_text + codemap results)
            ``rrec`` — report recall on ``report_corpus`` (final answer after last tool call)
            ``delta`` — erec - rrec
            ``deff`` — erec_tp / max(tool_calls, 1)

        Supplementary:
            ``skill_coverage`` — fraction of expected rdeps in the skill result (codemap only)

        Legacy:
            ``leaf_recall`` etc. — leaf-name matching on ``output_text`` for backward compat
        """
        exp = self.expected.get(task_id, set())
        if not exp:
            return QualityScore(scored=False)

        # ── Primary: multi-form matching (v2) ──
        erec_matched = {r for r in exp if self._rdep_found(r, exposure_corpus)}
        rrec_matched = {r for r in exp if self._rdep_found(r, report_corpus)}
        n_exp = len(exp)
        erec_tp = len(erec_matched)
        rrec_tp = len(rrec_matched)
        erec = erec_tp / n_exp
        rrec = rrec_tp / n_exp
        delta = erec - rrec
        deff = erec_tp / max(tool_calls, 1)

        # ── Skill coverage (codemap arm only) ──
        skill_coverage: Optional[float] = None
        skill_returned: Optional[int] = None
        if skill_result_text:
            try:
                data = json.loads(skill_result_text)
                returned = set(data.get("imported_by", []))
            except (json.JSONDecodeError, AttributeError):
                returned = set(self._MODULE_RE.findall(skill_result_text))
            skill_returned = len(returned)
            skill_coverage = len(returned & exp) / n_exp

        # ── Legacy: leaf-name matching on output_text ──
        expected_leaves = {m.split(".")[-1] for m in exp}
        ambiguous = sum(1 for leaf in expected_leaves if len(leaf) < 6)
        matched_leaves = {
            lf for lf in expected_leaves if re.search(r"\b" + re.escape(lf) + r"\b", output_text, re.IGNORECASE)
        }
        leaf_tp = len(matched_leaves)
        leaf_fn = len(expected_leaves) - leaf_tp
        leaf_recall = leaf_tp / len(expected_leaves) if expected_leaves else 0.0
        all_output_leaves = {
            lf for lf in self.all_leaf_names if re.search(r"\b" + re.escape(lf) + r"\b", output_text, re.IGNORECASE)
        }
        leaf_fp = len(all_output_leaves - expected_leaves)
        prec = leaf_tp / (leaf_tp + leaf_fp) if (leaf_tp + leaf_fp) > 0 else 0.0
        f1 = 2 * prec * leaf_recall / (prec + leaf_recall) if (prec + leaf_recall) > 0 else 0.0

        return QualityScore(
            scored=True,
            # v2 primary
            erec=erec,
            erec_tp=erec_tp,
            erec_fn=n_exp - erec_tp,
            rrec=rrec,
            rrec_tp=rrec_tp,
            rrec_fn=n_exp - rrec_tp,
            delta=delta,
            deff=deff,
            # Skill coverage
            skill_coverage=skill_coverage,
            skill_returned=skill_returned,
            # Legacy
            precision=prec,
            recall=leaf_recall,
            f1=f1,
            tp=leaf_tp,
            fp=leaf_fp,
            fn=leaf_fn,
            leaf_recall=leaf_recall,
            leaf_tp=leaf_tp,
            leaf_fn=leaf_fn,
            ambiguous_leaves=ambiguous,
        )

    @classmethod
    def _extract_modules(cls, text: str) -> set[str]:
        """Extract dotted lightning.* module names from agent output.

        Handles two forms agents use:
        - Dotted: ``lightning.pytorch.trainer.trainer``
        - File path: ``src/lightning/pytorch/trainer/trainer.py`` -> converted to dotted
        """
        dotted = set(cls._MODULE_RE.findall(text))
        from_paths = {m.replace("/", ".") for m in cls._PATH_RE.findall(text)}
        return dotted | from_paths


# ---------------------------------------------------------------------------
# Claude CLI runner
# ---------------------------------------------------------------------------


class ModelRunner:
    """Runs benchmark tasks against a specific Claude model tier.

    Encapsulates model identity, repo path, and timeout. The ``run()`` method launches a Claude
    subprocess and parses stream-json events into a ``BenchmarkRun`` result.

    Arm prompts and CLI constants live here — only ModelRunner constructs or launches claude;
    nothing else needs them.
    """

    # Base claude CLI invocation
    _CMD = ["claude", "-p", "--verbose", "--output-format", "stream-json", "--max-turns", "25"]
    # Tools counted as exploration overhead
    EXPLORATION_TOOLS = {"Grep", "Glob", "Bash", "Skill"}

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
    _PLAIN_SUPPLEMENT = """

## Structural navigation with native tools

Use Grep and Glob for all import graph questions — avoid reading full files
unless the import block alone is not enough:

  What imports X?   Grep the dotted module name across **/*.py
  What does X import?   Read only the import block at the top of X's file
  Blast radius ranking:   Count Grep matches per module; more matches = wider blast
  Import path A → B:   Follow deps of A and rdeps of B until they intersect"""

    _CODEMAP_SUPPLEMENT = """

## codemap plugin installed

You have the /codemap:query skill available. It answers import-graph questions
instantly from a pre-built index — one call replaces many grep passes.

**SYNTAX — colon separator, never a space:**
  Skill tool name: codemap:query      ← correct
  NOT: codemap query                  ← wrong — will fail silently

Commands:
  /codemap:query rdeps <module>    — what imports this module?
  /codemap:query deps <module>     — what does this module import?
  /codemap:query central --top 10  — most-imported modules (blast radius ranking)
  /codemap:query coupled --top 10  — most-coupled modules
  /codemap:query path <from> <to>  — shortest import path between two modules

The result includes an "exhaustive": true field — the index is complete, no
grep verification pass is needed or useful.

**Hard rules — no exceptions:**
1. NEVER use Grep, Glob, or Bash to investigate import relationships, including
   running grep/rg/find via Bash shell commands.
2. NEVER spawn sub-agents (no Agent tool calls) for import-graph questions.
   Sub-agents do not have this skill — call /codemap:query directly yourself.

Grep/Glob/Bash are permitted only for reading source code (finding a literal string)."""

    @classmethod
    def _system_prompt(cls, task_type: str, arm: str) -> str:
        """Build the system prompt for one arm × task-type combination."""
        base = cls._PLAIN_SKILLS.get(task_type, cls._PLAIN_SKILLS["fix"])
        supplement = cls._CODEMAP_SUPPLEMENT if arm == "codemap" else cls._PLAIN_SUPPLEMENT
        return base + supplement

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

    def run(self, task: Task, arm: str) -> BenchmarkRun:
        """Run one task in one arm; parse stream-json for tool + token metrics."""
        system_prompt = self._system_prompt(task.type, arm)
        result = BenchmarkRun(arm=arm, task_id=task.id, task_type=task.type, model=self.model_short, success=False)
        cmd = [*self._CMD, "--model", self.model_id, "--system-prompt", system_prompt, task.prompt]
        self._stream_events(cmd, result)
        return result

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        """Return os.environ augmented with the codemap plugin bin directory on PATH.

        Plugin bin/ directories are not reliably added to PATH in ``claude -p`` mode,
        so we inject it explicitly here. This ensures ``scan-query`` is always reachable
        inside skill Bash calls regardless of how the shell or Claude Code manage PATH.
        """
        env = os.environ.copy()
        plugin_cache = Path.home() / ".claude" / "plugins" / "cache" / "borda-ai-home" / "codemap"
        bin_dirs = sorted(plugin_cache.glob("*/bin"), reverse=True)  # latest version first
        if bin_dirs:
            env["PATH"] = str(bin_dirs[0]) + os.pathsep + env.get("PATH", "")
        return env

    def _stream_events(self, cmd: list[str], result: BenchmarkRun) -> None:
        """Launch claude, enforce wall-clock timeout, and parse stream-json into *result*."""
        pending: dict[str, float] = {}
        pending_codemap_ids: set[str] = set()  # all codemap skill calls (for erec corpus)
        pending_rdeps_ids: set[str] = set()  # codemap rdeps calls specifically (for sc)
        t_start = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.repo_path),
                env=self._subprocess_env(),
            )
            kill_timer = threading.Timer(self.timeout, proc.kill)
            kill_timer.start()
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    ts = time.monotonic()
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._handle_event(event, result, pending, pending_codemap_ids, pending_rdeps_ids, ts)
                stderr_out = proc.stderr.read() if proc.stderr else ""
                proc.wait(timeout=10)
                if not result.success and not result.error and stderr_out:
                    result.error = stderr_out.strip()[:300]
            finally:
                kill_timer.cancel()
            if proc.returncode and proc.returncode < 0 and not result.error:
                result.error = f"timeout ({self.timeout}s)"
        except subprocess.TimeoutExpired:
            proc.kill()
            result.error = f"timeout ({self.timeout}s)"
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)[:300]
        finally:
            result.elapsed_s = time.monotonic() - t_start

    def _handle_event(
        self,
        event: dict,
        result: BenchmarkRun,
        pending: dict[str, float],
        pending_codemap_ids: set[str],
        pending_rdeps_ids: set[str],
        ts: float,
    ) -> None:
        """Route a parsed stream-json event to the appropriate handler."""
        etype = event.get("type", "")

        if etype == "assistant":
            has_tool_use = False
            for block in event.get("message", {}).get("content", []):
                self._on_tool_use(block, result, pending, ts)
                if block.get("type") == "text":
                    result.output_text += block.get("text", "")
                # Track codemap skill calls for erec corpus + skill_coverage
                if block.get("type") == "tool_use" and block.get("name") == "Skill":
                    has_tool_use = True
                    tool_id = block.get("id", "")
                    skill_str = block.get("input", {}).get("skill", "")
                    args_str = block.get("input", {}).get("args", "")
                    if "codemap" in skill_str:
                        pending_codemap_ids.add(tool_id)
                        if "rdeps" in args_str or "rdeps" in skill_str:
                            pending_rdeps_ids.add(tool_id)
                elif block.get("type") == "tool_use" and block.get("name") == "Bash":
                    has_tool_use = True
                    tool_id = block.get("id", "")
                    cmd = block.get("input", {}).get("command", "")
                    # In claude -p mode the skill sub-model never spawns; capture scan-query rdeps
                    # Bash calls as the equivalent fallback so sc/erec are meaningful.
                    # Match both "scan-query rdeps <m>" and "/path/to/scan-query rdeps <m>".
                    if re.search(r"(?:^|/)scan-query\s+rdeps\s+\S", cmd):
                        pending_codemap_ids.add(tool_id)
                        pending_rdeps_ids.add(tool_id)
                elif block.get("type") == "tool_use":
                    has_tool_use = True
            if has_tool_use:
                result.last_tool_text_offset = len(result.output_text)
        elif etype == "user":
            # Tool results arrive as {"type":"user","message":{"content":[{"type":"tool_result",...}]}}
            has_tool_result = False
            for block in event.get("message", {}).get("content", []):
                if block.get("type") != "tool_result":
                    continue
                has_tool_result = True
                tool_id = block.get("tool_use_id", "")
                if tool_id in pending:
                    result.tool_elapsed_s += ts - pending.pop(tool_id)
                is_codemap = tool_id in pending_codemap_ids
                is_rdeps = tool_id in pending_rdeps_ids
                content_raw = block.get("content", "")
                if is_codemap:
                    pending_codemap_ids.discard(tool_id)
                if is_rdeps:
                    pending_rdeps_ids.discard(tool_id)
                self._on_tool_result(content_raw, result, is_codemap=is_codemap, is_rdeps=is_rdeps)
            if has_tool_result:
                result.last_tool_text_offset = len(result.output_text)
        elif etype == "result":
            usage = event.get("usage", {})
            # input_tokens is only the uncached portion; sum all parts for real context usage
            result.input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            result.output_tokens = usage.get("output_tokens", 0)
            result.success = event.get("subtype") == "success"

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

    @staticmethod
    def _on_tool_result(
        content: str | list, result: BenchmarkRun, is_codemap: bool = False, is_rdeps: bool = False
    ) -> None:
        """Accumulate token count and capture codemap results from a tool result content field.

        Args:
            content: Raw content from the tool_result event.
            result: The accumulating BenchmarkRun.
            is_codemap: True when this result is from any codemap skill call (appended to codemap_results for erec).
            is_rdeps: True when this result is from a codemap:query rdeps call (saved to skill_result_text for sc).
        """

        def _capture(text: str) -> None:
            result.tool_result_tokens += count_tokens(text)
            # Skip error responses and skill executor status placeholders
            if "<tool_use_error>" in text or text.startswith("Launching skill:"):
                return
            if is_codemap:
                result.codemap_results.append(text)
            if is_rdeps and not result.skill_result_text:
                result.skill_result_text = text

        if isinstance(content, str):
            _capture(content)
        elif isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    text = c.get("text") or c.get("content") or ""
                    if isinstance(text, str):
                        _capture(text)
                elif isinstance(c, str):
                    _capture(c)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _median_metrics(rlist: list[BenchmarkRun]) -> dict[str, float]:
    ok = [r for r in rlist if r.success]
    if not ok:
        return {}
    return {
        "tool_calls": statistics.median([r.tools.total for r in ok]),
        "input_tokens": statistics.median([r.input_tokens for r in ok]),
        "tool_result_tokens": statistics.median([r.tool_result_tokens for r in ok]),
        "tool_elapsed_s": statistics.median([r.tool_elapsed_s for r in ok]),
        "elapsed_s": statistics.median([r.elapsed_s for r in ok]),
    }


def aggregate(
    results: list[BenchmarkRun],
    task_ids: list[str],
    model_short: str | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Return {task_id: {arm: {metric: value}}} optionally filtered to one model tier."""
    filtered = [r for r in results if model_short is None or r.model == model_short]
    by_task_arm: dict[str, dict[str, list[BenchmarkRun]]] = defaultdict(lambda: defaultdict(list))
    for r in filtered:
        by_task_arm[r.task_id][r.arm].append(r)

    out: dict[str, dict[str, dict[str, float]]] = {}
    for tid in task_ids:
        out[tid] = {}
        for arm, rlist in by_task_arm.get(tid, {}).items():
            out[tid][arm] = _median_metrics(rlist)
    return out


# ---------------------------------------------------------------------------
# Report renderer
# ---------------------------------------------------------------------------


class Report:
    """Renders a markdown benchmark report from a set of completed runs.

    Encapsulates the savings summary, per-task tables, and report assembly logic. All
    report-specific constants are class-level attributes.
    """

    _BASELINE = "plain"
    _INJECTED = "codemap"
    _NO_PAIRS_MD = "_(no completed plain + codemap pairs)_"

    # Limitations appended verbatim to every report
    _LIMITATIONS_MD = [
        "## Limitations",
        "",
        "- Purely quantitative — answer quality / correctness is not scored",
        "- Tool time tracks wall-clock including I/O; LLM think time is not isolated",
        "- tiktoken o200k_base approximates Claude's tokeniser (not exact)",
        "- Results vary across runs; model tier is the primary variance axis here",
        "- Tested on pytorch-lightning; generalisation to other corpora not assessed",
        "",
    ]

    @staticmethod
    def _fmt_tokens(v: float) -> str:
        return f"{v / 1000:.1f}k"

    @staticmethod
    def _fmt_s(v: float) -> str:
        return f"{v:.1f}s"

    @staticmethod
    def _fmt_int(v: float) -> str:
        return f"{v:.0f}"

    # Key metrics first — these are the headline savings signal.
    # Diagnostic metrics follow (tool breakdown, tool-only time).
    _METRICS = [
        ("elapsed_s", "Elapsed (s)", _fmt_s),
        ("input_tokens", "Input tokens (k)", _fmt_tokens),
        ("tool_calls", "Tool calls", _fmt_int),
        ("tool_result_tokens", "Tool result tokens (k)", _fmt_tokens),
        ("tool_elapsed_s", "Tool time (s)", _fmt_s),
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
        models_label = ", ".join(self.model_tiers) if self.model_tiers else self.metadata.get("models", "n/a")

        lines = [
            f"# Codemap Skill Benchmark Report — {self.metadata.get('date', 'n/a')}",
            "",
            f"**Models**: {models_label}  ",
            f"**Repo**: {self.metadata.get('repo', 'n/a')}  ",
            f"**Index**: {self.metadata.get('index', 'n/a')}  ",
            f"**Tasks**: {len(self.task_ids)}  ",
            "",
            "> Savings = 1 − (codemap / plain) per task; positive = codemap needs less.",
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

        lines += self._LIMITATIONS_MD

        return "\n".join(lines)

    def _savings_summary(self, agg: dict) -> list[dict]:
        """Build savings rows for one model's aggregated results."""
        baseline, injected = self._BASELINE, self._INJECTED
        rows = []
        for key, label, _ in self._METRICS:
            savings_per_task = []
            for tid in self.task_ids:
                bv = agg.get(tid, {}).get(baseline, {}).get(key)
                iv = agg.get(tid, {}).get(injected, {}).get(key)
                if bv and iv and bv > 0:
                    savings_per_task.append(1.0 - iv / bv)
            if not savings_per_task:
                continue
            rows.append(
                {
                    "Metric": label,
                    "Median savings": f"{statistics.median(savings_per_task):.0%}",
                    "Mean savings": f"{statistics.mean(savings_per_task):.0%}",
                    "Min savings": f"{min(savings_per_task):.0%}",
                    "Max savings": f"{max(savings_per_task):.0%}",
                }
            )
        return rows

    def _per_task_tables(self, agg: dict) -> list[str]:
        """Return markdown lines for per-task metric tables."""

        baseline, injected = self._BASELINE, self._INJECTED
        lines: list[str] = []
        for key, label, fmt in self._METRICS:
            rows = []
            for tid in self.task_ids:
                t = self.task_meta.get(tid)
                bv = agg.get(tid, {}).get(baseline, {}).get(key)
                iv = agg.get(tid, {}).get(injected, {}).get(key)
                have_pair = bv is not None and iv is not None and bv > 0
                saved = f"{1.0 - iv / bv:.0%}" if have_pair else "—"
                arrow = ("↓" if iv < bv else "↑") if have_pair else ""
                rows.append(
                    {
                        "Task": tid,
                        "Type": t.type if t else "?",
                        "Plain": fmt(bv) if bv is not None else "—",
                        "Codemap": fmt(iv) if iv is not None else "—",
                        "Savings": f"{saved} {arrow}".strip(),
                    }
                )
            lines += [f"### {label}", "", pd.DataFrame(rows).to_markdown(index=False), ""]
        return lines


# ---------------------------------------------------------------------------
# Run-loop helpers
# ---------------------------------------------------------------------------


# ANSI colors for run-line output — arm colors make plain/codemap pairs easy to scan
_COLOR_PLAIN = "\033[33m"  # yellow
_COLOR_CODEMAP = "\033[36m"  # cyan
_COLOR_FAIL = "\033[31m"  # red — overrides arm color on failure
_COLOR_RESET = "\033[0m"

_ARM_COLOR = {"plain": _COLOR_PLAIN, "codemap": _COLOR_CODEMAP}


def _run_line(run_n: int, total_runs: int, task: Task, model_short: str, arm: str, result: BenchmarkRun) -> str:
    """Format the one-line progress summary printed after each run."""
    error_suffix = f" | ✗ {result.error or 'failed'}" if not result.success else ""
    tc = result.tools
    q = result.quality
    if q.scored:
        erec_part = f"erec={q.erec:4.0%} rrec={q.rrec:4.0%}"
        sc_part = f"  sc={q.skill_coverage:4.0%}" if q.skill_coverage is not None else ""
        quality_suffix = f"\t| {erec_part}{sc_part}"
    else:
        quality_suffix = "\t| quality=n/a"
    return (
        f"[{run_n:0{len(str(total_runs))}}/{total_runs}] {task.id} ({task.type}) | {model_short:<6} | {arm:<7}"
        f"\t| elapsed={result.elapsed_s:7.1f}s"
        f" | tokens={result.input_tokens / 1000:7.1f}k"
        f" | calls={result.tools.total:3}"
        f" (grep={tc.grep:3}; glob={tc.glob:2}; bash={tc.bash:3}; skill={tc.skill:2})"
        f"{quality_suffix}"
        f"{error_suffix}"
    )


# ---------------------------------------------------------------------------
# Benchmark orchestrator
# ---------------------------------------------------------------------------


class Benchmark:
    """Orchestrates the full benchmark run: iterates tasks x arms x models.

    Constructs ``GroundTruth`` internally from the index, manages result accumulation, tool-call
    logging, and snapshot persistence.
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
    ) -> None:
        self.tasks = tasks
        self.arms = arms
        self.models = models
        self.repo_path = repo_path
        self.output_path = output_path
        self.log_path = log_path
        self.gt = GroundTruth(index_path, tasks)
        self.results: list[BenchmarkRun] = []

    def run(self, metadata: dict) -> list[BenchmarkRun]:
        """Execute all benchmark runs and return the accumulated results."""
        total_runs = len(self.tasks) * len(self.arms) * len(self.models)
        run_n = 0
        pbar = tqdm(total=total_runs, unit="run", dynamic_ncols=True)
        for task in self.tasks:
            for model_short, model_id in self.models:
                for arm in self.arms:
                    run_n += 1
                    pbar.set_description(f"{task.id} | {model_short} | {arm}")
                    runner = ModelRunner(model_short, model_id, self.repo_path)
                    result = runner.run(task, arm)
                    # Build corpora for v2 quality scoring
                    exposure_corpus = result.output_text + "\n" + "\n".join(result.codemap_results)
                    report_corpus = result.output_text[result.last_tool_text_offset :]
                    result.quality = self.gt.score(
                        task_id=task.id,
                        output_text=result.output_text,
                        exposure_corpus=exposure_corpus,
                        report_corpus=report_corpus,
                        tool_calls=result.tools.total,
                        skill_result_text=result.skill_result_text or None,
                    )
                    # Codemap arm that never invoked the Skill tool is a failure —
                    # it fell back to grep/bash entirely, defeating the purpose.
                    if arm == "codemap" and result.tools.skill == 0 and result.success:
                        result.success = False
                        result.error = "codemap skill never called"
                    self.results.append(result)
                    self._write_tool_log(result)
                    color = _COLOR_FAIL if not result.success else _ARM_COLOR.get(arm, "")
                    tqdm.write(f"{color}{_run_line(run_n, total_runs, task, model_short, arm, result)}{_COLOR_RESET}")
                    pbar.update(1)
                    self._save_snapshot(metadata)

        pbar.close()
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
        """Overwrite the results JSON with the current snapshot (called after every run)."""
        serialised = []
        for r in self.results:
            d = asdict(r)
            for key in ("skill_result_text", "codemap_results", "last_tool_text_offset"):
                d.pop(key, None)
            serialised.append(d)
        with self.output_path.open("w") as fh:
            json.dump({"metadata": metadata, "results": serialised}, fh, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Codemap skill benchmark — agent exploration cost with vs without structural context."
    )
    parser.add_argument("--repo-path", required=True, type=Path, help="Path to the indexed repo")
    parser.add_argument("--index", type=Path, help="Explicit index path (auto-discovered if omitted)")
    parser.add_argument(
        "--tasks-file",
        type=Path,
        default=Path("benchmarks/tasks-agentic.json"),
        help="Task definition file (default: benchmarks/tasks-agentic.json)",
    )
    parser.add_argument(
        "--model",
        choices=list(MODELS.keys()),
        help="Run a single model tier (default: all three — haiku / sonnet / opus)",
    )
    parser.add_argument(
        "--arm",
        choices=["plain", "codemap"],
        help="Run only one arm (default: both)",
    )
    parser.add_argument("--all", action="store_true", help="Run all tasks in both arms")
    parser.add_argument("--tasks", nargs="+", metavar="ID", help="Run specific task IDs only")
    parser.add_argument("--report", action="store_true", help="Write markdown report alongside JSON")
    parser.add_argument("--output", type=Path, help="JSON output path (auto-named if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without running claude")
    args = parser.parse_args()

    if not args.all and not args.tasks and not args.arm and not args.dry_run:
        parser.error("Specify --all to run everything, or narrow with --tasks / --arm.")

    # ── Load tasks ────────────────────────────────────────────────────────
    if not args.tasks_file.exists():
        sys.exit(f"Tasks file not found: {args.tasks_file}")
    with args.tasks_file.open() as f:
        all_tasks: list[Task] = [
            Task(id=t["id"], type=t["type"], prompt=t["prompt"], primary_module=t.get("primary_module", ""))
            for t in json.load(f)
        ]
    if args.tasks:
        all_tasks = [t for t in all_tasks if t.id in args.tasks]
    if not all_tasks:
        sys.exit("No tasks to run.")

    # ── Locate prerequisites (index existence validated, not measured) ────
    repo_path = args.repo_path.resolve()
    index_path = find_index(repo_path, args.index)

    arms = [args.arm] if args.arm else ["plain", "codemap"]
    models_to_run: list[tuple[str, str]] = [(args.model, MODELS[args.model])] if args.model else list(MODELS.items())
    total_runs = len(all_tasks) * len(arms) * len(models_to_run)

    model_names = ", ".join(m for m, _ in models_to_run)

    # ── Output path + tool-call log ───────────────────────────────────────
    date_slug = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_path = args.output or (RESULTS_DIR / f"code-{date_slug}.json")
    # Tool-call log: one JSON line per run, for post-run investigation of bash commands
    log_dir = Path(".temp") / f"bench-{date_slug}"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tool-calls.jsonl"

    print(f"[→ repo:        {repo_path}]")
    print(f"[→ index:       {index_path}]")
    print(f"[→ models:      {model_names}]")
    print(f"[→ tasks:       {len(all_tasks)}, arms: {len(arms)}, models: {len(models_to_run)}]")
    print(f"[→ total runs:  {total_runs}]")
    print(f"[→ tool log:    {log_path}]")

    if args.dry_run:
        for task in all_tasks:
            for model_short, _ in models_to_run:
                for arm in arms:
                    print(f"  [DRY RUN] {task.id} ({task.type}) | {model_short} | {arm}")
        return

    if "codemap" in arms:
        _sample = ModelRunner._system_prompt("fix", "codemap")
        print(f"[→ codemap arm:  skill + /codemap:query available ({len(_sample)} chars for fix type)]")
    output_path = _unique_path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    metadata = {
        "date": datetime.now(timezone.utc).isoformat(),
        "models": model_names,
        "repo": str(repo_path),
        "index": str(index_path),
        "task_count": len(all_tasks),
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
    )
    print(f"[→ quality gt:   {len(benchmark.gt.expected)} tasks with rdep ground truth]")

    # ── Run ───────────────────────────────────────────────────────────────
    all_results = benchmark.run(metadata)

    # ── Report ────────────────────────────────────────────────────────────
    if args.report:
        report = Report(all_results, all_tasks, {**metadata, "date": date_slug})
        report_md = report.render()
        report_path = output_path.with_suffix(".md")
        report_path.write_text(report_md)
        print(f"\n→ Report: {report_path}")

    print(f"→ Data:   {output_path}")


if __name__ == "__main__":
    main()
