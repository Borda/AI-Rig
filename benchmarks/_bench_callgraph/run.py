"""Run the call-graph census on each pre-registered repository and write JSON plus Markdown.

Per repository, in order: fetch the pinned commit into an empty directory, build a fresh index with the scanner
checkout, collect definitions and call sites of every indexed module, resolve the candidate call sites with the
reference backend, build the truth, draw the stratified sample from the truth's caller counts, and only then query ``fn-
rdeps`` for the sampled targets and score. Fetch, checkout and index helpers come from the import-edge census.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from _bench_census.run import (
    _ENTRY_FILE,
    CensusError,
    RepoCoordinate,
    Scanner,
    _scanner_env,
    build_index,
    fetch_repository,
    load_coordinates,
)

from _bench_callgraph.sample import STRATA, stratified_sample, stratum_of
from _bench_callgraph.score import (
    MissClass,
    Scored,
    adjudication_draw,
    edge_class,
    extra_class,
    instance_base,
    score_sample,
    site_class,
)
from _bench_callgraph.truth import (
    CallSite,
    Definition,
    DefLocation,
    ModuleFacts,
    ReferenceBackend,
    Truth,
    build_truth,
    candidate_names,
    module_facts,
)

_QUERY_TIMEOUT_S = 1800
_SNIPPET_RADIUS = 1

ResolvedSites = dict[str, list[tuple[CallSite, tuple[DefLocation, ...]]]]


@dataclass(frozen=True)
class Registry:
    """The pre-registered census inputs.

    Attributes:
        path: Registry file.
        sha256: SHA-256 of the registry bytes, logged before any scoring.
        coordinates: Pinned repositories.
        seed: Sampling seed.
        per_stratum: Draw per stratum before top-up.
        min_total: Smallest sample per repository.
        adjudicate_per_side: Missed and extra edges drawn per repository for hand adjudication.
    """

    path: Path
    sha256: str
    coordinates: tuple[RepoCoordinate, ...]
    seed: int
    per_stratum: int
    min_total: int
    adjudicate_per_side: int


def load_registry(path: Path) -> Registry:
    """Read and hash the registry.

    Raises:
        CensusError: When the ``sampling`` block is missing or malformed.
    """
    raw = path.read_bytes()
    sampling = json.loads(raw).get("sampling")
    keys = ("seed", "per_stratum", "min_total", "adjudicate_per_side")
    if not isinstance(sampling, dict) or not all(isinstance(sampling.get(key), int) for key in keys):
        raise CensusError(f"{path}: 'sampling' must hold integer {', '.join(keys)}")
    return Registry(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        coordinates=tuple(load_coordinates(path)),
        **{key: sampling[key] for key in keys},
    )


@dataclass(slots=True)
class RepoFacts:
    """Parsed facts of one indexed checkout.

    Attributes:
        modules: Facts per indexed ``ok`` module, sorted by path.
        index_symbols: Every ``module::qualified_name`` the index holds.
        not_indexed: Index module entries whose status is not ``ok``.
        parse_failures: Indexed paths the truth builder could not parse.
    """

    modules: list[ModuleFacts] = field(default_factory=list)
    index_symbols: frozenset[str] = frozenset()
    not_indexed: int = 0
    parse_failures: list[str] = field(default_factory=list)


def collect_facts(clone: Path, index_path: Path) -> RepoFacts:
    """Parse every module the index holds as ``ok``, naming modules exactly as the index does."""
    index = json.loads(index_path.read_text(encoding="utf-8"))
    facts = RepoFacts()
    symbols: set[str] = set()
    for entry in sorted(index.get("modules", []), key=lambda m: m.get("path", "")):
        if entry.get("status") != "ok":
            facts.not_indexed += 1
            continue
        symbols.update(f"{entry['name']}::{sym['qualified_name']}" for sym in entry.get("symbols", []))
        try:
            tree = ast.parse((clone / entry["path"]).read_text(encoding="utf-8"), filename=entry["path"])
        except (SyntaxError, ValueError, OSError):
            facts.parse_failures.append(entry["path"])
            continue
        facts.modules.append(module_facts(tree, entry["name"], entry["path"]))
    facts.index_symbols = frozenset(symbols)
    return facts


def _resolve_job(job: tuple[ReferenceBackend, str, list[tuple[int, int]]]) -> list[tuple[DefLocation, ...]]:
    """Resolve one file's positions; top-level so a process pool can pickle it."""
    backend, path, positions = job
    return backend.resolve(path, positions)


def resolve_sites(backend: ReferenceBackend, facts: RepoFacts, workers: int) -> ResolvedSites:
    """Resolve every candidate call site, grouped by caller (``None`` caller keyed as empty string).

    Args:
        backend: Reference backend; must be picklable when *workers* > 1.
        facts: Parsed repository facts.
        workers: Process count; ``1`` resolves in this process.

    Returns:
        Candidate sites with their answers, per caller.
    """
    targets = frozenset(d.qualified_name.rsplit(".", 1)[-1] for m in facts.modules for d in m.definitions if d.public)
    jobs: list[tuple[ModuleFacts, list[CallSite]]] = []
    for module in facts.modules:
        names = candidate_names(module, targets)
        chosen = [site for site in module.sites if site.callee_name in names]
        if chosen:
            jobs.append((module, chosen))
    payloads = [(backend, module.path, [(s.line, s.column) for s in chosen]) for module, chosen in jobs]
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            answers = list(pool.map(_resolve_job, payloads, chunksize=1))
    else:
        answers = [_resolve_job(payload) for payload in payloads]
    grouped: ResolvedSites = {}
    for (_module, chosen), found in zip(jobs, answers):
        for site, locations in zip(chosen, found):
            grouped.setdefault(site.caller or "", []).append((site, locations))
    return grouped


def truth_from(facts: RepoFacts, resolved: ResolvedSites) -> tuple[Truth, dict[str, Definition]]:
    """Build the truth and the public target table from resolved sites."""
    public = {(d.path, d.line): d for m in facts.modules for d in m.definitions if d.public}
    flat = [pair for pairs in resolved.values() for pair in pairs]
    truth = build_truth([site for site, _ in flat], [found for _, found in flat], public)
    return truth, {d.qname: d for d in public.values()}


def query_fn_rdeps(scanner: Scanner, clone: Path, index_path: Path, targets: Sequence[str], scratch: Path) -> dict:
    """Return ``fn-rdeps`` callers per target from one ``batch`` call of the scanner checkout.

    Raises:
        CensusError: When the query fails, an item is not ``ok``, or a caller list is shorter than its count.
    """
    batch_file = scratch / f"{clone.name}-fn-rdeps-batch.json"
    batch_file.write_text(json.dumps([{"cmd": "fn-rdeps", "args": [t]} for t in targets]), encoding="utf-8")
    command = [sys.executable, str(scanner.plugin_root / _ENTRY_FILE), "query", "--index", str(index_path)]
    command += ["--root", str(clone), "batch", str(batch_file)]
    env = {**_scanner_env(), "CODEMAP_LOGGING": "false"}
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, cwd=clone, env=env, capture_output=True, text=True, timeout=_QUERY_TIMEOUT_S, check=False
    )
    if completed.returncode != 0:
        raise CensusError(f"fn-rdeps batch failed in {clone.name}: {completed.stderr.strip()[-2000:]}")
    payload = json.loads(completed.stdout)
    callers: dict[str, frozenset[str]] = {}
    for target, item in zip(targets, payload["batch"]):
        result = item.get("result") or {}
        if not item.get("ok") or len(result.get("called_by", [])) != result.get("count"):
            raise CensusError(f"fn-rdeps {target} in {clone.name}: not ok or truncated: {json.dumps(item)[:400]}")
        callers[target] = frozenset(entry["caller"] for entry in result["called_by"])
    return {"callers": callers, "index": payload.get("index", {})}


def _snippet(clone: Path, path: str, line: int) -> list[str]:
    """Return source lines around *line*, numbered."""
    lines = (clone / path).read_text(encoding="utf-8").splitlines()
    low, high = max(1, line - _SNIPPET_RADIUS), min(len(lines), line + _SNIPPET_RADIUS)
    return [f"{number}: {lines[number - 1]}" for number in range(low, high + 1)]


@dataclass(frozen=True)
class RepoContext:
    """Everything one repository's classification step reads.

    Attributes:
        clone: Repository checkout.
        facts: Parsed facts.
        resolved: Resolved candidate sites per caller.
        truth: The truth.
        targets: Public target definitions by qname.
    """

    clone: Path
    facts: RepoFacts
    resolved: ResolvedSites
    truth: Truth
    targets: Mapping[str, Definition]

    def imports_of(self, caller: str) -> Mapping[str, str]:
        """Return the import bindings of the caller's module."""
        module = caller.partition("::")[0]
        return next((m.imports for m in self.facts.modules if m.module == module), {})


def classify(context: RepoContext, scored: Scored) -> dict[str, object]:
    """Class every missed and extra edge; report instance-method shares both ways."""
    miss_counts: Counter[str] = Counter()
    strict_instance = 0
    bases: Counter[str] = Counter()
    for target, caller in scored.missed:
        definition, sites = context.targets[target], context.truth.edges[target][caller]
        imports = context.imports_of(caller)
        cls = edge_class(definition, sites, imports)
        miss_counts[cls.value] += 1
        if cls == MissClass.INSTANCE_METHOD:
            bases.update(instance_base(s) for s in sites if site_class(definition, s, imports) == cls)
            strict_instance += all(site_class(definition, s, imports) == cls for s in sites)
    extra_counts = Counter(
        extra_class(context.targets[target], context.resolved.get(caller, [])).value for target, caller in scored.extra
    )
    return {
        "miss_classes": dict(miss_counts.most_common()),
        "instance_method_all_sites": strict_instance,
        "instance_method_site_bases": dict(bases.most_common()),
        "extra_classes": dict(extra_counts.most_common()),
    }


def adjudication(context: RepoContext, scored: Scored, registry: Registry, label: str) -> list[dict[str, object]]:
    """Draw missed and extra edges and attach the source lines a reviewer needs."""
    records: list[dict[str, object]] = []
    for side, edges in (("missed", scored.missed), ("extra", scored.extra)):
        for target, caller in adjudication_draw(edges, registry.adjudicate_per_side, f"{registry.seed}:{label}:{side}"):
            definition = context.targets[target]
            terminal = definition.qualified_name.rsplit(".", 1)[-1]
            if side == "missed":
                sites = context.truth.edges[target][caller]
                cls = edge_class(definition, sites, context.imports_of(caller)).value
            else:
                sites = [s for s, _ in context.resolved.get(caller, []) if s.callee_name == terminal]
                cls = extra_class(definition, context.resolved.get(caller, [])).value
            site = sites[0] if sites else None
            records.append(
                {
                    "side": side,
                    "target": target,
                    "target_def": f"{definition.path}:{definition.line}",
                    "caller": caller,
                    "class": cls,
                    "site": site.to_dict() if site else None,
                    "snippet": _snippet(context.clone, site.path, site.line) if site else [],
                }
            )
    return records


def score_repository(
    coordinate: RepoCoordinate,
    clone: Path,
    index_path: Path,
    backend: ReferenceBackend,
    query: Callable[[Sequence[str]], dict],
    registry: Registry,
    workers: int,
) -> dict[str, object]:
    """Build the truth, draw the sample, then query and score one repository."""
    facts = collect_facts(clone, index_path)
    resolved = resolve_sites(backend, facts, workers)
    truth, targets = truth_from(facts, resolved)
    counts = {qname: truth.caller_count(qname) for qname in targets}
    sample = stratified_sample(
        counts,
        seed=registry.seed,
        label=coordinate.label,
        per_stratum=registry.per_stratum,
        min_total=registry.min_total,
    )
    population = Counter(stratum_of(count) for count in counts.values())
    vocabulary_gaps = sorted({c for callers in truth.edges.values() for c in callers} - facts.index_symbols)
    answer = query([q for members in sample.values() for q in members])
    truth_callers = {q: frozenset(truth.edges.get(q, {})) for q in targets}
    scored = score_sample(sample, population, truth_callers, answer["callers"])
    context = RepoContext(clone, facts, resolved, truth, targets)
    recall, precision = scored.recall(), scored.precision()
    weighted_recall, weighted_precision = scored.weighted()
    return {
        "coordinate": {"label": coordinate.label, "url": coordinate.url, "commit": coordinate.commit},
        "modules_parsed": len(facts.modules),
        "modules_not_indexed": facts.not_indexed,
        "parse_failures": facts.parse_failures,
        "targets": len(targets),
        "population_per_stratum": {label: population.get(label, 0) for label, _l, _h in STRATA},
        "sample": sample,
        "sites": {
            "total": sum(len(m.sites) for m in facts.modules),
            "candidates": sum(len(v) for v in resolved.values()),
            "resolved_to_target": truth.resolved_sites,
            "unresolved": truth.unresolved_sites,
            "resolved_elsewhere": truth.external_sites,
            "module_scope_to_target": sum(len(v) for v in truth.unattributed.values()),
        },
        "vocabulary_gaps": vocabulary_gaps,
        "strata": {label: vars_of(tally) for label, tally in scored.strata.items()},
        "recall": recall.to_dict(),
        "precision": precision.to_dict(),
        "weighted": {"recall": weighted_recall, "precision": weighted_precision},
        "missed": [list(edge) for edge in scored.missed],
        "extra": [list(edge) for edge in scored.extra],
        **classify(context, scored),
        "adjudication": adjudication(context, scored, registry, coordinate.label),
        "fn_rdeps_index": answer["index"],
    }


def vars_of(tally: object) -> dict[str, object]:
    """Serialize a slotted tally."""
    return {name: getattr(tally, name) for name in tally.__slots__}  # type: ignore[attr-defined]


def run_repository(
    coordinate: RepoCoordinate,
    scanner: Scanner,
    workdir: Path,
    backend_factory: Callable[[Path], ReferenceBackend],
    registry: Registry,
    workers: int,
) -> dict[str, object]:
    """Fetch, index and score one repository."""
    clone = fetch_repository(coordinate, workdir / "repos" / coordinate.name).resolve()
    index_path = build_index(scanner, clone)
    scratch = workdir / "queries"
    scratch.mkdir(parents=True, exist_ok=True)
    return score_repository(
        coordinate,
        clone,
        index_path,
        backend_factory(clone),
        lambda targets: query_fn_rdeps(scanner, clone, index_path, targets, scratch),
        registry,
        workers,
    )


def render_markdown(results: Sequence[Mapping[str, object]]) -> str:
    """Render the headline and class tables."""
    from _bench_census.report import Align, format_rate, render_table
    from _bench_census.score import Rate

    rows = []
    for result in results:
        recall, precision = Rate(**_counts(result["recall"])), Rate(**_counts(result["precision"]))
        weighted = result["weighted"]
        strata = result["strata"]
        rows.append(
            [
                result["coordinate"]["label"],
                str(result["targets"]),
                " / ".join(f"{strata[label]['sampled']}" for label, _l, _h in STRATA),
                str(recall.total),
                str(precision.total),
                str(len(result["missed"])),
                str(len(result["extra"])),
                format_rate(recall),
                format_rate(precision),
                _fmt(weighted["recall"]),
                _fmt(weighted["precision"]),
            ]
        )
    headers = [
        "Repository",
        "Targets",
        "Sample 0 / 1 / 2-5 / 6+",
        "Truth edges",
        "fn-rdeps edges",
        "FN",
        "FP",
        "Recall [95% CI]",
        "Precision [95% CI]",
        "Weighted recall",
        "Weighted precision",
    ]
    aligns = [Align.LEFT, *[Align.RIGHT] * 6, Align.LEFT, Align.LEFT, Align.RIGHT, Align.RIGHT]
    return render_table(headers, aligns, rows) + "\n"


def _counts(rate: Mapping[str, object]) -> dict[str, int]:
    """Pick the counts out of a serialized rate."""
    return {"hits": int(rate["hits"]), "total": int(rate["total"])}


def _fmt(value: object) -> str:
    """Format an optional proportion."""
    return "n/a" if value is None else f"{float(value):.3f}"
