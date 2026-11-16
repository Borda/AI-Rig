"""Decide how complete an answer is and render the coverage block attached to it."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path

from codemap_py import query_state as state

# Transitional seam: exclusion rules live in codemap_py.scanner, but this
# module still reaches them through the old bare-name ``_exclusions`` import
# (bin/_exclusions.py, itself a shim onto codemap_py.scanner) via a
# bin/-relative sys.path insert, the same route bin/scan-index used to take.
# Every other import below is a direct package-internal import.
# parents[3] not [2]: this file sits one level deeper than the pre-split query.py
#: Plugin bin/ directory, added to sys.path so the _exclusions shim can be imported.
_BIN = Path(__file__).resolve().parents[3] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))
from codemap_py.telemetry import runtime_id  # noqa: E402

# Reached through the module, not a bound name: tests patch these on the defining
# module (monkeypatch.setattr(query.index_io, ...)), which a `from .index_io import`
# binding here would not see.
from . import index_io  # noqa: E402
from .index_io import (  # noqa: E402
    _SHAS_GIT_ERROR,
    _alias_limitations_for_target,
    _current_file_shas,
    _has_call_graph,
    _indexed_untracked_modified,
    _symbol_alias_limitations,
    check_staleness,
)

# Blind spots disclosed in every import-graph result's ``not_covered`` field.
# Relative imports and known ``from package import submodule`` edges are resolved
# during scanning; dynamic import forms remain outside the static import graph.
#: Dynamic import forms listed in the not_covered field of every import-graph result.
_IMPORT_GRAPH_NOT_COVERED = [
    "importlib.import_module",
    "__import__",
    "lazy-loading",
]


# Blind spots disclosed in every static call-graph result's ``not_covered``
# field. Relative import aliases are resolved during scope construction.
#: Dispatch styles listed in the not_covered field of every static call-graph result.
_CALL_GRAPH_NOT_COVERED = [
    "dynamic-dispatch",
    "hook-callbacks",
    "string-dispatch",
]


_coverage_cache: dict | None = None


def _coverage(index: dict) -> dict:
    """Return the shared, direction-independent coverage state for a query result.

    Computes the base facts every command shares — module counts, degraded set,
    staleness, and index-level blind spots (untracked ``.py`` files, qualname
    collisions). Direction-scoped completeness (``query_complete``) is layered on
    top per command by :func:`_cmd_coverage`; this function never decides it.

    Args:
        index: parsed codemap index dict.
    """
    global _coverage_cache
    if _coverage_cache is not None:
        return _coverage_cache

    modules = index.get("modules", [])
    total = sum(1 for m in modules if m.get("status") == "ok")
    # Each degraded module carries its own parse error in ``reason``; surface it per
    # file so a caller sees WHICH file broke and WHY, instead of a blanket "verify with
    # grep". Sorted by path for deterministic output. Missing ``reason`` (older index)
    # falls back to a generic label rather than an empty string.
    degraded_files = sorted(
        (
            {"path": m.get("path", ""), "error": m.get("reason", "") or "parse error (no detail recorded)"}
            for m in modules
            if m.get("status") == "degraded"
        ),
        key=lambda d: d["path"],
    )
    degraded = len(degraded_files)
    total_syms = sum(len(m.get("symbols", [])) for m in modules if m.get("status") == "ok")
    star_modules = sum(1 for m in modules if m.get("status") == "ok" and m.get("has_star_imports"))
    has_call_graph = _has_call_graph(index)

    stored_shas = index.get("file_shas")
    undetermined = False
    if isinstance(stored_shas, dict):
        current = _current_file_shas()
        changed = index_io._changed_py_files(index)
        stale = bool(changed)
        undetermined = current.status == _SHAS_GIT_ERROR
    else:
        # v2 index fallback: timestamp-based check (mirrors warn_if_stale behaviour; avoids
        # false-positive stale when index lives in a subdirectory of a larger host repo whose
        # HEAD SHA changes on every unrelated commit)
        scanned_at: str = index.get("scanned_at", "")
        stale = bool(scanned_at and check_staleness(scanned_at))

    # Retain the legacy unindexed-source diagnostic for frozen or unmeasurable indexes. Working-tree hashes now
    # drive freshness; the old timestamp heuristic is needed only for indexes without a file hash map.
    untracked_all = index_io._untracked_py_files()
    indexed_paths = {m.get("path", "") for m in modules}
    untracked = [p for p in untracked_all if p not in indexed_paths]
    if not stale and not isinstance(stored_shas, dict):
        stale = _indexed_untracked_modified(
            [p for p in untracked_all if p in indexed_paths], index.get("scanned_at", "")
        )

    # scan-index may write a `collisions` list of
    # {name, kept, dropped} into index meta. Older indexes lack the key — treat missing as
    # empty, never crash on absence. Keep the resolved names so a local query can check
    # whether ITS module is the colliding one.
    collisions = index.get("collisions", []) or []
    collision_names = frozenset(str(c.get("name", "")) for c in collisions if isinstance(c, dict))

    _coverage_cache = {
        "total_modules": total,
        "total_symbols": total_syms,
        "degraded": degraded,
        "degraded_files": degraded_files,
        "star_import_modules": star_modules,
        "has_call_graph": has_call_graph,
        "stale": stale,
        "untracked_py": untracked,
        "collision_count": len(collisions),
        "root_mismatch": state._root_mismatch,
        # Internal: consumed by _query_complete for the local-collision check; not emitted.
        "_collision_names": collision_names,
    }
    # Added only when git failed inside a repository, so a caller that never hits that
    # path sees the block it always saw. Its presence is the honest "we could not tell"
    # signal that `stale: false` on its own cannot express.
    if undetermined:
        _coverage_cache["stale_undetermined"] = True
    return _coverage_cache


# Command → direction class. Drives which incompleteness sources can hide a result:
#   local      — answer read straight from the queried module's own entry; only that
#                module's parse status matters.
#   global-in  — answer aggregates edges pointing INTO a target; any degraded file
#                anywhere could hide an inbound edge → complete iff degraded == 0.
#   whole-graph— answer ranges over the entire graph; complete iff degraded == 0.
# Anything unlisted defaults to whole-graph (the strictest: never over-claims).
# Local = module-scoped read from ONE named module's own entry: `deps` (that module's
# imports) and `symbols` (that module's symbols). `symbol <name>` is NOT local — it
# matches by name across the whole graph, so a degraded module could hide another
# definition; it falls through to whole-graph.
#: Commands whose answer comes from one module's own entry, so only that module's parse status matters.
_LOCAL_DIRECTION_CMDS = frozenset({"deps", "symbols"})


#: Commands that aggregate inbound edges across the graph, where any degraded file could hide an edge.
_GLOBAL_IN_DIRECTION_CMDS = frozenset({"rdeps", "fn-rdeps", "mock-rdeps", "test-impact"})


# One table drives both the human ``note`` and the machine ``remedies`` field, so the two texts cannot drift. A note
# segment is ``diagnosis + remedy``; a single-reason note therefore keeps the exact wording it had before the table
# existed. A diagnosis may interpolate counts (``{degraded}``, ``{untracked}``, ``{collisions}``) but never a name. A
# remedy is a fixed, identity-free template — no file, module, symbol or path — because the telemetry record keeps the
# full query result and ``bin/anonymize.py`` only scrubs qualified-name tokens out of ``remedies``.
#: Completeness reducer slug mapped to its ``(diagnosis, remedy)`` pair.
_REDUCERS: dict[str, tuple[str, str]] = {
    "root_mismatch": (
        "the index was built for a different project root than the one queried "
        "(--root or CWD differs from the index's scan_root) — this result describes another tree. ",
        "Re-scan the current root, or query with a matching --root.",
    ),
    "stale": (
        "the index is stale (source files changed since last scan). ",
        "Re-run /codemap-py:scan-codebase to update.",
    ),
    "stale_undetermined": (
        "source fingerprints could not be read, so whether the index is stale is UNKNOWN — "
        "this answer may describe an out-of-date tree. ",
        "Re-run /codemap-py:scan-codebase, or verify with grep.",
    ),
    "module_degraded": ("the queried module failed to parse and was skipped; ", "verify with grep."),
    "degraded": (
        "{degraded} module(s) failed to parse and were skipped — ",
        "see the degraded_files list (each with its parse error) for the files that may hide an edge into this result.",
    ),
    "untracked": (
        "{untracked} new .py file(s) are not represented in the index and may hide an edge; ",
        "re-scan or verify with grep.",
    ),
    # A full scan is required: exclusions do not trigger incremental rebuilds, so an incremental scan would keep the
    # already-indexed copy that the new exclusion is meant to drop.
    "collision": (
        "{collisions} qualname collision(s) in the index dropped a module — it may hide an edge; ",
        "the collisions list in the index file (see index_path) names each kept and dropped path — exclude the "
        "unwanted copy ([tool.codemap] exclude or .codemapignore) or list its intended root first in [tool.codemap] "
        "src_roots, then run a full /codemap-py:scan-codebase without --incremental.",
    ),
    "symbol_alias_ambiguous": (
        "a rejected top-level alias path may hide a caller of this symbol; ",
        "see symbol_alias_limitations for the alias and reason, then verify that path with grep.",
    ),
}

#: Remedy for ``symbol_alias_ambiguous`` when the compact block carries only a bounded sample of the alias records.
_TRUNCATED_ALIAS_REMEDY = (
    "the compact result shows {shown} of {total} symbol_alias_limitations records — Run without --compact to inspect "
    "every alias and reason, then verify that path with grep."
)

#: Diagnosis and remedy for a reducer slug the table does not name, so an unknown veto still carries an action.
_UNKNOWN_REDUCER = ("a structural blind spot may hide an edge; ", "verify with grep.")

#: Separator between reducer segments when more than one reducer makes a result incomplete.
_NOTE_SEGMENT_JOIN = " Also: "

# ``dynamic_sites`` is informational, never a completeness reducer: it neither flips ``query_complete`` nor rewords the
# ``note``. Its text is one fixed, identity-free sentence (no file, module, symbol, path or dotted token), because the
# telemetry record keeps the full query result and ``bin/anonymize.py`` passes keys it does not list through unchanged;
# counts and the scope slug travel as separate keys.
#: Fixed instruction attached to every ``dynamic_sites`` record.
_DYNAMIC_SITES_CHECK = (
    "Call sites in the searched modules pass a non-literal value to import_module, __import__ or getattr on an "
    "indexed module, so the module or attribute each one reaches is unknown and missing from this answer; inspect "
    "those call sites (search for import_module, __import__ and getattr) before treating the answer as complete."
)


def _module_dynamic_sites(module: object) -> int:
    """Return one index module's ``dynamic_sites`` count, reading the field defensively (malformed counts as 0)."""
    value = module.get("dynamic_sites", 0) if isinstance(module, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _dynamic_sites_payload(index: dict, *, command: str, module_name: str | None) -> dict:
    """Return the ``dynamic_sites`` record scoped to this query's search set, or ``{}`` when it holds none.

    A module-local query (``deps``, ``symbols``) searches one module's own entry, so only that module's sites count;
    every other query searches the whole index, so every module's sites count.

    Args:
        index: parsed codemap index dict.
        command: the scan-query subcommand name.
        module_name: queried module for module-local commands.

    Examples:
        >>> idx = {"modules": [{"name": "a", "dynamic_sites": 2}, {"name": "b", "dynamic_sites": 1}, {"name": "c"}]}
        >>> _dynamic_sites_payload(idx, command="deps", module_name="a")["dynamic_sites"]["count"]
        2
        >>> rec = _dynamic_sites_payload(idx, command="rdeps", module_name=None)["dynamic_sites"]
        >>> rec["count"], rec["modules"], rec["scope"]
        (3, 2, 'index')
        >>> _dynamic_sites_payload(idx, command="deps", module_name="c")
        {}
    """
    modules = index.get("modules", [])
    modules = modules if isinstance(modules, list) else []
    scope = "index"
    if command in _LOCAL_DIRECTION_CMDS and module_name:
        scope = "module"
        modules = [m for m in modules if isinstance(m, dict) and m.get("name") == module_name]
    counts = [count for count in map(_module_dynamic_sites, modules) if count]
    if not counts:
        return {}
    return {
        "dynamic_sites": {"count": sum(counts), "modules": len(counts), "scope": scope, "check": _DYNAMIC_SITES_CHECK}
    }


def _completeness_reasons(base: dict, *, command: str, module_status: str | None, module_name: str | None) -> list[str]:
    """Return every active completeness reducer for one command, in veto-priority order.

    HARD RULE: never return an empty list for a global-in or whole-graph query
    while ``degraded > 0`` — a false ``query_complete`` arms guard-redundant-scan
    against the exact grep that would surface the missing edge.

    The veto sources differ by direction:

    * **local** (``deps``/``symbols``): the answer is read straight from ONE
      module's own index entry. An untracked new ``.py`` file elsewhere cannot
      change that entry's ``direct_imports`` / symbols, so untracked files do NOT
      veto local. A qualname collision vetoes local only when the queried module's
      OWN name is the colliding one. Staleness still vetoes (the entry itself may
      be out of date), as does the module failing to parse.
    * **global-in / whole-graph**: any degraded file, untracked file, or collision
      anywhere can hide an inbound / graph-wide edge → all veto.

    Every reducer that applies is returned, not only the first, so each one keeps its remedy when several co-occur.
    The first entry is the reducer :func:`_query_complete` reports, so ``completeness_reason`` is unchanged.

    Args:
        base: the shared coverage dict from :func:`_coverage`.
        command: the scan-query subcommand name (``args.command``).
        module_status: for local queries, the queried module's ``status`` field
            (``"ok"``, ``"degraded"``, or None when it is not in the index).
        module_name: for local queries, the queried module's dotted name — used to
            check whether it is itself a colliding name.

    Returns:
        Reducer slugs in priority order; empty when the answer is complete.

    Examples:
        >>> base = {"stale": True, "degraded": 0, "untracked_py": ["n.py"], "collision_count": 1}
        >>> _completeness_reasons(base, command="rdeps", module_status=None, module_name=None)
        ['stale', 'untracked', 'collision']
        >>> local = {"stale": False, "_collision_names": frozenset({"pkg.a"})}
        >>> _completeness_reasons(local, command="deps", module_status="ok", module_name="pkg.b")
        []
    """
    reasons: list[str] = []
    # a root-mismatched index describes a DIFFERENT project — no direction
    # (not even a local module read) can be a complete answer here. Vetoes first,
    # ahead of staleness, since the whole graph is off-target.
    if base.get("root_mismatch"):
        reasons.append("root_mismatch")
    # Staleness poisons every direction: even a local module's own entry may be stale.
    if base["stale"]:
        reasons.append("stale")
    # Staleness that could not be measured is not the same as staleness ruled out —
    # claiming a complete answer here would rest on a git call that never returned.
    # Measured staleness already says more than "unknown", so the two never both apply.
    elif base.get("stale_undetermined"):
        reasons.append("stale_undetermined")
    if command in _LOCAL_DIRECTION_CMDS:
        reasons.extend(_local_reasons(base, module_status=module_status, module_name=module_name))
    else:
        reasons.extend(_wide_reasons(base))
    return reasons


def _first_veto(reasons: list[str]) -> tuple[bool, str]:
    """Collapse an ordered reducer list to the legacy ``(complete, reason)`` verdict.

    Args:
        reasons: reducer slugs in priority order, as from :func:`_completeness_reasons`.

    Examples:
        >>> _first_veto(["stale", "untracked"])
        (False, 'stale')
        >>> _first_veto([])
        (True, 'ok')
    """
    return (False, reasons[0]) if reasons else (True, "ok")


def _query_complete(
    base: dict, *, command: str, module_status: str | None, module_name: str | None
) -> tuple[bool, str]:
    """Decide direction-scoped completeness for one command, with the highest-priority reason.

    See :func:`_completeness_reasons` for the per-direction veto rules; this keeps the original single-reason shape.

    Args:
        base: the shared coverage dict from :func:`_coverage`.
        command: the scan-query subcommand name (``args.command``).
        module_status: for local queries, the queried module's ``status`` field.
        module_name: for local queries, the queried module's dotted name.

    Returns:
        ``(complete, reason)`` where ``reason`` is a short slug naming the veto
        source (``"ok"`` when complete) so the caller can emit a consistent note.
    """
    return _first_veto(
        _completeness_reasons(base, command=command, module_status=module_status, module_name=module_name)
    )


def _local_reasons(base: dict, *, module_status: str | None, module_name: str | None) -> list[str]:
    """Return the local-direction reducers (module parse failure, own-name collision) in priority order.

    Args:
        base: the shared coverage dict from :func:`_coverage`.
        module_status: the queried module's ``status`` field.
        module_name: the queried module's dotted name.
    """
    reasons: list[str] = []
    if module_status != "ok":
        reasons.append("module_degraded")
    # Untracked files never affect a single module's own entry → not a local veto.
    # A collision vetoes only when THIS module's name is the ambiguous one.
    if module_name is not None and module_name in base["_collision_names"]:
        reasons.append("collision")
    return reasons


def _wide_reasons(base: dict) -> list[str]:
    """Return the global-in / whole-graph reducers (degraded, untracked, collision) in priority order.

    Args:
        base: the shared coverage dict from :func:`_coverage`.
    """
    sources = (("degraded", "degraded"), ("untracked_py", "untracked"), ("collision_count", "collision"))
    return [reason for key, reason in sources if base[key]]


def _local_complete(base: dict, *, module_status: str | None, module_name: str | None) -> tuple[bool, str]:
    """Completeness for a local (module-scoped) query.

    See :func:`_query_complete`.
    """
    return _first_veto(_local_reasons(base, module_status=module_status, module_name=module_name))


def _wide_complete(base: dict) -> tuple[bool, str]:
    """Completeness for a global-in / whole-graph query.

    See :func:`_query_complete`.
    """
    return _first_veto(_wide_reasons(base))


def _remedy_for(reason: str, *, alias_limitations_total: int = 0, alias_limitations_truncated: bool = False) -> str:
    """Return the one-line, identity-free remedy for one completeness reducer.

    Args:
        reason: the reducer slug.
        alias_limitations_total: number of relevant rejected alias paths.
        alias_limitations_truncated: whether compact output emits only the bounded alias sample.

    Examples:
        >>> _remedy_for("stale")
        'Re-run /codemap-py:scan-codebase to update.'
        >>> _remedy_for("not-a-reducer")
        'verify with grep.'
    """
    if reason == "symbol_alias_ambiguous" and alias_limitations_truncated:
        return _TRUNCATED_ALIAS_REMEDY.format(shown=_COMPACT_ALIAS_LIMITATION_LIMIT, total=alias_limitations_total)
    return _REDUCERS.get(reason, _UNKNOWN_REDUCER)[1]


def _coverage_remedies(
    reasons: list[str], *, alias_limitations_total: int = 0, alias_limitations_truncated: bool = False
) -> list[dict[str, str]]:
    """Return one ``{reason, remedy}`` entry per active reducer, in the same order as *reasons*.

    Args:
        reasons: reducer slugs in priority order.
        alias_limitations_total: number of relevant rejected alias paths.
        alias_limitations_truncated: whether compact output emits only the bounded alias sample.

    Examples:
        >>> _coverage_remedies(["untracked"])
        [{'reason': 'untracked', 'remedy': 're-scan or verify with grep.'}]
        >>> _coverage_remedies([])
        []
    """
    return [
        {
            "reason": reason,
            "remedy": _remedy_for(
                reason,
                alias_limitations_total=alias_limitations_total,
                alias_limitations_truncated=alias_limitations_truncated,
            ),
        }
        for reason in reasons
    ]


def _target_path_tokens(query_target: str | None) -> list[str]:
    """Return path-shaped tokens for *query_target*, for degraded-file relevance matching.

    A query names a module (``mypkg.auth``) or a symbol (``mypkg.auth::login``). Its
    source file lives at a matching path (``mypkg/auth.py``). This yields the tokens a
    degraded file's path is likely to share with that target: the dotted module turned
    into a path fragment (``mypkg/auth``) and its leaf name (``auth``). Empty targets
    and pure symbol suffixes yield nothing.

    Args:
        query_target: the module or ``module::symbol`` string the command queried,
            or None when the command has no single target (e.g. ``central``).

    Examples:
        >>> _target_path_tokens("mypkg.auth::login")
        ['mypkg/auth', 'auth']
        >>> _target_path_tokens("utils")
        ['utils']
        >>> _target_path_tokens(None)
        []
    """
    if not query_target:
        return []
    module = query_target.split("::", 1)[0]
    if not module:
        return []
    as_path = module.replace(".", "/")
    leaf = module.rsplit(".", 1)[-1]
    tokens = [as_path]
    if leaf and leaf != as_path:
        tokens.append(leaf)
    return tokens


def _degraded_relevant(base: dict, query_target: str | None) -> list[dict]:
    """Return the degraded files whose path overlaps the query target, most-relevant first.

    "Relevant" means the degraded file's path shares a path fragment with the queried
    module/symbol — a strong hint that THIS query's answer, specifically, may be hiding
    an edge in that unparsed file (versus the general "some file elsewhere is degraded"
    signal the ``degraded`` count already carries). Path-prefix / leaf-name overlap is a
    heuristic, not a guarantee, so the caller frames it as a hint, never a veto.

    Args:
        base: the shared coverage dict from :func:`_coverage` (source of
            ``degraded_files``, each ``{path, error}``).
        query_target: the module or ``module::symbol`` this command queried.
    """
    tokens = _target_path_tokens(query_target)
    if not tokens:
        return []
    relevant = []
    for entry in base.get("degraded_files", []):
        path = entry.get("path", "")
        if any(tok in path for tok in tokens):
            relevant.append(entry)
    return relevant


def _coverage_note(
    base: dict,
    *,
    complete: bool,
    reason: str,
    alias_limitations_total: int = 0,
    alias_limitations_truncated: bool = False,
    answer_hint: bool = False,
    reasons: list[str] | None = None,
) -> str:
    """Build a human note that never contradicts the emitted ``query_complete`` or the answer's own ``hint``.

    F1: the note must track the direction-scoped flag, not the direction-agnostic
    stale/degraded facts alone — otherwise an untracked/collision veto could ship
    "This result is complete" next to ``query_complete: false``.

    An incomplete note carries one ``diagnosis + remedy`` segment per active reducer, taken from the same table as the
    ``remedies`` field, so co-occurring reducers each keep their remedy.

    A zero-caller answer stays complete for the static graph while its ``hint`` says the real callers are unresolved
    and, for most methods, names a reference search. Telling that reader "grep/bash verification is not needed" in the
    same payload contradicted the hint, so a hinted complete answer defers to the hint instead.

    Args:
        base: the shared coverage dict from :func:`_coverage`.
        complete: the decided ``query_complete`` value for this command.
        reason: the veto slug from :func:`_query_complete` (``"ok"`` when complete).
        alias_limitations_total: number of relevant rejected alias paths.
        alias_limitations_truncated: whether compact output emits only the bounded sample.
        answer_hint: whether the command's payload carries a zero-caller ``hint`` beside this coverage block.
        reasons: every active reducer in priority order (``reason`` first); defaults to ``[reason]``.

    Examples:
        >>> _coverage_note({"total_modules": 3}, complete=True, reason="ok").endswith("verification is not needed.")
        True
        >>> "not needed" in _coverage_note({"total_modules": 3}, complete=True, reason="ok", answer_hint=True)
        False
        >>> base = {"total_modules": 3, "untracked_py": ["n.py"], "collision_count": 1}
        >>> note = _coverage_note(base, complete=False, reason="untracked", reasons=["untracked", "collision"])
        >>> "re-scan or verify with grep." in note and "without --incremental." in note
        True
    """
    total = base["total_modules"]
    if complete and answer_hint:
        return (
            f"All {total} indexed modules were searched. This result is complete for static call edges only — zero "
            "callers here is unresolved, not absent; follow the answer's hint before treating the symbol as unused."
        )
    if complete:
        return f"All {total} indexed modules were searched. This result is complete — grep/bash verification is not needed."
    prefix = f"All {total} indexed modules were searched. ⚠ This result may be incomplete — "
    counts = {
        "degraded": base.get("degraded", 0),
        "untracked": len(base.get("untracked_py", [])),
        "collisions": base.get("collision_count", 0),
    }
    segments = [
        _REDUCERS.get(active, _UNKNOWN_REDUCER)[0].format(**counts)
        + _remedy_for(
            active,
            alias_limitations_total=alias_limitations_total,
            alias_limitations_truncated=alias_limitations_truncated,
        )
        for active in (reasons or [reason])
    ]
    return prefix + _NOTE_SEGMENT_JOIN.join(segments)


_SESSION_MARKER_TTL_MS = 30 * 60 * 1000  # 30 min — matches the hook writer's guard


#: Maximum number of alias limitation records shown in a compact coverage block.
_COMPACT_ALIAS_LIMITATION_LIMIT = 8


_coverage_full_keys = (
    "total_modules",
    "total_symbols",
    "degraded",
    "degraded_files",
    "star_import_modules",
    "has_call_graph",
    "untracked_py",
    "collision_count",
)


def _read_session_marker() -> str | None:
    """Return the current session id from the hook-written marker, or None if absent.

    Cross-layer contract (the hook writes, scan-query reads): each host marker lives at
    ``<git-root>/.cache/codemap/current-session-<runtime>.json`` and holds single-line JSON
    ``{"session_id": "<id>", "ts": <epoch-ms>}``. Any of missing file, unparsable
    JSON, missing/empty ``session_id``, or a ``ts`` older than
    :data:`_SESSION_MARKER_TTL_MS` is treated as "no marker" so the caller falls back
    to the full coverage block (fail-verbose). scan-query never writes this file.

    Returns:
        The marker's ``session_id`` when present and fresh, else None.
    """
    git_root = index_io._fingerprint_root or index_io._get_git_root_cached()
    if git_root is None:
        return None
    runtime = runtime_id()
    if runtime not in {"claude", "codex"}:
        return None
    marker = git_root / ".cache" / "codemap" / f"current-session-{runtime}.json"
    try:
        raw = marker.read_text(encoding="utf-8", errors="replace").strip()
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return _valid_session_id(data)


def _valid_session_id(data: object) -> str | None:
    """Return the marker's ``session_id`` when *data* is a well-formed, still-fresh marker dict.

    Args:
        data: the JSON-decoded marker payload (expected ``{"session_id": str, "ts": epoch-ms}``).
    """
    if not isinstance(data, dict):
        return None
    sid = data.get("session_id")
    ts = data.get("ts")
    if not sid or not isinstance(sid, str):
        return None
    if not isinstance(ts, (int, float)):
        return None
    now = time.time() * 1000
    if not 0 <= ts <= now or now - ts > _SESSION_MARKER_TTL_MS:
        return None
    return sid


def _coverage_already_emitted(session_id: str) -> bool:
    """Return True if the full coverage block was already emitted this session.

    Uses a per-session sentinel file in a private user directory under the OS temp dir (NOT the git-root marker,
    which the hook owns) keyed on *session_id*. Absent → this is the session's first
    query: create the sentinel and return False (caller emits the full block).
    Present → a prior query in the same session already emitted it: return True
    (caller emits the compact block). Any filesystem error falls back to "not
    emitted" so a broken sentinel yields a full block rather than a silent compact.

    Args:
        session_id: the fresh session id from :func:`_read_session_marker`.
    """
    identity = str(os.getuid()) if hasattr(os, "getuid") else str(Path.home())
    user_key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / f"codemap-coverage-{user_key}"
    sentinel = directory / hashlib.sha256(session_id.encode()).hexdigest()
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return False
        if hasattr(os, "getuid"):
            if info.st_uid != os.getuid():
                print(f"codemap: coverage directory has a foreign owner: {directory}", file=sys.stderr)
                return False
            if info.st_mode & 0o077:
                directory.chmod(0o700)
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        return False
    except FileExistsError:
        try:
            return stat.S_ISREG(sentinel.lstat().st_mode)
        except OSError:
            return False
    except OSError:
        return False


def _should_compact_coverage() -> bool:
    """Decide whether to emit the compact coverage block for this invocation.

    Compact only when ALL hold: ``--verbose-coverage`` was not passed, a fresh
    same-session marker exists, and the full block was already emitted earlier this
    session. Any failure of those conditions → full block (fail-verbose).
    """
    if state._force_compact_coverage:
        return True
    if state._verbose_coverage:
        return False
    session_id = _read_session_marker()
    if session_id is None:
        return False
    return _coverage_already_emitted(session_id)


def _compact_alias_limitations(records: list[dict[str, str]]) -> dict[str, object]:
    """Return bounded alias evidence while preserving the exact limitation count.

    Compact query output must remain safe to place directly in an agent context:
    global commands can otherwise repeat every persisted ambiguous-alias record.
    The sample is deterministic because ``_symbol_alias_limitations`` sorts records.
    A caller can always omit ``--compact`` to obtain the lossless full record list.
    """
    total = len(records)
    shown = records[:_COMPACT_ALIAS_LIMITATION_LIMIT]
    truncated = total > len(shown)
    payload: dict[str, object] = {
        "symbol_alias_limitations": shown,
        "symbol_alias_limitations_total": total,
        "symbol_alias_limitations_truncated": truncated,
    }
    if truncated:
        payload["symbol_alias_limitations_hint"] = (
            "Run without --compact to inspect every symbol_alias_limitations record."
        )
    return payload


def _cmd_coverage(
    index: dict,
    *,
    command: str = "",
    module_status: str | None = None,
    module_name: str | None = None,
    query_target: str | None = None,
    answer_hint: bool = False,
    **extra: object,
) -> dict:
    """Merge shared coverage with direction-scoped completeness and per-command metadata.

    Emits both the forward ``query_complete`` field (direction-scoped, per this
    command) and the legacy ``exhaustive`` field (kept byte-compatible for one
    deprecation cycle so existing consumers keep parsing). For local queries pass
    ``module_status`` and ``module_name``; whole-graph/global-in queries ignore them.

    Degraded-file surfacing: the full block carries ``degraded_files`` (each
    ``{path, error}``) so a caller sees which files failed to parse and why, rather
    than a blanket "verify with grep". When the query names a target whose path
    overlaps a degraded file, a ``degraded_relevant`` subset is added — the files most
    likely to hide an edge in THIS answer specifically. ``query_target`` defaults to
    ``module_name`` so local commands need not pass it twice.

    diet: after the first query in a session the shared, session-invariant keys
    (module counts, degraded_files, star imports, etc.) are dropped and only the
    per-query honesty signals survive — ``query_complete``, ``stale``,
    ``root_mismatch``, plus ``degraded`` count and ``note`` when the result is
    incomplete (the reason for incompleteness is never compacted away). The full
    ``degraded_files`` / ``degraded_relevant`` detail is FULL-block only; the compact
    block keeps the ``degraded`` count alone.

    Args:
        index: parsed codemap index dict.
        command: the scan-query subcommand name, used to pick the direction class.
        module_status: queried module's ``status`` for local-direction commands.
        module_name: queried module's dotted name for local-direction commands.
        query_target: module or ``module::symbol`` this command queried, for
            degraded-file relevance; defaults to ``module_name`` when omitted.
        answer_hint: the payload carries a zero-caller ``hint`` beside this block; only rewords the
            complete-answer ``note`` (see :func:`_coverage_note`) and is never emitted itself.
        **extra: per-command fields (method, not_covered, hint, scope, etc.).
    """
    base = _coverage(index)
    resolved_command = command or state._CMD
    reasons = _completeness_reasons(
        base, command=resolved_command, module_status=module_status, module_name=module_name
    )
    alias_limitations = (
        _symbol_alias_limitations(index)
        if resolved_command == "fn-central"
        else _alias_limitations_for_target(index, query_target)
    )
    # The alias veto leads (it names this symbol's own blind spot); the reducers behind it keep their remedies.
    if resolved_command in {"fn-blast", "fn-central", "fn-rdeps"} and alias_limitations:
        reasons = ["symbol_alias_ambiguous", *reasons]
    complete, reason = _first_veto(reasons)
    compact_mode = _should_compact_coverage()
    alias_payload = _compact_alias_limitations(alias_limitations) if compact_mode and alias_limitations else {}
    alias_total = len(alias_limitations)
    alias_truncated = bool(alias_payload.get("symbol_alias_limitations_truncated"))
    note = _coverage_note(
        base,
        complete=complete,
        reason=reason,
        alias_limitations_total=alias_total,
        alias_limitations_truncated=alias_truncated,
        answer_hint=answer_hint,
        reasons=reasons,
    )
    # Present only when incomplete, so a complete block keeps exactly the keys it always had.
    remedy_payload = (
        {
            "remedies": _coverage_remedies(
                reasons, alias_limitations_total=alias_total, alias_limitations_truncated=alias_truncated
            )
        }
        if reasons
        else {}
    )
    # Present whenever the search set holds a non-literal dynamic site, complete or not, in both blocks.
    dynamic_payload = _dynamic_sites_payload(index, command=resolved_command, module_name=module_name)
    # Drop the internal collision-names set from the emitted block; keep it out of JSON.
    emitted = {k: v for k, v in base.items() if k != "_collision_names"}
    if compact_mode:
        compact = {
            "query_complete": complete,
            "stale": emitted["stale"],
            "root_mismatch": emitted["root_mismatch"],
            "compact": True,
        }
        # Honesty signal must survive the diet: when incomplete, keep the degraded
        # count, the note that says WHY and every reducer's remedy. A complete result
        # needs none of them. The per-file degraded detail stays FULL-block only
        # (diet keeps counts only).
        if not complete:
            compact["degraded"] = emitted["degraded"]
            compact["note"] = note
            compact["completeness_reason"] = reason
            compact.update(remedy_payload)
        # Provenance survives the diet even though it is session-invariant: it is what
        # lets a consumer prove it read the index it thinks it read, and a consumer that
        # only ever sees compacted blocks would otherwise never see it at all.
        if state._LOADED_INDEX_PATH:
            compact["index_path"] = state._LOADED_INDEX_PATH
        return {**compact, **dynamic_payload, **extra, **alias_payload}
    relevant = _degraded_relevant(base, query_target if query_target is not None else module_name)
    full = {
        **emitted,
        "note": note,
        "query_complete": complete,
        "exhaustive": complete,
        # Machine-readable veto slug ("ok" when complete) — debrief aggregates WHY
        # completeness fails per project instead of regex-mining the human note.
        "completeness_reason": reason,
        # Every active reducer with its one-line remedy, not only the first veto; absent when complete.
        **remedy_payload,
    }
    if state._LOADED_INDEX_PATH:
        full["index_path"] = state._LOADED_INDEX_PATH
    if relevant:
        full["degraded_relevant"] = relevant
    full.update(dynamic_payload)
    return {**full, **extra, **({"symbol_alias_limitations": alias_limitations} if alias_limitations else {})}
