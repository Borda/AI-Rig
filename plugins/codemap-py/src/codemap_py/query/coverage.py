"""Decide how complete an answer is and render the coverage block attached to it."""

from __future__ import annotations

import json
import re
import sys
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
    if stored_shas:
        # v3 index: precise file-SHA comparison (works correctly for subdirectory repos
        # that share a host repo git history — avoids false-positive stale on unrelated commits)
        current = _current_file_shas()
        if current.shas:
            changed = [p for p in current.shas if stored_shas.get(p) != current.shas[p]]
            added = [p for p in current.shas if p not in stored_shas]
            deleted = [p for p in stored_shas if p not in current.shas]
            stale = bool(changed + added + deleted)
        else:
            # Nothing came back. Only a git failure *inside* a repository is an anomaly:
            # with no repository at all there was never an answer to get, so that stays
            # silent. Either way `stale` is not evidence of freshness, and the
            # git-failure case says so out loud rather than defaulting to "current".
            stale = False
            undetermined = current.status == _SHAS_GIT_ERROR
    else:
        # v2 index fallback: timestamp-based check (mirrors warn_if_stale behaviour; avoids
        # false-positive stale when index lives in a subdirectory of a larger host repo whose
        # HEAD SHA changes on every unrelated commit)
        scanned_at: str = index.get("scanned_at", "")
        stale = bool(scanned_at and check_staleness(scanned_at))

    # Blind spot: brand-new untracked .py files are invisible to the git-blob SHA diff,
    # so they never register as "stale". A whole-graph / global-in query must not claim
    # completeness while one exists — surfaced here, consumed by _query_complete.
    # Files that ARE in the index despite being git-untracked (scan-index walks the
    # filesystem, not git) are covered by the graph, so they must not veto — the
    # 2026-07 usage audit found permanently-untracked scratch files (demo/*.py)
    # vetoing completeness forever. Their residual blind spot — an edit after the
    # scan is invisible to the SHA diff — is closed by the mtime check below.
    untracked_all = index_io._untracked_py_files()
    indexed_paths = {m.get("path", "") for m in modules}
    untracked = [p for p in untracked_all if p not in indexed_paths]
    if not stale:
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


def _query_complete(
    base: dict, *, command: str, module_status: str | None, module_name: str | None
) -> tuple[bool, str]:
    """Decide direction-scoped completeness for one command, with a matching reason.

    HARD RULE: never return ``(True, ...)`` for a global-in or whole-graph query
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

    Args:
        base: the shared coverage dict from :func:`_coverage`.
        command: the scan-query subcommand name (``args.command``).
        module_status: for local queries, the queried module's ``status`` field
            (``"ok"``, ``"degraded"``, or None when it is not in the index).
        module_name: for local queries, the queried module's dotted name — used to
            check whether it is itself a colliding name.

    Returns:
        ``(complete, reason)`` where ``reason`` is a short slug naming the veto
        source (``"ok"`` when complete) so the caller can emit a consistent note.
    """
    # a root-mismatched index describes a DIFFERENT project — no direction
    # (not even a local module read) can be a complete answer here. Vetoes first,
    # ahead of staleness, since the whole graph is off-target.
    if base.get("root_mismatch"):
        return False, "root_mismatch"
    # Staleness poisons every direction: even a local module's own entry may be stale.
    if base["stale"]:
        return False, "stale"
    # Staleness that could not be measured is not the same as staleness ruled out —
    # claiming a complete answer here would rest on a git call that never returned.
    if base.get("stale_undetermined"):
        return False, "stale_undetermined"
    if command in _LOCAL_DIRECTION_CMDS:
        return _local_complete(base, module_status=module_status, module_name=module_name)
    return _wide_complete(base)


def _local_complete(base: dict, *, module_status: str | None, module_name: str | None) -> tuple[bool, str]:
    """Completeness for a local (module-scoped) query.

    See :func:`_query_complete`.
    """
    if module_status != "ok":
        return False, "module_degraded"
    # Untracked files never affect a single module's own entry → not a local veto.
    # A collision vetoes only when THIS module's name is the ambiguous one.
    if module_name is not None and module_name in base["_collision_names"]:
        return False, "collision"
    return True, "ok"


def _wide_complete(base: dict) -> tuple[bool, str]:
    """Completeness for a global-in / whole-graph query.

    See :func:`_query_complete`.
    """
    for key, reason in (("degraded", "degraded"), ("untracked_py", "untracked"), ("collision_count", "collision")):
        if base[key]:
            return False, reason
    return True, "ok"


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
) -> str:
    """Build a human note that never contradicts the emitted ``query_complete`` or the answer's own ``hint``.

    F1: the note must track the direction-scoped flag, not the direction-agnostic
    stale/degraded facts alone — otherwise an untracked/collision veto could ship
    "This result is complete" next to ``query_complete: false``.

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

    Examples:
        >>> _coverage_note({"total_modules": 3}, complete=True, reason="ok").endswith("verification is not needed.")
        True
        >>> "not needed" in _coverage_note({"total_modules": 3}, complete=True, reason="ok", answer_hint=True)
        False
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
    detail = {
        "stale": "the index is stale (source files changed since last scan); a bounded self-heal was attempted. "
        "Re-run /codemap-py:scan-codebase to update.",
        "stale_undetermined": "git could not be queried, so whether the index is stale is UNKNOWN — "
        "this answer may describe an out-of-date tree. Re-run /codemap-py:scan-codebase, "
        "or verify with grep.",
        "module_degraded": "the queried module failed to parse and was skipped; verify with grep.",
        "degraded": f"{base['degraded']} module(s) failed to parse and were skipped — "
        "see the degraded_files list (each with its parse error) for the files that may hide an edge into this result.",
        "untracked": f"{len(base['untracked_py'])} new .py file(s) are untracked and invisible to the staleness "
        "diff — they may hide an edge; git add them and re-scan, or verify with grep.",
        "collision": f"{base['collision_count']} qualname collision(s) in the index dropped a module — "
        "it may hide an edge; verify with grep.",
        "root_mismatch": "the index was built for a different project root than the one queried "
        "(--root or CWD differs from the index's scan_root) — this result describes another tree. "
        "Re-scan the current root, or query with a matching --root.",
        "symbol_alias_ambiguous": (
            "a rejected top-level alias path may hide a caller of this symbol; "
            + (
                f"the compact result shows {_COMPACT_ALIAS_LIMITATION_LIMIT} of {alias_limitations_total} "
                "symbol_alias_limitations records — Run without --compact to inspect every alias and reason, "
                "then verify that path with grep."
                if alias_limitations_truncated
                else "see symbol_alias_limitations for the alias and reason, then verify that path with grep."
            )
        ),
    }.get(reason, "a structural blind spot may hide an edge; verify with grep.")
    return prefix + detail


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
    git_root = index_io._get_git_root_cached()
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
    if (time.time() * 1000) - ts > _SESSION_MARKER_TTL_MS:
        return None
    return sid


def _coverage_already_emitted(session_id: str) -> bool:
    """Return True if the full coverage block was already emitted this session.

    Uses a per-session sentinel file in the OS temp dir (NOT the git-root marker,
    which the hook owns) keyed on *session_id*. Absent → this is the session's first
    query: create the sentinel and return False (caller emits the full block).
    Present → a prior query in the same session already emitted it: return True
    (caller emits the compact block). Any filesystem error falls back to "not
    emitted" so a broken sentinel yields a full block rather than a silent compact.

    Args:
        session_id: the fresh session id from :func:`_read_session_marker`.
    """
    import tempfile

    safe = re.sub(r"[^A-Za-z0-9_-]", "-", session_id)
    sentinel = Path(tempfile.gettempdir()) / f"codemap-coverage-{safe}"
    try:
        if sentinel.exists():
            return True
        sentinel.touch()
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
    complete, reason = _query_complete(
        base, command=resolved_command, module_status=module_status, module_name=module_name
    )
    alias_limitations = (
        _symbol_alias_limitations(index)
        if resolved_command == "fn-central"
        else _alias_limitations_for_target(index, query_target)
    )
    if resolved_command in {"fn-blast", "fn-central", "fn-rdeps"} and alias_limitations:
        complete, reason = False, "symbol_alias_ambiguous"
    compact_mode = _should_compact_coverage()
    alias_payload = _compact_alias_limitations(alias_limitations) if compact_mode and alias_limitations else {}
    note = _coverage_note(
        base,
        complete=complete,
        reason=reason,
        alias_limitations_total=len(alias_limitations),
        alias_limitations_truncated=bool(alias_payload.get("symbol_alias_limitations_truncated")),
        answer_hint=answer_hint,
    )
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
        # count and the note that says WHY. A complete result needs neither. The
        # per-file degraded detail stays FULL-block only (diet keeps counts only).
        if not complete:
            compact["degraded"] = emitted["degraded"]
            compact["note"] = note
            compact["completeness_reason"] = reason
        # Provenance survives the diet even though it is session-invariant: it is what
        # lets a consumer prove it read the index it thinks it read, and a consumer that
        # only ever sees compacted blocks would otherwise never see it at all.
        if state._LOADED_INDEX_PATH:
            compact["index_path"] = state._LOADED_INDEX_PATH
        return {**compact, **extra, **alias_payload}
    relevant = _degraded_relevant(base, query_target if query_target is not None else module_name)
    full = {
        **emitted,
        "note": note,
        "query_complete": complete,
        "exhaustive": complete,
        # Machine-readable veto slug ("ok" when complete) — debrief aggregates WHY
        # completeness fails per project instead of regex-mining the human note.
        "completeness_reason": reason,
    }
    if state._LOADED_INDEX_PATH:
        full["index_path"] = state._LOADED_INDEX_PATH
    if relevant:
        full["degraded_relevant"] = relevant
    return {**full, **extra, **({"symbol_alias_limitations": alias_limitations} if alias_limitations else {})}
