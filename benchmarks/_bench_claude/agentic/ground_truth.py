"""Ground-truth construction for agentic answer scoring."""

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional


# Re-exported for call-site/test compatibility (tests reference it via this module's namespace).
from _bench_common.agentic_contracts import (
    AgenticOracle,  # noqa: F401
    AnswerScore,  # noqa: F401
)

# Stage plumbing lives in a private module so this runner stays under the suite's 250 KB maintenance limit.
# Every name it defines is re-exported here, including ones this file no longer calls itself: callers and tests
# reach these through the runner module, so pruning an apparently unused re-export breaks patch.object targets.

from _bench_claude.agentic.models import QualityScore, Task
from _bench_claude.agentic.discovery import _scan_repo_importers


# ---------------------------------------------------------------------------
# Quality scoring — deterministic ground truth
# ---------------------------------------------------------------------------


class GroundTruth:
    """Tool-independent ground truth for quality scoring benchmark runs.

    Loads the codemap index once for centrality metadata, then (when a repo path is given) derives the authoritative
    expected rdep sets from an independent AST scan of the repo — not from the same index the codemap arm queries. The
    index-derived list is retained as a diagnostic; per-task divergences are logged so index blind spots become visible
    instead of invisible. Exposes a ``score()`` method for comparing agent output to truth.
    """

    @staticmethod
    def _build_module_regexes(packages: set[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
        """Build dotted-name and ``src/`` path regexes for the given top-level packages.

        The package set is derived from the benchmark's tasks (each ``primary_module``'s
        first dotted component), so the extractor stays repo-agnostic — no package name is
        hardcoded. For ``pytorch-lightning`` (``packages={"lightning"}``) this reproduces the
        legacy ``\\blightning(?:\\.…)+`` behaviour exactly.

        Args:
            packages: Top-level package names to match (e.g. ``{"lightning"}``).

        Returns:
            ``(module_re, path_re)`` — the dotted-name matcher and the ``src/<pkg>/…\\.py``
            matcher. When ``packages`` is empty, both patterns match nothing.

        Examples:
            >>> mod_re, _ = GroundTruth._build_module_regexes({"lightning"})
            >>> mod_re.findall("see lightning.pytorch.trainer.trainer here")
            ['lightning.pytorch.trainer.trainer']
        """
        if not packages:
            never = re.compile(r"(?!x)x")  # matches nothing
            return never, never
        alt = "|".join(re.escape(p) for p in sorted(packages))
        module_re = re.compile(rf"\b(?:{alt})(?:\.[a-zA-Z_][a-zA-Z0-9_]*)+")
        path_re = re.compile(rf"\bsrc/((?:{alt})(?:/[a-zA-Z_][a-zA-Z0-9_]*)+)\.py\b")
        return module_re, path_re

    def __init__(self, index_path: Path, tasks: list[Task], repo_path: Optional[Path] = None) -> None:
        """Load the index and pre-compute expected rdep sets for each task.

        Args:
            index_path: Path to the pre-built codemap JSON index produced by ``scan-index``.
                Used for module inventory and dep-count centrality only, never as the rdep oracle.
            tasks: Task definitions used to derive per-task ground-truth rdep sets. Tasks
                without a ``primary_module`` are skipped silently.
            repo_path: Repository root. When provided, expected rdeps come from an independent
                AST scan and divergences from the index are logged. When ``None`` (unit tests that
                supply only an index), the index-derived list is used as a fallback.
        """
        with index_path.open() as f:
            index = json.load(f)
        self.all_modules: set[str] = {m["name"] for m in index.get("modules", []) if m.get("status") == "ok"}
        # Top-level package name(s) for the repo under test, derived from the tasks' primary
        # modules (first dotted component) — keeps module extraction repo-agnostic (no hardcode).
        self.packages: set[str] = {
            getattr(t, "primary_module", "").split(".")[0] for t in tasks if getattr(t, "primary_module", "")
        }
        self._module_re, self._path_re = self._build_module_regexes(self.packages)
        self.index_expected: dict[str, set[str]] = self._index_rdeps(index, tasks)
        # Independent oracle: {imported_module: {importer, ...}}; empty when no repo path supplied.
        self.ast_importers: dict[str, set[str]] = _scan_repo_importers(repo_path) if repo_path else {}
        self.expected, self.divergences = self._resolve_expected(tasks, used_ast=bool(repo_path))
        # in-degree = reverse-dependency count per module (how many modules import it), derived from
        # the index import graph. Tasks define "central" as "imported by the most modules" — i.e.
        # in-degree — so top-10 ranks by in-degree, NOT dep_count. dep_count is the forward import
        # count (out-degree) and would rank on the opposite axis, measuring recall on the wrong
        # modules.
        _in_degrees: dict[str, int] = defaultdict(int)
        for m in index.get("modules", []):
            if m.get("status") != "ok":
                continue
            for imported in m.get("direct_imports", []):
                _in_degrees[imported] += 1
        # Top-10 most-central rdeps per task (by in-degree descending); k=min(10, |rdeps|)
        self.top10_expected: dict[str, frozenset[str]] = {}
        for task in tasks:
            rdeps = self.expected.get(task.id, set())
            if rdeps:
                ranked = sorted(rdeps, key=lambda m: _in_degrees.get(m, 0), reverse=True)[:10]
                self.top10_expected[task.id] = frozenset(ranked)
        self.all_leaf_names: set[str] = {m.split(".")[-1] for m in self.all_modules}
        # Match patterns cover the index inventory plus any AST-only expected rdep, so an arm that
        # finds a real importer the index missed can still be credited in the corpus.
        pattern_targets = set(self.all_modules)
        for rdeps in self.expected.values():
            pattern_targets |= rdeps
        self._match_patterns: dict[str, list[re.Pattern]] = {m: self._generate_match_set(m) for m in pattern_targets}
        self._emit_divergences()

    @staticmethod
    def _index_rdeps(index: dict, tasks: list[Task]) -> dict[str, set[str]]:
        """Derive the diagnostic index-based rdep set per task from ``direct_imports``."""
        modules = index.get("modules", [])
        out: dict[str, set[str]] = {}
        for task in tasks:
            pm = getattr(task, "primary_module", "")
            if not pm:
                continue
            out[task.id] = {
                m["name"]
                for m in modules
                if pm in m.get("direct_imports", [])
                and m.get("status") == "ok"
                # is_test is the scanner's own classification and catches non-"tests." roots
                # like tests_pytorch.*; the name-prefix check stays for indexes predating the
                # flag. The AST oracle prunes tests dirs entirely, so without this the same
                # test importers surface as spurious missing_in_ast gt-divergences (BA-16).
                and not m.get("is_test")
                and not m["name"].startswith("tests.")
            }
        return out

    def _resolve_expected(self, tasks: list[Task], used_ast: bool) -> tuple[dict[str, set[str]], dict[str, dict]]:
        """Select expected rdeps (AST when scanned, else index) and record index divergences.

        Args:
            tasks: Task definitions to resolve expected rdeps for.
            used_ast: True when an AST scan ran; its result becomes authoritative.

        Returns:
            ``(expected, divergences)`` where divergences maps task_id to
            ``{"ast", "index", "missing_in_index", "missing_in_ast"}`` for tasks that disagree.
        """
        if not used_ast:
            return dict(self.index_expected), {}
        expected: dict[str, set[str]] = {}
        divergences: dict[str, dict] = {}
        for task in tasks:
            pm = getattr(task, "primary_module", "")
            if not pm:
                continue
            ast_set = {m for m in self.ast_importers.get(pm, set()) if not m.startswith("tests.")}
            expected[task.id] = ast_set
            index_set = self.index_expected.get(task.id, set())
            missing_in_index = ast_set - index_set
            missing_in_ast = index_set - ast_set
            if missing_in_index or missing_in_ast:
                divergences[task.id] = {
                    "ast": len(ast_set),
                    "index": len(index_set),
                    "missing_in_index": sorted(missing_in_index),
                    "missing_in_ast": sorted(missing_in_ast),
                }
        return expected, divergences

    def _emit_divergences(self) -> None:
        """Print one summary line when the AST oracle and index disagree on any task.

        Per-task detail stays in ``self.divergences`` for callers/tests; a non-empty ``missing_in_index`` means the AST
        scan found real importers the index lacks — a potential plugin blind spot and the harness's added diagnostic
        value.
        """
        if not self.divergences:
            return
        missing_in_index = sum(len(d["missing_in_index"]) for d in self.divergences.values())
        missing_in_ast = sum(len(d["missing_in_ast"]) for d in self.divergences.values())
        if missing_in_index and missing_in_ast:
            detail = "index has blind spots and extra entries vs AST oracle"
        elif missing_in_index:
            detail = "index misses real importers the AST oracle found"
        else:
            detail = "index has extra entries the AST oracle excludes (e.g. test modules)"
        print(f"[gt-divergence] {len(self.divergences)}/{len(self.expected)} tasks diverged vs AST oracle ({detail})")

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
        semble_result_text: str | None = None,
    ) -> QualityScore:
        """Compute quality score using multi-form matching and optional coverage lenses.

        Primary metrics (v2):
            ``erec`` — exposure recall on ``exposure_corpus`` (agent output_text only; tool outputs excluded)
            ``rrec`` — report recall on ``report_corpus`` (final answer after last tool call)
            ``delta`` — erec - rrec
            ``deff`` — erec_tp / max(tool_calls, 1)

        Supplementary:
            ``skill_coverage`` — fraction of expected rdeps in the skill result (codemap only)
            ``chunk_hit_rate`` — fraction of expected rdeps whose module/file appears in any
                retrieved semble chunk (semble / combined only); ``None`` when no semble corpus

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

        # erec@10 — exposure recall on top-10 most-central rdeps
        top10 = self.top10_expected.get(task_id)
        if top10:
            top10_tp = sum(1 for r in top10 if self._rdep_found(r, exposure_corpus))
            erec_top10 = top10_tp / len(top10)
            erec_top10_k = len(top10)
        else:
            erec_top10 = erec
            erec_top10_k = n_exp

        # ── Skill coverage (codemap arm only) ──
        # Two capture paths:
        # 1. Agent ran scan-query via Bash → skill_result_text is raw JSON → parse imported_by.
        # 2. Agent used Skill tool → tool returns rendered markdown, one module per line →
        #    extract dotted module names via regex (require ≥1 dot to avoid YAML-key false-positives).
        # Prose error text (blocked, permission denied) → None (unscored), not sc=0%.
        skill_coverage: Optional[float] = None
        skill_returned: Optional[int] = None
        if skill_result_text:
            returned: Optional[set] = None
            # The corpus is usually SEVERAL one-line scan-query JSON objects joined by
            # newlines (one per rdeps call) — whole-text json.loads fails with "Extra data"
            # on the second object, which silently killed sc for every multi-call run.
            # Parse per line and union imported_by across all parseable objects first.
            union: set[str] = set()
            parsed_any = False
            for line in skill_result_text.splitlines():
                line = line.strip()
                if not (line.startswith("{") and line.endswith("}")):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("imported_by"), list):
                    union |= set(data["imported_by"])
                    parsed_any = True
            if parsed_any:
                returned = union
            else:
                try:
                    data = json.loads(skill_result_text)
                    if "imported_by" in data:
                        returned = set(data["imported_by"])
                except (json.JSONDecodeError, AttributeError, TypeError):
                    # Rendered markdown path — extract lines that look like dotted module paths.
                    modules = re.findall(r"^([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+)\s*$", skill_result_text, re.MULTILINE)
                    if modules:
                        returned = set(modules)
            if returned is not None:
                skill_returned = len(returned)
                skill_coverage = len(returned & exp) / n_exp

        # ── Semble chunk-hit rate (semble / combined arm only) ──
        # Module-file granularity: an expected rdep counts as hit if any of its surface forms
        # appears in the concatenated semble search chunks — semantic search need not enumerate
        # exact dotted rdeps to get credit.
        chunk_hit_rate: Optional[float] = None
        if semble_result_text:
            chunk_hits = sum(1 for r in exp if self._rdep_found(r, semble_result_text))
            chunk_hit_rate = chunk_hits / n_exp

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
            erec_top10=erec_top10,
            erec_top10_k=erec_top10_k,
            # Skill coverage
            skill_coverage=skill_coverage,
            skill_returned=skill_returned,
            # Semble-native lens
            chunk_hit_rate=chunk_hit_rate,
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

    def _extract_modules(self, text: str) -> set[str]:
        """Extract dotted package-namespaced module names from agent output.

        Matches only the repo's own top-level packages (``self.packages``, derived from the
        tasks' primary modules), so a non-lightning repo works without editing code.

        Handles two forms agents use:
        - Dotted: ``lightning.pytorch.trainer.trainer``
        - File path: ``src/lightning/pytorch/trainer/trainer.py`` -> converted to dotted
        """
        dotted = set(self._module_re.findall(text))
        from_paths = {m.replace("/", ".") for m in self._path_re.findall(text)}
        return dotted | from_paths
